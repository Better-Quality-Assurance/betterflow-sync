"""bf-idle-tracker self-heal: a stale ad-hoc tracker left by a pre-signing
build keeps a fragile TCC grant that macOS silently denies (blind tracker, no
AFK heartbeat -> false idle). The app must reinstall the Developer-ID-signed
bundle copy so the Input Monitoring grant is stable across updates.

Proven live on a fleet machine 2026-06-18: the persistent tracker was ad-hoc
(Identifier=bf-idle-tracker-5555..., TeamIdentifier=not set) while the bundled
copy was Developer ID (Team 87NVC57J44). Removing the persistent copy made the
app reinstall a working, signed, self-contained tracker; a one-time re-grant
restored the heartbeat. These tests pin the reinstall DECISION.
"""

from unittest.mock import patch

from src.aw_manager import BETTERQA_TEAM_ID, AWManager


def _teams(install, bundle):
    """Patch the codesign reader so the install/bundle binaries report fixed
    team identifiers (None = ad-hoc / unsigned).

    Match on a substring rather than a leading slash so this is independent of
    the path separator — os.path.join uses backslashes on Windows CI, which a
    `startswith("/install/")` check would miss.
    """

    def fake(binary_path):
        if "install" in binary_path:
            return install
        if "bundle" in binary_path:
            return bundle
        return None

    return patch.object(AWManager, "_tracker_team_identifier", staticmethod(fake))


def test_reinstall_when_installed_adhoc_and_bundle_signed():
    """The real-world bug: ad-hoc persistent copy, Developer-ID bundle."""
    with _teams(install=None, bundle=BETTERQA_TEAM_ID):
        assert AWManager._should_reinstall_trackers("/install", "/bundle") is True


def test_no_reinstall_when_both_signed_with_our_team():
    """Already healed (or shipped signed from the start) — leave it alone."""
    with _teams(install=BETTERQA_TEAM_ID, bundle=BETTERQA_TEAM_ID):
        assert AWManager._should_reinstall_trackers("/install", "/bundle") is False


def test_no_reinstall_when_bundle_also_adhoc():
    """Old build whose bundle is ad-hoc too — swapping ad-hoc for ad-hoc would
    only churn the binary and force a needless re-grant for no gain."""
    with _teams(install=None, bundle=None):
        assert AWManager._should_reinstall_trackers("/install", "/bundle") is False


def test_reinstall_when_installed_is_foreign_team_but_bundle_ours():
    """Defensive: a tracker signed by some other team still gets replaced by our
    properly-signed bundle copy."""
    with _teams(install="OTHERTEAM9", bundle=BETTERQA_TEAM_ID):
        assert AWManager._should_reinstall_trackers("/install", "/bundle") is True


def test_team_identifier_is_none_off_macos():
    """On non-macOS there is no codesign, so the reader returns None and the
    decision never triggers a reinstall (no TCC subject to heal)."""
    with patch("src.aw_manager.platform.system", return_value="Linux"):
        assert AWManager._tracker_team_identifier("/anything") is None
