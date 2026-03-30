"""macOS permission checking utilities.

Uses pyobjc (preferred) or ctypes to check macOS privacy permissions.
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


def check_input_monitoring(prompt: bool = False) -> bool:
    """Check if Input Monitoring permission is granted.

    Keypress capture on modern macOS requires the ListenEvent / Input
    Monitoring privacy permission in addition to Accessibility.

    Args:
        prompt: When True, ask macOS to show the permission prompt if needed.
    """
    if not _IS_MACOS:
        return True

    try:
        from Quartz import (
            CGPreflightListenEventAccess,
            CGRequestListenEventAccess,
        )

        if CGPreflightListenEventAccess():
            return True
        if prompt:
            return bool(CGRequestListenEventAccess())
    except Exception as e:
        logger.debug(f"Input Monitoring check failed: {e}")

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


def open_input_monitoring_settings() -> None:
    """Open System Settings to Input Monitoring pane."""
    if not _IS_MACOS:
        return

    try:
        subprocess.Popen([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent",
        ])
    except Exception as e:
        logger.warning(f"Failed to open Input Monitoring settings: {e}")


_BUNDLE_ID = "co.betterqa.betterflow"


def grant_tcc_permissions() -> bool:
    """Grant Accessibility and Input Monitoring via TCC database with admin auth.

    Shows the native macOS password dialog ("Allow Always" style).
    Returns True if the grant succeeded.
    """
    if not _IS_MACOS:
        return True

    services = []
    if not check_accessibility():
        services.append("kTCCServiceAccessibility")
    if not check_input_monitoring():
        services.append("kTCCServiceListenEvent")

    if not services:
        return True

    # Build SQL statements for each missing permission
    sql_parts = []
    for svc in services:
        sql_parts.append(
            f"INSERT OR REPLACE INTO access "
            f"(service, client, client_type, auth_value, auth_reason, auth_version, "
            f"indirect_object_identifier_type, indirect_object_identifier, flags, last_modified) "
            f"VALUES ('{svc}', '{_BUNDLE_ID}', 0, 2, 3, 1, 0, 'UNUSED', 0, "
            f"CAST(strftime('%s','now') AS INTEGER));"
        )
    sql = " ".join(sql_parts)

    # Use osascript to show the native admin password prompt and run sqlite3 as root
    script = (
        f'do shell script '
        f'"sqlite3 \\"/Library/Application Support/com.apple.TCC/TCC.db\\" '
        f'\\"{sql}\\"" '
        f'with administrator privileges'
    )

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            logger.info("TCC permissions granted via admin auth")
            return True
        else:
            stderr = result.stderr.strip()
            if "User canceled" in stderr or "-128" in stderr:
                logger.info("User cancelled admin auth for TCC grant")
            else:
                logger.warning(f"TCC grant failed: {stderr}")
            return False
    except subprocess.TimeoutExpired:
        logger.warning("TCC grant timed out waiting for admin auth")
        return False
    except Exception as e:
        logger.warning(f"TCC grant error: {e}")
        return False
