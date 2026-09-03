"""In external-server mode the watchdog must still supervise OUR watchers.

After the first launch the persistent bf-data-service is reused, so
``_using_external`` is True forever on that machine. The watchdog used to be
disabled in that mode two ways: ``is_managing`` returned False (so
``restart_if_needed`` was never called) and ``_restart_if_needed_locked``
returned early. A blind/orphaned bf-idle-tracker therefore never self-healed —
it took a manual app restart (furdui.iancu, 2026-06-17). These pin that the
watchers are supervised in external mode and that orphans are reaped.
"""

import os
from unittest.mock import MagicMock

import src.aw_manager as awm
from src.aw_manager import AWManager


def test_is_managing_true_in_external_mode_when_watchers_owned():
    mgr = AWManager()
    mgr._using_external = True
    mgr._processes = {"bf-idle-tracker": MagicMock()}
    assert mgr.is_managing, "watchers are ours even on a shared server — watchdog must run"


def test_restarts_stale_idle_tracker_in_external_mode():
    """The exact recurring failure: external server up, idle tracker blind."""
    mgr = AWManager()
    mgr._using_external = True
    mgr._port_in_use = MagicMock(return_value=True)  # external server healthy
    # ...and healthy means it ANSWERS, not merely that it holds the socket
    # (#246). The comment above always claimed this; only the port was stubbed,
    # because a held socket was all the code read.
    mgr._server_responding = MagicMock(return_value=True)
    window = MagicMock()
    window.poll.return_value = None
    idle = MagicMock()
    idle.poll.return_value = None
    # External mode: server is NOT in _processes, only our watchers are.
    mgr._processes = {"bf-window-tracker": window, "bf-idle-tracker": idle}
    mgr._get_binaries_dir = MagicMock(return_value="/tmp/bin")
    mgr._start_component = MagicMock()
    mgr.check_health = MagicMock(return_value=True)
    mgr._get_latest_afk_event_age = MagicMock(return_value=1800)  # 30 min silent
    mgr._get_latest_window_event_age = MagicMock(return_value=5)  # user active

    mgr.restart_if_needed()

    assert idle.terminate.called, "blind idle tracker must be restarted in external mode"
    assert ("bf-idle-tracker",) in [
        c.args[:1] for c in mgr._start_component.call_args_list
    ]


def test_external_server_vanished_falls_back_to_own():
    mgr = AWManager()
    mgr._using_external = True
    mgr._port_in_use = MagicMock(return_value=False)  # external server gone
    mgr._processes = {"bf-idle-tracker": MagicMock()}
    mgr._start_locked = MagicMock(return_value=True)

    mgr.restart_if_needed()

    assert mgr._start_locked.called, "must start our own stack when external server disappears"
    assert mgr._using_external is False


def test_reaper_kills_orphans_only(monkeypatch):
    mgr = AWManager()
    managed = MagicMock()
    managed.poll.return_value = None
    managed.pid = 1111
    mgr._processes = {"bf-idle-tracker": managed}

    monkeypatch.setattr(awm, "_resolve_binary_path", lambda d, n: "/x/bf-idle-tracker")
    monkeypatch.setattr(awm, "_find_pids_by_path", lambda p: [1111, 2222, os.getpid()])
    killed = []
    monkeypatch.setattr(awm, "_terminate_pid", lambda pid, **k: killed.append(pid))

    mgr._reap_orphan_processes("bf-idle-tracker", "/x")

    assert killed == [2222], "kills the orphan only — not our managed PID, not ourselves"


def test_force_restart_reaps_hung_server_then_starts_fresh(monkeypatch):
    """A hung-but-listening server (port held, HTTP dead) must be reclaimed:
    stop watchers, reap stray server+watcher processes, then start fresh."""
    mgr = AWManager()
    mgr._using_external = True  # attached to what we now believe is a dead server
    mgr._get_binaries_dir = MagicMock(return_value="/tmp/bin")

    reaped = []
    monkeypatch.setattr(mgr, "_stop_locked", lambda: reaped.append("stopped"))
    monkeypatch.setattr(
        mgr, "_reap_orphan_processes",
        lambda name, d: reaped.append(name),
    )
    mgr._start_locked = MagicMock(return_value=True)

    ok = mgr.force_restart(reason="server unreachable")

    assert ok is True
    assert reaped[0] == "stopped", "watchers stopped first"
    assert "bf-data-service" in reaped, "the hung server is reaped, not just the watchers"
    assert mgr._using_external is False, "drops external attachment so a fresh server can start"
    assert mgr._start_locked.called


def test_restart_idle_tracker_terminates_reaps_and_restarts():
    """Blind tracker (afk-while-input) recovery: kill the tracked proc, reap
    orphans, start fresh."""
    mgr = AWManager()
    idle = MagicMock()
    idle.poll.return_value = None  # alive
    mgr._processes = {"bf-idle-tracker": idle}
    mgr._get_binaries_dir = MagicMock(return_value="/tmp/bin")
    mgr._reap_orphan_processes = MagicMock()
    mgr._start_component = MagicMock()

    mgr.restart_idle_tracker(reason="afk while input active")

    assert idle.terminate.called, "stuck tracker is terminated"
    mgr._reap_orphan_processes.assert_called_once_with("bf-idle-tracker", "/tmp/bin")
    assert ("bf-idle-tracker",) in [
        c.args[:1] for c in mgr._start_component.call_args_list
    ], "a fresh idle tracker is started"


def test_restart_idle_tracker_skips_disabled():
    mgr = AWManager()
    idle = MagicMock()
    idle.poll.return_value = None
    mgr._processes = {"bf-idle-tracker": idle}
    mgr._disabled_components.add("bf-idle-tracker")
    mgr._get_binaries_dir = MagicMock(return_value="/tmp/bin")
    mgr._start_component = MagicMock()

    mgr.restart_idle_tracker()

    assert not idle.terminate.called, "a disabled idle tracker is never restarted"
    assert not mgr._start_component.called
