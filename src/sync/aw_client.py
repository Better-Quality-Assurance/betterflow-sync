"""ActivityWatch client - reads events from local aw-server."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

# ActivityWatch bucket types we care about
# aw-server-rust uses "aw-watcher-window" / "aw-watcher-afk"
# aw-server (Python) uses "currentwindow" / "afkstatus"
BUCKET_TYPE_WINDOW = "currentwindow"
BUCKET_TYPE_WINDOW_ALT = "aw-watcher-window"
BUCKET_TYPE_AFK = "afkstatus"
BUCKET_TYPE_AFK_ALT = "aw-watcher-afk"
BUCKET_TYPE_WEB = "aw-watcher-web"
BUCKET_TYPE_INPUT = "aw-watcher-input"  # Keystroke/click tracking for fraud detection


@dataclass
class AWEvent:
    """Represents an ActivityWatch event."""

    id: int
    timestamp: datetime
    duration: float  # seconds
    data: dict

    @classmethod
    def from_dict(cls, data: dict) -> "AWEvent":
        """Create AWEvent from API response."""
        timestamp = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
        return cls(
            id=data.get("id", 0),
            timestamp=timestamp,
            duration=data.get("duration", 0),
            data=data.get("data", {}),
        )

    @property
    def app(self) -> Optional[str]:
        """Get app name from event data."""
        return self.data.get("app")

    @property
    def title(self) -> Optional[str]:
        """Get window title from event data."""
        return self.data.get("title")

    @property
    def url(self) -> Optional[str]:
        """Get URL from event data (browser events)."""
        return self.data.get("url")

    @property
    def status(self) -> Optional[str]:
        """Get AFK status from event data."""
        return self.data.get("status")

    @property
    def presses(self) -> int:
        """Get keystroke count from input event."""
        return self.data.get("presses", 0)

    @property
    def clicks(self) -> int:
        """Get mouse click count from input event."""
        return self.data.get("clicks", 0)

    @property
    def scrolls(self) -> int:
        """Get scroll count from input event."""
        return self.data.get("scrolls", 0)


@dataclass
class AWBucket:
    """Represents an ActivityWatch bucket."""

    id: str
    name: str
    type: str
    client: str
    hostname: str
    created: datetime

    @classmethod
    def from_dict(cls, bucket_id: str, data: dict) -> "AWBucket":
        """Create AWBucket from API response."""
        created = datetime.fromisoformat(data["created"].replace("Z", "+00:00"))
        return cls(
            id=bucket_id,
            name=data.get("name", bucket_id),
            type=data.get("type", ""),
            client=data.get("client", ""),
            hostname=data.get("hostname", ""),
            created=created,
        )


class AWClientError(Exception):
    """ActivityWatch client error."""

    pass


class AWClient:
    """Client for reading from local ActivityWatch server."""

    def __init__(self, host: str = "localhost", port: int = 5600, timeout: int = 10):
        """Initialize ActivityWatch client.

        Args:
            host: ActivityWatch server host
            port: ActivityWatch server port
            timeout: Request timeout in seconds
        """
        self.base_url = f"http://{host}:{port}/api/0/"
        self.timeout = timeout
        self._session = requests.Session()

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        """Make request to ActivityWatch API."""
        url = urljoin(self.base_url, endpoint)
        kwargs.setdefault("timeout", self.timeout)

        try:
            response = self._session.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json() if response.content else {}
        except requests.exceptions.ConnectionError as e:
            raise AWClientError(f"Cannot connect to ActivityWatch at {self.base_url}") from e
        except requests.exceptions.Timeout as e:
            raise AWClientError(f"ActivityWatch request timed out") from e
        except requests.exceptions.HTTPError as e:
            raise AWClientError(f"ActivityWatch API error: {e}") from e
        except Exception as e:
            raise AWClientError(f"Unexpected error: {e}") from e

    def is_running(self) -> bool:
        """Check if ActivityWatch server is running."""
        try:
            self.get_info()
            return True
        except AWClientError:
            return False

    def get_info(self) -> dict:
        """Get server info (version, hostname, etc.)."""
        return self._request("GET", "info")

    def get_buckets(self) -> dict[str, AWBucket]:
        """Get all buckets."""
        response = self._request("GET", "buckets/")
        return {
            bucket_id: AWBucket.from_dict(bucket_id, data)
            for bucket_id, data in response.items()
        }

    def get_bucket(self, bucket_id: str) -> Optional[AWBucket]:
        """Get a specific bucket."""
        try:
            response = self._request("GET", f"buckets/{bucket_id}")
            return AWBucket.from_dict(bucket_id, response)
        except AWClientError:
            return None

    def get_events(
        self,
        bucket_id: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 1000,
    ) -> list[AWEvent]:
        """Get events from a bucket.

        Args:
            bucket_id: The bucket to query
            start: Start time (inclusive)
            end: End time (inclusive)
            limit: Maximum events to return

        Returns:
            List of AWEvent objects, newest first
        """
        params = {"limit": limit}
        if start:
            params["start"] = start.isoformat()
        if end:
            params["end"] = end.isoformat()

        response = self._request("GET", f"buckets/{bucket_id}/events", params=params)
        return [AWEvent.from_dict(event) for event in response]

    def get_window_buckets(self) -> list[AWBucket]:
        """Get all window watcher buckets."""
        buckets = self.get_buckets()
        return [b for b in buckets.values() if b.type in (BUCKET_TYPE_WINDOW, BUCKET_TYPE_WINDOW_ALT)]

    def get_afk_buckets(self) -> list[AWBucket]:
        """Get all AFK watcher buckets."""
        buckets = self.get_buckets()
        return [b for b in buckets.values() if b.type in (BUCKET_TYPE_AFK, BUCKET_TYPE_AFK_ALT)]

    def get_web_buckets(self) -> list[AWBucket]:
        """Get all web watcher buckets."""
        buckets = self.get_buckets()
        return [b for b in buckets.values() if b.type == BUCKET_TYPE_WEB]

    def get_input_buckets(self) -> list[AWBucket]:
        """Get all input watcher buckets (keystroke/click tracking)."""
        buckets = self.get_buckets()
        return [b for b in buckets.values() if b.type == BUCKET_TYPE_INPUT]

    def get_events_since(
        self, bucket_id: str, since: datetime, limit: int = 1000
    ) -> list[AWEvent]:
        """Get events since a specific timestamp.

        Convenience method for incremental sync.
        """
        # ActivityWatch returns events newest-first, so we get events
        # between 'since' and now
        now = datetime.now(timezone.utc)
        return self.get_events(bucket_id, start=since, end=now, limit=limit)

    def get_hostname(self) -> str:
        """Get the hostname from server info."""
        info = self.get_info()
        return info.get("hostname", "unknown")

    def close(self) -> None:
        """Close the session."""
        if self._session is not None:
            self._session.close()
            self._session = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self) -> "AWClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
