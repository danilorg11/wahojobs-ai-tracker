from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest

from wahojobs import authenticated_profile_matches as matches_module
from wahojobs import public_company_page, public_jobs_catalog
from wahojobs.authenticated_profile_matches import (
    AuthenticatedProfileMatchesBrowserIntegration,
    AuthenticatedProfileMatchesService,
)
from wahojobs.matching.metadata_overlay import OpportunityMetadataOverlay
from tests.test_public_job_page import JOB_PATH, OBSERVED_AT, ORIGIN, seed_public_job


COMPANY_PATH = "/company/acme-ai"


class ReadOnlyProvider:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def __call__(self):
        connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = ON")
        try:
            yield connection
        finally:
            connection.close()


class PublicCompanyPageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "public-company.sqlite"
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            seed_public_job(connection)
        finally:
            connection.close()

    def load_jobs(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            return public_jobs_catalog.load_public_jobs(
                connection,
                now=matches_module.datetime.fromisoformat(OBSERVED_AT),
            )
        finally:
            connection.close()

    def integration(self):
        integration = AuthenticatedProfileMatchesBrowserIntegration(
            object.__new__(AuthenticatedProfileMatchesService),
            connection_provider=ReadOnlyProvider(self.path),
            metadata_overlay=OpportunityMetadataOverlay(
                path=self.path.with_suffix(".overlay.json"),
                records_by_key={},
            ),
            confirmed_profile_artifact_sink=lambda _artifact: None,
            completed_profile_confirmation_authenticator=lambda _request: None,
            public_origin=ORIGIN,
            now=lambda: matches_module.datetime.fromisoformat(OBSERVED_AT),
        )
        self.addCleanup(integration.close)
        return integration

    def test_stable_company_path_uses_only_trusted_current_inventory(self):
        self.assertEqual(
            public_company_page.parse_public_company_path(COMPANY_PATH),
            "acme-ai",
        )
        self.assertIsNone(public_company_page.parse_public_company_path("/company/Acme"))
        self.assertIsNone(public_company_page.parse_public_company_path("/company/acme/jobs"))

        company = public_company_page.build_public_company(
            self.load_jobs(),
            COMPANY_PATH,
        )
        self.assertEqual(company["name"], "Acme AI")
        self.assertEqual(company["opportunity_count"], 1)
        self.assertEqual(company["official_url"], "https://careers.example.test/acme")
        self.assertIsNone(
            public_company_page.build_public_company(
                self.load_jobs(),
                "/company/not-present",
            )
        )

    def test_render_has_supported_sections_internal_job_links_and_no_metadata(self):
        company = public_company_page.build_public_company(
            self.load_jobs(),
            COMPANY_PATH,
        )
        page = public_company_page.render_public_company_page(
            company,
            public_origin=ORIGIN,
        )

        for expected in (
            "<h1>Acme AI</h1>",
            "1 current opportunity",
            "Current opportunities",
            f"href='{JOB_PATH}'",
            "Company website or careers",
            "Find the opportunities that fit you",
            "Find jobs that fit you",
            "Create a profile or sign in",
            "rel='canonical' href='https://app.test/company/acme-ai'",
        ):
            self.assertIn(expected, page)
        for absent in (
            "acme_official_api",
            "official-job-detail-v1",
            "provider:",
            "source type:",
            "source_tier",
            "inventory_model",
            "market_count_policy",
            "return_to=",
            ">Unknown<",
        ):
            self.assertNotIn(absent, page)

    def test_api_company_url_is_omitted_without_removing_internal_page(self):
        jobs = self.load_jobs()
        jobs[0]["careers_url"] = None
        company = public_company_page.build_public_company(jobs, COMPANY_PATH)
        page = public_company_page.render_public_company_page(
            company,
            public_origin=ORIGIN,
        )

        self.assertIsNone(company["official_url"])
        self.assertIn("<h1>Acme AI</h1>", page)
        self.assertIn(f"href='{JOB_PATH}'", page)
        self.assertNotIn("Company website or careers", page)

    def test_company_jobs_paginate_with_direct_internal_job_links(self):
        base = self.load_jobs()[0]
        jobs = []
        for index in range(31):
            job = deepcopy(base)
            job["job_id"] = 20_000 + index
            job["path"] = f"/job/acme-ai-{20_000 + index}"
            job["source_title"] = f"Role {index:02d}"
            public_jobs_catalog.prepare_catalog_presentation(job)
            jobs.append(job)

        company = public_company_page.build_public_company(
            jobs,
            COMPANY_PATH,
            page=2,
        )
        page = public_company_page.render_public_company_page(
            company,
            public_origin=ORIGIN,
            query_present=True,
        )
        self.assertEqual(company["catalog"]["page"], 2)
        self.assertEqual(company["catalog"]["page_result_count"], 1)
        self.assertIn("Showing 31–31 of 31 current opportunities", page)
        self.assertIn("Page 2 of 2", page)
        self.assertIn(f"href='{COMPANY_PATH}'>Previous</a>", page)
        self.assertIn("<meta name='robots' content='noindex,follow'>", page)
        self.assertNotIn("return_to=", page)

    def test_route_is_public_and_rejects_bad_methods_or_queries(self):
        integration = self.integration()
        self.assertTrue(integration.matches_route(COMPANY_PATH))

        response = integration.handle(
            "GET",
            COMPANY_PATH,
            (("Host", "app.test"),),
        )
        body = response.body.decode("utf-8")
        self.assertEqual(response.status, 200, body)
        self.assertEqual(dict(response.headers)["Cache-Control"], "public, max-age=300")
        self.assertIn("<h1>Acme AI</h1>", body)
        self.assertIn(f"href='{JOB_PATH}'", body)

        rejected = integration.handle(
            "POST",
            COMPANY_PATH,
            (("Host", "app.test"),),
        )
        self.assertEqual(rejected.status, 405)

        malformed = integration.handle(
            "GET",
            COMPANY_PATH + "?provider=secret",
            (("Host", "app.test"),),
        )
        self.assertEqual(malformed.status, 400)


if __name__ == "__main__":
    unittest.main()
