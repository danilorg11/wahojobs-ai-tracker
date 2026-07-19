import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import scripts.persistent_profile_canonical_v2_migration as migration
from tests.ownership_test_support import database_snapshot
from tests.persistent_profile_canonical_v2_test_support import (
    canonical_v2_document,
    insert_revision_v2,
)
from tests.persistent_profiles_test_support import (
    add_development_principal,
    canonical_document,
    create_profile,
    insert_revision,
    insert_source,
    install_persistent_profiles,
    stable_id,
    timestamp,
)
from wahojobs.persistent_profile_canonical_v2_schema import (
    MIGRATION_PATH,
    MIGRATION_VERSION,
    TEMPORARY_TABLES,
    attest_persistent_profile_canonical_v2_schema,
    migration_statement_count,
)
from wahojobs.persistent_profile_schema import (
    PROFILE_INDEXES,
    PROFILE_TABLES,
    PROFILE_TRIGGERS,
    PROFILE_VIEWS,
    attest_persistent_profile_schema,
    iter_sql_statements,
)


ROOT = Path(__file__).resolve().parents[1]


class PersistentProfileCanonicalV2MigrationTests(unittest.TestCase):
    def test_cli_help_workspace_guard_and_nonexistent_database(self):
        command = [sys.executable, "-B", str(ROOT / "scripts" / "persistent_profile_canonical_v2_migration.py")]
        help_result = subprocess.run(command + ["--help"], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--allow-workspace-db", help_result.stdout)
        blocked = subprocess.run(
            command + ["--db", str(ROOT / "data" / "wahojobs.sqlite"), "--json"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        self.assertEqual(blocked.returncode, 1)
        self.assertEqual(json.loads(blocked.stdout)["database_state"], "workspace_database_blocked")
        with tempfile.TemporaryDirectory() as tmp:
            missing = subprocess.run(
                command + ["--db", str(Path(tmp) / "missing.sqlite"), "--json"],
                cwd=ROOT, capture_output=True, text=True, check=False,
            )
            self.assertEqual(missing.returncode, 0)
            self.assertEqual(json.loads(missing.stdout)["database_state"], "nonexistent")

    def test_pending_apply_exact_inventory_combined_attestation_and_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m005.sqlite"
            conn = install_persistent_profiles(path)
            before = database_snapshot(conn)
            pending = migration.classify_database(conn)
            self.assertEqual(pending["database_state"], "pending")
            result = migration.apply_persistent_profile_canonical_v2_migration(conn)
            self.assertTrue(result["changed"])
            self.assertEqual(result["statement_count"], 41)
            self.assertEqual(result["statement_count"], migration_statement_count())
            final = attest_persistent_profile_canonical_v2_schema(conn)
            self.assertEqual(final["state"], "correctly_installed")
            self.assertEqual(final["expected_object_count"], 32)
            self.assertEqual(final["expected_object_count"], len(final["present_objects"]))
            self.assertEqual(attest_persistent_profile_schema(conn)["state"], "correctly_installed")
            object_types = {item.split(":", 1)[0] for item in final["present_objects"]}
            self.assertEqual(object_types, {"table", "view", "index", "trigger"})
            self.assertEqual(
                sum(item.startswith("index:sqlite_autoindex_") for item in final["present_objects"]), 12
            )
            for table, fingerprint in before.items():
                if table in {*PROFILE_TABLES, "wahojobs_schema_migrations"}:
                    continue
                self.assertEqual(database_snapshot(conn)[table], fingerprint)
            second = migration.apply_persistent_profile_canonical_v2_migration(conn)
            self.assertFalse(second["changed"])
            self.assertEqual(second["database_state"], "already_migrated")
            conn.close()

    def test_nonempty_state_is_refused_before_and_after_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            before_path = Path(tmp) / "before.sqlite"
            conn = install_persistent_profiles(before_path)
            principal = add_development_principal(conn, "1")
            create_profile(conn, principal, suffix="1")
            before = database_snapshot(conn)
            classified = migration.classify_database(conn)
            self.assertEqual(classified["database_state"], "persistent_profile_state_not_empty")
            with self.assertRaises(migration.PersistentProfileCanonicalV2MigrationError):
                migration.apply_persistent_profile_canonical_v2_migration(conn)
            self.assertEqual(database_snapshot(conn), before)
            conn.close()

            race_path = Path(tmp) / "race.sqlite"
            conn = install_persistent_profiles(race_path)
            principal = add_development_principal(conn, "2")
            inserted = threading.Event()
            writer_finished = threading.Event()

            def writer():
                writer_conn = sqlite3.connect(race_path, timeout=5)
                writer_conn.execute("PRAGMA foreign_keys = ON")
                profile_id = stable_id("prf", 2)
                revision_id = stable_id("pvr", 2)
                writer_conn.execute("BEGIN IMMEDIATE")
                writer_conn.execute(
                    "INSERT INTO product_profiles "
                    "(profile_id, principal_id, environment_namespace, initial_revision_id, created_at) "
                    "VALUES (?, ?, 'test', ?, ?)",
                    (profile_id, principal, revision_id, timestamp()),
                )
                insert_source(
                    writer_conn,
                    source_id=stable_id("pfs", 2),
                    revision_id=revision_id,
                    profile_id=profile_id,
                    principal_id=principal,
                    environment="test",
                    source_content="confirmed",
                    accepted_at=timestamp(),
                )
                insert_revision(
                    writer_conn,
                    revision_id=revision_id,
                    profile_id=profile_id,
                    principal_id=principal,
                    environment="test",
                    revision_number=1,
                    previous_revision_id=None,
                    revision_kind="initial",
                    lifecycle_status="active",
                    structured_json=canonical_document(profile_id),
                    created_at=timestamp(),
                )
                inserted.set()
                time.sleep(0.2)
                writer_conn.commit()
                writer_conn.close()
                writer_finished.set()

            def inject(point):
                if point == "before_begin_immediate" and not inserted.is_set():
                    threading.Thread(target=writer, daemon=True).start()
                    self.assertTrue(inserted.wait(2))

            with self.assertRaisesRegex(
                migration.PersistentProfileCanonicalV2MigrationError,
                "persistent_profile_state_not_empty",
            ):
                migration.apply_persistent_profile_canonical_v2_migration(
                    conn, failure_injector=inject
                )
            self.assertTrue(writer_finished.wait(2))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM product_profiles").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM wahojobs_schema_migrations WHERE version=?", (MIGRATION_VERSION,)).fetchone()[0], 0)
            self.assertEqual(attest_persistent_profile_schema(conn)["state"], "correctly_installed")
            conn.close()

    def test_forged_marker_missing_marker_partial_residue_and_definition_drift_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            forged = install_persistent_profiles(Path(tmp) / "forged.sqlite")
            forged.execute("INSERT INTO wahojobs_schema_migrations(version) VALUES (?)", (MIGRATION_VERSION,))
            forged.commit()
            self.assertNotEqual(attest_persistent_profile_canonical_v2_schema(forged)["state"], "correctly_installed")
            self.assertEqual(attest_persistent_profile_schema(forged)["state"], "forward_schema_invalid")
            forged.close()

            missing = install_persistent_profiles(Path(tmp) / "missing-marker.sqlite")
            migration.apply_persistent_profile_canonical_v2_migration(missing)
            missing.execute("DELETE FROM wahojobs_schema_migrations WHERE version=?", (MIGRATION_VERSION,))
            missing.commit()
            self.assertEqual(attest_persistent_profile_canonical_v2_schema(missing)["state"], "partial_inconsistent")
            missing.close()

            residue = install_persistent_profiles(Path(tmp) / "residue.sqlite")
            residue.execute("CREATE TABLE product_profiles_m005_backup (profile_id TEXT)")
            residue.commit()
            attestation = attest_persistent_profile_canonical_v2_schema(residue)
            self.assertNotEqual(attestation["state"], "pending")
            self.assertIn("product_profiles_m005_backup", attestation["temporary_residue"])
            residue.close()

            drift = install_persistent_profiles(Path(tmp) / "drift.sqlite")
            migration.apply_persistent_profile_canonical_v2_migration(drift)
            drift.execute("DROP TRIGGER trg_product_profile_revisions_insert_guard")
            drift.execute(
                "CREATE TRIGGER trg_product_profile_revisions_insert_guard BEFORE INSERT ON product_profile_revisions "
                "BEGIN SELECT RAISE(ABORT, 'replacement'); END"
            )
            drift.commit()
            self.assertEqual(attest_persistent_profile_canonical_v2_schema(drift)["state"], "schema_mismatch")
            self.assertEqual(attest_persistent_profile_schema(drift)["state"], "forward_schema_invalid")
            drift.close()

    def test_m005_specific_classifications_precede_m004_forward_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_path = Path(tmp) / "missing-marker.sqlite"
            missing = install_persistent_profiles(missing_path)
            migration.apply_persistent_profile_canonical_v2_migration(missing)
            missing.execute(
                "DELETE FROM wahojobs_schema_migrations WHERE version=?",
                (MIGRATION_VERSION,),
            )
            missing.commit()
            self.assertEqual(
                attest_persistent_profile_canonical_v2_schema(missing)["state"],
                "partial_inconsistent",
            )
            self.assertEqual(
                migration.classify_database(missing)["database_state"],
                "migration_005_partial_inconsistent",
            )
            missing.close()

            code, payload = self._run_main("--db", str(missing_path), "--json")
            self.assertEqual(code, 0)
            self.assertEqual(
                payload["database_state"], "migration_005_partial_inconsistent"
            )
            code, output = self._run_main_text("--db", str(missing_path))
            self.assertEqual(code, 0)
            self.assertIn(
                "Database state: migration_005_partial_inconsistent", output
            )

            forged = install_persistent_profiles(Path(tmp) / "forged-marker.sqlite")
            forged.execute(
                "INSERT INTO wahojobs_schema_migrations(version) VALUES (?)",
                (MIGRATION_VERSION,),
            )
            forged.commit()
            self.assertEqual(
                migration.classify_database(forged)["database_state"],
                "migration_005_partial_inconsistent",
            )
            forged.close()

            drift = install_persistent_profiles(Path(tmp) / "drift.sqlite")
            migration.apply_persistent_profile_canonical_v2_migration(drift)
            drift.execute("DROP TRIGGER trg_product_profile_revisions_insert_guard")
            drift.execute(
                "CREATE TRIGGER trg_product_profile_revisions_insert_guard "
                "BEFORE INSERT ON product_profile_revisions "
                "BEGIN SELECT RAISE(ABORT, 'replacement'); END"
            )
            drift.commit()
            self.assertEqual(
                migration.classify_database(drift)["database_state"],
                "migration_005_schema_mismatch",
            )
            drift.close()

            conflict = install_persistent_profiles(Path(tmp) / "conflict.sqlite")
            migration.apply_persistent_profile_canonical_v2_migration(conflict)
            conflict.execute("DROP VIEW current_product_profiles")
            conflict.execute("CREATE TABLE current_product_profiles (profile_id TEXT)")
            conflict.commit()
            self.assertEqual(
                migration.classify_database(conflict)["database_state"],
                "migration_005_conflicting",
            )
            conflict.close()

    def test_structured_taxonomy_matrix_and_cli_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def m004(name):
                return install_persistent_profiles(root / f"{name}.sqlite")

            def final(name):
                conn = m004(name)
                migration.apply_persistent_profile_canonical_v2_migration(conn)
                return conn

            partial_setups = {
                "marker_with_m004": lambda conn: conn.execute(
                    "INSERT INTO wahojobs_schema_migrations(version) VALUES (?)",
                    (MIGRATION_VERSION,),
                ),
                "missing_final_view": lambda conn: conn.execute(
                    "DROP VIEW current_product_profiles"
                ),
                "missing_lifecycle_trigger": lambda conn: conn.execute(
                    "DROP TRIGGER trg_product_profile_sources_insert_guard"
                ),
                "missing_json_trigger": lambda conn: conn.execute(
                    "DROP TRIGGER trg_product_profile_revisions_insert_guard"
                ),
                "missing_named_index": lambda conn: conn.execute(
                    "DROP INDEX idx_product_profile_sources_profile"
                ),
                "profiles_backup": lambda conn: conn.execute(
                    "CREATE TABLE product_profiles_m005_backup(profile_id TEXT)"
                ),
                "revisions_backup": lambda conn: conn.execute(
                    "CREATE TABLE product_profile_revisions_m005_backup(revision_id TEXT)"
                ),
                "sources_backup": lambda conn: conn.execute(
                    "CREATE TABLE product_profile_sources_m005_backup(source_id TEXT)"
                ),
            }
            for name, mutate in partial_setups.items():
                with self.subTest(category="partial", name=name):
                    conn = m004(name) if name == "marker_with_m004" else final(name)
                    mutate(conn)
                    conn.commit()
                    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
                    conn.close()
                    self._assert_taxonomy_path(
                        path,
                        "partial_inconsistent",
                        "migration_005_partial_inconsistent",
                    )

            missing_marker = final("missing_marker")
            missing_marker.execute(
                "DELETE FROM wahojobs_schema_migrations WHERE version=?",
                (MIGRATION_VERSION,),
            )
            missing_marker.commit()
            missing_marker_path = Path(
                missing_marker.execute("PRAGMA database_list").fetchone()[2]
            )
            missing_marker.close()
            self._assert_taxonomy_path(
                missing_marker_path,
                "partial_inconsistent",
                "migration_005_partial_inconsistent",
            )

            statements = list(iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8")))
            one_table = m004("one_rebuilt_table")
            one_table.execute("BEGIN IMMEDIATE")
            one_table.execute("PRAGMA defer_foreign_keys = ON")
            for statement in statements[:20]:
                one_table.execute(statement)
            one_table.execute(
                "INSERT INTO wahojobs_schema_migrations(version) VALUES (?)",
                (MIGRATION_VERSION,),
            )
            one_table.commit()
            one_table_path = Path(one_table.execute("PRAGMA database_list").fetchone()[2])
            one_table.close()
            self._assert_taxonomy_path(
                one_table_path,
                "partial_inconsistent",
                "migration_005_partial_inconsistent",
            )

            conflict_definitions = {
                "unexpected_table": "CREATE TABLE product_profile_m005_unexpected(value TEXT)",
                "unexpected_view": "CREATE VIEW current_product_profile_m005_unexpected AS SELECT 1 AS value",
                "unexpected_index": "CREATE INDEX idx_product_profiles_m005_unexpected ON product_profiles(created_at)",
                "unexpected_trigger": "CREATE TRIGGER trg_product_profiles_m005_unexpected AFTER INSERT ON product_profiles BEGIN SELECT 1; END",
            }
            for name, statement in conflict_definitions.items():
                with self.subTest(category="conflicting", name=name):
                    conn = final(name)
                    conn.execute(statement)
                    conn.commit()
                    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
                    conn.close()
                    self._assert_taxonomy_path(
                        path, "conflicting", "migration_005_conflicting"
                    )

            wrong_type = final("wrong_type")
            wrong_type.execute("DROP VIEW current_product_profiles")
            wrong_type.execute("CREATE TABLE current_product_profiles(profile_id TEXT)")
            wrong_type.commit()
            wrong_type_path = Path(wrong_type.execute("PRAGMA database_list").fetchone()[2])
            wrong_type.close()
            self._assert_taxonomy_path(
                wrong_type_path, "conflicting", "migration_005_conflicting"
            )

            multiple = final("multiple_conflicts")
            multiple.execute("CREATE TABLE product_profile_m005_unexpected(value TEXT)")
            multiple.execute(
                "CREATE VIEW current_product_profile_m005_unexpected AS SELECT 1 AS value"
            )
            multiple.commit()
            multiple_path = Path(multiple.execute("PRAGMA database_list").fetchone()[2])
            multiple.close()
            self._assert_taxonomy_path(
                multiple_path, "conflicting", "migration_005_conflicting"
            )

            mismatch_mutations = {
                "weakened_v2_check": (
                    "CHECK (canonical_schema_version = 'canonical_profile_v2')",
                    "CHECK (canonical_schema_version IN ('canonical_profile_v1', 'canonical_profile_v2'))",
                ),
                "weakened_required_field": (
                    "json_type(structured_profile_json, '$.schema_version') IS 'text'",
                    "json_type(structured_profile_json, '$.schema_version') IS NOT NULL",
                ),
                "altered_lifecycle_trigger": (
                    "profile lifecycle transition is invalid",
                    "altered lifecycle transition guard",
                ),
                "altered_view": (
                    "revision.revision_kind AS current_revision_kind",
                    "revision.lifecycle_status AS current_revision_kind",
                ),
                "changed_named_index": (
                    "CREATE INDEX idx_product_profile_sources_profile",
                    "CREATE UNIQUE INDEX idx_product_profile_sources_profile",
                ),
                "changed_deferred_fk": (
                    "ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED",
                    "ON DELETE CASCADE",
                ),
                "changed_automatic_index": (
                    "  UNIQUE (profile_id, revision_number),\n",
                    "  UNIQUE (profile_id, revision_number, reason_code),\n",
                ),
            }
            canonical_sql = MIGRATION_PATH.read_text(encoding="utf-8")
            for name, (old, new) in mismatch_mutations.items():
                with self.subTest(category="mismatch", name=name):
                    self.assertIn(old, canonical_sql)
                    conn = m004(name)
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("PRAGMA defer_foreign_keys = ON")
                    for statement in iter_sql_statements(canonical_sql.replace(old, new, 1)):
                        conn.execute(statement)
                    conn.execute(
                        "INSERT INTO wahojobs_schema_migrations(version) VALUES (?)",
                        (MIGRATION_VERSION,),
                    )
                    conn.commit()
                    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
                    conn.close()
                    self._assert_taxonomy_path(
                        path, "schema_mismatch", "migration_005_schema_mismatch"
                    )

            combined = {
                "mismatch_plus_unexpected": (
                    "conflicting",
                    "migration_005_conflicting",
                    (
                        "DROP VIEW current_product_profiles",
                        "CREATE VIEW current_product_profiles AS SELECT profile_id FROM product_profiles",
                        "CREATE TABLE product_profile_m005_unexpected(value TEXT)",
                    ),
                ),
                "mismatch_plus_missing": (
                    "partial_inconsistent",
                    "migration_005_partial_inconsistent",
                    (
                        "DROP VIEW current_product_profiles",
                        "CREATE VIEW current_product_profiles AS SELECT profile_id FROM product_profiles",
                        "DROP TRIGGER trg_product_profile_revisions_insert_guard",
                    ),
                ),
                "missing_plus_unexpected": (
                    "conflicting",
                    "migration_005_conflicting",
                    (
                        "DROP VIEW current_product_profiles",
                        "CREATE TABLE product_profile_m005_unexpected(value TEXT)",
                    ),
                ),
                "backup_plus_mismatch": (
                    "partial_inconsistent",
                    "migration_005_partial_inconsistent",
                    (
                        "DROP VIEW current_product_profiles",
                        "CREATE VIEW current_product_profiles AS SELECT profile_id FROM product_profiles",
                        "CREATE TABLE product_profiles_m005_backup(profile_id TEXT)",
                    ),
                ),
            }
            for name, (attestation_state, cli_state, operations) in combined.items():
                with self.subTest(category="combined", name=name):
                    conn = final(name)
                    for statement in operations:
                        conn.execute(statement)
                    conn.commit()
                    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
                    conn.close()
                    self._assert_taxonomy_path(path, attestation_state, cli_state)

    def test_malformed_nonempty_profile_state_precedes_integrity_and_foreign_keys(self):
        setup_cases = (
            ("isolated_profile", self._insert_isolated_profile),
            ("isolated_revision", self._insert_isolated_revision),
            ("isolated_source", self._insert_isolated_source),
            ("malformed_json", self._insert_malformed_json_revision),
            ("invalid_lifecycle_chain", self._insert_invalid_lifecycle_revision),
        )
        for name, setup in setup_cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                conn = install_persistent_profiles(Path(tmp) / f"{name}.sqlite")
                setup(conn)
                before = database_snapshot(conn)
                result = migration.classify_database(conn)
                self.assertEqual(
                    result["database_state"], "persistent_profile_state_not_empty"
                )
                self.assertEqual(database_snapshot(conn), before)
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM wahojobs_schema_migrations WHERE version=?",
                        (MIGRATION_VERSION,),
                    ).fetchone()[0],
                    0,
                )
                conn.close()

    def test_non_profile_rows_do_not_trigger_profile_nonempty_classification(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = install_persistent_profiles(Path(tmp) / "unrelated.sqlite")
            add_development_principal(conn, "80")
            conn.execute(
                "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                ("Unrelated", "unrelated-m005", "https://example.test/jobs"),
            )
            conn.commit()
            self.assertEqual(migration.classify_database(conn)["database_state"], "pending")

            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "INSERT INTO auth_identities "
                "(auth_identity_id, user_id, provider, provider_subject, verified_email, "
                "email_verified, created_at, last_authenticated_at, disabled_at, "
                "link_idempotency_key, request_fingerprint) "
                "VALUES (?, ?, 'google', ?, NULL, 0, ?, ?, NULL, ?, ?)",
                (
                    stable_id("aid", 80),
                    stable_id("usr", 9980),
                    "subject-9980",
                    timestamp(),
                    timestamp(),
                    "link-9980",
                    "0" * 64,
                ),
            )
            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(
                migration.classify_database(conn)["database_state"],
                "foreign_key_invalid",
            )
            conn.close()

    def test_every_fault_hook_rolls_back_to_exact_m004_state(self):
        statements = list(iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8")))
        points = migration.failure_injection_points(statements)
        self.assertEqual(len(points), 106)
        self.assertEqual(
            migration.failure_injection_accounting(statements),
            {"fault_injection_hook_count": 106, "durable_state_checkpoint_count": 43},
        )
        for point in points:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "fault.sqlite"
                conn = install_persistent_profiles(path)
                before = database_snapshot(conn)

                def fail(current):
                    if current == point:
                        raise RuntimeError("injected migration failure")

                with self.assertRaises(RuntimeError):
                    migration.apply_persistent_profile_canonical_v2_migration(
                        conn, failure_injector=fail
                    )
                self.assertFalse(conn.in_transaction)
                self.assertEqual(database_snapshot(conn), before)
                self.assertEqual(attest_persistent_profile_schema(conn)["state"], "correctly_installed")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM wahojobs_schema_migrations WHERE version=?", (MIGRATION_VERSION,)).fetchone()[0], 0)
                self.assertFalse(any(conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() for name in TEMPORARY_TABLES))
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                conn.close()
                self.assertFalse(Path(str(path) + "-journal").exists())
                self.assertFalse(Path(str(path) + "-wal").exists())
                self.assertFalse(Path(str(path) + "-shm").exists())

    def test_caller_transaction_is_rejected_without_committing_or_erasing_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = install_persistent_profiles(Path(tmp) / "caller.sqlite")
            conn.execute("BEGIN")
            conn.execute(
                "INSERT INTO companies(name,slug,careers_url) VALUES ('Caller', 'caller-m005', 'https://example.test')"
            )
            with self.assertRaises(migration.PersistentProfileCanonicalV2MigrationError):
                migration.apply_persistent_profile_canonical_v2_migration(conn)
            self.assertTrue(conn.in_transaction)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM companies WHERE slug='caller-m005'").fetchone()[0], 1)
            conn.rollback()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM companies WHERE slug='caller-m005'").fetchone()[0], 0)
            conn.close()

    def test_sql_tampering_of_named_and_automatic_indexes_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            named = install_persistent_profiles(Path(tmp) / "named.sqlite")
            migration.apply_persistent_profile_canonical_v2_migration(named)
            named.execute("DROP INDEX idx_product_profile_sources_profile")
            named.execute("CREATE INDEX idx_product_profile_sources_profile ON product_profile_sources(accepted_at, profile_id)")
            named.commit()
            self.assertNotEqual(attest_persistent_profile_canonical_v2_schema(named)["state"], "correctly_installed")
            named.close()

            automatic = install_persistent_profiles(Path(tmp) / "automatic.sqlite")
            changed_sql = MIGRATION_PATH.read_text(encoding="utf-8").replace(
                "  UNIQUE (revision_id, profile_id),\n", "", 1
            )
            automatic.execute("BEGIN IMMEDIATE")
            automatic.execute("PRAGMA defer_foreign_keys = ON")
            for statement in iter_sql_statements(changed_sql):
                automatic.execute(statement)
            automatic.execute("INSERT INTO wahojobs_schema_migrations(version) VALUES (?)", (MIGRATION_VERSION,))
            automatic.commit()
            self.assertNotEqual(attest_persistent_profile_canonical_v2_schema(automatic)["state"], "correctly_installed")
            automatic.close()

    def test_each_security_relevant_final_definition_tamper_is_detected(self):
        canonical = MIGRATION_PATH.read_text(encoding="utf-8")
        mutations = {
            "v2_check": ("CHECK (canonical_schema_version = 'canonical_profile_v2')", "CHECK (canonical_schema_version IN ('canonical_profile_v1', 'canonical_profile_v2'))"),
            "schema_version_presence": ("json_type(structured_profile_json, '$.schema_version') IS 'text'", "json_type(structured_profile_json, '$.schema_version') IS NOT NULL"),
            "identity_object_presence": ("json_type(structured_profile_json, '$.identity') IS 'object'", "json_type(structured_profile_json, '$.identity') IS NOT NULL"),
            "profile_identity_presence": ("json_type(structured_profile_json, '$.identity.profile_id') IS 'text'", "json_type(structured_profile_json, '$.identity.profile_id') IS NOT NULL"),
            "profile_identity_agreement": ("json_extract(structured_profile_json, '$.identity.profile_id') IS profile_id", "json_extract(structured_profile_json, '$.identity.profile_id') IS NOT NULL"),
            "json_version_trigger": ("NEW.canonical_schema_version IS NOT 'canonical_profile_v2'", "NEW.canonical_schema_version IS NULL"),
            "schema_presence_trigger": ("json_type(NEW.structured_profile_json, '$.schema_version') IS NOT 'text'", "json_type(NEW.structured_profile_json, '$.schema_version') IS NULL"),
            "identity_presence_trigger": ("json_type(NEW.structured_profile_json, '$.identity.profile_id') IS NOT 'text'", "json_type(NEW.structured_profile_json, '$.identity.profile_id') IS NULL"),
            "source_enum": ("'user_confirmed_correction', 'confirmed_lifecycle_action'", "'user_confirmed_correction', 'confirmed_lifecycle_action', 'other_source'"),
            "lifecycle_content": ("AND source_content IN (", "AND source_content NOT IN ("),
            "action_agreement": ("source.source_content = (", "source.source_content <> ("),
            "structured_preservation": ("NEW.structured_profile_json IS NOT (", "NEW.structured_profile_json IS ("),
            "source_count_ordinal": ("OR NEW.source_count <> 1", "OR NEW.source_count <> 2"),
            "terminality": ("= 'deletion_requested'\n    OR", "= 'active'\n    OR"),
            "source_sealing": ("WHERE revision.revision_id = NEW.revision_id\n  )", "WHERE revision.revision_id = NEW.revision_id AND 0\n  )"),
            "revision_immutability": ("product profile revisions are immutable", "replacement revision guard"),
            "source_immutability": ("product profile sources are immutable", "replacement source guard"),
            "current_view": ("revision.revision_kind AS current_revision_kind", "revision.lifecycle_status AS current_revision_kind"),
            "deferred_fk": ("ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED", "ON DELETE CASCADE"),
        }
        for name, (old, new) in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                self.assertIn(old, canonical)
                conn = install_persistent_profiles(Path(tmp) / f"{name}.sqlite")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("PRAGMA defer_foreign_keys = ON")
                for statement in iter_sql_statements(canonical.replace(old, new, 1)):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO wahojobs_schema_migrations(version) VALUES (?)",
                    (MIGRATION_VERSION,),
                )
                conn.commit()
                self.assertNotEqual(
                    attest_persistent_profile_canonical_v2_schema(conn)["state"],
                    "correctly_installed",
                )
                conn.close()

    def test_workspace_identity_guard_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace.sqlite"
            conn = install_persistent_profiles(workspace)
            conn.close()
            hard_link = Path(tmp) / "alias.sqlite"
            os.link(workspace, hard_link)
            with mock.patch.object(migration.migration_004, "DB_PATH", workspace):
                code, payload = self._run_main("--db", str(hard_link), "--json")
                self.assertEqual(code, 1)
                self.assertEqual(payload["database_state"], "workspace_database_blocked")

    @staticmethod
    def _insert_isolated_profile(conn):
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TRIGGER trg_product_profiles_insert_guard")
        conn.execute(
            "INSERT INTO product_profiles "
            "(profile_id, principal_id, environment_namespace, initial_revision_id, created_at) "
            "VALUES (?, ?, 'test', ?, ?)",
            (stable_id("prf", 901), stable_id("prn", 901), stable_id("pvr", 901), timestamp()),
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _insert_isolated_revision(conn):
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TRIGGER trg_product_profile_revisions_insert_guard")
        profile_id = stable_id("prf", 902)
        insert_revision(
            conn,
            revision_id=stable_id("pvr", 902),
            profile_id=profile_id,
            principal_id=stable_id("prn", 902),
            environment="test",
            revision_number=1,
            previous_revision_id=None,
            revision_kind="initial",
            lifecycle_status="active",
            structured_json=canonical_document(profile_id),
            created_at=timestamp(),
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _insert_isolated_source(conn):
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TRIGGER trg_product_profile_sources_insert_guard")
        insert_source(
            conn,
            source_id=stable_id("pfs", 903),
            revision_id=stable_id("pvr", 903),
            profile_id=stable_id("prf", 903),
            principal_id=stable_id("prn", 903),
            environment="test",
            source_content="hostile isolated source",
            accepted_at=timestamp(),
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _insert_malformed_json_revision(conn):
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("DROP TRIGGER trg_product_profile_revisions_insert_guard")
        insert_revision(
            conn,
            revision_id=stable_id("pvr", 904),
            profile_id=stable_id("prf", 904),
            principal_id=stable_id("prn", 904),
            environment="test",
            revision_number=1,
            previous_revision_id=None,
            revision_kind="initial",
            lifecycle_status="active",
            structured_json="{",
            created_at=timestamp(),
        )
        conn.commit()
        conn.execute("PRAGMA ignore_check_constraints = OFF")
        conn.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _insert_invalid_lifecycle_revision(conn):
        migration.apply_persistent_profile_canonical_v2_migration(conn)
        conn.execute(
            "DELETE FROM wahojobs_schema_migrations WHERE version=?",
            (MIGRATION_VERSION,),
        )
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute("DROP TRIGGER trg_product_profile_revisions_insert_guard")
        profile_id = stable_id("prf", 905)
        document = canonical_v2_document(profile_id)
        insert_revision_v2(
            conn,
            revision_id=stable_id("pvr", 905),
            profile_id=profile_id,
            principal_id=stable_id("prn", 905),
            environment="test",
            revision_number=1,
            previous_revision_id=None,
            revision_kind="archive",
            lifecycle_status="active",
            structured_json=document,
            created_at=timestamp(),
        )
        conn.commit()
        conn.execute("PRAGMA ignore_check_constraints = OFF")
        conn.execute("PRAGMA foreign_keys = ON")

    def _run_main(self, *arguments):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", [str(ROOT / "scripts" / "persistent_profile_canonical_v2_migration.py"), *arguments]), contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            migration.main()
        return raised.exception.code, json.loads(output.getvalue())

    def _run_main_text(self, *arguments):
        output = io.StringIO()
        with mock.patch.object(
            sys,
            "argv",
            [str(ROOT / "scripts" / "persistent_profile_canonical_v2_migration.py"), *arguments],
        ), contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            migration.main()
        return raised.exception.code, output.getvalue()

    def _assert_taxonomy_path(self, path, attestation_state, database_state):
        before = (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        conn = migration.migration_004.connect(path, read_only=True)
        try:
            attestation = attest_persistent_profile_canonical_v2_schema(conn)
            direct = migration.classify_database(conn)
        finally:
            conn.close()

        self.assertEqual(attestation["state"], attestation_state)
        self.assertIn(attestation_state, attestation["finding_categories"])
        self.assertTrue(attestation["blocking"])
        self.assertFalse(attestation["applicable"])
        self.assertEqual(direct["database_state"], database_state)

        json_code, json_payload = self._run_main("--db", str(path), "--json")
        self.assertEqual(json_code, 0)
        self.assertEqual(json_payload["database_state"], database_state)
        self.assertEqual(
            json_payload["schema_attestation"]["state"], attestation_state
        )

        text_code, text_output = self._run_main_text("--db", str(path))
        self.assertEqual(text_code, 0)
        self.assertIn(f"Database state: {database_state}", text_output)

        after = (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
