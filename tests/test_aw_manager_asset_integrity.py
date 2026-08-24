"""Tracker-archive integrity pins.

`_download_aw_binaries` verifies the downloaded ActivityWatch archive against
`RELEASE_SHA256` and fails CLOSED on a mismatch or a missing pin. That makes a
routine `AW_VERSION` bump dangerous: forget to recompute the hashes and every
machine that needs a bootstrap (or a `_should_reinstall_trackers` reinstall)
silently installs nothing — no trackers, zero activity captured, one log line.

These tests make that mistake fail in CI instead of on the fleet.
"""

import os
import re
import zipfile
from unittest.mock import patch

import pytest

from src import aw_manager
from src.aw_manager import AW_VERSION, RELEASE_ASSETS, RELEASE_SHA256

# The AW_VERSION whose archives were hand-vetted (shasum -a 256 on the release
# assets) to produce the RELEASE_SHA256 values in src/aw_manager.py. Bumping
# AW_VERSION without re-vetting and updating BOTH this constant and the hashes
# is exactly the silent-outage scenario above, so it must break the build.
#
# NOTE: this record is a hand-copy of the same literals, so it catches a
# forgotten bump but CANNOT catch a wrong digest. The independent check is
# scripts/verify_tracker_pins.py, which downloads the real upstream archives
# (nightly CI). Do not treat a green run here as proof the pins are correct.
VETTED_HASHES = {
    "v0.13.2": {
        "darwin": "e62a76c0ec3c0e69d58ba207bb8da6d8d47d0c7ad1bc871ddf702168f291cf5b",
        "windows": "a067fa765678a411991826c4da811fd2d8ca260c2db9d6d897957565b61c369f",
        "linux": "8f62b10babf8a8f108cbdf7267c02fbc1ce2a970fa9535f230b3416b803e3360",
    },
    # v0.14.0b4 is the first pin with a per-architecture macOS key, because it
    # is the first release that publishes both. That is the whole point of the
    # bump (#216): the arm64 archive is what lets Apple Silicon run the
    # trackers natively instead of through Rosetta 2.
    "v0.14.0b4": {
        "darwin-arm64": "98a142c47aadc3873cf3e6f4e71c28c4897a4b48868e4586ed08680c23f06584",
        "darwin-x86_64": "090b91b269b2d18049c44b4d10f9142bcd7c72269b199a570665927d5521f665",
        "windows": "c7acb66d5824aeeef17e0c941efd1f0dbaf216e112260972efa21cff40c25832",
        "linux": "5f608c7c1a717e98b9e46738a0d6aca2906b73d70271fc9882bbabb9aebbbf76",
    },
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def test_every_release_asset_has_a_pinned_hash():
    assert set(RELEASE_SHA256) == set(RELEASE_ASSETS)


@pytest.mark.parametrize("plat", sorted(RELEASE_SHA256))
def test_pinned_hashes_are_lowercase_sha256(plat):
    assert _SHA256_RE.match(RELEASE_SHA256[plat]), (
        f"RELEASE_SHA256[{plat!r}] is not 64 lowercase hex chars"
    )


def test_pinned_hashes_match_the_vetted_record_for_this_aw_version():
    assert AW_VERSION in VETTED_HASHES, (
        f"AW_VERSION {AW_VERSION} has no vetted-hash record. Recompute the "
        "archive SHA-256s (shasum -a 256) and add them to VETTED_HASHES — a "
        "version bump without fresh hashes disables tracker installs fleet-wide."
    )
    assert RELEASE_SHA256 == VETTED_HASHES[AW_VERSION]


def _fake_archive(path):
    """A zip containing all three expected launchers, so only the hash gate
    can be what stops the install."""
    with zipfile.ZipFile(path, "w") as zf:
        for original in aw_manager.AW_TO_BF_NAMES:
            zf.writestr(f"activitywatch/{original}/{original}", b"binary")


class _FakeResponse:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, body, url):
        self._body = body
        self._url = url
        self._offset = 0
        self.headers = {"Content-Length": str(len(body))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return self._url

    def read(self, size):
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk


def test_download_refuses_archive_with_wrong_hash(tmp_path):
    archive = tmp_path / "aw.zip"
    _fake_archive(archive)
    body = archive.read_bytes()
    install_dir = tmp_path / "install"

    url = f"{aw_manager.RELEASE_BASE}/whatever.zip"
    with patch.object(aw_manager.urllib.request, "urlopen",
                      return_value=_FakeResponse(body, url)), \
            patch.object(aw_manager, "RELEASE_SHA256", dict.fromkeys(RELEASE_ASSETS, "0" * 64)):
        assert _download(str(install_dir)) is False

    # Fail closed: nothing extracted, nothing chmod'ed.
    assert not os.path.isdir(install_dir)


def test_download_refuses_when_platform_has_no_pinned_hash(tmp_path):
    archive = tmp_path / "aw.zip"
    _fake_archive(archive)
    body = archive.read_bytes()
    install_dir = tmp_path / "install"

    url = f"{aw_manager.RELEASE_BASE}/whatever.zip"
    with patch.object(aw_manager.urllib.request, "urlopen",
                      return_value=_FakeResponse(body, url)), \
            patch.object(aw_manager, "RELEASE_SHA256", {}):
        assert _download(str(install_dir)) is False

    assert not os.path.isdir(install_dir)


def test_download_refuses_redirect_off_the_allowlist(tmp_path):
    # urlopen follows 3xx transparently, so the pre-flight allowlist check only
    # covered the first hop. A redirect landing off-GitHub must abort before the
    # body is read, hashed or extracted.
    archive = tmp_path / "aw.zip"
    _fake_archive(archive)
    body = archive.read_bytes()
    install_dir = tmp_path / "install"

    with patch.object(aw_manager.urllib.request, "urlopen",
                      return_value=_FakeResponse(body, "https://evil.example.com/a.zip")):
        assert _download(str(install_dir)) is False

    assert not os.path.isdir(install_dir)


def _download(install_dir):
    return aw_manager._download_aw_binaries(install_dir)
