"""#211: three copies of the agent in /Applications, all under one bundle id.

`/Applications` held `BetterFlow.app` (1.5.125), `BetterFlow.app.old` (1.5.120)
and `BetterFlow-1.5.119-backup.app` (1.5.119) simultaneously, every one signed
by us and registered under `co.betterqa.betterflow`. The machine booted the
1.5.119 copy and stayed below the server's minimum floor for days, and the
Accessibility grant flapped — macOS keeps ONE row per app, and which copy that
row is understood to mean is not something we control.

Neither leftover name is one current code produces. `_apply_macos_update` makes
`<stem>.old.app` and removes it on success; `.app.old` is the shape the Linux
AppImage path builds. So these are sediment from older versions, which is
exactly why a sweep is needed rather than a fix to whichever writer is current:
the copies already exist on machines in the fleet and no future correctness
removes them.

**This deletes directories in /Applications, so the tests below are mostly about
what it must REFUSE to touch.** The load-bearing guard is the bundle id: a
candidate is removed only if its own Info.plist says it is us. Name patterns
alone are not enough — they are attacker-free but not accident-free, and a
sweep that trusts a filename is one rename away from deleting somebody's work.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from src.self_updater import (
    BUNDLE_ID,
    find_stale_bundle_copies,
    purge_stale_bundle_copies,
)


def _make_app(parent, name, bundle_id=BUNDLE_ID, version="1.0.0"):
    app = parent / name
    (app / "Contents").mkdir(parents=True)
    info = {"CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": version}
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    return app


def test_it_finds_the_three_shapes_that_have_actually_been_seen(tmp_path):
    """The two from the incident plus the one current code creates."""
    running = _make_app(tmp_path, "BetterFlow.app", version="1.5.125")
    old_suffix = _make_app(tmp_path, "BetterFlow.app.old", version="1.5.120")
    backup = _make_app(tmp_path, "BetterFlow-1.5.119-backup.app", version="1.5.119")
    dot_old = _make_app(tmp_path, "BetterFlow.old.app", version="1.5.118")

    found = set(find_stale_bundle_copies(running))

    assert found == {old_suffix, backup, dot_old}


def test_it_never_returns_the_running_bundle(tmp_path):
    """The one deletion that would brick the machine.

    Honest scope: today this passes because of the NAME filter, not the
    `entry == running_app` check — a bundle's own name cannot match a pattern
    built from its own stem, so the exclusion is unreachable and a mutation run
    correctly reports it as unwitnessed. That is defence in depth, not a gap,
    and it was proven as a pair rather than argued:

        neuter the name filter alone     -> running bundle NOT returned
        neuter the name filter AND it    -> running bundle IS returned

    Recorded here and at the call site so the next mutation run does not read a
    surviving mutant as dead code and delete the last guard on the running app.
    """
    running = _make_app(tmp_path, "BetterFlow.app")

    assert find_stale_bundle_copies(running) == []


def test_a_copy_with_someone_elses_bundle_id_is_left_alone(tmp_path):
    """THE safety guard. The name matches our pattern exactly; the identity does
    not. Name-only matching would delete this."""
    running = _make_app(tmp_path, "BetterFlow.app")
    _make_app(tmp_path, "BetterFlow.app.old", bundle_id="com.someoneelse.thing")

    assert find_stale_bundle_copies(running) == []


def test_a_bundle_we_cannot_identify_is_left_alone(tmp_path):
    """Fail closed. An unreadable or absent Info.plist means "I do not know
    whose this is", which is not permission to delete it."""
    running = _make_app(tmp_path, "BetterFlow.app")
    (tmp_path / "BetterFlow.app.old" / "Contents").mkdir(parents=True)
    (tmp_path / "BetterFlow.app.old" / "Contents" / "Info.plist").write_bytes(b"not a plist")
    _make_app(tmp_path, "BetterFlow.old.app").joinpath("Contents/Info.plist").unlink()

    assert find_stale_bundle_copies(running) == []


def test_unrelated_apps_of_ours_are_not_swept(tmp_path):
    """Scoped to copies of THIS bundle's name. Another BetterQA app that happens
    to sit in the same folder is not sediment from our updater.

    It carries a different bundle id, so the identity guard already covers it —
    this pins the NAME scope separately, so a later loosening of one guard does
    not silently rely on the other.
    """
    running = _make_app(tmp_path, "BetterFlow.app")
    _make_app(tmp_path, "SomeOtherTool.app.old", bundle_id="co.betterqa.othertool")

    assert find_stale_bundle_copies(running) == []


def test_it_does_not_leave_the_directory_it_was_given(tmp_path):
    """Siblings only. A copy one level down is not ours to reason about, and a
    recursive sweep of /Applications is not a thing this should ever do."""
    running = _make_app(tmp_path, "BetterFlow.app")
    nested = tmp_path / "nested"
    nested.mkdir()
    _make_app(nested, "BetterFlow.app.old")

    assert find_stale_bundle_copies(running) == []


def test_a_symlink_is_never_a_candidate(tmp_path):
    """Deleting through a symlink deletes the target. Whatever it points at,
    the link is not a stale copy our updater left."""
    running = _make_app(tmp_path, "BetterFlow.app")
    real = _make_app(tmp_path / "elsewhere", "BetterFlow.app")
    (tmp_path / "BetterFlow.app.old").symlink_to(real)

    assert find_stale_bundle_copies(running) == []


def test_a_plain_file_wearing_the_name_is_not_a_bundle(tmp_path):
    running = _make_app(tmp_path, "BetterFlow.app")
    (tmp_path / "BetterFlow.app.old").write_text("not a bundle")

    assert find_stale_bundle_copies(running) == []


@pytest.mark.parametrize("name", [
    "BetterFlow.app",           # the running one, under a different parent check
    "BetterFlowExtra.app",      # prefix match is not a pattern match
    "MyBetterFlow.app.old",     # suffix match is not a pattern match
    "BetterFlow.app.old.txt",   # trailing junk
    "BetterFlow-backup.app",    # no version segment
])
def test_names_outside_the_known_patterns_are_ignored(tmp_path, name):
    """The pattern list is deliberately closed. Anything we did not demonstrably
    create stays put — a leftover costs disk, a wrong deletion costs an app."""
    running = _make_app(tmp_path, "BetterFlow.app")
    if name != "BetterFlow.app":
        _make_app(tmp_path, name)

    assert find_stale_bundle_copies(running) == []


# ── The two things a helper-only test cannot say ────────────────────────


def test_run_dispatches_the_sweep():
    """Phantom 3: everything else here passes the moment the function EXISTS,
    and an uncalled sweep leaves the #211 device exactly as it was.

    Scoped to ``BetterFlowApp.run``'s own source rather than the whole 4200-line
    module, which narrows both error directions at once: a call moved out of
    ``run()`` reddens (correctly), and a stray mention elsewhere in main.py no
    longer greens it. Comment lines are stripped so the guard cannot be
    satisfied by the prose explaining it, with a control below proving the
    stripping happens.

    This pins ONE dispatch line, which is all a source grep is good for. What
    the method does is tested behaviourally below.
    """
    import inspect

    from src.main import BetterFlowApp

    source = inspect.getsource(BetterFlowApp.run)
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )

    assert "self._sweep_stale_bundle_copies()" in code, "run() does not call the sweep"
    commented = "\n".join(
        line for line in source.splitlines() if line.strip().startswith("#")
    )
    assert "self._sweep_stale_bundle_copies()" not in commented


def test_the_sweep_method_passes_the_resolved_bundle_and_logs_what_went(monkeypatch):
    """Behavioural, not textual: the method must resolve the running bundle and
    hand THAT to the purge. An earlier revision re-derived the path by string
    split, which diverges from the module's own resolver through a symlinked
    ancestor — different directory scanned, silently."""
    import src.main as main_mod
    from src.main import BetterFlowApp

    calls = []
    monkeypatch.setattr(main_mod.sys, "platform", "darwin")
    import src.self_updater as su

    monkeypatch.setattr(su, "_get_app_bundle_path", lambda: Path("/Applications/BetterFlow.app"))
    monkeypatch.setattr(su, "get_app_bundle_path", lambda: Path("/Applications/BetterFlow.app"))
    monkeypatch.setattr(
        su, "purge_stale_bundle_copies",
        lambda running: calls.append(running) or [Path("/Applications/BetterFlow.app.old")],
    )

    BetterFlowApp._sweep_stale_bundle_copies(object())

    assert calls == [Path("/Applications/BetterFlow.app")]


def test_the_sweep_does_nothing_off_macos(monkeypatch):
    """The patterns and the whole duplicate-registration problem are macOS's."""
    import src.main as main_mod
    from src.main import BetterFlowApp

    called = []
    monkeypatch.setattr(main_mod.sys, "platform", "win32")
    import src.self_updater as su

    monkeypatch.setattr(su, "purge_stale_bundle_copies", lambda r: called.append(r) or [])

    BetterFlowApp._sweep_stale_bundle_copies(object())

    assert called == []


def test_a_failing_sweep_never_stops_the_agent_starting(monkeypatch):
    """The one job it has. Tidying up is not worth a startup failure."""
    import src.main as main_mod
    from src.main import BetterFlowApp

    monkeypatch.setattr(main_mod.sys, "platform", "darwin")
    import src.self_updater as su

    monkeypatch.setattr(su, "get_app_bundle_path", lambda: Path("/Applications/BetterFlow.app"))
    monkeypatch.setattr(
        su, "purge_stale_bundle_copies",
        lambda r: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    BetterFlowApp._sweep_stale_bundle_copies(object())  # must not raise


def test_dev_mode_is_skipped_rather_than_guessed_at(monkeypatch):
    """No bundle means nothing to sweep siblings of. Guessing a path here would
    point the sweep at whatever directory the interpreter happens to live in."""
    import src.main as main_mod
    from src.main import BetterFlowApp

    called = []
    monkeypatch.setattr(main_mod.sys, "platform", "darwin")
    import src.self_updater as su

    monkeypatch.setattr(su, "get_app_bundle_path", lambda: None)
    monkeypatch.setattr(su, "purge_stale_bundle_copies", lambda r: called.append(r) or [])

    BetterFlowApp._sweep_stale_bundle_copies(object())

    assert called == []


def test_the_identity_macos_knows_us_by_is_written_once_per_reason():
    """The sweep deletes on identity, so it must not be reasoning from a stale
    copy of the id — and the AUTHORITY is none of the Python constants.

    What `find_stale_bundle_copies` compares against is `CFBundleIdentifier`
    out of a candidate's Info.plist, and that value is put there by
    `build.spec`. Change build.spec alone and two things break while a
    constants-only guard stays green: the sweep matches nothing, forever and
    silently; and `permissions._BUNDLE_ID`'s TCC insert writes a grant for a
    bundle id no installed app claims — Accessibility never granted, which is
    the OTHER symptom #211 reports.

    A source-shape check over a declaration file with exactly one spelling of
    the token is the one place such a check is defensible, and it is the only
    mechanical link to the authority available without doing a build.
    """
    from src.ui import permissions

    assert permissions._BUNDLE_ID == BUNDLE_ID, "same concept, must agree"

    spec = (Path(__file__).parent.parent / "build.spec").read_text()
    assert f'bundle_identifier="{BUNDLE_ID}"' in spec, (
        "build.spec is what actually stamps CFBundleIdentifier into the shipped "
        "bundle; the sweep compares against that, not against this constant"
    )


def test_the_launchd_label_currently_equals_the_bundle_id():
    """A COINCIDENCE, pinned so a divergence is deliberate — not an invariant.

    `LAUNCHAGENT_LABEL` is a launchd label that happens to equal the bundle id.
    Apple's own convention routinely gives an agent `<bundleid>.agent`, so a
    legitimate change there would redden a test documented as protecting the
    sweep — the "false-fails on a legitimate refactor gets loosened" shape this
    repo paid for in #218. If the label diverges on purpose, change THIS test,
    not BUNDLE_ID.
    """
    from src import autostart

    assert autostart.LAUNCHAGENT_LABEL == BUNDLE_ID


# ── The half that deletes ────────────────────────────────────────────────
#
# Everything above pins `find_`, which returns paths and touches nothing.
# `purge_` is the shipped function — the one with `shutil.rmtree` in it — and
# had no tests at all: a later edit dropping the `continue`, or widening the
# target, would have reddened nothing.


def test_purge_removes_exactly_what_find_named(tmp_path):
    running = _make_app(tmp_path, "BetterFlow.app")
    doomed = _make_app(tmp_path, "BetterFlow.app.old")
    spared = _make_app(tmp_path, "Unrelated.app", bundle_id="com.other.thing")

    removed = purge_stale_bundle_copies(running)

    assert removed == [doomed]
    assert not doomed.exists()
    assert running.exists(), "deleted the app that is executing"
    assert spared.exists(), "deleted a bundle that is not ours"


def test_one_copy_it_cannot_remove_does_not_abort_the_rest(tmp_path, monkeypatch):
    """The `continue`. Without it a single permissions error leaves every later
    copy in place, and the sweep quietly does a fraction of its job."""
    running = _make_app(tmp_path, "BetterFlow.app")
    stubborn = _make_app(tmp_path, "BetterFlow.app.old")
    removable = _make_app(tmp_path, "BetterFlow-1.5.119-backup.app")

    import src.self_updater as su

    real_rmtree = su.shutil.rmtree

    def rmtree(path, *a, **k):
        if Path(path) == stubborn:
            raise PermissionError("nope")
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(su.shutil, "rmtree", rmtree)

    removed = purge_stale_bundle_copies(running)

    assert removed == [removable], "the failure must not swallow the others"
    assert stubborn.exists()
    assert not removable.exists()


def test_the_return_value_lists_only_what_actually_went(tmp_path, monkeypatch):
    """A path that failed to delete must not be reported as removed — the
    caller logs this line as the fleet's record of what it cleaned."""
    running = _make_app(tmp_path, "BetterFlow.app")
    _make_app(tmp_path, "BetterFlow.app.old")

    import src.self_updater as su

    monkeypatch.setattr(
        su.shutil, "rmtree",
        lambda *a, **k: (_ for _ in ()).throw(OSError("busy")),
    )

    assert purge_stale_bundle_copies(running) == []


def test_purge_is_a_no_op_from_a_non_canonical_copy(tmp_path):
    """#211's actual device. It had booted BetterFlow-1.5.119-backup.app, where
    the patterns — built from the RUNNING stem — cannot see the canonical
    install's siblings. Returning nothing is the correct action (sweeping from
    here would have a backup copy delete the NEWER app beside it); the defect
    was that it happened silently, which the warning now fixes."""
    canonical = _make_app(tmp_path, "BetterFlow.app", version="1.5.125")
    backup = _make_app(tmp_path, "BetterFlow-1.5.119-backup.app", version="1.5.119")
    dot_old = _make_app(tmp_path, "BetterFlow.app.old", version="1.5.120")

    assert purge_stale_bundle_copies(backup) == []
    assert canonical.exists() and dot_old.exists() and backup.exists()
