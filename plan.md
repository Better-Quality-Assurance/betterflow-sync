# BetterFlow Sync — Improvement Plan

## Critical (data loss / tracking accuracy)

- [x] **1. No event deduplication** — if sync restarts mid-batch, same events get sent twice. Fix: use AW event ID + bucket_id as composite dedup key; verify server upsert in response.
- [x] **2. No DST transition handling** — clock jump can double-count or skip an hour. Fix: detect system time jump via delta check; reset hourly cache on large jumps.
- [x] **3. No clock skew detection** — AW time vs system time drift breaks checkpoints. Fix: compare server time from heartbeat response; warn if > 5min skew.
- [x] **4. No exponential backoff on queue retry** — failed queue retries hammer the server every 60s. Fix: implement per-event retry delay with exponential backoff + jitter.
- [x] **5. No SQLite corruption recovery** — corrupted queue.db makes app unusable. Fix: enable WAL mode; add integrity check on startup; auto-reset on corruption.
- [x] **6. No session cleanup on logout** — `end_session()` never called before logout, orphaned sessions on server. Fix: call `end_session()` in `_on_logout` before stopping coordinator.
- [x] **7. No AW crash recovery** — if ActivityWatch dies, tray shows ERROR indefinitely until manual restart. Fix: AWManager should auto-restart the full suite when `is_running()` returns False.
- [x] **8. No partial batch failure handling** — server accepts 8/10 events, client retries all 10 = duplicates. Fix: server returns accepted event IDs; client only re-queues failures.
- [x] **9. No version compatibility check** — old client + new server API = silent data loss. Fix: heartbeat response includes `minimum_agent_version`; app warns/blocks if outdated.
- [x] **10. No event timestamp validation** — future timestamp advances checkpoint, skipping current events. Fix: clamp event timestamps to now(); warn on large skew.

## Important (reliability / UX)

- [x] **11. No auto-update mechanism** — users stuck on old versions forever. Fix: check for updates via GitHub releases API; prompt user to download.
- [x] **12. No screen lock detection** — locked screen doesn't trigger AFK on some systems. Fix: add OS-level lock/unlock detection (macOS distributed notifications, Windows WTS events).
- [x] **13. No "forgot to clock out" warning** — if app crashes, session stays active on server. Fix: on startup, check if last session is still active; warn user.
- [x] **14. No diagnostics panel** — user can't check AW health, API connectivity, token validity, queue status. Fix: add "Diagnostics" submenu in tray with live status checks.
- [x] **15. No log export** — users must manually find log files to share for debugging. Fix: add "Export Logs" in Preferences; zip logs + redacted config.
- [x] **16. No macOS Accessibility permission request** in setup wizard — first sync silently fails. Fix: check/request permission in wizard step.
- [x] **17. No daily summary in tray** — just hours, no top apps or productivity breakdown. Fix: hours today shown in tray header.
- [x] **18. Queue age limit missing** — stale events sit in queue for weeks, never expire. Fix: auto-expire queue items older than 30 days.

## Nice-to-have (polish / advanced)

- [x] **19. Screenshot capability** (anti-fraud, compliance). Fix: togglable screenshots at configurable intervals; encrypted upload.
- [x] **20. Client-side app categorization database**. Fix: maintain local category DB; sync from server; allow overrides.
- [x] **21. Multi-monitor / virtual desktop awareness**. Fix: requires AW enhancement; agent could tag current desktop/space.
- [x] **22. Weekly/monthly trends in tray**. Fix: cache weekly summary; show in tray submenu.
- [x] **23. Beta/canary update channel**. Fix: add update channel setting; check beta endpoint.
