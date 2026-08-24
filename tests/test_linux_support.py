"""Tests for the Linux platform branches added for the AppImage build.

These run on any host (they patch platform/sys), so they execute as part of
the normal macOS/CI suite without needing a Linux machine.
"""

import importlib.util
import os
import pathlib
from unittest.mock import MagicMock, patch

import src.autostart as autostart
import src.aw_manager as awm
import src.notifications as notifications_module
import src.self_updater as self_updater
from src.notifications import send_notification
from src.update_checker import _ASSET_PATTERNS, _find_platform_asset

# ---------------------------------------------------------------------------
# update_checker: Linux .AppImage asset selection
# ---------------------------------------------------------------------------

class TestUpdateCheckerLinux:
    def test_linux_pattern_registered(self):
        assert _ASSET_PATTERNS["Linux"] == "BetterFlow-linux"

    def test_finds_appimage_asset(self):
        release = {
            "assets": [
                {"name": "BetterFlow-macOS-arm64.dmg", "browser_download_url": "https://x/m.dmg"},
                {"name": "BetterFlow-Windows.zip", "browser_download_url": "https://x/w.zip"},
                {"name": "BetterFlow-linux-x86_64.AppImage", "browser_download_url": "https://x/l.AppImage"},
            ]
        }
        assert _find_platform_asset(release, system="Linux") == "https://x/l.AppImage"

    def test_returns_none_when_no_appimage(self):
        release = {"assets": [{"name": "BetterFlow-Windows.zip", "browser_download_url": "https://x/w.zip"}]}
        assert _find_platform_asset(release, system="Linux") is None


# ---------------------------------------------------------------------------
# aw_manager / download_aw: Linux platform key, asset, paths
# ---------------------------------------------------------------------------

class TestAwManagerLinux:
    def test_release_asset_present(self):
        assert "linux" in awm.RELEASE_ASSETS
        assert awm.RELEASE_ASSETS["linux"].endswith("-linux-x86_64.zip")

    def test_platform_key(self, monkeypatch):
        monkeypatch.setattr(awm.platform, "system", lambda: "Linux")
        assert awm._get_platform_key() == "linux"

    def test_install_and_db_dirs_use_xdg(self, monkeypatch, tmp_path):
        monkeypatch.setattr(awm.platform, "system", lambda: "Linux")
        monkeypatch.setattr(awm, "user_data_dir", lambda *a, **k: str(tmp_path / "BetterFlow"))

        install_dir = awm._get_install_dir()
        db_dir = awm._get_db_dir()

        assert install_dir.endswith(os.path.join("trackers", "linux"))
        assert str(tmp_path) in install_dir
        assert db_dir.endswith(os.path.join("data", "aw-db.sqlite"))
        assert str(tmp_path) in db_dir


def _load_download_aw():
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "download_aw.py"
    spec = importlib.util.spec_from_file_location("download_aw", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestDownloadAwScriptLinux:
    def test_release_asset_present(self):
        download_aw = _load_download_aw()
        assert "linux" in download_aw.RELEASE_ASSETS

    def test_get_platform_linux(self, monkeypatch):
        # `get_platform` now delegates to src.aw_release.platform_key, so the
        # script no longer imports `platform` itself and there is no
        # `download_aw.platform` to reach through. Patch the stdlib module the
        # delegate actually reads.
        import platform as platform_module

        download_aw = _load_download_aw()
        monkeypatch.setattr(platform_module, "system", lambda: "Linux")
        assert download_aw.get_platform() == "linux"

    def test_linux_takes_the_x86_64_asset_whatever_the_machine_reports(self, monkeypatch):
        """Linux has no architecture dimension, deliberately.

        Upstream publishes no linux-arm64 build, so an arm64 Linux host keeps
        getting the x86_64 archive exactly as it did before the macOS split —
        unchanged, not improved. Pinned so the asymmetry is not "tidied" into
        symmetry, which would turn that host into a hard "no release for this
        platform".
        """
        import platform as platform_module

        download_aw = _load_download_aw()
        monkeypatch.setattr(platform_module, "system", lambda: "Linux")
        monkeypatch.setattr(platform_module, "machine", lambda: "aarch64")
        assert download_aw.get_asset_key() == "linux"
        assert download_aw.RELEASE_ASSETS["linux"].endswith("-linux-x86_64.zip")


# ---------------------------------------------------------------------------
# autostart: XDG .desktop entry
# ---------------------------------------------------------------------------

class TestAutostartLinux:
    def test_write_and_remove(self, monkeypatch, tmp_path):
        monkeypatch.setattr(autostart.platform, "system", lambda: "Linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setenv("APPIMAGE", "/home/u/Apps/BetterFlow.AppImage")
        monkeypatch.setattr(autostart.sys, "frozen", True, raising=False)

        assert autostart.set_auto_start(True) is True
        entry = tmp_path / "autostart" / "co.betterqa.betterflow.desktop"
        assert entry.exists()
        content = entry.read_text()
        assert "Exec=/home/u/Apps/BetterFlow.AppImage" in content
        assert "X-GNOME-Autostart-enabled=true" in content
        assert autostart.get_auto_start() is True

        assert autostart.set_auto_start(False) is True
        assert not entry.exists()
        assert autostart.get_auto_start() is False

    def test_quotes_exec_path_with_spaces(self, monkeypatch, tmp_path):
        monkeypatch.setattr(autostart.platform, "system", lambda: "Linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setenv("APPIMAGE", "/home/u/My Apps/BetterFlow.AppImage")
        monkeypatch.setattr(autostart.sys, "frozen", True, raising=False)

        assert autostart.set_auto_start(True) is True
        content = (tmp_path / "autostart" / "co.betterqa.betterflow.desktop").read_text()
        assert 'Exec="/home/u/My Apps/BetterFlow.AppImage"' in content

    def test_dev_mode_refused(self, monkeypatch, tmp_path):
        monkeypatch.setattr(autostart.platform, "system", lambda: "Linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        monkeypatch.setattr(autostart.sys, "frozen", False, raising=False)
        assert autostart.set_auto_start(True) is False


# ---------------------------------------------------------------------------
# notifications: notify-send dispatch
# ---------------------------------------------------------------------------

class TestNotificationsLinux:
    def test_dispatch_calls_notify_send(self, monkeypatch):
        monkeypatch.setattr(notifications_module.platform, "system", lambda: "Linux")
        with patch("src.notifications.subprocess.run") as mock_run, patch(
            "src.notifications._resolve_icon_path", return_value=None
        ):
            mock_run.return_value = MagicMock(returncode=0)
            send_notification("Title", "Body")

        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "notify-send"
        assert "Title" in args
        assert "Body" in args

    def test_missing_notify_send_is_silent(self, monkeypatch):
        monkeypatch.setattr(notifications_module.platform, "system", lambda: "Linux")
        with patch("src.notifications.subprocess.run", side_effect=FileNotFoundError), patch(
            "src.notifications._resolve_icon_path", return_value=None
        ):
            # Should not raise.
            send_notification("Title", "Body")


# ---------------------------------------------------------------------------
# self_updater: AppImage path resolution
# ---------------------------------------------------------------------------

class TestSelfUpdaterLinux:
    def test_app_bundle_path_from_appimage_env(self, monkeypatch, tmp_path):
        # Use a real file so .resolve() is stable across hosts (macOS firmlinks
        # rewrite non-existent /home paths, which is irrelevant on Linux).
        appimage = tmp_path / "BetterFlow.AppImage"
        appimage.write_text("")
        monkeypatch.setattr(self_updater.sys, "platform", "linux")
        monkeypatch.setenv("APPIMAGE", str(appimage))
        assert self_updater._get_app_bundle_path() == appimage.resolve()

    def test_app_bundle_path_none_without_appimage(self, monkeypatch):
        monkeypatch.setattr(self_updater.sys, "platform", "linux")
        monkeypatch.delenv("APPIMAGE", raising=False)
        assert self_updater._get_app_bundle_path() is None


def test_afk_source_unavailable_on_linux():
    """Linux has no OS idle clock → AfkSource.available() is False, so the engine
    keeps the external bf-idle-tracker path regardless of the config flag."""
    from src.sync.afk_source import AfkSource
    src = AfkSource(600, "host", idle_clock=lambda: None)
    assert src.available() is False
