"""Multi-monitor and virtual desktop tracking.

Detects which monitor and virtual desktop (macOS Space / Windows Virtual Desktop)
the user is working on. Runs a daemon thread that caches the current state as a
frozen dataclass, safe to read from the sync thread without locking.

The feature is opt-in (track_display_info = False by default).
"""

import logging
import platform
import threading
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_system = platform.system()


@dataclass(frozen=True)
class DisplayState:
    """Immutable snapshot of the current display context.

    frozen=True makes it safe to read from the sync thread without locking --
    the tracker atomically swaps self._state to a new instance.
    """

    monitor_name: Optional[str] = None      # e.g. "Built-in Retina Display", "DELL U2720Q"
    monitor_index: Optional[int] = None      # 0-based index in screen list
    desktop_id: Optional[str] = None         # Space ID (macOS) or VD GUID (Windows)
    desktop_index: Optional[int] = None      # 1-based discovery-order index


class DisplayTracker:
    """Null tracker -- returns empty state on unsupported platforms."""

    def __init__(self) -> None:
        self._state = DisplayState()

    @property
    def state(self) -> DisplayState:
        return self._state

    def stop(self) -> None:
        pass


# ---------------------------------------------------------------------------
# macOS implementation
# ---------------------------------------------------------------------------

def _start_macos_tracker() -> DisplayTracker:
    """Start a macOS display tracker using NSScreen and CGSGetActiveSpace."""
    from AppKit import NSScreen, NSWorkspace, NSObject
    from Foundation import NSRunLoop, NSDate, NSTimer
    from PyObjCTools import AppHelper

    # Try importing private Quartz APIs for space tracking
    _cgs_available = False
    CGSMainConnectionID = None
    CGSGetActiveSpace = None
    try:
        from Quartz import CGSMainConnectionID, CGSGetActiveSpace  # type: ignore[attr-defined]
        _cgs_available = True
    except (ImportError, AttributeError):
        logger.debug("Quartz CGS APIs unavailable -- desktop tracking will use fallback counter")

    tracker = DisplayTracker()
    # Mutable state for the observer -- only accessed from the run-loop thread
    _space_order: dict[int, int] = {}  # space_id -> 1-based discovery index
    _space_counter = [0]  # mutable counter in list for closure access

    def _update_monitor_state() -> None:
        """Read current main screen and update tracker state."""
        try:
            main = NSScreen.mainScreen()
            if main is None:
                return

            screens = NSScreen.screens()
            monitor_index = None
            monitor_name = None

            main_num = main.deviceDescription().get("NSScreenNumber")
            for i, scr in enumerate(screens):
                scr_num = scr.deviceDescription().get("NSScreenNumber")
                if scr_num == main_num:
                    monitor_index = i
                    break

            # localizedName available on macOS 10.15+
            try:
                monitor_name = main.localizedName()
            except AttributeError:
                pass

            # Build new state preserving desktop fields
            old = tracker._state
            tracker._state = DisplayState(
                monitor_name=monitor_name,
                monitor_index=monitor_index,
                desktop_id=old.desktop_id,
                desktop_index=old.desktop_index,
            )
        except Exception as e:
            logger.debug(f"Monitor state update failed: {e}")

    def _update_desktop_state() -> None:
        """Read current desktop/space and update tracker state."""
        try:
            desktop_id = None
            desktop_index = None

            if _cgs_available and CGSMainConnectionID and CGSGetActiveSpace:
                conn = CGSMainConnectionID()
                space = CGSGetActiveSpace(conn)
                if space:
                    desktop_id = str(space)
                    if space not in _space_order:
                        _space_counter[0] += 1
                        _space_order[space] = _space_counter[0]
                    desktop_index = _space_order[space]
            else:
                # Fallback: no space ID available, but we still track changes
                # via notification count
                pass

            old = tracker._state
            tracker._state = DisplayState(
                monitor_name=old.monitor_name,
                monitor_index=old.monitor_index,
                desktop_id=desktop_id,
                desktop_index=desktop_index,
            )
        except Exception as e:
            logger.debug(f"Desktop state update failed: {e}")

    class _Observer(NSObject):
        """Observes workspace notifications for space/screen changes."""

        def spaceChanged_(self, notification) -> None:
            _update_desktop_state()

        def screenChanged_(self, notification) -> None:
            _update_monitor_state()

    _stop_event = threading.Event()

    def _run() -> None:
        try:
            observer = _Observer.alloc().init()
            nc = NSWorkspace.sharedWorkspace().notificationCenter()

            # Space change notification
            nc.addObserver_selector_name_object_(
                observer,
                "spaceChanged:",
                "NSWorkspaceActiveSpaceDidChangeNotification",
                None,
            )
            # Screen parameters change (resolution, arrangement)
            nc.addObserver_selector_name_object_(
                observer,
                "screenChanged:",
                "NSApplicationDidChangeScreenParametersNotification",
                None,
            )

            # Initial state
            _update_monitor_state()
            _update_desktop_state()

            # Poll mainScreen every 5s via timer on the run loop
            # (no notification fires when the focused window moves to another monitor)
            def _poll_timer_fired(timer) -> None:
                if _stop_event.is_set():
                    AppHelper.stopEventLoop()
                    return
                _update_monitor_state()

            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                5.0,
                observer,
                "pollTimer:",
                None,
                True,
            )

            # Add pollTimer: method dynamically
            import objc
            def pollTimer_(self, timer):
                _poll_timer_fired(timer)

            # Use a simpler approach: run the event loop with periodic checks
            while not _stop_event.is_set():
                NSRunLoop.currentRunLoop().runMode_beforeDate_(
                    "NSDefaultRunLoopMode",
                    NSDate.dateWithTimeIntervalSinceNow_(5.0),
                )
                _update_monitor_state()

            nc.removeObserver_(observer)
        except Exception as e:
            logger.debug(f"macOS display tracker run loop failed: {e}")

    def _stop() -> None:
        _stop_event.set()

    thread = threading.Thread(target=_run, daemon=True, name="display-tracker-macos")
    thread.start()

    # Override stop method
    original_stop = tracker.stop
    tracker.stop = _stop  # type: ignore[assignment]

    return tracker


# ---------------------------------------------------------------------------
# Windows implementation
# ---------------------------------------------------------------------------

def _start_windows_tracker() -> DisplayTracker:
    """Start a Windows display tracker using ctypes Win32 APIs."""
    import ctypes
    import ctypes.wintypes

    tracker = DisplayTracker()
    _stop_event = threading.Event()

    # Desktop order tracking
    _desktop_order: dict[str, int] = {}  # GUID string -> 1-based index
    _desktop_counter = [0]

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]

    # Monitor info struct
    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.wintypes.DWORD),
            ("rcMonitor", ctypes.wintypes.RECT),
            ("rcWork", ctypes.wintypes.RECT),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("szDevice", ctypes.c_wchar * 32),
        ]

    MONITOR_DEFAULTTONEAREST = 2

    # Try COM for virtual desktop
    _vd_available = False
    _vd_manager = None
    GUID = None
    try:
        import comtypes  # noqa: F401
        from ctypes import byref
        from comtypes import GUID as _GUID, CoCreateInstance, CLSCTX_ALL  # type: ignore[attr-defined]

        GUID = _GUID

        class IVirtualDesktopManager(comtypes.IUnknown):
            _iid_ = _GUID("{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}")
            _methods_ = [
                comtypes.COMMETHOD(
                    [],
                    ctypes.HRESULT,
                    "IsWindowOnCurrentVirtualDesktop",
                    (["in"], ctypes.wintypes.HWND, "topLevelWindow"),
                    (["out"], ctypes.POINTER(ctypes.c_int), "onCurrentDesktop"),
                ),
                comtypes.COMMETHOD(
                    [],
                    ctypes.HRESULT,
                    "GetWindowDesktopId",
                    (["in"], ctypes.wintypes.HWND, "topLevelWindow"),
                    (["out"], ctypes.POINTER(_GUID), "desktopId"),
                ),
                comtypes.COMMETHOD(
                    [],
                    ctypes.HRESULT,
                    "MoveWindowToDesktop",
                    (["in"], ctypes.wintypes.HWND, "topLevelWindow"),
                    (["in"], ctypes.POINTER(_GUID), "desktopId"),
                ),
            ]

        CLSID_VirtualDesktopManager = _GUID("{AA509086-5CA9-4C25-8F95-589D3C07B48A}")
        _vd_manager = CoCreateInstance(
            CLSID_VirtualDesktopManager,
            interface=IVirtualDesktopManager,
            clsctx=CLSCTX_ALL,
        )
        _vd_available = True
    except Exception:
        logger.debug("COM IVirtualDesktopManager unavailable -- desktop tracking disabled on Windows")

    def _get_monitor_info():
        """Get current monitor name and index."""
        try:
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None, None

            hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
            if not hmon:
                return None, None

            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if not user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                return None, None

            monitor_name = info.szDevice

            # Enumerate monitors to find index
            monitors = []

            @ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.RECT), ctypes.c_void_p)
            def enum_callback(hmon_enum, hdc, rect, lparam):
                monitors.append(hmon_enum)
                return 1

            user32.EnumDisplayMonitors(None, None, enum_callback, 0)

            monitor_index = None
            for i, m in enumerate(monitors):
                if m == hmon:
                    monitor_index = i
                    break

            return monitor_name, monitor_index
        except Exception as e:
            logger.debug(f"Monitor info failed: {e}")
            return None, None

    def _get_desktop_info():
        """Get current virtual desktop GUID and index."""
        if not _vd_available or _vd_manager is None or GUID is None:
            return None, None

        try:
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None, None

            desktop_guid = GUID()
            _vd_manager.GetWindowDesktopId(hwnd, ctypes.byref(desktop_guid))
            guid_str = str(desktop_guid)

            if guid_str not in _desktop_order:
                _desktop_counter[0] += 1
                _desktop_order[guid_str] = _desktop_counter[0]

            return guid_str, _desktop_order[guid_str]
        except Exception as e:
            logger.debug(f"Desktop info failed: {e}")
            return None, None

    def _poll() -> None:
        """Poll loop for Windows -- runs every 2s."""
        try:
            # Initialize COM for this thread
            if _vd_available:
                try:
                    ctypes.windll.ole32.CoInitialize(None)  # type: ignore[attr-defined]
                except Exception:
                    pass

            while not _stop_event.wait(2):
                monitor_name, monitor_index = _get_monitor_info()
                desktop_id, desktop_index = _get_desktop_info()
                tracker._state = DisplayState(
                    monitor_name=monitor_name,
                    monitor_index=monitor_index,
                    desktop_id=desktop_id,
                    desktop_index=desktop_index,
                )
        except Exception as e:
            logger.debug(f"Windows display tracker poll failed: {e}")

    def _stop() -> None:
        _stop_event.set()

    thread = threading.Thread(target=_poll, daemon=True, name="display-tracker-windows")
    thread.start()

    tracker.stop = _stop  # type: ignore[assignment]
    return tracker


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def start_display_tracker() -> DisplayTracker:
    """Create and start a platform-appropriate display tracker.

    Returns a NullTracker (empty state) if the platform is unsupported
    or if any initialization error occurs.
    """
    try:
        if _system == "Darwin":
            return _start_macos_tracker()
        elif _system == "Windows":
            return _start_windows_tracker()
    except Exception as e:
        logger.debug(f"Failed to start display tracker: {e}")

    return DisplayTracker()
