"""A >24h AFK event must be split on EVERY path that queues it, not just live sync.

#269 split an over-long ActivityWatch event into <=24h chunks, but only inside
``_transform_and_checkpoint`` (the live sync path). Two other paths transform AW
events and enqueue them directly, and both still enqueued the event WHOLE:

- ``_flush_pre_window_tail`` — runs on every pause (system sleep, and since #271
  every screen lock) and fetches each bucket from its CHECKPOINT to now. For an
  ongoing AFK event the checkpoint is that event's own start (the checkpoint is
  the newest event's timestamp), so after a machine sits idle for a day the
  fetch returns the whole >24h event.
- ``_reconcile_backlog`` — the work-day replay.

The whole event is then evicted by ``evict_unstorable`` as over-long and reported
as real loss:

    Dropped 1 queued event(s) after max retries — the server rejected them;
    held in dead-letter for replay, not discarded (buckets=aw-watcher-afk,
    oldest ~2151m old, spans ~0m)

(ops board 2026-10-09, device 10, agent 1.5.138 — which already carries #269).
It also bumps the device's ``dead_letter_count``, the fleet's only loss signal.

The assertion is on the CONSUMER: what ``evict_unstorable`` does with the queue
afterwards, using the same storability rule the server's 86400s cap mirrors.
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.config import Config
from src.sync.aw_client import BUCKET_TYPE_AFK, AWEvent
from src.sync.queue import MAX_EVENT_DURATION_SECONDS, OfflineQueue
from src.sync.sync_engine import SyncEngine

BUCKET_ID = "aw-watcher-afk_test-host"


class _Bucket:
    def __init__(self, bid, btype):
        self.id = bid
        self.type = btype


def _engine(aw_events) -> tuple[SyncEngine, OfflineQueue]:
    config = Config()
    config.working_hours.known = True  # 24/7: no working-hours gate
    queue = OfflineQueue(db_path=Path(tempfile.mkdtemp()) / "q.db", max_size=10000)
    aw = Mock()
    # Returned by the FIRST fetch only: the reconcile walks the day in 2h
    # windows and a static return value would re-deliver the event per window.
    calls = iter([list(aw_events)])
    aw.get_events.side_effect = lambda *a, **k: next(calls, [])
    engine = SyncEngine(aw=aw, bf=Mock(), queue=queue, config=config,
                        time_tracker=Mock())
    engine.error_reporter = Mock()
    return engine, queue


def _queued(queue: OfflineQueue) -> list[dict]:
    return [q.event_data for q in queue.dequeue(batch_size=1000)]


def _afk(start: datetime, hours: float, event_id: int = 4242) -> AWEvent:
    return AWEvent(id=event_id, timestamp=start, duration=hours * 3600,
                   data={"status": "afk"})


def _run_tail_flush(engine, now):
    engine._flush_pre_window_tail([_Bucket(BUCKET_ID, BUCKET_TYPE_AFK)], now, "pause")


def _run_reconcile(engine, now):
    engine._reconcile_backlog([_Bucket(BUCKET_ID, BUCKET_TYPE_AFK)])


@pytest.mark.parametrize("path", [_run_tail_flush, _run_reconcile],
                         ids=["pause-tail-flush", "backlog-reconcile"])
def test_over_24h_afk_event_is_queued_in_storable_chunks(path):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(hours=36)
    engine, queue = _engine([_afk(start, 35.8)])
    # The tail flush reads from the bucket's checkpoint; for an ongoing AFK
    # event that is the event's own start.
    queue.set_checkpoint(BUCKET_ID, start)

    path(engine, now)

    queued = _queued(queue)
    assert queued, "nothing was queued"
    assert all(0 < e["duration"] <= MAX_EVENT_DURATION_SECONDS for e in queued), (
        [e["duration"] for e in queued]
    )
    # Same chunk ids as the live path (#269), so the server upserts in place
    # instead of storing a second copy.
    assert sorted(str(e["id"]) for e in queued) == ["4242", "4242_1"]
    assert round(sum(e["duration"] for e in queued)) == round(35.8 * 3600)

    evicted = queue.evict_unstorable()
    assert evicted["count"] == 0, evicted
    assert evicted["real_loss_count"] == 0


@pytest.mark.parametrize("path", [_run_tail_flush, _run_reconcile],
                         ids=["pause-tail-flush", "backlog-reconcile"])
def test_afk_event_within_24h_is_queued_whole_with_its_aw_id(path):
    """Control: an ordinary event is untouched (passes before and after the fix)."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(hours=3)
    engine, queue = _engine([_afk(start, 2)])
    queue.set_checkpoint(BUCKET_ID, start)

    path(engine, now)

    queued = _queued(queue)
    assert [str(e["id"]) for e in queued] == ["4242"]
    assert queued[0]["duration"] == 2 * 3600
