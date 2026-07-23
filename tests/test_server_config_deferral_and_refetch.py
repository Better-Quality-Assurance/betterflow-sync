"""Hardening for the newly-live /config path (v1.5.100).

The /config envelope fix means server config reaches the agent for the FIRST
time on upgrade. Three gaps that follows from that, closed here:

1. PRIVACY-EGRESS settings must NOT silently flip on upgrade. A device row
   carrying collect_full_urls=1 / track_browser_domains=0 would otherwise start
   egressing full URLs the moment this build lands — a privacy change opposite
   to the release's intent. The whole privacy/collection blocks are deferred;
   only working_hours + operational sync tuning go live.
2. A wrapped body with data:null must not crash update_from_server.
3. Config is re-fetched periodically so a mid-session schedule change reaches a
   running agent (was fetched once per process → restricted-after-startup users
   were recorded until restart).
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock

from src.config import Config
import src.config as config_module
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine


def _engine(tmp: Path) -> SyncEngine:
    return SyncEngine(
        aw=Mock(),
        bf=Mock(),
        queue=OfflineQueue(db_path=tmp / "q.db", max_size=10000),
        config=Config(),
        time_tracker=Mock(),
    )


# --- 1: privacy-egress settings are deferred; feature + sync tuning are not ---


def test_privacy_egress_settings_do_not_apply_on_upgrade():
    cfg = Config()
    before_full_urls = cfg.privacy.collect_full_urls
    before_domain_only = cfg.privacy.domain_only_urls
    before_allowlist = list(cfg.privacy.title_allowlist)

    cfg.update_from_server({
        "privacy": {
            "collect_full_urls": True,       # would egress FULL urls
            "track_browser_domains": False,  # would flip domain_only_urls off
            "title_allowlist": ["SomeAppNotInDefaults"],
        },
        "collection": {"track_browser_urls": True, "collect_page_category": True},
    })

    assert cfg.privacy.collect_full_urls == before_full_urls, (
        "server collect_full_urls must be deferred, not applied silently on upgrade"
    )
    assert cfg.privacy.domain_only_urls == before_domain_only
    assert cfg.privacy.title_allowlist == before_allowlist, "title_allowlist deferred too"


def test_capture_and_billing_blocks_do_not_apply_on_upgrade():
    # First-ever config delivery must not silently flip capture/billing behaviour
    # from a stale device row. All of these stay at local defaults.
    cfg = Config()
    before = (
        cfg.foreground_activity.enabled,
        cfg.call_detection.enabled,
        cfg.engagement.window_minutes,
        cfg.fraud_detection.min_app_diversity,
    )

    cfg.update_from_server({
        "foreground_activity": {"enabled": True, "max_credit_minutes": 90},
        "call_detection": {"enabled": True},
        "sync": {"in_process_input": True, "batch_size": 250},  # batch_size is benign → applies
        "engagement": {"window_minutes": 99},
        "fraud_detection": {"min_app_diversity": 42},
    })

    assert (
        cfg.foreground_activity.enabled,
        cfg.call_detection.enabled,
        cfg.engagement.window_minutes,
        cfg.fraud_detection.min_app_diversity,
    ) == before, "capture/billing/engagement/fraud blocks must be deferred on upgrade"
    # ...but benign sync tuning in the same payload still applies:
    assert cfg.sync.batch_size == 250
    # ...and so does in_process_input — UN-DEFERRED on purpose (2026-07-17): it is
    # the shipped remediation for Windows devices whose external input watcher is
    # hook-blocked and who report zero keystrokes all month (fraud false
    # positives). Still opt-in: only an explicit server true enables it.
    assert cfg.sync.in_process_input is True


def test_in_process_input_server_flag_round_trip():
    # Un-deferred, string-safe, and still remotely kill-switchable. Deliberately
    # platform-INDEPENDENT: the default is the subject of the two tests around
    # this one, so this case starts from a known-off flag instead of patching
    # the real sys.platform process-wide. (It also used to read the RUNNER's
    # platform, which passes on the ubuntu PR job and fails on the
    # windows-latest leg of the release-tag matrix — a red release build days
    # after a green PR.)
    cfg = Config()
    cfg.sync.in_process_input = False

    cfg.update_from_server({"sync": {"in_process_input": "false"}})
    assert cfg.sync.in_process_input is False, 'STRING "false" must not enable'

    cfg.update_from_server({"sync": {"in_process_input": True}})
    assert cfg.sync.in_process_input is True

    cfg.update_from_server({"sync": {"in_process_input": False}})
    assert cfg.sync.in_process_input is False, "server can also switch it back off"


def test_in_process_input_defaults_on_for_windows(monkeypatch):
    monkeypatch.setattr(config_module.sys, "platform", "win32")

    cfg = Config()

    assert cfg.sync.in_process_input is True

    cfg.update_from_server({"sync": {"in_process_input": False}})
    assert cfg.sync.in_process_input is False, "server kill-switch must still win"


def test_in_process_input_ships_dormant_off_windows(monkeypatch):
    """The other half of the platform default, and the half that has to stay
    OFF. Making the round-trip test above platform-independent removed the only
    assertion that macOS/Linux ship dormant — and on those platforms "enabled"
    means installing a CGEventTap under the user's Input Monitoring grant, so
    it must be opt-in (server) and never a default. Pinned to a patched
    platform, not the CI runner's, so it holds on every leg of the matrix."""
    for plat in ("darwin", "linux"):
        monkeypatch.setattr(config_module.sys, "platform", plat)
        assert Config().sync.in_process_input is False, f"{plat} must ship dormant"


def test_working_hours_and_sync_tuning_still_apply():
    # The gate must NOT swallow the intended-live settings.
    cfg = Config()
    cfg.update_from_server({
        "working_hours": {
            "enforced": True, "work_start": "07:30", "work_end": "16:00",
            "working_days": [1, 2, 3, 4, 5], "timezone": "Europe/Bucharest",
        },
        "sync": {"batch_size": 250, "sync_interval_seconds": 90},
    })
    assert cfg.working_hours.known is True
    assert cfg.working_hours.enforced is True
    assert cfg.working_hours.work_start == "07:30"
    assert cfg.sync.batch_size == 250
    assert cfg.sync.interval_seconds == 90


# --- 2: data:null envelope must not crash the config path ---------------------


def test_get_config_data_null_returns_empty_not_none():
    from src.sync.bf_client import BetterFlowClient

    client = BetterFlowClient.__new__(BetterFlowClient)
    client._request = Mock(return_value={"success": True, "data": None})
    result = client.get_config()
    assert result == {}, "data:null must degrade to {}, never None (which crashes update_from_server)"
    # And the empty dict is safe to feed onward:
    Config().update_from_server(result)  # must not raise


# --- 3: periodic refetch so schedule changes reach a running agent ------------


def test_config_refetch_due_logic():
    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)
    interval = SyncEngine._CONFIG_REFETCH_INTERVAL_SECONDS

    # Never fetched yet → always due.
    assert engine._config_refetch_due(1000.0) is True

    # After a successful fetch, not due again until the interval elapses.
    engine._config_fetched = True
    engine._last_config_fetch_monotonic = 1000.0
    assert engine._config_refetch_due(1000.0 + interval - 1) is False
    assert engine._config_refetch_due(1000.0 + interval) is True
    assert engine._config_refetch_due(1000.0 + interval + 5) is True


def test_fetch_server_config_stamps_the_refetch_clock():
    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)
    engine.bf.get_config = Mock(return_value={})
    assert engine._last_config_fetch_monotonic == 0.0
    engine.fetch_server_config()
    assert engine._config_fetched is True
    assert engine._last_config_fetch_monotonic > 0.0


def test_config_refetch_due_holds_when_flag_set_without_a_real_timestamp():
    # The guard branch: _config_fetched forced True (e.g. in other tests) but no
    # real fetch ever stamped the clock → must NOT refetch (0.0 is not "stale").
    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)
    engine._config_fetched = True
    engine._last_config_fetch_monotonic = 0.0
    assert engine._config_refetch_due(1_000_000.0) is False


def test_failed_refetch_backs_off_instead_of_retrying_every_cycle():
    # THE finding-1 regression guard: a /config route that 500s while the rest of
    # the API is healthy must not leave an already-configured agent "due" every
    # cycle (which would burn the whole per-cycle budget in the retry chain).
    # Monotonic is pinned so the assertion is clock-independent (CI runners have
    # low uptime, so real monotonic() can be < any hardcoded value).
    from unittest.mock import patch as _patch

    from src.sync.bf_client import BetterFlowClientError

    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)
    engine._config_fetched = True
    engine._last_config_fetch_monotonic = 1000.0  # old, relative to the pinned now
    engine.bf.get_config = Mock(side_effect=BetterFlowClientError("config route 500"))

    with _patch("src.sync.sync_engine.time.monotonic", return_value=9000.0):
        engine.fetch_server_config()

    assert engine._config_fetched is True, "a failed refetch must not lose configured state"
    assert engine._last_config_fetch_monotonic == 9000.0, (
        "a failed refetch must stamp the clock so it backs off, not retry every cycle"
    )
    # ...so it is NOT immediately due again.
    assert engine._config_refetch_due(9000.0 + 1) is False


def test_failed_initial_fetch_keeps_retrying_every_cycle():
    # The other direction: a brand-new agent that has never succeeded must keep
    # retrying fast (it must learn its fail-closed schedule ASAP), not back off.
    from src.sync.bf_client import BetterFlowClientError

    tmp = Path(tempfile.mkdtemp())
    engine = _engine(tmp)
    assert engine._config_fetched is False
    engine.bf.get_config = Mock(side_effect=BetterFlowClientError("down"))

    engine.fetch_server_config()

    assert engine._config_fetched is False
    assert engine._config_refetch_due(1_000_000.0) is True, (
        "an agent that never fetched must keep retrying, not back off"
    )


def test_sync_gates_config_fetch_on_config_refetch_due():
    # Wiring test: prove sync() gates the fetch on _config_refetch_due(), not the
    # old `not self._config_fetched`. With _config_fetched=True, reverting the
    # gate would stop consulting the method and this would fail. Patching the
    # decision (rather than the clock) keeps it deterministic and avoids poking
    # the drain loop's monotonic-based deadline.
    from unittest.mock import patch as _patch

    aw, bf, queue = Mock(), Mock(), Mock()
    bf.is_reachable.return_value = True
    queue.is_empty.return_value = True
    aw.get_buckets.return_value = {}
    engine = SyncEngine(aw=aw, bf=bf, queue=queue, config=Config(), time_tracker=Mock())
    engine._backlog_reconciled = True
    engine._config_fetched = True  # already configured → old gate would never fetch
    bf.get_config = Mock(return_value={})

    with _patch.object(engine, "_config_refetch_due", return_value=False):
        engine.sync()
        assert not bf.get_config.called, "not due → sync() must NOT re-fetch"

    bf.get_config.reset_mock()
    with _patch.object(engine, "_config_refetch_due", return_value=True):
        engine.sync()
        assert bf.get_config.called, "due → sync() must re-fetch (proves the gate is wired)"


# -- the persisted-config override that would have made the rollout a no-op ---
#
# Origin 2026-07-23: flipping in_process_input's default ON for Windows fixes
# nothing on a device that already has a config.json — update_from_server() ends
# with self.save(), so every Windows agent in the fleet has written
# "in_process_input": false to disk, and _from_dict rebuilds SyncSettings from
# that. The new default only ever reaches FRESH installs. Same trap, same file,
# as foreground_activity.enabled (see save()/_from_dict).


def test_persisted_in_process_input_does_not_override_platform_default(monkeypatch):
    monkeypatch.setattr(config_module.sys, "platform", "win32")

    # What every upgraded Windows device has on disk, written by <=1.5.116.
    cfg = Config._from_dict({"sync": {"in_process_input": False, "batch_size": 250}})

    assert cfg.sync.in_process_input is True, (
        "a stale persisted false must not pin the Windows default off on upgrade"
    )
    assert cfg.sync.batch_size == 250, "unrelated persisted sync tuning still loads"

    # The server keeps both directions: it can still switch it off per device.
    cfg.update_from_server({"sync": {"in_process_input": False}})
    assert cfg.sync.in_process_input is False


def test_in_process_input_is_never_persisted(monkeypatch, tmp_path):
    monkeypatch.setattr(config_module.sys, "platform", "win32")
    monkeypatch.setattr(Config, "get_config_file",
                        classmethod(lambda cls: tmp_path / "config.json"))

    cfg = Config()
    cfg.update_from_server({"sync": {"in_process_input": True}})  # ends in save()

    import json
    written = json.loads((tmp_path / "config.json").read_text())

    assert "in_process_input" not in written.get("sync", {}), (
        "the flag is platform-defaulted and server-driven; persisting it lets a "
        "stale disk value outrank both"
    )
    assert "batch_size" in written.get("sync", {}), "sibling sync settings still persist"


# -- _from_dict must not damage the caller's dict, or lose the file to one bad
#    block. Both traps applied to every nested block, not just the one that
#    happened to need a fix.


def test_from_dict_does_not_mutate_the_callers_nested_blocks():
    """`data = dict(data)` is a TOP-LEVEL copy, so popping a key out of a nested
    block reached through to the caller's dict — which the in_process_input and
    foreground_activity.enabled drops both do."""
    original = {
        "sync": {"in_process_input": False, "batch_size": 250},
        "foreground_activity": {"enabled": True, "max_credit_minutes": 90},
    }
    snapshot = {k: dict(v) for k, v in original.items()}

    Config._from_dict(original)

    assert original == snapshot, "_from_dict must leave the caller's dict intact"


def test_one_corrupt_block_does_not_discard_the_whole_config():
    """_from_dict runs inside load()'s except, so a block that is not a mapping
    used to raise and take the ENTIRE config with it — api_url, working_hours,
    engagement — silently falling back to defaults."""
    for bad in (None, [], "nonsense", 42):
        cfg = Config._from_dict({
            "api_url": "https://app.betterflow.eu/api/agent",
            "sync": bad,
            "working_hours": {"enforced": True, "work_start": "07:30"},
        })
        assert cfg.api_url == "https://app.betterflow.eu/api/agent", (
            f"a {type(bad).__name__} sync block must not cost us the rest of the file"
        )
        assert cfg.working_hours.work_start == "07:30", "sibling blocks survive"
        assert cfg.sync.batch_size == Config().sync.batch_size, "bad block -> defaults"
