"""Persistent trackers must be replaced when they are not the ones this build ships.

The defect: `_install_to_persistent` copies the bundled trackers to a stable
per-user dir ONCE, and the only two reasons to ever replace them (architecture,
signing) are macOS-only. The Windows uninstaller leaves %APPDATA% alone. So a
Windows or Linux machine ran the trackers of its FIRST install forever: no app
update and no uninstall+reinstall replaced them, and the ActivityWatch bump in
#217 (v0.13.2 -> v0.14.0b4) reached no existing Windows machine.

These tests drive real files in tmp dirs through the production resolver
(`_get_binaries_dir`) and installer, and assert what is ON DISK afterwards.
Module attributes added by the fix are reached through `awm` at call time, so
against pre-fix code the defect tests fail on behaviour, not on import.
"""

import logging
import os
from unittest.mock import MagicMock, patch

import pytest

import src.aw_manager as awm
from src.aw_manager import ALL_COMPONENTS, AWManager

STAMP = ".bundle-stamp"  # the file name is part of the on-disk contract


@pytest.fixture(autouse=True)
def _fresh_digest_cache():
    cache = getattr(awm, "_BUNDLE_DIGEST_CACHE", None)
    if cache is not None:
        cache.clear()
    yield
    if cache is not None:
        cache.clear()


def _tree(root, content: bytes, ext: str = "") -> str:
    """A bundled-layout tracker tree: <root>/<component>/<component><ext>."""
    for name in ALL_COMPONENTS:
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / (name + ext)).write_bytes(content + name.encode())
        (d / "_internal.bin").write_bytes(b"runtime-" + content)
    return str(root)


def _read(tree: str, name: str, ext: str = "") -> bytes:
    with open(os.path.join(tree, name, name + ext), "rb") as fh:
        return fh.read()


def _frozen_bundle(tmp_path, content: bytes, plat: str = "linux", ext: str = ""):
    """A fake PyInstaller _MEIPASS holding resources/trackers/<plat>."""
    meipass = tmp_path / "meipass"
    _tree(meipass / "resources" / "trackers" / plat, content, ext)
    return str(meipass)


def _resolver_env(meipass: str, install_dir: str, system: str = "Linux", plat: str = "linux"):
    """Patches that make `_get_binaries_dir` take the frozen, persistent path."""
    return [
        patch.object(awm.sys, "frozen", True, create=True),
        patch.object(awm.sys, "_MEIPASS", meipass, create=True),
        patch("src.aw_manager._get_install_dir", return_value=install_dir),
        patch("src.aw_manager._get_platform_key", return_value=plat),
        patch("src.aw_manager.platform.system", return_value=system),
    ]


def _enter(patches):
    for p in patches:
        p.start()
    return patches


def _exit(patches):
    for p in reversed(patches):
        p.stop()


def _mgr() -> AWManager:
    return AWManager(aw_port=5600, afk_timeout=600)


# --------------------------------------------------------------------------
# (1) The heal: an install with no stamp is replaced, off macOS.
# --------------------------------------------------------------------------


def test_missing_stamp_off_darwin_is_replaced_by_the_bundle(tmp_path):
    """The live population: devices first set up before stamping existed."""
    installed = _tree(tmp_path / "installed", b"OLD-v0.13.2-")
    meipass = _frozen_bundle(tmp_path, b"NEW-v0.14.0b4-")
    env = _enter(_resolver_env(meipass, installed))
    try:
        reason = AWManager._tracker_reinstall_reason(
            installed, os.path.join(meipass, "resources", "trackers", "linux")
        )
        assert reason is not None, "a stamp-less install off macOS must be replaced"
        assert "stamp" in reason

        assert _mgr()._get_binaries_dir() == installed
        for name in ALL_COMPONENTS:
            assert _read(installed, name).startswith(b"NEW-v0.14.0b4-"), (
                f"{name} on disk is still the first install's binary"
            )
        assert os.path.isfile(os.path.join(installed, STAMP))
        # Settled: asking again finds nothing to do, or it would re-copy forever.
        assert AWManager._tracker_reinstall_reason(
            installed, os.path.join(meipass, "resources", "trackers", "linux")
        ) is None
        leftovers = [e for e in os.listdir(installed) if ".bf-" in e]
        assert leftovers == [], f"staging/retired dirs left behind: {leftovers}"
    finally:
        _exit(env)


def test_missing_stamp_on_windows_replaces_the_exe_tree(tmp_path):
    """Same heal on the platform the live case is on (.exe layout)."""
    installed = _tree(tmp_path / "installed", b"OLD-", ext=".exe")
    meipass = _frozen_bundle(tmp_path, b"NEW-", plat="windows", ext=".exe")
    env = _enter(_resolver_env(meipass, installed, system="Windows", plat="windows"))
    try:
        assert _mgr()._get_binaries_dir() == installed
        for name in ALL_COMPONENTS:
            assert _read(installed, name, ".exe").startswith(b"NEW-")
    finally:
        _exit(env)


# --------------------------------------------------------------------------
# (2) / (3) Matching stamp keeps; different stamp replaces.
# --------------------------------------------------------------------------


def test_matching_stamp_is_kept(tmp_path):
    bundle = _tree(tmp_path / "bundle", b"SAME-")
    installed = str(tmp_path / "installed")
    with patch("src.aw_manager.platform.system", return_value="Linux"), \
         patch("src.aw_manager.subprocess.run"):
        assert AWManager._install_to_persistent(bundle, installed) is True
        assert os.path.isfile(os.path.join(installed, STAMP)), "install wrote no stamp"
        assert AWManager._tracker_reinstall_reason(installed, bundle) is None


def test_different_stamp_is_replaced(tmp_path):
    """A device updated across a tracker change (a later AW bump)."""
    old_bundle = _tree(tmp_path / "old-bundle", b"OLD-")
    new_bundle = _tree(tmp_path / "new-bundle", b"NEW-")
    installed = str(tmp_path / "installed")
    with patch("src.aw_manager.platform.system", return_value="Linux"), \
         patch("src.aw_manager.subprocess.run"):
        assert AWManager._install_to_persistent(old_bundle, installed) is True
        reason = AWManager._tracker_reinstall_reason(installed, new_bundle)
        assert reason is not None
        assert "differ" in reason


def test_unhashable_bundle_never_triggers(tmp_path):
    """'Could not tell' is never 'different': an empty/missing bundle keeps the copy."""
    installed = _tree(tmp_path / "installed", b"OLD-")
    with patch("src.aw_manager.platform.system", return_value="Linux"):
        assert AWManager._tracker_reinstall_reason(installed, str(tmp_path / "nope")) is None


# --------------------------------------------------------------------------
# (4) macOS keeps its old behaviour: no stamp reason there.
# --------------------------------------------------------------------------


def test_darwin_ignores_a_missing_stamp(tmp_path):
    """A macOS reinstall can cost the user an Input Monitoring re-grant, so the
    stamp reason is deliberately off there (control: passes pre-fix too)."""
    installed = _tree(tmp_path / "installed", b"OLD-")
    bundle = _tree(tmp_path / "bundle", b"NEW-")
    with patch("src.aw_manager.platform.system", return_value="Darwin"), \
         patch.object(AWManager, "_should_reinstall_for_signing", return_value=False):
        assert AWManager._tracker_reinstall_reason(installed, bundle) is None


# --------------------------------------------------------------------------
# (5) Never replace a running tree off macOS; never retry-spam a failure.
# --------------------------------------------------------------------------


def _running():
    proc = MagicMock()
    proc.poll.return_value = None
    return proc


def test_running_trackers_defer_the_replace_and_log_once(tmp_path, caplog):
    """Windows cannot delete a running .exe: replacing a live tree failed on
    every cycle. Deferred while ours run, done once they are stopped."""
    installed = _tree(tmp_path / "installed", b"OLD-", ext=".exe")
    meipass = _frozen_bundle(tmp_path, b"NEW-", plat="windows", ext=".exe")
    env = _enter(_resolver_env(meipass, installed, system="Windows", plat="windows"))
    try:
        mgr = _mgr()
        proc = _running()
        mgr._processes = {"bf-window-tracker": proc}
        with patch.object(AWManager, "_tracker_reinstall_reason", return_value="stale"), \
             patch.object(AWManager, "_install_to_persistent", return_value=True) as install, \
             caplog.at_level(logging.INFO, logger="src.aw_manager"):
            for _ in range(5):
                assert mgr._get_binaries_dir() == installed
            assert install.call_count == 0, "replaced a tree whose trackers are running"
            deferred = [r for r in caplog.records if "deferred" in r.getMessage()]
            assert len(deferred) == 1, f"deferral logged {len(deferred)} times"

            proc.poll.return_value = 0  # stopped
            mgr._get_binaries_dir()
            assert install.call_count == 1, "not replaced once the trackers stopped"
    finally:
        _exit(env)


def test_failed_replace_backs_off_instead_of_retrying_every_cycle(tmp_path, caplog):
    installed = _tree(tmp_path / "installed", b"OLD-", ext=".exe")
    meipass = _frozen_bundle(tmp_path, b"NEW-", plat="windows", ext=".exe")
    env = _enter(_resolver_env(meipass, installed, system="Windows", plat="windows"))
    clock = [1000.0]
    try:
        mgr = _mgr()
        with patch.object(AWManager, "_tracker_reinstall_reason", return_value="stale"), \
             patch.object(AWManager, "_install_to_persistent", return_value=False) as install, \
             patch("src.aw_manager.time.monotonic", side_effect=lambda: clock[0]), \
             caplog.at_level(logging.ERROR, logger="src.aw_manager"):
            for _ in range(10):
                mgr._get_binaries_dir()
                clock[0] += 30
            assert install.call_count == 1, f"retried {install.call_count} times in 5 min"
            errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
            assert len(errors) == 1

            clock[0] += 900  # past the retry interval
            mgr._get_binaries_dir()
            assert install.call_count == 2, "never retried after the backoff"
    finally:
        _exit(env)


def test_locked_live_tree_is_left_intact(tmp_path):
    """A locked live dir (antivirus or another open handle; a merely RUNNING
    .exe does not lock it, see the real-Windows test): renaming it aside raises. The old
    tree must still be complete (capture keeps working) and the stamp absent,
    so a later attempt retries."""
    installed = _tree(tmp_path / "installed", b"OLD-")
    bundle = _tree(tmp_path / "bundle", b"NEW-")
    real_rename = os.rename

    def locked_rename(src, dst):
        if os.path.basename(src) in ALL_COMPONENTS:
            raise PermissionError(32, "The process cannot access the file")
        return real_rename(src, dst)

    with patch("src.aw_manager.platform.system", return_value="Linux"), \
         patch("src.aw_manager.os.rename", side_effect=locked_rename):
        assert AWManager._install_to_persistent(bundle, installed) is False
    for name in ALL_COMPONENTS:
        assert _read(installed, name).startswith(b"OLD-"), f"{name} lost on a failed swap"
    assert not os.path.exists(os.path.join(installed, STAMP))
    assert [e for e in os.listdir(installed) if ".bf-" in e] == []
    with patch("src.aw_manager.platform.system", return_value="Linux"):
        assert AWManager._tracker_reinstall_reason(installed, bundle) is not None


def test_lock_after_the_first_swap_rolls_the_earlier_swaps_back(tmp_path):
    """The server swaps fine, then a watcher dir is locked (an orphan .exe).
    The tree must not be left half new / half old."""
    installed = _tree(tmp_path / "installed", b"OLD-")
    bundle = _tree(tmp_path / "bundle", b"NEW-")
    real_rename = os.rename
    locked = ALL_COMPONENTS[-1]

    def locked_rename(src, dst):
        if os.path.basename(src) == locked:
            raise PermissionError(32, "The process cannot access the file")
        return real_rename(src, dst)

    with patch("src.aw_manager.platform.system", return_value="Linux"), \
         patch("src.aw_manager.os.rename", side_effect=locked_rename):
        assert AWManager._install_to_persistent(bundle, installed) is False
    for name in ALL_COMPONENTS:
        assert _read(installed, name).startswith(b"OLD-"), f"{name} left swapped"
    assert not os.path.exists(os.path.join(installed, STAMP))

    with patch("src.aw_manager.platform.system", return_value="Linux"):
        assert AWManager._install_to_persistent(bundle, installed) is True
        for name in ALL_COMPONENTS:
            assert _read(installed, name).startswith(b"NEW-")
        assert [e for e in os.listdir(installed) if ".bf-" in e] == []


def test_copy_failure_midway_leaves_the_live_tree_and_a_clean_retry(tmp_path):
    """The old installer deleted each live dir BEFORE copying, so a copy that
    died midway left a broken tree. Now nothing live is touched until every
    component is staged, and a leftover staging dir does not wedge the retry."""
    installed = _tree(tmp_path / "installed", b"OLD-")
    bundle = _tree(tmp_path / "bundle", b"NEW-")
    real_copytree = awm.shutil.copytree
    calls = [0]

    def flaky_copytree(src, dst, *a, **kw):
        calls[0] += 1
        if calls[0] == 2:
            real_copytree(src, dst, *a, **kw)  # half-written staging, then die
            raise OSError(28, "No space left on device")
        return real_copytree(src, dst, *a, **kw)

    with patch("src.aw_manager.platform.system", return_value="Linux"), \
         patch("src.aw_manager.shutil.copytree", side_effect=flaky_copytree):
        assert AWManager._install_to_persistent(bundle, installed) is False
    for name in ALL_COMPONENTS:
        assert _read(installed, name).startswith(b"OLD-"), f"{name} broken by a failed copy"

    with patch("src.aw_manager.platform.system", return_value="Linux"):
        assert AWManager._install_to_persistent(bundle, installed) is True
        for name in ALL_COMPONENTS:
            assert _read(installed, name).startswith(b"NEW-")
        assert AWManager._tracker_reinstall_reason(installed, bundle) is None


# --------------------------------------------------------------------------
# (6) Launch with OUR OWN leftover tracker still running from the old tree.
#
# The case the replace exists for on Windows: the previous instance crashed or
# was killed, its bf-data-service (and maybe watchers) survived, and they hold
# their .exe files open. Windows refuses to rename that tree, so before the fix
# the launch-time replace failed on exactly the machines it targets, and
# `_start_locked` attached to the old server as "external" and ran the stale
# trackers for another session. The fix reaps processes whose EXECUTABLE lives
# in the persistent install dir (never by name) before replacing.
# --------------------------------------------------------------------------


class _FakeProc:
    """A psutil.Process stand-in. `alive` flips False on terminate/kill."""

    def __init__(self, pid: int, exe: str):
        self.pid = pid
        self.info = {"pid": pid, "exe": exe}
        self.alive = True
        self.terminated = False

    def terminate(self):
        self.terminated = True
        self.alive = False

    def kill(self):
        self.alive = False

    def is_running(self):
        return self.alive


def _psutil_patches(procs):
    def wait_procs(ps, timeout=None):
        gone = [p for p in ps if not p.alive]
        return gone, [p for p in ps if p.alive]

    return [
        patch("psutil.process_iter", side_effect=lambda *a, **kw: list(procs)),
        patch("psutil.wait_procs", side_effect=wait_procs),
    ]


def _locking_rename(holders):
    """os.rename that refuses to move a component dir while a holder runs from it."""
    real_rename = os.rename

    def rename(src, dst):
        base = os.path.basename(src)
        for p in holders:
            if p.alive and os.path.dirname(p.info["exe"]) == src:
                raise PermissionError(32, f"The process cannot access the file ({base})")
        return real_rename(src, dst)

    return rename


def test_leftover_server_holding_the_tree_is_reaped_so_the_replace_lands(tmp_path):
    installed = _tree(tmp_path / "installed", b"OLD-", ext=".exe")
    meipass = _frozen_bundle(tmp_path, b"NEW-", plat="windows", ext=".exe")
    leftover = _FakeProc(4242, os.path.join(installed, "bf-data-service", "bf-data-service.exe"))
    # Same NAME, different install: must never be touched (path-scoped, not by name).
    foreign_dir = tmp_path / "someone-else" / "bf-data-service"
    foreign = _FakeProc(5151, str(foreign_dir / "bf-data-service.exe"))
    # A sibling dir sharing the install dir's PREFIX (`windows2` next to
    # `windows`): a string-prefix match would wrongly take it as "inside".
    sibling = _FakeProc(
        6161, os.path.join(installed + "2", "bf-data-service", "bf-data-service.exe")
    )
    env = _enter(
        _resolver_env(meipass, installed, system="Windows", plat="windows")
        + _psutil_patches([leftover, foreign, sibling])
        + [patch("src.aw_manager.os.rename", side_effect=_locking_rename([leftover]))]
    )
    try:
        assert _mgr()._get_binaries_dir() == installed
        assert leftover.terminated, "our own leftover tracker was not stopped"
        assert foreign.alive and not foreign.terminated, "reaped a process outside our install"
        assert sibling.alive and not sibling.terminated, (
            "reaped a process in a sibling dir that merely shares the install prefix"
        )
        for name in ALL_COMPONENTS:
            assert _read(installed, name, ".exe").startswith(b"NEW-"), (
                f"{name} is still the old binary: the leftover held the tree"
            )
        assert os.path.isfile(os.path.join(installed, STAMP))
    finally:
        _exit(env)


def test_launch_starts_our_own_server_after_reaping_the_leftover(tmp_path):
    """The port must be probed AFTER the reap. Probed before, launch sees the
    leftover's socket, attaches to it as 'external', and the server it attached
    to is the one the replace just killed."""
    installed = _tree(tmp_path / "installed", b"OLD-", ext=".exe")
    meipass = _frozen_bundle(tmp_path, b"NEW-", plat="windows", ext=".exe")
    leftover = _FakeProc(4242, os.path.join(installed, "bf-data-service", "bf-data-service.exe"))
    env = _enter(
        _resolver_env(meipass, installed, system="Windows", plat="windows")
        + _psutil_patches([leftover])
        + [patch("src.aw_manager.os.rename", side_effect=_locking_rename([leftover]))]
    )
    try:
        mgr = _mgr()
        started = []
        with patch.object(AWManager, "_port_in_use", side_effect=lambda self=None: leftover.alive), \
             patch.object(AWManager, "_rosetta_required", return_value=False), \
             patch.object(AWManager, "_start_component",
                          side_effect=lambda name, d: started.append((name, d)) or True), \
             patch.object(AWManager, "_wait_for_server", return_value=True), \
             patch.object(AWManager, "_reap_orphan_processes"):
            assert mgr._start_locked() is True
        assert not mgr._using_external, "attached to the leftover server it just reaped"
        assert started and started[0] == (awm.BF_SERVER, installed), started
        assert _read(installed, awm.BF_SERVER, ".exe").startswith(b"NEW-")
    finally:
        _exit(env)


def test_darwin_reinstall_does_not_reap(tmp_path):
    """macOS keeps its old behaviour: unlinking a running binary is allowed
    there, so nothing is killed for a reinstall (control)."""
    installed = _tree(tmp_path / "installed", b"OLD-")
    meipass = _frozen_bundle(tmp_path, b"NEW-", plat="darwin")
    leftover = _FakeProc(4242, os.path.join(installed, "bf-data-service", "bf-data-service"))
    env = _enter(
        _resolver_env(meipass, installed, system="Darwin", plat="darwin")
        + _psutil_patches([leftover])
    )
    try:
        with patch.object(AWManager, "_tracker_reinstall_reason", return_value="arch"), \
             patch.object(AWManager, "_install_to_persistent", return_value=True):
            _mgr()._get_binaries_dir()
        assert not leftover.terminated
    finally:
        _exit(env)


def test_force_restart_applies_a_pending_replace_after_the_reap(tmp_path):
    """force_restart is the one mid-session route that retries a replace the
    launch could not land (deferred or failed). It must still apply it."""
    installed = _tree(tmp_path / "installed", b"OLD-", ext=".exe")
    meipass = _frozen_bundle(tmp_path, b"NEW-", plat="windows", ext=".exe")
    env = _enter(
        _resolver_env(meipass, installed, system="Windows", plat="windows")
        + _psutil_patches([])
    )
    try:
        mgr = _mgr()
        with patch.object(AWManager, "_reap_orphan_processes"), \
             patch.object(AWManager, "_start_locked", return_value=True):
            assert mgr.force_restart("test") is True
        for name in ALL_COMPONENTS:
            assert _read(installed, name, ".exe").startswith(b"NEW-"), (
                f"{name}: force_restart did not apply the pending replace"
            )
    finally:
        _exit(env)


# --------------------------------------------------------------------------
# (7) REAL Windows: a real running .exe really locks its directory, and the
# reap really releases it. Runs on the windows-latest PR leg
# (windows-tests.yml); everything above models these semantics with fakes.
# --------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="real Win32 process + NTFS lock semantics")
def test_real_windows_running_tracker_is_reaped_and_tree_replaced(tmp_path):
    import shutil as _sh
    import subprocess as _sp
    import time as _t

    installed = _tree(tmp_path / "installed", b"OLD-", ext=".exe")
    meipass = _frozen_bundle(tmp_path, b"NEW-", plat="windows", ext=".exe")
    server_dir = os.path.join(installed, "bf-data-service")
    exe = os.path.join(server_dir, "bf-data-service.exe")
    ping = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "PING.EXE")
    _sh.copyfile(ping, exe)
    proc = _sp.Popen([exe, "-n", "120", "127.0.0.1"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
    try:
        _t.sleep(1.0)
        assert proc.poll() is None, "the stand-in tracker did not stay running"
        # No "the tree is locked" control here, on purpose. One was tried and
        # windows-latest disproved it (PR #320, job 135563436656: renaming the
        # directory of a running .exe did NOT raise). So the staged swap is not
        # blocked by a running tracker; the reap is still required so the NEW
        # binaries are the ones that start, which the asserts below witness.

        env = _enter(_resolver_env(meipass, installed, system="Windows", plat="windows"))
        try:
            assert _mgr()._get_binaries_dir() == installed
        finally:
            _exit(env)

        assert proc.poll() is not None, "our leftover tracker is still running"
        for name in ALL_COMPONENTS:
            assert _read(installed, name, ".exe").startswith(b"NEW-"), (
                f"{name} is still the old binary on real Windows"
            )
        assert os.path.isfile(os.path.join(installed, STAMP))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
