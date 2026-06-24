"""Watchdog restarts a BLIND window tracker — one alive but emitting ZERO events.

The window-stale check only restarted when _get_latest_window_event_age() returned
a number greater than the threshold. But that helper returns None when the window
bucket is empty — i.e. a tracker that's alive but has never emitted anything (blind
from launch). So a totally-dead window tracker was never restarted: window data
stayed frozen while AFK/input kept flowing (Sachi, win32, 2026-06-24 — the server
flagged window_ingest_stalled with no window events at all).

Fix: treat "no events, well past a launch grace, while AW is reachable" as stale
too. Guards: the launch grace avoids restarting a just-launched tracker before it
emits; the reachability check avoids mistaking an AW outage (also age None) for a
blind tracker.
"""

import time
from unittest.mock import MagicMock

from src.aw_manager import STALE_THRESHOLD, WINDOW_BLIND_GRACE, AWManager


def _mgr(window_age, running_for, reachable=True):
    mgr = AWManager()
    mgr._using_external = False
    server = MagicMock()
    server.poll.return_value = None
    window = MagicMock()
    window.poll.return_value = None
    idle = MagicMock()
    idle.poll.return_value = None
    mgr._processes = {
        "bf-data-service": server,
        "bf-window-tracker": window,
        "bf-idle-tracker": idle,
    }
    mgr._get_binaries_dir = MagicMock(return_value="/tmp/bin")
    mgr._start_component = MagicMock()
    mgr._get_latest_window_event_age = MagicMock(return_value=window_age)
    mgr._get_latest_afk_event_age = MagicMock(return_value=5)  # afk fresh → idle block no-op
    mgr._port_in_use = MagicMock(return_value=reachable)
    mgr._component_started_at = (
        {} if running_for is None
        else {"bf-window-tracker": time.monotonic() - running_for}
    )
    return mgr, window


def _restarted(mgr, window):
    return window.terminate.called and (
        ("bf-window-tracker",) in [c.args[:1] for c in mgr._start_component.call_args_list]
    )


# --- pure decision logic ---------------------------------------------------- #

def test_is_window_tracker_stale_decision():
    m = AWManager()
    assert m._is_window_tracker_stale(age=5, running_for=600, reachable=True) is False
    assert m._is_window_tracker_stale(age=STALE_THRESHOLD + 1, running_for=600, reachable=True) is True
    # blind: no events, past grace, AW reachable
    assert m._is_window_tracker_stale(age=None, running_for=WINDOW_BLIND_GRACE + 1, reachable=True) is True
    # just launched: no events yet, within grace
    assert m._is_window_tracker_stale(age=None, running_for=10, reachable=True) is False
    # AW unreachable: None may be an outage, not a blind tracker
    assert m._is_window_tracker_stale(age=None, running_for=600, reachable=False) is False
    # unknown launch time → can't claim blind
    assert m._is_window_tracker_stale(age=None, running_for=None, reachable=True) is False


# --- watchdog integration --------------------------------------------------- #

def test_blind_window_tracker_is_restarted():
    mgr, window = _mgr(window_age=None, running_for=WINDOW_BLIND_GRACE + 120, reachable=True)
    mgr.restart_if_needed()
    assert _restarted(mgr, window), "a blind window tracker (zero events past grace) must be restarted"


def test_blind_within_launch_grace_not_restarted():
    mgr, window = _mgr(window_age=None, running_for=10, reachable=True)
    mgr.restart_if_needed()
    assert not window.terminate.called, "a just-launched tracker must get its grace before restart"


def test_no_events_but_aw_unreachable_not_restarted():
    mgr, window = _mgr(window_age=None, running_for=WINDOW_BLIND_GRACE + 120, reachable=False)
    mgr.restart_if_needed()
    assert not window.terminate.called, "age None during an AW outage is not a blind tracker"


def test_frozen_window_tracker_still_restarted():
    # Pre-existing behavior must be preserved: events exist but stale.
    mgr, window = _mgr(window_age=STALE_THRESHOLD + 10, running_for=WINDOW_BLIND_GRACE + 120)
    mgr.restart_if_needed()
    assert _restarted(mgr, window)


def test_fresh_window_tracker_not_restarted():
    mgr, window = _mgr(window_age=5, running_for=WINDOW_BLIND_GRACE + 120)
    mgr.restart_if_needed()
    assert not window.terminate.called
