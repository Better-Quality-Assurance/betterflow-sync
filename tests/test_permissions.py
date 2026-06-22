"""Tests for macOS Accessibility permission detection (src/ui/permissions.py).

Regression context: check_accessibility() used an AppleScript / System Events
call as its practical fallback. That call only proves *Automation* permission,
not the process's own *Accessibility* grant — so after a reinstall changed the
code signature and macOS dropped the Accessibility grant, the probe still
returned True. The window watcher (which uses the real in-process AX API) then
silently emitted empty titles, and the startup re-grant/notification never
fired. The fallback now exercises the real AX API instead.

These tests inject a fake ``ApplicationServices`` module so the macOS-only code
paths run deterministically on any CI platform (Linux/Windows included).
"""
import ctypes.util
import sys
import types
from unittest.mock import MagicMock

import src.ui.permissions as permissions

# AXError codes (from <HIServices/AXError.h>)
AX_SUCCESS = 0
AX_ERROR_NO_VALUE = -25212
AX_ERROR_API_DISABLED = -25211  # accessibility disabled for this process


def _install_fake_app_services(monkeypatch, *, trusted, ax_err):
    """Install a fake ApplicationServices module and force the macOS path.

    Also makes the ctypes AXIsProcessTrusted fallback fail, so the outcome is
    determined solely by the fake pyobjc result + the AX probe — never the
    host's real Accessibility state.
    """
    mod = types.ModuleType("ApplicationServices")
    mod.AXIsProcessTrusted = lambda: trusted
    mod.AXUIElementCreateSystemWide = lambda: object()
    mod.AXUIElementCopyAttributeValue = lambda element, attr, none: (ax_err, None)
    monkeypatch.setitem(sys.modules, "ApplicationServices", mod)
    monkeypatch.setattr(ctypes.util, "find_library", lambda name: "/nonexistent/lib")
    monkeypatch.setattr(permissions, "_IS_MACOS", True)


def test_returns_true_when_axisprocesstrusted_true(monkeypatch):
    """Fast path: a positive AXIsProcessTrusted short-circuits to granted."""
    _install_fake_app_services(monkeypatch, trusted=True, ax_err=AX_ERROR_API_DISABLED)
    assert permissions.check_accessibility() is True


def test_rosetta_false_negative_confirmed_by_ax_read(monkeypatch):
    """AXIsProcessTrusted lies (False) but the real AX read is serviced →
    granted. Preserves the Rosetta/unsigned-binary handling the old AppleScript
    fallback was added for."""
    _install_fake_app_services(monkeypatch, trusted=False, ax_err=AX_SUCCESS)
    assert permissions.check_accessibility() is True


def test_real_loss_detected_not_masked(monkeypatch):
    """The bug: AXIsProcessTrusted False AND the AX API disabled must report
    MISSING. The old AppleScript probe returned True here (Automation), masking
    the loss and suppressing the re-grant."""
    _install_fake_app_services(monkeypatch, trusted=False, ax_err=AX_ERROR_API_DISABLED)
    assert permissions.check_accessibility() is False


def test_granted_but_nothing_focused_counts_as_granted(monkeypatch):
    """No focused app (kAXErrorNoValue) still means the AX subsystem serviced
    the request — that's a grant, not a denial."""
    _install_fake_app_services(monkeypatch, trusted=False, ax_err=AX_ERROR_NO_VALUE)
    assert permissions.check_accessibility() is True


def test_never_shells_out_to_applescript(monkeypatch):
    """The Automation-based AppleScript probe is gone: check_accessibility must
    not invoke subprocess, even on the negative path."""
    _install_fake_app_services(monkeypatch, trusted=False, ax_err=AX_ERROR_API_DISABLED)
    run = MagicMock()
    monkeypatch.setattr(permissions.subprocess, "run", run)
    assert permissions.check_accessibility() is False
    run.assert_not_called()


def test_non_macos_returns_true(monkeypatch):
    """Non-macOS platforms are always 'granted' (no AX model)."""
    monkeypatch.setattr(permissions, "_IS_MACOS", False)
    assert permissions.check_accessibility() is True


def test_ax_api_works_maps_error_codes(monkeypatch):
    """_ax_api_works: only kAXErrorAPIDisabled means not-granted."""
    for err, expected in [
        (AX_SUCCESS, True),
        (AX_ERROR_NO_VALUE, True),
        (AX_ERROR_API_DISABLED, False),
    ]:
        mod = types.ModuleType("ApplicationServices")
        mod.AXUIElementCreateSystemWide = lambda: object()
        mod.AXUIElementCopyAttributeValue = lambda e, a, n, _e=err: (_e, None)
        monkeypatch.setitem(sys.modules, "ApplicationServices", mod)
        assert permissions._ax_api_works() is expected


def test_ax_api_works_false_when_binding_unavailable(monkeypatch):
    """If the AX binding can't be imported, fail safe (report not-granted
    rather than falsely granting)."""
    broken = types.ModuleType("ApplicationServices")  # missing AX symbols
    monkeypatch.setitem(sys.modules, "ApplicationServices", broken)
    assert permissions._ax_api_works() is False
