"""Resolve the human-facing release version (which may carry a prerelease suffix).

``__version__`` / ``CFBundleVersion`` must stay a plain dotted number (macOS
rejects a non-numeric ``CFBundleVersion``), so the ``-beta.N`` / ``-rc.N`` suffix
that distinguishes a prerelease lives only on the git tag. This helper recovers
that suffix for *display* (tray menu + tooltip) at build time, so a beta build
shows ``v1.5.68-beta.1`` instead of a bare ``v1.5.68`` indistinguishable from
stable.

Kept as a tiny dependency-free pure function so build.spec can import it and it
can be unit-tested without invoking PyInstaller.
"""
import re

# A version-like tag: optional leading "v", then MAJOR.MINOR.PATCH, optionally
# followed by a "-beta.N" / "-rc.N" / etc. prerelease suffix.
_VERSION_TAG_RE = re.compile(r"^v?\d+\.\d+\.\d+")


def format_release_version(ci_ref: str, git_tag: str, app_version: str) -> str:
    """Pick the display version, preferring a real release tag over the number.

    Args:
        ci_ref: CI-provided ref name (e.g. GitHub ``GITHUB_REF_NAME``). On a tag
            build this is the tag (``v1.5.68-beta.1``); on a branch/PR build it
            is the branch name, which must be ignored.
        git_tag: Output of ``git describe --tags --exact-match`` (the tag at
            HEAD), or "" when HEAD is not tagged.
        app_version: The numeric ``__version__`` fallback.

    Returns:
        The display version with any prerelease suffix, ``v`` stripped — or the
        numeric ``app_version`` when neither input is a version tag.
    """
    for candidate in (ci_ref, git_tag):
        if candidate and _VERSION_TAG_RE.match(candidate.strip()):
            return candidate.strip().lstrip("v")
    return app_version
