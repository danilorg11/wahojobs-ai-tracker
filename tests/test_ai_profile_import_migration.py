from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.ai_profile_import_migration import (
    AIProfileImportMigrationError,
    apply_ai_profile_import_migration,
)
from scripts.public_job_identity_migration import apply_public_job_identity_migration
from tests.persistent_profiles_repository_test_support import (
    create_command,
    development_context,
)
from tests.persistent_profile_read_authorization_test_support import (
    seed_authorized_account,
)
from tests.workos_authkit_test_support import build_m008
from wahojobs.ai_profile_import_schema import (
    EXPECTED_MIGRATION_VERSIONS,
    EXPECTED_SCHEMA_FINGERPRINT,
    EXPECTED_SCHEMA_OBJECT_COUNT,
    attest_ai_profile_import_schema,
    migration_statement_count,
)
from wahojobs.closed_schema_authority import current_closed_schema_is_exact
from wahojobs.persistent_profiles_repository import PersistentProfileRepository
from wahojobs.persistent_profiles_repository import capture_profile_create_lineage


class AIProfileImportMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="wahojobs-m010-test-", ignore_cleanup_errors=True
        )
        self.path = Path(self.directory.name) / "database.sqlite3"
        self.connection = build_m008(self.path)
        apply_public_job_identity_migration(self.connection)

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def test_exact_m009_upgrades_to_attested_empty_m010_and_reapplies_as_noop(self):
        self.assertEqual(
            attest_ai_profile_import_schema(self.connection)["state"],
            "ai_profile_import_pending",
        )
        result = apply_ai_profile_import_migration(self.connection)
        self.assertTrue(result["applied"])
        report = attest_ai_profile_import_schema(self.connection)
        self.assertEqual(report["state"], "correctly_installed")
        self.assertEqual(report["actual_schema_object_count"], EXPECTED_SCHEMA_OBJECT_COUNT)
        self.assertEqual(report["actual_schema_fingerprint"], EXPECTED_SCHEMA_FINGERPRINT)
        self.assertEqual(report["present_migration_versions"], list(EXPECTED_MIGRATION_VERSIONS))
        self.assertGreater(migration_statement_count(), 10)
        self.assertTrue(current_closed_schema_is_exact(self.connection))
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertFalse(apply_ai_profile_import_migration(self.connection)["applied"])

    def test_failure_injection_rolls_back_schema_data_and_marker(self):
        for point in ("after_statement_4", "after_marker", "before_commit"):
            with self.subTest(point=point):
                def fail(boundary):
                    if boundary == point:
                        raise RuntimeError("synthetic")

                with self.assertRaises(AIProfileImportMigrationError):
                    apply_ai_profile_import_migration(
                        self.connection,
                        failure_injector=fail,
                    )
                self.assertEqual(
                    attest_ai_profile_import_schema(self.connection)["state"],
                    "ai_profile_import_pending",
                )
                self.assertEqual(
                    self.connection.execute(
                        "SELECT COUNT(*) FROM wahojobs_schema_migrations WHERE version='010_ai_profile_import'"
                    ).fetchone()[0],
                    0,
                )
                source_sql = self.connection.execute(
                    "SELECT sql FROM sqlite_schema WHERE type='table' AND name='product_profile_sources'"
                ).fetchone()[0]
                self.assertNotIn("user_confirmed_ai_import", source_sql)

    def test_existing_manual_profile_and_source_are_preserved(self):
        principal = development_context(self.connection, "97")
        created = PersistentProfileRepository().create(
            self.connection,
            create_command(principal, idempotency_key="manual-before-m010-0001"),
        )
        before = tuple(
            self.connection.execute(
                "SELECT source_id,revision_id,profile_id,source_type,source_content_sha256 "
                "FROM product_profile_sources"
            ).fetchall()
        )
        apply_ai_profile_import_migration(self.connection)
        self.assertEqual(
            tuple(
                self.connection.execute(
                    "SELECT source_id,revision_id,profile_id,source_type,source_content_sha256 "
                    "FROM product_profile_sources"
                ).fetchall()
            ),
            before,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT profile_id FROM product_profiles"
            ).fetchone()[0],
            created.profile_id,
        )
        later = development_context(self.connection, "98")
        PersistentProfileRepository().create(
            self.connection,
            create_command(later, idempotency_key="manual-after-m010-0001"),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM ai_profile_import_entitlements"
            ).fetchone()[0],
            0,
        )

    def test_attestation_rejects_a_tampered_installed_m010(self):
        apply_ai_profile_import_migration(self.connection)
        self.connection.execute(
            "DROP TRIGGER trg_ai_profile_import_attempts_no_delete"
        )
        report = attest_ai_profile_import_schema(self.connection)
        self.assertEqual(report["state"], "partial_inconsistent")
        self.assertTrue(report["blocking"])
        self.assertFalse(report["applicable"])
        self.assertFalse(current_closed_schema_is_exact(self.connection))

    def test_constraints_reject_duplicate_entitlement_invalid_states_and_unsafe_ai_source(self):
        apply_ai_profile_import_migration(self.connection)
        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
        self.assertIn("ai_profile_import_entitlements", tables)
        self.assertIn("ai_profile_import_attempts", tables)
        entitlement_sql = self.connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='ai_profile_import_entitlements'"
        ).fetchone()[0]
        attempt_sql = self.connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='ai_profile_import_attempts'"
        ).fetchone()[0]
        self.assertIn("PRIMARY KEY (environment_namespace, account_id, entitlement_code)", entitlement_sql)
        self.assertIn("'available', 'reserved', 'consumed'", entitlement_sql)
        self.assertIn("'reserved', 'released', 'expired', 'failed', 'succeeded'", attempt_sql)

        principal = development_context(self.connection, "99")
        command = create_command(principal, idempotency_key="source-constraint-m010-0001")
        # Direct SQL cannot smuggle arbitrary AI provenance through the widened vocabulary.
        self.connection.execute("BEGIN")
        self.connection.execute(
            "INSERT INTO product_profiles(profile_id,principal_id,environment_namespace,initial_revision_id,created_at) "
            "VALUES (?,?,?,?,?)",
            (command.profile_id, principal.principal_id, "test", command.revision_id, command.accepted_at),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO product_profile_sources(source_id,revision_id,profile_id,principal_id,environment_namespace,"
                "source_ordinal,source_type,source_format,source_content,source_content_sha256,source_schema_version,parser_version,accepted_at) "
                "VALUES (?,?,?,?,?,1,'user_confirmed_ai_import','application/json',?,?,'user_confirmed_ai_import_v1',NULL,?)",
                (
                    command.source_ids[0], command.revision_id, command.profile_id,
                    principal.principal_id, "test", '{"resume_text":"secret"}', "a" * 64,
                    command.accepted_at,
                ),
            )
        self.connection.rollback()

    def test_entitlement_keys_states_foreign_keys_and_attempt_metadata_are_enforced(self):
        apply_ai_profile_import_migration(self.connection)
        state = seed_authorized_account(self.connection, suffix="100")
        timestamp = "2026-07-20T13:05:00+00:00"
        entitlement = (
            state["environment"],
            state["account_id"],
            "ai_profile_import_v1",
            timestamp,
            timestamp,
        )
        insert_entitlement = (
            "INSERT INTO ai_profile_import_entitlements "
            "(environment_namespace,account_id,entitlement_code,state,reservation_id,attempt_id,"
            "lease_expires_at,consumed_at,created_at,updated_at) "
            "VALUES (?,?,?,'available',NULL,NULL,NULL,NULL,?,?)"
        )
        self.connection.execute(insert_entitlement, entitlement)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(insert_entitlement, entitlement)
        self.connection.rollback()

        self.connection.execute(insert_entitlement, entitlement)
        second = seed_authorized_account(self.connection, suffix="101")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO ai_profile_import_entitlements "
                "(environment_namespace,account_id,entitlement_code,state,reservation_id,attempt_id,"
                "lease_expires_at,consumed_at,created_at,updated_at) "
                "VALUES (?,?,?,'granted',NULL,NULL,NULL,NULL,?,?)",
                (
                    second["environment"],
                    second["account_id"],
                    "ai_profile_import_v1",
                    timestamp,
                    timestamp,
                ),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                insert_entitlement,
                ("private_beta", "usr_ffffffffffffffffffffffffffffffff", "ai_profile_import_v1", timestamp, timestamp),
            )

        lineage = capture_profile_create_lineage(
            self.connection,
            account_id=state["account_id"],
            environment_namespace=state["environment"],
            principal_id=state["principal_id"],
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO ai_profile_import_attempts "
                "(attempt_id,reservation_id,environment_namespace,account_id,principal_id,entitlement_code,state,"
                "idempotency_key_sha256,request_fingerprint,confirmation_fingerprint,source_metadata_json,"
                "binding_id,binding_version,latest_event_version,latest_event_id,lineage_sha256,lease_expires_at,"
                "result_code,result_profile_id,result_revision_id,created_at,updated_at,completed_at) "
                "VALUES (?,?,?,?,?,?,'reserved',?,?,NULL,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,NULL)",
                (
                    "aip_1234567890abcdef1234567890abcdef",
                    "air_1234567890abcdef1234567890abcdef",
                    state["environment"],
                    state["account_id"],
                    state["principal_id"],
                    "ai_profile_import_v1",
                    "a" * 64,
                    "b" * 64,
                    "{}",
                    lineage.binding_id,
                    lineage.binding_version,
                    lineage.latest_event_version,
                    lineage.latest_event_id,
                    lineage.lineage_sha256,
                    "2026-07-20T13:17:00+00:00",
                    timestamp,
                    timestamp,
                ),
            )
        self.connection.rollback()


if __name__ == "__main__":
    unittest.main()
