"""Tests for the schedule-aware capture gate and its supporting config.

Covers the privacy boundary the admin UI promises: a restricted user is not
collected outside the enforced window unless they explicitly opt in for the day,
and the persisted schedule survives a restart so the launch decision is correct
before the first server fetch (fail-closed launch).
"""

from datetime import datetime, timedelta, timezone

from src.config import Config, WorkingHoursConfig
from src.work_schedule_gate import WorkScheduleGate


# Wednesday 2026-06-17 and Saturday 2026-06-20 (UTC) — stable, no Date.now().
def wed(h, m=0):
    return datetime(2026, 6, 17, h, m, tzinfo=timezone.utc)


def sat(h=10, m=0):
    return datetime(2026, 6, 20, h, m, tzinfo=timezone.utc)


def _restricted() -> WorkingHoursConfig:
    return WorkingHoursConfig(
        enforced=True,
        work_start="08:00",
        work_end="22:00",
        working_days=[1, 2, 3, 4, 5],
        timezone="UTC",
    )


class TestWorkingHoursWindow:
    def test_enforces_reflects_flag(self):
        assert _restricted().enforces() is True
        assert WorkingHoursConfig().enforces() is False  # default unrestricted

    def test_is_within_window_boundaries(self):
        wh = _restricted()
        assert wh.is_within_window(wed(7, 30)) is False  # before clock-in
        assert wh.is_within_window(wed(8, 0)) is True     # exactly at start
        assert wh.is_within_window(wed(13, 0)) is True    # mid-day
        assert wh.is_within_window(wed(22, 0)) is True     # exactly at end
        assert wh.is_within_window(wed(22, 1)) is False    # past end

    def test_is_within_window_non_working_day(self):
        assert _restricted().is_within_window(sat()) is False

    def test_naive_datetime_treated_as_utc(self):
        wh = _restricted()
        naive = datetime(2026, 6, 17, 9, 0)  # no tzinfo
        assert wh.is_within_window(naive) is True


class TestConfigRoundTrip:
    def test_working_hours_survive_save_load(self, tmp_path, monkeypatch):
        """The fail-closed-launch fix: an enforced schedule must be restored as a
        WorkingHoursConfig (not a raw dict) so enforces()/is_within_window work
        before the first post-login server fetch."""
        monkeypatch.setattr("src.config.user_config_dir", lambda *a, **k: str(tmp_path))

        cfg = Config()
        cfg.working_hours = _restricted()
        cfg.save()

        loaded = Config.load()
        assert isinstance(loaded.working_hours, WorkingHoursConfig)
        assert loaded.working_hours.enforces() is True
        assert loaded.working_hours.work_start == "08:00"
        assert loaded.working_hours.working_days == [1, 2, 3, 4, 5]
        assert loaded.working_hours.is_within_window(wed(7, 0)) is False
        assert loaded.working_hours.is_within_window(wed(9, 0)) is True


class TestCollectionAllowed:
    def test_unrestricted_always_allowed(self):
        cfg = Config()  # default enforced=False
        gate = WorkScheduleGate(cfg)
        assert gate.collection_allowed(sat(3)) is True
        assert gate.collection_allowed(wed(2)) is True

    def test_restricted_blocks_outside_window(self):
        cfg = Config()
        cfg.working_hours = _restricted()
        gate = WorkScheduleGate(cfg)
        assert gate.collection_allowed(wed(7, 0)) is False   # before hours
        assert gate.collection_allowed(wed(9, 0)) is True    # inside hours
        assert gate.collection_allowed(wed(23, 0)) is False  # after hours
        assert gate.collection_allowed(sat()) is False        # weekend

    def test_override_allows_outside_window(self):
        cfg = Config()
        cfg.working_hours = _restricted()
        gate = WorkScheduleGate(cfg)
        outside = wed(23, 0)
        assert gate.collection_allowed(outside) is False
        gate.request_work_outside_hours(now=outside)
        assert gate.collection_allowed(outside) is True

    def test_override_expires_at_end_of_local_day(self):
        cfg = Config()
        cfg.working_hours = _restricted()
        gate = WorkScheduleGate(cfg)
        outside = wed(23, 0)
        gate.request_work_outside_hours(now=outside)
        assert gate.override_active(outside) is True
        # Next day, same hour — past local midnight, so the override is gone.
        next_day = outside + timedelta(days=1)
        assert gate.override_active(next_day) is False
        assert gate.collection_allowed(next_day) is False

    def test_clear_override(self):
        cfg = Config()
        cfg.working_hours = _restricted()
        gate = WorkScheduleGate(cfg)
        outside = wed(23, 0)
        gate.request_work_outside_hours(now=outside)
        gate.clear_override()
        assert gate.override_active(outside) is False


class TestShouldOfferOverride:
    def test_offered_only_when_restricted_outside_and_not_overridden(self):
        cfg = Config()
        cfg.working_hours = _restricted()
        gate = WorkScheduleGate(cfg)
        # Outside hours, no override yet → offer it.
        assert gate.should_offer_override(wed(23, 0)) is True
        # Inside hours → nothing to offer.
        assert gate.should_offer_override(wed(9, 0)) is False
        # Once armed, the item disappears.
        gate.request_work_outside_hours(now=wed(23, 0))
        assert gate.should_offer_override(wed(23, 0)) is False

    def test_never_offered_for_unrestricted(self):
        gate = WorkScheduleGate(Config())
        assert gate.should_offer_override(sat(3)) is False
