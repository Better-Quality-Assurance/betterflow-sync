"""#225: the upload stopped at the rotation boundary, so old incidents were unreachable.

The agent uploads `_read_log_tail(log_dir / "betterflow.log")` — the last 512 KB
of the CURRENT file only. `setup_logging` configures
`RotatingFileHandler(maxBytes=5 MiB, backupCount=3)`, so up to ~20 MB of history
sits on disk in `betterflow.log.1/.2/.3` and none of it is ever sent.

Cost, measured on 2026-08-25 while investigating #211: a capture from device 14
covered only that day. The line that would have settled the diagnosis
(`Extracted BetterFlow.app from DMG`, 2026-08-23) had rotated out, so the fix
shipped with its mechanism marked *inferred* and that evidence is gone for good.

The fix deliberately costs nothing from the trade-offs #225 listed. The budget is
unchanged, so the payload cannot grow; the server still receives one `log` file,
so the contract is unchanged; nothing new is written to disk. It only fills the
SAME 512 KB from further back when the current file does not fill it on its own
— which is exactly the state right after a rotation, and exactly the state that
lost the #211 evidence.

Consequence worth stating: when `betterflow.log` alone exceeds the budget the
behaviour is byte-identical to before. `test_a_full_current_log_is_unchanged`
pins that, because a "fix" that changed the common case would be a payload
regression on every device in the fleet.
"""

from __future__ import annotations

from src.sync.sync_engine import SyncEngine


def _write(p, text):
    p.write_bytes(text.encode("utf-8"))
    return p


def test_the_tail_reaches_back_into_the_rotated_file(tmp_path):
    """THE defect. A small current log means the incident is one file back."""
    _write(tmp_path / "betterflow.log.1", "OLD-INCIDENT-LINE\n")
    _write(tmp_path / "betterflow.log", "today\n")

    tail = SyncEngine._read_rotated_log_tail(tmp_path / "betterflow.log")

    assert b"OLD-INCIDENT-LINE" in tail
    assert b"today" in tail


def test_the_oldest_content_comes_first(tmp_path):
    """Chronological order, or a reader cannot follow it. `.2` is older than
    `.1`, which is older than the live file."""
    _write(tmp_path / "betterflow.log.2", "aaa\n")
    _write(tmp_path / "betterflow.log.1", "bbb\n")
    _write(tmp_path / "betterflow.log", "ccc\n")

    tail = SyncEngine._read_rotated_log_tail(tmp_path / "betterflow.log")

    assert tail == b"aaa\nbbb\nccc\n"


def test_a_full_current_log_is_unchanged(tmp_path):
    """The control, and the one that protects the fleet. When the live file
    already fills the budget the result must be byte-identical to reading it
    alone — otherwise this is a payload regression on every device."""
    _write(tmp_path / "betterflow.log.1", "OLD\n")
    _write(tmp_path / "betterflow.log", "x" * 4096)

    spanning = SyncEngine._read_rotated_log_tail(tmp_path / "betterflow.log", max_bytes=1024)
    single = SyncEngine._read_log_tail(tmp_path / "betterflow.log", max_bytes=1024)

    assert spanning == single
    assert b"OLD" not in spanning


def test_it_never_exceeds_the_budget(tmp_path):
    """The payload cap is the whole reason this fix is free.

    The live file is deliberately SMALLER than the budget. An earlier version
    made all four files 4096 bytes against a 1000-byte budget, so the live file
    filled it alone, the loop broke after one iteration and no cross-file
    accounting ever ran — a mutant giving every file the full cap survived it.
    Phantom 16: the fixture sat where correct and wrong agree.
    """
    for name in ("betterflow.log.3", "betterflow.log.2", "betterflow.log.1"):
        _write(tmp_path / name, "y" * 4096)
    _write(tmp_path / "betterflow.log", "today\n")

    tail = SyncEngine._read_rotated_log_tail(tmp_path / "betterflow.log", max_bytes=1000)

    assert len(tail) <= 1000


def test_replacement_expansion_cannot_burst_the_budget(tmp_path):
    """`_read_log_tail` normalises with errors="replace" and U+FFFD is THREE
    bytes, so an invalid byte expands 1->3 AFTER the budget was decremented.

    Measured at the real 512 KB budget before this was capped: a rotated file of
    invalid bytes returned 1,572,864 bytes. The server enforces max:1024 KB and
    hard-422s above it, and `logs_requested_at` clears only on success — so the
    agent would retry every heartbeat forever and the admin would never get
    logs. Below that it keeps the LAST 512 KB, trimming the opposite end to the
    one we fill, discarding the rotated history this function exists to deliver.
    """
    (tmp_path / "betterflow.log.1").write_bytes(b"\x97" * 4096)
    _write(tmp_path / "betterflow.log", "today\n")

    tail = SyncEngine._read_rotated_log_tail(tmp_path / "betterflow.log", max_bytes=1000)

    assert len(tail) <= 1000, f"expansion burst the cap: {len(tail)} bytes"
    tail.decode("utf-8")  # the trim must not cut a character in half


def test_the_trim_keeps_the_newest_end(tmp_path):
    """When the cap bites, keep the same end the server keeps. Trimming the
    other one would leave agent and server discarding different halves."""
    _write(tmp_path / "betterflow.log.1", "OLDEST-" + "o" * 2000)
    _write(tmp_path / "betterflow.log", "NEWEST-LINE\n")

    tail = SyncEngine._read_rotated_log_tail(tmp_path / "betterflow.log", max_bytes=500)

    assert len(tail) <= 500
    assert b"NEWEST-LINE" in tail


def test_a_missing_rotation_is_not_an_error(tmp_path):
    """The common case on a fresh install: no `.1` exists yet."""
    _write(tmp_path / "betterflow.log", "only\n")

    assert SyncEngine._read_rotated_log_tail(tmp_path / "betterflow.log") == b"only\n"


def test_an_unreadable_rotation_does_not_lose_the_live_log(tmp_path):
    """One bad file must not cost us the content we CAN read — the same
    fail-soft posture `prune_old_logs` takes three functions away."""
    _write(tmp_path / "betterflow.log", "live\n")
    (tmp_path / "betterflow.log.1").mkdir()  # a directory where a file belongs

    tail = SyncEngine._read_rotated_log_tail(tmp_path / "betterflow.log")

    assert b"live" in tail


def test_an_empty_live_log_still_yields_the_rotated_history(tmp_path):
    """The exact post-rotation window. `betterflow.log` has just been recreated
    and is empty; today the upload is skipped entirely and the caller logs
    "empty/unreadable — skipping this cycle", so the request is burned and the
    history one file back is never sent."""
    _write(tmp_path / "betterflow.log.1", "INCIDENT\n")
    _write(tmp_path / "betterflow.log", "")

    tail = SyncEngine._read_rotated_log_tail(tmp_path / "betterflow.log")

    assert b"INCIDENT" in tail


def test_a_missing_live_log_is_still_none(tmp_path):
    """No files at all reads as nothing, so the caller's `if not tail` guard
    behaves exactly as before."""
    assert not SyncEngine._read_rotated_log_tail(tmp_path / "betterflow.log")


def test_the_result_is_valid_utf8(tmp_path):
    """Each chunk is normalised before concatenation. Older Windows logs were
    cp1252, and one invalid byte makes the server's INSERT fail with MySQL 1366
    — silently dropping the whole upload."""
    (tmp_path / "betterflow.log.1").write_bytes(b"caf\x97 old\n")
    (tmp_path / "betterflow.log").write_bytes(b"caf\x97 new\n")

    tail = SyncEngine._read_rotated_log_tail(tmp_path / "betterflow.log")

    tail.decode("utf-8")  # must not raise
    assert b"old" in tail and b"new" in tail


# ── The callsite, which the helper tests cannot speak for ────────────────


def test_the_upload_path_actually_sends_the_rotated_history(tmp_path, monkeypatch):
    """Phantom 3, caught by mutation: every test above calls
    `_read_rotated_log_tail` directly, so reverting the CALLER back to
    `_read_log_tail` left all nine green. The whole fix could ship inert on the
    fleet and nothing here would notice.

    This drives `_upload_requested_logs` and asserts on the bytes handed to
    `bf.upload_logs` — the actual payload, not the helper's return value.
    """
    from unittest.mock import MagicMock

    from src.sync.sync_engine import SyncEngine

    (tmp_path / "betterflow.log.1").write_bytes(b"OLD-INCIDENT\n")
    (tmp_path / "betterflow.log").write_bytes(b"today\n")

    engine = SyncEngine.__new__(SyncEngine)  # no __init__: this method needs two attrs
    engine.config = MagicMock()
    engine.config.get_log_dir.return_value = tmp_path
    engine.bf = MagicMock()

    engine._upload_requested_logs()

    engine.bf.upload_logs.assert_called_once()
    sent = engine.bf.upload_logs.call_args[0][0]
    assert b"OLD-INCIDENT" in sent, (
        "the upload path still reads only the live file — the fix is inert"
    )
    assert b"today" in sent
