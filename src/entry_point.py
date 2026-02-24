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

# Now import and run the canonical app from main.py
from main import main  # noqa: E402

if __name__ == "__main__":
    main()
