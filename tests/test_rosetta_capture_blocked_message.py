"""#188: the persistent surface named the wrong component for 90 minutes.

On a clean Apple Silicon Mac with no Rosetta 2, every bundled x86_64 tracker
fails to spawn with ``[Errno 86] Bad CPU type in executable``. Capture never
starts, so the ActivityWatch watchdog escalates and the tray goes ERROR — and
says **"ActivityWatch not responding"**.

That sentence is true and useless. It names a component the user has never heard
of, on a machine that is recording zero seconds, when the one thing that would
have fixed it fits on a line:

    softwareupdate --install-rosetta

Carmen Lapusan lost ~90 minutes to that on 2026-08-13 (agent v1.5.122), and
Laszlo Fabian Raul's device recorded zero seconds across two days before her.
The one-shot toast that *did* say the right thing fires once per process, during
the noisiest moment of a new laptop's first launch, and ``clear_notifications()``
wipes it again when the app quits. The tray is the surface that persists for as
long as the fault does, so the tray is where the remedy has to be.

**This is a wording/routing fix on an ALREADY-FIRING surface, not new UI.** The
tray was red for the whole 90 minutes; it was pointed at the wrong cause.

Scope note, stated plainly: the Rosetta state itself is INJECTED in every test
here. This machine has Rosetta installed (``arch -x86_64 /usr/bin/true`` exits
0) and Rosetta cannot be uninstalled, so the genuine zero-capture state is not
reproducible on this hardware or on the ubuntu PR runner. What is verified here
is that the message the surface carries is derived from the blocked state and
names the remedy; that a real Apple Silicon Mac without Rosetta reaches this
branch is inherited from ``_rosetta_missing()``, which is unchanged and has its
own tests in ``test_aw_manager_rosetta_preflight.py``.
"""

import re
from unittest.mock import MagicMock

import pytest

from src.aw_manager import AWManager
from src.main import SyncCoordinator

# The command is the entire point of the change. A message that says "Rosetta"
# without it leaves the user exactly where Carmen was: knowing a word, not a fix.
_INSTALL_COMMAND = "softwareupdate --install-rosetta"


def _names_the_remedy(text: str) -> bool:
    """True only when the text identifies the fault AND gives the command.

    Whole words over substrings, and BOTH halves required, for the reason
    ``tests/remedy_wording.py`` documents: a naive membership check is satisfied
    by prose that happens to contain the token and carries no remedy at all.
    """
    low = text.lower()
    names_cause = re.search(r"\brosetta\b", low) is not None
    gives_command = _INSTALL_COMMAND in low
    return names_cause and gives_command


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def coord():
    c = SyncCoordinator.__new__(SyncCoordinator)
    c._aw_unreachable_streak = 0
    c._aw_unreachable_since = None
    c._AW_UNREACHABLE_ERROR_THRESHOLD = 2
    c._AW_UNREACHABLE_ESCALATE_SECONDS = 180.0
    c._aw_buckets_failed_streak = 0
    c.error_reporter = None
    c.aw_manager = MagicMock()
    c.aw_manager.is_managing = False
    c.tray = MagicMock()
    return c


def _tray_message(coord) -> str:
    """The string the tray was actually handed. set_state(state, message)."""
    assert coord.tray.set_state.call_args is not None, "the tray was never told anything"
    return coord.tray.set_state.call_args[0][1]


def _escalate(coord):
    clock = _Clock()
    coord._note_aw_unreachable(now=clock())
    clock.advance(181)
    coord._escalate_aw_unreachable(now=clock())


# ── The producer: aw_manager knows which of the three causes it is ──────


def _manager_with_rosetta_memo(missing, capture_dead=True):
    """A real AWManager carrying only the two fields the remedy may read.

    ``__new__`` rather than a constructed manager: this method must read these
    two and nothing else, so a fixture that cannot supply anything else is the
    strongest available statement of that.

    ``capture_dead`` is ``tracker_download_failed`` — the manager's own latch
    for "this device is capturing nothing". It defaults True because that is the
    state every caller of this helper was implicitly assuming before the flag
    was part of the condition at all.

    ``force_restart`` is the one method stubbed, for the escalation tests that
    hand this object to a coordinator: it tears down process trees and now
    re-probes Rosetta, which is a separate mechanism with its own tests. Left
    real it would raise on the absent lock, be swallowed by the caller's
    try/except, and quietly make this a test of the error path instead.
    """
    m = AWManager.__new__(AWManager)
    m._rosetta_missing_cached = missing
    m.tracker_download_failed = capture_dead
    m.force_restart = MagicMock(return_value=False)
    return m


def test_the_manager_reports_the_remedy_when_rosetta_is_the_established_cause():
    remedy = _manager_with_rosetta_memo(True).capture_blocked_remedy()

    assert remedy is not None, "the one cause we can name must be named"
    assert _names_the_remedy(remedy), remedy


def test_the_manager_withholds_a_remedy_from_a_device_that_is_recording():
    """A missing Rosetta is not the same fact as a dead capture.

    A user who hit the "nothing is being tracked" wall and installed a native
    arm64 ActivityWatch themselves keeps the memo at True forever while
    recording perfectly well. Gating on the memo alone put "Not recording —
    Rosetta 2 required" in front of that person on any unrelated outage, about a
    component with nothing to do with it. ``tracker_download_failed`` is the
    manager's own answer to "is this device capturing nothing", and
    ``_start_locked`` deliberately leaves it False when an external server holds
    the port.
    """
    assert _manager_with_rosetta_memo(True, capture_dead=False).capture_blocked_remedy() is None


def test_the_manager_withholds_a_remedy_when_rosetta_is_fine():
    """The control. A manager that has established Rosetta IS present must not
    volunteer a Rosetta remedy — otherwise every unrelated ActivityWatch outage
    on every Mac starts telling people to install something they already have."""
    assert _manager_with_rosetta_memo(False).capture_blocked_remedy() is None


def test_the_manager_withholds_a_remedy_when_the_probe_never_ran():
    """``None`` is the un-probed memo, and is NOT False. Claiming a remedy from
    a state nobody established would be a confident guess on the one surface
    that exists to stop guessing."""
    assert _manager_with_rosetta_memo(None).capture_blocked_remedy() is None


def test_the_remedy_never_forks_a_subprocess(monkeypatch):
    """``capture_blocked_remedy()`` is read while building the tray message, on
    the sync cycle and under the menu lock. ``_rosetta_missing()`` forks
    ``/usr/bin/arch``; this must read the MEMO it leaves behind and never run a
    probe of its own. The tray's own siblings (`_arch_menu_item`,
    `serial_menu_row`) carry the same rule in their docstrings."""
    import subprocess

    def explode(*a, **k):
        raise AssertionError("capture_blocked_remedy forked a subprocess")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(subprocess, "check_output", explode)

    assert _manager_with_rosetta_memo(True).capture_blocked_remedy() is not None


# ── The consumer: the tray message the user actually sees ───────────────
#
# Witnessed separately from the producer. A producer with no consumer is
# Phantom 7 — every assertion above passes while the tray still says
# "ActivityWatch not responding", which is exactly the shipped defect.


def test_the_tray_names_rosetta_when_that_is_why_capture_is_dead(coord):
    """THE defect. 90 minutes of a red tray pointing at the wrong component."""
    coord.aw_manager.capture_blocked_remedy.return_value = (
        "Not recording — Rosetta 2 required. Open Terminal and run: "
        f"{_INSTALL_COMMAND}"
    )

    _escalate(coord)

    assert _names_the_remedy(_tray_message(coord)), _tray_message(coord)


def test_the_bucket_failure_path_says_the_same_thing(coord):
    """The SECOND call site. Both escalations set the identical literal today,
    so a fix applied to one leaves a device that reaches the other still reading
    "ActivityWatch not responding" — the same outage, the same wrong sentence.
    Mutation: revert either call site alone and exactly one of these two reddens.

    Driven by a REAL manager rather than a mocked return value. With a mock this
    test asserted only that whatever the manager said reached the tray, which is
    true of a manager answering wrongly — it pinned "a bucket failure renders the
    Rosetta remedy" as correct, unconditionally, which is exactly the defect its
    sibling below now covers. The manager here is in the state the sentence
    actually describes: Rosetta missing AND nothing being captured.
    """
    coord.aw_manager = _manager_with_rosetta_memo(True)

    for _ in range(coord._AW_UNREACHABLE_ERROR_THRESHOLD):
        coord._handle_aw_bucket_failure()

    assert _names_the_remedy(_tray_message(coord)), _tray_message(coord)


def test_a_bucket_failure_on_a_RECORDING_device_names_no_remedy(coord):
    """The case the mocked version of the test above could not express.

    Same escalation, same missing Rosetta, but an external native-arm64 server
    is capturing — so ``tracker_download_failed`` is False and the outage is
    something else entirely. The tray must fall back to the generic sentence
    rather than confidently blaming a component that is not the cause.
    """
    coord.aw_manager = _manager_with_rosetta_memo(True, capture_dead=False)

    for _ in range(coord._AW_UNREACHABLE_ERROR_THRESHOLD):
        coord._handle_aw_bucket_failure()

    message = _tray_message(coord)
    assert message == "ActivityWatch not responding"
    assert "rosetta" not in message.lower()


def test_an_ordinary_outage_still_reads_as_before(coord):
    """The control, and it must pass BOTH pre- and post-fix.

    Every Mac with Rosetta, every Windows box and every Linux box reaches this
    branch. If the Rosetta wording leaked into it, this change would have
    replaced one wrong sentence with a differently wrong one on the whole fleet.
    """
    coord.aw_manager.capture_blocked_remedy.return_value = None

    _escalate(coord)

    message = _tray_message(coord)
    assert message == "ActivityWatch not responding"
    assert "rosetta" not in message.lower()


def test_a_broken_manager_never_costs_the_escalation(coord):
    """A manager that raises must not swallow the tray escalation — that would
    hide the very fault it was trying to report, the rule the sibling
    force_restart handler already follows."""
    coord.aw_manager.capture_blocked_remedy.side_effect = RuntimeError("boom")

    _escalate(coord)

    coord.tray.set_state.assert_called_once()
    assert _tray_message(coord) == "ActivityWatch not responding"


def test_a_nonsense_remedy_never_reaches_the_user(coord):
    """The message is rendered into the tray verbatim. A non-string (or an empty
    one) must fall back rather than putting a repr in front of the user — and
    every existing escalation test hands this coordinator a bare MagicMock,
    which is truthy, so a plain `remedy or fallback` would ship `<MagicMock ...>`
    as the fault message."""
    coord.aw_manager.capture_blocked_remedy.return_value = MagicMock()

    _escalate(coord)

    assert _tray_message(coord) == "ActivityWatch not responding"


def test_an_empty_remedy_falls_back(coord):
    coord.aw_manager.capture_blocked_remedy.return_value = "   "

    _escalate(coord)

    assert _tray_message(coord) == "ActivityWatch not responding"
