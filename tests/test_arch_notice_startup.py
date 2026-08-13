"""The wrong-arch notice, and the ordering that keeps ``sysctl`` off the tray lock.

``_warn_if_wrong_arch_build`` looks like a pure user-facing courtesy, so the
obvious refactor is to move it, defer it, or drop it onto a worker. It has a
second job that nothing in its own body advertises: it is the FIRST caller of
``machine_arch``'s memoised probe, and it runs on the main thread while the
tray is still idle.

That matters because the architecture row is rebuilt with every menu rebuild,
and ``TrayIcon._create_menu()`` runs while holding ``_menu_lock`` on the sync
cycle. With a cold memo the row forks ``sysctl`` (``timeout=2``) under that
lock. Today it is always warm by then — but only because of where the warning
sits in ``run()``, and nothing pinned that until this file. The comments in
``machine_arch`` and ``TrayIcon._arch_menu_item`` both assert the property as
though it were structural; it is positional.

So there are two tests here, and they fail for different reasons. The ordering
test catches the call moving or being deleted. The behavioural test pins the
REASON the ordering matters, by counting real subprocess forks — it would still
fail if the memo were removed while the ordering stayed correct.
"""

import inspect
from unittest.mock import MagicMock, patch

import pytest

from src import machine_arch as ma
from src.main import BetterFlowApp
from src.ui.tray import arch_menu_label


@pytest.fixture(autouse=True)
def _clear_arch_cache():
    ma.reset_cache_for_tests()
    yield
    ma.reset_cache_for_tests()


# --- the callsite guards ---------------------------------------------------


def test_run_warns_about_a_wrong_arch_build():
    """Fails if the call is dropped from run(), not only if the logic breaks."""
    source = inspect.getsource(BetterFlowApp.run)
    assert "_warn_if_wrong_arch_build()" in source


def test_the_warning_runs_before_the_tray_loop():
    """Ordering is load-bearing twice over.

    After ``tray.run_blocking()`` the call would never execute at all — that
    method blocks for the life of the process. And the probe would then first
    run from a menu rebuild, i.e. under ``_menu_lock``, which is the thing the
    memo exists to prevent (see this module's docstring).
    """
    source = inspect.getsource(BetterFlowApp.run)
    warn = source.index("self._warn_if_wrong_arch_build()")
    tray = source.index("self.tray.run_blocking()")
    assert warn < tray, (
        "the arch warning moved after the tray loop — it now never fires, and "
        "the first sysctl probe lands under _menu_lock on the sync cycle"
    )


def test_the_warning_is_not_nested_behind_a_platform_branch():
    """It must reach every launch; is_rosetta_translated() does the filtering.

    Gating the call on a platform check in run() would put the same rule in two
    places, and the copy in run() is the one that would drift.
    """
    source = inspect.getsource(BetterFlowApp.run)
    line = [
        line
        for line in source.splitlines()
        if "self._warn_if_wrong_arch_build()" in line
    ][0]
    # Top level of run(): 8 spaces of indentation, not inside a conditional.
    assert len(line) - len(line.lstrip()) == 8, (
        "the arch warning is nested inside a branch in run() — some platform "
        "or some state will not warm the probe"
    )


# --- the reason the ordering matters ---------------------------------------


def _counting_probe():
    """The real probe, wrapped so we can count actual subprocess forks."""
    calls = []
    real = ma._read_proc_translated

    def counting():
        calls.append(1)
        return real()

    return calls, counting


def test_a_cold_probe_really_does_fork_from_the_menu_row():
    """The positive control, and it is not optional.

    The test below asserts an ABSENCE of forks. An absence is also what a
    broken counter, an unreachable row, or a label that stopped consulting
    machine_arch would produce. This proves the row can reach the probe at all,
    so the zero next door means something.
    """
    calls, counting = _counting_probe()
    with patch.object(ma, "_read_proc_translated", counting):
        ma.reset_cache_for_tests()
        arch_menu_label()

    assert len(calls) == 1, (
        "the architecture row no longer reaches the sysctl probe — the test "
        "below is now asserting nothing"
    )


def test_warming_the_probe_first_makes_every_menu_rebuild_free():
    """What run() buys the tray: no fork under _menu_lock, ever.

    50 rebuilds stands in for a long-lived session — the tray rebuilds its menu
    on every stats update, so an un-memoised probe would fork sysctl on the
    sync cycle for the life of the process.
    """
    calls, counting = _counting_probe()
    with patch.object(ma, "_read_proc_translated", counting):
        ma.reset_cache_for_tests()

        # Exactly what _warn_if_wrong_arch_build does first.
        ma.is_rosetta_translated()
        warmup_forks = len(calls)
        calls.clear()

        for _ in range(50):
            arch_menu_label()

    assert warmup_forks == 1, "the warm-up did not probe; nothing was cached"
    assert calls == [], (
        f"the architecture row forked sysctl {len(calls)} times after the "
        "warm-up — the memo in machine_arch is gone, and menu rebuilds now "
        "block _menu_lock on the sync cycle"
    )


def test_the_warning_survives_a_probe_that_blows_up():
    """Best-effort: a broken sysctl loses the notice and nothing else.

    Startup must not die because a diagnostic could not be computed.
    """
    stub = MagicMock()
    with patch.object(
        ma, "_read_proc_translated_cached", side_effect=OSError("no sysctl")
    ):
        BetterFlowApp._warn_if_wrong_arch_build(stub)  # must not raise

    stub.tray.assert_not_called()
