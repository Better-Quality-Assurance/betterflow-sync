"""Tests for OS notification delivery."""

from unittest.mock import patch

from src.notifications import send_notification


class TestSendNotification:
    """Tests for send_notification()."""

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications.subprocess.run")
    def test_macos_calls_osascript(self, mock_run, _mock_sys):
        send_notification("Title", "Body")

        mock_run.assert_called_once()
        args = mock_run.call_args
        assert args[0][0][0] == "osascript"
        assert args[0][0][1] == "-e"
        assert 'display notification "Body" with title "Title"' in args[0][0][2]
        assert 'sound name "default"' in args[0][0][2]

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
    @patch("src.notifications.subprocess.run", side_effect=OSError("fail"))
    def test_exception_is_swallowed(self, mock_run, _mock_sys):
        # Should not raise
        send_notification("Title", "Body")
