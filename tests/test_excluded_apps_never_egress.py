"""The excluded-app privacy guarantee, asserted on the WIRE.

The guarantee ("password managers, Keychain, System Settings never leave your
device") is written into the Regulament Intern employees sign, so it has to
hold for every event producer, not just the one that happened to be filtered.

These tests enable each in-process source, let it produce a real event for an
excluded app, drive the real send path, and inspect the HTTP request body that
``requests`` would have put on the socket. Asserting on an intermediate dict
would not distinguish "filtered" from "filtered somewhere that a third producer
can bypass".
"""

import gzip
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from src.config import Config
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.bf_client import BetterFlowClient
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.input_source import InputSource
from src.sync.sync_engine import SyncEngine, SyncStats
from src.sync.window_source import WindowSource

EXCLUDED_APP = "1Password"
ALLOWED_APP = "Code"
SECRET_TITLE = "Personal Vault - brad@betterqa.co"


def _now() -> datetime:
    """Anchor every fixture to the CURRENT instant.

    Deliberately not a hardcoded calendar date: a fixed date drifts out of
    every freshness / retention window the engine applies and the test starts
    failing on its own anniversary.
    """
    return datetime.now(timezone.utc)


class _WireRecorder:
    """Stands in for ``requests.Session`` and records what was transmitted."""

    def __init__(self):
        self.bodies: list[dict] = []
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        if "data" in kwargs and isinstance(kwargs["data"], (bytes, bytearray)):
            # Event batches are gzipped; decode exactly as the server would.
            self.bodies.append(json.loads(gzip.decompress(kwargs["data"])))
        elif "json" in kwargs:
            self.bodies.append(kwargs["json"])
        response = Mock()
        response.status_code = 200
        response.headers = {}
        response.content = b"{}"
        response.json.return_value = {
            "data": {"processed": 1, "failed": 0, "accepted_ids": []}
        }
        return response

    def transmitted_text(self) -> str:
        return json.dumps(self.bodies)

    def transmitted_events(self) -> list[dict]:
        out: list[dict] = []
        for body in self.bodies:
            out.extend(body.get("events", []))
        return out


def _client(config: Config) -> tuple[BetterFlowClient, _WireRecorder]:
    recorder = _WireRecorder()
    kwargs = dict(
        api_url="https://app.betterflow.eu/api/agent", token="t", device_id="d"
    )
    try:
        client = BetterFlowClient(
            **kwargs, excluded_apps_provider=lambda: config.privacy.exclude_apps
        )
    except TypeError:
        # The pre-fix client has no such parameter. Degrade instead of erroring
        # so the proof-of-failure handshake (checkout HEAD~1 -- <impl>, run
        # these tests) fails on the ACTUAL guarantee — "excluded app reached the
        # wire" — rather than on a constructor signature, which would prove only
        # that the API is new. Post-fix behaviour is unchanged: an unwired client
        # falls back to the shipped exclusion defaults, which contain 1Password.
        client = BetterFlowClient(**kwargs)
    client._session = recorder
    return client, recorder


def _engine(config: Config, client: BetterFlowClient) -> SyncEngine:
    return SyncEngine(
        aw=Mock(),
        bf=client,
        queue=Mock(),
        config=config,
        activity_analyzer=Mock(spec=ActivityAnalyzer),
        time_tracker=Mock(spec=DailyTimeTracker),
    )


class _ScriptedWindowSource(WindowSource):
    """A WindowSource pre-loaded with samples (probe forced usable)."""

    def __init__(self, samples):
        super().__init__(hostname="host", foreground_getter=lambda: (ALLOWED_APP, "x"))
        with self._lock:
            for sample in samples:
                self._samples.append(sample)


class _ScriptedInputSource(InputSource):
    """An InputSource with counts already accrued and a scripted frontmost app."""

    def __init__(self, app: str):
        super().__init__(hostname="host", frontmost_app_getter=lambda: app)
        self._available = True
        with self._lock:
            self._presses = 42
            self._clicks = 7
            self._scrolls = 3

    def available(self) -> bool:
        return True


def _assert_clean(recorder: _WireRecorder):
    """Nothing about the excluded app may appear anywhere in what was sent."""
    text = recorder.transmitted_text()
    assert EXCLUDED_APP not in text, f"excluded app reached the wire: {text}"
    assert SECRET_TITLE not in text, f"excluded app's title reached the wire: {text}"
    for event in recorder.transmitted_events():
        assert event.get("data", {}).get("app") != EXCLUDED_APP


# ---------------------------------------------------------------------------
# In-process WINDOW source
# ---------------------------------------------------------------------------

def test_inproc_window_source_excluded_app_never_reaches_the_wire():
    cfg = Config()
    cfg.sync.in_process_window = True
    client, recorder = _client(cfg)
    engine = _engine(cfg, client)

    t0 = _now()
    engine.window_source = _ScriptedWindowSource([
        (t0, EXCLUDED_APP, SECRET_TITLE),
        (t0 + timedelta(seconds=30), EXCLUDED_APP, SECRET_TITLE),
    ])
    engine._build_inproc_window(t0)  # seeds the checkpoint
    events, pending = engine._build_inproc_window(t0 + timedelta(seconds=30))

    # The source really did produce an excluded-app event — otherwise this test
    # would pass for the wrong reason (nothing to filter).
    assert events and events[0]["data"]["app"] == EXCLUDED_APP
    assert pending is not None

    engine._send_events(events, SyncStats())
    _assert_clean(recorder)


def test_inproc_window_source_still_sends_non_excluded_apps():
    """The guard must not be a blanket mute — collection is unchanged."""
    cfg = Config()
    cfg.sync.in_process_window = True
    client, recorder = _client(cfg)
    engine = _engine(cfg, client)

    t0 = _now()
    engine.window_source = _ScriptedWindowSource([
        (t0, ALLOWED_APP, "sync_engine.py"),
        (t0 + timedelta(seconds=30), ALLOWED_APP, "sync_engine.py"),
    ])
    engine._build_inproc_window(t0)
    events, _ = engine._build_inproc_window(t0 + timedelta(seconds=30))
    assert events

    engine._send_events(events, SyncStats())
    sent = recorder.transmitted_events()
    assert [e["data"]["app"] for e in sent] == [ALLOWED_APP]


def test_inproc_window_mixed_batch_drops_only_the_excluded_app():
    cfg = Config()
    cfg.sync.in_process_window = True
    client, recorder = _client(cfg)
    engine = _engine(cfg, client)

    t0 = _now()
    engine.window_source = _ScriptedWindowSource([
        (t0, ALLOWED_APP, "sync_engine.py"),
        (t0 + timedelta(seconds=20), EXCLUDED_APP, SECRET_TITLE),
        (t0 + timedelta(seconds=40), ALLOWED_APP, "sync_engine.py"),
        (t0 + timedelta(seconds=60), ALLOWED_APP, "sync_engine.py"),
    ])
    engine._build_inproc_window(t0)
    events, _ = engine._build_inproc_window(t0 + timedelta(seconds=60))
    assert any(e["data"]["app"] == EXCLUDED_APP for e in events)
    assert any(e["data"]["app"] == ALLOWED_APP for e in events)

    engine._send_events(events, SyncStats())
    _assert_clean(recorder)
    assert [e["data"]["app"] for e in recorder.transmitted_events()] == [
        ALLOWED_APP, ALLOWED_APP
    ]


# ---------------------------------------------------------------------------
# In-process INPUT source
# ---------------------------------------------------------------------------

def test_inproc_input_source_excluded_app_never_reaches_the_wire():
    cfg = Config()
    cfg.sync.in_process_input = True
    client, recorder = _client(cfg)
    engine = _engine(cfg, client)

    t0 = _now()
    engine.input_source = _ScriptedInputSource(EXCLUDED_APP)
    assert engine._should_use_inproc_input() is True
    engine._build_inproc_input(t0)  # seeds the checkpoint
    engine.input_source._presses = 42  # re-accrue after the seed drain
    event, pending = engine._build_inproc_input(t0 + timedelta(seconds=60))

    assert event is not None and event["data"]["app"] == EXCLUDED_APP
    assert pending is not None

    engine._send_events([event], SyncStats())
    _assert_clean(recorder)


def test_inproc_input_source_still_sends_non_excluded_apps():
    cfg = Config()
    cfg.sync.in_process_input = True
    client, recorder = _client(cfg)
    engine = _engine(cfg, client)

    t0 = _now()
    engine.input_source = _ScriptedInputSource(ALLOWED_APP)
    engine._build_inproc_input(t0)
    engine.input_source._presses = 42
    event, _ = engine._build_inproc_input(t0 + timedelta(seconds=60))
    assert event is not None

    engine._send_events([event], SyncStats())
    sent = recorder.transmitted_events()
    assert len(sent) == 1
    assert sent[0]["data"]["app"] == ALLOWED_APP
    assert sent[0]["data"]["presses"] == 42


# ---------------------------------------------------------------------------
# The chokepoint itself: every other producer, and everything added later
# ---------------------------------------------------------------------------

def test_offline_queue_drain_cannot_egress_an_excluded_app():
    """A queue row written by a pre-fix build must not egress on drain."""
    cfg = Config()
    client, recorder = _client(cfg)
    legacy_row = {
        "id": "win-inproc_host_1",
        "timestamp": _now().isoformat(),
        "duration": 30.0,
        "bucket_id": "bf-window-inproc_host",
        "bucket_type": "currentwindow",
        "data": {"app": EXCLUDED_APP, "title": SECRET_TITLE},
    }
    result = client.send_events([legacy_row])

    assert recorder.calls == 0, "an excluded-app-only batch must not be transmitted"
    _assert_clean(recorder)
    # Reported as retired, not as a delivery failure: exclusion is permanent,
    # so the caller must drop the queue row instead of retrying it forever.
    assert result.success is True
    assert result.events_synced == 0
    assert result.accepted_ids == ["win-inproc_host_1"]


def test_client_without_a_wired_provider_still_honours_the_defaults():
    """Fail closed: an unwired client uses the shipped exclusion list."""
    recorder = _WireRecorder()
    client = BetterFlowClient(
        api_url="https://app.betterflow.eu/api/agent", token="t", device_id="d"
    )
    client._session = recorder
    client.send_events([{
        "id": "x1",
        "timestamp": _now().isoformat(),
        "duration": 10.0,
        "bucket_id": "b",
        "bucket_type": "currentwindow",
        "data": {"app": EXCLUDED_APP, "title": SECRET_TITLE},
    }])
    assert recorder.calls == 0
    _assert_clean(recorder)


def test_a_provider_that_raises_falls_back_to_the_defaults():
    def boom():
        raise RuntimeError("config unreadable")

    recorder = _WireRecorder()
    client = BetterFlowClient(
        api_url="https://app.betterflow.eu/api/agent", token="t", device_id="d",
        excluded_apps_provider=boom,
    )
    client._session = recorder
    client.send_events([{
        "id": "x1",
        "timestamp": _now().isoformat(),
        "duration": 10.0,
        "bucket_id": "b",
        "bucket_type": "currentwindow",
        "data": {"app": EXCLUDED_APP, "title": SECRET_TITLE},
    }])
    assert recorder.calls == 0
    _assert_clean(recorder)


def test_a_newly_excluded_app_stops_egressing_on_the_very_next_send():
    """The provider is read live, not snapshotted at construction."""
    cfg = Config()
    client, recorder = _client(cfg)
    event = {
        "id": "x1",
        "timestamp": _now().isoformat(),
        "duration": 10.0,
        "bucket_id": "b",
        "bucket_type": "currentwindow",
        "data": {"app": "Bitwarden", "title": "vault"},
    }
    client.send_events([event])
    assert recorder.calls == 1  # not excluded yet

    cfg.privacy.exclude_apps = cfg.privacy.exclude_apps + ["Bitwarden"]
    client.send_events([dict(event, id="x2")])
    assert recorder.calls == 1, "a newly excluded app kept egressing"


# ---------------------------------------------------------------------------
# The server-config surface for the exclusion list
# ---------------------------------------------------------------------------

def test_server_exclude_apps_is_deferred_like_the_rest_of_the_privacy_block():
    import src.config as config_module

    cfg = Config()
    baseline = list(cfg.privacy.exclude_apps)
    assert config_module.DEFER_UNAPPLIED_SERVER_SETTINGS is True
    cfg.update_from_server({"privacy": {"exclude_apps": ["Bitwarden"]}})
    assert cfg.privacy.exclude_apps == baseline


def test_server_exclude_apps_can_only_extend_never_replace(monkeypatch):
    import src.config as config_module

    monkeypatch.setattr(config_module, "DEFER_UNAPPLIED_SERVER_SETTINGS", False)
    cfg = Config()
    baseline = list(cfg.privacy.exclude_apps)
    assert EXCLUDED_APP in baseline

    # A payload that omits 1Password must NOT be able to un-exclude it.
    cfg.update_from_server({"privacy": {"exclude_apps": ["Bitwarden"]}})
    assert EXCLUDED_APP in cfg.privacy.exclude_apps
    assert "Bitwarden" in cfg.privacy.exclude_apps
    assert set(baseline).issubset(set(cfg.privacy.exclude_apps))


@pytest.mark.parametrize("payload", ["Bitwarden", {"a": 1}, 5, None])
def test_server_exclude_apps_ignores_malformed_payloads(monkeypatch, payload):
    import src.config as config_module

    monkeypatch.setattr(config_module, "DEFER_UNAPPLIED_SERVER_SETTINGS", False)
    cfg = Config()
    baseline = list(cfg.privacy.exclude_apps)
    cfg.update_from_server({"privacy": {"exclude_apps": payload}})
    assert cfg.privacy.exclude_apps == baseline
