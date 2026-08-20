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


# --- The one-shot TCC grant must not report a permission it does not have (#205) ---
#
# grant_tcc_permissions() writes ~/Library/Application Support/BetterFlow/
# .tcc_grant_done in a `finally`, so a CANCELLED prompt and a FAILED sqlite
# write both mark the grant "attempted". Every later launch then takes the
# early return, which answered True — the docstring's "succeeded or was already
# attempted". Callers read True as "we have permission"; four devices sat
# window_titles_blind for 15-21 days while this returned True on every launch.
#
# The marker's purpose is real (do not re-prompt for an admin password on every
# launch) so these tests pin BOTH halves: it must still not re-prompt, and it
# must stop claiming success.

def _marker_path(monkeypatch, tmp_path, *, exists):
    marker = tmp_path / ".tcc_grant_done"
    if exists:
        marker.touch()
    monkeypatch.setattr(permissions, "_IS_MACOS", True)
    monkeypatch.setattr(permissions, "_tcc_grant_marker", lambda: marker)
    return marker


def test_grant_does_not_claim_success_when_permission_is_still_missing(monkeypatch, tmp_path):
    """The defect: marker present, permission absent, answer was True."""
    _marker_path(monkeypatch, tmp_path, exists=True)
    monkeypatch.setattr(permissions, "check_accessibility", lambda: False)
    monkeypatch.setattr(permissions, "check_input_monitoring", lambda: False)

    assert permissions.grant_tcc_permissions() is False


def test_grant_reports_success_when_permission_is_actually_present(monkeypatch, tmp_path):
    """The allowance half. A guard tested only on its denial gets inverted later."""
    _marker_path(monkeypatch, tmp_path, exists=True)
    monkeypatch.setattr(permissions, "check_accessibility", lambda: True)
    monkeypatch.setattr(permissions, "check_input_monitoring", lambda: True)

    assert permissions.grant_tcc_permissions() is True


def test_an_existing_marker_still_suppresses_the_admin_password_prompt(monkeypatch, tmp_path):
    """Control: honesty must not turn into nagging.

    The marker exists precisely so the user is asked for an admin password once,
    not on every launch. If this test ever goes red the fix has traded a silent
    failure for a prompt loop, which is worse.
    """
    _marker_path(monkeypatch, tmp_path, exists=True)
    monkeypatch.setattr(permissions, "check_accessibility", lambda: False)
    monkeypatch.setattr(permissions, "check_input_monitoring", lambda: False)

    called = []
    monkeypatch.setattr(
        permissions.subprocess, "run",
        lambda *a, **k: called.append(a) or MagicMock(returncode=0, stderr=""),
    )

    permissions.grant_tcc_permissions()
    assert called == [], "marker present must not spawn osascript"


def test_a_successful_sqlite_write_is_not_a_granted_permission(monkeypatch, tmp_path):
    """The OTHER return-True site — the class had two addresses.

    returncode 0 means the sqlite write into TCC.db succeeded. It does not mean
    this process now holds the grant: macOS generally does not re-read TCC for a
    running client. Reporting the write's exit status as the permission state is
    the same claim the marker branch was fixed for, one branch down in the same
    function, and it had no test until a surviving mutant said so.
    """
    _marker_path(monkeypatch, tmp_path, exists=False)
    monkeypatch.setattr(permissions, "check_accessibility", lambda: False)
    monkeypatch.setattr(permissions, "check_input_monitoring", lambda: False)
    monkeypatch.setattr(
        permissions.subprocess, "run",
        lambda *a, **k: MagicMock(returncode=0, stderr=""),
    )

    assert permissions.grant_tcc_permissions() is False


def test_a_write_that_really_did_land_reports_success(monkeypatch, tmp_path):
    """Allowance half: when the grant IS live afterwards, say so."""
    marker = _marker_path(monkeypatch, tmp_path, exists=False)
    state = {"granted": False}
    monkeypatch.setattr(permissions, "check_accessibility", lambda: state["granted"])
    monkeypatch.setattr(permissions, "check_input_monitoring", lambda: state["granted"])

    def _write(*a, **k):
        state["granted"] = True          # the grant takes effect
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(permissions.subprocess, "run", _write)

    assert permissions.grant_tcc_permissions() is True
    assert marker.exists(), "the attempt must still be recorded"
