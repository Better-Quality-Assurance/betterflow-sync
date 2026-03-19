"""BetterFlow API client - syncs events to BetterFlow server."""

import logging
import platform
import uuid
from dataclasses import dataclass, field
from typing import Optional

import requests

try:
    from .. import __version__
    from ..config import DEFAULT_API_URL, get_machine_uuid
    from .http_client import BaseApiClient, BetterFlowClientError, BetterFlowAuthError
    from .retry import RetryConfig
except ImportError:
    from src import __version__
    from config import DEFAULT_API_URL, get_machine_uuid
    from sync.http_client import BaseApiClient, BetterFlowClientError, BetterFlowAuthError
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

    # =========================================================================
    # Authentication
    # =========================================================================

    def exchange_code(
        self,
        code: str,
        device_name: str,
        code_verifier: Optional[str] = None,
        device_info: Optional[DeviceInfo] = None,
    ) -> AuthResult:
        """Exchange an authorization code for a Sanctum token.

        Args:
            code: 64-char authorization code from browser flow
            device_name: Name for this device token
            code_verifier: PKCE code verifier (required for PKCE flow)
            device_info: Pre-collected device info (avoids redundant collect)

        Returns:
            AuthResult with api_token on success
        """
        url = f"{self.web_base_url}/api/v1/sync/auth/token"
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
        if code_verifier:
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
            if response.status_code in (400, 401, 403, 422):
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
                device_id=device_name,
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
        """Revoke this device's token."""
        try:
            self._request("POST", "revoke")
            return True
        except BetterFlowClientError:
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

        try:
            # Idempotency key prevents duplicate processing if the connection
            # drops after the server processes the batch but before we receive
            # the response (N1).
            idempotency_key = str(uuid.uuid4())
            response = self._request(
                "POST", "events/batch",
                data={"events": events},
                compress=True,
                extra_headers={"X-Idempotency-Key": idempotency_key},
            )
            return SyncResult(
                success=True,
                events_synced=response.get("processed", len(events)),
                events_queued=response.get("failed", 0),
                accepted_ids=response.get("accepted_ids", []),
            )
        except BetterFlowAuthError:
            raise  # Callers must handle token refresh / re-login
        except BetterFlowClientError as e:
            return SyncResult(success=False, error=str(e))

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

    def heartbeat(self, agent_version: str = AGENT_VERSION) -> dict:
        """Send heartbeat to server.

        Uses a shorter timeout (5s) and fewer retries (1) to avoid
        blocking the sync loop when the server is slow (N14).

        Returns server commands (pause/deregister) and config update flag.
        """
        return self._request(
            "POST", "heartbeat", data={
                "agent_version": agent_version,
                "timezone": self._detect_timezone(),
            },
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

    # =========================================================================
    # Configuration
    # =========================================================================

    def get_config(self) -> dict:
        """Get configuration from server."""
        return self._request("GET", "config")

    def get_projects(self) -> list[dict]:
        """Get list of projects for app mapping."""
        return self._request("GET", "projects")

    def get_categories(self) -> dict:
        """Get app-to-category mappings from server.

        Returns:
            Dict with "categories" key mapping app names to categories.
        """
        return self._request("GET", "categories")

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
