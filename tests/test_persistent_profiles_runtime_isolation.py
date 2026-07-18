import tempfile
import unittest
from pathlib import Path

from tests.ownership_test_support import database_snapshot, install_ownership

import scripts.persistent_profiles_migration as migration


ROOT = Path(__file__).resolve().parents[1]


class PersistentProfilesRuntimeIsolationTests(unittest.TestCase):
    def test_base_schema_and_browser_runtime_do_not_install_or_import_migration_004(self):
        schema = (ROOT / "wahojobs" / "db" / "schema.sql").read_text(encoding="utf-8")
        local_app = (ROOT / "scripts" / "local_product_app.py").read_text(encoding="utf-8")
        self.assertNotIn("product_profile_revisions", schema)
        self.assertNotIn("product_profile_sources", schema)
        self.assertNotIn("current_product_profiles", schema)
        self.assertNotIn("persistent_profiles_migration", local_app)
        self.assertNotIn("persistent_profile_schema", local_app)
        self.assertNotIn("principal_id", local_app)
        self.assertNotIn("persistent_profile_id", local_app)

    def test_migration_changes_only_empty_dormant_objects_in_temporary_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "compatibility.sqlite"
            conn = install_ownership(path)
            before = database_snapshot(conn)
            migration.apply_persistent_profiles_migration(conn)
            after = database_snapshot(conn)
            for table, fingerprint in before.items():
                if table == "wahojobs_schema_migrations":
                    continue
                self.assertEqual(after[table], fingerprint)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_pipeline_items").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM applicant_status_updates").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM product_profiles").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM product_principals").fetchone()[0], 0
            )
            conn.close()

    def test_documented_dormant_privacy_lifecycle_and_fault_contracts(self):
        documentation = (
            ROOT / "docs" / "persistent_product_profiles.md"
        ).read_text(encoding="utf-8")
        for statement in (
            "Migration 004 is committed infrastructure only. It is not installed",
            "No source can be inserted, updated, or deleted after its revision exists.",
            "lowercase ASCII `snake_case` object keys",
            "Normal Unicode remains valid in profile values.",
            "every other C0 control (U+0000–U+001F)",
            "every C1 control (U+0080–U+009F)",
            "Archived profiles may receive edit or correction revisions",
            "reactivation requires a distinct `reactivate` revision",
            "54 logical hook labels covering 22 distinct transaction-visible database states",
            "B2A has no profile row service or row-level profile reconciliation",
        ):
            with self.subTest(statement=statement):
                self.assertIn(statement, documentation)


if __name__ == "__main__":
    unittest.main()
