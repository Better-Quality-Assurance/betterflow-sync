"""In-process macOS input watcher using Quartz CGEventTap.

Counts keystrokes, mouse clicks, and scroll events without capturing
any content (privacy-safe). Posts aggregated counts to the local
ActivityWatch server as input bucket events for fraud detection.

Requires the same Accessibility permission as the window watcher.
"""

import logging
import platform
import threading
from datetime import datetime, timezone
from typing import Callable, Optional, Union

try:
    from ..ui.permissions import (
        check_accessibility,
        check_input_monitoring,
        has_capture_permissions,
    )
except ImportError:
    from ui.permissions import (
        check_accessibility,
        check_input_monitoring,
        has_capture_permissions,
    )

try:
    from .input_span import DEFAULT_MAX_SILENCE_S, HeldInputSpans
except ImportError:  # PyInstaller bundle (src/ is import root)
    from sync.input_span import DEFAULT_MAX_SILENCE_S, HeldInputSpans

logger = logging.getLogger(__name__)

# Quartz event type constants (defined here to avoid import at module level)
_kCGEventKeyDown = 10
_kCGEventLeftMouseDown = 1
_kCGEventRightMouseDown = 3
_kCGEventOtherMouseDown = 25
_kCGEventScrollWheel = 22
# CGEventField kCGKeyboardEventAutorepeat: non-zero on a held key's repeats.
_kCGKeyboardEventAutorepeat = 8  # noqa: N816 — Quartz name
_kCGEventTapDisabledByTimeout = 0xFFFFFFFE
# macOS disables a tap for two reasons, not one. Handling only the timeout case
# left a tap disabled by user input dead forever: counters frozen at 0 while
# is_running() still reports True, which reads downstream as "user is idle".
_kCGEventTapDisabledByUserInput = 0xFFFFFFFF
_TAP_DISABLED_EVENT_TYPES = (
    _kCGEventTapDisabledByTimeout,
    _kCGEventTapDisabledByUserInput,
)

_EVENT_MASK = (
    (1 << _kCGEventKeyDown)
    | (1 << _kCGEventLeftMouseDown)
    | (1 << _kCGEventRightMouseDown)
    | (1 << _kCGEventOtherMouseDown)
    | (1 << _kCGEventScrollWheel)
)


def _is_autorepeat(event) -> bool:
    """True when a keyDown is a held key's auto-repeat, which is real input but
    not a new key press. Reads only the autorepeat flag, never the key. Any
    failure answers False, so the press is counted (the behaviour before this
    filter existed) rather than silently dropped."""
    try:
        from Quartz import CGEventGetIntegerValueField
        return CGEventGetIntegerValueField(event, _kCGKeyboardEventAutorepeat) != 0
    except Exception:
        return False


class MacOSInputWatcher:
    """Daemon threads that count input events via CGEventTap and post to AW."""

    # How often the permission-retry thread re-checks Accessibility +
    # Input Monitoring after start() failed because of a missing grant.
    # Keep it modest so users see input tracking come online within ~30s
    # of toggling the switch in System Settings, without spamming logs.
    _PERMISSION_RETRY_INTERVAL_S = 30.0


    def __init__(
        self,
        aw_client,
        emit_interval: float = 10.0,
        count_only: bool = False,
        max_silence_s: Union[float, Callable[[], float]] = DEFAULT_MAX_SILENCE_S,
    ):
        self._aw = aw_client
        self._emit_interval = emit_interval
        # count_only: run ONLY the CGEventTap counter — no emitter thread and no
        # AW bucket. The in-process InputSource macOS backend uses this and
        # drains the raw counters itself; running the emitter too would subtract
        # the counts out from under the backend and undercount.
        self._count_only = count_only
        self._stop_event = threading.Event()
        self._tap_thread: Optional[threading.Thread] = None
        self._emit_thread: Optional[threading.Thread] = None
        self._hostname = platform.node()
        self._bucket_id = f"aw-watcher-input_{self._hostname}"

        # Raw counters — count_only mode only: updated from the CGEventTap
        # callback, read-and-zeroed by the in-process backend's poll
        self._presses = 0
        self._clicks = 0
        self._scrolls = 0
        # Most-recent input timestamp — updated on every event, read by the
        # SyncCoordinator's bf-idle-tracker health check. Distinct from the
        # emitted counters: those reset to 0 each emit cycle, this stays
        # monotonic across the watcher's lifetime so a 2-min "did we see ANY
        # input recently" probe works regardless of where in the emit cycle
        # we land.
        self._last_input_at: Optional[datetime] = None
        # The same held counts as segments with first/last count instants, split
        # at silences longer than max_silence_s (the configured AFK timeout). The
        # emitter stamps each segment's own span from it (src/sync/input_span.py,
        # the rule shared with InputSource): an outage's counts are not crammed
        # into the last 10 s (implausible_input_rate), and counts held across a
        # sleep or a long break are not stretched over it (device 53, 2026-11-09:
        # one press held across a 68 h sleep was billed 13.6 h). Emitter mode
        # only: in count_only mode the InputSource keeps its own. Local only.
        self._held = HeldInputSpans(max_silence_s=max_silence_s, pad_s=emit_interval)
        # The instant up to which the emitter has accounted for input: its last
        # tick that found nothing held, or its last successful post. The floor a
        # held span may start from, like the in-process stream's checkpoint.
        self._accounted_until: Optional[datetime] = None
        self._lock = threading.Lock()

        # Store CFRunLoop ref so stop() can break out of it
        self._run_loop = None
        self._tap_ref = None
        self._loop_ready = threading.Event()

        # Permission self-heal: when start() fails because Accessibility
        # or Input Monitoring isn't granted yet, this thread polls every
        # _PERMISSION_RETRY_INTERVAL_S and calls start() again once both
        # are present. Avoids needing the user to restart the app after
        # toggling permissions in System Settings.
        self._permission_retry_thread: Optional[threading.Thread] = None
        self._permission_retry_stop = threading.Event()

    @property
    def is_running(self) -> bool:
        """True if threads are alive."""
        return (
            (self._tap_thread is not None and self._tap_thread.is_alive())
            or (self._emit_thread is not None and self._emit_thread.is_alive())
        )

    def get_last_input_at(self) -> Optional[datetime]:
        """Return the timestamp of the most-recent observed input event.

        Used by the SyncCoordinator's idle-tracker health check to detect
        when bf-idle-tracker is reporting AFK while the main-process input
        watcher is still seeing keystrokes — the signature of a missing
        Input Monitoring grant on the tracker subprocess (which has its own
        TCC subject distinct from the main app).

        Returns None when the watcher hasn't observed any input yet (fresh
        start, or running headless during tests).
        """
        with self._lock:
            return self._last_input_at

    def start(self) -> bool:
        """Create the AW bucket and start the tap + emitter threads."""
        # Guard against double-start (e.g. idle resume while already running)
        if self.is_running:
            logger.debug("MacOSInputWatcher already running — skipping start")
            return True

        try:
            from Quartz import (  # noqa: F401
                CGEventTapCreate,
                kCGSessionEventTap,
                kCGHeadInsertEventTap,
                kCGEventTapOptionListenOnly,
            )
        except ImportError:
            logger.warning("Quartz not available — input tracking disabled")
            return False

        if not check_accessibility():
            logger.warning(
                "Process does NOT have Accessibility permission — input tracking disabled"
            )
            self._ensure_permission_retry()
            return False
        # Passing prompt=True calls CGRequestListenEventAccess, which shows
        # Apple's native Input Monitoring dialog on first launch and registers
        # the app in System Settings > Privacy & Security > Input Monitoring
        # so the user can toggle it later. Without this, the app silently
        # disables keypress tracking until the user finds the pane manually.
        if not check_input_monitoring(prompt=True):
            logger.warning(
                "Process does NOT have Input Monitoring permission — keypress tracking disabled"
            )
            self._ensure_permission_retry()
            return False
        if not self._count_only:
            try:
                self._aw.create_bucket(self._bucket_id, "aw-watcher-input", self._hostname)
            except Exception as e:
                logger.warning(f"Failed to create input bucket: {e}")

        # Reset stop signal so new threads don't exit immediately
        self._stop_event.clear()
        self._loop_ready.clear()
        with self._lock:
            self._presses = 0
            self._clicks = 0
            self._scrolls = 0
            self._held.clear()
            self._accounted_until = None
        self._run_loop = None
        self._tap_ref = None

        self._tap_thread = threading.Thread(
            target=self._run_tap, daemon=True, name="macos-input-tap",
        )
        self._tap_thread.start()
        # count_only: no emitter — the backend drains the counters itself.
        if not self._count_only:
            self._emit_thread = threading.Thread(
                target=self._run_emitter, daemon=True, name="macos-input-emitter",
            )
            self._emit_thread.start()
        logger.info(
            f"MacOSInputWatcher started (bucket={self._bucket_id}, "
            f"count_only={self._count_only}, interval={self._emit_interval}s)"
        )
        return True

    def _ensure_permission_retry(self) -> None:
        """Spawn (or keep alive) a background thread that retries start()
        every _PERMISSION_RETRY_INTERVAL_S until both Accessibility and
        Input Monitoring are granted, then exits."""
        if self._permission_retry_thread and self._permission_retry_thread.is_alive():
            return
        self._permission_retry_stop.clear()
        self._permission_retry_thread = threading.Thread(
            target=self._permission_retry_loop,
            daemon=True,
            name="macos-input-permission-retry",
        )
        self._permission_retry_thread.start()
        logger.info(
            "Input watcher will recheck permissions every "
            f"{int(self._PERMISSION_RETRY_INTERVAL_S)}s until granted"
        )

    def _permission_retry_loop(self) -> None:
        while not self._permission_retry_stop.wait(self._PERMISSION_RETRY_INTERVAL_S):
            if self.is_running:
                # Another start() call already brought us online.
                return
            # has_capture_permissions() is the one definition of this pair
            # (src/ui/permissions.py). check_input_monitoring defaults to
            # prompt=False, so this does not re-show dialogs on every retry —
            # the first start() already registered the app in System Settings.
            if has_capture_permissions():
                logger.info(
                    "Input watcher permissions now granted — starting capture"
                )
                if self.start():
                    return
                # start() failed for some other reason; keep retrying.

    def stop(self) -> None:
        """Signal threads to stop and clean up."""
        # Always stop the permission-retry watchdog, even if no capture
        # threads are running yet.
        self._permission_retry_stop.set()
        if (
            self._permission_retry_thread
            and self._permission_retry_thread.is_alive()
            and self._permission_retry_thread is not threading.current_thread()
        ):
            self._permission_retry_thread.join(timeout=2.0)
        self._permission_retry_thread = None

        if not self.is_running:
            return

        self._stop_event.set()

        # Wait for the run loop to be initialised before stopping it
        self._loop_ready.wait(timeout=2.0)

        # Break the CFRunLoop so the tap thread exits
        if self._run_loop is not None:
            try:
                from CoreFoundation import CFRunLoopStop
                CFRunLoopStop(self._run_loop)
            except Exception as e:
                logger.warning("CFRunLoopStop failed during shutdown: %s", e)

        if self._tap_thread and self._tap_thread.is_alive():
            self._tap_thread.join(timeout=3.0)
        if self._tap_thread and not self._tap_thread.is_alive():
            self._tap_thread = None
        if self._emit_thread and self._emit_thread.is_alive():
            self._emit_thread.join(timeout=3.0)
        if self._emit_thread and not self._emit_thread.is_alive():
            self._emit_thread = None
        logger.info("MacOSInputWatcher stopped")

    def _current_frontmost_app(self) -> tuple[Optional[str], Optional[str]]:
        """Return (localizedName, bundleIdentifier) of the frontmost app.

        Called from the emitter thread only — NSWorkspace is not safe to
        call from the CGEventTap callback, but read access from a regular
        background thread is fine.
        """
        try:
            from AppKit import NSWorkspace
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            if app is None:
                return (None, None)
            name = app.localizedName()
            bundle = app.bundleIdentifier()
            return (str(name) if name else None, str(bundle) if bundle else None)
        except Exception as e:
            logger.debug("frontmostApplication lookup failed: %s", e)
            return (None, None)

    def _event_callback(self, proxy, event_type, event, refcon):
        """CGEventTap callback — increment counters. Never reads key values;
        on a keyDown it reads only the autorepeat flag, so a held key counts
        as one press."""
        if event_type in _TAP_DISABLED_EVENT_TYPES:
            # Re-enable the tap if macOS disabled it. A tap that stays disabled
            # never increments a counter again, so the agent silently reports
            # the user idle instead of reporting itself blind — log at warning
            # so a re-enable that fails is visible in the field.
            reason = (
                "timeout"
                if event_type == _kCGEventTapDisabledByTimeout
                else "user input"
            )
            if self._tap_ref is not None:
                try:
                    from Quartz import CGEventTapEnable
                    CGEventTapEnable(self._tap_ref, True)
                    logger.warning("Re-enabled CGEventTap after %s disable", reason)
                except Exception as e:
                    logger.warning("Failed to re-enable CGEventTap after %s disable: %s", reason, e)
            else:
                logger.warning("CGEventTap disabled by %s but no tap ref to re-enable", reason)
            return event

        # Read outside the lock: it calls into Quartz.
        repeat = event_type == _kCGEventKeyDown and _is_autorepeat(event)
        # Classify ONCE: the counters and the held spans are both fed from this
        # one triple, so they cannot disagree about what an event counts as.
        presses = clicks = scrolls = 0
        recognized = True
        if event_type == _kCGEventKeyDown:
            # A repeat stays "recognized": it is real input, so it still
            # moves the input-observed timestamp below; it is not a press.
            if not repeat:
                presses = 1
        elif event_type in (
            _kCGEventLeftMouseDown,
            _kCGEventRightMouseDown,
            _kCGEventOtherMouseDown,
        ):
            clicks = 1
        elif event_type == _kCGEventScrollWheel:
            scrolls = 1
        else:
            recognized = False
        with self._lock:
            if self._count_only:
                # count_only: the in-process backend drains these raw counters
                # (with _last_input_at as their instant) into InputSource's own
                # HeldInputSpans. The emitter never reads them.
                self._presses += presses
                self._clicks += clicks
                self._scrolls += scrolls
            if recognized:
                # Monotonic input-observed timestamp used by the bf-idle-tracker
                # health check — see SyncCoordinator._check_idle_tracker_health.
                # We update under the same lock that guards the counters so a
                # concurrent reader gets a coherent (count, ts) pair.
                now = datetime.now(timezone.utc)
                self._last_input_at = now
                if not self._count_only:
                    self._held.note(
                        now, presses=presses, clicks=clicks, scrolls=scrolls,
                    )

        return event

    def _run_tap(self) -> None:
        """Create CGEventTap and run its CFRunLoop."""
        try:
            from Quartz import (
                CGEventTapCreate,
                CGEventTapEnable,
                kCGSessionEventTap,
                kCGHeadInsertEventTap,
                kCGEventTapOptionListenOnly,
            )
            from CoreFoundation import (
                CFMachPortCreateRunLoopSource,
                CFRunLoopGetCurrent,
                CFRunLoopAddSource,
                CFRunLoopRun,
                kCFRunLoopCommonModes,
            )
        except ImportError:
            logger.error("Failed to import Quartz/CoreFoundation for input tap")
            return

        tap = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            kCGEventTapOptionListenOnly,
            _EVENT_MASK,
            self._event_callback,
            None,
        )

        if tap is None:
            logger.error(
                "CGEventTapCreate returned None — Accessibility permission "
                "may not be granted. Input tracking disabled."
            )
            self._loop_ready.set()  # unblock stop() immediately
            return

        self._tap_ref = tap
        CGEventTapEnable(tap, True)

        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        self._run_loop = CFRunLoopGetCurrent()
        CFRunLoopAddSource(self._run_loop, source, kCFRunLoopCommonModes)

        logger.debug("CGEventTap run loop starting")
        self._loop_ready.set()
        CFRunLoopRun()
        logger.debug("CGEventTap run loop exited")

    def _run_emitter(self) -> None:
        """Periodically read counters and post events to AW."""
        while not self._stop_event.wait(self._emit_interval):
            # Snapshot without zeroing — we only remove what was sent AFTER the
            # post succeeds, so a failed post leaves the counts for the next tick.
            with self._lock:
                emit_now = datetime.now(timezone.utc)
                # Gate on the held spans, the same store the events are built
                # from, so a nonzero gate can never meet an empty span list.
                # Skip zero-count events to avoid 8,640 idle events/day.
                # AFK bucket already handles idle detection. Nothing held means
                # everything up to now is accounted for.
                if not any(self._held.totals()):
                    self._accounted_until = emit_now
                    continue
                # One span per segment of held counts, never one span over all
                # of them: see src/sync/input_span.py. In steady state that is a
                # single [last tick, now] span, the old [now - 10 s, now] stamp.
                spans = self._held.stamp(emit_now, floor=self._accounted_until)

            if not spans:
                continue

            # Tag the batch with the currently focused app so the server can
            # attribute these keystrokes to a real aggregate row rather than
            # dumping them all into "Unknown". Close enough for fraud
            # detection at a 10s window — if the user switched apps mid-batch
            # we attribute everything to whichever app was frontmost at emit
            # time, which is a small, acceptable approximation.
            app_name, app_bundle = self._current_frontmost_app()

            events = []
            for span in spans:
                data = {
                    "presses": span.counts[0],
                    "clicks": span.counts[1],
                    "scrolls": span.counts[2],
                }
                if app_name:
                    data["app"] = app_name
                if app_bundle:
                    data["bundle"] = app_bundle
                events.append({
                    "timestamp": span.start.isoformat(),
                    "duration": span.duration_s,
                    "data": data,
                })
            try:
                self._aw.post_events(self._bucket_id, events)
            except Exception as e:
                # Leave the counters alone so the next tick re-sends them.
                logger.warning("Input emitter post failed (will retry): %s", e)
                continue

            # Post succeeded — atomically remove the counts we just sent.
            # Removing (rather than clearing) preserves any events the event
            # tap recorded between the snapshot and this point. consume() clamps
            # to zero and skips segments already gone, defending against the
            # (narrow) permission-retry race where two emitter threads briefly
            # run concurrently and would otherwise double-subtract the same
            # snapshot into negative counts — negative values would corrupt the
            # server-side fraud signal.
            with self._lock:
                # Whatever is left arrived after the snapshot, so its span
                # starts no earlier than this post.
                self._held.consume(spans, taken_at=emit_now)
                if self._accounted_until is None or emit_now > self._accounted_until:
                    self._accounted_until = emit_now
