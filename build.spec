# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for BetterFlow."""

import platform
import re
import sys
from datetime import date
from pathlib import Path

block_cipher = None

# Allow overriding target architecture via environment variable (e.g. x86_64, arm64)
import os
TARGET_ARCH = os.environ.get("TARGET_ARCH") or None

# Read version from src/__init__.py (single source of truth)
_version_file = Path(SPECPATH) / "src" / "__init__.py"
_version_match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', _version_file.read_text())
APP_VERSION = _version_match.group(1) if _version_match else "0.0.0"

# Stamp build metadata into _build_info.py
_build_info = Path(SPECPATH) / "src" / "_build_info.py"
_build_info.write_text(
    f'"""Build metadata - regenerated at build time."""\n\n'
    f'APP_VERSION = "{APP_VERSION}"\n'
    f'BUILD_DATE = "{date.today().isoformat()}"\n'
)

# Stamp the (optional) error-reporting DSN into a gitignored module so it ships
# inside the bundle but never lands in source control. Values are empty unless
# the corresponding BETTERFLOW_ERROR_* env vars are set on the build machine.
_build_secrets = Path(SPECPATH) / "src" / "_build_secrets.py"
_build_secrets.write_text(
    '"""Build-time secrets — generated, gitignored, never committed."""\n\n'
    f'ERROR_DSN = {os.environ.get("BETTERFLOW_ERROR_DSN")!r}\n'
    f'ERROR_ENDPOINT = {os.environ.get("BETTERFLOW_ERROR_ENDPOINT")!r}\n'
    f'ERROR_ENV = {os.environ.get("BETTERFLOW_ERROR_ENV")!r}\n'
)

# Determine platform
is_mac = platform.system() == "Darwin"
is_windows = platform.system() == "Windows"
is_linux = platform.system() == "Linux"

# Paths
root_dir = Path(SPECPATH)
src_dir = root_dir / "src"
resources_dir = root_dir / "resources"

# Data files
datas = [
    (str(resources_dir), "resources"),
]

# Tracker binaries (included as binaries to preserve execute permissions)
if is_mac:
    aw_platform = "darwin"
elif is_windows:
    aw_platform = "windows"
else:
    aw_platform = "linux"
aw_dir = resources_dir / "trackers" / aw_platform
aw_binaries = []
if aw_dir.exists():
    for binary in aw_dir.rglob("*"):
        if binary.is_file():
            rel_parent = binary.relative_to(aw_dir).parent
            target_dir = Path("resources/trackers") / aw_platform / rel_parent
            aw_binaries.append((str(binary), str(target_dir)))

# Collect Tcl/Tk data files so tkinter works in the bundle
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs
tcl_tk_datas = collect_data_files("tkinter")
tcl_tk_binaries = collect_dynamic_libs("_tkinter")

# Platform-specific hidden imports (tray backend, keyring backend, native libs).
if is_mac:
    platform_hiddenimports = [
        "pystray._darwin",
        "keyring.backends.macOS",
        # macOS frameworks for in-process watchers (CGEventTap, AX API, etc.)
        "Quartz",
        "CoreFoundation",
        "AppKit",
        "Foundation",
        "SystemConfiguration",
        "ApplicationServices",
    ]
elif is_windows:
    platform_hiddenimports = [
        "pystray._win32",
        "keyring.backends.Windows",
    ]
else:  # Linux
    platform_hiddenimports = [
        "pystray._appindicator",
        "pystray._xorg",
        "gi",
        "gi.repository.Gtk",
        "gi.repository.AyatanaAppIndicator3",
        "Xlib",
        "Xlib.support.unix_connect",
        "keyring.backends.SecretService",
        "secretstorage",
        "jeepney",
        "jeepney.io.blocking",
        "jeepney.bus_messages",
    ]

# Hidden imports for pystray, keyring backends, and our modules
hiddenimports = [
    *platform_hiddenimports,
    "tkinter",
    "_tkinter",
    "PIL._tkinter_finder",
    "apscheduler.triggers.interval",
    "apscheduler.schedulers.background",
    # Our modules (absolute imports from src/)
    "config",
    "sync",
    "sync.aw_client",
    "sync.bf_client",
    "sync.sync_engine",
    "sync.queue",
    "sync.retry",
    "sync.protocols",
    "auth",
    "auth.keychain",
    "auth.config_access",
    "auth.login",
    "ui",
    "ui.tray",
    "ui.permissions",
    "ui.setup_wizard",
    "aw_manager",
    "autostart",
    "windows_tray",  # Windows 11 tray-icon promotion (best-effort, win-only)
    "error_reporter",  # Failure reporting to betterqa-bot logs channel
    "_build_secrets",  # Generated at build time (baked DSN); gitignored
    "display_info",
    "reminders",
    "notifications",
    "self_updater",
    "update_checker",
    "system_events",
    "auth.browser_auth",
    "sync.macos_window_watcher",
    "sync.macos_input_watcher",
    "_build_info",  # Generated at build time by the spec preamble
]

a = Analysis(
    [str(src_dir / "entry_point.py")],
    pathex=[str(root_dir), str(src_dir)],
    binaries=aw_binaries + tcl_tk_binaries,
    datas=datas + tcl_tk_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
        "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if is_mac:
    exe = EXE(
        pyz,
        a.scripts,
        exclude_binaries=True,
        name="BetterFlow",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=TARGET_ARCH,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(resources_dir / "icon.icns") if (resources_dir / "icon.icns").exists() else None,
    )

    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=True,
        name="BetterFlow",
    )

    app = BUNDLE(
        coll,
        name="BetterFlow.app",
        icon=str(resources_dir / "icon.icns") if (resources_dir / "icon.icns").exists() else None,
        bundle_identifier="co.betterqa.betterflow",
        info_plist={
            "CFBundleName": "BetterFlow",
            "CFBundleDisplayName": "BetterFlow",
            "CFBundleVersion": APP_VERSION,
            "CFBundleShortVersionString": APP_VERSION,
            "LSUIElement": True,  # Hide from dock (menu bar app)
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "10.15",
            "NSRequiresAquaSystemAppearance": True,
            "NSAppleEventsUsageDescription": "BetterFlow needs this to track your active applications for time tracking.",
        },
    )

elif is_windows:
    # One-dir build (matches macOS/Linux). A one-file exe unpacks the whole app
    # into %TEMP%\_MEIxxxxx on every launch and must delete it on exit; when any
    # handle into that dir is still open at shutdown the windowed bootloader
    # pops a blocking "Failed to remove temporary directory" dialog (reported
    # on Windows 2026-06-17). One-dir runs in place and never creates _MEItemp,
    # so that failure mode is structurally impossible. It also starts faster and
    # avoids the per-launch unpack that AV heuristics dislike. The Inno Setup
    # installer ships the folder; the self-updater already replaces a directory.
    exe = EXE(
        pyz,
        a.scripts,
        exclude_binaries=True,
        name="BetterFlow",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        # UPX OFF on Windows: UPX-packed binaries are the #1 antivirus heuristic
        # trigger (Avast/Defender flag them on sight). An unsigned + UPX-packed
        # exe was getting quarantined ("Suspicious file detected", Claudia
        # 2026-06-16). Dropping UPX trades a slightly larger exe for far fewer
        # false positives. The durable fix is Authenticode code-signing.
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=TARGET_ARCH,
        icon=str(resources_dir / "icon.ico") if (resources_dir / "icon.ico").exists() else None,
    )

    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        name="BetterFlow",
    )

elif is_linux:
    # One-dir build; scripts/build-appimage.sh wraps dist/BetterFlow into an
    # AppDir and packages it as a single .AppImage.
    exe = EXE(
        pyz,
        a.scripts,
        exclude_binaries=True,
        name="BetterFlow",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=TARGET_ARCH,
    )

    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=True,
        name="BetterFlow",
    )
