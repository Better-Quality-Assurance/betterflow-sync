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

    def test_transform_event_does_not_track_idle_active_time(self):
        """Test that idle-active events don't add time to tracker when input data exists."""
        self.engine._has_input_data = True
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

    def test_transform_event_no_input_data_uses_afk_for_active(self):
        """Test that without input data, AFK 'not-afk' events mark window as active."""
        self.engine._has_input_data = False
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

    def test_transform_event_no_input_data_no_afk_defaults_active(self):
        """Test that without input data AND no AFK events, defaults to active."""
        self.engine._has_input_data = False
        self.engine._current_afk_events = []
        event = AWEvent(
            id=1, timestamp=datetime.now(timezone.utc), duration=60,
            data={"app": "Firefox", "title": "Test"},
        )

        result = self.engine._transform_event(event, "bucket-123", BUCKET_TYPE_WINDOW)

        assert result is not None
        assert result["activity_state"] == "active"

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
        assert self.coordinator._on_break is True
        self.sync_engine.pause.assert_called_once()

    def test_double_start_break_is_idempotent(self):
        """Second start_break is a no-op."""
        self.coordinator.start_break()
        self.coordinator.start_break()
        self.sync_engine.pause.assert_called_once()

    def test_end_break_clears_flag(self):
        """end_break clears _on_break."""
        self.coordinator._on_break = True
        self.coordinator._pre_break_paused = False
        self.coordinator._pre_break_private = False
        self.coordinator.end_break()
        assert self.coordinator._on_break is False
        self.sync_engine.resume.assert_called_once()

    def test_end_break_restores_paused_state(self):
        """end_break should not resume if app was paused before break."""
        self.coordinator._on_break = True
        self.coordinator._pre_break_paused = True
        self.coordinator._pre_break_private = False

        self.coordinator.end_break()

        self.sync_engine.resume.assert_not_called()
        self.tray.set_state.assert_called_with(TrayState.PAUSED)

    def test_end_break_restores_private_state(self):
        """end_break should resume the engine then re-enable private mode.

        start_break() pauses the engine before enabling private mode, so
        end_break() must un-pause it (resume) before re-applying private mode.
        This ensures both flags are consistent: paused=False, private=True.
        """
        self.coordinator._on_break = True
        self.coordinator._pre_break_paused = False
        self.coordinator._pre_break_private = True

        self.coordinator.end_break()

        self.sync_engine.resume.assert_called_once()
        self.sync_engine.set_private_mode.assert_called_once_with(True)
        self.tray.set_state.assert_called_with(TrayState.PRIVATE)

    def test_end_break_silent_no_notification(self):
        """end_break(silent=True) should not send notification."""
        self.coordinator._on_break = True
        self.coordinator._pre_break_paused = False
        self.coordinator._pre_break_private = False

        with patch("src.main.send_notification") as mock_notify:
            self.coordinator.end_break(silent=True)
            mock_notify.assert_not_called()
