"""An alert with no kind was ambiguous between "old agent" and "unrecognised reason".

#254 named a local sync failure by its kind. But when no kind matched, the
final branch of `server_status_summary` emitted the very string #254 replaced:

    "; 1 local reason(s) recorded in local dead-letter"

That is byte-identical to what a pre-#254 agent sends, so an operator cannot
tell "this agent predates the fix" from "this agent ran the fix and the
reason matched none of our prefixes". Absence was not self-describing
(fail-closed.md Rule 4), and it read in the reassuring direction.

The fix emits a fixed literal, ``[unclassified]``, when no kind matched. It is
a token we wrote, chosen from no input, so the redaction property (only our
own tokens leave the device) is unchanged.
"""

from src.sync.sync_engine import server_status_summary

PRE_254_BYTES = "; 1 local reason(s) recorded in local dead-letter"
UNKNOWN = "Bank details 1234-5678 rejected by upstream"


def test_an_unmatched_reason_is_not_byte_identical_to_a_pre_254_agent():
    out = server_status_summary([UNKNOWN])
    assert out != PRE_254_BYTES, out
    assert out == "; 1 local reason(s) [unclassified] recorded in local dead-letter", out


def test_an_unmatched_reason_beside_a_server_status_says_unclassified():
    out = server_status_summary(["API error (500): boom", UNKNOWN])
    assert out == ("; server status 500 plus 1 local reason(s) [unclassified], "
                   "full detail in local dead-letter"), out


def test_CONTROL_the_unclassified_reason_text_still_never_leaves_the_device():
    out = server_status_summary([UNKNOWN])
    for leaked in ("1234-5678", "Bank details", "upstream"):
        assert leaked not in out, f"{leaked!r} leaked: {out}"


def test_CONTROL_a_matched_reason_carries_its_kind_and_not_unclassified():
    out = server_status_summary(["Failed to sync bucket aw-watcher-window_HOST: boom"])
    assert out == "; 1 local reason(s) [bucket-sync-failed] recorded in local dead-letter", out


def test_CONTROL_status_only_reasons_are_unchanged():
    out = server_status_summary(["API error (422): Draft.docx"])
    assert out == "; server status 422, full reason in local dead-letter", out


def test_CONTROL_no_reasons_is_still_empty():
    assert server_status_summary([]) == ""
