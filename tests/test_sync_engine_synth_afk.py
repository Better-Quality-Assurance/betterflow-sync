"""Synthesizing a not-afk span when the idle tracker has frozen.

The SERVER decides active-vs-idle from the uploaded AFK stream
(sync_engine.py: "active-vs-idle is decided SERVER-SIDE from the AFK stream").
When bf-idle-tracker freezes, the agent uploads NO fresh not-afk events, so the
server bills the worked span as idle — the recurring "Active time not advancing
while the user works" fleet alert (Razvan, 2026-06-19: ~2.9h of a clicking/typing
user billed idle while his AFK age read 37,159s).

#53/#56 added an OS-idle-clock fallback, but only inside idle_manager's LOCAL
pause decision — it never injects a not-afk event into the uploaded stream, so
server-side active time still freezes. These tests cover the missing piece: when
the AFK event is stale AND the OS idle clock shows the user was active since the
freeze, the engine emits a synthetic not-afk span so the server counts it active.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from src.config import Config
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.aw_client import BUCKET_TYPE_AFK, AWEvent
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.sync_engine import SyncEngine


def _make_engine():
    engine = SyncEngine(
        aw=Mock(),
        bf=Mock(),
        queue=Mock(),
        config=Config(),
        activity_analyzer=Mock(spec=ActivityAnalyzer),
        time_tracker=Mock(spec=DailyTimeTracker),
    )
    return engine


def _afk(start: datetime, dur: float, status: str = "afk") -> AWEvent:
    return AWEvent(id=0, timestamp=start, duration=dur, data={"status": status})


NOW = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


def test_synthesizes_notafk_when_tracker_frozen_and_user_active():
    """Stale AFK (froze 1h ago) + OS clock says active 5s ago -> not-afk span
    covering [freeze point, last input], so the server stops billing it idle."""
    engine = _make_engine()
    engine._get_system_idle_seconds = lambda: 5.0  # user active 5s ago
    # Tracker froze at 11:00 (event ended), now is 12:00 -> 3600s stale.
    frozen = _afk(NOW - timedelta(seconds=3660), 60, "afk")  # ends 11:00:00

    ev = engine._synthesize_active_afk_event(frozen, "aw-watcher-afk_host", now=NOW)

    assert ev is not None, "must emit a not-afk span to cover the worked gap"
    assert ev["data"]["status"] == "not-afk"
    assert ev["bucket_type"] == BUCKET_TYPE_AFK
    assert ev["bucket_id"] == "aw-watcher-afk_host"
    # Span: from the freeze point (11:00:00) to the last real input (now-5s).
    assert ev["timestamp"] == (NOW - timedelta(seconds=3600)).isoformat()
    assert round(ev["duration"]) == 3595  # 3600s gap minus the trailing 5s idle


def test_no_synth_when_tracker_fresh():
    """A live tracker heartbeats; a recent AFK event is not stale -> nothing."""
    engine = _make_engine()
    engine._get_system_idle_seconds = lambda: 1.0
    fresh = _afk(NOW - timedelta(seconds=30), 20, "afk")  # ends 30s ago, < grace

    assert engine._synthesize_active_afk_event(fresh, "b", now=NOW) is None


def test_no_synth_when_os_clock_says_idle():
    """Tracker stale but the OS idle clock confirms the user is genuinely idle
    -> never fabricate activity."""
    engine = _make_engine()
    engine._get_system_idle_seconds = lambda: 4000.0  # idle well past grace
    frozen = _afk(NOW - timedelta(seconds=3660), 60, "afk")

    assert engine._synthesize_active_afk_event(frozen, "b", now=NOW) is None


def test_no_synth_when_os_clock_unavailable():
    """No OS idle clock (Linux / ioreg failure) -> can't confirm activity, so
    don't synthesize (conservative; never invent billable time)."""
    engine = _make_engine()
    engine._get_system_idle_seconds = lambda: None
    frozen = _afk(NOW - timedelta(seconds=3660), 60, "afk")

    assert engine._synthesize_active_afk_event(frozen, "b", now=NOW) is None


def test_no_synth_when_no_activity_after_freeze_point():
    """The last input predates the freeze point (user idle through the whole
    stale span, tracker just lagged) -> nothing to claim as active."""
    engine = _make_engine()
    # Froze 100s ago but last input was 200s ago -> last_input < freeze end.
    engine._get_system_idle_seconds = lambda: 200.0
    frozen = _afk(NOW - timedelta(seconds=160), 60, "afk")  # ends 100s ago

    assert engine._synthesize_active_afk_event(frozen, "b", now=NOW) is None


def test_no_synth_when_no_afk_event_at_all():
    """No AFK stream to extend from -> conservative no-op."""
    engine = _make_engine()
    engine._get_system_idle_seconds = lambda: 5.0

    assert engine._synthesize_active_afk_event(None, "b", now=NOW) is None


def _bucket(bucket_id: str):
    b = Mock()
    b.id = bucket_id
    return b


def test_gather_synthesizes_for_betterflow_afk_bucket():
    """The cycle-level gather pulls the latest AFK event + the bf-idle-tracker
    bucket id and produces the not-afk span the server bills from."""
    engine = _make_engine()
    engine._get_system_idle_seconds = lambda: 5.0
    engine.aw.get_latest_afk_event.return_value = _afk(
        NOW - timedelta(seconds=3660), 60, "afk"
    )
    buckets = [_bucket("aw-watcher-afk_host"), _bucket("bf-idle-tracker_host")]

    ev = engine._synthesize_for_stale_afk(buckets, now=NOW)

    assert ev is not None
    assert ev["data"]["status"] == "not-afk"
    assert ev["bucket_id"] == "bf-idle-tracker_host", "prefer the BetterFlow bucket"


def test_gather_returns_none_without_afk_buckets():
    engine = _make_engine()
    engine._get_system_idle_seconds = lambda: 5.0
    assert engine._synthesize_for_stale_afk([], now=NOW) is None


def test_engine_exposes_real_os_idle_clock():
    """Production wiring: SyncEngine must have a real OS-idle-clock method (not
    only the monkeypatched test stub) returning seconds or None — otherwise the
    synthesizer can never fire on a real machine."""
    engine = _make_engine()
    result = engine._get_system_idle_seconds()
    assert result is None or isinstance(result, (int, float))


def test_synth_id_is_stable_across_cycles_so_server_patches_in_place():
    """While the tracker stays frozen at the same point, the synthetic event's
    id must stay constant so the server upserts (patches the growing duration)
    instead of accumulating overlapping not-afk spans."""
    engine = _make_engine()
    engine._get_system_idle_seconds = lambda: 5.0
    frozen = _afk(NOW - timedelta(seconds=3660), 60, "afk")  # freeze point fixed

    first = engine._synthesize_active_afk_event(frozen, "b", now=NOW)
    later = engine._synthesize_active_afk_event(
        frozen, "b", now=NOW + timedelta(seconds=600)
    )

    assert first["id"] == later["id"], "stable id keyed on the freeze point"
    assert later["duration"] > first["duration"], "duration grows as freeze persists"
