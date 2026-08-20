"""Capture-permission state as a surface that cannot be missed.

#204: the agent asks for a missing Accessibility grant through a notification,
and `_send_macos_pyobjc` returns True whenever `deliverNotification_` did not
raise -- which it never does. macOS silently discards notifications from an app
never authorised to post them, so the one moment the message matters is the
moment the channel is least likely to work. Five devices sat with empty window
titles for up to 21 days; four were already running the release that added the
prompt. A Diagnostics row has no such failure mode.

The row is a PURE FORMATTER over values the model already carries. It must not
probe: `_create_menu` runs inside `_menu_lock`, and `check_accessibility` falls
through to a live AX call against the frontmost app in exactly the state this
row reports. Both sibling rows say so in their own docstrings.

**Headless.** pystray binds its backend at import, so on the ubuntu PR runner
`tray.pystray` is None and TrayIcon() raises. Label rules are asserted on the
pure function; the callsite guard renders the REAL `_create_menu()` with Item
swapped for a recorder, so dropping the row from production fails a test.
"""

from unittest.mock import MagicMock, patch

import pytest

from tests.remedy_wording import conveys_the_off_then_on_remedy
from src.ui import tray as tray_mod
from src.ui.tray import TrayIcon, capture_permissions_row

T, F = True, False


class _RecordedItem:
    instances: list = []

    def __init__(self, text, action=None, enabled=True, **kwargs):
        self.text = text
        self.action = action
        self.enabled = enabled
        _RecordedItem.instances.append(self)

    def __call__(self, icon=None):
        if self.action is not None:
            self.action(icon, self)


def _render_real_menu(icon) -> list:
    _RecordedItem.instances = []
    with patch.object(tray_mod, "Item", _RecordedItem), \
            patch.object(tray_mod, "pystray", MagicMock()):
        icon._create_menu()
    return list(_RecordedItem.instances)


def _make_tray() -> TrayIcon:
    with patch.object(tray_mod, "pystray", MagicMock()):
        return TrayIcon()


def _capture_rows(icon) -> list:
    return [str(i.text) for i in _render_real_menu(icon)
            if str(i.text).startswith("Capture permissions:")]


# ── The label ──────────────────────────────────────────────────────────

def test_healthy_state_says_OK():
    assert capture_permissions_row("Darwin", accessibility=T, input_monitoring=T) \
        == "Capture permissions: OK"


@pytest.mark.parametrize("a,i", [(F, T), (T, F), (F, F)])
def test_EVERY_failure_state_carries_the_off_then_on_remedy(a, i):
    """The non-guessable fact, and it was witnessed on only one branch before.

    Stripping the remedy from the Input Monitoring branch used to leave the
    suite green, because the naive `"on" in row` is satisfied by the "on" inside
    "Monitoring". Both-blocked is the commonest real state — one signature
    change kills both TCC entries together — so the branch most devices hit was
    the one with no guard at all.
    """
    row = capture_permissions_row("Darwin", accessibility=a, input_monitoring=i)
    assert conveys_the_off_then_on_remedy(row), f"no remedy in: {row}"


def test_it_names_accessibility_and_its_consequence_only():
    row = capture_permissions_row("Darwin", accessibility=F, input_monitoring=T)
    assert "Accessibility" in row and "window titles" in row.lower()
    assert "Input Monitoring" not in row


def test_it_names_input_monitoring_without_blaming_titles():
    row = capture_permissions_row("Darwin", accessibility=T, input_monitoring=F)
    assert "Input Monitoring" in row and "keystroke" in row.lower()
    assert "window titles" not in row.lower()
    assert "Accessibility" not in row


def test_it_names_BOTH_when_both_are_missing():
    row = capture_permissions_row("Darwin", accessibility=F, input_monitoring=F)
    assert "Accessibility" in row and "Input Monitoring" in row


@pytest.mark.parametrize("a,i", [(T, T), (T, F), (F, T), (F, F)])
def test_it_says_OK_exactly_when_capture_is_actually_possible(a, i):
    """The rot guard: OK must track the real conjunction, not drift from it.

    Reddens if someone adds a third grant to the capture definition and teaches
    only one side about it -- which is the failure that cost the blind days.
    """
    row = capture_permissions_row("Darwin", accessibility=a, input_monitoring=i)
    assert (row == "Capture permissions: OK") == (a and i)


@pytest.mark.parametrize("system", ["Windows", "Linux"])
def test_off_macos_it_says_not_applicable_rather_than_vanishing(system):
    """Adversarial fixture: BOTH grants false, so the platform gate has to win.

    Passing True/True would prove nothing the gate short-circuits anyway. And
    the row is present rather than absent because an absent row is
    indistinguishable from an agent too old to have one.
    """
    row = capture_permissions_row(system, accessibility=F, input_monitoring=F)
    assert row == f"Capture permissions: not applicable on {system}"


# ── The callsite: the row must reach the real menu, from the MODEL ─────

def test_the_row_is_in_the_menu_and_reads_the_model(monkeypatch):
    """Phantom 3, plus proof it renders model state rather than a live probe."""
    monkeypatch.setattr(tray_mod.platform, "system", lambda: "Darwin")
    icon = _make_tray()
    with icon.model.lock:
        icon.model.accessibility_ok = False
        icon.model.input_monitoring_ok = True

    rows = _capture_rows(icon)
    assert len(rows) == 1, f"expected exactly one capture row, got {rows}"
    assert "Accessibility" in rows[0]
    assert conveys_the_off_then_on_remedy(rows[0])


def test_the_rendered_row_follows_the_model_when_the_user_fixes_it(monkeypatch):
    """The point of reading the model: the row must CHANGE after the remedy.

    A live probe under _menu_lock could not do this without a rebuild trigger,
    and the permission booleans were not in the snapshot before. A row still
    saying "blocked" after the user fixed it is the dead end this closes.
    """
    monkeypatch.setattr(tray_mod.platform, "system", lambda: "Darwin")
    icon = _make_tray()
    with icon.model.lock:
        icon.model.accessibility_ok = False
        icon.model.input_monitoring_ok = False
    assert "blocked" in _capture_rows(icon)[0]

    with icon.model.lock:
        icon.model.accessibility_ok = True
        icon.model.input_monitoring_ok = True
    assert _capture_rows(icon) == ["Capture permissions: OK"]


def test_the_menu_never_probes_the_os_for_this_row(monkeypatch):
    """It must not fork an AX call under _menu_lock. Siblings say so explicitly.

    Rendering with the probes replaced by exploding stubs: if _create_menu still
    calls either, this test raises rather than silently costing IPC per rebuild.
    """
    import src.ui.permissions as perms

    def _boom():  # pragma: no cover - only runs if the row regresses
        raise AssertionError("capture row probed the OS during _create_menu()")

    monkeypatch.setattr(tray_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(perms, "check_accessibility", _boom)
    monkeypatch.setattr(perms, "check_input_monitoring", _boom)
    icon = _make_tray()
    assert len(_capture_rows(icon)) == 1
