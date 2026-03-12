"""System tray icon and menu."""

import logging
import math
import os
import platform
import subprocess
import sys
import threading
import time
import webbrowser
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional, TypedDict

try:
    from .._build_info import APP_VERSION as _APP_VERSION, BUILD_DATE  # module execution
except ImportError:
    try:
        import _build_info as _bi  # PyInstaller bundle (src/ is root, no parent package)
        _APP_VERSION = _bi.APP_VERSION
        BUILD_DATE = _bi.BUILD_DATE
    except ImportError:
        try:
            from .. import __version__ as _APP_VERSION
        except ImportError:
            _APP_VERSION = "unknown"
        BUILD_DATE = "dev"

from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    from ..config import Config


def _prevent_termination() -> None:
    """Prevent macOS from silently killing the tray app.

    Without these calls, macOS may terminate a background-only app after
    a few minutes via Sudden Termination or Automatic Termination.

    Note: Dock hiding is handled by LSUIElement=True in the Info.plist
    (set in build.spec).  We intentionally do NOT call
    setActivationPolicy_(1) here because it conflicts with pystray's
    NSApplication setup and can prevent the status bar item from appearing.
    """
    if platform.system() != "Darwin":
        return
    try:
        import Foundation

        proc_info = Foundation.NSProcessInfo.processInfo()
        proc_info.setAutomaticTerminationSupportEnabled_(False)
        proc_info.disableSuddenTermination()
    except Exception:
        pass


__all__ = ["TrayIcon", "TrayState", "TrayModel", "STATE_COLORS", "create_icon_image", "ProjectDict"]


class ProjectDict(TypedDict):
    """Typed project dictionary from the API."""

    id: int
    name: str


class TrayModel:
    """Observable state model for the tray icon.

    Holds all display state (user info, stats, preferences, projects).
    TrayIcon reads from this for rendering; external code mutates it
    through public methods on TrayIcon which delegate here.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.state: TrayState = TrayState.STARTING
        self.paused: bool = False
        self.private_mode: bool = False
        self.status_text: str = "Starting..."
        self.hours_today: str = "0h 0m"
        self.hours_this_week: str = "---"
        self.hours_this_month: str = "---"
        self.daily_avg_this_week: str = "---"
        self.last_sync: str = "Never"
        self.queue_size: int = 0
        self.user_email: Optional[str] = None
        self.user_name: Optional[str] = None
        self.user_role: Optional[str] = None

        # Projects
        self.projects: list[ProjectDict] = []
        self.current_project: Optional[ProjectDict] = None
        self.project_started_at: Optional[float] = None  # time.monotonic() when project was selected

        # Preferences
        self.sync_interval: int = 30
        self.hash_titles: bool = False
        self.domain_only_urls: bool = False
        self.debug_mode: bool = False
        self.auto_start: bool = False
        self.update_channel: str = "stable"
        self.update_version: Optional[str] = None
        self.update_url: Optional[str] = None
        self.update_asset_url: Optional[str] = None  # Direct ZIP download for self-update
        self.update_in_progress: bool = False
        self.update_status: str = ""
        self.config_file_path: Optional[str] = None
        self.dashboard_url: str = ""  # Set by set_config() from api_url
        self.company_name: Optional[str] = None

        # Reminder preferences
        self.break_reminders_enabled: bool = True
        self.break_interval_hours: int = 2
        self.private_reminders_enabled: bool = True
        self.private_interval_minutes: int = 20

        # Categorization
        self.auto_categorize: bool = True

        # Display tracking
        self.track_display_info: bool = False

        # Break state
        self.on_break: bool = False
        self.break_minutes_left: int = 0

        # Permissions
        self.needs_permissions: bool = False


try:
    import pystray
    from pystray import MenuItem as Item
except ImportError:
    pystray = None
    Item = None

logger = logging.getLogger(__name__)


class TrayState(Enum):
    """Tray icon states."""

    SYNCING = "syncing"  # Green - connected and active
    QUEUED = "queued"  # Yellow - offline, events queued
    QUEUE_WARNING = "queue_warning"  # Orange - queue approaching capacity
    ERROR = "error"  # Red - auth failed or AW not running
    PAUSED = "paused"  # Gray - user paused tracking
    PRIVATE = "private"  # Dark gray - private time, nothing recorded
    ON_BREAK = "on_break"  # Amber - auto-break active
    NEEDS_PERMISSIONS = "needs_permissions"  # Amber - macOS permissions missing
    STARTING = "starting"  # Blue - starting up
    WAITING_AUTH = "waiting_auth"  # Amber - waiting for browser login


# Colors for each state — BetterFlow brand palette
STATE_COLORS = {
    TrayState.SYNCING: "#7D69B8",  # BetterFlow purple - active
    TrayState.QUEUED: "#eab308",  # Yellow - offline
    TrayState.QUEUE_WARNING: "#f97316",  # Orange - queue nearing capacity
    TrayState.ERROR: "#c96660",  # BetterFlow error red
    TrayState.PAUSED: "#9ca3af",  # Gray
    TrayState.PRIVATE: "#6b7280",  # Dark gray - private time
    TrayState.ON_BREAK: "#B57EF5",  # BetterFlow light purple - break
    TrayState.NEEDS_PERMISSIONS: "#f59e0b",  # Amber - permissions missing
    TrayState.STARTING: "#614D87",  # BetterFlow dark purple
    TrayState.WAITING_AUTH: "#f59e0b",  # Amber
}


def _resources_dir() -> Path:
    """Return the resources directory (works for dev and PyInstaller)."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "resources"
    return Path(__file__).resolve().parent.parent.parent / "resources"


# Cache the logo template to avoid re-reading from disk on every state change.
_logo_template: Optional[Image.Image] = None


def _get_logo_template() -> Optional[Image.Image]:
    """Load and cache the B logo as an alpha mask."""
    global _logo_template
    if _logo_template is not None:
        return _logo_template
    logo_path = _resources_dir() / "logo.png"
    if not logo_path.exists():
        return None
    try:
        img = Image.open(logo_path).convert("RGBA")
        _logo_template = img
        return _logo_template
    except Exception:
        return None


def create_icon_image(color: str, size: int = 64) -> Image.Image:
    """Create a clock icon with the given state color.

    Draws a round clock face with hour marks, hour/minute hands showing
    the current local time, and a "B" glyph in the center.

    Args:
        color: Hex color code for the clock color
        size: Icon size in pixels

    Returns:
        PIL Image
    """
    from datetime import datetime

    now = datetime.now()
    hour = now.hour % 12
    minute = now.minute

    # Use 4x supersampling for smooth anti-aliased lines
    ss = 4
    big = size * ss
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    cx, cy = big // 2, big // 2
    radius = int(big * 0.42)
    ring_width = max(2, big // 16)

    # Clock face ring
    draw.ellipse(
        [cx - radius, cy - radius, cx + radius, cy + radius],
        outline=color,
        width=ring_width,
    )

    # Hour tick marks
    tick_outer = radius - ring_width // 2 - max(1, big // 64)
    tick_inner_long = int(tick_outer * 0.72)
    tick_inner_short = int(tick_outer * 0.84)
    for i in range(12):
        angle = math.radians(i * 30 - 90)
        inner = tick_inner_long if i % 3 == 0 else tick_inner_short
        x1 = cx + int(inner * math.cos(angle))
        y1 = cy + int(inner * math.sin(angle))
        x2 = cx + int(tick_outer * math.cos(angle))
        y2 = cy + int(tick_outer * math.sin(angle))
        w = max(2, big // 32) if i % 3 == 0 else max(1, big // 48)
        draw.line([x1, y1, x2, y2], fill=color, width=w)

    # Minute hand
    minute_angle = math.radians(minute * 6 - 90)
    minute_len = int(tick_outer * 0.82)
    draw.line(
        [cx, cy,
         cx + int(minute_len * math.cos(minute_angle)),
         cy + int(minute_len * math.sin(minute_angle))],
        fill=color,
        width=max(2, big // 32),
    )

    # Hour hand (shorter, thicker)
    hour_angle = math.radians((hour * 30 + minute * 0.5) - 90)
    hour_len = int(tick_outer * 0.55)
    draw.line(
        [cx, cy,
         cx + int(hour_len * math.cos(hour_angle)),
         cy + int(hour_len * math.sin(hour_angle))],
        fill=color,
        width=max(3, big // 24),
    )

    # "B" letter in the center
    font_size = int(radius * 1.1)
    try:
        if platform.system() == "Darwin":
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        elif platform.system() == "Windows":
            font = ImageFont.truetype("segoeui.ttf", font_size)
        else:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except (OSError, IOError):
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), "B", font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = cx - tw // 2 - bbox[0]
    ty = cy - th // 2 - bbox[1]
    draw.text((tx, ty), "B", fill=color, font=font)

    # Downsample for smooth result
    image = image.resize((size, size), Image.LANCZOS)
    return image


class TrayIcon:
    """System tray icon with status indicator."""

    def __init__(
        self,
        on_login: Optional[Callable[[], None]] = None,
        on_pause: Optional[Callable[[], None]] = None,
        on_resume: Optional[Callable[[], None]] = None,
        on_preferences: Optional[Callable[[str, object], None]] = None,
        on_logout: Optional[Callable[[], None]] = None,
        on_quit: Optional[Callable[[], None]] = None,
        on_project_change: Optional[Callable[[Optional[ProjectDict]], None]] = None,
        on_private_toggle: Optional[Callable[[bool], None]] = None,
        on_sync_now: Optional[Callable[[], None]] = None,
        on_export_logs: Optional[Callable[[], None]] = None,
        on_open_permissions: Optional[Callable[[], None]] = None,
        on_start_break: Optional[Callable[[], None]] = None,
        on_end_break: Optional[Callable[[], None]] = None,
        on_cancel_login: Optional[Callable[[], None]] = None,
        on_install_update: Optional[Callable[[str], None]] = None,
    ):
        """Initialize tray icon.

        Args:
            on_login: Callback when login is clicked (opens browser)
            on_pause: Callback when pause is clicked
            on_resume: Callback when resume is clicked
            on_preferences: Callback when preferences is clicked
            on_logout: Callback when sign out is clicked
            on_quit: Callback when quit is clicked
            on_project_change: Callback when project is switched (receives project dict or None)
            on_private_toggle: Callback when private time is toggled (receives bool)
            on_sync_now: Callback to trigger an immediate sync
            on_export_logs: Callback to export logs to a zip file
            on_open_permissions: Callback to open system permission settings
            on_start_break: Callback when user starts a manual break
            on_end_break: Callback when user ends break early
            on_cancel_login: Callback when user cancels in-progress login
            on_install_update: Callback when user clicks Install & Restart (receives asset URL)
        """
        if pystray is None:
            raise ImportError("pystray is required for system tray support")

        self._on_login = on_login
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._on_preferences = on_preferences  # callback(key, value) for setting changes
        self._on_logout = on_logout
        self._on_quit = on_quit
        self._on_project_change = on_project_change
        self._on_private_toggle = on_private_toggle
        self._on_sync_now = on_sync_now
        self._on_export_logs = on_export_logs
        self._on_open_permissions = on_open_permissions
        self._on_start_break = on_start_break
        self._on_end_break = on_end_break
        self._on_cancel_login = on_cancel_login
        self._on_install_update = on_install_update

        self.model = TrayModel()

        self._icon: Optional[pystray.Icon] = None
        self._thread: Optional[threading.Thread] = None

        # Menu debounce: rebuild at most once per second
        self._menu_debounce_interval = 1.0
        self._menu_last_rebuild: float = 0.0
        self._menu_pending: bool = False
        self._menu_lock = threading.Lock()

        # Sync Now cooldown (5 seconds)
        self._sync_now_cooldown = 5.0
        self._sync_now_last: float = 0.0
        self._sync_now_lock = threading.Lock()

    def _snapshot_model(self) -> dict:
        """Take a consistent snapshot of all model fields under lock."""
        with self.model.lock:
            return {
                "state": self.model.state,
                "paused": self.model.paused,
                "private_mode": self.model.private_mode,
                "status_text": self.model.status_text,
                "hours_today": self.model.hours_today,
                "hours_this_week": self.model.hours_this_week,
                "hours_this_month": self.model.hours_this_month,
                "daily_avg_this_week": self.model.daily_avg_this_week,
                "last_sync": self.model.last_sync,
                "queue_size": self.model.queue_size,
                "user_email": self.model.user_email,
                "user_name": self.model.user_name,
                "user_role": self.model.user_role,
                "projects": list(self.model.projects),
                "current_project": self.model.current_project,
                "project_started_at": self.model.project_started_at,
                "on_break": self.model.on_break,
                "break_minutes_left": self.model.break_minutes_left,
                "needs_permissions": self.model.needs_permissions,
                "sync_interval": self.model.sync_interval,
                "hash_titles": self.model.hash_titles,
                "domain_only_urls": self.model.domain_only_urls,
                "debug_mode": self.model.debug_mode,
                "auto_start": self.model.auto_start,
                "update_channel": self.model.update_channel,
                "update_version": self.model.update_version,
                "update_url": self.model.update_url,
                "update_asset_url": self.model.update_asset_url,
                "update_in_progress": self.model.update_in_progress,
                "update_status": self.model.update_status,
                "dashboard_url": self.model.dashboard_url,
                "company_name": self.model.company_name,
                "config_file_path": self.model.config_file_path,
                "break_reminders_enabled": self.model.break_reminders_enabled,
                "break_interval_hours": self.model.break_interval_hours,
                "private_reminders_enabled": self.model.private_reminders_enabled,
                "private_interval_minutes": self.model.private_interval_minutes,
                "auto_categorize": self.model.auto_categorize,
                "track_display_info": self.model.track_display_info,
            }

    def _create_menu(self) -> pystray.Menu:
        """Create the tray menu.

        Takes a snapshot of all model fields under lock, then builds the
        menu from the snapshot without holding the lock.
        """
        s = self._snapshot_model()
        items = []
        logged_in = s["user_email"] is not None

        # ── User identity & status ──────────────────────────
        if s["user_email"]:
            if s["user_name"] and s["user_name"] != s["user_email"]:
                label = f"{s['user_name']} ({s['user_email']})"
            else:
                label = s["user_email"]
            items.append(Item(label, None, enabled=False))

        items.append(Item(f"App status: {self._get_status_text(s)}", None, enabled=False))
        if s["needs_permissions"]:
            items.append(Item("\u26a0 Enable Permissions...", self._handle_open_permissions))
        items.append(Item(f"Hours today: {s['hours_today']}", None, enabled=False))
        items.append(Item("Trends", pystray.Menu(
            Item(f"Hours this week: {s['hours_this_week']}", None, enabled=False),
            Item(f"Hours this month: {s['hours_this_month']}", None, enabled=False),
            Item(f"Daily avg this week: {s['daily_avg_this_week']}", None, enabled=False),
        ), enabled=logged_in))

        # ── Dashboard link ─────────────────────────────────
        items.append(Item("Show My Hours", self._handle_show_dashboard, enabled=logged_in))

        items.append(Item("─" * 20, None, enabled=False))

        # ── Assigned Projects ───────────────────────────────
        if s["projects"]:
            items.append(Item("Assigned Projects", None, enabled=False))
            for proj in s["projects"]:
                is_current = (
                    s["current_project"] is not None
                    and s["current_project"]["id"] == proj["id"]
                )
                items.append(Item(
                    f"  {proj['name']}",
                    self._make_project_handler(proj),
                    checked=lambda item, p=proj: self._is_current_project(p),
                    enabled=logged_in and not is_current,
                ))
            items.append(Item(
                f"  Stop ({s['hours_today']})",
                self._handle_stop_project,
                enabled=logged_in and s["current_project"] is not None,
            ))
            items.append(Item("─" * 20, None, enabled=False))

        # ── Break toggle ───────────────────────────────────
        if s["on_break"]:
            items.append(Item("End Break", self._handle_end_break, enabled=logged_in))
        else:
            items.append(Item("Start Break", self._handle_start_break, enabled=logged_in and not s["paused"]))

        # ── Private Time toggle (replaces both pause and old private time) ──
        if s["private_mode"] or (s["paused"] and not s["on_break"]):
            items.append(Item("End Private Time", self._handle_private_toggle, enabled=logged_in))
        elif not s["on_break"]:
            items.append(Item("Private Time", self._handle_private_toggle, enabled=logged_in))

        # ── Private Time Reminder submenu ───────────────────
        items.append(Item("Private Time Reminder", pystray.Menu(
            Item(
                "Disabled",
                self._make_private_reminder_handler(enabled=False),
                checked=lambda item: self._is_private_reminder(False),
            ),
            Item(
                "Every 15 Minutes",
                self._make_private_reminder_handler(enabled=True, minutes=15),
                checked=lambda item: self._is_private_reminder(True, 15),
            ),
            Item(
                "Every 35 Minutes",
                self._make_private_reminder_handler(enabled=True, minutes=35),
                checked=lambda item: self._is_private_reminder(True, 35),
            ),
            Item(
                "Every 45 Minutes",
                self._make_private_reminder_handler(enabled=True, minutes=45),
                checked=lambda item: self._is_private_reminder(True, 45),
            ),
        ), enabled=logged_in))

        # ── Break Time Reminder submenu ─────────────────────
        items.append(Item("Break Time Reminder", pystray.Menu(
            Item(
                "Disabled",
                self._make_break_reminder_handler(enabled=False),
                checked=lambda item: self._is_break_reminder(False),
            ),
            Item(
                "After 1 Hour",
                self._make_break_reminder_handler(enabled=True, hours=1),
                checked=lambda item: self._is_break_reminder(True, 1),
            ),
            Item(
                "After 2 Hours",
                self._make_break_reminder_handler(enabled=True, hours=2),
                checked=lambda item: self._is_break_reminder(True, 2),
            ),
            Item(
                "After 3 Hours",
                self._make_break_reminder_handler(enabled=True, hours=3),
                checked=lambda item: self._is_break_reminder(True, 3),
            ),
            Item(
                "After 4 Hours",
                self._make_break_reminder_handler(enabled=True, hours=4),
                checked=lambda item: self._is_break_reminder(True, 4),
            ),
        ), enabled=logged_in))

        items.append(Item("─" * 20, None, enabled=False))

        # ── Diagnostics submenu ─────────────────────────────
        diag_items = [
            Item(f"ActivityWatch: {'Running' if self._check_aw_status(s) else 'Not running'}", None, enabled=False),
            Item(f"API: {'Connected' if self._check_api_status(s) else 'Unreachable'}", None, enabled=False),
            Item(f"Queue: {s['queue_size']} events", None, enabled=False),
            Item(f"Last sync: {s['last_sync']}", None, enabled=False),
        ]
        items.append(Item("Diagnostics", pystray.Menu(*diag_items), enabled=logged_in))
        items.append(Item("Sync Now", self._handle_sync_now, enabled=logged_in))

        # ── Preferences submenu ─────────────────────────────
        items.append(Item("Preferences", pystray.Menu(
            Item(
                "Launch at Login",
                self._make_toggle_handler("auto_start", "auto_start"),
                checked=lambda item: self._is_pref("auto_start"),
            ),
            Item(
                "Debug Mode",
                self._make_toggle_handler("debug_mode", "debug_mode"),
                checked=lambda item: self._is_pref("debug_mode"),
            ),
            Item("Export Logs", self._handle_export_logs),
            Item("Open Config File", self._handle_open_config),
        ), enabled=logged_in))

        items.append(Item("─" * 20, None, enabled=False))

        # ── Account ─────────────────────────────────────────
        if s["user_email"]:
            items.append(Item("Log Out", self._handle_logout))
        elif s["state"] == TrayState.WAITING_AUTH:
            items.append(Item("Cancel Login", self._handle_cancel_login))
            items.append(Item("Retry Login", self._handle_login))
        else:
            items.append(Item("Login", self._handle_login))

        items.append(Item("─" * 20, None, enabled=False))

        # ── Update available ──────────────────────────────────
        if s["update_in_progress"]:
            status = s["update_status"] or "Updating..."
            items.append(Item(status, None, enabled=False))
        elif s["update_version"]:
            if s["update_asset_url"]:
                items.append(Item(
                    f"Install v{s['update_version']} & Restart",
                    self._handle_install_update,
                ))
            else:
                items.append(Item(
                    f"Update available (v{s['update_version']})",
                    self._handle_open_update,
                ))

        # ── Version info ───────────────────────────────────────
        items.append(Item(f"v{_APP_VERSION} ({BUILD_DATE})", None, enabled=False))

        if s["user_role"] in ("admin", "super-admin"):
            items.append(Item("Quit", self._handle_quit))

        return pystray.Menu(*items)

    def _get_status_text(self, s: Optional[dict] = None) -> str:
        """Get short status text for the menu.

        Args:
            s: Optional model snapshot dict. If None, reads from model directly.
        """
        on_break = s["on_break"] if s else self.model.on_break
        private_mode = s["private_mode"] if s else self.model.private_mode
        state = s["state"] if s else self.model.state
        break_minutes_left = s["break_minutes_left"] if s else self.model.break_minutes_left

        if on_break:
            return f"On Break ({break_minutes_left}m left)"
        elif private_mode:
            return "Private Time"
        elif state == TrayState.SYNCING:
            return "Active"
        elif state == TrayState.QUEUED:
            return "Offline"
        elif state == TrayState.QUEUE_WARNING:
            return "Offline (queue full)"
        elif state == TrayState.ERROR:
            return "Error"
        elif state == TrayState.PAUSED:
            return "Paused"
        elif state == TrayState.NEEDS_PERMISSIONS:
            return "Permissions Required"
        elif state == TrayState.WAITING_AUTH:
            return "Waiting for login..."
        else:
            return "Starting..."

    # -- Lock-protected checkers for `checked` lambdas ----------------------

    def _is_current_project(self, proj: ProjectDict) -> bool:
        with self.model.lock:
            cp = self.model.current_project
            return cp is not None and cp["id"] == proj["id"]

    def _is_private_reminder(self, enabled: bool, minutes: int = 0) -> bool:
        with self.model.lock:
            if not enabled:
                return not self.model.private_reminders_enabled
            return self.model.private_reminders_enabled and self.model.private_interval_minutes == minutes

    def _is_break_reminder(self, enabled: bool, hours: int = 0) -> bool:
        with self.model.lock:
            if not enabled:
                return not self.model.break_reminders_enabled
            return self.model.break_reminders_enabled and self.model.break_interval_hours == hours

    def _is_pref(self, attr: str) -> bool:
        with self.model.lock:
            return getattr(self.model, attr)

    # -- Diagnostics helpers --------------------------------------------------

    def _check_aw_status(self, s: Optional[dict] = None) -> bool:
        """Check if ActivityWatch is reachable (best-effort, non-blocking)."""
        state = s["state"] if s else self.model.state
        return state not in (TrayState.ERROR, TrayState.STARTING, TrayState.WAITING_AUTH)

    def _check_api_status(self, s: Optional[dict] = None) -> bool:
        """Check if BetterFlow API is reachable (best-effort, non-blocking)."""
        state = s["state"] if s else self.model.state
        return state not in (TrayState.QUEUED, TrayState.QUEUE_WARNING, TrayState.ERROR)

    # -- Menu action handlers ------------------------------------------------

    def _handle_login(self, icon, item) -> None:
        """Handle login menu click."""
        if self._on_login:
            self._on_login()

    def _handle_cancel_login(self, icon, item) -> None:
        """Handle cancel login menu click."""
        if self._on_cancel_login:
            self._on_cancel_login()

    def _handle_open_permissions(self, icon, item) -> None:
        """Handle open permissions menu click."""
        if self._on_open_permissions:
            self._on_open_permissions()

    def _handle_start_break(self, icon, item) -> None:
        """Handle start break menu click."""
        if self._on_start_break:
            self._on_start_break()

    def _handle_end_break(self, icon, item) -> None:
        """Handle end break menu click."""
        if self._on_end_break:
            self._on_end_break()

    def _handle_pause(self, icon, item) -> None:
        """Handle pause menu click."""
        if self._on_pause:
            self._on_pause()

    def _handle_resume(self, icon, item) -> None:
        """Handle resume menu click."""
        if self._on_resume:
            self._on_resume()

    def _handle_private_toggle(self, icon, item) -> None:
        """Handle private time toggle."""
        with self.model.lock:
            # Can't enable private time while paused
            if not self.model.private_mode and self.model.paused:
                return
            self.model.private_mode = not self.model.private_mode
            new_mode = self.model.private_mode
            on_break = self.model.on_break
            paused = self.model.paused
        if new_mode:
            self.set_state(TrayState.PRIVATE)
        if self._on_private_toggle:
            self._on_private_toggle(new_mode)
        if not new_mode:
            if on_break:
                self.set_state(TrayState.ON_BREAK)
            elif paused:
                self.set_state(TrayState.PAUSED)
            else:
                self.set_state(TrayState.SYNCING)
        self._update_menu()

    def _handle_show_dashboard(self, icon, item) -> None:
        """Open dashboard in browser."""
        with self.model.lock:
            url = self.model.dashboard_url
        webbrowser.open(url)

    def _handle_stop_project(self, icon, item) -> None:
        """Clear the currently running project."""
        with self.model.lock:
            self.model.current_project = None
            self.model.project_started_at = None
        if self._on_project_change:
            self._on_project_change(None)
        self._update_menu()

    def _handle_sync_now(self, icon, item) -> None:
        """Trigger an immediate sync (debounced to prevent rapid clicks)."""
        now = time.monotonic()
        with self._sync_now_lock:
            if now - self._sync_now_last < self._sync_now_cooldown:
                return
            self._sync_now_last = now
        if self._on_sync_now:
            self._on_sync_now()

    def _make_project_handler(self, project: Optional[ProjectDict]) -> Callable:
        """Create a handler for switching to a project."""
        def handler(icon, item):
            with self.model.lock:
                self.model.current_project = project
                self.model.project_started_at = time.monotonic() if project else None
            if self._on_project_change:
                self._on_project_change(project)
            self._update_menu()
        return handler

    def _handle_logout(self, icon, item) -> None:
        """Handle log out menu click."""
        if self._on_logout:
            self._on_logout()

    def _handle_open_update(self, icon, item) -> None:
        """Open the GitHub release page for the available update."""
        with self.model.lock:
            url = self.model.update_url
        if url:
            webbrowser.open(url)

    def _handle_install_update(self, icon, item) -> None:
        """Download and install the update, then restart."""
        with self.model.lock:
            asset_url = self.model.update_asset_url
            if self.model.update_in_progress:
                return
            self.model.update_in_progress = True
        self._update_menu()

        if self._on_install_update and asset_url:
            self._on_install_update(asset_url)

    def _handle_quit(self, icon, item) -> None:
        """Handle quit menu click."""
        if self._on_quit:
            self._on_quit()
        self.stop()

    # -- Preference handlers -------------------------------------------------

    def _make_interval_handler(self, seconds: int) -> Callable:
        """Create a handler for setting sync interval."""
        def handler(icon, item):
            with self.model.lock:
                self.model.sync_interval = seconds
            if self._on_preferences:
                self._on_preferences("sync_interval", seconds)
            self._update_menu()
        return handler

    def _make_toggle_handler(self, attr: str, key: str) -> Callable:
        """Create a handler that toggles a boolean preference."""
        def handler(icon, item):
            with self.model.lock:
                new_value = not getattr(self.model, attr)
                setattr(self.model, attr, new_value)
            if self._on_preferences:
                self._on_preferences(key, new_value)
        return handler

    def _make_break_reminder_handler(self, enabled: bool, hours: int = 0) -> Callable:
        """Create a combined handler for break reminder radio selection."""
        def handler(icon, item):
            with self.model.lock:
                self.model.break_reminders_enabled = enabled
                if enabled and hours:
                    self.model.break_interval_hours = hours
            if self._on_preferences:
                self._on_preferences("break_reminders_enabled", enabled)
            if enabled and hours:
                if self._on_preferences:
                    self._on_preferences("break_interval_hours", hours)
            self._update_menu()
        return handler

    def _make_private_reminder_handler(self, enabled: bool, minutes: int = 0) -> Callable:
        """Create a combined handler for private time reminder radio selection."""
        def handler(icon, item):
            with self.model.lock:
                self.model.private_reminders_enabled = enabled
                if enabled and minutes:
                    self.model.private_interval_minutes = minutes
            if self._on_preferences:
                self._on_preferences("private_reminders_enabled", enabled)
            if enabled and minutes:
                if self._on_preferences:
                    self._on_preferences("private_interval_minutes", minutes)
            self._update_menu()
        return handler

    def _make_channel_handler(self, channel: str) -> Callable:
        """Create a handler for switching update channel."""
        def handler(icon, item):
            with self.model.lock:
                self.model.update_channel = channel
            if self._on_preferences:
                self._on_preferences("update_channel", channel)
            self._update_menu()
        return handler

    def _handle_export_logs(self, icon, item) -> None:
        """Handle export logs menu click."""
        if self._on_export_logs:
            self._on_export_logs()

    def _handle_open_config(self, icon, item) -> None:
        with self.model.lock:
            path = self.model.config_file_path
        if not path:
            return
        if platform.system() == "Windows":
            os.startfile(path)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def set_config(self, config: "Config") -> None:
        """Sync tray preferences state from Config object."""
        from urllib.parse import urlparse
        parsed = urlparse(config.api_url)
        dashboard_url = f"{parsed.scheme}://{parsed.netloc}/agent/my"
        with self.model.lock:
            self.model.sync_interval = config.sync.interval_seconds
            self.model.hash_titles = config.privacy.hash_titles
            self.model.domain_only_urls = config.privacy.domain_only_urls
            self.model.auto_categorize = config.privacy.auto_categorize
            self.model.track_display_info = config.privacy.track_display_info
            self.model.debug_mode = config.debug_mode
            self.model.auto_start = config.auto_start
            self.model.config_file_path = str(config.get_config_file())
            self.model.break_reminders_enabled = config.reminders.break_reminders_enabled
            self.model.break_interval_hours = config.reminders.break_interval_hours
            self.model.private_reminders_enabled = config.reminders.private_reminders_enabled
            self.model.private_interval_minutes = config.reminders.private_interval_minutes
            self.model.update_channel = config.update_channel
            self.model.dashboard_url = dashboard_url
        self._update_menu()

    def set_state(self, state: TrayState, status_text: Optional[str] = None) -> None:
        """Update tray icon state.

        Args:
            state: New state
            status_text: Optional status message for error state
        """
        with self.model.lock:
            self.model.state = state
            if status_text:
                self.model.status_text = status_text
        self._update_icon()

    def set_paused(self, paused: bool) -> None:
        """Set paused state."""
        with self.model.lock:
            self.model.paused = paused
        if paused:
            self.set_state(TrayState.PAUSED)
        else:
            with self.model.lock:
                on_break = self.model.on_break
                private = self.model.private_mode
            if on_break:
                self.set_state(TrayState.ON_BREAK)
            elif private:
                self.set_state(TrayState.PRIVATE)
            else:
                self.set_state(TrayState.SYNCING)

    def update_stats(
        self,
        hours_today: Optional[str] = None,
        last_sync: Optional[str] = None,
        queue_size: Optional[int] = None,
        events_today: Optional[int] = None,
        hours_this_week: Optional[str] = None,
        hours_this_month: Optional[str] = None,
        daily_avg_this_week: Optional[str] = None,
        **_kwargs,
    ) -> None:
        """Update statistics shown in menu.

        Args:
            hours_today: Formatted hours string (e.g. "4h 24m")
            last_sync: Last sync time string
            queue_size: Number of events in offline queue
            events_today: Backward-compatible alias from older callers
            hours_this_week: Formatted weekly hours string
            hours_this_month: Formatted monthly hours string
            daily_avg_this_week: Formatted daily average string
        """
        # Backward compatibility for older builds that pass events_today
        if hours_today is None and events_today is not None:
            try:
                total_seconds = int(events_today)
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                hours_today = f"{hours}h {minutes}m"
            except (TypeError, ValueError):
                pass

        with self.model.lock:
            if hours_today is not None:
                self.model.hours_today = hours_today
            if last_sync is not None:
                self.model.last_sync = last_sync
            if queue_size is not None:
                self.model.queue_size = queue_size
            if hours_this_week is not None:
                self.model.hours_this_week = hours_this_week
            if hours_this_month is not None:
                self.model.hours_this_month = hours_this_month
            if daily_avg_this_week is not None:
                self.model.daily_avg_this_week = daily_avg_this_week
        self._update_menu()

    def set_user(self, email: Optional[str], name: Optional[str] = None, role: Optional[str] = None) -> None:
        """Set current user info."""
        with self.model.lock:
            self.model.user_email = email
            self.model.user_name = name
            self.model.user_role = role
        self._update_menu()

    def set_projects(self, projects: list[ProjectDict], current_project: Optional[ProjectDict] = None) -> None:
        """Set available projects and current selection."""
        with self.model.lock:
            self.model.projects = projects
            if current_project:
                self.model.current_project = current_project
                # Only set start time if not already tracking this project
                if self.model.project_started_at is None:
                    self.model.project_started_at = time.monotonic()
        self._update_menu()

    def set_active_time(self, active_time) -> None:
        """Update tooltip and menu with today's active work time.

        Args:
            active_time: timedelta with today's active time
        """
        total_seconds = int(active_time.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        with self.model.lock:
            self.model.hours_today = f"{hours}h {minutes}m"
        self._update_tooltip(f"BetterFlow - Today: {hours}h {minutes}m active")
        self._update_menu()

    def set_update_available(self, version: str, url: str, asset_url: Optional[str] = None) -> None:
        """Show an update-available item in the tray menu."""
        with self.model.lock:
            self.model.update_version = version
            self.model.update_url = url
            self.model.update_asset_url = asset_url
        self._update_menu()

    def set_update_progress(self, status: str) -> None:
        """Update the self-update progress text in the menu."""
        with self.model.lock:
            self.model.update_status = status
        self._update_menu()

    def _update_tooltip(self, tooltip: str) -> None:
        """Update the tray icon tooltip."""
        try:
            icon = self._icon
            if icon:
                icon.title = tooltip
        except AttributeError:
            pass  # Icon was stopped concurrently

    def tick_clock(self) -> None:
        """Called periodically to advance the clock-face icon hands."""
        self._update_icon()

    def _update_icon(self) -> None:
        """Update the tray icon image and menu.

        On macOS, image updates are dispatched to the main thread via
        performSelectorOnMainThread because AppKit calls from background
        threads silently fail in PyInstaller .app bundles.
        """
        icon = self._icon
        if not icon:
            return
        with self.model.lock:
            state = self.model.state
        color = STATE_COLORS.get(state, STATE_COLORS[TrayState.STARTING])
        new_image = create_icon_image(color)

        if platform.system() == "Darwin":
            try:
                self._set_icon_main_thread(new_image)
            except Exception:
                logger.debug("Main-thread icon update failed, using fallback")
                self._icon.icon = new_image
        else:
            self._icon.icon = new_image

        self._update_menu()

    def _set_icon_main_thread(self, pil_img: Image.Image) -> None:
        """Update the NSStatusItem button image on the main thread (macOS).

        Converts a PIL image to NSImage and dispatches setImage: to the
        main thread run loop where AppKit can properly render it.
        """
        import AppKit
        import Foundation
        import io

        thickness = 22
        try:
            thickness = max(int(self._icon._status_bar.thickness()), 22)
        except Exception:
            pass

        sz = thickness
        if pil_img.size != (sz, sz):
            pil_img = pil_img.resize((sz, sz), Image.LANCZOS)

        buf = io.BytesIO()
        pil_img.save(buf, "png")
        ns_data = Foundation.NSData(buf.getvalue())
        ns_img = AppKit.NSImage.alloc().initWithData_(ns_data)
        ns_img.setSize_(Foundation.NSSize(sz, sz))

        # Update pystray internal state to prevent stale-image redraws
        try:
            self._icon._icon = pil_img
            self._icon._icon_valid = True
        except AttributeError:
            logger.debug("pystray internal attributes unavailable for icon state update")

        # Dispatch setImage: to the main thread where AppKit works correctly
        try:
            button = self._icon._status_item.button()
            if button:
                button.performSelectorOnMainThread_withObject_waitUntilDone_(
                    b"setImage:", ns_img, False,
                )
        except AttributeError:
            logger.debug("pystray _status_item unavailable, falling back to icon assignment")
            self._icon.icon = pil_img

    def _update_menu(self) -> None:
        """Update the tray menu (debounced: max once per second)."""
        icon = self._icon
        if not icon:
            return
        now = time.monotonic()
        with self._menu_lock:
            elapsed = now - self._menu_last_rebuild
            if elapsed >= self._menu_debounce_interval:
                self._menu_last_rebuild = now
                self._menu_pending = False
                self._icon.menu = self._create_menu()
            elif not self._menu_pending:
                self._menu_pending = True
                delay = self._menu_debounce_interval - elapsed
                timer = threading.Timer(delay, self._flush_menu)
                timer.daemon = True
                timer.start()

    def _flush_menu(self) -> None:
        """Deferred menu rebuild after debounce delay."""
        with self._menu_lock:
            self._menu_pending = False
            self._menu_last_rebuild = time.monotonic()
            if self._icon:
                self._icon.menu = self._create_menu()

    def start(self) -> None:
        """Start the tray icon in a background thread."""
        if self._icon is not None:
            return

        color = STATE_COLORS[self.model.state]
        self._icon = pystray.Icon(
            "BetterFlow",
            create_icon_image(color),
            "BetterFlow",
            self._create_menu(),
        )

        # Run in background thread
        self._thread = threading.Thread(target=self._icon.run, daemon=True)
        self._thread.start()
        logger.info("Tray icon started")

    def stop(self) -> None:
        """Stop the tray icon."""
        if self._icon:
            self._icon.stop()
            self._icon = None
            logger.info("Tray icon stopped")

    def run_blocking(self) -> None:
        """Run the tray icon in the main thread (blocking).

        Retries up to 3 times if the event loop exits within 2 seconds,
        which can happen on macOS when NSApplication fails to initialise.
        """
        _prevent_termination()

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            if self._icon is None:
                color = STATE_COLORS[self.model.state]
                self._icon = pystray.Icon(
                    "BetterFlow",
                    create_icon_image(color),
                    "BetterFlow",
                    self._create_menu(),
                )

            # Ensure activation policy is Accessory (1) for tray apps.
            # 0=Regular, 1=Accessory (tray apps), 2=Prohibited (no UI).
            try:
                policy = self._icon._app.activationPolicy()
                logger.debug("NSApp activation policy: %d", policy)
                if policy != 1:
                    self._icon._app.setActivationPolicy_(1)
                    logger.debug("Set activation policy to Accessory (1)")
            except Exception:
                logger.debug("Failed to check/set activation policy", exc_info=True)

            # Make the icon visible on the main thread BEFORE the event
            # loop starts.  pystray's default setup runs visible=True on
            # a background thread, and AppKit calls from background threads
            # silently fail in PyInstaller .app bundles on macOS.
            self._icon.visible = True

            start = time.monotonic()
            logger.info("Tray event loop starting (attempt %d/%d)", attempt, max_retries)
            # Pass no-op setup since we already set visible=True above.
            self._icon.run(setup=lambda icon: None)
            elapsed = time.monotonic() - start

            if elapsed > 2.0:
                # Normal exit (user quit or stop() called)
                logger.info("Tray event loop exited after %.1fs", elapsed)
                return

            logger.warning(
                "Tray event loop exited after %.1fs (attempt %d/%d)",
                elapsed, attempt, max_retries,
            )
            self._icon = None  # Reset for retry
            time.sleep(0.5)

        logger.error("Tray icon failed to start after %d attempts", max_retries)
