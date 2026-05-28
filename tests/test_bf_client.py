"""Tests for BetterFlow API client."""

import gzip
import json
from unittest.mock import patch

import pytest
import requests.exceptions
import responses

from src.sync.bf_client import (
    BetterFlowAuthError,
    BetterFlowClient,
    BetterFlowClientError,
    DeviceInfo,
)


class TestDeviceInfo:
    """Tests for DeviceInfo dataclass."""

    @patch(
        "src.sync.bf_client.get_machine_uuid", return_value="aaaabbbb-1111-2222-3333-444455556666"
    )
    def test_collect(self, _mock_uuid):
        """Test collecting device information including machine_id."""
        info = DeviceInfo.collect(agent_version="1.2.3")

        assert info.agent_version == "1.2.3"
        assert info.hostname is not None
        assert info.os_name is not None
        assert info.os_version is not None
        assert info.machine_id == "aaaabbbb-1111-2222-3333-444455556666"

    def test_to_dict(self):
        """Test converting to dictionary."""
        from dataclasses import asdict

        info = DeviceInfo(
            hostname="test-host",
            os_name="Darwin",
            os_version="23.0.0",
            agent_version="1.0.0",
            machine_id="test-uuid",
        )
        result = asdict(info)

        assert result["hostname"] == "test-host"
        assert result["os_name"] == "Darwin"
        assert result["os_version"] == "23.0.0"
        assert result["agent_version"] == "1.0.0"
        assert result["machine_id"] == "test-uuid"

    @patch(
        "src.sync.bf_client.get_machine_uuid", return_value="aaaabbbb-1111-2222-3333-444455556666"
    )
    def test_collect_uses_persistent_uuid(self, _mock_uuid):
        """Test collect() resolves machine_id from the persistent UUID."""
        info = DeviceInfo.collect()
        assert info.machine_id == "aaaabbbb-1111-2222-3333-444455556666"

    @patch(
        "src.sync.bf_client.get_machine_uuid", return_value="aaaabbbb-1111-2222-3333-444455556666"
    )
    def test_machine_id_stable_across_hostname_changes(self, _mock_uuid):
        """Test machine_id stays the same when hostname changes."""
        with patch("src.sync.bf_client.platform.node", return_value="MacBookPro"):
            info_a = DeviceInfo.collect()
        with patch("src.sync.bf_client.platform.node", return_value="Brads-MBP.local"):
            info_b = DeviceInfo.collect()
        assert info_a.hostname != info_b.hostname
        assert info_a.machine_id == info_b.machine_id

    @pytest.mark.parametrize(
        "os_name,expected_key",
        [
            ("Darwin", "darwin"),
            ("Windows", "win32"),
            ("Linux", "linux"),
            ("FreeBSD", "linux"),  # Unknown OS falls back to linux
        ],
    )
    def test_platform_key_mapping(self, os_name, expected_key):
        """Test platform_key maps OS names correctly."""
        info = DeviceInfo(
            hostname="test",
            os_name=os_name,
            os_version="1.0",
            agent_version="1.0.0",
            machine_id="test-uuid",
        )
        assert info.platform_key == expected_key


class TestBetterFlowClient:
    """Tests for BetterFlowClient."""

    def setup_method(self):
        """Set up test fixtures."""
        self.client = BetterFlowClient(
            api_url="https://betterflow.eu/api/agent",
            token="test-token",
            device_id="test-device",
        )

    def teardown_method(self):
        """Clean up."""
        self.client.close()

    def test_init_defaults(self):
        """Test default initialization."""
        client = BetterFlowClient()
        assert client.api_url == "https://app.betterflow.eu/api/agent"
        assert client.token is None
        assert client.device_id is None
        assert client.compress is True
        assert client.timeout == 30
        client.close()

    def test_get_headers_with_token(self):
        """Test headers include authorization when token is set."""
        headers = self.client._get_headers()

        assert headers["Authorization"] == "Bearer test-token"
        assert headers["X-Device-ID"] == "test-device"
        assert headers["Accept"] == "application/json"

    def test_get_headers_without_token(self):
        """Test headers without token."""
        client = BetterFlowClient()
        headers = client._get_headers()

        assert "Authorization" not in headers
        assert "X-Device-ID" not in headers
        client.close()

    def test_web_base_url(self):
        """Test deriving web base URL from API URL."""
        assert self.client.web_base_url == "https://betterflow.eu"

    def test_web_base_url_staging(self):
        """Test web base URL for staging."""
        client = BetterFlowClient(api_url="https://staging.betterflow.eu/api/agent")
        assert client.web_base_url == "https://staging.betterflow.eu"
        client.close()

    def test_web_base_url_override(self):
        """Test explicit web base URL override."""
        client = BetterFlowClient(
            api_url="http://127.0.0.1:8001/api/agent",
            web_base_url="https://app.betterflow.eu",
        )
        assert client.web_base_url == "https://app.betterflow.eu"
        client.close()

    def test_web_base_url_from_env(self):
        """Test web base URL override via environment."""
        with patch.dict("os.environ", {"BETTERFLOW_WEB_BASE_URL": "https://app.betterflow.eu"}):
            client = BetterFlowClient(api_url="http://127.0.0.1:8001/api/agent")
            assert client.web_base_url == "https://app.betterflow.eu"
            client.close()

    @responses.activate
    def test_is_reachable_true(self):
        """Test is_reachable when server responds."""
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/health",
            json={"status": "ok"},
            status=200,
        )

        assert self.client.is_reachable() is True

    @responses.activate
    def test_is_reachable_fallback_to_status(self):
        """Test is_reachable falls back to status endpoint."""
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/health",
            body=requests.exceptions.ConnectionError("Not found"),
        )
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/events/status",
            json={"events_today": 0},
            status=200,
        )

        assert self.client.is_reachable() is True

    @responses.activate
    def test_is_reachable_false(self):
        """Test is_reachable when server is down."""
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/health",
            body=requests.exceptions.ConnectionError("Connection refused"),
        )
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/events/status",
            body=requests.exceptions.ConnectionError("Connection refused"),
        )

        assert self.client.is_reachable() is False

    @responses.activate
    def test_request_auth_error_401(self):
        """Test 401 response raises BetterFlowAuthError."""
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/test",
            json={"error": "Unauthorized"},
            status=401,
        )

        with pytest.raises(BetterFlowAuthError, match="Invalid or expired API token"):
            self.client._request("GET", "test")

    @responses.activate
    def test_request_auth_error_403(self):
        """Test 403 response raises BetterFlowAuthError."""
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/test",
            json={"error": "Forbidden"},
            status=403,
        )

        with pytest.raises(BetterFlowAuthError, match="Device not authorized"):
            self.client._request("GET", "test")

    @responses.activate
    def test_request_connection_error(self):
        """Test connection error raises BetterFlowClientError."""
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/test",
            body=requests.exceptions.ConnectionError("Connection refused"),
        )

        with pytest.raises(BetterFlowClientError, match="Cannot connect"):
            self.client._request("GET", "test")

    @responses.activate
    def test_request_timeout_error(self):
        """Test timeout raises BetterFlowClientError."""
        from requests.exceptions import Timeout

        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/test",
            body=Timeout("Request timed out"),
        )

        with pytest.raises(BetterFlowClientError, match="timed out"):
            self.client._request("GET", "test")

    @responses.activate
    def test_request_http_error(self):
        """Test HTTP error includes status code and message."""
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/test",
            json={"message": "Unprocessable Entity"},
            status=422,
        )

        with pytest.raises(BetterFlowClientError, match="422"):
            self.client._request("GET", "test", retry=False)

    @responses.activate
    def test_send_events_success(self):
        """Test successful event sync."""
        responses.add(
            responses.POST,
            "https://betterflow.eu/api/agent/events/batch",
            json={"synced": 5, "queued": 0},
            status=200,
        )

        events = [
            {"timestamp": "2026-02-18T10:00:00Z", "duration": 60, "data": {}} for _ in range(5)
        ]
        result = self.client.send_events(events)

        assert result.success is True
        assert result.events_synced == 5
        assert result.events_queued == 0

    @responses.activate
    def test_send_events_empty_list(self):
        """Test sending empty event list."""
        result = self.client.send_events([])

        assert result.success is True
        assert result.events_synced == 0

    @responses.activate
    def test_send_events_with_compression(self):
        """Test events are gzip compressed."""

        def check_gzip(request):
            assert request.headers.get("Content-Encoding") == "gzip"
            # Decompress and verify
            decompressed = gzip.decompress(request.body)
            data = json.loads(decompressed)
            assert "events" in data
            return (200, {}, json.dumps({"synced": 1}))

        responses.add_callback(
            responses.POST,
            "https://betterflow.eu/api/agent/events/batch",
            callback=check_gzip,
        )

        events = [{"timestamp": "2026-02-18T10:00:00Z", "duration": 60, "data": {}}]
        result = self.client.send_events(events)

        assert result.success is True

    @responses.activate
    def test_send_events_without_compression(self):
        """Test events sent without compression when disabled."""
        client = BetterFlowClient(
            api_url="https://betterflow.eu/api/agent",
            token="test-token",
            compress=False,
        )

        responses.add(
            responses.POST,
            "https://betterflow.eu/api/agent/events/batch",
            json={"synced": 1},
            status=200,
        )

        events = [{"timestamp": "2026-02-18T10:00:00Z", "duration": 60, "data": {}}]
        result = client.send_events(events)

        assert result.success is True
        # Check that Content-Encoding was not set to gzip
        assert responses.calls[0].request.headers.get("Content-Encoding") != "gzip"
        client.close()

    @responses.activate
    def test_send_events_auth_error(self):
        """Test send_events raises BetterFlowAuthError on 401."""
        responses.add(
            responses.POST,
            "https://betterflow.eu/api/agent/events/batch",
            json={"error": "Unauthorized"},
            status=401,
        )

        events = [{"timestamp": "2026-02-18T10:00:00Z", "duration": 60, "data": {}}]
        with pytest.raises(BetterFlowAuthError):
            self.client.send_events(events)

    @responses.activate
    def test_send_events_network_error(self):
        """Test send_events handles network errors."""
        responses.add(
            responses.POST,
            "https://betterflow.eu/api/agent/events/batch",
            body=requests.exceptions.ConnectionError("Connection refused"),
        )

        events = [{"timestamp": "2026-02-18T10:00:00Z", "duration": 60, "data": {}}]
        result = self.client.send_events(events)

        assert result.success is False
        assert result.error is not None

    @responses.activate
    def test_exchange_code_success(self):
        """Test successful code exchange."""
        responses.add(
            responses.POST,
            "https://betterflow.eu/api/v1/sync/auth/token",
            json={
                "access_token": "new-token-123",
                "user": {"email": "user@example.com", "name": "Test User"},
            },
            status=200,
        )

        result = self.client.exchange_code(
            code="auth-code-123",
            device_name="test-device",
            code_verifier="pkce-verifier",
        )

        assert result.success is True
        assert result.api_token == "new-token-123"
        assert result.user_email == "user@example.com"
        assert result.user_name == "Test User"

    @responses.activate
    def test_exchange_code_with_pkce_verifier(self):
        """Test code exchange includes PKCE verifier."""

        def check_verifier(request):
            data = json.loads(request.body)
            assert data["code_verifier"] == "pkce-verifier-123"
            return (200, {}, json.dumps({"access_token": "token"}))

        responses.add_callback(
            responses.POST,
            "https://betterflow.eu/api/v1/sync/auth/token",
            callback=check_verifier,
        )

        self.client.exchange_code(
            code="auth-code",
            device_name="device",
            code_verifier="pkce-verifier-123",
        )

    @responses.activate
    @patch(
        "src.sync.bf_client.get_machine_uuid", return_value="aabbccdd-1111-4222-8333-444455556666"
    )
    def test_exchange_code_sends_hostname_and_machine_uuid(self, _mock_uuid):
        """Test exchange_code payload includes hostname and UUID-based machine_id."""

        def check_payload(request):
            data = json.loads(request.body)
            assert "hostname" in data, "hostname missing from payload"
            assert data["machine_id"] == "aabbccdd-1111-4222-8333-444455556666"
            return (200, {}, json.dumps({"access_token": "tok", "user": {}}))

        responses.add_callback(
            responses.POST,
            "https://betterflow.eu/api/v1/sync/auth/token",
            callback=check_payload,
        )

        self.client.exchange_code(code="code", device_name="dev", code_verifier="verifier")

    @responses.activate
    def test_exchange_code_invalid_code(self):
        """Test exchange with invalid code."""
        responses.add(
            responses.POST,
            "https://betterflow.eu/api/v1/sync/auth/token",
            json={"message": "Invalid or expired code"},
            status=401,
        )

        result = self.client.exchange_code(
            code="bad-code", device_name="device", code_verifier="verifier"
        )

        assert result.success is False
        assert "Invalid" in result.error

    @responses.activate
    def test_exchange_code_connection_error(self):
        """Test exchange handles connection errors."""
        responses.add(
            responses.POST,
            "https://betterflow.eu/api/v1/sync/auth/token",
            body=requests.exceptions.ConnectionError("Connection refused"),
        )

        result = self.client.exchange_code(
            code="code", device_name="device", code_verifier="verifier"
        )

        assert result.success is False
        assert "connect" in result.error.lower()

    @responses.activate
    def test_revoke_success(self):
        """Test successful token revocation."""
        responses.add(
            responses.POST,
            "https://betterflow.eu/api/agent/revoke",
            json={"revoked": True},
            status=200,
        )

        result = self.client.revoke()

        assert result is True

    @responses.activate
    def test_revoke_failure(self):
        """Test revoke handles errors gracefully."""
        responses.add(
            responses.POST,
            "https://betterflow.eu/api/agent/revoke",
            body=requests.exceptions.ConnectionError("Network error"),
        )

        result = self.client.revoke()

        assert result is False

    @responses.activate
    def test_start_session(self):
        """Test starting a tracking session."""
        responses.add(
            responses.POST,
            "https://betterflow.eu/api/agent/sessions/start",
            json={"session_id": "sess-123"},
            status=200,
        )

        result = self.client.start_session()

        assert result["session_id"] == "sess-123"

    @responses.activate
    def test_end_session(self):
        """Test ending a tracking session."""
        responses.add(
            responses.POST,
            "https://betterflow.eu/api/agent/sessions/end",
            json={"ended": True},
            status=200,
        )

        result = self.client.end_session(reason="user_stopped")

        assert result["ended"] is True

    @responses.activate
    def test_get_status(self):
        """Test getting sync status."""
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/events/status",
            json={"events_today": 150, "last_sync": "2026-02-18T10:00:00Z"},
            status=200,
        )

        result = self.client.get_status()

        assert result["events_today"] == 150

    @responses.activate
    def test_get_config(self):
        """Test getting server configuration."""
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/config",
            json={"sync": {"interval_seconds": 30}},
            status=200,
        )

        result = self.client.get_config()

        assert result["sync"]["interval_seconds"] == 30

    @responses.activate
    def test_get_projects(self):
        """Test getting projects list."""
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/projects",
            json=[{"id": 1, "name": "Project A"}, {"id": 2, "name": "Project B"}],
            status=200,
        )

        result = self.client.get_projects()

        assert len(result) == 2
        assert result[0]["name"] == "Project A"

    def test_set_credentials(self):
        """Test setting credentials."""
        client = BetterFlowClient()
        client.set_credentials("new-token", "new-device")

        assert client.token == "new-token"
        assert client.device_id == "new-device"
        client.close()

    def test_clear_credentials(self):
        """Test clearing credentials."""
        self.client.clear_credentials()

        assert self.client.token is None
        assert self.client.device_id is None

    def test_context_manager(self):
        """Test using client as context manager."""
        with BetterFlowClient() as client:
            assert client is not None


class TestRetryBehavior:
    """Tests for retry with exponential backoff."""

    def setup_method(self):
        """Set up test fixtures."""
        from src.sync.retry import RetryConfig

        # Fast retries for testing
        self.fast_retry = RetryConfig(
            max_retries=2,
            base_delay=0.01,
            max_delay=0.1,
            exponential_base=2.0,
            jitter=False,
        )
        self.client = BetterFlowClient(
            api_url="https://betterflow.eu/api/agent",
            token="test-token",
            retry_config=self.fast_retry,
        )

    def teardown_method(self):
        """Clean up."""
        self.client.close()

    @responses.activate
    def test_retry_on_server_error(self):
        """Test request retries on 5xx errors."""
        # First two calls fail with 503, third succeeds
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/test",
            json={"error": "Service Unavailable"},
            status=503,
        )
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/test",
            json={"error": "Service Unavailable"},
            status=503,
        )
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/test",
            json={"status": "ok"},
            status=200,
        )

        result = self.client._request("GET", "test")

        assert result["status"] == "ok"
        assert len(responses.calls) == 3

    @responses.activate
    def test_retry_exhausted_raises_error(self):
        """Test exhausted retries raise BetterFlowClientError."""
        # All calls fail
        for _ in range(3):
            responses.add(
                responses.GET,
                "https://betterflow.eu/api/agent/test",
                json={"error": "Service Unavailable"},
                status=503,
            )

        with pytest.raises(BetterFlowClientError, match="Server returned"):
            self.client._request("GET", "test")

    @responses.activate
    def test_no_retry_on_auth_error(self):
        """Test auth errors are not retried."""
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/test",
            json={"error": "Unauthorized"},
            status=401,
        )

        with pytest.raises(BetterFlowAuthError):
            self.client._request("GET", "test")

        # Should only call once (no retry)
        assert len(responses.calls) == 1

    @responses.activate
    def test_no_retry_when_disabled(self):
        """Test retry can be disabled per-request."""
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/test",
            json={"error": "Service Unavailable"},
            status=503,
        )

        with pytest.raises(BetterFlowClientError, match="Server returned"):
            self.client._request("GET", "test", retry=False)

        # Should only call once
        assert len(responses.calls) == 1

    @responses.activate
    def test_retry_on_connection_error(self):
        """Test request retries on connection errors."""
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/test",
            body=requests.exceptions.ConnectionError("Connection refused"),
        )
        responses.add(
            responses.GET,
            "https://betterflow.eu/api/agent/test",
            json={"status": "ok"},
            status=200,
        )

        result = self.client._request("GET", "test")

        assert result["status"] == "ok"
        assert len(responses.calls) == 2
