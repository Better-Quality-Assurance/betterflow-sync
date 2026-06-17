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


def test_resumes_when_a_call_starts_during_an_existing_idle_pause():
    """A call that begins AFTER the idle pause must resume tracking.

    The is_in_call guard only blocks ENTERING idle. If the user was already
    idle-paused (stepped away, no call) and then joins a meeting but stays AFK
    (listening, no keyboard), nothing cleared the pause — the whole call span
    was painted Idle. This is the symmetric exit: still AFK, but now in a call.
    """
    idle_mgr, reschedule, trigger_sync, tray = _make(idle_in_call=True)
    # Simulate "already idle-paused before the call started".
    idle_mgr._idle_paused = True

    idle_mgr.check_idle_status(
        logged_in=True,
        is_on_break=False,
        reschedule=reschedule,
        trigger_sync=trigger_sync,
    )

    assert idle_mgr.idle_paused is False, "a call starting mid-idle must resume"
    reschedule.assert_called_once_with(idle_mgr.config.sync.interval_seconds)
    trigger_sync.assert_called_once_with("call_resume_sync")
