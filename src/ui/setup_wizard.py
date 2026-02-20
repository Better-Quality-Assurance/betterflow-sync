"""First-run setup wizard for BetterFlow Sync.

A polished onboarding wizard: Welcome → Browser login → Success.
Runs only when config.setup_complete is False.
"""

import logging
import threading
import tkinter as tk
from dataclasses import dataclass
from typing import Optional
import itertools

try:
    from ..config import Config
    from ..auth.login import LoginManager, LoginState
except ImportError:
    from config import Config
    from auth.login import LoginManager, LoginState

logger = logging.getLogger(__name__)

WINDOW_WIDTH = 720
WINDOW_HEIGHT = 520

# Colors
BG_COLOR = "#0a1022"
CARD_COLOR = "#0f1936"
CARD_BORDER = "#2d4480"
ACCENT_COLOR = "#1a2c58"
PRIMARY_COLOR = "#00c5e6"
PRIMARY_HOVER = "#00aac8"
TEXT_COLOR = "#f4f7ff"
TEXT_MUTED = "#a8badf"
SUCCESS_COLOR = "#37d67a"
ERROR_COLOR = "#ff5a7a"
BTN_TEXT = "#ffffff"
STEP_ACTIVE = "#00d2ff"
STEP_INACTIVE = "#50618d"


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
        self._button_id = itertools.count(1)

    def show(self) -> SetupResult:
        """Show the wizard and return result when closed."""
        self._window = tk.Tk()
        self._window.title("BetterFlow Sync")
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
        if hasattr(self, "_spinner_after_id"):
            try:
                self._window.after_cancel(self._spinner_after_id)
            except tk.TclError:
                pass
            delattr(self, "_spinner_after_id")
        self._canvas.configure(cursor="")
        self._canvas.delete("all")
        for widget in self._canvas.winfo_children():
            widget.destroy()

    def _on_close(self) -> None:
        """Handle window close."""
        self._result = SetupResult(completed=False)
        self._window.destroy()

    def _make_button(self, text, command, x, y, width=248, primary=True):
        """Create a cross-platform canvas button (macOS tk.Button ignores custom bg)."""
        bg = PRIMARY_COLOR if primary else ACCENT_COLOR
        hover_bg = PRIMARY_HOVER if primary else "#274078"
        border = "#5fdfff" if primary else "#3f588e"
        text_color = "#06243a" if primary else BTN_TEXT
        height = 46
        x1, y1 = x - (width // 2), y - (height // 2)
        x2, y2 = x + (width // 2), y + (height // 2)
        tag = f"btn_{next(self._button_id)}"

        rect_id = self._create_rounded_rect(
            x1, y1, x2, y2, radius=13, fill=bg, outline=border, width=1, tags=(tag, "btn")
        )
        self._canvas.create_text(
            x, y, text=text, font=("Avenir Next", 13, "bold"), fill=text_color, tags=(tag, "btn")
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

    def _create_rounded_rect(self, x1, y1, x2, y2, radius=10, **kwargs):
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

        # Layered ambient bands for depth.
        bands = [
            ("#101938", 0, 0, WINDOW_WIDTH, 120),
            ("#0f1731", 0, 120, WINDOW_WIDTH, 280),
            ("#0c1328", 0, 280, WINDOW_WIDTH, WINDOW_HEIGHT),
        ]
        for color, x1, y1, x2, y2 in bands:
            self._canvas.create_rectangle(x1, y1, x2, y2, fill=color, outline="")

        # Soft glows.
        self._canvas.create_oval(-120, -110, 230, 170, fill="#13274a", outline="")
        self._canvas.create_oval(460, 280, 860, 650, fill="#0f2448", outline="")

    def _draw_card_shell(self) -> tuple[int, int, int, int]:
        """Return content bounds; the window itself is the only card surface."""
        return 56, 34, WINDOW_WIDTH - 56, WINDOW_HEIGHT - 34

    def _draw_step_indicator(self, step: int) -> None:
        """Render wizard step chips."""
        labels = ["Welcome", "Connect", "Done"]
        start_x = WINDOW_WIDTH // 2 - 128
        y = 66
        for idx, label in enumerate(labels, start=1):
            is_active = idx <= step
            color = STEP_ACTIVE if is_active else STEP_INACTIVE
            fg = "#0a1328" if is_active else "#ccd7f5"
            chip_x = start_x + (idx - 1) * 128
            self._canvas.create_rectangle(chip_x, y, chip_x + 106, y + 28, fill=color, outline="")
            self._canvas.create_text(
                chip_x + 53,
                y + 14,
                text=label,
                font=("Avenir Next", 10, "bold"),
                fill=fg,
            )

    def _draw_scene(self, step: int, title: str, subtitle: str) -> int:
        """Shared wizard scene shell. Returns content center x."""
        self._clear()
        self._draw_background()
        self._draw_card_shell()
        self._draw_step_indicator(step)
        cx = WINDOW_WIDTH // 2
        self._canvas.create_text(
            cx, 126,
            text=title,
            font=("Avenir Next", 30, "bold"),
            fill=TEXT_COLOR,
        )
        self._canvas.create_text(
            cx, 160,
            text=subtitle,
            font=("Avenir Next", 13),
            fill=TEXT_MUTED,
        )
        return cx

    # ── Welcome Screen ───────────────────────────────────────────────

    def _show_welcome(self) -> None:
        """Show the welcome screen."""
        cx = self._draw_scene(
            step=1,
            title="Welcome to BetterFlow Sync",
            subtitle="Install local tracking and connect your BetterFlow account",
        )

        # Logo circle
        logo_y = 244
        r = 44
        self._canvas.create_oval(
            cx - r, logo_y - r, cx + r, logo_y + r,
            fill=PRIMARY_COLOR, outline=""
        )
        self._canvas.create_oval(
            cx - r - 9, logo_y - r - 9, cx + r + 9, logo_y + r + 9,
            outline="#35e0ff", width=2
        )
        self._canvas.create_text(
            cx, logo_y,
            text="BF",
            font=("Avenir Next", 26, "bold"),
            fill="#071226",
        )

        # Description
        self._canvas.create_text(
            cx, 332,
            text=(
                "Runs in your menu bar, captures activity on-device,\n"
                "and syncs private summaries to BetterFlow."
            ),
            font=("Avenir Next", 12),
            fill=TEXT_MUTED,
            justify=tk.CENTER,
        )

        self._canvas.create_text(
            cx, 384,
            text="The next step opens your browser for secure sign-in.",
            font=("Avenir Next", 11),
            fill="#8fa7d6",
            justify=tk.CENTER,
        )

        self._make_button("Install and Connect", self._start_login, cx, 432, width=276)

    # ── Signing In Screen ────────────────────────────────────────────

    def _start_login(self) -> None:
        """Show signing in state and open browser."""
        cx = self._draw_scene(
            step=2,
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
            font=("Avenir Next", 12),
            fill=TEXT_MUTED,
            justify=tk.CENTER,
        )

        # Retry button (hidden initially, placed for later use)
        self._retry_btn = None

        # Start login in background
        def do_login():
            state = self._login_manager.login_via_browser()
            self._window.after(0, lambda: self._on_login_complete(state))

        threading.Thread(target=do_login, daemon=True).start()

        # Start spinner animation
        self._animate_spinner(cx, 250)

    def _draw_spinner(self, cx, cy):
        """Draw the spinner arc."""
        r = 30
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
        # Background ring
        self._canvas.create_arc(
            cx - r, cy - r, cx + r, cy + r,
            start=0,
            extent=359,
            style=tk.ARC,
            outline="#304a89",
            width=3,
            tags="spinner_bg",
        )
        # Bring spinner to front
        self._canvas.tag_raise("spinner")

    def _animate_spinner(self, cx, cy):
        """Animate the spinner."""
        if not self._canvas.winfo_exists():
            return
        try:
            self._canvas.delete("spinner")
            self._spinner_angle = (self._spinner_angle + 10) % 360
            r = 30
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
                24, lambda: self._animate_spinner(cx, cy)
            )
        except tk.TclError:
            pass

    def _on_login_complete(self, state: LoginState) -> None:
        """Handle login result."""
        # Stop spinner
        if hasattr(self, "_spinner_after_id"):
            self._window.after_cancel(self._spinner_after_id)

        if state.logged_in:
            self._login_state = state
            self._show_success(state.user_email or "")
        else:
            self._show_error(state.error or "Login failed")

    def _show_error(self, error: str) -> None:
        """Show error state with retry."""
        cx = self._draw_scene(
            step=2,
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
            font=("Avenir Next", 34, "bold"),
            fill=BTN_TEXT,
        )

        self._canvas.create_text(
            cx, 332,
            text=error,
            font=("Avenir Next", 12),
            fill=TEXT_MUTED,
        )

        self._make_button("Try Again", self._start_login, cx, 430, width=220)

    # ── Success Screen ───────────────────────────────────────────────

    def _show_success(self, email: str) -> None:
        """Show success screen."""
        cx = self._draw_scene(
            step=3,
            title="You’re All Set",
            subtitle="BetterFlow Sync is ready to run",
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
            font=("Avenir Next", 33, "bold"),
            fill=BTN_TEXT,
        )

        # Email
        if email:
            self._canvas.create_text(
                cx, 304,
                text=f"Signed in as {email}",
                font=("Avenir Next", 12, "bold"),
                fill=SUCCESS_COLOR,
            )

        # Description
        self._canvas.create_text(
            cx, 352,
            text=(
                "BetterFlow Sync will now run in your menu bar.\n"
                "It will automatically track and sync your activity."
            ),
            font=("Avenir Next", 12),
            fill=TEXT_MUTED,
            justify=tk.CENTER,
        )

        # Launch button
        self._make_button("Start Using BetterFlow", self._finish, cx, 438, width=280)

    def _finish(self) -> None:
        """Complete and close the wizard only."""
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
