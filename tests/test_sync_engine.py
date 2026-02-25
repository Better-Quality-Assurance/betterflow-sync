"""Tests for sync engine."""

import pytest
from datetime import datetime, timezone
from unittest.mock import Mock, MagicMock, patch

from src.config import Config, PrivacySettings
from src.sync.aw_client import AWEvent, BUCKET_TYPE_WINDOW, BUCKET_TYPE_AFK
from src.sync.sync_engine import SyncEngine


class TestSyncEngine:
    """Tests for SyncEngine."""

    def setup_method(self):
        """Set up test fixtures."""
        self.aw = Mock()
        self.bf = Mock()
        self.queue = Mock()
        self.config = Config()

        # bf.get_config returns a dict for server config updates
        self.bf.get_config.return_value = {}

        self.engine = SyncEngine(
            aw=self.aw,
            bf=self.bf,
            queue=self.queue,
            config=self.config,
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

        stats = self.engine.sync()

        assert "ActivityWatch is not running" in stats.errors

    def test_transform_event_filters_short_events(self):
        """Test that very short events are filtered."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=0.3,  # Less than 0.5 second threshold
            data={"app": "Test"},
        )

        result = self.engine._transform_event(event, "test-bucket", BUCKET_TYPE_WINDOW)
        assert result is None

    def test_transform_event_filters_excluded_apps(self):
        """Test that excluded apps are filtered."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "1Password"},
        )

        result = self.engine._transform_event(event, "test-bucket", BUCKET_TYPE_WINDOW)
        assert result is None

    def test_transform_event_sends_raw_title(self):
        """Test that window titles are sent raw (server handles privacy)."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Firefox", "title": "Secret Document - Mozilla Firefox"},
        )

        result = self.engine._transform_event(event, "test-bucket", BUCKET_TYPE_WINDOW)

        assert result is not None
        # Title should be sent raw — server handles privacy
        assert result["data"]["title"] == "Secret Document - Mozilla Firefox"

    def test_transform_event_allows_raw_title_for_allowlisted_apps(self):
        """Test that allowlisted apps keep raw titles."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Visual Studio Code", "title": "main.py - myproject"},
        )

        result = self.engine._transform_event(event, "test-bucket", BUCKET_TYPE_WINDOW)

        assert result is not None
        assert result["data"]["title"] == "main.py - myproject"

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

        result = self.engine._transform_event(event, "test-bucket", BUCKET_TYPE_WINDOW)

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

        result = self.engine._transform_event(event, "test-bucket", BUCKET_TYPE_AFK)

        assert result is not None
        assert result["data"]["status"] == "not-afk"

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

    def test_shutdown_ends_session(self):
        """Test that shutdown ends the active session."""
        self.engine._session_active = True

        self.engine.shutdown()

        self.bf.end_session.assert_called_once_with("app_quit")
        assert self.engine._session_active is False
