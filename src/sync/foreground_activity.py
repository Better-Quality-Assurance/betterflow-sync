"""Foreground-CPU activity detection.

Credits "engaged but no input" work — e.g. watching an active Claude Code /
build / render / data job in the *focused* window — that the AFK timeout would
otherwise paint idle after ``afk_timeout`` of no keyboard/mouse input.

This mirrors ``CallDetector``'s role for meetings: it recognizes a non-input
engagement context and feeds the same three consumers a meeting does —

  * ``get_last_active_at()`` → folded into the in-process AFK stream
    (``AfkSource``) so the *uploaded* stream stays not-afk and the server bills
    the span (macOS/Windows; the in-process AFK source is inert on Linux).
  * ``is_active()``          → consulted by ``IdleManager`` next to
    ``is_in_call()`` so the local pause doesn't fire mid-session.
  * ``flush()``              → emits an auditable ``dev-session`` span carrying
    the raw CPU signal so the SERVER can validate/override the client credit
    (the durable path on Linux, where AFK injection is unavailable).

Anti-fraud guardrails — input is the signal precisely because it proves a human
is present, so a CPU-only signal must not become an hours farm:

  * **Focus-gated** — only the FRONTMOST process is ever sampled; a background
    CPU hog never counts.
  * **Human-presence anchored** — credit requires the user to have produced
    real keyboard/mouse input at least once, and extends at most
    ``max_credit_seconds`` past the *most recent* real input. An unattended
    busy machine stops being credited once that window lapses, until real input
    returns. (A genuine session — approving a tool call, typing a follow-up —
    keeps resetting the anchor.)
  * **Server-validated** — the raw cpu_percent rides on the uploaded span so the
    backend can re-decide, exactly as ``ActivityMetrics`` does for engagement.
"""

import contextlib
import logging
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol

try:
    from .aw_client import BUCKET_TYPE_DEV_SESSION
except ImportError:  # PyInstaller bundle (src/ is import root)
    from sync.aw_client import BUCKET_TYPE_DEV_SESSION

logger = logging.getLogger(__name__)


@dataclass
class ForegroundSample:
    """A single observation of the frontmost process.

    ``cpu_percent`` is None when it can't be read (no foreground app, a probe
    error, or the first sighting of a pid whose counter still needs seeding) —
    distinct from 0.0, which is a real "focused but idle" reading.
    """

    pid: Optional[int]
    app: str
    cpu_percent: Optional[float]


class ForegroundProbe(Protocol):
    """Samples the frontmost process. Injected so the detector's credit logic is
    testable without psutil or a real desktop."""

    def sample(self) -> ForegroundSample: ...


class ForegroundActivityDetector:
    """Tracks contiguous foreground-CPU activity and turns it into AFK credit
    and an auditable span.

    Thread-safety: ``observe`` runs on the sync thread; ``get_last_active_at`` /
    ``is_active`` may be read from the idle thread and from inside
    ``AfkSource.record_sample``. All shared state is guarded by ``_lock``.
    """

    def __init__(
        self,
        *,
        hostname: str,
        probe: ForegroundProbe,
        cpu_threshold_percent: float = 15.0,
        max_credit_seconds: float = 1200.0,
        min_session_seconds: float = 30.0,
    ) -> None:
        self._hostname = hostname
        self._probe = probe
        self._cpu_threshold = float(cpu_threshold_percent)
        self._max_credit_seconds = float(max_credit_seconds)
        self._min_session_seconds = float(min_session_seconds)
        self._lock = threading.Lock()

        # Most recent instant the frontmost process was CPU-active AND still
        # credit-eligible (within max_credit of real input). Drives AFK credit.
        self._last_active_at: Optional[datetime] = None
        # Open audit-span state (start/last-seen/app/peak cpu), or None when no
        # session is currently open.
        self._session_start: Optional[datetime] = None
        self._session_last_seen: Optional[datetime] = None
        self._session_app: str = ""
        self._session_peak_cpu: float = 0.0

    def observe(
        self, now: datetime, last_real_input_at: Optional[datetime]
    ) -> Optional[dict]:
        """Sample the frontmost process and update credit/span state.

        Returns a finished ``dev-session`` event dict when this sample ends an
        open session (the user disengaged or credit lapsed) and it was long
        enough; otherwise None. Call once per sync cycle.
        """
        try:
            sample = self._probe.sample()
        except Exception as e:
            # A probe failure must never break a sync cycle, and must not be
            # read as "active" — close any open session conservatively.
            logger.debug("foreground probe failed: %s", e)
            return self._close_session()

        active = (
            sample.pid is not None
            and sample.cpu_percent is not None
            and sample.cpu_percent >= self._cpu_threshold
        )
        # Human-presence anchor: only credit while within max_credit of the most
        # recent real input. None (never any input) is never eligible.
        eligible = (
            active
            and last_real_input_at is not None
            and (now - last_real_input_at).total_seconds() <= self._max_credit_seconds
        )

        if not eligible:
            return self._close_session()

        with self._lock:
            self._last_active_at = now
            if self._session_start is None:
                self._session_start = now
                self._session_app = sample.app or "Unknown"
                self._session_peak_cpu = sample.cpu_percent or 0.0
                logger.info(
                    "Foreground activity session started: %s (cpu=%.0f%%)",
                    self._session_app, sample.cpu_percent or 0.0,
                )
            self._session_last_seen = now
            if (sample.cpu_percent or 0.0) > self._session_peak_cpu:
                self._session_peak_cpu = sample.cpu_percent
        return None

    def snapshot(self) -> Optional[dict]:
        """Emit the currently-open session as an event WITHOUT closing it, for
        live server-side validation each cycle. None when no session is open or it
        hasn't yet reached ``min_session_seconds``. The deterministic id means the
        server upserts/extends one record across cycles, and ``is_active`` stays
        continuously True (no per-cycle flap that could trip the idle pause)."""
        with self._lock:
            if self._session_start is None:
                return None
            start = self._session_start
            end = self._session_last_seen or start
            app = self._session_app
            peak = self._session_peak_cpu
        duration = (end - start).total_seconds()
        if duration < self._min_session_seconds:
            return None
        return self._make_event(app, start, duration, peak)

    def flush(self) -> Optional[dict]:
        """Force-close any open session (e.g. at shutdown). Mirrors
        ``CallDetector.flush``; resets ``is_active`` to False."""
        return self._close_session()

    def _close_session(self) -> Optional[dict]:
        with self._lock:
            if self._session_start is None:
                return None
            start = self._session_start
            end = self._session_last_seen or start
            app = self._session_app
            peak_cpu = self._session_peak_cpu
            self._session_start = None
            self._session_last_seen = None
            self._session_app = ""
            self._session_peak_cpu = 0.0

        duration = (end - start).total_seconds()
        if duration < self._min_session_seconds:
            logger.debug(
                "Foreground session too short (%.0fs < %.0fs), discarding",
                duration, self._min_session_seconds,
            )
            return None
        logger.info(
            "Foreground activity session ended: %s duration=%.0fs peak_cpu=%.0f%%",
            app, duration, peak_cpu,
        )
        return self._make_event(app, start, duration, peak_cpu)

    def get_last_active_at(
        self, base_last_input: Optional[datetime], now: datetime
    ) -> Optional[datetime]:
        """Most recent credit-eligible instant, for ``AfkSource`` to fold into
        its last-input reconciliation. Signature matches the activity-source
        contract (base last-input + now) used by ``AfkSource``."""
        with self._lock:
            return self._last_active_at

    def is_active(self) -> bool:
        """True while a foreground-activity session is currently open — consumed
        by ``IdleManager`` to suppress the idle pause, like ``is_in_call``."""
        with self._lock:
            return self._session_start is not None

    def _make_event(
        self, app: str, start: datetime, duration: float, peak_cpu: float
    ) -> dict:
        return {
            # Deterministic id so the server upserts (no double-billing if a
            # session is flushed then continued across cycles), mirroring
            # _make_call_bf_event.
            "id": f"devsession_{app}_{int(start.timestamp())}",
            "timestamp": start.isoformat(),
            "duration": round(duration, 2),
            "bucket_id": f"bf-foreground-activity_{self._hostname}",
            "bucket_type": BUCKET_TYPE_DEV_SESSION,
            "data": {
                "app": app,
                "peak_cpu_percent": round(peak_cpu, 1),
                "cpu_threshold_percent": round(self._cpu_threshold, 1),
                "trigger": "foreground_cpu",
                "synthetic": True,
            },
        }


# -- Platform frontmost-pid probes --------------------------------------------
# Each returns (pid, app_name); (None, "") when the frontmost process can't be
# resolved. CPU sampling is shared (psutil); only "who is in front" is OS-specific.


def _frontmost_pid_macos() -> tuple[Optional[int], str]:
    from AppKit import NSWorkspace

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if not app:
        return None, ""
    return int(app.processIdentifier()), str(app.localizedName() or "")


def _frontmost_pid_windows() -> tuple[Optional[int], str]:
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None, ""
    pid = ctypes.c_ulong(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None, ""
    # Window title as the app label (best-effort; psutil fills the process name).
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return int(pid.value), buf.value or ""


def _frontmost_pid_linux() -> tuple[Optional[int], str]:
    # X11 only (Wayland exposes no portable active-window pid). Uses python-xlib,
    # already a dependency for the XOrg tray fallback. _NET_ACTIVE_WINDOW gives
    # the focused window; _NET_WM_PID gives its owning process.
    from Xlib import X, display

    disp = display.Display()
    try:
        root = disp.screen().root
        net_active = disp.intern_atom("_NET_ACTIVE_WINDOW")
        net_pid = disp.intern_atom("_NET_WM_PID")
        active = root.get_full_property(net_active, X.AnyPropertyType)
        if not active or not active.value:
            return None, ""
        win = disp.create_resource_object("window", active.value[0])
        pid_prop = win.get_full_property(net_pid, X.AnyPropertyType)
        if not pid_prop or not pid_prop.value:
            return None, ""
        return int(pid_prop.value[0]), ""
    finally:
        disp.close()


def _default_pid_getter():
    """Return the platform frontmost-pid getter, or None when unsupported."""
    if sys.platform == "darwin":
        return _frontmost_pid_macos
    if sys.platform == "win32":
        return _frontmost_pid_windows
    if sys.platform.startswith("linux"):
        return _frontmost_pid_linux
    return None


class PsutilForegroundProbe:
    """Foreground probe backed by psutil.

    Reports the CPU of the frontmost process **and its descendants** (when
    ``include_children``, the default). This is what makes terminal-hosted work
    register: an active Claude Code / build / test run is a CHILD of the focused
    terminal, so the terminal's *own* process CPU stays low while the child burns
    a core — summing the tree credits it. GUI apps that do their own work
    (IDE builds, browser/Electron compute, renders) are covered either way.

    ``cpu_percent(None)`` is per-process and measures the average over the
    interval since that object's previous call, so each tracked process is cached
    across cycles (ideal at a 30-60s cadence). A process seen for the first time
    seeds its counter and contributes from the NEXT cycle (its first reading is
    always 0.0); a process that dies between membership and read is skipped."""

    def __init__(self, pid_getter=None, include_children: bool = True):
        self._pid_getter = pid_getter or _default_pid_getter()
        self._include_children = include_children
        self._root_pid: Optional[int] = None
        # pid -> psutil.Process for the current foreground tree (all seeded).
        self._procs: dict = {}

    @property
    def available(self) -> bool:
        return self._pid_getter is not None

    def _tree_pids(self, psutil, root) -> set:
        pids = {root.pid}
        if self._include_children:
            # children() can race a dying process; a missed child this cycle is
            # harmless (picked up next cycle), so suppress rather than spam logs.
            with contextlib.suppress(psutil.Error):
                pids |= {c.pid for c in root.children(recursive=True)}
        return pids

    def sample(self) -> ForegroundSample:
        if self._pid_getter is None:
            return ForegroundSample(None, "", None)
        import psutil

        pid, app = self._pid_getter()
        if pid is None:
            self._root_pid = None
            self._procs = {}
            return ForegroundSample(None, app, None)

        if pid != self._root_pid:
            # Foreground changed: (re)seed the new process tree's counters and
            # report unknown this cycle (every counter's first read is 0.0).
            self._root_pid = pid
            self._procs = {}
            try:
                root = psutil.Process(pid)
                for tpid in self._tree_pids(psutil, root):
                    try:
                        proc = psutil.Process(tpid)
                        proc.cpu_percent(None)  # seed
                        self._procs[tpid] = proc
                    except psutil.Error:
                        continue
            except psutil.Error:
                self._root_pid = None
                self._procs = {}
            return ForegroundSample(pid, app, None)

        # Same foreground: refresh membership and sum CPU over the interval.
        # Single-core basis: 100 == one fully-busy core (can exceed 100 on
        # multi-core), so the threshold reads as "% of one core" across the tree.
        try:
            root = self._procs.get(pid) or psutil.Process(pid)
        except psutil.Error:
            self._root_pid = None
            self._procs = {}
            return ForegroundSample(pid, app, None)

        total = 0.0
        got_reading = False
        next_procs: dict = {}
        for tpid in self._tree_pids(psutil, root):
            proc = self._procs.get(tpid)
            if proc is None:
                # Newly-spawned member: seed now, contributes next cycle.
                try:
                    proc = psutil.Process(tpid)
                    proc.cpu_percent(None)
                    next_procs[tpid] = proc
                except psutil.Error:
                    pass
                continue
            try:
                total += proc.cpu_percent(None)
                got_reading = True
                next_procs[tpid] = proc
            except psutil.Error:
                continue  # died between membership and read — drop it
        self._procs = next_procs
        return ForegroundSample(pid, app, total if got_reading else None)


def create_detector(config, hostname: str) -> Optional[ForegroundActivityDetector]:
    """Build a detector from config, or None when disabled or unsupportable on
    this platform (no frontmost-pid getter, or psutil missing)."""
    if not getattr(config, "enabled", False):
        return None
    probe = PsutilForegroundProbe()
    if not probe.available:
        logger.info("Foreground activity detection unsupported on this platform")
        return None
    try:
        import psutil  # noqa: F401
    except ImportError:
        logger.warning("psutil not available — foreground activity detection disabled")
        return None
    return ForegroundActivityDetector(
        hostname=hostname,
        probe=probe,
        cpu_threshold_percent=config.cpu_threshold_percent,
        max_credit_seconds=config.max_credit_minutes * 60,
        min_session_seconds=config.min_session_seconds,
    )
