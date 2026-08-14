"""The wrong-arch notice: that it fires at all, and that the probe stays memoised.

``_warn_if_wrong_arch_build`` sits in ``BetterFlowApp.run()`` just before
``self.tray.run_blocking()``. That position is load-bearing for one plain
reason: ``run_blocking()`` blocks for the life of the process, so a call moved
below it never executes. Nothing pinned the position until this file.

It is ALSO the first caller of ``machine_arch``'s memoised ``sysctl`` probe —
but read the next paragraph before pricing that, because the obvious reading is
wrong and was in this file's first draft.

The tray rebuilds its menu on every stats update, and ``_create_menu()`` runs
under ``_menu_lock`` (built at ``tray.py:1675`` and ``:1689``, under the locks
taken at ``:1666`` and ``:1685``). The menu is built EAGERLY — ``_arch_menu_item``
calls ``arch_menu_label()`` and hands the finished string to ``Item`` — so an
UN-MEMOISED probe really would fork ``sysctl`` (``timeout=2``) under that lock
for the life of the process. The memo is what prevents it.

The warm-up ordering is NOT what prevents it, though that was this file's first
draft: ``_update_menu`` returns early while ``self._icon`` is ``None``, and the
first ``_create_menu()`` in production is a constructor argument
(``tray.py:1795``) inside ``run_blocking()``, evaluated before ``_icon`` is
assigned at ``:1791`` and holding no menu lock. So dropping the warm-up costs
exactly one lock-free fork on the main thread at startup — not an ongoing cost,
and not a cost under the lock.

Hence two independent guards, failing for different reasons: the ordering tests
catch the call moving or being deleted, and the fork-counting pair catches the
MEMO being removed. Do not merge their rationales; they are not the same claim.

Every test here injects ``system="Darwin"`` rather than reading the host's. The
short-circuit is in ``machine_arch.py:120`` — ``is_rosetta_translated`` returns
``False`` for a non-Darwin system without ever consulting the reader. Note that
``arch_menu_label`` does NOT itself short-circuit first: it calls
``is_rosetta_translated`` unconditionally (``tray.py:306``) and only then takes
its non-Darwin branch (``:308``). The net effect is the same — no fork off
macOS — but the mechanism lives in one place, not two, and this file exists to
stop the next reader inheriting a mechanism that is not there.

Either way a host-dependent version of these tests counts zero forks and fails
on the ubuntu runner that gates every PR — and on three of the four platforms
of the tag build, which produces no release when any leg is red. A ``skipif``
would be worse than useless: only tag builds run a macOS job, so the guard
would then execute in no PR-gating job at all.
"""

import inspect
from unittest.mock import MagicMock, patch

import pytest

from src import machine_arch as ma
from src.machine_arch import X86_64
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
    """``run_blocking()`` blocks for the life of the process.

    A call moved below it does not run late — it never runs at all, on any
    machine, forever, with nothing to show for it.
    """
    source = inspect.getsource(BetterFlowApp.run)
    warn = source.index("self._warn_if_wrong_arch_build()")
    tray = source.index("self.tray.run_blocking()")
    assert warn < tray, (
        "the arch warning moved below the tray loop — run_blocking() never "
        "returns, so the notice can no longer fire on any machine"
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
        "or some state will not see it"
    )


# --- the memo, which is what keeps sysctl off _menu_lock -------------------


def _counting_probe():
    """The real probe, wrapped so we can count actual subprocess forks.

    The counter increments BEFORE delegating, so a host with no ``sysctl`` (any
    CI runner that is not a Mac) still records the attempt. That is what lets
    these assertions mean the same thing on every platform.
    """
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
    machine_arch would produce — and it is exactly what the whole non-Darwin
    branch produces. This proves the row can reach the probe at all, so the
    zero next door means something.
    """
    calls, counting = _counting_probe()
    with patch.object(ma, "_read_proc_translated", counting):
        ma.reset_cache_for_tests()
        arch_menu_label(system="Darwin", machine=X86_64)

    assert len(calls) == 1, (
        "the architecture row no longer reaches the sysctl probe — the test "
        "below is now asserting nothing"
    )


def test_the_memo_makes_every_later_menu_rebuild_free():
    """Menu rebuilds run under ``_menu_lock``; a fork there blocks the tray.

    50 rebuilds stands in for a long-lived session — the tray rebuilds on every
    stats update, so without the memo this is a 2s-worst-case fork under the
    lock on the sync cycle, for the life of the process.
    """
    calls, counting = _counting_probe()
    with patch.object(ma, "_read_proc_translated", counting):
        ma.reset_cache_for_tests()

        ma.is_rosetta_translated(system="Darwin")
        warmup_forks = len(calls)
        calls.clear()

        for _ in range(50):
            arch_menu_label(system="Darwin", machine=X86_64)

    assert warmup_forks == 1, "the first probe did not fork; nothing was cached"
    assert calls == [], (
        f"the architecture row forked sysctl {len(calls)} times after the "
        "first probe — the memo in machine_arch is gone, and menu rebuilds "
        "now block _menu_lock on the sync cycle"
    )


def test_a_translated_mac_actually_gets_the_notification():
    """The POSITIVE control the rest of this file was missing.

    Every other assertion about ``send_notification`` here is a negative one —
    ``assert_not_called`` on a broken probe — and the ordering tests read
    ``run``'s source for the CALL SITE, never the method body. So emptying the
    whole ``send_notification(...)`` block left the entire suite green: the one
    user-facing half of #185 was witnessed by nothing.

    That matters more than the usual coverage gap because this path runs on
    essentially no developer machine and on no CI leg. It executes only on an
    Apple Silicon Mac running the x86_64 build, which is exactly the
    configuration nobody testing this has in front of them.
    """
    stub = MagicMock()
    with patch("src.machine_arch.platform.system", return_value="Darwin"), patch.object(
        ma, "_read_proc_translated_cached", return_value="1"
    ), patch("src.main.send_notification") as notify:
        BetterFlowApp._warn_if_wrong_arch_build(stub)

    notify.assert_called_once()
    title, body = notify.call_args[0][:2]
    # Pin the SUBJECT rather than the wording: the notice is useless if it does
    # not name the remedy, since "Intel build" does not read as "wrong" alone.
    assert "Apple Silicon" in title or "Apple Silicon" in body
    assert "Reinstall" in body


def test_the_warning_survives_a_probe_that_blows_up():
    """Best-effort: a broken sysctl loses the notice and nothing else.

    Two halves, and the second is the one worth writing. Startup must not die
    because a diagnostic could not be computed — AND the failure must stay
    silent, because a notice fired on an unknown architecture would tell a
    genuine Intel Mac it is running the wrong build.

    ``platform.system`` is forced because on a Linux runner
    ``is_rosetta_translated`` returns before consulting the probe at all, so
    the OSError would never be raised and this would pass vacuously.
    """
    stub = MagicMock()
    with patch("src.machine_arch.platform.system", return_value="Darwin"), patch.object(
        ma, "_read_proc_translated_cached", side_effect=OSError("no sysctl")
    ), patch("src.main.send_notification") as notify:
        BetterFlowApp._warn_if_wrong_arch_build(stub)  # must not raise

    notify.assert_not_called()
