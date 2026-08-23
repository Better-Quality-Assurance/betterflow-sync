"""Native OS notifications for BetterFlow.

macOS uses pyobjc/NSUserNotification so the notifications are owned by
BetterFlow's own bundle identifier rather than `osascript` / Script Editor.
This means they:

1. Show up under "BetterFlow" in Notification Center (not Script Editor).
2. Can actually be cleared via ``clear_notifications()`` on startup and
   shutdown — the previous osascript-based path left ghosts forever.

We fall back to osascript if pyobjc is unavailable (dev environments
without the bundle); those fallback notifications behave like before.

What this module can and cannot observe
---------------------------------------

``NSUserNotificationCenter.deliverNotification_`` is fire-and-forget: it
returns ``None`` whether or not macOS kept the notification, so "the call
did not raise" has never been evidence that anyone was told. Two shipped
prompts (Rosetta, #188; Accessibility, #197) were load-bearing on exactly
that non-evidence.

The one readable signal macOS does offer is
``NSUserNotificationCenter.deliveredNotifications()``. It reflects the
system's own record — a separate process can read notifications posted by
the app — so tagging each notification with a unique identifier and
looking for it afterwards distinguishes *macOS kept this* from *macOS
discarded this*. That is what :func:`send_notification` now reports.

Deliberately NOT claimed:

* **That the user saw it.** Do Not Disturb / a Focus mode still files a
  notification into Notification Center without showing a banner, so
  ``DELIVERED`` means macOS accepted it, never that a human read it.
* **An authorisation status.** The legacy center exposes no public
  ``authorizationStatus``; the only authorisation-shaped selectors on it
  are private (``_shouldPresentNotification_`` and friends), so the state
  is inferred from the delivery read or not at all.
* **Anything about osascript, PowerShell or notify-send.** Those report
  that the *command* ran. Whether the OS then presented the notification
  is not readable, and a delivered-list entry cannot be attributed to one
  of them because none of them can be tagged. Those paths report
  ``UNKNOWN``, which is not a success.
"""

import logging
import platform
import re
import subprocess
import sys
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MACOS_PYOBJC_AVAILABLE: bool | None = None
_CACHED_ICON_PATH: Optional[Path] = None


class NotificationOutcome(Enum):
    """What we actually know about a notification we tried to post.

    Five values because five different things can be true, and collapsing
    any of them into "success" is the defect this enum exists to close.
    Each one implies a different follow-up:

    ``DELIVERED``
        The OS's own delivered-notification list contains the notification
        we posted, found by an identifier we set. macOS accepted it. This
        is NOT proof the user saw it — a Focus mode files it silently.
    ``SUPPRESSED``
        The post call completed without error and the OS then had no
        record of it. The user was not reached on this channel; something
        else has to say the thing.
    ``FAILED``
        The sending mechanism itself broke — an exception, a non-zero exit,
        a missing binary. The channel is broken, not the permission.
    ``UNKNOWN``
        The mechanism completed and offers no way to read the result back.
        Evidence of nothing, and deliberately not ``DELIVERED``.
    ``UNSUPPORTED``
        There is no notification channel here at all, so nothing was sent.
    """

    DELIVERED = "delivered"
    SUPPRESSED = "suppressed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"

    def __bool__(self) -> bool:
        """Truthy only for a verified delivery.

        Enum members are truthy by default, so without this a caller
        writing the obvious ``if send_notification(...):`` would treat
        ``FAILED`` as success — the exact bug this type exists to close,
        rebuilt one layer up. Fail closed: anything short of an observed
        delivery is falsy.
        """
        return self is NotificationOutcome.DELIVERED


# How hard to look for our own notification in the delivered list before
# concluding macOS dropped it. Delivery is asynchronous: measured at ~0.1s
# on a healthy machine, so this is a generous ceiling and the common case
# costs one sleep. Bounded because this runs on the notification path, and
# a notification is not worth blocking a caller for a second.
_DELIVERY_CONFIRM_ATTEMPTS = 6
_DELIVERY_CONFIRM_INTERVAL = 0.1


def _resolve_icon_path() -> Optional[Path]:
    """Locate the BetterFlow icon PNG for both bundled and dev runs.

    Cached after first resolution. Returns None if no icon is found — the
    notification still fires, just without the extra content image.
    """
    global _CACHED_ICON_PATH
    if _CACHED_ICON_PATH is not None:
        return _CACHED_ICON_PATH if _CACHED_ICON_PATH.exists() else None

    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "resources" / "icon.png")
    # Dev mode: src/notifications.py → src/ → repo root → resources/
    candidates.append(Path(__file__).resolve().parent.parent / "resources" / "icon.png")

    for candidate in candidates:
        if candidate.exists():
            _CACHED_ICON_PATH = candidate
            return candidate
    logger.debug("No notification icon found in %s", [str(c) for c in candidates])
    return None


def _try_load_macos_pyobjc() -> bool:
    """Return True if the pyobjc NSUserNotification path is usable.

    Result is cached. Failure is logged once at warning level so dev runs
    without pyobjc aren't noisy on every notification.
    """
    global _MACOS_PYOBJC_AVAILABLE
    if _MACOS_PYOBJC_AVAILABLE is not None:
        return _MACOS_PYOBJC_AVAILABLE
    try:
        import Foundation  # noqa: F401 — probing availability
        _MACOS_PYOBJC_AVAILABLE = True
    except ImportError as e:
        logger.warning(
            "pyobjc unavailable (%s) — falling back to osascript notifications "
            "which will be attributed to Script Editor in Notification Center",
            e,
        )
        _MACOS_PYOBJC_AVAILABLE = False
    return _MACOS_PYOBJC_AVAILABLE


def send_notification(
    title: str, message: str, sound: bool = True
) -> NotificationOutcome:
    """Send a native OS notification and report what we know about it.

    The return value is the point of this function. Callers that carry a
    user instruction the agent cannot otherwise deliver — the Rosetta
    notice (#188) and the Accessibility notice (#197) — must read it,
    because a notification macOS discarded and one it presented used to be
    indistinguishable from here.

    Never raises: a notification that cannot be sent must not stop the
    caller. Failures come back as :class:`NotificationOutcome` values
    instead, so "it did not blow up" stops being mistaken for "it worked".

    Args:
        title: Notification title.
        message: Notification body text.
        sound: Whether to play a sound (macOS only).

    Returns:
        The strongest claim the platform actually supports. Only
        ``DELIVERED`` means the OS kept the notification, and even that is
        not proof the user saw it — see the module docstring.
    """
    system = platform.system()
    try:
        if system == "Darwin":
            outcome = NotificationOutcome.UNKNOWN
            if _try_load_macos_pyobjc():
                outcome = _send_macos_pyobjc(title, message, sound)
                if outcome is NotificationOutcome.DELIVERED:
                    return outcome
            # Reachable again. It never was while the pyobjc path returned a
            # bare True, so a suppressed notification had no second chance.
            # osascript posts under Script Editor's identity, which carries
            # its own notification permission, so it is a genuinely
            # different channel rather than a retry of the same one.
            fallback = _send_macos_osascript(title, message, sound)
            if fallback is NotificationOutcome.FAILED:
                # Both channels are accounted for; keep the pyobjc verdict
                # when it was the more specific one.
                return (
                    outcome
                    if outcome is NotificationOutcome.SUPPRESSED
                    else NotificationOutcome.FAILED
                )
            # osascript ran and cannot be read back. That downgrades a
            # SUPPRESSED verdict to UNKNOWN rather than confirming it: we
            # posted a second time on a channel we cannot observe.
            return fallback
        elif system == "Windows":
            return _send_windows(title, message)
        elif system == "Linux":
            return _send_linux(title, message)
        else:
            logger.debug("Notifications not supported on %s", system)
            return NotificationOutcome.UNSUPPORTED
    except Exception as e:
        logger.warning("Failed to send notification: %s", e)
        return NotificationOutcome.FAILED


def clear_notifications() -> None:
    """Remove all delivered notifications owned by BetterFlow.

    On macOS this clears notifications posted via the pyobjc path. We
    cannot clear osascript/Script-Editor-attributed notifications from a
    previous fallback run — those must be dismissed manually.

    On Windows this clears BetterFlow toast history.
    """
    system = platform.system()
    try:
        if system == "Darwin":
            if _try_load_macos_pyobjc():
                _clear_macos_pyobjc()
        elif system == "Windows":
            _clear_windows()
    except Exception as e:
        logger.warning("Failed to clear notifications: %s", e)


# --------------------------------------------------------------------------
# macOS — pyobjc (preferred)
# --------------------------------------------------------------------------


def _send_macos_pyobjc(
    title: str, message: str, sound: bool
) -> NotificationOutcome:
    """Post a notification via NSUserNotification and verify it landed.

    ``deliverNotification_`` returns nothing, so the post itself proves
    nothing. We tag the notification with a one-shot identifier and then
    look for that identifier in the center's delivered list, which is the
    OS's own record and is readable across processes.

    Returns ``DELIVERED`` only on a positive read. An absent identifier is
    ``SUPPRESSED``; a broken post is ``FAILED``; a post that succeeded
    while the read-back broke is ``UNKNOWN``, because at that point we
    genuinely do not know and saying either of the other two would be a
    guess wearing a verdict's clothes.
    """
    try:
        from Foundation import NSUserNotification, NSUserNotificationCenter
    except ImportError:
        return NotificationOutcome.FAILED
    try:
        note = NSUserNotification.alloc().init()
        note.setTitle_(title)
        note.setInformativeText_(message)
        if sound:
            # NSUserNotificationDefaultSoundName is a module-level string constant.
            note.setSoundName_("NSUserNotificationDefaultSoundName")

        # Attach the BetterFlow logo as the notification's content image.
        # Note: macOS draws the *app* icon on the left from the bundle
        # identifier — that one comes for free in .app builds because
        # Info.plist points at icon.icns. contentImage is the secondary
        # image shown on the right side of the banner.
        icon_path = _resolve_icon_path()
        if icon_path is not None:
            try:
                from AppKit import NSImage
                ns_image = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
                if ns_image is not None:
                    note.setContentImage_(ns_image)
            except Exception as e:
                logger.debug("Failed to attach notification icon: %s", e)

        # Unique per send, so the read below is attributable to THIS post
        # and macOS never coalesces two notices into one. Notifications
        # posted before this change carry no identifier at all, which is
        # why nothing could be attributed to a send.
        marker = f"betterflow-{uuid.uuid4()}"
        note.setIdentifier_(marker)

        center = NSUserNotificationCenter.defaultUserNotificationCenter()
        if center is None:
            # Nil for a process the notification system will not talk to.
            # Nothing was posted, so this is not merely unverifiable.
            logger.warning("No NSUserNotificationCenter available — nothing was posted")
            return NotificationOutcome.FAILED
        center.deliverNotification_(note)
    except Exception as e:
        logger.warning("pyobjc notification failed, will fall back: %s", e)
        return NotificationOutcome.FAILED

    return _confirm_macos_delivery(marker)


def _confirm_macos_delivery(marker: str) -> NotificationOutcome:
    """Look for ``marker`` in macOS's delivered-notification list.

    Split out from the post so a test can drive the read on its own, and
    so the read's own failure cannot be mistaken for a delivery verdict.

    A false ``SUPPRESSED`` is possible and cheap: if the user (or our own
    ``clear_notifications()``) clears Notification Center inside the poll
    window we lose sight of a notification that really was delivered. The
    cost is one extra osascript post and a warning line. The opposite
    error — reporting delivery we never saw — is the one that shipped two
    notices to nobody, so the read is deliberately biased this way.
    """
    try:
        from Foundation import NSUserNotificationCenter
    except ImportError:
        return NotificationOutcome.UNKNOWN
    try:
        center = NSUserNotificationCenter.defaultUserNotificationCenter()
        if center is None:
            return NotificationOutcome.UNKNOWN
        for attempt in range(_DELIVERY_CONFIRM_ATTEMPTS):
            if attempt:
                time.sleep(_DELIVERY_CONFIRM_INTERVAL)
            for delivered in center.deliveredNotifications():
                if delivered.identifier() == marker:
                    return NotificationOutcome.DELIVERED
    except Exception as e:
        # The post may well have worked. We simply cannot see, and
        # "cannot see" is not "delivered" and is not "suppressed".
        logger.warning("Could not read back macOS notification delivery: %s", e)
        return NotificationOutcome.UNKNOWN

    logger.warning(
        "macOS accepted a notification and kept no record of it — it was not "
        "shown to the user (title suppressed here; check notification "
        "permission for this app)"
    )
    return NotificationOutcome.SUPPRESSED


def _clear_macos_pyobjc() -> None:
    """Remove every BetterFlow-owned delivered notification."""
    try:
        from Foundation import NSUserNotificationCenter
    except ImportError:
        return
    try:
        center = NSUserNotificationCenter.defaultUserNotificationCenter()
        center.removeAllDeliveredNotifications()
    except Exception as e:
        logger.warning("pyobjc clear failed: %s", e)


# --------------------------------------------------------------------------
# macOS — osascript fallback (only used when pyobjc is unavailable)
# --------------------------------------------------------------------------


def _send_macos_osascript(
    title: str, message: str, sound: bool
) -> NotificationOutcome:
    """Legacy osascript path. Notifications are attributed to Script Editor
    and CANNOT be cleared by ``clear_notifications()`` — use only as a
    last-resort fallback.

    Reports ``UNKNOWN`` on a clean exit, never ``DELIVERED``. ``osascript``
    exits 0 once the AppleScript ran; it says nothing about whether macOS
    then presented the notification. The delivered-list read used by the
    pyobjc path cannot rescue this either — ``display notification`` takes
    no identifier, so a delivered entry cannot be attributed to this call
    rather than to the pyobjc attempt that preceded it.
    """
    safe_title = re.sub(r'[\x00-\x1f\x7f]', '', title)[:200].replace("\\", "\\\\").replace('"', '\\"')
    safe_message = re.sub(r'[\x00-\x1f\x7f]', '', message)[:500].replace("\\", "\\\\").replace('"', '\\"')

    sound_clause = ' sound name "default"' if sound else ""
    script = (
        f'display notification "{safe_message}" '
        f'with title "{safe_title}"{sound_clause}'
    )
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        timeout=5,
    )
    if result.returncode != 0:
        logger.warning(
            "osascript notification failed (exit %s): %s",
            result.returncode,
            result.stderr.decode(errors="replace")[:200],
        )
        return NotificationOutcome.FAILED
    return NotificationOutcome.UNKNOWN


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------


def _send_windows(title: str, message: str) -> NotificationOutcome:
    """Send toast notification via PowerShell on Windows.

    ``ToastNotifier.Show`` returns void and Focus Assist can swallow a
    toast without telling anyone, so a clean PowerShell exit buys
    ``UNKNOWN`` and nothing better. Windows does expose a readable
    ``ToastNotifier.Setting`` (``NotificationsDisabledByUser`` and
    friends); wiring that up would earn a real verdict here, and until
    someone does, this must not claim one.
    """
    # Sanitize for PowerShell single-quoted string literals:
    # strip control chars, limit length, escape single quotes.
    safe_title = re.sub(r'[\x00-\x1f\x7f]', '', title)[:200].replace("'", "''")
    safe_message = re.sub(r'[\x00-\x1f\x7f]', '', message)[:500].replace("'", "''")

    # Upgrade from ToastText02 to ToastImageAndText02 when we have an icon
    # available — same two-line layout plus a logo on the left.
    icon_path = _resolve_icon_path()
    if icon_path is not None:
        # Toast image URIs must be absolute file:/// URIs on Windows.
        icon_uri = f"file:///{str(icon_path).replace(chr(92), '/').lstrip('/')}"
        safe_icon = icon_uri.replace("'", "''")
        template_name = "ToastImageAndText02"
        image_node_setup = (
            "$imageNodes = $template.GetElementsByTagName('image'); "
            f"$imageNodes.Item(0).Attributes.GetNamedItem('src').NodeValue = '{safe_icon}'; "
        )
    else:
        template_name = "ToastText02"
        image_node_setup = ""

    ps_script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] > $null; "
        "$template = [Windows.UI.Notifications.ToastNotificationManager]::"
        f"GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::{template_name}); "
        "$textNodes = $template.GetElementsByTagName('text'); "
        f"$textNodes.Item(0).AppendChild($template.CreateTextNode('{safe_title}')) > $null; "
        f"$textNodes.Item(1).AppendChild($template.CreateTextNode('{safe_message}')) > $null; "
        f"{image_node_setup}"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        "CreateToastNotifier('BetterFlow').Show($toast)"
    )
    result = subprocess.run(
        ["powershell", "-Command", ps_script],
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        logger.warning(
            "PowerShell notification failed (exit %s): %s",
            result.returncode,
            result.stderr.decode(errors="replace")[:200],
        )
        return NotificationOutcome.FAILED
    return NotificationOutcome.UNKNOWN


def _clear_windows() -> None:
    """Clear toast notifications on Windows."""
    ps_script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] > $null; "
        "$history = [Windows.UI.Notifications.ToastNotificationManager]::History; "
        "$history.Clear('BetterFlow')"
    )
    result = subprocess.run(
        ["powershell", "-Command", ps_script],
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        logger.warning(
            "_clear_windows failed (rc=%s): %s",
            result.returncode,
            result.stderr.decode(errors="replace").strip(),
        )


# --------------------------------------------------------------------------
# Linux — notify-send (libnotify)
# --------------------------------------------------------------------------


def _send_linux(title: str, message: str) -> NotificationOutcome:
    """Send a desktop notification via notify-send (libnotify).

    Arguments are passed as an argv list (no shell), so notify-send handles
    quoting; we still strip control chars and cap length defensively. If
    notify-send is not installed we log at debug and return rather than raise.

    A clean exit is ``UNKNOWN``: notify-send returns as soon as the
    notification daemon accepts the D-Bus call, which is upstream of
    whether anything was rendered.
    """
    safe_title = re.sub(r'[\x00-\x1f\x7f]', '', title)[:200]
    safe_message = re.sub(r'[\x00-\x1f\x7f]', '', message)[:500]

    args = ["notify-send", "--app-name=BetterFlow"]
    icon_path = _resolve_icon_path()
    if icon_path is not None:
        args.append(f"--icon={icon_path}")
    args.extend([safe_title, safe_message])

    try:
        result = subprocess.run(args, capture_output=True, timeout=5)
    except FileNotFoundError:
        logger.debug("notify-send not found — desktop notification skipped")
        return NotificationOutcome.FAILED
    if result.returncode != 0:
        logger.warning(
            "notify-send failed (exit %s): %s",
            result.returncode,
            result.stderr.decode(errors="replace")[:200],
        )
        return NotificationOutcome.FAILED
    return NotificationOutcome.UNKNOWN
