"""A dead tracker server must not be reported as a blind window tracker.

_is_window_tracker_stale reads `reachable` as "AW is reachable", per its own
docstring -- otherwise a None event age is just an AW outage. It was fed
_port_in_use(), and a corpse holding the port answers True, so an outage was
classified as a blind tracker: a force-restart burst plus a latched
_window_tracker_blind, which publishes a permissions story for a dead server.
"""

from unittest.mock import MagicMock

from src.aw_manager import AWManager


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
