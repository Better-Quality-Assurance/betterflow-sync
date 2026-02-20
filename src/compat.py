"""PyInstaller compatibility utilities.

This module provides utilities for handling imports that work both in
development (with relative imports) and when bundled with PyInstaller
(with absolute imports from the bundle directory).

Usage in entry point modules:

    # Support both relative imports (module) and absolute imports (PyInstaller)
    try:
        from .config import Config
        from .sync import AWClient
    except ImportError:
        from config import Config
        from sync import AWClient

The try/except pattern handles:
- Development: Running as a module with `python -m src.main`
- PyInstaller: Running as frozen bundle where relative imports fail
"""

import sys


def is_frozen() -> bool:
    """Check if running as a PyInstaller bundle.

    Returns:
        True if running as a frozen PyInstaller executable
    """
    return getattr(sys, "frozen", False)


def get_bundle_dir() -> str:
    """Get the PyInstaller bundle directory.

    Returns:
        Path to bundle directory if frozen, else empty string
    """
    if is_frozen():
        return sys._MEIPASS  # type: ignore
    return ""
