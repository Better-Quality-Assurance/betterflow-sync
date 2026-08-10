# Watchdog overrun duration and phase — design

Status: **implemented.** Closes the measurement half of issue #179. The bands
and the cycle-end outcome report are live in `SyncCoordinator._do_sync` /
`_report_overrun_outcome` and the module-level `_overrun_fingerprint`
(`src/main.py`, fingerprints `sync-watchdog-overrun-marginal` /
`-moderate` / `-severe`), guarded by `tests/test_watchdog_overrun_bands.py`,
`tests/test_watchdog_cycle_phase.py` and
`tests/test_watchdog_overrun_outcome.py`.

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
fire" for a cycle that did. It also means **no test needs the Timer to fire at
all** — a sub-second sleep past a shrunk deadline is sufficient, so the tests
never depend on thread scheduling to produce their result.

### 2. Bucketed fingerprints carry the distribution

Bands are expressed as **multiples of the deadline**, not absolute seconds.

| elapsed | fingerprint | at the 150s deadline | level |
|---|---|---|---|
| 1.0× ≤ e < 1.2× | `sync-watchdog-overrun-marginal` | 150–180s | `warning` |
| 1.2× ≤ e < 2.0× | `sync-watchdog-overrun-moderate` | 180–300s | `warning` |
| e ≥ 2.0× | `sync-watchdog-overrun-severe` | ≥300s | `warning` |

Ratios rather than absolute seconds for two concrete reasons. The deadline has
already moved once (120s → 150s), and absolute bands would have quietly become
wrong rather than failing. And `tests/test_sync_watchdog_outcome_classification.py`
shrinks the deadline to 0.3s through an instance override, so absolute bands
would put every test cycle in the first bucket and leave the other two
unreachable without a five-minute sleep — the measurement would ship untested.

Selection lives in a pure module-level `_overrun_fingerprint(elapsed, deadline)`,
which is what makes the boundaries testable *exactly*: a timed cycle cannot land
on 1.2× reliably, a unit test can.

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

**The outcome report opts out of the client-side dedup** (`dedup_window=0` on
its `capture` call). `ErrorReporter` suppresses a repeated fingerprint within
300s, which was uniform while every overrun shared one fingerprint. Split into
three bands it is not: the marginal band (150–180s) puts the next tick roughly
180s later and loses about 40% of its reports to that window, while the severe
band (≥300s) can never be throttled at all. The digest would then overstate
severe's share by ~1.5–2× — the wrong direction for a question whose known
failure mode is a *false* "hung" (the 150.86s false fire above). Since the
counter is the whole measurement, throttling it non-uniformly is not a volume
control, it is a corrupted statistic. Volume stays bounded by construction: the
report fires only on an overrun, an overrun is ≥150s, and overlapping ticks are
skipped by `_acquire_sync_slot` — so ~24/hour/device at worst, all `warning`,
none of it paging. The fire-time report keeps the default window.

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

**But this argument holds only for cycles that eventually RETURN.** A true
deadlock never reaches `finally`, so it emits no outcome report at all and is
counted in no band. The severe band therefore sizes the population of *slow but
finishing* cycles, never the wedged population — which is the half of #179 most
worth counting. `sync-wedged` remains the only signal for that, and the two have
to be read together: `severe = 1` means one cycle ran past 300s **and came
back**, not that one device wedged.

## Unchanged by design

Called out so they read as deliberate:

- **`_DO_SYNC_DEADLINE` stays 150.** This is measurement, not tuning. Both the
  issue and the task brief explicitly forbid raising it to silence the alert.
- **`_SYNC_WEDGE_CEILING` stays 420**, fingerprint `sync-wedged`, still `error`.
- **Both fire-time fingerprints and levels keep their identity**
  (`sync-watchdog-timeout` / `sync-watchdog-timeout-offline`), so the existing
  34-occurrence group stays continuous and comparable across the change.

  Worth saying out loud: once the bands exist, the fire-time report is
  **strictly redundant** as a measurement — every cycle it fires on also
  reports a band, with strictly more information. It is kept for continuity
  (an unbroken 34-occurrence series across the change is what makes
  before/after comparable) and because it is the report that *pages* while the
  bands only measure. Once the bands have a few weeks of history and the
  paging role has been moved onto one of them, the fire-time report can be
  retired. That is a follow-up with evidence behind it, not something to do in
  this change.
- `bf.reset_session()` / `aw.reset_session()` keep running in both watchdog
  branches.

## Testing

Against the real `SyncCoordinator._do_sync` with stub clients, asserting on
captured reports — never on arguments forwarded between functions
(`rules/test-fixture-discipline.md`).

**Clock: no fake clock, and deliberately so.** The obvious move is to patch
`time.monotonic`, and the repo has already shipped a partial-clock bomb doing
something adjacent — #131, where the reader took an injected `now=` but the
sibling `remove_failed` that wrote the timestamp did not, so the test broke the
day wall clock passed the fixture date. The existing harness avoids the whole
class instead: it shrinks `_DO_SYNC_DEADLINE` to 0.3s via an *instance* override
and sleeps for real. Every clock read in the cycle then comes from one real
clock, so there is no seam to be partially injected. Reuse that pattern; it is
also why the bands have to be ratios.

Split by what each level of test can actually prove:

**Unit, against the pure band function** — exact boundaries, no timing:

1. 150.0 → marginal; 151.2 → marginal; 179.9 → marginal.
2. **Exactly 180.0 (1.2×) → moderate, NOT marginal**, and exactly 300.0 (2.0×)
   → severe. Both ends of every range get a case
   (`diagnosis-discipline.md` Rule 3). A timed cycle cannot land on 1.2×
   reliably, which is the whole reason this function is extracted.
3. The three bands are distinct strings — a band silently aliasing another
   would turn two counts into one and look like a real distribution.

**Integration, driving the real `_do_sync`** — wiring, not arithmetic:

4. A cycle at ~1.1× emits exactly one capture at the marginal fingerprint,
   `level="warning"`, `tags={"component": "sync-watchdog"}`; ~1.5× the moderate
   one; ~2.5× the severe one.
5. The report carries `elapsed_seconds`, `phase` and `deadline_seconds` in
   `context`, with elapsed asserted as a *range* around the real sleep rather
   than an equality — a constant would satisfy an equality against a stub.
6. **Critical negative: a cycle finishing well inside the deadline emits ZERO
   outcome captures.** This catches the predicate being implemented backwards,
   whose failure mode is an outcome report on every healthy cycle — flooding
   the ingest this change exists to quieten.
7. **The negative's own vacuity control.** A cycle that never ran also produces
   zero outcome captures, so the same harness must be shown producing exactly
   one when it overruns. Without this, test 6 passes for the wrong reason
   forever.
8. Phase attribution: an overrun stalled in `_monitor_capture_health` reports
   `capture_health`; one stalled in `sync_engine.sync()` reports `sync`. The
   second alone is not enough — every fixture in the outcome tests overruns
   inside `sync`, so a hardcoded `"sync"` would satisfy all of them.
9. An overrun still emits its fire-time `sync-watchdog-timeout` error. The
   outcome report is additive, not a replacement.

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
