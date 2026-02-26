"""Tests for activity analyzer."""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from src.sync.activity_analyzer import (
    ActivityAnalyzer,
    ActivityMetrics,
    EngagementThresholds,
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
    ) -> AWEvent:
        """Create an input event for testing."""
        return AWEvent(
            id=1,
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
        old_event = self._make_input_event(
            self.now - timedelta(minutes=6), presses=100
        )
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
            self._make_window_event(
                self.now - timedelta(minutes=1), app="App2", title="Different"
            ),
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
        """Old events should be automatically pruned."""
        # Add old event (20 minutes ago, should be pruned at 2x window = 10 min)
        old_event = self._make_input_event(
            self.now - timedelta(minutes=20), presses=100
        )
        self.analyzer.add_input_events([old_event])

        # The old event should have been pruned
        assert len(self.analyzer._input_events) == 0

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
        analyzer = ActivityAnalyzer(
            thresholds=EngagementThresholds(window_minutes=2)
        )

        # Add event 3 minutes ago (outside 2-min window)
        event = self._make_input_event(self.now - timedelta(minutes=3), presses=100)
        analyzer.add_input_events([event])

        # Should not count the event
        metrics = analyzer.get_raw_metrics(self.now)
        assert metrics.presses == 0
