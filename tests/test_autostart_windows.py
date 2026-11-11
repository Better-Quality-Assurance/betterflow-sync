"""Windows auto-start: a Run entry left by ANOTHER copy must be re-pointed.

Sachi (Windows, device 16), 2026-11-06 to 11-11: every login started an old
copy (1.5.29 on 11-11, also 1.5.29 / 1.5.53 / 1.5.140 on 11-06), which then
self-updated to 1.5.145. The old copies died silently when the network came
back while logged out, and stayed grey after outages.

The Run value stores the exe path of whichever copy registered it, and
`_get_windows()` only asked whether the value EXISTED. So the current copy
saw "enabled" and `ensure_synced()` never re-pointed it at itself: the old
copy kept winning every login. macOS already treats a plist naming another
bundle as not-current (`_macos_plist_is_current`); this is the Windows twin.

winreg only exists on Windows, so a fake module with the same calls stands in
for it, backed by a dict.
"""

from __future__ import annotations

import sys
import types

import pytest

import src.autostart as autostart

CURRENT = r"C:\Users\Sachi\AppData\Local\Programs\BetterFlow\BetterFlow.exe"
OLD = r"C:\Users\Sachi\Downloads\BetterFlow\BetterFlow.exe"


class _FakeWinreg(types.ModuleType):
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        super().__init__("winreg")
        self.values: dict[str, str] = {}
        self.writes: list[str] = []

    def OpenKey(self, root, path, reserved, access):
        assert path == autostart._WIN_RUN_KEY
        return object()

    def CloseKey(self, key):
        pass

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value
        self.writes.append(value)

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


@pytest.fixture
def reg(monkeypatch):
    fake = _FakeWinreg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(autostart.platform, "system", lambda: "Windows")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", CURRENT)
    return fake


def test_entry_naming_an_old_copy_is_repointed_at_startup(reg):
    reg.values[autostart._WIN_VALUE_NAME] = OLD

    autostart.ensure_synced()

    assert reg.values[autostart._WIN_VALUE_NAME] == CURRENT, (
        "the login entry still launches the old copy"
    )


def test_entry_naming_this_copy_is_left_alone(reg):
    reg.values[autostart._WIN_VALUE_NAME] = CURRENT

    autostart.ensure_synced()

    assert reg.writes == [], "a correct entry must not be rewritten every launch"


@pytest.mark.parametrize(
    "stored",
    [
        f'"{CURRENT}"',  # quoted, the form installers usually write
        CURRENT.upper(),  # Windows paths are case-insensitive
        CURRENT.replace("\\", "/"),
        f"  {CURRENT}  ",
    ],
)
def test_same_exe_spelled_differently_counts_as_current(reg, stored):
    reg.values[autostart._WIN_VALUE_NAME] = stored

    autostart.ensure_synced()

    assert reg.writes == [], f"{stored!r} names this copy and must not churn"


def test_missing_entry_is_created(reg):
    autostart.ensure_synced()
    assert reg.values[autostart._WIN_VALUE_NAME] == CURRENT


def test_repoint_logs_the_old_path(reg, caplog):
    reg.values[autostart._WIN_VALUE_NAME] = OLD
    with caplog.at_level("INFO", logger="src.autostart"):
        autostart.ensure_synced()
    assert any(OLD in r.getMessage() for r in caplog.records), (
        "the old copy's path must be logged so support can tell the user what to delete"
    )


def test_tray_checkmark_reads_off_for_a_foreign_entry(reg):
    # Same contract as macOS _get_macos: an entry that launches another copy
    # is not "this app launches at login".
    reg.values[autostart._WIN_VALUE_NAME] = OLD
    assert autostart.get_auto_start() is False
    reg.values[autostart._WIN_VALUE_NAME] = CURRENT
    assert autostart.get_auto_start() is True
