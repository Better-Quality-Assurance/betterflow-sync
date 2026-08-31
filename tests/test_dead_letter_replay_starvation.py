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


class TestUnreadableTimestampsAreNeverDeleted:
    """The Critical this branch was reworked to fix.

    An earlier version DELETED rows on `not _timestamp_within(...)`, which is
    False for a missing, null, wrong-typed or unparseable timestamp as well as a
    genuinely old one. Six rows were destroyed, including one captured an hour
    earlier, because the code had no evidence of their age and acted anyway.

    The property that matters is RETENTION, not the flag. These rows ARE marked
    terminal now — they can never be storable, since `is_event_storable` needs a
    timestamp that parses, and a dead-letter payload is immutable — but marking
    keeps them: still stored, still returned by `get_dead_letter_events`, still
    counted by `dead_letter_count`. A serialisation bug stays fully diagnosable.

    Asserting "not marked" was the wrong invariant: it left these rows holding
    slots in the replay ordering and starved it at 20,000
    (TestUnmarkableBacklogStillConverges below).
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
    def test_an_unreadable_timestamp_is_retained_and_inspectable(self, label, payload):
        self._dead_letter(payload)

        self.queue.requeue_storable_dead_letter(
            now=self.now + timedelta(hours=1), min_dead_age_seconds=0
        )

        assert self.queue.dead_letter_count() == 1, (
            f"{label}: the row was DELETED. This is the likely shape of a "
            "serialisation bug and is the only trace of it."
        )
        rows = self.queue.get_dead_letter_events()
        assert len(rows) == 1, f"{label}: not inspectable after marking"
        assert rows[0]["event_data"], f"{label}: payload lost"

    def test_a_provably_old_timestamp_is_marked_and_retained(self):
        self._dead_letter(
            {"timestamp": (self.now - timedelta(days=30)).isoformat()}
        )

        result = self.queue.requeue_storable_dead_letter(
            now=self.now + timedelta(hours=1), min_dead_age_seconds=0
        )

        assert result["marked_terminal"] == 1
        assert self.queue.dead_letter_count() == 1, "marking must never delete"

    def test_a_boolean_duration_does_not_exempt_an_ancient_row(self):
        """The bool guard stops True being read as the number 1. It must skip
        the DURATION branch only — returning early let an unrelated field
        exempt a provably-ancient row from marking."""
        self._dead_letter({
            "timestamp": (self.now - timedelta(days=400)).isoformat(),
            "duration": True,
        })

        result = self.queue.requeue_storable_dead_letter(
            now=self.now + timedelta(hours=1), min_dead_age_seconds=0
        )

        assert result["marked_terminal"] == 1, (
            "a boolean duration short-circuited the independent timestamp test"
        )

    def test_the_caller_window_cannot_widen_the_mark(self):
        """`stale_after_days` legitimately shrinks the SKIP window. It must not
        move the durable mark: a 10-day-old row is skipped under
        stale_after_days=1 but must NOT be marked terminal."""
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


class TestUnmarkableBacklogStillConverges:
    """The composition no test made: MORE THAN THE SCAN BUDGET of rows the rule
    refuses to mark, in front of one recoverable event.

    Every other convergence test here uses a MARKABLE class, and the
    unreadable-timestamp tests use a single row. Neither arrangement can see a
    backlog of unmarkable rows starving the scan — which is exactly the shape a
    systematic serialisation bug produces.
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

    def test_timestampless_backlog_past_the_budget_does_not_starve(self):
        # no timestamp at all: the class the old rule refused to mark
        for _ in range(21):
            self._dead_letter([
                {"duration": 60, "bucket_id": "bf-status_h"} for _ in range(1000)
            ])
        self._dead_letter([{
            "timestamp": self.now.isoformat(), "duration": 60,
            "bucket_id": "bf-status_h", "bucket_type": "idle_time", "data": {},
        }])
        total_before = self.queue.dead_letter_count()

        for cycle in range(1, 6):
            result = self.queue.requeue_storable_dead_letter(
                now=self.now + timedelta(hours=2 + cycle)
            )
            if result["requeued"] == 1:
                break
            assert result["marked_terminal"] > 0, (
                f"cycle {cycle} made no progress: {result}. Unmarkable rows are "
                "starving the replay — the original bug at a higher threshold."
            )
        else:
            pytest.fail("did not recover within 5 cycles")

        assert self.queue.dead_letter_count() == total_before - 1, (
            "only the requeued row may leave; nothing is deleted"
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
