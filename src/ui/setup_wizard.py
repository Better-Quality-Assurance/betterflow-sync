"""First-run setup wizard for BetterFlow.

A polished onboarding wizard: Welcome → Browser login → Success.
Runs only when config.setup_complete is False.
"""

import logging
import platform
import sys
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import itertools

from PIL import Image, ImageTk

try:
    from ..auth.login import LoginManager, LoginState
    from ..config import Config
    from .permissions import check_accessibility, open_accessibility_settings
except ImportError:
    from auth.login import LoginManager, LoginState
    from config import Config
    from ui.permissions import check_accessibility, open_accessibility_settings

logger = logging.getLogger(__name__)

WINDOW_WIDTH = 720
WINDOW_HEIGHT = 520

# Typography — platform-conditional font family
if platform.system() == "Darwin":
    FONT_FAMILY = "Avenir Next"
elif platform.system() == "Windows":
    FONT_FAMILY = "Segoe UI"
else:
    FONT_FAMILY = "sans-serif"
FONT_TITLE = (FONT_FAMILY, 30, "bold")
FONT_SUBTITLE = (FONT_FAMILY, 13)
FONT_BODY = (FONT_FAMILY, 12)
FONT_SMALL = (FONT_FAMILY, 11)
FONT_BUTTON = (FONT_FAMILY, 13, "bold")

# Layout
BTN_HEIGHT = 46
BTN_BORDER_RADIUS = 13
SPINNER_RADIUS = 30
SPINNER_FRAME_MS = 50

# Colors — BetterFlow brand palette
BG_COLOR = "#0f0a1a"
CARD_COLOR = "#1a1028"
CARD_BORDER = "#3d2d6b"
ACCENT_COLOR = "#2a1d45"
PRIMARY_COLOR = "#7D69B8"
PRIMARY_HOVER = "#614D87"
TEXT_COLOR = "#f4f0ff"
TEXT_MUTED = "#b8a8d6"
SUCCESS_COLOR = "#368a5e"
ERROR_COLOR = "#c96660"
BTN_TEXT = "#ffffff"


def _resources_dir() -> Path:
    """Return the resources directory (works for dev and PyInstaller)."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "resources"
    return Path(__file__).resolve().parent.parent.parent / "resources"


@dataclass
class SetupResult:
    """Result from the setup wizard."""

    completed: bool = False
    logged_in: bool = False
    login_state: Optional[LoginState] = None


class SetupWizard:
    """First-run setup wizard window."""

    def __init__(self, config: Config, login_manager: LoginManager):
        self._config = config
        self._login_manager = login_manager
        self._result = SetupResult()
        self._window: Optional[tk.Tk] = None
        self._login_state: Optional[LoginState] = None
        self._closing = False
        self._spinner_after_id: Optional[str] = None
        self._permissions_refresh_id: Optional[str] = None
        self._button_id = itertools.count(1)

    def show(self) -> SetupResult:
        """Show the wizard and return result when closed."""
        self._window = tk.Tk()
        self._window.title("BetterFlow")
        self._window.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self._window.resizable(False, False)
        self._window.configure(bg=BG_COLOR)
        if self._window.tk.call("tk", "windowingsystem") == "aqua":
            try:
                # Prefer light native titlebar appearance on macOS.
                self._window.tk.call(
                    "::tk::unsupported::MacWindowStyle",
                    "appearance",
                    self._window._w,
                    "aqua",
                )
            except tk.TclError:
                pass

        # Center on screen
        self._window.update_idletasks()
        x = (self._window.winfo_screenwidth() - WINDOW_WIDTH) // 2
        y = (self._window.winfo_screenheight() - WINDOW_HEIGHT) // 2
        self._window.geometry(f"+{x}+{y}")

        # Handle window close
        self._window.protocol("WM_DELETE_WINDOW", self._on_close)

        # Main canvas for custom drawing
        self._canvas = tk.Canvas(
            self._window,
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            bg=BG_COLOR,
            highlightthickness=0,
        )
        self._canvas.pack(fill=tk.BOTH, expand=True)

        self._show_welcome()

        self._window.mainloop()
        return self._result

    def _clear(self) -> None:
        """Clear all canvas items and widgets."""
        # Stop any pending spinner callback before clearing canvas items.
        if self._spinner_after_id is not None:
            try:
                self._window.after_cancel(self._spinner_after_id)
            except tk.TclError:
                pass
            self._spinner_after_id = None
        if self._permissions_refresh_id is not None:
            try:
                self._window.after_cancel(self._permissions_refresh_id)
            except tk.TclError:
                pass
            self._permissions_refresh_id = None
        self._canvas.configure(cursor="")
        self._canvas.delete("all")
        for widget in self._canvas.winfo_children():
            widget.destroy()

    def _on_close(self) -> None:
        """Handle window close — cancel any in-progress login."""
        self._closing = True
        if self._permissions_refresh_id is not None:
            try:
                self._window.after_cancel(self._permissions_refresh_id)
            except tk.TclError:
                pass
            self._permissions_refresh_id = None
        self._result = SetupResult(completed=False)
        self._login_manager.cancel_login()
        self._window.destroy()

    def _make_button(self, text: str, command: callable, x: int, y: int, width: int = 248, primary: bool = True) -> str:
        """Create a cross-platform canvas button (macOS tk.Button ignores custom bg)."""
        bg = PRIMARY_COLOR if primary else ACCENT_COLOR
        hover_bg = PRIMARY_HOVER if primary else "#362654"
        border = "#B57EF5" if primary else "#4a3875"
        text_color = BTN_TEXT
        x1, y1 = x - (width // 2), y - (BTN_HEIGHT // 2)
        x2, y2 = x + (width // 2), y + (BTN_HEIGHT // 2)
        tag = f"btn_{next(self._button_id)}"

        rect_id = self._create_rounded_rect(
            x1, y1, x2, y2, radius=BTN_BORDER_RADIUS, fill=bg, outline=border, width=1, tags=(tag, "btn")
        )
        self._canvas.create_text(
            x, y, text=text, font=FONT_BUTTON, fill=text_color, tags=(tag, "btn")
        )

        def on_enter(_event):
            self._canvas.itemconfigure(rect_id, fill=hover_bg)
            self._canvas.configure(cursor="hand2")

        def on_leave(_event):
            self._canvas.itemconfigure(rect_id, fill=bg)
            self._canvas.configure(cursor="")

        self._canvas.tag_bind(tag, "<Enter>", on_enter)
        self._canvas.tag_bind(tag, "<Leave>", on_leave)
        # Defer scene-changing command to next event-loop turn.
        # Tk 9 can crash if canvas items are deleted while click handlers are active.
        self._canvas.tag_bind(tag, "<Button-1>", lambda _event: self._window.after(1, command))
        return tag

    def _create_rounded_rect(self, x1: int, y1: int, x2: int, y2: int, radius: int = 10, **kwargs) -> int:
        """Draw a rounded rectangle on canvas and return item id."""
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        return self._canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)

    def _draw_background(self) -> None:
        """Draw atmospheric gradient-like background."""
        self._canvas.create_rectangle(0, 0, WINDOW_WIDTH, WINDOW_HEIGHT, fill=BG_COLOR, outline="")

        # Layered ambient bands for depth — purple tones.
        bands = [
            ("#150e24", 0, 0, WINDOW_WIDTH, 120),
            ("#120b20", 0, 120, WINDOW_WIDTH, 280),
            ("#0e081a", 0, 280, WINDOW_WIDTH, WINDOW_HEIGHT),
        ]
        for color, x1, y1, x2, y2 in bands:
            self._canvas.create_rectangle(x1, y1, x2, y2, fill=color, outline="")

        # Soft purple glows.
        self._canvas.create_oval(-120, -110, 230, 170, fill="#1e1340", outline="")
        self._canvas.create_oval(460, 280, 860, 650, fill="#18103a", outline="")

    def _draw_card_shell(self) -> tuple[int, int, int, int]:
        """Return content bounds; the window itself is the only card surface."""
        return 56, 34, WINDOW_WIDTH - 56, WINDOW_HEIGHT - 34

    def _draw_scene(self, title: str, subtitle: str) -> int:
        """Shared wizard scene shell. Returns content center x."""
        self._clear()
        self._draw_background()
        self._draw_card_shell()
        cx = WINDOW_WIDTH // 2
        self._canvas.create_text(
            cx, 126,
            text=title,
            font=FONT_TITLE,
            fill=TEXT_COLOR,
        )
        self._canvas.create_text(
            cx, 160,
            text=subtitle,
            font=FONT_SUBTITLE,
            fill=TEXT_MUTED,
        )
        return cx

    # ── Welcome Screen ───────────────────────────────────────────────

    def _show_welcome(self) -> None:
        """Show the welcome screen."""
        cx = self._draw_scene(
            title="Welcome to BetterFlow",
            subtitle="Install local tracking and connect your BetterFlow account",
        )

        # Logo image
        logo_y = 244
        logo_path = _resources_dir() / "logo.png"
        if logo_path.exists():
            logo_img = Image.open(logo_path).convert("RGBA")
            logo_img = logo_img.resize((80, 80), Image.LANCZOS)
            self._logo_photo = ImageTk.PhotoImage(logo_img)
            self._canvas.create_image(cx, logo_y, image=self._logo_photo)
        else:
            # Fallback: text logo
            r = 44
            self._canvas.create_oval(
                cx - r, logo_y - r, cx + r, logo_y + r,
                fill=PRIMARY_COLOR, outline=""
            )
            self._canvas.create_text(
                cx, logo_y,
                text="B",
                font=(FONT_FAMILY, 32, "bold"),
                fill="#ffffff",
            )

        # Description
        self._canvas.create_text(
            cx, 332,
            text=(
                "Runs in your menu bar, captures activity on-device,\n"
                "and syncs private summaries to BetterFlow."
            ),
            font=FONT_BODY,
            fill=TEXT_MUTED,
            justify=tk.CENTER,
        )

        self._canvas.create_text(
            cx, 384,
            text="The next step opens your browser for secure sign-in.",
            font=FONT_SMALL,
            fill="#9a87c4",
            justify=tk.CENTER,
        )

        self._make_button("Install and Connect", self._start_login, cx, 432, width=276)

    # ── Signing In Screen ────────────────────────────────────────────

    def _start_login(self) -> None:
        """Show signing in state and open browser."""
        cx = self._draw_scene(
            title="Installing and Connecting",
            subtitle="Preparing local services and opening secure browser sign-in",
        )

        # Spinner circle
        self._spinner_angle = 0
        self._draw_spinner(cx, 250)

        # Subtitle
        self._status_id = self._canvas.create_text(
            cx, 332,
            text="Your browser is opening now. Complete sign-in there.",
            font=FONT_BODY,
            fill=TEXT_MUTED,
            justify=tk.CENTER,
        )

        # Retry button (hidden initially, placed for later use)
        self._retry_btn = None

        # Start login in background
        def do_login():
            state = self._login_manager.login_via_browser()
            if self._closing:
                return
            try:
                self._window.after(0, lambda: self._on_login_complete(state))
            except tk.TclError:
                pass

        threading.Thread(target=do_login, daemon=True).start()

        # Start spinner animation
        self._animate_spinner(cx, 250)

    def _draw_spinner(self, cx: int, cy: int) -> None:
        """Draw the spinner arc."""
        r = SPINNER_RADIUS
        self._canvas.delete("spinner")
        self._canvas.create_arc(
            cx - r, cy - r, cx + r, cy + r,
            start=self._spinner_angle,
            extent=80,
            style=tk.ARC,
            outline=PRIMARY_COLOR,
            width=6,
            tags="spinner",
        )
        # Background ring (reuse r from above)
        self._canvas.create_arc(
            cx - r, cy - r, cx + r, cy + r,
            start=0,
            extent=359,
            style=tk.ARC,
            outline="#3d2d6b",
            width=3,
            tags="spinner_bg",
        )
        # Bring spinner to front
        self._canvas.tag_raise("spinner")

    def _animate_spinner(self, cx: int, cy: int) -> None:
        """Animate the spinner."""
        if not self._canvas.winfo_exists():
            return
        try:
            self._canvas.delete("spinner")
            self._spinner_angle = (self._spinner_angle + 10) % 360
            r = SPINNER_RADIUS
            self._canvas.create_arc(
                cx - r, cy - r, cx + r, cy + r,
                start=self._spinner_angle,
                extent=80,
                style=tk.ARC,
                outline=PRIMARY_COLOR,
                width=6,
                tags="spinner",
            )
            self._spinner_after_id = self._window.after(
                SPINNER_FRAME_MS, lambda: self._animate_spinner(cx, cy)
            )
        except tk.TclError:
            pass

    def _on_login_complete(self, state: LoginState) -> None:
        """Handle login result."""
        # Stop spinner
        if self._spinner_after_id is not None:
            self._window.after_cancel(self._spinner_after_id)

        if state.logged_in:
            self._login_state = state
            self._show_success(state.user_email or "")
        else:
            self._show_error(state.error or "Login failed")

    def _show_error(self, error: str) -> None:
        """Show error state with retry."""
        cx = self._draw_scene(
            title="Connection Problem",
            subtitle="We could not complete setup",
        )

        # Error icon
        r = 35
        self._canvas.create_oval(
            cx - r, 250 - r, cx + r, 250 + r,
            fill=ERROR_COLOR, outline=""
        )
        self._canvas.create_text(
            cx, 250,
            text="!",
            font=(FONT_FAMILY, 34, "bold"),
            fill=BTN_TEXT,
        )

        self._canvas.create_text(
            cx, 332,
            text=error,
            font=FONT_BODY,
            fill=TEXT_MUTED,
        )

        self._make_button("Try Again", self._start_login, cx, 430, width=220)

    # ── Success Screen ───────────────────────────────────────────────

    def _show_success(self, email: str) -> None:
        """Show success screen."""
        cx = self._draw_scene(
            title="You’re All Set",
            subtitle="BetterFlow is ready to run",
        )

        # Success checkmark circle
        r = 35
        self._canvas.create_oval(
            cx - r, 240 - r, cx + r, 240 + r,
            fill=SUCCESS_COLOR, outline=""
        )
        self._canvas.create_text(
            cx, 238,
            text="\u2713",
            font=(FONT_FAMILY, 33, "bold"),
            fill=BTN_TEXT,
        )

        # Email
        if email:
            self._canvas.create_text(
                cx, 304,
                text=f"Signed in as {email}",
                font=(FONT_FAMILY, 12, "bold"),
                fill=SUCCESS_COLOR,
            )

        # Description
        self._canvas.create_text(
            cx, 352,
            text=(
                "BetterFlow will now run in your menu bar.\n"
                "It will automatically track and sync your activity."
            ),
            font=FONT_BODY,
            fill=TEXT_MUTED,
            justify=tk.CENTER,
        )

        # Launch button — go to permissions screen next
        self._make_button("Continue", self._show_permissions_entry, cx, 438, width=280)

    # ── Permissions Screen ─────────────────────────────────────────

    def _show_permissions_entry(self) -> None:
        """Entry point from success screen — skip entirely if already granted."""
        if platform.system() != "Darwin":
            self._finish()
            return
        if check_accessibility():
            self._finish()
            return
        self._show_permissions()

    def _show_permissions(self) -> None:
        """Show the Accessibility permission screen."""
        granted = check_accessibility()

        cx = self._draw_scene(
            title="Accessibility Permission",
            subtitle="BetterFlow needs this to track active window titles",
        )

        row_width = 560
        row_height = 76
        row_x1 = cx - row_width // 2
        row_x2 = cx + row_width // 2

        # ── Accessibility row ──
        row_y = 230
        cy = row_y + row_height // 2
        icon = "\u2713" if granted else "\u2717"
        icon_color = SUCCESS_COLOR if granted else ERROR_COLOR
        bg = "#1a1430" if granted else "#1a1028"
        border = "#3d5040" if granted else CARD_BORDER

        self._create_rounded_rect(
            row_x1, row_y, row_x2, row_y + row_height,
            radius=12, fill=bg, outline=border, width=1,
        )
        self._canvas.create_text(
            row_x1 + 32, cy, text=icon,
            font=(FONT_FAMILY, 22, "bold"), fill=icon_color,
        )
        self._canvas.create_text(
            row_x1 + 64, cy - 10, text="Accessibility",
            font=(FONT_FAMILY, 14, "bold"), fill=TEXT_COLOR, anchor=tk.W,
        )
        self._canvas.create_text(
            row_x1 + 64, cy + 14,
            text="Allow BetterFlow to read window titles",
            font=FONT_SMALL, fill=TEXT_MUTED, anchor=tk.W,
        )
        if not granted:
            self._make_button(
                "Open Settings", self._open_accessibility,
                row_x2 - 82, cy, width=128, primary=False,
            )

        # ── Bottom area ──
        if granted:
            self._canvas.create_text(
                cx, row_y + row_height + 40,
                text="Permission granted!",
                font=(FONT_FAMILY, 14, "bold"), fill=SUCCESS_COLOR,
            )
            self._make_button(
                "Start Using BetterFlow", self._finish,
                cx, row_y + row_height + 88, width=280,
            )
        else:
            self._canvas.create_text(
                cx, row_y + row_height + 34,
                text="If already toggled on, try switching it off and on again.",
                font=(FONT_FAMILY, 11), fill="#8a7ab0",
            )
            btn_y = row_y + row_height + 82
            self._make_button(
                "Refresh Status", self._show_permissions,
                cx - 104, btn_y, width=180,
            )
            self._make_button(
                "Skip for Now", self._finish,
                cx + 104, btn_y, width=180, primary=False,
            )

        # Auto-refresh every 3 seconds (only while waiting for grant)
        if not granted:
            self._permissions_refresh_id = self._window.after(
                3000, self._auto_refresh_permissions,
            )

    def _auto_refresh_permissions(self) -> None:
        """Auto-refresh the permissions screen when status changes."""
        if self._closing:
            return
        try:
            if check_accessibility():
                self._show_permissions()
            else:
                self._permissions_refresh_id = self._window.after(
                    3000, self._auto_refresh_permissions,
                )
        except tk.TclError:
            pass

    def _open_accessibility(self) -> None:
        """Open macOS Accessibility settings."""
        open_accessibility_settings()

    def _finish(self) -> None:
        """Complete and close the wizard only."""
        # Enable launch at login on first setup
        try:
            try:
                from ..autostart import set_auto_start
            except ImportError:
                from autostart import set_auto_start
            set_auto_start(True)
            self._config.auto_start = True
        except Exception:
            pass  # Non-critical — user can enable manually

        self._result = SetupResult(
            completed=True,
            logged_in=bool(self._login_state and self._login_state.logged_in),
            login_state=self._login_state,
        )
        self._window.destroy()


def show_setup_wizard(config: Config, login_manager: LoginManager) -> SetupResult:
    """Show the first-run setup wizard.

    Args:
        config: Current configuration
        login_manager: Login manager for browser auth

    Returns:
        SetupResult indicating whether setup completed and login status
    """
    wizard = SetupWizard(config, login_manager)
    return wizard.show()
