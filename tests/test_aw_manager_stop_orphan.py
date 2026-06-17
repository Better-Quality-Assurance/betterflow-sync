"""Stopping must terminate the watchers WE started, even in external mode.

Root cause of the recurring "idle frozen / hours undercounted after update"
(furdui.iancu, 2026-06-17): on every launch after the first, the persistent
bf-data-service is reused ("using external instance") so ``_using_external`` is
True. The old ``stop()`` early-returned in that case and never terminated the
bf-idle-tracker IT started, so an app quit / self-update relaunch left it
orphaned. Two trackers then posted to the same AFK bucket → duplicate,
overlapping events + apparent staleness. Across the log: 88 watcher starts, 2
stops.
"""

from unittest.mock import MagicMock

from src.aw_manager import AWManager


def _alive_proc():
    proc = MagicMock()
    proc.poll.return_value = None  # alive
    return proc


def test_stop_terminates_watchers_when_using_external_server():
    mgr = AWManager()
    mgr._using_external = True  # attached to a shared/persistent server
    window = _alive_proc()
    idle = _alive_proc()
    # In external mode the server is NOT in _processes — only our watchers are.
    mgr._processes = {"bf-window-tracker": window, "bf-idle-tracker": idle}

    mgr.stop()

    assert window.terminate.called, "window watcher we started must be terminated"
    assert idle.terminate.called, "idle tracker we started must be terminated (no orphan)"
    assert not mgr._processes, "process table cleared after stop"


def test_stop_is_noop_with_no_processes():
    mgr = AWManager()
    mgr._using_external = True
    mgr._processes = {}
    # Must not raise.
    mgr.stop()


def test_update_exit_stops_trackers():
    """_flush_before_update_exit must terminate trackers before os._exit(0)."""
    from src.update_handler import UpdateHandler

    handler = UpdateHandler.__new__(UpdateHandler)
    handler.coordinator = MagicMock()

    handler._flush_before_update_exit()

    handler.coordinator.aw_manager.stop.assert_called_once()
