"""A status span longer than 24h must reach the server, not the dead-letter table.

The ops board (2026-09/10) showed a weekly cluster of
``Dropped 1 queued event(s) after max retries — the server rejected them; held
in dead-letter for replay, not discarded (buckets=bf-status, oldest ~2000-4000m
old, spans ~0m)`` on many devices, every Monday morning, with no server status
suffix. That exact text is produced by ``evict_unstorable`` (whose summary
carries no ``last_errors``), not by the retry ceiling: a sleep/idle/private span
covering a weekend lid-close is longer than the server's 24h per-event cap
(internal-tool2 ``AgentEventController`` ``events.*.duration`` ``max:86400``), so
``_send_status_span`` sent it, the server 422'd it, it was queued, and the next
drain evicted it as over-long. The replay never resurrects it (the duration bound
is permanent), so the span was lost for good.

The fix splits an over-long span into contiguous chunks of at most 24h at the
source, so every chunk is server-acceptable.

The server here is a FAKE that applies the same duration rule as the real
validator: any event over 86400s rejects the whole batch with a 422.
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from src.config import Config
from src.sync.bf_client import SyncResult
from src.sync.queue import MAX_EVENT_DURATION_SECONDS, OfflineQueue
from src.sync.sync_engine import SyncEngine, SyncStats


class _CapValidatingServer:
    """Accepts a batch only when every duration is within [0, 86400]."""

    def __init__(self):
        self.stored: dict[str, dict] = {}
        self.rejected_batches = 0

    def send_events(self, events):
        if any(not (0 <= e["duration"] <= 86400) for e in events):
            self.rejected_batches += 1
            return SyncResult(
                success=False,
                error="API error (422): The events.0.duration field must not be greater than 86400.",
                transient=False,
            )
        for e in events:
            self.stored[e["id"]] = e
        return SyncResult(success=True, events_synced=len(events))


def _engine(tmp: Path, server) -> SyncEngine:
    config = Config()
    # Known-and-unrestricted schedule (24/7): no working-hours clamp, which is
    # exactly the configuration where a weekend span is not cut at work_end.
    config.working_hours.known = True
    engine = SyncEngine(
        aw=Mock(),
        bf=server,
        queue=OfflineQueue(db_path=tmp / "q.db", max_size=10000),
        config=config,
        time_tracker=Mock(),
    )
    engine.error_reporter = Mock()
    return engine


def _weekend_sleep():
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(hours=62)  # Friday 18:00 -> Monday 08:00
    return start, end


def test_weekend_sleep_span_is_delivered_in_full_and_nothing_is_dropped():
    server = _CapValidatingServer()
    engine = _engine(Path(tempfile.mkdtemp()), server)
    start, end = _weekend_sleep()

    engine.send_sleep_event(start, end)
    engine._process_queue(SyncStats())

    # Consumer-side: what the SERVER ended up holding, not what we built.
    assert server.rejected_batches == 0, "a >24h event reached the server"
    assert engine.queue.dead_letter_count() == 0
    assert engine.queue.size() == 0
    dropped = [
        c for c in engine.error_reporter.capture.call_args_list
        if "Dropped" in c.args[0]
    ]
    assert dropped == [], f"real-loss warning fired: {dropped}"

    chunks = sorted(server.stored.values(), key=lambda e: e["timestamp"])
    assert len(chunks) == 3
    assert all(0 < c["duration"] <= MAX_EVENT_DURATION_SECONDS for c in chunks)
    assert sum(c["duration"] for c in chunks) == (end - start).total_seconds()
    # Contiguous: each chunk starts where the previous one ended.
    cursor = start
    for c in chunks:
        assert datetime.fromisoformat(c["timestamp"]) == cursor
        cursor += timedelta(seconds=c["duration"])
    assert cursor == end
    assert all(c["bucket_type"] == "sleep_time" for c in chunks)
    assert all(c["data"] == {"status": "sleep"} for c in chunks)
    assert all(c["bucket_id"].startswith("bf-status_") for c in chunks)


def test_first_chunk_keeps_the_span_id_so_private_snapshots_still_patch_in_place():
    """The per-cycle private_time refresh re-sends the SAME id with a longer
    duration. Past 24h the first chunk must keep that id (the server already
    holds it) and later chunks must have ids that are stable across cycles."""
    server = _CapValidatingServer()
    engine = _engine(Path(tempfile.mkdtemp()), server)
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=30)

    engine._send_status_span(kind="private", start=start,
                             end=start + timedelta(hours=29),
                             queue_on_failure=False)
    engine._send_status_span(kind="private", start=start,
                             end=start + timedelta(hours=30),
                             queue_on_failure=False)

    expected_first = f"private_{int(start.timestamp())}_{id(engine)}"
    assert expected_first in server.stored
    assert server.stored[expected_first]["duration"] == MAX_EVENT_DURATION_SECONDS
    assert len(server.stored) == 2, sorted(server.stored)
    second = [e for k, e in server.stored.items() if k != expected_first][0]
    assert second["duration"] == 6 * 3600  # latest snapshot superseded the 5h one


def test_span_within_24h_is_still_one_event_with_the_original_id():
    server = _CapValidatingServer()
    engine = _engine(Path(tempfile.mkdtemp()), server)
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=24)

    engine.send_idle_event(start, start + timedelta(seconds=MAX_EVENT_DURATION_SECONDS))

    assert list(server.stored) == [f"idle_{int(start.timestamp())}_{id(engine)}"]
    assert server.stored[f"idle_{int(start.timestamp())}_{id(engine)}"]["duration"] == MAX_EVENT_DURATION_SECONDS


def test_sleep_event_reason_is_an_observability_tag_that_survives_24h_chunking():
    """``reason`` (used for a screen-lock-caused span, system_event_handler.py)
    rides the exact same pipeline as a plain sleep event, so a long-running
    lock spanning a day boundary must be split into contiguous <=24h chunks
    exactly like a long sleep is — every chunk still carries bucket_type
    "sleep_time" (the server's exclusion keys on that alone) AND the reason,
    and omitting reason entirely must leave ``data`` byte-identical to a
    genuine sleep event (see the weekend-sleep test above: exactly
    ``{"status": "sleep"}``, no "reason" key)."""
    server = _CapValidatingServer()
    engine = _engine(Path(tempfile.mkdtemp()), server)
    start, end = _weekend_sleep()

    engine.send_sleep_event(start, end, reason="lock")

    chunks = sorted(server.stored.values(), key=lambda e: e["timestamp"])
    assert len(chunks) == 3
    assert all(c["bucket_type"] == "sleep_time" for c in chunks)
    assert all(c["data"] == {"status": "sleep", "reason": "lock"} for c in chunks)

    # No reason given -> no "reason" key at all, not reason=None.
    server2 = _CapValidatingServer()
    engine2 = _engine(Path(tempfile.mkdtemp()), server2)
    engine2.send_sleep_event(start, start + timedelta(hours=1))
    [plain_event] = server2.stored.values()
    assert plain_event["data"] == {"status": "sleep"}
    assert "reason" not in plain_event["data"]
