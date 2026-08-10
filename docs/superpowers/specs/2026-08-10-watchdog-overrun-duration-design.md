# Watchdog overrun duration and phase — design

Status: **designed, ready to implement.** Closes the measurement half of
issue #179.

## Problem

`SyncCoordinator._watchdog()` (`src/main.py:1586`) already classifies an overrun
into *slow* (API unreachable) versus *hung* (no transient failures) — that is the
2026-07-22 outcome-classification design, and it shipped. What neither branch can
say is **how long the cycle actually ran**.

Over a 7-day window issue #179 reports:

```
Sync hung — exceeded 150s watchdog deadline                     (x34)
Sync slow — exceeded 150s while the BetterFlow API was unreachable
  (4 transient failures this cycle)                             (x1)
```

Every one of the 34 is indistinguishable from every other. A cycle that finished
at 150.9s and a cycle that deadlocked for twenty minutes produce the identical
row, and those need opposite fixes: the first is a budget that has grown past its
envelope, the second is the deadlock path `_acquire_sync_slot` exists to survive.

The in-file comment at `src/main.py:1378-1385` already records one confirmed
false fire — device 18, 2026-07-22, 150.86s, no log gap, all events delivered on
the next cycle — which is why the answer here is measurement and not a threshold
bump.

## Two constraints that determine the whole design

Both were established by reading the ingest, not assumed.

### The watchdog fires *at* the deadline, so it cannot know the duration

`threading.Timer(self._DO_SYNC_DEADLINE, _watchdog)` fires at 150s. Elapsed time
observed inside `_watchdog()` is therefore always ≈150s, whatever the cycle goes
on to do. The real duration is knowable only when the cycle ends.

### The backend aggregates by fingerprint and by nothing else

Traced in `betterqa-bot`:

- `context` **is** persisted to a `jsonb` column (`src/db/schema.ts:164-176`), but
  the row is a per-fingerprint aggregate and the upsert does
  `context = COALESCE(EXCLUDED.context, error_reports.context)`
  (`src/webhooks/error-ingest.ts:740`) — newest wins, per-occurrence history is
  not kept.
- The daily digest selects `message`, `level`, `occurrences` only
  (`src/scheduler/error-daily-summary.ts:119-137`) and renders
  `` `${r.occurrences}×` ${clip(r.message)} `` at `:176`. It never reads
  `context`. No percentile or numeric aggregation exists against
  `error_reports` anywhere.
- Grouping is `(project, fingerprint)` (`src/db/schema.ts:241`), and the agent's
  explicit `fingerprint` wins outright (`src/monitor/error-fingerprint.ts:66-68`).
  `message` is the one column overwritten unconditionally
  (`error-ingest.ts:736`) — newest wins.

So the literal reading of #179 — "record the elapsed time on every deadline
breach" via the payload's `context` — ships a number that survives exactly one
occurrence and appears in no report. **The only aggregation available is the
occurrence counter per fingerprint.** Any measurement that must be countable has
to ride in the fingerprint.

## Design

### 1. The predicate is elapsed, not "did the watchdog fire"

`_do_sync` stamps `cycle_started_at` at entry. In `finally` it computes
`elapsed = monotonic() - cycle_started_at` and emits an outcome report iff
`elapsed >= _DO_SYNC_DEADLINE`.

Deriving it from elapsed rather than from a `watchdog_fired` Event avoids a race
against the in-flight Timer thread: a watchdog that has passed its cancelled
check but not yet set a flag would otherwise leave `finally` reading "did not
fire" for a cycle that did. It also means **no test needs the 150s timer to
fire** — advancing a fake clock is sufficient.

### 2. Bucketed fingerprints carry the distribution

| elapsed | fingerprint | level |
|---|---|---|
| 150s ≤ e < 180s | `sync-watchdog-overrun-150-180` | `warning` |
| 180s ≤ e < 300s | `sync-watchdog-overrun-180-300` | `warning` |
| e ≥ 300s | `sync-watchdog-overrun-300plus` | `warning` |

The digest then reads as a distribution without any backend change:

```
Sync overran the 150s deadline — finished at 151.2s in phase 'sync'   (x31)
Sync overran the 150s deadline — finished at 246.0s in phase 'sync'   (x2)
Sync overran the 150s deadline — finished at 903.4s in phase 'sync'   (x1)
```

Each line's count is trustworthy; each line's *seconds* are a sample, because
message is overwritten newest-wins. That asymmetry is deliberate and must be
stated wherever these numbers get quoted — the bucket is the measurement, the
figure in the text is one arbitrary member of it.

**All three levels are `warning` on purpose.** The fire-time report already
paged this same cycle at 150s as `error` (`sync-watchdog-timeout`). A second
`error` at cycle end adds no paging value and doubles the alert volume this
change is meant to reduce. The outcome report measures; the fire-time report
pages.

Exact seconds also go in `context` (`elapsed_seconds`, `phase`) for the
`errors_detail` path, accepting that only the newest survives.

### 3. Phase

A per-cycle mutable holder, captured by the watchdog closure and read in
`finally`. **Never an instance attribute** — a cycle abandoned by
`_acquire_sync_slot`'s takeover keeps running, and an attribute would let that
zombie overwrite its successor's phase. This is the same reasoning the existing
code gives at `src/main.py:1578-1582` for capturing the transient counter as a
per-thread object.

Stages stamped in `_do_sync` only:

```
startup -> capture_health -> sync -> post_sync -> hours_fetch -> done
```

Honest limitation, stated up front: most overruns will report `sync`, because
`sync_engine.sync()` is where the retry chain and the send budget live. That is
still worth having — it positively rules out the other four stages, and it is
one file instead of instrumenting the ~1000-line `sync_engine.sync()` on the
hottest path in the repo. If the shipped distribution shows the time really is
all inside `sync`, deeper instrumentation becomes a follow-up with evidence
behind it rather than a guess.

Phase rides in both the fire-time reports (as `context`, where "where is it
stuck right now" is the question and newest-wins is acceptable) and the outcome
report (in message and context).

### 4. A zombie's late outcome report is a feature

A cycle abandoned at `_SYNC_WEDGE_CEILING` (420s) that eventually returns at,
say, 1400s reaches `finally` and reports `300plus` with its true elapsed. That
is precisely the "is it unbounded?" evidence #179 asks for, and `sync-wedged`
alone cannot supply it — `sync-wedged` records only that we gave up at 420s,
never what the real duration turned out to be.

## Unchanged by design

Called out so they read as deliberate:

- **`_DO_SYNC_DEADLINE` stays 150.** This is measurement, not tuning. Both the
  issue and the task brief explicitly forbid raising it to silence the alert.
- **`_SYNC_WEDGE_CEILING` stays 420**, fingerprint `sync-wedged`, still `error`.
- **Both fire-time fingerprints and levels keep their identity**
  (`sync-watchdog-timeout` / `sync-watchdog-timeout-offline`), so the existing
  34-occurrence group stays continuous and comparable across the change.
- `bf.reset_session()` / `aw.reset_session()` keep running in both watchdog
  branches.

## Testing

Against the real `SyncCoordinator._do_sync` with stub clients, asserting on
captured reports — never on arguments forwarded between functions
(`rules/test-fixture-discipline.md`).

**Clock:** patch `time.monotonic` at the `main` module level so *every* clock
read in the cycle moves together. This repo has already shipped a partial-clock
bomb — #131, where the reader took an injected `now=` but the sibling
`remove_failed` that wrote the timestamp did not, so the test broke the day wall
clock passed the fixture date. A whole-module fake has no such seam.

Required cases:

1. Overrun finishing at 151s → exactly one capture at
   `sync-watchdog-overrun-150-180`, level `warning`.
2. Overrun finishing at 246s → `sync-watchdog-overrun-180-300`.
3. Overrun finishing at 903s → `sync-watchdog-overrun-300plus`.
4. **Boundary: exactly 180.0s lands in `180-300`, not `150-180`.** Both ends of
   the range get a case (`diagnosis-discipline.md` Rule 3).
5. **Critical negative: a cycle finishing at 149s emits ZERO outcome captures.**
   This is the assertion that catches the predicate being implemented backwards,
   and it must be paired with a positive assertion that the capture list is
   non-empty in the overrun cases — an empty list satisfies "no outcome capture"
   for the wrong reason.
6. Phase attribution: an overrun stalled in `_monitor_capture_health` reports
   `capture_health`; one stalled in `sync_engine.sync()` reports `sync`.

**Guard witnessing.** Per `test-fixture-discipline.md` Phantom 5, each mechanism
gets its own mutant and must redden a distinct named test: (a) invert the
`elapsed >= deadline` comparison, (b) collapse the three buckets to one
fingerprint, (c) drop the phase stamp. Reverting all three at once proves
nothing about any one of them. Verify each mutation actually applied (`cmp`
before/after) before trusting a green run.

`tests/test_sync_watchdog_budget.py` and
`tests/test_sync_watchdog_outcome_classification.py` must stay green.

## Blast radius

One production file (`src/main.py`), one new test file. No change to
`sync_engine.py`, no schema change, no server change, no config flag, no new
dependency.

Failure mode if the elapsed predicate is inverted: an outcome report on every
healthy cycle, flooding the ingest. Test 5 exists to prevent exactly that.

## Repo hygiene shipped alongside

`docs/superpowers/specs/2026-07-22-watchdog-outcome-classification-design.md`
still reads `Status: designed, not scheduled` although it shipped — the
classification it describes is live at `src/main.py:1586-1622` with tests at
`tests/test_sync_watchdog_outcome_classification.py`. That stale line sent this
session looking for unimplemented work and will do the same to the next one. Its
status line gets corrected to point at the implementation.
