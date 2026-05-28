"""Shared data models and utilities for the sync engine.

Contains:
- SyncStats       — per-cycle statistics dataclass
- BoundedLRU      — ordered-dict LRU cache with hard-cap eviction
- String length constants for event field validation
"""

from collections import OrderedDict
from dataclasses import dataclass, field

# Field-length caps (applied before sending to the API)

MAX_APP_LENGTH = 256
MAX_TITLE_LENGTH = 1024
MAX_URL_LENGTH = 2048


# SyncStats


@dataclass
class SyncStats:
    """Statistics from a sync cycle."""

    events_fetched: int = 0
    events_filtered: int = 0
    events_sent: int = 0
    events_queued: int = 0
    buckets_synced: int = 0
    gaps_filled: int = 0
    calls_detected: int = 0
    errors: list[str] = field(default_factory=list)
    queued_bucket_ids: set = field(default_factory=set)
    _should_heartbeat: bool = False

    @property
    def success(self) -> bool:
        return len(self.errors) == 0


# BoundedLRU


class BoundedLRU:
    """Ordered dict with hard-capped size.

    Writes move the key to the end (most-recent). When the size exceeds
    ``maxsize`` the oldest entry is evicted. This replaces three nearly
    identical hand-rolled caches (_sent_cache, _gap_filled_originals,
    _time_cache) that each duplicated the same eviction logic.

    Not thread-safe on its own — callers must wrap operations in a lock
    when shared across threads, exactly as the original caches did.

    Does NOT inherit from dict — callers must use the explicit API below.
    """

    def __init__(self, maxsize: int) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self._maxsize = maxsize
        self._od: OrderedDict = OrderedDict()

    def get(self, key, default=None):
        if key in self._od:
            self._od.move_to_end(key)
            return self._od[key]
        return default

    def __contains__(self, key) -> bool:
        return key in self._od

    def __getitem__(self, key):
        self._od.move_to_end(key)
        return self._od[key]

    def __setitem__(self, key, value) -> None:
        self._od[key] = value
        self._od.move_to_end(key)
        if len(self._od) > self._maxsize:
            self._od.popitem(last=False)

    def __len__(self) -> int:
        return len(self._od)
