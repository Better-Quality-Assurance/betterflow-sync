"""A native crash must leave a file on disk, and the next launch must report it.

Background (Sachi, Windows, device 16, 2026-11-11): the agent vanished at
16:16 local time with no "Shutting down" line, no error-board report and no
tray notification, and it had done the same six times on 2026-11-06. The
excepthooks only see Python exceptions, and faulthandler, the one tool that
sees a native crash, wrote to sys.stderr, which a windowed Windows build
points at the null device (entry_point._ensure_std_streams). Every native
crash on Windows was therefore written to nowhere.

The first test reproduces that exact shape for real: a child process imports
the entry point with stderr pointed at the null device, then segfaults. The
dump must land in the log directory.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import Mock

import pytest

from src import native_crash_log
from src.native_crash_log import CRASH_FILE, PREVIOUS_FILE

REPO = Path(__file__).parent.parent


def _child_env(home: Path) -> dict:
    env = dict(os.environ)
    # platformdirs resolves the log dir from HOME (macOS ~/Library/Logs,
    # Linux ~/.local/state); clearing XDG_STATE_HOME keeps Linux on HOME too.
    env["HOME"] = str(home)
    env.pop("XDG_STATE_HOME", None)
    env["PYTHONPATH"] = str(REPO)
    return env


@pytest.mark.skipif(sys.platform == "win32", reason="HOME does not move the Windows log dir")
def test_segfault_with_null_stderr_writes_the_dump_into_the_log_dir(tmp_path):
    script = textwrap.dedent(
        """
        import faulthandler, os, sys
        # The windowed-build condition: no console, stderr is the null device.
        sys.stderr = open(os.devnull, "w")
        import src.entry_point  # noqa: F401  (startup wiring under test)
        from config import Config
        print(Config.get_log_dir(), flush=True)
        faulthandler._sigsegv()
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=_child_env(tmp_path),
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode != 0, "the child must actually crash"
    log_dir = Path(proc.stdout.strip().splitlines()[0])
    assert str(log_dir).startswith(str(tmp_path)), log_dir

    crash = log_dir / CRASH_FILE
    assert crash.exists(), "a native crash left no file in the log dir"
    text = crash.read_text(encoding="utf-8", errors="replace")
    assert "Fatal Python error: Segmentation fault" in text, text


def test_clean_run_leaves_nothing_to_report(tmp_path):
    fh = native_crash_log.enable(tmp_path)
    try:
        assert fh is not None
        assert (tmp_path / CRASH_FILE).read_text() == ""
    finally:
        fh.close()

    # Next launch: an empty file means the last run did not crash natively.
    fh2 = native_crash_log.enable(tmp_path)
    fh2.close()
    assert not (tmp_path / PREVIOUS_FILE).exists()
    reporter = Mock()
    assert native_crash_log.report_previous(tmp_path, reporter) is None
    reporter.capture.assert_not_called()


def test_next_launch_reports_the_previous_crash_once(tmp_path):
    (tmp_path / CRASH_FILE).write_text(
        "Fatal Python error: Segmentation fault\n\nThread 0x1 (most recent call first):\n"
        '  File "pystray/_win32.py", line 42 in _run\n',
        encoding="utf-8",
    )

    fh = native_crash_log.enable(tmp_path)  # the next launch
    fh.close()
    assert (tmp_path / CRASH_FILE).read_text() == "", "the new run starts a fresh file"

    reporter = Mock()
    dump = native_crash_log.report_previous(tmp_path, reporter)
    assert dump is not None and "pystray/_win32.py" in dump

    reporter.capture.assert_called_once()
    kwargs = reporter.capture.call_args.kwargs
    assert kwargs["level"] == "fatal"
    assert kwargs["tags"] == {"component": "native-crash"}
    assert "pystray/_win32.py" in kwargs["context"]["faulthandler"]
    assert not (tmp_path / PREVIOUS_FILE).exists(), "reported once, then consumed"

    reporter2 = Mock()
    assert native_crash_log.report_previous(tmp_path, reporter2) is None
    reporter2.capture.assert_not_called()


def test_second_process_leaves_a_live_owners_file_alone(tmp_path):
    """A process starting while the agent is alive (a rejected second launch,
    the --privacy-notice child) must not truncate or rotate the agent's file."""
    fh = native_crash_log.enable(tmp_path)  # the live agent
    assert fh is not None
    try:
        fh.write("live owner's dump in progress\n")
        fh.flush()
        script = textwrap.dedent(
            f"""
            import sys
            from src import native_crash_log
            fh = native_crash_log.enable({str(tmp_path)!r})
            sys.exit(0 if fh is None else 3)
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            env=_child_env(tmp_path),
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, (proc.returncode, proc.stderr)
        assert not (tmp_path / PREVIOUS_FILE).exists(), "the live file was rotated"
        assert (tmp_path / CRASH_FILE).read_text() == "live owner's dump in progress\n"
    finally:
        fh.close()


def test_report_is_bounded_and_keeps_the_head(tmp_path):
    # faulthandler writes the crashing thread FIRST; the head is what matters.
    (tmp_path / PREVIOUS_FILE).write_text("HEAD" + "x" * 100_000, encoding="utf-8")
    reporter = Mock()
    native_crash_log.report_previous(tmp_path, reporter)
    sent = reporter.capture.call_args.kwargs["context"]["faulthandler"]
    assert len(sent) <= native_crash_log.MAX_REPORT_CHARS
    assert sent.startswith("HEAD")


def test_unwritable_log_dir_does_not_break_startup(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file where the log dir should be")
    assert native_crash_log.enable(blocker / "logs") is None


def test_app_startup_calls_report_previous():
    """Callsite guard: the helper is worthless if main stops calling it."""
    src = (REPO / "src" / "main.py").read_text(encoding="utf-8")
    assert "native_crash_log.report_previous(" in src
