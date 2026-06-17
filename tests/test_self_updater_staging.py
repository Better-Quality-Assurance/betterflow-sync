"""Tests for staged auto-update logic (the loop/brick-safety-critical parts).

These don't touch the real binary-replace path (which needs a real OS); they
verify version gating, loop prevention, path-traversal rejection, and that
staging is always cleared before applying.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import src.config as cfg
import src.self_updater as su


@pytest.fixture
def staging(tmp_path, monkeypatch):
    # Point the staging dir at a fresh temp dir for each test.
    monkeypatch.setattr(cfg.Config, "get_data_dir", classmethod(lambda cls: tmp_path))
    return tmp_path


def _write_staged(version: str, artifact: str | None, *, create_file: bool = True) -> None:
    sd = su._staging_dir()
    sd.mkdir(parents=True, exist_ok=True)
    if artifact and create_file:
        (sd / artifact).write_bytes(b"\x7fELFfake")
    su._staging_meta_path().write_text(json.dumps({"version": version, "artifact": artifact}))


class TestGetStagedUpdate:
    def test_nothing_staged(self, staging):
        assert su.get_staged_update("1.5.22") is None

    def test_older_version_discarded(self, staging):
        _write_staged("1.5.20", "a.AppImage")
        assert su.get_staged_update("1.5.22") is None
        assert not su._staging_meta_path().exists()  # cleared

    def test_equal_version_discarded_loop_guard(self, staging):
        # The critical guard: a staged build equal to the running version must
        # never be applied, or launch would apply→relaunch→apply forever.
        _write_staged("1.5.22", "a.AppImage")
        assert su.get_staged_update("1.5.22") is None

    def test_newer_version_returned(self, staging):
        _write_staged("1.5.23", "BetterFlow-new.AppImage")
        p = su.get_staged_update("1.5.22")
        assert p is not None and p.name == "BetterFlow-new.AppImage"

    def test_path_traversal_artifact_rejected(self, staging):
        _write_staged("1.5.23", "../evil", create_file=False)
        assert su.get_staged_update("1.5.22") is None

    def test_missing_artifact_file_discarded(self, staging):
        _write_staged("1.5.23", "gone.AppImage", create_file=False)
        assert su.get_staged_update("1.5.22") is None

    def test_corrupt_metadata_discarded(self, staging):
        sd = su._staging_dir()
        sd.mkdir(parents=True, exist_ok=True)
        su._staging_meta_path().write_text("{not json")
        assert su.get_staged_update("1.5.22") is None
        assert not su._staging_meta_path().exists()


class TestApplyStagedUpdate:
    def test_noop_when_nothing_staged(self, staging):
        assert su.apply_staged_update("1.5.22") is False

    def test_applies_and_clears_staging_first(self, staging):
        _write_staged("1.5.23", "new.AppImage")
        with patch("src.self_updater._apply_local_artifact", return_value=True) as m:
            assert su.apply_staged_update("1.5.22") is True
        m.assert_called_once()
        # staging is cleared BEFORE applying, so a relaunch can't re-trigger.
        assert not su._staging_meta_path().exists()

    def test_failed_apply_still_clears_staging(self, staging):
        _write_staged("1.5.23", "new.AppImage")
        with patch("src.self_updater._apply_local_artifact", return_value=False):
            assert su.apply_staged_update("1.5.22") is False
        assert not su._staging_meta_path().exists()  # no retry loop


class TestStageUpdate:
    def test_records_metadata(self, staging):
        def fake_dl(url, dest, on_progress=None):
            Path(dest).write_bytes(b"payload")

        with patch("src.self_updater._download_to_file", side_effect=fake_dl):
            ok = su.stage_update(
                "https://github.com/Better-Quality-Assurance/betterflow-sync/releases/download/v1.5.23/BetterFlow-linux-x86_64.AppImage",
                "1.5.23",
            )
        assert ok is True
        meta = json.loads(su._staging_meta_path().read_text())
        assert meta["version"] == "1.5.23"
        assert meta["artifact"] == "BetterFlow-linux-x86_64.AppImage"

    def test_rejects_non_https(self, staging):
        # Allowlisted host but http:// — must be rejected on the scheme alone.
        assert su.stage_update("http://github.com/x/a.AppImage", "1.5.23") is False

    def test_rejects_off_allowlist_host(self, staging):
        # HTTPS but a non-GitHub host: stage_update must refuse it at the gate,
        # matching apply_update's is_safe_fetch_url check (not just https). The
        # download must never be attempted (pre-fix it was — the gate only
        # checked the scheme), so assert _download_to_file is never called.
        with patch("src.self_updater._download_to_file") as dl:
            assert su.stage_update("https://evil.example.com/a.AppImage", "1.5.23") is False
        dl.assert_not_called()

    def test_failed_download_clears_staging(self, staging):
        # Use an allowlisted URL so we actually reach (and fail) the download —
        # otherwise the host gate would short-circuit before _download_to_file.
        url = "https://github.com/Better-Quality-Assurance/betterflow-sync/releases/download/v1.5.23/a.AppImage"
        with patch("src.self_updater._download_to_file", side_effect=OSError("boom")):
            assert su.stage_update(url, "1.5.23") is False
        assert not su._staging_meta_path().exists()


class TestDownloadSizeCap:
    """The download must refuse oversized bodies so a poisoned/misconfigured
    response can't fill the disk (installers are ~60-80 MB; cap is 500 MB)."""

    def _fake_get(self, content_length, chunks):
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.headers = {"content-length": str(content_length)}
        resp.raise_for_status.return_value = None
        resp.iter_content.return_value = iter(chunks)
        ctx = MagicMock()
        ctx.__enter__.return_value = resp
        ctx.__exit__.return_value = False
        return ctx

    def test_rejects_oversized_content_length(self, tmp_path, monkeypatch):
        # Shrink the cap so the test stays tiny (no real 500MB anything).
        monkeypatch.setattr(su, "_MAX_DOWNLOAD_BYTES", 100)
        dest = tmp_path / "big.bin"
        ctx = self._fake_get(101, [b"x"])
        with patch("src.self_updater.requests.get", return_value=ctx):
            with pytest.raises(ValueError):
                su._download_to_file("https://github.com/x/big.bin", dest)

    def test_aborts_when_stream_exceeds_cap(self, tmp_path, monkeypatch):
        # content-length lies (0), but the streamed body blows past the cap.
        # Tiny cap + tiny chunk so we don't allocate hundreds of MB in CI.
        monkeypatch.setattr(su, "_MAX_DOWNLOAD_BYTES", 100)
        dest = tmp_path / "big.bin"
        ctx = self._fake_get(0, [b"x" * 101])
        with patch("src.self_updater.requests.get", return_value=ctx):
            with pytest.raises(ValueError):
                su._download_to_file("https://github.com/x/big.bin", dest)

    def test_allows_normal_size(self, tmp_path):
        dest = tmp_path / "ok.bin"
        ctx = self._fake_get(7, [b"payload"])
        with patch("src.self_updater.requests.get", return_value=ctx):
            su._download_to_file("https://github.com/x/ok.bin", dest)
        assert dest.read_bytes() == b"payload"


class TestArtifactFilename:
    def test_keeps_real_name_and_extension(self):
        assert (
            su._artifact_filename_from_url("https://x/y/BetterFlow-macOS-arm64.dmg?token=1")
            == "BetterFlow-macOS-arm64.dmg"
        )

    def test_empty_falls_back(self):
        assert su._artifact_filename_from_url("https://x/") == "update.bin"
