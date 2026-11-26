"""#300 item 2: the tray's local DailyTimeTracker must not count the same wall
time twice when the in-process window stream goes blind and the external
bf-window-tracker bucket is replayed over the span the in-process stream had
already credited (reviewer repro: 55 s became 110 s).

Drives the real transform path (``_transform_window_event_with_timeout``) with
a recording tracker, and asserts on the SECONDS credited, not on call counts.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from src.config import Config
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.aw_client import AWEvent, BUCKET_TYPE_WINDOW
from src.sync.sync_engine import SyncEngine, _SyncCycleContext
from src.sync.window_source import WindowSource

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)
EXTERNAL = "aw-watcher-window_host"


class _Tracker:
    def __init__(self):
        self.seconds = 0.0

    def add_active_time(self, seconds, event_date):
        self.seconds += seconds


def _engine():
    cfg = Config()
    cfg.sync.in_process_window = True
    tracker = _Tracker()
    eng = SyncEngine(aw=Mock(), bf=Mock(), queue=Mock(), config=cfg,
                     activity_analyzer=Mock(spec=ActivityAnalyzer),
                     time_tracker=tracker)
    eng.window_source = WindowSource(hostname="host", foreground_getter=lambda: ("Code", "a"))
    eng._persist_counted_time = Mock()
    return eng, tracker


def _ev(eid, start, seconds):
    return AWEvent(id=eid, timestamp=start, duration=seconds,
                   data={"app": "Code", "title": "a"})


def _credit(eng, bucket, ev):
    eng._transform_window_event_with_timeout(
        ev, bucket, BUCKET_TYPE_WINDOW, _SyncCycleContext()
    )


def test_blind_fallback_replay_does_not_double_count_the_same_wall_time():
    eng, tracker = _engine()
    inproc = eng.window_source.bucket_id
    _credit(eng, inproc, _ev("win-inproc_host_1", T0, 55))
    assert tracker.seconds == 55
    # Probe goes blind; the external bucket replays the SAME 55 s under its own id.
    _credit(eng, EXTERNAL, _ev(7, T0, 55))
    assert tracker.seconds == 55, "same wall time credited twice"


def test_external_event_only_credits_the_part_not_already_counted():
    eng, tracker = _engine()
    _credit(eng, eng.window_source.bucket_id, _ev("win-inproc_host_1", T0, 55))
    # External event spans the 55 s already counted plus 30 s of new time.
    _credit(eng, EXTERNAL, _ev(7, T0, 85))
    assert tracker.seconds == 85


def test_disjoint_external_time_is_still_counted():
    eng, tracker = _engine()
    _credit(eng, eng.window_source.bucket_id, _ev("win-inproc_host_1", T0, 55))
    _credit(eng, EXTERNAL, _ev(8, T0 + timedelta(seconds=120), 40))
    assert tracker.seconds == 95


def test_external_only_counting_is_unchanged():
    """Control: two non-overlapping external events, flag-off style, count in full."""
    eng, tracker = _engine()
    _credit(eng, EXTERNAL, _ev(1, T0, 30))
    _credit(eng, EXTERNAL, _ev(2, T0 + timedelta(seconds=30), 30))
    assert tracker.seconds == 60


def test_inproc_recovery_after_external_does_not_double_count():
    eng, tracker = _engine()
    _credit(eng, EXTERNAL, _ev(7, T0, 55))
    _credit(eng, eng.window_source.bucket_id, _ev("win-inproc_host_1", T0, 55))
    assert tracker.seconds == 55


def test_events_growing_after_cross_source_overlap_count_every_wall_second():
    """Both sources overlap, then both events grow: 85 s of wall time must read
    85, not 55 (a clipped per-event cumulative shrinks and loses the growth)."""
    eng, tracker = _engine()
    inproc = eng.window_source.bucket_id
    _credit(eng, EXTERNAL, _ev(7, T0, 55))
    _credit(eng, inproc, _ev("win-inproc_host_1", T0, 55))
    _credit(eng, EXTERNAL, _ev(7, T0, 85))
    _credit(eng, inproc, _ev("win-inproc_host_1", T0, 85))
    assert tracker.seconds == 85


class _CountedTimeQueue:
    """The counted_time table's contract, in memory (day scoping not needed)."""

    def __init__(self):
        self.rows = {}

    def get_counted_times(self, day):
        return dict(self.rows)

    def set_counted_time(self, bucket_id, event_id, seconds, day):
        self.rows[(bucket_id, event_id)] = seconds

    def prune_counted_time(self, before_day):
        return 0


def _engine_on(store):
    cfg = Config()
    cfg.sync.in_process_window = True
    tracker = _Tracker()
    queue = Mock()
    queue.get_counted_times = store.get_counted_times
    queue.set_counted_time = store.set_counted_time
    queue.prune_counted_time = store.prune_counted_time
    eng = SyncEngine(aw=Mock(), bf=Mock(), queue=queue, config=cfg,
                     activity_analyzer=Mock(spec=ActivityAnalyzer),
                     time_tracker=tracker)
    eng.window_source = WindowSource(hostname="host", foreground_getter=lambda: ("Code", "a"))
    eng._load_counted_time_cache()
    return eng, tracker


def test_restart_replay_in_other_bucket_order_does_not_double_count():
    store = _CountedTimeQueue()
    eng, tracker = _engine_on(store)
    _credit(eng, EXTERNAL, _ev(7, T0, 55))
    _credit(eng, eng.window_source.bucket_id, _ev("win-inproc_host_1", T0, 55))
    assert tracker.seconds == 55
    # Restart: the reconcile replays the in-process bucket first.
    eng2, tracker2 = _engine_on(store)
    _credit(eng2, eng2.window_source.bucket_id, _ev("win-inproc_host_1", T0, 55))
    _credit(eng2, EXTERNAL, _ev(7, T0, 55))
    assert tracker2.seconds == 0, "restart re-credited already-counted wall time"


def test_restart_then_growth_only_credits_new_wall_time():
    store = _CountedTimeQueue()
    eng, tracker = _engine_on(store)
    _credit(eng, eng.window_source.bucket_id, _ev("win-inproc_host_1", T0, 55))
    _credit(eng, EXTERNAL, _ev(7, T0, 85))
    assert tracker.seconds == 85
    eng2, tracker2 = _engine_on(store)
    _credit(eng2, EXTERNAL, _ev(7, T0, 90))
    assert tracker2.seconds == 5


def test_flag_off_persists_only_the_per_event_row():
    """Default installs must not pay for the ledger: one counted_time write per
    credit, no `__wall_ledger__:` rows."""
    eng, tracker = _engine()
    eng.config.sync.in_process_window = False
    _credit(eng, EXTERNAL, _ev(1, T0, 30))
    assert tracker.seconds == 30
    buckets = [c.args[0] for c in eng._persist_counted_time.call_args_list]
    assert buckets == [EXTERNAL]


def test_web_bucket_is_clipped_against_the_inprocess_window_stream():
    """Pinned on purpose: browser (web) time over wall time the in-process window
    stream already counted is the same minute, so it is credited once."""
    eng, tracker = _engine()
    _credit(eng, eng.window_source.bucket_id, _ev("win-inproc_host_1", T0, 55))
    _credit(eng, "aw-watcher-web-chrome_host", _ev(9, T0, 55))
    assert tracker.seconds == 55
