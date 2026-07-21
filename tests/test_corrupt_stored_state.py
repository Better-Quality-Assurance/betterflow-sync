"""Corrupt stored state must degrade, not wedge.

Two one-token `except` tuples stand between a bad stored row and a permanently
stuck agent, and a refactor can undo either with no visible signal:

* `KeychainManager.load()` — a ValueError escaping `StoredCredentials.from_json`
  propagates through `try_auto_login` into a background thread's fatal
  excepthook, leaving the app stuck at "Restoring session...".
* `OfflineQueue.dequeue()` — a row with a non-ISO `created_at` raises inside
  `QueuedEvent.from_row`, so the drain never completes and the queue never
  empties.

Both failures only appear on a real user's machine with an already-bad row.
"""

import json
from unittest.mock import patch

from src.auth.keychain import KeychainManager
from src.sync.queue import OfflineQueue


def _blob(**overrides):
    data = {
        "api_token": "tok",
        "device_id": "dev-1",
        "user_email": "user@example.com",
    }
    data.update(overrides)
    return json.dumps(data)


def _load_with(blob):
    with patch("src.auth.keychain.keyring.get_password", return_value=blob):
        return KeychainManager().load()


def test_load_returns_none_for_oversized_token():
    assert _load_with(_blob(api_token="x" * 5000)) is None


def test_load_returns_none_for_token_with_newline():
    assert _load_with(_blob(api_token="tok\nX-Injected: 1")) is None
    assert _load_with(_blob(api_token="tok\rX-Injected: 1")) is None


def test_load_returns_none_for_non_string_token():
    assert _load_with(_blob(api_token=1234)) is None


def test_load_returns_credentials_for_a_good_blob():
    creds = _load_with(_blob())
    assert creds is not None
    assert creds.api_token == "tok"
    assert creds.device_id == "dev-1"


def test_dequeue_drops_row_with_unparseable_created_at(tmp_path):
    queue = OfflineQueue(db_path=tmp_path / "queue.db")
    queue.enqueue([{"id": "first"}, {"id": "second"}])

    # Corrupt the OLDEST row's timestamp: it sorts first, so a raising
    # from_row would take the whole batch — including the healthy row behind it.
    with queue._cursor() as cursor:
        cursor.execute("SELECT id FROM queued_events ORDER BY created_at ASC LIMIT 1")
        bad_id = cursor.fetchone()[0]
        cursor.execute(
            "UPDATE queued_events SET created_at = ? WHERE id = ?",
            ("not-a-timestamp", bad_id),
        )

    events = queue.dequeue()
    assert [e.event_data["id"] for e in events] == ["second"]

    # The corrupt row is removed, not left to shrink every future batch.
    with queue._cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM queued_events WHERE id = ?", (bad_id,))
        assert cursor.fetchone()[0] == 0
