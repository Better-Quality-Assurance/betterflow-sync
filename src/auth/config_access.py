"""Minimal config directory access to avoid circular imports with config.py."""

from pathlib import Path


def get_config_dir() -> Path:
    """Return the config directory, delegating to ``Config.get_config_dir()``.

    This is the ONLY platformdirs-backed config-dir reader outside ``config.py``,
    and it deliberately does not call ``platformdirs.user_config_dir`` itself.
    Delegating keeps a single source of truth and — critically — means test
    isolation is automatic: ``tests/conftest.py`` monkeypatches
    ``Config.get_config_dir`` for the whole session, so a test that exercises a
    real ``KeychainManager`` (which reads this via ``_legacy_credentials_file``)
    is redirected to the temp dir instead of the developer's real
    ``~/Library/.../BetterFlow/.credentials``.

    ``Config`` is imported inside the function body, not at module top, because
    this module exists to avoid the circular import that a top-level
    ``from ..config import Config`` would risk. By call time ``config`` is fully
    loaded, so the deferred import resolves cleanly.
    """
    try:
        from ..config import Config
    except ImportError:
        from config import Config
    return Config.get_config_dir()
