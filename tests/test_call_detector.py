"""Tests for call/meeting detection."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from src.config import CallDetectionSettings, Config
from src.sync.call_detector import CallDetector, CallEvent, _match_browser, _match_native


def _ts(minutes_offset: float = 0) -> datetime:
    """Helper: return a UTC timestamp offset by minutes from a fixed base."""
    base = datetime(2026, 3, 16, 10, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(minutes=minutes_offset)


class TestPatternMatching:
    """Test individual pattern matching functions."""

    def test_zoom_native_meeting(self):
        assert _match_native("zoom.us", "Zoom Meeting") == "Zoom"

    def test_zoom_native_webinar(self):
        assert _match_native("zoom.us", "Zoom Webinar") == "Zoom"

    def test_zoom_native_meeting_id(self):
        assert _match_native("zoom.us", "123456789") == "Zoom"

    def test_zoom_native_no_match(self):
        assert _match_native("zoom.us", "Zoom - Home") is None

    def test_teams_native_meeting(self):
        assert _match_native("Microsoft Teams", "Meeting with John") == "Microsoft Teams"

    def test_teams_native_call(self):
        assert _match_native("Microsoft Teams", "Call with Sarah") == "Microsoft Teams"

    def test_teams_native_in_call(self):
        assert _match_native("Microsoft Teams", "In a call") == "Microsoft Teams"

    def test_teams_native_chat_no_match(self):
        assert _match_native("Microsoft Teams", "Chat - General") is None

    def test_slack_huddle(self):
        assert _match_native("Slack", "Huddle in #engineering") == "Slack"

    def test_slack_chat_no_match(self):
        assert _match_native("Slack", "Slack - #general") is None

    def test_discord_voice(self):
        assert _match_native("Discord", "Voice Connected - gaming") == "Discord"

    def test_discord_text_no_match(self):
        assert _match_native("Discord", "#general - Discord") is None

    def test_facetime_any_window(self):
        assert _match_native("FaceTime", "anything here") == "FaceTime"

    def test_webex_meeting(self):
        assert _match_native("Webex", "Meeting - Sprint Review") == "Webex"

    def test_unrelated_app(self):
        assert _match_native("Visual Studio Code", "main.py") is None

    def test_google_meet_browser(self):
        assert _match_browser("https://meet.google.com/abc-defg-hij") == "Google Meet"

    def test_google_meet_landing_no_match(self):
        assert _match_browser("https://meet.google.com/") is None

    def test_teams_browser_meeting(self):
        assert _match_browser("https://teams.microsoft.com/v2/#/meeting/123") == "Microsoft Teams"

    def test_zoom_browser_join(self):
        assert _match_browser("https://zoom.us/j/123456789") == "Zoom"

    def test_zoom_browser_web_client(self):
        assert _match_browser("https://zoom.us/wc/123456789") == "Zoom"

    def test_slack_browser_huddle(self):
        assert _match_browser("https://app.slack.com/huddle/T12345/C67890") == "Slack"

    def test_unrelated_url(self):
        assert _match_browser("https://github.com/pulls") is None

    def test_empty_url(self):
        assert _match_browser("") is None

    def test_none_url(self):
        assert _match_browser(None) is None


class TestCallDetector:
    """Test the CallDetector state machine."""

    def test_zoom_call_detected_and_ended(self):
        """Full call lifecycle: start, continue, end."""
        detector = CallDetector(min_duration=10)

        # Start a Zoom call
        result = detector.process_event("zoom.us", "Zoom Meeting", None, _ts(0), 5.0)
        assert result is None  # Call just started, no event yet

        # Continue in call (2 more events)
        result = detector.process_event("zoom.us", "Zoom Meeting", None, _ts(1), 60.0)
        assert result is None

        result = detector.process_event("zoom.us", "Zoom Meeting", None, _ts(2), 60.0)
        assert result is None

        # Switch away for longer than grace period (>30s)
        result = detector.process_event("Visual Studio Code", "main.py", None, _ts(3), 5.0)
        assert result is None  # Grace period starts

        result = detector.process_event("Visual Studio Code", "main.py", None, _ts(4), 60.0)
        assert result is not None  # Grace expired -> call ended

        assert result.app == "Zoom"
        assert result.call_type == "native"
        assert result.start == _ts(0)
        assert result.end == _ts(3)  # End = when user left call app
        assert result.duration > 0

    def test_google_meet_browser_detected(self):
        """Browser-based Google Meet detection via URL."""
        detector = CallDetector(min_duration=10)

        result = detector.process_event(
            "Google Chrome",
            "Meeting - Google Meet",
            "https://meet.google.com/abc-defg-hij",
            _ts(0),
            5.0,
        )
        assert result is None

        result = detector.process_event(
            "Google Chrome",
            "Meeting - Google Meet",
            "https://meet.google.com/abc-defg-hij",
            _ts(5),
            300.0,
        )
        assert result is None

        # End: navigate to different page
        result = detector.process_event(
            "Google Chrome", "GitHub", "https://github.com", _ts(10), 5.0
        )
        assert result is None  # Grace period

        result = detector.process_event(
            "Google Chrome", "GitHub", "https://github.com", _ts(11), 60.0
        )
        assert result is not None

        assert result.app == "Google Meet"
        assert result.call_type == "browser"

    def test_short_open_filtered(self):
        """Opens Zoom briefly (<30s min_duration) should be discarded."""
        detector = CallDetector(min_duration=30)

        result = detector.process_event("zoom.us", "Zoom Meeting", None, _ts(0), 5.0)
        assert result is None

        # Switch away after only 15 seconds total
        result = detector.process_event("Safari", "Google", None, _ts(0.25), 5.0)
        assert result is None  # Grace starts

        result = detector.process_event("Safari", "Google", None, _ts(1.25), 60.0)
        # Grace expired, call ended but too short
        assert result is None  # Filtered by min_duration

    def test_grace_period_prevents_splitting(self):
        """Brief app switch during a call should not split it."""
        detector = CallDetector(min_duration=10, grace_period=30.0)

        # Start Zoom call
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(0), 5.0)
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(1), 60.0)

        # Brief switch to Slack (< 30s grace period, only 15 seconds away)
        detector.process_event("Slack", "Slack - #general", None, _ts(2), 5.0)

        # Return to Zoom within grace period (15 seconds later)
        result = detector.process_event("zoom.us", "Zoom Meeting", None, _ts(2.25), 60.0)
        assert result is None  # Still in the same call

        # End the call for real
        detector.process_event("Safari", "Google", None, _ts(10), 5.0)
        result = detector.process_event("Safari", "Google", None, _ts(11), 60.0)
        assert result is not None

        # Should be one continuous call, not two
        assert result.app == "Zoom"
        assert result.start == _ts(0)

    def test_flush_emits_ongoing_call(self):
        """flush() should emit the current call at sync boundary."""
        detector = CallDetector(min_duration=10)

        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(0), 5.0)
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(5), 300.0)

        result = detector.flush()
        assert result is not None
        assert result.app == "Zoom"
        assert result.start == _ts(0)

        # After flush, state is reset
        result2 = detector.flush()
        assert result2 is None

    def test_flush_no_active_call(self):
        """flush() with no active call returns None."""
        detector = CallDetector()
        assert detector.flush() is None

    def test_different_call_app_ends_previous(self):
        """Switching from Zoom to Teams should end the Zoom call."""
        detector = CallDetector(min_duration=10)

        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(0), 5.0)
        detector.process_event("zoom.us", "Zoom Meeting", None, _ts(5), 300.0)

        # Switch to Teams call
        result = detector.process_event("Microsoft Teams", "Meeting with Bob", None, _ts(10), 5.0)
        assert result is not None
        assert result.app == "Zoom"

        # Teams call is now active; add more events so it exceeds min_duration
        detector.process_event("Microsoft Teams", "Meeting with Bob", None, _ts(15), 300.0)
        result = detector.flush()
        assert result is not None
        assert result.app == "Microsoft Teams"

    def test_case_insensitive_app_match(self):
        """App matching should be case-insensitive."""
        detector = CallDetector(min_duration=0)

        detector.process_event("Zoom.US", "Zoom Meeting", None, _ts(0), 5.0)
        result = detector.flush()
        assert result is not None
        assert result.app == "Zoom"

    def test_no_events_processed(self):
        """Detector handles empty event stream gracefully."""
        detector = CallDetector()
        assert detector.flush() is None


class TestCallDetectorDisabled:
    """Test that disabled config produces no call detector."""

    def test_disabled_config(self):
        """When call_detection.enabled is False, SyncEngine should not
        create a call detector (tested at integration level in test_sync_engine)."""
        config = CallDetectionSettings(enabled=False)
        assert config.enabled is False

    def test_min_duration_zero_allows_short_calls(self):
        """min_duration=0 should allow even 1-second calls."""
        detector = CallDetector(min_duration=0)
        detector.process_event("FaceTime", "FaceTime call", None, _ts(0), 1.0)

        # End the call
        detector.process_event("Safari", "Google", None, _ts(0.5), 5.0)
        result = detector.process_event("Safari", "Google", None, _ts(1.5), 60.0)
        assert result is not None
        assert result.app == "FaceTime"
        assert result.duration > 0


class TestSyncEngineCallIntegration:
    """Test CallDetector integration in SyncEngine."""

    def setup_method(self):
        from src.sync.activity_analyzer import ActivityAnalyzer
        from src.sync.daily_time_tracker import DailyTimeTracker
        from src.sync.sync_engine import SyncEngine

        self.aw = Mock()
        self.bf = Mock()
        self.queue = Mock()
        self.queue.get_checkpoint.return_value = None
        self.queue.get_all_categories.return_value = {}

        self.config = Config()
        self.config.call_detection.enabled = True
        self.config.call_detection.min_call_duration = 10

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

    def test_call_detector_initialized_when_enabled(self):
        assert self.engine._call_detector is not None

    def test_call_detector_none_when_disabled(self):
        from src.sync.sync_engine import SyncEngine

        config = Config()
        config.call_detection.enabled = False

        engine = SyncEngine(
            aw=self.aw,
            bf=self.bf,
            queue=self.queue,
            config=config,
            activity_analyzer=self.activity_analyzer,
            time_tracker=self.time_tracker,
        )
        assert engine._call_detector is None

    def test_make_call_bf_event(self):
        ce = CallEvent(
            app="Zoom",
            start=_ts(0),
            end=_ts(30),
            duration=1800.0,
            call_type="native",
        )
        result = self.engine._make_call_bf_event(ce)
        assert result["bucket_type"] == "call"
        assert result["duration"] == 1800.0
        assert result["data"]["app"] == "Zoom"
        assert result["data"]["call_type"] == "native"
        assert result["data"]["status"] == "completed"
        assert "bf-call-detector_" in result["bucket_id"]

    def test_make_call_bf_event_with_project(self):
        self.engine.set_current_project({"id": 42, "name": "Test"})
        ce = CallEvent(
            app="Google Meet",
            start=_ts(0),
            end=_ts(30),
            duration=1800.0,
            call_type="browser",
        )
        result = self.engine._make_call_bf_event(ce)
        assert result["project_id"] == 42
