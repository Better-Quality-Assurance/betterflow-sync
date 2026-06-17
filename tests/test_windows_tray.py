"""Tests for the Windows 11 tray-icon promotion matching logic."""

import sys

import pytest

from src.windows_tray import select_entries_to_promote

EXE = r"C:\Users\sachi\AppData\Local\Programs\BetterFlow\BetterFlow.exe"


def test_promotes_matching_unpromoted_entry():
    entries = [("1001", EXE, 0)]
    assert select_entries_to_promote(entries, EXE) == ["1001"]


def test_skips_already_promoted_entry():
    entries = [("1001", EXE, 1)]
    assert select_entries_to_promote(entries, EXE) == []


def test_ignores_other_apps():
    entries = [
        ("1001", r"C:\Windows\System32\OtherApp.exe", 0),
        ("1002", EXE, 0),
    ]
    assert select_entries_to_promote(entries, EXE) == ["1002"]


def test_match_is_case_and_separator_insensitive():
    # Registry may store a different casing / forward slashes than sys.executable.
    stored = EXE.lower().replace("\\", "/")
    entries = [("1001", stored, 0)]
    assert select_entries_to_promote(entries, EXE) == ["1001"]


def test_entry_without_executable_path_is_skipped():
    entries = [("1001", None, 0), ("1002", EXE, 0)]
    assert select_entries_to_promote(entries, EXE) == ["1002"]


def test_missing_is_promoted_value_is_treated_as_unpromoted():
    # IsPromoted absent (None) -> not yet promoted -> should be promoted.
    entries = [("1001", EXE, None)]
    assert select_entries_to_promote(entries, EXE) == ["1001"]


def test_promote_tray_icon_is_noop_off_windows(monkeypatch):
    if sys.platform == "win32":
        pytest.skip("covers the non-Windows guard")
    from src.windows_tray import promote_tray_icon

    assert promote_tray_icon(EXE) is False
