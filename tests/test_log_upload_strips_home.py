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
    frame = f'  File "{home}{sep}src{sep}app.py", line 4, in run'
    (tmp_path / "betterflow.log").write_text(
        f"{CONTROL}\n{frame}\n{CONTROL}\n", encoding="utf-8"
    )
    (tmp_path / "self-update-relaunch.log").write_text(
        f"relaunch {home}{sep}AppData{sep}x\n{CONTROL}\n", encoding="utf-8"
    )
    engine, bf = _engine(tmp_path)

    engine._send_heartbeat()

    bf.upload_logs.assert_called_once()
    log_tail, relaunch_tail = bf.upload_logs.call_args.args
    text = log_tail.decode("utf-8")
    assert "alice" not in text
    assert f'  File "~{sep}src{sep}app.py", line 4, in run' in text
    # A line with no home path is untouched.
    assert text.count(CONTROL) == 2
    rtext = relaunch_tail.decode("utf-8")
    assert "alice" not in rtext
    assert f"relaunch ~{sep}AppData{sep}x" in rtext
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

    engine._send_heartbeat()

    bf.upload_logs.assert_not_called()
