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
IDLE_TRACKER = "bf-idle-tracker"
BF_WATCHERS = ["bf-window-tracker", IDLE_TRACKER]
ALL_COMPONENTS = [BF_SERVER] + BF_WATCHERS

# Our Apple Developer Team. The build signs the bundled trackers with this
# (Developer ID, hardened runtime, stable identifier). A persistent tracker that
# is NOT signed with this team is a stale ad-hoc copy from a pre-signing build:
# its TCC (Input Monitoring) grant is fragile and gets silently denied, so the
# tracker runs but stays blind. See _should_reinstall_trackers.
BETTERQA_TEAM_ID = "87NVC57J44"

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
# A window tracker that has emitted ZERO events this long after launch (its bucket
# is still empty) is blind, not just quiet — aw-watcher-window heartbeats even a
# static window, so a healthy one emits within seconds. Past this grace, with AW
# reachable, restart it (Sachi, win32, 2026-06-24: alive but never emitted, so the
# age-based stale check — which needs an event to measure — never fired). The grace
# also doubles as restart backoff: _start_component resets the launch clock, so a
# persistently-blind tracker is re-probed at most once per grace, not every tick.
WINDOW_BLIND_GRACE = 180  # seconds
# After this many consecutive stale-restarts that DON'T take (the tracker emits
# nothing again right after we restart it), bf-idle-tracker is blind — almost
# always a missing Input Monitoring grant, which a restart can't fix (#46). Stop
# churning a process every cycle and flag it so the app re-prompts for permission.
IDLE_BLIND_RESTART_THRESHOLD = 5
# Once blind, probe-restart at most this often (so a later permission grant still
# auto-recovers) instead of restarting on every health-check tick.
IDLE_BLIND_RETRY_INTERVAL = 1800  # 30 minutes
# Same idea for bf-window-tracker. The launch-grace re-probe above only throttles
# the BLIND (zero-events) case; a FROZEN tracker (an old event whose age keeps
# growing) returns age > threshold every tick and so was kill+relaunched every
# health-check with no backoff (Sachi, win32, 2026-06-30: restart #1→#5 every 30s
# while the event age climbed 148→268s, never recovering). After this many
# consecutive stale-restarts that don't take, treat it as blind, stop churning,
# and probe at most once per retry interval.
WINDOW_BLIND_RESTART_THRESHOLD = 5
WINDOW_BLIND_RETRY_INTERVAL = 1800  # 30 minutes
# A FLAPPING (not permanently-wedged) window-capture source emits the odd event
# between blind spells; clearing the blind flag on the first one reset the churn
# counter and let it re-enter a full restart burst ~30s later, defeating the
# retry-interval backoff (Sachi, win32, 2026-07-01: blind at 09:50, a lone event
# ~09:55 cleared it, then a second 4-restart burst at 09:57). Require this many
# CONSECUTIVE healthy health-checks (~30s each) before trusting recovery and
# unlatching, so a flapping source stays backed off instead of churning per flap.
WINDOW_BLIND_CLEAR_HEALTHY_CYCLES = 3


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
            # urlopen follows 3xx transparently, so the allowlist check above
            # only covers the FIRST hop. GitHub legitimately redirects assets to
            # objects.githubusercontent.com; anywhere else means the download was
            # steered off-allowlist, so abort before reading/writing the body.
            final_url = response.geturl()
            if not is_safe_fetch_url(final_url):
                raise ValueError(
                    f"Tracker download redirected to a disallowed host: {final_url}"
                )
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

    def __init__(self, aw_port: int = 5600, afk_timeout: int = 600,
                 stop_external_when_inproc: bool = False):
        # Clamp to reasonable ranges. These values are baked into argv for
        # the tracker binaries; bounding them prevents future config drift
        # from producing nonsense or huge argv strings.
        if not isinstance(aw_port, int) or not (1024 <= aw_port <= 65535):
            raise ValueError(f"aw_port out of range: {aw_port!r}")
        if not isinstance(afk_timeout, int) or not (30 <= afk_timeout <= 86400):
            raise ValueError(f"afk_timeout out of range: {afk_timeout!r}")
        self.aw_port = aw_port
        self.afk_timeout = afk_timeout  # seconds
        # Stage 2 of tracker-convergence (default OFF, opt-in via
        # config.sync.stop_external_afk_tracker): when True AND in-process AFK is
        # the source, STOP the external bf-idle-tracker process entirely rather
        # than just ignoring its bucket — killing the dual-source surface that
        # produced Bug A. Default OFF because, with the tracker stopped, recovery
        # can't fall back to it without flipping the (currently local-only)
        # kill-switch — so it ships dark until validated/remotely-flippable.
        self._stop_external_when_inproc = bool(stop_external_when_inproc)
        # Serializes every mutation of _processes, _using_external,
        # _disabled_components, and _stale_restart_count so scheduler
        # callbacks, tray callbacks, and shutdown never race.
        self._lifecycle_lock = threading.RLock()
        self._processes: dict[str, subprocess.Popen] = {}
        # monotonic launch time per component, for the window-tracker blind-grace
        # (a tracker emitting nothing only counts as blind once it's run past the
        # grace; resetting this on (re)start also backs off blind re-probes).
        self._component_started_at: dict[str, float] = {}
        self._using_external = False
        # Capture suppressed because we are outside the user's working hours (or
        # their schedule is not known yet). While set, the trackers stay DOWN and
        # every path that would bring them back up — start(), restart_if_needed(),
        # force_restart() — is a no-op. Without this the health/stale-recovery
        # checks would simply resurrect the watchers we just stopped and carry on
        # recording the person's machine at midnight. Guarded by _lifecycle_lock
        # alongside the other lifecycle state it gates.
        self._capture_suppressed = False
        # Components intentionally disabled for this app session.
        self._disabled_components: set[str] = set()
        # Any-tracker force-restart count (window OR idle) — drives the
        # cross-tracker restart-loop escalation check. NOT a per-tracker figure;
        # use _idle_stale_restart_count for the idle-specific telemetry.
        self._stale_restart_count: int = 0
        # bf-idle-tracker-only force-restart count this session. Reported as
        # idle_tracker_stale_restarts so the "Active time not advancing (N
        # restarts)" fleet alert reflects idle-tracker churn alone, not a flapping
        # window tracker bleeding into the figure.
        self._idle_stale_restart_count: int = 0
        # Consecutive bf-idle-tracker stale-restarts that didn't take; once it
        # crosses IDLE_BLIND_RESTART_THRESHOLD the tracker is treated as blind
        # (missing Input Monitoring) — restarts back off and the app re-prompts.
        # Reset to 0 when the tracker starts emitting fresh AFK events again.
        self._idle_consecutive_stale: int = 0
        self._idle_tracker_blind: bool = False
        self._idle_last_restart_mono: float = 0.0
        # Same trio for bf-window-tracker: once it stays stale across
        # WINDOW_BLIND_RESTART_THRESHOLD restarts it is blind (wedged/blocked
        # window-capture source a restart can't fix) — back off churning and flag
        # it. Reset to 0 when the tracker emits fresh window events again.
        self._window_consecutive_stale: int = 0
        self._window_tracker_blind: bool = False
        self._window_last_restart_mono: float = 0.0
        # Consecutive healthy (fresh-event) health-checks observed WHILE blind.
        # The blind flag only clears once this reaches
        # WINDOW_BLIND_CLEAR_HEALTHY_CYCLES so a flapping source's stray events
        # don't unlatch it prematurely (see WINDOW_BLIND_CLEAR_HEALTHY_CYCLES).
        self._window_healthy_streak: int = 0
        # When the agent uploads its own in-process AFK stream, the external
        # bf-idle-tracker bucket is ignored — don't restart it or raise blind
        # alerts about a tracker we no longer consume.
        self._inproc_afk_active: bool = False

    @property
    def idle_tracker_blind(self) -> bool:
        """True when bf-idle-tracker has stayed stale across repeated restarts —
        the signature of a missing Input Monitoring grant (a restart can't fix
        it). The app reads this to re-prompt for permission and to stop churning
        restarts. Clears automatically once the tracker emits fresh events."""
        with self._lifecycle_lock:
            return self._idle_tracker_blind

    @property
    def window_tracker_blind(self) -> bool:
        """True when bf-window-tracker has stayed stale across repeated restarts —
        a wedged/blocked window-capture source a restart can't fix. The watchdog
        reads this to stop churning restarts and surface it; per-app attribution
        pauses but billing continues via the activity stream. Clears automatically
        once the tracker emits fresh window events again."""
        with self._lifecycle_lock:
            return self._window_tracker_blind

    def set_inproc_afk_active(self, active: bool) -> None:
        """Mark whether the agent is uploading its own in-process AFK stream. When
        active, the bf-idle-tracker stale/blind detection is suppressed (we no
        longer consume its bucket, so it must not restart or raise alerts).

        Stage 2 (only when ``stop_external_when_inproc`` is enabled): on the
        transition into/out of active, also STOP / re-launch the external
        bf-idle-tracker process. Driven via ``_disabled_components`` so the single
        membership flip gates every (re)start path (`_start_locked`, the watchdog,
        `restart_idle_tracker`). Idempotent — only acts on an actual TRANSITION, as
        the flag is now published every sync cycle.

        The transition does subprocess I/O (terminate/wait, orphan reap) under the
        lock. That's bounded: `_inproc_afk_active()` is config + sticky
        `available()`, neither of which changes at runtime, so a session flips at
        most once (typically once at startup) — this is a one-time lock hold, the
        same pattern `restart_idle_tracker`/`_stop_locked` already use, not a
        per-cycle cost."""
        active = bool(active)
        with self._lifecycle_lock:
            was_active = self._inproc_afk_active
            self._inproc_afk_active = active
            if not self._stop_external_when_inproc or active == was_active:
                return
            if active:
                # In-process is the sole source: disable + stop the external
                # tracker so two AFK sources can't diverge and we don't run a dead
                # process whose bucket we ignore anyway.
                self._disabled_components.add(IDLE_TRACKER)
                self._stop_idle_tracker_locked()
            else:
                # In-process unavailable (config off / no OS idle clock / Linux):
                # fall back to the external tracker — re-enable and (re)start it.
                self._disabled_components.discard(IDLE_TRACKER)
                self._start_idle_tracker_locked()

    def _stop_idle_tracker_locked(self) -> None:
        """Terminate the bf-idle-tracker process if we're running it, and reap any
        orphan so it can't keep posting to the AFK bucket. No-op if not running.
        Caller holds _lifecycle_lock."""
        proc = self._processes.pop(IDLE_TRACKER, None)
        if proc is not None and proc.poll() is None:
            logger.info("Stopping %s (in-process AFK is the sole source)", IDLE_TRACKER)
            proc.terminate()
            try:
                proc.wait(timeout=SHUTDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
        binaries_dir = self._get_binaries_dir()
        if binaries_dir:
            self._reap_orphan_processes(IDLE_TRACKER, binaries_dir)

    def _start_idle_tracker_locked(self) -> None:
        """(Re)start bf-idle-tracker as the fallback when in-process AFK is no
        longer the source. No-op if already running or no binaries. Caller holds
        _lifecycle_lock."""
        if IDLE_TRACKER in self._disabled_components:
            return  # left disabled by someone else — respect it
        if not self._processes:
            # The stack hasn't been start()ed yet (this fired from a pre-start
            # reconcile/cycle). Don't launch a lone tracker against a server that
            # isn't up — start() will bring it up with the rest now that it's
            # re-enabled. Without this guard an early False transition could leak
            # a serverless tracker process.
            return
        existing = self._processes.get(IDLE_TRACKER)
        if existing is not None and existing.poll() is None:
            return  # already running
        binaries_dir = self._get_binaries_dir()
        if not binaries_dir:
            return
        logger.info("(Re)starting %s (in-process AFK no longer the source)", IDLE_TRACKER)
        self._reap_orphan_processes(IDLE_TRACKER, binaries_dir)
        self._start_component(IDLE_TRACKER, binaries_dir)

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

    @property
    def capture_suppressed(self) -> bool:
        with self._lifecycle_lock:
            return self._capture_suppressed

    def set_capture_suppressed(self, suppressed: bool, reason: str = "") -> None:
        """Suppress or resume ALL local capture, converging on the desired state.

        Suppressing stops the tracker processes outright — this is the difference
        between "we don't upload your evening" and "we don't watch your evening".
        Filtering at upload time still left window titles and input activity being
        written to a local store on the employee's machine around the clock; only
        stopping the watchers actually stops the recording.

        This ENSURES the end state rather than reacting to a flag transition. It
        is the sole owner of tracker startup now (AppController no longer calls
        start() directly), so an early-return on "flag already False" would leave a
        fresh process — where the flag starts False and nothing is running — with
        the trackers never started at all.

        Idempotent in both directions, so the 60s policy tick can call it every
        cycle: _stop_locked() no-ops with no processes, and the start is guarded on
        nothing of ours already running. Reviving processes that died mid-session
        is restart_if_needed()'s job, not this one's — calling _start_locked() on a
        live stack would see our own server on the port and misfile it as external.
        """
        suppressed = bool(suppressed)
        with self._lifecycle_lock:
            changed = self._capture_suppressed != suppressed
            self._capture_suppressed = suppressed

            if suppressed:
                if changed:
                    logger.info(
                        "Capture suppressed (%s) — stopping trackers",
                        reason or "outside working hours",
                    )
                self._stop_locked()
            elif not self._processes:
                logger.info(
                    "Capture allowed (%s) — starting trackers",
                    reason or "inside working hours",
                )
                self._start_locked()

    def start(self) -> bool:
        """Start tracker components. Returns True if tracker is available."""
        with self._lifecycle_lock:
            return self._start_locked()

    def _start_locked(self) -> bool:
        # Every route back to a running tracker funnels through here, so this one
        # guard is enough to keep start()/restart_if_needed()/force_restart() from
        # resurrecting capture while it is suppressed.
        if self._capture_suppressed:
            logger.debug("Tracker start refused: capture is suppressed")
            return False

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
            if self._capture_suppressed:
                logger.debug("Force-restart refused (%s): capture is suppressed", reason or "unspecified")
                return False
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
            if self._capture_suppressed:
                return
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

    def stale_restart_count(self) -> int:
        """Idle/window tracker force-restart count this session (lock-safe).

        Cheap counterpart to health_snapshot() — no tracker-server I/O — for the
        restart-loop escalation check that runs every sync cycle."""
        with self._lifecycle_lock:
            return self._stale_restart_count

    def health_snapshot(self) -> dict:
        """Public read-only view of tracker health for telemetry/heartbeat.

        Returns the idle-tracker force-restart count plus the age (seconds) of
        the most recent AFK and window events. A high AFK age while the window
        age stays low is the "active but idle tracker frozen" signature the
        backend uses to mark a device tracking_degraded.

        ``idle_tracker_blind`` lets the backend distinguish a transient restart
        from a chronically blind tracker (a denied Input Monitoring grant a
        restart can't fix) so the alert can tell the user to grant permission
        rather than wait it out.

        The counters/flag are read under the lifecycle lock; the event ages are
        fetched from the local tracker server (no shared mutable state) so they
        sit outside the lock to avoid holding it across network I/O.
        """
        with self._lifecycle_lock:
            idle_restarts = self._idle_stale_restart_count
            blind = self._idle_tracker_blind
            window_blind = self._window_tracker_blind
            inproc = self._inproc_afk_active

        window_age = self._get_latest_window_event_age()
        # When the agent owns the AFK stream in-process, the external
        # bf-idle-tracker bucket is ignored — do NOT report its (likely stale)
        # event age. Reporting it makes the backend fire false "Active time not
        # advancing" alerts for an agent that is billing correctly (the external
        # tracker is still running but irrelevant). The restart count + blind flag
        # are already 0/False on this path (the restart loop is suppressed).
        afk_age = None if inproc else self._get_latest_afk_event_age()
        return {
            # idle-tracker-only — window-tracker restarts are excluded so this
            # figure isn't inflated by an unrelated flapping window watcher.
            "idle_tracker_stale_restarts": idle_restarts,
            "idle_tracker_blind": blind,
            # Chronically-blind window tracker: per-app attribution is degraded
            # even though active/billed time keeps flowing — lets the backend tell
            # "no app breakdown" apart from "not tracking".
            "window_tracker_blind": window_blind,
            # True => agent generates its own AFK stream; backend should ignore
            # external-tracker staleness for this device.
            "inproc_afk": inproc,
            # Ints are friendlier to JSON / the backend's unsigned columns; the
            # sub-second precision is irrelevant for a staleness signal.
            "afk_event_age_seconds": int(afk_age) if afk_age is not None else None,
            "window_event_age_seconds": int(window_age) if window_age is not None else None,
        }

    def restart_if_needed(self) -> bool:
        """Restart crashed or stalled components. Returns True if tracker is healthy."""
        with self._lifecycle_lock:
            return self._restart_if_needed_locked()

    def _restart_if_needed_locked(self) -> bool:
        # Trackers are meant to be down outside working hours; "down" is the
        # healthy state, not a crash to recover from. Without this the staleness
        # watchdog would read the silence as a stalled tracker and restart it,
        # quietly undoing capture suppression a minute after it took effect.
        if self._capture_suppressed:
            return True

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

        # Detect stalled window tracker: either it froze (events exist but their
        # end is old) OR it's blind — alive but emitting nothing at all, so the
        # age helper has no event to measure and returns None. The blind case used
        # to slip through (`age is not None` only), leaving window data frozen
        # while AFK/input flowed (Sachi, win32).
        watcher = "bf-window-tracker"
        if (
            watcher not in self._disabled_components
            and watcher in self._processes
            and self._processes[watcher].poll() is None
        ):
            age = self._get_latest_window_event_age()
            running_for = self._component_running_seconds(watcher)
            reachable = self._port_in_use()
            if not self._is_window_tracker_stale(age, running_for, reachable):
                # Emitting fresh window events again. (age None here is just
                # launch lag or an AW outage, not recovery — don't count it.)
                if age is not None and age <= STALE_THRESHOLD:
                    if self._window_tracker_blind:
                        # A flapping source emits a stray event between blind
                        # spells; unlatching on the FIRST one lets it re-enter a
                        # full restart burst, defeating the retry-interval backoff
                        # (see WINDOW_BLIND_CLEAR_HEALTHY_CYCLES). Require sustained
                        # emission before we trust recovery and clear the flag.
                        self._window_healthy_streak += 1
                        if self._window_healthy_streak >= WINDOW_BLIND_CLEAR_HEALTHY_CYCLES:
                            self._window_tracker_blind = False
                            self._window_consecutive_stale = 0
                            self._window_healthy_streak = 0
                            logger.info(
                                "%s emitting steadily again across %d checks — "
                                "clearing blind flag; per-app attribution restored",
                                watcher,
                                WINDOW_BLIND_CLEAR_HEALTHY_CYCLES,
                            )
                    else:
                        # Not blind: reset promptly so a future genuine stall
                        # restarts on the next tick, not after a fresh burst.
                        self._window_consecutive_stale = 0
                else:
                    # age None (launch lag / AW outage): not proof of recovery —
                    # a flap through such a gap must not count toward the streak.
                    self._window_healthy_streak = 0
            else:
                # Staleness breaks any in-progress recovery streak — the healthy
                # cycles must be CONSECUTIVE for the blind flag to clear.
                self._window_healthy_streak = 0
                # Stale. If it has stayed stale across several restarts it is
                # blind — a wedged/blocked window-capture source a restart can't
                # fix (Sachi, win32, 2026-06-30: restart #1→#5 every 30s while the
                # event age climbed, never recovering), not a crash. Back off to
                # one probe-restart per retry interval instead of kill+relaunching
                # every tick. Tracking continues via the activity stream meanwhile.
                now_mono = time.monotonic()
                # Read the authoritative flag (set once consecutive crossed the
                # threshold and cleared on recovery together with the counter) so
                # the backoff gate and the heartbeat's window_tracker_blind never
                # diverge as this method grows.
                blind = self._window_tracker_blind
                if blind and (now_mono - self._window_last_restart_mono) < WINDOW_BLIND_RETRY_INTERVAL:
                    logger.debug(
                        "%s still stale but blind (%d restarts didn't take) — "
                        "backing off; next probe in %.0fs",
                        watcher,
                        self._window_consecutive_stale,
                        WINDOW_BLIND_RETRY_INTERVAL - (now_mono - self._window_last_restart_mono),
                    )
                else:
                    self._stale_restart_count += 1
                    self._window_consecutive_stale += 1
                    self._window_last_restart_mono = now_mono
                    detail = (
                        f"no new events for {age:.0f}s" if age is not None
                        else f"no events {running_for:.0f}s after launch (blind)"
                    )
                    logger.warning(
                        f"{watcher} stale: {detail} (threshold {STALE_THRESHOLD}s, "
                        f"restart #{self._stale_restart_count}, "
                        f"consecutive {self._window_consecutive_stale})"
                    )
                    proc = self._processes[watcher]
                    proc.terminate()
                    try:
                        proc.wait(timeout=SHUTDOWN_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    # An orphan window watcher fighting over the bucket keeps us
                    # stale no matter how often we restart our own PID — reap it
                    # too (the "stale survives restarts" signature, same as idle).
                    self._reap_orphan_processes(watcher, binaries_dir)
                    self._start_component(watcher, binaries_dir)
                    if (
                        self._window_consecutive_stale >= WINDOW_BLIND_RESTART_THRESHOLD
                        and not self._window_tracker_blind
                    ):
                        self._window_tracker_blind = True
                        logger.warning(
                            "%s has stayed stale across %d restarts — a restart "
                            "can't fix it (the window-capture source is wedged or "
                            "blocked). Backing off and flagging blind; per-app "
                            "attribution is paused but billing continues via the "
                            "activity stream.",
                            watcher,
                            self._window_consecutive_stale,
                        )

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
            not self._inproc_afk_active
            and idle_watcher not in self._disabled_components
            and idle_watcher in self._processes
            and self._processes[idle_watcher].poll() is None
        ):
            afk_age = self._get_latest_afk_event_age()
            window_age = self._get_latest_window_event_age()
            is_stale = (
                afk_age is not None
                and afk_age > STALE_THRESHOLD
                and window_age is not None
                and window_age <= STALE_THRESHOLD
            )
            if not is_stale:
                # Recovery: the tracker is emitting fresh AFK events again, so
                # clear any blind state — a future genuine stall restarts
                # promptly and the permission re-prompt resolves.
                if afk_age is not None and afk_age <= STALE_THRESHOLD:
                    self._idle_consecutive_stale = 0
                    self._idle_tracker_blind = False
            else:
                # If the tracker has already stayed stale across several restarts
                # it is blind (missing Input Monitoring / TCC), not crashed (#46)
                # — restarting can't fix it. Back off to one probe-restart per
                # retry interval instead of churning a process every tick, and
                # flag it so the app re-prompts for permission.
                now_mono = time.monotonic()
                blind = self._idle_consecutive_stale >= IDLE_BLIND_RESTART_THRESHOLD
                if blind and (now_mono - self._idle_last_restart_mono) < IDLE_BLIND_RETRY_INTERVAL:
                    logger.debug(
                        "%s still stale but blind (%d restarts didn't take) — backing "
                        "off; next probe in %.0fs",
                        idle_watcher,
                        self._idle_consecutive_stale,
                        IDLE_BLIND_RETRY_INTERVAL - (now_mono - self._idle_last_restart_mono),
                    )
                else:
                    self._stale_restart_count += 1
                    self._idle_stale_restart_count += 1
                    self._idle_consecutive_stale += 1
                    self._idle_last_restart_mono = now_mono
                    logger.warning(
                        f"{idle_watcher} stale: no AFK events for {afk_age:.0f}s "
                        f"while the window tracker is fresh ({window_age:.0f}s) "
                        f"(threshold {STALE_THRESHOLD}s, restart #{self._stale_restart_count}, "
                        f"consecutive {self._idle_consecutive_stale})"
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
                    if (
                        self._idle_consecutive_stale >= IDLE_BLIND_RESTART_THRESHOLD
                        and not self._idle_tracker_blind
                    ):
                        self._idle_tracker_blind = True
                        logger.warning(
                            "%s has stayed stale across %d restarts — a restart "
                            "can't fix it. Likely a stale or denied Input Monitoring "
                            "grant on the tracker (a separate TCC subject from the "
                            "main app). Backing off restarts and flagging for a "
                            "permission re-prompt; tracking continues via the OS "
                            "idle clock meanwhile.",
                            idle_watcher,
                            self._idle_consecutive_stale,
                        )

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
            self._component_started_at[name] = time.monotonic()
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
        hostname = urllib.parse.quote(platform.node(), safe="")
        return self._get_latest_event_age_for_bucket(f"{bucket_prefix}_{hostname}")

    def _get_latest_event_age_for_bucket(self, bucket_id: str) -> Optional[float]:
        """Seconds since the latest event in an exact bucket id."""
        try:
            url = (
                f"http://localhost:{self.aw_port}/api/0/buckets/"
                f"{urllib.parse.quote(bucket_id, safe='')}/events?limit=1"
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
            logger.debug("_get_latest_event_age_for_bucket(%s) failed: %s", bucket_id, e)
            return None

    def _get_buckets(self) -> dict:
        """Return AW bucket metadata, or an empty dict on error."""
        try:
            url = f"http://localhost:{self.aw_port}/api/0/buckets/"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                buckets = json.loads(resp.read())
            return buckets if isinstance(buckets, dict) else {}
        except Exception as e:
            logger.debug("_get_buckets failed: %s", e)
            return {}

    def _get_latest_event_age_from_buckets(self, bucket_ids: list[str]) -> Optional[float]:
        """Return the freshest/latest event age among exact bucket ids."""
        ages = [
            age
            for bucket_id in bucket_ids
            if (age := self._get_latest_event_age_for_bucket(bucket_id)) is not None
        ]
        return min(ages) if ages else None

    def _get_latest_window_event_age(self) -> Optional[float]:
        """Return seconds since the most recent window event, or None on error."""
        return self._get_latest_event_age("aw-watcher-window")

    def _component_running_seconds(self, name: str) -> Optional[float]:
        """Seconds since we last (re)started ``name``, or None if we never did."""
        started = self._component_started_at.get(name)
        if started is None:
            return None
        return time.monotonic() - started

    @staticmethod
    def _is_window_tracker_stale(
        age: Optional[float], running_for: Optional[float], reachable: bool
    ) -> bool:
        """Whether the window tracker should be restarted.

        - ``age`` is not None: events exist; stale if their end is older than the
          threshold (the original frozen-tracker case).
        - ``age`` is None: no events at all. That's a blind tracker ONLY when it's
          run well past the launch grace AND AW is reachable — otherwise None is
          just startup lag or an AW outage (handled elsewhere), not a dead tracker.
        """
        if age is not None:
            return age > STALE_THRESHOLD
        return bool(reachable and running_for is not None and running_for > WINDOW_BLIND_GRACE)

    def _get_latest_afk_event_age(self) -> Optional[float]:
        """Return seconds since the most recent AFK event, or None on error.

        The branded idle tracker can register under ActivityWatch's historical
        prefix with BetterFlow identity in the id/name/client (for example
        ``aw-watcher-afk_bf-idle-tracker_<host>``). Discover buckets first and
        prefer that live BetterFlow-owned bucket so a stale legacy
        ``aw-watcher-afk_<host>`` bucket cannot drive restart decisions forever.
        Fall back to older exact-id guesses for older installs.
        """
        buckets = self._get_buckets()
        afk_bucket_ids = [
            bucket_id
            for bucket_id, meta in buckets.items()
            if isinstance(meta, dict)
            and meta.get("type") in {"afkstatus", "aw-watcher-afk"}
        ]
        preferred = [
            bucket_id
            for bucket_id in afk_bucket_ids
            if "bf-idle-tracker" in bucket_id
            or "bf-idle-tracker" in str(buckets[bucket_id].get("name", ""))
            or "bf-idle-tracker" in str(buckets[bucket_id].get("client", ""))
        ]

        age = self._get_latest_event_age_from_buckets(preferred)
        if age is not None:
            return age
        age = self._get_latest_event_age_from_buckets(afk_bucket_ids)
        if age is not None:
            return age

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
            # Heal a stale ad-hoc tracker left by a pre-signing build. Such a
            # copy keeps its fragile ad-hoc TCC grant, which macOS silently
            # denies → the tracker runs but never sees input (blind, no AFK
            # heartbeat → false idle). If the bundle ships a properly
            # Developer-ID-signed tracker, reinstall it so the grant becomes
            # stable across updates. The user must re-grant Input Monitoring
            # once (the new signing identity is a fresh TCC subject), but it
            # then survives every future update instead of breaking on each one.
            if getattr(sys, "frozen", False):
                base = os.path.join(sys._MEIPASS, "resources", "trackers", plat)
                if (
                    os.path.isdir(base)
                    and _binaries_present(base)
                    and self._should_reinstall_trackers(install_dir, base)
                ):
                    logger.warning(
                        "Persistent bf-idle-tracker is ad-hoc/mis-signed while the "
                        "bundled copy is Developer-ID signed (Team %s) — reinstalling "
                        "so its Input Monitoring grant is stable. Re-grant Input "
                        "Monitoring for bf-idle-tracker once after this update.",
                        BETTERQA_TEAM_ID,
                    )
                    self._install_to_persistent(base, install_dir)
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
    def _tracker_team_identifier(binary_path: str) -> Optional[str]:
        """Return the Apple Developer Team Identifier the binary is signed with,
        or None if it is ad-hoc / unsigned / unreadable.

        macOS only — codesign does not exist elsewhere, so other platforms
        always return None (the caller treats that as "can't tell", which never
        triggers a reinstall). codesign writes its details to stderr.
        """
        if platform.system() != "Darwin":
            return None
        try:
            result = subprocess.run(
                ["codesign", "-dv", "--verbose=2", binary_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as e:
            logger.debug("codesign check failed for %s: %s", binary_path, e)
            return None
        for line in (result.stderr or "").splitlines():
            if line.startswith("TeamIdentifier="):
                team = line.split("=", 1)[1].strip()
                return None if team in ("", "not set") else team
        return None

    @classmethod
    def _should_reinstall_trackers(cls, install_dir: str, bundle_dir: str) -> bool:
        """True when the installed bf-idle-tracker is NOT signed with our
        Developer ID team but the bundled one IS.

        That's the stale-ad-hoc case: a tracker copied to the persistent dir by
        a pre-signing build keeps its ad-hoc signature, whose Input Monitoring
        grant macOS silently denies (the tracker is alive but blind — no AFK
        heartbeat). Replacing it with the Developer-ID-signed bundle copy fixes
        it for good. We deliberately do NOT reinstall when the bundle is also
        un-teamed (an old build): swapping ad-hoc for ad-hoc would only churn
        the binary and force a needless re-grant. Returns False off macOS.
        """
        idle = "bf-idle-tracker"
        bundle_team = cls._tracker_team_identifier(
            os.path.join(bundle_dir, idle, idle)
        )
        if bundle_team != BETTERQA_TEAM_ID:
            return False
        installed_team = cls._tracker_team_identifier(
            os.path.join(install_dir, idle, idle)
        )
        return installed_team != BETTERQA_TEAM_ID

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
