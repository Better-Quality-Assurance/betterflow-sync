"""BW TASK-002506: the logs_requested upload must not carry the OS login name.

``_upload_requested_logs`` sent the raw betterflow.log tail, so every
``/Users/<login>/...`` frame path left the device with the employee's login.
Crash reports already redact this with ``error_reporter.strip_home``; the log
upload goes through that SAME function.

Drives the real ``_send_heartbeat`` -> ``_upload_requested_logs`` with real log
files on disk and asserts on the bytes the fake bf client was actually handed.
"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from src import error_reporter
from src.config import Config
from src.sync.sync_engine import SyncEngine

CONTROL = "2026-11-14 10:00:00 INFO sync: 3 events sent (no path here)"


def _engine(log_dir):
    queue = Mock()
    queue.get_checkpoint.return_value = None
    queue.evict_unstorable.return_value = {
        "count": 0, "bucket_ids": [], "oldest": None, "newest": None,
        "real_loss_count": 0, "unstorable_count": 0,
    }
    config = Config()
    config.working_hours.known = True
    config.get_log_dir = lambda: Path(log_dir)
    bf = Mock()
    bf.heartbeat.return_value = {"success": True, "data": {"logs_requested": True}}
    engine = SyncEngine(
        aw=Mock(), bf=bf, queue=queue, config=config,
        activity_analyzer=Mock(), time_tracker=Mock(),
    )
    return engine, bf


@pytest.mark.parametrize(
    "home,sep",
    [
        ("/Users/alice", "/"),
        ("C:\\Users\\alice", "\\"),
        ("/home/alice", "/"),
    ],
    ids=["macos", "windows", "linux"],
)
def test_uploaded_log_tail_shows_home_as_tilde(tmp_path, monkeypatch, home, sep):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(home)))
    monkeypatch.setattr(error_reporter, "_home_dirs", lambda: [home])
    # Two or more home-bearing lines per log, and two on one line: a strip_home
    # that replaces only the first match (count=1) must turn this red.
    frame = f'  File "{home}{sep}src{sep}app.py", line 4, in run'
    frame2 = f'  File "{home}{sep}src{sep}b.py", line 9, in go; also {home}{sep}c.py'
    (tmp_path / "betterflow.log").write_text(
        f"{CONTROL}\n{frame}\n{frame2}\n{CONTROL}\n", encoding="utf-8"
    )
    (tmp_path / "self-update-relaunch.log").write_text(
        f"relaunch {home}{sep}AppData{sep}x\nagain {home}{sep}AppData{sep}y\n{CONTROL}\n",
        encoding="utf-8",
    )
    engine, bf = _engine(tmp_path)

    engine._send_heartbeat()

    bf.upload_logs.assert_called_once()
    log_tail, relaunch_tail = bf.upload_logs.call_args.args
    text = log_tail.decode("utf-8")
    assert "alice" not in text
    assert f'  File "~{sep}src{sep}app.py", line 4, in run' in text
    assert f'  File "~{sep}src{sep}b.py", line 9, in go; also ~{sep}c.py' in text
    assert text.count("~") == 3  # every one of the 3 home occurrences redacted
    # A line with no home path is untouched.
    assert text.count(CONTROL) == 2
    rtext = relaunch_tail.decode("utf-8")
    assert "alice" not in rtext
    assert f"relaunch ~{sep}AppData{sep}x" in rtext
    assert f"again ~{sep}AppData{sep}y" in rtext
    assert rtext.count("~") == 2
    assert CONTROL in rtext


def test_log_without_home_path_is_byte_identical(tmp_path, monkeypatch):
    monkeypatch.setattr(error_reporter, "_home_dirs", lambda: ["/Users/alice"])
    payload = (CONTROL + "\n").encode("utf-8")
    (tmp_path / "betterflow.log").write_bytes(payload)
    engine, bf = _engine(tmp_path)

    engine._send_heartbeat()

    assert bf.upload_logs.call_args.args[0] == payload
    assert bf.upload_logs.call_args.args[1] is None


def test_redaction_failure_uploads_nothing(tmp_path, monkeypatch):
    # Fail closed: if strip_home cannot run, the raw tail must not be sent.
    (tmp_path / "betterflow.log").write_text("/Users/alice/x\n", encoding="utf-8")
    import src.error_reporter as se

    def boom(_v):
        raise RuntimeError("boom")

    monkeypatch.setattr(se, "strip_home", boom)
    engine, bf = _engine(tmp_path)
    engine.error_reporter = Mock()

    engine._send_heartbeat()

    bf.upload_logs.assert_not_called()
    # The admin's request must stay diagnosable: the failure is reported.
    engine.error_reporter.capture.assert_called_once()
    assert engine.error_reporter.capture.call_args.kwargs["context"] == {
        "reason": "home-path redaction failed"
    }


# --------------------------------------------------------------------------- #
# Round 2 (I-B): a tail window that starts MID-path must not leak a fragment   #
# --------------------------------------------------------------------------- #

MAX = 512 * 1024


def test_window_starting_mid_home_path_sends_no_login_fragment(tmp_path, monkeypatch):
    # 524,299 bytes: the 512 KiB window starts 11 bytes in, 3 bytes into
    # "/Users/alice/...", so the raw tail begins "ers/alice/..." which strip_home
    # (which needs the whole "/Users/alice") cannot see.
    monkeypatch.setattr(error_reporter, "_home_dirs", lambda: ["/Users/alice"])
    head = b"12345678" + b"/Users/alice/src/app.py\n"
    body = (CONTROL + "\n").encode()
    pad = MAX + 11 - len(head)
    content = head + body * (pad // len(body))
    content += b"x" * (MAX + 11 - len(content) - 1) + b"\n"
    assert len(content) == MAX + 11
    assert content[11:].startswith(b"ers/alice/")
    (tmp_path / "betterflow.log").write_bytes(content)
    engine, bf = _engine(tmp_path)

    engine._send_heartbeat()

    sent = bf.upload_logs.call_args.args[0]
    assert b"alice" not in sent
    assert len(sent) <= MAX
    # Not over-trimmed: the whole lines after the partial one are all there.
    assert sent.count(CONTROL.encode()) >= (pad // len(body)) - 1


@pytest.mark.parametrize("max_bytes", range(8, 60))
def test_no_cut_point_of_the_tail_reads_leaves_a_login_fragment(tmp_path, max_bytes):
    # Every possible window start, live file and rotated span alike.
    live = tmp_path / "betterflow.log"
    live.write_bytes(b"\xff\xfe junk\n  File \"/Users/alice/a.py\", line 1\nlast\n")
    (tmp_path / "betterflow.log.1").write_bytes(b"old /Users/alice/b.py\nolder\n")
    for tail in (
        SyncEngine._read_log_tail(live, max_bytes),
        SyncEngine._read_rotated_log_tail(live, max_bytes),
    ):
        # A whole "/Users/alice" is what strip_home removes; any "alice" left
        # after that is a fragment it could not see.
        text = tail.decode("utf-8").replace("/Users/alice", "~")
        assert "alice" not in text, (max_bytes, text)
