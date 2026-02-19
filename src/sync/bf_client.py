"""BetterFlow API client - syncs events to BetterFlow server."""

import gzip
import hashlib
import json
import logging
import platform
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests

from config import DEFAULT_API_URL

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
        self._web_base_url: Optional[str] = None
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
        """Make request to BetterFlow API.

        Returns the unwrapped 'data' field from the API response envelope.
        Backend wraps all responses as: {"success": bool, "data": {...}, "meta": {...}}
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

        try:
            response = self._session.request(method, url, **kwargs)

            if response.status_code == 401:
                raise BetterFlowAuthError("Invalid or expired API token")
            if response.status_code == 403:
                raise BetterFlowAuthError("Device not authorized")

            response.raise_for_status()
            body = response.json() if response.content else {}

            # Unwrap the API envelope — backend returns {success, data, meta}
            if isinstance(body, dict) and "data" in body:
                return body["data"]
            return body

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
            self._request("GET", "health")
            return True
        except BetterFlowClientError:
            # Try the status endpoint as fallback
            try:
                self._request("GET", "events/status")
                return True
            except BetterFlowClientError:
                return False

    def exchange_code(self, code: str, device_name: str) -> AuthResult:
        """Exchange an authorization code for a Sanctum token.

        Args:
            code: 64-char authorization code from browser flow
            device_name: Name for this device token

        Returns:
            AuthResult with api_token on success
        """
        url = f"{self.web_base_url}/api/v1/sync/auth/token"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "BetterFlow-Sync/1.0.0",
        }
        try:
            response = self._session.post(
                url,
                json={
                    "code": code,
                    "device_name": device_name,
                    "platform": DeviceInfo.collect().platform_key,
                    "os_version": platform.release(),
                    "machine_id": DeviceInfo.collect().machine_id,
                    "agent_version": AGENT_VERSION,
                },
                headers=headers,
                timeout=self.timeout,
            )
            if response.status_code in (401, 403, 422):
                try:
                    data = response.json()
                    msg = data.get("message", response.reason)
                except Exception:
                    msg = response.text or response.reason
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
        except Exception as e:
            return AuthResult(success=False, error=str(e))

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
