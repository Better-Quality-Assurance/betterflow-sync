"""Tests for transient-auth-error tolerance.

A single 401/403 (backend deploy, momentary token-lookup blip) used to log the
user out immediately and stop tracking → dashboard showed "idle". Now:
- SyncCoordinator._handle_auth_error tolerates failures below a count+time
  threshold; a fresh login resets the streak.
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

    def test_threshold_inside_grace_window_keeps_session(self):
        coord = _make_coordinator()
        for _ in range(coord._AUTH_FAILURE_LOGOUT_THRESHOLD):
            coord._handle_auth_error(BetterFlowAuthError("401"), source="sync")
        assert coord.logged_in is True
        coord._on_auth_error.assert_not_called()

    def test_sustained_threshold_logs_out_and_triggers_relogin(self):
        coord = _make_coordinator()
        coord._first_auth_failure_monotonic = 0.0
        with patch("src.main.time.monotonic", return_value=coord._AUTH_FAILURE_LOGOUT_MIN_SECONDS + 1):
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
        assert coord._first_auth_failure_monotonic is None
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

    def test_persistent_auth_error_is_tolerated_before_the_window(self):
        """A rejection at LAUNCH is not more conclusive than the same rejection
        a minute later, so it no longer gets a 6-second death sentence.

        Startup used to wipe the token after 3 attempts ~6s apart, while the
        running agent tolerates the identical 401 for 15 minutes
        (SyncCoordinator._AUTH_FAILURE_LOGOUT_MIN_SECONDS). Two rules for one
        decision, and the trigger-happy one owned the credential.
        """
        mgr, bf = self._mgr([BetterFlowAuthError("401")] * 3)
        with patch("src.auth.login.time.sleep"):
            state = mgr.try_auto_login()
        assert state.logged_in is False
        assert state.transient is True  # keep retrying, do not prompt yet
        bf.clear_credentials.assert_not_called()

    def test_persistent_auth_error_clears_once_the_window_passes(self):
        """...but it must still END. A revoked token that is tolerated forever
        is an agent that never tracks and never says why."""
        clock = _FakeClock()
        mgr, bf = self._mgr([BetterFlowAuthError("401")] * 6)
        mgr._time_source = clock

        with patch("src.auth.login.time.sleep"):
            assert mgr.try_auto_login().transient is True
            clock.advance(LoginManager.AUTH_TOLERANCE_SECONDS + 1)
            state = mgr.try_auto_login()

        assert state.logged_in is False
        assert state.transient is False  # definitive: caller should prompt re-auth
        bf.clear_credentials.assert_called_once()

    def test_a_successful_restore_resets_the_window(self):
        clock = _FakeClock()
        # One call consumes up to 3 side effects (the in-call retries): three
        # 401s exhaust call 1, the None satisfies call 2, three more 401s make
        # call 3 fail. Pairing these by hand rather than by count is what keeps
        # a rejected call from silently succeeding on the next entry.
        mgr, bf = self._mgr(
            [BetterFlowAuthError("401")] * 3 + [None] + [BetterFlowAuthError("401")] * 3
        )
        mgr._time_source = clock

        with patch("src.auth.login.time.sleep"):
            assert mgr.try_auto_login().transient is True
            clock.advance(60)
            assert mgr.try_auto_login().logged_in is True
            # Long after the ORIGINAL failure — but the streak was reset, so
            # this new one opens its own window instead of escalating at once.
            clock.advance(LoginManager.AUTH_TOLERANCE_SECONDS * 3)
            assert mgr.try_auto_login().transient is True

        bf.clear_credentials.assert_not_called()

    def test_a_network_outage_does_not_advance_the_auth_window(self):
        """An unreachable server says nothing about the credential. It must
        neither start the give-up clock nor reset one already running."""
        clock = _FakeClock()
        mgr, bf = self._mgr([BetterFlowClientError("network")] * 3)
        mgr._time_source = clock

        with patch("src.auth.login.time.sleep"):
            mgr.try_auto_login()
            clock.advance(LoginManager.AUTH_TOLERANCE_SECONDS * 2)
            mgr.try_auto_login()

        assert mgr._first_restore_failure is None
        bf.clear_credentials.assert_not_called()

    def test_a_browser_login_resets_the_window_too(self):
        """The streak has TWO ways to end, and only one of them was watched.

        A user who signs in through the browser after an escalation must not
        inherit the old first-failure timestamp: the next single 401 would
        then look 15 minutes old and clear the fresh token on the first try —
        the exact trigger-happy behaviour the window exists to stop.

        Asserted through the CONSUMER (the next failure is tolerated), not by
        reading the private timestamp, so it fails if the reset stops having
        an effect rather than merely if the assignment moves.
        """
        clock = _FakeClock()
        mgr, bf = self._mgr([BetterFlowAuthError("401")] * 3)
        mgr._time_source = clock
        bf.web_base_url = "https://app.betterflow.eu"

        # Burn the tolerance window down to an escalation.
        with patch("src.auth.login.time.sleep"):
            mgr.try_auto_login()
            clock.advance(LoginManager.AUTH_TOLERANCE_SECONDS + 1)

        # Precondition: the streak really is running and really is past the
        # window. Without this the closing assertions ("tolerated", "not
        # cleared") are exactly what a fixture that never started a streak
        # would produce, and the test would pass while proving nothing.
        assert mgr._first_restore_failure is not None
        assert clock() - mgr._first_restore_failure > LoginManager.AUTH_TOLERANCE_SECONDS

        flow = MagicMock()
        flow.start.return_value = MagicMock(success=True, code="c", code_verifier="v")
        # DeviceInfo.collect() resolves the persistent machine id for real,
        # which reads the hardware serial and WRITES .machine_id into the live
        # config dir. Stub it the way the rest of the suite does — a unit test
        # must not mint this machine's identity as a side effect.
        with patch("src.auth.login.BrowserAuthFlow", return_value=flow), patch(
            "src.sync.bf_client.get_machine_uuid",
            return_value="aaaabbbb-1111-2222-3333-444455556666",
        ):
            assert mgr.login_via_browser().logged_in is True

        # A single 401 straight after that login must be tolerated, not fatal.
        bf.get_status.side_effect = [BetterFlowAuthError("401")] * 3
        bf.clear_credentials.reset_mock()
        with patch("src.auth.login.time.sleep"):
            state = mgr.try_auto_login()

        assert state.transient is True
        bf.clear_credentials.assert_not_called()

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


class _FakeClock:
    """A monotonic clock the test drives, so the whole tolerance window is
    exercised without sleeping and without racing the real clock."""

    def __init__(self, now: float = 1000.0):
        self._now = now

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds
