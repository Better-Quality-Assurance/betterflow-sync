"""macOS permission checking utilities.

Uses pyobjc (preferred) or ctypes to check Accessibility permission.
Returns True on non-macOS platforms.

After a fresh build the app's code signature changes. macOS may show
the toggle as ON in System Settings while AXIsProcessTrusted() returns
False because the TCC entry still references the old signature. Toggling
the permission off and on again in System Settings re-registers it.
"""

import logging
import platform
import subprocess

logger = logging.getLogger(__name__)

_IS_MACOS = platform.system() == "Darwin"


def check_accessibility() -> bool:
    """Check if Accessibility permission is granted.

    Tries pyobjc ApplicationServices first (more reliable in PyInstaller
    bundles), falls back to ctypes.

    Returns True on non-macOS platforms.
    """
    if not _IS_MACOS:
        return True

    # Primary: pyobjc ApplicationServices binding
    try:
        from ApplicationServices import AXIsProcessTrusted

        return bool(AXIsProcessTrusted())
    except Exception:
        logger.debug("pyobjc accessibility check failed, trying ctypes")

    # Fallback: ctypes
    try:
        import ctypes
        import ctypes.util

        lib = ctypes.cdll.LoadLibrary(
            ctypes.util.find_library("ApplicationServices")
        )
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return lib.AXIsProcessTrusted()
    except Exception:
        logger.debug("Could not check Accessibility permission, assuming granted")
        return True


def open_accessibility_settings() -> None:
    """Open System Settings to Accessibility pane."""
    if not _IS_MACOS:
        return

    try:
        subprocess.Popen([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        ])
    except Exception as e:
        logger.warning(f"Failed to open Accessibility settings: {e}")
