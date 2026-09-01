"""#2413 — a latched idle_tracker_blind must not outlive the subsystem it describes.

Both writes to `_idle_tracker_blind` — the latch after
IDLE_BLIND_RESTART_THRESHOLD failed restarts, and the clear on recovery — live
inside `_restart_if_needed_locked`'s `if not self._inproc_afk_active` branch. So
a device that latched blind while on the external bf-idle-tracker and then moved
to in-process AFK kept publishing `idle_tracker_blind: true` with nothing on
either side able to take it back: the agent cannot reach its own clear, and the
server's `readLatchedFlag` falls back to the stored column when a heartbeat omits
the key.

`health_snapshot()` publishes the flag every cycle regardless of which AFK source
is live, so an unretractable true is an alert that outlives the thing it
describes.

Latent rather than live when filed: the fleet read 57 devices with
idle_tracker_blind false on 43, null on 14, **true on 0**. These pin the
transition so it stays that way.
"""

from unittest.mock import Mock

from src.aw_manager import AWManager

IDLE = "bf-idle-tracker"


def _mgr(stop_external):
    m = AWManager(stop_external_when_inproc=stop_external)
    m._start_component = Mock()
    m._reap_orphan_processes = Mock()
    m._bin_dir = "/fake/bin"
    return m


def test_switching_to_inproc_retracts_a_latched_blind():
    """The regression. Pre-fix the flag survived the switch with no way back."""
    m = _mgr(stop_external=True)
    m._idle_tracker_blind = True

    m.set_inproc_afk_active(True)

    assert m._idle_tracker_blind is False, (
        "the agent must retract a latch it can no longer reach its own clear for"
    )


def test_retraction_does_not_depend_on_stop_external_when_inproc():
    """The half a naive fix misses.

    `set_inproc_afk_active` returns early when `_stop_external_when_inproc` is
    off — and that setting defaults OFF on the fleet. A clear placed after that
    return would fix the config we happen to enable and leave the default one
    latched forever.
    """
    m = _mgr(stop_external=False)
    m._idle_tracker_blind = True

    m.set_inproc_afk_active(True)

    assert m._inproc_afk_active is True
    assert m._idle_tracker_blind is False, (
        "the latch must be retracted even when stage 2 is disabled"
    )


def test_leaving_inproc_does_not_invent_a_blind_flag():
    """The allowance witness in the other direction.

    Going back to the external tracker must not SET the flag — blindness is
    something the restart loop observes, not something a source switch asserts.
    Without this, `self._idle_tracker_blind = not active` would pass the two
    tests above and start alerting on every fallback.
    """
    m = _mgr(stop_external=True)
    m._processes["bf-data-service"] = Mock(poll=Mock(return_value=None))
    m.set_inproc_afk_active(True)
    assert m._idle_tracker_blind is False

    m.set_inproc_afk_active(False)

    assert m._idle_tracker_blind is False, (
        "returning to the external tracker is not evidence it is blind"
    )


def test_a_healthy_device_switching_in_is_unaffected():
    """Allowance witness: the common path writes nothing surprising."""
    m = _mgr(stop_external=False)
    assert m._idle_tracker_blind is False

    m.set_inproc_afk_active(True)

    assert m._idle_tracker_blind is False
    assert m._inproc_afk_active is True
