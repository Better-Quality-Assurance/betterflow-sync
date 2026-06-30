"""Watchdog vs. in-cycle retry budget invariant.

Origin: 2026-06-30 server outage (Redis getaddrinfo / 502 storm on
app.betterflow.eu). Agents whose connections *timed out* (rather than fast-502'd)
fired the "Sync hung — exceeded 120s watchdog deadline" alert, fingerprint
707a9ecc, seen across the darwin/1.5.84 fleet. It looked like a deadlock but was
not: the batch-upload path (``events/batch``) retries on the default config —
``max_retries=3`` (4 attempts) at a 30s request timeout plus exponential backoff
— so a *single* request chain against a hanging server takes

    4 * 30s  +  (1.25 + 2.5 + 5.0)  ≈  128.75s

which EXCEEDS the 120s ``_DO_SYNC_DEADLINE``. The cycle breaks after the first
failed batch (sync_engine ``_process_queue``), so one chain dominates the cycle.
The watchdog therefore fires on a transient server outage, and can trip the 300s
wedge re-arm — even though nothing is wedged.

The fix bounds the in-cycle chain comfortably under the watchdog (fewer in-cycle
retries — the OfflineQueue is the durable cross-cycle retry) AND restores real
margin on the deadline. These tests pin the invariant so it can't silently
regress again.

Pre-fix: ``test_in_cycle_upload_chain_fits_under_watchdog`` FAILS (128.75 ≥ 120).
Post-fix: it PASSES with margin.
"""

import http.server
import inspect
import threading
import time

import pytest

from src.main import SyncCoordinator
from src.sync.bf_client import BetterFlowClient
from src.sync.http_client import BaseApiClient, BetterFlowClientError
from src.sync.retry import RetryConfig, calculate_delay


def _client_default_timeout() -> float:
    """The request timeout the in-cycle BetterFlowClient runs with (no override)."""
    return float(inspect.signature(BetterFlowClient.__init__).parameters["timeout"].default)


def _worst_case_chain_seconds(cfg: RetryConfig, timeout: float) -> float:
    """Upper bound on the wall-clock of ONE retrying request whose every attempt
    consumes the full request timeout (server accepts the connection then hangs).

    Mirrors ``retry_with_backoff``: ``max_retries + 1`` attempts, with a backoff
    sleep before each of the ``max_retries`` retries. Jitter only ever *adds*
    (0–25%), so the worst case is the no-jitter delay scaled by 1.25.
    """
    attempts = cfg.max_retries + 1
    backoff = sum(
        calculate_delay(
            attempt,
            cfg.base_delay,
            cfg.max_delay,
            cfg.exponential_base,
            jitter=False,
        )
        * 1.25
        for attempt in range(cfg.max_retries)
    )
    return attempts * timeout + backoff


def test_in_cycle_upload_chain_fits_under_watchdog():
    """A single batch-upload retry chain must finish well inside the watchdog
    deadline, so a hung/slow server can't masquerade as a wedged sync.

    Asserts the worst-case chain leaves headroom for the rest of the cycle
    (heartbeat etc.) before ``_DO_SYNC_DEADLINE``."""
    cfg = BaseApiClient.DEFAULT_RETRY_CONFIG
    worst = _worst_case_chain_seconds(cfg, _client_default_timeout())
    deadline = SyncCoordinator._DO_SYNC_DEADLINE

    # Require the dominant in-cycle network cost to fit with >=25% headroom for
    # the remainder of the cycle (heartbeat, bookkeeping). Pre-fix worst≈128.75s
    # vs deadline 120s fails this outright.
    assert worst < deadline * 0.75, (
        f"worst-case batch-upload chain {worst:.1f}s does not fit under "
        f"{deadline * 0.75:.1f}s (75% of the {deadline}s watchdog). A slow/hung "
        f"server will false-trip the 'Sync hung' watchdog."
    )


def test_wedge_ceiling_above_watchdog():
    """The wedge re-arm must sit above the watchdog so the watchdog's session
    reset always gets its chance first (and a real wedge still self-recovers)."""
    assert SyncCoordinator._SYNC_WEDGE_CEILING > SyncCoordinator._DO_SYNC_DEADLINE


class _RetryableHandler(http.server.BaseHTTPRequestHandler):
    """Always returns 503 (a retryable transient) and counts attempts."""

    attempts = 0

    def do_POST(self):  # noqa: N802
        type(self).attempts += 1
        self.send_response(503)
        self.end_headers()
        self.wfile.write(b'{"message":"unavailable"}')

    def log_message(self, *args):  # silence test server noise
        pass


def test_real_request_chain_attempt_count_matches_budget_formula():
    """Ground the budget formula in real behaviour: a real request chain against
    a transient-failing server makes exactly ``max_retries + 1`` attempts, so the
    ``attempts`` factor in ``_worst_case_chain_seconds`` is observed, not assumed.

    Uses a scaled-down (near-zero) backoff config so the test is fast; the point
    is the attempt COUNT, which is what the fix changes."""
    _RetryableHandler.attempts = 0
    server = http.server.HTTPServer(("127.0.0.1", 0), _RetryableHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host, port = server.server_address
        fast_cfg = RetryConfig(
            max_retries=BaseApiClient.DEFAULT_RETRY_CONFIG.max_retries,
            base_delay=0.001,
            max_delay=0.005,
            exponential_base=2.0,
            jitter=False,
        )
        client = BaseApiClient(
            api_url=f"http://{host}:{port}",
            timeout=2,
            retry_config=fast_cfg,
        )
        start = time.monotonic()
        with pytest.raises(BetterFlowClientError):
            client._request("POST", "events/batch", data={"events": []})
        elapsed = time.monotonic() - start
    finally:
        server.shutdown()

    expected_attempts = BaseApiClient.DEFAULT_RETRY_CONFIG.max_retries + 1
    assert _RetryableHandler.attempts == expected_attempts, (
        f"expected {expected_attempts} attempts (max_retries+1), got "
        f"{_RetryableHandler.attempts}"
    )
    # The chain must not exceed the formula's bound for this scaled config (503 is
    # instant, so this is essentially the backoff sum) — proves the formula is an
    # upper bound on real wall-clock.
    assert elapsed <= _worst_case_chain_seconds(fast_cfg, timeout=2) + 1.0


def test_queue_drain_skip_keeps_dual_chain_under_watchdog():
    """The regular send AND the queue drain both run inside one watchdog cycle.
    The queue is skipped once the cycle has spent _QUEUE_SKIP_IF_CYCLE_ELAPSED, so
    the worst case is that elapsed budget + ONE more request chain. That must stay
    under the watchdog deadline, or a slow server + non-empty queue false-trips
    "Sync hung" (audit round 2)."""
    from src.sync.sync_engine import SyncEngine

    cfg = BaseApiClient.DEFAULT_RETRY_CONFIG
    worst_chain = _worst_case_chain_seconds(cfg, _client_default_timeout())
    deadline = SyncCoordinator._DO_SYNC_DEADLINE
    budget = SyncEngine._QUEUE_SKIP_IF_CYCLE_ELAPSED

    assert budget + worst_chain < deadline, (
        f"queue-skip budget {budget}s + one chain {worst_chain:.1f}s = "
        f"{budget + worst_chain:.1f}s must stay under the {deadline}s watchdog"
    )
