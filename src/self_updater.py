"""Self-update: download a new release ZIP, replace the running .app, and relaunch."""

import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)


def _get_app_bundle_path() -> Optional[Path]:
    """Return the path to the running .app bundle (macOS) or .exe dir (Windows).

    Works for both PyInstaller bundles and dev mode.
    """
    exe = Path(sys.executable).resolve()

    if sys.platform == "darwin":
        # PyInstaller: /Applications/BetterFlow.app/Contents/MacOS/BetterFlow
        # Walk up to find the .app
        for parent in exe.parents:
            if parent.suffix == ".app":
                return parent
        return None

    elif sys.platform == "win32":
        # PyInstaller: dist/BetterFlow/BetterFlow.exe — the folder is the app
        if getattr(sys, "frozen", False):
            return Path(sys._MEIPASS).parent if hasattr(sys, "_MEIPASS") else exe.parent
        return None

    return None


def apply_update(
    download_url: str,
    on_progress: Optional[Callable[[str], None]] = None,
) -> bool:
    """Download, extract, replace, and relaunch.

    Args:
        download_url: Direct URL to the platform ZIP asset.
        on_progress: Optional callback(status_message) for UI feedback.

    Returns:
        True if update was applied (app will relaunch), False on failure.
    """

    def _status(msg: str) -> None:
        logger.info(f"Self-update: {msg}")
        if on_progress:
            on_progress(msg)

    app_path = _get_app_bundle_path()
    if app_path is None:
        _status("Cannot determine app location — update aborted")
        return False

    tmp_dir = None
    try:
        # 1. Download
        _status("Downloading update...")
        resp = requests.get(download_url, stream=True, timeout=120)
        resp.raise_for_status()

        tmp_dir = Path(tempfile.mkdtemp(prefix="betterflow-update-"))
        zip_path = tmp_dir / "update.zip"

        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(zip_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    pct = int(downloaded / total * 100)
                    _status(f"Downloading... {pct}%")

        # 2. Extract (with Zip Slip protection)
        _status("Extracting...")
        extract_dir = tmp_dir / "extracted"
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.namelist():
                member_path = (extract_dir / member).resolve()
                if not str(member_path).startswith(str(extract_dir.resolve())):
                    raise ValueError(f"Zip entry escapes target directory: {member}")
            zf.extractall(extract_dir)

        # 3. Find the new .app or exe dir inside the extract
        if sys.platform == "darwin":
            new_app = _find_app_in(extract_dir)
            if new_app is None:
                _status("No .app found in update archive — aborted")
                return False

            # 4. Replace: move old to trash, move new in place
            backup_path = app_path.parent / f"{app_path.stem}.old.app"
            if backup_path.exists():
                shutil.rmtree(backup_path)

            _status("Installing...")
            # Rename current -> backup
            app_path.rename(backup_path)
            try:
                # Move new -> install location
                shutil.move(str(new_app), str(app_path))
                # Preserve executable permissions
                _fix_permissions(app_path)
            except Exception:
                # Rollback on failure
                if backup_path.exists() and not app_path.exists():
                    backup_path.rename(app_path)
                raise

            # Clean up backup
            shutil.rmtree(backup_path, ignore_errors=True)

            # 5. Relaunch
            _status("Restarting...")
            subprocess.Popen(["open", str(app_path)])
            # Exit current process
            sys.exit(0)

        elif sys.platform == "win32":
            # Windows: extract contains BetterFlow.exe + supporting files
            # Use a bat script to replace after this process exits
            new_files = list(extract_dir.iterdir())
            if not new_files:
                _status("Empty update archive — aborted")
                return False

            bat_path = tmp_dir / "update.bat"
            exe_path = app_path / "BetterFlow.exe"
            # Paths are from tempfile.mkdtemp (safe) and validated app_path,
            # but quote defensively against spaces in install paths.
            bat_content = '@echo off\r\n'
            bat_content += 'timeout /t 2 /nobreak >nul\r\n'
            bat_content += f'xcopy /E /Y /Q "{extract_dir}\\*" "{app_path}\\"\r\n'
            bat_content += f'start "" "{exe_path}"\r\n'
            bat_content += f'rd /s /q "{tmp_dir}"\r\n'
            bat_content += 'del "%~f0"\r\n'
            bat_path.write_text(bat_content)
            subprocess.Popen(
                ["cmd", "/c", str(bat_path)],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            sys.exit(0)

        return True

    except Exception as e:
        _status(f"Update failed: {e}")
        logger.exception("Self-update failed")
        return False
    finally:
        # Clean up temp dir (except on Windows where bat script needs it)
        if tmp_dir and sys.platform != "win32":
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _find_app_in(directory: Path) -> Optional[Path]:
    """Find a .app bundle inside an extracted directory."""
    # Direct child
    for item in directory.iterdir():
        if item.suffix == ".app" and item.is_dir():
            return item
    # One level deeper (in case ZIP has a wrapper folder)
    for item in directory.iterdir():
        if item.is_dir():
            for sub in item.iterdir():
                if sub.suffix == ".app" and sub.is_dir():
                    return sub
    return None


def _fix_permissions(app_path: Path) -> None:
    """Ensure the main executable inside the .app has owner-execute permission."""
    macos_dir = app_path / "Contents" / "MacOS"
    if macos_dir.exists():
        for f in macos_dir.iterdir():
            if f.is_file():
                f.chmod(f.stat().st_mode | 0o100)  # owner-execute only
    # Also fix bundled tracker binaries
    resources = app_path / "Contents" / "Resources"
    if resources.exists():
        for tracker in resources.rglob("*"):
            if tracker.is_file() and not tracker.suffix:
                try:
                    tracker.chmod(tracker.stat().st_mode | 0o100)  # owner-execute only
                except Exception:
                    pass


def apply_update_async(
    download_url: str,
    on_progress: Optional[Callable[[str], None]] = None,
    on_complete: Optional[Callable[[bool], None]] = None,
) -> None:
    """Run apply_update in a background thread."""

    def _run():
        result = apply_update(download_url, on_progress=on_progress)
        if on_complete:
            on_complete(result)

    thread = threading.Thread(target=_run, name="self-updater", daemon=True)
    thread.start()
