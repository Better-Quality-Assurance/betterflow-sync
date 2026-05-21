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
from typing import Optional

try:
    from ..ui.permissions import check_accessibility, check_input_monitoring
except ImportError:
    from ui.permissions import check_accessibility, check_input_monitoring

logger = logging.getLogger(__name__)

# Quartz event type constants (defined here to avoid import at module level)
_kCGEventKeyDown = 10
_kCGEventLeftMouseDown = 1
_kCGEventRightMouseDown = 3
_kCGEventOtherMouseDown = 25
_kCGEventScrollWheel = 22
_kCGEventTapDisabledByTimeout = 0xFFFFFFFE

_EVENT_MASK = (
    (1 << _kCGEventKeyDown)
    | (1 << _kCGEventLeftMouseDown)
    | (1 << _kCGEventRightMouseDown)
    | (1 << _kCGEventOtherMouseDown)
    | (1 << _kCGEventScrollWheel)
)


class MacOSInputWatcher:
    """Daemon threads that count input events via CGEventTap and post to AW."""

    # How often the permission-retry thread re-checks Accessibility +
    # Input Monitoring after start() failed because of a missing grant.
    # Keep it modest so users see input tracking come online within ~30s
    # of toggling the switch in System Settings, without spamming logs.
    _PERMISSION_RETRY_INTERVAL_S = 30.0

    def __init__(self, aw_client, emit_interval: float = 10.0):
        self._aw = aw_client
        self._emit_interval = emit_interval
        self._stop_event = threading.Event()
        self._tap_thread: Optional[threading.Thread] = None
        self._emit_thread: Optional[threading.Thread] = None
        self._hostname = platform.node()
        self._bucket_id = f"aw-watcher-input_{self._hostname}"

        # Counters — updated from CGEventTap callback, read/reset by emitter
        self._presses = 0
        self._clicks = 0
        self._scrolls = 0
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
        self._run_loop = None
        self._tap_ref = None

        self._tap_thread = threading.Thread(
            target=self._run_tap, daemon=True, name="macos-input-tap",
        )
        self._emit_thread = threading.Thread(
            target=self._run_emitter, daemon=True, name="macos-input-emitter",
        )
        self._tap_thread.start()
        self._emit_thread.start()
        logger.info(
            f"MacOSInputWatcher started (bucket={self._bucket_id}, "
            f"interval={self._emit_interval}s)"
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
            # prompt=False so we don't keep showing dialogs on every retry;
            # the first start() already registered the app in System Settings.
            if check_accessibility() and check_input_monitoring(prompt=False):
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
        """CGEventTap callback — increment counters. Never reads key values."""
        if event_type == _kCGEventTapDisabledByTimeout:
            # Re-enable the tap if macOS disabled it
            if self._tap_ref is not None:
                try:
                    from Quartz import CGEventTapEnable
                    CGEventTapEnable(self._tap_ref, True)
                    logger.debug("Re-enabled CGEventTap after timeout")
                except Exception as e:
                    logger.warning(f"Failed to re-enable CGEventTap: {e}")
            return event

        with self._lock:
            if event_type == _kCGEventKeyDown:
                self._presses += 1
            elif event_type in (
                _kCGEventLeftMouseDown,
                _kCGEventRightMouseDown,
                _kCGEventOtherMouseDown,
            ):
                self._clicks += 1
            elif event_type == _kCGEventScrollWheel:
                self._scrolls += 1

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
            # Snapshot without zeroing — we only zero AFTER the post succeeds.
            # Previously we zeroed under the lock and then posted: if the post
            # failed, that batch's counters were lost forever. Now a failure
            # leaves counters intact so the next cycle picks them up.
            with self._lock:
                presses = self._presses
                clicks = self._clicks
                scrolls = self._scrolls

            # Skip zero-count events to avoid 8,640 idle events/day.
            # AFK bucket already handles idle detection.
            if presses == 0 and clicks == 0 and scrolls == 0:
                continue

            # Tag the batch with the currently focused app so the server can
            # attribute these keystrokes to a real aggregate row rather than
            # dumping them all into "Unknown". Close enough for fraud
            # detection at a 10s window — if the user switched apps mid-batch
            # we attribute everything to whichever app was frontmost at emit
            # time, which is a small, acceptable approximation.
            app_name, app_bundle = self._current_frontmost_app()

            try:
                now = datetime.now(timezone.utc).isoformat()
                data = {
                    "presses": presses,
                    "clicks": clicks,
                    "scrolls": scrolls,
                }
                if app_name:
                    data["app"] = app_name
                if app_bundle:
                    data["bundle"] = app_bundle
                self._aw.post_events(self._bucket_id, [{
                    "timestamp": now,
                    "duration": self._emit_interval,
                    "data": data,
                }])
            except Exception as e:
                # Leave the counters alone so the next tick re-sends them.
                logger.warning("Input emitter post failed (will retry): %s", e)
                continue

            # Post succeeded — atomically subtract the counts we just sent.
            # Subtracting (rather than zeroing) preserves any events the event
            # tap recorded between the snapshot and this point.
            with self._lock:
                self._presses -= presses
                self._clicks -= clicks
                self._scrolls -= scrolls
