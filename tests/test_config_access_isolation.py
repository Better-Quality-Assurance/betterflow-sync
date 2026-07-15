"""Finding 1: config_access.get_config_dir() must be redirected by the autouse
test-isolation fixture.

`src/auth/config_access.py` used to have its OWN `platformdirs.user_config_dir()`
reader — a 4th reader of the developer's real config dir that the conftest fixture
(which only patches `Config.get_config_dir` / `src.config.user_config_dir`) did NOT
cover. A test exercising a real `KeychainManager` (whose `_purge_legacy_file` /
`_legacy_credentials_file` resolve through config_access) would therefore
read/write the developer's real `~/Library/.../BetterFlow/.credentials` — the exact
incident class the fixture claims to close.

config_access now delegates to `Config.get_config_dir()`, so the fixture's existing
patch covers it automatically. These tests pin that redirection.
"""

from src.auth import config_access
from src.auth.keychain import _legacy_credentials_file
from src.config import Config


def test_config_access_get_config_dir_is_redirected(tmp_path):
    """The autouse `_isolate_agent_config` fixture (tests/conftest.py) redirects
    Config.get_config_dir() to tmp_path/"config"; config_access must follow it,
    proving there is no longer an independent platformdirs reader here."""
    got = config_access.get_config_dir()

    # Single source of truth: it resolves to exactly what Config resolves to.
    assert got == Config.get_config_dir()
    # And that is the fixture's temp dir, never the developer's real home.
    assert got == tmp_path / "config"
    assert tmp_path in got.parents


def test_legacy_credentials_path_lands_in_temp_dir(tmp_path):
    """The concrete incident surface: keychain._legacy_credentials_file() (called by
    _purge_legacy_file on every store()/delete()) must resolve under the temp dir,
    not the real ~/.../BetterFlow/.credentials."""
    legacy = _legacy_credentials_file()

    assert legacy == tmp_path / "config" / ".credentials"
    assert tmp_path in legacy.parents
