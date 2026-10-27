"""A machine that woke unattended must not stay "asleep" once a person works (#288).

On Windows a timer wake sends only PBT_APMRESUMEAUTOMATIC, which no longer ends
the asleep state. While asleep the sync engine is paused, and the idle check
returns early for a paused engine, so input alone never resumed tracking. If
Windows also never sent PBT_APMRESUMESUSPEND (unverified on Modern Standby) and
there is no lock screen, the user would work unbilled. Input after the sleep
began therefore ends the asleep state.
"""

import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src.system_event_handler import SystemEventHandler
from src.main import SyncCoordinator


def _handler(wake_on_user_input=True):
    coordinator = MagicMock()
    coordinator.is_on_break = False
    sync_engine = MagicMock()
    sync_engine.is_private = False
    return SystemEventHandler(
        sync_engine=sync_engine, tray=MagicMock(), coordinator=coordinator,
        reminder_manager=MagicMock(), bf=MagicMock(), aw=MagicMock(),
        pause_state_lock=threading.RLock(), shutdown_fn=MagicMock(), wake_on_user_input=wake_on_user_input,
    )


def test_input_after_the_sleep_began_ends_the_asleep_state():
    h = _handler()
    h.on_system_sleep()
    h.sync_engine.resume.reset_mock()
    h.wake_if_user_input_since_sleep(datetime.now(timezone.utc) + timedelta(seconds=5))
    h.sync_engine.resume.assert_called_once()
    assert h._sleep_start is None


def test_input_from_before_the_sleep_does_not_end_it():
    h = _handler()
    h.on_system_sleep()
    h.sync_engine.resume.reset_mock()
    h.wake_if_user_input_since_sleep(datetime.now(timezone.utc) - timedelta(minutes=5))
    h.sync_engine.resume.assert_not_called()
    assert h._sleep_start is not None


def test_no_input_ever_seen_does_not_end_it():
    h = _handler()
    h.on_system_sleep()
    h.wake_if_user_input_since_sleep(None)
    assert h._sleep_start is not None


def test_awake_machine_is_left_alone():
    h = _handler()
    h.wake_if_user_input_since_sleep(datetime.now(timezone.utc))
    h.sync_engine.resume.assert_not_called()


def test_macos_handler_ignores_input_because_a_wake_always_arrives():
    h = _handler(wake_on_user_input=False)
    h.on_system_sleep()
    h.wake_if_user_input_since_sleep(datetime.now(timezone.utc) + timedelta(seconds=5))
    assert h._sleep_start is not None


def test_the_idle_tick_feeds_the_last_input_to_the_wake_hook():
    t = datetime.now(timezone.utc)
    stub = MagicMock()
    stub.wake_if_user_input = MagicMock()
    stub.idle_mgr.input_watcher.get_last_input_at.return_value = t
    SyncCoordinator._check_idle_status(stub)
    stub.wake_if_user_input.assert_called_once_with(t)
    stub.idle_mgr.check_idle_status.assert_called_once()


def test_no_hook_wired_still_runs_the_idle_check():
    stub = MagicMock()
    stub.wake_if_user_input = None
    SyncCoordinator._check_idle_status(stub)
    stub.idle_mgr.check_idle_status.assert_called_once()
