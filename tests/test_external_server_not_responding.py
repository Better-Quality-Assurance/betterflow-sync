"""A device attached to a corpse must SAY so (F-3, reporting half).

The lifecycle is deliberately unchanged: a foreign process holding :5600 is
unreapable (_reap_orphan_processes is path-scoped to our binaries), so
attaching is the behaviour that keeps the watchers alive and self-heals when
the holder dies. Two attempts to change that both regressed. What was missing
was telling the fleet, which is what this flag does.
"""

from unittest.mock import MagicMock

from src.aw_manager import AWManager


def _mgr(*, port_held, answers):
    m = AWManager()
    m._capture_suppressed = False
    m._get_binaries_dir = MagicMock(return_value="/tmp/bin")
    m._rosetta_required = MagicMock(return_value=False)
    m._port_in_use = MagicMock(return_value=port_held)
    m._server_responding = MagicMock(return_value=answers)
    m._start_component = MagicMock(return_value=True)
    m._wait_for_server = MagicMock(return_value=True)
    m._reap_orphan_processes = MagicMock()
    return m


class TestAttachedToACorpseIsReported:
    def test_a_dead_holder_sets_the_flag(self):
        m = _mgr(port_held=True, answers=False)
        m._start_locked()
        assert m._external_server_not_responding is True

    def test_the_flag_survives_the_watcher_loop(self):
        """_start_component clears the two capture-dead flags on success, which
        is why this is a separate flag and not a reuse of those."""
        m = _mgr(port_held=True, answers=False)
        m._start_locked()
        assert m._start_component.called, "fixture never reached the watcher loop"
        assert m._external_server_not_responding is True

    def test_the_lifecycle_is_UNCHANGED_by_this(self):
        """The whole point: we still attach, and the watchers still start."""
        m = _mgr(port_held=True, answers=False)
        assert m._start_locked() is True
        assert m._using_external is True
        started = [c.args[0] for c in m._start_component.call_args_list]
        assert "bf-window-tracker" in started and "bf-idle-tracker" in started


class TestTheAllowanceDirection:
    def test_a_live_external_server_does_not_set_it(self):
        m = _mgr(port_held=True, answers=True)
        m._start_locked()
        assert m._external_server_not_responding is False

    def test_starting_our_own_server_clears_it(self):
        """A stale True must not outlive the condition."""
        m = _mgr(port_held=False, answers=False)
        m._external_server_not_responding = True
        m._start_locked()
        assert m._external_server_not_responding is False


class TestItReachesTheWire:
    def test_health_snapshot_publishes_it(self):
        m = _mgr(port_held=True, answers=False)
        m._get_latest_window_event_age = MagicMock(return_value=5)
        m._get_latest_afk_event_age = MagicMock(return_value=5)
        m._window_titles_captured_recently = MagicMock(return_value=True)
        m._start_locked()

        assert m.health_snapshot()["external_server_not_responding"] is True

    def test_the_key_is_on_the_heartbeat_allowlist(self):
        """A field missing from HEARTBEAT_HEALTH_KEYS never leaves the machine,
        silently -- health_snapshot publishing it is not enough."""
        from src.sync.bf_client import BetterFlowClient

        assert (
            "external_server_not_responding"
            in BetterFlowClient.HEARTBEAT_HEALTH_KEYS
        )


class TestTheFlagIsALatchUntilTheNextStartEvaluation:
    def test_the_flag_is_a_latch_until_the_next_start_evaluation(self):
        """Known, deliberate limitation -- not a regression to chase.

        A prior fix wave (round 3) tried to clear this flag from the routine
        60s tick's window-tracker reachability check, the moment /api/0/info
        started answering again. That clear lived INSIDE the block gated on
        `watcher not in self._disabled_components` (watcher =
        "bf-window-tracker"), and main.py unconditionally disables
        "bf-window-tracker" on darwin -- the in-process macOS window watcher
        covers it instead. So the clear was inert on exactly the platform
        this file's Rosetta/Accessibility complexity exists for, and worked
        on Windows/Linux only: the same flag would mean a different thing
        depending on the OS the device happened to run.

        Reverted rather than repaired. The honest, platform-independent
        behaviour pinned here: this flag is set or cleared ONLY inside a
        `_start_locked` evaluation, and the routine 60s tick does not
        re-enter `_start_locked` while the port stays held (see
        _restart_if_needed_locked's bare-port check above). So a corpse that
        starts answering again with no restart-ish event in between --
        force_restart() via the 180s unreachable watchdog, or
        set_capture_suppressed(False) on an empty process set -- keeps this
        flag at its stale value. No consumer reads this key yet, so the cost
        of the latch is zero today; fixing it for real needs a
        platform-independent reachability point under the lock, which this
        test deliberately does not attempt.
        """
        m = _mgr(port_held=True, answers=False)
        m._start_locked()
        assert m._external_server_not_responding is True

        # The corpse "recovers" -- starts answering -- with no restart-ish
        # event in between. Give the instance a live watcher process so the
        # routine tick's window-tracker section runs (it used to be where
        # the now-reverted clear lived).
        window = MagicMock()
        window.poll.return_value = None
        m._processes = {"bf-window-tracker": window}
        m._get_latest_window_event_age = MagicMock(return_value=5)
        m._get_latest_afk_event_age = MagicMock(return_value=5)
        m._server_responding = MagicMock(return_value=True)

        for _ in range(3):
            m.restart_if_needed()

        assert m._external_server_not_responding is True, (
            "the routine tick must not clear this -- the clear was inert on "
            "macOS and is gone on every platform now; see the class docstring"
        )


class TestTheFlagDoesNotOutliveTheCondition:
    def test_the_flag_does_not_outlive_the_condition_that_set_it(self):
        """The reviewer's exact two-cycle repro (Finding 1, fix round 1).

        cycle 1: normal path, corpse holds the port -> flag correctly True.
        cycle 2: Rosetta branch, external server now genuinely capturing
                 (_port_in_use=True, _server_responding=True) -> attaches
                 through a DIFFERENT branch than the normal path, and the
                 stale True from cycle 1 must not survive.
        """
        m = _mgr(port_held=True, answers=False)
        m._start_locked()
        assert m._external_server_not_responding is True

        m._rosetta_required = MagicMock(return_value=True)
        m._port_in_use = MagicMock(return_value=True)
        m._server_responding = MagicMock(return_value=True)
        m._start_locked()

        assert m._external_server_not_responding is False

    def test_the_flag_does_not_outlive_suppressed_capture(self):
        """Fix round 2, Finding 2. My own re-review reproduction, THEN
        corrected (fix round 3) to drive the real production entrypoint.

        cycle 1 (corpse):             flag=True
        cycle 2 (capture suppressed): flag=True   <- outlived its condition

        set_capture_suppressed(True) is the ONLY production caller that
        suppresses capture, and it calls _stop_locked() -- never
        _start_locked(). The public start() has zero callers in src/ or
        tests/, so a test driving _start_locked() a second time to simulate
        suppression witnesses a path production never takes; it was green
        even while _stop_locked() left the flag latched. False is also the
        honest value here: while capture is suppressed the device is not
        attached to a dead external server, it is not attached to anything.
        """
        m = _mgr(port_held=True, answers=False)
        m._start_locked()
        assert m._external_server_not_responding is True

        m.set_capture_suppressed(True)

        assert m._capture_suppressed is True
        assert m._external_server_not_responding is False
