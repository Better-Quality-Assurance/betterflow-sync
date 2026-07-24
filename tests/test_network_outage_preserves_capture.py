"""Work done during a network outage must survive it.

A network drop used to call SyncEngine.pause(), which exists to mean "this
window must never be recorded" — private time, a manual break, a working-hours
close — and enforces that by advancing every bucket checkpoint past the window
so the events can never be fetched again.

Applied to a network outage that deletes billable work. And it deletes it in the
most misleading way possible: the events are never FETCHED, so the offline queue
never sees them, so the agent reports "0 queued" through an outage that lost real
time. That is why this went unnoticed for so long.

Livia Cimpeanu reported the symptom on 2026-06-16, five weeks before it was
found in the code: "I was tracked before my break. It isn't anymore."
"""

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from src.config import Config
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.afk_source import AfkSource
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.sync_engine import SyncEngine

T0 = datetime(2026, 7, 23, 19, 9, 26, tzinfo=timezone.utc)  # the real 07-23 drop
WIN_BUCKET = "aw-watcher-window_host"
LOOKBACK = timedelta(minutes=2)


class _Bucket:
    def __init__(self, bid):
        self.id = bid


class _FakeQueue:
    def __init__(self):
        self._cp = {}

    def set_checkpoint(self, bucket_id, ts, event_id=None):
        self._cp[bucket_id] = ts

    def set_checkpoint_forward(self, bucket_id, ts, event_id=None):
        if bucket_id not in self._cp or ts > self._cp[bucket_id]:
            self._cp[bucket_id] = ts

    def get_checkpoint(self, bucket_id):
        return self._cp.get(bucket_id)


def _engine():
    cfg = Config()
    cfg.sync.in_process_afk = True
    aw = Mock()
    aw.get_window_buckets.return_value = [_Bucket(WIN_BUCKET)]
    aw.get_web_buckets.return_value = []
    aw.get_afk_buckets.return_value = []
    aw.get_input_buckets.return_value = []
    eng = SyncEngine(aw=aw, bf=Mock(), queue=_FakeQueue(), config=cfg,
                     activity_analyzer=Mock(spec=ActivityAnalyzer),
                     time_tracker=Mock(spec=DailyTimeTracker))
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: 5.0)
    return eng


def _outage(engine, minutes, monkeypatch):
    """Drive a network outage of `minutes` and return where the next fetch starts."""
    import src.sync.sync_engine as se

    clock = {"t": T0}

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["t"]

    monkeypatch.setattr(se, "datetime", _DT)
    engine.queue.set_checkpoint(WIN_BUCKET, T0 - timedelta(seconds=30))
    engine.afk_source.record_sample(T0)
    engine._build_inproc_afk(T0)

    engine.suspend_upload("network offline")
    clock["t"] = T0 + timedelta(minutes=minutes)
    engine.resume_upload("network online")

    return engine.queue.get_checkpoint(WIN_BUCKET) - LOOKBACK


def test_an_eight_minute_outage_loses_nothing(monkeypatch):
    # Pre-fix this lost 6 minutes: 8 minutes of outage minus the 2-minute
    # lookback that was the only thing rescuing short blips.
    eng = _engine()
    next_fetch = _outage(eng, 8, monkeypatch)
    assert next_fetch <= T0, (
        "the next fetch must still reach back to before the outage started; "
        f"it starts at {next_fetch}, outage began {T0}"
    )


def test_a_ninety_five_minute_outage_loses_nothing(monkeypatch):
    # The real 2026-07-19 window. Pre-fix: 93 minutes deleted.
    eng = _engine()
    assert _outage(eng, 95, monkeypatch) <= T0


def test_the_inproc_afk_stream_is_not_skipped_either(monkeypatch):
    # The AW bucket checkpoint is not the only way the window can be dropped —
    # the in-process AFK stream keeps its own, and _advance_checkpoints_to_now
    # moves that one too. Assert it did not jump past the outage.
    eng = _engine()
    _outage(eng, 20, monkeypatch)
    assert eng._afk_inproc_checkpoint is None or eng._afk_inproc_checkpoint <= T0


def test_suspending_upload_does_not_mark_the_engine_paused(monkeypatch):
    # is_paused gates whether a cycle records at all. If suspend_upload set it,
    # capture would stop and the fix would be cosmetic.
    eng = _engine()
    eng.suspend_upload("network offline")
    assert eng.is_upload_suspended is True
    assert eng.is_paused is False


def test_resume_upload_clears_the_state(monkeypatch):
    eng = _engine()
    eng.suspend_upload("network offline")
    eng.resume_upload("network online")
    assert eng.is_upload_suspended is False


def _handler(engine, coordinator):
    from src.system_event_handler import SystemEventHandler

    return SystemEventHandler(
        sync_engine=engine,
        tray=Mock(),
        coordinator=coordinator,
        reminder_manager=Mock(),
        bf=Mock(),
        aw=Mock(),
        pause_state_lock=threading.RLock(),
        shutdown_fn=Mock(),
    )


def test_reconnect_resumes_upload_even_if_the_network_flag_was_cleared():
    # paused_by_network is cleared by paths that can fire mid-outage — a system
    # sleep (on_system_sleep) and a manual pause (_set_user_paused). If the
    # reconnect branch resumes only when that flag is still set, the engine
    # stays upload-suspended for the rest of the process with nothing left to
    # clear it.
    eng = _engine()
    coordinator = Mock()
    coordinator.paused_by_network = False
    handler = _handler(eng, coordinator)

    handler.on_network_change(False)
    assert eng.is_upload_suspended is True
    # ... a sleep or a manual pause clears the flag while still offline
    coordinator.paused_by_network = False

    handler.on_network_change(True)
    assert eng.is_upload_suspended is False


def test_an_outage_driven_through_the_handler_does_not_advance_checkpoints(monkeypatch):
    # The one test that exercises the branch that actually held the bug. Every
    # other test above drives suspend_upload/resume_upload directly, so they
    # assert the new flag flips and nothing about on_network_change — which is
    # where pause() was called. Drive the real OS-event entry point and assert
    # the property users care about: the window is still fetchable afterwards.
    import src.sync.sync_engine as se

    clock = {"t": T0}

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["t"]

    monkeypatch.setattr(se, "datetime", _DT)
    eng = _engine()
    eng.queue.set_checkpoint(WIN_BUCKET, T0 - timedelta(seconds=30))
    eng.afk_source.record_sample(T0)
    eng._build_inproc_afk(T0)

    coordinator = Mock()
    coordinator.paused_by_network = False
    coordinator.is_on_break = False
    handler = _handler(eng, coordinator)

    handler.on_network_change(False)
    clock["t"] = T0 + timedelta(minutes=20)
    handler.on_network_change(True)

    # Pre-fix both of these sat at T0 + 20min — pause() advanced them on the way
    # in and resume() advanced them again on the way out, so the 20 minutes of
    # work could never be fetched.
    assert eng.queue.get_checkpoint(WIN_BUCKET) - LOOKBACK <= T0
    assert eng._afk_inproc_checkpoint <= T0
    assert eng.is_paused is False


# --- the deliberate-discard cases must NOT regress -------------------------

def test_private_time_still_discards_its_window(monkeypatch):
    # Private Time records nothing. That is the contract, and it must survive
    # this change untouched.
    import src.sync.sync_engine as se

    clock = {"t": T0}

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["t"]

    monkeypatch.setattr(se, "datetime", _DT)
    eng = _engine()
    eng.queue.set_checkpoint(WIN_BUCKET, T0 - timedelta(seconds=30))
    eng.afk_source.record_sample(T0)
    eng._build_inproc_afk(T0)

    eng.set_private_mode(True)
    clock["t"] = T0 + timedelta(hours=1)
    eng.set_private_mode(False)

    leave = T0 + timedelta(hours=1)
    assert eng.queue.get_checkpoint(WIN_BUCKET) >= leave
    assert eng._afk_inproc_checkpoint >= leave


def test_a_manual_pause_still_discards_its_window(monkeypatch):
    import src.sync.sync_engine as se

    clock = {"t": T0}

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["t"]

    monkeypatch.setattr(se, "datetime", _DT)
    eng = _engine()
    eng.queue.set_checkpoint(WIN_BUCKET, T0 - timedelta(seconds=30))
    eng.afk_source.record_sample(T0)
    eng._build_inproc_afk(T0)

    eng.pause()
    clock["t"] = T0 + timedelta(minutes=20)
    eng.resume()

    assert eng.queue.get_checkpoint(WIN_BUCKET) >= T0 + timedelta(minutes=20)
