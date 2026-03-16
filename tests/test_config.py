"""Tests for get_machine_uuid() in config module."""

import uuid
from unittest.mock import patch

import src.config as config_module
from src.config import _UUID_RE


class TestGetMachineUuid:
    """Tests for the persistent machine UUID function."""

    def setup_method(self):
        """Reset the in-memory cache before each test."""
        config_module._machine_uuid_cache = None

    def teardown_method(self):
        """Clear the cache to avoid leaking into other test modules."""
        config_module._machine_uuid_cache = None

    def test_generates_valid_uuid4(self, tmp_path, monkeypatch):
        """First call with no file generates a valid UUID4."""
        monkeypatch.setattr(
            "src.config.user_config_dir", lambda *a, **kw: str(tmp_path)
        )
        result = config_module.get_machine_uuid()
        parsed = uuid.UUID(result, version=4)
        assert str(parsed) == result

    def test_persists_to_file(self, tmp_path, monkeypatch):
        """Generated UUID is written to .machine_id file."""
        monkeypatch.setattr(
            "src.config.user_config_dir", lambda *a, **kw: str(tmp_path)
        )
        result = config_module.get_machine_uuid()
        file_content = (tmp_path / ".machine_id").read_text().strip()
        assert file_content == result

    def test_cache_returns_same_value(self, tmp_path, monkeypatch):
        """Second call returns cached value without file I/O."""
        monkeypatch.setattr(
            "src.config.user_config_dir", lambda *a, **kw: str(tmp_path)
        )
        first = config_module.get_machine_uuid()
        # Delete the file to prove second call uses cache, not disk.
        (tmp_path / ".machine_id").unlink()
        second = config_module.get_machine_uuid()
        assert first == second

    def test_reads_existing_valid_file(self, tmp_path, monkeypatch):
        """Reads and caches a pre-existing valid UUID from disk."""
        monkeypatch.setattr(
            "src.config.user_config_dir", lambda *a, **kw: str(tmp_path)
        )
        expected = "12345678-1234-4234-8234-123456789012"
        (tmp_path / ".machine_id").write_text(expected)
        assert config_module.get_machine_uuid() == expected

    def test_rejects_invalid_uuid_in_file(self, tmp_path, monkeypatch):
        """Non-UUID content in file is rejected; a new UUID is generated."""
        monkeypatch.setattr(
            "src.config.user_config_dir", lambda *a, **kw: str(tmp_path)
        )
        (tmp_path / ".machine_id").write_text("not-a-uuid")
        result = config_module.get_machine_uuid()
        assert result != "not-a-uuid"
        assert _UUID_RE.match(result)

    def test_rejects_multiline_content(self, tmp_path, monkeypatch):
        """Multi-line file content fails UUID validation."""
        monkeypatch.setattr(
            "src.config.user_config_dir", lambda *a, **kw: str(tmp_path)
        )
        (tmp_path / ".machine_id").write_text("line1\nline2")
        result = config_module.get_machine_uuid()
        assert "\n" not in result
        assert _UUID_RE.match(result)

    def test_handles_empty_file(self, tmp_path, monkeypatch):
        """Empty file falls through to UUID generation."""
        monkeypatch.setattr(
            "src.config.user_config_dir", lambda *a, **kw: str(tmp_path)
        )
        (tmp_path / ".machine_id").write_text("   \n   ")
        result = config_module.get_machine_uuid()
        assert _UUID_RE.match(result)

    def test_handles_unicode_decode_error(self, tmp_path, monkeypatch):
        """Binary file content triggers UnicodeDecodeError, falls through."""
        monkeypatch.setattr(
            "src.config.user_config_dir", lambda *a, **kw: str(tmp_path)
        )
        (tmp_path / ".machine_id").write_bytes(b"\x80\x81\x82\xff")
        result = config_module.get_machine_uuid()
        assert _UUID_RE.match(result)

    def test_handles_read_permission_error(self, tmp_path, monkeypatch):
        """OSError on read falls through to UUID generation."""
        monkeypatch.setattr(
            "src.config.user_config_dir", lambda *a, **kw: str(tmp_path)
        )
        id_file = tmp_path / ".machine_id"
        id_file.write_text("12345678-1234-4234-8234-123456789012")
        id_file.chmod(0o000)
        try:
            result = config_module.get_machine_uuid()
            assert _UUID_RE.match(result)
        finally:
            id_file.chmod(0o644)

    def test_handles_write_failure(self, tmp_path, monkeypatch):
        """Write failure still returns a UUID (graceful degradation)."""
        monkeypatch.setattr(
            "src.config.user_config_dir", lambda *a, **kw: str(tmp_path / "readonly")
        )
        # Create a read-only parent so mkdir succeeds but write fails.
        readonly = tmp_path / "readonly"
        readonly.mkdir()
        readonly.chmod(0o555)
        try:
            result = config_module.get_machine_uuid()
            assert _UUID_RE.match(result)
            # File should not exist since write failed.
            assert not (readonly / ".machine_id").exists()
        finally:
            readonly.chmod(0o755)

    def test_atomic_write_no_leftover_tmp(self, tmp_path, monkeypatch):
        """Successful write should not leave a .tmp file behind."""
        monkeypatch.setattr(
            "src.config.user_config_dir", lambda *a, **kw: str(tmp_path)
        )
        config_module.get_machine_uuid()
        assert not (tmp_path / ".machine_id.tmp").exists()
        assert (tmp_path / ".machine_id").exists()

    def test_tmp_cleaned_up_on_replace_failure(self, tmp_path, monkeypatch):
        """Stale .tmp file is removed when os.replace() fails."""
        monkeypatch.setattr(
            "src.config.user_config_dir", lambda *a, **kw: str(tmp_path)
        )
        # Let write_text succeed but os.replace fail
        with patch("src.config.os.replace", side_effect=OSError("replace failed")):
            result = config_module.get_machine_uuid()
        assert _UUID_RE.match(result)
        # .tmp should be cleaned up, not left behind
        assert not (tmp_path / ".machine_id.tmp").exists()

    def test_thread_safety_single_uuid(self, tmp_path, monkeypatch):
        """Concurrent calls from multiple threads all get the same UUID."""
        monkeypatch.setattr(
            "src.config.user_config_dir", lambda *a, **kw: str(tmp_path)
        )
        results = []
        import threading

        def worker():
            results.append(config_module.get_machine_uuid())

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(results)) == 1, f"Got multiple UUIDs: {set(results)}"


class TestUuidRegex:
    """Sanity checks for the UUID validation regex."""

    def test_accepts_valid_uuid4(self):
        assert _UUID_RE.match("550e8400-e29b-41d4-a716-446655440000")

    def test_accepts_uppercase(self):
        assert _UUID_RE.match("550E8400-E29B-41D4-A716-446655440000")

    def test_rejects_short_string(self):
        assert not _UUID_RE.match("550e8400")

    def test_rejects_no_dashes(self):
        assert not _UUID_RE.match("550e8400e29b41d4a716446655440000")

    def test_rejects_empty(self):
        assert not _UUID_RE.match("")
