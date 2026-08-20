# Agent Capture-Health Remainder — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining capture-health defects so that a macOS device losing window titles either cannot happen, or is fixed without a human noticing a Slack alert.

**Architecture:** Phase 0 answers two blocking questions cheaply, because both determine the SHAPE of later work rather than its details. Phases 1-3 then split by owning repo, one plan each, written after Phase 0 lands.

**Tech Stack:** Python 3.11 / pytest (betterflow-sync), Laravel/PHP + MySQL (internal-tool2), TypeScript (betterqa-bot), Apple PPPC configuration profiles + Miradore MDM.

**Spec:** the four issues themselves — betterflow-sync #205, #204, #195, internal-tool2 #2307. Each carries its own verified evidence and is the spec for its phase.

## Global Constraints

- **CI does not run on merge in most org repos; it DOES run on PRs in betterflow-sync.** Replicate every non-deploy workflow step locally before any merge regardless.
- **betterflow-sync tag builds run the suite on all four platforms.** A test that only passes on macOS is a FAILED RELEASE, not a red PR. Windows cannot be validated from a Mac (`browser_tracker.py` takes a `ctypes.windll` import branch and pytest dies at collection).
- **Never `skipif(darwin)`.** The PR gate is ubuntu-latest. Force `platform.system()` and inject fake modules instead.
- **internal-tool2 is actively worked** (main moved 2026-08-20 11:19). Re-run the Rule 1 remote check immediately before branching AND immediately before merging — a clean check has a shelf life.
- **Run the suite with** `PYTHONPATH=. python3 -m pytest` from the repo root.
- **Bump `src/__init__.py` before any build**, and remember the tag is a separate manual step after merging a release PR.

## Status of the wider effort

| Item | State |
|---|---|
| betterqa-bot #524 — cohort alerting | **CLOSED 2026-08-20 12:20**, fixed in #528. Not in this plan. |
| v1.5.125 | Released, all 4 legs green, both DMGs notarized, downloads verified by byte count. |
| Five blind devices | Recovered same day after a direct message. |

---

## Phase 0: Answer the two blocking questions

Both are cheap. Both change what Phases 1-2 should be. Do not plan #205 in detail before these land.

### Task 0.1: Determine whether a TCC.db write can succeed at all

**Why this blocks:** `grant_tcc_permissions()` writes `INSERT OR REPLACE INTO access` via `osascript ... with administrator privileges`. If that cannot work on supported macOS, the path is dead weight that burns the user's single admin-password prompt for nothing, and #205's fix is DELETION. If it can work, the fix is to stop writing the marker on failure and re-arm it on version change.

**This requires an interactive admin prompt, so an agent cannot run it.** Hand to a human.

**Files:** none. Research only.

- [ ] **Step 1: Back up the database**

```bash
sudo cp "/Library/Application Support/com.apple.TCC/TCC.db" \
        ~/tcc-backup-$(date +%Y%m%d-%H%M%S).db && echo "backed up"
```

- [ ] **Step 2: Test whether ROOT can write (the SIP question)**

Non-mutating: `BEGIN IMMEDIATE` takes a write lock and `ROLLBACK` releases it. Nothing changes.

```bash
sudo sqlite3 "/Library/Application Support/com.apple.TCC/TCC.db" \
  "BEGIN IMMEDIATE; ROLLBACK;" && echo "ROOT: WRITABLE" || echo "ROOT: BLOCKED"
```

- [ ] **Step 3: Test the path the CODE actually takes (the TCC question)**

This matters separately: `sudo` from a terminal holding Full Disk Access can succeed where an app-spawned admin shell fails. This is the faithful reproduction.

```bash
osascript -e 'do shell script "sqlite3 \"/Library/Application Support/com.apple.TCC/TCC.db\" \"BEGIN IMMEDIATE; ROLLBACK;\"" with administrator privileges' \
  && echo "OSASCRIPT: WRITABLE" || echo "OSASCRIPT: BLOCKED"
```

- [ ] **Step 4: Record both answers on issue #205**

They can differ, and the osascript one is the one that decides the fix. Record the macOS version (`sw_vers -productVersion`) and SIP state (`csrutil status`) alongside — the answer may be version-dependent.

- [ ] **Step 5: Restore only if anything changed**

Nothing above mutates, so restoration should be unnecessary. Confirm rather than assume:

```bash
sudo sqlite3 "/Library/Application Support/com.apple.TCC/TCC.db" "select count(*) from access;"
```

Compare against the same query on the backup. Equal ⇒ nothing changed.

### Task 0.2: Establish whether an MDM PPPC profile can grant Accessibility

**Why this blocks:** a PPPC (Privacy Preferences Policy Control) payload pushed from Miradore grants Accessibility permanently, survives app updates and code-signature changes, and needs no user action. That does not improve the failure — it removes the whole class, and makes #205 and much of #204 moot. It is the highest-leverage item remaining.

**Known inputs, already gathered:**
- Bundle identifier: `co.betterqa.betterflow` (`build.spec:254`, `src/ui/permissions.py:309`)
- Apple Team ID: `87NVC57J44`
- The app is Developer ID-signed and notarized in CI (`.github/workflows/build.yml`), and `make pkg` produces a signed .pkg for MDM.

**Files:**
- Create: `docs/mdm-pppc-accessibility.md` (findings + the payload, or the reason it will not work)

- [ ] **Step 1: Derive the code requirement string**

PPPC entries are keyed on identifier + a code requirement, not on a path. Get ours from a signed build:

```bash
codesign -dr - /Applications/BetterFlow.app 2>&1 | sed -n 's/^designated => //p'
```

Expected shape: `identifier "co.betterqa.betterflow" and anchor apple generic and certificate leaf[subject.OU] = "87NVC57J44"`

- [ ] **Step 2: Confirm Accessibility is PPPC-grantable**

This is the crux and it must be checked, not assumed. Apple allows MDM to pre-grant some services outright and others only as "AllowStandardUserToSetSystemService". Confirm which applies to `kTCCServiceAccessibility` for a supervised/managed Mac, and whether our fleet's Macs are supervised (user-approved MDM enrolment is required for PPPC to apply at all).

- [ ] **Step 3: Check what Miradore supports**

Miradore must be able to push a custom `.mobileconfig` with a `com.apple.TCC.configuration-profile-policy` payload. If it cannot push arbitrary profiles, this route dies here and that is a valid, cheap answer.

- [ ] **Step 4: Write up the finding**

State plainly which of these it is: (a) works, here is the payload; (b) works only for supervised devices and ours are not; (c) Miradore cannot push it; (d) Apple does not permit MDM to pre-grant Accessibility. Each leads somewhere different.

- [ ] **Step 5: Commit and post the outcome to #205**

Because #205's decision depends on it.

---

## Decision gate

After Phase 0, both answers are known. Then, and only then, write the per-repo plans:

| Outcome | Consequence for #205 |
|---|---|
| PPPC works | The sqlite path becomes vestigial. Delete it; MDM carries the grant. #205 closes as "fixed by configuration". |
| PPPC blocked + osascript write WORKS | Repair the path: stop writing the marker on cancel/failure, re-arm it when `agent_version` changes. |
| PPPC blocked + osascript write BLOCKED | Delete the path. Replace with the sanctioned `AXIsProcessTrusted(kAXTrustedCheckOptionPrompt: true)`, which opens the correct pane and costs nothing when dismissed. |

---

## Phase 1: betterflow-sync — #204, then #205 (separate plan, written after the gate)

**#204 does not depend on Phase 0 and can start immediately.** Scope, as its own plan:

- `_send_macos_pyobjc` returns `True` whenever `deliverNotification_` did not raise, which it never does. Check the authorization status before claiming success, so the caller's fallback becomes reachable.
- The persistent surfaces already reach the user and currently say the wrong thing: the tray shows `"ActivityWatch not responding"` where the real cause is a missing grant. Sending the CAUSE to a surface that persists beats a toast that may never render.
- Add a tray Diagnostics row for capture-permission state, matching the existing Device serial and Architecture rows.

**Verification note:** a notification's delivery cannot be asserted in jsdom-equivalent terms here either. `browser-platform-verification.md` applies — a real macOS smoke with notifications DENIED, and a second with them granted, is required before merge.

## Phase 2: internal-tool2 + betterqa-bot — #195 (separate plan)

Coordinate before branching; internal-tool2 is actively worked. Two halves:

1. **internal-tool2:** add an `os_idle_seconds` column to `agent_devices`, add the key to `AgentHeartbeatController`'s persisted whitelist and to `hasHealthTelemetry()`, and expose it on `InternalStatsController::deviceHealth`. Migration BEFORE merge (Railway auto-deploys `main`).
2. **betterqa-bot:** require it in the `no_capture` rule — no capture AND recent input is a fault; no capture while the OS idle clock says nobody was there is a user who went home. Tri-state, and **fail toward NOT suppressing when the field is absent**, or the alert silently stops firing for every build that cannot report it.

The server-side contract is already posted on #195 (2026-08-18). Read it rather than re-deriving.

## Phase 3: internal-tool2 #2307 — degraded_since semantics (separate plan)

The quoted duration is observability age, not fault age, and it fails toward inventing a cause: two devices' stamps land within 94 seconds of the v1.5.120 tag purely because that is when they started reporting. Either compute a duration that reflects the fault, or change the wording so it does not claim to. Smallest honest fix is the wording.

## Phase 4 (ops, no code): three unreachable devices

Not fixable by message; each needs the machine.

| Device | User | State | Action |
|---|---|---|---|
| 54 | Catalin Moise | `window_titles_blind`, last seen 15d, v1.5.120 | He has a working current machine. Confirm retired, then mark the row inactive so it stops occupying the degraded count. |
| 44 | Matei Cocora | `no_capture`, last seen 3.4d | Check whether the person is active; a `no_capture` on a live machine is billable time lost. |
| 51 | Celia Stir | `tracker_install_failed`, last seen 16d | Tracker binaries never installed. Needs a reinstall on the machine. |

A retired device that keeps reporting degraded is noise in every future count, so closing these out is what keeps the cohort alert meaningful.
