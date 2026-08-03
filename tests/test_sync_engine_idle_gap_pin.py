"""Regression: an idle gap longer than _BACKLOG_WINDOW must not pin the cursor.

Incident (PiratesMac / device 14, 2026-06-23): the user stopped work the prior
evening, so the window/input forward checkpoint sat at that evening's last event.
Overnight the gap exceeded _BACKLOG_WINDOW (2h). The next morning, every sync
cycle fetched `[checkpoint-2min, checkpoint+2h]` — a slice entirely in the past
that contained only the pre-gap tail event (re-surfaced by the 2-minute
lookback). Because the slice wasn't *literally* empty, the "empty leading window"
gap-jump never fired, the checkpoint never advanced, and that day's events were
never read by the sync engine — captured locally, never uploaded.

The fix gates the gap-jump on events strictly AFTER the checkpoint, so the
lookback's re-surfaced tail can't keep the cursor pinned.
"""
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from src.config import Config
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.aw_client import AWEvent
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine, SyncStats

BUCKET = "aw-watcher-window_test"


def _engine(tmp, aw):
    return SyncEngine(
        aw=aw,
        bf=Mock(),
        queue=OfflineQueue(db_path=tmp / "q.db", max_size=100000),
        config=Config(),
        activity_analyzer=Mock(spec=ActivityAnalyzer),
        time_tracker=Mock(spec=DailyTimeTracker),
    )


def _aw_serving(events):
    """Mock AW whose get_events honours the [start, end) window like the real one."""
    aw = Mock()

    def get_events(bucket_id, start=None, end=None, limit=1000):
        return [
            e for e in events
            if (start is None or e.timestamp >= start)
            and (end is None or e.timestamp < end)
        ]

    aw.get_events.side_effect = get_events
    return aw


def test_gap_jump_fires_when_only_pregap_tail_in_window():
    """Single cycle: checkpoint pinned at an idle-gap tail, the only in-window
    event is that tail (≤ checkpoint). The cursor must advance, not pin."""
    tmp = Path(tempfile.mkdtemp())
    now = datetime.now(timezone.utc)
    tail = now - timedelta(hours=13)  # last event before a long overnight gap

    aw = _aw_serving([AWEvent(id=1, timestamp=tail, duration=10.0, data={"app": "Terminal"})])
    engine = _engine(tmp, aw)
    try:
        engine.queue.set_checkpoint(BUCKET, tail)

        engine._fetch_bucket_events(BUCKET, SyncStats())

        cp = engine.queue.get_checkpoint(BUCKET)
        # With the bug the slice isn't empty (tail re-surfaced) so the gap-jump
        # is skipped and cp stays == tail. The fix advances past the gap.
        assert cp > tail, f"checkpoint pinned at the idle-gap tail ({cp.isoformat()})"
    finally:
        engine.queue.close()


def test_post_gap_events_are_eventually_fetched():
    """End-to-end: after a >2h overnight gap, the morning's events must actually
    get read. With the bug the cursor pins forever and they never are."""
    tmp = Path(tempfile.mkdtemp())
    now = datetime.now(timezone.utc)
    tail = now - timedelta(hours=13)        # pre-gap tail
    resume = now - timedelta(minutes=30)    # work resumes after the gap

    events = [AWEvent(id=1, timestamp=tail, duration=10.0, data={"app": "Terminal"})]
    events += [
        AWEvent(id=100 + i, timestamp=resume + timedelta(seconds=30 * i),
                duration=10.0, data={"app": "Terminal"})
        for i in range(3)
    ]
    aw = _aw_serving(events)
    engine = _engine(tmp, aw)
    try:
        engine.queue.set_checkpoint(BUCKET, tail)

        fetched_post_gap = []
        for _ in range(30):  # bounded; the walk-forward needs only ~7 cycles
            batch, _lb = engine._fetch_bucket_events(BUCKET, SyncStats())
            if batch:
                # Simulate the downstream successful-send advance.
                engine.queue.set_checkpoint_forward(BUCKET, max(e.timestamp for e in batch))
                fetched_post_gap += [e for e in batch if e.timestamp >= resume]
                if fetched_post_gap:
                    break

        assert fetched_post_gap, (
            "post-gap events were never fetched — the cursor pinned at the "
            "idle-gap tail and never reached the morning's events"
        )
        assert engine.queue.get_checkpoint(BUCKET) >= resume
    finally:
        engine.queue.close()


def test_multi_day_gap_crossed_in_single_cycle():
    """A weekend-sized EMPTY gap must be crossed inside ONE fetch call.

    Advancing one _BACKLOG_WINDOW (2h) per 60s sync cycle crosses a 62h weekend
    in ~31 cycles = ~31 minutes, during which the morning's window/input events
    sit captured-but-unsent and the server's 15-min window-ingest-stall alert
    fires (device 14, 2026-08-03 — every Monday, every device with a weekend
    gap). The empty-window walk must happen inside the cycle, not across cycles.
    """
    tmp = Path(tempfile.mkdtemp())
    now = datetime.now(timezone.utc)
    tail = now - timedelta(hours=62)        # Friday-evening pre-weekend tail
    resume = now - timedelta(minutes=10)    # Monday-morning work

    events = [AWEvent(id=1, timestamp=tail, duration=10.0, data={"app": "Terminal"})]
    events += [
        AWEvent(id=100 + i, timestamp=resume + timedelta(seconds=30 * i),
                duration=10.0, data={"app": "Terminal"})
        for i in range(3)
    ]
    aw = _aw_serving(events)
    engine = _engine(tmp, aw)
    try:
        engine.queue.set_checkpoint(BUCKET, tail)

        batch, _lb = engine._fetch_bucket_events(BUCKET, SyncStats())

        post_gap = [e for e in batch if e.timestamp >= resume]
        assert post_gap, (
            "one fetch cycle returned no post-gap events — the empty weekend "
            "span is being crossed one 2h window per cycle, stalling upload "
            "~31 min and tripping the server's window-ingest-stall alert"
        )
    finally:
        engine.queue.close()


def test_empty_gap_walk_is_bounded_per_cycle():
    """The in-cycle walk must terminate after a bounded number of empty windows
    (each is a local HTTP call), resuming from the advanced checkpoint next
    cycle — an ancient checkpoint must not turn one cycle into thousands of
    fetches."""
    tmp = Path(tempfile.mkdtemp())
    now = datetime.now(timezone.utc)
    tail = now - timedelta(days=400)

    aw = _aw_serving([])  # nothing to find, ever
    engine = _engine(tmp, aw)
    try:
        engine.queue.set_checkpoint(BUCKET, tail)

        batch, _lb = engine._fetch_bucket_events(BUCKET, SyncStats())

        assert batch == []
        cp = engine.queue.get_checkpoint(BUCKET)
        max_walk = (
            engine._EMPTY_GAP_MAX_WINDOWS_PER_CYCLE * engine._BACKLOG_WINDOW
            + engine._BACKLOG_WINDOW  # the pre-loop first window
        )
        assert cp > tail, "cursor did not advance at all"
        assert cp <= tail + max_walk, (
            f"cursor advanced {(cp - tail)} in one cycle — the per-cycle bound "
            "on empty-window fetches is not being enforced"
        )
        # And the number of AW round-trips this cycle is the bound, not the gap size.
        assert aw.get_events.call_count <= engine._EMPTY_GAP_MAX_WINDOWS_PER_CYCLE + 1
    finally:
        engine.queue.close()
