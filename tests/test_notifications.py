"""Tests for OS notification delivery."""

from unittest.mock import patch, MagicMock

from src.notifications import send_notification, clear_notifications


class TestSendNotification:
    """Tests for send_notification()."""

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications._get_mac_center")
    def test_macos_native_notification(self, mock_center_fn, _mock_sys):
        """NSUserNotificationCenter is used when available."""
        mock_center = MagicMock()
        mock_center_fn.return_value = mock_center

        mock_cls = MagicMock()
        mock_notification = MagicMock()
        mock_cls.alloc.return_value.init.return_value = mock_notification
        mock_objc = MagicMock()
        mock_objc.lookUpClass.return_value = mock_cls

        with patch.dict("sys.modules", {"objc": mock_objc}):
            send_notification("Title", "Body")

        mock_notification.setTitle_.assert_called_once_with("Title")
        mock_notification.setInformativeText_.assert_called_once_with("Body")
        mock_notification.setSoundName_.assert_called_once_with("default")
        mock_center.deliverNotification_.assert_called_once_with(mock_notification)

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications._get_mac_center")
    def test_macos_no_sound(self, mock_center_fn, _mock_sys):
        mock_center = MagicMock()
        mock_center_fn.return_value = mock_center

        mock_cls = MagicMock()
        mock_notification = MagicMock()
        mock_cls.alloc.return_value.init.return_value = mock_notification
        mock_objc = MagicMock()
        mock_objc.lookUpClass.return_value = mock_cls

        with patch.dict("sys.modules", {"objc": mock_objc}):
            send_notification("Title", "Body", sound=False)

        mock_notification.setSoundName_.assert_not_called()

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications._get_mac_center", return_value=None)
    @patch("src.notifications.subprocess.run")
    def test_macos_fallback_to_osascript(self, mock_run, _mock_center, _mock_sys):
        """Falls back to osascript when NSUserNotificationCenter is unavailable."""
        send_notification("Title", "Body")

        mock_run.assert_called_once()
        args = mock_run.call_args
        assert args[0][0][0] == "osascript"
        assert 'display notification "Body" with title "Title"' in args[0][0][2]

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications._get_mac_center", return_value=None)
    @patch("src.notifications.subprocess.run")
    def test_macos_escapes_quotes(self, mock_run, _mock_center, _mock_sys):
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
    @patch("src.notifications._get_mac_center", side_effect=Exception("fail"))
    def test_exception_is_swallowed(self, _mock_center, _mock_sys):
        # Should not raise
        send_notification("Title", "Body")


class TestClearNotifications:
    """Tests for clear_notifications()."""

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications._get_mac_center")
    def test_macos_clears_delivered(self, mock_center_fn, _mock_sys):
        mock_center = MagicMock()
        mock_center_fn.return_value = mock_center

        clear_notifications()

        mock_center.removeAllDeliveredNotifications.assert_called_once()

    @patch("src.notifications.platform.system", return_value="Darwin")
    @patch("src.notifications._get_mac_center", return_value=None)
    def test_macos_no_center_no_error(self, _mock_center, _mock_sys):
        # Should not raise
        clear_notifications()

    @patch("src.notifications.platform.system", return_value="Windows")
    @patch("src.notifications.subprocess.run")
    def test_windows_clears_toast(self, mock_run, _mock_sys):
        clear_notifications()
        mock_run.assert_called_once()
        # The PowerShell command string is passed via -Command
        ps_command = mock_run.call_args[1].get("args", mock_run.call_args[0][0])
        assert any("History" in str(arg) for arg in ps_command)
