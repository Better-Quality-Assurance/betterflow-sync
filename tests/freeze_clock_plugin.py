"""Opt-in pytest plugin: run the suite at a chosen wall-clock instant.

Not a conftest, and deliberately not auto-loaded — it only engages when you pass
``-p tests.freeze_clock_plugin``, so a normal run is untouched.

    PYTHONPATH=. python3 -m pytest tests/ -p tests.freeze_clock_plugin
    BF_FREEZE_AT="2026-12-31 23:59" PYTHONPATH=. python3 -m pytest tests/ \
        -p tests.freeze_clock_plugin

``BF_FREEZE_AT`` is a naive ``YYYY-MM-DD HH:MM`` read as LOCAL time (the
day boundary this exists to probe is the local one, not UTC). ``off`` disables
the freeze so the plugin can stay loaded in a control run.

Why it exists
-------------
Three releases were damaged by fixtures that pass on the developer's clock and
fail at some other hour (#182, #168/#171, #187). Sweeping for the whole class
needs a harness, and the obvious hand-rolled one is wrong in a way that is very
hard to see: freezing ``datetime`` alone leaves ``time.time()`` on the real
clock, so the two disagree by however many hours you travelled. Code that
stamps an event with ``time.time()`` and ages it with ``datetime.now()`` then
reports a multi-hour age, and the resulting failures read as day-boundary bugs.
That instrument produced 22 failures, then 8 after being narrowed, then 7 after
#189 — and all 7 were itself (#190).

``time_machine`` moves ``time.time()``, ``time.gmtime()``, ``time.localtime()``
and ``datetime`` together at the C level, which is the property a hand-rolled
patch cannot get right.

Known limit, stated rather than discovered later: under ``tick=False``
``time.monotonic()`` is NOT pinned, it continues at the real rate. That is
correct rather than a gap — monotonic has no wall-clock epoch to travel to, and
real code only ever takes deltas from it. The residual drift is bounded by one
test's own runtime, and cannot produce the multi-hour skew described above.

Witnessing it
-------------
A harness whose correct answer is "everything passes" is indistinguishable from
one that never engaged, so do not trust a green run on its own. The control is
one command: assert inside a test that ``datetime.datetime.now()`` reads the
frozen hour, and confirm that same assertion FAILS without ``-p``.
"""

import datetime as _dt
import os

import pytest

_SPEC_ENV = "BF_FREEZE_AT"
_DEFAULT_SPEC = "2026-08-18 00:01"


def _destination():
    """Resolve BF_FREEZE_AT to an aware datetime, or None when disabled."""
    spec = os.environ.get(_SPEC_ENV, _DEFAULT_SPEC)
    if spec.strip().lower() == "off":
        return None
    naive = _dt.datetime.strptime(spec.strip(), "%Y-%m-%d %H:%M")
    # astimezone() on a naive datetime attaches the machine's local offset, so
    # "00:01" means 00:01 where the developer is, which is the boundary the
    # sweep is about.
    return naive.astimezone()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Wrap setup, call and teardown of every test in the frozen instant.

    Wrapping the whole protocol rather than using an autouse fixture is
    deliberate: fixtures that stamp timestamps during setup have to see the
    frozen clock too, and autouse ordering against other fixtures is not
    something a sweep should have to reason about.
    """
    destination = _destination()
    if destination is None:
        yield
        return

    import time_machine

    with time_machine.travel(destination, tick=False):
        yield
