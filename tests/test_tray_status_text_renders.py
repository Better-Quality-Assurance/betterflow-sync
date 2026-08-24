"""#188 round 2: the remedy has to reach a surface, and ``status_text`` was not one.

The first pass at #188 routed a better sentence — "Not recording — Rosetta 2
required. Open Terminal and run: softwareupdate --install-rosetta" — from
``AWManager`` through ``SyncCoordinator`` into ``TrayIcon.set_state(...)``. Every
link in that chain was tested and every test passed. The user still saw
``App status: Error``.

``TrayIcon.model.status_text`` had **zero readers**. ``_snapshot_model`` copied
it into the snapshot; ``_create_menu`` rendered ``_get_status_text(s)``, whose
ERROR branch returned the constant ``"Error"`` and never looked at it; the
tooltip renders hours. So the field was write-only from its first commit, and
improving the string written into it could not change one character on screen.

**Why the existing suite could not see that.**
``tests/test_rosetta_capture_blocked_message.py`` sets ``c.tray = MagicMock()``
and asserts on ``coord.tray.set_state.call_args``. That is the right test for
the *routing* question it asks, and it is structurally incapable of answering
this one: the consumer it asserts against is a mock argument one layer ABOVE the
real consumer. A mock accepts any call and renders nothing (Phantom 7, one level
up). Every test in this file therefore drives the REAL ``TrayIcon``.

Two levels of witness, deliberately:

- ``_get_status_text`` — the function that holds the branch.
- ``_create_menu`` — the actual render path, asserted on the label list the menu
  is built from. That one fails if the branch is correct but nothing calls it,
  which is the exact class of defect this file exists for.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.ui.tray import TrayIcon, TrayState

# The literal the producer emits (src/aw_manager.py capture_blocked_remedy).
# Spelled out rather than imported: this file's question is "does the tray render
# the string it was handed", and importing the producer's constant would let a
# change to that constant move both sides of the assertion together.
_REMEDY = (
    "Not recording — Rosetta 2 required. Open Terminal and run: "
    "softwareupdate --install-rosetta"
)


def _make_tray() -> TrayIcon:
    """A real TrayIcon with only the display backend stubbed.

    ``pystray`` binds its backend at import time and has none on a headless
    runner, so the module-level name is patched — but ``TrayIcon`` itself,
    ``_get_status_text`` and ``_create_menu`` are the genuine article. Same
    construction ``tests/test_tray_state_transitions.py`` already uses.
    """
    with patch("src.ui.tray.pystray"):
        tray = TrayIcon(
            on_login=lambda: None,
            on_logout=lambda: None,
            on_pause=lambda: None,
            on_resume=lambda: None,
            on_quit=lambda: None,
        )
    tray._icon = MagicMock()
    tray._update_icon = MagicMock()
    tray._update_menu = MagicMock()
    return tray


def _menu_labels(tray: TrayIcon) -> list[str]:
    """Every label ``_create_menu`` builds, from the REAL menu build.

    ``Item`` is a module-level name in ``src.ui.tray``; recording it captures
    exactly what the menu is constructed from. The two hardware rows are stubbed
    because they probe the machine (``sysctl``/``ioreg``) and this test is about
    a string, not about silicon — everything else in the build runs for real.
    """
    labels: list[str] = []

    def record(label, *args, **kwargs):
        if isinstance(label, str):
            labels.append(label)
        return MagicMock()

    tray._serial_menu_item = MagicMock()
    tray._arch_menu_item = MagicMock()
    with patch("src.ui.tray.pystray"), patch("src.ui.tray.Item", side_effect=record):
        tray._create_menu()
    return labels


def _app_status_row(tray: TrayIcon) -> str:
    rows = [line for line in _menu_labels(tray) if line.startswith("App status:")]
    assert len(rows) == 1, f"expected exactly one App status row, got {rows}"
    return rows[0]


# ── The branch ──────────────────────────────────────────────────────────


def test_the_error_status_carries_the_sentence_it_was_given():
    """THE defect. Pre-fix this returns the constant "Error" and the remedy —
    correctly produced, correctly routed, correctly stored — dies here."""
    tray = _make_tray()

    tray.set_state(TrayState.ERROR, _REMEDY)

    assert tray._get_status_text() == _REMEDY


def test_the_error_status_reads_the_snapshot_too():
    """``_create_menu`` passes a snapshot rather than reading the model, so the
    snapshot path is a separate branch and gets its own witness. A fix applied
    to only one of the two leaves the menu — the surface that matters — unchanged.
    """
    tray = _make_tray()

    tray.set_state(TrayState.ERROR, _REMEDY)

    assert tray._get_status_text(tray._snapshot_model()) == _REMEDY


# ── The render path ─────────────────────────────────────────────────────
#
# The tests above pass against a `_get_status_text` nothing calls. These do not.


def test_the_menu_row_the_user_reads_names_the_remedy():
    """The whole point of #188: the persistent surface carries the command.

    Asserted on the label ``_create_menu`` actually builds, so deleting the read
    OR the call to ``_get_status_text`` reddens it.
    """
    tray = _make_tray()

    tray.set_state(TrayState.ERROR, _REMEDY)

    row = _app_status_row(tray)
    assert "Rosetta" in row, row
    assert "softwareupdate --install-rosetta" in row, row


def test_an_ordinary_error_still_reads_as_before():
    """The control, and it must degrade to the old constant.

    Also the witness for the ENTRY clear in ``set_state``. The model seeds
    ``status_text`` to ``"Starting..."``, so before that clear existed this
    rendered ``App status: Starting...`` under a red icon — found by running
    this very test, not by reading. Every escalation in ``src/`` passes a
    message, so nothing live lands here; the guard is on the shape.
    """
    tray = _make_tray()

    tray.set_state(TrayState.ERROR)

    assert tray._get_status_text() == "Error"
    assert _app_status_row(tray) == "App status: Error"


def test_a_previous_states_message_never_reappears_under_a_fault():
    """The realistic form of the case above. ``_check_permissions`` in main.py
    writes ``status_text`` directly for the NEEDS_PERMISSIONS hint. A later
    text-less escalation must not render that hint as the cause of THIS fault —
    a wrong sentence is what #188 was about, and rendering the field is what
    made that reachable."""
    tray = _make_tray()

    tray.model.status_text = "Grant Input Monitoring to record activity"
    tray.set_state(TrayState.NEEDS_PERMISSIONS)
    tray.set_state(TrayState.ERROR)

    assert _app_status_row(tray) == "App status: Error"


def test_a_queue_message_does_not_survive_into_a_tracker_fault():
    """`faulted` holds TWO states, so the first cut of the entry clear —
    `previous_state not in faulted` — read QUEUE_WARNING → ERROR as staying
    put and cleared nothing. The queue's own sentence then rendered as the
    cause of a tracker fault: a specific, confident, wrong diagnosis on the one
    surface #188 exists to make trustworthy.
    """
    tray = _make_tray()

    tray.set_state(TrayState.ERROR, _REMEDY)
    tray.set_state(TrayState.QUEUE_WARNING, "Queue 92% full")
    tray.set_state(TrayState.ERROR)

    assert _app_status_row(tray) == "App status: Error"


def test_a_stale_remedy_is_not_resurrected_by_a_hop_through_the_other_fault():
    """The nastier half of the same hole: two text-less transitions in a row.

    ERROR(remedy) → QUEUE_WARNING(no text) → ERROR(no text) cleared on neither
    hop, so a remedy from an outage that had already been superseded came back
    under a later, unrelated fault — the surface asserting a cause nobody
    established.
    """
    tray = _make_tray()

    tray.set_state(TrayState.ERROR, _REMEDY)
    tray.set_state(TrayState.QUEUE_WARNING)
    tray.set_state(TrayState.ERROR)

    row = _app_status_row(tray)
    assert row == "App status: Error"
    assert "Rosetta" not in row


def test_the_guarded_half_still_works():
    """The control the original guard was written for, kept explicit so a
    mutant that fixes the new door by breaking the old one is visible."""
    tray = _make_tray()

    tray.set_state(TrayState.ERROR, _REMEDY)
    tray.set_state(TrayState.SYNCING)
    tray.set_state(TrayState.ERROR)

    assert _app_status_row(tray) == "App status: Error"


def test_a_repeat_queue_warning_keeps_its_own_sentence():
    """The ERROR → ERROR retention, in its sibling state. The inequality has to
    preserve BOTH diagonals of `faulted`, not only the one that already had a
    test — a `previous_state == TrayState.ERROR` special-case would satisfy
    every other test in this file and silently blank this one.

    Asserted on the MODEL, deliberately, and it is the one test here that
    cannot use the menu row: `_get_status_text`'s QUEUE_WARNING branch returns
    the constant "Offline (queue full)" and reads no field. So this pins what
    `set_state` stores rather than what is drawn — the honest scope, and it
    stops being a lie the day that branch starts rendering too.
    """
    tray = _make_tray()

    tray.set_state(TrayState.QUEUE_WARNING, "Queue 92% full")
    tray.set_state(TrayState.QUEUE_WARNING)

    with tray.model.lock:
        assert tray.model.status_text == "Queue 92% full"


def test_a_repeat_escalation_keeps_the_sentence_it_already_carries():
    """The other side of the entry clear: ERROR → ERROR is the SAME outage
    escalating again, not a new one. Clearing there would blank the remedy on
    the second watchdog tick, which is the surface going dark exactly when the
    user is most likely to look at it."""
    tray = _make_tray()

    tray.set_state(TrayState.ERROR, _REMEDY)
    tray.set_state(TrayState.ERROR)

    assert tray._get_status_text() == _REMEDY


@pytest.mark.parametrize("junk", ["", "   ", None])
def test_blank_text_falls_back_rather_than_rendering_an_empty_row(junk):
    """``App status:`` with nothing after it is worse than ``Error`` — it reads
    as a broken app rather than a reported fault."""
    tray = _make_tray()

    tray.set_state(TrayState.ERROR, "something")
    tray.model.status_text = junk

    assert tray._get_status_text() == "Error"
    assert _app_status_row(tray) == "App status: Error"


def test_a_non_string_never_reaches_the_row():
    """Mirrors the guard ``_tracker_fault_message`` already carries one layer up:
    the value is interpolated verbatim, so a truthy non-string would put a repr
    in front of the user."""
    tray = _make_tray()

    tray.set_state(TrayState.ERROR, "something")
    tray.model.status_text = MagicMock()

    assert tray._get_status_text() == "Error"


def test_recovery_stops_rendering_the_stale_sentence():
    """``set_state`` clears ``status_text`` on ERROR → healthy. That clear was
    previously unobservable — nothing rendered the field either way — so it now
    needs a witness on the surface: a device that recovers must not keep telling
    the user to install Rosetta."""
    tray = _make_tray()

    tray.set_state(TrayState.ERROR, _REMEDY)
    tray.set_state(TrayState.SYNCING)

    row = _app_status_row(tray)
    assert row == "App status: Active"
    assert "Rosetta" not in row


# ── The rest of the menu must not contradict the row ────────────────────


def _diag_rows(tray: TrayIcon) -> dict[str, str]:
    rows = {}
    for line in _menu_labels(tray):
        for key in ("ActivityWatch: ", "API: "):
            if line.startswith(key):
                rows[key.rstrip(": ")] = line
    return rows


def test_a_tracker_fault_does_not_also_claim_the_api_is_unreachable():
    """The whole menu is read at once, and it used to disagree with itself.

    `_check_api_status` derived `API: Unreachable` from TrayState.ERROR. Not
    one producer of that state implies an unreachable API — the two tracker
    escalations reach the backend on the same cycle to report the fault, and
    `_report_sync_failure` routes an unreachable API to QUEUED before it can
    get here. Pre-#188 the top row said "Error" and "API: Unreachable" read as
    elaboration; now the top row makes a specific, correct, actionable claim
    and the row below it names a different cause.
    """
    tray = _make_tray()

    tray.set_state(TrayState.ERROR, _REMEDY)

    rows = _diag_rows(tray)
    assert rows["ActivityWatch"] == "ActivityWatch: Not running"
    assert rows["API"] == "API: Connected", (
        "the menu contradicts the remedy one row above it"
    )


def test_the_queue_states_still_report_the_api_as_unreachable():
    """The control. ERROR was the wrong signal; the queue states are the right
    one, and removing them to silence the row above would blind the surface
    that exists to say "your events are not leaving this machine".
    """
    tray = _make_tray()

    for state in (TrayState.QUEUED, TrayState.QUEUE_WARNING):
        tray.set_state(state, None)
        assert _diag_rows(tray)["API"] == "API: Unreachable", state

    tray.set_state(TrayState.SYNCING)
    assert _diag_rows(tray)["API"] == "API: Connected"


def test_the_other_states_are_untouched_by_the_error_branch():
    """The ERROR branch reads a key its siblings do not. If that read leaked to
    the top of the function, every other state would start depending on a
    snapshot key — and callers passing a partial dict would raise instead of
    answering."""
    tray = _make_tray()

    def status_for(state):
        return tray._get_status_text(
            {"on_break": False, "private_mode": False,
             "state": state, "break_minutes_left": 0}
        )

    assert status_for(TrayState.SYNCING) == "Active"
    assert status_for(TrayState.PRIVATE_HOURS) == "Outside working hours"
    assert status_for(TrayState.STARTING) == "Starting..."
