"""#221 follow-up — a coalesced notice must be posted ONCE, not twice.

#221 gives routine notices a stable identifier so macOS replaces the previous
one, and pays for that by returning UNKNOWN instead of DELIVERED: with a stable
id the delivered-list read-back cannot tell this post's record from the leftover
of the one it just replaced. That trade is right.

The plumbing did not carry it out. ``send_notification`` short-circuits only on
DELIVERED, so the deliberate UNKNOWN fell through to the osascript fallback and
posted the notice a SECOND time -- and that second copy is the worst one
available:

  * it is attributed to Script Editor, so ``clear_notifications()`` explicitly
    cannot remove it (``_send_macos_osascript`` docstring);
  * ``display notification`` takes no identifier, so it cannot coalesce.

So the change written to stop routine notices stacking doubled their volume and
moved half of them onto a channel that stacks forever. It also spawned an
``osascript`` subprocess, with a 5s timeout, on every screen unlock.

These tests drive the REAL ``send_notification``. The #221 suite drives
``_send_macos_pyobjc`` directly, which is why it could not see this: the defect
is entirely in the caller's control flow, and no fixture in that file ever
reaches it.
"""

from unittest.mock import patch

import src.notifications as N
from src.notifications import NotificationOutcome


def _drive(coalesce_key, *, pyobjc_outcome=None):
    """Post through the real send_notification on a healthy-pyobjc Darwin.

    Returns (outcome, posts) where posts counts each channel that actually
    put a notification in front of the user.
    """
    posts = {"pyobjc": 0, "osascript": 0}

    def fake_pyobjc(title, message, sound, key=None):
        posts["pyobjc"] += 1
        if pyobjc_outcome is not None:
            return pyobjc_outcome
        # Mirror the real body's tail: a coalesced post returns UNKNOWN before
        # the read-back; an uncoalesced one gets a verdict from it.
        return (
            NotificationOutcome.UNKNOWN if key else NotificationOutcome.DELIVERED
        )

    def fake_osascript(title, message, sound):
        posts["osascript"] += 1
        return NotificationOutcome.UNKNOWN

    with patch.object(N.platform, "system", return_value="Darwin"), patch.object(
        N, "_try_load_macos_pyobjc", return_value=True
    ), patch.object(N, "_send_macos_pyobjc", side_effect=fake_pyobjc), patch.object(
        N, "_send_macos_osascript", side_effect=fake_osascript
    ):
        outcome = N.send_notification(
            "Tracking Paused", "body", sound=False, coalesce_key=coalesce_key
        )
    return outcome, posts


class TestACoalescedNoticeIsPostedOnce:
    def test_coalesced_post_does_not_fall_through_to_osascript(self):
        outcome, posts = _drive("tracking-state")

        # Precondition: we really did take the pyobjc path.
        assert posts["pyobjc"] == 1, "fixture never reached the pyobjc post"
        assert posts["osascript"] == 0, (
            "the coalesced notice was posted a SECOND time via osascript -- an "
            "identifier-less, Script-Editor-attributed copy that cannot coalesce "
            "and cannot be cleared, which is the exact stacking #221 removes"
        )
        assert outcome is NotificationOutcome.UNKNOWN

    def test_the_user_sees_exactly_one_notification(self):
        _, posts = _drive("welcome-back")
        assert posts["pyobjc"] + posts["osascript"] == 1

    def test_a_failed_coalesced_post_STILL_uses_the_fallback(self):
        """The carve-out is 'posted, unverifiable', never 'do not retry'.

        FAILED means the pyobjc mechanism itself broke, so nothing reached the
        user and the second channel is exactly what should run. Without this the
        fix would trade a double-post for a silently dropped notice.
        """
        outcome, posts = _drive(
            "tracking-state", pyobjc_outcome=NotificationOutcome.FAILED
        )
        assert posts["osascript"] == 1, (
            "a coalesced post whose mechanism FAILED reached nobody; the "
            "fallback channel must still run"
        )
        assert outcome is NotificationOutcome.UNKNOWN


class TestUncoalescedNoticesAreUnchanged:
    """Control: the #204 verdict path must not be touched by any of this."""

    def test_delivered_still_short_circuits(self):
        outcome, posts = _drive(None)
        assert outcome is NotificationOutcome.DELIVERED
        assert posts["osascript"] == 0

    def test_a_suppressed_instruction_notice_still_gets_the_second_channel(self):
        """The Rosetta/Accessibility notices depend on this fall-through."""
        outcome, posts = _drive(
            None, pyobjc_outcome=NotificationOutcome.SUPPRESSED
        )
        assert posts["osascript"] == 1
        assert outcome is NotificationOutcome.UNKNOWN


class TestTheCarveOutIsAPositiveAssertion:
    """`is UNKNOWN`, never `is not FAILED` (design-lens M-5).

    Both are correct today: a coalesced post can only return UNKNOWN (the
    by-construction value) or FAILED (the mechanism broke), because the
    coalesce return precedes the read-back. But a negative test says "anything
    except the one failure I thought of". If the coalesced path ever learns to
    read back, SUPPRESSED -- the OS dropped it, the user was NOT reached --
    would be silently swallowed by `is not FAILED`, which is the exact
    reassuring-direction failure NotificationOutcome exists to close.
    """

    def test_a_suppressed_coalesced_post_would_still_reach_the_fallback(self):
        outcome, posts = _drive(
            "tracking-state", pyobjc_outcome=NotificationOutcome.SUPPRESSED
        )
        assert posts["osascript"] == 1, (
            "the OS dropped this notice and the carve-out swallowed it; a "
            "coalesced post that was SUPPRESSED reached nobody"
        )
        assert outcome is NotificationOutcome.UNKNOWN
