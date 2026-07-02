"""A Windows app running from a Downloads folder must not churn self-updates.

Sachi (device 16, 2026-07-02) ran BetterFlow straight out of
``C:\\Users\\Administrator\\Downloads\\BetterFlow-Windows (1)``. Windows can't
replace a locked .exe in place, so every self-update silently failed to
persist: the app cold-started back on an ancient bundled 1.5.29, re-detected
the update, re-applied, relaunched, and reverted again — forever. Her update
banners showed ``1.5.29 -> v1.5.82/83/84/87/88`` across sessions.

The updater now refuses to stage/apply when it's running from a Downloads
folder (Windows only) and instead nags the user to install it properly.

Windows paths are exercised via PureWindowsPath so the logic is testable on a
POSIX CI host (a bare ``Path(r"C:\\...")`` there is a single POSIX component).
"""

from pathlib import Path, PureWindowsPath
from unittest.mock import Mock

import pytest

import src.self_updater as su


def _reset_warning():
    su._downloads_warning_sent = False


class TestRunningFromDownloads:
    def test_standard_downloads_path_is_detected(self):
        p = PureWindowsPath(r"C:\Users\Administrator\Downloads\BetterFlow-Windows (1)")
        assert su._running_from_downloads(p) is True

    def test_case_insensitive(self):
        p = PureWindowsPath(r"C:\Users\admin\downloads\BetterFlow")
        assert su._running_from_downloads(p) is True

    def test_stable_install_location_is_not_flagged(self):
        p = PureWindowsPath(r"C:\Program Files\BetterFlow")
        assert su._running_from_downloads(p) is False

    def test_none_is_not_flagged(self):
        assert su._running_from_downloads(None) is False

    def test_downloads_as_a_substring_is_not_a_false_positive(self):
        # "Downloads2" is a different folder — only an exact path component counts.
        p = PureWindowsPath(r"C:\Users\admin\Downloads2\BetterFlow")
        assert su._running_from_downloads(p) is False


class TestApplyRefusesFromDownloads:
    def test_apply_returns_false_and_notifies_without_extracting(self, monkeypatch, tmp_path):
        _reset_warning()
        monkeypatch.setattr(su.sys, "platform", "win32", raising=False)
        dl = PureWindowsPath(r"C:\Users\Administrator\Downloads\BetterFlow-Windows (1)")
        monkeypatch.setattr(su, "_get_app_bundle_path", lambda: dl)
        notify = Mock()
        monkeypatch.setattr("src.notifications.send_notification", notify)
        # If the guard fails to short-circuit, extraction would run — make it
        # explode so the test can't pass by accident.
        monkeypatch.setattr(su.tempfile, "mkdtemp",
                            Mock(side_effect=AssertionError("should not extract")))

        artifact = tmp_path / "BetterFlow-Windows-Update.zip"
        artifact.write_bytes(b"not a real zip")

        ok = su._apply_local_artifact(artifact)

        assert ok is False
        notify.assert_called_once()

    def test_apply_not_blocked_on_macos_from_downloads(self, monkeypatch, tmp_path):
        # macOS replaces the whole .app atomically — a Downloads run is fine and
        # must NOT be refused. Prove the guard didn't short-circuit by letting
        # execution reach extraction (mkdtemp), and confirm no nag fired.
        _reset_warning()
        monkeypatch.setattr(su.sys, "platform", "darwin", raising=False)
        dl = Path("/Users/x/Downloads/BetterFlow.app")
        monkeypatch.setattr(su, "_get_app_bundle_path", lambda: dl)
        notify = Mock()
        monkeypatch.setattr("src.notifications.send_notification", notify)
        monkeypatch.setattr(su.tempfile, "mkdtemp",
                            Mock(side_effect=RuntimeError("reached extraction")))

        artifact = tmp_path / "BetterFlow-macOS-arm64.dmg"
        artifact.write_bytes(b"x")

        with pytest.raises(RuntimeError, match="reached extraction"):
            su._apply_local_artifact(artifact)
        notify.assert_not_called()  # the Downloads guard is win32-only


class TestStageSkipsFromDownloads:
    def test_stage_returns_false_without_downloading(self, monkeypatch):
        _reset_warning()
        monkeypatch.setattr(su.sys, "platform", "win32", raising=False)
        dl = PureWindowsPath(r"C:\Users\Administrator\Downloads\BetterFlow-Windows (1)")
        monkeypatch.setattr(su, "_get_app_bundle_path", lambda: dl)
        monkeypatch.setattr("src.notifications.send_notification", Mock())
        # Downloading must never be attempted when the guard trips.
        monkeypatch.setattr(su, "_download_to_file",
                            Mock(side_effect=AssertionError("should not download")))

        url = ("https://github.com/Better-Quality-Assurance/betterflow-sync/"
               "releases/download/v1.5.90/BetterFlow-Windows-Update.zip")
        ok = su.stage_update(url, "1.5.90")
        assert ok is False
