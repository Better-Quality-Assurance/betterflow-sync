"""Protocol types for SyncEngine dependencies.

Defines the interfaces that SyncEngine requires from its collaborators,
enabling easier testing and looser coupling.
"""

from datetime import datetime
from typing import Optional, Protocol, runtime_checkable

try:
    from .aw_client import AWBucket, AWEvent
    from .bf_client import SyncResult
except ImportError:
    from sync.aw_client import AWBucket, AWEvent
    from sync.bf_client import SyncResult


@runtime_checkable
class AWClientProtocol(Protocol):
    """Interface for reading events from ActivityWatch."""

    def is_running(self) -> bool: ...

    def get_window_buckets(self) -> list[AWBucket]: ...

    def get_web_buckets(self) -> list[AWBucket]: ...

    def get_afk_buckets(self) -> list[AWBucket]: ...

    def get_input_buckets(self) -> list[AWBucket]: ...

    def get_events_since(
        self, bucket_id: str, since: datetime, limit: int = 1000
    ) -> list[AWEvent]: ...

    def get_events(
        self,
        bucket_id: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        limit: int = 1000,
    ) -> list[AWEvent]: ...


@runtime_checkable
class BFClientProtocol(Protocol):
    """Interface for sending data to BetterFlow API."""

    def is_reachable(self) -> bool: ...

    def get_config(self) -> dict: ...

    def start_session(self) -> dict: ...

    def end_session(self, reason: str = "app_quit") -> dict: ...

    def send_events(self, events: list[dict]) -> SyncResult: ...

    def heartbeat(self, agent_version: str = ...) -> dict: ...

    def get_trends(self) -> dict: ...


@runtime_checkable
class OfflineQueueProtocol(Protocol):
    """Interface for offline event storage and checkpoint tracking."""

    def get_checkpoint(self, bucket_id: str) -> Optional[datetime]: ...

    def set_checkpoint(
        self, bucket_id: str, timestamp: datetime, event_id: Optional[int] = None
    ) -> None: ...

    def get_all_checkpoints(self) -> dict[str, datetime]: ...

    def is_empty(self) -> bool: ...

    def enqueue(self, events: list[dict]) -> None: ...

    def dequeue(self, limit: int) -> list: ...

    def remove(self, event_ids: list[int]) -> None: ...

    def remove_failed(self, max_retries: int = 5) -> None: ...

    def increment_retry(self, event_ids: list[int]) -> None: ...

    def size(self) -> int: ...

    def get_category(self, app_name: str) -> Optional[str]: ...

    def get_all_categories(self) -> dict[str, str]: ...
