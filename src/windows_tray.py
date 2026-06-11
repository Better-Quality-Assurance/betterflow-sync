"""Windows-only: pin the BetterFlow tray icon to a stable GUID and lift it out
of the notification-area overflow (the "show hidden icons" ⌃ flyout) into the
always-visible part of the taskbar.

Background
----------
Windows decides tray-icon placement from a *per-icon identity*. pystray
registers its icon with no GUID and — because a stray ``hID=`` keyword is
silently dropped by ctypes (the struct field is ``uID``) — with ``uID == 0``.
That leaves no stable key for Windows to attach a placement preference to, so
the icon defaults into the hidden overflow on every launch.

This module:
  1. Patches pystray's win32 backend so every ``Shell_NotifyIcon`` call carries
     a fixed ``guidItem`` + ``NIF_GUID``. That gives Windows a stable
     ``IconGuid`` to remember.
  2. Once the icon is live, finds the matching entry under
     ``HKCU\\Control Panel\\NotifyIconSettings`` and sets ``IsPromoted = 1``.

Promotion via ``NotifyIconSettings`` is a Windows 11 mechanism. On Windows 10
the registry write is a harmless no-op, but the stable GUID still makes a user's
manual "show on taskbar" choice persist across restarts instead of resetting.

Everything here is best-effort: failing to patch or promote must never stop the
tray from appearing. Nothing is swallowed silently — failures are logged.
"""

from __future__ import annotations

import logging
import platform
import threading
import time
import uuid

logger = logging.getLogger(__name__)

# Fixed identity for the BetterFlow tray icon. NEVER change this once shipped —
# Windows keys the user's placement preference (and our IsPromoted write) on it,
# so a new value would orphan every existing install's saved choice.
TRAY_ICON_GUID = uuid.UUID("2b7a9d14-3c6e-4f8b-9a21-bf10c5e7d042")

# Guards the one-time monkeypatch install against concurrent callers.
_patch_lock = threading.Lock()


def is_supported() -> bool:
    """True only on Windows; every public entry point no-ops elsewhere."""
    return platform.system() == "Windows"


def _to_win_guid(u: uuid.UUID, guid_cls):
    """Convert a :class:`uuid.UUID` into pystray's ``NOTIFYICONDATAW.GUID``.

    GUID memory layout is mixed-endian: ``Data1/2/3`` are little-endian
    integers, ``Data4`` is the final 8 bytes in big-endian order. ``Data4`` is
    a signed ``BYTE`` array, so values above 127 are written as their signed
    equivalent to stay inside ctypes' range checks.
    """
    time_low, time_mid, time_hi, clk_hi, clk_lo, node = u.fields
    tail = [
        clk_hi,
        clk_lo,
        (node >> 40) & 0xFF,
        (node >> 32) & 0xFF,
        (node >> 24) & 0xFF,
        (node >> 16) & 0xFF,
        (node >> 8) & 0xFF,
        node & 0xFF,
    ]
    g = guid_cls()
    g.Data1 = time_low
    g.Data2 = time_mid
    g.Data3 = time_hi
    for i, b in enumerate(tail):
        g.Data4[i] = b - 256 if b >= 128 else b
    return g


def install_stable_guid() -> bool:
    """Patch pystray's win32 backend to register the icon with a fixed GUID.

    Idempotent and Windows-only. Must be called *before* the ``pystray.Icon`` is
    constructed (i.e. before ``TrayIcon.start()`` / ``run_blocking()``).

    Returns True if the patch is installed (or already was), False otherwise.
    """
    if not is_supported():
        return False

    with _patch_lock:
        try:
            from pystray import _win32
            from pystray._util import win32
        except Exception as e:
            logger.warning("Tray GUID patch skipped: pystray win32 backend unavailable (%s)", e)
            return False

        if getattr(_win32.Icon, "_bf_guid_installed", False):
            return True

        try:
            win_guid = _to_win_guid(TRAY_ICON_GUID, win32.NOTIFYICONDATAW.GUID)
        except Exception as e:
            logger.warning("Tray GUID patch skipped: could not build GUID struct (%s)", e)
            return False

        original_message = _win32.Icon._message

        def _message_with_guid(self, code, flags, **kwargs):
            # Carry the GUID on every message so add/modify/delete all identify
            # the icon the same way. pystray never sends NIM_SETVERSION, so the
            # WM_NOTIFY message packing it relies on is unchanged.
            kwargs.setdefault("guidItem", win_guid)
            if code == win32.NIM_ADD:
                # A crash or relaunch race can leave a stale icon registered
                # under this GUID; Shell_NotifyIcon(NIM_ADD) then silently fails
                # (pystray sets no errcheck on it) and the icon never appears.
                # Clear any stale registration first — failure here is expected
                # and harmless when nothing stale exists.
                try:
                    original_message(self, win32.NIM_DELETE, win32.NIF_GUID, guidItem=win_guid)
                except Exception as e:
                    logger.debug("Pre-add stale-GUID cleanup no-op: %s", e)
            return original_message(self, code, flags | win32.NIF_GUID, **kwargs)

        _win32.Icon._message = _message_with_guid
        _win32.Icon._bf_guid_installed = True
        logger.info("Tray icon registered with stable GUID {%s}", str(TRAY_ICON_GUID).upper())
        return True


def schedule_promotion(initial_delay: float = 3.0) -> None:
    """Promote the icon to the always-visible area, on a background thread.

    Windows creates the ``NotifyIconSettings`` entry a moment after the icon is
    added, so this retries with a short backoff. Windows-only; no-ops elsewhere.
    """
    if not is_supported():
        return

    def _worker() -> None:
        for attempt, delay in enumerate((initial_delay, 5.0, 10.0), start=1):
            time.sleep(delay)
            try:
                if _promote_once():
                    logger.info("Tray icon promoted to the always-visible taskbar area")
                    return
            except Exception as e:
                logger.warning("Tray promotion attempt %d failed: %s", attempt, e)
        logger.info(
            "Tray icon not auto-promoted (Windows 10, or no NotifyIconSettings "
            "entry yet); the user can drag it onto the taskbar to pin it."
        )

    threading.Thread(target=_worker, name="tray-promote", daemon=True).start()


def _current_executable() -> str | None:
    """Normalised path of the running executable, or None outside a frozen build.

    Only meaningful for the packaged app: in dev the executable is the Python
    interpreter, whose registry entry we must not touch — so we fall back to
    matching purely on GUID there.
    """
    import os
    import sys

    if getattr(sys, "frozen", False):
        try:
            return os.path.normcase(os.path.abspath(sys.executable))
        except Exception as e:
            logger.debug("Could not resolve frozen executable path: %s", e)
    return None


def _read_str(key, name: str) -> str | None:
    import winreg

    try:
        value, _type = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return value if isinstance(value, str) else None


def _matches(key, target_guid: str, exe: str | None) -> bool:
    """Whether a NotifyIconSettings subkey is our icon (by GUID, else exe path)."""
    import os

    icon_guid = _read_str(key, "IconGuid")
    if icon_guid is not None:
        return icon_guid.strip("{}").upper() == target_guid.strip("{}").upper()

    if exe is not None:
        exe_path = _read_str(key, "ExecutablePath")
        if exe_path is not None:
            return os.path.normcase(os.path.abspath(exe_path)) == exe
    return False


def _promote_once() -> bool:
    """Set ``IsPromoted=1`` on our NotifyIconSettings entry. True if one matched."""
    import winreg

    target_guid = str(TRAY_ICON_GUID).upper()
    exe = _current_executable()
    promoted = False

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Control Panel\NotifyIconSettings",
        0,
        winreg.KEY_READ | winreg.KEY_WRITE,
    ) as root:
        subkey_count = winreg.QueryInfoKey(root)[0]
        for idx in range(subkey_count):
            try:
                sub_name = winreg.EnumKey(root, idx)
            except OSError:
                break  # enumeration ran past the end (entries changed under us)
            with winreg.OpenKey(
                root, sub_name, 0, winreg.KEY_READ | winreg.KEY_WRITE
            ) as sub:
                if _matches(sub, target_guid, exe):
                    winreg.SetValueEx(sub, "IsPromoted", 0, winreg.REG_DWORD, 1)
                    promoted = True

    return promoted
