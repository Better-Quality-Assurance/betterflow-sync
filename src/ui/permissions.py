"""macOS permission checking utilities.

Uses ctypes to check Screen Recording and Accessibility permissions
without requiring pyobjc. Returns True on non-macOS platforms.
"""

import logging
import platform
import subprocess

logger = logging.getLogger(__name__)

_IS_MACOS = platform.system() == "Darwin"


def check_screen_recording() -> bool:
    """Check if Screen Recording permission is granted.

    Returns True on non-macOS platforms.
    """
    if not _IS_MACOS:
        return True

    try:
        import ctypes
        import ctypes.util

        cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return cg.CGPreflightScreenCaptureAccess()
    except Exception:
        logger.debug("Could not check Screen Recording permission, assuming granted")
        return True


def check_accessibility() -> bool:
    """Check if Accessibility permission is granted.

    Returns True on non-macOS platforms.
    """
    if not _IS_MACOS:
        return True

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


def open_screen_recording_settings() -> None:
    """Open System Settings to Screen Recording pane."""
    if not _IS_MACOS:
        return

    try:
        subprocess.Popen([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
        ])
    except Exception as e:
        logger.warning(f"Failed to open Screen Recording settings: {e}")


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
