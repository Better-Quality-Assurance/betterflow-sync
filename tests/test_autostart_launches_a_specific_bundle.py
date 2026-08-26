"""#211: the LaunchAgent named one copy and macOS started another.

The plist said `open -a /Applications/BetterFlow.app`. The machine came up
running `/Applications/BetterFlow-1.5.119-backup.app` — a version below the
server's own minimum floor — and stayed there for days while every sync logged
"update required".

``open -a X`` does not mean "run the bundle at path X". ``-a`` names an
APPLICATION, and LaunchServices resolves it through its registration database,
where all three copies on that machine were registered under one bundle id
(``co.betterqa.betterflow``). Which copy wins is LaunchServices' choice, not
ours. Dropping ``-a`` makes the argument a path again: ``open <path>`` opens
that bundle and no other.

**Scope, stated honestly.** The issue marks the LaunchServices resolution as
INFERRED — tccd logs had aged out of unified-log retention, so nobody watched
macOS pick the wrong copy. This change is therefore not "the proven fix for that
morning"; it is removing the only degree of freedom the launch command had. It
is strictly more precise whether or not it was the cause, which is why it is
worth making before the mechanism can be witnessed again.

Guarding it because the argument is invisible at runtime: the wrong form
launches SOMETHING every time, usually the right copy, so nothing fails and no
log line records which bundle LaunchServices chose.
"""

from __future__ import annotations

import sys

import pytest

from src import autostart


@pytest.fixture
def bundled(monkeypatch):
    """Pretend we are the PyInstaller bundle, which is the only branch that
    builds an ``open`` command at all."""
    exe = "/Applications/BetterFlow.app/Contents/MacOS/BetterFlow"
    monkeypatch.setattr(sys, "executable", exe)
    return exe


def test_the_launch_command_names_a_path_not_an_application(bundled):
    """THE defect. ``-a`` hands the choice of copy to LaunchServices."""
    args = autostart._app_launch_args()

    assert "-a" not in args, (
        "open -a resolves through the LaunchServices database, so a duplicate "
        "registered under the same bundle id can be launched instead"
    )
    assert args == ["open", "/Applications/BetterFlow.app"]


def test_the_bundle_path_is_still_derived_from_the_running_executable(bundled):
    """The control: dropping ``-a`` must not change WHICH bundle we name. A fix
    that hardcoded a path would pass the test above and break every install
    that lives somewhere other than /Applications."""
    monkey_exe = "/Users/someone/Applications/BetterFlow.app/Contents/MacOS/BetterFlow"
    import sys as _sys

    original = _sys.executable
    try:
        _sys.executable = monkey_exe
        assert autostart._app_launch_args() == [
            "open",
            "/Users/someone/Applications/BetterFlow.app",
        ]
    finally:
        _sys.executable = original


def test_a_non_bundled_run_is_untouched(monkeypatch):
    """Running from source must still exec the interpreter, not `open`."""
    monkeypatch.setattr(sys, "executable", "/usr/local/bin/python3")

    assert autostart._app_launch_args() == ["/usr/local/bin/python3", "-m", "src.main"]


def test_an_existing_plist_written_with_the_old_form_reads_as_not_current(
    bundled, monkeypatch, tmp_path
):
    """The migration, which is the whole reason this is safe to ship.

    Every device in the fleet has a plist carrying the ``-a`` form.
    ``_macos_plist_is_current`` compares the installed ProgramArguments against
    ``_app_launch_args()``, so changing the latter makes every existing plist
    read as stale and ``ensure_synced()`` rewrites it on the next startup. No
    migration code, but also no silent no-op: if this assertion ever flips, the
    fleet keeps the old command forever and the fix reaches nobody.
    """
    import plistlib

    plist = tmp_path / "co.betterqa.betterflow.plist"
    plist.write_bytes(
        plistlib.dumps(
            {"ProgramArguments": ["open", "-a", "/Applications/BetterFlow.app"]}
        )
    )
    monkeypatch.setattr(autostart, "_plist_path", lambda: plist)

    assert autostart._macos_plist_program_args() == [
        "open", "-a", "/Applications/BetterFlow.app",
    ]
    assert autostart._macos_plist_is_current() is False
