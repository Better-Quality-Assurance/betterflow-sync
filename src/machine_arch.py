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
from typing import Callable, Optional

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

# Set by _read_proc_translated when the probe failed for a reason that says
# nothing about the hardware (sysctl raised — a timeout above all), as opposed
# to answering "no such key". See _read_proc_translated_cached.
_last_probe_transient = False
_probe_attempts = 0

# A permanently broken sysctl must still stop forking. Two attempts covers the
# one-off timeout this exists for without turning a hard failure into a loop.
_MAX_PROBE_ATTEMPTS = 2


def _read_proc_translated_cached() -> Optional[str]:
    """``_read_proc_translated`` probed at most once per process.

    The answer cannot change while this process lives, so re-probing is pure
    waste. A CONCLUSIVE failure is cached too — same rule, same reason, as
    ``hardware_serial.get_hardware_serial``: on a genuine Intel Mac the key does
    not exist and never will, so a probe that retried would spawn a subprocess
    on every menu rebuild forever.

    A TRANSIENT failure is a different thing wearing the same return value, and
    memoising it silently reinstates the bug this module exists to fix. sysctl
    raising says nothing about the hardware, but caching that ``None`` makes the
    machine read as untranslated for the rest of the session: the Diagnostics
    row states "Intel" about an Apple Silicon Mac, and the next update check
    re-selects the Intel DMG. The window is not theoretical — the warm-up fires
    at the busiest moment of launch, in a process that (in the affected case) is
    itself running through Rosetta, i.e. the slowest fork the machine will do.

    So the line is exit code vs exception, which is exactly the line between
    "asked and answered no" and "never got an answer".
    """
    global _cached_raw, _probed, _last_probe_transient, _probe_attempts

    with _lock:
        if _probed:
            return _cached_raw

        _last_probe_transient = False
        raw = _read_proc_translated()
        _probe_attempts += 1

        if not _last_probe_transient or _probe_attempts >= _MAX_PROBE_ATTEMPTS:
            _cached_raw = raw
            _probed = True
        else:
            logger.debug(
                f"{_PROC_TRANSLATED_KEY} probe failed transiently "
                f"({_probe_attempts}/{_MAX_PROBE_ATTEMPTS}) — not memoising, "
                "so a timeout cannot pin this process to the wrong architecture"
            )

        return raw


def reset_cache_for_tests() -> None:
    """Drop the memo. Tests only — the architecture never changes at runtime."""
    global _cached_raw, _probed, _last_probe_transient, _probe_attempts
    with _lock:
        _cached_raw = None
        _probed = False
        _last_probe_transient = False
        _probe_attempts = 0


def _read_proc_translated() -> Optional[str]:
    """Return the raw ``sysctl.proc_translated`` value, or None if unreadable.

    None means "could not determine" — a missing key (real Intel Mac), a sysctl
    that is not on PATH, a timeout, or a sandbox that blocks the call. Callers
    must treat None as *not translated*; see ``is_rosetta_translated``.
    """
    try:
        result = subprocess.run(
            ["sysctl", "-n", _PROC_TRANSLATED_KEY],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Transient: a timeout, a sandbox denial, sysctl briefly unavailable.
        # None of these are statements about the hardware, so the memo must not
        # keep this answer. subprocess.TimeoutExpired is a SubprocessError.
        global _last_probe_transient
        _last_probe_transient = True
        logger.debug(f"Could not read {_PROC_TRANSLATED_KEY}: {exc}")
        return None

    if result.returncode != 0:
        # Expected on Intel Macs: "second level name 'proc_translated' in
        # 'sysctl.proc_translated' is invalid". Not an error worth logging loudly.
        return None
    return result.stdout.strip()


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
