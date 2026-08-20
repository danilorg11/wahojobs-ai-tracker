"""Build a minimal public-only SQLite projection for the preview catalog."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wahojobs.public_catalog_origin import EXPECTED_EMPTY_TABLES, EXPECTED_TABLES
from wahojobs.public_jobs_catalog import load_public_jobs


class PublicCatalogProjectionError(Exception):
    pass


COMPANY_COLUMNS = (
    "id", "name", "slug", "careers_url", "source_tier", "inventory_model",
    "market_count_policy", "created_at", "updated_at",
)
CANONICAL_COLUMNS = (
    "id", "company_id", "canonical_key", "canonical_title", "normalized_title",
    "source_category", "language", "language_locale", "first_seen_at",
    "last_seen_at", "is_active", "variant_count", "created_at", "updated_at",
)
JOB_COLUMNS = (
    "id", "company_id", "canonical_opportunity_id", "external_id", "title",
    "location", "department", "expertise", "commitment", "url", "source_hash",
    "opportunity_kind", "availability_basis", "include_in_live_market_estimate",
    "first_seen_at", "last_seen_at", "is_active", "removed_at", "created_at",
    "updated_at",
)
CRAWL_RUN_COLUMNS = (
    "id", "company_id", "status", "started_at", "finished_at", "jobs_found_count",
    "jobs_new_count", "jobs_reactivated_count", "jobs_updated_count",
    "jobs_removed_count", "used_sample_data", "error_message", "created_at",
)
ENRICHMENT_COLUMNS = (
    "canonical_opportunity_id", "schema_version", "taxonomy_version",
    "extractor_version", "input_sha256", "status", "automatic_document_json",
    "model_provider", "model_name", "prompt_version", "generated_at", "created_at",
    "updated_at",
)


def build_public_catalog_preview_database(source_path, output_path, *, now=None):
    source = _existing_absolute_file(source_path)
    output = _new_absolute_path(output_path)
    observed_at = _utc(now or datetime.now(timezone.utc))
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".public-catalog-", suffix=".sqlite3", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        source_connection = sqlite3.connect(
            source.as_uri() + "?mode=ro", uri=True, isolation_level=""
        )
        source_connection.row_factory = sqlite3.Row
        source_connection.execute("PRAGMA foreign_keys = ON")
        source_connection.execute("PRAGMA query_only = ON")
        target = sqlite3.connect(temporary)
        target.row_factory = sqlite3.Row
        target.execute("PRAGMA foreign_keys = ON")
        try:
            source_connection.execute("BEGIN")
            jobs = load_public_jobs(source_connection, now=observed_at)
            if not jobs:
                raise PublicCatalogProjectionError("public_inventory_empty")
            schema = (ROOT / "wahojobs" / "db" / "schema.sql").read_text(
                encoding="utf-8"
            )
            target.executescript(schema)
            _copy_projection(source_connection, target, jobs)
            target.commit()
            _attest_target(target)
            projection_counts = {
                table: int(
                    target.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )
                for table in (
                    "companies",
                    "canonical_opportunities",
                    "jobs",
                    "opportunity_enrichments",
                )
            }
        finally:
            if source_connection.in_transaction:
                source_connection.rollback()
            source_connection.close()
            target.close()
        os.chmod(temporary, 0o600)
        temporary.replace(output)
        digest = _sha256_file(output)
        return {
            "version": 1,
            "database_sha256": digest,
            "company_count": projection_counts["companies"],
            "opportunity_count": projection_counts["canonical_opportunities"],
            "job_count": projection_counts["jobs"],
            "enrichment_count": projection_counts["opportunity_enrichments"],
            "copied_tables": sorted(
                table for table in EXPECTED_TABLES if table not in EXPECTED_EMPTY_TABLES
            ),
            "empty_tables": sorted(EXPECTED_EMPTY_TABLES),
            "excluded_data_families": [
                "accounts",
                "sessions",
                "profiles",
                "invitations",
                "workos",
                "source_bodies",
                "public_job_identities",
            ],
        }
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except PublicCatalogProjectionError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError):
        raise PublicCatalogProjectionError("projection_unavailable") from None
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _copy_projection(source, target, jobs):
    job_ids = sorted({int(job["job_id"]) for job in jobs})
    identifier_placeholders = ",".join("?" for _ in job_ids)
    links = source.execute(
        "SELECT id, company_id, canonical_opportunity_id FROM jobs "
        f"WHERE id IN ({identifier_placeholders}) ORDER BY id",
        tuple(job_ids),
    ).fetchall()
    if len(links) != len(job_ids):
        raise PublicCatalogProjectionError("projection_incomplete")
    canonical_ids = sorted({int(row["canonical_opportunity_id"]) for row in links})
    company_ids = sorted({int(row["company_id"]) for row in links})
    source_run_ids = sorted(
        {int(job["source_run_id"]) for job in jobs if job.get("source_run_id") is not None}
    )
    _copy_rows(source, target, "companies", COMPANY_COLUMNS, "id", company_ids)
    _copy_rows(
        source, target, "canonical_opportunities", CANONICAL_COLUMNS, "id", canonical_ids
    )
    _copy_rows(source, target, "jobs", JOB_COLUMNS, "id", job_ids)
    _copy_rows(source, target, "crawl_runs", CRAWL_RUN_COLUMNS, "id", source_run_ids)
    _copy_rows(
        source,
        target,
        "opportunity_enrichments",
        ENRICHMENT_COLUMNS,
        "canonical_opportunity_id",
        canonical_ids,
        optional=True,
    )


def _copy_rows(source, target, table, columns, key, identifiers, *, optional=False):
    if not identifiers:
        return
    identifier_placeholders = ",".join("?" for _ in identifiers)
    value_placeholders = ",".join("?" for _ in columns)
    quoted = ",".join(f'"{column}"' for column in columns)
    try:
        rows = source.execute(
            f'SELECT {quoted} FROM "{table}" WHERE "{key}" IN ({identifier_placeholders}) '
            f'ORDER BY "{key}"',
            tuple(identifiers),
        ).fetchall()
    except sqlite3.Error:
        if optional:
            return
        raise
    if not optional and len(rows) != len(identifiers):
        raise PublicCatalogProjectionError("projection_incomplete")
    target.executemany(
        f'INSERT INTO "{table}" ({quoted}) VALUES ({value_placeholders})',
        [tuple(row[column] for column in columns) for row in rows],
    )


def _attest_target(connection):
    tables = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    )
    if tables != EXPECTED_TABLES:
        raise PublicCatalogProjectionError("projection_schema_invalid")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise PublicCatalogProjectionError("projection_integrity_invalid")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise PublicCatalogProjectionError("projection_foreign_keys_invalid")
    for table in EXPECTED_EMPTY_TABLES:
        if connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]:
            raise PublicCatalogProjectionError("private_data_present")


def _existing_absolute_file(value):
    path = Path(value)
    resolved = path.resolve(strict=True)
    if not path.is_absolute() or str(path) != str(resolved) or not resolved.is_file():
        raise PublicCatalogProjectionError("source_unavailable")
    return resolved


def _new_absolute_path(value):
    path = Path(value)
    if not path.is_absolute() or path.exists():
        raise PublicCatalogProjectionError("output_unavailable")
    parent = path.parent.resolve(strict=True)
    resolved = parent / path.name
    if str(path) != str(resolved):
        raise PublicCatalogProjectionError("output_unavailable")
    return resolved


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise PublicCatalogProjectionError("invalid_time")
    return value.astimezone(timezone.utc)


def _sha256_file(path):
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--manifest", required=True)
    arguments = parser.parse_args(argv)
    manifest_path = _new_absolute_path(arguments.manifest)
    try:
        result = build_public_catalog_preview_database(
            arguments.source, arguments.output
        )
        manifest_path.write_text(
            json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(manifest_path, 0o600)
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except Exception:
        print("Public catalog preview projection failed safely.", file=sys.stderr)
        return 1
    print(
        "PUBLIC_CATALOG_PROJECTION_OK "
        f"companies={result['company_count']} "
        f"opportunities={result['opportunity_count']} jobs={result['job_count']} "
        f"sha256={result['database_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
