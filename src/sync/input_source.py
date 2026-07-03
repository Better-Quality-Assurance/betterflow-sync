"""In-process input source: counts keystrokes, mouse clicks, and scroll events
inside the agent process and uploads them as the input bucket, replacing the
external ``aw-watcher-input`` tracker on machines where its low-level hook is
blocked (Windows UIPI / AV) and produces ZERO input events for hours.

Mirrors ``WindowSource`` / ``AfkSource``: thread-safe state under a lock, a
sticky ``available()`` latch, a single-source-of-truth ``bucket_id``, and a
drain-and-build reconstructor (``drain_input_event``). Ships dormant
(``SyncSettings.in_process_input`` defaults False) — opt-in per the AFK/window
convergence playbook.

Counting backend: unlike WindowSource (which polls a frontmost probe each
cycle), input COUNTS must be captured continuously — a keystroke between two
60s cycles cannot be recovered by a later poll. So a backend runs a listener
thread that increments this source's counters as events arrive:

  * Windows: low-level ctypes hooks (``WH_KEYBOARD_LL`` / ``WH_MOUSE_LL``)
    installed IN the agent process. This is the whole point of the feature —
    the external tracker's hook is the one being blocked, so we run our own in
    the (already-permitted) main process.
  * macOS: reuses the existing ``MacOSInputWatcher``'s Quartz ``CGEventTap``
    counters (it already counts in-process under the app's Input Monitoring
    grant) — we do NOT reimplement an OS hook there.
  * Linux / no backend: unavailable (a gap, never invented counts) — exactly
    like WindowSource on a platform with no frontmost probe.

Privacy: only COUNTS are captured, never key values / content — same as the
external watcher and ``MacOSInputWatcher``.
"""

import logging
import platform
import threading
from datetime import datetime
from typing import Callable, Optional

try:
    from .aw_client import BUCKET_TYPE_INPUT
except ImportError:  # PyInstaller bundle (src/ is import root)
    from sync.aw_client import BUCKET_TYPE_INPUT

logger = logging.getLogger(__name__)

# Sentinel: distinguishes "caller didn't pass a backend" (build the platform
# default) from "caller passed None" (explicitly no backend — e.g. a test
# feeding counts directly, or a platform with no in-process hook).
_UNSET = object()


class InputSource:
    """Counts input events via a listener backend and drains them to input
    events for upload.

    Divergence from ``WindowSource``: there is no per-cycle sampling. A backend
    thread increments the counters live (``_on_press`` / ``_on_click`` /
    ``_on_scroll``); the sync cycle only DRAINS the accumulated counts into an
    event for [checkpoint, now] and resets them. A period with no backend
    available produces no event (gap), exactly like a blind WindowSource probe —
    we never invent counts.
    """

    def __init__(
        self,
        hostname: str,
        *,
        backend=_UNSET,
        frontmost_app_getter=_UNSET,
    ) -> None:
        self._hostname = hostname
        # Thread-safe counters, incremented by the backend listener thread and
        # snapshot-and-reset by drain_input_event on the sync thread. Guarded by
        # the same lock so a drain reads a coherent (presses, clicks, scrolls)
        # triple.
        self._presses = 0
        self._clicks = 0
        self._scrolls = 0
        self._lock = threading.Lock()

        # Sticky platform-capability latch: once the backend has started (the
        # in-process hook installed), the platform HAS the capability, so a
        # transient nothing-happened cycle must not flap in-process input off.
        # Mirrors WindowSource / AfkSource audit finding A.
        self._available_latched = False

        # Not passed -> platform default backend. Passed as None -> no backend
        # (unsupported platform, or a test that injects counts directly): the
        # source stays permanently unavailable unless something injects counts.
        self._backend = (
            _default_input_backend(self) if backend is _UNSET else backend
        )
        # Not passed -> platform default frontmost-app getter (tag the batch so
        # the server attributes counts to a real app row rather than "Unknown").
        # Passed as None -> no app tagging (headless / unsupported).
        self._frontmost_app_getter: Optional[Callable[[], Optional[str]]] = (
            _default_frontmost_app_getter()
            if frontmost_app_getter is _UNSET else frontmost_app_getter
        )

    # -- backend callbacks (called from the listener thread) ------------------

    def _on_press(self, n: int = 1) -> None:
        with self._lock:
            self._presses += n

    def _on_click(self, n: int = 1) -> None:
        with self._lock:
            self._clicks += n

    def _on_scroll(self, n: int = 1) -> None:
        with self._lock:
            self._scrolls += n

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> bool:
        """Start the backend listener thread. No-op (returns False) when there
        is no backend. Latches ``available()`` on once the backend reports it
        started, so a quiet first cycle doesn't hand input back to the external
        tracker."""
        if self._backend is None:
            return False
        try:
            ok = bool(self._backend.start())
        except Exception as e:
            logger.warning("InputSource backend start failed: %s", e)
            return False
        if ok:
            self._available_latched = True
        return ok

    def stop(self) -> None:
        """Stop the backend listener thread (no-op when there is no backend)."""
        if self._backend is None:
            return
        try:
            self._backend.stop()
        except Exception as e:
            logger.debug("InputSource backend stop failed: %s", e)

    @property
    def bucket_id(self) -> str:
        """The synthetic input bucket id this source uploads under. Single
        source of truth so the upload (_event) and the checkpoint-commit gate
        agree."""
        return f"bf-input-inproc_{self._hostname}"

    @property
    def counts(self) -> tuple[int, int, int]:
        """Current (presses, clicks, scrolls) accumulated since the last drain.
        For diagnostics/tests; the live values, read under the lock."""
        with self._lock:
            return (self._presses, self._clicks, self._scrolls)

    def available(self) -> bool:
        """True when an in-process input backend is (or has ever been) usable on
        this platform. Sticky: once the backend has started it stays True, so a
        cycle with no keystrokes cannot flap in-process input off. False forever
        on a platform with no backend."""
        if self._available_latched:
            return True
        if self._backend is None:
            return False
        try:
            ok = bool(self._backend.available())
        except Exception:
            ok = False
        if ok:
            self._available_latched = True
        return ok

    def _frontmost_app(self) -> Optional[str]:
        """Best-effort frontmost app name to attribute this batch to. None when
        unavailable — never blocks or fails the drain."""
        getter = self._frontmost_app_getter
        if getter is None:
            return None
        try:
            app = getter()
        except Exception as e:
            logger.debug("InputSource frontmost-app lookup failed: %s", e)
            return None
        app = str(app or "").strip()
        return app or None

    def drain_input_event(
        self, range_start: datetime, range_end: datetime
    ) -> Optional[dict]:
        """Snapshot-and-reset the counters and build ONE input event spanning
        [range_start, range_end] carrying the accumulated counts.

        Returns None (and leaves the counters untouched) when the source isn't
        available, when the range is empty/inverted, or when nothing was counted
        — a zero-count period is a gap, never an emitted zero event (mirrors
        ``MacOSInputWatcher``'s skip-zero and WindowSource's blind handling; it
        also avoids ~1,440 empty events/day).

        The counters are reset ONLY after we've captured the snapshot to build
        the event, so the accumulation for the next range starts clean. On a
        send failure the caller holds the checkpoint and the counts are already
        folded into this (idempotent, stable-id) event, which the offline queue
        redelivers — the same durability model as the AFK/window streams."""
        if not self.available():
            return None
        if range_end <= range_start:
            return None
        with self._lock:
            presses = self._presses
            clicks = self._clicks
            scrolls = self._scrolls
            # Reset now that we've snapshotted — the next range accrues fresh.
            self._presses = 0
            self._clicks = 0
            self._scrolls = 0
        if presses == 0 and clicks == 0 and scrolls == 0:
            return None
        return self._event(range_start, range_end, presses, clicks, scrolls)

    def _event(
        self, start: datetime, end: datetime,
        presses: int, clicks: int, scrolls: int,
    ) -> dict:
        duration = (end - start).total_seconds()
        data = {"presses": presses, "clicks": clicks, "scrolls": scrolls}
        app = self._frontmost_app()
        if app:
            data["app"] = app
        return {
            # Millisecond precision to match the AFK/window synthetic streams:
            # two drains can legitimately start within the same whole second on a
            # fast manual sync + cycle overlap; a second-truncated id would
            # collide and the server upsert would drop one, under-counting.
            "id": f"input-inproc_{self._hostname}_{int(start.timestamp() * 1000)}",
            "timestamp": start.isoformat(),
            "duration": round(duration, 2),
            "bucket_id": self.bucket_id,
            "bucket_type": BUCKET_TYPE_INPUT,
            "data": data,
        }


def _default_frontmost_app_getter() -> Optional[Callable[[], Optional[str]]]:
    """Return a getter yielding the frontmost app's name, or None when the
    platform has no cheap frontmost probe. Reuses the same probes the other
    in-process sources use so counts attribute to the same app rows."""
    if platform.system() == "Darwin":
        def getter() -> Optional[str]:
            try:
                from AppKit import NSWorkspace
                app = NSWorkspace.sharedWorkspace().frontmostApplication()
                if app is None:
                    return None
                name = app.localizedName()
                return str(name) if name else None
            except Exception as e:
                logger.debug("frontmostApplication lookup failed: %s", e)
                return None
        return getter
    # Windows/Linux: resolve the foreground window's process name via the same
    # pid probe the foreground-activity detector uses (psutil for the app name).
    try:
        from .foreground_activity import _default_pid_getter
    except ImportError:
        from sync.foreground_activity import _default_pid_getter
    pid_getter = _default_pid_getter()
    if pid_getter is None:
        return None

    def getter() -> Optional[str]:
        pid, _label = pid_getter()
        if pid is None:
            return None
        try:
            import psutil
            return psutil.Process(pid).name()
        except Exception as e:
            logger.debug("InputSource process-name lookup failed: %s", e)
            return None

    return getter


def _default_input_backend(source: "InputSource"):
    """Build the platform's in-process input-counting backend, or None when the
    platform has no in-process hook.

    Windows -> ctypes low-level hooks (the machines the feature exists for).
    macOS   -> reuse the existing MacOSInputWatcher's CGEventTap counters.
    Linux   -> None (no in-process backend; the external tracker keeps its job).
    """
    system = platform.system()
    if system == "Windows":
        return _WindowsHookBackend(source)
    if system == "Darwin":
        return _MacOSTapBackend(source)
    return None


class _WindowsHookBackend:
    """Low-level Windows input hooks (``WH_KEYBOARD_LL`` / ``WH_MOUSE_LL``)
    installed in the agent process, incrementing the InputSource counters.

    Runs its own thread with a Windows message loop (``GetMessage``) — the OS
    only delivers low-level hook callbacks to a thread that pumps messages. The
    callback reads ONLY the event type (WM_KEYDOWN / WM_*BUTTONDOWN / WM_*WHEEL),
    never key values, then chains to the next hook.
    """

    # Windows message / hook constants (defined here to avoid importing win32 at
    # module load — this module is imported on every platform).
    _WH_KEYBOARD_LL = 13
    _WH_MOUSE_LL = 14
    _WM_KEYDOWN = 0x0100
    _WM_SYSKEYDOWN = 0x0104
    _WM_LBUTTONDOWN = 0x0201
    _WM_RBUTTONDOWN = 0x0204
    _WM_MBUTTONDOWN = 0x0207
    _WM_XBUTTONDOWN = 0x020B
    _WM_MOUSEWHEEL = 0x020A
    _WM_MOUSEHWHEEL = 0x020E

    def __init__(self, source: "InputSource") -> None:
        self._source = source
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._kbd_hook = None
        self._mouse_hook = None
        # Keep the ctypes callback objects alive for the hook's lifetime —
        # letting them be GC'd while Windows holds the pointer crashes the app.
        self._kbd_cb = None
        self._mouse_cb = None
        self._started = threading.Event()
        self._start_ok = False

    def available(self) -> bool:
        """ctypes + user32 are always present on Windows; the hook install is
        what can fail, and start() latches that. Report capability here."""
        try:
            import ctypes  # noqa: F401
            return platform.system() == "Windows"
        except Exception:
            return False

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return self._start_ok
        self._started.clear()
        self._start_ok = False
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="win-input-hook",
        )
        self._thread.start()
        # Wait briefly for the hooks to install so available() reflects reality.
        self._started.wait(timeout=2.0)
        return self._start_ok

    def stop(self) -> None:
        tid = self._thread_id
        if tid is None:
            return
        try:
            import ctypes
            # WM_QUIT == 0x0012 — breaks the message loop so the thread exits.
            ctypes.windll.user32.PostThreadMessageW(tid, 0x0012, 0, 0)
        except Exception as e:
            logger.debug("Windows input hook stop failed: %s", e)

    def _run(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes
        except Exception as e:
            logger.warning("ctypes unavailable — Windows input hook disabled: %s", e)
            self._started.set()
            return

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # LowLevelKeyboardProc / LowLevelMouseProc signature.
        HOOKPROC = ctypes.CFUNCTYPE(
            ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
        )

        src = self._source

        def _kbd_proc(nCode, wParam, lParam):
            if nCode == 0:  # HC_ACTION — a real event to process
                if wParam in (self._WM_KEYDOWN, self._WM_SYSKEYDOWN):
                    src._on_press()
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        def _mouse_proc(nCode, wParam, lParam):
            if nCode == 0:
                if wParam in (
                    self._WM_LBUTTONDOWN,
                    self._WM_RBUTTONDOWN,
                    self._WM_MBUTTONDOWN,
                    self._WM_XBUTTONDOWN,
                ):
                    src._on_click()
                elif wParam in (self._WM_MOUSEWHEEL, self._WM_MOUSEHWHEEL):
                    src._on_scroll()
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._kbd_cb = HOOKPROC(_kbd_proc)
        self._mouse_cb = HOOKPROC(_mouse_proc)

        # A NULL hMod with dwThreadId 0 requires the callback to live in this
        # module (it does). Pass the module handle for robustness.
        h_mod = kernel32.GetModuleHandleW(None)
        self._kbd_hook = user32.SetWindowsHookExW(
            self._WH_KEYBOARD_LL, self._kbd_cb, h_mod, 0
        )
        self._mouse_hook = user32.SetWindowsHookExW(
            self._WH_MOUSE_LL, self._mouse_cb, h_mod, 0
        )
        if not self._kbd_hook or not self._mouse_hook:
            logger.warning(
                "SetWindowsHookEx failed (kbd=%s mouse=%s) — in-process input "
                "counting disabled", self._kbd_hook, self._mouse_hook,
            )
            self._unhook(user32)
            self._started.set()
            return

        self._thread_id = kernel32.GetCurrentThreadId()
        self._start_ok = True
        self._started.set()
        logger.info("Windows in-process input hooks installed")

        # Pump the message loop so the OS delivers hook callbacks. GetMessage
        # blocks until WM_QUIT (posted by stop()), then returns 0.
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        self._unhook(user32)
        logger.info("Windows in-process input hooks removed")

    def _unhook(self, user32) -> None:
        for hook in (self._kbd_hook, self._mouse_hook):
            if hook:
                try:
                    user32.UnhookWindowsHookEx(hook)
                except Exception:
                    logger.debug("UnhookWindowsHookEx failed", exc_info=True)
        self._kbd_hook = None
        self._mouse_hook = None


class _MacOSTapBackend:
    """macOS backend that reuses the existing ``MacOSInputWatcher``'s Quartz
    ``CGEventTap`` counters instead of reimplementing an OS hook.

    ``MacOSInputWatcher`` already counts presses/clicks/scrolls in-process under
    the app's Input Monitoring grant. We wrap ONE watcher instance, poll the
    delta of its cumulative counters on a light thread, and feed the delta into
    the InputSource — so the drain/event/bucket machinery is identical across
    platforms and the CGEventTap logic lives in exactly one place."""

    _POLL_INTERVAL_S = 1.0

    def __init__(self, source: "InputSource") -> None:
        self._source = source
        self._watcher = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _build_watcher(self):
        try:
            from .macos_input_watcher import MacOSInputWatcher
        except ImportError:
            from sync.macos_input_watcher import MacOSInputWatcher
        # count_only=True: the watcher runs ONLY its CGEventTap counter — no
        # emitter thread, no AW bucket. This backend drains the raw counters
        # itself (below); if the watcher's own emitter also ran it would subtract
        # counts out from under our poll and undercount. The no-op AW client is
        # then never touched, but kept as a defensive stub.
        return MacOSInputWatcher(_NullAwClient(), count_only=True)

    def available(self) -> bool:
        """True when Quartz is importable (the CGEventTap capability). The
        permission itself is handled by MacOSInputWatcher.start()."""
        try:
            import Quartz  # noqa: F401
            return True
        except Exception:
            return False

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        self._watcher = self._build_watcher()
        ok = False
        try:
            ok = bool(self._watcher.start())
        except Exception as e:
            logger.warning("MacOS input tap backend start failed: %s", e)
            return False
        if not ok:
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="macos-input-poll",
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        # Stop the poll thread first so it can't race the final drain below.
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        # Final drain: capture whatever accrued since the last poll (<= poll
        # interval) before tearing down the tap, so a shutdown loses nothing.
        presses, clicks, scrolls = self._drain_watcher_counts()
        if presses > 0:
            self._source._on_press(presses)
        if clicks > 0:
            self._source._on_click(clicks)
        if scrolls > 0:
            self._source._on_scroll(scrolls)
        if self._watcher is not None:
            try:
                self._watcher.stop()
            except Exception:
                logger.debug("MacOS input watcher stop failed", exc_info=True)

    def _drain_watcher_counts(self) -> tuple[int, int, int]:
        """Atomically read AND zero the watcher's counters under its lock, so
        this backend is the SOLE drainer. With count_only=True the watcher has no
        emitter subtracting concurrently, so nothing is lost between polls."""
        w = self._watcher
        if w is None:
            return (0, 0, 0)
        with w._lock:
            counts = (w._presses, w._clicks, w._scrolls)
            w._presses = 0
            w._clicks = 0
            w._scrolls = 0
            return counts

    def _poll_loop(self) -> None:
        while not self._stop.wait(self._POLL_INTERVAL_S):
            presses, clicks, scrolls = self._drain_watcher_counts()
            if presses > 0:
                self._source._on_press(presses)
            if clicks > 0:
                self._source._on_click(clicks)
            if scrolls > 0:
                self._source._on_scroll(scrolls)


class _NullAwClient:
    """No-op AW client for the macOS tap backend: MacOSInputWatcher calls
    create_bucket/post_events, but this backend consumes counters directly and
    must NOT also write the external aw-watcher-input bucket."""

    def create_bucket(self, *args, **kwargs) -> None:
        return None

    def post_events(self, *args, **kwargs) -> None:
        return None
