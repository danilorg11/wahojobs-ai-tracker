"""Read-only exact-schema authority for Migration 011."""

from __future__ import annotations

from pathlib import Path
import sqlite3

from wahojobs.closed_schema_authority import (
    AI_PROFILE_IMPORT_CLOSED_SCHEMA_FINGERPRINT,
    AI_PROFILE_IMPORT_CLOSED_SCHEMA_MARKERS,
    AI_PROFILE_IMPORT_CLOSED_SCHEMA_OBJECT_COUNT,
    RESUMABLE_AI_INTAKE_CLOSED_SCHEMA_FINGERPRINT,
    RESUMABLE_AI_INTAKE_CLOSED_SCHEMA_MARKERS,
    RESUMABLE_AI_INTAKE_CLOSED_SCHEMA_MIGRATION,
    RESUMABLE_AI_INTAKE_CLOSED_SCHEMA_OBJECT_COUNT,
    ClosedSchemaAttestationError,
    capture_closed_schema_identity,
)
from wahojobs.workos_authkit_schema import iter_sql_statements


MIGRATION_VERSION = RESUMABLE_AI_INTAKE_CLOSED_SCHEMA_MIGRATION
MIGRATION_PATH = (
    Path(__file__).resolve().parent
    / "db"
    / "migrations"
    / "011_resumable_ai_profile_intake.sql"
)
PREREQUISITE_MIGRATION_VERSIONS = AI_PROFILE_IMPORT_CLOSED_SCHEMA_MARKERS
EXPECTED_MIGRATION_VERSIONS = RESUMABLE_AI_INTAKE_CLOSED_SCHEMA_MARKERS
PREREQUISITE_SCHEMA_OBJECT_COUNT = AI_PROFILE_IMPORT_CLOSED_SCHEMA_OBJECT_COUNT
PREREQUISITE_SCHEMA_FINGERPRINT = AI_PROFILE_IMPORT_CLOSED_SCHEMA_FINGERPRINT
EXPECTED_SCHEMA_OBJECT_COUNT = RESUMABLE_AI_INTAKE_CLOSED_SCHEMA_OBJECT_COUNT
EXPECTED_SCHEMA_FINGERPRINT = RESUMABLE_AI_INTAKE_CLOSED_SCHEMA_FINGERPRINT


def attest_resumable_ai_profile_intake_schema(connection: sqlite3.Connection) -> dict:
    """Accept only exact installed M010 or exact installed M011 closure."""

    try:
        identity = capture_closed_schema_identity(connection)
    except (ClosedSchemaAttestationError, sqlite3.Error, TypeError, ValueError):
        return _report("invalid_prerequisite", blocking=True, applicable=False, identity=None)
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
        state = "resumable_ai_intake_pending"
    elif MIGRATION_VERSION in identity.migration_markers:
        state = "partial_inconsistent"
    elif identity.migration_markers != PREREQUISITE_MIGRATION_VERSIONS:
        state = "invalid_prerequisite"
    else:
        state = "schema_mismatch"
    return _report(
        state,
        blocking=state not in {"correctly_installed", "resumable_ai_intake_pending"},
        applicable=state == "resumable_ai_intake_pending",
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
        "actual_schema_object_count": None if identity is None else identity.object_count,
        "expected_schema_object_count": EXPECTED_SCHEMA_OBJECT_COUNT,
        "actual_schema_fingerprint": None if identity is None else identity.fingerprint,
        "expected_schema_fingerprint": EXPECTED_SCHEMA_FINGERPRINT,
        "present_migration_versions": [] if identity is None else list(identity.migration_markers),
        "expected_migration_versions": list(EXPECTED_MIGRATION_VERSIONS),
        "temporary_object_count": None if identity is None else identity.temporary_object_count,
    }


__all__ = (
    "EXPECTED_MIGRATION_VERSIONS",
    "EXPECTED_SCHEMA_FINGERPRINT",
    "EXPECTED_SCHEMA_OBJECT_COUNT",
    "MIGRATION_PATH",
    "MIGRATION_VERSION",
    "PREREQUISITE_MIGRATION_VERSIONS",
    "attest_resumable_ai_profile_intake_schema",
    "migration_statement_count",
)
