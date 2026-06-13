"""Watchdog restarts a HUNG idle/AFK tracker (Alexandru, 2026-06-12).

bf-idle-tracker can stay alive (process running) while emitting no new AFK
events — e.g. it hangs. The window + input watchers keep running, so the user
goes on working, but "Active time" (computed from the AFK not-afk signal)
freezes. The existing stale-check only covered bf-window-tracker; these tests
pin that a hung idle tracker is detected (AFK stale while window fresh) and
restarted, and that we DON'T churn-restart it during legitimate idle/sleep.
"""

from unittest.mock import MagicMock

from src.aw_manager import STALE_THRESHOLD, AWManager


def _make_manager(afk_age, window_age, idle_alive=True):
    """An AWManager with mocked watchers and event-age probes."""
    mgr = AWManager()
    mgr._using_external = False

    server = MagicMock()
    server.poll.return_value = None  # alive
    window = MagicMock()
    window.poll.return_value = None  # alive
    idle = MagicMock()
    idle.poll.return_value = None if idle_alive else 1
    mgr._processes = {
        "bf-data-service": server,
        "bf-window-tracker": window,
        "bf-idle-tracker": idle,
    }

    mgr._get_binaries_dir = MagicMock(return_value="/tmp/bin")
    mgr._start_component = MagicMock()
    mgr.check_health = MagicMock(return_value=True)
    mgr._get_latest_afk_event_age = MagicMock(return_value=afk_age)
    mgr._get_latest_window_event_age = MagicMock(return_value=window_age)
    return mgr, idle


def _restarted(mgr, idle_proc):
    return (
        idle_proc.terminate.called
        and (
            ("bf-idle-tracker",) in [c.args[:1] for c in mgr._start_component.call_args_list]
        )
    )


def test_restarts_idle_tracker_when_afk_stale_but_window_fresh():
    # Alexandru's exact signature: AFK silent for 30 min, window fresh.
    mgr, idle = _make_manager(afk_age=1800, window_age=5)

    mgr.restart_if_needed()

    assert _restarted(mgr, idle), "a hung idle tracker (AFK stale, user active) must be restarted"


def test_does_not_restart_when_both_watchers_are_stale():
    # Legitimate full-system idle / sleep: both paused → don't churn-restart.
    mgr, idle = _make_manager(afk_age=1800, window_age=1800)

    mgr.restart_if_needed()

    assert not idle.terminate.called, "no restart when the user is genuinely idle"


def test_does_not_restart_when_afk_is_fresh():
    mgr, idle = _make_manager(afk_age=5, window_age=5)

    mgr.restart_if_needed()

    assert not idle.terminate.called, "healthy AFK watcher is left alone"


def test_does_not_restart_disabled_idle_tracker():
    mgr, idle = _make_manager(afk_age=1800, window_age=5)
    mgr._disabled_components.add("bf-idle-tracker")

    mgr.restart_if_needed()

    assert not idle.terminate.called, "a disabled component is never restarted"


def test_boundary_just_over_threshold_restarts():
    mgr, idle = _make_manager(afk_age=STALE_THRESHOLD + 1, window_age=STALE_THRESHOLD)

    mgr.restart_if_needed()

    assert _restarted(mgr, idle), "AFK age just over threshold while window at/under it → restart"
