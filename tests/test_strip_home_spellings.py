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


# 8.3 short names. Fixtures follow the DOCUMENTED rule, not our implementation:
# Microsoft KB 142982 "How Windows Generates 8.3 File Names from Long File
# Names": delete spaces and invalid characters; drop every period but the last;
# the part before the last period is cut to 6 characters + "~N"; the part after
# it (the "extension") is cut to 3. The KB's own example is "This is a long
# filename.789" -> THISIS~1.789, i.e. the extension survives. So:
#   john.smith       -> JOHN~1.SMI   (stem "john", extension "smi")
#   alice.wonderland -> ALICE~1.WON
#   administrator    -> ADMINI~1     (no period: 6 chars, no extension)
# UNVERIFIED on a real Windows box: these come from the KB text. Limit: after
# ~4 collisions Windows 2000+ switches to a hash form (a public example:
# p.znosko -> P310C~1.ZNO); that form is not derivable and is not handled.
@pytest.mark.parametrize("login,short", [
    ("john.smith", "JOHN~1.SMI"),
    ("alice.wonderland", "ALICE~1.WON"),
    ("administrator", "ADMINI~1"),
    ("john smith", "JOHNSM~1"),
    ("a.b.company", "AB~1.COM"),
], ids=["john.smith", "alice.wonderland", "administrator", "space", "two-dots"])
@pytest.mark.parametrize("n", [1, 4, 9])
def test_short_name_8_3(monkeypatch, login, short, n):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(error_reporter, "_home_dirs", lambda: ["C:\\Users\\" + login])
    stem, _, ext = short.partition(".")
    short_n = stem.replace("~1", f"~{n}") + ("." + ext if ext else "")
    out = error_reporter.strip_home(f"C:\\Users\\{short_n}\\AppData\\x and C:\\Users\\{login}\\y")
    assert out == "~\\AppData\\x and ~\\y"


def test_wrong_short_names_are_not_matched(monkeypatch):
    # The pre-fix derivation produced ALICEW~1 (dots removed, then 6 chars);
    # Windows never generates that for alice.wonderland.
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(error_reporter, "_home_dirs", lambda: ["C:\\Users\\alice.wonderland"])
    s = "C:\\Users\\ALICEW~1\\x"
    assert error_reporter.strip_home(s) == s


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


# --------------------------------------------------------------------------- #
# Round 3 (PERF): a backslash run must not make strip_home quadratic           #
# --------------------------------------------------------------------------- #
#
# _SEP was r"(?:\\+|/)": on a run of N backslashes that is not followed by a
# home component, the regex engine retries from every offset in the run and each
# attempt consumes the rest of it, so cost grows with N squared (measured pre-fix:
# 131,072 backslashes = 39 s on darwin). strip_home runs over the whole log tail
# (up to 512 KB) on the heartbeat thread, and over every crash report. A bound
# of 2 s is generous: the fixed code measures in milliseconds.

import time  # noqa: E402

PERF_BOUND_S = 2.0


@pytest.mark.parametrize("platform,home", [
    ("darwin", "/Users/alice"),
    ("win32", "C:\\Users\\alice"),
], ids=["darwin", "win32"])
def test_backslash_run_is_not_quadratic(monkeypatch, platform, home):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(error_reporter, "_home_dirs", lambda: [home])
    for text in ("\\" * 200_000, "x" + "\\" * 200_000 + "Users"):
        t0 = time.monotonic()
        out = error_reporter.strip_home(text)
        elapsed = time.monotonic() - t0
        assert out == text  # nothing to redact in it
        assert elapsed < PERF_BOUND_S, f"{platform}: {elapsed:.1f}s for {len(text)} chars"


def test_512kb_realistic_log_is_fast(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(error_reporter, "_home_dirs", lambda: [WIN_HOME])
    line = 'File "C:\\\\Users\\\\alice\\\\x.py", line 3 and C:\\\\Windows\\\\a.dll ' + "\\" * 300 + "\n"
    text = line * ((512 * 1024) // len(line))
    t0 = time.monotonic()
    out = error_reporter.strip_home(text)
    elapsed = time.monotonic() - t0
    assert "alice" not in out
    assert elapsed < PERF_BOUND_S, f"{elapsed:.1f}s for {len(text)} chars"


def test_separator_run_of_up_to_8_still_redacted(win):
    # The bound is 8 backslashes (json.dumps of repr is 4): all of 1..8 match.
    for n in range(1, 9):
        sep = "\\" * n
        out = error_reporter.strip_home(f"C:{sep}Users{sep}alice{sep}x")
        assert "alice" not in out, n
