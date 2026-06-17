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


def _make_bucket(bucket_id: str) -> AWBucket:
    return AWBucket(
        id=bucket_id,
        name=bucket_id,
        type="afkstatus",
        client="afk",
        hostname="test",
        created=datetime.now(timezone.utc),
    )


def _make_coordinator(
    input_watcher=None,
    latest_afk_event=None,
    extra_buckets=None,
) -> SyncCoordinator:
    """Build a coordinator with mock deps and inject the input_watcher +
    a fake AWClient that returns canned AFK buckets/events.

    `extra_buckets` lets a test simulate the "user migrated from vanilla
    ActivityWatch" case where both a bf-idle-tracker bucket and a stale
    aw-watcher-afk bucket exist side-by-side.
    """
    tray = MagicMock()
    tray.model = MagicMock()
    tray.model.state = TrayState.SYNCING

    aw = MagicMock()
    if latest_afk_event is None and not extra_buckets:
        aw.get_afk_buckets.return_value = []
        aw.get_events.return_value = []
        aw.get_latest_afk_event.return_value = None
    else:
        primary_bucket = _make_bucket("aw-watcher-afk_bf-idle-tracker_test")
        all_buckets = [primary_bucket]
        per_bucket_events = {primary_bucket.id: [latest_afk_event] if latest_afk_event else []}
        for extra_id, extra_event in (extra_buckets or []):
            extra_bucket = _make_bucket(extra_id)
            all_buckets.append(extra_bucket)
            per_bucket_events[extra_bucket.id] = [extra_event] if extra_event else []
        aw.get_afk_buckets.return_value = all_buckets
        aw.get_events.side_effect = lambda bucket_id, **kwargs: per_bucket_events.get(bucket_id, [])

        bf_buckets = [b for b in all_buckets if "bf-idle-tracker" in b.id]
        candidate_buckets = bf_buckets or all_buckets
        latest = None
        for bucket in candidate_buckets:
            events = per_bucket_events.get(bucket.id, [])
            if not events:
                continue
            event = events[0]
            if latest is None or event.timestamp > latest.timestamp:
                latest = event
        aw.get_latest_afk_event.return_value = latest

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


def _afk_event(status: str, age_seconds: int = 0, duration: float = 60.0) -> AWEvent:
    """Construct an AFK status event. The bucket returns newest-first."""
    return AWEvent(
        id=1,
        timestamp=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        duration=duration,
        data={"status": status},
    )


@patch("src.main.send_notification")
def test_warns_when_input_recent_but_afk_bucket_says_afk(mock_send):
    input_watcher = MagicMock()
    input_watcher.get_last_input_at.return_value = datetime.now(timezone.utc) - timedelta(seconds=30)

    # AFK event clearly stale (1h old, 5min duration → ended 55min ago,
    # well before the 30s-margin guard against return-from-AFK lag).
    coord = _make_coordinator(
        input_watcher=input_watcher,
        latest_afk_event=_afk_event("afk", age_seconds=3600, duration=300),
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
        latest_afk_event=_afk_event("afk", age_seconds=3600, duration=300),
    )

    coord._check_idle_tracker_health()

    mock_send.assert_not_called()


@patch("src.main.send_notification")
def test_silent_when_input_watcher_unset(mock_send):
    """Tests / non-macOS / no Quartz available — coordinator has no
    input_watcher reference. Check is a no-op rather than crashing."""
    coord = _make_coordinator(
        input_watcher=None,
        latest_afk_event=_afk_event("afk", age_seconds=3600, duration=300),
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
        latest_afk_event=_afk_event("afk", age_seconds=3600, duration=300),
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
        latest_afk_event=_afk_event("afk", age_seconds=3600, duration=300),
    )

    coord._check_idle_tracker_health()
    coord._check_idle_tracker_health()
    coord._check_idle_tracker_health()

    assert mock_send.call_count == 1, (
        "Three checks in quick succession must collapse into one notification"
    )


@patch("src.main.send_notification")
def test_silent_during_return_from_afk_transition_lag(mock_send):
    """User was genuinely AFK for 15 min, returns and types at T=0. The AFK
    bucket's last event still says 'afk' for another ~5s until the watcher
    posts the 'not-afk' transition. Pre-fix this fired a spurious warn
    every time someone came back to their machine. Post-fix the 30s margin
    against last_input swallows the transition lag."""
    input_watcher = MagicMock()
    input_watcher.get_last_input_at.return_value = datetime.now(timezone.utc) - timedelta(seconds=2)

    # AFK event 15 min old, 15 min duration → ENDS right now, overlapping
    # the last_input. This is "tracker hasn't caught up yet", not a real bug.
    coord = _make_coordinator(
        input_watcher=input_watcher,
        latest_afk_event=_afk_event("afk", age_seconds=900, duration=900),
    )

    coord._check_idle_tracker_health()

    mock_send.assert_not_called()


@patch("src.main.send_notification")
def test_prefers_bf_idle_tracker_bucket_over_stale_aw_watcher_afk(mock_send):
    """Users who migrated from vanilla ActivityWatch sometimes have both
    `bf-idle-tracker_$host` AND a stale `aw-watcher-afk_$host` bucket. The
    stale one's last event is frozen at 'afk' forever. Pre-fix `buckets[0]`
    was non-deterministic and could pick the stale one — guaranteed false
    positive on every tick. Post-fix the bf-idle-tracker bucket wins."""
    input_watcher = MagicMock()
    input_watcher.get_last_input_at.return_value = datetime.now(timezone.utc) - timedelta(seconds=10)

    coord = _make_coordinator(
        input_watcher=input_watcher,
        # bf-idle-tracker says "not-afk" (correct — user is typing).
        latest_afk_event=_afk_event("not-afk", age_seconds=10, duration=5),
        # Stale ActivityWatch bucket frozen on "afk" forever.
        extra_buckets=[
            ("aw-watcher-afk_stale-host", _afk_event("afk", age_seconds=86400, duration=3600)),
        ],
    )

    coord._check_idle_tracker_health()

    # We MUST pick the bf-idle-tracker bucket and see "not-afk" → no warn.
    mock_send.assert_not_called()


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
