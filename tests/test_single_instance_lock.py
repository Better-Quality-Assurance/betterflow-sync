"""Single-instance lock must actually block a second instance — on every OS.

Regression: two BetterFlow instances ran side-by-side on Windows (fighting over
the AW server port + the input hook -> 0 events for the user). Cause:
msvcrt.locking() locks bytes at the CURRENT file position, and the lock file is
opened "a+" (pointer at end-of-file). Once the first instance wrote its PID, a
second instance opened at a non-zero offset and locked a DIFFERENT byte, so the
two never conflicted. The fix seeks to byte 0 before locking.

These tests run on the Windows CI runner too (the build matrix runs pytest on
win32), so they exercise the msvcrt path that regressed — not just the Unix
fcntl path. The key case is `second instance blocked even when the lock file is
already non-empty`, which is exactly what the offset bug broke.
"""

import sys

import pytest

from src.main import SingleInstanceLock


def _lock(path) -> SingleInstanceLock:
    lock = SingleInstanceLock()
    lock._path = str(path)  # bypass Config.get_config_dir() for a temp lockfile
    return lock


def test_second_instance_is_blocked_while_first_holds(tmp_path):
    p = tmp_path / ".betterflow.lock"
    first = _lock(p)
    second = _lock(p)
    assert first.acquire() is True
    try:
        # The lock file now contains the first PID — i.e. it is NON-EMPTY, which
        # is precisely where the Windows byte-offset bug let a second instance
        # slip through. It must still be blocked.
        assert second.acquire() is False, "a second instance must not acquire the lock"
    finally:
        first.release()


def test_lock_is_reusable_after_release(tmp_path):
    p = tmp_path / ".betterflow.lock"
    a = _lock(p)
    assert a.acquire() is True
    a.release()
    # A fresh instance can take the lock once the holder released it.
    b = _lock(p)
    assert b.acquire() is True, "lock must be re-acquirable after release"
    b.release()


def test_context_manager_releases(tmp_path):
    p = tmp_path / ".betterflow.lock"
    a = _lock(p)
    assert a.acquire() is True
    with a:  # __exit__ releases
        pass
    # Released — another instance can now acquire.
    b = _lock(p)
    assert b.acquire() is True
    b.release()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows byte-range locks are MANDATORY, so reading the locked lock "
    "file raises PermissionError. The structural guarantee (acquire fails at "
    "msvcrt.locking before the truncate/write) is covered by the 'second instance "
    "blocked' test, which passes on the Windows runner.",
)
def test_first_instance_pid_survives_a_blocked_second_attempt(tmp_path):
    """A blocked second acquire must NOT truncate/overwrite the holder's lock
    file (the old Windows path locked a different byte, then truncated + wrote its
    own PID, clobbering the real holder's)."""
    p = tmp_path / ".betterflow.lock"
    first = _lock(p)
    assert first.acquire() is True
    try:
        pid_after_first = p.read_text().strip()
        second = _lock(p)
        assert second.acquire() is False
        assert p.read_text().strip() == pid_after_first, "holder's PID must be intact"
    finally:
        first.release()
