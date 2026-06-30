"""Tests for foreground-CPU activity detection and its AFK integration."""

from datetime import datetime, timedelta, timezone

from src.sync.afk_source import AfkSource
from src.sync.foreground_activity import (
    ForegroundActivityDetector,
    ForegroundSample,
)

T0 = datetime(2026, 6, 19, 12, 0, 0, tzinfo=timezone.utc)


class FakeProbe:
    """Returns a scripted sample each call (last value repeats)."""

    def __init__(self, samples):
        self._samples = list(samples)
        self._i = 0

    def sample(self) -> ForegroundSample:
        s = self._samples[min(self._i, len(self._samples) - 1)]
        self._i += 1
        return s


def _detector(samples, **kw):
    kw.setdefault("cpu_threshold_percent", 15.0)
    kw.setdefault("max_credit_seconds", 1200.0)
    kw.setdefault("min_session_seconds", 30.0)
    return ForegroundActivityDetector(hostname="host", probe=FakeProbe(samples), **kw)


def _active(cpu=50.0, pid=42, app="Claude Code"):
    return ForegroundSample(pid=pid, app=app, cpu_percent=cpu)


# -- credit eligibility -------------------------------------------------------

def test_busy_foreground_with_recent_input_is_credited():
    det = _detector([_active()])
    det.observe(T0, last_real_input_at=T0 - timedelta(seconds=5))
    assert det.is_active() is True
    assert det.get_last_active_at(None, T0) == T0


def test_below_threshold_cpu_is_not_credited():
    det = _detector([_active(cpu=5.0)])  # under 15%
    det.observe(T0, last_real_input_at=T0 - timedelta(seconds=5))
    assert det.is_active() is False
    assert det.get_last_active_at(None, T0) is None


def test_unknown_cpu_is_not_credited():
    # First sighting of a pid returns cpu_percent=None (counter seeding) — must
    # not be read as active.
    det = _detector([ForegroundSample(pid=42, app="x", cpu_percent=None)])
    det.observe(T0, last_real_input_at=T0 - timedelta(seconds=5))
    assert det.is_active() is False


def test_no_foreground_pid_is_not_credited():
    det = _detector([ForegroundSample(pid=None, app="", cpu_percent=None)])
    det.observe(T0, last_real_input_at=T0 - timedelta(seconds=5))
    assert det.is_active() is False


# -- human-presence anchoring (the anti-fraud cap) ----------------------------

def test_no_real_input_ever_is_never_credited():
    det = _detector([_active()])
    det.observe(T0, last_real_input_at=None)
    assert det.is_active() is False
    assert det.get_last_active_at(None, T0) is None


def test_credit_lapses_past_the_cap_without_input():
    det = _detector([_active()], max_credit_seconds=1200.0)
    # 25 min since last real input, cap is 20 min -> no longer eligible.
    now = T0 + timedelta(minutes=25)
    det.observe(now, last_real_input_at=T0)
    assert det.is_active() is False


def test_credit_within_the_cap_is_allowed():
    det = _detector([_active()], max_credit_seconds=1200.0)
    now = T0 + timedelta(minutes=19)  # within 20-min cap
    det.observe(now, last_real_input_at=T0)
    assert det.is_active() is True


def test_real_input_resets_the_cap_window():
    det = _detector([_active(), _active()], max_credit_seconds=600.0)
    # First sample at 9 min past input: eligible.
    det.observe(T0 + timedelta(minutes=9), last_real_input_at=T0)
    assert det.is_active() is True
    # User typed again; now only 1 min past the fresh input -> still eligible.
    t2 = T0 + timedelta(minutes=20)
    det.observe(t2, last_real_input_at=t2 - timedelta(minutes=1))
    assert det.is_active() is True


# -- session span emission ----------------------------------------------------

def test_session_span_emitted_on_natural_end_when_long_enough():
    det = _detector(
        [_active(cpu=80.0), _active(cpu=80.0), ForegroundSample(None, "", None)],
        min_session_seconds=30.0,
    )
    det.observe(T0, last_real_input_at=T0)  # open
    assert det.observe(T0 + timedelta(seconds=60), last_real_input_at=T0 + timedelta(seconds=60)) is None
    # Foreground gone -> session closes and emits a span.
    span = det.observe(T0 + timedelta(seconds=90), last_real_input_at=T0 + timedelta(seconds=90))
    assert span is not None
    assert span["bucket_type"] == "dev-session"
    assert span["data"]["app"] == "Claude Code"
    assert span["data"]["peak_cpu_percent"] == 80.0
    assert span["duration"] == 60.0  # T0 -> T0+60 (last active instant)
    assert span["id"] == f"devsession_Claude Code_{int(T0.timestamp())}"
    assert det.is_active() is False


def test_short_session_is_discarded():
    det = _detector([_active(), ForegroundSample(None, "", None)], min_session_seconds=30.0)
    det.observe(T0, last_real_input_at=T0)  # open, 0s so far
    span = det.observe(T0 + timedelta(seconds=10), last_real_input_at=T0)  # closes at 0s duration
    assert span is None


def test_snapshot_emits_open_session_without_closing():
    det = _detector([_active(cpu=40.0)], min_session_seconds=30.0)
    det.observe(T0, last_real_input_at=T0)
    det.observe(T0 + timedelta(seconds=60), last_real_input_at=T0 + timedelta(seconds=60))
    snap = det.snapshot()
    assert snap is not None
    assert snap["duration"] == 60.0
    assert det.is_active() is True  # still open after snapshot
    # Same deterministic id as the eventual final span -> server upserts one row.
    assert snap["id"] == f"devsession_Claude Code_{int(T0.timestamp())}"


def test_snapshot_none_before_min_duration():
    det = _detector([_active()], min_session_seconds=30.0)
    det.observe(T0, last_real_input_at=T0)
    assert det.snapshot() is None  # 0s so far


def test_flush_closes_open_session():
    det = _detector([_active(cpu=40.0)], min_session_seconds=30.0)
    det.observe(T0, last_real_input_at=T0)
    det.observe(T0 + timedelta(seconds=60), last_real_input_at=T0 + timedelta(seconds=60))
    span = det.flush()
    assert span is not None and span["duration"] == 60.0
    assert det.is_active() is False


def test_probe_failure_closes_session_and_never_crashes():
    class Boom:
        def sample(self):
            raise RuntimeError("probe died")
    det = ForegroundActivityDetector(hostname="h", probe=Boom())
    # No open session: returns None, no raise.
    assert det.observe(T0, last_real_input_at=T0) is None
    assert det.is_active() is False


# -- AfkSource integration ----------------------------------------------------

def test_afk_source_folds_in_activity_source_credit():
    """A foreground session keeps last_input_at fresh in the uploaded AFK
    stream even though the OS idle clock shows a long idle."""
    det = _detector([_active(cpu=50.0)])
    det.observe(T0, last_real_input_at=T0 - timedelta(seconds=10))
    # OS idle clock says 30 min idle; the activity source should override to T0.
    src = AfkSource(
        afk_timeout_seconds=600, hostname="host",
        idle_clock=lambda: 1800.0, activity_sources=[det],
    )
    src.record_sample(T0)
    assert src.samples == [(T0, T0)]


def test_afk_source_activity_source_cannot_push_into_the_future():
    class Rogue:
        def get_last_active_at(self, base, now):
            return now + timedelta(hours=1)  # bug: future timestamp
    src = AfkSource(
        afk_timeout_seconds=600, hostname="host",
        idle_clock=lambda: 5.0, activity_sources=[Rogue()],
    )
    src.record_sample(T0)
    # Clamped: never beyond `now`; falls back to the idle-clock anchor.
    (_, last_input) = src.samples[0]
    assert last_input <= T0


def test_afk_source_ignores_failing_activity_source():
    class Boom:
        def get_last_active_at(self, base, now):
            raise RuntimeError("nope")
    src = AfkSource(
        afk_timeout_seconds=600, hostname="host",
        idle_clock=lambda: 30.0, activity_sources=[Boom()],
    )
    src.record_sample(T0)
    assert src.samples == [(T0, T0 - timedelta(seconds=30))]


def test_base_last_input_at_excludes_activity_sources():
    det = _detector([_active()])
    det.observe(T0, last_real_input_at=T0)
    src = AfkSource(
        afk_timeout_seconds=600, hostname="host",
        idle_clock=lambda: 30.0, activity_sources=[det],
    )
    # base anchor is the OS idle clock only, NOT the activity source.
    assert src.base_last_input_at(T0) == T0 - timedelta(seconds=30)


def test_base_last_input_at_none_on_linux_like_clock():
    src = AfkSource(afk_timeout_seconds=600, hostname="host", idle_clock=lambda: None)
    assert src.base_last_input_at(T0) is None


# -- PsutilForegroundProbe: process-tree CPU sum ------------------------------
# A fake psutil so the tree-walk/seed/sum logic is testable without the real
# dependency (cpu_percent's first call always seeds to 0.0, like real psutil).

import sys  # noqa: E402
import types  # noqa: E402

from src.sync.foreground_activity import PsutilForegroundProbe  # noqa: E402


class _FakeError(Exception):
    pass


class _FakeProc:
    def __init__(self, pid, cpu, children=()):
        self.pid = pid
        self._cpu = cpu
        self._children = list(children)
        self._calls = 0
        self.alive = True

    def cpu_percent(self, interval):
        if not self.alive:
            raise _FakeError(self.pid)
        self._calls += 1
        return 0.0 if self._calls == 1 else self._cpu  # first call seeds

    def children(self, recursive=False):
        return [c for c in self._children if c.alive]


def _install_fake_psutil(monkeypatch, table):
    mod = types.ModuleType("psutil")
    mod.Error = _FakeError

    def Process(pid):  # noqa: N802 (mirror psutil.Process)
        p = table.get(pid)
        if p is None or not p.alive:
            raise _FakeError(pid)
        return p

    mod.Process = Process
    monkeypatch.setitem(sys.modules, "psutil", mod)


def test_probe_sums_root_and_children(monkeypatch):
    child = _FakeProc(11, 80.0)
    root = _FakeProc(10, 20.0, children=[child])
    _install_fake_psutil(monkeypatch, {10: root, 11: child})
    probe = PsutilForegroundProbe(pid_getter=lambda: (10, "Terminal"))
    assert probe.sample().cpu_percent is None      # seed cycle
    s = probe.sample()
    assert s.cpu_percent == 100.0                  # 20 (terminal) + 80 (child)
    assert s.pid == 10 and s.app == "Terminal"


def test_probe_excludes_children_when_disabled(monkeypatch):
    child = _FakeProc(11, 80.0)
    root = _FakeProc(10, 20.0, children=[child])
    _install_fake_psutil(monkeypatch, {10: root, 11: child})
    probe = PsutilForegroundProbe(pid_getter=lambda: (10, "Terminal"), include_children=False)
    probe.sample()                                 # seed
    assert probe.sample().cpu_percent == 20.0      # root only


def test_probe_skips_child_that_dies(monkeypatch):
    child = _FakeProc(11, 80.0)
    root = _FakeProc(10, 20.0, children=[child])
    _install_fake_psutil(monkeypatch, {10: root, 11: child})
    probe = PsutilForegroundProbe(pid_getter=lambda: (10, "Terminal"))
    probe.sample()                                 # seed both
    child.alive = False                            # child exits
    assert probe.sample().cpu_percent == 20.0      # only root counts


def test_probe_new_child_is_seeded_then_counted(monkeypatch):
    root = _FakeProc(10, 20.0)                      # no children yet
    child = _FakeProc(11, 80.0)
    _install_fake_psutil(monkeypatch, {10: root, 11: child})
    probe = PsutilForegroundProbe(pid_getter=lambda: (10, "Terminal"))
    probe.sample()                                 # seed root
    root._children.append(child)                    # child spawns
    # Child is newly-seen -> seeded this cycle (0), only root counts.
    assert probe.sample().cpu_percent == 20.0
    # Next cycle the child contributes.
    assert probe.sample().cpu_percent == 100.0


def test_probe_reseeds_on_foreground_change(monkeypatch):
    a = _FakeProc(10, 20.0)
    b = _FakeProc(20, 50.0)
    _install_fake_psutil(monkeypatch, {10: a, 20: b})
    fg = {"pid": 10}
    probe = PsutilForegroundProbe(pid_getter=lambda: (fg["pid"], "App"))
    probe.sample()                                 # seed app A
    assert probe.sample().cpu_percent == 20.0
    fg["pid"] = 20                                 # user switches apps
    assert probe.sample().cpu_percent is None      # reseed B, unknown this cycle
    assert probe.sample().cpu_percent == 50.0


def test_probe_no_foreground_returns_none(monkeypatch):
    _install_fake_psutil(monkeypatch, {})
    probe = PsutilForegroundProbe(pid_getter=lambda: (None, ""))
    assert probe.sample().cpu_percent is None


# -- SyncEngine wiring --------------------------------------------------------

def _engine():
    from unittest.mock import Mock

    from src.config import Config
    from src.sync.activity_analyzer import ActivityAnalyzer
    from src.sync.daily_time_tracker import DailyTimeTracker
    from src.sync.sync_engine import SyncEngine, _SyncCycleContext

    eng = SyncEngine(
        aw=Mock(), bf=Mock(), queue=Mock(), config=Config(),
        activity_analyzer=Mock(spec=ActivityAnalyzer),
        time_tracker=Mock(spec=DailyTimeTracker),
    )
    return eng, _SyncCycleContext


def test_engine_uploads_live_snapshot_without_counting_open_session():
    eng, ctx_cls = _engine()
    from src.sync.sync_engine import SyncStats

    det = _detector([_active(cpu=70.0)], min_session_seconds=30.0)
    eng._foreground_detector = det
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: None)  # no OS clock
    # _observe_foreground_activity samples real wall-clock now, so anchor the
    # session to now: open it well in the past (so duration >= min_session) but
    # keep real input recent so it stays credit-eligible.
    now = datetime.now(timezone.utc)
    det.observe(now - timedelta(seconds=120), last_real_input_at=now - timedelta(seconds=120))

    out: list = []
    stats = SyncStats()
    eng._observe_foreground_activity(out, stats, ctx_cls(last_input_at=now))
    # Live snapshot uploaded, but an OPEN session is not counted as finished.
    assert len(out) == 1 and out[0]["bucket_type"] == "dev-session"
    assert stats.dev_sessions_detected == 0
    assert eng.is_active_dev_session() is True


def test_engine_observe_appends_span_and_increments_stat():
    eng, ctx_cls = _engine()
    from src.sync.sync_engine import SyncStats

    det = _detector(
        [_active(cpu=70.0), _active(cpu=70.0), ForegroundSample(None, "", None)],
        min_session_seconds=30.0,
    )
    eng._foreground_detector = det
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: None)

    out: list = []
    stats = SyncStats()
    # Open at T0.
    det.observe(T0, last_real_input_at=T0)
    # Keep alive at +60s.
    det.observe(T0 + timedelta(seconds=60), last_real_input_at=T0 + timedelta(seconds=60))
    assert eng.is_active_dev_session() is True

    # Now drive a cycle where the foreground goes away -> span emitted + counted.
    eng._observe_foreground_activity(out, stats, ctx_cls(last_input_at=T0 + timedelta(seconds=90)))
    assert len(out) == 1
    assert out[0]["bucket_type"] == "dev-session"
    assert stats.dev_sessions_detected == 1
    assert eng.is_active_dev_session() is False


def test_engine_is_active_dev_session_false_without_detector():
    eng, _ = _engine()
    eng._foreground_detector = None
    assert eng.is_active_dev_session() is False


def test_foreground_activity_is_default_off_for_billing_safety():
    """The foreground-CPU detector must default OFF. It changes tracked/billed
    time (AFK credit + dev-session spans) and its backend support is an
    unfinished follow-up, so a fleet update must NOT activate it for everyone;
    the server enables it deliberately via update_from_server. Guards against a
    silent flip back to on. See PR shipping v1.5.85."""
    from src.config import ForegroundActivitySettings
    from src.sync.foreground_activity import create_detector

    assert ForegroundActivitySettings().enabled is False
    # With the default (disabled) config, no detector is built — fully inert.
    assert create_detector(ForegroundActivitySettings(), "test-host") is None
