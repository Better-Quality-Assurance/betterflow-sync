"""Working-hours boundary trigger — capture must stop/start AT the window edge.

The 60s enforcement tick converged capture toward the schedule only every 60s, so a
window closing at 22:00 kept every in-process recorder running for up to a full tick
(plus scheduler jitter) past the boundary. sync() already stopped UPLOADING at the
exact instant via a live now() read, so nothing left the machine — but local
RECORDING had a tail, which the enforcement docstrings say must not happen.

These tests pin the one-shot DateTrigger (SyncCoordinator.arm_capture_boundary /
_fire_capture_boundary) that lands the hard stop/start ON the edge and re-arms for
the next one, and the WorkingHoursConfig.next_boundary_after helper that computes it.
"""

import threading
from datetime import datetime, timedelta, timezone
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

# Known + enforced but EDGELESS: a full 00:00-23:59 window on every day means
# allows() is constant-True, so next_boundary_after finds no flip in its 8-day walk
# and returns None — the same None an unknown/unrestricted schedule yields, but here
# the self-heal still treats the schedule as restricted (known+enforced).
FULL_WINDOW = {
    "working_hours": {
        "enforced": True,
        "work_start": "00:00",
        "work_end": "23:59",
        "working_days": [1, 2, 3, 4, 5, 6, 7],
        "timezone": "Europe/Bucharest",
    }
}

# 2026-07-13 is a Monday; Europe/Bucharest is UTC+3 (EEST) in July.
IN_HOURS_UTC = datetime(2026, 7, 13, 18, 0, tzinfo=timezone.utc)        # 21:00 Bucharest
# allows() is inclusive of 22:00, so it flips False at 22:01 Bucharest = 19:01 UTC.
WINDOW_CLOSE_UTC = datetime(2026, 7, 13, 19, 1, tzinfo=timezone.utc)
OUT_OF_HOURS_UTC = datetime(2026, 7, 13, 20, 55, tzinfo=timezone.utc)   # 23:55 Bucharest
# Next open after that night: Tue 07:30 Bucharest = 04:30 UTC.
NEXT_OPEN_UTC = datetime(2026, 7, 14, 4, 30, tzinfo=timezone.utc)


def _wh(payload=RESTRICTED):
    cfg = Config()
    cfg.update_from_server(payload)
    return cfg.working_hours


class TestNextBoundaryAfter:
    def test_window_close_is_the_next_boundary_from_inside_hours(self):
        wh = _wh()
        b = wh.next_boundary_after(IN_HOURS_UTC)
        assert b == WINDOW_CLOSE_UTC
        assert wh.allows(IN_HOURS_UTC) is True
        assert wh.allows(b) is False  # capture must be OFF at (and past) the edge

    def test_next_open_is_the_next_boundary_from_outside_hours(self):
        wh = _wh()
        b = wh.next_boundary_after(OUT_OF_HOURS_UTC)
        assert b == NEXT_OPEN_UTC
        assert wh.allows(OUT_OF_HOURS_UTC) is False
        assert wh.allows(b) is True

    def test_unknown_schedule_has_no_boundary(self):
        # Fail-closed schedule: allows() is always False, so there is no edge.
        assert Config().working_hours.next_boundary_after(IN_HOURS_UTC) is None

    def test_unrestricted_schedule_has_no_boundary(self):
        wh = _wh({"working_hours": {"enforced": False}})
        assert wh.next_boundary_after(IN_HOURS_UTC) is None

    def test_edgeless_enforced_schedule_has_no_boundary(self):
        # A full 00:00-23:59 all-days window is known+enforced yet allows() never
        # flips, so the 8-day walk finds no edge and returns None — the root cause of
        # the self-heal churn the coordinator guard fixes.
        wh = _wh(FULL_WINDOW)
        assert wh.known is True and wh.enforced is True
        assert wh.allows(IN_HOURS_UTC) is True
        assert wh.next_boundary_after(IN_HOURS_UTC) is None


class _FakeScheduler:
    """Records add/remove so the boundary job can be inspected without a real
    APScheduler thread."""

    def __init__(self, running=True):
        self.running = running
        self.jobs = {}
        self.removed = []

    def add_job(self, fn, trigger=None, id=None, replace_existing=False, **kw):
        self.jobs[id] = {"fn": fn, "trigger": trigger, "kwargs": kw}

    def remove_job(self, id):
        if id not in self.jobs:
            raise main.JobLookupError(id)
        del self.jobs[id]
        self.removed.append(id)

    def get_job(self, id):
        return self.jobs.get(id)


def _coordinator(cfg, running=True):
    c = object.__new__(main.SyncCoordinator)
    c.config = cfg
    c.scheduler = _FakeScheduler(running=running)
    c.apply_capture_policy = Mock()
    c._boundary_lock = threading.Lock()
    return c


def _restricted_cfg():
    cfg = Config()
    cfg.update_from_server(RESTRICTED)
    return cfg


def _edgeless_cfg():
    cfg = Config()
    cfg.update_from_server(FULL_WINDOW)
    return cfg


class TestArmCaptureBoundary:
    def test_boundary_is_armed_at_the_window_close_not_60s_later(self):
        """THE fix: a one-shot job is scheduled at the EXACT window edge. Pre-fix
        there is no boundary trigger at all, so capture only stopped whenever the
        next 60s tick happened to fire — up to a full tick past 22:00."""
        c = _coordinator(_restricted_cfg())

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c.arm_capture_boundary()

        assert "capture_boundary" in c.scheduler.jobs
        trig = c.scheduler.jobs["capture_boundary"]["trigger"]
        assert trig.run_date == WINDOW_CLOSE_UTC

    def test_firing_the_boundary_applies_policy_and_re_arms(self):
        c = _coordinator(_restricted_cfg())

        # The job fires AT the window-close instant, so that is what now() reads.
        with patch("src.main.datetime") as dt:
            dt.now.return_value = WINDOW_CLOSE_UTC
            c._fire_capture_boundary()

        # Enforcement runs AT the edge, tagged so logs/telemetry can tell it apart.
        c.apply_capture_policy.assert_called_once_with("boundary")
        # ...and it re-arms for the following edge (the next window open).
        assert "capture_boundary" in c.scheduler.jobs
        assert c.scheduler.jobs["capture_boundary"]["trigger"].run_date == NEXT_OPEN_UTC

    def test_unknown_schedule_arms_nothing_and_clears_a_stale_job(self):
        c = _coordinator(Config())  # never talked to the server
        c.scheduler.jobs["capture_boundary"] = {"fn": None, "trigger": None}  # stale

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c.arm_capture_boundary()

        assert "capture_boundary" not in c.scheduler.jobs
        assert "capture_boundary" in c.scheduler.removed

    def test_boundary_computation_error_clears_a_stale_job(self):
        # next_boundary_after raises (e.g. schedule superseded mid-computation).
        # A boundary job left armed from an EARLIER successful compute was scheduled
        # against a now-stale instant; it must be dropped so the 60s tick is the sole
        # enforcement authority until the next successful arm. No exception may escape.
        c = _coordinator(_restricted_cfg())
        c.config.working_hours.next_boundary_after = Mock(
            side_effect=RuntimeError("schedule superseded")
        )
        c.scheduler.jobs["capture_boundary"] = {"fn": None, "trigger": None}  # stale

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c.arm_capture_boundary()  # must NOT raise

        assert "capture_boundary" not in c.scheduler.jobs
        assert "capture_boundary" in c.scheduler.removed

    def test_no_arming_when_policy_is_unwired(self):
        c = _coordinator(_restricted_cfg())
        c.apply_capture_policy = None

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c.arm_capture_boundary()

        assert "capture_boundary" not in c.scheduler.jobs

    def test_no_arming_when_scheduler_stopped(self):
        c = _coordinator(_restricted_cfg(), running=False)

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c.arm_capture_boundary()

        assert "capture_boundary" not in c.scheduler.jobs


class TestMisfireSafeArming:
    """Finding 1 (HIGH): a >1s stall across the run date must NOT let APScheduler
    silently DISCARD the one-shot (the default misfire_grace_time=1 does), because
    the re-arm lives only in _fire_capture_boundary's finally — which never runs for
    a discarded job."""

    def test_boundary_add_job_uses_none_misfire_grace_time(self):
        c = _coordinator(_restricted_cfg())

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c.arm_capture_boundary()

        # None = "fire however late"; a late fire is safe because
        # _fire_capture_boundary re-reads allows(now) live at fire time.
        assert c.scheduler.jobs["capture_boundary"]["kwargs"]["misfire_grace_time"] is None


class TestBoundarySelfHeal:
    """Finding 1: the 60s tick re-arms a boundary that a misfire discarded, so a
    restricted user is never left with the recording tail until the next refetch."""

    def test_tick_rearms_a_missing_boundary_when_restricted(self):
        c = _coordinator(_restricted_cfg())
        assert c.scheduler.get_job("capture_boundary") is None  # discarded by a misfire

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c._self_heal_capture_boundary()

        assert "capture_boundary" in c.scheduler.jobs
        assert c.scheduler.jobs["capture_boundary"]["trigger"].run_date == WINDOW_CLOSE_UTC

    def test_tick_leaves_an_already_armed_boundary_untouched(self):
        c = _coordinator(_restricted_cfg())
        sentinel = {"fn": None, "trigger": None}
        c.scheduler.jobs["capture_boundary"] = sentinel

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c._self_heal_capture_boundary()

        # No re-arm churn when the job is present.
        assert c.scheduler.jobs["capture_boundary"] is sentinel

    def test_tick_arms_nothing_for_an_unrestricted_schedule(self):
        cfg = Config()
        cfg.update_from_server({"working_hours": {"enforced": False}})
        c = _coordinator(cfg)

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c._self_heal_capture_boundary()

        assert "capture_boundary" not in c.scheduler.jobs

    def test_tick_arms_nothing_for_an_unknown_schedule(self):
        c = _coordinator(Config())  # never talked to the server → known=False

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c._self_heal_capture_boundary()

        assert "capture_boundary" not in c.scheduler.jobs

    def test_tick_does_not_churn_or_log_for_an_edgeless_enforced_schedule(self):
        """Round-2 (MEDIUM): a known+enforced but EDGELESS schedule (full all-day
        window) legitimately arms no boundary. The self-heal must NOT re-arm+log every
        tick — that was ~1440 misleading 'job missing' lines/day plus an 8-day allows()
        re-walk under _boundary_lock each tick, forever."""
        c = _coordinator(_edgeless_cfg())

        # The real arm resolves to no boundary and records the edgeless state.
        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c.arm_capture_boundary()
        assert "capture_boundary" not in c.scheduler.jobs
        assert c._boundary_edgeless is True

        # Now hammer the tick: it passes the known+enforced gate but must stop at the
        # edgeless guard — no re-arm attempt, no "job missing" log.
        c.arm_capture_boundary = Mock()
        with patch("src.main.datetime") as dt, patch("src.main.logger") as log:
            dt.now.return_value = IN_HOURS_UTC
            for _ in range(5):
                c._self_heal_capture_boundary()

        c.arm_capture_boundary.assert_not_called()
        missing_logs = [
            call for call in log.info.call_args_list if "missing" in str(call)
        ]
        assert not missing_logs, f"self-heal churned a log while edgeless: {missing_logs}"

    def test_tick_resumes_rearming_after_schedule_gains_an_edge(self):
        """The edgeless latch must clear when the schedule regains a real edge, so a
        genuinely misfire-discarded job is still re-armed. Arm edgeless first (latch
        set), then swap in a restricted schedule and arm (latch cleared), then let a
        misfire discard the job — the next tick must re-arm."""
        c = _coordinator(_edgeless_cfg())
        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c.arm_capture_boundary()
        assert c._boundary_edgeless is True

        # Schedule changes to a bounded window; arm clears the latch and arms a job.
        c.config = _restricted_cfg()
        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c.arm_capture_boundary()
        assert c._boundary_edgeless is False
        assert "capture_boundary" in c.scheduler.jobs

        # Simulate a misfire discarding the job; the tick must re-arm it now.
        c._disarm_capture_boundary()
        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c._self_heal_capture_boundary()
        assert "capture_boundary" in c.scheduler.jobs


class TestArmPathIsFailSafe:
    """Finding 3 (MEDIUM): the remove/add now sits INSIDE the try. A scheduler shut
    down between the .running check and the add (a real TOCTOU) — or a jobstore
    error — must be swallowed, not escape into _fire_capture_boundary's finally
    (which would kill re-arm) or into fetch_server_config (whose except only catches
    auth/client errors)."""

    def test_add_job_failure_does_not_escape(self):
        c = _coordinator(_restricted_cfg())
        c.scheduler.add_job = Mock(side_effect=RuntimeError("scheduler shut down"))

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c.arm_capture_boundary()  # must NOT raise

    def test_fire_boundary_rearm_failure_does_not_escape(self):
        c = _coordinator(_restricted_cfg())
        c.scheduler.add_job = Mock(side_effect=RuntimeError("boom"))

        with patch("src.main.datetime") as dt:
            dt.now.return_value = WINDOW_CLOSE_UTC
            c._fire_capture_boundary()  # must NOT raise

        # Enforcement at the edge still ran even though the re-arm failed.
        c.apply_capture_policy.assert_called_once_with("boundary")


class TestArmPathLocking:
    """Finding 2 (MEDIUM): arm_capture_boundary is called from three threads (boundary
    worker, sync thread via _on_config_updated, wake handler); its compute→remove→add
    must run under _boundary_lock so an interleaving can't leave a restricted user with
    no boundary job."""

    def test_arm_body_runs_under_the_boundary_lock(self):
        c = _coordinator(_restricted_cfg())
        events = []

        class TrackingLock:
            def __enter__(self):
                events.append("enter")
                return self

            def __exit__(self, *a):
                events.append("exit")
                return False

        c._boundary_lock = TrackingLock()

        # add_job must be observed WHILE the lock is held (between enter and exit).
        def _record_add(*a, **kw):
            events.append("add_job")

        c.scheduler.add_job = _record_add

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c.arm_capture_boundary()

        assert events == ["enter", "add_job", "exit"]

    def test_concurrent_arms_leave_a_boundary_armed(self):
        # Hammer arm from several threads at once against a real lock; the invariant
        # is that a restricted schedule ALWAYS ends with a boundary job present.
        c = _coordinator(_restricted_cfg())

        def _worker():
            for _ in range(20):
                c.arm_capture_boundary()

        # ONE patch around all threads. mock.patch enter/exit is NOT
        # thread-safe against itself for the same target: two threads
        # patching concurrently can save the other thread's MOCK as the
        # "original", leaving src.main.datetime a leaked frozen MagicMock for
        # the rest of the pytest process — which made the idle-tracker-health
        # freshness gate compute a negative input age and flake
        # test_silent_when_input_is_stale suite-wide (reproduced and proven
        # by instrumentation).
        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            threads = [threading.Thread(target=_worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert "capture_boundary" in c.scheduler.jobs


OVERNIGHT = {
    "working_hours": {
        "enforced": True,
        "work_start": "22:00",
        "work_end": "06:00",
        "working_days": [1, 2, 3, 4, 5],
        "timezone": "Europe/Bucharest",
    }
}


class TestBoundaryAcrossDstAndOvernight:
    """Findings 5/6: the minute-walk boundary must land on the right UTC instant
    across a Bucharest DST transition (the wall-clock edge holds; the offset moves)
    and for an overnight (start > end) shift."""

    def test_boundary_spans_bucharest_dst_fall_back(self):
        # Fall-back is Sun 2026-10-25 (EEST UTC+3 → EET UTC+2). From a weekend instant
        # before it, the next OPEN is Mon 2026-10-26 07:30 LOCAL — by then EET, so
        # 05:30 UTC (not 04:30, which would be the pre-change offset).
        wh = _wh()
        when = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)  # Sat, outside hours
        b = wh.next_boundary_after(when)
        assert b == datetime(2026, 10, 26, 5, 30, tzinfo=timezone.utc)
        assert wh.allows(b) is True
        assert wh.allows(b - timedelta(minutes=1)) is False

    def test_boundary_spans_bucharest_spring_forward(self):
        # Spring-forward is Sun 2026-03-29 (EET UTC+2 → EEST UTC+3). Next open Mon
        # 2026-03-30 07:30 LOCAL is EEST → 04:30 UTC.
        wh = _wh()
        when = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)  # Sat, outside hours
        b = wh.next_boundary_after(when)
        assert b == datetime(2026, 3, 30, 4, 30, tzinfo=timezone.utc)
        assert wh.allows(b) is True
        assert wh.allows(b - timedelta(minutes=1)) is False

    def test_overnight_shift_close_boundary(self):
        wh = _wh(OVERNIGHT)
        # Tue 01:00 Bucharest (= the tail of Monday's 22:00–06:00 shift) = Mon 22:00 UTC.
        mid = datetime(2026, 7, 13, 22, 0, tzinfo=timezone.utc)
        assert wh.allows(mid) is True
        b = wh.next_boundary_after(mid)
        # allows is inclusive through 06:00, so it flips at 06:01 local = 03:01 UTC.
        assert b == datetime(2026, 7, 14, 3, 1, tzinfo=timezone.utc)
        assert wh.allows(b) is False

    def test_overnight_shift_open_boundary(self):
        wh = _wh(OVERNIGHT)
        # Tue 12:00 Bucharest — the daylight gap between shifts = 09:00 UTC.
        gap = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
        assert wh.allows(gap) is False
        b = wh.next_boundary_after(gap)
        # Next open: Tue 22:00 local = 19:00 UTC.
        assert b == datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)
        assert wh.allows(b) is True


class TestRealSchedulerMisfire:
    """Finding 6/1: an integration proof against a REAL BackgroundScheduler that a
    one-shot whose run_date is already in the past (a stall past the edge) STILL
    fires with misfire_grace_time=None. Under the default grace=1 it is discarded."""

    def test_near_past_run_date_still_fires_with_none_grace(self):
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.date import DateTrigger

        fired = threading.Event()
        sched = BackgroundScheduler()
        sched.start()
        try:
            run_date = datetime.now(timezone.utc) - timedelta(seconds=3)
            sched.add_job(
                fired.set,
                trigger=DateTrigger(run_date=run_date),
                id="capture_boundary",
                misfire_grace_time=None,
            )
            assert fired.wait(timeout=5), "past-dated one-shot was discarded despite grace=None"
        finally:
            sched.shutdown(wait=False)
