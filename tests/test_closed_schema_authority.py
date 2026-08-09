from __future__ import annotations

import sqlite3
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.closed_schema_convergence_test_support import (
    CANONICAL_SCHEMA_FINGERPRINT,
    LEGACY_SCHEMA_FINGERPRINT,
    apply_m007,
    build_fresh_m001_m006,
    build_legacy_m001_m006,
    schema_object_map,
)
from wahojobs import durable_google_login_runtime as runtime
from wahojobs import private_beta_invitation_operations as pb_ops
from wahojobs.closed_schema_authority import (
    CURRENT_CLOSED_SCHEMA_FINGERPRINT,
    CURRENT_CLOSED_SCHEMA_MARKERS,
    CURRENT_CLOSED_SCHEMA_OBJECT_COUNT,
    capture_closed_schema_identity,
    current_closed_schema_is_exact,
)
from wahojobs.closed_schema_convergence_schema import (
    EXPECTED_MIGRATION_VERSIONS,
    LEGACY_SCHEMA_OBJECT_COUNT,
    PREREQUISITE_MIGRATION_VERSIONS,
    attest_closed_schema_convergence_schema,
)


class ClosedSchemaAuthorityTests(unittest.TestCase):
    def setUp(self):
        super().setUp()

        def deny_socket(*_args, **_kwargs):
            raise AssertionError("live_socket_access_forbidden")

        for attribute in ("socket", "create_connection", "getaddrinfo"):
            patcher = mock.patch.object(socket, attribute, deny_socket)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_synthetic_historical_and_fresh_lineages_are_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = build_legacy_m001_m006(root / "legacy.sqlite")
            fresh = build_fresh_m001_m006(root / "fresh.sqlite")
            try:
                legacy_identity = capture_closed_schema_identity(legacy)
                fresh_identity = capture_closed_schema_identity(fresh)
                self.assertEqual(legacy_identity.object_count, 176)
                self.assertEqual(
                    legacy_identity.object_count,
                    LEGACY_SCHEMA_OBJECT_COUNT,
                )
                self.assertEqual(
                    legacy_identity.fingerprint,
                    LEGACY_SCHEMA_FINGERPRINT,
                )
                self.assertEqual(fresh_identity.object_count, 176)
                self.assertEqual(
                    fresh_identity.object_count,
                    CURRENT_CLOSED_SCHEMA_OBJECT_COUNT,
                )
                self.assertEqual(
                    fresh_identity.fingerprint,
                    CANONICAL_SCHEMA_FINGERPRINT,
                )
                self.assertEqual(
                    fresh_identity.fingerprint,
                    CURRENT_CLOSED_SCHEMA_FINGERPRINT,
                )
                self.assertEqual(
                    legacy_identity.migration_markers,
                    PREREQUISITE_MIGRATION_VERSIONS,
                )
                self.assertEqual(
                    fresh_identity.migration_markers,
                    PREREQUISITE_MIGRATION_VERSIONS,
                )
                self.assertFalse(current_closed_schema_is_exact(legacy))
                self.assertFalse(current_closed_schema_is_exact(fresh))
                self.assertEqual(
                    attest_closed_schema_convergence_schema(legacy)["state"],
                    "legacy_rebuild_pending",
                )
                self.assertEqual(
                    attest_closed_schema_convergence_schema(fresh)["state"],
                    "canonical_marker_pending",
                )
            finally:
                legacy.close()
                fresh.close()

    def test_exact_legacy_to_fresh_difference_is_companies_and_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = build_legacy_m001_m006(root / "legacy.sqlite")
            fresh = build_fresh_m001_m006(root / "fresh.sqlite")
            try:
                legacy_objects = schema_object_map(legacy)
                fresh_objects = schema_object_map(fresh)
                self.assertEqual(set(legacy_objects), set(fresh_objects))
                changed = {
                    key
                    for key in legacy_objects
                    if legacy_objects[key] != fresh_objects[key]
                }
                self.assertEqual(
                    changed,
                    {("table", "companies"), ("table", "jobs")},
                )

                self.assertEqual(
                    tuple(
                        row[1]
                        for row in legacy.execute("PRAGMA table_xinfo(companies)")
                    ),
                    (
                        "id",
                        "name",
                        "slug",
                        "careers_url",
                        "created_at",
                        "updated_at",
                        "source_tier",
                        "inventory_model",
                        "market_count_policy",
                    ),
                )
                self.assertEqual(
                    tuple(
                        row[1]
                        for row in fresh.execute("PRAGMA table_xinfo(companies)")
                    ),
                    (
                        "id",
                        "name",
                        "slug",
                        "careers_url",
                        "source_tier",
                        "inventory_model",
                        "market_count_policy",
                        "created_at",
                        "updated_at",
                    ),
                )
                self.assertEqual(
                    tuple(row[1] for row in legacy.execute("PRAGMA table_xinfo(jobs)")),
                    (
                        "id",
                        "company_id",
                        "external_id",
                        "title",
                        "location",
                        "url",
                        "source_hash",
                        "first_seen_at",
                        "last_seen_at",
                        "is_active",
                        "removed_at",
                        "created_at",
                        "updated_at",
                        "department",
                        "commitment",
                        "expertise",
                        "canonical_opportunity_id",
                        "opportunity_kind",
                        "availability_basis",
                        "include_in_live_market_estimate",
                    ),
                )
                self.assertEqual(
                    tuple(row[1] for row in fresh.execute("PRAGMA table_xinfo(jobs)")),
                    (
                        "id",
                        "company_id",
                        "canonical_opportunity_id",
                        "external_id",
                        "title",
                        "location",
                        "department",
                        "expertise",
                        "commitment",
                        "url",
                        "source_hash",
                        "opportunity_kind",
                        "availability_basis",
                        "include_in_live_market_estimate",
                        "first_seen_at",
                        "last_seen_at",
                        "is_active",
                        "removed_at",
                        "created_at",
                        "updated_at",
                    ),
                )
                self.assertEqual(
                    _foreign_key_edges(legacy, table="jobs"),
                    {("jobs", "companies", "company_id", "id")},
                )
                self.assertEqual(
                    _foreign_key_edges(fresh, table="jobs"),
                    {
                        ("jobs", "companies", "company_id", "id"),
                        (
                            "jobs",
                            "canonical_opportunities",
                            "canonical_opportunity_id",
                            "id",
                        ),
                    },
                )
            finally:
                legacy.close()
                fresh.close()

    def test_complete_declared_dependency_graph_and_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = build_fresh_m001_m006(
                Path(directory) / "dependencies.sqlite"
            )
            try:
                edges = _foreign_key_edges(connection)
                relevant = {
                    edge
                    for edge in edges
                    if edge[0] in {"companies", "jobs"}
                    or edge[1] in {"companies", "jobs"}
                }
                self.assertEqual(
                    relevant,
                    {
                        ("jobs", "companies", "company_id", "id"),
                        (
                            "jobs",
                            "canonical_opportunities",
                            "canonical_opportunity_id",
                            "id",
                        ),
                        (
                            "canonical_opportunities",
                            "companies",
                            "company_id",
                            "id",
                        ),
                        ("crawl_runs", "companies", "company_id", "id"),
                        ("job_events", "jobs", "job_id", "id"),
                    },
                )
                company_indexes = _index_details(connection, "companies")
                self.assertEqual(
                    set(company_indexes),
                    {"sqlite_autoindex_companies_1"},
                )
                self.assertEqual(
                    company_indexes["sqlite_autoindex_companies_1"][1],
                    ("slug",),
                )
                job_indexes = _index_details(connection, "jobs")
                self.assertEqual(
                    set(job_indexes),
                    {
                        "sqlite_autoindex_jobs_1",
                        "idx_jobs_company_active",
                        "idx_jobs_first_seen_at",
                        "idx_jobs_last_seen_at",
                        "idx_jobs_live_market",
                        "idx_jobs_canonical_opportunity",
                    },
                )
                self.assertEqual(
                    job_indexes["sqlite_autoindex_jobs_1"][1],
                    ("company_id", "source_hash"),
                )
                self.assertEqual(
                    job_indexes["idx_jobs_company_active"][1],
                    ("company_id", "is_active"),
                )
                self.assertEqual(
                    job_indexes["idx_jobs_live_market"][1],
                    ("include_in_live_market_estimate", "is_active"),
                )
                self.assertEqual(
                    job_indexes["idx_jobs_canonical_opportunity"][1],
                    ("canonical_opportunity_id",),
                )

                trigger_or_view_references = connection.execute(
                    "SELECT type, name FROM sqlite_schema "
                    "WHERE type IN ('trigger','view') AND "
                    "(lower(sql) LIKE '%companies%' OR lower(sql) LIKE '%jobs%')"
                ).fetchall()
                self.assertEqual(trigger_or_view_references, [])
            finally:
                connection.close()

    def test_marker_authority_has_exact_seven_version_contract(self):
        self.assertEqual(CURRENT_CLOSED_SCHEMA_OBJECT_COUNT, 176)
        self.assertEqual(CURRENT_CLOSED_SCHEMA_FINGERPRINT, CANONICAL_SCHEMA_FINGERPRINT)
        self.assertEqual(EXPECTED_MIGRATION_VERSIONS, CURRENT_CLOSED_SCHEMA_MARKERS)
        self.assertEqual(
            CURRENT_CLOSED_SCHEMA_MARKERS,
            (
                "001_pipeline_state",
                "002_accounts_sessions",
                "003_product_principals",
                "004_persistent_product_profiles",
                "005_persistent_profile_canonical_v2",
                "006_google_oidc_authorization_transactions",
                "007_closed_schema_convergence",
            ),
        )

    def test_runtime_and_pb_ops_reject_both_predecessors_then_accept_m007(self):
        for name, builder in (
            ("historical", build_legacy_m001_m006),
            ("fresh", build_fresh_m001_m006),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / f"{name}.sqlite"
                connection = builder(path)
                try:
                    targets = SimpleNamespace(database_path=path.resolve(strict=True))
                    self._assert_both_consumers_reject(connection, targets)

                    apply_m007(connection, path)
                    self.assertIsNone(
                        runtime._attest_closed_database_schema(connection)
                    )
                    self.assertIsNone(pb_ops._attest_database(connection, targets))
                finally:
                    connection.close()

    def test_runtime_and_pb_ops_fail_closed_for_every_schema_and_marker_drift(self):
        canonical_cases = (
            (
                "missing_live_market_helper_index",
                lambda connection: connection.execute(
                    "DROP INDEX idx_jobs_live_market"
                ),
                False,
            ),
            (
                "missing_canonical_opportunity_helper_index",
                lambda connection: connection.execute(
                    "DROP INDEX idx_jobs_canonical_opportunity"
                ),
                False,
            ),
            (
                "unsupported_174_both_helper_indexes_missing",
                _drop_both_helper_indexes,
                False,
            ),
            (
                "unexpected_177th_object",
                lambda connection: connection.execute(
                    "CREATE INDEX idx_jobs_unapproved_177 ON jobs(title)"
                ),
                False,
            ),
            (
                "altered_companies_sql",
                lambda connection: _rewrite_stored_table_sql(
                    connection,
                    table="companies",
                    old="source_tier TEXT NOT NULL DEFAULT 'core'",
                    new="source_tier TEXT NOT NULL DEFAULT 'experimental'",
                ),
                True,
            ),
            (
                "altered_jobs_sql_missing_canonical_fk",
                lambda connection: _rewrite_stored_table_sql(
                    connection,
                    table="jobs",
                    old=(
                        "  FOREIGN KEY (canonical_opportunity_id) "
                        "REFERENCES canonical_opportunities(id),\n"
                    ),
                    new="",
                ),
                True,
            ),
            (
                "missing_m007_marker",
                lambda connection: connection.execute(
                    "DELETE FROM wahojobs_schema_migrations "
                    "WHERE version='007_closed_schema_convergence'"
                ),
                False,
            ),
            (
                "wrong_marker_lineage",
                _replace_prerequisite_marker_with_unapproved_marker,
                False,
            ),
            (
                "main_backup_residue",
                lambda connection: connection.execute(
                    "CREATE TABLE companies_m007_backup (id INTEGER)"
                ),
                False,
            ),
        )
        for name, mutate, reopen in canonical_cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / f"{name}.sqlite"
                connection = build_fresh_m001_m006(path)
                try:
                    apply_m007(connection, path)
                    self.assertTrue(current_closed_schema_is_exact(connection))
                    mutate(connection)
                    connection.commit()
                    if reopen:
                        connection.close()
                        connection = _raw_connection(path)
                    self.assertFalse(current_closed_schema_is_exact(connection))
                    self._assert_both_consumers_reject(
                        connection,
                        SimpleNamespace(database_path=path.resolve(strict=True)),
                    )
                finally:
                    connection.close()

        with self.subTest(name="forged_m007_marker_on_historical_lineage"):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "forged-m007.sqlite"
                connection = build_legacy_m001_m006(path)
                try:
                    connection.execute(
                        "INSERT INTO wahojobs_schema_migrations(version) VALUES (?)",
                        ("007_closed_schema_convergence",),
                    )
                    connection.commit()
                    self.assertFalse(current_closed_schema_is_exact(connection))
                    self._assert_both_consumers_reject(
                        connection,
                        SimpleNamespace(database_path=path.resolve(strict=True)),
                    )
                finally:
                    connection.close()

    def test_pb_ops_rejects_foreign_key_integrity_and_sidecar_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "consumer-integrity.sqlite"
            connection = build_fresh_m001_m006(path)
            sidecar = Path(str(path) + "-wal")
            try:
                apply_m007(connection, path)
                targets = SimpleNamespace(database_path=path.resolve(strict=True))

                integrity_failure = _ConnectionWithQuickCheckFailure(connection)
                with self.assertRaises(
                    pb_ops.PrivateBetaInvitationOperationError
                ):
                    pb_ops._attest_database(integrity_failure, targets)

                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute(
                    "INSERT INTO jobs "
                    "(company_id, title, url, source_hash, first_seen_at, "
                    "last_seen_at) VALUES "
                    "(9223372036854775000, 'orphan', "
                    "'https://example.invalid/orphan', 'orphan-hash', "
                    "'2026-08-08T00:00:00+00:00', "
                    "'2026-08-08T00:00:00+00:00')"
                )
                connection.commit()
                connection.execute("PRAGMA foreign_keys = ON")
                self.assertTrue(current_closed_schema_is_exact(connection))
                self.assertIsNone(
                    runtime._attest_closed_database_schema(connection)
                )
                with self.assertRaises(
                    pb_ops.PrivateBetaInvitationOperationError
                ):
                    pb_ops._attest_database(connection, targets)

                connection.execute(
                    "DELETE FROM jobs WHERE source_hash='orphan-hash'"
                )
                connection.commit()
                sidecar.write_bytes(b"synthetic-sidecar-sentinel")
                with self.assertRaises(
                    pb_ops.PrivateBetaInvitationOperationError
                ):
                    pb_ops._attest_database(connection, targets)
            finally:
                if sidecar.exists():
                    sidecar.unlink()
                connection.close()

    def test_full_runtime_and_pb_ops_reject_foreign_key_invalid_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "consumer-foreign-key.sqlite"
            connection = build_fresh_m001_m006(path)
            try:
                apply_m007(connection, path)
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute(
                    "INSERT INTO jobs "
                    "(company_id, title, url, source_hash, first_seen_at, "
                    "last_seen_at) VALUES "
                    "(9223372036854775000, 'orphan', "
                    "'https://example.invalid/orphan', 'orphan-hash', "
                    "'2026-08-08T00:00:00+00:00', "
                    "'2026-08-08T00:00:00+00:00')"
                )
                connection.commit()
            finally:
                connection.close()

            target = runtime._database_target_authority(path.resolve(strict=True))
            coordinator = runtime._CleanupCoordinator()
            with self.assertRaises(runtime.DurableGoogleLoginConfigurationError):
                runtime._attest_existing_database(
                    target,
                    cleanup_coordinator=coordinator,
                )
            report = coordinator.snapshot()
            self.assertTrue(report.cleanup_complete)
            self.assertEqual(report.unresolved_resources, ())

            connection = _raw_connection(path)
            try:
                self.assertTrue(current_closed_schema_is_exact(connection))
                with self.assertRaises(
                    pb_ops.PrivateBetaInvitationOperationError
                ):
                    pb_ops._attest_database(
                        connection,
                        SimpleNamespace(database_path=path.resolve(strict=True)),
                    )
            finally:
                connection.close()

    def _assert_both_consumers_reject(self, connection, targets):
        with self.assertRaises(runtime.DurableGoogleLoginConfigurationError):
            runtime._attest_closed_database_schema(connection)
        with self.assertRaises(pb_ops.PrivateBetaInvitationOperationError):
            pb_ops._attest_database(connection, targets)


def _foreign_key_edges(connection, *, table=None):
    tables = (
        (table,)
        if table is not None
        else tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
            )
        )
    )
    edges = set()
    for child in tables:
        for row in connection.execute(f'PRAGMA foreign_key_list("{child}")'):
            edges.add((child, row[2], row[3], row[4]))
    return edges


def _index_details(connection, table):
    result = {}
    for row in connection.execute(f'PRAGMA index_list("{table}")'):
        columns = tuple(
            detail[2]
            for detail in connection.execute(f'PRAGMA index_xinfo("{row[1]}")')
            if detail[5] == 1
        )
        result[row[1]] = ((row[2], row[3], row[4]), columns)
    return result


def _drop_both_helper_indexes(connection):
    connection.execute("DROP INDEX idx_jobs_live_market")
    connection.execute("DROP INDEX idx_jobs_canonical_opportunity")


def _replace_prerequisite_marker_with_unapproved_marker(connection):
    connection.execute(
        "DELETE FROM wahojobs_schema_migrations "
        "WHERE version='006_google_oidc_authorization_transactions'"
    )
    connection.execute(
        "INSERT INTO wahojobs_schema_migrations(version) "
        "VALUES ('006_unapproved_lineage')"
    )


def _rewrite_stored_table_sql(connection, *, table, old, new):
    original = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?",
        (table,),
    ).fetchone()[0]
    if original.count(old) != 1:
        raise AssertionError(f"unexpected_{table}_sql")
    rewritten = original.replace(old, new)
    schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
    connection.execute("PRAGMA writable_schema = ON")
    try:
        connection.execute(
            "UPDATE sqlite_schema SET sql=? WHERE type='table' AND name=?",
            (rewritten, table),
        )
    finally:
        connection.execute("PRAGMA writable_schema = OFF")
    connection.execute(f"PRAGMA schema_version = {schema_version + 1}")


def _raw_connection(path):
    connection = sqlite3.connect(path, timeout=2.0)
    connection.row_factory = None
    connection.text_factory = str
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


class _ConnectionWithQuickCheckFailure:
    def __init__(self, connection):
        self._connection = connection

    @property
    def in_transaction(self):
        return self._connection.in_transaction

    def cursor(self):
        return _CursorWithQuickCheckFailure(self._connection.cursor())


class _CursorWithQuickCheckFailure:
    def __init__(self, cursor):
        self._cursor = cursor
        self._quick_check = False

    @property
    def row_factory(self):
        return self._cursor.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._cursor.row_factory = value

    def execute(self, statement, parameters=()):
        self._quick_check = statement.strip().casefold() == "pragma quick_check(1)"
        self._cursor.execute(statement, parameters)
        return self

    def fetchone(self):
        if self._quick_check:
            return ("synthetic_integrity_failure",)
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self):
        self._cursor.close()


if __name__ == "__main__":
    unittest.main()
