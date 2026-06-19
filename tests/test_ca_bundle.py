"""Tests for TLS CA bundle resolution (sync.http_client.resolve_ca_bundle).

Regression cover for the Windows 2026-06-18 sync blackout: the bundled certifi
cacert.pem went missing and every HTTPS request failed with "Could not find a
suitable TLS CA certificate bundle", silently stopping all sync. The resolver
must fall back to an env override or the shipped redundant copy, and must log
loudly (not raise) when nothing exists.
"""

import os

import pytest

import src.sync.http_client as http_client
from src.sync.http_client import resolve_ca_bundle


@pytest.fixture(autouse=True)
def _reset_cache():
    """resolve_ca_bundle() memoizes — clear it around every test."""
    http_client._CACHED_CA_BUNDLE = None
    http_client._CA_BUNDLE_RESOLVED = False
    yield
    http_client._CACHED_CA_BUNDLE = None
    http_client._CA_BUNDLE_RESOLVED = False


def test_uses_certifi_when_present():
    """Default path: certifi's bundle exists, so it is returned."""
    path = resolve_ca_bundle()
    assert path is not None
    assert os.path.isfile(path)


def test_env_override_takes_precedence(tmp_path, monkeypatch):
    """REQUESTS_CA_BUNDLE wins over certifi (operator escape hatch)."""
    custom = tmp_path / "custom-ca.pem"
    custom.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(custom))
    assert resolve_ca_bundle() == str(custom)


def test_falls_back_when_certifi_missing(tmp_path, monkeypatch):
    """If certifi.where() points at a missing file, fall back to the shipped
    redundant copy rather than handing requests a dead path."""
    fallback = tmp_path / "cacert.pem"
    fallback.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(http_client.certifi, "where", lambda: str(tmp_path / "gone.pem"))
    monkeypatch.setattr(http_client, "_bundled_cacert_fallback", lambda: str(fallback))
    assert resolve_ca_bundle() == str(fallback)


def test_returns_none_and_logs_when_nothing_exists(tmp_path, monkeypatch, caplog):
    """No bundle anywhere → None (never raises), and an error is logged so the
    failure is visible instead of silently breaking every request."""
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(http_client.certifi, "where", lambda: str(tmp_path / "gone.pem"))
    monkeypatch.setattr(
        http_client, "_bundled_cacert_fallback", lambda: str(tmp_path / "also-gone.pem")
    )
    with caplog.at_level("ERROR"):
        assert resolve_ca_bundle() is None
    assert any("No TLS CA bundle found" in r.message for r in caplog.records)


def test_new_session_pins_verify(monkeypatch):
    """_new_verified_session sets session.verify to the resolved bundle."""
    monkeypatch.setattr(http_client, "resolve_ca_bundle", lambda: "/tmp/some-ca.pem")
    session = http_client._new_verified_session()
    try:
        assert session.verify == "/tmp/some-ca.pem"
    finally:
        session.close()
