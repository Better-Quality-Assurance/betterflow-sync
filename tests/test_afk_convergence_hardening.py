"""Tracker-convergence hardening (stage 1).

Two independent failure modes from the in-process-AFK rollout:

1. The aw_manager "in-process AFK active" flag (which gates the idle-tracker
   watchdog + AFK health telemetry) was a CACHE kept in step with the engine's
   real per-cycle decision only by a separate 60s reconcile timer. When that
   timer silently died (Bug A, #76/#78: an AttributeError swallowed by
   _tick_60s's per-task try/except), the flag went stale for a whole release and
   nothing complained. Fix: the sync cycle itself publishes the decision to the
   flag sink on the path where the decision is made — so during active work the
   flag tracks the engine regardless of the timer's health (one source of truth).

2. _tick_60s swallows every sub-task exception with a local logger.warning, so a
   sub-task (like the reconcile above) can fail every 60s for a release with no
   ops signal. Fix: escalate a repeatedly-failing sub-task to the error_reporter
   so the same class of silent breakage surfaces in minutes, not on manual
   fleet-log reading.
"""

from datetime import timedelta
from unittest.mock import Mock

from src.config import Config
from src.main import SyncCoordinator
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.afk_source import AfkSource
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.sync_engine import SyncEngine

# --------------------------------------------------------------------------- #
# 1. Single source of truth: the cycle publishes its decision to the flag sink #
# --------------------------------------------------------------------------- #

def _runnable_engine(in_process_afk: bool) -> SyncEngine:
    """A SyncEngine wired so a full sync() cycle runs to the AFK-decision point
    with no real buckets — exercises the real code path, not a stub of it."""
    cfg = Config()
    cfg.sync.in_process_afk = in_process_afk

    aw = Mock()
    aw.is_running.return_value = True
    aw.get_window_buckets.return_value = []
    aw.get_web_buckets.return_value = []
    aw.get_afk_buckets.return_value = []
    aw.get_input_buckets.return_value = []

    bf = Mock()
    bf.is_reachable.return_value = True

    queue = Mock()
    queue.get_checkpoint.return_value = None
    queue.is_empty.return_value = True

    analyzer = Mock(spec=ActivityAnalyzer)
    analyzer.get_activity_state.return_value = "active"
    analyzer.get_raw_metrics.return_value = Mock(
        to_dict=lambda: {"presses": 0, "clicks": 0, "scrolls": 0, "window_changes": 0}
    )
    analyzer.get_fraud_assessment.return_value = Mock(
        score=0, signals=[], extra_metrics={"unique_apps": 0, "keystroke_variance": None}
    )

    tracker = Mock(spec=DailyTimeTracker)
    tracker.get_today_active_time.return_value = timedelta(hours=1)

    eng = SyncEngine(aw=aw, bf=bf, queue=queue, config=cfg,
                     activity_analyzer=analyzer, time_tracker=tracker)
    eng._config_fetched = True      # skip the first-cycle server-config fetch
    eng._backlog_reconciled = True  # skip the one-time start-of-day backlog reconcile
    return eng


def test_cycle_publishes_inproc_flag_true_when_active():
    eng = _runnable_engine(True)
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: 5.0)
    published: list = []
    eng.inproc_afk_flag_sink = published.append

    eng.sync()

    # The decision (skip the external bucket, use the in-process stream) was
    # pushed to the sink on the real cycle path.
    assert published and published[-1] is True


def test_cycle_publishes_inproc_flag_false_when_off():
    eng = _runnable_engine(False)
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: 5.0)
    published: list = []
    eng.inproc_afk_flag_sink = published.append

    eng.sync()

    # Flag off → the external tracker is still the source, so the flag MUST be
    # False (otherwise its watchdog/alerts stay wrongly suppressed).
    assert published and published[-1] is False


def test_cycle_publish_matches_should_skip_decision():
    eng = _runnable_engine(True)
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: None)  # Linux: unavailable
    published: list = []
    eng.inproc_afk_flag_sink = published.append

    eng.sync()

    # Unavailable clock → not active → publish False, matching the gate.
    assert published[-1] is eng._should_skip_external_afk() is False


def test_missing_sink_does_not_break_the_cycle():
    eng = _runnable_engine(True)
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: 5.0)
    eng.inproc_afk_flag_sink = None  # not wired
    # Must not raise.
    eng.sync()


# --------------------------------------------------------------------------- #
# 2. Loud escalation of a repeatedly-failing _tick_60s sub-task                #
# --------------------------------------------------------------------------- #

def _coord() -> SyncCoordinator:
    c = SyncCoordinator.__new__(SyncCoordinator)
    c._tick_failure_counts = {}
    c._tick_failure_reported = set()
    c.error_reporter = Mock()
    c.reminder_manager = None
    return c


def test_tick_task_escalates_after_threshold():
    c = _coord()

    def boom():
        raise RuntimeError("AttributeError-style silent breakage")

    threshold = SyncCoordinator._TICK_FAILURE_ESCALATE_THRESHOLD
    for _ in range(threshold):
        c._run_tick_task(boom, "inproc_afk_reconcile")

    assert c.error_reporter.capture.call_count == 1
    kwargs = c.error_reporter.capture.call_args.kwargs
    assert kwargs.get("fingerprint") == "tick-60s-inproc_afk_reconcile"


def test_tick_task_escalates_only_once_while_still_failing():
    c = _coord()

    def boom():
        raise RuntimeError("still broken")

    for _ in range(SyncCoordinator._TICK_FAILURE_ESCALATE_THRESHOLD + 5):
        c._run_tick_task(boom, "task")

    assert c.error_reporter.capture.call_count == 1  # latched, no spam


def test_tick_task_success_resets_and_re_arms_escalation():
    c = _coord()

    def boom():
        raise RuntimeError("broken")

    def ok():
        return None

    for _ in range(SyncCoordinator._TICK_FAILURE_ESCALATE_THRESHOLD):
        c._run_tick_task(boom, "task")
    assert c.error_reporter.capture.call_count == 1

    c._run_tick_task(ok, "task")  # recovered → counter clears, latch re-arms

    for _ in range(SyncCoordinator._TICK_FAILURE_ESCALATE_THRESHOLD):
        c._run_tick_task(boom, "task")
    assert c.error_reporter.capture.call_count == 2  # a fresh outage escalates again


def test_tick_task_below_threshold_does_not_escalate():
    c = _coord()

    def boom():
        raise RuntimeError("transient")

    for _ in range(SyncCoordinator._TICK_FAILURE_ESCALATE_THRESHOLD - 1):
        c._run_tick_task(boom, "task")

    c.error_reporter.capture.assert_not_called()


def test_tick_task_no_error_reporter_is_tolerated():
    c = _coord()
    c.error_reporter = None

    def boom():
        raise RuntimeError("broken")

    for _ in range(SyncCoordinator._TICK_FAILURE_ESCALATE_THRESHOLD + 1):
        c._run_tick_task(boom, "task")  # must not raise
