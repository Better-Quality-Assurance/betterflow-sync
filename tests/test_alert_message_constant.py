"""An ops alert's MESSAGE must not vary with the measurement it reports.

Origin: betterflow-sync#266 (BW TASK-001269). betterqa-bot's DSN ingest treats
a client-supplied fingerprint as a SPLITTER, not a name: the stored key is
``mixClientFingerprint(serverDerived, client)`` where ``serverDerived`` is a
hash of the normalised MESSAGE (betterqa-bot ``src/webhooks/error-ingest.ts``
``resolveIngestFingerprint``). Its normaliser does not eat bare numbers
(measured: "oldest ~3716m old" and "oldest ~2100m old" normalise to two
different strings), so every distinct age, count or version in a message
becomes its own error row and its own Slack alert. One dead-letter condition
produced 8 rows in 48h.

So the fix is the one the ticket names: the message is a constant per
fingerprint, and every varying value rides in ``context`` (stored on the row,
newest-wins).

Each test below captures the SAME condition twice with DIFFERENT values and
asserts the two messages are byte-identical, and that the values still reach
the ingest through ``context`` (the boring half: a message emptied of
everything would pass the first assertion alone).
"""

from datetime import datetime, timedelta, timezone

from src.sync.sync_engine import SyncEngine
from tests.test_sync_failure_alert_names_every_reason import (
    _coordinator,
    _failed_stats,
)


class _Recorder:
    def __init__(self):
        self.captures = []

    def capture(self, message, **kwargs):
        self.captures.append({"message": message, **kwargs})


def _engine():
    engine = SyncEngine.__new__(SyncEngine)
    engine.error_reporter = _Recorder()
    return engine


def _iso(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _only(recorder, fingerprint):
    got = [c for c in recorder.captures if c.get("fingerprint") == fingerprint]
    assert len(got) == 1, recorder.captures
    return got[0]


# ---------------------------------------------------------------- dead letter


def _drop(engine, *, real, count, minutes_ago, buckets, errors):
    SyncEngine._report_dropped_events(engine, {
        "count": count,
        "real_loss_count": real,
        "bucket_ids": buckets,
        "oldest": _iso(minutes_ago),
        "newest": _iso(minutes_ago - 1),
        "last_errors": errors,
    })


def test_dead_letter_message_is_identical_for_two_events_of_different_ages():
    a, b = _engine(), _engine()
    _drop(a, real=1, count=1, minutes_ago=3716,
          buckets=["bf-status_host-a"], errors=[])
    _drop(b, real=3, count=5, minutes_ago=2100,
          buckets=["aw-watcher-window_host-b", "bf-status_host-b"],
          errors=["API error (422): nope"])

    ca = _only(a.error_reporter, "offline-queue-events-dropped")
    cb = _only(b.error_reporter, "offline-queue-events-dropped")

    assert ca["message"] == cb["message"], (ca["message"], cb["message"])
    # The values moved, they did not vanish.
    assert cb["context"]["dropped_count"] == 3
    assert cb["context"]["other_unstorable_count"] == 2
    assert cb["context"]["buckets"] == "aw-watcher-window,bf-status"
    assert "2100m" in cb["context"]["window"], cb["context"]
    assert "server status 422" in cb["context"]["reason"], cb["context"]
    assert "3716m" in ca["context"]["window"], ca["context"]


def test_flushed_unstorable_message_is_identical_for_two_events_of_different_ages():
    a, b = _engine(), _engine()
    _drop(a, real=0, count=2, minutes_ago=20000,
          buckets=["bf-status_h"], errors=[])
    _drop(b, real=0, count=7, minutes_ago=90000,
          buckets=["aw-watcher-afk_h"], errors=[])

    ca = _only(a.error_reporter, "offline-queue-events-flushed-unstorable")
    cb = _only(b.error_reporter, "offline-queue-events-flushed-unstorable")

    assert ca["message"] == cb["message"], (ca["message"], cb["message"])
    assert cb["context"]["flushed_count"] == 7
    assert cb["context"]["buckets"] == "aw-watcher-afk"
    assert "90000m" in cb["context"]["window"], cb["context"]


# ------------------------------------------------------- other SyncEngine sites


def test_window_probe_blind_message_does_not_carry_the_failure_count():
    a, b = _engine(), _engine()
    SyncEngine._report_window_blind(a, 5)
    SyncEngine._report_window_blind(b, 50)
    ca = _only(a.error_reporter, "inproc-window-blind")
    cb = _only(b.error_reporter, "inproc-window-blind")
    assert ca["message"] == cb["message"], (ca["message"], cb["message"])
    assert cb["context"] == {"consecutive_failures": 50}


def test_afk_clock_blind_message_does_not_carry_the_failure_count():
    a, b = _engine(), _engine()
    SyncEngine._report_inproc_blind(a, 3)
    SyncEngine._report_inproc_blind(b, 30)
    ca = _only(a.error_reporter, "inproc-afk-blind")
    cb = _only(b.error_reporter, "inproc-afk-blind")
    assert ca["message"] == cb["message"], (ca["message"], cb["message"])
    assert cb["context"] == {"consecutive_failures": 30}


def test_below_minimum_message_does_not_carry_the_versions():
    a, b = _engine(), _engine()
    SyncEngine._report_below_minimum(a, "1.5.100")
    SyncEngine._report_below_minimum(b, "1.5.140")
    ca = _only(a.error_reporter, "agent-below-minimum-version")
    cb = _only(b.error_reporter, "agent-below-minimum-version")
    assert ca["message"] == cb["message"], (ca["message"], cb["message"])
    assert cb["context"]["min_version"] == "1.5.140"
    assert cb["context"]["agent_version"]


def test_log_upload_failure_message_does_not_carry_the_reason():
    a, b = _engine(), _engine()
    SyncEngine._report_upload_failure(a, "betterflow.log empty/unreadable")
    SyncEngine._report_upload_failure(b, "upload POST failed: API error (503): x")
    ca = _only(a.error_reporter, "log-upload-failed")
    cb = _only(b.error_reporter, "log-upload-failed")
    assert ca["message"] == cb["message"], (ca["message"], cb["message"])
    assert cb["context"] == {"reason": "upload POST failed: API error (503): x"}


# ----------------------------------------------------------- SyncCoordinator


def test_repeated_sync_failure_message_does_not_carry_the_consecutive_count():
    stats = _failed_stats(["Sync failed"])
    first = _coordinator(stats)
    later = _coordinator(stats)
    # first crosses the threshold; later is well past it
    later._consecutive_sync_failures = later._SYNC_FAILURE_ALERT_THRESHOLD + 6

    first._do_sync()
    later._do_sync()

    def _msgs(coord):
        return [
            c for c in coord.error_reporter.capture.call_args_list
            if c.kwargs.get("fingerprint") == "sync-repeated-failure"
        ]

    (fa,), (la,) = _msgs(first), _msgs(later)
    assert fa.args[0] == la.args[0], (fa.args[0], la.args[0])
    assert la.kwargs["context"]["consecutive_failures"] == (
        later._SYNC_FAILURE_ALERT_THRESHOLD + 7
    )


def test_restart_loop_message_does_not_carry_the_restart_count():
    def _run(restarts):
        coord = _coordinator(_failed_stats([]))
        del coord._monitor_capture_health  # the real one, not the stub
        coord.aw_manager.is_managing = True
        coord.aw_manager.stale_restart_count.return_value = restarts
        coord._restart_loop_escalated = False
        coord._monitor_capture_health()
        got = [
            c for c in coord.error_reporter.capture.call_args_list
            if c.kwargs.get("fingerprint") == "idle-tracker-restart-loop"
        ]
        assert len(got) == 1, coord.error_reporter.capture.call_args_list
        return got[0]

    threshold = _coordinator(_failed_stats([]))._RESTART_LOOP_ALERT_THRESHOLD
    a, b = _run(threshold), _run(threshold + 9)
    assert a.args[0] == b.args[0], (a.args[0], b.args[0])
    assert b.kwargs["context"] == {"stale_restarts": threshold + 9}


def test_recorder_sanity_a_varying_message_would_be_caught():
    # Positive control for the comparison itself: two genuinely different
    # strings must compare unequal through the same recorder path.
    r = _Recorder()
    r.capture("x 1", fingerprint="f")
    r.capture("x 2", fingerprint="f")
    assert r.captures[0]["message"] != r.captures[1]["message"]
