from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import scripts.accounts_migration as migration_002
import scripts.closed_schema_convergence_migration as migration_007
import scripts.google_oidc_authorization_transactions_migration as migration_006
import scripts.ownership_migration as migration_003
import scripts.persistent_profile_canonical_v2_migration as migration_005
import scripts.persistent_profiles_migration as migration_004
import scripts.pipeline_state_migration as migration_001
from wahojobs.closed_schema_authority import capture_closed_schema_identity
from wahojobs.database_lifetime_ownership import (
    ROLE_OFFLINE_OPERATOR,
    acquire_database_lifetime_ownership,
    release_database_lifetime_ownership,
)
from wahojobs.db.repository import initialize_database, install_base_schema


LEGACY_SCHEMA_FINGERPRINT = (
    "e866286ba8b1dd28c6b5258c3bd04ddb30ccb00760e677893c22b2c6decf042e"
)
CANONICAL_SCHEMA_FINGERPRINT = (
    "37a156dd9677e2bdb0eba5168a4ea150e30d771e1e7104a3b368f31667c2eaed"
)
SQLITE_MAX_INTEGER = 9_223_372_036_854_775_807


# This is the exact first tracked shape from commit 9007811b.  The ALTER list
# below deliberately models how a long-lived database reached current main;
# using today's CREATE TABLE text here would erase the contradiction under test.
_PHASE_ONE_COMPANIES_AND_JOBS = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS companies (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  slug TEXT NOT NULL UNIQUE,
  careers_url TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company_id INTEGER NOT NULL,
  external_id TEXT,
  title TEXT NOT NULL,
  location TEXT,
  url TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  removed_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,

  FOREIGN KEY (company_id) REFERENCES companies(id),
  UNIQUE (company_id, source_hash)
);
"""

_HISTORICAL_ALTERS = (
    "ALTER TABLE jobs ADD COLUMN department TEXT",
    "ALTER TABLE jobs ADD COLUMN commitment TEXT",
    "ALTER TABLE jobs ADD COLUMN expertise TEXT",
    "ALTER TABLE jobs ADD COLUMN canonical_opportunity_id INTEGER",
    "ALTER TABLE companies ADD COLUMN source_tier TEXT NOT NULL DEFAULT 'core'",
    "ALTER TABLE companies ADD COLUMN inventory_model TEXT NOT NULL DEFAULT 'live_feed'",
    "ALTER TABLE companies ADD COLUMN market_count_policy TEXT NOT NULL DEFAULT 'count_live'",
    "ALTER TABLE jobs ADD COLUMN opportunity_kind TEXT NOT NULL DEFAULT 'live_posting'",
    "ALTER TABLE jobs ADD COLUMN availability_basis TEXT NOT NULL DEFAULT 'api_feed'",
    "ALTER TABLE jobs ADD COLUMN include_in_live_market_estimate INTEGER NOT NULL DEFAULT 1",
)

COMPANY_COLUMNS = (
    "id",
    "name",
    "slug",
    "careers_url",
    "source_tier",
    "inventory_model",
    "market_count_policy",
    "created_at",
    "updated_at",
)
JOB_COLUMNS = (
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
)

DEPENDENT_TABLES = (
    "canonical_opportunities",
    "crawl_runs",
    "job_events",
)


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=2.0)
    # M001--M005 and repository installation consume named sqlite3.Row fields.
    # The constructor switches back to raw tuples after the migration chain so
    # the closed-schema/PB-OPS contracts see their production row shape.
    connection.row_factory = sqlite3.Row
    connection.text_factory = str
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def build_legacy_m001_m006(path: Path) -> sqlite3.Connection:
    """Build the accepted ALTER-evolved e866 lineage without historical data."""

    connection = connect(path)
    connection.executescript(_PHASE_ONE_COMPANIES_AND_JOBS)
    for statement in _HISTORICAL_ALTERS:
        connection.execute(statement)
    # The current installer supplies every unaffected base object and the two
    # accepted opportunity indexes.  Existing historical tables are not rebuilt.
    install_base_schema(connection)
    connection.commit()
    _install_migrations_001_through_006(connection, path)
    _assert_constructor_identity(connection, LEGACY_SCHEMA_FINGERPRINT)
    return connection


def build_fresh_m001_m006(path: Path) -> sqlite3.Connection:
    """Build the canonical current-source 37a lineage at one explicit temp path."""

    # This is intentionally the public initializer rather than a test-only
    # schema shortcut.  Its checked-in synthetic public defaults are acceptable
    # in the disposable test target and prove the real constructor's schema.
    initialize_database(path)
    connection = connect(path)
    _install_migrations_001_through_006(connection, path)
    _assert_constructor_identity(connection, CANONICAL_SCHEMA_FINGERPRINT)
    return connection


def _install_migrations_001_through_006(
    connection: sqlite3.Connection,
    path: Path,
) -> None:
    migration_001.apply_pipeline_state_migration(connection)
    migration_002.apply_accounts_migration(connection)
    migration_003.apply_ownership_migration(connection)
    migration_004.apply_persistent_profiles_migration(connection)
    migration_005.apply_persistent_profile_canonical_v2_migration(connection)
    migration_006.apply_google_oidc_authorization_transactions_migration(
        connection,
        requested_path=path.resolve(strict=True),
        expected_identity=migration_006.database_file_identity(path),
    )
    connection.row_factory = None
    connection.text_factory = str
    connection.execute("PRAGMA foreign_keys = ON")


def _assert_constructor_identity(
    connection: sqlite3.Connection,
    fingerprint: str,
) -> None:
    identity = capture_closed_schema_identity(connection)
    if (
        identity.object_count != 176
        or identity.fingerprint != fingerprint
        or identity.migration_markers
        != migration_007.PREREQUISITE_MIGRATION_VERSIONS
        or identity.temporary_object_count != 0
    ):
        raise AssertionError(f"synthetic_constructor_drift:{identity!r}")


@contextmanager
def offline_owner(path: Path):
    owner = acquire_database_lifetime_ownership(
        path.resolve(strict=True),
        role=ROLE_OFFLINE_OPERATOR,
    )
    try:
        yield owner
    finally:
        release_database_lifetime_ownership(
            owner,
            role=ROLE_OFFLINE_OPERATOR,
            database_path=path.resolve(strict=True),
        )


def apply_m007(
    connection: sqlite3.Connection,
    path: Path,
    *,
    failure_injector=None,
    commit_state=None,
):
    with offline_owner(path) as owner:
        return migration_007.apply_closed_schema_convergence_migration(
            connection,
            requested_path=path.resolve(strict=True),
            expected_identity=migration_006.database_file_identity(path),
            ownership=owner,
            failure_injector=failure_injector,
            commit_state=commit_state,
        )


def insert_unicode_high_id_graph(connection: sqlite3.Connection) -> None:
    high_company_id = SQLITE_MAX_INTEGER - 101
    high_job_id = SQLITE_MAX_INTEGER - 201
    long_title = "Trabalho—Δοκιμή—仕事—🙂—" + ("界" * 4_096)
    connection.execute(
        "INSERT INTO companies "
        "(id, name, slug, careers_url, source_tier, inventory_model, "
        "market_count_policy, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            high_company_id,
            "Companhia Ångström 東京 🙂",
            "unicode-company",
            "https://example.invalid/carreiras/ação",
            "core",
            "live_feed",
            "count_live",
            "2026-08-08T01:02:03+00:00",
            "2026-08-08T02:03:04+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO companies "
        "(id, name, slug, careers_url, source_tier, inventory_model, "
        "market_count_policy, created_at, updated_at) "
        "VALUES (17, 'Second', 'second-company', 'https://example.invalid/second', "
        "'experimental', 'public_inventory', 'report_separately', "
        "'2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO canonical_opportunities "
        "(id, company_id, canonical_key, canonical_title, normalized_title, "
        "source_category, language, language_locale, first_seen_at, last_seen_at, "
        "is_active, variant_count, created_at, updated_at) "
        "VALUES (71, ?, 'κλειδί🙂', ?, 'unicode title', 'synthetic', 'Português', "
        "'pt-BR', '2026-02-01T00:00:00+00:00', '2026-02-02T00:00:00+00:00', "
        "1, 1, '2026-02-01T00:00:00+00:00', '2026-02-02T00:00:00+00:00')",
        (high_company_id, long_title),
    )
    connection.execute(
        "INSERT INTO crawl_runs "
        "(id, company_id, status, started_at, finished_at, jobs_found_count, "
        "jobs_new_count, jobs_reactivated_count, jobs_updated_count, "
        "jobs_removed_count, used_sample_data, error_message, created_at) "
        "VALUES (81, ?, 'completed', '2026-03-01T00:00:00+00:00', "
        "'2026-03-01T00:01:00+00:00', 2, 2, 0, 0, 0, 0, NULL, "
        "'2026-03-01T00:00:00+00:00')",
        (high_company_id,),
    )
    connection.execute(
        "INSERT INTO jobs "
        "(id, company_id, canonical_opportunity_id, external_id, title, location, "
        "department, expertise, commitment, url, source_hash, opportunity_kind, "
        "availability_basis, include_in_live_market_estimate, first_seen_at, "
        "last_seen_at, is_active, removed_at, created_at, updated_at) "
        "VALUES (?, ?, 71, 'ext-🙂', ?, 'São Paulo / 東京', 'Pesquisa Δ', "
        "'Python / lingüística', 'CONTRACT', 'https://example.invalid/vaga/🙂', "
        "'sha-unicode-one', 'live_posting', 'api_feed', 1, "
        "'2026-04-01T00:00:00+00:00', '2026-04-02T00:00:00+00:00', 1, NULL, "
        "'2026-04-01T00:00:00+00:00', '2026-04-02T00:00:00+00:00')",
        (high_job_id, high_company_id, long_title),
    )
    connection.execute(
        "INSERT INTO jobs "
        "(id, company_id, canonical_opportunity_id, external_id, title, location, "
        "department, expertise, commitment, url, source_hash, opportunity_kind, "
        "availability_basis, include_in_live_market_estimate, first_seen_at, "
        "last_seen_at, is_active, removed_at, created_at, updated_at) "
        "VALUES (27, 17, NULL, NULL, 'Inactive exact timestamp', NULL, NULL, NULL, "
        "NULL, 'https://example.invalid/inactive', 'sha-two', 'public_inventory', "
        "'catalog', 0, '2025-01-01T00:00:00+00:00', "
        "'2025-01-02T00:00:00+00:00', 0, '2025-01-03T00:00:00+00:00', "
        "'2025-01-01T00:00:00+00:00', '2025-01-03T00:00:00+00:00')"
    )
    connection.execute(
        "INSERT INTO job_events (id, job_id, crawl_run_id, event_type, created_at) "
        "VALUES (91, ?, 81, 'discovered', '2026-04-01T00:00:01+00:00')",
        (high_job_id,),
    )

    # Exercise high authority and protect every unrelated sequence row with a
    # distinct sentinel.  M007 may rewrite only companies/jobs while restoring
    # the final logical sequence inventory exactly.
    sequence_values = {
        "companies": SQLITE_MAX_INTEGER,
        "jobs": SQLITE_MAX_INTEGER,
        "canonical_opportunities": 7_071,
        "crawl_runs": 8_081,
        "job_events": 9_091,
        "user_profiles": 10_001,
        "user_pipeline_items": 10_002,
        "applicant_status_updates": 10_003,
        "user_pipeline_transitions": 10_004,
    }
    for table, value in sequence_values.items():
        cursor = connection.execute(
            "UPDATE sqlite_sequence SET seq=? WHERE name=?",
            (value, table),
        )
        if cursor.rowcount == 0:
            connection.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                (table, value),
            )
    connection.commit()


def schema_objects(connection: sqlite3.Connection) -> tuple[tuple, ...]:
    return tuple(
        connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE type IN ('table','index','trigger','view') "
            "ORDER BY type, name, tbl_name"
        ).fetchall()
    )


def schema_object_map(connection: sqlite3.Connection) -> dict[tuple[str, str], tuple]:
    return {
        (row[0], row[1]): tuple(row)
        for row in schema_objects(connection)
    }


def migration_markers(connection: sqlite3.Connection) -> tuple[tuple, ...]:
    return tuple(
        connection.execute(
            "SELECT version, applied_at FROM wahojobs_schema_migrations "
            "ORDER BY version"
        ).fetchall()
    )


def sequence_rows(connection: sqlite3.Connection) -> tuple[tuple, ...]:
    return tuple(
        connection.execute(
            "SELECT rowid, name, typeof(seq), seq FROM sqlite_sequence ORDER BY rowid"
        ).fetchall()
    )


def named_table_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> tuple[tuple, ...]:
    quoted = ", ".join(_quote_identifier(column) for column in columns)
    typed = ", ".join(
        f"typeof({_quote_identifier(column)})" for column in columns
    )
    return tuple(
        connection.execute(
            f"SELECT {quoted}, {typed} FROM {_quote_identifier(table)} "
            f"ORDER BY {_quote_identifier(columns[0])}"
        ).fetchall()
    )


def dependent_schema(connection: sqlite3.Connection) -> tuple[tuple, ...]:
    return tuple(
        row
        for row in schema_objects(connection)
        if row[2] in DEPENDENT_TABLES
    )


def logical_snapshot(connection: sqlite3.Connection) -> dict:
    tables = tuple(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
        )
        if row[0] != "sqlite_sequence"
    )
    table_rows = []
    for table in tables:
        columns = tuple(
            row[1]
            for row in connection.execute(
                f"PRAGMA table_xinfo({_quote_identifier(table)})"
            )
            if row[6] == 0
        )
        projection = ", ".join(_quote_identifier(column) for column in columns)
        raw_rows = connection.execute(
            f"SELECT {projection} FROM {_quote_identifier(table)}"
        ).fetchall()
        encoded = sorted(
            json.dumps(
                [_typed_value(value) for value in row],
                ensure_ascii=True,
                separators=(",", ":"),
            )
            for row in raw_rows
        )
        table_rows.append((table, columns, tuple(encoded)))
    return {
        "schema": schema_objects(connection),
        "tables": tuple(table_rows),
        "sequence": sequence_rows(connection),
        "markers": migration_markers(connection),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone(),
        "foreign_keys": tuple(connection.execute("PRAGMA foreign_key_check")),
        "temp_count": connection.execute(
            "SELECT COUNT(*) FROM temp.sqlite_schema"
        ).fetchone(),
    }


def file_snapshot(path: Path) -> tuple[int, int, str]:
    metadata = path.stat()
    return (
        metadata.st_size,
        metadata.st_mtime_ns,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _typed_value(value):
    if value is None:
        return ["null", None]
    if type(value) is bytes:
        return ["blob", value.hex()]
    if type(value) is int:
        return ["integer", value]
    if type(value) is float:
        return ["real", value.hex()]
    if type(value) is str:
        return ["text", value]
    raise AssertionError(f"unexpected_sqlite_value:{type(value)!r}")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


__all__ = (
    "CANONICAL_SCHEMA_FINGERPRINT",
    "COMPANY_COLUMNS",
    "DEPENDENT_TABLES",
    "JOB_COLUMNS",
    "LEGACY_SCHEMA_FINGERPRINT",
    "SQLITE_MAX_INTEGER",
    "apply_m007",
    "build_fresh_m001_m006",
    "build_legacy_m001_m006",
    "connect",
    "dependent_schema",
    "file_snapshot",
    "insert_unicode_high_id_graph",
    "logical_snapshot",
    "migration_markers",
    "named_table_rows",
    "offline_owner",
    "schema_object_map",
    "schema_objects",
    "sequence_rows",
)
