# Truthful reporting when the tracker port is held by something that is not capturing

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a device that has attached to a process holding port 5600 which does not answer `/api/0/info` report that fact to the fleet, without changing what processes the agent runs.

**Architecture:** The two open defects (F-3, F-4) were attacked twice as *lifecycle* bugs and both repairs regressed. They are not lifecycle bugs. When a foreign process holds the port it is **unreapable by construction** — `_reap_orphan_processes` is path-scoped to our own binaries (`aw_manager.py:1479-1497`) — so no restart can fix it, and the current behaviour (attach, keep the watchers alive) is the best available: it survives and self-heals the moment the holder dies. The actual defect is that the device reports *healthy* while capturing nothing. So this plan changes the **reporting layer only** and leaves every control-flow decision exactly as it is.

**Tech Stack:** Python 3.11, pytest, `unittest.mock`. No new dependencies.

**Spec:** This plan argues from evidence gathered in the 2026-09-03/04 audit session and recorded in PR #246 (merged, `33855d8`), its three review rounds, and the two project memories `held-socket-is-not-a-capture.md` and `two-routes-back-into-start-locked.md`. The measurements quoted below were reproduced on unmodified source with controls; each is marked VERIFIED.

---

## Global Constraints

- Run the suite as `PYTHONPATH=. ./venv/bin/python -m pytest -q`. Baseline on `main` (`33855d8`) is **2119 passed, 1 skipped**. Every task must report baseline + its own added tests; a count that does not reconcile means you tested the wrong tree.
- **One worktree per session** (`rules/cross-repo-safety.md` Rule 0). `git add` explicit paths only, never `-A`.
- **Commit before running any mutation experiment.** `git restore`/`git checkout -- <path>` are blocked by a safety hook; restore a file with `T="${TMPDIR:-/tmp}"; git show HEAD:<path> > "$T/r.$$" && mv "$T/r.$$" <path>`. An uncommitted edit destroyed by a restore has already cost this workstream one round of work.
- **Do not change any control flow in `_start_locked` or `restart_if_needed`.** That is the entire point of this plan. If a task seems to need one, stop and escalate — two previous attempts did exactly that and both shipped regressions.
- Every new guard is witnessed by a mutation that reddens a **distinct named test**, including the allowance direction (the guard must not fire when the external server is healthy).
- `src/disclosure_baseline.py` is a deliberate second copy of `HEARTBEAT_HEALTH_KEYS` and `tests/test_disclosure_baseline.py` fails on any divergence. Any new heartbeat key goes in **both**, with a written reason in the baseline saying what it reports about the machine.

---

## Background — why the two previous attempts failed

Both are VERIFIED with controls; do not re-derive, and do not repeat them.

| attempt | change | measured result |
|---|---|---|
| round 1 | recovery predicate asks `/api/0/info` | healthy shared ActivityWatch missing two answers → `_using_external=False`, our server spawns, cannot bind, `self.stop()` clears every watcher, `is_managing` goes False |
| round 2 | skip that `stop()` to preserve watchers | dead `bf-data-service` left in `_processes`, which disarms `set_capture_suppressed`'s `elif not self._processes` rebuild route (`aw_manager.py:752`) — zero watchers until the 180s escalation |

Control, same probe, `main.py`'s real tick order, corpse releases the port at tick 4:

```
origin/main   tick1-3 WATCHERS=2 ...  tick4: 3 procs   recovers
round-1 fix   tick1-5 WATCHERS=0 ...                   does not
```

**Two facts this plan depends on, both VERIFIED:**

1. **A latch on the existing capture-dead flags does not survive.** `_start_component` clears both `tracker_download_failed` and `_managed_components_unavailable` on Popen success (`aw_manager.py:2112`), and the watcher loop runs *after* the attach branch. Measured: flags set to `True` before `_start_locked()` read `False` after it. **This is why the plan adds a new flag rather than reusing one.** A new flag is untouched by `_start_component`, so it survives the watcher loop.
2. **There are two routes back into `_start_locked`** — the `_processes`-gated tick route, and `force_restart` via `main.py`'s 180s unreachable escalation, which is a *sibling* of the `is_managing` gate rather than inside it. Neither can reap a foreign holder.

---

## File Structure

| file | responsibility | change |
|---|---|---|
| `src/aw_manager.py` | tracker lifecycle + health | add `_external_server_not_responding` state, set/clear it at the attach decision, publish it from `health_snapshot`, and fix the sixth site's `reachable` read |
| `src/sync/bf_client.py` | heartbeat wire format | add the key to `HEARTBEAT_HEALTH_KEYS` |
| `src/disclosure_baseline.py` | the declaration half of the allowlist | add the key with its written reason |
| `tests/test_external_server_not_responding.py` | **new** — the reporting behaviour | all of Task 1 and Task 2's assertions |
| `tests/test_window_stale_needs_a_responding_server.py` | **new** — the sixth site | Task 3 |

No file is split; `aw_manager.py` is large by house convention and this adds ~25 lines to it.

---

### Task 1: report an attached-but-dead external server

**Files:**
- Modify: `src/aw_manager.py` — `__init__` (add the attribute), and the external-attach branch at `aw_manager.py:1433-1441`
- Test: `tests/test_external_server_not_responding.py` (create)

**Interfaces:**
- Produces: `AWManager._external_server_not_responding: bool` — True only while we are attached to a process that holds the port and does not answer `/api/0/info`. Read by `health_snapshot()` in Task 2.

- [ ] **Step 1: Write the failing test**

Create `tests/test_external_server_not_responding.py`:

```python
"""A device attached to a corpse must SAY so (F-3, reporting half).

The lifecycle is deliberately unchanged: a foreign process holding :5600 is
unreapable (_reap_orphan_processes is path-scoped to our binaries), so
attaching is the behaviour that keeps the watchers alive and self-heals when
the holder dies. Two attempts to change that both regressed. What was missing
was telling the fleet, which is what this flag does.
"""

from unittest.mock import MagicMock

from src.aw_manager import AWManager


def _mgr(*, port_held, answers):
    m = AWManager()
    m._capture_suppressed = False
    m._get_binaries_dir = MagicMock(return_value="/tmp/bin")
    m._rosetta_required = MagicMock(return_value=False)
    m._port_in_use = MagicMock(return_value=port_held)
    m._server_responding = MagicMock(return_value=answers)
    m._start_component = MagicMock(return_value=True)
    m._wait_for_server = MagicMock(return_value=True)
    m._reap_orphan_processes = MagicMock()
    return m


class TestAttachedToACorpseIsReported:
    def test_a_dead_holder_sets_the_flag(self):
        m = _mgr(port_held=True, answers=False)
        m._start_locked()
        assert m._external_server_not_responding is True

    def test_the_flag_survives_the_watcher_loop(self):
        """_start_component clears the two capture-dead flags on success, which
        is why this is a separate flag and not a reuse of those."""
        m = _mgr(port_held=True, answers=False)
        m._start_locked()
        assert m._start_component.called, "fixture never reached the watcher loop"
        assert m._external_server_not_responding is True

    def test_the_lifecycle_is_UNCHANGED_by_this(self):
        """The whole point: we still attach, and the watchers still start."""
        m = _mgr(port_held=True, answers=False)
        assert m._start_locked() is True
        assert m._using_external is True
        started = [c.args[0] for c in m._start_component.call_args_list]
        assert "bf-window-tracker" in started and "bf-idle-tracker" in started


class TestTheAllowanceDirection:
    def test_a_live_external_server_does_not_set_it(self):
        m = _mgr(port_held=True, answers=True)
        m._start_locked()
        assert m._external_server_not_responding is False

    def test_starting_our_own_server_clears_it(self):
        """A stale True must not outlive the condition."""
        m = _mgr(port_held=False, answers=False)
        m._external_server_not_responding = True
        m._start_locked()
        assert m._external_server_not_responding is False
```

- [ ] **Step 2: Run it and watch it fail**

Run: `PYTHONPATH=. ./venv/bin/python -m pytest -q tests/test_external_server_not_responding.py`
Expected: 5 failures, all `AttributeError: 'AWManager' object has no attribute '_external_server_not_responding'`.

- [ ] **Step 3: Add the attribute**

In `AWManager.__init__`, beside the other capture-health state:

```python
        # True only while we are attached to a process that holds the tracker
        # port and does not answer /api/0/info. Deliberately NOT one of the two
        # capture-dead flags: _start_component clears those on Popen success
        # (:2110), and the watcher loop runs after the attach, so anything
        # latched there is wiped inside the same _start_locked call (measured).
        # Nothing else writes this one.
        self._external_server_not_responding = False
```

- [ ] **Step 4: Set and clear it at the attach decision**

Replace the external-attach branch (`aw_manager.py:1433-1441`, the `if server_already_running:` that logs "using external instance"):

```python
        if server_already_running:
            # ASK -- and deliberately do NOT act on the answer here. Read the
            # deferral note above: a foreign holder is unreapable
            # (_reap_orphan_processes is path-scoped to our own binaries), so
            # attaching is the behaviour that keeps the watchers alive and
            # self-heals when the holder dies. Two attempts to change that both
            # regressed. What was missing was never the decision -- it was
            # TELLING THE FLEET, which is all this does.
            self._external_server_not_responding = not self._server_responding()
            if self._external_server_not_responding:
                logger.warning(
                    "Attached to the process holding port %s, but it does not "
                    "answer /api/0/info — this device is capturing NOTHING",
                    self.aw_port,
                )
            else:
                logger.info(
                    f"Tracker server already running on port {self.aw_port}, "
                    "using external instance"
                )
            self._using_external = True
        else:
            # We own the server here, so the external-server verdict is not ours
            # to carry -- clear it or a stale True outlives its condition.
            self._external_server_not_responding = False
            logger.info(f"Starting tracker components from {binaries_dir}")
```

- [ ] **Step 5: Run the tests and the full suite**

Run: `PYTHONPATH=. ./venv/bin/python -m pytest -q tests/test_external_server_not_responding.py`
Expected: 5 passed.

Run: `PYTHONPATH=. ./venv/bin/python -m pytest -q`
Expected: **2124 passed, 1 skipped** (2119 baseline + 5).

- [ ] **Step 6: Commit, then witness by mutation**

```bash
git add src/aw_manager.py tests/test_external_server_not_responding.py
git commit -m "fix(aw): report an attached-but-dead external server"
```

Then, one at a time, restoring between each:

| mutant | must redden |
|---|---|
| `not self._server_responding()` → `False` | `test_a_dead_holder_sets_the_flag` |
| `not self._server_responding()` → `True` | both allowance tests |
| delete the `else:` clear | `test_starting_our_own_server_clears_it` |

Restore with `T="${TMPDIR:-/tmp}"; git show HEAD:src/aw_manager.py > "$T/r.$$" && mv "$T/r.$$" src/aw_manager.py`, and delete `__pycache__` before each run. Record the matrix in the PR body. A mutant that reports *fewer collected tests* than the control did not survive — it crashed; fix the mutant and re-run.

---

### Task 2: put it on the heartbeat

**Files:**
- Modify: `src/aw_manager.py` — `health_snapshot()` (`aw_manager.py:1699-1730`)
- Modify: `src/sync/bf_client.py` — `HEARTBEAT_HEALTH_KEYS` (`bf_client.py:444`)
- Modify: `src/disclosure_baseline.py` — the declaration copy (beside the two capture-dead keys at `disclosure_baseline.py:624-625`)
- Test: `tests/test_external_server_not_responding.py` (append)

**Interfaces:**
- Consumes: `AWManager._external_server_not_responding` from Task 1.
- Produces: heartbeat key `external_server_not_responding` (bool).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_external_server_not_responding.py`:

```python
class TestItReachesTheWire:
    def test_health_snapshot_publishes_it(self):
        m = _mgr(port_held=True, answers=False)
        m._get_latest_window_event_age = MagicMock(return_value=5)
        m._get_latest_afk_event_age = MagicMock(return_value=5)
        m._window_titles_captured_recently = MagicMock(return_value=True)
        m._start_locked()

        assert m.health_snapshot()["external_server_not_responding"] is True

    def test_the_key_is_on_the_heartbeat_allowlist(self):
        """A field missing from HEARTBEAT_HEALTH_KEYS never leaves the machine,
        silently -- health_snapshot publishing it is not enough."""
        from src.sync.bf_client import BetterFlowClient

        assert (
            "external_server_not_responding"
            in BetterFlowClient.HEARTBEAT_HEALTH_KEYS
        )
```

- [ ] **Step 2: Run it and watch it fail**

Run: `PYTHONPATH=. ./venv/bin/python -m pytest -q tests/test_external_server_not_responding.py -k ItReachesTheWire`
Expected: 2 failures — `KeyError: 'external_server_not_responding'` and an assertion failure on the allowlist.

- [ ] **Step 3: Read it under the lock and publish it**

In `health_snapshot()`, inside the existing `with self._lifecycle_lock:` block, beside `download_failed = self.tracker_download_failed`:

```python
            external_dead = self._external_server_not_responding
```

and in the returned dict, beside the other capture-health entries:

```python
            # We are attached to a process that holds the tracker port and does
            # not answer /api/0/info, so nothing is being captured -- but this is
            # NOT tracker_download_failed: our binaries are fine and there is
            # nothing to reap (the holder is not ours). Distinct key so the alert
            # can say "something else owns port 5600" instead of blaming the
            # download.
            "external_server_not_responding": external_dead,
```

- [ ] **Step 4: Add the key to both copies of the allowlist**

In `src/sync/bf_client.py`, inside `HEARTBEAT_HEALTH_KEYS`:

```python
        # Attached to a process holding the tracker port that does not answer
        # /api/0/info: the device is capturing NOTHING, and no restart can fix
        # it because a foreign holder is unreapable. Distinct from
        # tracker_download_failed (our binaries are fine) and from
        # managed_components_unavailable (we did start our watchers).
        "external_server_not_responding",
```

In `src/disclosure_baseline.py`, in the same position relative to the other keys:

```python
    # Whether some OTHER process owns the tracker port and is not serving.
    # Reports a property of the MACHINE's port 5600, never anything about what
    # the user did.
    "external_server_not_responding",
```

- [ ] **Step 5: Run the tests and the full suite**

Run: `PYTHONPATH=. ./venv/bin/python -m pytest -q tests/test_external_server_not_responding.py tests/test_disclosure_baseline.py`
Expected: 7 passed for the new file, and `test_disclosure_baseline.py` green — it fails if the two allowlist copies diverge, so a green run is the proof both were edited.

Run: `PYTHONPATH=. ./venv/bin/python -m pytest -q`
Expected: **2126 passed, 1 skipped** (2119 baseline + 7).

- [ ] **Step 6: Commit**

```bash
git add src/aw_manager.py src/sync/bf_client.py src/disclosure_baseline.py tests/test_external_server_not_responding.py
git commit -m "fix(aw): publish external_server_not_responding on the heartbeat"
```

---

### Task 3: the sixth site — a dead server must not read as a blind window tracker

**Files:**
- Modify: `src/aw_manager.py:1828` (the `reachable = self._port_in_use()` feeding `_is_window_tracker_stale`)
- Test: `tests/test_window_stale_needs_a_responding_server.py` (create)

**Interfaces:** none new. This task is independent of Tasks 1-2 and may be done in either order.

`_is_window_tracker_stale`'s own docstring (`aw_manager.py:2408`) reads its `reachable` argument as *"AW is reachable... otherwise None is just startup lag or an AW outage"*. A corpse holding the port answers `True` to `_port_in_use()`, so a window tracker emitting nothing **because the server is dead** is classed blind and force-restarted, and `_window_tracker_blind` can latch — publishing a permissions story for a dead server. Opposite direction to F-1/F-2: a false *alarm* rather than a false all-clear.

- [ ] **Step 1: Write the failing test**

Create `tests/test_window_stale_needs_a_responding_server.py`:

```python
"""A dead tracker server must not be reported as a blind window tracker.

_is_window_tracker_stale reads `reachable` as "AW is reachable", per its own
docstring -- otherwise a None event age is just an AW outage. It was fed
_port_in_use(), and a corpse holding the port answers True, so an outage was
classified as a blind tracker: a force-restart burst plus a latched
_window_tracker_blind, which publishes a permissions story for a dead server.
"""

from unittest.mock import MagicMock

from src.aw_manager import AWManager


def _mgr(*, port_held, answers):
    m = AWManager()
    m._port_in_use = MagicMock(return_value=port_held)
    m._server_responding = MagicMock(return_value=answers)
    return m


def test_a_corpse_holding_the_port_is_not_reachable():
    m = _mgr(port_held=True, answers=False)
    assert m._window_tracker_reachable() is False


def test_a_responding_server_is_reachable():
    """Allowance: the ordinary healthy case must still be reachable, or every
    quiet window tracker stops being restarted."""
    m = _mgr(port_held=True, answers=True)
    assert m._window_tracker_reachable() is True


def test_a_free_port_is_not_reachable():
    m = _mgr(port_held=False, answers=False)
    assert m._window_tracker_reachable() is False
```

- [ ] **Step 2: Run it and watch it fail**

Run: `PYTHONPATH=. ./venv/bin/python -m pytest -q tests/test_window_stale_needs_a_responding_server.py`
Expected: 3 failures, `AttributeError: ... has no attribute '_window_tracker_reachable'`.

- [ ] **Step 3: Add the named helper**

Beside `_server_responding` in `aw_manager.py`:

```python
    def _window_tracker_reachable(self) -> bool:
        """Is AW reachable for the purpose of judging window-tracker staleness?

        Named rather than inlined because the question is not "is the port
        held". _is_window_tracker_stale reads this as "AW is reachable", and a
        corpse holding the port answers True to a TCP connect -- so an AW
        OUTAGE was being classified as a blind tracker, force-restarted, and
        latched as _window_tracker_blind, which tells the user to check a
        permission when the real fault is a dead server.

        One HTTP ask, no port pre-check: a server that answers necessarily
        holds the port, so the TCP connect would only add latency.
        """
        return self._server_responding()
```

- [ ] **Step 4: Use it at the call site**

At `aw_manager.py:1828`, replace:

```python
        reachable = self._port_in_use()
```

with:

```python
        reachable = self._window_tracker_reachable()
```

- [ ] **Step 5: Run the tests and the full suite**

Run: `PYTHONPATH=. ./venv/bin/python -m pytest -q tests/test_window_stale_needs_a_responding_server.py`
Expected: 3 passed.

Run: `PYTHONPATH=. ./venv/bin/python -m pytest -q`
Expected: **2122 passed, 1 skipped** if run alone (2119 + 3), or 2129 if Tasks 1-2 are already in.

If any pre-existing test in `tests/test_aw_manager_*.py` fails here, read it before touching either side: the likely cause is a fixture that stubs `_port_in_use` and not `_server_responding`, which is the same agreement region PR #246 documented. Making such a fixture answer the question the code now asks is a correction; changing an assertion is not.

- [ ] **Step 6: Commit and witness**

```bash
git add src/aw_manager.py tests/test_window_stale_needs_a_responding_server.py
git commit -m "fix(aw): an AW outage is not a blind window tracker"
```

Mutation: `return self._server_responding()` → `return self._port_in_use()` must redden `test_a_corpse_holding_the_port_is_not_reachable`; → `return True` must redden both negative tests; → `return False` must redden `test_a_responding_server_is_reachable`.

---

### Task 4: retire the deferral notes, since they now describe the wrong thing

**Files:**
- Modify: `src/aw_manager.py` — the F-3 deferral note above the external-attach branch, and the F-4 note in `restart_if_needed`
- Modify: `tests/test_external_server_capturing_class.py` — the two `*_Deferred` / `*_Deliberately` class docstrings

Both notes currently say the sites are deferred because the bug is unfixed. After Tasks 1-2 the *reporting* half is fixed and the *lifecycle* is deliberately unchanged — a different and much narrower claim. Leaving the old wording is the exact failure PR #246's round 3 caught twice: a stale rationale that stops the next reader re-deriving.

- [ ] **Step 1: Rewrite the F-3 note**

Keep the measurement table (it is still the evidence for not touching lifecycle) and replace the framing paragraph with:

```python
        # NOT _external_server_capturing(), deliberately, and this is no longer
        # a deferred bug -- it is a decided design. A foreign holder is
        # unreapable (_reap_orphan_processes is path-scoped to our binaries), so
        # attaching is the behaviour that keeps the watchers alive and recovers
        # when the holder dies; both attempts to change it regressed (table
        # below). The device now REPORTS the condition instead, via
        # _external_server_not_responding on the heartbeat. Change the reporting
        # if it is wrong; do not change this line.
```

- [ ] **Step 2: Rewrite the F-4 note**

```python
        # DELIBERATELY the bare port read. Tearing down external mode achieves
        # nothing when the holder is unreapable, and asking /info here destroyed
        # every watcher on a healthy server that merely blipped (measured). The
        # condition is reported via _external_server_not_responding instead, so
        # the fleet sees it without the agent thrashing over it.
```

- [ ] **Step 3: Update both test class docstrings** to say the sites are decided-and-reported rather than deferred-and-broken, keeping the measurement tables.

- [ ] **Step 4: Run the full suite**

Expected: unchanged from Task 3's count. These are comments and docstrings; if a count moves, something else was edited.

- [ ] **Step 5: Commit**

```bash
git add src/aw_manager.py tests/test_external_server_capturing_class.py
git commit -m "docs(aw): the attach sites are decided and reported, not deferred"
```

---

### Task 5: the consumers — WITHOUT THIS, TASKS 1-2 CHANGE NOTHING

**Files (other repos — read `rules/cross-repo-safety.md` Rule 0 and Rule 1 before branching):**
- `internal-tool2` — the `agent_devices` column + the heartbeat controller that persists it
- `betterqa-bot` — the fleet alert rule that reads it

A heartbeat key nobody stores and nobody alerts on is a write with no reader (`rules/one-rule-one-implementation.md` §"a write with no reader"). This repo has shipped that exact shape before: the `os_idle_seconds` work needed agent + server column + alert rule, and the agent half alone fixed nothing.

- [ ] **Step 1: Confirm the column does not already exist**

Query the live schema rather than reading a migration; `mcp__betterflow__betterflow_admin_artisan` or the MCP `betterflow_agent_devices` tool will show which fields come back per device. An `unknown_fields` bucket in the response means the agent is sending something the server drops.

- [ ] **Step 2: Add the column in `internal-tool2`** via a migration, and persist it in `AgentHeartbeatController` alongside `tracker_download_failed`. **Run the migration against prod BEFORE merging** — Railway auto-deploys `main`, so merging is deploying, and a read path selecting a column the DB lacks 500s (`rules/schema-rename-drift.md` §additive columns).

- [ ] **Step 3: Add the alert rule in `betterqa-bot`.** It must say *"another process owns port 5600 on this device"*, NOT *"tracker download failed"* — the whole reason for a distinct key is that the remedy is different and no restart will help.

- [ ] **Step 4: Verify end to end against a real device**, not a unit test: set the condition on one machine, confirm the value lands in `agent_devices`, and confirm the alert fires with the right wording.

- [ ] **Step 5: Commit and open PRs in each repo, cross-referencing this plan and the agent-side PR.**

Write cross-repo claims in the tense that matches the merge state — "ships in betterflow-sync #NNN, not live today" — never the present tense (`rules/diagnosis-discipline.md` Rule 6a corollary).

---

## Sequencing and review

Tasks 1 → 2 are ordered. Task 3 is independent. Task 4 depends on 1-2. Task 5 is a different repo and gates the *value* of 1-2, not their correctness.

Ship Tasks 1-4 as one PR against `betterflow-sync`. It is non-trivial, so it takes the pre-merge audit gate: two reviewers in parallel on the branch diff, one correctness lens, one SOLID lens, then a refutation round on the fix. Give both this instruction explicitly, because it is where the last three rounds spent their findings:

> The branch deliberately does not change control flow. Findings that propose changing what processes run, or how `_start_locked` and `restart_if_needed` branch, must first refute the measurement table in the F-3 note — two such changes have already shipped regressions.

---

## Self-review

**Spec coverage.** F-3 → Tasks 1, 2, 4. F-4 → Tasks 1, 2, 4 (same flag; its predicate stays unchanged by design, which Task 4 records). Sixth site → Task 3. Cross-repo consumers → Task 5. No requirement is unassigned.

**Placeholder scan.** Every step carries the code or the exact command. No "add error handling", no "similar to Task N".

**Type consistency.** `_external_server_not_responding` (bool) is spelled identically in Task 1 (`__init__`, both branches), Task 2 (`health_snapshot`, both allowlists, both tests) and Task 4. `_window_tracker_reachable()` (Task 3) is defined and used under one name. The heartbeat key string `external_server_not_responding` matches the attribute minus its leading underscore, consistent with the other health keys.

**Known residual, stated rather than hidden.** This plan does not make a device with a foreign process on :5600 capture anything — it cannot, and neither can any change at this layer. It makes the device say so. If the fleet later wants recovery rather than reporting, the change is in the rebuild route or a port-conflict resolution step, not in these five sites.
