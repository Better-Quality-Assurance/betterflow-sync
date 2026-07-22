"""Agent log files must not survive longer than the disclosed retention window.

The signed monitoring disclosure (Regulament Intern art. 68^1 alin. 8 lit. f)
states agent log files are kept 30 days. The logs carry app names, the machine
hostname, and OS usernames inside stack-trace paths, so "kept 30 days" is a
privacy *ceiling*, not a floor: nothing older than 30 days may remain.

Size-based rotation alone cannot deliver that promise — a quiet machine keeps a
5 MB file for months, a busy one for hours. These tests pin a real time bound on
top of the size cap: a startup retention pass that removes any log file, rotated
or active, whose last write is older than the window.

No absolute dates: every fixture is anchored to the real clock via os.utime, so
the test cannot rot as the calendar moves (two time-bombed tests detonated in
these repos this week).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from src.config import LOG_RETENTION_DAYS, prune_old_logs, setup_logging


def _age(path: Path, days: float) -> None:
    """Backdate a file's mtime by `days`, relative to now."""
    when = time.time() - days * 86400
    os.utime(path, (when, when))


def _mk(path: Path, content: str = "x") -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_the_window_is_the_disclosed_thirty_days():
    # If this number moves, the Regulament sentence moves with it. Pin it so a
    # silent change to 7 or 90 fails here rather than in a signed document.
    assert LOG_RETENTION_DAYS == 30


def test_a_rotated_log_older_than_the_window_is_removed(tmp_path: Path):
    old = _mk(tmp_path / "betterflow.log.3")
    _age(old, days=31)

    removed = prune_old_logs(tmp_path, now=time.time())

    assert old in removed
    assert not old.exists(), "a 31-day-old rotated log survived the retention pass"


def test_a_recent_log_is_kept(tmp_path: Path):
    recent = _mk(tmp_path / "betterflow.log.1")
    _age(recent, days=3)

    removed = prune_old_logs(tmp_path, now=time.time())

    assert recent not in removed
    assert recent.exists(), "a 3-day-old log was deleted; the pass is too aggressive"


def test_the_active_log_is_swept_when_its_last_write_predates_the_window(tmp_path: Path):
    # The ceiling applies to the live file too: on an ultra-quiet machine the
    # active betterflow.log can hold lines older than 30 days and never rotate.
    # Its mtime is the last write, so mtime past the window means every line is.
    active = _mk(tmp_path / "betterflow.log")
    _age(active, days=45)

    removed = prune_old_logs(tmp_path, now=time.time())

    assert active in removed
    assert not active.exists()


def test_only_our_log_files_are_touched(tmp_path: Path):
    # The pass must scope to betterflow logs, not empty the log directory of a
    # neighbour's files that happen to be old.
    ours = _mk(tmp_path / "betterflow.log.2")
    _age(ours, days=40)
    theirs = _mk(tmp_path / "some-other-app.log")
    _age(theirs, days=40)
    unrelated = _mk(tmp_path / "notes.txt")
    _age(unrelated, days=40)

    prune_old_logs(tmp_path, now=time.time())

    assert not ours.exists()
    assert theirs.exists(), "an unrelated .log was deleted"
    assert unrelated.exists(), "an unrelated file was deleted"


def test_a_missing_or_empty_directory_is_not_an_error(tmp_path: Path):
    # Retention must never break startup. A first launch has no log dir yet.
    assert prune_old_logs(tmp_path / "does-not-exist", now=time.time()) == []
    assert prune_old_logs(tmp_path, now=time.time()) == []


def test_an_unstattable_file_does_not_abort_the_sweep(tmp_path: Path, monkeypatch):
    # One unreadable file must not stop the others being pruned — retention that
    # gives up on the first error silently keeps everything behind it.
    good = _mk(tmp_path / "betterflow.log.1")
    _age(good, days=40)

    real_unlink = Path.unlink
    calls = {"n": 0}

    def flaky_unlink(self, *a, **k):
        calls["n"] += 1
        if self.name == "betterflow.log.9":
            raise PermissionError("locked")
        return real_unlink(self, *a, **k)

    bad = _mk(tmp_path / "betterflow.log.9")
    _age(bad, days=40)
    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    removed = prune_old_logs(tmp_path, now=time.time())

    assert good in removed and not good.exists()


def test_setup_logging_runs_the_pass(tmp_path: Path, monkeypatch):
    # The callsite guard: a prune function nobody calls is Phantom 3. Prove
    # setup_logging actually invokes it, against a real old file in the real
    # log dir, driving the real code path rather than asserting on a mock.
    monkeypatch.setattr("src.config.Config.get_log_dir", classmethod(lambda cls: tmp_path))
    stale = _mk(tmp_path / "betterflow.log.3")
    _age(stale, days=60)

    try:
        setup_logging(debug=False)
        assert not stale.exists(), "setup_logging did not run the retention pass"
    finally:
        for h in logging.getLogger().handlers[:]:
            logging.getLogger().removeHandler(h)
            h.close()
