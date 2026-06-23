"""Stage 2 of tracker-convergence: stop the external bf-idle-tracker process when
the in-process AFK source is active, re-launch it as the fallback when in-process
goes unavailable.

Stage 1 (#81) made the in-process stream the sole *uploaded* AFK source, but the
external tracker kept running and being ignored — the dual-source surface that
produced Bug A. Stage 2 stops the redundant process entirely. It is gated behind
`AWManager(stop_external_when_inproc=...)` (wired from
`config.sync.stop_external_afk_tracker`, default OFF) so it ships without changing
fleet behaviour until explicitly enabled, and so it is independently reversible —
important because with the tracker STOPPED, recovery can't fall back to it the way
stage 1 (tracker merely ignored) could.

The mechanism reuses the existing `_disabled_components` set, which already makes
`_start_locked`, the watchdog (`_restart_if_needed_locked`), and
`restart_idle_tracker` all skip a component — so one membership flip gates every
(re)start path.
"""

from unittest.mock import Mock

from src.aw_manager import AWManager

IDLE = "bf-idle-tracker"


class FakeProc:
    def __init__(self, alive=True):
        self._alive = alive
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.killed = True


def _mgr(stop_external: bool) -> AWManager:
    m = AWManager(stop_external_when_inproc=stop_external)
    m._get_binaries_dir = lambda: "/fake/bin"
    m._start_component = Mock(return_value=True)
    m._reap_orphan_processes = Mock()
    return m


def test_flag_defaults_off():
    assert AWManager()._stop_external_when_inproc is False


def test_off_mode_never_touches_the_tracker():
    m = _mgr(stop_external=False)
    proc = FakeProc()
    m._processes[IDLE] = proc

    m.set_inproc_afk_active(True)

    # Stage-2 disabled → behave exactly as before: flag set, tracker untouched.
    assert m._inproc_afk_active is True
    assert IDLE not in m._disabled_components
    assert proc.terminated is False
    assert IDLE in m._processes


def test_active_stops_and_disables_the_tracker():
    m = _mgr(stop_external=True)
    proc = FakeProc()
    m._processes[IDLE] = proc

    m.set_inproc_afk_active(True)

    assert IDLE in m._disabled_components       # gates every (re)start path
    assert proc.terminated is True              # process actually stopped
    assert IDLE not in m._processes             # untracked so health checks ignore it
    m._reap_orphan_processes.assert_called_once_with(IDLE, "/fake/bin")


def test_inactive_reenables_and_restarts_the_tracker():
    m = _mgr(stop_external=True)
    # A running stack: server + window tracker stay up; only the idle tracker is
    # cycled. _processes is non-empty after the idle tracker is stopped, so the
    # fallback restart is valid (server is up to receive its events).
    m._processes["bf-data-service"] = FakeProc()
    m._processes[IDLE] = FakeProc()
    m.set_inproc_afk_active(True)               # disable + stop idle tracker
    assert IDLE in m._disabled_components

    m.set_inproc_afk_active(False)              # fallback: re-enable + start

    assert IDLE not in m._disabled_components
    m._start_component.assert_called_with(IDLE, "/fake/bin")


def test_inactive_before_start_does_not_launch_a_serverless_tracker():
    # Transition fired from a pre-start reconcile/cycle (nothing started yet):
    # the fallback must NOT launch a lone tracker against a server that isn't up —
    # start() will bring it up with the rest. (Review finding #2.)
    m = _mgr(stop_external=True)
    m.set_inproc_afk_active(True)               # _processes empty → disable only
    m.set_inproc_afk_active(False)              # re-enable, but defer the start

    assert IDLE not in m._disabled_components
    m._start_component.assert_not_called()


def test_no_transition_is_idempotent():
    m = _mgr(stop_external=True)
    proc = FakeProc()
    m._processes[IDLE] = proc

    m.set_inproc_afk_active(True)
    m.set_inproc_afk_active(True)               # same value — no second stop

    assert m._reap_orphan_processes.call_count == 1
    assert IDLE in m._disabled_components


def test_active_when_tracker_not_running_is_a_noop_stop():
    m = _mgr(stop_external=True)
    # No process tracked yet (e.g. set before start()).
    m.set_inproc_afk_active(True)

    assert IDLE in m._disabled_components        # still disabled so start() skips it
    m._start_component.assert_not_called()


def test_restart_idle_tracker_skips_a_stage2_disabled_tracker():
    # The existing disabled-gate must keep the blind-tracker health path from
    # resurrecting a tracker stage 2 intentionally stopped.
    m = _mgr(stop_external=True)
    m.set_inproc_afk_active(True)
    m._start_component.reset_mock()

    m.restart_idle_tracker(reason="health check")

    m._start_component.assert_not_called()
