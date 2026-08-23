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


# AXError code: the Accessibility API is disabled for this process (i.e. NOT
# granted). Any other result from an AXUIElement query means the AX subsystem
# is servicing our requests = granted. From <HIServices/AXError.h>.
_kAXErrorAPIDisabled = -25211


def _ax_api_works() -> bool:
    """Authoritative Accessibility test: exercise the real in-process AX API.

    Asks the system-wide AX element for the focused application — the same
    Accessibility machinery the window watcher relies on to read titles. If the
    grant is present the call is serviced (success, or no-value when nothing is
    focused); if it is absent macOS returns ``kAXErrorAPIDisabled``. This is the
    only honest practical probe: it tests *this process's own* Accessibility
    grant, and it succeeds even when ``AXIsProcessTrusted()`` returns a false
    negative for unsigned x86_64 binaries under Rosetta.

    Crucially it does NOT shell out to AppleScript/System Events — that path
    only proves *Automation* permission, so after a reinstall dropped the AX
    grant it reported Accessibility as present and the watcher silently emitted
    empty titles with no re-prompt.
    """
    try:
        from ApplicationServices import (
            AXUIElementCopyAttributeValue,
            AXUIElementCreateSystemWide,
        )

        system_wide = AXUIElementCreateSystemWide()
        err, _value = AXUIElementCopyAttributeValue(
            system_wide, "AXFocusedApplication", None,
        )
        return err != _kAXErrorAPIDisabled
    except Exception as e:
        logger.debug("AX system-wide probe failed: %s", e)
        return False


def check_accessibility() -> bool:
    """Check if Accessibility permission is granted.

    Tries pyobjc ``AXIsProcessTrusted`` first (most reliable in PyInstaller
    bundles), falls back to ctypes, then confirms a negative with a real
    in-process AX read (``_ax_api_works``).

    The final AX read is needed because ``AXIsProcessTrusted()`` can return
    False for unsigned x86_64 PyInstaller binaries running under Rosetta even
    when the System Settings toggle is ON. It replaces a former AppleScript
    probe that only exercised Automation permission and so falsely reported
    Accessibility as granted after a signature change dropped the grant.

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

    # AXIsProcessTrusted() said False — confirm against the real AX API rather
    # than trusting it (Rosetta false-negative) or AppleScript (Automation
    # false-positive). This is the authoritative, watcher-matching test.
    return _ax_api_works()


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

# Stamped into the TCC marker when the running version cannot be read at all.
# A constant, so it compares equal to itself: an agent that can never resolve
# its version still asks exactly once, not on every launch.
_UNKNOWN_AGENT_VERSION = "unknown"


def _tcc_grant_marker() -> Path:
    """Path to the marker file that records the TCC grant was already attempted."""
    try:
        from src.config import Config
    except ImportError:
        from config import Config
    return Config.get_data_dir() / ".tcc_grant_done"


def _agent_version() -> str:
    """The running agent version, used to stamp the TCC grant marker.

    Total by construction: every lookup is wrapped and the fallback is a
    constant, because a version we cannot read must never be able to raise out
    of a permission check. ``_UNKNOWN_AGENT_VERSION`` still behaves correctly as
    a stamp — it simply means the fuse re-arms only when the *reading* starts
    working, never spuriously.

    Mirrors the bundle-first order ``ui/tray.py`` uses: PyInstaller flattens
    ``src/`` to the root, so the relative import that works from a checkout is
    the one that fails in a shipped build.
    """
    try:
        import _build_info as _bi  # PyInstaller bundle (src/ is root)

        version = getattr(_bi, "APP_VERSION", None)
        if version:
            return str(version)
    except Exception:  # noqa: BLE001 - never raise out of a permission check
        pass
    try:
        from .. import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001
        pass
    try:
        from src import __version__  # type: ignore[no-redef]

        return str(__version__)
    except Exception:  # noqa: BLE001
        pass
    return _UNKNOWN_AGENT_VERSION


def _grant_attempt_is_armed(marker: Path, version: str) -> bool:
    """May this launch spend the user's one admin-password prompt?

    The marker records that we ASKED, and the fuse is per-VERSION rather than
    per-install. That is the whole of #205: written in a ``finally``, a
    cancelled prompt and a failed sqlite write both blew a fuse that nothing
    re-armed, so four devices sat ``window_titles_blind`` for 15-21 days having
    been asked exactly once, months earlier, on a build whose code signature no
    longer existed.

    Three states, and each direction was chosen for how it fails:

    * **No marker** -> armed. A fresh install must be able to ask.
    * **Marker stamped with a DIFFERENT version (or with nothing at all)** ->
      armed, once. This is the re-arm. The file's own header documents why an
      update is the right moment: a new build changes the code signature, macOS
      may keep showing the toggle ON while ``AXIsProcessTrusted()`` returns
      False, and the grant genuinely needs re-establishing. A legacy marker
      written by the old ``touch()`` carries no version and reads as "" here,
      so the already-blind fleet re-arms on upgrade rather than staying blind
      forever - which is the outcome this issue exists to produce.
    * **Marker stamped with THIS version** -> spent. This is the half that must
      not regress: the fuse exists so the admin password is asked for once, not
      on every launch. Trading a silent failure for a prompt loop is worse than
      the bug.

    An existing-but-unreadable marker reads as SPENT, not armed. We know an
    attempt happened (the file is there) and cannot tell which version made it;
    re-arming on an ``OSError`` we would hit again on the next write is how a
    single prompt becomes an infinite one. The honest state still reaches the
    user - the caller reports ``has_capture_permissions()`` and
    ``_maybe_warn_capture_permissions()`` names the missing grant.
    """
    if not marker.exists():
        return True
    try:
        stamped = marker.read_text(encoding="utf-8").strip()
    except OSError as e:
        logger.debug("Could not read TCC marker (%s); treating as spent", e)
        return False
    return stamped != version


def _record_grant_attempt(marker: Path, version: str) -> None:
    """Stamp the marker with the version that spent the attempt.

    Replaces a bare ``touch()``. The content is the entire re-arm mechanism: an
    empty marker cannot say WHICH build asked, so it can only ever mean "never
    ask again".
    """
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{version}\n", encoding="utf-8")
    except OSError as e:
        logger.debug("Could not write TCC marker: %s", e)


def has_capture_permissions() -> bool:
    """The single definition of "we hold the grants capture needs".

    Written three times before this existed — ``not A or not B`` at the
    ``main.py`` callsite, ``A and B`` in the marker branch below, and the pair
    again while building ``services``. De Morgan-equivalent, so nothing was
    broken; adding a third grant to one spelling and not the others is a
    one-line edit away, and it fails silently in both directions (#205).
    """
    return check_accessibility() and check_input_monitoring()


def grant_tcc_permissions() -> bool:
    """Grant Accessibility and Input Monitoring via TCC database with admin auth.

    Shows the native macOS password dialog **once per agent version**, not once
    per install. Later launches on the same version skip the prompt; the next
    update re-arms it exactly once. If permissions are still missing after
    that, the user grants them manually via System Settings.

    Returns True only when the permissions are actually held. The marker
    records that we ASKED, never that we succeeded: it is written in a finally,
    so a cancelled prompt and a failed sqlite write both set it. Two separate
    defects came out of that, and only the first is fixed by honesty alone.

    * Reporting "attempted" as True made every later launch claim a permission
      the process did not have. Four devices sat ``window_titles_blind`` for
      15-21 days with this answering True on each start (#205).
    * The record itself was permanent, so the single recovery attempt was spent
      at first install even when it FAILED. Version-stamping the marker is what
      re-arms it — see ``_grant_attempt_is_armed`` for why an update is the
      right moment and why "never write on failure" is not the fix (it turns
      one prompt into a prompt on every launch, which is worse).
    """
    if not _IS_MACOS:
        return True

    marker = _tcc_grant_marker()
    version = _agent_version()
    if not _grant_attempt_is_armed(marker, version):
        # Still no re-prompt — the fuse is spent for THIS version, so the admin
        # password is asked for once per build, not on every launch. Only the
        # ANSWER changes: report what is true right now instead of reporting
        # that we once tried.
        granted = has_capture_permissions()
        logger.debug(
            "TCC grant already attempted for %s, skipping admin prompt "
            "(granted=%s)",
            version,
            granted,
        )
        return granted

    services = []
    if not check_accessibility():
        services.append("kTCCServiceAccessibility")
    if not check_input_monitoring():
        services.append("kTCCServiceListenEvent")

    if not services:
        # Permissions already granted — stamp the marker so we don't re-check
        # until the next version, when the signature may have changed.
        _record_grant_attempt(marker, version)
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
            # returncode 0 means the sqlite write succeeded, NOT that this
            # process now holds the grant — macOS generally does not re-read TCC
            # for a running client. Ask, do not assume: this is the same claim
            # the marker branch above was fixed for, one branch down.
            logger.info("TCC permissions granted via admin auth")
            return has_capture_permissions()
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
        # Record the attempt regardless of outcome, so a cancelled prompt does
        # not re-ask on the very next launch — but stamp it with the VERSION,
        # which is what stops the record meaning "never ask again". The bare
        # touch() this replaces made a cancel permanent (#205).
        _record_grant_attempt(marker, version)
