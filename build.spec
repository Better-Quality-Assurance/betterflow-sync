# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for BetterFlow Sync."""

import platform
import sys
from pathlib import Path

block_cipher = None

# Determine platform
is_mac = platform.system() == "Darwin"
is_windows = platform.system() == "Windows"

# Paths
root_dir = Path(SPECPATH)
src_dir = root_dir / "src"
resources_dir = root_dir / "resources"

# Data files
datas = [
    (str(resources_dir), "resources"),
]

# Hidden imports for pystray and keyring backends
hiddenimports = [
    "pystray._darwin" if is_mac else "pystray._win32",
    "keyring.backends.macOS" if is_mac else "keyring.backends.Windows",
    "PIL._tkinter_finder",
    "apscheduler.triggers.interval",
    "apscheduler.schedulers.background",
]

a = Analysis(
    [str(src_dir / "main.py")],
    pathex=[str(root_dir)],
    binaries=[],
    datas=datas,
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
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name="BetterFlow Sync",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=True,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=str(resources_dir / "icon.icns") if (resources_dir / "icon.icns").exists() else None,
    )

    app = BUNDLE(
        exe,
        name="BetterFlow Sync.app",
        icon=str(resources_dir / "icon.icns") if (resources_dir / "icon.icns").exists() else None,
        bundle_identifier="co.betterqa.betterflow-sync",
        info_plist={
            "CFBundleName": "BetterFlow Sync",
            "CFBundleDisplayName": "BetterFlow Sync",
            "CFBundleVersion": "1.0.0",
            "CFBundleShortVersionString": "1.0.0",
            "LSUIElement": True,  # Hide from dock (menu bar app)
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "10.15",
        },
    )

elif is_windows:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name="BetterFlow Sync",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        icon=str(resources_dir / "icon.ico") if (resources_dir / "icon.ico").exists() else None,
    )
