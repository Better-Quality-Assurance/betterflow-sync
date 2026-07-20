#!/usr/bin/env python3
"""Verify RELEASE_SHA256 against the real upstream ActivityWatch archives.

tests/test_aw_manager_asset_integrity.py can only prove the pins are well-formed
and that they match a second hand-copied record of the same literals. Both
copies were authored in the same change, so a wrong digest passes the unit
suite and only surfaces on user machines — where the download fails closed and
the device installs no trackers at all.

This script is the independent provenance check: it downloads each asset named
in RELEASE_ASSETS and asserts the real digest equals the pin. Run by
.github/workflows/verify-tracker-pins.yml (nightly + on changes to the pins).

Exit code 0 = every pin matches. 1 = at least one mismatch or download failure.
"""

import hashlib
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.aw_manager import (  # noqa: E402
    AW_VERSION,
    RELEASE_ASSETS,
    RELEASE_BASE,
    RELEASE_SHA256,
)

# Upstream archives are ~100 MB. Cap the read so a redirect to something huge
# can't fill the runner disk before we ever get to compare a digest.
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 300


class _HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject any redirect hop that leaves HTTPS.

    Checking only response.geturl() catches the final URL but not the path
    taken to it: urllib follows redirects itself, so an https -> http -> https
    chain sends a hop in the clear (and http/ftp/file targets are all allowed
    by the default handler) before we ever see the last URL. Fail on the hop.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise ValueError(f"refusing a redirect off HTTPS to: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HTTPSOnlyRedirectHandler)


def _sha256_of_url(url: str) -> str:
    """Stream a URL through SHA-256 without holding the archive in memory."""
    # This script exists to prove provenance, so the transport has to be
    # trustworthy too: a RELEASE_BASE edited down to http://, or a redirect that
    # downgrades off TLS (or to file://), would let an on-path attacker choose
    # the bytes we then bless as "verified". Fail closed on both.
    if not url.lower().startswith("https://"):
        raise ValueError(f"refusing to fetch over a non-HTTPS URL: {url}")
    digest = hashlib.sha256()
    total = 0
    with _OPENER.open(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        final_url = response.geturl()
        if not final_url.lower().startswith("https://"):
            raise ValueError(f"redirected off HTTPS to: {final_url}")
        while True:
            chunk = response.read(CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError(
                    f"archive exceeded {MAX_ARCHIVE_BYTES} bytes before EOF"
                )
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    missing = set(RELEASE_ASSETS) - set(RELEASE_SHA256)
    if missing:
        print(f"FAIL: no pinned hash for platform(s): {sorted(missing)}")
        return 1

    failures = []
    for plat in sorted(RELEASE_ASSETS):
        url = f"{RELEASE_BASE}/{RELEASE_ASSETS[plat]}"
        expected = RELEASE_SHA256[plat]
        print(f"[{plat}] fetching {url}")
        try:
            actual = _sha256_of_url(url)
        except (urllib.error.URLError, ValueError, OSError) as exc:
            failures.append(f"[{plat}] download failed: {exc}")
            print(f"[{plat}] ERROR: {exc}")
            continue

        if actual == expected:
            print(f"[{plat}] OK {actual}")
        else:
            failures.append(f"[{plat}] expected {expected}, upstream is {actual}")
            print(f"[{plat}] MISMATCH expected={expected} actual={actual}")

    if failures:
        print(
            f"\nFAIL: {len(failures)} pin(s) unverified for AW_VERSION {AW_VERSION}.\n"
            "A mismatch means either the pins in src/aw_manager.py are wrong (every "
            "machine needing a bootstrap installs nothing) or upstream mutated an "
            "asset under a pinned tag (treat as a supply-chain incident, do NOT "
            "blindly update the pins)."
        )
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"\nAll {len(RELEASE_ASSETS)} pins verified for AW_VERSION {AW_VERSION}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
