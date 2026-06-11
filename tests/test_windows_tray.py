"""Tests for the Windows tray-promotion helper.

The registry-promotion and pystray-patch paths only run on Windows, but the
GUID byte conversion is pure bit-twiddling that must produce exactly the layout
Windows expects — a wrong byte order would silently key the icon under the
wrong identity, so it gets a sharp guardrail test that runs on every platform.
"""
from __future__ import annotations

import ctypes

from src import windows_tray as wt


class _GUID(ctypes.Structure):
    """Mirror of pystray's NOTIFYICONDATAW.GUID with Windows field widths."""

    _layout_ = "ms"
    _pack_ = 1
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_byte * 8),
    ]


class TestGuidConversion:
    def test_matches_uuid_bytes_le(self) -> None:
        # A GUID struct is the same 16-byte mixed-endian layout as UUID.bytes_le
        # (LE Data1/2/3, big-endian Data4). Exact-match or the icon is mis-keyed.
        g = wt._to_win_guid(wt.TRAY_ICON_GUID, _GUID)
        raw = bytes(
            ctypes.cast(ctypes.byref(g), ctypes.POINTER(ctypes.c_ubyte * 16)).contents
        )
        assert raw == wt.TRAY_ICON_GUID.bytes_le

    def test_high_byte_node_survives_signed_array(self) -> None:
        # The node bytes of our GUID include values > 127 (0xBF, 0xC5, 0xD0);
        # they must round-trip through the signed BYTE array unchanged.
        g = wt._to_win_guid(wt.TRAY_ICON_GUID, _GUID)
        data4 = bytes(b & 0xFF for b in g.Data4)
        assert data4 == wt.TRAY_ICON_GUID.bytes[8:]


class TestNonWindowsGuards:
    """Every public entry point must be an inert no-op off Windows."""

    def test_is_supported_false_off_windows(self, monkeypatch) -> None:
        monkeypatch.setattr(wt.platform, "system", lambda: "Linux")
        assert wt.is_supported() is False
        assert wt.install_stable_guid() is False
        # Must not spawn a thread or touch a registry that isn't there.
        wt.schedule_promotion()
