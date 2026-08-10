"""Severity bands for a watchdog overrun.

The ingest (betterqa-bot) aggregates error reports by fingerprint and by
nothing else — `context` is overwritten newest-wins per fingerprint and the
daily digest reads only `message` + `occurrences`. So the ONLY way an elapsed
time becomes countable is by riding in the fingerprint. These bands are that
mechanism.

Bands are multiples of the deadline rather than absolute seconds: the deadline
has moved once already (120s -> 150s), and the watchdog integration tests
shrink it to sub-second, which would collapse absolute bands into one bucket.

Boundaries are tested here, exactly, because a timed cycle cannot land on 1.2x
reliably.
"""

import pytest

from src.main import _overrun_fingerprint

MARGINAL = "sync-watchdog-overrun-marginal"
MODERATE = "sync-watchdog-overrun-moderate"
SEVERE = "sync-watchdog-overrun-severe"


@pytest.mark.parametrize(
    "elapsed,expected",
    [
        # At the deadline exactly — the smallest possible overrun.
        (150.0, MARGINAL),
        (151.2, MARGINAL),
        (179.9, MARGINAL),
        # 1.2x boundary: BELONGS TO moderate, not marginal. Both ends of the
        # range get a case (diagnosis-discipline.md Rule 3).
        (180.0, MODERATE),
        (246.0, MODERATE),
        (299.9, MODERATE),
        # 2.0x boundary: belongs to severe.
        (300.0, SEVERE),
        (903.4, SEVERE),
        (5000.0, SEVERE),
    ],
)
def test_bands_at_the_production_deadline(elapsed, expected):
    assert _overrun_fingerprint(elapsed, 150.0) == expected


@pytest.mark.parametrize(
    "elapsed,expected",
    [
        (0.30, MARGINAL),
        (0.35, MARGINAL),
        (0.36, MODERATE),
        (0.45, MODERATE),
        (0.60, SEVERE),
        (0.90, SEVERE),
    ],
)
def test_bands_scale_with_a_shrunk_deadline(elapsed, expected):
    """The property that makes the integration tests possible at all."""
    assert _overrun_fingerprint(elapsed, 0.3) == expected


def test_returns_exactly_three_distinct_fingerprints():
    """A band that silently aliases another would make two counts one count."""
    produced = {
        _overrun_fingerprint(e, 150.0) for e in (150.0, 200.0, 400.0)
    }
    assert produced == {MARGINAL, MODERATE, SEVERE}


def test_nonpositive_deadline_does_not_divide_by_zero():
    """Defensive only: a zero deadline is not reachable in production, but a
    ZeroDivisionError here would escape into _do_sync's finally block and mask
    whatever real failure the cycle was already reporting."""
    assert _overrun_fingerprint(10.0, 0.0) == SEVERE
    assert _overrun_fingerprint(10.0, -1.0) == SEVERE
