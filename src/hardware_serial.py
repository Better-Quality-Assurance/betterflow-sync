"""Hardware serial number probe — the fleet's join key to the MDM inventory.

The MDM (Miradore) identifies a laptop by its hardware serial; the agent
identifies itself as ``sync:<uuid>`` generated at first run. Nothing joined the
two, so "which agent devices are not managed?" could only be answered by fuzzy
name matching across two systems that disagree on name order — exactly the kind
of instrument that produces a confident wrong answer. Reporting the serial makes
that question a join.

Design notes that matter:

- **``None`` is a first-class value, not an error.** A VM, a container, a
  locked-down Linux box (``/sys/class/dmi/id/product_serial`` is usually
  root-only) and a plain failed probe all legitimately have no serial. Nothing
  here may raise into the sync or heartbeat path.
- **Probed once and cached, including a failure.** The serial cannot change for
  the life of the machine, so a per-heartbeat probe is pure waste — and a failed
  probe that retried would spawn a subprocess every heartbeat forever.
- This is hardware identification only. It says nothing about the person using
  the machine and never touches tracked or billed time.

See ``docs/superpowers/specs/2026-07-22-hardware-serial-reporting-design.md``.
"""

import logging
import platform
import subprocess
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Values firmware vendors and hypervisors write when no serial was ever
# programmed. They are placeholders, not identifiers, and joining on them would
# merge unrelated machines into one MDM row.
_PLACEHOLDER_SERIALS = {
    "",
    "0",
    "00000000",
    "default string",
    "none",
    "not applicable",
    "not available",
    "not specified",
    "n/a",
    "o.e.m.",
    "system serial number",
    "to be filled by o.e.m.",
    "unknown",
}

_LINUX_DMI_PATH = "/sys/class/dmi/id/product_serial"

# Guards the memo below. Probing is cheap but not free (a subprocess on
# Windows), and the heartbeat can be driven from two scheduler threads.
_lock = threading.Lock()
_cached: Optional[str] = None
_probed = False


def get_hardware_serial() -> Optional[str]:
    """Return this machine's hardware serial, or ``None``.

    Probed at most once per process. Never raises: any failure — unsupported
    platform, missing permission, absent firmware field — is a ``None``.
    """
    global _cached, _probed

    with _lock:
        if _probed:
            return _cached
        try:
            raw = _probe_serial()
        except Exception as e:  # noqa: BLE001 — a serial is never worth a crash
            logger.debug("hardware serial probe failed: %s", e)
            raw = None
        _cached = _normalise(raw)
        _probed = True
        if _cached is None:
            logger.debug("no hardware serial available on this machine")
        return _cached


def reset_cache_for_tests() -> None:
    """Drop the memo. Tests only — the serial never changes at runtime."""
    global _cached, _probed
    with _lock:
        _cached = None
        _probed = False


def _normalise(raw: Optional[str]) -> Optional[str]:
    """Trim, then reject firmware placeholders. Junk in, ``None`` out."""
    if raw is None:
        return None
    value = raw.strip()
    if not value or value.lower() in _PLACEHOLDER_SERIALS:
        return None
    return value


def _probe_serial() -> Optional[str]:
    """Dispatch to the per-platform probe. May raise; the caller absorbs it."""
    system = platform.system()
    if system == "Darwin":
        return _probe_macos()
    if system == "Windows":
        return _probe_windows()
    if system == "Linux":
        return _probe_linux()
    return None


def _probe_macos() -> Optional[str]:
    """Read ``IOPlatformSerialNumber`` off ``IOPlatformExpertDevice``.

    Straight IOKit via ctypes rather than pyobjc-framework-IOKit: it is the same
    registry read, needs no permission and no extra dependency in the bundle.
    """
    import ctypes
    import ctypes.util

    iokit_path = ctypes.util.find_library("IOKit")
    cf_path = ctypes.util.find_library("CoreFoundation")
    if not iokit_path or not cf_path:
        return None

    iokit = ctypes.CDLL(iokit_path)
    cf = ctypes.CDLL(cf_path)

    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32
    ]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32
    ]
    cf.CFRelease.argtypes = [ctypes.c_void_p]

    iokit.IOServiceMatching.restype = ctypes.c_void_p
    iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
    iokit.IOServiceGetMatchingService.restype = ctypes.c_uint32
    iokit.IOServiceGetMatchingService.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    iokit.IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
    iokit.IORegistryEntryCreateCFProperty.argtypes = [
        ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32
    ]
    iokit.IOObjectRelease.restype = ctypes.c_int
    iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]

    k_cf_string_encoding_utf8 = 0x08000100

    # IOServiceGetMatchingService CONSUMES the matching dictionary — do not
    # release it ourselves or this is a double-free.
    matching = iokit.IOServiceMatching(b"IOPlatformExpertDevice")
    if not matching:
        return None
    # kIOMainPortDefault is 0 (a.k.a. the deprecated kIOMasterPortDefault).
    service = iokit.IOServiceGetMatchingService(0, matching)
    if not service:
        return None

    key = None
    prop = None
    try:
        key = cf.CFStringCreateWithCString(
            None, b"IOPlatformSerialNumber", k_cf_string_encoding_utf8
        )
        if not key:
            return None
        prop = iokit.IORegistryEntryCreateCFProperty(service, key, None, 0)
        if not prop:
            return None
        buf = ctypes.create_string_buffer(128)
        if not cf.CFStringGetCString(
            prop, buf, ctypes.sizeof(buf), k_cf_string_encoding_utf8
        ):
            return None
        return buf.value.decode("utf-8", errors="replace")
    finally:
        if prop:
            cf.CFRelease(ctypes.c_void_p(prop))
        if key:
            cf.CFRelease(ctypes.c_void_p(key))
        iokit.IOObjectRelease(service)


def _probe_windows() -> Optional[str]:
    """Read ``Win32_BIOS.SerialNumber`` via WMI.

    Goes through PowerShell's CIM cmdlet rather than ``wmic``, which is
    deprecated and absent from recent Windows builds. No console window: this
    runs on the startup path of a tray app.
    """
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "(Get-CimInstance -ClassName Win32_BIOS).SerialNumber",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        startupinfo=startupinfo,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        logger.debug("Win32_BIOS query failed (rc=%s)", result.returncode)
        return None
    return result.stdout.strip()


def _probe_linux() -> Optional[str]:
    """Read the DMI product serial.

    Usually mode 0400 root-only, so a PermissionError here is the ordinary case
    on a hardened box, not an incident — it becomes ``None`` like any other
    unreadable serial.
    """
    try:
        with open(_LINUX_DMI_PATH, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except (PermissionError, FileNotFoundError, OSError) as e:
        logger.debug("DMI serial unreadable: %s", e)
        return None
