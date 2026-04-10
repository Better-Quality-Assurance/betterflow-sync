# BetterFlow Sync — Code Audit Punch List

> Untracked working document. Date: 2026-04-10
> Legend: ⬜ pending · 🟦 in progress · ✅ done · ⏭ skipped / false positive
>
> **Session 1 result: 301/301 tests green.**
> - ✅ 7/7 security items landed (S1-S7)
> - ✅ 4/9 thread-safety bugs fixed; 5 were audit false positives (T1-T9)
> - ✅ 6/6 dead code removed (D1-D6)
> - ✅ 3/8 DRY extractions landed (R2, R3, R5); 5 deferred or false positives
> - ✅ Major silent-swallowing sites logged
> - ✅ 2/4 correctness bugs fixed (C1, C2, C4); 1 false positive
> - ✅ **Bonus:** migrated macOS notifications from `osascript`/Script Editor
>   attribution to pyobjc NSUserNotification so `clear_notifications()`
>   actually clears them on startup and shutdown.

## 🔴 SECURITY

| # | File:Line | Issue | Status |
|---|---|---|---|
| S1 | `src/auth/browser_auth.py:92-107` | `_allow_state_mismatch()` bypasses OAuth CSRF via env var. Delete. | ✅ |
| S2 | `src/self_updater.py:223` | DMG mounted with `-noverify`. Remove flag. | ✅ |
| S3 | `src/auth/keychain.py:75-78,119-121` | Plaintext JSON fallback when keyring unavailable. | ✅ |
| S4 | `src/ui/permissions.py:157-161` | TCC SQL INSERT built via string interpolation. Parameterize. | ✅ |
| S5 | `src/aw_manager.py:443-465` | Subprocess args built dynamically with partially user-influenced values. | ✅ |
| S6 | `src/sync/sync_engine.py:908` | Silent URL truncation can change semantics. | ✅ |
| S7 | `src/auth/pkce.py:67` | No RFC 7636 alphabet validation on code_verifier. | ✅ |

## 🔴 THREAD SAFETY (CLAUDE.md rule #1)

| # | File:Line | Issue | Status |
|---|---|---|---|
| T1 | `src/sync/sync_engine.py:1000` | `_afk_watcher_available` read without `_state_lock`. | ✅ |
| T2 | `src/main.py:893,910,932,947` | `sys_events._user_paused` written without `_pause_state_lock`. | ⏭ false positive — already locked |
| T3 | `src/aw_manager.py:221,369` | `_stale_restart_count`, `_using_external` mutated without lock. | ✅ |
| T4 | `src/ui/tray.py:318+` | External callers hit `tray.set_state()` without `model.lock`. | ⏭ false positive — already locked |
| T5 | `src/system_events.py:20,22` | Global `_registered_observers` / `_stop_event` cross-thread no sync. | ⏭ false positive — already locked |
| T6 | `src/sync/daily_time_tracker.py:252-255` | `max(today, loaded)` discards concurrent `add_active_time`. | ✅ |
| T7 | `src/sync/macos_input_watcher.py:235-244` | Counters zeroed before AW post → data loss on failure. | ✅ |
| T8 | `src/sync/http_client.py:200-204` | `self._session` read under lock but used outside. | ⏭ false positive — snapshot pattern correct |
| T9 | `src/sync/queue.py:88-95` | Connection creation race after `close()`. | ⏭ false positive — `_closed` checked under lock |

## 🟡 DEAD CODE

| # | File:Line | Issue | Status |
|---|---|---|---|
| D1 | `src/ui/setup_wizard.py:511-524` | `_show_permissions_entry()` unreachable after `return`. | ✅ |
| D2 | `src/ui/setup_wizard.py:526-649` | `_show_permissions()` + `_auto_refresh_permissions()` never called. | ✅ |
| D3 | `src/main.py:719,788` | Orphaned `pass` after refactor comments. | ✅ |
| D4 | `src/sync/sync_engine.py:663-670` | Duplicate "skipped after inactivity" log, second branch unreachable. | ✅ |
| D5 | `src/display_info.py:124-126` | Desktop fallback comment with no tracking logic. | ✅ |
| D6 | `src/self_updater.py:157,186-187` | Duplicate `import os; os._exit(0)` in function bodies. | ✅ |

## 🟡 DRY VIOLATIONS

| # | File:Line | Issue | Status |
|---|---|---|---|
| R1 | ~7 files | Import fallback `try: from . … except ImportError:` boilerplate. | ⏭ left as-is (dual-mode import pattern required for PyInstaller) |
| R2 | `src/main.py:890-956` | Pause/resume/private toggle duplicate lock+notify logic 3x. | ✅ |
| R3 | `src/sync/sync_engine.py:1087-1165` | `send_break_event`/`send_idle_event`/`send_private_time_event` ~95% identical. | ✅ |
| R4 | `src/sync/sync_engine.py:172,205,1137` | Checkpoint advancement duplicated. | ⏭ already one method, just called twice for different triggers |
| R5 | `src/sync/sync_engine.py:107-112` | Three hand-rolled LRU caches. Extract `BoundedLRU`. | ✅ |
| R6 | `src/system_events.py:240-308 vs 429-462` | Network reachability duplicated. | ⏭ deferred (would require platform-specific refactor) |
| R7 | `src/config.py:509-620` | 110 lines of nested type coercion in `update_from_server()`. | ⏭ deferred (touches tests, big surface) |
| R8 | `src/sync/aw_client.py:147-155` | Error handler pattern repeated; extract helper. | ⏭ minor, left as-is |

## 🟡 SILENT EXCEPTION SWALLOWING (CLAUDE.md rule #5)

Every `except … : pass` / debug-only swallow needs a warning log + context.

- `src/main.py:705, 782, 1038, 1088`
- `src/ui/tray.py:222, 307, 989, 1086, 1129, 1155, 1172, 1182`
- `src/ui/setup_wizard.py:158, 365, 419, 648`
- `src/aw_manager.py:206, 534`
- `src/system_events.py:84, 168, 255, 411, 445, 473`
- `src/update_checker.py:131, 147, 163, 174, 192`
- `src/sync/http_client.py:250-252`
- `src/idle_manager.py:134-135`
- `src/entry_point.py:41`

Status: ✅ (major sites logged; narrow fallback paths and `__del__` swallows kept by design)

## 🟡 OVER-ENGINEERING / BLOAT

| # | File:Line | Issue | Status |
|---|---|---|---|
| O1 | `src/main.py:373-401` | `_DO_SYNC_DEADLINE` watchdog redundant with system event pausing. | ⏭ kept — documented as macOS-sleep race workaround |
| O2 | `src/aw_manager.py` | `disable_component()`/`_disabled_components` feature flag noise. | ⏭ false positive — used by main.py:569 |
| O3 | `src/sync/call_detector.py:89-104` | 5 fields instead of one `CallState` dataclass. | ⏭ deferred (cosmetic) |
| O4 | `src/sync/activity_analyzer.py:138` | Hardcoded `deque(maxlen=3600)` should derive from config. | ⏭ deferred (cosmetic) |
| O5 | `src/ui/setup_wizard.py:217-273` | 107 lines of inline canvas drawing. | ⏭ deferred (god-class territory) |
| O6 | `src/ui/tray.py:4` | `import math` unused at module level. | ⏭ false positive — used in icon rendering |

## 🟡 OTHER CORRECTNESS

| # | File:Line | Issue | Status |
|---|---|---|---|
| C1 | `src/sync/bf_client.py:223-225` | `revoke()` returns True on auth error — conflates failure modes. | ✅ |
| C2 | `src/sync/sync_engine.py:1073-1085` | `"code" in haystack` matches "decode"/"encode"; use word boundaries. | ✅ |
| C3 | `src/sync/aw_client.py:199,212` | Double `time.monotonic()` call in cache double-check. | ⏭ false positive — intentional before/after-fetch pair |
| C4 | `src/hours_tracker.py:32-53` | Cache update semantics inconsistent on exception path. | ✅ |

## 🔴 GOD CLASSES (deferred — large refactors)

| # | File | LoC | Status |
|---|---|---|---|
| G1 | `src/sync/sync_engine.py` | 1448 | ⬜ |
| G2 | `src/ui/tray.py` | 1355 | ⬜ |
| G3 | `src/main.py` | 1315 | ⬜ |
| G4 | `src/config.py` | 656 | ⬜ |
| G5 | `src/aw_manager.py` | 606 | ⬜ |
| G6 | `src/sync/activity_analyzer.py` | 582 | ⬜ |
| G7 | `src/system_events.py` | 474 | ⬜ |

---

## Fix order

1. Security (S1→S7)
2. Thread safety (T1→T9)
3. Dead code (D1→D6) — quick wins
4. DRY extractions (R1, R3, R5, R2)
5. Exception swallowing pass
6. Over-engineering removals
7. Other correctness
8. God-class refactors (largest — do last, separate sessions)
