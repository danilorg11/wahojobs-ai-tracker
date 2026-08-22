from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.parse import urljoin, urlsplit

from scripts.build_public_catalog_production_database import (
    OWNED_ROUTES,
    build_public_catalog_production_database,
)
from scripts.configure_public_catalog_origin import create_configuration
from tests.test_public_catalog_origin import _seed_projection_inventory
from wahojobs import public_job_identity
from wahojobs.public_catalog_origin import (
    EXPECTED_EMPTY_TABLES,
    PublicCatalogOriginConfigurationError,
    PublicCatalogOriginIntegration,
    PublicCatalogOriginResponse,
    attest_public_projection,
    load_public_catalog_origin_configuration,
)
from wahojobs.public_job_release import (
    HANDSHAKE_CANARY_CANONICAL_KEY,
    HANDSHAKE_CANARY_PUBLIC_JOB_ID,
    HANDSHAKE_CANARY_PUBLIC_JOB_PATH,
    KARL_PUBLIC_JOB_PATH,
)


NOW = datetime(2026, 8, 21, 15, 35, 26, tzinfo=timezone.utc)
TOKEN = "P" * 43


class _HrefCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs = []

    def handle_starttag(self, _tag, attributes):
        for name, value in attributes:
            if name.casefold() == "href" and value is not None:
                self.hrefs.append(value)


def _production_internal_href_paths(document: str) -> tuple[str, ...]:
    collector = _HrefCollector()
    collector.feed(document)
    public_page = "https://www.wahojobs.com/jobs"
    public_authority = urlsplit(public_page).netloc
    result = []
    for href in collector.hrefs:
        resolved = urlsplit(urljoin(public_page, href))
        if resolved.netloc.casefold() == public_authority:
            result.append(resolved.path)
    return tuple(result)


def _write_production_inputs(root: Path) -> tuple[Path, Path]:
    timestamp = NOW.isoformat()
    artifact = public_job_identity.public_job_registry_artifact(
        {
            "format": public_job_identity.PUBLIC_JOB_REGISTRY_FORMAT,
            "identities": [
                {
                    "public_job_id": HANDSHAKE_CANARY_PUBLIC_JOB_ID,
                    "disposition": "serving",
                    "redirect_target_public_job_id": None,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
            ],
            "paths": [
                {
                    "path": HANDSHAKE_CANARY_PUBLIC_JOB_PATH,
                    "normalized_path": HANDSHAKE_CANARY_PUBLIC_JOB_PATH,
                    "public_job_id": HANDSHAKE_CANARY_PUBLIC_JOB_ID,
                    "path_role": "primary",
                    "created_at": timestamp,
                }
            ],
        }
    )
    registry = root / "registry.json"
    registry.write_bytes(artifact.canonical_json)
    bindings = root / "bindings.json"
    bindings.write_text(
        json.dumps(
            {
                "format": "wahojobs-public-job-production-bindings-v1",
                "bindings": [
                    {
                        "public_job_id": HANDSHAKE_CANARY_PUBLIC_JOB_ID,
                        "canonical_key": HANDSHAKE_CANARY_CANONICAL_KEY,
                    }
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="ascii",
        newline="\n",
    )
    return registry, bindings


class PublicCatalogProductionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="wahojobs-public-catalog-production-", ignore_cleanup_errors=True
        )
        self.root = Path(self.directory.name)
        self.source = self.root / "private-source.sqlite3"
        _seed_projection_inventory(self.source)
        connection = sqlite3.connect(self.source)
        try:
            connection.execute(
                "UPDATE canonical_opportunities SET canonical_key = ? WHERE id = 7002",
                (HANDSHAKE_CANARY_CANONICAL_KEY,),
            )
            connection.commit()
        finally:
            connection.close()
        registry, bindings = _write_production_inputs(self.root)
        self.database = self.root / "catalog.sqlite3"
        self.release_manifest = self.root / "release-manifest.json"
        self.projection = build_public_catalog_production_database(
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
            public_origin="https://www.wahojobs.com",
            deployment_environment="production",
            bind_port=18082,
            release_manifest_path=self.release_manifest,
        )
        self.configuration = load_public_catalog_origin_configuration(
            str(self.configuration_path)
        )
        self.integration = PublicCatalogOriginIntegration(
            self.configuration,
            origin_auth_token=TOKEN,
        )

    def tearDown(self):
        self.integration.close()
        self.directory.cleanup()

    def headers(self, token=TOKEN, *, release=True, extra=()):
        result = (("X-Wahojobs-Origin-Auth", token),)
        if release:
            result += (
                ("X-Wahojobs-Release-Id", self.configuration.release.release_id),
            )
        return result + tuple(extra)

    def test_projection_is_public_only_and_exactly_canary_bound(self):
        self.assertEqual(tuple(self.projection["owned_routes"]), OWNED_ROUTES)
        self.assertEqual(self.projection["published_detail_count"], 1)
        self.assertEqual(
            (
                self.projection["identity_count"],
                self.projection["path_count"],
                self.projection["binding_count"],
            ),
            (1, 1, 1),
        )
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT path, public_job_id FROM public_job_paths"
                ).fetchall(),
                [(HANDSHAKE_CANARY_PUBLIC_JOB_PATH, HANDSHAKE_CANARY_PUBLIC_JOB_ID)],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM public_job_paths WHERE path = ?",
                    (KARL_PUBLIC_JOB_PATH,),
                ).fetchone()[0],
                0,
            )
            for table in EXPECTED_EMPTY_TABLES:
                with self.subTest(table=table):
                    self.assertEqual(
                        connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0],
                        0,
                    )
        finally:
            connection.close()
        attestation = attest_public_projection(self.configuration)
        self.assertEqual(
            (attestation.identity_count, attestation.path_count, attestation.binding_count),
            (1, 1, 1),
        )

    def test_catalog_links_and_canonicals_are_production_safe(self):
        catalog = self.integration.handle(
            "GET", "/jobs", self.headers(), loopback_peer=True
        )
        self.assertEqual(catalog.status, 200)
        body = catalog.body.decode("utf-8")
        self.assertIn(
            "rel='canonical' href='https://www.wahojobs.com/jobs'",
            body,
        )
        self.assertEqual(body.count(f"href='{HANDSHAKE_CANARY_PUBLIC_JOB_PATH}'"), 2)
        self.assertNotIn(KARL_PUBLIC_JOB_PATH, body)
        self.assertNotIn("/job/opportunity-", body)
        self.assertNotIn("href='/find-matches'", body)
        self.assertNotIn("href='/login'", body)
        internal_paths = _production_internal_href_paths(body)
        self.assertTrue(internal_paths)
        self.assertEqual(set(internal_paths), set(OWNED_ROUTES))
        for path in internal_paths:
            with self.subTest(internal_href_path=path):
                self.assertIn(path, OWNED_ROUTES)
        self.assertIn("https://jobs.example.test/unpublished-active", body)
        headers = dict(catalog.headers)
        self.assertEqual(headers["X-Wahojobs-Origin"], "public-catalog-production")
        self.assertEqual(headers["Cache-Control"], "private, no-store, max-age=0")
        self.assertEqual(headers["CDN-Cache-Control"], "no-store")
        self.assertEqual(headers["Vercel-CDN-Cache-Control"], "no-store")

        detail = self.integration.handle(
            "GET", HANDSHAKE_CANARY_PUBLIC_JOB_PATH, self.headers(), loopback_peer=True
        )
        self.assertEqual(detail.status, 200)
        detail_body = detail.body.decode("utf-8")
        canonical = "https://www.wahojobs.com" + HANDSHAKE_CANARY_PUBLIC_JOB_PATH
        self.assertIn(f"rel='canonical' href='{canonical}'", detail_body)
        self.assertIn(f"property='og:url' content='{canonical}'", detail_body)

    def test_origin_owns_only_jobs_and_canary_and_health_reattests(self):
        for path in (KARL_PUBLIC_JOB_PATH, "/", "/jobs/", "/robots.txt", "/api/job"):
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
        metrics = self.integration.handle(
            "GET", "/__origin/metrics", self.headers(), loopback_peer=True
        )
        self.assertEqual(metrics.status, 200)

    def test_production_origin_fails_closed_if_renderer_emits_unowned_internal_href(self):
        class UnsafeCatalogDelegate:
            def handle(self, _method, _target, _headers, _body_stream):
                body = b"<html><body><a href='/login'>Sign in</a></body></html>"
                return PublicCatalogOriginResponse(
                    status=200,
                    body=body,
                    headers=(("Content-Type", "text/html; charset=utf-8"),),
                )

            def close(self):
                return True

        self.integration._delegate = UnsafeCatalogDelegate()
        response = self.integration.handle(
            "GET", "/jobs", self.headers(), loopback_peer=True
        )
        self.assertEqual(response.status, 503)
        self.assertNotIn(b"/login", response.body)
        self.assertEqual(
            dict(response.headers)["Cache-Control"],
            "private, no-store, max-age=0",
        )

    def test_production_origin_rejects_browser_equivalent_and_noncanonical_hrefs(self):
        attacks = (
            r"http:\\www.wahojobs.com\login",
            r"http:/\www.wahojobs.com/login",
            r"https:\\www.wahojobs.com\login",
            r"https:/\www.wahojobs.com/login",
            "https://www.wahojobs.com./login",
            "http://www.wahojobs.com./login",
            "//www.wahojobs.com./login",
            "https://WWW.WAHOJOBS.COM/login",
            "https://www%2ewahojobs%2ecom/login",
            "https://www.wahojobs.com:443/login",
            " https://www.wahojobs.com/login",
            "https://jobs.example.test\\@www.wahojobs.com/login",
        )

        class AdversarialCatalogDelegate:
            def __init__(self, href):
                self.href = href

            def handle(self, _method, _target, _headers, _body_stream):
                body = f"<html><body><a href='{self.href}'>Open</a></body></html>".encode(
                    "utf-8"
                )
                return PublicCatalogOriginResponse(
                    status=200,
                    body=body,
                    headers=(("Content-Type", "text/html; charset=utf-8"),),
                )

            def close(self):
                return True

        for href in attacks:
            with self.subTest(href=href):
                self.integration._delegate = AdversarialCatalogDelegate(href)
                response = self.integration.handle(
                    "GET", "/jobs", self.headers(), loopback_peer=True
                )
                self.assertEqual(response.status, 503)
                self.assertNotIn(href.encode("utf-8"), response.body)

    def test_production_origin_accepts_only_canonical_published_and_external_https_hrefs(self):
        safe_hrefs = (
            "/jobs",
            "/jobs?q=AI+Evaluation+Specialist",
            HANDSHAKE_CANARY_PUBLIC_JOB_PATH,
            "https://www.wahojobs.com/jobs",
            "https://www.wahojobs.com" + HANDSHAKE_CANARY_PUBLIC_JOB_PATH,
            "https://jobs.example.test/external-opportunity",
        )

        class CanonicalCatalogDelegate:
            def handle(self, _method, _target, _headers, _body_stream):
                body = (
                    "<html><body>"
                    + "".join(f"<a href='{href}'>Open</a>" for href in safe_hrefs)
                    + "</body></html>"
                ).encode("utf-8")
                return PublicCatalogOriginResponse(
                    status=200,
                    body=body,
                    headers=(("Content-Type", "text/html; charset=utf-8"),),
                )

            def close(self):
                return True

        self.integration._delegate = CanonicalCatalogDelegate()
        response = self.integration.handle(
            "GET", "/jobs", self.headers(), loopback_peer=True
        )
        self.assertEqual(response.status, 200)

    def test_origin_auth_release_and_public_origin_fail_closed(self):
        cases = (
            ((), 403),
            (self.headers("W" * 43), 403),
            (self.headers(release=False), 409),
            (
                self.headers(
                    release=False,
                    extra=(("X-Wahojobs-Release-Id", "0" * 64),),
                ),
                409,
            ),
            (self.headers(extra=(("Cookie", "private=1"),)), 400),
            (self.headers(extra=(("Authorization", "Bearer private"),)), 400),
        )
        for headers, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    self.integration.handle(
                        "GET", "/jobs", headers, loopback_peer=True
                    ).status,
                    expected,
                )

        invalid = self.root / "invalid-origin.json"
        with self.assertRaises(ValueError):
            create_configuration(
                self.database,
                invalid,
                public_origin="https://production.example.test",
                deployment_environment="production",
                bind_port=18083,
                release_manifest_path=self.release_manifest,
            )
        with self.assertRaises(PublicCatalogOriginConfigurationError):
            load_public_catalog_origin_configuration(str(self.configuration_path) + ".missing")


if __name__ == "__main__":
    unittest.main()
