"""The offline queue must survive a server outage without dropping real activity.

Origin: 2026-06-30 — internal-tool2 deploy `e2fd9a72` failed its healthcheck and
betterflow.eu 500'd for ~1-2h. Agents on 1.5.84 dropped real tracked activity;
Azorel reported a burst of "Dropped N queued event(s) after max retries — likely
real lost activity" (12–100 events, spans up to ~18.5h; buckets
aw-watcher-input / aw-watcher-window / bf-afk-inproc).

Root cause: `_process_queue` incremented `retry_count` for the WHOLE batch on any
whole-batch failure — server down / 5xx / timeout / no delivery confirmation. So
`retry_count` counted "cycles the server was down", not "times the event was
rejected". After 5 down-cycles the events crossed `max_retries=5` and
`remove_failed` deleted them — permanent loss of good activity the server never
actually refused.

Fix: only count a retry when the server DEFINITIVELY rejected the batch (a 4xx,
surfaced as `SyncResult.transient is False`). A transient failure holds the
events at their current `retry_count` to retry when the server returns; a
genuinely poison 4xx batch still increments so it can't head-of-line-block the
queue forever.

`test_outage_does_not_drop_events` fails pre-fix (events dropped after 5 cycles).
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import Mock

from src.config import Config
from src.sync.bf_client import SyncResult
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine, SyncStats


def _engine(tmp: Path, batch_size: Optional[int] = None) -> SyncEngine:
    config = Config()
    if batch_size is not None:
        config.sync.batch_size = batch_size
    return SyncEngine(
        aw=Mock(),
        bf=Mock(),
        queue=OfflineQueue(db_path=tmp / "q.db", max_size=10000),
        config=config,
        time_tracker=Mock(),
    )


def _events(n: int) -> list[dict]:
    """n storable events — recent timestamp + bucket_id, so a drop counts as
    'real loss' (not a benign unstorable flush)."""
    now = datetime.now(timezone.utc).isoformat()
    return [
        {
            "id": f"evt-{i}",
            "timestamp": now,
            "duration": 60.0,
            "bucket_id": "aw-watcher-window_host",
            "data": {"app": "Terminal"},
        }
        for i in range(n)
    ]


def _run_cycles(engine: SyncEngine, n: int) -> None:
    """Run n queue-processing cycles, clearing the backoff gate before each so
    every cycle actually attempts a send (mirrors a sustained outage)."""
    for _ in range(n):
        engine._queue_backoff_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        engine._process_queue(SyncStats())


def test_outage_does_not_drop_events():
    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)
    engine.queue.enqueue(_events(10))
    assert engine.queue.size() == 10

    # Server is DOWN: every batch fails transiently (no per-event verdict).
    engine.bf.send_events = Mock(
        return_value=SyncResult(success=False, events_queued=10, transient=True)
    )

    # 8 outage cycles — well past the 5-retry drop ceiling.
    _run_cycles(engine, 8)

    assert engine.bf.send_events.called, "precondition: the queue was actually attempted"
    assert engine.queue.size() == 10, (
        "a transient server outage must NOT drop queued activity; the events "
        "should still be queued, waiting for the server to return"
    )
    assert engine.queue.failed_event_summary(max_retries=5)["count"] == 0


def test_definitive_rejection_still_drops_to_avoid_head_of_line_block():
    """A genuinely unstorable batch (server 4xx) must still accrue retries and
    become droppable — otherwise it blocks the queue head forever."""
    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)
    engine.queue.enqueue(_events(3))

    engine.bf.send_events = Mock(
        return_value=SyncResult(success=False, events_queued=3, transient=False)
    )
    _run_cycles(engine, 6)  # > max_retries=5

    assert engine.queue.size() == 0, (
        "a definitively-rejected (4xx) batch must still drop after max retries "
        "so it can't head-of-line-block the queue"
    )


def test_storable_stuck_head_is_held_not_dropped():
    """A stuck head that keeps failing transiently is HELD, never dropped — a long
    events-route degradation (server reachable, batches 5xx) must not lose real
    billable time, no matter how many cycles it lasts."""
    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)
    engine.queue.enqueue(_events(3))  # storable: recent ts + bucket_id
    engine.bf.send_events = Mock(
        return_value=SyncResult(success=False, events_queued=3, transient=True)
    )
    _run_cycles(engine, 40)  # sustained outage, well past any historical ceiling

    assert engine.queue.size() == 3, "transiently-failing activity must never be dropped"
    assert engine.queue.failed_event_summary(max_retries=5)["count"] == 0


def test_recent_poison_head_does_not_freeze_newer_events():
    """GAP #1: a persistent recent 5xx head must NOT freeze the events behind it.
    The queue drains AROUND the stuck head (dequeue offset); the poison is held
    (never lost), the good events flow. Pre-fix this failed — only the poison head
    was ever attempted and every newer event stayed frozen, heartbeat green."""
    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp, batch_size=1)  # one event per batch/head

    # Separate enqueue calls => strictly increasing created_at, so the poison is
    # unambiguously the oldest (dequeue head).
    engine.queue.enqueue([_evt("POISON")])
    engine.queue.enqueue([_evt("good-1")])
    engine.queue.enqueue([_evt("good-2")])

    attempted: list[str] = []

    def fake_send(events):
        eid = events[0]["id"]
        attempted.append(eid)
        if eid == "POISON":
            return SyncResult(success=False, events_queued=1, transient=True)
        return SyncResult(success=True, events_queued=0, events_synced=1)

    engine.bf.send_events = Mock(side_effect=fake_send)
    _run_cycles(engine, 3)

    # Good events were sent and removed; the poison is held (skipped), not lost.
    assert "good-1" in attempted and "good-2" in attempted, (
        f"newer events must drain around a stuck head; only {set(attempted)} tried"
    )
    remaining_ids = {
        ev.event_data["id"] for ev in engine.queue.dequeue(batch_size=100)
    }
    # The poison event is still queued (held for a future retry); the good events
    # have drained.
    assert engine.queue.size() == 1, "exactly the poison event should remain queued"
    assert "good-1" not in remaining_ids and "good-2" not in remaining_ids


def _evt(eid: str) -> dict:
    """A single storable event with a stable id (recent ts + bucket_id)."""
    return {
        "id": eid,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration": 60.0,
        "bucket_id": "aw-watcher-window_host",
        "data": {"app": "Terminal"},
    }


def test_full_transient_outage_holds_all_events():
    """A full outage (every batch fails transiently) holds ALL activity — never
    dropped — however long it lasts (the #99 guarantee)."""
    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)
    engine.queue.enqueue(_events(5))
    engine.bf.send_events = Mock(
        return_value=SyncResult(success=False, events_queued=5, transient=True)
    )
    _run_cycles(engine, 30)

    assert engine.queue.size() == 5, "a transient outage must not drop activity"
    assert engine.queue.failed_event_summary(max_retries=5)["count"] == 0
