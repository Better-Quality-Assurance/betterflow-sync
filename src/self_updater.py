"""Self-update: download a new release, replace the running .app, and relaunch.

Supports both ZIP and DMG formats (macOS uses DMG, Windows uses ZIP).
"""

import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

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
        # PyInstaller: dist/BetterFlow/BetterFlow.exe - the folder is the app
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
        download_url: Direct URL to the platform asset (DMG on macOS, ZIP on Windows).
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
        _status("Cannot determine app location - update aborted")
        return False

    # Reject non-HTTPS download URLs to prevent MITM attacks
    parsed_url = urlparse(download_url)
    if parsed_url.scheme != "https":
        _status(f"Refusing non-HTTPS download URL: {download_url}")
        return False

    tmp_dir = None
    try:
        # 1. Download
        _status("Downloading update...")
        resp = requests.get(download_url, stream=True, timeout=120)
        resp.raise_for_status()

        tmp_dir = Path(tempfile.mkdtemp(prefix="betterflow-update-"))
        # Parse URL path to detect format (handles query params like ?token=abc)
        url_path = urlparse(download_url).path.lower()
        is_dmg = url_path.endswith(".dmg")
        dl_filename = "update.dmg" if is_dmg else "update.zip"
        dl_path = tmp_dir / dl_filename

        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(dl_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    pct = int(downloaded / total * 100)
                    _status(f"Downloading... {pct}%")

        # 2. Extract
        _status("Extracting...")
        extract_dir = tmp_dir / "extracted"
        extract_dir.mkdir()

        if is_dmg:
            _extract_from_dmg(dl_path, extract_dir)
        else:
            # ZIP extraction with Zip Slip protection
            with zipfile.ZipFile(dl_path, "r") as zf:
                for member in zf.namelist():
                    member_path = (extract_dir / member).resolve()
                    if not str(member_path).startswith(str(extract_dir.resolve())):
                        raise ValueError(f"Zip entry escapes target directory: {member}")
                zf.extractall(extract_dir)

        # 3. Find the new .app or exe dir inside the extract
        if sys.platform == "darwin":
            new_app = _find_app_in(extract_dir)
            if new_app is None:
                _status("No .app found in update archive - aborted")
                return False

            # 3b. Verify code signature before installing
            if not _verify_codesign(new_app):
                _status("Code signature verification failed - update aborted")
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
            # Exit current process — os._exit works from any thread,
            # unlike sys.exit which only raises SystemExit in the calling thread.
            import os
            os._exit(0)

        elif sys.platform == "win32":
            # Windows: extract contains BetterFlow.exe + supporting files
            # Use a bat script to replace after this process exits
            new_files = list(extract_dir.iterdir())
            if not new_files:
                _status("Empty update archive - aborted")
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
            import os
            os._exit(0)

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


def _extract_from_dmg(dmg_path: Path, extract_dir: Path) -> None:
    """Mount a DMG, copy the .app out, and unmount."""
    mount_point = Path(tempfile.mkdtemp(prefix="betterflow-dmg-mount-"))
    try:
        # Mount DMG read-only, hidden from Finder
        subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-readonly", "-noverify",
             "-mountpoint", str(mount_point), str(dmg_path)],
            capture_output=True, text=True, timeout=60, check=True,
        )
        # Find .app inside mounted volume
        app_found = None
        for item in mount_point.iterdir():
            if item.suffix == ".app" and item.is_dir():
                app_found = item
                break
        if app_found is None:
            raise FileNotFoundError("No .app found in DMG")
        # Copy .app to extract directory (with path traversal guard)
        dest = (extract_dir / app_found.name).resolve()
        if not str(dest).startswith(str(extract_dir.resolve())):
            raise ValueError(f"DMG app name escapes extract directory: {app_found.name}")
        shutil.copytree(str(app_found), str(dest), symlinks=True)
        logger.info(f"Extracted {app_found.name} from DMG")
    finally:
        # Always attempt to detach, then clean up the mount point directory
        try:
            result = subprocess.run(
                ["hdiutil", "detach", str(mount_point), "-quiet"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                logger.error(
                    f"hdiutil detach failed (rc={result.returncode}): {result.stderr.strip()}"
                )
                raise RuntimeError("Failed to detach DMG mount - aborting update")
        except subprocess.TimeoutExpired as e:
            logger.error("hdiutil detach timed out - DMG may still be mounted")
            raise RuntimeError("Failed to detach DMG mount - aborting update") from e
        finally:
            # Remove the mount point directory itself
            shutil.rmtree(mount_point, ignore_errors=True)


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
                    logger.warning(f"Failed to set execute permission on {tracker}")


def _verify_codesign(app_path: Path) -> bool:
    """Verify macOS code signature on the extracted .app bundle.

    Allows unsigned apps (unsigned-to-unsigned update is fine).
    Only rejects apps with an invalid/tampered signature.
    """
    try:
        result = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(app_path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("Code signature verified successfully")
            return True
        stderr = result.stderr.strip()
        # "code object is not signed at all" means unsigned - acceptable for unsigned-to-unsigned
        if "code object is not signed at all" in stderr:
            logger.info("App is unsigned - allowing update (unsigned-to-unsigned)")
            return True
        logger.error(f"codesign verification failed: {stderr}")
        return False
    except FileNotFoundError:
        logger.warning("codesign binary not found - skipping verification")
        return True
    except subprocess.TimeoutExpired:
        logger.error("codesign verification timed out")
        return False


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
