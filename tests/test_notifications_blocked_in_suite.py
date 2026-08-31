"""Guard: a normal test must not be able to post a real notification.

The suite reaches send_notification through break_manager, reminders,
system_event_handler, aw_manager and main. Before the conftest block those all
posted for real — on macOS via osascript, attributed to Script Editor, which
clear_notifications() cannot clear programmatically.
"""

import src.notifications as notifications


def test_senders_are_blocked_for_an_ordinary_test():
    for name in (
        "_send_macos_pyobjc",
        "_send_macos_osascript",
        "_send_windows",
        "_send_linux",
    ):
        fn = getattr(notifications, name)
        assert fn.__name__ == "_blocked", (
            f"{name} is the REAL sender during an ordinary test — a suite run "
            "can post notifications to the machine running it"
        )


def test_send_notification_does_not_reach_the_os(monkeypatch):
    """The public entry point still runs its own logic, but nothing escapes."""
    import subprocess
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    notifications.send_notification("Break Over", "Tracking resumed - welcome back!")
    assert called == [], f"a real subprocess call escaped: {called}"
