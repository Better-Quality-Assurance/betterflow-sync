"""Regression test for the 2026-06-17 fleet outage: a self-update must strip the
com.apple.quarantine xattr the new bundle inherits from the downloaded DMG.

Left on, Gatekeeper App-Translocation runs the updated app from an ephemeral
read-only path (or blocks the headless post-update `open` entirely), so the
relaunch never starts and every agent goes dark after an update. _strip_quarantine
removes it; this test drives the real `xattr` tool against a real bundle dir.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from src.self_updater import _strip_quarantine

_QUARANTINE = "com.apple.quarantine"
_QVALUE = "0081;00000000;Safari;"


def _has_quarantine(path: Path) -> bool:
    out = subprocess.run(["xattr", str(path)], capture_output=True, text=True).stdout
    return _QUARANTINE in out


@pytest.mark.skipif(sys.platform != "darwin", reason="com.apple.quarantine is macOS-only")
def test_strip_quarantine_clears_bundle_and_nested_files():
    app = Path(tempfile.mkdtemp()) / "BetterFlow.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    binary = macos / "BetterFlow"
    binary.write_text("#!/bin/sh\n")

    # Quarantine the bundle root AND a nested binary — the strip must recurse.
    subprocess.run(["xattr", "-w", _QUARANTINE, _QVALUE, str(app)], check=True)
    subprocess.run(["xattr", "-w", _QUARANTINE, _QVALUE, str(binary)], check=True)
    assert _has_quarantine(app) and _has_quarantine(binary), "precondition: quarantined"

    _strip_quarantine(app)

    assert not _has_quarantine(app), "quarantine must be removed from the bundle root"
    assert not _has_quarantine(binary), "quarantine must be removed recursively"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
def test_strip_quarantine_is_a_noop_when_absent():
    # A clean bundle (no quarantine) must not raise — xattr -d on a missing attr
    # is tolerated, so the update flow proceeds normally.
    app = Path(tempfile.mkdtemp()) / "Clean.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    _strip_quarantine(app)  # must not raise
    assert not _has_quarantine(app)
