"""A backed-off download must not report a recording Mac as capture-dead (#223).

When the tracker download has failed and the retry is in backoff, the branch
that handles it says in its own comment: "Report the same outcome the last
real attempt did: an attached external server still captures, nothing else
does." It did not carry that out -- it returned the bare port boolean and left
``tracker_download_failed`` latched and ``_using_external`` False.

The window is not small. The retry interval escalates to an hour, so a stuck
device spends nearly every 60-second cycle on this path. Support sees a device
flagged capture-dead that is recording fine, and the longer that runs the more
the ops ingest learns to ignore the flag -- which is the signal that exists to
catch genuinely silent machines.
"""

import time
from unittest.mock import MagicMock

from src.aw_manager import AWManager


def _backed_off_manager(*, port_held: bool, server_answers: bool) -> AWManager:
    """A manager parked in the exact state the issue describes: download has
    failed, no binaries on disk, retry still inside its backoff window."""
    mgr = AWManager()
    mgr._capture_suppressed = False
    mgr._get_binaries_dir = MagicMock(return_value=None)   # nothing downloaded
    mgr._rosetta_required = MagicMock(return_value=False)  # not the Rosetta path
    mgr._port_in_use = MagicMock(return_value=port_held)
    mgr._server_responding = MagicMock(return_value=server_answers)
    mgr.tracker_download_failed = True                     # latched by a real failure
    mgr._using_external = False
    mgr._last_download_attempt = time.monotonic()          # just tried
    mgr._download_retry_interval = 3600.0                  # escalated to an hour
    return mgr


class TestBackoffWithACapturingServer:
    """Something IS capturing on our port. The device is not capture-dead."""

    def test_clears_the_capture_dead_flag(self):
        mgr = _backed_off_manager(port_held=True, server_answers=True)
        result = mgr._start_locked()

        # Precondition: we really are on the backoff path, not some other
        # early return that happens to produce the same answer.
        assert mgr._server_responding.called, (
            "fixture never reached the backoff attach branch"
        )
        assert result is True
        assert mgr.tracker_download_failed is False, (
            "device is recording via an external server but still reports "
            "capture-dead to the fleet -- #223"
        )

    def test_records_that_it_attached_to_a_server_it_did_not_start(self):
        mgr = _backed_off_manager(port_held=True, server_answers=True)
        mgr._start_locked()
        assert mgr._using_external is True, (
            "_using_external must reflect the attach, or the watchdog "
            "supervises the wrong thing"
        )


class TestBackoffWithoutACapturingServer:
    """The discriminating half. A naive 'always clear' fix passes the tests
    above and reports a silent machine as healthy -- which is strictly worse
    than the bug, because this flag exists to catch exactly that device."""

    def test_a_held_but_dead_port_does_not_clear_the_flag(self):
        # Port answers a TCP connect but nothing answers /api/0/info: a
        # process holds the socket and captures nothing.
        mgr = _backed_off_manager(port_held=True, server_answers=False)
        mgr._start_locked()
        assert mgr.tracker_download_failed is True, (
            "a held-but-dead port must NOT be read as a capture -- clearing "
            "here reports a silent device healthy"
        )
        assert mgr._using_external is False

    def test_nothing_on_the_port_does_not_clear_the_flag(self):
        mgr = _backed_off_manager(port_held=False, server_answers=False)
        result = mgr._start_locked()
        assert result is False
        assert mgr.tracker_download_failed is True
        assert mgr._using_external is False

    def test_the_clear_is_gated_on_the_http_ask_not_the_tcp_connect(self):
        """Pins WHICH probe decides. _port_in_use is a bare TCP connect and is
        routinely true for a dead process; _server_responding is the /api/0/info
        answer. Swapping them is invisible in every test above except this one."""
        mgr = _backed_off_manager(port_held=True, server_answers=False)
        mgr._start_locked()
        assert mgr._port_in_use.called
        assert mgr._server_responding.called, (
            "the branch must pay for the HTTP ask before granting an attach"
        )
        assert mgr.tracker_download_failed is True
