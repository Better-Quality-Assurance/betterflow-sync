"""System tray icon and menu."""

import logging
import platform
import threading
import webbrowser
from enum import Enum
from typing import TYPE_CHECKING, Callable, Optional, TypedDict

if TYPE_CHECKING:
    from ..config import Config


def _hide_from_dock() -> None:
    """Hide the app from the macOS Dock."""
    if platform.system() != "Darwin":
        return
    try:
        import AppKit
        NSApp = AppKit.NSApplication.sharedApplication()
        # NSApplicationActivationPolicyAccessory = 1 (no Dock icon)
        NSApp.setActivationPolicy_(1)
    except Exception:
        pass

from PIL import Image, ImageDraw

__all__ = ["TrayIcon", "TrayState", "STATE_COLORS", "create_icon_image", "ProjectDict"]


class ProjectDict(TypedDict):
    """Typed project dictionary from the API."""

    id: int
    name: str

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

        self._state = TrayState.STARTING
        self._paused = False
        self._private_mode = False
        self._status_text = "Starting..."
        self._hours_today = "0h 0m"
        self._last_sync = "Never"
        self._queue_size = 0
        self._user_email: Optional[str] = None
        self._user_name: Optional[str] = None

        # Project state
        self._projects: list[ProjectDict] = []
        self._current_project: Optional[ProjectDict] = None

        # Preferences state
        self._sync_interval: int = 60
        self._hash_titles: bool = True
        self._domain_only_urls: bool = True
        self._debug_mode: bool = False
        self._auto_start: bool = False
        self._config_file_path: Optional[str] = None
        self._dashboard_url: str = "https://app.betterflow.eu/dashboard"

        self._icon: Optional[pystray.Icon] = None
        self._thread: Optional[threading.Thread] = None

    def _create_menu(self) -> pystray.Menu:
        """Create the tray menu."""
        items = []

        # User identity at top
        if self._user_email:
            if self._user_name and self._user_name != self._user_email:
                label = f"{self._user_name} ({self._user_email})"
            else:
                label = self._user_email
            items.append(Item(label, None, enabled=False))

        # Status line
        items.append(Item(self._get_status_line(), None, enabled=False))

        # Project line
        if self._current_project:
            items.append(Item(f"Project: {self._current_project['name']}", None, enabled=False))
        elif self._projects:
            items.append(Item("Project: None", None, enabled=False))

        items.append(Item("─" * 20, None, enabled=False))

        # Stats
        items.append(Item(f"Last sync: {self._last_sync}", None, enabled=False))
        items.append(Item(f"Hours today: {self._hours_today}", None, enabled=False))
        if self._queue_size > 0:
            items.append(Item(f"Queued: {self._queue_size:,}", None, enabled=False))

        items.append(Item("─" * 20, None, enabled=False))

        logged_in = self._user_email is not None

        # Private Time toggle
        if self._private_mode:
            items.append(Item("End Private Time", self._handle_private_toggle, enabled=logged_in))
        else:
            items.append(Item("Private Time", self._handle_private_toggle, enabled=logged_in))

        # Pause/Resume (only show when not in private mode)
        if not self._private_mode:
            if self._paused:
                items.append(Item("Resume Tracking", self._handle_resume, enabled=logged_in))
            else:
                items.append(Item("Pause Tracking", self._handle_pause, enabled=logged_in))

        # Switch Project submenu
        if self._projects:
            project_items = []
            # "None" option to clear project
            project_items.append(Item(
                "None",
                self._make_project_handler(None),
                checked=lambda item: self._current_project is None,
            ))
            for proj in self._projects:
                project_items.append(Item(
                    proj["name"],
                    self._make_project_handler(proj),
                    checked=lambda item, p=proj: self._current_project is not None and self._current_project["id"] == p["id"],
                ))
            items.append(Item("Switch Project", pystray.Menu(*project_items), enabled=logged_in))

        # Actions
        items.append(Item("Preferences", pystray.Menu(
            Item(
                "Sync Interval",
                pystray.Menu(
                    Item("30s", self._make_interval_handler(30), checked=lambda item: self._sync_interval == 30),
                    Item("60s", self._make_interval_handler(60), checked=lambda item: self._sync_interval == 60),
                    Item("120s", self._make_interval_handler(120), checked=lambda item: self._sync_interval == 120),
                    Item("300s", self._make_interval_handler(300), checked=lambda item: self._sync_interval == 300),
                ),
            ),
            Item(
                "Hash Window Titles",
                self._make_toggle_handler("_hash_titles", "hash_titles"),
                checked=lambda item: self._hash_titles,
            ),
            Item(
                "Domain Only URLs",
                self._make_toggle_handler("_domain_only_urls", "domain_only_urls"),
                checked=lambda item: self._domain_only_urls,
            ),
            Item(
                "Debug Mode",
                self._make_toggle_handler("_debug_mode", "debug_mode"),
                checked=lambda item: self._debug_mode,
            ),
            Item(
                "Launch at Login",
                self._make_toggle_handler("_auto_start", "auto_start"),
                checked=lambda item: self._auto_start,
            ),
            Item("─" * 15, None, enabled=False),
            Item("Open Config File", self._handle_open_config),
        ), enabled=logged_in))
        items.append(Item("View Dashboard", self._handle_dashboard, enabled=logged_in))

        items.append(Item("─" * 20, None, enabled=False))

        # Account
        if self._user_email:
            items.append(Item("Sign Out", self._handle_logout))
        else:
            items.append(Item("Login", self._handle_login))

        items.append(Item("─" * 20, None, enabled=False))
        items.append(Item("Quit", self._handle_quit))

        return pystray.Menu(*items)

    def _get_status_line(self) -> str:
        """Get the status line text."""
        if self._private_mode:
            return "Status: Private Time"
        elif self._state == TrayState.SYNCING:
            return "Status: Active"
        elif self._state == TrayState.QUEUED:
            return "Status: Offline"
        elif self._state == TrayState.QUEUE_WARNING:
            return "Status: Offline (queue full)"
        elif self._state == TrayState.ERROR:
            return "Status: Error"
        elif self._state == TrayState.PAUSED:
            return "Status: Paused"
        elif self._state == TrayState.WAITING_AUTH:
            return "Status: Waiting for login..."
        else:
            return "Status: Starting..."

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
        self._private_mode = not self._private_mode
        if self._private_mode:
            self.set_state(TrayState.PRIVATE)
        else:
            self.set_state(TrayState.SYNCING)
        if self._on_private_toggle:
            self._on_private_toggle(self._private_mode)
        self._update_menu()

    def _make_project_handler(self, project: Optional[ProjectDict]) -> Callable:
        """Create a handler for switching projects."""
        def handler(icon, item):
            self._current_project = project
            if self._on_project_change:
                self._on_project_change(project)
            self._update_menu()
        return handler

    def _handle_dashboard(self, icon, item) -> None:
        """Open dashboard in browser."""
        webbrowser.open(self._dashboard_url)

    def _handle_logout(self, icon, item) -> None:
        """Handle sign out menu click."""
        if self._on_logout:
            self._on_logout()

    def _handle_quit(self, icon, item) -> None:
        """Handle quit menu click."""
        if self._on_quit:
            self._on_quit()
        self.stop()

    def _make_interval_handler(self, seconds: int) -> Callable:
        """Create a handler for setting sync interval."""
        def handler(icon, item):
            self._sync_interval = seconds
            if self._on_preferences:
                self._on_preferences("sync_interval", seconds)
            self._update_menu()
        return handler

    def _make_toggle_handler(self, attr: str, key: str) -> Callable:
        """Create a handler that toggles a boolean preference."""
        def handler(icon, item):
            new_value = not getattr(self, attr)
            setattr(self, attr, new_value)
            if self._on_preferences:
                self._on_preferences(key, new_value)
        return handler

    def _handle_open_config(self, icon, item) -> None:
        if self._config_file_path:
            import subprocess
            subprocess.Popen(["open", self._config_file_path])

    def set_config(self, config: "Config") -> None:
        """Sync tray preferences state from Config object."""
        self._sync_interval = config.sync.interval_seconds
        self._hash_titles = config.privacy.hash_titles
        self._domain_only_urls = config.privacy.domain_only_urls
        self._debug_mode = config.debug_mode
        self._auto_start = config.auto_start
        self._config_file_path = str(config.get_config_file())
        # Derive dashboard URL from API URL (e.g. https://app.betterflow.eu/api/agent -> https://app.betterflow.eu/dashboard)
        from urllib.parse import urlparse
        parsed = urlparse(config.api_url)
        self._dashboard_url = f"{parsed.scheme}://{parsed.netloc}/dashboard"
        self._update_menu()

    def set_state(self, state: TrayState, status_text: Optional[str] = None) -> None:
        """Update tray icon state.

        Args:
            state: New state
            status_text: Optional status message for error state
        """
        self._state = state
        if status_text:
            self._status_text = status_text
        self._update_icon()

    def set_paused(self, paused: bool) -> None:
        """Set paused state."""
        self._paused = paused
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
            self._hours_today = hours_today
        if last_sync is not None:
            self._last_sync = last_sync
        if queue_size is not None:
            self._queue_size = queue_size
        self._update_menu()

    def set_user(self, email: Optional[str], name: Optional[str] = None) -> None:
        """Set current user info."""
        self._user_email = email
        self._user_name = name
        self._update_menu()

    def set_projects(self, projects: list[ProjectDict], current_project: Optional[ProjectDict] = None) -> None:
        """Set available projects and current selection."""
        self._projects = projects
        if current_project:
            self._current_project = current_project
        self._update_menu()

    def _update_icon(self) -> None:
        """Update the tray icon image and menu."""
        if self._icon:
            color = STATE_COLORS.get(self._state, STATE_COLORS[TrayState.STARTING])
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

        color = STATE_COLORS[self._state]
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
            color = STATE_COLORS[self._state]
            self._icon = pystray.Icon(
                "BetterFlow Sync",
                create_icon_image(color),
                "BetterFlow Sync",
                self._create_menu(),
            )
        self._icon.run()
