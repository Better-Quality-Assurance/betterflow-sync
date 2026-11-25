"""Issue #314, the class rather than the one regex: strip_home against the
shapes Windows really produces, on every field an error report carries.

Three defects these pin, each reproduced on main bdf27cb before the fix:

- A sibling login that CONTINUES this login with a character a Windows user
  name may hold (space, "(", "'") was cut at the prefix: home C:\\Users\\al turned
  C:\\Users\\al bundy\\x into "~ bundy\\x", keeping part of the other person's name
  and corrupting the path.
- An extended-length UNC path (\\\\?\\UNC\\HOST\\C$\\Users\\<login>) was not
  recognised at all: the login went out whole.
- A spelling the pattern cannot derive went out whole instead of being dropped.
"""

import json
import sys

import pytest

from src import error_reporter
from src.error_reporter import ErrorReporter


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    def homes(*h):
        monkeypatch.setattr(error_reporter, "_home_dirs", lambda: list(h))

    return homes


@pytest.mark.parametrize("tail", [" bundy", "(work)", "'brien"])
def test_a_sibling_login_continued_by_a_legal_character_is_never_cut(win, tail):
    win("C:\\Users\\al", "/c/Users/al")
    s = f'File "C:\\Users\\al{tail}\\AppData\\Local\\a.py", line 3'
    out = error_reporter.strip_home(s)
    # Untouched or dropped whole: never "~ bundy", half of someone else's name.
    # getattr: the pre-fix module has no _DROPPED, and the pre-fix run must fail
    # on the mangled output, not on an AttributeError.
    assert out in (s, getattr(error_reporter, "_DROPPED", None)), out


@pytest.mark.parametrize(
    "s",
    [
        "[Errno 13] Permission denied: 'C:\\\\Users\\\\al'",
        'File "C:\\Users\\al\\a.py", line 3',
        "C:\\Users\\al",
        "home C:\\Users\\al: full",
        "C:\\Users\\al\nnext line",
    ],
)
def test_this_users_own_home_is_still_redacted(win, s):
    win("C:\\Users\\al")
    out = error_reporter.strip_home(s)
    assert "Users" not in out and "~" in out, out


def test_extended_length_unc_path_loses_the_login(win):
    win("C:\\Users\\john.smith")
    out = error_reporter.strip_home("\\\\?\\UNC\\FILESRV\\C$\\Users\\john.smith\\a.py")
    assert "john.smith" not in out.lower(), out
    assert out == "~\\a.py"


def test_a_spelling_the_pattern_misses_drops_that_line_only(win):
    win("C:\\Users\\john.smith")
    bs = "\\" * 10  # a separator run longer than _SEP recognises
    s = f"C:{bs}Users{bs}john.smith{bs}a.py\nkept line"
    out = error_reporter.strip_home(s)
    assert "john.smith" not in out.lower(), out
    assert out == error_reporter._DROPPED + "\nkept line"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_a_posix_home_followed_by_text_then_a_path_is_redacted_not_dropped(
    monkeypatch, platform
):
    # The Windows boundary read " python=" as a folder name and dropped the line.
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(error_reporter, "_home_dirs", lambda: ["/Users/al"])
    out = error_reporter.strip_home("cwd=/Users/al python=/usr/bin/python3")
    assert out == "cwd=~ python=/usr/bin/python3", out


def test_residual_check_is_linear_on_a_long_backslash_run(monkeypatch):
    # A home with no parent folder: the residual run must not be retried from
    # every offset of the run (quadratic: 131,072 backslashes took 39 s).
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(error_reporter, "_home_dirs", lambda: ["/root"])
    s = "\\" * 131072
    assert error_reporter.strip_home(s) == s


def test_every_field_of_the_report_is_redacted(win):
    win("C:\\Users\\john.smith")
    p = "\\\\?\\UNC\\FILESRV\\C$\\Users\\john.smith\\AppData\\a.db"
    try:
        raise OSError(2, "No such file", p)
    except OSError as e:
        exc = e
    payload = ErrorReporter(
        endpoint="https://example.invalid/ingest", dsn="d", release="t"
    )._build_payload(
        f"open failed: {p}", "error", exc, {"path": p}, {"file": p, p: [p]}, fingerprint=p
    )
    wire = json.dumps(payload)
    assert "john.smith" not in wire.lower(), wire
