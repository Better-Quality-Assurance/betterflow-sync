"""Tests for the paused-state liveness heartbeat.

The normal heartbeat rides _do_sync, which is skipped while the agent is
paused (break / screen lock / manual). A break longer than the server's
30-minute stale-session cleanup then gets its session marked "crashed", so
tracking does not resume when the user returns. _liveness_heartbeat keeps the
session alive by heartbeating from the 60s tick while paused-but-running.

Mirrors test_auth_warn.py: a SyncCoordinator with mocked deps, exercising one
60s-tick helper directly (no scheduler / AW / tray loop).
"""

import time
from unittest.mock import MagicMock

from src.main import SyncCoordinator
from src.sync.http_client import BetterFlowAuthError


def _make_coordinator() -> SyncCoordinator:
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
    coord.break_mgr = MagicMock()
    coord.break_mgr.is_on_break = False
    return coord


def test_no_heartbeat_while_actively_syncing():
    coord = _make_coordinator()  # not paused
    coord._liveness_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_not_called()


def test_heartbeat_when_paused():
    coord = _make_coordinator()
    coord.sync_engine.is_paused = True
    coord._liveness_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_called_once()


def test_heartbeat_when_on_break():
    coord = _make_coordinator()
    coord.break_mgr.is_on_break = True
    coord._liveness_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_called_once()


def test_heartbeat_when_private():
    coord = _make_coordinator()
    coord.sync_engine.is_private = True
    coord._liveness_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_called_once()


def test_throttled_within_interval():
    coord = _make_coordinator()
    coord.sync_engine.is_paused = True
    coord._liveness_heartbeat()
    coord._liveness_heartbeat()
    coord._liveness_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_called_once()


def test_refires_after_interval_elapses():
    coord = _make_coordinator()
    coord.sync_engine.is_paused = True
    coord._liveness_heartbeat()
    # Pretend the throttle clock advanced past the interval.
    coord._last_liveness_heartbeat = time.monotonic() - coord._LIVENESS_HEARTBEAT_INTERVAL - 1
    coord._liveness_heartbeat()
    assert coord.sync_engine.send_heartbeat_now.call_count == 2


def test_not_logged_in_skips():
    coord = _make_coordinator()
    coord.logged_in = False
    coord.sync_engine.is_paused = True
    coord._liveness_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_not_called()


def test_offline_skips():
    coord = _make_coordinator()
    coord.sync_engine.is_paused = True
    coord.paused_by_network = True  # offline — server unreachable
    coord._liveness_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_not_called()


def test_auth_error_triggers_relogin():
    coord = _make_coordinator()
    coord.sync_engine.is_paused = True
    coord.sync_engine.send_heartbeat_now.return_value = BetterFlowAuthError("401")
    coord._liveness_heartbeat()
    coord.sync_engine.send_heartbeat_now.assert_called_once()
    assert coord.logged_in is False  # _handle_auth_error flipped it
