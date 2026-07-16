"""Shared AFK-credit math for engagement detectors (call, mic, ...).

One function, used by every detector that injects no-input engagement credit
into the billed AFK stream, so the anti-fraud bound can never drift between
them (a one-sided fix here was the reviewer-flagged failure mode of keeping
per-detector copies).

The bound: credit extends at most ``cap_seconds`` past the ANCHOR — the most
recent REAL keyboard/mouse input, as passed by ``AfkSource`` to every activity
source. Anchoring on the session/call START instead let the cap reset by
cycling sessions (drop the mic for two minutes, or alternate two call-titled
windows: each new session re-armed a fresh cap → indefinite zero-input
credit). Anchored on real input, chaining is pointless: one genuine human
action opens one bounded credit window, shared by all detectors.
"""

from datetime import datetime, timedelta
from typing import Optional


def capped_credit(
    engaged_until: datetime,
    anchor: Optional[datetime],
    fallback_anchor: Optional[datetime],
    cap_seconds: float,
    now: datetime,
) -> Optional[datetime]:
    """Bound an engagement instant by the credit cap.

    Returns ``min(engaged_until, anchor + cap_seconds, now)``.

    ``anchor`` is the most recent real input (may be None when the OS idle
    clock was unreadable); ``fallback_anchor`` (the session/call start) keeps
    the old per-session bound in that corner rather than granting unbounded
    credit. None when no anchor of any kind exists.
    """
    effective_anchor = anchor or fallback_anchor
    if effective_anchor is None:
        return None
    cap_end = effective_anchor + timedelta(seconds=cap_seconds)
    return min(engaged_until, cap_end, now)
