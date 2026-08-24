"""Native arm64 trackers must run WITHOUT Rosetta 2 (#216).

The preflight added in #213 asks whether the HOST can execute x86_64. That was
correct exactly while every bundled macOS tracker was x86_64 — true from the
first release until the ActivityWatch pin moved to v0.14.0b4. From that moment
it is the wrong question, and asking it alone means a clean Apple Silicon Mac
with no Rosetta refuses to spawn arm64 binaries that would have run perfectly
well. The bump would have shipped native trackers and left Rosetta just as
mandatory as before.

`test_native_trackers_start_on_a_mac_with_no_rosetta` is the proof-of-failure
for that: it FAILS against the pre-#216 gate and passes after it.

Mach-O headers here are written for real rather than mocked. The production
code reads the header of the binary it is about to spawn, so a test that
patched that read would assert against its own answer — the exact shape of the
bug being fixed, one layer up.
"""

import os
import platform
import struct
import subprocess
from unittest.mock import patch

import pytest

from src.aw_manager import ALL_COMPONENTS, IDLE_TRACKER, AWManager
from src.machine_arch import ARM64, X86_64, macho_arches

_MH_MAGIC_64 = 0xFEEDFACF
_MH_CIGAM_64 = 0xCFFAEDFE  # the byte-swapped spelling; see test_reads_a_big_endian_header
_FAT_MAGIC = 0xCAFEBABE
_CPU_TYPE_X86_64 = 0x01000007
_CPU_TYPE_ARM64 = 0x0100000C


def _thin(cputype: int) -> bytes:
    return struct.pack("<II", _MH_MAGIC_64, cputype)


def _fat(*cputypes: int) -> bytes:
    """A universal binary header: big-endian magic, count, then fat_arch rows."""
    out = struct.pack(">II", _FAT_MAGIC, len(cputypes))
    for ct in cputypes:
        out += struct.pack(">IIIII", ct, 0, 0, 0, 0)
    return out


# The gate resolves each component through `_resolve_binary_path`, which
# appends ".exe" on Windows. Fixtures must name their launchers the way the
# running host really would, or the whole tree resolves to nothing on the
# Windows CI leg and every assertion below passes for the wrong reason.
_EXE = ".exe" if platform.system() == "Windows" else ""


def _tracker_tree(tmp_path, cputype: int, name: str) -> str:
    root = tmp_path / name
    for component in ALL_COMPONENTS:
        comp_dir = root / component
        comp_dir.mkdir(parents=True, exist_ok=True)
        (comp_dir / (component + _EXE)).write_bytes(_thin(cputype))
    return str(root)


# --------------------------------------------------------------------------
# The header reader. Its answers are what every decision below rests on, so it
# gets its own coverage rather than being exercised only through the gate.
# --------------------------------------------------------------------------


def test_reads_a_thin_arm64_binary(tmp_path):
    p = tmp_path / "a"
    p.write_bytes(_thin(_CPU_TYPE_ARM64))
    assert macho_arches(str(p)) == {ARM64}


def test_reads_a_thin_x86_64_binary(tmp_path):
    p = tmp_path / "b"
    p.write_bytes(_thin(_CPU_TYPE_X86_64))
    assert macho_arches(str(p)) == {X86_64}


def test_reads_a_universal_binary_as_both(tmp_path):
    p = tmp_path / "c"
    p.write_bytes(_fat(_CPU_TYPE_X86_64, _CPU_TYPE_ARM64))
    assert macho_arches(str(p)) == {X86_64, ARM64}


def test_a_real_arm64_binary_starts_with_the_bytes_we_expect():
    """Pins the on-disk layout the reader is written against.

    Verified against the shipped ActivityWatch arm64 binary:

        $ xxd -l 8 aw-server-rust
        00000000: cffa edfe 0c00 0001

    So a normal little-endian Mach-O stores `cf fa ed fe`, which reads as
    MH_MAGIC_64 little-endian, and its cputype follows little-endian. Writing
    this down stops the next person "fixing" the reader's endianness against an
    invented fixture — the first draft of the byte-swap test below did exactly
    that and failed against correct code.
    """
    assert _thin(_CPU_TYPE_ARM64) == bytes.fromhex("cffaedfe0c000001")


def test_reads_a_big_endian_header(tmp_path):
    # A big-endian Mach-O writes `fe ed fa cf`, which read little-endian is
    # MH_CIGAM_64 — the byte-swapped spelling — and its cputype is big-endian
    # too. Getting this backwards reads the cputype as garbage and silently
    # answers "could not tell" for a perfectly good binary.
    p = tmp_path / "d"
    p.write_bytes(struct.pack(">II", _MH_MAGIC_64, _CPU_TYPE_ARM64))
    assert struct.unpack("<I", p.read_bytes()[:4])[0] == _MH_CIGAM_64
    assert macho_arches(str(p)) == {ARM64}


@pytest.mark.parametrize(
    "content",
    [
        b"",                      # empty
        b"\x00\x01",              # shorter than a header
        b"#!/bin/sh\necho hi\n",  # a script, not Mach-O
        b"MZ\x90\x00\x03\x00\x00\x00",  # a PE binary
    ],
)
def test_unreadable_input_is_could_not_tell_not_an_architecture(tmp_path, content):
    p = tmp_path / "e"
    p.write_bytes(content)
    assert macho_arches(str(p)) == set()


def test_a_missing_file_is_could_not_tell(tmp_path):
    assert macho_arches(str(tmp_path / "nope")) == set()


def test_an_implausible_fat_count_is_rejected(tmp_path):
    # A corrupt or hostile nfat_arch must not make us read megabytes looking
    # for slices that are not there.
    p = tmp_path / "f"
    p.write_bytes(struct.pack(">II", _FAT_MAGIC, 100_000))
    assert macho_arches(str(p)) == set()


# --------------------------------------------------------------------------
# The gate.
# --------------------------------------------------------------------------


def _mgr() -> AWManager:
    mgr = AWManager(aw_port=5600)
    mgr._rosetta_missing_cached = None
    mgr._rosetta_notified = False
    return mgr


def _apple_silicon_without_rosetta():
    """An arm64 Mac whose `/usr/bin/arch -x86_64` probe FAILS — no Rosetta.

    The tracker directory is NOT patched in: the gate takes it as an argument.
    `_get_binaries_dir` is the installer as well as the resolver — in a frozen
    macOS build it can rewrite the whole persistent tracker tree before
    returning — so it is patched to RAISE here, and a regression that reaches
    for it from inside the query fails loudly instead of quietly reporting on
    binaries it just created.
    """
    return (
        patch("src.aw_manager.sys.platform", "darwin"),
        patch("src.aw_manager.platform.machine", return_value=ARM64),
        patch(
            "src.aw_manager.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=1),
        ),
        patch.object(
            AWManager,
            "_get_binaries_dir",
            side_effect=AssertionError("the Rosetta gate must not resolve or install binaries"),
        ),
    )


def test_native_trackers_start_on_a_mac_with_no_rosetta(tmp_path):
    """THE regression test for #216.

    Fails against the pre-fix gate, which returned True here because the host
    lacks Rosetta, and so refused to spawn binaries that need none.
    """
    native = _tracker_tree(tmp_path, _CPU_TYPE_ARM64, "native")
    mgr = _mgr()
    p1, p2, p3, p4 = _apple_silicon_without_rosetta()
    with p1, p2, p3, p4:
        # Precondition: the fixture really is the case under test.
        assert macho_arches(
            os.path.join(native, IDLE_TRACKER, IDLE_TRACKER + _EXE)
        ) == {ARM64}
        assert mgr._rosetta_required(native) is False


def test_x86_trackers_on_a_mac_with_no_rosetta_are_still_blocked(tmp_path):
    """The control. Without it the test above is satisfied by a gate that has
    simply been switched off, which would silently restore the 21-EBADARCH-
    failures-per-hour behaviour the preflight was built to end."""
    intel = _tracker_tree(tmp_path, _CPU_TYPE_X86_64, "intel")
    mgr = _mgr()
    p1, p2, p3, p4 = _apple_silicon_without_rosetta()
    with p1, p2, p3, p4:
        assert mgr._rosetta_required(intel) is True


def test_a_half_reinstalled_tree_is_blocked_rather_than_spawned(tmp_path):
    """A reinstall that fails partway leaves one component at the new
    architecture and another at the old one. Judging the tree by the first
    component we can read calls that healthy, so the start proceeds, the other
    component EBADARCHes every cycle, AFK capture is dead, and nothing is
    recorded about why. Every component has to agree."""
    mixed = tmp_path / "mixed"
    for i, component in enumerate(ALL_COMPONENTS):
        d = mixed / component
        d.mkdir(parents=True)
        # First component native, the rest left at the old architecture.
        d.joinpath(component + _EXE).write_bytes(
            _thin(_CPU_TYPE_ARM64 if i == 0 else _CPU_TYPE_X86_64)
        )
    mgr = _mgr()
    p1, p2, p3, p4 = _apple_silicon_without_rosetta()
    with p1, p2, p3, p4:
        assert mgr._bundled_trackers_need_rosetta(str(mixed)) is True


def test_a_flat_layout_install_is_judged_too(tmp_path):
    """The persistent tree can still be in the LEGACY FLAT layout.

    `_resolve_binary_path` accepts `<dir>/bf-idle-tracker` beside a `Python/`
    runtime dir, `_binaries_present` is built on it, and `_start_component`
    spawns whatever it returns. A gate that knew only the bundled layout would
    find no file for any component, answer "could not tell", and let exactly
    the stale x86_64 tree it exists to catch start unblocked — neither refused
    nor reinstalled, EBADARCH every cycle.
    """
    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "Python").mkdir()  # the adjacent runtime the flat layout needs
    for component in ALL_COMPONENTS:
        (flat / (component + _EXE)).write_bytes(_thin(_CPU_TYPE_X86_64))
    mgr = _mgr()
    p1, p2, p3, p4 = _apple_silicon_without_rosetta()
    with p1, p2, p3, p4:
        assert mgr._bundled_trackers_need_rosetta(str(flat)) is True


def test_x86_trackers_are_fine_once_rosetta_is_present(tmp_path):
    intel = _tracker_tree(tmp_path, _CPU_TYPE_X86_64, "intel2")
    mgr = _mgr()
    with patch("src.aw_manager.sys.platform", "darwin"), \
         patch("src.aw_manager.platform.machine", return_value=ARM64), \
         patch("src.aw_manager.subprocess.run",
               return_value=subprocess.CompletedProcess(args=[], returncode=0)):
        assert mgr._rosetta_required(intel) is False


def test_a_native_install_never_forks_the_rosetta_probe(tmp_path):
    """Cost, and the reason the cheap question is asked first.

    `_rosetta_required` runs on the 60-second start path. On a native install
    the binary check settles it from a few bytes of a local file, so
    `/usr/bin/arch` must never be spawned at all — and, because the host probe
    never runs, `capture_blocked_remedy()` offers no Rosetta instruction on a
    machine that does not need one.
    """
    native = _tracker_tree(tmp_path, _CPU_TYPE_ARM64, "native2")
    mgr = _mgr()
    with patch("src.aw_manager.sys.platform", "darwin"), \
         patch("src.aw_manager.platform.machine", return_value=ARM64), \
         patch("src.aw_manager.subprocess.run") as run:
        assert mgr._rosetta_required(native) is False
        arch_calls = [
            c for c in run.call_args_list
            if c.args and c.args[0] and c.args[0][0] == "/usr/bin/arch"
        ]
        assert arch_calls == [], "forked the Rosetta probe on a native install"
        assert mgr._rosetta_missing_cached is None
        assert mgr.capture_blocked_remedy() is None


def test_no_binaries_yet_does_not_block(tmp_path):
    """First run: nothing downloaded. The gate must not block, or a clean Mac
    never gets as far as downloading the arm64 archive that would fix it."""
    mgr = _mgr()
    p1, p2, p3, p4 = _apple_silicon_without_rosetta()
    with p1, p2, p3, p4:
        assert mgr._bundled_trackers_need_rosetta(None) is False
        assert mgr._rosetta_required(None) is False


def test_an_unreadable_binary_does_not_block(tmp_path):
    """A failed header read is 'could not tell', and must fail toward
    attempting the start — the same direction the Rosetta probe itself fails.
    A false block records nothing at all, which is the harm this area exists to
    prevent; a wrong start attempt still hits the EBADARCH handler."""
    junk = tmp_path / "junk"
    for component in ALL_COMPONENTS:
        d = junk / component
        d.mkdir(parents=True)
        (d / (component + _EXE)).write_bytes(b"not a mach-o")
    mgr = _mgr()
    p1, p2, p3, p4 = _apple_silicon_without_rosetta()
    with p1, p2, p3, p4:
        assert mgr._bundled_trackers_need_rosetta(str(junk)) is False


def test_non_macos_is_never_gated(tmp_path):
    intel = _tracker_tree(tmp_path, _CPU_TYPE_X86_64, "linuxish")
    mgr = _mgr()
    with patch("src.aw_manager.sys.platform", "linux"):
        assert mgr._rosetta_required(intel) is False


# --------------------------------------------------------------------------
# The upgrade path. Without this the bump helps nobody who already has the app.
# --------------------------------------------------------------------------


def test_an_existing_x86_install_is_replaced_by_the_native_bundle(tmp_path):
    """`_install_to_persistent` copies trackers to a stable path exactly once,
    so an Apple Silicon machine that installed BetterFlow before the bump keeps
    its x86_64 trackers after updating to a native build. Both copies are
    signed with our team, so the signing check sees nothing wrong — that Mac
    would need Rosetta forever."""
    installed = _tracker_tree(tmp_path, _CPU_TYPE_X86_64, "installed")
    bundled = _tracker_tree(tmp_path, _CPU_TYPE_ARM64, "bundled")
    with patch("src.aw_manager.platform.machine", return_value=ARM64):
        reason = AWManager._tracker_reinstall_reason(installed, bundled)
    assert reason is not None
    assert "cannot run natively" in reason


def test_a_matching_arch_is_not_reinstalled_for_arch_reasons(tmp_path):
    """The control. A needless reinstall costs the user a fresh Input
    Monitoring grant, so it must happen only when the installed copy genuinely
    cannot run here."""
    installed = _tracker_tree(tmp_path, _CPU_TYPE_ARM64, "ok-installed")
    bundled = _tracker_tree(tmp_path, _CPU_TYPE_ARM64, "ok-bundled")
    with patch("src.aw_manager.platform.machine", return_value=ARM64), \
         patch.object(AWManager, "_should_reinstall_for_signing", return_value=False):
        assert AWManager._tracker_reinstall_reason(installed, bundled) is None


def test_an_intel_mac_keeps_its_x86_trackers(tmp_path):
    """A genuine Intel Mac must not be told its x86_64 trackers are wrong."""
    installed = _tracker_tree(tmp_path, _CPU_TYPE_X86_64, "intel-installed")
    bundled = _tracker_tree(tmp_path, _CPU_TYPE_X86_64, "intel-bundled")
    with patch("src.aw_manager.platform.machine", return_value=X86_64), \
         patch.object(AWManager, "_should_reinstall_for_signing", return_value=False):
        assert AWManager._tracker_reinstall_reason(installed, bundled) is None


def test_an_unreadable_bundle_does_not_trigger_a_reinstall(tmp_path):
    """Fails toward KEEPING the installed copy: swapping on the strength of a
    failed probe would churn the binary and cost a re-grant for nothing."""
    installed = _tracker_tree(tmp_path, _CPU_TYPE_X86_64, "u-installed")
    bundled = tmp_path / "u-bundled"
    for component in ALL_COMPONENTS:
        d = bundled / component
        d.mkdir(parents=True)
        (d / (component + _EXE)).write_bytes(b"nope")
    with patch("src.aw_manager.platform.machine", return_value=ARM64), \
         patch.object(AWManager, "_should_reinstall_for_signing", return_value=False):
        assert AWManager._tracker_reinstall_reason(installed, str(bundled)) is None


def test_the_signing_reason_still_fires_and_says_so(tmp_path):
    """The pre-existing reason must survive, and must be reported as ITSELF.
    #213 exists because a surface named the wrong cause for 90 minutes; a
    single fixed log line for both reinstall reasons repeats that one layer
    down."""
    installed = _tracker_tree(tmp_path, _CPU_TYPE_ARM64, "s-installed")
    bundled = _tracker_tree(tmp_path, _CPU_TYPE_ARM64, "s-bundled")
    with patch("src.aw_manager.platform.machine", return_value=ARM64), \
         patch.object(AWManager, "_should_reinstall_for_signing", return_value=True):
        reason = AWManager._tracker_reinstall_reason(installed, bundled)
    assert reason is not None
    assert "Input Monitoring" in reason
    assert "cannot run natively" not in reason


# --------------------------------------------------------------------------
# The build side. The same rule, applied one step earlier — and the only thing
# standing between the arm64 DMG and a leftover Intel tracker tree.
# --------------------------------------------------------------------------


def _download_aw():
    """`scripts/download_aw.py`, loaded by path (it is not an importable pkg)."""
    import importlib.util

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts",
        "download_aw.py",
    )
    spec = importlib.util.spec_from_file_location("download_aw_arch_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _macos_tree(tmp_path, binaries, cputype: int, name: str) -> str:
    """A `resources/trackers/darwin` tree as the build script writes one."""
    root = tmp_path / name
    for binary in binaries:
        d = root / binary
        d.mkdir(parents=True)
        (d / binary).write_bytes(_thin(cputype))
    return str(root)


def test_the_build_redownloads_a_leftover_tree_from_the_other_macos_leg(tmp_path):
    """Build the x86_64 DMG and then the arm64 one in the same worktree and the
    first leg's trackers are still on disk — complete, correctly named, and
    wrong. `binaries_exist` says True, so `arch_mismatch` is the only thing
    stopping the arm64 DMG from shipping Intel trackers: #216 restored at build
    time, with every other test green."""
    dl = _download_aw()
    intel = _macos_tree(tmp_path, dl.BF_BINARIES, _CPU_TYPE_X86_64, "darwin")

    # Precondition: the stale tree really does look complete to the build.
    assert dl.binaries_exist(intel, "darwin") is True
    assert dl.arch_mismatch(intel, "darwin", "darwin-arm64") is True
    # The control: the leg that matches must not re-download on every build.
    assert dl.arch_mismatch(intel, "darwin", "darwin-x86_64") is False


def test_the_build_does_not_redownload_on_a_failed_probe(tmp_path):
    """Unreadable headers are 'could not tell'. Re-downloading on the strength
    of a failed probe would make every Windows/Linux build refetch too."""
    dl = _download_aw()
    junk = tmp_path / "junk"
    for binary in dl.BF_BINARIES:
        d = junk / binary
        d.mkdir(parents=True)
        (d / binary).write_bytes(b"not a mach-o")

    assert dl.arch_mismatch(str(junk), "darwin", "darwin-arm64") is False
    # Non-macOS platforms publish one architecture into their own directory, so
    # there is nothing to compare and the key carries no arch to compare with.
    intel = _macos_tree(tmp_path, dl.BF_BINARIES, _CPU_TYPE_X86_64, "windows")
    assert dl.arch_mismatch(intel, "windows", "windows") is False
