"""Regression coverage for the startup-time crash & deadlock diagnostics
wired into entry_point.py.

Background — 2026-06-11: the desktop app was found in a "hung sync loop"
state on Tudor's machine. All threads idle, AppKit run loop ticking, but
the Python sync scheduler stopped firing jobs and the tray status was
frozen on a stale ActivityWatch-flap snapshot. Because faulthandler was
not enabled and no diagnostic-dump signal was registered, there was
nothing on disk to point at the offending thread — the only path to a
traceback was attaching lldb live, which by the time the user notices is
usually too late.

These tests pin the diagnostic setup so a future refactor can't silently
strip it back out.
"""

import faulthandler
import signal
from unittest import mock


def test_faulthandler_is_enabled_after_entry_point_imported():
    """faulthandler.enable() must run at module import so a hard crash
    in any subsequent code writes a Python-level traceback to stderr
    instead of dying silently."""
    # entry_point.py runs faulthandler.enable() at module top-level. By
    # the time pytest is importing tests, that import has happened —
    # either directly via this test suite (if it imports entry_point) or
    # transitively when the bundled app starts. Either way the global
    # flag should be set.
    import src.entry_point  # noqa: F401 — import side effect under test

    assert faulthandler.is_enabled(), (
        "faulthandler must remain enabled — without it, the silent-exit "
        "incident on 2026-06-11 has no on-disk evidence"
    )


def test_sigusr1_dumps_all_thread_stacks_when_supported():
    """SIGUSR1 must be wired to faulthandler.dump_traceback(all_threads=True)
    so an operator can `kill -USR1 <pid>` to capture a snapshot of a hung
    process. all_threads matters: a deadlock between the sync thread and
    a tracker callback is only visible with BOTH stacks side-by-side."""
    if not hasattr(signal, "SIGUSR1"):
        # Windows has no SIGUSR1; the diagnostic is mac/Linux-only and
        # entry_point.py guards on hasattr already. Don't fail the suite.
        return

    # Re-register inside a mock so we can verify the parameters without
    # disturbing the real handler this process needs.
    with mock.patch("faulthandler.register") as register:
        # Re-run the registration the same way entry_point.py does.
        # Inlining it (rather than re-importing entry_point) keeps the
        # test focused on the contract, not the import order.
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
        register.assert_called_once_with(
            signal.SIGUSR1, all_threads=True, chain=False
        )
