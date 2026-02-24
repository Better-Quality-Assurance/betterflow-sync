"""Manage bundled tracker processes (ActivityWatch components, white-labeled)."""

import json
import logging
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import zipfile
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

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
    "windows": f"activitywatch-{AW_VERSION}-windows.zip",
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
    return "darwin" if platform.system() == "Darwin" else "windows"


def _get_install_dir() -> str:
    """Get persistent directory for tracker binaries (survives app updates)."""
    if platform.system() == "Darwin":
        base = os.path.expanduser("~/Library/Application Support/BetterFlow Sync")
    else:
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        base = os.path.join(base, "BetterQA", "BetterFlow Sync")
    return os.path.join(base, "trackers", _get_platform_key())


def _get_db_dir() -> str:
    """Get sqlite file path for tracker database storage."""
    if platform.system() == "Darwin":
        base = os.path.expanduser("~/Library/Application Support/BetterFlow Sync")
    else:
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        base = os.path.join(base, "BetterQA", "BetterFlow Sync")
    return os.path.join(base, "data", "aw-db.sqlite")


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


def _download_aw_binaries(install_dir: str) -> bool:
    """Download and extract tracker binaries to install_dir. Returns True on success."""
    plat = _get_platform_key()
    asset = RELEASE_ASSETS.get(plat)
    if not asset:
        logger.error(f"No release available for platform: {plat}")
        return False

    url = f"{RELEASE_BASE}/{asset}"
    logger.info(f"Downloading tracker components {AW_VERSION} from {url} ...")

    tmp_zip = None
    try:
        tmp_zip = tempfile.mktemp(suffix=".zip")
        urllib.request.urlretrieve(url, tmp_zip)

        size_mb = os.path.getsize(tmp_zip) / (1024 * 1024)
        logger.info(f"Downloaded {size_mb:.1f} MB, extracting binaries...")

        ext = ".exe" if plat == "windows" else ""
        # Find full paths to component launchers in the archive.
        launchers: dict[str, str] = {}

        os.makedirs(install_dir, exist_ok=True)

        with zipfile.ZipFile(tmp_zip, "r") as zf:
            for info in zf.infolist():
                basename = os.path.basename(info.filename)
                original_name = basename.replace(ext, "") if ext else basename
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

                    target_path = os.path.join(target_root, rel_name)
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

        # macOS: fix permissions + strip quarantine
        if plat == "darwin":
            for root, _, files in os.walk(install_dir):
                for file_name in files:
                    path = os.path.join(root, file_name)
                    if os.path.basename(path).startswith("bf-"):
                        st = os.stat(path)
                        os.chmod(
                            path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
                        )
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
        self.aw_port = aw_port
        self.afk_timeout = afk_timeout  # seconds
        self._processes: dict[str, subprocess.Popen] = {}
        self._using_external = False
        # Components intentionally disabled for this app session.
        self._disabled_components: set[str] = set()
        self._stale_restart_count: int = 0

    @property
    def is_managing(self) -> bool:
        """True if we started tracker processes (not using external)."""
        return bool(self._processes) and not self._using_external

    def start(self) -> bool:
        """Start tracker components. Returns True if tracker is available."""
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
                self._start_component(watcher, binaries_dir)

        logger.info("Tracker components started")
        return True

    def stop(self) -> None:
        """Stop all managed tracker processes."""
        if self._using_external:
            logger.debug("Using external tracker — nothing to stop")
            return

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

    def check_health(self) -> bool:
        """Check if all managed components are still running."""
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
        if self._using_external:
            if self._port_in_use():
                return True
            # External tracker disappeared — try to start our own
            logger.warning("External tracker no longer running")
            self._using_external = False
            return self.start()

        if not self._processes:
            return False

        binaries_dir = self._get_binaries_dir()
        if not binaries_dir:
            return False

        restarted = False
        for name, proc in list(self._processes.items()):
            if name in self._disabled_components:
                continue
            if proc.poll() is not None:
                logger.info(
                    f"Restarting {name} (exited with code {proc.returncode})"
                )
                self._start_component(name, binaries_dir)
                restarted = True

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
                restarted = True

        # If server was restarted, wait for it
        if restarted and BF_SERVER in [
            n for n, p in self._processes.items() if p.poll() is None
        ]:
            self._wait_for_server()

        return self.check_health()

    def set_afk_timeout(self, seconds: int) -> None:
        """Update AFK timeout and restart idle tracker if running."""
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
                # Watchers with bundled runtime expect execution from their own dir.
                if name in BF_WATCHERS:
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

    def _is_process_running(self, name: str) -> bool:
        """Check if a process with this name is already running (outside our management)."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", name], capture_output=True, text=True
            )
            return result.returncode == 0
        except Exception:
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

    def _get_latest_window_event_age(self) -> Optional[float]:
        """Return seconds since the most recent window event, or None on error."""
        try:
            url = (
                f"http://localhost:{self.aw_port}/api/0/buckets/"
                f"aw-watcher-window_{platform.node()}/events?limit=1"
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
        except Exception:
            return None

    def _get_binaries_dir(self) -> Optional[str]:
        """Resolve path to tracker binaries directory.

        Priority: persistent install dir > dev path > PyInstaller bundle.
        This order ensures that binaries with existing macOS Accessibility
        permission are preferred over freshly-bundled copies that would
        require the user to re-grant permission.
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

        # PyInstaller frozen bundle (last resort — may need new permission grant)
        if getattr(sys, "frozen", False):
            base = os.path.join(sys._MEIPASS, "resources", "trackers", plat)
            if os.path.isdir(base) and _binaries_present(base):
                return base

        return None
