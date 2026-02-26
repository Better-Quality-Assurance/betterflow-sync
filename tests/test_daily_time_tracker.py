"""Tests for daily time tracker."""

import pytest
import sqlite3
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from src.sync.daily_time_tracker import DailyTimeTracker


class TestDailyTimeTracker:
    """Tests for DailyTimeTracker."""

    def setup_method(self):
        """Set up test fixtures with a temp database."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_daily_time.db"
        self.tracker = DailyTimeTracker(db_path=self.db_path)
        self.today = date.today()

    def teardown_method(self):
        """Clean up after tests."""
        self.tracker.close()
        if self.db_path.exists():
            self.db_path.unlink()

    def test_initial_time_is_zero(self):
        """Initial active time should be zero."""
        active_time = self.tracker.get_today_active_time()

        assert active_time == timedelta(seconds=0)

    def test_add_active_time(self):
        """Adding active time should accumulate correctly."""
        self.tracker.add_active_time(60.0, self.today)  # 1 minute
        self.tracker.add_active_time(120.0, self.today)  # 2 minutes

        active_time = self.tracker.get_today_active_time()

        assert active_time == timedelta(seconds=180)

    def test_add_zero_time_ignored(self):
        """Adding zero or negative time should be ignored."""
        self.tracker.add_active_time(60.0, self.today)
        self.tracker.add_active_time(0.0, self.today)
        self.tracker.add_active_time(-10.0, self.today)

        active_time = self.tracker.get_today_active_time()

        assert active_time == timedelta(seconds=60)

    def test_persistence_across_instances(self):
        """Time should persist across tracker instances."""
        self.tracker.add_active_time(300.0, self.today)
        self.tracker.close()

        # Create new tracker with same database
        new_tracker = DailyTimeTracker(db_path=self.db_path)

        active_time = new_tracker.get_today_active_time()
        new_tracker.close()

        assert active_time == timedelta(seconds=300)

    def test_day_rollover_resets(self):
        """Time should reset when day changes."""
        yesterday = self.today - timedelta(days=1)

        # Add time for "yesterday"
        self.tracker.add_active_time(3600.0, yesterday)

        # Add time for "today"
        self.tracker.add_active_time(60.0, self.today)

        active_time = self.tracker.get_today_active_time()

        # Should only show today's time
        assert active_time == timedelta(seconds=60)

    def test_day_rollover_preserves_previous_day(self):
        """Previous day's data should still be queryable."""
        yesterday = self.today - timedelta(days=1)

        # Add time for yesterday
        self.tracker.add_active_time(3600.0, yesterday)

        # Add time for today
        self.tracker.add_active_time(60.0, self.today)

        # Query yesterday's time
        yesterday_time = self.tracker.get_active_time_for_date(yesterday)

        assert yesterday_time == timedelta(seconds=3600)

    def test_get_active_time_for_date_unknown_date(self):
        """Getting time for unknown date should return zero."""
        unknown_date = date(2020, 1, 1)

        active_time = self.tracker.get_active_time_for_date(unknown_date)

        assert active_time == timedelta(seconds=0)

    def test_multiple_days_tracked(self):
        """Multiple days should be tracked independently."""
        day1 = self.today - timedelta(days=2)
        day2 = self.today - timedelta(days=1)
        day3 = self.today

        self.tracker.add_active_time(100.0, day1)
        self.tracker.add_active_time(200.0, day2)
        self.tracker.add_active_time(300.0, day3)

        assert self.tracker.get_active_time_for_date(day1) == timedelta(seconds=100)
        assert self.tracker.get_active_time_for_date(day2) == timedelta(seconds=200)
        assert self.tracker.get_active_time_for_date(day3) == timedelta(seconds=300)

    @patch.object(DailyTimeTracker, "_get_local_date")
    def test_midnight_rollover(self, mock_get_date):
        """Midnight rollover should reset today's counter."""
        yesterday = self.today - timedelta(days=1)

        # Simulate yesterday
        mock_get_date.return_value = yesterday
        tracker = DailyTimeTracker(db_path=self.db_path)
        tracker.add_active_time(3600.0, yesterday)

        # Simulate today (midnight passed)
        mock_get_date.return_value = self.today

        active_time = tracker.get_today_active_time()
        tracker.close()

        # Should be zero since we haven't tracked today yet
        assert active_time == timedelta(seconds=0)

    def test_fractional_seconds(self):
        """Fractional seconds should be handled correctly."""
        self.tracker.add_active_time(45.5, self.today)
        self.tracker.add_active_time(30.3, self.today)

        active_time = self.tracker.get_today_active_time()

        assert active_time == timedelta(seconds=75.8)

    def test_large_accumulated_time(self):
        """Large accumulated times should be handled correctly."""
        # Add 8 hours in 1-hour chunks
        for _ in range(8):
            self.tracker.add_active_time(3600.0, self.today)

        active_time = self.tracker.get_today_active_time()

        assert active_time == timedelta(hours=8)

    def test_thread_safety(self):
        """Multiple threads should be able to use tracker safely."""
        import threading
        import time

        errors = []

        def add_time():
            try:
                for _ in range(100):
                    self.tracker.add_active_time(1.0, self.today)
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_time) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

        # Total should be 5 threads * 100 iterations * 1 second
        active_time = self.tracker.get_today_active_time()
        assert active_time == timedelta(seconds=500)

    def test_database_schema_created(self):
        """Database should have the correct schema."""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        # Check table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='daily_active_time'"
        )
        assert cursor.fetchone() is not None

        # Check columns
        cursor.execute("PRAGMA table_info(daily_active_time)")
        columns = {row[1] for row in cursor.fetchall()}
        assert columns == {"date", "active_seconds", "updated_at"}

        conn.close()

    def test_close_idempotent(self):
        """Closing multiple times should not raise errors."""
        self.tracker.close()
        self.tracker.close()  # Should not raise

    def test_adding_after_close_creates_new_connection(self):
        """Adding time after close should work with new connection."""
        self.tracker.add_active_time(60.0, self.today)
        self.tracker.close()

        # Should create new connection automatically
        self.tracker.add_active_time(60.0, self.today)

        # Re-open to verify
        new_tracker = DailyTimeTracker(db_path=self.db_path)
        active_time = new_tracker.get_today_active_time()
        new_tracker.close()

        assert active_time == timedelta(seconds=120)
