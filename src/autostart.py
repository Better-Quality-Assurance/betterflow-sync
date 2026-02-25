"""Platform-specific auto-start (login item) management."""

import logging
import platform
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

LAUNCHAGENT_LABEL = "co.betterqa.betterflow-sync"


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
        else:
            return False
    except Exception:
        return False


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


def _set_macos(enabled: bool) -> bool:
    import plistlib

    plist_file = _plist_path()

    if not enabled:
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
    return True


def _get_macos() -> bool:
    return _plist_path().exists()


# -- Windows: Registry Run key ------------------------------------------------

_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WIN_VALUE_NAME = "BetterFlow Sync"


def _set_windows(enabled: bool) -> bool:
    import winreg

    key = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, winreg.KEY_SET_VALUE
    )
    try:
        if enabled:
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
