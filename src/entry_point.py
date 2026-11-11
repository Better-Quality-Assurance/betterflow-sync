"""Entry point for PyInstaller bundle.

Sets up import paths for the frozen environment, then delegates
to the canonical application class in main.py.
"""

import faulthandler
import io
import os
import signal
import sys


def _ensure_std_streams() -> None:
    """Guarantee sys.stdout/sys.stderr are real, writable streams.

    A windowed PyInstaller build (console=False) on Windows has NO console
    attached, so the bootloader leaves sys.stdout and sys.stderr as None.
    The very next line, faulthandler.enable(), writes to sys.stderr and
    raises "RuntimeError: sys.stderr is None" — crashing the app on launch
    before any window or tray icon appears (Claudia Malau, Windows,
    2026-06-12). The same None streams would also blow up the first stderr
    write or any library that logs to them.

    Point the missing streams at the null device (or an in-memory buffer if
    even that fails) so startup is robust regardless of how the app was
    launched. Must run before ANYTHING touches the streams.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w"))
            except OSError:
                setattr(sys, name, io.StringIO())


_ensure_std_streams()

# Enable faulthandler at startup so a hard crash (segfault, abort) writes
# a Python-level traceback to stderr instead of dying silently. Also
# register SIGUSR1 to dump stacks of every thread on demand — the user
# can `kill -USR1 <pid>` to capture a snapshot of a hung process without
# needing a debugger attached. Without this, the only way to investigate
# a hung-thread scenario (sync loop stops logging, AppKit keeps spinning)
# was to attach lldb live — which by the time the user notices is usually
# too late.
#
# Wrapped defensively: faulthandler.enable() needs a valid stderr, and a
# diagnostic aid must never be the thing that crashes startup.
try:
    faulthandler.enable()
    if hasattr(signal, "SIGUSR1"):
        # all_threads=True is the whole point — a deadlock between the sync
        # thread and a tracker callback only becomes visible when we see
        # both stacks at once.
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
except (RuntimeError, ValueError, OSError):
    # No usable stderr/file descriptor (e.g. a stripped windowed environment).
    # Lose the crash-dump aid rather than the app.
    pass

# Set up import path before any project imports.
# PyInstaller bundles everything under sys._MEIPASS; for normal
# (non-frozen) execution we add the src/ directory instead.
if getattr(sys, "frozen", False):
    bundle_dir = sys._MEIPASS  # type: ignore
    if bundle_dir not in sys.path:
        sys.path.insert(0, bundle_dir)
else:
    src_dir = os.path.dirname(os.path.abspath(__file__))
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

# Re-point faulthandler from stderr to a file in the log dir. On a windowed
# Windows build stderr is the null device (see _ensure_std_streams), so the
# enable() above writes every native crash to nowhere: the agent just vanishes
# (Sachi, 2026-11-11). main.py reports the previous run's dump on the next
# launch. _native_crash_file must stay referenced for the life of the process:
# faulthandler keeps only the descriptor, which closes if the object is freed.
# Wrapped like the enable() above: a diagnostic aid must never break startup.
# The --privacy-notice child is a throwaway Tk host the live agent spawns from
# its own executable; it must never own the agent's crash file. (enable() also
# refuses while another process holds the file, which covers a second launch
# turned away as "already running".)
_native_crash_file = None
if "--privacy-notice" not in sys.argv[1:]:
    try:
        from config import Config as _Config
        import native_crash_log as _native_crash_log

        _native_crash_file = _native_crash_log.enable(_Config.get_log_dir())
    except Exception as _e:
        # Logging is not configured yet; stderr is the only channel (and is the
        # null device on Windows, which is exactly the case the file is for).
        sys.stderr.write(f"BetterFlow: native crash log unavailable: {_e}\n")

# Warn if running directly from a DMG (macOS).  The mounted disk image
# can be ejected while the app is running, which causes a SIGBUS crash
# because the executable's memory-mapped code pages become invalid.
if getattr(sys, "frozen", False) and sys.platform == "darwin":
    _exe = os.path.realpath(sys.executable)
    if _exe.startswith("/Volumes/"):
        try:
            import tkinter as tk
            from tkinter import messagebox

            _root = tk.Tk()
            _root.withdraw()
            messagebox.showwarning(
                "BetterFlow",
                "Please drag BetterFlow to your Applications folder "
                "before launching.\n\n"
                "Running from the disk image will cause crashes.",
            )
            _root.destroy()
        except Exception as _e:
            # tkinter can be missing or the display not ready. We can't
            # log (logging isn't configured yet this early in startup),
            # so surface the warning to stderr at least.
            sys.stderr.write(
                f"BetterFlow: failed to show DMG warning dialog: {_e}\n"
            )
        sys.exit(1)

if __name__ == "__main__":
    # Import the canonical app lazily so importing this module (e.g. in tests
    # exercising the stream guard above) doesn't drag in the whole app and its
    # heavy dependencies.
    from main import main

    main()
