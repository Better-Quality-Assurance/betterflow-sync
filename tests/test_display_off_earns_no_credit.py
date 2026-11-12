"""A display that turns off from inactivity earns no credit (betterflow-sync#273).

Tudor's decision, 2026-10-09: the display turning off by itself is treated
like a lock -- no credit while it is off, and none of the idle minutes before
it bridged back by the server's sleep grace. The first attempt (#271, round 3,
reverted) routed display-off straight into on_screen_lock and never closed the
span at wake, so display-off 10:00 / mouse 10:02 / real lock 18:00 uploaded a
lock_time span of 10:00-18:00: a whole working day zeroed.

What these tests pin, all behind ``config.capabilities.lock_time`` (the same
server-advertised gate as the real lock signal, default OFF):

* display-off then mouse movement: the span closes AT THE WAKE, tracking
  resumes, and a later real lock opens its own span at its own instant;
* display-off then a real lock then unlock: one span from the display-off to
  the unlock, nothing credited in between, in either wake/unlock order;
* display-off then a real system sleep: the display-off span ends at the
  sleep boundary, the sleep reports as sleep_time, and nothing reopens at
  wake unless a real lock was seen;
* a real lock followed by display-off: one span from the lock, the display
  wake neither closes it nor resumes while the lock screen is up;
* a display wake during a break or a user pause does not resume tracking
  (the same guards on_system_wake keeps);
* gate off: display-off is the legacy sleep it always was, with the old
  listener dedup (a WillSleep after display-off runs the sleep path once).

This supersedes tests/test_display_sleep_cannot_reach_lock.py, which pinned
item 3 OUT of #271 and is retired here.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.system_event_handler import SystemEventHandler

UTC = timezone.utc


def _at(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 1, 1, hh, mm, ss, tzinfo=UTC)


class _SettableClock:
    """``datetime.now`` replacement that returns whatever ``t`` is set to."""

    def __init__(self, start: datetime):
        self.t = start

    def now(self, *args, **kwargs):
        return self.t


class _DatetimeProxy:
    def __init__(self, clock: _SettableClock):
        self.now = clock.now


def _make_handler(lock_time_capable: bool = True) -> SystemEventHandler:
    coordinator = MagicMock()
    coordinator.is_on_break = False
    coordinator.paused_by_network = False
    sync_engine = MagicMock()
    sync_engine.is_private = False
    sync_engine.config.capabilities.lock_time = lock_time_capable
    return SystemEventHandler(
        sync_engine=sync_engine,
        tray=MagicMock(),
        coordinator=coordinator,
        reminder_manager=MagicMock(),
        bf=MagicMock(),
        aw=MagicMock(),
        pause_state_lock=threading.RLock(),
        shutdown_fn=MagicMock(),
    )


def _clock(start: datetime):
    clock = _SettableClock(start)
    return patch("src.system_event_handler.datetime", _DatetimeProxy(clock)), clock


def _lock_spans(handler: SystemEventHandler) -> list[tuple]:
    """Every send_lock_event call as (start, end-or-None)."""
    out = []
    for call in handler.sync_engine.send_lock_event.call_args_list:
        args, kwargs = call
        start = args[0]
        end = args[1] if len(args) > 1 else kwargs.get("end")
        out.append((start, end))
    return out


# -- done-means scenario 1: display-off, then mouse movement ------------------


def test_display_off_then_mouse_closes_the_span_at_wake_and_resumes():
    handler = _make_handler()
    patcher, clock = _clock(_at(10, 0))
    with patcher:
        handler.on_display_sleep()
        assert handler.sync_engine.pause.called, "display-off must stop credit"
        assert _lock_spans(handler) == [], "nothing is sent while the display is off"

        clock.t = _at(10, 2)
        handler.on_display_wake()

    assert _lock_spans(handler) == [(_at(10, 0), _at(10, 2))], (
        "the display-off span must close AT THE WAKE, not stay open"
    )
    handler.sync_engine.resume.assert_called_once()
    handler.sync_engine.send_sleep_event.assert_not_called()


def test_display_off_without_a_lock_does_not_lock_the_rest_of_the_day():
    """#271's Critical, as reported: display-off 10:00, mouse 10:02, a full
    day of work, a real lock at 18:00 and unlock at 18:05. The real lock must
    open its own span at 18:00, never inherit the 10:00 display-off."""
    handler = _make_handler()
    patcher, clock = _clock(_at(10, 0))
    with patcher:
        handler.on_display_sleep()
        clock.t = _at(10, 2)
        handler.on_display_wake()

        clock.t = _at(18, 0)
        handler.on_screen_lock()
        clock.t = _at(18, 5)
        handler.on_screen_unlock()

    spans = _lock_spans(handler)
    assert spans == [(_at(10, 0), _at(10, 2)), (_at(18, 0), None)], spans


def test_display_off_then_only_a_system_wake_still_closes_the_span():
    """Robustness: if the wake reaches us as DidWake rather than
    ScreensDidWake, the display-off span must still close at that wake --
    otherwise it is #271's open-forever span again."""
    handler = _make_handler()
    patcher, clock = _clock(_at(10, 0))
    with patcher:
        handler.on_display_sleep()
        clock.t = _at(10, 2)
        handler.on_system_wake()
        clock.t = _at(18, 0)
        handler.on_screen_lock()
        clock.t = _at(18, 5)
        handler.on_screen_unlock()

    assert _lock_spans(handler) == [(_at(10, 0), _at(10, 2)), (_at(18, 0), None)]


# -- done-means scenario 2: display-off, lock, unlock -------------------------


def test_display_off_then_lock_then_unlock_credits_nothing_in_between():
    handler = _make_handler()
    patcher, clock = _clock(_at(10, 0))
    with patcher:
        handler.on_display_sleep()
        clock.t = _at(10, 5)  # password required 5 min after display-off
        handler.on_screen_lock()
        clock.t = _at(10, 30)  # mouse moved: display on, lock screen showing
        handler.on_display_wake()
        assert _lock_spans(handler) == [], "still locked: nothing closes at the display wake"
        handler.sync_engine.resume.assert_not_called()
        clock.t = _at(10, 31)
        handler.on_screen_unlock()

    assert _lock_spans(handler) == [(_at(10, 0), None)], (
        "one span from the display-off instant, closed by the unlock"
    )
    handler.sync_engine.resume.assert_called_once()


def test_display_off_lock_unlock_processed_before_the_display_wake():
    """Lock/unlock and power notifications arrive on different threads, so
    the unlock can be processed before the display wake. The display wake
    that follows must not open, close or send anything."""
    handler = _make_handler()
    patcher, clock = _clock(_at(10, 0))
    with patcher:
        handler.on_display_sleep()
        clock.t = _at(10, 5)
        handler.on_screen_lock()
        clock.t = _at(10, 31)
        handler.on_screen_unlock()
        clock.t = _at(10, 31, 1)
        handler.on_display_wake()
        clock.t = _at(18, 0)
        handler.on_screen_lock()
        clock.t = _at(18, 5)
        handler.on_screen_unlock()

    assert _lock_spans(handler) == [(_at(10, 0), None), (_at(18, 0), None)]


def test_lock_then_display_off_then_wake_while_locked_keeps_one_lock_span():
    """The common order: the user locks, the display goes dark a minute
    later, the mouse wakes the display with the lock screen still up. The
    display-off must not restart the open lock span, and the display wake
    must not close a span it did not open or resume while still locked."""
    handler = _make_handler()
    patcher, clock = _clock(_at(10, 0))
    with patcher:
        handler.on_screen_lock()
        clock.t = _at(10, 1)
        handler.on_display_sleep()
        clock.t = _at(10, 30)
        handler.on_display_wake()
        assert _lock_spans(handler) == [], "still locked: nothing closes at the display wake"
        handler.sync_engine.resume.assert_not_called()
        clock.t = _at(10, 31)
        handler.on_screen_unlock()

    assert _lock_spans(handler) == [(_at(10, 0), None)], (
        "one span from the REAL lock instant, closed by the unlock"
    )
    handler.sync_engine.resume.assert_called_once()


# -- a display wake must keep on_system_wake's stay-paused guards -------------


def _assert_display_wake_stays_paused(handler: SystemEventHandler) -> None:
    patcher, clock = _clock(_at(12, 0))
    with patcher:
        handler.on_display_sleep()
        clock.t = _at(12, 5)
        handler.on_display_wake()

    handler.sync_engine.resume.assert_not_called()
    assert all(end is not None for _start, end in _lock_spans(handler)), (
        "a display-off span must still close at the wake, paused or not"
    )


def test_display_wake_during_a_break_does_not_resume():
    handler = _make_handler()
    handler.coordinator.is_on_break = True
    _assert_display_wake_stays_paused(handler)


def test_display_wake_during_a_user_pause_does_not_resume():
    handler = _make_handler()
    handler._user_paused = True
    _assert_display_wake_stays_paused(handler)


# -- display-off followed by a real system sleep ------------------------------


def test_display_off_then_real_sleep_splits_at_the_sleep_boundary():
    handler = _make_handler()
    patcher, clock = _clock(_at(10, 0))
    with patcher:
        handler.on_display_sleep()
        clock.t = _at(10, 20)
        handler.on_system_sleep()
        clock.t = _at(12, 0)
        handler.on_system_wake()
        clock.t = _at(12, 0, 1)
        handler.on_display_wake()
        clock.t = _at(18, 0)
        handler.on_screen_lock()
        clock.t = _at(18, 5)
        handler.on_screen_unlock()

    assert _lock_spans(handler) == [(_at(10, 0), _at(10, 20)), (_at(18, 0), None)], (
        "display-off ends at the sleep boundary; no lock reopens at wake without a real lock"
    )
    handler.sync_engine.send_sleep_event.assert_called_once_with(_at(10, 20))
    assert handler.sync_engine.resume.call_count == 2, "once at the wake, once at the unlock"


def test_display_off_lock_then_real_sleep_reopens_the_lock_at_wake():
    handler = _make_handler()
    patcher, clock = _clock(_at(10, 0))
    with patcher:
        handler.on_display_sleep()
        clock.t = _at(10, 5)
        handler.on_screen_lock()
        clock.t = _at(10, 20)
        handler.on_system_sleep()
        clock.t = _at(12, 0)
        handler.on_system_wake()
        handler.on_display_wake()
        clock.t = _at(12, 1)
        handler.on_screen_unlock()

    assert _lock_spans(handler) == [(_at(10, 0), _at(10, 20)), (_at(12, 0), None)]
    handler.sync_engine.send_sleep_event.assert_called_once_with(_at(10, 20))


# -- gate off: legacy behaviour, unchanged ------------------------------------


def test_gate_off_display_off_is_the_legacy_sleep():
    handler = _make_handler(lock_time_capable=False)
    patcher, clock = _clock(_at(10, 0))
    with patcher:
        handler.on_display_sleep()
        clock.t = _at(10, 2)
        handler.on_display_wake()

    handler.sync_engine.send_sleep_event.assert_called_once_with(_at(10, 0))
    handler.sync_engine.send_lock_event.assert_not_called()
    handler.sync_engine.resume.assert_called_once()


def test_gate_off_display_off_then_willsleep_runs_the_sleep_path_once():
    """The old listener fed ScreensDidSleep and WillSleep into one deduped
    callback. With the two now routed separately, the gate-off path must
    keep that dedup: one pause, one sleep span from the display-off, one
    resume however the two wakes arrive."""
    handler = _make_handler(lock_time_capable=False)
    patcher, clock = _clock(_at(10, 0))
    with patcher:
        handler.on_display_sleep()
        clock.t = _at(10, 20)
        handler.on_system_sleep()
        clock.t = _at(12, 0)
        handler.on_system_wake()
        handler.on_display_wake()

    assert handler.sync_engine.pause.call_count == 1
    handler.sync_engine.send_sleep_event.assert_called_once_with(_at(10, 0))
    handler.sync_engine.resume.assert_called_once()


def test_gate_off_display_wake_before_did_wake_resumes_once():
    handler = _make_handler(lock_time_capable=False)
    patcher, clock = _clock(_at(10, 0))
    with patcher:
        handler.on_display_sleep()
        clock.t = _at(10, 20)
        handler.on_system_sleep()
        clock.t = _at(12, 0)
        handler.on_display_wake()
        handler.on_system_wake()

    handler.sync_engine.send_sleep_event.assert_called_once_with(_at(10, 0))
    handler.sync_engine.resume.assert_called_once()


def test_gate_turned_off_mid_span_still_closes_it_at_wake():
    """The span is closed by state, not by the gate: a gate that flips off
    while the display is off must not leave a span open. send_lock_event
    drops it itself when the gate is off."""
    handler = _make_handler(lock_time_capable=True)
    patcher, clock = _clock(_at(10, 0))
    with patcher:
        handler.on_display_sleep()
        handler.sync_engine.config.capabilities.lock_time = False
        clock.t = _at(10, 2)
        handler.on_display_wake()
        clock.t = _at(18, 0)
        handler.on_screen_lock()
        clock.t = _at(18, 5)
        handler.on_screen_unlock()

    assert _lock_spans(handler) == [(_at(10, 0), _at(10, 2)), (_at(18, 0), None)]
    assert handler.sync_engine.resume.call_count == 2, "once at the wake, once at the unlock"


def test_gate_turned_on_after_a_legacy_pair_still_closes_the_next_span():
    """A gate-off display-off whose only wake was the display wake leaves the
    legacy dedup state at "woken". If the gate turns on before the next
    display-off, that stale state must not swallow the next display wake --
    otherwise the span stays open, #271's Critical by another road."""
    handler = _make_handler(lock_time_capable=False)
    patcher, clock = _clock(_at(9, 0))
    with patcher:
        handler.on_display_sleep()
        clock.t = _at(9, 5)
        handler.on_display_wake()  # no DidWake follows: legacy state stays "woken"

        handler.sync_engine.config.capabilities.lock_time = True
        clock.t = _at(10, 0)
        handler.on_display_sleep()
        clock.t = _at(10, 2)
        handler.on_display_wake()

    assert _lock_spans(handler) == [(_at(10, 0), _at(10, 2))]
    assert handler._display_off_start is None


# -- lid close: ScreensDidSleep a moment before WillSleep (review round 1) ---


def test_lid_close_display_off_then_sleep_2s_later_uploads_no_lock():
    """A lid close fires ScreensDidSleep a second or two before WillSleep.
    That is one suspend, not a display-off: uploading the 2s as lock_time
    would make the server's sleep bridge see a lock in the gap and cancel
    the suspend's presence grace. The sleep owns that time, from the
    display-off instant, exactly as the legacy path reported it."""
    handler = _make_handler()
    patcher, clock = _clock(_at(10, 0))
    with patcher:
        handler.on_display_sleep()
        clock.t = _at(10, 0, 2)
        handler.on_system_sleep()
        clock.t = _at(12, 0)
        handler.on_system_wake()
        handler.on_display_wake()

    handler.sync_engine.send_lock_event.assert_not_called()
    handler.sync_engine.send_sleep_event.assert_called_once_with(_at(10, 0))
    handler.sync_engine.resume.assert_called_once()


def test_display_off_then_sleep_just_past_the_window_still_uploads_the_span():
    from src.system_event_handler import DISPLAY_OFF_IS_SLEEP_WITHIN_SECONDS

    handler = _make_handler()
    patcher, clock = _clock(_at(10, 0))
    with patcher:
        handler.on_display_sleep()
        sleep_at = _at(10, 0) + timedelta(seconds=DISPLAY_OFF_IS_SLEEP_WITHIN_SECONDS + 1)
        clock.t = sleep_at
        handler.on_system_sleep()
        clock.t = _at(12, 0)
        handler.on_system_wake()

    assert _lock_spans(handler) == [(_at(10, 0), sleep_at)]
    handler.sync_engine.send_sleep_event.assert_called_once_with(sleep_at)


def test_lock_seen_during_a_lid_close_still_reopens_at_wake():
    """Lid close with password-on-sleep (the usual corporate setting):
    display-off 10:00:00, lock 10:00:01, WillSleep 10:00:02. One suspend:
    no lock_time before the sleep (it would cancel the server's sleep
    bridge), the sleep starts at the display-off, and because the screen is
    still locked the lock reopens at the wake and the unlock closes it."""
    handler = _make_handler()
    patcher, clock = _clock(_at(10, 0))
    with patcher:
        handler.on_display_sleep()
        clock.t = _at(10, 0, 1)
        handler.on_screen_lock()
        clock.t = _at(10, 0, 2)
        handler.on_system_sleep()
        assert _lock_spans(handler) == [], "nothing may be uploaded before the sleep"
        clock.t = _at(12, 0)
        handler.on_system_wake()
        clock.t = _at(12, 1)
        handler.on_screen_unlock()

    assert _lock_spans(handler) == [(_at(12, 0), None)]
    handler.sync_engine.send_sleep_event.assert_called_once_with(_at(10, 0))
    handler.sync_engine.resume.assert_called_once()


def test_lock_on_a_display_off_then_sleep_past_the_window_uploads_the_lock():
    """Same shape, but the sleep comes 65s after the display-off: outside
    the window, so this is a display-off, a lock, then a separate sleep."""
    handler = _make_handler()
    patcher, clock = _clock(_at(10, 0))
    with patcher:
        handler.on_display_sleep()
        clock.t = _at(10, 0, 1)
        handler.on_screen_lock()
        clock.t = _at(10, 1, 5)
        handler.on_system_sleep()
        clock.t = _at(12, 0)
        handler.on_system_wake()
        clock.t = _at(12, 1)
        handler.on_screen_unlock()

    assert _lock_spans(handler) == [(_at(10, 0), _at(10, 1, 5)), (_at(12, 0), None)]
    handler.sync_engine.send_sleep_event.assert_called_once_with(_at(10, 1, 5))


def test_a_real_lock_before_the_display_off_still_stops_the_bridge():
    """Ctrl-Cmd-Q: a deliberate lock BEFORE the display-off is a real lock
    in the gap, even with a sleep 2s later. Unchanged: flushed at the sleep."""
    handler = _make_handler()
    patcher, clock = _clock(_at(10, 0))
    with patcher:
        handler.on_screen_lock()
        clock.t = _at(10, 0, 1)
        handler.on_display_sleep()
        clock.t = _at(10, 0, 2)
        handler.on_system_sleep()
        clock.t = _at(12, 0)
        handler.on_system_wake()
        clock.t = _at(12, 1)
        handler.on_screen_unlock()

    assert _lock_spans(handler) == [(_at(10, 0), _at(10, 0, 2)), (_at(12, 0), None)]
    handler.sync_engine.send_sleep_event.assert_called_once_with(_at(10, 0, 2))
