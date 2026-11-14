"""BW TASK-002506 round 2: strip_home must catch every SPELLING of the home dir
that real producers emit, not just the one os.path gives us.

str(OSError) renders the filename with repr(), so ``C:\\Users\\alice`` comes out
with doubled backslashes; json.dumps of that doubles them again; paths also turn
up with forward slashes, as UNC admin shares, and as 8.3 short names.
"""

import json
import sys

import pytest

from src import error_reporter

WIN_HOME = "C:\\Users\\alice"
FRAME = "AppData\\Local\\BetterFlow\\credentials.json"


@pytest.fixture
def win(monkeypatch):
    # strip_home reads sys.platform at call time to pick IGNORECASE.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(error_reporter, "_home_dirs", lambda: [WIN_HOME])


def _oserror_text():
    return str(PermissionError(13, "x", WIN_HOME + "\\" + FRAME))


def test_oserror_repr_doubled_backslashes(win):
    text = _oserror_text()
    assert "alice" in text and "\\\\" in text  # the fixture IS the repr shape
    out = error_reporter.strip_home(text)
    assert "alice" not in out
    assert "~" in out and "credentials.json" in out


def test_json_escaped_doubled_doubled_backslashes(win):
    text = json.dumps(_oserror_text())
    assert "alice" in text
    out = error_reporter.strip_home(text)
    assert "alice" not in out and "credentials.json" in out


@pytest.mark.parametrize("sp", [
    "C:/Users/alice/AppData/x.log",
    "c:/users/ALICE/AppData/x.log",
    "\\\\HOST\\C$\\Users\\alice\\AppData\\x.log",
    "\\\\?\\C:\\Users\\alice\\AppData\\x.log",
    "C:\\Users\\alice\\AppData\\x.log",
    "c:\\users\\Alice\\AppData\\x.log",
], ids=["fwd", "fwd-case", "unc", "extended", "plain", "mixed-case"])
def test_windows_spellings_redacted(win, sp):
    out = error_reporter.strip_home(f'File "{sp}", line 3')
    assert "alice" not in out.lower()
    assert "AppData" in out


def test_short_name_8_3(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(error_reporter, "_home_dirs", lambda: ["C:\\Users\\alice.wonderland"])
    out = error_reporter.strip_home("C:\\Users\\ALICEW~1\\AppData\\x and C:\\Users\\alice.wonderland\\y")
    assert out == "~\\AppData\\x and ~\\y"


def test_controls_untouched(win):
    for s in ["C:\\Program Files\\App\\x.py", "C:\\Users\\alicexyz\\x", "C:\\Users\\bob\\x",
              'File "C:\\\\Windows\\\\System32\\\\a.dll"']:
        assert error_reporter.strip_home(s) == s


def test_sibling_login_boundary_for_doubled(win):
    out = error_reporter.strip_home("C:\\\\Users\\\\alice\\\\x C:\\\\Users\\\\alicia\\\\y")
    assert "alice" not in out and "alicia" in out


def test_macos_lowercase_users(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(error_reporter, "_home_dirs", lambda: ["/Users/alice"])
    out = error_reporter.strip_home("/users/alice/x /Users/Alice/y /Users/alicia/z")
    assert out == "~/x ~/y /Users/alicia/z"
