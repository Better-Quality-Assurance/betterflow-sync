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
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Browser apps that support URL retrieval via AppleScript
_CHROMIUM_BROWSERS = frozenset({
    "Google Chrome", "Google Chrome Canary", "Chromium", "Brave Browser",
})
_URL_BROWSERS = _CHROMIUM_BROWSERS | {"Safari"}


class MacOSWindowWatcher:
    """Daemon thread that polls the active window via PyObjC and posts heartbeats to AW."""

    def __init__(self, aw_client, poll_interval: float = 1.0):
        self._aw = aw_client
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._hostname = platform.node()
        self._bucket_id = f"aw-watcher-window_{self._hostname}"

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

        try:
            self._aw.create_bucket(self._bucket_id, "currentwindow", self._hostname)
        except Exception as e:
            logger.warning(f"Failed to create window bucket (will retry on heartbeat): {e}")

        self._thread = threading.Thread(
            target=self._run, daemon=True, name="macos-window-watcher",
        )
        self._thread.start()
        logger.info(f"MacOSWindowWatcher started (bucket={self._bucket_id}, poll={self._poll_interval}s)")
        return True

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

        # For browsers, get URL via AppleScript (doesn't need Accessibility)
        if app_name in _URL_BROWSERS:
            url, incognito = self._get_browser_url(app_name)
            if url:
                result["url"] = url
            if incognito is not None:
                result["incognito"] = incognito

        return result

    @staticmethod
    def _get_browser_url(app_name: str) -> tuple[Optional[str], Optional[bool]]:
        """Get URL from browser via AppleScript (doesn't need Accessibility)."""
        try:
            if app_name == "Safari":
                script = 'tell application "Safari" to return URL of current tab of front window'
                result = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, text=True, timeout=3,
                )
                if result.returncode == 0:
                    return result.stdout.strip(), None
            elif app_name in _CHROMIUM_BROWSERS:
                # Get URL and mode in one call
                script = f'tell application "{app_name}" to return (URL of active tab of front window) & "\\n" & (mode of front window as text)'
                result = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True, text=True, timeout=3,
                )
                if result.returncode == 0:
                    lines = result.stdout.strip().split("\n")
                    url = lines[0] if lines else None
                    incognito = lines[1] == "incognito" if len(lines) > 1 else None
                    return url, incognito
        except (subprocess.TimeoutExpired, Exception):
            pass
        return None, None

    def _run(self) -> None:
        """Poll loop: get active window via PyObjC, post heartbeat."""
        while not self._stop_event.wait(self._poll_interval):
            try:
                data = self._get_active_window()
                if not data or not data.get("app"):
                    continue

                # Build heartbeat data matching AW window watcher format
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

            except Exception as e:
                logger.warning(f"Window watcher poll error: {e}")
