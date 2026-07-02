from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from src.config import Config
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.sync_engine import SyncEngine, SyncStats, _is_window_like
from src.sync.window_source import WindowSource

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


def _engine(in_process_window: bool):
    cfg = Config()
    cfg.sync.in_process_window = in_process_window
    return SyncEngine(aw=Mock(), bf=Mock(), queue=Mock(), config=cfg,
                      activity_analyzer=Mock(spec=ActivityAnalyzer),
                      time_tracker=Mock(spec=DailyTimeTracker))


class _FakeSource(WindowSource):
    """A WindowSource with a scripted-usable probe (available() forced on)."""

    def __init__(self, samples):
        super().__init__(hostname="host", foreground_getter=lambda: ("Code", "a"))
        with self._lock:
            for st, app, title in samples:
                self._samples.append((st, app, title))


def test_flag_off_is_no_op():
    eng = _engine(False)
    eng.window_source = _FakeSource([(T0, "Code", "a")])
    assert eng._should_use_inproc_window() is False
    assert eng._should_skip_external_window() is False
    # Build is inert even with samples present.
    assert eng._build_inproc_window(T0 + timedelta(seconds=60)) == []


def test_active_when_flag_on_and_available():
    eng = _engine(True)
    eng.window_source = _FakeSource([(T0, "Code", "a")])
    assert eng._should_use_inproc_window() is True
    assert eng._should_skip_external_window() is True


def test_inactive_when_no_source_wired():
    eng = _engine(True)
    assert eng._should_skip_external_window() is False


def test_inactive_when_probe_unavailable():
    eng = _engine(True)
    # No frontmost probe on this platform.
    eng.window_source = WindowSource(hostname="host", foreground_getter=None)
    assert eng._should_skip_external_window() is False


def test_inproc_window_events_built_for_uploaded_range():
    eng = _engine(True)
    eng.window_source = _FakeSource([
        (T0, "Code", "a"),
        (T0 + timedelta(seconds=30), "Code", "a"),
    ])
    eng._build_inproc_window(T0)  # first call seeds the checkpoint at T0, returns []
    events = eng._build_inproc_window(T0 + timedelta(seconds=30))
    assert events
    assert all(e["bucket_id"] == "bf-window-inproc_host" for e in events)
    assert events[0]["data"]["app"] == "Code"


def test_checkpoint_committed_only_after_successful_send():
    eng = _engine(True)
    eng.window_source = _FakeSource([
        (T0, "Code", "a"),
        (T0 + timedelta(seconds=30), "Code", "a"),
    ])
    eng._build_inproc_window(T0)  # seed
    eng._build_inproc_window(T0 + timedelta(seconds=30))  # sets pending
    pending = eng._window_inproc_pending
    assert pending is not None

    # Queued send -> checkpoint held, pending cleared so next cycle rebuilds.
    stats = SyncStats()
    stats.queued_bucket_ids.add("bf-window-inproc_host")
    eng._commit_inproc_window_checkpoint(stats)
    assert eng._window_inproc_checkpoint == T0  # unchanged
    assert eng._window_inproc_pending is None


def test_checkpoint_advances_on_confirmed_send():
    eng = _engine(True)
    eng.window_source = _FakeSource([
        (T0, "Code", "a"),
        (T0 + timedelta(seconds=30), "Code", "a"),
    ])
    eng._build_inproc_window(T0)
    eng._build_inproc_window(T0 + timedelta(seconds=30))
    stats = SyncStats()  # no queued buckets -> confirmed send
    eng._commit_inproc_window_checkpoint(stats)
    assert eng._window_inproc_checkpoint == T0 + timedelta(seconds=30)


def _bucket(bucket_id, btype):
    b = Mock()
    b.id = bucket_id
    b.type = btype
    return b


def test_external_window_buckets_skipped_when_active():
    from src.sync.aw_client import BUCKET_TYPE_WINDOW as WIN
    eng = _engine(True)
    eng.window_source = _FakeSource([(T0, "Code", "a")])
    window_buckets = [_bucket("aw-watcher-window_host", WIN)]
    skip = eng._should_skip_external_window()
    to_sync = [b for b in window_buckets if not _is_window_like(b.type)] if skip else window_buckets
    assert to_sync == []  # external window bucket dropped, no double-count


def test_external_window_buckets_kept_when_flag_off():
    from src.sync.aw_client import BUCKET_TYPE_WINDOW as WIN
    eng = _engine(False)
    eng.window_source = _FakeSource([(T0, "Code", "a")])
    window_buckets = [_bucket("aw-watcher-window_host", WIN)]
    skip = eng._should_skip_external_window()
    to_sync = [b for b in window_buckets if not _is_window_like(b.type)] if skip else window_buckets
    assert to_sync == window_buckets  # unchanged when dormant


def test_record_window_sample_if_active_gates_on_flag():
    """The public sampler (used by both the per-cycle call and the ~5s tick)
    records only when the in-process window source is active, and is a cheap
    no-op otherwise — so the default (flag-off) path adds no window sampling."""
    # Active: a fresh sample is appended.
    eng = _engine(True)
    src = _FakeSource([])
    eng.window_source = src
    before = len(src.samples)
    eng.record_window_sample_if_active(T0)
    assert len(src.samples) == before + 1

    # Flag off: no-op (no sample recorded).
    eng_off = _engine(False)
    src_off = _FakeSource([])
    eng_off.window_source = src_off
    eng_off.record_window_sample_if_active(T0)
    assert len(src_off.samples) == 0

    # No source wired: no crash, no-op.
    eng_none = _engine(True)
    eng_none.record_window_sample_if_active(T0)  # must not raise
