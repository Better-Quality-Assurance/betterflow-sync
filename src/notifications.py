"""Native OS notifications for BetterFlow."""

import logging
import platform
import re
import subprocess

logger = logging.getLogger(__name__)


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
    """Remove all delivered notifications from Notification Center.

    Note: on macOS this is a no-op. The deprecated NSUserNotificationCenter
    API (removeAllDeliveredNotifications) has no effect on macOS 13+, and
    osascript-based notifications cannot be programmatically cleared.
    Windows toast notifications can be cleared via ToastNotificationManager.
    """
    system = platform.system()
    try:
        if system == "Windows":
            _clear_windows()
    except Exception as e:
        logger.debug(f"Failed to clear notifications: {e}")


def _send_macos(title: str, message: str, sound: bool) -> None:
    """Send notification via osascript on macOS.

    Uses 'display notification' which routes through the modern
    UserNotifications framework. The deprecated NSUserNotificationCenter
    API was removed because it causes ghost notifications on macOS 13+.
    """
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
        "CreateToastNotifier('BetterFlow').Show($toast)"
    )
    result = subprocess.run(
        ["powershell", "-Command", ps_script],
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        logger.debug(
            f"PowerShell notification failed (exit {result.returncode}): "
            f"{result.stderr.decode(errors='replace')[:200]}"
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
        logger.debug(f"[notifications] _clear_windows failed (rc={result.returncode}): {result.stderr.decode(errors='replace').strip()}")
