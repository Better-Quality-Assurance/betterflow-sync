"""Tests for the EXPECTED_TEAM_ID pin in self_updater._verify_codesign.

The pin ensures even a fresh install rejects updates signed by a
different (but otherwise valid) Apple Developer ID team. This is
defense-in-depth on top of the existing team-ID-mismatch check, which
only catches changes between two installs.
"""

from unittest.mock import patch

import src.self_updater as su


class TestExpectedTeamIDPin:
    def test_constant_matches_betterqa_srl(self):
        assert su.EXPECTED_TEAM_ID == "87NVC57J44"

    def test_accepts_correct_team(self, tmp_path):
        new_app = tmp_path / "new.app"
        new_app.mkdir()
        good = su._SigningInfo(is_signed=True, team_id="87NVC57J44", version="1.5.26")
        with patch("src.self_updater._codesign_verify", return_value=True), \
             patch("src.self_updater._get_signing_info", return_value=good):
            assert su._verify_codesign(new_app, current_app_path=None) is True

    def test_rejects_wrong_team(self, tmp_path):
        new_app = tmp_path / "new.app"
        new_app.mkdir()
        bad = su._SigningInfo(is_signed=True, team_id="EVIL12345A", version="1.5.26")
        with patch("src.self_updater._codesign_verify", return_value=True), \
             patch("src.self_updater._get_signing_info", return_value=bad):
            assert su._verify_codesign(new_app, current_app_path=None) is False

    def test_rejects_unsigned_when_pin_active(self, tmp_path):
        new_app = tmp_path / "new.app"
        new_app.mkdir()
        adhoc = su._SigningInfo(is_signed=True, team_id=None, version="1.5.26")
        with patch("src.self_updater._codesign_verify", return_value=True), \
             patch("src.self_updater._get_signing_info", return_value=adhoc):
            assert su._verify_codesign(new_app, current_app_path=None) is False

    def test_pin_still_rejects_when_current_team_matches(self, tmp_path):
        new_app = tmp_path / "new.app"
        current_app = tmp_path / "current.app"
        new_app.mkdir()
        current_app.mkdir()
        evil = su._SigningInfo(is_signed=True, team_id="EVIL12345A", version="1.5.26")
        with patch("src.self_updater._codesign_verify", return_value=True), \
             patch("src.self_updater._get_signing_info", return_value=evil):
            assert su._verify_codesign(new_app, current_app_path=current_app) is False
