"""An unresolved Rosetta probe must reach the updater as doubt, not as a guess.

`update_checker._is_wrong_arch` has always had a safety branch for exactly this:

    if not arch:
        return any(token in name for token in (ARM64, X86_64))

"When we do not know which build is right, the only safe answer is neither."
But `true_machine_arch` returned `""` only when `platform.machine()` itself did,
which does not happen on macOS — so the branch guarded a case that could not
arise, while a Mac whose probe timed out reported a confident "x86_64" and was
offered the Intel build. The module knew it was uncertain and discarded that
knowledge one function before the code that would act on it (#196).

These pin the distinction that makes it real: mid-backoff is NOT undetermined
(the retry schedule covers a briefly-busy machine), settled-with-no-answer is.
"""

from unittest.mock import patch

import pytest

from src import machine_arch as ma
from src.machine_arch import ARM64, X86_64, ProbeResult, true_machine_arch
from src.ui.tray import arch_menu_label
from src.update_checker import _find_platform_asset, _is_wrong_arch


@pytest.fixture(autouse=True)
def _clear():
    ma.reset_cache_for_tests()
    yield
    ma.reset_cache_for_tests()


def _exhaust_the_budget():
    """Drive the probe to its settled-unresolved state.

    Every attempt returns a TIMEOUT (conclusive=False) and the clock is advanced
    past each backoff window, so the retry schedule is spent rather than skipped
    — the same path a permanently congested Mac takes.
    """
    clock = [0.0]
    with patch.object(ma, "_read_proc_translated", lambda: ProbeResult(None, conclusive=False)), \
         patch.object(ma, "_monotonic", lambda: clock[0]):
        for _ in range(len(ma._RETRY_BACKOFF_SECONDS) + 2):
            ma._read_proc_translated_cached()
            clock[0] += 100000.0


def test_a_settled_unresolved_probe_reports_undetermined():
    _exhaust_the_budget()
    assert ma.probe_settled_unresolved() is True
    assert true_machine_arch(system="Darwin", machine=X86_64) == ""


def test_mid_backoff_is_not_undetermined():
    """A machine that lost ONE probe is busy, not unknowable.

    Reporting it as undetermined would withhold updates from a Mac that is
    merely mid-boot-storm — a worse trade than the one this feature guards
    against, and the reason `probe_settled_unresolved` is not just "has failed".
    """
    with patch.object(ma, "_read_proc_translated", lambda: ProbeResult(None, conclusive=False)):
        ma._read_proc_translated_cached()  # one transient failure, budget intact

    assert ma.probe_settled_unresolved() is False
    assert true_machine_arch(system="Darwin", machine=X86_64) == X86_64


def test_a_conclusive_answer_is_never_undetermined():
    """The control. A real Intel Mac has no proc_translated key, forever."""
    with patch.object(ma, "_read_proc_translated", lambda: ProbeResult(None, conclusive=True)):
        ma._read_proc_translated_cached()

    assert ma.probe_settled_unresolved() is False
    assert true_machine_arch(system="Darwin", machine=X86_64) == X86_64


def test_undetermined_is_macos_only():
    """Windows and Linux never run this probe, so they can never be unknowable.

    Leaking `""` off Darwin would make those hosts refuse assets they have
    always accepted.
    """
    _exhaust_the_budget()
    assert true_machine_arch(system="Windows", machine="AMD64") == "AMD64"
    assert true_machine_arch(system="Linux", machine=X86_64) == X86_64


# --- the point of all of it: the updater must withhold ---------------------


RELEASE = {
    "assets": [
        {"name": "BetterFlow-macOS-arm64.dmg", "browser_download_url": "https://x/arm64.dmg"},
        {"name": "BetterFlow-macOS-x86_64.dmg", "browser_download_url": "https://x/x86_64.dmg"},
    ]
}


def test_an_undetermined_mac_is_offered_no_arch_suffixed_build():
    """End to end through the real entry point, not just the helper.

    This is the assertion the whole change exists for: before it, the settled
    -unresolved Mac matched the Intel DMG in the first loop and `_is_wrong_arch`
    was never consulted at all.
    """
    _exhaust_the_budget()
    with patch("platform.machine", lambda: X86_64):
        assert _find_platform_asset(RELEASE, system="Darwin") is None


def test_a_determined_mac_still_gets_its_build():
    """The permissive direction. Withholding from everyone would also 'pass'."""
    with patch.object(ma, "_read_proc_translated", lambda: ProbeResult("1", conclusive=True)):
        ma._read_proc_translated_cached()

    with patch("platform.machine", lambda: X86_64):
        assert _find_platform_asset(RELEASE, system="Darwin") == "https://x/arm64.dmg"


def test_the_non_darwin_guard_in_is_wrong_arch_is_witnessed():
    """Requested on #196: this guard survived deletion against the full suite.

    With an unknown arch the refusal is platform-blind by construction, so
    without the Darwin gate a Windows host that could not determine its
    architecture would refuse an asset it has always accepted.
    """
    assert _is_wrong_arch("BetterFlow-Windows-x86_64-Update.zip", "", "Windows") is False
    # ...while the same unknown arch on macOS still refuses both suffixes.
    assert _is_wrong_arch("BetterFlow-macOS-x86_64.dmg", "", "Darwin") is True
    assert _is_wrong_arch("BetterFlow-macOS-arm64.dmg", "", "Darwin") is True

    # And through the entry point, because the assertions above test the
    # PRODUCER: they would all still hold if nothing consulted `_is_wrong_arch`
    # on the Windows path at all. The consequence a Windows user feels is
    # whether the download URL comes back.
    #
    # `arch=""` cannot be injected as a parameter — `arch or true_machine_arch()`
    # treats it as "not supplied" — so the unknown has to arrive the way it would
    # in the wild, out of platform.machine(). That is the exact witness #196
    # asked for: system="Windows", machine="".
    windows_release = {
        "assets": [
            {
                "name": "BetterFlow-Windows-x86_64-Update.zip",
                "browser_download_url": "https://x/win.zip",
            }
        ]
    }
    with patch("platform.machine", lambda: ""):
        assert _find_platform_asset(windows_release, system="Windows") == "https://x/win.zip"


# --- the tray must not claim an architecture nobody established ------------


def test_the_tray_row_says_it_could_not_determine():
    _exhaust_the_budget()
    label = arch_menu_label(system="Darwin", machine=X86_64, translated=False)

    assert "could not determine" in label
    # It must NOT fall through to the Intel wording, which is the pre-fix
    # rendering and reads as an established fact.
    assert "Intel (" not in label
    # The process arch is still worth showing — it is what a support
    # conversation starts from.
    assert X86_64 in label


def test_the_tray_row_is_unchanged_when_the_probe_resolved():
    with patch.object(ma, "_read_proc_translated", lambda: ProbeResult(None, conclusive=True)):
        ma._read_proc_translated_cached()

    assert arch_menu_label(system="Darwin", machine=X86_64, translated=False) == (
        "Architecture: Intel (x86_64)"
    )
    assert arch_menu_label(system="Darwin", machine=ARM64, translated=False) == (
        "Architecture: Apple Silicon (native)"
    )
