"""Manage bundled ActivityWatch processes."""

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
from typing import Optional

logger = logging.getLogger(__name__)

# Binaries to manage (start order matters: server first, then watchers)
AW_SERVER = "aw-server-rust"
AW_WATCHERS = ["aw-watcher-window", "aw-watcher-afk"]
ALL_COMPONENTS = [AW_SERVER] + AW_WATCHERS

AW_VERSION = "v0.13.2"
RELEASE_BASE = (
    f"https://github.com/ActivityWatch/activitywatch/releases/download/{AW_VERSION}"
)
RELEASE_ASSETS = {
    "darwin": f"activitywatch-{AW_VERSION}-macos-x86_64.zip",
    "windows": f"activitywatch-{AW_VERSION}-windows.zip",
}

STARTUP_TIMEOUT = 10  # seconds to wait for aw-server to be ready
SHUTDOWN_TIMEOUT = 5  # seconds before force-killing


def _get_platform_key() -> str:
    return "darwin" if platform.system() == "Darwin" else "windows"


def _get_install_dir() -> str:
    """Get persistent directory for AW binaries (survives app updates)."""
    if platform.system() == "Darwin":
        base = os.path.expanduser("~/Library/Application Support/BetterFlow Sync")
    else:
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        base = os.path.join(base, "BetterQA", "BetterFlow Sync")
    return os.path.join(base, "activitywatch", _get_platform_key())


def _binaries_present(directory: str) -> bool:
    """Check if all required AW binaries exist in directory."""
    ext = ".exe" if platform.system() == "Windows" else ""
    return all(
        os.path.exists(os.path.join(directory, name + ext))
        for name in ALL_COMPONENTS
    )


def _download_aw_binaries(install_dir: str) -> bool:
    """Download and extract AW binaries to install_dir. Returns True on success."""
    plat = _get_platform_key()
    asset = RELEASE_ASSETS.get(plat)
    if not asset:
        logger.error(f"No AW release available for platform: {plat}")
        return False

    url = f"{RELEASE_BASE}/{asset}"
    logger.info(f"Downloading ActivityWatch {AW_VERSION} from {url} ...")

    tmp_zip = None
    try:
        tmp_zip = tempfile.mktemp(suffix=".zip")
        urllib.request.urlretrieve(url, tmp_zip)

        size_mb = os.path.getsize(tmp_zip) / (1024 * 1024)
        logger.info(f"Downloaded {size_mb:.1f} MB, extracting binaries...")

        ext = ".exe" if plat == "windows" else ""
        needed = {name + ext for name in ALL_COMPONENTS}

        os.makedirs(install_dir, exist_ok=True)

        with zipfile.ZipFile(tmp_zip, "r") as zf:
            for info in zf.infolist():
                basename = os.path.basename(info.filename)
                if basename in needed:
                    target = os.path.join(install_dir, basename)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    logger.info(f"  Extracted {basename}")
                    needed.discard(basename)

        if needed:
            logger.error(f"Missing binaries in archive: {needed}")
            return False

        # macOS: fix permissions + strip quarantine
        if plat == "darwin":
            for name in ALL_COMPONENTS:
                path = os.path.join(install_dir, name)
                if os.path.exists(path):
                    st = os.stat(path)
                    os.chmod(
                        path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
                    )
                    subprocess.run(
                        ["xattr", "-d", "com.apple.quarantine", path],
                        capture_output=True,
                    )

        logger.info("ActivityWatch binaries installed successfully")
        return True

    except Exception as e:
        logger.error(f"Failed to download ActivityWatch: {e}")
        return False
    finally:
        if tmp_zip and os.path.exists(tmp_zip):
            os.unlink(tmp_zip)


class AWManager:
    """Manages lifecycle of bundled ActivityWatch processes."""

    def __init__(self, aw_port: int = 5600):
        self.aw_port = aw_port
        self._processes: dict[str, subprocess.Popen] = {}
        self._using_external = False

    @property
    def is_managing(self) -> bool:
        """True if we started AW processes (not using external)."""
        return bool(self._processes) and not self._using_external

    def start(self) -> bool:
        """Start AW components. Returns True if AW is available."""
        # Check if AW is already running externally
        if self._port_in_use():
            logger.info(
                f"ActivityWatch already running on port {self.aw_port}, "
                "using external instance"
            )
            self._using_external = True
            return True

        binaries_dir = self._get_binaries_dir()

        # Auto-download if binaries not found
        if not binaries_dir:
            logger.info("ActivityWatch binaries not found, downloading...")
            install_dir = _get_install_dir()
            if _download_aw_binaries(install_dir):
                binaries_dir = install_dir
            else:
                logger.error("Failed to download ActivityWatch binaries")
                return False

        logger.info(f"Starting bundled ActivityWatch from {binaries_dir}")

        # Start server first
        if not self._start_component(AW_SERVER, binaries_dir):
            return False

        # Wait for server to be ready
        if not self._wait_for_server():
            logger.error("aw-server-rust failed to start")
            self.stop()
            return False

        # Start watchers
        for watcher in AW_WATCHERS:
            self._start_component(watcher, binaries_dir)

        logger.info("ActivityWatch components started")
        return True

    def stop(self) -> None:
        """Stop all managed AW processes."""
        if self._using_external:
            logger.debug("Using external AW — nothing to stop")
            return

        if not self._processes:
            return

        logger.info("Stopping ActivityWatch components...")

        # Stop watchers first, then server
        stop_order = AW_WATCHERS + [AW_SERVER]

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
        logger.info("ActivityWatch components stopped")

    def check_health(self) -> bool:
        """Check if all managed components are still running."""
        if self._using_external:
            return self._port_in_use()

        if not self._processes:
            return False

        for name, proc in self._processes.items():
            if proc.poll() is not None:
                logger.warning(f"{name} has exited (code {proc.returncode})")
                return False
        return True

    def restart_if_needed(self) -> bool:
        """Restart crashed components. Returns True if AW is healthy."""
        if self._using_external:
            if self._port_in_use():
                return True
            # External AW disappeared — try to start our own
            logger.warning("External ActivityWatch no longer running")
            self._using_external = False
            return self.start()

        if not self._processes:
            return False

        binaries_dir = self._get_binaries_dir()
        if not binaries_dir:
            return False

        restarted = False
        for name, proc in list(self._processes.items()):
            if proc.poll() is not None:
                logger.info(
                    f"Restarting {name} (exited with code {proc.returncode})"
                )
                self._start_component(name, binaries_dir)
                restarted = True

        # If server was restarted, wait for it
        if restarted and AW_SERVER in [
            n for n, p in self._processes.items() if p.poll() is None
        ]:
            self._wait_for_server()

        return self.check_health()

    def _start_component(self, name: str, binaries_dir: str) -> bool:
        """Start a single AW component."""
        ext = ".exe" if platform.system() == "Windows" else ""
        binary_path = os.path.join(binaries_dir, name + ext)

        if not os.path.exists(binary_path):
            logger.error(f"Binary not found: {binary_path}")
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

            # Platform-specific: prevent console window on Windows
            if platform.system() == "Windows":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                kwargs["startupinfo"] = startupinfo

            args = [binary_path]

            # Pass port to server if non-default
            if name == AW_SERVER and self.aw_port != 5600:
                args.extend(["--port", str(self.aw_port)])

            proc = subprocess.Popen(args, **kwargs)
            self._processes[name] = proc
            logger.info(f"Started {name} (PID {proc.pid})")
            return True

        except Exception as e:
            logger.error(f"Failed to start {name}: {e}")
            return False

    def _wait_for_server(self) -> bool:
        """Wait for aw-server-rust to accept connections."""
        url = f"http://localhost:{self.aw_port}/api/0/info"
        deadline = time.monotonic() + STARTUP_TIMEOUT

        while time.monotonic() < deadline:
            # Check if process died
            proc = self._processes.get(AW_SERVER)
            if proc and proc.poll() is not None:
                logger.error(
                    f"aw-server-rust exited during startup "
                    f"(code {proc.returncode})"
                )
                return False

            try:
                req = urllib.request.urlopen(url, timeout=2)
                req.close()
                logger.info("aw-server-rust is ready")
                return True
            except (urllib.error.URLError, OSError):
                time.sleep(0.5)

        logger.error(f"aw-server-rust not ready after {STARTUP_TIMEOUT}s")
        return False

    def _port_in_use(self) -> bool:
        """Check if something is listening on the AW port."""
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            try:
                s.connect(("localhost", self.aw_port))
                return True
            except (ConnectionRefusedError, OSError):
                return False

    def _get_binaries_dir(self) -> Optional[str]:
        """Resolve path to AW binaries directory."""
        plat = _get_platform_key()

        # PyInstaller frozen bundle
        if getattr(sys, "frozen", False):
            base = os.path.join(sys._MEIPASS, "resources", "activitywatch", plat)
            if os.path.isdir(base) and _binaries_present(base):
                return base

        # Persistent install directory (auto-downloaded)
        install_dir = _get_install_dir()
        if os.path.isdir(install_dir) and _binaries_present(install_dir):
            return install_dir

        # Development: relative to project root
        src_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(src_dir)
        dev_path = os.path.join(project_root, "resources", "activitywatch", plat)
        if os.path.isdir(dev_path) and _binaries_present(dev_path):
            return dev_path

        return None
