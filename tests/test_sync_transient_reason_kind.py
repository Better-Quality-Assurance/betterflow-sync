"""A transient upload failure reached ops as a bare count.

Board row 84c8250f (fingerprint sync-repeated-failure, device 55, release
1.5.138, 4 occurrences 2026-09-29..2026-10-08):

    "Sync failing repeatedly (3×): 1 local reason(s) recorded in local dead-letter"

No status and no kind, so the reason was neither "API error (NNN)" nor one of
the four SyncEngine prefixes. The one remaining producer of `stats.errors` on a
failing cycle is `stats.errors.append(result.error)` after a batch upload, and
on a TRANSIENT failure `result.error` is a sentence http_client / bf_client
wrote: "Request timed out", "Cannot connect to BetterFlow API", "Server error:
502", "server returned no delivery confirmation; ...". Those carry no tenant
data, but `local_reason_kinds` did not know them, so ops was told "sync failed
three times, the reason is on the user's laptop" for what is usually a network
problem with a one-word name.

The fixtures below are the bytes the REAL client produces (Phantom 4): each
reason comes from driving BetterFlowClient against a `responses` mock, never
from a string typed into this file.

Proof of failure: against the pre-fix sync_engine.py every "names the kind"
case fails (the summary is a bare count). The redaction controls pass on both.
"""

import re
import socket

import pytest
import requests
import responses

from src.sync.bf_client import BetterFlowClient, BetterFlowClientError
from src.sync.sync_engine import local_reason_kinds, server_status_summary

BASE = "https://api.example.test/api"


def _client() -> BetterFlowClient:
    return BetterFlowClient(api_url=BASE, token="t", device_id="d")


def _reason_from(body=None, status=200, json_body=None, headers=None) -> str:
    """Make one real request and return str() of the error it raises."""
    client = _client()
    with responses.RequestsMock() as rsps:
        kwargs = {"status": status}
        if body is not None:
            kwargs = {"body": body}
        if json_body is not None:
            kwargs["json"] = json_body
        if headers:
            kwargs["headers"] = headers
        rsps.add(responses.POST, f"{BASE}/events/batch", **kwargs)
        with pytest.raises(BetterFlowClientError) as exc:
            client._request("POST", "events/batch", data={"events": []}, retry=False)
    return str(exc.value)


def _dns_reason() -> str:
    err = requests.exceptions.ConnectionError("Max retries exceeded")
    err.__cause__ = socket.gaierror(8, "nodename nor servname provided, or not known")
    return _reason_from(body=err)


PRODUCERS = {
    "timeout": lambda: _reason_from(body=requests.exceptions.ReadTimeout("read timed out")),
    "connect-failed": lambda: _reason_from(body=requests.exceptions.ConnectionError("refused")),
    "dns-failed": _dns_reason,
    "connection-dropped": lambda: _reason_from(body=requests.exceptions.ChunkedEncodingError("x")),
    "server-error": lambda: _reason_from(status=502),
    "server-unavailable": lambda: _reason_from(status=503),
    "rate-limited": lambda: _reason_from(status=429, headers={"Retry-After": "1"}),
}


@pytest.mark.parametrize("kind", sorted(PRODUCERS))
def test_a_transient_client_failure_is_named_by_kind(kind):
    reason = PRODUCERS[kind]()
    out = server_status_summary([reason])
    assert f"[{kind}]" in out, f"{reason!r} -> {out!r}"


def test_no_delivery_confirmation_is_named():
    # bf_client's own transient verdict on a 2xx with no per-event answer.
    client = _client()
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, f"{BASE}/events/batch", json={}, status=200)
        result = client.send_events([{"id": "e1", "timestamp": "2026-10-08T10:00:00Z",
                                      "duration": 1, "data": {"app": "x"},
                                      "bucket_id": "b"}])
    assert result.success is False and result.error, result
    out = server_status_summary([result.error])
    assert "[no-confirmation]" in out, f"{result.error!r} -> {out!r}"


def test_a_429_is_rate_limited_only_not_also_server_unavailable():
    # Both start "Server returned"; one reason is one kind.
    reason = PRODUCERS["rate-limited"]()
    assert local_reason_kinds([reason]) == ["rate-limited"], reason


def test_CONTROL_the_interpolated_text_still_never_leaves_the_device():
    # The DNS reason carries the resolver's own text; only the kind may ship.
    out = server_status_summary([_dns_reason()])
    for leaked in ("nodename", "servname", "gaierror", "Max retries"):
        assert leaked not in out, f"{leaked!r} leaked: {out}"
    assert re.fullmatch(r"; 1 local reason\(s\)( \[[a-z-]+\])? recorded in local dead-letter", out), out


def test_CONTROL_an_unrecognised_reason_is_still_counted_not_named():
    out = server_status_summary(["Bank details 1234-5678 rejected by upstream"])
    assert out == "; 1 local reason(s) recorded in local dead-letter", out


def test_CONTROL_a_definitive_rejection_still_reports_its_status():
    out = server_status_summary([_reason_from(status=422, json_body={"message": "Draft.docx"})])
    assert out == "; server status 422, full reason in local dead-letter", out
