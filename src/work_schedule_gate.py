"""Schedule-aware capture gate.

Decides whether the agent is allowed to *collect* activity right now, based on
the server-enforced working-hours schedule. This is the privacy boundary the
admin UI promises: for a restricted user (B2E / Trainee), the agent must not
read the machine outside the working window at all — not merely decline to
upload it. The caller (the app) acts on the decision by stopping/starting the
window + browser capture and pausing/resuming the sync engine.

Fail-closed by construction: an *enforced* schedule is a hard boundary. The only
way to collect outside it is the explicit, user-initiated override
(:meth:`request_work_outside_hours`) — the "unless strictly requested" escape —
which the user re-arms each day and which auto-expires at the end of the local
day so it can never silently persist into tomorrow.

Unrestricted users (B2B / flexible / no schedule) are never gated: ``enforces``
is False, so :meth:`collection_allowed` is always True and behaviour is
unchanged for them.

Pure decision logic with a small amount of override state — no I/O, no watcher
control. Owns its own leaf lock (no nesting with other locks).
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


class WorkScheduleGate:
    """Answers "may the agent collect right now?" from the working-hours config.

    Holds the transient "work outside hours" override. Thread-safe; the override
    is read every 60s tick from the scheduler thread and written from the tray
    callback thread.
    """

    def __init__(self, config) -> None:
        self.config = config
        self._lock = threading.Lock()
        # Aware-UTC instant the override stops applying; None means no override.
        self._override_until: Optional[datetime] = None

    # ── window helpers ────────────────────────────────────────────────
    def _schedule_tz(self):
        tz_name = self.config.working_hours.timezone
        if not tz_name:
            return None
        try:
            return ZoneInfo(tz_name)
        except Exception:
            return None

    def _end_of_local_day(self, now: datetime) -> datetime:
        """The next local midnight (in the schedule's timezone, falling back to
        the machine's local zone) as an aware-UTC instant. The override expires
        here so "work outside hours" never leaks into the following day."""
        tz = self._schedule_tz()
        local = now.astimezone(tz) if tz else now.astimezone()
        next_midnight = (local + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return next_midnight.astimezone(timezone.utc)

    @staticmethod
    def _now(now: Optional[datetime]) -> datetime:
        return now if now is not None else datetime.now(timezone.utc)

    # ── override ──────────────────────────────────────────────────────
    def request_work_outside_hours(self, now: Optional[datetime] = None) -> None:
        """Arm the explicit override: collect outside the window until the end of
        the current local day. Idempotent within a day. No effect on the actual
        suspend/resume — the caller re-evaluates :meth:`collection_allowed`."""
        now = self._now(now)
        until = self._end_of_local_day(now)
        with self._lock:
            self._override_until = until
        logger.info("Work-outside-hours override armed until %s", until.isoformat())

    def clear_override(self) -> None:
        with self._lock:
            had = self._override_until is not None
            self._override_until = None
        if had:
            logger.info("Work-outside-hours override cleared")

    def override_active(self, now: Optional[datetime] = None) -> bool:
        now = self._now(now)
        with self._lock:
            until = self._override_until
            if until is None:
                return False
            if now >= until:
                # Expired — drop it so a stale instant can't be re-read.
                self._override_until = None
                return False
            return True

    # ── decision ──────────────────────────────────────────────────────
    def collection_allowed(self, now: Optional[datetime] = None) -> bool:
        """True if the agent may collect right now.

        Unrestricted schedule → always True. Restricted → True only inside the
        window OR while the user's override is active. Fail-closed otherwise."""
        wh = self.config.working_hours
        if not wh.enforces():
            return True
        now = self._now(now)
        if wh.is_within_window(now):
            return True
        return self.override_active(now)

    def should_offer_override(self, now: Optional[datetime] = None) -> bool:
        """Whether the tray should show the "Work outside hours" item: only for a
        restricted user who is currently outside the window and has not already
        armed the override."""
        wh = self.config.working_hours
        if not wh.enforces():
            return False
        now = self._now(now)
        if wh.is_within_window(now):
            return False
        return not self.override_active(now)
