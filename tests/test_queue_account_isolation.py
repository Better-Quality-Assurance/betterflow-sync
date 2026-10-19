"""Queued activity is only ever uploaded under the account that recorded it.

Origin: 2026-10-19, a laptop handed from one employee to another. The offline
queue was one per MACHINE and carried no owner. When the server started
rejecting the first person's token (the admin had reassigned the device), the
agent kept tracking and queuing for the ~15 min auth-tolerance window, the
rejected batches went into the queue, and the moment the second person signed
in, _process_queue drained the first person's activity under the second
person's token: it landed as THEIR hours.

The fix tags every queued and dead-lettered row with the account that was
signed in when it was recorded, binds the queue to an account BEFORE a login
makes a new token live, and drops (with a logged count) every row belonging
to anyone else. These tests drive the real LoginManager, the real OfflineQueue
and the real SyncEngine send/drain paths; only the HTTP client is faked, and
the fake records which TOKEN each event was delivered under.
"""

import ast
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from src.auth.login import LoginManager
from src.config import Config
from src.sync.bf_client import AuthResult, SyncResult
from src.sync.http_client import BetterFlowAuthError
from src.sync.queue import OfflineQueue
from src.sync.sync_engine import SyncEngine, SyncStats

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeBF:
    """Stands in for BetterFlowClient. Records the token every accepted event
    was delivered under — the property under test is WHOSE account an event
    lands in, and on the wire that is decided by the token alone."""

    web_base_url = "https://betterflow.test"

    def __init__(self):
        self.token = None
        self.device_id = None
        self.rejected: set = set()
        self.delivered: list[tuple[str, str]] = []  # (token, event id)
        self.next_auth: AuthResult | None = None
        self.send_hook = None

    def set_credentials(self, token, device_id):
        self.token = token
        self.device_id = device_id

    def clear_credentials(self):
        self.token = None

    def get_status(self):
        if self.token is None or self.token in self.rejected:
            raise BetterFlowAuthError("401 Unauthenticated")
        return {}

    def exchange_code(self, code, device_name, code_verifier, device_info=None):
        return self.next_auth

    def send_events(self, events):
        if self.send_hook is not None:
            self.send_hook()
        # Read the token at "request time", AFTER any hook: that is when the
        # real client builds its Authorization header.
        token = self.token
        if token is None or token in self.rejected:
            raise BetterFlowAuthError("401 Unauthenticated")
        ids = [e["id"] for e in events]
        self.delivered.extend((token, i) for i in ids)
        return SyncResult(success=True, events_synced=len(ids), accepted_ids=ids)


def _events(prefix: str, n: int) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    return [
        {
            "id": f"{prefix}-{i}",
            "timestamp": now,
            "duration": 30.0,
            "bucket_id": "aw-watcher-window_host",
            "data": {"app": "Terminal"},
        }
        for i in range(n)
    ]


def _engine(bf: FakeBF, queue: OfflineQueue) -> SyncEngine:
    return SyncEngine(
        aw=Mock(), bf=bf, queue=queue, config=Config(), time_tracker=Mock()
    )


def _login_manager(bf: FakeBF, queue: OfflineQueue, keychain=None) -> LoginManager:
    if keychain is None:
        keychain = MagicMock()
        keychain.store.return_value = True
        keychain.load.return_value = None
    return LoginManager(bf, keychain, account_binder=queue.bind_account)


def _browser_login(mgr: LoginManager, bf: FakeBF, email: str, token: str):
    bf.next_auth = AuthResult(
        success=True, device_id="42", api_token=token, user_email=email,
        user_name=email.split("@")[0], user_role="user",
    )
    flow = Mock()
    flow.start.return_value = SimpleNamespace(
        success=True, code="code", code_verifier="verifier", error=None
    )
    with patch("src.auth.login.BrowserAuthFlow", return_value=flow), patch(
        "src.auth.login.DeviceInfo.collect",
        return_value=SimpleNamespace(machine_id="m" * 16),
    ):
        state = mgr.login_via_browser()
    assert state.logged_in, state.error
    return state


def _drain(engine: SyncEngine) -> None:
    """Drain everything the queue will hand out, dead letters included."""
    engine.queue.requeue_storable_dead_letter(min_dead_age_seconds=0)
    for _ in range(5):
        engine._queue_backoff_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        engine._process_queue(SyncStats())


def _queue_under_rejected_token(engine: SyncEngine, bf: FakeBF, events: list[dict]):
    """The live send path with the current token rejected: exactly what the
    agent does for ~15 min after an admin reassigns the laptop."""
    bf.rejected.add(bf.token)
    with pytest.raises(BetterFlowAuthError):
        engine._send_bucket_events(events, SyncStats())


def _dead_letter(queue: OfflineQueue, events: list[dict]) -> None:
    """Put events in the dead-letter table through the real path."""
    queue.enqueue(events)
    ids = [q.id for q in queue.dequeue(1000) if q.event_data["id"] in {e["id"] for e in events}]
    for _ in range(5):
        queue.increment_retry(ids, "422 rejected")
    queue.remove_failed(max_retries=5)


def _owned_by(bf: FakeBF, prefix: str) -> list[tuple[str, str]]:
    return [(tok, eid) for tok, eid in bf.delivered if eid.startswith(prefix)]


class TestAccountSwitch:
    def test_previous_accounts_queue_is_not_uploaded_under_the_next_login(
        self, tmp_path, caplog
    ):
        bf = FakeBF()
        queue = OfflineQueue(db_path=tmp_path / "q.db")
        engine = _engine(bf, queue)
        mgr = _login_manager(bf, queue)

        _browser_login(mgr, bf, "alice@corp.test", "tok-alice")
        _queue_under_rejected_token(engine, bf, _events("alice", 3))
        _dead_letter(queue, _events("alice-dl", 2))
        assert queue.size() == 3
        assert queue.dead_letter_count() == 2

        caplog.set_level(logging.WARNING)
        _browser_login(mgr, bf, "bob@corp.test", "tok-bob")
        _drain(engine)

        # THE assertion: nothing Alice recorded reached the server as Bob's.
        assert _owned_by(bf, "alice") == []
        # Dropped, not parked behind Bob's own rows.
        assert queue.size() == 0
        assert queue.dead_letter_count() == 0
        # Never silently: the count is in the log.
        drop_lines = [r.getMessage() for r in caplog.records
                      if "another account" in r.getMessage()]
        assert drop_lines, [r.getMessage() for r in caplog.records]
        assert "3 queued" in drop_lines[0] and "2 dead-lettered" in drop_lines[0]

    def test_binder_failure_never_lets_the_new_token_drain_the_old_queue(self, tmp_path):
        """bind_account runs before the new token goes live. If it raises (e.g.
        SQLite 'database is locked'), login must either fail or still never
        deliver the previous account's rows under the new token."""
        bf = FakeBF()
        queue = OfflineQueue(db_path=tmp_path / "q.db")
        engine = _engine(bf, queue)
        fail = {"on": False}

        def binder(*args, **kwargs):
            if fail["on"]:
                raise sqlite3.OperationalError("database is locked")
            return queue.bind_account(*args, **kwargs)

        keychain = MagicMock()
        keychain.store.return_value = True
        keychain.load.return_value = None
        mgr = LoginManager(bf, keychain, account_binder=binder)

        _browser_login(mgr, bf, "alice@corp.test", "tok-alice")
        _queue_under_rejected_token(engine, bf, _events("alice", 2))
        _dead_letter(queue, _events("alice-dl", 1))

        fail["on"] = True
        try:
            _browser_login(mgr, bf, "bob@corp.test", "tok-bob")
        except (AssertionError, sqlite3.OperationalError):
            pass  # login refusing is an acceptable outcome; leaking is not
        assert bf.token != "tok-bob"  # the new token never went live
        keychain.store.assert_called_once()  # only Alice's sign-in stored
        try:
            _drain(engine)
        except BetterFlowAuthError:
            pass  # Alice's token is still the rejected one: nothing can send

        assert [d for d in bf.delivered if d[0] == "tok-bob"] == []
        assert _owned_by(bf, "alice") == []

    def test_next_accounts_own_rows_still_upload(self, tmp_path):
        """Bob's own offline activity after the switch must still drain —
        the fix must not turn into 'drop everything on login'."""
        bf = FakeBF()
        queue = OfflineQueue(db_path=tmp_path / "q.db")
        engine = _engine(bf, queue)
        mgr = _login_manager(bf, queue)

        _browser_login(mgr, bf, "alice@corp.test", "tok-alice")
        _queue_under_rejected_token(engine, bf, _events("alice", 2))
        _browser_login(mgr, bf, "bob@corp.test", "tok-bob")
        queue.enqueue(_events("bob", 2))
        _drain(engine)

        assert sorted(_owned_by(bf, "bob")) == [("tok-bob", "bob-0"), ("tok-bob", "bob-1")]
        assert _owned_by(bf, "alice") == []

    def test_email_case_and_whitespace_is_the_same_account(self, tmp_path):
        bf = FakeBF()
        queue = OfflineQueue(db_path=tmp_path / "q.db")
        engine = _engine(bf, queue)
        mgr = _login_manager(bf, queue)

        _browser_login(mgr, bf, "Alice@Corp.test", "tok-1")
        _queue_under_rejected_token(engine, bf, _events("alice", 2))
        _browser_login(mgr, bf, " alice@corp.test ", "tok-2")
        _drain(engine)

        assert sorted(_owned_by(bf, "alice")) == [("tok-2", "alice-0"), ("tok-2", "alice-1")]


class TestSameAccountRelogin:
    """Control: the person whose token lapsed gets their own activity back."""

    def test_same_account_browser_relogin_drains_its_queue_and_dead_letters(self, tmp_path):
        bf = FakeBF()
        queue = OfflineQueue(db_path=tmp_path / "q.db")
        engine = _engine(bf, queue)
        mgr = _login_manager(bf, queue)

        _browser_login(mgr, bf, "alice@corp.test", "tok-alice")
        _queue_under_rejected_token(engine, bf, _events("alice", 3))
        _dead_letter(queue, _events("alice-dl", 1))

        _browser_login(mgr, bf, "alice@corp.test", "tok-alice-2")
        _drain(engine)

        got = sorted(_owned_by(bf, "alice"))
        assert got == [
            ("tok-alice-2", "alice-0"), ("tok-alice-2", "alice-1"),
            ("tok-alice-2", "alice-2"), ("tok-alice-2", "alice-dl-0"),
        ]

    def test_restored_session_after_restart_drains_its_queue(self, tmp_path):
        """The tag must survive a restart: a new process, a new queue object on
        the same file, and the session restored from the keychain."""
        bf = FakeBF()
        queue = OfflineQueue(db_path=tmp_path / "q.db")
        engine = _engine(bf, queue)
        _browser_login(_login_manager(bf, queue), bf, "alice@corp.test", "tok-alice")
        _queue_under_rejected_token(engine, bf, _events("alice", 2))
        queue.close()

        bf2 = FakeBF()
        queue2 = OfflineQueue(db_path=tmp_path / "q.db")
        keychain = MagicMock()
        keychain.load.return_value = SimpleNamespace(
            api_token="tok-alice-new", device_id="42", user_email="alice@corp.test",
            user_name="alice", user_role="user",
        )
        state = _login_manager(bf2, queue2, keychain).try_auto_login()
        assert state.logged_in
        _drain(_engine(bf2, queue2))

        assert sorted(_owned_by(bf2, "alice")) == [
            ("tok-alice-new", "alice-0"), ("tok-alice-new", "alice-1"),
        ]


class TestInFlightDelivery:
    def test_a_batch_in_flight_at_switch_time_is_not_sent_with_the_new_token(
        self, tmp_path
    ):
        """The sync thread can be mid-drain when the next person signs in on
        the login thread. The new token must not go live while a batch the
        previous account owns is still on its way out."""
        bf = FakeBF()
        queue = OfflineQueue(db_path=tmp_path / "q.db")
        engine = _engine(bf, queue)
        mgr = _login_manager(bf, queue)
        _browser_login(mgr, bf, "alice@corp.test", "tok-alice")
        queue.enqueue(_events("alice", 2))

        in_send = threading.Event()
        release = threading.Event()

        def hook():
            bf.send_hook = None
            in_send.set()
            release.wait(5)

        # Daemon threads, and any exception they raise is captured: a deadlock
        # between drain and login must fail this test, not hang the run, and a
        # failed login must not be reported as a token mismatch.
        errors: dict[str, BaseException] = {}

        def run(name, fn, *args):
            try:
                fn(*args)
            except BaseException as e:  # noqa: BLE001 - re-raised via the assert below
                errors[name] = e

        bf.send_hook = hook
        drain = threading.Thread(target=run, args=("drain", _drain, engine), daemon=True)
        drain.start()
        assert in_send.wait(5)

        login = threading.Thread(
            target=run,
            args=("login", _browser_login, mgr, bf, "bob@corp.test", "tok-bob"),
            daemon=True,
        )
        login.start()
        time.sleep(0.3)  # give the login thread every chance to race ahead
        release.set()
        drain.join(10)
        login.join(10)

        assert not drain.is_alive() and not login.is_alive(), "drain/login deadlocked"
        assert errors == {}, errors
        assert sorted(_owned_by(bf, "alice")) == [
            ("tok-alice", "alice-0"), ("tok-alice", "alice-1"),
        ]
        assert bf.token == "tok-bob"


# The pre-upgrade schema, verbatim from origin/main at 152fb0e: no account
# column on either table. Built by hand so the legacy rows are the bytes an
# upgrading machine actually has on disk.
_LEGACY_DDL = [
    """CREATE TABLE queued_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_data TEXT NOT NULL,
        created_at TEXT NOT NULL,
        retry_count INTEGER DEFAULT 0,
        last_error TEXT)""",
    """CREATE TABLE dead_letter_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        original_id INTEGER,
        event_data TEXT NOT NULL,
        bucket_id TEXT,
        created_at TEXT NOT NULL,
        retry_count INTEGER,
        last_error TEXT,
        dropped_at TEXT NOT NULL,
        terminal INTEGER NOT NULL DEFAULT 0)""",
]


def _legacy_db(path: Path, queued: list[dict], dead: list[dict]) -> None:
    import json

    conn = sqlite3.connect(str(path))
    for ddl in _LEGACY_DDL:
        conn.execute(ddl)
    now = datetime.now(timezone.utc)
    for e in queued:
        conn.execute(
            "INSERT INTO queued_events (event_data, created_at) VALUES (?, ?)",
            (json.dumps(e), now.isoformat()),
        )
    for e in dead:
        conn.execute(
            "INSERT INTO dead_letter_events (event_data, bucket_id, created_at, "
            "retry_count, dropped_at) VALUES (?, ?, ?, 5, ?)",
            (json.dumps(e), e["bucket_id"], now.isoformat(),
             (now - timedelta(hours=1)).isoformat()),
        )
    conn.commit()
    conn.close()


class TestLegacyUntaggedRows:
    def test_restored_session_at_upgrade_claims_and_uploads_legacy_rows(self, tmp_path):
        """Upgrade with the same person still signed in (session restored from
        the keychain): their pre-upgrade backlog is theirs and must upload."""
        _legacy_db(tmp_path / "q.db", _events("old", 2), _events("old-dl", 1))
        bf = FakeBF()
        queue = OfflineQueue(db_path=tmp_path / "q.db")
        keychain = MagicMock()
        keychain.load.return_value = SimpleNamespace(
            api_token="tok-alice", device_id="42", user_email="alice@corp.test",
            user_name="alice", user_role="user",
        )
        assert _login_manager(bf, queue, keychain).try_auto_login().logged_in
        _drain(_engine(bf, queue))

        assert sorted(_owned_by(bf, "old")) == [
            ("tok-alice", "old-0"), ("tok-alice", "old-1"), ("tok-alice", "old-dl-0"),
        ]

    def test_fresh_login_at_upgrade_drops_legacy_rows_with_a_count(self, tmp_path, caplog):
        """Upgrade with NOBODY's session to restore, then a browser sign-in:
        the backlog's owner is unknowable, so it must not become this person's
        hours."""
        _legacy_db(tmp_path / "q.db", _events("old", 2), _events("old-dl", 1))
        bf = FakeBF()
        queue = OfflineQueue(db_path=tmp_path / "q.db")
        caplog.set_level(logging.WARNING)
        _browser_login(_login_manager(bf, queue), bf, "bob@corp.test", "tok-bob")
        _drain(_engine(bf, queue))

        assert _owned_by(bf, "old") == []
        assert queue.size() == 0 and queue.dead_letter_count() == 0
        lines = [r.getMessage() for r in caplog.records if "untagged" in r.getMessage()]
        assert lines, [r.getMessage() for r in caplog.records]
        assert "2 queued" in lines[0] and "1 dead-lettered" in lines[0], lines[0]

    def test_legacy_claim_happens_once_not_on_every_restore(self, tmp_path):
        """The 'restored session owns the legacy rows' rule is an UPGRADE rule.
        After Alice's restore claimed them, Bob's later restore must not claim
        anything — and must still drop Alice's rows."""
        _legacy_db(tmp_path / "q.db", _events("old", 1), [])
        bf = FakeBF()
        queue = OfflineQueue(db_path=tmp_path / "q.db")
        kc = MagicMock()
        kc.load.return_value = SimpleNamespace(
            api_token="tok-alice", device_id="42", user_email="alice@corp.test",
            user_name="alice", user_role="user",
        )
        bf.rejected.add("tok-alice")  # Alice's restore is rejected: nothing drains
        _login_manager(bf, queue, kc).try_auto_login()
        assert queue.size() == 1

        kc.load.return_value = SimpleNamespace(
            api_token="tok-bob", device_id="42", user_email="bob@corp.test",
            user_name="bob", user_role="user",
        )
        assert _login_manager(bf, queue, kc).try_auto_login().logged_in
        _drain(_engine(bf, queue))
        assert _owned_by(bf, "old") == []


class TestActivityWatchBacklogAfterSwitch:
    """ActivityWatch's buckets are per-MACHINE too. After a switch, the
    start-of-day reconcile and the live fetch must not reach back into the
    previous person's part of the day."""

    def _switched_engine(self, tmp_path):
        bf = FakeBF()
        queue = OfflineQueue(db_path=tmp_path / "q.db")
        engine = _engine(bf, queue)
        mgr = _login_manager(bf, queue)
        _browser_login(mgr, bf, "alice@corp.test", "tok-alice")
        queue.set_checkpoint(
            "aw-watcher-window_host", datetime.now(timezone.utc) - timedelta(hours=3)
        )
        _browser_login(mgr, bf, "bob@corp.test", "tok-bob")
        return engine, queue

    def test_checkpoints_move_to_the_switch(self, tmp_path):
        engine, queue = self._switched_engine(tmp_path)
        cp = queue.get_checkpoint("aw-watcher-window_host")
        assert cp >= queue.owner_since

    def test_live_fetch_does_not_look_back_past_the_switch(self, tmp_path):
        engine, queue = self._switched_engine(tmp_path)
        engine.aw.get_events = Mock(return_value=[])
        engine._fetch_bucket_events("aw-watcher-window_host", SyncStats())
        start = engine.aw.get_events.call_args_list[0].kwargs["start"]
        assert start >= queue.owner_since

    def test_reconcile_starts_at_the_switch_not_midnight(self, tmp_path):
        engine, queue = self._switched_engine(tmp_path)
        engine.aw.get_events = Mock(return_value=[])
        bucket = SimpleNamespace(id="aw-watcher-window_host")
        engine._reconcile_backlog([bucket])
        start = engine.aw.get_events.call_args_list[0].kwargs["start"]
        assert start >= queue.owner_since


def test_app_wires_the_queue_into_the_login_manager():
    """Callsite guard: the binding only protects anyone if the app passes the
    queue's binder to the LoginManager it actually uses."""
    tree = ast.parse((REPO_ROOT / "src" / "main.py").read_text())
    wired = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "LoginManager":
            for kw in node.keywords:
                if kw.arg == "account_binder" and ast.unparse(kw.value) == "self.queue.bind_account":
                    wired = True
    assert wired
