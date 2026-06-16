"""The backlog reconcile must ENQUEUE the whole work day's AW events so a
stranded mid-day gap reaches prod and the queue reflects the real backlog.

Regression for the 2026-06-16 incident: ~1000 events at 05:00-07:00 UTC were
captured locally but never synced, while the queue showed 0. The old reconcile
rewound the forward checkpoint and relied on a newest-first + limited fetch,
which never reached the older gap. The new reconcile pages oldest-first across
start-of-day -> now and enqueues every event (backend dedups by id).
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from src.config import Config
from src.sync.aw_client import BUCKET_TYPE_INPUT, AWBucket, AWEvent
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine


def _engine(tmp: Path, aw) -> SyncEngine:
    return SyncEngine(
        aw=aw,
        bf=Mock(),
        queue=OfflineQueue(db_path=tmp / "q.db", max_size=100000),
        config=Config(),
        time_tracker=DailyTimeTracker(db_path=tmp / "t.db"),
    )


def test_reconcile_enqueues_a_stranded_mid_day_gap():
    tmp = Path(tempfile.mkdtemp())

    # A stranded gap: ~400 input events spread across the MIDDLE of today, well
    # before "now". Under the old newest-first+limit fetch these were skipped.
    now = datetime.now(timezone.utc)
    day_start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    gap_start = day_start + timedelta(hours=5)
    gap_events = [
        AWEvent(
            id=1000 + i,
            timestamp=gap_start + timedelta(seconds=10 * i),
            duration=10.0,
            data={"app": "Terminal", "bundle": "com.apple.Terminal", "presses": 5},
        )
        for i in range(400)
    ]
    bucket_id = "aw-watcher-input_test"

    # Mock AW: return only events whose timestamp falls in [start, end), so the
    # paged forward windows reproduce real per-window fetches.
    aw = Mock()

    def get_events(bid, start=None, end=None, limit=1000):
        assert bid == bucket_id
        return [e for e in gap_events if (start is None or e.timestamp >= start) and (end is None or e.timestamp < end)]

    aw.get_events.side_effect = get_events

    engine = _engine(tmp, aw)
    try:
        bucket = AWBucket(id=bucket_id, name=bucket_id, type=BUCKET_TYPE_INPUT, client="", hostname="test", created=now)
        assert engine.queue.size() == 0

        engine._reconcile_backlog([bucket])

        # Every stranded event is now queued to drain (none lost to newest-first).
        assert engine.queue.size() == 400, f"expected 400 enqueued, got {engine.queue.size()}"

        # And they cover the gap window, oldest included (id 1000 = the very first).
        queued = engine.queue.dequeue(batch_size=400, max_retries=5)
        ids = {e.event_data.get("id") for e in queued}
        assert 1000 in ids and 1399 in ids, "oldest and newest gap events must both be enqueued"
    finally:
        engine.queue.close()
        engine._time_tracker.close()
