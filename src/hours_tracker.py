"""Hours and trends tracking: fetch from API, format for tray display."""

import logging
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)


class HoursTracker:
    """Fetches and caches hours/trends from the BetterFlow API.

    Owns its own _state_lock (leaf lock, no nesting with other locks).
    """

    def __init__(self, bf, sync_engine, tray) -> None:
        self.bf = bf
        self.sync_engine = sync_engine
        self.tray = tray

        self._state_lock = threading.Lock()
        self._hours_today_seconds = 0
        self._hours_today_cache = "0h 0m"
        self._last_hours_refresh: float = 0.0
        self._trends_cache: dict[str, str] = {
            "hours_this_week": "---",
            "hours_this_month": "---",
            "daily_avg_this_week": "---",
        }

    def fetch_hours_today(self) -> str:
        """Fetch today's tracked hours from API (source of truth).

        On failure, returns the most recently cached value and leaves the
        internal state untouched — ``_last_hours_refresh`` is only bumped
        on a successful fetch so the next retry isn't suppressed by the
        30-second throttle.
        """
        try:
            status = self.bf.get_status()
            summary = status.get("data", {}).get("today_summary", {})
            tracked_seconds = summary.get("tracked_seconds")

            if tracked_seconds is None:
                total_seconds = int(self.sync_engine.get_today_active_time().total_seconds())
            else:
                total_seconds = int(tracked_seconds)

            clamped = max(0, total_seconds)
            formatted = self.format_hours(clamped)
            with self._state_lock:
                self._hours_today_seconds = clamped
                self._hours_today_cache = formatted
                self._last_hours_refresh = time.monotonic()
            return formatted
        except Exception as e:
            logger.warning("fetch_hours_today failed, returning cached value: %s", e)
            with self._state_lock:
                return self._hours_today_cache

    def refresh_hours_today(self, *, logged_in: bool) -> None:
        """Refresh tray hours from server.

        Always runs (even when paused/private) so the tray resets to
        ``0h 0m`` at midnight instead of showing yesterday's stale value.
        """
        try:
            if not logged_in:
                logger.debug("_refresh_hours: skipped (not logged in)")
                return

            with self._state_lock:
                last_refresh = self._last_hours_refresh
            if time.monotonic() - last_refresh < 30:
                return

            hours = self.fetch_hours_today()
            self.tray.update_stats(hours_today=hours)
        except Exception as e:
            logger.warning(f"Failed to refresh tray hours: {e}")

    def fetch_trends(self, *, logged_in: bool) -> None:
        """Fetch weekly/monthly trend data from server."""
        try:
            if not logged_in:
                return
            response = self.bf.get_trends()
            data = response.get("data", {})
            cache = {
                "hours_this_week": self.format_hours(int(data.get("week_total_seconds", 0))),
                "hours_this_month": self.format_hours(int(data.get("month_total_seconds", 0))),
                "daily_avg_this_week": self.format_hours(int(data.get("week_daily_avg_seconds", 0))),
            }
            with self._state_lock:
                self._trends_cache = cache
            self.tray.update_stats(**cache)
        except Exception as e:
            logger.debug(f"Failed to fetch trends: {e}")

    def reset(self) -> None:
        """Reset cached trends and hours to placeholder values."""
        with self._state_lock:
            self._hours_today_seconds = 0
            self._hours_today_cache = "0h 0m"
            self._last_hours_refresh = 0.0
            self._trends_cache = {
                "hours_this_week": "---",
                "hours_this_month": "---",
                "daily_avg_this_week": "---",
            }
        self.tray.update_stats(
            hours_today="0h 0m",
            hours_this_week="---",
            hours_this_month="---",
            daily_avg_this_week="---",
        )

    @staticmethod
    def format_hours(total_seconds: int) -> str:
        """Format accumulated seconds as `Xh Ym` for tray display."""
        hours = int(total_seconds) // 3600
        minutes = (int(total_seconds) % 3600) // 60
        return f"{hours}h {minutes}m"
