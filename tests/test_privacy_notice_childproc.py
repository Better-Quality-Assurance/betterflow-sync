"""The privacy notice runs in an isolated child process, not inline.

Root cause of #158 ("Tray icon died — agent force-exiting (ghost process)"):
the one-time privacy notice ran Tk's ``mainloop()`` on the main thread, in the
SAME process, immediately before pystray's ``run_blocking()``. On macOS Tk and
pystray share one ``NSApplication``; after Tk created and tore down its NSApp
the freshly-created ``NSStatusItem`` went unstable, the tray-health probe failed
two ticks, and ``_on_tray_died`` called ``os._exit(1)``.

The fix hands the notice to a throwaway child process (``--privacy-notice``) so
the agent keeps a pristine ``NSApplication``. These tests pin the parts that CAN
be checked headlessly:

  * ``main(--privacy-notice)`` routes to the child WITHOUT taking the
    single-instance lock (the parent holds it) and exits with the child's code;
  * the child maps acknowledge/dismiss/render-error to exit 0/2/3;
  * the parent records the acknowledgement ONLY on exit 0;
  * the spawn command is correct — and argv-carrying — both frozen and unfrozen.

The one thing they cannot check is the real NSStatusItem surviving after the Tk
child exits: that needs a real menu bar and is the human-QA smoke in the PR.
"""

import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import src.main as m
from src import privacy_notice as pn
from src.config import Config

# ── main() argument routing ─────────────────────────────────────────────

def test_privacy_notice_flag_routes_to_child_without_taking_the_lock():
    """``--privacy-notice`` must be handled before the single-instance lock.

    The parent agent already holds the lock; if the child tried to acquire it
    it would lose, print "already running", and never show the notice.
    """
    lock = MagicMock()
    with patch.object(m, "_instance_lock", lock), \
         patch.object(m, "_run_privacy_notice_child", return_value=0) as child, \
         patch.object(m, "BetterFlowApp") as app, \
         patch.object(sys, "argv", ["betterflow", "--privacy-notice"]), \
         pytest.raises(SystemExit) as exc:
        m.main()

    child.assert_called_once()
    lock.acquire.assert_not_called()
    app.assert_not_called()
    assert exc.value.code == 0


def test_privacy_notice_flag_exit_code_is_the_childs():
    with patch.object(m, "_instance_lock", MagicMock()), \
         patch.object(m, "_run_privacy_notice_child", return_value=2), \
         patch.object(m, "BetterFlowApp"), \
         patch.object(sys, "argv", ["betterflow", "--privacy-notice"]), \
         pytest.raises(SystemExit) as exc:
        m.main()
    assert exc.value.code == 2


def test_normal_launch_still_takes_the_lock():
    """Without the flag, nothing changes: acquire the lock, run the app."""
    lock = MagicMock()
    lock.acquire.return_value = True
    with patch.object(m, "_instance_lock", lock), \
         patch.object(m, "_run_privacy_notice_child") as child, \
         patch.object(m, "BetterFlowApp") as app, \
         patch.object(sys, "argv", ["betterflow"]):
        m.main()
    child.assert_not_called()
    lock.acquire.assert_called_once()
    app.assert_called_once()


# ── child exit-code mapping ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "shows, expected",
    [(True, m.PRIVACY_NOTICE_ACKNOWLEDGED), (False, m.PRIVACY_NOTICE_DISMISSED)],
)
def test_child_maps_acknowledge_and_dismiss(shows, expected):
    with patch(
        "src.ui.privacy_notice_window.show_privacy_notice",
        MagicMock(return_value=shows),
    ):
        assert m._run_privacy_notice_child() == expected


def test_child_maps_render_failure_to_its_own_code():
    """A broken display must exit non-0-non-2 so the parent does not record it."""
    with patch(
        "src.ui.privacy_notice_window.show_privacy_notice",
        MagicMock(side_effect=RuntimeError("no display name and no $DISPLAY")),
    ):
        code = m._run_privacy_notice_child()
    assert code == m.PRIVACY_NOTICE_RENDER_ERROR
    assert code not in (m.PRIVACY_NOTICE_ACKNOWLEDGED, m.PRIVACY_NOTICE_DISMISSED)


# ── parent records ONLY on exit 0 ───────────────────────────────────────

def _run_parent_with_returncode(returncode):
    config = Config()
    stub = MagicMock()
    stub.config = config
    run = MagicMock(return_value=MagicMock(returncode=returncode))
    with patch("src.main.subprocess.run", run):
        m.BetterFlowApp._show_privacy_notice_if_needed(stub)
    return config


def test_parent_records_on_acknowledge():
    config = _run_parent_with_returncode(m.PRIVACY_NOTICE_ACKNOWLEDGED)
    assert pn.needs_acknowledgement(config) is False


def test_parent_does_not_record_on_dismiss():
    config = _run_parent_with_returncode(m.PRIVACY_NOTICE_DISMISSED)
    assert pn.needs_acknowledgement(config) is True


def test_parent_does_not_record_on_render_error():
    config = _run_parent_with_returncode(m.PRIVACY_NOTICE_RENDER_ERROR)
    assert pn.needs_acknowledgement(config) is True, (
        "a render failure banked an acknowledgement nobody made"
    )


# ── spawn command: frozen vs unfrozen, and it MUST carry the flag ───────

def test_child_argv_frozen_runs_the_bundle_executable_directly():
    """Frozen: sys.executable IS the app binary. Run it directly with the flag —
    a direct exec passes argv, unlike macOS ``open`` (which _relaunch uses).
    """
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(sys, "executable", "/Applications/BetterFlow.app/Contents/MacOS/BetterFlow"):
        argv, cwd = m._privacy_notice_child_argv()
    assert argv[0] == "/Applications/BetterFlow.app/Contents/MacOS/BetterFlow"
    assert "--privacy-notice" in argv
    # No `open`, no Apple-Events indirection that would drop the flag.
    assert "open" not in argv
    assert cwd is None


def test_child_argv_unfrozen_reruns_the_module_from_repo_root():
    with patch.object(sys, "frozen", False, create=True), \
         patch.object(sys, "executable", "/usr/bin/python3"):
        argv, cwd = m._privacy_notice_child_argv()
    assert argv == ["/usr/bin/python3", "-m", "src.main", "--privacy-notice"]
    # cwd must be the repo root (parent of src/) so `-m src.main` imports resolve.
    assert cwd == str(Path(m.__file__).resolve().parents[1])
    assert (Path(cwd) / "src" / "main.py").exists()


# ── the force-exit must actually exit, even when cleanup deadlocks ───────

def test_on_tray_died_force_exits_even_when_shutdown_deadlocks(monkeypatch):
    """The tray-died force-exit must not depend on cleanup returning.

    _on_tray_died runs on an APScheduler worker thread (tick_clock -> _tick_60s),
    and _shutdown() stops that SAME scheduler with wait=True — a self-join that
    deadlocks and never reaches os._exit(), leaving exactly the ghost process
    this guard exists to prevent (reproduced 2026-07-23: both the notice child
    and the parent left alive, scheduler error-storming). An armed backstop must
    call os._exit even if _shutdown never comes back.
    """
    stub = MagicMock()
    hang = threading.Event()
    stub._shutdown = MagicMock(side_effect=lambda: hang.wait(2.0))
    codes = []
    exited = threading.Event()

    def fake_exit(code):
        codes.append(code)
        hang.set()  # let the hung _shutdown unwind so the worker thread ends
        exited.set()

    monkeypatch.setattr(m.os, "_exit", fake_exit)
    # Short backstop so the test is fast; raising=False keeps the pre-fix RED
    # clean (the constant does not exist until the fix adds it).
    monkeypatch.setattr(m, "_TRAY_DIED_HARD_EXIT_SECONDS", 0.05, raising=False)

    worker = threading.Thread(
        target=m.BetterFlowApp._on_tray_died, args=(stub,), daemon=True
    )
    worker.start()

    assert exited.wait(1.0), "force-exit never fired while _shutdown was hung"
    assert 1 in codes
