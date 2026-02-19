"""System tray icon and menu."""

import logging
import platform
import threading
import webbrowser
from enum import Enum
from typing import Callable, Optional


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
    ERROR = "error"  # Red - auth failed or AW not running
    PAUSED = "paused"  # Gray - user paused tracking
    STARTING = "starting"  # Blue - starting up
    WAITING_AUTH = "waiting_auth"  # Amber - waiting for browser login


# Colors for each state
STATE_COLORS = {
    TrayState.SYNCING: "#22c55e",  # Green
    TrayState.QUEUED: "#eab308",  # Yellow
    TrayState.ERROR: "#ef4444",  # Red
    TrayState.PAUSED: "#9ca3af",  # Gray
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
        on_pause: Optional[Callable[[], None]] = None,
        on_resume: Optional[Callable[[], None]] = None,
        on_preferences: Optional[Callable[[], None]] = None,
        on_logout: Optional[Callable[[], None]] = None,
        on_quit: Optional[Callable[[], None]] = None,
    ):
        """Initialize tray icon.

        Args:
            on_pause: Callback when pause is clicked
            on_resume: Callback when resume is clicked
            on_preferences: Callback when preferences is clicked
            on_logout: Callback when sign out is clicked
            on_quit: Callback when quit is clicked
        """
        if pystray is None:
            raise ImportError("pystray is required for system tray support")

        self._on_pause = on_pause
        self._on_resume = on_resume
        self._on_preferences = on_preferences
        self._on_logout = on_logout
        self._on_quit = on_quit

        self._state = TrayState.STARTING
        self._paused = False
        self._status_text = "Starting..."
        self._events_today = 0
        self._last_sync = "Never"
        self._queue_size = 0
        self._user_email: Optional[str] = None
        self._user_name: Optional[str] = None

        self._icon: Optional[pystray.Icon] = None
        self._thread: Optional[threading.Thread] = None

    def _create_menu(self) -> pystray.Menu:
        """Create the tray menu."""
        items = []

        # User name at top
        if self._user_name:
            items.append(Item(self._user_name, None, enabled=False))

        # Status line
        items.append(Item(self._get_status_line(), None, enabled=False))
        items.append(Item("─" * 20, None, enabled=False))

        # Stats
        items.append(Item(f"Last sync: {self._last_sync}", None, enabled=False))
        items.append(Item(f"Events today: {self._events_today:,}", None, enabled=False))
        if self._queue_size > 0:
            items.append(Item(f"Queued: {self._queue_size:,}", None, enabled=False))

        items.append(Item("─" * 20, None, enabled=False))

        # Pause/Resume
        if self._paused:
            items.append(Item("Resume Tracking", self._handle_resume))
        else:
            items.append(Item("Pause Tracking", self._handle_pause))

        # Actions
        items.append(Item("Preferences...", self._handle_preferences))
        items.append(Item("View Dashboard", self._handle_dashboard))

        items.append(Item("─" * 20, None, enabled=False))

        # Account
        if self._user_email:
            items.append(Item("Sign Out", self._handle_logout))
        else:
            items.append(Item("Sign In...", self._handle_preferences))

        items.append(Item("─" * 20, None, enabled=False))
        items.append(Item("Quit", self._handle_quit))

        return pystray.Menu(*items)

    def _get_status_line(self) -> str:
        """Get the status line text."""
        if self._state == TrayState.SYNCING:
            return "Status: Syncing ✓"
        elif self._state == TrayState.QUEUED:
            return "Status: Offline (queuing)"
        elif self._state == TrayState.ERROR:
            return f"Status: {self._status_text}"
        elif self._state == TrayState.PAUSED:
            return "Status: Paused"
        elif self._state == TrayState.WAITING_AUTH:
            return "Status: Waiting for browser login..."
        else:
            return "Status: Starting..."

    def _handle_pause(self, icon, item) -> None:
        """Handle pause menu click."""
        if self._on_pause:
            self._on_pause()

    def _handle_resume(self, icon, item) -> None:
        """Handle resume menu click."""
        if self._on_resume:
            self._on_resume()

    def _handle_preferences(self, icon, item) -> None:
        """Handle preferences menu click."""
        if self._on_preferences:
            self._on_preferences()

    def _handle_dashboard(self, icon, item) -> None:
        """Open dashboard in browser."""
        webbrowser.open("https://betterflow.eu/dashboard")

    def _handle_logout(self, icon, item) -> None:
        """Handle sign out menu click."""
        if self._on_logout:
            self._on_logout()

    def _handle_quit(self, icon, item) -> None:
        """Handle quit menu click."""
        if self._on_quit:
            self._on_quit()
        self.stop()

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
        events_today: Optional[int] = None,
        last_sync: Optional[str] = None,
        queue_size: Optional[int] = None,
    ) -> None:
        """Update statistics shown in menu.

        Args:
            events_today: Number of events synced today
            last_sync: Last sync time string
            queue_size: Number of events in offline queue
        """
        if events_today is not None:
            self._events_today = events_today
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
