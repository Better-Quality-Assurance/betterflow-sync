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

import pytest

from src.self_updater import BUNDLE_ID, find_stale_bundle_copies


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


def test_the_sweep_is_actually_called_at_startup():
    """Phantom 3: every test above passes the moment the function EXISTS.

    None of them says anything about whether the agent ever runs it, and an
    uncalled sweep leaves the #211 device exactly as it was. There is no cheap
    behavioural way to assert this — the call sits partway through a long
    startup method that builds a tray, a keychain and a scheduler — so this is
    a source-shape callsite guard and is worth only what such a guard is worth.

    Matching on a CALL (`name(`) rather than the bare name, and stripping
    comment lines first, so the guard cannot be satisfied by the prose that
    explains it (diagnosis-discipline Rule 9 — a detector poisoned by its own
    corpus). The control below proves the stripping actually happens.
    """
    from pathlib import Path as _P

    import src.main  # noqa: F401  — import proves the module is loadable

    source = _P(src.main.__file__).read_text()
    code = "\n".join(
        line for line in source.splitlines()
        if not line.strip().startswith("#")
    )

    assert "purge_stale_bundle_copies(" in code, (
        "the sweep is defined but nothing calls it"
    )
    # Control: the stripping is real, so the assertion above is about code.
    commented = "\n".join(
        line for line in source.splitlines() if line.strip().startswith("#")
    )
    assert "purge_stale_bundle_copies(" not in commented


def test_every_spelling_of_our_bundle_id_agrees():
    """The id is written in three modules for three different reasons, and a
    sweep that deletes on identity must not be reasoning from a stale copy of
    it. Pinned rather than refactored: threading one constant through the tray
    and the launch agent would couple modules that have no other reason to know
    about each other, for a string that changes approximately never.
    """
    from src import autostart
    from src.ui import permissions

    assert autostart.LAUNCHAGENT_LABEL == BUNDLE_ID
    assert permissions._BUNDLE_ID == BUNDLE_ID
