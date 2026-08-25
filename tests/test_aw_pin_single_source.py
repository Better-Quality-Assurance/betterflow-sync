"""The ActivityWatch pin must have exactly ONE definition.

It used to have two, and nothing compared them:

    src/aw_manager.py       AW_VERSION + RELEASE_ASSETS   <- verify_tracker_pins.py checks this
    scripts/download_aw.py  a second AW_VERSION + assets  <- build.yml FETCHES with this

So the nightly pin guard verified digests for a version the build did not
necessarily download. They happened to agree, which is not the same as being
unable to disagree — and a bump that updated one and not the other would have
shipped binaries whose hashes the agent then refused, disabling capture on the
whole fleet while every test stayed green.

These are source-shape guards, which cannot prove behaviour. They are here
because the failure they cover is invisible at runtime until a bump, and by
then it is a fleet-wide outage rather than a red test.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
SCRIPTS = REPO / "scripts"

# `AW_VERSION = "..."` — an ASSIGNMENT, not an import or a use.
_ASSIGNMENT = re.compile(r"^\s*AW_VERSION\s*=\s*[\"']", re.MULTILINE)


def _python_files():
    for base in (SRC, SCRIPTS):
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


def test_only_one_file_defines_the_version():
    # encoding="utf-8" explicitly, matching the house convention. `read_text()`
    # uses the platform default, which is cp1252 on Windows — and this codebase
    # is full of UTF-8 em-dashes, so the bare form raises UnicodeDecodeError
    # there while passing on macOS and Linux. It does not show up on the `test`
    # job either, since that is Linux-only: the Windows leg runs only in the
    # release-tag build, so a source-scanning guard written without this is
    # green until the day someone cuts a release.
    definers = [
        p for p in _python_files() if _ASSIGNMENT.search(p.read_text(encoding="utf-8"))
    ]
    assert [p.name for p in definers] == ["aw_release.py"], (
        "AW_VERSION must be assigned in src/aw_release.py and nowhere else; "
        f"found assignments in {[str(p.relative_to(REPO)) for p in definers]}"
    )


def test_the_guard_can_actually_see_a_second_definition(tmp_path):
    """Positive control for the pattern above.

    A regex that matched nothing would pass `test_only_one_file_defines_the_version`
    forever while the drift returned. Assert the pattern fires on the exact
    shape it is meant to catch, and does NOT fire on an import of the name.
    """
    assert _ASSIGNMENT.search('AW_VERSION = "v0.13.2"\n')
    assert _ASSIGNMENT.search("AW_VERSION = 'v0.14.0b4'\n")
    assert not _ASSIGNMENT.search("from src.aw_release import AW_VERSION\n")
    assert not _ASSIGNMENT.search("print(AW_VERSION)\n")


def test_the_build_script_and_the_agent_resolve_the_same_pin():
    """The property the guard above only approximates.

    Source shape says "one assignment"; this says the two consumers actually
    end up with the same object. Both matter: a second literal could be added
    under a different name, and an import could be rewired to a stale copy.
    """
    import sys

    sys.path.insert(0, str(REPO))
    sys.path.insert(0, str(SCRIPTS))

    import download_aw

    from src import aw_manager, aw_release

    assert aw_manager.AW_VERSION is aw_release.AW_VERSION
    assert download_aw.AW_VERSION is aw_release.AW_VERSION
    assert download_aw.RELEASE_ASSETS is aw_release.RELEASE_ASSETS
    assert aw_manager.RELEASE_SHA256 is aw_release.RELEASE_SHA256
    # The build no longer names the digest table at all: it calls the shared
    # fail-closed check, which reads the pins next to it. Same pin, and now the
    # same rule enforcing it.
    assert download_aw.digest_mismatch is aw_release.digest_mismatch


@pytest.mark.parametrize(
    "key,expected_suffix",
    [
        ("darwin-arm64", "-macos-arm64.zip"),
        ("darwin-x86_64", "-macos-x86_64.zip"),
        ("windows", "-windows-x86_64.zip"),
        ("linux", "-linux-x86_64.zip"),
    ],
)
def test_every_key_names_the_archive_it_claims(key, expected_suffix):
    """A transposed pair here hands Apple Silicon the Intel archive, which is
    #216 restored in one character and invisible to every other test."""
    from src.aw_release import RELEASE_ASSETS

    assert RELEASE_ASSETS[key].endswith(expected_suffix)


def test_windows_and_linux_have_no_architecture_dimension():
    """Deliberate asymmetry, pinned so it is not 'tidied' into symmetry.

    Windows on ARM emulates x64 transparently, so keying Windows on arm64 would
    find no asset and break a configuration that works today. Upstream
    publishes no linux-arm64 at all.
    """
    from src.aw_release import asset_key

    assert asset_key(system="Windows", machine="ARM64") == "windows"
    assert asset_key(system="Windows", machine="AMD64") == "windows"
    assert asset_key(system="Linux", machine="aarch64") == "linux"
    assert asset_key(system="Linux", machine="x86_64") == "linux"


def test_asset_arch_is_the_inverse_of_asset_key():
    """`arch_mismatch` decides what "the right architecture" is entirely from
    this function, and it is the only thing stopping a second `make dmg` in one
    worktree from bundling the other leg's trackers. Answering the platform
    instead of the arch — or None for a `darwin-*` key — makes that check say
    False, the download is skipped, and the arm64 DMG ships Intel trackers.
    `asset_key` has coverage in both directions; without this its inverse had
    none."""
    from src.aw_release import asset_arch, asset_key

    for machine in ("arm64", "x86_64"):
        assert asset_arch(asset_key(system="Darwin", machine=machine)) == machine
    # Windows and Linux keys carry no architecture (see the module docstring).
    # Guessing one for them would re-download on every build of those legs.
    assert asset_arch("windows") is None
    assert asset_arch("linux") is None


def test_macos_picks_the_archive_matching_the_running_build():
    from src.aw_release import asset_key

    assert asset_key(system="Darwin", machine="arm64") == "darwin-arm64"
    assert asset_key(system="Darwin", machine="x86_64") == "darwin-x86_64"
    # An unexpected value takes the Intel build, which Rosetta can translate,
    # rather than an arm64 one that a non-arm64 host could never execute.
    assert asset_key(system="Darwin", machine="") == "darwin-x86_64"
