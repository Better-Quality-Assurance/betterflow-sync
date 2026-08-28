"""A listener on port 5600 is not a capturing tracker server. Issue #215.

Two sites decided "an external server is healthy" from ``_port_in_use()`` — a
bare TCP connect. A process that holds the port and no longer answers HTTP
satisfies that, so the agent reported capture as healthy on a device recording
nothing, and the fleet was told everything was fine. PR 213 fixed the third
instance of the same conflation and extracted ``_server_responding()``.

WHY THESE FIXTURES LOOK ODD, and it is the point. Issue #215 records that
``_port_in_use`` is supplied by fixtures at six existing test sites, and that at
every one the surrounding prose calls it "external server healthy" or
"capturing" — a claim a TCP connect cannot support. Every one of those supplies
the boolean production computes, so **no test in the suite could distinguish a
serving ActivityWatch from a dead socket**, and fixing the code without changing
how it is tested would have left that gap intact.

So every case below pins the two probes to DIFFERENT answers:

    _port_in_use       True   <- something holds the socket
    _server_responding False  <- but no tracker answers /api/0/info

That divergence is the whole test. A fixture where both agree cannot fail
against the old code.
"""

import errno
from unittest.mock import patch

from src.aw_manager import AWManager


def _mgr() -> AWManager:
    mgr = AWManager(aw_port=5600)
    mgr.tracker_download_failed = False
    mgr._rosetta_missing_cached = None
    mgr._rosetta_notified = False
    return mgr


def _dead_socket():
    """The case the old code could not see: port accepts, server does not answer."""
    return (
        patch.object(AWManager, "_port_in_use", return_value=True),
        patch.object(AWManager, "_server_responding", return_value=False),
    )


def test_check_health_is_false_when_the_port_answers_but_the_server_does_not():
    mgr = _mgr()
    mgr._using_external = True

    port, server = _dead_socket()
    with port, server:
        assert mgr.check_health() is False, (
            "a bare listener was reported as a healthy external tracker"
        )


def test_check_health_is_true_when_the_server_actually_answers():
    # Control. Without it the assertion above is satisfied by a method that
    # always returns False.
    mgr = _mgr()
    mgr._using_external = True

    with patch.object(AWManager, "_port_in_use", return_value=True), \
         patch.object(AWManager, "_server_responding", return_value=True):
        assert mgr.check_health() is True


def test_exec_failure_latches_capture_dead_when_the_external_server_is_only_a_socket():
    """The EBADARCH carve-out.

    On Apple Silicon without Rosetta every managed tracker raises EBADARCH. The
    carve-out exists so a device that is genuinely recording through an external
    server is not told its tracking is dead. Against a dead socket that carve-out
    fired anyway, and the person was told nothing while recording nothing.
    """
    mgr = _mgr()
    mgr._using_external = True

    port, server = _dead_socket()
    with port, server, \
         patch("src.aw_manager._resolve_binary_path", return_value="/tmp/bf-window-watcher"), \
         patch("src.aw_manager.subprocess.Popen",
               side_effect=OSError(getattr(errno, "EBADARCH", 86), "bad CPU type")), \
         patch.object(AWManager, "_notify_rosetta_required_once"):
        started = mgr._start_component("bf-window-watcher", "/tmp")

    # Fixture precondition: _start_component returns early on a missing binary,
    # and that early return is indistinguishable from the carve-out firing.
    assert started is False, "fixture never reached the EBADARCH branch"
    assert mgr.tracker_download_failed is True, (
        "a dead socket kept the capture-dead flag off — the device reports healthy "
        "while recording nothing"
    )


def test_exec_failure_keeps_the_carve_out_when_the_server_really_answers():
    # Control for the case above: a genuinely capturing external server must
    # still suppress the capture-dead flag, or this fix would just make every
    # Rosetta-less Mac shout.
    mgr = _mgr()
    mgr._using_external = True

    with patch.object(AWManager, "_port_in_use", return_value=True), \
         patch.object(AWManager, "_server_responding", return_value=True), \
         patch("src.aw_manager._resolve_binary_path", return_value="/tmp/bf-window-watcher"), \
         patch("src.aw_manager.subprocess.Popen",
               side_effect=OSError(getattr(errno, "EBADARCH", 86), "bad CPU type")), \
         patch.object(AWManager, "_notify_rosetta_required_once"):
        mgr._start_component("bf-window-watcher", "/tmp")

    assert mgr.tracker_download_failed is False, (
        "a device recording through a real external server was told its tracking is dead"
    )
