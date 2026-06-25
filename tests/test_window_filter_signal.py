"""Tests for the per-reason window-filter signal (_assess_window_filter).

Origin: Cristian Dragota / sync:67a77a43-787, 2026-06-25 — the server saw window/
app data go stale while AFK/input uploaded normally. Two ways that happens:
the watcher went quiet (v1.5.83 logs that watcher-side), OR the watcher produced
window events but the privacy filter dropped them all (excluded app frontmost, or
sub-minimum flickers). This signal names the latter so the next occurrence
classifies itself instead of being an indistinguishable silence.

These drive _assess_window_filter directly with crafted SyncStats — no AW/network
needed. Each fails pre-fix (the method + counters don't exist).
"""

import logging
from unittest.mock import Mock

from src.config import Config
from src.sync.sync_engine import SyncEngine, SyncStats


def _engine():
    return SyncEngine(aw=Mock(), bf=Mock(), queue=Mock(), config=Config())


def _filtered_stats(*, excluded=None, short=0):
    s = SyncStats()
    s.window_seen = 1
    s.window_sent = 0
    if excluded:
        s.window_drop_excluded_apps.update(excluded)
    s.window_drop_short = short
    return s


def test_warns_after_streak_naming_the_excluded_app(caplog):
    eng = _engine()
    with caplog.at_level(logging.WARNING):
        for _ in range(eng._WINDOW_FILTER_WARN_CYCLES - 1):
            eng._assess_window_filter(_filtered_stats(excluded={"1Password"}))
        assert not any("dropping them all" in r.getMessage() for r in caplog.records)

        eng._assess_window_filter(_filtered_stats(excluded={"1Password"}))
        warns = [r for r in caplog.records if "dropping them all" in r.getMessage()]
        assert len(warns) == 1
        assert "1Password" in warns[0].getMessage()
        assert "Billing is unaffected" in warns[0].getMessage()

        # One-shot: further filtered cycles don't re-warn.
        eng._assess_window_filter(_filtered_stats(excluded={"1Password"}))
        warns = [r for r in caplog.records if "dropping them all" in r.getMessage()]
        assert len(warns) == 1


def test_short_flicker_cause_is_named(caplog):
    eng = _engine()
    with caplog.at_level(logging.WARNING):
        for _ in range(eng._WINDOW_FILTER_WARN_CYCLES):
            eng._assess_window_filter(_filtered_stats(short=4))
        warns = [r for r in caplog.records if "dropping them all" in r.getMessage()]
        assert len(warns) == 1
        assert "minimum" in warns[0].getMessage()  # "...under the 5s minimum (flicker filter)"


def test_healthy_cycle_resets_streak_and_logs_recovery(caplog):
    eng = _engine()
    with caplog.at_level(logging.INFO):
        for _ in range(eng._WINDOW_FILTER_WARN_CYCLES):
            eng._assess_window_filter(_filtered_stats(excluded={"Keychain Access"}))
        assert eng._window_filter_warned is True

        healthy = SyncStats()
        healthy.window_seen = 2
        healthy.window_sent = 2
        eng._assess_window_filter(healthy)

        assert eng._window_filter_streak == 0
        assert eng._window_filter_warned is False
        assert any("reaching the server again" in r.getMessage() for r in caplog.records)


def test_no_window_events_does_not_trip_filter_signal(caplog):
    # window_seen == 0 is the watcher-quiet case (v1.5.83's signal), NOT a filter
    # drop — it must not accumulate the filter streak.
    eng = _engine()
    with caplog.at_level(logging.WARNING):
        empty = SyncStats()  # window_seen == 0, window_sent == 0
        for _ in range(eng._WINDOW_FILTER_WARN_CYCLES + 2):
            eng._assess_window_filter(empty)
        assert eng._window_filter_streak == 0
        assert not any("dropping them all" in r.getMessage() for r in caplog.records)
