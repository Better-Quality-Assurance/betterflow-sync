"""A tracker binary that downloaded fine but cannot RUN must be reported.

`tracker_download_failed` was only ever set when the archive failed to arrive.
Laszlo Fabian Raul's device had the archive: the binaries were on disk, verified
and extracted. They just could not execute —

    aw_manager - ERROR - Failed to start bf-data-service:
        [Errno 86] Bad CPU type in executable: '/Users/fabian/.../trackers/darwin/...'

21 times on 2026-07-23 alone, retried every 60s, and both 07-22 and 07-23
recorded zero seconds. Neither health flag was set, so the heartbeat reported a
perfectly healthy device for two solid days.

macOS trackers are x86_64-only (upstream ActivityWatch v0.13.2 publishes no
arm64 asset at all — arm64 first appears in the v0.14.0b* betas), so on Apple
Silicon they need Rosetta 2. Without it this is permanent, not transient, and
must not be retried in silence.
"""

import errno
from unittest.mock import MagicMock, patch

from src.aw_manager import AWManager

# errno.EBADARCH only exists on macOS, and CI runs the suite on ubuntu — read it
# through the same fallback the agent uses so these tests don't AttributeError
# on the platforms that never see the failure.
EBADARCH = getattr(errno, "EBADARCH", 86)


def _mgr() -> AWManager:
    mgr = AWManager(aw_port=5600)
    mgr.tracker_download_failed = False
    return mgr


def test_bad_cpu_type_marks_the_trackers_unusable():
    mgr = _mgr()
    exc = OSError(EBADARCH, "Bad CPU type in executable")

    with patch("src.aw_manager._resolve_binary_path", return_value="/fake/bf-data-service"), \
         patch("src.aw_manager.subprocess.Popen", side_effect=exc), \
         patch.object(mgr, "_dispatch_download_failure_report") as dispatch:
        started = mgr._start_component("bf-data-service", "/fake")

    assert started is False
    assert mgr.tracker_download_failed is True, (
        "a binary that cannot execute is as unusable as one that never arrived; "
        "if this stays False the device reports itself healthy while recording "
        "nothing, which is exactly what happened for two days"
    )
    # Only the person at the keyboard can install Rosetta 2, so this has to
    # leave the machine the same way a failed download does — ops ingest plus a
    # toast. A flag on the next heartbeat is still log-only to them.
    dispatch.assert_called_once_with("exec")


def test_exec_format_error_is_treated_the_same():
    # Linux/other reports the same class of failure as ENOEXEC.
    mgr = _mgr()
    exc = OSError(errno.ENOEXEC, "Exec format error")

    with patch("src.aw_manager._resolve_binary_path", return_value="/fake/bf-data-service"), \
         patch("src.aw_manager.subprocess.Popen", side_effect=exc), \
         patch.object(mgr, "_dispatch_download_failure_report") as dispatch:
        mgr._start_component("bf-data-service", "/fake")

    assert mgr.tracker_download_failed is True
    dispatch.assert_called_once_with("exec")


def test_a_transient_start_failure_is_NOT_marked_permanent():
    # The distinction is the whole point: a transient failure should keep
    # retrying quietly, an unrunnable binary should be reported. Flagging both
    # would make the signal useless.
    mgr = _mgr()

    with patch("src.aw_manager._resolve_binary_path", return_value="/fake/bf-data-service"), \
         patch("src.aw_manager.subprocess.Popen", side_effect=OSError(errno.EAGAIN, "try again")), \
         patch.object(mgr, "_dispatch_download_failure_report") as dispatch:
        started = mgr._start_component("bf-data-service", "/fake")

    assert started is False
    assert mgr.tracker_download_failed is False
    dispatch.assert_not_called()


def test_the_unusable_flag_reaches_the_health_snapshot():
    # Setting internal state proves nothing on its own — assert the consumer.
    mgr = _mgr()
    exc = OSError(EBADARCH, "Bad CPU type in executable")

    with patch("src.aw_manager._resolve_binary_path", return_value="/fake/bf-data-service"), \
         patch("src.aw_manager.subprocess.Popen", side_effect=exc), \
         patch.object(mgr, "_dispatch_download_failure_report"):
        mgr._start_component("bf-data-service", "/fake")

    snapshot = mgr.health_snapshot()
    assert snapshot["tracker_download_failed"] is True


def test_a_component_that_does_start_unlatches_the_flag():
    # The latch has to clear on EVERY route back to a running tracker, not just
    # _start_locked: the watchdog restarts components directly, so a device that
    # recovers (Rosetta 2 finally installed) would otherwise report "capturing
    # NOTHING" forever and train the ops ingest to ignore the signal.
    mgr = _mgr()
    mgr.tracker_download_failed = True
    mgr._managed_components_unavailable = True

    with patch("src.aw_manager._resolve_binary_path", return_value="/fake/bf-window-tracker"), \
         patch("src.aw_manager.subprocess.Popen", return_value=MagicMock(pid=4242)):
        started = mgr._start_component("bf-window-tracker", "/fake")

    assert started is True
    assert mgr.tracker_download_failed is False
    assert mgr._managed_components_unavailable is False


def test_a_sibling_that_starts_does_not_unlatch_for_an_unrunnable_one():
    # Only some binaries need be unrunnable (one corrupt/foreign-arch file) for
    # that component to be blind forever. If the next component's successful
    # start clears the latch, the device reports itself healthy again — the same
    # false-healthy this whole branch exists to end.
    mgr = _mgr()
    exc = OSError(EBADARCH, "Bad CPU type in executable")

    with patch("src.aw_manager._resolve_binary_path", return_value="/fake/bf-window-tracker"), \
         patch("src.aw_manager.subprocess.Popen", side_effect=exc), \
         patch.object(mgr, "_dispatch_download_failure_report"):
        mgr._start_component("bf-window-tracker", "/fake")

    assert mgr.tracker_download_failed is True

    with patch("src.aw_manager._resolve_binary_path", return_value="/fake/bf-idle-tracker"), \
         patch("src.aw_manager.subprocess.Popen", return_value=MagicMock(pid=4243)):
        assert mgr._start_component("bf-idle-tracker", "/fake") is True

    assert mgr.tracker_download_failed is True
    assert mgr._managed_components_unavailable is True
    assert mgr.health_snapshot()["tracker_download_failed"] is True


def test_an_attached_external_server_is_not_reported_as_a_blackout():
    # Mirror of the download-failure carve-out: with a server we attached to
    # still listening on the port, capture continues from ITS watchers — only
    # our managed ones are unusable. Latching the capture-dead flag or toasting
    # here would report a blackout on a device that is recording fine.
    mgr = _mgr()
    mgr._using_external = True
    exc = OSError(EBADARCH, "Bad CPU type in executable")

    with patch("src.aw_manager._resolve_binary_path", return_value="/fake/bf-window-tracker"), \
         patch("src.aw_manager.subprocess.Popen", side_effect=exc), \
         patch.object(mgr, "_port_in_use", return_value=True), \
         patch.object(mgr, "_dispatch_download_failure_report") as dispatch:
        started = mgr._start_component("bf-window-tracker", "/fake")

    assert started is False
    assert mgr.tracker_download_failed is False
    assert mgr._managed_components_unavailable is True
    dispatch.assert_not_called()


def test_a_stale_external_flag_still_reports_the_blackout():
    # _using_external is only refreshed on the health tick, so it can outlive
    # the server it describes. Fall back to the live port check: no listener
    # means nothing is capturing and the user must be told.
    mgr = _mgr()
    mgr._using_external = True
    exc = OSError(EBADARCH, "Bad CPU type in executable")

    with patch("src.aw_manager._resolve_binary_path", return_value="/fake/bf-window-tracker"), \
         patch("src.aw_manager.subprocess.Popen", side_effect=exc), \
         patch.object(mgr, "_port_in_use", return_value=False), \
         patch.object(mgr, "_dispatch_download_failure_report") as dispatch:
        mgr._start_component("bf-window-tracker", "/fake")

    assert mgr.tracker_download_failed is True
    dispatch.assert_called_once_with("exec")
