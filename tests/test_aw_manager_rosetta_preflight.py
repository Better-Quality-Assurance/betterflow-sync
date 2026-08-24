"""Apple Silicon without Rosetta 2 must be detected, reported and not retried.

RELEASE_ASSETS points at x86_64 on macOS because upstream ActivityWatch
publishes nothing else: v0.13.2 ships only `-macos-x86_64.zip`, and an arm64
asset first appears in the v0.14.0b* betas. So on Apple Silicon the trackers
need Rosetta 2, and without it every spawn raises

    [Errno 86] Bad CPU type in executable

Laszlo Fabian Raul's device did that 21 times on 2026-07-23 alone, at 60-second
intervals, recording zero seconds on both 07-22 and 07-23 while reporting itself
healthy. No amount of retrying installs Rosetta, so the loop was pure noise.

**Both halves of the gate must be supplied, not just the host half.** Since #216
the start path gates on `_rosetta_required()`, the conjunction of "the bundled
binary needs Rosetta" and "the host lacks Rosetta". A test that patches only
`_rosetta_missing` no longer blocks anything: the binary half reads the Mach-O
header of the tracker on disk, and on the Linux CI runner those are ELF, so it
answers "could not tell" and correctly declines to block. That is not a
hypothetical — it reddened seven tests here that were green on macOS, where the
worktree simply had no trackers on disk at all, and it sent the start path on to
a real 187 MB download inside a unit test.
"""

import contextlib
import errno
import logging
import os
import platform
import struct
import subprocess
import tempfile
import urllib.error
from unittest.mock import patch

from src.aw_manager import ALL_COMPONENTS, ROSETTA_REPROBE_INTERVAL, AWManager

# Mach-O header bytes, written for real rather than mocked. `_rosetta_required`
# reads the header of the tracker it is about to spawn, so a test that patched
# that read would be asserting against its own answer (the whole reason the old
# preflight was wrong: it asked the host, not the binary).
_MH_MAGIC_64 = 0xFEEDFACF
_CPU_TYPE_X86_64 = 0x01000007
_CPU_TYPE_ARM64 = 0x0100000C


def _macho_header(cputype: int) -> bytes:
    return struct.pack("<II", _MH_MAGIC_64, cputype)


def _tracker_tree(cputype: int) -> str:
    """A tracker directory whose binaries really carry the given architecture.

    Returns the directory; the caller points `_get_binaries_dir` at it. The
    files are 8-byte headers, which is all `macho_arches` reads and all that is
    needed to answer "can this machine execute this".
    """
    root = tempfile.mkdtemp(prefix="bf-trackers-")
    # ".exe" on Windows because the gate resolves each component through
    # `_resolve_binary_path`, which appends it there — a fixture named without
    # it resolves to nothing on the Windows CI leg.
    ext = ".exe" if platform.system() == "Windows" else ""
    for name in ALL_COMPONENTS:
        comp = os.path.join(root, name)
        os.makedirs(comp, exist_ok=True)
        with open(os.path.join(comp, name + ext), "wb") as fh:
            fh.write(_macho_header(cputype))
    return root


# The stale-install world the Rosetta path now describes: x86_64 trackers
# already sitting at the persistent path on an arm64 Mac. A FRESH Apple Silicon
# install no longer reaches this path at all — it downloads the arm64 archive
# and runs natively — which is exactly the behaviour change #216 asked for.
_X86_TRACKERS = _tracker_tree(_CPU_TYPE_X86_64)
_ARM64_TRACKERS = _tracker_tree(_CPU_TYPE_ARM64)


class _Clock:
    """A monotonic clock the test drives, so the re-probe interval is exercised
    rather than waited out. Real time would make these tests either slow or
    dependent on how fast the runner is."""

    def __init__(self):
        self.t = 10_000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _mgr() -> AWManager:
    mgr = AWManager(aw_port=5600)
    mgr.tracker_download_failed = False
    mgr._rosetta_missing_cached = None
    mgr._rosetta_notified = False
    return mgr


def _on_apple_silicon(rosetta_ok: bool):
    """Patch the platform probes so the test does not depend on the host Mac."""
    completed = subprocess.CompletedProcess(args=[], returncode=0 if rosetta_ok else 1)
    return (
        patch("src.aw_manager.sys.platform", "darwin"),
        patch("src.aw_manager.platform.machine", return_value="arm64"),
        patch("src.aw_manager.subprocess.run", return_value=completed),
    )


def test_apple_silicon_without_rosetta_is_detected():
    mgr = _mgr()
    p1, p2, p3 = _on_apple_silicon(rosetta_ok=False)
    with p1, p2, p3:
        assert mgr._rosetta_missing() is True


def test_apple_silicon_WITH_rosetta_is_not_flagged():
    # The negative control. If this also returned True the probe would be
    # measuring nothing, and it would refuse to start trackers on every Mac.
    mgr = _mgr()
    p1, p2, p3 = _on_apple_silicon(rosetta_ok=True)
    with p1, p2, p3:
        assert mgr._rosetta_missing() is False


def test_intel_macs_are_never_flagged():
    mgr = _mgr()
    with patch("src.aw_manager.sys.platform", "darwin"), \
         patch("src.aw_manager.platform.machine", return_value="x86_64"):
        assert mgr._rosetta_missing() is False


def test_windows_and_linux_are_never_flagged():
    for plat in ("win32", "linux"):
        mgr = _mgr()
        with patch("src.aw_manager.sys.platform", plat):
            assert mgr._rosetta_missing() is False, plat


def test_a_broken_probe_does_not_block_a_healthy_mac():
    # Fail towards attempting the start. Claiming "Rosetta missing" because the
    # probe itself blew up would stop capture on a machine that is fine — the
    # EBADARCH handler in _start_component still catches the real thing.
    mgr = _mgr()
    with patch("src.aw_manager.sys.platform", "darwin"), \
         patch("src.aw_manager.platform.machine", return_value="arm64"), \
         patch("src.aw_manager.subprocess.run", side_effect=OSError("no /usr/bin/arch")):
        assert mgr._rosetta_missing() is False


def test_the_probe_is_cached_not_run_every_start():
    # This sits on the 60s start path; Rosetta cannot appear without a reboot.
    mgr = _mgr()
    completed = subprocess.CompletedProcess(args=[], returncode=1)
    with patch("src.aw_manager.sys.platform", "darwin"), \
         patch("src.aw_manager.platform.machine", return_value="arm64"), \
         patch("src.aw_manager.subprocess.run", return_value=completed) as run:
        mgr._rosetta_missing()
        mgr._rosetta_missing()
        mgr._rosetta_missing()

    assert run.call_count == 1


def test_start_refuses_and_reports_instead_of_spawning():
    # The consumer. Detecting it internally proves nothing if the start path
    # still spawns 21 times and the backend still sees a healthy device.
    mgr = _mgr()
    # The notification is patched out too: `src.aw_manager.subprocess` IS the
    # shared stdlib module, so patching Popen on it also intercepts the
    # notifier's own spawn (notify-send on Linux). Without this the assertion
    # below reads that as "a tracker was started" — green on macOS, red on the
    # Linux CI runner.
    #
    # _port_in_use is pinned because _start_locked now consults it BEFORE the
    # Rosetta branch, so an unpinned probe makes this test answer differently
    # depending on whether the developer happens to have ActivityWatch running.
    # It found this the honest way: on a laptop with a live server on 5600 the
    # unpinned version returned True and reddened here.
    with patch.object(AWManager, "_rosetta_missing", return_value=True), \
         patch.object(AWManager, "_bundled_trackers_need_rosetta", return_value=True), \
         patch.object(AWManager, "_notify_rosetta_required_once"), \
         patch.object(AWManager, "_port_in_use", return_value=False), \
         patch("src.aw_manager.subprocess.Popen") as popen:
        started = mgr._start_locked()

    assert started is False
    popen.assert_not_called()
    assert mgr.tracker_download_failed is True
    assert mgr._managed_components_unavailable is True
    assert mgr.health_snapshot()["tracker_download_failed"] is True


def test_an_external_server_is_attached_to_rather_than_declared_dead():
    """The other end of the branch above, and the reason it has two.

    Rosetta missing means OUR binaries cannot execute. It does not mean this
    device records nothing: a user who hit the wall and installed a native arm64
    ActivityWatch themselves has a live server on the port. Before the port
    probe moved above the Rosetta branch, that machine latched
    tracker_download_failed and never reached the external-attach path — so it
    reported capturing nothing while capturing, and once capture_blocked_remedy
    began reading that latch it would have been told to install Rosetta over
    some unrelated outage.

    managed_components_unavailable stays True either way: our watchers really
    are unavailable, and the backend needs to know the device cannot self-heal.

    **The fixture, not the assertion, was the round-2 defect.** This test used
    to pin `_port_in_use=True` alone and assert "no remedy" under the sentence
    *this person is recording* — but a TCP connect cannot establish that
    premise, so the assertion also certified a device whose port is held by
    something dead. `_server_responding` is now pinned as well, which is what
    makes the docstring true of the device the fixture actually builds; the
    case it was wrongly covering has its own test below.

    **Second defect in the same fixture, found by running the new sibling.**
    Patching the `_rosetta_missing` METHOD skips the memo write the real probe
    performs, so `_rosetta_missing_cached` stayed None — and
    `capture_blocked_remedy()` returns None on a None memo by its own first
    line. The "THE assertion" below was therefore satisfied by an un-probed
    memo whatever `tracker_download_failed` said, i.e. it passed identically
    against the bug, the fix, and no implementation at all. The memo is now set
    to what the real probe would have written, which is what makes the
    `tracker_download_failed` half of the condition the thing under test.
    """
    mgr = _mgr()
    # What `_rosetta_missing()` writes on the real path; the patch below
    # replaces the method, so the fixture owes the memo.
    mgr._rosetta_missing_cached = True
    with patch.object(AWManager, "_rosetta_missing", return_value=True), \
         patch.object(AWManager, "_bundled_trackers_need_rosetta", return_value=True), \
         patch.object(AWManager, "_notify_rosetta_required_once") as notify, \
         patch.object(AWManager, "_port_in_use", return_value=True), \
         patch.object(AWManager, "_server_responding", return_value=True), \
         patch("src.aw_manager.subprocess.Popen") as popen:
        started = mgr._start_locked()

    assert started is True
    popen.assert_not_called()
    assert mgr._using_external is True
    # THE assertion: not capture-dead, so no remedy is owed.
    assert mgr.tracker_download_failed is False
    assert mgr.capture_blocked_remedy() is None
    # ...and no toast either. This person is recording.
    notify.assert_not_called()
    # Still un-self-healing, and the fleet must still see that.
    assert mgr._managed_components_unavailable is True


def test_a_held_port_that_answers_nothing_is_not_a_recording_device():
    """The fixture no test could express, and the regression it hid.

    Round 2 hoisted the port probe above the Rosetta branch and gave the
    external-server carve-out to ANY TCP listener. `_port_in_use()` is a bare
    connect, so on a Rosetta-blocked Mac holding a hung `bf-data-service` —
    port 5600 held, HTTP dead, which is the state `force_restart()`'s own
    docstring exists to describe — `_start_locked` returned True, left
    `tracker_download_failed` False, and `capture_blocked_remedy()` answered
    None. The tray then fell back to "ActivityWatch not responding": the
    literal #188 exists to remove, restored on #188's own device.

    Un-reapable, too: `_reap_orphan_processes` is path-scoped to binaries_dir
    and our binaries have never executed here, so nothing clears it and the
    contradictory steady state (started=True, /info dead, fleet sees healthy)
    persists for the life of the process.

    Both booleans are pinned deliberately — production computes them, so an
    unpinned probe would answer differently on a developer laptop that happens
    to run ActivityWatch. The memo is supplied for the same reason: patching
    the `_rosetta_missing` METHOD skips the write the real probe performs, and
    `capture_blocked_remedy()` short-circuits on a None memo — which is what
    made the sibling above pass against the regression.
    """
    mgr = _mgr()
    mgr._rosetta_missing_cached = True
    with patch.object(AWManager, "_rosetta_missing", return_value=True), \
         patch.object(AWManager, "_bundled_trackers_need_rosetta", return_value=True), \
         patch.object(AWManager, "_notify_rosetta_required_once") as notify, \
         patch.object(AWManager, "_port_in_use", return_value=True), \
         patch.object(AWManager, "_server_responding", return_value=False), \
         patch("src.aw_manager.subprocess.Popen") as popen:
        started = mgr._start_locked()

    assert started is False
    popen.assert_not_called()
    # Never claim an attachment to something that does not answer.
    assert mgr._using_external is False
    # THE assertion: capture IS dead here, so the remedy is owed and the fleet
    # can see the device.
    assert mgr.tracker_download_failed is True
    remedy = mgr.capture_blocked_remedy()
    assert remedy is not None and "Rosetta" in remedy, remedy
    notify.assert_called_once()
    assert mgr._managed_components_unavailable is True


def test_a_transient_info_failure_does_not_latch_for_the_life_of_the_process():
    """The cost of the gate above, and the reason the attach branch CLEARS
    rather than merely declining to set.

    /info can time out on a healthy server — a busy laptop, a 2s budget. That
    single cycle latches `tracker_download_failed`, and the Rosetta branch
    returns before the "binaries resolved" clear forty lines down, so on a
    Rosetta-missing Mac nothing else ever unsets it. The device would attach
    externally and record perfectly while showing a permanent "install
    Rosetta" and reporting itself capture-dead to the fleet: round 1's
    Important arriving through a blip instead of a config.

    Walks three real cycles with one blip in them. Found this way rather than
    by reading, which is why the walk is the test.
    """
    mgr = _mgr()
    mgr._rosetta_missing_cached = True
    answers = iter([False, True, True])

    with patch.object(AWManager, "_rosetta_missing", return_value=True), \
         patch.object(AWManager, "_bundled_trackers_need_rosetta", return_value=True), \
         patch.object(AWManager, "_notify_rosetta_required_once"), \
         patch.object(AWManager, "_port_in_use", return_value=True), \
         patch.object(AWManager, "_server_responding", side_effect=lambda: next(answers)), \
         patch("src.aw_manager.subprocess.Popen"):
        # Cycle 1: the blip. Correctly read as capture-dead on the evidence
        # available at the time — this half must NOT be softened.
        assert mgr._start_locked() is False
        assert mgr.tracker_download_failed is True
        assert mgr.capture_blocked_remedy() is not None

        # Cycle 2: the server answers. The device is recording.
        assert mgr._start_locked() is True
        assert mgr._using_external is True
        # THE assertion: the latch is cleared, so the surface goes green and
        # the fleet stops seeing a capture-dead device.
        assert mgr.tracker_download_failed is False
        assert mgr.capture_blocked_remedy() is None

        # Cycle 3: and it stays that way.
        assert mgr._start_locked() is True
        assert mgr.capture_blocked_remedy() is None


def test_the_carve_out_asks_over_http_not_over_tcp():
    """Names the mechanism, so a future refactor back to `_port_in_use()` alone
    reddens even if someone rewrites the two tests above.

    Also pins the short-circuit: with nothing on the port there is no reason to
    pay for an HTTP request, and this branch runs on the 60s start path.
    """
    mgr = _mgr()
    with patch.object(AWManager, "_rosetta_missing", return_value=True), \
         patch.object(AWManager, "_bundled_trackers_need_rosetta", return_value=True), \
         patch.object(AWManager, "_notify_rosetta_required_once"), \
         patch.object(AWManager, "_port_in_use", return_value=True), \
         patch.object(AWManager, "_server_responding", return_value=False) as info, \
         patch("src.aw_manager.subprocess.Popen"):
        mgr._start_locked()
    assert info.call_count == 1, "the carve-out did not consult /api/0/info"

    mgr = _mgr()
    with patch.object(AWManager, "_rosetta_missing", return_value=True), \
         patch.object(AWManager, "_bundled_trackers_need_rosetta", return_value=True), \
         patch.object(AWManager, "_notify_rosetta_required_once"), \
         patch.object(AWManager, "_port_in_use", return_value=False), \
         patch.object(AWManager, "_server_responding", return_value=True) as info, \
         patch("src.aw_manager.subprocess.Popen"):
        mgr._start_locked()
    assert info.call_count == 0, "asked over HTTP with nothing on the port"


def test_server_responding_is_the_http_ask_wait_for_server_polls():
    """One implementation, two callers. `_wait_for_server` used to inline this
    request; a second spelling is how the two drift into disagreeing about what
    "the server is up" means.

    Drives the REAL `_server_responding` against a patched urlopen — the
    positive AND the negative, because a probe that answers False for every
    input would satisfy the regression test above while measuring nothing.
    """
    mgr = _mgr()

    with patch("src.aw_manager.urllib.request.urlopen") as urlopen:
        assert mgr._server_responding() is True
    assert urlopen.call_args[0][0] == "http://localhost:5600/api/0/info"
    assert urlopen.call_args[1]["timeout"] == 2

    with patch("src.aw_manager.urllib.request.urlopen",
               side_effect=OSError("connection reset")):
        assert mgr._server_responding() is False

    # A held-but-dead socket answers the TCP connect and refuses the request;
    # URLError is what urlopen raises there and it must not escape.
    with patch("src.aw_manager.urllib.request.urlopen",
               side_effect=urllib.error.URLError("timed out")):
        assert mgr._server_responding() is False

    # And _wait_for_server routes through it rather than asking again itself.
    with patch.object(AWManager, "_server_responding", return_value=True) as ok:
        assert mgr._wait_for_server() is True
    assert ok.call_count == 1


def test_the_user_is_told_once_not_every_cycle():
    mgr = _mgr()
    with patch.object(AWManager, "_rosetta_missing", return_value=True), \
         patch.object(AWManager, "_bundled_trackers_need_rosetta", return_value=True), \
         patch.object(AWManager, "_port_in_use", return_value=False), \
         patch("src.notifications.send_notification") as notify:
        mgr._start_locked()
        mgr._start_locked()
        mgr._start_locked()

    assert notify.call_count == 1
    assert "rosetta" in notify.call_args[0][1].lower()


def test_ebadarch_still_caught_if_the_preflight_is_bypassed():
    # Belt and braces: the preflight is an optimisation over the real handler,
    # not a replacement for it. A Mac that somehow gets past it must still be
    # reported rather than looping.
    mgr = _mgr()
    exc = OSError(getattr(errno, "EBADARCH", 86), "Bad CPU type in executable")
    with patch("src.aw_manager._resolve_binary_path", return_value="/fake/bf-data-service"), \
         patch("src.aw_manager.subprocess.Popen", side_effect=exc):
        assert mgr._start_component("bf-data-service", "/fake") is False

    assert mgr.tracker_download_failed is True


def _no_tracker_stack(rosetta_ok):
    """Apple Silicon, a chosen Rosetta answer, and NOTHING that touches the
    network, the process table or the port.

    ``_download_aw_binaries`` is the one that matters and is easy to miss: with
    no binaries on disk ``_start_locked`` falls through to the auto-download,
    and an unpatched run fetches ~100 MB of upstream release archives from a
    unit test. Found by running it — the first version of the test below hung
    until the harness killed it at two minutes.

    ``rosetta_ok`` may be a callable so a test can flip the answer mid-run,
    which is the whole point of the re-probe.
    """
    def probe(*_a, **_k):
        ok = rosetta_ok() if callable(rosetta_ok) else rosetta_ok
        return subprocess.CompletedProcess(args=[], returncode=0 if ok else 1)

    return (
        patch("src.aw_manager.sys.platform", "darwin"),
        patch("src.aw_manager.platform.machine", return_value="arm64"),
        patch("src.aw_manager.subprocess.run", side_effect=probe),
        patch("src.aw_manager.subprocess.Popen"),
        patch("src.aw_manager._download_aw_binaries", return_value=False),
        patch.object(AWManager, "_notify_rosetta_required_once"),
        patch.object(AWManager, "_dispatch_download_failure_report"),
        # x86_64 trackers ON DISK. Before #216 this fixture returned None and
        # the Rosetta branch was still reached, because the gate asked only
        # whether the HOST had Rosetta. It now asks what the BINARY needs, so a
        # fixture with no binaries cannot reach the branch these tests are
        # about — see the precondition assertion in _assert_rosetta_path_live.
        patch.object(AWManager, "_get_binaries_dir", return_value=_X86_TRACKERS),
        patch.object(AWManager, "_port_in_use", return_value=False),
        patch.object(AWManager, "_stop_locked"),
    )


def _arch_probe_calls(run_mock) -> int:
    """How many times the ROSETTA PROBE forked — not every subprocess.

    `subprocess.run` is also used by `_reap_orphan_processes` (one `pgrep` per
    component), and that path became reachable in these fixtures once
    `_get_binaries_dir` returned a real directory. Counting the mock wholesale
    therefore measured "arch probes + 3 pgreps per escalation" while claiming to
    measure re-probes. Filtering on the probe's own argv keeps the assertion
    pointed at the thing the rate limit governs.
    """
    return sum(
        1
        for call in run_mock.call_args_list
        if call.args and call.args[0] and call.args[0][0] == "/usr/bin/arch"
    )


def _assert_rosetta_path_live(mgr):
    """Precondition: this fixture can actually reach the Rosetta branch.

    Without this, every negative assertion below ("no second log line", "the
    remedy persists") is satisfied just as well by a fixture that never enters
    the branch at all — the phantom that made these tests need updating in the
    first place.
    """
    assert mgr._bundled_trackers_need_rosetta(_X86_TRACKERS) is True, (
        "fixture precondition: the trackers on disk must be x86-only, or the "
        "Rosetta branch is unreachable and these assertions prove nothing"
    )


def test_installing_rosetta_is_noticed_without_an_agent_restart():
    """The user's "did my fix work?" loop has to be able to close.

    The memo is written once per process. Cleared nowhere, a user who read the
    tray, ran `softwareupdate --install-rosetta` and watched it finish kept
    seeing "Not recording — Rosetta 2 required" until they thought to quit and
    relaunch — and no surface asked them to. tray.py's capture_permissions_row()
    already states that rule for its own row: for a surface whose whole job is
    "did my fix work?", still saying blocked afterwards is the dead end.

    force_restart is where a device in this state actually arrives: capture is
    gone, so the unreachable watchdog escalates and calls it every cycle. This
    drives that real sequence — blocked, remedy owed; user installs Rosetta;
    next escalation — and asserts the remedy is withdrawn on its own.
    """
    mgr = _mgr()
    installed = {"yet": False}
    clock = _Clock()

    with contextlib.ExitStack() as stack:
        for ctx in _no_tracker_stack(lambda: installed["yet"]):
            stack.enter_context(ctx)
        stack.enter_context(patch("src.aw_manager.time.monotonic", clock))

        # Blocked. The tray owes a remedy.
        assert mgr._start_locked() is False
        assert mgr.capture_blocked_remedy() is not None

        # The user runs the command. Nothing in the agent is restarted.
        installed["yet"] = True

        # The next watchdog escalation force-restarts the stack, as it has been
        # doing every cycle throughout the outage.
        clock.advance(ROSETTA_REPROBE_INTERVAL)
        mgr.force_restart(reason="server unreachable")

        # THE assertion: the surface can go green on its own.
        assert mgr._rosetta_missing_cached is False
        assert mgr.capture_blocked_remedy() is None


def test_the_re_probe_is_rate_limited_off_the_sixty_second_escalation():
    """The cost of the clear above, and the reason it is not unconditional.

    _note_aw_unreachable returns True on EVERY cycle once its 180s grace period
    is up — only a reachable ActivityWatch resets it — so a device capturing
    nothing force-restarts every 60 seconds for the whole outage. An
    unconditional clear therefore forks /usr/bin/arch once a minute on exactly
    the Mac the memo was introduced to protect: the "21 identical failures at
    60-second intervals" pattern the preflight replaced, arriving by another
    door. Found by reading the escalation cadence, not by a failing test.
    """
    mgr = _mgr()
    clock = _Clock()

    with contextlib.ExitStack() as stack:
        for ctx in _no_tracker_stack(False):
            stack.enter_context(ctx)
        run = stack.enter_context(
            patch("src.aw_manager.subprocess.run",
                  return_value=subprocess.CompletedProcess(args=[], returncode=1))
        )
        stack.enter_context(patch("src.aw_manager.time.monotonic", clock))

        assert mgr._start_locked() is False
        _assert_rosetta_path_live(mgr)
        assert _arch_probe_calls(run) == 1

        # Escalations at the real 60s cadence, stopping one short of the
        # interval. Both ends of the boundary are asserted deliberately: the
        # first draft advanced 5 x 60 = exactly 300, landed ON the `>=`, and
        # reddened — a fixture bug that reads exactly like a broken rate limit.
        for _ in range(4):
            clock.advance(60)
            mgr.force_restart(reason="server unreachable")

        assert clock.t - 10_000.0 < ROSETTA_REPROBE_INTERVAL, "fixture is past the boundary"
        assert _arch_probe_calls(run) == 1, (
            "the probe re-forked inside the interval — this is the 60s spam again"
        )

        # Crossing it, the probe DOES re-ask. Without this half the assertion
        # above is satisfied by a memo that never re-probes at all, which is the
        # dead end the clear exists to remove.
        clock.advance(60)
        mgr.force_restart(reason="server unreachable")
        assert _arch_probe_calls(run) == 2


def test_a_persisting_outage_logs_the_cause_once_not_once_per_re_probe(caplog):
    """The other half of the same cost. Re-probing makes _rosetta_missing run
    repeatedly, so it now logs the ANSWER changing rather than the probe
    running — otherwise the identical error line reappears every interval for
    the life of the outage."""
    mgr = _mgr()
    clock = _Clock()

    with contextlib.ExitStack() as stack:
        for ctx in _no_tracker_stack(False):
            stack.enter_context(ctx)
        stack.enter_context(patch("src.aw_manager.time.monotonic", clock))

        with caplog.at_level(logging.ERROR, logger="src.aw_manager"):
            mgr._start_locked()
            for _ in range(3):
                clock.advance(ROSETTA_REPROBE_INTERVAL)
                mgr.force_restart(reason="server unreachable")

    blocked = [r for r in caplog.records if "Rosetta 2 is not installed" in r.getMessage()]
    assert len(blocked) == 1, f"logged the same cause {len(blocked)} times"


def test_a_still_missing_rosetta_survives_the_re_probe():
    """The control for the clear above. Re-probing must not amnesty a machine
    that is still blocked — the remedy has to persist across every escalation
    for as long as the fault does, which is the whole reason the tray (not the
    one-shot toast) is the surface #188 needed."""
    mgr = _mgr()
    clock = _Clock()

    with contextlib.ExitStack() as stack:
        for ctx in _no_tracker_stack(False):
            stack.enter_context(ctx)
        stack.enter_context(patch("src.aw_manager.time.monotonic", clock))

        assert mgr._start_locked() is False
        clock.advance(ROSETTA_REPROBE_INTERVAL)
        mgr.force_restart(reason="server unreachable")

        assert mgr._rosetta_missing_cached is True
        assert mgr.capture_blocked_remedy() is not None


def test_the_re_probe_does_not_reach_the_sixty_second_start_path():
    """force_restart is an escalation, not the sync cycle. The memo exists to
    keep `/usr/bin/arch` off the 60s path, and clearing it there instead of here
    would have re-forked a subprocess every minute on every Mac in the fleet."""
    mgr = _mgr()
    completed = subprocess.CompletedProcess(args=[], returncode=1)

    with patch("src.aw_manager.sys.platform", "darwin"), \
         patch("src.aw_manager.platform.machine", return_value="arm64"), \
         patch("src.aw_manager.subprocess.run", return_value=completed) as run, \
         patch.object(AWManager, "_notify_rosetta_required_once"), \
         patch.object(AWManager, "_port_in_use", return_value=False), \
         patch("src.aw_manager.subprocess.Popen"):
        mgr._start_locked()
        mgr._start_locked()
        mgr._start_locked()

    assert run.call_count == 1
