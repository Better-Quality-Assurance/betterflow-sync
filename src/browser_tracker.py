"""Active browser-tab URL tracking (macOS + Windows).

The bundled ActivityWatch window watcher reports the app + window title but NOT
the URL of the page in the foreground browser tab. Without a URL, every browser
event collapses to a generic "Google Chrome" with no domain, so the backend
can't categorise it (it lands in generic "browsing") and the activity cards have
nothing to show.

This module fills that gap without a browser extension. A daemon thread polls
the frontmost browser's active-tab URL and keeps a short, time-ordered ring
buffer of (timestamp, url) samples; the sync engine looks up the URL active at a
window event's time and attaches it, after which the EXISTING privacy logic
(domain_only_urls / collect_full_urls) applies — so the full URL never leaves
this process unless the server has explicitly enabled full-URL collection.

Per-platform readers:
  - macOS:   AppleScript (`osascript`) asks the frontmost browser for its
             active-tab URL. Needs macOS Automation permission.
  - Windows: UI Automation reads the foreground Chromium browser's omnibox
             value. Needs the `uiautomation` package (bundled in the Windows
             build); the foreground process is checked so Electron apps that
             share Chromium's window class aren't mis-read as browsers.

Opt-in (privacy.track_browser_urls = False by default). Every reader is
fail-closed: a missing permission/library or any error returns None and
tracking is simply a no-op — never a crash.
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
# Windows implementation (UI Automation, no extension)
# ---------------------------------------------------------------------------
#
# The bundled window watcher gives the browser's app + title but not the URL.
# On Windows we read the foreground Chromium browser's address bar via UI
# Automation (UIA) — the same no-extension approach DeskTime used. UIA querying
# also nudges Chromium to expose its accessibility tree, where the omnibox Edit
# control's value holds the current URL.
#
# Two guards keep this honest:
#   1. Process gate — Chromium browsers AND Electron apps (Slack, VS Code,
#      Teams) share the "Chrome_WidgetWin_1" window class, so matching on class
#      would mis-read chat apps as browsers. We resolve the foreground window's
#      process name and only proceed for a known browser executable.
#   2. Value gate — we accept a value only if it normalizes to an http(s) URL,
#      so a focused/empty omnibox, a typed search query, or an internal page
#      (chrome://, edge://) yields nothing rather than garbage.
#
# Everything is fail-closed: missing UIA libs, no permission, or any traversal
# error returns None. Opt-in via privacy.track_browser_urls, same as macOS.
# Foreground process basenames (lowercased) we treat as Chromium browsers.
_WINDOWS_BROWSER_PROCS = {
    "chrome.exe",
    "msedge.exe",
    "brave.exe",
    "vivaldi.exe",
    "opera.exe",
    "arc.exe",
    "chromium.exe",
}

# Bound the UIA tree walk so we never scan a huge page DOM. The omnibox lives in
# the toolbar near the top of the window tree, so a shallow, capped search reaches
# it quickly while staying cheap.
_WIN_UIA_MAX_DEPTH = 8
_WIN_UIA_MAX_NODES = 400


def _normalize_windows_url(raw: Optional[str]) -> Optional[str]:
    """Turn an address-bar value into a real http(s) URL, or None.

    Chromium hides the scheme in the omnibox, so "github.com/x" must become
    "https://github.com/x". Rejects empty values, internal pages, and typed
    search queries (which contain spaces / no dotted host).
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    if s.startswith(("http://", "https://")):
        return s
    low = s.lower()
    if low.startswith(("chrome://", "edge://", "brave://", "about:", "view-source:", "file://", "chrome-extension://")):
        return None
    # A typed search query (spaces) is not a URL.
    if " " in s:
        return None
    # Must look like a host: the part before the first slash needs a dot.
    host = s.split("/", 1)[0]
    if "." not in host or host.startswith(".") or host.endswith("."):
        return None
    return "https://" + s


def _first_url_from_values(values) -> Optional[str]:
    """Return the first value that normalizes to an http(s) URL, or None."""
    for v in values:
        url = _normalize_windows_url(v)
        if url:
            return url
    return None


def _foreground_browser_proc() -> Optional[tuple]:
    """Return (hwnd, proc_basename) for the foreground window when it belongs to
    a known Chromium browser, else None. Pure ctypes — no third-party deps."""
    import ctypes
    import os
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000  # noqa: N806 — Win32 constant name
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        proc = os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(handle)

    if proc not in _WINDOWS_BROWSER_PROCS:
        return None
    return hwnd, proc


def _collect_uia_url_values(root, max_depth: int = _WIN_UIA_MAX_DEPTH, max_nodes: int = _WIN_UIA_MAX_NODES):
    """Bounded breadth-first walk collecting Edit/Document control values.

    Edit controls (the omnibox) come first so a real URL is preferred over a
    page Document value. Bounded by depth and node count so we never traverse a
    full page DOM. Defensive: any per-node failure is skipped.
    """
    edits: list = []
    docs: list = []
    # deque, not a list: this is a BFS, and list.pop(0) is O(n) per dequeue
    # (O(n^2) over the walk). deque.popleft() is O(1).
    queue: Deque[Tuple[object, int]] = deque([(root, 0)])
    seen = 0
    while queue and seen < max_nodes:
        node, depth = queue.popleft()
        seen += 1
        try:
            type_name = node.ControlTypeName
        except Exception:
            type_name = ""
        if type_name in ("EditControl", "DocumentControl"):
            try:
                value = node.GetValuePattern().Value
            except Exception:
                value = None
            if value:
                (edits if type_name == "EditControl" else docs).append(value)
        if depth < max_depth:
            try:
                children = node.GetChildren()
            except Exception:
                children = []
            for child in children:
                queue.append((child, depth + 1))
    return edits + docs


def get_active_browser_url_windows() -> Optional[str]:
    """Read the foreground Chromium browser's active-tab URL via UIA, or None.

    Fail-closed: returns None when the foreground app isn't a known browser, the
    UIA library isn't bundled, accessibility is unavailable, or no control yields
    a value that normalizes to an http(s) URL.
    """
    found = _foreground_browser_proc()
    if found is None:
        return None
    hwnd, _proc = found
    try:
        import uiautomation as auto  # lazy: absent in builds without the dep
    except Exception:
        return None
    try:
        control = auto.ControlFromHandle(hwnd)
        if control is None:
            return None
        values = _collect_uia_url_values(control)
        return _first_url_from_values(values)
    except Exception as e:  # never raise into the poll thread
        logger.debug("windows browser url read failed: %s", e)
        return None


def _start_windows_tracker(
    poll_interval: float,
    retention_seconds: float,
    match_tolerance_seconds: float,
) -> BrowserURLTracker:
    """Start a Windows tracker polling the active-tab URL via UI Automation."""
    tracker = BrowserURLTracker(retention_seconds, match_tolerance_seconds)
    stop_event = threading.Event()

    def _run() -> None:
        while not stop_event.wait(poll_interval):
            try:
                url = get_active_browser_url_windows()
                if url:
                    tracker.record(time.time(), url)
            except Exception as e:  # never let the poll thread die
                logger.debug("browser url poll failed: %s", e)

    thread = threading.Thread(target=_run, daemon=True, name="browser-url-tracker-windows")
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
        if _system == "Windows":
            return _start_windows_tracker(poll_interval, retention_seconds, match_tolerance_seconds)
    except Exception as e:
        logger.debug("Failed to start browser tracker: %s", e)
    return BrowserURLTracker(retention_seconds, match_tolerance_seconds)
