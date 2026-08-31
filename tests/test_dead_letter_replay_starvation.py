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

from src.sync.queue import MAX_EVENT_DURATION_SECONDS, OfflineQueue


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
            assert result["marked_terminal"] > 0, (
                f"cycle {cycle} made NO progress: {result}. The candidate set is not "
                "shrinking, so this is permanent starvation, not slow recovery."
            )
        pytest.fail("did not recover within 5 cycles")


class TestUnreadableTimestampsAreNeverMarked:
    """The fixture class the first version of this fix did not have.

    That version marked-and-DELETED on `not _timestamp_within(...)`, which is
    False for a missing, null, wrong-typed or unparseable timestamp as well as
    for a genuinely old one — so "provably ancient" and "age unreadable"
    collapsed into one branch. Six rows with unreadable timestamps were deleted,
    including one captured an hour earlier.

    It survived its own test file because every fixture there built events
    through one helper that always emitted `ts.isoformat()`: the only two inputs
    the file could produce were the two where a correct rule and a broken one
    agree. These cases are the ones that disagree.
    """

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.queue = OfflineQueue(db_path=Path(self.temp_dir) / "q.db", max_size=1000)
        self.now = datetime.now(timezone.utc)

    def teardown_method(self):
        self.queue.close()

    def _dead_letter(self, extra):
        self.queue.enqueue([{"duration": 60, "bucket_id": "bf-status_h", **extra}])
        ids = [e.id for e in self.queue.dequeue(batch_size=1)]
        for _ in range(5):
            self.queue.increment_retry(ids, "API error (422): rejected")
        self.queue.remove_failed(max_retries=5, last_error="generic")

    @pytest.mark.parametrize("label,payload", [
        ("absent",        {}),
        ("null",          {"timestamp": None}),
        ("epoch int",     {"timestamp": 1756641600}),
        ("epoch float",   {"timestamp": 1756641600.5}),
        ("empty string",  {"timestamp": ""}),
        ("rfc1123",       {"timestamp": "Sun, 31 Aug 2026 11:00:00 GMT"}),
        ("nested dict",   {"timestamp": {"$date": "2026-08-31T11:00:00+00:00"}}),
    ])
    def test_an_unreadable_timestamp_is_retained_and_not_marked(self, label, payload):
        self._dead_letter(payload)

        self.queue.requeue_storable_dead_letter(
            now=self.now + timedelta(hours=1), min_dead_age_seconds=0
        )

        assert self.queue.dead_letter_count() == 1, (
            f"{label}: the row was DELETED — its age was never readable, so "
            "nothing justified acting on it"
        )
        with self.queue._cursor() as cur:
            flags = [r[0] for r in cur.execute(
                "SELECT terminal FROM dead_letter_events")]
        assert flags == [0], (
            f"{label}: marked terminal on an unreadable timestamp — this is the "
            "likely shape of a serialisation bug and must stay replayable"
        )

    def test_a_provably_old_timestamp_IS_marked(self):
        """The positive control: without this, the test above passes for a rule
        that never marks anything."""
        self._dead_letter(
            {"timestamp": (self.now - timedelta(days=30)).isoformat()}
        )

        result = self.queue.requeue_storable_dead_letter(
            now=self.now + timedelta(hours=1), min_dead_age_seconds=0
        )

        assert result["marked_terminal"] == 1
        assert self.queue.dead_letter_count() == 1, "marking must never delete"

    def test_the_caller_window_cannot_widen_the_mark(self):
        """`stale_after_days` legitimately shrinks the SKIP window, which is
        reversible. It must not move the durable mark: a 10-day-old row is
        skipped under stale_after_days=1 but must NOT be marked terminal."""
        self._dead_letter(
            {"timestamp": (self.now - timedelta(days=10)).isoformat()}
        )

        result = self.queue.requeue_storable_dead_letter(
            now=self.now + timedelta(hours=1),
            min_dead_age_seconds=0,
            stale_after_days=1,
        )

        assert result["marked_terminal"] == 0, (
            "a caller-supplied window moved the durable mark"
        )


class TestRecentButPermanentlyUnstorableAlsoConverges:
    """The second starvation class, found by a refutation reviewer.

    Marking used to key on timestamp AGE alone, so a row that is unstorable for
    a reason age cannot see — an over-long `duration`, or no `bucket_id` — was
    never marked and held its slot while its timestamp stayed recent. Since the
    queue holds MAX_QUEUE_SIZE (100,000) and the scan budget is 20,000, that is
    one full-queue drain away, not a theoretical bound. Measured before the fix:

        recent-unstorable=19999 -> requeued 1
        recent-unstorable=20000 -> requeued 0, marked 0   <- starved
        recent-unstorable=60000 -> requeued 0, marked 0

    Both reasons are PERMANENT: a dead-letter payload is immutable (the only
    UPDATE on that table sets the terminal flag) and `backfill_status_bucket_ids`
    repairs `queued_events` only. So they are marked on a value that was READ,
    which is the distinction that keeps this rule away from the deleted-data bug
    (see TestUnreadableTimestampsAreNeverMarked).
    """

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.queue = OfflineQueue(db_path=Path(self.temp_dir) / "q.db", max_size=200000)
        self.now = datetime.now(timezone.utc)

    def teardown_method(self):
        self.queue.close()

    def _dead_letter(self, events):
        self.queue.enqueue(events)
        ids = [e.id for e in self.queue.dequeue(batch_size=len(events))]
        for _ in range(5):
            self.queue.increment_retry(ids, "API error (422): rejected")
        self.queue.remove_failed(max_retries=5, last_error="generic")

    def _overlong(self):
        return {"timestamp": self.now.isoformat(),
                "duration": MAX_EVENT_DURATION_SECONDS + 1,
                "bucket_id": "bf-status_h", "bucket_type": "idle_time", "data": {}}

    def _recoverable(self):
        return {"timestamp": self.now.isoformat(), "duration": 60,
                "bucket_id": "bf-status_h", "bucket_type": "idle_time", "data": {}}

    def test_recent_overlong_rows_past_the_budget_still_converge(self):
        for _ in range(25):
            self._dead_letter([self._overlong() for _ in range(1000)])
        self._dead_letter([self._recoverable()])
        total_before = self.queue.dead_letter_count()

        for cycle in range(1, 6):
            result = self.queue.requeue_storable_dead_letter(
                now=self.now + timedelta(hours=2 + cycle)
            )
            if result["requeued"] == 1:
                break
            assert result["marked_terminal"] > 0, (
                f"cycle {cycle} made no progress: {result}. Recent-but-permanently"
                "-unstorable rows are starving the replay again."
            )
        else:
            pytest.fail("did not recover within 5 cycles")

        assert self.queue.dead_letter_count() == total_before - 1, (
            "marking must never delete — only the requeued row leaves"
        )

    def test_a_bucketless_row_is_marked_but_retained(self):
        self._dead_letter([{"timestamp": self.now.isoformat(), "duration": 60}])

        result = self.queue.requeue_storable_dead_letter(
            now=self.now + timedelta(hours=1), min_dead_age_seconds=0
        )

        assert result["marked_terminal"] == 1, "unroutable forever, so mark it"
        assert self.queue.dead_letter_count() == 1, "but never delete it"
