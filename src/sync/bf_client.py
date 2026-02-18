"""BetterFlow API client - syncs events to BetterFlow server."""

import gzip
import json
import logging
import platform
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

import requests

from ..config import DEFAULT_API_URL

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


class BetterFlowClient:
    """Client for syncing events to BetterFlow server."""

    def __init__(
        self,
        api_url: str = DEFAULT_API_URL,
        token: Optional[str] = None,
        device_id: Optional[str] = None,
        compress: bool = True,
        timeout: int = 30,
    ):
        """Initialize BetterFlow client.

        Args:
            api_url: BetterFlow API base URL
            token: API token for authentication
            device_id: Device ID from registration
            compress: Use gzip compression for event batches
            timeout: Request timeout in seconds
        """
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.device_id = device_id
        self.compress = compress
        self.timeout = timeout
        self._session = requests.Session()

    def _get_headers(self) -> dict:
        """Get request headers."""
        headers = {
            "Accept": "application/json",
            "User-Agent": f"BetterFlow-Sync/1.0.0",
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
    ) -> dict:
        """Make request to BetterFlow API."""
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

        try:
            response = self._session.request(method, url, **kwargs)

            if response.status_code == 401:
                raise BetterFlowAuthError("Invalid or expired API token")
            if response.status_code == 403:
                raise BetterFlowAuthError("Device not authorized")

            response.raise_for_status()
            return response.json() if response.content else {}

        except requests.exceptions.ConnectionError as e:
            raise BetterFlowClientError(f"Cannot connect to BetterFlow API") from e
        except requests.exceptions.Timeout as e:
            raise BetterFlowClientError(f"Request timed out") from e
        except requests.exceptions.HTTPError as e:
            error_detail = ""
            try:
                error_detail = e.response.json().get("message", "")
            except Exception:
                pass
            raise BetterFlowClientError(
                f"API error ({e.response.status_code}): {error_detail or str(e)}"
            ) from e

    def is_reachable(self) -> bool:
        """Check if BetterFlow API is reachable."""
        try:
            self._request("GET", "health")
            return True
        except BetterFlowClientError:
            # Try the status endpoint as fallback
            try:
                self._request("GET", "events/status")
                return True
            except BetterFlowClientError:
                return False

    def register(
        self, email: str, password: str, device_info: Optional[DeviceInfo] = None
    ) -> AuthResult:
        """Register this device with BetterFlow.

        Args:
            email: User's BetterFlow email
            password: User's BetterFlow password
            device_info: Optional device information

        Returns:
            AuthResult with device_id and api_token on success
        """
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
