"""A laptop that wakes must not keep reporting itself asleep.

Once the tray started rendering ``status_text`` for PAUSED (#214), the sentence
written on the way DOWN — "Sleeping", "Screen locked" — became visible on the
way back UP. Both resume handlers early-return when the user had also paused
manually (`system_event_handler.py`, the `user_paused` branches), and neither
touched the tray on that path, so nothing cleared the stale sentence and nothing
else would until the next state change.

The mutation matrix is why this file exists: reverting the two re-asserts left
all 49 tests in the tray suite GREEN. The fix was real and completely
unwitnessed, which is indistinguishable from not having made it.

Driving the REAL TrayIcon rather than a Mock. A mock tray records the call and
renders nothing, so it can only answer "was set_state invoked" — the question
here is what the row SAYS, and a mock cannot be wrong about it (Phantom 7).
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from src.system_event_handler import SystemEventHandler
from src.ui.tray import TrayIcon, TrayState


def _make_tray() -> TrayIcon:
    with patch("src.ui.tray.pystray"):
        tray = TrayIcon(
            on_login=lambda: None, on_logout=lambda: None, on_pause=lambda: None,
            on_resume=lambda: None, on_quit=lambda: None,
        )
    tray._icon = MagicMock()
    tray._update_icon = MagicMock()
    tray._update_menu = MagicMock()
    return tray


def _make_handler(tray: TrayIcon) -> SystemEventHandler:
    coordinator = MagicMock()
    coordinator.is_on_break = False
    coordinator.paused_by_network = False
    sync_engine = MagicMock()
    sync_engine.is_private = False
    return SystemEventHandler(
        sync_engine=sync_engine,
        tray=tray,
        coordinator=coordinator,
        reminder_manager=MagicMock(),
        bf=MagicMock(),
        aw=MagicMock(),
        pause_state_lock=threading.RLock(),
        shutdown_fn=MagicMock(),
    )


_RESUME_PATHS = [
    ("on_system_sleep", "on_system_wake", "Sleeping"),
    ("on_screen_lock", "on_screen_unlock", "Screen locked"),
]


@pytest.mark.parametrize("down,up,sentence", _RESUME_PATHS)
def test_the_resume_path_stops_reporting_the_cause_that_ended(down, up, sentence):
    """The user had paused manually, so both handlers take the early return that
    keeps the agent paused. The state is right; the SENTENCE is stale."""
    tray = _make_tray()
    handler = _make_handler(tray)
    with handler._pause_state_lock:
        handler._user_paused = True

    getattr(handler, down)()
    assert tray._get_status_text() == sentence, "precondition: the cause is on screen"

    getattr(handler, up)()

    assert tray.model.state is TrayState.PAUSED, "still paused — only the reason ended"
    assert tray._get_status_text() == "Paused", (
        f"the tray still says {sentence!r} after {up}"
    )


@pytest.mark.parametrize("down,up,sentence", _RESUME_PATHS)
def test_the_same_holds_when_a_BREAK_is_what_keeps_it_paused(down, up, sentence):
    """The second early return on each resume path, found by walking the class
    rather than the reported instance.

    Both handlers have TWO reasons to stay paused: a manual pause and an active
    break. The first was fixed because it was the one under the microscope; this
    branch sits two lines below it, reached whenever someone's laptop sleeps or
    locks during a break, and it had exactly the same stale sentence.

    Note ``tray.model.on_break`` stays False here on purpose. ``_get_status_text``
    checks that FLAG before the state, so a tray that already knows it is on a
    break renders "On Break (Nm left)" and hides the defect. The failing case is
    the coordinator knowing about the break while the tray's flag does not —
    which is precisely the state these handlers are called in.
    """
    tray = _make_tray()
    handler = _make_handler(tray)
    handler.coordinator.is_on_break = True

    getattr(handler, down)()
    assert tray._get_status_text() == sentence, "precondition: the cause is on screen"

    getattr(handler, up)()

    assert tray._get_status_text() == "Paused", (
        f"the tray still says {sentence!r} after {up} during a break"
    )
