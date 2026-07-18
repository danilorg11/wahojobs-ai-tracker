import tempfile
import unittest
from pathlib import Path

from tests.accounts_test_support import install_accounts
from tests.ownership_test_support import database_snapshot
from wahojobs.ownership_reconciliation import reconcile_ownership


ROOT = Path(__file__).resolve().parents[1]


class OwnershipRuntimeIsolationTests(unittest.TestCase):
    def test_base_schema_and_local_product_runtime_do_not_install_or_import_ownership(self):
        schema = (ROOT / "wahojobs" / "db" / "schema.sql").read_text(encoding="utf-8")
        local_app = (ROOT / "scripts" / "local_product_app.py").read_text(encoding="utf-8")
        self.assertNotIn("product_principals", schema)
        self.assertNotIn("ownership_migration", local_app)
        self.assertNotIn("ownership_reconciliation", local_app)
        self.assertNotIn("from wahojobs import ownership", local_app)
        self.assertNotIn("/claim", local_app)

    def test_read_only_discovery_does_not_change_existing_pipeline_or_account_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "compatibility.sqlite"
            conn = install_accounts(path)
            before = database_snapshot(conn)
            report = reconcile_ownership(conn)
            after = database_snapshot(conn)
            self.assertTrue(report["blocking"])
            self.assertIn("migration_marker_missing", report["blocking_reasons"])
            self.assertEqual(before, after)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
            conn.close()


if __name__ == "__main__":
    unittest.main()
