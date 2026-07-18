import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.accounts_test_support import install_accounts


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "scripts" / "ownership_migration.py"
RECONCILE = ROOT / "scripts" / "ownership_reconcile.py"


class OwnershipCliTests(unittest.TestCase):
    def test_default_inspection_and_absent_reconciliation_are_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ownership.sqlite"
            conn = install_accounts(path)
            conn.close()
            before = (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes())
            inspected = self._run(MIGRATION, "--db", str(path), "--json")
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            self.assertEqual(json.loads(inspected.stdout)["database_state"], "pending")
            reconciled = self._run(RECONCILE, "--db", str(path), "--json")
            self.assertEqual(reconciled.returncode, 1)
            self.assertIn("migration_marker_missing", json.loads(reconciled.stdout)["blocking_reasons"])
            after = (path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes())
            self.assertEqual(after, before)

    def test_explicit_apply_then_reconcile_and_second_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ownership.sqlite"
            conn = install_accounts(path)
            conn.close()
            applied = self._run(MIGRATION, "--db", str(path), "--yes", "--json")
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertTrue(json.loads(applied.stdout)["changed"])
            reconciled = self._run(RECONCILE, "--db", str(path), "--json")
            self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
            self.assertFalse(json.loads(reconciled.stdout)["blocking"])
            repeated = self._run(MIGRATION, "--db", str(path), "--yes", "--json")
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertFalse(json.loads(repeated.stdout)["changed"])

    def _run(self, script, *args):
        return subprocess.run(
            [sys.executable, "-B", str(script), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )


if __name__ == "__main__":
    unittest.main()
