"""A held socket is not a capture. One rule, every attach point (#215 class).

`_server_responding()` was extracted by #213 "so the two callers cannot drift".
The probe did not drift; the CARVE-OUTS around it did. "Is an external server
actually capturing on our port" ended up written at four attach points in
_start_locked plus the recovery predicate in restart_if_needed, and the copies
disagree:

    :1243  Rosetta attach          asks /info, and RE-LATCHES when it goes
    :1320  backoff attach (#223)   asks /info, and does NOT re-latch   <- F-1
    :1354  download-failure attach bare TCP connect                    <- F-2
    :1373  normal-path attach      bare TCP connect                    <- F-3
    :1702  external-vanished       bare TCP connect                    <- F-4

Every defect below is one of those copies missing a half the others have.

F-1 and F-2 make a device that is capturing NOTHING publish the same flag pair
as one that is recording, so the fleet's no-capture alert reads it as healthy.
F-3 is the widest: it needs no download failure and no Rosetta at all, just a
port held by something that does not answer, and it makes a HEALTHY machine
attach to that corpse instead of starting its own trackers. F-4 is why F-3 is
permanent -- the "external server vanished" recovery keys on the port being
RELEASED, and a corpse holds it forever.
"""

import time
from unittest.mock import MagicMock

import src.aw_manager as A
from src.aw_manager import AWManager


def _mgr(*, binaries, port_held, answers, backed_off=False):
    m = AWManager()
    m._capture_suppressed = False
    m._get_binaries_dir = MagicMock(return_value=binaries)
    m._rosetta_required = MagicMock(return_value=False)
    m._port_in_use = MagicMock(return_value=port_held)
    m._server_responding = MagicMock(return_value=answers)
    m._dispatch_download_failure_report = MagicMock()
    m._start_component = MagicMock(return_value=True)
    m._wait_for_server = MagicMock(return_value=True)
    if backed_off:
        m._last_download_attempt = time.monotonic()
        m._download_retry_interval = 3600.0
    return m


class TestF1BackoffAttachReLatches:
    """#230 cleared the capture-dead flag on attach and copied none of the
    re-latch the Rosetta sibling forty lines up has. The clear survives into
    later cycles, so when the external server dies the device keeps publishing
    healthy -- for up to DOWNLOAD_RETRY_MAX_INTERVAL, an hour, per cycle."""

    def test_flag_re_latches_when_the_external_server_dies(self):
        m = _mgr(binaries=None, port_held=True, answers=True, backed_off=True)
        assert m._start_locked() is True
        assert m.tracker_download_failed is False, "precondition: attached and cleared"

        # Same manager, next 60s cycle, still inside backoff. The server died.
        m._port_in_use = MagicMock(return_value=False)
        m._server_responding = MagicMock(return_value=False)
        m._last_download_attempt = time.monotonic()

        assert m._start_locked() is False
        assert m.tracker_download_failed is True, (
            "a device capturing nothing is publishing tracker_download_failed="
            "False, which the fleet's no-capture alert reads as healthy"
        )
        assert m._using_external is False, "still claiming an external server"

    def test_a_held_but_dead_port_does_not_clear_the_flag(self):
        m = _mgr(binaries=None, port_held=True, answers=False, backed_off=True)
        m.tracker_download_failed = True
        # The RETURN matters as much as the flag, and leaving it unasserted let
        # a mutant survive the first cut of this branch: restoring the pre-fix
        # `return server_already_running` here reports a device as *started*
        # while a corpse holds the port and nothing is capturing.
        assert m._start_locked() is False
        assert m.tracker_download_failed is True

    def test_a_live_external_server_still_clears_it(self):
        """Control: #230's actual fix must survive. A recording Mac must not
        report capture-dead."""
        m = _mgr(binaries=None, port_held=True, answers=True, backed_off=True)
        m.tracker_download_failed = True
        assert m._start_locked() is True
        assert m.tracker_download_failed is False
        assert m._using_external is True


class TestF2DownloadFailureAttachAsksTheServer:
    """#233 said "the last two sites" and there was a third."""

    def test_a_held_but_dead_port_is_not_treated_as_capturing(self):
        m = _mgr(binaries=None, port_held=True, answers=False)
        orig = A._download_aw_binaries
        A._download_aw_binaries = lambda *a, **k: False
        try:
            started = m._start_locked()
        finally:
            A._download_aw_binaries = orig

        assert m._server_responding.called, "the socket was trusted without asking"
        assert started is False
        assert m._using_external is False
        assert m.tracker_download_failed is True

    def test_a_live_external_server_is_still_attached_to(self):
        """Control: the legitimate case #233's branch exists for."""
        m = _mgr(binaries=None, port_held=True, answers=True)
        orig = A._download_aw_binaries
        A._download_aw_binaries = lambda *a, **k: False
        try:
            assert m._start_locked() is True
        finally:
            A._download_aw_binaries = orig
        assert m._using_external is True
        assert m.tracker_download_failed is False


class TestF3NormalPathAttachAsksTheServer:
    """The widest of the four: no download failure, no Rosetta, binaries fine.
    A port held by a corpse made a healthy machine attach to it and run its
    watchers into a server that answers nothing."""

    def test_a_dead_holder_does_not_win_over_our_own_trackers(self):
        m = _mgr(binaries="/tmp/fake-binaries", port_held=True, answers=False)
        m._start_locked()

        assert m._server_responding.called, "the socket was trusted without asking"
        assert m._using_external is False, (
            "attached to a process that does not answer /api/0/info; the "
            "watchers now report into nothing"
        )

    def test_a_live_external_server_is_still_used(self):
        """Control: users who run their own ActivityWatch must keep working.
        Attaching is correct here and starting a competing server is not."""
        m = _mgr(binaries="/tmp/fake-binaries", port_held=True, answers=True)
        assert m._start_locked() is True
        assert m._using_external is True
        started = [c.args[0] for c in m._start_component.call_args_list]
        assert A.BF_SERVER not in started, "started a second server beside a live one"

    def test_a_free_port_is_unchanged(self):
        """Control: the ordinary path must not start asking about a port
        nobody holds."""
        m = _mgr(binaries="/tmp/fake-binaries", port_held=False, answers=False)
        assert m._start_locked() is True
        assert m._using_external is False
        started = [c.args[0] for c in m._start_component.call_args_list]
        assert A.BF_SERVER in started


class TestF4RecoveryStillReadsTheBarePort_Deliberately:
    """F-4 is DEFERRED, and this pins the deferral so nobody flips it back.

    Making the external-vanished recovery ask /api/0/info is the obvious fourth
    member of this class, and it was written, measured and reverted. On a
    HEALTHY shared ActivityWatch that missed two answers it dropped external
    mode, spawned our own server against the port that healthy server still
    owned, failed to bind, and cleared every watcher -- after which is_managing
    reads False, main.py stops calling restart_if_needed, and nothing ever
    re-enters the branch. Permanent, on a healthy machine, both capture-dead
    flags reading False. Control that reverted only that predicate:

        asked /info   rc=False  _using_external=False  watchers=[]
        bare port     rc=True   _using_external=True   watchers=[2]

    So it needs a debounce -- N consecutive non-answers, resetting on any
    success -- which is its own change with its own evidence. Until then a
    corpse holding the port keeps external mode, and the device recovers on the
    next app start via the normal-path attach (F-3), which no longer defers to
    a corpse in the first place.
    """

    def test_a_corpse_holding_the_port_does_NOT_trigger_recovery_yet(self):
        m = _mgr(binaries="/tmp/fake-binaries", port_held=True, answers=False)
        m._using_external = True
        m._processes = {}
        m._start_locked = MagicMock(return_value=True)

        m.restart_if_needed()
        assert not m._start_locked.called, (
            "recovery fired on a single unanswered ask -- that is the reverted "
            "behaviour, and it destroys watchers on a healthy machine that "
            "merely blipped. It needs a debounce first."
        )

    def test_recovery_still_fires_when_the_port_is_actually_released(self):
        """The case this branch has always handled, and must keep handling."""
        m = _mgr(binaries="/tmp/fake-binaries", port_held=False, answers=False)
        m._using_external = True
        m._processes = {}
        m._start_locked = MagicMock(return_value=True)

        assert m.restart_if_needed() is True
        assert m._start_locked.called

    def test_recovery_does_not_fire_on_a_live_external_server(self):
        """Control: a healthy shared server must not be torn down."""
        m = _mgr(binaries="/tmp/fake-binaries", port_held=True, answers=True)
        m._using_external = True
        m._processes = {}
        m._start_locked = MagicMock(return_value=True)

        m.restart_if_needed()
        assert not m._start_locked.called


class TestABlipMustNotDestroyOurWatchers:
    """C-1, found by the pre-merge gate refuting the first cut of this branch.

    The first version made the external-vanished recovery ask /api/0/info. On a
    HEALTHY shared ActivityWatch that missed two answers -- a load spike, a wake
    from sleep, a long AW query -- it dropped external mode, spawned our own
    server against the port that healthy server still owned, failed to bind, and
    `_start_locked`'s `self.stop()` cleared every watcher. `is_managing` then
    reads False, so main.py's 60s tick stops calling restart_if_needed at all
    and nothing re-enters the branch. Permanent, on a healthy machine, with both
    capture-dead flags reading False.

    Measured against a control that reverted only that one predicate:

        post-fix  rc=False  _using_external=False  watchers=[]      is_managing=False
        control   rc=True   _using_external=True   watchers=[2]     is_managing=True

    So the rule this file exists for cuts both ways: a held socket is not a
    capture, AND a single unanswered ask is not a vanished server.
    """

    def _blipping(self, answers):
        m = _mgr(binaries="/tmp/bin", port_held=True, answers=True)
        seq = list(answers)
        m._server_responding = MagicMock(
            side_effect=lambda: seq.pop(0) if seq else True
        )
        m._wait_for_server = MagicMock(return_value=False)  # cannot bind: port taken
        m._using_external = True
        m._processes = {
            "bf-window-tracker": MagicMock(**{"poll.return_value": None}),
            "bf-idle-tracker": MagicMock(**{"poll.return_value": None}),
        }
        m.check_health = MagicMock(return_value=True)
        m._get_latest_afk_event_age = MagicMock(return_value=5)
        m._get_latest_window_event_age = MagicMock(return_value=5)
        return m

    def test_a_failed_bind_against_a_held_port_keeps_our_watchers(self):
        """The holder is still there, so the bind failing is evidence ABOUT the
        holder, never a reason to tear down watchers that are working."""
        m = self._blipping([False, False])
        m._start_locked()

        assert sorted(m._processes) == ["bf-idle-tracker", "bf-window-tracker"], (
            "our watchers were destroyed because a server we should never have "
            "started could not bind a port somebody else owns"
        )

    def test_the_device_stays_supervised_after_a_blip(self):
        m = self._blipping([False, False])
        m.restart_if_needed()

        assert m.is_managing, (
            "is_managing went False, so main.py's 60s tick stops calling "
            "restart_if_needed and nothing can ever re-enter recovery"
        )

    def test_a_failed_bind_on_a_FREE_port_still_tears_down(self):
        """Control: the ordinary failure. Nobody holds the port, our server just
        did not come up, and the pre-existing cleanup must still run."""
        m = _mgr(binaries="/tmp/bin", port_held=False, answers=False)
        m._wait_for_server = MagicMock(return_value=False)
        m._processes = {"bf-idle-tracker": MagicMock(**{"poll.return_value": None})}

        assert m._start_locked() is False
        assert m._processes == {}, "the ordinary teardown must be untouched"
