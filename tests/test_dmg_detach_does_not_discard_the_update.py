"""#211: a cleanup failure threw away an update that had already been extracted.

Martin's Mac, 2026-08-23. An update from 1.5.119 to 1.5.125 died on

    hdiutil detach failed (rc=1)
    Update failed: Failed to detach DMG mount - aborting update

and the machine then ran 1.5.119 for days, below the server's own minimum
version floor, while every sync logged "update required".

**The `.app` had already been copied out before that happened.** Reading
`_extract_from_dmg`: `shutil.copytree` completes inside the `try`, and the
`RuntimeError` is raised from the `finally` — pure housekeeping, after the work
succeeded. Unmounting a disk image is not a reason to refuse an update the user
needs; a leaked mount costs one entry under /var/folders until reboot (we attach with
`-mountpoint <mkdtemp>` and `-nobrowse`, so it never appears in /Volumes), while
the abort cost a fleet device stuck on an old build.

The same construct has a second defect that is invisible in the incident logs:
**`raise` inside `finally` REPLACES any exception still in flight.** If
`copytree` had been the real failure, that cause would be discarded and the
operator would be told the problem was `detach`. `test_a_real_failure_is_not_relabelled` pins that direction, because fixing only the first defect leaves it.

So detach now retries (the second attempt with `-force`, which is what clears
the usual cause — a straggling process holding the volume) and, if it still
fails, LOGS rather than raising.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

import src.self_updater as su


def _dmg_with_app(tmp_path):
    """A mount point that already contains a .app, as a mounted DMG would."""
    mount = tmp_path / "mnt"
    app = mount / "BetterFlow.app" / "Contents" / "MacOS"
    app.mkdir(parents=True)
    (app / "BetterFlow").write_text("#!/bin/sh\n")
    return mount


def _run_stub(mount, *, detach_rc, attach_rc=0, calls=None):
    """Stand in for hdiutil.

    It does NOT materialise anything — `_dmg_with_app` pre-creates the tree and
    `mkdtemp` is monkeypatched to return it, so the copy under test operates on
    real files that were already there. An earlier version of this docstring
    claimed otherwise, which a reviewer had to correct.
    """
    def run(cmd, *a, **k):
        if calls is not None:
            calls.append(list(cmd))
        if cmd[:2] == ["hdiutil", "attach"]:
            if attach_rc != 0:
                raise subprocess.CalledProcessError(attach_rc, cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["hdiutil", "detach"]:
            rc = detach_rc.pop(0) if isinstance(detach_rc, list) else detach_rc
            return subprocess.CompletedProcess(cmd, rc, "", "hdiutil: couldn't unmount")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return run


def test_a_failed_detach_keeps_the_extracted_app(tmp_path, monkeypatch):
    """THE defect. The copy is done; the unmount is not the deliverable."""
    mount = _dmg_with_app(tmp_path)
    extract = tmp_path / "out"
    extract.mkdir()
    monkeypatch.setattr(su.tempfile, "mkdtemp", lambda **kw: str(mount))

    with patch.object(su.subprocess, "run", _run_stub(mount, detach_rc=1)):
        su._extract_from_dmg(tmp_path / "x.dmg", extract)

    assert (extract / "BetterFlow.app").is_dir(), (
        "the app was extracted and then discarded because unmounting failed"
    )


def test_it_retries_the_detach_with_force(tmp_path, monkeypatch):
    """A straggling process holding the volume is the usual cause, and `-force`
    is what clears it. Asserted on the ARGUMENTS, so a retry that just repeats
    the same failing command does not pass."""
    mount = _dmg_with_app(tmp_path)
    extract = tmp_path / "out"
    extract.mkdir()
    monkeypatch.setattr(su.tempfile, "mkdtemp", lambda **kw: str(mount))
    calls: list[list[str]] = []

    with patch.object(su.subprocess, "run", _run_stub(mount, detach_rc=[1, 0], calls=calls)):
        su._extract_from_dmg(tmp_path / "x.dmg", extract)

    detaches = [c for c in calls if c[:2] == ["hdiutil", "detach"]]
    assert len(detaches) == 2, detaches
    assert "-force" not in detaches[0], "the first attempt should be the polite one"
    assert "-force" in detaches[1], "the retry must escalate, or it is the same command twice"


def test_a_detach_that_never_succeeds_still_does_not_abort(tmp_path, monkeypatch):
    """Both attempts fail. A leaked mount costs one /var/folders entry until
    reboot; the alternative is a device stuck below the minimum version floor."""
    mount = _dmg_with_app(tmp_path)
    extract = tmp_path / "out"
    extract.mkdir()
    monkeypatch.setattr(su.tempfile, "mkdtemp", lambda **kw: str(mount))

    with patch.object(su.subprocess, "run", _run_stub(mount, detach_rc=[1, 1])):
        su._extract_from_dmg(tmp_path / "x.dmg", extract)

    assert (extract / "BetterFlow.app").is_dir()


def test_a_timeout_on_detach_is_also_not_fatal(tmp_path, monkeypatch):
    """The incident's sibling branch — same `raise`, reached a different way."""
    mount = _dmg_with_app(tmp_path)
    extract = tmp_path / "out"
    extract.mkdir()
    monkeypatch.setattr(su.tempfile, "mkdtemp", lambda **kw: str(mount))

    def run(cmd, *a, **k):
        if cmd[:2] == ["hdiutil", "detach"]:
            raise subprocess.TimeoutExpired(cmd, 30)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch.object(su.subprocess, "run", run):
        su._extract_from_dmg(tmp_path / "x.dmg", extract)

    assert (extract / "BetterFlow.app").is_dir()


def test_a_real_failure_is_not_relabelled(tmp_path, monkeypatch):
    """The second defect, which fixing the first one alone would leave.

    `raise` inside `finally` REPLACES the exception in flight. With no .app in
    the image the real error is FileNotFoundError; pre-fix the operator was told
    the problem was the detach instead, and the actual cause never reached a log.
    """
    mount = tmp_path / "mnt"
    mount.mkdir()  # mounted, but empty — no .app inside
    extract = tmp_path / "out"
    extract.mkdir()
    monkeypatch.setattr(su.tempfile, "mkdtemp", lambda **kw: str(mount))

    with patch.object(su.subprocess, "run", _run_stub(mount, detach_rc=1)), \
            pytest.raises(FileNotFoundError):
        su._extract_from_dmg(tmp_path / "x.dmg", extract)


def test_the_happy_path_still_detaches_exactly_once(tmp_path, monkeypatch):
    """The control. Making failure non-fatal must not stop us unmounting when
    unmounting works — that would leak a volume on every single update."""
    mount = _dmg_with_app(tmp_path)
    extract = tmp_path / "out"
    extract.mkdir()
    monkeypatch.setattr(su.tempfile, "mkdtemp", lambda **kw: str(mount))
    calls: list[list[str]] = []

    with patch.object(su.subprocess, "run", _run_stub(mount, detach_rc=0, calls=calls)):
        su._extract_from_dmg(tmp_path / "x.dmg", extract)

    assert len([c for c in calls if c[:2] == ["hdiutil", "detach"]]) == 1
    assert (extract / "BetterFlow.app").is_dir()


def test_a_failed_attach_does_not_try_to_unmount_anything(tmp_path, monkeypatch, caplog):
    """The attach path, which had no test and is why the noise got through.

    `hdiutil attach` runs with `check=True` and deliberately keeps checksum
    verification on, so a corrupt or truncated download fails THERE. The
    `finally` fires regardless. Ungated, it spent two hdiutil calls producing
    "no such volume" and then logged a confident "could not unmount" ABOVE the
    real cause — and on this fleet's diagnosis route (fetch the device's log,
    grep it) that points the next reader at #211's ghost instead of the
    download.

    Uses `attach_rc`, which existed but was dead until this test.
    """
    import logging

    mount = _dmg_with_app(tmp_path)
    extract = tmp_path / "out"
    extract.mkdir()
    monkeypatch.setattr(su.tempfile, "mkdtemp", lambda **kw: str(mount))
    calls: list[list[str]] = []

    with caplog.at_level(logging.WARNING, logger="src.self_updater"), \
            patch.object(su.subprocess, "run",
                         _run_stub(mount, detach_rc=1, attach_rc=1, calls=calls)), \
            pytest.raises(subprocess.CalledProcessError):
        su._extract_from_dmg(tmp_path / "x.dmg", extract)

    assert [c for c in calls if c[:2] == ["hdiutil", "detach"]] == [], (
        "tried to unmount a volume that never attached"
    )
    assert not [r for r in caplog.records if "unmount" in r.getMessage()], (
        "logged a false unmount failure ahead of the real cause"
    )


def test_a_total_failure_reports_back_and_says_so(tmp_path, monkeypatch, caplog):
    """Three mutants survived the first matrix and this closes all of them:
    the return value (nothing read it), the final log line (nothing asserted
    it), and the retry-after-timeout (no test counted the calls).

    They survived because I only wrote the mutants I had thought of — the
    author is the worst-placed person to enumerate them.
    """
    import logging

    mount = _dmg_with_app(tmp_path)
    calls: list[list[str]] = []

    with caplog.at_level(logging.WARNING, logger="src.self_updater"), \
            patch.object(su.subprocess, "run",
                         _run_stub(mount, detach_rc=[1, 1], calls=calls)):
        assert su._detach_dmg(mount) is False

    assert len([c for c in calls if c[:2] == ["hdiutil", "detach"]]) == 2
    assert any("Could not unmount" in r.getMessage() for r in caplog.records), (
        "total failure left no trace at all"
    )


def test_a_hung_detach_is_still_retried_with_force(tmp_path, monkeypatch):
    """A HUNG unmount is precisely the "something is holding the volume" case
    that `-force` exists for, so the timeout branch must continue rather than
    give up. Nothing counted the calls before, so `continue` -> `break` was
    invisible."""
    mount = _dmg_with_app(tmp_path)
    calls: list[list[str]] = []

    def run(cmd, *a, **k):
        calls.append(list(cmd))
        if cmd[:2] == ["hdiutil", "detach"]:
            raise subprocess.TimeoutExpired(cmd, 30)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch.object(su.subprocess, "run", run):
        assert su._detach_dmg(mount) is False

    detaches = [c for c in calls if c[:2] == ["hdiutil", "detach"]]
    assert len(detaches) == 2, "gave up after the first hang"
    assert "-force" in detaches[1]


def test_the_happy_path_returns_true(tmp_path):
    """The positive control for the boolean, so `return True` -> `return False`
    is not a free mutation."""
    mount = _dmg_with_app(tmp_path)
    with patch.object(su.subprocess, "run", _run_stub(mount, detach_rc=0)):
        assert su._detach_dmg(mount) is True


# ── The contract, not just the paths I imagined ─────────────────────────
#
# `_detach_dmg` promises it never raises. It caught only TimeoutExpired, so an
# OSError from subprocess.run — raised BEFORE the child starts — escaped through
# the `finally` and restored both of #211's defects: it destroys an update whose
# copytree already succeeded, and it replaces the exception in flight. A
# reviewer found this with three failing probes; they are kept here so the
# contract is tested rather than asserted in a docstring.


@pytest.mark.parametrize("boom", [
    FileNotFoundError(2, "No such file or directory: 'hdiutil'"),
    OSError(12, "Cannot allocate memory"),
    BlockingIOError(35, "Resource temporarily unavailable"),
    PermissionError(1, "Operation not permitted"),
], ids=["hdiutil-absent", "ENOMEM", "EAGAIN", "sandboxed"])
def test_detach_never_raises_whatever_subprocess_does(tmp_path, boom):
    """The contract itself. Each of these is a real way `subprocess.run` fails
    before the child exists: hdiutil unresolvable on PATH, fork/posix_spawn
    under memory or process pressure in a long-lived multi-threaded tray app,
    or a sandbox profile."""
    mount = _dmg_with_app(tmp_path)

    def run(cmd, *a, **k):
        raise boom

    with patch.object(su.subprocess, "run", run):
        assert su._detach_dmg(mount) is False  # returns, does not raise


def test_an_oserror_during_cleanup_does_not_destroy_the_extract(tmp_path, monkeypatch):
    """#211's first defect, reached through the door TimeoutExpired-only left
    open."""
    mount = _dmg_with_app(tmp_path)
    extract = tmp_path / "out"
    extract.mkdir()
    monkeypatch.setattr(su.tempfile, "mkdtemp", lambda **kw: str(mount))

    def run(cmd, *a, **k):
        if cmd[:2] == ["hdiutil", "detach"]:
            raise OSError(12, "Cannot allocate memory")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch.object(su.subprocess, "run", run):
        su._extract_from_dmg(tmp_path / "x.dmg", extract)

    assert (extract / "BetterFlow.app").is_dir()


def test_an_oserror_during_cleanup_does_not_relabel_the_real_failure(tmp_path, monkeypatch):
    """#211's second defect, same door. The image has no .app, so the caller
    must still see FileNotFoundError from the extraction — not whatever the
    cleanup tripped over on the way out."""
    mount = tmp_path / "mnt"
    mount.mkdir()
    extract = tmp_path / "out"
    extract.mkdir()
    monkeypatch.setattr(su.tempfile, "mkdtemp", lambda **kw: str(mount))

    def run(cmd, *a, **k):
        if cmd[:2] == ["hdiutil", "detach"]:
            raise OSError(12, "Cannot allocate memory")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch.object(su.subprocess, "run", run), \
            pytest.raises(FileNotFoundError):
        su._extract_from_dmg(tmp_path / "x.dmg", extract)


def test_the_detach_command_keeps_its_stderr(tmp_path):
    """`-quiet` blanked the only diagnostic we get — measured, both rc=1 and
    rc=16 return empty stderr with it, so the "rc=%d: %s" log line always
    printed an empty %s. Asserted on the command, because the emptiness is
    invisible from inside a stubbed test."""
    mount = _dmg_with_app(tmp_path)
    calls: list[list[str]] = []

    with patch.object(su.subprocess, "run", _run_stub(mount, detach_rc=[1, 1], calls=calls)):
        su._detach_dmg(mount)

    for c in [c for c in calls if c[:2] == ["hdiutil", "detach"]]:
        assert "-quiet" not in c, "restored the flag that blanks stderr"
