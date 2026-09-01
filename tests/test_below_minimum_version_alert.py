"""A device stuck below the server's minimum version must be visible to ops.

`_handle_heartbeat_payload` already detects below-minimum and does two things:
logs a warning, and stages an update. Both are LOCAL. Nothing leaves the
machine, so a device that keeps failing to update sits below the floor for days
while the only evidence is a log nobody pulls — the exact "if this failed, would
anything be different?" shape from fail-closed.md. Answer today: no.

Issue #211 observed it on a real device: 1.5.119 against a 1.5.124 floor, the
server logging `below minimum` on every sync, for days, with window-title
categorisation lost the whole time.

Deliberately NOT tracking duration on the device. The ops ingest already keys by
fingerprint and records first_seen/last_seen, so "below the floor for more than a
day" is a question the monitor can answer from the report stream. Persisting a
first-seen timestamp on the agent would need a new table and would still reset
if the DB were rebuilt, to answer a question the ingest answers for free.
"""

from unittest.mock import Mock

from src.sync.sync_engine import SyncEngine


class _Reporter:
    def __init__(self):
        self.captures = []

    def capture(self, message, **kw):
        self.captures.append((message, kw))


def _engine(reporter):
    engine = SyncEngine.__new__(SyncEngine)
    engine.error_reporter = reporter
    engine.on_update_required = None
    return engine


def test_below_minimum_is_reported_to_ops():
    reporter = _Reporter()
    engine = _engine(reporter)

    SyncEngine._report_below_minimum(engine, "1.5.124")

    assert reporter.captures, (
        "a device below the minimum version reported nothing off-device; "
        "the condition is invisible to ops until someone pulls the log"
    )
    message, kw = reporter.captures[0]
    assert "1.5.124" in message, message
    assert kw["level"] == "warning"
    assert kw["fingerprint"] == "agent-below-minimum-version", (
        "a stable fingerprint is what lets the ingest age this into "
        "'stuck for more than a day' via first_seen"
    )


def test_the_report_carries_both_versions():
    """Ops needs the gap, not just the fact. One version alone cannot tell a
    device one release behind from one stuck twenty releases back."""
    reporter = _Reporter()
    SyncEngine._report_below_minimum(_engine(reporter), "1.5.124")

    message = reporter.captures[0][0]
    from src.sync.bf_client import AGENT_VERSION
    assert AGENT_VERSION in message, f"running version missing: {message}"


def test_no_reporter_is_survivable():
    """Telemetry must never break the heartbeat it rides on."""
    engine = SyncEngine.__new__(SyncEngine)
    engine.error_reporter = None
    SyncEngine._report_below_minimum(engine, "1.5.124")  # must not raise


def test_a_reporter_that_raises_does_not_break_the_heartbeat():
    engine = SyncEngine.__new__(SyncEngine)
    engine.error_reporter = Mock()
    engine.error_reporter.capture.side_effect = RuntimeError("ingest down")
    SyncEngine._report_below_minimum(engine, "1.5.124")  # must not raise


def test_the_detection_site_calls_it():
    """Witness the CALL, not just the helper. A reporter nobody invokes is the
    same silence this fixes (test-fixture-discipline Phantom 3)."""
    import inspect
    src = inspect.getsource(SyncEngine._send_heartbeat)
    assert "_report_below_minimum" in src, (
        "the below-minimum branch does not report; the helper is dead code"
    )
