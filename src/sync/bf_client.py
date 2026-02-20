"""BetterFlow API client - syncs events to BetterFlow server."""

import hashlib
import logging
import platform
import warnings
from dataclasses import dataclass
from typing import Optional

import requests

from ..config import DEFAULT_API_URL
from .http_client import BaseApiClient, BetterFlowClientError, BetterFlowAuthError
from .retry import RetryConfig

__all__ = [
    "BetterFlowClient",
    "BetterFlowClientError",
    "BetterFlowAuthError",
    "DeviceInfo",
    "AuthResult",
    "SyncResult",
]

logger = logging.getLogger(__name__)

AGENT_VERSION = "1.0.0"


@dataclass
class DeviceInfo:
    """Information about this device."""

    hostname: str
    os_name: str
    os_version: str
    agent_version: str

    @classmethod
    def collect(cls, agent_version: str = AGENT_VERSION) -> "DeviceInfo":
        """Collect device information."""
        return cls(
            hostname=platform.node(),
            os_name=platform.system(),
            os_version=platform.release(),
            agent_version=agent_version,
        )

    @property
    def device_name(self) -> str:
        return f"{self.hostname} ({self.os_name})"

    @property
    def machine_id(self) -> str:
        """Generate a stable machine ID from hostname + OS."""
        raw = f"{self.hostname}-{self.os_name}-{self.os_version}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

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
    error: Optional[str] = None


@dataclass
class SyncResult:
    """Result of event sync."""

    success: bool
    events_synced: int = 0
    events_queued: int = 0
    error: Optional[str] = None


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
        super().__init__(
            api_url=api_url,
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
        self, code: str, device_name: str, code_verifier: Optional[str] = None
    ) -> AuthResult:
        """Exchange an authorization code for a Sanctum token.

        Args:
            code: 64-char authorization code from browser flow
            device_name: Name for this device token
            code_verifier: PKCE code verifier (required for PKCE flow)

        Returns:
            AuthResult with api_token on success
        """
        url = f"{self.web_base_url}/api/v1/sync/auth/token"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.USER_AGENT,
        }
        device_info = DeviceInfo.collect()
        payload = {
            "code": code,
            "device_name": device_name,
            "platform": device_info.platform_key,
            "os_version": platform.release(),
            "machine_id": device_info.machine_id,
            "agent_version": AGENT_VERSION,
        }
        if code_verifier:
            payload["code_verifier"] = code_verifier

        try:
            response = self._session.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            if response.status_code in (400, 401, 403, 422):
                try:
                    data = response.json()
                    msg = data.get("message", data.get("error", "Authentication failed"))
                except (ValueError, Exception):
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
            )
        except requests.exceptions.ConnectionError:
            return AuthResult(success=False, error="Cannot connect to BetterFlow")
        except requests.exceptions.Timeout:
            return AuthResult(success=False, error="Request timed out")
        except requests.exceptions.HTTPError as e:
            return AuthResult(success=False, error=f"HTTP error: {e.response.status_code}")
        except (KeyError, ValueError) as e:
            return AuthResult(success=False, error=f"Invalid response: {e}")

    def register(
        self, email: str, password: str, device_info: Optional[DeviceInfo] = None
    ) -> AuthResult:
        """Register this device with BetterFlow.

        DEPRECATED: Use exchange_code() with browser OAuth flow instead.
        """
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
                    "device_name": device_info.device_name,
                    "machine_id": device_info.machine_id,
                    "platform": device_info.platform_key,
                    "os_version": device_info.os_version,
                    "agent_version": device_info.agent_version,
                },
            )
            return AuthResult(
                success=True,
                device_id=str(response.get("device_id", "")),
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
            response = self._request(
                "POST",
                "events/batch",
                data={"events": events},
                compress=True,
            )
            return SyncResult(
                success=True,
                events_synced=response.get("processed", len(events)),
                events_queued=response.get("failed", 0),
            )
        except BetterFlowAuthError as e:
            return SyncResult(success=False, error=str(e))
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

    def heartbeat(self, agent_version: str = AGENT_VERSION) -> dict:
        """Send heartbeat to server.

        Returns server commands (pause/deregister) and config update flag.
        """
        return self._request(
            "POST", "heartbeat", data={"agent_version": agent_version}
        )

    def get_status(self) -> dict:
        """Get sync status."""
        return self._request("GET", "events/status")

    # =========================================================================
    # Configuration
    # =========================================================================

    def get_config(self) -> dict:
        """Get configuration from server."""
        return self._request("GET", "config")

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
