"""Tests for sync engine."""

import threading
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, MagicMock, patch

from src.config import Config, PrivacySettings
from src.sync.aw_client import AWEvent, BUCKET_TYPE_WINDOW, BUCKET_TYPE_AFK, BUCKET_TYPE_INPUT
from src.sync.sync_engine import SyncEngine
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.daily_time_tracker import DailyTimeTracker
from src.main import SyncCoordinator
from src.reminders import ReminderManager
from src.ui.tray import TrayState


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
        self.activity_analyzer.get_fraud_assessment.return_value = Mock(
            score=0,
            signals=[],
            extra_metrics={"unique_apps": 0, "keystroke_variance": None},
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

    def test_transform_event_filters_short_window_events(self):
        """Test that window events below min_window_event_seconds (5s) are filtered."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=4.0,  # Below default 5s threshold
            data={"app": "Test"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)
        assert result is None

    def test_transform_event_short_input_not_filtered(self):
        """Test that short input events (< 5s) are NOT filtered (only window events are)."""
        event = AWEvent(
            id=2,
            timestamp=datetime.now(timezone.utc),
            duration=1.0,  # Below 5s but above 0.5s
            data={"presses": 10, "clicks": 5, "scrolls": 2},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_INPUT)
        assert result is not None
        assert result["data"]["presses"] == 10

    def test_transform_event_short_afk_not_filtered(self):
        """Test that short AFK events (< 5s) are NOT filtered."""
        event = AWEvent(
            id=3,
            timestamp=datetime.now(timezone.utc),
            duration=2.0,  # Below 5s but above 0.5s
            data={"status": "not-afk"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_AFK)
        assert result is not None
        assert result["data"]["status"] == "not-afk"

    def test_transform_event_window_at_threshold_passes(self):
        """Test that a window event exactly at the 5s threshold passes."""
        event = AWEvent(
            id=4,
            timestamp=datetime.now(timezone.utc),
            duration=5.0,
            data={"app": "Firefox", "title": "Test"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)
        assert result is not None

    def test_status_at_returns_covering_afk_status(self):
        """_status_at should return the status covering a timestamp."""
        now = datetime.now(timezone.utc)
        afk_events = [
            AWEvent(
                id=10,
                timestamp=now - timedelta(minutes=5),
                duration=600,
                data={"status": "not-afk"},
            ),
        ]

        status = self.engine._status_at(now, afk_events)

        assert status == "not-afk"

    def test_transform_event_uses_not_afk_fallback_without_input(self):
        """Window events should stay active when AFK data says not-afk at event end."""
        self.engine._has_input_data = False
        now = datetime.now(timezone.utc)
        self.engine._current_afk_events = [
            AWEvent(
                id=11,
                timestamp=now - timedelta(minutes=1),
                duration=120,
                data={"status": "not-afk"},
            ),
        ]
        event = AWEvent(
            id=12,
            timestamp=now,
            duration=30.0,
            data={"app": "Terminal", "title": "Work"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)

        assert result is not None
        assert result["activity_state"] == "active"

    def test_transform_and_checkpoint_splits_window_around_afk(self):
        """Long window events should count only their non-AFK slices."""
        self.engine._has_input_data = False
        self.engine._afk_watcher_available = True
        now = datetime.now(timezone.utc)
        self.engine._latest_input_at = now
        self.engine._current_afk_events = [
            AWEvent(
                id=20,
                timestamp=now + timedelta(seconds=30),
                duration=30,
                data={"status": "afk"},
            ),
        ]
        event = AWEvent(
            id=21,
            timestamp=now,
            duration=90.0,
            data={"app": "Terminal", "title": "Work"},
        )
        stats = Mock(events_filtered=0)

        transformed, checkpoint = self.engine._transform_and_checkpoint(
            [event],
            "bucket-123",
            BUCKET_TYPE_WINDOW,
            stats,
        )

        assert checkpoint == ("bucket-123", now, 21)
        assert len(transformed) == 2
        assert transformed[0]["id"] == "21:0"
        assert transformed[0]["duration"] == 30.0
        assert transformed[1]["id"] == "21:1"
        assert transformed[1]["duration"] == 30.0
        assert all(item["activity_state"] == "active" for item in transformed)
        # Time tracking is per-event (sum of segments = 60s), not per-segment
        self.time_tracker.add_active_time.assert_called_once()
        assert self.time_tracker.add_active_time.call_args[0][0] == 60.0

    def test_transform_and_checkpoint_counts_full_window_without_afk(self):
        """Without AFK overlap, no-input windows should still count fully."""
        self.engine._has_input_data = False
        self.engine._afk_watcher_available = True
        self.engine._latest_input_at = datetime.now(timezone.utc)
        self.engine._current_afk_events = []
        now = datetime.now(timezone.utc)
        event = AWEvent(
            id=22,
            timestamp=now,
            duration=60.0,
            data={"app": "Terminal", "title": "Work"},
        )
        stats = Mock(events_filtered=0)

        transformed, _ = self.engine._transform_and_checkpoint(
            [event],
            "bucket-123",
            BUCKET_TYPE_WINDOW,
            stats,
        )

        assert len(transformed) == 1
        assert transformed[0]["duration"] == 60.0
        assert transformed[0]["activity_state"] == "active"
        self.time_tracker.add_active_time.assert_called_once()

    def test_transform_and_checkpoint_caps_counting_at_input_timeout(self):
        """Counted time must stop at last_input + afk_timeout."""
        self.engine._has_input_data = True
        self.engine._latest_input_at = datetime.now(timezone.utc)
        now = self.engine._latest_input_at - timedelta(minutes=5)
        event = AWEvent(
            id=23,
            timestamp=now,
            duration=20 * 60.0,
            data={"app": "Terminal", "title": "Work"},
        )
        stats = Mock(events_filtered=0)

        transformed, _ = self.engine._transform_and_checkpoint(
            [event],
            "bucket-123",
            BUCKET_TYPE_WINDOW,
            stats,
        )

        assert len(transformed) == 1
        assert transformed[0]["duration"] == 15 * 60.0
        self.time_tracker.add_active_time.assert_called_once()
        assert self.time_tracker.add_active_time.call_args[0][0] == 15 * 60.0

    def test_transform_and_checkpoint_skips_window_after_timeout(self):
        """A window fully beyond last_input + afk_timeout must not count."""
        self.engine._has_input_data = True
        self.engine._afk_watcher_available = False
        self.engine._latest_input_at = datetime.now(timezone.utc) - timedelta(minutes=11)
        event = AWEvent(
            id=24,
            timestamp=datetime.now(timezone.utc),
            duration=120.0,
            data={"app": "Terminal", "title": "Work"},
        )
        stats = Mock(events_filtered=0)

        transformed, _ = self.engine._transform_and_checkpoint(
            [event],
            "bucket-123",
            BUCKET_TYPE_WINDOW,
            stats,
        )

        assert transformed == []
        self.time_tracker.add_active_time.assert_not_called()

    def test_transform_and_checkpoint_uses_afk_when_input_is_stale(self):
        """AFK data should remain authoritative when input watcher goes stale."""
        now = datetime.now(timezone.utc)
        self.engine._has_input_data = True
        self.engine._afk_watcher_available = True
        self.engine._latest_input_at = now - timedelta(minutes=30)
        self.engine._current_afk_events = [
            AWEvent(
                id=25,
                timestamp=now - timedelta(minutes=40),
                duration=40 * 60.0,
                data={"status": "not-afk"},
            ),
        ]
        event = AWEvent(
            id=26,
            timestamp=now - timedelta(minutes=5),
            duration=300.0,
            data={"app": "Terminal", "title": "Work"},
        )
        stats = Mock(events_filtered=0)

        transformed, _ = self.engine._transform_and_checkpoint(
            [event],
            "bucket-123",
            BUCKET_TYPE_WINDOW,
            stats,
        )

        assert len(transformed) == 1
        assert transformed[0]["duration"] == 300.0
        self.time_tracker.add_active_time.assert_called_once()

    def test_get_category_db_only_returns_none_for_unmapped(self):
        """Test that _get_category only checks DB, returns None for unmapped apps."""
        self.queue.get_all_categories.return_value = {}
        self.engine._category_cache = None

        result = self.engine._get_category("Claude")
        assert result is None
        self.queue.set_category.assert_not_called()

    def test_transform_event_persists_fallback_category(self):
        """Test that fallback categories are persisted with source='fallback' via _transform_event."""
        self.queue.get_all_categories.return_value = {}
        self.engine._category_cache = None

        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Claude", "title": "Chat"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)
        assert result is not None
        assert result["data"]["app_category"] == "development"
        self.queue.set_category.assert_called_once_with("Claude", "development", source="fallback")

    def test_transform_event_persists_fallback_only_once(self):
        """Test that repeated events for the same app don't re-persist fallback."""
        self.queue.get_all_categories.return_value = {}
        self.engine._category_cache = None

        for i in range(3):
            event = AWEvent(
                id=i + 10,
                timestamp=datetime.now(timezone.utc),
                duration=60,
                data={"app": "Claude", "title": "Chat"},
            )
            self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)

        # Only one DB write despite three events
        self.queue.set_category.assert_called_once()

    def test_get_category_db_value_takes_precedence(self):
        """Test that a DB-cached value takes precedence over fallback."""
        self.queue.get_all_categories.return_value = {"Claude": "productivity"}
        self.engine._category_cache = None

        result = self.engine._get_category("Claude")
        assert result == "productivity"
        self.queue.set_category.assert_not_called()

    def test_get_category_double_miss_returns_none(self):
        """Test that _get_category returns None when both DB and fallback miss."""
        self.queue.get_all_categories.return_value = {}
        self.engine._category_cache = None

        result = self.engine._get_category("SomeObscureApp")
        assert result is None

    def test_get_category_empty_string_in_db_is_not_bypassed(self):
        """Test that an empty-string category from DB is returned (not treated as miss)."""
        self.queue.get_all_categories.return_value = {"Claude": ""}
        self.engine._category_cache = None

        result = self.engine._get_category("Claude")
        assert result == ""

    def test_db_sourced_category_not_repersisted_as_fallback(self):
        """Test that a DB-sourced category does NOT trigger fallback persistence."""
        self.queue.get_all_categories.return_value = {"Claude": "productivity"}
        self.engine._category_cache = None

        event = AWEvent(
            id=1, timestamp=datetime.now(timezone.utc), duration=60,
            data={"app": "Claude", "title": "Chat"},
        )
        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)
        assert result["data"]["app_category"] == "productivity"
        self.queue.set_category.assert_not_called()

    def test_persisted_fallbacks_cleared_on_shutdown(self):
        """_persisted_fallbacks must be cleared on shutdown for clean re-login."""
        self.queue.get_all_categories.return_value = {}
        self.engine._category_cache = None

        event = AWEvent(
            id=1, timestamp=datetime.now(timezone.utc), duration=60,
            data={"app": "Claude", "title": "Chat"},
        )
        self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)
        assert "Claude" in self.engine._persisted_fallbacks

        self.engine.shutdown()
        assert len(self.engine._persisted_fallbacks) == 0

    def test_invalidate_cache_clears_persisted_fallbacks(self):
        """invalidate_category_cache must also clear _persisted_fallbacks."""
        self.engine._persisted_fallbacks.add("Claude")
        self.engine.invalidate_category_cache()
        assert len(self.engine._persisted_fallbacks) == 0

    def test_transform_event_nan_duration_returns_none(self):
        """Test that NaN duration is rejected (would crash timedelta otherwise)."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=float("nan"),
            data={"app": "Test"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)
        assert result is None

    def test_transform_event_inf_duration_returns_none(self):
        """Test that infinite duration is rejected."""
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=float("inf"),
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

    def test_transform_event_enriches_browser_url_from_tracker(self):
        """A browser window event with no URL is enriched from the browser
        tracker, then domain-stripped by the default privacy policy."""

        class _StubTracker:
            def url_at(self, ts):
                return "https://github.com/Better-Quality-Assurance/x/pull/1"

        self.engine._browser_tracker = _StubTracker()
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Google Chrome", "title": "PR"},
        )

        result = self.engine._transform_event(event, "bucket-1", BUCKET_TYPE_WINDOW)

        assert result is not None
        # domain_only_urls=True by default -> domain, not the full URL
        assert result["data"]["url"] == "github.com"

    def test_transform_event_no_tracker_url_for_non_browser_app(self):
        """Non-browser apps are never queried for a URL, even with a tracker."""

        class _StubTracker:
            def url_at(self, ts):
                raise AssertionError("url_at must not be called for non-browsers")

        self.engine._browser_tracker = _StubTracker()
        event = AWEvent(
            id=2,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Terminal", "title": "zsh"},
        )

        result = self.engine._transform_event(event, "bucket-1", BUCKET_TYPE_WINDOW)

        assert result is not None
        assert "url" not in result["data"]

    def test_transform_event_afk_status_not_relabeled_as_break(self):
        """AFK (away-from-keyboard) must NOT be sent as a 'break'.

        A break is an intentional user-initiated pause (break_time). Blanket-
        relabeling AFK as break turned ordinary no-input work (reading, meetings,
        watching a screen) into phantom 'Break' cards for people who took no
        break. AFK keeps its real bucket_type so the backend classifies long
        spans as Idle.
        """
        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=600,
            data={"status": "afk"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_AFK)

        assert result is not None
        assert result["data"]["status"] == "afk"
        assert result["bucket_type"] == BUCKET_TYPE_AFK
        assert result["bucket_type"] != "break"

    def test_transform_event_adds_activity_state_for_window_events(self):
        """Test that window events include activity state and metrics."""
        self.engine._has_input_data = True
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
        self.engine._has_input_data = True
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

    def test_transform_event_tracks_idle_active_time(self):
        """Test that idle-active events still add counted time to tracker."""
        self.engine._has_input_data = True
        self.activity_analyzer.get_activity_state.return_value = "idle-active"

        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Firefox", "title": "Test"},
        )

        self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)

        self.time_tracker.add_active_time.assert_called_once()

    def test_transform_event_no_input_data_uses_afk_for_active(self):
        """Test that without input data, AFK 'not-afk' events mark window as active."""
        self.engine._has_input_data = False
        self.engine._afk_watcher_available = True
        now = datetime.now(timezone.utc)
        # AFK says user is active during this window
        self.engine._current_afk_events = [
            AWEvent(id=10, timestamp=now - timedelta(seconds=10), duration=120, data={"status": "not-afk"}),
        ]
        event = AWEvent(id=1, timestamp=now, duration=60, data={"app": "Firefox", "title": "Test"})

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)

        assert result is not None
        assert result["activity_state"] == "active"
        self.time_tracker.add_active_time.assert_called_once()

    def test_transform_event_no_input_data_uses_afk_for_inactive(self):
        """Test that without input data, AFK 'afk' events mark window as inactive."""
        self.engine._has_input_data = False
        self.engine._afk_watcher_available = True
        now = datetime.now(timezone.utc)
        # AFK says user is idle during this window
        self.engine._current_afk_events = [
            AWEvent(id=10, timestamp=now - timedelta(seconds=10), duration=120, data={"status": "afk"}),
        ]
        event = AWEvent(id=1, timestamp=now, duration=60, data={"app": "Firefox", "title": "Test"})

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)

        assert result is not None
        assert result["activity_state"] == "inactive"
        self.time_tracker.add_active_time.assert_not_called()

    def test_transform_event_no_input_data_no_afk_watcher_defaults_active(self):
        """Test that without AFK watcher, defaults to active.

        When the AFK watcher is completely down, window events still prove
        the user was at the computer. Defaulting to inactive would cause
        silent zero-hour days which is worse than slightly inflated counts.
        """
        self.engine._has_input_data = False
        self.engine._current_afk_events = []
        self.engine._afk_watcher_available = False
        event = AWEvent(
            id=1, timestamp=datetime.now(timezone.utc), duration=60,
            data={"app": "Firefox", "title": "Test"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)

        assert result is not None
        assert result["activity_state"] == "active"

    def test_transform_event_no_input_data_afk_watcher_no_events_defaults_inactive(self):
        """Test that with AFK watcher running but no matching events, defaults to inactive.

        When the AFK watcher is available but has no events covering this
        window event's time range, the user was genuinely idle.
        """
        self.engine._has_input_data = False
        self.engine._current_afk_events = []
        self.engine._afk_watcher_available = True
        event = AWEvent(
            id=1, timestamp=datetime.now(timezone.utc), duration=60,
            data={"app": "Firefox", "title": "Test"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)

        assert result is not None
        assert result["activity_state"] == "inactive"
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

    def test_transform_event_delta_time_tracking_on_refetch(self):
        """Test that re-fetched events with grown duration only add the delta."""
        self.engine._has_input_data = True
        self.activity_analyzer.get_activity_state.return_value = "active"

        event_v1 = AWEvent(
            id=42,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "VSCode", "title": "editor"},
        )

        # First fetch — full 60s should be added
        self.engine._transform_event(event_v1, "bucket-1", BUCKET_TYPE_WINDOW)
        self.time_tracker.add_active_time.assert_called_once()
        assert self.time_tracker.add_active_time.call_args[0][0] == 60

        self.time_tracker.add_active_time.reset_mock()

        # Re-fetch with grown duration (heartbeat extension: 60 → 90)
        event_v2 = AWEvent(
            id=42,
            timestamp=event_v1.timestamp,
            duration=90,
            data={"app": "VSCode", "title": "editor"},
        )
        self.engine._transform_event(event_v2, "bucket-1", BUCKET_TYPE_WINDOW)
        self.time_tracker.add_active_time.assert_called_once()
        assert self.time_tracker.add_active_time.call_args[0][0] == 30  # delta only

    def test_transform_event_no_double_count_same_duration(self):
        """Test that re-fetched events with same duration don't add time."""
        self.engine._has_input_data = True
        self.activity_analyzer.get_activity_state.return_value = "active"

        event = AWEvent(
            id=99,
            timestamp=datetime.now(timezone.utc),
            duration=120,
            data={"app": "Firefox", "title": "Test"},
        )

        # First fetch
        self.engine._transform_event(event, "bucket-1", BUCKET_TYPE_WINDOW)
        self.time_tracker.add_active_time.reset_mock()

        # Re-fetch with identical duration — delta is 0, should not call
        self.engine._transform_event(event, "bucket-1", BUCKET_TYPE_WINDOW)
        self.time_tracker.add_active_time.assert_not_called()

    def test_transform_event_different_buckets_track_separately(self):
        """Test that same event ID in different buckets tracks time independently."""
        self.engine._has_input_data = True
        self.activity_analyzer.get_activity_state.return_value = "active"

        event = AWEvent(
            id=1,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "Chrome", "title": "Test"},
        )

        # Same event ID, different bucket — both should add full duration
        self.engine._transform_event(event, "bucket-A", BUCKET_TYPE_WINDOW)
        self.engine._transform_event(event, "bucket-B", BUCKET_TYPE_WINDOW)

        assert self.time_tracker.add_active_time.call_count == 2
        for call in self.time_tracker.add_active_time.call_args_list:
            assert call[0][0] == 60

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

    def test_time_cache_uses_cache_lock(self):
        """Test that _time_cache operations are protected by _cache_lock."""
        import threading

        self.engine._has_input_data = True
        self.activity_analyzer.get_activity_state.return_value = "active"

        event = AWEvent(
            id=50,
            timestamp=datetime.now(timezone.utc),
            duration=60,
            data={"app": "VSCode", "title": "editor"},
        )

        # Replace _cache_lock with a counting wrapper
        acquire_count = 0
        real_lock = threading.Lock()

        class CountingLock:
            def acquire(self, *args, **kwargs):
                nonlocal acquire_count
                acquire_count += 1
                return real_lock.acquire(*args, **kwargs)

            def release(self, *args, **kwargs):
                return real_lock.release(*args, **kwargs)

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *args):
                self.release()

        self.engine._cache_lock = CountingLock()

        self.engine._transform_event(event, "bucket-1", BUCKET_TYPE_WINDOW)

        # _cache_lock should have been acquired for _time_cache operations
        # _transform_event accesses _time_cache when activity_state is "active"
        assert acquire_count >= 1, f"Expected >= 1 lock acquisition for _time_cache, got {acquire_count}"


class TestStatusSpanEvents:
    """Tests for break_time / idle_time / sleep_time / private_time emission."""

    def setup_method(self):
        from src.sync.bf_client import SyncResult
        self.aw = Mock()
        self.bf = Mock()
        self.bf.send_events.return_value = SyncResult(success=True, events_synced=1)
        self.queue = Mock()
        self.queue.get_checkpoint.return_value = None
        self.config = Config()
        self.activity_analyzer = Mock(spec=ActivityAnalyzer)
        self.time_tracker = Mock(spec=DailyTimeTracker)
        self.engine = SyncEngine(
            aw=self.aw, bf=self.bf, queue=self.queue, config=self.config,
            activity_analyzer=self.activity_analyzer, time_tracker=self.time_tracker,
        )

    def _captured_event(self):
        assert self.bf.send_events.call_count == 1
        events = self.bf.send_events.call_args[0][0]
        assert len(events) == 1
        return events[0]

    def test_send_sleep_event_emits_sleep_time_bucket(self):
        """sleep_time bucket distinguishes machine-asleep from user-idle."""
        start = datetime(2026, 6, 9, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 6, 9, 6, 30, 0, tzinfo=timezone.utc)

        self.engine.send_sleep_event(start, end)

        ev = self._captured_event()
        assert ev["bucket_type"] == "sleep_time"
        assert ev["data"]["status"] == "sleep"
        assert ev["duration"] == pytest.approx(6.5 * 3600)
        assert ev["id"].startswith("sleep_")

    def test_send_sleep_event_queues_on_network_failure(self):
        """Network failure paths through queue, not silent drop."""
        from src.sync.bf_client import SyncResult
        self.bf.send_events.return_value = SyncResult(success=False, error="network down")
        start = datetime(2026, 6, 9, 0, 0, 0, tzinfo=timezone.utc)

        self.engine.send_sleep_event(start, start + timedelta(hours=6))

        self.queue.enqueue.assert_called_once()
        queued = self.queue.enqueue.call_args[0][0]
        assert queued[0]["bucket_type"] == "sleep_time"

    def test_send_sleep_event_with_end_before_start_is_dropped_with_warning(self, caplog):
        """NTP step backwards during a long Mac sleep could make end<start.

        Without the explicit log, the existing `if duration < 1: return`
        silently discarded the entire span (potentially hours long). We
        still drop the event — submitting negative durations would poison
        the server aggregator — but the warning makes the drop observable.
        """
        import logging
        start = datetime(2026, 6, 9, 8, 0, 0, tzinfo=timezone.utc)
        end = start - timedelta(seconds=30)
        with caplog.at_level(logging.WARNING, logger="src.sync.sync_engine"):
            self.engine.send_sleep_event(start, end)
        self.bf.send_events.assert_not_called()
        self.queue.enqueue.assert_not_called()
        assert any("NTP clock correction" in r.message for r in caplog.records), (
            "negative-duration drop must log a warning so it is observable"
        )

    def test_sleep_idle_break_get_distinct_id_prefixes_and_bucket_types(self):
        """Status spans with same start must not collide on id OR bucket_type.

        Asserting `bucket_type` for break/idle (not just sleep) protects
        against a regression where _send_status_span was hardcoded to one
        kind — the id-prefix-only check alone would silently miss it.
        """
        from src.sync.bf_client import SyncResult
        self.bf.send_events.return_value = SyncResult(success=True, events_synced=1)
        start = datetime(2026, 6, 9, 0, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(minutes=30)

        self.engine.send_idle_event(start, end)
        idle_ev = self.bf.send_events.call_args[0][0][0]
        self.bf.send_events.reset_mock()

        self.engine.send_sleep_event(start, end)
        sleep_ev = self.bf.send_events.call_args[0][0][0]
        self.bf.send_events.reset_mock()

        self.engine.send_break_event(start, end)
        break_ev = self.bf.send_events.call_args[0][0][0]

        # Ids
        assert idle_ev["id"].startswith("idle_")
        assert sleep_ev["id"].startswith("sleep_")
        assert break_ev["id"].startswith("break_")
        assert idle_ev["id"] != sleep_ev["id"] != break_ev["id"]
        # Bucket types — the field the server keys on for event_type inference
        assert idle_ev["bucket_type"] == "idle_time"
        assert sleep_ev["bucket_type"] == "sleep_time"
        assert break_ev["bucket_type"] == "break_time"
        # data.status (sent through to the timeline)
        assert idle_ev["data"]["status"] == "idle"
        assert sleep_ev["data"]["status"] == "sleep"
        assert break_ev["data"]["status"] == "break"


class TestSystemSleepWakeEmitsSleepSpan:
    """on_system_wake must emit a sleep_time event for the sleep span."""

    def _make_handler(self):
        from src.system_event_handler import SystemEventHandler
        sync_engine = Mock()
        sync_engine.is_private = False
        coordinator = Mock()
        coordinator.is_on_break = False
        coordinator.paused_by_network = False
        handler = SystemEventHandler(
            sync_engine=sync_engine,
            tray=Mock(),
            coordinator=coordinator,
            reminder_manager=Mock(),
            bf=Mock(),
            aw=Mock(),
            pause_state_lock=threading.RLock(),
            shutdown_fn=Mock(),
        )
        return handler, sync_engine

    def test_wake_emits_sleep_event_for_captured_span(self):
        handler, sync_engine = self._make_handler()
        handler.on_system_sleep()
        sleep_start = handler._sleep_start
        assert sleep_start is not None

        handler.on_system_wake()

        sync_engine.send_sleep_event.assert_called_once()
        passed_start = sync_engine.send_sleep_event.call_args[0][0]
        assert passed_start == sleep_start
        # Must be cleared so the next sleep doesn't reuse this start
        assert handler._sleep_start is None

    def test_wake_without_prior_sleep_does_not_emit(self):
        handler, sync_engine = self._make_handler()
        handler.on_system_wake()
        sync_engine.send_sleep_event.assert_not_called()

    def test_two_sleeps_without_wake_keep_earliest_timestamp(self):
        """macOS can fire Display Sleep then System Sleep without a wake between.

        Overwriting _sleep_start would silently truncate the front of the
        sleep span — emit the *earliest* timestamp instead.
        """
        handler, sync_engine = self._make_handler()
        handler.on_system_sleep()
        first_start = handler._sleep_start
        assert first_start is not None
        import time as _time
        _time.sleep(0.01)  # ensure a measurable timestamp difference
        handler.on_system_sleep()
        second_start = handler._sleep_start
        assert second_start == first_start, (
            "second sleep without an intervening wake must NOT overwrite _sleep_start"
        )

    def test_send_sleep_event_failure_does_not_break_wake_flow(self):
        handler, sync_engine = self._make_handler()
        sync_engine.send_sleep_event.side_effect = RuntimeError("queue full")
        handler.on_system_sleep()
        # Must not raise — wake flow continues even if event emission fails.
        handler.on_system_wake()
        sync_engine.resume.assert_called_once()


class TestSyncCoordinatorBreak:
    """Tests for SyncCoordinator break state management."""

    def setup_method(self):
        self.config = Config()
        self.aw = Mock()
        self.bf = Mock()
        self.queue = Mock()
        self.queue.get_checkpoint.return_value = None
        self.sync_engine = Mock(spec=SyncEngine)
        self.sync_engine.is_paused = False
        self.sync_engine.is_private = False
        self.tray = Mock()
        self.tray.model = Mock()
        self.tray.model.lock = threading.RLock()
        self.tray.model.on_break = False
        self.tray.model.break_minutes_left = 0
        self.tray.model.needs_permissions = False
        self.tray.model.state = TrayState.SYNCING
        self.aw_manager = Mock()
        self.reminder = Mock(spec=ReminderManager)
        self.coordinator = SyncCoordinator(
            config=self.config,
            aw=self.aw,
            bf=self.bf,
            queue=self.queue,
            sync_engine=self.sync_engine,
            tray=self.tray,
            aw_manager=self.aw_manager,
            reminder_manager=self.reminder,
        )
        self.coordinator.scheduler = Mock()
        self.coordinator.scheduler.running = True

    def test_start_break_sets_flag(self):
        """start_break sets _on_break."""
        self.coordinator.start_break()
        assert self.coordinator.break_mgr._on_break is True
        self.sync_engine.pause.assert_called_once()

    def test_double_start_break_is_idempotent(self):
        """Second start_break is a no-op."""
        self.coordinator.start_break()
        self.coordinator.start_break()
        self.sync_engine.pause.assert_called_once()

    def test_end_break_clears_flag(self):
        """end_break clears _on_break."""
        self.coordinator.break_mgr._on_break = True
        self.coordinator.break_mgr._break_start = datetime.now(timezone.utc) - timedelta(seconds=120)
        self.coordinator.break_mgr._pre_break_paused = False
        self.coordinator.break_mgr._pre_break_private = False
        self.coordinator.end_break()
        assert self.coordinator.break_mgr._on_break is False
        self.sync_engine.resume.assert_called_once()

    def test_end_break_restores_paused_state(self):
        """end_break should not resume if app was paused before break."""
        self.coordinator.break_mgr._on_break = True
        self.coordinator.break_mgr._break_start = datetime.now(timezone.utc) - timedelta(seconds=120)
        self.coordinator.break_mgr._pre_break_paused = True
        self.coordinator.break_mgr._pre_break_private = False

        self.coordinator.end_break()

        self.sync_engine.resume.assert_not_called()
        self.tray.set_state.assert_called_with(TrayState.PAUSED)

    def test_end_break_restores_private_state(self):
        """end_break should resume the engine then re-enable private mode.

        start_break() pauses the engine before enabling private mode, so
        end_break() must un-pause it (resume) before re-applying private mode.
        This ensures both flags are consistent: paused=False, private=True.
        """
        self.coordinator.break_mgr._on_break = True
        self.coordinator.break_mgr._break_start = datetime.now(timezone.utc) - timedelta(seconds=120)
        self.coordinator.break_mgr._pre_break_paused = False
        self.coordinator.break_mgr._pre_break_private = True

        self.coordinator.end_break()

        self.sync_engine.resume.assert_called_once()
        self.sync_engine.set_private_mode.assert_called_once_with(True)
        self.tray.set_state.assert_called_with(TrayState.PRIVATE)

    def test_end_break_silent_no_notification(self):
        """end_break(silent=True) should not send notification."""
        self.coordinator.break_mgr._on_break = True
        self.coordinator.break_mgr._break_start = datetime.now(timezone.utc) - timedelta(seconds=120)
        self.coordinator.break_mgr._pre_break_paused = False
        self.coordinator.break_mgr._pre_break_private = False

        with patch("src.break_manager.send_notification") as mock_notify:
            self.coordinator.end_break(silent=True)
            mock_notify.assert_not_called()

    def test_end_break_too_soon_rejected_without_force(self):
        """end_break() is a no-op when called < 60s after start (scheduler race guard)."""
        self.coordinator.break_mgr._on_break = True
        self.coordinator.break_mgr._break_start = datetime.now(timezone.utc) - timedelta(seconds=10)
        self.coordinator.break_mgr._pre_break_paused = False
        self.coordinator.break_mgr._pre_break_private = False

        self.coordinator.end_break(force=False)

        assert self.coordinator.break_mgr._on_break is True
        self.sync_engine.resume.assert_not_called()

    def test_end_break_force_bypasses_60s_guard(self):
        """end_break(force=True) succeeds even when < 60s have elapsed."""
        self.coordinator.break_mgr._on_break = True
        self.coordinator.break_mgr._break_start = datetime.now(timezone.utc) - timedelta(seconds=5)
        self.coordinator.break_mgr._pre_break_paused = False
        self.coordinator.break_mgr._pre_break_private = False

        self.coordinator.end_break(force=True)

        assert self.coordinator.break_mgr._on_break is False
        self.sync_engine.resume.assert_called_once()

    def test_end_break_no_timestamp_without_force_rejected(self):
        """end_break() is a no-op when _break_start is None and force=False."""
        self.coordinator.break_mgr._on_break = True
        self.coordinator.break_mgr._break_start = None
        self.coordinator.break_mgr._pre_break_paused = False
        self.coordinator.break_mgr._pre_break_private = False

        self.coordinator.end_break(force=False)

        assert self.coordinator.break_mgr._on_break is True
        self.sync_engine.resume.assert_not_called()


class TestConfigDefaultCategories:
    """Tests for default_categories save/load round-trip."""

    def test_save_load_roundtrip_restores_default_categories(self, tmp_path, monkeypatch):
        """default_categories must come from dataclass defaults, not config.json."""
        monkeypatch.setattr(Config, "get_config_dir", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(Config, "get_config_file", classmethod(lambda cls: tmp_path / "config.json"))
        config = Config()
        assert "Claude" in config.privacy.default_categories
        config.save()
        loaded = Config.load()
        assert "Claude" in loaded.privacy.default_categories
