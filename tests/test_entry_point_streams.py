"""The entry point must survive a windowed build with no console.

A windowed PyInstaller build (console=False) on Windows attaches no console,
so the bootloader leaves sys.stdout and sys.stderr as None. The very first
startup line, faulthandler.enable(), then raised
"RuntimeError: sys.stderr is None" and the app crashed before the tray icon
ever appeared (Claudia Malau, Windows, 2026-06-12). These pin the guard that
fixes it.
"""

import faulthandler
import sys

from src.entry_point import _ensure_std_streams


def test_none_streams_are_replaced_with_writable_sinks():
    orig_out, orig_err = sys.stdout, sys.stderr
    try:
        sys.stdout = None  # type: ignore[assignment]
        sys.stderr = None  # type: ignore[assignment]

        _ensure_std_streams()

        assert sys.stdout is not None, "stdout must be repaired"
        assert sys.stderr is not None, "stderr must be repaired"
        # Writable without raising — the condition every stderr write needs.
        sys.stdout.write("ok")
        sys.stderr.write("ok")
        # The original crash site: faulthandler.enable() defaults to sys.stderr
        # and must no longer see None.
        faulthandler.enable()
    finally:
        # Restore pytest's captured streams and faulthandler's default target.
        sys.stdout, sys.stderr = orig_out, orig_err
        faulthandler.enable()


def test_real_streams_are_left_untouched():
    orig_out, orig_err = sys.stdout, sys.stderr

    _ensure_std_streams()

    assert sys.stdout is orig_out, "a present stdout must not be replaced"
    assert sys.stderr is orig_err, "a present stderr must not be replaced"
