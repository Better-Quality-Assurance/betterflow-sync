"""Regression for Bug A (audit finding A, #76): _reconcile_inproc_afk_flag.

The method lives on SyncCoordinator and runs every 60s via _tick_60s. It used
`self.coordinator.sync_engine` / `self.coordinator.aw_manager`, but
`self.coordinator` only exists on BetterFlowApp — so on SyncCoordinator it threw
`AttributeError: 'SyncCoordinator' object has no attribute 'coordinator'` every
60s and the reconcile never ran (it gates the idle-tracker watchdog + AFK
telemetry). It must use self.sync_engine / self.aw_manager.
"""
from unittest.mock import Mock

from src.main import SyncCoordinator


def _coord(afk_source, inproc_active=True):
    # Bypass the heavy __init__; the method only touches these two attributes.
    c = SyncCoordinator.__new__(SyncCoordinator)
    c.sync_engine = Mock()
    c.sync_engine.afk_source = afk_source
    c.sync_engine.inproc_afk_active = inproc_active
    c.aw_manager = Mock()
    return c


def test_reconcile_propagates_flag_without_attributeerror():
    """With a wired afk source, the flag is pushed to aw_manager — and crucially
    the call does not raise (the old self.coordinator.* threw every 60s)."""
    c = _coord(afk_source=object(), inproc_active=True)
    SyncCoordinator._reconcile_inproc_afk_flag(c)  # must not raise
    c.aw_manager.set_inproc_afk_active.assert_called_once_with(True)


def test_reconcile_noop_when_no_afk_source():
    """No in-process AFK source → early return, no flag write, no raise."""
    c = _coord(afk_source=None)
    SyncCoordinator._reconcile_inproc_afk_flag(c)
    c.aw_manager.set_inproc_afk_active.assert_not_called()
