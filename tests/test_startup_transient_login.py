"""Startup auto-login must not kick a valid session to re-auth on an outage.

2026-07-02 Railway (hosting) outage: app.betterflow.eu returned 502 / timed
out. On launch, `try_auto_login` failed with a *network* error — but the
startup path treated that identically to invalid credentials: it flipped the
tray to WAITING_AUTH and fired a "your session ended, sign back in"
notification. Users (Tiberiu, Lucian) were pushed into a browser OAuth flow
that could not complete while the server was down, and experienced it as "the
agent doesn't start / it crashed." Their tokens were valid the whole time.

The fix: distinguish transient (server-unreachable) from definitive (bad
credentials) at startup. On a transient failure keep the session, show an
offline/reconnecting state, and retry auto-login in the background until the
outage clears — never prompt re-auth.

These tests drive the real dispatch + reconnect methods on a bare
BetterFlowApp instance (built via __new__ so the heavy __init__ is skipped;
only the handful of collaborators the methods touch are wired as mocks).
"""

import threading
from unittest.mock import MagicMock

from src.auth.login import LoginState
from src.main import BetterFlowApp
from src.ui.tray import TrayState


def _make_app() -> BetterFlowApp:
    """A BetterFlowApp with only the attributes the login-dispatch/reconnect
    paths use. No scheduler, tray loop, or AW client is started."""
    app = BetterFlowApp.__new__(BetterFlowApp)
    app._shutdown_event = threading.Event()
    app._login_lock = threading.Lock()
    app._reconnect_lock = threading.Lock()
    app._reconnect_thread = None
    app.coordinator = MagicMock()
    app.coordinator.logged_in = False
    app.tray = MagicMock()
    app.login_manager = MagicMock()
    # Stub the heavy post-login work — we assert it's *called*, not its guts.
    app._finish_logged_in_startup = MagicMock()
    app._ensure_update_checks_started = MagicMock()
    return app


class TestStartupDispatch:
    def test_transient_failure_does_not_prompt_reauth(self):
        app = _make_app()
        # Don't actually spawn the background thread in this unit.
        app._start_reconnect_retry = MagicMock()

        app._apply_startup_login_state(
            LoginState(logged_in=False, transient=True, error="Network error")
        )

        # No "session ended" warning, no WAITING_AUTH — instead an offline
        # state and a background reconnect.
        app.coordinator._maybe_warn_login_required.assert_not_called()
        app._start_reconnect_retry.assert_called_once()
        state_arg = app.tray.set_state.call_args[0][0]
        assert state_arg == TrayState.QUEUED
        app._finish_logged_in_startup.assert_not_called()

    def test_definitive_logout_prompts_reauth(self):
        app = _make_app()
        app._start_reconnect_retry = MagicMock()

        app._apply_startup_login_state(
            LoginState(logged_in=False, transient=False,
                       error="Stored credentials are invalid")
        )

        app.coordinator._maybe_warn_login_required.assert_called_once_with(
            source="startup"
        )
        app._start_reconnect_retry.assert_not_called()
        state_arg = app.tray.set_state.call_args[0][0]
        assert state_arg == TrayState.WAITING_AUTH

    def test_logged_in_finishes_startup(self):
        app = _make_app()
        state = LoginState(logged_in=True, user_email="a@b.co")

        app._apply_startup_login_state(state)

        app._finish_logged_in_startup.assert_called_once()
        app.coordinator._maybe_warn_login_required.assert_not_called()


class TestReconnectLoop:
    def test_recovers_when_server_returns(self):
        app = _make_app()
        # First retry still down (transient), second succeeds.
        app.login_manager.try_auto_login.side_effect = [
            LoginState(logged_in=False, transient=True),
            LoginState(logged_in=True, user_email="a@b.co"),
        ]
        # Make the interruptible sleep instant and finite so the loop runs
        # exactly its two iterations without a wall-clock wait.
        app._RECONNECT_RETRY_INTERVAL_S = 0
        app._shutdown_event.wait = MagicMock(return_value=False)

        app._reconnect_loop()

        assert app.login_manager.try_auto_login.call_count == 2
        app._finish_logged_in_startup.assert_called_once()
        # Recovery path must NOT nag the user to re-auth.
        app.coordinator._maybe_warn_login_required.assert_not_called()

    def test_falls_back_to_reauth_if_credentials_become_invalid(self):
        app = _make_app()
        app.login_manager.try_auto_login.return_value = LoginState(
            logged_in=False, transient=False
        )
        app._RECONNECT_RETRY_INTERVAL_S = 0
        app._shutdown_event.wait = MagicMock(return_value=False)

        app._reconnect_loop()

        app._finish_logged_in_startup.assert_not_called()
        app.coordinator._maybe_warn_login_required.assert_called_once_with(
            source="reconnect"
        )
        state_arg = app.tray.set_state.call_args[0][0]
        assert state_arg == TrayState.WAITING_AUTH

    def test_stops_on_shutdown(self):
        app = _make_app()
        app._shutdown_event.set()  # already shutting down

        app._reconnect_loop()

        app.login_manager.try_auto_login.assert_not_called()
