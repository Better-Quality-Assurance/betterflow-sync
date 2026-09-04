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
