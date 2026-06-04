#!/usr/bin/env python3
"""Submit a signed DMG to Apple's notary service via xcrun notarytool.

Uses the keychain profile 'betterqa' (set up via `xcrun notarytool
store-credentials`). Blocks until Apple returns Accepted or Invalid.
On Invalid, prints the notary log before exiting non-zero.

Usage:
    python3 scripts/notarize-mac.py dist/BetterFlow-macOS-arm64.dmg
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

KEYCHAIN_PROFILE = "betterqa"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <path/to/dmg>", file=sys.stderr)
        return 2

    dmg = Path(argv[1])
    if not dmg.is_file():
        print(f"[notarize-mac] {dmg} not found", file=sys.stderr)
        return 1

    print(f"[notarize-mac] Submitting {dmg} to Apple notary service (may take 2-10 min)…")
    result = subprocess.run(
        [
            "xcrun", "notarytool", "submit", str(dmg),
            "--keychain-profile", KEYCHAIN_PROFILE,
            "--wait",
            "--output-format", "json",
        ],
        capture_output=True, text=True,
    )

    stdout = result.stdout.strip()
    if not stdout:
        print(f"[notarize-mac] notarytool produced no output", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        return 1

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        print(f"[notarize-mac] could not parse notarytool JSON: {exc}", file=sys.stderr)
        print(stdout, file=sys.stderr)
        return 1

    submission_id = payload.get("id", "<unknown>")
    status = payload.get("status", "<unknown>")
    print(f"[notarize-mac] Status: {status} (submission {submission_id})")

    if status == "Accepted":
        return 0

    print(f"[notarize-mac] Notarization FAILED. Fetching log…", file=sys.stderr)
    log_result = subprocess.run(
        [
            "xcrun", "notarytool", "log", submission_id,
            "--keychain-profile", KEYCHAIN_PROFILE,
        ],
        capture_output=True, text=True,
    )
    print(log_result.stdout, file=sys.stderr)
    if log_result.stderr:
        print(log_result.stderr, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
