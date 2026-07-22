"""Minimal clipboard writer for the tray's copyable menu items.

No new dependency: this shells out to the platform's own clipboard tool. The
tray runs headless-ish inside pystray, and the obvious alternative — spinning a
tkinter root purely to reach ``clipboard_append`` — is unsafe off the main
thread on macOS. Small subprocess, no import-time cost, nothing to bundle.

``clipboard_available()`` exists so callers can decide whether to *offer* a copy
affordance at all. A menu item that looks clickable and silently does nothing is
worse than an inert one, and on Linux a clipboard tool genuinely may not be
installed (a bare Wayland/X session with neither wl-copy nor xclip nor xsel).
"""

import logging
import platform
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# Linux has no single answer; try Wayland first, then the two X11 classics.
# Each entry is (executable, full argv).
_LINUX_CANDIDATES = (
    ("wl-copy", ["wl-copy"]),
    ("xclip", ["xclip", "-selection", "clipboard"]),
    ("xsel", ["xsel", "--clipboard", "--input"]),
)


def _clipboard_command() -> Optional[list]:
    """Resolve the argv that writes stdin to the clipboard, or None."""
    system = platform.system()

    if system == "Darwin":
        # Absolute path: pbcopy is always here, and the agent's PATH under a
        # LaunchAgent is not the user's shell PATH.
        return ["/usr/bin/pbcopy"] if shutil.which("/usr/bin/pbcopy") else None

    if system == "Windows":
        exe = shutil.which("clip")
        return [exe] if exe else None

    if system == "Linux":
        for name, argv in _LINUX_CANDIDATES:
            if shutil.which(name):
                return argv
        return None

    return None


def clipboard_available() -> bool:
    """True when this machine has a clipboard tool we can drive."""
    return _clipboard_command() is not None


def copy_to_clipboard(text: str) -> bool:
    """Put ``text`` on the system clipboard. Returns success, never raises.

    A failed copy is a cosmetic disappointment, not an incident — the value is
    also rendered in the menu label the user just clicked.
    """
    cmd = _clipboard_command()
    if cmd is None:
        logger.debug("no clipboard tool available on this platform")
        return False

    kwargs = {}
    if platform.system() == "Windows":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        result = subprocess.run(
            cmd, input=text, text=True, capture_output=True, timeout=5, **kwargs
        )
    except Exception as e:  # noqa: BLE001 — a copy is never worth a crash
        logger.debug("clipboard write failed: %s", e)
        return False

    if result.returncode != 0:
        logger.debug("clipboard tool %s exited %s", cmd[0], result.returncode)
        return False
    return True
