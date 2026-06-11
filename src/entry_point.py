"""Entry point for PyInstaller bundle.

Sets up import paths for the frozen environment, then delegates
to the canonical application class in main.py.
"""

import faulthandler
import os
import signal
import sys

# Enable faulthandler at startup so a hard crash (segfault, abort) writes
# a Python-level traceback to stderr instead of dying silently. Also
# register SIGUSR1 to dump stacks of every thread on demand — the user
# can `kill -USR1 <pid>` to capture a snapshot of a hung process without
# needing a debugger attached. Without this, the only way to investigate
# a hung-thread scenario (sync loop stops logging, AppKit keeps spinning)
# was to attach lldb live — which by the time the user notices is usually
# too late.
faulthandler.enable()
if hasattr(signal, "SIGUSR1"):
    # all_threads=True is the whole point — a deadlock between the sync
    # thread and a tracker callback only becomes visible when we see
    # both stacks at once.
    faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)

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

# Now import and run the canonical app from main.py
from main import main  # noqa: E402

if __name__ == "__main__":
    main()
