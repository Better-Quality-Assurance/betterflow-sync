"""Regression test: a long-running agent must prune persisted counted-time on a
local-day rollover, not only at process start.

`prune_counted_time` was called solely from `_load_counted_time_cache`, which
runs at construction (and on an explicit reset). An agent left running across
midnight therefore never pruned — every day it stayed up leaked that day's
counted-time rows into the SQLite store, and yesterday's in-memory dedup entries
lingered. `sync()` now calls `_maybe_rollover_counted_day` each cycle, which is a
no-op on the same day and, on a rollover, clears the per-day dedup cache and
reloads (which prunes prior days).

Drives the real OfflineQueue persistence + the real BoundedLRU cache; no mocking
of the components under test.
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock

from src.config import Config
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine


def _build_engine(queue_db: Path, time_db: Path) -> SyncEngine:
    engine = SyncEngine(
        aw=Mock(),
        bf=Mock(),
        queue=OfflineQueue(db_path=queue_db, max_size=1000),
        config=Config(),
        time_tracker=Mock(),
    )
    # Exit the sync cycle right after the rollover housekeeping (which is all
    # this test exercises): no server config fetch, no AW work.
    engine.bf.is_reachable.return_value = False
    engine.aw.is_running.return_value = False
    return engine


def test_day_rollover_prunes_yesterdays_counted_time_without_restart():
    tmp = Path(tempfile.mkdtemp())
    engine = _build_engine(tmp / "offline_queue.db", tmp / "time.db")

    today = engine._counted_cache_day
    yesterday = "2000-01-01"  # any day strictly before "today"

    # A counted-time row persisted "yesterday", plus a lingering in-memory
    # dedup entry from that day.
    engine.queue.set_counted_time("aw-window", "evt-1", 123.0, yesterday)
    engine._time_cache[("aw-window", "evt-1")] = 123.0
    assert engine.queue.get_counted_times(yesterday), "precondition: yesterday persisted"

    # Pretend the cache was last loaded yesterday, then run a sync cycle. No AW,
    # so sync() bails early after the rollover housekeeping — which is the point.
    engine._counted_cache_day = yesterday
    engine.sync()

    assert engine._counted_cache_day == today, "cache day advanced to today"
    assert engine.queue.get_counted_times(yesterday) == {}, (
        "yesterday's counted-time must be pruned on rollover"
    )
    assert ("aw-window", "evt-1") not in engine._time_cache, (
        "stale dedup entry from yesterday must be cleared"
    )

    engine.queue.close()


def test_same_day_sync_does_not_prune_or_clear():
    """Control: within the same day the rollover check is a cheap no-op."""
    tmp = Path(tempfile.mkdtemp())
    engine = _build_engine(tmp / "offline_queue.db", tmp / "time.db")

    today = engine._counted_cache_day
    engine.queue.set_counted_time("aw-window", "evt-2", 50.0, today)
    engine._time_cache[("aw-window", "evt-2")] = 50.0

    engine.sync()

    assert engine._counted_cache_day == today
    assert engine.queue.get_counted_times(today), "today's counted-time is retained"
    assert ("aw-window", "evt-2") in engine._time_cache, "today's dedup entry retained"

    engine.queue.close()
