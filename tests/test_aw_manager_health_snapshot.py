"""Smoke tests for AWManager.health_snapshot — the read side of option B.

health_snapshot() is what feeds the heartbeat: the idle-tracker restart count
plus the AFK and window event ages. The "idle but bucket has events" signature
is a high AFK age with a low window age.
"""

from src.aw_manager import AWManager


def _manager_with(monkeypatch, *, afk_age, window_age, restarts):
    mgr = AWManager(aw_port=5600)
    mgr._stale_restart_count = restarts
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
