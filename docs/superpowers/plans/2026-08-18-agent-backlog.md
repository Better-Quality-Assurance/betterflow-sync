# BetterFlow Agent Backlog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clear the five open issues on `betterflow-sync` plus one alert-wording defect, so the repo's open list reflects only work nobody has done.

**Architecture:** Five independent workstreams. Two are issue hygiene with no code. One (#195) is a small telemetry change: the discriminating signal already exists on-device and simply never reaches the server. One is a one-line message correction with a test. Two are investigations that may legitimately end in "no code change", and the plan says so rather than forcing a fix.

**Tech Stack:** Python 3.11, pytest, PyInstaller. Run the suite with `PYTHONPATH=. python3 -m pytest`.

**Spec:** The issues themselves are the spec: #194, #184, #195, #188, #190, plus the audit finding on `_report_dropped_events`'s wording (this session, 2026-08-17).

## Global Constraints

- **The PR gate is ubuntu, and the tag build is four platforms.** Never write a test that only passes on macOS. Inject the platform (`system="Darwin"`) rather than `skipif`; a skip means the guard runs in no PR-gating job at all. Verify with the fake-platform plugin in `[[tests-that-only-pass-on-macos]]` BEFORE dispatching reviewers.
- **Every bug fix ships with a test that fails pre-fix and passes post-fix.** Prove it: commit the fix, `git checkout HEAD~1 -- <impl paths>`, watch it fail, restore. Never `git stash` (shared across worktrees).
- **CI does run on PRs here** (`build.yml` `test` job, ubuntu, on `pull_request`), but `build`/`release` are tag-only and the tag build *skips* `test`. Read `gh pr checks`; never assume.
- **`HEARTBEAT_HEALTH_KEYS` is a hard allowlist.** A field can be collected, logged and unit-tested end to end and still never leave the machine because its name is missing from that tuple. Any new heartbeat field needs BOTH the producer and the allowlist entry, and a test that would fail if either is missing.
- **Privacy:** anything added to the heartbeat must describe the machine's *capability to record*, never what was recorded. Update the "Device identifiers sent on the heartbeat" section of `CLAUDE.md` in the same PR as any heartbeat change.
- **No closing keywords in commit messages** — a squash executes them. Put `Closes #N` in the PR body only.
- Explicit-path staging only, never `git add -A`.
- Work in a git worktree cut from `origin/main`, never the primary checkout.

---

## Workstream A — Issue hygiene (no code)

### Task 1: Close #194 with shipped evidence

**Files:** none (GitHub only)

**Interfaces:**
- Consumes: nothing
- Produces: nothing

- [ ] **Step 1: Verify the fix is in the shipped tag, not just on main**

```bash
cd <worktree>
git show v1.5.124:src/sync/macos_window_watcher.py | grep -c "_notify_accessibility_required_once"   # expect 4
git show v1.5.123:src/sync/macos_window_watcher.py | grep -c "_notify_accessibility_required_once"   # expect 0
```

Expected: `4` then `0`. If the second is non-zero, STOP — the fix predates the release and the issue was open for a different reason.

- [ ] **Step 2: Confirm the release is the one the fleet has**

```bash
gh api repos/Better-Quality-Assurance/betterflow-sync/releases/latest --jq .tag_name   # expect v1.5.124
```

- [ ] **Step 3: Close with the evidence in the comment**

```bash
gh issue close 194 --repo Better-Quality-Assurance/betterflow-sync --comment "Shipped in v1.5.124 (PR #197).

Verified in the tag rather than on main: \`_notify_accessibility_required_once\` appears 4x in \`v1.5.124:src/sync/macos_window_watcher.py\` and 0x in \`v1.5.123\`, and \`releases/latest\` is v1.5.124.

The agent now asks the user to grant Accessibility once per launch, re-arming if the grant is later revoked, instead of only writing a warning to a log file nobody reads. Capture of app names and durations was never affected; window titles were."
```

---

### Task 2: Update #184 — two of three asks shipped

**Files:** none (GitHub only)

**Interfaces:**
- Consumes: nothing
- Produces: the remaining scope that Task 5 implements

- [ ] **Step 1: Verify each of the three asks against the shipped tag**

```bash
cd <worktree>
# (a) self-perpetuating updater — arch-aware asset selection
git show v1.5.124:src/update_checker.py | grep -c "true_machine_arch"        # expect >0
# (b) user can see which build they are on — tray row
git show v1.5.124:src/ui/tray.py | grep -c "Architecture:"                   # expect >0
# (c) agent reports its architecture so the fleet can answer "who is on Intel?"
git show v1.5.124:src/sync/bf_client.py | grep -c "machine_arch\|arch"       # expect 0 — still missing
```

- [ ] **Step 2: Post the status, do NOT close**

```bash
gh issue comment 184 --repo Better-Quality-Assurance/betterflow-sync --body "Two of the three asks shipped in v1.5.124; leaving this open for the third.

**Done — the updater no longer traps people on the Intel build.** #191 and #198 make asset selection read the HARDWARE architecture via \`true_machine_arch()\`, so an Intel build on an M-series Mac is now offered the arm64 DMG. Smoked against the live release payload: \`Intel build on M-series -> BetterFlow-macOS-arm64.dmg\`.

**Done — the user can see which build they are on.** The tray Diagnostics row reads \`Architecture: Intel build on Apple Silicon — switch to the Apple Silicon version\` on exactly that case.

**Still open — we cannot answer 'who is on Intel?'.** The agent does not report its architecture on the heartbeat. \`HEARTBEAT_HEALTH_KEYS\` has 8 keys and none of them is architecture, so the fleet view still cannot enumerate affected devices. That is the remaining scope of this issue."
```

---

## Workstream B — #195: give the no_capture alert its discriminator

The alert cannot tell *user away* from *tracker dead*. The agent already knows: `src/sync/os_idle.py::get_system_idle_seconds()` reads `HIDIdleTime` on macOS and `GetLastInputInfo` on Windows, and is already used by `idle_manager` and `sync_engine`. It is simply never sent. This workstream sends it.

**Design decision:** send the raw reading, not a verdict. The server owns the alerting policy, and a boolean computed on-device would be undebuggable when it disagrees with the timeline.

**Privacy note for the PR body and `CLAUDE.md`:** `os_idle_seconds` says "the keyboard/mouse were last touched N seconds ago". It describes presence, not content, and is strictly coarser than the AFK stream already uploaded. It introduces no new data category.

### Task 3: Add `os_idle_seconds` to the heartbeat

**Files:**
- Modify: `src/sync/bf_client.py` (the `HEARTBEAT_HEALTH_KEYS` tuple, ~line 444)
- Modify: `src/main.py` (the telemetry dict, near the `sync_stale_seconds` block ~line 2016)
- Modify: `CLAUDE.md` ("Device identifiers sent on the heartbeat")
- Test: `tests/test_os_idle_heartbeat.py`

**Interfaces:**
- Consumes: `src.sync.os_idle.get_system_idle_seconds() -> Optional[float]`
- Produces: heartbeat key `os_idle_seconds: int | None`

- [ ] **Step 1: Write the failing test**

```python
"""The no_capture alert cannot tell a user who is away from a dead tracker (#195).

The agent has always known: get_system_idle_seconds() reads HIDIdleTime on
macOS and GetLastInputInfo on Windows. It just never left the device, because
HEARTBEAT_HEALTH_KEYS is a hard allowlist and the key was not in it.

Both halves are asserted here on purpose. A producer-only test passes while the
allowlist silently drops the field, which is the exact failure the tuple's own
comment warns about.
"""

from unittest.mock import MagicMock, patch

from src.sync.bf_client import BetterFlowClient


def test_the_allowlist_carries_os_idle_seconds():
    assert "os_idle_seconds" in BetterFlowClient.HEARTBEAT_HEALTH_KEYS


def test_a_supplied_os_idle_reading_reaches_the_request_body():
    client = BetterFlowClient.__new__(BetterFlowClient)
    captured = {}

    def _fake_request(method, path, data=None, **kw):
        captured.update(data or {})
        return {"data": {}}

    client._request = _fake_request
    client._detect_timezone = lambda: "Europe/Bucharest"

    client.send_heartbeat(agent_version="1.5.125", health={"os_idle_seconds": 12})

    assert captured["os_idle_seconds"] == 12


def test_an_unreadable_idle_clock_is_omitted_not_zeroed():
    """None must not become 0. Zero means 'the user is at the keyboard right
    now', which is the opposite of 'we could not tell' — and it is the reading
    the alert would act on."""
    client = BetterFlowClient.__new__(BetterFlowClient)
    captured = {}
    client._request = lambda method, path, data=None, **kw: (captured.update(data or {}), {"data": {}})[1]
    client._detect_timezone = lambda: "UTC"

    client.send_heartbeat(agent_version="1.5.125", health={})

    assert "os_idle_seconds" not in captured
```

- [ ] **Step 2: Run it and watch it fail**

Run: `PYTHONPATH=. python3 -m pytest tests/test_os_idle_heartbeat.py -v`
Expected: FAIL — `test_the_allowlist_carries_os_idle_seconds` asserts a key that is not in the tuple, and the body test finds the field dropped at the allowlist boundary.

- [ ] **Step 3: Add the key to the allowlist**

In `src/sync/bf_client.py`, inside `HEARTBEAT_HEALTH_KEYS`, after `"sync_stale_seconds",`:

```python
        # Seconds since the user last touched the keyboard or mouse, read from
        # the OS (HIDIdleTime / GetLastInputInfo), NOT from the AFK tracker.
        # This is the discriminator the no_capture alert never had: with no
        # events and a LARGE idle time the user is away and nothing is wrong;
        # with no events and a SMALL idle time the trackers are dead and
        # billable time is being lost. Those two produced identical evidence on
        # the wire until this field existed (#195). Omitted entirely when the
        # probe cannot read a value — see the producer in main.py for why null
        # must not become 0.
        "os_idle_seconds",
```

- [ ] **Step 4: Add the producer**

In `src/main.py`, immediately after the `sync_stale_seconds` block:

```python
        # The OS idle clock, so the server can tell "user away" from "tracker
        # dead". Both look identical from event ages alone, which is why the
        # no_capture alert has fired on people who were simply not at their
        # desk (#195). Best-effort and OMITTED when unreadable: a null coerced
        # to 0 would read as "at the keyboard this second", turning an unknown
        # into the strongest possible claim of presence.
        try:
            idle_seconds = get_system_idle_seconds()
            if idle_seconds is not None:
                telemetry["os_idle_seconds"] = int(idle_seconds)
        except Exception as e:  # noqa: BLE001
            logger.debug("os-idle telemetry unavailable: %s", e)
```

Add the import beside the other `src.sync` imports in `main.py`, following the module's try/except pattern:

```python
try:
    from .sync.os_idle import get_system_idle_seconds
except ImportError:
    from sync.os_idle import get_system_idle_seconds
```

- [ ] **Step 5: Run the tests and the neighbours**

Run: `PYTHONPATH=. python3 -m pytest tests/test_os_idle_heartbeat.py tests/test_heartbeat_health.py -v`
Expected: PASS. If `tests/test_heartbeat_health.py` does not exist, run `PYTHONPATH=. python3 -m pytest tests/ -q -k heartbeat` instead.

- [ ] **Step 6: Update CLAUDE.md**

In the "Device identifiers sent on the heartbeat" section, add `os_idle_seconds` to the tracker-health bullet, describing it as presence-not-content. While there, correct the sentence claiming `HEARTBEAT_HEALTH_KEYS` is "the complete, enforced list of what the heartbeat forwards" — it is the allowlist for the `health` dict only; `agent_version`, `timezone`, `hardware_serial` and `disclosure_acknowledgement` are separate top-level fields.

- [ ] **Step 7: Commit**

```bash
git add src/sync/bf_client.py src/main.py CLAUDE.md tests/test_os_idle_heartbeat.py
git commit -m "feat(telemetry): report the OS idle clock so no_capture can tell away from dead"
```

- [ ] **Step 8: Prove the pre-fix failure**

```bash
git checkout HEAD~1 -- src/sync/bf_client.py src/main.py
PYTHONPATH=. python3 -m pytest tests/test_os_idle_heartbeat.py -v   # expect FAIL
git checkout HEAD -- src/sync/bf_client.py src/main.py
PYTHONPATH=. python3 -m pytest tests/test_os_idle_heartbeat.py -v   # expect PASS
```

- [ ] **Step 9: Witness the allowlist independently of the producer**

Mutate ONLY the allowlist (remove `"os_idle_seconds"`) and confirm a named test reddens; then restore. A producer that works while the allowlist drops the field is the failure this pair exists to catch, so both must be witnessed separately.

---

### Task 4: Hand the server the rule (cross-repo, do not merge blind)

**Files:** none in this repo.

- [ ] **Step 1: Write the consumer requirement into #195**

The agent half is useless until the alert reads it. Post to #195 the exact contract: `os_idle_seconds` is an integer, absent when unreadable, and the `no_capture` rule should require it to be **present and small** (suggest < 900s, i.e. the user was at the machine within the alert window) before firing. Absence must NOT satisfy the condition — an old agent that never sends it would otherwise silently stop alerting.

- [ ] **Step 2: State the cross-repo tense explicitly**

When referring to this from betterqa-bot or internal-tool2, write "ships in betterflow-sync v1.5.125, not live on the fleet today" rather than the present tense. A reviewer reading the other repo's main cannot see this change and will correctly report the field as non-existent.

---

## Workstream C — #184 remainder: report the architecture

### Task 5: Add `machine_arch` to the heartbeat

**Files:**
- Modify: `src/sync/bf_client.py` (`HEARTBEAT_HEALTH_KEYS`)
- Modify: `src/main.py` (telemetry dict)
- Test: `tests/test_arch_heartbeat.py`

**Interfaces:**
- Consumes: `src.machine_arch.true_machine_arch() -> str` (returns `""` when undetermined — see its docstring)
- Produces: heartbeat key `machine_arch: str | None`

- [ ] **Step 1: Write the failing test**

```python
"""The fleet cannot answer "who is on the Intel build?" (#184).

true_machine_arch() returns the HARDWARE architecture, seeing through Rosetta,
and returns "" when its probe never resolved. That empty string must be sent as
null rather than as an empty string, so the fleet view can distinguish "we asked
and could not tell" from "arm64" without string-matching on emptiness.
"""

from unittest.mock import patch

from src.sync.bf_client import BetterFlowClient


def test_the_allowlist_carries_machine_arch():
    assert "machine_arch" in BetterFlowClient.HEARTBEAT_HEALTH_KEYS


def test_a_rosetta_translated_mac_reports_arm64_not_x86_64():
    from src.machine_arch import true_machine_arch

    arch = true_machine_arch(system="Darwin", machine="x86_64", translated=True)

    assert arch == "arm64", "the whole point: report the hardware, not the process"


def test_an_undetermined_arch_is_sent_as_null_not_empty_string():
    client = BetterFlowClient.__new__(BetterFlowClient)
    captured = {}
    client._request = lambda method, path, data=None, **kw: (captured.update(data or {}), {"data": {}})[1]
    client._detect_timezone = lambda: "UTC"

    client.send_heartbeat(agent_version="1.5.125", health={"machine_arch": None})

    assert "machine_arch" in captured, "membership is tested with `in`, so null is a real report"
    assert captured["machine_arch"] is None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `PYTHONPATH=. python3 -m pytest tests/test_arch_heartbeat.py -v`
Expected: FAIL on the allowlist assertion.

- [ ] **Step 3: Add the key**

In `HEARTBEAT_HEALTH_KEYS`:

```python
        # The HARDWARE architecture, seeing through Rosetta 2 — an x86_64 build
        # translated on Apple Silicon reports arm64 here, which is the whole
        # point. Without it the fleet cannot enumerate who is on the wrong build
        # (#184), and the answer was previously only visible in the tray of the
        # affected machine. null means true_machine_arch() returned "" (its probe
        # never resolved); do not coerce that to a string.
        "machine_arch",
```

- [ ] **Step 4: Add the producer**

In `src/main.py`, after the `os_idle_seconds` block:

```python
        # Report the hardware architecture so the fleet can answer "who is on
        # the Intel build?". true_machine_arch() returns "" when its Rosetta
        # probe never resolved; send that as null so the server can tell
        # "undetermined" from a real value without string-matching emptiness.
        try:
            arch = true_machine_arch()
            telemetry["machine_arch"] = arch or None
        except Exception as e:  # noqa: BLE001
            logger.debug("machine-arch telemetry unavailable: %s", e)
```

`main.py` already imports `true_machine_arch`; confirm with `grep -n "true_machine_arch" src/main.py` before adding a duplicate import.

- [ ] **Step 5: Run, commit, prove pre-fix failure**

Same shape as Task 3 steps 5, 7 and 8, substituting `tests/test_arch_heartbeat.py`.

---

## Workstream D — correct the dropped-events wording

### Task 6: Stop calling preserved events "lost"

**Files:**
- Modify: `src/sync/sync_engine.py:4093`
- Test: `tests/test_dropped_events_wording.py`

**Interfaces:**
- Consumes: `SyncEngine._report_dropped_events(summary: dict)`
- Produces: nothing

**Context the implementer needs:** `_report_dropped_events` is already correct about *severity* — it splits genuine loss from a benign flush and only warns when recent, bucketed events were rejected. The defect is narrower: `remove_failed()` **moves** exhausted events to a dead-letter table ("does NOT hard-delete") and `requeue_storable_dead_letter()` replays rows that become storable again, so the events are undelivered-and-preserved, not lost. `dead_letter_count` already rides the heartbeat, so ops can see the backlog. The current wording sends people looking for data that is sitting in a table.

- [ ] **Step 1: Write the failing test**

```python
"""The drop alert calls preserved events "lost activity".

remove_failed() MOVES exhausted events to the dead-letter table rather than
deleting them, and requeue_storable_dead_letter() replays the ones that become
storable again. The events are undelivered and preserved. Telling ops they are
lost sends someone hunting for data that is in a table, and it inflates a
warning that is otherwise correctly scoped to genuine rejections.
"""

from unittest.mock import MagicMock

from src.sync.sync_engine import SyncEngine


def _engine_with_reporter():
    engine = SyncEngine.__new__(SyncEngine)
    engine.error_reporter = MagicMock()
    engine._dropped_window_age = lambda oldest, newest: "spanning 2h"
    return engine


def test_the_drop_warning_does_not_claim_the_events_are_lost():
    engine = _engine_with_reporter()

    engine._report_dropped_events(
        {"count": 3, "real_loss_count": 3, "bucket_ids": ["aw-watcher-window_host"], "oldest": 1, "newest": 2}
    )

    message = engine.error_reporter.capture.call_args[0][0]
    assert "lost activity" not in message
    assert "dead-letter" in message, "say where they are, or the reader cannot go look"


def test_it_still_warns_and_still_names_the_count():
    """The severity is right and must not be softened along with the wording:
    these are events the server rejected, and somebody should look."""
    engine = _engine_with_reporter()

    engine._report_dropped_events(
        {"count": 3, "real_loss_count": 3, "bucket_ids": ["aw-watcher-window_host"], "oldest": 1, "newest": 2}
    )

    kwargs = engine.error_reporter.capture.call_args.kwargs
    assert kwargs["level"] == "warning"
    assert kwargs["fingerprint"] == "offline-queue-events-dropped"
    assert "3" in engine.error_reporter.capture.call_args[0][0]


def test_the_benign_flush_path_is_untouched():
    engine = _engine_with_reporter()

    engine._report_dropped_events(
        {"count": 2, "real_loss_count": 0, "bucket_ids": [], "oldest": 1, "newest": 2}
    )

    kwargs = engine.error_reporter.capture.call_args.kwargs
    assert kwargs["level"] == "info"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `PYTHONPATH=. python3 -m pytest tests/test_dropped_events_wording.py -v`
Expected: FAIL on `"lost activity" not in message`.

- [ ] **Step 3: Correct the message**

In `src/sync/sync_engine.py`, replace the warning string:

```python
                self.error_reporter.capture(
                    f"Dropped {real} queued event(s) after max retries — the "
                    f"server rejected them; held in dead-letter for replay, not "
                    f"discarded (buckets={buckets}, {window}{extra})",
                    level="warning",
                    tags={"component": "offline-queue"},
                    fingerprint="offline-queue-events-dropped",
                )
```

- [ ] **Step 4: Run, commit, prove pre-fix failure, restore**

Same shape as Task 3 steps 5, 7 and 8.

---

## Workstream E — investigations that may end in no code

### Task 7: #188 — why did the Rosetta notice not save Carmen?

**Files:** none until the mechanism is known.

The preflight and the notification both already exist: `aw_manager._rosetta_missing()` (line 768) and `_notify_rosetta_required_once()` (line 822, called at 857). Carmen still lost ~90 minutes on **v1.5.122**, which contains both. So the issue as written ("the app looks healthy") is either stale or the notice did not reach her.

- [ ] **Step 1: Establish which, before proposing anything**

```bash
git tag --contains $(git log -1 --format=%H -S"_notify_rosetta_required_once" -- src/aw_manager.py) \
  | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | head -1
```

Expected: the first release carrying the notice. If it is **after** v1.5.122, the issue is already fixed and needs closing with that evidence. If it is v1.5.122 or earlier, the notice fired and did not help — a different problem, and a bigger one.

- [ ] **Step 2: If the notice shipped before her incident, get her device's log**

Ask for the agent log around 2026-08-13 and grep for the notification call and for `send_notification` failures. macOS suppresses notifications for apps that have never been granted them, and `_notify_rosetta_required_once` is best-effort with a swallowed exception — so "fired" and "was seen" are different claims.

- [ ] **Step 3: Record the verdict on the issue either way**

"The notice fires and macOS suppressed it" and "the notice shipped after the incident" lead to opposite fixes. Do not write code until one of them is evidenced. Note that the underlying fix — native arm64 trackers — remains blocked upstream: ActivityWatch publishes macOS arm64 binaries only in `v0.14.0b*` prereleases, and `aw_manager.AW_VERSION` pins `v0.13.2`.

---

### Task 8: #190 — build a harness that can actually answer the question

**Files:**
- Modify: `requirements-dev.txt`
- Create: `tests/conftest_time_freeze.py` (or extend the existing sweep harness)

The seven listed tests are an **unverified lead**, not findings. The harness that produced them freezes `datetime` but not `time.time()` / `time.monotonic()`, and `src/aw_manager.py` alone has 2 and 12 of those respectively — so a frozen-`datetime` run skews the two clocks ~21 hours apart. The reported `assert 32366.98 < 120` is consistent with skew, not with a day-boundary bug.

- [ ] **Step 1: Add `time-machine` to `requirements-dev.txt`**

It patches `time.time`, `time.monotonic` and `datetime` together at the C level, which is the property the current harness lacks.

- [ ] **Step 2: Re-run the seven under a harness that freezes all three clocks**

```bash
PYTHONPATH=. python3 -m pytest \
  tests/test_aw_manager_idle_restart.py::test_afk_age_prefers_discovered_betterflow_bucket_over_stale_legacy \
  tests/test_mic_activity.py tests/test_window_title_capture_telemetry.py -v
```
under a `time_machine.travel("2026-08-18 00:01", tick=False)` fixture.

- [ ] **Step 3: Control the harness before believing it**

Run the same seven on the real clock and confirm they pass (`1671 passed` was the recorded baseline). A harness that reddens tests on both clocks is measuring itself. Then re-run the FULL suite under the new harness: if the failure count drops from 8 toward 1, the previous 14 really were the instrument.

- [ ] **Step 4: Report the number, then decide**

Only tests that fail under the all-clocks harness AND pass on the real clock are findings. Fix those with the injected-`now` pattern already used in `tests/test_queue.py`; close the rest as instrument artifacts, naming them.

---

## Self-Review

**Spec coverage:** #194 → A1. #184 → A2 (status) and C1 (remaining scope). #195 → B1 (agent half) and B2 (server contract). Alert wording → D1. #188 → E1. #190 → E2. No issue is unaddressed.

**Placeholders:** none. Every code step carries the actual code; the two investigations carry the exact command that decides the branch, and say plainly that "already fixed" and "no code needed" are successful outcomes.

**Type consistency:** `get_system_idle_seconds() -> Optional[float]` is consumed in B1 and cast with `int()`. `true_machine_arch() -> str` is consumed in C1 and mapped `"" -> None`. Both heartbeat keys are added to the same `HEARTBEAT_HEALTH_KEYS` tuple and asserted by name in their own tests. `_report_dropped_events(summary: dict)` keeps its signature in D1.

**Sequencing note:** B1 and C1 both edit `HEARTBEAT_HEALTH_KEYS` and the same telemetry block in `main.py`. Run them in order on one branch, or expect a conflict. Everything else is independent.
