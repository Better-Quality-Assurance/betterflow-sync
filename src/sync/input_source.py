"""In-process input source: counts keystrokes, mouse clicks, and scroll events
inside the agent process and uploads them as the input bucket, replacing the
external ``aw-watcher-input`` tracker on machines where its low-level hook is
blocked (Windows UIPI / AV) and produces ZERO input events for hours.

Mirrors ``WindowSource`` / ``AfkSource``: thread-safe state under a lock, an
``available()`` that delegates to the backend (no cached verdict — see
``InputSource.available``), a single-source-of-truth ``bucket_id``, and a
drain-and-build reconstructor (``drain_input_event``). Enabled by default on
Windows, where the external tracker is known to report zero input on affected
devices; macOS stays opt-in under its Input Monitoring grant.

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


def _listener_stopped(thread: Optional[threading.Thread]) -> bool:
    """True when a listener thread was started and is no longer running.

    Ground truth (the thread object) rather than a status flag, so there is no
    third piece of state to keep in sync with reality. A backend that has been
    stopped — shutdown, or _stop_watchers() enforcing the working-hours capture
    policy — is not counting, and available() has to say so or the engine goes on
    suppressing the external input bucket in favour of a sensor that is switched
    off. ``None`` means no start has been attempted yet: that is the
    capability-probe case, not a stopped one."""
    return thread is not None and not thread.is_alive()


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

        # No availability latch here — see available(). The backend owns the
        # verdict, because only the backend knows whether its hook/tap is still
        # installed, and a cached "yes" is exactly how a dead sensor kept
        # reporting healthy.

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
        is no backend. The backend records its own install verdict, which
        ``available()`` then reflects — nothing is cached here."""
        if self._backend is None:
            return False
        try:
            return bool(self._backend.start())
        except Exception as e:
            logger.warning("InputSource backend start failed: %s", e)
            return False

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
        """True when the backend can currently count input. Delegates — the
        backend owns the verdict and nothing is cached here. False forever on a
        platform with no backend.

        There used to be a sticky latch: once a start() succeeded, available()
        answered True from then on, so a cycle with no keystrokes couldn't flap
        the source off. It was a defence against a FLAPPY probe, and neither
        backend has one — each answers "is the capability present AND did the
        last install succeed", which never varies with whether anyone typed. What
        the latch did instead was outlive the thing it vouched for: a hook that
        installed at 09:00 and later died (crashed thread, GetMessageW -1, a tap
        killed with the Input Monitoring grant) kept reporting a working sensor,
        and _should_skip_external_input went on dropping the external input
        bucket while nothing in-process counted anything. Delegating removes that
        whole staleness class rather than shortening it."""
        if self._backend is None:
            return False
        try:
            return bool(self._backend.available())
        except Exception as e:
            # Log it: a permanently failing probe silently disables in-process
            # input capture and hands it back to the external tracker with no
            # diagnostic trail.
            logger.debug("InputSource backend probe failed: %s", e)
            return False

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
        # Set by _run() when the OS refuses the hooks (UIPI / AV / EDR) or ctypes
        # is unusable. Windows ships NO external input tracker, so this backend is
        # the only input source there — a refused hook that still reported
        # "available" would emit nothing forever and look byte-identical to an
        # employee who never touched the keyboard. Only _run() writes it: it
        # clears on a CONFIRMED install, so a block that later lifts still
        # self-heals (start() re-runs every 60s via _apply_capture_policy ->
        # _start_watchers) without start() first blanking the verdict — the sync
        # thread reads available() concurrently, and clearing it up-front reopens
        # a window every tick where a refused hook reads as a working sensor and
        # _should_skip_external_input drops the external bucket.
        self._install_failed = False

    def available(self) -> bool:
        """ctypes + user32 are always present on Windows, so this is a platform
        probe, NOT proof that the hooks installed — report the last install
        attempt's verdict once there has been one, and never vouch for a hook
        thread that has stopped."""
        if self._install_failed or _listener_stopped(self._thread):
            return False
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
        try:
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="win-input-hook",
            )
            self._thread.start()
        except Exception as e:
            # Thread exhaustion / RuntimeError: _run never runs, so nothing else
            # records the verdict and available() would vouch for a hook that was
            # never attempted.
            logger.warning("Windows input hook thread could not start: %s", e)
            self._install_failed = True
            return False
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
        """Thread target. Any escape from the hook thread is recorded as an
        install failure: a thread that died before (or after) the install left
        ``_install_failed`` False, so ``available()`` kept reporting a working
        sensor while nothing was counted — the exact shape this flag exists to
        make visible. ``_started`` is set on every path so ``start()`` can never
        block for its full timeout on a thread that has already gone."""
        try:
            self._run_hooks()
        except Exception as e:
            logger.warning(
                "Windows input hook thread failed: %s", e, exc_info=True
            )
            self._install_failed = True
        finally:
            # Drop the thread id before anything can read it again. Windows
            # RECYCLES thread ids, and stop() posts WM_QUIT to whatever id is
            # stored here — so a stale id from a thread that has exited (the
            # crash path above, or the pump's -1 branch, both of which leave the
            # thread dead while the hooks were installed) can tear down an
            # unrelated live message loop in this process. Safe to clear here:
            # start() only spawns a replacement once is_alive() is False, which
            # Python reports only after this finally has already run.
            self._thread_id = None
            # Same reasoning for _start_ok: a dead thread is not a started one,
            # and start()'s is_alive() fast-path returns this value. Left True it
            # reports a working sensor for whatever window the caller observes
            # the thread mid-teardown.
            self._start_ok = False
            self._started.set()

    def _run_hooks(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes
        except Exception as e:
            logger.warning("ctypes unavailable — Windows input hook disabled: %s", e)
            self._install_failed = True
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
            self._install_failed = True
            self._started.set()
            return

        self._thread_id = kernel32.GetCurrentThreadId()
        self._start_ok = True
        # A confirmed install is the only thing that clears a previous refusal,
        # so a lifted block self-heals without ever reporting "available" on the
        # strength of an attempt that hasn't resolved yet.
        self._install_failed = False
        self._started.set()
        logger.info("Windows in-process input hooks installed")

        # Hold the MSG in a local: the pump writes into it for the whole loop,
        # and only this reference keeps it alive.
        msg = wintypes.MSG()
        try:
            self._pump_messages(user32, ctypes.byref(msg))
        finally:
            # Unhook on EVERY exit, including a raising pump. _run()'s except
            # records the verdict but cannot reach user32 to release the hooks,
            # so a raise here used to leave WH_KEYBOARD_LL/WH_MOUSE_LL installed
            # against a thread that no longer pumps messages — the OS then stalls
            # every keystroke in the session on the low-level hook timeout until
            # it evicts them. Removing them is the one piece of cleanup that must
            # happen while user32 is still in scope.
            self._unhook(user32)
        logger.info("Windows in-process input hooks removed")

    def _pump_messages(self, user32, msg_ref) -> None:
        """Pump the message loop so the OS delivers hook callbacks.

        GetMessageW returns 0 for WM_QUIT (posted by stop() — a deliberate,
        clean exit), a positive value for a real message, and **-1 on error**.
        The original `!= 0` condition accepted -1 as a message and dispatched an
        uninitialised MSG in a tight spin: the hooks are installed, so
        available() reads healthy, while nothing is being pumped and no
        keystroke is ever counted. Treat it as what it is — the sensor is dead,
        record the verdict and leave.

        Split out of _run_hooks purely so this is testable without a real
        user32; the -1 branch cannot be reproduced on demand from a Mac."""
        while True:
            ret = user32.GetMessageW(msg_ref, None, 0, 0)
            if ret == 0:  # WM_QUIT — stop() asked us to leave
                return
            if ret == -1:
                logger.warning(
                    "GetMessageW failed in the input hook pump — in-process "
                    "input counting is dead until the next start attempt"
                )
                self._install_failed = True
                return
            user32.TranslateMessage(msg_ref)
            user32.DispatchMessageW(msg_ref)

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
        # Mirrors _WindowsHookBackend: the tap install is what fails here (a
        # denied/stale Input Monitoring grant), and "does Quartz import" cannot
        # see that. Without this a refused tap probes available() -> True and
        # reads as a working sensor forever. Cleared by a confirmed start(), so
        # re-granting Input Monitoring takes effect on the next 60s converge.
        self._install_failed = False

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
        """True when Quartz is importable (the CGEventTap capability), the last
        tap install did not fail, and the poll thread is still running. Quartz
        alone is a platform probe — it is importable on every Mac, including one
        where the Input Monitoring grant is denied and the tap never starts."""
        if self._install_failed or _listener_stopped(self._thread):
            return False
        try:
            import Quartz  # noqa: F401
            return True
        except Exception:
            return False

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        # Drain, then release, any watcher left over from a previous attempt
        # BEFORE building another. _poll_loop can die on its own and leave a LIVE
        # CGEventTap behind with nothing draining it; overwriting self._watcher
        # there would leak that tap for the life of the process — one more every
        # 60s converge — while its counts went nowhere.
        # The drain is not optional: the tap goes on counting after the poll
        # thread dies, available() is False for that whole span so the checkpoint
        # never advances, and the next drain covers it. Releasing without
        # draining would silently discard real keystrokes on exactly the devices
        # this feature exists to stop under-reporting.
        # Guarded like every other reach into a leftover watcher in this class:
        # an escape here would leave start() before it records a verdict, and
        # available() would go on vouching for the tap we just abandoned.
        try:
            self._drain_to_source()
        except Exception:
            logger.debug("MacOS leftover watcher drain failed", exc_info=True)
        self._release_watcher()
        ok = False
        try:
            # Build inside the try too: _build_watcher only catches the relative
            # ImportError, so a missing sync.macos_input_watcher or a raising
            # MacOSInputWatcher constructor would escape start() with
            # _install_failed still False — available() would then vouch for a
            # tap that was never even constructed, the exact phantom sensor this
            # flag exists to prevent.
            self._watcher = self._build_watcher()
            ok = bool(self._watcher.start())
        except Exception as e:
            logger.warning("MacOS input tap backend start failed: %s", e)
        # Record the verdict on BOTH failure paths (raised, and a watcher that
        # cleanly reported it couldn't create the tap) before returning, or
        # available() goes on vouching for a sensor that isn't running.
        self._install_failed = not ok
        if not ok:
            # Release the watcher we just built. _apply_capture_policy converges
            # every 60s, so a Mac with Input Monitoring denied would otherwise
            # build and abandon a MacOSInputWatcher every minute, indefinitely.
            self._release_watcher()
            return False
        self._stop.clear()
        try:
            self._thread = threading.Thread(
                target=self._poll_loop, daemon=True, name="macos-input-poll",
            )
            self._thread.start()
        except Exception as e:
            # Thread exhaustion / RuntimeError: the tap is already live but
            # nothing would ever drain it, so available() would vouch for a
            # sensor whose counts go nowhere. Mirrors the same guard in
            # _WindowsHookBackend.start().
            logger.warning("MacOS input poll thread could not start: %s", e)
            self._install_failed = True
            self._thread = None
            self._release_watcher()
            return False
        return True

    def _release_watcher(self) -> None:
        """Stop and drop the current watcher, if any. One implementation of the
        teardown so every abandon path releases the CGEventTap the same way; a
        raising stop() must still clear the reference or the next attempt leaks
        it."""
        if self._watcher is None:
            return
        try:
            self._watcher.stop()
        except Exception:
            logger.debug("MacOS input watcher stop failed", exc_info=True)
        self._watcher = None

    def stop(self) -> None:
        self._stop.set()
        # Stop the poll thread first so it can't race the final drain below.
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        # Keep the (now dead) thread reference rather than clearing it: it IS
        # the "am I still counting?" signal available() reads via
        # _listener_stopped. start() only checks is_alive(), so a dead reference
        # does not block a restart.
        # Final drain: capture whatever accrued since the last poll (<= poll
        # interval) before tearing down the tap, so a shutdown loses nothing.
        # In a finally, like the leftover drain in start(): _stop_watchers()
        # calls this to ENFORCE the capture policy ("nothing on this machine
        # records them"), so a raising drain must never skip the release and
        # leave a live CGEventTap counting past the moment capture was
        # suppressed. The raise still propagates — InputSource.stop() logs it.
        try:
            self._drain_to_source()
        finally:
            self._release_watcher()

    def _drain_to_source(self) -> None:
        """Fold whatever the watcher has counted since the last poll onto the
        source. One implementation, so every path that abandons a watcher
        preserves its counts the same way instead of dropping them — the tap
        keeps counting even once nothing is polling it."""
        presses, clicks, scrolls = self._drain_watcher_counts()
        if presses > 0:
            self._source._on_press(presses)
        if clicks > 0:
            self._source._on_click(clicks)
        if scrolls > 0:
            self._source._on_scroll(scrolls)

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
        """Drain the watcher's counters onto the source until stopped.

        Guarded: an escape here killed the poll thread with an unhandled
        traceback and left _install_failed False, so available() went on
        reporting a working sensor while nothing drained — the macOS twin of the
        Windows dead-thread case. Record the verdict so the next 60s converge
        rebuilds instead of trusting a thread that is gone."""
        try:
            while not self._stop.wait(self._POLL_INTERVAL_S):
                self._drain_to_source()
        except Exception as e:
            logger.warning(
                "MacOS input poll thread failed: %s", e, exc_info=True
            )
            self._install_failed = True


class _NullAwClient:
    """No-op AW client for the macOS tap backend: MacOSInputWatcher calls
    create_bucket/post_events, but this backend consumes counters directly and
    must NOT also write the external aw-watcher-input bucket."""

    def create_bucket(self, *args, **kwargs) -> None:
        return None

    def post_events(self, *args, **kwargs) -> None:
        return None
