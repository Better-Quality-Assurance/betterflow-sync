"""Terminal dead letters must not starve the replay.

`requeue_storable_dead_letter` selects candidates with
`ORDER BY dropped_at ASC LIMIT n` and then SKIPS rows that are no longer
storable — without removing them. A row whose timestamp has aged past
EVENT_RETENTION_DAYS can never become storable again (wall-clock only moves
forward), so it holds its slot in that ordering permanently.

Once a device accumulates `limit` such rows, every replay cycle examines the
same terminal rows and requeues nothing. Any genuinely recoverable event
dead-lettered afterwards then ages out within the retention window and is lost
for real — silent loss of billable activity, on a path whose whole purpose is
to preserve it.

Measured before the fix (default limit=200):
    terminal=  0 -> requeued 1
    terminal=199 -> requeued 1     <- positive control: the window still fits it
    terminal=200 -> requeued 0     <- starved
    terminal=500 -> requeued 0
"""

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.sync.queue import OfflineQueue


class TestTerminalRowsDoNotStarveReplay:
    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.queue = OfflineQueue(db_path=Path(self.temp_dir) / "q.db", max_size=100000)
        self.now = datetime.now(timezone.utc)

    def teardown_method(self):
        self.queue.close()

    def _ev(self, ts):
        return {"timestamp": ts.isoformat(), "duration": 60,
                "bucket_id": "bf-status_h", "bucket_type": "idle_time", "data": {}}

    def _dead_letter(self, events):
        self.queue.enqueue(events)
        ids = [e.id for e in self.queue.dequeue(batch_size=len(events))]
        for _ in range(5):
            self.queue.increment_retry(ids, "API error (422): rejected")
        self.queue.remove_failed(max_retries=5, last_error="generic")

    @pytest.mark.parametrize("n_terminal", [0, 199, 200, 500, 1000])
    def test_a_replayable_event_survives_any_number_of_terminal_rows(self, n_terminal):
        """The recoverable event must be requeued regardless of how many
        permanently-unstorable rows sit ahead of it in dropped_at order."""
        old = self.now - timedelta(days=30)  # past retention: terminal forever
        if n_terminal:
            self._dead_letter([self._ev(old) for _ in range(n_terminal)])
        self._dead_letter([self._ev(self.now)])  # genuinely replayable

        result = self.queue.requeue_storable_dead_letter(
            now=self.now + timedelta(hours=2)  # past the cooldown
        )

        assert result["requeued"] == 1, (
            f"{n_terminal} terminal rows starved the replay: {result}. "
            "A recoverable event will now age out of retention and be lost."
        )
        assert self.queue.size() == 1

    def test_repeated_cycles_still_make_progress(self):
        """Starvation is not merely slow — it never recovers. Two cycles with
        a terminal backlog must both deliver their replayable event."""
        old = self.now - timedelta(days=30)
        self._dead_letter([self._ev(old) for _ in range(400)])

        for cycle in range(2):
            self._dead_letter([self._ev(self.now)])
            result = self.queue.requeue_storable_dead_letter(
                now=self.now + timedelta(hours=2 + cycle)
            )
            assert result["requeued"] == 1, f"cycle {cycle} starved: {result}"
            self.queue.clear()


class TestBacklogConverges:
    """Beyond the scan budget a single cycle may spend itself entirely on
    pruning, so recovery takes more than one cycle. It must still CONVERGE —
    the backlog shrinks every cycle and the recoverable event gets through.
    Verified at 45,001 rows: recovered on cycle 3, table fully drained."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.queue = OfflineQueue(db_path=Path(self.temp_dir) / "q.db", max_size=300000)
        self.now = datetime.now(timezone.utc)

    def teardown_method(self):
        self.queue.close()

    def _ev(self, ts):
        return {"timestamp": ts.isoformat(), "duration": 60,
                "bucket_id": "bf-status_h", "bucket_type": "idle_time", "data": {}}

    def _dead_letter_chunked(self, ts, n, chunk=1000):
        for _ in range(0, n, chunk):
            batch = min(chunk, n)
            self.queue.enqueue([self._ev(ts) for _ in range(batch)])
            ids = [e.id for e in self.queue.dequeue(batch_size=batch)]
            for _ in range(5):
                self.queue.increment_retry(ids, "API error (422): rejected")
            self.queue.remove_failed(max_retries=5, last_error="generic")
            n -= batch

    def test_a_backlog_beyond_the_scan_budget_still_recovers(self):
        old = self.now - timedelta(days=30)
        self._dead_letter_chunked(old, 25000)
        self._dead_letter_chunked(self.now, 1)  # the recoverable one

        for cycle in range(1, 6):
            result = self.queue.requeue_storable_dead_letter(
                now=self.now + timedelta(hours=2 + cycle)
            )
            if result["requeued"] == 1:
                return  # converged
            assert result["pruned"] > 0, (
                f"cycle {cycle} made NO progress: {result}. The backlog is not "
                "shrinking, so this is permanent starvation, not slow recovery."
            )
        pytest.fail("did not recover within 5 cycles")
