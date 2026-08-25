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
needs; the cost of a leaked mount is a stale entry in `/Volumes`, and the cost
of the abort was a fleet device stuck on an old build.

The same construct has a second defect that is invisible in the incident logs:
**`raise` inside `finally` REPLACES any exception still in flight.** If
`copytree` had been the real failure, that cause would be discarded and the
operator would be told the problem was `detach`. `test_a_real_failure_is_not_
relabelled` pins that direction, because fixing only the first defect leaves it.

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
    """Stand in for hdiutil. `attach` materialises the mount point the way the
    real command would, so the copy under test has something real to copy."""
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
    """Both attempts fail. A leaked mount is a stale entry in /Volumes; the
    alternative is a device stuck below the minimum version floor."""
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
