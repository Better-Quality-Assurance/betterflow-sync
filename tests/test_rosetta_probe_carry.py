"""A broken Rosetta probe must not overwrite an answer we already had.

Separate file on purpose: `test_aw_manager_rosetta_preflight.py` is being
rewritten by the native-arm64 work (#216/#217), and appending here would put a
conflict in front of that branch for no reason.
"""

import contextlib
import subprocess
from unittest.mock import patch

from src.aw_manager import AWManager


def _mgr() -> AWManager:
    mgr = AWManager(aw_port=5600)
    mgr.tracker_download_failed = False
    mgr._rosetta_missing_cached = None
    mgr._rosetta_notified = False
    return mgr


@contextlib.contextmanager
def _probe(result):
    """Apple Silicon, with `/usr/bin/arch` answering `result`.

    `result` is a CompletedProcess to return, or an exception to raise.
    """
    kw = {"side_effect": result} if isinstance(result, BaseException) else {"return_value": result}
    with patch("src.aw_manager.sys.platform", "darwin"), \
         patch("src.aw_manager.platform.machine", return_value="arm64"), \
         patch("src.aw_manager.subprocess.run", **kw):
        yield


MISSING = subprocess.CompletedProcess(args=[], returncode=1)
PRESENT = subprocess.CompletedProcess(args=[], returncode=0)


def test_a_broken_re_probe_keeps_the_answer_it_already_had():
    """A fork refusal is not evidence that Rosetta appeared.

    force_restart clears the memo every ROSETTA_REPROBE_INTERVAL, so on an
    affected Mac this runs ~288 times a day. One EAGAIN used to withdraw
    capture_blocked_remedy(), reverting the tray to "ActivityWatch not
    responding" -- the string #188 exists to delete.
    """
    mgr = _mgr()
    with _probe(MISSING):
        assert mgr._rosetta_missing() is True
    mgr.tracker_download_failed = True
    assert mgr.capture_blocked_remedy() is not None, "precondition: the remedy is owed"

    mgr._rosetta_missing_cached = None  # what force_restart does after the interval
    with _probe(OSError("EAGAIN")):
        assert mgr._rosetta_missing() is True

    assert mgr.capture_blocked_remedy() is not None


def test_a_broken_re_probe_does_not_log_a_recovery_it_never_saw(caplog):
    """That line is the only record that a user's install landed; support reads
    it to answer exactly that question."""
    caplog.set_level("INFO")
    mgr = _mgr()
    with _probe(MISSING):
        mgr._rosetta_missing()
    mgr._rosetta_missing_cached = None
    with _probe(OSError("EAGAIN")):
        mgr._rosetta_missing()
    assert not [r for r in caplog.records if "now available" in r.message]


def test_a_broken_FIRST_probe_still_fails_open(caplog):
    """The control. With no conclusive answer on record a broken probe must
    still fail toward attempting the start -- refusing there would stop capture
    on a healthy Mac, which is the harm the original behaviour prevented."""
    caplog.set_level("INFO")
    mgr = _mgr()
    with _probe(OSError("EAGAIN")):
        assert mgr._rosetta_missing() is False
    assert not [r for r in caplog.records if "now available" in r.message]


def test_rosetta_genuinely_appearing_is_still_noticed():
    """The carry must not pin the old answer forever."""
    mgr = _mgr()
    with _probe(MISSING):
        assert mgr._rosetta_missing() is True
    mgr._rosetta_missing_cached = None
    with _probe(PRESENT):
        assert mgr._rosetta_missing() is False
    assert mgr._rosetta_missing_conclusive is False
