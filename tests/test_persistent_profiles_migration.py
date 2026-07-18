import contextlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.ownership_test_support import database_snapshot, install_ownership
from wahojobs.persistent_profile_schema import (
    MIGRATION_VERSION,
    PROFILE_INDEXES,
    PROFILE_TABLES,
    PROFILE_TRIGGERS,
    PROFILE_VIEWS,
    attest_persistent_profile_schema,
    migration_statement_count,
)
from wahojobs.ownership_schema import attest_ownership_schema

import scripts.persistent_profiles_migration as migration


ROOT = Path(__file__).resolve().parents[1]


def profile_object_names(conn):
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE "
            "name LIKE 'product_profile%' OR name LIKE 'current_product_profile%' "
            "OR name LIKE 'idx_product_profile%' "
            "OR name LIKE 'trg_product_profile%' "
            "OR name = 'uq_product_principals_profile_environment'"
        )
    }


class PersistentProfilesMigrationTests(unittest.TestCase):
    def test_cli_help_and_workspace_guard(self):
        command = [
            sys.executable,
            "-B",
            str(ROOT / "scripts" / "persistent_profiles_migration.py"),
        ]
        help_result = subprocess.run(
            command + ["--help"], cwd=ROOT, capture_output=True, text=True, check=False
        )
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--allow-workspace-db", help_result.stdout)
        blocked = subprocess.run(
            command
            + ["--db", str(ROOT / "data" / "wahojobs.sqlite"), "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(blocked.returncode, 1)
        payload = json.loads(blocked.stdout)
        self.assertEqual(payload["database_state"], "workspace_database_blocked")
        self.assertFalse(payload["changed"])

    def test_workspace_guard_uses_file_identity_and_allows_external_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace" / "wahojobs.sqlite"
            workspace.parent.mkdir()
            conn = install_ownership(workspace)
            conn.close()
            hard_link = root / "hard-link.sqlite"
            os.link(workspace, hard_link)
            self.assertTrue(os.path.samefile(hard_link, workspace))
            external = root / "copy" / "wahojobs.sqlite"
            external.parent.mkdir()
            shutil.copy2(workspace, external)

            variants = (
                workspace,
                Path(os.path.relpath(workspace, ROOT)),
                workspace.parent / "child" / ".." / workspace.name,
                Path(str(workspace).upper()),
                hard_link,
            )
            for candidate in variants:
                with self.subTest(candidate=candidate):
                    self.assertTrue(
                        migration.is_workspace_database_file(
                            candidate, workspace_path=workspace
                        )
                    )

            self.assertFalse(
                migration.is_workspace_database_file(
                    external, workspace_path=workspace
                )
            )
            self.assertFalse(
                migration.is_workspace_database_file(
                    root / "missing.sqlite", workspace_path=workspace
                )
            )

            with mock.patch.object(migration, "DB_PATH", workspace):
                blocked = self._run_main("--db", str(hard_link), "--json")
                self.assertEqual(blocked[0], 1)
                self.assertEqual(blocked[1]["database_state"], "workspace_database_blocked")
                mutation_blocked = self._run_main(
                    "--db", str(hard_link), "--yes", "--json"
                )
                self.assertEqual(mutation_blocked[0], 1)
                self.assertEqual(
                    mutation_blocked[1]["database_state"],
                    "workspace_database_blocked",
                )
                check_conn = migration.connect(workspace, read_only=True)
                try:
                    marker_count = check_conn.execute(
                        "SELECT COUNT(*) FROM wahojobs_schema_migrations WHERE version=?",
                        (MIGRATION_VERSION,),
                    ).fetchone()[0]
                finally:
                    check_conn.close()
                self.assertEqual(marker_count, 0)

                allowed = self._run_main(
                    "--db", str(hard_link), "--allow-workspace-db", "--json"
                )
                self.assertEqual(allowed[0], 0)
                self.assertEqual(allowed[1]["database_state"], "pending")
                applied = self._run_main(
                    "--db",
                    str(hard_link),
                    "--yes",
                    "--allow-workspace-db",
                    "--json",
                )
                self.assertEqual(applied[0], 0)
                self.assertEqual(applied[1]["database_state"], "migrated")

            external_result = self._run_main("--db", str(external), "--json")
            self.assertEqual(external_result[0], 0)
            self.assertEqual(external_result[1]["database_state"], "pending")
            missing_result = self._run_main(
                "--db", str(root / "missing.sqlite"), "--json"
            )
            self.assertEqual(missing_result[0], 0)
            self.assertEqual(missing_result[1]["database_state"], "nonexistent")

    def test_workspace_guard_symlink_when_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace.sqlite"
            conn = install_ownership(workspace)
            conn.close()
            link = root / "workspace-link.sqlite"
            try:
                os.symlink(workspace, link)
            except OSError as exc:
                self.skipTest(f"File symlinks are unavailable on this platform: {exc}")
            self.assertTrue(
                migration.is_workspace_database_file(link, workspace_path=workspace)
            )

    def test_workspace_guard_junction_requires_platform_file_alias_support(self):
        self.skipTest(
            "Windows directory junctions cannot directly alias one SQLite file; "
            "resolved-path, symlink, and hard-link identities are covered separately."
        )

    def test_pending_apply_inventory_attestation_and_second_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profiles.sqlite"
            conn = install_ownership(path)
            before = database_snapshot(conn)
            pending = migration.classify_database(conn)
            self.assertEqual(pending["database_state"], "pending")
            self.assertEqual(pending["schema_attestation"]["state"], "pending")

            result = migration.apply_persistent_profiles_migration(conn)
            self.assertTrue(result["changed"])
            self.assertEqual(result["statement_count"], migration_statement_count())
            self.assertEqual(result["profile_table_counts"], {table: 0 for table in PROFILE_TABLES})
            attestation = attest_persistent_profile_schema(conn)
            self.assertEqual(attestation["state"], "correctly_installed")
            self.assertFalse(attestation["findings"])
            self.assertEqual(attest_ownership_schema(conn)["state"], "correctly_installed")
            self.assertEqual(
                set(attestation["expected_objects"]), set(attestation["present_objects"])
            )
            objects = {
                (row[0], row[1])
                for row in conn.execute(
                    "SELECT type, name FROM sqlite_master WHERE name IN ("
                    + ",".join("?" for _ in (*PROFILE_TABLES, *PROFILE_VIEWS, *PROFILE_INDEXES, *PROFILE_TRIGGERS))
                    + ")",
                    (*PROFILE_TABLES, *PROFILE_VIEWS, *PROFILE_INDEXES, *PROFILE_TRIGGERS),
                )
            }
            self.assertTrue({("table", name) for name in PROFILE_TABLES}.issubset(objects))
            self.assertTrue({("view", name) for name in PROFILE_VIEWS}.issubset(objects))
            self.assertTrue({("index", name) for name in PROFILE_INDEXES}.issubset(objects))
            self.assertTrue({("trigger", name) for name in PROFILE_TRIGGERS}.issubset(objects))
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM wahojobs_schema_migrations WHERE version=?",
                    (MIGRATION_VERSION,),
                ).fetchone()[0],
                1,
            )
            after = database_snapshot(conn)
            for table, fingerprint in before.items():
                if table == "wahojobs_schema_migrations":
                    continue
                self.assertEqual(after[table], fingerprint)

            second = migration.apply_persistent_profiles_migration(conn)
            self.assertFalse(second["changed"])
            self.assertEqual(second["database_state"], "already_migrated")
            conn.close()

    def test_prerequisite_partial_conflicting_and_definition_drift_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.sqlite"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE wahojobs_schema_migrations (version TEXT PRIMARY KEY)")
            conn.commit()
            self.assertEqual(
                migration.classify_database(conn)["database_state"],
                "prerequisite_migration_absent",
            )
            conn.close()

            partial_path = Path(tmp) / "partial.sqlite"
            conn = install_ownership(partial_path)
            conn.execute("CREATE TABLE product_profiles (profile_id TEXT)")
            conn.commit()
            self.assertEqual(
                migration.classify_database(conn)["database_state"],
                "migration_004_partial_inconsistent",
            )
            conn.close()

            conflict_path = Path(tmp) / "conflict.sqlite"
            conn = install_ownership(conflict_path)
            conn.execute("CREATE VIEW product_profiles AS SELECT 1 AS profile_id")
            conn.commit()
            self.assertEqual(
                migration.classify_database(conn)["database_state"],
                "unexpected_object_conflict",
            )
            conn.close()

            drift_path = Path(tmp) / "drift.sqlite"
            conn = install_ownership(drift_path)
            migration.apply_persistent_profiles_migration(conn)
            conn.execute("DROP TRIGGER trg_product_profiles_no_update")
            conn.execute(
                "CREATE TRIGGER trg_product_profiles_no_update BEFORE UPDATE ON product_profiles "
                "BEGIN SELECT RAISE(ABORT, 'replacement'); END"
            )
            conn.commit()
            attestation = attest_persistent_profile_schema(conn)
            self.assertEqual(attestation["state"], "schema_definition_mismatch")
            self.assertEqual(
                migration.classify_database(conn)["database_state"],
                "migration_004_schema_definition_mismatch",
            )
            conn.close()

    def test_complete_manifest_detects_table_fk_index_view_trigger_and_unexpected_drift(self):
        canonical = migration.MIGRATION_PATH.read_text(encoding="utf-8")
        mutations = {
            "table_check": lambda sql: sql.replace(
                "CHECK (revision_number >= 1)", "CHECK (revision_number >= 0)", 1
            ),
            "foreign_key": lambda sql: sql.replace(
                "REFERENCES product_profiles(profile_id, principal_id, environment_namespace) ON DELETE CASCADE",
                "REFERENCES product_profiles(profile_id, principal_id, environment_namespace) ON DELETE RESTRICT",
                1,
            ),
            "named_index": lambda sql: sql.replace(
                "ON product_profiles(environment_namespace, created_at)",
                "ON product_profiles(created_at, environment_namespace)",
                1,
            ),
            "view": lambda sql: sql.replace(
                "revision.revision_kind AS current_revision_kind",
                "revision.lifecycle_status AS current_revision_kind",
                1,
            ),
            "trigger": lambda sql: sql.replace(
                "product profile identity is immutable", "replacement body", 1
            ),
            "source_sealing": lambda sql: sql.replace(
                "WHERE revision.revision_id = NEW.revision_id",
                "WHERE revision.revision_id = NEW.revision_id AND 0",
                1,
            ),
            "source_c1_controls": lambda sql: sql.replace(
                "BETWEEN 127 AND 159", "= 127", 1
            ),
            "structured_key_grammar": lambda sql: sql.replace(
                "node.key GLOB '*[^a-z0-9_]*'",
                "node.key GLOB '*[^a-z0-9_.-]*'",
                1,
            ),
            "structured_privacy": lambda sql: sql.replace(
                "'originaltext', 'rawtext', 'rawinput', 'rawcontent'",
                "'originaltext', 'rawinput', 'rawcontent', 'allowedrawtext'",
                1,
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"{name}.sqlite"
                conn = install_ownership(path)
                conn.executescript(mutate(canonical))
                conn.execute(
                    "INSERT INTO wahojobs_schema_migrations(version) VALUES (?)",
                    (MIGRATION_VERSION,),
                )
                conn.commit()
                self.assertNotEqual(
                    attest_persistent_profile_schema(conn)["state"],
                    "correctly_installed",
                )
                conn.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unexpected.sqlite"
            conn = install_ownership(path)
            migration.apply_persistent_profiles_migration(conn)
            conn.execute(
                "CREATE INDEX idx_product_profiles_unexpected ON product_profiles(created_at)"
            )
            conn.commit()
            reasons = {
                item["reason"] for item in attest_persistent_profile_schema(conn)["findings"]
            }
            self.assertIn("unexpected_persistent_profile_object", reasons)
            conn.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unexpected-table.sqlite"
            conn = install_ownership(path)
            migration.apply_persistent_profiles_migration(conn)
            conn.execute("CREATE TABLE product_profile_events (event_id TEXT)")
            conn.commit()
            reasons = {
                item["reason"] for item in attest_persistent_profile_schema(conn)["findings"]
            }
            self.assertIn("unexpected_persistent_profile_object", reasons)
            conn.close()

    def test_every_fault_hook_and_durable_checkpoint_rolls_back_all_004_state(self):
        statement_count = migration_statement_count()
        checkpoints = migration.failure_injection_state_map(statement_count)
        points = tuple(checkpoints)
        self.assertEqual(len(points), 54)
        self.assertEqual(len(set(checkpoints.values())), 22)
        self.assertEqual(
            migration.failure_injection_accounting(statement_count),
            {
                "fault_injection_hook_count": 54,
                "durable_state_checkpoint_count": 22,
            },
        )
        covered_checkpoints = set()
        for point in points:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "failure.sqlite"
                conn = install_ownership(path)
                before = database_snapshot(conn)

                def fail(current):
                    if current == point:
                        raise RuntimeError("injected migration failure")

                with self.assertRaises(RuntimeError):
                    migration.apply_persistent_profiles_migration(
                        conn, failure_injector=fail
                    )
                covered_checkpoints.add(checkpoints[point])
                self.assertFalse(conn.in_transaction)
                self.assertEqual(database_snapshot(conn), before)
                self.assertFalse(profile_object_names(conn))
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM wahojobs_schema_migrations WHERE version=?",
                        (MIGRATION_VERSION,),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                conn.close()
                self.assertFalse(Path(str(path) + "-journal").exists())
                self.assertFalse(Path(str(path) + "-wal").exists())
                self.assertFalse(Path(str(path) + "-shm").exists())
        self.assertEqual(covered_checkpoints, set(checkpoints.values()))

    def test_caller_transaction_is_rejected_without_changing_caller_control(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "caller.sqlite"
            conn = install_ownership(path)
            conn.execute("BEGIN")
            conn.execute(
                "INSERT INTO companies(name, slug, careers_url) "
                "VALUES ('Caller Work', 'caller-work', 'https://example.test')"
            )
            with self.assertRaises(migration.PersistentProfilesMigrationError):
                migration.apply_persistent_profiles_migration(conn)
            self.assertTrue(conn.in_transaction)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM companies WHERE name='Caller Work'").fetchone()[0],
                1,
            )
            self.assertFalse(profile_object_names(conn))
            conn.rollback()
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM companies WHERE name='Caller Work'").fetchone()[0],
                0,
            )
            conn.close()

    def _run_main(self, *arguments):
        output = io.StringIO()
        with mock.patch.object(
            sys,
            "argv",
            [str(ROOT / "scripts" / "persistent_profiles_migration.py"), *arguments],
        ), contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            migration.main()
        return raised.exception.code, json.loads(output.getvalue())


if __name__ == "__main__":
    unittest.main()
