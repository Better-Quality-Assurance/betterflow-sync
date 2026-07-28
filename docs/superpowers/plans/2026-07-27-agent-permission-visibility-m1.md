# Agent Permission & Capture Visibility — M1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the three agent capture-failure classes visible as deduped, deterministic Slack alerts, without an agent release.

**Architecture:** A new `permissions_degraded` category in the existing `/api/internal/device-health` endpoint emits three reason codes derived server-side. The existing betterqa-bot monitor (10-min cron, `alert_state` cooldown, template-literal formatting, Slack path) consumes them with three new `reasonText()` cases and a per-reason cooldown. Nothing flows through `tracking_degraded`, so no existing consumer changes behaviour.

**Tech Stack:** PHP 8 / Laravel 12 / MySQL / PHPUnit 11 (internal-tool2); TypeScript / vitest / Drizzle+Postgres (betterqa-bot).

**Spec:** `docs/superpowers/specs/2026-07-27-agent-permission-visibility-design.md`

## Global Constraints

Every task's requirements implicitly include this section.

- **internal-tool2 is PR-only. No auto-merge.** 31 commits in 7 days across two authors; the agent-health code in scope changed twice in the three days before this plan (#2170, #2178). Per `rules/cross-repo-safety.md` Rule 1 the auto-merge default is SUSPENDED. Open the PR and stop.
- **Edit internal-tool2 in a worktree, but run its PHP suite against the MAIN checkout.** The Docker test container mounts the main checkout's `src/`, not worktrees (`rules/cross-repo-safety.md`, internal-tool2 caveat). Coordinate so only one session drives the main checkout, on a clean `main`.
- **Migrations run BEFORE merge, never after.** Railway auto-deploys `main`, so merging is deploying (`rules/schema-rename-drift.md` §additive columns).
- **CI does not run. You are the CI.** `ls` each repo's `.github/workflows/` and run every non-deploy workflow's steps locally. Do not look for `ci.yml` by name — betterflow-sync and others have no file by that name.
- **New columns are nullable with NO default.** `tracking_degraded`, `consecutive_sync_failures`, `idle_tracker_stale_restarts` and `idle_while_active_detections` are all `NOT NULL DEFAULT` columns where never-reported is byte-identical to healthy. Do not add a fifth. Match `window_titles_captured_recently`.
- **Every new detector excludes never-reported devices by an explicit predicate**, never by relying on a default.
- **Severity for every `permissions_degraded` reason is `warning`, never `critical`.** `critical` bypasses Slack coalescing and triggers the Telegram fallback. A standing config fault must not page.
- **The Slack line must be self-sufficient** — person, device, platform, and the exact remedial action. There is deliberately no UI to click through to.
- **Do NOT flip `ALERT_ON_BLIND_WINDOW_TITLES`.** It stays `false` permanently and is superseded.
- **Deploy order is server-first and that is safe.** The bot's `reasonText()` `default:` branch names the gap honestly rather than fabricating, so a server ahead of the bot degrades gracefully.

---

## File Structure

**internal-tool2** (worktree off `origin/main`):
- Create: `src/database/migrations/2026_07_28_000000_add_idle_tracker_blind_to_agent_devices_table.php` — one nullable boolean column.
- Modify: `src/app/Http/Controllers/Api/Agent/AgentHeartbeatController.php` — accept and persist `idle_tracker_blind`.
- Modify: `src/app/Http/Controllers/Api/InternalStatsController.php` — extract the two existing detectors to private methods, add a third.
- Test: `src/tests/Feature/Agent/AgentDeviceHealthEndpointTest.php` — existing file, extend it.

**betterqa-bot** (worktree off `origin/main`):
- Modify: `src/monitor/betterflow-device-health.ts` — category union, three `reasonText()` cases, `severityFor()` clause, per-reason cooldown.
- Test: `src/__tests__/betterflow-device-health.test.ts` — existing file, extend it.

`deviceHealth()` is currently 204 lines holding two inline detection blocks. Task 2 extracts each into a private method before Task 3 adds a third, so the method becomes a readable pipeline instead of growing to ~280 lines.

---

### Task 1: Persist `idle_tracker_blind`

The agent has been sending this key since it was added to `HEARTBEAT_HEALTH_KEYS`; the server has nowhere to put it. This is the only migration in M1.

**Files:**
- Create: `src/database/migrations/2026_07_28_000000_add_idle_tracker_blind_to_agent_devices_table.php`
- Modify: `src/app/Http/Controllers/Api/Agent/AgentHeartbeatController.php` (`hasHealthTelemetry()` ~`:275-291`, `buildHealthUpdates()` ~`:456-496`)
- Test: `src/tests/Feature/Agent/AgentDeviceHealthEndpointTest.php`

**Interfaces:**
- Produces: `agent_devices.idle_tracker_blind` — `boolean NULL`, no default. Read by Task 4.

- [ ] **Step 1: Write the failing test**

Add to `AgentDeviceHealthEndpointTest.php`:

```php
    #[Test]
    public function heartbeat_persists_idle_tracker_blind_and_leaves_it_null_when_unreported(): void
    {
        $device = $this->makeDevice();

        // A heartbeat that does not mention the key must leave it NULL —
        // "never told us" must stay distinguishable from "told us false".
        $this->postJson('/api/agent/heartbeat', [
            'agent_version' => '1.5.118',
            'consecutive_sync_failures' => 0,
        ], $this->agentAuthHeaders($device))->assertOk();

        $this->assertNull(
            DB::table('agent_devices')->where('id', $device->id)->value('idle_tracker_blind'),
            'an unreported idle_tracker_blind must stay NULL, not default to false'
        );

        $this->postJson('/api/agent/heartbeat', [
            'agent_version' => '1.5.118',
            'idle_tracker_blind' => true,
        ], $this->agentAuthHeaders($device))->assertOk();

        $this->assertSame(
            1,
            (int) DB::table('agent_devices')->where('id', $device->id)->value('idle_tracker_blind')
        );
    }
```

If `makeDevice()` / `agentAuthHeaders()` do not already exist in this test class, read the file's existing setup helpers and use those instead — do not invent new ones.

- [ ] **Step 2: Run the test to verify it fails**

From the MAIN internal-tool2 checkout on a clean `main`:

```bash
docker compose exec app php artisan test --filter=heartbeat_persists_idle_tracker_blind
```

Expected: FAIL — `SQLSTATE... Unknown column 'idle_tracker_blind'`.

- [ ] **Step 3: Write the migration**

```php
<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    /**
     * The agent already reports idle_tracker_blind on every heartbeat; the server
     * had nowhere to store it, so the one signal that distinguishes "a denied
     * Input Monitoring grant a restart can't fix" from a transient restart was
     * being dropped on arrival.
     *
     * Nullable with NO default, deliberately. Its neighbours
     * (tracking_degraded, consecutive_sync_failures, idle_tracker_stale_restarts,
     * idle_while_active_detections) are NOT NULL DEFAULT, which makes a device
     * that has never reported byte-identical to a healthy one. Do not copy that.
     */
    public function up(): void
    {
        Schema::table('agent_devices', function (Blueprint $table) {
            $table->boolean('idle_tracker_blind')
                ->nullable()
                ->after('idle_tracker_stale_restarts');
        });
    }

    public function down(): void
    {
        Schema::table('agent_devices', function (Blueprint $table) {
            $table->dropColumn('idle_tracker_blind');
        });
    }
};
```

- [ ] **Step 4: Accept the key on the heartbeat**

In `hasHealthTelemetry()`, add `'idle_tracker_blind'` to the `hasAny([...])` array.

In `buildHealthUpdates()`, alongside the other reads, add:

```php
        // Tri-state: only write when the agent actually sent it. A missing key
        // must leave the stored value untouched (NULL for a device that has
        // never reported), never coerce to false.
        if ($request->has('idle_tracker_blind')) {
            $updates['idle_tracker_blind'] = $request->boolean('idle_tracker_blind');
        }
```

Place it next to the existing `window_titles_captured_recently` block, which uses the same `$request->has()` guard — follow that shape exactly.

- [ ] **Step 5: Run the test to verify it passes**

```bash
docker compose exec app php artisan test --filter=heartbeat_persists_idle_tracker_blind
```

Expected: PASS.

- [ ] **Step 6: Run the full agent test group for regressions**

```bash
docker compose exec app php artisan test --testsuite=Feature --filter=Agent
```

Expected: no new failures. Note any pre-existing failures in the commit body rather than fixing them here.

- [ ] **Step 7: Commit**

```bash
git add src/database/migrations/2026_07_28_000000_add_idle_tracker_blind_to_agent_devices_table.php \
        src/app/Http/Controllers/Api/Agent/AgentHeartbeatController.php \
        src/tests/Feature/Agent/AgentDeviceHealthEndpointTest.php
git commit -m "feat(agent-health): persist idle_tracker_blind, which the agent already sends

The agent has been reporting this on every heartbeat and the server had no
column for it, so the signal distinguishing a denied Input Monitoring grant
from a transient tracker restart was dropped on arrival.

Nullable with no default: never-reported must stay distinguishable from
reported-false. Test asserts both directions."
```

---

### Task 2: Extract the two existing detectors (no behaviour change)

Pure refactor. `deviceHealth()` is 204 lines with two inline detection blocks; Task 3 adds a third. Extract first so the third lands in a readable method.

**Files:**
- Modify: `src/app/Http/Controllers/Api/InternalStatsController.php:247-451`

**Interfaces:**
- Produces: `private function detectTelemetryDegraded(): array` and `private function detectIngestStalled(array $alreadyFlagged): array`, each returning a list of flagged-device arrays in the existing shape. Task 3 adds a sibling.

- [ ] **Step 1: Confirm the existing tests pass before touching anything**

```bash
docker compose exec app php artisan test --filter=AgentDeviceHealthEndpointTest
```

Expected: PASS. This is the regression baseline — a refactor with no behaviour change must leave it green. Record the test count.

- [ ] **Step 2: Extract both blocks**

Move lines 259-318 verbatim into `private function detectTelemetryDegraded(): array`, returning `$flagged`. Move lines 348-442 verbatim into `private function detectIngestStalled(array $alreadyFlagged): array`, returning only the rows it appends. Keep every comment — they encode incident history (Anamaria Strezoiu 2026-06-23, Cristian Dragota 2026-06-25) and must not be lost.

`deviceHealth()` becomes:

```php
    public function deviceHealth(Request $request): JsonResponse
    {
        if ($deny = $this->denyIfBadKey($request)) {
            return $deny;
        }

        $flagged = $this->detectTelemetryDegraded();
        $flagged = array_merge($flagged, $this->detectIngestStalled(array_column($flagged, 'device_id')));
        $flagged = $this->annotateWorkSchedule($flagged);

        return response()->json([
            'count' => count($flagged),
            'flagged' => $flagged,
            'generated_at' => now()->toIso8601String(),
        ]);
    }
```

- [ ] **Step 3: Run the tests to verify nothing changed**

```bash
docker compose exec app php artisan test --filter=AgentDeviceHealthEndpointTest
```

Expected: PASS, with the **same test count** as Step 1. A changed count means you altered behaviour.

- [ ] **Step 4: Commit**

```bash
git add src/app/Http/Controllers/Api/InternalStatsController.php
git commit -m "refactor(device-health): extract the two detectors from deviceHealth

No behaviour change. deviceHealth was 204 lines holding two inline detection
blocks and is about to gain a third; this makes it a pipeline instead.
Incident comments moved verbatim."
```

---

### Task 3: `window_titles_blind` detector

**Files:**
- Modify: `src/app/Http/Controllers/Api/InternalStatsController.php`
- Test: `src/tests/Feature/Agent/AgentDeviceHealthEndpointTest.php`

**Interfaces:**
- Consumes: `detectTelemetryDegraded()` / `detectIngestStalled()` from Task 2.
- Produces: `private function detectPermissionsDegraded(array $alreadyFlagged): array`, emitting rows with `'category' => 'permissions_degraded'`. Tasks 4 and 5 add reasons to this same method.

- [ ] **Step 1: Write the failing tests**

```php
    #[Test]
    public function device_health_flags_a_title_blind_device(): void
    {
        $device = $this->makeDevice([
            'status' => 'active',
            'window_titles_captured_recently' => false,
            'health_reported_at' => now(),
            'last_seen_at' => now(),
        ]);

        $flagged = $this->getJson('/api/internal/device-health', $this->internalApiHeaders())
            ->assertOk()->json('flagged');

        $row = collect($flagged)->firstWhere('device_id', $device->id);
        $this->assertNotNull($row, 'a title-blind device must be flagged');
        $this->assertSame('permissions_degraded', $row['category']);
        $this->assertSame('window_titles_blind', $row['reason']);
        $this->assertFalse($row['tracking_degraded'], 'must not route through tracking_degraded');
    }

    #[Test]
    public function device_health_does_not_flag_a_device_that_never_reported_titles(): void
    {
        $device = $this->makeDevice([
            'status' => 'active',
            'window_titles_captured_recently' => null,
            'health_reported_at' => null,
            'last_seen_at' => now(),
        ]);

        $flagged = $this->getJson('/api/internal/device-health', $this->internalApiHeaders())
            ->assertOk()->json('flagged');

        $this->assertNull(
            collect($flagged)->firstWhere('device_id', $device->id),
            'NULL means never reported, not broken — must not be flagged'
        );
    }
```

Reuse the existing helpers in this test class for device creation and internal-API auth headers; read the file first and match them.

- [ ] **Step 2: Run to verify both fail**

```bash
docker compose exec app php artisan test --filter=device_health_flags_a_title_blind_device
docker compose exec app php artisan test --filter=device_health_does_not_flag_a_device_that_never_reported_titles
```

Expected: the first FAILS (`a title-blind device must be flagged`); the second may already pass vacuously — that is fine and expected, it is a guard, not a proof.

- [ ] **Step 3: Implement the detector**

```php
    /**
     * Devices whose OS capture capability is broken, as opposed to devices whose
     * tracking is degraded. Deliberately emits tracking_degraded => false: routing
     * this through that flag would change what every existing consumer sees, and a
     * missing permission is a standing config fault, not "tracking is degraded
     * right now". This is why ALERT_ON_BLIND_WINDOW_TITLES stays off permanently.
     *
     * Each reason excludes never-reported devices with an explicit predicate.
     * NULL means "the agent has not told us", which is not a fault.
     */
    private function detectPermissionsDegraded(array $alreadyFlagged): array
    {
        $liveSince = now()->subMinutes(30);
        $out = [];

        // window_titles_blind — macOS Accessibility missing, Windows tracker
        // blind, or Linux X11 watcher dead. One symptom, three causes; M2's
        // explicit grant booleans will disambiguate.
        $titleRows = DB::table('agent_devices as d')
            ->leftJoin('users as u', 'u.id', '=', 'd.user_id')
            ->leftJoin('tenants as t', 't.id', '=', 'd.tenant_id')
            ->where('d.status', 'active')
            ->whereNull('d.deleted_at')
            ->where('d.window_titles_captured_recently', false)
            ->whereNotNull('d.window_titles_captured_recently')
            ->where('d.last_seen_at', '>=', $liveSince)
            ->select(
                'd.id', 'd.device_name', 'd.platform', 'd.agent_version', 'd.user_id',
                'd.tenant_id', 'd.timezone', 'd.last_seen_at', 'd.last_sync_at',
                'd.health_reported_at', 'u.email', 'u.name', 't.name as tenant_name',
            )
            ->limit(200)
            ->get();

        foreach ($titleRows as $r) {
            if (in_array((int) $r->id, $alreadyFlagged, true)) {
                continue;
            }
            $out[] = $this->permissionsRow($r, 'window_titles_blind');
        }

        return $out;
    }

    /** Shape a permissions_degraded row. Keeps the payload identical across reasons. */
    private function permissionsRow(object $r, string $reason): array
    {
        return [
            'device_id' => (int) $r->id,
            'device_name' => $r->device_name,
            'platform' => $r->platform,
            'agent_version' => $r->agent_version,
            'user_id' => $r->user_id,
            'email' => $r->email,
            'name' => $r->name,
            'tenant_id' => $r->tenant_id,
            'tenant_name' => $r->tenant_name,
            'device_timezone' => $r->timezone,
            'category' => 'permissions_degraded',
            'reason' => $reason,
            'last_seen_at' => $r->last_seen_at,
            'last_sync_at' => $r->last_sync_at ?? null,
            'tracking_degraded' => false,
            'tracking_degraded_since' => null,
            'idle_tracker_stale_restarts' => 0,
            'afk_event_age_seconds' => null,
            'window_event_age_seconds' => null,
            'consecutive_sync_failures' => 0,
            'idle_while_active_detections' => 0,
            'health_reported_at' => $r->health_reported_at ?? null,
        ];
    }
```

Wire it into `deviceHealth()` after the ingest detector:

```php
        $flagged = array_merge($flagged, $this->detectPermissionsDegraded(array_column($flagged, 'device_id')));
```

- [ ] **Step 4: Run both tests to verify they pass**

```bash
docker compose exec app php artisan test --filter=AgentDeviceHealthEndpointTest
```

Expected: PASS, including the pre-existing tests.

- [ ] **Step 5: Commit**

```bash
git add src/app/Http/Controllers/Api/InternalStatsController.php src/tests/Feature/Agent/AgentDeviceHealthEndpointTest.php
git commit -m "feat(device-health): surface title-blind devices as permissions_degraded

Emits tracking_degraded => false so no existing consumer of that flag changes
behaviour — the reason ALERT_ON_BLIND_WINDOW_TITLES is superseded rather than
flipped. NULL title capture means never-reported and is explicitly excluded."
```

---

### Task 4: `idle_tracker_blind` reason

**Files:**
- Modify: `src/app/Http/Controllers/Api/InternalStatsController.php` (`detectPermissionsDegraded()`)
- Test: `src/tests/Feature/Agent/AgentDeviceHealthEndpointTest.php`

**Interfaces:**
- Consumes: `agent_devices.idle_tracker_blind` (Task 1), `permissionsRow()` (Task 3).

- [ ] **Step 1: Write the failing test**

```php
    #[Test]
    public function device_health_flags_a_blind_idle_tracker(): void
    {
        $device = $this->makeDevice([
            'status' => 'active',
            'idle_tracker_blind' => true,
            'health_reported_at' => now(),
            'last_seen_at' => now(),
        ]);

        $row = collect(
            $this->getJson('/api/internal/device-health', $this->internalApiHeaders())
                ->assertOk()->json('flagged')
        )->firstWhere('device_id', $device->id);

        $this->assertNotNull($row);
        $this->assertSame('idle_tracker_blind', $row['reason']);
    }
```

- [ ] **Step 2: Run to verify it fails**

```bash
docker compose exec app php artisan test --filter=device_health_flags_a_blind_idle_tracker
```

Expected: FAIL — `Failed asserting that null is not null`.

- [ ] **Step 3: Add the query to `detectPermissionsDegraded()`**

Insert before `return $out;`, and add `$flaggedSoFar = array_merge($alreadyFlagged, array_column($out, 'device_id'));` above it so the reasons do not double-flag one device:

```php
        // idle_tracker_blind — the AFK tracker stayed stale across repeated
        // restarts, which the agent reports specifically because a restart
        // cannot fix it: a denied Input Monitoring grant. NULL = never reported.
        $flaggedSoFar = array_merge($alreadyFlagged, array_column($out, 'device_id'));

        $blindRows = DB::table('agent_devices as d')
            ->leftJoin('users as u', 'u.id', '=', 'd.user_id')
            ->leftJoin('tenants as t', 't.id', '=', 'd.tenant_id')
            ->where('d.status', 'active')
            ->whereNull('d.deleted_at')
            ->where('d.idle_tracker_blind', true)
            ->whereNotNull('d.idle_tracker_blind')
            ->where('d.last_seen_at', '>=', $liveSince)
            ->select(
                'd.id', 'd.device_name', 'd.platform', 'd.agent_version', 'd.user_id',
                'd.tenant_id', 'd.timezone', 'd.last_seen_at', 'd.last_sync_at',
                'd.health_reported_at', 'u.email', 'u.name', 't.name as tenant_name',
            )
            ->limit(200)
            ->get();

        foreach ($blindRows as $r) {
            if (in_array((int) $r->id, $flaggedSoFar, true)) {
                continue;
            }
            $out[] = $this->permissionsRow($r, 'idle_tracker_blind');
        }
```

- [ ] **Step 4: Run to verify it passes**

```bash
docker compose exec app php artisan test --filter=AgentDeviceHealthEndpointTest
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/app/Http/Controllers/Api/InternalStatsController.php src/tests/Feature/Agent/AgentDeviceHealthEndpointTest.php
git commit -m "feat(device-health): surface a blind idle tracker as permissions_degraded

Consumes the column added one commit earlier. The agent sets this only after
the tracker has stayed stale across repeated restarts, which is its signal
that a restart cannot fix it."
```

---

### Task 5: `input_counters_dead` detector

The derived reason, and the only failure class with no agent-side signal at all. This is what catches the two Windows devices logging 8-9h/day with zero input.

**Files:**
- Modify: `src/app/Http/Controllers/Api/InternalStatsController.php` (`detectPermissionsDegraded()`)
- Test: `src/tests/Feature/Agent/AgentDeviceHealthEndpointTest.php`

**Interfaces:**
- Consumes: `agent_activity_aggregates` (`device_id`, `work_date`, `active_seconds`, `keystrokes_count`, `clicks_count`, `scrolls_count`), `permissionsRow()`.

- [ ] **Step 1: Write the failing tests — including the two adversarial cases**

```php
    #[Test]
    public function device_health_flags_a_device_with_dead_input_counters(): void
    {
        $device = $this->makeDevice(['status' => 'active', 'last_seen_at' => now()]);
        $this->seedAggregate($device->id, today(), activeSeconds: 20000, keys: 0, clicks: 0, scrolls: 0);

        $row = collect(
            $this->getJson('/api/internal/device-health', $this->internalApiHeaders())
                ->assertOk()->json('flagged')
        )->firstWhere('device_id', $device->id);

        $this->assertNotNull($row, '5.5h active with zero input of any kind must flag');
        $this->assertSame('input_counters_dead', $row['reason']);
    }

    #[Test]
    public function device_health_does_not_flag_when_scrolls_are_present(): void
    {
        // THE adversarial case. Zero keystrokes and zero clicks but real scrolls
        // is a PARTIAL failure, not a dead tracker, and must not be flagged here.
        $device = $this->makeDevice(['status' => 'active', 'last_seen_at' => now()]);
        $this->seedAggregate($device->id, today(), activeSeconds: 20000, keys: 0, clicks: 0, scrolls: 4200);

        $flagged = $this->getJson('/api/internal/device-health', $this->internalApiHeaders())
            ->assertOk()->json('flagged');

        $this->assertNull(collect($flagged)->firstWhere('device_id', $device->id));
    }

    #[Test]
    public function device_health_does_not_flag_a_device_with_no_aggregate_rows(): void
    {
        // A read that returned nothing is not proof that there is nothing.
        $device = $this->makeDevice(['status' => 'active', 'last_seen_at' => now()]);

        $flagged = $this->getJson('/api/internal/device-health', $this->internalApiHeaders())
            ->assertOk()->json('flagged');

        $this->assertNull(collect($flagged)->firstWhere('device_id', $device->id));
    }

    #[Test]
    public function device_health_does_not_flag_below_the_active_floor(): void
    {
        $device = $this->makeDevice(['status' => 'active', 'last_seen_at' => now()]);
        $this->seedAggregate($device->id, today(), activeSeconds: 3600, keys: 0, clicks: 0, scrolls: 0);

        $flagged = $this->getJson('/api/internal/device-health', $this->internalApiHeaders())
            ->assertOk()->json('flagged');

        $this->assertNull(collect($flagged)->firstWhere('device_id', $device->id), '1h is under the 4h floor');
    }
```

Add the seeding helper to the test class:

```php
    private function seedAggregate(
        int $deviceId,
        \Illuminate\Support\Carbon $workDate,
        int $activeSeconds,
        int $keys,
        int $clicks,
        int $scrolls,
    ): void {
        DB::table('agent_activity_aggregates')->insert([
            'device_id' => $deviceId,
            'work_date' => $workDate->toDateString(),
            'app_name' => 'EXCEL.EXE',
            'active_seconds' => $activeSeconds,
            'total_seconds' => $activeSeconds,
            'keystrokes_count' => $keys,
            'clicks_count' => $clicks,
            'scrolls_count' => $scrolls,
            'created_at' => now(),
            'updated_at' => now(),
        ]);
    }
```

If `agent_activity_aggregates` has NOT NULL columns beyond these, read the migration and add them — do not guess.

- [ ] **Step 2: Run to verify the first fails and the guards pass**

```bash
docker compose exec app php artisan test --filter=device_health_flags_a_device_with_dead_input_counters
```

Expected: FAIL. Then run the three guards; they should pass vacuously, which is correct — they exist to stay green after Step 3.

- [ ] **Step 3: Implement the detector**

Append to `detectPermissionsDegraded()`, before `return $out;`:

```php
        // input_counters_dead — window and AFK capture are healthy but the input
        // counters are flat zero. No agent-side signal exists for this, so it is
        // derived. Scrolls are the discriminator: a quiet user still scrolls
        // constantly (a healthy device logged 58,631 scrolls in Chrome in one
        // day), so all three counters at zero across 4h+ of active time is a
        // tracker that is not reporting, not a person working quietly. Summing
        // all three makes this STRICTLY harder to satisfy than keystrokes+clicks
        // alone — scrolls-but-no-keystrokes is a different, partial failure and
        // must not land here.
        //
        // HAVING over grouped rows means this fires only when rows EXIST and sum
        // to zero. "No rows at all" is not "no input" and must never flag.
        $flaggedSoFar = array_merge($alreadyFlagged, array_column($out, 'device_id'));

        $deadInputDeviceIds = DB::table('agent_activity_aggregates')
            ->where('work_date', '>=', now()->subDay()->toDateString())
            ->groupBy('device_id', 'work_date')
            ->havingRaw('SUM(active_seconds) >= ?', [4 * 3600])
            ->havingRaw('SUM(keystrokes_count) + SUM(clicks_count) + SUM(scrolls_count) = 0')
            ->pluck('device_id')
            ->unique()
            ->all();

        if ($deadInputDeviceIds !== []) {
            $inputRows = DB::table('agent_devices as d')
                ->leftJoin('users as u', 'u.id', '=', 'd.user_id')
                ->leftJoin('tenants as t', 't.id', '=', 'd.tenant_id')
                ->where('d.status', 'active')
                ->whereNull('d.deleted_at')
                ->whereIn('d.id', $deadInputDeviceIds)
                ->select(
                    'd.id', 'd.device_name', 'd.platform', 'd.agent_version', 'd.user_id',
                    'd.tenant_id', 'd.timezone', 'd.last_seen_at', 'd.last_sync_at',
                    'd.health_reported_at', 'u.email', 'u.name', 't.name as tenant_name',
                )
                ->limit(200)
                ->get();

            foreach ($inputRows as $r) {
                if (in_array((int) $r->id, $flaggedSoFar, true)) {
                    continue;
                }
                $out[] = $this->permissionsRow($r, 'input_counters_dead');
            }
        }
```

Note this detector deliberately does NOT gate on `last_seen_at` — a device whose input died and then went offline for the evening is still broken and still worth reporting the next morning.

- [ ] **Step 4: Run the whole test file**

```bash
docker compose exec app php artisan test --filter=AgentDeviceHealthEndpointTest
```

Expected: PASS, all four new tests plus every pre-existing one.

- [ ] **Step 5: Commit**

```bash
git add src/app/Http/Controllers/Api/InternalStatsController.php src/tests/Feature/Agent/AgentDeviceHealthEndpointTest.php
git commit -m "feat(device-health): derive input_counters_dead, which had no signal at all

Window and AFK capture healthy, input counters flat zero. Found on two Windows
devices logging 8-9h/day for 10 and 7 consecutive days with zero keystrokes,
clicks AND scrolls — invisible to every existing signal.

Scrolls are the discriminator and are included in the sum, which makes the
predicate strictly harder to satisfy: scrolls-without-keystrokes is a partial
failure and is explicitly tested as a non-flagging case. HAVING over grouped
rows means no-rows can never masquerade as no-input."
```

---

### Task 6: Bot — recognise the three reasons

**Files:**
- Modify: `src/monitor/betterflow-device-health.ts` (category union `:53`, `reasonText()` `:223-282`, `severityFor()` `:285-297`)
- Test: `src/__tests__/betterflow-device-health.test.ts`

**Interfaces:**
- Consumes: `category: "permissions_degraded"` and reasons `window_titles_blind` / `idle_tracker_blind` / `input_counters_dead` from Tasks 3-5.

- [ ] **Step 1: Write the failing tests**

```ts
  it("describes every permissions_degraded reason without falling through to default", () => {
    for (const reason of ["window_titles_blind", "idle_tracker_blind", "input_counters_dead"]) {
      const text = reasonText({ ...baseDevice, category: "permissions_degraded", reason } as FlaggedDevice);
      expect(text).not.toContain("no description for that reason yet");
      expect(text.length).toBeGreaterThan(40);
    }
  });

  it("never escalates a permissions fault to critical, however long it persists", () => {
    const severity = severityFor({
      ...baseDevice,
      category: "permissions_degraded",
      reason: "window_titles_blind",
      tracking_degraded_since: new Date(Date.now() - 72 * 3600 * 1000).toISOString(),
    } as FlaggedDevice);
    expect(severity).toBe("warning");
  });
```

Read the existing test file first: reuse its `baseDevice` fixture and match how it imports the internals. If `reasonText` / `severityFor` are not exported, export them for test only, following whatever pattern the file already uses for the other internals it tests.

- [ ] **Step 2: Run to verify they fail**

```bash
cd /Users/brad/Code2/betterqa-bot && npx vitest run src/__tests__/betterflow-device-health.test.ts
```

Expected: FAIL — the first on the default-branch text, the second on `critical`.

- [ ] **Step 3: Widen the category union**

```ts
  category: "telemetry_degraded" | "heartbeat_stale" | "ingest_stalled" | "permissions_degraded";
```

- [ ] **Step 4: Add the three `reasonText()` cases**

Insert before `default:`. Each names the exact remedial action, because there is no UI to click through to:

```ts
    case "window_titles_blind":
      return `window titles are empty — the agent is recording app names and durations but no titles, so meeting detection and categorisation are degraded. On macOS this is a missing Accessibility grant (System Settings > Privacy & Security > Accessibility > enable BetterFlow); on Windows the bf-window-tracker is blind; on Linux the X11 watcher is dead`;
    case "idle_tracker_blind":
      return `idle tracker is blind — it has stayed stale across repeated restarts, which the agent reports specifically because restarting cannot fix it. Almost always a denied Input Monitoring grant (System Settings > Privacy & Security > Input Monitoring > enable BetterFlow). Tracking continues via the OS idle clock meanwhile, so time is not lost`;
    case "input_counters_dead":
      return `input counters are dead — window and idle capture are healthy but keystrokes, clicks AND scrolls are all zero across 4h+ of active time. That is a tracker not reporting, not a quiet user. Fraud-detection input is empty for this device. Check AV/endpoint protection blocking bf-idle-tracker, then reinstall the agent`;
```

- [ ] **Step 5: Pin the severity**

In `severityFor()`, immediately after the existing `window_ingest_stalled` clause:

```ts
  // A missing permission is a standing config fault that only a human clears —
  // it will "persist" indefinitely by construction, so the age-based escalation
  // below is meaningless for it. critical also bypasses Slack coalescing and
  // fires the Telegram fallback, which is wrong for a config fault.
  if (d.category === "permissions_degraded") {
    return "warning";
  }
```

- [ ] **Step 6: Run to verify they pass**

```bash
npx vitest run src/__tests__/betterflow-device-health.test.ts
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/monitor/betterflow-device-health.ts src/__tests__/betterflow-device-health.test.ts
git commit -m "feat(device-health): describe the three permissions_degraded reasons

Each line names the exact remedial action because there is deliberately no UI
to click through to. Severity pinned to warning: a config fault persists by
construction, so age-based escalation is meaningless, and critical would bypass
Slack coalescing and fire the Telegram fallback."
```

---

### Task 7: Bot — per-reason cooldown

`COOLDOWN_SECONDS` is one module-level constant at 6h shared by every reason. A permission fault is cleared only by a human, so 6h means four pings per device per day forever.

**Files:**
- Modify: `src/monitor/betterflow-device-health.ts:84`, `filterByCooldown()` `:364-392`
- Test: `src/__tests__/betterflow-device-health.test.ts`

- [ ] **Step 1: Write the failing test**

```ts
  it("gives permission faults a much longer cooldown than transient faults", () => {
    expect(cooldownSecondsFor("window_titles_blind")).toBe(72 * 3600);
    expect(cooldownSecondsFor("input_counters_dead")).toBe(72 * 3600);
    expect(cooldownSecondsFor("idle_tracker_stale")).toBe(6 * 3600);
    expect(cooldownSecondsFor("some_reason_invented_later")).toBe(6 * 3600);
  });
```

- [ ] **Step 2: Run to verify it fails**

```bash
npx vitest run src/__tests__/betterflow-device-health.test.ts
```

Expected: FAIL — `cooldownSecondsFor is not defined`.

- [ ] **Step 3: Implement**

Replace the bare constant with a default plus an override map, keeping the old name as the default so nothing else changes:

```ts
const COOLDOWN_SECONDS = 6 * 3600; // default: one ping per device+reason per 6h
// A permission/capture fault is a STANDING condition that only a human clears,
// so the default 6h would ping four times a day per device indefinitely and
// train everyone to ignore the channel — the exact failure the server-side
// ALERT_ON_BLIND_WINDOW_TITLES docblock warns about.
const PERMISSION_COOLDOWN_SECONDS = 72 * 3600;
const COOLDOWN_OVERRIDES: Record<string, number> = {
  window_titles_blind: PERMISSION_COOLDOWN_SECONDS,
  idle_tracker_blind: PERMISSION_COOLDOWN_SECONDS,
  input_counters_dead: PERMISSION_COOLDOWN_SECONDS,
};

/** Cooldown for a reason. Unknown reasons get the default, never zero. */
export function cooldownSecondsFor(reason: string): number {
  return COOLDOWN_OVERRIDES[reason] ?? COOLDOWN_SECONDS;
}
```

In `filterByCooldown()`, the comparison currently uses the single constant against every row. Change it to evaluate per row via `cooldownSecondsFor(d.reason)`. Read the existing implementation and keep its fail-open behaviour on DB error exactly as-is — that is deliberate.

- [ ] **Step 4: Run to verify it passes**

```bash
npx vitest run src/__tests__/betterflow-device-health.test.ts
```

Expected: PASS.

- [ ] **Step 5: Run the full bot suite for regressions**

```bash
npx vitest run
```

Expected: no new failures. The cooldown change touches a shared path, so read the summary — do not trust the exit code alone.

- [ ] **Step 6: Commit**

```bash
git add src/monitor/betterflow-device-health.ts src/__tests__/betterflow-device-health.test.ts
git commit -m "feat(device-health): per-reason alert cooldown, 72h for permission faults

A single 6h constant was shared by every reason. A permission fault is cleared
only by a human, so it would ping four times a day per device forever. Unknown
reasons keep the 6h default."
```

---

### Task 8: Contract test — server reasons must not outrun the bot

The bot's `default:` branch is honest, but it is the drift point. This is the guard.

**Files:**
- Test: `src/__tests__/betterflow-device-health.test.ts`

- [ ] **Step 1: Write the test**

```ts
  // The server invents reasons faster than this file learns them. The default
  // branch keeps that honest rather than fabricating, but this asserts we
  // noticed. Update BOTH lists when the server gains a reason.
  const SERVER_PERMISSION_REASONS = [
    "window_titles_blind",
    "idle_tracker_blind",
    "input_counters_dead",
  ];

  it("has a description for every permissions reason the server can emit", () => {
    const undescribed = SERVER_PERMISSION_REASONS.filter((reason) =>
      reasonText({ ...baseDevice, category: "permissions_degraded", reason } as FlaggedDevice)
        .includes("no description for that reason yet")
    );
    expect(undescribed).toEqual([]);
  });
```

- [ ] **Step 2: Verify the guard is witnessed**

Per `rules/test-fixture-discipline.md` Phantom 5, a guard nobody has watched fail does not exist. Temporarily delete the `case "input_counters_dead":` branch, re-run, and confirm the suite goes RED naming that reason. Then restore it and confirm GREEN.

```bash
npx vitest run src/__tests__/betterflow-device-health.test.ts
```

Expected: RED with the branch removed, GREEN with it restored. Do not skip this step.

- [ ] **Step 3: Commit**

```bash
git add src/__tests__/betterflow-device-health.test.ts
git commit -m "test(device-health): guard that every server permissions reason has bot text

Verified by deletion: removing the input_counters_dead case turns the suite red
naming that reason, then green when restored."
```

---

### Task 9: Verify against the real fleet before merging

Acceptance is checkable against reality, which is the strongest signal available here.

**Files:** none — verification only.

- [ ] **Step 1: Apply the migration to prod BEFORE merging**

Railway auto-deploys `main`, so merging is deploying. Confirm the column does not exist, apply, confirm it does. Use the DB service's public proxy and the full `psql` path per `rules/coding-standards.md` §7 — never a bare `psql`.

- [ ] **Step 2: Run every non-deploy workflow's steps locally, in both repos**

```bash
ls /Users/brad/Code2/internal-tool2/.github/workflows/
ls /Users/brad/Code2/betterqa-bot/.github/workflows/
```

Do not look for `ci.yml` by name. Read each workflow's step list and run every step, honouring any `working-directory:`. Say explicitly which steps you ran and which you skipped and why.

- [ ] **Step 3: Hit the endpoint against prod data and check the expected output**

The endpoint must return, in the `permissions_degraded` category:
- exactly **3** `window_titles_blind` rows — devices 17, 6 and 22 (Sergiu Olpretean, Tudor Brad, Timea Eniko Schvartz)
- exactly **2** `input_counters_dead` rows — devices 24 and 34 (Claudia Malau, Youssef Abdelmeged)

**14 rows, or 0 rows, means the query is wrong — stop and diagnose, do not merge.** Cross-check the title figure independently with `betterflow_agent_devices` using `title_capture="broken"`.

- [ ] **Step 4: Confirm no existing consumer changed**

The `telemetry_degraded` and `ingest_stalled` populations must be identical to before the change. Diff the counts against a pre-change call.

- [ ] **Step 5: Open the PRs and STOP**

internal-tool2 is PR-only, no auto-merge — Martin is active in it. Write the verification evidence from Steps 3 and 4 into the PR description. betterqa-bot follows the ecosystem rules, but ship the server side first: the bot's `default:` branch handles an unknown reason honestly, so server-ahead-of-bot is safe and bot-ahead-of-server is a no-op.

---

## Self-Review

**Spec coverage:** `window_titles_blind` → Task 3. `idle_tracker_blind` → Tasks 1+4. `input_counters_dead` → Task 5. Fail-closed rules → Tasks 3, 5 guards. Reused alert pipeline → Tasks 6, 7. Self-sufficient Slack line → Task 6 Step 4. Per-reason cooldown → Task 7. Contract test → Task 8. Acceptance 3+2 → Task 9. Do-not-flip-the-constant → Global Constraints + Task 3 comment. No grouping, no upgrade nag, no UI → not implemented, by design.

**Not covered here, by design:** M2 (agent grant reporting) is a separate plan. The class-1 backlog reconciliation and the missing ack/snooze are spec open questions, not tasks.

**Type consistency:** `permissionsRow()` shapes every row identically across Tasks 3-5. `cooldownSecondsFor()` is defined in Task 7 and used only there. The category string `permissions_degraded` and the three reason literals are identical in the PHP emitters (Tasks 3-5), the TS union (Task 6), and the contract test (Task 8).
