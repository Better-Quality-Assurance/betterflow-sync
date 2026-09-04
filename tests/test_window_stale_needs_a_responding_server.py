"""A dead tracker server must not be reported as a blind window tracker.

_is_window_tracker_stale reads `reachable` as "AW is reachable", per its own
docstring -- otherwise a None event age is just an AW outage. It was fed
_port_in_use(), and a corpse holding the port answers True, so an outage was
classified as a blind tracker: a force-restart burst plus a latched
_window_tracker_blind, which publishes a permissions story for a dead server.
"""

import time
from unittest.mock import MagicMock

from src.aw_manager import WINDOW_BLIND_GRACE, AWManager


def _mgr(*, port_held, answers):
    m = AWManager()
    m._port_in_use = MagicMock(return_value=port_held)
    m._server_responding = MagicMock(return_value=answers)
    return m


def test_a_corpse_holding_the_port_is_not_reachable():
    m = _mgr(port_held=True, answers=False)
    assert m._window_tracker_reachable() is False


def test_a_responding_server_is_reachable():
    """Allowance: the ordinary healthy case must still be reachable, or every
    quiet window tracker stops being restarted."""
    m = _mgr(port_held=True, answers=True)
    assert m._window_tracker_reachable() is True


def test_a_free_port_is_not_reachable():
    m = _mgr(port_held=False, answers=False)
    assert m._window_tracker_reachable() is False


def test_a_held_but_dead_port_does_not_force_restart_the_watchers():
    """The divergent case neither existing fixture can express.

    tests/test_aw_manager_window_blind.py::_mgr feeds ONE value to both
    _port_in_use and _server_responding, so no test drives the real
    _restart_if_needed_locked with the shape this file exists for: the port
    is HELD (a corpse), but the server does not ANSWER. That must read as an
    AW outage, not a blind window tracker -- _is_window_tracker_stale's own
    reachable=False branch exists precisely to avoid force-restarting a
    watcher into a dead server. Reverting _window_tracker_reachable to ask
    _port_in_use() instead of _server_responding() must redden this.
    """
    mgr = AWManager()
    mgr._using_external = True
    mgr._port_in_use = MagicMock(return_value=True)
    mgr._server_responding = MagicMock(return_value=False)
    mgr._get_binaries_dir = MagicMock(return_value="/tmp/bin")
    mgr._start_component = MagicMock()
    mgr._get_latest_window_event_age = MagicMock(return_value=None)
    mgr._get_latest_afk_event_age = MagicMock(return_value=5)  # afk fresh -> idle block no-op

    window = MagicMock()
    window.poll.return_value = None
    idle = MagicMock()
    idle.poll.return_value = None
    mgr._processes = {
        "bf-window-tracker": window,
        "bf-idle-tracker": idle,
    }
    mgr._component_started_at = {
        "bf-window-tracker": time.monotonic() - (WINDOW_BLIND_GRACE + 120),
    }

    mgr.restart_if_needed()

    assert not window.terminate.called, (
        "a corpse holding the port must not be misread as a blind window "
        "tracker and force-restarted"
    )
    assert not idle.terminate.called
