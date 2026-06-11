"""Active browser-tab URL tracking (macOS).

The bundled ActivityWatch window watcher reports the app + window title but NOT
the URL of the page in the foreground browser tab. Without a URL, every browser
event collapses to a generic "Google Chrome" with no domain, so the backend
can't categorise it (it lands in generic "browsing") and the activity cards have
nothing to show.

This module fills that gap on macOS without requiring a browser extension: a
daemon thread polls the frontmost browser's active-tab URL via AppleScript
(`osascript`) and keeps a short, time-ordered ring buffer of (timestamp, url)
samples. The sync engine looks up the URL active at a window event's time and
attaches it, after which the EXISTING privacy logic (domain_only_urls /
collect_full_urls) applies — so the full URL never leaves this process unless
the server has explicitly enabled full-URL collection.

Opt-in (privacy.track_browser_urls = False by default). Querying a browser via
AppleScript needs macOS Automation permission; if it's not granted the query
fails closed (returns None) and tracking is simply a no-op — never a crash.
"""

import logging
import platform
import subprocess
import threading
import time
from collections import deque
from typing import Deque, Optional, Tuple

logger = logging.getLogger(__name__)

_system = platform.system()

# How often to sample the active tab, and how long to keep samples. Sync runs
# on a ~60s loop and only processes recent events, so a few minutes of history
# is plenty; the cap also bounds memory.
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_RETENTION_SECONDS = 600.0
# A window event is matched to the most recent URL sample at-or-before its end,
# but only if that sample is within this tolerance — otherwise we have no
# trustworthy URL for that moment and attach nothing.
DEFAULT_MATCH_TOLERANCE_SECONDS = 8.0

# Chromium-family browsers expose `URL of active tab of front window`; Safari
# uses `URL of front document`. Keys are the macOS application names as seen by
# System Events (also the window-watcher app name), matched case-insensitively.
_CHROMIUM_BROWSERS = {
    "google chrome",
    "google chrome canary",
    "brave browser",
    "microsoft edge",
    "chromium",
    "vivaldi",
    "opera",
    "arc",
}
_SAFARI_BROWSERS = {"safari", "safari technology preview"}

_BROWSER_APPS = _CHROMIUM_BROWSERS | _SAFARI_BROWSERS


def is_browser_app(app: Optional[str]) -> bool:
    """True if ``app`` is a browser whose active-tab URL we know how to read."""
    return bool(app) and app.strip().lower() in _BROWSER_APPS


class BrowserURLTracker:
    """Null tracker — returns no URL. Base class / unsupported-platform fallback.

    The buffer logic lives here so it is unit-testable without a real browser:
    feed samples with ``record()`` and read them back with ``url_at()``.
    """

    def __init__(
        self,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        match_tolerance_seconds: float = DEFAULT_MATCH_TOLERANCE_SECONDS,
    ) -> None:
        self._retention = retention_seconds
        self._tolerance = match_tolerance_seconds
        # (epoch_seconds, url), kept in ascending time order.
        self._samples: Deque[Tuple[float, str]] = deque()
        self._lock = threading.Lock()

    def record(self, ts_epoch: float, url: str) -> None:
        """Append a sample and evict anything older than the retention window."""
        if not url:
            return
        with self._lock:
            self._samples.append((ts_epoch, url))
            cutoff = ts_epoch - self._retention
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()

    def url_at(self, ts_epoch: float) -> Optional[str]:
        """Return the URL active at ``ts_epoch``.

        Picks the most recent sample at-or-before the timestamp, but only if it
        is within the match tolerance (a stale sample from minutes ago is not a
        trustworthy answer). Returns None when there is no close-enough sample.
        """
        best: Optional[Tuple[float, str]] = None
        with self._lock:
            for sample_ts, url in self._samples:
                if sample_ts <= ts_epoch:
                    best = (sample_ts, url)
                else:
                    break
        if best is None:
            return None
        if ts_epoch - best[0] > self._tolerance:
            return None
        return best[1]

    def stop(self) -> None:
        pass


# ---------------------------------------------------------------------------
# macOS implementation
# ---------------------------------------------------------------------------

# Resolve the frontmost app once, then read the URL from the matching browser.
# `System Events` gives the frontmost process name; we only script a browser we
# recognise so we never send Apple Events to unrelated apps.
_FRONTMOST_APP_SCRIPT = (
    'tell application "System Events" to get name of first application '
    "process whose frontmost is true"
)


def _osascript(script: str, timeout: float = 2.0) -> Optional[str]:
    """Run an AppleScript and return its trimmed stdout, or None on any failure.

    Fails closed: a missing Automation permission, a browser with no open
    window, or a timeout all yield None rather than raising.
    """
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug("osascript failed: %s", e)
        return None
    if proc.returncode != 0:
        # e.g. "not allowed assistive access" / "not authorised to send Apple
        # events" when Automation permission is missing.
        logger.debug("osascript non-zero (%s): %s", proc.returncode, proc.stderr.strip())
        return None
    out = proc.stdout.strip()
    return out or None


def _active_url_script_for(app_name: str) -> Optional[str]:
    """AppleScript that returns the active-tab URL for a supported browser."""
    key = app_name.strip().lower()
    if key in _CHROMIUM_BROWSERS:
        return f'tell application "{app_name}" to get URL of active tab of front window'
    if key in _SAFARI_BROWSERS:
        return f'tell application "{app_name}" to get URL of front document'
    return None


def get_active_browser_url() -> Optional[str]:
    """Return the frontmost browser's active-tab URL, or None.

    None when the frontmost app isn't a supported browser, has no window, or
    when Automation permission is unavailable.
    """
    app = _osascript(_FRONTMOST_APP_SCRIPT)
    if not app or not is_browser_app(app):
        return None
    script = _active_url_script_for(app)
    if script is None:
        return None
    url = _osascript(script)
    # Browsers report internal pages (new tab, settings) as empty or non-http;
    # only keep real web URLs.
    if url and (url.startswith("http://") or url.startswith("https://")):
        return url
    return None


def _start_macos_tracker(
    poll_interval: float,
    retention_seconds: float,
    match_tolerance_seconds: float,
) -> BrowserURLTracker:
    """Start a macOS tracker polling the active-tab URL via AppleScript."""
    tracker = BrowserURLTracker(retention_seconds, match_tolerance_seconds)
    stop_event = threading.Event()

    def _run() -> None:
        while not stop_event.wait(poll_interval):
            try:
                url = get_active_browser_url()
                if url:
                    tracker.record(time.time(), url)
            except Exception as e:  # never let the poll thread die
                logger.debug("browser url poll failed: %s", e)

    thread = threading.Thread(target=_run, daemon=True, name="browser-url-tracker-macos")
    thread.start()
    tracker.stop = stop_event.set  # type: ignore[assignment]
    return tracker


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def start_browser_tracker(
    enabled: bool = True,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    retention_seconds: float = DEFAULT_RETENTION_SECONDS,
    match_tolerance_seconds: float = DEFAULT_MATCH_TOLERANCE_SECONDS,
) -> BrowserURLTracker:
    """Create and start a browser URL tracker.

    Returns a null tracker (no URLs) when disabled, on unsupported platforms,
    or if initialization fails — callers can always use the result safely.
    """
    if not enabled:
        return BrowserURLTracker(retention_seconds, match_tolerance_seconds)
    try:
        if _system == "Darwin":
            return _start_macos_tracker(poll_interval, retention_seconds, match_tolerance_seconds)
    except Exception as e:
        logger.debug("Failed to start browser tracker: %s", e)
    return BrowserURLTracker(retention_seconds, match_tolerance_seconds)
