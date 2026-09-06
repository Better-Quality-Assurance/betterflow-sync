"""Two health flags that could not take themselves back.

Both publish to the fleet every cycle via health_snapshot(), so a flag that
cannot be retracted is an alert that outlives the thing it describes.

#247  external_server_not_responding was a LATCH. It is derived only during a
      _start_locked evaluation, and the routine 60s tick does not re-enter
      _start_locked while the port stays held. A first attempt to clear it in
      _restart_if_needed_locked was reverted because it sat INSIDE the
      `bf-window-tracker` block -- and main.py disables that component
      unconditionally on darwin, so it worked on Windows/Linux and was inert on
      the platform this file's Rosetta/Accessibility complexity exists for.

#240  _idle_tracker_blind is retracted when in-process AFK takes over, because
      both of its writes live in a branch that source can no longer reach. But
      it is not RE-DERIVED on the way out: _idle_consecutive_stale survives the
      round trip, so a tracker that was genuinely blind before the switch comes
      back reporting healthy, and the Input Monitoring re-prompt stays
      suppressed while billing runs off a frozen AFK stream.
"""

from unittest.mock import MagicMock

from src.aw_manager import IDLE_BLIND_RESTART_THRESHOLD, AWManager


def _ticking(*, responding, disabled=()):
    """A manager whose 60s tick will actually run (processes present)."""
    m = AWManager()
    m._capture_suppressed = False
    m._get_binaries_dir = MagicMock(return_value="/tmp/bin")
    m._port_in_use = MagicMock(return_value=True)
    m._server_responding = MagicMock(return_value=responding)
    m._start_component = MagicMock(return_value=True)
    m.check_health = MagicMock(return_value=True)
    m._get_latest_afk_event_age = MagicMock(return_value=5)
    m._get_latest_window_event_age = MagicMock(return_value=5)
    m._using_external = True
    for c in disabled:
        m._disabled_components.add(c)
    m._processes = {
        n: MagicMock(**{"poll.return_value": None})
        for n in ("bf-window-tracker", "bf-idle-tracker")
        if n not in disabled
    }
    return m


class TestExternalServerFlagClearsOnEveryPlatform:
    """macOS is the case the reverted attempt could not reach."""

    def test_it_clears_on_the_macos_shape(self):
        m = _ticking(responding=True, disabled=("bf-window-tracker",))
        m._external_server_not_responding = True

        m.restart_if_needed()

        assert m._external_server_not_responding is False, (
            "still latched with bf-window-tracker disabled -- the clear is "
            "inside that component's block again, so it is inert on macOS"
        )

    def test_it_clears_on_the_windows_linux_shape(self):
        m = _ticking(responding=True)
        m._external_server_not_responding = True
        m.restart_if_needed()
        assert m._external_server_not_responding is False

    def test_a_still_dead_server_keeps_the_flag(self):
        """Control: the flag must not clear just because a tick ran."""
        m = _ticking(responding=False, disabled=("bf-window-tracker",))
        m._external_server_not_responding = True
        m.restart_if_needed()
        assert m._external_server_not_responding is True

    def test_no_probe_is_paid_when_there_is_nothing_to_clear(self):
        """Cost control: the flag is False on virtually every device, and this
        runs every 60s under _lifecycle_lock. Asking /api/0/info when there is
        nothing to retract would add a 2s-timeout call to every tick on macOS,
        where the window-tracker block does not run at all."""
        m = _ticking(responding=True, disabled=("bf-window-tracker",))
        m._external_server_not_responding = False
        before = m._server_responding.call_count

        m.restart_if_needed()

        assert m._server_responding.call_count == before, (
            "paid for a reachability probe with no flag to clear"
        )


class TestIdleBlindIsReDerivedOnTheWayOut:
    def test_a_still_earned_latch_comes_back(self):
        m = _ticking(responding=True)
        m._idle_consecutive_stale = IDLE_BLIND_RESTART_THRESHOLD
        m._idle_tracker_blind = True

        m.set_inproc_afk_active(True)
        assert m._idle_tracker_blind is False, "precondition: #2413's retraction"

        m.set_inproc_afk_active(False)

        assert m._idle_tracker_blind is True, (
            "the external tracker is live again and its stale counter never "
            "moved, so it is still blind -- but the flag says healthy and the "
            "Input Monitoring re-prompt stays suppressed"
        )

    def test_a_tracker_that_was_never_blind_stays_clear(self):
        """Control: leaving in-process AFK must not INVENT a blind flag."""
        m = _ticking(responding=True)
        m._idle_consecutive_stale = 0
        m.set_inproc_afk_active(True)
        m.set_inproc_afk_active(False)
        assert m._idle_tracker_blind is False

    def test_entering_in_process_afk_still_retracts(self):
        """Control: #2413's fix must survive."""
        m = _ticking(responding=True)
        m._idle_consecutive_stale = IDLE_BLIND_RESTART_THRESHOLD
        m._idle_tracker_blind = True
        m.set_inproc_afk_active(True)
        assert m._idle_tracker_blind is False
