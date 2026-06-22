"""Tests for release-version display resolution (src/release_version.py).

A beta build must be distinguishable in the tray. __version__ stays numeric (for
CFBundleVersion), so the -beta.N/-rc.N suffix is recovered from the release tag
at build time and shown as RELEASE_VERSION.
"""
from src.release_version import format_release_version


def test_ci_beta_tag_keeps_suffix():
    assert format_release_version("v1.5.68-beta.1", "", "1.5.68") == "1.5.68-beta.1"


def test_ci_rc_tag_keeps_suffix():
    assert format_release_version("v2.0.0-rc.3", "", "2.0.0") == "2.0.0-rc.3"


def test_ci_stable_tag_strips_v():
    assert format_release_version("v1.5.69", "", "1.5.69") == "1.5.69"


def test_branch_ref_ignored_falls_back_to_git_tag():
    # On a branch/PR build GITHUB_REF_NAME is the branch — must be ignored in
    # favour of an exact git tag at HEAD.
    assert format_release_version("main", "v1.5.68-beta.2", "1.5.68") == "1.5.68-beta.2"


def test_branch_ref_and_no_tag_falls_back_to_numeric():
    assert format_release_version("feat/some-branch", "", "1.5.68") == "1.5.68"


def test_no_inputs_returns_numeric():
    assert format_release_version("", "", "1.5.68") == "1.5.68"


def test_tag_without_leading_v():
    assert format_release_version("1.5.68-beta.1", "", "1.5.68") == "1.5.68-beta.1"


def test_whitespace_trimmed():
    assert format_release_version(" v1.5.68-beta.1\n", "", "1.5.68") == "1.5.68-beta.1"


def test_tray_uses_release_version_for_display():
    """The tray surfaces a release version (not None) and wires it into the
    hover tooltip. Doesn't import _build_info directly: that module is generated
    at build time and is absent during CI's test step, where the tray falls back
    to __version__ — the wiring must hold on every import path."""
    from src.ui import tray

    assert tray._RELEASE_VERSION  # set on every import path (build_info or fallback)
    assert f"BetterFlow v{tray._RELEASE_VERSION}" == tray._BASE_TOOLTIP
