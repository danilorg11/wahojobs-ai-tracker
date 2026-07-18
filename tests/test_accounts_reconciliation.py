import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tests.accounts_test_support import NOW, ROOT, create_user, install_accounts
from wahojobs import accounts
from wahojobs.account_reconciliation import reconcile_accounts


RECONCILE_SCRIPT = ROOT / "scripts" / "accounts_reconcile.py"


class AccountsReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "accounts.sqlite"
        self.conn = install_accounts(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_clean_database_and_zero_user_database_reconcile(self):
        empty = reconcile_accounts(self.conn, now=NOW)
        self.assertFalse(empty["blocking"])
        self.assertEqual(empty["counts"]["users"], 0)
        create_user(self.conn)
        populated = reconcile_accounts(self.conn, now=NOW)
        self.assertFalse(populated["blocking"])
        self.assertEqual(populated["counts"]["users_active"], 1)

    def test_schema_marker_and_append_only_trigger_drift_are_blocking(self):
        self.conn.execute("DELETE FROM wahojobs_schema_migrations WHERE version='002_accounts_sessions'")
        self.conn.execute("DROP TRIGGER trg_consent_events_no_delete")
        self.conn.commit()
        report = reconcile_accounts(self.conn, now=NOW)
        self.assertTrue(report["blocking"])
        self.assertTrue(report["checks"]["migration_marker_missing"])
        self.assertTrue(report["checks"]["required_objects_missing"])

    def make_rotation_table_permissive(self):
        self.conn.execute("DROP TABLE account_session_rotations")
        self.conn.execute(
            """
            CREATE TABLE account_session_rotations (
              rotation_id TEXT,
              user_id TEXT,
              predecessor_session_id TEXT,
              replacement_session_id TEXT,
              rotated_at TEXT,
              created_at TEXT
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX idx_account_session_rotations_user_time "
            "ON account_session_rotations(user_id, rotated_at)"
        )
        for name, operation in (
            ("trg_account_session_rotations_insert_guard", "INSERT"),
            ("trg_account_session_rotations_no_update", "UPDATE"),
            ("trg_account_session_rotations_no_delete", "DELETE"),
        ):
            self.conn.execute(
                f"CREATE TRIGGER {name} BEFORE {operation} ON account_session_rotations "
                "BEGIN SELECT 1; END"
            )

    def insert_drift_edge(self, suffix, user_id, predecessor_id, replacement_id, at=NOW):
        self.conn.execute(
            "INSERT INTO account_session_rotations VALUES (?,?,?,?,?,?)",
            (
                f"rot_drift_{suffix}",
                user_id,
                predecessor_id,
                replacement_id,
                at.isoformat(),
                at.isoformat(),
            ),
        )

    def test_reconciliation_detects_independent_rotation_defects(self):
        _, created = create_user(self.conn, "rotation-drift")
        _, other_user = create_user(self.conn, "rotation-drift-other")
        sessions = [
            accounts.create_session(
                self.conn,
                user_id=created.user.user_id,
                idle_ttl=timedelta(hours=1),
                absolute_ttl=timedelta(days=1),
                idempotency_key=f"rotation-drift-{index}",
                now=NOW,
            )
            for index in range(3)
        ]
        other = accounts.create_session(
            self.conn,
            user_id=other_user.user.user_id,
            idle_ttl=timedelta(hours=1),
            absolute_ttl=timedelta(days=1),
            idempotency_key="rotation-drift-other",
            now=NOW,
        )
        first, second, third = (item.session.session_id for item in sessions)
        self.make_rotation_table_permissive()
        self.insert_drift_edge("one", created.user.user_id, first, second)
        self.insert_drift_edge("fork", created.user.user_id, first, third)
        self.insert_drift_edge("reverse", created.user.user_id, third, second)
        self.insert_drift_edge("cycle", created.user.user_id, second, first)
        self.insert_drift_edge("self", created.user.user_id, third, third)
        self.insert_drift_edge(
            "cross", created.user.user_id, first, other.session.session_id
        )
        self.insert_drift_edge(
            "missing-predecessor",
            created.user.user_id,
            "ses_" + "f" * 32,
            second,
        )
        self.insert_drift_edge(
            "missing-replacement",
            created.user.user_id,
            first,
            "ses_" + "e" * 32,
        )
        self.insert_drift_edge(
            "temporal",
            created.user.user_id,
            second,
            third,
            NOW - timedelta(seconds=1),
        )
        self.conn.commit()

        report = reconcile_accounts(self.conn, now=NOW)
        for check in (
            "session_rotation_cross_user",
            "session_rotation_self_reference",
            "session_rotation_fork",
            "session_rotation_reverse_fork",
            "session_rotation_cycle",
            "session_rotation_missing_predecessor",
            "session_rotation_missing_replacement",
            "session_rotation_temporal_mismatch",
            "predecessor_not_revoked",
            "active_predecessor_with_active_replacement",
        ):
            with self.subTest(check=check):
                self.assertTrue(report["checks"][check])

    def test_reconciliation_reports_lineage_and_user_creation_time_defects_together(self):
        _, created = create_user(self.conn, "combined-drift")
        first = accounts.create_session(
            self.conn,
            user_id=created.user.user_id,
            idle_ttl=timedelta(hours=1),
            absolute_ttl=timedelta(days=1),
            idempotency_key="combined-drift-first",
            now=NOW,
        )
        second = accounts.create_session(
            self.conn,
            user_id=created.user.user_id,
            idle_ttl=timedelta(hours=1),
            absolute_ttl=timedelta(days=1),
            idempotency_key="combined-drift-second",
            now=NOW,
        )
        self.make_rotation_table_permissive()
        self.insert_drift_edge(
            "half-linked",
            created.user.user_id,
            first.session.session_id,
            second.session.session_id,
        )
        for trigger in (
            "trg_consent_events_user_time_guard",
            "trg_consent_events_contiguous",
            "trg_account_lifecycle_events_user_time_guard",
            "trg_account_lifecycle_events_contiguous",
            "trg_account_deletion_requests_user_time_guard",
            "trg_account_sessions_user_time_guard",
            "trg_account_sessions_core_immutable",
            "trg_account_invitations_consumption_time_guard",
        ):
            self.conn.execute(f"DROP TRIGGER {trigger}")
            table = (
                "consent_events" if "consent" in trigger
                else "account_lifecycle_events" if "lifecycle_events" in trigger
                else "account_deletion_requests" if "deletion_requests" in trigger
                else "account_invitations" if "invitations" in trigger
                else "account_sessions"
            )
            operation = "INSERT" if "immutable" not in trigger else "UPDATE"
            self.conn.execute(
                f"CREATE TRIGGER {trigger} BEFORE {operation} ON {table} BEGIN SELECT 1; END"
            )
        self.conn.execute("PRAGMA ignore_check_constraints = ON")
        before = (NOW - timedelta(days=1)).isoformat()
        self.conn.execute(
            """
            INSERT INTO consent_events (
              consent_event_id,user_id,purpose,policy_version,action,occurred_at,
              source,consent_version_before,consent_version_after,metadata_json,
              idempotency_key,request_fingerprint
            ) VALUES ('cns_before_user',?,'profile_storage','v1','granted',?,'audit',0,1,'{}',?,?)
            """,
            (created.user.user_id, before, "consent-before-user", "a" * 64),
        )
        self.conn.execute(
            """
            INSERT INTO account_lifecycle_events (
              lifecycle_event_id,user_id,event_type,occurred_at,source,
              account_version_before,account_version_after,metadata_json,
              idempotency_key,request_fingerprint
            ) VALUES ('life_before_user',?,'account_suspended',?,'audit',1,2,'{}',?,?)
            """,
            (created.user.user_id, before, "lifecycle-before-user", "b" * 64),
        )
        self.conn.execute(
            "UPDATE users SET updated_at=? WHERE user_id=?",
            (before, created.user.user_id),
        )
        self.conn.execute(
            "UPDATE account_sessions SET created_at=?, last_seen_at=? WHERE session_id=?",
            (before, before, first.session.session_id),
        )
        self.conn.execute(
            "UPDATE account_invitations SET consumed_at=? WHERE consumed_by_user_id=?",
            (before, created.user.user_id),
        )
        self.conn.execute(
            """
            INSERT INTO account_deletion_requests (
              deletion_request_id,user_id,requested_at,cooling_period_ends_at,
              purge_eligible_at,status,request_source,restore_lifecycle_status,
              deactivation_evidence_json,idempotency_key,request_fingerprint
            ) VALUES ('del_before_user',?,?,?,?,'pending_cooling','audit','active','{}',?,?)
            """,
            (
                created.user.user_id,
                before,
                NOW.isoformat(),
                (NOW + timedelta(days=1)).isoformat(),
                "deletion-before-user",
                "c" * 64,
            ),
        )
        self.conn.commit()

        report = reconcile_accounts(self.conn, now=NOW)
        for check in (
            "predecessor_not_revoked",
            "active_predecessor_with_active_replacement",
            "lifecycle_event_predates_user",
            "consent_event_predates_user",
            "deletion_request_predates_user",
            "session_predates_user",
            "invitation_consumption_predates_user",
            "lifecycle_projection_predates_user",
        ):
            with self.subTest(check=check):
                self.assertTrue(report["checks"][check])

        self.conn.close()
        before_bytes = self.db_path.read_bytes()
        before_stat = self.db_path.stat()
        json_result = subprocess.run(
            [sys.executable, "-B", str(RECONCILE_SCRIPT), "--db", str(self.db_path), "--json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        human_result = subprocess.run(
            [sys.executable, "-B", str(RECONCILE_SCRIPT), "--db", str(self.db_path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(json_result.returncode, 0)
        self.assertNotEqual(human_result.returncode, 0)
        cli_report = json.loads(json_result.stdout)
        for check in (
            "predecessor_not_revoked",
            "lifecycle_event_predates_user",
            "consent_event_predates_user",
            "invitation_consumption_predates_user",
        ):
            self.assertEqual(cli_report["checks"][check], report["checks"][check])
            label = check.replace("_", " ").title()
            self.assertIn(f"{label}: {len(report['checks'][check])}", human_result.stdout)
        self.assertEqual(self.db_path.read_bytes(), before_bytes)
        self.assertEqual(self.db_path.stat().st_mtime_ns, before_stat.st_mtime_ns)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def test_orphan_detection_and_foreign_key_check(self):
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute(
            """
            INSERT INTO auth_identities (
              auth_identity_id,user_id,provider,provider_subject,verified_email,
              email_verified,created_at,last_authenticated_at,link_idempotency_key,
              request_fingerprint
            ) VALUES ('auth_orphan','usr_00000000000000000000000000000000','google',
                      'orphan-subject',NULL,0,?,?, 'orphan-link',?)
            """,
            (NOW.isoformat(), NOW.isoformat(), "a" * 64),
        )
        self.conn.commit()
        report = reconcile_accounts(self.conn, now=NOW)
        self.assertTrue(report["checks"]["orphan_auth_identities"])
        self.assertTrue(report["checks"]["foreign_key_violations"])

    def test_duplicate_provider_subject_and_projection_mismatch_are_detected(self):
        _, created = create_user(self.conn)
        original = self.conn.execute("SELECT * FROM auth_identities").fetchone()
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("ALTER TABLE auth_identities RENAME TO auth_identities_original")
        self.conn.execute(
            """
            CREATE TABLE auth_identities (
              auth_identity_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
              provider TEXT NOT NULL, provider_subject TEXT NOT NULL,
              verified_email TEXT, email_verified INTEGER NOT NULL,
              created_at TEXT NOT NULL, last_authenticated_at TEXT NOT NULL,
              disabled_at TEXT, link_idempotency_key TEXT NOT NULL,
              request_fingerprint TEXT NOT NULL
            )
            """
        )
        values = tuple(original[key] for key in original.keys())
        placeholders = ",".join("?" for _ in values)
        columns = ",".join(original.keys())
        self.conn.execute(
            f"INSERT INTO auth_identities ({columns}) VALUES ({placeholders})", values
        )
        duplicate = list(values)
        duplicate[0] = "auth_duplicate"
        duplicate[9] = "duplicate-link-key"
        self.conn.execute(
            f"INSERT INTO auth_identities ({columns}) VALUES ({placeholders})", tuple(duplicate)
        )
        self.conn.execute(
            "UPDATE users SET lifecycle_status='suspended' WHERE user_id=?",
            (created.user.user_id,),
        )
        self.conn.commit()
        report = reconcile_accounts(self.conn, now=NOW)
        self.assertTrue(report["checks"]["duplicate_provider_subjects"])
        self.assertTrue(report["checks"]["lifecycle_projection_mismatch"])

    def test_human_and_json_cli_are_read_only_and_equivalent(self):
        create_user(self.conn)
        self.conn.close()
        before_bytes = self.db_path.read_bytes()
        before_hash = hashlib.sha256(before_bytes).hexdigest()
        json_result = subprocess.run(
            [sys.executable, "-B", str(RECONCILE_SCRIPT), "--db", str(self.db_path), "--json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        human_result = subprocess.run(
            [sys.executable, "-B", str(RECONCILE_SCRIPT), "--db", str(self.db_path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        report = json.loads(json_result.stdout)
        self.assertFalse(report["blocking"])
        self.assertIn("Users Active: 1", human_result.stdout)
        self.assertIn("Result: clean", human_result.stdout)
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), before_hash)
        self.assertEqual(self.db_path.read_bytes(), before_bytes)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row


if __name__ == "__main__":
    unittest.main()
