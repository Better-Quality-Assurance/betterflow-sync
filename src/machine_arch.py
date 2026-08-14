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
# Did the kernel ever answer? False means `_cached_raw is None` records "we never
# found out" rather than "not translated". Those two states share a return value
# and only one of them should make the updater withhold a build — see
# `probe_settled_unresolved`. Tracks ProbeResult.determined, NOT .conclusive:
# a sandbox denial is conclusive (stop asking) and undetermined (we know
# nothing), and reading the retry flag here is what let an EMFILE fork failure
# masquerade as a hardware answer.
_cached_determined: bool = True


class ProbeResult(NamedTuple):
    """What the probe said, whether the kernel answered, and whether to retry.

    Two INDEPENDENT questions, and collapsing them is a live defect rather than
    a tidiness point:

    - ``determined`` — did we learn the hardware's answer? True only where the
      kernel actually replied: a value, or the "no such key" that IS the answer
      on a genuine Intel Mac.
    - ``conclusive`` — is another fork worth it? A retry-policy decision, and
      deliberately True for some UNDETERMINED outcomes: no sysctl on PATH and a
      sandbox denial will not lift mid-session, so retrying costs a fork per
      menu rebuild and buys nothing.

    They were one field until an EMFILE fork failure — the file-descriptor twin
    of the EAGAIN case this module already reasons about — took the "permanent"
    branch and so reported a *confident* architecture for a machine that had
    never been asked. That is the exact shape of the bug the Rosetta work
    exists to fix, so the epistemic question now has its own field and
    ``probe_settled_unresolved`` reads THAT one.

    Returned as a value rather than signalled through module state so the
    classification cannot be read out of order, and so the producer has no side
    effect its docstring has to warn about.
    """

    raw: Optional[str]
    determined: bool
    conclusive: bool


# The two OSErrors that are PERMANENT for this process. No sysctl on PATH (every
# Windows host) and a sandbox/EDR denial will not lift mid-session, so they must
# be memoised or the memo never engages and menu rebuilds fork forever.
#
# Stated as an allowlist of the permanent cases, NOT as a list of the transient
# ones. It used to be `_TRANSIENT_ERRNOS = {EAGAIN, ENOMEM}`, and the set of ways
# a busy Mac can refuse a fork is not enumerable: EMFILE and ENFILE (the file-
# descriptor twins of the EAGAIN case the paragraph below describes) fell
# straight through it into "permanent", which is the wrong direction on both
# axes — no retry AND a confident answer nobody obtained. Everything unlisted is
# now treated as congestion: retried within the budget, and never determined.
#
# Matched on TYPE rather than errno on purpose. `FileNotFoundError("sysctl not
# found")` raised without an errno has `exc.errno is None`, so an errno-keyed
# test silently reclassifies it as congestion and reinstates the Windows
# fork-per-rebuild regression.
_PERMANENT_OSERRORS = (FileNotFoundError, PermissionError)

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
    global _cached_raw, _probed, _probe_attempts, _last_transient_at, _cached_determined

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
            # Record whether anyone ever answered. Exhausting the budget memoises
            # `raw=None` exactly like a genuine "no such key" does, and from here
            # on the two are indistinguishable by return value alone — which is
            # how a congested Mac ends up confidently reporting itself as Intel.
            # `.determined`, never `.conclusive`: this branch is also reached by
            # the permanent-but-unanswered cases (no sysctl, sandbox denial).
            _cached_determined = result.determined
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
    global _cached_raw, _probed, _probe_attempts, _last_transient_at, _cached_determined
    with _lock:
        _cached_raw = None
        _probed = False
        _probe_attempts = 0
        _last_transient_at = None
        _cached_determined = True


def probe_settled_unresolved() -> bool:
    """True once we have STOPPED asking and the kernel never answered.

    Deliberately not "the probe has failed so far". A machine that lost one
    sysctl to a boot storm is mid-backoff, not undetermined — the retry schedule
    exists precisely to cover that, and reporting it as unknown would withhold
    updates from a Mac that is merely busy. Only the settled state qualifies,
    and it is permanent for the life of the process.

    Two ways to reach it, and the second is easy to miss: the retry budget ran
    out on a machine that stayed congested, OR the probe hit a permanent
    obstacle that is not an answer (no sysctl on PATH, a sandbox/EDR denial).
    Both stop the asking; neither learned anything. Hence ``_cached_determined``
    rather than the retry flag.

    Split out rather than folded into ``true_machine_arch`` so it can be
    asserted directly, and because the settled-unresolved state is a fact about
    the probe rather than about any one caller's architecture question. Note the
    tray does NOT call this — it reads the ``""`` that ``true_machine_arch``
    returns, which is the better shape: one translation of this state into a
    caller-facing answer, not two that can drift.
    """
    with _lock:
        # `_probed and` is belt-and-braces: `_cached_determined` is only set
        # False in the branch that also sets `_probed = True`, so a mutant
        # dropping it survives the suite. Kept because the pair is the actual
        # invariant, and the default `True` protects the un-probed state only by
        # coincidence of its initial value.
        return _probed and not _cached_determined


def _read_proc_translated() -> ProbeResult:
    """Return the raw ``sysctl.proc_translated`` value and whether it settles it.

    ``raw`` is None whenever the value could not be determined — a missing key
    (real Intel Mac), a sysctl that is not on PATH, a timeout, or a sandbox that
    blocks the call. Callers must treat None as *not translated*; see
    ``is_rosetta_translated``.

    ``determined`` says whether the KERNEL answered, and only the two
    ``returncode`` paths below qualify: a value, or the non-zero exit that is
    the genuine "no such key" of an Intel Mac. Every exception path leaves us
    knowing nothing about the hardware, whatever we decide about retrying.

    ``conclusive`` is the separate retry decision, and is deliberately True for
    two UNDETERMINED outcomes: a missing binary and a denied call will not
    resolve mid-session, so treating them as retryable buys nothing and costs a
    fork on every menu rebuild — on Windows, where sysctl does not exist at all,
    it also breaks the memo outright. "Stop asking" and "we found out" are not
    the same claim, and `probe_settled_unresolved` reads the second one.
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
        return ProbeResult(None, determined=False, conclusive=False)
    except _PERMANENT_OSERRORS as exc:
        # No sysctl at all (every Windows host) or a sandbox/EDR denial. Neither
        # resolves mid-session, so stop asking — but neither is an answer about
        # the hardware, so this is emphatically not `determined`.
        logger.debug(f"Cannot read {_PROC_TRANSLATED_KEY} on this host: {exc}")
        return ProbeResult(None, determined=False, conclusive=True)
    except OSError as exc:
        # Congestion in one of its many spellings: an overloaded machine refuses
        # the fork with EAGAIN once the per-user process table is full, ENOMEM
        # under memory pressure, EMFILE/ENFILE when the fd table is at its cap.
        # Reproduced under RLIMIT_NPROC as BlockingIOError. Anything that is not
        # one of the two permanent cases above is treated this way, because the
        # ways a busy machine can refuse are not enumerable and the cost of
        # guessing wrong is a confident answer nobody obtained.
        logger.debug(f"Could not read {_PROC_TRANSLATED_KEY} (errno={exc.errno}): {exc}")
        return ProbeResult(None, determined=False, conclusive=False)
    except subprocess.SubprocessError as exc:
        logger.debug(f"Could not read {_PROC_TRANSLATED_KEY}: {exc}")
        return ProbeResult(None, determined=False, conclusive=True)

    if result.returncode < 0:
        # Killed by a signal — jetsam/OOM under the same memory pressure that
        # produces the timeout. The hardware never got a word in.
        logger.debug(f"{_PROC_TRANSLATED_KEY} probe killed by signal {-result.returncode}")
        return ProbeResult(None, determined=False, conclusive=False)

    if result.returncode != 0:
        # Expected on Intel Macs: "second level name 'proc_translated' in
        # 'sysctl.proc_translated' is invalid". Not an error worth logging loudly
        # — and it IS the answer, which is why this is determined.
        return ProbeResult(None, determined=True, conclusive=True)

    return ProbeResult(result.stdout.strip(), determined=True, conclusive=True)


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

    Returns ``platform.machine()`` unchanged everywhere except two cases: an
    x86_64 process translated by Rosetta 2, which is really running on arm64
    silicon; and a Mac whose Rosetta probe never resolved, which returns ``""``.

    **The empty string means "we could not determine it", and callers must not
    read it as an architecture.** ``platform.machine()`` is documented to return
    ``""`` for the same reason, so this reuses that vocabulary rather than
    inventing a third one, and ``update_checker._is_wrong_arch`` already refuses
    every arch-suffixed asset when handed it. Before this, that safety branch was
    reachable only through a ``platform.machine()`` value that does not occur on
    macOS: the probe's own doubt was discarded one function earlier, so a
    congested Mac reported a confident ``"x86_64"`` and was offered the Intel
    build — the exact outcome the Rosetta work exists to prevent.

    Only the SETTLED-unresolved state qualifies (see ``probe_settled_unresolved``);
    a machine still inside its retry backoff keeps reporting its process
    architecture, because withholding updates from a Mac that is briefly busy
    would be a worse trade than the one this guards against.

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

    # Not translated — but on a Mac that can mean "asked and answered no" or
    # "never got an answer". Only the second is undetermined.
    #
    # The two overrides are NOT symmetric here, which is worth stating because
    # it looks like an oversight:
    #
    #   sysctl_reader= replaces the probe itself, so the module memo describes a
    #     DIFFERENT probe than the one that produced `translated`. Mixing them
    #     would report this process's doubt about a reader the caller never ran.
    #     Skipped.
    #   translated= overrides only the derived boolean. No reader ran, so the
    #     module memo is still the only — and the correct — record of whether
    #     THIS process ever got an answer. Consulted.
    #
    # That is what lets the tray pass `translated=` to control its Rosetta
    # wording while still reflecting the real probe's doubt in the arch row.
    if system == "Darwin" and sysctl_reader is None and probe_settled_unresolved():
        return ""

    return machine
