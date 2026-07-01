"""Tests for the active-state fast-poll heartbeat.

The full heartbeat rides _do_sync and only fires every few cycles (~5 min), so
an admin-set logs_requested flag could sit unseen for minutes. _logs_poll_heartbeat
reuses the on-demand heartbeat path at a shorter, configurable cadence while a
session is ACTIVE so a pending logs request is picked up within ~1-2 min. It is
the active-state complement of _liveness_heartbeat (which covers the paused case).

Mirrors test_liveness_heartbeat.py: a SyncCoordinator with mocked deps,
exercising one 60s-tick helper directly (no scheduler / AW / tray loop).
"""

import time
from unittest.mock import MagicMock

from src.main import SyncCoordinator
from src.sync.http_client import BetterFlowAuthError


def _make_coordinator(interval: int = 90) -> SyncCoordinator:
    tray = MagicMock()
    tray.model = MagicMock()

    coord = SyncCoordinator(
        config=MagicMock(),
        aw=MagicMock(),
        bf=MagicMock(),
        queue=MagicMock(),
        sync_engine=MagicMock(),
        tray=tray,
        aw_manager=MagicMock(),
    )
    coord.logged_in = True
    coord.paused_by_network = False
    # MagicMock attrs are truthy by default — pin the states we branch on.
    coord.sync_engine.is_paused = False
    coord.sync_engine.is_private = False
    coord.sync_engine.send_heartbeat_now.return_value = None
    coord.config.sync.logs_poll_interval_seconds = interval
    coord.break_mgr = MagicMock()
    coord.break_mgr.is_on_break = False
    return coord


def test_heartbeat_when_active():
    coord = _make_coordinator()  # logged in, online, not paused
    coord._logs_poll_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_called_once()


def test_no_heartbeat_when_paused():
    # The paused case is handled by _liveness_heartbeat; this must not double up.
    coord = _make_coordinator()
    coord.sync_engine.is_paused = True
    coord._logs_poll_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_not_called()


def test_no_heartbeat_when_private():
    coord = _make_coordinator()
    coord.sync_engine.is_private = True
    coord._logs_poll_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_not_called()


def test_no_heartbeat_when_on_break():
    coord = _make_coordinator()
    coord.break_mgr.is_on_break = True
    coord._logs_poll_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_not_called()


def test_not_logged_in_skips():
    coord = _make_coordinator()
    coord.logged_in = False
    coord._logs_poll_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_not_called()


def test_offline_skips():
    coord = _make_coordinator()
    coord.paused_by_network = True  # offline — server unreachable
    coord._logs_poll_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_not_called()


def test_throttled_within_interval():
    coord = _make_coordinator()
    coord._logs_poll_heartbeat()
    coord._logs_poll_heartbeat()
    coord._logs_poll_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_called_once()


def test_refires_after_interval_elapses():
    coord = _make_coordinator(interval=90)
    coord._logs_poll_heartbeat()
    # Pretend the throttle clock advanced past the configured interval.
    coord._last_logs_poll_heartbeat = time.monotonic() - coord._logs_poll_interval - 1
    coord._logs_poll_heartbeat()
    assert coord.sync_engine.send_heartbeat_now.call_count == 2


def test_interval_is_read_from_config():
    coord = _make_coordinator(interval=120)
    assert coord._logs_poll_interval == 120


def test_interval_is_floor_clamped():
    # A too-small config value can't turn this into a per-tick heartbeat storm.
    coord = _make_coordinator(interval=5)
    assert coord._logs_poll_interval == coord._LOGS_POLL_INTERVAL_FLOOR


def test_interval_falls_back_on_bad_value():
    coord = _make_coordinator()
    coord.config.sync.logs_poll_interval_seconds = "not-an-int"
    assert coord._logs_poll_interval == coord._LOGS_POLL_INTERVAL_FALLBACK


def test_auth_error_routes_through_handler_and_is_tolerated():
    # An auth error from the fast poll is routed to _handle_auth_error, which
    # tolerates one transient failure rather than logging out immediately.
    coord = _make_coordinator()
    coord.sync_engine.send_heartbeat_now.return_value = BetterFlowAuthError("401")
    coord._logs_poll_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_called_once()
    assert coord.logged_in is True  # tolerated, not an immediate logout
    assert coord._consecutive_auth_failures == 1
