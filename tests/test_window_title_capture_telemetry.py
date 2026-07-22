"""Window-title capture telemetry — `window_titles_captured_recently`.

Design: docs/superpowers/specs/2026-07-22-window-title-capture-telemetry-design.md

Nothing tells us when an agent stops capturing window TITLES. Tracked time keeps
flowing and app attribution keeps working, so every existing health field stays
green while the title detail silently goes missing. On 2026-07-22 a manual sweep
found 14 of the 18 measurable macOS devices running without Accessibility
permission — `AXIsProcessTrusted()` false, every title empty, no signal anywhere.

The field reports the SYMPTOM, not the per-platform cause:

- ``True``  — at least one window event in the last 15 min had a non-empty title.
- ``False`` — window events exist in that period but every title is empty.
               (macOS Accessibility missing / Windows tracker blind / Linux
               watcher dead — all the same user-visible fault.)
- ``None``  — no window events at all. Deliberately DISTINCT from ``False``:
               "not tracking" is a different fault from "tracking without
               titles", and ``window_event_age_seconds`` already covers it.

These assert on the built heartbeat payload, and drive the real HTTP/JSON parse
path (only the *transport* is faked), so they cannot pass by inspecting an
argument handed between two functions.
"""

import json
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from src.aw_manager import AWManager


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _event(app, title, *, age_seconds=30.0, duration=5.0):
    """An AW window event whose END is ``age_seconds`` ago."""
    end = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    start = end - timedelta(seconds=duration)
    return {
        "id": 1,
        "timestamp": start.isoformat(),
        "duration": duration,
        "data": {"app": app, "title": title},
    }


def _manager_serving(monkeypatch, events):
    """AWManager whose window bucket serves ``events`` over the real fetch path.

    The AFK/window *age* helpers are stubbed out the way the existing
    health_snapshot tests do it — this file is about the title field only.
    """
    mgr = AWManager(aw_port=5600)
    monkeypatch.setattr(mgr, "_get_latest_afk_event_age", lambda: 5.0)
    monkeypatch.setattr(mgr, "_get_latest_window_event_age", lambda: 5.0)

    seen_urls = []

    def fake_urlopen(req, *args, **kwargs):
        seen_urls.append(getattr(req, "full_url", req))
        return _FakeResponse(events)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return mgr, seen_urls


def test_true_when_a_window_event_carries_a_title(monkeypatch):
    mgr, _ = _manager_serving(monkeypatch, [_event("Slack", "#general — BetterQA")])

    assert mgr.health_snapshot()["window_titles_captured_recently"] is True


def test_false_when_events_exist_but_every_title_is_empty(monkeypatch):
    """The finding. App names are correct, only the TITLE is gone.

    This is exactly the shape a macOS device without Accessibility permission
    emits: `MacOSWindowWatcher._get_active_window` gets the app name from
    NSWorkspace (no permission needed) and leaves `title` as "" because the
    AXFocusedWindow/AXTitle round-trip fails. Same shape as a blind
    bf-window-tracker on Windows and a dead X11 watcher on Linux.
    """
    mgr, _ = _manager_serving(
        monkeypatch,
        [
            _event("Slack", "", age_seconds=30.0),
            _event("Google Chrome", "", age_seconds=120.0),
            _event("Terminal", "   ", age_seconds=200.0),  # whitespace is empty
        ],
    )

    snap = mgr.health_snapshot()

    assert snap["window_titles_captured_recently"] is False
    # Must not be collapsed into the "not tracking at all" signal.
    assert snap["window_titles_captured_recently"] is not None


def test_none_when_there_are_no_window_events(monkeypatch):
    mgr, _ = _manager_serving(monkeypatch, [])

    assert mgr.health_snapshot()["window_titles_captured_recently"] is None


def test_events_older_than_the_lookback_do_not_count(monkeypatch):
    """A title captured yesterday says nothing about capture right now."""
    mgr, _ = _manager_serving(
        monkeypatch, [_event("Slack", "#general", age_seconds=3600.0)]
    )

    assert mgr.health_snapshot()["window_titles_captured_recently"] is None


def test_recent_title_wins_over_older_empty_ones(monkeypatch):
    mgr, _ = _manager_serving(
        monkeypatch,
        [
            _event("Slack", "", age_seconds=600.0),
            _event("Slack", "#general", age_seconds=60.0),
        ],
    )

    assert mgr.health_snapshot()["window_titles_captured_recently"] is True


def test_probe_failure_reports_none_not_false(monkeypatch):
    """An unreachable tracker server must not be reported as "titles broken"."""
    mgr = AWManager(aw_port=5600)
    monkeypatch.setattr(mgr, "_get_latest_afk_event_age", lambda: 5.0)
    monkeypatch.setattr(mgr, "_get_latest_window_event_age", lambda: None)

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)

    assert mgr.health_snapshot()["window_titles_captured_recently"] is None


def test_probe_asks_the_window_bucket_for_a_bounded_recent_window(monkeypatch):
    mgr, seen_urls = _manager_serving(monkeypatch, [_event("Slack", "x")])

    mgr.health_snapshot()

    assert any("aw-watcher-window" in url for url in seen_urls), seen_urls
    assert any("limit=" in url for url in seen_urls), seen_urls


def test_field_reaches_the_heartbeat_wire():
    """Envelope round-trip.

    See memory/heartbeat-response-envelope-bug: a whole generation of heartbeat
    features silently no-op'd because the field never survived the payload
    boundary while the tests asserted on the wrong shape. bf_client forwards
    only whitelisted keys, so a new health key is dropped unless it is added
    there — assert on the request BODY.
    """
    responses = pytest.importorskip("responses")

    from src.sync.bf_client import BetterFlowClient

    @responses.activate
    def _run():
        responses.add(
            responses.POST,
            "https://betterflow.eu/api/agent/heartbeat",
            json={"status": "active", "commands": []},
            status=200,
        )
        client = BetterFlowClient(
            api_url="https://betterflow.eu/api/agent",
            token="test-token",
            device_id="test-device",
        )
        try:
            client.heartbeat(health={"window_titles_captured_recently": False})
        finally:
            client.close()
        return json.loads(responses.calls[-1].request.body)

    body = _run()
    assert body["window_titles_captured_recently"] is False


def test_field_reaches_the_heartbeat_wire_as_null():
    """``None`` must survive too — it is a distinct state, not "absent"."""
    responses = pytest.importorskip("responses")

    from src.sync.bf_client import BetterFlowClient

    @responses.activate
    def _run():
        responses.add(
            responses.POST,
            "https://betterflow.eu/api/agent/heartbeat",
            json={"status": "active", "commands": []},
            status=200,
        )
        client = BetterFlowClient(
            api_url="https://betterflow.eu/api/agent",
            token="test-token",
            device_id="test-device",
        )
        try:
            client.heartbeat(health={"window_titles_captured_recently": None})
        finally:
            client.close()
        return json.loads(responses.calls[-1].request.body)

    body = _run()
    assert "window_titles_captured_recently" in body
    assert body["window_titles_captured_recently"] is None


# --- macOS: the exact fault the sweep found -------------------------------


def test_macos_accessibility_denied_produces_empty_title_with_app_name(monkeypatch):
    """The realism check behind the ``False`` fixture above.

    With Accessibility denied, `AXIsProcessTrusted()` is false and every
    `AXUIElementCopyAttributeValue` call fails — but NSWorkspace still names the
    frontmost app. So the watcher emits {"app": <real>, "title": ""}, which is
    what makes ``False`` (not ``None``) the right answer for that device.
    """
    pytest.importorskip("AppKit", reason="PyObjC not installed — macOS-only")
    import platform as _platform

    if _platform.system() != "Darwin":
        pytest.skip("macOS-only watcher")

    import ApplicationServices

    from src.sync.macos_window_watcher import MacOSWindowWatcher

    monkeypatch.setattr(
        ApplicationServices, "AXIsProcessTrusted", lambda: False, raising=False
    )
    # Denied AX => every attribute read errors out.
    monkeypatch.setattr(
        ApplicationServices,
        "AXUIElementCopyAttributeValue",
        lambda *a, **k: (-25211, None),
        raising=False,
    )

    from unittest.mock import MagicMock

    watcher = MacOSWindowWatcher(MagicMock(), poll_interval=0.05)
    window = watcher._get_active_window()

    if window is None:
        pytest.skip("no frontmost application in this environment")

    assert window["title"] == "", window
    assert window["app"], "app name must still be captured without Accessibility"


def test_macos_accessibility_denied_shape_reports_false(monkeypatch):
    """End-to-end: AX-denied event shape -> payload says ``False``, not ``None``."""
    mgr, _ = _manager_serving(monkeypatch, [_event("Slack", "")])

    assert mgr.health_snapshot()["window_titles_captured_recently"] is False
