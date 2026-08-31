"""Tests for offline queue."""

import json
import pytest
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.sync.queue import (
    MAX_EVENT_DURATION_SECONDS,
    OfflineQueue,
    QueuedEvent,
)


class TestOfflineQueue:
    """Tests for OfflineQueue."""

    def setup_method(self):
        """Set up test fixtures."""
        # Use temp file for each test
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "test_queue.db"
        self.queue = OfflineQueue(db_path=self.db_path, max_size=100)

    def teardown_method(self):
        """Clean up."""
        self.queue.close()

    def test_enqueue_single_event(self):
        """Test enqueueing a single event."""
        events = [{"timestamp": "2026-02-18T10:00:00Z", "duration": 60, "data": {}}]

        count = self.queue.enqueue(events)

        assert count == 1
        assert self.queue.size() == 1

    def test_enqueue_multiple_events(self):
        """Test enqueueing multiple events."""
        events = [
            {"timestamp": "2026-02-18T10:00:00Z", "duration": 60, "data": {}},
            {"timestamp": "2026-02-18T10:01:00Z", "duration": 60, "data": {}},
            {"timestamp": "2026-02-18T10:02:00Z", "duration": 60, "data": {}},
        ]

        count = self.queue.enqueue(events)

        assert count == 3
        assert self.queue.size() == 3

    def test_dequeue_returns_oldest_first(self):
        """Test that dequeue returns events in FIFO order."""
        events = [
            {"timestamp": "2026-02-18T10:00:00Z", "data": {"order": 1}},
            {"timestamp": "2026-02-18T10:01:00Z", "data": {"order": 2}},
        ]
        self.queue.enqueue(events)

        queued = self.queue.dequeue(batch_size=1)

        assert len(queued) == 1
        assert queued[0].event_data["data"]["order"] == 1

    def test_dequeue_respects_batch_size(self):
        """Test that dequeue respects batch size limit."""
        events = [{"timestamp": f"2026-02-18T10:0{i}:00Z", "data": {}} for i in range(10)]
        self.queue.enqueue(events)

        queued = self.queue.dequeue(batch_size=3)

        assert len(queued) == 3

    def test_remove_events(self):
        """Test removing events by ID."""
        events = [{"timestamp": "2026-02-18T10:00:00Z", "data": {}}]
        self.queue.enqueue(events)

        queued = self.queue.dequeue(batch_size=1)
        removed = self.queue.remove([q.id for q in queued])

        assert removed == 1
        assert self.queue.is_empty()

    def test_increment_retry_count(self):
        """Test incrementing retry count."""
        events = [{"timestamp": "2026-02-18T10:00:00Z", "data": {}}]
        self.queue.enqueue(events)

        queued = self.queue.dequeue(batch_size=1)
        self.queue.increment_retry([q.id for q in queued])

        # Dequeue again and check retry count
        queued = self.queue.dequeue(batch_size=1)
        assert queued[0].retry_count == 1

    def test_remove_failed_events(self):
        """Test removing events that exceeded max retries."""
        events = [{"timestamp": "2026-02-18T10:00:00Z", "data": {}}]
        self.queue.enqueue(events)

        # Simulate multiple retries
        queued = self.queue.dequeue(batch_size=1)
        for _ in range(5):
            self.queue.increment_retry([q.id for q in queued])

        # Before removal, the about-to-drop events are summarized for ops
        # (read-only — must not delete them).
        summary = self.queue.failed_event_summary(max_retries=5)
        assert summary["count"] == 1
        assert self.queue.size() == 1, "failed_event_summary must not delete"

        removed = self.queue.remove_failed(max_retries=5)

        assert removed == 1
        assert self.queue.is_empty()

    def test_remove_failed_moves_to_dead_letter_not_lost(self):
        """A 4xx-exhausted event must be MOVED to dead_letter_events, not hard
        deleted — nothing is lost. Its event_data, bucket_id, retry_count and a
        dropped_at stamp are preserved, and it leaves queued_events."""
        event = {
            "id": "real-activity",
            "bucket_id": "aw-watcher-afk_host",
            "timestamp": "2026-07-09T05:00:00+00:00",
            "duration": 42,
            "data": {"status": "not-afk"},
        }
        self.queue.enqueue([event])
        queued = self.queue.dequeue(batch_size=1)
        for _ in range(5):
            self.queue.increment_retry([q.id for q in queued])

        assert self.queue.dead_letter_count() == 0
        moved = self.queue.remove_failed(
            max_retries=5, last_error="definitive 4xx"
        )

        # Removed from the live queue...
        assert moved == 1
        assert self.queue.is_empty()
        # ...but preserved in the dead-letter table, nothing lost.
        assert self.queue.dead_letter_count() == 1
        dead = self.queue.get_dead_letter_events()
        assert len(dead) == 1
        row = dead[0]
        assert row["bucket_id"] == "aw-watcher-afk_host"
        assert row["retry_count"] >= 5
        assert row["last_error"] == "definitive 4xx"
        assert row["dropped_at"] is not None
        # Original payload is intact and replayable.
        import json
        restored = json.loads(row["event_data"])
        assert restored["id"] == "real-activity"
        assert restored["data"]["status"] == "not-afk"

    def test_remove_failed_no_events_leaves_dead_letter_empty(self):
        """Nothing past the ceiling → nothing moved, dead-letter stays empty."""
        self.queue.enqueue([
            {"id": "fresh", "timestamp": "2026-07-09T05:00:00+00:00", "data": {}}
        ])
        assert self.queue.remove_failed(max_retries=5) == 0
        assert self.queue.dead_letter_count() == 0
        assert self.queue.size() == 1

    def test_requeue_storable_dead_letter_resurrects_only_storable(self):
        """The bounded dead-letter replay must resurrect a dead-lettered row that
        is storable AGAIN (bucket + timestamp within retention + duration inside
        the server's bounds) while leaving genuinely-unstorable rows in the
        dead-letter table. Mirrors evict_unstorable's classification, so a row the
        server would still reject is never resurrected."""
        now = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)
        recent_ts = (now - timedelta(hours=1)).isoformat()
        stale_ts = (now - timedelta(days=30)).isoformat()

        storable = {
            "id": "storable-1", "bucket_id": "aw-watcher-afk_host",
            "timestamp": recent_ts, "duration": 42, "data": {"status": "not-afk"},
        }
        no_bucket = {"id": "nobucket-1", "timestamp": recent_ts, "data": {}}
        stale = {
            "id": "stale-1", "bucket_id": "aw-watcher-window_host",
            "timestamp": stale_ts, "data": {},
        }
        # The weekend lid-close span: recent AND routable, so the bucket+timestamp
        # pair alone calls it storable — but its duration is past the server's
        # accepted bound, which is exactly why evict_unstorable dead-lettered it.
        # Resurrecting it re-poisons the batch it rides in and drags its healthy
        # neighbours to the drop ceiling, then re-evicts with a fresh dropped_at
        # so the cooldown restarts and the loop runs every 30 min for the whole
        # retention window.
        over_long = {
            "id": "overlong-1", "bucket_id": "bf-status_host",
            "timestamp": recent_ts,
            "duration": MAX_EVENT_DURATION_SECONDS + 3600,
            "data": {"status": "sleep"},
        }

        # Drive them to the retry ceiling and let remove_failed dead-letter all
        # four (a poison batch bumps storable + unstorable together — exactly
        # what evict_unstorable/#130 exists to prevent, but historical rows and
        # genuine 4xx-rejected storable events still land here).
        self.queue.enqueue([storable, no_bucket, stale, over_long])
        queued = self.queue.dequeue(batch_size=10)
        for _ in range(5):
            self.queue.increment_retry([q.id for q in queued])
        # now= is REQUIRED, not tidiness: dropped_at is what the replay's
        # cooldown filters on, so a real-clock stamp against an injected reader
        # clock makes this test fail on a date, not on a defect.
        self.queue.remove_failed(
            max_retries=5, last_error="poison batch", now=now
        )
        assert self.queue.dead_letter_count() == 4
        assert self.queue.is_empty()

        # min_dead_age_seconds=0 isolates the storable/unstorable classification
        # from the cooldown gate (exercised separately below).
        result = self.queue.requeue_storable_dead_letter(now=now, min_dead_age_seconds=0)

        # Only the storable row returned to the live queue.
        assert result["requeued"] == 1
        assert result["skipped_unstorable"] == 3
        assert self.queue.size() == 1
        # The three unstorable rows stay dead-lettered — not resurrected, not lost.
        # 2, not 3: one of the three skipped rows is past retention + the prune
        # margin, so the replay scan pruned it on the way through. That pruning
        # is what stops terminal rows piling up at the head of the candidate
        # ordering and starving the replay - see
        # tests/test_dead_letter_replay_starvation.py.
        assert result["pruned"] == 1
        assert self.queue.dead_letter_count() == 2
        assert {
            json.loads(row["event_data"])["id"]
            for row in self.queue.get_dead_letter_events()
        } == {"nobucket-1", "overlong-1"}
        # "stale-1" is gone: past retention + the prune margin, so terminal.
        # The other two are unstorable but RECENT — a missing bucket_id or an
        # over-long duration is repairable by a code change, so those are NOT
        # pruned. The prune rule is bounded strictly by timestamp age.
        # The resurrected event is the storable one, retry_count reset so it gets
        # a fresh delivery attempt (not instantly re-dropped at the ceiling).
        back = self.queue.dequeue(batch_size=10)
        assert len(back) == 1
        assert back[0].event_data["id"] == "storable-1"
        assert back[0].retry_count == 0

    def test_requeue_storable_dead_letter_bounded_and_no_double_resurrect(self):
        """Bounded by ``limit``; MOVE semantics mean a resurrected row leaves the
        dead-letter table, so it can never be requeued twice (no double-send)."""
        now = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)
        recent_ts = (now - timedelta(hours=1)).isoformat()
        events = [
            {"id": f"s{i}", "bucket_id": "aw-watcher-afk_h",
             "timestamp": recent_ts, "data": {}}
            for i in range(5)
        ]
        self.queue.enqueue(events)
        queued = self.queue.dequeue(batch_size=10)
        for _ in range(5):
            self.queue.increment_retry([q.id for q in queued])
        self.queue.remove_failed(max_retries=5, now=now)
        assert self.queue.dead_letter_count() == 5

        first = self.queue.requeue_storable_dead_letter(
            now=now, limit=2, min_dead_age_seconds=0
        )
        assert first["requeued"] == 2
        assert self.queue.dead_letter_count() == 3  # 2 moved out
        assert self.queue.size() == 2

        # Drain the requeued ones, then resurrect the rest. Total resurrected
        # across both calls is exactly 5 — the first two are never re-moved.
        self.queue.clear()
        second = self.queue.requeue_storable_dead_letter(
            now=now, limit=100, min_dead_age_seconds=0
        )
        assert second["requeued"] == 3
        assert self.queue.dead_letter_count() == 0
        assert self.queue.size() == 3

    def test_requeue_storable_dead_letter_cooldown_holds_fresh_drops(self):
        """A row must sit past the cooldown before the replay retries it — so a
        batch ``remove_failed`` JUST dropped for a genuine definitive rejection is
        not instantly resurrected into the same 4xx (which would re-block the
        queue head and thrash drop→resurrect→drop). Once the cooldown elapses the
        SAME storable row IS resurrected."""
        event = {
            "id": "storable-cd", "bucket_id": "aw-watcher-afk_h",
            "timestamp": datetime.now(timezone.utc).isoformat(), "data": {},
        }
        self.queue.enqueue([event])
        queued = self.queue.dequeue(batch_size=10)
        for _ in range(5):
            self.queue.increment_retry([q.id for q in queued])
        self.queue.remove_failed(max_retries=5)  # dropped_at ~= real now
        assert self.queue.dead_letter_count() == 1

        # Immediately (well within the 30-min cooldown): held, not resurrected.
        held = self.queue.requeue_storable_dead_letter(min_dead_age_seconds=1800)
        assert held["requeued"] == 0
        assert self.queue.dead_letter_count() == 1
        assert self.queue.is_empty()

        # After the cooldown has elapsed (evaluate with a future `now`): the same
        # storable row is resurrected for another attempt.
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        aged = self.queue.requeue_storable_dead_letter(
            now=future, min_dead_age_seconds=1800
        )
        assert aged["requeued"] == 1
        assert self.queue.dead_letter_count() == 0
        assert self.queue.size() == 1

    def _dead_letter_storable(self, queue, count: int, now: datetime) -> None:
        """Drive ``count`` storable events to the ceiling and dead-letter them."""
        recent_ts = (now - timedelta(hours=1)).isoformat()
        queue.enqueue([
            {"id": f"dl{i}", "bucket_id": "aw-watcher-afk_h",
             "timestamp": recent_ts, "duration": 30, "data": {}}
            for i in range(count)
        ])
        queued = queue.dequeue(batch_size=1000)
        for _ in range(5):
            queue.increment_retry([q.id for q in queued])
        queue.remove_failed(max_retries=5, now=now)

    def test_requeue_storable_dead_letter_respects_max_size(self):
        """The replay raw-INSERTs into queued_events, skipping the max_size check
        and oldest-eviction that enqueue() performs. dead_letter_events is never
        pruned, so on the first post-upgrade drain a device carrying weeks of
        dead letters resurrects up to `limit` rows per cycle straight past
        max_size — and capacity_percent() then pins at 1.0 permanently.

        The replay must take only the headroom that exists. It must NOT reuse
        enqueue()'s oldest-eviction: that would delete live, never-rejected
        events to make room for already-rejected ones.
        """
        now = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)
        path = Path(self.temp_dir) / "cap.db"
        queue = OfflineQueue(db_path=path, max_size=10)
        try:
            self._dead_letter_storable(queue, 8, now)
            assert queue.dead_letter_count() == 8

            # 8 live events already in the queue: headroom is 2, not 8.
            live = [
                {"id": f"live{i}", "bucket_id": "aw-watcher-window_h",
                 "timestamp": (now - timedelta(minutes=5)).isoformat(),
                 "duration": 10, "data": {}}
                for i in range(8)
            ]
            queue.enqueue(live)
            assert queue.size() == 8

            result = queue.requeue_storable_dead_letter(now=now, min_dead_age_seconds=0)

            assert queue.size() <= 10, (
                "the replay resurrected past max_size; capacity_percent() now "
                "pins at 1.0 and every capacity signal is dead"
            )
            assert result["requeued"] == 2
            # The 6 that did not fit are still preserved, not lost.
            assert queue.dead_letter_count() == 6
            # And no live event was evicted to make room for a dead-lettered one.
            back = queue.dequeue(batch_size=100)
            assert sum(1 for q in back if q.event_data["id"].startswith("live")) == 8
        finally:
            queue.close()

    def test_requeue_storable_dead_letter_at_capacity_resurrects_nothing(self):
        """No headroom at all → the replay is a clean no-op. Rows stay
        dead-lettered for a later cycle; nothing is dropped to make room."""
        now = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)
        path = Path(self.temp_dir) / "full.db"
        queue = OfflineQueue(db_path=path, max_size=5)
        try:
            self._dead_letter_storable(queue, 4, now)
            queue.enqueue([
                {"id": f"live{i}", "bucket_id": "aw-watcher-window_h",
                 "timestamp": (now - timedelta(minutes=5)).isoformat(),
                 "duration": 10, "data": {}}
                for i in range(5)
            ])
            assert queue.size() == 5
            assert queue.capacity_percent() == 1.0

            result = queue.requeue_storable_dead_letter(now=now, min_dead_age_seconds=0)

            assert result["requeued"] == 0
            assert queue.size() == 5
            assert queue.dead_letter_count() == 4
        finally:
            queue.close()

    def test_requeue_storable_dead_letter_empty_is_noop(self):
        """Nothing dead-lettered → a clean zero result, no error."""
        result = self.queue.requeue_storable_dead_letter()
        assert result["requeued"] == 0
        assert result["skipped_unstorable"] == 0

    def test_failed_event_summary_reports_buckets_and_span(self):
        """The drop summary carries enough to diagnose the loss: count, the
        distinct bucket_ids affected, and the oldest/newest event timestamps."""
        events = [
            {"id": "e1", "bucket_id": "aw-watcher-window_h", "timestamp": "2026-06-23T05:00:00Z", "data": {}},
            {"id": "e2", "bucket_id": "aw-watcher-afk_h", "timestamp": "2026-06-23T05:10:00Z", "data": {}},
        ]
        self.queue.enqueue(events)
        queued = self.queue.dequeue(batch_size=10)
        for _ in range(5):
            self.queue.increment_retry([q.id for q in queued])

        summary = self.queue.failed_event_summary(max_retries=5)

        assert summary["count"] == 2
        assert summary["bucket_ids"] == ["aw-watcher-afk_h", "aw-watcher-window_h"]
        assert summary["oldest"] == "2026-06-23T05:00:00Z"
        assert summary["newest"] == "2026-06-23T05:10:00Z"

    def test_failed_event_summary_clean_queue(self):
        """No events past the ceiling → count 0, empty fields."""
        self.queue.enqueue([{"id": "fresh", "timestamp": "2026-06-23T05:00:00Z", "data": {}}])
        summary = self.queue.failed_event_summary(max_retries=5)
        assert summary == {
            "count": 0, "bucket_ids": [], "oldest": None, "newest": None,
            "real_loss_count": 0, "unstorable_count": 0, "last_errors": [],
        }

    def _drop_n(self, events):
        """Enqueue events and push them past the retry ceiling so they count as
        about-to-drop in failed_event_summary."""
        self.queue.enqueue(events)
        queued = self.queue.dequeue(batch_size=len(events))
        for _ in range(5):
            self.queue.increment_retry([q.id for q in queued])

    def test_failed_event_summary_classifies_stale_as_unstorable(self):
        """Events older than the storable window (~7d) are unstorable — the
        server legitimately rejects them, so dropping is a benign flush, not
        real loss. (Diana's 06-16/06-17 batches.)"""
        now = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)
        self._drop_n([
            {"id": "a", "bucket_id": "aw-watcher-window_h", "timestamp": "2026-06-16T15:56:00+00:00", "duration": 60, "data": {}},
            {"id": "b", "bucket_id": "aw-watcher-input_h", "timestamp": "2026-06-17T06:04:00+00:00", "duration": 30, "data": {}},
        ])
        summary = self.queue.failed_event_summary(max_retries=5, now=now)
        assert summary["count"] == 2
        assert summary["real_loss_count"] == 0
        assert summary["unstorable_count"] == 2

    def test_failed_event_summary_classifies_no_bucket_as_unstorable(self):
        """A recent event with no bucket_id can't be routed/stored → unstorable
        (the 2026-06-19 buckets=unknown drop)."""
        now = datetime(2026, 6, 19, 15, 0, tzinfo=timezone.utc)
        self._drop_n([
            {"id": "c", "timestamp": "2026-06-19T14:50:37+00:00", "duration": 60, "data": {}},
        ])
        summary = self.queue.failed_event_summary(max_retries=5, now=now)
        assert summary["count"] == 1
        assert summary["real_loss_count"] == 0
        assert summary["unstorable_count"] == 1

    def test_failed_event_summary_counts_recent_bucketed_as_real_loss(self):
        """Recent + bucketed = genuine lost activity the server should have
        accepted → real loss (warning-worthy)."""
        now = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)
        self._drop_n([
            {"id": "d", "bucket_id": "aw-watcher-window_h", "timestamp": "2026-06-24T07:30:00+00:00", "duration": 60, "data": {}},
        ])
        summary = self.queue.failed_event_summary(max_retries=5, now=now)
        assert summary["real_loss_count"] == 1
        assert summary["unstorable_count"] == 0

    def test_failed_event_summary_mixed_real_and_unstorable(self):
        """A mix → counts split; the caller escalates because real loss > 0."""
        now = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)
        self._drop_n([
            {"id": "e", "bucket_id": "aw-watcher-window_h", "timestamp": "2026-06-24T07:30:00+00:00", "duration": 60, "data": {}},
            {"id": "f", "bucket_id": "aw-watcher-window_h", "timestamp": "2026-06-10T07:30:00+00:00", "duration": 60, "data": {}},
            {"id": "g", "timestamp": "2026-06-24T07:31:00+00:00", "duration": 60, "data": {}},
        ])
        summary = self.queue.failed_event_summary(max_retries=5, now=now)
        assert summary["real_loss_count"] == 1
        assert summary["unstorable_count"] == 2

    def test_max_size_enforcement(self):
        """Test that queue enforces max size."""
        # Use a separate DB for this test
        small_db_path = Path(self.temp_dir) / "small_queue.db"
        queue = OfflineQueue(db_path=small_db_path, max_size=5)

        # Add 10 events
        events = [{"timestamp": f"2026-02-18T10:{i:02d}:00Z", "data": {"i": i}} for i in range(10)]
        queue.enqueue(events)

        # Should only have 5 events (newest)
        assert queue.size() == 5
        queue.close()

    def test_clear_queue(self):
        """Test clearing the queue."""
        events = [{"timestamp": "2026-02-18T10:00:00Z", "data": {}}]
        self.queue.enqueue(events)

        cleared = self.queue.clear()

        assert cleared == 1
        assert self.queue.is_empty()

    def test_checkpoint_get_set(self):
        """Test setting and getting checkpoints."""
        bucket_id = "aw-watcher-window_test"
        timestamp = datetime.now(timezone.utc)

        self.queue.set_checkpoint(bucket_id, timestamp)
        loaded = self.queue.get_checkpoint(bucket_id)

        assert loaded is not None
        # Compare without microseconds (SQLite precision)
        assert loaded.replace(microsecond=0) == timestamp.replace(microsecond=0)

    def test_checkpoint_update(self):
        """Test updating an existing checkpoint."""
        bucket_id = "aw-watcher-window_test"
        old_time = datetime.now(timezone.utc) - timedelta(hours=1)
        new_time = datetime.now(timezone.utc)

        self.queue.set_checkpoint(bucket_id, old_time)
        self.queue.set_checkpoint(bucket_id, new_time)

        loaded = self.queue.get_checkpoint(bucket_id)
        assert loaded.replace(microsecond=0) == new_time.replace(microsecond=0)

    def test_checkpoint_forward_advances_on_newer(self):
        """set_checkpoint_forward moves the checkpoint forward (#5a)."""
        bucket_id = "aw-watcher-window_test"
        old_time = datetime.now(timezone.utc) - timedelta(hours=1)
        new_time = datetime.now(timezone.utc)

        self.queue.set_checkpoint_forward(bucket_id, old_time)
        self.queue.set_checkpoint_forward(bucket_id, new_time)

        loaded = self.queue.get_checkpoint(bucket_id)
        assert loaded.replace(microsecond=0) == new_time.replace(microsecond=0)

    def test_checkpoint_forward_is_noop_on_older(self):
        """set_checkpoint_forward never rewinds a checkpoint backward (#5a).

        Guards the doc's "silently re-scan from a 2-day-old checkpoint" class:
        a lookback re-fetch whose last event end predates the stored checkpoint
        must not drag it back.
        """
        bucket_id = "aw-watcher-window_test"
        new_time = datetime.now(timezone.utc)
        old_time = new_time - timedelta(days=2)

        self.queue.set_checkpoint_forward(bucket_id, new_time)
        self.queue.set_checkpoint_forward(bucket_id, old_time)  # must be ignored

        loaded = self.queue.get_checkpoint(bucket_id)
        assert loaded.replace(microsecond=0) == new_time.replace(microsecond=0)

    def test_checkpoint_forward_seeds_when_absent(self):
        """First forward write creates the row (no existing checkpoint)."""
        bucket_id = "aw-watcher-window_test"
        ts = datetime.now(timezone.utc)

        self.queue.set_checkpoint_forward(bucket_id, ts)

        loaded = self.queue.get_checkpoint(bucket_id)
        assert loaded is not None
        assert loaded.replace(microsecond=0) == ts.replace(microsecond=0)

    def test_checkpoint_forward_equal_is_noop(self):
        """An equal timestamp is not 'forward' — keeps the stored event_id."""
        bucket_id = "aw-watcher-window_test"
        ts = datetime(2026, 6, 20, 10, 0, 0, tzinfo=timezone.utc)

        self.queue.set_checkpoint_forward(bucket_id, ts, event_id=1)
        self.queue.set_checkpoint_forward(bucket_id, ts, event_id=2)

        loaded = self.queue.get_checkpoint(bucket_id)
        assert loaded.replace(microsecond=0) == ts.replace(microsecond=0)

    def test_get_nonexistent_checkpoint(self):
        """Test getting a checkpoint that doesn't exist."""
        loaded = self.queue.get_checkpoint("nonexistent_bucket")
        assert loaded is None

    def test_get_all_checkpoints(self):
        """Test getting all checkpoints."""
        now = datetime.now(timezone.utc)
        self.queue.set_checkpoint("bucket1", now)
        self.queue.set_checkpoint("bucket2", now)

        checkpoints = self.queue.get_all_checkpoints()

        assert len(checkpoints) == 2
        assert "bucket1" in checkpoints
        assert "bucket2" in checkpoints

    def test_is_empty(self):
        """Test is_empty method."""
        assert self.queue.is_empty() is True

        self.queue.enqueue([{"timestamp": "2026-02-18T10:00:00Z", "data": {}}])
        assert self.queue.is_empty() is False

    def test_capacity_percent_empty(self):
        """Test capacity_percent returns 0 for empty queue."""
        assert self.queue.capacity_percent() == 0.0

    def test_capacity_percent_partial(self):
        """Test capacity_percent with partial fill."""
        # Queue max_size is 100, add 25 events
        events = [{"timestamp": f"2026-02-18T10:{i:02d}:00Z", "data": {}} for i in range(25)]
        self.queue.enqueue(events)

        assert self.queue.capacity_percent() == 0.25

    def test_capacity_percent_full(self):
        """Test capacity_percent at max capacity."""
        # Queue max_size is 100, fill completely
        events = [{"timestamp": f"2026-02-18T10:{i:02d}:00Z", "data": {}} for i in range(100)]
        self.queue.enqueue(events)

        assert self.queue.capacity_percent() == 1.0

    def test_is_near_capacity_false(self):
        """Test is_near_capacity returns False below threshold."""
        # Add 50 events (50% capacity)
        events = [{"timestamp": f"2026-02-18T10:{i:02d}:00Z", "data": {}} for i in range(50)]
        self.queue.enqueue(events)

        assert self.queue.is_near_capacity() is False
        assert self.queue.is_near_capacity(threshold=0.8) is False

    def test_is_near_capacity_true(self):
        """Test is_near_capacity returns True at/above threshold."""
        # Add 80 events (80% capacity)
        events = [{"timestamp": f"2026-02-18T10:{i:02d}:00Z", "data": {}} for i in range(80)]
        self.queue.enqueue(events)

        assert self.queue.is_near_capacity() is True
        assert self.queue.is_near_capacity(threshold=0.8) is True

    def test_is_near_capacity_custom_threshold(self):
        """Test is_near_capacity with custom threshold."""
        # Add 50 events (50% capacity)
        events = [{"timestamp": f"2026-02-18T10:{i:02d}:00Z", "data": {}} for i in range(50)]
        self.queue.enqueue(events)

        assert self.queue.is_near_capacity(threshold=0.5) is True
        assert self.queue.is_near_capacity(threshold=0.6) is False

    # Per-event counted-time persistence (restart-safe time dedup)

    def test_counted_time_roundtrip(self):
        """set_counted_time then get_counted_times returns the value."""
        self.queue.set_counted_time("aw-watcher-window_host", "42", 600.0, "2026-06-15")
        got = self.queue.get_counted_times("2026-06-15")
        assert got == {("aw-watcher-window_host", "42"): 600.0}

    def test_counted_time_upsert_overwrites(self):
        """Re-writing the same event updates the cumulative, not appends."""
        self.queue.set_counted_time("b", "1", 100.0, "2026-06-15")
        self.queue.set_counted_time("b", "1", 250.0, "2026-06-15")
        got = self.queue.get_counted_times("2026-06-15")
        assert got == {("b", "1"): 250.0}

    def test_counted_time_scoped_by_day(self):
        """get_counted_times only returns rows for the requested day."""
        self.queue.set_counted_time("b", "1", 100.0, "2026-06-14")
        self.queue.set_counted_time("b", "2", 200.0, "2026-06-15")
        assert self.queue.get_counted_times("2026-06-15") == {("b", "2"): 200.0}
        assert self.queue.get_counted_times("2026-06-14") == {("b", "1"): 100.0}

    def test_counted_time_survives_reopen(self):
        """Counts persist across an OfflineQueue restart (same db file)."""
        self.queue.set_counted_time("b", "1", 123.0, "2026-06-15")
        self.queue.close()
        reopened = OfflineQueue(db_path=self.db_path, max_size=100)
        try:
            assert reopened.get_counted_times("2026-06-15") == {("b", "1"): 123.0}
        finally:
            reopened.close()
        # Re-point self.queue so teardown's close() doesn't double-close.
        self.queue = OfflineQueue(db_path=self.db_path, max_size=100)

    def test_prune_counted_time_drops_older_days(self):
        """prune_counted_time removes rows strictly before the cutoff day."""
        self.queue.set_counted_time("b", "1", 1.0, "2026-06-13")
        self.queue.set_counted_time("b", "2", 2.0, "2026-06-14")
        self.queue.set_counted_time("b", "3", 3.0, "2026-06-15")
        removed = self.queue.prune_counted_time("2026-06-15")
        assert removed == 2
        assert self.queue.get_counted_times("2026-06-15") == {("b", "3"): 3.0}
        assert self.queue.get_counted_times("2026-06-14") == {}
