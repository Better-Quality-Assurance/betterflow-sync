"""The analyzer must end up with the counts that were UPLOADED.

The top-of-cycle peek feeds the analyzer [cp, t_peek] under id
``input-inproc_<host>_<cp ms>``; the drain at the send uploads the SAME id over
[cp, t_drain] with larger counts. Input typed in between, and input typed after
a failed cycle, used to be uploaded but never analysed (the analyzer dropped a
later event with a known id).
"""

from datetime import datetime, timedelta, timezone

from tests.test_inproc_input_fraud_score import _engine
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.aw_client import BUCKET_TYPE_INPUT, AWEvent


def _total(analyzer):
    return sum(e.presses for e in analyzer._input_events)


def _ev(i, presses, secs, start=None):
    start = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return AWEvent(id=i, timestamp=start, duration=secs,
                   data={"presses": presses, "clicks": 0, "scrolls": 0})


def _press_during_window_sync(eng, n):
    orig = eng._sync_window_buckets

    def wrapped(*a, **k):
        eng.input_source._on_press(n)
        return orig(*a, **k)

    eng._sync_window_buckets = wrapped


def test_a_input_typed_during_the_cycle_is_analysed_across_cycles():
    eng, analyzer, sent = _engine()
    eng.input_source._on_press(40)
    _press_during_window_sync(eng, 30)

    eng.sync()

    uploaded = sum(e["data"]["presses"] for e in sent
                   if e.get("bucket_type") == BUCKET_TYPE_INPUT)
    assert uploaded == 70, uploaded
    assert _total(analyzer) == 70

    # Next cycle: nothing typed. Analyzer total unchanged (no double count).
    eng._sync_window_buckets = type(eng)._sync_window_buckets.__get__(eng)
    eng.sync()
    assert _total(analyzer) == 70


def test_b_failed_cycle_then_recovery_holds_recovered_counts_not_a_sum():
    eng, analyzer, sent = _engine()
    eng.input_source._on_press(40)
    orig = eng._sync_window_buckets
    state = {"raise": True}

    def boom(*a, **k):
        if state["raise"]:
            state["raise"] = False
            raise RuntimeError("cycle dies before the send")
        return orig(*a, **k)

    eng._sync_window_buckets = boom
    try:
        eng.sync()
    except Exception:
        pass
    assert _total(analyzer) == 40  # the peek of the failed cycle

    eng.input_source._on_press(500)
    eng.sync()  # recovery: same checkpoint, same id, larger span
    assert _total(analyzer) == 540


def test_c_quiet_following_cycle_leaves_total_unchanged():
    eng, analyzer, _sent = _engine()
    eng.input_source._on_press(40)
    eng.sync()
    first = _total(analyzer)
    assert first == 40
    eng.sync()
    eng.sync()
    assert _total(analyzer) == first


def test_analyzer_replaces_same_id_with_superset_never_sums():
    a = ActivityAnalyzer()
    a.add_input_events([_ev("x", 40, 10)])
    a.add_input_events([_ev("x", 540, 20)])
    assert [e.presses for e in a._input_events] == [540]


def test_analyzer_ignores_stale_smaller_redelivery_of_same_id():
    a = ActivityAnalyzer()
    a.add_input_events([_ev("x", 540, 20)])
    a.add_input_events([_ev("x", 40, 10)])
    assert [e.presses for e in a._input_events] == [540]
