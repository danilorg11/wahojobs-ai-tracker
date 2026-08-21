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


class PublicCatalogOriginTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="wahojobs-public-catalog-origin-", ignore_cleanup_errors=True
        )
        self.root = Path(self.directory.name)
        self.source = self.root / "private-beta-source.sqlite3"
        _seed_configured_inventory(self.source, observed_at=NOW)
        connection = sqlite3.connect(self.source)
        try:
            connection.execute(
                "INSERT INTO user_profiles "
                "(user_id, profile_id, display_name, notes, is_sample) "
                "VALUES ('private-user', 'private-profile', 'Private Person', "
                "'must never be copied', 0)"
            )
            connection.commit()
        finally:
            connection.close()
        self.database = self.root / "catalog.sqlite3"
        self.manifest = build_public_catalog_preview_database(
            self.source, self.database, now=NOW
        )
        self.configuration_path = self.root / "origin.json"
        create_configuration(
            self.database,
            self.configuration_path,
            public_origin="https://wahojobs-proof-git-preview.vercel.app",
            deployment_environment="preview",
            bind_port=18080,
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

    @staticmethod
    def headers(token=TOKEN, *extra):
        return (("X-Wahojobs-Origin-Auth", token),) + tuple(extra)

    def test_projection_contains_only_public_catalog_rows(self):
        self.assertEqual(self.manifest["job_count"], 1)
        self.assertIn("accounts", self.manifest["excluded_data_families"])
        self.assertIn("public_job_identities", self.manifest["excluded_data_families"])
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
            serialized = self.database.read_bytes()
            self.assertNotIn(b"private-user", serialized)
            self.assertNotIn(b"private-profile", serialized)
            self.assertNotIn(b"must never be copied", serialized)
        finally:
            connection.close()

    def test_exact_jobs_and_health_are_the_only_owned_paths(self):
        catalog = self.integration.handle(
            "GET", "/jobs", self.headers(), loopback_peer=True
        )
        self.assertEqual(catalog.status, 200)
        self.assertIn(b"Distinctive Bilingual Data Annotation Reviewer", catalog.body)
        self.assertEqual(
            dict(catalog.headers)["X-Wahojobs-Origin"], "public-catalog-preview"
        )
        for path in (
            "/",
            "/job/opportunity-7002",
            "/company/configured-production",
            "/online-jobs/anything",
            "/robots.txt",
            "/sitemap.xml",
            "/random",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.integration.handle(
                        "GET", path, self.headers(), loopback_peer=True
                    ).status,
                    404,
                )
        ready = self.integration.handle(
            "GET", "/__origin/ready", self.headers(), loopback_peer=True
        )
        self.assertEqual(ready.status, 200)
        self.assertEqual(json.loads(ready.body), {"ready": True})

    def test_origin_auth_loopback_and_guest_only_boundaries_fail_closed(self):
        cases = (
            (self.headers("W" * 43), True, 403),
            ((), True, 403),
            (self.headers(), False, 403),
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

    def test_readiness_detects_database_change(self):
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
                        runtime_database_path=runtime_path,
                    )


if __name__ == "__main__":
    unittest.main()
