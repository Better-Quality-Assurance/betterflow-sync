"""ActivityWatch client - reads events from local aw-server."""

import logging
import threading
import time
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
BUCKET_TYPE_CALL = "call"
BUCKET_TYPE_DEV_SESSION = "dev-session"  # Foreground-CPU activity (engaged, no input)

# data.status values on call-bucket events (window calls AND mic sessions) —
# a server contract: "ongoing" = per-cycle live snapshot of a still-open
# meeting (same deterministic id keeps upserting one growing row); "completed"
# = the meeting really ended. Constants so a rename can't miss a producer.
CALL_STATUS_ONGOING = "ongoing"
CALL_STATUS_COMPLETED = "completed"


@dataclass(frozen=True)
class AWEvent:
    """Represents an ActivityWatch event (immutable)."""

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
        self._session_lock = threading.Lock()
        self._buckets_lock = threading.Lock()
        self._buckets_cache: Optional[dict[str, "AWBucket"]] = None
        self._buckets_cache_time: float = 0.0
        self._buckets_cache_ttl: float = 30.0

    # Connection-failure recovery: reset the pooled session and retry with a
    # short backoff. The first retry (after reset) fixes a rotted client socket
    # immediately; the later backoff retries ride out a brief server stall.
    _CONNECT_ATTEMPTS = 3
    _CONNECT_BACKOFF = (0.25, 0.5)  # seconds before retry 2 and 3

    def _request(self, method: str, endpoint: str, timeout: Optional[int] = None, **kwargs) -> dict:
        """Make request to ActivityWatch API.

        On a connection failure the pooled session is reset and the request
        retried with a short backoff. A stale keep-alive socket to the LOCAL
        server raises ConnectionError even though the server is up (the socket
        rots after the server has been alive a long time). This is why a manual
        app restart always "fixed" sync stalls — a fresh process builds a fresh
        session. Doing it here lets the agent self-heal automatically instead of
        silently dropping syncs until the user notices missing hours
        (furdui.iancu, 2026-06-17).
        """
        url = urljoin(self.base_url, endpoint)
        kwargs["timeout"] = timeout if timeout is not None else self.timeout

        last_exc: Optional[Exception] = None
        for attempt in range(self._CONNECT_ATTEMPTS):
            with self._session_lock:
                session = self._session
            if session is None:
                raise AWClientError("AWClient has been closed")
            try:
                response = session.request(method, url, **kwargs)
                response.raise_for_status()
                return response.json() if response.content else {}
            except requests.exceptions.ConnectionError as e:
                last_exc = e
                # Reset the (possibly stale) pooled connections and back off
                # before retrying; only the final attempt gives up.
                if attempt < self._CONNECT_ATTEMPTS - 1:
                    with self._session_lock:
                        still_open = self._session is not None
                    if still_open:
                        logger.info(
                            "AW connection failed (%s) — reset+retry %d/%d",
                            e, attempt + 1, self._CONNECT_ATTEMPTS - 1,
                        )
                        self.reset_session()
                        time.sleep(self._CONNECT_BACKOFF[min(attempt, len(self._CONNECT_BACKOFF) - 1)])
                        continue
                raise AWClientError(f"Cannot connect to ActivityWatch at {self.base_url}") from e
            except requests.exceptions.Timeout as e:
                raise AWClientError("ActivityWatch request timed out") from e
            except requests.exceptions.HTTPError as e:
                raise AWClientError(f"ActivityWatch API error: {e}") from e
            except Exception as e:
                raise AWClientError(f"Unexpected error: {e}") from e
        # Unreachable: the loop either returns or raises on every path.
        raise AWClientError(f"Cannot connect to ActivityWatch at {self.base_url}") from last_exc

    def reset_session(self) -> None:
        """Drop pooled connections and create a fresh session.

        Call after system wake to avoid stale TCP connections.
        Thread-safe: swaps the reference under lock, closes old outside.
        Also invalidates the bucket cache since monotonic clock pauses
        during macOS sleep, making the TTL check unreliable after wake.
        """
        with self._session_lock:
            old = self._session
            if old is None:
                return  # already closed, do not resurrect
            self._session = requests.Session()
        try:
            old.close()
        except Exception as e:
            logger.debug("Closing old AW session raised: %s", e)
        with self._buckets_lock:
            self._buckets_cache = None
            self._buckets_cache_time = 0.0

    def is_running(self) -> bool:
        """Check if ActivityWatch server is running.

        Uses a shorter timeout (3s) to fail fast on unreachable servers (N6).
        """
        try:
            self._request("GET", "info", timeout=3)
            return True
        except AWClientError:
            return False

    def get_info(self) -> dict:
        """Get server info (version, hostname, etc.)."""
        return self._request("GET", "info")

    def get_buckets(self) -> dict[str, "AWBucket"]:
        """Get all buckets (cached with 30s TTL).

        The HTTP request is made outside the lock to avoid blocking
        other threads while waiting for ActivityWatch to respond.
        """
        with self._buckets_lock:
            now = time.monotonic()
            if self._buckets_cache is not None and (now - self._buckets_cache_time) < self._buckets_cache_ttl:
                return self._buckets_cache

        # Fetch outside lock to avoid blocking other callers
        response = self._request("GET", "buckets/")
        result = {
            bucket_id: AWBucket.from_dict(bucket_id, data)
            for bucket_id, data in response.items()
        }

        with self._buckets_lock:
            # Re-check in case another thread populated the cache while we fetched
            now2 = time.monotonic()
            if self._buckets_cache is not None and (now2 - self._buckets_cache_time) < self._buckets_cache_ttl:
                return self._buckets_cache
            self._buckets_cache = result
            self._buckets_cache_time = now2
            return result

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

    def get_latest_afk_event(self) -> Optional[AWEvent]:
        """Return the latest AFK event from the active BetterFlow bucket.

        Old installs can have both BetterFlow's ``bf-idle-tracker`` bucket and
        stale vanilla ActivityWatch AFK buckets. A stale bucket frozen on
        ``afk`` must never win the live idle decision while the BetterFlow
        bucket is reporting ``not-afk``. Prefer BetterFlow-owned buckets; if
        they have no readable events, fall back to the newest event from any
        AFK bucket.
        """
        buckets = self.get_afk_buckets()
        if not buckets:
            return None

        preferred = [
            bucket
            for bucket in buckets
            if "bf-idle-tracker" in bucket.id
            or "bf-idle-tracker" in bucket.name
            or "bf-idle-tracker" in bucket.client
        ]

        latest = self._latest_event_from_buckets(preferred)
        if latest is not None:
            return latest
        return self._latest_event_from_buckets(buckets)

    def _latest_event_from_buckets(self, buckets: list[AWBucket]) -> Optional[AWEvent]:
        latest: Optional[AWEvent] = None
        for bucket in buckets:
            try:
                events = self.get_events(bucket.id, limit=1)
            except AWClientError as e:
                logger.debug("AFK bucket %s latest-event fetch failed: %s", bucket.id, e)
                continue
            if not events:
                continue
            event = events[0]
            if latest is None or event.timestamp > latest.timestamp:
                latest = event
        return latest

    def get_web_buckets(self) -> list[AWBucket]:
        """Get all web watcher buckets."""
        buckets = self.get_buckets()
        return [b for b in buckets.values() if b.type == BUCKET_TYPE_WEB]

    def get_input_buckets(self) -> list[AWBucket]:
        """Get all input watcher buckets (keystroke/click tracking)."""
        buckets = self.get_buckets()
        return [b for b in buckets.values() if b.type == BUCKET_TYPE_INPUT]

    def create_bucket(self, bucket_id: str, bucket_type: str, hostname: str) -> None:
        """Create a bucket (idempotent — AW ignores if already exists)."""
        self._request("POST", f"buckets/{bucket_id}", json={
            "client": "betterflow",
            "type": bucket_type,
            "hostname": hostname,
        })

    def post_heartbeat(self, bucket_id: str, timestamp: str, data: dict, pulsetime: float = 5.0) -> None:
        """Send a heartbeat event (AW merges with previous if same data within pulsetime)."""
        self._request("POST", f"buckets/{bucket_id}/heartbeat?pulsetime={pulsetime}", json={
            "timestamp": timestamp,
            "duration": 0,
            "data": data,
        })

    def post_events(self, bucket_id: str, events: list[dict]) -> None:
        """Insert events into a bucket (no merging, unlike heartbeat)."""
        self._request("POST", f"buckets/{bucket_id}/events", json=events)

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
        """Close the session and null out the reference.

        Thread-safe: acquires _session_lock so concurrent _request()
        calls snapshot either the live session or None.
        """
        with self._session_lock:
            session = self._session
            self._session = None
        if session is not None:
            try:
                session.close()
            except Exception as e:
                logger.debug("AW session close failed: %s", e)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            # __del__ must not raise; any error here is unrecoverable.
            pass

    def __enter__(self) -> "AWClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
