"""System event listeners for sleep/wake, shutdown, and network changes.

Platform-specific implementations:
- macOS: pyobjc NSWorkspace notifications + SCNetworkReachability
- Windows: ctypes hidden window message pump
- Fallback: socket-based network poller
"""

import logging
import platform
import socket
import threading
from typing import Callable

logger = logging.getLogger(__name__)

_system = platform.system()


def start_system_event_listener(
    on_sleep: Callable,
    on_wake: Callable,
    on_shutdown: Callable,
    on_network_change: Callable,  # fn(is_online: bool)
) -> None:
    """Start platform-specific system event listeners.

    All listeners run on daemon threads and die automatically on process exit.
    """
    if _system == "Darwin":
        _start_macos_power_listener(on_sleep, on_wake, on_shutdown)
        _start_macos_network_listener(on_network_change)
    elif _system == "Windows":
        _start_windows_listener(on_sleep, on_wake, on_shutdown)
        _start_network_poller(on_network_change)
    else:
        logger.warning(f"System events not supported on {_system}")


# ---------------------------------------------------------------------------
# macOS: NSWorkspace notifications for power events
# ---------------------------------------------------------------------------

def _start_macos_power_listener(
    on_sleep: Callable,
    on_wake: Callable,
    on_shutdown: Callable,
) -> None:
    """Listen for macOS sleep/wake/shutdown via NSWorkspace notifications."""
    try:
        from Foundation import NSObject
        from AppKit import NSWorkspace
        from PyObjCTools import AppHelper
    except ImportError:
        logger.warning("pyobjc not available — sleep/wake detection disabled")
        return

    # Deduplication flag — both ScreensDidSleep and WillSleep can fire
    state = {"sleeping": False}

    class _PowerObserver(NSObject):
        def handleSleep_(self, notification):
            if not state["sleeping"]:
                state["sleeping"] = True
                logger.info("System sleep detected — pausing")
                _safe_call(on_sleep)

        def handleWake_(self, notification):
            if state["sleeping"]:
                state["sleeping"] = False
                logger.info("System wake detected — resuming")
                _safe_call(on_wake)

        def handleShutdown_(self, notification):
            logger.info("System shutdown detected")
            _safe_call(on_shutdown)

    def run_loop():
        observer = _PowerObserver.alloc().init()
        center = NSWorkspace.sharedWorkspace().notificationCenter()

        # Sleep notifications
        center.addObserver_selector_name_object_(
            observer, "handleSleep:",
            "NSWorkspaceWillSleepNotification", None,
        )
        center.addObserver_selector_name_object_(
            observer, "handleSleep:",
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

        logger.debug("macOS power event listener started")
        AppHelper.runConsoleEventLoop()

    thread = threading.Thread(target=run_loop, name="system-power-listener", daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# macOS: SCNetworkReachability for network changes
# ---------------------------------------------------------------------------

def _start_macos_network_listener(
    on_network_change: Callable,
    host: str = "api.betterflow.app",
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
        loop.run()

    thread = threading.Thread(target=run_loop, name="system-network-listener", daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# Windows: hidden message-only window for power/session events
# ---------------------------------------------------------------------------

def _start_windows_listener(
    on_sleep: Callable,
    on_wake: Callable,
    on_shutdown: Callable,
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
    WM_DESTROY = 0x0002
    PBT_APMSUSPEND = 0x0004
    PBT_APMRESUMEAUTOMATIC = 0x0012
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
        elif msg == WM_QUERYENDSESSION:
            logger.info("System shutdown detected")
            _safe_call(on_shutdown)
            return 1  # Allow shutdown to proceed
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run_message_pump():
        wnd_proc_cb = WNDPROC(wnd_proc)

        class_name = "BetterFlowSyncEvents"

        wc = wintypes.WNDCLASSW()
        wc.lpfnWndProc = wnd_proc_cb
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = class_name

        if not user32.RegisterClassW(ctypes.byref(wc)):
            logger.warning("Failed to register window class for system events")
            return

        hwnd = user32.CreateWindowExW(
            0, class_name, "BetterFlow Sync Events", 0,
            0, 0, 0, 0,
            HWND_MESSAGE, None, wc.hInstance, None,
        )
        if not hwnd:
            logger.warning("Failed to create message window for system events")
            return

        logger.debug("Windows system event listener started")

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    thread = threading.Thread(target=run_message_pump, name="system-power-listener", daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# Fallback: socket-based network poller
# ---------------------------------------------------------------------------

def _start_network_poller(
    on_change: Callable,
    host: str = "api.betterflow.app",
    interval: int = 15,
) -> None:
    """Poll network connectivity and fire callback on state changes."""
    state = {"online": None}  # None = unknown

    def poll():
        while True:
            try:
                socket.create_connection((host, 443), timeout=5).close()
                online = True
            except OSError:
                online = False

            if state["online"] is not None and state["online"] != online:
                status = "online" if online else "offline"
                logger.info(f"Network change detected — {status}")
                _safe_call(on_change, online)
            state["online"] = online

            threading.Event().wait(interval)

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
        logger.exception(f"Error in system event callback {fn.__name__}")
