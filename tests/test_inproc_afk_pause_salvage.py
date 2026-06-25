"""In-process AFK: salvage the observed active tail across a long pause/sleep.

Regression for the "~10-20 min idle/empty gap hugging every pause" report
(Ecaterina Cocora / Matei Cocora device 44, 1.5.74, 2026-06-25).

When the machine sleeps for longer than the sample-retention window the
checkpoint freezes past retention and the cycle re-seeds. The old behaviour
discarded the WHOLE window, including the genuine active work the agent had
already observed in the final moments before the machine slept — so the
server had no not-afk evidence there and rendered it as an idle/empty gap.

These tests exercise the real AfkSource + SyncEngine (no phantom mocks):
the pre-pause active samples must survive the wake-cycle prune and be emitted
as not-afk spans, while the genuinely-unobserved sleep window stays uncovered.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from src.config import Config
from src.sync.activity_analyzer import ActivityAnalyzer
from src.sync.afk_source import AfkSource
from src.sync.daily_time_tracker import DailyTimeTracker
from src.sync.sync_engine import SyncEngine

T0 = datetime(2026, 6, 25, 9, 0, 0, tzinfo=timezone.utc)


def _engine(retention=7200.0, idle=5.0):
    cfg = Config()
    cfg.sync.in_process_afk = True
    eng = SyncEngine(aw=Mock(), bf=Mock(), queue=Mock(), config=cfg,
                     activity_analyzer=Mock(spec=ActivityAnalyzer),
                     time_tracker=Mock(spec=DailyTimeTracker))
    eng.afk_source = AfkSource(600, "host", idle_clock=lambda: idle,
                               retention_seconds=retention)
    return eng


def test_long_pause_reseed_salvages_observed_active_tail():
    """A 5h sleep (> 2h retention) must NOT drop the active work observed in
    the final minutes before the machine slept."""
    eng = _engine(retention=7200.0)
    pause_start = T0 - timedelta(hours=5)
    # The last cycle finalized here, just before a final active burst + sleep.
    eng._afk_inproc_checkpoint = pause_start - timedelta(seconds=5)

    # Dense active samples in the last couple of minutes before the lid closed.
    for off in (0, 60, 120):
        eng.afk_source.record_sample(
            pause_start + timedelta(seconds=off),
            protect_since=eng._afk_inproc_checkpoint,
        )
    # 5h sleep; the machine wakes and the cycle records one fresh sample. The
    # wake-cycle prune must keep the protected pre-pause samples.
    eng.afk_source.record_sample(T0, protect_since=eng._afk_inproc_checkpoint)

    events = eng._build_inproc_afk(T0)

    notafk = [e for e in events if e["data"]["status"] == "not-afk"]
    assert notafk, "observed pre-pause active tail was dropped (the idle-gap bug)"
    # The checkpoint still re-seeds to now: the unobserved sleep window is left
    # uncovered, never billed as active.
    assert eng._afk_inproc_checkpoint == T0
    # No span may extend into the unobserved sleep window as not-afk.
    for e in notafk:
        assert datetime.fromisoformat(e["timestamp"]) < pause_start + timedelta(minutes=10)


def test_long_pause_with_no_observed_tail_emits_nothing():
    """If nothing was observed before the pause (only the wake sample), the
    re-seed still emits nothing — we never invent activity over a blank window.

    NOTE: unlike the salvage test above, this one also passes on PRE-fix code
    (the old re-seed also returned []). It is not a regression test — it guards
    that the salvage path does not OVER-bill when there is no observed tail.
    """
    eng = _engine(retention=7200.0)
    eng._afk_inproc_checkpoint = T0 - timedelta(hours=5)
    eng.afk_source.record_sample(T0, protect_since=eng._afk_inproc_checkpoint)

    events = eng._build_inproc_afk(T0)

    assert events == [], "must not reconstruct an unobserved multi-hour window"
    assert eng._afk_inproc_checkpoint == T0


def test_protect_since_respects_maxlen_backstop():
    """`protect_since` keeps samples past retention, but the deque's maxlen is an
    absolute memory cap — a frozen checkpoint can't grow it without bound."""
    src = AfkSource(600, "host", idle_clock=lambda: 5.0,
                    retention_seconds=7200.0, max_samples=100)
    frozen_cp = T0  # checkpoint never advances (e.g. continuous send failure)
    for i in range(500):
        src.record_sample(T0 + timedelta(seconds=i), protect_since=frozen_cp)
    assert len(src.samples) <= 100
