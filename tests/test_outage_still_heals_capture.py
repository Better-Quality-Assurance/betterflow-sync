"""A network outage suspends UPLOAD, not capture — so the local tracker stack
must still be health-checked and self-healed during one.

Before this fix `_do_sync` returned the instant `paused_by_network` was True,
BEFORE the restart/rebuild path. `suspend_upload()` keeps the trackers recording
and queueing for the whole outage (often hours), so a tracker that crashed — or,
like Fabian's device, never started at all — recorded nothing until the network
came back. That is the exact "device silently records nothing" failure mode the
1.5.118 tracking-blindspot work exists to close, reintroduced by the one path
nobody re-checked: an outage.

The escalation tests drive `_note_aw_unreachable` / `_escalate_aw_unreachable`
directly and never through `_do_sync`, so this branch had zero coverage.
"""

import threading
import time
from unittest.mock import MagicMock, PropertyMock, patch

import src.main as main_mod
from src.main import SyncCoordinator


def _coord() -> SyncCoordinator:
    c = SyncCoordinator.__new__(SyncCoordinator)
    c._DO_SYNC_DEADLINE = 150
    c._RESTART_LOOP_ALERT_THRESHOLD = 999
    c._restart_loop_escalated = False
    c._aw_unreachable_streak = 0
    c._aw_unreachable_since = None
    c._AW_UNREACHABLE_ESCALATE_SECONDS = 180.0
    c._sync_takeover_lock = threading.Lock()
    c._sync_holder = None
    c._sync_started_at = None
    c.error_reporter = None
    c.aw = MagicMock()
    c.aw_manager = MagicMock()
    c.aw_manager.is_managing = False
    c.tray = MagicMock()
    c.queue = MagicMock()
    c.sync_engine = MagicMock()
    c.sync_engine.is_private = False
    c.break_mgr = MagicMock()
    c.break_mgr.is_on_break = False
    c.config = MagicMock()
    c.config.working_hours.allows.return_value = True
    return c


def _drive_do_sync(c: SyncCoordinator):
    lock = MagicMock()
    with patch.object(SyncCoordinator, "paused_by_network", new_callable=PropertyMock, return_value=True), \
         patch.object(SyncCoordinator, "idle_paused", new_callable=PropertyMock, return_value=False), \
         patch.object(main_mod.threading, "Timer", return_value=MagicMock()), \
         patch.object(c, "_acquire_sync_slot", return_value=lock):
        c._do_sync()
    return lock


# ---- the outage branch of _do_sync must run capture health ----

def test_outage_branch_invokes_capture_health_and_skips_upload():
    c = _coord()
    lock = MagicMock()
    with patch.object(SyncCoordinator, "paused_by_network", new_callable=PropertyMock, return_value=True), \
         patch.object(SyncCoordinator, "idle_paused", new_callable=PropertyMock, return_value=False), \
         patch.object(main_mod.threading, "Timer", return_value=MagicMock()), \
         patch.object(c, "_acquire_sync_slot", return_value=lock), \
         patch.object(c, "_monitor_capture_health") as health:
        c._do_sync()

    health.assert_called_once()
    # Upload cannot land offline — sync() is still skipped.
    c.sync_engine.sync.assert_not_called()
    lock.release.assert_called_once()


def test_a_dead_tracker_during_an_outage_is_rebuilt():
    # Fabian's exact shape: nothing ever started (is_managing False), AW dead,
    # inside working hours, unreachable past the grace window. Pre-fix the outage
    # branch returned before any of this, so the rebuild never fired.
    c = _coord()
    c.aw.is_running.return_value = False
    # Already unreachable for longer than the 180s grace. Derived from the real
    # monotonic clock, NOT a small constant: _note_aw_unreachable compares against
    # time.monotonic(), so a literal 0.001 only looks "200s ago" on a machine with
    # high uptime. On a fresh CI runner monotonic() is ~95s, making elapsed < 180
    # and the escalation silently not fire (this test was green locally, red on CI).
    c._aw_unreachable_since = time.monotonic() - 200.0

    _drive_do_sync(c)

    c.aw_manager.force_restart.assert_called_once()


# ---- _monitor_capture_health itself (the extracted, shared helper) ----

def test_monitor_returns_true_and_notes_recovery_when_aw_answers():
    c = _coord()
    c.aw.is_running.return_value = True

    assert c._monitor_capture_health() is True
    assert c._aw_unreachable_since is None  # recovery recorded


def test_monitor_skips_sync_and_escalates_a_real_outage():
    c = _coord()
    c.aw.is_running.return_value = False
    c._aw_unreachable_since = time.monotonic() - 200.0  # past the 180s grace (clock-derived)

    assert c._monitor_capture_health() is False
    c.aw_manager.force_restart.assert_called_once()


def test_monitor_does_not_escalate_or_rebuild_out_of_hours():
    # Out of hours the trackers are down on purpose; escalating would rebuild a
    # stack meant to be off and flip the tray to an error every night.
    c = _coord()
    c.config.working_hours.allows.return_value = False
    c.aw.is_running.return_value = False

    assert c._monitor_capture_health() is True  # falls through to sync() online
    c.aw_manager.force_restart.assert_not_called()


def test_monitor_restarts_managed_trackers():
    c = _coord()
    c.aw_manager.is_managing = True
    c.aw_manager.stale_restart_count.return_value = 0
    c.aw.is_running.return_value = True

    c._monitor_capture_health()

    c.aw_manager.restart_if_needed.assert_called_once()
