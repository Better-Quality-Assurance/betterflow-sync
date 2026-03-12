"""Entry point for PyInstaller bundle.

Sets up import paths for the frozen environment, then delegates
to the canonical application class in main.py.
"""

import os
import sys

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
        except Exception:
            pass
        sys.exit(1)

# Now import and run the canonical app from main.py
from main import main  # noqa: E402

if __name__ == "__main__":
    main()
