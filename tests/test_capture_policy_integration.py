"""Integration tests for the ACTUAL enforcement point.

The first cut of the working-hours fix shipped 34 green unit tests and was still
completely inert: `_apply_capture_policy` was defined on BetterFlowApp but called
from SyncCoordinator._tick_60s, so every tick raised AttributeError while building
its sub-task tuple — before the loop, hence outside _run_tick_task's try/except.
Capture never stopped at 22:00, and idle detection / hours / permissions / reminders
died with it. Nothing in the suite touched _tick_60s, so nothing caught it.

These tests exercise the wiring, not the pieces.
"""

import threading
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import src.main as main
from src.config import Config

RESTRICTED = {
    "working_hours": {
        "enforced": True,
        "work_start": "07:30",
        "work_end": "22:00",
        "working_days": [1, 2, 3, 4, 5],
        "timezone": "Europe/Bucharest",
    }
}

IN_HOURS = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)    # Mon 12:00 Bucharest
OUT_OF_HOURS = datetime(2026, 7, 13, 20, 55, tzinfo=timezone.utc)  # Mon 23:55 — the incident


def _coordinator() -> main.SyncCoordinator:
    """A SyncCoordinator with only what _tick_60s touches."""
    c = object.__new__(main.SyncCoordinator)
    c._tick_failure_counts = {}
    c._tick_failure_reported = set()
    c.error_reporter = None
    c.reminder_manager = None
    c.apply_capture_policy = None
    c.tray = Mock()
    for name in (
        "_reconcile_inproc_afk_flag", "_check_idle_status", "_heartbeat_floor",
        "_refresh_hours_today", "_check_permissions", "_check_auth_warn",
        "_check_idle_tracker_health", "_check_private_auto_end",
    ):
        setattr(c, name, Mock())
    return c


class TestTickIsNotDead:
    def test_tick_runs_without_a_capture_policy_wired(self):
        """The tick must survive apply_capture_policy being unset — it is declared
        None on SyncCoordinator and injected later by BetterFlowApp. Reaching for an
        attribute that does not exist here kills every sub-task, not just this one,
        because the tuple is built before the loop."""
        c = _coordinator()

        c._tick_60s()  # must not raise

        c._check_idle_status.assert_called_once()
        c._refresh_hours_today.assert_called_once()

    def test_tick_invokes_the_capture_policy_when_wired(self):
        c = _coordinator()
        policy = Mock()
        c.apply_capture_policy = policy

        c._tick_60s()

        policy.assert_called_once()
        # ...and the rest of the tick still runs.
        c._check_idle_status.assert_called_once()

    def test_a_failing_capture_policy_does_not_kill_the_rest_of_the_tick(self):
        c = _coordinator()
        c.apply_capture_policy = Mock(side_effect=RuntimeError("boom"))

        c._tick_60s()  # swallowed by _run_tick_task

        c._check_idle_status.assert_called_once()
        c._refresh_hours_today.assert_called_once()


class TestBetterFlowAppWiresTheCoordinator:
    def test_apply_capture_policy_is_injected_onto_the_coordinator(self):
        """Guards the exact defect: the policy living on a class the tick can't see."""
        assert hasattr(main.BetterFlowApp, "_apply_capture_policy")
        assert not hasattr(main.SyncCoordinator, "_apply_capture_policy")
        # The tick must reference the injected handle, never the app's method.
        import inspect
        src = inspect.getsource(main.SyncCoordinator._tick_60s)
        assert "self.apply_capture_policy" in src
        assert "self._apply_capture_policy" not in src


class TestCapturePolicyStopsEverything:
    def _app(self, cfg) -> main.BetterFlowApp:
        app = object.__new__(main.BetterFlowApp)
        app.config = cfg
        app._capture_lock = threading.RLock()
        app._capture_allowed = None
        app.aw_manager = Mock()
        app.sync_engine = Mock()
        app.window_watcher = Mock()
        app.input_watcher = Mock()
        app.input_source = Mock()
        app.browser_tracker = Mock()
        app.display_tracker = Mock()
        app._start_watchers = Mock()
        return app

    def _restricted_cfg(self):
        cfg = Config()
        cfg.update_from_server(RESTRICTED)
        return cfg

    def test_out_of_hours_stops_the_browser_tracker(self):
        """The browser tracker reads the frontmost tab's URL. It is the most
        sensitive thing we run, and the first cut of this fix never stopped it.

        NOTE this deliberately does NOT assert `app.sync_engine.browser_tracker is
        None`. sync_engine is a Mock here, and a Mock accepts and returns any
        attribute you invent — that assertion passed while the real engine (which
        reads `_browser_tracker`) was never wired at all. Assert the CALL instead,
        and see test_capture_lifecycle_real.py for the real-object contract.
        """
        app = self._app(self._restricted_cfg())
        browser = app.browser_tracker

        with patch("src.main.datetime") as dt:
            dt.now.return_value = OUT_OF_HOURS
            app._apply_capture_policy("test")

        browser.stop.assert_called_once()
        assert app.browser_tracker is None
        app.sync_engine.set_enrichment_trackers.assert_called_with(
            browser_tracker=None, display_tracker=None
        )

    def test_out_of_hours_stops_every_in_process_recorder(self):
        app = self._app(self._restricted_cfg())
        watchers = [app.window_watcher, app.input_watcher, app.input_source,
                    app.browser_tracker, app.display_tracker]

        with patch("src.main.datetime") as dt:
            dt.now.return_value = OUT_OF_HOURS
            app._apply_capture_policy("test")

        for w in watchers:
            w.stop.assert_called_once()
        app.aw_manager.set_capture_suppressed.assert_called_with(True, "test")

    def test_policy_converges_and_re_stops_a_resurrected_watcher(self):
        """_on_idle_pause used to restart the input watcher at 22:30 and the policy —
        which latched on transitions — never took it back down. The tick must
        re-assert the end state, not skip because 'nothing changed'."""
        app = self._app(self._restricted_cfg())

        with patch("src.main.datetime") as dt:
            dt.now.return_value = OUT_OF_HOURS
            app._apply_capture_policy("tick 1")
            app.input_watcher.stop.reset_mock()
            app._apply_capture_policy("tick 2")  # same state, must still stop

        app.input_watcher.stop.assert_called_once()

    def test_in_hours_starts_capture(self):
        app = self._app(self._restricted_cfg())

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS
            app._apply_capture_policy("test")

        app.aw_manager.set_capture_suppressed.assert_called_with(False, "test")
        app._start_watchers.assert_called_once()

    def test_unknown_schedule_stops_capture(self):
        app = self._app(Config())  # never talked to the server

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS  # inside hours, but we don't KNOW that
            app._apply_capture_policy("startup")

        app.aw_manager.set_capture_suppressed.assert_called_with(True, "startup")
        app._start_watchers.assert_not_called()


class TestIdlePauseDoesNotResurrectTheTap:
    def _app(self, cfg):
        app = object.__new__(main.BetterFlowApp)
        app.config = cfg
        app.input_watcher = Mock()
        app.input_watcher.is_running = False
        app.window_watcher = None
        app.reminder_manager = None
        return app

    def test_input_watcher_not_restarted_while_suppressed(self):
        cfg = Config()
        cfg.update_from_server(RESTRICTED)
        app = self._app(cfg)

        with patch("src.main.datetime") as dt:
            dt.now.return_value = OUT_OF_HOURS
            app._on_idle_pause(False)  # user touches the keyboard at 23:55

        app.input_watcher.start.assert_not_called()

    def test_input_watcher_restarted_when_allowed(self):
        cfg = Config()
        cfg.update_from_server(RESTRICTED)
        app = self._app(cfg)

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS
            app._on_idle_pause(False)

        app.input_watcher.start.assert_called_once()


class TestWindowSamplerIsGated:
    def test_sampler_is_a_noop_out_of_hours(self):
        c = object.__new__(main.SyncCoordinator)
        c.config = Config()
        c.config.update_from_server(RESTRICTED)
        c.sync_engine = Mock()

        with patch("src.main.datetime") as dt:
            dt.now.return_value = OUT_OF_HOURS
            c._sample_window()

        c.sync_engine.record_window_sample_if_active.assert_not_called()

    def test_sampler_runs_in_hours(self):
        c = object.__new__(main.SyncCoordinator)
        c.config = Config()
        c.config.update_from_server(RESTRICTED)
        c.sync_engine = Mock()

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS
            c._sample_window()

        c.sync_engine.record_window_sample_if_active.assert_called_once()
