"""Regression test: a call/meeting must not be marked as Idle.

Sachi (Windows, 2026-06-15) showed ~2h of "Idle" mid-day while actually in a
meeting. On Windows the agent has no in-process input watcher and no system-idle
fallback (`_get_system_idle_seconds` is macOS-only), so idle is decided purely
from the AFK watcher: any stretch with no keyboard/mouse for >= idle_pause_minutes
(default 20) trips the idle pause, and on resume one idle_time event paints the
whole span gray — even when a meeting app was focused the entire time.

Fix: `IdleManager.check_idle_status` consults the call/meeting detector (via
`sync_engine.is_in_call()`) and does not pause while a call is active. This test
drives the real `check_idle_status` with an AFK event well over threshold and
asserts: in a call -> no idle pause; not in a call -> idle pause as before.
"""

import sys
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

# The real tray module imports PIL at module load; stub it only if that import
# is broken in this environment (no-op in CI where PIL works). IdleManager
# imports TrayState lazily inside check_idle_status, so this only needs to
# satisfy that import.
try:  # pragma: no cover - environment dependent
    import src.ui.tray  # noqa: F401
except Exception:  # pragma: no cover
    _tray = types.ModuleType("src.ui.tray")

    class TrayState:  # minimal stand-in; identity is all the code needs
        SYNCING = "syncing"
        PAUSED = "paused"

    _tray.TrayState = TrayState
    sys.modules.setdefault("src.ui", types.ModuleType("src.ui"))
    sys.modules["src.ui"].tray = _tray
    sys.modules["src.ui.tray"] = _tray
    sys.modules.setdefault("ui", types.ModuleType("ui"))
    sys.modules["ui.tray"] = _tray

from src.config import Config
from src.idle_manager import IdleManager


def _make(idle_in_call: bool):
    """Build an IdleManager whose AFK bucket reports a 2h AFK event.

    Returns (idle_mgr, reschedule_mock, trigger_sync_mock, tray_mock).
    """
    config = Config()  # idle_pause_minutes defaults to 20 -> threshold 1200s

    afk_event = types.SimpleNamespace(
        status="afk",
        duration=2 * 3600.0,  # 2 hours AFK, well over the 20-min threshold
        timestamp=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    aw = Mock()
    aw.get_afk_buckets.return_value = [types.SimpleNamespace(id="afk-bucket")]
    aw.get_events.return_value = [afk_event]
    aw.get_latest_afk_event.return_value = afk_event

    sync_engine = Mock()
    sync_engine.is_paused = False
    sync_engine.is_private = False
    sync_engine.is_in_call.return_value = idle_in_call

    tray = Mock()
    idle_mgr = IdleManager(sync_engine, tray, aw, config)
    return idle_mgr, Mock(), Mock(), tray


def test_does_not_pause_idle_during_a_call():
    idle_mgr, reschedule, trigger_sync, tray = _make(idle_in_call=True)

    idle_mgr.check_idle_status(
        logged_in=True,
        is_on_break=False,
        reschedule=reschedule,
        trigger_sync=trigger_sync,
    )

    assert idle_mgr.idle_paused is False, "should not mark idle while in a call"
    reschedule.assert_not_called()
    tray.set_state.assert_not_called()


def test_pauses_idle_when_not_in_a_call():
    """Control: outside a call, a long AFK stretch still pauses as before."""
    idle_mgr, reschedule, trigger_sync, tray = _make(idle_in_call=False)

    idle_mgr.check_idle_status(
        logged_in=True,
        is_on_break=False,
        reschedule=reschedule,
        trigger_sync=trigger_sync,
    )

    assert idle_mgr.idle_paused is True, "long AFK outside a call should pause"
    reschedule.assert_called_once_with(idle_mgr._IDLE_SYNC_INTERVAL)


def _fake_input_watcher(last_input_secs_ago):
    """An input watcher whose last observed input was N seconds ago (or None)."""
    w = Mock()
    if last_input_secs_ago is None:
        w.get_last_input_at.return_value = None
    else:
        w.get_last_input_at.return_value = (
            datetime.now(timezone.utc) - timedelta(seconds=last_input_secs_ago)
        )
    return w


def test_blind_afk_tracker_does_not_pause_while_input_is_live():
    """Phantom-idle regression: AFK bucket says 'afk' (blind/stuck tracker) but
    the in-process input watcher saw a keystroke 5s ago. The user is typing —
    must NOT pause as idle even though the AFK event is well over threshold."""
    idle_mgr, reschedule, trigger_sync, tray = _make(idle_in_call=False)
    idle_mgr.input_watcher = _fake_input_watcher(last_input_secs_ago=5)

    idle_mgr.check_idle_status(
        logged_in=True,
        is_on_break=False,
        reschedule=reschedule,
        trigger_sync=trigger_sync,
    )

    assert idle_mgr.idle_paused is False, "live input must override a blind AFK bucket"
    reschedule.assert_not_called()
    tray.set_state.assert_not_called()
    # Short-circuited before even reading the AFK bucket.
    idle_mgr.aw.get_events.assert_not_called()


def test_recent_input_clears_an_existing_phantom_idle_pause():
    """If we somehow already entered idle and then the input watcher sees real
    input within the threshold, resume immediately (don't wait for the
    blind-tracker restart to flip the AFK bucket)."""
    idle_mgr, reschedule, trigger_sync, tray = _make(idle_in_call=False)
    idle_mgr.input_watcher = _fake_input_watcher(last_input_secs_ago=2)
    idle_start = datetime.now(timezone.utc) - timedelta(minutes=30)
    idle_mgr._idle_paused = True
    idle_mgr._idle_start = idle_start

    idle_mgr.check_idle_status(
        logged_in=True,
        is_on_break=False,
        reschedule=reschedule,
        trigger_sync=trigger_sync,
    )

    assert idle_mgr.idle_paused is False, "recent input must clear the idle pause"
    reschedule.assert_called_once_with(idle_mgr.config.sync.interval_seconds)
    trigger_sync.assert_called_once_with("idle_resume_sync")


def test_genuine_idle_still_pauses_when_input_is_old():
    """Control: no recent input (last keystroke older than the threshold) — the
    override must NOT fire, so a real idle stretch still pauses as before."""
    idle_mgr, reschedule, trigger_sync, tray = _make(idle_in_call=False)
    # idle_pause_minutes defaults to 20 (1200s); last input 2h ago is well past it.
    idle_mgr.input_watcher = _fake_input_watcher(last_input_secs_ago=2 * 3600)

    idle_mgr.check_idle_status(
        logged_in=True,
        is_on_break=False,
        reschedule=reschedule,
        trigger_sync=trigger_sync,
    )

    assert idle_mgr.idle_paused is True, "old input must not suppress genuine idle"
    reschedule.assert_called_once_with(idle_mgr._IDLE_SYNC_INTERVAL)


def test_no_input_watcher_falls_back_to_afk_path():
    """Windows/Linux have no in-process watcher (input_watcher is None) — the
    override is a no-op and idle detection works exactly as before."""
    idle_mgr, reschedule, trigger_sync, tray = _make(idle_in_call=False)
    assert idle_mgr.input_watcher is None  # default

    idle_mgr.check_idle_status(
        logged_in=True,
        is_on_break=False,
        reschedule=reschedule,
        trigger_sync=trigger_sync,
    )

    assert idle_mgr.idle_paused is True, "no watcher -> unchanged AFK-based idle"


def test_resumes_when_a_call_starts_during_an_existing_idle_pause():
    """A call that begins AFTER the idle pause must resume tracking.

    The is_in_call guard only blocks ENTERING idle. If the user was already
    idle-paused (stepped away, no call) and then joins a meeting but stays AFK
    (listening, no keyboard), nothing cleared the pause — the whole call span
    was painted Idle. This is the symmetric exit: still AFK, but now in a call.
    """
    idle_mgr, reschedule, trigger_sync, tray = _make(idle_in_call=True)
    # Simulate "already idle-paused before the call started" — both the flag and
    # the start timestamp, as the real pause path sets them together, so the
    # idle_time event send is actually exercised.
    idle_start = datetime.now(timezone.utc) - timedelta(hours=2)
    idle_mgr._idle_paused = True
    idle_mgr._idle_start = idle_start

    idle_mgr.check_idle_status(
        logged_in=True,
        is_on_break=False,
        reschedule=reschedule,
        trigger_sync=trigger_sync,
    )

    assert idle_mgr.idle_paused is False, "a call starting mid-idle must resume"
    reschedule.assert_called_once_with(idle_mgr.config.sync.interval_seconds)
    trigger_sync.assert_called_once_with("call_resume_sync")
    # The pre-call idle stretch is still recorded as idle (send_event=True path).
    idle_mgr.sync_engine.send_idle_event.assert_called_once_with(idle_start)

    from src.ui.tray import TrayState

    tray.set_state.assert_called_once_with(TrayState.SYNCING)


def test_stale_afk_bucket_does_not_pause_when_latest_betterflow_bucket_is_active():
    """The live idle check must not pick a stale vanilla AFK bucket first."""
    config = Config()
    active_event = types.SimpleNamespace(
        status="not-afk",
        duration=60.0,
        timestamp=datetime.now(timezone.utc) - timedelta(seconds=60),
    )
    aw = Mock()
    aw.get_latest_afk_event.return_value = active_event

    sync_engine = Mock()
    sync_engine.is_paused = False
    sync_engine.is_private = False
    sync_engine.is_in_call.return_value = False

    tray = Mock()
    idle_mgr = IdleManager(sync_engine, tray, aw, config)

    idle_mgr.check_idle_status(
        logged_in=True,
        is_on_break=False,
        reschedule=Mock(),
        trigger_sync=Mock(),
    )

    assert idle_mgr.idle_paused is False
    tray.set_state.assert_not_called()
