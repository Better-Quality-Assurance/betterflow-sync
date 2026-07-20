"""Storability classifier + legacy-status-span backfill (v1.5.97).

Two upgrade-path / billing gaps found auditing v1.5.96 before the fleet push:

1a. LEGACY BUCKETLESS STATUS SPANS EVICTED ON UPGRADE (real billing loss).
    Before betterflow-sync #129, idle/break/private/sleep spans were emitted
    with NO bucket_id. #130's evict_unstorable classifies a bucketless event as
    unstorable and dead-letters it before batching — but the server accepts a
    bucketless span (it types the event off bucket_type). So a legacy span
    sitting in the queue at upgrade time is a lost carve-out. Fix: backfill the
    same bf-status_<host> id every current span already carries, at startup,
    BEFORE the first eviction — keeping the prod-proven "bucketed == storable"
    rule intact instead of loosening it (which would risk reviving the very
    poison-drop incident #130 fixed).

1b. OVER-LONG STATUS SPAN POISONS ITS BATCH (pre-existing, weekend trigger).
    The server 4xx-rejects the WHOLE batch when any event's duration is outside
    [0, 86400] (a >24h weekend lid-close sleep, or a private session left on
    across a weekend). Pre-fix the over-long span had a bucket_id + recent
    timestamp so it was classified STORABLE, stayed in the queue, and 422'd
    every batch it joined — dragging storable neighbours to the drop ceiling.
    Fix: an additive duration bound in the shared classifier so the poison is
    evicted first, keeping every batch clean.

Both are verified pre-fix-failing here.
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from src.config import Config
from src.sync.bf_client import SyncResult
from src.sync.queue import MAX_EVENT_DURATION_SECONDS, OfflineQueue, is_event_storable
from src.sync.sync_engine import SyncEngine, SyncStats


def _engine(tmp: Path) -> SyncEngine:
    return SyncEngine(
        aw=Mock(),
        bf=Mock(),
        queue=OfflineQueue(db_path=tmp / "q.db", max_size=10000),
        config=Config(),
        time_tracker=Mock(),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- 1a: legacy bucketless status span survives upgrade and delivers ---------


def test_legacy_bucketless_status_span_is_backfilled_and_delivered():
    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)
    host = engine._hostname

    # A <=1.5.95 idle span: bucket_type + data.status, but NO bucket_id.
    legacy = {
        "id": "idle_123_456",
        "timestamp": _now_iso(),
        "duration": 1800.0,
        "bucket_type": "idle_time",
        "data": {"status": "idle"},
    }
    engine.queue.enqueue([legacy])

    # Startup backfill (SyncEngine.__init__ runs this against the real queue).
    assert engine.queue.backfill_status_bucket_ids(host) == 1

    # Now first-class: eviction must NOT touch it, nothing dead-lettered.
    evicted = engine.queue.evict_unstorable()
    assert evicted["count"] == 0
    assert engine.queue.dead_letter_count() == 0

    # And it delivers to a permissive server (the pre-fix path evicted it, so it
    # never reached delivery).
    delivered: set[str] = set()

    def _accept(events):
        delivered.update(e["id"] for e in events)
        return SyncResult(success=True, events_synced=len(events))

    engine.bf.send_events = Mock(side_effect=_accept)
    engine._queue_backoff_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    engine._process_queue(SyncStats())

    assert "idle_123_456" in delivered, (
        "a legacy bucketless status span must be backfilled and delivered on "
        "upgrade, not evicted as unstorable"
    )


def test_backfill_only_touches_bucketless_status_spans():
    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)
    host = engine._hostname

    already_bucketed = {
        "id": "idle_new", "timestamp": _now_iso(), "duration": 60.0,
        "bucket_id": f"bf-status_{host}", "bucket_type": "idle_time",
        "data": {"status": "idle"},
    }
    real_window = {
        "id": "win", "timestamp": _now_iso(), "duration": 60.0,
        "bucket_id": "aw-watcher-window_h", "data": {"app": "Terminal"},
    }
    legacy = {
        "id": "break_old", "timestamp": _now_iso(), "duration": 300.0,
        "bucket_type": "break_time", "data": {"status": "break"},
    }
    engine.queue.enqueue([already_bucketed, real_window, legacy])

    # Only the one legacy bucketless status span is rewritten; idempotent re-run.
    assert engine.queue.backfill_status_bucket_ids(host) == 1
    assert engine.queue.backfill_status_bucket_ids(host) == 0


def test_sanitize_project_ids_removes_invalid_queued_values():
    tmp = Path(tempfile.mkdtemp())
    queue = OfflineQueue(db_path=tmp / "q.db", max_size=1000)
    try:
        queue.enqueue([
            {
                "id": "idle_bad_project",
                "timestamp": _now_iso(),
                "duration": 60.0,
                "bucket_id": "bf-status_host",
                "bucket_type": "idle_time",
                "project_id": "4152903c-0894-48ed-ad50-491f97f52a46",
                "data": {"status": "idle"},
            },
            {
                "id": "idle_string_project",
                "timestamp": _now_iso(),
                "duration": 60.0,
                "bucket_id": "bf-status_host",
                "bucket_type": "idle_time",
                "project_id": "42",
                "data": {"status": "idle"},
            },
        ])

        assert queue.sanitize_project_ids() == 2

        queued = queue.dequeue(batch_size=10)
        by_id = {q.event_data["id"]: q.event_data for q in queued}
        assert "project_id" not in by_id["idle_bad_project"]
        assert by_id["idle_string_project"]["project_id"] == 42
    finally:
        queue.close()


def test_startup_sanitizes_invalid_project_id_before_status_span_drain():
    tmp = Path(tempfile.mkdtemp())
    queue = OfflineQueue(db_path=tmp / "q.db", max_size=1000)
    queue.enqueue([
        {
            "id": "idle_bad_project",
            "timestamp": _now_iso(),
            "duration": 60.0,
            "bucket_id": "bf-status_host",
            "bucket_type": "idle_time",
            "project_id": "not-a-backend-project-id",
            "data": {"status": "idle"},
        }
    ])

    engine = SyncEngine(
        aw=Mock(),
        bf=Mock(),
        queue=queue,
        config=Config(),
        time_tracker=Mock(),
    )

    delivered: list[dict] = []

    def _accept(events):
        delivered.extend(events)
        return SyncResult(success=True, events_synced=len(events))

    engine.bf.send_events = Mock(side_effect=_accept)
    engine._queue_backoff_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    engine._process_queue(SyncStats())

    assert delivered
    assert "project_id" not in delivered[0]
    assert engine.queue.size() == 0
    engine.queue.close()


# --- 1b: over-long span is evicted, not left to poison the batch --------------


def test_oversize_duration_span_is_evicted_before_batching():
    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)
    host = engine._hostname

    oversize = {
        "id": "sleep_1", "timestamp": _now_iso(),
        "duration": float(MAX_EVENT_DURATION_SECONDS + 3600),  # >24h weekend sleep
        "bucket_id": f"bf-status_{host}", "bucket_type": "sleep_time",
        "data": {"status": "sleep"},
    }
    storable = {
        "id": "win_1", "timestamp": _now_iso(), "duration": 60.0,
        "bucket_id": "aw-watcher-window_h", "data": {"app": "Terminal"},
    }
    engine.queue.enqueue([oversize, storable])

    evicted = engine.queue.evict_unstorable()
    assert evicted["count"] == 1, "the >24h span must be evicted as poison"
    assert engine.queue.dead_letter_count() == 1
    assert engine.queue.size() == 1, "the storable neighbour must be retained"
    # A recent, routable span evicted purely for over-long duration is real
    # activity the server rejected for size (a lost carve-out that can overbill
    # the window) — it must surface as loss (warning), not a quiet benign flush.
    assert evicted["real_loss_count"] == 1
    assert evicted["unstorable_count"] == 0


def test_bucketless_and_stale_evictions_stay_benign_flushes():
    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)

    bucketless = {  # nowhere to route → genuinely unstorable, benign
        "id": "orphan", "timestamp": _now_iso(), "duration": 60.0, "data": {},
    }
    stale = {  # past the 7d retention window → benign
        "id": "old", "timestamp": "2000-01-01T00:00:00+00:00", "duration": 60.0,
        "bucket_id": "aw-watcher-window_h", "data": {},
    }
    engine.queue.enqueue([bucketless, stale])

    evicted = engine.queue.evict_unstorable()
    assert evicted["count"] == 2
    assert evicted["real_loss_count"] == 0, "bucketless / stale drops are benign"
    assert evicted["unstorable_count"] == 2


class _DurationStrictServer:
    """Real server model: 4xx the WHOLE batch if any event's duration is outside
    [0, 86400] (internal-tool2 AgentEventController). Bucketless events are
    ACCEPTED — the server types them off bucket_type."""

    def __init__(self) -> None:
        self.delivered: set[str] = set()

    def __call__(self, events: list[dict]) -> SyncResult:
        if any(not (0 <= e.get("duration", 0) <= MAX_EVENT_DURATION_SECONDS) for e in events):
            return SyncResult(success=False, events_queued=len(events), transient=False)
        for e in events:
            self.delivered.add(e["id"])
        return SyncResult(success=True, events_synced=len(events))


def _storable(n: int) -> list[dict]:
    now = _now_iso()
    return [
        {
            "id": f"good-{i}", "timestamp": now, "duration": 60.0,
            "bucket_id": "aw-watcher-window_h", "data": {"app": "Terminal"},
        }
        for i in range(n)
    ]


def test_oversize_poison_does_not_drop_storable_neighbours():
    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)
    host = engine._hostname

    oversize = {
        "id": "sleep_poison", "timestamp": _now_iso(),
        "duration": float(MAX_EVENT_DURATION_SECONDS + 7200),
        "bucket_id": f"bf-status_{host}", "bucket_type": "sleep_time",
        "data": {"status": "sleep"},
    }
    engine.queue.enqueue([oversize])   # oldest-first: sits at the batch head
    engine.queue.enqueue(_storable(9))
    assert engine.queue.size() == 10

    server = _DurationStrictServer()
    engine.bf.send_events = Mock(side_effect=server)

    for _ in range(8):  # well past max_retries=5
        engine._queue_backoff_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        engine._process_queue(SyncStats())

    good_ids = {f"good-{i}" for i in range(9)}
    assert good_ids <= server.delivered, (
        "storable activity must be delivered, not dragged to the drop ceiling "
        f"by a co-batched over-long poison span; delivered={server.delivered}"
    )
    assert engine.queue.failed_event_summary(max_retries=5)["real_loss_count"] == 0
    assert engine.queue.dead_letter_count() == 1  # the poison span, preserved


# --- classifier unit matrix ---------------------------------------------------


def test_is_event_storable_matrix():
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    recent = _now_iso()
    base = {"bucket_id": "b", "timestamp": recent, "duration": 60, "data": {}}

    assert is_event_storable(base, stale_cutoff=cutoff) is True
    # bucketless — kept unstorable (prod-proven rule; legacy spans are backfilled)
    assert is_event_storable({**base, "bucket_id": None}, stale_cutoff=cutoff) is False
    # over-long duration — the batch poison
    assert is_event_storable({**base, "duration": MAX_EVENT_DURATION_SECONDS + 1}, stale_cutoff=cutoff) is False
    # boundary values are fine
    assert is_event_storable({**base, "duration": 0}, stale_cutoff=cutoff) is True
    assert is_event_storable({**base, "duration": MAX_EVENT_DURATION_SECONDS}, stale_cutoff=cutoff) is True
    # negative duration — invalid
    assert is_event_storable({**base, "duration": -1}, stale_cutoff=cutoff) is False
    # missing/non-numeric duration — not our call to evict; leave to the server
    assert is_event_storable({**base, "duration": None}, stale_cutoff=cutoff) is True
    # bool is an int subclass but never a valid duration
    assert is_event_storable({**base, "duration": True}, stale_cutoff=cutoff) is False
    # stale timestamp — past retention
    assert is_event_storable({**base, "timestamp": "2000-01-01T00:00:00Z"}, stale_cutoff=cutoff) is False
    # non-dict
    assert is_event_storable(None, stale_cutoff=cutoff) is False
