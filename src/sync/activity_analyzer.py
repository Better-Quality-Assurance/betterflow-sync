"""Activity analyzer for detecting engagement vs idle-active states."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from .aw_client import AWEvent
except ImportError:
    from sync.aw_client import AWEvent

__all__ = ["ActivityAnalyzer", "ActivityMetrics", "EngagementThresholds"]

logger = logging.getLogger(__name__)


@dataclass
class EngagementThresholds:
    """Server-configurable thresholds for engagement detection.

    These thresholds define what constitutes "engaged" work vs "idle-active"
    (mouse-only activity that may indicate fake activity like mouse wiggling).
    """

    sustained_typing_presses: int = 50  # Presses in window = engaged
    window_changes_min: int = 2  # Task switching = engaged
    scroll_threshold: int = 10  # Reading behavior = engaged
    combined_presses_min: int = 10  # For combined signal checks
    combined_scrolls_min: int = 5  # For combined signal checks
    window_minutes: int = 5  # Rolling window size in minutes


@dataclass
class ActivityMetrics:
    """Raw activity metrics computed over a time window.

    These metrics are sent to the server alongside the client's classification,
    allowing the server to validate or recalculate the activity state.
    """

    presses: int = 0
    clicks: int = 0
    scrolls: int = 0
    window_changes: int = 0

    def is_engaged(self, thresholds: EngagementThresholds) -> bool:
        """Check if activity indicates real engagement.

        Returns True if any of these conditions are met:
        - Sustained typing: presses > sustained_typing_presses
        - Task switching: window_changes >= window_changes_min
        - Reading: scrolls > scroll_threshold
        - Combined typing + scrolling
        - Combined typing + window switching
        """
        # Sustained typing
        if self.presses > thresholds.sustained_typing_presses:
            return True

        # Task switching
        if self.window_changes >= thresholds.window_changes_min:
            return True

        # Reading behavior
        if self.scrolls > thresholds.scroll_threshold:
            return True

        # Combined signals: typing + scrolling
        if (
            self.presses > thresholds.combined_presses_min
            and self.scrolls > thresholds.combined_scrolls_min
        ):
            return True

        # Combined signals: typing + window switching
        if self.presses > thresholds.combined_presses_min and self.window_changes >= 1:
            return True

        return False

    def to_dict(self) -> dict:
        """Convert to dictionary for API transmission."""
        return {
            "presses": self.presses,
            "clicks": self.clicks,
            "scrolls": self.scrolls,
            "window_changes": self.window_changes,
        }


class ActivityAnalyzer:
    """Analyzes activity patterns to detect engagement vs idle-active.

    This class maintains a rolling window of input events (keystrokes, clicks,
    scrolls) and window events (app switches) to determine whether the user
    is genuinely engaged or just wiggling their mouse to appear active.

    Usage:
        analyzer = ActivityAnalyzer()
        analyzer.add_input_events(input_events)
        analyzer.add_window_events(window_events)
        state = analyzer.get_activity_state(event.timestamp)
        metrics = analyzer.get_raw_metrics(event.timestamp)
    """

    def __init__(self, thresholds: Optional[EngagementThresholds] = None):
        """Initialize the analyzer.

        Args:
            thresholds: Optional custom thresholds. Defaults to EngagementThresholds().
        """
        self._thresholds = thresholds or EngagementThresholds()
        self._input_events: list[AWEvent] = []
        self._window_events: list[AWEvent] = []

    def update_thresholds(self, thresholds: EngagementThresholds) -> None:
        """Update thresholds from server config.

        Args:
            thresholds: New thresholds to use.
        """
        self._thresholds = thresholds
        logger.debug(f"Updated engagement thresholds: {thresholds}")

    @property
    def thresholds(self) -> EngagementThresholds:
        """Get current thresholds."""
        return self._thresholds

    def add_input_events(self, events: list[AWEvent]) -> None:
        """Add input events, pruning events older than the window.

        Input events contain keystroke, click, and scroll counts.
        Deduplicates by event ID to handle overlapping fetches.

        Args:
            events: List of input bucket events from ActivityWatch.
        """
        if not events:
            return

        # Deduplicate by event ID
        existing_ids = {e.id for e in self._input_events}
        new_events = [e for e in events if e.id not in existing_ids]
        self._input_events.extend(new_events)

        # Prune old events (older than 2x window to allow for lookback)
        cutoff = datetime.now(timezone.utc) - timedelta(
            minutes=self._thresholds.window_minutes * 2
        )
        self._input_events = [e for e in self._input_events if e.timestamp >= cutoff]

        # Sort by timestamp for consistent processing
        self._input_events.sort(key=lambda e: e.timestamp)

    def add_window_events(self, events: list[AWEvent]) -> None:
        """Add window events for switch detection.

        Window events are used to count app/window switches.
        Deduplicates by event ID to handle overlapping fetches.

        Args:
            events: List of window bucket events from ActivityWatch.
        """
        if not events:
            return

        # Deduplicate by event ID
        existing_ids = {e.id for e in self._window_events}
        new_events = [e for e in events if e.id not in existing_ids]
        self._window_events.extend(new_events)

        # Prune old events (older than 2x window to allow for lookback)
        cutoff = datetime.now(timezone.utc) - timedelta(
            minutes=self._thresholds.window_minutes * 2
        )
        self._window_events = [e for e in self._window_events if e.timestamp >= cutoff]

        # Sort by timestamp for consistent processing
        self._window_events.sort(key=lambda e: e.timestamp)

    def get_activity_state(self, timestamp: datetime) -> str:
        """Get activity state for a given timestamp.

        Args:
            timestamp: The timestamp to check.

        Returns:
            "active" if engaged work detected, "idle-active" otherwise.
        """
        metrics = self._compute_metrics(timestamp)
        return "active" if metrics.is_engaged(self._thresholds) else "idle-active"

    def get_raw_metrics(self, timestamp: datetime) -> ActivityMetrics:
        """Get raw metrics for server validation.

        Args:
            timestamp: The timestamp to compute metrics for.

        Returns:
            ActivityMetrics with computed values.
        """
        return self._compute_metrics(timestamp)

    def _compute_metrics(self, timestamp: datetime) -> ActivityMetrics:
        """Compute activity metrics for the window ending at timestamp.

        Args:
            timestamp: End of the window.

        Returns:
            ActivityMetrics computed over the rolling window.
        """
        window_start = timestamp - timedelta(minutes=self._thresholds.window_minutes)

        # Sum input metrics in window
        total_presses = 0
        total_clicks = 0
        total_scrolls = 0

        for event in self._input_events:
            if window_start <= event.timestamp <= timestamp:
                total_presses += event.presses
                total_clicks += event.clicks
                total_scrolls += event.scrolls

        # Count window changes in window
        window_changes = self._count_window_changes(window_start, timestamp)

        return ActivityMetrics(
            presses=total_presses,
            clicks=total_clicks,
            scrolls=total_scrolls,
            window_changes=window_changes,
        )

    def _count_window_changes(self, start: datetime, end: datetime) -> int:
        """Count the number of window/app changes in a time range.

        A window change is when the app or title changes between consecutive events.

        Args:
            start: Start of the time range.
            end: End of the time range.

        Returns:
            Number of window changes.
        """
        # Filter events in range
        events_in_range = [
            e for e in self._window_events if start <= e.timestamp <= end
        ]

        if len(events_in_range) < 2:
            return 0

        changes = 0
        for i in range(1, len(events_in_range)):
            prev = events_in_range[i - 1]
            curr = events_in_range[i]

            # Check if app changed
            if prev.app != curr.app:
                changes += 1
            # Or if title changed (different task in same app)
            elif prev.title != curr.title:
                changes += 1

        return changes

    def clear(self) -> None:
        """Clear all stored events."""
        self._input_events.clear()
        self._window_events.clear()
