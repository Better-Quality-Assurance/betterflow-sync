"""In-process macOS window watcher using native PyObjC APIs.

Replaces the bf-window-tracker subprocess on macOS so that window tracking
uses the main process's Accessibility permission directly (via PyObjC)
instead of shelling out to osascript (which is a separate binary and needs
its own Accessibility grant).

Follows the same daemon-thread pattern as display_info.py.
"""

import logging
import platform
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Browser bundle IDs for safe AppleScript invocation (prevents injection via app name).
# Using `tell application id "..."` instead of `tell application "..."`.
_BROWSER_BUNDLE_IDS: dict[str, str] = {
    "Google Chrome": "com.google.Chrome",
    "Google Chrome Canary": "com.google.Chrome.canary",
    "Chromium": "org.chromium.Chromium",
    "Brave Browser": "com.brave.Browser",
    "Safari": "com.apple.Safari",
    "Microsoft Edge": "com.microsoft.edgemac",
    "Arc": "company.thebrowser.Browser",
    "Firefox": "org.mozilla.firefox",
}

_CHROMIUM_BROWSERS = frozenset({
    "Google Chrome", "Google Chrome Canary", "Chromium", "Brave Browser",
    "Microsoft Edge", "Arc",
})
_URL_BROWSERS = _CHROMIUM_BROWSERS | {"Safari"}

# Terminal apps with AppleScript support for tab-specific titles.
_TERMINAL_BUNDLE_IDS: dict[str, str] = {
    "Terminal": "com.apple.Terminal",
    "iTerm2": "com.googlecode.iterm2",
    "iTerm": "com.googlecode.iterm2",
}


class MacOSWindowWatcher:
    """Daemon thread that polls the active window via PyObjC and posts heartbeats to AW."""

    # How often _run() re-checks AXIsProcessTrusted() so we log when
    # Accessibility goes from missing → granted (or vice versa) without
    # the user having to restart the app.
    _ACCESSIBILITY_RECHECK_INTERVAL_S = 30.0

    # Warn once when the watcher has posted no window heartbeat for this long
    # while still running — surfaces a silent window-ingest stall, which was
    # invisible in the logs during the Cristian Dragota incident (2026-06-25:
    # window/app data went stale on the server for ~15 min while AFK/input kept
    # flowing, with nothing in the agent log to say the window watcher had gone
    # quiet or why).
    _NO_EMIT_WARN_SECONDS = 90.0

    # Bound each Accessibility messaging round-trip so a hung/unresponsive
    # frontmost app can't block the poll thread inside AXUIElementCopyAttribute-
    # Value (which would freeze window tracking with no exception and no log).
    _AX_MESSAGING_TIMEOUT_S = 2.0

    def __init__(self, aw_client, poll_interval: float = 2.0):
        self._aw = aw_client
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._hostname = platform.node()
        self._bucket_id = f"aw-watcher-window_{self._hostname}"
        # Last known Accessibility-permission state; lets the poll loop
        # emit a one-shot log on each transition.
        self._last_accessibility: Optional[bool] = None
        self._last_accessibility_check_ts: float = 0.0
        # One-shot flag: the AX messaging-timeout symbol is either present for
        # the whole process lifetime or never, so warn about it only once.
        self._ax_timeout_warned = False
        # Cache terminal tab title to avoid spawning osascript every poll.
        # _terminal_cache_key is (app_name, ax_title); value is the tab title or None.
        self._terminal_cache_key: Optional[tuple[str, str]] = None
        self._terminal_cache_hit: bool = False
        self._terminal_cache_value: Optional[str] = None
        # Cache browser URL to avoid spawning osascript every poll.
        # Only re-fetch when AXTitle changes (tab switch changes title).
        self._browser_cache_key: Optional[tuple[str, str]] = None
        self._browser_cache_url: Optional[str] = None
        self._browser_cache_incognito: Optional[bool] = None
        # No-emit instrumentation: when post_heartbeat stops being called
        # (frontmost is None, an AX error, or a blocked poll) window/app data
        # silently goes stale on the server. Track the no-emit streak so a real
        # gap leaves a one-shot fingerprint in the log instead of silence.
        self._no_emit_since: Optional[float] = None
        self._no_emit_warned: bool = False

    def start(self) -> bool:
        """Create the AW bucket and start the polling thread.

        Returns True if started successfully, False otherwise.
        """
        # Verify PyObjC Accessibility APIs are available
        try:
            from AppKit import NSWorkspace  # noqa: F401
            from ApplicationServices import (  # noqa: F401
                AXIsProcessTrusted,
                AXUIElementCreateApplication,
            )
            trusted = AXIsProcessTrusted()
            if not trusted:
                logger.warning("Process does NOT have Accessibility permission — window titles will be empty")
        except ImportError:
            logger.error("Cannot start MacOSWindowWatcher: pyobjc-framework-ApplicationServices not installed")
            return False

        # Idempotent: the working-hours capture policy re-asserts the desired state
        # on every 60s tick, so start() is now called repeatedly while running.
        # Without this guard each call span a fresh AX-polling thread — one a
        # minute, forever — each one hammering the Accessibility API and posting
        # heartbeats into the local tracker server.
        if self.is_running:
            return True

        try:
            self._aw.create_bucket(self._bucket_id, "currentwindow", self._hostname)
        except Exception as e:
            logger.warning(f"Failed to create window bucket (will retry on heartbeat): {e}")

        # Reset the stop signal so a restarted thread doesn't exit immediately.
        # stop() sets this and nothing ever cleared it, so after the first capture
        # suppression (22:00) every later start() spawned a thread that fell out of
        # `while not self._stop_event.wait(...)` on its first iteration — window
        # tracking was silently dead for the rest of the process's life, with no
        # bf-window-tracker fallback on macOS to cover it. MacOSInputWatcher has
        # always done this; this watcher never did, because until now it was only
        # ever started once.
        self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._run, daemon=True, name="macos-window-watcher",
        )
        self._thread.start()
        logger.info(f"MacOSWindowWatcher started (bucket={self._bucket_id}, poll={self._poll_interval}s)")
        return True

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def set_poll_interval(self, interval: float) -> None:
        """Adjust poll rate (e.g. slower when AFK)."""
        self._poll_interval = max(0.5, min(interval, 10.0))

    def stop(self) -> None:
        """Signal the thread to stop."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("MacOSWindowWatcher stopped")

    def _get_active_window(self) -> Optional[dict]:
        """Get active window info using native macOS APIs (PyObjC).

        Uses NSWorkspace for the frontmost app and Accessibility APIs
        for the window title — both run in-process so they use this app's
        Accessibility permission, not osascript's.
        """
        from AppKit import NSWorkspace
        from ApplicationServices import (
            AXUIElementCopyAttributeValue,
            AXUIElementCreateApplication,
        )

        workspace = NSWorkspace.sharedWorkspace()
        active_app = workspace.frontmostApplication()
        if not active_app:
            return None

        app_name = active_app.localizedName()
        pid = active_app.processIdentifier()

        # Get window title via Accessibility API
        title = ""
        app_ref = AXUIElementCreateApplication(pid)
        # Bound the AX round-trips: an unresponsive frontmost app can otherwise
        # block AXUIElementCopyAttributeValue and freeze this poll thread with no
        # exception (silent window stall). The symbol is absent on some PyObjC
        # builds, so guard it and fall back to the system default timeout.
        try:
            from ApplicationServices import AXUIElementSetMessagingTimeout

            AXUIElementSetMessagingTimeout(app_ref, self._AX_MESSAGING_TIMEOUT_S)
        except Exception as e:
            # This is the guard that stops an unresponsive frontmost app from
            # blocking the poll thread inside AXUIElementCopyAttributeValue.
            # When it silently fails to install, a hung app freezes window
            # tracking with no exception and no log — exactly the silent stall
            # the fallback is meant to bound. Keep the fallback, log the reason.
            # Poll loop runs every few seconds; log once, not on every poll.
            if not self._ax_timeout_warned:
                self._ax_timeout_warned = True
                logger.warning(
                    "AX messaging timeout unavailable (%s) — window polling can "
                    "stall on an unresponsive app", e,
                )
        err, focused_window = AXUIElementCopyAttributeValue(
            app_ref, "AXFocusedWindow", None,
        )
        if err == 0 and focused_window:
            err2, ax_title = AXUIElementCopyAttributeValue(
                focused_window, "AXTitle", None,
            )
            if err2 == 0 and ax_title:
                title = str(ax_title)

        result = {"app": app_name, "title": title}

        # For terminals, get tab-specific title via AppleScript (cached until AXTitle changes)
        if app_name in _TERMINAL_BUNDLE_IDS:
            cache_key = (app_name, title)
            if cache_key == self._terminal_cache_key and self._terminal_cache_hit:
                if self._terminal_cache_value:
                    result["title"] = self._terminal_cache_value
            else:
                tab_title = self._get_terminal_tab_title(app_name)
                if tab_title:
                    result["title"] = tab_title
                self._terminal_cache_key = cache_key
                self._terminal_cache_hit = True
                self._terminal_cache_value = tab_title

        # For browsers, get URL via AppleScript (cached until AXTitle changes).
        # When Accessibility is missing, title is always empty so the cache
        # key never changes — skip the cache entirely in that case to avoid
        # returning a stale URL for every Chrome poll.
        # Only query browser URL when we have a valid title (Accessibility
        # granted). When title is empty (no permission), querying would spawn
        # a subprocess every 2s poll cycle — thousands per work day for no
        # useful result.
        if app_name in _URL_BROWSERS and title:
            cache_key = (app_name, title)
            if cache_key == self._browser_cache_key:
                url = self._browser_cache_url
                incognito = self._browser_cache_incognito
            else:
                url, incognito = self._get_browser_url(app_name)
                self._browser_cache_key = cache_key
                self._browser_cache_url = url
                self._browser_cache_incognito = incognito
            if url:
                result["url"] = url
            if incognito is not None:
                result["incognito"] = incognito

        return result

    @staticmethod
    def _get_browser_url(app_name: str) -> tuple[Optional[str], Optional[bool]]:
        """Get URL from browser via AppleScript (doesn't need Accessibility).

        Uses bundle IDs (tell application id "...") instead of app names
        to prevent AppleScript injection via malicious process names.
        """
        bundle_id = _BROWSER_BUNDLE_IDS.get(app_name)
        if not bundle_id:
            return None, None

        try:
            if app_name == "Safari":
                script = f'tell application id "{bundle_id}" to return URL of current tab of front window'
                result = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, text=True, timeout=3,
                )
                if result.returncode == 0:
                    return result.stdout.strip(), None
            elif app_name in _CHROMIUM_BROWSERS:
                # Get URL and mode in one call
                script = (
                    f'tell application id "{bundle_id}" to return '
                    f'(URL of active tab of front window) & "\\n" & (mode of front window as text)'
                )
                result = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, text=True, timeout=3,
                )
                if result.returncode == 0:
                    lines = result.stdout.strip().split("\n")
                    url = lines[0] if lines else None
                    incognito = lines[1] == "incognito" if len(lines) > 1 else None
                    return url, incognito
        except subprocess.TimeoutExpired:
            logger.debug(f"AppleScript timed out for {app_name}")
        except Exception as e:
            logger.debug(f"AppleScript failed for {app_name}: {e}")
        return None, None

    @staticmethod
    def _get_terminal_tab_title(app_name: str) -> Optional[str]:
        """Get the active tab/session title from a terminal app via AppleScript."""
        bundle_id = _TERMINAL_BUNDLE_IDS.get(app_name)
        if not bundle_id:
            return None

        try:
            if bundle_id == "com.apple.Terminal":
                script = f'tell application id "{bundle_id}" to return name of selected tab of front window'
            elif bundle_id == "com.googlecode.iterm2":
                script = f'tell application id "{bundle_id}" to return name of current session of current tab of current window'
            else:
                return None

            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:
                title = result.stdout.strip()
                if title:
                    return title
        except subprocess.TimeoutExpired:
            logger.debug(f"AppleScript timed out for {app_name}")
        except Exception as e:
            logger.debug(f"AppleScript failed for {app_name}: {e}")
        return None

    def _run(self) -> None:
        """Poll loop: get active window via PyObjC, post heartbeat."""
        try:
            import objc
            _has_objc = True
        except ImportError:
            _has_objc = False

        while not self._stop_event.wait(self._poll_interval):
            try:
                self._maybe_log_accessibility_transition()
                # Wrap each iteration in an autorelease pool so that
                # ObjC objects created by NSWorkspace / Accessibility
                # APIs are freed at the end of each poll cycle.
                # Without this, every ObjC object leaks (~11 MB/sec).
                if _has_objc:
                    with objc.autorelease_pool():
                        self._poll_once()
                else:
                    self._poll_once()
            except Exception as e:
                logger.warning(f"Window watcher poll error: {e}")
                # A raising poll also produces no heartbeat — count it toward the
                # no-emit streak so a persistent failure escalates to the stall
                # warning instead of just per-poll noise.
                self._note_no_emit(f"poll error: {e}")

    def _maybe_log_accessibility_transition(self) -> None:
        """Re-check AXIsProcessTrusted() occasionally and log when the
        grant flips. Title fetches already work transparently once the
        permission appears — this just makes the change visible in logs."""
        import time
        now = time.monotonic()
        if now - self._last_accessibility_check_ts < self._ACCESSIBILITY_RECHECK_INTERVAL_S:
            return
        self._last_accessibility_check_ts = now
        try:
            from ApplicationServices import AXIsProcessTrusted
            trusted = bool(AXIsProcessTrusted())
        except Exception:
            return
        if self._last_accessibility is None:
            self._last_accessibility = trusted
            return
        if trusted and not self._last_accessibility:
            logger.info(
                "Accessibility permission now granted — window titles will be tracked"
            )
        elif not trusted and self._last_accessibility:
            logger.warning(
                "Accessibility permission revoked — window titles will be empty"
            )
        self._last_accessibility = trusted

    def _note_emit(self) -> None:
        """A window heartbeat was posted — clear any no-emit streak (and log a
        one-line recovery if we'd previously warned about a stall)."""
        if self._no_emit_warned and self._no_emit_since is not None:
            logger.info(
                "Window watcher resumed posting after %.0fs of no window events",
                time.monotonic() - self._no_emit_since,
            )
        self._no_emit_since = None
        self._no_emit_warned = False

    def _note_no_emit(self, reason: str) -> None:
        """A poll produced no window heartbeat. Warn once when the gap crosses
        the threshold so a silent window-ingest stall is diagnosable on the next
        occurrence (Cristian Dragota, 2026-06-25)."""
        now = time.monotonic()
        if self._no_emit_since is None:
            self._no_emit_since = now
        gap = now - self._no_emit_since
        if not self._no_emit_warned and gap >= self._NO_EMIT_WARN_SECONDS:
            self._no_emit_warned = True
            logger.warning(
                "Window watcher has posted no window event for %.0fs (%s) — "
                "window/app data is going stale on the server while the agent runs",
                gap, reason,
            )

    def _poll_once(self) -> None:
        """Single poll iteration: get active window, post heartbeat."""
        data = self._get_active_window()
        if not data or not data.get("app"):
            self._note_no_emit("no frontmost app")
            return

        heartbeat_data = {
            "app": data["app"],
            "title": data.get("title", ""),
        }
        if data.get("url"):
            heartbeat_data["url"] = data["url"]
        if data.get("incognito") is not None:
            heartbeat_data["incognito"] = data["incognito"]

        timestamp = datetime.now(timezone.utc).isoformat()
        self._aw.post_heartbeat(
            self._bucket_id, timestamp, heartbeat_data,
            pulsetime=self._poll_interval + 1.0,
        )
        self._note_emit()
