"""Cross-check tests for the bf-idle-tracker false-AFK detection
(Lucian, 2026-06-11).

bf-idle-tracker runs in a separate subprocess under its own TCC subject.
If it's missing Input Monitoring (separate grant from the main app),
it silently reports the user as AFK while the in-process input watcher
keeps seeing keystrokes. These tests pin the disagreement detection.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.main import SyncCoordinator
from src.sync.aw_client import AWBucket, AWEvent
from src.ui.tray import TrayState


def _make_coordinator(input_watcher=None, latest_afk_event=None) -> SyncCoordinator:
    """Build a coordinator with mock deps and inject the input_watcher +
    a fake AWClient that returns one canned AFK event."""
    tray = MagicMock()
    tray.model = MagicMock()
    tray.model.state = TrayState.SYNCING

    aw = MagicMock()
    if latest_afk_event is None:
        aw.get_afk_buckets.return_value = []
        aw.get_events.return_value = []
    else:
        aw.get_afk_buckets.return_value = [
            AWBucket(
                id="aw-watcher-afk_test",
                name="aw-watcher-afk",
                type="afkstatus",
                client="aw-watcher-afk",
                hostname="test",
                created=datetime.now(timezone.utc),
            ),
        ]
        aw.get_events.return_value = [latest_afk_event]

    coord = SyncCoordinator(
        config=MagicMock(),
        aw=aw,
        bf=MagicMock(),
        queue=MagicMock(),
        sync_engine=MagicMock(),
        tray=tray,
        aw_manager=MagicMock(),
    )
    coord.logged_in = True
    coord._input_watcher = input_watcher
    return coord


def _afk_event(status: str, age_seconds: int = 0) -> AWEvent:
    """Construct an AFK status event. The bucket returns newest-first."""
    return AWEvent(
        id=1,
        timestamp=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        duration=60.0,
        data={"status": status},
    )


@patch("src.main.send_notification")
def test_warns_when_input_recent_but_afk_bucket_says_afk(mock_send):
    input_watcher = MagicMock()
    input_watcher.get_last_input_at.return_value = datetime.now(timezone.utc) - timedelta(seconds=30)

    coord = _make_coordinator(
        input_watcher=input_watcher,
        latest_afk_event=_afk_event("afk"),
    )

    coord._check_idle_tracker_health()

    mock_send.assert_called_once()
    title, body = mock_send.call_args[0][:2]
    assert title == "BetterFlow may not be detecting your input"
    assert "Input Monitoring" in body


@patch("src.main.send_notification")
def test_silent_when_afk_bucket_says_not_afk(mock_send):
    """The tracker agrees with the input watcher — no warning."""
    input_watcher = MagicMock()
    input_watcher.get_last_input_at.return_value = datetime.now(timezone.utc) - timedelta(seconds=30)

    coord = _make_coordinator(
        input_watcher=input_watcher,
        latest_afk_event=_afk_event("not-afk"),
    )

    coord._check_idle_tracker_health()

    mock_send.assert_not_called()


@patch("src.main.send_notification")
def test_silent_when_input_is_stale(mock_send):
    """Genuine user-AFK — input watcher hasn't seen anything for a while,
    so the tracker reporting 'afk' is correct, no disagreement."""
    input_watcher = MagicMock()
    input_watcher.get_last_input_at.return_value = (
        datetime.now(timezone.utc) - timedelta(seconds=600)
    )

    coord = _make_coordinator(
        input_watcher=input_watcher,
        latest_afk_event=_afk_event("afk"),
    )

    coord._check_idle_tracker_health()

    mock_send.assert_not_called()


@patch("src.main.send_notification")
def test_silent_when_input_watcher_unset(mock_send):
    """Tests / non-macOS / no Quartz available — coordinator has no
    input_watcher reference. Check is a no-op rather than crashing."""
    coord = _make_coordinator(
        input_watcher=None,
        latest_afk_event=_afk_event("afk"),
    )

    coord._check_idle_tracker_health()

    mock_send.assert_not_called()


@patch("src.main.send_notification")
def test_silent_when_input_watcher_has_seen_nothing(mock_send):
    """Fresh watcher start — no observations yet. Can't compare,
    so no warning until at least one input event arrives."""
    input_watcher = MagicMock()
    input_watcher.get_last_input_at.return_value = None

    coord = _make_coordinator(
        input_watcher=input_watcher,
        latest_afk_event=_afk_event("afk"),
    )

    coord._check_idle_tracker_health()

    mock_send.assert_not_called()


@patch("src.main.send_notification")
def test_silent_when_not_logged_in(mock_send):
    """No point warning about tracking if the user isn't even logged in
    — the auth warn is the right notification in that case."""
    input_watcher = MagicMock()
    input_watcher.get_last_input_at.return_value = datetime.now(timezone.utc) - timedelta(seconds=10)

    coord = _make_coordinator(
        input_watcher=input_watcher,
        latest_afk_event=_afk_event("afk"),
    )
    coord.logged_in = False

    coord._check_idle_tracker_health()

    mock_send.assert_not_called()


@patch("src.main.send_notification")
def test_repeated_disagreements_within_window_are_throttled(mock_send):
    input_watcher = MagicMock()
    input_watcher.get_last_input_at.return_value = datetime.now(timezone.utc) - timedelta(seconds=10)

    coord = _make_coordinator(
        input_watcher=input_watcher,
        latest_afk_event=_afk_event("afk"),
    )

    coord._check_idle_tracker_health()
    coord._check_idle_tracker_health()
    coord._check_idle_tracker_health()

    assert mock_send.call_count == 1, (
        "Three checks in quick succession must collapse into one notification"
    )


@patch("src.main.send_notification")
def test_aw_unreachable_falls_through_silently(mock_send):
    """Best-effort diagnostic: any AW client error means we can't compare,
    so we don't warn (and don't crash _tick_60s)."""
    input_watcher = MagicMock()
    input_watcher.get_last_input_at.return_value = datetime.now(timezone.utc) - timedelta(seconds=10)

    coord = _make_coordinator(input_watcher=input_watcher)
    coord.aw.get_afk_buckets.side_effect = ConnectionError("AW down")

    # Must not raise.
    coord._check_idle_tracker_health()

    mock_send.assert_not_called()
