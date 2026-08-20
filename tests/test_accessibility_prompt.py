"""The user gets ASKED when Accessibility is denied, not just logged at.

Five macOS devices have been recording empty window titles for 9-15 days while
the watcher logged "Process does NOT have Accessibility permission" once a
minute. The log is on the user's own disk; the grant can only be given by the
person sitting at the machine; nobody ever connected the two (#194).

These tests are deliberately NOT gated on Darwin. `tests/test_macos_window_watcher.py`
carries `importorskip("AppKit")` + `skipif(system() != "Darwin")`, which is right
for tests that drive the real AX API — but the PR gate runs on ubuntu-latest, so
a guard living only there executes in no merge-gating job at all. The notify
latch is pure Python and the watcher class imports without pyobjc (AppKit and
ApplicationServices are imported inside methods, not at module level), so this
runs on every runner. The one test that needs the AX symbol injects a fake
module rather than requiring the real one.
"""

import sys
import types
from unittest.mock import MagicMock, patch

from tests.remedy_wording import conveys_the_off_then_on_remedy
from src.sync.macos_window_watcher import MacOSWindowWatcher

NOTIFY = "src.notifications.send_notification"


def _watcher():
    return MacOSWindowWatcher(MagicMock(), poll_interval=0.1)


def _fake_ax(trusted):
    """A stand-in ApplicationServices.

    Carries ``AXUIElementCreateApplication`` as well as ``AXIsProcessTrusted``
    because ``start()`` imports BOTH in one statement — omit either and the
    whole import raises, sending start() down its "pyobjc not installed" branch
    and returning False before the permission check is ever reached. That
    failure looks like the feature not working; it is the fixture not reaching
    the subject.
    """
    mod = types.ModuleType("ApplicationServices")
    mod.AXIsProcessTrusted = lambda: trusted
    mod.AXUIElementCreateApplication = lambda pid: None
    return mod


def test_the_user_is_notified_when_accessibility_is_denied():
    w = _watcher()
    with patch(NOTIFY) as notify:
        w._notify_accessibility_required_once()

    notify.assert_called_once()
    title, body = notify.call_args[0][0], notify.call_args[0][1]
    # The prompt has to name the remedy: "can't read window titles" alone gives
    # the user nothing to do, which is the state this fix exists to end.
    assert "Accessibility" in body
    assert "System Settings" in body
    assert title


def test_the_prompt_says_tracking_still_works():
    """Do not imply the day is not being recorded — it is.

    Accessibility gates the window TITLE only; app names, durations and
    categorisation all survive without it. A prompt that reads like "tracking is
    broken" would send the user to support over an attribution-detail fault.
    """
    w = _watcher()
    with patch(NOTIFY) as notify:
        w._notify_accessibility_required_once()

    body = notify.call_args[0][1].lower()
    assert "still" in body and ("tracked" in body or "tracking" in body)


def test_it_fires_once_per_launch_not_once_per_check():
    """start() re-runs every 60s under the capture policy.

    That is exactly why the log line was useless — 60 warnings an hour that
    nobody reads. A notification at that rate trains the user to dismiss it,
    which would reproduce the same uselessness with more annoyance.
    """
    w = _watcher()
    with patch(NOTIFY) as notify:
        for _ in range(50):
            w._notify_accessibility_required_once()

    assert notify.call_count == 1


def test_a_second_revocation_notifies_again():
    """The latch re-arms on a grant.

    A latch that only ever sets would make the SECOND revocation exactly as
    silent as the first one was — the bug, reintroduced one level down.
    """
    w = _watcher()
    w._last_accessibility = False

    with patch(NOTIFY) as notify:
        w._notify_accessibility_required_once()  # first denial
        assert notify.call_count == 1

        # The user grants it: the transition handler must re-arm.
        w._last_accessibility_check_ts = 0.0
        with patch.dict(sys.modules, {"ApplicationServices": _fake_ax(True)}):
            w._maybe_log_accessibility_transition()
        assert w._accessibility_notified is False, "grant did not re-arm the latch"

        # ...and takes it away again.
        w._notify_accessibility_required_once()
        assert notify.call_count == 2


def test_a_revocation_while_running_prompts():
    """Not only the start-up path. A grant can be withdrawn mid-session."""
    w = _watcher()
    w._last_accessibility = True
    w._last_accessibility_check_ts = 0.0

    with patch(NOTIFY) as notify, patch.dict(
        sys.modules, {"ApplicationServices": _fake_ax(False)}
    ):
        w._maybe_log_accessibility_transition()

    notify.assert_called_once()


def _fake_appkit():
    mod = types.ModuleType("AppKit")
    mod.NSWorkspace = object
    return mod


def test_start_prompts_when_the_grant_is_already_missing():
    """The CALLSITE guard, and it is the one that matters.

    Every test above drives `_notify_accessibility_required_once` directly, so
    they all pass whether or not anything calls it — the helper-only shape that
    lets a fix ship wired to nothing. This one goes through `start()`, which is
    the path Emilian's device actually takes on every launch and every 60s
    capture-policy re-assert.

    Both pyobjc modules are injected rather than required, so this runs on the
    ubuntu gate as well as on a Mac.
    """
    w = _watcher()
    fakes = {"AppKit": _fake_appkit(), "ApplicationServices": _fake_ax(False)}

    with patch(NOTIFY) as notify, patch.dict(sys.modules, fakes), patch.object(w, "_run"):
        started = w.start()

    assert started is True, "the watcher must still start — titles degrade, capture does not"
    notify.assert_called_once()
    w.stop()


def test_start_does_not_nag_when_the_grant_is_present():
    """The permissive direction, witnessed.

    Tests cluster on the firing path, so the line that exists purely to stay
    QUIET routinely has no guard — and a prompt on every healthy launch would be
    worse than the silence this fix replaces.
    """
    w = _watcher()
    fakes = {"AppKit": _fake_appkit(), "ApplicationServices": _fake_ax(True)}

    with patch(NOTIFY) as notify, patch.dict(sys.modules, fakes), patch.object(w, "_run"):
        w.start()

    notify.assert_not_called()
    w.stop()


def test_a_broken_notifier_never_stops_the_watcher():
    """Best-effort. Losing the prompt must not cost the capture."""
    w = _watcher()
    with patch(NOTIFY, side_effect=OSError("no notification centre")):
        w._notify_accessibility_required_once()  # must not raise

    # And the latch still moved, so a failing notifier cannot become a retry
    # loop that spawns an osascript every 60s forever.
    assert w._accessibility_notified is True


def test_the_prompt_warns_the_toggle_may_already_look_enabled():
    """"Grant Accessibility" is a no-op instruction for the commonest cause.

    src/ui/permissions.py's own header records why: "After a fresh build the
    app's code signature changes. macOS may show the toggle as ON in System
    Settings while AXIsProcessTrusted() returns False... Toggling the permission
    off and on again in System Settings re-registers it."

    The agent auto-updates, so that is the path most of the fleet takes into this
    state. A user told to "grant Accessibility" opens the pane, sees BetterFlow
    already switched on, concludes the message is stale and closes it. Four
    devices sat blind for 15-21 days, two of them for five days on v1.5.124 —
    the release that added this very prompt.

    Asserting the information is PRESENT rather than banning phrasings: the ways
    to word this are unbounded, so a blocklist would be unfinishable.
    """
    w = _watcher()
    with patch(NOTIFY) as notify:
        w._notify_accessibility_required_once()

    assert _conveys_the_remedy(notify.call_args[0][1])


# The v1.5.124 body, verbatim. This is the negative control: without it nobody
# has ever seen the assertion above distinguish a good body from the one that
# left four devices blind for 15-21 days.
V1_5_124_BODY = (
    "Grant Accessibility to BetterFlow in System Settings > Privacy & "
    "Security > Accessibility. App names and time are still being tracked."
)


def _conveys_the_remedy(body: str) -> bool:
    """Delegates to the one definition (tests/remedy_wording.py).

    This used to be defined here and hand-copied into the tray tests in a naive
    substring form that was vacuous. Two copies, one of them broken, is the
    defect these very tests exist to catch, so there is now one.
    """
    return conveys_the_off_then_on_remedy(body)


def test_the_remedy_check_rejects_the_body_that_left_four_devices_blind():
    """Control on the guard above — an unwitnessed predicate proves nothing."""
    assert _conveys_the_remedy(V1_5_124_BODY) is False


def test_the_remedy_check_accepts_reasonable_rewordings():
    """It must not false-fail a better message, or it gets weakened not fixed."""
    assert _conveys_the_remedy(
        "BetterFlow may already be listed - disable it and re-enable it."
    ) is True


def test_the_remedy_check_rejects_a_half_remedy():
    """"Switch it off" with no second step is actively harmful, not merely vague."""
    assert _conveys_the_remedy(
        "BetterFlow may already look enabled there - if so, switch it off."
    ) is False


def test_the_remedy_check_is_not_satisfied_by_the_word_offline():
    assert _conveys_the_remedy(
        "You may already be offline. App names and time are still being tracked."
    ) is False
