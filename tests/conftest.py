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

# The zone the whole suite treats as "this machine" unless a test overrides it.
# Working-hours evaluation is now machine-local (config.WorkingHoursConfig._localize
# resolves detect_machine_timezone() live), so the tests must pin the detected zone
# or they would read the CI runner's real clock — UTC on ubuntu, the developer's
# zone locally — and disagree across platforms. The existing working-hours fixtures
# were all written against Europe/Bucharest schedules with Bucharest instants, so
# pinning the machine to Bucharest reproduces their pre-change (schedule==machine)
# behaviour exactly. A test exercising DRIFT re-patches detect_machine_timezone to a
# different zone.
TEST_MACHINE_TZ = "Europe/Bucharest"


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


@pytest.fixture(autouse=True)
def _pin_machine_timezone(monkeypatch):
    """Make the machine's detected timezone deterministic across CI platforms.

    _localize() resolves detect_machine_timezone() on every call, which reads the
    real OS /etc/localtime (or tzlocal). Left unpinned, working-hours assertions
    would depend on the runner's own clock (UTC on ubuntu-latest, whatever locally).
    Pin it to Europe/Bucharest — the zone every existing working-hours fixture was
    authored in — so machine-local evaluation lands exactly where schedule-anchored
    evaluation used to. One setattr on config is authoritative: both _localize and
    bf_client._detect_timezone reach the detector through the config module, so a
    test that wants a different machine zone re-patches only this one attribute
    (see _machine_tz in test_working_hours_capture.py).

    NB this is an OPT-OUT default: a test that means to exercise a non-Bucharest
    machine-local path must override it explicitly, or it silently runs as Bucharest.
    """
    monkeypatch.setattr(config_module, "detect_machine_timezone", lambda: TEST_MACHINE_TZ)


@pytest.fixture(autouse=True)
def _no_real_mic_probe(monkeypatch):
    """Never let a test-constructed SyncEngine talk to the REAL microphone.

    call_detection.mic_signal defaults on, so on a macOS/Windows dev machine
    every `SyncEngine(...)` in the suite would build a live CoreAudio/registry
    probe — and a developer running the tests while in a meeting (hot mic)
    would flip mic-session state inside unrelated engine tests. CI (Linux)
    never constructs one, so this also keeps local runs equal to CI. Tests
    that want a mic detector inject one with a fake probe.
    """
    import src.sync.sync_engine as sync_engine_module

    monkeypatch.setattr(
        sync_engine_module, "create_mic_detector", lambda *a, **k: None
    )
