"""Native OS notifications for BetterFlow Sync."""

import logging
import platform
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


def _send_macos(title: str, message: str, sound: bool) -> None:
    """Send notification via osascript on macOS."""
    # Escape double quotes and backslashes for AppleScript string literals.
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_message = message.replace("\\", "\\\\").replace('"', '\\"')

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
    # Escape single quotes for PowerShell string literals.
    safe_title = title.replace("'", "''")
    safe_message = message.replace("'", "''")

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
