"""#214: ``status_text`` reached the user on ERROR only, and seven states threw it away.

PR #213 taught ``_get_status_text`` to render ``model.status_text`` — for the
ERROR branch, because ERROR is the state #188 was about. Every other branch kept
returning its hard-coded label, so the sentence its writer had already composed
was stored, snapshotted, and discarded one line before the screen.

The cost is per-state and it is the same shape each time: a person whose session
expired, a person whose queue is 87% full and a person whose laptop is asleep are
all shown one generic word, and the sentence that would tell them what happened
never renders. ``src/main.py:2846`` says why that matters, about its own writer:
"A keychain we cannot open is not an offline agent, and telling the user it is
sends them to check their wifi."

**The membership question is the actual defect, not the eight branches.**
``set_state`` decided which states own the field (``faulted``: ERROR and
QUEUE_WARNING) and ``_get_status_text`` decided again, differently (ERROR alone).
One rule, two implementations, disagreeing — and the disagreement is not
academic: the clears are what stop a stale sentence rendering under the next
state's label, so a branch that renders without a matching clear is worse than
the generic label it replaced.

So the fix is a single ``STATUS_TEXT_STATES``, and the guard below is derived
FROM it rather than hand-listing states: it walks every member of ``TrayState``
and asserts the partition in both directions. A state added to the enum later
is covered without anyone remembering to extend a list here.

The eight owners were enumerated from the writers, not from the issue — which
listed five. ``NEEDS_PERMISSIONS`` writes ``status_text`` directly under the
model lock at ``src/main.py:810`` rather than through ``set_state``, and
``STARTING`` receives one from ``_set_startup_status``; both are invisible to a
grep for ``set_state(TrayState.X, "..."``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.ui import tray as tray_module
from src.ui.tray import STATUS_TEXT_STATES, TrayIcon, TrayState

SENTINEL = "a sentence only its writer could have composed"


def _make_tray() -> TrayIcon:
    """A real TrayIcon with only the display backend stubbed.

    Same construction as ``tests/test_tray_status_text_renders.py`` — the branch
    under test is the genuine article.
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


# ── The defect, one test per state that was discarding its sentence ──────
#
# Parametrised over the REAL writers' states with the REAL text they write, so a
# failure names the state rather than an index. The sentences are copied from the
# writers rather than imported: this file asks "does the tray render what it was
# handed", and importing the producer's literal would move both sides of the
# assertion together (diagnosis-discipline.md Rule 6a).

@pytest.mark.parametrize(
    "state,written,generic",
    [
        (TrayState.QUEUE_WARNING, "Queue 87% full", "Offline (queue full)"),
        (TrayState.PRIVATE_HOURS,
         "Private hours — not recording (draining 12 queued)",
         "Outside working hours"),
        (TrayState.QUEUED, "Offline — reconnecting...", "Offline"),
        (TrayState.WAITING_AUTH, "Session expired, re-login required",
         "Waiting for login..."),
        (TrayState.PAUSED, "Screen locked", "Paused"),
        (TrayState.NEEDS_PERMISSIONS, "Input Monitoring permission required",
         "Permissions needed"),
        (TrayState.STARTING, "Restoring your session...", "Starting..."),
    ],
    ids=lambda v: getattr(v, "value", str(v)[:24]),
)
def test_each_state_renders_the_sentence_its_writer_composed(state, written, generic):
    """THE defect. Pre-fix every one of these returns ``generic`` and the text —
    correctly composed, correctly routed, correctly stored — dies here."""
    tray = _make_tray()

    tray.set_state(state, written)

    assert tray._get_status_text() == written, (
        f"{state.value} discarded its sentence and rendered the generic label"
    )
    # Equality, not ``generic not in row``: QUEUED's real sentence is
    # "Offline — reconnecting...", which contains its own generic label as a
    # substring, so the negative form fails against a correct fix. A test that
    # cannot pass against the right answer is a broken instrument.
    assert _app_status_row(tray) == f"App status: {written}"

    # The other direction, so ``generic`` is load-bearing rather than decoration:
    # the same state with nothing to say falls back to its own label.
    #
    # Entered from SYNCING rather than from a fresh tray. A fresh tray is seeded
    # ``state=STARTING, status_text="Starting..."``, so ``set_state(STARTING)``
    # on one is a same-state re-entry and the value read back is the SEED, not
    # the fallback — the two strings coincide, so the assertion passed while
    # testing nothing. A reviewer's in-memory mutant of that dict entry survived
    # the whole module because of it.
    bare = _make_tray()
    bare.set_state(TrayState.SYNCING)
    bare.set_state(state)
    assert bare._get_status_text() == generic


def test_the_menu_row_the_user_reads_carries_it():
    """The render path, not just the branch. A fix to ``_get_status_text`` that
    nothing calls would pass the tests above and change nothing on screen."""
    tray = _make_tray()

    tray.set_state(TrayState.WAITING_AUTH, "Session expired, re-login required")

    assert _app_status_row(tray) == "App status: Session expired, re-login required"


# ── The membership rule, derived rather than hand-listed ─────────────────


def test_every_state_either_owns_the_field_or_provably_ignores_it():
    """The partition, walked over the whole enum in BOTH directions.

    The negative half is the half that matters: it fails if the read leaks to the
    top of the function, which would make SYNCING render a stale fault sentence
    under a green icon — strictly worse than the generic label, and the exact
    harm ``set_state``'s entry-clear was added to prevent.
    """
    for state in TrayState:
        tray = _make_tray()
        tray.set_state(state, SENTINEL)
        rendered = tray._get_status_text()

        if state in STATUS_TEXT_STATES:
            assert rendered == SENTINEL, f"{state.value} owns the field but dropped it"
        else:
            assert rendered != SENTINEL, (
                f"{state.value} is not in STATUS_TEXT_STATES yet rendered the field; "
                "a state that renders it must also be cleared on entry"
            )


def test_every_ordered_pair_of_states_leaves_the_right_sentence():
    """The whole 11x11 transition matrix, replacing a source-shape guard.

    The first version of this asserted that ``inspect.getsource(set_state)``
    contains "STATUS_TEXT_STATES" and lacks "faulted = (". Both reviewers broke
    it, in both directions, and they were right:

    - restoring the exact defect (a local ``(ERROR, QUEUE_WARNING)`` tuple) left
      it GREEN, because the literal it greps for also appears in the comment
      above the line and in the docstring — a detector satisfied by prose
      describing the thing it looks for;
    - a behaviour-preserving extraction of the clear turned it RED.

    A guard that false-fails on a legitimate refactor gets loosened rather than
    fixed, so it was a liability rather than coverage. This asserts the RULE
    instead of the text, and no rename or extraction can defeat it.
    """
    for a in TrayState:
        for b in TrayState:
            tray = _make_tray()
            tray.set_state(a, SENTINEL)
            tray.set_state(b)  # no text: b has nothing of its own to say

            rendered = tray._get_status_text()
            retains = a is b and b in tray_module.STATUS_TEXT_RETAIN_ON_REENTRY

            if retains:
                assert rendered == SENTINEL, f"{a.value} -> {b.value} should retain"
            else:
                assert rendered != SENTINEL, (
                    f"{a.value} -> {b.value} leaked the previous sentence"
                )


def test_only_single_cause_states_retain_their_sentence_on_re_entry():
    """Retention is earned by having ONE cause, not by owning the field.

    PAUSED is the counter-example that forced this to become data: five writers
    (sleep, screen lock, idle, a user pause, a break) with five different
    sentences, so PAUSED -> PAUSED is normally a CHANGE of cause. Retaining
    there put "Screen locked" on an unlocked laptop and "Idle" on a machine
    whose user had just clicked Pause — while the pause notification fired
    beside it saying otherwise. All three read "Paused" on origin/main, so the
    widening introduced them.
    """
    assert tray_module.STATUS_TEXT_RETAIN_ON_REENTRY <= set(STATUS_TEXT_STATES), (
        "a state cannot retain a field it does not own"
    )
    assert TrayState.PAUSED not in tray_module.STATUS_TEXT_RETAIN_ON_REENTRY

    tray = _make_tray()
    tray.set_state(TrayState.PAUSED, "Idle")
    tray.set_paused(True)  # the real path: main.py:3467 -> tray.py set_state(PAUSED)
    assert tray._get_status_text() == "Paused"


# ── Controls: the floors, and no leaking across transitions ──────────────


@pytest.mark.parametrize("junk", ["", "   ", None, 12, object()])
def test_a_missing_or_junk_sentence_falls_back_to_the_states_own_label(junk):
    """Every owning state needs ERROR's floor, not just ERROR. An empty string
    would render ``App status:`` with nothing after it; a non-string would put a
    repr in front of the user."""
    for state in STATUS_TEXT_STATES:
        tray = _make_tray()
        tray.set_state(state, SENTINEL)
        with tray.model.lock:
            tray.model.status_text = junk

        rendered = tray._get_status_text()
        assert isinstance(rendered, str) and rendered.strip(), (state, junk)
        assert rendered != SENTINEL
        assert "object at 0x" not in rendered


def test_a_sentence_never_survives_into_a_different_state():
    """The ordering hazard #214 names. Rendering a state's ``status_text``
    requires that state to be covered by the entry-clear, or the previous
    state's sentence renders under the new label."""
    for state in STATUS_TEXT_STATES:
        tray = _make_tray()
        tray.set_state(TrayState.ERROR, SENTINEL)
        tray.set_state(state)  # no text: this state has nothing to say

        if state is TrayState.ERROR:
            continue  # same-state re-escalation deliberately retains, see #213
        assert tray._get_status_text() != SENTINEL, (
            f"a stale ERROR sentence leaked into {state.value}"
        )


def test_recovery_to_a_healthy_state_still_clears():
    """The #213 guard, re-witnessed now that the owning set is wider: a device
    that recovers must not keep telling the user to install Rosetta."""
    tray = _make_tray()

    tray.set_state(TrayState.ERROR, SENTINEL)
    tray.set_state(TrayState.SYNCING)

    assert _app_status_row(tray) == "App status: Active"


def test_a_partial_snapshot_still_answers():
    """``_get_status_text`` takes a snapshot dict, and a caller passing one
    without the key must get the generic label rather than a KeyError. Pins the
    property ``test_the_other_states_are_untouched_by_the_error_branch`` asserts
    for the ERROR branch, now that seven more branches read the same key."""
    tray = _make_tray()

    for state in STATUS_TEXT_STATES:
        partial = {
            "on_break": False,
            "private_mode": False,
            "state": state,
            "break_minutes_left": 0,
        }
        rendered = tray._get_status_text(partial)
        assert isinstance(rendered, str) and rendered.strip(), state

    # The other half, which the #213 version of this could not ask: a NON-owner
    # handed a snapshot that DOES carry the key must ignore it. Omitting the key
    # cannot distinguish "does not read it" from "reads a key I did not supply"
    # (Phantom 16, an agreement region) — and that is exactly why the guard
    # written to catch this widening did not catch it.
    for state in (TrayState.SYNCING, TrayState.PRIVATE, TrayState.ON_BREAK):
        supplied = {
            "on_break": False,
            "private_mode": False,
            "state": state,
            "break_minutes_left": 0,
            "status_text": SENTINEL,
        }
        assert tray._get_status_text(supplied) != SENTINEL, state


def test_the_break_and_private_flags_still_win_over_the_state():
    """Ordering control. ``on_break``/``private_mode`` are checked before the
    state, and widening the owning set must not reorder that."""
    tray = _make_tray()
    tray.set_state(TrayState.PAUSED, SENTINEL)

    with tray.model.lock:
        tray.model.on_break = True
        tray.model.break_minutes_left = 7

    assert tray._get_status_text() == "On Break (7m left)"
