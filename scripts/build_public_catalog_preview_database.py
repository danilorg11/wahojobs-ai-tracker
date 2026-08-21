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
from wahojobs import public_job_identity, public_job_page
from wahojobs.public_job_identity_schema import MIGRATION_PATH as PUBLIC_JOB_MIGRATION_PATH
from wahojobs.public_job_release import (
    PublicJobReleaseError,
    build_preview_release,
    canonical_preview_release_json,
    load_preview_binding_publications,
    load_preview_registry_artifact,
)
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


def build_public_catalog_preview_database(
    source_path,
    output_path,
    *,
    registry_path,
    bindings_path,
    release_manifest_path,
    now=None,
):
    source = _existing_absolute_file(source_path)
    output = _new_absolute_path(output_path)
    release_manifest_output = _new_absolute_path(release_manifest_path)
    if output == release_manifest_output:
        raise PublicCatalogProjectionError("output_unavailable")
    try:
        registry_artifact = load_preview_registry_artifact(registry_path)
        bindings = load_preview_binding_publications(bindings_path)
    except PublicJobReleaseError:
        raise PublicCatalogProjectionError("public_registry_invalid") from None
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
            binding_rows = _resolve_binding_rows(source_connection, bindings)
            detail_jobs = _load_bound_detail_jobs(
                source_connection,
                binding_rows,
                observed_at,
            )
            schema = (ROOT / "wahojobs" / "db" / "schema.sql").read_text(
                encoding="utf-8"
            )
            target.executescript(schema)
            _copy_projection(source_connection, target, jobs, detail_jobs)
            target.executescript(PUBLIC_JOB_MIGRATION_PATH.read_text(encoding="utf-8"))
            public_job_identity.import_public_job_registry_artifact(
                target,
                registry_artifact,
            )
            for publication in bindings:
                public_job_identity.bind_imported_public_job(
                    target,
                    publication.public_job_id,
                    binding_rows[publication.public_job_id]["canonical_opportunity_id"],
                    now=observed_at,
                )
            public_job_identity.assert_public_job_identity_consistent(target)
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
        digest = _sha256_file(temporary)
        release = build_preview_release(
            database_sha256=digest,
            registry_artifact=registry_artifact,
            bindings=bindings,
        )
        result = {
            "version": 2,
            "database_sha256": digest,
            "release_id": release.release_id,
            "registry_sha256": release.registry_sha256,
            "company_count": projection_counts["companies"],
            "opportunity_count": projection_counts["canonical_opportunities"],
            "job_count": projection_counts["jobs"],
            "catalog_job_count": len(jobs),
            "published_detail_count": len(release.published_details),
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
            ],
        }
        _write_create_only(
            release_manifest_output,
            canonical_preview_release_json(release),
        )
        try:
            temporary.replace(output)
        except Exception:
            try:
                release_manifest_output.unlink()
            except OSError:
                pass
            raise
        return result
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except PublicCatalogProjectionError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, PublicJobReleaseError):
        raise PublicCatalogProjectionError("projection_unavailable") from None
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _copy_projection(source, target, jobs, detail_jobs):
    all_jobs = tuple(jobs) + tuple(detail_jobs)
    job_ids = sorted({int(job["job_id"]) for job in all_jobs})
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
        {
            int(job["source_run_id"])
            for job in all_jobs
            if job.get("source_run_id") is not None
        }
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


def _resolve_binding_rows(source, bindings):
    result = {}
    for publication in bindings:
        rows = source.execute(
            "SELECT id AS canonical_opportunity_id, canonical_key "
            "FROM canonical_opportunities WHERE canonical_key = ?",
            (publication.canonical_key,),
        ).fetchall()
        if len(rows) != 1:
            raise PublicCatalogProjectionError("public_binding_unresolved")
        result[publication.public_job_id] = dict(rows[0])
    return result


def _load_bound_detail_jobs(source, binding_rows, observed_at):
    result = []
    seen_job_ids = set()
    for row in binding_rows.values():
        canonical_id = int(row["canonical_opportunity_id"])
        job = public_job_page.load_public_job(
            source,
            public_job_page.public_job_path(canonical_id),
            now=observed_at,
        )
        if job is None:
            raise PublicCatalogProjectionError("published_detail_unavailable")
        if int(job["job_id"]) not in seen_job_ids:
            result.append(job)
            seen_job_ids.add(int(job["job_id"]))
    return tuple(result)


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


def _write_create_only(path, payload):
    if type(payload) is not bytes or not payload:
        raise PublicCatalogProjectionError("manifest_unavailable")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--bindings", required=True)
    parser.add_argument("--release-manifest", required=True)
    parser.add_argument(
        "--observed-at",
        help="fixed timezone-aware ISO-8601 projection time for reproducible release builds",
    )
    arguments = parser.parse_args(argv)
    manifest_path = _new_absolute_path(arguments.manifest)
    try:
        result = build_public_catalog_preview_database(
            arguments.source,
            arguments.output,
            registry_path=arguments.registry,
            bindings_path=arguments.bindings,
            release_manifest_path=arguments.release_manifest,
            now=(
                datetime.fromisoformat(arguments.observed_at)
                if arguments.observed_at
                else None
            ),
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
