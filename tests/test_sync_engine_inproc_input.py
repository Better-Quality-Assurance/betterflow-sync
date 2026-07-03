from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from src.config import Config
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.aw_client import BUCKET_TYPE_INPUT
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.input_source import InputSource
from src.sync.sync_engine import SyncEngine, SyncStats, _is_input_like

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


def _engine(in_process_input: bool):
    cfg = Config()
    cfg.sync.in_process_input = in_process_input
    return SyncEngine(aw=Mock(), bf=Mock(), queue=Mock(), config=cfg,
                      activity_analyzer=Mock(spec=ActivityAnalyzer),
                      time_tracker=Mock(spec=DailyTimeTracker))


class _FakeBackend:
    def available(self):
        return True

    def start(self):
        return True

    def stop(self):
        pass


def _source():
    """An InputSource with a forced-usable backend and no app tagging."""
    return InputSource(hostname="host", backend=_FakeBackend(),
                       frontmost_app_getter=None)


def _bucket(bucket_id, btype):
    b = Mock()
    b.id = bucket_id
    b.type = btype
    return b


# -- gating ------------------------------------------------------------------


def test_flag_off_is_no_op():
    eng = _engine(False)
    eng.input_source = _source()
    eng.input_source._on_press(5)
    assert eng._should_use_inproc_input() is False
    assert eng._should_skip_external_input() is False
    # Build is inert even with counts present, and the counters are untouched.
    assert eng._build_inproc_input(T0 + timedelta(seconds=60)) == (None, None)
    assert eng.input_source.counts == (5, 0, 0)


def test_active_when_flag_on_and_available():
    eng = _engine(True)
    eng.input_source = _source()
    assert eng._should_use_inproc_input() is True
    assert eng._should_skip_external_input() is True


def test_inactive_when_no_source_wired():
    eng = _engine(True)
    assert eng._should_skip_external_input() is False


def test_inactive_when_backend_unavailable():
    eng = _engine(True)
    eng.input_source = InputSource(hostname="host", backend=None,
                                   frontmost_app_getter=None)
    assert eng._should_skip_external_input() is False


# -- build / drain -----------------------------------------------------------


def test_first_build_seeds_checkpoint_and_emits_nothing():
    eng = _engine(True)
    eng.input_source = _source()
    eng.input_source._on_press(3)  # counts before first cycle
    event, pending = eng._build_inproc_input(T0)
    assert event is None and pending is None
    assert eng._input_inproc_checkpoint == T0


def test_build_drains_counts_into_event():
    eng = _engine(True)
    eng.input_source = _source()
    eng._build_inproc_input(T0)  # seed
    eng.input_source._on_press(7)
    eng.input_source._on_click(2)
    event, pending = eng._build_inproc_input(T0 + timedelta(seconds=60))
    assert event is not None
    assert event["data"]["presses"] == 7
    assert event["data"]["clicks"] == 2
    assert event["bucket_id"] == "bf-input-inproc_host"
    assert event["bucket_type"] == BUCKET_TYPE_INPUT
    assert pending == T0 + timedelta(seconds=60)
    # Drained -> counters reset.
    assert eng.input_source.counts == (0, 0, 0)


def test_zero_count_span_advances_checkpoint_without_event():
    eng = _engine(True)
    eng.input_source = _source()
    eng._build_inproc_input(T0)  # seed
    event, pending = eng._build_inproc_input(T0 + timedelta(seconds=60))
    assert event is None and pending is None
    # Checkpoint advanced so the next span starts fresh (no stall).
    assert eng._input_inproc_checkpoint == T0 + timedelta(seconds=60)


def test_clock_step_back_reseeds():
    eng = _engine(True)
    eng.input_source = _source()
    eng._build_inproc_input(T0)  # seed at T0
    eng.input_source._on_press(1)
    event, pending = eng._build_inproc_input(T0 - timedelta(seconds=30))
    assert event is None and pending is None
    assert eng._input_inproc_checkpoint == T0 - timedelta(seconds=30)


# -- checkpoint commit -------------------------------------------------------


def test_checkpoint_advances_on_confirmed_send():
    eng = _engine(True)
    eng.input_source = _source()
    eng._build_inproc_input(T0)  # seed
    eng.input_source._on_press(4)
    _, pending = eng._build_inproc_input(T0 + timedelta(seconds=60))
    stats = SyncStats()  # no queued buckets -> confirmed
    eng._commit_inproc_input_checkpoint(stats, pending)
    assert eng._input_inproc_checkpoint == T0 + timedelta(seconds=60)


def test_checkpoint_advances_even_when_queued():
    """Unlike window/AFK, the drain already RESET the counters, so the counts now
    live only in the (stable-id) event the queue redelivers. Holding the
    checkpoint would re-drain an empty counter into a duplicate span, so we
    advance regardless — the queued event's stable id makes redelivery
    idempotent."""
    eng = _engine(True)
    eng.input_source = _source()
    eng._build_inproc_input(T0)  # seed
    eng.input_source._on_press(4)
    _, pending = eng._build_inproc_input(T0 + timedelta(seconds=60))
    stats = SyncStats()
    stats.queued_bucket_ids.add("bf-input-inproc_host")
    eng._commit_inproc_input_checkpoint(stats, pending)
    assert eng._input_inproc_checkpoint == T0 + timedelta(seconds=60)


def test_commit_is_forward_only():
    eng = _engine(True)
    eng.input_source = _source()
    eng._input_inproc_checkpoint = T0 + timedelta(seconds=100)
    stats = SyncStats()
    # A stale pending from an abandoned cycle must not rewind the checkpoint.
    eng._commit_inproc_input_checkpoint(stats, T0 + timedelta(seconds=30))
    assert eng._input_inproc_checkpoint == T0 + timedelta(seconds=100)


def test_commit_noop_when_pending_none():
    eng = _engine(True)
    eng.input_source = _source()
    eng._input_inproc_checkpoint = T0
    eng._commit_inproc_input_checkpoint(SyncStats(), None)
    assert eng._input_inproc_checkpoint == T0


# -- coexistence with the external input path --------------------------------


def test_external_input_buckets_skipped_when_active():
    eng = _engine(True)
    eng.input_source = _source()
    input_buckets = [_bucket("aw-watcher-input_host", BUCKET_TYPE_INPUT)]
    skip = eng._should_skip_external_input()
    to_sync = [b for b in input_buckets if not _is_input_like(b.type)] if skip else input_buckets
    assert to_sync == []  # external input bucket dropped, no double-count


def test_external_input_buckets_kept_when_flag_off():
    eng = _engine(False)
    eng.input_source = _source()
    input_buckets = [_bucket("aw-watcher-input_host", BUCKET_TYPE_INPUT)]
    skip = eng._should_skip_external_input()
    to_sync = [b for b in input_buckets if not _is_input_like(b.type)] if skip else input_buckets
    assert to_sync == input_buckets  # unchanged when dormant


def test_is_input_like_only_matches_input_type():
    from src.sync.aw_client import BUCKET_TYPE_WINDOW, BUCKET_TYPE_AFK
    assert _is_input_like(BUCKET_TYPE_INPUT) is True
    assert _is_input_like(BUCKET_TYPE_WINDOW) is False
    assert _is_input_like(BUCKET_TYPE_AFK) is False
