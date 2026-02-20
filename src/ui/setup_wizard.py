"""First-run setup wizard for BetterFlow Sync.

A 4-step tkinter wizard that guides users through permissions and login
on first launch. Runs only when config.setup_complete is False.
"""

import logging
import platform
import threading
import tkinter as tk
from tkinter import ttk
from dataclasses import dataclass
from typing import Optional

try:
    from ..config import Config
    from ..auth.login import LoginManager, LoginState
    from .permissions import (
        check_screen_recording,
        check_accessibility,
        open_screen_recording_settings,
        open_accessibility_settings,
    )
except ImportError:
    from config import Config
    from auth.login import LoginManager, LoginState
    from ui.permissions import (
        check_screen_recording,
        check_accessibility,
        open_screen_recording_settings,
        open_accessibility_settings,
    )

logger = logging.getLogger(__name__)

_IS_MACOS = platform.system() == "Darwin"

WINDOW_WIDTH = 600
WINDOW_HEIGHT = 450


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
        self._current_step = 0
        self._window: Optional[tk.Tk] = None
        self._content_frame: Optional[ttk.Frame] = None
        self._login_state: Optional[LoginState] = None

    def show(self) -> SetupResult:
        """Show the wizard and return result when closed."""
        self._window = tk.Tk()
        self._window.title("BetterFlow Sync Setup")
        self._window.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self._window.resizable(False, False)

        # Center on screen
        self._window.update_idletasks()
        x = (self._window.winfo_screenwidth() - WINDOW_WIDTH) // 2
        y = (self._window.winfo_screenheight() - WINDOW_HEIGHT) // 2
        self._window.geometry(f"+{x}+{y}")

        # Handle window close (X button)
        self._window.protocol("WM_DELETE_WINDOW", self._on_close)

        # Main container
        self._content_frame = ttk.Frame(self._window, padding=30)
        self._content_frame.pack(fill=tk.BOTH, expand=True)

        # Show first step
        self._show_step_welcome()

        self._window.mainloop()
        return self._result

    def _clear_content(self) -> None:
        """Clear the content frame for the next step."""
        for widget in self._content_frame.winfo_children():
            widget.destroy()

    def _on_close(self) -> None:
        """Handle window close — exit without completing."""
        self._result = SetupResult(completed=False)
        self._window.destroy()

    # ── Step 1: Welcome ──────────────────────────────────────────────

    def _show_step_welcome(self) -> None:
        """Show the welcome step."""
        self._clear_content()
        self._current_step = 1

        # Spacer
        ttk.Frame(self._content_frame).pack(expand=True)

        # Heading
        heading = ttk.Label(
            self._content_frame,
            text="Welcome to BetterFlow Sync",
            font=("Helvetica", 22, "bold"),
        )
        heading.pack(pady=(0, 15))

        # Description
        desc = ttk.Label(
            self._content_frame,
            text=(
                "BetterFlow Sync automatically tracks your work activity\n"
                "and sends it to BetterFlow for effortless time tracking.\n\n"
                "This setup will help you:\n"
                "  1. Grant required macOS permissions\n"
                "  2. Sign in to your BetterFlow account"
            ),
            font=("Helvetica", 13),
            justify=tk.CENTER,
            wraplength=450,
        )
        desc.pack(pady=(0, 30))

        # Get Started button
        btn = ttk.Button(
            self._content_frame,
            text="Get Started",
            command=self._next_from_welcome,
        )
        btn.pack(pady=10)

        # Spacer
        ttk.Frame(self._content_frame).pack(expand=True)

    def _next_from_welcome(self) -> None:
        """Advance from welcome step."""
        if _IS_MACOS:
            self._show_step_permissions()
        else:
            self._show_step_login()

    # ── Step 2: Permissions (macOS only) ─────────────────────────────

    def _show_step_permissions(self) -> None:
        """Show the permissions step."""
        self._clear_content()
        self._current_step = 2

        heading = ttk.Label(
            self._content_frame,
            text="macOS Permissions",
            font=("Helvetica", 18, "bold"),
        )
        heading.pack(pady=(10, 5))

        desc = ttk.Label(
            self._content_frame,
            text=(
                "BetterFlow Sync needs these permissions to track\n"
                "which apps you use. Grant them in System Settings."
            ),
            font=("Helvetica", 12),
            justify=tk.CENTER,
            wraplength=450,
        )
        desc.pack(pady=(0, 20))

        # Permission rows container
        perm_frame = ttk.Frame(self._content_frame)
        perm_frame.pack(fill=tk.X, padx=20)

        # Screen Recording row
        self._sr_status_label = self._build_permission_row(
            perm_frame,
            "Screen Recording",
            "Required to capture active window titles",
            check_screen_recording,
            open_screen_recording_settings,
        )

        ttk.Separator(perm_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)

        # Accessibility row
        self._ax_status_label = self._build_permission_row(
            perm_frame,
            "Accessibility",
            "Required to detect keyboard/mouse activity",
            check_accessibility,
            open_accessibility_settings,
        )

        # Buttons at bottom
        btn_frame = ttk.Frame(self._content_frame)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(20, 0))

        ttk.Button(
            btn_frame,
            text="Refresh",
            command=self._refresh_permissions,
        ).pack(side=tk.LEFT)

        ttk.Button(
            btn_frame,
            text="Continue",
            command=self._show_step_login,
        ).pack(side=tk.RIGHT)

    def _build_permission_row(
        self, parent, name, description, check_fn, open_fn
    ) -> ttk.Label:
        """Build a single permission row. Returns the status label."""
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, pady=5)

        # Left side: name + description
        left = ttk.Frame(row)
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Label(left, text=name, font=("Helvetica", 13, "bold")).pack(anchor=tk.W)
        ttk.Label(
            left, text=description, font=("Helvetica", 11), foreground="gray"
        ).pack(anchor=tk.W)

        # Right side: status + button
        right = ttk.Frame(row)
        right.pack(side=tk.RIGHT)

        granted = check_fn()
        status_text = "Granted" if granted else "Not Granted"
        status_color = "green" if granted else "red"
        status_label = ttk.Label(
            right, text=status_text, foreground=status_color, font=("Helvetica", 11)
        )
        status_label.pack(side=tk.LEFT, padx=(0, 10))

        if not granted:
            ttk.Button(
                right,
                text="Open Settings",
                command=open_fn,
            ).pack(side=tk.LEFT)

        # Store check_fn on label for refresh
        status_label._check_fn = check_fn
        status_label._open_fn = open_fn

        return status_label

    def _refresh_permissions(self) -> None:
        """Re-check permissions and update display."""
        self._show_step_permissions()

    # ── Step 3: Login ────────────────────────────────────────────────

    def _show_step_login(self) -> None:
        """Show the login step."""
        self._clear_content()
        self._current_step = 3

        # Spacer
        ttk.Frame(self._content_frame).pack(expand=True)

        heading = ttk.Label(
            self._content_frame,
            text="Sign In",
            font=("Helvetica", 18, "bold"),
        )
        heading.pack(pady=(0, 5))

        desc = ttk.Label(
            self._content_frame,
            text="Sign in to your BetterFlow account to start syncing.",
            font=("Helvetica", 12),
            justify=tk.CENTER,
        )
        desc.pack(pady=(0, 25))

        # Status area
        self._login_status = ttk.Label(
            self._content_frame,
            text="",
            font=("Helvetica", 12),
            justify=tk.CENTER,
        )
        self._login_status.pack(pady=(0, 15))

        # Sign in button
        self._login_btn = ttk.Button(
            self._content_frame,
            text="Sign In with Browser",
            command=self._start_login,
        )
        self._login_btn.pack(pady=5)

        # Skip link
        self._skip_btn = ttk.Button(
            self._content_frame,
            text="Skip — sign in later",
            command=self._skip_login,
        )
        self._skip_btn.pack(pady=(10, 0))

        # Spacer
        ttk.Frame(self._content_frame).pack(expand=True)

    def _start_login(self) -> None:
        """Start the browser login flow in a background thread."""
        self._login_btn.configure(state=tk.DISABLED)
        self._skip_btn.configure(state=tk.DISABLED)
        self._login_status.configure(
            text="Waiting for browser...", foreground="gray"
        )

        def do_login():
            state = self._login_manager.login_via_browser()
            # Schedule UI update on main thread
            self._window.after(0, lambda: self._on_login_complete(state))

        threading.Thread(target=do_login, daemon=True).start()

    def _on_login_complete(self, state: LoginState) -> None:
        """Handle login result on the main thread."""
        if state.logged_in:
            self._login_state = state
            email = state.user_email or "Unknown"
            self._login_status.configure(
                text=f"Signed in as {email}", foreground="green"
            )
            self._login_btn.pack_forget()
            self._skip_btn.pack_forget()
            # Auto-advance after 1 second
            self._window.after(1000, self._show_step_done)
        else:
            error = state.error or "Login failed"
            self._login_status.configure(text=error, foreground="red")
            self._login_btn.configure(state=tk.NORMAL, text="Retry")
            self._skip_btn.configure(state=tk.NORMAL)

    def _skip_login(self) -> None:
        """Skip login and proceed to done."""
        self._login_state = None
        self._show_step_done()

    # ── Step 4: Done ─────────────────────────────────────────────────

    def _show_step_done(self) -> None:
        """Show the completion step."""
        self._clear_content()
        self._current_step = 4

        # Spacer
        ttk.Frame(self._content_frame).pack(expand=True)

        heading = ttk.Label(
            self._content_frame,
            text="You're All Set!",
            font=("Helvetica", 22, "bold"),
        )
        heading.pack(pady=(0, 15))

        if self._login_state and self._login_state.logged_in:
            summary = (
                "BetterFlow Sync will now run in your system tray\n"
                "and automatically track your work activity."
            )
        else:
            summary = (
                "BetterFlow Sync will now run in your system tray.\n"
                "Sign in from the tray menu to start syncing."
            )

        ttk.Label(
            self._content_frame,
            text=summary,
            font=("Helvetica", 13),
            justify=tk.CENTER,
            wraplength=450,
        ).pack(pady=(0, 30))

        ttk.Button(
            self._content_frame,
            text="Start BetterFlow Sync",
            command=self._finish,
        ).pack(pady=10)

        # Spacer
        ttk.Frame(self._content_frame).pack(expand=True)

    def _finish(self) -> None:
        """Complete the wizard."""
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
