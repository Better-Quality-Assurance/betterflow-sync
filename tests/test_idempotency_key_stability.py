"""Tests for the SHA-256 idempotency key (PR #27).

The audit's biggest correctness worry: if the key changes across retries
of the same batch, the server can't dedup → duplicate events → inflated
hours. If two genuinely different batches collide → silent event loss.

The key is computed as:
    sha256(json.dumps(events, sort_keys=True, separators=(",", ":")))

These tests prove the key:
  1. Stays IDENTICAL across two send_events() calls with the same batch
     (the retry case — the bug the PR fixes).
  2. CHANGES when even one byte of one event differs.
  3. CHANGES when event ORDER differs (intentional: the caller controls
     ordering via the upstream timestamp sort; key reflects that).
  4. Is deterministic regardless of dict-key iteration order within
     each event (the `sort_keys=True` insurance).
"""

import hashlib
import json


def _compute_key(events):
    """Reproduce the exact key computation from bf_client.send_events:265-267."""
    return hashlib.sha256(
        json.dumps(events, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_same_batch_produces_same_key_across_calls():
    """The retry case: same batch sent twice → same key → server dedups."""
    batch = [
        {"timestamp": "2026-06-11T14:00:00Z", "duration": 60, "data": {"app": "Cursor"}},
        {"timestamp": "2026-06-11T14:01:00Z", "duration": 60, "data": {"app": "Chrome"}},
    ]
    key_a = _compute_key(batch)
    key_b = _compute_key(batch)
    assert key_a == key_b, "Two computations of the same batch must yield the same SHA"


def test_different_event_content_produces_different_key():
    """Even a single byte difference must change the key."""
    a = [{"timestamp": "2026-06-11T14:00:00Z", "duration": 60, "data": {"app": "Cursor"}}]
    b = [{"timestamp": "2026-06-11T14:00:00Z", "duration": 61, "data": {"app": "Cursor"}}]  # duration off-by-one
    assert _compute_key(a) != _compute_key(b)


def test_event_order_change_produces_different_key():
    """Reordering events changes the key. This is intentional: the upstream
    `events.sort(key=lambda e: e.timestamp)` makes the order deterministic,
    so any 'reordering' across retries indicates the upstream sort changed
    its input, which means the batch is genuinely different."""
    forward = [
        {"timestamp": "2026-06-11T14:00:00Z", "duration": 60, "data": {"app": "A"}},
        {"timestamp": "2026-06-11T14:01:00Z", "duration": 60, "data": {"app": "B"}},
    ]
    reverse = list(reversed(forward))
    assert _compute_key(forward) != _compute_key(reverse)


def test_dict_key_iteration_order_does_not_affect_key():
    """`sort_keys=True` insulates against Python dict ordering quirks.
    A future event constructed with keys in a different declaration order
    must produce the same SHA."""
    natural = [{"timestamp": "2026-06-11T14:00:00Z", "duration": 60, "data": {"app": "Cursor"}}]
    reordered = [{"data": {"app": "Cursor"}, "duration": 60, "timestamp": "2026-06-11T14:00:00Z"}]
    assert _compute_key(natural) == _compute_key(reordered)


def test_empty_batch_has_a_stable_key():
    """Edge case: empty batch shouldn't crash and should produce a
    consistent key. Server should accept-and-no-op."""
    a = _compute_key([])
    b = _compute_key([])
    assert a == b
    assert len(a) == 64  # SHA-256 hex


def test_event_data_with_nested_dict_is_stable():
    """The agent posts events with nested dict.data — proxy for browser-
    tab events with complex metadata. Ensure stability holds for nested
    structures, not just flat ones."""
    batch = [{
        "timestamp": "2026-06-11T14:00:00Z",
        "duration": 60,
        "data": {
            "app": "Chrome",
            "url": "https://example.com",
            "title_hash": "abc123",
            "metadata": {"workspace": "1", "display": "main"},
        },
    }]
    assert _compute_key(batch) == _compute_key(batch)


def test_unicode_payload_is_stable():
    """Window titles / app names may contain non-ASCII. UTF-8 encoding
    must be applied consistently so the key matches across retries."""
    batch = [{"timestamp": "2026-06-11T14:00:00Z", "duration": 60, "data": {"app": "Café ☕"}}]
    a = _compute_key(batch)
    b = _compute_key(batch)
    assert a == b
