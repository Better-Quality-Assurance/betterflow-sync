"""Preferences window using tkinter."""

import logging
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable, Optional

try:
    from ..config import Config
except ImportError:
    from config import Config

logger = logging.getLogger(__name__)


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


def show_preferences_window(config: Config, on_save: Callable[[Config], None]) -> None:
    """Show preferences window.

    Args:
        config: Current configuration
        on_save: Callback with updated config
    """
    window = PreferencesWindow(config, on_save)
    window.show()
