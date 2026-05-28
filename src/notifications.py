"""Native OS notifications for BetterFlow.

macOS uses pyobjc/NSUserNotification so the notifications are owned by
BetterFlow's own bundle identifier rather than `osascript` / Script Editor.
This means they:

1. Show up under "BetterFlow" in Notification Center (not Script Editor).
2. Can actually be cleared via ``clear_notifications()`` on startup and
   shutdown — the previous osascript-based path left ghosts forever.

We fall back to osascript if pyobjc is unavailable (dev environments
without the bundle); those fallback notifications behave like before.
"""

import logging
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_MACOS_PYOBJC_AVAILABLE: bool | None = None
_CACHED_ICON_PATH: Optional[Path] = None


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


def send_notification(title: str, message: str, sound: bool = True) -> None:
    """Send a native OS notification.

    Args:
        title: Notification title.
        message: Notification body text.
        sound: Whether to play a sound (macOS only).
    """
    system = platform.system()
    try:
        if system == "Darwin":
            if _try_load_macos_pyobjc() and _send_macos_pyobjc(title, message, sound):
                return
            _send_macos_osascript(title, message, sound)
        elif system == "Windows":
            _send_windows(title, message)
        elif system == "Linux":
            _send_linux(title, message)
        else:
            logger.debug("Notifications not supported on %s", system)
    except Exception as e:
        logger.warning("Failed to send notification: %s", e)


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


# macOS — pyobjc (preferred)


def _send_macos_pyobjc(title: str, message: str, sound: bool) -> bool:
    """Post a notification via NSUserNotification. Returns False on failure."""
    try:
        from Foundation import NSUserNotification, NSUserNotificationCenter
    except ImportError:
        return False
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

        center = NSUserNotificationCenter.defaultUserNotificationCenter()
        center.deliverNotification_(note)
        return True
    except Exception as e:
        logger.warning("pyobjc notification failed, will fall back: %s", e)
        return False


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


# macOS — osascript fallback (only used when pyobjc is unavailable)


def _send_macos_osascript(title: str, message: str, sound: bool) -> None:
    """Legacy osascript path. Notifications are attributed to Script Editor
    and CANNOT be cleared by ``clear_notifications()`` — use only as a
    last-resort fallback.
    """
    safe_title = (
        re.sub(r"[\x00-\x1f\x7f]", "", title)[:200].replace("\\", "\\\\").replace('"', '\\"')
    )
    safe_message = (
        re.sub(r"[\x00-\x1f\x7f]", "", message)[:500].replace("\\", "\\\\").replace('"', '\\"')
    )

    sound_clause = ' sound name "default"' if sound else ""
    script = f'display notification "{safe_message}" with title "{safe_title}"{sound_clause}'
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


# Windows


def _send_windows(title: str, message: str) -> None:
    """Send toast notification via PowerShell on Windows."""
    # Sanitize for PowerShell single-quoted string literals:
    # strip control chars, limit length, escape single quotes.
    safe_title = re.sub(r"[\x00-\x1f\x7f]", "", title)[:200].replace("'", "''")
    safe_message = re.sub(r"[\x00-\x1f\x7f]", "", message)[:500].replace("'", "''")

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


# Linux — notify-send (libnotify)


def _send_linux(title: str, message: str) -> None:
    """Send a desktop notification via notify-send (libnotify).

    Arguments are passed as an argv list (no shell), so notify-send handles
    quoting; we still strip control chars and cap length defensively. If
    notify-send is not installed we log at debug and return rather than raise.
    """
    safe_title = re.sub(r"[\x00-\x1f\x7f]", "", title)[:200]
    safe_message = re.sub(r"[\x00-\x1f\x7f]", "", message)[:500]

    args = ["notify-send", "--app-name=BetterFlow"]
    icon_path = _resolve_icon_path()
    if icon_path is not None:
        args.append(f"--icon={icon_path}")
    args.extend([safe_title, safe_message])

    try:
        result = subprocess.run(args, capture_output=True, timeout=5)
    except FileNotFoundError:
        logger.debug("notify-send not found — desktop notification skipped")
        return
    if result.returncode != 0:
        logger.warning(
            "notify-send failed (exit %s): %s",
            result.returncode,
            result.stderr.decode(errors="replace")[:200],
        )
