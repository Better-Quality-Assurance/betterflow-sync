"""A single unstorable "poison" event must not drag its storable batch-mates to
the drop ceiling.

Origin: 2026-07 — production 1.5.95 emitted a steady stream of
    "Dropped N queued event(s) after max retries — likely real lost activity
     (buckets=aw-watcher-input,aw-watcher-window, ...; M other unstorable)"
The tell is the trailing "M other unstorable": the dropped batch mixed real
aw-watcher-input/window activity with a handful of unstorable events (no
bucket_id to route to, or already past the server's retention window).

Root cause: `dequeue()` returns events oldest-first, so an unstorable event sits
at the head of the queue and is batched together with storable events. The server
4xx-rejects the whole batch on account of the unstorable poison, and
`_process_queue`'s whole-batch retry bump (`increment_retry(event_ids)` on a
definitive 4xx) then increments EVERY event in the batch — the storable ones in
lockstep with the poison. After `max_retries` cycles the whole batch crosses the
ceiling and the storable events are dropped as "real lost activity", even though
the server would have accepted them in a clean batch.

Fix: evict genuinely-unstorable events (no bucket / past retention) from the
active queue BEFORE batching, so every batch is storable-only and the server
accepts it. `test_mixed_batch_does_not_drop_storable_activity` fails pre-fix
(the storable events are never delivered and are dropped as real loss).
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


def _storable(n: int) -> list[dict]:
    """n storable events: recent timestamp + a real bucket the server accepts."""
    now = datetime.now(timezone.utc).isoformat()
    return [
        {
            "id": f"good-{i}",
            "timestamp": now,
            "duration": 60.0,
            "bucket_id": "aw-watcher-window_host",
            "data": {"app": "Terminal"},
        }
        for i in range(n)
    ]


def _unstorable(n: int) -> list[dict]:
    """n events the server can NEVER store: bucketless (nowhere to route)."""
    now = datetime.now(timezone.utc).isoformat()
    return [
        {"id": f"poison-{i}", "timestamp": now, "duration": 60.0, "data": {}}
        for i in range(n)
    ]


class _StrictServer:
    """Models a server that validates the whole batch: if ANY event is
    unstorable (no bucket_id), it 4xx-rejects the entire batch (a definitive,
    non-transient rejection with no per-event verdict). A fully-storable batch is
    accepted and its ids recorded."""

    def __init__(self) -> None:
        self.delivered: set[str] = set()

    def __call__(self, events: list[dict]) -> SyncResult:
        if any(not e.get("bucket_id") for e in events):
            # Definitive 4xx on the whole batch — no accepted_ids, not transient.
            return SyncResult(
                success=False, events_queued=len(events), transient=False
            )
        for e in events:
            self.delivered.add(e["id"])
        return SyncResult(success=True, events_synced=len(events))


def _run_cycles(engine: SyncEngine, n: int) -> None:
    for _ in range(n):
        engine._queue_backoff_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        engine._process_queue(SyncStats())


def test_mixed_batch_does_not_drop_storable_activity():
    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)

    # Unstorable events enqueued FIRST so they sit at the oldest-first dequeue
    # head, exactly as in production; storable activity right behind them.
    engine.queue.enqueue(_unstorable(2))
    engine.queue.enqueue(_storable(9))
    assert engine.queue.size() == 11

    server = _StrictServer()
    engine.bf.send_events = Mock(side_effect=server)

    # 8 cycles — well past the max_retries=5 ceiling.
    _run_cycles(engine, 8)

    # The 9 storable events must reach the server, not be dropped as collateral.
    good_ids = {f"good-{i}" for i in range(9)}
    assert good_ids <= server.delivered, (
        "storable aw-watcher activity must be delivered, not dragged to the drop "
        f"ceiling by a co-batched unstorable poison event; delivered={server.delivered}"
    )

    # And none of it is classified as real lost activity.
    assert engine.queue.failed_event_summary(max_retries=5)["real_loss_count"] == 0, (
        "no storable event should reach the retry ceiling"
    )

    # The 2 unstorable poison events are preserved (dead-lettered), not silently
    # gone, and are out of the active queue.
    assert engine.queue.dead_letter_count() == 2
