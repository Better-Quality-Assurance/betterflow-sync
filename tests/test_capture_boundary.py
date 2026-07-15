"""Working-hours boundary trigger — capture must stop/start AT the window edge.

The 60s enforcement tick converged capture toward the schedule only every 60s, so a
window closing at 22:00 kept every in-process recorder running for up to a full tick
(plus scheduler jitter) past the boundary. sync() already stopped UPLOADING at the
exact instant via a live now() read, so nothing left the machine — but local
RECORDING had a tail, which the enforcement docstrings say must not happen.

These tests pin the one-shot DateTrigger (SyncCoordinator._arm_capture_boundary /
_fire_capture_boundary) that lands the hard stop/start ON the edge and re-arms for
the next one, and the WorkingHoursConfig.next_boundary_after helper that computes it.
"""

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


class _FakeScheduler:
    """Records add/remove so the boundary job can be inspected without a real
    APScheduler thread."""

    def __init__(self, running=True):
        self.running = running
        self.jobs = {}
        self.removed = []

    def add_job(self, fn, trigger=None, id=None, replace_existing=False, **kw):
        self.jobs[id] = {"fn": fn, "trigger": trigger}

    def remove_job(self, id):
        if id not in self.jobs:
            raise main.JobLookupError(id)
        del self.jobs[id]
        self.removed.append(id)


def _coordinator(cfg, running=True):
    c = object.__new__(main.SyncCoordinator)
    c.config = cfg
    c.scheduler = _FakeScheduler(running=running)
    c.apply_capture_policy = Mock()
    return c


def _restricted_cfg():
    cfg = Config()
    cfg.update_from_server(RESTRICTED)
    return cfg


class TestArmCaptureBoundary:
    def test_boundary_is_armed_at_the_window_close_not_60s_later(self):
        """THE fix: a one-shot job is scheduled at the EXACT window edge. Pre-fix
        there is no boundary trigger at all, so capture only stopped whenever the
        next 60s tick happened to fire — up to a full tick past 22:00."""
        c = _coordinator(_restricted_cfg())

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c._arm_capture_boundary()

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
            c._arm_capture_boundary()

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
            c._arm_capture_boundary()  # must NOT raise

        assert "capture_boundary" not in c.scheduler.jobs
        assert "capture_boundary" in c.scheduler.removed

    def test_no_arming_when_policy_is_unwired(self):
        c = _coordinator(_restricted_cfg())
        c.apply_capture_policy = None

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c._arm_capture_boundary()

        assert "capture_boundary" not in c.scheduler.jobs

    def test_no_arming_when_scheduler_stopped(self):
        c = _coordinator(_restricted_cfg(), running=False)

        with patch("src.main.datetime") as dt:
            dt.now.return_value = IN_HOURS_UTC
            c._arm_capture_boundary()

        assert "capture_boundary" not in c.scheduler.jobs
