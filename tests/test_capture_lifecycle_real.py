"""Capture lifecycle against REAL objects, not Mocks.

Every bug in review #2 slipped through because the integration tests mocked the
exact objects that were broken:

  app.sync_engine    = Mock()   -> `assert sync_engine.browser_tracker is None`
                                   passes on ANY attribute name. The engine actually
                                   reads `_browser_tracker`, so the real wiring was
                                   dead and the test was green.
  app._start_watchers = Mock()  -> the real start path never ran, so a per-minute
                                   thread leak and a never-cleared _stop_event (window
                                   tracking dead after the first night) were invisible.

So: real SyncEngine, real MacOSWindowWatcher, real round trips.
"""

import sys
import threading
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from src.config import Config, WorkingHoursConfig
from src.sync.sync_engine import SyncEngine

RESTRICTED = {
    "working_hours": {
        "enforced": True, "work_start": "07:30", "work_end": "22:00",
        "working_days": [1, 2, 3, 4, 5], "timezone": "Europe/Bucharest",
    }
}


class TestEnrichmentTrackerWiring:
    """2a: the engine reads self._browser_tracker. Assigning engine.browser_tracker
    from outside created a NEW attribute nothing read, and moving construction out of
    __init__ meant _browser_tracker stayed None for the whole process — every browser
    event shipping with no URL, collapsing to generic 'browsing' = productive. That is
    the browser_domain-empty incident, reintroduced by the privacy fix itself."""

    def _engine(self):
        return SyncEngine(aw=Mock(), bf=Mock(), queue=Mock(), config=Config(),
                          time_tracker=Mock())

    def test_setter_reaches_the_attribute_the_engine_actually_reads(self):
        engine = self._engine()
        browser, display = Mock(), Mock()

        engine.set_enrichment_trackers(browser_tracker=browser, display_tracker=display)

        # The private names are what _transform_event consults.
        assert engine._browser_tracker is browser
        assert engine._display_tracker is display

    def test_detaching_clears_them(self):
        engine = self._engine()
        engine.set_enrichment_trackers(browser_tracker=Mock(), display_tracker=Mock())

        engine.set_enrichment_trackers(browser_tracker=None, display_tracker=None)

        assert engine._browser_tracker is None
        assert engine._display_tracker is None

    def test_a_bare_attribute_assignment_would_NOT_have_worked(self):
        """Pins the trap itself, so nobody 'simplifies' the setter away."""
        engine = self._engine()
        browser = Mock()

        engine.browser_tracker = browser  # the old, broken call

        assert engine._browser_tracker is None  # ...reaches nothing the engine reads


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS window watcher")
class TestWindowWatcherSurvivesTheNight:
    """2b + 2c. The capture policy now calls _start_watchers() on EVERY 60s tick, so
    MacOSWindowWatcher.start() must be idempotent; and stop() must not permanently
    poison the watcher, or window tracking is dead from night two onward with no
    bf-window-tracker fallback on macOS."""

    def _watcher(self):
        from src.sync.macos_window_watcher import MacOSWindowWatcher
        return MacOSWindowWatcher(Mock())

    def test_repeated_start_does_not_leak_a_thread_per_call(self):
        w = self._watcher()
        before = threading.active_count()

        for _ in range(5):  # five 60s ticks inside the window
            w.start()
        try:
            leaked = threading.active_count() - before
            assert leaked <= 1, f"start() spawned {leaked} threads; must be idempotent"
        finally:
            w.stop()

    def test_restart_after_stop_actually_runs(self):
        """stop() sets _stop_event and nothing cleared it, so every later start()
        spawned a thread that fell straight out of `while not _stop_event.wait(...)`.
        The watcher looked started and recorded nothing, forever."""
        w = self._watcher()
        w.start()
        w.stop()
        assert not w.is_running

        w.start()
        try:
            assert w.is_running, "watcher did not come back after a stop/start cycle"
            assert not w._stop_event.is_set(), "_stop_event still set: thread will exit at once"
        finally:
            w.stop()


class TestStatusSpanEndIsClamped:
    """1a. IdleManager polls the OS idle clock on the 60s tick, independently of every
    tracker we stop. Gating the span on its START alone let an idle span that began
    21:45 and ended 23:55 ship — telling the server the employee became active at
    23:55, the exact fact the feature exists not to collect."""

    def _wh(self):
        cfg = Config()
        cfg.update_from_server(RESTRICTED)
        return cfg.working_hours

    def test_window_close_is_the_local_22_00(self):
        wh = self._wh()
        # 21:45 Bucharest == 18:45 UTC on a Monday.
        start = datetime(2026, 7, 13, 18, 45, tzinfo=timezone.utc)

        close = wh.window_close_after(start)

        # 22:00 Bucharest == 19:00 UTC.
        assert close == datetime(2026, 7, 13, 19, 0, tzinfo=timezone.utc)

    def test_an_idle_span_across_the_boundary_is_cut_at_the_close(self):
        wh = self._wh()
        start = datetime(2026, 7, 13, 18, 45, tzinfo=timezone.utc)   # 21:45 local
        end = datetime(2026, 7, 13, 20, 55, tzinfo=timezone.utc)     # 23:55 local

        close = wh.window_close_after(start)

        assert wh.allows(start) is True      # the span legitimately begins in-window
        assert wh.allows(end) is False       # ...but its end must never be published
        assert end > close
        clamped = min(end, close)
        assert clamped == datetime(2026, 7, 13, 19, 0, tzinfo=timezone.utc)

    def test_unrestricted_users_are_never_clamped(self):
        wh = WorkingHoursConfig(enforced=False, known=True)
        assert wh.window_close_after(datetime.now(timezone.utc)) is None


class TestOvernightShiftDayOfWeek:
    """1b. The shift is named for the day it STARTS on. Testing the instant's own local
    day got it wrong in both directions — and the wrong direction that matters is
    Monday 02:00, the tail of a Sunday night the user does not work, being ALLOWED."""

    def _night_shift(self):
        cfg = Config()
        cfg.update_from_server({
            "working_hours": {
                "enforced": True, "work_start": "22:00", "work_end": "06:00",
                "working_days": [1, 2, 3, 4, 5], "timezone": "Europe/Bucharest",
            }
        })
        return cfg.working_hours

    def _at(self, y, m, d, hh, mm):
        from zoneinfo import ZoneInfo
        return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("Europe/Bucharest"))

    def test_monday_0200_is_the_tail_of_an_unworked_sunday_night(self):
        # OVER-COLLECTION: Sunday is not a working day, so its night shift never
        # started, so 02:00 Monday must not be recorded.
        assert self._night_shift().allows(self._at(2026, 7, 13, 2, 0)) is False

    def test_saturday_0200_is_the_tail_of_fridays_shift(self):
        # Friday IS a working day; its shift legitimately runs into Saturday morning.
        assert self._night_shift().allows(self._at(2026, 7, 11, 2, 0)) is True

    def test_friday_evening_and_tuesday_small_hours_are_in_shift(self):
        wh = self._night_shift()
        assert wh.allows(self._at(2026, 7, 10, 23, 0)) is True   # Fri 23:00
        assert wh.allows(self._at(2026, 7, 14, 5, 59)) is True   # Tue 05:59 (Mon shift)

    def test_the_daylight_gap_is_never_recorded(self):
        assert self._night_shift().allows(self._at(2026, 7, 13, 12, 0)) is False

    def test_saturday_evening_never_starts_a_shift(self):
        assert self._night_shift().allows(self._at(2026, 7, 11, 23, 0)) is False
