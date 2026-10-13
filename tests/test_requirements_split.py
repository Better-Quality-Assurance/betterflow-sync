"""The test dependency list must not pull in the Linux tray stack (#262).

``PyGObject`` has no wheel on Linux and is compiled against libgirepository and
cairo, so a test install that includes it cannot run in a container without a
C toolchain, and the suite is never reached. ``requirements-test.txt`` exists to
install what the tests import and nothing that needs compiling.

The other half of the contract matters as much: splitting the files must not
change what a real install gets. ``requirements.txt`` (what the Windows build,
the macOS signing docs and ``make install`` use) must still carry the tray
stack, and ``requirements-dev.txt`` (what the release build job uses before
running PyInstaller) must still resolve to the full runtime set plus tooling.

These files are parsed with a small reader that follows ``-r`` includes, so the
assertions are about the resolved package set, not the file text.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The Linux desktop/tray backends. PyGObject is the one that must be compiled;
# pystray and python-xlib are only reached through it or lazily inside
# functions no test calls for real (tests patch ``src.ui.tray.pystray``).
TRAY_STACK = {"pygobject", "pystray", "python-xlib"}

# What the suite imports directly or needs to run.
TEST_TOOLING = {"pytest", "pytest-mock", "pytest-cov", "responses", "time-machine"}

_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _resolve(path: Path, stack: tuple[Path, ...] = ()) -> set[str]:
    """Return the normalised package names a requirements file installs.

    ``stack`` is the current include chain, not every file seen: the dev file
    reaches requirements-core.txt twice (via requirements.txt and via
    requirements-test.txt), which is a diamond, not a cycle.
    """
    path = path.resolve()
    assert path not in stack, f"requirements include cycle at {path.name}"
    stack = (*stack, path)
    names: set[str] = set()
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r ", "--requirement ")):
            names |= _resolve(path.parent / line.split(None, 1)[1].strip(), stack)
            continue
        assert not line.startswith("-"), f"unhandled option in {path.name}: {line}"
        match = _NAME.match(line)
        assert match, f"unparseable requirement in {path.name}: {line}"
        names.add(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return names


def test_test_requirements_exclude_the_linux_tray_stack():
    test_set = _resolve(ROOT / "requirements-test.txt")
    assert not test_set & TRAY_STACK, sorted(test_set & TRAY_STACK)


def test_test_requirements_carry_the_test_tooling():
    test_set = _resolve(ROOT / "requirements-test.txt")
    assert test_set >= TEST_TOOLING, sorted(TEST_TOOLING - test_set)
    # tests import keyring.errors directly; it must stay.
    assert "keyring" in test_set


def test_runtime_install_still_carries_the_tray_stack():
    runtime = _resolve(ROOT / "requirements.txt")
    assert runtime >= TRAY_STACK, sorted(TRAY_STACK - runtime)


def test_test_set_is_the_runtime_set_minus_only_the_tray_stack():
    runtime = _resolve(ROOT / "requirements.txt")
    test_set = _resolve(ROOT / "requirements-test.txt")
    assert test_set - TEST_TOOLING == runtime - TRAY_STACK


def test_dev_install_is_runtime_plus_test_plus_build_tooling():
    dev = _resolve(ROOT / "requirements-dev.txt")
    runtime = _resolve(ROOT / "requirements.txt")
    test_set = _resolve(ROOT / "requirements-test.txt")
    assert dev == runtime | test_set | {"pyinstaller", "ruff"}
