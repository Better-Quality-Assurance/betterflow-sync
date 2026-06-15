"""Call/meeting detection from window events.

Identifies active calls by matching app names and window titles against
known patterns for video conferencing tools (Zoom, Teams, Meet, etc.).
Uses a state machine to track call duration, with a grace period to
avoid splitting a single call when the user briefly switches apps.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CallEvent:
    """Emitted when a detected call ends."""

    app: str  # Normalized name: "Zoom", "Google Meet", etc.
    start: datetime
    end: datetime
    duration: float  # seconds (end - start)
    call_type: str  # "native" or "browser"


# -- Native app patterns ------------------------------------------------
# Each entry: (app-name substring aliases, title regex or None, display name).
# Aliases cover macOS app names AND Windows/Linux process names — e.g. macOS
# reports "zoom.us" / "Microsoft Teams" while Windows reports "Zoom" / the
# new-Teams "ms-teams". A single macOS-only substring meant native calls went
# undetected on Windows (Sachi, 2026-06-15). Each entry stays title-gated, so
# widening the app match can never turn "app merely open" into a false call.
_NATIVE_PATTERNS: list[tuple[tuple[str, ...], Optional[re.Pattern], str]] = [
    (("zoom.us", "zoom"), re.compile(r"Zoom (Meeting|Webinar)|\d{9,11}", re.IGNORECASE), "Zoom"),
    (("microsoft teams", "ms-teams", "msteams", "teams"), re.compile(r"(Meeting|Call) with|In a call", re.IGNORECASE), "Microsoft Teams"),
    (("slack",), re.compile(r"Huddle", re.IGNORECASE), "Slack"),
    (("discord",), re.compile(r"Voice Connected|voice channel", re.IGNORECASE), "Discord"),
    (("facetime",), None, "FaceTime"),  # Any active FaceTime window = call
    (("webex",), re.compile(r"Meeting", re.IGNORECASE), "Webex"),
]

# -- Browser URL patterns -----------------------------------------------
# Each entry: (url substring, extra url regex or None, display name)
_BROWSER_PATTERNS: list[tuple[str, Optional[re.Pattern], str]] = [
    ("meet.google.com/", re.compile(r"meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}"), "Google Meet"),
    ("teams.microsoft.com/", re.compile(r"teams\.microsoft\.com.*/meeting", re.IGNORECASE), "Microsoft Teams"),
    ("zoom.us/j/", None, "Zoom"),
    ("zoom.us/wc/", None, "Zoom"),
    ("app.slack.com/", re.compile(r"huddle", re.IGNORECASE), "Slack"),
]

# Default grace period: if user switches away for less than this many
# seconds, treat it as still in the same call.
_DEFAULT_GRACE_PERIOD = 30.0


def _match_native(app: str, title: str) -> Optional[str]:
    """Return display name if (app, title) matches a native call pattern."""
    if not title:
        title = ""
    app_lower = app.lower()
    for app_substrs, title_re, name in _NATIVE_PATTERNS:
        if any(sub in app_lower for sub in app_substrs) and (
            title_re is None or title_re.search(title)
        ):
            return name
    return None


def _match_browser(url: str) -> Optional[str]:
    """Return display name if url matches a browser call pattern."""
    if not url:
        return None
    for url_substr, url_re, name in _BROWSER_PATTERNS:
        if url_substr in url and (url_re is None or url_re.search(url)):
            return name
    return None


class CallDetector:
    """Detects calls from a stream of window events.

    Feed events chronologically via process_event(). When a call ends,
    it returns a CallEvent. Call flush() at sync boundaries to emit any
    ongoing call.

    State machine:
        IDLE --(call pattern)--> IN_CALL
        IN_CALL --(same call continues)--> IN_CALL
        IN_CALL --(non-call event, within grace)--> IN_CALL (grace)
        IN_CALL --(non-call event, grace expired)--> IDLE (emit CallEvent)
    """

    def __init__(
        self,
        min_duration: int = 30,
        grace_period: float = _DEFAULT_GRACE_PERIOD,
    ):
        self._min_duration = min_duration
        self._grace_period = grace_period

        # Active call state
        self._call_app: Optional[str] = None
        self._call_type: Optional[str] = None
        self._call_start: Optional[datetime] = None
        self._call_last_seen: Optional[datetime] = None
        # Timestamp when user first left the call app (for grace tracking)
        self._left_at: Optional[datetime] = None

    def process_event(
        self,
        app: str,
        title: str,
        url: Optional[str],
        timestamp: datetime,
        duration: float,
    ) -> Optional[CallEvent]:
        """Process a window event. Returns a CallEvent if a call just ended."""
        # Check if this event is a call
        matched_name = _match_native(app, title)
        call_type = "native" if matched_name else None
        if not matched_name and url:
            matched_name = _match_browser(url)
            call_type = "browser" if matched_name else None

        if self._call_app is None:
            # IDLE state
            if matched_name:
                self._start_call(matched_name, call_type, timestamp)
            return None

        # IN_CALL state
        if matched_name and matched_name == self._call_app:
            # Same call continues
            self._call_last_seen = timestamp
            self._left_at = None
            return None

        if matched_name and matched_name != self._call_app:
            # Switched to a different call app - end the current call
            # at the switch timestamp, start a new one
            ended = self._end_call(end_override=timestamp)
            self._start_call(matched_name, call_type, timestamp)
            return ended

        # Non-call event while in a call
        if self._left_at is None:
            # First non-call event: start grace timer
            self._left_at = timestamp
            return None

        # Check if grace period expired (guard against out-of-order timestamps)
        gap = max(0.0, (timestamp - self._left_at).total_seconds())
        if gap >= self._grace_period:
            # Grace expired: end the call (end time = when user left)
            return self._end_call()

        # Still within grace period
        return None

    def flush(self) -> Optional[CallEvent]:
        """Force-end any active call. Call at sync boundaries."""
        if self._call_app is not None:
            return self._end_call()
        return None

    def is_in_call(self) -> bool:
        """True while a call/meeting is currently active (incl. grace period).

        Used by idle detection to avoid marking the user idle during a call:
        in a meeting they're engaged (listening/watching) even with no
        keyboard/mouse input, which would otherwise trip the AFK-only idle
        pause — especially on Windows, which has no in-process input watcher.
        """
        return self._call_app is not None

    def _start_call(
        self, name: str, call_type: Optional[str], timestamp: datetime
    ) -> None:
        self._call_app = name
        self._call_type = call_type or "native"
        self._call_start = timestamp
        self._call_last_seen = timestamp
        self._left_at = None
        logger.info(f"Call started: {name} ({call_type}) at {timestamp.isoformat()}")

    def _end_call(self, end_override: Optional[datetime] = None) -> Optional[CallEvent]:
        """End the current call and return a CallEvent (or None if too short).

        Args:
            end_override: Use this timestamp as end time instead of inferring
                from _left_at/_call_last_seen (e.g. when switching calls).
        """
        if self._call_start is None:
            self._reset()
            return None

        end = end_override or (self._left_at if self._left_at else self._call_last_seen)
        if end is None:
            end = self._call_start
        duration = (end - self._call_start).total_seconds()

        app = self._call_app or "Unknown"
        call_type = self._call_type or "native"
        start = self._call_start

        logger.info(f"Call ended: {app} duration={duration:.0f}s")
        self._reset()

        if duration < self._min_duration:
            logger.debug(f"Call too short ({duration:.0f}s < {self._min_duration}s), discarding")
            return None

        return CallEvent(
            app=app,
            start=start,
            end=end,
            duration=round(duration, 2),
            call_type=call_type,
        )

    def _reset(self) -> None:
        self._call_app = None
        self._call_type = None
        self._call_start = None
        self._call_last_seen = None
        self._left_at = None
