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
