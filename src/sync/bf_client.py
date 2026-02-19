"""BetterFlow API client - syncs events to BetterFlow server."""

import gzip
import json
import logging
import platform
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import requests

from ..config import DEFAULT_API_URL
from .retry import RetryConfig, retry_with_backoff, RetryExhausted

__all__ = [
    "BetterFlowClient",
    "BetterFlowClientError",
    "BetterFlowAuthError",
    "DeviceInfo",
    "AuthResult",
    "SyncResult",
]

logger = logging.getLogger(__name__)


@dataclass
class DeviceInfo:
    """Information about this device."""

    hostname: str
    os_name: str
    os_version: str
    agent_version: str

    @classmethod
    def collect(cls, agent_version: str = "1.0.0") -> "DeviceInfo":
        """Collect device information."""
        return cls(
            hostname=platform.node(),
            os_name=platform.system(),
            os_version=platform.release(),
            agent_version=agent_version,
        )

    def to_dict(self) -> dict:
        return {
            "hostname": self.hostname,
            "os_name": self.os_name,
            "os_version": self.os_version,
            "agent_version": self.agent_version,
        }


@dataclass
class AuthResult:
    """Result of authentication."""

    success: bool
    device_id: Optional[str] = None
    api_token: Optional[str] = None
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    error: Optional[str] = None


@dataclass
class SyncResult:
    """Result of event sync."""

    success: bool
    events_synced: int = 0
    events_queued: int = 0
    error: Optional[str] = None


class BetterFlowClientError(Exception):
    """BetterFlow client error."""

    pass


class BetterFlowAuthError(BetterFlowClientError):
    """Authentication error."""

    pass


class _TransientError(Exception):
    """Internal: Marks an error as transient/retryable."""

    pass


class BetterFlowClient:
    """Client for syncing events to BetterFlow server."""

    # Default retry configuration for transient network failures
    DEFAULT_RETRY_CONFIG = RetryConfig(
        max_retries=3,
        base_delay=1.0,
        max_delay=30.0,
        exponential_base=2.0,
        jitter=True,
    )

    def __init__(
        self,
        api_url: str = DEFAULT_API_URL,
        token: Optional[str] = None,
        device_id: Optional[str] = None,
        compress: bool = True,
        timeout: int = 30,
        retry_config: Optional[RetryConfig] = None,
    ):
        """Initialize BetterFlow client.

        Args:
            api_url: BetterFlow API base URL
            token: API token for authentication
            device_id: Device ID from registration
            compress: Use gzip compression for event batches
            timeout: Request timeout in seconds
            retry_config: Configuration for retry with exponential backoff
        """
        self.api_url = api_url.rstrip("/")
        self._web_base_url: Optional[str] = None
        self.token = token
        self.device_id = device_id
        self.compress = compress
        self.timeout = timeout
        self.retry_config = retry_config or self.DEFAULT_RETRY_CONFIG
        self._session = requests.Session()

    def _get_headers(self) -> dict:
        """Get request headers."""
        headers = {
            "Accept": "application/json",
            "User-Agent": "BetterFlow-Sync/1.0.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.device_id:
            headers["X-Device-ID"] = self.device_id
        return headers

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[dict] = None,
        compress: bool = False,
        retry: bool = True,
    ) -> dict:
        """Make request to BetterFlow API.

        Args:
            method: HTTP method
            endpoint: API endpoint
            data: Request data
            compress: Whether to gzip compress the payload
            retry: Whether to retry on transient failures (default True)

        Returns:
            Response data as dict

        Raises:
            BetterFlowAuthError: For 401/403 responses (not retried)
            BetterFlowClientError: For other errors
        """
        url = f"{self.api_url}/{endpoint.lstrip('/')}"
        headers = self._get_headers()

        kwargs = {"timeout": self.timeout, "headers": headers}

        if data:
            if compress and self.compress:
                # Gzip compress the JSON payload
                json_data = json.dumps(data).encode("utf-8")
                compressed = gzip.compress(json_data)
                headers["Content-Type"] = "application/json"
                headers["Content-Encoding"] = "gzip"
                kwargs["data"] = compressed
            else:
                kwargs["json"] = data

        def do_request() -> dict:
            try:
                response = self._session.request(method, url, **kwargs)

                if response.status_code == 401:
                    raise BetterFlowAuthError("Invalid or expired API token")
                if response.status_code == 403:
                    raise BetterFlowAuthError("Device not authorized")

                # Server errors (5xx) are retryable
                if response.status_code >= 500:
                    raise _TransientError(f"Server error: {response.status_code}")

                response.raise_for_status()
                return response.json() if response.content else {}

            except requests.exceptions.ConnectionError:
                raise _TransientError("Cannot connect to BetterFlow API")
            except requests.exceptions.Timeout:
                raise _TransientError("Request timed out")
            except requests.exceptions.HTTPError as e:
                error_detail = ""
                try:
                    error_detail = e.response.json().get("message", "")
                except Exception:
                    pass
                raise BetterFlowClientError(
                    f"API error ({e.response.status_code}): {error_detail or str(e)}"
                ) from e

        if retry:
            try:
                return retry_with_backoff(
                    do_request,
                    config=self.retry_config,
                    retryable_exceptions=(_TransientError,),
                )
            except RetryExhausted as e:
                # Convert to client error after exhausting retries
                if e.last_error:
                    raise BetterFlowClientError(str(e.last_error)) from e.last_error
                raise BetterFlowClientError("Request failed after retries") from e
        else:
            try:
                return do_request()
            except _TransientError as e:
                raise BetterFlowClientError(str(e)) from e

    @property
    def web_base_url(self) -> str:
        """Derive the web base URL from the API URL.

        e.g. "https://betterflow.eu/api/agent" -> "https://betterflow.eu"
        """
        if self._web_base_url:
            return self._web_base_url
        parsed = urlparse(self.api_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    def is_reachable(self) -> bool:
        """Check if BetterFlow API is reachable."""
        try:
            self._request("GET", "health", retry=False)
            return True
        except BetterFlowClientError:
            # Try the status endpoint as fallback
            try:
                self._request("GET", "events/status", retry=False)
                return True
            except BetterFlowClientError:
                return False

    def exchange_code(
        self, code: str, device_name: str, code_verifier: Optional[str] = None
    ) -> AuthResult:
        """Exchange an authorization code for a Sanctum token.

        Args:
            code: 64-char authorization code from browser flow
            device_name: Name for this device token
            code_verifier: PKCE code verifier (if PKCE was used in auth flow)

        Returns:
            AuthResult with api_token on success
        """
        url = f"{self.web_base_url}/api/v1/sync/auth/token"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "BetterFlow-Sync/1.0.0",
        }
        payload = {"code": code, "device_name": device_name}
        if code_verifier:
            payload["code_verifier"] = code_verifier
        try:
            response = self._session.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            if response.status_code == 401:
                data = response.json()
                return AuthResult(success=False, error=data.get("message", "Invalid code"))
            response.raise_for_status()
            data = response.json()
            user = data.get("user", {})
            return AuthResult(
                success=True,
                api_token=data["access_token"],
                device_id=device_name,
                user_email=user.get("email"),
                user_name=user.get("name"),
            )
        except requests.exceptions.ConnectionError:
            return AuthResult(success=False, error="Cannot connect to BetterFlow")
        except requests.exceptions.Timeout:
            return AuthResult(success=False, error="Request timed out")
        except Exception as e:
            return AuthResult(success=False, error=str(e))

    def register(
        self, email: str, password: str, device_info: Optional[DeviceInfo] = None
    ) -> AuthResult:
        """Register this device with BetterFlow.

        DEPRECATED: Use exchange_code() with browser OAuth flow instead.
        This method sends passwords over the network which is less secure
        than the browser-based OAuth flow with PKCE.

        Args:
            email: User's BetterFlow email
            password: User's BetterFlow password
            device_info: Optional device information

        Returns:
            AuthResult with device_id and api_token on success
        """
        import warnings
        warnings.warn(
            "register() is deprecated. Use browser OAuth flow with exchange_code() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        if device_info is None:
            device_info = DeviceInfo.collect()

        try:
            response = self._request(
                "POST",
                "register",
                data={
                    "email": email,
                    "password": password,
                    "device_info": device_info.to_dict(),
                },
            )
            return AuthResult(
                success=True,
                device_id=response.get("device_id"),
                api_token=response.get("api_token"),
            )
        except BetterFlowAuthError as e:
            return AuthResult(success=False, error=str(e))
        except BetterFlowClientError as e:
            return AuthResult(success=False, error=str(e))

    def revoke(self) -> bool:
        """Revoke this device's token."""
        try:
            self._request("POST", "revoke")
            return True
        except BetterFlowClientError:
            return False

    def send_events(self, events: list[dict]) -> SyncResult:
        """Send a batch of events to BetterFlow.

        Args:
            events: List of event dictionaries with timestamp, duration, data

        Returns:
            SyncResult with success status and count
        """
        if not events:
            return SyncResult(success=True, events_synced=0)

        try:
            response = self._request(
                "POST",
                "events/batch",
                data={"events": events},
                compress=True,
            )
            return SyncResult(
                success=True,
                events_synced=response.get("synced", len(events)),
                events_queued=response.get("queued", 0),
            )
        except BetterFlowAuthError as e:
            return SyncResult(success=False, error=str(e))
        except BetterFlowClientError as e:
            return SyncResult(success=False, error=str(e))

    def start_session(self) -> dict:
        """Start a tracking session."""
        return self._request("POST", "sessions/start")

    def end_session(self, reason: str = "user_stopped") -> dict:
        """End the current tracking session.

        Args:
            reason: Reason for ending (user_stopped, idle, shutdown, error)
        """
        return self._request("POST", "sessions/end", data={"reason": reason})

    def get_status(self) -> dict:
        """Get sync status."""
        return self._request("GET", "events/status")

    def get_config(self) -> dict:
        """Get configuration from server."""
        return self._request("GET", "config")

    def get_projects(self) -> list[dict]:
        """Get list of projects for app mapping."""
        return self._request("GET", "projects")

    def update_project_mapping(self, mappings: dict[str, int]) -> dict:
        """Update app to project mappings.

        Args:
            mappings: Dict of app_name -> project_id
        """
        return self._request("POST", "config/project-mapping", data={"mappings": mappings})

    def set_credentials(self, token: str, device_id: str) -> None:
        """Set authentication credentials."""
        self.token = token
        self.device_id = device_id

    def clear_credentials(self) -> None:
        """Clear authentication credentials."""
        self.token = None
        self.device_id = None

    def close(self) -> None:
        """Close the session."""
        self._session.close()

    def __enter__(self) -> "BetterFlowClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
