"""The notice window: acknowledgement semantics and text parity.

These must RUN in CI, not skip. A Linux runner has no display, so ``tkinter`` is
replaced wholesale with a recorder and the window's own callbacks are driven
directly — the same shape as ``test_tray_state_transitions``' pystray patch. A
test that skips in the only environment that gates merges is not a guard.

Two things are actually being pinned:

1. **Closing the window is not an acknowledgement.** The record has to mean
   someone dismissed the text deliberately, or it is not evidence of anything.
2. **The window renders the versioned text.** The version recorded against an
   acknowledgement is a hash of ``notice_lines()``; any copy hard-coded in the
   window would be text the user read and the record does not describe.
"""

import inspect
from unittest.mock import MagicMock, patch

from src import privacy_notice as pn
from src.ui import privacy_notice_window as win


def _drive(*, press_button: bool = False, close_window: bool = False):
    """Run the real show_privacy_notice() against a recorded tkinter."""
    tk_mock = MagicMock()
    root = MagicMock()

    def _mainloop():
        if press_button:
            # The ack control is a tk.Label with a "<Button-1>" binding, not a
            # tk.Button (Aqua ignores a Button's bg — see the window module).
            # Drive the REAL bound callback, so this exercises the same click
            # path a user takes rather than a stand-in.
            binds = tk_mock.Label.return_value.bind.call_args_list
            click = next(c for c in binds if c.args[0] == "<Button-1>")
            click.args[1](None)  # the callback takes an event; a dummy is fine
        if close_window:
            root.protocol.call_args.args[1]()

    root.mainloop.side_effect = _mainloop

    with patch.object(win, "tk", tk_mock):
        result = win.show_privacy_notice(_root_factory=lambda: root)
    return result, tk_mock, root


def test_pressing_the_button_acknowledges():
    result, _tk, root = _drive(press_button=True)
    assert result is True
    root.destroy.assert_called()


def test_closing_the_window_is_not_an_acknowledgement():
    """The half that keeps the record honest — dismissal must be deliberate."""
    result, _tk, root = _drive(close_window=True)
    assert result is False
    root.destroy.assert_called()


def test_walking_away_is_not_an_acknowledgement():
    result, _tk, _root = _drive()
    assert result is False


def test_the_window_renders_the_versioned_text():
    """Every disclosure line the version hashes must reach the widget."""
    _result, tk_mock, _root = _drive(press_button=True)
    inserted = tk_mock.Text.return_value.insert.call_args.args[1]
    for line in pn.notice_lines():
        assert line in inserted, f"the window dropped the line {line!r}"


def test_the_window_shows_the_notice_title_and_subtitle():
    _result, tk_mock, _root = _drive(press_button=True)
    label_texts = [c.kwargs.get("text") for c in tk_mock.Label.call_args_list]
    assert pn.NOTICE_TITLE in label_texts
    assert pn.NOTICE_SUBTITLE in label_texts


def test_the_body_is_read_only():
    _result, tk_mock, _root = _drive(press_button=True)
    tk_mock.Text.return_value.config.assert_any_call(state=tk_mock.DISABLED)


# ── The parity guard ────────────────────────────────────────────────────

def test_the_window_does_not_carry_its_own_copy_of_the_notice():
    """Reading the source is the only thing that catches a second copy.

    A hard-coded duplicate that happens to match today passes every rendering
    assertion above and drifts the first time only one of them is edited
    (one-rule-one-implementation.md).
    """
    source = inspect.getsource(win)
    assert "notice_lines()" in source, "the window stopped calling the shared renderer"
    for fragment in (
        "dpo@betterqa.co",
        "hardware serial number",
        "keystrokes",
        "as recorded",
        "Regulamentul Intern",
    ):
        assert fragment not in source, (
            f"the window hard-codes {fragment!r} instead of rendering "
            "NOTICE_SECTIONS — the recorded version would not describe what "
            "the user read"
        )
