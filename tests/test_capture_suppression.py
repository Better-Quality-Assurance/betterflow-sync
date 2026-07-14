"""Capture suppression: outside working hours the trackers must be DOWN, and
must stay down.

Filtering at upload time was never enough — the watchers kept writing window
titles and input activity to a local store on the employee's machine all night;
we simply declined to receive it. These tests pin the stronger property: outside
the window nothing records, and none of the health/restart paths may quietly
bring the trackers back up.
"""

from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest

from src.aw_manager import AWManager
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


@pytest.fixture
def manager():
    mgr = AWManager(aw_port=5600, afk_timeout=1200)
    # Never touch real binaries/processes in a unit test.
    with patch.object(AWManager, "_start_locked", autospec=True) as start, \
         patch.object(AWManager, "_stop_locked", autospec=True) as stop:
        start.return_value = True
        mgr._spy_start, mgr._spy_stop = start, stop
        yield mgr


class TestAWManagerSuppression:
    def test_suppressing_stops_the_trackers(self, manager):
        manager.set_capture_suppressed(True, "outside working hours")

        assert manager.capture_suppressed is True
        assert manager._spy_stop.called

    def test_repeated_suppression_converges_and_kills_strays(self):
        """The 60s tick re-asserts suppression every cycle. That is deliberate: if
        anything managed to spawn a tracker while we were suppressed, the next tick
        takes it back down. (The real _stop_locked no-ops when there is nothing to
        stop, so this costs nothing on a quiet night.)"""
        mgr = AWManager(aw_port=5600, afk_timeout=1200)
        mgr.set_capture_suppressed(True, "night")
        assert mgr.is_managing is False

        stray = Mock()
        stray.poll.return_value = None  # a live process appeared out of nowhere
        mgr._processes["bf-window-tracker"] = stray

        mgr.set_capture_suppressed(True, "night")  # next tick

        stray.terminate.assert_called()
        assert mgr.is_managing is False

    def test_resuming_starts_them_again(self, manager):
        manager.set_capture_suppressed(True, "night")
        manager.set_capture_suppressed(False, "morning")

        assert manager.capture_suppressed is False
        assert manager._spy_start.called

    def test_allowing_capture_on_a_fresh_process_starts_the_trackers(self, manager):
        """Regression: set_capture_suppressed() is now the ONLY thing that starts
        the trackers (AppController stopped calling start() directly). On a fresh
        process the flag already reads False, so an early-return on "no transition"
        left the trackers never started — the agent would record nothing, ever."""
        assert manager.capture_suppressed is False  # fresh process, flag untouched

        manager.set_capture_suppressed(False, "startup")

        assert manager._spy_start.called

    def test_allowing_capture_again_does_not_restart_a_live_stack(self, manager):
        """The 60s policy tick calls this every cycle while inside the window.
        Re-running _start_locked() on a live stack would see our OWN server on the
        port and misfile it as an external instance we must not manage."""
        manager.set_capture_suppressed(False, "startup")
        manager._spy_start.reset_mock()
        manager._processes["bf-data-service"] = Mock()  # stack is up

        manager.set_capture_suppressed(False, "tick")

        assert not manager._spy_start.called


class TestSuppressionSurvivesTheHealthChecks:
    """The whole point. AWManager aggressively self-heals a stopped tracker; if
    any of these paths still started it, capture suppression would last exactly
    until the next 60s health check and the machine would be recorded anyway."""

    def test_start_is_refused_while_suppressed(self):
        mgr = AWManager(aw_port=5600, afk_timeout=1200)
        mgr.set_capture_suppressed(True, "night")
        # Real _start_locked: must bail on the guard before touching binaries.
        assert mgr.start() is False
        assert mgr.is_managing is False

    def test_stale_watchdog_treats_down_as_healthy(self):
        mgr = AWManager(aw_port=5600, afk_timeout=1200)
        mgr.set_capture_suppressed(True, "night")
        # "Down on purpose" is healthy — not a stall to recover from.
        assert mgr.restart_if_needed() is True
        assert mgr.is_managing is False

    def test_force_restart_is_refused_while_suppressed(self):
        mgr = AWManager(aw_port=5600, afk_timeout=1200)
        mgr.set_capture_suppressed(True, "night")
        assert mgr.force_restart("server unreachable") is False
        assert mgr.is_managing is False

    def test_idle_tracker_restart_is_refused_while_suppressed(self):
        mgr = AWManager(aw_port=5600, afk_timeout=1200)
        mgr.set_capture_suppressed(True, "night")
        mgr.restart_idle_tracker("blind tracker")
        assert mgr.is_managing is False


class TestSyncLoopHonoursSuppression:
    def _engine(self, cfg):
        from src.sync.sync_engine import SyncEngine

        aw, bf, queue = Mock(), Mock(), Mock()
        bf.is_reachable.return_value = True
        queue.is_empty.return_value = True
        aw.get_buckets.return_value = {}  # no buckets: an in-hours cycle runs to a clean no-op
        engine = SyncEngine(aw=aw, bf=bf, queue=queue, config=cfg, time_tracker=Mock())
        engine._config_fetched = True  # skip the first-sync config fetch
        engine._backlog_reconciled = True  # skip the one-time whole-day replay
        return engine, aw, bf, queue

    def _sync_at(self, engine, when):
        with patch("src.sync.sync_engine.datetime") as dt:
            dt.now.return_value = when
            # Other datetime uses in the module must keep working.
            dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            return engine.sync()

    def test_out_of_hours_cycle_reads_nothing(self):
        cfg = Config()
        cfg.update_from_server(RESTRICTED)
        engine, aw, _bf, _q = self._engine(cfg)

        # 20:55 UTC == 23:55 Bucharest — the incident.
        stats = self._sync_at(engine, datetime(2026, 7, 13, 20, 55, tzinfo=timezone.utc))

        assert stats.capture_suppressed is True
        aw.get_events.assert_not_called()
        # A deliberately-stopped tracker is not an outage.
        assert "ActivityWatch is not running" not in stats.errors

    def test_out_of_hours_cycle_still_drains_the_queue(self):
        """In-hours work captured before the window shut must still upload — it
        must not be held hostage until the window reopens."""
        cfg = Config()
        cfg.update_from_server(RESTRICTED)
        engine, _aw, _bf, queue = self._engine(cfg)
        queue.is_empty.return_value = False

        with patch.object(engine, "_process_queue") as drain:
            self._sync_at(engine, datetime(2026, 7, 13, 20, 55, tzinfo=timezone.utc))

        drain.assert_called_once()

    def test_in_hours_cycle_is_not_suppressed(self):
        """Positive control. Without this, every assertion above would still pass
        if the gate simply suppressed everything always.

        Proves an in-hours cycle falls THROUGH the suppression branch and reaches
        the ActivityWatch check below it — the branch is time-dependent, not a
        blanket off-switch.
        """
        cfg = Config()
        cfg.update_from_server(RESTRICTED)
        engine, aw, _bf, _q = self._engine(cfg)
        aw.is_running.return_value = False  # bail at the AW check, just past the gate

        # 09:00 UTC == 12:00 Bucharest, a Monday — squarely inside 07:30-22:00.
        stats = self._sync_at(engine, datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc))

        assert stats.capture_suppressed is False
        assert "ActivityWatch is not running" in stats.errors

    def test_unknown_schedule_reads_nothing(self):
        """Fail closed: a device that has never been told its schedule records
        nothing, rather than everything."""
        engine, aw, _bf, _q = self._engine(Config())

        stats = self._sync_at(engine, datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc))

        assert stats.capture_suppressed is True
        aw.get_events.assert_not_called()


class TestTrayShowsPrivateHours:
    """A person whose machine has stopped being watched should see that in the tray,
    not have to take it on faith. Showing SYNCING while suppressed would be a lie."""

    def test_private_hours_state_exists_and_is_grey(self):
        from src.ui.tray import STATE_COLORS, TrayState

        assert TrayState.PRIVATE_HOURS in STATE_COLORS
        colour = STATE_COLORS[TrayState.PRIVATE_HOURS].lstrip("#")
        r, g, b = (int(colour[i:i + 2], 16) for i in (0, 2, 4))
        # Grey-ish: the three channels sit close together (no colour cast).
        assert max(r, g, b) - min(r, g, b) < 40, f"{colour} is not grey"

    def test_it_is_distinct_from_user_declared_private_time(self):
        """Same effect (nothing recorded), different cause: PRIVATE is a choice the
        user made, PRIVATE_HOURS is their contracted hours ending. The tooltip has to
        be able to tell them apart."""
        from src.ui.tray import STATE_COLORS, TrayState

        assert STATE_COLORS[TrayState.PRIVATE_HOURS] != STATE_COLORS[TrayState.PRIVATE]
        assert STATE_COLORS[TrayState.PRIVATE_HOURS] != STATE_COLORS[TrayState.PAUSED]

    def test_sync_still_runs_while_suppressed(self):
        """Guards the trap: the tray state is set from stats.capture_suppressed AFTER
        sync() runs, never by short-circuiting _do_sync. sync() is where the offline
        queue drains and where fetch_server_config() retries — skipping it would
        recreate the lockout where an agent that never learned its schedule never can."""
        import inspect

        import src.main as main

        src = inspect.getsource(main.SyncCoordinator._do_sync)
        assert "stats.capture_suppressed" in src, "tray state must come from the sync stats"
        # The suppression check must not appear before sync() as an early return.
        before_sync = src.split("self.sync_engine.sync()")[0]
        assert "PRIVATE_HOURS" not in before_sync, (
            "_do_sync short-circuits on suppression — that skips the queue drain and "
            "the config re-fetch"
        )
