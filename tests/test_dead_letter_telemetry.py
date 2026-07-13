"""Dead-letter visibility + bounded replay wiring.

PR #129 made ``remove_failed`` MOVE exhausted events into ``dead_letter_events``
(preserved for inspection/replay) instead of hard-deleting them, but nothing in
production ever surfaced the table's size or re-enqueued its rows. A silently
growing dead-letter is real lost activity that no one can see. These tests pin
the two production hooks:

1. the agent's health/heartbeat telemetry reports ``dead_letter_count`` so a
   growing table is visible to ops, and
2. the sync cycle drives the bounded ``requeue_storable_dead_letter`` replay so
   rows that are storable again get another delivery attempt.
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock

from src.main import SyncCoordinator
from src.sync.queue import OfflineQueue


def _dead_letter_some(queue: OfflineQueue, events: list) -> None:
    """Drive ``events`` to the retry ceiling and move them to dead_letter."""
    queue.enqueue(events)
    queued = queue.dequeue(batch_size=100)
    for _ in range(5):
        queue.increment_retry([q.id for q in queued])
    queue.remove_failed(max_retries=5, last_error="test")


def _coordinator_with_queue(queue: OfflineQueue) -> SyncCoordinator:
    aw_manager = MagicMock()
    aw_manager.health_snapshot.return_value = {}  # clean mapping for .update()
    tray = MagicMock()
    tray.model = MagicMock()
    return SyncCoordinator(
        config=MagicMock(),
        aw=MagicMock(),
        bf=MagicMock(),
        queue=queue,
        sync_engine=MagicMock(),
        tray=tray,
        aw_manager=aw_manager,
    )


def test_health_telemetry_exposes_dead_letter_count():
    """A non-empty dead-letter table must surface in the heartbeat telemetry so
    ops can see it growing (the whole point of preserving rows)."""
    tmp = Path(tempfile.mkdtemp())
    queue = OfflineQueue(db_path=tmp / "q.db", max_size=1000)
    try:
        _dead_letter_some(queue, [
            {"id": "a", "bucket_id": "aw-watcher-afk_h",
             "timestamp": "2026-07-13T05:00:00+00:00", "data": {}},
            {"id": "b", "bucket_id": "aw-watcher-window_h",
             "timestamp": "2026-07-13T05:01:00+00:00", "data": {}},
        ])
        assert queue.dead_letter_count() == 2

        coord = _coordinator_with_queue(queue)
        telemetry = coord._build_health_telemetry()

        assert telemetry.get("dead_letter_count") == 2
    finally:
        queue.close()


def test_health_telemetry_reports_zero_when_dead_letter_empty():
    """Reported even at 0 — an explicit healthy signal, not an omission ops has
    to interpret."""
    tmp = Path(tempfile.mkdtemp())
    queue = OfflineQueue(db_path=tmp / "q.db", max_size=1000)
    try:
        coord = _coordinator_with_queue(queue)
        telemetry = coord._build_health_telemetry()
        assert telemetry.get("dead_letter_count") == 0
    finally:
        queue.close()


def test_process_queue_replays_storable_dead_letter():
    """The sync cycle's queue processing must drive the bounded replay so a
    dead-lettered row that is storable again is resurrected into the live queue
    (and an unstorable one is left behind)."""
    from src.config import Config
    from src.sync.bf_client import SyncResult
    from src.sync.daily_time_tracker import DailyTimeTracker
    from src.sync.sync_engine import SyncEngine, SyncStats

    tmp = Path(tempfile.mkdtemp())
    queue = OfflineQueue(db_path=tmp / "q.db", max_size=1000)
    tt = DailyTimeTracker(db_path=tmp / "t.db")
    now = datetime.now(timezone.utc)
    try:
        _dead_letter_some(queue, [
            {"id": "storable", "bucket_id": "aw-watcher-afk_h",
             "timestamp": (now - timedelta(hours=1)).isoformat(), "data": {}},
            {"id": "stale", "bucket_id": "aw-watcher-window_h",
             "timestamp": (now - timedelta(days=30)).isoformat(), "data": {}},
        ])
        assert queue.dead_letter_count() == 2
        # Backdate the drop stamp so both rows are past the replay cooldown (the
        # gate that stops a just-dropped poison batch from being resurrected into
        # the same rejection). This simulates rows that have sat a while.
        old = (now - timedelta(hours=2)).isoformat()
        with queue._cursor() as cur:
            cur.execute("UPDATE dead_letter_events SET dropped_at = ?", (old,))

        bf = Mock()
        bf.send_events = Mock(return_value=SyncResult(success=True, events_synced=1))
        engine = SyncEngine(aw=Mock(), bf=bf, queue=queue, config=Config(), time_tracker=tt)
        # Keep the drain out of backoff and give the cycle a fresh budget.
        engine._cycle_start_monotonic = None
        engine._process_queue(SyncStats())

        # The storable row was resurrected and then delivered; the stale row is
        # still dead-lettered (never resurrected — the server would reject it).
        assert queue.dead_letter_count() == 1
        remaining = queue.get_dead_letter_events()
        assert remaining[0]["bucket_id"] == "aw-watcher-window_h"
        # The resurrected event actually reached the server this cycle.
        sent_ids = {
            e.get("id")
            for call in bf.send_events.call_args_list
            for e in call.args[0]
        }
        assert "storable" in sent_ids
        assert "stale" not in sent_ids
    finally:
        queue.close()
        tt.close()
