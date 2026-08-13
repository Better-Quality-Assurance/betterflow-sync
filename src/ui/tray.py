"""System tray icon and menu."""

# Annotations are lazy (PEP 563) so the module imports cleanly on headless hosts
# where pystray is unavailable (pystray=None) — type hints like `-> pystray.Menu`
# must not be evaluated at definition time.
from __future__ import annotations

import contextlib
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
    from .. import _build_info as _bi  # module execution
    _APP_VERSION = _bi.APP_VERSION
    BUILD_DATE = _bi.BUILD_DATE
    # RELEASE_VERSION carries any -beta.N/-rc.N suffix; getattr keeps us working
    # against an older bundle stamped before the field existed.
    _RELEASE_VERSION = getattr(_bi, "RELEASE_VERSION", _APP_VERSION)
except ImportError:
    try:
        import _build_info as _bi  # PyInstaller bundle (src/ is root, no parent package)
        _APP_VERSION = _bi.APP_VERSION
        BUILD_DATE = _bi.BUILD_DATE
        _RELEASE_VERSION = getattr(_bi, "RELEASE_VERSION", _APP_VERSION)
    except ImportError:
        try:
            from .. import __version__ as _APP_VERSION
        except ImportError:
            _APP_VERSION = "unknown"
        BUILD_DATE = "dev"
        _RELEASE_VERSION = _APP_VERSION

# Tray hover tooltip base. Surfaces the running version on hover even when the
# icon sits in the Windows hidden-icons overflow, where the right-click menu
# (and its Preferences > version item) isn't reachable. Uses the release version
# so a beta hover reads "BetterFlow v1.5.68-beta.1".
_BASE_TOOLTIP = f"BetterFlow v{_RELEASE_VERSION}"

from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    from datetime import datetime as _datetime
    from ..config import Config

try:
    from ..config import PRIVACY_POLICY_URL
except ImportError:
    with contextlib.suppress(ImportError):
        import _build_info  # noqa: F401 — ensure bundle path detection runs first
    from config import PRIVACY_POLICY_URL  # type: ignore[no-redef]

try:
    from ..clipboard import clipboard_available, copy_to_clipboard
    from ..hardware_serial import get_hardware_serial
    from ..machine_arch import ARM64, is_rosetta_translated, true_machine_arch
    from ..notifications import send_notification
except ImportError:
    from clipboard import clipboard_available, copy_to_clipboard  # type: ignore[no-redef]
    from hardware_serial import get_hardware_serial  # type: ignore[no-redef]
    from machine_arch import (  # type: ignore[no-redef]
        ARM64,
        is_rosetta_translated,
        true_machine_arch,
    )
    from notifications import send_notification  # type: ignore[no-redef]


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
    except Exception as e:
        logging.getLogger(__name__).debug(f"_prevent_termination failed: {e}")


__all__ = ["TrayIcon", "TrayState", "TrayModel", "STATE_COLORS", "create_icon_image", "ProjectDict"]


# ── macOS main-thread menu dispatch ──────────────────────────────
# AppKit is NOT thread-safe. NSMenu must be built and assigned on the main
# thread.  This helper NSObject receives a timer callback on the main
# run-loop and calls pystray's internal _update_menu() which does
# NSMenu construction + NSStatusItem.setMenu: safely.
_MenuProxy: type | None = None

if platform.system() == "Darwin":
    try:
        import Foundation as _Foundation
        import objc as _objc

        class _MenuProxy(_Foundation.NSObject):  # type: ignore[no-redef]
            """NSObject that triggers a pystray menu rebuild on the main thread."""

            _pystray_icon = _objc.ivar()

            def rebuildMenu_(self, timer) -> None:  # noqa: ANN001
                icon = self._pystray_icon
                if icon is not None:
                    icon._update_menu()
    except ImportError:
        pass


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
        # macOS Input Monitoring (keystroke/click capture). True until a check
        # proves otherwise so non-macOS platforms never show the warning.
        self.input_monitoring_ok: bool = True


# On Linux, pystray binds its backend at import time. Choose it explicitly:
# AppIndicator (gives a proper status-menu) when the GTK/AppIndicator GObject
# bindings are present, otherwise the pure-Xlib XOrg backend. Respect an
# operator-provided PYSTRAY_BACKEND if already set.
if sys.platform.startswith("linux") and not os.environ.get("PYSTRAY_BACKEND"):
    try:
        import gi  # noqa: F401

        gi.require_version("Gtk", "3.0")
        try:
            gi.require_version("AyatanaAppIndicator3", "0.1")
        except ValueError:
            gi.require_version("AppIndicator3", "0.1")
        os.environ["PYSTRAY_BACKEND"] = "appindicator"
    except (ImportError, ValueError):
        os.environ["PYSTRAY_BACKEND"] = "xorg"

try:
    import pystray
    from pystray import MenuItem as Item
except Exception:
    # A backend can fail to bind for reasons beyond ImportError — on a headless
    # box GTK fails to initialise (AppIndicator) or Xlib finds no display
    # (XOrg). Any failure here just means "no usable tray backend"; the app
    # already treats pystray=None as run-without-tray (e.g. headless servers,
    # CI). Try the pure-Xlib XOrg backend once — a failed import is dropped from
    # sys.modules, so re-importing re-runs pystray's backend selection — then
    # degrade gracefully.
    pystray = None
    Item = None
    if sys.platform.startswith("linux") and os.environ.get("PYSTRAY_BACKEND") != "xorg":
        os.environ["PYSTRAY_BACKEND"] = "xorg"
        try:
            import pystray
            from pystray import MenuItem as Item
        except Exception:
            pystray = None
            Item = None

logger = logging.getLogger(__name__)


# Windows reports "AMD64", Linux "x86_64"/"aarch64", Windows-on-ARM "ARM64" —
# four spellings of two instruction sets. Lower-cased keys, because the
# spelling is the platform's choice and not something a user should have to
# decode. Anything absent renders as its raw token: an unknown architecture is
# better shown verbatim than guessed at.
_NON_DARWIN_ARCH_ISAS = {
    "amd64": "64-bit x86",
    "x86_64": "64-bit x86",
    "arm64": "64-bit ARM",
    "aarch64": "64-bit ARM",
}


def arch_menu_label(
    system: Optional[str] = None,
    machine: Optional[str] = None,
    translated: Optional[bool] = None,
) -> str:
    """Build the label for the tray's architecture row.

    Pure and pystray-free for the same reason as ``serial_menu_row()`` below:
    the backend binds at import time and is unavailable on a headless CI runner,
    so a rule reachable only through a live TrayIcon could not be exercised in
    the environment that gates merges. The UI copy lives here rather than in
    ``machine_arch`` because that module's job is resolving the architecture,
    not wording a menu — the same split ``serial_menu_row``/``hardware_serial``
    already uses.

    The architecture itself comes from ``true_machine_arch()``, never re-derived
    here: this row and the updater's DMG choice are two halves of one promise,
    and a second copy of the mapping is how the tray ends up naming one
    architecture while the updater downloads another.

    The mismatch case names the remedy, not just the state: a user reading
    "Intel build" has no reason to know that is the wrong one.

    Off macOS the row reads "<instruction set> (<raw>)" to match the macOS
    rows, which are prose rather than a bare token. Two things it deliberately
    does NOT claim there, because only the Darwin branch resolves hardware:

    - **No CPU vendor.** Every x86_64 Mac really is an Intel Mac, so the Darwin
      row can say "Intel". ``AMD64`` on Windows names the x86-64 instruction
      set and says nothing about who made the chip, so copying that wording
      would be a plain factual error on a Ryzen.
    - **No hardware family.** These names describe the ISA this PROCESS
      executes, which is all ``platform.machine()`` reports. An x64 build under
      Windows-on-ARM emulation genuinely is executing x86-64, so "64-bit x86"
      stays true where "this is an x86 machine" would not. Seeing through that
      emulation needs a Windows equivalent of ``proc_translated``, which
      ``machine_arch`` deliberately does not model.

    An unrecognised value falls back to the raw token rather than guessing.

    Args:
        system: Override ``platform.system()`` for testing.
        machine: Override ``platform.machine()`` for testing.
        translated: Override the Rosetta determination for testing.
    """
    system = system or platform.system()
    machine = machine or platform.machine()
    if translated is None:
        translated = is_rosetta_translated(system=system)

    if system != "Darwin":
        isa = _NON_DARWIN_ARCH_ISAS.get(machine.lower())
        if not isa:
            return f"Architecture: {machine}"
        return f"Architecture: {isa} ({machine})"

    arch = true_machine_arch(system=system, machine=machine, translated=translated)
    if translated:
        return "Architecture: Intel build on Apple Silicon — switch to the Apple Silicon version"
    if arch == ARM64:
        return "Architecture: Apple Silicon (native)"
    return f"Architecture: Intel ({arch})"


def serial_menu_row() -> tuple:
    """Build the (label, copyable) pair for the tray's device-serial row.

    Pure: no pystray, no TrayIcon, no display backend. That is deliberate —
    pystray's backend binds at import time and fails on a headless CI runner,
    which leaves ``pystray = None`` (see the import guard above) and makes
    ``TrayIcon()`` unconstructable there. A row-building rule that could only be
    exercised through a live TrayIcon would therefore be untestable in the one
    environment that gates merges, and per test-fixture-discipline.md a guard
    that skips where it matters is not a guard.

    ``TrayIcon._serial_menu_item`` is the only consumer and does nothing except
    turn this pair into a MenuItem, so the production menu and the test assert
    on the same string.

    Returns:
        (label, copyable). ``copyable`` is True only when there is a serial to
        copy AND a clipboard tool exists to copy it with.
    """
    serial = get_hardware_serial()

    if serial is None:
        # Explicitly "unavailable", never blank and never the repr "None". A VM,
        # a container or a root-only /sys/class/dmi/id/product_serial genuinely
        # has no readable serial; that must read as an honest absence rather
        # than as a broken app.
        return ("Device serial: unavailable", False)

    # No clipboard tool on this box (a bare Linux session with no
    # wl-copy/xclip/xsel) => not copyable. A menu row that looks clickable and
    # silently does nothing is worse than an inert one, and the label still
    # carries the string the user needs to read off.
    return (f"Device serial: {serial}", clipboard_available())


class TrayState(Enum):
    """Tray icon states."""

    SYNCING = "syncing"  # Green - connected and active
    QUEUED = "queued"  # Yellow - offline, events queued
    QUEUE_WARNING = "queue_warning"  # Orange - queue approaching capacity
    ERROR = "error"  # Red - auth failed or AW not running
    PAUSED = "paused"  # Gray - user paused tracking
    PRIVATE = "private"  # Dark gray - private time, nothing recorded
    # Grey - outside the enforced working-hours window. Distinct from PRIVATE (which
    # the USER declares) because the cause is different and the tooltip must say so:
    # this one is not a choice they made, it is their contracted hours ending. Same
    # effect though — nothing is recorded.
    PRIVATE_HOURS = "private_hours"
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
    TrayState.PRIVATE_HOURS: "#94a3b8",  # Grey - outside working hours, not recording
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
_logo_template_lock = threading.Lock()


def _get_logo_template() -> Optional[Image.Image]:
    """Load and cache the B logo as an alpha mask (thread-safe)."""
    global _logo_template
    if _logo_template is not None:
        return _logo_template
    with _logo_template_lock:
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


def _readable_fg_for(color: str) -> str:
    """Pick a foreground colour with adequate contrast against the disk fill.

    Uses ITU-R BT.601 perceived luminance: bright fills (yellow, light gray,
    amber) get a dark glyph; saturated/dark fills (purple, red, blue) keep
    white. Without this the PAUSED state (#9ca3af, luminance ≈163) and
    QUEUED state (#eab308, luminance ≈176) rendered white-on-light at
    roughly 2:1 contrast — invisible on light-mode macOS menu bars.

    PAUSED sits only ~3 units above the cut-off; if STATE_COLORS ever
    shifts its gray slightly darker the foreground flips back to white,
    which still works against a dark menu bar but loses on a light one.
    Adjust the threshold rather than nudging the palette in that case.

    Accepts colours with or without a leading "#" so future callers can
    pass either form without silently producing the wrong fg.
    """
    hex_str = color.lstrip("#")
    if len(hex_str) != 6:
        return "#FFFFFF"
    try:
        r = int(hex_str[0:2], 16)
        g = int(hex_str[2:4], 16)
        b = int(hex_str[4:6], 16)
    except ValueError:
        return "#FFFFFF"
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return "#1a1a1a" if luminance > 160 else "#FFFFFF"


def create_icon_image(
    color: str,
    size: int = 64,
    now: "Optional[_datetime]" = None,
) -> Image.Image:
    """Create a clock icon with the given state color.

    Solid-filled disk in the state color with hands/ticks/B-glyph drawn in
    a contrast-aware foreground colour. Solid fill is what makes the icon
    readable against macOS dark menu bars — earlier outline-only design was
    nearly invisible (Emilian, 2026-06-10).

    Args:
        color: Hex color code for the clock color
        size: Icon size in pixels
        now: Optional pre-captured datetime so the caller's cache-key
            matches what gets drawn. When omitted we sample fresh, which
            is fine for ad-hoc renders but races with the redraw cache in
            `TrayIcon._set_icon` across minute boundaries.

    Returns:
        PIL Image
    """
    from datetime import datetime

    if now is None:
        now = datetime.now()
    hour = now.hour % 12
    minute = now.minute
    fg = _readable_fg_for(color)

    # Use 4x supersampling for smooth anti-aliased lines
    ss = 4
    big = size * ss
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    cx, cy = big // 2, big // 2
    radius = int(big * 0.46)

    # Solid clock face — this is the part the menu bar actually sees.
    draw.ellipse(
        [cx - radius, cy - radius, cx + radius, cy + radius],
        fill=color,
    )

    # Hour tick marks
    tick_outer = int(radius * 0.92)
    tick_inner_long = int(tick_outer * 0.74)
    tick_inner_short = int(tick_outer * 0.86)
    for i in range(12):
        angle = math.radians(i * 30 - 90)
        inner = tick_inner_long if i % 3 == 0 else tick_inner_short
        x1 = cx + int(inner * math.cos(angle))
        y1 = cy + int(inner * math.sin(angle))
        x2 = cx + int(tick_outer * math.cos(angle))
        y2 = cy + int(tick_outer * math.sin(angle))
        w = max(2, big // 32) if i % 3 == 0 else max(1, big // 48)
        draw.line([x1, y1, x2, y2], fill=fg, width=w)

    # Minute hand
    minute_angle = math.radians(minute * 6 - 90)
    minute_len = int(tick_outer * 0.82)
    draw.line(
        [cx, cy,
         cx + int(minute_len * math.cos(minute_angle)),
         cy + int(minute_len * math.sin(minute_angle))],
        fill=fg,
        width=max(2, big // 32),
    )

    # Hour hand (shorter, thicker)
    hour_angle = math.radians((hour * 30 + minute * 0.5) - 90)
    hour_len = int(tick_outer * 0.55)
    draw.line(
        [cx, cy,
         cx + int(hour_len * math.cos(hour_angle)),
         cy + int(hour_len * math.sin(hour_angle))],
        fill=fg,
        width=max(3, big // 24),
    )

    # "B" letter in the center. Sized so the glyph fits inside the inner
    # tick ring; larger ratios overlap the clock hands at small render sizes
    # (the macOS menu bar asks for 22px).
    font_size = int(radius * 0.85)
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
    draw.text((tx, ty), "B", fill=fg, font=font)

    # Downsample for smooth result
    image = image.resize((size, size), Image.LANCZOS)
    return image


class TrayIcon:
    """System tray icon with status indicator."""

    # Consecutive failed health probes before the tray is declared dead and the
    # agent shuts down. A dead NSStatusItem stays dead, so requiring two ticks
    # costs one tick of latency while stopping a transient AppKit error from
    # ending the user's tracking day.
    _TRAY_HEALTH_FAILURES_TO_DIE = 2

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
        on_start_break: Optional[Callable[[], None]] = None,
        on_end_break: Optional[Callable[[], None]] = None,
        on_cancel_login: Optional[Callable[[], None]] = None,
        on_install_update: Optional[Callable[[str], None]] = None,
        on_check_update: Optional[Callable[[], None]] = None,
        on_tray_died: Optional[Callable[[], None]] = None,
        on_show_hours: Optional[Callable[[], Optional[str]]] = None,
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
            on_start_break: Callback when user starts a manual break
            on_end_break: Callback when user ends break early
            on_cancel_login: Callback when user cancels in-progress login
            on_install_update: Callback when user clicks Install & Restart (receives asset URL)
            on_tray_died: Callback when the tray icon becomes invalid (ghost process detection)
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
        self._on_start_break = on_start_break
        self._on_end_break = on_end_break
        self._on_cancel_login = on_cancel_login
        self._on_install_update = on_install_update
        self._on_check_update = on_check_update
        self._on_tray_died = on_tray_died
        self._on_show_hours = on_show_hours

        # Consecutive failed tray health probes. Incremented by tick_clock on the
        # scheduler thread and reset by the icon-creation paths on the main
        # thread, so every access takes _tray_health_lock: the increment is a
        # read-modify-write, and a reset landing inside one would let a stale
        # count carry onto a fresh status item and kill it a tick early. See
        # _TRAY_HEALTH_FAILURES_TO_DIE for why the counter exists.
        self._tray_health_lock = threading.Lock()
        self._tray_health_failures = 0

        # Whether the tray has begun its lifecycle — run_blocking()/start() has
        # been entered and is responsible for a live status item. Until then a
        # None status item means "not started yet", NOT "died": the 60s tick
        # calls tick_clock from the moment background startup runs, while the
        # one-time privacy notice still blocks run_blocking on the main thread,
        # so counting those ticks force-exited a HEALTHY agent mid-notice (the
        # ghost-process crash reproduced 2026-07-23). Set once, never cleared.
        self._tray_started = False

        self.model = TrayModel()

        self._icon: Optional[pystray.Icon] = None
        self._thread: Optional[threading.Thread] = None

        # Menu debounce: rebuild at most once per second
        self._menu_debounce_interval = 1.0
        self._menu_last_rebuild: float = 0.0
        self._menu_pending: bool = False
        self._menu_lock = threading.Lock()

        # macOS main-thread proxy for menu rebuilds (lazily bound to pystray icon)
        self._menu_proxy: object | None = None

        # Sync Now cooldown (5 seconds)
        self._sync_now_cooldown = 5.0
        self._sync_now_last: float = 0.0
        self._sync_now_lock = threading.Lock()

        # Icon rendering cache: skip PIL + NSImage when (hour, minute, color) unchanged
        self._last_icon_key: Optional[tuple] = None
        self._last_icon_image: Optional[Image.Image] = None

        # Menu snapshot cache: skip rebuild when model state unchanged
        self._last_menu_hash: Optional[int] = None

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
                "sync_interval": self.model.sync_interval,
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
                "needs_permissions": self.model.needs_permissions,
                "input_monitoring_ok": self.model.input_monitoring_ok,
            }

    def _create_menu(self) -> pystray.Menu:
        """Create the tray menu.

        Takes a snapshot of all model fields under lock, then builds the
        menu from the snapshot without holding the lock.
        """
        s = self._snapshot_model()
        items = []
        logged_in = s["user_email"] is not None

        # ── Permission warning (macOS Input Monitoring) ─────
        # Surfaced at the very top so a disabled keystroke/click capture — which
        # makes the day look like a jiggler server-side — is impossible to miss.
        if not s["input_monitoring_ok"]:
            items.append(Item(
                "⚠️  Input tracking off — Fix Permissions",
                self._handle_fix_permissions,
            ))
            items.append(Item("─" * 20, None, enabled=False))

        # ── User identity & status ──────────────────────────
        if s["user_email"]:
            if s["user_name"] and s["user_name"] != s["user_email"]:
                label = f"{s['user_name']} ({s['user_email']})"
            else:
                label = s["user_email"]
            items.append(Item(label, None, enabled=False))

        items.append(Item(f"App status: {self._get_status_text(s)}", None, enabled=False))
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
            stop_label = f"  Stop ({s['hours_today']})"
            items.append(Item(
                stop_label,
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
            Item("─" * 20, None, enabled=False),
            self._serial_menu_item(),
            self._arch_menu_item(),
            Item("Privacy Policy", self._handle_open_privacy),
        ]
        items.append(Item("Diagnostics", pystray.Menu(*diag_items), enabled=logged_in))
        items.append(Item("Sync Now", self._handle_sync_now, enabled=logged_in))

        # ── Preferences submenu ─────────────────────────────
        pref_items = [
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
            Item("─" * 20, None, enabled=False),
            Item("Export Logs", self._handle_export_logs),
            Item("Open Config File", self._handle_open_config),
            Item("─" * 20, None, enabled=False),
        ]

        # Update items inside Preferences
        if s["update_in_progress"]:
            pref_items.append(Item(s["update_status"] or "Updating...", None, enabled=False))
        elif s["update_version"]:
            if s["update_asset_url"]:
                pref_items.append(Item(
                    f"Install v{s['update_version']} & Restart",
                    self._handle_install_update,
                ))
            else:
                pref_items.append(Item(
                    f"Update available (v{s['update_version']})",
                    self._handle_open_update,
                ))
        else:
            pref_items.append(Item("Check for Update", self._handle_check_update))

        pref_items.append(Item(f"v{_RELEASE_VERSION} ({BUILD_DATE})", None, enabled=False))

        items.append(Item("Preferences", pystray.Menu(*pref_items), enabled=logged_in))

        items.append(Item("─" * 20, None, enabled=False))

        # ── Account ─────────────────────────────────────────
        if s["user_email"]:
            items.append(Item("Log Out", self._handle_logout))
        elif s["state"] == TrayState.WAITING_AUTH:
            items.append(Item("Cancel Login", self._handle_cancel_login))
            items.append(Item("Retry Login", self._handle_login))
        else:
            items.append(Item("Login", self._handle_login))

        items.append(Item("Quit", self._handle_quit))

        return pystray.Menu(*items)

    def _arch_menu_item(self) -> "Item":
        """The architecture row: which build of BetterFlow is actually running.

        Sits in Diagnostics because it answers a question users could not
        otherwise answer about their own machine — an Intel build runs happily
        on Apple Silicon through Rosetta, so there is no symptom to notice and
        no way to tell from the app which one was installed. Before this row the
        only answer was `lipo -archs` on the bundle.

        The label lives in the module-level arch_menu_label() so it can be
        asserted without a live pystray backend; this method only wraps it in a
        MenuItem. Do not re-derive the label here — a second copy is how the test
        and the menu drift apart.

        arch_menu_label() reads the memoised probe in src/machine_arch.py, so
        rebuilding the menu on every state change costs nothing — the row must
        never fork sysctl under _menu_lock on the sync cycle.
        """
        return Item(arch_menu_label(), None, enabled=False)

    def _serial_menu_item(self) -> "Item":
        """The device-serial row: readable value, copyable when we can copy.

        Sits in Diagnostics next to the Privacy Policy link on purpose. The agent
        reports this serial to the server, so the honest thing is to show the
        user the exact value we hold about their machine rather than describe it
        in a first-run wizard they clicked through months ago. Seeing the literal
        string also makes it self-evident that this identifies the hardware and
        not them.

        The label/copyable decision lives in the module-level serial_menu_row()
        so it can be asserted without a live pystray backend; this method only
        wraps that pair in a MenuItem. Do not re-derive the label here — a
        second copy is how the test and the menu drift apart.

        serial_menu_row() reads the cached probe from src/hardware_serial.py,
        the same call site the heartbeat uses, memoised for the life of the
        process, so rebuilding the menu on every state change costs nothing.
        """
        label, copyable = serial_menu_row()
        if not copyable:
            return Item(label, None, enabled=False)
        return Item(label, self._handle_copy_serial)

    def _handle_copy_serial(self, icon, item) -> None:
        """Copy the device serial to the clipboard, with a brief confirmation."""
        serial = get_hardware_serial()
        if serial is None:
            return
        if copy_to_clipboard(serial):
            send_notification("BetterFlow", f"Device serial {serial} copied.")
        else:
            # Don't claim success we didn't get. The value is still on screen.
            send_notification(
                "BetterFlow", f"Could not copy. Your device serial is {serial}."
            )

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
        elif state == TrayState.WAITING_AUTH:
            return "Waiting for login..."
        elif state == TrayState.NEEDS_PERMISSIONS:
            return "Permissions needed"
        elif state == TrayState.PRIVATE_HOURS:
            # Without this branch a user outside their enforced working-hours
            # window read "App status: Starting...", which looks like a hung app
            # rather than a deliberate not-recording state.
            return "Outside working hours"
        elif state == TrayState.ON_BREAK:
            # Normally covered by the on_break flag above; this catches the state
            # being set without the model flag, which also fell through to
            # "Starting...".
            return "On Break"
        elif state == TrayState.PRIVATE:
            # Same gap as ON_BREAK: normally covered by the private_mode flag,
            # but the state alone must not read as "Starting...".
            return "Private Time"
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

    def _handle_fix_permissions(self, icon, item) -> None:
        """Register the app for Input Monitoring and open its System Settings pane.

        input_monitoring_active(prompt=True) calls IOHIDRequestAccess, which
        lists BetterFlow under Input Monitoring so there's a toggle to flip; the
        60s permission re-check clears the warning once the user enables it.
        """
        try:
            from .permissions import (
                input_monitoring_active,
                open_input_monitoring_settings,
            )
        except ImportError:
            from permissions import (  # type: ignore[no-redef]
                input_monitoring_active,
                open_input_monitoring_settings,
            )
        try:
            input_monitoring_active(prompt=True)
            open_input_monitoring_settings()
        except Exception as e:
            logger.warning("Failed to open Input Monitoring settings: %s", e)

    def _handle_login(self, icon, item) -> None:
        """Handle login menu click."""
        if self._on_login:
            self._on_login()

    def _handle_cancel_login(self, icon, item) -> None:
        """Handle cancel login menu click."""
        if self._on_cancel_login:
            self._on_cancel_login()

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
        # Let the callback set the authoritative tray state (it calls
        # set_paused / set_state), avoiding a race where we set PRIVATE
        # here and the callback immediately overwrites with PAUSED.
        if self._on_private_toggle:
            self._on_private_toggle(new_mode)
        else:
            # No callback: set state ourselves
            if new_mode:
                self.set_state(TrayState.PRIVATE)
            elif on_break:
                self.set_state(TrayState.ON_BREAK)
            elif paused:
                self.set_state(TrayState.PAUSED)
            else:
                self.set_state(TrayState.SYNCING)
        self._update_menu()

    def _handle_show_dashboard(self, icon, item) -> None:
        """Open the web dashboard in the browser.

        Prefer a one-time authenticated URL (so multi-Google-account users skip
        the login wall); fall back to the plain /agent/my URL if minting fails
        or no callback is wired. The mint hits the network, so it runs off the
        tray thread to keep the menu responsive.
        """
        with self.model.lock:
            fallback_url = self.model.dashboard_url
        on_show_hours = self._on_show_hours

        def worker() -> None:
            url = fallback_url
            if on_show_hours is not None:
                try:
                    authenticated = on_show_hours()
                    if authenticated:
                        url = authenticated
                except Exception:
                    logger.exception(
                        "Show My Hours: failed to mint authenticated URL; "
                        "opening plain dashboard instead"
                    )
            webbrowser.open(url)

        threading.Thread(target=worker, name="show-my-hours", daemon=True).start()

    def _handle_open_privacy(self, icon, item) -> None:
        """Open the public privacy policy in the user's default browser.

        Surfaced from Diagnostics so the disclosure stays one click away
        from any session, not just the first-run permission gate.
        """
        webbrowser.open(PRIVACY_POLICY_URL)

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

    def _handle_check_update(self, icon, item) -> None:
        """Trigger a manual update check."""
        if self._on_check_update:
            self._on_check_update()

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
        else:
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
            status_text: Optional status message for error state. Passing None
                explicitly when transitioning OUT of an error/queue state
                clears the previous text so it doesn't leak across transitions.
        """
        with self.model.lock:
            previous_state = self.model.state
            self.model.state = state
            if status_text is not None:
                self.model.status_text = status_text
            elif previous_state in (TrayState.ERROR, TrayState.QUEUE_WARNING) and state not in (
                TrayState.ERROR, TrayState.QUEUE_WARNING,
            ):
                # Recovery transition: ERROR/QUEUE_WARNING → anything-healthy.
                # Old status_text (e.g. "ActivityWatch is not running") would
                # leak into the next display path until something else writes
                # to it. Clear at the transition so the recovery is clean.
                self.model.status_text = None
        self._update_icon()
        # Also refresh the menu so the "App status: …" line flips with the
        # state, not on the next tick when something else triggers a menu
        # rebuild (the recurring "tray stuck on Error" UX bug Tudor saw
        # multiple times today on bf-data-service transient blips).
        self._update_menu()

    def set_paused(self, paused: bool) -> None:
        """Set paused state."""
        with self.model.lock:
            self.model.paused = paused
            on_break = self.model.on_break
            private = self.model.private_mode
        if paused:
            self.set_state(TrayState.PAUSED)
        elif on_break:
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
            else:
                # Clear current project when none is selected or list is emptied
                self.model.current_project = None
                self.model.project_started_at = None
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
        self._update_tooltip(f"{_BASE_TOOLTIP} - Today: {hours}h {minutes}m active")
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

    def _check_tray_health(self) -> bool:
        """Check whether the tray icon is still alive.

        On macOS, the NSStatusItem can be deallocated while the NSApplication
        run loop keeps spinning, creating a ghost process with no visible icon.
        Returns True if healthy, False if the icon is dead.
        """
        icon = self._icon
        if icon is None:
            return False

        if platform.system() == "Darwin":
            try:
                status_item = getattr(icon, "_status_item", None)
                if status_item is None:
                    return False
                button = status_item.button()
                if button is None:
                    return False
            except Exception as e:
                # A False here shuts the whole agent down via _on_tray_died, so
                # never swallow the reason — an unexplained mid-day exit with a
                # bare "Tray icon is dead" line is undiagnosable in the field.
                logger.warning("Tray health probe failed: %s", e, exc_info=True)
                return False

        return True

    def tick_clock(self) -> None:
        """Called periodically to advance the clock-face icon hands."""
        if not self._tray_started:
            # The tray has not begun its lifecycle yet — run_blocking()/start()
            # has not created a status item. A None status item here means "not
            # started" (e.g. the one-time privacy notice is still blocking the
            # main thread), never "died", so a failing probe must not accrue
            # toward death. Without this gate the 60s tick force-exited a healthy
            # agent ~2 minutes into an open notice window (reproduced 2026-07-23).
            return
        if not self._check_tray_health():
            # Require consecutive failures before declaring death: this path
            # shuts the agent down and stops capture for the rest of the day,
            # and a single transient AppKit/pyobjc hiccup is not proof the
            # status item was deallocated. A genuinely dead icon stays dead and
            # trips on the next tick.
            with self._tray_health_lock:
                self._tray_health_failures += 1
                failures = self._tray_health_failures
            if failures < self._TRAY_HEALTH_FAILURES_TO_DIE:
                logger.warning(
                    "Tray health check failed (%d/%d) — retrying next tick",
                    failures,
                    self._TRAY_HEALTH_FAILURES_TO_DIE,
                )
                return
            logger.error("Tray icon is dead — triggering shutdown")
            if self._on_tray_died:
                self._on_tray_died()
            return
        with self._tray_health_lock:
            self._tray_health_failures = 0
        self._update_icon()

    def _update_icon(self) -> None:
        """Update the tray icon image and menu.

        Skips the expensive PIL render + NSImage conversion when the clock
        hands and state color haven't changed since the last call.

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

        from datetime import datetime as _dt
        _now = _dt.now()
        icon_key = (_now.hour % 12, _now.minute, color)

        if icon_key != self._last_icon_key:
            # Pass the captured _now so the rendered hands match the cache
            # key — otherwise create_icon_image samples datetime.now() a
            # second time and a minute roll-over between the two calls
            # leaves the icon stale until the next tick.
            new_image = create_icon_image(color, now=_now)
            self._last_icon_key = icon_key
            self._last_icon_image = new_image

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

        Wrapped in an autorelease pool so NSData/NSImage temporaries
        created on background threads are freed promptly.
        """
        import AppKit
        import Foundation
        import io
        import objc

        with objc.autorelease_pool():
            thickness = 22
            try:
                thickness = max(int(self._icon._status_bar.thickness()), 22)
            except Exception as e:
                logger.debug("status bar thickness probe failed: %s", e)

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

    @staticmethod
    def _make_hashable(obj: object) -> object:
        """Convert a snapshot value to a hashable form for change detection."""
        if isinstance(obj, dict):
            return tuple(sorted((k, TrayIcon._make_hashable(v)) for k, v in obj.items()))
        if isinstance(obj, list):
            return tuple(TrayIcon._make_hashable(i) for i in obj)
        return obj

    def _update_menu(self) -> None:
        """Update the tray menu (debounced: max once per second).

        Skips the rebuild entirely if the model snapshot hasn't changed.
        """
        icon = self._icon
        if not icon:
            return

        # Snapshot and hash — must be checked inside _menu_lock to prevent
        # two threads from both passing the hash check concurrently.
        snap = self._snapshot_model()
        snap_hash = hash(TrayIcon._make_hashable(snap))

        now = time.monotonic()
        with self._menu_lock:
            if snap_hash == self._last_menu_hash:
                return
            self._last_menu_hash = snap_hash

            elapsed = now - self._menu_last_rebuild
            if elapsed >= self._menu_debounce_interval:
                self._menu_last_rebuild = now
                self._menu_pending = False
                self._apply_menu(self._create_menu())
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
                self._apply_menu(self._create_menu())

    def _apply_menu(self, new_menu: "pystray.Menu") -> None:
        """Apply a pystray menu, dispatching to the main thread on macOS.

        AppKit is not thread-safe; building NSMenu items and calling
        NSStatusItem.setMenu: from a background thread causes re-entrant
        ffi_closure recursion that hangs the app.
        """
        if not self._icon:
            return

        if platform.system() != "Darwin" or threading.current_thread() is threading.main_thread():
            self._icon.menu = new_menu
            return

        # Background thread on macOS: set the menu descriptor (pure Python,
        # thread-safe) and dispatch the NSMenu rebuild to the main thread.
        self._icon._menu = new_menu
        try:
            self._dispatch_menu_rebuild()
        except Exception:
            logger.debug("Main-thread menu dispatch failed, falling back", exc_info=True)
            self._icon.menu = new_menu

    def _dispatch_menu_rebuild(self) -> None:
        """Schedule pystray's _update_menu on the main run-loop via NSTimer."""
        import Foundation

        if self._menu_proxy is None and _MenuProxy is not None:
            self._menu_proxy = _MenuProxy.alloc().init()
        proxy = self._menu_proxy
        if proxy is None:
            self._icon._update_menu()
            return
        proxy._pystray_icon = self._icon
        timer = Foundation.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            0.0, proxy, b"rebuildMenu:", None, False,
        )
        Foundation.NSRunLoop.mainRunLoop().addTimer_forMode_(
            timer, Foundation.NSDefaultRunLoopMode,
        )

    def start(self) -> None:
        """Start the tray icon in a background thread."""
        if self._icon is not None:
            return

        # The tray's lifecycle has begun (see run_blocking): a None status item
        # from here on is a real fault, not the not-yet-started state.
        self._tray_started = True

        # A fresh icon gets a fresh streak: stop() nulls _icon, so a tick landing
        # in a stop→start window (logout/re-login) counts a failure that has
        # nothing to do with the new status item.
        with self._tray_health_lock:
            self._tray_health_failures = 0

        from datetime import datetime as _dt
        color = STATE_COLORS[self.model.state]
        # Pin the initial render's clock-time to a captured `now` for the
        # same reason _update_icon does — keeps the first paint and the
        # first tick's cache key on the same minute.
        self._icon = pystray.Icon(
            "BetterFlow",
            create_icon_image(color, now=_dt.now()),
            _BASE_TOOLTIP,
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
        # The tray's lifecycle has begun: from here a None status item is a
        # failure to keep alive, not the not-yet-started state tick_clock skips.
        self._tray_started = True

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            if self._icon is None:
                # Fresh icon, fresh streak — same reason as start(): failures
                # counted against the previous (or not-yet-created) status item
                # must not carry into the new one and shorten its life.
                with self._tray_health_lock:
                    self._tray_health_failures = 0
                color = STATE_COLORS[self.model.state]
                self._icon = pystray.Icon(
                    "BetterFlow",
                    create_icon_image(color),
                    _BASE_TOOLTIP,
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

            start = time.monotonic()
            logger.info("Tray event loop starting (attempt %d/%d)", attempt, max_retries)
            if sys.platform == "darwin":
                # macOS: pystray's default setup flips visible=True on a
                # background thread, and AppKit calls off the main thread
                # silently fail inside PyInstaller .app bundles. So make the
                # icon visible on THIS (main) thread before the loop and pass
                # a no-op setup.
                self._icon.visible = True
                self._icon.run(setup=lambda icon: None)
            else:
                # Windows/Linux: the tray icon's native handle isn't created
                # until run() starts, so setting visible beforehand is a no-op
                # and the icon never appears at all. Make it visible from the
                # setup callback, which pystray invokes once the backend is
                # ready — the supported way to show an icon on launch.
                self._icon.run(setup=lambda icon: setattr(icon, "visible", True))
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
