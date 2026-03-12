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

    @property
    def is_running(self) -> bool:
        """True if threads are alive."""
        return (
            self._tap_thread is not None and self._tap_thread.is_alive()
            or self._emit_thread is not None and self._emit_thread.is_alive()
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

        try:
            self._aw.create_bucket(self._bucket_id, "aw-watcher-input", self._hostname)
        except Exception as e:
            logger.warning(f"Failed to create input bucket: {e}")

        # Reset stop signal so new threads don't exit immediately
        self._stop_event.clear()
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

    def stop(self) -> None:
        """Signal threads to stop and clean up."""
        if not self.is_running:
            return

        self._stop_event.set()

        # Break the CFRunLoop so the tap thread exits
        if self._run_loop is not None:
            try:
                from CoreFoundation import CFRunLoopStop
                CFRunLoopStop(self._run_loop)
            except Exception:
                pass

        if self._tap_thread and self._tap_thread.is_alive():
            self._tap_thread.join(timeout=3.0)
        if self._emit_thread and self._emit_thread.is_alive():
            self._emit_thread.join(timeout=3.0)
        self._tap_thread = None
        self._emit_thread = None
        logger.info("MacOSInputWatcher stopped")

    def _event_callback(self, proxy, event_type, event, refcon):
        """CGEventTap callback — increment counters. Never reads key values."""
        if event_type == _kCGEventTapDisabledByTimeout:
            # Re-enable the tap if macOS disabled it
            if self._tap_ref is not None:
                try:
                    from Quartz import CGEventTapEnable
                    CGEventTapEnable(self._tap_ref, True)
                    logger.debug("Re-enabled CGEventTap after timeout")
                except Exception:
                    pass
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
            return

        self._tap_ref = tap
        CGEventTapEnable(tap, True)

        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        self._run_loop = CFRunLoopGetCurrent()
        CFRunLoopAddSource(self._run_loop, source, kCFRunLoopCommonModes)

        logger.debug("CGEventTap run loop starting")
        CFRunLoopRun()
        logger.debug("CGEventTap run loop exited")

    def _run_emitter(self) -> None:
        """Periodically read counters and post events to AW."""
        while not self._stop_event.wait(self._emit_interval):
            try:
                # Atomically swap out the counters
                with self._lock:
                    presses = self._presses
                    clicks = self._clicks
                    scrolls = self._scrolls
                    self._presses = 0
                    self._clicks = 0
                    self._scrolls = 0

                # Always post — zero counts tell the backend "user was idle"
                now = datetime.now(timezone.utc).isoformat()
                self._aw.post_events(self._bucket_id, [{
                    "timestamp": now,
                    "duration": self._emit_interval,
                    "data": {
                        "presses": presses,
                        "clicks": clicks,
                        "scrolls": scrolls,
                    },
                }])
            except Exception as e:
                logger.debug(f"Input emitter error: {e}")
