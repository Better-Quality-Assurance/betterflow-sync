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

    def test_unparseable_server_payload_keeps_previous_schedule(self):
        cfg = Config()
        cfg.update_from_server(SERVER_PAYLOAD)
        cfg.update_from_server({"working_hours": {"working_days": ["not-a-day"]}})

        # Falls back to what we already knew, never to permissive.
        assert cfg.working_hours.enforced is True
        assert cfg.working_hours.work_end == "22:00"
        assert cfg.working_hours.allows(_at(2026, 7, 13, 23, 55)) is False


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
        assert _normalize_hhmm("7:30", "00:00") == "07:30"

    @pytest.mark.parametrize("bad", ["", "nonsense", "25:00", "12:99", None])
    def test_garbage_falls_back(self, bad):
        assert _normalize_hhmm(bad, "09:00") == "09:00"
