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
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_IS_MACOS = platform.system() == "Darwin"

# One-shot guard so input_monitoring_active() logs its macOS diagnostics once
# per process instead of on every poll.
_IM_DIAG_LOGGED = False

# Whitelist of TCC services we will ever insert into TCC.db. Guards
# grant_tcc_permissions() against accidental SQL injection if callers ever
# route user input into the services list.
_ALLOWED_TCC_SERVICES = frozenset({
    "kTCCServiceAccessibility",
    "kTCCServiceListenEvent",
})

# Reverse-DNS bundle ID format (letters, digits, dots, hyphens, underscores).
_BUNDLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$")


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
    except Exception as e:
        logger.debug("ctypes AXIsProcessTrusted probe failed: %s", e)

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
    except Exception as e:
        logger.debug("AppleScript accessibility probe failed: %s", e)

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


def _probe_listen_event_tap() -> bool:
    """Create a throwaway listen-only CGEventTap.

    Doing so is the canonical way to (1) make the app appear in System Settings
    > Privacy & Security > Input Monitoring — without this the app is never
    listed and the user has no toggle to flip — and (2) authoritatively detect
    the grant: CGEventTapCreate only returns a tap when access is granted.

    Returns True if the tap was created (access granted), else False.
    """
    try:
        from Quartz import (
            CGEventTapCreate,
            kCGEventTapOptionListenOnly,
            kCGHeadInsertEventTap,
            kCGSessionEventTap,
        )
    except Exception as e:
        logger.debug("Quartz event-tap import failed: %s", e)
        return False

    def _noop(proxy, event_type, event, refcon):
        return event

    tap = None
    try:
        # kCGEventKeyDown == 10. A minimal mask is enough to trigger the grant
        # check and register the app in the Input Monitoring list.
        tap = CGEventTapCreate(
            kCGSessionEventTap,
            kCGHeadInsertEventTap,
            kCGEventTapOptionListenOnly,
            (1 << 10),
            _noop,
            None,
        )
        return tap is not None
    except Exception as e:
        logger.debug("CGEventTapCreate probe failed: %s", e)
        return False
    finally:
        # We never run this tap — tear it down so it doesn't leak.
        if tap is not None:
            try:
                from CoreFoundation import CFMachPortInvalidate, CFRelease

                CFMachPortInvalidate(tap)
                CFRelease(tap)
            except Exception as e:
                logger.debug("event-tap probe cleanup failed: %s", e)


# IOKit HID request types / access states (from <IOKit/hidsystem/IOHIDLib.h>).
_kIOHIDRequestTypeListenEvent = 1
_kIOHIDAccessTypeGranted = 0
_IOKIT_FRAMEWORK = "/System/Library/Frameworks/IOKit.framework/IOKit"


def _iokit_hid():
    """Load IOKit and bind the HID access functions, or return None.

    These (IOHIDCheckAccess / IOHIDRequestAccess) are the canonical
    Input Monitoring APIs — calling IOHIDRequestAccess is what actually lists
    the app under System Settings > Privacy & Security > Input Monitoring.
    pyobjc doesn't expose them, so we bind them through ctypes.
    """
    import ctypes

    try:
        lib = ctypes.CDLL(_IOKIT_FRAMEWORK)
        lib.IOHIDCheckAccess.restype = ctypes.c_int
        lib.IOHIDCheckAccess.argtypes = [ctypes.c_uint32]
        lib.IOHIDRequestAccess.restype = ctypes.c_bool
        lib.IOHIDRequestAccess.argtypes = [ctypes.c_uint32]
        return lib
    except Exception as e:
        logger.debug("IOKit HID bind failed: %s", e)
        return None


def input_monitoring_active(prompt: bool = False) -> bool:
    """Authoritative Input Monitoring check that also registers the app.

    Primary path is IOKit HID: IOHIDCheckAccess reports the true grant state,
    and IOHIDRequestAccess (when prompt=True) registers the app in System
    Settings > Input Monitoring and shows the system prompt — this is the call
    that makes BetterFlow appear in the list so the user can toggle it. Falls
    back to a listen-only event-tap probe if IOKit can't be bound.

    Args:
        prompt: When True, request access (registers the app + shows prompt).

    Returns True on non-macOS platforms.
    """
    if not _IS_MACOS:
        return True

    access = None
    requested = None
    granted = False
    lib = _iokit_hid()
    if lib is not None:
        try:
            access = int(lib.IOHIDCheckAccess(_kIOHIDRequestTypeListenEvent))
            if access != _kIOHIDAccessTypeGranted and prompt:
                requested = bool(lib.IOHIDRequestAccess(_kIOHIDRequestTypeListenEvent))
                access = int(lib.IOHIDCheckAccess(_kIOHIDRequestTypeListenEvent))
            granted = access == _kIOHIDAccessTypeGranted
        except Exception as e:
            logger.debug("IOKit HID access check failed: %s", e)
            lib = None

    if lib is None:
        # Fallback: listen-only event-tap probe (also registers, less reliably).
        granted = _probe_listen_event_tap()

    # One-shot INFO diagnostic so the grant state + registration result are
    # visible in the log. Logged once per process to avoid spamming the poll.
    global _IM_DIAG_LOGGED
    if not _IM_DIAG_LOGGED:
        _IM_DIAG_LOGGED = True
        logger.info(
            "Input Monitoring diagnostics: hid_access=%s requested=%s granted=%s "
            "(0=granted,1=denied,2=unknown)",
            access, requested, granted,
        )

    return granted


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


def _tcc_grant_marker() -> Path:
    """Path to the marker file that records the TCC grant was already attempted."""
    try:
        from src.config import Config
    except ImportError:
        from config import Config
    return Config.get_data_dir() / ".tcc_grant_done"


def grant_tcc_permissions() -> bool:
    """Grant Accessibility and Input Monitoring via TCC database with admin auth.

    Shows the native macOS password dialog once on first install. Subsequent
    launches skip the prompt — if permissions are still missing, the user
    must grant them manually via System Settings.

    Returns True if the grant succeeded or was already attempted.
    """
    if not _IS_MACOS:
        return True

    marker = _tcc_grant_marker()
    if marker.exists():
        logger.debug("TCC grant already attempted, skipping admin prompt")
        return True

    services = []
    if not check_accessibility():
        services.append("kTCCServiceAccessibility")
    if not check_input_monitoring():
        services.append("kTCCServiceListenEvent")

    if not services:
        # Permissions already granted — write marker so we don't re-check.
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError as e:
            logger.debug("Could not write TCC marker: %s", e)
        return True

    # Defensive: refuse to interpolate anything we didn't hardcode ourselves.
    # Both _ALLOWED_TCC_SERVICES and _BUNDLE_ID_RE are checked even though
    # current callers only pass compile-time constants — this stops the
    # pattern from silently becoming injectable if someone later threads
    # user-controlled values through here.
    for svc in services:
        if svc not in _ALLOWED_TCC_SERVICES:
            logger.error("Refusing to grant unknown TCC service %r", svc)
            return False
    if not _BUNDLE_ID_RE.match(_BUNDLE_ID):
        logger.error("Refusing to grant TCC with malformed bundle id %r", _BUNDLE_ID)
        return False

    # Build SQL statements for each missing permission. Values are validated
    # above, so string interpolation is safe here.
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
    finally:
        # Mark the grant as attempted regardless of outcome so the user
        # is never prompted again on subsequent launches.
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError as exc:
            logger.debug("Could not write TCC marker: %s", exc)
