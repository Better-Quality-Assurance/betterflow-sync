"""Tests for OS notification delivery."""

from unittest.mock import MagicMock, patch

from src.notifications import clear_notifications, send_notification
import src.notifications as notifications_module


class TestSendNotification:
    """Tests for send_notification()."""

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications._try_load_macos_pyobjc", return_value=False)
    @patch("src.notifications.subprocess.run")
    def test_macos_osascript_fallback(self, mock_run, _mock_pyobjc, _mock_sys):
        """Falls back to osascript when pyobjc is unavailable."""
        send_notification("Title", "Body")

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "osascript"
        assert 'display notification "Body" with title "Title"' in args[2]
        assert 'sound name "default"' in args[2]

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications._try_load_macos_pyobjc", return_value=False)
    @patch("src.notifications.subprocess.run")
    def test_macos_no_sound(self, mock_run, _mock_pyobjc, _mock_sys):
        send_notification("Title", "Body", sound=False)

        script = mock_run.call_args[0][0][2]
        assert "sound name" not in script

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications._try_load_macos_pyobjc", return_value=False)
    @patch("src.notifications.subprocess.run")
    def test_macos_escapes_quotes(self, mock_run, _mock_pyobjc, _mock_sys):
        send_notification('Say "hello"', 'It\'s a "test"')

        script = mock_run.call_args[0][0][2]
        assert '\\"hello\\"' in script
        assert '\\"test\\"' in script

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications._try_load_macos_pyobjc", return_value=True)
    @patch("src.notifications._send_macos_pyobjc", return_value=True)
    @patch("src.notifications._send_macos_osascript")
    def test_macos_pyobjc_preferred(
        self, mock_osascript, mock_pyobjc_send, _mock_try, _mock_sys
    ):
        """When pyobjc is available, osascript path is skipped entirely."""
        send_notification("Title", "Body")
        mock_pyobjc_send.assert_called_once()
        mock_osascript.assert_not_called()

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications._try_load_macos_pyobjc", return_value=True)
    @patch("src.notifications._send_macos_pyobjc", return_value=False)
    @patch("src.notifications._send_macos_osascript")
    def test_macos_pyobjc_failure_falls_back(
        self, mock_osascript, _mock_pyobjc_send, _mock_try, _mock_sys
    ):
        """If the pyobjc call fails, we still try osascript."""
        send_notification("Title", "Body")
        mock_osascript.assert_called_once()

    @patch("src.notifications.platform.system", return_value="Windows")
    @patch("src.notifications.subprocess.run")
    def test_windows_calls_powershell(self, mock_run, _mock_sys):
        send_notification("Title", "Body")

        mock_run.assert_called_once()
        args = mock_run.call_args
        assert args[0][0][0] == "powershell"

    @patch("src.notifications.platform.system", return_value="FreeBSD")
    @patch("src.notifications.subprocess.run")
    def test_unsupported_platform_no_error(self, mock_run, _mock_sys):
        # macOS/Windows/Linux are all supported; anything else is a silent no-op.
        send_notification("Title", "Body")
        mock_run.assert_not_called()

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications._try_load_macos_pyobjc", return_value=False)
    @patch("src.notifications.subprocess.run", side_effect=Exception("fail"))
    def test_exception_is_swallowed(self, _mock_run, _mock_pyobjc, _mock_sys):
        # Should not raise
        send_notification("Title", "Body")


class TestClearNotifications:
    """Tests for clear_notifications()."""

    def setup_method(self) -> None:
        # Reset the pyobjc-availability cache so each test controls it.
        notifications_module._MACOS_PYOBJC_AVAILABLE = None

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications._try_load_macos_pyobjc", return_value=True)
    @patch("src.notifications._clear_macos_pyobjc")
    def test_macos_clears_via_pyobjc(self, mock_clear, _mock_try, _mock_sys):
        """macOS clear calls the pyobjc path when pyobjc is available."""
        clear_notifications()
        mock_clear.assert_called_once()

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications._try_load_macos_pyobjc", return_value=False)
    @patch("src.notifications._clear_macos_pyobjc")
    def test_macos_clear_noop_without_pyobjc(
        self, mock_clear, _mock_try, _mock_sys
    ):
        """With no pyobjc, clear is a no-op (can't clear Script Editor notifications)."""
        clear_notifications()
        mock_clear.assert_not_called()

    @patch("src.notifications.platform.system", return_value="Windows")
    @patch("src.notifications.subprocess.run")
    def test_windows_clears_toast(self, mock_run, _mock_sys):
        clear_notifications()
        mock_run.assert_called_once()
        ps_command = mock_run.call_args[1].get("args", mock_run.call_args[0][0])
        assert any("History" in str(arg) for arg in ps_command)
