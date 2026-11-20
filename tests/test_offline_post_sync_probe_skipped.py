"""A cycle that already failed to CONNECT must not probe reachability again (#318).

Origin: 2026-11-17 08:41, Darwin 25.5.0, agent 1.5.148 — "Sync overran the 150s
deadline — finished at 153.6s in phase 'post_sync'". After a failed sync,
``_set_sync_failure_state`` called ``bf.is_reachable()`` to choose Offline vs
Error. That probe tries two endpoints at REACHABILITY_TIMEOUT (5s) per resolved
address and runs after ``sync()`` has spent the cycle's network budget, so
against a blackholed API it pushed a ~144s cycle past the 150s watchdog.

When this cycle has already seen a connect failure the answer is known: the
API is unreachable, so the tray reads Offline without asking again.

The connect failure is produced by the REAL requests/urllib3 path (transport
stubbed at urllib3's ``create_connection``, as in
test_sync_hang_unreachable_api), and the cycle is the REAL ``_do_sync``; the
assertions are on what the tray was told and whether the probe ran.
"""

import http.server
import socket
import threading
from types import SimpleNamespace

import pytest
import urllib3.util.connection as urllib3_connection

from src.sync.bf_client import BetterFlowClient
from src.sync.http_client import BetterFlowClientError
from src.ui.tray import TrayState
from tests.test_sync_watchdog_outcome_classification import _CoordinatorHarness

# Long enough that the watchdog never fires inside these cycles.
_QUIET_DEADLINE = 30


def _failed_stats(error="Cannot connect to BetterFlow API"):
    return SimpleNamespace(
        success=False,
        events_sent=0,
        events_queued=3,
        events_filtered=0,
        gaps_filled=0,
        errors=[error],
        aw_bucket_fetch_failed=False,
    )


def _blackhole(monkeypatch):
    def _connect(address, timeout=None, *args, **kwargs):
        raise socket.timeout("timed out")

    monkeypatch.setattr(urllib3_connection, "create_connection", _connect)


class _Always503(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        self.send_response(503)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class TestPostSyncProbe(_CoordinatorHarness):
    def setup_method(self):
        super().setup_method()
        self.coord._DO_SYNC_DEADLINE = _QUIET_DEADLINE
        self.bf.is_reachable.return_value = True

    def _tray_states(self):
        return [c.args[0] for c in self.tray.set_state.call_args_list]

    def test_connect_failure_this_cycle_skips_the_probe_and_shows_offline(
        self, monkeypatch
    ):
        _blackhole(monkeypatch)
        client = BetterFlowClient(
            api_url="https://api.example.test/api", token="t", device_id="d"
        )

        def _sync_that_cannot_connect(*_a, **_k):
            with pytest.raises(BetterFlowClientError, match="Cannot connect"):
                client._request("POST", "events/batch", data={"events": []}, retry=False)
            return _failed_stats()

        self.sync_engine.sync.side_effect = _sync_that_cannot_connect
        self.coord._do_sync()

        assert self.bf.is_reachable.call_count == 0, (
            "post_sync probed reachability after this cycle already failed to "
            "connect — an unbudgeted 5-20s on a blackholed network (#318)"
        )
        assert TrayState.QUEUED in self._tray_states(), self._tray_states()
        assert TrayState.ERROR not in self._tray_states(), self._tray_states()

    def test_connect_failure_on_an_earlier_cycle_does_not_skip_this_ones_probe(
        self, monkeypatch
    ):
        """The signal is per CYCLE: a connect failure before this cycle began
        (same thread) must not suppress this cycle's probe."""
        _blackhole(monkeypatch)
        client = BetterFlowClient(
            api_url="https://api.example.test/api", token="t", device_id="d"
        )
        with pytest.raises(BetterFlowClientError):
            client._request("POST", "events/batch", data={"events": []}, retry=False)

        self.sync_engine.sync.return_value = _failed_stats("Upload rejected")
        self.coord._do_sync()

        assert self.bf.is_reachable.call_count == 1
        self.tray.set_state.assert_any_call(TrayState.ERROR, "Upload rejected")

    def test_failure_without_network_failure_still_probes(self):
        """Control: no connect failure this cycle, so the probe still decides.
        Reachable -> a real Error."""
        self.sync_engine.sync.return_value = _failed_stats("Upload rejected")
        self.coord._do_sync()

        assert self.bf.is_reachable.call_count == 1
        self.tray.set_state.assert_any_call(TrayState.ERROR, "Upload rejected")

    def test_server_error_is_not_a_connect_failure_and_still_probes(self):
        """Control: a 503 is transient but the server ANSWERED. It must not be
        read as 'offline' without the probe — that is the case the probe is
        there to decide."""
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Always503)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = BetterFlowClient(
                api_url=f"http://127.0.0.1:{server.server_address[1]}/api",
                token="t",
                device_id="d",
            )

            def _sync_that_gets_503(*_a, **_k):
                with pytest.raises(BetterFlowClientError, match="503"):
                    client._request(
                        "POST", "events/batch", data={"events": []}, retry=False
                    )
                return _failed_stats("Server unavailable 503")

            self.sync_engine.sync.side_effect = _sync_that_gets_503
            self.coord._do_sync()
        finally:
            server.shutdown()
            server.server_close()

        assert self.bf.is_reachable.call_count == 1
        self.tray.set_state.assert_any_call(TrayState.ERROR, "Server unavailable 503")

    def test_sync_raising_after_a_connect_failure_also_skips_the_probe(
        self, monkeypatch
    ):
        """The except-Exception branch picks the tray state the same way."""
        _blackhole(monkeypatch)
        client = BetterFlowClient(
            api_url="https://api.example.test/api", token="t", device_id="d"
        )

        def _sync_that_connects_then_raises(*_a, **_k):
            with pytest.raises(BetterFlowClientError):
                client._request("POST", "events/batch", data={"events": []}, retry=False)
            raise RuntimeError("boom after the failed send")

        self.sync_engine.sync.side_effect = _sync_that_connects_then_raises
        self.coord._do_sync()

        assert self.bf.is_reachable.call_count == 0
        assert TrayState.QUEUED in self._tray_states(), self._tray_states()
