"""Tests for UpdateHandler.trigger_remote_update (heartbeat-driven update push).

The server advertises a fleet update floor (minimum_agent_version) on the
heartbeat; the sync engine calls trigger_remote_update when the agent is below
it. The handler must stage the latest build (applied on next idle) WITHOUT
re-downloading on every 5-min heartbeat.
"""

from unittest.mock import MagicMock, patch

from src.update_handler import UpdateHandler


def _handler(version: str = "1.5.68") -> UpdateHandler:
    tray = MagicMock()
    tray.model.update_in_progress = False
    config = MagicMock(check_updates=True, update_channel="stable")
    coordinator = MagicMock()
    h = UpdateHandler(tray, config, coordinator, version)
    h._periodic_update_check = MagicMock()  # don't hit GitHub in tests
    return h


def test_trigger_stages_when_agent_is_behind():
    h = _handler("1.5.68")
    h.trigger_remote_update("1.5.71")
    h._periodic_update_check.assert_called_once()


def test_trigger_is_throttled_across_repeated_heartbeats():
    """The heartbeat keeps reporting the floor every ~5 min until the agent
    updates; the handler must not re-download each time."""
    h = _handler("1.5.68")
    h.trigger_remote_update("1.5.71")
    h.trigger_remote_update("1.5.71")
    h.trigger_remote_update("1.5.71")
    assert h._periodic_update_check.call_count == 1


def test_trigger_skips_when_target_already_staged():
    """A build >= the target is already downloaded — it applies on idle; we
    must not fetch again."""
    h = _handler("1.5.68")
    h._staged_version = "1.5.71"
    h.trigger_remote_update("1.5.71")
    h._periodic_update_check.assert_not_called()


def test_trigger_stages_when_staged_is_older_than_target():
    h = _handler("1.5.68")
    h._staged_version = "1.5.70"
    h.trigger_remote_update("1.5.71")
    h._periodic_update_check.assert_called_once()


def test_first_push_not_throttled_on_fresh_boot():
    """Regression: on a freshly-booted machine time.monotonic() is small. With
    the throttle seeded to 0.0, `now - 0.0 < THROTTLE` wrongly suppressed the
    FIRST push (green on a long-uptime dev box, red on a fresh CI runner). The
    first check must always fire regardless of the monotonic clock's origin."""
    h = _handler("1.5.68")
    with patch("src.update_handler.time.monotonic", return_value=5.0):
        h.trigger_remote_update("1.5.71")
    h._periodic_update_check.assert_called_once()


def test_trigger_respects_check_updates_disabled():
    h = _handler("1.5.68")
    h.config.check_updates = False
    h.trigger_remote_update("1.5.71")
    h._periodic_update_check.assert_not_called()
