"""The session greeting must report whether it actually arrived (#220).

The greeting is the only signal a user gets that BetterFlow started and is
tracking them. Before this fix all four login paths called
``send_notification(...)`` and dropped the return value, so a greeting macOS
suppressed and one it presented were indistinguishable from the logs -- the
field report ("good morning notification not sent on opening") could not be
investigated at all, because the grep for it returns 0 across the whole log
history whether it worked or not.

#204 added the delivery outcome precisely so a caller could tell. These tests
pin that this caller reads it.
"""

import logging
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.main import BetterFlowApp
from src.notifications import NotificationOutcome


def _app() -> BetterFlowApp:
    return BetterFlowApp.__new__(BetterFlowApp)


def _state(name="Timea Pop"):
    s = MagicMock()
    s.user_name = name
    return s


class TestGreetingReportsItsOwnDelivery:
    def test_undelivered_greeting_is_logged_with_the_outcome(self, caplog):
        """The whole point: a greeting that did not arrive must say so."""
        app = _app()
        with caplog.at_level(logging.DEBUG), patch(
            "src.main.send_notification",
            return_value=NotificationOutcome.SUPPRESSED,
        ) as send:
            app._announce_session(_state(), cold_launch=True)

        # Precondition: the subject was actually reached. Without this the
        # assertions below pass just as well when nothing ran at all.
        assert send.call_count == 1, "fixture never reached send_notification"

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "an undelivered greeting logged nothing -- #220"
        assert "suppressed" in warnings[0].getMessage().lower(), (
            "the log must name WHICH outcome, or support still cannot tell "
            "a suppressed notification from a broken code path"
        )

    def test_failed_greeting_is_not_read_as_success(self, caplog):
        """FAILED is falsy via NotificationOutcome.__bool__ -- pin that here,
        because `if send_notification(...)` on a bare Enum would be truthy."""
        app = _app()
        with caplog.at_level(logging.DEBUG), patch(
            "src.main.send_notification", return_value=NotificationOutcome.FAILED
        ):
            app._announce_session(_state(), cold_launch=False)
        assert [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_delivered_greeting_is_recorded_too(self, caplog):
        """A silent success is the other half of an uninvestigable failure."""
        app = _app()
        with caplog.at_level(logging.DEBUG), patch(
            "src.main.send_notification", return_value=NotificationOutcome.DELIVERED
        ):
            app._announce_session(_state(), cold_launch=True)

        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "delivered" in msgs.lower()
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
            "a delivered greeting must not warn"
        )


class TestGreetingWording:
    def test_cold_launch_uses_the_time_of_day_greeting(self):
        app = _app()
        with patch(
            "src.main.send_notification", return_value=NotificationOutcome.DELIVERED
        ) as send:
            app._announce_session(_state("Timea Pop"), cold_launch=True)
        title = send.call_args[0][0]
        assert "Timea" in title
        assert "Welcome back" not in title

    def test_resumed_session_says_welcome_back(self):
        app = _app()
        with patch(
            "src.main.send_notification", return_value=NotificationOutcome.DELIVERED
        ) as send:
            app._announce_session(_state("Timea Pop"), cold_launch=False)
        assert send.call_args[0][0] == "Welcome back, Timea!"

    def test_missing_name_does_not_produce_a_dangling_comma(self):
        app = _app()
        with patch(
            "src.main.send_notification", return_value=NotificationOutcome.DELIVERED
        ) as send:
            app._announce_session(_state(None), cold_launch=False)
        assert send.call_args[0][0] == "Welcome back!"


class TestNoLoginPathRerollsTheGreeting:
    """Callsite guard (one-rule-one-implementation).

    The extraction only helps if every login path uses it. Three of the four
    sites were byte-identical copies, so a fix to one reached none of the
    others -- that is the shape this guard exists to stop coming back.
    """

    def test_no_inline_greeting_send_survives_in_main(self):
        src = Path(__file__).resolve().parents[1] / "src" / "main.py"
        # Skip comment lines rather than regex-stripping comment BLOCKS: a
        # path glob inside a `#` comment would open a block that never closes.
        code = "\n".join(
            l for l in src.read_text(encoding="utf-8").splitlines() if not l.strip().startswith("#")
        )
        assert not re.search(r"send_notification\(\s*greeting\b", code), (
            "a login path is building the greeting inline again instead of "
            "calling _announce_session, so its delivery outcome is dropped"
        )
        # Control: the guard must be able to SEE greeting sends at all.
        assert "_announce_session(" in code
        assert code.count("self._announce_session(") == 4, (
            "expected all four login paths to route through the helper"
        )
