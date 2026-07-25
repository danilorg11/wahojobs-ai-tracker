from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scripts.google_oidc_authorization_transactions_migration as migration
from tests.persistent_profile_canonical_v2_test_support import (
    install_canonical_v2_profiles,
)
from wahojobs.google_oidc_authorization_transaction_schema import (
    EXPECTED_SCHEMA_FINGERPRINT,
    MIGRATION_PATH,
    MIGRATION_VERSION,
    PREREQUISITE_MIGRATION_VERSIONS,
    TRANSACTION_COLUMNS,
    TRANSACTION_INDEXES,
    TRANSACTION_TABLE,
    TRANSACTION_TRIGGERS,
    attest_google_oidc_authorization_transaction_schema,
    expected_google_oidc_authorization_transaction_manifest,
    google_oidc_authorization_transaction_schema_fingerprint,
    iter_sql_statements,
)
from wahojobs.persistent_profile_canonical_v2_schema import (
    attest_persistent_profile_canonical_v2_schema,
)


ROOT = Path(__file__).resolve().parent.parent
CREATED_AT = "2026-07-24T03:00:00+00:00"
EXPIRES_AT = "2026-07-24T03:10:00+00:00"


class GoogleOidcAuthorizationTransactionsMigrationTests(unittest.TestCase):
    def setUp(self):
        super().setUp()

        def deny_socket(*_args, **_kwargs):
            raise AssertionError("live_socket_access_forbidden")

        for attribute in ("socket", "create_connection", "getaddrinfo"):
            patcher = mock.patch.object(socket, attribute, deny_socket)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_pending_apply_exact_install_and_idempotent_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration.sqlite"
            conn = install_canonical_v2_profiles(path)
            before = _preserved_snapshot(conn)

            pending = migration.classify_database(conn)
            self.assertEqual(pending["database_state"], "pending")
            self.assertTrue(pending["applicable"])

            result = migration.apply_google_oidc_authorization_transactions_migration(
                conn
            )
            self.assertEqual(result["database_state"], "migrated")
            self.assertTrue(result["changed"])
            self.assertEqual(result["authorization_transaction_count"], 0)
            self.assertEqual(result["empty_reconciliation_status"], "clean")
            self.assertFalse(conn.in_transaction)
            self.assertEqual(_preserved_snapshot(conn), before)

            installed = migration.classify_database(conn)
            self.assertEqual(installed["database_state"], "exact_installed")
            self.assertFalse(installed["applicable"])
            second = migration.apply_google_oidc_authorization_transactions_migration(
                conn
            )
            self.assertEqual(second["database_state"], "exact_installed")
            self.assertFalse(second["changed"])
            self.assertFalse(conn.in_transaction)
            conn.close()

            reopened = migration.verify_committed_database_read_only(path)
            self.assertEqual(reopened["database_state"], "exact_installed")
            self.assertEqual(reopened["empty_reconciliation_status"], "clean")
            self.assertEqual(reopened["schema_fingerprint"], EXPECTED_SCHEMA_FINGERPRINT)
            self.assertEqual(migration.existing_sqlite_sidecars(path), ())

    def test_exact_columns_indexes_triggers_marker_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = install_canonical_v2_profiles(Path(tmp) / "schema.sqlite")
            migration.apply_google_oidc_authorization_transactions_migration(conn)

            columns = conn.execute(
                f'PRAGMA table_xinfo("{TRANSACTION_TABLE}")'
            ).fetchall()
            self.assertEqual(tuple(row[1] for row in columns), TRANSACTION_COLUMNS)
            self.assertEqual(len(columns), 18)
            self.assertEqual(tuple(row[2] for row in columns), (
                "TEXT",
                "INTEGER",
                "TEXT",
                "TEXT",
                "BLOB",
                "INTEGER",
                "INTEGER",
                "BLOB",
                "TEXT",
                "TEXT",
                "TEXT",
                "TEXT",
                "TEXT",
                "INTEGER",
                "INTEGER",
                "INTEGER",
                "BLOB",
                "BLOB",
            ))
            self.assertEqual(tuple(row[3] for row in columns), (
                1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 1, 1, 1, 1, 1
            ))
            self.assertEqual(conn.execute(
                f'PRAGMA foreign_key_list("{TRANSACTION_TABLE}")'
            ).fetchall(), [])

            indexes = {
                row[1]: (row[2], row[3], row[4])
                for row in conn.execute(f'PRAGMA index_list("{TRANSACTION_TABLE}")')
            }
            self.assertEqual(set(TRANSACTION_INDEXES).issubset(indexes), True)
            self.assertEqual(
                indexes["uq_google_oidc_authorization_transactions_state_lookup"],
                (1, "c", 0),
            )
            self.assertEqual(
                indexes["uq_google_oidc_authorization_transactions_protection_nonce"],
                (1, "c", 0),
            )
            self.assertEqual(
                indexes["idx_google_oidc_authorization_transactions_prepared_expiry"],
                (0, "c", 1),
            )
            self.assertEqual(
                indexes["idx_google_oidc_authorization_transactions_terminal_cleanup"],
                (0, "c", 1),
            )
            triggers = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    f"AND tbl_name='{TRANSACTION_TABLE}'"
                )
            }
            self.assertEqual(triggers, set(TRANSACTION_TRIGGERS))
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM wahojobs_schema_migrations WHERE version=?",
                    (MIGRATION_VERSION,),
                ).fetchone()[0],
                1,
            )

            attestation = attest_google_oidc_authorization_transaction_schema(conn)
            self.assertEqual(attestation["state"], "correctly_installed")
            self.assertTrue(attestation["marker_lineage_valid"])
            self.assertEqual(attestation["missing_prerequisite_migrations"], [])
            self.assertEqual(
                attestation["present_migration_versions"],
                [*PREREQUISITE_MIGRATION_VERSIONS, MIGRATION_VERSION],
            )
            self.assertEqual(
                google_oidc_authorization_transaction_schema_fingerprint(conn),
                EXPECTED_SCHEMA_FINGERPRINT,
            )
            self.assertEqual(
                expected_google_oidc_authorization_transaction_manifest()["fingerprint"],
                EXPECTED_SCHEMA_FINGERPRINT,
            )
            self.assertEqual(attestation["temporary_owned_objects"], [])
            conn.close()

    def test_storage_value_and_unique_constraints_are_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = install_canonical_v2_profiles(Path(tmp) / "constraints.sqlite")
            migration.apply_google_oidc_authorization_transactions_migration(conn)
            _insert_transaction(conn, _valid_row(1))
            conn.commit()

            invalid_cases = {
                "transaction_identifier": {"transaction_id": "oidctx_" + "g" * 32},
                "record_version": {"record_version": 2},
                "provider": {"provider": "other"},
                "environment": {"environment_namespace": "Test"},
                "fingerprint_storage": {"configuration_fingerprint": "c" * 32},
                "fingerprint_length": {"configuration_fingerprint": b"c" * 31},
                "state_digest_version": {"state_digest_version": 2},
                "lookup_version_zero": {"lookup_key_version": 0},
                "digest_storage": {"state_lookup_digest": "d" * 32},
                "digest_length": {"state_lookup_digest": b"d" * 31},
                "created_time": {"created_at": "2026-07-24T03:00:00Z"},
                "expiry_interval": {"expires_at": "2026-07-24T03:10:01+00:00"},
                "lifecycle": {"lifecycle": "claimed"},
                "row_version": {"row_version": 2},
                "envelope_version": {"protection_envelope_version": 2},
                "protection_version_zero": {"protection_key_version": 0},
                "nonce_storage": {"protection_nonce": "n" * 12},
                "nonce_length": {"protection_nonce": b"n" * 11},
                "ciphertext_storage": {"protected_material": "x" * 17},
                "ciphertext_short": {"protected_material": b"x" * 16},
                "ciphertext_long": {"protected_material": b"x" * 529},
            }
            for offset, (name, replacements) in enumerate(
                invalid_cases.items(), start=10
            ):
                with self.subTest(name=name):
                    values = {**_valid_row(offset), **replacements}
                    with self.assertRaises(sqlite3.IntegrityError):
                        _insert_transaction(conn, values)
                    conn.rollback()
                    self.assertFalse(conn.in_transaction)

            duplicate_digest = _valid_row(100)
            duplicate_digest["state_lookup_digest"] = _valid_row(1)[
                "state_lookup_digest"
            ]
            with self.assertRaises(sqlite3.IntegrityError):
                _insert_transaction(conn, duplicate_digest)
            conn.rollback()
            duplicate_nonce = _valid_row(101)
            duplicate_nonce["protection_nonce"] = _valid_row(1)["protection_nonce"]
            with self.assertRaises(sqlite3.IntegrityError):
                _insert_transaction(conn, duplicate_nonce)
            conn.rollback()
            self.assertEqual(
                conn.execute(f'SELECT COUNT(*) FROM "{TRANSACTION_TABLE}"').fetchone()[0],
                1,
            )
            conn.close()

    def test_update_guard_allows_only_one_prepared_to_terminal_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = install_canonical_v2_profiles(Path(tmp) / "transitions.sqlite")
            migration.apply_google_oidc_authorization_transactions_migration(conn)
            _insert_transaction(conn, _valid_row(1))
            conn.commit()

            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "identity is immutable"
            ):
                conn.execute(
                    f'UPDATE "{TRANSACTION_TABLE}" SET provider="other" '
                    "WHERE transaction_id=?",
                    (_valid_row(1)["transaction_id"],),
                )
            conn.rollback()
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "transition is invalid"
            ):
                conn.execute(
                    f'UPDATE "{TRANSACTION_TABLE}" SET row_version=1 '
                    "WHERE transaction_id=?",
                    (_valid_row(1)["transaction_id"],),
                )
            conn.rollback()
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "cannot be deleted"
            ):
                conn.execute(
                    f'DELETE FROM "{TRANSACTION_TABLE}" WHERE transaction_id=?',
                    (_valid_row(1)["transaction_id"],),
                )
            conn.rollback()

            claimed = "2026-07-24T03:04:00+00:00"
            conn.execute(
                f'UPDATE "{TRANSACTION_TABLE}" '
                "SET lifecycle='consumed', claimed_at=?, terminal_at=?, row_version=2 "
                "WHERE transaction_id=?",
                (claimed, claimed, _valid_row(1)["transaction_id"]),
            )
            conn.commit()
            with self.assertRaisesRegex(
                sqlite3.IntegrityError, "transition is invalid"
            ):
                conn.execute(
                    f'UPDATE "{TRANSACTION_TABLE}" SET lifecycle="invalidated" '
                    "WHERE transaction_id=?",
                    (_valid_row(1)["transaction_id"],),
                )
            conn.rollback()
            conn.execute(
                f'DELETE FROM "{TRANSACTION_TABLE}" WHERE transaction_id=?',
                (_valid_row(1)["transaction_id"],),
            )
            conn.commit()
            self.assertEqual(
                conn.execute(f'SELECT COUNT(*) FROM "{TRANSACTION_TABLE}"').fetchone()[0],
                0,
            )
            conn.close()

    def test_expired_and_invalidated_terminal_chronology(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = install_canonical_v2_profiles(Path(tmp) / "chronology.sqlite")
            migration.apply_google_oidc_authorization_transactions_migration(conn)
            _insert_transaction(conn, _valid_row(1))
            _insert_transaction(conn, _valid_row(2))
            conn.commit()

            conn.execute(
                f'UPDATE "{TRANSACTION_TABLE}" '
                "SET lifecycle='expired', terminal_at=?, row_version=2 "
                "WHERE transaction_id=?",
                (EXPIRES_AT, _valid_row(1)["transaction_id"]),
            )
            conn.execute(
                f'UPDATE "{TRANSACTION_TABLE}" '
                "SET lifecycle='invalidated', terminal_at=created_at, row_version=2 "
                "WHERE transaction_id=?",
                (_valid_row(2)["transaction_id"],),
            )
            conn.commit()
            rows = conn.execute(
                f'SELECT lifecycle, claimed_at, row_version FROM "{TRANSACTION_TABLE}" '
                "ORDER BY transaction_id"
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in rows],
                [("expired", None, 2), ("invalidated", None, 2)],
            )
            conn.close()

    def test_attestation_taxonomy_covers_lineage_partial_conflict_drift_and_residue(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)

            missing_lineage = install_canonical_v2_profiles(
                directory / "lineage.sqlite"
            )
            missing_lineage.execute(
                "DELETE FROM wahojobs_schema_migrations WHERE version=?",
                ("005_persistent_profile_canonical_v2",),
            )
            missing_lineage.commit()
            self.assertEqual(
                attest_google_oidc_authorization_transaction_schema(
                    missing_lineage
                )["state"],
                "invalid_prerequisite",
            )
            missing_lineage.close()

            unexpected_marker = install_canonical_v2_profiles(
                directory / "unexpected-marker.sqlite"
            )
            unexpected_marker.execute(
                "INSERT INTO wahojobs_schema_migrations(version) VALUES (?)",
                ("999_unreviewed",),
            )
            unexpected_marker.commit()
            unexpected_report = (
                attest_google_oidc_authorization_transaction_schema(
                    unexpected_marker
                )
            )
            unexpected_marker.close()
            self.assertEqual(unexpected_report["state"], "invalid_prerequisite")
            self.assertEqual(
                unexpected_report["unexpected_migration_versions"],
                ["999_unreviewed"],
            )

            forged = install_canonical_v2_profiles(directory / "forged.sqlite")
            forged.execute(
                "INSERT INTO wahojobs_schema_migrations(version) VALUES (?)",
                (MIGRATION_VERSION,),
            )
            forged.commit()
            self.assertEqual(
                attest_google_oidc_authorization_transaction_schema(forged)["state"],
                "partial_inconsistent",
            )
            self.assertEqual(migration.classify_database(forged)["database_state"], "partial")
            forged.close()

            partial = install_canonical_v2_profiles(directory / "partial.sqlite")
            first_statement = next(
                iter(iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8")))
            )
            partial.execute(first_statement)
            partial.commit()
            self.assertEqual(
                attest_google_oidc_authorization_transaction_schema(partial)["state"],
                "partial_inconsistent",
            )
            partial.close()

            conflict = install_canonical_v2_profiles(directory / "conflict.sqlite")
            conflict.execute(f"CREATE VIEW {TRANSACTION_TABLE} AS SELECT 1 AS value")
            conflict.commit()
            self.assertEqual(
                attest_google_oidc_authorization_transaction_schema(conflict)["state"],
                "conflicting",
            )
            self.assertEqual(
                migration.classify_database(conflict)["database_state"], "conflicting"
            )
            conflict.close()

            drift = install_canonical_v2_profiles(directory / "drift.sqlite")
            changed = MIGRATION_PATH.read_text(encoding="utf-8").replace(
                "length(protected_material) BETWEEN 17 AND 528",
                "length(protected_material) BETWEEN 17 AND 529",
                1,
            )
            drift.execute("BEGIN IMMEDIATE")
            for statement in iter_sql_statements(changed):
                drift.execute(statement)
            drift.execute(
                "INSERT INTO wahojobs_schema_migrations(version) VALUES (?)",
                (MIGRATION_VERSION,),
            )
            drift.commit()
            self.assertEqual(
                attest_google_oidc_authorization_transaction_schema(drift)["state"],
                "schema_mismatch",
            )
            self.assertEqual(migration.classify_database(drift)["database_state"], "drifted")
            drift.close()

            residue = install_canonical_v2_profiles(directory / "residue.sqlite")
            residue.execute(
                "CREATE TEMP TABLE google_oidc_authorization_transaction_residue(value TEXT)"
            )
            report = attest_google_oidc_authorization_transaction_schema(residue)
            self.assertEqual(report["state"], "residue")
            self.assertEqual(report["finding_categories"], ["residue"])
            self.assertEqual(
                report["temporary_owned_objects"],
                ["temp:table:google_oidc_authorization_transaction_residue"],
            )
            residue.close()

    def test_prerequisite_closure_gates_migration_inspection_apply_and_postcommit(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            pending_path = directory / "prerequisite-closure-pending.sqlite"
            pending = install_canonical_v2_profiles(pending_path)
            pending.executescript(
                "CREATE TABLE audit_migration_namespace_target(value TEXT); "
                'CREATE INDEX "IdX_UsEr_PiPeLiNe_StAtE_'
                'migration_inspection" '
                "ON audit_migration_namespace_target(value);"
            )
            pending.commit()
            before = _database_snapshot(pending)

            classification = migration.classify_database(pending)
            self.assertEqual(
                classification["database_state"],
                "invalid_prerequisite",
            )
            self.assertFalse(classification["applicable"])
            with self.assertRaises(
                migration.GoogleOidcAuthorizationTransactionsMigrationError
            ):
                migration.apply_google_oidc_authorization_transactions_migration(
                    pending
                )
            self.assertEqual(_database_snapshot(pending), before)
            self.assertEqual(
                pending.execute(
                    "SELECT COUNT(*) FROM wahojobs_schema_migrations "
                    "WHERE version=?",
                    (MIGRATION_VERSION,),
                ).fetchone()[0],
                0,
            )
            pending.close()

            installed_path = directory / "prerequisite-closure-installed.sqlite"
            installed = install_canonical_v2_profiles(installed_path)
            migration.apply_google_oidc_authorization_transactions_migration(
                installed
            )
            installed.executescript(
                "CREATE TABLE audit_postcommit_namespace_target(value TEXT); "
                'CREATE TRIGGER "TrG_UsEr_PiPeLiNe_StAtE_'
                'postcommit_verification" '
                "AFTER INSERT ON audit_postcommit_namespace_target "
                "BEGIN SELECT 1; END;"
            )
            installed.commit()
            installed.close()

            with self.assertRaises(
                migration.GoogleOidcAuthorizationTransactionsMigrationError
            ):
                migration.verify_committed_database_read_only(installed_path)

    def test_every_fault_hook_rolls_back_to_exact_m005_state(self):
        points = migration.failure_injection_points()
        self.assertEqual(
            migration.failure_injection_accounting()["fault_injection_hook_count"],
            len(points),
        )
        self.assertEqual(
            len(points),
            18
            + 2
            * len(
                list(
                    iter_sql_statements(
                        MIGRATION_PATH.read_text(encoding="utf-8")
                    )
                )
            ),
        )
        for point in points:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "fault.sqlite"
                conn = install_canonical_v2_profiles(path)
                before = _database_snapshot(conn)

                def fail(current):
                    if current == point:
                        raise RuntimeError("injected migration failure")

                with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
                    migration.apply_google_oidc_authorization_transactions_migration(
                        conn, failure_injector=fail
                    )
                self.assertFalse(conn.in_transaction)
                self.assertEqual(_database_snapshot(conn), before)
                self.assertEqual(
                    attest_persistent_profile_canonical_v2_schema(conn)["state"],
                    "correctly_installed",
                )
                self.assertEqual(
                    attest_google_oidc_authorization_transaction_schema(conn)["state"],
                    "pending",
                )
                conn.close()
                self.assertEqual(migration.existing_sqlite_sidecars(path), ())

    def test_caller_transaction_and_foreign_keys_disabled_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = install_canonical_v2_profiles(Path(tmp) / "caller.sqlite")
            conn.execute("BEGIN")
            conn.execute(
                "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                ("Caller", "caller-m006", "https://example.test/jobs"),
            )
            with self.assertRaises(
                migration.GoogleOidcAuthorizationTransactionsMigrationError
            ):
                migration.apply_google_oidc_authorization_transactions_migration(conn)
            self.assertTrue(conn.in_transaction)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM companies WHERE slug='caller-m006'"
                ).fetchone()[0],
                1,
            )
            conn.rollback()
            conn.execute("PRAGMA foreign_keys = OFF")
            with self.assertRaises(
                migration.GoogleOidcAuthorizationTransactionsMigrationError
            ):
                migration.apply_google_oidc_authorization_transactions_migration(conn)
            self.assertFalse(conn.in_transaction)
            conn.close()

    def test_migration_connection_reenables_and_reverifies_recursive_triggers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "recursive-trigger-contract.sqlite"
            conn = install_canonical_v2_profiles(path)
            conn.execute("PRAGMA recursive_triggers = OFF")
            observed = []

            def weaken_after_lock(point):
                if point == "after_locked_prerequisite_attestation":
                    conn.execute("PRAGMA recursive_triggers = OFF")
                    observed.append(
                        conn.execute("PRAGMA recursive_triggers").fetchone()[0]
                    )

            result = migration.apply_google_oidc_authorization_transactions_migration(
                conn,
                failure_injector=weaken_after_lock,
            )
            self.assertTrue(result["changed"])
            self.assertEqual(observed, [0])
            self.assertEqual(
                conn.execute("PRAGMA recursive_triggers").fetchone()[0],
                1,
            )
            conn.execute("PRAGMA recursive_triggers = OFF")
            no_op = migration.apply_google_oidc_authorization_transactions_migration(
                conn
            )
            self.assertFalse(no_op["changed"])
            self.assertEqual(
                conn.execute("PRAGMA recursive_triggers").fetchone()[0],
                1,
            )
            conn.close()

    def test_cli_inspection_is_read_only_and_missing_or_sidecar_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            missing = directory / "missing.sqlite"
            code, payload = self._run_main("--db", str(missing), "--json")
            self.assertEqual(code, 1)
            self.assertEqual(payload["database_state"], "nonexistent")
            self.assertFalse(missing.exists())

            path = directory / "inspect.sqlite"
            conn = install_canonical_v2_profiles(path)
            conn.close()
            before = _file_snapshot(path)
            code, payload = self._run_main("--db", str(path), "--json")
            self.assertEqual(code, 0)
            self.assertEqual(payload["database_state"], "pending")
            self.assertEqual(payload["mode"], "inspection")
            self.assertFalse(payload["changed"])
            self.assertEqual(_file_snapshot(path), before)

            sidecar = Path(str(path) + "-wal")
            sidecar.write_bytes(b"guard")
            code, payload = self._run_main("--db", str(path), "--json")
            self.assertEqual(code, 1)
            self.assertEqual(payload["database_state"], "sqlite_sidecar_present")
            self.assertEqual(payload["sidecar_suffixes"], ["-wal"])

    def test_cli_workspace_apply_requires_and_verifies_external_exact_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            workspace = directory / "workspace.sqlite"
            conn = install_canonical_v2_profiles(workspace)
            conn.close()
            backup_directory = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, backup_directory, True)
            backup = backup_directory / "verified.sqlite"
            shutil.copyfile(workspace, backup)

            with mock.patch.object(migration.migration_004, "DB_PATH", workspace):
                code, payload = self._run_main("--db", str(workspace), "--json")
                self.assertEqual(code, 1)
                self.assertEqual(payload["database_state"], "workspace_database_blocked")

                code, payload = self._run_main(
                    "--db",
                    str(workspace),
                    "--allow-workspace-db",
                    "--yes",
                    "--json",
                )
                self.assertEqual(code, 1)
                self.assertEqual(
                    payload["database_state"], "verified_external_backup_required"
                )

                code, payload = self._run_main(
                    "--db",
                    str(workspace),
                    "--allow-workspace-db",
                    "--yes",
                    "--verified-backup",
                    str(backup),
                    "--json",
                )
                self.assertEqual(code, 0)
                self.assertEqual(payload["database_state"], "migrated")
                self.assertTrue(payload["verified_external_backup"]["verified"])
                self.assertEqual(
                    payload["verified_external_backup"]["sha256"],
                    hashlib.sha256(backup.read_bytes()).hexdigest(),
                )
                self.assertEqual(
                    payload["post_commit_read_only_verification"]["database_state"],
                    "exact_installed",
                )

    def test_workspace_backup_race_matrix_executes_zero_m006_statements(self):
        race_points = migration.workspace_backup_race_points()
        self.assertEqual(
            race_points,
            (
                "after_preliminary_backup_validation",
                "before_target_open",
                "after_target_open",
                "before_locked_target_reverification",
            ),
        )
        for point in race_points:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                workspace = directory / "workspace-race.sqlite"
                conn = install_canonical_v2_profiles(workspace)
                conn.close()
                backup_directory = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, backup_directory, True)
                backup = backup_directory / "workspace-race-backup.sqlite"
                shutil.copyfile(workspace, backup)
                changed = False
                migration_statements = []

                def tracked_connect(*args, **kwargs):
                    connection = sqlite3.connect(*args, **kwargs)

                    def trace(statement):
                        normalized = " ".join(statement.split()).upper()
                        if (
                            normalized.startswith("CREATE ")
                            and "GOOGLE_OIDC_AUTHORIZATION_TRANSACTION"
                            in normalized
                        ) or (
                            normalized.startswith(
                                "INSERT INTO WAHOJOBS_SCHEMA_MIGRATIONS"
                            )
                            and MIGRATION_VERSION.upper() in normalized
                        ):
                            migration_statements.append(statement)

                    connection.set_trace_callback(trace)
                    return connection

                def race(current):
                    nonlocal changed
                    if current == point and not changed:
                        stat_result = workspace.stat()
                        os.utime(
                            workspace,
                            ns=(
                                stat_result.st_atime_ns,
                                stat_result.st_mtime_ns + 10_000_000,
                            ),
                        )
                        changed = True

                with mock.patch.object(migration.migration_004, "DB_PATH", workspace):
                    code, payload = self._run_main(
                        "--db",
                        str(workspace),
                        "--allow-workspace-db",
                        "--yes",
                        "--verified-backup",
                        str(backup),
                        "--json",
                        failure_injector=race,
                        _connect=tracked_connect,
                    )
                self.assertTrue(changed)
                self.assertEqual(code, 1)
                self.assertFalse(payload["changed"])
                self.assertEqual(migration_statements, [])
                inspection = sqlite3.connect(workspace)
                try:
                    self.assertEqual(
                        inspection.execute(
                            "SELECT COUNT(*) FROM sqlite_master "
                            "WHERE name LIKE 'google_oidc_authorization_transaction%'"
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        inspection.execute(
                            "SELECT COUNT(*) FROM wahojobs_schema_migrations "
                            "WHERE version=?",
                            (MIGRATION_VERSION,),
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    inspection.close()

    def test_workspace_live_serialized_image_rejects_owned_uncommitted_changes(self):
        def change_text(connection):
            connection.execute(
                "UPDATE companies SET name='Bravo' WHERE slug='live-backup'"
            )

        def change_blob(connection):
            connection.execute(
                "UPDATE backup_image_probe SET payload=? WHERE probe_id=1",
                (b"B" * 32,),
            )

        def replace_row_preserving_count(connection):
            company_id = connection.execute(
                "SELECT id FROM companies WHERE slug='live-backup'"
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM companies WHERE id=?",
                (company_id,),
            )
            connection.execute(
                "INSERT INTO companies(id, name, slug, careers_url) "
                "VALUES (?, 'Delta', 'live-backup', 'https://example.test/jobs')",
                (company_id,),
            )

        def change_row_outside_preserved_counts(connection):
            connection.execute(
                "UPDATE wahojobs_schema_migrations "
                "SET applied_at='2000-01-01 00:00:00' "
                "WHERE version='001_pipeline_state'"
            )

        cases = {
            "same_length_text_update": change_text,
            "same_length_blob_update": change_blob,
            "delete_insert_same_count": replace_row_preserving_count,
            "excluded_marker_metadata_update": change_row_outside_preserved_counts,
        }
        for name, mutate in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                workspace, backup = _workspace_and_exact_backup(
                    directory,
                    suffix=name,
                )
                before_bytes = workspace.read_bytes()
                before_logical = _live_backup_logical_snapshot(workspace)
                owned = [None]
                serialize_calls = 0
                mutated = False

                def tracked_connect(*args, **kwargs):
                    writable = "mode=rw" in str(args[0])
                    connection = sqlite3.connect(*args, **kwargs)
                    if writable:
                        self.assertEqual(
                            connection.execute(
                                "PRAGMA journal_mode=MEMORY"
                            ).fetchone()[0],
                            "memory",
                        )
                        owned[0] = connection
                    return connection

                original_fingerprint = (
                    migration._serialized_main_database_fingerprint
                )

                def fingerprint_with_owned_mutation(connection):
                    nonlocal mutated, serialize_calls
                    serialize_calls += 1
                    if serialize_calls == 2:
                        self.assertIsNotNone(owned[0])
                        self.assertTrue(owned[0].in_transaction)
                        sqlite3.Connection.set_authorizer(
                            owned[0],
                            None,
                        )
                        mutate(owned[0])
                        mutated = True
                    return original_fingerprint(connection)

                with mock.patch.object(
                    migration.migration_004,
                    "DB_PATH",
                    workspace,
                ), mock.patch.object(
                    migration,
                    "_serialized_main_database_fingerprint",
                    side_effect=fingerprint_with_owned_mutation,
                ):
                    code, payload = self._run_main(
                        "--db",
                        str(workspace),
                        "--allow-workspace-db",
                        "--yes",
                        "--verified-backup",
                        str(backup),
                        "--json",
                        _connect=tracked_connect,
                    )
                self.assertTrue(mutated)
                self.assertEqual(serialize_calls, 2)
                self.assertEqual(code, 1)
                self.assertFalse(payload["changed"])
                rendered = json.dumps(payload, sort_keys=True)
                self.assertNotIn("Alpha", rendered)
                self.assertNotIn((b"A" * 32).hex(), rendered)
                self.assertNotIn(str(workspace), rendered)
                self.assertEqual(workspace.read_bytes(), before_bytes)
                self.assertEqual(
                    _live_backup_logical_snapshot(workspace),
                    before_logical,
                )
                inspection = sqlite3.connect(workspace)
                try:
                    self.assertEqual(
                        inspection.execute(
                            "SELECT COUNT(*) FROM wahojobs_schema_migrations "
                            "WHERE version=?",
                            (MIGRATION_VERSION,),
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    inspection.close()

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            workspace, backup = _workspace_and_exact_backup(
                directory,
                suffix="human-live-mismatch",
            )
            before_bytes = workspace.read_bytes()
            before_logical = _live_backup_logical_snapshot(workspace)
            owned = [None]
            serialize_calls = 0

            def tracked_human_connect(*args, **kwargs):
                writable = "mode=rw" in str(args[0])
                connection = sqlite3.connect(*args, **kwargs)
                if writable:
                    connection.execute("PRAGMA journal_mode=MEMORY")
                    owned[0] = connection
                return connection

            original_fingerprint = (
                migration._serialized_main_database_fingerprint
            )

            def fingerprint_with_human_mutation(connection):
                nonlocal serialize_calls
                serialize_calls += 1
                if serialize_calls == 2:
                    sqlite3.Connection.set_authorizer(owned[0], None)
                    owned[0].execute(
                        "UPDATE companies SET name='Bravo' "
                        "WHERE slug='live-backup'"
                    )
                return original_fingerprint(connection)

            output = io.StringIO()
            argv = [
                str(
                    ROOT
                    / "scripts"
                    / "google_oidc_authorization_transactions_migration.py"
                ),
                "--db",
                str(workspace),
                "--allow-workspace-db",
                "--yes",
                "--verified-backup",
                str(backup),
            ]
            with mock.patch.object(
                migration.migration_004,
                "DB_PATH",
                workspace,
            ), mock.patch.object(
                migration,
                "_serialized_main_database_fingerprint",
                side_effect=fingerprint_with_human_mutation,
            ), mock.patch.object(sys, "argv", argv), (
                contextlib.redirect_stdout(output)
            ), self.assertRaises(SystemExit) as raised:
                migration.main(
                    _connect=tracked_human_connect,
                )
            rendered = output.getvalue()
            self.assertEqual(raised.exception.code, 1)
            self.assertIn("Changed: no", rendered)
            self.assertIn("failed before commit", rendered)
            self.assertNotIn("Alpha", rendered)
            self.assertNotIn("Bravo", rendered)
            self.assertNotIn(str(workspace), rendered)
            self.assertEqual(serialize_calls, 2)
            self.assertEqual(workspace.read_bytes(), before_bytes)
            self.assertEqual(
                _live_backup_logical_snapshot(workspace),
                before_logical,
            )

    def test_workspace_business_data_mutation_timing_matrix(self):
        cases = (
            (
                "after_preliminary_before_target_open",
                "after_preliminary_backup_validation",
                "external",
            ),
            (
                "after_open_before_begin_immediate",
                "after_target_open",
                "owned",
            ),
            (
                "after_begin_before_live_serialization",
                "before_operation_1_",
                "owned",
            ),
        )
        for name, checkpoint, mutation_owner in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                workspace, backup = _workspace_and_exact_backup(
                    directory,
                    suffix=name,
                )
                before_bytes = workspace.read_bytes()
                before_logical = _live_backup_logical_snapshot(workspace)
                expected_external_bytes = [None]
                expected_external_logical = [None]
                owned = [None]
                serialize_calls = 0

                def tracked_connect(*args, **kwargs):
                    writable = "mode=rw" in str(args[0])
                    connection = sqlite3.connect(*args, **kwargs)
                    if writable:
                        connection.execute("PRAGMA journal_mode=MEMORY")
                        owned[0] = connection
                    return connection

                original_fingerprint = (
                    migration._serialized_main_database_fingerprint
                )

                def track_fingerprint(connection):
                    nonlocal serialize_calls
                    serialize_calls += 1
                    return original_fingerprint(connection)

                mutated = False

                def mutate_business_data(point):
                    nonlocal mutated
                    matches = (
                        point.startswith(checkpoint)
                        if checkpoint.endswith("_")
                        else point == checkpoint
                    )
                    if not matches or mutated:
                        return
                    if mutation_owner == "external":
                        external = sqlite3.connect(workspace)
                        try:
                            external.execute(
                                "UPDATE companies SET name='Echo' "
                                "WHERE slug='live-backup'"
                            )
                            external.commit()
                        finally:
                            external.close()
                        expected_external_bytes[0] = workspace.read_bytes()
                        expected_external_logical[0] = (
                            _live_backup_logical_snapshot(workspace)
                        )
                    else:
                        self.assertIsNotNone(owned[0])
                        sqlite3.Connection.set_authorizer(
                            owned[0],
                            None,
                        )
                        owned[0].execute(
                            "UPDATE companies SET name='Echo' "
                            "WHERE slug='live-backup'"
                        )
                        self.assertTrue(owned[0].in_transaction)
                    mutated = True

                with mock.patch.object(
                    migration.migration_004,
                    "DB_PATH",
                    workspace,
                ), mock.patch.object(
                    migration,
                    "_serialized_main_database_fingerprint",
                    side_effect=track_fingerprint,
                ):
                    code, payload = self._run_main(
                        "--db",
                        str(workspace),
                        "--allow-workspace-db",
                        "--yes",
                        "--verified-backup",
                        str(backup),
                        "--json",
                        failure_injector=mutate_business_data,
                        _connect=tracked_connect,
                    )
                self.assertTrue(mutated)
                self.assertEqual(code, 1)
                self.assertFalse(payload["changed"])
                rendered = json.dumps(payload, sort_keys=True)
                self.assertNotIn("Alpha", rendered)
                self.assertNotIn("Echo", rendered)
                self.assertNotIn(str(workspace), rendered)
                if mutation_owner == "external":
                    self.assertEqual(
                        workspace.read_bytes(),
                        expected_external_bytes[0],
                    )
                    self.assertEqual(
                        _live_backup_logical_snapshot(workspace),
                        expected_external_logical[0],
                    )
                    self.assertNotEqual(workspace.read_bytes(), before_bytes)
                    self.assertNotEqual(
                        _live_backup_logical_snapshot(workspace),
                        before_logical,
                    )
                    self.assertEqual(serialize_calls, 0)
                else:
                    self.assertEqual(workspace.read_bytes(), before_bytes)
                    self.assertEqual(
                        _live_backup_logical_snapshot(workspace),
                        before_logical,
                    )
                    self.assertEqual(
                        serialize_calls,
                        2 if checkpoint.endswith("_") else 0,
                    )
                inspection = sqlite3.connect(workspace)
                try:
                    self.assertEqual(
                        inspection.execute(
                            "SELECT COUNT(*) FROM wahojobs_schema_migrations "
                            "WHERE version=?",
                            (MIGRATION_VERSION,),
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    inspection.close()

    def test_workspace_live_backup_evidence_and_serialization_failure_matrix(self):
        cases = (
            "backup_file_changed",
            "backup_evidence_changed",
            "serialize_unavailable",
            "serialize_failed",
            "serialize_wrong_length",
            "clean_exact_copy",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                workspace, backup = _workspace_and_exact_backup(
                    directory,
                    suffix=case,
                )
                before_bytes = workspace.read_bytes()
                before_logical = _live_backup_logical_snapshot(workspace)
                migration_statements = []
                events = []
                serialize_calls = 0

                def tracked_connect(*args, **kwargs):
                    writable = "mode=rw" in str(args[0])
                    connection = sqlite3.connect(*args, **kwargs)
                    if writable:
                        def trace(statement):
                            before = len(migration_statements)
                            _collect_m006_statement(
                                statement,
                                migration_statements,
                            )
                            if len(migration_statements) > before:
                                events.append("m006_statement")

                        connection.set_trace_callback(trace)
                    return connection

                changed_backup = False

                def change_backup_after_preliminary(point):
                    nonlocal changed_backup
                    if (
                        case == "backup_file_changed"
                        and point == "after_preliminary_backup_validation"
                        and not changed_backup
                    ):
                        contents = bytearray(backup.read_bytes())
                        contents[-1] ^= 1
                        backup.write_bytes(contents)
                        contents.clear()
                        changed_backup = True

                evidence_holder = {}
                original_verify = migration.verify_external_backup_evidence

                def capture_evidence(*args, **kwargs):
                    evidence = original_verify(*args, **kwargs)
                    evidence_holder["value"] = evidence
                    return evidence

                def alter_evidence_under_lock(point):
                    events.append(f"hook:{point}")
                    change_backup_after_preliminary(point)
                    if (
                        case == "backup_evidence_changed"
                        and point == "before_locked_target_reverification"
                    ):
                        evidence_holder["value"]["sha256"] = "0" * 64

                original_serialize_fingerprint = (
                    migration._serialized_main_database_fingerprint
                )

                def track_serialize_fingerprint(connection):
                    nonlocal serialize_calls
                    serialize_calls += 1
                    events.append(
                        f"serialize_live_main:{serialize_calls}"
                    )
                    if serialize_calls == 2:
                        if case == "serialize_unavailable":
                            return None
                        if case == "serialize_failed":
                            raise RuntimeError(
                                "raw-serialize-failure "
                                "C:\\sensitive\\workspace.sqlite"
                            )
                        if case == "serialize_wrong_length":
                            size, digest = (
                                original_serialize_fingerprint(connection)
                            )
                            return size - 1, digest
                    return original_serialize_fingerprint(connection)

                with mock.patch.object(
                    migration.migration_004,
                    "DB_PATH",
                    workspace,
                ), mock.patch.object(
                    migration,
                    "verify_external_backup_evidence",
                    side_effect=capture_evidence,
                ), mock.patch.object(
                    migration,
                    "_serialized_main_database_fingerprint",
                    side_effect=track_serialize_fingerprint,
                ):
                    code, payload = self._run_main(
                        "--db",
                        str(workspace),
                        "--allow-workspace-db",
                        "--yes",
                        "--verified-backup",
                        str(backup),
                        "--json",
                        failure_injector=alter_evidence_under_lock,
                        _connect=tracked_connect,
                    )

                if case == "clean_exact_copy":
                    self.assertEqual(code, 0)
                    self.assertTrue(payload["changed"])
                    self.assertEqual(serialize_calls, 2)
                    self.assertEqual(payload["statement_count"], 8)
                    self.assertEqual(migration_statements, [])
                    serialization_index = events.index(
                        "serialize_live_main:2"
                    )
                    self.assertTrue(
                        events[serialization_index + 1].startswith(
                            "hook:after_commit"
                        )
                    )
                    inspection = sqlite3.connect(workspace)
                    try:
                        self.assertEqual(
                            inspection.execute(
                                "SELECT COUNT(*) FROM "
                                "wahojobs_schema_migrations "
                                "WHERE version=?",
                                (MIGRATION_VERSION,),
                            ).fetchone()[0],
                            1,
                        )
                        self.assertEqual(
                            inspection.execute(
                                "SELECT COUNT(*) FROM sqlite_schema "
                                "WHERE name=? OR tbl_name=?",
                                (TRANSACTION_TABLE, TRANSACTION_TABLE),
                            ).fetchone()[0],
                            9,
                        )
                    finally:
                        inspection.close()
                    continue

                self.assertEqual(code, 1)
                self.assertFalse(payload["changed"])
                self.assertEqual(migration_statements, [])
                self.assertEqual(workspace.read_bytes(), before_bytes)
                self.assertEqual(
                    _live_backup_logical_snapshot(workspace),
                    before_logical,
                )
                rendered = json.dumps(payload, sort_keys=True)
                self.assertNotIn("raw-serialize-failure", rendered)
                self.assertNotIn(str(workspace), rendered)
                if case == "backup_file_changed":
                    self.assertTrue(changed_backup)
                    self.assertEqual(serialize_calls, 1)
                elif case == "backup_evidence_changed":
                    self.assertEqual(serialize_calls, 1)
                else:
                    self.assertEqual(serialize_calls, 2)

    def test_sealed_migration_disables_reentrant_callback_matrix(self):
        cases = (
            ("trace_recorder", "before_apply"),
            ("trace_update", "before_final"),
            ("trace_replace_preserving_count", "before_final"),
            ("progress_update", "before_final"),
            ("authorizer_update", "before_begin"),
            ("row_factory_update", "before_final"),
            ("text_factory_update", "before_final"),
        )
        for callback_kind, timing in cases:
            with self.subTest(
                callback=callback_kind,
                timing=timing,
            ), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"{callback_kind}.sqlite"
                connection = install_canonical_v2_profiles(path)
                connection.execute(
                    "INSERT INTO companies(name, slug, careers_url) "
                    "VALUES ('Alpha', 'callback-probe', "
                    "'https://example.test/jobs')"
                )
                company_id = connection.execute(
                    "SELECT id FROM companies "
                    "WHERE slug='callback-probe'"
                ).fetchone()[0]
                connection.commit()
                preserved_before = _preserved_snapshot(connection)
                callback_events = []
                mutation_attempts = []
                installed = False
                sealed_interval = False
                fingerprint_calls = 0

                def attempt_mutation():
                    mutation_attempts.append(callback_kind)
                    try:
                        sqlite3.Connection.set_authorizer(
                            connection,
                            None,
                        )
                        if callback_kind == "trace_replace_preserving_count":
                            sqlite3.Connection.execute(
                                connection,
                                "DELETE FROM companies "
                                f"WHERE id={int(company_id)}",
                            )
                            sqlite3.Connection.execute(
                                connection,
                                "INSERT INTO companies("
                                "id, name, slug, careers_url"
                                ") VALUES ("
                                f"{int(company_id)}, 'Bravo', "
                                "'callback-probe', "
                                "'https://example.test/jobs')",
                            )
                        else:
                            sqlite3.Connection.execute(
                                connection,
                                "UPDATE companies SET name='Bravo' "
                                "WHERE slug='callback-probe'",
                            )
                    except sqlite3.DatabaseError:
                        pass

                def install_callback():
                    nonlocal installed
                    if installed:
                        return
                    installed = True
                    if callback_kind.startswith("trace_"):
                        def trace(statement):
                            normalized = " ".join(
                                statement.split()
                            ).upper()
                            is_m006 = (
                                "GOOGLE_OIDC_AUTHORIZATION_TRANSACTION"
                                in normalized
                                or (
                                    normalized.startswith(
                                        "INSERT INTO "
                                        "WAHOJOBS_SCHEMA_MIGRATIONS"
                                    )
                                    and MIGRATION_VERSION.upper()
                                    in normalized
                                )
                            )
                            if sealed_interval or is_m006:
                                callback_events.append(normalized)
                                if callback_kind != "trace_recorder":
                                    attempt_mutation()

                        sqlite3.Connection.set_trace_callback(
                            connection,
                            trace,
                        )
                    elif callback_kind == "progress_update":
                        def progress():
                            if sealed_interval:
                                callback_events.append("progress")
                                attempt_mutation()
                            return 0

                        sqlite3.Connection.set_progress_handler(
                            connection,
                            progress,
                            1,
                        )
                    elif callback_kind == "authorizer_update":
                        def authorizer(*_arguments):
                            if sealed_interval:
                                callback_events.append("authorizer")
                                attempt_mutation()
                            return sqlite3.SQLITE_OK

                        sqlite3.Connection.set_authorizer(
                            connection,
                            authorizer,
                        )
                    elif callback_kind == "row_factory_update":
                        def row_factory(_cursor, row):
                            if sealed_interval:
                                callback_events.append("row_factory")
                                attempt_mutation()
                            return tuple(row)

                        connection.row_factory = row_factory
                    elif callback_kind == "text_factory_update":
                        def text_factory(value):
                            if sealed_interval:
                                callback_events.append("text_factory")
                                attempt_mutation()
                            return value.decode("utf-8")

                        connection.text_factory = text_factory
                    else:
                        self.fail(f"unknown callback kind: {callback_kind}")

                if timing == "before_apply":
                    install_callback()

                def inject_callback(point):
                    if (
                        timing == "before_begin"
                        and point == "before_begin_immediate"
                    ):
                        install_callback()
                    if (
                        timing == "before_final"
                        and point.startswith("before_operation_1_")
                    ):
                        install_callback()

                original_fingerprint = (
                    migration._serialized_main_database_fingerprint
                )

                def track_final_serialization(current):
                    nonlocal fingerprint_calls, sealed_interval
                    fingerprint_calls += 1
                    if fingerprint_calls == 2:
                        sealed_interval = True
                    return original_fingerprint(current)

                with mock.patch.object(
                    migration,
                    "_serialized_main_database_fingerprint",
                    side_effect=track_final_serialization,
                ):
                    result = (
                        migration
                        .apply_google_oidc_authorization_transactions_migration(
                            connection,
                            failure_injector=inject_callback,
                        )
                    )

                self.assertTrue(installed)
                self.assertEqual(fingerprint_calls, 2)
                self.assertEqual(callback_events, [])
                self.assertEqual(mutation_attempts, [])
                self.assertTrue(result["changed"])
                self.assertEqual(
                    connection.execute(
                        "SELECT name FROM companies "
                        "WHERE slug='callback-probe'"
                    ).fetchone()[0],
                    "Alpha",
                )
                self.assertEqual(
                    _preserved_snapshot(connection),
                    preserved_before,
                )
                self.assertEqual(
                    migration.classify_database(connection)[
                        "database_state"
                    ],
                    "exact_installed",
                )
                connection.close()

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            workspace, backup = _workspace_and_exact_backup(
                directory,
                suffix="preliminary-callback",
            )
            traced_m006 = []

            def callback_connect(*args, **kwargs):
                connection = sqlite3.connect(*args, **kwargs)

                def trace(statement):
                    _collect_m006_statement(statement, traced_m006)

                connection.set_trace_callback(trace)
                return connection

            with mock.patch.object(
                migration.migration_004,
                "DB_PATH",
                workspace,
            ):
                code, payload = self._run_main(
                    "--db",
                    str(workspace),
                    "--allow-workspace-db",
                    "--yes",
                    "--verified-backup",
                    str(backup),
                    "--json",
                    _connect=callback_connect,
                )
            self.assertEqual(code, 0)
            self.assertTrue(payload["changed"])
            self.assertEqual(traced_m006, [])

    def test_seal_clears_callback_finalizer_reinstallations(self):
        for callback_kind in ("trace", "binary_collation"):
            with self.subTest(
                callback=callback_kind,
            ), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"{callback_kind}-finalizer.sqlite"
                connection = install_canonical_v2_profiles(path)
                connection.execute(
                    "INSERT INTO companies(name, slug, careers_url) "
                    "VALUES ('Alpha', 'finalizer-probe', "
                    "'https://example.test/jobs')"
                )
                connection.commit()
                preserved_before = _preserved_snapshot(connection)
                finalizer_events = []
                reinstalled_trace_events = []

                def reinstalled_trace(statement):
                    reinstalled_trace_events.append(statement)
                    try:
                        sqlite3.Connection.set_authorizer(
                            connection,
                            None,
                        )
                        sqlite3.Connection.execute(
                            connection,
                            "UPDATE companies SET name='Bravo' "
                            "WHERE slug='finalizer-probe'",
                        )
                        sqlite3.Connection.commit(connection)
                    except sqlite3.DatabaseError:
                        pass

                class ReinstallingCallback:
                    __slots__ = ("owned_connection",)

                    def __init__(self, owned_connection):
                        self.owned_connection = owned_connection

                    def __call__(self, *arguments):
                        if len(arguments) == 2:
                            left, right = arguments
                            return (left > right) - (left < right)
                        return None

                    def __del__(self):
                        finalizer_events.append(callback_kind)
                        try:
                            sqlite3.Connection.set_trace_callback(
                                self.owned_connection,
                                reinstalled_trace,
                            )
                        except BaseException:
                            pass

                if callback_kind == "trace":
                    sqlite3.Connection.set_trace_callback(
                        connection,
                        ReinstallingCallback(connection),
                    )
                else:
                    sqlite3.Connection.create_collation(
                        connection,
                        "BINARY",
                        ReinstallingCallback(connection),
                    )

                result = (
                    migration
                    .apply_google_oidc_authorization_transactions_migration(
                        connection
                    )
                )
                self.assertTrue(result["changed"])
                self.assertEqual(finalizer_events, [callback_kind])
                self.assertEqual(reinstalled_trace_events, [])
                self.assertEqual(
                    connection.execute(
                        "SELECT name FROM companies "
                        "WHERE slug='finalizer-probe'"
                    ).fetchone()[0],
                    "Alpha",
                )
                self.assertEqual(
                    _preserved_snapshot(connection),
                    preserved_before,
                )
                connection.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "binary-commit-finalizer.sqlite"
            connection = install_canonical_v2_profiles(path)
            connection.execute(
                "INSERT INTO companies(name, slug, careers_url) "
                "VALUES ('Alpha', 'commit-finalizer', "
                "'https://example.test/jobs')"
            )
            connection.commit()
            preserved_before = _preserved_snapshot(connection)
            installed = False
            finalizer_results = []

            class CommitOnRelease:
                __slots__ = ("owned_connection",)

                def __init__(self, owned_connection):
                    self.owned_connection = owned_connection

                def __call__(self, left, right):
                    return (left > right) - (left < right)

                def __del__(self):
                    try:
                        sqlite3.Connection.commit(self.owned_connection)
                    except sqlite3.DatabaseError:
                        finalizer_results.append("blocked")
                    else:
                        finalizer_results.append("committed")

            def install_after_begin(point):
                nonlocal installed
                if (
                    not installed
                    and point.startswith("before_operation_1_")
                ):
                    installed = True
                    sqlite3.Connection.create_collation(
                        connection,
                        "BINARY",
                        CommitOnRelease(connection),
                    )

            result = (
                migration
                .apply_google_oidc_authorization_transactions_migration(
                    connection,
                    failure_injector=install_after_begin,
                )
            )
            self.assertTrue(installed)
            self.assertEqual(finalizer_results, ["blocked"])
            self.assertTrue(result["changed"])
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM companies "
                    "WHERE slug='commit-finalizer'"
                ).fetchone()[0],
                "Alpha",
            )
            self.assertEqual(
                _preserved_snapshot(connection),
                preserved_before,
            )
            connection.close()

    def test_sealed_migration_restores_binary_and_preserves_nocase(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collations.sqlite"
            connection = install_canonical_v2_profiles(path)
            connection.execute(
                "CREATE TABLE audit_names("
                "v TEXT COLLATE NOCASE NOT NULL"
                ")"
            )
            connection.execute(
                "CREATE INDEX idx_audit_names_nocase "
                "ON audit_names(v COLLATE NOCASE)"
            )
            connection.execute(
                "INSERT INTO audit_names(v) VALUES ('Zulu'), ('alpha')"
            )
            connection.commit()
            preserved_before = _preserved_snapshot(connection)
            binary_calls = []
            nocase_calls_after_serialization = []
            final_serialization = False
            fingerprint_calls = 0

            def hostile_binary(left, right):
                binary_calls.append((left, right))
                try:
                    sqlite3.Connection.set_authorizer(connection, None)
                    sqlite3.Connection.execute(
                        connection,
                        "UPDATE audit_names SET v='mutated' "
                        "WHERE v='alpha'",
                    )
                    sqlite3.Connection.commit(connection)
                except sqlite3.DatabaseError:
                    pass
                return (left > right) - (left < right)

            def tracked_nocase(left, right):
                if final_serialization:
                    nocase_calls_after_serialization.append((left, right))
                folded_left = left.lower()
                folded_right = right.lower()
                return (folded_left > folded_right) - (
                    folded_left < folded_right
                )

            connection.create_collation("BINARY", hostile_binary)
            connection.create_collation("NOCASE", tracked_nocase)
            original_fingerprint = (
                migration._serialized_main_database_fingerprint
            )

            def track_final_serialization(current):
                nonlocal fingerprint_calls, final_serialization
                fingerprint_calls += 1
                if fingerprint_calls == 2:
                    final_serialization = True
                return original_fingerprint(current)

            with mock.patch.object(
                migration,
                "_serialized_main_database_fingerprint",
                side_effect=track_final_serialization,
            ):
                result = (
                    migration
                    .apply_google_oidc_authorization_transactions_migration(
                        connection
                    )
                )

            self.assertTrue(result["changed"])
            self.assertEqual(fingerprint_calls, 2)
            self.assertEqual(binary_calls, [])
            self.assertEqual(nocase_calls_after_serialization, [])
            final_serialization = False
            self.assertEqual(
                connection.execute(
                    "SELECT v FROM audit_names ORDER BY v COLLATE NOCASE"
                ).fetchall(),
                [("alpha",), ("Zulu",)],
            )
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone(),
                ("ok",),
            )
            self.assertEqual(
                _preserved_snapshot(connection),
                preserved_before,
            )
            connection.close()

    def test_sealed_migration_avoids_adapter_and_converter_callbacks(self):
        adapters_before = dict(sqlite3.adapters)
        converters_before = dict(sqlite3.converters)
        connection = None
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "adapter-converter.sqlite"
                setup = install_canonical_v2_profiles(path)
                setup.execute(
                    "INSERT INTO companies(name, slug, careers_url) "
                    "VALUES ('Alpha', 'adapter-probe', "
                    "'https://example.test/jobs')"
                )
                setup.commit()
                setup.close()

                connection = sqlite3.connect(
                    path,
                    detect_types=sqlite3.PARSE_DECLTYPES,
                )
                connection.execute("PRAGMA foreign_keys = ON")
                sealed_interval = False
                fingerprint_calls = 0
                adapter_calls_after_serialization = []
                converter_calls_after_serialization = []

                def attempt_mutation():
                    try:
                        sqlite3.Connection.set_authorizer(
                            connection,
                            None,
                        )
                        sqlite3.Connection.execute(
                            connection,
                            "UPDATE companies SET name='Bravo' "
                            "WHERE slug='adapter-probe'",
                        )
                    except sqlite3.DatabaseError:
                        pass

                def str_adapter(value):
                    if sealed_interval:
                        adapter_calls_after_serialization.append(value)
                        attempt_mutation()
                    return value

                def text_converter(value):
                    if sealed_interval:
                        converter_calls_after_serialization.append(value)
                        attempt_mutation()
                    return value.decode("utf-8")

                sqlite3.register_adapter(str, str_adapter)
                sqlite3.register_converter("TEXT", text_converter)
                preserved_before = _preserved_snapshot(connection)
                original_fingerprint = (
                    migration._serialized_main_database_fingerprint
                )

                def track_final_serialization(current):
                    nonlocal fingerprint_calls, sealed_interval
                    fingerprint_calls += 1
                    if fingerprint_calls == 2:
                        sealed_interval = True
                    return original_fingerprint(current)

                with mock.patch.object(
                    migration,
                    "_serialized_main_database_fingerprint",
                    side_effect=track_final_serialization,
                ):
                    result = (
                        migration
                        .apply_google_oidc_authorization_transactions_migration(
                            connection
                        )
                    )

                self.assertTrue(result["changed"])
                self.assertEqual(fingerprint_calls, 2)
                self.assertEqual(
                    adapter_calls_after_serialization,
                    [],
                )
                self.assertEqual(
                    converter_calls_after_serialization,
                    [],
                )
                sealed_interval = False
                self.assertEqual(
                    connection.execute(
                        "SELECT name FROM companies "
                        "WHERE slug='adapter-probe'"
                    ).fetchone()[0],
                    "Alpha",
                )
                self.assertEqual(
                    _preserved_snapshot(connection),
                    preserved_before,
                )
                connection.close()
                connection = None
        finally:
            if connection is not None:
                connection.close()
            sqlite3.adapters.clear()
            sqlite3.adapters.update(adapters_before)
            sqlite3.converters.clear()
            sqlite3.converters.update(converters_before)
        self.assertEqual(sqlite3.adapters, adapters_before)
        self.assertEqual(sqlite3.converters, converters_before)

    def test_failure_to_establish_sealed_state_is_clean_and_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "untrusted-function.sqlite"
            connection = install_canonical_v2_profiles(path)
            before = sqlite3.Connection.serialize(connection, name="main")
            function_calls = []
            connection.create_function(
                "untrusted_callback",
                0,
                lambda: function_calls.append("called") or 1,
            )
            with self.assertRaises(
                migration.GoogleOidcAuthorizationTransactionsMigrationError
            ):
                (
                    migration
                    .apply_google_oidc_authorization_transactions_migration(
                        connection
                    )
                )
            self.assertEqual(function_calls, [])
            self.assertFalse(connection.in_transaction)
            self.assertEqual(
                sqlite3.Connection.serialize(connection, name="main"),
                before,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sqlite_schema "
                    "WHERE name=? OR tbl_name=?",
                    (TRANSACTION_TABLE, TRANSACTION_TABLE),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM wahojobs_schema_migrations "
                    "WHERE version=?",
                    (MIGRATION_VERSION,),
                ).fetchone()[0],
                0,
            )
            connection.close()

        for output_mode in ("json", "human"):
            with self.subTest(
                output=output_mode,
            ), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / f"seal-failure-{output_mode}.sqlite"
                setup = install_canonical_v2_profiles(path)
                before = _database_snapshot(setup)
                setup.close()
                original_seal = migration._seal_migration_connection

                def fail_final_seal(current, *args, **kwargs):
                    if kwargs.get("final"):
                        raise (
                            migration
                            .GoogleOidcAuthorizationTransactionsMigrationError(
                                "raw-seal-failure "
                                "C:\\sensitive\\migration.sqlite"
                            )
                        )
                    return original_seal(current, *args, **kwargs)

                with mock.patch.object(
                    migration,
                    "_seal_migration_connection",
                    side_effect=fail_final_seal,
                ):
                    if output_mode == "json":
                        code, payload = self._run_main(
                            "--db",
                            str(path),
                            "--yes",
                            "--json",
                        )
                        self.assertEqual(code, 1)
                        self.assertFalse(payload["changed"])
                        rendered = json.dumps(payload, sort_keys=True)
                    else:
                        output = io.StringIO()
                        argv = [
                            str(
                                ROOT
                                / "scripts"
                                / (
                                    "google_oidc_authorization_"
                                    "transactions_migration.py"
                                )
                            ),
                            "--db",
                            str(path),
                            "--yes",
                        ]
                        with mock.patch.object(
                            sys,
                            "argv",
                            argv,
                        ), contextlib.redirect_stdout(
                            output
                        ), self.assertRaises(SystemExit) as raised:
                            migration.main()
                        self.assertEqual(raised.exception.code, 1)
                        rendered = output.getvalue()
                        self.assertIn("Changed: no", rendered)

                self.assertNotIn("raw-seal-failure", rendered)
                self.assertNotIn(str(path), rendered)
                inspection = sqlite3.connect(path)
                try:
                    self.assertEqual(_database_snapshot(inspection), before)
                    self.assertEqual(
                        inspection.execute(
                            "SELECT COUNT(*) FROM sqlite_schema "
                            "WHERE name=? OR tbl_name=?",
                            (TRANSACTION_TABLE, TRANSACTION_TABLE),
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        inspection.execute(
                            "SELECT COUNT(*) FROM "
                            "wahojobs_schema_migrations WHERE version=?",
                            (MIGRATION_VERSION,),
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    inspection.close()

    def test_post_commit_failure_matrix_reports_durable_changed_true(self):
        points = migration.post_commit_failure_injection_points()
        self.assertGreaterEqual(len(points), 20)
        for point in points:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "post-commit.sqlite"
                conn = install_canonical_v2_profiles(path)
                conn.close()

                def fail(current):
                    if current == point:
                        raise RuntimeError(
                            "raw-internal C:\\sensitive\\database.sqlite"
                        )

                code, payload = self._run_main(
                    "--db",
                    str(path),
                    "--yes",
                    "--json",
                    failure_injector=fail,
                )
                self.assertEqual(code, 1)
                self.assertTrue(payload["changed"])
                self.assertTrue(payload["durable_commit"])
                self.assertEqual(
                    payload["database_state"],
                    "migrated_verification_failed",
                )
                rendered = json.dumps(payload, sort_keys=True)
                self.assertNotIn("raw-internal", rendered)
                self.assertNotIn(str(path), rendered)
                self.assertNotIn("rollback", rendered.lower())
                reopened = migration.open_canonical_sqlite_database(
                    path,
                    read_only=True,
                )
                try:
                    self.assertEqual(
                        migration.classify_database(reopened)["database_state"],
                        "exact_installed",
                    )
                finally:
                    reopened.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "post-commit-human.sqlite"
            conn = install_canonical_v2_profiles(path)
            conn.close()

            def fail_human(current):
                if current == "post_commit_before_reopen":
                    raise RuntimeError("raw-human-internal-detail")

            output = io.StringIO()
            argv = [
                str(
                    ROOT
                    / "scripts"
                    / "google_oidc_authorization_transactions_migration.py"
                ),
                "--db",
                str(path),
                "--yes",
            ]
            with mock.patch.object(sys, "argv", argv), (
                contextlib.redirect_stdout(output)
            ), self.assertRaises(SystemExit) as raised:
                migration.main(failure_injector=fail_human)
            rendered = output.getvalue()
            self.assertEqual(raised.exception.code, 1)
            self.assertIn("Changed: yes", rendered)
            self.assertIn(
                "commit is durable, but post-commit read-only verification failed",
                rendered,
            )
            self.assertNotIn("raw-human", rendered)
            self.assertNotIn("rollback", rendered.lower())
            code, payload = self._run_main(
                "--db",
                str(path),
                "--yes",
                "--json",
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["database_state"], "exact_installed")
            self.assertFalse(payload["changed"])

    def test_precommit_control_flow_and_rollback_failure_are_sanitized(self):
        control_flow = (KeyboardInterrupt, SystemExit, GeneratorExit)
        for exception_type in control_flow:
            with self.subTest(exception=exception_type.__name__), (
                tempfile.TemporaryDirectory()
            ) as tmp:
                path = Path(tmp) / "control-flow.sqlite"
                conn = install_canonical_v2_profiles(path)
                before = _database_snapshot(conn)

                def interrupt(current):
                    if current == "before_marker_write":
                        raise exception_type("raw-control-flow-detail")

                with self.assertRaises(exception_type):
                    migration.apply_google_oidc_authorization_transactions_migration(
                        conn,
                        failure_injector=interrupt,
                    )
                self.assertFalse(conn.in_transaction)
                self.assertEqual(_database_snapshot(conn), before)
                conn.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ordinary-precommit.sqlite"
            conn = install_canonical_v2_profiles(path)
            conn.close()

            def ordinary_failure(current):
                if current == "before_commit":
                    raise RuntimeError(
                        "raw-precommit C:\\sensitive\\precommit.sqlite"
                    )

            code, payload = self._run_main(
                "--db",
                str(path),
                "--yes",
                "--json",
                failure_injector=ordinary_failure,
            )
            self.assertEqual(code, 1)
            self.assertFalse(payload["changed"])
            self.assertNotIn(
                "raw-precommit",
                json.dumps(payload, sort_keys=True),
            )
            reopened = sqlite3.connect(path)
            try:
                self.assertEqual(
                    reopened.execute(
                        "SELECT COUNT(*) FROM wahojobs_schema_migrations "
                        "WHERE version=?",
                        (MIGRATION_VERSION,),
                    ).fetchone()[0],
                    0,
                )
            finally:
                reopened.close()

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollback-failure.sqlite"
            conn = install_canonical_v2_profiles(path)
            conn.close()

            def fail_before_write(current):
                if current == "before_marker_write":
                    raise RuntimeError(
                        "raw-rollback C:\\sensitive\\rollback.sqlite"
                    )

            direct = sqlite3.connect(path)
            direct.execute("PRAGMA foreign_keys = ON")
            with mock.patch.object(
                migration,
                "_rollback_owned_transaction",
                return_value=False,
            ), self.assertRaises(
                migration.GoogleOidcAuthorizationTransactionsMigrationError
            ) as raised:
                migration.apply_google_oidc_authorization_transactions_migration(
                    direct,
                    failure_injector=fail_before_write,
                )
            self.assertIsNone(raised.exception.__cause__)
            self.assertIsNone(raised.exception.__context__)
            self.assertNotIn("raw-rollback", str(raised.exception))
            if direct.in_transaction:
                direct.rollback()
            direct.close()

            with mock.patch.object(
                migration,
                "_rollback_owned_transaction",
                return_value=False,
            ):
                code, payload = self._run_main(
                    "--db",
                    str(path),
                    "--yes",
                    "--json",
                    failure_injector=fail_before_write,
                )
            self.assertEqual(code, 1)
            self.assertFalse(payload["changed"])
            rendered = json.dumps(payload, sort_keys=True)
            self.assertNotIn("raw-rollback", rendered)
            self.assertNotIn(str(path), rendered)
            self.assertIn("rollback could not be verified", payload["reason"])
            reopened = sqlite3.connect(path)
            try:
                self.assertEqual(
                    reopened.execute(
                        "SELECT COUNT(*) FROM wahojobs_schema_migrations "
                        "WHERE version=?",
                        (MIGRATION_VERSION,),
                    ).fetchone()[0],
                    0,
                )
            finally:
                reopened.close()

    def test_canonical_sqlite_opener_escapes_and_proves_main_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = directory / "literal%23 name # café.sqlite"
            conn = install_canonical_v2_profiles(path)
            conn.close()
            identity = migration.database_file_identity(path)
            uri = migration.sqlite_file_uri(
                path,
                read_only=True,
            )
            self.assertIn("%2523", uri)
            self.assertIn("%20", uri)
            self.assertIn("%23", uri)
            self.assertNotIn("#", uri)
            immutable_uri = migration.sqlite_file_uri(
                path,
                read_only=True,
                immutable=True,
            )
            self.assertIn("mode=ro", immutable_uri)
            self.assertIn("immutable=1", immutable_uri)
            with self.assertRaises(
                migration.GoogleOidcAuthorizationTransactionsMigrationError
            ):
                migration.sqlite_file_uri(
                    path,
                    read_only=False,
                    immutable=True,
                )
            mixed = str(path).replace("\\", "/")
            self.assertEqual(
                migration.canonical_database_path(mixed),
                path.resolve(),
            )
            relative = os.path.relpath(path, ROOT)
            self.assertEqual(
                migration.canonical_database_path(relative),
                path.resolve(),
            )
            opened = migration.open_canonical_sqlite_database(
                path,
                read_only=True,
                expected_identity=identity,
            )
            try:
                self.assertTrue(
                    migration.opened_database_matches(
                        opened,
                        path,
                        identity,
                    )
                )
            finally:
                opened.close()
            code, payload = self._run_main(
                "--db",
                str(path),
                "--yes",
                "--json",
            )
            self.assertEqual(code, 0)
            self.assertTrue(payload["changed"])
            self.assertEqual(
                payload["post_commit_read_only_verification"]["database_state"],
                "exact_installed",
            )

            decoy = directory / "decoy.sqlite"
            decoy_connection = install_canonical_v2_profiles(decoy)
            decoy_connection.close()

            def open_decoy(_uri, **kwargs):
                return sqlite3.connect(
                    decoy,
                    timeout=kwargs.get("timeout", 2.0),
                )

            with self.assertRaises(
                migration.GoogleOidcAuthorizationTransactionsMigrationError
            ):
                migration.open_canonical_sqlite_database(
                    path,
                    read_only=True,
                    expected_identity=identity,
                    connect=open_decoy,
                )
            code, payload = self._run_main(
                "--db",
                str(path),
                "--json",
                _connect=open_decoy,
            )
            self.assertEqual(code, 1)
            self.assertFalse(payload["changed"])
            self.assertNotIn(str(path), json.dumps(payload, sort_keys=True))

            symbolic_alias = directory / "symbolic-alias.sqlite"
            try:
                symbolic_alias.symlink_to(path)
            except OSError:
                symbolic_alias = None
            if symbolic_alias is not None:
                with self.assertRaises(
                    migration.GoogleOidcAuthorizationTransactionsMigrationError
                ):
                    migration.canonical_database_path(symbolic_alias)
            with mock.patch.object(
                migration.stat,
                "S_ISLNK",
                return_value=True,
            ):
                self.assertTrue(
                    migration._path_contains_filesystem_alias(path)
                )
            reparse = mock.Mock(
                st_mode=0,
                st_file_attributes=getattr(
                    migration.stat,
                    "FILE_ATTRIBUTE_REPARSE_POINT",
                    0x400,
                ),
            )
            with mock.patch.object(
                migration.os,
                "lstat",
                return_value=reparse,
            ), mock.patch.object(
                migration.stat,
                "S_ISLNK",
                return_value=False,
            ):
                self.assertTrue(
                    migration._path_contains_filesystem_alias(path)
                )

            hardlink_alias = directory / "hardlink-alias.sqlite"
            try:
                os.link(path, hardlink_alias)
            except OSError:
                hardlink_alias = None
            if hardlink_alias is not None:
                for candidate in (path, hardlink_alias):
                    with self.subTest(hardlink=candidate.name), (
                        self.assertRaises(
                            migration.GoogleOidcAuthorizationTransactionsMigrationError
                        )
                    ):
                        migration.canonical_database_path(candidate)

    def test_import_and_argument_surface_have_no_database_default(self):
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["migration.py", "--help"]), (
            contextlib.redirect_stdout(output)
        ), self.assertRaises(SystemExit) as raised:
            migration.parse_args()
        self.assertEqual(raised.exception.code, 0)
        help_result = output.getvalue()
        self.assertIn("--db DB", help_result)
        self.assertIn("--yes", help_result)
        self.assertIn("--allow-workspace-db", help_result)
        self.assertIn("--verified-backup", help_result)
        self.assertIn("--json", help_result)

    @staticmethod
    def _run_main(
        *arguments,
        failure_injector=None,
        _connect=sqlite3.connect,
    ):
        output = io.StringIO()
        argv = [
            str(
                ROOT
                / "scripts"
                / "google_oidc_authorization_transactions_migration.py"
            ),
            *arguments,
        ]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(
            output
        ), unittest.TestCase().assertRaises(SystemExit) as raised:
            migration.main(
                failure_injector=failure_injector,
                _connect=_connect,
            )
        return raised.exception.code, json.loads(output.getvalue())


def _workspace_and_exact_backup(directory, *, suffix):
    workspace = directory / f"workspace-live-{suffix}.sqlite"
    connection = install_canonical_v2_profiles(workspace)
    connection.execute(
        "INSERT INTO companies(name, slug, careers_url) "
        "VALUES ('Alpha', 'live-backup', 'https://example.test/jobs')"
    )
    connection.execute(
        "CREATE TABLE backup_image_probe("
        "probe_id INTEGER PRIMARY KEY, "
        "payload BLOB NOT NULL CHECK(typeof(payload)='blob' AND length(payload)=32)"
        ")"
    )
    connection.execute(
        "INSERT INTO backup_image_probe(probe_id, payload) VALUES (1, ?)",
        (b"A" * 32,),
    )
    connection.commit()
    connection.close()
    backup = directory / f"external-live-{suffix}.sqlite"
    shutil.copyfile(workspace, backup)
    return workspace, backup


def _live_backup_logical_snapshot(path):
    connection = sqlite3.connect(path)
    try:
        return (
            tuple(
                connection.execute(
                    "SELECT id, name, slug, careers_url FROM companies "
                    "ORDER BY id"
                )
            ),
            tuple(
                connection.execute(
                    "SELECT probe_id, typeof(payload), hex(payload) "
                    "FROM backup_image_probe ORDER BY probe_id"
                )
            ),
            tuple(
                connection.execute(
                    "SELECT version, applied_at "
                    "FROM wahojobs_schema_migrations ORDER BY version"
                )
            ),
        )
    finally:
        connection.close()


def _collect_m006_statement(statement, collected):
    normalized = " ".join(statement.split()).upper()
    if (
        normalized.startswith("CREATE ")
        and "GOOGLE_OIDC_AUTHORIZATION_TRANSACTION" in normalized
    ) or (
        normalized.startswith("INSERT INTO WAHOJOBS_SCHEMA_MIGRATIONS")
        and MIGRATION_VERSION.upper() in normalized
    ):
        collected.append(statement)


def _valid_row(suffix: int) -> dict:
    value = suffix.to_bytes(4, "big")
    return {
        "transaction_id": f"oidctx_{suffix:032x}",
        "record_version": 1,
        "provider": "google",
        "environment_namespace": "test",
        "configuration_fingerprint": b"c" * 28 + value,
        "state_digest_version": 1,
        "lookup_key_version": 1,
        "state_lookup_digest": b"d" * 28 + value,
        "created_at": CREATED_AT,
        "expires_at": EXPIRES_AT,
        "lifecycle": "prepared",
        "claimed_at": None,
        "terminal_at": None,
        "row_version": 1,
        "protection_envelope_version": 1,
        "protection_key_version": 1,
        "protection_nonce": b"n" * 8 + value,
        "protected_material": b"x" * 17,
    }


def _insert_transaction(conn, values):
    conn.execute(
        f'INSERT INTO "{TRANSACTION_TABLE}" ({", ".join(TRANSACTION_COLUMNS)}) '
        f'VALUES ({", ".join("?" for _ in TRANSACTION_COLUMNS)})',
        tuple(values[name] for name in TRANSACTION_COLUMNS),
    )


def _database_snapshot(conn):
    objects = tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger', 'view') "
            "ORDER BY type, name"
        )
    )
    markers = tuple(
        tuple(row)
        for row in conn.execute(
            "SELECT version, applied_at FROM wahojobs_schema_migrations ORDER BY version"
        )
    )
    counts = tuple(
        (
            row[0],
            conn.execute(f'SELECT COUNT(*) FROM "{row[0]}"').fetchone()[0],
        )
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    )
    return objects, markers, counts


def _preserved_snapshot(conn):
    return migration._preserved_database_manifest(conn)


def _file_snapshot(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
