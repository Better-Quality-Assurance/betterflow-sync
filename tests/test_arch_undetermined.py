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

import errno
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import src.update_handler as uh
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
    with patch.object(ma, "_read_proc_translated", lambda: ProbeResult(None, determined=False, conclusive=False)), \
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
    with patch.object(ma, "_read_proc_translated", lambda: ProbeResult(None, determined=False, conclusive=False)):
        ma._read_proc_translated_cached()  # one transient failure, budget intact

    assert ma.probe_settled_unresolved() is False
    assert true_machine_arch(system="Darwin", machine=X86_64) == X86_64


def test_a_conclusive_answer_is_never_undetermined():
    """The control. A real Intel Mac has no proc_translated key, forever."""
    with patch.object(ma, "_read_proc_translated", lambda: ProbeResult(None, determined=True, conclusive=True)):
        ma._read_proc_translated_cached()

    assert ma.probe_settled_unresolved() is False
    assert true_machine_arch(system="Darwin", machine=X86_64) == X86_64


# --- "stop asking" and "we found out" are different claims -----------------
#
# The first cut of this fix read ProbeResult.conclusive, which is the RETRY
# decision. Several outcomes are conclusive-but-unanswered, and each of them
# reinstated the whole #196 defect through a side door: a confident "x86_64"
# for a machine the kernel never spoke to. One witness per door.


def _settle_by_raising(exc):
    """Drive the real probe to its settled state with `exc` from every fork.

    Goes through `subprocess.run` rather than stubbing `_read_proc_translated`,
    because the classification under test IS `_read_proc_translated`'s. Stubbing
    it would assert the mapping the test itself supplied.
    """
    clock = [0.0]

    def _boom(*a, **k):
        raise exc

    with patch.object(ma.subprocess, "run", _boom), patch.object(ma, "_monotonic", lambda: clock[0]):
        for _ in range(len(ma._RETRY_BACKOFF_SECONDS) + 2):
            ma._read_proc_translated_cached()
            clock[0] += 100000.0


def test_a_fork_refused_with_emfile_is_undetermined_not_intel():
    """EMFILE is congestion, and it used to take the "permanent" branch.

    The old classifier asked `exc.errno in {EAGAIN, ENOMEM}`. A full file-
    descriptor table refuses posix_spawn with EMFILE, not EAGAIN — the same
    overloaded machine, one table along — so it fell through to conclusive,
    memoised as though the hardware had answered, and an Apple Silicon Mac
    running the Intel build was handed the Intel DMG again. The set of ways a
    busy machine can refuse a fork is not enumerable, which is why the module
    now lists the PERMANENT cases instead.
    """
    _settle_by_raising(OSError(errno.EMFILE, "Too many open files"))

    assert ma.probe_settled_unresolved() is True
    assert true_machine_arch(system="Darwin", machine=X86_64) == ""
    with patch("platform.machine", lambda: X86_64):
        assert _find_platform_asset(RELEASE, system="Darwin") is None


def test_a_timed_out_probe_is_undetermined_not_intel():
    """The motivating case, and it was witnessed by NOTHING.

    Every other test reaching this state goes through `_exhaust_the_budget`,
    which stubs `_read_proc_translated` wholesale — so the timeout branch's own
    `determined=False` was supplied by the fixture rather than tested. Flipping
    it to True in the source left the entire suite green while restoring the
    #196 defect verbatim: a Mac that loses all four probes to a boot storm
    reports a confident "x86_64" and is handed the Intel DMG.
    """
    _settle_by_raising(subprocess.TimeoutExpired(cmd="sysctl", timeout=2))

    assert ma.probe_settled_unresolved() is True
    assert true_machine_arch(system="Darwin", machine=X86_64) == ""


def test_a_probe_killed_by_a_signal_is_undetermined_not_intel():
    """jetsam/OOM under the same memory pressure. The hardware never spoke.

    Reaches the classifier by RETURNING a negative returncode rather than
    raising, so it exercises the branch the exception tests cannot.
    """
    clock = [0.0]
    with patch.object(
        ma.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=-9, stdout="", stderr="")
    ), patch.object(ma, "_monotonic", lambda: clock[0]):
        for _ in range(len(ma._RETRY_BACKOFF_SECONDS) + 2):
            ma._read_proc_translated_cached()
            clock[0] += 100000.0

    assert ma.probe_settled_unresolved() is True
    assert true_machine_arch(system="Darwin", machine=X86_64) == ""


def test_a_native_arm64_mac_is_never_undetermined():
    """An arm64 PROCESS proves arm64 HARDWARE — no probe required.

    Rosetta translates x86 onto arm and never the reverse, so the ambiguity this
    module exists to resolve simply does not arise for an arm64 process. Without
    the `machine == X86_64` clause, a native Apple Silicon Mac whose sysctl is
    denied by an EDR policy is told "could not determine (process: arm64)" and
    offered no build at all — worse than the behaviour this branch inherited, on
    a machine whose architecture was never in doubt.
    """
    _settle_by_raising(PermissionError(errno.EACCES, "Operation not permitted"))
    assert ma.probe_settled_unresolved() is True

    assert true_machine_arch(system="Darwin", machine=ARM64) == ARM64
    assert arch_menu_label(system="Darwin", machine=ARM64, translated=False) == (
        "Architecture: Apple Silicon (native)"
    )
    with patch("platform.machine", lambda: ARM64):
        assert _find_platform_asset(RELEASE, system="Darwin") == "https://x/arm64.dmg"

    # ...while the genuinely ambiguous x86_64 process on the same denied probe
    # still withholds. Same memo, opposite answer — that is the whole point.
    assert true_machine_arch(system="Darwin", machine=X86_64) == ""


def test_a_sandbox_denial_is_undetermined_while_staying_memoised():
    """Both halves, because they pull in opposite directions.

    A denied call will not lift mid-session, so the retry policy must still
    stop after ONE fork — retrying costs a fork per menu rebuild and buys
    nothing. But it is not an answer about the hardware, so the arch must come
    back undetermined. `conclusive=True, determined=False` is exactly this
    cell, and collapsing the two fields loses it.
    """
    calls = []

    def _denied(*a, **k):
        calls.append(1)
        raise PermissionError(errno.EACCES, "Operation not permitted")

    clock = [0.0]
    with patch.object(ma.subprocess, "run", _denied), patch.object(ma, "_monotonic", lambda: clock[0]):
        for _ in range(5):
            clock[0] += 100000.0
            ma._read_proc_translated_cached()

    assert len(calls) == 1, "a permanent denial must still be memoised on the first probe"
    assert ma.probe_settled_unresolved() is True
    assert true_machine_arch(system="Darwin", machine=X86_64) == ""


def test_a_missing_sysctl_is_undetermined_while_staying_memoised():
    """The same cell reached the other way, and the one that guards Windows.

    `FileNotFoundError("sysctl not found")` carries no errno at all, so any
    errno-keyed classifier reads `exc.errno is None`, calls it congestion, and
    reinstates the fork-per-menu-rebuild regression that once broke the
    windows-latest leg of a tagged release. Matching on the exception TYPE is
    what makes this stable.
    """
    calls = []

    def _absent(*a, **k):
        calls.append(1)
        raise FileNotFoundError("sysctl not found")

    clock = [0.0]
    with patch.object(ma.subprocess, "run", _absent), patch.object(ma, "_monotonic", lambda: clock[0]):
        for _ in range(5):
            clock[0] += 100000.0
            ma._read_proc_translated_cached()

    assert len(calls) == 1, "a permanently absent sysctl must be memoised on the first probe"
    assert ma.probe_settled_unresolved() is True


def test_a_real_intel_mac_is_determined_by_the_answer_it_gets():
    """The control for all three above, through the same real code path.

    sysctl exits non-zero because the key does not exist — and that IS the
    answer. If this ever reported undetermined, every genuine Intel Mac would
    stop being offered its own build, which is the failure mode that makes
    "withhold when unsure" dangerous rather than free.
    """
    clock = [0.0]
    with patch.object(
        ma.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="invalid")
    ), patch.object(ma, "_monotonic", lambda: clock[0]):
        ma._read_proc_translated_cached()

    assert ma.probe_settled_unresolved() is False
    assert true_machine_arch(system="Darwin", machine=X86_64) == X86_64
    with patch("platform.machine", lambda: X86_64):
        assert _find_platform_asset(RELEASE, system="Darwin") == "https://x/x86_64.dmg"


def test_an_injected_reader_is_never_answered_from_this_process_memo():
    """The `sysctl_reader is None` guard, which nothing witnessed.

    An injected reader REPLACES the probe, so this process's memo describes a
    different one and must not be mixed in. Injecting `translated=` is the
    opposite case — no reader ran, so the memo is the only record of whether
    this process got an answer — and the tray depends on exactly that, which
    `test_the_tray_row_says_it_could_not_determine` pins.
    """
    _exhaust_the_budget()
    assert ma.probe_settled_unresolved() is True

    # Same settled-unresolved memo, but the caller brought its own probe.
    assert true_machine_arch(system="Darwin", machine=X86_64, sysctl_reader=lambda: "0") == X86_64
    assert true_machine_arch(system="Darwin", machine=X86_64, sysctl_reader=lambda: "1") == ARM64


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
    with patch.object(ma, "_read_proc_translated", lambda: ProbeResult("1", determined=True, conclusive=True)):
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


# --- and ops has to be able to see it --------------------------------------


def _update_handler():
    """Hand-built like the other UpdateHandler suites — see test_update_handler.py."""
    import threading

    from src.update_handler import UpdateHandler

    h = UpdateHandler.__new__(UpdateHandler)
    h.tray = MagicMock()
    h.config = MagicMock()
    h.config.auto_install_updates = True
    h.coordinator = MagicMock()
    h._version = "1.5.123"
    h._notified_version = None
    h._managed_warned_version = None
    h._arch_undetermined_version = None
    h._staged_version = None
    h._staged_lock = threading.Lock()
    return h


def test_a_mac_left_with_no_asset_by_an_undetermined_arch_is_reported_once():
    """Withholding correctly is not the same as withholding visibly.

    This repo publishes ONLY arm64 and x86_64 DMGs — no universal DMG, no macOS
    ZIP — so an undetermined Mac gets no asset for EVERY release until the
    process restarts. The user still sees the release-page toast; without this,
    ops sees a device quietly stuck on an old version and nothing else.
    """
    _exhaust_the_budget()
    h = _update_handler()

    # `_on_update_available` asks `true_machine_arch()` with no override, so it
    # reads the REAL platform. Without these patches this test passes only on a
    # Mac: on the ubuntu runner that gates every PR here, the arch comes back
    # truthy, the report never fires, and this assertion fails. Patch the
    # platform rather than stubbing `true_machine_arch`, so the memo built by
    # `_exhaust_the_budget` is still the thing under test.
    with patch("platform.system", lambda: "Darwin"), patch("platform.machine", lambda: X86_64), \
         patch.object(h, "_report_arch_undetermined") as report:
        h._on_update_available("1.5.124", "http://rel", asset_url=None)
        h._on_update_available("1.5.124", "http://rel", asset_url=None)  # 30-min re-check

    report.assert_called_once()


def test_no_asset_for_an_ordinary_reason_is_not_reported_as_undetermined():
    """The control, and the one that keeps the fingerprint meaningful.

    A release with nothing this platform can use also arrives here with
    `asset_url=None`. Reporting that as an architecture failure would make the
    signal fire on healthy machines, and the error ingest aggregates by
    fingerprint alone — so a noisy fingerprint is not a smaller signal, it is
    no signal.
    """
    with patch.object(ma, "_read_proc_translated", lambda: ProbeResult(None, determined=True, conclusive=True)):
        ma._read_proc_translated_cached()
    h = _update_handler()

    # Darwin pinned for the same reason as above — otherwise this control passes
    # on the CI runner whatever the code does, which is the shape of a test that
    # proves nothing.
    with patch("platform.system", lambda: "Darwin"), patch("platform.machine", lambda: X86_64), \
         patch.object(h, "_report_arch_undetermined") as report:
        h._on_update_available("1.5.124", "http://rel", asset_url=None)

    report.assert_not_called()


def test_the_ops_report_carries_its_own_fingerprint():
    """The body itself, unpatched — everything else mocks this method out.

    The error ingest aggregates by fingerprint ALONE (context and message are
    overwritten newest-wins and the digest reads neither), so the fingerprint
    is the entire signal. Reusing the managed-install one would silently merge
    two unrelated ops conditions into a single count, and every other test here
    would stay green.
    """
    h = _update_handler()
    reporter = MagicMock()
    h.coordinator.error_reporter = reporter

    h._report_arch_undetermined("1.5.124")

    reporter.capture.assert_called_once()
    kwargs = reporter.capture.call_args.kwargs
    assert kwargs["fingerprint"] == "update-arch-undetermined"
    assert kwargs["level"] == "warning"


def test_the_ops_report_never_raises_into_the_update_path():
    """A broken reporter must not take the updater down with it."""
    h = _update_handler()
    h.coordinator.error_reporter = MagicMock()
    h.coordinator.error_reporter.capture.side_effect = RuntimeError("ingest down")

    h._report_arch_undetermined("1.5.124")  # must not raise


# --- notifying on an assumption is free; INSTALLING on one is not ----------


def test_no_self_install_while_the_probe_is_still_retrying():
    """The mid-backoff window, which is ~36 minutes long and auto-applies.

    `true_machine_arch` deliberately still reports the process architecture
    here — the retry schedule exists to cover a briefly-busy Mac, and issue #196
    asks for exactly that. But the launch check arrives with `apply_now=True`
    and `auto_install_updates` defaults True, so an Apple Silicon Mac that lost
    its probe to a login boot storm would download and APPLY the Intel DMG,
    re-pinning itself to the build this module exists to get it off.

    The user is still notified — only the self-install is deferred.
    """
    # One transient failure: budget intact, so the answer is provisional.
    with patch.object(ma, "_read_proc_translated", lambda: ProbeResult(None, determined=False, conclusive=False)):
        ma._read_proc_translated_cached()
    assert ma.probe_settled_unresolved() is False, "precondition: mid-backoff, not settled"

    h = _update_handler()
    with patch("platform.system", lambda: "Darwin"), patch("platform.machine", lambda: X86_64), \
         patch.object(h, "_stage_and_maybe_apply") as stage, \
         patch.object(uh, "send_notification") as notify:
        h._on_update_available("1.5.124", "http://rel", asset_url="http://x/x86_64.dmg", apply_now=True)

    stage.assert_not_called()
    notify.assert_called_once()  # told, just not installed for them


def test_the_self_install_resumes_once_the_probe_settles():
    """The permissive direction — deferring forever would also pass the above."""
    with patch.object(ma, "_read_proc_translated", lambda: ProbeResult(None, determined=True, conclusive=True)):
        ma._read_proc_translated_cached()

    h = _update_handler()
    with patch("platform.system", lambda: "Darwin"), patch("platform.machine", lambda: X86_64), \
         patch.object(h, "_stage_and_maybe_apply") as stage, \
         patch.object(uh, "send_notification"):
        h._on_update_available("1.5.124", "http://rel", asset_url="http://x/x86_64.dmg", apply_now=True)

    stage.assert_called_once()


def test_an_unprobed_mac_asks_rather_than_deferring_forever():
    """"Nobody has looked yet" must not read as "still retrying".

    The first cut of this predicate read the memo passively, so a Mac arriving
    here before anything had probed saw `_probed is False`, called the answer
    provisional, and deferred — with nothing scheduled to ever change that. It
    also broke `test_replaceable_install_proceeds_to_stage`, an existing test of
    the ordinary healthy-Mac staging path, which is how it was caught.
    """
    ma.reset_cache_for_tests()
    calls = []

    def _answered(*a, **k):
        calls.append(1)
        return SimpleNamespace(returncode=1, stdout="", stderr="invalid")  # real Intel Mac

    with patch("platform.system", lambda: "Darwin"), patch.object(ma.subprocess, "run", _answered):
        assert ma.arch_answer_is_provisional() is False

    assert calls, "the predicate must PROBE, not just read a memo somebody else filled"


def test_windows_and_linux_self_installs_are_never_deferred():
    """The predicate MUST be Darwin-gated inside itself.

    Off macOS the probe never runs, so `_probed` stays False for the life of the
    process — an ungated "is it unsettled?" would defer every self-install on
    Windows and Linux permanently.
    """
    for sysname, mach in (("Windows", "AMD64"), ("Linux", X86_64)):
        ma.reset_cache_for_tests()
        h = _update_handler()
        with patch("platform.system", lambda s=sysname: s), patch("platform.machine", lambda m=mach: m), \
             patch.object(h, "_stage_and_maybe_apply") as stage, \
             patch.object(uh, "send_notification"):
            h._on_update_available("1.5.124", "http://rel", asset_url="http://x/app.zip", apply_now=True)

        assert ma.arch_answer_is_provisional(system=sysname) is False, sysname
        stage.assert_called_once()


def test_the_tray_row_is_unchanged_when_the_probe_resolved():
    with patch.object(ma, "_read_proc_translated", lambda: ProbeResult(None, determined=True, conclusive=True)):
        ma._read_proc_translated_cached()

    assert arch_menu_label(system="Darwin", machine=X86_64, translated=False) == (
        "Architecture: Intel (x86_64)"
    )
    assert arch_menu_label(system="Darwin", machine=ARM64, translated=False) == (
        "Architecture: Apple Silicon (native)"
    )
