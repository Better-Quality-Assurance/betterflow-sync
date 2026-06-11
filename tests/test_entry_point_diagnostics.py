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

The tests below pin the diagnostic wiring at the SOURCE level rather
than importing entry_point. Importing the real module would chain into
main.py (and from there into pystray / AppKit), which registers an
Objective-C class — that registration fires once per process, so a
second import inside the test runner throws
`objc.error: _MenuProxy is overriding existing Objective-C class`. The
source-text check is intentionally narrow: it catches the only
regression we care about (someone removed the diagnostic setup) without
booting the GUI stack.
"""

import re
from pathlib import Path

ENTRY_POINT = Path(__file__).parent.parent / "src" / "entry_point.py"


def _source() -> str:
    return ENTRY_POINT.read_text(encoding="utf-8")


def test_entry_point_imports_faulthandler():
    """Without the import there is nothing to call on a hard crash."""
    src = _source()
    assert re.search(r"^import faulthandler\b", src, re.MULTILINE), (
        "entry_point.py must import faulthandler at module top-level"
    )


def test_entry_point_enables_faulthandler_at_startup():
    """faulthandler.enable() must run unconditionally at import time so a
    segfault / abort writes a Python-level traceback to stderr instead of
    dying silently. We assert the textual call rather than the runtime
    state because importing entry_point here would re-register
    Objective-C classes via the main.py chain."""
    src = _source()
    assert "faulthandler.enable()" in src, (
        "entry_point.py must call faulthandler.enable() at module load — "
        "without it, the silent-exit incident on 2026-06-11 has no "
        "on-disk evidence next time it recurs"
    )


def test_entry_point_registers_sigusr1_for_all_thread_dump():
    """SIGUSR1 must dispatch to faulthandler.dump_traceback(all_threads=True)
    so an operator can `kill -USR1 <pid>` to capture a snapshot of a hung
    process. all_threads matters: a deadlock between the sync thread and
    a tracker callback is only visible with both stacks side-by-side."""
    src = _source()
    # signal.SIGUSR1 is mac/Linux-only, so the wiring is rightly behind a
    # hasattr() guard. Verify both: the guard exists AND the registration
    # asks for all-threads.
    assert "hasattr(signal, \"SIGUSR1\")" in src or "hasattr(signal, 'SIGUSR1')" in src, (
        "SIGUSR1 wiring must be guarded by hasattr — Windows has no SIGUSR1"
    )
    assert re.search(
        r"faulthandler\.register\(\s*signal\.SIGUSR1\s*,[^)]*all_threads\s*=\s*True",
        src,
        re.DOTALL,
    ), (
        "SIGUSR1 must register with all_threads=True — a single-thread "
        "dump won't show the deadlock partner"
    )
