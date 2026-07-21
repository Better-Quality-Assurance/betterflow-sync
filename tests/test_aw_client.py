"""Tests for ActivityWatch client."""

import pytest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import responses

from src.sync.aw_client import AWClient, AWEvent, AWBucket, AWClientError


class TestAWEvent:
    """Tests for AWEvent dataclass."""

    def test_from_dict(self):
        """Test creating AWEvent from API response."""
        data = {
            "id": 123,
            "timestamp": "2026-02-18T10:00:00+00:00",
            "duration": 120.5,
            "data": {
                "app": "Visual Studio Code",
                "title": "main.py - myproject",
            },
        }
        event = AWEvent.from_dict(data)

        assert event.id == 123
        assert event.duration == 120.5
        assert event.app == "Visual Studio Code"
        assert event.title == "main.py - myproject"

    def test_from_dict_with_z_timestamp(self):
        """Test parsing timestamp with Z suffix."""
        data = {
            "id": 1,
            "timestamp": "2026-02-18T10:00:00Z",
            "duration": 60,
            "data": {},
        }
        event = AWEvent.from_dict(data)
        assert event.timestamp.tzinfo == timezone.utc

    def test_url_property(self):
        """Test URL property for browser events."""
        data = {
            "id": 1,
            "timestamp": "2026-02-18T10:00:00Z",
            "duration": 60,
            "data": {"url": "https://github.com/BetterQA/betterflow"},
        }
        event = AWEvent.from_dict(data)
        assert event.url == "https://github.com/BetterQA/betterflow"

    def test_status_property(self):
        """Test status property for AFK events."""
        data = {
            "id": 1,
            "timestamp": "2026-02-18T10:00:00Z",
            "duration": 60,
            "data": {"status": "not-afk"},
        }
        event = AWEvent.from_dict(data)
        assert event.status == "not-afk"


class TestAWBucket:
    """Tests for AWBucket dataclass."""

    def test_from_dict(self):
        """Test creating AWBucket from API response."""
        data = {
            "name": "aw-watcher-window",
            "type": "aw-watcher-window",
            "client": "aw-watcher-window",
            "hostname": "macbook",
            "created": "2026-01-01T00:00:00Z",
        }
        bucket = AWBucket.from_dict("aw-watcher-window_macbook", data)

        assert bucket.id == "aw-watcher-window_macbook"
        assert bucket.type == "aw-watcher-window"
        assert bucket.hostname == "macbook"


class TestAWClient:
    """Tests for AWClient."""

    @responses.activate
    def test_is_running_true(self):
        """Test is_running when server is up."""
        responses.add(
            responses.GET,
            "http://localhost:5600/api/0/info",
            json={"hostname": "test", "version": "0.12.0"},
            status=200,
        )

        client = AWClient()
        assert client.is_running() is True

    @responses.activate
    def test_is_running_false(self):
        """Test is_running when server is down."""
        responses.add(
            responses.GET,
            "http://localhost:5600/api/0/info",
            body=Exception("Connection refused"),
        )

        client = AWClient()
        assert client.is_running() is False

    @responses.activate
    def test_get_buckets(self):
        """Test getting all buckets."""
        responses.add(
            responses.GET,
            "http://localhost:5600/api/0/buckets/",
            json={
                "aw-watcher-window_host": {
                    "name": "aw-watcher-window",
                    "type": "aw-watcher-window",
                    "client": "aw-watcher-window",
                    "hostname": "host",
                    "created": "2026-01-01T00:00:00Z",
                },
            },
            status=200,
        )

        client = AWClient()
        buckets = client.get_buckets()

        assert "aw-watcher-window_host" in buckets
        assert buckets["aw-watcher-window_host"].type == "aw-watcher-window"

    @responses.activate
    def test_get_events(self):
        """Test getting events from a bucket."""
        responses.add(
            responses.GET,
            "http://localhost:5600/api/0/buckets/test-bucket/events",
            json=[
                {
                    "id": 1,
                    "timestamp": "2026-02-18T10:00:00Z",
                    "duration": 60,
                    "data": {"app": "Terminal"},
                },
            ],
            status=200,
        )

        client = AWClient()
        events = client.get_events("test-bucket")

        assert len(events) == 1
        assert events[0].app == "Terminal"

    @responses.activate
    def test_get_window_buckets(self):
        """Test filtering window buckets."""
        responses.add(
            responses.GET,
            "http://localhost:5600/api/0/buckets/",
            json={
                "aw-watcher-window_host": {
                    "name": "window",
                    "type": "aw-watcher-window",
                    "client": "aw-watcher-window",
                    "hostname": "host",
                    "created": "2026-01-01T00:00:00Z",
                },
                "aw-watcher-afk_host": {
                    "name": "afk",
                    "type": "aw-watcher-afk",
                    "client": "aw-watcher-afk",
                    "hostname": "host",
                    "created": "2026-01-01T00:00:00Z",
                },
            },
            status=200,
        )

        client = AWClient()
        window_buckets = client.get_window_buckets()

        assert len(window_buckets) == 1
        assert window_buckets[0].type == "aw-watcher-window"

    @responses.activate
    def test_get_latest_afk_event_prefers_betterflow_bucket(self):
        """A stale vanilla AFK bucket must not drive live idle state."""
        responses.add(
            responses.GET,
            "http://localhost:5600/api/0/buckets/",
            json={
                "aw-watcher-afk_stale-host": {
                    "name": "aw-watcher-afk",
                    "type": "aw-watcher-afk",
                    "client": "aw-watcher-afk",
                    "hostname": "host",
                    "created": "2026-01-01T00:00:00Z",
                },
                "aw-watcher-afk_bf-idle-tracker_host": {
                    "name": "bf-idle-tracker",
                    "type": "afkstatus",
                    "client": "bf-idle-tracker",
                    "hostname": "host",
                    "created": "2026-01-01T00:00:00Z",
                },
            },
            status=200,
        )
        responses.add(
            responses.GET,
            "http://localhost:5600/api/0/buckets/aw-watcher-afk_bf-idle-tracker_host/events",
            json=[
                {
                    "id": 2,
                    "timestamp": "2026-06-17T09:59:00Z",
                    "duration": 5,
                    "data": {"status": "not-afk"},
                },
            ],
            status=200,
        )

        client = AWClient()
        event = client.get_latest_afk_event()

        assert event is not None
        assert event.status == "not-afk"

    def test_context_manager(self):
        """Test using client as context manager."""
        with AWClient() as client:
            assert client is not None

    def test_request_resets_session_and_retries_on_connection_error(self):
        """A stale pooled socket (ConnectionError) must trigger a session
        reset + retry — the automatic equivalent of the manual restart that
        always 'fixed' sync stalls (furdui.iancu, 2026-06-17)."""
        import requests

        client = AWClient()
        first_session = client._session

        calls = {"n": 0}

        def flaky_request(self, method, url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.exceptions.ConnectionError("stale socket")
            resp = Mock()
            resp.raise_for_status = Mock()
            resp.content = b'{"version": "0.13.2"}'
            resp.json = Mock(return_value={"version": "0.13.2"})
            return resp

        with patch.object(requests.Session, "request", flaky_request), \
                patch("time.sleep"):
            result = client.get_info()

        assert result == {"version": "0.13.2"}
        assert calls["n"] == 2, "recovers on the first retry after resetting the session"
        assert client._session is not first_session, "session was rebuilt on connection failure"

    def test_request_retries_through_a_brief_stall_then_succeeds(self):
        """Two consecutive failures (e.g. a brief server stall) still recover on
        the third attempt thanks to the backoff retries — no manual restart."""
        import requests

        client = AWClient()
        calls = {"n": 0}

        def flaky_request(self, method, url, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise requests.exceptions.ConnectionError("stall")
            resp = Mock()
            resp.raise_for_status = Mock()
            resp.content = b'{"ok": true}'
            resp.json = Mock(return_value={"ok": True})
            return resp

        with patch.object(requests.Session, "request", flaky_request), \
                patch("time.sleep"):
            result = client.get_info()

        assert result == {"ok": True}
        assert calls["n"] == 3, "uses all backoff retries before succeeding"

    def test_request_gives_up_after_max_attempts(self):
        """Persistent connection failure surfaces as AWClientError after exactly
        _CONNECT_ATTEMPTS tries — bounded, no infinite loop."""
        import requests

        client = AWClient()
        calls = {"n": 0}

        def always_down(self, method, url, **kwargs):
            calls["n"] += 1
            raise requests.exceptions.ConnectionError("down")

        with patch.object(requests.Session, "request", always_down), \
                patch("time.sleep"):
            with pytest.raises(AWClientError):
                client.get_info()

        assert calls["n"] == AWClient._CONNECT_ATTEMPTS


class TestMalformedEventTolerance:
    """A malformed event from the local tracker server must cost that event,
    not the whole bucket fetch. Losing the fetch loses the user's tracked time
    for the cycle."""

    @responses.activate
    def test_get_events_skips_bad_events_and_keeps_good_ones(self):
        responses.add(
            responses.GET,
            "http://localhost:5600/api/0/buckets/test-bucket/events",
            json=[
                {"id": 1, "timestamp": "2026-02-18T10:00:00Z", "duration": 60,
                 "data": {"app": "Terminal"}},
                {"id": 2, "duration": 60, "data": {}},                       # no timestamp
                {"id": 3, "timestamp": "not-a-timestamp", "duration": 60, "data": {}},
                {"id": 4, "timestamp": "2026-02-18T10:02:00Z", "duration": "abc",
                 "data": {}},                                                # bad duration
                {"id": 5, "timestamp": "2026-02-18T10:03:00Z", "duration": -30,
                 "data": {}},                                                # negative
                {"id": 6, "timestamp": "2026-02-18T10:04:00Z", "duration": 30,
                 "data": {"app": "Code"}},
            ],
            status=200,
        )

        events = AWClient().get_events("test-bucket")

        assert [e.id for e in events] == [1, 6], "good events must survive bad neighbours"
        assert [e.app for e in events] == ["Terminal", "Code"]

    @responses.activate
    def test_get_events_survives_an_all_malformed_bucket(self):
        responses.add(
            responses.GET,
            "http://localhost:5600/api/0/buckets/test-bucket/events",
            json=[{"id": 1, "timestamp": None, "duration": 60, "data": {}}],
            status=200,
        )

        assert AWClient().get_events("test-bucket") == []

    def test_from_dict_rejects_negative_duration(self):
        """A negative duration flows into span math and yields backwards spans."""
        with pytest.raises(ValueError, match="negative event duration"):
            AWEvent.from_dict(
                {"id": 1, "timestamp": "2026-02-18T10:00:00Z", "duration": -1, "data": {}}
            )

    @pytest.mark.parametrize("bad", ["nan", "inf", "-inf", float("nan"), float("inf")])
    def test_from_dict_rejects_non_finite_duration(self, bad):
        """NaN compares False to everything, so `< 0` alone lets it through and
        it poisons every sum it reaches."""
        with pytest.raises(ValueError, match="non-finite event duration"):
            AWEvent.from_dict(
                {"id": 1, "timestamp": "2026-02-18T10:00:00Z", "duration": bad, "data": {}}
            )

    def test_from_dict_accepts_zero_and_numeric_string_duration(self):
        zero = AWEvent.from_dict(
            {"id": 1, "timestamp": "2026-02-18T10:00:00Z", "duration": 0, "data": {}}
        )
        assert zero.duration == 0.0
        coerced = AWEvent.from_dict(
            {"id": 2, "timestamp": "2026-02-18T10:00:00Z", "duration": "12.5", "data": {}}
        )
        assert coerced.duration == 12.5
