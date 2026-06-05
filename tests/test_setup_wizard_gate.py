"""Tests for the SetupWizard permission-gate code path.

Covers the three gate outcomes ('granted', 'restart', 'quit') and the
_render_permissions early-return guard, without starting a real Tk event loop.
"""

from unittest.mock import MagicMock, patch

from src.config import Config
from src.ui.setup_wizard import SetupWizard, run_permission_gate


class TestPermissionGate:
    """Unit tests for _finish_gate and related gate-mode behaviour."""

    def _make_wizard(self, config=None) -> SetupWizard:
        """Return a SetupWizard wired with mocked Tk objects.

        _build_window is never called, so no real display is needed.
        """
        w = SetupWizard(config)
        w._window = MagicMock()
        w._canvas = MagicMock()
        w._canvas.winfo_exists.return_value = True
        return w

    # ------------------------------------------------------------------
    # _finish_gate — result propagation
    # ------------------------------------------------------------------

    def test_finish_gate_granted_sets_result(self):
        w = self._make_wizard()
        w._finish_gate("granted")
        assert w._gate_result == "granted"

    def test_finish_gate_restart_sets_result(self):
        w = self._make_wizard()
        w._finish_gate("restart")
        assert w._gate_result == "restart"

    def test_finish_gate_quit_sets_result(self):
        w = self._make_wizard()
        w._finish_gate("quit")
        assert w._gate_result == "quit"

    # ------------------------------------------------------------------
    # _finish_gate — timer cancellation
    # ------------------------------------------------------------------

    def test_finish_gate_cancels_pending_timer(self):
        w = self._make_wizard()
        w._spinner_after_id = "timer-id"
        w._finish_gate("granted")
        w._window.after_cancel.assert_called_once_with("timer-id")
        assert w._spinner_after_id is None

    def test_finish_gate_no_timer_does_not_call_after_cancel(self):
        w = self._make_wizard()
        assert w._spinner_after_id is None
        w._finish_gate("granted")
        w._window.after_cancel.assert_not_called()

    def test_finish_gate_destroys_window(self):
        w = self._make_wizard()
        w._finish_gate("granted")
        w._window.destroy.assert_called_once()

    def test_finish_gate_swallows_tkerror_on_cancel(self):
        """TclError during after_cancel must not propagate."""
        import tkinter as tk

        w = self._make_wizard()
        w._spinner_after_id = "stale-id"
        w._window.after_cancel.side_effect = tk.TclError("bad token")
        w._finish_gate("granted")  # should not raise
        assert w._gate_result == "granted"
        assert w._spinner_after_id is None

    # ------------------------------------------------------------------
    # _on_close — gate mode behaviour
    # ------------------------------------------------------------------

    def test_on_close_sets_quit_in_gate_mode(self):
        w = self._make_wizard()
        w._gate_only = True
        w._on_close()
        assert w._gate_result == "quit"

    def test_on_close_overwrites_earlier_gate_result(self):
        """Even if _gate_result was already set, close forces 'quit'."""
        w = self._make_wizard()
        w._gate_only = True
        w._gate_result = "granted"
        w._on_close()
        assert w._gate_result == "quit"

    def test_on_close_non_gate_mode_does_not_touch_gate_result(self):
        w = self._make_wizard()
        w._gate_only = False
        w._on_close()
        # Default value should be unchanged
        assert w._gate_result == "quit"

    # ------------------------------------------------------------------
    # _render_permissions — early-return guard when closing
    # ------------------------------------------------------------------

    def test_render_permissions_returns_early_when_closing(self):
        w = self._make_wizard()
        w._closing = True
        with patch("src.ui.permissions.input_monitoring_active") as mock_ima:
            w._render_permissions()
            mock_ima.assert_not_called()

    def test_render_permissions_returns_early_when_canvas_gone(self):
        w = self._make_wizard()
        w._canvas.winfo_exists.return_value = False
        with patch("src.ui.permissions.input_monitoring_active") as mock_ima:
            w._render_permissions()
            mock_ima.assert_not_called()

    # ------------------------------------------------------------------
    # run_permission_gate module function — optional config
    # ------------------------------------------------------------------

    def test_run_permission_gate_accepts_no_config(self):
        """run_permission_gate() must be callable without a Config argument."""
        wizard_mock = MagicMock()
        wizard_mock.run_permission_gate.return_value = "granted"
        with patch("src.ui.setup_wizard.SetupWizard", return_value=wizard_mock) as mock_cls:
            result = run_permission_gate()
        mock_cls.assert_called_once_with(None)
        assert result == "granted"

    def test_run_permission_gate_forwards_config_when_supplied(self):
        """Existing callers that supply a Config continue to work."""
        cfg = MagicMock(spec=Config)
        wizard_mock = MagicMock()
        wizard_mock.run_permission_gate.return_value = "granted"
        with patch("src.ui.setup_wizard.SetupWizard", return_value=wizard_mock) as mock_cls:
            result = run_permission_gate(cfg)
        mock_cls.assert_called_once_with(cfg)
        assert result == "granted"
