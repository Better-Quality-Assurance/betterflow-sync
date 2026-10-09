"""The deploy-order gate for lock_time (betterflow-sync#271 / internal-tool2#2771).

An unpatched server's AgentEvent::inferEventType falls through every
bucket_type/bucket_id check for "lock_time" and lands on EVENT_TYPE_APP,
whose default activity_state='active' path BILLS the locked time as active
work -- worse than today's baseline of uploading nothing. So the agent must
never emit lock_time unless the server it is actually talking to has
advertised support for it, and the gate must default OFF (fail closed: an
absent/malformed/empty "capabilities" response from the server, or no
config fetch having succeeded yet, means NOT supported).

The channel: AgentConfigController's existing /api/agent/config response
(already polled periodically by fetch_server_config ->
Config.update_from_server) gains a `capabilities` array. No new endpoint.
"""

from pathlib import Path
from unittest.mock import Mock

from src.config import Config
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine


def _engine(tmp: Path, config: Config = None) -> SyncEngine:
    config = config or Config()
    # allows() is fail-closed on an unknown schedule (WorkingHoursConfig.known
    # defaults False) -- unrelated to the lock_time gate under test, but a
    # real SyncEngine.send_lock_event runs through it via _send_status_span.
    config.working_hours.known = True
    return SyncEngine(
        aw=Mock(),
        bf=Mock(),
        queue=OfflineQueue(db_path=tmp / "q.db", max_size=10000),
        config=config,
        time_tracker=Mock(),
    )


# --- update_from_server: the array -> field mapping ---


def test_capability_defaults_off_before_any_config_fetch():
    cfg = Config()
    assert cfg.capabilities.lock_time is False


def test_server_advertising_lock_time_turns_the_gate_on():
    cfg = Config()
    cfg.update_from_server({"capabilities": ["lock_time"]})
    assert cfg.capabilities.lock_time is True


def test_absent_capabilities_key_stays_off_old_or_unpatched_server():
    """The key an unpatched server's /config response simply does not have --
    this is what talking to a server that predates this feature looks like."""
    cfg = Config()
    cfg.update_from_server({"privacy": {}, "sync": {}})
    assert cfg.capabilities.lock_time is False


def test_empty_capabilities_list_is_off():
    cfg = Config()
    cfg.update_from_server({"capabilities": []})
    assert cfg.capabilities.lock_time is False


def test_capabilities_list_without_lock_time_name_is_off():
    cfg = Config()
    cfg.update_from_server({"capabilities": ["something_else"]})
    assert cfg.capabilities.lock_time is False


def test_malformed_capabilities_value_fails_closed():
    """A non-list value (server bug, or a future incompatible shape) must not
    be treated as support -- never raise, never default on."""
    cfg = Config()
    cfg.update_from_server({"capabilities": "lock_time"})
    assert cfg.capabilities.lock_time is False

    cfg2 = Config()
    cfg2.update_from_server({"capabilities": {"lock_time": True}})
    assert cfg2.capabilities.lock_time is False

    cfg3 = Config()
    cfg3.update_from_server({"capabilities": None})
    assert cfg3.capabilities.lock_time is False


def test_capability_is_recomputed_fresh_every_fetch_not_merged():
    """A server that advertised lock_time and then stops (rollback, or a
    later fetch reaching a different/older server) must be able to turn the
    gate back OFF without a restart -- this is NOT a one-way ratchet."""
    cfg = Config()
    cfg.update_from_server({"capabilities": ["lock_time"]})
    assert cfg.capabilities.lock_time is True

    cfg.update_from_server({"capabilities": []})
    assert cfg.capabilities.lock_time is False, (
        "a later fetch with no lock_time advertised must turn the gate back "
        "off, not leave the previous session's value in place"
    )


def test_capability_is_never_persisted_to_disk(tmp_path, monkeypatch):
    """Mirrors foreground_activity.enabled: re-confirmed by the server every
    session, never trusted from a stale config.json. A build that once
    talked to a patched server and saved lock_time=true must not keep
    emitting lock_time after being pointed at (or falling back to) a server
    that has since rolled the capability back."""
    import src.config as config_module

    monkeypatch.setattr(config_module.Config, "get_config_dir", classmethod(lambda cls: tmp_path))

    cfg = Config()
    cfg.update_from_server({"capabilities": ["lock_time"]})
    assert cfg.capabilities.lock_time is True

    # update_from_server's own save() already ran; load a FRESH Config from
    # that file (new process) and confirm the safe default wins.
    reloaded = Config.load()
    assert reloaded.capabilities.lock_time is False, (
        "capabilities.lock_time must never be trusted from disk -- it is "
        "re-confirmed by the server every session"
    )


# --- send_lock_event: the one call site the gate protects ---


def test_send_lock_event_is_a_no_op_with_the_gate_off(tmp_path):
    """Gate off (default, or a server that has not advertised support): the
    call must be byte-identical to a build that never shipped this method --
    no event sent, no exception, no queueing."""
    cfg = Config()
    assert cfg.capabilities.lock_time is False
    engine = _engine(tmp_path, cfg)
    engine.queue.enqueue = Mock()

    from datetime import datetime, timezone
    engine.send_lock_event(datetime(2026, 1, 1, tzinfo=timezone.utc))

    engine.bf.send_events.assert_not_called()
    engine.queue.enqueue.assert_not_called()


def test_send_lock_event_sends_with_the_gate_on(tmp_path):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    cfg = Config()
    cfg.update_from_server({"capabilities": ["lock_time"]})
    assert cfg.capabilities.lock_time is True
    engine = _engine(tmp_path, cfg)

    result = MagicMock()
    result.success = True
    engine.bf.send_events.return_value = result

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
    engine.send_lock_event(start, end)

    engine.bf.send_events.assert_called_once()
    (events,), _ = engine.bf.send_events.call_args
    assert len(events) == 1
    assert events[0]["bucket_type"] == "lock_time"


def test_send_lock_event_gate_check_happens_before_any_network_call(tmp_path):
    """The gate must refuse BEFORE touching the network/queue, not merely
    suppress a failure afterward -- confirmed by a bf mock that would raise
    if ever called."""
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    cfg = Config()  # gate off
    engine = _engine(tmp_path, cfg)
    engine.bf = MagicMock()
    engine.bf.send_events.side_effect = AssertionError("must not touch the network with the gate off")

    engine.send_lock_event(datetime(2026, 1, 1, tzinfo=timezone.utc))  # must not raise
