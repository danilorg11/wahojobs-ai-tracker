from __future__ import annotations

import sqlite3
from pathlib import Path

from wahojobs.closed_schema_authority import (
    CURRENT_CLOSED_SCHEMA_FINGERPRINT,
    CURRENT_CLOSED_SCHEMA_MARKERS,
    CURRENT_CLOSED_SCHEMA_MIGRATION,
    CURRENT_CLOSED_SCHEMA_OBJECT_COUNT,
    ClosedSchemaAttestationError,
    capture_closed_schema_identity,
)


MIGRATION_VERSION = CURRENT_CLOSED_SCHEMA_MIGRATION
MIGRATION_PATH = (
    Path(__file__).resolve().parent
    / "db"
    / "migrations"
    / "007_closed_schema_convergence.sql"
)
PREREQUISITE_MIGRATION_VERSIONS = CURRENT_CLOSED_SCHEMA_MARKERS[:-1]
EXPECTED_MIGRATION_VERSIONS = CURRENT_CLOSED_SCHEMA_MARKERS

LEGACY_SCHEMA_OBJECT_COUNT = 176
LEGACY_SCHEMA_FINGERPRINT = (
    "e866286ba8b1dd28c6b5258c3bd04ddb30ccb00760e677893c22b2c6decf042e"
)
EXPECTED_SCHEMA_OBJECT_COUNT = CURRENT_CLOSED_SCHEMA_OBJECT_COUNT
EXPECTED_SCHEMA_FINGERPRINT = CURRENT_CLOSED_SCHEMA_FINGERPRINT

TEMPORARY_TABLES = (
    "companies_m007_backup",
    "jobs_m007_backup",
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


def expected_closed_schema_convergence_manifest() -> dict:
    return {
        "migration_version": MIGRATION_VERSION,
        "migration_versions": EXPECTED_MIGRATION_VERSIONS,
        "schema_object_count": EXPECTED_SCHEMA_OBJECT_COUNT,
        "schema_fingerprint": EXPECTED_SCHEMA_FINGERPRINT,
        "temporary_object_count": 0,
    }


def attest_closed_schema_convergence_schema(conn: sqlite3.Connection) -> dict:
    try:
        identity = capture_closed_schema_identity(conn)
    except ClosedSchemaAttestationError:
        return _attestation(
            "invalid_prerequisite",
            blocking=True,
            applicable=False,
            object_count=None,
            fingerprint=None,
            versions=(),
            temporary_object_count=None,
        )

    object_count = identity.object_count
    fingerprint = identity.fingerprint
    versions = identity.migration_markers
    temporary_object_count = identity.temporary_object_count
    try:
        main_residue_object_count = _main_residue_object_count(conn)
    except (AttributeError, TypeError, ValueError, sqlite3.Error):
        return _attestation(
            "invalid_prerequisite",
            blocking=True,
            applicable=False,
            object_count=object_count,
            fingerprint=fingerprint,
            versions=versions,
            temporary_object_count=temporary_object_count,
            main_residue_object_count=None,
        )
    if temporary_object_count or main_residue_object_count:
        state = "residue"
    elif (
        object_count == EXPECTED_SCHEMA_OBJECT_COUNT
        and fingerprint == EXPECTED_SCHEMA_FINGERPRINT
        and versions == EXPECTED_MIGRATION_VERSIONS
    ):
        state = "correctly_installed"
    elif (
        object_count == EXPECTED_SCHEMA_OBJECT_COUNT
        and fingerprint == EXPECTED_SCHEMA_FINGERPRINT
        and versions == PREREQUISITE_MIGRATION_VERSIONS
    ):
        state = "canonical_marker_pending"
    elif (
        object_count == LEGACY_SCHEMA_OBJECT_COUNT
        and fingerprint == LEGACY_SCHEMA_FINGERPRINT
        and versions == PREREQUISITE_MIGRATION_VERSIONS
    ):
        state = "legacy_rebuild_pending"
    elif MIGRATION_VERSION in versions:
        state = "partial_inconsistent"
    elif versions != PREREQUISITE_MIGRATION_VERSIONS:
        state = "invalid_prerequisite"
    else:
        state = "schema_mismatch"

    applicable = state in {
        "canonical_marker_pending",
        "legacy_rebuild_pending",
    }
    return _attestation(
        state,
        blocking=state not in {
            "correctly_installed",
            "canonical_marker_pending",
            "legacy_rebuild_pending",
        },
        applicable=applicable,
        object_count=object_count,
        fingerprint=fingerprint,
        versions=versions,
        temporary_object_count=temporary_object_count,
        main_residue_object_count=main_residue_object_count,
    )


def migration_statement_count() -> int:
    return sum(
        1
        for _ in iter_sql_statements(
            MIGRATION_PATH.read_text(encoding="utf-8")
        )
    )


def iter_sql_statements(sql_text: str):
    buffer = []
    for line in sql_text.splitlines():
        buffer.append(line)
        candidate = "\n".join(buffer).strip()
        if candidate and sqlite3.complete_statement(candidate):
            yield candidate.rstrip().rstrip(";")
            buffer = []
    if "\n".join(buffer).strip():
        raise ValueError("incomplete_migration_sql")


def _attestation(
    state,
    *,
    blocking,
    applicable,
    object_count,
    fingerprint,
    versions,
    temporary_object_count,
    main_residue_object_count=0,
) -> dict:
    return {
        "migration_version": MIGRATION_VERSION,
        "state": state,
        "blocking": blocking,
        "applicable": applicable,
        "actual_schema_object_count": object_count,
        "expected_schema_object_count": EXPECTED_SCHEMA_OBJECT_COUNT,
        "actual_schema_fingerprint": fingerprint,
        "expected_schema_fingerprint": EXPECTED_SCHEMA_FINGERPRINT,
        "present_migration_versions": list(versions),
        "expected_migration_versions": list(EXPECTED_MIGRATION_VERSIONS),
        "temporary_object_count": temporary_object_count,
        "main_migration_residue_object_count": main_residue_object_count,
    }


def _main_residue_object_count(conn: sqlite3.Connection) -> int:
    cursor = conn.cursor()
    try:
        cursor.row_factory = None
        placeholders = ",".join("?" for _ in TEMPORARY_TABLES)
        row = cursor.execute(
            "SELECT COUNT(*) FROM main.sqlite_schema WHERE name IN ("
            + placeholders
            + ")",
            TEMPORARY_TABLES,
        ).fetchone()
    finally:
        cursor.close()
    if (
        type(row) is not tuple
        or len(row) != 1
        or type(row[0]) is not int
        or row[0] < 0
    ):
        raise ValueError("invalid_migration_residue_count")
    return row[0]
