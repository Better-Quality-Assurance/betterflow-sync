"""Drained in-process input counts must survive a failed cycle (#276 item 2).

#275 moved the in-process input drain to the TOP of ``sync()`` so the fraud
analyzer can see it. The drain is destructive (counters zeroed into the event),
so an unexpected exception between the drain and the send dropped the counts:
the cycle died, the event never reached the queue, and the next cycle drained an
empty counter. Before the move the drain sat next to the send, so nothing could
fall in between.

Asserted on what a later, healthy cycle uploads: the user's keystrokes.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.sync.aw_client import BUCKET_TYPE_INPUT
from tests.test_inproc_input_fraud_score import _engine


def _uploaded_presses(sent):
    return sum(
        e["data"]["presses"] for e in sent if e.get("bucket_type") == BUCKET_TYPE_INPUT
    )


def test_counts_drained_before_an_unexpected_exception_upload_on_recovery(monkeypatch):
    eng, _analyzer, sent = _engine()
    eng.input_source._on_press(40)

    def boom(*a, **k):
        raise RuntimeError("unexpected, not an AWClientError")

    real = eng._sync_window_buckets
    monkeypatch.setattr(eng, "_sync_window_buckets", boom)
    with pytest.raises(RuntimeError):
        eng.sync()
    assert _uploaded_presses(sent) == 0, "premise: the failed cycle sent nothing"

    monkeypatch.setattr(eng, "_sync_window_buckets", real)
    eng.sync()

    assert _uploaded_presses(sent) == 40


def test_keystrokes_typed_after_the_failure_add_to_the_restored_ones(monkeypatch):
    eng, _analyzer, sent = _engine()
    eng.input_source._on_press(40)
    real = eng._sync_window_buckets

    def boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(eng, "_sync_window_buckets", boom)
    with pytest.raises(RuntimeError):
        eng.sync()
    eng.input_source._on_press(5)  # typed while the cycle was down

    monkeypatch.setattr(eng, "_sync_window_buckets", real)
    eng.sync()

    assert _uploaded_presses(sent) == 45


def test_successful_cycle_uploads_the_counts_exactly_once():
    """Control for the restore: it must not fire on the success path."""
    eng, _analyzer, sent = _engine()
    eng.input_source._on_press(40)

    eng.sync()
    eng.sync()  # a second healthy cycle: nothing new was typed

    assert _uploaded_presses(sent) == 40
    assert eng.input_source.counts == (0, 0, 0)


def test_checkpoint_is_not_advanced_by_the_failed_cycle(monkeypatch):
    """The recovery span must start where the lost one did, so the restored
    counts are stamped over the time they were typed in."""
    eng, _analyzer, sent = _engine()
    cp = datetime.now(timezone.utc) - timedelta(seconds=300)
    eng._input_inproc_checkpoint = cp
    eng.input_source._on_press(40)

    def boom(*a, **k):
        raise RuntimeError("unexpected")

    real = eng._sync_window_buckets
    monkeypatch.setattr(eng, "_sync_window_buckets", boom)
    with pytest.raises(RuntimeError):
        eng.sync()
    monkeypatch.setattr(eng, "_sync_window_buckets", real)
    eng.sync()

    event = [e for e in sent if e.get("bucket_type") == BUCKET_TYPE_INPUT][0]
    assert datetime.fromisoformat(event["timestamp"]) == cp
