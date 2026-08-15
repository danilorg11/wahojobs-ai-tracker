"""Read-only M008 schema attestation for the WorkOS AuthKit provider."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from wahojobs.closed_schema_authority import (
    CURRENT_CLOSED_SCHEMA_FINGERPRINT,
    CURRENT_CLOSED_SCHEMA_MARKERS,
    CURRENT_CLOSED_SCHEMA_OBJECT_COUNT,
    WORKOS_AUTHKIT_CLOSED_SCHEMA_FINGERPRINT,
    WORKOS_AUTHKIT_CLOSED_SCHEMA_MARKERS,
    WORKOS_AUTHKIT_CLOSED_SCHEMA_MIGRATION,
    WORKOS_AUTHKIT_CLOSED_SCHEMA_OBJECT_COUNT,
    ClosedSchemaAttestationError,
    capture_closed_schema_identity,
)


MIGRATION_VERSION = WORKOS_AUTHKIT_CLOSED_SCHEMA_MIGRATION
MIGRATION_PATH = (
    Path(__file__).resolve().parent
    / "db"
    / "migrations"
    / "008_workos_authkit_provider.sql"
)
PREREQUISITE_MIGRATION_VERSIONS = CURRENT_CLOSED_SCHEMA_MARKERS
EXPECTED_MIGRATION_VERSIONS = WORKOS_AUTHKIT_CLOSED_SCHEMA_MARKERS
EXPECTED_SCHEMA_OBJECT_COUNT = WORKOS_AUTHKIT_CLOSED_SCHEMA_OBJECT_COUNT
EXPECTED_SCHEMA_FINGERPRINT = WORKOS_AUTHKIT_CLOSED_SCHEMA_FINGERPRINT
TEMPORARY_TABLE = "auth_identities_m008_backup"


def attest_workos_authkit_schema(connection: sqlite3.Connection) -> dict:
    """Classify the exact M007 prerequisite or installed M008 closure."""

    try:
        identity = capture_closed_schema_identity(connection)
        residue = _residue_count(connection)
    except (ClosedSchemaAttestationError, sqlite3.Error, TypeError, ValueError):
        return _report(
            "invalid_prerequisite",
            blocking=True,
            applicable=False,
            identity=None,
            residue=None,
        )

    if identity.temporary_object_count or residue:
        state = "residue"
    elif (
        identity.object_count == EXPECTED_SCHEMA_OBJECT_COUNT
        and identity.fingerprint == EXPECTED_SCHEMA_FINGERPRINT
        and identity.migration_markers == EXPECTED_MIGRATION_VERSIONS
    ):
        state = "correctly_installed"
    elif (
        identity.object_count == CURRENT_CLOSED_SCHEMA_OBJECT_COUNT
        and identity.fingerprint == CURRENT_CLOSED_SCHEMA_FINGERPRINT
        and identity.migration_markers == PREREQUISITE_MIGRATION_VERSIONS
    ):
        state = "provider_expansion_pending"
    elif MIGRATION_VERSION in identity.migration_markers:
        state = "partial_inconsistent"
    elif identity.migration_markers != PREREQUISITE_MIGRATION_VERSIONS:
        state = "invalid_prerequisite"
    else:
        state = "schema_mismatch"
    return _report(
        state,
        blocking=state not in {
            "correctly_installed",
            "provider_expansion_pending",
        },
        applicable=state == "provider_expansion_pending",
        identity=identity,
        residue=residue,
    )


def migration_statement_count() -> int:
    return sum(
        1
        for _statement in iter_sql_statements(
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


def _residue_count(connection) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM main.sqlite_schema WHERE name = ?",
        (TEMPORARY_TABLE,),
    ).fetchone()
    if row is None or type(row[0]) is not int or row[0] < 0:
        raise ValueError("invalid_migration_residue_count")
    return row[0]


def _report(state, *, blocking, applicable, identity, residue):
    return {
        "migration_version": MIGRATION_VERSION,
        "state": state,
        "blocking": blocking,
        "applicable": applicable,
        "actual_schema_object_count": (
            None if identity is None else identity.object_count
        ),
        "expected_schema_object_count": EXPECTED_SCHEMA_OBJECT_COUNT,
        "actual_schema_fingerprint": (
            None if identity is None else identity.fingerprint
        ),
        "expected_schema_fingerprint": EXPECTED_SCHEMA_FINGERPRINT,
        "present_migration_versions": (
            [] if identity is None else list(identity.migration_markers)
        ),
        "expected_migration_versions": list(EXPECTED_MIGRATION_VERSIONS),
        "temporary_object_count": (
            None if identity is None else identity.temporary_object_count
        ),
        "main_migration_residue_object_count": residue,
    }


__all__ = [
    "EXPECTED_MIGRATION_VERSIONS",
    "EXPECTED_SCHEMA_FINGERPRINT",
    "EXPECTED_SCHEMA_OBJECT_COUNT",
    "MIGRATION_PATH",
    "MIGRATION_VERSION",
    "PREREQUISITE_MIGRATION_VERSIONS",
    "attest_workos_authkit_schema",
    "iter_sql_statements",
    "migration_statement_count",
]
