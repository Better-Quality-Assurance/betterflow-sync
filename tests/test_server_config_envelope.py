"""The /config response envelope.

The API wraps every payload: BaseApiController::successResponse produces
    {"success": true, "message": "...", "data": {...}}
and BaseApiClient._request returns that verbatim — callers unwrap for themselves.

get_config() did not, and handed the whole envelope to Config.update_from_server(),
which reads TOP-LEVEL keys. Nothing ever matched, so NO server configuration had ever
reached any agent: not afk_timeout, not the privacy flags, not working_hours. The
failure was silent — update_from_server ends in save(), so the agent wrote the
unchanged config back and logged "Server configuration applied".

It was invisible for as long as config was merely advisory. It became a total outage
the moment capture went fail-closed: no schedule -> known=False -> record nothing,
forever. Caught on a real machine on 2026-07-14, after the beta had already shipped.

These tests pin the wire contract on both sides of the unwrap.
"""

from unittest.mock import Mock, patch

from src.config import Config
from src.sync.bf_client import BetterFlowClient

# Exactly what src/app/Http/Controllers/Api/BaseApiController.php emits.
WIRE_RESPONSE = {
    "success": True,
    "message": "Operation successful",
    "data": {
        "device": {"id": 14, "device_name": "sync:abc"},
        "tracking": {"afk_timeout_minutes": 20},
        "working_hours": {
            "enforced": True,
            "work_start": "07:30",
            "work_end": "22:00",
            "working_days": [1, 2, 3, 4, 5],
            "timezone": "Europe/Bucharest",
        },
    },
}


class TestGetConfigUnwrapsTheEnvelope:
    def _client(self):
        return BetterFlowClient(api_url="https://example.test/api/agent")

    def test_returns_the_payload_not_the_envelope(self):
        client = self._client()
        with patch.object(BetterFlowClient, "_request", return_value=WIRE_RESPONSE):
            cfg = client.get_config()

        # The keys update_from_server actually looks for must be at the top level.
        assert "working_hours" in cfg
        assert "tracking" in cfg
        assert "success" not in cfg and "data" not in cfg

    def test_an_already_unwrapped_body_passes_through(self):
        """Tolerate a response with no envelope rather than returning {}."""
        client = self._client()
        bare = {"working_hours": {"enforced": False}}
        with patch.object(BetterFlowClient, "_request", return_value=bare):
            assert client.get_config() == bare

    def test_a_non_dict_body_is_not_fed_to_update_from_server(self):
        client = self._client()
        with patch.object(BetterFlowClient, "_request", return_value=None):
            assert client.get_config() == {}


class TestTheWireResponseActuallyConfiguresTheAgent:
    """End to end over the real shapes: what the server sends must arrive as a KNOWN
    schedule, or a fail-closed agent records nothing for the rest of its life."""

    def test_working_hours_arrive_and_the_schedule_becomes_known(self):
        client = BetterFlowClient(api_url="https://example.test/api/agent")
        config = Config()

        with patch.object(BetterFlowClient, "_request", return_value=WIRE_RESPONSE):
            config.update_from_server(client.get_config())

        assert config.working_hours.known is True, (
            "schedule never became known: a fail-closed agent would suppress capture "
            "forever and track nothing"
        )
        assert config.working_hours.enforced is True
        assert config.working_hours.work_start == "07:30"

    def test_other_server_settings_arrive_too(self):
        """The unwrap must feed the WHOLE payload, not just working_hours — proven with
        a setting that is not behind the deferral gate.

        (afk_timeout would be the obvious one, but it is deliberately deferred: see
        TestDeferredSettingsDoNotRideAlong. It has been silently ignored for the life of
        the feature, and un-ignoring it moves billed hours on 37 devices.)"""
        client = BetterFlowClient(api_url="https://example.test/api/agent")
        config = Config()

        payload = dict(WIRE_RESPONSE["data"])
        payload["sync"] = {"batch_size": 250, "sync_interval_seconds": 90}
        with patch.object(BetterFlowClient, "_request",
                          return_value={"success": True, "data": payload}):
            config.update_from_server(client.get_config())

        assert config.sync.batch_size == 250
        assert config.sync.interval_seconds == 90

    def test_the_raw_envelope_configures_NOTHING(self):
        """Pins the bug itself, so nobody 'simplifies' the unwrap back out."""
        config = Config()
        config.update_from_server(WIRE_RESPONSE)  # the old, un-unwrapped call

        assert config.working_hours.known is False
        assert config.aw.afk_timeout_minutes != 20


class TestDeferredSettingsDoNotRideAlong:
    """The envelope fix makes EVERY server setting apply for the first time ever. Two of
    them change the real world and have nothing to do with working hours, so they are
    gated behind DEFER_UNAPPLIED_SERVER_SETTINGS until each is rolled out on purpose:

      afk_timeout_minutes  37/44 prod devices say 20, agents have run 10 -> moves HOURS
      hash_window_titles   41/44 say ON -> window titles become hashes, admins lose them

    The privacy release must change exactly one thing: we stop recording out of hours.
    """

    def _applied(self):
        client = BetterFlowClient(api_url="https://example.test/api/agent")
        config = Config()
        before = (config.aw.afk_timeout_minutes, config.privacy.hash_titles)
        payload = dict(WIRE_RESPONSE["data"])
        payload["privacy"] = {"hash_window_titles": True, "track_browser_domains": True}
        with patch.object(BetterFlowClient, "_request",
                          return_value={"success": True, "data": payload}):
            config.update_from_server(client.get_config())
        return config, before

    def test_the_schedule_still_applies(self):
        config, _ = self._applied()
        assert config.working_hours.known is True
        assert config.working_hours.enforced is True
        assert config.working_hours.work_start == "07:30"

    def test_the_afk_timeout_does_not_move(self):
        config, before = self._applied()
        assert config.aw.afk_timeout_minutes == before[0], (
            "AFK timeout moved: this silently changes billed hours on 37 devices"
        )

    def test_window_titles_do_not_start_hashing(self):
        config, before = self._applied()
        assert config.privacy.hash_titles == before[1], (
            "hash_window_titles applied: admins would silently lose readable titles"
        )


class TestSyncEngineFetchPath:
    def test_fetch_server_config_applies_the_schedule(self):
        """The real call chain: SyncEngine.fetch_server_config -> bf.get_config ->
        Config.update_from_server."""
        from src.sync.sync_engine import SyncEngine

        bf = Mock()
        bf.get_config.return_value = WIRE_RESPONSE["data"]  # client unwraps first
        config = Config()
        engine = SyncEngine(aw=Mock(), bf=bf, queue=Mock(), config=config,
                            time_tracker=Mock())

        engine.fetch_server_config()

        assert config.working_hours.known is True
        assert config.working_hours.enforced is True
