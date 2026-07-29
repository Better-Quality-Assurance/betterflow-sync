"""app_bundle_replaceable() gates the self-update against a bundle the current
user can't replace — the root-owned MDM .pkg install that EPERMs on the rename
every cycle (Tudor/Fabian, 2026-07). See self_updater.app_bundle_replaceable.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import src.self_updater as su


class _FakeApp:
    def __init__(self, uid: int) -> None:
        self._uid = uid

    def stat(self) -> SimpleNamespace:
        return SimpleNamespace(st_uid=self._uid)


def test_non_darwin_is_always_replaceable():
    with patch.object(su.sys, "platform", "win32"):
        assert su.app_bundle_replaceable() is True


def test_unknown_layout_is_replaceable():
    with patch.object(su.sys, "platform", "darwin"), \
         patch.object(su, "_get_app_bundle_path", return_value=None):
        assert su.app_bundle_replaceable() is True


# These three force the darwin branch via the platform patch, so they exercise
# macOS logic on EVERY runner — including Windows, where `os.getuid` does not
# exist at all. Two consequences, both load-bearing:
#   * use a literal uid, never `os.getuid()`, or the test file raises
#     AttributeError on Windows before any assertion runs;
#   * patch `su.os.getuid` with `create=True`, because patch.object refuses by
#     default to patch an attribute the target module does not have.
# Dropping either one turns the whole Windows build red — and since the release
# job needs every platform green, that blocks the release rather than just
# failing a test (v1.5.119's first tag build, 2026-07-29).
_A_NORMAL_UID = 501
_ROOT_UID = 0


def test_user_owned_bundle_is_replaceable():
    with patch.object(su.sys, "platform", "darwin"), \
         patch.object(su, "_get_app_bundle_path", return_value=_FakeApp(_A_NORMAL_UID)), \
         patch.object(su.os, "getuid", create=True, return_value=_A_NORMAL_UID):
        assert su.app_bundle_replaceable() is True


def test_root_owned_bundle_is_not_replaceable():
    # The reported failure: bundle owned by root (uid 0), we are a normal user.
    with patch.object(su.sys, "platform", "darwin"), \
         patch.object(su, "_get_app_bundle_path", return_value=_FakeApp(_ROOT_UID)), \
         patch.object(su.os, "getuid", create=True, return_value=_A_NORMAL_UID):
        assert su.app_bundle_replaceable() is False


def test_running_as_root_can_replace_anything():
    with patch.object(su.sys, "platform", "darwin"), \
         patch.object(su, "_get_app_bundle_path", return_value=_FakeApp(_ROOT_UID)), \
         patch.object(su.os, "getuid", create=True, return_value=_ROOT_UID):
        assert su.app_bundle_replaceable() is True


# ---- real-environment checks against the actual machine state ----

@pytest.mark.skipif(sys.platform != "darwin", reason="macOS ownership semantics")
def test_real_root_owned_backup_is_not_replaceable():
    p = Path("/Applications/BetterFlow-old-1.5.106.app")
    if not p.exists():
        pytest.skip("no root-owned backup bundle present on this machine")
    if p.stat().st_uid == os.getuid():
        pytest.skip("backup bundle is not root-owned here")
    with patch.object(su, "_get_app_bundle_path", return_value=p):
        assert su.app_bundle_replaceable() is False


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS ownership semantics")
def test_real_user_owned_app_matches_ownership():
    p = Path("/Applications/BetterFlow.app")
    if not p.exists():
        pytest.skip("no installed app present on this machine")
    with patch.object(su, "_get_app_bundle_path", return_value=p):
        assert su.app_bundle_replaceable() is (p.stat().st_uid == os.getuid())
