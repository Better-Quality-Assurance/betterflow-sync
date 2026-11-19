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

        assert _mgr()._get_binaries_dir(reinstall=True) == installed
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
        assert _mgr()._get_binaries_dir(reinstall=True) == installed
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
    """Replacing the tree under our own running trackers would leave them
    running the OLD binaries from the retired copy (the rename does not fail on
    Windows) under a stamp saying the new ones are installed. Deferred while
    ours run, done once they are stopped."""
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
                assert mgr._get_binaries_dir(reinstall=True) == installed
            assert install.call_count == 0, "replaced a tree whose trackers are running"
            deferred = [r for r in caplog.records if "deferred" in r.getMessage()]
            assert len(deferred) == 1, f"deferral logged {len(deferred)} times"

            proc.poll.return_value = 0  # stopped
            mgr._get_binaries_dir(reinstall=True)
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
                mgr._get_binaries_dir(reinstall=True)
                clock[0] += 30
            assert install.call_count == 1, f"retried {install.call_count} times in 5 min"
            errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
            assert len(errors) == 1

            clock[0] += 900  # past the retry interval
            mgr._get_binaries_dir(reinstall=True)
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
    """The server swaps fine, then a watcher dir is locked (antivirus or an
    open handle). The tree must not be left half new / half old."""
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
# was killed and its bf-data-service (and maybe watchers) survived. Renaming
# their tree does NOT fail (measured on windows-latest, PR #320 job
# 135563436656), so the swap "succeeds" around them: they keep running the OLD
# binaries from the retired `.bf-old-*` copy and hold the port, `_start_locked`
# attaches to that server as "external", and the stamp already says the replace
# landed, so nothing retries. The fix reaps processes whose EXECUTABLE lives in
# the persistent install dir (never by name) before replacing, and REFUSES the
# replace (no swap, no stamp, retried after the backoff) when one survives.
# --------------------------------------------------------------------------


class _FakeProc:
    """A psutil.Process stand-in. `alive` flips False on terminate/kill, unless
    `stubborn` (it survives both, as a process we lack the rights to stop does).
    `exe=None` models psutil's AccessDenied answer for the executable path."""

    def __init__(self, pid: int, exe, name: str = "", stubborn: bool = False):
        self.pid = pid
        if not name and exe:
            name = os.path.basename(exe)
        self.info = {"pid": pid, "exe": exe, "name": name}
        self.alive = True
        self.terminated = False
        self.killed = False
        self.stubborn = stubborn

    def terminate(self):
        self.terminated = True
        if not self.stubborn:
            self.alive = False

    def kill(self):
        self.killed = True
        if not self.stubborn:
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
    )
    try:
        assert _mgr()._get_binaries_dir(reinstall=True) == installed
        assert leftover.terminated, "our own leftover tracker was not stopped"
        assert foreign.alive and not foreign.terminated, "reaped a process outside our install"
        assert sibling.alive and not sibling.terminated, (
            "reaped a process in a sibling dir that merely shares the install prefix"
        )
        for name in ALL_COMPONENTS:
            assert _read(installed, name, ".exe").startswith(b"NEW-"), (
                f"{name} is still the old binary"
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
            _mgr()._get_binaries_dir(reinstall=True)
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


def test_reap_missed_holder_refuses_the_replace_and_retries_later(tmp_path):
    """A leftover the reap cannot stop (it survives terminate + kill). The swap
    would NOT fail around it (renaming a running .exe's tree is allowed), so
    before the fix the replace "succeeded", the stamp was written, and launch
    attached to the stale server now running from the retired `.bf-old-*`
    copy. Since the stamp matched, nothing ever retried.

    Fixed: no swap, no stamp, the reason stays pending, and once the holder is
    gone (and the backoff has passed) the next launch lands the replace."""
    installed = _tree(tmp_path / "installed", b"OLD-", ext=".exe")
    meipass = _frozen_bundle(tmp_path, b"NEW-", plat="windows", ext=".exe")
    bundle = os.path.join(meipass, "resources", "trackers", "windows")
    server_exe = os.path.join(installed, "bf-data-service", "bf-data-service.exe")
    holder = _FakeProc(4242, server_exe, stubborn=True)
    clock = [1000.0]
    env = _enter(
        _resolver_env(meipass, installed, system="Windows", plat="windows")
        + _psutil_patches([holder])
        + [patch("src.aw_manager.time.monotonic", side_effect=lambda: clock[0])]
    )
    try:
        mgr = _mgr()
        started = []
        with patch.object(AWManager, "_port_in_use", side_effect=lambda self=None: holder.alive), \
             patch.object(AWManager, "_rosetta_required", return_value=False), \
             patch.object(AWManager, "_start_component",
                          side_effect=lambda name, d: started.append((name, d)) or True), \
             patch.object(AWManager, "_wait_for_server", return_value=True), \
             patch.object(AWManager, "_server_responding", return_value=True), \
             patch.object(AWManager, "_reap_orphan_processes"):
            mgr._start_locked()
            assert holder.terminated and holder.killed, "the reap never tried"
            assert not os.path.exists(os.path.join(installed, STAMP)), (
                "stamp written although a leftover still runs from the old tree: "
                "the replace will never be retried"
            )
            retired = [e for e in os.listdir(installed) if ".bf-old-" in e]
            assert retired == [], (
                f"swapped around a running leftover; it now serves from {retired}"
            )
            # What we are attached to is the live install, not a retired copy.
            assert os.path.isfile(server_exe)
            assert _read(installed, awm.BF_SERVER, ".exe").startswith(b"OLD-")
            assert AWManager._tracker_reinstall_reason(installed, bundle) is not None

            # The holder finally exits; the next launch lands the replace.
            holder.alive = False
            clock[0] += awm.REINSTALL_RETRY_INTERVAL + 1
            relaunched = _mgr()
            relaunched._start_locked()
        assert not relaunched._using_external
        assert (awm.BF_SERVER, installed) in started
        for name in ALL_COMPONENTS:
            assert _read(installed, name, ".exe").startswith(b"NEW-"), name
        assert AWManager._tracker_reinstall_reason(installed, bundle) is None
    finally:
        _exit(env)


def test_uninspectable_process_named_like_a_tracker_blocks_but_is_never_killed(tmp_path):
    """psutil reports exe=None on AccessDenied. A process NAMED like one of our
    components may be ours running from this tree: never killed (a name is not
    proof), but the replace waits for it rather than stamping around it."""
    installed = _tree(tmp_path / "installed", b"OLD-", ext=".exe")
    meipass = _frozen_bundle(tmp_path, b"NEW-", plat="windows", ext=".exe")
    hidden = _FakeProc(7070, None, name="bf-data-service.exe")
    env = _enter(
        _resolver_env(meipass, installed, system="Windows", plat="windows")
        + _psutil_patches([hidden])
    )
    try:
        assert _mgr()._get_binaries_dir(reinstall=True) == installed
        assert not hidden.terminated and not hidden.killed, "killed a process by NAME"
        assert not os.path.exists(os.path.join(installed, STAMP))
        for name in ALL_COMPONENTS:
            assert _read(installed, name, ".exe").startswith(b"OLD-"), name
    finally:
        _exit(env)


def test_uninspectable_process_with_another_name_does_not_block(tmp_path):
    """Control: every Windows host has AccessDenied processes (services, other
    users). Counting those would block every replace forever."""
    installed = _tree(tmp_path / "installed", b"OLD-", ext=".exe")
    meipass = _frozen_bundle(tmp_path, b"NEW-", plat="windows", ext=".exe")
    system_proc = _FakeProc(4, None, name="svchost.exe")
    env = _enter(
        _resolver_env(meipass, installed, system="Windows", plat="windows")
        + _psutil_patches([system_proc])
    )
    try:
        assert _mgr()._get_binaries_dir(reinstall=True) == installed
        assert not system_proc.terminated
        for name in ALL_COMPONENTS:
            assert _read(installed, name, ".exe").startswith(b"NEW-"), name
        assert os.path.isfile(os.path.join(installed, STAMP))
    finally:
        _exit(env)


def test_mid_session_resolve_never_reaps_the_attached_external_server(tmp_path):
    """Attached to an external server (not in `_processes`, so the
    `_managed_tracker_running` guard cannot see it) that runs from the
    persistent tree. A mid-session resolve (idle tracker stop/start,
    restart_idle_tracker, the health loop, the AFK-timeout update) must not
    reap it or replace the tree: nothing would re-probe the port, leaving
    `_using_external` True on a dead server."""
    installed = _tree(tmp_path / "installed", b"OLD-", ext=".exe")
    meipass = _frozen_bundle(tmp_path, b"NEW-", plat="windows", ext=".exe")
    external = _FakeProc(
        4242, os.path.join(installed, "bf-data-service", "bf-data-service.exe")
    )
    env = _enter(
        _resolver_env(meipass, installed, system="Windows", plat="windows")
        + _psutil_patches([external])
    )
    try:
        mgr = _mgr()
        mgr._using_external = True
        mgr._processes = {}
        with patch.object(AWManager, "_start_component", return_value=True), \
             patch.object(AWManager, "_reap_orphan_processes"):
            assert mgr._get_binaries_dir() == installed
            mgr.restart_idle_tracker("blind")
            with mgr._lifecycle_lock:
                mgr._stop_idle_tracker_locked()
        assert external.alive and not external.terminated, (
            "a mid-session resolve killed the external server we are attached to"
        )
        assert mgr._using_external
        for name in ALL_COMPONENTS:
            assert _read(installed, name, ".exe").startswith(b"OLD-"), name
        assert not os.path.exists(os.path.join(installed, STAMP))
    finally:
        _exit(env)


def test_windows_legacy_flat_exe_install_is_replaced(tmp_path):
    """A Windows LEGACY FLAT install keeps `<name>.exe` directly in the install
    dir. The swap removed only `<name>` (no extension), so the flat .exe
    survived, `_resolve_binary_path` (flat first) kept spawning it, and the
    stamp was written on top: the replace never took and never retried."""
    installed = tmp_path / "installed"
    installed.mkdir()
    for name in ALL_COMPONENTS:
        (installed / (name + ".exe")).write_bytes(b"OLD-flat-" + name.encode())
    installed = str(installed)
    meipass = _frozen_bundle(tmp_path, b"NEW-", plat="windows", ext=".exe")
    bundle = os.path.join(meipass, "resources", "trackers", "windows")
    env = _enter(
        _resolver_env(meipass, installed, system="Windows", plat="windows")
        + _psutil_patches([])
    )
    try:
        assert _mgr()._get_binaries_dir(reinstall=True) == installed
        for name in ALL_COMPONENTS:
            resolved = awm._resolve_binary_path(installed, name)
            assert resolved == os.path.join(installed, name, name + ".exe"), (
                f"{name} still resolves to {resolved}"
            )
            with open(resolved, "rb") as fh:
                assert fh.read().startswith(b"NEW-"), name
        assert AWManager._tracker_reinstall_reason(installed, bundle) is None
    finally:
        _exit(env)


# --------------------------------------------------------------------------
# (7) REAL Windows: a real running .exe is reaped and the tree replaced. Runs
# on the windows-latest PR leg (windows-tests.yml). It does NOT lock its
# directory (that leg measured it), and the fakes above model exactly that:
# os.rename is the real one, and only the reap's outcome gates the replace.
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
            assert _mgr()._get_binaries_dir(reinstall=True) == installed
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
