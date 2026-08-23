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

import pytest

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

# The version every test pretends to be running, so a stamped marker means
# "this build already spent its prompt" without depending on src.__version__.
THIS_VERSION = "9.9.9"
OLDER_VERSION = "9.9.8"


class _RealOsascriptInvoked(BaseException):
    """Deliberately NOT an Exception subclass.

    ``grant_tcc_permissions`` wraps its ``subprocess.run`` in
    ``except Exception``, so an ordinary error raised here would be swallowed
    and reported as a plain ``False`` — the guard would fire and look exactly
    like a normal denial. Deriving from BaseException makes it escape the
    handler and fail the test out loud.
    """


@pytest.fixture(autouse=True)
def _never_spawn_real_osascript(monkeypatch):
    """No test in this module may reach the real admin-password dialog.

    Not hypothetical. ``test_grant_does_not_claim_success_when_permission_is_
    still_missing`` sets up a marker and does NOT patch ``subprocess.run``,
    which was harmless only while an existing marker took the early return.
    The moment a legacy marker began re-arming (#205) that test fell through to
    a real ``osascript ... with administrator privileges``, popped a genuine
    password prompt on the developer's machine, blocked the full 120s
    subprocess timeout — and then PASSED, because a timeout also returns False.

    A green that arrives via the timeout path is the Phantom 4 shape: the right
    answer for the wrong reason. Tests that mean to exercise the write patch
    ``subprocess.run`` themselves and override this fixture; anything else
    reaching it is a bug in the test, and now says so in under a second.
    """
    def _boom(*args, **kwargs):
        raise _RealOsascriptInvoked(
            f"test tried to run a real subprocess: {args!r}"
        )

    monkeypatch.setattr(permissions.subprocess, "run", _boom)


def _marker_path(monkeypatch, tmp_path, *, exists, version=THIS_VERSION):
    """Point permissions at a scratch marker, optionally already stamped.

    ``version`` is what the marker CLAIMS spent the attempt. The default is the
    running version, i.e. the fuse is spent — which is what every caller of this
    helper predating #205 meant by ``exists=True``. Pass ``OLDER_VERSION`` for
    the post-update case, or ``""`` for a legacy marker written by the old bare
    ``touch()``.
    """
    marker = tmp_path / ".tcc_grant_done"
    if exists:
        marker.write_text(version, encoding="utf-8")
    monkeypatch.setattr(permissions, "_IS_MACOS", True)
    monkeypatch.setattr(permissions, "_tcc_grant_marker", lambda: marker)
    # raising=False so this helper still works against the PRE-FIX module,
    # where _agent_version does not exist. Without it every test below dies
    # on an AttributeError during the proof-of-failure run and the handshake
    # proves only that the symbol is new — not that the defect was real.
    monkeypatch.setattr(
        permissions, "_agent_version", lambda: THIS_VERSION, raising=False
    )
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


# --- The one attempt must not be spent forever by a failure (#205) -----------
#
# The three points of #205 were: (1) the fuse is single-use and blows on
# failure, (2) the early return claimed a grant it did not hold, (3) the caller
# discarded the result. (2) and (3) shipped earlier. This block is (1), the
# issue's title: a cancelled prompt burned the agent's only automated recovery
# path permanently, so four devices sat window_titles_blind for 15-21 days
# having been asked exactly once, months earlier.
#
# Both directions are pinned deliberately. A fix that merely stops recording a
# failed attempt turns one prompt into a prompt on EVERY launch, which is worse
# than the bug it replaces — so every "must re-arm" test below has a "must stay
# spent" partner, and the mutation matrix requires both.


def _prompt_recorder(monkeypatch, *, returncode=1, stderr="User canceled."):
    """Capture osascript invocations instead of running them."""
    calls = []

    def _run(*args, **kwargs):
        calls.append(args)
        return MagicMock(returncode=returncode, stderr=stderr)

    monkeypatch.setattr(permissions.subprocess, "run", _run)
    return calls


def test_a_cancelled_prompt_is_retried_after_an_update(monkeypatch, tmp_path):
    """THE defect. A marker from an older build must not silence the new one.

    Pre-fix this returns without prompting: the marker merely had to EXIST.
    That is the whole 15-21 day outage — the agent had one chance, spent it on
    a cancel, and every later launch (including every auto-update, which is
    exactly when the code signature changes and the grant needs re-establishing)
    took the early return.
    """
    _marker_path(monkeypatch, tmp_path, exists=True, version=OLDER_VERSION)
    monkeypatch.setattr(permissions, "check_accessibility", lambda: False)
    monkeypatch.setattr(permissions, "check_input_monitoring", lambda: False)
    calls = _prompt_recorder(monkeypatch)

    permissions.grant_tcc_permissions()

    assert calls, "a new version must get its own attempt at the grant"


def test_a_legacy_unstamped_marker_re_arms_once(monkeypatch, tmp_path):
    """The already-blind fleet's second chance.

    Devices 17, 22, 23 and 53 carry a marker written by the old bare touch():
    zero bytes, no version, indistinguishable from "asked on some build we can
    no longer name". Reading that as spent would leave them blind forever, so
    it re-arms — which is the entire point of shipping this.
    """
    _marker_path(monkeypatch, tmp_path, exists=True, version="")
    monkeypatch.setattr(permissions, "check_accessibility", lambda: False)
    monkeypatch.setattr(permissions, "check_input_monitoring", lambda: False)
    calls = _prompt_recorder(monkeypatch)

    permissions.grant_tcc_permissions()

    assert calls, "an unstamped legacy marker must re-arm exactly once"


def test_the_same_version_never_prompts_twice(monkeypatch, tmp_path):
    """The other direction, and the one that must never regress.

    Re-arming per LAUNCH instead of per VERSION would trade a silent failure
    for an admin-password prompt on every start. If this goes red the fix has
    become worse than the defect.
    """
    _marker_path(monkeypatch, tmp_path, exists=True, version=THIS_VERSION)
    monkeypatch.setattr(permissions, "check_accessibility", lambda: False)
    monkeypatch.setattr(permissions, "check_input_monitoring", lambda: False)
    calls = _prompt_recorder(monkeypatch)

    permissions.grant_tcc_permissions()

    assert calls == [], "the fuse is spent for this version — do not re-ask"


def test_a_cancelled_attempt_still_records_itself(monkeypatch, tmp_path):
    """Cancelling must not re-prompt on the very next launch of the SAME build.

    The pair of this and the test above is what makes "one prompt per version"
    a real contract rather than a hope: the attempt is recorded even when it
    fails, and it is the STAMP, not the absence of a file, that re-arms later.
    """
    marker = _marker_path(monkeypatch, tmp_path, exists=False)
    monkeypatch.setattr(permissions, "check_accessibility", lambda: False)
    monkeypatch.setattr(permissions, "check_input_monitoring", lambda: False)
    _prompt_recorder(monkeypatch)

    assert permissions.grant_tcc_permissions() is False
    assert marker.read_text(encoding="utf-8").strip() == THIS_VERSION

    # Second launch, same build: the recorded attempt must silence the prompt.
    calls = _prompt_recorder(monkeypatch)
    permissions.grant_tcc_permissions()
    assert calls == [], "a recorded cancel must not re-prompt on the same build"


def test_a_successful_grant_does_not_re_prompt_on_the_same_version(monkeypatch, tmp_path):
    """Success direction: a grant that landed must not keep asking either."""
    marker = _marker_path(monkeypatch, tmp_path, exists=False)
    state = {"granted": False}
    monkeypatch.setattr(permissions, "check_accessibility", lambda: state["granted"])
    monkeypatch.setattr(permissions, "check_input_monitoring", lambda: state["granted"])

    def _run(*args, **kwargs):
        state["granted"] = True
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(permissions.subprocess, "run", _run)
    assert permissions.grant_tcc_permissions() is True
    assert marker.read_text(encoding="utf-8").strip() == THIS_VERSION

    calls = _prompt_recorder(monkeypatch)
    assert permissions.grant_tcc_permissions() is True
    assert calls == [], "a granted permission must never re-prompt"


def test_an_already_granted_machine_restamps_on_upgrade(monkeypatch, tmp_path):
    """An upgrade re-arms, but must not PROMPT a machine that is already fine.

    Re-arming is permission to ask, not an obligation. With both grants held
    the services list is empty, so the new version simply re-stamps and returns
    — no dialog. Without this the re-arm would show a password prompt to the
    whole healthy fleet on every single update.
    """
    marker = _marker_path(monkeypatch, tmp_path, exists=True, version=OLDER_VERSION)
    monkeypatch.setattr(permissions, "check_accessibility", lambda: True)
    monkeypatch.setattr(permissions, "check_input_monitoring", lambda: True)
    calls = _prompt_recorder(monkeypatch)

    assert permissions.grant_tcc_permissions() is True
    assert calls == [], "a healthy machine must not be prompted by an upgrade"
    assert marker.read_text(encoding="utf-8").strip() == THIS_VERSION


def test_an_unreadable_marker_reads_as_spent(monkeypatch, tmp_path):
    """Fail toward not-nagging when the marker exists but cannot be read.

    We know an attempt happened — the file is there — and cannot tell which
    build made it. Re-arming on an OSError we would hit again on the next write
    is how a single prompt becomes an infinite one.
    """
    marker = _marker_path(monkeypatch, tmp_path, exists=True, version=OLDER_VERSION)
    monkeypatch.setattr(permissions, "check_accessibility", lambda: False)
    monkeypatch.setattr(permissions, "check_input_monitoring", lambda: False)

    def _explode(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(permissions.Path, "read_text", _explode)
    calls = _prompt_recorder(monkeypatch)

    permissions.grant_tcc_permissions()

    assert calls == [], "an unreadable marker must not start a prompt loop"
    assert marker.exists()


def test_agent_version_is_total(monkeypatch):
    """_agent_version must never raise out of a permission check."""
    monkeypatch.setitem(sys.modules, "_build_info", None)
    assert isinstance(permissions._agent_version(), str)
    assert permissions._agent_version() != ""
