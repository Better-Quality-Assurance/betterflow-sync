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


class TestF3NormalPathStillTrustsTheSocket_Deferred:
    """F-3 is a DECIDED DESIGN, and this pins it so nobody flips the line.

    The normal-path attach is the widest member of this class -- no download
    failure, no Rosetta, just a port held by something that answers nothing --
    and attaching to it anyway is deliberate: a foreign holder is unreapable
    (_reap_orphan_processes is path-scoped to our own binaries), so attaching
    is what keeps the watchers alive and lets the device recover the moment
    the holder dies. The device now REPORTS the condition instead of hiding
    it, via _external_server_not_responding on the heartbeat.

    Making it ask was written, measured and reverted TWICE. The question is not
    the problem; what you can DO with a "no" is. The only answer available is to
    start our own server, which cannot bind a port somebody else owns, and
    `_wait_for_server` failing then runs a blanket `self.stop()`. Driven through
    main.py's real tick order against a corpse that releases the port at tick 4:

        as-is (this code)      watchers=2 throughout, recovers on release
        ask + blanket stop()   watchers=0
        ask + skip the stop()  watchers=0, AND a dead bf-data-service left in
                               _processes, which disarms
                               set_capture_suppressed's
                               `elif not self._processes` rebuild route (:752)

    That measurement drove set_capture_suppressed and restart_if_needed only.
    force_restart() is a SECOND route into _start_locked, fired by main.py's
    unreachable watchdog after 180s, so both variants cost a BOUNDED outage
    rather than a permanent one. Still worse than this code, which never loses
    the watchers -- but the first draft of this docstring said "permanently"
    and that was not measured.

    Both repairs were worse than this design. Change the reporting if it is
    wrong; do not change this line.
    """

    def test_a_dead_holder_is_still_attached_to_for_now(self):
        m = _mgr(binaries="/tmp/fake-binaries", port_held=True, answers=False)
        m._start_locked()

        assert m._using_external is True, (
            "this line was flipped without moving the rebuild route -- read the "
            "class docstring: both repairs left the device with zero watchers "
            "permanently, which is worse than the bug being fixed"
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
    """F-4 is a DECIDED DESIGN, and this pins it so nobody flips it back.

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

    So it still needs a debounce -- N consecutive non-answers, resetting on
    any success -- which is its own change with its own evidence; that is
    unchanged by the reporting fix in Tasks 1-2, because THIS predicate never
    writes _external_server_not_responding. That field is F-3's: set only in
    _start_locked's normal-path attach branch, and re-derived fresh on every
    _start_locked call. Until a debounced ask lands, a corpse holding the port
    keeps external mode here, and the device recovers on the next app start
    via the normal-path attach (F-3), which no longer defers to a corpse in
    the first place.
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


class TestTheTeardownOnAFailedServerStartIsWitnessed:
    """The blanket `self.stop()` after `_wait_for_server()` fails had NO test.

    Round 3 mutation: replacing it with `pass`, and flipping its `return False`
    to `return True`, both survived the whole 2111-test suite. Every
    `_wait_for_server` stub in the repo returns True, so nothing entered that
    branch -- and the change that annotated it as load-bearing added eight lines
    of comment and no witness. A comment about a trap does not stop the trap.

    It IS load-bearing: emptying `_processes` re-arms set_capture_suppressed's
    `elif not self._processes` rebuild route (aw_manager.py:752), and the repair
    that skipped it to preserve watchers is exactly what round 2 measured as a
    regression.
    """

    def _failing_start(self, *, port_held):
        m = _mgr(binaries="/tmp/bin", port_held=port_held, answers=False)
        m._wait_for_server = MagicMock(return_value=False)  # server never binds
        m._processes = {"bf-idle-tracker": MagicMock(**{"poll.return_value": None})}
        return m

    def test_a_failed_server_start_empties_processes(self):
        m = self._failing_start(port_held=False)
        assert m._start_locked() is False, "a failed server start must not report success"
        assert m._processes == {}, (
            "_processes still holds entries, so set_capture_suppressed's "
            "`elif not self._processes` rebuild route is disarmed and nothing "
            "re-enters _start_locked on the normal tick"
        )

    def test_the_rebuild_route_is_re_armed_afterwards(self):
        """Assert the CONSEQUENCE, not just the field: the next tick must be
        able to call _start_locked again."""
        m = self._failing_start(port_held=False)
        m._start_locked()

        reached = []
        m._start_locked = lambda: reached.append(1)
        m.set_capture_suppressed(False, "next tick")
        assert reached, "the rebuild route did not fire, so the device is wedged"
