"""Read-only authority for the current complete Wahojobs SQLite schema.

Importing this module performs no filesystem, database, configuration, network,
clock, or randomness work.  The exact closed-schema read is performed only when
one of the public functions is called with an already-open SQLite connection.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import sqlite3

from wahojobs.opportunity_enrichment_schema import (
    OPPORTUNITY_ENRICHMENT_SCHEMA_OBJECTS,
    OpportunityEnrichmentSchemaError,
    attest_opportunity_enrichment_schema_extension,
)


CURRENT_CLOSED_SCHEMA_MIGRATION = "007_closed_schema_convergence"
CURRENT_CLOSED_SCHEMA_MARKERS = (
    "001_pipeline_state",
    "002_accounts_sessions",
    "003_product_principals",
    "004_persistent_product_profiles",
    "005_persistent_profile_canonical_v2",
    "006_google_oidc_authorization_transactions",
    CURRENT_CLOSED_SCHEMA_MIGRATION,
)
CURRENT_CLOSED_SCHEMA_OBJECT_COUNT = 176
CURRENT_CLOSED_SCHEMA_FINGERPRINT = (
    "37a156dd9677e2bdb0eba5168a4ea150e30d771e1e7104a3b368f31667c2eaed"
)

# M007 remains an accepted closed schema for the unchanged Google runtime.
# M008 changes only the auth-identity provider check and is accepted alongside
# it so an upgraded database preserves every existing capability.
WORKOS_AUTHKIT_CLOSED_SCHEMA_MIGRATION = "008_workos_authkit_provider"
WORKOS_AUTHKIT_CLOSED_SCHEMA_MARKERS = (
    *CURRENT_CLOSED_SCHEMA_MARKERS,
    WORKOS_AUTHKIT_CLOSED_SCHEMA_MIGRATION,
)
WORKOS_AUTHKIT_CLOSED_SCHEMA_OBJECT_COUNT = 176
WORKOS_AUTHKIT_CLOSED_SCHEMA_FINGERPRINT = (
    "c906791bbbe607ec42ed6b5953d86a7f9ed580c38919704a6abf2e1740fb30e3"
)

# M009 is an additive, dormant public-routing authority.  It changes no M008
# account object and is accepted as a third exact closed schema so compatible
# runtimes can keep operating before any public-ID canary is enabled.
PUBLIC_JOB_IDENTITY_CLOSED_SCHEMA_MIGRATION = "009_public_job_identity"
PUBLIC_JOB_IDENTITY_CLOSED_SCHEMA_MARKERS = (
    *WORKOS_AUTHKIT_CLOSED_SCHEMA_MARKERS,
    PUBLIC_JOB_IDENTITY_CLOSED_SCHEMA_MIGRATION,
)
PUBLIC_JOB_IDENTITY_CLOSED_SCHEMA_OBJECT_COUNT = 202
PUBLIC_JOB_IDENTITY_CLOSED_SCHEMA_FINGERPRINT = (
    "42c039abd1483123e1c067f6a85a8c6ae1f3dae420abdd6a198eed8e44f3be2c"
)

_MAX_CLOSED_SCHEMA_SQL_BYTES = 1_048_576


class ClosedSchemaAttestationError(Exception):
    """One bounded, detail-free failure for a malformed schema snapshot."""

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class ClosedSchemaIdentity:
    object_count: int
    fingerprint: str
    migration_markers: tuple[str, ...]
    temporary_object_count: int


def capture_closed_schema_identity(connection) -> ClosedSchemaIdentity:
    """Capture one bounded identity without accepting or mutating the schema."""

    cursor = None
    rows = []
    markers = []
    total_sql_bytes = 0
    try:
        cursor = connection.cursor()
        cursor.row_factory = None
        attest_opportunity_enrichment_schema_extension(cursor)
        extension_placeholders = ",".join(
            "?" for _ in OPPORTUNITY_ENRICHMENT_SCHEMA_OBJECTS
        )
        cursor.execute(
            "SELECT CAST(type AS BLOB), CAST(name AS BLOB), "
            "CAST(tbl_name AS BLOB), CAST(sql AS BLOB) "
            "FROM main.sqlite_schema "
            "WHERE type IN ('table','index','trigger','view') "
            "AND name NOT IN (" + extension_placeholders + ") "
            "ORDER BY type, name, tbl_name "
            f"LIMIT {PUBLIC_JOB_IDENTITY_CLOSED_SCHEMA_OBJECT_COUNT + 1}",
            OPPORTUNITY_ENRICHMENT_SCHEMA_OBJECTS,
        )
        for raw in cursor.fetchall():
            if (
                type(raw) is not tuple
                or len(raw) != 4
                or any(type(value) is not bytes for value in raw[:3])
                or (raw[3] is not None and type(raw[3]) is not bytes)
            ):
                raise ClosedSchemaAttestationError()
            kind, name, table_name = (
                value.decode("utf-8", "strict") for value in raw[:3]
            )
            sql = None
            if raw[3] is not None:
                total_sql_bytes += len(raw[3])
                if total_sql_bytes > _MAX_CLOSED_SCHEMA_SQL_BYTES:
                    raise ClosedSchemaAttestationError()
                sql = raw[3].decode("utf-8", "strict")
            rows.append((kind, name, table_name, sql))

        marker_rows = cursor.execute(
            "SELECT CAST(version AS BLOB) "
            "FROM main.wahojobs_schema_migrations "
            "ORDER BY version "
            f"LIMIT {len(PUBLIC_JOB_IDENTITY_CLOSED_SCHEMA_MARKERS) + 1}"
        ).fetchall()
        for raw in marker_rows:
            if type(raw) is not tuple or len(raw) != 1 or type(raw[0]) is not bytes:
                raise ClosedSchemaAttestationError()
            markers.append(raw[0].decode("utf-8", "strict"))

        temporary_count = cursor.execute(
            "SELECT COUNT(*) FROM temp.sqlite_schema"
        ).fetchone()
        if (
            type(temporary_count) is not tuple
            or len(temporary_count) != 1
            or type(temporary_count[0]) is not int
            or temporary_count[0] < 0
        ):
            raise ClosedSchemaAttestationError()
    except ClosedSchemaAttestationError:
        raise
    except OpportunityEnrichmentSchemaError:
        raise ClosedSchemaAttestationError() from None
    except (AttributeError, TypeError, UnicodeError, ValueError, sqlite3.Error):
        raise ClosedSchemaAttestationError() from None
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except sqlite3.Error:
                raise ClosedSchemaAttestationError() from None

    payload = json.dumps(
        rows,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return ClosedSchemaIdentity(
        object_count=len(rows),
        fingerprint=hashlib.sha256(payload).hexdigest(),
        migration_markers=tuple(markers),
        temporary_object_count=temporary_count[0],
    )


def current_closed_schema_is_exact(connection) -> bool:
    """Return exact ``True`` for either approved M007 or M008 closure."""

    identity = capture_closed_schema_identity(connection)
    accepted = (
        (
            CURRENT_CLOSED_SCHEMA_OBJECT_COUNT,
            CURRENT_CLOSED_SCHEMA_FINGERPRINT,
            CURRENT_CLOSED_SCHEMA_MARKERS,
        ),
        (
            WORKOS_AUTHKIT_CLOSED_SCHEMA_OBJECT_COUNT,
            WORKOS_AUTHKIT_CLOSED_SCHEMA_FINGERPRINT,
            WORKOS_AUTHKIT_CLOSED_SCHEMA_MARKERS,
        ),
        (
            PUBLIC_JOB_IDENTITY_CLOSED_SCHEMA_OBJECT_COUNT,
            PUBLIC_JOB_IDENTITY_CLOSED_SCHEMA_FINGERPRINT,
            PUBLIC_JOB_IDENTITY_CLOSED_SCHEMA_MARKERS,
        ),
    )
    return identity.temporary_object_count == 0 and any(
        identity.object_count == object_count
        and hmac.compare_digest(identity.fingerprint, fingerprint)
        and identity.migration_markers == markers
        for object_count, fingerprint, markers in accepted
    )
