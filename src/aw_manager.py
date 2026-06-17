"""Manage bundled tracker processes (ActivityWatch components, white-labeled)."""

import json
import logging
import os
import platform
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from platformdirs import user_data_dir

logger = logging.getLogger(__name__)

# Identity used for platformdirs paths on Linux (matches src/config.py).
APP_NAME = "BetterFlow"
APP_AUTHOR = "BetterQA"

# Binaries to manage (start order matters: server first, then watchers)
# These are renamed from aw-* originals for white-labeling
BF_SERVER = "bf-data-service"
BF_WATCHERS = ["bf-window-tracker", "bf-idle-tracker"]
ALL_COMPONENTS = [BF_SERVER] + BF_WATCHERS

AW_VERSION = "v0.13.2"
RELEASE_BASE = (
    f"https://github.com/ActivityWatch/activitywatch/releases/download/{AW_VERSION}"
)
RELEASE_ASSETS = {
    "darwin": f"activitywatch-{AW_VERSION}-macos-x86_64.zip",
    "windows": f"activitywatch-{AW_VERSION}-windows-x86_64.zip",
    "linux": f"activitywatch-{AW_VERSION}-linux-x86_64.zip",
}

# Mapping from original AW names to our branded names (used during download/extract)
AW_TO_BF_NAMES = {
    "aw-server-rust": "bf-data-service",
    "aw-watcher-window": "bf-window-tracker",
    "aw-watcher-afk": "bf-idle-tracker",
}

STARTUP_TIMEOUT = 10  # seconds to wait for server to be ready
SHUTDOWN_TIMEOUT = 5  # seconds before force-killing
STALE_THRESHOLD = 120  # seconds with no new events before force-restarting watcher


def _get_platform_key() -> str:
    system = platform.system()
    if system == "Darwin":
        return "darwin"
    if system == "Windows":
        return "windows"
    return "linux"


def _app_support_base() -> str:
    """Base application-support directory shared by trackers and the DB.

    Kept identical to the historical macOS/Windows locations so existing
    installs are not orphaned; Linux uses the XDG data dir (matching
    src/config.py's platformdirs usage).
    """
    system = platform.system()
    if system == "Darwin":
        return os.path.expanduser("~/Library/Application Support/BetterFlow")
    if system == "Windows":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, "BetterQA", "BetterFlow")
    return user_data_dir(APP_NAME, APP_AUTHOR)


def _get_install_dir() -> str:
    """Get persistent directory for tracker binaries (survives app updates)."""
    return os.path.join(_app_support_base(), "trackers", _get_platform_key())


def _get_db_dir() -> str:
    """Get sqlite file path for tracker database storage."""
    return os.path.join(_app_support_base(), "data", "aw-db.sqlite")


def _binaries_present(directory: str) -> bool:
    """Check if all required tracker binaries exist in directory."""
    return all(_resolve_binary_path(directory, name) is not None for name in ALL_COMPONENTS)


def _resolve_binary_path(directory: str, name: str) -> Optional[str]:
    """Resolve component binary path (supports both flat and bundled layouts)."""
    ext = ".exe" if platform.system() == "Windows" else ""

    # Legacy flat layout: trackers/darwin/bf-window-tracker
    flat = os.path.join(directory, name + ext)
    if os.path.isfile(flat):
        # On macOS, watcher binaries need adjacent runtime files in flat mode.
        if platform.system() == "Darwin" and name in BF_WATCHERS:
            if os.path.exists(os.path.join(directory, "Python")):
                return flat
            return None
        return flat

    # Bundled layout: trackers/darwin/bf-window-tracker/bf-window-tracker
    bundled = os.path.join(directory, name, name + ext)
    if os.path.isfile(bundled):
        return bundled

    return None


def _find_pids_by_path(binary_path: str) -> list[int]:
    """PIDs whose command line contains the exact bundled ``binary_path``.

    Path-scoped on purpose: matching the full install path (not the bare name)
    means we never touch an unrelated process that merely shares a name. Unix
    only — the Windows updater replaces files via a batch script rather than an
    orphaning ``os._exit``, so there is no orphan to reap there.
    """
    if platform.system() not in ("Darwin", "Linux"):
        return []
    try:
        result = subprocess.run(
            ["pgrep", "-f", binary_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as e:
        logger.debug("pgrep for %s failed: %s", binary_path, e)
        return []
    pids: list[int] = []
    for token in result.stdout.split():
        try:
            pids.append(int(token))
        except ValueError:
            continue
    return pids


def _terminate_pid(pid: int, grace: float = SHUTDOWN_TIMEOUT) -> None:
    """SIGTERM a pid, then SIGKILL if it outlives the grace period."""
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)  # liveness probe
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _download_aw_binaries(install_dir: str) -> bool:
    """Download and extract tracker binaries to install_dir. Returns True on success."""
    plat = _get_platform_key()
    asset = RELEASE_ASSETS.get(plat)
    if not asset:
        logger.error(f"No release available for platform: {plat}")
        return False

    url = f"{RELEASE_BASE}/{asset}"
    # Defense-in-depth: only ever fetch tracker binaries over HTTPS from GitHub.
    try:
        from .url_safety import is_safe_fetch_url
    except ImportError:
        from url_safety import is_safe_fetch_url
    if not is_safe_fetch_url(url):
        logger.error(f"Refusing unsafe tracker download URL (must be HTTPS from GitHub): {url}")
        return False
    logger.info(f"Downloading tracker components {AW_VERSION} from {url} ...")

    tmp_zip = None
    try:
        fd, tmp_zip = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        _MAX_AW_DOWNLOAD_BYTES = 500 * 1024 * 1024
        req = urllib.request.Request(url, headers={"User-Agent": "BetterFlow-Sync"})
        with urllib.request.urlopen(req, timeout=120) as response:
            try:
                total = int(response.headers.get("Content-Length") or 0)
            except (ValueError, TypeError):
                total = 0
            if total > _MAX_AW_DOWNLOAD_BYTES:
                raise ValueError(
                    f"Refusing to download {total} bytes (cap {_MAX_AW_DOWNLOAD_BYTES})"
                )
            downloaded = 0
            with open(tmp_zip, "wb") as f:
                for chunk in iter(lambda: response.read(256 * 1024), b""):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded > _MAX_AW_DOWNLOAD_BYTES:
                        raise ValueError(
                            f"Download exceeded {_MAX_AW_DOWNLOAD_BYTES} bytes; aborting"
                        )

        size_mb = os.path.getsize(tmp_zip) / (1024 * 1024)
        logger.info(f"Downloaded {size_mb:.1f} MB, extracting binaries...")

        ext = ".exe" if plat == "windows" else ""
        # Find full paths to component launchers in the archive.
        launchers: dict[str, str] = {}

        os.makedirs(install_dir, exist_ok=True)

        with zipfile.ZipFile(tmp_zip, "r") as zf:
            for info in zf.infolist():
                basename = os.path.basename(info.filename)
                original_name = basename.removesuffix(ext) if ext else basename
                if original_name in AW_TO_BF_NAMES and not info.is_dir():
                    launchers[original_name] = info.filename

            missing = [name for name in AW_TO_BF_NAMES.keys() if name not in launchers]
            if missing:
                logger.error(f"Missing binaries in archive: {missing}")
                return False

            # Extract full component runtime directories for watchers.
            for original_name, launcher_path in launchers.items():
                branded_name = AW_TO_BF_NAMES[original_name]
                base_dir = os.path.dirname(launcher_path)
                target_root = os.path.join(install_dir, branded_name)

                if os.path.isdir(target_root):
                    shutil.rmtree(target_root)
                os.makedirs(target_root, exist_ok=True)

                prefix = (base_dir + "/") if base_dir else ""
                extracted_any = False
                for member in zf.infolist():
                    if member.is_dir():
                        continue
                    if prefix and not member.filename.startswith(prefix):
                        continue
                    if not prefix and member.filename != launcher_path:
                        continue

                    rel_name = member.filename[len(prefix):] if prefix else os.path.basename(member.filename)
                    source_base = os.path.basename(member.filename)
                    if source_base == os.path.basename(launcher_path):
                        rel_name = branded_name + ext

                    target_path = Path(os.path.realpath(os.path.join(target_root, rel_name)))
                    try:
                        target_path.relative_to(os.path.realpath(target_root))
                    except ValueError:
                        logger.warning(f"ZIP member escapes target dir: {rel_name}")
                        continue
                    target_path = str(target_path)
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    with zf.open(member) as src, open(target_path, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    extracted_any = True

                if not extracted_any:
                    logger.error(f"Failed to extract runtime for {original_name}")
                    return False
                logger.info(f"  Extracted {original_name} runtime -> {branded_name}/")

        if not _binaries_present(install_dir):
            logger.error("Tracker extraction incomplete after install")
            return False

        # POSIX: make launchers executable. macOS additionally strips the
        # quarantine xattr (no such concept on Linux).
        if plat in ("darwin", "linux"):
            for root, _, files in os.walk(install_dir):
                for file_name in files:
                    path = os.path.join(root, file_name)
                    if os.path.basename(path).startswith("bf-"):
                        st = os.stat(path)
                        os.chmod(
                            path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
                        )
                    if plat == "darwin":
                        subprocess.run(
                            ["xattr", "-d", "com.apple.quarantine", path],
                            capture_output=True,
                        )

        logger.info("Tracker binaries installed successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to download tracker components: {e}")
        return False
    finally:
        if tmp_zip and os.path.exists(tmp_zip):
            os.unlink(tmp_zip)


class AWManager:
    """Manages lifecycle of bundled tracker processes."""

    def __init__(self, aw_port: int = 5600, afk_timeout: int = 600):
        # Clamp to reasonable ranges. These values are baked into argv for
        # the tracker binaries; bounding them prevents future config drift
        # from producing nonsense or huge argv strings.
        if not isinstance(aw_port, int) or not (1024 <= aw_port <= 65535):
            raise ValueError(f"aw_port out of range: {aw_port!r}")
        if not isinstance(afk_timeout, int) or not (30 <= afk_timeout <= 86400):
            raise ValueError(f"afk_timeout out of range: {afk_timeout!r}")
        self.aw_port = aw_port
        self.afk_timeout = afk_timeout  # seconds
        # Serializes every mutation of _processes, _using_external,
        # _disabled_components, and _stale_restart_count so scheduler
        # callbacks, tray callbacks, and shutdown never race.
        self._lifecycle_lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen] = {}
        self._using_external = False
        # Components intentionally disabled for this app session.
        self._disabled_components: set[str] = set()
        self._stale_restart_count: int = 0

    def disable_component(self, name: str) -> None:
        """Prevent a component from being started/restarted."""
        with self._lifecycle_lock:
            self._disabled_components.add(name)

    @property
    def is_managing(self) -> bool:
        """True when we own at least one tracker process to supervise.

        The watchers are ALWAYS ours, even when we attached to an external
        server — so this must be true in external mode too. It gates whether
        the watchdog (restart_if_needed) runs; the old ``not _using_external``
        clause silently disabled the watchdog on every post-first launch, so a
        blind/orphaned bf-idle-tracker was never auto-recovered (it took a
        manual app restart). Only ``_processes`` membership matters; the
        external server is never stored there.
        """
        with self._lifecycle_lock:
            return bool(self._processes)

    def start(self) -> bool:
        """Start tracker components. Returns True if tracker is available."""
        with self._lifecycle_lock:
            return self._start_locked()

    def _start_locked(self) -> bool:
        server_already_running = self._port_in_use()

        binaries_dir = self._get_binaries_dir()

        # Auto-download if binaries not found
        if not binaries_dir:
            logger.info("Tracker components not found, downloading...")
            install_dir = _get_install_dir()
            if _download_aw_binaries(install_dir):
                binaries_dir = install_dir
            else:
                logger.error("Failed to download tracker components")
                return server_already_running

        if server_already_running:
            logger.info(
                f"Tracker server already running on port {self.aw_port}, "
                "using external instance"
            )
            self._using_external = True
        else:
            logger.info(f"Starting tracker components from {binaries_dir}")

            # Start server first
            if not self._start_component(BF_SERVER, binaries_dir):
                return False

            # Wait for server to be ready
            if not self._wait_for_server():
                logger.error("Tracker server failed to start")
                self.stop()
                return False

        # Always start managed watchers to avoid stale process-name detection.
        for watcher in BF_WATCHERS:
            if watcher in self._disabled_components:
                continue
            existing = self._processes.get(watcher)
            if not existing or existing.poll() is not None:
                # Reap any orphan of this watcher left by a prior instance that
                # exited uncleanly (crash, force-quit, or a pre-fix self-update)
                # BEFORE starting a fresh one — otherwise two instances post to
                # the same bucket and the day's data is corrupted on launch.
                self._reap_orphan_processes(watcher, binaries_dir)
                self._start_component(watcher, binaries_dir)

        logger.info("Tracker components started")
        return True

    def _reap_orphan_processes(self, name: str, binaries_dir: str) -> None:
        """Kill stray processes of watcher ``name`` not owned by this instance.

        Orphans accumulate when a previous instance exited without a clean
        ``stop()`` (the self-update ``os._exit`` path before this fix, or a
        crash/force-quit). Two bf-idle-trackers posting to the same AFK bucket
        produce overlapping/duplicate events and apparent staleness a normal
        restart can't clear. Best-effort and path-scoped — never touches our
        currently-managed PID or any unrelated process.
        """
        binary_path = _resolve_binary_path(binaries_dir, name)
        if not binary_path:
            return
        keep = {os.getpid()}
        managed = self._processes.get(name)
        if managed is not None and managed.poll() is None:
            keep.add(managed.pid)
        try:
            for pid in _find_pids_by_path(binary_path):
                if pid in keep:
                    continue
                logger.warning("Reaping orphan %s (PID %d)", name, pid)
                _terminate_pid(pid)
        except Exception:
            logger.debug("orphan reap for %s failed", name, exc_info=True)

    def stop(self) -> None:
        """Stop the tracker processes WE started.

        The watchers (bf-window-tracker, bf-idle-tracker) are always launched by
        this instance, so they must always be terminated here — even when we
        attached to an external/shared server. The previous early-return on
        ``_using_external`` skipped them entirely, so an app quit or self-update
        relaunch left bf-idle-tracker running as an orphan. Two trackers then
        post to the same AFK bucket → overlapping/duplicate events and apparent
        staleness a normal restart can't clear (furdui.iancu, 2026-06-17:
        88 watcher starts vs 2 stops across the log). The shared external server
        is never in ``_processes``, so iterating it only ever touches our own
        processes — there is nothing belonging to another instance to spare.
        """
        with self._lifecycle_lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        """Terminate every tracked process. Caller must hold _lifecycle_lock."""
        if not self._processes:
            return

        logger.info("Stopping tracker components...")

        # Stop watchers first, then server
        stop_order = BF_WATCHERS + [BF_SERVER]

        for name in stop_order:
            proc = self._processes.get(name)
            if proc and proc.poll() is None:
                logger.debug(f"Terminating {name} (PID {proc.pid})")
                proc.terminate()

        # Wait for graceful shutdown
        deadline = time.monotonic() + SHUTDOWN_TIMEOUT
        for name in stop_order:
            proc = self._processes.get(name)
            if proc and proc.poll() is None:
                remaining = max(0, deadline - time.monotonic())
                try:
                    proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    logger.warning(f"Force-killing {name} (PID {proc.pid})")
                    proc.kill()

        self._processes.clear()
        logger.info("Tracker components stopped")

    def force_restart(self, reason: str = "") -> bool:
        """Tear down the whole tracker stack and rebuild it from scratch.

        Unlike ``stop()`` + ``start()`` (which only cycles the watchers and
        re-attaches to whatever server holds the port), this also reclaims a
        **hung-but-listening** server: a ``bf-data-service`` that still holds
        port 5600 but no longer answers HTTP, so ``_port_in_use()`` reads True
        and nothing ever restarts it. Path-scoped kill of any stray server +
        watcher processes, then a clean start. Used by the sync loop after the
        local server is unreachable for several consecutive cycles.
        """
        with self._lifecycle_lock:
            logger.warning("Force-restarting tracker stack (%s)", reason or "unspecified")
            self._stop_locked()
            binaries_dir = self._get_binaries_dir()
            if binaries_dir:
                # Reap a hung server too — in external mode it isn't in
                # _processes, so _stop_locked() can't reach it.
                for name in (BF_SERVER, *BF_WATCHERS):
                    self._reap_orphan_processes(name, binaries_dir)
            # Drop external attachment so _start_locked starts our own server
            # if the port is now free after the reap.
            self._using_external = False
            return self._start_locked()

    def restart_idle_tracker(self, reason: str = "") -> None:
        """Restart ONLY the idle tracker, reaping orphans first.

        Used when bf-idle-tracker reports 'afk' while the user is demonstrably
        typing — a blind/stuck tracker. The window/AFK staleness watchdog can't
        catch this: a blind tracker still emits 'afk' events, so it never looks
        stale. (If the cause is genuinely missing Input Monitoring permission a
        restart won't help — the user is separately notified to grant it — but a
        hung/stuck tracker recovers, and any orphan is cleared.)
        """
        name = "bf-idle-tracker"
        with self._lifecycle_lock:
            if name in self._disabled_components:
                return
            binaries_dir = self._get_binaries_dir()
            if not binaries_dir:
                return
            logger.warning("Restarting %s (%s)", name, reason or "blind tracker")
            proc = self._processes.get(name)
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=SHUTDOWN_TIMEOUT)
                except subprocess.TimeoutExpired:
                    proc.kill()
            self._reap_orphan_processes(name, binaries_dir)
            self._start_component(name, binaries_dir)

    def check_health(self) -> bool:
        """Check if all managed components are still running."""
        with self._lifecycle_lock:
            if self._using_external:
                return self._port_in_use()

            if not self._processes:
                return False

            for name, proc in self._processes.items():
                if name in self._disabled_components:
                    continue
                if proc.poll() is not None:
                    logger.warning(f"{name} has exited (code {proc.returncode})")
                    return False
            return True

    def restart_if_needed(self) -> bool:
        """Restart crashed or stalled components. Returns True if tracker is healthy."""
        with self._lifecycle_lock:
            return self._restart_if_needed_locked()

    def _restart_if_needed_locked(self) -> bool:
        # The SERVER may be external (a shared instance we didn't start); the
        # WATCHERS are always ours. Only the "external server vanished" case is
        # special — otherwise fall through to watcher health/stale recovery,
        # which MUST run in external mode too. Returning early there was why a
        # blind/orphaned bf-idle-tracker never self-healed after an update.
        if self._using_external and not self._port_in_use():
            logger.warning("External tracker server no longer running — starting our own")
            self._using_external = False
            return self._start_locked()

        if not self._processes:
            return False

        binaries_dir = self._get_binaries_dir()
        if not binaries_dir:
            return False

        server_restarted = False
        for name, proc in list(self._processes.items()):
            if name in self._disabled_components:
                continue
            if proc.poll() is not None:
                logger.info(
                    f"Restarting {name} (exited with code {proc.returncode})"
                )
                self._start_component(name, binaries_dir)
                if name == BF_SERVER:
                    server_restarted = True

        # Detect stalled window tracker (process alive but no new events)
        watcher = "bf-window-tracker"
        if (
            watcher not in self._disabled_components
            and watcher in self._processes
            and self._processes[watcher].poll() is None
        ):
            age = self._get_latest_window_event_age()
            if age is not None and age > STALE_THRESHOLD:
                self._stale_restart_count += 1
                logger.warning(
                    f"{watcher} stale: no new events for {age:.0f}s "
                    f"(threshold {STALE_THRESHOLD}s, "
                    f"restart #{self._stale_restart_count})"
                )
                proc = self._processes[watcher]
                proc.terminate()
                try:
                    proc.wait(timeout=SHUTDOWN_TIMEOUT)
                except subprocess.TimeoutExpired:
                    proc.kill()
                self._start_component(watcher, binaries_dir)

        # Detect a stalled idle/AFK tracker (process alive but emitting no new
        # events). The AFK watcher heartbeats its current event, so a frozen
        # end-time means it hung — and a hung AFK watcher silently freezes
        # "Active time" while the user keeps working (Alexandru, 2026-06-12: AFK
        # froze at 18:27 while window+input ran to 19:53, so the day read 7h not
        # 8h). Gate on the WINDOW tracker being fresh: only restart when the user
        # is demonstrably active but AFK has gone silent — this is the precise
        # "hung while working" signature and avoids churn during legitimate
        # full-system idle or a sleep/wake where both watchers are paused.
        idle_watcher = "bf-idle-tracker"
        if (
            idle_watcher not in self._disabled_components
            and idle_watcher in self._processes
            and self._processes[idle_watcher].poll() is None
        ):
            afk_age = self._get_latest_afk_event_age()
            window_age = self._get_latest_window_event_age()
            if (
                afk_age is not None
                and afk_age > STALE_THRESHOLD
                and window_age is not None
                and window_age <= STALE_THRESHOLD
            ):
                self._stale_restart_count += 1
                logger.warning(
                    f"{idle_watcher} stale: no AFK events for {afk_age:.0f}s "
                    f"while the window tracker is fresh ({window_age:.0f}s) "
                    f"(threshold {STALE_THRESHOLD}s, "
                    f"restart #{self._stale_restart_count})"
                )
                proc = self._processes[idle_watcher]
                proc.terminate()
                try:
                    proc.wait(timeout=SHUTDOWN_TIMEOUT)
                except subprocess.TimeoutExpired:
                    proc.kill()
                # Kill any orphan too — staleness that survives repeated restarts
                # is the signature of a second tracker fighting over the bucket;
                # restarting only our PID can never win against it.
                self._reap_orphan_processes(idle_watcher, binaries_dir)
                self._start_component(idle_watcher, binaries_dir)

        # Only block waiting for the server when the server itself was
        # restarted. The previous check (`BF_SERVER in <currently-running>`)
        # fired on every watcher restart, polling the info endpoint
        # unnecessarily.
        if server_restarted:
            self._wait_for_server()

        return self.check_health()

    def set_afk_timeout(self, seconds: int) -> None:
        """Update AFK timeout and restart idle tracker if running."""
        with self._lifecycle_lock:
            if seconds == self.afk_timeout:
                return

            self.afk_timeout = seconds
            logger.info(f"AFK timeout updated to {seconds}s")

            # Restart idle tracker if it's currently running
            proc = self._processes.get("bf-idle-tracker")
            if proc and proc.poll() is None:
                logger.info("Restarting bf-idle-tracker with new timeout")
                proc.terminate()
                try:
                    proc.wait(timeout=SHUTDOWN_TIMEOUT)
                except subprocess.TimeoutExpired:
                    proc.kill()

                binaries_dir = self._get_binaries_dir()
                if binaries_dir:
                    self._start_component("bf-idle-tracker", binaries_dir)

    def _start_component(self, name: str, binaries_dir: str) -> bool:
        """Start a single tracker component."""
        binary_path = _resolve_binary_path(binaries_dir, name)

        if not binary_path:
            logger.error(f"Binary not found for component: {name} in {binaries_dir}")
            return False

        try:
            env = os.environ.copy()
            kwargs: dict = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "env": env,
            }

            # Platform-specific: prevent dock icon on macOS
            if platform.system() == "Darwin":
                env["LSBackgroundOnly"] = "1"
            # Watchers ship as bundled runtimes (macOS/Linux) and expect to be
            # launched from their own directory so they can find their payload.
            if platform.system() in ("Darwin", "Linux") and name in BF_WATCHERS:
                kwargs["cwd"] = os.path.dirname(binary_path)

            # Platform-specific: prevent console window on Windows
            if platform.system() == "Windows":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                kwargs["startupinfo"] = startupinfo

            args = [binary_path]
            if name == "bf-window-tracker":
                args.extend(["--poll-time", "1.0"])
            if platform.system() == "Darwin" and name == "bf-window-tracker":
                # Default to JXA to avoid repeated Accessibility prompts from the
                # Swift strategy in unsigned/dev builds.
                strategy = os.environ.get("BETTERFLOW_WINDOW_STRATEGY", "jxa").strip().lower()
                if strategy not in {"jxa", "applescript", "swift"}:
                    strategy = "jxa"
                args.extend(["--strategy", strategy])

            # Pass AFK timeout to idle tracker
            if name == "bf-idle-tracker":
                args.extend(["--timeout", str(self.afk_timeout)])

            # Pass port and dbpath to server
            if name == BF_SERVER:
                if self.aw_port != 5600:
                    args.extend(["--port", str(self.aw_port)])
                # Redirect database to BetterFlow's app support directory
                db_path = _get_db_dir()
                os.makedirs(os.path.dirname(db_path), exist_ok=True)
                args.extend(["--dbpath", db_path])

            proc = subprocess.Popen(args, **kwargs)
            self._processes[name] = proc
            logger.info(f"Started {name} (PID {proc.pid})")
            return True

        except Exception as e:
            logger.error(f"Failed to start {name}: {e}")
            return False

    def _wait_for_server(self) -> bool:
        """Wait for tracker server to accept connections."""
        url = f"http://localhost:{self.aw_port}/api/0/info"
        deadline = time.monotonic() + STARTUP_TIMEOUT

        while time.monotonic() < deadline:
            # Check if process died
            proc = self._processes.get(BF_SERVER)
            if proc and proc.poll() is not None:
                logger.error(
                    f"Tracker server exited during startup "
                    f"(code {proc.returncode})"
                )
                return False

            try:
                req = urllib.request.urlopen(url, timeout=2)
                req.close()
                logger.info("Tracker server is ready")
                return True
            except (urllib.error.URLError, OSError):
                time.sleep(0.5)

        logger.error(f"Tracker server not ready after {STARTUP_TIMEOUT}s")
        return False

    def _port_in_use(self) -> bool:
        """Check if something is listening on the tracker port."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            try:
                s.connect(("localhost", self.aw_port))
                return True
            except (ConnectionRefusedError, OSError):
                return False

    def _get_latest_event_age(self, bucket_prefix: str) -> Optional[float]:
        """Seconds since the most recent event in the host's bucket whose id
        starts with ``bucket_prefix`` (e.g. ``aw-watcher-window`` /
        ``aw-watcher-afk``), or None on error/no events.

        Age is measured from the event's END (timestamp + duration). Both the
        window and AFK watchers heartbeat their current event, so a healthy
        watcher keeps the end near now; a hung one freezes it.
        """
        try:
            hostname = urllib.parse.quote(platform.node(), safe="")
            url = (
                f"http://localhost:{self.aw_port}/api/0/buckets/"
                f"{bucket_prefix}_{hostname}/events?limit=1"
            )
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                events = json.loads(resp.read())
            if not events:
                return None
            # Timestamp format: "2026-02-24T13:31:24.123456+00:00" or "...Z"
            ts_str = events[0]["timestamp"]
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            duration = events[0].get("duration", 0)
            event_end = ts.timestamp() + duration
            age = time.time() - event_end
            return max(0, age)
        except Exception as e:
            logger.debug("_get_latest_event_age(%s) failed: %s", bucket_prefix, e)
            return None

    def _get_latest_window_event_age(self) -> Optional[float]:
        """Return seconds since the most recent window event, or None on error."""
        return self._get_latest_event_age("aw-watcher-window")

    def _get_latest_afk_event_age(self) -> Optional[float]:
        """Return seconds since the most recent AFK event, or None on error.

        The branded idle tracker registers a ``bf-idle-tracker_<host>`` bucket
        while vanilla installs use ``aw-watcher-afk_<host>``. Try the branded id
        first so the staleness watchdog isn't silently disabled on branded-only
        installs, then fall back to the vanilla prefix.
        """
        age = self._get_latest_event_age("bf-idle-tracker")
        if age is not None:
            return age
        return self._get_latest_event_age("aw-watcher-afk")

    def _get_binaries_dir(self) -> Optional[str]:
        """Resolve path to tracker binaries directory.

        Priority: persistent install dir > dev path > PyInstaller bundle.
        This order ensures that binaries with existing macOS Accessibility
        permission are preferred over freshly-bundled copies that would
        require the user to re-grant permission.

        On first run from a frozen bundle, the trackers are copied to the
        persistent install dir so that macOS Accessibility permission (which
        is granted per-binary path) survives app updates.
        """
        plat = _get_platform_key()

        # Persistent install directory (auto-downloaded, permissions survive updates)
        install_dir = _get_install_dir()
        if os.path.isdir(install_dir) and _binaries_present(install_dir):
            return install_dir

        # Development: relative to project root (already has permissions)
        src_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(src_dir)
        dev_path = os.path.join(project_root, "resources", "trackers", plat)
        if os.path.isdir(dev_path) and _binaries_present(dev_path):
            return dev_path

        # PyInstaller frozen bundle — copy to persistent dir so Accessibility
        # permission survives app updates (macOS grants it per binary path).
        if getattr(sys, "frozen", False):
            base = os.path.join(sys._MEIPASS, "resources", "trackers", plat)
            if os.path.isdir(base) and _binaries_present(base):
                if self._install_to_persistent(base, install_dir):
                    return install_dir
                return base

        return None

    @staticmethod
    def _install_to_persistent(source_dir: str, install_dir: str) -> bool:
        """Copy tracker binaries from app bundle to persistent location.

        This ensures macOS Accessibility permission (granted per binary path)
        survives app updates, since the persistent path never changes.
        """
        try:
            os.makedirs(install_dir, exist_ok=True)
            for name in ALL_COMPONENTS:
                src_subdir = os.path.join(source_dir, name)
                dst_subdir = os.path.join(install_dir, name)
                if os.path.isdir(src_subdir):
                    if os.path.isdir(dst_subdir):
                        shutil.rmtree(dst_subdir)
                    shutil.copytree(src_subdir, dst_subdir)
                    # Ensure binaries are executable
                    for root, _, files in os.walk(dst_subdir):
                        for f in files:
                            p = os.path.join(root, f)
                            if os.path.basename(p).startswith("bf-"):
                                st = os.stat(p)
                                os.chmod(p, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
                            # Strip quarantine (macOS only; `xattr` does not exist
                            # on Linux and would raise FileNotFoundError here).
                            if platform.system() == "Darwin":
                                subprocess.run(
                                    ["xattr", "-d", "com.apple.quarantine", p],
                                    capture_output=True,
                                )
            logger.info(f"Installed tracker binaries to {install_dir}")
            return _binaries_present(install_dir)
        except Exception as e:
            logger.warning(f"Failed to install trackers to persistent dir: {e}")
            return False
