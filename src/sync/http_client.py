"""Base HTTP client with retry logic for BetterFlow API."""

import gzip
import json
import logging
import os
from typing import Optional
from urllib.parse import urlparse

import requests

from .retry import RetryConfig, retry_with_backoff, RetryExhausted

__all__ = [
    "BaseApiClient",
    "BetterFlowClientError",
    "BetterFlowAuthError",
]

logger = logging.getLogger(__name__)


class BetterFlowClientError(Exception):
    """BetterFlow client error."""

    pass


class BetterFlowAuthError(BetterFlowClientError):
    """Authentication error."""

    pass


class _TransientError(Exception):
    """Internal: Marks an error as transient/retryable."""

    pass


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

    DEFAULT_RETRY_CONFIG = RetryConfig(
        max_retries=3,
        base_delay=1.0,
        max_delay=30.0,
        exponential_base=2.0,
        jitter=True,
    )

    USER_AGENT = "BetterFlow-Sync/1.0.0"

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
        self._session = session or requests.Session()
        self._owns_session = session is None  # Track if we created the session

    @property
    def web_base_url(self) -> str:
        """Derive the web base URL from the API URL.

        e.g. "https://betterflow.eu/api/agent" -> "https://betterflow.eu"
        """
        if self._web_base_url:
            return self._web_base_url
        parsed = urlparse(self.api_url)
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        # In some local setups, localhost is routed differently than 127.0.0.1.
        # Force loopback IP for local dev auth URLs.
        if host == "localhost":
            return f"{parsed.scheme}://127.0.0.1{port}"
        return f"{parsed.scheme}://{parsed.netloc}"

    def _get_headers(self) -> dict:
        """Get request headers with authentication."""
        headers = {
            "Accept": "application/json",
            "User-Agent": self.USER_AGENT,
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
            endpoint: API endpoint (relative to api_url)
            data: Request data
            compress: Whether to gzip compress the payload
            retry: Whether to retry on transient failures

        Returns:
            Response data as dict

        Raises:
            BetterFlowAuthError: For 401/403 responses (not retried)
            BetterFlowClientError: For other errors
        """
        url = f"{self.api_url}/{endpoint.lstrip('/')}"
        headers = self._get_headers()
        kwargs: dict = {"timeout": self.timeout, "headers": headers}

        if data:
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
                if e.last_error:
                    raise BetterFlowClientError(str(e.last_error)) from e.last_error
                raise BetterFlowClientError("Request failed after retries") from e
        else:
            try:
                return do_request()
            except _TransientError as e:
                raise BetterFlowClientError(str(e)) from e

    def set_credentials(self, token: str, device_id: str) -> None:
        """Set authentication credentials."""
        self.token = token
        self.device_id = device_id

    def clear_credentials(self) -> None:
        """Clear authentication credentials."""
        self.token = None
        self.device_id = None

    def is_reachable(self) -> bool:
        """Check if BetterFlow API is reachable."""
        try:
            self._request("GET", "health", retry=False)
            return True
        except BetterFlowClientError:
            try:
                self._request("GET", "events/status", retry=False)
                return True
            except BetterFlowClientError:
                return False

    def close(self) -> None:
        """Close the session if we own it."""
        if self._owns_session and self._session is not None:
            self._session.close()
            self._session = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self) -> "BaseApiClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
