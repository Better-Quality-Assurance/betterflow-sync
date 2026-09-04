"""Manage bundled tracker processes (ActivityWatch components, white-labeled)."""

import errno
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
from datetime import datetime, timedelta, timezone
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
# tracker runs but stays blind. See _tracker_reinstall_reason.
BETTERQA_TEAM_ID = "87NVC57J44"

# The pinned upstream release lives in ONE place (src/aw_release.py) and is
# re-exported here. These names stay module-level attributes of aw_manager on
# purpose: callers and tests reach them as `aw_manager.RELEASE_SHA256` and patch
# them with `patch.object`, which only works on a real module global.
try:
    from .machine_arch import common_arches
except ImportError:  # pragma: no cover - frozen/script import path
    from machine_arch import common_arches

try:
    from .aw_release import (  # noqa: F401
        AW_TO_BF_NAMES,
        AW_VERSION,
        RELEASE_ASSETS,
        RELEASE_BASE,
        RELEASE_SHA256,
        asset_key,
        digest_mismatch,
        platform_key,
    )
except ImportError:  # pragma: no cover - frozen/script import path
    from aw_release import (  # noqa: F401
        AW_TO_BF_NAMES,
        AW_VERSION,
        RELEASE_ASSETS,
        RELEASE_BASE,
        RELEASE_SHA256,
        asset_key,
        digest_mismatch,
        platform_key,
    )

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
# Window-title capture telemetry (see
# docs/superpowers/specs/2026-07-22-window-title-capture-telemetry-design.md).
# "Did ANY window event in the last 15 minutes carry a non-empty title?" — a
# platform-independent symptom check for the fault whose cause differs per OS
# (macOS Accessibility not granted / Windows bf-window-tracker blind / Linux X11
# watcher dead). A boolean, deliberately not a ratio: some apps legitimately
# report an empty title (screensaver, login window, app mid-launch), so any
# ratio threshold needs per-platform tuning and then drifts silently. On a
# working machine the answer is yes within one tick; on a broken one it is no,
# forever. This is VISIBILITY ONLY and must never influence tracked/billed time.
WINDOW_TITLE_CAPTURE_LOOKBACK = 900  # 15 minutes
# Cap the fetch: one poll every couple of seconds for 15 min is well under this,
# and we only need to know whether ANY title arrived.
WINDOW_TITLE_EVENT_LIMIT = 500
# Min gap between "tracker components could not be installed" notifications /
# error reports. start() is retried from the health-check tick, so without this
# a fail-closed download would toast the user every cycle.
DOWNLOAD_FAILURE_REPORT_INTERVAL = 3600  # 1 hour
# The one thing that fixes a zero-recording Apple Silicon Mac, spelled once.
# Three surfaces quote it — the log line, the toast, and (since #188) the tray
# state that persists for as long as the fault does. It is the whole remedy, so
# a typo in any single copy is a user typing a command that does nothing on the
# day they are already recording nothing; one constant is what stops the three
# from drifting. The agent cannot run it itself: it needs an admin password.
ROSETTA_INSTALL_COMMAND = "softwareupdate --install-rosetta"
# How long a Rosetta answer stands before force_restart() is allowed to re-ask.
# Installing Rosetta needs no reboot, so the answer CAN change under a running
# agent and a memo held for the life of the process left the user stuck at "still
# blocked" with nothing telling them to relaunch. Five minutes because the two
# costs pull opposite ways: the escalation path that clears this runs every 60s
# for the whole of a total-capture outage, and re-probing on each one puts a
# subprocess fork per minute back on precisely the machine the memo protects.
ROSETTA_REPROBE_INTERVAL = 300.0
# (ops summary, ops fingerprint, user toast) per cause of "this device captures
# NOTHING". Keyed rather than branched so a new cause is a table row. Both are
# the same outage to the person using the machine, so both must reach them —
# a device whose binaries cannot execute recorded zero seconds for two days
# while only a log line said so.
CAPTURE_UNAVAILABLE_REPORTS = {
    "download": (
        "Tracker component download failed — capture unavailable",
        "aw_manager:tracker_download_failed",
        "Tracker components could not be installed, so activity is not "
        "being recorded. Please contact support.",
    ),
    "exec": (
        "Tracker components cannot execute — capture unavailable",
        "aw_manager:tracker_binary_cannot_execute",
        "Tracker components are installed but cannot run on this computer, so "
        "activity is not being recorded. On Apple Silicon Macs this usually "
        "means Rosetta 2 is not installed. Please contact support.",
    ),
}
# Backoff for the DOWNLOAD ITSELF (not just its notification). _start_locked is
# re-entered from the ~60s capture-policy tick, and every re-entry with no
# binaries on disk re-fetches a 115-207 MB archive. Without this a device that
# can never install trackers would pull that archive once a minute forever.
DOWNLOAD_RETRY_MIN_INTERVAL = 300  # 5 minutes
DOWNLOAD_RETRY_MAX_INTERVAL = 3600  # 1 hour


def _get_platform_key() -> str:
    """The on-disk tracker directory name. Delegates to the one definition.

    Note this is the DIRECTORY key, not the download key: a build ships exactly
    one architecture, so the layout under `resources/trackers/` stays per-OS.
    Use `asset_key()` to choose which archive to fetch.
    """
    return platform_key()


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
    # TWO keys, and they are not interchangeable. The DOWNLOAD key carries an
    # architecture on macOS ("darwin-arm64"); the PLATFORM key never does, and
    # everything below that asks "which OS is this" — the ".exe" suffix, the
    # chmod, the quarantine strip — must keep using the platform key. Feeding it
    # the asset key instead silently skips the POSIX branch on every Mac,
    # leaving freshly extracted launchers at zipfile's 0644 with the quarantine
    # xattr intact while this function still logs success.
    key = asset_key()
    plat = platform_key()
    asset = RELEASE_ASSETS.get(key)
    if not asset:
        logger.error(f"No release available for platform: {key}")
        return False

    url = f"{RELEASE_BASE}/{asset}"
    # Defense-in-depth: only ever fetch tracker binaries over HTTPS from GitHub.
    try:
        from .url_safety import assert_safe_final_url, is_safe_fetch_url
    except ImportError:
        from url_safety import assert_safe_final_url, is_safe_fetch_url
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
            assert_safe_final_url(response.geturl(), "Tracker download")
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

        # Integrity check: the tag is pinned but GitHub release assets are
        # mutable by the upstream account, so verify the archive against the
        # vetted hash before extracting (and before any chmod/quarantine-strip).
        # The rule itself lives beside the pins (aw_release.digest_mismatch) so
        # the build script enforces the identical one; RELEASE_SHA256 is passed
        # explicitly because it is this module's global that tests patch.
        problem = digest_mismatch(tmp_zip, key, RELEASE_SHA256)
        if problem:
            logger.error(f"Refusing to install tracker archive: {problem}")
            return False

        logger.info(f"Downloaded {size_mb:.1f} MB (SHA-256 verified), extracting binaries...")

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
        # Optional ops-ingest reporter, assigned by the app after construction
        # (same pattern as sync_engine.error_reporter). A tracker download that
        # fails closed means ZERO capture, so it must not be log-only.
        self.error_reporter = None
        # Set when the tracker bootstrap could not install binaries. Read by the
        # health telemetry so a silently-idle agent is distinguishable from a
        # healthy one that simply has nothing to report.
        self.tracker_download_failed: bool = False
        # monotonic timestamp of the last download-failure notify/report.
        self._last_download_failure_report: float = float("-inf")
        # monotonic timestamp of the last tracker-archive download ATTEMPT, plus
        # the current (exponentially backed off) gap required before retrying.
        self._last_download_attempt: float = float("-inf")
        self._download_retry_interval: float = DOWNLOAD_RETRY_MIN_INTERVAL
        # True while we have no managed watchers of our own (download failed).
        # Distinct from tracker_download_failed: with an external server on the
        # port we still capture, but restart_if_needed/force_restart manage
        # nothing, so the backend should know this device is un-self-healing.
        self._managed_components_unavailable: bool = False
        # True when the most recent evaluation attached to a process that holds
        # the tracker port and does not answer /api/0/info. Re-derived as the
        # FIRST statement of _start_locked, before even the capture-suppressed
        # guard, so it cannot outlive its condition -- reset to False there and
        # set True only on the normal-path attach branch; the other three
        # attach branches only fire when _external_server_capturing() is
        # already true, so False is correct there without them touching this
        # field. Deliberately NOT one of the two capture-dead flags:
        # _start_component clears those on Popen success, and the watcher loop
        # runs after the attach, so anything latched there is wiped inside the
        # same _start_locked call (measured).
        self._external_server_not_responding = False
        # Tri-state cache for the Apple-Silicon-without-Rosetta check. None =
        # not probed yet. Installing Rosetta needs no reboot, so force_restart()
        # clears this to let a device that was fixed recover on its own — but
        # only once per ROSETTA_REPROBE_INTERVAL, because a sustained outage
        # calls force_restart on every 60s cycle and this memo exists to keep
        # `/usr/bin/arch` off that path.
        self._rosetta_missing_cached: Optional[bool] = None
        # monotonic timestamp of the last completed probe, for that interval.
        self._rosetta_probed_at: float = float("-inf")
        # The last answer we LOGGED. Re-probing must not reprint the same error
        # every few minutes for the life of an outage — that is the 60-second
        # log spam the preflight was written to end, arriving by another door.
        self._rosetta_logged: Optional[bool] = None
        # The last answer the probe actually GAVE, as opposed to the memo above
        # which force_restart clears on purpose. A probe that raised is not an
        # answer, and once re-probing exists a transient fork refusal
        # (EAGAIN/EMFILE, or the 10s timeout) can overwrite a known "Rosetta is
        # missing" with a guessed "available". Deliberately NOT cleared by
        # force_restart: its whole job is to survive the memo.
        self._rosetta_missing_conclusive: Optional[bool] = None
        self._rosetta_notified: bool = False
        # Components whose binary is on disk but cannot EXECUTE (EBADARCH /
        # ENOEXEC). Keeps the capture-dead latch honest when only SOME binaries
        # are unrunnable: a sibling that starts fine must not unlatch on behalf
        # of a component that is permanently blind. Guarded by _lifecycle_lock.
        self._exec_failed_components: set[str] = set()

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

            # RETRACT THE LATCH THIS SUBSYSTEM CAN NO LONGER SET (#2413).
            #
            # Both writes to `_idle_tracker_blind` -- the latch after
            # IDLE_BLIND_RESTART_THRESHOLD failed restarts and the clear on
            # recovery -- live inside `_restart_if_needed_locked`'s
            # `if not self._inproc_afk_active` branch. So a device that latched
            # blind while on the external bf-idle-tracker and then moved to
            # in-process AFK kept publishing `idle_tracker_blind: true` with
            # nothing on either side able to take it back: the agent cannot reach
            # its own clear, and the server's readLatchedFlag falls back to the
            # stored column when a heartbeat omits the key.
            #
            # `health_snapshot()` publishes the flag every cycle regardless of
            # which AFK source is live, so an unretractable true is an alert that
            # outlives the thing it describes.
            #
            # BEFORE the early return below, deliberately: that return is gated
            # on `_stop_external_when_inproc`, and this defect does not depend on
            # that setting. Putting the clear after it would fix the config we
            # happen to run and leave the other one latched.
            #
            # Unconditional rather than transition-only. The latch is reachable
            # only from the `not _inproc_afk_active` branch, so a transition-only
            # clear would be sufficient -- but this is one boolean under a lock
            # already held, and "cannot leave a stale latch" is worth more than
            # the saved write.
            if active:
                self._idle_tracker_blind = False

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

    def _dispatch_download_failure_report(self, reason: str = "download") -> None:
        """Throttle-check, then hand the notify/report off to a daemon thread.

        ``reason`` keys CAPTURE_UNAVAILABLE_REPORTS: the archive never arrived
        ("download"), or it arrived and cannot execute ("exec"). Both share the
        throttle — they are the same outage and never happen together.

        The caller holds _lifecycle_lock, and the notification path shells out
        (osascript/notify-send) — blocking there would stall `is_managing` and
        set_capture_suppressed, i.e. the watchdog and sync loop, for seconds on
        a hung helper. Only the cheap throttle bookkeeping stays under the lock.
        """
        now = time.monotonic()
        if now - self._last_download_failure_report < DOWNLOAD_FAILURE_REPORT_INTERVAL:
            return
        self._last_download_failure_report = now
        threading.Thread(
            target=self._report_download_failure,
            args=(reason,),
            name="aw-download-failure-report",
            daemon=True,
        ).start()

    def _report_download_failure(self, reason: str = "download") -> None:
        """Notify the user and the ops ingest that tracker components are not
        usable (so tracking is unavailable). Runs off _lifecycle_lock;
        throttling is done by _dispatch_download_failure_report.

        Ops ingest FIRST, user toast last: this failure happens during startup,
        and the macOS notification path (NSUserNotificationCenter) is documented
        in main.py to deadlock when driven off the main thread before the Cocoa
        run loop is up. If it hangs, the ops signal must already be away.
        """
        summary, fingerprint, toast = CAPTURE_UNAVAILABLE_REPORTS.get(
            reason, CAPTURE_UNAVAILABLE_REPORTS["download"]
        )
        reporter = self.error_reporter
        if reporter is not None:
            try:
                reporter.capture(
                    summary,
                    level="error",
                    tags={"component": "aw_manager", "platform": _get_platform_key()},
                    context={"aw_version": AW_VERSION},
                    fingerprint=fingerprint,
                )
            except Exception:
                logger.warning("Failed to report tracker download failure", exc_info=True)
        try:
            try:
                from .notifications import send_notification
            except ImportError:
                from notifications import send_notification
            send_notification(
                "BetterFlow tracking unavailable",
                toast,
            )
        except Exception:
            logger.warning("Failed to notify about tracker download failure", exc_info=True)

    @staticmethod
    def _tracker_tree_arches(tree_dir: str) -> Optional[set]:
        """The architectures EVERY readable tracker in `tree_dir` can run as.

        The single answer to "what architecture is this tracker tree", used by
        the Rosetta start gate and the reinstall decision alike (and mirrored by
        `scripts/download_aw.py` at build time). Three sites that disagree about
        it is three different ideas of which tree is runnable, which is how a
        tree one of them accepts becomes a tree another one spawns and watches
        die.

        The layout is `_resolve_binary_path`'s business, not this method's: it
        is what `_binaries_present` and `_start_component` use to decide which
        file actually gets SPAWNED, and it accepts the legacy flat layout
        (`<tree>/<component>` plus an adjacent `Python/`) as well as the bundled
        one. Re-spelling a single branch of it here would judge a flat install
        by files that do not exist, answer "could not tell" for every component,
        and leave exactly the stale x86_64 tree this gate exists to catch both
        unblocked and un-reinstalled. `common_arches` owns the rule; None means
        "could not tell" — see its docstring.
        """
        paths = [_resolve_binary_path(tree_dir, name) for name in ALL_COMPONENTS]
        return common_arches([p for p in paths if p])

    @staticmethod
    def _describe_arches(arches: set) -> str:
        """Render an arch set for a log line, including the empty case.

        Empty is not "unknown" here — it is a tree whose components share no
        architecture — so it must not print as a blank.
        """
        return "/".join(sorted(arches)) or "a mix with no common architecture"

    @classmethod
    def _bundled_trackers_need_rosetta(cls, binaries_dir: Optional[str]) -> bool:
        """True when the trackers in `binaries_dir` cannot run natively here.

        Reads the Mach-O headers of the trackers we are actually going to
        spawn. Those are the artifacts whose requirement decides the question —
        not the pinned asset name, which describes what a *fresh* install would
        fetch and says nothing about the copy already sitting at the persistent
        path.

        **A pure query, and it takes the directory rather than resolving one.**
        `_get_binaries_dir()` is a resolver that also INSTALLS: in a frozen
        macOS build it can rewrite the whole persistent tracker tree before
        answering. A predicate that called it would report on binaries it had
        just created rather than on the state it was asked about, and would
        cost two `codesign` forks on a path that runs every 60 seconds. The
        caller resolves once and passes the answer in.

        Every component is consulted, via `common_arches` — see its docstring
        for why the first readable one is not allowed to settle it.

        Fails toward **False** (do not block) whenever the answer is not
        established: no binaries resolved yet on a first run, unreadable
        headers, a path that does not exist. That direction is deliberate and
        matches `_rosetta_missing`'s own broken-probe behaviour — a start
        attempt that turns out to be wrong still hits the EBADARCH handler in
        `_start_component`, whereas a false block records nothing at all and is
        the exact harm this whole area exists to prevent.
        """
        if not binaries_dir:
            # First run, nothing downloaded yet. The download picks the archive
            # matching this host (see aw_release.asset_key), so there is no
            # reason to assume the wrong architecture is coming.
            return False

        arches = cls._tracker_tree_arches(binaries_dir)
        if arches is None:
            # Nothing readable. Not established, so do not block.
            return False
        # An EMPTY set is established, and the worst case: the tree is mixed
        # (a reinstall that failed partway), so no single architecture runs all
        # of it. Blocking is right — half the trackers would EBADARCH on every
        # cycle otherwise, with AFK capture silently dead and nothing recorded
        # about why.
        return platform.machine() not in arches

    def _rosetta_required(self, binaries_dir: Optional[str]) -> bool:
        """True when capture is blocked for want of Rosetta 2 — the real gate.

        Two independent questions, and conflating them is the defect this
        method exists to fix (#216):

            does the BINARY need Rosetta?   _bundled_trackers_need_rosetta()
            does the HOST lack Rosetta?     _rosetta_missing()

        Only both together block capture. The predicate used to ask the second
        alone, which was correct exactly while every bundled macOS tracker was
        x86_64 — true from day one until the ActivityWatch pin moved to
        v0.14.0b4. From that moment the host question is the wrong one: a clean
        Apple Silicon Mac with no Rosetta still answers "missing", and native
        arm64 trackers that would run perfectly well would never be spawned.
        Shipping native binaries and keeping this gate would have left Rosetta
        just as mandatory as before, with nothing in any test to say so.

        Order matters for cost as well as correctness. The binary check reads a
        few bytes of already-resolved local files; `_rosetta_missing()` forks
        `/usr/bin/arch`. Asking the cheap, decisive question first means a
        native install never pays for the fork at all, on a path that runs every
        60 seconds — and it leaves `_rosetta_missing_cached` as `None` there, so
        `capture_blocked_remedy()` correctly offers no Rosetta instruction on a
        machine that does not need one.

        Args:
            binaries_dir: The already-resolved tracker directory (None if there
                is none yet). Taken as an argument rather than resolved here so
                this stays a query: see `_bundled_trackers_need_rosetta`.
        """
        if sys.platform != "darwin":
            return False
        if not self._bundled_trackers_need_rosetta(binaries_dir):
            return False
        return self._rosetta_missing()

    def _rosetta_missing(self) -> bool:
        """True only when this Mac is Apple Silicon AND cannot run x86_64.

        **This is the HOST question only.** It answers "can this machine
        execute x86_64 code", which stopped being sufficient on its own once
        the bundled trackers became native arm64. `_rosetta_required()` is the
        gate; this is one of its two inputs. Calling it directly to decide
        whether capture is blocked is the bug described in #216.

        RELEASE_ASSETS is x86_64 on every platform because upstream
        ActivityWatch publishes nothing else for macOS: v0.13.2 has only
        `activitywatch-v0.13.2-macos-x86_64.zip`, and an arm64 asset first
        appears in the v0.14.0b* betas. So on Apple Silicon these binaries need
        Rosetta 2, and without it every start raises

            [Errno 86] Bad CPU type in executable

        forever, at 60-second intervals, while the agent reports itself healthy.
        Laszlo Fabian Raul's device did exactly that for two days, recording
        zero seconds on 2026-07-22 and 07-23.

        The probe runs a known-good system binary under the x86_64 personality.
        It is the same thing the failing spawn does, minus the tracker, so a
        false negative here would also have been a real spawn failure. Cached:
        Rosetta cannot appear or vanish without a reboot, and this is on the
        60-second start path.
        """
        if self._rosetta_missing_cached is not None:
            return self._rosetta_missing_cached

        if sys.platform != "darwin" or platform.machine() != "arm64":
            self._rosetta_missing_cached = False
            self._rosetta_missing_conclusive = False
            return False

        try:
            result = subprocess.run(
                ["/usr/bin/arch", "-x86_64", "/usr/bin/true"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            missing = result.returncode != 0
            self._rosetta_missing_conclusive = missing
        except Exception as e:
            # Probe itself failed. Do NOT claim Rosetta is missing on a broken
            # probe — that would refuse to start trackers on a machine that is
            # fine. Fail towards attempting the start; the EBADARCH handler in
            # _start_component still catches the real thing.
            # "Fail toward available" is the right default only when we have
            # never had a real answer. force_restart re-probes every
            # ROSETTA_REPROBE_INTERVAL, so on an affected Mac this path runs
            # ~288 times a day, and each run is a fresh chance for a fork
            # refusal to replace a KNOWN "missing" with a guess. That guess is
            # not free: it makes capture_blocked_remedy() return None, so the
            # tray drops the one actionable sentence and falls back to
            # "ActivityWatch not responding" -- the string #188 exists to
            # delete -- and it flips _rosetta_logged so the log claims "Rosetta
            # 2 is now available" on a machine where nothing changed.
            #
            # So carry the last CONCLUSIVE answer forward and guess only when
            # there has never been one. Same determined/conclusive split
            # machine_arch.ProbeResult already uses, for the same reason: a
            # failed probe must not masquerade as a result.
            logger.warning("Rosetta probe failed, keeping last answer: %s", e)
            missing = (
                self._rosetta_missing_conclusive
                if self._rosetta_missing_conclusive is not None
                else False
            )

        self._rosetta_missing_cached = missing
        self._rosetta_probed_at = time.monotonic()
        # Log the ANSWER changing, not the probe running. force_restart clears
        # the memo so an installed Rosetta is noticed without an agent restart,
        # which means this function now runs repeatedly during an outage instead
        # of once per process; reprinting the same error each time would rebuild
        # the identical-line spam the preflight exists to have ended.
        if missing is not self._rosetta_logged:
            if missing:
                logger.error(
                    "This Mac is Apple Silicon and Rosetta 2 is not installed, so the "
                    "x86_64 ActivityWatch trackers cannot run. Nothing will be tracked "
                    "until it is installed: run `%s` "
                    "(needs an admin password, so the agent cannot do it for you).",
                    ROSETTA_INSTALL_COMMAND,
                )
            elif self._rosetta_logged is True:
                # The recovery. Worth a line of its own: it is the only record
                # that the user's install landed and capture can resume, and
                # support reads these logs to answer exactly that question.
                logger.info(
                    "Rosetta 2 is now available — the x86_64 trackers can start. "
                    "Capture should resume on this cycle."
                )
            self._rosetta_logged = missing
        return missing

    def capture_blocked_remedy(self) -> Optional[str]:
        """One sentence for a surface that PERSISTS, or None.

        Returns the user-facing remedy when this device is recording nothing
        for a cause the person at the keyboard can actually fix, and ``None``
        whenever we have not established such a cause. Today that is exactly
        one cause: Apple Silicon without Rosetta 2.

        **Why this exists.** The Rosetta fault already reached the user twice —
        a one-shot toast, and a red tray. The toast fires once per process,
        during the noisiest minute of a new laptop's first launch, and
        ``clear_notifications()`` wipes it on quit; the tray persisted for the
        whole outage and said *"ActivityWatch not responding"*, naming a
        component the user has never heard of. So the fault was reported on a
        surface nobody could act on and mis-labelled on the surface that lasted.
        Carmen Lapusan lost ~90 minutes to that on 2026-08-13, after Laszlo
        Fabian Raul's device recorded zero seconds across 2026-07-22/23. This
        method is what lets the persistent surface carry the actionable text.

        **Reads the MEMO, never the probe.** ``_rosetta_missing()`` forks
        ``/usr/bin/arch``; this is called while building a tray message, on the
        sync cycle, so it consults the cached answer only. The same rule the
        tray's own ``_arch_menu_item`` and ``serial_menu_row`` carry.

        **Two conditions, not one, and the second is the one that was missing.**
        A Rosetta memo of ``True`` says these binaries cannot execute here. It
        does NOT say this device is recording nothing — a user who installed a
        native arm64 ActivityWatch themselves has a live server on the port and
        a memo that will read ``True`` forever. Gating on the memo alone put
        *"Not recording — Rosetta 2 required"* in front of someone who IS
        recording, about a component irrelevant to whatever actually failed,
        the moment any unrelated bucket failure escalated.

        So the remedy also requires ``tracker_download_failed`` — the manager's
        own latch for "this device captures nothing", set on the same branch of
        ``_start_locked`` and deliberately NOT set when an external server is
        attached. ``_managed_components_unavailable`` is the wrong flag for this
        question: it is true on the recording-via-external device too, by
        design, because our watchers really are unavailable there.

        **"Attached" means answering ``/api/0/info``, not holding the socket.**
        The first cut of the condition above rode on ``_port_in_use()``, a bare
        TCP connect, so ANY process on port 5600 withheld the remedy — and a
        Rosetta-blocked Mac holding a dead ``bf-data-service`` is precisely a
        device with a listener and no capture, un-reapable because our binaries
        have never run there. That returned ``None``, the tray fell back to
        *"ActivityWatch not responding"*, and the fleet saw
        ``tracker_download_failed=False``: the exact outage this method exists
        to name, restored on the exact device it was written for.

        **Deliberately NOT under ``_lifecycle_lock``**, unlike its sibling
        ``health_snapshot()`` which reads the same latch under it. The caller is
        the tray-message path, and ``force_restart()`` holds that lock across
        process spawns — taking it here would block a UI update behind tracker
        teardown. Both reads are single attribute loads of a bool/tri-state, so
        there is no torn read; the worst case is a value that was true an
        instant ago, which is all a tray message can ever claim anyway.

        (Until this commit the argument was "the memo is write-once and
        monotonic". That stopped being true when ``force_restart()`` began
        clearing it so an installed Rosetta can be noticed without an agent
        restart. The conclusion survives; the reason for it did not, and a stale
        reason is worse than none.)

        **Three states, not two.** The memo is ``None`` until something probed:

            True  -> established: no Rosetta, these binaries cannot run
            False -> established: Rosetta is present, this is some OTHER fault
            None  -> nobody has asked yet

        Only ``True`` earns a remedy. Treating ``None`` as ``True`` would put a
        confident Rosetta instruction on every unrelated outage on every
        platform, including machines that already have it — a guess, on the one
        surface that exists to stop the user guessing. Fail toward saying
        nothing, exactly as ``_rosetta_missing()`` itself fails toward
        attempting the start.
        """
        if self._rosetta_missing_cached is not True:
            return None
        if not self.tracker_download_failed:
            return None
        return (
            "Not recording — Rosetta 2 required. Open Terminal and run: "
            f"{ROSETTA_INSTALL_COMMAND}"
        )

    def _notify_rosetta_required_once(self) -> None:
        """Tell the user once per process. The tray state is set by the caller's
        health reporting; this is the actionable instruction, and repeating it
        every 60s would train them to dismiss it."""
        if self._rosetta_notified:
            return
        self._rosetta_notified = True
        try:
            try:
                from .notifications import NotificationOutcome, send_notification
            except ImportError:
                from notifications import NotificationOutcome, send_notification
            outcome = send_notification(
                "BetterFlow can't track on this Mac",
                "Rosetta 2 is required. Open Terminal and run: "
                f"{ROSETTA_INSTALL_COMMAND}",
            )
        except Exception:
            logger.debug("Rosetta notification failed", exc_info=True)
            return

        # This device records ZERO time until someone installs Rosetta, and
        # the notification is the only thing that asks them to. Whether it
        # arrived was previously unknowable — the notice shipped in v1.5.118
        # and a user still lost ~90 minutes on v1.5.122 (#204), with nothing
        # anywhere to say whether they were ever told. Say so now, either way.
        if outcome is NotificationOutcome.DELIVERED:
            logger.info(
                "Rosetta notice accepted by Notification Center. That is not "
                "proof the user read it — a Focus mode files it silently."
            )
            return

        logger.error(
            "Rosetta notice was NOT delivered (%s). This Mac is recording no "
            "time and the person at the keyboard has not been asked to fix it; "
            "the tray state and the tracker_install_failed signal are the only "
            "remaining routes to them.",
            outcome.value,
        )
        reporter = self.error_reporter
        if reporter is not None:
            try:
                reporter.capture(
                    "Rosetta required notice not delivered to the user",
                    level="error",
                    tags={
                        "component": "aw_manager",
                        "platform": _get_platform_key(),
                        "notification_outcome": outcome.value,
                    },
                    context={"aw_version": AW_VERSION},
                    fingerprint="rosetta-notice-undelivered",
                )
            except Exception:
                logger.warning(
                    "Failed to report undelivered Rosetta notice", exc_info=True
                )

    def _start_locked(self) -> bool:
        # Reset per evaluation -- the FIRST statement in this method, before
        # even the capture-suppressed guard, so "re-derived on every
        # _start_locked call" is literally true with no exception to document.
        # False is also the honest value while capture is suppressed: the
        # device is not attached to a dead external server, it is not attached
        # to anything. Of the four branches below that can attach to an
        # external server -- the Rosetta attach, the backoff attach, the
        # download-failure attach, and the normal-path attach -- only the
        # normal-path attach can attach to a NON-responding one; the other
        # three attach only when _external_server_capturing() is already true
        # and each returns immediately after, so False is already correct
        # there without them touching this field. Resetting here and setting
        # True in the one normal-path branch is the whole invariant.
        self._external_server_not_responding = False

        # Every route back to a running tracker funnels through here, so this one
        # guard is enough to keep start()/restart_if_needed()/force_restart() from
        # resurrecting capture while it is suppressed.
        if self._capture_suppressed:
            logger.debug("Tracker start refused: capture is suppressed")
            return False

        # Probed BEFORE the Rosetta branch below, not after it. Our managed
        # binaries being unrunnable says nothing about whether SOMETHING is
        # capturing on this port: a user who hit the "nothing is being tracked"
        # wall and installed a native arm64 ActivityWatch themselves is
        # recording perfectly well. With the probe below the branch, that device
        # latched the capture-dead flags and never reached the external-attach
        # path at all, so it reported itself as recording nothing while
        # recording — and, once capture_blocked_remedy() started reading those
        # flags, would have been told to install Rosetta over an unrelated
        # bucket failure. The download-failure path a few lines down has always
        # asked this question in this order; the Rosetta path did not.
        server_already_running = self._port_in_use()

        # Resolved BEFORE the Rosetta gate, and exactly once. _get_binaries_dir
        # is also the installer: on a frozen macOS build it replaces a stale
        # x86_64 tracker tree with the bundled arm64 one. Asking the gate first
        # and resolving afterwards would block that very machine on the trackers
        # it is about to stop having — capture dead forever on a device whose
        # whole fix is one directory copy away.
        binaries_dir = self._get_binaries_dir()

        # Trackers this machine cannot execute. Checked BEFORE spawning rather
        # than after 21 identical EBADARCH failures, and reported rather than
        # retried, because no amount of retrying installs Rosetta.
        # See _rosetta_required() — and note it asks what the INSTALLED BINARIES
        # need, not merely what this host supports.
        if self._rosetta_required(binaries_dir):
            # Managed watchers are unavailable either way — these binaries do
            # not execute on this machine, so nothing here self-heals.
            self._managed_components_unavailable = True
            # `server_already_running` is a TCP connect, and a held socket is
            # NOT a running tracker. This branch pays for one HTTP ask before
            # granting the carve-out.
            #
            # It used to read "Here — and ONLY here", with a paragraph below
            # explaining why the other paths could get away with the TCP answer
            # because they only decide whether to START something. That was
            # already half-false after #233, and now every attach point asks:
            # this branch and the two download-path attaches ask through
            # _external_server_capturing(); the normal-path attach asks too, via
            # _server_responding() directly (see the external-attach design note
            # above it) -- it just does not ACT on a "no", only reports it
            # through _external_server_not_responding. So "only here" is wrong,
            # and so is "the normal-path attach doesn't ask" -- it asks, it just
            # doesn't decide anything on the answer.
            #
            # What is still TRUE and specific to this branch: on a
            # Rosetta-missing Mac the listener is un-reapable by construction --
            # _reap_orphan_processes is path-scoped to binaries_dir and OUR
            # binaries have never executed on this machine -- so the only
            # decision left here is what to TELL the person, and getting that
            # wrong cost ~90 minutes once.
            #
            # Failing this way is also the safe direction: a false "responding"
            # withholds the remedy (the regression below), while a false "not
            # responding" merely offers a Rosetta instruction to a device that
            # is, in fact, running an ARM ActivityWatch which just failed to
            # answer /info within 2s — visible, wrong for one cycle, and it
            # self-corrects on the next probe.
            if self._external_server_capturing():
                # ...but an external server IS capturing on this port, so this
                # is not a "capturing nothing" device. Verbatim the rule the
                # download-failure branch below already applies: attach, log,
                # and do NOT latch the capture-dead flag or alarm the user.
                # Telling this person to install Rosetta would name a component
                # irrelevant to whatever actually went wrong.
                logger.warning(
                    "Managed trackers cannot run (no Rosetta 2), but an external "
                    "server is running on port %s — attaching to it",
                    self.aw_port,
                )
                self._using_external = True
                # CLEAR the latch, do not merely decline to set it. This branch
                # returns before the "binaries resolved" clear forty lines down,
                # so on a Rosetta-missing Mac nothing else ever unsets it — and
                # a single /info timeout on a healthy external server would
                # otherwise latch "captures nothing" for the life of the
                # process, putting a permanent "install Rosetta" in front of
                # someone who IS recording and reporting a false capture-dead
                # device to the fleet. Found by walking three cycles with one
                # transient blip in them, not by reading.
                #
                # Safe because of what this branch has just established: a
                # server ANSWERED /api/0/info on our port. That is the same
                # evidence the flag's own name asks for, so clearing it here is
                # the flag being accurate rather than an optimistic reset.
                self.tracker_download_failed = False
                return True
            if server_already_running:
                # Held, but dead. Worth its own line: this device looks
                # "started" to every port-level check in the file while
                # capturing nothing, and nothing here can reap the listener.
                logger.warning(
                    "Managed trackers cannot run (no Rosetta 2) and the process "
                    "holding port %s does not answer /api/0/info — treating this "
                    "device as capturing nothing",
                    self.aw_port,
                )
            self.tracker_download_failed = True
            self._notify_rosetta_required_once()
            return False

        if binaries_dir and not (self._exec_failed_components - self._disabled_components):
            # Clear the latch on EVERY route that resolves usable binaries, not
            # just a fresh download — the frozen-bundle path installs trackers
            # without downloading, so a device that recovered via an app update
            # would otherwise keep reporting tracker_download_failed forever and
            # train the ops ingest to ignore the signal.
            #
            # But NOT while a component is still known-unrunnable. "binaries_dir
            # resolved" proves they DOWNLOADED; it has never proved they EXECUTE,
            # and tracker_download_failed now also means "cannot execute" (see the
            # EBADARCH/ENOEXEC handler in _start_component). Clearing here on an
            # exec-broken device — before the watcher loop below re-latches it, or
            # when BF_SERVER/_wait_for_server bails first and the loop never runs —
            # reports the device healthy while it captures nothing. The successful
            # _start_component below is the only proof of execution and clears the
            # flags itself under this exact same guard; that is where the clear
            # belongs. _lifecycle_lock (RLock) is held by our caller.
            self.tracker_download_failed = False
            self._managed_components_unavailable = False

        # Auto-download if binaries not found
        if not binaries_dir:
            now = time.monotonic()
            since_last = now - self._last_download_attempt
            if since_last < self._download_retry_interval:
                # Backed off. Report the same outcome the last real attempt did:
                # an attached external server still captures, nothing else does.
                logger.debug(
                    "Skipping tracker download retry (%.0fs since last attempt, "
                    "backoff %.0fs)",
                    since_last,
                    self._download_retry_interval,
                )
                self._managed_components_unavailable = True
                if self._external_server_capturing():
                    # The comment above says "an attached external server still
                    # captures" and this branch did not carry it out: it left
                    # tracker_download_failed latched and _using_external False,
                    # so a Mac that IS recording kept reporting itself
                    # capture-dead to the fleet -- for nearly every 60s cycle,
                    # because the retry interval escalates to an hour (#223).
                    #
                    # Guarded on _server_responding(), NOT on the bare
                    # server_already_running TCP connect, for the same reason
                    # the Rosetta attach path forty lines up is: a held socket
                    # proves a process, never a capture. Clearing the flag on a
                    # held-but-dead port would report a silent device healthy,
                    # which is the failure this flag exists to catch.
                    self._using_external = True
                    self.tracker_download_failed = False
                    return True
                # RE-LATCH, symmetric with the clear above and with the Rosetta
                # sibling's `else` forty lines up. #230 copied the clear and not
                # this, so the clear from a PREVIOUS cycle of this same branch
                # survived into one where nothing is capturing: the device kept
                # publishing tracker_download_failed=False while recording
                # nothing, for up to DOWNLOAD_RETRY_MAX_INTERVAL (an hour) per
                # cycle, which the fleet's no-capture alert reads as healthy.
                # Its own tests could not see it -- every fixture hard-set the
                # flag True, so no test drove this branch twice.
                self._using_external = False
                self.tracker_download_failed = True
                return False
            self._last_download_attempt = now
            logger.info("Tracker components not found, downloading...")
            install_dir = _get_install_dir()
            if _download_aw_binaries(install_dir):
                binaries_dir = install_dir
                self.tracker_download_failed = False
                self._managed_components_unavailable = False
                self._download_retry_interval = DOWNLOAD_RETRY_MIN_INTERVAL
            else:
                logger.error("Failed to download tracker components")
                # Escalate 5 min -> 1 h so a permanently-failing device stops
                # re-pulling a multi-hundred-MB archive on every policy tick.
                self._download_retry_interval = min(
                    self._download_retry_interval * 2, DOWNLOAD_RETRY_MAX_INTERVAL
                )
                self._managed_components_unavailable = True
                if self._external_server_capturing():
                    # Asked, not assumed. This branch read the bare TCP connect
                    # until #246: a socket held by a process that no longer
                    # answers /api/0/info reported this device as started and
                    # attached while it captured nothing. #233 closed this class
                    # at two sites and its message said "the last two"; this was
                    # the third.
                    #
                    # An external server is already capturing on this port, so
                    # this is NOT a "capturing nothing" situation — only our
                    # managed watchers are unavailable. Log it; don't latch the
                    # flag or alarm the user, and keep reporting success.
                    logger.warning(
                        "Managed tracker components unavailable, but an external "
                        "server is running on port %s — attaching to it",
                        self.aw_port,
                    )
                    self._using_external = True
                    return True
                # Fail-closed download (bad/absent pinned SHA, missing binaries)
                # means the agent keeps running while capturing NOTHING. Surface
                # it instead of leaving a lone log line on the user's machine.
                self.tracker_download_failed = True
                self._dispatch_download_failure_report()
                return False

        # NOT _external_server_capturing(), deliberately, and this is no longer
        # a deferred bug -- it is a decided design. A foreign holder is
        # unreapable (_reap_orphan_processes is path-scoped to our binaries), so
        # attaching is the behaviour that keeps the watchers alive and recovers
        # when the holder dies; both attempts to change it regressed (table
        # below). The device now REPORTS the condition instead, via
        # _external_server_not_responding on the heartbeat. Change the reporting
        # if it is wrong; do not change this line.
        #
        # Asking here was written, measured and reverted twice. The problem is
        # not the question, it is what you can do with a "no": the only answer
        # available is to start our own server, it cannot bind a port somebody
        # else owns, and `_wait_for_server` failing then runs the blanket
        # `self.stop()` below. Both repairs made things WORSE than leaving it
        # alone. Driven through main.py's real tick order against a corpse that
        # releases the port at tick 4:
        #
        #   as-is (this code)     watchers=2 throughout, and recovers the
        #                         moment the corpse dies
        #   ask + blanket stop()  watchers=0
        #   ask + skip the stop() watchers=0, AND a dead bf-data-service left
        #                         in _processes, which disarms
        #                         set_capture_suppressed's
        #                         `elif not self._processes` rebuild route (:752)
        #
        # SCOPE OF THAT MEASUREMENT, because the first draft of this note
        # overstated it and then prescribed work on the strength of the
        # overstatement: it drove set_capture_suppressed and restart_if_needed
        # only. It did NOT drive the unreachable watchdog, and force_restart()
        # is a SECOND route into _start_locked -- main.py's
        # `elif not self.aw.is_running()` is a SIBLING of the is_managing gate,
        # not inside it, so after _AW_UNREACHABLE_ESCALATE_SECONDS (180s) it
        # fires and rebuilds. Measured from the wedge state: force_restart ->
        # _start_locked called, watchers restored. So both reverted variants
        # cost an outage BOUNDED by that escalation, not a permanent one. They
        # are still worse than this code, which never loses the watchers.
        #
        # The fix likely belongs at a DIFFERENT LAYER -- the rebuild route, or a
        # spawn that reaps only the server it started -- but that is now an
        # opinion rather than something the measurement establishes. Do not flip
        # this line on its own; two attempts produced two different regressions.
        if server_already_running:
            # ASK -- and deliberately do NOT act on the answer here. Read the
            # external-attach design note above: a foreign holder is unreapable
            # (_reap_orphan_processes is path-scoped to our own binaries), so
            # attaching is the behaviour that keeps the watchers alive and
            # self-heals when the holder dies. Two attempts to change that both
            # regressed. What was missing was never the decision -- it was
            # TELLING THE FLEET, which is all this does.
            self._external_server_not_responding = not self._server_responding()
            if self._external_server_not_responding:
                logger.warning(
                    "Attached to the process holding port %s, but it does not "
                    "answer /api/0/info — this device is capturing NOTHING",
                    self.aw_port,
                )
            else:
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
                # The blanket stop() is deliberate and load-bearing: emptying
                # `_processes` is what re-arms set_capture_suppressed's
                # `elif not self._processes` rebuild route (:752). A guard that
                # skipped this to preserve watchers was tried and reverted -- it
                # left a dead bf-data-service in `_processes` and disarmed that
                # route, so the device sat with zero watchers until the 180s
                # unreachable escalation fired force_restart (which IS a second
                # route in; the first draft of this note wrongly called :752 the
                # only one). Bounded, not permanent -- and still worse than
                # keeping the watchers. See the external-attach design note above.
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
        # Reset here too, before the early return below, mirroring the reset at
        # the top of _start_locked: once we stop, we are not attached to
        # anything, so "attached to a non-responding external server" cannot be
        # true. set_capture_suppressed(True) calls ONLY this method, never
        # _start_locked -- so without this reset a corpse-attach flag latched on
        # a prior cycle survives suppression, and a device that is not
        # capturing BY DESIGN reports itself as attached to a dead server.
        self._external_server_not_responding = False
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
            # Re-probe Rosetta on the way back up. The memo is otherwise written
            # once per process and cleared nowhere, so a user who read the tray,
            # ran `softwareupdate --install-rosetta` and watched it finish still
            # saw "Not recording — Rosetta 2 required" until they thought to
            # quit and relaunch the agent — and nothing on any surface asked
            # them to. That is the dead end tray.py's capture_permissions_row()
            # already names in its own docstring: for a surface whose whole job
            # is "did my fix work?", still saying "blocked" afterwards is the
            # failure, not a cosmetic lag.
            #
            # Here rather than on a timer because this is the one path that
            # already exists to rebuild a stack believed dead, and it is exactly
            # where a device in this state arrives: capture is gone, so the
            # unreachable watchdog escalates and calls force_restart on every
            # cycle. Clearing the memo makes the very next start attempt real,
            # the trackers spawn, and the tray goes green on its own.
            #
            # Rate-limited, and that is not tidiness. _note_aw_unreachable
            # returns True on EVERY cycle once the 180s grace period is up (only
            # a reachable ActivityWatch resets it), so a device that is capturing
            # nothing force-restarts every 60 seconds for as long as the outage
            # lasts — which on a Rosetta-missing Mac is indefinitely. Clearing
            # unconditionally would therefore fork `/usr/bin/arch` once a minute
            # on exactly the machine this memo was introduced to protect, which
            # is the "21 identical failures at 60-second intervals" pattern the
            # preflight replaced, wearing a different hat.
            #
            # ROSETTA_REPROBE_INTERVAL bounds it to one fork per five minutes,
            # so the user who runs the command waits at most that long for the
            # tray to go green — a loop that closes slowly still closes, and an
            # unbounded fork rate on a broken device does not.
            #
            # A probe that fails still fails toward "available" (see
            # _rosetta_missing), so the worst case is one spawn attempt the
            # EBADARCH handler already covers.
            if time.monotonic() - self._rosetta_probed_at >= ROSETTA_REPROBE_INTERVAL:
                self._rosetta_missing_cached = None
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
                # #215 — ASK THE SERVER, not the socket. A process holding port
                # 5600 that no longer answers HTTP satisfies a bare TCP connect,
                # so this branch used to report capture as healthy on a device
                # recording nothing, and the fleet was told everything was fine.
                #
                # `_server_responding()` is the /api/0/info ask `_wait_for_server`
                # already performs, extracted by PR 213 so the callers cannot
                # drift. One request, 2s timeout — a health check is exactly the
                # caller that should pay that.
                return self._server_responding()

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
            managed_unavailable = self._managed_components_unavailable
            # Written under this same lock in _start_locked, so read under it too
            # instead of racing a concurrent start/restart.
            download_failed = self.tracker_download_failed
            external_dead = self._external_server_not_responding

        window_age = self._get_latest_window_event_age()
        # When the agent owns the AFK stream in-process, the external
        # bf-idle-tracker bucket is ignored — do NOT report its (likely stale)
        # event age. Reporting it makes the backend fire false "Active time not
        # advancing" alerts for an agent that is billing correctly (the external
        # tracker is still running but irrelevant). The restart count + blind flag
        # are already 0/False on this path (the restart loop is suppressed).
        afk_age = None if inproc else self._get_latest_afk_event_age()
        # Same rule as the ages above: tracker-server I/O, no shared mutable
        # state, so it stays OUTSIDE the lifecycle lock.
        titles_recent = self._window_titles_captured_recently()
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
            # True  => at least one window event in the last 15 min had a
            #          non-empty title.
            # False => window events exist but every title is empty (macOS
            #          Accessibility missing / Windows tracker blind / Linux
            #          watcher dead). This is the finding.
            # None  => no window events in the period, or the probe couldn't run.
            #          NEVER collapse None into False — "not tracking" is a
            #          different fault, already covered by the age field above.
            "window_titles_captured_recently": titles_recent,
            # True => the tracker binaries could not be installed (fail-closed
            # integrity check or a bad archive), so this device is capturing
            # NOTHING even though the agent looks alive.
            "tracker_download_failed": download_failed,
            # True => we have no managed watchers of our own. Capture may still
            # be flowing via an external server on the port, but nothing here can
            # restart or self-heal it, so an outage will not recover on its own.
            "managed_components_unavailable": managed_unavailable,
            # We are attached to a process that holds the tracker port and does
            # not answer /api/0/info, so nothing is being captured -- but this is
            # NOT tracker_download_failed: our binaries are fine and there is
            # nothing to reap (the holder is not ours). Distinct key so the alert
            # can say "something else owns port 5600" instead of blaming the
            # download.
            "external_server_not_responding": external_dead,
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
        # DELIBERATELY the bare port read, not _external_server_capturing() --
        # and not _external_server_not_responding either. That field reports
        # the ATTACH-side condition (does the server we are attached to answer
        # /info); it is set True only in _start_locked's normal-path attach
        # branch, and every other write resets it to False. This predicate
        # asks something that field cannot answer: has the external server's
        # socket disappeared. Tearing down external mode achieves nothing when
        # the holder is unreapable (_reap_orphan_processes is path-scoped to
        # our own binaries), so asking /info and acting on a "no" was tried
        # and reverted instead: on a HEALTHY shared ActivityWatch that missed
        # two answers (a load spike, a wake from sleep, a long AW query) it
        # dropped external mode, spawned our own server against the port that
        # healthy server still owns, failed to bind, and the teardown below
        # cleared every watcher. `is_managing` then reads False, so main.py's
        # 60s tick stops calling this method and nothing re-enters the branch
        # -- permanent, on a healthy machine, with both capture-dead flags
        # reading False. Measured against a control that reverted only this
        # predicate:
        #
        #   asked /info   rc=False  _using_external=False  watchers=[]
        #   bare port     rc=True   _using_external=True   watchers=[2]
        #
        # A held socket is not a capture, AND one unanswered ask is not a
        # vanished server. Making this ask needs a debounce (N consecutive
        # non-answers, resetting on any success) so a blip cannot fire it; that
        # is its own change with its own evidence, not a line to flip here.
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
            reachable = self._window_tracker_reachable()
            if reachable:
                # The tick already asked /api/0/info to judge window-tracker staleness,
                # and a server that ANSWERS is by definition not the non-responding
                # holder this flag reports. Without this the flag is a latch on one 2s
                # probe: an ActivityWatch that was merely still booting when the agent
                # started sets it True and nothing re-derives it, because the routine
                # tick never re-enters _start_locked while the port stays held. The
                # asymmetry is what made it worth fixing -- on a real corpse the
                # watchdog forces a restart and the flag refreshes, so it only latched
                # in the case where it was WRONG.
                self._external_server_not_responding = False
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
            # A component that actually launched proves this device's binaries
            # both exist AND execute, so unlatch here as well. The clear in
            # _start_locked is not enough now that the latch can be set from a
            # start attempt: the watchdog restarts components directly, so a
            # device that recovers (Rosetta 2 installed, say) would otherwise
            # keep reporting "capturing NOTHING" forever and train the ops
            # ingest to ignore the signal — see the same argument at the
            # _start_locked clear.
            with self._lifecycle_lock:
                self._exec_failed_components.discard(name)
                # Unlatch only once NOTHING is still unrunnable. A single
                # corrupt/foreign-arch binary leaves that one component blind
                # forever, and clearing on a sibling's success would report the
                # device healthy again — the exact false-healthy this branch
                # exists to end. Disabled components are never started, so they
                # must not hold the latch either.
                if not (self._exec_failed_components - self._disabled_components):
                    self.tracker_download_failed = False
                    self._managed_components_unavailable = False
            logger.info(f"Started {name} (PID {proc.pid})")
            return True

        except OSError as e:
            # A binary that cannot EXECUTE is as unusable as one that never
            # arrived, and unlike a transient failure it will never fix itself:
            # retrying every 60s forever just produces a silent, permanently
            # blind agent. macOS trackers are x86_64-only (upstream v0.13.2
            # publishes no arm64 asset; arm64 first appears in the v0.14.0b*
            # betas), so on Apple Silicon they require Rosetta 2. Without it
            # every start raises EBADARCH.
            #
            # Fabian's device, 2026-07-23: this exact error 21 times, and both
            # 07-22 and 07-23 recorded zero seconds while the heartbeat reported
            # a healthy device, because neither health flag covered "downloaded
            # fine, cannot run".
            #
            # Reusing tracker_download_failed rather than adding a third flag:
            # it already means "this device has no usable trackers" to every
            # consumer, and a new egress field would need its own disclosure
            # review for no gain. The name is narrower than the meaning; the
            # comment on the attribute says so.
            permanent = e.errno in (getattr(errno, "EBADARCH", 86), errno.ENOEXEC)
            logger.error(
                "Failed to start %s: %s%s", name, e,
                " — binary cannot execute on this CPU; on Apple Silicon this "
                "usually means Rosetta 2 is not installed. Not retryable."
                if permanent else "",
            )
            if permanent:
                # #215 — same conflation as check_health: a listener is not a
                # capturing server, and this carve-out decides whether to tell
                # the machine's owner their tracking is dead.
                #
                # Asked BEFORE the lock, deliberately. `_server_responding()`
                # blocks for up to 2s and `_lifecycle_lock` is held across
                # component lifecycle work, so doing it inside would hold the
                # lock on an HTTP timeout. `_using_external` is read-mostly —
                # it flips only when we adopt or abandon an external server —
                # so reading it a moment early is a far better trade than a
                # 2s network wait under the lock.
                external_serving = self._using_external and self._server_responding()
                with self._lifecycle_lock:
                    self._exec_failed_components.add(name)
                    self._managed_components_unavailable = True
                    if external_serving:
                        # Same carve-out the download-failure path makes: a
                        # server we attached to (never one we started — that
                        # leaves _using_external False) is still capturing on
                        # this port, so only OUR watchers are unusable. Latching
                        # the capture-dead flag or toasting here would report a
                        # blackout on a device that is recording fine.
                        logger.warning(
                            "Managed tracker components cannot execute, but an "
                            "external server is running on port %s — attaching "
                            "to it",
                            self.aw_port,
                        )
                    else:
                        self.tracker_download_failed = True
                        # Same outage as a failed download — zero capture — so
                        # it takes the same route out: ops ingest plus a user
                        # toast, throttled to hourly. The heartbeat flag alone
                        # is log-only from the machine owner's point of view,
                        # and only they can install Rosetta 2.
                        self._dispatch_download_failure_report("exec")
            return False

        except Exception as e:
            logger.error(f"Failed to start {name}: {e}")
            return False

    def _external_server_capturing(self) -> bool:
        """True only if an external tracker server is ACTUALLY capturing here.

        One rule for the question every attach point in this file asks. The
        probe below was extracted by #213 "so the two callers cannot drift";
        it did not drift, the CARVE-OUTS around it did. This same judgement
        ended up written at four attach points in ``_start_locked`` plus the
        recovery predicate in ``restart_if_needed``, and the copies disagreed:
        two asked ``/api/0/info``, three trusted the bare TCP connect, and one
        of the two that asked forgot to re-latch. Each disagreement shipped as
        its own defect (#230's missing re-latch, #233's third site).

        A held socket proves a PROCESS, never a CAPTURE, and the difference is
        a device silently recording nothing. Both halves belong together, so
        there is one place to be wrong.
        """
        return self._port_in_use() and self._server_responding()

    def _server_responding(self) -> bool:
        """True only if a tracker server ANSWERS ``/api/0/info`` on our port.

        The single HTTP ask that ``_wait_for_server`` polls, lifted out so the
        two callers cannot drift. This is the difference between *something
        holds the socket* and *a tracker server is up*, and those are not the
        same question — ``_port_in_use()`` answers the first and is routinely
        read as the second. ``force_restart()``'s own docstring names the gap:
        a ``bf-data-service`` that still holds port 5600 and no longer answers
        HTTP is exactly what it exists to reclaim.

        One request, one 2s timeout, no retry loop: callers that need to WAIT
        for a boot own the deadline (``_wait_for_server``); callers deciding
        what is true right now do not.
        """
        url = f"http://localhost:{self.aw_port}/api/0/info"
        try:
            req = urllib.request.urlopen(url, timeout=2)
            req.close()
            return True
        except (urllib.error.URLError, OSError):
            return False

    def _window_tracker_reachable(self) -> bool:
        """Is AW reachable for the purpose of judging window-tracker staleness?

        Named rather than inlined because the question is not "is the port
        held". _is_window_tracker_stale reads this as "AW is reachable", and a
        corpse holding the port answers True to a TCP connect -- so an AW
        OUTAGE was being classified as a blind tracker, force-restarted, and
        latched as _window_tracker_blind, which tells the user to check a
        permission when the real fault is a dead server.

        One HTTP ask, no port pre-check: a server that answers necessarily
        holds the port, so the TCP connect would only add latency.
        """
        return self._server_responding()

    def _wait_for_server(self) -> bool:
        """Wait for tracker server to accept connections."""
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

            if self._server_responding():
                logger.info("Tracker server is ready")
                return True
            time.sleep(0.5)

        logger.error(f"Tracker server not ready after {STARTUP_TIMEOUT}s")
        return False

    def _port_in_use(self) -> bool:
        """Check if something is listening on the tracker port.

        A bare TCP connect. It proves the socket is held; it does NOT prove a
        tracker is capturing there — see ``_server_responding()`` for that
        question, and do not use this one to answer it.
        """
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

    def _window_bucket_id(self) -> str:
        """The host's window bucket id (both the bundled bf-window-tracker and
        the in-process macOS watcher register under this id)."""
        return f"aw-watcher-window_{platform.node()}"

    def _get_recent_window_events(self, lookback_seconds: float) -> Optional[list]:
        """Window events whose range overlaps the last ``lookback_seconds``.

        Returns None on any fetch/parse failure — an unreachable tracker server
        must never be reported as "titles are broken" (that's a different fault,
        already covered by ``window_event_age_seconds``).
        """
        now = datetime.now(timezone.utc)
        start = now - timedelta(seconds=lookback_seconds)
        try:
            url = (
                f"http://localhost:{self.aw_port}/api/0/buckets/"
                f"{urllib.parse.quote(self._window_bucket_id(), safe='')}/events"
                f"?limit={WINDOW_TITLE_EVENT_LIMIT}"
                f"&start={urllib.parse.quote(start.isoformat(), safe='')}"
                f"&end={urllib.parse.quote(now.isoformat(), safe='')}"
            )
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=3) as resp:
                events = json.loads(resp.read())
            return events if isinstance(events, list) else None
        except Exception as e:
            logger.debug("_get_recent_window_events failed: %s", e)
            return None

    def _window_titles_captured_recently(self) -> Optional[bool]:
        """Whether ANY window event in the last 15 minutes carried a title.

        True  => titles are arriving (platform- and cause-independent).
        False => window events exist but every title is empty. THIS is the
                 finding: macOS Accessibility missing, Windows tracker blind,
                 Linux watcher dead — one symptom, three causes, no per-platform
                 detector needed.
        None  => no window events in the period (or the probe could not run).
                 Kept DISTINCT from False on purpose: "not tracking" is a
                 different fault from "tracking without titles", and
                 ``window_event_age_seconds`` already covers the former.
        """
        events = self._get_recent_window_events(WINDOW_TITLE_CAPTURE_LOOKBACK)
        if not events:
            return None

        cutoff = time.time() - WINDOW_TITLE_CAPTURE_LOOKBACK
        saw_event_in_window = False
        for event in events:
            if not isinstance(event, dict):
                continue
            # The server should have honoured start/end, but don't rely on it:
            # an older AW build that ignores the params would otherwise let a
            # title captured yesterday claim capture is healthy today.
            try:
                ts = datetime.fromisoformat(
                    str(event["timestamp"]).replace("Z", "+00:00")
                )
                event_end = ts.timestamp() + (event.get("duration") or 0)
            except Exception:
                continue
            if event_end < cutoff:
                continue
            saw_event_in_window = True
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            title = data.get("title")
            if isinstance(title, str) and title.strip():
                return True

        return False if saw_event_in_window else None

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
                if os.path.isdir(base) and _binaries_present(base):
                    # Log the reason the check actually found, not a fixed
                    # sentence: an architecture reinstall and a signing
                    # reinstall are different faults with different follow-up
                    # for the user, and only one of them costs them a re-grant.
                    reason = self._tracker_reinstall_reason(install_dir, base)
                    if reason:
                        logger.warning("Reinstalling bundled trackers: %s.", reason)
                        # Read the result. The reason is recomputed every cycle,
                        # so a silent failure here is not a one-off — it is the
                        # same warning every 60 seconds for the life of the
                        # install, with capture dead throughout and nothing in
                        # the log saying the repair never landed.
                        if not self._install_to_persistent(base, install_dir):
                            logger.error(
                                "Tracker reinstall FAILED — capture stays blocked and "
                                "this will be retried every cycle. Reason it was "
                                "attempted: %s.",
                                reason,
                            )
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
    def _tracker_reinstall_reason(cls, install_dir: str, bundle_dir: str) -> Optional[str]:
        """Why the persistent trackers must be replaced, or None to keep them.

        Returns a short sentence for the log rather than a bool, because the
        two reasons need different words and #213 was written after a surface
        confidently named the wrong cause for 90 minutes. A caller that logs one
        fixed message for every reinstall repeats that mistake one layer down.

        **Architecture is checked first and is the more severe of the two.** A
        mis-signed tracker runs but goes blind; a wrong-architecture tracker
        does not start at all, so capture is dead rather than degraded.

        This is the upgrade path for #216 and without it the ActivityWatch bump
        would help nobody who already has BetterFlow. `_install_to_persistent`
        copies the bundle's trackers to a stable path once, deliberately, so the
        Accessibility grant survives updates — which also means an existing
        Apple Silicon install keeps the x86_64 trackers it first installed, even
        after updating to a build whose bundle is native arm64. Both copies are
        signed with our team, so the signing check below sees nothing wrong and
        the machine would need Rosetta forever.
        """
        idle = IDLE_TRACKER
        # The WHOLE tree, not just the idle tracker: `_install_to_persistent`
        # copies component by component, so a copy that raises partway leaves a
        # mixed tree. Judging it by one component would call such a tree healthy
        # and leave the other component unable to start — the same first-readable
        # trap `common_arches` exists to close, and the same rule the start gate
        # and the build's re-download check now apply.
        installed_arches = cls._tracker_tree_arches(install_dir)
        bundled_arches = cls._tracker_tree_arches(bundle_dir)
        host = platform.machine()
        # Every clause is required, and each one fails toward KEEPING the
        # installed copy — the conservative direction, since a needless
        # reinstall costs the user a fresh Input Monitoring grant.
        #   - neither is None: unreadable headers mean "could not tell", never
        #     "wrong arch" (see common_arches).
        #   - host not in installed: the copy on disk genuinely cannot run here
        #     (an empty set says no single architecture runs all of it).
        #   - host in bundled: and the replacement genuinely can. Without this a
        #     build whose bundle is equally unrunnable would churn the binary
        #     and change nothing, the same trap the signing check avoids by
        #     refusing to swap ad-hoc for ad-hoc.
        if (
            installed_arches is not None
            and bundled_arches is not None
            and host not in installed_arches
            and host in bundled_arches
        ):
            return (
                f"installed trackers are {cls._describe_arches(installed_arches)} and "
                f"cannot run natively on this {host} Mac, while the bundled copy is "
                f"{cls._describe_arches(bundled_arches)} — reinstalling so capture no "
                "longer depends on Rosetta 2"
            )

        if cls._should_reinstall_for_signing(install_dir, bundle_dir):
            return (
                f"persistent {idle} is ad-hoc/mis-signed while the bundled copy is "
                f"Developer-ID signed (Team {BETTERQA_TEAM_ID}) — reinstalling so its "
                "Input Monitoring grant is stable. Re-grant Input Monitoring for "
                f"{idle} once after this update"
            )

        return None

    @classmethod
    def _should_reinstall_for_signing(cls, install_dir: str, bundle_dir: str) -> bool:
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
                    elif os.path.exists(dst_subdir):
                        # A LEGACY FLAT install puts a FILE at this path
                        # (`trackers/darwin/bf-idle-tracker`) rather than a
                        # directory, so the isdir branch above misses it and
                        # copytree then raises FileExistsError. Every reinstall
                        # fails, and because the reason is recomputed each cycle
                        # the device logs the same warning every 60 seconds and
                        # never repairs — permanently, on exactly the machines
                        # #216 exists to rescue.
                        #
                        # Removing the launcher is also what fixes RESOLUTION:
                        # _resolve_binary_path checks the flat path FIRST, so a
                        # surviving file here would keep winning over the
                        # bundled tree we are about to write. The flat layout's
                        # other siblings (`Python`, `libssl…`) are inert once
                        # the launcher is gone, since nothing resolves to them
                        # on their own.
                        os.remove(dst_subdir)
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
