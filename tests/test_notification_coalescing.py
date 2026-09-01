"""#221 — routine notices must replace each other; instruction notices must not.

#204 tags every notification with a one-shot identifier so the delivered-list
read-back is attributable to THIS post, which is the whole basis of DELIVERED.
A unique identifier is also exactly what tells macOS these are different
notifications, so it never replaces an earlier one: 18 screen unlocks in one
measured day produced 18 separate "Welcome back!" entries.

The cost is not clutter. It trains people to dismiss BetterFlow notifications
without reading them, and the same channel carries the Rosetta (#188) and
Accessibility (#194, #205) notices — one-shot messages a user must read and act
on, sitting in a stack of twenty routine ones.

So the two properties are in direct tension and the split is per-caller:
routine repeating notices pass a coalesce_key and give up a verdict nobody
reads; instruction notices keep the unique id and the verdict.
"""

from unittest.mock import patch

from src.notifications import NotificationOutcome, _send_macos_pyobjc


class FakeNote:
    def __init__(self):
        self.identifier = None

    def setTitle_(self, _):
        pass

    def setInformativeText_(self, _):
        pass

    def setSoundName_(self, _):
        pass

    def setContentImage_(self, _):
        pass

    def setIdentifier_(self, value):
        self.identifier = value


def _post(coalesce_key=None):
    """Drive the real sender with the Foundation layer stubbed out.

    Returns (outcome, identifier-actually-set).
    """
    note = FakeNote()

    class FakeNSUserNotification:
        @staticmethod
        def alloc():
            class _A:
                @staticmethod
                def init():
                    return note
            return _A()

    class FakeCenter:
        @staticmethod
        def defaultUserNotificationCenter():
            class _C:
                @staticmethod
                def deliverNotification_(_):
                    pass
            return _C()

    fake_foundation = type("M", (), {
        "NSUserNotification": FakeNSUserNotification,
        "NSUserNotificationCenter": FakeCenter,
    })

    with patch.dict("sys.modules", {"Foundation": fake_foundation}):
        with patch("src.notifications._resolve_icon_path", return_value=None):
            with patch(
                "src.notifications._confirm_macos_delivery",
                return_value=NotificationOutcome.DELIVERED,
            ):
                outcome = _send_macos_pyobjc("t", "m", False, coalesce_key)
    return outcome, note.identifier


def test_a_coalesce_key_produces_a_stable_identifier():
    """The regression. Two posts of the same kind must carry the SAME id, which
    is what makes macOS replace rather than stack."""
    _, first = _post(coalesce_key="welcome-back")
    _, second = _post(coalesce_key="welcome-back")

    assert first == second == "betterflow-welcome-back"


def test_without_a_key_the_identifier_is_still_unique():
    """The allowance witness for #204. Instruction notices must keep the
    per-post id, or the delivered read-back stops being attributable and
    DELIVERED becomes a guess."""
    _, first = _post()
    _, second = _post()

    assert first != second
    assert first.startswith("betterflow-")


def test_a_coalesced_post_does_not_claim_DELIVERED():
    """The honest half, and the reason this is not free.

    With a stable id a read-back hit could be this post's record or the
    leftover of the one it just replaced. The stub above returns DELIVERED
    deliberately: if the sender consulted it, this test would fail.
    """
    outcome, _ = _post(coalesce_key="welcome-back")

    assert outcome is NotificationOutcome.UNKNOWN
    assert not outcome, "UNKNOWN must stay falsy — only a verified delivery is truthy"


def test_an_uncoalesced_post_still_reports_the_verified_verdict():
    """The other side of the same witness: the verdict path is untouched for
    callers that did not opt in."""
    outcome, _ = _post()

    assert outcome is NotificationOutcome.DELIVERED
    assert outcome
