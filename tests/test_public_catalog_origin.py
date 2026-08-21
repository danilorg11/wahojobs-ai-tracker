from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.build_public_catalog_preview_database import (
    build_public_catalog_preview_database,
)
from scripts.configure_public_catalog_origin import create_configuration
from tests.test_authenticated_profile_matches import _seed_configured_inventory
from wahojobs import public_job_identity
from wahojobs.public_catalog_origin import (
    EXPECTED_EMPTY_TABLES,
    EXPECTED_TABLES,
    PublicCatalogOriginConfigurationError,
    PublicCatalogOriginIntegration,
    attest_public_projection,
    load_public_catalog_origin_configuration,
)


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
TOKEN = "T" * 43
NEW_ID = "j11111111111111111111111111111111"
KARL_ID = "j7b8550e11700c9b26ac68deb753e1f82"
NEW_PATH = (
    "/job/configured-production-distinctive-bilingual-data-annotation-reviewer-"
    + NEW_ID
)
KARL_PATH = "/job/oneforma-karl-llm-1"


def _seed_projection_inventory(path: Path) -> None:
    _seed_configured_inventory(path, observed_at=NOW)
    observed = NOW.isoformat()
    connection = sqlite3.connect(path)
    try:
        # Detail-only historical row for Karl. It must render but must not enter
        # the active catalog inventory.
        connection.execute(
            "INSERT INTO canonical_opportunities "
            "(id, company_id, canonical_key, canonical_title, normalized_title, "
            "source_category, language, language_locale, first_seen_at, "
            "last_seen_at, is_active, variant_count) "
            "VALUES (7102, 7001, 'oneforma::177080', 'Karl LLM Evaluator', "
            "'karl llm evaluator', 'Generalist', NULL, NULL, ?, ?, 0, 1)",
            (observed, observed),
        )
        connection.execute(
            "INSERT INTO jobs "
            "(id, company_id, canonical_opportunity_id, external_id, title, "
            "location, department, expertise, commitment, url, source_hash, "
            "opportunity_kind, availability_basis, "
            "include_in_live_market_estimate, first_seen_at, last_seen_at, "
            "is_active, removed_at, updated_at) "
            "VALUES (7103, 7001, 7102, 'karl-fixture', 'Karl LLM Evaluator', "
            "'Remote', 'Generalist', 'Generalist', 'Freelance', "
            "'https://jobs.example.test/karl', 'karl-source-hash', "
            "'live_posting', 'api_feed', 1, ?, ?, 0, ?, ?)",
            (observed, observed, observed, observed),
        )
        # Active but deliberately unpublished: its card must link to the
        # official source, never to a synthesized internal detail path.
        connection.execute(
            "INSERT INTO canonical_opportunities "
            "(id, company_id, canonical_key, canonical_title, normalized_title, "
            "source_category, language, language_locale, first_seen_at, "
            "last_seen_at, is_active, variant_count) "
            "VALUES (7202, 7001, 'unpublished-active-fixture', "
            "'Unpublished Active Reviewer', 'unpublished active reviewer', "
            "'Generalist', NULL, NULL, ?, ?, 1, 1)",
            (observed, observed),
        )
        connection.execute(
            "INSERT INTO jobs "
            "(id, company_id, canonical_opportunity_id, external_id, title, "
            "location, department, expertise, commitment, url, source_hash, "
            "opportunity_kind, availability_basis, "
            "include_in_live_market_estimate, first_seen_at, last_seen_at, "
            "is_active, updated_at) "
            "VALUES (7203, 7001, 7202, 'unpublished-active-fixture', "
            "'Unpublished Active Reviewer', 'Remote', 'Generalist', "
            "'Generalist', 'Freelance', "
            "'https://jobs.example.test/unpublished-active', "
            "'unpublished-source-hash', 'live_posting', 'api_feed', 1, ?, ?, 1, ?)",
            (observed, observed, observed),
        )
        connection.execute(
            "INSERT INTO user_profiles "
            "(user_id, profile_id, display_name, notes, is_sample) "
            "VALUES ('private-user', 'private-profile', 'Private Person', "
            "'must never be copied', 0)"
        )
        connection.commit()
    finally:
        connection.close()


def _write_release_inputs(root: Path) -> tuple[Path, Path]:
    timestamp = NOW.isoformat()
    artifact = public_job_identity.public_job_registry_artifact(
        {
            "format": public_job_identity.PUBLIC_JOB_REGISTRY_FORMAT,
            "identities": [
                {
                    "public_job_id": NEW_ID,
                    "disposition": "serving",
                    "redirect_target_public_job_id": None,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
                {
                    "public_job_id": KARL_ID,
                    "disposition": "serving",
                    "redirect_target_public_job_id": None,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            ],
            "paths": [
                {
                    "path": NEW_PATH,
                    "normalized_path": NEW_PATH,
                    "public_job_id": NEW_ID,
                    "path_role": "primary",
                    "created_at": timestamp,
                },
                {
                    "path": KARL_PATH,
                    "normalized_path": KARL_PATH,
                    "public_job_id": KARL_ID,
                    "path_role": "primary",
                    "created_at": timestamp,
                },
            ],
        }
    )
    registry = root / "registry.json"
    registry.write_bytes(artifact.canonical_json)
    bindings = root / "bindings.json"
    bindings.write_text(
        json.dumps(
            {
                "format": "wahojobs-public-job-preview-bindings-v1",
                "bindings": [
                    {
                        "public_job_id": NEW_ID,
                        "canonical_key": "distinctive-bilingual-data-annotation-reviewer",
                    },
                    {
                        "public_job_id": KARL_ID,
                        "canonical_key": "oneforma::177080",
                    },
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="ascii",
    )
    return registry, bindings


class PublicCatalogOriginTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="wahojobs-public-catalog-origin-", ignore_cleanup_errors=True
        )
        self.root = Path(self.directory.name)
        self.source = self.root / "private-beta-source.sqlite3"
        _seed_projection_inventory(self.source)
        registry, bindings = _write_release_inputs(self.root)
        self.database = self.root / "catalog.sqlite3"
        self.release_manifest = self.root / "release-manifest.json"
        self.manifest = build_public_catalog_preview_database(
            self.source,
            self.database,
            registry_path=registry,
            bindings_path=bindings,
            release_manifest_path=self.release_manifest,
            now=NOW,
        )
        self.configuration_path = self.root / "origin.json"
        create_configuration(
            self.database,
            self.configuration_path,
            public_origin="https://wahojobs-proof-git-preview.vercel.app",
            deployment_environment="preview",
            bind_port=18080,
            release_manifest_path=self.release_manifest,
        )
        self.configuration = load_public_catalog_origin_configuration(
            str(self.configuration_path)
        )
        self.integration = PublicCatalogOriginIntegration(
            self.configuration, origin_auth_token=TOKEN
        )

    def tearDown(self):
        self.integration.close()
        self.directory.cleanup()

    def headers(self, token=TOKEN, *extra, release=True):
        result = (("X-Wahojobs-Origin-Auth", token),)
        if release:
            result += (("X-Wahojobs-Release-Id", self.configuration.release.release_id),)
        return result + tuple(extra)

    def test_projection_contains_only_public_rows_and_exact_release(self):
        self.assertEqual(self.manifest["job_count"], 3)
        self.assertEqual(self.manifest["catalog_job_count"], 2)
        self.assertEqual(self.manifest["published_detail_count"], 2)
        self.assertIn("accounts", self.manifest["excluded_data_families"])
        self.assertNotIn(
            "public_job_identities", self.manifest["excluded_data_families"]
        )
        connection = sqlite3.connect(self.database)
        try:
            tables = frozenset(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            )
            self.assertEqual(tables, EXPECTED_TABLES)
            for table in EXPECTED_EMPTY_TABLES:
                with self.subTest(table=table):
                    self.assertEqual(
                        connection.execute(
                            f'SELECT COUNT(*) FROM "{table}"'
                        ).fetchone()[0],
                        0,
                    )
            self.assertEqual(
                connection.execute(
                    "SELECT path FROM public_job_paths ORDER BY path"
                ).fetchall(),
                [(NEW_PATH,), (KARL_PATH,)],
            )
            serialized = self.database.read_bytes()
            self.assertNotIn(b"private-user", serialized)
            self.assertNotIn(b"private-profile", serialized)
            self.assertNotIn(b"must never be copied", serialized)
        finally:
            connection.close()
        attestation = self.integration.attestation
        self.assertEqual(attestation.release_id, self.manifest["release_id"])
        self.assertEqual(attestation.registry_sha256, self.manifest["registry_sha256"])
        self.assertEqual(
            (attestation.identity_count, attestation.path_count, attestation.binding_count),
            (2, 2, 2),
        )

    def test_catalog_links_only_published_details_or_official_sources(self):
        catalog = self.integration.handle(
            "GET", "/jobs", self.headers(), loopback_peer=True
        )
        self.assertEqual(catalog.status, 200)
        self.assertIn(NEW_PATH.encode("ascii"), catalog.body)
        self.assertNotIn(KARL_PATH.encode("ascii"), catalog.body)
        self.assertNotIn(b"/job/opportunity-7002", catalog.body)
        self.assertNotIn(b"/job/opportunity-7202", catalog.body)
        self.assertIn(
            b"https://jobs.example.test/unpublished-active", catalog.body
        )
        headers = dict(catalog.headers)
        self.assertEqual(headers["X-Wahojobs-Origin"], "public-catalog-preview")
        self.assertEqual(
            headers["X-Wahojobs-Release-Id"], self.configuration.release.release_id
        )

    def test_exact_manifest_details_render_and_all_other_job_paths_are_unowned(self):
        for path, title in (
            (NEW_PATH, b"Distinctive Bilingual Data Annotation Reviewer"),
            (KARL_PATH, b"Karl LLM Evaluator"),
        ):
            with self.subTest(path=path):
                response = self.integration.handle(
                    "GET", path, self.headers(), loopback_peer=True
                )
                self.assertEqual(response.status, 200)
                self.assertIn(title, response.body)
                self.assertEqual(
                    dict(response.headers)["X-Wahojobs-Release-Id"],
                    self.configuration.release.release_id,
                )

        for path in (
            "/",
            "/jobs/",
            "/job/opportunity-7002",
            "/job/unknown-j00000000000000000000000000000000",
            NEW_PATH.upper(),
            NEW_PATH + "/",
            NEW_PATH.replace("/job/", "/job/%68"),
            "/job/not-a-public-id",
            "/company/configured-production",
            "/api/job",
            "/api/jobs",
            "/robots.txt",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.integration.handle(
                        "GET", path, self.headers(), loopback_peer=True
                    ).status,
                    404,
                )

    def test_release_auth_loopback_and_guest_only_boundaries_fail_closed(self):
        cases = (
            (self.headers("W" * 43), True, 403),
            ((), True, 403),
            (self.headers(), False, 403),
            (self.headers(release=False), True, 409),
            (
                self.headers(
                    TOKEN,
                    ("X-Wahojobs-Release-Id", "0" * 64),
                    release=False,
                ),
                True,
                409,
            ),
            (self.headers(TOKEN, ("Cookie", "private=1")), True, 400),
            (self.headers(TOKEN, ("Authorization", "Bearer private")), True, 400),
        )
        for headers, loopback, expected in cases:
            with self.subTest(expected=expected, loopback=loopback):
                self.assertEqual(
                    self.integration.handle(
                        "GET", "/jobs", headers, loopback_peer=loopback
                    ).status,
                    expected,
                )

    def test_readiness_and_metrics_attest_the_projection(self):
        ready = self.integration.handle(
            "GET", "/__origin/ready", self.headers(), loopback_peer=True
        )
        self.assertEqual(ready.status, 200)
        self.assertEqual(json.loads(ready.body), {"ready": True})
        metrics = self.integration.handle(
            "GET", "/__origin/metrics", self.headers(), loopback_peer=True
        )
        self.assertEqual(metrics.status, 200)
        self.assertEqual(
            json.loads(metrics.body),
            {"details": 0, "health": 2, "jobs": 0, "rejected": 0},
        )

        self.integration.close()
        with self.database.open("ab") as stream:
            stream.write(b"tamper")
        with self.assertRaises(PublicCatalogOriginConfigurationError):
            attest_public_projection(self.configuration)

    def test_preview_configuration_cannot_target_real_production_origin(self):
        second = self.root / "forbidden.json"
        create_configuration(
            self.database,
            second,
            public_origin="https://www.wahojobs.com",
            deployment_environment="production",
            bind_port=18081,
            release_manifest_path=self.release_manifest,
        )
        document = json.loads(second.read_text(encoding="utf-8"))
        document["deployment_environment"] = "preview"
        second.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(PublicCatalogOriginConfigurationError):
            load_public_catalog_origin_configuration(str(second))

    def test_configuration_accepts_a_linux_runtime_database_path_on_windows(self):
        second = self.root / "linux-runtime.json"
        create_configuration(
            self.database,
            second,
            public_origin="https://wahojobs-proof-git-preview.vercel.app",
            deployment_environment="preview",
            bind_port=18081,
            release_manifest_path=self.release_manifest,
            runtime_database_path="/var/lib/wahojobs-preview/catalog.sqlite3",
        )
        document = json.loads(second.read_text(encoding="utf-8"))
        self.assertEqual(
            document["database_path"],
            "/var/lib/wahojobs-preview/catalog.sqlite3",
        )

    def test_configuration_rejects_a_relative_or_traversing_runtime_path(self):
        for index, runtime_path in enumerate(
            ("var/lib/catalog.sqlite3", "/var/lib/../private.sqlite3")
        ):
            with self.subTest(runtime_path=runtime_path):
                with self.assertRaises(ValueError):
                    create_configuration(
                        self.database,
                        self.root / f"invalid-runtime-{index}.json",
                        public_origin="https://wahojobs-proof-git-preview.vercel.app",
                        deployment_environment="preview",
                        bind_port=18081,
                        release_manifest_path=self.release_manifest,
                        runtime_database_path=runtime_path,
                    )


if __name__ == "__main__":
    unittest.main()
