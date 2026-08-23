"""The macOS notifier must not report a delivery it never observed (#204).

``NSUserNotificationCenter.deliverNotification_`` returns nothing, so the
old ``return True`` after it meant "the call did not raise" while reading
as "the user was told". Two shipped user prompts — the Rosetta notice
(#188) and the Accessibility notice (#197) — were load-bearing on that.

Every test here drives a FAKE ``Foundation`` module installed into
``sys.modules``. That is deliberate on two counts: it works identically on
the Linux CI runner where pyobjc does not exist, and it lets a test stage
the one arrangement a real Mac will not stage on demand — macOS accepting
a notification and then keeping no record of it.

The fake's centre is the whole point. ``deliverNotification_`` returns
``None`` exactly as the real one does, so any code that infers success
from it is inferring it from nothing.
"""

# ruff: noqa: N802 — the fakes below must answer to the Objective-C selector
# names the code under test calls (setTitle_, deliveredNotifications, ...).
# Renaming them to snake_case would make the fakes stop standing in for the
# real API, which is the one thing they exist to do.
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import src.notifications as notifications

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeNote:
    """Stands in for NSUserNotification, recording what was set on it."""

    def __init__(self) -> None:
        self.title = None
        self.body = None
        self.sound_name = None
        self.content_image = None
        self._identifier = None

    # NSUserNotification.alloc().init()
    def init(self) -> "FakeNote":
        return self

    def setTitle_(self, value) -> None:
        self.title = value

    def setInformativeText_(self, value) -> None:
        self.body = value

    def setSoundName_(self, value) -> None:
        self.sound_name = value

    def setContentImage_(self, value) -> None:
        self.content_image = value

    def setIdentifier_(self, value) -> None:
        self._identifier = value

    def identifier(self):
        return self._identifier


class FakeNoteFactory:
    """NSUserNotification: ``alloc()`` then ``init()``."""

    def __init__(self) -> None:
        self.notes: list[FakeNote] = []

    def alloc(self) -> FakeNote:
        note = FakeNote()
        self.notes.append(note)
        return note


class FakeCenter:
    """NSUserNotificationCenter.

    ``keeps`` models the only thing that actually varies in the wild:
    whether macOS files the notification or silently drops it.
    """

    def __init__(self, keeps: bool = True, appear_after: int = 0) -> None:
        self.keeps = keeps
        self.appear_after = appear_after
        self.posted: list[FakeNote] = []
        self.read_calls = 0
        self.removed_all = 0

    def deliverNotification_(self, note):
        self.posted.append(note)
        # The defect in one line: the real selector returns void, so there
        # is nothing here for a caller to check.
        return None

    def deliveredNotifications(self):
        self.read_calls += 1
        if not self.keeps:
            return []
        if self.read_calls <= self.appear_after:
            return []
        return list(self.posted)

    def removeAllDeliveredNotifications(self) -> None:
        self.removed_all += 1


class ExplodingCenter(FakeCenter):
    """A centre whose read-back breaks after a successful post."""

    def deliveredNotifications(self):
        self.read_calls += 1
        raise RuntimeError("distributed object connection died")


class FakeFoundation:
    def __init__(self, center) -> None:
        self.NSUserNotification = FakeNoteFactory()
        self.NSUserNotificationCenter = MagicMock()
        self.NSUserNotificationCenter.defaultUserNotificationCenter.return_value = center


@pytest.fixture
def no_icon():
    """Keep AppKit out of it — the icon path is not under test here."""
    with patch.object(notifications, "_resolve_icon_path", return_value=None):
        yield


@pytest.fixture
def instant_poll(monkeypatch):
    """Zero the confirmation backoff so the suite does not sleep.

    Replaces the whole ``time`` attribute with ``raising=False`` rather
    than patching ``notifications.time.sleep``. The pre-fix module does
    not import ``time`` at all, so the obvious form dies in fixture setup
    and every test built on it ERRORS instead of failing — which reaches
    the subject exactly never, and would make a proof-of-failure run prove
    nothing (test_fixture_discipline Phantom 4).
    """
    sleeper = MagicMock()
    monkeypatch.setattr(
        notifications, "time", SimpleNamespace(sleep=sleeper), raising=False
    )
    return sleeper


def install_foundation(monkeypatch, center) -> FakeFoundation:
    """Put a fake ``Foundation`` where the function-local import will find it.

    Returns the fake so a test can assert against the note that was built.
    """
    fake = FakeFoundation(center)
    monkeypatch.setitem(sys.modules, "Foundation", fake)
    return fake


# ---------------------------------------------------------------------------
# The defect
# ---------------------------------------------------------------------------


class TestUnearnedSuccess:
    """These fail against the pre-fix implementation. That is the point."""

    def test_discarded_notification_is_not_reported_as_success(
        self, monkeypatch, no_icon, instant_poll
    ):
        """macOS took the notification and kept no record of it.

        Pre-fix this returns ``True`` — a delivery claim with nothing
        behind it. Asserted with ``is not True`` rather than against the
        new enum so the assertion means the same thing in both worlds.
        """
        center = FakeCenter(keeps=False)
        install_foundation(monkeypatch, center)

        outcome = notifications._send_macos_pyobjc("Title", "Body", False)

        assert center.posted, "precondition: the fake centre must have been posted to"
        assert outcome is not True
        assert outcome is notifications.NotificationOutcome.SUPPRESSED
        assert not outcome, "a suppressed notification must be falsy"

    def test_suppression_makes_the_osascript_fallback_reachable(
        self, monkeypatch, no_icon, instant_poll
    ):
        """The fallback was dead code while pyobjc always returned True.

        Also the proof the subprocess is faked: the assertion is on the
        RECORDED call, so a test that somehow shelled out for real, or that
        never reached the fallback, cannot satisfy it.
        """
        center = FakeCenter(keeps=False)
        install_foundation(monkeypatch, center)

        with patch.object(notifications.platform, "system", return_value="Darwin"), \
             patch.object(notifications, "_try_load_macos_pyobjc", return_value=True), \
             patch.object(notifications.subprocess, "run") as run:
            run.return_value = MagicMock(returncode=0, stderr=b"")
            outcome = notifications.send_notification("Title", "Body", sound=False)

        assert isinstance(run, MagicMock), "subprocess.run was not patched"
        assert run.call_count == 1, "the osascript fallback did not run"
        argv = run.call_args[0][0]
        assert argv[0] == "osascript"
        assert 'display notification "Body" with title "Title"' in argv[2]
        # A second, unreadable channel was tried, so the honest verdict is
        # neither the suppression we saw nor a delivery we did not.
        assert outcome is notifications.NotificationOutcome.UNKNOWN

    def test_delivered_notification_is_reported_delivered(
        self, monkeypatch, no_icon, instant_poll
    ):
        """The positive control. Without this, 'never reports success' would
        be trivially satisfiable by never reporting success at all."""
        center = FakeCenter(keeps=True)
        install_foundation(monkeypatch, center)

        outcome = notifications._send_macos_pyobjc("Title", "Body", False)

        assert outcome is notifications.NotificationOutcome.DELIVERED
        assert outcome, "a verified delivery must be truthy"

    def test_delivery_is_attributed_by_a_unique_identifier(
        self, monkeypatch, no_icon, instant_poll
    ):
        """Two sends must not confirm each other.

        Every notification this agent has ever posted carries
        ``identifier() is None``, so a delivered-list read could match any
        of them. Confirming on an untagged read would let an OLD delivered
        notification certify a NEW suppressed one.
        """
        center = FakeCenter(keeps=True)
        fake = install_foundation(monkeypatch, center)

        notifications._send_macos_pyobjc("Title", "Body", False)
        notifications._send_macos_pyobjc("Title", "Body", False)

        markers = [n.identifier() for n in fake.NSUserNotification.notes]
        assert all(m for m in markers), "every notification must be tagged"
        assert len(set(markers)) == len(markers), "identifiers must be unique per send"

    def test_a_stale_delivered_notification_cannot_certify_a_new_send(
        self, monkeypatch, no_icon, instant_poll
    ):
        """A centre already holding someone else's notification, which then
        drops ours, must still read as suppressed."""

        class PrepopulatedCenter(FakeCenter):
            def deliveredNotifications(self):
                self.read_calls += 1
                stale = FakeNote()
                stale.setIdentifier_("someone-elses-notification")
                untagged = FakeNote()  # identifier() is None, like every
                return [stale, untagged]  # notification we have ever posted

        center = PrepopulatedCenter(keeps=False)
        install_foundation(monkeypatch, center)

        outcome = notifications._send_macos_pyobjc("Title", "Body", False)

        assert center.read_calls > 0, "precondition: the delivered list was read"
        assert outcome is notifications.NotificationOutcome.SUPPRESSED


# ---------------------------------------------------------------------------
# Unknown is its own answer
# ---------------------------------------------------------------------------


class TestUnknownIsNotSuccess:
    def test_unreadable_delivery_is_unknown_not_delivered(
        self, monkeypatch, no_icon, instant_poll
    ):
        """The post worked and the read broke. We do not know, and saying
        either of the other two answers would be a guess."""
        center = ExplodingCenter(keeps=True)
        install_foundation(monkeypatch, center)

        outcome = notifications._send_macos_pyobjc("Title", "Body", False)

        assert center.posted, "precondition: the notification WAS posted"
        assert outcome is notifications.NotificationOutcome.UNKNOWN
        assert outcome is not notifications.NotificationOutcome.SUPPRESSED
        assert not outcome

    def test_nil_center_is_a_failure_not_an_unknown(
        self, monkeypatch, no_icon, instant_poll, caplog
    ):
        """``defaultUserNotificationCenter()`` is nil for a process the
        notification system will not talk to. Nothing was posted at all, so
        this is not merely unverifiable.

        The log line is asserted too, and that is the half that witnesses
        the guard. Deleting the nil check leaves the outcome at ``FAILED``
        anyway — posting to nil raises and the handler catches it — so the
        outcome alone cannot tell the guard from its absence. What changes
        is whether the operator reads "the notification system will not
        talk to this process" or a bare ``NoneType has no attribute``.
        """
        import logging

        install_foundation(monkeypatch, None)

        with caplog.at_level(logging.DEBUG):
            outcome = notifications._send_macos_pyobjc("Title", "Body", False)

        assert outcome is notifications.NotificationOutcome.FAILED
        messages = [r.getMessage() for r in caplog.records]
        assert any("NSUserNotificationCenter" in m for m in messages), (
            "a nil centre has to say so, not surface as a generic pyobjc error"
        )

    def test_osascript_success_is_unknown_never_delivered(self, monkeypatch):
        """osascript exiting 0 means the AppleScript ran. It says nothing
        about whether macOS presented anything."""
        with patch.object(notifications.subprocess, "run") as run:
            run.return_value = MagicMock(returncode=0, stderr=b"")
            outcome = notifications._send_macos_osascript("T", "B", False)

        assert run.call_count == 1, "subprocess.run was not exercised"
        assert outcome is notifications.NotificationOutcome.UNKNOWN
        assert outcome is not notifications.NotificationOutcome.DELIVERED

    def test_osascript_nonzero_exit_is_a_failure(self, monkeypatch):
        with patch.object(notifications.subprocess, "run") as run:
            run.return_value = MagicMock(returncode=1, stderr=b"boom")
            outcome = notifications._send_macos_osascript("T", "B", False)

        assert run.call_count == 1
        assert outcome is notifications.NotificationOutcome.FAILED

    def test_unsupported_platform_is_not_unknown(self):
        """We know perfectly well nothing was sent — that is not 'unknown'."""
        with patch.object(notifications.platform, "system", return_value="FreeBSD"):
            outcome = notifications.send_notification("T", "B")
        assert outcome is notifications.NotificationOutcome.UNSUPPORTED

    def test_windows_clean_exit_is_unknown(self):
        with patch.object(notifications.platform, "system", return_value="Windows"), \
             patch.object(notifications.subprocess, "run") as run:
            run.return_value = MagicMock(returncode=0, stderr=b"")
            outcome = notifications.send_notification("T", "B")
        assert run.call_count == 1
        assert run.call_args[0][0][0] == "powershell"
        assert outcome is notifications.NotificationOutcome.UNKNOWN

    def test_linux_clean_exit_is_unknown(self):
        with patch.object(notifications.platform, "system", return_value="Linux"), \
             patch.object(notifications, "_resolve_icon_path", return_value=None), \
             patch.object(notifications.subprocess, "run") as run:
            run.return_value = MagicMock(returncode=0, stderr=b"")
            outcome = notifications.send_notification("T", "B")
        assert run.call_count == 1
        assert run.call_args[0][0][0] == "notify-send"
        assert outcome is notifications.NotificationOutcome.UNKNOWN

    def test_missing_notify_send_is_a_failure(self):
        with patch.object(notifications.platform, "system", return_value="Linux"), \
             patch.object(notifications, "_resolve_icon_path", return_value=None), \
             patch.object(notifications.subprocess, "run", side_effect=FileNotFoundError):
            outcome = notifications.send_notification("T", "B")
        assert outcome is notifications.NotificationOutcome.FAILED


# ---------------------------------------------------------------------------
# The confirmation read itself
# ---------------------------------------------------------------------------


class TestDeliveryConfirmation:
    def test_confirmation_retries_because_delivery_is_asynchronous(
        self, monkeypatch, no_icon, instant_poll
    ):
        """Measured at ~0.1s on a real Mac, so a single read would call a
        healthy delivery suppressed."""
        center = FakeCenter(keeps=True, appear_after=2)
        install_foundation(monkeypatch, center)

        outcome = notifications._send_macos_pyobjc("Title", "Body", False)

        assert center.read_calls == 3, "the read must be retried, not attempted once"
        assert outcome is notifications.NotificationOutcome.DELIVERED

    def test_confirmation_backs_off_between_reads(
        self, monkeypatch, no_icon, instant_poll
    ):
        """Witnesses the sleep specifically: without it the retry loop would
        spin the whole budget away inside one millisecond and read as a
        single attempt against a slow centre."""
        center = FakeCenter(keeps=False)
        install_foundation(monkeypatch, center)

        notifications._send_macos_pyobjc("Title", "Body", False)

        assert instant_poll.call_count == notifications._DELIVERY_CONFIRM_ATTEMPTS - 1
        assert all(
            call.args[0] == notifications._DELIVERY_CONFIRM_INTERVAL
            for call in instant_poll.call_args_list
        )

    def test_confirmation_gives_up_rather_than_polling_forever(
        self, monkeypatch, no_icon, instant_poll
    ):
        center = FakeCenter(keeps=False)
        install_foundation(monkeypatch, center)

        notifications._send_macos_pyobjc("Title", "Body", False)

        assert center.read_calls == notifications._DELIVERY_CONFIRM_ATTEMPTS


# ---------------------------------------------------------------------------
# Behaviour that was already correct, pinned so the fix did not move it
# ---------------------------------------------------------------------------


class TestUnchangedBehaviour:
    def test_title_and_body_still_reach_the_notification(
        self, monkeypatch, no_icon, instant_poll
    ):
        center = FakeCenter(keeps=True)
        fake = install_foundation(monkeypatch, center)

        notifications._send_macos_pyobjc("A title", "A body", False)

        note = fake.NSUserNotification.notes[0]
        assert note.title == "A title"
        assert note.body == "A body"

    def test_sound_flag_is_still_honoured(self, monkeypatch, no_icon, instant_poll):
        center = FakeCenter(keeps=True)
        fake = install_foundation(monkeypatch, center)

        notifications._send_macos_pyobjc("T", "B", True)
        notifications._send_macos_pyobjc("T", "B", False)

        loud, quiet = fake.NSUserNotification.notes
        assert loud.sound_name == "NSUserNotificationDefaultSoundName"
        assert quiet.sound_name is None

    def test_a_raising_post_is_still_not_a_success(
        self, monkeypatch, no_icon, instant_poll
    ):
        """Already true pre-fix (it returned False). Pinned so the rewrite
        did not quietly convert an exception into a delivery."""

        class RaisingCenter(FakeCenter):
            def deliverNotification_(self, note):
                raise RuntimeError("nope")

        install_foundation(monkeypatch, RaisingCenter())

        outcome = notifications._send_macos_pyobjc("T", "B", False)

        assert outcome is not True
        assert not outcome

    def test_send_notification_never_raises(self, monkeypatch):
        """A notification that cannot be sent must not stop its caller."""
        with patch.object(notifications.platform, "system", return_value="Darwin"), \
             patch.object(notifications, "_try_load_macos_pyobjc", return_value=False), \
             patch.object(notifications.subprocess, "run", side_effect=Exception("x")):
            outcome = notifications.send_notification("T", "B")
        assert outcome is notifications.NotificationOutcome.FAILED
