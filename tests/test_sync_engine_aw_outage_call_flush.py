"""Regression test: an ActivityWatch outage mid-call must not leave the call
detector stuck "in a call" forever.

sync() early-returns the moment aw.is_running() is False, which is BEFORE the
end-of-sync flush that normally finalizes an in-progress call. So if AW crashes
while the user is in a meeting, every subsequent cycle bails early, the detector
keeps is_in_call() == True, and IdleManager's "don't pause while in a call"
guard suppresses idle for the whole outage — painting the post-call AFK stretch
as worked time. The fix flushes the detector in the AW-down branch so the stale
call state can't carry forward (the observed portion is still recorded if the
backend is reachable).
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src.config import Config
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine


def _build_engine() -> SyncEngine:
    tmp = Path(tempfile.mkdtemp())
    engine = SyncEngine(
        aw=Mock(),
        bf=Mock(),
        queue=OfflineQueue(db_path=tmp / "q.db", max_size=1000),
        config=Config(),  # call_detection.enabled defaults True
        time_tracker=Mock(),
    )
    # Skip the first-sync server-config fetch (its Mock return isn't a dict).
    engine._config_fetched = True
    # A well-formed send result so _send_events doesn't choke on a bare Mock.
    engine.bf.send_events.return_value = SimpleNamespace(
        success=True, events_synced=1, accepted_ids=[], error=None
    )
    return engine


def _enter_call(engine: SyncEngine) -> None:
    """Drive the detector into an active 2-minute Teams call."""
    t0 = datetime(2026, 6, 17, 13, 0, 0, tzinfo=timezone.utc)
    engine._call_detector.process_event("Microsoft Teams", "Meeting with X", None, t0, 60)
    engine._call_detector.process_event(
        "Microsoft Teams", "Meeting with X", None, t0 + timedelta(minutes=2), 60
    )
    assert engine.is_in_call(), "precondition: detector should report in-call"


def test_aw_outage_clears_stuck_call_state_backend_unreachable():
    engine = _build_engine()
    _enter_call(engine)

    engine.aw.is_running.return_value = False
    engine.bf.is_reachable.return_value = False  # offline → don't try to send

    engine.sync()

    assert engine.is_in_call() is False, (
        "an AW outage mid-call must flush the detector so idle isn't suppressed"
    )


def test_aw_outage_clears_stuck_call_state_and_records_when_reachable():
    engine = _build_engine()
    _enter_call(engine)

    engine.aw.is_running.return_value = False
    engine.bf.is_reachable.return_value = True  # online → the observed call is sent

    engine.sync()

    assert engine.is_in_call() is False, "stale call state cleared on AW outage"
    # The observed 2-minute call portion is recorded (>= min_call_duration).
    assert engine.bf.send_events.called, "the call up to the outage should be sent"
