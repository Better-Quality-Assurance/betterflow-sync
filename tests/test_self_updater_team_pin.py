"""Tests for the EXPECTED_TEAM_ID pin in self_updater._verify_codesign.

The pin ensures even a fresh install rejects updates signed by a
different (but otherwise valid) Apple Developer ID team. This is
defense-in-depth on top of the existing team-ID-mismatch check, which
only catches changes between two installs.
"""

from unittest.mock import MagicMock, patch

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

    def test_rejects_when_codesign_verify_fails(self, tmp_path):
        new_app = tmp_path / "new.app"
        new_app.mkdir()
        good = su._SigningInfo(is_signed=True, team_id="87NVC57J44", version="1.5.26")
        with patch("src.self_updater._codesign_verify", return_value=False), \
             patch("src.self_updater._get_signing_info", return_value=good):
            assert su._verify_codesign(new_app, current_app_path=None) is False

    def test_rejects_version_downgrade(self, tmp_path):
        new_app = tmp_path / "new.app"
        current_app = tmp_path / "current.app"
        new_app.mkdir()
        current_app.mkdir()
        current_info = su._SigningInfo(is_signed=True, team_id="87NVC57J44", version="1.5.26")
        new_info = su._SigningInfo(is_signed=True, team_id="87NVC57J44", version="1.5.25")

        def _fake_signing_info(path):
            return current_info if path == current_app else new_info

        with patch("src.self_updater._codesign_verify", return_value=True), \
             patch("src.self_updater._get_signing_info", side_effect=_fake_signing_info):
            assert su._verify_codesign(new_app, current_app_path=current_app) is False

    def test_allows_newer_version(self, tmp_path):
        new_app = tmp_path / "new.app"
        current_app = tmp_path / "current.app"
        new_app.mkdir()
        current_app.mkdir()
        current_info = su._SigningInfo(is_signed=True, team_id="87NVC57J44", version="1.5.25")
        new_info = su._SigningInfo(is_signed=True, team_id="87NVC57J44", version="1.5.26")

        def _fake_signing_info(path):
            return current_info if path == current_app else new_info

        with patch("src.self_updater._codesign_verify", return_value=True), \
             patch("src.self_updater._get_signing_info", side_effect=_fake_signing_info):
            assert su._verify_codesign(new_app, current_app_path=current_app) is True

    def test_rejects_signed_to_unsigned_downgrade(self, tmp_path):
        new_app = tmp_path / "new.app"
        current_app = tmp_path / "current.app"
        new_app.mkdir()
        current_app.mkdir()
        current_info = su._SigningInfo(is_signed=True, team_id="87NVC57J44", version="1.5.26")
        new_info = su._SigningInfo(is_signed=False, team_id=None, version="1.5.26")

        def _fake_signing_info(path):
            return current_info if path == current_app else new_info

        # The pin check (team_id != EXPECTED_TEAM_ID) fires before the
        # current_app_path branch because new_info.team_id is None.
        # Both guards independently reject this update.
        with patch("src.self_updater._codesign_verify", return_value=True), \
             patch("src.self_updater._get_signing_info", side_effect=_fake_signing_info):
            assert su._verify_codesign(new_app, current_app_path=current_app) is False


class TestGetSigningInfo:
    """Unit tests for _get_signing_info parsing, especially the ad-hoc edge case.

    Prior to the fix, the regex for TeamIdentifier matched only the first
    word of "TeamIdentifier=not set", capturing "not" instead of None.  The
    fixed regex with strip() and the != "not set" guard now correctly returns
    team_id=None for ad-hoc-signed bundles.
    """

    def _make_codesign_result(self, returncode: int, stderr: str):
        result = MagicMock()
        result.returncode = returncode
        result.stderr = stderr
        result.stdout = ""
        return result

    def test_adhoc_signed_returns_team_id_none(self, tmp_path):
        """'TeamIdentifier=not set' must produce team_id=None, not team_id='not'."""
        adhoc_stderr = (
            "Executable=/Applications/BetterFlow.app/Contents/MacOS/BetterFlow\n"
            "Identifier=com.betterqa.betterflow\n"
            "Format=app bundle with Mach-O thin (arm64)\n"
            "CodeDirectory v=20400 size=1234 flags=0x2(adhoc)\n"
            "TeamIdentifier=not set\n"
            "Sealed Resources version=2 rules=13 files=42\n"
        )
        codesign_result = self._make_codesign_result(0, adhoc_stderr)
        defaults_result = MagicMock()
        defaults_result.returncode = 0
        defaults_result.stdout = "1.5.26\n"

        with patch("subprocess.run", side_effect=[codesign_result, defaults_result]):
            info = su._get_signing_info(tmp_path)

        assert info.is_signed is True
        assert info.team_id is None, (
            "Ad-hoc-signed bundle must yield team_id=None, not 'not'"
        )

    def test_developer_id_signed_returns_team_id(self, tmp_path):
        """A Developer ID signature with a real team ID is parsed correctly."""
        dev_stderr = (
            "Executable=/Applications/BetterFlow.app/Contents/MacOS/BetterFlow\n"
            "Identifier=com.betterqa.betterflow\n"
            "Format=app bundle with Mach-O thin (arm64)\n"
            "CodeDirectory v=20400 size=1234 flags=0x0(none)\n"
            "TeamIdentifier=87NVC57J44\n"
            "Sealed Resources version=2 rules=13 files=42\n"
        )
        codesign_result = self._make_codesign_result(0, dev_stderr)
        defaults_result = MagicMock()
        defaults_result.returncode = 0
        defaults_result.stdout = "1.5.26\n"

        with patch("subprocess.run", side_effect=[codesign_result, defaults_result]):
            info = su._get_signing_info(tmp_path)

        assert info.is_signed is True
        assert info.team_id == "87NVC57J44"

    def test_unsigned_binary_returns_is_signed_false(self, tmp_path):
        """codesign non-zero with 'code object is not signed' sets is_signed=False."""
        unsigned_stderr = "BetterFlow.app: code object is not signed at all\n"
        codesign_result = self._make_codesign_result(1, unsigned_stderr)
        defaults_result = MagicMock()
        defaults_result.returncode = 0
        defaults_result.stdout = "1.5.26\n"

        with patch("subprocess.run", side_effect=[codesign_result, defaults_result]):
            info = su._get_signing_info(tmp_path)

        assert info.is_signed is False
        assert info.team_id is None
