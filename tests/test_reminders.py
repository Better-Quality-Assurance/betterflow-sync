"""Tests for ReminderManager."""

from unittest.mock import patch

from src.config import ReminderSettings
from src.reminders import ReminderManager


class TestBreakReminders:
    """Tests for break-time reminder logic."""

    def setup_method(self):
        self.settings = ReminderSettings(
            break_reminders_enabled=True,
            break_interval_hours=2,
            private_reminders_enabled=True,
            private_interval_minutes=20,
        )
        self.mgr = ReminderManager(self.settings)

    @patch("src.reminders.send_notification")
    @patch("src.reminders.time.monotonic")
    def test_break_fires_after_interval(self, mock_mono, mock_notify):
        mock_mono.return_value = 0.0
        self.mgr.on_tracking_started()

        # Just under 2 hours — no notification
        mock_mono.return_value = 7199.0
        self.mgr.check()
        mock_notify.assert_not_called()

        # At 2 hours — fires
        mock_mono.return_value = 7200.0
        self.mgr.check()
        mock_notify.assert_called_once()
        assert "break" in mock_notify.call_args[0][0].lower()

    @patch("src.reminders.send_notification")
    @patch("src.reminders.time.monotonic")
    def test_break_repeats_at_interval(self, mock_mono, mock_notify):
        mock_mono.return_value = 0.0
        self.mgr.on_tracking_started()

        # 2h mark
        mock_mono.return_value = 7200.0
        self.mgr.check()
        assert mock_notify.call_count == 1

        # 3h mark — not yet another interval since last notification
        mock_mono.return_value = 10800.0
        self.mgr.check()
        assert mock_notify.call_count == 1

        # 4h mark — another interval passed since last notification at 2h
        mock_mono.return_value = 14400.0
        self.mgr.check()
        assert mock_notify.call_count == 2

    @patch("src.reminders.send_notification")
    @patch("src.reminders.time.monotonic")
    def test_break_resets_on_pause(self, mock_mono, mock_notify):
        mock_mono.return_value = 0.0
        self.mgr.on_tracking_started()

        # Work for 1.5h then pause
        mock_mono.return_value = 5400.0
        self.mgr.on_tracking_stopped()

        # Resume and work another 1.5h (total 3h wall time, but only 1.5h session)
        mock_mono.return_value = 10800.0
        self.mgr.on_tracking_started()

        mock_mono.return_value = 16200.0  # 1.5h into new session
        self.mgr.check()
        mock_notify.assert_not_called()

        # 2h into new session — fires
        mock_mono.return_value = 18000.0
        self.mgr.check()
        mock_notify.assert_called_once()

    @patch("src.reminders.send_notification")
    @patch("src.reminders.time.monotonic")
    def test_disabled_break_reminders(self, mock_mono, mock_notify):
        self.settings.break_reminders_enabled = False
        self.mgr.update_settings(self.settings)

        mock_mono.return_value = 0.0
        self.mgr.on_tracking_started()

        mock_mono.return_value = 14400.0  # 4 hours
        self.mgr.check()
        mock_notify.assert_not_called()


class TestPrivateReminders:
    """Tests for private-time reminder logic."""

    def setup_method(self):
        self.settings = ReminderSettings(
            break_reminders_enabled=True,
            break_interval_hours=2,
            private_reminders_enabled=True,
            private_interval_minutes=20,
        )
        self.mgr = ReminderManager(self.settings)

    @patch("src.reminders.send_notification")
    @patch("src.reminders.time.monotonic")
    def test_private_fires_after_interval(self, mock_mono, mock_notify):
        mock_mono.return_value = 0.0
        self.mgr.on_private_started()

        # Under 20 minutes
        mock_mono.return_value = 1199.0
        self.mgr.check()
        mock_notify.assert_not_called()

        # At 20 minutes
        mock_mono.return_value = 1200.0
        self.mgr.check()
        mock_notify.assert_called_once()
        assert "private" in mock_notify.call_args[0][0].lower()

    @patch("src.reminders.send_notification")
    @patch("src.reminders.time.monotonic")
    def test_private_repeats(self, mock_mono, mock_notify):
        mock_mono.return_value = 0.0
        self.mgr.on_private_started()

        # 20m
        mock_mono.return_value = 1200.0
        self.mgr.check()
        assert mock_notify.call_count == 1

        # 40m
        mock_mono.return_value = 2400.0
        self.mgr.check()
        assert mock_notify.call_count == 2

    @patch("src.reminders.send_notification")
    @patch("src.reminders.time.monotonic")
    def test_private_stops_on_end(self, mock_mono, mock_notify):
        mock_mono.return_value = 0.0
        self.mgr.on_private_started()

        mock_mono.return_value = 600.0  # 10m
        self.mgr.on_private_ended()

        # Even after long time, no notification
        mock_mono.return_value = 7200.0
        self.mgr.check()
        mock_notify.assert_not_called()

    @patch("src.reminders.send_notification")
    @patch("src.reminders.time.monotonic")
    def test_disabled_private_reminders(self, mock_mono, mock_notify):
        self.settings.private_reminders_enabled = False
        self.mgr.update_settings(self.settings)

        mock_mono.return_value = 0.0
        self.mgr.on_private_started()

        mock_mono.return_value = 3600.0
        self.mgr.check()
        mock_notify.assert_not_called()


class TestSettingsUpdate:
    """Tests for live settings update."""

    @patch("src.reminders.send_notification")
    @patch("src.reminders.time.monotonic")
    def test_update_break_interval(self, mock_mono, mock_notify):
        settings = ReminderSettings(break_interval_hours=1)
        mgr = ReminderManager(settings)

        mock_mono.return_value = 0.0
        mgr.on_tracking_started()

        # At 1h — fires with original 1h interval
        mock_mono.return_value = 3600.0
        mgr.check()
        assert mock_notify.call_count == 1

        # Change interval to 4h
        settings.break_interval_hours = 4
        mgr.update_settings(settings)

        # At 2h — doesn't fire because new interval is 4h
        mock_mono.return_value = 7200.0
        mgr.check()
        assert mock_notify.call_count == 1
