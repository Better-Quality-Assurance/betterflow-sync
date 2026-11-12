"""A connect failure reached ops, and the laptop's own log, with no cause.

Device 50's ops alert read:

    "Sync failing repeatedly (3×): 1 local reason(s) [connect-failed] recorded
    in local dead-letter"

and betterflow.log on the laptop said only "Cannot connect to BetterFlow API",
because http_client's ``except requests.exceptions.ConnectionError`` branch
raised the bare sentence and dropped the exception it caught. SSLError and
ProxyError are ConnectionError subclasses, so TLS interception, a dead proxy,
ECONNREFUSED and ECONNRESET all read identically.

Two halves, both pinned here:

* LOCAL: the reason (what log_retry prints and the dead-letter keeps) names
  the cause class and its errno / SSL reason code, still starting with
  REASON_CONNECT_FAILED, and never carries the host, port or URL.
* OPS: server_status_summary names a bounded sub-kind (connect-failed:ssl,
  :proxy, :refused, :reset, :unreachable, :other) chosen FROM a fixed table.

The failures are real wherever a real one is cheap (Phantom 4: fixtures are
the bytes the tool returns): a port with nothing listening, a local server that
answers with an RST, a local TLS server with a self-signed certificate, a proxy
address with nothing listening. Only EHOSTUNREACH, which loopback cannot
produce, is built from the real urllib3 / requests exception classes.

Proof of failure: against the pre-fix http_client.py every sub-kind and
local-detail case fails (the reason is the bare sentence, the summary says
[connect-failed]). The CONTROL cases pass on both.
"""

import contextlib
import errno
import re
import shutil
import socket
import ssl
import struct
import subprocess
import threading

import pytest
import requests
import responses
import urllib3.exceptions
from urllib3.exceptions import MaxRetryError, NewConnectionError

from src.sync.bf_client import BetterFlowClient, BetterFlowClientError
from src.sync.http_client import REASON_CONNECT_FAILED
from src.sync.sync_engine import local_reason_kinds, server_status_summary


@pytest.fixture(autouse=True)
def _no_ambient_proxy(monkeypatch):
    # A developer or CI proxy in the environment would turn every case below
    # into a proxy failure and the test would measure the machine.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                 "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _reason_at(api_url: str, *, proxies=None) -> str:
    """Make one real request at api_url and return str() of what it raises."""
    client = BetterFlowClient(api_url=api_url, token="t", device_id="d")
    if proxies:
        client._session.proxies.update(proxies)
    try:
        with pytest.raises(BetterFlowClientError) as exc:
            client._request("POST", "events/batch", data={"events": []}, retry=False)
    finally:
        client.close()
    return str(exc.value)


def _refused_reason() -> tuple[str, int]:
    port = _free_port()
    return _reason_at(f"http://127.0.0.1:{port}/api"), port


def _reset_reason() -> tuple[str, int]:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def rst():
        conn, _ = srv.accept()
        conn.settimeout(5)
        with contextlib.suppress(OSError):
            conn.recv(65536)  # wait for the request, so the RST hits a reader
        # SO_LINGER 0: close() sends RST instead of FIN.
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        conn.close()

    t = threading.Thread(target=rst, daemon=True)
    t.start()
    try:
        return _reason_at(f"http://127.0.0.1:{port}/api"), port
    finally:
        t.join(5)
        srv.close()


def _remote_close_reason() -> tuple[str, int]:
    # The server reads the request and closes with a FIN, never answering: the
    # keep-alive a server or load balancer drops. http.client raises
    # RemoteDisconnected for it, a ConnectionResetError built WITHOUT an errno.
    # On Linux a real RST (_reset_reason) is read this way too, ~1% of the time
    # (21 of 2000 in python:3.11; main run 43171894746 hit it).
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def close_without_answer():
        conn, _ = srv.accept()
        conn.settimeout(5)
        # Read the WHOLE request (headers, then Content-Length bytes of body)
        # before closing: unread bytes would make close() send an RST instead.
        buf = b""
        with contextlib.suppress(OSError):
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            head, _, body = buf.partition(b"\r\n\r\n")
            m = re.search(rb"(?im)^content-length:\s*(\d+)", head)
            want = int(m.group(1)) if m else 0
            while len(body) < want:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                body += chunk
        conn.close()

    t = threading.Thread(target=close_without_answer, daemon=True)
    t.start()
    try:
        return _reason_at(f"http://127.0.0.1:{port}/api"), port
    finally:
        t.join(5)
        srv.close()


def _ssl_reason(tmp_path) -> tuple[str, int]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl CLI not available to mint a self-signed cert")
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
         "-out", str(cert), "-days", "1", "-subj", "/CN=localhost"],
        check=True, capture_output=True, timeout=60,
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def serve():
        conn, _ = srv.accept()
        conn.settimeout(5)
        try:
            ctx.wrap_socket(conn, server_side=True).close()
        except (ssl.SSLError, OSError):
            conn.close()  # the client refusing our cert is the point

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        return _reason_at(f"https://127.0.0.1:{port}/api"), port
    finally:
        t.join(5)
        srv.close()


def _proxy_reason() -> tuple[str, int]:
    port = _free_port()
    proxy = f"http://127.0.0.1:{port}"
    return _reason_at("http://bf.example.test/api", proxies={"http": proxy, "https": proxy}), port


def _unreachable_reason() -> str:
    # Loopback cannot produce EHOSTUNREACH, so build the exact chain requests
    # raises for it (ConnectionError <- MaxRetryError <- NewConnectionError <-
    # OSError(EHOSTUNREACH)) from the real classes, and let BetterFlowClient
    # catch it.
    os_err = OSError(errno.EHOSTUNREACH, "No route to host")
    new_conn = NewConnectionError(None, "Failed to establish a new connection")
    new_conn.__cause__ = os_err
    retry = MaxRetryError(None, "/api/events/batch", reason=new_conn)
    retry.__cause__ = new_conn
    err = requests.exceptions.ConnectionError(retry)
    client = BetterFlowClient(api_url="https://bf.example.test/api", token="t", device_id="d")
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, "https://bf.example.test/api/events/batch", body=err)
        with pytest.raises(BetterFlowClientError) as exc:
            client._request("POST", "events/batch", data={"events": []}, retry=False)
    return str(exc.value)


def _other_reason() -> str:
    client = BetterFlowClient(api_url="https://bf.example.test/api", token="t", device_id="d")
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, "https://bf.example.test/api/events/batch",
                 body=requests.exceptions.ConnectionError("refused"))
        with pytest.raises(BetterFlowClientError) as exc:
            client._request("POST", "events/batch", data={"events": []}, retry=False)
    return str(exc.value)


def _summary(reason: str) -> str:
    return server_status_summary([reason])


# ---- LOCAL detail ------------------------------------------------------------


def test_refused_reason_names_the_errno_locally():
    reason, port = _refused_reason()
    assert reason.startswith(REASON_CONNECT_FAILED), reason
    # errno differs by platform (61 macOS, 111 Linux, 10061 Windows).
    assert re.fullmatch(re.escape(REASON_CONNECT_FAILED) + r" \(refused: errno \d+\)", reason), reason
    assert "127.0.0.1" not in reason and str(port) not in reason, reason


def test_reset_reason_names_the_errno_locally():
    reason, port = _reset_reason()
    # A real RST surfaces as ConnectionResetError(ECONNRESET) almost always,
    # and on Linux occasionally as RemoteDisconnected (the client read EOF):
    # both are real, and neither may print "errno None".
    assert re.fullmatch(
        re.escape(REASON_CONNECT_FAILED) + r" \(reset: (errno \d+|RemoteDisconnected)\)", reason
    ), reason
    assert "127.0.0.1" not in reason and str(port) not in reason, reason


def test_a_remote_close_without_an_answer_names_its_class_not_errno_none():
    reason, port = _remote_close_reason()
    assert reason == f"{REASON_CONNECT_FAILED} (reset: RemoteDisconnected)", reason
    assert "127.0.0.1" not in reason and str(port) not in reason, reason
    assert local_reason_kinds([reason]) == ["connect-failed:reset"], reason


def test_ssl_reason_names_the_openssl_code_locally(tmp_path):
    reason, port = _ssl_reason(tmp_path)
    assert reason == f"{REASON_CONNECT_FAILED} (ssl: CERTIFICATE_VERIFY_FAILED)", reason
    assert "127.0.0.1" not in reason and str(port) not in reason, reason


def test_proxy_reason_names_the_proxy_hop_and_its_errno_locally():
    reason, port = _proxy_reason()
    assert re.fullmatch(re.escape(REASON_CONNECT_FAILED) + r" \(proxy: refused errno \d+\)",
                        reason), reason
    assert "127.0.0.1" not in reason and str(port) not in reason, reason
    assert "bf.example.test" not in reason, reason


def test_unreachable_reason_names_the_errno_locally():
    reason = _unreachable_reason()
    assert reason == f"{REASON_CONNECT_FAILED} (unreachable: errno {errno.EHOSTUNREACH})", reason


def test_an_unrecognised_connection_error_still_names_its_class_locally():
    reason = _other_reason()
    assert reason == f"{REASON_CONNECT_FAILED} (other: ConnectionError)", reason


def test_the_retry_log_line_carries_the_cause(caplog):
    # log_retry prints str(error); that line is what betterflow.log holds.
    port = _free_port()
    client = BetterFlowClient(api_url=f"http://127.0.0.1:{port}/api", token="t", device_id="d")
    from src.sync.retry import RetryConfig
    caplog.set_level("WARNING")
    try:
        with pytest.raises(BetterFlowClientError):
            client._request("POST", "events/batch", data={"events": []},
                            retry_config_override=RetryConfig(max_retries=1, base_delay=0.01,
                                                              max_delay=0.01, jitter=False))
    finally:
        client.close()
    lines = [r.getMessage() for r in caplog.records if "Attempt 1 failed" in r.getMessage()]
    assert lines, [r.getMessage() for r in caplog.records]
    assert "(refused: errno" in lines[0], lines[0]


# ---- OPS sub-kind ------------------------------------------------------------


def test_refused_is_named_connect_failed_refused_on_ops():
    reason, _ = _refused_reason()
    assert _summary(reason) == \
        "; 1 local reason(s) [connect-failed:refused] recorded in local dead-letter", reason


def test_reset_is_named_connect_failed_reset_on_ops():
    reason, _ = _reset_reason()
    assert local_reason_kinds([reason]) == ["connect-failed:reset"], reason


def test_ssl_is_named_connect_failed_ssl_on_ops(tmp_path):
    reason, _ = _ssl_reason(tmp_path)
    assert local_reason_kinds([reason]) == ["connect-failed:ssl"], reason


def test_proxy_is_named_connect_failed_proxy_only_not_also_refused():
    # The proxy chain CONTAINS a ConnectionRefusedError; the failing hop is the
    # proxy, and one reason is one kind.
    reason, _ = _proxy_reason()
    assert local_reason_kinds([reason]) == ["connect-failed:proxy"], reason


def test_unreachable_is_named_connect_failed_unreachable_on_ops():
    assert local_reason_kinds([_unreachable_reason()]) == ["connect-failed:unreachable"]


def test_an_unrecognised_connection_error_is_connect_failed_other_on_ops():
    assert local_reason_kinds([_other_reason()]) == ["connect-failed:other"]


# ---- Proxy inner-cause wording and ssl-vs-socket precedence -------------------
#
# Built from the real exception classes: a proxy whose own certificate fails
# verification cannot be stood up on loopback without a TLS proxy server. The
# chain is the one urllib3's _wrap_proxy_error raises (requests ProxyError <-
# MaxRetryError <- urllib3 ProxyError <- urllib3 SSLError <- ssl.SSLError).


def _openssl_verify_error(context: BaseException = None) -> ssl.SSLError:
    err = ssl.SSLCertVerificationError(1, "certificate verify failed")
    # OpenSSL sets .reason on the instances it raises; a hand-built one has none.
    err.reason = "CERTIFICATE_VERIFY_FAILED"
    if context is not None:
        err.__context__ = context
    return err


def _classify(exc):
    # Imported at call time, not module level: against pre-fix code the name
    # does not exist, and a module-level ImportError would fail the whole file
    # (CONTROLs included) at collection instead of on these assertions.
    from src.sync.http_client import classify_connect_failure
    return classify_connect_failure(exc)


def test_proxy_tls_failure_names_the_ssl_code_and_outranks_a_socket_errno():
    # The chain ALSO holds a refused errno: the ssl code must win inside the
    # proxy branch, as it does outside it.
    ssl_err = _openssl_verify_error(ConnectionRefusedError(errno.ECONNREFUSED, "refused"))
    proxy_err = urllib3.exceptions.ProxyError(
        "Unable to connect to proxy", urllib3.exceptions.SSLError(ssl_err))
    exc = requests.exceptions.ProxyError(MaxRetryError(None, "/api", reason=proxy_err))
    message = _classify(exc)
    assert message == f"{REASON_CONNECT_FAILED} (proxy: ssl CERTIFICATE_VERIFY_FAILED)", message
    assert local_reason_kinds([message]) == ["connect-failed:proxy"]


def test_a_proxy_connect_timeout_is_named_timeout_inside_the_proxy_branch():
    # The timeout check sits inside the proxy branch too: a timed-out proxy hop
    # must read as "proxy: timeout", not as its class name or a socket cause.
    reason = urllib3.exceptions.ConnectTimeoutError(None, "Connection timed out")
    proxy_err = urllib3.exceptions.ProxyError("Unable to connect to proxy", reason)
    exc = requests.exceptions.ProxyError(MaxRetryError(None, "/api", reason=proxy_err))
    message = _classify(exc)
    assert message == f"{REASON_CONNECT_FAILED} (proxy: timeout)", message
    assert local_reason_kinds([message]) == ["connect-failed:proxy"]


def test_proxy_failure_with_no_socket_or_ssl_cause_names_its_class():
    message = _classify(requests.exceptions.ProxyError("Cannot connect to proxy."))
    assert message == f"{REASON_CONNECT_FAILED} (proxy: ProxyError)", message
    assert local_reason_kinds([message]) == ["connect-failed:proxy"]


def test_ssl_outranks_a_socket_errno_in_the_same_chain():
    ssl_err = _openssl_verify_error(ConnectionResetError(errno.ECONNRESET, "reset"))
    exc = requests.exceptions.SSLError(
        MaxRetryError(None, "/api", reason=urllib3.exceptions.SSLError(ssl_err)))
    message = _classify(exc)
    assert message == f"{REASON_CONNECT_FAILED} (ssl: CERTIFICATE_VERIFY_FAILED)", message
    assert local_reason_kinds([message]) == ["connect-failed:ssl"]


# ---- Connect timeout ---------------------------------------------------------
#
# Not a real socket: a loopback listener with a full backlog still completes the
# handshake on macOS (measured: 64 queued connects, then a ReadTimeout, not a
# ConnectTimeout), and on Windows it answers with an RST. So the chain requests'
# adapter raises for a connect timeout (ConnectTimeout <- MaxRetryError <-
# urllib3 ConnectTimeoutError) is built from the real classes and driven
# through the real client.


def _connect_timeout_reason() -> str:
    reason = urllib3.exceptions.ConnectTimeoutError(None, "Connection timed out")
    err = requests.exceptions.ConnectTimeout(MaxRetryError(None, "/api", reason=reason))
    client = BetterFlowClient(api_url="https://bf.example.test/api", token="t", device_id="d")
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, "https://bf.example.test/api/events/batch", body=err)
        with pytest.raises(BetterFlowClientError) as exc:
            client._request("POST", "events/batch", data={"events": []}, retry=False)
    return str(exc.value)


def test_a_connect_timeout_is_named_connect_failed_timeout():
    # ConnectTimeout is a ConnectionError AND a Timeout; `except ConnectionError`
    # catches it first, where it used to read as an anonymous connect failure.
    reason = _connect_timeout_reason()
    assert reason == f"{REASON_CONNECT_FAILED} (timeout: connect)", reason
    assert local_reason_kinds([reason]) == ["connect-failed:timeout"], reason


def test_a_refused_connect_is_not_mistaken_for_a_timeout():
    # urllib3's NewConnectionError SUBCLASSES ConnectTimeoutError: a naive
    # isinstance turns every refused connect into a timeout. Real socket.
    reason, _ = _refused_reason()
    assert local_reason_kinds([reason]) == ["connect-failed:refused"], reason


# ---- DNS: a REAL lookup through the real client -------------------------------
#
# `.invalid` is reserved (RFC 6761): the resolver must answer NXDOMAIN, and no
# request leaves the machine. The earlier version of this test hand-set
# err.__cause__ = gaierror, a shape requests never produces (it raises
# ConnectionError(MaxRetryError) inside an except block, without `from`), so it
# passed on main and on a branch that changed real DNS behaviour.
#
# Main vs this branch, measured with this exact call: main makes 3 attempts and
# says "Cannot connect to BetterFlow API"; a draft of this branch made 1 attempt
# (non-retryable). The attempt-count assertion pins main's behaviour; the label
# assertions pin the new name.

_DNS_PROBE_HOST = "bf-dns-probe.invalid"


def test_a_real_dns_failure_is_retried_like_main_and_named_dns_failed():
    try:
        socket.getaddrinfo(_DNS_PROBE_HOST, 443)
    except socket.gaierror:
        pass
    else:
        pytest.skip("this resolver answers for .invalid; cannot produce a DNS failure")
    from src.sync.retry import RetryConfig

    client = BetterFlowClient(api_url=f"https://{_DNS_PROBE_HOST}/api", token="t", device_id="d")
    original = client._session.request
    attempts = []

    def counting_request(*args, **kwargs):
        attempts.append(1)
        return original(*args, **kwargs)

    client._session.request = counting_request
    try:
        with pytest.raises(BetterFlowClientError) as exc:
            client._request(
                "POST", "events/batch", data={"events": []},
                retry_config_override=RetryConfig(max_retries=2, base_delay=0.01,
                                                  max_delay=0.01, jitter=False),
            )
    finally:
        client.close()
    reason = str(exc.value)
    assert len(attempts) == 3, f"DNS must be retried as on main: {len(attempts)} attempt(s)"
    assert exc.value.is_transient, reason
    assert re.fullmatch(r"DNS resolution failed: errno -?\d+", reason), reason
    assert _DNS_PROBE_HOST not in reason, reason
    assert server_status_summary([reason]) == \
        "; 1 local reason(s) [dns-failed] recorded in local dead-letter", reason


# ---- CONTROLS (pass pre- and post-fix) ---------------------------------------


def test_CONTROL_ops_tokens_never_carry_interpolated_text(tmp_path):
    reasons = [_refused_reason()[0], _reset_reason()[0], _proxy_reason()[0],
               _unreachable_reason(), _other_reason()]
    out = server_status_summary(reasons)
    m = re.fullmatch(r"; 5 local reason\(s\) \[([a-z:,-]+)\] recorded in local dead-letter", out)
    assert m, out
    for token in m.group(1).split(","):
        assert re.fullmatch(r"[a-z-]+(:[a-z]+)?", token), token
    for leaked in ("errno", "127.0.0.1", "bf.example.test", "ConnectionError", "refused errno"):
        assert leaked not in out, f"{leaked!r} reached the ops summary: {out}"
