"""Preferences and login windows using tkinter."""

import logging
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable, Optional

from ..config import Config

logger = logging.getLogger(__name__)


class LoginWindow:
    """Login dialog window."""

    def __init__(
        self,
        on_login: Callable[[str, str], bool],
        on_cancel: Optional[Callable[[], None]] = None,
    ):
        """Initialize login window.

        Args:
            on_login: Callback with (email, password), returns True if successful
            on_cancel: Callback when cancelled
        """
        self._on_login = on_login
        self._on_cancel = on_cancel
        self._window: Optional[tk.Tk] = None
        self._email_var: Optional[tk.StringVar] = None
        self._password_var: Optional[tk.StringVar] = None
        self._error_var: Optional[tk.StringVar] = None
        self._login_button: Optional[ttk.Button] = None

    def show(self) -> None:
        """Show the login window."""
        self._window = tk.Tk()
        self._window.title("BetterFlow Sync - Sign In")
        self._window.geometry("400x300")
        self._window.resizable(False, False)

        # Center on screen
        self._window.update_idletasks()
        x = (self._window.winfo_screenwidth() - 400) // 2
        y = (self._window.winfo_screenheight() - 300) // 2
        self._window.geometry(f"+{x}+{y}")

        # Variables
        self._email_var = tk.StringVar()
        self._password_var = tk.StringVar()
        self._error_var = tk.StringVar()

        # Main frame
        frame = ttk.Frame(self._window, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)

        # Title
        title = ttk.Label(
            frame, text="Sign in to BetterFlow", font=("Helvetica", 16, "bold")
        )
        title.pack(pady=(0, 20))

        # Email
        email_frame = ttk.Frame(frame)
        email_frame.pack(fill=tk.X, pady=5)
        ttk.Label(email_frame, text="Email:").pack(anchor=tk.W)
        email_entry = ttk.Entry(email_frame, textvariable=self._email_var, width=40)
        email_entry.pack(fill=tk.X)
        email_entry.focus()

        # Password
        password_frame = ttk.Frame(frame)
        password_frame.pack(fill=tk.X, pady=5)
        ttk.Label(password_frame, text="Password:").pack(anchor=tk.W)
        password_entry = ttk.Entry(
            password_frame, textvariable=self._password_var, show="*", width=40
        )
        password_entry.pack(fill=tk.X)

        # Error message
        error_label = ttk.Label(
            frame, textvariable=self._error_var, foreground="red", wraplength=360
        )
        error_label.pack(pady=10)

        # Buttons
        button_frame = ttk.Frame(frame)
        button_frame.pack(fill=tk.X, pady=10)

        cancel_button = ttk.Button(button_frame, text="Cancel", command=self._cancel)
        cancel_button.pack(side=tk.LEFT)

        self._login_button = ttk.Button(
            button_frame, text="Sign In", command=self._login
        )
        self._login_button.pack(side=tk.RIGHT)

        # Bind Enter key
        self._window.bind("<Return>", lambda e: self._login())
        self._window.bind("<Escape>", lambda e: self._cancel())

        # Handle window close
        self._window.protocol("WM_DELETE_WINDOW", self._cancel)

        self._window.mainloop()

    def _login(self) -> None:
        """Handle login button click."""
        email = self._email_var.get().strip()
        password = self._password_var.get()

        if not email:
            self._error_var.set("Please enter your email")
            return
        if not password:
            self._error_var.set("Please enter your password")
            return

        # Disable button while logging in
        self._login_button.config(state=tk.DISABLED)
        self._error_var.set("Signing in...")

        # Run login in background
        def do_login():
            try:
                success = self._on_login(email, password)
                self._window.after(0, lambda: self._handle_login_result(success))
            except Exception as e:
                self._window.after(
                    0, lambda: self._handle_login_result(False, str(e))
                )

        threading.Thread(target=do_login, daemon=True).start()

    def _handle_login_result(self, success: bool, error: Optional[str] = None) -> None:
        """Handle login result on main thread."""
        self._login_button.config(state=tk.NORMAL)

        if success:
            self._window.destroy()
        else:
            self._error_var.set(error or "Invalid email or password")

    def _cancel(self) -> None:
        """Handle cancel."""
        if self._on_cancel:
            self._on_cancel()
        if self._window:
            self._window.destroy()


class PreferencesWindow:
    """Preferences dialog window."""

    def __init__(self, config: Config, on_save: Callable[[Config], None]):
        """Initialize preferences window.

        Args:
            config: Current configuration
            on_save: Callback with updated config
        """
        self._config = config
        self._on_save = on_save
        self._window: Optional[tk.Tk] = None

    def show(self) -> None:
        """Show the preferences window."""
        self._window = tk.Tk()
        self._window.title("BetterFlow Sync - Preferences")
        self._window.geometry("500x400")
        self._window.resizable(False, False)

        # Center on screen
        self._window.update_idletasks()
        x = (self._window.winfo_screenwidth() - 500) // 2
        y = (self._window.winfo_screenheight() - 400) // 2
        self._window.geometry(f"+{x}+{y}")

        # Notebook for tabs
        notebook = ttk.Notebook(self._window)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # General tab
        general_frame = ttk.Frame(notebook, padding=10)
        notebook.add(general_frame, text="General")
        self._build_general_tab(general_frame)

        # Privacy tab
        privacy_frame = ttk.Frame(notebook, padding=10)
        notebook.add(privacy_frame, text="Privacy")
        self._build_privacy_tab(privacy_frame)

        # Advanced tab
        advanced_frame = ttk.Frame(notebook, padding=10)
        notebook.add(advanced_frame, text="Advanced")
        self._build_advanced_tab(advanced_frame)

        # Buttons
        button_frame = ttk.Frame(self._window)
        button_frame.pack(fill=tk.X, padx=10, pady=10)

        cancel_button = ttk.Button(button_frame, text="Cancel", command=self._cancel)
        cancel_button.pack(side=tk.LEFT)

        save_button = ttk.Button(button_frame, text="Save", command=self._save)
        save_button.pack(side=tk.RIGHT)

        self._window.mainloop()

    def _build_general_tab(self, frame: ttk.Frame) -> None:
        """Build the general settings tab."""
        # Auto-start
        self._auto_start_var = tk.BooleanVar(value=self._config.auto_start)
        ttk.Checkbutton(
            frame,
            text="Start BetterFlow Sync when you log in",
            variable=self._auto_start_var,
        ).pack(anchor=tk.W, pady=5)

        # Check for updates
        self._check_updates_var = tk.BooleanVar(value=self._config.check_updates)
        ttk.Checkbutton(
            frame,
            text="Automatically check for updates",
            variable=self._check_updates_var,
        ).pack(anchor=tk.W, pady=5)

        # Sync interval
        interval_frame = ttk.Frame(frame)
        interval_frame.pack(fill=tk.X, pady=10)
        ttk.Label(interval_frame, text="Sync interval (seconds):").pack(side=tk.LEFT)
        self._interval_var = tk.StringVar(
            value=str(self._config.sync.interval_seconds)
        )
        interval_spin = ttk.Spinbox(
            interval_frame,
            from_=30,
            to=300,
            width=5,
            textvariable=self._interval_var,
        )
        interval_spin.pack(side=tk.LEFT, padx=10)

    def _build_privacy_tab(self, frame: ttk.Frame) -> None:
        """Build the privacy settings tab."""
        # Hash titles
        self._hash_titles_var = tk.BooleanVar(value=self._config.privacy.hash_titles)
        ttk.Checkbutton(
            frame,
            text="Hash window titles (recommended for privacy)",
            variable=self._hash_titles_var,
        ).pack(anchor=tk.W, pady=5)

        # Domain only URLs
        self._domain_only_var = tk.BooleanVar(
            value=self._config.privacy.domain_only_urls
        )
        ttk.Checkbutton(
            frame,
            text="Only send domain names, not full URLs",
            variable=self._domain_only_var,
        ).pack(anchor=tk.W, pady=5)

        # Info text
        ttk.Label(
            frame,
            text=(
                "BetterFlow Sync respects your privacy:\n"
                "• Window titles are hashed by default\n"
                "• Only domain names are sent for browser activity\n"
                "• No keystrokes or screenshots are ever captured\n"
                "• You control what data is shared"
            ),
            foreground="gray",
            wraplength=400,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=20)

    def _build_advanced_tab(self, frame: ttk.Frame) -> None:
        """Build the advanced settings tab."""
        # ActivityWatch settings
        aw_frame = ttk.LabelFrame(frame, text="ActivityWatch Connection", padding=10)
        aw_frame.pack(fill=tk.X, pady=10)

        host_frame = ttk.Frame(aw_frame)
        host_frame.pack(fill=tk.X, pady=2)
        ttk.Label(host_frame, text="Host:").pack(side=tk.LEFT)
        self._aw_host_var = tk.StringVar(value=self._config.aw.host)
        ttk.Entry(host_frame, textvariable=self._aw_host_var, width=20).pack(
            side=tk.LEFT, padx=10
        )

        port_frame = ttk.Frame(aw_frame)
        port_frame.pack(fill=tk.X, pady=2)
        ttk.Label(port_frame, text="Port:").pack(side=tk.LEFT)
        self._aw_port_var = tk.StringVar(value=str(self._config.aw.port))
        ttk.Entry(port_frame, textvariable=self._aw_port_var, width=10).pack(
            side=tk.LEFT, padx=10
        )

        # Debug mode
        self._debug_var = tk.BooleanVar(value=self._config.debug_mode)
        ttk.Checkbutton(
            frame, text="Enable debug logging", variable=self._debug_var
        ).pack(anchor=tk.W, pady=10)

        # API URL (read-only info)
        ttk.Label(
            frame,
            text=f"API: {self._config.api_url}",
            foreground="gray",
        ).pack(anchor=tk.W, pady=5)

    def _save(self) -> None:
        """Save settings."""
        try:
            # Update config
            self._config.auto_start = self._auto_start_var.get()
            self._config.check_updates = self._check_updates_var.get()
            self._config.sync.interval_seconds = int(self._interval_var.get())
            self._config.privacy.hash_titles = self._hash_titles_var.get()
            self._config.privacy.domain_only_urls = self._domain_only_var.get()
            self._config.aw.host = self._aw_host_var.get()
            self._config.aw.port = int(self._aw_port_var.get())
            self._config.debug_mode = self._debug_var.get()

            self._on_save(self._config)
            self._window.destroy()
        except ValueError as e:
            messagebox.showerror("Invalid Input", str(e))

    def _cancel(self) -> None:
        """Cancel and close."""
        self._window.destroy()


def show_login_window(on_login: Callable[[str, str], bool]) -> None:
    """Show login window in a new thread.

    Args:
        on_login: Callback with (email, password), returns True if successful
    """
    window = LoginWindow(on_login)
    window.show()


def show_preferences_window(config: Config, on_save: Callable[[Config], None]) -> None:
    """Show preferences window.

    Args:
        config: Current configuration
        on_save: Callback with updated config
    """
    window = PreferencesWindow(config, on_save)
    window.show()
