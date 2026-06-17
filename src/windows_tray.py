"""Windows 11 system-tray icon promotion.

Windows 11 stores each tray icon's "show on the taskbar vs. hide in the overflow
flyout" state under ``HKCU\\Control Panel\\NotifyIconSettings\\<id>``. Every
subkey carries an ``ExecutablePath`` value (which app the icon belongs to) and an
``IsPromoted`` DWORD (1 = always shown on the taskbar, 0 = hidden in the ``^``
overflow). There is no documented API for this, but the Settings app toggles
exactly this value — so writing ``IsPromoted=1`` for our own executable promotes
the icon onto the taskbar with no user action.

This only works because the icon now ships from a stable install path (the
one-dir build): the subkey is keyed to the executable, so a path that changed
every launch (the retired one-file ``%TEMP%\\_MEIxxxxx`` layout) could never be
matched or persisted.

Best-effort and Windows-only: a silent no-op in dev mode, on non-Windows, and on
Windows 10 (which uses a different, encrypted ``IconStreams`` store this does not
touch). Explorer reads ``IsPromoted`` when the icon is (re)added, so a freshly
installed icon may sit in the overflow for its first session and surface on the
taskbar from the next launch on.
"""

import logging
import sys

logger = logging.getLogger(__name__)

# HKCU subkey holding the Windows 11 per-icon promotion state.
_NOTIFY_ICON_SETTINGS = r"Control Panel\NotifyIconSettings"


def _normalize(path: str) -> str:
    """Case- and separator-normalized Windows path for robust comparison.

    Done explicitly (not via ``os.path.normcase``) so the comparison applies
    Windows semantics — case-insensitive, ``/`` and ``\\`` equivalent —
    regardless of the host OS the tests run on.
    """
    return path.replace("/", "\\").rstrip("\\").lower()


def select_entries_to_promote(entries, exe_path):
    """Return the subkey names whose icon belongs to ``exe_path`` and is not
    already promoted.

    Pure (no registry I/O) so the matching logic is unit-testable.

    Args:
        entries: iterable of ``(subkey_name, executable_path, is_promoted)``.
            ``executable_path`` may be None when the subkey lacks the value;
            ``is_promoted`` is the int DWORD (or None if absent).
        exe_path: our running executable's path.

    Returns:
        list of subkey names to set ``IsPromoted=1`` on.
    """
    target = _normalize(exe_path)
    out = []
    for name, executable_path, is_promoted in entries:
        if not executable_path:
            continue
        if _normalize(executable_path) != target:
            continue
        if is_promoted == 1:
            continue  # already on the taskbar — leave it alone
        out.append(name)
    return out


def _read_entries(winreg, root):
    """Yield ``(subkey_name, executable_path, is_promoted)`` for each icon."""
    i = 0
    while True:
        try:
            name = winreg.EnumKey(root, i)
        except OSError:
            break  # no more subkeys
        i += 1
        try:
            with winreg.OpenKey(root, name) as sub:
                try:
                    executable_path, _ = winreg.QueryValueEx(sub, "ExecutablePath")
                except FileNotFoundError:
                    executable_path = None
                try:
                    is_promoted, _ = winreg.QueryValueEx(sub, "IsPromoted")
                except FileNotFoundError:
                    is_promoted = None
        except OSError as e:
            logger.debug("Skipping NotifyIconSettings subkey %s: %s", name, e)
            continue
        yield name, executable_path, is_promoted


def promote_tray_icon(exe_path=None):
    """Promote our tray icon onto the Windows 11 taskbar (best-effort).

    Returns True once our icon's entry has been found (whether it had to be
    promoted or was already promoted), so the caller can stop retrying. Returns
    False when no entry for our executable exists yet (Explorer has not created
    it — worth retrying) or on any error (e.g. Windows 10, missing key).
    """
    if sys.platform != "win32":
        return False
    if exe_path is None:
        exe_path = sys.executable

    try:
        import winreg
    except ImportError:  # not on Windows
        return False

    try:
        root = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _NOTIFY_ICON_SETTINGS,
            0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE,
        )
    except OSError as e:
        # Key absent on Windows 10 and before the first icon is ever shown.
        logger.debug("NotifyIconSettings key unavailable: %s", e)
        return False

    try:
        entries = list(_read_entries(winreg, root))
        matched = any(
            ep and _normalize(ep) == _normalize(exe_path) for _, ep, _ in entries
        )
        to_promote = select_entries_to_promote(entries, exe_path)
        for name in to_promote:
            try:
                with winreg.OpenKey(root, name, 0, winreg.KEY_SET_VALUE) as sub:
                    winreg.SetValueEx(sub, "IsPromoted", 0, winreg.REG_DWORD, 1)
                logger.info("Promoted tray icon to the taskbar (NotifyIconSettings\\%s)", name)
            except OSError as e:
                logger.warning("Failed to set IsPromoted on %s: %s", name, e)
        return matched
    finally:
        winreg.CloseKey(root)
