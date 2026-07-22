"""Working-hours enforcement: the schedule must survive a save/load round-trip,
must fail closed when unknown, and must stop CAPTURE (not just upload).

Context (2026-07-14): a restricted user (07:30-22:00 Mon-Fri) was recorded until
23:55 and the events reached the server. Root cause was not the gate logic — it
was that `working_hours` was rebuilt from disk as a plain dict, so
`update_from_server` raised AttributeError (swallowed) and the gate's
`getattr(wh, "enforced", False)` read False. Enforcement was therefore off on
every device that had ever written a config.json. These tests pin all three
layers so it cannot silently regress to fail-open again.
"""

import json
from dataclasses import asdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from src.config import Config, WorkingHoursConfig, _normalize_hhmm

BUCHAREST = ZoneInfo("Europe/Bucharest")

# The real payload the server sends for a B2E user (see AgentConfigController).
# NOTE the empty timezone — production genuinely sends this, because the schedule
# row has no timezone set. The agent must fall back to machine-local, not crash
# and not fail open.
SERVER_PAYLOAD = {
    "working_hours": {
        "enforced": True,
        "work_start": "07:30",
        "work_end": "22:00",
        "working_days": [1, 2, 3, 4, 5],
        "timezone": "Europe/Bucharest",
    }
}


def _at(y, m, d, hh, mm, tz=BUCHAREST):
    return datetime(y, m, d, hh, mm, tzinfo=tz)


class TestSchedulePersistence:
    """The bug itself: a saved config must not degrade working_hours to a dict."""

    def test_working_hours_survives_save_load_as_dataclass(self):
        cfg = Config()
        cfg.update_from_server(SERVER_PAYLOAD)

        on_disk = json.loads(json.dumps(asdict(cfg)))
        reloaded = Config._from_dict(on_disk)

        assert isinstance(reloaded.working_hours, WorkingHoursConfig)
        assert reloaded.working_hours.enforced is True
        assert reloaded.working_hours.work_start == "07:30"
        assert reloaded.working_hours.work_end == "22:00"
        assert reloaded.working_hours.working_days == [1, 2, 3, 4, 5]
        assert reloaded.working_hours.known is True

    def test_server_config_applies_to_a_reloaded_config(self):
        """The exact production sequence: save -> relaunch -> load -> server says
        'you are restricted'. Before the fix this raised AttributeError internally,
        was swallowed, and left enforcement off forever."""
        stale = Config._from_dict(json.loads(json.dumps(asdict(Config()))))
        stale.update_from_server(SERVER_PAYLOAD)

        assert stale.working_hours.enforced is True
        assert stale.working_hours.known is True
        # 23:55 on a Monday — the exact moment we wrongly recorded.
        assert stale.working_hours.allows(_at(2026, 7, 13, 23, 55)) is False


class TestFailClosed:
    def test_unknown_schedule_records_nothing(self):
        """'We haven't been told the schedule' must mean 'do not record', not
        'record everything' — the old enforced=False default."""
        wh = WorkingHoursConfig()
        assert wh.known is False
        assert wh.allows(datetime.now(timezone.utc)) is False

    def test_known_and_unrestricted_records_always(self):
        """B2B / unrestricted users are 24/7 and must be unaffected."""
        wh = WorkingHoursConfig(enforced=False, known=True)
        assert wh.allows(_at(2026, 7, 13, 3, 0)) is True
        assert wh.allows(_at(2026, 7, 12, 23, 59)) is True  # Sunday

    @pytest.mark.parametrize(
        "bad",
        [
            {"enforced": True, "work_start": "7.30", "work_end": "22:00",
             "working_days": [1, 2, 3, 4, 5], "timezone": "Europe/Bucharest"},
            {"enforced": True, "work_start": "07:30", "work_end": "nonsense",
             "working_days": [1, 2, 3, 4, 5], "timezone": "Europe/Bucharest"},
            {"enforced": True, "work_start": "07:30", "work_end": "22:00",
             "working_days": [], "timezone": "Europe/Bucharest"},
            {"enforced": True, "work_start": "07:30", "work_end": "22:00",
             "working_days": ["not-a-day"], "timezone": "Europe/Bucharest"},
            {"enforced": True},  # window fields missing entirely
        ],
    )
    def test_a_malformed_window_never_widens_the_schedule(self, bad):
        """The whole hole, in one line of backend typo. An earlier version of
        _normalize_hhmm substituted the WIDEST defaults on bad input ("00:00" /
        "23:59") and then set known=True anyway — so `work_start: "7.30"` silently
        became a window starting at midnight and the restricted user was recorded
        all night. Bad input must keep the schedule we already trusted."""
        cfg = Config()
        cfg.update_from_server(SERVER_PAYLOAD)
        cfg.update_from_server({"working_hours": bad})

        assert cfg.working_hours.enforced is True
        assert cfg.working_hours.work_start == "07:30"
        assert cfg.working_hours.work_end == "22:00"
        assert cfg.working_hours.allows(_at(2026, 7, 13, 23, 55)) is False
        assert cfg.working_hours.allows(_at(2026, 7, 13, 0, 30)) is False

    def test_a_malformed_window_with_nothing_trusted_yet_records_nothing(self):
        cfg = Config()
        cfg.update_from_server({"working_hours": {"enforced": True, "work_start": "7.30"}})

        assert cfg.working_hours.known is False
        assert cfg.working_hours.allows(_at(2026, 7, 13, 12, 0)) is False

    def test_unrestricted_payload_needs_no_window(self):
        """B2B users get enforced:false and the server may send nothing else. That
        must still mark the schedule KNOWN, or they would never be tracked."""
        cfg = Config()
        cfg.update_from_server({"working_hours": {"enforced": False}})

        assert cfg.working_hours.known is True
        assert cfg.working_hours.allows(_at(2026, 7, 12, 3, 0)) is True  # Sunday 03:00


class TestWindow:
    @pytest.fixture
    def wh(self):
        cfg = Config()
        cfg.update_from_server(SERVER_PAYLOAD)
        return cfg.working_hours

    @pytest.mark.parametrize(
        "when,allowed",
        [
            (_at(2026, 7, 13, 7, 29), False),  # Mon, one minute early
            (_at(2026, 7, 13, 7, 30), True),   # Mon, window opens
            (_at(2026, 7, 13, 9, 32), True),   # Mon, her real arrival
            (_at(2026, 7, 13, 22, 0), True),   # Mon, last allowed minute
            (_at(2026, 7, 13, 22, 1), False),  # Mon, window shut
            (_at(2026, 7, 13, 23, 55), False), # Mon, the incident
            (_at(2026, 7, 14, 0, 30), False),  # Tue, after midnight
            (_at(2026, 7, 11, 12, 0), False),  # Saturday
            (_at(2026, 7, 12, 12, 0), False),  # Sunday
        ],
    )
    def test_window_boundaries(self, wh, when, allowed):
        assert wh.allows(when) is allowed

    def test_utc_instant_is_evaluated_in_the_schedule_timezone(self):
        """20:55 UTC is 23:55 in Bucharest — out of hours. Evaluating in UTC would
        wave it through, which is how a naive fix would still leak the incident."""
        cfg = Config()
        cfg.update_from_server(SERVER_PAYLOAD)
        instant = datetime(2026, 7, 13, 20, 55, tzinfo=timezone.utc)
        assert cfg.working_hours.allows(instant) is False


class TestNormalizeHhmm:
    def test_pads_so_string_compare_is_valid(self):
        # "7:30" > "22:00" lexically — unpadded, the whole night would pass.
        assert _normalize_hhmm("7:30") == "07:30"

    @pytest.mark.parametrize("bad", ["", "nonsense", "25:00", "12:99", "7.30", "0730", None])
    def test_garbage_raises_rather_than_widening_the_window(self, bad):
        # Must RAISE, not substitute a default. update_from_server catches this and
        # keeps the last trusted schedule; a silent fallback to "00:00"/"23:59" is
        # how a one-character backend typo reopens the whole night.
        with pytest.raises(ValueError):
            _normalize_hhmm(bad)


class TestOvernightWindow:
    """A night shift (22:00-06:00) made `start <= hhmm <= end` an EMPTY range, so
    the user was recorded for exactly zero seconds a day with nothing in the log to
    explain it. Fail-closed, but a total product break."""

    def _night_shift(self):
        cfg = Config()
        cfg.update_from_server({
            "working_hours": {
                "enforced": True, "work_start": "22:00", "work_end": "06:00",
                "working_days": [1, 2, 3, 4, 5], "timezone": "Europe/Bucharest",
            }
        })
        return cfg.working_hours

    @pytest.mark.parametrize(
        "when,allowed",
        [
            (_at(2026, 7, 13, 21, 59), False),  # Mon, before the shift
            (_at(2026, 7, 13, 22, 0), True),    # Mon, shift opens
            (_at(2026, 7, 13, 23, 55), True),   # Mon, mid-shift
            (_at(2026, 7, 14, 2, 0), True),     # Tue 02:00, still the shift
            (_at(2026, 7, 14, 6, 0), True),     # Tue, last minute
            (_at(2026, 7, 14, 6, 1), False),    # Tue, shift shut
            (_at(2026, 7, 14, 12, 0), False),   # Tue midday
        ],
    )
    def test_wraps_past_midnight(self, when, allowed):
        assert self._night_shift().allows(when) is allowed


def _machine_tz(monkeypatch, name):
    """Pin what the device reports as its own timezone, overriding the conftest
    Bucharest default. One setattr suffices: both _localize and
    bf_client._detect_timezone reach the detector through the config module."""
    import src.config as config_module

    monkeypatch.setattr(config_module, "detect_machine_timezone", lambda: name)


class TestTimezoneHarmonization:
    """The device's own timezone is authoritative for evaluation; a schedule whose
    server anchor has drifted from it self-heals, and the drift is reported.

    Origin: 2026-07-22 — a device reporting America/Los_Angeles while the schedule
    was authored for a Europe-based user recorded nothing all day, because the window
    was judged in the stale LA anchor. Correcting the machine's clock must fix it
    immediately, without waiting for an admin to re-anchor server-side."""

    def _la_anchored(self):
        wh = WorkingHoursConfig(
            enforced=True, work_start="07:30", work_end="22:00",
            working_days=[1, 2, 3, 4, 5], timezone="America/Los_Angeles", known=True,
        )
        return wh

    def test_drifted_anchor_self_heals_to_machine_local(self, monkeypatch):
        """Anchor says LA, machine is really Bucharest. Wed 10:00 Bucharest is a work
        moment and MUST be allowed — pre-fix it was evaluated in LA (00:00 there) and
        rejected, zeroing the day."""
        _machine_tz(monkeypatch, "Europe/Bucharest")
        wh = self._la_anchored()
        wed_10_bucharest = datetime(2026, 7, 22, 7, 0, tzinfo=timezone.utc)  # 10:00 EEST
        assert wh.allows(wed_10_bucharest) is True

    def test_drifted_anchor_still_closes_the_evening_in_machine_local(self, monkeypatch):
        """Late-evening Bucharest is outside the window even though the LA anchor
        would still call it midday — machine-local is what protects the evening."""
        _machine_tz(monkeypatch, "Europe/Bucharest")
        wh = self._la_anchored()
        wed_2330_bucharest = datetime(2026, 7, 22, 20, 30, tzinfo=timezone.utc)  # 23:30 EEST
        assert wh.allows(wed_2330_bucharest) is False

    def test_timezone_mismatch_reports_drifted_anchor(self, monkeypatch):
        _machine_tz(monkeypatch, "Europe/Bucharest")
        assert self._la_anchored().timezone_mismatch() == "America/Los_Angeles"

    def test_timezone_mismatch_none_when_offsets_agree(self, monkeypatch):
        """Aligned anchor, and same-offset aliases, are NOT drift: comparison is by
        live UTC offset, not zone name."""
        _machine_tz(monkeypatch, "Europe/Bucharest")
        aligned = WorkingHoursConfig(
            enforced=True, work_start="07:30", work_end="22:00",
            working_days=[1, 2, 3, 4, 5], timezone="Europe/Bucharest", known=True,
        )
        assert aligned.timezone_mismatch() is None

        _machine_tz(monkeypatch, "Etc/UTC")
        utc_alias = WorkingHoursConfig(
            enforced=True, work_start="07:30", work_end="22:00",
            working_days=[1, 2, 3, 4, 5], timezone="UTC", known=True,
        )
        assert utc_alias.timezone_mismatch() is None

    def test_timezone_mismatch_none_when_no_anchor_or_not_enforced(self, monkeypatch):
        _machine_tz(monkeypatch, "Europe/Bucharest")
        no_anchor = WorkingHoursConfig(
            enforced=True, work_start="07:30", work_end="22:00",
            working_days=[1, 2, 3, 4, 5], timezone="", known=True,
        )
        assert no_anchor.timezone_mismatch() is None
        assert WorkingHoursConfig(enforced=False, known=True).timezone_mismatch() is None
        assert WorkingHoursConfig().timezone_mismatch() is None  # unknown

    def test_window_close_after_uses_machine_local_not_anchor(self, monkeypatch):
        """window_close_after must clamp to the machine-local 22:00, so a span that
        began in-window closes at the employee's real evening, not LA's."""
        _machine_tz(monkeypatch, "Europe/Bucharest")
        wh = self._la_anchored()
        start = datetime(2026, 7, 22, 15, 0, tzinfo=timezone.utc)  # 18:00 Bucharest
        close = wh.window_close_after(start)
        # 22:00 Bucharest (EEST, UTC+3) == 19:00 UTC
        assert close == datetime(2026, 7, 22, 19, 0, tzinfo=timezone.utc)

    def test_next_boundary_after_uses_machine_local(self, monkeypatch):
        """The next flip is the machine-local window edge. From 06:00 Bucharest the
        next boundary is 07:30 Bucharest (04:30 UTC), regardless of the LA anchor."""
        _machine_tz(monkeypatch, "Europe/Bucharest")
        wh = self._la_anchored()
        when = datetime(2026, 7, 22, 3, 0, tzinfo=timezone.utc)  # 06:00 Bucharest
        boundary = wh.next_boundary_after(when)
        assert boundary == datetime(2026, 7, 22, 4, 30, tzinfo=timezone.utc)

    def test_detection_is_cache_served_not_per_call(self, monkeypatch):
        """_localize calls detect per event (upload gate) and up to thousands of
        times per next_boundary_after walk; detection must be cache-served within a
        time bucket, not an os.readlink/tzlocal lookup each time. Guards against
        reverting to the uncached hot-path storm both reviewers flagged."""
        import src.config as config_module

        calls = {"n": 0}

        def counting():
            calls["n"] += 1
            return "Europe/Bucharest"

        monkeypatch.setattr(
            config_module, "_detect_machine_timezone_uncached", counting
        )
        config_module._detect_machine_timezone_bucketed.cache_clear()
        bucket = 424242  # one fixed monotonic bucket
        for _ in range(200):
            config_module._detect_machine_timezone_bucketed(bucket)
        assert calls["n"] == 1
        config_module._detect_machine_timezone_bucketed.cache_clear()
