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


def test_user_owned_bundle_is_replaceable():
    with patch.object(su.sys, "platform", "darwin"), \
         patch.object(su, "_get_app_bundle_path", return_value=_FakeApp(os.getuid())), \
         patch.object(su.os, "getuid", return_value=os.getuid()):
        assert su.app_bundle_replaceable() is True


def test_root_owned_bundle_is_not_replaceable():
    # The reported failure: bundle owned by root (uid 0), we are a normal user.
    with patch.object(su.sys, "platform", "darwin"), \
         patch.object(su, "_get_app_bundle_path", return_value=_FakeApp(0)), \
         patch.object(su.os, "getuid", return_value=501):
        assert su.app_bundle_replaceable() is False


def test_running_as_root_can_replace_anything():
    with patch.object(su.sys, "platform", "darwin"), \
         patch.object(su, "_get_app_bundle_path", return_value=_FakeApp(0)), \
         patch.object(su.os, "getuid", return_value=0):
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
