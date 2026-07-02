"""Tests for transient-auth-error tolerance.

A single 401/403 (backend deploy, momentary token-lookup blip) used to log the
user out immediately and stop tracking → dashboard showed "idle". Now:
- SyncCoordinator._handle_auth_error tolerates failures below a threshold and
  only logs out after N consecutive ones; a fresh login resets the streak.
- LoginManager.try_auto_login retries auth failures with backoff before wiping
  stored credentials.
"""

from unittest.mock import MagicMock, patch

from src.auth.login import LoginManager
from src.main import SyncCoordinator
from src.sync.http_client import BetterFlowAuthError, BetterFlowClientError


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
    coord._on_auth_error = MagicMock()
    return coord


class TestHandleAuthErrorTolerance:
    def test_single_auth_error_is_tolerated(self):
        coord = _make_coordinator()
        coord._handle_auth_error(BetterFlowAuthError("401"), source="sync")
        assert coord.logged_in is True
        coord._on_auth_error.assert_not_called()

    def test_below_threshold_keeps_session(self):
        coord = _make_coordinator()
        for _ in range(coord._AUTH_FAILURE_LOGOUT_THRESHOLD - 1):
            coord._handle_auth_error(BetterFlowAuthError("401"), source="heartbeat")
        assert coord.logged_in is True
        coord._on_auth_error.assert_not_called()

    def test_threshold_logs_out_and_triggers_relogin(self):
        coord = _make_coordinator()
        for _ in range(coord._AUTH_FAILURE_LOGOUT_THRESHOLD):
            coord._handle_auth_error(BetterFlowAuthError("401"), source="sync")
        assert coord.logged_in is False
        coord._on_auth_error.assert_called_once()

    def test_login_resets_the_streak(self):
        coord = _make_coordinator()
        # Two failures (below threshold), then a successful (re)login.
        coord._handle_auth_error(BetterFlowAuthError("401"), source="sync")
        coord._handle_auth_error(BetterFlowAuthError("401"), source="sync")
        coord.logged_in = True  # setter resets the auth streak
        assert coord._consecutive_auth_failures == 0
        # Two more failures must still be tolerated (streak restarted).
        coord._handle_auth_error(BetterFlowAuthError("401"), source="sync")
        coord._handle_auth_error(BetterFlowAuthError("401"), source="sync")
        assert coord.logged_in is True


class TestTryAutoLoginRetries:
    def _mgr(self, get_status_side_effect):
        bf = MagicMock()
        bf.get_status.side_effect = get_status_side_effect
        keychain = MagicMock()
        creds = MagicMock()
        creds.api_token = "tok"
        creds.device_id = "dev"
        creds.user_email = "a@b.co"
        creds.user_name = "A"
        creds.user_role = "user"
        keychain.load.return_value = creds
        return LoginManager(bf_client=bf, keychain=keychain), bf

    def test_transient_auth_error_then_success_does_not_clear(self):
        # 401 twice, then OK — should log in and NEVER clear credentials.
        mgr, bf = self._mgr([BetterFlowAuthError("401"), BetterFlowAuthError("401"), None])
        with patch("src.auth.login.time.sleep"):
            state = mgr.try_auto_login()
        assert state.logged_in is True
        bf.clear_credentials.assert_not_called()

    def test_persistent_auth_error_clears_after_retries(self):
        mgr, bf = self._mgr([BetterFlowAuthError("401")] * 3)
        with patch("src.auth.login.time.sleep"):
            state = mgr.try_auto_login()
        assert state.logged_in is False
        assert state.transient is False  # definitive: caller should prompt re-auth
        bf.clear_credentials.assert_called_once()

    def test_network_error_never_clears(self):
        mgr, bf = self._mgr([BetterFlowClientError("network")])
        with patch("src.auth.login.time.sleep"):
            state = mgr.try_auto_login()
        assert state.logged_in is False
        # A server outage is transient — credentials stay, caller must NOT
        # prompt re-auth (Railway outage, 2026-07-02: valid sessions were
        # wrongly kicked to a browser login that couldn't complete).
        assert state.transient is True
        bf.clear_credentials.assert_not_called()
