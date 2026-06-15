"""Tests for the macOS active-tab URL tracker (src/browser_tracker.py).

The buffer logic and AppleScript plumbing are tested without a real browser:
samples are fed directly, and osascript is mocked.
"""

from unittest.mock import MagicMock, patch

import src.browser_tracker as bt


class TestIsBrowserApp:
    def test_recognizes_chromium_and_safari(self):
        assert bt.is_browser_app("Google Chrome")
        assert bt.is_browser_app("google chrome")  # case-insensitive
        assert bt.is_browser_app("Brave Browser")
        assert bt.is_browser_app("Safari")
        assert bt.is_browser_app("Arc")

    def test_rejects_non_browsers_and_empty(self):
        assert not bt.is_browser_app("Terminal")
        assert not bt.is_browser_app("Slack")
        assert not bt.is_browser_app("")
        assert not bt.is_browser_app(None)


class TestUrlBuffer:
    def test_url_at_returns_most_recent_at_or_before(self):
        t = bt.BrowserURLTracker(match_tolerance_seconds=8.0)
        t.record(100.0, "https://a.com")
        t.record(105.0, "https://b.com")
        t.record(110.0, "https://c.com")

        assert t.url_at(106.0) == "https://b.com"   # most recent <= 106
        assert t.url_at(110.0) == "https://c.com"   # exact match
        assert t.url_at(112.0) == "https://c.com"   # within tolerance after last

    def test_url_at_none_before_first_sample(self):
        t = bt.BrowserURLTracker()
        t.record(100.0, "https://a.com")
        assert t.url_at(50.0) is None

    def test_url_at_respects_tolerance(self):
        t = bt.BrowserURLTracker(match_tolerance_seconds=8.0)
        t.record(100.0, "https://a.com")
        # 9s later — beyond tolerance, so the stale sample is not trusted.
        assert t.url_at(109.0) is None
        assert t.url_at(107.9) == "https://a.com"

    def test_record_evicts_beyond_retention(self):
        t = bt.BrowserURLTracker(retention_seconds=60.0)
        t.record(100.0, "https://old.com")
        t.record(170.0, "https://new.com")  # 70s later evicts the first
        assert len(t._samples) == 1
        assert t._samples[0][1] == "https://new.com"

    def test_record_ignores_empty_url(self):
        t = bt.BrowserURLTracker()
        t.record(100.0, "")
        assert len(t._samples) == 0

    def test_null_tracker_returns_none(self):
        t = bt.BrowserURLTracker()
        assert t.url_at(123.0) is None


class TestActiveUrlScript:
    def test_chromium_uses_active_tab(self):
        script = bt._active_url_script_for("Google Chrome")
        assert "active tab of front window" in script
        assert "Google Chrome" in script

    def test_safari_uses_front_document(self):
        script = bt._active_url_script_for("Safari")
        assert "front document" in script

    def test_unknown_app_returns_none(self):
        assert bt._active_url_script_for("Terminal") is None


class TestGetActiveBrowserUrl:
    def test_returns_url_for_frontmost_browser(self):
        with patch.object(bt, "_osascript", side_effect=["Google Chrome", "https://github.com/foo"]):
            assert bt.get_active_browser_url() == "https://github.com/foo"

    def test_none_when_frontmost_is_not_a_browser(self):
        with patch.object(bt, "_osascript", side_effect=["Terminal"]) as m:
            assert bt.get_active_browser_url() is None
            # Should not even attempt the URL query for a non-browser.
            assert m.call_count == 1

    def test_none_for_non_http_url(self):
        with patch.object(bt, "_osascript", side_effect=["Google Chrome", "chrome://newtab"]):
            assert bt.get_active_browser_url() is None

    def test_none_when_osascript_fails(self):
        with patch.object(bt, "_osascript", side_effect=[None]):
            assert bt.get_active_browser_url() is None


class TestOsascript:
    def test_returns_none_on_nonzero_exit(self):
        proc = MagicMock(returncode=1, stdout="", stderr="not authorised")
        with patch("src.browser_tracker.subprocess.run", return_value=proc):
            assert bt._osascript("whatever") is None

    def test_returns_stdout_on_success(self):
        proc = MagicMock(returncode=0, stdout="https://x.com\n", stderr="")
        with patch("src.browser_tracker.subprocess.run", return_value=proc):
            assert bt._osascript("whatever") == "https://x.com"

    def test_returns_none_on_timeout(self):
        import subprocess
        with patch("src.browser_tracker.subprocess.run", side_effect=subprocess.TimeoutExpired("osascript", 2)):
            assert bt._osascript("whatever") is None


class TestStartBrowserTracker:
    def test_disabled_returns_null_tracker(self):
        t = bt.start_browser_tracker(enabled=False)
        assert t.url_at(123.0) is None
        t.stop()  # no-op, must not raise


# ---------------------------------------------------------------------------
# Windows UI Automation reader (logic that is testable off-Windows)
# ---------------------------------------------------------------------------


class TestNormalizeWindowsUrl:
    def test_passes_through_http_and_https(self):
        assert bt._normalize_windows_url("https://github.com/x") == "https://github.com/x"
        assert bt._normalize_windows_url("http://example.com") == "http://example.com"

    def test_adds_scheme_to_bare_host(self):
        # Chromium hides the scheme in the omnibox.
        assert bt._normalize_windows_url("github.com/issues") == "https://github.com/issues"
        assert bt._normalize_windows_url("  app.betterflow.eu  ") == "https://app.betterflow.eu"

    def test_rejects_search_queries(self):
        assert bt._normalize_windows_url("how to test windows") is None

    def test_rejects_internal_and_non_web(self):
        assert bt._normalize_windows_url("chrome://settings") is None
        assert bt._normalize_windows_url("edge://flags") is None
        assert bt._normalize_windows_url("about:blank") is None
        assert bt._normalize_windows_url("file:///C:/x.txt") is None

    def test_rejects_empty_and_non_hosts(self):
        assert bt._normalize_windows_url("") is None
        assert bt._normalize_windows_url("   ") is None
        assert bt._normalize_windows_url(None) is None
        assert bt._normalize_windows_url("localhost") is None  # no dot
        assert bt._normalize_windows_url("newtab") is None


class TestFirstUrlFromValues:
    def test_returns_first_valid_url(self):
        assert bt._first_url_from_values(["", "search terms", "github.com"]) == "https://github.com"

    def test_none_when_no_valid(self):
        assert bt._first_url_from_values(["", "chrome://x", "just text here"]) is None


class _FakeControl:
    """Minimal stand-in for a uiautomation Control for the BFS walk."""

    def __init__(self, type_name="PaneControl", value=None, children=None):
        self._type = type_name
        self._value = value
        self._children = children or []

    @property
    def ControlTypeName(self):  # noqa: N802
        return self._type

    def GetChildren(self):  # noqa: N802
        return self._children

    def GetValuePattern(self):  # noqa: N802
        v = self._value

        class _VP:
            Value = v

        return _VP()


class TestCollectUiaUrlValues:
    def test_edits_come_before_documents(self):
        tree = _FakeControl(children=[
            _FakeControl("DocumentControl", value="example.org/page"),
            _FakeControl("ToolbarControl", children=[
                _FakeControl("EditControl", value="github.com/x"),
            ]),
        ])
        values = bt._collect_uia_url_values(tree)
        # Edit value precedes Document value so a real URL is preferred.
        assert values == ["github.com/x", "example.org/page"]

    def test_depth_bound_stops_descent(self):
        deep = _FakeControl("EditControl", value="deep.com")
        # Nest the edit far below the depth bound.
        node = deep
        for _ in range(12):
            node = _FakeControl("PaneControl", children=[node])
        values = bt._collect_uia_url_values(node, max_depth=3)
        assert "deep.com" not in values

    def test_node_cap_is_respected(self):
        wide = _FakeControl(children=[_FakeControl("PaneControl") for _ in range(1000)])
        # Should not raise / hang; just returns what it scanned within the cap.
        assert bt._collect_uia_url_values(wide, max_nodes=10) == []


class TestWindowsTrackerPoll:
    def test_poll_records_url_into_buffer(self):
        with patch.object(bt, "get_active_browser_url_windows", return_value="https://x.com"):
            tracker = bt._start_windows_tracker(
                poll_interval=0.01, retention_seconds=600.0, match_tolerance_seconds=8.0
            )
            try:
                import time as _t
                deadline = _t.time() + 2.0
                got = None
                while _t.time() < deadline:
                    got = tracker.url_at(_t.time())
                    if got:
                        break
                    _t.sleep(0.02)
                assert got == "https://x.com"
            finally:
                tracker.stop()
