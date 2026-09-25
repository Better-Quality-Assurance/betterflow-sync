"""An unreachable API must read as slow, and must fit inside the watchdog.

Origin: 2026-09-24 13:27Z, device 14 (macOS 25.5.0, agent 1.5.134). The
BetterFlow API was unreachable from the device for ~5 minutes. One sync cycle
ran 297.2s and paged ``Sync hung — exceeded 150s watchdog deadline`` (ERROR,
fingerprint 63a18e4f) although nothing was hung:

    16:27:39  cycle starts; events/batch attempt 1 ... Request timed out (30s)
    16:29:10  attempt 2 ... Cannot connect (~60s)
    16:30:09  watchdog fires: 0 transient failures counted -> "Sync hung"
    16:30:12  attempt 3 fails (~60s); chain exhausted -> first counted failure
    16:32:13  "Sync failed" after ~120s of silent is_reachable() probing
    16:32:36  cycle ends (297.2s)

Three defects, one test class each:

1. **Connect cost multiplies by address count.** requests hands a scalar
   timeout to the connect of EVERY resolved address. app.betterflow.eu has two
   A records, so a "30s" attempt against a blackholed network costs 60s, and
   the ~94s chain the budget assumed really costs ~153s.
2. **Reachability probes run at the full timeout.** The pre-drain and
   tray-state is_reachable() calls sit outside the in-cycle network budget and
   try two endpoints each — ~120s per probe against an unreachable API.
3. **Failed retry attempts were invisible to the watchdog.** The transient
   counter moved only when an exhausted chain built a BetterFlowClientError,
   so a chain still retrying at the deadline read as zero network failures.

The transport is stubbed at urllib3's ``create_connection`` — the real
requests/urllib3 path runs above it, so the timeouts asserted are the ones the
socket layer actually receives, not arguments forwarded between our functions.
"""

import http.server
import socket
import threading

import pytest
import urllib3.util.connection as urllib3_connection

import src.sync.retry as retry_module
from src.main import SyncCoordinator
from src.sync.bf_client import BetterFlowClient
from src.sync.http_client import (
    BaseApiClient,
    BetterFlowClientError,
    transient_failure_count,
)
from src.sync.retry import RetryConfig
from tests.test_sync_watchdog_outcome_classification import (
    _TEST_DEADLINE,
    _CoordinatorHarness,
)

# app.betterflow.eu resolves to two IPv4 addresses (Cloudflare). A connect
# timeout is paid once PER ADDRESS when every address is blackholed.
_PRODUCTION_ADDRESS_COUNT = 2


class _BlackholedNetwork:
    """Stands in for a network where no connect ever completes.

    Records the connect timeout urllib3 hands the socket layer for each
    attempt, and charges a simulated clock ``timeout x address count`` for it
    — which is what urllib3's own create_connection spends iterating the
    resolved addresses (measured: a 2-address host at timeout=2 took 4.0s).
    """

    def __init__(self, monkeypatch, addresses: int = _PRODUCTION_ADDRESS_COUNT):
        self.addresses = addresses
        self.connect_timeouts: list = []
        self.simulated_seconds = 0.0
        monkeypatch.setattr(urllib3_connection, "create_connection", self._connect)
        # Backoff sleeps between retries go on the same simulated clock.
        monkeypatch.setattr(retry_module.time, "sleep", self._sleep)

    def _connect(self, address, timeout=None, *args, **kwargs):
        self.connect_timeouts.append(timeout)
        self.simulated_seconds += float(timeout) * self.addresses
        raise socket.timeout("timed out")

    def _sleep(self, seconds):
        self.simulated_seconds += seconds


def _client() -> BetterFlowClient:
    return BetterFlowClient(api_url="https://api.example.test/api", token="t", device_id="d")


class TestConnectTimeoutIsBounded:
    def test_connect_phase_gets_a_short_timeout_read_keeps_30s(self, monkeypatch):
        net = _BlackholedNetwork(monkeypatch)
        client = _client()
        seen_read_timeouts = []
        real_request = client._session.request

        def recording_request(method, url, **kwargs):
            seen_read_timeouts.append(kwargs["timeout"])
            return real_request(method, url, **kwargs)

        monkeypatch.setattr(client._session, "request", recording_request)

        with pytest.raises(BetterFlowClientError, match="Cannot connect"):
            client._request("POST", "events/batch", data={"events": []}, retry=False)

        assert net.connect_timeouts, "the request never reached the socket layer"
        assert all(t <= 10 for t in net.connect_timeouts), net.connect_timeouts
        # The read timeout is deliberately unchanged: a slow-but-reachable
        # server still gets its full 30s to answer.
        assert seen_read_timeouts[0][1] == 30, seen_read_timeouts

    def test_short_override_is_not_lengthened_by_the_connect_bound(self, monkeypatch):
        net = _BlackholedNetwork(monkeypatch)
        with pytest.raises(BetterFlowClientError):
            _client()._request("GET", "heartbeat", retry=False, timeout_override=5)
        assert net.connect_timeouts == [5]


class TestReachabilityProbeIsBounded:
    def test_is_reachable_probes_with_a_short_timeout(self, monkeypatch):
        net = _BlackholedNetwork(monkeypatch)

        assert _client().is_reachable() is False

        # Both endpoints were tried (health, then events/status) ...
        assert len(net.connect_timeouts) == 2, net.connect_timeouts
        # ... and neither at the 30s request timeout.
        assert all(t <= 5 for t in net.connect_timeouts), net.connect_timeouts


class TestUnreachableCycleFitsTheWatchdog:
    def test_unreachable_api_cycle_finishes_before_the_deadline(self, monkeypatch):
        """Replays the network calls one cycle makes when the API is down, on
        the simulated clock: the events/batch retry chain, the pre-drain
        is_reachable(), the tray-state is_reachable() after the failed sync,
        and the in-cycle hours fetch (events/status). Pre-fix this was ~440s
        of network wait; the incident cycle ran 297s."""
        net = _BlackholedNetwork(monkeypatch)
        client = _client()

        with pytest.raises(BetterFlowClientError):
            client._request("POST", "events/batch", data={"events": []})
        client.is_reachable()  # SyncEngine.sync, before the queue drain
        client.is_reachable()  # SyncCoordinator._set_sync_failure_state
        with pytest.raises(BetterFlowClientError):
            client.get_status()  # hours_fetch phase

        deadline = SyncCoordinator._DO_SYNC_DEADLINE
        assert net.simulated_seconds < deadline, (
            f"an unreachable-API cycle waits {net.simulated_seconds:.1f}s on the "
            f"network, past the {deadline}s watchdog: it will page 'Sync hung' "
            f"with nothing hung"
        )


class TestExhaustedChainCountsEachAttemptOnce:
    def test_three_failed_attempts_count_exactly_three(self, monkeypatch):
        """N-1 attempts are counted by the on_retry hook and the last by the
        BetterFlowClientError the exhausted chain raises. If retry_with_backoff
        ever called on_retry on the final attempt too, or _request stopped
        building the error, the watchdog's "N transient failures" would be off
        by one."""
        net = _BlackholedNetwork(monkeypatch)
        client = _client()
        attempts = client.retry_config.max_retries + 1
        assert attempts == 3, "the default chain length changed; revisit this test"

        before = transient_failure_count()
        with pytest.raises(BetterFlowClientError, match="Cannot connect"):
            client._request("POST", "events/batch", data={"events": []}, retry=True)

        assert len(net.connect_timeouts) == attempts, net.connect_timeouts
        assert transient_failure_count() - before == attempts


class _FlakyThenOkHandler(http.server.BaseHTTPRequestHandler):
    """503 on the first request, 200 after — a chain that fails, retries, and
    recovers without ever building a BetterFlowClientError."""

    requests_seen = 0

    def do_POST(self):  # noqa: N802
        type(self).requests_seen += 1
        if type(self).requests_seen == 1:
            self.send_response(503)
            self.end_headers()
            return
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestFailedAttemptsReachTheWatchdog(_CoordinatorHarness):
    def test_retried_network_failure_is_reported_as_slow_not_hung(self):
        _FlakyThenOkHandler.requests_seen = 0
        server = http.server.HTTPServer(("127.0.0.1", 0), _FlakyThenOkHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        host, port = server.server_address
        client = BaseApiClient(
            api_url=f"http://{host}:{port}",
            timeout=2,
            retry_config=RetryConfig(max_retries=2, base_delay=0.001, jitter=False),
        )

        def retry_through_a_failure():
            # Runs on the sync thread, inside the cycle, before the deadline —
            # the same position as the incident's in-flight events/batch chain.
            assert client._request("POST", "events/batch", data={}) == {"ok": True}
            assert _FlakyThenOkHandler.requests_seen == 2

        try:
            self._run_overrunning_cycle(before_overrun=retry_through_a_failure)
        finally:
            server.shutdown()

        assert self.recorder.by_fingerprint("sync-watchdog-timeout") == [], (
            "a cycle that saw the API fail and retried was paged as a hang"
        )
        offline = self.recorder.by_fingerprint("sync-watchdog-timeout-offline")
        assert len(offline) == 1, self.recorder.captures
        assert "1 transient failure" in offline[0]["message"]
        assert str(_TEST_DEADLINE) in offline[0]["message"]

    def test_success_with_no_failed_attempt_still_pages_as_hung(self):
        """The allowance's control: a chain that succeeds FIRST time adds
        nothing, so a genuine hang after it still pages as an error."""
        _FlakyThenOkHandler.requests_seen = 1  # skip the 503
        server = http.server.HTTPServer(("127.0.0.1", 0), _FlakyThenOkHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        host, port = server.server_address
        client = BaseApiClient(api_url=f"http://{host}:{port}", timeout=2)
        try:
            self._run_overrunning_cycle(
                before_overrun=lambda: client._request("POST", "events/batch", data={})
            )
        finally:
            server.shutdown()

        assert len(self.recorder.by_fingerprint("sync-watchdog-timeout")) == 1
        assert self.recorder.by_fingerprint("sync-watchdog-timeout-offline") == []
