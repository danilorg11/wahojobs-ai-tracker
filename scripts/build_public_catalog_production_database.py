"""Build the offline Production Exact-Route Release v1 public projection."""

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

from scripts.build_public_catalog_preview_database import (
    PublicCatalogProjectionError,
    _attest_target,
    _copy_projection,
    _existing_absolute_file,
    _load_bound_detail_jobs,
    _new_absolute_path,
    _resolve_binding_rows,
    _sha256_file,
    _utc,
    _write_create_only,
)
from wahojobs import public_job_identity
from wahojobs.public_job_identity_schema import MIGRATION_PATH as PUBLIC_JOB_MIGRATION_PATH
from wahojobs.public_job_release import (
    HANDSHAKE_CANARY_CANONICAL_KEY,
    HANDSHAKE_CANARY_PUBLIC_JOB_ID,
    HANDSHAKE_CANARY_PUBLIC_JOB_PATH,
    KARL_PUBLIC_JOB_PATH,
    PublicJobReleaseError,
    build_production_release,
    canonical_production_release_json,
    load_production_binding_publications,
    load_production_registry_artifact,
)
from wahojobs.public_jobs_catalog import load_public_jobs


PROJECTION_FORMAT = "wahojobs-public-catalog-production-projection-v1"
PUBLIC_ORIGIN = "https://www.wahojobs.com"
OWNED_ROUTES = ("/jobs", HANDSHAKE_CANARY_PUBLIC_JOB_PATH)


def build_public_catalog_production_database(
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
        registry_artifact = load_production_registry_artifact(registry_path)
        bindings = load_production_binding_publications(bindings_path)
    except PublicJobReleaseError:
        raise PublicCatalogProjectionError("public_registry_invalid") from None
    observed_at = _utc(now or datetime.now(timezone.utc))
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".public-catalog-production-", suffix=".sqlite3", dir=output.parent
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
            canary = binding_rows.get(HANDSHAKE_CANARY_PUBLIC_JOB_ID)
            if (
                canary is None
                or canary.get("canonical_key") != HANDSHAKE_CANARY_CANONICAL_KEY
                or int(canary["canonical_opportunity_id"])
                not in {int(job["canonical_opportunity_id"]) for job in jobs}
            ):
                raise PublicCatalogProjectionError("production_canary_not_catalog_eligible")
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
            public_job_identity.bind_imported_public_job(
                target,
                HANDSHAKE_CANARY_PUBLIC_JOB_ID,
                int(canary["canonical_opportunity_id"]),
                now=observed_at,
            )
            public_job_identity.assert_public_job_identity_consistent(target)
            target.commit()
            _attest_target(target)
            published_rows = target.execute(
                "SELECT path.path, path.public_job_id, canonical.canonical_key "
                "FROM public_job_paths path "
                "JOIN public_job_bindings binding "
                "ON binding.public_job_id = path.public_job_id "
                "JOIN canonical_opportunities canonical "
                "ON canonical.id = binding.canonical_opportunity_id"
            ).fetchall()
            if [tuple(row) for row in published_rows] != [
                (
                    HANDSHAKE_CANARY_PUBLIC_JOB_PATH,
                    HANDSHAKE_CANARY_PUBLIC_JOB_ID,
                    HANDSHAKE_CANARY_CANONICAL_KEY,
                )
            ]:
                raise PublicCatalogProjectionError("production_release_scope_invalid")
            if target.execute(
                "SELECT COUNT(*) FROM public_job_paths WHERE path = ?",
                (KARL_PUBLIC_JOB_PATH,),
            ).fetchone()[0]:
                raise PublicCatalogProjectionError("production_release_scope_invalid")
            projection_counts = {
                table: int(
                    target.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )
                for table in (
                    "companies",
                    "canonical_opportunities",
                    "jobs",
                    "opportunity_enrichments",
                    "public_job_identities",
                    "public_job_paths",
                    "public_job_bindings",
                )
            }
        finally:
            if source_connection.in_transaction:
                source_connection.rollback()
            source_connection.close()
            target.close()
        os.chmod(temporary, 0o600)
        database_digest = _sha256_file(temporary)
        release = build_production_release(
            database_sha256=database_digest,
            registry_artifact=registry_artifact,
            bindings=bindings,
        )
        result = {
            "format": PROJECTION_FORMAT,
            "observed_at": observed_at.isoformat(),
            "public_origin": PUBLIC_ORIGIN,
            "owned_routes": list(OWNED_ROUTES),
            "cache_policy": "no-store",
            "database_sha256": database_digest,
            "release_id": release.release_id,
            "registry_sha256": release.registry_sha256,
            "company_count": projection_counts["companies"],
            "opportunity_count": projection_counts["canonical_opportunities"],
            "job_count": projection_counts["jobs"],
            "catalog_job_count": len(jobs),
            "published_detail_count": len(release.published_details),
            "enrichment_count": projection_counts["opportunity_enrichments"],
            "identity_count": projection_counts["public_job_identities"],
            "path_count": projection_counts["public_job_paths"],
            "binding_count": projection_counts["public_job_bindings"],
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
            canonical_production_release_json(release),
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


def _source_sha256(path_value) -> str:
    path = _existing_absolute_file(path_value)
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
    parser.add_argument("--registry", required=True)
    parser.add_argument("--bindings", required=True)
    parser.add_argument("--release-manifest", required=True)
    parser.add_argument(
        "--observed-at",
        help="fixed timezone-aware ISO-8601 projection time for reproducible builds",
    )
    arguments = parser.parse_args(argv)
    manifest_path = _new_absolute_path(arguments.manifest)
    try:
        result = build_public_catalog_production_database(
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
        result["source_database_sha256"] = _source_sha256(arguments.source)
        descriptor = os.open(
            manifest_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, sort_keys=True, indent=2)
            stream.write("\n")
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except Exception:
        print("Production public catalog projection failed safely.", file=sys.stderr)
        return 1
    print(
        "PUBLIC_CATALOG_PRODUCTION_PROJECTION_OK "
        f"release={result['release_id']} database={result['database_sha256']} "
        f"routes={len(result['owned_routes'])} identities={result['identity_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
