"""Apple Silicon without Rosetta 2 must be detected, reported and not retried.

RELEASE_ASSETS points at x86_64 on macOS because upstream ActivityWatch
publishes nothing else: v0.13.2 ships only `-macos-x86_64.zip`, and an arm64
asset first appears in the v0.14.0b* betas. So on Apple Silicon the trackers
need Rosetta 2, and without it every spawn raises

    [Errno 86] Bad CPU type in executable

Laszlo Fabian Raul's device did that 21 times on 2026-07-23 alone, at 60-second
intervals, recording zero seconds on both 07-22 and 07-23 while reporting itself
healthy. No amount of retrying installs Rosetta, so the loop was pure noise.
"""

import errno
import subprocess
from unittest.mock import patch

from src.aw_manager import AWManager


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
    with patch.object(AWManager, "_rosetta_missing", return_value=True), \
         patch("src.aw_manager.subprocess.Popen") as popen:
        started = mgr._start_locked()

    assert started is False
    popen.assert_not_called()
    assert mgr.tracker_download_failed is True
    assert mgr._managed_components_unavailable is True
    assert mgr.health_snapshot()["tracker_download_failed"] is True


def test_the_user_is_told_once_not_every_cycle():
    mgr = _mgr()
    with patch.object(AWManager, "_rosetta_missing", return_value=True), \
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
