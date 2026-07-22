"""The one-time in-app privacy notice — a legal artifact, not a UI nicety.

Romanian Law 190/2018 art. 5 lit. b requires *complete and explicit prior
information* of employees before workplace electronic monitoring. The agent has
always carried that disclosure, but only on the first-run path: ``_show_consent``
(Windows/Linux) and the Input Monitoring gate's ``_draw_disclosure_columns``
(macOS). Every device already in the fleet onboarded before the current text
existed, so the installed base has never seen it — and macOS never runs the
consent screen at all, so the entire Mac fleet has only ever seen the permission
gate. This module closes that gap and, just as importantly, produces the
*record* that the condition was met.

Four properties this module exists to guarantee:

1. **The text is data, not markup.** ``NOTICE_SECTIONS`` is the single source;
   the window renders it and the version hashes it. There is no second copy for
   the two to drift apart on (``one-rule-one-implementation.md``).

2. **A text change cannot ship without a version change.** ``NOTICE_VERSION``
   is *derived* from a SHA-256 of the canonical text at import. It is not a
   constant a developer edits and can forget — forgetting is impossible. That
   matters because the failure being fixed is exactly "a new data category
   reached devices that had already acknowledged the old text".

3. **The mandatory qualifiers are asserted, not trusted.** Three phrases do
   legal work rather than describe a feature, and a future round of "make it
   fit on one screen" would delete them first. ``REQUIRED_QUALIFIERS`` is
   enforced by a test.

4. **Nothing here can block work.** Pure functions over a ``Config``; the only
   side effect is ``config.save()`` in ``record_acknowledgement``. The caller
   wraps everything — an unshowable notice must never stop tracking, syncing,
   or billing, on the same principle as the hardware-serial probe.

Authoritative source: Regulament Intern art. 64^1 alin. (4) and (5), in
Romanian. The agent UI is English, so the notice is English — but if the two
ever disagree, **the Romanian wins**; it is the version employees sign. That is
stated inside the notice itself rather than only in this docstring.
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Human-readable half of the version, for ordering records by eye. It carries no
# correctness weight: the digest below is what actually guarantees a text change
# is visible, so forgetting to bump this cannot make an edit invisible.
NOTICE_REVISION = "r1"

NOTICE_TITLE = "What BetterFlow records on this computer"

NOTICE_SUBTITLE = (
    "BetterFlow is already installed on this computer. This is the full "
    "description of what it records. Please read it once."
)

# Each section is (lead paragraph, bullets). Bullets may be empty for a section
# that is a plain paragraph. This structure IS the notice — the renderer walks
# it and the version hashes it, so neither can quietly disagree with the other.
NOTICE_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "What BetterFlow records while it is running on your work computer:",
        (
            "The name of the application you are using and the title of the "
            "active window",
            "Web addresses, reduced to the domain only (for example "
            "github.com, not the full page address)",
            # QUALIFIER: without "Counts only ... not which keys you press"
            # this bullet reads as a keylogger.
            "Counts of keystrokes, mouse clicks and scrolls. Counts only. The "
            "application does not record which keys you press",
            "Whether the computer is active or idle",
            # QUALIFIER: "identifies the machine" is the entire justification
            # for collecting a durable hardware identifier.
            "The computer's hardware serial number, which identifies the "
            "machine so IT can match it to the company asset list",
            "Technical details needed to keep the application working: the "
            "application version and the computer's time zone",
        ),
    ),
    (
        # QUALIFIER: "as recorded". Dropping it implies titles are anonymised
        # before they leave the machine. They are not — there is no client-side
        # title hashing in the agent, and the `hash_titles` setting NAME already
        # misleads people on exactly this point (it is a preference forwarded to
        # the server, not a local transform). See CLAUDE.md, Privacy Model.
        "Window titles are sent to our servers as recorded and are categorised "
        "there rather than on the computer. A window title can contain the name "
        "of a document or a web page you have open.",
        (),
    ),
    (
        "What it does not record:",
        (
            "The keys you press or the text you type",
            "Passwords or message content",
            "Screenshots",
            "Anything from excluded applications. Password managers and system "
            "security prompts — including 1Password, Keychain Access and System "
            "Settings — are excluded by default",
            "Full web addresses",
        ),
    ),
    (
        "Questions or objections: write to dpo@betterqa.co.",
        (),
    ),
    (
        "This notice reflects Regulamentul Intern, art. 64^1 alin. (4) and (5). "
        "The Romanian text is the version you signed and is the one that "
        "prevails if the two ever differ.",
        (),
    ),
)

# Phrases that must survive any future edit. Each is doing legal work; each is
# the first thing a "make it shorter" pass would delete. Enforced by
# tests/test_privacy_notice.py, which is the point — a comment warning about a
# trap does not stop the trap (diagnosis-discipline.md).
REQUIRED_QUALIFIERS: tuple[str, ...] = (
    # Titles leave the machine unmodified.
    "as recorded",
    # Input is counted, not captured.
    "Counts only. The application does not record which keys you press",
    # The serial identifies the machine, not the person.
    "identifies the machine",
)

ACK_BUTTON_TEXT = "I have read this"


def notice_lines() -> tuple[str, ...]:
    """Render the notice as display lines, bullets prefixed.

    The single renderer. The Tk window walks this and the version hashes the
    joined result, so what the user reads and what gets recorded are the same
    bytes by construction rather than by review.
    """
    lines: list[str] = []
    for lead, bullets in NOTICE_SECTIONS:
        if lines:
            lines.append("")
        lines.append(lead)
        for bullet in bullets:
            lines.append(f"•  {bullet}")
    return tuple(lines)


def notice_text() -> str:
    """The canonical notice as one string — what the version is computed over."""
    return "\n".join((NOTICE_TITLE, NOTICE_SUBTITLE, *notice_lines()))


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# Derived, never hand-written. Editing a single character of the notice changes
# this, which re-shows the notice to every device that acknowledged the old text.
# That is the whole mechanism: a developer CANNOT change the copy and forget the
# version, because there is no version field to forget.
NOTICE_VERSION = f"{NOTICE_REVISION}-{_digest(notice_text())}"


def needs_acknowledgement(config) -> bool:
    """Has this device acknowledged the notice text it is about to be shown?

    Compared by VALUE against the current version, never by truthiness: a device
    holding an older version is exactly the case that must re-show, and a
    truthiness check ("has some ack") would swallow it — which is the defect
    this whole feature exists to prevent.
    """
    recorded = getattr(config, "privacy_notice_ack_version", None)
    return recorded != NOTICE_VERSION


def record_acknowledgement(config, *, now: Optional[datetime] = None) -> None:
    """Persist that the user acknowledged the CURRENT text, and when.

    ``now`` is injectable so tests need no wall-clock fixture. Stored UTC ISO
    8601 — the timestamp is evidence, so it carries an explicit offset rather
    than a naive local reading nobody can later interpret.
    """
    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    config.privacy_notice_ack_version = NOTICE_VERSION
    config.privacy_notice_ack_at = when.astimezone(timezone.utc).isoformat()
    config.save()
    logger.info(
        "Privacy notice acknowledged (version=%s at=%s)",
        config.privacy_notice_ack_version,
        config.privacy_notice_ack_at,
    )


def acknowledgement_telemetry(config) -> Optional[dict]:
    """The heartbeat payload for a recorded acknowledgement, or ``None``.

    Reported on EVERY heartbeat for as long as an acknowledgement exists, not
    once. There is deliberately no delivery state machine here: the server-side
    reader ships separately and later, so a send-once design would silently lose
    every acknowledgement made before that deploy — the exact
    write-with-no-reader failure in one-rule-one-implementation.md, inverted.
    An idempotent ~80-byte upsert on a 2.5-minute cadence is the cheaper trade.

    ``device_id`` rides along so the record is self-describing; the user is
    identified by the per-device token the heartbeat is authenticated with.
    """
    version = getattr(config, "privacy_notice_ack_version", None)
    acknowledged_at = getattr(config, "privacy_notice_ack_at", None)
    # Both halves or nothing: a version with no timestamp is not evidence of
    # delivery, and reporting it would let a half-written record read as proof.
    if version is None or acknowledged_at is None:
        return None
    payload = {"version": version, "acknowledged_at": acknowledged_at}
    device_id = getattr(config, "device_id", None)
    if device_id:
        payload["device_id"] = device_id
    return payload
