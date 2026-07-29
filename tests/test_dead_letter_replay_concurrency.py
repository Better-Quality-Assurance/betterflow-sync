"""The dead-letter replay's "a row can never be resurrected twice" must be true.

``requeue_storable_dead_letter`` documents MOVE semantics — INSERT into
``queued_events`` and DELETE from ``dead_letter_events`` in ONE transaction — and
concludes from that "so a row can never be resurrected twice (no double-send)".
The INSERT and DELETE are indeed atomic. The SELECT that chooses the rows was
not part of that transaction: pysqlite opens the implicit transaction at the
first DML statement, so the read ran in autocommit. With per-thread connections
two threads could both SELECT the same ids and both INSERT a copy.

That is reachable in production, not theoretical: ``main._acquire_sync_slot``
deliberately abandons a wedged cycle after 420s and starts a fresh ``_do_sync``
while the zombie thread is still inside ``_process_queue``.

Tracked time is billed. A duplicated event is a double-billed one.
"""

import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import src.sync.queue as queue_mod
from src.sync.queue import OfflineQueue

SLOW_THREAD = "replay-A"


def test_two_concurrent_replays_resurrect_a_row_only_once():
    """Two overlapping replays over one dead-lettered row must produce exactly
    ONE live copy. Pre-fix the loser's SELECT ran outside any write transaction
    and saw the row the winner was mid-move on, so both INSERTed it.

    The interleave is forced deterministically: the classifier is wrapped so the
    first thread stalls between its SELECT and its INSERT, which is exactly the
    window the missing transaction left open.
    """
    tmp = Path(tempfile.mkdtemp())
    queue = OfflineQueue(db_path=tmp / "q.db", max_size=1000)
    now = datetime.now(timezone.utc)
    try:
        queue.enqueue([
            {"id": "billable-1", "bucket_id": "aw-watcher-afk_h",
             "timestamp": (now - timedelta(hours=1)).isoformat(),
             "duration": 60, "data": {"status": "not-afk"}},
        ])
        queued = queue.dequeue(batch_size=10)
        for _ in range(5):
            queue.increment_retry([q.id for q in queued])
        # dropped_at well past the cooldown so both calls consider the row.
        queue.remove_failed(max_retries=5, now=now - timedelta(hours=2))
        assert queue.dead_letter_count() == 1
        assert queue.is_empty()

        real_storable = queue_mod.is_event_storable
        in_the_window = threading.Event()

        def stalling_storable(ev, *, stale_cutoff):
            # Only the first thread stalls; the second must be free to race in.
            if threading.current_thread().name == SLOW_THREAD:
                in_the_window.set()
                time.sleep(0.6)
            return real_storable(ev, stale_cutoff=stale_cutoff)

        results: dict[str, dict] = {}

        def run(tag: str) -> None:
            results[tag] = queue.requeue_storable_dead_letter(now=now)

        with patch.object(queue_mod, "is_event_storable", stalling_storable):
            a = threading.Thread(target=run, args=("a",), name=SLOW_THREAD)
            a.start()
            assert in_the_window.wait(timeout=5), "thread A never reached the window"
            b = threading.Thread(target=run, args=("b",), name="replay-B")
            b.start()
            a.join(timeout=15)
            b.join(timeout=15)
        assert not a.is_alive() and not b.is_alive()

        # The invariant, stated as the docstring states it: ONE resurrection.
        assert queue.size() == 1, (
            "the row was resurrected twice — it will be sent twice, and tracked "
            "time is billed"
        )
        assert results["a"]["requeued"] + results["b"]["requeued"] == 1
        assert queue.dead_letter_count() == 0
        back = queue.dequeue(batch_size=10)
        assert [q.event_data["id"] for q in back] == ["billable-1"]
    finally:
        queue.close()


def test_two_concurrent_remove_failed_dead_letter_a_row_only_once():
    """The same shape one step upstream — and it feeds the replay, so it is the
    same double-send by a longer road.

    ``remove_failed`` documents "The INSERT and the matching DELETE run inside
    the SAME ``_cursor()`` transaction". They do. The SELECT that picks the
    over-ceiling rows did not, so two threads could both pick the same row and
    write TWO dead_letter_events rows for one event. The replay then resurrects
    both, and the same billable span is delivered twice.
    """
    tmp = Path(tempfile.mkdtemp())
    queue = OfflineQueue(db_path=tmp / "q.db", max_size=1000)
    now = datetime.now(timezone.utc)
    try:
        queue.enqueue([
            {"id": "billable-2", "bucket_id": "aw-watcher-afk_h",
             "timestamp": (now - timedelta(hours=1)).isoformat(),
             "duration": 60, "data": {"status": "not-afk"}},
        ])
        queued = queue.dequeue(batch_size=10)
        for _ in range(5):
            queue.increment_retry([q.id for q in queued])

        real_loads = queue_mod.json.loads
        in_the_window = threading.Event()

        def stalling_loads(raw, *a, **kw):
            if threading.current_thread().name == SLOW_THREAD:
                in_the_window.set()
                time.sleep(0.6)
            return real_loads(raw, *a, **kw)

        def run() -> None:
            queue.remove_failed(max_retries=5, now=now)

        with patch.object(queue_mod.json, "loads", stalling_loads):
            a = threading.Thread(target=run, name=SLOW_THREAD)
            a.start()
            assert in_the_window.wait(timeout=5), "thread A never reached the window"
            b = threading.Thread(target=run, name="dl-B")
            b.start()
            a.join(timeout=15)
            b.join(timeout=15)
        assert not a.is_alive() and not b.is_alive()

        assert queue.dead_letter_count() == 1, (
            "one event produced two dead-letter rows; the replay will resurrect "
            "both and the same billable span is sent twice"
        )
        assert queue.is_empty()
    finally:
        queue.close()
