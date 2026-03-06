"""macOS permission checking utilities.

Uses pyobjc (preferred) or ctypes to check Screen Recording and
Accessibility permissions. Returns True on non-macOS platforms.

After a fresh build the app's code signature changes. macOS may show
the toggle as ON in System Settings while AXIsProcessTrusted() returns
False because the TCC entry still references the old signature. Toggling
the permission off and on again in System Settings re-registers it.
"""

import logging
import os
import platform
import subprocess

logger = logging.getLogger(__name__)

_IS_MACOS = platform.system() == "Darwin"


def check_screen_recording() -> bool:
    """Check if Screen Recording permission is granted.

    Uses CGPreflightScreenCaptureAccess (ctypes) as the primary check,
    with a practical fallback that tries reading window names from other
    processes via Quartz (requires screen recording on macOS 10.15+).

    Returns True on non-macOS platforms.
    """
    if not _IS_MACOS:
        return True

    # Primary: CGPreflightScreenCaptureAccess via ctypes
    try:
        import ctypes
        import ctypes.util

        cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        if cg.CGPreflightScreenCaptureAccess():
            return True
    except Exception:
        logger.debug("ctypes screen recording check failed")

    # Fallback: practical test — can we read window names from other apps?
    # Without screen recording, kCGWindowName is empty for other processes.
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListOptionOnScreenOnly,
            kCGWindowName,
            kCGWindowOwnerPID,
        )

        my_pid = os.getpid()
        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly, kCGNullWindowID,
        )
        for w in (windows or []):
            pid = w.get(kCGWindowOwnerPID, 0)
            name = w.get(kCGWindowName, "")
            if pid != my_pid and name:
                return True
    except Exception:
        logger.debug("Quartz screen recording fallback failed")

    return False


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


def all_permissions_granted() -> bool:
    """Check if both Accessibility and Screen Recording permissions are granted.

    Returns True on non-macOS platforms.
    """
    return check_accessibility() and check_screen_recording()
