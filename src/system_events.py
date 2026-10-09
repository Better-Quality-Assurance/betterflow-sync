"""System event listeners for sleep/wake, shutdown, and network changes.

Platform-specific implementations:
- macOS: pyobjc NSWorkspace notifications + SCNetworkReachability
- Windows: ctypes hidden window message pump
- Linux: systemd-logind PrepareForSleep D-Bus signal (via jeepney)
- Fallback: socket-based network poller
"""

import logging
import platform
import socket
import threading
from typing import Callable

logger = logging.getLogger(__name__)

_system = platform.system()

# Store observer refs for cleanup (M5)
_registered_observers: list[tuple] = []  # [(center, observer), ...]
_observers_lock = threading.Lock()
_stop_event = threading.Event()  # Signal run loops to exit on cleanup


def _is_localhost(host: str) -> bool:
    """Check if a host refers to the local machine."""
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def start_system_event_listener(
    on_sleep: Callable,
    on_wake: Callable,
    on_shutdown: Callable,
    on_network_change: Callable,  # fn(is_online: bool)
    on_screen_lock: Callable = None,   # fn() — screen locked
    on_screen_unlock: Callable = None,  # fn() — screen unlocked
    on_display_sleep: Callable = None,  # fn() — macOS display sleep (inactivity or as part of a real sleep)
    reachability_host: str = "",  # Host to check for network reachability
    reachability_port: int = 443,  # Port to check for network reachability
) -> None:
    """Start platform-specific system event listeners.

    All listeners run on daemon threads and die automatically on process exit.

    ``on_display_sleep`` is macOS-only (NSWorkspaceScreensDidSleepNotification
    has no Windows/Linux equivalent wired here) and optional: a caller that
    omits it gets exactly today's behaviour (display-off still reported
    through ``on_sleep``) — see _start_macos_power_listener's docstring.
    """
    host = reachability_host or "app.betterflow.eu"

    # Localhost APIs are always reachable — skip network monitoring and report online
    if _is_localhost(host):
        logger.info(f"API host is localhost ({host}) — assuming always online")
        _safe_call(on_network_change, True)
        # Still start power/screen listeners, just skip network monitoring
        if _system == "Darwin":
            _start_macos_power_listener(on_sleep, on_wake, on_shutdown, on_display_sleep)
            if on_screen_lock or on_screen_unlock:
                _start_macos_screen_lock_listener(on_screen_lock, on_screen_unlock)
        elif _system == "Windows":
            _start_windows_listener(on_sleep, on_wake, on_shutdown, on_screen_lock, on_screen_unlock)
        elif _system == "Linux":
            _start_linux_power_listener(on_sleep, on_wake)
        return

    if _system == "Darwin":
        _start_macos_power_listener(on_sleep, on_wake, on_shutdown, on_display_sleep)
        _start_macos_network_listener(on_network_change, host=host)
        if on_screen_lock or on_screen_unlock:
            _start_macos_screen_lock_listener(on_screen_lock, on_screen_unlock)
    elif _system == "Windows":
        _start_windows_listener(on_sleep, on_wake, on_shutdown, on_screen_lock, on_screen_unlock)
        _start_network_poller(on_network_change, host=host, port=reachability_port)
    elif _system == "Linux":
        _start_linux_power_listener(on_sleep, on_wake)
        _start_network_poller(on_network_change, host=host, port=reachability_port)
    else:
        logger.warning(f"System events not supported on {_system}")


# ---------------------------------------------------------------------------
# macOS: NSWorkspace notifications for power events
# ---------------------------------------------------------------------------

def _start_macos_power_listener(
    on_sleep: Callable,
    on_wake: Callable,
    on_shutdown: Callable,
    on_display_sleep: Callable = None,
) -> None:
    """Listen for macOS sleep/wake/shutdown via NSWorkspace notifications.

    NSWorkspaceWillSleepNotification (real system/lid sleep) and
    NSWorkspaceScreensDidSleepNotification (the display turning off — from
    inactivity, or as a side effect of a real sleep) used to be routed to the
    SAME handler, deduplicated by a single `sleeping` flag: both fire for a
    real sleep, but on a desktop that never sleeps (always plugged in) only
    ScreensDidSleep ever fires, and it was reported as sleep_time — which
    earns internal-tool2's presence bridge exactly like a real suspend, per
    Tudor's 2026-10-08 product decision ("if you are not present, you are not
    working") it must not. ScreensDidSleep now routes to its OWN callback
    (`on_display_sleep`, optional — omitted callers get byte-identical
    behaviour to before this split, since nothing then listens for that
    notification name at all... except we always start it below when the
    caller provides the lock callbacks; see the docstring on
    SystemEventHandler.on_display_sleep for what it does with the gate).

    WillSleep is now called UNCONDITIONALLY, with no dedup against
    ScreensDidSleep — on purpose. The two are independent signals (ordering
    between them is not guaranteed), and on_system_sleep's own span-flush
    logic already handles "a display-off span is open when real sleep
    begins" correctly (closes it at the sleep boundary); suppressing WillSleep
    because ScreensDidSleep won a race would have meant a real sleep could be
    classified as mere display-off and never produce a sleep_time event at
    all. on_system_sleep/on_screen_lock are each idempotent against being
    called more than once without an intervening wake (see their own
    docstrings), so a real sleep firing BOTH notifications no longer needs a
    shared "first one wins" flag to stay correct.

    Wake notifications are deliberately left COMBINED (unlike sleep): both
    DidWake and ScreensDidWake still share the dedup flag below and call
    `on_wake` exactly as before this split. on_system_wake's own logic
    already branches on STATE (was there an open sleep_start / lock span?),
    not on which notification arrived, so no new wake callback is needed —
    see SystemEventHandler.on_system_wake.
    """
    try:
        from AppKit import NSWorkspace
        from Foundation import NSObject
    except ImportError:
        logger.warning("pyobjc not available — sleep/wake detection disabled")
        return

    # Wake-side dedup only (see docstring above) — DidWake and ScreensDidWake
    # still share this. Set by EITHER sleep variant below so a pure
    # display-off-then-wake (no real sleep ever) still fires on_wake exactly
    # once, same as before this split.
    state = {"sleeping": False}

    class _PowerObserver(NSObject):
        def handleSleep_(self, notification):
            state["sleeping"] = True
            logger.info("System sleep detected on %s - pausing", threading.current_thread().name)
            _safe_call(on_sleep)

        def handleScreensDidSleep_(self, notification):
            state["sleeping"] = True
            logger.info("Display sleep detected on %s", threading.current_thread().name)
            _safe_call(on_display_sleep if on_display_sleep is not None else on_sleep)

        def handleWake_(self, notification):
            if state["sleeping"]:
                state["sleeping"] = False
                logger.info("System wake detected on %s - resuming", threading.current_thread().name)
                _safe_call(on_wake)

        def handleShutdown_(self, notification):
            logger.info("System shutdown detected")
            _safe_call(on_shutdown)

    def run_loop():
        observer = _PowerObserver.alloc().init()
        center = NSWorkspace.sharedWorkspace().notificationCenter()

        # Sleep notifications — kept as two distinct selectors (see docstring
        # above): real sleep is never deduped against display-off any more.
        center.addObserver_selector_name_object_(
            observer, "handleSleep:",
            "NSWorkspaceWillSleepNotification", None,
        )
        center.addObserver_selector_name_object_(
            observer, "handleScreensDidSleep:",
            "NSWorkspaceScreensDidSleepNotification", None,
        )

        # Wake notifications
        center.addObserver_selector_name_object_(
            observer, "handleWake:",
            "NSWorkspaceDidWakeNotification", None,
        )
        center.addObserver_selector_name_object_(
            observer, "handleWake:",
            "NSWorkspaceScreensDidWakeNotification", None,
        )

        # Shutdown
        center.addObserver_selector_name_object_(
            observer, "handleShutdown:",
            "NSWorkspaceWillPowerOffNotification", None,
        )

        # Store refs for cleanup (M5)
        with _observers_lock:
            _registered_observers.append((center, observer))

        logger.debug("macOS power event listener started")
        try:
            # NSWorkspace.notificationCenter() delivers notifications via
            # pystray's NSApplication run loop on the main thread, not on
            # this observer thread.  A run loop here would spin at 100% CPU
            # with no sources.  _stop_event.wait() keeps the thread alive
            # (retaining the observer ref) while the main thread dispatches.
            _stop_event.wait()
        finally:
            center.removeObserver_(observer)

    thread = threading.Thread(target=run_loop, name="system-power-listener", daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# macOS: Screen lock/unlock detection via distributed notifications
# ---------------------------------------------------------------------------

def _start_macos_screen_lock_listener(
    on_lock: Callable = None,
    on_unlock: Callable = None,
) -> None:
    """Detect macOS screen lock/unlock via DistributedNotificationCenter."""
    try:
        from Foundation import NSObject, NSDistributedNotificationCenter
    except ImportError:
        logger.warning("pyobjc not available — screen lock detection disabled")
        return

    class _LockObserver(NSObject):
        def handleLock_(self, notification):
            logger.info("Screen locked — treating as AFK")
            if on_lock:
                _safe_call(on_lock)

        def handleUnlock_(self, notification):
            logger.info("Screen unlocked — user returned")
            if on_unlock:
                _safe_call(on_unlock)

    def run_loop():
        from Foundation import NSRunLoop, NSDefaultRunLoopMode, NSDate

        observer = _LockObserver.alloc().init()
        center = NSDistributedNotificationCenter.defaultCenter()

        center.addObserver_selector_name_object_(
            observer, "handleLock:",
            "com.apple.screenIsLocked", None,
        )
        center.addObserver_selector_name_object_(
            observer, "handleUnlock:",
            "com.apple.screenIsUnlocked", None,
        )

        # Store refs for cleanup (M5)
        with _observers_lock:
            _registered_observers.append((center, observer))

        logger.debug("macOS screen lock listener started")
        try:
            # NSDistributedNotificationCenter requires an active run loop
            # on the observer thread to deliver notifications (unlike
            # NSWorkspace.notificationCenter which uses the main thread).
            # Run the loop in 5s intervals so _stop_event can interrupt.
            loop = NSRunLoop.currentRunLoop()
            while not _stop_event.is_set():
                ran = loop.runMode_beforeDate_(
                    NSDefaultRunLoopMode,
                    NSDate.dateWithTimeIntervalSinceNow_(5.0),
                )
                if not ran:
                    # Run loop has no active sources — sleep to prevent
                    # 100% CPU spin (sources can vanish after invalidation).
                    _stop_event.wait(5.0)
        finally:
            center.removeObserver_(observer)

    thread = threading.Thread(target=run_loop, name="screen-lock-listener", daemon=True)
    thread.start()


def cleanup_observers() -> None:
    """Signal observer threads to stop (M5).

    Threads remove their own observers in their finally blocks.
    We just clear the reference list here.
    """
    _stop_event.set()
    with _observers_lock:
        _registered_observers.clear()


# ---------------------------------------------------------------------------
# macOS: SCNetworkReachability for network changes
# ---------------------------------------------------------------------------

def _start_macos_network_listener(
    on_network_change: Callable,
    host: str = "app.betterflow.eu",
) -> None:
    """Monitor network reachability on macOS via SystemConfiguration."""
    try:
        from SystemConfiguration import (
            SCNetworkReachabilityCreateWithName,
            SCNetworkReachabilitySetCallback,
            SCNetworkReachabilityScheduleWithRunLoop,
            SCNetworkReachabilityGetFlags,
            kSCNetworkReachabilityFlagsReachable,
            kSCNetworkReachabilityFlagsConnectionRequired,
        )
        from Foundation import NSRunLoop, NSDefaultRunLoopMode
    except ImportError:
        logger.debug("SystemConfiguration not available — falling back to network poller")
        _start_network_poller(on_network_change, host)
        return

    def _is_reachable(flags):
        reachable = flags & kSCNetworkReachabilityFlagsReachable
        needs_connection = flags & kSCNetworkReachabilityFlagsConnectionRequired
        return bool(reachable and not needs_connection)

    state = {"online": None}  # None = unknown, detect initial state

    def _reachability_callback(target, flags, info):
        online = _is_reachable(flags)
        if state["online"] != online:
            state["online"] = online
            status = "online" if online else "offline"
            logger.info(f"Network change detected — {status}")
            _safe_call(on_network_change, online)

    def run_loop():
        target = SCNetworkReachabilityCreateWithName(None, host.encode("utf-8"))
        if target is None:
            logger.warning("Failed to create reachability target — falling back to poller")
            _start_network_poller(on_network_change, host)
            return

        SCNetworkReachabilitySetCallback(target, _reachability_callback, None)

        loop = NSRunLoop.currentRunLoop()
        SCNetworkReachabilityScheduleWithRunLoop(
            target, loop.getCFRunLoop(), NSDefaultRunLoopMode,
        )

        # Get initial state
        ok, flags = SCNetworkReachabilityGetFlags(target, None)
        if ok:
            state["online"] = _is_reachable(flags)

        logger.debug("macOS network reachability listener started")
        # Use interruptible run loop instead of blocking loop.run()
        from Foundation import NSDate
        while not _stop_event.is_set():
            ran = loop.runMode_beforeDate_(
                NSDefaultRunLoopMode,
                NSDate.dateWithTimeIntervalSinceNow_(5.0),
            )
            if not ran:
                # Run loop has no active sources — sleep to prevent
                # 100% CPU spin (sources can vanish after invalidation).
                _stop_event.wait(5.0)

    thread = threading.Thread(target=run_loop, name="system-network-listener", daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# Windows: hidden message-only window for power/session events
# ---------------------------------------------------------------------------

def _start_windows_listener(
    on_sleep: Callable,
    on_wake: Callable,
    on_shutdown: Callable,
    on_screen_lock: Callable = None,
    on_screen_unlock: Callable = None,
) -> None:
    """Listen for Windows power and session events via a hidden window."""
    try:
        import ctypes
        import ctypes.wintypes as wintypes
    except ImportError:
        logger.warning("ctypes not available — sleep/wake detection disabled")
        return

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # Constants
    WM_POWERBROADCAST = 0x0218
    WM_QUERYENDSESSION = 0x0011
    WM_WTSSESSION_CHANGE = 0x02B1
    WM_DESTROY = 0x0002
    PBT_APMSUSPEND = 0x0004
    PBT_APMRESUMEAUTOMATIC = 0x0012
    WTS_SESSION_LOCK = 0x7
    WTS_SESSION_UNLOCK = 0x8
    NOTIFY_FOR_THIS_SESSION = 0
    HWND_MESSAGE = -3

    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_long, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM,
    )

    def wnd_proc(hwnd, msg, wparam, lparam):
        if msg == WM_POWERBROADCAST:
            if wparam == PBT_APMSUSPEND:
                logger.info("System sleep detected — pausing")
                _safe_call(on_sleep)
            elif wparam == PBT_APMRESUMEAUTOMATIC:
                logger.info("System wake detected — resuming")
                _safe_call(on_wake)
        elif msg == WM_WTSSESSION_CHANGE:
            if wparam == WTS_SESSION_LOCK and on_screen_lock:
                logger.info("Screen locked — treating as AFK")
                _safe_call(on_screen_lock)
            elif wparam == WTS_SESSION_UNLOCK and on_screen_unlock:
                logger.info("Screen unlocked — user returned")
                _safe_call(on_screen_unlock)
        elif msg == WM_QUERYENDSESSION:
            logger.info("System shutdown detected")
            _safe_call(on_shutdown)
            return 1  # Allow shutdown to proceed
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run_message_pump():
        wnd_proc_cb = WNDPROC(wnd_proc)

        class_name = "BetterFlowEvents"

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        wc = WNDCLASSW()
        wc.lpfnWndProc = wnd_proc_cb
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = class_name

        if not user32.RegisterClassW(ctypes.byref(wc)):
            logger.warning("Failed to register window class for system events")
            return

        hwnd = user32.CreateWindowExW(
            0, class_name, "BetterFlow Events", 0,
            0, 0, 0, 0,
            HWND_MESSAGE, None, wc.hInstance, None,
        )
        if not hwnd:
            logger.warning("Failed to create message window for system events")
            return

        # Register for WTS session notifications (lock/unlock)
        try:
            wtsapi32 = ctypes.windll.wtsapi32
            wtsapi32.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION)
        except Exception:
            logger.debug("WTS session notification registration failed")

        logger.debug("Windows system event listener started")

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    thread = threading.Thread(target=run_message_pump, name="system-power-listener", daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# Linux: systemd-logind PrepareForSleep signal (via jeepney)
# ---------------------------------------------------------------------------

def _start_linux_power_listener(
    on_sleep: Callable,
    on_wake: Callable,
) -> None:
    """Listen for suspend/resume via systemd-logind's PrepareForSleep signal.

    logind emits ``PrepareForSleep(True)`` just before the system suspends and
    ``PrepareForSleep(False)`` after it resumes. Shutdown is intentionally not
    handled here — the process receives SIGTERM on logout/shutdown, which is
    wired up separately. Uses jeepney (pure-Python D-Bus, no native deps).
    """
    try:
        from jeepney import MatchRule
        from jeepney.bus_messages import message_bus
        from jeepney.io.blocking import open_dbus_connection
    except ImportError:
        logger.warning("jeepney not available — sleep/wake detection disabled on Linux")
        return

    def run_loop():
        try:
            conn = open_dbus_connection(bus="SYSTEM")
        except Exception as e:
            logger.warning("Could not connect to system D-Bus (%s) — sleep/wake disabled", e)
            return

        rule = MatchRule(
            type="signal",
            sender="org.freedesktop.login1",
            interface="org.freedesktop.login1.Manager",
            member="PrepareForSleep",
            path="/org/freedesktop/login1",
        )
        try:
            conn.send_and_get_reply(message_bus.AddMatch(rule))
        except Exception as e:
            logger.warning("Failed to register logind match rule: %s", e)
            conn.close()
            return

        logger.debug("Linux logind sleep/wake listener started")
        try:
            with conn.filter(rule) as queue:
                while not _stop_event.is_set():
                    try:
                        msg = conn.recv_until_filtered(queue, timeout=1.0)
                    except TimeoutError:
                        continue
                    try:
                        going_to_sleep = bool(msg.body[0])
                    except (IndexError, TypeError):
                        continue
                    if going_to_sleep:
                        logger.info("System sleep detected (logind) — pausing")
                        _safe_call(on_sleep)
                    else:
                        logger.info("System wake detected (logind) — resuming")
                        _safe_call(on_wake)
        except Exception:
            logger.exception("Linux sleep/wake listener stopped unexpectedly")
        finally:
            conn.close()

    thread = threading.Thread(target=run_loop, name="system-power-listener", daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# Fallback: socket-based network poller
# ---------------------------------------------------------------------------

def _start_network_poller(
    on_change: Callable,
    host: str = "app.betterflow.eu",
    port: int = 443,
    interval: int = 5,
) -> None:
    """Poll network connectivity and fire callback on state changes."""
    state = {"online": None}  # None = unknown

    def poll():
        # Immediate first check before entering the wait loop (N10)
        first = True
        while not _stop_event.is_set():
            try:
                socket.create_connection((host, port), timeout=5).close()
                online = True
            except OSError:
                online = False

            if state["online"] != online:
                status = "online" if online else "offline"
                logger.info(f"Network change detected — {status}")
                _safe_call(on_change, online)
            elif first:
                # Fire initial state so the app knows network status at startup
                _safe_call(on_change, online)
            state["online"] = online
            first = False

            _stop_event.wait(interval)

    thread = threading.Thread(target=poll, name="system-network-poller", daemon=True)
    thread.start()
    logger.debug(f"Network poller started (interval: {interval}s)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_call(fn: Callable, *args) -> None:
    """Call a function, catching and logging any exceptions."""
    try:
        fn(*args)
    except Exception:
        logger.exception(f"Error in system event callback {getattr(fn, '__name__', repr(fn))}")
