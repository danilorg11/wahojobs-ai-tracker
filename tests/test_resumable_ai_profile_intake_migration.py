from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.ai_profile_import_migration import apply_ai_profile_import_migration
from scripts.public_job_identity_migration import apply_public_job_identity_migration
from scripts.resumable_ai_profile_intake_migration import (
    ResumableAIProfileIntakeMigrationError,
    apply_resumable_ai_profile_intake_migration,
)
from tests.workos_authkit_test_support import build_m008
from wahojobs.closed_schema_authority import current_closed_schema_is_exact
from wahojobs.resumable_ai_profile_intake_schema import (
    EXPECTED_MIGRATION_VERSIONS,
    EXPECTED_SCHEMA_FINGERPRINT,
    EXPECTED_SCHEMA_OBJECT_COUNT,
    attest_resumable_ai_profile_intake_schema,
    migration_statement_count,
)


class ResumableAIProfileIntakeMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="wahojobs-m011-test-", ignore_cleanup_errors=True
        )
        self.path = Path(self.directory.name) / "database.sqlite3"
        self.connection = build_m008(self.path)
        apply_public_job_identity_migration(self.connection)
        apply_ai_profile_import_migration(self.connection)

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def test_exact_m010_upgrades_to_attested_empty_m011_and_reapplies_as_noop(self):
        self.assertEqual(
            attest_resumable_ai_profile_intake_schema(self.connection)["state"],
            "resumable_ai_intake_pending",
        )
        result = apply_resumable_ai_profile_intake_migration(self.connection)
        self.assertTrue(result["applied"])
        report = attest_resumable_ai_profile_intake_schema(self.connection)
        self.assertEqual(report["state"], "correctly_installed")
        self.assertEqual(report["actual_schema_object_count"], EXPECTED_SCHEMA_OBJECT_COUNT)
        self.assertEqual(report["actual_schema_fingerprint"], EXPECTED_SCHEMA_FINGERPRINT)
        self.assertEqual(report["present_migration_versions"], list(EXPECTED_MIGRATION_VERSIONS))
        self.assertGreaterEqual(migration_statement_count(), 10)
        self.assertTrue(current_closed_schema_is_exact(self.connection))
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertFalse(apply_resumable_ai_profile_intake_migration(self.connection)["applied"])

    def test_failure_rolls_back_table_triggers_and_marker(self):
        with self.assertRaises(ResumableAIProfileIntakeMigrationError):
            apply_resumable_ai_profile_intake_migration(
                self.connection,
                failure_injector=lambda point: (_ for _ in ()).throw(RuntimeError())
                if point == "after_statement_3"
                else None,
            )
        self.assertEqual(
            attest_resumable_ai_profile_intake_schema(self.connection)["state"],
            "resumable_ai_intake_pending",
        )
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM sqlite_schema WHERE name='ai_profile_intake_checkpoints'"
            ).fetchone()
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM wahojobs_schema_migrations "
                "WHERE version='011_resumable_ai_profile_intake'"
            ).fetchone()[0],
            0,
        )

    def test_tampered_installed_schema_is_rejected(self):
        apply_resumable_ai_profile_intake_migration(self.connection)
        self.connection.execute(
            "DROP TRIGGER trg_ai_profile_intake_checkpoints_update_guard"
        )
        report = attest_resumable_ai_profile_intake_schema(self.connection)
        self.assertEqual(report["state"], "partial_inconsistent")
        self.assertTrue(report["blocking"])
        self.assertFalse(current_closed_schema_is_exact(self.connection))


if __name__ == "__main__":
    unittest.main()
