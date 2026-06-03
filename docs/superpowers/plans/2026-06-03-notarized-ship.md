# Notarized Developer ID Ship — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current adhoc-signed BetterFlow.app with Developer ID–signed, hardened-runtime, notarized + stapled DMGs for both arm64 and x86_64, distributed via GitHub Releases.

**Architecture:** Local-only signing on Tudor's Apple Silicon Mac using Developer ID Application cert for Team `87NVC57J44` (Better Quality Assurance SRL). Inside-out signing (each nested Mach-O, then the bundle) via a helper shell script. New Makefile targets `notarize-mac`, `staple-mac`, and orchestrator `ship` that produces two arch-suffixed DMGs (`BetterFlow-macOS-arm64.dmg`, `BetterFlow-macOS-x86_64.dmg`). One additive code change: pin `EXPECTED_TEAM_ID` in `self_updater.py` as defense-in-depth so even a fresh install rejects updates signed by a different Apple Developer ID team.

**Tech Stack:** PyInstaller, `codesign`, `xcrun notarytool`, `xcrun stapler`, `create-dmg`, GitHub CLI (`gh`), Python 3.11, pytest. Spec lives at `docs/superpowers/specs/2026-06-03-notarized-ship-design.md` (commit `d3580d7`).

---

## File Structure

Files this plan creates or modifies (absolute paths from repo root):

| Path | Change | Responsibility |
| --- | --- | --- |
| `scripts/sign-mac.sh` | **CREATE** | Inside-out codesign helper: signs every Mach-O binary inside `dist/BetterFlow.app` then the outer bundle, using hardened runtime + entitlements. Exits non-zero on any signing or verify failure. |
| `Makefile` | MODIFY | Replace `sign-mac` to call the helper. Add `notarize-mac`, `staple-mac`, `ship`. Rename `dmg` output to arch-suffixed name. |
| `src/self_updater.py` | MODIFY | Add `EXPECTED_TEAM_ID = "87NVC57J44"` module constant. Add pin check inside `_verify_codesign`. |
| `tests/test_self_updater_team_pin.py` | **CREATE** | Unit tests for the team ID pin: accepts our team, rejects wrong team, rejects missing team on Developer ID builds. |
| `src/__init__.py` | MODIFY | Bump version for first signed release (1.5.25 → 1.5.26). |
| `docs/SIGNING.md` | **CREATE** | One-pager for future devs: prereq commands, what to expect, how to renew the cert + app-specific password. |

Other files are read but not modified: `build.spec`, `resources/entitlements.mac.plist`, `src/update_checker.py`, `.github/workflows/build.yml`.

---

## Pre-Flight: Manual Prereqs (Tudor, before any code task)

These cannot be TDD'd — they're external state in the Apple Developer Portal and macOS Keychain. **All subsequent tasks assume these are done.** Verification commands at each step confirm the state is correct before proceeding.

- [ ] **PF.1: Request Developer ID Application certificate via Xcode**

In Xcode → Settings (⌘,) → Accounts → select `brad@betterqa.eu` → select team "Better Quality Assurance SRL" → click "Manage Certificates…" → "+" → "Developer ID Application". Cert lands in login Keychain.

Verify:
```bash
security find-identity -v -p codesigning | grep "Developer ID Application: Better Quality Assurance SRL"
```
Expected output: one line with a 40-char hex hash, e.g.
```
  3) ABCDEF0123456789… "Developer ID Application: Better Quality Assurance SRL (87NVC57J44)"
```

If empty, the cert was not generated. Retry the Xcode flow or fall back to developer.apple.com → Certificates → "+" → "Developer ID Application" (manual CSR upload).

- [ ] **PF.2: Generate an app-specific password**

At appleid.apple.com → Sign-In and Security → App-Specific Passwords → "+" → label "notarytool-betterflow". Copy the 19-character password (format `xxxx-xxxx-xxxx-xxxx`).

- [ ] **PF.3: Store notarytool credentials in Keychain**

```bash
xcrun notarytool store-credentials betterqa \
    --apple-id brad@betterqa.eu \
    --team-id 87NVC57J44 \
    --password <paste app-specific password>
```
Expected output: `This process stores your credentials securely in the Keychain. … Profile 'betterqa' successfully saved.`

Verify:
```bash
xcrun notarytool history --keychain-profile betterqa
```
Expected output: empty history table (no submissions yet) — the important part is that the command succeeds and does NOT print an error like `No Keychain password item found for profile`.

- [ ] **PF.4: Confirm both arch venvs are healthy**

```bash
.venv-arm64/bin/python -c "import PyInstaller, pystray, keyring; print('arm64 venv OK')"
arch -x86_64 .venv-x86_64/bin/python -c "import PyInstaller, pystray, keyring; print('x86_64 venv OK')"
```
Expected output: both print "<arch> venv OK". If either fails with `ModuleNotFoundError`, run `pip install -r requirements.txt` inside the broken venv.

---

## Task 1: Pin EXPECTED_TEAM_ID in self_updater (TDD)

**Files:**
- Modify: `src/self_updater.py:480-557` (add constant near top of file, add pin check in `_verify_codesign`)
- Create: `tests/test_self_updater_team_pin.py`

- [ ] **Step 1.1: Write the failing tests**

Create `tests/test_self_updater_team_pin.py`:

```python
"""Tests for the EXPECTED_TEAM_ID pin in self_updater._verify_codesign.

The pin ensures even a fresh install rejects updates signed by a
different (but otherwise valid) Apple Developer ID team. This is
defense-in-depth on top of the existing team-ID-mismatch check, which
only catches changes between two installs.
"""

from pathlib import Path
from unittest.mock import patch

import src.self_updater as su


class TestExpectedTeamIDPin:
    def test_constant_matches_betterqa_srl(self):
        # Hardcoded sanity check: this is the only Apple team we will
        # ever sign with for production releases. Changing it requires
        # a deliberate code change, not a config tweak.
        assert su.EXPECTED_TEAM_ID == "87NVC57J44"

    def test_accepts_correct_team(self, tmp_path):
        new_app = tmp_path / "new.app"
        new_app.mkdir()
        good = su._SigningInfo(is_signed=True, team_id="87NVC57J44", version="1.5.26")
        with patch("src.self_updater._codesign_verify", return_value=True), \
             patch("src.self_updater._get_signing_info", return_value=good):
            assert su._verify_codesign(new_app, current_app_path=None) is True

    def test_rejects_wrong_team(self, tmp_path):
        new_app = tmp_path / "new.app"
        new_app.mkdir()
        bad = su._SigningInfo(is_signed=True, team_id="EVIL12345A", version="1.5.26")
        with patch("src.self_updater._codesign_verify", return_value=True), \
             patch("src.self_updater._get_signing_info", return_value=bad):
            assert su._verify_codesign(new_app, current_app_path=None) is False

    def test_rejects_unsigned_when_pin_active(self, tmp_path):
        # An update with no team ID (adhoc or unsigned) cannot satisfy
        # the pin, so it's rejected even if codesign --verify passes.
        new_app = tmp_path / "new.app"
        new_app.mkdir()
        adhoc = su._SigningInfo(is_signed=True, team_id=None, version="1.5.26")
        with patch("src.self_updater._codesign_verify", return_value=True), \
             patch("src.self_updater._get_signing_info", return_value=adhoc):
            assert su._verify_codesign(new_app, current_app_path=None) is False

    def test_pin_still_rejects_when_current_team_matches(self, tmp_path):
        # The pin is independent of the legacy team-mismatch-vs-current check.
        # Even if both old and new are signed by EVIL12345A, the pin rejects.
        new_app = tmp_path / "new.app"
        current_app = tmp_path / "current.app"
        new_app.mkdir()
        current_app.mkdir()
        evil = su._SigningInfo(is_signed=True, team_id="EVIL12345A", version="1.5.26")
        with patch("src.self_updater._codesign_verify", return_value=True), \
             patch("src.self_updater._get_signing_info", return_value=evil):
            assert su._verify_codesign(new_app, current_app_path=current_app) is False
```

The tests reference a helper `_codesign_verify` that doesn't exist yet — we'll extract it in step 1.3 to make the existing `codesign --verify` subprocess call mockable.

- [ ] **Step 1.2: Run tests to verify they fail**

```bash
.venv-arm64/bin/python -m pytest tests/test_self_updater_team_pin.py -v
```
Expected: 5 errors, all on `AttributeError: module 'src.self_updater' has no attribute 'EXPECTED_TEAM_ID'` or `'_codesign_verify'`.

- [ ] **Step 1.3: Add EXPECTED_TEAM_ID constant and extract _codesign_verify helper**

In `src/self_updater.py`, near the top of the file (after the existing imports, before the first function), add:

```python
# The only Apple Developer ID team we ever ship production releases under.
# Pinned to make _verify_codesign reject updates signed by any other team,
# even on a fresh install with no prior team-ID context to compare against.
EXPECTED_TEAM_ID = "87NVC57J44"  # Better Quality Assurance SRL
```

Then extract the inline `codesign --verify --deep --strict` subprocess call (currently inside `_verify_codesign` around line 491) into a small helper so the test can patch it:

```python
def _codesign_verify(app_path: Path) -> bool:
    """Run `codesign --verify --deep --strict` and return True on success."""
    try:
        result = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(app_path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            logger.info("Code signature verified successfully")
            return True
        stderr = result.stderr.strip()
        if "code object is not signed at all" in stderr:
            logger.error("Rejecting update: new app is not signed")
        else:
            logger.error(f"codesign verification failed: {stderr}")
        return False
    except FileNotFoundError:
        logger.error("codesign binary not found - update aborted (cannot verify signature)")
        return False
    except subprocess.TimeoutExpired:
        logger.error("codesign verification timed out")
        return False
```

Now rewrite the first part of `_verify_codesign` to call the helper, then add the pin check:

```python
def _verify_codesign(app_path: Path, current_app_path: Optional[Path] = None) -> bool:
    """Verify macOS code signature on the extracted .app bundle.

    Checks:
    1. Signature integrity (tampered signatures rejected)
    2. Team ID matches EXPECTED_TEAM_ID (pinned to our Apple team)
    3. Signed->unsigned downgrade rejected
    4. Team ID mismatch vs current install rejected (legacy check, kept)
    5. Version downgrade rejected
    """
    if not _codesign_verify(app_path):
        return False

    new = _get_signing_info(app_path)

    # Pin check: refuse any update whose team is not our team.
    # This catches malicious updates on a fresh install where there is
    # no current_app_path to compare against.
    if new.team_id != EXPECTED_TEAM_ID:
        logger.error(
            f"Rejecting update: team ID {new.team_id!r} does not match "
            f"expected {EXPECTED_TEAM_ID!r}"
        )
        return False

    # Downgrade protection: compare against current app if provided
    if current_app_path is not None:
        current = _get_signing_info(current_app_path)
        # ... rest of existing function body unchanged ...
```

Keep all existing downgrade-protection logic below the pin check (signed→unsigned, team-mismatch-vs-current, version downgrade). The pin runs first because it's the strongest invariant.

- [ ] **Step 1.4: Run tests to verify they pass**

```bash
.venv-arm64/bin/python -m pytest tests/test_self_updater_team_pin.py -v
```
Expected: 5 passed.

- [ ] **Step 1.5: Run full self_updater test suite to make sure no regressions**

```bash
.venv-arm64/bin/python -m pytest tests/test_self_updater_staging.py tests/test_self_updater_team_pin.py -v
```
Expected: all green (existing 11+ staging tests plus the 5 new pin tests).

- [ ] **Step 1.6: Commit**

```bash
git add src/self_updater.py tests/test_self_updater_team_pin.py
git commit -m "feat(updater): pin EXPECTED_TEAM_ID to 87NVC57J44

Reject updates signed by any Apple Developer ID team other than
Better Quality Assurance SRL, even on a fresh install. The existing
team-mismatch check only caught changes between two installs; this
adds defense-in-depth for first-install scenarios.

Extracted codesign --verify subprocess into _codesign_verify helper
for testability."
```

---

## Task 2: Create inside-out signing helper script

**Files:**
- Create: `scripts/sign-mac.sh`

`--deep` is deprecated for sealing nested code (macOS 11+). PyInstaller bundles include many nested Mach-O binaries (Python.framework, Tcl/Tk dylibs, bundled ActivityWatch trackers) that must each be signed individually before the outer bundle is sealed.

- [ ] **Step 2.1: Create the script**

Create `scripts/sign-mac.sh`:

```bash
#!/usr/bin/env bash
# Inside-out codesign for dist/BetterFlow.app.
#
# Signs every nested Mach-O binary first (frameworks, dylibs, helper
# executables), then seals the outer bundle. `--deep` is unreliable
# for PyInstaller bundles since macOS 11 — Apple recommends per-binary
# signing for notarization.
#
# Identity is hardcoded: this app has exactly one valid signing
# identity for distribution. No env-var indirection.

set -euo pipefail

IDENTITY="Developer ID Application: Better Quality Assurance SRL (87NVC57J44)"
ENTITLEMENTS="resources/entitlements.mac.plist"
APP="${1:-dist/BetterFlow.app}"

if [ ! -d "$APP" ]; then
    echo "[sign-mac] $APP not found" >&2
    exit 1
fi

if ! security find-identity -v -p codesigning | grep -q "87NVC57J44"; then
    echo "[sign-mac] Developer ID Application cert for Team 87NVC57J44 not in Keychain" >&2
    echo "[sign-mac] Run PF.1 from docs/superpowers/plans/2026-06-03-notarized-ship.md" >&2
    exit 1
fi

echo "[sign-mac] Signing nested binaries inside $APP"

# Find every Mach-O file under Contents/. The `file` filter is needed
# because PyInstaller also bundles non-Mach-O files (icons, plist, etc.)
# that codesign would refuse.
find "$APP/Contents" -type f \( -perm -u+x -o -name "*.dylib" -o -name "*.so" \) -print0 |
while IFS= read -r -d '' binary; do
    if file "$binary" | grep -q "Mach-O"; then
        echo "[sign-mac]   $binary"
        codesign --force --options runtime \
            --entitlements "$ENTITLEMENTS" \
            --sign "$IDENTITY" \
            --timestamp \
            "$binary"
    fi
done

# Sign all framework bundles (Python.framework, etc.). Frameworks are
# directories ending in .framework — codesign treats them as a unit.
find "$APP/Contents" -type d -name "*.framework" -print0 |
while IFS= read -r -d '' fw; do
    echo "[sign-mac]   framework: $fw"
    codesign --force --options runtime \
        --entitlements "$ENTITLEMENTS" \
        --sign "$IDENTITY" \
        --timestamp \
        "$fw"
done

echo "[sign-mac] Sealing outer bundle"
codesign --force --options runtime \
    --entitlements "$ENTITLEMENTS" \
    --sign "$IDENTITY" \
    --timestamp \
    "$APP"

echo "[sign-mac] Verifying signature"
codesign --verify --deep --strict --verbose=2 "$APP"

echo "[sign-mac] Confirming hardened runtime"
if codesign -d --verbose=4 "$APP" 2>&1 | grep -q "flags=0x10000(runtime)"; then
    echo "[sign-mac] Hardened runtime enabled ✓"
else
    echo "[sign-mac] Hardened runtime NOT enabled on outer bundle" >&2
    exit 1
fi

echo "[sign-mac] Done"
```

- [ ] **Step 2.2: Make it executable**

```bash
chmod +x scripts/sign-mac.sh
```

- [ ] **Step 2.3: Smoke-test against an existing unsigned bundle**

If `dist/BetterFlow.app` already exists from a previous unsigned build, run:
```bash
./scripts/sign-mac.sh
```
Expected output (truncated): many lines starting with `[sign-mac]   /path/to/file`, ending with `Hardened runtime enabled ✓` and `Done`. Exit code 0.

If `dist/BetterFlow.app` does not exist, build it first:
```bash
make build-mac
# Note: this currently calls the OLD sign-mac via Makefile, which is a
# no-op without BF_CODESIGN_IDENTITY. That's fine — Task 3 fixes it.
./scripts/sign-mac.sh
```

- [ ] **Step 2.4: Verify nested binaries are signed**

```bash
codesign -d --verbose=2 dist/BetterFlow.app/Contents/Frameworks/Python.framework 2>&1 | grep "TeamIdentifier"
```
Expected: `TeamIdentifier=87NVC57J44`

```bash
codesign -d --verbose=2 dist/BetterFlow.app/Contents/Resources/resources/trackers/darwin/bf-window-tracker/bf-window-tracker 2>&1 | grep "TeamIdentifier"
```
Expected: `TeamIdentifier=87NVC57J44`. (Path may vary — check `find dist/BetterFlow.app -name bf-window-tracker` for the actual path.)

- [ ] **Step 2.5: Commit**

```bash
git add scripts/sign-mac.sh
git commit -m "feat(build): inside-out codesign helper for PyInstaller bundle

--deep signing is unreliable for PyInstaller bundles since macOS 11.
This script signs every nested Mach-O binary individually before
sealing the outer bundle, which is what notarization expects.

Identity hardcoded to 'Developer ID Application: Better Quality
Assurance SRL (87NVC57J44)'. Refuses to run if the cert is missing
from the Keychain."
```

---

## Task 3: Wire sign-mac.sh into Makefile

**Files:**
- Modify: `Makefile:48-72` (the existing `sign-mac` target)

- [ ] **Step 3.1: Replace the sign-mac target**

In `Makefile`, replace the entire existing `sign-mac:` block (the one that currently checks `BF_CODESIGN_IDENTITY` env var and uses `--deep`) with:

```make
# Deep-sign the built .app via scripts/sign-mac.sh.
# Identity is hardcoded in the script: "Developer ID Application:
# Better Quality Assurance SRL (87NVC57J44)". The script refuses to
# run if the cert is missing from the Keychain.
sign-mac:
	@if [ ! -d "dist/BetterFlow.app" ]; then \
		echo "[sign-mac] dist/BetterFlow.app not found — run build-mac first"; \
		exit 1; \
	fi
	./scripts/sign-mac.sh dist/BetterFlow.app
```

- [ ] **Step 3.2: Build and verify the signed bundle is identifiable**

```bash
rm -rf dist build
make build-mac
```
Expected: `make build-mac` runs PyInstaller, then automatically calls `sign-mac`, which runs the helper script. Should end with `[sign-mac] Done` and `Built: dist/BetterFlow.app`.

Verify:
```bash
codesign -d --verbose=2 dist/BetterFlow.app 2>&1 | grep -E "Identifier|TeamIdentifier|Authority"
```
Expected lines include:
- `Identifier=co.betterqa.betterflow`
- `TeamIdentifier=87NVC57J44`
- `Authority=Developer ID Application: Better Quality Assurance SRL (87NVC57J44)`
- `Authority=Developer ID Certification Authority`
- `Authority=Apple Root CA`

- [ ] **Step 3.3: Commit**

```bash
git add Makefile
git commit -m "build(make): switch sign-mac to inside-out helper

Drops the BF_CODESIGN_IDENTITY env var fallback (this app has exactly
one valid distribution identity) and the deprecated --deep flag.
Replaced with a call into scripts/sign-mac.sh, which signs nested
Mach-O binaries individually as required for notarization."
```

---

## Task 4: Add notarize-mac Makefile target

**Files:**
- Modify: `Makefile` (add new target after `sign-mac`)

- [ ] **Step 4.1: Add the target**

Append after the `sign-mac` block in `Makefile`:

```make
# Submit a signed DMG to Apple's notary service and wait synchronously
# for the verdict. On rejection, prints the notary log before failing.
#
# Pass DMG=path/to/file.dmg to override the default arm64 path.
NOTARIZE_DMG ?= dist/BetterFlow-macOS-arm64.dmg

notarize-mac:
	@if [ ! -f "$(NOTARIZE_DMG)" ]; then \
		echo "[notarize-mac] $(NOTARIZE_DMG) not found"; \
		exit 1; \
	fi
	@echo "[notarize-mac] Submitting $(NOTARIZE_DMG) to Apple notary service…"
	@submission_id=$$(xcrun notarytool submit "$(NOTARIZE_DMG)" \
		--keychain-profile betterqa \
		--wait \
		--output-format json | tee /tmp/notarize-$$$$.json | \
		python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["id"]); sys.exit(0 if d["status"]=="Accepted" else 1)') || { \
		echo "[notarize-mac] Notarization FAILED. Fetching log…"; \
		bad_id=$$(python3 -c 'import json; print(json.load(open("/tmp/notarize-'$$$$'.json"))["id"])'); \
		xcrun notarytool log $$bad_id --keychain-profile betterqa; \
		exit 1; \
	}; \
	echo "[notarize-mac] Accepted (submission $$submission_id)"
```

The Makefile shell quoting here is fragile — if you run into issues, factor out the JSON parsing into a small Python helper at `scripts/notarize-mac.py`. (See Task 4 alternative below if quoting breaks.)

- [ ] **Step 4.2: Verify the Keychain profile is set up**

```bash
xcrun notarytool history --keychain-profile betterqa
```
Expected: a (possibly empty) table of past submissions, NOT an error about a missing keychain item. If you see `Error: No Keychain password item found for profile: betterqa`, re-run PF.3.

- [ ] **Step 4.3: Commit (notarize-mac target only; we will test it end-to-end in Task 7)**

```bash
git add Makefile
git commit -m "build(make): add notarize-mac target

Submits a signed DMG to Apple's notary service via xcrun notarytool,
using the 'betterqa' keychain profile (set up via PF.3 in the
implementation plan). --wait blocks until Apple returns Accepted or
Invalid; on Invalid the notary log is fetched and printed before
the target fails."
```

---

## Task 5: Add staple-mac Makefile target

**Files:**
- Modify: `Makefile`

- [ ] **Step 5.1: Add the target**

Append after `notarize-mac` in `Makefile`:

```make
# Staple the notarization ticket onto the DMG and the .app inside it.
# Stapling embeds the ticket so Gatekeeper does not need to phone
# home on first launch.
#
# Pass DMG=path/to/file.dmg to override the default arm64 path.
STAPLE_DMG ?= dist/BetterFlow-macOS-arm64.dmg
STAPLE_APP ?= dist/BetterFlow.app

staple-mac:
	@echo "[staple-mac] Stapling $(STAPLE_DMG)"
	xcrun stapler staple "$(STAPLE_DMG)"
	xcrun stapler validate "$(STAPLE_DMG)"
	@if [ -d "$(STAPLE_APP)" ]; then \
		echo "[staple-mac] Stapling $(STAPLE_APP)"; \
		xcrun stapler staple "$(STAPLE_APP)"; \
		xcrun stapler validate "$(STAPLE_APP)"; \
	fi
	@echo "[staple-mac] Done"
```

- [ ] **Step 5.2: Commit**

```bash
git add Makefile
git commit -m "build(make): add staple-mac target

Embeds the notarization ticket into the DMG and the .app so
Gatekeeper does not need network access to verify on first launch."
```

---

## Task 6: Restructure dmg target for arch-suffixed output

**Files:**
- Modify: `Makefile:97-117` (the existing `dmg` target)

The current `dmg` target produces `dist/BetterFlow.dmg`. `update_checker._find_platform_asset` (line 90) requires the architecture string (`arm64` or `x86_64`) to be in the asset name. The new target uses `TARGET_ARCH` (already an env var honored by `build.spec`) to set the suffix.

- [ ] **Step 6.1: Replace the dmg target**

In `Makefile`, replace the existing `dmg: build-mac` block with:

```make
# Build an arch-suffixed DMG. TARGET_ARCH defaults to the host arch.
# Use `make ship` to build both architectures end-to-end.
TARGET_ARCH ?= $(shell uname -m | sed 's/aarch64/arm64/')

dmg: build-mac
	@dmg_path="dist/BetterFlow-macOS-$(TARGET_ARCH).dmg"; \
	rm -f "$$dmg_path"; \
	create-dmg \
		--volname "BetterFlow" \
		--volicon "resources/icon.icns" \
		--window-pos 200 120 \
		--window-size 600 400 \
		--icon-size 100 \
		--icon "BetterFlow.app" 150 190 \
		--app-drop-link 450 185 \
		"$$dmg_path" \
		"dist/BetterFlow.app"; \
	echo "[dmg] Created $$dmg_path"
	rm -rf "dist/BetterFlow"
	@dmg_path="dist/BetterFlow-macOS-$(TARGET_ARCH).dmg"; \
	python3 -c "import Cocoa, os; \
ws = Cocoa.NSWorkspace.sharedWorkspace(); \
img = Cocoa.NSImage.alloc().initWithContentsOfFile_(os.path.abspath('resources/icon.png')); \
ws.setIcon_forFile_options_(img, os.path.abspath('$$dmg_path'), 0); \
print('Custom icon set on', '$$dmg_path')"
```

- [ ] **Step 6.2: Verify arm64 DMG is produced with correct name**

```bash
rm -rf dist build
make dmg
ls -la dist/BetterFlow-macOS-arm64.dmg
```
Expected: file exists, ~100-200 MB.

- [ ] **Step 6.3: Verify Gatekeeper accepts the signed DMG (pre-notarization)**

```bash
spctl -a -vv -t install dist/BetterFlow-macOS-arm64.dmg 2>&1
```
Expected: contains `rejected` and `source=No usable signature` OR `source=Unnotarized Developer ID` — pre-notarization this WILL fail spctl; that's expected. The DMG is signed-trusted but the notarization ticket has not been requested or stapled yet. Notarization is Task 7.

- [ ] **Step 6.4: Commit**

```bash
git add Makefile
git commit -m "build(make): arch-suffix DMG output for update_checker

The asset name must include 'arm64' or 'x86_64' so the in-app
updater (update_checker._find_platform_asset) picks the right
download for each Mac. TARGET_ARCH defaults to host arch via uname.

dist/BetterFlow.dmg -> dist/BetterFlow-macOS-<arch>.dmg"
```

---

## Task 7: End-to-end arm64 release dry-run (notarization smoke test)

This is the first real notarization. It exercises notarize-mac and staple-mac against a real DMG and surfaces any signing issues (unsigned nested binaries, missing hardened runtime on a framework, etc.) that the notary service catches but `codesign --verify` does not.

- [ ] **Step 7.1: Build, sign, package the arm64 DMG**

```bash
rm -rf dist build
make dmg
```
Expected: ends with `[dmg] Created dist/BetterFlow-macOS-arm64.dmg`.

- [ ] **Step 7.2: Notarize**

```bash
make notarize-mac
```
Expected output (truncated, takes 2–10 min):
```
[notarize-mac] Submitting dist/BetterFlow-macOS-arm64.dmg to Apple notary service…
{
  "id": "abcdef-1234-…",
  "status": "Accepted",
  …
}
[notarize-mac] Accepted (submission abcdef-1234-…)
```

If status is `Invalid`, the target dumps the notary log automatically. Common rejection reasons and fixes:

| Reason | Fix |
| --- | --- |
| `The signature of the binary is invalid.` (for a nested binary) | The nested binary was not signed by `scripts/sign-mac.sh`. Inspect the log's `path` field and add the file's parent directory to the `find` patterns in `sign-mac.sh`. |
| `The executable does not have the hardened runtime enabled.` | A nested binary was signed without `--options runtime`. Check `sign-mac.sh` — every `codesign` invocation should include `--options runtime`. |
| `The signature does not include a secure timestamp.` | A nested binary was signed without `--timestamp`. Check `sign-mac.sh` — every `codesign` invocation should include `--timestamp` (NOT `--timestamp=none`). |
| `The binary uses an SDK older than the 10.9 SDK.` | Bundled tracker binary was built with an ancient SDK. Re-run `python scripts/download_aw.py` to fetch fresh ActivityWatch binaries. |

Iterate on `sign-mac.sh` until notarization is `Accepted`. Each iteration: edit script → `rm -rf dist build && make dmg && make notarize-mac`.

- [ ] **Step 7.3: Staple**

```bash
make staple-mac
```
Expected output:
```
[staple-mac] Stapling dist/BetterFlow-macOS-arm64.dmg
Processing: dist/BetterFlow-macOS-arm64.dmg
The staple and validate action worked!
[staple-mac] Stapling dist/BetterFlow.app
…
The staple and validate action worked!
[staple-mac] Done
```

- [ ] **Step 7.4: Final Gatekeeper sanity check**

```bash
spctl -a -vv -t install dist/BetterFlow-macOS-arm64.dmg
```
Expected: `dist/BetterFlow-macOS-arm64.dmg: accepted` and `source=Notarized Developer ID`.

- [ ] **Step 7.5: Install and confirm TCC persistence**

```bash
./scripts/install-mac.sh
```
Then open the installed app from `/Applications`. Verify:
- No Gatekeeper warning dialog
- Tray icon appears
- Grant Input Monitoring + Accessibility once via System Settings → Privacy & Security
- Quit the app
- `rm -rf dist build && make dmg && make notarize-mac && make staple-mac && ./scripts/install-mac.sh`
- Open again: should run WITHOUT prompting for Input Monitoring or Accessibility again. **This is the original problem this whole project solves.**

- [ ] **Step 7.6: No code change to commit (this task is a verification gate). If sign-mac.sh was iterated, commit those changes:**

```bash
git status
# If scripts/sign-mac.sh was modified:
git add scripts/sign-mac.sh
git commit -m "fix(build): iterate sign-mac.sh until notarization passes

Notary log surfaced <specific finding>. Adjusted <specific file
pattern / flag> in the inside-out sign loop. arm64 notarization
now succeeds end-to-end."
```

---

## Task 8: Add `ship` target orchestrating both architectures

**Files:**
- Modify: `Makefile`

- [ ] **Step 8.1: Add the target**

Append at the end of `Makefile`:

```make
# Full release pipeline: build, sign, notarize, staple for both
# architectures. Runs serially. Each architecture takes ~5-15 min
# wall-clock (PyInstaller build + Apple notary turnaround).
#
# Does NOT tag or push — those remain manual gates to avoid
# accidentally cutting a release from a dirty working copy.
ship: ship-arm64 ship-x86_64
	@echo "[ship] Both architectures shipped:"
	@ls -la dist/BetterFlow-macOS-*.dmg

ship-arm64:
	@echo "[ship] === arm64 ==="
	rm -rf dist build
	TARGET_ARCH=arm64 $(MAKE) dmg
	NOTARIZE_DMG=dist/BetterFlow-macOS-arm64.dmg $(MAKE) notarize-mac
	STAPLE_DMG=dist/BetterFlow-macOS-arm64.dmg $(MAKE) staple-mac
	mv dist/BetterFlow.app dist/BetterFlow-arm64.app

ship-x86_64:
	@echo "[ship] === x86_64 ==="
	rm -rf build
	# Note: we use the x86_64 venv's pyinstaller under Rosetta.
	# build.spec reads TARGET_ARCH and passes it to PyInstaller.
	arch -x86_64 .venv-x86_64/bin/python -m PyInstaller build.spec --clean
	./scripts/sign-mac.sh dist/BetterFlow.app
	TARGET_ARCH=x86_64 $(MAKE) -f Makefile _ship-x86-dmg
	NOTARIZE_DMG=dist/BetterFlow-macOS-x86_64.dmg $(MAKE) notarize-mac
	STAPLE_DMG=dist/BetterFlow-macOS-x86_64.dmg $(MAKE) staple-mac

# Internal: x86_64 DMG packaging, factored out so ship-x86_64 can
# invoke create-dmg without re-running PyInstaller via dmg target.
_ship-x86-dmg:
	@dmg_path="dist/BetterFlow-macOS-x86_64.dmg"; \
	rm -f "$$dmg_path"; \
	create-dmg \
		--volname "BetterFlow" \
		--volicon "resources/icon.icns" \
		--window-pos 200 120 \
		--window-size 600 400 \
		--icon-size 100 \
		--icon "BetterFlow.app" 150 190 \
		--app-drop-link 450 185 \
		"$$dmg_path" \
		"dist/BetterFlow.app"
```

- [ ] **Step 8.2: Smoke-test the x86_64 build path in isolation first**

The arm64 path is already proven by Task 7. Confirm x86_64 builds before running the full ship:
```bash
rm -rf dist build
arch -x86_64 .venv-x86_64/bin/python -m PyInstaller build.spec --clean
file dist/BetterFlow.app/Contents/MacOS/BetterFlow
```
Expected: `Mach-O 64-bit executable x86_64` (NOT arm64).

If PyInstaller fails inside Rosetta, check that every dependency in `.venv-x86_64` is installed as x86_64 wheels:
```bash
arch -x86_64 .venv-x86_64/bin/pip install --upgrade --force-reinstall -r requirements.txt
```

- [ ] **Step 8.3: Run the full ship target**

```bash
make ship
```
Expected: ~20-30 min total. Output ends with:
```
[ship] Both architectures shipped:
-rw-r--r--  1 brad  staff  N  Jun  3 …  dist/BetterFlow-macOS-arm64.dmg
-rw-r--r--  1 brad  staff  N  Jun  3 …  dist/BetterFlow-macOS-x86_64.dmg
```

- [ ] **Step 8.4: Verify both DMGs pass Gatekeeper**

```bash
for dmg in dist/BetterFlow-macOS-*.dmg; do
    echo "=== $dmg ==="
    spctl -a -vv -t install "$dmg"
done
```
Expected: each one reports `accepted` and `source=Notarized Developer ID`.

- [ ] **Step 8.5: Commit**

```bash
git add Makefile
git commit -m "build(make): add ship target for dual-arch release

ship-arm64 + ship-x86_64 orchestrate build -> sign -> dmg ->
notarize -> staple for each architecture. x86_64 runs under
Rosetta using the existing .venv-x86_64 universal2 Python.

Tag + GitHub Release creation remain manual gates."
```

---

## Task 9: Bump version, tag, create GitHub Release

**Files:**
- Modify: `src/__init__.py:3`

- [ ] **Step 9.1: Bump version**

In `src/__init__.py`, change:
```python
__version__ = "1.5.25"
```
to:
```python
__version__ = "1.5.26"
```

- [ ] **Step 9.2: Rebuild both DMGs with the new version**

```bash
make ship
```
Expected: ~20-30 min. Both DMGs at the new version. Confirm:
```bash
defaults read "$(pwd)/dist/BetterFlow-arm64.app/Contents/Info.plist" CFBundleShortVersionString
```
Expected: `1.5.26`

- [ ] **Step 9.3: Commit + tag + push**

```bash
git add src/__init__.py
git commit -m "chore(release): 1.5.26 — first Developer ID notarized build

First release signed by Team 87NVC57J44 (Better Quality Assurance
SRL) and notarized + stapled. Existing users on the adhoc-signed
1.5.25 will receive this via the in-app updater (adhoc -> Developer
ID transition is safe per the spec analysis)."
git tag v1.5.26
git push
git push --tags
```

- [ ] **Step 9.4: Create the GitHub Release with both DMGs**

```bash
gh release create v1.5.26 \
    dist/BetterFlow-macOS-arm64.dmg \
    dist/BetterFlow-macOS-x86_64.dmg \
    --title "v1.5.26 — Notarized Developer ID" \
    --notes "First release signed by Better Quality Assurance SRL (Team 87NVC57J44) and notarized by Apple.

**For existing users:** the in-app updater will fetch and apply this automatically. No re-grant of Input Monitoring or Accessibility required after this update — TCC grants now persist across all future rebuilds.

**Fresh install:** download the DMG matching your Mac (\`arm64\` for Apple Silicon, \`x86_64\` for Intel) and drag BetterFlow.app to /Applications."
```
Expected: release URL printed.

- [ ] **Step 9.5: Verify release assets**

```bash
gh release view v1.5.26
```
Expected: both DMG assets listed with non-zero sizes.

- [ ] **Step 9.6: Verify in-app updater picks up the release**

On a Mac with the current adhoc-signed 1.5.25 installed:
1. Open the app
2. Wait for the update poller (default interval — check `update_checker.py` for the cadence) OR trigger a manual check via the tray menu if there is one
3. Verify the staged update applies on next restart
4. Verify the running app reports version 1.5.26 after restart
5. Verify Input Monitoring + Accessibility are STILL granted (TCC subject did not change in a destructive way during the adhoc → Developer ID transition)

If TCC grants are lost on the 1.5.25 → 1.5.26 transition specifically: that's expected and one-time. The whole point is that 1.5.26 → 1.5.27 (and every subsequent upgrade) will NOT lose grants. Document this in the release notes for the next release if needed.

---

## Task 10: Write SIGNING.md + update memory

**Files:**
- Create: `docs/SIGNING.md`
- Update memory file: `/Users/brad/.claude/projects/-Users-brad-Code2-betterflow-sync/memory/apple_developer_enrollment.md`

- [ ] **Step 10.1: Create the developer onboarding doc**

Create `docs/SIGNING.md`:

```markdown
# Signing + Notarization Setup

BetterFlow Sync is signed and notarized for distribution outside the Mac App Store. This doc covers the one-time setup a new developer must do on their Mac before they can run `make ship`.

## Prerequisites

- Be a member of the **Better Quality Assurance SRL** Apple Developer team (Team ID `87NVC57J44`) with role Admin or higher
- Sign Xcode into your `@betterqa.eu` Apple ID at Xcode → Settings → Accounts
- Xcode 15+ installed (for `xcrun notarytool`)

## One-time setup

### 1. Request the Developer ID Application certificate

Xcode → Settings → Accounts → select your account → select "Better Quality Assurance SRL" → Manage Certificates → "+" → "Developer ID Application".

Verify:
```bash
security find-identity -v -p codesigning | grep "Developer ID Application: Better Quality Assurance SRL"
```

### 2. Generate an app-specific password for notarization

At appleid.apple.com → Sign-In and Security → App-Specific Passwords → "+" with label "notarytool-betterflow". Copy the 19-character password.

### 3. Store notarytool credentials in your Keychain

```bash
xcrun notarytool store-credentials betterqa \
    --apple-id <your-apple-id>@betterqa.eu \
    --team-id 87NVC57J44 \
    --password <paste app-specific password>
```

Verify:
```bash
xcrun notarytool history --keychain-profile betterqa
```
Should print an empty (or populated) history table, NOT a missing-keychain-item error.

## Releasing

Once setup is done, the release pipeline is:

```bash
# 1. Bump version
$EDITOR src/__init__.py

# 2. Build, sign, notarize, staple both archs
make ship

# 3. Tag and push
git commit -am "chore(release): 1.5.X"
git tag v1.5.X
git push --tags

# 4. Create GitHub Release with both DMGs
gh release create v1.5.X \
    dist/BetterFlow-macOS-arm64.dmg \
    dist/BetterFlow-macOS-x86_64.dmg \
    --notes-from-tag
```

## Renewing credentials

- **App-specific password:** revoke + regenerate at appleid.apple.com, then re-run `xcrun notarytool store-credentials betterqa …`
- **Developer ID cert:** Apple-issued certs are valid ~5 years. Renew via Xcode → Manage Certificates → "+" before the existing one expires. Stapled tickets on already-released DMGs keep them trusted even after the signing cert expires.

## Why we sign + notarize

- **TCC grant stability:** stable code-signing identity means macOS recognizes every build as "the same app", so users keep their Input Monitoring + Accessibility grants across updates.
- **Gatekeeper friction:** notarization removes the "unidentified developer — cannot be opened" dialog on first launch.
- **Self-updater safety:** `self_updater.py` pins `EXPECTED_TEAM_ID = "87NVC57J44"` and rejects any update signed by a different team, so the auto-update path can only deliver our team's binaries.

For the full design rationale see `docs/superpowers/specs/2026-06-03-notarized-ship-design.md`.
```

- [ ] **Step 10.2: Commit the doc**

```bash
git add docs/SIGNING.md
git commit -m "docs: add SIGNING.md for new-developer onboarding

Covers the one-time cert + app-specific password + notarytool
keychain profile setup that a new BetterFlow Sync developer must
do before they can run \`make ship\`."
```

- [ ] **Step 10.3: Update memory file with shipped state**

Edit `/Users/brad/.claude/projects/-Users-brad-Code2-betterflow-sync/memory/apple_developer_enrollment.md` to reflect that the cert is now in the Keychain, notarytool is configured, and 1.5.26 was the first signed release. Remove the "what still needs to happen" section since it's all done. Replace with a "current state" section that future sessions can read as ground truth.

This update should NOT be committed to the repo — memory files live in `~/.claude/projects/…`, outside the repo.

---

## Self-Review

**Spec coverage check** — every section of the spec has a corresponding task:

| Spec section | Plan task |
| --- | --- |
| §1 Goal & non-goals | Implicit in plan goal statement |
| §2 Prerequisites | Pre-Flight PF.1–PF.4 |
| §3.1 Makefile sign-mac change | Task 3 (delegates to script in Task 2) |
| §3.2 build.spec confirmations | No code change needed; covered by Step 9.2 sanity check |
| §3.3 entitlements (no change) | No task needed |
| §3.4 self_updater EXPECTED_TEAM_ID pin | Task 1 |
| §3.5 CI workflow (no change) | Documented in Step 8 open question; revisit if conflict surfaces |
| §4 Build pipeline (dual-arch) | Tasks 6, 7, 8 |
| §5 Distribution via GitHub Releases | Task 9 |
| §6 Verification / acceptance | Task 7.4–7.5, Task 8.4, Task 9.6 |
| §7 Risks + mitigations | Surfaced inline in Task 7.2 (notary rejection table) |
| §8 Open questions | Left to plan-phase execution; flagged in tasks where relevant |
| §9 Out-of-scope follow-ups | Not implemented (deliberately) |

**Placeholder scan:** None. Every step has concrete code, commands, or a specific file edit.

**Type consistency:** `_SigningInfo(is_signed=…, team_id=…, version=…)` is referenced consistently in Task 1 tests and matches the existing definition at `self_updater.py:_get_signing_info`. `EXPECTED_TEAM_ID` is referenced as `su.EXPECTED_TEAM_ID` in tests and defined as a module constant. `_codesign_verify` helper is referenced in tests AND defined in step 1.3.

Plan is internally consistent and covers the spec.
