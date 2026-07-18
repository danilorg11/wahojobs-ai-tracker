import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tests.accounts_test_support import (
    INVITATION_KEY,
    NOW,
    ROOT,
    connect,
    create_user,
    install_accounts,
)
from wahojobs import accounts


WORKER = r"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

from tests.accounts_test_support import (
    ACCOUNT_SERVICE, INVITATION_KEY, connect, trusted_identity
)
from wahojobs import accounts

payload = json.loads(sys.stdin.read())
conn = connect(payload["db"])
now = datetime.fromisoformat(payload["now"])
try:
    op = payload["op"]
    if op == "consume":
        result = ACCOUNT_SERVICE.create_invited_user(
            conn,
            identity=trusted_identity(payload["subject"], payload["email"], authenticated_at=now),
            invitation_token=payload["invitation_token"],
            invitation_lookup_key=INVITATION_KEY,
            idempotency_key=payload["idempotency_key"],
            now=now,
        )
        output = {"ok": True, "id": result.user.user_id}
    elif op == "rotate":
        result = accounts.rotate_session(
            conn,
            session_token=payload["session_token"],
            expected_session_version=1,
            idle_ttl=timedelta(hours=1),
            idempotency_key=payload["idempotency_key"],
            now=now + timedelta(minutes=1),
        )
        output = {"ok": True, "id": result.session.session_id}
    elif op == "revoke_all":
        count = accounts.revoke_all_sessions(
            conn,
            user_id=payload["user_id"],
            expected_user_version=payload.get("expected_version", 1),
            reason="security_reset",
            now=now + timedelta(minutes=1),
        )
        output = {"ok": True, "count": count}
    elif op == "validate_session":
        result = accounts.resolve_session(
            conn, session_token=payload["session_token"], now=now
        )
        output = {"ok": True, "id": result.session_id}
    elif op == "validate_csrf":
        result = accounts.validate_session_csrf(
            conn,
            session_token=payload["session_token"],
            csrf_secret=payload["csrf_secret"],
            now=now,
        )
        output = {"ok": True, "id": result.session_id}
    elif op == "create_session":
        result = accounts.create_session(
            conn,
            user_id=payload["user_id"],
            idle_ttl=timedelta(hours=1),
            absolute_ttl=timedelta(days=1),
            idempotency_key=payload["idempotency_key"],
            now=now,
        )
        output = {"ok": True, "id": result.session.session_id}
    elif op == "create_duplicate_csrf":
        values = iter((payload["raw_token"], payload["shared_csrf"]))
        accounts.secrets.token_urlsafe = lambda _size: next(values)
        result = accounts.create_session(
            conn,
            user_id=payload["user_id"],
            idle_ttl=timedelta(hours=1),
            absolute_ttl=timedelta(days=1),
            idempotency_key=payload["idempotency_key"],
            now=now,
        )
        output = {"ok": True, "id": result.session.session_id}
    elif op == "request_deletion":
        result = accounts.request_account_deletion(
            conn,
            user_id=payload["user_id"],
            expected_version=1,
            cooling_period=timedelta(days=7),
            purge_after=timedelta(days=30),
            request_source="account_settings",
            idempotency_key=payload["idempotency_key"],
            now=now,
        )
        output = {"ok": True, "id": result.request.deletion_request_id}
    elif op == "crash_invitation":
        def crash(point):
            if point == "after_invitation_insert":
                os._exit(29)
        accounts.create_invitation(
            conn,
            email="crash@example.test",
            lookup_key=INVITATION_KEY,
            expires_at=now + timedelta(days=1),
            created_by="test_admin",
            idempotency_key="crash-invitation-key",
            now=now,
            failure_injector=crash,
        )
        output = {"ok": False, "error": "crash_not_triggered"}
    else:
        raise RuntimeError("unsupported worker operation")
except Exception as exc:
    output = {"ok": False, "error_type": type(exc).__name__, "message": str(exc)}
finally:
    conn.close()
if "output" in locals():
    print(json.dumps(output, sort_keys=True))
"""


class AccountSubprocessConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "accounts.sqlite"
        self.conn = install_accounts(self.db_path)

    def tearDown(self):
        if self.conn is not None:
            self.conn.close()
        self.temp_dir.cleanup()

    def payload(self, op, **values):
        return {
            "op": op,
            "db": str(self.db_path),
            "now": NOW.isoformat(),
            **values,
        }

    def launch(self, payload):
        return subprocess.Popen(
            [sys.executable, "-B", "-c", WORKER],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ), json.dumps(payload)

    def run_pair(self, first, second):
        first_process, first_input = self.launch(first)
        second_process, second_input = self.launch(second)
        first_stdout, first_stderr = first_process.communicate(first_input, timeout=15)
        second_stdout, second_stderr = second_process.communicate(second_input, timeout=15)
        self.assertEqual(first_process.returncode, 0, first_stderr)
        self.assertEqual(second_process.returncode, 0, second_stderr)
        return json.loads(first_stdout), json.loads(second_stdout)

    def close_parent(self):
        self.conn.close()
        self.conn = None

    def reopen(self):
        self.conn = connect(self.db_path)

    def assert_healthy(self):
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_two_simultaneous_invitation_consumers(self):
        creation = accounts.create_invitation(
            self.conn,
            email="race@example.test",
            lookup_key=INVITATION_KEY,
            expires_at=NOW + timedelta(days=1),
            created_by="test_admin",
            idempotency_key="subprocess-invitation-create",
            now=NOW,
        )
        self.close_parent()
        base = {
            "email": "race@example.test",
            "subject": "subprocess-race-subject",
            "invitation_token": creation.invitation_token,
        }
        results = self.run_pair(
            self.payload("consume", idempotency_key="subprocess-consume-one", **base),
            self.payload("consume", idempotency_key="subprocess-consume-two", **base),
        )
        self.reopen()
        self.assertEqual(sum(result["ok"] for result in results), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM account_invitations WHERE invitation_status='consumed'"
            ).fetchone()[0],
            1,
        )
        self.assert_healthy()

    def test_two_simultaneous_rotations_leave_one_lineage(self):
        _, created = create_user(self.conn, "subprocess-rotate")
        session = accounts.create_session(
            self.conn,
            user_id=created.user.user_id,
            idle_ttl=timedelta(hours=1),
            absolute_ttl=timedelta(days=1),
            idempotency_key="subprocess-session-create",
            now=NOW,
        )
        self.close_parent()
        results = self.run_pair(
            self.payload(
                "rotate",
                session_token=session.session_token,
                idempotency_key="subprocess-rotate-one",
            ),
            self.payload(
                "rotate",
                session_token=session.session_token,
                idempotency_key="subprocess-rotate-two",
            ),
        )
        self.reopen()
        self.assertEqual(sum(result["ok"] for result in results), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0], 2)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM account_session_rotations WHERE predecessor_session_id=?",
                (session.session.session_id,),
            ).fetchone()[0],
            1,
        )
        self.assert_healthy()

    def test_revoke_all_racing_rotation_leaves_no_active_session(self):
        _, created = create_user(self.conn, "subprocess-revoke-rotate")
        session = accounts.create_session(
            self.conn,
            user_id=created.user.user_id,
            idle_ttl=timedelta(hours=1),
            absolute_ttl=timedelta(days=1),
            idempotency_key="subprocess-revoke-rotate-create",
            now=NOW,
        )
        self.close_parent()
        self.run_pair(
            self.payload(
                "rotate",
                session_token=session.session_token,
                idempotency_key="subprocess-revoke-rotate-child",
            ),
            self.payload("revoke_all", user_id=created.user.user_id),
        )
        self.reopen()
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM account_sessions WHERE user_id=? AND revoked_at IS NULL",
                (created.user.user_id,),
            ).fetchone()[0],
            0,
        )
        self.assert_healthy()

    def test_deletion_races_validation_and_session_creation(self):
        for suffix, competing_op in (("validation", "validate_session"), ("creation", "create_session")):
            with self.subTest(competing_op=competing_op):
                if self.conn is None:
                    self.reopen()
                _, created = create_user(self.conn, f"subprocess-delete-{suffix}")
                session = accounts.create_session(
                    self.conn,
                    user_id=created.user.user_id,
                    idle_ttl=timedelta(hours=1),
                    absolute_ttl=timedelta(days=1),
                    idempotency_key=f"subprocess-delete-session-{suffix}",
                    now=NOW,
                )
                self.close_parent()
                competing = (
                    self.payload("validate_session", session_token=session.session_token)
                    if competing_op == "validate_session"
                    else self.payload(
                        "create_session",
                        user_id=created.user.user_id,
                        idempotency_key=f"subprocess-delete-new-{suffix}",
                    )
                )
                self.run_pair(
                    self.payload(
                        "request_deletion",
                        user_id=created.user.user_id,
                        idempotency_key=f"subprocess-delete-request-{suffix}",
                    ),
                    competing,
                )
                self.reopen()
                self.assertEqual(
                    self.conn.execute(
                        "SELECT lifecycle_status FROM users WHERE user_id=?",
                        (created.user.user_id,),
                    ).fetchone()[0],
                    "deletion_requested",
                )
                self.assertEqual(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM account_sessions WHERE user_id=? AND revoked_at IS NULL",
                        (created.user.user_id,),
                    ).fetchone()[0],
                    0,
                )
                self.assert_healthy()
                self.close_parent()
        self.reopen()

    def test_two_deletion_requests_return_one_request(self):
        _, created = create_user(self.conn, "subprocess-delete-pair")
        self.close_parent()
        results = self.run_pair(
            self.payload(
                "request_deletion",
                user_id=created.user.user_id,
                idempotency_key="subprocess-delete-pair-one",
            ),
            self.payload(
                "request_deletion",
                user_id=created.user.user_id,
                idempotency_key="subprocess-delete-pair-two",
            ),
        )
        self.reopen()
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(results[0]["id"], results[1]["id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM account_deletion_requests").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM account_lifecycle_events WHERE event_type='deletion_requested'"
            ).fetchone()[0],
            1,
        )
        self.assert_healthy()

    def test_csrf_validation_racing_rotation_and_duplicate_csrf_creation(self):
        _, created = create_user(self.conn, "subprocess-csrf")
        session = accounts.create_session(
            self.conn,
            user_id=created.user.user_id,
            idle_ttl=timedelta(hours=1),
            absolute_ttl=timedelta(days=1),
            idempotency_key="subprocess-csrf-session",
            now=NOW,
        )
        self.close_parent()
        self.run_pair(
            self.payload(
                "validate_csrf",
                session_token=session.session_token,
                csrf_secret=session.csrf_secret,
            ),
            self.payload(
                "rotate",
                session_token=session.session_token,
                idempotency_key="subprocess-csrf-rotate",
            ),
        )
        self.reopen()
        with self.assertRaises(accounts.SessionUnavailable):
            accounts.validate_session_csrf(
                self.conn,
                session_token=session.session_token,
                csrf_secret=session.csrf_secret,
                now=NOW + timedelta(minutes=2),
            )

        _, second_user = create_user(self.conn, "subprocess-duplicate-csrf")
        self.close_parent()
        shared = "c" * 43
        results = self.run_pair(
            self.payload(
                "create_duplicate_csrf",
                user_id=second_user.user.user_id,
                idempotency_key="subprocess-duplicate-csrf-one",
                raw_token="a" * 43,
                shared_csrf=shared,
            ),
            self.payload(
                "create_duplicate_csrf",
                user_id=second_user.user.user_id,
                idempotency_key="subprocess-duplicate-csrf-two",
                raw_token="b" * 43,
                shared_csrf=shared,
            ),
        )
        self.reopen()
        self.assertEqual(sum(result["ok"] for result in results), 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM account_sessions WHERE csrf_secret_hash=?",
                (accounts._secret_hash(shared),),
            ).fetchone()[0],
            1,
        )
        self.assert_healthy()

    def test_locked_database_and_process_crash_leave_no_partial_rows(self):
        _, created = create_user(self.conn, "subprocess-lock")
        self.conn.execute("BEGIN IMMEDIATE")
        process, worker_input = self.launch(
            self.payload(
                "create_session",
                user_id=created.user.user_id,
                idempotency_key="subprocess-locked-session",
            )
        )
        stdout, stderr = process.communicate(worker_input, timeout=15)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertFalse(json.loads(stdout)["ok"])
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0], 0)

        self.close_parent()
        process, worker_input = self.launch(self.payload("crash_invitation"))
        process.communicate(worker_input, timeout=15)
        self.assertEqual(process.returncode, 29)
        self.reopen()
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM account_invitations WHERE idempotency_key='crash-invitation-key'"
            ).fetchone()[0],
            0,
        )
        self.assert_healthy()


if __name__ == "__main__":
    unittest.main()
