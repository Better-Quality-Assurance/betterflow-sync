"""Tests for OS notification delivery."""

from unittest.mock import patch

from src.notifications import clear_notifications, send_notification


class TestSendNotification:
    """Tests for send_notification()."""

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications.subprocess.run")
    def test_macos_uses_osascript(self, mock_run, _mock_sys):
        """macOS sends via osascript display notification."""
        send_notification("Title", "Body")

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "osascript"
        assert 'display notification "Body" with title "Title"' in args[2]
        assert 'sound name "default"' in args[2]

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications.subprocess.run")
    def test_macos_no_sound(self, mock_run, _mock_sys):
        send_notification("Title", "Body", sound=False)

        script = mock_run.call_args[0][0][2]
        assert "sound name" not in script

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications.subprocess.run")
    def test_macos_escapes_quotes(self, mock_run, _mock_sys):
        send_notification('Say "hello"', 'It\'s a "test"')

        script = mock_run.call_args[0][0][2]
        assert '\\"hello\\"' in script
        assert '\\"test\\"' in script

    @patch("src.notifications.platform.system", return_value="Windows")
    @patch("src.notifications.subprocess.run")
    def test_windows_calls_powershell(self, mock_run, _mock_sys):
        send_notification("Title", "Body")

        mock_run.assert_called_once()
        args = mock_run.call_args
        assert args[0][0][0] == "powershell"

    @patch("src.notifications.platform.system", return_value="Linux")
    @patch("src.notifications.subprocess.run")
    def test_unsupported_platform_no_error(self, mock_run, _mock_sys):
        send_notification("Title", "Body")
        mock_run.assert_not_called()

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications.subprocess.run", side_effect=Exception("fail"))
    def test_exception_is_swallowed(self, _mock_run, _mock_sys):
        # Should not raise
        send_notification("Title", "Body")


class TestClearNotifications:
    """Tests for clear_notifications()."""

    @patch("src.notifications.platform.system", return_value="Darwin")
    def test_macos_is_noop(self, _mock_sys):
        """macOS clear is a no-op (osascript notifications can't be cleared)."""
        # Should not raise and should not call subprocess
        clear_notifications()

    @patch("src.notifications.platform.system", return_value="Windows")
    @patch("src.notifications.subprocess.run")
    def test_windows_clears_toast(self, mock_run, _mock_sys):
        clear_notifications()
        mock_run.assert_called_once()
        # The PowerShell command string is passed via -Command
        ps_command = mock_run.call_args[1].get("args", mock_run.call_args[0][0])
        assert any("History" in str(arg) for arg in ps_command)
