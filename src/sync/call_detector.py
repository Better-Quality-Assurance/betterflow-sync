"""Call/meeting detection from window events.

Identifies active calls by matching app names and window titles against
known patterns for video conferencing tools (Zoom, Teams, Meet, etc.).
Uses a state machine to track call duration, with a grace period to
avoid splitting a single call when the user briefly switches apps.
"""

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
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
    # NOTE: dropped the old `\d{9,11}` Zoom fallback — a 9–11 digit ticket /
    # phone / timestamp in a Zoom window title was mis-read as a meeting ID
    # (mirrors the PHP CallEventDetector E3 fix; KEEP THE TWO IN SYNC). Real
    # Zoom meetings still match the "Zoom Meeting"/"Zoom Webinar" native title
    # or the zoom.us/j/ | zoom.us/wc/ browser URL below.
    (("zoom.us", "zoom"), re.compile(r"Zoom (Meeting|Webinar)", re.IGNORECASE), "Zoom"),
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


# Bare aliases matched on a WORD BOUNDARY rather than as a substring, so they
# can't bleed into an unrelated app name:
#   - "teams" must not match "teamspeak"/"teamviewer", "zoom" not "zoomtext".
#   - "facetime" must not match "FaceTimeHelper" / other facetime-prefixed
#     helper processes — and unlike the others it has NO title gate (any window
#     counts), so a loose substring there would be the easiest to false-trigger.
# Aliases kept as plain containment are EITHER title-gated ("slack"/"discord"/
# "msteams" need an in-call title too, so a stray substring match can't bill on
# its own) OR must match a compound process name — notably "webex", which has to
# match the Windows process "CiscoWebexStart" where a \b boundary would break it.
_WORD_BOUNDARY_ALIASES = frozenset({"teams", "zoom", "facetime"})


def _app_contains(app_lower: str, sub: str) -> bool:
    """Match an app-name alias against the (lowercased) app name.

    Word-boundary for the ambiguous bare aliases in _WORD_BOUNDARY_ALIASES;
    plain containment for everything else (separator aliases like "ms-teams"
    where \\b is wrong, and distinctive substrings like "webex" that must still
    match compound process names).
    """
    if sub in _WORD_BOUNDARY_ALIASES:
        return re.search(rf"\b{re.escape(sub)}\b", app_lower) is not None
    return sub in app_lower


def _match_native(app: str, title: str) -> Optional[str]:
    """Return display name if (app, title) matches a native call pattern."""
    if not title:
        title = ""
    app_lower = app.lower()
    for app_substrs, title_re, name in _NATIVE_PATTERNS:
        if any(_app_contains(app_lower, sub) for sub in app_substrs) and (
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
        max_credit_seconds: float = 14400.0,
    ):
        self._min_duration = min_duration
        self._grace_period = grace_period
        # Hard ceiling on AFK credit per call (get_last_active_at). A window
        # title stuck matching a call pattern must not keep the uploaded AFK
        # stream not-afk indefinitely — credit stops max_credit_seconds after
        # the call started, even if the "call" never ends.
        self._max_credit_seconds = float(max_credit_seconds)

        # State is mutated on the sync thread (process_event/flush) but read
        # from the idle thread (is_in_call) and inside AfkSource.record_sample
        # (get_last_active_at) — guard every access.
        self._lock = threading.Lock()

        # Active call state
        self._call_app: Optional[str] = None
        self._call_type: Optional[str] = None
        self._call_start: Optional[datetime] = None
        self._call_last_seen: Optional[datetime] = None
        # Timestamp when user first left the call app (for grace tracking)
        self._left_at: Optional[datetime] = None
        # Span of the most recently ENDED call (kept across _reset). AFK credit
        # (get_last_active_at) stays truthful after a call ends or is flushed:
        # the user was engaged until that end instant, so credit freezes there
        # instead of vanishing the moment the state machine resets.
        self._last_call_start: Optional[datetime] = None
        self._last_call_end: Optional[datetime] = None

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

        with self._lock:
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
        """Force-end any active call (shutdown / AW-outage boundaries)."""
        with self._lock:
            if self._call_app is not None:
                return self._end_call()
            return None

    def snapshot(self) -> Optional[CallEvent]:
        """Emit the currently-active call as a CallEvent WITHOUT ending it.

        Uploaded each sync cycle for live server-side visibility: the event id
        derives from (app, start), so the server upserts one growing row across
        cycles — mirroring ``ForegroundActivityDetector.snapshot``. The old
        per-cycle ``flush()`` at the sync boundary instead ENDED the call every
        cycle, which fragmented one meeting into per-cycle call rows, dropped
        ``is_in_call()`` to False between cycles (the idle-pause suppression
        raced and mostly never engaged), and reset the state the AFK activity
        source needs. None when no call is active or it is still shorter than
        ``min_duration``.
        """
        with self._lock:
            if self._call_app is None or self._call_start is None:
                return None
            end = self._left_at or self._call_last_seen or self._call_start
            duration = (end - self._call_start).total_seconds()
            if duration < self._min_duration:
                return None
            return CallEvent(
                app=self._call_app,
                start=self._call_start,
                end=end,
                duration=round(duration, 2),
                call_type=self._call_type or "native",
            )

    def is_in_call(self) -> bool:
        """True while a call/meeting is currently active (incl. grace period).

        Used by idle detection to avoid marking the user idle during a call:
        in a meeting they're engaged (listening/watching) even with no
        keyboard/mouse input, which would otherwise trip the AFK-only idle
        pause — especially on Windows, which has no in-process input watcher.
        """
        with self._lock:
            return self._call_app is not None

    def get_last_active_at(
        self, base_last_input: Optional[datetime], now: datetime
    ) -> Optional[datetime]:
        """Most recent call-engaged instant, for ``AfkSource`` to fold into its
        last-input reconciliation (activity-source contract, same as
        ``ForegroundActivityDetector.get_last_active_at``).

        While a call is active the user is engaged (listening/watching) even
        with no keyboard/mouse input, so the *uploaded* AFK stream — which the
        server bills active-vs-idle from — must stay not-afk. Suppressing only
        the local idle pause (``is_in_call``) never reached that stream, so a
        long hands-off meeting still painted Idle on the dashboard (Ecaterina's
        49-minute huddle, 2026-07-15).

        Credit rules:
          * None when no call was ever observed — never fabricates activity.
          * During the leave-grace window, credit stops at the instant the user
            left the call window (they produced real input to switch anyway).
          * After a call ends (or is flushed at an AW-outage/shutdown
            boundary), credit freezes at that call's END — the user was
            engaged until then, and that stays true forever after.
          * Capped at ``max_credit_seconds`` past call start, so a stuck title
            can't keep the stream not-afk forever. AfkSource additionally
            clamps any source's answer to ``now``.
        """
        with self._lock:
            if self._call_start is not None:
                engaged_until = self._left_at or now
                cap_end = self._call_start + timedelta(seconds=self._max_credit_seconds)
                return min(engaged_until, cap_end, now)
            if self._last_call_start is not None and self._last_call_end is not None:
                cap_end = self._last_call_start + timedelta(seconds=self._max_credit_seconds)
                return min(self._last_call_end, cap_end, now)
            return None

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
        # Remember the span for AFK credit even when the event itself is
        # discarded as too short — the engagement was real either way.
        self._last_call_start = self._call_start
        self._last_call_end = end

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
