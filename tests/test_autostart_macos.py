"""macOS auto-start: stale LaunchAgent plist self-heal.

`ensure_synced()` only rewrites the plist when `get_auto_start()` is False, and
the old `_get_macos()` returned True whenever the plist merely existed + was
loaded — even if its ProgramArguments pointed at a renamed/moved app that no
longer launches. These pin that a stale plist is detected as not-current so the
startup sync rewrites it (Tiberiu, 2026-06-22: auto-start silently stopped)."""

import plistlib

import src.autostart as autostart


def _write_plist(path, program_args):
    with open(path, "wb") as f:
        plistlib.dump(
            {"Label": "co.betterqa.betterflow", "ProgramArguments": program_args,
             "RunAtLoad": True},
            f,
        )


def test_plist_program_args_reads_installed_args(tmp_path, monkeypatch):
    pl = tmp_path / "agent.plist"
    _write_plist(pl, ["open", "-a", "/Applications/BetterFlow.app"])
    monkeypatch.setattr(autostart, "_plist_path", lambda: pl)
    assert autostart._macos_plist_program_args() == ["open", "-a", "/Applications/BetterFlow.app"]


def test_plist_program_args_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart, "_plist_path", lambda: tmp_path / "nope.plist")
    assert autostart._macos_plist_program_args() is None


def test_is_current_true_when_args_match(tmp_path, monkeypatch):
    pl = tmp_path / "agent.plist"
    _write_plist(pl, ["open", "-a", "/Applications/BetterFlow.app"])
    monkeypatch.setattr(autostart, "_plist_path", lambda: pl)
    monkeypatch.setattr(autostart, "_app_launch_args",
                        lambda: ["open", "-a", "/Applications/BetterFlow.app"])
    assert autostart._macos_plist_is_current() is True


def test_is_current_false_when_path_is_stale(tmp_path, monkeypatch):
    # plist points at the OLD app name; the running app is now BetterFlow.app.
    pl = tmp_path / "agent.plist"
    _write_plist(pl, ["open", "-a", "/Applications/BetterFlow Sync.app"])
    monkeypatch.setattr(autostart, "_plist_path", lambda: pl)
    monkeypatch.setattr(autostart, "_app_launch_args",
                        lambda: ["open", "-a", "/Applications/BetterFlow.app"])
    assert autostart._macos_plist_is_current() is False


def test_is_current_false_when_plist_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart, "_plist_path", lambda: tmp_path / "nope.plist")
    monkeypatch.setattr(autostart, "_app_launch_args",
                        lambda: ["open", "-a", "/Applications/BetterFlow.app"])
    assert autostart._macos_plist_is_current() is False
