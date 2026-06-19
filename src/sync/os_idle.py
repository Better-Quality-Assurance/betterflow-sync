"""OS-level idle clock — seconds since the last real keyboard/mouse input.

This reads the operating system's own input-idle timer, independent of the
external bf-idle-tracker (a separate process / TCC subject that can freeze or go
blind). Two consumers rely on it as the safety net when bf-idle-tracker can't be
trusted:

  * IdleManager — the LOCAL pause decision (don't pause an active user).
  * SyncEngine  — synthesizing a not-afk span for the UPLOADED stream so the
    server (which bills active-vs-idle from that stream) doesn't count a worked
    span as idle while the tracker is frozen.

Shared here so the platform syscalls live in exactly one place.
"""

import logging
import sys
from typing import Optional

logger = logging.getLogger(__name__)


def get_system_idle_seconds() -> Optional[float]:
    """OS-level idle duration (seconds since last keyboard/mouse input).

    macOS: HIDIdleTime via ioreg. Windows: GetLastInputInfo via ctypes — this is
    Windows' safety net against a frozen bf-idle-tracker (Windows has no
    in-process input watcher), giving it the same real-activity signal macOS
    gets. Returns None on Linux / on any error.
    """
    if sys.platform == "darwin":
        try:
            import subprocess as _sp
            out = _sp.check_output(
                ["ioreg", "-c", "IOHIDSystem", "-d", "4"],
                timeout=5,
                text=True,
            )
            for line in out.splitlines():
                if "HIDIdleTime" in line and "=" in line:
                    val = line.split("=")[-1].strip()
                    try:
                        return int(val) / 1_000_000_000
                    except ValueError:
                        logger.debug(f"get_system_idle_seconds: unexpected HIDIdleTime value {val!r}")
                        return None
        except Exception as e:
            logger.debug(f"get_system_idle_seconds failed: {e}")
        return None

    if sys.platform == "win32":
        try:
            import ctypes

            class _LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

            info = _LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(info)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                return None
            # Both GetTickCount and dwTime are 32-bit ms tick counts; mask the
            # difference to 32 bits so it stays correct across the ~49.7-day
            # GetTickCount wraparound. Pin restype to unsigned so the value is
            # explicit (the mask makes the result correct either way, but this
            # avoids a signed-vs-unsigned footgun).
            ctypes.windll.kernel32.GetTickCount.restype = ctypes.c_uint32
            tick = ctypes.windll.kernel32.GetTickCount()
            elapsed_ms = (tick - info.dwTime) & 0xFFFFFFFF
            return elapsed_ms / 1000.0
        except Exception as e:
            logger.debug(f"get_system_idle_seconds (win) failed: {e}")
        return None

    return None
