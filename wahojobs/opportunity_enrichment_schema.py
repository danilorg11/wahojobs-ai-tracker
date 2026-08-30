"""Exact additive schema contract for durable opportunity enrichment.

The authentication database has a separately attested closed schema.  These
objects are an optional, narrowly sanctioned domain extension: either all are
absent or all must match this exact contract.
"""

from __future__ import annotations

import re
import sqlite3


_PRE_VERSIONING_OPPORTUNITY_ENRICHMENTS_STATEMENT = """
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
    """

_PRE_VERSIONING_OPPORTUNITY_ENRICHMENT_RUNS_STATEMENT = """
    CREATE TABLE IF NOT EXISTS opportunity_enrichment_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      canonical_opportunity_id INTEGER NOT NULL,
      input_sha256 TEXT NOT NULL,
      outcome TEXT NOT NULL CHECK (outcome IN ('succeeded', 'failed')),
      model_provider TEXT NOT NULL,
      model_name TEXT NOT NULL,
      prompt_version TEXT NOT NULL,
      response_id TEXT,
      input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
      output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
      total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
      estimated_cost_usd REAL CHECK (
        estimated_cost_usd IS NULL OR estimated_cost_usd >= 0
      ),
      error_type TEXT,
      started_at TEXT NOT NULL,
      finished_at TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY (canonical_opportunity_id) REFERENCES canonical_opportunities(id)
        ON DELETE CASCADE
    )
    """

_SEMANTIC_AUTHORITY_COLUMN_PATTERN = re.compile(
    r"semantic_authority_state\s+TEXT\s+NOT\s+NULL\s+"
    r"DEFAULT\s+'legacy_accepted'\s+CHECK\s*\(\s*"
    r"semantic_authority_state\s+IN\s*\(\s*"
    r"'legacy_accepted'\s*,\s*'pending'\s*,\s*"
    r"'versioned_accepted'\s*\)\s*\)",
    re.IGNORECASE,
)


OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS job_source_contents (
      job_id INTEGER PRIMARY KEY,
      provider TEXT NOT NULL,
      source_type TEXT NOT NULL,
      source_url TEXT NOT NULL,
      external_id TEXT,
      body TEXT,
      body_format TEXT CHECK (
        body_format IS NULL
        OR body_format IN ('text/plain', 'text/html', 'text/markdown')
      ),
      metadata_json TEXT NOT NULL DEFAULT '{}',
      material_content_sha256 TEXT NOT NULL,
      source_updated_at TEXT,
      first_captured_at TEXT NOT NULL,
      last_captured_at TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      CHECK (
        (body IS NULL AND body_format IS NULL)
        OR (body IS NOT NULL AND body_format IS NOT NULL)
      )
    )
    """,
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
      semantic_input_version TEXT,
      derivation_fingerprint TEXT,
      FOREIGN KEY (canonical_opportunity_id) REFERENCES canonical_opportunities(id)
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS opportunity_enrichment_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      canonical_opportunity_id INTEGER NOT NULL,
      input_sha256 TEXT NOT NULL,
      outcome TEXT NOT NULL CHECK (outcome IN ('succeeded', 'failed')),
      model_provider TEXT NOT NULL,
      model_name TEXT NOT NULL,
      prompt_version TEXT NOT NULL,
      response_id TEXT,
      input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
      output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
      total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
      estimated_cost_usd REAL CHECK (
        estimated_cost_usd IS NULL OR estimated_cost_usd >= 0
      ),
      error_type TEXT,
      started_at TEXT NOT NULL,
      finished_at TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      semantic_input_version TEXT,
      derivation_fingerprint TEXT,
      FOREIGN KEY (canonical_opportunity_id) REFERENCES canonical_opportunities(id)
        ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS opportunity_enrichment_run_diagnostics (
      run_id INTEGER PRIMARY KEY,
      diagnostic_json TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY (run_id) REFERENCES opportunity_enrichment_runs(id)
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
    """
    CREATE INDEX IF NOT EXISTS idx_job_source_contents_material_hash
    ON job_source_contents(material_content_sha256)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_opportunity_enrichment_runs_canonical
    ON opportunity_enrichment_runs(canonical_opportunity_id, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS job_source_content_captures (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      job_id INTEGER NOT NULL,
      crawl_run_id INTEGER,
      provider TEXT NOT NULL,
      source_type TEXT NOT NULL,
      source_url TEXT NOT NULL,
      external_id TEXT,
      body TEXT,
      body_format TEXT CHECK (
        body_format IS NULL
        OR body_format IN ('text/plain', 'text/html', 'text/markdown')
      ),
      metadata_json TEXT NOT NULL DEFAULT '{}',
      material_content_sha256 TEXT NOT NULL,
      semantic_job_fields_json TEXT NOT NULL,
      semantic_material_sha256 TEXT NOT NULL,
      source_updated_at TEXT,
      source_timestamp_status TEXT NOT NULL CHECK (
        source_timestamp_status IN ('absent', 'valid', 'invalid')
      ),
      capture_quality TEXT NOT NULL CHECK (
        capture_quality IN (
          'healthy_body', 'metadata_only', 'empty', 'blocked_or_error'
        )
      ),
      provider_outcome TEXT NOT NULL CHECK (
        provider_outcome IN ('success', 'partial', 'anomalous', 'contract_drift')
      ),
      used_sample_data INTEGER NOT NULL CHECK (used_sample_data IN (0, 1)),
      snapshot_complete INTEGER NOT NULL CHECK (snapshot_complete IN (0, 1)),
      pagination_complete INTEGER NOT NULL CHECK (pagination_complete IN (0, 1)),
      empty_snapshot_validated INTEGER NOT NULL CHECK (
        empty_snapshot_validated IN (0, 1)
      ),
      raw_record_count INTEGER NOT NULL CHECK (raw_record_count >= 0),
      normalized_record_count INTEGER NOT NULL CHECK (normalized_record_count >= 0),
      candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
      rejected_record_count INTEGER NOT NULL CHECK (rejected_record_count >= 0),
      payload_shape TEXT NOT NULL DEFAULT '',
      schema_fingerprint TEXT NOT NULL DEFAULT '',
      record_promotion_contract_id TEXT NOT NULL DEFAULT '',
      body_observation TEXT NOT NULL DEFAULT 'not_observed' CHECK (
        body_observation IN ('present', 'explicitly_empty', 'not_observed')
      ),
      authority_evidence_json TEXT NOT NULL DEFAULT '{}',
      capture_contract_version TEXT NOT NULL,
      promotion_policy_version TEXT NOT NULL,
      promotion_decision TEXT NOT NULL CHECK (
        promotion_decision IN (
          'promoted', 'confirmed', 'held_degraded',
          'held_non_authoritative', 'held_source_conflict'
        )
      ),
      decision_reasons_json TEXT NOT NULL DEFAULT '[]',
      observed_at TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY (job_id) REFERENCES jobs(id),
      FOREIGN KEY (crawl_run_id) REFERENCES crawl_runs(id) ON DELETE RESTRICT,
      UNIQUE (id, job_id),
      CHECK (
        (body IS NULL AND body_format IS NULL)
        OR (body IS NOT NULL AND body_format IS NOT NULL)
      )
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_source_content_acceptances (
      job_id INTEGER PRIMARY KEY,
      accepted_capture_id INTEGER NOT NULL,
      promotion_policy_version TEXT NOT NULL,
      accepted_at TEXT NOT NULL,
      last_confirmed_at TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY (job_id) REFERENCES jobs(id),
      FOREIGN KEY (accepted_capture_id, job_id)
        REFERENCES job_source_content_captures(id, job_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_job_source_content_captures_job
    ON job_source_content_captures(job_id, id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_job_source_content_captures_crawl_run
    ON job_source_content_captures(crawl_run_id, id)
    """,
)

OPPORTUNITY_ENRICHMENT_SCHEMA_OBJECTS = (
    "idx_job_source_content_captures_crawl_run",
    "idx_job_source_content_captures_job",
    "idx_job_source_contents_material_hash",
    "idx_opportunity_enrichment_overrides_canonical",
    "idx_opportunity_enrichment_runs_canonical",
    "idx_opportunity_enrichments_status",
    "job_source_content_acceptances",
    "job_source_content_captures",
    "job_source_contents",
    "opportunity_enrichment_run_diagnostics",
    "opportunity_enrichment_overrides",
    "opportunity_enrichment_runs",
    "opportunity_enrichments",
    "sqlite_autoindex_job_source_content_captures_1",
    "sqlite_autoindex_opportunity_enrichment_overrides_1",
)

_RECORD_PROMOTION_CAPTURE_COLUMN_SQL = """
      record_promotion_contract_id TEXT NOT NULL DEFAULT '',
      body_observation TEXT NOT NULL DEFAULT 'not_observed' CHECK (
        body_observation IN ('present', 'explicitly_empty', 'not_observed')
      ),
      authority_evidence_json TEXT NOT NULL DEFAULT '{}',
"""
_PRE_RECORD_PROMOTION_CAPTURE_STATEMENT = (
    OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[9].replace(
        _RECORD_PROMOTION_CAPTURE_COLUMN_SQL,
        "",
    )
)
_ALTERED_RECORD_PROMOTION_CAPTURE_STATEMENT = (
    _PRE_RECORD_PROMOTION_CAPTURE_STATEMENT.replace(
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,",
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "record_promotion_contract_id TEXT NOT NULL DEFAULT '', "
        "body_observation TEXT NOT NULL DEFAULT 'not_observed' CHECK ("
        "body_observation IN ('present', 'explicitly_empty', 'not_observed')), "
        "authority_evidence_json TEXT NOT NULL DEFAULT '{}',",
    )
)

_EXPECTED_OBJECTS = {
    "job_source_contents": (
        "table",
        "job_source_contents",
        OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[0],
    ),
    "opportunity_enrichments": (
        "table",
        "opportunity_enrichments",
        OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[1],
    ),
    "opportunity_enrichment_overrides": (
        "table",
        "opportunity_enrichment_overrides",
        OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[4],
    ),
    "opportunity_enrichment_runs": (
        "table",
        "opportunity_enrichment_runs",
        OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[2],
    ),
    "opportunity_enrichment_run_diagnostics": (
        "table",
        "opportunity_enrichment_run_diagnostics",
        OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[3],
    ),
    "idx_opportunity_enrichments_status": (
        "index",
        "opportunity_enrichments",
        OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[5],
    ),
    "idx_opportunity_enrichment_overrides_canonical": (
        "index",
        "opportunity_enrichment_overrides",
        OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[6],
    ),
    "idx_job_source_contents_material_hash": (
        "index",
        "job_source_contents",
        OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[7],
    ),
    "idx_opportunity_enrichment_runs_canonical": (
        "index",
        "opportunity_enrichment_runs",
        OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[8],
    ),
    "job_source_content_captures": (
        "table",
        "job_source_content_captures",
        OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[9],
    ),
    "job_source_content_acceptances": (
        "table",
        "job_source_content_acceptances",
        OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[10],
    ),
    "idx_job_source_content_captures_job": (
        "index",
        "job_source_content_captures",
        OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[11],
    ),
    "idx_job_source_content_captures_crawl_run": (
        "index",
        "job_source_content_captures",
        OPPORTUNITY_ENRICHMENT_SCHEMA_STATEMENTS[12],
    ),
    "sqlite_autoindex_job_source_content_captures_1": (
        "index",
        "job_source_content_captures",
        None,
    ),
    "sqlite_autoindex_opportunity_enrichment_overrides_1": (
        "index",
        "opportunity_enrichment_overrides",
        None,
    ),
}

_PRE_RECORD_PROMOTION_EXPECTED_OBJECTS = dict(_EXPECTED_OBJECTS)
_PRE_RECORD_PROMOTION_EXPECTED_OBJECTS["job_source_content_captures"] = (
    "table",
    "job_source_content_captures",
    _PRE_RECORD_PROMOTION_CAPTURE_STATEMENT,
)
_ALTERED_RECORD_PROMOTION_EXPECTED_OBJECTS = dict(_EXPECTED_OBJECTS)
_ALTERED_RECORD_PROMOTION_EXPECTED_OBJECTS["job_source_content_captures"] = (
    "table",
    "job_source_content_captures",
    _ALTERED_RECORD_PROMOTION_CAPTURE_STATEMENT,
)

_SOURCE_CAPTURE_SCHEMA_OBJECTS = frozenset(
    {
        "idx_job_source_content_captures_crawl_run",
        "idx_job_source_content_captures_job",
        "job_source_content_acceptances",
        "job_source_content_captures",
        "sqlite_autoindex_job_source_content_captures_1",
    }
)
# The enrichment-versioning extension was deployed before append-only source
# capture. Its exact shape remains accepted so a normal ensure can add the two
# tables and indexes without touching legacy accepted content.
_PRE_CAPTURE_EXPECTED_OBJECTS = {
    name: expected
    for name, expected in _EXPECTED_OBJECTS.items()
    if name not in _SOURCE_CAPTURE_SCHEMA_OBJECTS
}

_PRE_VERSIONING_EXPECTED_OBJECTS = dict(_EXPECTED_OBJECTS)
_PRE_VERSIONING_EXPECTED_OBJECTS["opportunity_enrichments"] = (
    "table",
    "opportunity_enrichments",
    _PRE_VERSIONING_OPPORTUNITY_ENRICHMENTS_STATEMENT,
)
_PRE_VERSIONING_EXPECTED_OBJECTS["opportunity_enrichment_runs"] = (
    "table",
    "opportunity_enrichment_runs",
    _PRE_VERSIONING_OPPORTUNITY_ENRICHMENT_RUNS_STATEMENT,
)

_PRE_CAPTURE_PRE_VERSIONING_EXPECTED_OBJECTS = dict(
    _PRE_CAPTURE_EXPECTED_OBJECTS
)
_PRE_CAPTURE_PRE_VERSIONING_EXPECTED_OBJECTS["opportunity_enrichments"] = (
    "table",
    "opportunity_enrichments",
    _PRE_VERSIONING_OPPORTUNITY_ENRICHMENTS_STATEMENT,
)
_PRE_CAPTURE_PRE_VERSIONING_EXPECTED_OBJECTS["opportunity_enrichment_runs"] = (
    "table",
    "opportunity_enrichment_runs",
    _PRE_VERSIONING_OPPORTUNITY_ENRICHMENT_RUNS_STATEMENT,
)

_CURRENT_PRIOR_RICH_EXPECTED_OBJECTS = {
    name: expected
    for name, expected in _EXPECTED_OBJECTS.items()
    if name != "opportunity_enrichment_run_diagnostics"
}

_PRE_CAPTURE_CURRENT_PRIOR_RICH_EXPECTED_OBJECTS = {
    name: expected
    for name, expected in _PRE_CAPTURE_EXPECTED_OBJECTS.items()
    if name != "opportunity_enrichment_run_diagnostics"
}

_CURRENT_LEGACY_EXPECTED_OBJECTS = {
    name: _EXPECTED_OBJECTS[name]
    for name in (
        "idx_opportunity_enrichment_overrides_canonical",
        "idx_opportunity_enrichments_status",
        "opportunity_enrichment_overrides",
        "opportunity_enrichments",
        "sqlite_autoindex_opportunity_enrichment_overrides_1",
    )
}

# Rich source persistence predates the sanitized run-diagnostics table.  Its
# exact shape remains accepted so the next normal schema ensure can add the
# companion table without rewriting existing run history.
_PRIOR_RICH_EXPECTED_OBJECTS = {
    name: expected
    for name, expected in _PRE_VERSIONING_EXPECTED_OBJECTS.items()
    if name != "opportunity_enrichment_run_diagnostics"
}

_PRE_CAPTURE_PRIOR_RICH_EXPECTED_OBJECTS = {
    name: expected
    for name, expected in _PRE_CAPTURE_PRE_VERSIONING_EXPECTED_OBJECTS.items()
    if name != "opportunity_enrichment_run_diagnostics"
}

# The exact V2 extension shape already deployed before rich source persistence.
# It remains an accepted read-only predecessor so upgraded runtimes can open an
# existing database; the next normal schema ensure adds the missing objects.
_LEGACY_EXPECTED_OBJECTS = {
    name: _PRE_VERSIONING_EXPECTED_OBJECTS[name]
    for name in (
        "idx_opportunity_enrichment_overrides_canonical",
        "idx_opportunity_enrichments_status",
        "opportunity_enrichment_overrides",
        "opportunity_enrichments",
        "sqlite_autoindex_opportunity_enrichment_overrides_1",
    )
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
    semantic_authority_present = _attest_semantic_authority_column(cursor)
    if not rows:
        if semantic_authority_present:
            raise OpportunityEnrichmentSchemaError()
        return False

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
    candidates = [
        expected_objects
        for expected_objects in (
            _EXPECTED_OBJECTS,
            _PRE_RECORD_PROMOTION_EXPECTED_OBJECTS,
            _ALTERED_RECORD_PROMOTION_EXPECTED_OBJECTS,
            _PRE_VERSIONING_EXPECTED_OBJECTS,
            _PRE_CAPTURE_EXPECTED_OBJECTS,
            _PRE_CAPTURE_PRE_VERSIONING_EXPECTED_OBJECTS,
            _CURRENT_PRIOR_RICH_EXPECTED_OBJECTS,
            _PRIOR_RICH_EXPECTED_OBJECTS,
            _PRE_CAPTURE_CURRENT_PRIOR_RICH_EXPECTED_OBJECTS,
            _PRE_CAPTURE_PRIOR_RICH_EXPECTED_OBJECTS,
            _CURRENT_LEGACY_EXPECTED_OBJECTS,
            _LEGACY_EXPECTED_OBJECTS,
        )
        if set(actual) == set(expected_objects)
    ]
    if not candidates:
        raise OpportunityEnrichmentSchemaError()
    for expected_objects in candidates:
        matches = True
        for name, (
            expected_kind,
            expected_table,
            expected_sql,
        ) in expected_objects.items():
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
                matches = False
                break
        if matches:
            capture_schema_present = (
                "job_source_content_captures" in expected_objects
            )
            if semantic_authority_present != capture_schema_present:
                raise OpportunityEnrichmentSchemaError()
            return True
    raise OpportunityEnrichmentSchemaError()


def _attest_semantic_authority_column(cursor) -> bool:
    try:
        sql_row = cursor.execute(
            "SELECT CAST(sql AS BLOB) FROM main.sqlite_schema "
            "WHERE type = 'table' AND name = 'jobs'"
        ).fetchone()
        columns = cursor.execute("PRAGMA main.table_xinfo(jobs)").fetchall()
    except (AttributeError, TypeError, ValueError, sqlite3.Error):
        raise OpportunityEnrichmentSchemaError() from None
    if (
        type(sql_row) is not tuple
        or len(sql_row) != 1
        or type(sql_row[0]) is not bytes
        or any(type(row) is not tuple or len(row) != 7 for row in columns)
    ):
        raise OpportunityEnrichmentSchemaError()
    try:
        sql = sql_row[0].decode("utf-8", "strict")
    except UnicodeError:
        raise OpportunityEnrichmentSchemaError() from None
    semantic_columns = [
        row for row in columns if row[1] == "semantic_authority_state"
    ]
    matches = list(_SEMANTIC_AUTHORITY_COLUMN_PATTERN.finditer(sql))
    if not semantic_columns and not matches:
        return False
    if (
        len(semantic_columns) != 1
        or len(matches) != 1
        or semantic_columns[0][2].casefold() != "text"
        or semantic_columns[0][3] != 1
        or semantic_columns[0][4] != "'legacy_accepted'"
        or semantic_columns[0][5] != 0
        or semantic_columns[0][6] != 0
    ):
        raise OpportunityEnrichmentSchemaError()
    suffix = sql[matches[0].end() :]
    if not suffix.lstrip().startswith(","):
        raise OpportunityEnrichmentSchemaError()
    return True


def normalize_opportunity_enrichment_closed_schema_sql(
    object_name: str,
    sql: str | None,
) -> str | None:
    """Remove only the separately attested optional jobs-column extension."""

    if object_name != "jobs" or sql is None:
        return sql
    matches = list(_SEMANTIC_AUTHORITY_COLUMN_PATTERN.finditer(sql))
    if not matches:
        return sql
    if len(matches) != 1:
        raise OpportunityEnrichmentSchemaError()
    match = matches[0]
    line_start = sql.rfind("\n", 0, match.start()) + 1
    prefix = sql[line_start : match.start()]
    suffix_start = match.end()
    while suffix_start < len(sql) and sql[suffix_start] in " \t":
        suffix_start += 1
    if suffix_start >= len(sql) or sql[suffix_start] != ",":
        raise OpportunityEnrichmentSchemaError()

    if not prefix.strip():
        suffix_start += 1
        if sql[suffix_start : suffix_start + 2] == "\r\n":
            suffix_start += 2
        elif suffix_start < len(sql) and sql[suffix_start] == "\n":
            suffix_start += 1
        return sql[:line_start] + sql[suffix_start:]

    leading = match.start()
    while leading > line_start and sql[leading - 1] in " \t":
        leading -= 1
    if leading <= line_start or sql[leading - 1] != ",":
        raise OpportunityEnrichmentSchemaError()
    return sql[: leading - 1] + sql[match.end() :]


def _normalize_sql(value: str) -> str:
    normalized = re.sub(
        r"\s+", " ", str(value or "").strip().rstrip(";")
    ).casefold()
    return re.sub(r"^(create (?:table|index)) if not exists ", r"\1 ", normalized)
