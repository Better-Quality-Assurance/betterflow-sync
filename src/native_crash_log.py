"""Keep native crash dumps on disk, and report the last one on the next launch.

A native crash (access violation in a C extension, a tray/GUI library, the
interpreter itself) never reaches sys.excepthook, so the error reporter never
sees it. faulthandler does see it, but by default it writes to sys.stderr, and
a windowed Windows build has no console: entry_point points stderr at the null
device, so the dump was written to nowhere. Sachi's agent (Windows, device 16)
vanished at 16:16 on 2026-11-11 with no log line and no report, and had done
so six times on 2026-11-06; this is the instrument that would have said why.

Lifecycle, one file per run:
  * enable(log_dir) at startup. If native-crash.log is non-empty, the previous
    run died natively: move it to native-crash.prev.log, then start a fresh,
    empty native-crash.log and point faulthandler at it.
  * report_previous(log_dir, reporter) once logging and the error reporter
    exist: log and send native-crash.prev.log as a fatal report, then delete
    it so it is sent once.

An empty native-crash.log after a silent exit is evidence too: the process was
killed from outside (TerminateProcess, logout, an OS or security tool), not
crashed, because faulthandler cannot run when the process is terminated.

A dump is NOT proof of a crash on Windows. faulthandler installs a vectored
exception handler, which runs BEFORE any __try/__except frame gets a chance,
so an SEH exception a library catches and recovers from (ctypes turns an
access violation into OSError; COM and the shell raise and handle RPC
exceptions routinely) writes "Windows fatal exception: ..." plus every
thread's stack, and the process carries on. The text of a handled dump is
byte-for-byte the shape of a fatal one, so the only discriminator is whether
the process OUTLIVED it. checkpoint() is that test, run by the owner while it
is alive:
  * on the 60s tick: content that has not grown for a whole interval was
    written by a process that is still running, so it was handled. It is
    logged locally (never reported) and the file is truncated.
  * past MAX_INRUN_BYTES: truncated regardless, so a run that keeps hitting a
    handled exception cannot grow the file without bound.
  * at clean shutdown (final=True): everything in the file was survived.
What is left for report_previous() is only what was written in the last
interval or two before the process died without shutting down: a real fatal.
Residual, accepted: a handled exception in that last window followed by an
external kill is still reported; and a fatal whose process is held alive
past a full interval (a WER dialog) is logged locally as survived instead of
reported. Both stay in betterflow.log in full.

One owner at a time: enable() takes an exclusive lock on native-crash.lock and
holds it for the life of the process. A process that starts while another one
owns the file (a second launch turned away as "already running", or the
--privacy-notice child the agent spawns from its own executable) cannot take
the lock, so it touches neither file and leaves faulthandler where it was.
Without this, that process would truncate or rotate the live owner's file, and
its own crash would be reported as the agent's.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
from pathlib import Path
from typing import IO, Dict, Optional

logger = logging.getLogger(__name__)

CRASH_FILE = "native-crash.log"
PREVIOUS_FILE = "native-crash.prev.log"
LOCK_FILE = "native-crash.lock"

# Owner locks this process holds, by lock path. Held for the life of the
# process: closing the file drops the lock.
_owner_locks: Dict[str, IO[str]] = {}

# faulthandler writes the crashing thread first, then the others. The head is
# the useful part; bound what goes over the wire.
MAX_REPORT_CHARS = 8000

# In-run bound on native-crash.log. One all-threads dump is a few KB; this
# holds several. checkpoint() truncates past it, so the file is bounded by
# this plus one tick's worth of writes (faulthandler writes from C, so
# nothing in Python can refuse an individual write).
MAX_INRUN_BYTES = 64 * 1024

# The crash file this process owns (set by enable()), and its size at the
# previous checkpoint(): the "has it been quiet for a whole interval?" probe.
_owned_file: Optional[IO[str]] = None
_settle_size: int = 0


def enable(log_dir: Path) -> Optional[IO[str]]:
    """Point faulthandler at ``log_dir/native-crash.log``.

    Returns the open file, which the caller MUST keep referenced for the life
    of the process (faulthandler holds only its file descriptor; if the file
    object is garbage-collected the descriptor closes and the dump is lost).

    Returns None if the log dir is unusable, or if another live process owns
    the crash file (see the module docstring). This runs before logging is
    configured, so there is nowhere to log the failure; the None is the
    signal, and startup must never fail over a diagnostic aid. faulthandler
    keeps whatever target it already had.
    """
    global _owned_file, _settle_size
    try:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        if not _acquire_owner_lock(log_dir / LOCK_FILE):
            return None
        crash = log_dir / CRASH_FILE
        if crash.exists() and crash.stat().st_size > 0:
            # We hold the owner lock, so no live process is writing here:
            # content means the previous owner died natively.
            crash.replace(log_dir / PREVIOUS_FILE)
        # Append mode on purpose: checkpoint() truncates this file while
        # faulthandler still holds the descriptor, and only O_APPEND makes
        # the next write land at the new end instead of at the old offset
        # (which would leave a hole of NULs the size of what was removed).
        open(crash, "w", encoding="utf-8").close()  # start this run empty
        fh = open(crash, "a", encoding="utf-8")  # noqa: SIM115 - held for the process
        faulthandler.enable(file=fh, all_threads=True)
        _owned_file, _settle_size = fh, 0
        return fh
    except (OSError, RuntimeError, ValueError):
        return None


def _acquire_owner_lock(path: Path) -> bool:
    """Take the exclusive, process-lifetime lock on ``path``; False if held.

    Same byte-0 locking as main.SingleInstanceLock (which cannot be imported
    this early in startup). Re-entrant within one process: if this process
    already owns it, that is success.
    """
    key = str(path)
    if key in _owner_locks:
        return True
    fh = open(path, "a+")  # noqa: SIM115 - held open on purpose
    try:
        # msvcrt locks at the current position and "a+" opens at EOF; seek so
        # every process contends for the same byte. flock ignores position.
        fh.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _owner_locks[key] = fh
    return True


def checkpoint(*, final: bool = False) -> int:
    """Consume dumps this live process has survived; return the bytes removed.

    Called by the owner on the 60s tick and once at clean shutdown
    (``final=True``). See the module docstring for why surviving a dump is
    what separates a handled exception from a crash. A process that does not
    own the crash file (enable() returned None) has nothing to do here.

    Never raises: this runs on the tick and in shutdown, and a diagnostic aid
    must not break either.
    """
    global _settle_size
    fh = _owned_file
    if fh is None:
        return 0
    try:
        size = os.fstat(fh.fileno()).st_size
        if size == 0:
            _settle_size = 0
            return 0
        quiet = size == _settle_size  # nothing new for a whole interval, and we are running
        if not (final or quiet or size > MAX_INRUN_BYTES):
            _settle_size = size  # written recently: judge it next interval
            return 0
        with open(fh.name, "r", encoding="utf-8", errors="replace") as rd:
            text = rd.read(MAX_REPORT_CHARS)
        over_cap = size > MAX_INRUN_BYTES
        if not (final or over_cap) and os.fstat(fh.fileno()).st_size != size:
            # Written while we looked: that write has not been survived yet
            # (it may be a fatal one). Judge everything next interval. Not
            # past the cap: a storm that never pauses would never be cleared.
            _settle_size = os.fstat(fh.fileno()).st_size
            return 0
        os.ftruncate(fh.fileno(), 0)
        _settle_size = 0
    except (OSError, ValueError) as e:  # ValueError: the file was closed
        logger.debug("native crash checkpoint skipped: %s", e)
        return 0
    # Local only. This process outlived the dump, so it is not a crash and is
    # never sent as one.
    logger.warning(
        "Native exception survived by this process (handled, not a crash); "
        "%d bytes cleared from %s. Head:\n%s",
        size,
        CRASH_FILE,
        text,
    )
    return size


def report_previous(log_dir: Path, reporter) -> Optional[str]:
    """Report the previous run's native crash, if there was one, exactly once.

    Returns the dump that was reported (bounded), or None when the previous
    run left nothing. Never raises.
    """
    prev = Path(log_dir) / PREVIOUS_FILE
    try:
        if not prev.exists():
            return None
        dump = prev.read_text(encoding="utf-8", errors="replace")[:MAX_REPORT_CHARS]
    except OSError as e:
        logger.warning("Could not read the previous native crash dump: %s", e)
        return None
    if not dump.strip():
        _remove(prev)
        return None
    logger.error("Previous run ended in a native crash:\n%s", dump)
    try:
        reporter.capture(
            "Previous run ended in a native crash",
            level="fatal",
            tags={"component": "native-crash"},
            context={"faulthandler": dump},
            fingerprint="native-crash",
        )
    except Exception as e:  # capture() is documented never to raise; belt and braces
        logger.warning("Reporting the previous native crash failed: %s", e)
    finally:
        # Consumed whether or not the send worked: the dump is already in
        # betterflow.log (above), and re-sending the same crash on every
        # launch would read as a new crash each time.
        _remove(prev)
    return dump


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError as e:
        logger.warning("Could not remove %s: %s", path, e)
