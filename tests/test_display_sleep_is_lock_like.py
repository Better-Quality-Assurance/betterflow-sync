"""Display sleep (NSWorkspaceScreensDidSleepNotification) must not earn the
presence bridge. Tudor Brad, 2026-10-09: "the display turning off by itself
from inactivity is treated the same as a lock."

Before this change, ScreensDidSleep was wired to the SAME handler as
NSWorkspaceWillSleepNotification (real system/lid sleep) via a single
`sleeping` dedup flag in system_events.py, so a desktop that never truly
sleeps (always plugged in, display just dims from inactivity) reported
sleep_time for that whole dim period and earned
internal-tool2's bridgeActivityToSleepStarts/clampIdleTailBeforeSleep the
same way a genuine suspend does -- wrong, because nobody walked away from a
deliberate action the way closing a lid is one.

ScreensDidSleep now calls its own SystemEventHandler.on_display_sleep,
behind the SAME capabilities.lock_time gate as a literal screen lock:
  - gate OFF: delegates straight to on_system_sleep -- byte-identical to
    this agent's behaviour before this split existed. Unaffected: old agent
    builds (which don't have this code at all) and days already recorded.
  - gate ON: delegates to on_screen_lock instead, reusing its existing,
    already-tested span-open/no-overlap/contiguous-split logic rather than
    inventing a parallel one. If a real sleep follows (WillSleep, which is
    NO LONGER deduped against ScreensDidSleep -- see system_events.py), the
    EXISTING on_system_sleep boundary-flush closes the display-off-opened
    lock span at the sleep instant and reopens it fresh on wake, exactly as
    it already does for a literal screen lock open when sleep begins.

Wake notifications (DidWake / ScreensDidWake) are deliberately left
untouched and combined -- on_system_wake already branches on STATE
(_sleep_start / _locked_while_asleep), not on which notification arrived,
so no new wake callback was needed. See system_events.py's docstring for
the full reasoning, including why "premature" resume() between a display
wake and an eventual unlock is harmless: the lock_time exclusion is driven
by the eventually-sent SPAN's duration, not by whether local capture
happened to be paused throughout it.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.system_event_handler import SystemEventHandler


class _Clock:
    def __init__(self, start: datetime):
        self._next = start

    def now(self, *args, **kwargs):
        value = self._next
        self._next = self._next + timedelta(seconds=1)
        return value


def _make_handler(lock_time_capable: bool) -> SystemEventHandler:
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


class _DatetimeProxy:
    def __init__(self, clock: _Clock):
        self.now = clock.now


def _patch_clock(start=None):
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = _Clock(start)
    return patch("src.system_event_handler.datetime", _DatetimeProxy(clock)), clock


# --- gate OFF: byte-identical to before this split existed ---


def test_gate_off_display_sleep_becomes_sleep_time_not_lock_time():
    handler = _make_handler(lock_time_capable=False)
    patcher, _ = _patch_clock()
    with patcher:
        handler.on_display_sleep()
        handler.on_system_wake()

    handler.sync_engine.send_sleep_event.assert_called_once()
    handler.sync_engine.send_lock_event.assert_not_called()


def test_gate_off_display_sleep_call_shape_matches_on_system_sleep_directly():
    """The delegate must be a straight pass-through -- same call count and
    arguments as calling on_system_sleep itself, not a parallel reimplementation
    that happens to produce a similar-looking result."""
    direct = _make_handler(lock_time_capable=False)
    via_display = _make_handler(lock_time_capable=False)

    p1, _ = _patch_clock()
    with p1:
        direct.on_system_sleep()
        direct.on_system_wake()

    p2, _ = _patch_clock()
    with p2:
        via_display.on_display_sleep()
        via_display.on_system_wake()

    assert (
        direct.sync_engine.send_sleep_event.call_args
        == via_display.sync_engine.send_sleep_event.call_args
    )


# --- gate ON: the control pair the server-side bridge test needs ---


def test_gate_on_display_sleep_dispatches_lock_time_while_real_sleep_still_dispatches_sleep_time():
    """The control: display-off and a real sleep, same position, same gate.
    One must become lock_time (excluded, non-bridging server-side -- proven
    by LockDoesNotBridgePresenceTest::identical_gap_bridges_for_sleep_but_not_for_lock
    in internal-tool2), the other sleep_time (bridges). This test proves the
    AGENT'S half of that property: which event type each notification
    produces. The SERVER'S half (that lock_time does not bridge and
    sleep_time does) is proven independently in the paired repo."""
    display_off = _make_handler(lock_time_capable=True)
    real_sleep = _make_handler(lock_time_capable=True)

    p1, _ = _patch_clock()
    with p1:
        display_off.on_display_sleep()
        display_off.on_screen_unlock()  # eventual unlock — screen locked for real while dark

    p2, _ = _patch_clock()
    with p2:
        real_sleep.on_system_sleep()
        real_sleep.on_system_wake()

    display_off.sync_engine.send_lock_event.assert_called_once()
    display_off.sync_engine.send_sleep_event.assert_not_called()

    real_sleep.sync_engine.send_sleep_event.assert_called_once()
    real_sleep.sync_engine.send_lock_event.assert_not_called()


def test_gate_on_display_off_then_lock_while_dark_then_wake_then_unlock_is_one_span():
    """display-off -> lock while dark -> wake -> unlock. macOS fires the real
    screenIsLocked notification shortly after the display dims (if the OS is
    configured to require a password), then later the display wakes to show
    the lock screen (ScreensDidWake -- left untouched, routes through the
    existing combined on_system_wake, which does nothing to a still-open lock
    span per its own docstring), then the user actually unlocks. The whole
    episode must be ONE lock span, exactly like two real lock notifications
    before an unlock already keep the earliest start
    (test_a_second_lock_notification_without_unlock_keeps_the_earliest_start)."""
    handler = _make_handler(lock_time_capable=True)
    patcher, _ = _patch_clock()
    with patcher:
        handler.on_display_sleep()  # T0: display dims
        handler.on_screen_lock()  # T0+1s: the real OS lock notification follows
        handler.on_system_wake()  # T0+2s: display wakes, still locked (no real sleep occurred)
        handler.on_screen_unlock()  # T0+3s: user returns

    handler.sync_engine.send_lock_event.assert_called_once()
    handler.sync_engine.send_sleep_event.assert_not_called()
    args, kwargs = handler.sync_engine.send_lock_event.call_args
    assert args[0] == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc), (
        "the span must start at display-off, not at the later literal-lock "
        "notification -- the second notification is a duplicate of an "
        "already-open span (keep-the-earliest-start), same as two real locks"
    )


def test_gate_on_display_off_then_real_sleep_then_wake_splits_contiguously():
    """display-off -> real sleep -> wake. Mirrors
    test_lock_before_sleep_splits_into_three_contiguous_non_overlapping_spans
    exactly, but the pre-sleep span is opened by a display dim rather than a
    literal lock notification -- proving on_system_sleep's existing boundary
    flush doesn't care which path opened _lock_start."""
    handler = _make_handler(lock_time_capable=True)
    patcher, _ = _patch_clock()
    with patcher:
        handler.on_display_sleep()  # T0: display dims
        handler.on_system_sleep()  # T1: system actually sleeps while dimmed
        handler.on_system_wake()  # T2: wakes, still "locked" (display-off span reopened)
        handler.on_screen_unlock()  # T3: user returns

    lock_calls = handler.sync_engine.send_lock_event.call_args_list
    sleep_calls = handler.sync_engine.send_sleep_event.call_args_list
    assert len(lock_calls) == 2, lock_calls
    assert len(sleep_calls) == 1, sleep_calls

    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=1)
    t2 = t1 + timedelta(seconds=1)

    pre_sleep_lock = lock_calls[0]
    assert pre_sleep_lock.args[0] == t0
    assert pre_sleep_lock.args[1] == t1, "closed exactly at the sleep boundary"

    sleep_span = sleep_calls[0]
    assert sleep_span.args[0] == t1, "no gap: starts exactly where the display-off span ended"

    post_wake_lock = lock_calls[1]
    assert post_wake_lock.args[0] == t2, "no gap: starts exactly where wake fired"

    assert pre_sleep_lock.args[1] == sleep_span.args[0]


def test_gate_on_a_real_sleep_that_fires_will_sleep_and_screens_did_sleep_is_not_double_counted():
    """The common real-world case: a real sleep fires BOTH notifications.
    WillSleep (-> on_system_sleep, unconditional, never suppressed by
    ScreensDidSleep per system_events.py) must still be the one that opens
    the sleep span — ScreensDidSleep (-> on_display_sleep) arriving around
    the same moment must not also open a competing lock span that steals
    part of the sleep."""
    handler = _make_handler(lock_time_capable=True)
    patcher, _ = _patch_clock()
    with patcher:
        handler.on_system_sleep()  # WillSleep fires first
        handler.on_display_sleep()  # ScreensDidSleep follows moments later
        handler.on_system_wake()

    handler.sync_engine.send_sleep_event.assert_called_once()
    handler.sync_engine.send_lock_event.assert_not_called(), (
        "ScreensDidSleep arriving after WillSleep has already opened "
        "_sleep_start must defer via _locked_while_asleep, not open its own "
        "span -- on_screen_lock's existing guard already does this"
    )
