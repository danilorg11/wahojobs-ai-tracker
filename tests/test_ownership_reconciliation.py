import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.accounts_test_support import install_accounts
from tests.ownership_test_support import (
    NOW_TEXT,
    add_activation_event,
    add_active_user,
    add_alias,
    add_binding,
    add_principal,
    install_ownership,
)
from wahojobs.ownership import MIGRATION_VERSION, event_request_fingerprint
from wahojobs.ownership_reconciliation import reconcile_ownership


class OwnershipReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ownership.sqlite"
        self.conn = install_ownership(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_clean_dormant_schema_is_nonblocking(self):
        report = reconcile_ownership(self.conn)
        self.assertFalse(report["blocking"])
        self.assertTrue(report["read_only"])
        self.assertEqual(report["counts"]["principals"], 0)

    def test_clean_populated_projection_and_event_chain(self):
        user_id = add_active_user(self.conn)
        principal_id = add_principal(self.conn)
        binding_id = add_binding(self.conn, principal_id, user_id)
        add_activation_event(self.conn, principal_id, user_id, binding_id)
        report = reconcile_ownership(self.conn)
        self.assertFalse(report["blocking"], report["blocking_reasons"])

    def test_missing_append_only_trigger_is_blocking(self):
        user_id = add_active_user(self.conn)
        principal_id = add_principal(self.conn)
        binding_id = add_binding(self.conn, principal_id, user_id)
        add_activation_event(self.conn, principal_id, user_id, binding_id)
        self.conn.execute("DROP TRIGGER trg_ownership_binding_events_no_delete")
        report = reconcile_ownership(self.conn)
        self.assertIn("required_objects_missing", report["blocking_reasons"])

    def test_missing_event_history_is_projection_drift(self):
        user_id = add_active_user(self.conn)
        principal_id = add_principal(self.conn)
        add_binding(self.conn, principal_id, user_id)
        report = reconcile_ownership(self.conn)
        self.assertIn("ownership_binding_projection_mismatch", report["blocking_reasons"])

    def test_sensitive_metadata_and_inactive_account_are_detected(self):
        user_id = add_active_user(self.conn)
        principal_id = add_principal(self.conn)
        binding_id = add_binding(self.conn, principal_id, user_id)
        add_activation_event(self.conn, principal_id, user_id, binding_id)
        sensitive = '{"raw_application_content":"private material"}'
        self.conn.execute("DROP TRIGGER trg_product_principals_update_guard")
        self.conn.execute(
            "UPDATE product_principals SET provenance_json=?, "
            "version=version+1, updated_at=? WHERE principal_id=?",
            (sensitive, NOW_TEXT, principal_id),
        )
        self.conn.execute(
            "UPDATE users SET lifecycle_status='suspended', row_version=row_version+1, updated_at=? WHERE user_id=?",
            (NOW_TEXT, user_id),
        )
        report = reconcile_ownership(self.conn)
        self.assertTrue(report["checks"]["malformed_principal_provenance"])
        self.assertTrue(report["checks"]["bindings_to_unavailable_accounts"])
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn("private material", serialized)
        self.assertNotIn(user_id, serialized)

    def test_cli_style_reconciliation_does_not_change_file(self):
        self.conn.commit()
        self.conn.close()
        before = (self.path.stat().st_size, self.path.stat().st_mtime_ns, self.path.read_bytes())
        ro = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        try:
            first = reconcile_ownership(ro)
            second = reconcile_ownership(ro)
        finally:
            ro.close()
        after = (self.path.stat().st_size, self.path.stat().st_mtime_ns, self.path.read_bytes())
        self.assertEqual(first, second)
        self.assertEqual(after, before)
        self.conn = sqlite3.connect(self.path)

    def test_schema_drift_does_not_suppress_independent_row_findings(self):
        user_id = add_active_user(self.conn)
        first = add_principal(self.conn, suffix="70")
        second = add_principal(self.conn, suffix="71")
        binding_id = add_binding(self.conn, first, user_id, suffix="70")
        add_activation_event(self.conn, first, user_id, binding_id, suffix="70")
        add_alias(
            self.conn,
            first,
            suffix="70",
            kind="profile_id",
            value="split-owner",
        )

        self.conn.execute("DROP TRIGGER trg_legacy_owner_aliases_insert_guard")
        self.conn.execute("DROP TRIGGER trg_legacy_owner_aliases_no_update")
        self.conn.execute("DROP TRIGGER trg_product_principals_update_guard")
        self.conn.execute("DROP TRIGGER trg_principal_account_bindings_update_guard")
        add_alias(
            self.conn,
            second,
            suffix="71",
            kind="pipeline_owner",
            value="split-owner",
        )
        add_alias(
            self.conn,
            first,
            suffix="72",
            kind="legacy_user_id",
            value="malformed\nowner",
        )
        self.conn.execute(
            "UPDATE product_principals SET provenance_json=?, version=2, updated_at=? "
            "WHERE principal_id=?",
            ('{"nested":{"sql_query":"private"}}', NOW_TEXT, first),
        )
        self.conn.execute(
            "UPDATE principal_account_bindings SET binding_status='suspended', version=2, "
            "latest_event_version=2, suspended_at=?, updated_at=? WHERE binding_id=?",
            (NOW_TEXT, NOW_TEXT, binding_id),
        )
        self.conn.execute(
            "UPDATE users SET lifecycle_status='suspended', row_version=row_version+1, "
            "updated_at=? WHERE user_id=?",
            (NOW_TEXT, user_id),
        )

        report = reconcile_ownership(self.conn)
        required = {
            "schema_attestation_failures",
            "append_only_protection_missing",
            "malformed_aliases",
            "malformed_principal_provenance",
            "bindings_to_unavailable_accounts",
            "ownership_binding_projection_mismatch",
            "legacy_alias_principal_split",
        }
        self.assertTrue(required <= set(report["blocking_reasons"]), report["blocking_reasons"])
        self.assertEqual(len(report["checks"]["legacy_alias_principal_split"]), 1)
        serialized = json.dumps(report, sort_keys=True)
        for private in ("split-owner", "malformed\nowner", "private", user_id):
            self.assertNotIn(private, serialized)

    def test_reconciliation_reports_principal_scoped_raw_idempotency_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "duplicate-idempotency.sqlite"
            conn = install_accounts(path)
            migration_path = (
                Path(__file__).resolve().parents[1]
                / "wahojobs"
                / "db"
                / "migrations"
                / f"{MIGRATION_VERSION}.sql"
            )
            sql = migration_path.read_text(encoding="utf-8").replace(
                "  UNIQUE (principal_id, idempotency_key),\n", "", 1
            )
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO wahojobs_schema_migrations(version) VALUES (?)",
                (MIGRATION_VERSION,),
            )
            user_one = add_active_user(conn, "duplicate-one")
            user_two = add_active_user(conn, "duplicate-two")
            principal_id = add_principal(conn, suffix="80", exclusive=0)
            binding_one = add_binding(conn, principal_id, user_one, suffix="80")
            binding_two = add_binding(conn, principal_id, user_two, suffix="81")
            key = "raw-duplicate-idempotency-80"
            self._raw_activation(conn, "80", principal_id, user_one, binding_one, key)
            self._raw_activation(conn, "81", principal_id, user_two, binding_two, key)
            report = reconcile_ownership(conn)
            self.assertIn(
                "ownership_event_idempotency_conflict", report["blocking_reasons"]
            )
            self.assertEqual(
                len(report["checks"]["ownership_event_idempotency_conflict"]), 1
            )
            self.assertNotIn(key, json.dumps(report, sort_keys=True))
            conn.close()

    def _raw_activation(self, conn, suffix, principal_id, user_id, binding_id, key):
        fingerprint = event_request_fingerprint(
            principal_id=principal_id,
            binding_id=binding_id,
            user_id=user_id,
            expected_event_version=1,
            event_type="binding_activated",
            prior_status=None,
            resulting_status="active",
            actor_type="administrator",
            reason_code="manual_approval",
            approval_reference=None,
            occurred_at=NOW_TEXT,
            metadata={},
        )
        conn.execute(
            "INSERT INTO ownership_binding_events "
            "(event_id, principal_id, user_id, binding_id, environment_namespace, event_version, "
            "event_type, prior_status, resulting_status, actor_type, reason_code, approval_reference, "
            "idempotency_key, request_fingerprint, occurred_at, metadata_json) "
            "VALUES (?, ?, ?, ?, 'test', 1, 'binding_activated', NULL, 'active', "
            "'administrator', 'manual_approval', NULL, ?, ?, ?, '{}')",
            (
                f"obe_{int(suffix):032x}",
                principal_id,
                user_id,
                binding_id,
                key,
                fingerprint,
                NOW_TEXT,
            ),
        )


if __name__ == "__main__":
    unittest.main()
