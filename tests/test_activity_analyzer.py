"""Tests for activity analyzer."""

from datetime import datetime, timedelta, timezone

from src.config import FraudDetectionConfig
from src.sync.activity_analyzer import (
    ActivityAnalyzer,
    ActivityMetrics,
    EngagementThresholds,
    FraudAssessment,
    FraudSignalDetector,
)
from src.sync.aw_client import AWEvent


class TestActivityMetrics:
    """Tests for ActivityMetrics."""

    def test_sustained_typing_is_engaged(self):
        """Sustained typing (>50 presses) should be engaged."""
        metrics = ActivityMetrics(presses=51, clicks=0, scrolls=0, window_changes=0)
        thresholds = EngagementThresholds()

        assert metrics.is_engaged(thresholds) is True

    def test_window_switching_is_engaged(self):
        """Task switching (>=2 window changes) should be engaged."""
        metrics = ActivityMetrics(presses=0, clicks=0, scrolls=0, window_changes=2)
        thresholds = EngagementThresholds()

        assert metrics.is_engaged(thresholds) is True

    def test_scrolling_is_engaged(self):
        """Reading behavior (>10 scrolls) should be engaged."""
        metrics = ActivityMetrics(presses=0, clicks=0, scrolls=11, window_changes=0)
        thresholds = EngagementThresholds()

        assert metrics.is_engaged(thresholds) is True

    def test_combined_typing_scrolling_is_engaged(self):
        """Combined weak signals should be engaged."""
        metrics = ActivityMetrics(presses=11, clicks=0, scrolls=6, window_changes=0)
        thresholds = EngagementThresholds()

        assert metrics.is_engaged(thresholds) is True

    def test_combined_typing_window_change_is_engaged(self):
        """Combined typing + 1 window change should be engaged."""
        metrics = ActivityMetrics(presses=11, clicks=0, scrolls=0, window_changes=1)
        thresholds = EngagementThresholds()

        assert metrics.is_engaged(thresholds) is True

    def test_mouse_only_is_not_engaged(self):
        """Mouse clicks only (no typing, no scrolling) should be idle-active."""
        metrics = ActivityMetrics(presses=0, clicks=50, scrolls=0, window_changes=0)
        thresholds = EngagementThresholds()

        assert metrics.is_engaged(thresholds) is False

    def test_nothing_is_not_engaged(self):
        """No activity should be idle-active."""
        metrics = ActivityMetrics(presses=0, clicks=0, scrolls=0, window_changes=0)
        thresholds = EngagementThresholds()

        assert metrics.is_engaged(thresholds) is False

    def test_weak_signals_not_engaged(self):
        """Weak signals that don't meet any threshold should be idle-active."""
        metrics = ActivityMetrics(presses=5, clicks=10, scrolls=3, window_changes=0)
        thresholds = EngagementThresholds()

        assert metrics.is_engaged(thresholds) is False

    def test_custom_thresholds(self):
        """Custom thresholds should be respected."""
        metrics = ActivityMetrics(presses=25, clicks=0, scrolls=0, window_changes=0)

        # Default threshold is 50
        default_thresholds = EngagementThresholds()
        assert metrics.is_engaged(default_thresholds) is False

        # Lower threshold to 20
        custom_thresholds = EngagementThresholds(sustained_typing_presses=20)
        assert metrics.is_engaged(custom_thresholds) is True

    def test_to_dict(self):
        """to_dict should return correct dictionary."""
        metrics = ActivityMetrics(presses=10, clicks=5, scrolls=3, window_changes=2)

        result = metrics.to_dict()

        assert result == {
            "presses": 10,
            "clicks": 5,
            "scrolls": 3,
            "window_changes": 2,
        }


class TestActivityAnalyzer:
    """Tests for ActivityAnalyzer."""

    def setup_method(self):
        """Set up test fixtures."""
        self.analyzer = ActivityAnalyzer()
        self.now = datetime.now(timezone.utc)

    def _make_input_event(
        self,
        timestamp: datetime,
        presses: int = 0,
        clicks: int = 0,
        scrolls: int = 0,
        event_id: int = 1,
    ) -> AWEvent:
        """Create an input event for testing."""
        return AWEvent(
            id=event_id,
            timestamp=timestamp,
            duration=1.0,
            data={"presses": presses, "clicks": clicks, "scrolls": scrolls},
        )

    def _make_window_event(
        self,
        timestamp: datetime,
        app: str = "Test",
        title: str = "Test Title",
    ) -> AWEvent:
        """Create a window event for testing."""
        return AWEvent(
            id=1,
            timestamp=timestamp,
            duration=30.0,
            data={"app": app, "title": title},
        )

    def test_sustained_typing_returns_active(self):
        """Sustained typing should return 'active'."""
        # Add input events with >50 presses in window
        events = [
            self._make_input_event(self.now - timedelta(minutes=2), presses=30),
            self._make_input_event(self.now - timedelta(minutes=1), presses=30),
        ]
        self.analyzer.add_input_events(events)

        state = self.analyzer.get_activity_state(self.now)

        assert state == "active"

    def test_window_switching_returns_active(self):
        """Window switching should return 'active'."""
        # Add window events with app switches
        events = [
            self._make_window_event(self.now - timedelta(minutes=3), app="App1"),
            self._make_window_event(self.now - timedelta(minutes=2), app="App2"),
            self._make_window_event(self.now - timedelta(minutes=1), app="App3"),
        ]
        self.analyzer.add_window_events(events)

        state = self.analyzer.get_activity_state(self.now)

        assert state == "active"

    def test_mouse_only_returns_idle_active(self):
        """Mouse-only activity should return 'idle-active'."""
        # Add input events with only clicks (like mouse wiggling)
        events = [
            self._make_input_event(self.now - timedelta(minutes=2), clicks=10),
            self._make_input_event(self.now - timedelta(minutes=1), clicks=10),
        ]
        self.analyzer.add_input_events(events)

        state = self.analyzer.get_activity_state(self.now)

        assert state == "idle-active"

    def test_no_activity_returns_idle_active(self):
        """No activity should return 'idle-active'."""
        state = self.analyzer.get_activity_state(self.now)

        assert state == "idle-active"

    def test_events_outside_window_not_counted(self):
        """Events outside the rolling window should not be counted."""
        # Add typing event outside the window (6 minutes ago, window is 5 min)
        old_event = self._make_input_event(self.now - timedelta(minutes=6), presses=100)
        self.analyzer.add_input_events([old_event])

        state = self.analyzer.get_activity_state(self.now)

        assert state == "idle-active"

    def test_threshold_updates(self):
        """Threshold updates should affect classification."""
        # Add typing that's below default but above custom threshold
        events = [
            self._make_input_event(self.now - timedelta(minutes=1), presses=25),
        ]
        self.analyzer.add_input_events(events)

        # Default threshold: should be idle-active
        assert self.analyzer.get_activity_state(self.now) == "idle-active"

        # Update to lower threshold
        new_thresholds = EngagementThresholds(sustained_typing_presses=20)
        self.analyzer.update_thresholds(new_thresholds)

        # Now should be active
        assert self.analyzer.get_activity_state(self.now) == "active"

    def test_get_raw_metrics(self):
        """get_raw_metrics should return computed metrics."""
        events = [
            self._make_input_event(
                self.now - timedelta(minutes=1), presses=10, clicks=5, scrolls=3
            ),
        ]
        self.analyzer.add_input_events(events)

        metrics = self.analyzer.get_raw_metrics(self.now)

        assert metrics.presses == 10
        assert metrics.clicks == 5
        assert metrics.scrolls == 3

    def test_window_changes_counted(self):
        """Window changes should be counted in metrics."""
        events = [
            self._make_window_event(self.now - timedelta(minutes=3), app="App1"),
            self._make_window_event(self.now - timedelta(minutes=2), app="App2"),
            self._make_window_event(self.now - timedelta(minutes=1), app="App2", title="Different"),
        ]
        self.analyzer.add_window_events(events)

        metrics = self.analyzer.get_raw_metrics(self.now)

        # App change + title change = 2 window changes
        assert metrics.window_changes == 2

    def test_clear_removes_all_events(self):
        """clear() should remove all events."""
        events = [
            self._make_input_event(self.now - timedelta(minutes=1), presses=100),
        ]
        self.analyzer.add_input_events(events)

        # Before clear
        assert self.analyzer.get_activity_state(self.now) == "active"

        self.analyzer.clear()

        # After clear
        assert self.analyzer.get_activity_state(self.now) == "idle-active"

    def test_old_events_pruned(self):
        """Old events should be pruned when a newer event establishes the window."""
        # Add an old event AND a recent one. Pruning is anchored to the
        # latest event timestamp (not wall clock). With a 5-min window
        # (2x = 10 min cutoff), an event 20 min before the latest is pruned.
        old_event = self._make_input_event(self.now - timedelta(minutes=20), presses=100)
        recent_event = self._make_input_event(self.now, presses=1, event_id=99)
        self.analyzer.add_input_events([old_event, recent_event])

        # The old event should have been pruned, only recent remains
        assert len(self.analyzer._input_events) == 1
        assert self.analyzer._input_events[0].id == 99

    def test_multiple_input_events_summed(self):
        """Multiple input events in window should have counts summed."""
        events = [
            self._make_input_event(self.now - timedelta(minutes=4), presses=10),
            self._make_input_event(self.now - timedelta(minutes=3), presses=15),
            self._make_input_event(self.now - timedelta(minutes=2), presses=12),
            self._make_input_event(self.now - timedelta(minutes=1), presses=14),
        ]
        self.analyzer.add_input_events(events)

        metrics = self.analyzer.get_raw_metrics(self.now)

        # Total: 10 + 15 + 12 + 14 = 51
        assert metrics.presses == 51

    def test_custom_window_size(self):
        """Custom window size should be respected."""
        # Create analyzer with 2-minute window
        analyzer = ActivityAnalyzer(thresholds=EngagementThresholds(window_minutes=2))

        # Add event 3 minutes ago (outside 2-min window)
        event = self._make_input_event(self.now - timedelta(minutes=3), presses=100)
        analyzer.add_input_events([event])

        # Should not count the event
        metrics = analyzer.get_raw_metrics(self.now)
        assert metrics.presses == 0


class TestFraudSignalDetector:
    """Tests for FraudSignalDetector."""

    def setup_method(self):
        self.config = FraudDetectionConfig()
        self.detector = FraudSignalDetector(config=self.config)
        self.now = datetime.now(timezone.utc)

    def test_no_data_returns_zero_score(self):
        """Empty detector should return fraud score 0."""
        result = self.detector.assess()
        assert result.score == 0
        assert result.signals == []

    # --- Keystroke uniformity ---

    def test_uniform_keystrokes_flagged(self):
        """Identical keystroke counts across windows should trigger signal."""
        # 6 windows all with exactly 10 presses (CV = 0)
        for _ in range(6):
            self.detector.record_window_metrics(
                ActivityMetrics(presses=10, clicks=0, scrolls=0, window_changes=0)
            )

        result = self.detector.assess()
        assert "keystroke_uniformity" in result.signals
        assert result.score > 0

    def test_natural_keystroke_variance_passes(self):
        """Natural variance in keystroke counts should not trigger."""
        counts = [15, 45, 8, 72, 30, 22]
        for c in counts:
            self.detector.record_window_metrics(
                ActivityMetrics(presses=c, clicks=0, scrolls=0, window_changes=0)
            )

        result = self.detector.assess()
        assert "keystroke_uniformity" not in result.signals

    def test_keystroke_uniformity_needs_min_windows(self):
        """Should not flag keystroke uniformity with fewer than min_windows."""
        # Only 3 windows (default min is 6)
        for _ in range(3):
            self.detector.record_window_metrics(
                ActivityMetrics(presses=10, clicks=0, scrolls=0, window_changes=0)
            )

        result = self.detector.assess()
        assert "keystroke_uniformity" not in result.signals

    def test_keystroke_uniformity_zero_mean_safe(self):
        """Zero presses across all windows should not flag (no denominator issue)."""
        for _ in range(6):
            self.detector.record_window_metrics(
                ActivityMetrics(presses=0, clicks=5, scrolls=0, window_changes=0)
            )

        result = self.detector.assess()
        assert "keystroke_uniformity" not in result.signals

    # --- Input regularity ---

    def test_regular_intervals_flagged(self):
        """Events arriving at exactly regular intervals should trigger."""
        base = self.now - timedelta(minutes=10)
        for i in range(12):
            self.detector.record_input_timestamp(base + timedelta(seconds=i * 60))

        result = self.detector.assess()
        assert "input_regularity" in result.signals
        assert result.score > 0

    def test_random_intervals_pass(self):
        """Events with natural random intervals should not trigger."""
        base = self.now - timedelta(minutes=10)
        # Irregular gaps
        gaps = [3, 12, 45, 7, 22, 55, 8, 30, 15, 40]
        t = base
        for gap in gaps:
            t += timedelta(seconds=gap)
            self.detector.record_input_timestamp(t)

        result = self.detector.assess()
        assert "input_regularity" not in result.signals

    def test_input_regularity_needs_min_events(self):
        """Should not flag regularity with fewer than min events."""
        base = self.now
        for i in range(3):  # only 3 events, default min is 10
            self.detector.record_input_timestamp(base + timedelta(seconds=i * 60))

        result = self.detector.assess()
        assert "input_regularity" not in result.signals

    # --- Mouse-only streak ---

    def test_mouse_only_streak_detected(self):
        """Consecutive click-only windows should trigger."""
        for _ in range(4):
            self.detector.record_window_metrics(
                ActivityMetrics(presses=0, clicks=5, scrolls=0, window_changes=0)
            )

        result = self.detector.assess()
        assert "mouse_only_streak" in result.signals

    def test_mouse_only_streak_cleared_on_real_activity(self):
        """Real activity should reset the mouse-only streak."""
        # Build up a streak
        for _ in range(3):
            self.detector.record_window_metrics(
                ActivityMetrics(presses=0, clicks=5, scrolls=0, window_changes=0)
            )

        # Real activity breaks the streak
        self.detector.record_window_metrics(
            ActivityMetrics(presses=20, clicks=5, scrolls=0, window_changes=0)
        )

        result = self.detector.assess()
        assert "mouse_only_streak" not in result.signals

    def test_mouse_only_streak_below_threshold(self):
        """Below threshold consecutive click-only windows should not trigger."""
        for _ in range(2):  # default threshold is 3
            self.detector.record_window_metrics(
                ActivityMetrics(presses=0, clicks=5, scrolls=0, window_changes=0)
            )

        result = self.detector.assess()
        assert "mouse_only_streak" not in result.signals

    # --- Low app diversity ---

    def test_low_app_diversity_flagged(self):
        """Only 1 unique app after 60+ minutes of active time should trigger."""
        # Record 1 app with enough active time
        for _ in range(6):
            self.detector.record_window_metrics(
                ActivityMetrics(presses=10, clicks=0, scrolls=0, window_changes=0),
                app="OnlyApp",
            )
        self.detector.add_active_time(65 * 60)  # 65 minutes

        result = self.detector.assess()
        assert "low_app_diversity" in result.signals

    def test_app_diversity_ok_with_multiple_apps(self):
        """Multiple unique apps should not trigger."""
        self.detector.record_window_metrics(
            ActivityMetrics(presses=10, clicks=0, scrolls=0, window_changes=0),
            app="App1",
        )
        self.detector.record_window_metrics(
            ActivityMetrics(presses=10, clicks=0, scrolls=0, window_changes=0),
            app="App2",
        )
        self.detector.add_active_time(65 * 60)

        result = self.detector.assess()
        assert "low_app_diversity" not in result.signals

    def test_app_diversity_not_checked_below_time_threshold(self):
        """Should not check app diversity before enough active time."""
        self.detector.record_window_metrics(
            ActivityMetrics(presses=10, clicks=0, scrolls=0, window_changes=0),
            app="OnlyApp",
        )
        self.detector.add_active_time(30 * 60)  # Only 30 minutes

        result = self.detector.assess()
        assert "low_app_diversity" not in result.signals

    # --- Click-to-keystroke ratio ---

    def test_high_click_keystroke_ratio_flagged(self):
        """100+ clicks with 0 presses should trigger."""
        self.detector.record_window_metrics(
            ActivityMetrics(presses=0, clicks=150, scrolls=0, window_changes=0)
        )

        result = self.detector.assess()
        assert "click_keystroke_ratio" in result.signals
        assert result.score == 10

    def test_normal_click_keystroke_ratio_passes(self):
        """Normal click/keystroke balance should not trigger."""
        self.detector.record_window_metrics(
            ActivityMetrics(presses=50, clicks=100, scrolls=0, window_changes=0)
        )

        result = self.detector.assess()
        assert "click_keystroke_ratio" not in result.signals

    def test_click_keystroke_ratio_needs_min_clicks(self):
        """Should not flag ratio with fewer than 100 clicks."""
        self.detector.record_window_metrics(
            ActivityMetrics(presses=0, clicks=50, scrolls=0, window_changes=0)
        )

        result = self.detector.assess()
        assert "click_keystroke_ratio" not in result.signals

    # --- Score capping ---

    def test_fraud_score_capped_at_100(self):
        """Combined signals should not exceed 100."""
        # Trigger all signals simultaneously
        # Uniform keystrokes (6 identical windows)
        for _ in range(6):
            self.detector.record_window_metrics(
                ActivityMetrics(presses=10, clicks=20, scrolls=0, window_changes=0),
                app="OnlyApp",
            )

        # Regular input timestamps
        base = self.now
        for i in range(12):
            self.detector.record_input_timestamp(base + timedelta(seconds=i * 60))

        # Enough active time for app diversity
        self.detector.add_active_time(65 * 60)

        # Additional clicks to push ratio
        self.detector._total_clicks = 200
        self.detector._total_presses = 0

        # Force mouse-only streak
        self.detector._mouse_only_streak = 6

        result = self.detector.assess()
        assert result.score <= 100

    # --- Session clear ---

    def test_clear_resets_all_accumulators(self):
        """clear() should reset all session-level state."""
        # Build up state
        for _ in range(6):
            self.detector.record_window_metrics(
                ActivityMetrics(presses=10, clicks=0, scrolls=0, window_changes=0),
                app="TestApp",
            )
        self.detector.record_input_timestamp(self.now)
        self.detector.add_active_time(3600)

        self.detector.clear()

        assert self.detector._window_press_counts == []
        assert self.detector._unique_apps == set()
        assert self.detector._mouse_only_streak == 0
        assert len(self.detector._input_timestamps) == 0
        assert self.detector._total_clicks == 0
        assert self.detector._total_presses == 0
        assert self.detector._active_minutes == 0.0

        result = self.detector.assess()
        assert result.score == 0
        assert result.signals == []

    # --- Config thresholds ---

    def test_custom_config_thresholds_respected(self):
        """Custom config thresholds should change detection sensitivity."""
        # Use very lenient thresholds — 6 identical windows should NOT trigger
        lenient_config = FraudDetectionConfig(
            keystroke_cv_threshold=0.0,  # Only exact zero CV triggers
            min_windows_for_variance=20,  # Very high minimum
        )
        detector = FraudSignalDetector(config=lenient_config)

        for _ in range(6):
            detector.record_window_metrics(
                ActivityMetrics(presses=10, clicks=0, scrolls=0, window_changes=0)
            )

        result = detector.assess()
        assert "keystroke_uniformity" not in result.signals

    def test_config_update_takes_effect(self):
        """Updating config should change detection behavior."""
        # Start with default config — streak threshold 3
        for _ in range(3):
            self.detector.record_window_metrics(
                ActivityMetrics(presses=0, clicks=5, scrolls=0, window_changes=0)
            )

        result = self.detector.assess()
        assert "mouse_only_streak" in result.signals

        # Update to require 5 consecutive windows
        new_config = FraudDetectionConfig(mouse_only_streak_threshold=5)
        self.detector.update_config(new_config)

        result = self.detector.assess()
        assert "mouse_only_streak" not in result.signals

    # --- Extra metrics ---

    def test_extra_metrics_populated(self):
        """Assessment should include extra metrics for API."""
        self.detector.record_window_metrics(
            ActivityMetrics(presses=10, clicks=0, scrolls=0, window_changes=0),
            app="TestApp",
        )

        result = self.detector.assess()
        assert "unique_apps" in result.extra_metrics
        assert result.extra_metrics["unique_apps"] == 1
        assert "keystroke_variance" in result.extra_metrics


class TestActivityAnalyzerFraudIntegration:
    """Tests for ActivityAnalyzer fraud detection integration."""

    def setup_method(self):
        self.config = FraudDetectionConfig()
        self.analyzer = ActivityAnalyzer(fraud_config=self.config)
        self.now = datetime.now(timezone.utc)

    def _make_input_event(
        self,
        timestamp: datetime,
        event_id: int = 1,
        presses: int = 0,
        clicks: int = 0,
        scrolls: int = 0,
    ) -> AWEvent:
        return AWEvent(
            id=event_id,
            timestamp=timestamp,
            duration=1.0,
            data={"presses": presses, "clicks": clicks, "scrolls": scrolls},
        )

    def test_get_fraud_assessment_returns_assessment(self):
        """get_fraud_assessment should return a FraudAssessment."""
        result = self.analyzer.get_fraud_assessment(self.now)
        assert isinstance(result, FraudAssessment)
        assert result.score == 0

    def test_fraud_assessment_records_input_timestamps(self):
        """Adding input events should feed timestamps to fraud detector."""
        events = [
            self._make_input_event(self.now - timedelta(seconds=i * 60), event_id=i)
            for i in range(12)
        ]
        self.analyzer.add_input_events(events)

        assert len(self.analyzer._fraud_detector._input_timestamps) == 12

    def test_clear_resets_fraud_state(self):
        """clear() should also reset fraud detector state."""
        events = [
            self._make_input_event(self.now - timedelta(minutes=1), event_id=1, presses=10),
        ]
        self.analyzer.add_input_events(events)
        self.analyzer.get_fraud_assessment(self.now, app="TestApp")

        self.analyzer.clear()

        assert self.analyzer._fraud_detector._unique_apps == set()
        assert len(self.analyzer._fraud_detector._input_timestamps) == 0

    def test_fraud_config_update(self):
        """update_fraud_config should propagate to the detector."""
        new_config = FraudDetectionConfig(mouse_only_streak_threshold=10)
        self.analyzer.update_fraud_config(new_config)

        assert self.analyzer._fraud_detector._config.mouse_only_streak_threshold == 10
