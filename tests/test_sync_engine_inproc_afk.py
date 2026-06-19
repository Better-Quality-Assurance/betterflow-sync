from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from src.config import Config
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.afk_source import AfkSource
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.sync_engine import SyncEngine


def _engine(in_process_afk: bool):
    cfg = Config()
    cfg.sync.in_process_afk = in_process_afk
    return SyncEngine(aw=Mock(), bf=Mock(), queue=Mock(), config=cfg,
                      activity_analyzer=Mock(spec=ActivityAnalyzer),
                      time_tracker=Mock(spec=DailyTimeTracker))


def test_inproc_afk_events_built_for_uploaded_range():
    eng = _engine(True)
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: 5.0)
    now = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)
    eng.afk_source.record_sample(now - timedelta(seconds=30))
    eng.afk_source.record_sample(now)
    eng._build_inproc_afk(now)  # first call seeds the checkpoint at now, returns []
    # User stays active — a fresh input pushes the finalize point past the
    # checkpoint so the settled slice uploads.
    eng.afk_source.record_sample(now + timedelta(seconds=30))
    events = eng._build_inproc_afk(now + timedelta(seconds=30))
    assert events and all(e["bucket_id"] == "bf-afk-inproc_host" for e in events)


def test_external_afk_bucket_skipped_when_inproc_active():
    eng = _engine(True)
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: 5.0)
    assert eng._should_skip_external_afk() is True


def test_external_afk_bucket_used_when_flag_off():
    eng = _engine(False)
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: 5.0)
    assert eng._should_skip_external_afk() is False


def test_inproc_inactive_when_clock_unavailable():
    eng = _engine(True)
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: None)  # Linux
    assert eng._should_skip_external_afk() is False


def test_inproc_inactive_when_no_source_wired():
    eng = _engine(True)
    assert eng._should_skip_external_afk() is False
