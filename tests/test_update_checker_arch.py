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
    monkeypatch.setattr(
        ma, "_read_proc_translated", lambda: ma.ProbeResult(proc_translated, determined=True, conclusive=True)
    )


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
    than dead-end because no arm64-named asset exists.

    The fixture is deliberately UNSUFFIXED. A DMG that names no architecture is
    the only safe fallback: it may be a universal binary, and it certainly is
    not a build we know to be wrong for this machine. The two tests below pin
    the cases where the fallback DOES know better.
    """
    _fake_host(monkeypatch, machine=X86_64, proc_translated="1")
    release = _release("BetterFlow-macOS.dmg")

    assert (
        _find_platform_asset(release, system="Darwin")
        == "https://x/BetterFlow-macOS.dmg"
    )


def test_apple_silicon_is_never_handed_the_intel_dmg_as_a_fallback(monkeypatch):
    """A release missing its arm64 asset must yield NO update, not the Intel one.

    Serving a build the machine cannot run is worse than serving nothing: on an
    Apple Silicon Mac without Rosetta 2 the installed binary dies with
    EBADARCH/ENOEXEC and capture stops dead — the production fault already on
    record for Ardiel Plata's device (internal-tool2 #2298). Not updating just
    leaves a working install in place until the release is repaired.

    Reachable because the arch fix made it so: before it, a Rosetta install
    resolved arch=x86_64 and always matched the Intel DMG in the first loop, so
    it never reached the fallback at all.
    """
    _fake_host(monkeypatch, machine=X86_64, proc_translated="1")
    release = _release(INTEL_DMG, "BetterFlow-Windows-Setup.exe")

    assert _find_platform_asset(release, system="Darwin") is None


def test_an_intel_mac_is_never_handed_the_arm_dmg_as_a_fallback(monkeypatch):
    """The mirror, and the worse half: there is no reverse Rosetta.

    An arm64 build on a genuine Intel Mac cannot be made to run by any means the
    user has available, so this direction has no recovery path at all.
    """
    _fake_host(monkeypatch, machine=X86_64, proc_translated=None)
    release = _release(ARM_DMG, "BetterFlow-Windows-Setup.exe")

    assert _find_platform_asset(release, system="Darwin") is None


def test_an_undetermined_arch_refuses_every_arch_suffixed_dmg(monkeypatch):
    """platform.machine() is documented to return "" when it cannot tell.

    Pre-fix that was the worst possible value: `"" in name` is true for EVERY
    asset, so the arch-specific loop returned whichever DMG happened to be first
    in the release — a coin flip between a working install and a dead one. When
    we do not know which build is right, the only safe answer is neither.
    """
    _fake_host(monkeypatch, machine="", proc_translated=None)

    assert _find_platform_asset(_release(ARM_DMG, INTEL_DMG), system="Darwin") is None
    assert _find_platform_asset(_release(INTEL_DMG), system="Darwin") is None

    # An unsuffixed DMG names no architecture, so it is still offered.
    assert (
        _find_platform_asset(_release("BetterFlow-macOS.dmg"), system="Darwin")
        == "https://x/BetterFlow-macOS.dmg"
    )


def test_the_zip_fallback_refuses_a_wrong_arch_macos_zip(monkeypatch):
    """The DMG loop is not the only door: the trailing ZIP loop is shared with
    macOS, so it carries the identical hazard one line along.

    No release ships a macOS .zip today (build.yml produces -Update.zip only on
    windows-latest), so this guards a shape the project cannot currently
    produce. It is here because the loop is reachable for Darwin and a future
    packaging change would arrive silently — but it should be read as
    future-proofing, not as a fix for a live fault.
    """
    _fake_host(monkeypatch, machine=X86_64, proc_translated="1")

    assert _find_platform_asset(_release("BetterFlow-macOS-x86_64.zip"), system="Darwin") is None


def test_windows_selection_is_unaffected_by_the_arch_change(monkeypatch):
    """Windows matches on the zip and never on arch; the fix must not disturb
    it. Pinned because the arch lookup now runs for every platform."""
    _fake_host(monkeypatch, machine=X86_64, proc_translated=None)
    release = _release("BetterFlow-Windows-Update.zip", "BetterFlow-Windows-Setup.exe")

    assert (
        _find_platform_asset(release, system="Windows")
        == "https://x/BetterFlow-Windows-Update.zip"
    )
