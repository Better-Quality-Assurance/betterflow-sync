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
from unittest.mock import Mock, patch

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
    sync_engine.is_active_dev_session.return_value = False
    sync_engine.is_mic_meeting_active.return_value = False

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
    trigger_sync.assert_called_once_with("engaged_resume_sync")
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
    sync_engine.is_active_dev_session.return_value = False
    sync_engine.is_mic_meeting_active.return_value = False

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


def _make_with_afk_event(afk_event, *, in_call=False):
    """Build an IdleManager whose AFK bucket returns the given event."""
    config = Config()
    aw = Mock()
    aw.get_afk_buckets.return_value = [types.SimpleNamespace(id="afk-bucket")]
    aw.get_events.return_value = [afk_event]
    aw.get_latest_afk_event.return_value = afk_event
    sync_engine = Mock()
    sync_engine.is_paused = False
    sync_engine.is_private = False
    sync_engine.is_in_call.return_value = in_call
    sync_engine.is_active_dev_session.return_value = False
    sync_engine.is_mic_meeting_active.return_value = False
    tray = Mock()
    idle_mgr = IdleManager(sync_engine, tray, aw, config)
    return idle_mgr, Mock(), Mock(), tray


def test_stale_afk_event_does_not_pause_when_os_idle_is_low():
    """Frozen bf-idle-tracker: its latest event is 'afk' over threshold but it
    ENDED ~30 min ago (the tracker stopped heartbeating when the user came
    back). With no in-process input watcher (Windows) and the OS idle clock
    showing recent activity, the user must NOT be paused. Pre-fix this pinned
    active users as Idle indefinitely — the bug Sachi/Emilian reported on 1.5.53.
    """
    now = datetime.now(timezone.utc)
    stale_afk = types.SimpleNamespace(
        status="afk",
        duration=25 * 60.0,                      # over the 20-min threshold
        timestamp=now - timedelta(minutes=55),   # end = now - 30min -> stale
    )
    idle_mgr, reschedule, trigger_sync, tray = _make_with_afk_event(stale_afk)
    assert idle_mgr.input_watcher is None  # Windows: no in-process watcher

    with patch.object(IdleManager, "_get_system_idle_seconds", return_value=5.0):
        idle_mgr.check_idle_status(
            logged_in=True,
            is_on_break=False,
            reschedule=reschedule,
            trigger_sync=trigger_sync,
        )

    assert idle_mgr.idle_paused is False, "stale afk + recent OS input must not pause"
    reschedule.assert_not_called()


def test_stale_afk_event_still_detects_genuine_idle_via_os_clock():
    """A frozen tracker must not COST us idle detection either: when the OS idle
    clock shows the user genuinely away past threshold, we still pause even
    though the stale AFK event is ignored."""
    now = datetime.now(timezone.utc)
    stale = types.SimpleNamespace(
        status="not-afk",
        duration=10.0,
        timestamp=now - timedelta(minutes=55),   # stale -> ignored
    )
    idle_mgr, reschedule, trigger_sync, tray = _make_with_afk_event(stale)

    with patch.object(
        IdleManager, "_get_system_idle_seconds",
        return_value=idle_mgr._idle_pause_threshold + 60,
    ):
        idle_mgr.check_idle_status(
            logged_in=True,
            is_on_break=False,
            reschedule=reschedule,
            trigger_sync=trigger_sync,
        )

    assert idle_mgr.idle_paused is True, "genuine OS idle should still pause via fallback"


def test_current_afk_event_is_trusted_and_pauses():
    """Regression guard: a CURRENT 'afk' event (still heartbeating — end ~now)
    over threshold is trusted and pauses, exactly as before the freshness fix."""
    now = datetime.now(timezone.utc)
    current_afk = types.SimpleNamespace(
        status="afk",
        duration=25 * 60.0,
        timestamp=now - timedelta(minutes=25),   # end ~= now -> current
    )
    idle_mgr, reschedule, trigger_sync, tray = _make_with_afk_event(current_afk)

    idle_mgr.check_idle_status(
        logged_in=True,
        is_on_break=False,
        reschedule=reschedule,
        trigger_sync=trigger_sync,
    )

    assert idle_mgr.idle_paused is True, "a live afk event over threshold should still pause"


def test_afk_event_with_none_duration_falls_back_not_crash():
    """A malformed AFK event (duration=None — AW returned null) must not raise:
    it's treated as not-current, so the code reaches the OS-idle fallback and
    does not pause an active user. Pre-fix this raised TypeError before the
    fallback (swallowed by the outer handler -> idle check silently no-op'd)."""
    bad = types.SimpleNamespace(
        status="afk",
        duration=None,
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=30),
    )
    idle_mgr, reschedule, trigger_sync, tray = _make_with_afk_event(bad)
    with patch.object(IdleManager, "_get_system_idle_seconds", return_value=5.0) as sysidle:
        idle_mgr.check_idle_status(
            logged_in=True,
            is_on_break=False,
            reschedule=reschedule,
            trigger_sync=trigger_sync,
        )
    assert idle_mgr.idle_paused is False
    # Post-fix reaches the OS-idle fallback; pre-fix raised before it.
    sysidle.assert_called()


def test_idle_start_clamped_to_wake_after_suspend():
    """The suspend-carving bug: bf-idle-tracker resumes heartbeating its
    pre-suspend 'afk' event on wake WITHOUT resetting its start time, so the
    "current" AFK event's timestamp is a keystroke from BEFORE the lid
    closed even though its duration now reaches ~now. Pre-fix, check_idle_
    status backdates idle_start to that pre-suspend timestamp; the idle_time
    event sent on resume then reaches back across the suspend and re-carves
    real work the user logged before closing the lid.

    Fix: record_wake() anchors the wake instant; idle_start must clamp to it.
    """
    now = datetime.now(timezone.utc)
    wake_ts = now - timedelta(minutes=2)  # woke 2 minutes ago
    pre_suspend_keystroke = now - timedelta(hours=3)  # last input before lid close

    # AFK event "current" (heartbeating through to ~now) but anchored to the
    # pre-suspend keystroke — exactly what a resumed tracker reports.
    afk_event = types.SimpleNamespace(
        status="afk",
        duration=(now - pre_suspend_keystroke).total_seconds(),
        timestamp=pre_suspend_keystroke,
    )
    idle_mgr, reschedule, trigger_sync, tray = _make_with_afk_event(afk_event)
    idle_mgr.record_wake(wake_ts)

    idle_mgr.check_idle_status(
        logged_in=True,
        is_on_break=False,
        reschedule=reschedule,
        trigger_sync=trigger_sync,
    )

    assert idle_mgr.idle_paused is True
    assert idle_mgr._idle_start is not None
    assert idle_mgr._idle_start >= wake_ts, (
        "idle_start must never reach back before the recorded wake instant"
    )
    assert idle_mgr._idle_start != pre_suspend_keystroke, (
        "idle_start must not stay pinned to the pre-suspend keystroke"
    )


def test_idle_start_clamped_to_wake_via_os_idle_clock_fallback():
    """Same suspend-carving risk on the OS-idle-clock fallback path (Windows/
    no in-process watcher scenario, or a stale-AFK fallback): the OS idle
    clock can also report elapsed time stretching back before a suspend.
    idle_start must still clamp to the last recorded wake."""
    now = datetime.now(timezone.utc)
    wake_ts = now - timedelta(minutes=1)
    # No AFK bucket signal at all -> falls back to the OS idle clock, which
    # (pre-fix) would compute idle_start = now - system_idle, reaching back
    # to well before the wake.
    idle_mgr, reschedule, trigger_sync, tray = _make_with_afk_event(None)
    idle_mgr.aw.get_latest_afk_event.return_value = None
    idle_mgr.record_wake(wake_ts)

    system_idle_seconds = 3 * 3600.0  # 3h "idle" per OS clock, from before sleep
    with patch.object(
        IdleManager, "_get_system_idle_seconds", return_value=system_idle_seconds
    ):
        idle_mgr.check_idle_status(
            logged_in=True,
            is_on_break=False,
            reschedule=reschedule,
            trigger_sync=trigger_sync,
        )

    assert idle_mgr.idle_paused is True
    assert idle_mgr._idle_start >= wake_ts, (
        "OS-idle-clock-derived idle_start must also clamp to the last wake"
    )


def test_at_desk_idle_unchanged_without_a_wake_recorded():
    """Control: no suspend/wake happened (record_wake never called) — idle_start
    is exactly the AFK event's own timestamp, unclamped, as before this fix."""
    now = datetime.now(timezone.utc)
    afk_start = now - timedelta(minutes=25)
    afk_event = types.SimpleNamespace(
        status="afk",
        duration=25 * 60.0,
        timestamp=afk_start,
    )
    idle_mgr, reschedule, trigger_sync, tray = _make_with_afk_event(afk_event)
    assert idle_mgr._last_wake_ts is None  # no wake recorded this session

    idle_mgr.check_idle_status(
        logged_in=True,
        is_on_break=False,
        reschedule=reschedule,
        trigger_sync=trigger_sync,
    )

    assert idle_mgr.idle_paused is True
    assert idle_mgr._idle_start == afk_start, (
        "at-desk idle with no intervening suspend must be unaffected by the clamp"
    )


def test_at_desk_idle_unchanged_when_idle_start_is_after_an_old_wake():
    """A wake recorded much earlier in the session must not distort a later,
    unrelated at-desk idle stretch — the clamp is a max(), a no-op once
    idle_start already falls after the last wake."""
    now = datetime.now(timezone.utc)
    old_wake = now - timedelta(hours=5)  # woke up 5h ago, long since active
    afk_start = now - timedelta(minutes=25)  # genuine idle stretch, much later
    afk_event = types.SimpleNamespace(
        status="afk",
        duration=25 * 60.0,
        timestamp=afk_start,
    )
    idle_mgr, reschedule, trigger_sync, tray = _make_with_afk_event(afk_event)
    idle_mgr.record_wake(old_wake)

    idle_mgr.check_idle_status(
        logged_in=True,
        is_on_break=False,
        reschedule=reschedule,
        trigger_sync=trigger_sync,
    )

    assert idle_mgr.idle_paused is True
    assert idle_mgr._idle_start == afk_start, (
        "idle_start already after the last wake must pass through unclamped"
    )


def test_clamped_idle_start_is_what_gets_sent_on_resume():
    """End-to-end: the idle_time event actually emitted to the server (via
    clear_idle_pause -> send_idle_event) carries the CLAMPED start, not the
    pre-suspend keystroke — this is the payroll-facing half of the fix."""
    now = datetime.now(timezone.utc)
    wake_ts = now - timedelta(minutes=2)
    pre_suspend_keystroke = now - timedelta(hours=3)
    afk_event = types.SimpleNamespace(
        status="afk",
        duration=(now - pre_suspend_keystroke).total_seconds(),
        timestamp=pre_suspend_keystroke,
    )
    idle_mgr, reschedule, trigger_sync, tray = _make_with_afk_event(afk_event)
    idle_mgr.record_wake(wake_ts)

    idle_mgr.check_idle_status(
        logged_in=True,
        is_on_break=False,
        reschedule=reschedule,
        trigger_sync=trigger_sync,
    )
    idle_mgr.clear_idle_pause(send_event=True)

    idle_mgr.sync_engine.send_idle_event.assert_called_once()
    sent_start = idle_mgr.sync_engine.send_idle_event.call_args[0][0]
    assert sent_start >= wake_ts
    assert sent_start != pre_suspend_keystroke


def test_stale_afk_with_no_os_idle_signal_does_not_pause():
    """Stale AFK + no OS idle clock (Linux, or a Windows API failure -> None):
    is_afk stays False and the user is not paused. Guards the None-fallback
    branch so a future change to the default can't silently re-introduce idle."""
    stale = types.SimpleNamespace(
        status="afk",
        duration=25 * 60.0,
        timestamp=datetime.now(timezone.utc) - timedelta(minutes=55),  # stale
    )
    idle_mgr, reschedule, trigger_sync, tray = _make_with_afk_event(stale)
    with patch.object(IdleManager, "_get_system_idle_seconds", return_value=None):
        idle_mgr.check_idle_status(
            logged_in=True,
            is_on_break=False,
            reschedule=reschedule,
            trigger_sync=trigger_sync,
        )
    assert idle_mgr.idle_paused is False


def test_idle_start_clamped_to_last_engagement():
    """A pause entered right after a hands-off meeting must not backdate
    idle_start into the meeting: the AFK stream credited that hour as active,
    and the idle_time span sent on resume would carve the same hour back out."""
    config = Config()
    meeting_end = datetime.now(timezone.utc) - timedelta(minutes=5)

    sync_engine = Mock()
    sync_engine.is_paused = False
    sync_engine.is_private = False
    sync_engine.is_in_call.return_value = False
    sync_engine.is_active_dev_session.return_value = False
    sync_engine.is_mic_meeting_active.return_value = False
    mgr = IdleManager(sync_engine, Mock(), Mock(), config)

    # Engagement observed until meeting_end…
    with mgr._state_lock:
        mgr._last_engaged_ts = meeting_end
    # …then an idle pause whose AFK evidence reaches back before it.
    idle_start = meeting_end - timedelta(minutes=45)
    assert mgr._clamp_to_last_engagement(idle_start) == meeting_end
    # At-desk idle after the engagement is untouched.
    later = meeting_end + timedelta(minutes=10)
    assert mgr._clamp_to_last_engagement(later) == later


def test_engaged_check_stamps_last_engaged_ts():
    config = Config()
    sync_engine = Mock()
    sync_engine.is_in_call.return_value = True
    mgr = IdleManager(sync_engine, Mock(), Mock(), config)
    assert mgr._last_engaged_ts is None
    assert mgr._is_engaged_without_input() is True
    assert mgr._last_engaged_ts is not None
