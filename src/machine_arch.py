"""Resolve the machine's REAL architecture, seeing through Rosetta 2.

``platform.machine()`` reports the architecture of the **running process**, not
of the hardware. An x86_64 build running under Rosetta 2 on Apple Silicon
reports ``x86_64`` — byte-identical to what a genuine Intel Mac reports — so it
cannot tell "this IS an Intel Mac" from "this is the WRONG BUILD on an Apple
Silicon Mac". Verified on Apple Silicon:

    native:         platform.machine() = arm64    proc_translated = 0
    arch -x86_64:   platform.machine() = x86_64   proc_translated = 1

That ambiguity is not academic. The updater picks its download by matching the
architecture against the release asset names, so an Intel install on an M-series
Mac re-selected the Intel DMG on every update and could never climb out on its
own. The only discriminator is the ``sysctl.proc_translated`` flag.

Kept dependency-free and fully injectable (``machine`` / ``translated`` /
``sysctl_reader`` overrides) so the logic is unit-testable on any host, in the
same spirit as ``release_version.py``.
"""

import errno
import logging
import platform
import subprocess
import threading
import time
from typing import Callable, NamedTuple, Optional

logger = logging.getLogger(__name__)

ARM64 = "arm64"
X86_64 = "x86_64"

# `sysctl -n sysctl.proc_translated` answers:
#   "1"      -> this process is x86_64 running under Rosetta 2 on arm64 hardware
#   "0"      -> native process
#   <absent> -> the key does not exist at all on a real Intel Mac, so sysctl
#               exits non-zero and prints to stderr. Absent and "0" MUST be
#               treated the same; branching on presence marks every Intel Mac
#               as translated.
_PROC_TRANSLATED_KEY = "sysctl.proc_translated"

# Guards the memo below. The tray rebuilds its menu on every stats update, and
# _create_menu() runs while holding TrayIcon._menu_lock, so an un-memoised probe
# would fork `sysctl` (timeout=2) under that lock on the sync cycle.
_lock = threading.Lock()
_cached_raw: Optional[str] = None
_probed = False
_probe_attempts = 0
_last_transient_at: Optional[float] = None


class ProbeResult(NamedTuple):
    """What the probe said, and whether it is worth asking again.

    ``conclusive`` is False ONLY when we never got an answer — see
    ``_read_proc_translated``. Returned as a value rather than signalled through
    module state so the classification cannot be read out of order, and so the
    producer has no side effect its docstring has to warn about.
    """

    raw: Optional[str]
    conclusive: bool


# CONGESTION, in every spelling it reaches us in. A timeout is the obvious one,
# but the same overloaded machine also refuses a fork outright (EAGAIN, when the
# per-user process table is exhausted) or has the child killed under memory
# pressure (jetsam/OOM, which surfaces as a negative returncode). All three say
# "the machine was too busy", none says anything about its hardware, so none may
# be memoised.
#
# The distinction is NOT exception-vs-exit-code, which was this module's second
# wrong cut at it: FileNotFoundError (no sysctl — every Windows host) and
# PermissionError (a sandbox denial) are permanent for this process and MUST be
# memoised, or the memo never engages and menu rebuilds fork forever.
_TRANSIENT_ERRNOS = frozenset({errno.EAGAIN, errno.ENOMEM})

# Retries back off, and that is the point rather than politeness. The congestion
# that loses the first probe is a boot storm — Spotlight reindexing after an OS
# update, an MDM scan, Time Machine — and those last minutes, not seconds. A flat
# interval with a small cap spends the whole budget inside the storm and settles
# on the wrong answer just as the machine calms down, which is the same defect as
# a count-based budget wearing a clock. This schedule keeps trying past the hour
# for a total of four forks.
_RETRY_BACKOFF_SECONDS = (60.0, 300.0, 1800.0)


def _monotonic() -> float:
    """Indirected so tests can advance the clock without sleeping."""
    return time.monotonic()


def _read_proc_translated_cached() -> Optional[str]:
    """``_read_proc_translated`` probed at most once per process, plus retries.

    The answer cannot change while this process lives, so re-probing is pure
    waste. A CONCLUSIVE failure is cached too — same rule, same reason, as
    ``hardware_serial.get_hardware_serial``: on a genuine Intel Mac the key does
    not exist and never will, so a probe that retried would spawn a subprocess
    on every menu rebuild forever.

    CONGESTION is a different thing wearing the same return value, and memoising
    it silently reinstates the bug this module exists to fix: the machine reads
    as untranslated for the rest of the session, the Diagnostics row states
    "Intel" about an Apple Silicon Mac, and the next update check re-selects the
    Intel DMG.

    The retry is gated on ELAPSED TIME, not on a call count, and that distinction
    is the whole point. The two probes of a launch are consecutive statements —
    ``main.run`` warms the memo and then ``tray.run_blocking`` builds the menu
    milliseconds later — so a count-based budget is spent entirely inside the
    congested window that caused the timeout, and the retry lands in the same
    conditions as the failure. Waiting instead puts the next attempt somewhere
    the machine is no longer busy, which is the only place it can do any good,
    and keeps rapid menu rebuilds fork-free in the meantime.

    The interval BACKS OFF for the same reason it exists. This is a login-item
    daemon that then runs for days; a flat 60s with a small cap would exhaust the
    whole budget two minutes after launch, i.e. still inside the boot storm that
    lost the first probe, which is the very failure the previous paragraph
    rejects. The schedule runs past the hour and then settles.
    """
    global _cached_raw, _probed, _probe_attempts, _last_transient_at

    with _lock:
        if _probed:
            return _cached_raw

        # Inside the backoff window every menu rebuild reuses the last answer
        # rather than forking under _menu_lock, which is the cost the memo exists
        # to prevent. There IS no last answer yet — _cached_raw is only ever
        # assigned alongside _probed — so this returns None, i.e. "not
        # translated", which is the safe direction while we do not know.
        if _last_transient_at is not None:
            wait = _RETRY_BACKOFF_SECONDS[min(_probe_attempts, len(_RETRY_BACKOFF_SECONDS)) - 1]
            if _monotonic() - _last_transient_at < wait:
                return _cached_raw

        result = _read_proc_translated()
        _probe_attempts += 1

        if result.conclusive or _probe_attempts > len(_RETRY_BACKOFF_SECONDS):
            _cached_raw = result.raw
            _probed = True
            _last_transient_at = None
        else:
            _last_transient_at = _monotonic()
            wait = _RETRY_BACKOFF_SECONDS[min(_probe_attempts, len(_RETRY_BACKOFF_SECONDS)) - 1]
            logger.debug(
                f"{_PROC_TRANSLATED_KEY} probe hit congestion "
                f"({_probe_attempts}/{len(_RETRY_BACKOFF_SECONDS) + 1}) — not "
                f"memoising, so a busy launch cannot pin this process to the "
                f"wrong architecture; retrying no sooner than {wait:.0f}s from now"
            )

        return result.raw


def reset_cache_for_tests() -> None:
    """Drop the memo. Tests only — the architecture never changes at runtime."""
    global _cached_raw, _probed, _probe_attempts, _last_transient_at
    with _lock:
        _cached_raw = None
        _probed = False
        _probe_attempts = 0
        _last_transient_at = None


def _read_proc_translated() -> ProbeResult:
    """Return the raw ``sysctl.proc_translated`` value and whether it settles it.

    ``raw`` is None whenever the value could not be determined — a missing key
    (real Intel Mac), a sysctl that is not on PATH, a timeout, or a sandbox that
    blocks the call. Callers must treat None as *not translated*; see
    ``is_rosetta_translated``.

    ``conclusive`` separates "asked and answered no" from "never got an answer",
    and only a TIMEOUT is the latter. A missing binary or a denied call is an
    answer: sysctl will not appear mid-session and a sandbox will not lift, so
    treating those as retryable buys nothing and costs a fork on every menu
    rebuild — on Windows, where sysctl does not exist at all, it also breaks the
    memo outright.
    """
    try:
        result = subprocess.run(
            ["sysctl", "-n", _PROC_TRANSLATED_KEY],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except subprocess.TimeoutExpired as exc:
        # The machine was too busy to answer in two seconds.
        logger.debug(f"Timed out reading {_PROC_TRANSLATED_KEY}: {exc}")
        return ProbeResult(None, conclusive=False)
    except OSError as exc:
        # Same congestion, different spelling: an overloaded machine refuses the
        # fork itself with EAGAIN once the per-user process table is full, and
        # returns ENOMEM under memory pressure. Reproduced under RLIMIT_NPROC as
        # BlockingIOError, which is an OSError and was being memoised as though
        # the hardware had answered.
        #
        # Everything else here IS permanent for this process — FileNotFoundError
        # (no sysctl at all, i.e. every Windows host) and PermissionError (a
        # sandbox denial) will not resolve mid-session, and treating them as
        # retryable stops the memo engaging and forks on every menu rebuild.
        transient = exc.errno in _TRANSIENT_ERRNOS
        logger.debug(f"Could not read {_PROC_TRANSLATED_KEY} (errno={exc.errno}): {exc}")
        return ProbeResult(None, conclusive=not transient)
    except subprocess.SubprocessError as exc:
        logger.debug(f"Could not read {_PROC_TRANSLATED_KEY}: {exc}")
        return ProbeResult(None, conclusive=True)

    if result.returncode < 0:
        # Killed by a signal — jetsam/OOM under the same memory pressure that
        # produces the timeout. The hardware never got a word in.
        logger.debug(f"{_PROC_TRANSLATED_KEY} probe killed by signal {-result.returncode}")
        return ProbeResult(None, conclusive=False)

    if result.returncode != 0:
        # Expected on Intel Macs: "second level name 'proc_translated' in
        # 'sysctl.proc_translated' is invalid". Not an error worth logging loudly.
        return ProbeResult(None, conclusive=True)

    return ProbeResult(result.stdout.strip(), conclusive=True)


def is_rosetta_translated(
    system: Optional[str] = None,
    sysctl_reader: Optional[Callable[[], Optional[str]]] = None,
) -> bool:
    """True only when this process is provably running under Rosetta 2.

    Fails toward **False** on every uncertainty, and that direction is
    deliberate. A false "translated" is actively harmful in a way a false
    "native" is not: it would make the updater hand an arm64 DMG to a genuine
    Intel Mac, and there is no reverse Rosetta — that binary simply will not
    run, turning a working install into a dead one. A false "native" merely
    preserves today's behaviour.

    Args:
        system: Override ``platform.system()`` for testing (e.g. "Darwin").
        sysctl_reader: Override the sysctl probe for testing. Returns the raw
            string value, or None when unreadable.
    """
    system = system or platform.system()
    if system != "Darwin":
        # Rosetta 2 is macOS-only. Windows-on-ARM emulation is a separate
        # mechanism and is not something this agent ships a second build for.
        return False

    reader = sysctl_reader or _read_proc_translated_cached
    return reader() == "1"


def true_machine_arch(
    system: Optional[str] = None,
    machine: Optional[str] = None,
    translated: Optional[bool] = None,
    sysctl_reader: Optional[Callable[[], Optional[str]]] = None,
) -> str:
    """The architecture of the HARDWARE, not of the running process.

    Returns ``platform.machine()`` unchanged everywhere except the one case it
    gets wrong: an x86_64 process translated by Rosetta 2, which is really
    running on arm64 silicon.

    Args:
        system: Override ``platform.system()`` for testing.
        machine: Override ``platform.machine()`` for testing.
        translated: Override the Rosetta determination for testing.
        sysctl_reader: Override the sysctl probe for testing.
    """
    system = system or platform.system()
    machine = machine or platform.machine()

    if translated is None:
        translated = is_rosetta_translated(system=system, sysctl_reader=sysctl_reader)

    if translated:
        return ARM64
    return machine
