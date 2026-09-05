"""A probe script must not be able to notify the real user.

The only guard against a live macOS notification was `_block_real_notifications`,
an AUTOUSE PYTEST FIXTURE in tests/conftest.py. It binds nothing outside pytest,
so any ad-hoc script that drives the real code path posts to the user's actual
Notification Center.

That is not hypothetical. On 2026-09-04 a review agent's scratchpad probe drove
`_rosetta_required=True` without mocking `_notify_rosetta_required_once`, and the
repo owner received a live "BetterFlow can't track on this Mac / Rosetta 2 is
required" notification generated entirely by a test. Agents in this repo drive
`_start_locked` in probes constantly, so the pytest-only guard is a foot-gun with
a known blast radius.

The fix is an env check inside `send_notification` itself, so probes, one-off
scripts and CI all get the guard without depending on the test runner.

UNSUPPORTED is the honest return: there is deliberately no notification channel
here. DELIVERED would be a lie of exactly the kind NotificationOutcome exists to
prevent, and the module's own docstring says only DELIVERED means the OS kept it.
"""

import os
from unittest.mock import patch

import pytest

import src.notifications as N
from src.notifications import NotificationOutcome, send_notification


@pytest.fixture
def _no_pytest_guard(monkeypatch):
    """Defeat the conftest fixture, so these tests exercise the REAL function.

    Without this the autouse fixture replaces send_notification wholesale and
    every assertion below would pass for the wrong reason -- the exact
    agreement region this file exists to close.
    """
    monkeypatch.setattr(N, "_send_macos_pyobjc", lambda *a, **k: NotificationOutcome.DELIVERED)
    monkeypatch.setattr(N, "_send_macos_osascript", lambda *a, **k: NotificationOutcome.UNKNOWN)
    monkeypatch.setattr(N, "_try_load_macos_pyobjc", lambda: True)
    monkeypatch.setattr(N.platform, "system", lambda: "Darwin")


class TestTheEnvKillSwitch:
    def test_set_to_1_suppresses_and_says_so(self, monkeypatch, _no_pytest_guard):
        monkeypatch.setenv("BETTERFLOW_SUPPRESS_NOTIFICATIONS", "1")
        sent = []
        monkeypatch.setattr(N, "_send_macos_pyobjc", lambda *a, **k: sent.append(1))

        outcome = send_notification("probe", "body")

        assert sent == [], "a real notification was posted despite the kill switch"
        assert outcome is NotificationOutcome.UNSUPPORTED

    def test_it_does_not_lie_about_delivery(self, monkeypatch, _no_pytest_guard):
        """DELIVERED would be a lie; only an observed delivery may claim it."""
        monkeypatch.setenv("BETTERFLOW_SUPPRESS_NOTIFICATIONS", "1")
        assert bool(send_notification("probe", "body")) is False

    def test_unset_still_notifies(self, monkeypatch, _no_pytest_guard):
        """Control: the switch must not be on by default, or the product ships
        with notifications silently dead."""
        monkeypatch.delenv("BETTERFLOW_SUPPRESS_NOTIFICATIONS", raising=False)
        sent = []
        monkeypatch.setattr(
            N, "_send_macos_pyobjc",
            lambda *a, **k: (sent.append(1), NotificationOutcome.DELIVERED)[1],
        )

        outcome = send_notification("real", "body")

        assert sent == [1], "the ordinary path stopped notifying"
        assert outcome is NotificationOutcome.DELIVERED

    def test_an_empty_or_zero_value_does_not_suppress(self, monkeypatch, _no_pytest_guard):
        """Guard the OTHER end: a stray empty var must not silently disable
        every notification the product sends."""
        for value in ("", "0", "false"):
            monkeypatch.setenv("BETTERFLOW_SUPPRESS_NOTIFICATIONS", value)
            sent = []
            monkeypatch.setattr(
                N, "_send_macos_pyobjc",
                lambda *a, **k: (sent.append(1), NotificationOutcome.DELIVERED)[1],
            )
            assert sent == [] or sent == [1]
            assert send_notification("real", "body") is NotificationOutcome.DELIVERED, (
                f"value {value!r} suppressed notifications; only a truthy opt-in should"
            )
