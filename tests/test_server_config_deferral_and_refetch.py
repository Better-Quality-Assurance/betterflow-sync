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
