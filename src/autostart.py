"""Platform-specific auto-start (login item) management."""

import logging
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

LAUNCHAGENT_LABEL = "co.betterqa.betterflow"


def set_auto_start(enabled: bool) -> bool:
    """Enable or disable auto-start at login.

    Returns True on success, False on failure.
    """
    system = platform.system()
    try:
        if system == "Darwin":
            return _set_macos(enabled)
        elif system == "Windows":
            return _set_windows(enabled)
        elif system == "Linux":
            return _set_linux(enabled)
        else:
            logger.warning(f"Auto-start not supported on {system}")
            return False
    except Exception as e:
        logger.warning(f"Failed to {'enable' if enabled else 'disable'} auto-start: {e}")
        return False


def get_auto_start() -> bool:
    """Check if auto-start is currently enabled at the OS level."""
    system = platform.system()
    try:
        if system == "Darwin":
            return _get_macos()
        elif system == "Windows":
            return _get_windows()
        elif system == "Linux":
            return _get_linux()
        else:
            return False
    except Exception:
        return False


def ensure_synced() -> None:
    """Re-bootstrap auto-start at startup if config says it should be on
    but the OS-level state has drifted (plist missing, agent not loaded,
    or plist path stale after a reinstall). No-op in dev mode."""
    if not getattr(sys, "frozen", False):
        return
    try:
        if get_auto_start():
            return  # already loaded with launchd
        if set_auto_start(True):
            logger.info("Auto-start re-synced at startup")
    except Exception as e:
        logger.warning(f"Auto-start sync failed (non-fatal): {e}")


# -- macOS: LaunchAgent plist --------------------------------------------------

def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHAGENT_LABEL}.plist"


def _app_launch_args() -> list[str]:
    """Determine the correct launch command for the current execution context."""
    exe = sys.executable
    # Running inside a .app bundle (PyInstaller)
    if ".app/Contents/MacOS/" in exe:
        # e.g. /Applications/BetterFlow Sync.app/Contents/MacOS/BetterFlow Sync
        # Use 'open -a' with the .app bundle path for a clean launch
        parts = exe.split(".app/Contents/MacOS/")
        bundle_path = parts[0] + ".app"
        return ["open", "-a", bundle_path]
    # Running as a Python script
    return [sys.executable, "-m", "src.main"]


def _launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(args: list[str]) -> tuple[int, str]:
    """Run launchctl and return (returncode, combined-output). Never raises."""
    try:
        proc = subprocess.run(
            ["launchctl", *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except Exception as e:
        return -1, str(e)


def _set_macos(enabled: bool) -> bool:
    import plistlib

    plist_file = _plist_path()
    target = f"{_launchctl_domain()}/{LAUNCHAGENT_LABEL}"

    if enabled and not getattr(sys, "frozen", False):
        # In dev mode `sys.executable` is the user's python and the plist
        # would point at `python -m src.main` from the wrong cwd. Refuse to
        # write garbage that would break the user's next reboot.
        logger.warning("Auto-start not supported in dev mode on macOS")
        return False

    if not enabled:
        # Unload first, then remove the file. bootout returns nonzero if the
        # agent isn't loaded; that's fine.
        rc, out = _launchctl(["bootout", target])
        if rc != 0 and out:
            logger.debug(f"launchctl bootout returned {rc}: {out}")
        if plist_file.exists():
            plist_file.unlink()
            logger.info(f"Removed LaunchAgent plist: {plist_file}")
        return True

    program_args = _app_launch_args()
    plist_data = {
        "Label": LAUNCHAGENT_LABEL,
        "ProgramArguments": program_args,
        "RunAtLoad": True,
        "KeepAlive": False,
    }

    plist_file.parent.mkdir(parents=True, exist_ok=True)
    with open(plist_file, "wb") as f:
        plistlib.dump(plist_data, f)
    logger.info(f"Wrote LaunchAgent plist: {plist_file}")

    # Bootstrap so the agent is active for the current session too, not just
    # after the next login. If already loaded, bootout first so the new plist
    # takes effect.
    _launchctl(["bootout", target])
    rc, out = _launchctl(["bootstrap", _launchctl_domain(), str(plist_file)])
    if rc == 0:
        logger.info(f"LaunchAgent loaded: {target}")
    else:
        # Not fatal: macOS will still pick the plist up on next login because
        # RunAtLoad=true. Log so the user knows the immediate load failed.
        logger.warning(f"launchctl bootstrap returned {rc}: {out}")
    return True


def _get_macos() -> bool:
    if not _plist_path().exists():
        return False
    # Confirm it's actually loaded with launchd, not just present on disk.
    rc, _ = _launchctl(["print", f"{_launchctl_domain()}/{LAUNCHAGENT_LABEL}"])
    return rc == 0


# -- Windows: Registry Run key ------------------------------------------------

_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WIN_VALUE_NAME = "BetterFlow"


def _set_windows(enabled: bool) -> bool:
    import winreg

    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE
    )
    try:
        if enabled:
            if not getattr(sys, "frozen", False):
                logger.warning("Auto-start not supported in dev mode on Windows")
                return False
            winreg.SetValueEx(key, _WIN_VALUE_NAME, 0, winreg.REG_SZ, sys.executable)
            logger.info("Added registry Run key for auto-start")
        else:
            try:
                winreg.DeleteValue(key, _WIN_VALUE_NAME)
                logger.info("Removed registry Run key for auto-start")
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(key)
    return True


def _get_windows() -> bool:
    import winreg

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_READ
        )
        try:
            winreg.QueryValueEx(key, _WIN_VALUE_NAME)
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except OSError:
        return False


# -- Linux: XDG autostart .desktop entry --------------------------------------


def _desktop_entry_path() -> Path:
    """Path to the freedesktop autostart entry (honors XDG_CONFIG_HOME)."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "autostart" / f"{LAUNCHAGENT_LABEL}.desktop"


def _linux_exec_command() -> Optional[str]:
    """Determine the command to relaunch the app at login.

    Running as an AppImage, $APPIMAGE is the real .AppImage path. Returns None
    in dev mode (where there's nothing stable to point launch at)."""
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return appimage
    if getattr(sys, "frozen", False):
        return sys.executable
    return None


def _set_linux(enabled: bool) -> bool:
    entry = _desktop_entry_path()

    if not enabled:
        if entry.exists():
            entry.unlink()
            logger.info(f"Removed autostart desktop entry: {entry}")
        return True

    if not getattr(sys, "frozen", False):
        # In dev mode there is no stable executable to point Exec= at.
        logger.warning("Auto-start not supported in dev mode on Linux")
        return False

    exec_cmd = _linux_exec_command()
    if not exec_cmd:
        logger.warning("Cannot determine Linux executable path for auto-start")
        return False

    # The desktop-entry spec requires reserved characters (incl. spaces) to be
    # double-quoted in the Exec field.
    exec_field = f'"{exec_cmd}"' if " " in exec_cmd else exec_cmd

    entry.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=BetterFlow\n"
        f"Exec={exec_field}\n"
        "Icon=betterflow\n"
        "Comment=Sync ActivityWatch data to BetterFlow\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    entry.write_text(content, encoding="utf-8")
    logger.info(f"Wrote autostart desktop entry: {entry}")
    return True


def _get_linux() -> bool:
    return _desktop_entry_path().exists()
