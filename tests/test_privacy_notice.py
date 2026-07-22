"""The notice text, its version, and the acknowledgement record.

The defect being fixed is not "no notice exists" — it is "a new data category
can reach devices that already acknowledged the old text". So the load-bearing
assertions here are the ones about the VERSION, not the ones about the copy:

- the version is derived from the text, so editing copy without touching the
  version is impossible rather than merely discouraged;
- a device holding an older version re-shows, which is the same comparison as
  a device holding none.

The qualifier tests exist because three phrases are legal work disguised as
prose, and a future "make it fit on one screen" pass deletes them first. A
comment saying so does not stop it (diagnosis-discipline.md); a failing test
does.

No absolute dates anywhere: every timestamp is injected.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src import privacy_notice as pn
from src.config import Config


@pytest.fixture
def config(tmp_path):
    """A real Config, isolated by conftest's autouse path redirection."""
    return Config()


def _fixed_now(offset_seconds: int = 0) -> datetime:
    """A deterministic instant derived from the clock, never a literal date.

    A hardcoded datetime in a fixture is a time bomb; two detonated across
    these repos on 2026-07-22. This stays anchored to whenever the suite runs.
    """
    return datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)


# ── The text ────────────────────────────────────────────────────────────

def test_every_mandatory_qualifier_survives():
    """Each of these is doing legal work, not describing a feature."""
    text = pn.notice_text()
    for qualifier in pn.REQUIRED_QUALIFIERS:
        assert qualifier in text, (
            f"the qualifier {qualifier!r} was removed from the notice — it is "
            "load-bearing, see the comments in src/privacy_notice.py"
        )


def test_titles_are_not_described_as_anonymised():
    """"as recorded" is the whole point: nothing hashes titles on the device.

    The `hash_titles` setting NAME already misleads people here, so the notice
    must not repeat the mistake by implying a local transform.
    """
    text = pn.notice_text()
    assert "sent to our servers as recorded" in text
    lowered = text.lower()
    for forbidden in ("anonymis", "anonymiz", "hashed before", "hashed on this"):
        assert forbidden not in lowered, (
            f"the notice now claims {forbidden!r}; the agent does no client-side "
            "title hashing"
        )


def test_input_bullet_cannot_read_as_a_keylogger():
    text = pn.notice_text()
    assert "Counts only" in text
    assert "does not record which keys you press" in text


def test_contact_route_is_present():
    assert "dpo@betterqa.co" in pn.notice_text()


def test_romanian_source_prevails_is_stated_in_the_notice():
    """The English is a rendering; employees signed the Romanian."""
    text = pn.notice_text()
    assert "Regulamentul Intern" in text
    assert "prevails" in text


def test_every_bullet_reaches_the_rendered_lines():
    """The renderer must not drop a section — a lost bullet is a lost disclosure."""
    lines = pn.notice_lines()
    for lead, bullets in pn.NOTICE_SECTIONS:
        assert lead in lines
        for bullet in bullets:
            assert f"•  {bullet}" in lines


# ── The version ─────────────────────────────────────────────────────────

def test_version_is_derived_from_the_text_not_hand_written(monkeypatch):
    """Change a character of the notice; the version must change.

    This is requirement 2 in mechanical form. If NOTICE_VERSION were a constant
    a developer maintains, this test would pass while the guarantee was absent —
    so it recomputes the version the way production does, against mutated text.
    """
    baseline = pn.NOTICE_VERSION
    assert baseline == f"{pn.NOTICE_REVISION}-{pn._digest(pn.notice_text())}"

    mutated_sections = (
        ("A newly collected category nobody disclosed before.", ()),
        *pn.NOTICE_SECTIONS,
    )
    monkeypatch.setattr(pn, "NOTICE_SECTIONS", mutated_sections)
    mutated_version = f"{pn.NOTICE_REVISION}-{pn._digest(pn.notice_text())}"

    assert mutated_version != baseline, (
        "adding a data category to the notice did not change the version — a "
        "silent ship to already-acknowledged devices"
    )


def test_version_carries_a_readable_revision_prefix():
    assert pn.NOTICE_VERSION.startswith(f"{pn.NOTICE_REVISION}-")
    assert len(pn.NOTICE_VERSION) > len(pn.NOTICE_REVISION) + 1


# ── The acknowledgement record ──────────────────────────────────────────

def test_a_fresh_device_needs_the_notice(config):
    assert pn.needs_acknowledgement(config) is True


def test_acknowledging_stops_it_showing_again(config):
    """The "then stops" half. A notice that reappears every launch gets
    dismissed reflexively, and the record stops meaning anything."""
    pn.record_acknowledgement(config, now=_fixed_now())
    assert pn.needs_acknowledgement(config) is False
    # And it stays stopped across a reload from disk, not just in memory.
    assert pn.needs_acknowledgement(Config.load()) is False


def test_bumping_the_text_version_reshows_it(config, monkeypatch):
    pn.record_acknowledgement(config, now=_fixed_now())
    assert pn.needs_acknowledgement(config) is False

    monkeypatch.setattr(pn, "NOTICE_VERSION", pn.NOTICE_VERSION + "-next")
    assert pn.needs_acknowledgement(config) is True, (
        "a new text version did not re-show — the disclosure-gap bug is back"
    )


def test_a_stale_version_is_not_treated_as_acknowledged(config):
    """Compared by value, never by truthiness. A device holding SOME ack is not
    a device holding THIS ack."""
    config.privacy_notice_ack_version = "r0-deadbeefcafe"
    config.privacy_notice_ack_at = _fixed_now(-86400).isoformat()
    assert pn.needs_acknowledgement(config) is True


def test_the_recorded_time_is_when_it_happened(config):
    when = _fixed_now(-3600)
    pn.record_acknowledgement(config, now=when)
    assert config.privacy_notice_ack_at == when.astimezone(timezone.utc).isoformat()
    assert config.privacy_notice_ack_version == pn.NOTICE_VERSION


def test_a_naive_timestamp_is_recorded_as_utc(config):
    naive = _fixed_now().replace(tzinfo=None)
    pn.record_acknowledgement(config, now=naive)
    assert config.privacy_notice_ack_at.endswith("+00:00")


def test_the_record_survives_a_config_round_trip(config):
    pn.record_acknowledgement(config, now=_fixed_now())
    reloaded = Config.load()
    assert reloaded.privacy_notice_ack_version == config.privacy_notice_ack_version
    assert reloaded.privacy_notice_ack_at == config.privacy_notice_ack_at


# ── The heartbeat payload ───────────────────────────────────────────────

def test_no_telemetry_before_an_acknowledgement(config):
    assert pn.acknowledgement_telemetry(config) is None


def test_telemetry_is_exactly_the_server_contract(config):
    """{version, acknowledged_at} — the shape AgentHeartbeatController reads.

    Asserted as EQUALITY, not as "contains", so an extra field is a failure
    rather than a passing test and some bytes nobody reads. Note the device id
    is set on the config and still absent from the payload: the server binds the
    record to a device from the authenticated heartbeat and writes
    agent_device_id itself, so a second client-asserted copy could only agree
    (noise) or disagree (an expensive question on an evidence record).
    """
    config.device_id = "sync:abc-123"
    when = _fixed_now(-120)
    pn.record_acknowledgement(config, now=when)

    payload = pn.acknowledgement_telemetry(config)
    assert payload == {
        "version": pn.NOTICE_VERSION,
        "acknowledged_at": when.astimezone(timezone.utc).isoformat(),
    }


def test_a_half_written_record_is_not_reported(config):
    """A version with no timestamp is not evidence of delivery."""
    config.privacy_notice_ack_version = pn.NOTICE_VERSION
    config.privacy_notice_ack_at = None
    assert pn.acknowledgement_telemetry(config) is None
