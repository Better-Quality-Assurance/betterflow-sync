"""The ActivityWatch watchdog has to actually escalate.

Measured across the 47 agent logs uploaded on 2026-07-22/23:

    "ActivityWatch unreachable (1/2) — retrying next cycle"   9 times
    "ActivityWatch unreachable for N cycles — forcing ..."    0 times
    "Force-restarting tracker stack"                          0 times

Never once. Laszlo Fabian Raul's device had ActivityWatch dead for roughly four
hours on 2026-07-23 and the counter reached 1, one single time.

Two defects, both fixed here:

1. `_aw_unreachable_streak` counted CONSECUTIVE _do_sync ticks and was reset by
   any tick where capture was suppressed. Suppression is normal (outside working
   hours, break, private time), so on a device that alternates the counter never
   reaches 2 and the escalation is unreachable in practice. Replaced with "how
   long has it been unreachable while capture was ALLOWED", which cannot be
   reset by an unrelated suppressed tick.

2. The recovery was gated on `aw_manager.is_managing`, which is
   `bool(self._processes)` — false precisely when every component failed to
   start. That is the case where a rebuild is the ONLY thing that can help, and
   it was the one case that skipped it. Fabian's device: no component ever
   started, so `_processes` stayed empty and `force_restart` was unreachable.
"""

from unittest.mock import MagicMock

import pytest

from src.main import SyncCoordinator


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def coord():
    c = SyncCoordinator.__new__(SyncCoordinator)
    c._aw_unreachable_streak = 0
    c._aw_unreachable_since = None
    c._AW_UNREACHABLE_ERROR_THRESHOLD = 2
    c._AW_UNREACHABLE_ESCALATE_SECONDS = 180.0
    c.aw_manager = MagicMock()
    c.aw_manager.is_managing = False
    c.tray = MagicMock()
    return c


def test_escalates_after_the_grace_period(coord):
    clock = _Clock()
    assert coord._note_aw_unreachable(now=clock()) is False
    clock.advance(181)
    assert coord._note_aw_unreachable(now=clock()) is True, (
        "unreachable for longer than the grace period must escalate"
    )


def test_a_suppressed_tick_does_not_reset_the_clock(coord):
    # THE bug. A break, a private-time window or an out-of-hours tick used to
    # zero the counter, so the escalation never accumulated on a real device.
    clock = _Clock()
    coord._note_aw_unreachable(now=clock())
    clock.advance(100)
    coord._note_aw_capture_suppressed()          # normal, not a recovery
    clock.advance(100)
    assert coord._note_aw_unreachable(now=clock()) is True


def test_a_real_recovery_does_reset_the_clock(coord):
    clock = _Clock()
    coord._note_aw_unreachable(now=clock())
    clock.advance(170)
    coord._note_aw_reachable()                   # AW answered — genuinely fine
    clock.advance(170)
    assert coord._note_aw_unreachable(now=clock()) is False, (
        "a device that recovered must start its grace period over"
    )


def test_recovery_clears_the_error_state(coord):
    coord._note_aw_reachable()
    assert coord._aw_unreachable_since is None
    assert coord._aw_unreachable_streak == 0


def test_rebuild_is_attempted_even_when_nothing_is_managed(coord):
    # is_managing is bool(_processes) — false exactly when every component
    # failed to start, which is when a rebuild is the only thing that can help.
    coord.aw_manager.is_managing = False
    clock = _Clock()
    coord._note_aw_unreachable(now=clock())
    clock.advance(181)
    coord._escalate_aw_unreachable(now=clock())

    coord.aw_manager.force_restart.assert_called_once()
    coord.tray.set_state.assert_called_once()


def test_rebuild_is_attempted_when_components_are_managed_too(coord):
    coord.aw_manager.is_managing = True
    clock = _Clock()
    coord._note_aw_unreachable(now=clock())
    clock.advance(181)
    coord._escalate_aw_unreachable(now=clock())

    coord.aw_manager.force_restart.assert_called_once()


def test_a_failing_rebuild_still_surfaces_the_error(coord):
    # force_restart raising must not swallow the tray escalation — otherwise a
    # broken recovery path hides the fault it was trying to report.
    coord.aw_manager.force_restart.side_effect = RuntimeError("cannot rebuild")
    clock = _Clock()
    coord._note_aw_unreachable(now=clock())
    clock.advance(181)
    coord._escalate_aw_unreachable(now=clock())

    coord.tray.set_state.assert_called_once()


def test_bucket_fetch_escalation_rebuilds_without_the_is_managing_gate(coord):
    # Sibling of _escalate_aw_unreachable: AW answers /info but 503s on
    # /buckets/, i.e. a half-hung bf-data-service. Same gate, same reason for
    # removing it — bool(_processes) is false precisely when nothing started.
    coord.aw_manager.is_managing = False
    coord._aw_buckets_failed_streak = 0
    coord.error_reporter = None

    for _ in range(coord._AW_UNREACHABLE_ERROR_THRESHOLD):
        coord._handle_aw_bucket_failure()

    coord.aw_manager.force_restart.assert_called_once()
    coord.tray.set_state.assert_called_once()


def test_bucket_fetch_escalation_survives_a_failing_rebuild(coord):
    coord.aw_manager.is_managing = False
    coord._aw_buckets_failed_streak = 0
    coord.error_reporter = None
    coord.aw_manager.force_restart.side_effect = RuntimeError("cannot rebuild")

    for _ in range(coord._AW_UNREACHABLE_ERROR_THRESHOLD):
        coord._handle_aw_bucket_failure()

    coord.tray.set_state.assert_called_once()
