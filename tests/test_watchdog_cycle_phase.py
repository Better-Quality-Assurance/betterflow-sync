"""Which stage was _do_sync in when the watchdog fired.

Coarse by design: the phase is stamped in _do_sync only, so most real overruns
will report 'sync' because sync_engine.sync() is where the retry chain and the
send budget live. That still rules OUT the other four stages, at the cost of
one file instead of instrumenting the ~1000-line sync_engine.sync() on the
hottest path in the repo.

The phase lives in a per-cycle box rather than on the coordinator: a cycle
abandoned by _acquire_sync_slot's takeover keeps running, and an instance
attribute would let that zombie overwrite its successor's phase.
"""

import time
from unittest.mock import Mock

from src.main import _CyclePhase

from ._watchdog_harness import CoordinatorHarness, _ok_stats

_OVERRUN = 1.2


class TestCyclePhase(CoordinatorHarness):
    def test_a_fresh_box_starts_at_startup(self):
        assert _CyclePhase().name == "startup"

    def test_overrun_inside_sync_reports_phase_sync(self):
        def _slow_sync(*_a, **_k):
            time.sleep(_OVERRUN)
            return _ok_stats()

        self.sync_engine.sync.side_effect = _slow_sync
        self.coord._do_sync()

        hung = self.recorder.by_fingerprint("sync-watchdog-timeout")
        assert len(hung) == 1, self.recorder.captures
        assert hung[0]["context"]["phase"] == "sync"

    def test_overrun_inside_capture_health_reports_phase_capture_health(self):
        """The discriminating case: if the stamp were only ever set to 'sync',
        the test above would pass and this one would not."""

        def _slow_health():
            time.sleep(_OVERRUN)
            return True

        self.coord._monitor_capture_health = Mock(side_effect=_slow_health)
        self.coord._do_sync()

        hung = self.recorder.by_fingerprint("sync-watchdog-timeout")
        assert len(hung) == 1, self.recorder.captures
        assert hung[0]["context"]["phase"] == "capture_health"

    def test_existing_fire_time_report_keeps_its_identity(self):
        """Global constraint: adding context must not move the fingerprint,
        level or message of the group that already holds 34 occurrences."""

        def _slow_sync(*_a, **_k):
            time.sleep(_OVERRUN)
            return _ok_stats()

        self.sync_engine.sync.side_effect = _slow_sync
        self.coord._do_sync()

        hung = self.recorder.by_fingerprint("sync-watchdog-timeout")
        assert len(hung) == 1
        assert hung[0]["level"] == "error"
        assert "Sync hung" in hung[0]["message"]
        assert hung[0]["tags"] == {"component": "sync-watchdog"}
