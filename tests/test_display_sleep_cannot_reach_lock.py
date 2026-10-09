"""Display sleep must never be able to open or extend a lock_time span.

betterflow-sync#271: an earlier version of this PR routed
NSWorkspaceScreensDidSleepNotification (display-off from inactivity) to a
dedicated ``on_display_sleep`` handler that, behind the capabilities gate,
delegated straight to ``on_screen_lock`` -- reusing its span-open logic
instead of ``on_system_sleep``'s. That has a Critical: ``on_screen_lock``
opens ``_lock_start``, but the matching wake notification (ScreensDidWake)
is combined with the normal wake path and dispatches to ``on_system_wake``,
which only ever clears/reopens ``_lock_start`` when ``_locked_while_asleep``
was set -- and that flag is set exclusively by ``on_system_sleep``'s own
boundary flush, which never ran here (we went straight to ``on_screen_lock``
instead of ``on_system_sleep``). So a display-off that is never followed by
a real system sleep leaves ``_lock_start`` open indefinitely. A later REAL
lock then hits the "a _lock_start is already pending" branch and silently
keeps the stale display-off timestamp instead of the real lock instant.
Reported shape: display-off 10:00 -> mouse moved 10:02 (display wakes, nothing
else happens) -> hours of real work -> lid closes 18:00 -> lock_time
10:00-18:00 is uploaded, zeroing a full day of real work.

Per Tudor's 2026-10-09 decision, that whole feature (item 3) is OUT of this
PR -- reverted in full (see the revert of
"feat(sync): display sleep is lock-like behind the capabilities gate").
This file pins that it stays out: no path from a display-sleep notification
can reach ``on_screen_lock`` or set ``_lock_start``, so the stale-timestamp
Critical above cannot occur, regardless of which commit's wiring is under
test.

``_simulate_screens_did_sleep`` below deliberately does not hardcode which
SystemEventHandler method NSWorkspaceScreensDidSleepNotification dispatches
to -- it looks for a dedicated ``on_display_sleep`` entry point and falls
back to ``on_system_sleep`` (today's actual wiring; see
``_start_macos_power_listener`` in src/system_events.py, which routes BOTH
NSWorkspaceWillSleepNotification and NSWorkspaceScreensDidSleepNotification
to the same shared ``on_sleep`` callback). This is what lets the same test
file prove the regression on the pre-revert commit (where
``on_display_sleep`` exists and reaches ``on_screen_lock``) and prove the
fix on the post-revert commit (where it does not exist, so the fallback to
``on_system_sleep`` -- which never touches ``_lock_start`` -- is exercised
instead), without two copies of the test.

Proof-of-failure handshake: run against a copy of f63b62c (HEAD of this PR
before the revert) --
    test_no_display_sleep_entry_point_exists_on_the_handler FAILS
        (on_display_sleep is defined there)
    test_display_sleep_then_wake_then_real_lock_unlock_bills_only_the_real_lock
        FAILS (lock_time is sent starting at the display-off instant,
        10:00:00, not the real lock at 18:00:00)
Run against this commit (post-revert): both PASS.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.system_event_handler import SystemEventHandler


class _Clock:
    """Monotonically-advancing fake clock, one second per call by default, so
    every ``datetime.now(timezone.utc)`` call inside the handler gets a
    distinct, ordered, predictable timestamp. ``_next`` can be reassigned
    directly to fast-forward past a gap the test does not need call-by-call
    control over (hours of uneventful real work between the display-off and
    the real lock)."""

    def __init__(self, start: datetime):
        self._next = start

    def now(self, *args, **kwargs):
        value = self._next
        self._next = self._next + timedelta(seconds=1)
        return value


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


class _DatetimeProxy:
    def __init__(self, clock: _Clock):
        self.now = clock.now


def _patch_clock(start=None):
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = _Clock(start)
    return patch("src.system_event_handler.datetime", _DatetimeProxy(clock)), clock


def _simulate_screens_did_sleep(handler: SystemEventHandler) -> None:
    """Dispatch to whatever method NSWorkspaceScreensDidSleepNotification
    actually calls on this handler today -- never hardcode a hypothesis
    about which one that is. See module docstring."""
    target = getattr(handler, "on_display_sleep", None)
    (target or handler.on_system_sleep)()


def test_no_display_sleep_entry_point_exists_on_the_handler():
    """Structural guard: no dedicated display-sleep callback may exist on the
    handler. Fails on the pre-revert commit, which defines on_display_sleep."""
    handler = _make_handler()
    assert not hasattr(type(handler), "on_display_sleep"), (
        "a dedicated on_display_sleep entry point exists again -- re-audit "
        "whether it can reach on_screen_lock/_lock_start "
        "(betterflow-sync#271's Critical) before resurrecting item 3"
    )


def test_display_sleep_then_wake_then_real_lock_unlock_bills_only_the_real_lock():
    """display-off 10:00 -> wake 10:00:01 with no lock ever opened -> hours of
    real work -> a genuine lock+unlock at 18:00. The uploaded lock_time span
    must start at the real lock, never at the display-off instant."""
    handler = _make_handler(lock_time_capable=True)
    patcher, clock = _patch_clock(start=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc))
    with patcher:
        # 10:00:00 -- display-off (pre-revert: routed to on_display_sleep ->
        # on_screen_lock, opening _lock_start here; post-revert: routed to
        # on_system_sleep, which only ever touches _sleep_start).
        _simulate_screens_did_sleep(handler)

        # 10:00:01 -- display/system wakes (ScreensDidWake and DidWake share
        # one dispatch to on_system_wake on every commit). No real lock or
        # unlock has happened at all yet.
        handler.on_system_wake()
        handler.sync_engine.send_lock_event.assert_not_called(), (
            "no lock_time span may be reported before any real lock exists"
        )

        # Hours of real work pass uneventfully. Fast-forward explicitly so
        # the real lock is unambiguously later than the display-off, the
        # same shape as the reported incident (10:00 display-off, 18:00 lid
        # close).
        real_lock_instant = datetime(2026, 1, 1, 18, 0, 0, tzinfo=timezone.utc)
        clock._next = real_lock_instant

        # 18:00:00 -- the REAL lock.
        handler.on_screen_lock()
        handler.sync_engine.send_lock_event.assert_not_called(), (
            "no span may be sent while still locked"
        )

        # 18:00:01 -- the REAL unlock.
        handler.on_screen_unlock()

    handler.sync_engine.send_lock_event.assert_called_once()
    args, _kwargs = handler.sync_engine.send_lock_event.call_args
    sent_start = args[0]
    assert sent_start == real_lock_instant, (
        f"lock_time started at {sent_start.isoformat()}, not the real lock "
        f"at {real_lock_instant.isoformat()} -- a display-sleep-opened lock "
        "span survived into the real lock/unlock pair"
    )
