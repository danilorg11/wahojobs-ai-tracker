import hashlib
import gc
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.accounts_test_support import (
    ROOT,
    accounts_migration,
    connect,
    install_base,
    install_migration_001,
)


MIGRATION_SCRIPT = ROOT / "scripts" / "accounts_migration.py"


class AccountsMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "accounts.sqlite"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_classifies_nonexistent_empty_legacy_001_ready_and_applied(self):
        result = subprocess.run(
            [sys.executable, "-B", str(MIGRATION_SCRIPT), "--db", str(self.db_path), "--json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout)["database_state"], "nonexistent")
        self.assertFalse(self.db_path.exists())

        self.db_path.touch()
        empty_before = self.db_path.read_bytes()
        empty_stat = self.db_path.stat()
        empty_result = subprocess.run(
            [sys.executable, "-B", str(MIGRATION_SCRIPT), "--db", str(self.db_path), "--json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(json.loads(empty_result.stdout)["database_state"], "empty")
        self.assertEqual(self.db_path.read_bytes(), empty_before)
        self.assertEqual(self.db_path.stat().st_mtime_ns, empty_stat.st_mtime_ns)
        conn = connect(self.db_path)
        self.assertEqual(accounts_migration.classify_database(conn)["database_state"], "empty")
        conn.close()

        self.db_path.unlink()
        conn = install_base(self.db_path)
        self.assertEqual(
            accounts_migration.classify_database(conn)["database_state"],
            "migration_001_absent",
        )
        install_migration_001(conn)
        self.assertEqual(
            accounts_migration.classify_database(conn)["database_state"],
            "migration_001_present",
        )
        applied = accounts_migration.apply_accounts_migration(conn)
        self.assertTrue(applied["changed"])
        self.assertFalse(applied["reconciliation"]["blocking"])
        self.assertEqual(
            accounts_migration.classify_database(conn)["database_state"],
            "already_migrated",
        )
        second = accounts_migration.apply_accounts_migration(conn)
        self.assertFalse(second["changed"])
        conn.close()

    def test_default_cli_is_read_only_and_apply_requires_yes(self):
        conn = install_base(self.db_path)
        install_migration_001(conn)
        conn.close()
        before = self.db_path.read_bytes()
        result = subprocess.run(
            [sys.executable, "-B", str(MIGRATION_SCRIPT), "--db", str(self.db_path), "--json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        report = json.loads(result.stdout)
        self.assertEqual(report["mode"], "inspection")
        self.assertTrue(report["applicable"])
        self.assertEqual(self.db_path.read_bytes(), before)
        subprocess.run(
            [
                sys.executable,
                "-B",
                str(MIGRATION_SCRIPT),
                "--db",
                str(self.db_path),
                "--yes",
                "--json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        conn = connect(self.db_path)
        self.assertEqual(
            accounts_migration.classify_database(conn)["database_state"],
            "already_migrated",
        )
        conn.close()

    def test_invalid_sqlite_and_workspace_guard_are_privacy_safe(self):
        self.db_path.write_bytes(b"not sqlite")
        result = subprocess.run(
            [sys.executable, "-B", str(MIGRATION_SCRIPT), "--db", str(self.db_path), "--json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["database_state"], "invalid_sqlite")

        workspace = ROOT / "data" / "wahojobs.sqlite"
        result = subprocess.run(
            [sys.executable, "-B", str(MIGRATION_SCRIPT), "--db", str(workspace), "--json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["database_state"], "workspace_database_blocked")

    def test_partial_installation_is_refused(self):
        conn = install_base(self.db_path)
        install_migration_001(conn)
        conn.execute(
            "CREATE TABLE users (user_id TEXT PRIMARY KEY, lifecycle_status TEXT, row_version INTEGER)"
        )
        conn.commit()
        result = accounts_migration.classify_database(conn)
        self.assertEqual(result["database_state"], "migration_002_partial_inconsistent")
        with self.assertRaises(accounts_migration.AccountsMigrationError):
            accounts_migration.apply_accounts_migration(conn, classification=result)
        conn.close()

    def test_every_failure_boundary_rolls_back_all_002_objects_and_marker(self):
        statements = list(
            accounts_migration.iter_sql_statements(
                accounts_migration.MIGRATION_PATH.read_text(encoding="utf-8")
            )
        )
        points = [
            "before_first_ddl",
            "after_first_ddl",
            "before_trigger_install",
            "after_all_ddl",
            "before_marker_write",
            "after_marker_write",
            "before_reconciliation",
            "after_reconciliation_before_commit",
        ]
        points.extend(
            point
            for index in range(1, len(statements) + 1)
            for point in (f"before_statement_{index}", f"after_statement_{index}")
        )
        for index, point in enumerate(points):
            with self.subTest(point=point):
                path = Path(self.temp_dir.name) / f"failure-{index}.sqlite"
                conn = install_base(path)
                install_migration_001(conn)
                before = {
                    table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in accounts_migration.PRESERVED_TABLES
                }

                def fail(current):
                    if current == point:
                        raise RuntimeError(point)

                with self.assertRaises(RuntimeError):
                    accounts_migration.apply_accounts_migration(conn, failure_injector=fail)
                objects = {
                    (row["type"], row["name"])
                    for row in conn.execute(
                        "SELECT type, name FROM sqlite_master WHERE type IN ('table','index','trigger')"
                    )
                }
                self.assertFalse(accounts_migration.EXPECTED_ACCOUNT_OBJECTS & objects)
                self.assertIsNone(
                    conn.execute(
                        "SELECT 1 FROM wahojobs_schema_migrations WHERE version = ?",
                        (accounts_migration.MIGRATION_VERSION,),
                    ).fetchone()
                )
                after = {
                    table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in accounts_migration.PRESERVED_TABLES
                }
                self.assertEqual(after, before)
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                conn.close()
                self.assertFalse(Path(f"{path}-journal").exists())
                self.assertFalse(Path(f"{path}-wal").exists())
                self.assertFalse(Path(f"{path}-shm").exists())

    def test_migration_preserves_existing_business_rows_and_runtime_does_not_auto_install(self):
        conn = install_base(self.db_path)
        conn.execute(
            "INSERT INTO companies(name,slug,careers_url) VALUES ('Fixture','fixture','https://example.test')"
        )
        conn.commit()
        install_migration_001(conn)
        before = hashlib.sha256(
            json.dumps(
                [dict(row) for row in conn.execute("SELECT * FROM companies ORDER BY id")],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        accounts_migration.apply_accounts_migration(conn)
        after = hashlib.sha256(
            json.dumps(
                [dict(row) for row in conn.execute("SELECT * FROM companies ORDER BY id")],
                sort_keys=True,
            ).encode()
        ).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
        conn.close()

        other = Path(self.temp_dir.name) / "runtime.sqlite"
        from wahojobs.db.repository import initialize_database

        initialize_database(other)
        gc.collect()
        runtime_conn = connect(other)
        self.assertIsNone(
            runtime_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchone()
        )
        runtime_conn.close()


if __name__ == "__main__":
    unittest.main()
