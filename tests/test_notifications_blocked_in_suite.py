"""Guard: a normal test must not be able to post a real notification.

The suite reaches send_notification through break_manager, reminders,
system_event_handler, aw_manager and main. Before the conftest block those all
posted for real -- on macOS via osascript, attributed to Script Editor, which
clear_notifications() cannot clear programmatically. Four full-suite runs on
2026-08-31 put ten banners into a developer's Notification Center.

Three things are asserted here, and they are deliberately different questions:

1. The four private senders are not the module's own functions during an
   ordinary test (identity, against a reference captured at import time --
   i.e. before any fixture ran -- not against a NAME, which a rename breaks
   and any decoy satisfies).
2. Calling each sender, and calling send_notification on each platform,
   reaches no OS boundary: no subprocess, no Foundation delivery. Driving the
   platform explicitly matters -- on a macOS box with pyobjc installed,
   send_notification never touches subprocess at all, so a subprocess-only
   assertion holds whether or not the block works.
3. The set of files that opt OUT of the block is pinned, so a new opt-out has
   to be written into this list and reviewed rather than inherited by adding a
   test to an already-exempt file.

NB this file never spells the opt-out marker literally -- the scan in (3) reads
every tests/test_*.py, so a literal here would find itself (a detector poisoned
by its own sentinel). It is assembled from parts below.
"""

# ruff: noqa: N802 -- the Foundation fakes below must answer to the
# Objective-C selector names src/notifications.py calls, verbatim.
# Same convention as tests/test_notification_delivery_verification.py.

from pathlib import Path

import pytest

import src.notifications as notifications
from src.notifications import NotificationOutcome

_SENDERS = (
    "_send_macos_pyobjc",
    "_send_macos_osascript",
    "_send_windows",
    "_send_linux",
)

# Captured at import (collection) time, before any autouse fixture has run, so
# these are the module's real senders and are not something this file authored.
_ORIGINAL_SENDERS = {name: getattr(notifications, name) for name in _SENDERS}

# Argument shapes differ: the macOS senders take `sound`, the others do not.
_SENDER_ARGS = {
    "_send_macos_pyobjc": ("Break Over", "Tracking resumed", True),
    "_send_macos_osascript": ("Break Over", "Tracking resumed", True),
    "_send_windows": ("Break Over", "Tracking resumed"),
    "_send_linux": ("Break Over", "Tracking resumed"),
}


# ---------------------------------------------------------------------------
# A fake Foundation, so the pyobjc leg has a readable OS boundary on every
# platform. If the block is working nothing here is ever touched; if it breaks,
# the real _send_macos_pyobjc imports this and the delivery is recorded.
# ---------------------------------------------------------------------------


class _FakeNote:
    def __init__(self):
        self._identifier = None

    def setTitle_(self, value):
        pass

    def setInformativeText_(self, value):
        pass

    def setSoundName_(self, value):
        pass

    def setContentImage_(self, value):
        pass

    def setIdentifier_(self, value):
        self._identifier = value

    def identifier(self):
        return self._identifier


class _FakeNSUserNotification:
    @classmethod
    def alloc(cls):
        return cls

    @classmethod
    def init(cls):
        return _FakeNote()


class _FakeCenter:
    def __init__(self, delivered):
        self._delivered = delivered

    def deliverNotification_(self, note):
        self._delivered.append(note)

    def deliveredNotifications(self):
        return list(self._delivered)

    def removeAllDeliveredNotifications(self):
        self._delivered.clear()


@pytest.fixture
def os_boundary(monkeypatch):
    """Record every OS egress the notification module has, on any platform.

    Returns a dict with `subprocess` (argv lists) and `foundation` (delivered
    notifications). Both must stay empty for a blocked send.
    """
    import subprocess as subprocess_module
    import sys
    import types

    record = {"subprocess": [], "foundation": []}

    def _fake_run(args, *a, **kw):
        # Record rather than raise: a raise inside send_notification is caught
        # by its own `except Exception` and reported as FAILED, which reads as
        # an ordinary sender failure. The recorded argv is the evidence.
        record["subprocess"].append(args)
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess_module, "run", _fake_run)

    fake_foundation = types.ModuleType("Foundation")
    fake_foundation.NSUserNotification = _FakeNSUserNotification
    fake_foundation.NSUserNotificationCenter = types.SimpleNamespace(
        defaultUserNotificationCenter=lambda: _FakeCenter(record["foundation"])
    )
    monkeypatch.setitem(sys.modules, "Foundation", fake_foundation)

    # Keep the icon lookup off the real filesystem; an icon would drag AppKit in.
    monkeypatch.setattr(notifications, "_resolve_icon_path", lambda: None)
    return record


def _assert_nothing_escaped(record):
    assert record["subprocess"] == [], (
        f"a real subprocess call escaped: {record['subprocess']}"
    )
    assert record["foundation"] == [], (
        "a notification was handed to NSUserNotificationCenter -- the pyobjc "
        "sender ran for real"
    )


# ---------------------------------------------------------------------------
# 1. the senders are replaced (identity, not name)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _SENDERS)
def test_sender_is_not_the_real_function_during_an_ordinary_test(name):
    assert getattr(notifications, name) is not _ORIGINAL_SENDERS[name], (
        f"{name} is the REAL sender during an ordinary test -- a suite run "
        "can post notifications to the machine running it"
    )


# ---------------------------------------------------------------------------
# 2. and calling them reaches nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _SENDERS)
def test_calling_a_sender_directly_reaches_no_os_boundary(name, os_boundary):
    getattr(notifications, name)(*_SENDER_ARGS[name])
    _assert_nothing_escaped(os_boundary)


@pytest.mark.parametrize(
    "system,pyobjc",
    [
        ("Darwin", True),   # NSUserNotification leg -- never touches subprocess
        ("Darwin", False),  # osascript fallback leg
        ("Windows", None),
        ("Linux", None),
    ],
)
def test_send_notification_does_not_reach_the_os(
    system, pyobjc, os_boundary, monkeypatch
):
    """The public entry point still runs its own dispatch, but nothing escapes.

    Each platform is driven explicitly. Left to the real platform.system(), a
    macOS machine with pyobjc present routes to _send_macos_pyobjc and a
    subprocess assertion can never fail -- it would hold against a completely
    disabled block.
    """
    monkeypatch.setattr(notifications.platform, "system", lambda: system)
    if pyobjc is not None:
        monkeypatch.setattr(
            notifications, "_try_load_macos_pyobjc", lambda: pyobjc
        )

    outcome = notifications.send_notification("Break Over", "Tracking resumed")

    _assert_nothing_escaped(os_boundary)
    # The block's stub reports DELIVERED. Every real sender on these three
    # legs reports UNKNOWN (osascript, PowerShell and notify-send are all
    # unreadable), so this discriminates too rather than merely describing.
    assert outcome is NotificationOutcome.DELIVERED, (
        f"{system} (pyobjc={pyobjc}) produced {outcome} -- a real sender ran"
    )


# ---------------------------------------------------------------------------
# 3. the opt-out list is pinned
# ---------------------------------------------------------------------------

# Assembled, never written whole: this file is itself a tests/test_*.py and the
# scan below reads all of them, so a literal here would match itself.
_MARKER = "real_" + "notifications"

# Every file allowed to exempt itself from the conftest block, with the reason.
# conftest.py is not listed because the scan covers test_*.py only -- conftest
# is where the marker is DEFINED, and it is the positive control below.
_KNOWN_OPT_OUTS = {
    "test_notifications.py": (
        "module-level mark; exercises each sender's own internals and patches "
        "subprocess/Foundation beneath them itself"
    ),
    "test_notification_delivery_verification.py": (
        "module-level mark; drives the NSUserNotification delivery read-back "
        "against its own fake Foundation"
    ),
    "test_linux_support.py": (
        "class-level mark on TestNotificationsLinux only; tests _send_linux "
        "with subprocess.run patched"
    ),
}


def test_the_set_of_files_exempt_from_the_block_is_pinned():
    """A new opt-out must be added here deliberately, not inherited.

    The exemption is file-wide: any test later added to one of the files below
    is silently exempt too, and will post real notifications to whoever runs
    the suite, in a diff that does not look like a hygiene change.
    """
    tests_dir = Path(__file__).resolve().parent

    conftest = (tests_dir / "conftest.py").read_text(encoding="utf-8")
    assert _MARKER in conftest, (
        "control failed: the marker was not found in conftest.py, where it is "
        "defined -- the scan below is reading nothing and would pass empty"
    )

    found = {
        path.name
        for path in sorted(tests_dir.glob("test_*.py"))
        if _MARKER in path.read_text(encoding="utf-8")
    }

    expected = set(_KNOWN_OPT_OUTS)
    assert found == expected, (
        "the set of test files exempt from the real-notification block "
        f"changed.\n  added:   {sorted(found - expected)}\n"
        f"  removed: {sorted(expected - found)}\n"
        "Exempting a file exempts every test in it, forever. If the addition "
        "is deliberate, add it to _KNOWN_OPT_OUTS with a one-line reason and "
        "confirm the file mocks the OS boundary beneath the sender itself.\n"
        "Currently allowed:\n"
        + "\n".join(f"  - {k}: {v}" for k, v in sorted(_KNOWN_OPT_OUTS.items()))
    )
