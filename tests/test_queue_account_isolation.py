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
from src.sync.bf_client import AuthResult, REASON_CREDENTIALS_CHANGED, SyncResult
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
        self.generation = 0

    def set_credentials(self, token, device_id):
        self.token = token
        self.device_id = device_id
        self.generation += 1

    def clear_credentials(self):
        self.token = None
        self.generation += 1

    def credential_generation(self):
        return self.generation

    def get_status(self):
        if self.token is None or self.token in self.rejected:
            raise BetterFlowAuthError("401 Unauthenticated")
        return {}

    def exchange_code(self, code, device_name, code_verifier, device_info=None):
        return self.next_auth

    def send_events(self, events, *, expected_generation=None):
        if self.send_hook is not None:
            self.send_hook()
        # Read the token at "request time", AFTER any hook: that is when the
        # real client builds its Authorization header — and, like the real
        # client (http_client._get_headers), refuses a batch pinned to
        # credentials that have since been replaced.
        if expected_generation is not None and expected_generation != self.generation:
            return SyncResult(
                success=False, transient=True, error=REASON_CREDENTIALS_CHANGED
            )
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
        the login thread. The sign-in must not wait for the in-flight send
        (no lock across network I/O), and the batch, prepared under the
        previous account's credentials, must never go out with the new token."""
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
        login.join(2)
        # The sign-in completed while the send was still hung.
        assert not login.is_alive(), "sign-in blocked behind an in-flight send"
        release.set()
        drain.join(10)

        assert not drain.is_alive(), "drain never finished"
        assert errors == {}, errors
        assert bf.token == "tok-bob"
        assert [d for d in bf.delivered if d[0] == "tok-bob"] == []
        assert _owned_by(bf, "alice") == []
        assert queue.size() == 0


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


# ---- Fix round 1 (PR #282 review): one egress rule ---------------------------


def _future_events(prefix: str, n: int, minutes: int = 1) -> list[dict]:
    """Events timestamped AFTER any switch made now, so the owner_since clip
    cannot be what stops them: isolates the credential/owner checks."""
    ts = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    evs = _events(prefix, n)
    for e in evs:
        e["timestamp"] = ts
    return evs


class TestOneEgressRule:
    def test_events_fetched_before_a_switch_are_not_sent_after_it(self, tmp_path):
        """sync() fetches without any lock; the next person can finish signing
        in between the fetch and the send."""
        bf = FakeBF()
        q = OfflineQueue(db_path=tmp_path / "q.db")
        eng = _engine(bf, q)
        mgr = _login_manager(bf, q)
        _browser_login(mgr, bf, "alice@corp.test", "tok-alice")
        fetched = _events("alice", 2)
        for e in fetched:
            e["timestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        _browser_login(mgr, bf, "bob@corp.test", "tok-bob")
        eng._send_and_advance_checkpoints(fetched, [], SyncStats())
        assert _owned_by(bf, "alice") == []
        assert q.size() == 0

    def test_a_cycle_pinned_to_one_account_cannot_send_or_queue_as_the_next(
        self, tmp_path, caplog
    ):
        """Even data timestamped after the switch: a cycle that started as
        Alice is refused at egress (credentials changed) and its rows are
        refused by the queue (owner no longer bound) — never re-tagged Bob."""
        bf = FakeBF()
        q = OfflineQueue(db_path=tmp_path / "q.db")
        eng = _engine(bf, q)
        mgr = _login_manager(bf, q)
        _browser_login(mgr, bf, "alice@corp.test", "tok-alice")
        caplog.set_level(logging.WARNING)
        with eng._egress_cycle():
            _browser_login(mgr, bf, "bob@corp.test", "tok-bob")
            eng._send_events(_future_events("alice", 2), SyncStats())
        assert bf.delivered == []
        assert q.size() == 0
        assert any("Refused 2 event(s)" in r.getMessage() for r in caplog.records)

    def test_a_401_batch_is_not_requeued_as_the_account_bound_since(self, tmp_path):
        bf = FakeBF()
        q = OfflineQueue(db_path=tmp_path / "q.db")
        eng = _engine(bf, q)
        mgr = _login_manager(bf, q)
        _browser_login(mgr, bf, "alice@corp.test", "tok-alice")
        bf.rejected.add("tok-alice")
        with eng._egress_cycle():
            # Bound to Bob, Bob's token not live yet: the bind-then-activate gap.
            q.bind_account("bob@corp.test", restored_session=False)
            with pytest.raises(BetterFlowAuthError):
                eng._send_and_advance_checkpoints(_future_events("alice", 2), [], SyncStats())
        assert q.size() == 0

    def test_inproc_afk_spans_from_before_a_switch_never_upload(self, tmp_path):
        """Reviewer repro: Alice's held in-process AFK checkpoint after auth
        failures rebuilt ~40 not-afk spans and delivered them under Bob."""
        from src.sync.afk_source import AfkSource

        bf = FakeBF()
        queue = OfflineQueue(db_path=tmp_path / "q.db")
        mgr = _login_manager(bf, queue)
        _browser_login(mgr, bf, "alice@corp.test", "tok-alice")
        cfg = Config()
        cfg.sync.in_process_afk = True
        eng = SyncEngine(aw=Mock(), bf=bf, queue=queue, config=cfg, time_tracker=Mock())
        eng.afk_source = AfkSource(600, "host", idle_clock=lambda: 5.0, retention_seconds=7200.0)
        now = datetime.now(timezone.utc)
        t_a = now - timedelta(minutes=40)
        eng._afk_inproc_checkpoint = t_a
        for m in range(40):
            eng.afk_source.record_sample(t_a + timedelta(minutes=m), protect_since=t_a)
        _browser_login(mgr, bf, "bob@corp.test", "tok-bob")
        with eng._egress_cycle():
            # The switch was noticed at the cycle's snapshot: Alice's held
            # checkpoint no longer reaches back before Bob took over.
            assert eng._afk_inproc_checkpoint >= queue.owner_since
            eng.afk_source.record_sample(now, protect_since=eng._afk_inproc_checkpoint)
            events = eng._build_inproc_afk(now)
            eng._send_events(events, SyncStats())
        delivered_bob = {eid for tok, eid in bf.delivered if tok == "tok-bob"}
        leaked = [
            e for e in events
            if e["id"] in delivered_bob
            and datetime.fromisoformat(e["timestamp"]) < queue.owner_since
        ]
        assert leaked == []


class TestNoLockAcrossNetworkIO:
    def test_a_hung_send_blocks_neither_another_send_nor_pause_nor_sign_in(self, tmp_path):
        bf = FakeBF()
        queue = OfflineQueue(db_path=tmp_path / "q.db")
        mgr = _login_manager(bf, queue)
        _browser_login(mgr, bf, "alice@corp.test", "tok-alice")
        eng = _engine(bf, queue)
        hang = threading.Event()
        entered = threading.Event()

        def hung():
            bf.send_hook = None
            entered.set()
            hang.wait(30)

        bf.send_hook = hung
        threading.Thread(
            target=lambda: eng._send_events(_events("zombie", 1), SyncStats()),
            daemon=True,
        ).start()
        assert entered.wait(5)
        try:
            def timed(fn):
                done = threading.Event()

                def run():
                    fn()
                    done.set()

                threading.Thread(target=run, daemon=True).start()
                return done.wait(1.0)

            assert timed(lambda: eng._send_events(_events("fresh", 1), SyncStats())), \
                "a second send blocked behind the hung one"
            assert timed(eng.pause), "pause() blocked behind the hung send"
            assert timed(lambda: queue.bind_account("alice@corp.test", restored_session=True)), \
                "sign-in binding blocked behind the hung send"
        finally:
            hang.set()


class TestSamePersonSigningInAgain:
    def test_keychain_loss_re_sign_in_is_not_a_switch(self, tmp_path):
        """macOS re-signing the bundle makes the keychain unreadable and forces
        a browser sign-in (restored_session=False). Same person: nothing is
        dropped and nothing is moved."""
        bf = FakeBF()
        q = OfflineQueue(db_path=tmp_path / "q.db")
        eng = _engine(bf, q)
        mgr = _login_manager(bf, q)
        _browser_login(mgr, bf, "alice@corp.test", "tok-alice")
        _queue_under_rejected_token(eng, bf, _events("alice", 2))
        cp = datetime.now(timezone.utc) - timedelta(hours=3)
        q.set_checkpoint("aw-watcher-window_host", cp)
        since = q.owner_since

        r = q.bind_account("Alice@Corp.test", restored_session=False)
        assert r["switched"] is False
        assert q.size() == 2
        assert q.owner_since == since
        assert q.get_checkpoint("aw-watcher-window_host") == cp


class TestSignInWithoutAnAccount:
    def test_no_email_from_the_server_refuses_sign_in(self, tmp_path):
        bf = FakeBF()
        q = OfflineQueue(db_path=tmp_path / "q.db")
        keychain = MagicMock()
        keychain.store.return_value = True
        mgr = _login_manager(bf, q, keychain)
        bf.next_auth = AuthResult(
            success=True, device_id="42", api_token="tok-x", user_email=None,
            user_name="x", user_role="user",
        )
        flow = Mock()
        flow.start.return_value = SimpleNamespace(
            success=True, code="c", code_verifier="v", error=None
        )
        with patch("src.auth.login.BrowserAuthFlow", return_value=flow), patch(
            "src.auth.login.DeviceInfo.collect",
            return_value=SimpleNamespace(machine_id="m" * 16),
        ):
            state = mgr.login_via_browser()
        assert state.logged_in is False
        assert "which account" in state.error
        assert bf.token is None
        keychain.store.assert_not_called()


def _insert_row(q: OfflineQueue, table: str, account: str, event: dict, **extra):
    import json

    now = datetime.now(timezone.utc)
    conn = q._get_connection()
    if table == "queued_events":
        conn.execute(
            "INSERT INTO queued_events (event_data, created_at, account) VALUES (?, ?, ?)",
            (json.dumps(event), now.isoformat(), account),
        )
    else:
        conn.execute(
            "INSERT INTO dead_letter_events (event_data, bucket_id, created_at, "
            "retry_count, dropped_at, account) VALUES (?, ?, ?, 5, ?, ?)",
            (json.dumps(event), event["bucket_id"], now.isoformat(),
             (now - timedelta(hours=2)).isoformat(), account),
        )
    conn.commit()


class TestQueueOwnerWalls:
    """Each wall on its own, so removing one is seen (the purge in
    bind_account otherwise hides the read filters)."""

    def test_dequeue_never_hands_out_another_accounts_row(self, tmp_path):
        q = OfflineQueue(db_path=tmp_path / "q.db")
        q.bind_account("alice@corp.test", restored_session=False)
        _insert_row(q, "queued_events", "bob@corp.test", _events("bob", 1)[0])
        assert q.dequeue(100) == []
        assert q.dequeue(100, account="alice@corp.test") == []

    def test_replay_never_resurrects_another_accounts_dead_letter(self, tmp_path):
        q = OfflineQueue(db_path=tmp_path / "q.db")
        q.bind_account("alice@corp.test", restored_session=False)
        _insert_row(q, "dead_letter_events", "bob@corp.test", _events("bob", 1)[0])
        r = q.requeue_storable_dead_letter(min_dead_age_seconds=0)
        assert r["requeued"] == 0 and q.size() == 0

    def test_eviction_keeps_the_owner_tag(self, tmp_path):
        q = OfflineQueue(db_path=tmp_path / "q.db")
        q.bind_account("alice@corp.test", restored_session=False)
        ev = _events("alice", 1)[0]
        ev.pop("bucket_id")  # bucketless: unstorable, evicted to dead letter
        q.enqueue([ev])
        assert q.evict_unstorable()["count"] == 1
        rows = q._get_connection().execute(
            "SELECT account FROM dead_letter_events").fetchall()
        assert [r[0] for r in rows] == ["alice@corp.test"]

    def test_dequeue_refuses_a_snapshot_account_no_longer_bound(self, tmp_path):
        """Same owner rule as enqueue/replay: Alice's snapshot draining after
        Bob is bound gets nothing, even rows the switch did not purge."""
        q = OfflineQueue(db_path=tmp_path / "q.db")
        q.bind_account("bob@corp.test", restored_session=False)
        _insert_row(q, "queued_events", "alice@corp.test", _events("alice", 1)[0])
        assert q.dequeue(100, account="alice@corp.test") == []

    def test_enqueue_refuses_rows_of_an_account_no_longer_bound(self, tmp_path):
        q = OfflineQueue(db_path=tmp_path / "q.db")
        q.bind_account("bob@corp.test", restored_session=False)
        assert q.enqueue(_future_events("alice", 2), account="alice@corp.test") == 0
        assert q.size() == 0


_SINCE = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)


def _ev(offset_s: float, duration=None, **extra) -> dict:
    ev = {"id": "e", "timestamp": (_SINCE + timedelta(seconds=offset_s)).isoformat()}
    if duration is not None:
        ev["duration"] = duration
    ev.update(extra)
    return ev


class TestClipToOwner:
    """The owner_since boundary decides whose billed time each second is."""

    clip = staticmethod(SyncEngine._clip_to_owner)

    def test_straddling_event_is_clipped_to_start_at_owner_since(self):
        kept, dropped = self.clip([_ev(-60, 300)], _SINCE)
        assert dropped == 0
        assert kept[0]["timestamp"] == _SINCE.isoformat()
        assert kept[0]["duration"] == 240.0

    def test_clipping_does_not_mutate_the_callers_event(self):
        ev = _ev(-60, 300)
        self.clip([ev], _SINCE)
        assert ev["duration"] == 300

    def test_event_ending_exactly_at_owner_since_is_dropped(self):
        assert self.clip([_ev(-60, 60)], _SINCE) == ([], 1)

    def test_event_wholly_before_owner_since_is_dropped(self):
        assert self.clip([_ev(-600, 60)], _SINCE) == ([], 1)

    def test_event_starting_at_or_after_owner_since_is_kept_untouched(self):
        at, after = _ev(0, 30), _ev(5, 30)
        assert self.clip([at, after], _SINCE) == ([at, after], 0)

    def test_pre_switch_event_without_a_duration_is_dropped(self):
        assert self.clip([_ev(-1)], _SINCE) == ([], 1)

    def test_pre_switch_event_with_a_bool_duration_is_dropped(self):
        assert self.clip([_ev(-60, True)], _SINCE) == ([], 1)

    def test_unparseable_or_missing_timestamp_is_dropped(self):
        events = [{"timestamp": "not-a-date", "duration": 10},
                  {"duration": 10}, {"timestamp": 1234, "duration": 10}]
        assert self.clip(events, _SINCE) == ([], 3)

    def test_naive_timestamp_is_read_as_utc(self):
        naive = (_SINCE - timedelta(seconds=10)).replace(tzinfo=None).isoformat()
        kept, dropped = self.clip([{"timestamp": naive, "duration": 20}], _SINCE)
        assert dropped == 0 and kept[0]["duration"] == 10.0

    def test_no_owner_since_passes_everything_through(self):
        events = [_ev(-600, 1), {"timestamp": "junk"}]
        assert self.clip(events, None) == (events, 0)


class TestRealClientPinsCredentials:
    def test_a_pinned_send_is_refused_before_the_wire_once_credentials_change(self):
        from src.sync.bf_client import BetterFlowClient

        client = BetterFlowClient(api_url="https://betterflow.test/api/agent")
        client._session = MagicMock()
        client.set_credentials("tok-alice", "42")
        pinned = client.credential_generation()
        client.set_credentials("tok-bob", "42")
        result = client.send_events(_future_events("alice", 1), expected_generation=pinned)
        assert result.success is False and result.transient is True
        assert result.error == REASON_CREDENTIALS_CHANGED
        client._session.request.assert_not_called()


def test_every_send_and_enqueue_goes_through_the_one_egress_rule():
    """Structure guard: the account rule holds only if nothing bypasses it.
    `.send_events(` may be called only inside SyncEngine._egress_send,
    `.enqueue(` only inside _egress_enqueue, and every `.dequeue(` /
    `.requeue_storable_dead_letter(` in src/ must name the snapshot account."""
    allowed = {"send_events": "_egress_send", "enqueue": "_egress_enqueue"}
    violations = []
    seen = {"send_events": 0, "enqueue": 0, "dequeue": 0,
            "requeue_storable_dead_letter": 0}
    for path in (REPO_ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                name = node.func.attr
                where = f"{path.relative_to(REPO_ROOT)}:{node.lineno} in {fn.name}"
                if name in allowed:
                    seen[name] += 1
                    if fn.name != allowed[name]:
                        violations.append(where)
                elif name in ("dequeue", "requeue_storable_dead_letter"):
                    seen[name] += 1
                    if not any(kw.arg == "account" for kw in node.keywords):
                        violations.append(where)
    # Positive control: the scan saw the one legitimate call of each kind.
    assert seen == {"send_events": 1, "enqueue": 1, "dequeue": 1,
                    "requeue_storable_dead_letter": 1}, seen
    assert violations == []
