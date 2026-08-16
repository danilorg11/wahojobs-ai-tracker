"""Exact additive schema contract for durable opportunity enrichment.

The authentication database has a separately attested closed schema.  These
objects are an optional, narrowly sanctioned domain extension: either all are
absent or all must match this exact contract.
"""

from __future__ import annotations

import re
import sqlite3


OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS opportunity_enrichments (
      canonical_opportunity_id INTEGER PRIMARY KEY,
      schema_version TEXT NOT NULL,
      taxonomy_version TEXT NOT NULL,
      extractor_version TEXT NOT NULL,
      input_sha256 TEXT NOT NULL,
      status TEXT NOT NULL CHECK (status IN ('complete', 'partial', 'failed')),
      automatic_document_json TEXT NOT NULL,
      model_provider TEXT,
      model_name TEXT,
      prompt_version TEXT,
      generated_at TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY (canonical_opportunity_id) REFERENCES canonical_opportunities(id)
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS opportunity_enrichment_overrides (
      canonical_opportunity_id INTEGER NOT NULL,
      field_path TEXT NOT NULL,
      operation TEXT NOT NULL CHECK (operation IN ('set', 'set_unknown')),
      value_json TEXT,
      actor TEXT NOT NULL,
      reason TEXT NOT NULL,
      provenance_json TEXT NOT NULL DEFAULT '{}',
      automatic_input_sha256_at_override TEXT,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY (canonical_opportunity_id) REFERENCES canonical_opportunities(id)
        ON DELETE CASCADE,
      PRIMARY KEY (canonical_opportunity_id, field_path),
      CHECK (
        (operation = 'set' AND value_json IS NOT NULL)
        OR (operation = 'set_unknown' AND value_json IS NULL)
      )
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opportunity_enrichments_status
    ON opportunity_enrichments(status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opportunity_enrichment_overrides_canonical
    ON opportunity_enrichment_overrides(canonical_opportunity_id)
    """,
)

OPPORTUNITY_ENRICHMENT_SCHEMA_OBJECTS = (
    "idx_opportunity_enrichment_overrides_canonical",
    "idx_opportunity_enrichments_status",
    "opportunity_enrichment_overrides",
    "opportunity_enrichments",
    "sqlite_autoindex_opportunity_enrichment_overrides_1",
)

_EXPECTED_OBJECTS = {
    "opportunity_enrichments": (
        "table",
        "opportunity_enrichments",
        OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[0],
    ),
    "opportunity_enrichment_overrides": (
        "table",
        "opportunity_enrichment_overrides",
        OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[1],
    ),
    "idx_opportunity_enrichments_status": (
        "index",
        "opportunity_enrichments",
        OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[2],
    ),
    "idx_opportunity_enrichment_overrides_canonical": (
        "index",
        "opportunity_enrichment_overrides",
        OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[3],
    ),
    "sqlite_autoindex_opportunity_enrichment_overrides_1": (
        "index",
        "opportunity_enrichment_overrides",
        None,
    ),
}


class OpportunityEnrichmentSchemaError(Exception):
    __slots__ = ()


def attest_opportunity_enrichment_schema_extension(cursor) -> bool:
    """Return whether the exact extension exists; reject partial or drifted forms."""

    placeholders = ",".join("?" for _ in OPPORTUNITY_ENRICHMENT_SCHEMA_OBJECTS)
    try:
        rows = cursor.execute(
            "SELECT CAST(type AS BLOB), CAST(name AS BLOB), "
            "CAST(tbl_name AS BLOB), CAST(sql AS BLOB) "
            "FROM main.sqlite_schema WHERE name IN (" + placeholders + ") "
            "ORDER BY name",
            OPPORTUNITY_ENRICHMENT_SCHEMA_OBJECTS,
        ).fetchall()
    except (AttributeError, TypeError, ValueError, sqlite3.Error):
        raise OpportunityEnrichmentSchemaError() from None
    if not rows:
        return False
    if len(rows) != len(_EXPECTED_OBJECTS):
        raise OpportunityEnrichmentSchemaError()

    actual = {}
    try:
        for row in rows:
            if (
                type(row) is not tuple
                or len(row) != 4
                or any(type(value) is not bytes for value in row[:3])
                or (row[3] is not None and type(row[3]) is not bytes)
            ):
                raise OpportunityEnrichmentSchemaError()
            kind, name, table_name = (
                value.decode("utf-8", "strict") for value in row[:3]
            )
            sql = row[3].decode("utf-8", "strict") if row[3] is not None else None
            actual[name] = (kind, table_name, sql)
    except (UnicodeError, ValueError):
        raise OpportunityEnrichmentSchemaError() from None
    if set(actual) != set(_EXPECTED_OBJECTS):
        raise OpportunityEnrichmentSchemaError()
    for name, (expected_kind, expected_table, expected_sql) in _EXPECTED_OBJECTS.items():
        kind, table_name, sql = actual[name]
        if (
            kind != expected_kind
            or table_name != expected_table
            or (
                sql is not None
                and expected_sql is not None
                and _normalize_sql(sql) != _normalize_sql(expected_sql)
            )
            or ((sql is None) != (expected_sql is None))
        ):
            raise OpportunityEnrichmentSchemaError()
    return True


def _normalize_sql(value: str) -> str:
    normalized = re.sub(
        r"\s+", " ", str(value or "").strip().rstrip(";")
    ).casefold()
    return re.sub(r"^(create (?:table|index)) if not exists ", r"\1 ", normalized)
