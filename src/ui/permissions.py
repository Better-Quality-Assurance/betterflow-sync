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
    bundles), falls back to ctypes, then to a practical AppleScript test.

    The practical test is needed because AXIsProcessTrusted() can return
    False for unsigned x86_64 PyInstaller binaries running under Rosetta
    even when the System Settings toggle is ON.

    Returns True on non-macOS platforms.
    """
    if not _IS_MACOS:
        return True

    # Primary: pyobjc ApplicationServices binding
    try:
        from ApplicationServices import AXIsProcessTrusted

        if AXIsProcessTrusted():
            return True
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
        if lib.AXIsProcessTrusted():
            return True
    except Exception:
        pass

    # Practical test: try reading the frontmost app name via AppleScript.
    # This actually exercises the Accessibility API and works even when
    # AXIsProcessTrusted() lies (Rosetta/unsigned binary edge case).
    try:
        result = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to get name of first process whose frontmost is true'],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            logger.debug("AXIsProcessTrusted=False but AppleScript works, treating as granted")
            return True
    except Exception:
        pass

    return False


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
