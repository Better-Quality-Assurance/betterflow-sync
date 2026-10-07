"""An ActivityWatch AFK event longer than 24h must reach the server, not the dead-letter table.

Sibling of ``test_status_span_over_24h_split.py`` (#267), which fixed the same
loss for the agent's OWN status spans (bucket ``bf-status``). That fix did not
cover events read from ActivityWatch: ``_transform_event`` sends an
``aw-watcher-afk`` event with its real duration, and a machine left running but
untouched over a weekend produces one AFK event of ~40-65h (AW merges the
heartbeats). The server caps one event at 86400s (internal-tool2
``AgentEventController`` ``events.*.duration`` ``max:86400``) and 422s the whole
batch, so the event was queued, evicted by ``evict_unstorable`` as over-long and
reported on the ops board as

    Dropped 1 queued event(s) after max retries — the server rejected them;
    held in dead-letter for replay, not discarded (buckets=aw-watcher-afk,
    oldest ~4072m old, spans ~0m)

(2026-10-05, device 17, agent 1.5.136). The dead-letter replay never resurrects
it, so the span was lost — and it is the AFK span that tells the server the
weekend was idle.

The server here is a FAKE applying the real validator's duration rule.
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from src.config import Config
from src.sync.aw_client import AWEvent, BUCKET_TYPE_AFK
from src.sync.bf_client import SyncResult
from src.sync.queue import MAX_EVENT_DURATION_SECONDS, OfflineQueue
from src.sync.sync_engine import SyncEngine, SyncStats, _SyncCycleContext

BUCKET = "aw-watcher-afk_test-host"


class _CapValidatingServer:
    """Accepts a batch only when every duration is within [0, 86400]; upserts on id."""

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
            self.stored[str(e["id"])] = e
        return SyncResult(success=True, events_synced=len(events))


def _engine(server) -> SyncEngine:
    config = Config()
    config.working_hours.known = True  # 24/7: no working-hours gate
    engine = SyncEngine(
        aw=Mock(),
        bf=server,
        queue=OfflineQueue(db_path=Path(tempfile.mkdtemp()) / "q.db", max_size=10000),
        config=config,
        time_tracker=Mock(),
    )
    engine.error_reporter = Mock()
    return engine


def _afk(start: datetime, hours: float, event_id: int = 4242) -> AWEvent:
    return AWEvent(id=event_id, timestamp=start, duration=hours * 3600,
                   data={"status": "afk"})


def _transform_and_send(engine, server, event):
    transformed, _ = engine._transform_and_checkpoint(
        [event], BUCKET, BUCKET_TYPE_AFK, SyncStats(), _SyncCycleContext()
    )
    result = server.send_events(transformed)
    return transformed, result


def test_weekend_afk_event_is_delivered_in_full():
    server = _CapValidatingServer()
    engine = _engine(server)
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=63)

    transformed, result = _transform_and_send(engine, server, _afk(start, 62))

    # Consumer side: what the SERVER holds, not what we built.
    assert server.rejected_batches == 0, "a >24h AFK event reached the server"
    assert result.success
    chunks = sorted(server.stored.values(), key=lambda e: e["timestamp"])
    assert len(chunks) == 3
    assert all(0 < c["duration"] <= MAX_EVENT_DURATION_SECONDS for c in chunks)
    assert sum(c["duration"] for c in chunks) == 62 * 3600
    cursor = start
    for c in chunks:
        assert datetime.fromisoformat(c["timestamp"]) == cursor
        cursor += timedelta(seconds=c["duration"])
    assert all(c["bucket_id"] == BUCKET for c in chunks)
    assert all(c["bucket_type"] == BUCKET_TYPE_AFK for c in chunks)
    assert all(c["data"] == {"status": "afk"} for c in chunks)


def test_first_chunk_keeps_the_aw_id_and_later_ids_are_stable_as_the_event_grows():
    """AW extends the same event id every heartbeat and the server patches the
    row in place keyed on bucket|id. Past 24h the first chunk must keep the AW
    id (the server already holds it) and later chunk ids must not change as the
    event keeps growing, or every cycle would insert a new row."""
    server = _CapValidatingServer()
    engine = _engine(server)
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=40)

    _transform_and_send(engine, server, _afk(start, 23))
    _transform_and_send(engine, server, _afk(start, 30))
    _transform_and_send(engine, server, _afk(start, 31))

    assert server.rejected_batches == 0
    assert set(server.stored) == {"4242", "4242_1"}, sorted(server.stored)
    assert server.stored["4242"]["duration"] == MAX_EVENT_DURATION_SECONDS
    assert server.stored["4242_1"]["duration"] == 7 * 3600


def test_afk_event_within_24h_is_one_event_with_the_original_id():
    server = _CapValidatingServer()
    engine = _engine(server)
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=25)

    transformed, _ = _transform_and_send(engine, server, _afk(start, 24))

    assert len(transformed) == 1
    assert transformed[0]["id"] == 4242
    assert transformed[0]["duration"] == MAX_EVENT_DURATION_SECONDS
