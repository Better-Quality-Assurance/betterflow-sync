"""Losing `.machine_id` must not mint a second device for the same laptop.

The server keys a device as ``sha256(machine_id . platform)``
(``AgentDevice::generateDeviceId`` in internal-tool2), and the agent's
machine_id is a random UUID written to a file in the config directory. So
anything that loses that file — a wiped app-support directory, a launch under a
different container, a fresh profile — silently registers a NEW device for a
machine that already has one.

Verified in prod on 2026-08-04: two macOS users re-registered and ended up with
two ACTIVE device rows each, both syncing, overlapping in time. That is the
input shape behind cross-device hour double-counting, and it is invisible from
the agent (both rows look perfectly healthy).

The machine already reports a stable hardware identifier for MDM asset
correlation. Deriving the id from it when the file is gone makes the loss
recoverable instead of duplicating.
"""

import uuid
from unittest.mock import patch

import pytest

import src.config as config_module
from src.config import get_machine_uuid

_UUID_SHAPE = config_module._UUID_RE


def _fresh():
    """Simulate a launch that cannot see any previously written file."""
    config_module._machine_uuid_cache = None


class TestDerivedFromHardwareSerial:
    def test_a_lost_file_recreates_the_same_id(self, tmp_path, monkeypatch):
        """The property that matters: two machine-id-less launches on the SAME
        laptop must agree, so the server keeps one device row."""
        with patch.object(config_module, "get_hardware_serial", return_value="C02ABC123DEF"):
            monkeypatch.setattr(config_module, "user_config_dir", lambda *a, **k: str(tmp_path / "a"))
            _fresh()
            first = get_machine_uuid()

            # A different config directory, i.e. the file is gone.
            monkeypatch.setattr(config_module, "user_config_dir", lambda *a, **k: str(tmp_path / "b"))
            _fresh()
            second = get_machine_uuid()

        assert first == second
        assert _UUID_SHAPE.match(first), "the server and the file both require UUID shape"

    def test_two_different_machines_do_not_collide(self, tmp_path, monkeypatch):
        """The control. A derivation that mapped every machine to one id would
        pass the test above perfectly and merge the whole fleet into one device."""
        monkeypatch.setattr(config_module, "user_config_dir", lambda *a, **k: str(tmp_path / "a"))
        with patch.object(config_module, "get_hardware_serial", return_value="SERIAL-AAA"):
            _fresh()
            a = get_machine_uuid()
        monkeypatch.setattr(config_module, "user_config_dir", lambda *a, **k: str(tmp_path / "b"))
        with patch.object(config_module, "get_hardware_serial", return_value="SERIAL-BBB"):
            _fresh()
            b = get_machine_uuid()
        assert a != b

    def test_the_derived_id_is_written_to_the_file(self, tmp_path, monkeypatch):
        """So the serial probe is not on the startup path of every later launch."""
        monkeypatch.setattr(config_module, "user_config_dir", lambda *a, **k: str(tmp_path))
        with patch.object(config_module, "get_hardware_serial", return_value="C02ABC123DEF"):
            _fresh()
            derived = get_machine_uuid()
        assert (tmp_path / ".machine_id").read_text().strip() == derived


class TestExistingStateAlwaysWins:
    def test_a_present_file_is_not_replaced_by_the_derived_id(self, tmp_path, monkeypatch):
        """Pins the no-churn requirement: every machine in the fleet already has
        a random id, and rewriting them to derived ones would re-register the
        ENTIRE fleet as new devices — the exact harm this change prevents."""
        existing = str(uuid.uuid4())
        (tmp_path / ".machine_id").write_text(existing)
        monkeypatch.setattr(config_module, "user_config_dir", lambda *a, **k: str(tmp_path))
        with patch.object(config_module, "get_hardware_serial", return_value="C02ABC123DEF"):
            _fresh()
            assert get_machine_uuid() == existing

    def test_the_serial_is_not_probed_when_the_file_is_present(self, tmp_path, monkeypatch):
        (tmp_path / ".machine_id").write_text(str(uuid.uuid4()))
        monkeypatch.setattr(config_module, "user_config_dir", lambda *a, **k: str(tmp_path))
        with patch.object(config_module, "get_hardware_serial") as probe:
            _fresh()
            get_machine_uuid()
        probe.assert_not_called()


class TestFallbackWhenNoSerialExists:
    """A VM, a container and a locked-down Linux box legitimately have no
    serial. That must stay a working (if non-recoverable) id, never a crash and
    never a shared constant."""

    def test_no_serial_still_yields_a_valid_unique_id(self, tmp_path, monkeypatch):
        ids = set()
        for name in ("a", "b"):
            monkeypatch.setattr(config_module, "user_config_dir", lambda *a, _n=name, **k: str(tmp_path / _n))
            with patch.object(config_module, "get_hardware_serial", return_value=None):
                _fresh()
                value = get_machine_uuid()
            assert _UUID_SHAPE.match(value)
            ids.add(value)
        assert len(ids) == 2, "no serial must fall back to random, not to a shared constant"

    def test_a_raising_probe_does_not_break_startup(self, tmp_path, monkeypatch):
        """get_hardware_serial documents that it never raises. Do not rely on a
        promise made in another module for something on the launch path."""
        monkeypatch.setattr(config_module, "user_config_dir", lambda *a, **k: str(tmp_path))
        with patch.object(config_module, "get_hardware_serial", side_effect=RuntimeError("boom")):
            _fresh()
            value = get_machine_uuid()
        assert _UUID_SHAPE.match(value)


@pytest.fixture(autouse=True)
def _clear_cache_after():
    yield
    config_module._machine_uuid_cache = None
