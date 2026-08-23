"""Whether the user was actually told has to end up somewhere readable (#204).

Two notices carry an instruction only the person at the keyboard can act
on, and the agent is otherwise stuck until they do:

* Rosetta (#188, v1.5.118) — without it the Mac records **zero** time.
* Accessibility (#197, v1.5.124) — without it window titles are empty.

Both called ``send_notification`` and discarded the result. So when the
Rosetta notice failed to help Carmen Lapusan on v1.5.122 — four releases
after it shipped — there was no artifact anywhere saying whether she had
ever been shown it. These tests pin the outcome into the log, and for the
zero-time case into the ops ingest, so the question is answerable next
time.

They assert on the CONSUMER (a log record, a reporter call), never on the
value ``send_notification`` returned, because a return value nobody reads
is the shape of the bug being fixed.
"""

import logging
from unittest.mock import MagicMock, patch

from src.aw_manager import AWManager
from src.notifications import NotificationOutcome
from src.sync.macos_window_watcher import MacOSWindowWatcher

ROSETTA_NOTIFY = "src.notifications.send_notification"


def _mgr(reporter=None) -> AWManager:
    mgr = AWManager(aw_port=5600)
    mgr._rosetta_notified = False
    mgr.error_reporter = reporter
    return mgr


def _errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


def _infos(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]


class TestRosettaNotice:
    def test_an_undelivered_rosetta_notice_is_logged_as_an_error(self, caplog):
        mgr = _mgr()
        with caplog.at_level(logging.DEBUG), \
             patch(ROSETTA_NOTIFY, return_value=NotificationOutcome.SUPPRESSED):
            mgr._notify_rosetta_required_once()

        assert any("NOT delivered" in m for m in _errors(caplog)), (
            "a notice the user never saw must not pass in silence"
        )
        assert any("suppressed" in m for m in _errors(caplog)), (
            "the log has to say WHICH way it failed"
        )

    def test_an_undelivered_rosetta_notice_reaches_the_ops_ingest(self):
        """The device records no time and the user has not been asked to
        fix it. Nobody reads the agent's local log — that is #194's whole
        finding — so this one has to leave the machine."""
        reporter = MagicMock()
        mgr = _mgr(reporter)
        with patch(ROSETTA_NOTIFY, return_value=NotificationOutcome.SUPPRESSED):
            mgr._notify_rosetta_required_once()

        assert reporter.capture.call_count == 1
        kwargs = reporter.capture.call_args.kwargs
        assert kwargs["level"] == "error"
        assert kwargs["fingerprint"] == "rosetta-notice-undelivered"
        assert kwargs["tags"]["notification_outcome"] == "suppressed"

    def test_a_delivered_rosetta_notice_does_not_page_ops(self):
        """The negative control. Without it, 'reports undelivered notices'
        would be satisfied by a reporter that fires on every notice, which
        is indistinguishable from one that measures nothing."""
        reporter = MagicMock()
        mgr = _mgr(reporter)
        with patch(ROSETTA_NOTIFY, return_value=NotificationOutcome.DELIVERED):
            mgr._notify_rosetta_required_once()

        reporter.capture.assert_not_called()

    def test_a_delivered_rosetta_notice_does_not_claim_the_user_read_it(self, caplog):
        """DELIVERED means macOS filed it. A Focus mode files it silently,
        so the log must not upgrade that into 'the user knows'."""
        mgr = _mgr()
        with caplog.at_level(logging.DEBUG), \
             patch(ROSETTA_NOTIFY, return_value=NotificationOutcome.DELIVERED):
            mgr._notify_rosetta_required_once()

        assert not _errors(caplog)
        assert any("not proof" in m.lower() for m in _infos(caplog))

    def test_an_unknown_outcome_is_treated_as_undelivered(self):
        """UNKNOWN is not success. Reporting it is the conservative
        direction: a false alarm costs one look, and the alternative is the
        silence that let five devices sit degraded for 12-17 days."""
        reporter = MagicMock()
        mgr = _mgr(reporter)
        with patch(ROSETTA_NOTIFY, return_value=NotificationOutcome.UNKNOWN):
            mgr._notify_rosetta_required_once()

        assert reporter.capture.call_count == 1
        assert (
            reporter.capture.call_args.kwargs["tags"]["notification_outcome"]
            == "unknown"
        )

    def test_a_broken_reporter_does_not_stop_the_agent(self):
        reporter = MagicMock()
        reporter.capture.side_effect = RuntimeError("ingest down")
        mgr = _mgr(reporter)
        with patch(ROSETTA_NOTIFY, return_value=NotificationOutcome.SUPPRESSED):
            mgr._notify_rosetta_required_once()  # must not raise

    def test_a_raising_notifier_does_not_stop_the_agent(self, caplog):
        mgr = _mgr(MagicMock())
        with patch(ROSETTA_NOTIFY, side_effect=RuntimeError("boom")):
            mgr._notify_rosetta_required_once()  # must not raise
        assert mgr._rosetta_notified is True

    def test_the_notice_still_fires_only_once_per_process(self):
        reporter = MagicMock()
        mgr = _mgr(reporter)
        with patch(ROSETTA_NOTIFY, return_value=NotificationOutcome.SUPPRESSED) as n:
            mgr._notify_rosetta_required_once()
            mgr._notify_rosetta_required_once()
        assert n.call_count == 1
        assert reporter.capture.call_count == 1


class TestAccessibilityNotice:
    def _watcher(self) -> MacOSWindowWatcher:
        w = MacOSWindowWatcher(MagicMock(), poll_interval=0.1)
        w._accessibility_notified = False
        return w

    def test_an_undelivered_accessibility_notice_is_logged_as_an_error(self, caplog):
        w = self._watcher()
        with caplog.at_level(logging.DEBUG), \
             patch(ROSETTA_NOTIFY, return_value=NotificationOutcome.SUPPRESSED):
            w._notify_accessibility_required_once()

        assert any("NOT delivered" in m for m in _errors(caplog))
        assert any("suppressed" in m for m in _errors(caplog))

    def test_a_delivered_accessibility_notice_logs_no_error(self, caplog):
        w = self._watcher()
        with caplog.at_level(logging.DEBUG), \
             patch(ROSETTA_NOTIFY, return_value=NotificationOutcome.DELIVERED):
            w._notify_accessibility_required_once()

        assert not _errors(caplog)
        assert any("not proof" in m.lower() for m in _infos(caplog))

    def test_a_raising_notifier_does_not_stop_the_watcher(self):
        w = self._watcher()
        with patch(ROSETTA_NOTIFY, side_effect=RuntimeError("boom")):
            w._notify_accessibility_required_once()  # must not raise
        assert w._accessibility_notified is True
