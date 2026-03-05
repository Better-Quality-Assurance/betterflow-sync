"""Activity analyzer for detecting engagement vs idle-active states."""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    from .aw_client import AWEvent
except ImportError:
    from sync.aw_client import AWEvent

try:
    from ..config import FraudDetectionConfig
except ImportError:
    from config import FraudDetectionConfig

__all__ = [
    "ActivityAnalyzer",
    "ActivityMetrics",
    "EngagementThresholds",
    "FraudAssessment",
    "FraudSignalDetector",
]

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


@dataclass
class FraudAssessment:
    """Result of fraud signal detection."""

    score: int = 0  # 0-100, sum of signal scores capped at 100
    signals: list[str] = field(default_factory=list)  # Active signal labels
    extra_metrics: dict = field(default_factory=dict)  # Additional metrics for API


class FraudSignalDetector:
    """Session-level fraud signal detector.

    Accumulates patterns over the full session (not just a 5-min window)
    to detect sophisticated fraud like fake keystrokes, automation scripts,
    and session-level anomalies.

    Signals detected:
    - keystroke_uniformity: Suspiciously uniform keystroke counts across windows
    - input_regularity: Events fired at precise, machine-like intervals
    - mouse_only_streak: Consecutive windows with clicks but no other input
    - low_app_diversity: Too few unique apps over extended active time
    - click_keystroke_ratio: Abnormally high click-to-keystroke ratio
    """

    def __init__(self, config=None):
        self._config = config or FraudDetectionConfig()
        # Per-window keystroke snapshots: list of press counts per window
        self._window_press_counts: list[int] = []
        # Unique apps seen this session
        self._unique_apps: set[str] = set()
        # Consecutive mouse-only window count
        self._mouse_only_streak: int = 0
        # Input event timestamps for regularity analysis
        self._input_timestamps: list[datetime] = []
        # Session-level cumulative metrics
        self._total_clicks: int = 0
        self._total_presses: int = 0
        # Active time accumulated in minutes
        self._active_minutes: float = 0.0

    def update_config(self, config) -> None:
        self._config = config

    def record_window_metrics(self, metrics: "ActivityMetrics", app: Optional[str] = None) -> None:
        """Record metrics from one analysis window."""
        self._window_press_counts.append(metrics.presses)
        self._total_clicks += metrics.clicks
        self._total_presses += metrics.presses

        if app:
            self._unique_apps.add(app)

        # Track mouse-only streaks: clicks > 0 but no presses, scrolls, or switches
        if metrics.clicks > 0 and metrics.presses == 0 and metrics.scrolls == 0 and metrics.window_changes == 0:
            self._mouse_only_streak += 1
        else:
            self._mouse_only_streak = 0

    def record_input_timestamp(self, timestamp: datetime) -> None:
        """Record an input event timestamp for regularity analysis."""
        self._input_timestamps.append(timestamp)

    def add_active_time(self, duration_seconds: float) -> None:
        """Accumulate active time for app diversity checks."""
        self._active_minutes += duration_seconds / 60.0

    def assess(self) -> FraudAssessment:
        """Run all fraud signals and return combined assessment."""
        signals = []
        total_score = 0

        # Signal 1: Keystroke uniformity (0-30)
        ks_score, ks_cv = self._check_keystroke_uniformity()
        if ks_score > 0:
            signals.append("keystroke_uniformity")
            total_score += ks_score

        # Signal 2: Input regularity (0-25)
        ir_score, ir_cv = self._check_input_regularity()
        if ir_score > 0:
            signals.append("input_regularity")
            total_score += ir_score

        # Signal 3: Mouse-only streak (0-20)
        mo_score = self._check_mouse_only_streak()
        if mo_score > 0:
            signals.append("mouse_only_streak")
            total_score += mo_score

        # Signal 4: Low app diversity (0-15)
        ad_score = self._check_app_diversity()
        if ad_score > 0:
            signals.append("low_app_diversity")
            total_score += ad_score

        # Signal 5: Click-to-keystroke ratio (0-10)
        ck_score = self._check_click_keystroke_ratio()
        if ck_score > 0:
            signals.append("click_keystroke_ratio")
            total_score += ck_score

        return FraudAssessment(
            score=min(total_score, 100),
            signals=signals,
            extra_metrics={
                "unique_apps": len(self._unique_apps),
                "keystroke_variance": round(ks_cv, 4) if ks_cv is not None else None,
            },
        )

    def _check_keystroke_uniformity(self) -> tuple[int, Optional[float]]:
        """Check if per-window keystroke counts are suspiciously uniform.

        Returns (score 0-30, coefficient_of_variation or None).
        """
        cfg = self._config
        if len(self._window_press_counts) < cfg.min_windows_for_variance:
            return 0, None

        mean = sum(self._window_press_counts) / len(self._window_press_counts)
        if mean == 0:
            return 0, None

        variance = sum((x - mean) ** 2 for x in self._window_press_counts) / len(self._window_press_counts)
        std_dev = math.sqrt(variance)
        cv = std_dev / mean

        if cv < cfg.keystroke_cv_threshold:
            # Scale score: lower CV = higher score, max 30
            # At cv=0 -> 30, at cv=threshold -> 0
            score = int(30 * (1 - cv / cfg.keystroke_cv_threshold))
            return min(score, 30), cv

        return 0, cv

    def _check_input_regularity(self) -> tuple[int, Optional[float]]:
        """Check if input events arrive at suspiciously regular intervals.

        Returns (score 0-25, coefficient_of_variation or None).
        """
        cfg = self._config
        if len(self._input_timestamps) < cfg.min_input_events_for_regularity:
            return 0, None

        sorted_ts = sorted(self._input_timestamps)
        gaps = []
        for i in range(1, len(sorted_ts)):
            gap = (sorted_ts[i] - sorted_ts[i - 1]).total_seconds()
            if gap > 0:
                gaps.append(gap)

        if len(gaps) < 2:
            return 0, None

        mean = sum(gaps) / len(gaps)
        if mean == 0:
            return 0, None

        variance = sum((g - mean) ** 2 for g in gaps) / len(gaps)
        std_dev = math.sqrt(variance)
        cv = std_dev / mean

        if cv < cfg.input_regularity_cv_threshold:
            score = int(25 * (1 - cv / cfg.input_regularity_cv_threshold))
            return min(score, 25), cv

        return 0, cv

    def _check_mouse_only_streak(self) -> int:
        """Check for consecutive windows with clicks but no other input.

        Returns score 0-20.
        """
        cfg = self._config
        if self._mouse_only_streak >= cfg.mouse_only_streak_threshold:
            # Scale: at threshold -> 10, at 2x threshold -> 20
            excess = self._mouse_only_streak - cfg.mouse_only_streak_threshold
            score = 10 + min(excess * 5, 10)
            return min(score, 20)
        return 0

    def _check_app_diversity(self) -> int:
        """Check if too few unique apps over extended active time.

        Returns score 0-15.
        """
        cfg = self._config
        if self._active_minutes < cfg.app_diversity_min_minutes:
            return 0

        if len(self._unique_apps) < cfg.min_app_diversity:
            return 15
        return 0

    def _check_click_keystroke_ratio(self) -> int:
        """Check for abnormally high click-to-keystroke ratio.

        Returns score 0-10.
        """
        cfg = self._config
        if self._total_clicks < 100:
            return 0
        if self._total_presses == 0:
            return 10
        ratio = self._total_clicks / self._total_presses
        if ratio > cfg.click_keystroke_ratio_threshold:
            return 10
        return 0

    def clear(self) -> None:
        """Reset all session-level accumulators."""
        self._window_press_counts.clear()
        self._unique_apps.clear()
        self._mouse_only_streak = 0
        self._input_timestamps.clear()
        self._total_clicks = 0
        self._total_presses = 0
        self._active_minutes = 0.0


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

    def __init__(
        self,
        thresholds: Optional[EngagementThresholds] = None,
        fraud_config=None,
    ):
        """Initialize the analyzer.

        Args:
            thresholds: Optional custom thresholds. Defaults to EngagementThresholds().
            fraud_config: Optional FraudDetectionConfig for fraud signal detection.
        """
        self._thresholds = thresholds or EngagementThresholds()
        self._input_events: list[AWEvent] = []
        self._window_events: list[AWEvent] = []
        self._fraud_detector = FraudSignalDetector(config=fraud_config)
        self._last_fraud_window_count: int = 0

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

    def update_fraud_config(self, fraud_config) -> None:
        """Update fraud detection config from server."""
        self._fraud_detector.update_config(fraud_config)
        logger.debug("Updated fraud detection config")

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

        # Record timestamps for fraud regularity analysis
        for e in new_events:
            self._fraud_detector.record_input_timestamp(e.timestamp)

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

    def get_fraud_assessment(self, timestamp: datetime, app: Optional[str] = None) -> FraudAssessment:
        """Get fraud assessment incorporating session-level signals.

        Records the current window metrics into the fraud detector (if not
        already recorded) and returns the combined fraud assessment.

        Args:
            timestamp: The timestamp to assess.
            app: Optional app name for app diversity tracking.

        Returns:
            FraudAssessment with score, signals, and extra metrics.
        """
        metrics = self._compute_metrics(timestamp)

        # Record this window's metrics into the fraud detector.
        # Use a simple counter to avoid recording the same conceptual window twice.
        current_window_count = len(self._window_events) + len(self._input_events)
        if current_window_count != self._last_fraud_window_count:
            self._fraud_detector.record_window_metrics(metrics, app=app)
            self._last_fraud_window_count = current_window_count

        # Track active time for app diversity checks
        if metrics.is_engaged(self._thresholds):
            self._fraud_detector.add_active_time(self._thresholds.window_minutes * 60)

        return self._fraud_detector.assess()

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
        """Clear all stored events and reset session-level fraud tracking."""
        self._input_events.clear()
        self._window_events.clear()
        self._fraud_detector.clear()
        self._last_fraud_window_count = 0
