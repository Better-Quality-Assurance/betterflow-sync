"""Minimal config directory access to avoid circular imports with config.py."""

from pathlib import Path
from platformdirs import user_config_dir

APP_NAME = "BetterFlow"
APP_AUTHOR = "BetterQA"


def get_config_dir() -> Path:
    return Path(user_config_dir(APP_NAME, APP_AUTHOR))
