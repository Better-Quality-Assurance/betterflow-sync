"""The acknowledgement has to reach the WIRE, not just a dict.

``BetterFlowClient.heartbeat`` copies only keys listed in
``HEARTBEAT_HEALTH_KEYS``. A field absent from that tuple is dropped silently at
the boundary with no error anywhere — which is how #152 nearly shipped dead. So
the assertions here read the serialised request body, and the membership check
is spelled with ``in`` against the tuple rather than "does the telemetry look
populated".

The last test is the callsite guard: ``_build_health_telemetry`` is the only
production assembler of that dict, so a perfect payload builder that nothing
calls would otherwise pass everything above (Phantom 3).
"""

import inspect
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import responses

from src import privacy_notice as pn
from src.config import Config
from src.main import SyncCoordinator
from src.sync.bf_client import BetterFlowClient


def _make_client():
    return BetterFlowClient(
        api_url="https://betterflow.eu/api/agent",
        token="test-token",
        device_id="test-device",
    )


def _last_body():
    return json.loads(responses.calls[-1].request.body)


def _sample_ack():
    when = datetime.now(timezone.utc) - timedelta(minutes=5)
    return {"version": pn.NOTICE_VERSION, "acknowledged_at": when.isoformat()}


# ── The wire boundary ───────────────────────────────────────────────────

def test_the_key_is_whitelisted_for_the_heartbeat():
    """Membership, not truthiness. Absence here is a silent drop, not an error."""
    assert "disclosure_acknowledgement" in BetterFlowClient.HEARTBEAT_HEALTH_KEYS


@responses.activate
def test_the_acknowledgement_reaches_the_request_body():
    responses.add(
        responses.POST,
        "https://betterflow.eu/api/agent/heartbeat",
        json={"status": "active"},
        status=200,
    )
    ack = _sample_ack()
    client = _make_client()
    try:
        client.heartbeat(health={"disclosure_acknowledgement": ack})
    finally:
        client.close()

    body = _last_body()
    assert body["disclosure_acknowledgement"] == ack
    assert body["disclosure_acknowledgement"]["version"] == pn.NOTICE_VERSION
    assert body["disclosure_acknowledgement"]["acknowledged_at"]


@responses.activate
def test_a_falsy_payload_is_still_forwarded():
    """Adversarial arrangement: the forwarding must be ``in``, not ``if value``.

    An empty dict is the input that separates the two implementations — a
    truthiness filter drops it, and the bug only appears for whatever future
    payload shape happens to be falsy.
    """
    responses.add(
        responses.POST,
        "https://betterflow.eu/api/agent/heartbeat",
        json={"status": "active"},
        status=200,
    )
    client = _make_client()
    try:
        client.heartbeat(health={"disclosure_acknowledgement": {}})
    finally:
        client.close()

    assert "disclosure_acknowledgement" in _last_body()


@responses.activate
def test_a_device_that_never_acknowledged_sends_no_key():
    responses.add(
        responses.POST,
        "https://betterflow.eu/api/agent/heartbeat",
        json={"status": "active"},
        status=200,
    )
    client = _make_client()
    try:
        client.heartbeat(health={"consecutive_sync_failures": 0})
    finally:
        client.close()

    assert "disclosure_acknowledgement" not in _last_body()


# ── The callsite guard ──────────────────────────────────────────────────

def _telemetry_from_real_app(config) -> dict:
    """Drive the REAL _build_health_telemetry against a stub coordinator.

    Constructing a SyncCoordinator needs a tray, an engine and a keychain, none
    of which this path touches — so the production method runs unbound against
    the handful of attributes it actually reads.
    """
    import threading

    stub = MagicMock()
    stub.config = config
    stub._idle_tracker_warn_lock = threading.Lock()
    stub._blind_tracker_window = 0
    stub._consecutive_sync_failures = 0
    stub._last_successful_sync = None
    stub.aw_manager.health_snapshot.return_value = {}
    return SyncCoordinator._build_health_telemetry(stub)


def test_the_heartbeat_assembler_includes_the_acknowledgement(tmp_path):
    config = Config()
    config.device_id = "sync:abc-123"
    pn.record_acknowledgement(
        config, now=datetime.now(timezone.utc) - timedelta(minutes=5)
    )

    telemetry = _telemetry_from_real_app(config)
    assert "disclosure_acknowledgement" in telemetry
    assert telemetry["disclosure_acknowledgement"]["version"] == pn.NOTICE_VERSION
    assert set(telemetry["disclosure_acknowledgement"]) == {"version", "acknowledged_at"}


def test_the_assembler_omits_it_before_any_acknowledgement():
    telemetry = _telemetry_from_real_app(Config())
    assert "disclosure_acknowledgement" not in telemetry


def test_the_assembler_delegates_instead_of_rebuilding_the_payload():
    """One rule, one implementation — the shape must not be re-rolled here.

    Comparing outputs cannot catch a parallel builder that happens to agree
    today; reading the consumer's source can.
    """
    source = inspect.getsource(SyncCoordinator._build_health_telemetry)
    doc = SyncCoordinator._build_health_telemetry.__doc__
    if doc:
        source = source.replace(doc, "")
    assert "acknowledgement_telemetry(" in source, (
        "the heartbeat stopped calling the shared payload builder"
    )
    assert "acknowledged_at" not in source, "payload shape re-derived at the callsite"
    assert "NOTICE_VERSION" not in source, "version read a second time at the callsite"


def test_reporting_never_raises_into_the_heartbeat(monkeypatch):
    """A broken record must not cost the fleet its heartbeat."""
    def boom(_config):
        raise RuntimeError("corrupt acknowledgement record")

    monkeypatch.setattr(
        "src.main.acknowledgement_telemetry", boom, raising=True
    )
    telemetry = _telemetry_from_real_app(Config())
    assert isinstance(telemetry, dict)
    assert "disclosure_acknowledgement" not in telemetry
