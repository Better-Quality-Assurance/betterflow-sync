"""Native OS notifications for BetterFlow Sync."""

import logging
import platform
import subprocess

logger = logging.getLogger(__name__)

# macOS native notification center (lazy-initialized)
_mac_center = None


def _get_mac_center():
    """Get the macOS NSUserNotificationCenter singleton."""
    global _mac_center
    if _mac_center is not None:
        return _mac_center
    try:
        import objc

        NSUserNotificationCenter = objc.lookUpClass("NSUserNotificationCenter")
        _mac_center = NSUserNotificationCenter.defaultUserNotificationCenter()
        return _mac_center
    except Exception:
        return None


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
            _send_macos(title, message, sound)
        elif system == "Windows":
            _send_windows(title, message)
        else:
            logger.debug(f"Notifications not supported on {system}")
    except Exception as e:
        logger.debug(f"Failed to send notification: {e}")


def clear_notifications() -> None:
    """Remove all delivered notifications from Notification Center."""
    system = platform.system()
    try:
        if system == "Darwin":
            center = _get_mac_center()
            if center:
                center.removeAllDeliveredNotifications()
                logger.debug("Cleared all delivered notifications")
        elif system == "Windows":
            _clear_windows()
    except Exception as e:
        logger.debug(f"Failed to clear notifications: {e}")


def _send_macos(title: str, message: str, sound: bool) -> None:
    """Send notification via NSUserNotificationCenter on macOS."""
    center = _get_mac_center()
    if center:
        try:
            import objc

            NSUserNotification = objc.lookUpClass("NSUserNotification")
            notification = NSUserNotification.alloc().init()
            notification.setTitle_(title)
            notification.setInformativeText_(message)
            if sound:
                notification.setSoundName_("default")
            center.deliverNotification_(notification)
            return
        except Exception as e:
            logger.debug(f"NSUserNotification failed, falling back to osascript: {e}")

    # Fallback to osascript
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")
    safe_message = message.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")

    sound_clause = ' sound name "default"' if sound else ""
    script = (
        f'display notification "{safe_message}" '
        f'with title "{safe_title}"{sound_clause}'
    )
    subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        timeout=5,
    )


def _send_windows(title: str, message: str) -> None:
    """Send toast notification via PowerShell on Windows."""
    # Sanitize for PowerShell single-quoted string literals:
    # strip control chars, limit length, escape single quotes.
    import re
    safe_title = re.sub(r'[\x00-\x1f\x7f]', '', title)[:200].replace("'", "''")
    safe_message = re.sub(r'[\x00-\x1f\x7f]', '', message)[:500].replace("'", "''")

    ps_script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] > $null; "
        "$template = [Windows.UI.Notifications.ToastNotificationManager]::"
        "GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        "$textNodes = $template.GetElementsByTagName('text'); "
        f"$textNodes.Item(0).AppendChild($template.CreateTextNode('{safe_title}')) > $null; "
        f"$textNodes.Item(1).AppendChild($template.CreateTextNode('{safe_message}')) > $null; "
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($template); "
        "[Windows.UI.Notifications.ToastNotificationManager]::"
        "CreateToastNotifier('BetterFlow Sync').Show($toast)"
    )
    subprocess.run(
        ["powershell", "-Command", ps_script],
        capture_output=True,
        timeout=10,
    )


def _clear_windows() -> None:
    """Clear toast notifications on Windows."""
    ps_script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] > $null; "
        "$history = [Windows.UI.Notifications.ToastNotificationManager]::History; "
        "$history.Clear('BetterFlow Sync')"
    )
    subprocess.run(
        ["powershell", "-Command", ps_script],
        capture_output=True,
        timeout=10,
    )
