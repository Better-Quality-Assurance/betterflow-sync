"""A cycle that already failed to CONNECT must not probe reachability again (#318).

Origin: 2026-11-17 08:41, Darwin 25.5.0, agent 1.5.148 — "Sync overran the 150s
deadline — finished at 153.6s in phase 'post_sync'". After a failed sync,
``_set_sync_failure_state`` called ``bf.is_reachable()`` to choose Offline vs
Error. That probe tries two endpoints at REACHABILITY_TIMEOUT (5s) per resolved
address and runs after ``sync()`` has spent the cycle's network budget, so
against a blackholed API it pushed a ~144s cycle past the 150s watchdog.

When this cycle's last request got no answer at all (connect failure or
timeout) the answer is known: the API is unreachable, so the tray reads
Offline without asking again, and no further network call is made. Any HTTP
answer — any status, even one whose body is then cut off — leaves the probe
to decide.

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
from src.sync.retry import RetryConfig
from src.sync.sync_engine import SyncStats
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

    def test_no_network_call_after_sync_returns_on_a_blackholed_api(
        self, monkeypatch
    ):
        """End to end, with a REAL client wherever the coordinator talks to the
        API (the probe and the hours fetch), not the harness's mocks. Skipping
        the probe alone left hours_fetch's get_status(): one more 10s connect
        per address after sync() had spent the budget (review of #321)."""
        connects = []
        sync_done = {"v": False}

        def _connect(address, timeout=None, *args, **kwargs):
            connects.append((sync_done["v"], timeout))
            raise socket.timeout("timed out")

        monkeypatch.setattr(urllib3_connection, "create_connection", _connect)
        client = BetterFlowClient(
            api_url="https://api.example.test/api", token="t", device_id="d"
        )
        self.coord.bf = client
        self.coord.hours.bf = client
        del self.coord._fetch_hours_today  # un-stub the harness's hours mock

        def _sync(*_a, **_k):
            with pytest.raises(BetterFlowClientError):
                client._request("POST", "events/batch", data={"events": []}, retry=False)
            sync_done["v"] = True
            return _failed_stats()

        self.sync_engine.sync.side_effect = _sync
        self.coord._do_sync()

        after = [c for c in connects if c[0]]
        assert after == [], f"connects after sync() returned: {after}"
        assert TrayState.QUEUED in self._tray_states(), self._tray_states()
        # The tray still gets the (cached) hours value.
        hours_updates = [
            c.kwargs["hours_today"]
            for c in self.tray.update_stats.call_args_list
            if "hours_today" in c.kwargs
        ]
        assert hours_updates == ["0h 0m"], hours_updates

    def test_recovered_connection_error_then_503_still_probes(self):
        """A stale keep-alive reset on attempt 1, then the server answers 503
        until the chain is exhausted. The server ANSWERED: the probe decides,
        and a reachable server with a failed sync is an Error — not Offline
        because one attempt earlier in the cycle could not connect."""
        seen = {"n": 0}

        class _ResetThen503(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                seen["n"] += 1
                if seen["n"] == 1:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ResetThen503)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = BetterFlowClient(
                api_url=f"http://127.0.0.1:{server.server_address[1]}/api",
                token="t",
                device_id="d",
            )

            def _sync(*_a, **_k):
                with pytest.raises(BetterFlowClientError, match="503"):
                    client._request(
                        "POST",
                        "events/batch",
                        data={"events": []},
                        retry_config_override=RetryConfig(
                            max_retries=2, base_delay=0.01, jitter=False
                        ),
                    )
                return _failed_stats("Server returned 503")

            self.sync_engine.sync.side_effect = _sync
            self.coord._do_sync()
        finally:
            server.shutdown()
            server.server_close()

        assert seen["n"] >= 2, "the reset-then-503 sequence did not happen"
        assert self.bf.is_reachable.call_count == 1
        self.tray.set_state.assert_any_call(TrayState.ERROR, "Server returned 503")

    def test_success_shaped_cycle_on_a_blackholed_api_skips_the_hours_fetch(
        self, monkeypatch
    ):
        """sync() can return success=True on a blackholed API: session start
        fails and is swallowed, sends are budget-skipped and queued without an
        error. The hours fetch must not connect on that branch either (review
        round 2 of #321). Real SyncStats, so the success branch really runs."""
        connects = []
        sync_done = {"v": False}

        def _connect(address, timeout=None, *args, **kwargs):
            connects.append((sync_done["v"], timeout))
            raise socket.timeout("timed out")

        monkeypatch.setattr(urllib3_connection, "create_connection", _connect)
        client = BetterFlowClient(
            api_url="https://api.example.test/api", token="t", device_id="d"
        )
        self.coord.bf = client
        self.coord.hours.bf = client
        del self.coord._fetch_hours_today

        def _sync(*_a, **_k):
            with pytest.raises(BetterFlowClientError):
                client._request("POST", "sessions/start", data={}, retry=False)
            sync_done["v"] = True
            return SyncStats(events_queued=4)  # success=True, errors=[]

        self.sync_engine.sync.side_effect = _sync
        self.coord._do_sync()

        after = [c for c in connects if c[0]]
        assert after == [], f"connects after sync() returned: {after}"
        hours_updates = [
            c.kwargs["hours_today"]
            for c in self.tray.update_stats.call_args_list
            if "hours_today" in c.kwargs
        ]
        assert hours_updates == ["0h 0m"], hours_updates

    def test_server_that_accepts_and_never_answers_gets_no_more_calls(self):
        """A hung server or a captive portal holding the port: every request
        read-times-out, which is not a ConnectionError. The probe and the hours
        fetch would wait 5 + 5 + 10s more on it (review round 2 of #321)."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        accepted = []
        sync_done = {"v": False}
        stop = threading.Event()

        def _accept():
            listener.settimeout(0.1)
            while not stop.is_set():
                try:
                    conn, _ = listener.accept()
                except OSError:
                    continue
                accepted.append((sync_done["v"], conn))

        threading.Thread(target=_accept, daemon=True).start()
        try:
            client = BetterFlowClient(
                api_url=f"http://127.0.0.1:{listener.getsockname()[1]}/api",
                token="t",
                device_id="d",
                timeout=1,
            )
            self.coord.bf = client
            self.coord.hours.bf = client
            del self.coord._fetch_hours_today

            def _sync(*_a, **_k):
                with pytest.raises(BetterFlowClientError, match="timed out"):
                    client._request(
                        "POST", "events/batch", data={"events": []}, retry=False
                    )
                sync_done["v"] = True
                return _failed_stats("Request timed out")

            self.sync_engine.sync.side_effect = _sync
            self.coord._do_sync()
        finally:
            stop.set()
            listener.close()
            for _, conn in accepted:
                conn.close()

        after = [a for a, _ in accepted if a]
        assert len(after) == 0, f"{len(after)} connection(s) after sync() returned"
        assert TrayState.QUEUED in self._tray_states(), self._tray_states()

    def test_recovered_connection_error_then_4xx_still_probes(self):
        """Any HTTP answer clears the mark, not only a 5xx: reset on attempt 1,
        then a definitive 400."""
        seen = {"n": 0}

        class _ResetThen400(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                seen["n"] += 1
                if seen["n"] == 1:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                self.send_response(400)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ResetThen400)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = BetterFlowClient(
                api_url=f"http://127.0.0.1:{server.server_address[1]}/api",
                token="t",
                device_id="d",
            )

            def _sync(*_a, **_k):
                with pytest.raises(BetterFlowClientError, match="400"):
                    client._request(
                        "POST",
                        "events/batch",
                        data={"events": []},
                        retry_config_override=RetryConfig(
                            max_retries=2, base_delay=0.01, jitter=False
                        ),
                    )
                return _failed_stats("Upload rejected")

            self.sync_engine.sync.side_effect = _sync
            self.coord._do_sync()
        finally:
            server.shutdown()
            server.server_close()

        assert seen["n"] >= 2, "the reset-then-400 sequence did not happen"
        assert self.bf.is_reachable.call_count == 1
        self.tray.set_state.assert_any_call(TrayState.ERROR, "Upload rejected")

    def test_sync_raising_with_no_network_failure_still_probes(self):
        """Control for the except branch: sync() raises with a healthy API and
        no request failed, so the probe decides — reachable -> Error."""
        self.sync_engine.sync.side_effect = RuntimeError("bug, not network")
        self.coord._do_sync()

        assert self.bf.is_reachable.call_count == 1
        self.tray.set_state.assert_any_call(TrayState.ERROR, "Sync error")

    def test_connect_failure_on_another_thread_does_not_flip_this_cycle(
        self, monkeypatch
    ):
        """The mark is per THREAD: a tray or heartbeat thread failing to connect
        while this cycle runs must not make this cycle's failure read Offline."""
        _blackhole(monkeypatch)
        client = BetterFlowClient(
            api_url="https://api.example.test/api", token="t", device_id="d"
        )

        def _other_thread_fails():
            with pytest.raises(BetterFlowClientError):
                client._request("GET", "heartbeat", retry=False)

        def _sync(*_a, **_k):
            t = threading.Thread(target=_other_thread_fails)
            t.start()
            t.join()
            return _failed_stats("Upload rejected")

        self.sync_engine.sync.side_effect = _sync
        self.coord._do_sync()

        assert self.bf.is_reachable.call_count == 1
        self.tray.set_state.assert_any_call(TrayState.ERROR, "Upload rejected")

    def test_reset_then_truncated_body_still_probes(self):
        """A status line arrived, then the body was cut off: the server
        ANSWERED, so the probe decides (review round 3 of #321)."""
        seen = {"n": 0}

        class _ResetThenTruncated(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                seen["n"] += 1
                if seen["n"] == 1:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b'{"ok": tr')
                self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ResetThenTruncated)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = BetterFlowClient(
                api_url=f"http://127.0.0.1:{server.server_address[1]}/api",
                token="t",
                device_id="d",
            )

            def _sync(*_a, **_k):
                with pytest.raises(BetterFlowClientError) as ei:
                    client._request(
                        "POST",
                        "events/batch",
                        data={"events": []},
                        retry_config_override=RetryConfig(
                            max_retries=2, base_delay=0.01, jitter=False
                        ),
                    )
                return _failed_stats(str(ei.value))

            self.sync_engine.sync.side_effect = _sync
            self.coord._do_sync()
        finally:
            server.shutdown()
            server.server_close()

        assert seen["n"] >= 2, "the reset-then-truncated sequence did not happen"
        assert self.bf.is_reachable.call_count == 1
        assert TrayState.ERROR in self._tray_states(), self._tray_states()
