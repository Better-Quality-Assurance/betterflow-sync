"""A cycle abandoned by the wedge takeover must not overwrite its replacement's
input count (#275 pre-merge audit, round 1).

``_acquire_sync_slot`` abandons a cycle that has held the lock for 420 s and
starts a replacement while the old thread is still alive inside ``sync()``. The
in-process input id is ``input-inproc_<host>_<checkpoint ms>``, so two drains
over the same checkpoint share an id and the server keeps the last write.

Scripted interleaving, no sleeping: the abandoned cycle Z is paused inside
``_sync_window_buckets`` (as a hung ActivityWatch would pause it), the
replacement R runs a whole cycle inside that pause, then Z resumes. The
"server" is a dict upserting on id, last write wins, as the real one does.
"""

from src.sync.aw_client import BUCKET_TYPE_INPUT
from tests.test_inproc_input_fraud_score import _engine


def _server_total(sent):
    held = {}
    for e in sent:  # in send order; upsert on id
        if e.get("bucket_type") == BUCKET_TYPE_INPUT:
            held[e["id"]] = e["data"]["presses"]
    return sum(held.values())


def test_abandoned_cycle_does_not_overwrite_the_replacements_count(monkeypatch):
    eng, _analyzer, sent = _engine()
    eng.input_source._on_press(40)  # typed before Z starts
    real = eng._sync_window_buckets
    state = {"first": True}

    def hang_then_takeover(*a, **k):
        if state["first"]:
            state["first"] = False
            eng.input_source._on_press(5)  # typed while Z is hung
            eng.sync()  # R: the takeover cycle runs to completion inside Z's hang
        return real(*a, **k)  # Z wakes up and finishes normally

    monkeypatch.setattr(eng, "_sync_window_buckets", hang_then_takeover)
    eng.sync()

    assert _server_total(sent) == 45


def test_control_without_a_takeover_uploads_once(monkeypatch):
    eng, _analyzer, sent = _engine()
    eng.input_source._on_press(40)
    eng.input_source._on_press(5)

    eng.sync()

    assert _server_total(sent) == 45
