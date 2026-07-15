"""Test-wide safety net: never let the suite touch the REAL agent config.

`Config.update_from_server()` ends with `self.save()`, and `Config.save()` writes
to `platformdirs.user_config_dir(APP_NAME)` — i.e. the live
`~/Library/Application Support/BetterFlow/config.json` of whoever is running the
tests. Any test that feeds `update_from_server()` a payload (several do) therefore
overwrote the developer's own agent config with test defaults: device_id cleared,
setup_complete flipped to False, and — once the suite grew working-hours fixtures —
a bogus enforced schedule written into the running agent.

This hit a real machine on 2026-07-14. It is not a hypothetical.

Redirect every platformdirs-backed path at the class level, autouse, for the whole
session, so no individual test has to remember to monkeypatch it.
"""

import pytest

import src.config as config_module
from src.config import Config


@pytest.fixture(autouse=True)
def _isolate_agent_config(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    log_dir = tmp_path / "logs"
    for d in (cfg_dir, data_dir, log_dir):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Config, "get_config_dir", classmethod(lambda cls: cfg_dir))
    monkeypatch.setattr(Config, "get_data_dir", classmethod(lambda cls: data_dir))
    monkeypatch.setattr(Config, "get_log_dir", classmethod(lambda cls: log_dir))
    monkeypatch.setattr(
        Config, "get_config_file", classmethod(lambda cls: cfg_dir / "config.json")
    )

    # Config's classmethods above are not the only readers of the real
    # platformdirs location. get_machine_uuid() and _load_dotenv() call the
    # module-level `user_config_dir` directly, bypassing Config — so a test that
    # exercises them (e.g. exchange_code -> get_machine_uuid in test_bf_client)
    # would read/write the developer's REAL ~/.../BetterFlow/.machine_id and leak
    # the cached value across the session. Redirect the module-level function too,
    # and clear the process-wide UUID cache before and after each test so no real
    # machine id can bleed in or out.
    monkeypatch.setattr(
        config_module, "user_config_dir", lambda *a, **k: str(cfg_dir)
    )
    config_module._machine_uuid_cache = None
    yield
    config_module._machine_uuid_cache = None
