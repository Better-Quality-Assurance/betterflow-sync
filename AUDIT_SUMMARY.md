# BetterFlow — End-to-End Audit Summary (triaged)

_Date: 2026-06-17 · 88 source files · ~17.8k LOC in `src/` · 44 test files (563 tests passing)_

**Tools:** semgrep (security), bandit (Python security), gitleaks + trufflehog (secrets, incl. 406-commit history), trivy (dependency CVEs + secrets), radon (complexity/maintainability), plus the zero-dep baseline analyzer (`CODE_AUDIT_REPORT.md`).

## Verdict: clean on security/secrets/deps. The only real debt is code complexity in a few god-files.

Every raw tool finding below was read in source and triaged — not taken at face value.

## Security & secrets — CLEAN ✅
| Area | Result |
|---|---|
| Committed secrets | **0** — gitleaks across 406 commits, trufflehog 0 verified |
| `.env` | gitignored + **not tracked**; contains only `BETTERFLOW_API_URL` / `BETTERFLOW_WEB_BASE_URL` (no secrets). The baseline's "🔴 Critical .env" is a **non-issue**. |
| Dependency CVEs | **0** HIGH/CRITICAL (trivy) |
| Credentials at rest | system keychain via `keyring`; code explicitly refuses plaintext-to-disk |

### Static-analysis findings — all triaged as false positives or low
- **SQL injection (semgrep ×3 ERROR, bandit B608 ×3) — FALSE POSITIVE.** `src/sync/queue.py` builds `IN (?,?,?)` via `",".join("?" * len(ids))` and passes values as bound params to `execute(sql, params)`. Correct, safe SQLite idiom.
- **Credential logging (semgrep ×7) — FALSE POSITIVE.** `src/auth/keychain.py` logs the user **email** and **error messages** only, never the token/credential.
- **Insecure file permissions (semgrep ×3) — FALSE POSITIVE.** Adds the **execute** bit to our own bundled `bf-*` tracker binaries (not world-writable).
- **`shell=True` / `xml` / `xmlrpc` / dangerous-subprocess (baseline "High") — NOISE.** All in `.venv/`, `venv/`, `dist/` (third-party packages + PyInstaller output). **None in our code** (`src/`, `scripts/`).
- **dynamic-urllib (×4) — LOW.** `urllib`/`urlopen` on **hardcoded** GitHub/ActivityWatch URLs (updater + tracker download). Optional hardening: assert `https` + host allowlist before fetch.
- **Error handling:** **0** bare `except: pass` in `src/` — no silent exception swallowing.

> The baseline report's headline "1 Critical / 32 High / 167 Medium" is inflated by scanning `.venv`/`venv`/`dist`. After scoping to our code, there are **no real Critical/High security issues.**

## Tests — EXCELLENT ✅
93.6% test-file ratio (44 test files / 47 source), 563 passing. (Baseline's "no test framework" is a false positive — pytest is configured in `pyproject.toml`.)

## Real, actionable debt — code complexity (Medium)
The genuine finding is **maintainability**, concentrated in the same files behind the recent reliability incidents — static tools can't assess their concurrency/ACID logic, so complexity here is the best proxy for future-bug risk.

| File | Size / MI | Worst functions (cyclomatic complexity) |
|---|---|---|
| `src/main.py` | 2116 LOC, MI ~0 | `_do_sync` (27), `_check_idle_tracker_health` (23), `run` (18) |
| `src/sync/sync_engine.py` | 1999 LOC, MI ~0 | `_transform_event` (**55**), `sync` (**48**), `_transform_and_checkpoint` (19) |
| `src/config.py` | — | `update_from_server` (**67**) |
| `src/aw_manager.py` | MI 17 | `_download_aw_binaries` (32), `_restart_if_needed_locked` (24), `_start_component` (14) |
| `src/ui/tray.py` | 1558 LOC, MI 4.5 | `_create_menu` (24) |

**Highest-value refactors:** split `sync_engine._transform_event` and `.sync` (the per-event transform and the cycle orchestration are doing too much); extract `config.update_from_server`; break `aw_manager` lifecycle into smaller units. These three files are exactly where the orphan/sync/idle bugs lived.

**Also:** no dependency lockfile and `requirements.txt` uses unpinned `>=` ranges — add a lockfile (pip-tools/uv) for reproducible, supply-chain-safe builds (Low).

## Out of scope for static tools (covered by this session's targeted work)
Tracker-process lifecycle, AW→BetterFlow sync correctness, offline-queue/checkpoint ACID, self-updater, and thread-safety can't be assessed by scanners. Spot-checks during this audit: `queue.py` uses parameterized SQL + bounded operations; shared state is lock-guarded. The reliability fixes shipped in v1.5.50 (PR #44/#45) addressed the concrete defects.

_Full raw output: `CODE_AUDIT_REPORT.md`._
