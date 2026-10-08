"""A macOS screen lock must be as visible on the server as display sleep is.

Before this change, ``on_screen_lock``/``on_screen_unlock`` called
``sync_engine.pause()``/``resume()`` — exactly like system sleep does — but,
unlike sleep, uploaded NOTHING. Credit already stopped (pause() is what stops
it), but the server had no way to tell a lock apart from a crashed or quit
agent: both looked like silence.

The fix reuses the exact sleep_time pipeline (``sync_engine.send_sleep_event``)
with an optional ``reason="lock"`` tag, because internal-tool2's
``AgentEvent::inferEventType`` keys the exclusion on ``bucket_type ==
"sleep_time"`` alone and never inspects ``data`` — so no server change is
needed for the exclusion to apply; the tag only makes the raw event
forensically distinguishable from genuine sleep.

The hard part is the overlap: a lock commonly starts BEFORE a system sleep (the
screen locks on an idle timeout well before the display/system sleep timeout)
and macOS does not auto-unlock on wake, so the lock persists AFTER the wake
too. Sending an independent [lock_start, unlock] span alongside the existing
[sleep_start, wake] span would double-count the overlap, because the server
SUMS each STATEFUL_EVENT_TYPES span's duration rather than merging intervals
(internal-tool2 AgentEventProcessor::updateSessionTiming). So the lock span is
split at the sleep boundary and reopened at wake — the pre-sleep portion is
flushed when sleep begins, and a fresh span starts at the wake instant,
covering only the time after wake until the user actually unlocks.
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


def test_lock_then_unlock_uploads_a_single_lock_reason_span():
    """The basic case: a coffee-break lock with no sleep involved at all."""
    handler = _make_handler()
    patcher, _ = _patch_clock()
    with patcher:
        handler.on_screen_lock()
        handler.sync_engine.send_sleep_event.assert_not_called(), (
            "no span may be sent while still locked"
        )
        handler.on_screen_unlock()

    handler.sync_engine.send_sleep_event.assert_called_once()
    args, kwargs = handler.sync_engine.send_sleep_event.call_args
    assert kwargs.get("reason") == "lock"
    assert args[0] == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_app_quit_while_locked_sends_no_span():
    """No unlock ever arrives (process killed/quit while locked) — acceptable:
    nothing was ever flushed to begin with, matching how a sleep with no wake
    behaves identically today."""
    handler = _make_handler()
    patcher, _ = _patch_clock()
    with patcher:
        handler.on_screen_lock()

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

    args, kwargs = handler.sync_engine.send_sleep_event.call_args
    assert args[0] == datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc), (
        "the SECOND lock notification must not have reset the start"
    )


def test_sleep_only_with_no_lock_is_unaffected():
    """Regression guard: a sleep with no lock involved must produce the exact
    same call shape as before this change — no ``reason`` kwarg at all."""
    handler = _make_handler()
    patcher, _ = _patch_clock()
    with patcher:
        handler.on_system_sleep()
        handler.on_system_wake()

    handler.sync_engine.send_sleep_event.assert_called_once()
    args, kwargs = handler.sync_engine.send_sleep_event.call_args
    assert kwargs == {}, "a sleep-only span must carry no reason tag"
    assert len(args) == 1, "wake flushes with no explicit end, exactly as before"


def test_lock_before_sleep_splits_into_three_contiguous_non_overlapping_spans():
    """The dominant real-world ordering: the screen locks on its own (shorter)
    idle timeout, then later the system sleeps while already locked, then
    wakes (still locked, since macOS never auto-unlocks), then the user
    finally returns and unlocks.

    Must produce THREE spans that exactly tile the elapsed time with no gap
    and no overlap: [lock, sleep) as "lock", [sleep, wake) as sleep (no
    reason), [wake, unlock) as "lock" again.
    """
    handler = _make_handler()
    patcher, _ = _patch_clock()
    with patcher:
        handler.on_screen_lock()       # T0: lock begins
        handler.on_system_sleep()      # T1: system sleeps while locked
        handler.on_system_wake()       # T2: wakes, still locked
        handler.on_screen_unlock()     # T3: user returns

    calls = handler.sync_engine.send_sleep_event.call_args_list
    assert len(calls) == 3, calls

    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=1)
    t2 = t1 + timedelta(seconds=1)

    pre_sleep_lock = calls[0]
    assert pre_sleep_lock.args[0] == t0
    assert pre_sleep_lock.args[1] == t1, "closed exactly AT the sleep boundary"
    assert pre_sleep_lock.kwargs.get("reason") == "lock"

    sleep_span = calls[1]
    assert sleep_span.args[0] == t1, "no gap: starts exactly where the lock span ended"
    assert sleep_span.kwargs == {}, "the true sleep portion carries no lock reason"

    post_wake_lock = calls[2]
    assert post_wake_lock.args[0] == t2, "no gap: starts exactly where wake fired"
    assert post_wake_lock.kwargs.get("reason") == "lock"

    # The defining property: total exclusion time reported is the real elapsed
    # time, counted exactly once — not twice via an independent overlapping
    # lock span, and not with a gap at either sleep boundary.
    assert pre_sleep_lock.args[1] == sleep_span.args[0]


def test_lock_notification_racing_just_after_sleep_still_avoids_overlap():
    """The OS can, on some sleep paths, deliver the screenIsLocked
    notification concurrently with or just after NSWorkspaceWillSleep rather
    than before it. Whichever arrives first, the result must still be two
    non-overlapping spans, never three and never overlapping ones."""
    handler = _make_handler()
    patcher, _ = _patch_clock()
    with patcher:
        handler.on_system_sleep()      # T0: sleeps first, nothing locked yet
        handler.on_screen_lock()       # T1: lock notification arrives mid-sleep
        handler.on_system_wake()       # T2: wakes, still locked
        handler.on_screen_unlock()     # T3: user returns

    calls = handler.sync_engine.send_sleep_event.call_args_list
    assert len(calls) == 2, (
        "no pre-sleep lock portion exists in this ordering — only the real "
        "sleep span and the post-wake lock span"
    )

    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    # on_screen_lock consumes no clock tick here: it only sets a flag
    # (``_locked_while_asleep``) rather than opening a span, since the sleep
    # span already open covers this exclusion. So the SECOND clock tick is
    # the one on_system_wake uses to reopen the lock span.
    t1 = t0 + timedelta(seconds=1)

    sleep_span, post_wake_lock = calls
    assert sleep_span.args[0] == t0
    assert sleep_span.kwargs == {}
    assert post_wake_lock.args[0] == t1
    assert post_wake_lock.kwargs.get("reason") == "lock"


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

    handler.sync_engine.send_sleep_event.assert_called_once()
    assert handler.sync_engine.send_sleep_event.call_args.kwargs.get("reason") == "lock"
