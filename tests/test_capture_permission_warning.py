"""The still-missing-permission warning must be rare, and must blame the right grant.

Two defects this pins, both found by the pre-merge gate on the first attempt at
the fix and both verified against the code:

1. ``_start_watchers`` runs from ``_apply_capture_policy`` on EVERY 60s tick
   (src/main.py:2568; that method's own docstring says "Re-running the desired
   end state every 60s"). An unthrottled warning there is ~1,440 lines a day —
   the same once-a-minute log that ran for 9-21 days on four devices and changed
   nothing (#194). The first version of this fix reintroduced it.

2. The guard fires when EITHER grant is missing, so a message hardcoded to
   "window titles will be empty ... re-grant Accessibility" is false on an
   Input-Monitoring failure: titles are fine, keystroke counts are zero, and the
   pane to open is a different one.

Not gated on Darwin: the PR gate runs on ubuntu, and both permission probes are
monkeypatched here, so nothing touches the real AX API.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import src.main as main


def _bare_app():
    """Construct without __init__ — the pattern tests/test_capture_policy_integration.py uses."""
    app = object.__new__(main.BetterFlowApp)
    app._tcc_warn_lock = threading.Lock()
    app._last_tcc_warn_at = None
    return app


def _perms(monkeypatch, *, accessibility: bool, input_monitoring: bool):
    monkeypatch.setattr(main, "check_accessibility", lambda: accessibility)
    monkeypatch.setattr(main, "check_input_monitoring", lambda: input_monitoring)


def test_it_names_accessibility_when_that_is_the_missing_grant(monkeypatch, caplog):
    app = _bare_app()
    _perms(monkeypatch, accessibility=False, input_monitoring=True)
    with caplog.at_level(logging.WARNING):
        app._maybe_warn_capture_permissions()
    assert "Accessibility" in caplog.text
    assert "window titles" in caplog.text
    assert "Input Monitoring" not in caplog.text


def test_it_does_not_blame_titles_when_input_monitoring_is_what_failed(monkeypatch, caplog):
    """The wrong-blame defect: right about what, wrong about where."""
    app = _bare_app()
    _perms(monkeypatch, accessibility=True, input_monitoring=False)
    with caplog.at_level(logging.WARNING):
        app._maybe_warn_capture_permissions()
    assert "Input Monitoring" in caplog.text
    assert "window titles" not in caplog.text


def test_it_is_throttled_rather_than_firing_on_every_60s_tick(monkeypatch, caplog):
    app = _bare_app()
    _perms(monkeypatch, accessibility=False, input_monitoring=False)
    with caplog.at_level(logging.WARNING):
        for _ in range(10):
            app._maybe_warn_capture_permissions()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected 1 warning per interval, got {len(warnings)}"


def test_it_warns_again_once_the_interval_has_passed(monkeypatch, caplog):
    """Throttled must not mean silenced — a long outage still has to resurface."""
    app = _bare_app()
    _perms(monkeypatch, accessibility=False, input_monitoring=False)
    with caplog.at_level(logging.WARNING):
        app._maybe_warn_capture_permissions()
        app._last_tcc_warn_at = (
            datetime.now(timezone.utc) - main.BetterFlowApp._PERM_REWARN_INTERVAL - timedelta(minutes=1)
        )
        app._maybe_warn_capture_permissions()
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_the_throttle_re_arms_when_the_grants_come_back(monkeypatch, caplog):
    """A stale timestamp must not swallow the NEXT revocation."""
    app = _bare_app()
    _perms(monkeypatch, accessibility=False, input_monitoring=False)
    app._maybe_warn_capture_permissions()
    assert app._last_tcc_warn_at is not None

    _perms(monkeypatch, accessibility=True, input_monitoring=True)
    app._maybe_warn_capture_permissions()
    assert app._last_tcc_warn_at is None, "must re-arm so a later revocation warns"


def test_nothing_is_logged_while_both_grants_are_held(monkeypatch, caplog):
    """The allowance half. A guard tested only on its denial gets inverted later."""
    app = _bare_app()
    _perms(monkeypatch, accessibility=True, input_monitoring=True)
    with caplog.at_level(logging.WARNING):
        app._maybe_warn_capture_permissions()
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_the_rewarn_interval_is_long_enough_to_not_be_a_per_tick_log():
    """State the REQUIREMENT, not the code's current choice.

    test_it_warns_again_once_the_interval_has_passed computes its offset FROM
    _PERM_REWARN_INTERVAL, so both sides move together and no value of the
    constant can redden it — a mutant setting it to 1 second survived the whole
    1756-test suite. At 1s, with _start_watchers running every 60s, that is
    exactly the once-a-minute log this method exists to prevent.

    An hour is not the shipped value (4h); it is the loosest setting that is
    still defensible, so a deliberate retune stays green and a slip does not.
    """
    assert main.BetterFlowApp._PERM_REWARN_INTERVAL >= timedelta(hours=1)


def test_it_names_BOTH_grants_when_both_are_missing(monkeypatch, caplog):
    """The commonest state, and it was unpinned.

    The existing tests exercise one-missing-at-a-time, and the two both-missing
    tests assert only the COUNT of warning records, never the text. So `if` ->
    `elif`, or "; ".join(missing) -> missing[0], both survived: the user fixes
    Accessibility, titles return, and keystroke capture stays dead with nothing
    ever naming it again.
    """
    app = _bare_app()
    _perms(monkeypatch, accessibility=False, input_monitoring=False)
    with caplog.at_level(logging.WARNING):
        app._maybe_warn_capture_permissions()
    assert "Accessibility" in caplog.text
    assert "Input Monitoring" in caplog.text


def _app_for_start_watchers(monkeypatch):
    """A BetterFlowApp real enough to run _start_watchers end to end."""
    app = _bare_app()
    app.window_watcher = app.input_watcher = app.input_source = None
    app.browser_tracker = app.display_tracker = object()   # skip the start_* branches
    app.sync_engine = Mock()
    cfg = SimpleNamespace(
        sync=SimpleNamespace(in_process_input=False),
        privacy=SimpleNamespace(track_browser_urls=False, track_display_info=False),
    )
    app.config = cfg
    monkeypatch.setattr(main.sys, "platform", "darwin")
    return app


def test_start_watchers_actually_calls_the_warning(monkeypatch):
    """The callsite itself was unwitnessed — deleting it left 1756 tests green.

    Patches main.has_capture_permissions, NOT main.check_accessibility: the
    latter does not reach has_capture_permissions, which closes over
    permissions.check_accessibility. Patching the wrong one leaves this probing
    the real AX API and proving nothing.
    """
    app = _app_for_start_watchers(monkeypatch)
    monkeypatch.setattr(main, "has_capture_permissions", lambda: False)
    monkeypatch.setattr(main, "grant_tcc_permissions", lambda: False)
    called = []
    monkeypatch.setattr(
        type(app), "_maybe_warn_capture_permissions",
        lambda self: called.append(True), raising=True,
    )
    app._start_watchers()
    assert called == [True], "_start_watchers must consult the warning path"


def test_start_watchers_still_reports_when_the_grants_are_held(monkeypatch):
    """It must run even on the healthy path — that is what re-arms the throttle.

    Nested under `if not has_capture_permissions()` it could only ever run with
    a grant already missing, so the re-arm was dead and the next revocation was
    swallowed by a stale timestamp.
    """
    app = _app_for_start_watchers(monkeypatch)
    monkeypatch.setattr(main, "has_capture_permissions", lambda: True)
    granted = []
    monkeypatch.setattr(main, "grant_tcc_permissions", lambda: granted.append(True))
    called = []
    monkeypatch.setattr(
        type(app), "_maybe_warn_capture_permissions",
        lambda self: called.append(True), raising=True,
    )
    app._start_watchers()
    assert granted == [], "must not attempt a TCC grant when permissions are held"
    assert called == [True], "must still run, so the throttle re-arms"


def test_start_watchers_gates_on_BOTH_grants_not_just_accessibility(monkeypatch):
    """The discriminating input: Accessibility held, Input Monitoring missing.

    Swapping the unified gate for a bare check_accessibility() survived the
    whole suite, because every other test here makes the two agree. Only a case
    where they DISAGREE can tell them apart — and this is the real state of a
    machine that fixed Accessibility after the off-then-on prompt and still has
    no keystroke capture.
    """
    app = _app_for_start_watchers(monkeypatch)
    monkeypatch.setattr(main, "has_capture_permissions", lambda: False)
    monkeypatch.setattr(main, "check_accessibility", lambda: True)
    monkeypatch.setattr(main, "check_input_monitoring", lambda: False)
    attempted = []
    monkeypatch.setattr(main, "grant_tcc_permissions", lambda: attempted.append(True))
    monkeypatch.setattr(
        type(app), "_maybe_warn_capture_permissions", lambda self: None, raising=True,
    )
    app._start_watchers()
    assert attempted == [True], (
        "the gate must consider Input Monitoring too — a bare check_accessibility() "
        "would skip the grant attempt on this machine"
    )


def test_the_input_watcher_can_actually_resolve_the_shared_predicate():
    """A NameError in macOS-only code that 1762 green tests cannot see.

    macos_input_watcher._permission_retry_loop runs only on macOS with a grant
    missing, so no test drives it. Unifying it onto has_capture_permissions()
    without landing the import left a call to an undefined name — the module
    still imports, the suite still passes, and it raises on exactly the machines
    this change is about. This asserts the symbol is bound in the module, which
    is the cheapest thing that would have caught it.
    """
    import src.sync.macos_input_watcher as w
    assert callable(getattr(w, "has_capture_permissions", None)), (
        "has_capture_permissions is used in _permission_retry_loop but not imported"
    )
