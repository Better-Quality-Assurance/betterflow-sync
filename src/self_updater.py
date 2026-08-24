"""Self-update: download a new release, replace the running app, and relaunch.

Supports three formats: macOS uses DMG, Windows uses ZIP, Linux uses a single
.AppImage file replaced in place.

Two delivery paths:
- apply_update(url): download then apply immediately (manual "Install & Restart"
  and catch-on-launch).
- stage_update(url, version) + apply_staged_update(): download in the background
  during a session, then apply the staged artifact on next launch / idle.
  Staging is loop-safe — a staged build is only applied when STRICTLY newer than
  the running version, and staging is always cleared before applying so a failed
  or no-op apply can never re-trigger.
"""

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import requests

try:
    from .config import Config
    from .sync.http_client import resolve_ca_bundle
    from .update_checker import _version_tuple
except ImportError:  # PyInstaller bundle (src/ is import root)
    from config import Config
    from sync.http_client import resolve_ca_bundle
    from update_checker import _version_tuple

logger = logging.getLogger(__name__)

# Our bundle id. Already spelled in src/autostart.py (as the launchd label,
# which happens to be the same string) and src/ui/permissions.py; a guard test
# pins all three rather than a refactor threading one constant through modules
# that have no other reason to depend on each other.
BUNDLE_ID = "co.betterqa.betterflow"

# What _apply_macos_update leaves aside mid-replace. Spelled once because the
# sweep below has to recognise exactly what that function creates: change the
# writer and an independent copy in the sweep silently stops matching it, in the
# reassuring direction ("nothing to clean up") with no test failing.
_MACOS_BACKUP_SUFFIX = ".old.app"

# The bundle stem every shipped build installs under (build.spec's CFBundleName).
# The sweep derives its patterns from the RUNNING bundle's stem, so running from
# anything else means it cannot see the canonical copy's siblings.
_CANONICAL_STEM = "BetterFlow"

# Names our updater has left in /Applications across versions. Each is a
# FORMAT over the running bundle's stem, so the sweep can only ever match a
# sibling of the app that is running.
#
# Deliberately closed rather than a glob. A leftover copy costs disk; a wrong
# deletion costs somebody an application, so anything outside the shapes we can
# show we produced stays put:
#   <stem>.app.old   the Linux AppImage form, seen on a Mac in #211
#   <stem>.old.app   what _apply_macos_update creates today
#   <stem>-<ver>-backup.app   provenance unknown, seen on the #211 device

# The only Apple Developer ID team we ever ship production releases under.
# Pinned to make _verify_codesign reject updates signed by any other team,
# even on a fresh install with no prior team-ID context to compare against.
EXPECTED_TEAM_ID = "87NVC57J44"  # Better Quality Assurance SRL

# Hard cap on a downloaded release artifact. Our installers are ~60-80 MB;
# 500 MB leaves generous headroom while stopping a runaway/poisoned response
# from filling the disk.
_MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024


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
        # PyInstaller one-dir: <install>\BetterFlow\BetterFlow.exe — the folder
        # holding the exe IS the app. exe.parent is correct regardless of the
        # PyInstaller layout (one-dir puts binaries under _internal\, so
        # sys._MEIPASS points there, not at the install root). For the now-retired
        # one-file build sys._MEIPASS was the %TEMP%\_MEI dir, which made the old
        # _MEIPASS.parent resolution silently target %TEMP% instead of the app.
        if getattr(sys, "frozen", False):
            return exe.parent
        return None

    elif sys.platform.startswith("linux"):
        # AppImage sets $APPIMAGE to the real path of the .AppImage file
        # (sys.executable points into the read-only squashfs mount instead).
        appimage = os.environ.get("APPIMAGE")
        if appimage:
            return Path(appimage).resolve()
        return None

    return None


def app_bundle_replaceable() -> bool:
    """True if the current user can replace the running app in place.

    The self-update applies by renaming the bundle within its parent
    (``BetterFlow.app`` -> ``BetterFlow.old.app``) then moving the new one in. A
    bundle installed by the MDM ``.pkg`` lands in ``/Applications`` owned by
    **root**, so a user-context updater gets ``[Errno 13] Permission denied`` on
    that rename — every 30-min cycle, silently, forever (Tudor/Fabian, 2026-07).
    The updater cannot fix that itself; the caller must fail loud (tell the user
    to reinstall / route updates via MDM) rather than loop on a doomed attempt.

    macOS only: the root-owned ``.pkg`` case is macOS-specific, and the Windows /
    Linux paths differ (exe-dir swap / AppImage replace), so this returns True
    there. Returns True when the layout is unknown (dev/source run) so it never
    blocks a normal install; a genuine permission failure is still caught in the
    apply path.
    """
    if sys.platform != "darwin":
        return True
    app_path = _get_app_bundle_path()
    if app_path is None:
        return True
    try:
        bundle_uid = app_path.stat().st_uid
    except OSError:
        return True
    my_uid = os.getuid()
    if my_uid == 0:
        return True
    # Ownership is the reliable signal: a bundle we don't own can't be renamed by
    # us (the observed EPERM). /Applications is group-writable for admins, so an
    # os.access() on the parent would miss the root-owned case.
    return bundle_uid == my_uid


# Set once per process so the "install properly" nag isn't repeated on every
# stage / staged-apply attempt within a single run.
_downloads_warning_sent = False


def _running_from_downloads(app_path: Optional[Path]) -> bool:
    """True when the running app lives inside a Downloads folder.

    Windows can't reliably replace a locked .exe in place, so a self-update
    applied to an app that was unzipped-and-run from Downloads silently fails
    to persist: the next cold start comes back on the bundled (often ancient)
    version and re-applies the update, churning forever. Observed on Sachi's
    device (16), 2026-07-02 — stuck bouncing back to 1.5.29 out of
    ``C:\\Users\\Administrator\\Downloads\\BetterFlow-Windows (1)``.

    Matches both the standard ``~/Downloads`` and any path with a ``Downloads``
    component (a relocated-but-still-named Downloads folder).
    """
    if app_path is None:
        return False
    try:
        if any(part.lower() == "downloads" for part in app_path.parts):
            return True
        downloads = (Path.home() / "Downloads").resolve()
        resolved = app_path.resolve()
        return resolved == downloads or downloads in resolved.parents
    except Exception as e:
        # Fail OPEN (allow the update) but never silently: if path resolution
        # throws on the affected win32 machine, the guard would quietly disable
        # itself and the churn loop it exists to stop resumes with no trace.
        logger.debug("Downloads-path check failed, allowing update: %s", e, exc_info=True)
        return False


def _warn_downloads_install(
    app_path: Path, on_progress: Optional[Callable[[str], None]]
) -> None:
    """Log, surface status, and (once per process) notify the user that a
    Downloads install can't self-update and needs to be installed properly."""
    global _downloads_warning_sent
    logger.warning(
        "self-update: app is running from a Downloads folder (%s); an in-place "
        "update can't persist there and would revert on next launch. Skipping "
        "the update and asking the user to install to a stable location.",
        app_path,
    )
    if on_progress:
        on_progress("Install BetterFlow outside Downloads to get updates")
    if _downloads_warning_sent:
        return
    _downloads_warning_sent = True
    try:
        try:
            from .notifications import send_notification
        except ImportError:
            from notifications import send_notification
        send_notification(
            "BetterFlow can't update itself",
            "You're running BetterFlow from your Downloads folder, so updates "
            "won't stick. Please install it to a permanent location and delete "
            "the copies in Downloads.",
        )
    except Exception:
        logger.debug("Downloads-install notification failed", exc_info=True)


def _artifact_filename_from_url(download_url: str) -> str:
    """Derive a safe local filename (keeping the real extension) from the URL.

    The extension drives format detection in _apply_local_artifact, so we keep
    the asset's real name (e.g. BetterFlow-macOS-arm64.dmg) but strip any path
    components to prevent traversal.
    """
    name = os.path.basename(urlparse(download_url).path).replace("\\", "").strip()
    return name or "update.bin"


def _download_to_file(
    download_url: str,
    dest: Path,
    on_progress: Optional[Callable[[str], None]] = None,
) -> None:
    """Stream a download to ``dest``, reporting percent progress.

    Caps the body at ``_MAX_DOWNLOAD_BYTES`` so a misconfigured redirect or a
    malicious release asset can't fill the disk (our installers are ~60-80 MB).
    """
    # Pin TLS verification to a resolved CA bundle: a missing/clipped certifi
    # copy must not also break the self-updater, or the app can't download its
    # own fix. resolve_ca_bundle() returns None only when no bundle exists, in
    # which case verify=None falls back to the requests default (logged loudly).
    try:
        from .url_safety import assert_safe_final_url
    except ImportError:
        from url_safety import assert_safe_final_url

    with requests.get(
        download_url, stream=True, timeout=120, verify=resolve_ca_bundle()
    ) as resp:
        resp.raise_for_status()
        # requests follows redirects by default, so the caller's allowlist check
        # only gated the first hop. Re-validate the URL we actually landed on
        # (GitHub redirects assets to objects.githubusercontent.com) before
        # reading a byte of the body.
        assert_safe_final_url(resp.url, "Update download")
        try:
            # `or 0` guards an empty-string header; the except guards garbage.
            total = int(resp.headers.get("content-length", 0) or 0)
        except (ValueError, TypeError):
            total = 0
        if total > _MAX_DOWNLOAD_BYTES:
            raise ValueError(
                f"Refusing to download {total} bytes (cap {_MAX_DOWNLOAD_BYTES})"
            )
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                f.write(chunk)
                downloaded += len(chunk)
                if downloaded > _MAX_DOWNLOAD_BYTES:
                    raise ValueError(
                        f"Download exceeded {_MAX_DOWNLOAD_BYTES} bytes; aborting"
                    )
                if total > 0 and on_progress:
                    on_progress(f"Downloading... {int(downloaded / total * 100)}%")


def apply_update(
    download_url: str,
    on_progress: Optional[Callable[[str], None]] = None,
    on_pre_exit: Optional[Callable[[], None]] = None,
) -> bool:
    """Download a release artifact, then replace the running app and relaunch.

    Args:
        download_url: Direct HTTPS URL to the platform asset (DMG / ZIP / AppImage).
        on_progress: Optional callback(status_message) for UI feedback.

    Returns:
        True if the update was applied (app will relaunch), False on failure.
    """

    def _status(msg: str) -> None:
        logger.info(f"Self-update: {msg}")
        if on_progress:
            on_progress(msg)

    # Reject download URLs that aren't HTTPS from a known GitHub host (MITM /
    # spoofed-asset defense). The asset URL comes from the GitHub releases API,
    # so a tampered response can't redirect the download off-GitHub.
    try:
        from .url_safety import is_safe_fetch_url
    except ImportError:
        from url_safety import is_safe_fetch_url
    if not is_safe_fetch_url(download_url):
        parsed_url = urlparse(download_url)
        safe_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
        _status(f"Refusing unsafe download URL (must be HTTPS from GitHub): {safe_url}")
        return False

    tmp_dir = Path(tempfile.mkdtemp(prefix="betterflow-update-"))
    try:
        _status("Downloading update...")
        dl_path = tmp_dir / _artifact_filename_from_url(download_url)
        _download_to_file(download_url, dl_path, on_progress)
        return _apply_local_artifact(dl_path, on_progress, on_pre_exit)
    except Exception as e:
        _status(f"Update failed: {e}")
        logger.exception("Self-update failed")
        return False
    finally:
        # On Windows the bat script (spawned inside _apply_local_artifact) needs
        # its own temp dir; this download dir is separate and safe to remove.
        if sys.platform != "win32":
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _apply_local_artifact(
    artifact_path: Path,
    on_progress: Optional[Callable[[str], None]] = None,
    on_pre_exit: Optional[Callable[[], None]] = None,
) -> bool:
    """Replace the running app from an already-downloaded artifact and relaunch.

    Format is detected from the artifact's extension (.dmg / .AppImage / .zip).
    On success the process relaunches and never returns; on any failure it
    returns False so the caller can continue running the current version.
    """

    def _status(msg: str) -> None:
        logger.info(f"Self-update: {msg}")
        if on_progress:
            on_progress(msg)

    app_path = _get_app_bundle_path()
    # Instrumentation for the 2026-06-17 relaunch-failure investigation: the
    # update reached "Installing/Restarting" but /Applications was never updated
    # and the new process never started, and the cause isn't reproducible by
    # inspection. Log exactly where we resolved the bundle so a canary shows
    # whether app_path points at the real install or somewhere unexpected.
    logger.info("self-update: sys.executable=%s resolved app_path=%s", sys.executable, app_path)
    if app_path is None:
        _status("Cannot determine app location - update aborted")
        return False

    # Refuse to apply into a Downloads folder on Windows: the in-place replace
    # can't persist a locked .exe there, so the update silently reverts on the
    # next launch and the app churns re-applying it forever (Sachi, device 16).
    # Sitting still on the current version + nagging to install properly beats
    # an endless relaunch loop. macOS/Linux replace the whole bundle/AppImage
    # atomically, so they aren't affected.
    if sys.platform == "win32" and _running_from_downloads(app_path):
        _warn_downloads_install(app_path, on_progress)
        return False

    is_dmg = artifact_path.name.lower().endswith(".dmg")

    # Linux: the AppImage *is* the new app — replace in place, no extraction.
    if sys.platform.startswith("linux"):
        return _install_appimage(artifact_path, app_path, _status, on_pre_exit)

    tmp_dir = Path(tempfile.mkdtemp(prefix="betterflow-apply-"))
    try:
        # 1. Extract
        _status("Extracting...")
        extract_dir = tmp_dir / "extracted"
        extract_dir.mkdir()

        if is_dmg:
            _extract_from_dmg(artifact_path, extract_dir)
        else:
            # ZIP extraction with Zip Slip protection — validate the
            # resolved path, then write to it explicitly (never let
            # zf.extract re-derive the path from the raw member name,
            # which could normalize differently on case-insensitive FS).
            extract_prefix = str(extract_dir.resolve()) + os.sep
            with zipfile.ZipFile(artifact_path, "r") as zf:
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

        # 2. Find the new .app or exe dir inside the extract
        if sys.platform == "darwin":
            new_app = _find_app_in(extract_dir)
            if new_app is None:
                _status("No .app found in update archive - aborted")
                return False

            # 2b. Verify code signature and downgrade protection
            if not _verify_codesign(new_app, current_app_path=app_path):
                _status("Code signature verification failed - update aborted")
                return False

            # 3. Replace: move old aside, move new in place
            backup_path = app_path.parent / f"{app_path.stem}{_MACOS_BACKUP_SUFFIX}"
            if backup_path.exists():
                shutil.rmtree(backup_path)

            _status("Installing...")
            logger.info("self-update: moving %s aside to %s", app_path, backup_path)
            app_path.rename(backup_path)
            try:
                shutil.move(str(new_app), str(app_path))
            except Exception:
                # Rollback on failure
                if backup_path.exists() and not app_path.exists():
                    backup_path.rename(app_path)
                raise
            # Instrumentation: confirm the new bundle actually landed at app_path
            # (the 06-17 failure left the old app in place with no error logged).
            logger.info(
                "self-update: moved new app to %s (exists=%s)",
                app_path,
                app_path.exists(),
            )
            # Best-effort permission fix — app is already in place so
            # don't rollback on a perms error (would leave no app at all).
            try:
                _fix_permissions(app_path)
            except Exception as e:
                logger.warning("Permission fix failed, update may not launch: %s", e)

            shutil.rmtree(backup_path, ignore_errors=True)

            # 4. Flush pending data + relaunch
            if on_pre_exit:
                _status("Flushing data...")
                try:
                    on_pre_exit()
                except Exception as e:
                    logger.warning("Pre-exit flush failed: %s", e)
            _status("Restarting...")
            # `open` on a still-running app reactivates the current
            # (about-to-exit) instance instead of launching a new one — so the
            # app would vanish and never reopen after a self-update. We wait for
            # THIS process to die, then open a fresh instance, retrying a few
            # times in case Gatekeeper is still settling. Detached via
            # start_new_session so the helper survives our os._exit below.
            #
            # CRITICAL instrumentation: the relaunch runs AFTER os._exit, so the
            # main log can't capture whether `open` actually worked — which is
            # exactly the unknown in the 06-17 outage (the new process never
            # logged a line). The helper therefore writes its own outcome (each
            # open attempt + exit code) to self-update-relaunch.log, which is the
            # ground truth a canary needs.
            try:
                relaunch_log = str(Config.get_log_dir() / "self-update-relaunch.log")
            except Exception:
                relaunch_log = "/tmp/betterflow-self-update-relaunch.log"
            script = _build_macos_relaunch_script(str(app_path), os.getpid(), relaunch_log)
            subprocess.Popen(["/bin/sh", "-c", script], start_new_session=True)
            # os._exit works from any thread, unlike sys.exit which only raises
            # SystemExit in the calling thread.
            os._exit(0)

        elif sys.platform == "win32":
            # Windows: extract contains BetterFlow.exe + supporting files.
            # Use a bat script to replace after this process exits.
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
            # Switch the console to UTF-8 so non-ASCII install paths (e.g.
            # C:\Users\André\...) survive. Paired with a utf-8 byte write below;
            # write_text's locale default (cp1252) would mangle or crash on them.
            bat_content += 'chcp 65001 >nul\r\n'
            bat_content += 'timeout /t 2 /nobreak >nul\r\n'
            bat_content += f'xcopy /E /Y /Q "{extract_dir}\\*" "{app_path}\\"\r\n'
            bat_content += f'start "" "{exe_path}"\r\n'
            bat_content += f'rd /s /q "{tmp_dir}"\r\n'
            bat_content += 'del "%~f0"\r\n'
            bat_path.write_bytes(bat_content.encode("utf-8"))
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
        logger.exception("Self-update apply failed")
        return False
    finally:
        # Clean up temp dir (except on Windows where the bat script needs it)
        if sys.platform != "win32":
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
    # start_new_session detaches the relaunched AppImage from our process
    # group so the session manager's SIGHUP (after os._exit below) can't kill
    # it before it initializes — same reason the macOS relaunch helper does it.
    subprocess.Popen([str(app_path)], start_new_session=True)
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
            # Use (.+) not (\S+) so ad-hoc bundles ("TeamIdentifier=not set") are
            # captured in full; the strip()+guard then correctly leaves team_id=None.
            match = re.search(r"TeamIdentifier=(.+)", stderr)
            if match:
                raw = match.group(1).strip()
                if raw != "not set":
                    team_id = raw
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


def _codesign_verify(app_path: Path) -> bool:
    """Run `codesign --verify --deep --strict` and return True on success."""
    try:
        result = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(app_path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("Code signature verified successfully")
            return True
        stderr = result.stderr.strip()
        if "code object is not signed at all" in stderr:
            logger.error("Rejecting update: new app is not signed")
        else:
            logger.error(f"codesign verification failed: {stderr}")
        return False
    except FileNotFoundError:
        logger.error("codesign binary not found - update aborted (cannot verify signature)")
        return False
    except subprocess.TimeoutExpired:
        logger.error("codesign verification timed out")
        return False


def _build_macos_relaunch_script(app_path: str, pid: int, log_path: str) -> str:
    """Shell helper run (detached) after os._exit to reopen the app post-update.

    It waits for THIS process to die, opens the new bundle, and — crucially —
    VERIFIES a real process actually came up before trusting it, retrying the
    open if not. `open` returning 0 means LaunchServices ACCEPTED the request,
    NOT that the app started: it can silently no-op (or the new instance can die
    on startup) leaving NO running process while the helper logs "open OK" and
    exits. That is the 6.5h black hole Botond hit (2026-06-25): a 09:18 relaunch
    logged "open OK" but the agent never synced until the next relaunch at 15:35.
    `open` is idempotent (it just reactivates an already-running app), so the
    retry is always safe. The helper logs each outcome to its own file (the main
    log can't capture anything after os._exit) — ground truth for a canary.
    """
    quoted_app = shlex.quote(str(app_path))
    quoted_log = shlex.quote(str(log_path))
    return (
        f'L={quoted_log}; A={quoted_app}; '
        f'echo "$(date -u +%FT%TZ) waiting for pid {pid}" >> "$L"; '
        f'while kill -0 {pid} 2>/dev/null; do sleep 0.2; done; '
        f'echo "$(date -u +%FT%TZ) pid gone; opening $A" >> "$L"; '
        f'for i in 1 2 3 4 5; do '
        # Capture rc IMMEDIATELY after open — a $(date) in the same echo would
        # reset $? and we'd log rc=0 for every failure.
        f'open "$A" >> "$L" 2>&1; rc=$?; '
        f'if [ "$rc" -ne 0 ]; then echo "$(date -u +%FT%TZ) open FAILED rc=$rc (try $i)" >> "$L"; sleep 2; continue; fi; '
        f'sleep 3; '
        f'if pgrep -f "$A/Contents/MacOS/" >/dev/null 2>&1; then '
        f'echo "$(date -u +%FT%TZ) open OK + process alive (try $i)" >> "$L"; exit 0; fi; '
        f'echo "$(date -u +%FT%TZ) open returned 0 but NO running process (try $i) — retrying" >> "$L"; sleep 2; '
        f'done; '
        f'echo "$(date -u +%FT%TZ) RELAUNCH FAILED after 5 attempts (open accepted but no live process)" >> "$L"'
    )


def _verify_codesign(app_path: Path, current_app_path: Optional[Path] = None) -> bool:
    """Verify macOS code signature on the extracted .app bundle.

    Checks:
    1. Signature integrity (tampered signatures rejected)
    2. Team ID matches EXPECTED_TEAM_ID (pinned to our Apple team)
    3. Signed->unsigned downgrade rejected
    4. Team ID mismatch vs current install rejected (legacy check, kept)
    5. Version downgrade rejected
    """
    if not _codesign_verify(app_path):
        return False

    new = _get_signing_info(app_path)

    # Pin check: refuse any update whose team is not our team.
    # Catches malicious updates on a fresh install where there is no
    # current_app_path to compare against.
    if new.team_id != EXPECTED_TEAM_ID:
        logger.error(
            f"Rejecting update: team ID {new.team_id!r} does not match "
            f"expected {EXPECTED_TEAM_ID!r}"
        )
        return False

    # Downgrade protection: compare against current app if provided
    if current_app_path is not None:
        current = _get_signing_info(current_app_path)

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


# ---------------------------------------------------------------------------
# Staged updates: download in the background, apply on next launch / idle.
# ---------------------------------------------------------------------------

def _staging_dir() -> Path:
    return Config.get_data_dir() / "staged_update"


def _staging_meta_path() -> Path:
    return _staging_dir() / "staged.json"


def clear_staged_update() -> None:
    """Remove any staged artifact + metadata. Safe to call anytime."""
    shutil.rmtree(_staging_dir(), ignore_errors=True)


def stage_update(
    download_url: str,
    version: str,
    on_progress: Optional[Callable[[str], None]] = None,
) -> bool:
    """Download a release artifact into the staging area (does NOT apply it).

    Returns True if the artifact was downloaded and recorded.
    """
    try:
        from .url_safety import is_safe_fetch_url
    except ImportError:
        from url_safety import is_safe_fetch_url
    if not is_safe_fetch_url(download_url):
        # Match apply_update: HTTPS + GitHub-host allowlist, not just HTTPS.
        logger.warning("Refusing to stage update from disallowed URL host")
        return False

    # No point downloading ~60 MB every launch if the apply will be refused:
    # a Windows Downloads-folder install can't persist an update (see
    # _apply_local_artifact / _running_from_downloads).
    if sys.platform == "win32":
        app_path = _get_app_bundle_path()
        if _running_from_downloads(app_path):
            _warn_downloads_install(app_path, on_progress)
            return False

    clear_staged_update()
    staging = _staging_dir()
    try:
        staging.mkdir(parents=True, exist_ok=True)
        filename = _artifact_filename_from_url(download_url)
        dest = staging / filename
        logger.info("Staging update %s -> %s", version, dest)
        _download_to_file(download_url, dest, on_progress)
        _staging_meta_path().write_text(json.dumps({
            "version": str(version),
            "artifact": filename,
            "staged_at": datetime.now(timezone.utc).isoformat(),
        }))
        return True
    except Exception as e:
        logger.warning("Failed to stage update: %s", e)
        clear_staged_update()
        return False


def stage_update_async(
    download_url: str,
    version: str,
    on_complete: Optional[Callable[[bool], None]] = None,
) -> None:
    """Run stage_update on a background thread."""

    def _run():
        ok = stage_update(download_url, version)
        if on_complete:
            on_complete(ok)

    threading.Thread(target=_run, name="update-stager", daemon=True).start()


def get_staged_update(current_version: str) -> Optional[Path]:
    """Return the staged artifact path iff it is STRICTLY newer than current.

    Clears stale/invalid staging (older/equal version, missing file, bad
    metadata, suspicious filename) so it can never be applied. This is the core
    loop guard: a staged build equal to the running version is discarded, so an
    apply→relaunch→apply cycle is impossible.
    """
    meta_path = _staging_meta_path()
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text())
        staged_version = str(meta.get("version", ""))
        artifact = str(meta.get("artifact", ""))
    except Exception:
        clear_staged_update()
        return None

    # Only strictly-newer versions may be applied.
    try:
        if _version_tuple(staged_version) <= _version_tuple(current_version):
            clear_staged_update()
            return None
    except Exception:
        clear_staged_update()
        return None

    # Artifact must be a plain filename living directly in the staging dir.
    if not artifact or "/" in artifact or "\\" in artifact or artifact in (".", ".."):
        clear_staged_update()
        return None
    artifact_path = _staging_dir() / artifact
    if not artifact_path.is_file():
        clear_staged_update()
        return None
    return artifact_path


def apply_staged_update(
    current_version: str,
    on_progress: Optional[Callable[[str], None]] = None,
    on_pre_exit: Optional[Callable[[], None]] = None,
) -> bool:
    """Apply a previously staged update if one newer than current exists.

    Loop-safe: the artifact is moved out and staging is cleared BEFORE applying,
    so a failed or no-op apply never re-triggers on the next launch. On success
    the process relaunches (never returns). Returns False if there was nothing to
    apply or the apply failed (the caller should continue normal startup).
    """
    staged = get_staged_update(current_version)
    if staged is None:
        return False

    # Move the artifact out of staging, then wipe staging up-front.
    try:
        tmp_dir = Path(tempfile.mkdtemp(prefix="betterflow-staged-"))
        artifact = tmp_dir / staged.name
        shutil.move(str(staged), str(artifact))
    except Exception as e:
        logger.warning("Could not move staged artifact: %s", e)
        clear_staged_update()
        return False
    clear_staged_update()

    logger.info("Applying staged update from %s", artifact)
    try:
        return _apply_local_artifact(artifact, on_progress, on_pre_exit)
    finally:
        # On success the process relaunches (execvp / os._exit) and this
        # finally never runs. On failure we leak hundreds of MB (the DMG)
        # in /tmp unless we clean up here.
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _bundle_identifier(app: Path) -> Optional[str]:
    """The bundle id an .app claims, or None if it will not say.

    None means "I do not know whose this is", which is not permission to delete
    it — every caller must treat it as a refusal, not as a mismatch.
    """
    import plistlib

    try:
        with open(app / "Contents" / "Info.plist", "rb") as f:
            data = plistlib.load(f)
    except Exception:
        return None
    value = data.get("CFBundleIdentifier")
    return value if isinstance(value, str) else None


# Public name for the module-private resolver above. Renaming the original
# would touch 10 call sites across three test files this change has no other
# business in; an alias gives callers outside the module a public symbol
# without that blast radius.
get_app_bundle_path = _get_app_bundle_path


def find_stale_bundle_copies(running_app: Path) -> list[Path]:
    """Sibling copies of THIS app left behind by an earlier update.

    Returns paths only; deleting is the caller's job, so the decision can be
    tested without a filesystem that can lose something.

    #211: a device carried three copies under one bundle id, booted the oldest
    (below the server's minimum version floor) and had its Accessibility grant
    flap, because macOS keeps one row per app and which copy that row means is
    not ours to decide.

    Four guards, in order of how much they matter:

    1. **Identity.** The candidate's own Info.plist must name OUR bundle id.
       This is the one doing the real work: a name pattern is a guess about
       provenance, and an Info.plist is the bundle stating who it is. Anything
       that will not answer is left alone.
    2. **Name.** One of the shapes we can show our updater has produced,
       derived from the running bundle's stem. Closed list, not a glob.
    3. **Siblings only**, never recursive. A sweep of /Applications by
       directory walk is not a thing this should ever grow into.
    4. **No symlinks.** Deleting through one deletes its target, which by
       definition is not a copy our updater left here.
    """
    parent = running_app.parent
    stem = running_app.stem  # "BetterFlow" from "BetterFlow.app"
    patterns = [
        re.compile(rf"^{re.escape(stem)}\.app\.old$"),
        re.compile(rf"^{re.escape(stem + _MACOS_BACKUP_SUFFIX)}$"),
        re.compile(rf"^{re.escape(stem)}-[0-9][0-9.]*-backup\.app$"),
    ]

    if stem != _CANONICAL_STEM:
        # Every pattern is built from the RUNNING bundle's stem, so from a
        # non-canonical copy the sweep cannot see the canonical install's
        # siblings and returns [] — which is exactly the state #211 reported:
        # that device had booted BetterFlow-1.5.119-backup.app, where this
        # function finds nothing and both duplicates survive.
        #
        # Returning [] is still the RIGHT action. The alternative — resolving
        # the canonical stem from our own Info.plist and sweeping from here —
        # would have a backup copy delete the newer BetterFlow.app beside it,
        # which is worse than the sediment. The real repair is to get back into
        # the canonical bundle; the sweep then works on the next launch.
        #
        # What was missing is any trace of it. A device in this state produced
        # no output at all, and #211 was found by grepping one machine's log.
        logger.warning(
            "Running from %r, not the canonical %s.app — the stale-copy sweep "
            "cannot see the canonical install's siblings and is doing nothing "
            "(#211). This device is running a non-canonical copy of the agent.",
            running_app.name, _CANONICAL_STEM,
        )
        return []

    stale: list[Path] = []
    try:
        entries = sorted(parent.iterdir())
    except OSError as e:
        logger.warning("Could not list %s while looking for stale copies: %s", parent, e)
        return []

    for entry in entries:
        # `entry == running_app` is DEFENCE IN DEPTH and a mutation run will
        # flag it as unwitnessed. It is not a gap — it is unreachable while the
        # name patterns below are correct, because the running bundle's own
        # name cannot match a pattern built from its own stem. Proven as a pair
        # rather than left to argument:
        #
        #   neuter the name filter alone       -> running bundle NOT returned
        #   neuter the name filter AND this    -> running bundle IS returned
        #
        # So this line is the only thing standing between a broken name pattern
        # and deleting the app that is currently executing. Keep it.
        if entry == running_app or entry.is_symlink() or not entry.is_dir():
            continue
        if not any(p.fullmatch(entry.name) for p in patterns):
            continue
        identifier = _bundle_identifier(entry)
        if identifier != BUNDLE_ID:
            logger.info(
                "Leaving %s alone: bundle id is %r, not ours",
                entry, identifier,
            )
            continue
        stale.append(entry)
    return stale


def purge_stale_bundle_copies(running_app: Path) -> list[Path]:
    """Delete what find_stale_bundle_copies names. Returns what went.

    Best-effort by design: a copy we cannot remove (permissions, a file in use)
    is logged and skipped. Failing to tidy up must never stop the agent
    starting, which is the one job it has.
    """
    removed: list[Path] = []
    for app in find_stale_bundle_copies(running_app):
        try:
            shutil.rmtree(app)
        except Exception as e:
            logger.warning("Could not remove stale copy %s: %s", app, e)
            continue
        logger.info("Removed stale copy of this app: %s", app)
        removed.append(app)
    return removed
