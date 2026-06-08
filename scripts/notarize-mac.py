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
import os
import subprocess
import sys
from pathlib import Path

KEYCHAIN_PROFILE = "betterqa"


def _auth_args() -> list[str]:
    """notarytool credential args.

    Prefer explicit env-var credentials — CI runners have no stored keychain
    profile, so they pass the Apple ID / team / app-specific password directly:
    ``BF_NOTARY_APPLE_ID`` + ``BF_NOTARY_TEAM_ID`` + ``BF_NOTARY_PASSWORD``.
    Fall back to the local ``betterqa`` keychain profile on developer machines
    (set up via ``xcrun notarytool store-credentials``; see docs/SIGNING.md).
    """
    apple_id = os.environ.get("BF_NOTARY_APPLE_ID")
    team_id = os.environ.get("BF_NOTARY_TEAM_ID")
    password = os.environ.get("BF_NOTARY_PASSWORD")
    if apple_id and team_id and password:
        return ["--apple-id", apple_id, "--team-id", team_id, "--password", password]
    return ["--keychain-profile", KEYCHAIN_PROFILE]

# Apple's notary is usually 2-10 min but occasionally hangs ~30 min on
# bad days. Cap the --wait at 45 min so a stuck submission doesn't tie
# up the build forever - the submission keeps running on Apple's side
# and can be polled later via `xcrun notarytool info <id>`.
NOTARYTOOL_WAIT_TIMEOUT_SEC = 45 * 60
NOTARYTOOL_LOG_TIMEOUT_SEC = 60


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <path/to/dmg>", file=sys.stderr)
        return 2

    dmg = Path(argv[1])
    if not dmg.is_file():
        print(f"[notarize-mac] {dmg} not found", file=sys.stderr)
        return 1

    print(f"[notarize-mac] Submitting {dmg} to Apple notary service (may take 2-10 min)...")
    try:
        result = subprocess.run(
            [
                "xcrun", "notarytool", "submit", str(dmg),
                *_auth_args(),
                "--wait",
                "--output-format", "json",
            ],
            capture_output=True, text=True,
            timeout=NOTARYTOOL_WAIT_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        if isinstance(exc, FileNotFoundError):
            print(
                "[notarize-mac] xcrun not found — install Xcode CLT: xcode-select --install",
                file=sys.stderr,
            )
            return 1
        # The submission is still queued/processing on Apple's side; we
        # just could not wait any longer. Tell the caller how to resume.
        print(
            f"[notarize-mac] notarytool --wait exceeded {NOTARYTOOL_WAIT_TIMEOUT_SEC // 60} min. "
            "The submission may still complete on Apple's side. Resume with:\n"
            "  xcrun notarytool history --keychain-profile betterqa\n"
            "to find the submission id, then:\n"
            f"  xcrun notarytool wait <id> --keychain-profile {KEYCHAIN_PROFILE}\n"
            f"and re-run make staple-mac STAPLE_DMG={dmg} once Accepted.",
            file=sys.stderr,
        )
        if exc.stdout:
            print(exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode(errors="replace"), file=sys.stderr)
        return 1

    stdout = result.stdout.strip()
    if not stdout:
        print("[notarize-mac] notarytool produced no output. stderr was:", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        print(
            "[notarize-mac] Common causes: keychain profile 'betterqa' missing "
            "(re-run `xcrun notarytool store-credentials betterqa ...`), Apple "
            "outage (https://developer.apple.com/system-status/), or DMG "
            "rejected at upload (unsigned, wrong format).",
            file=sys.stderr,
        )
        return 1

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        print(f"[notarize-mac] could not parse notarytool JSON: {exc}", file=sys.stderr)
        print(stdout, file=sys.stderr)
        return 1

    submission_id = payload.get("id")
    status = payload.get("status", "<unknown>")
    print(f"[notarize-mac] Status: {status} (submission {submission_id or '<unknown>'})")

    if status == "Accepted":
        return 0

    if not submission_id:
        # Without a submission id we can't fetch the log; dump what we got
        # so the user has SOMETHING to diagnose with.
        print("[notarize-mac] Notarization FAILED with no submission id. Raw payload:", file=sys.stderr)
        print(json.dumps(payload, indent=2), file=sys.stderr)
        return 1

    print("[notarize-mac] Notarization FAILED. Fetching log...", file=sys.stderr)
    try:
        log_result = subprocess.run(
            [
                "xcrun", "notarytool", "log", submission_id,
                *_auth_args(),
            ],
            capture_output=True, text=True,
            timeout=NOTARYTOOL_LOG_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        if isinstance(exc, FileNotFoundError):
            print("[notarize-mac] xcrun not found", file=sys.stderr)
            return 1
        print(
            f"[notarize-mac] log fetch timed out. Status was {status}, submission {submission_id}. "
            f"Run `xcrun notarytool log {submission_id} --keychain-profile {KEYCHAIN_PROFILE}` manually.",
            file=sys.stderr,
        )
        return 1

    if log_result.returncode != 0:
        print(
            f"[notarize-mac] could not fetch log (notarytool log returncode={log_result.returncode}). "
            f"Status was {status}, submission {submission_id}. Manual recovery: "
            f"`xcrun notarytool log {submission_id} --keychain-profile {KEYCHAIN_PROFILE}`",
            file=sys.stderr,
        )
        if log_result.stderr:
            print(log_result.stderr, file=sys.stderr)
        return 1

    print(log_result.stdout, file=sys.stderr)
    if log_result.stderr:
        print(log_result.stderr, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
