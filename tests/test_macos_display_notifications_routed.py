"""macOS power listener: display-off/on reach their own callbacks (#273).

Before #273 NSWorkspaceScreensDidSleep/ScreensDidWake shared the system
sleep/wake selectors behind one dedup flag, so a WillSleep after a display-off
was swallowed and the handler could never tell a display-off from a suspend.
With ``on_display_sleep``/``on_display_wake`` supplied, each pair has its own
selector and its own dedup; without them the legacy wiring is unchanged.

pyobjc is replaced with a fake that records registrations, so this runs on
the Linux CI runner too.
"""

from __future__ import annotations

import sys
import threading
import types
from unittest.mock import MagicMock, patch

from src import system_events


class _FakeNSObject:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self


class _FakeCenter:
    def __init__(self):
        self.routes: dict[str, tuple[object, str]] = {}
        self.registered = threading.Event()

    def addObserver_selector_name_object_(self, observer, selector, name, obj):
        self.routes[name] = (observer, selector)
        if name == "NSWorkspaceWillPowerOffNotification":
            self.registered.set()

    def removeObserver_(self, observer):
        pass

    def post(self, name: str) -> None:
        observer, selector = self.routes[name]
        getattr(observer, selector.replace(":", "_"))(None)


def _start(**callbacks) -> _FakeCenter:
    center = _FakeCenter()
    workspace = MagicMock()
    workspace.notificationCenter.return_value = center
    appkit = types.ModuleType("AppKit")
    appkit.NSWorkspace = MagicMock()
    appkit.NSWorkspace.sharedWorkspace.return_value = workspace
    foundation = types.ModuleType("Foundation")
    foundation.NSObject = _FakeNSObject
    with patch.dict(sys.modules, {"AppKit": appkit, "Foundation": foundation}):
        system_events._start_macos_power_listener(**callbacks)
        assert center.registered.wait(5), "listener never registered its observers"
    return center


def _stop():
    system_events._stop_event.set()


def _reset():
    system_events._stop_event.clear()


def test_display_notifications_reach_the_display_callbacks_when_supplied():
    calls = []
    try:
        center = _start(
            on_sleep=lambda: calls.append("sleep"),
            on_wake=lambda: calls.append("wake"),
            on_shutdown=lambda: calls.append("shutdown"),
            on_display_sleep=lambda: calls.append("display_sleep"),
            on_display_wake=lambda: calls.append("display_wake"),
        )
        center.post("NSWorkspaceScreensDidSleepNotification")
        center.post("NSWorkspaceScreensDidSleepNotification")  # deduped
        center.post("NSWorkspaceWillSleepNotification")  # NOT swallowed any more
        center.post("NSWorkspaceDidWakeNotification")
        center.post("NSWorkspaceScreensDidWakeNotification")
        center.post("NSWorkspaceScreensDidWakeNotification")  # deduped
    finally:
        _stop()
    _reset()
    assert calls == ["display_sleep", "sleep", "wake", "display_wake"], calls


def test_without_display_callbacks_the_legacy_combined_dedup_is_unchanged():
    calls = []
    try:
        center = _start(
            on_sleep=lambda: calls.append("sleep"),
            on_wake=lambda: calls.append("wake"),
            on_shutdown=lambda: calls.append("shutdown"),
        )
        center.post("NSWorkspaceScreensDidSleepNotification")
        center.post("NSWorkspaceWillSleepNotification")  # deduped into the first
        center.post("NSWorkspaceDidWakeNotification")
        center.post("NSWorkspaceScreensDidWakeNotification")  # deduped
    finally:
        _stop()
    _reset()
    assert calls == ["sleep", "wake"], calls


def test_start_system_event_listener_forwards_the_display_callbacks():
    seen = {}

    def fake_power(on_sleep, on_wake, on_shutdown, on_display_sleep=None, on_display_wake=None):
        seen["display_sleep"] = on_display_sleep
        seen["display_wake"] = on_display_wake

    ds, dw = MagicMock(), MagicMock()
    with patch.object(system_events, "_system", "Darwin"), \
            patch.object(system_events, "_start_macos_power_listener", fake_power), \
            patch.object(system_events, "_start_macos_screen_lock_listener"), \
            patch.object(system_events, "_start_macos_network_listener"):
        system_events.start_system_event_listener(
            on_sleep=MagicMock(), on_wake=MagicMock(), on_shutdown=MagicMock(),
            on_network_change=MagicMock(), on_display_sleep=ds, on_display_wake=dw,
            reachability_host="localhost",
        )
    assert seen == {"display_sleep": ds, "display_wake": dw}
