from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.public_job_identity_migration import (
    PublicJobIdentityMigrationError,
    apply_public_job_identity_migration,
)
from tests.closed_schema_convergence_test_support import (
    apply_m007,
    build_fresh_m001_m006,
)
from tests.workos_authkit_test_support import build_m008
from wahojobs.closed_schema_authority import current_closed_schema_is_exact
from wahojobs.public_job_identity_schema import (
    EXPECTED_MIGRATION_VERSIONS,
    EXPECTED_SCHEMA_FINGERPRINT,
    EXPECTED_SCHEMA_OBJECT_COUNT,
    attest_public_job_identity_schema,
    migration_statement_count,
)
from wahojobs.workos_authkit_staging import (
    WorkOSAuthKitStagingError,
    validate_workos_authkit_staging_database,
)


class PublicJobIdentityMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="wahojobs-m009-test-",
            ignore_cleanup_errors=True,
        )
        self.path = Path(self.directory.name) / "database.sqlite3"
        self.connection = build_m008(self.path)

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def test_exact_m008_is_applicable_and_atomic_m009_is_exact_and_empty(self):
        before = attest_public_job_identity_schema(self.connection)
        self.assertEqual(before["state"], "public_job_identity_pending")
        self.assertTrue(before["applicable"])
        self.assertTrue(validate_workos_authkit_staging_database(self.connection))

        result = apply_public_job_identity_migration(self.connection)

        self.assertEqual(
            result,
            {
                "migration_version": "009_public_job_identity",
                "state": "correctly_installed",
                "applied": True,
            },
        )
        report = attest_public_job_identity_schema(self.connection)
        self.assertEqual(report["state"], "correctly_installed")
        self.assertEqual(report["actual_schema_object_count"], EXPECTED_SCHEMA_OBJECT_COUNT)
        self.assertEqual(report["actual_schema_fingerprint"], EXPECTED_SCHEMA_FINGERPRINT)
        self.assertEqual(report["present_migration_versions"], list(EXPECTED_MIGRATION_VERSIONS))
        self.assertEqual(
            {
                table: self.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in (
                    "public_job_identities",
                    "public_job_paths",
                    "public_job_bindings",
                )
            },
            {
                "public_job_identities": 0,
                "public_job_paths": 0,
                "public_job_bindings": 0,
            },
        )
        self.assertTrue(current_closed_schema_is_exact(self.connection))
        self.assertTrue(validate_workos_authkit_staging_database(self.connection))
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_failure_rolls_back_every_m009_object_and_marker(self):
        for failure_point in ("after_statement_8", "after_marker"):
            with self.subTest(failure_point=failure_point):
                def fail(point):
                    if point == failure_point:
                        raise RuntimeError("injected")

                with self.assertRaises(PublicJobIdentityMigrationError):
                    apply_public_job_identity_migration(
                        self.connection,
                        failure_injector=fail,
                    )

                self.assertEqual(
                    attest_public_job_identity_schema(self.connection)["state"],
                    "public_job_identity_pending",
                )
                self.assertEqual(
                    self.connection.execute(
                        "SELECT COUNT(*) FROM sqlite_schema "
                        "WHERE name LIKE 'public_job_%'"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    self.connection.execute(
                        "SELECT COUNT(*) FROM wahojobs_schema_migrations "
                        "WHERE version='009_public_job_identity'"
                    ).fetchone()[0],
                    0,
                )
                self.assertTrue(current_closed_schema_is_exact(self.connection))

    def test_exact_reapplication_is_a_noop_and_drift_is_rejected(self):
        first = apply_public_job_identity_migration(self.connection)
        second = apply_public_job_identity_migration(self.connection)
        self.assertTrue(first["applied"])
        self.assertFalse(second["applied"])

        self.connection.execute(
            "CREATE INDEX idx_public_job_unapproved ON public_job_identities(disposition)"
        )
        self.connection.commit()
        self.assertEqual(
            attest_public_job_identity_schema(self.connection)["state"],
            "partial_inconsistent",
        )
        self.assertFalse(current_closed_schema_is_exact(self.connection))
        with self.assertRaises(PublicJobIdentityMigrationError):
            apply_public_job_identity_migration(self.connection)
        with self.assertRaises(WorkOSAuthKitStagingError):
            validate_workos_authkit_staging_database(self.connection)

    def test_existing_inventory_rows_are_preserved(self):
        self.connection.execute(
            "INSERT INTO companies "
            "(id,name,slug,careers_url,source_tier,inventory_model,"
            "market_count_policy) VALUES "
            "(8101,'Synthetic Existing','synthetic-existing',"
            "'https://example.test/','core','live_feed','count_live')"
        )
        self.connection.execute(
            "INSERT INTO canonical_opportunities "
            "(id,company_id,canonical_key,canonical_title,normalized_title,"
            "source_category,first_seen_at,last_seen_at,is_active,variant_count) "
            "VALUES (8102,8101,'synthetic::existing','Synthetic Existing',"
            "'synthetic existing','Generalist','2026-08-20T00:00:00+00:00',"
            "'2026-08-20T00:00:00+00:00',1,0)"
        )
        self.connection.commit()
        before = tuple(
            self.connection.execute(
                "SELECT * FROM canonical_opportunities WHERE id=8102"
            ).fetchone()
        )

        apply_public_job_identity_migration(self.connection)

        after = tuple(
            self.connection.execute(
                "SELECT * FROM canonical_opportunities WHERE id=8102"
            ).fetchone()
        )
        self.assertEqual(after, before)

    def test_m007_is_not_an_m009_prerequisite(self):
        path = Path(self.directory.name) / "m007.sqlite3"
        connection = build_fresh_m001_m006(path)
        try:
            apply_m007(connection, path)
            self.assertEqual(
                attest_public_job_identity_schema(connection)["state"],
                "invalid_prerequisite",
            )
            with self.assertRaises(PublicJobIdentityMigrationError):
                apply_public_job_identity_migration(connection)
        finally:
            connection.close()

    def test_migration_module_is_inert_until_called(self):
        self.assertGreater(migration_statement_count(), 15)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM wahojobs_schema_migrations "
                "WHERE version='009_public_job_identity'"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
