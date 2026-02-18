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

    def test_context_manager(self):
        """Test using client as context manager."""
        with AWClient() as client:
            assert client is not None
