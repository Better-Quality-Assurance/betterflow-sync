"""System tray icon and menu."""

import logging
import os
import platform
import threading
import webbrowser
from enum import Enum
from typing import TYPE_CHECKING, Callable, Optional, TypedDict

from PIL import Image, ImageDraw

if TYPE_CHECKING:
    from ..config import Config


def _hide_from_dock() -> None:
    """Hide the app from the macOS Dock."""
    if platform.system() != "Darwin":
        return
    try:
        import AppKit
        ns_app = AppKit.NSApplication.sharedApplication()
        # NSApplicationActivationPolicyAccessory = 1 (no Dock icon)
        ns_app.setActivationPolicy_(1)
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
        self.state: TrayState = TrayState.STARTING
        self.paused: bool = False
        self.private_mode: bool = False
        self.status_text: str = "Starting..."
        self.hours_today: str = "0h 0m"
        self.last_sync: str = "Never"
        self.queue_size: int = 0
        self.user_email: Optional[str] = None
        self.user_name: Optional[str] = None

        # Projects
        self.projects: list[ProjectDict] = []
        self.current_project: Optional[ProjectDict] = None

        # Preferences
        self.sync_interval: int = 60
        self.hash_titles: bool = True
        self.domain_only_urls: bool = True
        self.debug_mode: bool = False
        self.auto_start: bool = False
        self.config_file_path: Optional[str] = None
        self.dashboard_url: str = "https://app.betterflow.eu/dashboard"
        self.company_name: Optional[str] = None

        # Reminder preferences
        self.break_reminders_enabled: bool = True
        self.break_interval_hours: int = 2
        self.private_reminders_enabled: bool = True
        self.private_interval_minutes: int = 20

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
    STARTING = "starting"  # Blue - starting up
    WAITING_AUTH = "waiting_auth"  # Amber - waiting for browser login


# Colors for each state
STATE_COLORS = {
    TrayState.SYNCING: "#22c55e",  # Green
    TrayState.QUEUED: "#eab308",  # Yellow
    TrayState.QUEUE_WARNING: "#f97316",  # Orange - queue nearing capacity
    TrayState.ERROR: "#ef4444",  # Red
    TrayState.PAUSED: "#9ca3af",  # Gray
    TrayState.PRIVATE: "#6b7280",  # Dark gray - private time
    TrayState.STARTING: "#3b82f6",  # Blue
    TrayState.WAITING_AUTH: "#f59e0b",  # Amber
}


def create_icon_image(color: str, size: int = 64) -> Image.Image:
    """Create a simple colored circle icon.

    Args:
        color: Hex color code
        size: Icon size in pixels

    Returns:
        PIL Image
    """
    # Create transparent image
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Draw filled circle
    margin = size // 8
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill=color,
    )

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

        self.model = TrayModel()

        self._icon: Optional[pystray.Icon] = None
        self._thread: Optional[threading.Thread] = None

    def _create_menu(self) -> pystray.Menu:
        """Create the tray menu."""
        items = []
        logged_in = self.model.user_email is not None

        # ── User identity & status ──────────────────────────
        if self.model.user_email:
            if self.model.user_name and self.model.user_name != self.model.user_email:
                label = f"{self.model.user_name} ({self.model.user_email})"
            else:
                label = self.model.user_email
            items.append(Item(label, None, enabled=False))

        items.append(Item(f"App status: {self._get_status_text()}", None, enabled=False))
        items.append(Item(f"Hours today: {self.model.hours_today}", None, enabled=False))

        # ── Dashboard & Project Manager links ───────────────
        items.append(Item("Show My Dashboard", self._handle_show_dashboard, enabled=logged_in))
        items.append(Item("Project Manager - Start / Stop / Create", self._handle_project_manager, enabled=logged_in))

        items.append(Item("─" * 20, None, enabled=False))

        # ── Running Project ─────────────────────────────────
        items.append(Item("Running Project", None, enabled=False))
        if self.model.current_project:
            items.append(Item(f"  {self.model.current_project['name']}", None, enabled=False))
            items.append(Item(f"  Stop ({self.model.hours_today})", self._handle_stop_project, enabled=logged_in))
        else:
            items.append(Item("  No project selected", None, enabled=False))

        items.append(Item("─" * 20, None, enabled=False))

        # ── Recent Projects ─────────────────────────────────
        if self.model.projects:
            items.append(Item("Recent Projects", None, enabled=False))
            for proj in self.model.projects:
                is_current = (
                    self.model.current_project is not None
                    and self.model.current_project["id"] == proj["id"]
                )
                items.append(Item(
                    f"  {proj['name']}",
                    self._make_project_handler(proj),
                    checked=lambda item, p=proj: (
                        self.model.current_project is not None
                        and self.model.current_project["id"] == p["id"]
                    ),
                    enabled=logged_in and not is_current,
                ))
            items.append(Item("─" * 20, None, enabled=False))

        # ── Private Time toggle ─────────────────────────────
        if self.model.private_mode:
            items.append(Item("End Private Time", self._handle_private_toggle, enabled=logged_in))
        else:
            items.append(Item("Private Time", self._handle_private_toggle, enabled=logged_in))

        # ── Private Time Reminder submenu ───────────────────
        items.append(Item("Private Time Reminder", pystray.Menu(
            Item(
                "Disabled",
                self._make_private_reminder_handler(enabled=False),
                checked=lambda item: not self.model.private_reminders_enabled,
            ),
            Item(
                "Every 15 Minutes",
                self._make_private_reminder_handler(enabled=True, minutes=15),
                checked=lambda item: self.model.private_reminders_enabled and self.model.private_interval_minutes == 15,
            ),
            Item(
                "Every 35 Minutes",
                self._make_private_reminder_handler(enabled=True, minutes=35),
                checked=lambda item: self.model.private_reminders_enabled and self.model.private_interval_minutes == 35,
            ),
            Item(
                "Every 45 Minutes",
                self._make_private_reminder_handler(enabled=True, minutes=45),
                checked=lambda item: self.model.private_reminders_enabled and self.model.private_interval_minutes == 45,
            ),
        ), enabled=logged_in))

        # ── Break Time Reminder submenu ─────────────────────
        items.append(Item("Break Time Reminder", pystray.Menu(
            Item(
                "Disabled",
                self._make_break_reminder_handler(enabled=False),
                checked=lambda item: not self.model.break_reminders_enabled,
            ),
            Item(
                "Every 1 Hour",
                self._make_break_reminder_handler(enabled=True, hours=1),
                checked=lambda item: self.model.break_reminders_enabled and self.model.break_interval_hours == 1,
            ),
            Item(
                "Every 2 Hours",
                self._make_break_reminder_handler(enabled=True, hours=2),
                checked=lambda item: self.model.break_reminders_enabled and self.model.break_interval_hours == 2,
            ),
            Item(
                "Every 3 Hours",
                self._make_break_reminder_handler(enabled=True, hours=3),
                checked=lambda item: self.model.break_reminders_enabled and self.model.break_interval_hours == 3,
            ),
        ), enabled=logged_in))

        # ── Quick Menu submenu ──────────────────────────────
        quick_items = []
        if not self.model.private_mode:
            if self.model.paused:
                quick_items.append(Item("Resume Tracking", self._handle_resume))
            else:
                quick_items.append(Item("Pause Tracking", self._handle_pause))
        quick_items.append(Item("Sync Now", self._handle_sync_now))
        items.append(Item("Quick Menu", pystray.Menu(*quick_items), enabled=logged_in))

        items.append(Item("─" * 20, None, enabled=False))

        # ── Preferences submenu ─────────────────────────────
        items.append(Item("Preferences", pystray.Menu(
            Item(
                "Sync Interval",
                pystray.Menu(
                    Item("30s", self._make_interval_handler(30), checked=lambda item: self.model.sync_interval == 30),
                    Item("60s", self._make_interval_handler(60), checked=lambda item: self.model.sync_interval == 60),
                    Item("120s", self._make_interval_handler(120), checked=lambda item: self.model.sync_interval == 120),
                    Item("300s", self._make_interval_handler(300), checked=lambda item: self.model.sync_interval == 300),
                ),
            ),
            Item(
                "Hash Window Titles",
                self._make_toggle_handler("hash_titles", "hash_titles"),
                checked=lambda item: self.model.hash_titles,
            ),
            Item(
                "Domain Only URLs",
                self._make_toggle_handler("domain_only_urls", "domain_only_urls"),
                checked=lambda item: self.model.domain_only_urls,
            ),
            Item(
                "Debug Mode",
                self._make_toggle_handler("debug_mode", "debug_mode"),
                checked=lambda item: self.model.debug_mode,
            ),
            Item(
                "Launch at Login",
                self._make_toggle_handler("auto_start", "auto_start"),
                checked=lambda item: self.model.auto_start,
            ),
            Item("Open Config File", self._handle_open_config),
        ), enabled=logged_in))

        items.append(Item("─" * 20, None, enabled=False))

        # ── Account ─────────────────────────────────────────
        if self.model.user_email:
            items.append(Item("Log Out", self._handle_logout))
        else:
            items.append(Item("Login", self._handle_login))

        items.append(Item("─" * 20, None, enabled=False))
        items.append(Item("Quit", self._handle_quit))

        return pystray.Menu(*items)

    def _get_status_text(self) -> str:
        """Get short status text for the menu."""
        if self.model.private_mode:
            return "Private Time"
        elif self.model.state == TrayState.SYNCING:
            return "Active"
        elif self.model.state == TrayState.QUEUED:
            return "Offline"
        elif self.model.state == TrayState.QUEUE_WARNING:
            return "Offline (queue full)"
        elif self.model.state == TrayState.ERROR:
            return "Error"
        elif self.model.state == TrayState.PAUSED:
            return "Paused"
        elif self.model.state == TrayState.WAITING_AUTH:
            return "Waiting for login..."
        else:
            return "Starting..."

    # -- Menu action handlers ------------------------------------------------

    def _handle_login(self, icon, item) -> None:
        """Handle login menu click."""
        if self._on_login:
            self._on_login()

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
        self.model.private_mode = not self.model.private_mode
        if self.model.private_mode:
            self.set_state(TrayState.PRIVATE)
        else:
            self.set_state(TrayState.SYNCING)
        if self._on_private_toggle:
            self._on_private_toggle(self.model.private_mode)
        self._update_menu()

    def _handle_show_dashboard(self, icon, item) -> None:
        """Open dashboard in browser."""
        webbrowser.open(self.model.dashboard_url)

    def _handle_project_manager(self, icon, item) -> None:
        """Open project manager page in browser."""
        base = self.model.dashboard_url.rsplit("/", 1)[0]
        webbrowser.open(f"{base}/projects")

    def _handle_stop_project(self, icon, item) -> None:
        """Clear the currently running project."""
        self.model.current_project = None
        if self._on_project_change:
            self._on_project_change(None)
        self._update_menu()

    def _handle_sync_now(self, icon, item) -> None:
        """Trigger an immediate sync."""
        if self._on_sync_now:
            self._on_sync_now()

    def _make_project_handler(self, project: Optional[ProjectDict]) -> Callable:
        """Create a handler for switching to a project."""
        def handler(icon, item):
            self.model.current_project = project
            if self._on_project_change:
                self._on_project_change(project)
            self._update_menu()
        return handler

    def _handle_logout(self, icon, item) -> None:
        """Handle log out menu click."""
        if self._on_logout:
            self._on_logout()

    def _handle_quit(self, icon, item) -> None:
        """Handle quit menu click."""
        if self._on_quit:
            self._on_quit()
        self.stop()

    # -- Preference handlers -------------------------------------------------

    def _make_interval_handler(self, seconds: int) -> Callable:
        """Create a handler for setting sync interval."""
        def handler(icon, item):
            self.model.sync_interval = seconds
            if self._on_preferences:
                self._on_preferences("sync_interval", seconds)
            self._update_menu()
        return handler

    def _make_toggle_handler(self, attr: str, key: str) -> Callable:
        """Create a handler that toggles a boolean preference."""
        def handler(icon, item):
            new_value = not getattr(self.model, attr)
            setattr(self.model, attr, new_value)
            if self._on_preferences:
                self._on_preferences(key, new_value)
        return handler

    def _make_break_reminder_handler(self, enabled: bool, hours: int = 0) -> Callable:
        """Create a combined handler for break reminder radio selection."""
        def handler(icon, item):
            self.model.break_reminders_enabled = enabled
            if self._on_preferences:
                self._on_preferences("break_reminders_enabled", enabled)
            if enabled and hours:
                self.model.break_interval_hours = hours
                if self._on_preferences:
                    self._on_preferences("break_interval_hours", hours)
            self._update_menu()
        return handler

    def _make_private_reminder_handler(self, enabled: bool, minutes: int = 0) -> Callable:
        """Create a combined handler for private time reminder radio selection."""
        def handler(icon, item):
            self.model.private_reminders_enabled = enabled
            if self._on_preferences:
                self._on_preferences("private_reminders_enabled", enabled)
            if enabled and minutes:
                self.model.private_interval_minutes = minutes
                if self._on_preferences:
                    self._on_preferences("private_interval_minutes", minutes)
            self._update_menu()
        return handler

    def _handle_open_config(self, icon, item) -> None:
        if self.model.config_file_path:
            import subprocess
            if platform.system() == "Windows":
                os.startfile(self.model.config_file_path)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", self.model.config_file_path])
            else:
                subprocess.Popen(["xdg-open", self.model.config_file_path])

    def set_config(self, config: "Config") -> None:
        """Sync tray preferences state from Config object."""
        self.model.sync_interval = config.sync.interval_seconds
        self.model.hash_titles = config.privacy.hash_titles
        self.model.domain_only_urls = config.privacy.domain_only_urls
        self.model.debug_mode = config.debug_mode
        self.model.auto_start = config.auto_start
        self.model.config_file_path = str(config.get_config_file())
        self.model.break_reminders_enabled = config.reminders.break_reminders_enabled
        self.model.break_interval_hours = config.reminders.break_interval_hours
        self.model.private_reminders_enabled = config.reminders.private_reminders_enabled
        self.model.private_interval_minutes = config.reminders.private_interval_minutes
        # Derive dashboard URL from API URL (e.g. https://app.betterflow.eu/api/agent -> https://app.betterflow.eu/dashboard)
        from urllib.parse import urlparse
        parsed = urlparse(config.api_url)
        self.model.dashboard_url = f"{parsed.scheme}://{parsed.netloc}/dashboard"
        self._update_menu()

    def set_state(self, state: TrayState, status_text: Optional[str] = None) -> None:
        """Update tray icon state.

        Args:
            state: New state
            status_text: Optional status message for error state
        """
        self.model.state = state
        if status_text:
            self.model.status_text = status_text
        self._update_icon()

    def set_paused(self, paused: bool) -> None:
        """Set paused state."""
        self.model.paused = paused
        if paused:
            self.set_state(TrayState.PAUSED)
        else:
            self.set_state(TrayState.SYNCING)

    def update_stats(
        self,
        hours_today: Optional[str] = None,
        last_sync: Optional[str] = None,
        queue_size: Optional[int] = None,
        events_today: Optional[int] = None,
        **_kwargs,
    ) -> None:
        """Update statistics shown in menu.

        Args:
            hours_today: Formatted hours string (e.g. "4h 24m")
            last_sync: Last sync time string
            queue_size: Number of events in offline queue
            events_today: Backward-compatible alias from older callers
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

        if hours_today is not None:
            self.model.hours_today = hours_today
        if last_sync is not None:
            self.model.last_sync = last_sync
        if queue_size is not None:
            self.model.queue_size = queue_size
        self._update_menu()

    def set_user(self, email: Optional[str], name: Optional[str] = None) -> None:
        """Set current user info."""
        self.model.user_email = email
        self.model.user_name = name
        self._update_menu()

    def set_projects(self, projects: list[ProjectDict], current_project: Optional[ProjectDict] = None) -> None:
        """Set available projects and current selection."""
        self.model.projects = projects
        if current_project:
            self.model.current_project = current_project
        self._update_menu()

    def _update_icon(self) -> None:
        """Update the tray icon image and menu."""
        if self._icon:
            color = STATE_COLORS.get(self.model.state, STATE_COLORS[TrayState.STARTING])
            self._icon.icon = create_icon_image(color)
            self._icon.menu = self._create_menu()

    def _update_menu(self) -> None:
        """Update the tray menu."""
        if self._icon:
            self._icon.menu = self._create_menu()

    def start(self) -> None:
        """Start the tray icon in a background thread."""
        if self._icon is not None:
            return

        color = STATE_COLORS[self.model.state]
        self._icon = pystray.Icon(
            "BetterFlow Sync",
            create_icon_image(color),
            "BetterFlow Sync",
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
        """Run the tray icon in the main thread (blocking)."""
        _hide_from_dock()
        if self._icon is None:
            color = STATE_COLORS[self.model.state]
            self._icon = pystray.Icon(
                "BetterFlow Sync",
                create_icon_image(color),
                "BetterFlow Sync",
                self._create_menu(),
            )
        self._icon.run()
