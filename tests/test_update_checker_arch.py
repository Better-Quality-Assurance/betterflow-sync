"""The updater must pick its DMG by HARDWARE architecture, not by process arch.

An Intel build on an Apple Silicon Mac runs under Rosetta 2, where
``platform.machine()`` answers ``x86_64``. The updater matched on that, so it
re-selected the Intel DMG on every single update and a wrong-arch install could
never correct itself — it stayed on Rosetta indefinitely, one silent update at a
time.

These drive the REAL detection chain (patching the sysctl probe, not passing
``arch=``) so they exercise the code path a user actually hits. Passing ``arch=``
would bypass the fix entirely and pass against the broken version.
"""

import platform

import pytest

from src import machine_arch as ma
from src.machine_arch import ARM64, X86_64
from src.update_checker import _find_platform_asset

ARM_DMG = "BetterFlow-macOS-arm64.dmg"
INTEL_DMG = "BetterFlow-macOS-x86_64.dmg"


@pytest.fixture(autouse=True)
def _clear_arch_cache():
    """The probe is memoised for the life of the process (it cannot change), so
    each simulated host must start from an unprobed module."""
    ma.reset_cache_for_tests()
    yield
    ma.reset_cache_for_tests()


def _release(*names):
    return {
        "assets": [
            {"name": n, "browser_download_url": f"https://x/{n}"} for n in names
        ]
    }


@pytest.fixture
def mac_release():
    return _release(ARM_DMG, INTEL_DMG, "BetterFlow-Windows-Setup.exe")


def _fake_host(monkeypatch, *, machine, proc_translated):
    """Simulate a Mac: what the process reports, and what sysctl says.

    ``proc_translated`` is the raw sysctl value — None models a real Intel Mac,
    where the key does not exist and sysctl exits non-zero.
    """
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.setattr(ma, "_read_proc_translated", lambda: proc_translated)


def test_rosetta_install_is_offered_the_apple_silicon_dmg(monkeypatch, mac_release):
    """THE DEFECT, end to end.

    Process reports x86_64 (Rosetta), hardware is arm64. Pre-fix this returned
    the Intel DMG, so the machine re-installed the wrong build forever.
    """
    _fake_host(monkeypatch, machine=X86_64, proc_translated="1")

    assert _find_platform_asset(mac_release, system="Darwin") == f"https://x/{ARM_DMG}"


def test_genuine_intel_mac_still_gets_the_intel_dmg(monkeypatch, mac_release):
    """The control, and the direction that must never break.

    There is no reverse Rosetta: handing an arm64 DMG to a real Intel Mac ships
    a binary it cannot execute. This test is what stops the fix overshooting.
    """
    _fake_host(monkeypatch, machine=X86_64, proc_translated=None)

    assert _find_platform_asset(mac_release, system="Darwin") == f"https://x/{INTEL_DMG}"


def test_native_apple_silicon_gets_the_arm_dmg(monkeypatch, mac_release):
    _fake_host(monkeypatch, machine=ARM64, proc_translated="0")

    assert _find_platform_asset(mac_release, system="Darwin") == f"https://x/{ARM_DMG}"


def test_explicit_arch_override_still_wins(monkeypatch, mac_release):
    """The existing `arch=` test hook must keep overriding detection, or every
    test written against it silently starts measuring the host instead."""
    _fake_host(monkeypatch, machine=X86_64, proc_translated="1")

    assert (
        _find_platform_asset(mac_release, system="Darwin", arch=X86_64)
        == f"https://x/{INTEL_DMG}"
    )


def test_rosetta_falls_back_to_generic_dmg_when_no_arm_asset(monkeypatch):
    """An older release with a single unsuffixed DMG must still update rather
    than dead-end because no arm64-named asset exists."""
    _fake_host(monkeypatch, machine=X86_64, proc_translated="1")
    release = _release("BetterFlow-macOS.dmg")

    assert (
        _find_platform_asset(release, system="Darwin")
        == "https://x/BetterFlow-macOS.dmg"
    )


def test_windows_selection_is_unaffected_by_the_arch_change(monkeypatch):
    """Windows matches on the zip and never on arch; the fix must not disturb
    it. Pinned because the arch lookup now runs for every platform."""
    _fake_host(monkeypatch, machine=X86_64, proc_translated=None)
    release = _release("BetterFlow-Windows-Update.zip", "BetterFlow-Windows-Setup.exe")

    assert (
        _find_platform_asset(release, system="Windows")
        == "https://x/BetterFlow-Windows-Update.zip"
    )
