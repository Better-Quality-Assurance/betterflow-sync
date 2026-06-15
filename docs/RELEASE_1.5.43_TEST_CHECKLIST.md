# BetterFlow Agent 1.5.43 — Release Test Checklist

Context: 1.5.43 fixes the 2026-06-15 fleet incident (agents stopped uploading
window/input activity; users showed idle while working). It pairs with server
PR internal-tool2 **#1833** (revert breaking aggregate index + send
`working_hours` in `/api/agent/config`).

**Do not release to the fleet until every box below passes on a real machine.**

## 0. Pre-req
- [ ] Server **#1833 deployed** first (so `/api/agent/config` returns `working_hours`
      and the broken `agent_aggregate_unique_v2` index is gone). Verify:
      `curl -s -H "Authorization: Bearer <device_token>" -H "User-Agent: BetterFlow/1.5.43" \
       https://app.betterflow.eu/api/agent/config | jq .data.working_hours`
- [ ] Build is `BetterFlow/1.5.43` (tray → About, or `--version`).

## 1. Basic upload (no regression)
- [ ] Install, launch, log in. Tray shows `API: Connected`, `ActivityWatch: Running`.
- [ ] Work for ~3 min (switch a couple apps, type). Within ~1–2 sync cycles, the
      events appear in prod: `agent_events` for the device has fresh `window` +
      `input` rows with `created_at` = now.
- [ ] `/agent/my` shows current activity in the timeline (not idle) and Active
      time increasing.

## 2. No client-side dropping (the core fix)
- [ ] Tail the agent log; confirm **no** `"Window event skipped after inactivity
      cutoff/AFK overlap"` lines appear during active work.
- [ ] Keep ONE window focused and keep typing for >10 min with few app switches;
      confirm those minutes still upload (previously dropped).
- [ ] `1 sent / 0 filtered` style log lines reflect real activity going up; the
      "filtered" count should be dedup only, not real-activity drops.

## 3. Backlog reconcile on restart (recovers a stuck day)
- [ ] Simulate a stall: stop networking / block the API for ~5 min while you work
      (events accumulate locally only). Confirm prod has a gap for that window.
- [ ] Restore networking, then **Quit + relaunch** the agent.
- [ ] After the first post-restart sync, the gap **backfills** in prod
      (`_reconcile_backlog` rewound checkpoints to start-of-day; server upserts by
      event id, so no duplicates). Confirm event counts didn't double.

## 4. Working-hours enforcement (B2E / Trainee)
- [ ] On a **B2E** account (`users.relationship = 'B2E'`), confirm config shows
      `working_hours.enforced = true`, `08:00`–`22:00`, Mon–Fri.
- [ ] Set the machine clock to **07:30** (or generate activity, then check a
      pre-08:00 timestamp): confirm those events are **NOT uploaded** (gated by
      `_within_working_hours`), and the checkpoint still advances (no infinite
      re-fetch).
- [ ] Set clock to **22:30**: same — not uploaded.
- [ ] Set clock to **10:00 weekday**: events upload normally.
- [ ] On a **B2B** account (`enforced=false`): activity at any hour uploads
      (unrestricted).

## 5. No idle over-count (pair with server idle handling)
- [ ] Leave the machine idle (no input, screen on one window) for >20 min.
- [ ] Confirm tracked/Active time does **not** keep climbing during the idle gap
      on `/agent/my` (server decides idle from AFK). If it does, the server-side
      idle handling (option 1) is still needed — flag before release.

## 6. Stability
- [ ] Run for ~30 min; no crashes, no runaway CPU, log clean.
- [ ] `events_synced_count` increases steadily; `Queue` stays near 0 under normal
      network.

## Rollback
- Agent: users reinstall the previous signed build; no data loss (local aw-db
  retains events).
- Server: revert PR #1833 is itself a revert; no separate rollback needed.

---
Reset the machine clock after the working-hours tests.
