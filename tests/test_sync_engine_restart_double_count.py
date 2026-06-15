"""Regression test: a restart (or the start-of-day backlog reconcile) must not
re-count already-counted active time into the local daily total.

Fleet incident 2026-06-15: 1.5.43 added `_reconcile_backlog`, which rewinds
bucket checkpoints to local midnight on first sync so events stranded on the
local DB during the server outage get re-sent. Re-sending is safe (the backend
dedups by AW event id). But the *local* daily time counter is deduped only by
the in-memory `_time_cache`, which is empty after a restart — so every replayed
event's full duration was being added a second time, inflating the tray's
"active time". Martin's own recovery instruction is "quit + restart", which
triggers exactly this path.

This test drives the real counting path (`_transform_window_event_with_timeout`)
with a real OfflineQueue and a real DailyTimeTracker — no mocking of the
components under test — across a simulated restart that shares both SQLite
files. Pre-fix the second pass doubles the total; post-fix it is a no-op.
"""

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from src.config import Config
from src.sync.aw_client import BUCKET_TYPE_WINDOW, AWEvent
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine

WINDOW_BUCKET = "aw-watcher-window_testhost"


def _build_engine(queue_db: Path, time_db: Path) -> SyncEngine:
    """Construct an engine backed by real persistence at the given paths.

    Each call simulates a fresh process: a new OfflineQueue and a new
    DailyTimeTracker opened on the same files the previous 'process' wrote.
    """
    return SyncEngine(
        aw=Mock(),
        bf=Mock(),
        queue=OfflineQueue(db_path=queue_db, max_size=1000),
        config=Config(),
        time_tracker=DailyTimeTracker(db_path=time_db),
    )


def test_restart_does_not_double_count_replayed_event():
    tmp = Path(tempfile.mkdtemp())
    queue_db = tmp / "offline_queue.db"
    time_db = tmp / "daily_time.db"

    # A 10-minute window event, "today" in local time.
    event = AWEvent(
        id=42,
        timestamp=datetime.now(timezone.utc),
        duration=600.0,
        data={"app": "Code", "title": "main.py"},
    )

    # --- process 1: count the event once ---
    engine1 = _build_engine(queue_db, time_db)
    out1 = engine1._transform_window_event_with_timeout(
        event, WINDOW_BUCKET, BUCKET_TYPE_WINDOW
    )
    assert out1, "window event should have been transformed and counted"
    total1 = engine1._time_tracker.get_today_active_time().total_seconds()
    assert abs(total1 - 600.0) < 1.0, f"expected ~600s counted, got {total1}"
    engine1.queue.close()
    engine1._time_tracker.close()

    # --- process 2: restart + reconcile replays the SAME event ---
    engine2 = _build_engine(queue_db, time_db)
    # The new process starts from the persisted daily total (600s already).
    assert abs(engine2._time_tracker.get_today_active_time().total_seconds() - 600.0) < 1.0
    engine2._transform_window_event_with_timeout(
        event, WINDOW_BUCKET, BUCKET_TYPE_WINDOW
    )
    total2 = engine2._time_tracker.get_today_active_time().total_seconds()

    # The replay must be a no-op for the daily total. Pre-fix this was ~1200s.
    assert abs(total2 - 600.0) < 1.0, (
        f"replayed event double-counted: total is {total2}s, expected ~600s"
    )
    engine2.queue.close()
    engine2._time_tracker.close()


def test_heartbeat_extension_counts_only_growth_across_restart():
    """An event whose duration grows after restart counts only the new seconds,
    not the whole grown duration."""
    tmp = Path(tempfile.mkdtemp())
    queue_db = tmp / "offline_queue.db"
    time_db = tmp / "daily_time.db"

    ts = datetime.now(timezone.utc)
    short = AWEvent(id=7, timestamp=ts, duration=300.0, data={"app": "Code", "title": "x"})

    engine1 = _build_engine(queue_db, time_db)
    engine1._transform_window_event_with_timeout(short, WINDOW_BUCKET, BUCKET_TYPE_WINDOW)
    assert abs(engine1._time_tracker.get_today_active_time().total_seconds() - 300.0) < 1.0
    engine1.queue.close()
    engine1._time_tracker.close()

    # Restart; the same event id now reports a grown duration (heartbeat).
    grown = AWEvent(id=7, timestamp=ts, duration=500.0, data={"app": "Code", "title": "x"})
    engine2 = _build_engine(queue_db, time_db)
    engine2._transform_window_event_with_timeout(grown, WINDOW_BUCKET, BUCKET_TYPE_WINDOW)
    total = engine2._time_tracker.get_today_active_time().total_seconds()
    # 300 (already) + 200 (growth) = 500, not 300 + 500 = 800.
    assert abs(total - 500.0) < 1.0, f"expected ~500s, got {total}"
    engine2.queue.close()
    engine2._time_tracker.close()
