"""A macOS screen lock must be as visible on the server as display sleep is —
but it must NOT count as presence. Tudor Brad, 2026-10-08: "if you are not
present, you are not working." The idle time before a lock stays unbilled;
the lock itself only stops counting from the instant it begins.

Before this change, ``on_screen_lock``/``on_screen_unlock`` called
``sync_engine.pause()``/``resume()`` — exactly like system sleep does — but,
unlike sleep, uploaded NOTHING. Credit already stopped (pause() is what stops
it), but the server had no way to tell a lock apart from a crashed or quit
agent: both looked like silence.

A lock uploads through its OWN ``lock_time`` bucket_type
(``sync_engine.send_lock_event``) — NOT ``sleep_time`` with a tag. That was
tried first and reverted: internal-tool2's ``AgentEvent::inferEventType``
classifies PURELY on ``bucket_type``, and its presence-bridge
(``AgentAnalyticsService::bridgeActivityToSleepStarts`` /
``clampIdleTailBeforeSleep``) treats every ``sleep_time`` block's START as
proof of presence and bridges/clamps the idle gap before it — correct for a
genuine suspend (closing the lid IS a deliberate action proving presence up
to that instant) and exactly backwards for a lock (an auto-lock within 20
minutes of the last keystroke must NOT earn that idle gap back). A ``data``
tag can't opt a span out of that bridge, because the bridge's own query
filters on ``event_type`` — only a distinct bucket_type can.

The hard part is the overlap: a lock commonly starts BEFORE a system sleep
(the screen locks on an idle timeout well before the display/system sleep
timeout) and macOS does not auto-unlock on wake, so the lock persists AFTER
the wake too. Two independent [lock_start, unlock] / [sleep_start, wake]
spans covering overlapping wall-clock time would double-claim that time as
BOTH locked and asleep. So the lock span is split at the sleep boundary and
reopened at wake — the pre-sleep portion is flushed when sleep begins, and a
fresh span starts at the wake instant, covering only the time after wake
until the user actually unlocks.

A second, sharper hazard in that split: the lock/unlock listener
(NSDistributedNotificationCenter, polling its own run loop every 5s) and the
sleep/wake listener (NSWorkspace, dispatched via the main thread) are
INDEPENDENT OS notification threads with no ordering guarantee between them.
An unlock can be processed before its matching wake. If on_system_wake
blindly reopens a lock span whenever the system went to sleep while locked,
that reopened span runs through real work the user already returned to —
Tudor's repro, reproduced below as
test_unlock_before_wake_is_authoritative_and_does_not_extend_a_bogus_span.
The fix: on_screen_unlock clears the deferred-reopen flag unconditionally,
because an unlock is authoritative regardless of which listener's thread
gets there first.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.system_event_handler import SystemEventHandler


class _Clock:
    """Monotonically-advancing fake clock, one second per call, so every
    ``datetime.now(timezone.utc)`` call inside the handler gets a distinct,
    ordered, fully-predictable timestamp without hardcoding a call count."""

    def __init__(self, start: datetime):
        self._next = start

    def now(self, *args, **kwargs):
        value = self._next
        self._next = self._next + timedelta(seconds=1)
        return value


def _make_handler() -> SystemEventHandler:
    coordinator = MagicMock()
    coordinator.is_on_break = False
    coordinator.paused_by_network = False
    sync_engine = MagicMock()
    sync_engine.is_private = False
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
    """Stands in for the ``datetime`` class inside system_event_handler: calls
    to ``.now(...)`` go to the fake clock; everything else (unused here) would
    raise, which is the point — it catches an accidental new use."""

    def __init__(self, clock: _Clock):
        self.now = clock.now


def _patch_clock(start=None):
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = _Clock(start)
    return patch("src.system_event_handler.datetime", _DatetimeProxy(clock)), clock


def test_lock_then_unlock_uploads_a_single_lock_event():
    """The basic case: a coffee-break lock with no sleep involved at all."""
    handler = _make_handler()
    patcher, _ = _patch_clock()
    with patcher:
        handler.on_screen_lock()
        handler.sync_engine.send_lock_event.assert_not_called(), (
            "no span may be sent while still locked"
        )
        handler.on_screen_unlock()

    handler.sync_engine.send_lock_event.assert_called_once()
    handler.sync_engine.send_sleep_event.assert_not_called()
    args, kwargs = handler.sync_engine.send_lock_event.call_args
    assert kwargs == {}, "no reason tag -- lock_time is its own bucket_type"
    assert args[0] == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_app_quit_while_locked_sends_no_span():
    """No unlock ever arrives (process killed/quit while locked) — acceptable:
    nothing was ever flushed to begin with, matching how a sleep with no wake
    behaves identically today."""
    handler = _make_handler()
    patcher, _ = _patch_clock()
    with patcher:
        handler.on_screen_lock()

    handler.sync_engine.send_lock_event.assert_not_called()
    handler.sync_engine.send_sleep_event.assert_not_called()


def test_a_second_lock_notification_without_unlock_keeps_the_earliest_start():
    """Mirrors the existing sleep-start dedup: two lock notifications before
    any unlock must not truncate the front of the span."""
    handler = _make_handler()
    patcher, _ = _patch_clock()
    with patcher:
        handler.on_screen_lock()
        handler.on_screen_lock()  # duplicate notification, no unlock between
        handler.on_screen_unlock()

    args, kwargs = handler.sync_engine.send_lock_event.call_args
    assert args[0] == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc), (
        "the SECOND lock notification must not have reset the start"
    )


def test_sleep_only_with_no_lock_is_unaffected():
    """Regression guard: a sleep with no lock involved must produce the exact
    same call shape as before lock support existed at all."""
    handler = _make_handler()
    patcher, _ = _patch_clock()
    with patcher:
        handler.on_system_sleep()
        handler.on_system_wake()

    handler.sync_engine.send_sleep_event.assert_called_once()
    handler.sync_engine.send_lock_event.assert_not_called()
    args, kwargs = handler.sync_engine.send_sleep_event.call_args
    assert kwargs == {}
    assert len(args) == 1, "wake flushes with no explicit end, exactly as before"


def test_lock_before_sleep_splits_into_three_contiguous_non_overlapping_spans():
    """The dominant real-world ordering: the screen locks on its own (shorter)
    idle timeout, then later the system sleeps while already locked, then
    wakes (still locked, since macOS never auto-unlocks), then the user
    finally returns and unlocks.

    Must produce a lock span, then a sleep span, then another lock span, that
    exactly tile the elapsed time with no gap and no overlap: [lock, sleep),
    [sleep, wake), [wake, unlock).

    This is the POSITIVE CONTROL for the unlock-before-wake fix below: here
    wake is processed BEFORE unlock (the normal order), so the deferred-
    reopen flag must still correctly produce a fresh lock span at wake.
    """
    handler = _make_handler()
    patcher, _ = _patch_clock()
    with patcher:
        handler.on_screen_lock()       # T0: lock begins
        handler.on_system_sleep()      # T1: system sleeps while locked
        handler.on_system_wake()       # T2: wakes, still locked
        handler.on_screen_unlock()     # T3: user returns

    lock_calls = handler.sync_engine.send_lock_event.call_args_list
    sleep_calls = handler.sync_engine.send_sleep_event.call_args_list
    assert len(lock_calls) == 2, lock_calls
    assert len(sleep_calls) == 1, sleep_calls

    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=1)
    t2 = t1 + timedelta(seconds=1)

    pre_sleep_lock = lock_calls[0]
    assert pre_sleep_lock.args[0] == t0
    assert pre_sleep_lock.args[1] == t1, "closed exactly AT the sleep boundary"

    sleep_span = sleep_calls[0]
    assert sleep_span.args[0] == t1, "no gap: starts exactly where the lock span ended"

    post_wake_lock = lock_calls[1]
    assert post_wake_lock.args[0] == t2, "no gap: starts exactly where wake fired"

    # The defining property: no gap and no overlap at the sleep boundary.
    assert pre_sleep_lock.args[1] == sleep_span.args[0]


def test_lock_notification_racing_just_after_sleep_still_avoids_overlap():
    """The OS can, on some sleep paths, deliver the screenIsLocked
    notification concurrently with or just after NSWorkspaceWillSleep rather
    than before it. Whichever arrives first, the result must still be one
    sleep span plus one lock span, never overlapping.

    The clock is advanced by hours between the lock notification and the wake
    notification (simulating a real multi-hour sleep, where nothing in the
    agent queries the clock again until wake) so that the two candidate start
    times for the post-wake lock span — "the instant the lock notification
    arrived" versus "the instant wake fired" — are hours apart and the test
    can actually tell them apart. Opening the span at the earlier instant
    would silently re-claim hours of already-reported sleep as locked too.
    """
    handler = _make_handler()
    patcher, clock = _patch_clock()
    with patcher:
        handler.on_system_sleep()      # T0: sleeps first, nothing locked yet
        handler.on_screen_lock()       # T0+1s: lock notification arrives mid-sleep
        clock._next += timedelta(hours=5)  # the machine sleeps for real hours
        handler.on_system_wake()       # wakes, hours later, still locked
        handler.on_screen_unlock()     # user returns

    sleep_calls = handler.sync_engine.send_sleep_event.call_args_list
    lock_calls = handler.sync_engine.send_lock_event.call_args_list
    assert len(sleep_calls) == 1, sleep_calls
    assert len(lock_calls) == 1, (
        "no pre-sleep lock portion exists in this ordering -- only the real "
        "sleep span and the post-wake lock span"
    )

    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    wake_instant = t0 + timedelta(seconds=1) + timedelta(hours=5)

    assert sleep_calls[0].args[0] == t0
    assert lock_calls[0].args[0] == wake_instant, (
        "the post-wake lock span must start at WAKE, not at the earlier "
        "lock-notification instant -- anything else re-claims real sleep "
        "time as lock time too"
    )


def test_unlock_before_wake_is_authoritative_and_does_not_extend_a_bogus_span():
    """CRITICAL (Tudor's repro, 2026-10-08). The lock/unlock listener and the
    sleep/wake listener are independent OS notification threads with no
    ordering guarantee between them, so an unlock can be fully processed
    BEFORE its matching wake fires.

    Sequence: sleep 07:00, lock 07:01 (mid-sleep, defers via the flag),
    unlock 09:00 (processed first), wake 09:00 (processed second), lock
    12:00, unlock 12:30.

    Pre-fix, on_system_wake unconditionally reopened a lock span whenever the
    deferred-reopen flag was set -- with no way to know an unlock had ALREADY
    happened. That reopened span (starting ~09:00) then absorbed the 12:00
    lock via the keep-the-earliest-start rule, producing ONE five-and-a-half-
    hour lock span covering 09:00-12:30 -- 3 hours of real, unlocked work
    reported as locked.

    Correct behaviour: the unlock at 09:00 is authoritative. It must clear
    the deferred-reopen flag, so the wake that follows does NOT reopen a
    span, and the 09:00-12:00 gap is reported as neither asleep nor locked
    (i.e. ordinary, billable presence).
    """
    handler = _make_handler()
    patcher, clock = _patch_clock(datetime(2026, 10, 8, 7, 0, tzinfo=timezone.utc))
    with patcher:
        clock._next = datetime(2026, 10, 8, 7, 0, tzinfo=timezone.utc)
        handler.on_system_sleep()  # 07:00
        clock._next = datetime(2026, 10, 8, 7, 1, tzinfo=timezone.utc)
        handler.on_screen_lock()  # 07:01, mid-sleep -> defers
        clock._next = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)
        handler.on_screen_unlock()  # 09:00, processed BEFORE wake
        handler.on_system_wake()  # 09:00, processed AFTER unlock
        clock._next = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
        handler.on_screen_lock()  # 12:00
        clock._next = datetime(2026, 10, 8, 12, 30, tzinfo=timezone.utc)
        handler.on_screen_unlock()  # 12:30

    sleep_calls = handler.sync_engine.send_sleep_event.call_args_list
    lock_calls = handler.sync_engine.send_lock_event.call_args_list

    assert len(sleep_calls) == 1
    assert sleep_calls[0].args[0] == datetime(2026, 10, 8, 7, 0, tzinfo=timezone.utc)

    assert len(lock_calls) == 1, (
        f"the first (07:01-during-sleep) lock notification must produce NO "
        f"span of its own -- only the real 12:00 lock should: {lock_calls}"
    )
    assert lock_calls[0].args[0] == datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc), (
        "the lock span must start at the REAL 12:00 lock, never at the "
        "09:00 wake instant -- starting there would report 09:00-12:00 "
        "real work as locked"
    )


def test_lock_unlock_cycle_fully_inside_a_break_does_not_lose_its_span():
    """Mirrors test_the_resume_path_stops_reporting_the_cause_that_ended in
    test_tray_reason_survives_wake_and_unlock.py: the resume path takes an
    early return (staying paused) when the user manually paused or is on a
    break, but the lock span must still be flushed — exactly like the sleep
    span already is on that same early-return path."""
    handler = _make_handler()
    with handler._pause_state_lock:
        handler._user_paused = True
    patcher, _ = _patch_clock()
    with patcher:
        handler.on_screen_lock()
        handler.on_screen_unlock()

    handler.sync_engine.send_lock_event.assert_called_once()
