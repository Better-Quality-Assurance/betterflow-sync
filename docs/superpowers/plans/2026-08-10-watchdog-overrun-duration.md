# Watchdog Overrun Duration and Phase — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a watchdog overrun say how long the cycle actually ran and which stage it was in, so issue #179's "how many of the 34 are genuinely wedged versus merely over the line" becomes answerable from the daily digest.

**Architecture:** `_do_sync` stamps a start time and a per-cycle phase box. In `finally`, if elapsed reached the deadline, it emits one extra `warning` report whose *fingerprint* encodes a severity band — because the ingest aggregates by fingerprint and by nothing else, so a bucket count is the only measurement that survives. The two existing fire-time reports gain the phase in `context` but keep their fingerprints and levels unchanged.

**Tech Stack:** Python 3, pytest, `unittest.mock`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-10-watchdog-overrun-duration-design.md`

## Global Constraints

- **Do not change `_DO_SYNC_DEADLINE` (150).** This is measurement, not tuning. The issue and the task brief both forbid raising it to silence the alert.
- **Do not change `_SYNC_WEDGE_CEILING` (420)** or the `sync-wedged` fingerprint/level.
- **Do not change the two existing fire-time fingerprints or their levels** — `sync-watchdog-timeout` (`error`) and `sync-watchdog-timeout-offline` (`warning`). The existing 34-occurrence group must stay continuous across this change. Adding `context` to them is permitted; changing `message`, `level` or `fingerprint` is not.
- **All new outcome reports are `level="warning"`.** The fire-time report already paged this cycle at 150s as `error`.
- **`tags={"component": "sync-watchdog"}`** on every new capture, matching the existing two.
- Work happens in the worktree `/Users/brad/Code2/betterflow-sync-watchdog` on branch `feat/watchdog-overrun-duration`. Do not touch the primary checkout at `/Users/brad/Code2/betterflow-sync`.
- Run tests with `PYTHONPATH=. python3 -m pytest` from the worktree root.
- `tests/test_sync_watchdog_budget.py` and `tests/test_sync_watchdog_outcome_classification.py` must stay green after every task.
- Python: 4-space indent. Conventional Commits. Never `git add -A` — stage explicit paths only.

---

### Task 1: The pure severity-band function

The bands are expressed as **multiples of the deadline**, not absolute seconds. Two reasons, both concrete: the deadline has already moved once (120s → 150s) and absolute bands would have silently become wrong; and `tests/test_sync_watchdog_outcome_classification.py` shrinks the deadline to 0.3s via an instance override, so absolute bands would collapse every test cycle into the first bucket and the other two would be unreachable without a 300-second sleep.

Keeping it a pure module-level function is what makes the boundaries testable exactly. A timed cycle cannot land on 1.2× reliably; a unit test can.

**Files:**
- Modify: `src/main.py` (add module-level constants and function above the `SyncCoordinator` class)
- Test: `tests/test_watchdog_overrun_bands.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_overrun_fingerprint(elapsed: float, deadline: float) -> str`, returning one of exactly three strings: `"sync-watchdog-overrun-marginal"`, `"sync-watchdog-overrun-moderate"`, `"sync-watchdog-overrun-severe"`. Task 3 calls this.

- [ ] **Step 1: Write the failing test**

Create `tests/test_watchdog_overrun_bands.py`:

```python
"""Severity bands for a watchdog overrun.

The ingest (betterqa-bot) aggregates error reports by fingerprint and by
nothing else — `context` is overwritten newest-wins per fingerprint and the
daily digest reads only `message` + `occurrences`. So the ONLY way an elapsed
time becomes countable is by riding in the fingerprint. These bands are that
mechanism.

Bands are multiples of the deadline rather than absolute seconds: the deadline
has moved once already (120s -> 150s), and the watchdog integration tests
shrink it to sub-second, which would collapse absolute bands into one bucket.

Boundaries are tested here, exactly, because a timed cycle cannot land on 1.2x
reliably.
"""

import pytest

from src.main import _overrun_fingerprint

MARGINAL = "sync-watchdog-overrun-marginal"
MODERATE = "sync-watchdog-overrun-moderate"
SEVERE = "sync-watchdog-overrun-severe"


@pytest.mark.parametrize(
    "elapsed,expected",
    [
        # At the deadline exactly — the smallest possible overrun.
        (150.0, MARGINAL),
        (151.2, MARGINAL),
        (179.9, MARGINAL),
        # 1.2x boundary: BELONGS TO moderate, not marginal. Both ends of the
        # range get a case (diagnosis-discipline.md Rule 3).
        (180.0, MODERATE),
        (246.0, MODERATE),
        (299.9, MODERATE),
        # 2.0x boundary: belongs to severe.
        (300.0, SEVERE),
        (903.4, SEVERE),
        (5000.0, SEVERE),
    ],
)
def test_bands_at_the_production_deadline(elapsed, expected):
    assert _overrun_fingerprint(elapsed, 150.0) == expected


@pytest.mark.parametrize(
    "elapsed,expected",
    [
        (0.30, MARGINAL),
        (0.35, MARGINAL),
        (0.36, MODERATE),
        (0.45, MODERATE),
        (0.60, SEVERE),
        (0.90, SEVERE),
    ],
)
def test_bands_scale_with_a_shrunk_deadline(elapsed, expected):
    """The property that makes the integration tests possible at all."""
    assert _overrun_fingerprint(elapsed, 0.3) == expected


def test_returns_exactly_three_distinct_fingerprints():
    """A band that silently aliases another would make two counts one count."""
    produced = {
        _overrun_fingerprint(e, 150.0) for e in (150.0, 200.0, 400.0)
    }
    assert produced == {MARGINAL, MODERATE, SEVERE}


def test_nonpositive_deadline_does_not_divide_by_zero():
    """Defensive only: a zero deadline is not reachable in production, but a
    ZeroDivisionError here would escape into _do_sync's finally block and mask
    whatever real failure the cycle was already reporting."""
    assert _overrun_fingerprint(10.0, 0.0) == SEVERE
    assert _overrun_fingerprint(10.0, -1.0) == SEVERE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m pytest tests/test_watchdog_overrun_bands.py -v`

Expected: FAIL at collection — `ImportError: cannot import name '_overrun_fingerprint' from 'src.main'`.

- [ ] **Step 3: Write minimal implementation**

In `src/main.py`, insert immediately **above** the `class SyncCoordinator` declaration (find it with `grep -n "^class SyncCoordinator" src/main.py`):

```python
# Watchdog overrun severity bands, as multiples of _DO_SYNC_DEADLINE.
#
# Ratios rather than absolute seconds, for two reasons. The deadline has moved
# once already (120s -> 150s) and absolute bands would have quietly become
# wrong. And the watchdog tests shrink the deadline to sub-second, where
# absolute bands would put every test cycle in the first bucket and leave the
# other two unreachable without a five-minute sleep.
#
# The band rides in the FINGERPRINT because that is the only thing the ingest
# aggregates: betterqa-bot stores `context` but overwrites it newest-wins per
# fingerprint, and the daily digest selects message + occurrences only. An
# elapsed time in `context` alone would survive one occurrence and appear in no
# report; a band in the fingerprint turns the occurrence counter into the
# distribution.
_OVERRUN_BANDS = (
    (1.2, "sync-watchdog-overrun-marginal"),
    (2.0, "sync-watchdog-overrun-moderate"),
)
_OVERRUN_BAND_SEVERE = "sync-watchdog-overrun-severe"


def _overrun_fingerprint(elapsed: float, deadline: float) -> str:
    """Dedup key for a cycle that ran `elapsed` against `deadline`.

    Upper bounds are exclusive, so exactly 1.2x is moderate and exactly 2.0x is
    severe. Callers must only reach here when elapsed >= deadline.
    """
    if deadline <= 0:
        return _OVERRUN_BAND_SEVERE
    ratio = elapsed / deadline
    for limit, fingerprint in _OVERRUN_BANDS:
        if ratio < limit:
            return fingerprint
    return _OVERRUN_BAND_SEVERE
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python3 -m pytest tests/test_watchdog_overrun_bands.py -v`

Expected: PASS, 19 passed.

- [ ] **Step 5: Verify the boundary assertions are load-bearing**

Change `ratio < limit` to `ratio <= limit` in `src/main.py`, then run the same command.

Expected: FAIL — specifically `test_bands_at_the_production_deadline[180.0-...moderate]` and `[300.0-...severe]` go red, because 180.0 would become marginal and 300.0 would become moderate.

Then revert that one character back to `<` and re-run. Expected: PASS again.

If the suite stayed green with `<=`, the boundary cases are not witnessing anything — stop and fix the test before continuing.

- [ ] **Step 6: Commit**

```bash
cd /Users/brad/Code2/betterflow-sync-watchdog
git add src/main.py tests/test_watchdog_overrun_bands.py
git commit -m "feat(watchdog): severity bands for a cycle that overran its deadline

Pure function, ratios of the deadline rather than absolute seconds so the
bands survive the deadline moving (120s -> 150s once already) and so tests
can shrink it. Boundaries are exclusive upper bounds, witnessed by flipping
< to <= and watching the 180.0 and 300.0 cases redden.

Not yet wired into _do_sync."
```

---

### Task 2: Per-cycle phase tracking

**Files:**
- Modify: `src/main.py:1554-1758` (`_do_sync`), plus a small class above `class SyncCoordinator`
- Test: `tests/test_watchdog_cycle_phase.py` (create)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `_CyclePhase` with a single mutable attribute `.name: str`, initialised to `"startup"`. A local named `phase` inside `_do_sync`, captured by the `_watchdog` closure and readable in `finally`. Task 3 reads `phase.name`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_watchdog_cycle_phase.py`:

```python
"""Which stage was _do_sync in when the watchdog fired.

Coarse by design: the phase is stamped in _do_sync only, so most real overruns
will report 'sync' because sync_engine.sync() is where the retry chain and the
send budget live. That still rules OUT the other four stages, at the cost of
one file instead of instrumenting the ~1000-line sync_engine.sync() on the
hottest path in the repo.

The phase lives in a per-cycle box rather than on the coordinator: a cycle
abandoned by _acquire_sync_slot's takeover keeps running, and an instance
attribute would let that zombie overwrite its successor's phase.
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from src.config import Config
from src.main import SyncCoordinator, _CyclePhase
from src.reminders import ReminderManager
from src.sync.sync_engine import SyncEngine

_TEST_DEADLINE = 0.3
_OVERRUN = 1.2


def _ok_stats():
    return SimpleNamespace(
        success=True,
        events_sent=0,
        events_queued=0,
        events_filtered=0,
        gaps_filled=0,
        errors=[],
        aw_bucket_fetch_failed=False,
    )


class _Recorder:
    def __init__(self):
        self.captures = []

    def capture(self, message, **kwargs):
        self.captures.append({"message": message, **kwargs})

    def by_fingerprint(self, fingerprint):
        return [c for c in self.captures if c.get("fingerprint") == fingerprint]


class TestCyclePhase:
    def setup_method(self):
        self.config = Config()
        self.aw = Mock()
        self.aw.is_running.return_value = True
        self.bf = Mock()
        self.queue = Mock()
        self.queue.get_checkpoint.return_value = None
        self.queue.size.return_value = 0
        self.queue.is_near_capacity.return_value = False
        self.sync_engine = Mock(spec=SyncEngine)
        self.sync_engine.is_paused = False
        self.sync_engine.is_private = False
        self.sync_engine.sync.return_value = _ok_stats()
        self.tray = Mock()
        self.tray.model = Mock()
        self.tray.model.lock = threading.RLock()
        self.aw_manager = Mock()
        self.aw_manager.is_managing = False
        self.reminder = Mock(spec=ReminderManager)
        self.coord = SyncCoordinator(
            config=self.config,
            aw=self.aw,
            bf=self.bf,
            queue=self.queue,
            sync_engine=self.sync_engine,
            tray=self.tray,
            aw_manager=self.aw_manager,
            reminder_manager=self.reminder,
        )
        self.coord.scheduler = Mock()
        self.coord.scheduler.running = True
        self.recorder = _Recorder()
        self.coord.error_reporter = self.recorder
        self.coord._fetch_hours_today = Mock(return_value="1:00")
        self.coord._DO_SYNC_DEADLINE = _TEST_DEADLINE

    def test_a_fresh_box_starts_at_startup(self):
        assert _CyclePhase().name == "startup"

    def test_overrun_inside_sync_reports_phase_sync(self):
        def _slow_sync(*_a, **_k):
            time.sleep(_OVERRUN)
            return _ok_stats()

        self.sync_engine.sync.side_effect = _slow_sync
        self.coord._do_sync()

        hung = self.recorder.by_fingerprint("sync-watchdog-timeout")
        assert len(hung) == 1, self.recorder.captures
        assert hung[0]["context"]["phase"] == "sync"

    def test_overrun_inside_capture_health_reports_phase_capture_health(self):
        """The discriminating case: if the stamp were only ever set to 'sync',
        the test above would pass and this one would not."""

        def _slow_health():
            time.sleep(_OVERRUN)
            return True

        self.coord._monitor_capture_health = Mock(side_effect=_slow_health)
        self.coord._do_sync()

        hung = self.recorder.by_fingerprint("sync-watchdog-timeout")
        assert len(hung) == 1, self.recorder.captures
        assert hung[0]["context"]["phase"] == "capture_health"

    def test_existing_fire_time_report_keeps_its_identity(self):
        """Global constraint: adding context must not move the fingerprint,
        level or message of the group that already holds 34 occurrences."""

        def _slow_sync(*_a, **_k):
            time.sleep(_OVERRUN)
            return _ok_stats()

        self.sync_engine.sync.side_effect = _slow_sync
        self.coord._do_sync()

        hung = self.recorder.by_fingerprint("sync-watchdog-timeout")
        assert len(hung) == 1
        assert hung[0]["level"] == "error"
        assert "Sync hung" in hung[0]["message"]
        assert hung[0]["tags"] == {"component": "sync-watchdog"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m pytest tests/test_watchdog_cycle_phase.py -v`

Expected: FAIL at collection — `ImportError: cannot import name '_CyclePhase' from 'src.main'`.

- [ ] **Step 3: Add the phase box**

In `src/main.py`, directly below the `_overrun_fingerprint` function added in Task 1:

```python
class _CyclePhase:
    """The stage _do_sync is currently in, as a per-cycle mutable box.

    Deliberately NOT an attribute on SyncCoordinator. A cycle abandoned by
    _acquire_sync_slot's takeover keeps running to completion, and an instance
    attribute would let that zombie overwrite the phase of the cycle that
    replaced it. Same reasoning the transient-failure counter is captured
    per-thread rather than re-read from the Timer thread.
    """

    __slots__ = ("name",)

    def __init__(self) -> None:
        self.name = "startup"
```

- [ ] **Step 4: Stamp the phases in `_do_sync`**

All edits are inside `_do_sync` (`src/main.py:1554-1758`).

**4a.** Directly after `cycle_transients = transient_failure_counter()` / `transient_at_cycle_start = cycle_transients.value` (`src/main.py:1583-1584`), add:

```python
        # Where the cycle is right now, for the watchdog and the cycle-end
        # outcome report. Per-cycle box, never an instance attribute — see
        # _CyclePhase.
        phase = _CyclePhase()
```

**4b.** In `_watchdog()`, add `context` to the **offline** capture. It currently reads (`src/main.py:1604-1610`):

```python
                    self.error_reporter.capture(
                        f"Sync slow — exceeded {self._DO_SYNC_DEADLINE}s while the "
                        "BetterFlow API was unreachable "
                        f"({transient_this_cycle} transient "
                        f"failure{'' if transient_this_cycle == 1 else 's'} this cycle)",
                        level="warning",
                        tags={"component": "sync-watchdog"},
                        fingerprint="sync-watchdog-timeout-offline",
                    )
```

Add one line before `fingerprint=`:

```python
                        context={"phase": phase.name},
```

**4c.** Same for the **hung** capture (`src/main.py:1616-1622`), adding `context={"phase": phase.name},` before its `fingerprint="sync-watchdog-timeout",` line.

**4d.** Stamp each stage. Set the phase on the line immediately **before** each of these existing lines:

| Existing line | Line to insert before it |
|---|---|
| `src/main.py:1662` `self._monitor_capture_health()` (inside the `paused_by_network` branch) | `phase.name = "capture_health"` |
| `src/main.py:1668` `if not self._monitor_capture_health():` | `phase.name = "capture_health"` |
| `src/main.py:1671` `stats = self.sync_engine.sync()` | `phase.name = "sync"` |
| `src/main.py:1724` `hours = self._fetch_hours_today()` | `phase.name = "hours_fetch"` |

Match the surrounding indentation exactly (the `paused_by_network` one is more deeply indented than the others).

**4e.** Directly after `stats = self.sync_engine.sync()` and its blank line, before `if stats.aw_bucket_fetch_failed:`, add:

```python
            phase.name = "post_sync"
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=. python3 -m pytest tests/test_watchdog_cycle_phase.py -v`

Expected: PASS, 4 passed.

- [ ] **Step 6: Run the two suites that must not regress**

Run:
```bash
PYTHONPATH=. python3 -m pytest tests/test_sync_watchdog_budget.py tests/test_sync_watchdog_outcome_classification.py -v
```

Expected: PASS, all green. If `test_sync_watchdog_outcome_classification.py` reddens, the fire-time captures' fingerprint/level/message were changed rather than merely gaining `context` — revert and redo step 4b/4c.

- [ ] **Step 7: Witness the phase stamp**

Delete the `phase.name = "capture_health"` line at the `if not self._monitor_capture_health():` site only, then run:

```bash
PYTHONPATH=. python3 -m pytest tests/test_watchdog_cycle_phase.py -v
```

Expected: FAIL, exactly `test_overrun_inside_capture_health_reports_phase_capture_health`, reporting `'startup' != 'capture_health'`. The other three stay green.

Confirm the deletion actually applied before trusting the result — a no-op edit produces zero failures, which is indistinguishable from an unwitnessed mechanism. Take a copy first and compare:

```bash
cp src/main.py /tmp/main.before
# ... delete the one line ...
cmp -s /tmp/main.before src/main.py && echo "NO-OP — mutant tested nothing"
```

**Do NOT restore with `git checkout src/main.py`.** Step 4's edits are still uncommitted at this point, so that would discard the whole task and leave the suite green on pre-change code — which reads exactly like success. Restore from the copy instead:

```bash
cp /tmp/main.before src/main.py
PYTHONPATH=. python3 -m pytest tests/test_watchdog_cycle_phase.py -v   # green again
```

- [ ] **Step 8: Commit**

```bash
cd /Users/brad/Code2/betterflow-sync-watchdog
git add src/main.py tests/test_watchdog_cycle_phase.py
git commit -m "feat(watchdog): stamp which stage a sync cycle is in

A per-cycle box, not an instance attribute: a cycle abandoned by the wedge
takeover keeps running and would otherwise overwrite its successor's phase.

Both existing fire-time reports gain context={'phase': ...}; their
fingerprints, levels and messages are untouched so the group that already
holds 34 occurrences stays continuous.

Witnessed by deleting the capture_health stamp and watching exactly that
test redden."
```

---

### Task 3: The cycle-end outcome report

This is the deliverable. Everything before it was scaffolding.

**Files:**
- Modify: `src/main.py:1749-1758` (the `finally` block of `_do_sync`), plus one line near the top of `_do_sync`
- Test: `tests/test_watchdog_overrun_outcome.py` (create)

**Interfaces:**
- Consumes: `_overrun_fingerprint(elapsed, deadline)` from Task 1; `_CyclePhase` / the `phase` local from Task 2.
- Produces: one `error_reporter.capture(...)` per overrunning cycle, `level="warning"`, `tags={"component": "sync-watchdog"}`, `context={"elapsed_seconds": float, "phase": str, "deadline_seconds": float}`, fingerprint from `_overrun_fingerprint`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_watchdog_overrun_outcome.py`:

```python
"""How long an overrunning cycle actually ran — issue #179.

The watchdog fires AT the deadline, so elapsed measured inside it is always
~150s no matter what the cycle goes on to do. The real duration is knowable
only at cycle end, which is where this report is emitted from.

The predicate is `elapsed >= deadline`, deliberately NOT "did the watchdog
fire": deriving it from elapsed avoids racing the in-flight Timer thread, and
means these tests never need the timer to fire at all.
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from src.config import Config
from src.main import SyncCoordinator
from src.reminders import ReminderManager
from src.sync.sync_engine import SyncEngine

_TEST_DEADLINE = 0.3

MARGINAL = "sync-watchdog-overrun-marginal"
MODERATE = "sync-watchdog-overrun-moderate"
SEVERE = "sync-watchdog-overrun-severe"
ALL_BANDS = (MARGINAL, MODERATE, SEVERE)


def _ok_stats():
    return SimpleNamespace(
        success=True,
        events_sent=0,
        events_queued=0,
        events_filtered=0,
        gaps_filled=0,
        errors=[],
        aw_bucket_fetch_failed=False,
    )


class _Recorder:
    def __init__(self):
        self.captures = []

    def capture(self, message, **kwargs):
        self.captures.append({"message": message, **kwargs})

    def by_fingerprint(self, fingerprint):
        return [c for c in self.captures if c.get("fingerprint") == fingerprint]

    def outcome_captures(self):
        return [c for c in self.captures if c.get("fingerprint") in ALL_BANDS]


class _Harness:
    def setup_method(self):
        self.config = Config()
        self.aw = Mock()
        self.aw.is_running.return_value = True
        self.bf = Mock()
        self.queue = Mock()
        self.queue.get_checkpoint.return_value = None
        self.queue.size.return_value = 0
        self.queue.is_near_capacity.return_value = False
        self.sync_engine = Mock(spec=SyncEngine)
        self.sync_engine.is_paused = False
        self.sync_engine.is_private = False
        self.sync_engine.sync.return_value = _ok_stats()
        self.tray = Mock()
        self.tray.model = Mock()
        self.tray.model.lock = threading.RLock()
        self.aw_manager = Mock()
        self.aw_manager.is_managing = False
        self.reminder = Mock(spec=ReminderManager)
        self.coord = SyncCoordinator(
            config=self.config,
            aw=self.aw,
            bf=self.bf,
            queue=self.queue,
            sync_engine=self.sync_engine,
            tray=self.tray,
            aw_manager=self.aw_manager,
            reminder_manager=self.reminder,
        )
        self.coord.scheduler = Mock()
        self.coord.scheduler.running = True
        self.recorder = _Recorder()
        self.coord.error_reporter = self.recorder
        self.coord._fetch_hours_today = Mock(return_value="1:00")
        self.coord._DO_SYNC_DEADLINE = _TEST_DEADLINE

    def run_cycle_taking(self, seconds):
        def _slow_sync(*_a, **_k):
            time.sleep(seconds)
            return _ok_stats()

        self.sync_engine.sync.side_effect = _slow_sync
        self.coord._do_sync()


class TestOverrunIsMeasured(_Harness):
    def test_a_marginal_overrun_reports_the_marginal_band(self):
        self.run_cycle_taking(0.33)  # ~1.1x of 0.3

        got = self.recorder.by_fingerprint(MARGINAL)
        assert len(got) == 1, self.recorder.captures
        assert got[0]["level"] == "warning"
        assert got[0]["tags"] == {"component": "sync-watchdog"}

    def test_a_moderate_overrun_reports_the_moderate_band(self):
        self.run_cycle_taking(0.45)  # 1.5x

        assert len(self.recorder.by_fingerprint(MODERATE)) == 1, self.recorder.captures
        assert self.recorder.by_fingerprint(MARGINAL) == []

    def test_a_severe_overrun_reports_the_severe_band(self):
        self.run_cycle_taking(0.75)  # 2.5x

        assert len(self.recorder.by_fingerprint(SEVERE)) == 1, self.recorder.captures
        assert self.recorder.by_fingerprint(MODERATE) == []

    def test_the_report_carries_the_elapsed_time_and_the_phase(self):
        self.run_cycle_taking(0.45)

        got = self.recorder.by_fingerprint(MODERATE)[0]
        assert got["context"]["phase"] == "sync"
        assert got["context"]["deadline_seconds"] == _TEST_DEADLINE
        # A real measurement, not a constant: at least the sleep, and not
        # absurdly more.
        assert 0.45 <= got["context"]["elapsed_seconds"] < 5.0
        assert "0.4" in got["message"] or "0.5" in got["message"]


class TestHealthyCyclesStaySilent(_Harness):
    """THE critical negative. If the predicate is implemented backwards this is
    the only test that catches it, and the failure mode is an outcome report on
    every healthy cycle — flooding the ingest this change exists to quieten."""

    def test_a_cycle_inside_the_deadline_emits_no_outcome_report(self):
        self.run_cycle_taking(0.02)  # far under 0.3

        assert self.recorder.outcome_captures() == [], self.recorder.captures

    def test_the_negative_above_is_not_passing_vacuously(self):
        """A cycle that never ran would also produce zero outcome captures.
        Prove the subject was reached: the same harness, overrunning, DOES
        produce one."""
        self.run_cycle_taking(0.02)
        assert self.recorder.outcome_captures() == []

        self.recorder.captures.clear()
        self.run_cycle_taking(0.45)
        assert len(self.recorder.outcome_captures()) == 1, self.recorder.captures


class TestExistingReportsAreUnchanged(_Harness):
    def test_an_overrun_still_emits_its_fire_time_error(self):
        """The outcome report is additive. The group that already holds 34
        occurrences must keep firing exactly as before."""
        self.run_cycle_taking(0.45)

        hung = self.recorder.by_fingerprint("sync-watchdog-timeout")
        assert len(hung) == 1, self.recorder.captures
        assert hung[0]["level"] == "error"

    def test_the_outcome_report_never_uses_error_level(self):
        """It measures; the fire-time report pages. A second error would double
        the alert volume."""
        self.run_cycle_taking(0.75)

        for capture in self.recorder.outcome_captures():
            assert capture["level"] == "warning"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m pytest tests/test_watchdog_overrun_outcome.py -v`

Expected: the three band tests, the payload test, the vacuity test and both `TestExistingReportsAreUnchanged`… precisely: 5 FAIL (every test asserting an outcome capture exists) and 3 PASS (`test_a_cycle_inside_the_deadline_emits_no_outcome_report`, and the two in `TestExistingReportsAreUnchanged` that only check pre-existing behaviour). The negative passing before the feature exists is expected and is exactly why `test_the_negative_above_is_not_passing_vacuously` is there.

- [ ] **Step 3: Stamp the cycle start**

In `_do_sync`, directly after `watchdog_cancelled = threading.Event()` (`src/main.py:1567`), add:

```python
        # Cycle start, for the cycle-end outcome report. The watchdog fires AT
        # the deadline, so elapsed measured inside it is always ~150s however
        # long the cycle really runs — the true duration is only knowable here.
        cycle_started_at = time.monotonic()
```

- [ ] **Step 4: Emit the outcome report**

Replace the `finally` block at `src/main.py:1749-1758`:

```python
        finally:
            # Clear the wedge stamp only if we're still the current holder — a
            # taken-over zombie reaching here must not wipe its successor's stamp.
            with self._sync_takeover_lock:
                if self._sync_holder is my_lock:
                    self._sync_started_at = None
                    self._sync_holder = None
            watchdog_cancelled.set()
            watchdog.cancel()
            my_lock.release()
```

with:

```python
        finally:
            # Clear the wedge stamp only if we're still the current holder — a
            # taken-over zombie reaching here must not wipe its successor's stamp.
            with self._sync_takeover_lock:
                if self._sync_holder is my_lock:
                    self._sync_started_at = None
                    self._sync_holder = None
            watchdog_cancelled.set()
            watchdog.cancel()
            my_lock.release()
            self._report_overrun_outcome(
                time.monotonic() - cycle_started_at, phase.name
            )
```

Then add the method to `SyncCoordinator`, immediately after `_do_sync` ends (i.e. before the `def _set_sync_failure_state` that currently follows it):

```python
    def _report_overrun_outcome(self, elapsed: float, phase_name: str) -> None:
        """Record how long a cycle that breached the deadline actually ran.

        Gated on elapsed rather than on "did the watchdog fire" so it cannot
        race the in-flight Timer thread: a watchdog past its cancelled check but
        not yet flagged would leave this reading "did not fire" for a cycle that
        did.

        Level is always warning. The fire-time report already paged this same
        cycle at the deadline as an error; a second error here would add no
        paging value and double the volume this change exists to reduce.

        A cycle abandoned by _acquire_sync_slot's takeover reaches here late,
        with its true elapsed — that is the "is it unbounded?" evidence
        sync-wedged cannot give, since sync-wedged records only that we gave up
        at the ceiling.
        """
        if elapsed < self._DO_SYNC_DEADLINE or self.error_reporter is None:
            return
        self.error_reporter.capture(
            f"Sync overran the {self._DO_SYNC_DEADLINE}s deadline — "
            f"finished at {elapsed:.1f}s in phase '{phase_name}'",
            level="warning",
            tags={"component": "sync-watchdog"},
            context={
                "elapsed_seconds": round(elapsed, 1),
                "phase": phase_name,
                "deadline_seconds": self._DO_SYNC_DEADLINE,
            },
            fingerprint=_overrun_fingerprint(elapsed, self._DO_SYNC_DEADLINE),
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=. python3 -m pytest tests/test_watchdog_overrun_outcome.py -v`

Expected: PASS, 8 passed.

- [ ] **Step 6: Run the full watchdog set**

Run:
```bash
PYTHONPATH=. python3 -m pytest tests/test_sync_watchdog_budget.py \
  tests/test_sync_watchdog_outcome_classification.py \
  tests/test_watchdog_overrun_bands.py \
  tests/test_watchdog_cycle_phase.py \
  tests/test_watchdog_overrun_outcome.py -v
```

Expected: all green.

- [ ] **Step 7: Witness each mechanism separately**

Three mutants, one per mechanism. Each must redden a **distinct named** test. Reverting all three at once proves nothing about any one of them. Before trusting any result, confirm the edit applied — a `sed`/`perl` that matched nothing produces zero failures, which looks identical to an unwitnessed mechanism.

For each: `cp src/main.py /tmp/main.before`, apply the mutant, `cmp -s /tmp/main.before src/main.py && echo "NO-OP — mutant tested nothing"`, run, then restore by reversing the edit by hand.

**Mutant A — invert the predicate.** In `_report_overrun_outcome`, change `if elapsed < self._DO_SYNC_DEADLINE` to `if elapsed > self._DO_SYNC_DEADLINE`.
Expected red: `test_a_marginal_overrun_reports_the_marginal_band` and the other band tests (they now emit nothing).

**Mutant B — collapse the bands.** Change `fingerprint=_overrun_fingerprint(...)` to `fingerprint="sync-watchdog-overrun-marginal"`.
Expected red: `test_a_moderate_overrun_reports_the_moderate_band` and `test_a_severe_overrun_reports_the_severe_band`.
This is the mechanism that carries the entire measurement, so if it survives, the change delivers nothing.

**Mutant C — drop the phase.** Change `"phase": phase_name,` to `"phase": "sync",`.
Expected red: `test_overrun_inside_capture_health_reports_phase_capture_health` in `tests/test_watchdog_cycle_phase.py`.
This is the agreement-region trap: every fixture in the *outcome* file overruns inside `sync`, so a hardcoded `"sync"` is indistinguishable there. Only the capture_health fixture separates them.

- [ ] **Step 8: Commit**

```bash
cd /Users/brad/Code2/betterflow-sync-watchdog
git add src/main.py tests/test_watchdog_overrun_outcome.py
git commit -m "feat(watchdog): report how long an overrunning cycle actually ran

Closes the measurement half of #179. The watchdog fires AT the 150s deadline,
so elapsed observed inside it is always ~150s; the real duration is only
knowable at cycle end, which is where this emits from.

The band rides in the fingerprint because that is the only thing the ingest
aggregates — context is overwritten newest-wins per fingerprint and the daily
digest reads message + occurrences only. Bucketing the fingerprint turns the
occurrence counter into the distribution, so the digest can finally
distinguish a 151s cycle from a deadlocked one.

Always warning: the fire-time report already paged this cycle as an error.

Each mechanism witnessed by its own mutant reddening a distinct named test
(predicate, band selection, phase), with a cmp check that each mutation
applied."
```

---

### Task 4: Correct the stale status on the 2026-07-22 spec

Small, but it is the thing that cost this session an hour: the parked spec says it was never built, and it shipped. The next reader deserves better, and a wrong status is worse than no status because it is actively believed.

**Files:**
- Modify: `docs/superpowers/specs/2026-07-22-watchdog-outcome-classification-design.md:3-5`

**Interfaces:** none.

- [ ] **Step 1: Verify the claim before writing it down**

Run:
```bash
cd /Users/brad/Code2/betterflow-sync-watchdog
grep -n "sync-watchdog-timeout-offline" src/main.py
ls tests/test_sync_watchdog_outcome_classification.py
```

Expected: the fingerprint exists in `src/main.py`, and the test file exists. If either is missing, stop — the premise of this task is wrong.

- [ ] **Step 2: Replace the status block**

The file currently opens:

```markdown
Status: **designed, not scheduled.** Deliberately parked on 2026-07-22 in favour
of higher-value work (see "Priority" below). Pick this up when the noise or the
autofix spend justifies it.
```

Replace those three lines with:

```markdown
Status: **implemented.** Parked on 2026-07-22, built later; the classification
described here is live in `SyncCoordinator._watchdog` (`src/main.py`, fingerprints
`sync-watchdog-timeout` / `sync-watchdog-timeout-offline`) and guarded by
`tests/test_sync_watchdog_outcome_classification.py`. The "Priority" section
below is preserved as the record of why it waited, not as current status.

What this design does NOT do is say how long an overrunning cycle ran, which is
issue #179 — see `2026-08-10-watchdog-overrun-duration-design.md`.
```

- [ ] **Step 3: Commit**

```bash
cd /Users/brad/Code2/betterflow-sync-watchdog
git add docs/superpowers/specs/2026-07-22-watchdog-outcome-classification-design.md
git commit -m "docs(watchdog): mark the 2026-07-22 classification design as shipped

It has read 'designed, not scheduled' since it landed, which sent a session
looking for work that was already done. Points forward to the duration design
for the part that genuinely remains."
```

---

## Final verification

- [ ] **Full suite**

```bash
cd /Users/brad/Code2/betterflow-sync-watchdog
PYTHONPATH=. python3 -m pytest > /tmp/wd-suite.log 2>&1; echo "EXIT: $?"
tail -15 /tmp/wd-suite.log
```

Never pipe this — a pipe reports the last element's status and a failed suite would read as success. Expected: EXIT 0, and a test count **higher** than the pre-change baseline. Record the baseline first with `git stash`-free comparison: run the same command on `origin/main` in a separate worktree if the count is in doubt. An identical count means the new files were not collected.

- [ ] **Lint**

```bash
cd /Users/brad/Code2/betterflow-sync-watchdog && make lint
```

Expected: clean. `make lint` runs ruff.

- [ ] **CI parity — there is no CI, you are it**

GitHub Actions is cost-capped org-wide, and this repo's workflow skips `test` on tag builds. Read `.github/workflows/build.yml` and run every non-deploy step locally:

```bash
cd /Users/brad/Code2/betterflow-sync-watchdog
ls .github/workflows/
grep -n "run:" .github/workflows/build.yml
```

Run each `run:` step from the `test` job. Note the Windows job only runs on tag builds, so a Windows-only breakage would first appear as a failed release — this change adds no `os`-specific calls, but confirm no new import is platform-gated.

- [ ] **Git status audit**

```bash
cd /Users/brad/Code2/betterflow-sync-watchdog
git status
git diff origin/main...HEAD --numstat
```

Every file must be one you intended. Use `--numstat` for the deletion count, never a `^-[^-]` grep — this diff contains markdown list items, whose deleted lines render as `--` and are silently dropped by that pattern.
