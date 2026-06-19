"""Smoke tests for AWManager.health_snapshot — the read side of option B.

health_snapshot() is what feeds the heartbeat: the idle-tracker restart count
plus the AFK and window event ages. The "idle but bucket has events" signature
is a high AFK age with a low window age.
"""

from src.aw_manager import AWManager


def _manager_with(monkeypatch, *, afk_age, window_age, restarts):
    mgr = AWManager(aw_port=5600)
    mgr._idle_stale_restart_count = restarts
    monkeypatch.setattr(mgr, "_get_latest_afk_event_age", lambda: afk_age)
    monkeypatch.setattr(mgr, "_get_latest_window_event_age", lambda: window_age)
    return mgr


def test_snapshot_reports_idle_frozen_signature(monkeypatch):
    mgr = _manager_with(monkeypatch, afk_age=300.7, window_age=5.2, restarts=12)
    snap = mgr.health_snapshot()
    assert snap["idle_tracker_stale_restarts"] == 12
    # Coerced to int for the backend's unsigned columns.
    assert snap["afk_event_age_seconds"] == 300
    assert snap["window_event_age_seconds"] == 5


def test_snapshot_tolerates_missing_ages(monkeypatch):
    mgr = _manager_with(monkeypatch, afk_age=None, window_age=None, restarts=0)
    snap = mgr.health_snapshot()
    assert snap["afk_event_age_seconds"] is None
    assert snap["window_event_age_seconds"] is None
    assert snap["idle_tracker_stale_restarts"] == 0


def test_idle_restart_count_excludes_window_tracker_restarts(monkeypatch):
    """idle_tracker_stale_restarts must count ONLY bf-idle-tracker restarts.

    Regression: it was sourced from the shared _stale_restart_count, which the
    window-tracker stale path also bumps — so a flapping window tracker inflated
    the idle figure and the "Active time not advancing (N tracker restarts)" fleet
    alert misattributed window churn to the idle tracker, the exact thing it's
    meant to diagnose.
    """
    mgr = _manager_with(monkeypatch, afk_age=300.0, window_age=5.0, restarts=2)
    mgr._stale_restart_count = 30  # heavy window-tracker churn this session

    snap = mgr.health_snapshot()

    assert snap["idle_tracker_stale_restarts"] == 2, "idle-only, not window+idle"


def test_snapshot_exposes_blind_flag(monkeypatch):
    """The backend/alert needs to tell a transient restart apart from a
    chronically blind tracker (missing Input Monitoring → tell the user to grant
    it, vs. wait it out)."""
    mgr = _manager_with(monkeypatch, afk_age=4000.0, window_age=3.0, restarts=5)
    mgr._idle_tracker_blind = True

    snap = mgr.health_snapshot()

    assert snap["idle_tracker_blind"] is True


def test_snapshot_blind_defaults_false(monkeypatch):
    mgr = _manager_with(monkeypatch, afk_age=5.0, window_age=5.0, restarts=0)
    assert mgr.health_snapshot()["idle_tracker_blind"] is False


def test_snapshot_suppresses_external_afk_age_when_inproc_active(monkeypatch):
    """When the agent owns the AFK stream in-process, the external bf-idle-tracker
    bucket is ignored — its staleness must NOT be reported, or the server fires a
    false "Active time not advancing" alert for an agent that is billing fine."""
    mgr = _manager_with(monkeypatch, afk_age=3000.0, window_age=2.0, restarts=0)
    mgr.set_inproc_afk_active(True)
    snap = mgr.health_snapshot()
    assert snap["inproc_afk"] is True
    assert snap["afk_event_age_seconds"] is None  # not the stale external age
    assert snap["window_event_age_seconds"] == 2


def test_snapshot_reports_external_afk_age_when_inproc_inactive(monkeypatch):
    mgr = _manager_with(monkeypatch, afk_age=3000.0, window_age=2.0, restarts=0)
    snap = mgr.health_snapshot()
    assert snap["inproc_afk"] is False
    assert snap["afk_event_age_seconds"] == 3000
