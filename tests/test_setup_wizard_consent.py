"""Tests for the first-run consent/transparency screen.

On non-macOS platforms (no OS permission gate) a successful login routes to
the consent screen before success; on macOS it goes straight to success.
No real Tk event loop is started — _window/_canvas are mocked.
"""

from unittest.mock import MagicMock, patch

from src.ui.setup_wizard import SetupWizard


def _make_wizard() -> SetupWizard:
    w = SetupWizard()
    w._window = MagicMock()
    w._canvas = MagicMock()
    w._spinner_after_id = None
    w._show_success = MagicMock()
    w._show_consent = MagicMock()
    return w


class TestConsentRouting:
    def test_non_macos_login_routes_to_consent(self):
        w = _make_wizard()
        state = MagicMock(logged_in=True, user_email="x@y.co")
        with patch("src.ui.setup_wizard.sys.platform", "win32"):
            w._on_login_complete(state)
        w._show_consent.assert_called_once_with("x@y.co")
        w._show_success.assert_not_called()

    def test_linux_login_routes_to_consent(self):
        w = _make_wizard()
        state = MagicMock(logged_in=True, user_email="x@y.co")
        with patch("src.ui.setup_wizard.sys.platform", "linux"):
            w._on_login_complete(state)
        w._show_consent.assert_called_once()
        w._show_success.assert_not_called()

    def test_macos_login_skips_consent(self):
        w = _make_wizard()
        state = MagicMock(logged_in=True, user_email="x@y.co")
        with patch("src.ui.setup_wizard.sys.platform", "darwin"):
            w._on_login_complete(state)
        w._show_success.assert_called_once_with("x@y.co")
        w._show_consent.assert_not_called()

    def test_failed_login_shows_neither(self):
        w = _make_wizard()
        w._show_error = MagicMock()
        state = MagicMock(logged_in=False, error="nope")
        with patch("src.ui.setup_wizard.sys.platform", "win32"):
            w._on_login_complete(state)
        w._show_consent.assert_not_called()
        w._show_success.assert_not_called()
        w._show_error.assert_called_once()


class TestConsentRender:
    def test_consent_screen_draws_and_offers_continue(self):
        w = _make_wizard()
        # Render against mocked canvas; isolate from _draw_scene/_make_button.
        w._draw_scene = MagicMock(return_value=300)
        w._make_button = MagicMock()
        # Use the real method (un-mock it for this render test).
        SetupWizard._show_consent(w, "x@y.co")
        w._draw_scene.assert_called_once()
        w._make_button.assert_called_once()
        # The action button forwards to the success screen on acknowledgement.
        label, action = w._make_button.call_args.args[0], w._make_button.call_args.args[1]
        assert "Agree" in label
        action()
        w._show_success.assert_called_once_with("x@y.co")
