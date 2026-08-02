"""Base HTTP client with retry logic for BetterFlow API."""

import gzip
import json
import logging
import os
import socket
import sys
import threading
import time
from typing import Optional
from urllib.parse import urlparse

import certifi
import requests

try:
    from .. import __version__
except ImportError:
    from src import __version__
from .retry import RetryConfig, retry_with_backoff, RetryExhausted

__all__ = [
    "BaseApiClient",
    "BetterFlowClientError",
    "BetterFlowAuthError",
    "resolve_ca_bundle",
    "transient_failure_count",
    "transient_failure_counter",
]

logger = logging.getLogger(__name__)


# ---- Transient-failure counter ----------------------------------------------
#
# Monotonic count of network-shaped failures, i.e. those the is_transient rule —
# the codebase's SINGLE "network problem vs definitive rejection" decision point —
# classified as transient. SyncCoordinator._do_sync snapshots it at cycle start
# so its watchdog can tell "the API was unreachable and the cycle ran long" (a
# warning) from "the cycle is genuinely hung" (an error) instead of reporting
# both as "Sync hung" (2026-07-22, device 18: a 150.86s cycle during an
# hour-long connectivity outage, nothing hung, nothing lost, paged as an ERROR).
#
# Incremented once per failure OBJECT, in __init__ — never in the is_transient
# getter, which stays a pure query. A read-counting metric inflates the moment
# anyone logs or re-reads the property, and inflation is the fail-open direction:
# it makes a real hang report as an outage warning.
#
# PER-THREAD, not process-wide. _acquire_sync_slot abandons a cycle that has held
# the sync lock past _SYNC_WEDGE_CEILING and re-arms a fresh lock — and documents
# that the stuck thread cannot be killed. So two _do_sync bodies can be live at
# once, and a single global would let the zombie cycle's late failures classify
# the healthy cycle's watchdog ("offline" over a genuine wedge). Each cycle's
# work runs on its own thread, so attribution by thread is attribution by cycle.
# The watchdog fires on a Timer thread, so _do_sync captures its thread's counter
# OBJECT and the closure reads that — never transient_failure_count(), which
# would answer for the Timer thread. Same reason a background HTTP failure (update
# checker, error-report daemon) no longer classifies a sync cycle.
#
# Deliberately NOT a second classifier — see rules/one-rule-one-implementation.md.
# Lock-free by construction: a thread's counter is written only by that thread.
_thread_state = threading.local()


class _TransientFailureCounter:
    """One thread's transient-failure tally. Mutable so a watchdog closure can
    hold the object and observe increments made after it was captured."""

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0


def transient_failure_counter() -> _TransientFailureCounter:
    """The CALLING THREAD's transient-failure counter, created on first use.

    Capture this object (not its value) when the reader runs on another thread —
    SyncCoordinator's watchdog Timer does exactly that.
    """
    counter = getattr(_thread_state, "counter", None)
    if counter is None:
        counter = _TransientFailureCounter()
        _thread_state.counter = counter
    return counter


def transient_failure_count() -> int:
    """The calling thread's transient-failure count (monotonic within a thread).

    Only meaningful as a delta between two reads ON THE SAME THREAD — callers
    snapshot it and check whether it moved.
    """
    return transient_failure_counter().value


# ---- TLS CA bundle resolution -----------------------------------------------
#
# requests verifies HTTPS against certifi.where(), which in a PyInstaller build
# resolves to _internal/certifi/cacert.pem. If that file is missing — not
# collected into the bundle, or clipped by antivirus after install — every
# HTTPS request raises OSError("Could not find a suitable TLS CA certificate
# bundle, invalid path: ...cacert.pem") and ALL sync silently fails until the
# app is restarted (reported on Windows 2026-06-18: ~1h40m sync blackout while
# the user kept working, surfacing as false idle on the dashboard). build.spec
# now ships a redundant copy under resources/; this resolver picks whichever CA
# bundle actually exists and logs loudly if none do, instead of letting requests
# fail the same way on every call.
_CACHED_CA_BUNDLE: Optional[str] = None
_CA_BUNDLE_RESOLVED = False


def _bundled_cacert_fallback() -> Optional[str]:
    """Path to the redundant cacert.pem shipped under resources/, if present."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return os.path.join(meipass, "resources", "cacert.pem")
    # Dev run: src/sync/http_client.py -> src/ -> repo root -> resources/
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "resources",
        "cacert.pem",
    )


def resolve_ca_bundle() -> Optional[str]:
    """Return a usable CA bundle path for TLS verification, or None.

    Resolution order (first existing file wins):
      1. REQUESTS_CA_BUNDLE / SSL_CERT_FILE env overrides (operator escape hatch)
      2. certifi.where() — the bundle requests would use by default
      3. resources/cacert.pem — the redundant copy build.spec ships

    Cached after the first call. Returns None only when no bundle exists at all,
    after logging an error — we never silently disable verification (that would
    trade a sync outage for a MITM risk); callers fall back to the requests
    default, which now fails loudly with a logged cause instead of silently.

    Called from several threads (sync scheduler, update-checker, error-report
    daemon). Intentionally lock-free: resolution is idempotent — every caller
    inspects the same filesystem/env and arrives at the identical result, and
    the string/bool assignments are atomic under the GIL, so a first-use race
    only recomputes the same value, never tears or returns a wrong one.
    """
    global _CACHED_CA_BUNDLE, _CA_BUNDLE_RESOLVED
    if _CA_BUNDLE_RESOLVED:
        return _CACHED_CA_BUNDLE

    candidates = []
    for env in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        val = os.environ.get(env)
        if val:
            candidates.append(val)
    try:
        candidates.append(certifi.where())
    except Exception as e:  # pragma: no cover - certifi.where() is pure pathjoin
        logger.warning("certifi.where() raised while resolving CA bundle: %s", e)
    fallback = _bundled_cacert_fallback()
    if fallback:
        candidates.append(fallback)

    for path in candidates:
        try:
            if path and os.path.isfile(path):
                _CACHED_CA_BUNDLE = path
                _CA_BUNDLE_RESOLVED = True
                return path
        except OSError:
            continue

    logger.error(
        "No TLS CA bundle found (checked: %s). HTTPS verification will fail; "
        "reinstall the app to restore the certifi bundle.",
        ", ".join(p for p in candidates if p) or "none",
    )
    _CACHED_CA_BUNDLE = None
    _CA_BUNDLE_RESOLVED = True
    return None


def _new_verified_session() -> requests.Session:
    """Create a requests.Session with TLS verification pinned to a resolved CA
    bundle, so a missing/clipped certifi copy can't silently break all sync."""
    session = requests.Session()
    ca_bundle = resolve_ca_bundle()
    if ca_bundle:
        session.verify = ca_bundle
    return session


class BetterFlowClientError(Exception):
    """BetterFlow client error.

    ``status_code`` is set only when the failure is a definitive HTTP rejection
    (a 4xx). Transient failures — 5xx, timeouts, connection/DNS errors, retries
    exhausted — leave it None. The queue uses this to avoid counting a transient
    server outage against an event's drop threshold (2026-06-30 data loss).
    """

    #: Whether an instance of this class counts toward the watchdog's
    #: network-shaped failure tally. False for auth errors — see __init__.
    _COUNTS_AS_NETWORK_FAILURE = True

    def __init__(self, message: str = "", status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code
        # Count the FAILURE, not the classification READ. Incrementing inside the
        # is_transient getter would make the metric a function of how many times
        # the property is consulted — one debug log or a second caller and it
        # inflates, which is the fail-open direction: an inflated count makes the
        # watchdog report a genuine hang as an outage warning. Counting once per
        # failure object is safe because every construction site in this module
        # raises immediately (test code does build these speculatively as mock
        # side-effects, which only shifts a per-thread tally the tests read as a
        # delta). The transient/definitive RULE still lives in exactly one place:
        # the pure query below.
        #
        # The counter and is_transient DELIBERATELY DISAGREE on auth errors, and
        # that asymmetry is not an oversight. A 401/403 is definitive for the
        # WATCHDOG's question ("was the network down?" — no, the token expired;
        # reporting "API unreachable" would hide the actionable condition), but it
        # must stay transient for the QUEUE's question ("burn a retry?" — no).
        # is_transient is load-bearing for offline-queue durability: transient
        # failures HOLD events without counting toward the drop threshold, so
        # flipping auth errors to definitive would burn retries and, past the
        # threshold, drop real activity — the 2026-06-30 data-loss class. The
        # watchdog's accuracy is not worth billing data, so the exclusion lives
        # here, in the counter, and never in the classifier.
        if self._COUNTS_AS_NETWORK_FAILURE and self.is_transient:
            transient_failure_counter().value += 1

    @property
    def is_transient(self) -> bool:
        """True when this failure is transient (hold the events, don't count them
        toward the drop): server unreachable, timeout/DNS, 5xx, or a retryable 4xx
        (429/408/503/504 are raised WITHOUT a status_code). ``status_code`` is set
        ONLY for a definitive client rejection (a non-retryable 4xx), so "no code"
        means transient. NOTE: this is a rejection classifier, not a raw HTTP
        status — do not set status_code on a retryable 4xx or it becomes
        definitive and its events burn retries during e.g. a rate-limit window.

        Pure query — free to read as often as you like. This is the codebase's
        ONLY transient/definitive rule; __init__ consults it to maintain the
        counter SyncCoordinator's watchdog reads (_TRANSIENT_FAILURE_COUNT)."""
        return self.status_code is None or self.status_code >= 500


class BetterFlowAuthError(BetterFlowClientError):
    """Authentication error (401/403).

    Excluded from the watchdog's network-failure tally: an expired token or an
    unauthorized device is a definitive rejection whose fix is re-authentication,
    not waiting out a network. ``is_transient`` stays True — the offline queue
    depends on it to hold events without burning retries — so only the COUNTER
    disagrees. See BetterFlowClientError.__init__ for why that asymmetry is
    deliberate.
    """

    _COUNTS_AS_NETWORK_FAILURE = False


class _TransientError(Exception):
    """Internal: Marks an error as transient/retryable."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class BaseApiClient:
    """Base HTTP client with retry logic.

    Handles:
    - Session management
    - Authentication headers
    - Gzip compression
    - Retry with exponential backoff
    - Error handling and classification

    Single Responsibility: HTTP communication only.
    """

    # In-cycle network budget. A single retrying request runs at most
    # (max_retries + 1) attempts * timeout + backoff. With max_retries=3 and a
    # 30s timeout that is ~129s — past the sync watchdog (SyncCoordinator
    # ._DO_SYNC_DEADLINE, 120s when this was written, 150s now), so a hung/slow
    # server (e.g. the 2026-06-30 Redis/502 outage) false-trips "Sync hung".
    # max_retries=2 bounds the worst-case chain
    # to ~94s, comfortably inside the watchdog; the OfflineQueue is the durable
    # cross-cycle retry, so a failed batch is re-sent next cycle regardless — the
    # 4th in-cycle attempt only amplified the stall. Guarded by
    # tests/test_sync_watchdog_budget.py.
    DEFAULT_RETRY_CONFIG = RetryConfig(
        max_retries=2,
        base_delay=1.0,
        max_delay=30.0,
        exponential_base=2.0,
        jitter=True,
    )

    USER_AGENT = f"BetterFlow/{__version__}"

    def __init__(
        self,
        api_url: str,
        web_base_url: Optional[str] = None,
        token: Optional[str] = None,
        device_id: Optional[str] = None,
        compress: bool = True,
        timeout: int = 30,
        retry_config: Optional[RetryConfig] = None,
        session: Optional[requests.Session] = None,
    ):
        """Initialize base API client.

        Args:
            api_url: BetterFlow API base URL
            web_base_url: Optional explicit web app base URL (for browser auth)
            token: API token for authentication
            device_id: Device ID from registration
            compress: Use gzip compression for payloads
            timeout: Request timeout in seconds
            retry_config: Configuration for retry with exponential backoff
            session: Optional requests session (for dependency injection/testing)
        """
        self.api_url = api_url.rstrip("/")
        parsed_api = urlparse(self.api_url)
        api_host = parsed_api.hostname or ""
        env_web_base = os.getenv("BETTERFLOW_WEB_BASE_URL")

        explicit_web_base = web_base_url
        if explicit_web_base is None and env_web_base and api_host in {"localhost", "127.0.0.1"}:
            explicit_web_base = env_web_base

        self._web_base_url: Optional[str] = (
            explicit_web_base.rstrip("/") if explicit_web_base else None
        )
        self.token = token
        self.device_id = device_id
        self.compress = compress
        self.timeout = timeout
        self.retry_config = retry_config or self.DEFAULT_RETRY_CONFIG
        self._session = session or _new_verified_session()
        self._owns_session = session is None  # Track if we created the session
        self._session_lock = threading.Lock()
        # Global rate-limit backoff. When the server returns 429, ALL outbound
        # calls (sync, hours, trends, heartbeat) pause until this monotonic
        # deadline — one coordinated pause instead of each endpoint independently
        # retrying into the same window (the retry storm that amplified the 429s).
        self._throttle_until = 0.0
        self._throttle_lock = threading.Lock()

    # ---- Global rate-limit backoff -------------------------------------------
    # Default backoff when a 429 carries no usable Retry-After header.
    _DEFAULT_THROTTLE_SECONDS = 30.0
    # Cap so a hostile/buggy Retry-After can't park the agent for minutes.
    _MAX_THROTTLE_SECONDS = 60.0

    def _throttle_remaining(self) -> float:
        """Seconds left in the active global backoff window (0 if not throttled)."""
        with self._throttle_lock:
            until = self._throttle_until
        remaining = until - time.monotonic()
        if remaining <= 0:
            return 0.0
        # Clamp to the cap: _enter_throttle never sets a deadline more than
        # _MAX_THROTTLE_SECONDS out, but float rounding of (t + CAP) - t can land
        # a hair above CAP when the monotonic clock is coarse (Windows returns the
        # same value for both reads), which would otherwise violate the documented
        # "never longer than _MAX_THROTTLE_SECONDS" invariant.
        return min(remaining, self._MAX_THROTTLE_SECONDS)

    def _enter_throttle(self, retry_after: float) -> None:
        """Open/extend the global backoff window (thread-safe).

        Only ever pushes the deadline later, never earlier, so concurrent 429s
        from different endpoints can't shorten an existing backoff.
        """
        retry_after = max(0.0, min(retry_after, self._MAX_THROTTLE_SECONDS))
        until = time.monotonic() + retry_after
        with self._throttle_lock:
            if until > self._throttle_until:
                self._throttle_until = until

    @property
    def web_base_url(self) -> str:
        """Derive the web base URL from the API URL.

        e.g. "https://betterflow.eu/api/agent" -> "https://betterflow.eu"
        """
        if self._web_base_url:
            return self._web_base_url
        parsed = urlparse(self.api_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def _get_headers(self) -> dict:
        """Get request headers with authentication (thread-safe)."""
        with self._session_lock:
            token = self.token
            device_id = self.device_id
        headers = {
            "Accept": "application/json",
            "User-Agent": self.USER_AGENT,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if device_id:
            headers["X-Device-ID"] = str(device_id)
        return headers

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[dict] = None,
        files: Optional[dict] = None,
        compress: bool = False,
        retry: bool = True,
        extra_headers: Optional[dict] = None,
        timeout_override: Optional[int] = None,
        retry_config_override: Optional[RetryConfig] = None,
    ) -> dict:
        """Make request to BetterFlow API.

        Args:
            method: HTTP method
            endpoint: API endpoint (relative to api_url)
            data: Request data (sent as JSON, or as form fields when files is set)
            files: Files for multipart upload, e.g. {"file": ("name", bytes, "mime")}
            compress: Whether to gzip compress the payload
            retry: Whether to retry on transient failures
            extra_headers: Additional headers to include in the request
            timeout_override: Override default timeout for this request
            retry_config_override: Override default retry config for this request

        Returns:
            Response data as dict

        Raises:
            BetterFlowAuthError: For 401/403 responses (not retried)
            BetterFlowClientError: For other errors
        """
        url = f"{self.api_url}/{endpoint.lstrip('/')}"
        headers = self._get_headers()
        if extra_headers:
            headers.update(extra_headers)
        effective_timeout = timeout_override if timeout_override is not None else self.timeout
        kwargs: dict = {"timeout": effective_timeout, "headers": headers}

        if files:
            # Multipart upload — data goes as form fields, not JSON
            kwargs["files"] = files
            if data:
                kwargs["data"] = data
            # Remove Accept: application/json so requests sets multipart headers
            headers.pop("Content-Type", None)
        elif data:
            if compress and self.compress:
                json_data = json.dumps(data).encode("utf-8")
                compressed = gzip.compress(json_data)
                headers["Content-Type"] = "application/json"
                headers["Content-Encoding"] = "gzip"
                kwargs["data"] = compressed
            else:
                kwargs["json"] = data

        def do_request() -> dict:
            try:
                # Global backoff: if a recent 429 opened a backoff window, skip the
                # network call entirely and fail fast so the caller queues/uses
                # cache. This is what stops every endpoint from hammering a
                # server that already told us to wait.
                backoff = self._throttle_remaining()
                if backoff > 0:
                    raise BetterFlowClientError(
                        f"Rate-limit backoff active, {backoff:.0f}s remaining"
                    )

                with self._session_lock:
                    session = self._session
                if session is None:
                    raise BetterFlowClientError("Client has been closed")
                response = session.request(method, url, **kwargs)

                if response.status_code == 401:
                    raise BetterFlowAuthError("Invalid or expired API token")
                if response.status_code == 403:
                    raise BetterFlowAuthError("Device not authorized")

                # Rate limited: open a GLOBAL backoff window (pauses every
                # endpoint, not just this call) and fail fast — non-retryable, so
                # we don't retry into the very window the server asked us to wait
                # out. Honors Retry-After, capped, with a sane default.
                if response.status_code == 429:
                    retry_after_secs = self._DEFAULT_THROTTLE_SECONDS
                    retry_after_hdr = response.headers.get("Retry-After")
                    if retry_after_hdr:
                        try:
                            retry_after_secs = min(
                                float(retry_after_hdr), self._MAX_THROTTLE_SECONDS
                            )
                        except (ValueError, TypeError):
                            pass
                    self._enter_throttle(retry_after_secs)
                    raise BetterFlowClientError(
                        f"Server returned 429 (Retry-After: {retry_after_hdr or 'none'}); "
                        f"backing off {retry_after_secs:.0f}s"
                    )

                # Other transient HTTP status codes stay per-call retryable (N12).
                if response.status_code in (408, 503, 504):
                    retry_after_hdr = response.headers.get("Retry-After")
                    retry_after_secs = None
                    msg = f"Server returned {response.status_code}"
                    if retry_after_hdr:
                        msg += f" (Retry-After: {retry_after_hdr}s)"
                        try:
                            retry_after_secs = min(float(retry_after_hdr), 60)
                        except (ValueError, TypeError):
                            pass
                    raise _TransientError(msg, retry_after=retry_after_secs)

                # Other server errors (5xx) are retryable
                if response.status_code >= 500:
                    raise _TransientError(f"Server error: {response.status_code}")

                response.raise_for_status()
                if not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError as e:
                    # Malformed body on a 2xx (requests raises JSONDecodeError,
                    # a ValueError subclass) — surface as our error type, not a
                    # raw decode error leaking to callers.
                    raise BetterFlowClientError("Invalid JSON in API response") from e

            except requests.exceptions.ConnectionError as e:
                # DNS resolution failures are not retryable (N8)
                cause = e.__cause__
                while cause is not None:
                    if isinstance(cause, socket.gaierror):
                        raise BetterFlowClientError(
                            f"DNS resolution failed: {cause}"
                        ) from e
                    cause = getattr(cause, "__cause__", None) or getattr(cause, "__context__", None)
                raise _TransientError("Cannot connect to BetterFlow API")
            except requests.exceptions.ChunkedEncodingError:
                raise _TransientError("Connection dropped mid-response")
            except requests.exceptions.ContentDecodingError:
                raise _TransientError("Response body decoding failed")
            except requests.exceptions.Timeout:
                raise _TransientError("Request timed out")
            except requests.exceptions.HTTPError as e:
                error_detail = ""
                try:
                    error_detail = e.response.json().get("message", "")
                except (ValueError, AttributeError) as parse_err:
                    logger.debug("HTTPError body parse failed: %s", parse_err)
                raise BetterFlowClientError(
                    f"API error ({e.response.status_code}): {error_detail or str(e)}",
                    status_code=e.response.status_code,
                ) from e

        effective_retry_config = retry_config_override or self.retry_config

        if retry:
            try:
                return retry_with_backoff(
                    do_request,
                    config=effective_retry_config,
                    retryable_exceptions=(_TransientError,),
                )
            except RetryExhausted as e:
                if e.last_error:
                    raise BetterFlowClientError(str(e.last_error)) from e.last_error
                raise BetterFlowClientError("Request failed after retries") from e
        else:
            try:
                return do_request()
            except _TransientError as e:
                raise BetterFlowClientError(str(e)) from e

    def reset_session(self) -> None:
        """Drop pooled connections and create a fresh session.

        Call after laptop wake to avoid stale TCP connections that would
        block the first request with a 30s timeout.

        Thread-safe: the lock ensures do_request() snapshots either the
        old or the new session, never a partially-swapped reference.
        """
        with self._session_lock:
            old = self._session
            was_owned = self._owns_session
            self._session = _new_verified_session()
            self._owns_session = True
        # Close outside lock to avoid holding it during I/O.
        # Only close the old session if we owned it - don't destroy
        # an injected session the caller still holds a reference to.
        if old is not None and was_owned:
            try:
                old.close()
            except Exception as e:
                logger.debug("Closing old HTTP session raised: %s", e)

    def set_credentials(self, token: str, device_id: str) -> None:
        """Set authentication credentials (thread-safe)."""
        with self._session_lock:
            self.token = token
            self.device_id = device_id

    def clear_credentials(self) -> None:
        """Clear authentication credentials (thread-safe)."""
        with self._session_lock:
            self.token = None
            self.device_id = None

    def is_reachable(self) -> bool:
        """Check if BetterFlow API is reachable."""
        try:
            self._request("GET", "health", retry=False)
            return True
        except BetterFlowAuthError:
            return True  # Server is reachable; auth is a separate concern
        except BetterFlowClientError:
            try:
                self._request("GET", "events/status", retry=False)
                return True
            except BetterFlowAuthError:
                return True
            except BetterFlowClientError:
                return False

    def close(self) -> None:
        """Close the session if we own it."""
        with self._session_lock:
            if not self._owns_session:
                return
            session = self._session
            self._session = None
        if session is not None:
            session.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            # __del__ must not raise. We intentionally swallow here since
            # the interpreter may already be tearing down when this runs.
            pass

    def __enter__(self) -> "BaseApiClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
