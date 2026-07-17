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
    *,
    anchor: Optional[datetime],
    engagement_start: Optional[datetime],
    cap_seconds: float,
    now: datetime,
) -> Optional[datetime]:
    """Bound an engagement instant by the credit cap.

    Returns ``min(engaged_until, anchor + cap_seconds, now)``, floored at
    ``engagement_start``.

    Keyword-only on purpose: four datetime parameters passed positionally
    would type-check when swapped and silently change billed hours.

    ``anchor`` is the most recent real input (None only when the OS idle
    clock was unreadable — in production AfkSource skips sampling entirely in
    that case, so sources are never consulted with a None anchor; the
    ``engagement_start`` fallback still bounds that corner rather than
    granting unbounded credit).

    The floor: when the cap expired BEFORE the engagement began
    (``anchor + cap < engagement_start`` — e.g. a calendar-auto-launched
    meeting hours after the last real input), the engagement earns nothing —
    returning the bare cap end would fabricate an activity instant at a time
    when there was provably no engagement and no input, and AfkSource would
    fold up to a full afk-timeout of phantom active time around it.
    """
    effective_anchor = anchor or engagement_start
    if effective_anchor is None:
        return None
    cap_end = effective_anchor + timedelta(seconds=cap_seconds)
    credit = min(engaged_until, cap_end, now)
    if engagement_start is not None and credit < engagement_start:
        return None
    return credit
