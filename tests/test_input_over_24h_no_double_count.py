"""An input span longer than 24 h must not multiply its counts (#276 item 1).

The server caps one event at 86400 s, so ``_split_over_long_event`` cuts a
longer event into <=24 h chunks. It used to copy ``data`` into EVERY chunk,
which is right for an AFK ``status`` and wrong for keystroke counts: 1000
presses over 25 h reached the server as 2 x 1000.

Two producers can emit such a span:
  * MacOSInputWatcher after an ActivityWatch outage > 24 h (its counters are
    held across failed posts and re-sent with the real, uncapped span);
  * the in-process InputSource on a machine typed on right after sleeping past
    a day (no splitter ran at all on that path, so the over-cap event went out
    whole and was dead-lettered).

The server here is a FAKE applying the real validator's duration rule and
upserting on id, so what is asserted is what the SERVER holds.
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from src.config import Config
from src.sync.aw_client import AWEvent, BUCKET_TYPE_INPUT
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine, SyncStats, _SyncCycleContext
from tests.test_afk_event_over_24h_split import _CapValidatingServer
from tests.test_inproc_input_fraud_score import _engine as _inproc_engine
from tests.test_input_span_catch_up import (
    T0, _Clock, _ScriptedAW, _install_clock, _press, _run, _watcher,
)

BUCKET = "aw-watcher-input_test-host"


def _engine(server) -> SyncEngine:
    config = Config()
    config.working_hours.known = True
    engine = SyncEngine(
        aw=Mock(),
        bf=server,
        queue=OfflineQueue(db_path=Path(tempfile.mkdtemp()) / "q.db", max_size=10000),
        config=config,
        time_tracker=Mock(),
    )
    engine.error_reporter = Mock()
    return engine


def _held(server, key):
    return sum(e["data"][key] for e in server.stored.values())


def test_macos_catch_up_over_24h_is_counted_once(monkeypatch):
    """The real emitter holds 1000 presses across a 25 h AW outage and posts
    them on recovery; what the server stores must total 1000, not 2000."""
    clock = _Clock(T0)
    _install_clock(monkeypatch, clock)
    aw = _ScriptedAW()
    w = _watcher(aw)
    aw.fail = True

    ticks = 25 * 3600 // 10

    def typing():
        if clock.now == T0 + timedelta(seconds=10):  # all 1000 up front
            _press(w, 1000)

    def recover():
        aw.fail = False

    _run(monkeypatch, w, clock, [typing] * (ticks - 1) + [recover])

    assert len(aw.posted) == 1
    posted = aw.posted[0]
    assert posted["data"]["presses"] == 1000
    assert posted["duration"] > 86400, "premise: the emitter stamped the real span"

    server = _CapValidatingServer()
    engine = _engine(server)
    transformed, _ = engine._transform_and_checkpoint(
        [AWEvent.from_dict({**posted, "id": 7})], BUCKET, BUCKET_TYPE_INPUT,
        SyncStats(), _SyncCycleContext(),
    )
    assert server.send_events(transformed).success

    assert server.rejected_batches == 0
    assert len(server.stored) == 2, "premise: split into two <=24h chunks"
    assert _held(server, "presses") == 1000


def test_split_shares_sum_exactly_and_follow_chunk_length():
    """Three chunks (24 h + 24 h + 2 h) with counts that do not divide evenly."""
    server = _CapValidatingServer()
    engine = _engine(server)
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=51)
    event = AWEvent(id=9, timestamp=start, duration=50 * 3600,
                    data={"presses": 1001, "clicks": 7, "scrolls": 0, "app": "Editor"})

    transformed, _ = engine._transform_and_checkpoint(
        [event], BUCKET, BUCKET_TYPE_INPUT, SyncStats(), _SyncCycleContext()
    )
    server.send_events(transformed)

    chunks = sorted(server.stored.values(), key=lambda e: e["timestamp"])
    assert [c["duration"] for c in chunks] == [86400, 86400, 7200]
    assert _held(server, "presses") == 1001
    assert _held(server, "clicks") == 7
    assert _held(server, "scrolls") == 0
    # Rate is uniform: the 2 h tail holds ~4% of the span's presses, not a third.
    assert chunks[2]["data"]["presses"] < 60
    assert all(c["data"]["app"] == "Editor" for c in chunks)


def test_inproc_input_slept_past_a_day_reaches_the_server_in_full():
    """The in-process stream (Windows default) never went through the splitter:
    a 25 h span uploaded whole and the server's 86400 cap refused it."""
    eng, _analyzer, sent = _inproc_engine()
    eng._input_inproc_checkpoint = datetime.now(timezone.utc) - timedelta(hours=25)
    eng.input_source._on_press(1000)

    eng.sync()

    inputs = [e for e in sent if e.get("bucket_type") == BUCKET_TYPE_INPUT]
    server = _CapValidatingServer()
    assert server.send_events(inputs).success
    assert server.rejected_batches == 0
    assert _held(server, "presses") == 1000
