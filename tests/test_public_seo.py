from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.parse import urlsplit
from xml.etree import ElementTree

from wahojobs import authenticated_profile_matches as matches_module
from wahojobs import public_seo
from wahojobs.authenticated_profile_matches import (
    AuthenticatedProfileMatchesBrowserIntegration,
    AuthenticatedProfileMatchesService,
)
from wahojobs.matching.metadata_overlay import OpportunityMetadataOverlay

from tests.test_public_job_page import JOB_PATH, OBSERVED_AT, ORIGIN, seed_public_job


class ReadOnlyProvider:
    def __init__(self, path):
        self.path = Path(path)
        self.calls = 0

    @contextmanager
    def __call__(self):
        self.calls += 1
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = ON")
        try:
            yield connection
        finally:
            connection.close()


class PublicSeoPolicyTests(unittest.TestCase):
    def test_policy_is_exact_immutable_and_defensively_copied(self):
        redirects = {"/legacy/job": JOB_PATH}
        gone = ["/legacy/removed"]
        policy = public_seo.PublicSeoRoutePolicy(redirects, gone)
        redirects["/legacy/other"] = "/jobs"
        gone.append("/legacy/other-gone")

        decision = policy.resolve_path("/legacy/job")
        self.assertEqual((decision.kind, decision.location), ("redirect", JOB_PATH))
        self.assertEqual(policy.resolve_path("/legacy/removed").kind, "gone")
        self.assertIsNone(policy.resolve_path("/legacy/other"))
        self.assertIsNone(policy.resolve_path("/legacy/other-gone"))
        with self.assertRaises(TypeError):
            policy.redirects["/legacy/new"] = "/jobs"

    def test_policy_rejects_unsafe_ambiguous_or_chained_directives(self):
        invalid = (
            ({"https://legacy.test/job": JOB_PATH}, ()),
            ({"//legacy.test/job": JOB_PATH}, ()),
            ({"/legacy//job": JOB_PATH}, ()),
            ({"/legacy/%2e%2e/job": JOB_PATH}, ()),
            ({"/legacy/%2Fjob": JOB_PATH}, ()),
            ({"/legacy/%5cjob": JOB_PATH}, ()),
            ({"/legacy/%3Fjob": JOB_PATH}, ()),
            ({"/legacy/%23job": JOB_PATH}, ()),
            ({"/legacy/%00job": JOB_PATH}, ()),
            ({"/legacy?job=1": JOB_PATH}, ()),
            ({"/legacy/../job": JOB_PATH}, ()),
            ({"/legacy": "https://app.test/jobs"}, ()),
            ({"/legacy": "/private"}, ()),
            ({"/legacy": "/job/opportunity-9223372036854775808"}, ()),
            ({"/legacy": "/legacy"}, ()),
            ({"/legacy-a": "/legacy-b", "/legacy-b": JOB_PATH}, ()),
            ({"/legacy": JOB_PATH}, ("/legacy",)),
        )
        for redirects, gone in invalid:
            with self.subTest(redirects=redirects, gone=gone):
                with self.assertRaises(ValueError):
                    public_seo.PublicSeoRoutePolicy(redirects, gone)

    def test_policy_rejects_ambiguous_gone_directive_sources(self):
        ambiguous_sources = (
            "/legacy//job",
            "/legacy/%2e%2e/job",
            "/legacy/%2Fjob",
            "/legacy/%5cjob",
            "/legacy/%3Fjob",
            "/legacy/%23job",
            "/legacy/%00job",
        )
        for source in ambiguous_sources:
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    public_seo.PublicSeoRoutePolicy({}, (source,))

    def test_policy_accepts_clean_local_sources_and_supported_targets(self):
        policy = public_seo.PublicSeoRoutePolicy(
            {
                "/legacy/jobs": "/jobs",
                "/legacy/company/mindrift": "/company/mindrift",
                "/legacy/job/1629": "/job/opportunity-1629",
            },
            ("/legacy/clean-removed-job",),
        )

        self.assertEqual(policy.resolve_path("/legacy/jobs").location, "/jobs")
        self.assertEqual(
            policy.resolve_path("/legacy/company/mindrift").location,
            "/company/mindrift",
        )
        self.assertEqual(
            policy.resolve_path("/legacy/job/1629").location,
            "/job/opportunity-1629",
        )
        self.assertEqual(
            policy.resolve_path("/legacy/clean-removed-job").kind,
            "gone",
        )


class PublicSeoIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "public-seo.sqlite"
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            seed_public_job(connection)
        finally:
            connection.close()

    def integration(self, policy=None, connection_provider=None):
        integration = AuthenticatedProfileMatchesBrowserIntegration(
            object.__new__(AuthenticatedProfileMatchesService),
            connection_provider=connection_provider or ReadOnlyProvider(self.path),
            metadata_overlay=OpportunityMetadataOverlay(
                path=self.path.with_suffix(".overlay.json"),
                records_by_key={},
            ),
            confirmed_profile_artifact_sink=lambda _artifact: None,
            completed_profile_confirmation_authenticator=lambda _request: None,
            public_origin=ORIGIN,
            now=lambda: matches_module.datetime.fromisoformat(OBSERVED_AT),
            public_seo_policy=policy,
        )
        self.addCleanup(integration.close)
        return integration

    @staticmethod
    def get(integration, target):
        return integration.handle("GET", target, (("Host", "app.test"),))

    def test_empty_policy_does_not_claim_unmapped_legacy_paths(self):
        integration = self.integration()
        self.assertFalse(integration.matches_route("/legacy/job"))

    def test_injected_redirect_and_gone_precede_normal_route_loading(self):
        policy = public_seo.PublicSeoRoutePolicy(
            {"/legacy/job": JOB_PATH},
            ("/legacy/removed",),
        )
        provider = ReadOnlyProvider(self.path)
        integration = self.integration(policy, connection_provider=provider)
        self.assertTrue(integration.matches_route("/legacy/job"))
        redirect = self.get(integration, "/legacy/job?utm_source=test")
        self.assertEqual(redirect.status, 301)
        self.assertEqual(dict(redirect.headers)["Location"], JOB_PATH)
        self.assertEqual(
            dict(redirect.headers)["X-Robots-Tag"],
            "noindex, nofollow",
        )
        gone = self.get(integration, "/legacy/removed")
        self.assertEqual(gone.status, 410)
        self.assertEqual(dict(gone.headers)["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(provider.calls, 0)

    def test_policy_owned_live_job_is_not_linked_or_sitemapped(self):
        policy = public_seo.PublicSeoRoutePolicy({JOB_PATH: "/jobs"}, ())
        integration = self.integration(policy)
        catalog = self.get(integration, "/jobs")
        jobs_sitemap = self.get(integration, public_seo.JOBS_SITEMAP_ROUTE)

        self.assertEqual(catalog.status, 200)
        self.assertNotIn(JOB_PATH, catalog.body.decode("utf-8"))
        self.assertNotIn(JOB_PATH, jobs_sitemap.body.decode("utf-8"))
        redirected = self.get(integration, JOB_PATH)
        self.assertEqual(redirected.status, 301)
        self.assertEqual(dict(redirected.headers)["Location"], "/jobs")

    def test_robots_and_sitemap_documents_are_public_and_deterministic(self):
        provider = ReadOnlyProvider(self.path)
        integration = self.integration(connection_provider=provider)
        robots = self.get(integration, public_seo.ROBOTS_ROUTE)
        self.assertEqual(robots.status, 200)
        self.assertEqual(
            dict(robots.headers)["Content-Type"],
            "text/plain; charset=utf-8",
        )
        self.assertEqual(
            robots.body.decode("utf-8"),
            "User-agent: *\nAllow: /\nSitemap: https://app.test/sitemap.xml\n",
        )

        index = self.get(integration, public_seo.SITEMAP_INDEX_ROUTE)
        self.assertEqual(index.status, 200)
        self.assertEqual(
            dict(index.headers)["Content-Type"],
            "application/xml; charset=utf-8",
        )
        root = ElementTree.fromstring(index.body)
        namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        self.assertEqual(
            [item.text for item in root.findall("s:sitemap/s:loc", namespace)],
            [
                ORIGIN + public_seo.STATIC_SITEMAP_ROUTE,
                ORIGIN + public_seo.JOBS_SITEMAP_ROUTE,
                ORIGIN + public_seo.COMPANIES_SITEMAP_ROUTE,
            ],
        )
        malformed = self.get(integration, public_seo.SITEMAP_INDEX_ROUTE + "?x=1")
        self.assertEqual(malformed.status, 400)

        static = self.get(integration, public_seo.STATIC_SITEMAP_ROUTE)
        self.assertEqual(static.status, 200)
        self.assertEqual(provider.calls, 0)

    def test_every_urlset_location_is_live_indexable_self_canonical_and_clean(self):
        integration = self.integration()
        namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locations = []
        for sitemap_path in (
            public_seo.STATIC_SITEMAP_ROUTE,
            public_seo.JOBS_SITEMAP_ROUTE,
            public_seo.COMPANIES_SITEMAP_ROUTE,
        ):
            response = self.get(integration, sitemap_path)
            self.assertEqual(response.status, 200)
            root = ElementTree.fromstring(response.body)
            locations.extend(
                item.text for item in root.findall("s:url/s:loc", namespace)
            )

        self.assertEqual(
            set(locations),
            {ORIGIN + "/jobs", ORIGIN + JOB_PATH, ORIGIN + "/company/acme-ai"},
        )
        for location in locations:
            with self.subTest(location=location):
                response = self.get(integration, urlsplit(location).path)
                headers = dict(response.headers)
                body = response.body.decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertNotIn("Location", headers)
                self.assertNotIn("X-Robots-Tag", headers)
                self.assertNotIn("name='robots' content='noindex", body)
                self.assertIn(f"rel='canonical' href='{location}'", body)

    def test_public_seo_get_and_head_leave_file_database_byte_identical(self):
        integration = self.integration()
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        targets = (
            public_seo.ROBOTS_ROUTE,
            public_seo.SITEMAP_INDEX_ROUTE,
            public_seo.STATIC_SITEMAP_ROUTE,
            public_seo.JOBS_SITEMAP_ROUTE,
            public_seo.COMPANIES_SITEMAP_ROUTE,
            "/jobs",
            "/jobs?q=Python",
            JOB_PATH,
            "/company/acme-ai",
        )
        for method in ("GET", "HEAD"):
            for target in targets:
                with self.subTest(method=method, target=target):
                    response = integration.handle(
                        method,
                        target,
                        (("Host", "app.test"),),
                    )
                    self.assertEqual(response.status, 200)
        after = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
