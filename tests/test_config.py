"""Tests for get_machine_uuid() in config module."""

import sys
import uuid
from unittest.mock import patch

import pytest

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

    @pytest.mark.skipif(sys.platform == "win32", reason="chmod read-only not enforced on Windows dirs")
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


def test_in_process_afk_defaults_on():
    from src.config import Config
    assert Config().sync.in_process_afk is True


def test_in_process_window_defaults_off():
    # Ships dormant/opt-in — the in-process window source must be off by default.
    from src.config import Config
    assert Config().sync.in_process_window is False


def test_in_process_window_enabled_from_server():
    from src.config import Config
    cfg = Config()
    cfg.update_from_server({"sync": {"in_process_window": True}})
    assert cfg.sync.in_process_window is True


def test_foreground_enabled_is_not_persisted(tmp_path, monkeypatch):
    """foreground_activity.enabled must never be written to config.json — it's a
    server-driven, default-OFF billing flag. Persisting it let a default-ON beta
    build pin enabled=true and override the safe default on update (audit
    round 2). On load, the code default (False) must win regardless of the saved
    file."""
    import json

    from src.config import Config

    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr(Config, "get_config_file", lambda self: cfg_file)

    cfg = Config()
    cfg.foreground_activity.enabled = True  # as a beta build would have it
    cfg.save()

    written = json.loads(cfg_file.read_text())
    assert "enabled" not in written.get("foreground_activity", {}), (
        "foreground_activity.enabled must not be persisted"
    )

    # Even a hand-edited config with enabled=true must not re-activate it.
    written.setdefault("foreground_activity", {})["enabled"] = True
    cfg_file.write_text(json.dumps(written))
    reloaded = Config._from_dict(json.loads(cfg_file.read_text()))
    assert reloaded.foreground_activity.enabled is False


class TestMachineUuidIsIsolatedByConftest:
    """Regression guard for the autouse conftest fixture.

    get_machine_uuid() and _load_dotenv() call the module-level `user_config_dir`
    directly, bypassing Config's get_config_dir/get_data_dir classmethods. Before
    the fixture was extended, it patched only those classmethods — so a test that
    triggered get_machine_uuid (several exchange_code paths in test_bf_client do)
    read/wrote the developer's REAL ~/.../BetterFlow/.machine_id and leaked the
    cached value across the session.

    This test relies ONLY on the autouse fixture (no per-test monkeypatch), so it
    fails if that module-level redirect is ever dropped again."""

    def test_uuid_is_written_into_the_isolated_config_dir(self):
        from src.config import Config, get_machine_uuid

        result = get_machine_uuid()
        uuid.UUID(result, version=4)  # must be a valid uuid4

        machine_id_file = Config.get_config_dir() / ".machine_id"
        assert machine_id_file.exists(), (
            "get_machine_uuid wrote OUTSIDE the isolated temp config dir — the "
            "conftest fixture is not redirecting module-level user_config_dir"
        )
        assert machine_id_file.read_text().strip() == result


class TestCallDetectionCreditCap:
    """max_credit_minutes: the ceiling on per-call AFK credit (billing-adjacent,
    so server values are clamped and the whole block stays deferral-gated)."""

    def test_default_is_240_minutes(self):
        from src.config import Config
        assert Config().call_detection.max_credit_minutes == 240

    def test_deferred_on_first_delivery(self):
        # call_detection is a capture/billing block: gated behind
        # DEFER_UNAPPLIED_SERVER_SETTINGS like the rest of it.
        from src.config import Config
        cfg = Config()
        cfg.update_from_server({"call_detection": {"max_credit_minutes": 5}})
        assert cfg.call_detection.max_credit_minutes == 240

    def test_server_value_clamped_when_rolled_out(self, monkeypatch):
        from src.config import Config
        monkeypatch.setattr(config_module, "DEFER_UNAPPLIED_SERVER_SETTINGS", False)
        cfg = Config()

        cfg.update_from_server({"call_detection": {"max_credit_minutes": 100000}})
        assert cfg.call_detection.max_credit_minutes == 480, "cap must never exceed 8h"

        cfg.update_from_server({"call_detection": {"max_credit_minutes": 0}})
        assert cfg.call_detection.max_credit_minutes == 1, "cap floor is 1 minute"

        cfg.update_from_server({"call_detection": {"max_credit_minutes": 90}})
        assert cfg.call_detection.max_credit_minutes == 90

        cfg.update_from_server({"call_detection": {"max_credit_minutes": "nope"}})
        assert cfg.call_detection.max_credit_minutes == 90, "invalid value ignored"
