"""Windows update-asset matching must stay compatible across the rename.

The human-facing Windows zip is renamed BetterFlow-Windows.zip ->
BetterFlow-Windows-Update.zip so users stop downloading it and running the loose
exe from inside the archive (Sachi, 2026-06-19: "Failed to load Python DLL
..._internal\\python311.dll" — a one-dir exe launched from Explorer's zip preview
without its _internal\\ folder). The installer (BetterFlow-Windows-Setup.exe) is
the human download; the zip exists ONLY for the in-app auto-updater.

These pin that the deployed fleet's substring matcher still resolves the renamed
zip (so auto-update keeps working) and never mistakes the Setup.exe for an update
artifact (it would be downloaded and xcopy-applied as if it were the app).
"""

from src.update_checker import _find_platform_asset


def _release(*names):
    return {
        "assets": [
            {"name": n, "browser_download_url": f"https://x/{n}"} for n in names
        ]
    }


def test_renamed_update_zip_is_still_matched():
    release = _release("BetterFlow-Windows-Update.zip", "BetterFlow-Windows-Setup.exe")
    assert (
        _find_platform_asset(release, system="Windows")
        == "https://x/BetterFlow-Windows-Update.zip"
    )


def test_legacy_zip_name_still_matched_for_old_releases():
    """Older releases still carry the un-suffixed name — the matcher must accept
    both so a mixed release history keeps updating."""
    release = _release("BetterFlow-Windows.zip")
    assert (
        _find_platform_asset(release, system="Windows")
        == "https://x/BetterFlow-Windows.zip"
    )


def test_installer_exe_is_never_chosen_as_update_artifact():
    """The Setup.exe must NEVER be returned — the updater downloads the artifact
    and xcopies its contents into the install dir; an .exe would be garbage."""
    release = _release("BetterFlow-Windows-Setup.exe")
    assert _find_platform_asset(release, system="Windows") is None
