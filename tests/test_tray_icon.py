"""Unit tests for the small pure helpers in src.ui.tray.

The icon-rendering function itself is a PIL surface and is hard to assert
on without snapshot tests, but `_readable_fg_for` is a pure function with
a sharp behavioural contract — it picks the foreground colour for the
state-coloured menu bar disk. Regressions here cause invisible icons on
macOS light mode (the issue Emilian flagged 2026-06-10), so a guardrail
test is cheap insurance.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src.ui.tray import STATE_COLORS, TrayState, _readable_fg_for


class TestReadableFgFor:
    """Boundary + state-color coverage for the luminance-based fg picker."""

    def test_dark_purple_gets_white(self) -> None:
        # SYNCING brand purple — well below the cut-off → white reads on it.
        assert _readable_fg_for("#7D69B8") == "#FFFFFF"

    def test_bright_yellow_gets_dark(self) -> None:
        # QUEUED yellow — luminance ≈176; white on this is invisible on
        # any menu bar background.
        assert _readable_fg_for("#eab308") == "#1a1a1a"

    def test_light_gray_gets_dark(self) -> None:
        # PAUSED light gray — luminance ≈163, only just above the
        # threshold; if this regresses to white the icon becomes
        # invisible on a light menu bar.
        assert _readable_fg_for("#9ca3af") == "#1a1a1a"

    def test_orange_warning_gets_white(self) -> None:
        # QUEUE_WARNING orange — luminance ≈144, white still reads.
        assert _readable_fg_for("#f97316") == "#FFFFFF"

    def test_accepts_color_without_leading_hash(self) -> None:
        # Defensive: future call sites that forget the "#" should still
        # get a correct pick rather than silently misreading bytes.
        assert _readable_fg_for("eab308") == "#1a1a1a"
        assert _readable_fg_for("7D69B8") == "#FFFFFF"

    def test_malformed_input_falls_back_to_white(self) -> None:
        # Anything we can't parse falls back to white (safer default on
        # the dark menu bar that's the common case).
        assert _readable_fg_for("nope") == "#FFFFFF"
        assert _readable_fg_for("") == "#FFFFFF"
        assert _readable_fg_for("#zzzzzz") == "#FFFFFF"
        assert _readable_fg_for("#ff") == "#FFFFFF"

    @pytest.mark.parametrize("state", list(TrayState))
    def test_every_state_color_yields_high_contrast_fg(self, state: TrayState) -> None:
        # Every defined state must produce a valid choice — guards against
        # someone adding a state with an unparseable colour string.
        fg = _readable_fg_for(STATE_COLORS[state])
        assert fg in ("#1a1a1a", "#FFFFFF")


def _tray_with_dead_icon(monkeypatch, died):
    """A TrayIcon whose health probe always fails, wired to record death."""
    from src.ui.tray import TrayIcon

    with patch("src.ui.tray.pystray"):
        tray = TrayIcon(on_tray_died=lambda: died.append(True))
    monkeypatch.setattr(tray, "_check_tray_health", lambda: False)
    monkeypatch.setattr(tray, "_update_icon", lambda: None)
    return tray


def test_single_health_failure_does_not_kill_the_agent(monkeypatch):
    """One failed probe is a transient AppKit hiccup, not a dead status item.

    _on_tray_died shuts the agent down and stops capture for the rest of the
    day, so it must not fire on a single failure.
    """
    died = []
    tray = _tray_with_dead_icon(monkeypatch, died)

    tray.tick_clock()

    assert died == []


def test_consecutive_health_failures_declare_death(monkeypatch):
    died = []
    tray = _tray_with_dead_icon(monkeypatch, died)

    for _ in range(tray._TRAY_HEALTH_FAILURES_TO_DIE):
        tray.tick_clock()

    assert died == [True]


def test_recovered_probe_resets_the_failure_streak(monkeypatch):
    """A healthy tick must clear the streak so isolated blips never accumulate
    into a shutdown across an entire workday."""
    died = []
    tray = _tray_with_dead_icon(monkeypatch, died)

    tray.tick_clock()                                    # fail 1
    monkeypatch.setattr(tray, "_check_tray_health", lambda: True)
    tray.tick_clock()                                    # recovered
    monkeypatch.setattr(tray, "_check_tray_health", lambda: False)
    tray.tick_clock()                                    # fail 1 again, not 2

    assert died == []
