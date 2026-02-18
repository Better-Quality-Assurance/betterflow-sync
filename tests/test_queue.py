"""Tests for offline queue."""

import pytest
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.sync.queue import OfflineQueue, QueuedEvent


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

        removed = self.queue.remove_failed(max_retries=5)

        assert removed == 1
        assert self.queue.is_empty()

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
