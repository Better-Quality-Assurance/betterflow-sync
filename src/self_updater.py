"""Self-update: download a new release, replace the running app, and relaunch.

Supports three formats: macOS uses DMG, Windows uses ZIP, Linux uses a single
.AppImage file replaced in place.
"""

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


def _get_app_bundle_path() -> Optional[Path]:
    """Return the path to the running app: .app bundle (macOS), .exe dir
    (Windows), or the .AppImage file (Linux).

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

    elif sys.platform.startswith("linux"):
        # AppImage sets $APPIMAGE to the real path of the .AppImage file
        # (sys.executable points into the read-only squashfs mount instead).
        appimage = os.environ.get("APPIMAGE")
        if appimage:
            return Path(appimage).resolve()
        return None

    return None


def apply_update(
    download_url: str,
    on_progress: Optional[Callable[[str], None]] = None,
    on_pre_exit: Optional[Callable[[], None]] = None,
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
        safe_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
        _status(f"Refusing non-HTTPS download URL: {safe_url}")
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
        is_appimage = url_path.endswith(".appimage")
        if is_appimage:
            dl_filename = "update.AppImage"
        elif is_dmg:
            dl_filename = "update.dmg"
        else:
            dl_filename = "update.zip"
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

        # Linux: the downloaded AppImage *is* the new app — replace in place,
        # no extraction or .app/.exe discovery needed.
        if sys.platform.startswith("linux"):
            return _install_appimage(dl_path, app_path, _status, on_pre_exit)

        # 2. Extract
        _status("Extracting...")
        extract_dir = tmp_dir / "extracted"
        extract_dir.mkdir()

        if is_dmg:
            _extract_from_dmg(dl_path, extract_dir)
        else:
            # ZIP extraction with Zip Slip protection — validate the
            # resolved path, then write to it explicitly (never let
            # zf.extract re-derive the path from the raw member name,
            # which could normalize differently on case-insensitive FS).
            extract_prefix = str(extract_dir.resolve()) + os.sep
            with zipfile.ZipFile(dl_path, "r") as zf:
                for member in zf.namelist():
                    member_path = (extract_dir / member).resolve()
                    if not str(member_path).startswith(extract_prefix):
                        raise ValueError(f"Zip entry escapes target directory: {member}")
                    if member.endswith("/"):
                        member_path.mkdir(parents=True, exist_ok=True)
                    else:
                        member_path.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as src, open(member_path, "wb") as dst:
                            shutil.copyfileobj(src, dst)

        # 3. Find the new .app or exe dir inside the extract
        if sys.platform == "darwin":
            new_app = _find_app_in(extract_dir)
            if new_app is None:
                _status("No .app found in update archive - aborted")
                return False

            # 3b. Verify code signature and downgrade protection
            if not _verify_codesign(new_app, current_app_path=app_path):
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
            except Exception:
                # Rollback on failure
                if backup_path.exists() and not app_path.exists():
                    backup_path.rename(app_path)
                raise
            # Best-effort permission fix — app is already in place so
            # don't rollback on a perms error (would leave no app at all).
            try:
                _fix_permissions(app_path)
            except Exception as e:
                logger.warning("Permission fix failed, update may not launch: %s", e)

            # Clean up backup
            shutil.rmtree(backup_path, ignore_errors=True)

            # 5. Flush pending data + relaunch
            if on_pre_exit:
                _status("Flushing data...")
                try:
                    on_pre_exit()
                except Exception as e:
                    logger.warning("Pre-exit flush failed: %s", e)
            _status("Restarting...")
            subprocess.Popen(["open", str(app_path)])
            # Exit current process — os._exit works from any thread,
            # unlike sys.exit which only raises SystemExit in the calling thread.
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
            # Validate paths contain no characters that could break batch quoting
            for p in (str(extract_dir), str(app_path), str(exe_path), str(tmp_dir)):
                if '"' in p or "%" in p:
                    _status("Update aborted: install path contains unsupported characters")
                    return False
            bat_content = '@echo off\r\n'
            bat_content += 'timeout /t 2 /nobreak >nul\r\n'
            bat_content += f'xcopy /E /Y /Q "{extract_dir}\\*" "{app_path}\\"\r\n'
            bat_content += f'start "" "{exe_path}"\r\n'
            bat_content += f'rd /s /q "{tmp_dir}"\r\n'
            bat_content += 'del "%~f0"\r\n'
            bat_path.write_text(bat_content)
            if on_pre_exit:
                try:
                    on_pre_exit()
                except Exception as e:
                    logger.warning("Pre-exit flush failed: %s", e)
            subprocess.Popen(
                ["cmd", "/c", str(bat_path)],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
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


def _install_appimage(
    new_appimage: Path,
    app_path: Path,
    status: Callable[[str], None],
    on_pre_exit: Optional[Callable[[], None]],
) -> bool:
    """Replace the running .AppImage in place and relaunch (Linux).

    A running AppImage can be safely renamed/replaced: the kernel keeps the
    executing inode alive (and the FUSE mount with it) until this process
    exits, so we move the old file aside, drop the new one in, and exec it.
    """
    # Sanity check: AppImages are ELF binaries (magic \x7fELF).
    try:
        with open(new_appimage, "rb") as f:
            if f.read(4) != b"\x7fELF":
                status("Downloaded file is not a valid AppImage - update aborted")
                return False
    except OSError as e:
        status(f"Could not read downloaded update: {e}")
        return False

    status("Installing...")
    new_appimage.chmod(0o755)

    # Use an explicit ".old" suffix on the full name (with_suffix would strip
    # the ".AppImage" extension).
    backup_path = Path(str(app_path) + ".old")
    if backup_path.exists():
        backup_path.unlink()

    app_path.rename(backup_path)
    try:
        shutil.move(str(new_appimage), str(app_path))
        app_path.chmod(0o755)
    except Exception:
        # Rollback so the user is never left without a runnable app.
        if backup_path.exists() and not app_path.exists():
            backup_path.rename(app_path)
        raise

    backup_path.unlink(missing_ok=True)

    if on_pre_exit:
        status("Flushing data...")
        try:
            on_pre_exit()
        except Exception as e:
            logger.warning("Pre-exit flush failed: %s", e)

    status("Restarting...")
    subprocess.Popen([str(app_path)])
    # os._exit works from any thread, unlike sys.exit.
    os._exit(0)


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
        # Mount DMG read-only, hidden from Finder.
        # NOTE: no -noverify — we want hdiutil to verify the DMG checksum at
        # mount time. Signature verification still happens post-extract.
        subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-readonly",
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
        if not str(dest).startswith(str(extract_dir.resolve()) + os.sep):
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


@dataclass(frozen=True)
class _SigningInfo:
    is_signed: bool
    team_id: Optional[str]
    version: Optional[str]


def _get_signing_info(app_path: Path) -> _SigningInfo:
    """Extract signing and version info from a macOS .app bundle."""
    is_signed = False
    team_id = None
    version = None

    # Get signing info via codesign
    try:
        result = subprocess.run(
            ["codesign", "--display", "--verbose=2", str(app_path)],
            capture_output=True, text=True, timeout=30,
        )
        stderr = result.stderr  # codesign writes to stderr
        if result.returncode == 0:
            is_signed = True
            # Extract TeamIdentifier from output like "TeamIdentifier=ABC123XYZ"
            match = re.search(r"TeamIdentifier=(\S+)", stderr)
            if match and match.group(1) != "not set":
                team_id = match.group(1)
        elif "code object is not signed at all" in stderr:
            is_signed = False
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("codesign probe failed: %s", e)

    # Get version from Info.plist
    plist_path = app_path / "Contents" / "Info.plist"
    if plist_path.exists():
        try:
            result = subprocess.run(
                ["defaults", "read", str(plist_path), "CFBundleShortVersionString"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                version = result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning("defaults read on Info.plist failed: %s", e)

    return _SigningInfo(is_signed=is_signed, team_id=team_id, version=version)


def _verify_codesign(app_path: Path, current_app_path: Optional[Path] = None) -> bool:
    """Verify macOS code signature on the extracted .app bundle.

    Checks:
    1. Signature integrity (tampered signatures rejected)
    2. Signed->unsigned downgrade rejected
    3. Team ID mismatch rejected
    4. Version downgrade rejected
    """
    try:
        result = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(app_path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("Code signature verified successfully")
        else:
            stderr = result.stderr.strip()
            if "code object is not signed at all" in stderr:
                logger.error("Rejecting update: new app is not signed")
                return False
            else:
                logger.error(f"codesign verification failed: {stderr}")
                return False
    except FileNotFoundError:
        logger.error("codesign binary not found - update aborted (cannot verify signature)")
        return False
    except subprocess.TimeoutExpired:
        logger.error("codesign verification timed out")
        return False

    # Downgrade protection: compare against current app if provided
    if current_app_path is not None:
        current = _get_signing_info(current_app_path)
        new = _get_signing_info(app_path)

        # Reject signed -> unsigned downgrade
        if current.is_signed and not new.is_signed:
            logger.error("Rejecting update: current app is signed but update is unsigned")
            return False

        # Reject team ID disappearance (could be a stripped/re-signed binary)
        if current.team_id and not new.team_id:
            logger.error("Rejecting update: current app has team ID but update does not")
            return False

        # Reject team ID mismatch
        if current.team_id and new.team_id and current.team_id != new.team_id:
            logger.error(
                f"Rejecting update: team ID mismatch (current={current.team_id}, new={new.team_id})"
            )
            return False

        # Reject version downgrade
        if current.version and new.version:
            try:
                from packaging.version import Version
                if Version(new.version) < Version(current.version):
                    logger.error(
                        f"Rejecting update: version downgrade ({current.version} -> {new.version})"
                    )
                    return False
            except Exception:
                # packaging not available or invalid version strings - compare as tuples
                try:
                    cur_parts = tuple(int(x) for x in current.version.split("."))
                    new_parts = tuple(int(x) for x in new.version.split("."))
                    if new_parts < cur_parts:
                        logger.error(
                            f"Rejecting update: version downgrade ({current.version} -> {new.version})"
                        )
                        return False
                except ValueError:
                    logger.warning("Could not compare versions, allowing update")

    return True


def apply_update_async(
    download_url: str,
    on_progress: Optional[Callable[[str], None]] = None,
    on_complete: Optional[Callable[[bool], None]] = None,
    on_pre_exit: Optional[Callable[[], None]] = None,
) -> None:
    """Run apply_update in a background thread."""

    def _run():
        result = apply_update(download_url, on_progress=on_progress, on_pre_exit=on_pre_exit)
        if on_complete:
            on_complete(result)

    thread = threading.Thread(target=_run, name="self-updater", daemon=True)
    thread.start()
