"""Read-only exact-schema authority for Migration 009.

Importing this module performs no filesystem or database work.  M009 is
applicable only to an exact M008 database and its installed state is accepted
only when the complete schema fingerprint and marker lineage agree.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from wahojobs.closed_schema_authority import (
    PUBLIC_JOB_IDENTITY_CLOSED_SCHEMA_FINGERPRINT,
    PUBLIC_JOB_IDENTITY_CLOSED_SCHEMA_MARKERS,
    PUBLIC_JOB_IDENTITY_CLOSED_SCHEMA_MIGRATION,
    PUBLIC_JOB_IDENTITY_CLOSED_SCHEMA_OBJECT_COUNT,
    WORKOS_AUTHKIT_CLOSED_SCHEMA_FINGERPRINT,
    WORKOS_AUTHKIT_CLOSED_SCHEMA_MARKERS,
    WORKOS_AUTHKIT_CLOSED_SCHEMA_OBJECT_COUNT,
    ClosedSchemaAttestationError,
    capture_closed_schema_identity,
)
from wahojobs.workos_authkit_schema import iter_sql_statements


MIGRATION_VERSION = PUBLIC_JOB_IDENTITY_CLOSED_SCHEMA_MIGRATION
MIGRATION_PATH = (
    Path(__file__).resolve().parent
    / "db"
    / "migrations"
    / "009_public_job_identity.sql"
)
PREREQUISITE_MIGRATION_VERSIONS = WORKOS_AUTHKIT_CLOSED_SCHEMA_MARKERS
EXPECTED_MIGRATION_VERSIONS = PUBLIC_JOB_IDENTITY_CLOSED_SCHEMA_MARKERS
PREREQUISITE_SCHEMA_OBJECT_COUNT = WORKOS_AUTHKIT_CLOSED_SCHEMA_OBJECT_COUNT
PREREQUISITE_SCHEMA_FINGERPRINT = WORKOS_AUTHKIT_CLOSED_SCHEMA_FINGERPRINT
EXPECTED_SCHEMA_OBJECT_COUNT = PUBLIC_JOB_IDENTITY_CLOSED_SCHEMA_OBJECT_COUNT
EXPECTED_SCHEMA_FINGERPRINT = PUBLIC_JOB_IDENTITY_CLOSED_SCHEMA_FINGERPRINT


def attest_public_job_identity_schema(connection: sqlite3.Connection) -> dict:
    """Classify an exact M008 prerequisite or exact installed M009 closure."""

    try:
        identity = capture_closed_schema_identity(connection)
    except (ClosedSchemaAttestationError, sqlite3.Error, TypeError, ValueError):
        return _report(
            "invalid_prerequisite",
            blocking=True,
            applicable=False,
            identity=None,
        )

    if identity.temporary_object_count:
        state = "residue"
    elif (
        identity.object_count == EXPECTED_SCHEMA_OBJECT_COUNT
        and identity.fingerprint == EXPECTED_SCHEMA_FINGERPRINT
        and identity.migration_markers == EXPECTED_MIGRATION_VERSIONS
    ):
        state = "correctly_installed"
    elif (
        identity.object_count == PREREQUISITE_SCHEMA_OBJECT_COUNT
        and identity.fingerprint == PREREQUISITE_SCHEMA_FINGERPRINT
        and identity.migration_markers == PREREQUISITE_MIGRATION_VERSIONS
    ):
        state = "public_job_identity_pending"
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
            "public_job_identity_pending",
        },
        applicable=state == "public_job_identity_pending",
        identity=identity,
    )


def migration_statement_count() -> int:
    return sum(
        1
        for _statement in iter_sql_statements(
            MIGRATION_PATH.read_text(encoding="utf-8")
        )
    )


def _report(state, *, blocking, applicable, identity):
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
    }


__all__ = [
    "EXPECTED_MIGRATION_VERSIONS",
    "EXPECTED_SCHEMA_FINGERPRINT",
    "EXPECTED_SCHEMA_OBJECT_COUNT",
    "MIGRATION_PATH",
    "MIGRATION_VERSION",
    "PREREQUISITE_MIGRATION_VERSIONS",
    "attest_public_job_identity_schema",
    "migration_statement_count",
]
