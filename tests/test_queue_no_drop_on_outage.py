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
from unittest.mock import Mock

from src.config import Config
from src.sync.bf_client import SyncResult
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine, SyncStats


def _engine(tmp: Path) -> SyncEngine:
    return SyncEngine(
        aw=Mock(),
        bf=Mock(),
        queue=OfflineQueue(db_path=tmp / "q.db", max_size=10000),
        config=Config(),
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


def _unstorable_events(n: int) -> list[dict]:
    """n events the server would reject anyway: bucketless (nowhere to route)."""
    now = datetime.now(timezone.utc).isoformat()
    return [
        {"id": f"u-{i}", "timestamp": now, "duration": 60.0, "data": {}}
        for i in range(n)
    ]


def test_storable_stuck_head_is_held_not_dropped():
    """A stuck head that still holds RECENT, BUCKETED activity must be HELD, never
    dropped — a long events-route degradation (server reachable, batches 5xx,
    nothing draining) past the ceiling must not lose real billable time (audit
    round 4: the round-1 ceiling-drop could shed good events here)."""
    from src.sync.sync_engine import SyncEngine

    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)
    engine.queue.enqueue(_events(3))  # storable: recent ts + bucket_id
    engine.bf.send_events = Mock(
        return_value=SyncResult(success=False, events_queued=3, transient=True)
    )
    _run_cycles(engine, SyncEngine._STUCK_HEAD_CEILING + 10)  # well past the ceiling

    assert engine.queue.size() == 3, "storable activity must never be shed at the ceiling"


def test_unstorable_stuck_head_is_dropped_to_unblock():
    """A stuck head the server would reject anyway (bucketless / stale) IS shed at
    the ceiling so it stops blocking the queue head."""
    from src.sync.sync_engine import SyncEngine

    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)
    engine.queue.enqueue(_unstorable_events(3))
    engine.bf.send_events = Mock(
        return_value=SyncResult(success=False, events_queued=3, transient=True)
    )
    _run_cycles(engine, SyncEngine._STUCK_HEAD_CEILING + 8)

    assert engine.queue.size() == 0, "an unstorable stuck head must be shed to unblock"


def test_transient_outage_within_ceiling_still_holds_events():
    """The unblock escape must NOT fire for an outage shorter than the ceiling —
    those events are held, not dropped (the #99 guarantee still holds)."""
    from src.sync.sync_engine import SyncEngine

    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)
    engine.queue.enqueue(_events(5))
    engine.bf.send_events = Mock(
        return_value=SyncResult(success=False, events_queued=5, transient=True)
    )
    _run_cycles(engine, SyncEngine._STUCK_HEAD_CEILING - 1)  # below the ceiling

    assert engine.queue.size() == 5, "a sub-ceiling outage must not drop activity"
    assert engine.queue.failed_event_summary(max_retries=5)["count"] == 0
