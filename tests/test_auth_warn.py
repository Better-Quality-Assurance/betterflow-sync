"""Tests for the session-lost notification helpers (Emilian, 2026-06-11).

The auth-warn throttle mirrors the input-monitoring perm-warn throttle so
a user who restarts their laptop and silently loses their session gets a
clear notification — at the transition, and again periodically while
WAITING_AUTH persists — instead of working a full day untracked.

The tests construct a SyncCoordinator with mock dependencies and exercise
the throttle directly. We do NOT boot the APScheduler, the AW client, or
the tray loop; only the small set of methods Emilian's incident touched.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.main import SyncCoordinator
from src.ui.tray import TrayState


def _make_coordinator() -> SyncCoordinator:
    """Build a SyncCoordinator with all deps mocked. We only exercise the
    auth-warn helpers and `_check_auth_warn` here; no scheduler is started."""
    tray = MagicMock()
    tray.model = MagicMock()
    tray.model.state = TrayState.WAITING_AUTH

    coord = SyncCoordinator(
        config=MagicMock(),
        aw=MagicMock(),
        bf=MagicMock(),
        queue=MagicMock(),
        sync_engine=MagicMock(),
        tray=tray,
        aw_manager=MagicMock(),
    )
    # Drop the scheduler — we never call .start()/_tick_60s().
    return coord


@patch("src.main.send_notification")
def test_warn_fires_once_per_transition(mock_send):
    coord = _make_coordinator()

    coord._maybe_warn_login_required(source="startup")

    mock_send.assert_called_once()
    args = mock_send.call_args[0]
    assert args[0] == "BetterFlow is not tracking"
    assert "session ended" in args[1].lower()


@patch("src.main.send_notification")
def test_repeated_warn_within_window_is_throttled(mock_send):
    coord = _make_coordinator()

    coord._maybe_warn_login_required(source="startup")
    coord._maybe_warn_login_required(source="periodic")
    coord._maybe_warn_login_required(source="periodic")

    assert mock_send.call_count == 1, (
        "Three warns in quick succession must collapse into one notification"
    )


@patch("src.main.send_notification")
def test_warn_refires_after_rewarn_window(mock_send):
    coord = _make_coordinator()

    coord._maybe_warn_login_required(source="startup")
    # Pretend the throttle clock advanced past the rewarn interval.
    past = datetime.now(timezone.utc) - coord._PERM_REWARN_INTERVAL - timedelta(minutes=1)
    coord._last_auth_warn_at = past

    coord._maybe_warn_login_required(source="periodic")

    assert mock_send.call_count == 2, (
        "After the rewarn window elapses, the next call must re-notify"
    )


@patch("src.main.send_notification")
def test_logged_in_then_lost_refires_immediately(mock_send):
    """The True → False transition is the one Emilian flagged: a user who
    was working fine then lost their session must be notified even if a
    notification fired earlier in the same process."""
    coord = _make_coordinator()

    coord._maybe_warn_login_required(source="startup")
    assert mock_send.call_count == 1

    # User logs in (resets the throttle), then session dies again.
    coord._mark_logged_in_for_warn()
    coord._maybe_warn_login_required(source="session_expired")

    assert mock_send.call_count == 2, (
        "Login then logout in quick succession must NOT be throttled — "
        "the transition matters, not the recency of the previous warn"
    )


@patch("src.main.send_notification")
def test_check_auth_warn_noop_when_logged_in(mock_send):
    coord = _make_coordinator()
    coord.logged_in = True

    coord._check_auth_warn()

    mock_send.assert_not_called()


@patch("src.main.send_notification")
def test_check_auth_warn_noop_when_tray_not_waiting_auth(mock_send):
    """A logged-out state with a different tray state (e.g. ERROR, OFFLINE)
    means something else is in flight — don't pile a login warn on top."""
    coord = _make_coordinator()
    coord.logged_in = False
    coord.tray.model.state = TrayState.ERROR

    coord._check_auth_warn()

    mock_send.assert_not_called()


@patch("src.main.send_notification")
def test_check_auth_warn_fires_when_logged_out_and_waiting(mock_send):
    coord = _make_coordinator()
    coord.logged_in = False
    coord.tray.model.state = TrayState.WAITING_AUTH

    coord._check_auth_warn()

    mock_send.assert_called_once()
