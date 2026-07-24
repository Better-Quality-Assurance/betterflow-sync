"""The capture-dead latch must survive a _start_locked() tick that resolves
binaries but bails before the watcher loop re-latches the broken component.

`_start_component`'s EBADARCH handler latches `_exec_failed_components` +
`tracker_download_failed` for a binary that cannot execute (Fabian's device,
2026-07-23: recorded zero seconds for two days while the heartbeat reported
healthy). But `_start_locked` unconditionally cleared both flags the moment
`_get_binaries_dir()` resolved — and "binaries on disk" only ever proved they
DOWNLOADED, never that they RUN. So on the next tick the latch was wiped before
anything re-verified execution; if BF_SERVER (or _wait_for_server) then failed
for any reason, the watcher loop that would have re-latched the still-broken
component never ran, and the device reported itself healthy while blind.

The fix gates the clear on the same condition the successful-start path uses:
don't clear while a component is still known-unrunnable.
"""

from unittest.mock import patch

from src.aw_manager import AWManager, BF_SERVER


def _mgr() -> AWManager:
    mgr = AWManager(aw_port=5600)
    mgr._capture_suppressed = False
    return mgr


def test_exec_latch_survives_a_server_start_failure_before_the_watcher_loop():
    mgr = _mgr()
    # A watcher is permanently unrunnable (latched on a prior tick), so the
    # device is capturing nothing and the flags say so.
    mgr._exec_failed_components = {"bf-window-tracker"}
    mgr.tracker_download_failed = True
    mgr._managed_components_unavailable = True

    # This tick: binaries resolve, but the server fails to start, so _start_locked
    # returns before the watcher loop that would re-latch the broken watcher.
    with patch.object(mgr, "_rosetta_missing", return_value=False), \
         patch.object(mgr, "_port_in_use", return_value=False), \
         patch.object(mgr, "_get_binaries_dir", return_value="/fake/trackers"), \
         patch.object(mgr, "_start_component", return_value=False) as start_component:
        result = mgr._start_locked()

    assert result is False
    # BF_SERVER was the thing we failed on; the watcher loop was never reached.
    start_component.assert_called_once_with(BF_SERVER, "/fake/trackers")
    assert mgr.tracker_download_failed is True, (
        "a still-exec-broken device must not report healthy just because its "
        "binaries are on disk — that is the two-day silent blackout this latch "
        "exists to end"
    )
    assert mgr._managed_components_unavailable is True
    assert "bf-window-tracker" in mgr._exec_failed_components


def test_a_clean_device_still_clears_the_download_latch_on_resolve():
    # The frozen-bundle recovery path must keep working: no exec failures
    # outstanding => "binaries resolved" clears a stale download latch even
    # though nothing was downloaded this run. Server start fails here too, so we
    # isolate exactly the clear-on-resolve behaviour (no watcher loop).
    mgr = _mgr()
    mgr._exec_failed_components = set()
    mgr.tracker_download_failed = True
    mgr._managed_components_unavailable = True

    with patch.object(mgr, "_rosetta_missing", return_value=False), \
         patch.object(mgr, "_port_in_use", return_value=False), \
         patch.object(mgr, "_get_binaries_dir", return_value="/fake/trackers"), \
         patch.object(mgr, "_start_component", return_value=False):
        mgr._start_locked()

    assert mgr.tracker_download_failed is False
    assert mgr._managed_components_unavailable is False


def test_a_disabled_broken_component_does_not_hold_the_latch():
    # A component that is DISABLED is never started, so it must not keep the
    # device pinned to "capturing nothing" — the guard subtracts disabled ones.
    mgr = _mgr()
    mgr._exec_failed_components = {"bf-window-tracker"}
    mgr._disabled_components = {"bf-window-tracker"}
    mgr.tracker_download_failed = True
    mgr._managed_components_unavailable = True

    with patch.object(mgr, "_rosetta_missing", return_value=False), \
         patch.object(mgr, "_port_in_use", return_value=False), \
         patch.object(mgr, "_get_binaries_dir", return_value="/fake/trackers"), \
         patch.object(mgr, "_start_component", return_value=False):
        mgr._start_locked()

    assert mgr.tracker_download_failed is False
    assert mgr._managed_components_unavailable is False
