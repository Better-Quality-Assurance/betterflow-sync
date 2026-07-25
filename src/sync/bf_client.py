"""BetterFlow API client - syncs events to BetterFlow server."""

import hashlib
import json
import logging
import platform
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

import requests

try:
    from .. import __version__
    from ..config import DEFAULT_API_URL, PrivacySettings, get_machine_uuid
    from .http_client import BaseApiClient, BetterFlowClientError, BetterFlowAuthError
    from .privacy_filter import partition_excluded
    from .retry import RetryConfig
except ImportError:
    from src import __version__
    from config import DEFAULT_API_URL, PrivacySettings, get_machine_uuid
    from sync.http_client import BaseApiClient, BetterFlowClientError, BetterFlowAuthError
    from sync.privacy_filter import partition_excluded
    from sync.retry import RetryConfig

__all__ = [
    "BetterFlowClient",
    "BetterFlowClientError",
    "BetterFlowAuthError",
    "DeviceInfo",
    "AuthResult",
    "SyncResult",
]

logger = logging.getLogger(__name__)

AGENT_VERSION = __version__


@dataclass
class DeviceInfo:
    """Information about this device.

    All fields are resolved at ``collect()`` time so the object is a pure
    data container with no hidden I/O.
    """

    hostname: str
    os_name: str
    os_version: str
    agent_version: str
    machine_id: str  # Persistent UUID, resolved once at collect() time

    @classmethod
    def collect(cls, agent_version: str = AGENT_VERSION) -> "DeviceInfo":
        """Collect device information."""
        return cls(
            hostname=platform.node(),
            os_name=platform.system(),
            os_version=platform.release(),
            agent_version=agent_version,
            machine_id=get_machine_uuid(),
        )

    @property
    def device_name(self) -> str:
        return f"{self.hostname} ({self.os_name})"

    @property
    def platform_key(self) -> str:
        """Map OS name to backend platform enum."""
        mapping = {"Darwin": "darwin", "Windows": "win32", "Linux": "linux"}
        return mapping.get(self.os_name, "linux")


@dataclass
class AuthResult:
    """Result of authentication."""

    success: bool
    device_id: Optional[str] = None
    api_token: Optional[str] = None
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    user_role: Optional[str] = None
    error: Optional[str] = None


@dataclass
class SyncResult:
    """Result of event sync."""

    success: bool
    events_synced: int = 0
    events_queued: int = 0
    error: Optional[str] = None
    accepted_ids: list[int] = field(default_factory=list)
    # True when a failed sync was TRANSIENT (server down / 5xx / timeout / no
    # delivery confirmation) rather than a definitive rejection of the batch (a
    # 4xx). The queue uses this to decide whether to count a retry toward the
    # drop threshold: a transient failure must NOT, or a long outage drops good
    # activity (2026-06-30). Only meaningful when success is False.
    transient: bool = False


class BetterFlowClient(BaseApiClient):
    """Client for syncing events to BetterFlow server.

    Inherits HTTP functionality from BaseApiClient.
    Provides domain-specific methods for:
    - Authentication (exchange_code, revoke)
    - Event sync (send_events, start_session, end_session)
    - Configuration (get_config, get_projects, update_project_mapping)
    """

    def __init__(
        self,
        api_url: str = DEFAULT_API_URL,
        web_base_url: Optional[str] = None,
        token: Optional[str] = None,
        device_id: Optional[str] = None,
        compress: bool = True,
        timeout: int = 30,
        retry_config: Optional[RetryConfig] = None,
        excluded_apps_provider: Optional[Callable[[], Iterable[str]]] = None,
    ):
        """Initialize BetterFlow client.

        Args:
            api_url: BetterFlow API base URL
            web_base_url: Optional explicit web app base URL (for browser auth)
            token: API token for authentication
            device_id: Device ID from registration
            compress: Use gzip compression for event batches
            timeout: Request timeout in seconds
            retry_config: Configuration for retry with exponential backoff
            excluded_apps_provider: Callable returning the CURRENT excluded-app
                list, consulted on every send so a live config edit takes effect
                immediately (a snapshotted list would keep egressing an app the
                user just excluded). When omitted, the shipped defaults apply:
                an unwired client still honours the baseline guarantee rather
                than failing open.
        """
        super().__init__(
            api_url=api_url,
            web_base_url=web_base_url,
            token=token,
            device_id=device_id,
            compress=compress,
            timeout=timeout,
            retry_config=retry_config,
        )
        self._excluded_apps_provider = excluded_apps_provider

    def _excluded_apps(self) -> Iterable[str]:
        """Current excluded-app list. Fails CLOSED: if the provider is missing
        or raises, fall back to the shipped defaults instead of transmitting
        everything."""
        provider = self._excluded_apps_provider
        if provider is None:
            return PrivacySettings().exclude_apps
        try:
            apps = provider()
        except Exception:
            logger.warning(
                "excluded-apps provider failed — falling back to the shipped "
                "exclusion defaults for this send",
                exc_info=True,
            )
            return PrivacySettings().exclude_apps
        if apps is None:
            return PrivacySettings().exclude_apps
        return apps

    # =========================================================================
    # Authentication
    # =========================================================================

    def exchange_code(
        self,
        code: str,
        device_name: str,
        code_verifier: str,
        device_info: Optional[DeviceInfo] = None,
    ) -> AuthResult:
        """Exchange an authorization code for a Sanctum token.

        Args:
            code: 64-char authorization code from browser flow
            device_name: Name for this device token
            code_verifier: PKCE code verifier (always required — desktop apps
                are public OAuth clients so PKCE is the sole protection
                against authorization code interception)
            device_info: Pre-collected device info (avoids redundant collect)

        Returns:
            AuthResult with api_token on success
        """
        url = f"{self.web_base_url}/api/v1/sync/auth/token"
        parsed_url = urlparse(url)
        if parsed_url.scheme != "https" and parsed_url.hostname not in ("localhost", "127.0.0.1"):
            return AuthResult(success=False, error="Token exchange requires HTTPS")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.USER_AGENT,
        }
        if device_info is None:
            device_info = DeviceInfo.collect()
        payload = {
            "code": code,
            "device_name": device_name,
            "platform": device_info.platform_key,
            "os_version": device_info.os_version,
            "machine_id": device_info.machine_id,
            "hostname": device_info.hostname,
            "agent_version": AGENT_VERSION,
        }
        payload["code_verifier"] = code_verifier

        try:
            with self._session_lock:
                session = self._session
            if session is None:
                return AuthResult(success=False, error="Client has been closed")
            response = session.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            # 409 = device already registered to another account (a duplicate
            # machine_id, e.g. a cloned VM image). Include it here so the user
            # sees the server's explanatory message instead of a bare
            # "HTTP error: 409" from the raise_for_status() path below.
            if response.status_code in (400, 401, 403, 409, 422):
                try:
                    data = response.json()
                    msg = data.get("message", data.get("error", "Authentication failed"))
                except Exception:
                    msg = response.text or response.reason or f"HTTP {response.status_code}"
                return AuthResult(success=False, error=msg)
            response.raise_for_status()
            data = response.json()
            user = data.get("user", {})
            return AuthResult(
                success=True,
                api_token=data["access_token"],
                device_id=str(data["device_id"]) if data.get("device_id") else device_name,
                user_email=user.get("email"),
                user_name=user.get("name"),
                user_role=user.get("role", "user"),
            )
        except requests.exceptions.ConnectionError:
            return AuthResult(success=False, error="Cannot connect to BetterFlow")
        except requests.exceptions.Timeout:
            return AuthResult(success=False, error="Request timed out")
        except requests.exceptions.HTTPError as e:
            return AuthResult(success=False, error=f"HTTP error: {e.response.status_code}")
        except (KeyError, ValueError) as e:
            return AuthResult(success=False, error=f"Invalid response: {e}")

    def revoke(self) -> bool:
        """Revoke this device's token.

        Returns True only when the server confirms the revoke. A 401/403
        (BetterFlowAuthError) is not treated as success — it could be a
        genuine auth failure rather than a prior revocation, and the
        caller must know to keep a scheduled retry.
        """
        try:
            self._request("POST", "revoke")
            return True
        except BetterFlowAuthError as e:
            logger.warning("Revoke returned auth error: %s", e)
            return False
        except BetterFlowClientError as e:
            logger.warning("Revoke failed: %s", e)
            return False

    # =========================================================================
    # Event Sync
    # =========================================================================

    def send_events(self, events: list[dict]) -> SyncResult:
        """Send a batch of events to BetterFlow.

        Args:
            events: List of event dictionaries with timestamp, duration, bucket_id, data

        Returns:
            SyncResult with success status and count
        """
        if not events:
            return SyncResult(success=True, events_synced=0)

        # THE PRIVACY EGRESS CHOKEPOINT. This is the one function that puts
        # events on the wire, so it is the one place the excluded-app guarantee
        # can be enforced for EVERY producer — external AW buckets, the
        # in-process window/input sources, status spans, the call detector, the
        # offline-queue drain, and whatever is added next. Enforcing it in the
        # producers instead is how the in-process sources shipped able to
        # egress 1Password titles. Do not move this below the request.
        events, dropped = partition_excluded(events, self._excluded_apps())
        dropped_ids = [e.get("id") for e in dropped if e.get("id") is not None]
        if dropped:
            # Never log the titles/URLs of an excluded app — the app names are
            # the most that may be recorded, and only locally.
            logger.info(
                "Privacy filter: dropped %d event(s) for excluded app(s) %s "
                "before egress",
                len(dropped),
                ", ".join(sorted({a for a in (
                    e.get("data", {}).get("app") for e in dropped
                ) if a})),
            )
        if not events:
            # Nothing left to transmit. Report SUCCESS with the dropped ids
            # reported as accepted: exclusion is a permanent local decision, so
            # callers must retire these events (drop the queue row, advance the
            # checkpoint) rather than retry them forever.
            return SyncResult(
                success=True, events_synced=0, accepted_ids=dropped_ids
            )

        try:
            # Idempotency key prevents duplicate processing when the same batch
            # is re-sent — either the retry loop after the connection drops once
            # the server has already processed it (N1), or a re-queue/resend on a
            # transient failure. It MUST be derived from the batch content (not a
            # fresh random UUID per call), so every resend of the same events
            # carries the same key and the server can dedup it. A random key
            # defeated the whole mechanism and produced duplicate/inflated hours.
            idempotency_key = hashlib.sha256(
                json.dumps(events, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            response = self._request(
                "POST", "events/batch",
                data={"events": events},
                compress=True,
                extra_headers={"X-Idempotency-Key": idempotency_key},
            )
            # Trust ONLY an explicit delivery confirmation from the server.
            #
            # The server wraps its payload in an envelope: the real counts live
            # under response["data"] ({"processed": N, "failed": M, ...}), NOT at
            # the top level. The previous code read response["processed"] (always
            # absent) and so ALWAYS fell back to the len(events) default —
            # reporting "all sent" no matter what the server actually stored.
            #
            # Worse: when the server 500s on the first attempt and the retry
            # returns a 2xx with an empty / confirmation-less body (an idempotent
            # replay, or a body we don't recognise), _request returns {} and the
            # old default again claimed full success. The sync checkpoint then
            # advanced past events the server never persisted and they were lost
            # for good (the offline queue stayed empty because we "succeeded").
            #
            # So: parse the envelope, accept both naming conventions, and when we
            # CANNOT confirm delivery, report failure. The caller re-queues the
            # batch and the content-derived idempotency key makes the resend safe.
            payload = response.get("data", response) if isinstance(response, dict) else {}
            accepted_ids = payload.get("accepted_ids") or []
            synced = payload.get("processed", payload.get("synced"))
            queued = payload.get("failed", payload.get("queued", 0))

            # No delivery confirmation — do not assume anything persisted. The
            # server was reached but gave no per-event verdict, so this is
            # transient (an idempotent replay, an unrecognised body), NOT a
            # definitive rejection: hold the batch, don't count it toward the
            # drop threshold. Covers both a confirmation-less body (synced None)
            # AND an explicit zero-verdict (processed:0, failed:0) on a 2xx — the
            # latter is just as ambiguous and must not silently drop good events.
            no_confirmation = (
                not accepted_ids
                and (synced is None or synced == 0)
                and not queued
            )
            if no_confirmation:
                return SyncResult(
                    success=False,
                    events_synced=0,
                    events_queued=len(events),
                    error="server returned no delivery confirmation; re-queuing batch",
                    transient=True,
                )

            events_synced = synced if synced is not None else len(accepted_ids)
            # Delivered = the server rejected nothing (failed == 0) AND gave some
            # positive confirmation (processed > 0 or accepted_ids) for a non-empty
            # batch. Do NOT require processed >= len(events): the server reports
            # fewer "processed" than sent when a batch already has some events, or
            # carries the same id more than once (the backlog reconcile can enqueue
            # duplicates). Those are accepted, not failures — requiring processed >=
            # len made every such batch look failed → re-queued, retried, dropped,
            # and the queue stalled in backoff (2026-06-16). The processed>0 clause
            # still rejects a genuine no-op (processed==0) so we never advance past
            # events the server didn't actually take.
            delivered = queued == 0 and (events_synced > 0 or bool(accepted_ids) or not events)
            return SyncResult(
                success=delivered,
                events_synced=events_synced,
                events_queued=queued,
                # Privacy-dropped ids ride along ONLY when the server gave a
                # per-event verdict. Callers use accepted_ids to decide what not
                # to re-queue, and a dropped event must never be re-queued; when
                # there is no verdict (accepted_ids empty) the caller holds the
                # whole batch and the next attempt re-drops them harmlessly.
                accepted_ids=(
                    list(accepted_ids) + dropped_ids if accepted_ids else accepted_ids
                ),
            )
        except BetterFlowAuthError:
            raise  # Callers must handle token refresh / re-login
        except BetterFlowClientError as e:
            # Transient unless the server gave a definitive 4xx rejection. The
            # error classifies itself (5xx / timeout / connection / DNS / 429 =>
            # transient; a non-retryable 4xx => definitive) so the queue knows
            # whether to count it toward the drop threshold.
            return SyncResult(success=False, error=str(e), transient=e.is_transient)

    def start_session(self) -> dict:
        """Start a tracking session."""
        return self._request("POST", "sessions/start")

    def end_session(self, reason: str = "app_quit") -> dict:
        """End the current tracking session.

        Args:
            reason: Reason for ending (user_logout, idle_timeout, app_quit, crash)
        """
        return self._request("POST", "sessions/end", data={"reason": reason})

    # Lightweight retry for heartbeat (N14): 1 retry, 5s timeout
    _HEARTBEAT_RETRY = RetryConfig(max_retries=1, base_delay=1.0, max_delay=5.0)

    # Health/metadata keys forwarded from the caller's dict onto the heartbeat
    # body. Anything not listed here is dropped at this boundary — a field can be
    # collected, logged and unit-tested end to end and still never reach the
    # server because its name is missing from this tuple. Membership is tested
    # with ``in``, never truthiness: ``hardware_serial: None`` is a meaningful
    # report ("this device has no readable serial"), not an absent value.
    HEARTBEAT_HEALTH_KEYS = (
        "idle_tracker_stale_restarts",
        "idle_tracker_blind",
        "inproc_afk",
        "afk_event_age_seconds",
        "window_event_age_seconds",
        "consecutive_sync_failures",
        "idle_while_active_detections",
        "sync_stale_seconds",
        # Tri-state (True/False/null) — null means "no window events to
        # judge", which is NOT the same as False ("events but no titles").
        # Forwarded verbatim; the membership test is `in`, not truthiness,
        # so a null survives the wire.
        "window_titles_captured_recently",
        # Stable hardware identifier joining this device to the MDM asset
        # inventory. str | None — see src/hardware_serial.py.
        "hardware_serial",
        # Record that this device was shown, and the user acknowledged, the
        # current data-collection notice: {version, acknowledged_at}. The Law
        # 190/2018 art. 5 lit. b evidence of prior information. The key name and
        # payload shape are the SERVER's contract — AgentHeartbeatController
        # reads `disclosure_acknowledgement` and stores it in
        # agent_disclosure_acknowledgements; do not rename either end alone.
        # The device is identified by the authenticated heartbeat context, so
        # the payload deliberately carries no device id. See
        # src/privacy_notice.py.
        "disclosure_acknowledgement",
        # The two "this device records NOTHING" flags. aw_manager computes both
        # (see its health payload) precisely to catch a device that heartbeats,
        # authenticates and looks healthy while capturing zero — and they were
        # absent from this tuple, so they never left the machine and the backend
        # could not grade the device degraded. Laszlo Fabian Raul's device 50 sat
        # in exactly that state for two full days on 1.5.116: bf-data-service
        # could not start at all ([Errno 86] Bad CPU type), both work days
        # recorded 0 seconds, and tracking_degraded stayed 0 the whole time.
        #
        # Forwarded as tri-state, like window_titles_captured_recently above: the
        # membership test below is `in`, not truthiness, so a False survives the
        # wire and can CLEAR a degraded episode rather than latching it.
        "tracker_download_failed",
        "managed_components_unavailable",
        # Third field of the same shape, found while fixing the two above: the
        # window watcher has stayed blind across repeated restarts (the macOS
        # Accessibility counterpart of idle_tracker_blind, which IS forwarded).
        # Computed into health_snapshot() and dropped here, so the backend could
        # see a blind idle tracker but never a blind window tracker. Same
        # category as its sibling — the agent reporting on its own watchers.
        "window_tracker_blind",
    )

    def heartbeat(
        self,
        agent_version: str = AGENT_VERSION,
        health: Optional[dict] = None,
    ) -> dict:
        """Send heartbeat to server.

        Uses a shorter timeout (5s) and fewer retries (1) to avoid
        blocking the sync loop when the server is slow (N14).

        ``health`` carries optional agent-health telemetry (idle-tracker
        restart count, AFK/window event ages, consecutive sync failures) so the
        backend can mark a device tracking_degraded even while it reports
        "Active". Only known keys are forwarded; everything else is ignored.

        Returns server commands (pause/deregister) and config update flag.
        """
        data = {
            "agent_version": agent_version,
            "timezone": self._detect_timezone(),
        }
        if health is not None:
            for key in self.HEARTBEAT_HEALTH_KEYS:
                if key in health:
                    data[key] = health[key]

        return self._request(
            "POST", "heartbeat", data=data,
            timeout_override=5,
            retry_config_override=self._HEARTBEAT_RETRY,
        )

    @staticmethod
    def _detect_timezone() -> str:
        """Detect local IANA timezone name, falling back to UTC offset."""
        import os
        from datetime import datetime, timezone as tz

        # macOS/Linux: read /etc/localtime symlink
        try:
            link = os.readlink("/etc/localtime")
            # e.g., /var/db/timezone/zoneinfo/Europe/Bucharest
            if "zoneinfo/" in link:
                return link.split("zoneinfo/")[1]
        except (OSError, IndexError):
            pass

        # Windows: use tzlocal if available
        try:
            from tzlocal import get_localzone
            return str(get_localzone())
        except ImportError:
            pass

        # Fallback: UTC offset like "+03:00"
        offset = datetime.now(tz.utc).astimezone().strftime("%z")  # "+0300"
        return f"{offset[:3]}:{offset[3:]}"  # "+03:00"

    def get_status(self) -> dict:
        """Get sync status (non-critical, short timeout)."""
        return self._request("GET", "events/status", retry=False, timeout_override=10)

    def get_trends(self) -> dict:
        """Get weekly/monthly trend summaries (non-critical, short timeout)."""
        return self._request("GET", "events/trends", retry=False, timeout_override=10)

    def upload_logs(self, log_tail: bytes, relaunch_tail: Optional[bytes] = None) -> dict:
        """Upload the agent's log tail(s) in response to a server logs_requested
        flag (admin diagnostics). POST /api/agent/logs (multipart).

        ``log`` (betterflow.log) is required by the server; ``relaunch_log`` is
        sent only when present (``None`` or empty ``b""`` are both omitted). The
        server keeps the last 512 KB per file and clears logs_requested_at on
        success. Not retried here — if it fails the flag stays set and the next
        heartbeat re-attempts.

        Privacy note: this is an admin-initiated diagnostic pull. The log can
        contain app names, the device hostname (in bucket ids), and OS usernames
        in stack-trace paths — but NOT window titles, URLs, or auth tokens
        (those are never logged). Acceptable within the tenant-admin trust model.
        """
        files: dict = {"log": ("betterflow.log", log_tail, "text/plain")}
        if relaunch_tail:
            files["relaunch_log"] = ("self-update-relaunch.log", relaunch_tail, "text/plain")
        return self._request("POST", "logs", files=files, retry=False)

    # =========================================================================
    # Web login passthrough
    # =========================================================================

    def get_web_login_url(self) -> Optional[str]:
        """Mint a one-time URL that opens the web dashboard already authenticated.

        Backs the "Show My Hours" tray action: the server trades this device's
        token for a short-lived, single-use URL so the user lands on /agent/my
        without logging in again (a pain for multi-Google-account users).

        Returns the URL, or None if the server response lacks one.
        """
        resp = self._request("POST", "web-login-link", retry=False, timeout_override=10)
        data = resp.get("data") or {}
        url = data.get("url")
        return url if isinstance(url, str) and url else None

    # =========================================================================
    # Configuration
    # =========================================================================

    def get_config(self) -> dict:
        """Get configuration from server, UNWRAPPED.

        The API wraps every payload in BaseApiController::successResponse ->
        {"success": true, "message": ..., "data": {...}}, and _request returns that
        envelope verbatim — each caller unwraps for itself (see send_events, which
        does exactly this at the `payload = response.get("data", response)` line).

        This method forgot to, and handed the whole envelope to
        Config.update_from_server(), which looks for TOP-LEVEL keys ("privacy",
        "tracking", "sync", "working_hours"). None of them ever matched. So no
        server-side configuration has ever been applied to any agent: not the AFK
        timeout, not the privacy flags, and not the working-hours schedule — which
        is why enforcement never worked, quite apart from the dict-vs-dataclass bug
        downstream of it. The failure was totally silent: update_from_server ends
        with save(), so the agent dutifully wrote the unchanged config back to disk
        and logged "Server configuration applied".

        This became load-bearing the moment capture went fail-closed: an agent that
        can never learn its schedule has known=False forever, so it suppresses
        capture forever and records nothing at all.
        """
        response = self._request("GET", "config")
        if not isinstance(response, dict):
            return {}
        # A wrapped body carrying data:null (or a non-dict data) must not reach
        # Config.update_from_server, which indexes it with `"privacy" in payload`
        # and would raise TypeError on None — surfacing as a failed sync cycle.
        payload = response.get("data", response)
        return payload if isinstance(payload, dict) else {}

    def get_projects(self) -> list[dict]:
        """Get list of projects for app mapping."""
        return self._request("GET", "projects")

    def update_project_mapping(self, app_name: str, project_id: int) -> dict:
        """Update app to project mapping.

        Args:
            app_name: Application name to map
            project_id: Project ID to assign
        """
        return self._request(
            "POST",
            "config/project-mapping",
            data={"app_name": app_name, "project_id": project_id},
        )
