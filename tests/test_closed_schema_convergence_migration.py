from __future__ import annotations

import contextlib
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

import scripts.closed_schema_convergence_migration as migration
from tests.closed_schema_convergence_test_support import (
    CANONICAL_SCHEMA_FINGERPRINT,
    COMPANY_COLUMNS,
    JOB_COLUMNS,
    LEGACY_SCHEMA_FINGERPRINT,
    apply_m007,
    build_fresh_m001_m006,
    build_legacy_m001_m006,
    connect,
    dependent_schema,
    file_snapshot,
    insert_unicode_high_id_graph,
    logical_snapshot,
    migration_markers,
    named_table_rows,
    schema_objects,
    sequence_rows,
)
from wahojobs.closed_schema_authority import (
    CURRENT_CLOSED_SCHEMA_MARKERS,
    capture_closed_schema_identity,
    current_closed_schema_is_exact,
)
from wahojobs.closed_schema_convergence_schema import (
    MIGRATION_PATH,
    MIGRATION_VERSION,
    PREREQUISITE_MIGRATION_VERSIONS,
    iter_sql_statements,
)
from wahojobs.database_lifetime_ownership import (
    ROLE_DURABLE_RUNTIME,
    ROLE_OFFLINE_OPERATOR,
    acquire_database_lifetime_ownership,
    release_database_lifetime_ownership,
)


ROOT = Path(__file__).resolve().parents[1]


class ClosedSchemaConvergenceMigrationTests(unittest.TestCase):
    def setUp(self):
        super().setUp()

        def deny_socket(*_args, **_kwargs):
            raise AssertionError("live_socket_access_forbidden")

        for attribute in ("socket", "create_connection", "getaddrinfo"):
            patcher = mock.patch.object(socket, attribute, deny_socket)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_legacy_rebuild_converges_and_preserves_every_named_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-populated.sqlite"
            connection = build_legacy_m001_m006(path)
            insert_unicode_high_id_graph(connection)

            companies_before = named_table_rows(
                connection, "companies", COMPANY_COLUMNS
            )
            jobs_before = named_table_rows(connection, "jobs", JOB_COLUMNS)
            dependent_before = dependent_schema(connection)
            unrelated_rows_before = _unrelated_table_rows(connection)
            markers_before = migration_markers(connection)
            sequences_before = sequence_rows(connection)
            unrelated_sequences_before = tuple(
                row
                for row in sequences_before
                if row[1] not in {"companies", "jobs"}
            )
            target_sequences_before = {
                row[1]: row[2:] for row in sequences_before if row[1] in {"companies", "jobs"}
            }

            result = apply_m007(connection, path)
            self.assertEqual(result["database_state"], "migrated")
            self.assertEqual(result["migration_action"], "legacy_rebuild")
            self.assertTrue(result["changed"])
            self.assertTrue(result["durable_commit"])
            self.assertEqual(result["schema_attestation"]["state"], "correctly_installed")

            identity = capture_closed_schema_identity(connection)
            self.assertEqual(identity.object_count, 176)
            self.assertEqual(identity.fingerprint, CANONICAL_SCHEMA_FINGERPRINT)
            self.assertEqual(identity.migration_markers, CURRENT_CLOSED_SCHEMA_MARKERS)
            self.assertTrue(current_closed_schema_is_exact(connection))
            self.assertEqual(
                named_table_rows(connection, "companies", COMPANY_COLUMNS),
                companies_before,
            )
            self.assertEqual(
                named_table_rows(connection, "jobs", JOB_COLUMNS),
                jobs_before,
            )
            self.assertEqual(dependent_schema(connection), dependent_before)
            self.assertEqual(
                _unrelated_table_rows(connection),
                unrelated_rows_before,
            )

            markers_after = migration_markers(connection)
            self.assertEqual(markers_after[:6], markers_before)
            self.assertEqual(markers_after[-1][0], MIGRATION_VERSION)
            sequences_after = sequence_rows(connection)
            self.assertEqual(sequences_after, sequences_before)
            self.assertEqual(
                tuple(
                    row
                    for row in sequences_after
                    if row[1] not in {"companies", "jobs"}
                ),
                unrelated_sequences_before,
            )
            self.assertEqual(
                {
                    row[1]: row[2:]
                    for row in sequences_after
                    if row[1] in {"companies", "jobs"}
                },
                target_sequences_before,
            )
            self.assertEqual(
                {
                    (row[2], row[3], row[4])
                    for row in connection.execute("PRAGMA foreign_key_list(jobs)")
                },
                {
                    ("companies", "company_id", "id"),
                    (
                        "canonical_opportunities",
                        "canonical_opportunity_id",
                        "id",
                    ),
                },
            )
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone(), ("ok",))
            self.assertEqual(connection.execute("PRAGMA quick_check(1)").fetchone(), ("ok",))
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM temp.sqlite_schema").fetchone(),
                (0,),
            )
            logical_after = logical_snapshot(connection)
            connection.close()
            file_after = file_snapshot(path)
            reopened = connect(path)
            reopened.row_factory = None
            try:
                replay = apply_m007(reopened, path)
                self.assertEqual(replay["database_state"], "exact_installed")
                self.assertEqual(replay["migration_action"], "none")
                self.assertFalse(replay["changed"])
                self.assertEqual(logical_snapshot(reopened), logical_after)
            finally:
                reopened.close()
            self.assertEqual(file_snapshot(path), file_after)
            self.assertEqual(migration.existing_sqlite_sidecars(path), ())

    def test_empty_legacy_rebuild_preserves_absent_sequence_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy-empty.sqlite"
            connection = build_legacy_m001_m006(path)
            before = tuple(
                row
                for row in sequence_rows(connection)
                if row[1] in {"companies", "jobs"}
            )
            self.assertEqual(before, ())

            result = apply_m007(connection, path)
            self.assertEqual(result["migration_action"], "legacy_rebuild")
            self.assertEqual(
                tuple(
                    row
                    for row in sequence_rows(connection)
                    if row[1] in {"companies", "jobs"}
                ),
                (),
            )
            self.assertEqual(result["table_counts"], {"companies": 0, "jobs": 0})
            connection.close()

    def test_fresh_schema_takes_marker_only_path_and_reapply_is_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fresh.sqlite"
            connection = build_fresh_m001_m006(path)
            schema_before = schema_objects(connection)
            sequence_before = sequence_rows(connection)
            first = apply_m007(connection, path)
            self.assertEqual(first["migration_action"], "marker_only")
            self.assertTrue(first["changed"])
            self.assertEqual(schema_objects(connection), schema_before)
            self.assertEqual(sequence_rows(connection), sequence_before)
            self.assertTrue(current_closed_schema_is_exact(connection))

            before_noop = logical_snapshot(connection)
            connection.close()
            bytes_before_noop = file_snapshot(path)
            reopened = connect(path)
            reopened.row_factory = None
            try:
                second = apply_m007(reopened, path)
                self.assertEqual(second["database_state"], "exact_installed")
                self.assertEqual(second["migration_action"], "none")
                self.assertFalse(second["changed"])
                self.assertEqual(logical_snapshot(reopened), before_noop)
            finally:
                reopened.close()
            self.assertEqual(file_snapshot(path), bytes_before_noop)

    def test_unsupported_object_marker_and_residue_states_fail_closed(self):
        cases = (
            (
                "stale_174",
                build_fresh_m001_m006,
                lambda conn: (
                    conn.execute("DROP INDEX idx_jobs_live_market"),
                    conn.execute("DROP INDEX idx_jobs_canonical_opportunity"),
                ),
                "drifted",
                174,
                "f45e9d4c8c0f487a8437fdf1f5a323010d7c0b56c5d4a61a07ee4fe1f4f53735",
            ),
            (
                "partial_175",
                build_fresh_m001_m006,
                lambda conn: conn.execute("DROP INDEX idx_jobs_live_market"),
                "drifted",
                175,
                None,
            ),
            (
                "extra_object",
                build_legacy_m001_m006,
                lambda conn: conn.execute("CREATE INDEX idx_jobs_m007_unexpected ON jobs(title)"),
                "drifted",
                177,
                None,
            ),
            (
                "wrong_index_definition",
                build_legacy_m001_m006,
                _replace_live_index_with_wrong_definition,
                "drifted",
                176,
                None,
            ),
            (
                "backup_residue",
                build_legacy_m001_m006,
                lambda conn: conn.execute(
                    "CREATE TABLE companies_m007_backup (id INTEGER)"
                ),
                "residue",
                177,
                None,
            ),
            (
                "marker_with_legacy_schema",
                build_legacy_m001_m006,
                lambda conn: conn.execute(
                    "INSERT INTO wahojobs_schema_migrations(version) VALUES (?)",
                    (MIGRATION_VERSION,),
                ),
                "partial",
                176,
                LEGACY_SCHEMA_FINGERPRINT,
            ),
            (
                "missing_prerequisite_marker",
                build_legacy_m001_m006,
                lambda conn: conn.execute(
                    "DELETE FROM wahojobs_schema_migrations "
                    "WHERE version='006_google_oidc_authorization_transactions'"
                ),
                "invalid_prerequisite",
                176,
                LEGACY_SCHEMA_FINGERPRINT,
            ),
            (
                "extra_marker",
                build_legacy_m001_m006,
                lambda conn: conn.execute(
                    "INSERT INTO wahojobs_schema_migrations(version) VALUES ('999_unapproved')"
                ),
                "invalid_prerequisite",
                176,
                LEGACY_SCHEMA_FINGERPRINT,
            ),
        )
        for name, builder, mutate, expected_state, count, fingerprint in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / f"{name}.sqlite"
                connection = builder(path)
                mutate(connection)
                connection.commit()
                identity = capture_closed_schema_identity(connection)
                self.assertEqual(identity.object_count, count)
                if fingerprint is not None:
                    self.assertEqual(identity.fingerprint, fingerprint)
                classification = migration.classify_database(connection)
                self.assertEqual(classification["database_state"], expected_state)
                self.assertFalse(classification["applicable"])
                before = logical_snapshot(connection)
                with self.assertRaises(migration.ClosedSchemaConvergenceMigrationError):
                    apply_m007(connection, path)
                self.assertEqual(logical_snapshot(connection), before)
                connection.close()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "temp-residue.sqlite"
            connection = build_legacy_m001_m006(path)
            connection.execute("CREATE TEMP TABLE m007_residue (value INTEGER)")
            classification = migration.classify_database(connection)
            self.assertEqual(classification["database_state"], "residue")
            self.assertFalse(classification["applicable"])
            connection.close()

    def test_every_orphan_edge_is_rejected_before_mutation(self):
        corruptions = {
            "job_company": (
                "UPDATE jobs SET company_id=999999 WHERE source_hash='sha-two'",
                True,
                "foreign_key_invalid",
            ),
            "job_canonical": (
                "UPDATE jobs SET canonical_opportunity_id=999999 "
                "WHERE source_hash='sha-two'",
                False,
                "data_incompatible",
            ),
            "canonical_company": (
                "UPDATE canonical_opportunities SET company_id=999999 WHERE id=71",
                True,
                "foreign_key_invalid",
            ),
            "crawl_company": (
                "UPDATE crawl_runs SET company_id=999999 WHERE id=81",
                True,
                "foreign_key_invalid",
            ),
            "event_job": (
                "UPDATE job_events SET job_id=999999 WHERE id=91",
                True,
                "foreign_key_invalid",
            ),
            "event_crawl": (
                "UPDATE job_events SET crawl_run_id=999999 WHERE id=91",
                True,
                "foreign_key_invalid",
            ),
        }
        for name, (statement, disable_fks, expected_state) in corruptions.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / f"orphan-{name}.sqlite"
                connection = build_legacy_m001_m006(path)
                insert_unicode_high_id_graph(connection)
                if disable_fks:
                    connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute(statement)
                connection.commit()
                connection.execute("PRAGMA foreign_keys = ON")
                classification = migration.classify_database(connection)
                self.assertEqual(classification["database_state"], expected_state)
                before = logical_snapshot(connection)
                with self.assertRaises(migration.ClosedSchemaConvergenceMigrationError):
                    apply_m007(connection, path)
                self.assertEqual(logical_snapshot(connection), before)
                connection.close()

    def test_cli_help_inspection_apply_and_path_guards(self):
        output = io.StringIO()
        with mock.patch.object(
            sys,
            "argv",
            [str(ROOT / "scripts" / "closed_schema_convergence_migration.py"), "--help"],
        ), contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            migration.main()
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--db", output.getvalue())
        self.assertIn("--yes", output.getvalue())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "cli.sqlite"
            connection = build_fresh_m001_m006(path)
            connection.close()
            before = file_snapshot(path)
            code, payload = self._run_main("--db", str(path), "--json")
            self.assertEqual(code, 0)
            self.assertEqual(payload["database_state"], "canonical_marker_pending")
            self.assertFalse(payload["changed"])
            self.assertEqual(file_snapshot(path), before)

            code, payload = self._run_main(
                "--db", str(path), "--yes", "--json"
            )
            self.assertEqual(code, 0)
            self.assertEqual(payload["database_state"], "migrated")
            self.assertEqual(payload["migration_action"], "marker_only")
            self.assertTrue(payload["changed"])
            reopened = connect(path)
            reopened.row_factory = None
            try:
                self.assertTrue(current_closed_schema_is_exact(reopened))
            finally:
                reopened.close()

            missing = root / "missing.sqlite"
            code, payload = self._run_main("--db", str(missing), "--json")
            self.assertEqual(code, 1)
            self.assertFalse(payload["changed"])
            self.assertFalse(missing.exists())

    def test_cli_apply_preserves_distinct_blocking_state_categories(self):
        cases = (
            (
                "drifted",
                build_fresh_m001_m006,
                lambda connection: connection.execute(
                    "DROP INDEX idx_jobs_live_market"
                ),
            ),
            (
                "partial",
                build_legacy_m001_m006,
                lambda connection: connection.execute(
                    "INSERT INTO wahojobs_schema_migrations(version) VALUES (?)",
                    (MIGRATION_VERSION,),
                ),
            ),
            (
                "data_incompatible",
                build_legacy_m001_m006,
                _insert_canonical_orphan,
            ),
            (
                "residue",
                build_legacy_m001_m006,
                lambda connection: connection.execute(
                    "CREATE TABLE jobs_m007_backup (id INTEGER)"
                ),
            ),
        )
        for expected_state, builder, mutate in cases:
            with self.subTest(state=expected_state), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / f"{expected_state}.sqlite"
                connection = builder(path)
                try:
                    mutate(connection)
                    connection.commit()
                finally:
                    connection.close()
                before = file_snapshot(path)
                code, payload = self._run_main(
                    "--db", str(path), "--yes", "--json"
                )
                self.assertEqual(code, 1)
                self.assertEqual(payload["database_state"], expected_state)
                self.assertFalse(payload["applicable"])
                self.assertFalse(payload["changed"])
                self.assertEqual(file_snapshot(path), before)

    def test_cli_sidecar_hardlink_symlink_and_workspace_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            sidecar_path = root / "sidecar.sqlite"
            connection = build_legacy_m001_m006(sidecar_path)
            connection.close()
            sidecar = Path(str(sidecar_path) + "-wal")
            sidecar.write_bytes(b"synthetic-sidecar")
            before = file_snapshot(sidecar_path)
            code, payload = self._run_main(
                "--db", str(sidecar_path), "--yes", "--json"
            )
            self.assertEqual(code, 1)
            self.assertEqual(payload["database_state"], "sqlite_sidecar_present")
            self.assertEqual(file_snapshot(sidecar_path), before)

            hardlink_source = root / "hardlink-source.sqlite"
            connection = build_fresh_m001_m006(hardlink_source)
            connection.close()
            hardlink = root / "hardlink.sqlite"
            os.link(hardlink_source, hardlink)
            code, payload = self._run_main("--db", str(hardlink), "--json")
            self.assertEqual(code, 1)
            self.assertFalse(payload["changed"])

            workspace_path = root / "workspace-simulated.sqlite"
            connection = build_fresh_m001_m006(workspace_path)
            connection.close()
            workspace_before = file_snapshot(workspace_path)
            workspace_predicate = (
                migration.migration_006.migration_004.is_workspace_database_file
            )
            with mock.patch.object(
                migration.migration_006.migration_004,
                "is_workspace_database_file",
                return_value=True,
            ):
                code, payload = self._run_main(
                    "--db", str(workspace_path), "--yes", "--json"
                )
                self.assertEqual(code, 1)
                self.assertFalse(payload["changed"])
                code, payload = self._run_main(
                    "--db",
                    str(workspace_path),
                    "--yes",
                    "--allow-workspace-db",
                    "--json",
                )
                self.assertEqual(code, 1)
                self.assertFalse(payload["changed"])
            self.assertIsNotNone(workspace_predicate)
            self.assertEqual(file_snapshot(workspace_path), workspace_before)

    def test_cli_rejects_symlink_when_platform_can_create_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite"
            connection = build_fresh_m001_m006(source)
            connection.close()
            alias = root / "alias.sqlite"
            try:
                alias.symlink_to(source)
            except OSError as exc:
                self.skipTest(f"symlink creation unavailable: {type(exc).__name__}")
            code, payload = self._run_main("--db", str(alias), "--json")
            self.assertEqual(code, 1)
            self.assertFalse(payload["changed"])

    def test_workspace_apply_accepts_and_preserves_one_verified_exact_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "workspace.sqlite"
            connection = build_legacy_m001_m006(target)
            source_before = logical_snapshot(connection)
            connection.close()
            backup = root / "workspace-pre-m007-backup.sqlite"
            shutil.copyfile(target, backup)
            backup_before = file_snapshot(backup)

            code, payload = self._run_workspace_apply(target, backup)

            self.assertEqual(code, 0)
            self.assertEqual(payload["database_state"], "migrated")
            self.assertEqual(payload["migration_action"], "legacy_rebuild")
            self.assertTrue(payload["changed"])
            self.assertTrue(payload["durable_commit"])
            self.assertEqual(
                payload["verified_external_backup"]["database_state"],
                "legacy_rebuild_pending",
            )
            self.assertTrue(payload["verified_external_backup"]["verified"])
            self.assertEqual(file_snapshot(backup), backup_before)
            self._assert_logical_snapshot(backup, source_before)
            self._assert_exact_target_without_residue(target)
            self.assertEqual(migration.existing_sqlite_sidecars(target), ())
            self.assertEqual(migration.existing_sqlite_sidecars(backup), ())

    def test_direct_workspace_apply_verifies_backup_path_internally(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "workspace.sqlite"
            connection = build_legacy_m001_m006(target)
            classification = migration.classify_database(connection)
            source_before = logical_snapshot(connection)
            connection.close()
            target_before = file_snapshot(target)
            target_identity = migration.migration_006.database_file_identity(target)
            backup = root / "workspace-pre-m007-backup.sqlite"
            shutil.copyfile(target, backup)
            backup_before = file_snapshot(backup)

            witness = connect(target)
            witness.row_factory = None
            owner = acquire_database_lifetime_ownership(
                target.resolve(strict=True),
                role=ROLE_OFFLINE_OPERATOR,
            )
            try:
                with mock.patch.object(
                    migration.migration_006.migration_004,
                    "is_workspace_database_file",
                    return_value=True,
                ), self.assertRaises(
                    migration.ClosedSchemaConvergenceMigrationError
                ) as raised:
                    migration.apply_closed_schema_convergence_migration(
                        witness,
                        requested_path=target.resolve(strict=True),
                        expected_identity=target_identity,
                        ownership=owner,
                        classification=classification,
                        verified_backup=None,
                    )
            finally:
                witness.close()
                release_database_lifetime_ownership(
                    owner,
                    role=ROLE_OFFLINE_OPERATOR,
                    database_path=target.resolve(strict=True),
                )

            self.assertEqual(
                raised.exception.category,
                "verified_external_backup_invalid",
            )
            self.assertEqual(file_snapshot(target), target_before)
            self.assertEqual(file_snapshot(backup), backup_before)
            self._assert_pending_source_without_residue(target, source_before)

            backup_summary = migration.verify_external_backup_evidence(
                target,
                str(backup),
                expected_target_identity=target_identity,
                expected_database_state=classification["database_state"],
            )
            self.assertEqual(
                backup_summary,
                {
                    "verified": True,
                    "external_to_repository": True,
                    "identity_distinct": True,
                    "size": backup_before[0],
                    "sha256": backup_before[2],
                    "database_state": "legacy_rebuild_pending",
                },
            )
            witness = connect(target)
            witness.row_factory = None
            owner = acquire_database_lifetime_ownership(
                target.resolve(strict=True),
                role=ROLE_OFFLINE_OPERATOR,
            )
            try:
                with mock.patch.object(
                    migration.migration_006.migration_004,
                    "is_workspace_database_file",
                    return_value=True,
                ):
                    result = migration.apply_closed_schema_convergence_migration(
                        witness,
                        requested_path=target.resolve(strict=True),
                        expected_identity=target_identity,
                        ownership=owner,
                        classification=classification,
                        verified_backup=str(backup),
                    )
            finally:
                witness.close()
                release_database_lifetime_ownership(
                    owner,
                    role=ROLE_OFFLINE_OPERATOR,
                    database_path=target.resolve(strict=True),
                )

            self.assertEqual(result["database_state"], "migrated")
            self.assertEqual(result["migration_action"], "legacy_rebuild")
            self.assertTrue(result["durable_commit"])
            self.assertEqual(result["verified_external_backup"], backup_summary)
            self.assertEqual(file_snapshot(backup), backup_before)
            self._assert_logical_snapshot(backup, source_before)
            self._assert_exact_target_without_residue(target)

    def test_invalid_non_sqlite_backup_rejected_by_public_apply_verifier(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "workspace.sqlite"
            connection = build_legacy_m001_m006(target)
            classification = migration.classify_database(connection)
            source_before = logical_snapshot(connection)
            connection.close()
            backup = root / "workspace-backup.sqlite"
            backup.write_bytes(b"not a sqlite database")
            backup_before = file_snapshot(backup)

            self._assert_public_apply_invalid_backup_before_mutation(
                target,
                classification,
                source_before,
                str(backup),
            )
            self.assertEqual(file_snapshot(backup), backup_before)

    def test_caller_objects_cannot_authorize_public_apply(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "workspace.sqlite"
            connection = build_legacy_m001_m006(target)
            classification = migration.classify_database(connection)
            source_before = logical_snapshot(connection)
            connection.close()
            target_identity = migration.migration_006.database_file_identity(target)
            backup = root / "workspace-backup.sqlite"
            shutil.copyfile(target, backup)
            summary = migration.verify_external_backup_evidence(
                target,
                str(backup),
                expected_target_identity=target_identity,
                expected_database_state=classification["database_state"],
            )
            manual_fields = {
                "verified": True,
                "external_to_repository": True,
                "identity_distinct": True,
                "size": summary["size"],
                "sha256": summary["sha256"],
                "database_state": summary["database_state"],
            }

            class OldEvidenceShape:
                __slots__ = ("_public_items", "_authority", "__weakref__")

            old_shape = OldEvidenceShape()
            old_shape._public_items = tuple(summary.items())
            old_shape._authority = object()

            self.assertFalse(hasattr(migration, "_VerifiedBackupEvidence"))
            self.assertFalse(hasattr(migration, "_IssuedBackupEvidenceAuthority"))
            self.assertFalse(hasattr(migration, "_BACKUP_EVIDENCE_ISSUANCE_CAPABILITY"))

            for case, supplied_backup in (
                ("dictionary_from_verifier_output", dict(summary)),
                ("manual_public_fields", manual_fields),
                ("old_verified_backup_evidence_shape", old_shape),
                ("old_authority_value", object()),
                ("arbitrary_object", object()),
            ):
                with self.subTest(case=case):
                    self._assert_public_apply_invalid_backup_before_mutation(
                        target,
                        classification,
                        source_before,
                        supplied_backup,
                    )

    def test_old_privileged_backup_evidence_keyword_is_not_public_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "workspace.sqlite"
            connection = build_legacy_m001_m006(target)
            classification = migration.classify_database(connection)
            source_before = logical_snapshot(connection)
            connection.close()
            target_identity = migration.migration_006.database_file_identity(target)
            target_before = file_snapshot(target)
            backup = root / "workspace-backup.sqlite"
            shutil.copyfile(target, backup)
            summary = migration.verify_external_backup_evidence(
                target,
                str(backup),
                expected_target_identity=target_identity,
                expected_database_state=classification["database_state"],
            )
            witness = connect(target)
            witness.row_factory = None
            owner = acquire_database_lifetime_ownership(
                target.resolve(strict=True),
                role=ROLE_OFFLINE_OPERATOR,
            )
            try:
                with mock.patch.object(
                    migration.migration_006.migration_004,
                    "is_workspace_database_file",
                    return_value=True,
                ), mock.patch.object(
                    migration.migration_006,
                    "_open_exact_private_migration_worker",
                ) as private_worker, self.assertRaises(TypeError):
                    migration.apply_closed_schema_convergence_migration(
                        witness,
                        requested_path=target.resolve(strict=True),
                        expected_identity=target_identity,
                        ownership=owner,
                        classification=classification,
                        backup_evidence=summary,
                    )
            finally:
                witness.close()
                release_database_lifetime_ownership(
                    owner,
                    role=ROLE_OFFLINE_OPERATOR,
                    database_path=target.resolve(strict=True),
                )

            private_worker.assert_not_called()
            self.assertEqual(file_snapshot(target), target_before)
            self._assert_pending_source_without_residue(target, source_before)

    def test_first_locked_backup_seal_rejects_same_identity_content_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "workspace.sqlite"
            connection = build_legacy_m001_m006(target)
            source_before = logical_snapshot(connection)
            connection.close()
            target_before = file_snapshot(target)
            backup = root / "workspace-backup.sqlite"
            shutil.copyfile(target, backup)
            identity_before = migration.migration_006.database_file_identity(backup)
            backup_before = file_snapshot(backup)
            tampered_snapshot = None

            def tamper(point):
                nonlocal tampered_snapshot
                if point == "before_locked_backup_check":
                    self.assertIsNone(tampered_snapshot)
                    tampered_snapshot = _tamper_same_identity_file(backup)

            code, payload = self._run_workspace_apply(
                target,
                backup,
                failure_injector=tamper,
            )

            self.assertIsNotNone(tampered_snapshot)
            self.assertEqual(code, 1)
            self.assertEqual(
                payload["database_state"], "verified_external_backup_invalid"
            )
            self.assertFalse(payload["changed"])
            self.assertEqual(file_snapshot(target), target_before)
            self._assert_pending_source_without_residue(target, source_before)
            self.assertEqual(
                migration.migration_006.database_file_identity(backup),
                identity_before,
            )
            self.assertEqual(file_snapshot(backup), tampered_snapshot)
            self.assertEqual(tampered_snapshot[:2], backup_before[:2])
            self.assertNotEqual(tampered_snapshot[2], backup_before[2])

    def test_first_locked_backup_seal_rejects_replaced_backup_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "workspace.sqlite"
            connection = build_legacy_m001_m006(target)
            source_before = logical_snapshot(connection)
            connection.close()
            target_before = file_snapshot(target)
            backup = root / "workspace-backup.sqlite"
            retained = root / "retained-original-backup.sqlite"
            staged = root / "replacement-backup.sqlite"
            shutil.copyfile(target, backup)
            shutil.copyfile(target, staged)
            original_backup = file_snapshot(backup)
            original_identity = migration.migration_006.database_file_identity(backup)
            replacement_snapshot = None
            replacement_identity = None

            def replace_backup(point):
                nonlocal replacement_identity, replacement_snapshot
                if point == "before_locked_backup_check":
                    os.replace(backup, retained)
                    os.replace(staged, backup)
                    replacement_identity = (
                        migration.migration_006.database_file_identity(backup)
                    )
                    replacement_snapshot = file_snapshot(backup)

            code, payload = self._run_workspace_apply(
                target,
                backup,
                failure_injector=replace_backup,
            )

            self.assertEqual(code, 1)
            self.assertEqual(
                payload["database_state"], "verified_external_backup_invalid"
            )
            self.assertFalse(payload["changed"])
            self.assertIsNotNone(replacement_identity)
            self.assertNotEqual(replacement_identity, original_identity)
            self.assertEqual(file_snapshot(target), target_before)
            self._assert_pending_source_without_residue(target, source_before)
            self.assertEqual(file_snapshot(retained), original_backup)
            self.assertEqual(file_snapshot(backup), replacement_snapshot)

    def test_workspace_backup_with_hardlink_alias_fails_closed_when_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "workspace.sqlite"
            connection = build_legacy_m001_m006(target)
            source_before = logical_snapshot(connection)
            connection.close()
            target_before = file_snapshot(target)
            backup = root / "workspace-backup.sqlite"
            alias = root / "workspace-backup-alias.sqlite"
            shutil.copyfile(target, backup)
            backup_before = file_snapshot(backup)
            try:
                os.link(backup, alias)
            except OSError as exc:
                self.skipTest(f"hardlink creation unavailable: {type(exc).__name__}")

            code, payload = self._run_workspace_apply(target, backup)

            self.assertEqual(code, 1)
            self.assertEqual(
                payload["database_state"], "verified_external_backup_invalid"
            )
            self.assertFalse(payload["changed"])
            self.assertEqual(file_snapshot(target), target_before)
            self._assert_pending_source_without_residue(target, source_before)
            self.assertEqual(file_snapshot(backup), backup_before)
            self.assertEqual(file_snapshot(alias), backup_before)

    def test_workspace_backup_sidecar_is_prohibited_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "workspace.sqlite"
            connection = build_legacy_m001_m006(target)
            source_before = logical_snapshot(connection)
            connection.close()
            target_before = file_snapshot(target)
            backup = root / "workspace-backup.sqlite"
            shutil.copyfile(target, backup)
            backup_before = file_snapshot(backup)
            sidecar = Path(str(backup) + "-wal")
            sidecar.write_bytes(b"synthetic-backup-sidecar-sentinel")
            sidecar_before = sidecar.read_bytes()

            code, payload = self._run_workspace_apply(target, backup)

            self.assertEqual(code, 1)
            self.assertEqual(
                payload["database_state"], "verified_external_backup_invalid"
            )
            self.assertFalse(payload["changed"])
            self.assertEqual(file_snapshot(target), target_before)
            self._assert_pending_source_without_residue(target, source_before)
            self.assertEqual(file_snapshot(backup), backup_before)
            self.assertEqual(sidecar.read_bytes(), sidecar_before)

    def test_backup_content_tamper_after_locked_seals_rolls_back_every_change(self):
        for hook in (
            "after_locked_backup_check",
            "after_final_backup_seal",
            "before_commit",
        ):
            with self.subTest(hook=hook), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "workspace.sqlite"
                connection = build_legacy_m001_m006(target)
                source_before = logical_snapshot(connection)
                connection.close()
                backup = root / "workspace-backup.sqlite"
                shutil.copyfile(target, backup)
                identity_before = migration.migration_006.database_file_identity(
                    backup
                )
                backup_before = file_snapshot(backup)
                tampered_snapshot = None

                def tamper(point):
                    nonlocal tampered_snapshot
                    if point == hook:
                        self.assertIsNone(tampered_snapshot)
                        tampered_snapshot = _tamper_same_identity_file(backup)

                code, payload = self._run_workspace_apply(
                    target,
                    backup,
                    failure_injector=tamper,
                )

                self.assertIsNotNone(tampered_snapshot)
                self.assertEqual(code, 1)
                self.assertEqual(
                    payload["database_state"],
                    "verified_external_backup_invalid",
                )
                self.assertFalse(payload["changed"])
                self.assertIsNot(payload.get("durable_commit"), True)
                self._assert_pending_source_without_residue(target, source_before)
                self.assertEqual(
                    migration.migration_006.database_file_identity(backup),
                    identity_before,
                )
                self.assertEqual(file_snapshot(backup), tampered_snapshot)
                self.assertEqual(tampered_snapshot[:2], backup_before[:2])
                self.assertNotEqual(tampered_snapshot[2], backup_before[2])

    def test_final_backup_seal_rejects_unavailable_or_replaced_backup(self):
        for case in ("unavailable", "replaced"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "workspace.sqlite"
                connection = build_legacy_m001_m006(target)
                source_before = logical_snapshot(connection)
                connection.close()
                backup = root / "workspace-backup.sqlite"
                retained = root / "retained-original-backup.sqlite"
                staged = root / "replacement-backup.sqlite"
                shutil.copyfile(target, backup)
                if case == "replaced":
                    shutil.copyfile(target, staged)
                original_backup = file_snapshot(backup)
                replacement_snapshot = None
                changed = False

                def change_at_final_seal(point):
                    nonlocal changed, replacement_snapshot
                    if point == "before_final_backup_seal":
                        self.assertFalse(changed)
                        os.replace(backup, retained)
                        if case == "replaced":
                            os.replace(staged, backup)
                            replacement_snapshot = file_snapshot(backup)
                        changed = True

                code, payload = self._run_workspace_apply(
                    target,
                    backup,
                    failure_injector=change_at_final_seal,
                )

                self.assertTrue(changed)
                self.assertEqual(code, 1)
                self.assertEqual(
                    payload["database_state"],
                    "verified_external_backup_invalid",
                )
                self.assertFalse(payload["changed"])
                self.assertIsNot(payload.get("durable_commit"), True)
                self._assert_pending_source_without_residue(target, source_before)
                self.assertEqual(file_snapshot(retained), original_backup)
                if case == "unavailable":
                    self.assertFalse(backup.exists())
                else:
                    self.assertEqual(file_snapshot(backup), replacement_snapshot)

    def test_target_replacement_identity_before_locked_validation_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.sqlite"
            connection = build_legacy_m001_m006(target)
            source_before = logical_snapshot(connection)
            connection.close()
            target_before = file_snapshot(target)
            replacement = root / "replacement.sqlite"
            shutil.copyfile(target, replacement)
            replacement_identity = migration.migration_006.database_file_identity(
                replacement
            )
            real_identity = migration.migration_006.database_file_identity
            armed = False
            replacement_observed = False

            def arm_replacement(point):
                nonlocal armed
                if point == "before_private_worker_open":
                    self.assertFalse(armed)
                    armed = True

            def replacement_identity_once(candidate):
                nonlocal replacement_observed
                if (
                    armed
                    and not replacement_observed
                    and Path(candidate) == target.resolve(strict=True)
                ):
                    replacement_observed = True
                    return replacement_identity
                return real_identity(candidate)

            with mock.patch.object(
                migration.migration_006,
                "database_file_identity",
                side_effect=replacement_identity_once,
            ):
                code, payload = self._run_main(
                    "--db",
                    str(target),
                    "--yes",
                    "--json",
                    failure_injector=arm_replacement,
                )

            self.assertTrue(armed)
            self.assertTrue(replacement_observed)
            self.assertEqual(code, 1)
            self.assertEqual(payload["database_state"], "target_changed")
            self.assertFalse(payload["changed"])
            self.assertEqual(file_snapshot(target), target_before)
            self._assert_pending_source_without_residue(target, source_before)

    def test_physical_target_replacement_rejects_sealed_old_target_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "workspace.sqlite"
            retained_old_target = root / "retained-old-workspace.sqlite"
            staged_replacement = root / "staged-replacement.sqlite"
            backup = root / "verified-old-workspace-backup.sqlite"
            connection = build_legacy_m001_m006(target)
            classification = migration.classify_database(connection)
            old_logical = logical_snapshot(connection)
            connection.close()
            old_identity = migration.migration_006.database_file_identity(target)
            old_snapshot = file_snapshot(target)
            shutil.copyfile(target, backup)
            backup_snapshot = file_snapshot(backup)

            shutil.copyfile(target, staged_replacement)
            replacement_connection = connect(staged_replacement)
            replacement_connection.row_factory = None
            replacement_connection.execute(
                "INSERT INTO companies "
                "(name, slug, careers_url, source_tier, inventory_model, "
                "market_count_policy) VALUES "
                "('Replacement Sentinel', 'replacement-sentinel', "
                "'https://example.invalid/replacement', 'experimental', "
                "'public_inventory', 'report_separately')"
            )
            replacement_connection.commit()
            replacement_logical = logical_snapshot(replacement_connection)
            replacement_connection.close()
            staged_snapshot = file_snapshot(staged_replacement)
            self.assertNotEqual(staged_snapshot[2], old_snapshot[2])

            os.replace(target, retained_old_target)
            os.replace(staged_replacement, target)
            replacement_identity = migration.migration_006.database_file_identity(
                target
            )
            self.assertNotEqual(replacement_identity, old_identity)
            replacement_snapshot = file_snapshot(target)
            witness = connect(target)
            owner = acquire_database_lifetime_ownership(
                target.resolve(strict=True),
                role=ROLE_OFFLINE_OPERATOR,
            )
            try:
                with self.assertRaises(
                    migration.ClosedSchemaConvergenceMigrationError
                ) as raised:
                    migration.apply_closed_schema_convergence_migration(
                        witness,
                        requested_path=target.resolve(strict=True),
                        expected_identity=old_identity,
                        ownership=owner,
                        classification=classification,
                        verified_backup=str(backup),
                    )
            finally:
                witness.close()
                release_database_lifetime_ownership(
                    owner,
                    role=ROLE_OFFLINE_OPERATOR,
                    database_path=target.resolve(strict=True),
                )

            self.assertEqual(raised.exception.category, "target_changed")
            self.assertEqual(file_snapshot(retained_old_target), old_snapshot)
            self.assertEqual(file_snapshot(target), replacement_snapshot)
            self.assertEqual(file_snapshot(backup), backup_snapshot)
            self._assert_pending_source_without_residue(
                retained_old_target,
                old_logical,
            )
            self._assert_pending_source_without_residue(target, replacement_logical)

    def test_durable_runtime_ownership_contention_then_clean_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "ownership.sqlite"
            connection = build_legacy_m001_m006(target)
            source_before = logical_snapshot(connection)
            connection.close()
            target_before = file_snapshot(target)
            backup = root / "ownership-backup.sqlite"
            shutil.copyfile(target, backup)
            backup_before = file_snapshot(backup)
            durable_owner = acquire_database_lifetime_ownership(
                target.resolve(strict=True),
                role=ROLE_DURABLE_RUNTIME,
            )
            try:
                code, payload = self._run_workspace_apply(
                    target,
                    backup,
                )
            finally:
                release_database_lifetime_ownership(
                    durable_owner,
                    role=ROLE_DURABLE_RUNTIME,
                    database_path=target.resolve(strict=True),
                )

            self.assertEqual(code, 1)
            self.assertEqual(payload["database_state"], "ownership_contended")
            self.assertFalse(payload["changed"])
            self.assertEqual(file_snapshot(target), target_before)
            self.assertEqual(file_snapshot(backup), backup_before)
            self._assert_pending_source_without_residue(target, source_before)

            code, payload = self._run_workspace_apply(target, backup)
            self.assertEqual(code, 0)
            self.assertEqual(payload["database_state"], "migrated")
            self.assertTrue(payload["durable_commit"])
            self.assertEqual(file_snapshot(backup), backup_before)
            self._assert_logical_snapshot(backup, source_before)
            self._assert_exact_target_without_residue(target)

    def test_competing_sqlite_writer_reports_busy_then_clean_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "writer-busy.sqlite"
            connection = build_legacy_m001_m006(target)
            source_before = logical_snapshot(connection)
            connection.close()
            target_before = file_snapshot(target)
            backup = root / "writer-busy-backup.sqlite"
            shutil.copyfile(target, backup)
            backup_before = file_snapshot(backup)
            writer = sqlite3.connect(target, timeout=0.0, isolation_level=None)
            try:
                writer.execute("BEGIN IMMEDIATE")
                code, payload = self._run_workspace_apply(
                    target,
                    backup,
                )
            finally:
                if writer.in_transaction:
                    writer.rollback()
                writer.close()

            self.assertEqual(code, 1)
            self.assertEqual(payload["database_state"], "database_busy")
            self.assertFalse(payload["changed"])
            self.assertEqual(file_snapshot(target), target_before)
            self.assertEqual(file_snapshot(backup), backup_before)
            self._assert_pending_source_without_residue(target, source_before)

            code, payload = self._run_workspace_apply(target, backup)
            self.assertEqual(code, 0)
            self.assertEqual(payload["database_state"], "migrated")
            self.assertTrue(payload["durable_commit"])
            self.assertEqual(file_snapshot(backup), backup_before)
            self._assert_logical_snapshot(backup, source_before)
            self._assert_exact_target_without_residue(target)

    def test_direct_public_apply_rejects_sidecars_without_touching_exact_target(self):
        for suffix in ("-wal", "-journal"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "exact.sqlite"
                connection = build_fresh_m001_m006(path)
                sidecar = Path(str(path) + suffix)
                try:
                    apply_m007(connection, path)
                    before_logical = logical_snapshot(connection)
                    before_file = file_snapshot(path)
                    sidecar.write_bytes(b"synthetic-sidecar-sentinel")
                    with self.assertRaises(
                        migration.ClosedSchemaConvergenceMigrationError
                    ) as raised:
                        apply_m007(connection, path)
                    self.assertEqual(
                        raised.exception.category,
                        "sqlite_sidecar_present",
                    )
                    self.assertEqual(file_snapshot(path), before_file)
                finally:
                    connection.close()
                    if sidecar.exists():
                        sidecar.unlink()
                reopened = connect(path)
                reopened.row_factory = None
                try:
                    self.assertEqual(logical_snapshot(reopened), before_logical)
                finally:
                    reopened.close()

    def test_every_exported_transaction_hook_rolls_back_before_commit_and_is_durable_after(self):
        statements = tuple(
            iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8"))
        )
        points = migration.failure_injection_points(statements)
        self.assertEqual(len(points), len(set(points)))
        self.assertIn("before_commit", points)
        self.assertEqual(points[-1], "before_commit")

        for point in points:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "fault.sqlite"
                connection = build_legacy_m001_m006(path)
                before = logical_snapshot(connection)
                connection.close()

                def fail(current):
                    if current == point:
                        raise RuntimeError("synthetic-m007-failure")

                code, payload = self._run_main(
                    "--db",
                    str(path),
                    "--yes",
                    "--json",
                    failure_injector=fail,
                )
                self.assertEqual(code, 1)
                reopened = connect(path)
                reopened.row_factory = None
                try:
                    self.assertFalse(payload["changed"])
                    self.assertIsNot(payload.get("durable_commit"), True)
                    self.assertEqual(
                        migration.classify_database(reopened)["database_state"],
                        "legacy_rebuild_pending",
                    )
                    self.assertEqual(logical_snapshot(reopened), before)
                finally:
                    reopened.close()
                self.assertEqual(migration.existing_sqlite_sidecars(path), ())

    def test_every_exported_postcommit_hook_reports_durable_exact_state(self):
        points = migration.post_commit_failure_injection_points()
        self.assertEqual(len(points), len(set(points)))
        self.assertGreaterEqual(len(points), 3)
        for point in points:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "postcommit.sqlite"
                connection = build_fresh_m001_m006(path)
                connection.close()

                def fail(current):
                    if current == point:
                        raise RuntimeError("synthetic-postcommit-failure")

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
                reopened = connect(path)
                reopened.row_factory = None
                try:
                    self.assertTrue(current_closed_schema_is_exact(reopened))
                finally:
                    reopened.close()
                self.assertEqual(migration.existing_sqlite_sidecars(path), ())

    def test_success_observes_every_declared_hook_for_legacy_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observed.sqlite"
            connection = build_legacy_m001_m006(path)
            connection.close()
            observed = []
            code, payload = self._run_main(
                "--db",
                str(path),
                "--yes",
                "--json",
                failure_injector=observed.append,
            )
            self.assertEqual(code, 0)
            self.assertTrue(payload["changed"])
            statements = tuple(
                iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8"))
            )
            self.assertEqual(
                set(observed),
                set(migration.failure_injection_points(statements))
                | set(migration.post_commit_failure_injection_points()),
            )

    def _assert_public_apply_invalid_backup_before_mutation(
        self,
        target,
        classification,
        source_before,
        verified_backup,
    ):
        target_before = file_snapshot(target)
        target_identity = migration.migration_006.database_file_identity(target)
        witness = connect(target)
        witness.row_factory = None
        owner = acquire_database_lifetime_ownership(
            target.resolve(strict=True),
            role=ROLE_OFFLINE_OPERATOR,
        )
        try:
            with mock.patch.object(
                migration.migration_006.migration_004,
                "is_workspace_database_file",
                return_value=True,
            ), mock.patch.object(
                migration.migration_006,
                "_open_exact_private_migration_worker",
            ) as private_worker, self.assertRaises(
                migration.ClosedSchemaConvergenceMigrationError
            ) as raised:
                migration.apply_closed_schema_convergence_migration(
                    witness,
                    requested_path=target.resolve(strict=True),
                    expected_identity=target_identity,
                    ownership=owner,
                    classification=classification,
                    verified_backup=verified_backup,
                )
        finally:
            witness.close()
            release_database_lifetime_ownership(
                owner,
                role=ROLE_OFFLINE_OPERATOR,
                database_path=target.resolve(strict=True),
            )

        self.assertEqual(
            raised.exception.category,
            "verified_external_backup_invalid",
        )
        private_worker.assert_not_called()
        self.assertEqual(file_snapshot(target), target_before)
        self._assert_pending_source_without_residue(target, source_before)

    def _run_workspace_apply(self, target, backup, *, failure_injector=None):
        with mock.patch.object(
            migration.migration_006.migration_004,
            "is_workspace_database_file",
            return_value=True,
        ):
            return self._run_main(
                "--db",
                str(target),
                "--yes",
                "--allow-workspace-db",
                "--verified-backup",
                str(backup),
                "--json",
                failure_injector=failure_injector,
            )

    def _assert_logical_snapshot(self, path, expected):
        connection = connect(path)
        connection.row_factory = None
        try:
            self.assertEqual(logical_snapshot(connection), expected)
        finally:
            connection.close()

    def _assert_pending_source_without_residue(self, path, expected):
        connection = connect(path)
        connection.row_factory = None
        try:
            self.assertEqual(logical_snapshot(connection), expected)
            self.assertEqual(
                migration.classify_database(connection)["database_state"],
                "legacy_rebuild_pending",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM wahojobs_schema_migrations "
                    "WHERE version=?",
                    (MIGRATION_VERSION,),
                ).fetchone(),
                (0,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sqlite_schema "
                    "WHERE name IN ('companies_m007_backup', 'jobs_m007_backup')"
                ).fetchone(),
                (0,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM temp.sqlite_schema"
                ).fetchone(),
                (0,),
            )
        finally:
            connection.close()
        self.assertEqual(migration.existing_sqlite_sidecars(path), ())

    def _assert_exact_target_without_residue(self, path):
        connection = connect(path)
        connection.row_factory = None
        try:
            self.assertTrue(current_closed_schema_is_exact(connection))
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sqlite_schema "
                    "WHERE name IN ('companies_m007_backup', 'jobs_m007_backup')"
                ).fetchone(),
                (0,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM temp.sqlite_schema"
                ).fetchone(),
                (0,),
            )
        finally:
            connection.close()
        self.assertEqual(migration.existing_sqlite_sidecars(path), ())

    def _run_main(self, *arguments, failure_injector=None):
        output = io.StringIO()
        argv = [
            str(ROOT / "scripts" / "closed_schema_convergence_migration.py"),
            *arguments,
        ]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(
            output
        ), self.assertRaises(SystemExit) as raised:
            migration.main(failure_injector=failure_injector)
        return raised.exception.code, json.loads(output.getvalue())

def _replace_live_index_with_wrong_definition(connection):
    connection.execute("DROP INDEX idx_jobs_live_market")
    connection.execute(
        "CREATE INDEX idx_jobs_live_market ON jobs(is_active, include_in_live_market_estimate)"
    )


def _insert_canonical_orphan(connection):
    insert_unicode_high_id_graph(connection)
    connection.execute(
        "UPDATE jobs SET canonical_opportunity_id=9223372036854775000 "
        "WHERE source_hash='sha-two'"
    )


def _tamper_same_identity_file(path):
    before_identity = migration.migration_006.database_file_identity(path)
    before_snapshot = file_snapshot(path)
    metadata = path.stat()
    with path.open("r+b") as handle:
        handle.seek(-1, os.SEEK_END)
        original = handle.read(1)
        if len(original) != 1:
            raise AssertionError("backup_tamper_requires_nonempty_file")
        handle.seek(-1, os.SEEK_END)
        handle.write(bytes((original[0] ^ 1,)))
        handle.flush()
        os.fsync(handle.fileno())
    os.utime(
        path,
        ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
    )
    after_identity = migration.migration_006.database_file_identity(path)
    after_snapshot = file_snapshot(path)
    if after_identity != before_identity:
        raise AssertionError(
            f"backup_tamper_changed_identity:{before_identity!r}:{after_identity!r}"
        )
    if after_snapshot[:2] != before_snapshot[:2]:
        raise AssertionError("backup_tamper_changed_size_or_mtime")
    if after_snapshot[2] == before_snapshot[2]:
        raise AssertionError("backup_tamper_did_not_change_digest")
    return after_snapshot


def _unrelated_table_rows(connection):
    return tuple(
        table
        for table in logical_snapshot(connection)["tables"]
        if table[0]
        not in {
            "companies",
            "jobs",
            "wahojobs_schema_migrations",
        }
    )


if __name__ == "__main__":
    unittest.main()
