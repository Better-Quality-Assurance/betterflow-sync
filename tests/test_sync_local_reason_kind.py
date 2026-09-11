"""A local sync failure told ops a count and nothing else.

`_note_sync_failure` reports a streak to the cross-tenant ops ingest, and it
passes the reason through `server_status_summary` first. That redaction is
right and its rationale is documented: the ingest is cross-tenant, a validation
error routinely echoes the value it rejected, and our payloads carry window
titles. Only digits matched by ``_SERVER_STATUS_RE`` — ``^API error \\((\\d{3})\\)``
— are ever emitted.

But a reason with no HTTP status fell to the last branch, which emitted a COUNT:

    "; 1 local reason(s) recorded in local dead-letter"

That is what reached the board on 2026-09-10T10:39Z (fingerprint
sync-repeated-failure, release 1.5.133), and it is the whole event: `stack` was
NULL because the `not stats.success` caller in _do_sync passes no `exc`, and `context` carries
only `consecutive_failures`. So an operator is told sync has failed three times
and the reason is on the user's laptop.

Every one of those reasons begins with a prefix WE wrote — "ActivityWatch is not
running", "Failed to get buckets", "Failed to sync bucket", "Authentication
error". Emitting the matched PREFIX names the failure while shipping none of the
interpolated text, which is the same principle server_status_summary already
works on: emit only tokens we control.
"""

import re

from src.sync.sync_engine import local_reason_kinds, server_status_summary


def test_a_local_reason_is_named_by_kind_not_only_counted():
    # The defect. "Failed to sync bucket aw-watcher-window_HOST: <e>" told ops
    # nothing; the kind tells them which subsystem to look at.
    out = server_status_summary(["Failed to sync bucket aw-watcher-window_LAPTOP: boom"])
    assert "bucket-sync-failed" in out, out


def test_the_interpolated_text_never_leaves_the_device():
    # THE LOAD-BEARING TEST. The bucket id, the host name and the exception text
    # are all in the input and none may appear in the output.
    reason = "Failed to sync bucket aw-watcher-window_SECRETHOST: ValueError('Draft contract.docx')"
    out = server_status_summary([reason])
    for leaked in ("SECRETHOST", "ValueError", "Draft contract.docx", "aw-watcher-window"):
        assert leaked not in out, f"{leaked!r} leaked into the ops summary: {out}"


def test_an_unrecognised_reason_is_COUNTED_not_named():
    # A reason we did not write has unknown provenance — sync_engine.py:3525
    # appends `result.error` straight through. Naming it would reopen exactly
    # the hole this redaction exists to close, so it is counted.
    out = server_status_summary(["Bank details 1234-5678 rejected by upstream"])
    assert "1234-5678" not in out
    assert "Bank details" not in out
    assert "1 local reason" in out, out


def test_kinds_come_from_a_closed_set():
    # Bounded by construction: the emitted token is chosen FROM our list, never
    # derived from the input, so the message stays groupable.
    known = set(local_reason_kinds(["ActivityWatch is not running"]))
    assert known == {"activitywatch-down"}
    everything = local_reason_kinds([
        "ActivityWatch is not running",
        "Failed to get buckets: x",
        "Failed to sync bucket b: y",
        "Authentication error: z",
        "something nobody wrote",
    ])
    assert everything == ["activitywatch-down", "bucket-list-failed", "bucket-sync-failed", "auth-error"]
    for kind in everything:
        assert re.fullmatch(r"[a-z-]+", kind), kind


def test_the_order_comes_from_our_list_not_from_the_input():
    # Supplied in REVERSE list order on purpose. An implementation that walks
    # the input instead of _LOCAL_REASON_KINDS returns them reversed, and two
    # runs with the same failures in a different sequence then produce two
    # different messages and group apart. An earlier draft of this file could
    # not tell the two apart: every fixture happened to arrive in list order,
    # so the mutant survived. Caught by the matrix, not by reading.
    reversed_input = [
        "Authentication error: z",
        "Failed to sync bucket b: y",
        "Failed to get buckets: x",
        "ActivityWatch is not running",
    ]
    assert local_reason_kinds(reversed_input) == [
        "activitywatch-down", "bucket-list-failed", "bucket-sync-failed", "auth-error",
    ]


def test_repeats_collapse_so_the_message_stays_bounded():
    # Twenty failing buckets is one kind, not twenty tokens.
    out = local_reason_kinds([f"Failed to sync bucket b{i}: e" for i in range(20)])
    assert out == ["bucket-sync-failed"]


def test_NEGATIVE_CONTROL_a_server_status_still_wins_and_still_redacts():
    # The existing behaviour must not move: a reason WITH a status keeps
    # reporting the status, and still ships no server text.
    out = server_status_summary(["API error (500): Draft contract.docx is invalid"])
    assert "server status 500" in out
    assert "Draft contract.docx" not in out


def test_a_mixed_batch_counts_only_the_uncoded_reasons_and_names_them():
    # The branch this change altered: the count came from `len(items) - len(codes)`
    # and now comes from the uncoded reasons themselves. One server rejection plus
    # one local failure is one of each, and the local one is named.
    out = server_status_summary(["API error (500): x", "Failed to sync bucket b: y"])
    assert out == ("; server status 500 plus 1 local reason(s) [bucket-sync-failed], "
                   "full detail in local dead-letter")


def test_two_rejections_sharing_one_status_are_not_reported_as_mixed():
    # `codes` is de-duplicated and `items` is not, so counting them against each
    # other (len(items) - len(codes) = 2 - 1) classified a pure server rejection
    # as mixed and invented a local reason. Measured on the pre-#254 bytes:
    #   "; server status 422 plus 1 local reason(s), full detail in local dead-letter"
    # ("plus 0" was never reachable: this branch needed len(items) > len(codes).)
    out = server_status_summary(["API error (422): duration",
                                 "API error (422): unknown project"])
    assert out == "; server status 422, full reason in local dead-letter"


def test_NEGATIVE_CONTROL_no_reasons_is_still_empty():
    assert server_status_summary([]) == ""
    assert server_status_summary(None) == ""
