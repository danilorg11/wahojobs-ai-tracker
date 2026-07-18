import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.accounts_test_support import install_accounts, install_base, install_migration_001
from tests.ownership_test_support import database_snapshot, ownership_object_names

import scripts.ownership_migration as migration
from wahojobs.ownership_schema import attest_ownership_schema


class OwnershipMigrationTests(unittest.TestCase):
    def test_inspection_apply_and_second_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ownership.sqlite"
            conn = install_accounts(path)
            tables_before = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            self.assertEqual(migration.classify_database(conn)["database_state"], "pending")
            first = migration.apply_ownership_migration(conn)
            self.assertTrue(first["changed"])
            self.assertEqual(first["reconciliation"]["counts"]["principals"], 0)
            tables_after = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            self.assertEqual(
                tables_after - tables_before,
                {
                    "product_principals",
                    "legacy_owner_aliases",
                    "principal_account_bindings",
                    "ownership_binding_events",
                },
            )
            self.assertNotIn("ownership_claims", tables_after)
            second = migration.apply_ownership_migration(conn)
            self.assertFalse(second["changed"])
            self.assertEqual(second["database_state"], "already_migrated")
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM wahojobs_schema_migrations WHERE version=?",
                    (migration.MIGRATION_VERSION,),
                ).fetchone()[0],
                1,
            )
            conn.close()

    def test_missing_or_unreconciled_prerequisite_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_path = Path(tmp) / "base.sqlite"
            conn = install_base(base_path)
            self.assertEqual(
                migration.classify_database(conn)["database_state"],
                "prerequisite_migration_absent",
            )
            conn.close()

            pipeline_path = Path(tmp) / "pipeline.sqlite"
            conn = install_base(pipeline_path)
            install_migration_001(conn)
            self.assertEqual(
                migration.classify_database(conn)["database_state"],
                "prerequisite_migration_absent",
            )
            conn.close()

            drift_path = Path(tmp) / "drift.sqlite"
            conn = install_accounts(drift_path)
            conn.execute("DROP TRIGGER trg_account_lifecycle_events_no_delete")
            conn.commit()
            self.assertEqual(
                migration.classify_database(conn)["database_state"],
                "prerequisite_reconciliation_blocking",
            )
            conn.close()

    def test_partial_and_wrong_type_objects_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            partial_path = Path(tmp) / "partial.sqlite"
            conn = install_accounts(partial_path)
            conn.execute("CREATE TABLE product_principals (principal_id TEXT)")
            conn.commit()
            self.assertEqual(
                migration.classify_database(conn)["database_state"],
                "migration_003_partial_inconsistent",
            )
            conn.close()

            conflict_path = Path(tmp) / "conflict.sqlite"
            conn = install_accounts(conflict_path)
            conn.execute("CREATE VIEW product_principals AS SELECT 1 AS principal_id")
            conn.commit()
            self.assertEqual(
                migration.classify_database(conn)["database_state"],
                "unexpected_object_conflict",
            )
            conn.close()

    def test_complete_attestation_rejects_constraint_fk_index_trigger_and_unexpected_drift(self):
        canonical = migration.MIGRATION_PATH.read_text(encoding="utf-8")
        mutations = {
            "missing_alias_unique_and_autoindex": lambda sql: sql.replace(
                "  UNIQUE (environment_namespace, alias_kind, alias_value),\n", "", 1
            ),
            "changed_check": lambda sql: sql.replace(
                "CHECK (exclusive_account_binding IN (0, 1))",
                "CHECK (exclusive_account_binding IN (0, 1, 2))",
                1,
            ),
            "changed_foreign_key": lambda sql: sql.replace(
                "FOREIGN KEY (principal_id) REFERENCES product_principals(principal_id) ON DELETE RESTRICT",
                "FOREIGN KEY (principal_id) REFERENCES product_principals(principal_id) ON DELETE CASCADE",
                1,
            ),
            "changed_named_index": lambda sql: sql.replace(
                "ON product_principals(environment_namespace, principal_type, lifecycle_status)",
                "ON product_principals(principal_type, environment_namespace, lifecycle_status)",
                1,
            ),
            "altered_trigger": lambda sql: sql.replace(
                "product principals cannot be deleted",
                "replacement trigger body",
                1,
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"{name}.sqlite"
                conn = install_accounts(path)
                conn.executescript(mutate(canonical))
                conn.execute(
                    "INSERT INTO wahojobs_schema_migrations(version) VALUES (?)",
                    (migration.MIGRATION_VERSION,),
                )
                conn.commit()
                attestation = attest_ownership_schema(conn)
                self.assertTrue(attestation["blocking"])
                self.assertNotEqual(attestation["state"], "correctly_installed")
                self.assertNotEqual(
                    migration.classify_database(conn)["database_state"],
                    "already_migrated",
                )
                conn.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unexpected.sqlite"
            conn = install_accounts(path)
            migration.apply_ownership_migration(conn)
            conn.execute(
                "CREATE INDEX idx_product_principals_unexpected "
                "ON product_principals(updated_at)"
            )
            conn.commit()
            attestation = attest_ownership_schema(conn)
            self.assertIn(
                "unexpected_ownership_object",
                {item["reason"] for item in attestation["findings"]},
            )
            self.assertEqual(
                migration.classify_database(conn)["database_state"],
                "migration_003_schema_definition_mismatch",
            )
            conn.close()

    def test_apply_rejects_an_unrelated_caller_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "caller.sqlite"
            conn = install_accounts(path)
            conn.execute("BEGIN")
            with self.assertRaises(migration.OwnershipMigrationError):
                migration.apply_ownership_migration(conn)
            conn.rollback()
            self.assertNotIn("product_principals", ownership_object_names(conn))
            conn.close()

    def test_every_statement_failure_rolls_back_all_003_objects_and_marker(self):
        statement_count = len(
            list(
                migration.iter_sql_statements(
                    migration.MIGRATION_PATH.read_text(encoding="utf-8")
                )
            )
        )
        failure_points = [f"after_statement_{index}" for index in range(1, statement_count + 1)]
        failure_points.extend(
            [
                "before_first_ddl",
                "after_all_ddl",
                "before_marker_write",
                "after_marker_write",
                "before_schema_attestation",
                "after_schema_attestation",
                "before_reconciliation",
                "after_reconciliation_before_commit",
                "before_integrity_check",
                "after_integrity_check",
                "before_foreign_key_check",
                "after_foreign_key_check",
                "before_preserved_count_check",
                "after_preserved_count_check",
            ]
        )
        for failure_point in failure_points:
            with self.subTest(failure_point=failure_point), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "failure.sqlite"
                conn = install_accounts(path)
                before = database_snapshot(conn)

                def fail(point):
                    if point == failure_point:
                        raise RuntimeError("injected migration failure")

                with self.assertRaises(RuntimeError):
                    migration.apply_ownership_migration(conn, failure_injector=fail)
                self.assertFalse(conn.in_transaction)
                self.assertEqual(database_snapshot(conn), before)
                self.assertFalse(ownership_object_names(conn))
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM wahojobs_schema_migrations WHERE version=?",
                        (migration.MIGRATION_VERSION,),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                conn.close()
                self.assertFalse(Path(str(path) + "-journal").exists())
                self.assertFalse(Path(str(path) + "-wal").exists())
                self.assertFalse(Path(str(path) + "-shm").exists())


if __name__ == "__main__":
    unittest.main()
