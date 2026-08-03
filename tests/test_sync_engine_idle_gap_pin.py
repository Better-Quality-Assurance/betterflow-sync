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
    """A weekend-sized gap must be crossed within ONE _fetch_bucket_events call.

    Incident (PiratesMac / device 14, 2026-08-03): the gap-jump fix above works
    but advanced only one _BACKLOG_WINDOW (2h) per 60s sync cycle, so a Monday
    morning after a ~24h weekend gap spent ~30 minutes uploading nothing but the
    afk heartbeat — empty dashboard graph, "--:--" Arrival, and an
    active_seconds=0 "Unknown" aggregate until the walker caught up. Consecutive
    EMPTY windows are provably safe to skip, so they must all be walked inside
    the same cycle."""
    tmp = Path(tempfile.mkdtemp())
    now = datetime.now(timezone.utc)
    tail = now - timedelta(hours=49)        # Fri evening -> Mon morning gap
    resume = now - timedelta(minutes=30)    # Monday work has begun

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

        assert [e for e in batch if e.timestamp >= resume], (
            "one cycle crossed only one empty window — post-gap events would "
            "wait ~1 min per 2h of gap before first upload"
        )
    finally:
        engine.queue.close()


def test_empty_window_walk_is_bounded_per_cycle():
    """The in-cycle walk must stop at _BACKLOG_MAX_EMPTY_WINDOWS_PER_CYCLE so a
    months-parked device cannot stall a sync cycle probing its whole history at
    once. Progress persists via the checkpoint, so the next cycle resumes."""
    tmp = Path(tempfile.mkdtemp())
    now = datetime.now(timezone.utc)
    tail = now - timedelta(days=30)  # long-parked device, nothing since

    aw = _aw_serving([AWEvent(id=1, timestamp=tail, duration=10.0, data={"app": "Terminal"})])
    engine = _engine(tmp, aw)
    try:
        engine.queue.set_checkpoint(BUCKET, tail)

        batch, _lb = engine._fetch_bucket_events(BUCKET, SyncStats())
        cp = engine.queue.get_checkpoint(BUCKET)

        assert batch == []
        # Loop ran: the cap's worth of windows was crossed in one cycle
        # (48 windows x ~1h58m net advance each ~= 94h).
        assert cp >= tail + timedelta(hours=90), (
            f"walk did not run inside the cycle (cp={cp.isoformat()})"
        )
        # Cap held: the 30-day span was NOT consumed in one cycle.
        assert cp < now - timedelta(days=20), (
            f"walk was unbounded within a single cycle (cp={cp.isoformat()})"
        )
    finally:
        engine.queue.close()


def test_walk_stops_at_mid_gap_event_cluster():
    """The walk must STOP at the first non-empty window, not blind-skip to the
    newest activity. A Saturday work blip inside a Fri->Mon gap is real billable
    time; skipping past it persists the checkpoint beyond it (monotonic), and
    _reconcile_backlog only replays local-midnight->now — the blip would be
    unrecoverable."""
    tmp = Path(tempfile.mkdtemp())
    now = datetime.now(timezone.utc)
    tail = now - timedelta(hours=49)      # Friday evening
    blip = now - timedelta(hours=35)      # Saturday afternoon blip
    resume = now - timedelta(minutes=30)  # Monday morning

    events = [AWEvent(id=1, timestamp=tail, duration=10.0, data={"app": "Terminal"})]
    events += [
        AWEvent(id=50 + i, timestamp=blip + timedelta(seconds=30 * i),
                duration=10.0, data={"app": "Terminal"})
        for i in range(3)
    ]
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

        got = {e.id for e in batch}
        assert {50, 51, 52} <= got, (
            f"mid-gap Saturday events were skipped (batch ids: {sorted(got)})"
        )
        assert not {100, 101, 102} & got, (
            "walk overshot the first non-empty window to Monday's events"
        )
        assert engine.queue.get_checkpoint(BUCKET) < blip, (
            "checkpoint persisted past an unfetched mid-gap cluster"
        )
    finally:
        engine.queue.close()


def test_error_mid_walk_keeps_per_skip_progress():
    """Each skipped window persists BEFORE the next probe, so a probe that
    errors at window N leaves the checkpoint N windows forward. Persisting only
    at loop exit would re-walk (and re-fail) the same span every cycle —
    reincarnating the stall this fix exists to cure."""
    from src.sync.aw_client import AWClientError

    tmp = Path(tempfile.mkdtemp())
    now = datetime.now(timezone.utc)
    tail = now - timedelta(days=30)

    calls = {"n": 0}

    def get_events(bucket_id, start=None, end=None, limit=1000):
        calls["n"] += 1
        if calls["n"] >= 4:  # initial probe + 3 loop probes, then the AW dies
            raise AWClientError("aw went away mid-walk")
        e = AWEvent(id=1, timestamp=tail, duration=10.0, data={"app": "Terminal"})
        return [e] if (start is None or e.timestamp >= start) and (end is None or e.timestamp < end) else []

    aw = Mock()
    aw.get_events.side_effect = get_events
    engine = _engine(tmp, aw)
    try:
        engine.queue.set_checkpoint(BUCKET, tail)

        try:
            engine._fetch_bucket_events(BUCKET, SyncStats())
        except AWClientError:
            pass  # propagates to the per-bucket handler in _sync_window_buckets

        cp = engine.queue.get_checkpoint(BUCKET)
        # 3 completed skips of ~1h58m each were persisted before the failing probe.
        assert cp >= tail + timedelta(hours=5), (
            f"progress before the failing probe was lost (cp={cp.isoformat()})"
        )
        assert cp <= tail + timedelta(hours=8), f"overshot past the failure point (cp={cp.isoformat()})"
    finally:
        engine.queue.close()


def test_walk_wall_clock_budget_bounds_slow_probes():
    """The iteration cap bounds probes, not time: a degraded local AW server can
    take seconds per probe without raising. The walk must also stop at
    _BACKLOG_WALK_BUDGET_SECONDS so one bucket cannot eat the sync watchdog's
    150s deadline."""
    from unittest.mock import patch

    tmp = Path(tempfile.mkdtemp())
    now = datetime.now(timezone.utc)
    tail = now - timedelta(days=30)

    aw = _aw_serving([AWEvent(id=1, timestamp=tail, duration=10.0, data={"app": "Terminal"})])
    engine = _engine(tmp, aw)
    try:
        engine.queue.set_checkpoint(BUCKET, tail)

        # Each monotonic() read advances 3 "seconds": deadline = t+5, so the
        # loop's second condition check reads past the deadline after ~2 skips.
        t = {"v": 1000.0}

        def fake_monotonic():
            t["v"] += 3.0
            return t["v"]

        with patch("src.sync.sync_engine.time.monotonic", side_effect=fake_monotonic):
            batch, _lb = engine._fetch_bucket_events(BUCKET, SyncStats())

        cp = engine.queue.get_checkpoint(BUCKET)
        assert batch == []
        assert cp > tail, "walk never ran"
        assert cp <= tail + timedelta(hours=10), (
            f"slow-probe walk was not time-bounded (cp={cp.isoformat()})"
        )
    finally:
        engine.queue.close()
