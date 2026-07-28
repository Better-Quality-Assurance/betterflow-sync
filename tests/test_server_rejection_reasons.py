"""The server says WHY it refused an event — the agent must not discard it.

`AgentEventController::batch` answers every batch with
`{processed, failed, errors: [{event, error}, ...], accepted_ids}`. An event
that lands in `errors` is deliberately left OUT of `accepted_ids`, so the
offline queue re-sends it every cycle, crosses the retry ceiling, and is
reported as "Dropped N queued event(s) after max retries — likely real lost
activity". That warning has recurred against `buckets=bf-status` across five
releases with no known mechanism, because `send_events` parsed `processed`,
`failed` and `accepted_ids` out of the envelope and threw `errors` away — the
one field that names the cause.

These tests pin the consumer, not the producer (test-fixture-discipline
Phantom 7): they assert the reason reaches a log record an admin can pull via
`logs_requested`, not that some helper returned a string.
"""

import logging

import pytest
import responses

from src.sync.bf_client import (
    MAX_LOGGED_REJECTIONS,
    MAX_REJECTION_REASON_CHARS,
    BetterFlowClient,
)

BATCH_URL = "https://betterflow.eu/api/agent/events/batch"


def _status_span(event_id="sleep_1753000000_4382919312"):
    """A bf-status span shaped exactly like `_send_status_span` emits one."""
    return {
        "id": event_id,
        "timestamp": "2026-07-25T18:00:00+00:00",
        "duration": 14400.0,
        "bucket_id": "bf-status_somehost",
        "bucket_type": "sleep_time",
        "data": {"status": "sleep"},
    }


def _envelope(*, processed, failed, errors, accepted_ids=None):
    return {
        "success": True,
        "message": "Operation successful",
        "data": {
            "processed": processed,
            "failed": failed,
            "errors": errors,
            "accepted_ids": accepted_ids or [],
        },
    }


@pytest.fixture
def client():
    return BetterFlowClient(
        api_url="https://betterflow.eu/api/agent",
        token="test-token",
        device_id="sync:test-device",
        compress=False,
    )


@responses.activate
def test_rejection_reason_is_logged_when_a_lone_status_span_is_refused(client, caplog):
    """The exact production shape: one bf-status event, refused, nothing else.

    Pre-fix the agent logged nothing at all here, so the retry-until-drop that
    follows had no recorded cause anywhere on the device.
    """
    responses.add(
        responses.POST,
        BATCH_URL,
        json=_envelope(
            processed=0,
            failed=1,
            errors=[
                {
                    "event": "sleep_1753000000_4382919312",
                    "error": "Event timestamp out of acceptable range",
                }
            ],
        ),
        status=200,
    )

    with caplog.at_level(logging.WARNING, logger="src.sync.bf_client"):
        result = client.send_events([_status_span()])

    assert result.success is False
    assert result.transient is False, (
        "a per-event rejection is definitive, not transient — this is what "
        "makes the queue count it toward the drop ceiling"
    )
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "Event timestamp out of acceptable range" in m for m in messages
    ), f"the server's reason never reached the log; records were {messages!r}"
    assert any(
        "sleep_1753000000_4382919312" in m for m in messages
    ), f"the rejected event was not identified; records were {messages!r}"


@responses.activate
def test_rejection_reason_is_logged_on_a_partial_batch(client, caplog):
    """A mixed batch: the good events are accepted, the status span is not.

    This is the branch `_process_queue` treats as partial success, where the
    unaccepted event is the only one whose retry counter is bumped.
    """
    responses.add(
        responses.POST,
        BATCH_URL,
        json=_envelope(
            processed=1,
            failed=1,
            errors=[{"event": "sleep_1753000000_1", "error": "SQLSTATE[23000] boom"}],
            accepted_ids=[41],
        ),
        status=200,
    )

    window_event = {
        "id": 41,
        "timestamp": "2026-07-25T18:00:00+00:00",
        "duration": 60,
        "bucket_id": "aw-watcher-window_somehost",
        "data": {"app": "Terminal"},
    }

    with caplog.at_level(logging.WARNING, logger="src.sync.bf_client"):
        result = client.send_events([window_event, _status_span("sleep_1753000000_1")])

    assert result.accepted_ids == [41]
    assert any(
        "SQLSTATE[23000] boom" in r.getMessage() for r in caplog.records
    ), "a partial batch's rejection reason must be logged too"


@responses.activate
def test_clean_batch_logs_no_rejection_warning(client, caplog):
    """Positive control for the assertion above.

    Without this the "reason is logged" tests could pass against a helper that
    logs on every batch, which would drown the signal it exists to carry.
    """
    responses.add(
        responses.POST,
        BATCH_URL,
        json=_envelope(processed=1, failed=0, errors=[], accepted_ids=[41]),
        status=200,
    )

    with caplog.at_level(logging.WARNING, logger="src.sync.bf_client"):
        result = client.send_events([_status_span()])

    assert result.success is True
    assert not [
        r for r in caplog.records if "Server rejected" in r.getMessage()
    ], "a clean batch must stay quiet"


@responses.activate
def test_rejection_log_is_bounded_in_count_and_length(client, caplog):
    """A pathological batch must not flood a log file under a retention cap."""
    long_reason = "x" * (MAX_REJECTION_REASON_CHARS + 500)
    errors = [
        {"event": f"evt_{i}", "error": long_reason}
        for i in range(MAX_LOGGED_REJECTIONS + 3)
    ]
    responses.add(
        responses.POST,
        BATCH_URL,
        json=_envelope(processed=0, failed=len(errors), errors=errors),
        status=200,
    )

    with caplog.at_level(logging.WARNING, logger="src.sync.bf_client"):
        client.send_events([_status_span()])

    logged = [r.getMessage() for r in caplog.records if "Server rejected" in r.getMessage()]
    assert len(logged) == 1
    message = logged[0]
    assert message.count("evt_") == MAX_LOGGED_REJECTIONS
    assert "+3 more" in message
    assert long_reason not in message, "the reason must be truncated"
    assert f"Server rejected {len(errors)} event(s)" in message, (
        "the total must still be reported even though the detail is capped"
    )


@responses.activate
def test_malformed_errors_field_does_not_break_the_send(client, caplog):
    """An older/odd server shape must not throw out of the egress path."""
    responses.add(
        responses.POST,
        BATCH_URL,
        json={
            "success": True,
            "data": {
                "processed": 0,
                "failed": 1,
                "errors": "something went wrong",
                "accepted_ids": [],
            },
        },
        status=200,
    )

    with caplog.at_level(logging.WARNING, logger="src.sync.bf_client"):
        result = client.send_events([_status_span()])

    assert result.success is False


@responses.activate
def test_plain_string_errors_entries_are_still_logged(client, caplog):
    """A list of bare strings (no {event, error} wrapper) must survive too.

    Covers the non-dict branch: the string IS the whole reason the agent has,
    so it must still reach the log rather than be dropped as unrecognised.
    """
    responses.add(
        responses.POST,
        BATCH_URL,
        json=_envelope(
            processed=0,
            failed=1,
            errors=["Event timestamp out of acceptable range"],
        ),
        status=200,
    )

    with caplog.at_level(logging.WARNING, logger="src.sync.bf_client"):
        result = client.send_events([_status_span()])

    assert result.success is False
    assert any(
        "Event timestamp out of acceptable range" in r.getMessage()
        for r in caplog.records
    ), "an unwrapped reason must not be discarded"
