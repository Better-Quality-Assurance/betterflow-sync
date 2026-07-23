"""The `hash_titles` / `title_allowlist` privacy control is GONE, and stays gone.

Why this file exists
--------------------
`Config.privacy.hash_titles` was declared, persisted, populated from the server
and carried in the tray model — and read by no capture or transform code
anywhere in `src/`. The agent has never hashed a window title; there is no
`hashlib` call on any title path. It was a switch that did nothing to anyone's
data, sitting in the product's settings state under a name that says otherwise.

Implementing it was ruled out: titles are the server-side categorisation signal
and we deliberately keep sending them raw. So the control was REMOVED (2026-07-23)
rather than wired up. Title handling is enforced server-side
(`AgentDevice::shouldStoreRawTitle` in internal-tool2, driven by the device row);
the agent has no say and must not appear to.

The agent never sent either value to the server — `bf_client` has no PATCH and no
outbound payload carrying them — so removing them changes no server behaviour.
The server still *sends* them on `/config`; `update_from_server` drops them on
purpose.

These tests are the guard against a well-meaning "restore the missing privacy
setting" commit. They are source-level on purpose: `pystray` binds a display
backend at import and is `None` on a headless CI runner, so anything that needs a
live `TrayIcon` cannot run in the environment that gates merges — and per
`test-fixture-discipline.md` a guard that skips where it matters is not a guard.
"""

from __future__ import annotations

import json
from dataclasses import fields as dc_fields
from pathlib import Path
from unittest.mock import patch

import src.config as config_module
import src.main as main_module
import src.ui.tray as tray_module
from src.config import Config, PrivacySettings
from src.ui.tray import TrayModel

_DEAD_SYMBOLS = ("hash_titles", "title_allowlist")

_SRC_DIR = Path(config_module.__file__).resolve().parent


# --- 1: the field is gone from the config dataclass ------------------------


def test_privacy_settings_declares_no_title_hashing_fields():
    names = {f.name for f in dc_fields(PrivacySettings)}
    for symbol in _DEAD_SYMBOLS:
        assert symbol not in names, (
            f"PrivacySettings re-declared {symbol!r}. It has no client-side "
            "consumer — the agent does not hash titles. See this module's docstring."
        )
    # The real, enforced client-side controls must still be there. If this ever
    # fails, the removal took a live control with it.
    assert "exclude_apps" in names
    assert "domain_only_urls" in names


# --- 2: it does not round-trip through config.json -------------------------


def test_legacy_config_file_does_not_resurrect_the_setting():
    """A device upgrading from an older build has these keys on disk already."""
    config_file = Config.get_config_file()
    config_file.write_text(json.dumps({
        "privacy": {
            "hash_titles": True,
            "title_allowlist": ["Visual Studio Code"],
            "domain_only_urls": True,
        },
    }))

    config = Config.load()

    for symbol in _DEAD_SYMBOLS:
        assert not hasattr(config.privacy, symbol), (
            f"{symbol!r} was restored from an old config.json — Config.load must "
            "drop unknown keys, not re-attach them."
        )
    # ...and saving must not write them back out.
    config.save()
    saved = json.loads(config_file.read_text())
    for symbol in _DEAD_SYMBOLS:
        assert symbol not in saved.get("privacy", {}), (
            f"{symbol!r} was persisted back to config.json"
        )


# --- 3: the server can keep sending it; we ignore it -----------------------


def test_server_sent_hash_window_titles_is_ignored():
    """The /config payload still carries these columns. Ungated, they must be
    dropped — not stored somewhere a future reader mistakes for a live control."""
    config = Config()

    # Run with the deferral gate OFF, which is the *only* state in which the
    # privacy block is applied at all. Testing it while deferred would pass for
    # the wrong reason (nothing in the block executes) — see
    # test-fixture-discipline.md, Phantom 4.
    with patch.object(config_module, "DEFER_UNAPPLIED_SERVER_SETTINGS", False):
        config.update_from_server({
            "privacy": {
                "hash_window_titles": True,
                "title_allowlist": ["SomeApp"],
                "track_browser_domains": True,
            },
        })

    for symbol in _DEAD_SYMBOLS:
        assert not hasattr(config.privacy, symbol), (
            f"update_from_server re-created privacy.{symbol} from the server payload"
        )
    # Proof the block actually ran, so the assertions above mean something:
    # track_browser_domains sits in the same `if` and DID apply.
    assert config.privacy.domain_only_urls is True


# --- 4: it is not in the tray ----------------------------------------------


def test_tray_model_carries_no_title_hashing_state():
    model = TrayModel()
    for symbol in _DEAD_SYMBOLS:
        assert not hasattr(model, symbol), (
            f"TrayModel re-declared {symbol!r}; the tray must not carry state for "
            "a control that does not exist."
        )


def test_tray_source_references_no_title_hashing_control():
    """Covers the menu items, the model snapshot and `set_config` in one sweep:
    if the tray mentions the symbol anywhere outside a comment, it is back."""
    for line_no, line in _code_lines(Path(tray_module.__file__)):
        for symbol in _DEAD_SYMBOLS:
            assert symbol not in line, (
                f"tray.py:{line_no} references {symbol!r}: {line.strip()!r}"
            )


def test_preference_handler_has_no_title_hashing_branch():
    for line_no, line in _code_lines(Path(main_module.__file__)):
        for symbol in _DEAD_SYMBOLS:
            assert symbol not in line, (
                f"main.py:{line_no} references {symbol!r}: {line.strip()!r}"
            )


# --- 5: the grep guard -----------------------------------------------------


def _code_lines(path: Path):
    """Yield (1-based line number, line) for non-comment lines of a source file.

    Comments are exempt on purpose: the removal left explanatory notes naming
    these symbols so the next reader does not "restore" them, and a guard that
    forbade its own explanation would force those notes to be deleted.
    """
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip().startswith("#"):
            continue
        yield i, line


def test_no_consumerless_title_hashing_symbol_anywhere_in_src():
    """Repo-wide guard. Fails if either symbol reappears as live code in `src/`.

    If you are here because this test failed: a client-side title-hashing control
    is only legitimate once something in the capture/transform path READS it.
    Add the consumer first, then relax this test and name the consumer in it.
    """
    offenders = []
    for py_file in sorted(_SRC_DIR.rglob("*.py")):
        for line_no, line in _code_lines(py_file):
            for symbol in _DEAD_SYMBOLS:
                if symbol in line:
                    rel = py_file.relative_to(_SRC_DIR)
                    offenders.append(f"src/{rel}:{line_no}: {line.strip()}")

    assert not offenders, (
        "Consumer-less title-hashing symbol(s) reintroduced:\n  "
        + "\n  ".join(offenders)
    )


def test_no_client_side_title_hashing_exists():
    """The claim the removed field made — that titles get hashed on this device —
    must stay false, or the field should come back and this file should go."""
    title_hashers = []
    for py_file in sorted(_SRC_DIR.rglob("*.py")):
        for line_no, line in _code_lines(py_file):
            lowered = line.lower()
            if "hexdigest" in lowered and "title" in lowered:
                title_hashers.append(f"{py_file.name}:{line_no}")

    assert not title_hashers, (
        "Client-side title hashing appeared at "
        + ", ".join(title_hashers)
        + " — if titles are now hashed on-device, the user-facing control and the "
        "CLAUDE.md Privacy Model section must be restored to describe it."
    )
