"""Tests for sync engine."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, MagicMock, patch

from src.config import Config, PrivacySettings
from src.sync.aw_client import AWEvent, BUCKET_TYPE_WINDOW, BUCKET_TYPE_AFK, BUCKET_TYPE_INPUT
from src.sync.sync_engine import SyncEngine
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.daily_time_tracker import DailyTimeTracker


class TestSyncEngine:
    """Tests for SyncEngine."""

    def setup_method(self):
        """Set up test fixtures."""
        self.aw = Mock()
        self.bf = Mock()
        self.queue = Mock()
        # Mock the exclude_apps as a list so "in" checks work
        self.queue.get_checkpoint.return_value = None
        self.config = Config()

        # Create mock activity analyzer and time tracker
        self.activity_analyzer = Mock(spec=ActivityAnalyzer)
        self.activity_analyzer.get_activity_state.return_value = "active"
        self.activity_analyzer.get_raw_metrics.return_value = Mock(
            to_dict=lambda: {"presses": 0, "clicks": 0, "scrolls": 0, "window_changes": 0}
        )

        self.time_tracker = Mock(spec=DailyTimeTracker)
        self.time_tracker.get_today_active_time.return_value = timedelta(hours=1)

        self.engine = SyncEngine(
            aw=self.aw,
            bf=self.bf,
            queue=self.queue,
            config=self.config,
            activity_analyzer=self.activity_analyzer,
            time_tracker=self.time_tracker,
        )

    def test_pause_resume(self):
        """Test pause and resume functionality."""
        assert self.engine.is_paused is False

        self.engine.pause()
        assert self.engine.is_paused is True

        self.engine.resume()
        assert self.engine.is_paused is False

    def test_sync_when_paused(self):
        """Test that sync does nothing when paused."""
        self.engine.pause()
        stats = self.engine.sync()

        assert stats.events_fetched == 0
        self.aw.is_running.assert_not_called()

    def test_sync_when_aw_not_running(self):
        """Test sync fails gracefully when AW is down."""
        self.aw.is_running.return_value = False
        self.bf.is_reachable.return_value = False

        stats = self.engine.sync()

        assert "ActivityWatch is not running" in stats.errors

    def test_transform_event_filters_short_events(self):
        """Test that very short events are filtered."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=0.4,  # Less than 0.5 seconds
            data={"app": "Test"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)
        assert result is None

    def test_transform_event_filters_excluded_apps(self):
        """Test that excluded apps are filtered."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "1Password"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)
        assert result is None

    def test_transform_event_includes_title(self):
        """Test that window events include title."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Firefox", "title": "Some Title - Mozilla Firefox"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)

        assert result is not None
        # Title is passed through (server handles privacy)
        assert result["data"]["title"] == "Some Title - Mozilla Firefox"

    def test_transform_event_extracts_domain_from_url(self):
        """Test that URLs are stripped to domain only."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={
                "app": "Chrome",
                "title": "Test",
                "url": "https://github.com/BetterQA/betterflow/pull/123",
            },
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)

        assert result is not None
        assert result["data"]["url"] == "github.com"

    def test_transform_event_handles_afk_bucket(self):
        """Test transforming AFK events."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=300,
            data={"status": "not-afk"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_AFK)

        assert result is not None
        assert result["data"]["status"] == "not-afk"

    def test_transform_event_adds_activity_state_for_window_events(self):
        """Test that window events include activity state and metrics."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Firefox", "title": "Test"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)

        assert result is not None
        assert "activity_state" in result
        assert "activity_metrics" in result
        assert result["activity_state"] == "active"

    def test_transform_event_tracks_active_time_for_active_events(self):
        """Test that active events add time to tracker."""
        self.activity_analyzer.get_activity_state.return_value = "active"

        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Firefox", "title": "Test"},
        )

        self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)

        # Should call add_active_time with duration and date
        self.time_tracker.add_active_time.assert_called_once()
        args = self.time_tracker.add_active_time.call_args[0]
        assert args[0] == 60  # duration

    def test_transform_event_does_not_track_idle_active_time(self):
        """Test that idle-active events don't add time to tracker."""
        self.activity_analyzer.get_activity_state.return_value = "idle-active"

        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Firefox", "title": "Test"},
        )

        self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)

        # Should not call add_active_time
        self.time_tracker.add_active_time.assert_not_called()

    def test_transform_event_input_bucket_no_activity_state(self):
        """Test that input events don't get activity state."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=1,
            data={"presses": 10, "clicks": 5, "scrolls": 2},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_INPUT)

        assert result is not None
        assert "activity_state" not in result
        assert result["data"]["presses"] == 10

    def test_get_status(self):
        """Test getting sync status."""
        self.aw.is_running.return_value = True
        self.bf.is_reachable.return_value = True
        self.queue.size.return_value = 10
        self.queue.get_all_checkpoints.return_value = {}

        status = self.engine.get_status()

        assert status["aw_running"] is True
        assert status["bf_reachable"] is True
        assert status["queue_size"] == 10

    def test_get_today_active_time(self):
        """Test getting today's active time from tracker."""
        expected = timedelta(hours=2, minutes=30)
        self.time_tracker.get_today_active_time.return_value = expected

        result = self.engine.get_today_active_time()

        assert result == expected

    def test_shutdown_ends_session(self):
        """Test that shutdown ends the active session."""
        self.engine._session_active = True

        self.engine.shutdown()

        self.bf.end_session.assert_called_once_with("app_quit")
        assert self.engine._session_active is False
        self.time_tracker.close.assert_called_once()

    def test_fetch_server_config_updates_analyzer_thresholds(self):
        """Test that fetching server config updates analyzer thresholds."""
        self.bf.get_config.return_value = {
            "engagement": {
                "sustained_typing_presses": 100,
                "window_changes_min": 3,
            }
        }

        self.engine.fetch_server_config()

        self.activity_analyzer.update_thresholds.assert_called_once()
