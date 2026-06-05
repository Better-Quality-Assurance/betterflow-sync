# SOLID + Gaps Audit — betterflow-sync

**Date:** 2026-06-05
**Scope:** Notarized Developer ID ship pipeline (sign-mac.sh, notarize-mac.py, Makefile ship targets, self_updater team-ID pin) and privacy gate rewrite (setup_wizard.py two-column DO/DON'T disclosure, tray.py Privacy Policy menu entry).
**Verdict:** Converged after 6 rounds
**Total fixes applied:** 11

## Summary

Six rounds of senior review hardened the Developer ID notarized ship pipeline and the rewritten Input Monitoring permission gate. All critical security paths (EXPECTED_TEAM_ID pin, inside-out codesign ordering, version-downgrade rejection, HTTPS-only update enforcement, Tk timer lifecycle) are correct and now exercised by tests (280 passing, up from 260). Round 6 found **zero important or critical issues** — only cosmetic and documentation gaps remain.

## Rounds

### Round 1 — 0 critical, 3 important

**Summary:** Notarized ship pipeline well-executed; inside-out codesign avoids `--deep` unreliability, team-ID pin correctly rejects ad-hoc/unsigned updates, disclosure gate legally defensible. Three pre-rollout issues found.

**Important findings:**
- **Missing test: `_codesign_verify` returns False path never exercised** (`tests/test_self_updater_team_pin.py`) — all 5 tests mocked True; regression that disabled crypto verification would pass silently. **Fixed:** added 6th test.
- **`sign-mac.sh` entitlements path is relative with no guard** (`scripts/sign-mac.sh:15`) — failure produces opaque error if run from outside repo root. **Fixed:** added existence check.
- **`_handle_open_privacy` lazy-imports `PRIVACY_POLICY_URL` inside method** (`src/ui/tray.py:849`) — import failure surfaces as silent runtime error on menu click. **Fixed:** moved to module-level with `contextlib.suppress`.

**Fix commit:** [`04be1a3`](../../commits/04be1a3) — 3/3 applied, 260 tests pass.

### Round 2 — 0 critical, 2 important

**Summary:** Codesign team-ID pin structurally sound. Two pre-rollout gaps: `.PHONY` missing new ship targets, and version-downgrade branch had no tests.

**Important findings:**
- **`.PHONY` missing ship/notarize-mac/staple-mac/_dmg-only/dmg** (`Makefile:3`) — a stale same-named file in repo root silently skips the entire release pipeline. **Fixed:** extended `.PHONY`.
- **No tests for version-downgrade rejection in `_verify_codesign`** (`tests/test_self_updater_team_pin.py`) — the `current_app_path` branch (signed→unsigned, version downgrade) was untested; this is the guard against compromised update servers serving older signed builds. **Fixed:** added 3 parametrized cases.

**Fix commit:** [`f2fa95f`](../../commits/f2fa95f) — 2/2 applied, 263 tests pass.

### Round 3 — 0 critical, 2 important

**Summary:** All prior fixes confirmed. Two issues: ISP violation forcing unused Config on gate callers, and zero test coverage on the rewritten permission gate logic.

**Important findings:**
- **`run_permission_gate` forces unused `Config` on callers** (`src/ui/setup_wizard.py:778`) — gate-only mode never reads `_config`. **Fixed:** made `config` Optional, added None guard in `_finish()`.
- **Zero test coverage on rewritten permission gate** (setup_wizard.py:145–620) — the 3 `_gate_result` outcomes (`granted`/`restart`/`quit`) and timer cancellation paths were untested. **Fixed:** created `tests/test_setup_wizard_gate.py` with 14 tests.

**Fix commit:** [`b552d2d`](../../commits/b552d2d) — 2/2 applied.

### Round 4 — 0 critical, 3 important

**Summary:** Pipeline structurally sound. Three issues block clean onboarding/CI.

**Important findings:**
- **`notarize-mac.py` missing `FileNotFoundError` for `xcrun`** (`scripts/notarize-mac.py:41`) — CI without Xcode CLT crashes with traceback instead of actionable message. **Fixed:** caught FileNotFoundError on submit + log-fetch.
- **`SIGNING.md` omits dual-arch venv setup** (`docs/SIGNING.md:63`) — `make ship` requires `.venv-arm64` + `.venv-x86_64` but doc skips straight to `make ship`. **Fixed:** added Section 0.
- **`run_permission_gate` docstring lies about "both permissions"** (`setup_wizard.py:148,782`) — implementation only checks Input Monitoring. **Fixed:** docstrings now match reality.

**Fix commit:** [`4f3d149`](../../commits/4f3d149) — 3/3 applied, 277 tests pass.

### Round 5 — 0 critical, 1 important

**Summary:** Pipeline ordering and `.NOTPARALLEL` correct. One regex correctness issue found.

**Important findings:**
- **`TeamIdentifier` regex sets `team_id="not"` for ad-hoc bundles** (`src/self_updater.py:462`) — `\S+` stops at space in `"not set"`, so the guard never fires; security outcome correct via pin but log message misleading and real parsing path untested. **Fixed:** changed regex to `(.+)` + `.strip()`; added `TestGetSigningInfo` with 3 unit tests.

**Fix commit:** [`601533a`](../../commits/601533a) — 1/1 applied, 280 tests pass.

### Round 6 — 0 critical, 0 important (CONVERGED)

**Summary:** All changed files re-read in full. Security-critical paths (team-ID pinning, codesign verification ordering, HTTPS-only download, Tk timer lifecycle) all correct. Ship pipeline sequencing (build → sign → DMG → notarize → staple → rename) correct for both architectures. No critical or important findings.

**Nice-to-have (deferred):**
- `sign-mac.sh:94` exact-string match `flags=0x10000(runtime)` — future flag combinations (e.g. `0x10002`) could false-negative. Consider `grep -qE 'flags=0x[0-9a-f]+\(runtime'`.
- `packaging` imported in `_verify_codesign:562` but not in `requirements.txt`. Tuple fallback makes it safe; explicit dep would let preferred path always run.
- `_dmg-only` (ship pipeline) skips the Cocoa setIcon step, so distribution DMGs show default icon.
- `SIGNING.md` documents that `notarytool` requires the `betterqa` Keychain profile but no pre-flight check exists.

## Open items

No fixes were skipped. Deferred nice-to-haves across all rounds (review at leisure):

| Area | Item |
|---|---|
| Build cosmetics | DMG custom Finder icon missing from `_dmg-only` (ship pipeline) |
| Build robustness | `sign-mac.sh` hardened-runtime flag match too literal; future-proof with regex |
| Build robustness | Add `packaging>=21.0` to `requirements.txt` (currently relies on fallback) |
| Build robustness | Pre-flight check for `betterqa` Keychain profile in `notarize-mac.py` |
| Privacy disclosure | "Active app name & window title" in DO column doesn't note that titles are SHA-256 hashed by default (GDPR Art. 13 precision) |
| Test coverage | `_codesign_verify` internal error branches (FileNotFoundError, TimeoutExpired) untested directly |
| Doc rot | `_spinner_after_id` field reused for two timers — naming misleading |
| Versioning | `PRIVACY_POLICY_URL` hardcoded; distributed builds can't follow domain changes |

## Files most touched

| Rank | File | Rounds | Notes |
|---|---|---|---|
| 1 | `src/self_updater.py` | 1, 2, 5, 6 | Team-ID pin, version-downgrade guard, TeamIdentifier regex |
| 2 | `tests/test_self_updater_team_pin.py` | 1, 2, 5 | Grew from 5 → 9 tests covering verify + parsing |
| 3 | `src/ui/setup_wizard.py` | 1, 3, 4 | Permission gate rewrite, Optional Config, docstring fix |
| 4 | `scripts/sign-mac.sh` | 1, 5, 6 | Entitlements guard, hardened-runtime check |
| 5 | `Makefile` | 2 | `.PHONY` extended for ship pipeline |

**Final state:** 280 tests passing, ship pipeline ready for first Developer ID notarized release.
