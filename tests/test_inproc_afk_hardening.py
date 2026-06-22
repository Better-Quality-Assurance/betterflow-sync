"""In-process AFK path hardening — the six audit findings (2026-06-22).

Each test fails against the pre-fix implementation and passes after the fix,
exercising real AfkSource / SyncEngine behaviour (no phantom mocks).

A. available() flap → sticky latch + aw_manager reconciliation
B. checkpoint advanced before send → permanent loss on terminal queue failure
C. pause/sleep gap > retention → mis-billed span on resume (re-seed)
D. wall-clock backward step → stalled/scrambled checkpoint (re-seed)
E. blind OS idle clock → silent idle-billing (hold + report)
F. synthetic id truncated to whole seconds → intra-batch collision
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from src.config import Config
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.afk_source import AfkSource
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.sync_engine import SyncEngine, SyncStats

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


def _engine(in_process_afk=True, idle=5.0, retention=7200.0):
    cfg = Config()
    cfg.sync.in_process_afk = in_process_afk
    eng = SyncEngine(aw=Mock(), bf=Mock(), queue=Mock(), config=cfg,
                     activity_analyzer=Mock(spec=ActivityAnalyzer),
                     time_tracker=Mock(spec=DailyTimeTracker))
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: idle,
                               retention_seconds=retention)
    return eng


# ── F: synthetic id sub-second precision ──────────────────────────────────────

def test_event_ids_distinct_within_same_second():
    src = AfkSource(600, "host", idle_clock=lambda: 5.0)
    a = src._event(T0, 0.3, "afk", None)
    b = src._event(T0 + timedelta(milliseconds=400), 0.3, "not-afk", None)
    assert a["id"] != b["id"], "two spans starting in the same second collide"


# ── A: available() sticky latch ───────────────────────────────────────────────

def test_available_latches_true_through_transient_failure():
    seq = iter([5.0, None, None, 5.0])
    src = AfkSource(600, "host", idle_clock=lambda: next(seq))
    assert src.available() is True   # first read OK → capability confirmed
    assert src.available() is True   # transient None must NOT revoke it
    assert src.available() is True


def test_available_false_until_first_success():
    src = AfkSource(600, "host", idle_clock=lambda: None)  # Linux / never readable
    assert src.available() is False
    assert src.available() is False


# ── E: blind-clock failure counter ────────────────────────────────────────────

def test_consecutive_clock_failures_tracked_and_reset():
    seq = iter([None, None, 5.0, None])
    src = AfkSource(600, "host", idle_clock=lambda: next(seq))
    src.record_sample(T0)
    assert src.consecutive_clock_failures == 1
    src.record_sample(T0)
    assert src.consecutive_clock_failures == 2
    src.record_sample(T0)
    assert src.consecutive_clock_failures == 0  # success resets
    src.record_sample(T0)
    assert src.consecutive_clock_failures == 1


def test_retention_seconds_exposed():
    src = AfkSource(600, "host", retention_seconds=1234.0, idle_clock=lambda: 5.0)
    assert src.retention_seconds == 1234.0


# ── B: checkpoint held back until send confirmed ──────────────────────────────

def test_inproc_checkpoint_held_until_send_confirmed():
    eng = _engine()
    eng.afk_source.record_sample(T0 - timedelta(seconds=30))
    eng.afk_source.record_sample(T0)
    eng._build_inproc_afk(T0)              # seed → checkpoint == T0
    seed_cp = eng._afk_inproc_checkpoint
    eng.afk_source.record_sample(T0 + timedelta(seconds=30))
    events = eng._build_inproc_afk(T0 + timedelta(seconds=30))
    assert events
    # Not committed yet — the send hasn't been confirmed.
    assert eng._afk_inproc_checkpoint == seed_cp

    queued = SyncStats()
    queued.queued_bucket_ids.add("bf-afk-inproc_host")
    eng._commit_inproc_afk_checkpoint(queued)
    assert eng._afk_inproc_checkpoint == seed_cp, "must stay held when queued"

    # Next cycle rebuilds the same span (stable ids → idempotent) and the send
    # succeeds this time, so the checkpoint finally advances.
    events2 = eng._build_inproc_afk(T0 + timedelta(seconds=30))
    assert events2, "held checkpoint means the span is rebuilt next cycle"
    ok = SyncStats()
    eng._commit_inproc_afk_checkpoint(ok)
    assert eng._afk_inproc_checkpoint > seed_cp, "commit once the send succeeded"


def test_inproc_afk_active_public_property():
    # Public surface so main.py can reconcile aw_manager's flag each cycle (A).
    assert _engine(in_process_afk=True).inproc_afk_active is True
    assert _engine(in_process_afk=False).inproc_afk_active is False


# ── C: pause/sleep gap beyond retention → re-seed, don't reconstruct ──────────

def test_reseed_when_gap_exceeds_retention():
    eng = _engine(retention=7200.0)
    eng._afk_inproc_checkpoint = T0 - timedelta(hours=5)   # paused/asleep 5h
    eng.afk_source.record_sample(T0)
    events = eng._build_inproc_afk(T0)
    assert events == [], "must not reconstruct an unobserved multi-hour window"
    assert eng._afk_inproc_checkpoint == T0, "checkpoint re-seeded to now"


# ── D: wall-clock backward step → re-seed, don't stall ────────────────────────

def test_reseed_on_backward_clock_step():
    eng = _engine()
    eng._afk_inproc_checkpoint = T0 + timedelta(minutes=10)  # checkpoint ahead of now
    eng.afk_source.record_sample(T0)
    events = eng._build_inproc_afk(T0)
    assert events == []
    assert eng._afk_inproc_checkpoint == T0, "re-seeded to now, not stuck in the future"


# ── E: blind clock → hold + report, don't backdate real work as idle ──────────

def test_blind_clock_holds_and_reports():
    eng = _engine()
    eng.error_reporter = Mock()
    eng.afk_source.record_sample(T0)
    eng._build_inproc_afk(T0)              # seed
    seed_cp = eng._afk_inproc_checkpoint
    eng.afk_source._consecutive_clock_failures = 5   # clock blind for several cycles
    events = eng._build_inproc_afk(T0 + timedelta(minutes=30))
    assert events == [], "must not finalize-afk over an unobserved window"
    assert eng._afk_inproc_checkpoint == seed_cp, "checkpoint held while blind"
    assert eng.error_reporter.capture.called, "blind clock surfaced to ops"
