from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.parse import urlencode

from wahojobs import authenticated_profile_matches as matches_module
from wahojobs import public_job_page, public_jobs_catalog
from wahojobs.authenticated_profile_matches import (
    AuthenticatedProfileMatchesBrowserIntegration,
    AuthenticatedProfileMatchesService,
)
from wahojobs.matching.metadata_overlay import OpportunityMetadataOverlay

from tests.test_public_job_page import JOB_PATH, OBSERVED_AT, ORIGIN, seed_public_job


NOW = datetime.fromisoformat(OBSERVED_AT)


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


class PublicJobsCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "public-jobs.sqlite"
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            seed_public_job(connection)
        finally:
            connection.close()

    def load(self, *, now=NOW):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            return public_jobs_catalog.load_public_jobs(connection, now=now)
        finally:
            connection.close()

    def integration(self, *, connection_provider=None, now=None):
        service = object.__new__(AuthenticatedProfileMatchesService)
        connection_provider = connection_provider or ReadOnlyProvider(self.path)
        integration = AuthenticatedProfileMatchesBrowserIntegration(
            service,
            connection_provider=connection_provider,
            metadata_overlay=OpportunityMetadataOverlay(
                path=self.path.with_suffix(".overlay.json"),
                records_by_key={},
            ),
            confirmed_profile_artifact_sink=lambda _artifact: None,
            completed_profile_confirmation_authenticator=lambda _request: None,
            public_origin=ORIGIN,
            now=now or (lambda: NOW),
        )
        self.addCleanup(integration.close)
        return integration

    def test_catalog_snapshot_makes_immediate_pagination_and_back_requests_reuse_inventory(self):
        provider = ReadOnlyProvider(self.path)
        current = [NOW]
        integration = self.integration(
            connection_provider=provider,
            now=lambda: current[0],
        )

        first = integration.handle("GET", "/jobs", (("Host", "app.test"),))
        next_page = integration.handle(
            "GET",
            "/jobs?page=2",
            (("Host", "app.test"),),
        )
        back = integration.handle("GET", "/jobs", (("Host", "app.test"),))

        self.assertEqual([first.status, next_page.status, back.status], [200, 404, 200])
        self.assertEqual(provider.calls, 1)

        current[0] += timedelta(seconds=301)
        refreshed = integration.handle("GET", "/jobs", (("Host", "app.test"),))
        self.assertEqual(refreshed.status, 200)
        self.assertEqual(provider.calls, 2)

    def test_inventory_is_canonical_deduplicated_and_links_to_stable_internal_page(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                """
                INSERT INTO jobs (
                  id, company_id, canonical_opportunity_id, external_id, title,
                  location, url, source_hash, opportunity_kind, availability_basis,
                  include_in_live_market_estimate, first_seen_at, last_seen_at, is_active
                ) VALUES (
                  9005, 9001, 9002, 'acme-9005', 'Alternate source variant',
                  'Remote', 'https://apply.example.test/acme-9005', 'source-hash-9005',
                  'live_posting', 'api_feed', 1, ?, ?, 1
                )
                """,
                (OBSERVED_AT, OBSERVED_AT),
            )
            connection.commit()
        finally:
            connection.close()

        jobs = self.load()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["canonical_opportunity_id"], 9002)
        self.assertEqual(jobs[0]["job_id"], 9003)
        self.assertEqual(jobs[0]["path"], JOB_PATH)

    def test_representative_source_changes_do_not_change_public_identity(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(
                """
                INSERT INTO jobs (
                  id, company_id, canonical_opportunity_id, external_id, title,
                  location, url, source_hash, opportunity_kind, availability_basis,
                  include_in_live_market_estimate, first_seen_at, last_seen_at, is_active
                ) VALUES (
                  9005, 9001, 9002, 'acme-9005', 'Updated representative variant',
                  'Remote — Brazil', 'https://apply.example.test/acme-9005',
                  'source-hash-9005', 'live_posting', 'api_feed', 1, ?, ?, 1
                )
                """,
                (OBSERVED_AT, "2026-08-16T12:31:00+00:00"),
            )
            body = "Employer source description for the updated representative. " * 10
            connection.execute(
                """
                INSERT INTO job_source_contents (
                  job_id, provider, source_type, source_url, external_id, body,
                  body_format, metadata_json, material_content_sha256,
                  source_updated_at, first_captured_at, last_captured_at
                ) VALUES (9005, 'acme', 'official-job-detail-v1', ?, 'acme-9005',
                  ?, 'text/plain', '{}', ?, ?, ?, ?)
                """,
                (
                    "https://apply.example.test/acme-9005",
                    body,
                    "b" * 64,
                    OBSERVED_AT,
                    OBSERVED_AT,
                    OBSERVED_AT,
                ),
            )
            connection.commit()

            catalog_job = public_jobs_catalog.load_public_jobs(connection, now=NOW)[0]
            detail_job = public_job_page.load_public_job(connection, JOB_PATH, now=NOW)
            self.assertEqual((catalog_job["job_id"], detail_job["job_id"]), (9005, 9005))
            self.assertEqual((catalog_job["path"], detail_job["path"]), (JOB_PATH, JOB_PATH))

            connection.execute("UPDATE jobs SET is_active = 0 WHERE id = 9005")
            connection.commit()
            restored_representative = public_job_page.load_public_job(
                connection,
                JOB_PATH,
                now=NOW,
            )
            self.assertEqual(restored_representative["job_id"], 9003)
            self.assertEqual(restored_representative["path"], JOB_PATH)
        finally:
            connection.close()

    def test_structured_filters_and_keyword_search_are_candidate_facing(self):
        jobs = self.load()
        catalog = public_jobs_catalog.build_catalog(jobs)
        self.assertNotIn("return_to", catalog)
        self.assertEqual(catalog["inventory_count"], 1)
        self.assertIn(
            "Brazil",
            {item["value"] for item in catalog["facets"]["location"]},
        )
        self.assertNotIn(
            "Remote",
            {item["value"] for item in catalog["facets"]["location"]},
        )
        self.assertIn(
            "AI training & evaluation",
            {item["value"] for item in catalog["facets"]["work"]},
        )
        self.assertIn(
            "Software development",
            {item["value"] for item in catalog["facets"]["work"]},
        )
        self.assertIn(
            "Software engineering",
            {item["value"] for item in catalog["facets"]["field"]},
        )
        self.assertIn(
            "English",
            {item["value"] for item in catalog["facets"]["language"]},
        )
        self.assertIn(
            "Contract",
            {item["value"] for item in catalog["facets"]["arrangement"]},
        )
        for filters in (
            {"q": "Python evaluation"},
            {"location": "Remote"},
            {"location": "Brazil"},
            {"work": "AI training & evaluation"},
            {"work": "Software development"},
            {"field": "Software engineering"},
            {"language": "English"},
            {"arrangement": "Contract"},
        ):
            self.assertEqual(public_jobs_catalog.build_catalog(jobs, filters)["result_count"], 1)
        self.assertEqual(
            public_jobs_catalog.build_catalog(jobs, {"q": "nursing"})["result_count"],
            0,
        )

    def test_partial_or_missing_enrichment_omits_unknown_content_without_hiding_job(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("DELETE FROM opportunity_enrichment_overrides")
            connection.execute("DELETE FROM opportunity_enrichments")
            connection.commit()
        finally:
            connection.close()

        jobs = self.load()
        self.assertEqual(len(jobs), 1)
        self.assertIsNone(jobs[0]["enrichment_status"])
        self.assertIsNone(jobs[0]["catalog_summary"])
        fallback_catalog = public_jobs_catalog.build_catalog(jobs)
        self.assertIn(
            "Brazil",
            {item["value"] for item in fallback_catalog["facets"]["location"]},
        )
        self.assertEqual(fallback_catalog["facets"]["work"], [])
        self.assertEqual(fallback_catalog["facets"]["field"], [])
        self.assertIn(
            "Contract",
            {item["value"] for item in fallback_catalog["facets"]["arrangement"]},
        )
        page = public_jobs_catalog.render_public_jobs_page(
            fallback_catalog,
            public_origin=ORIGIN,
        )
        self.assertIn("Applied AI Engineer — Model Evaluation", page)
        self.assertIn("Eligible in Brazil", page)
        self.assertNotIn(">Unknown<", page)
        self.assertNotIn("enrichment", page.casefold())
        self.assertIn("name='work'", page)
        self.assertIn("name='field'", page)
        self.assertNotIn("name='arrangement'", page)
        self.assertGreaterEqual(page.count("placeholder='Not available yet'"), 2)

        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            detail = public_job_page.load_public_job(connection, JOB_PATH, now=NOW)
        finally:
            connection.close()
        self.assertIsNotNone(detail)
        detail_page = public_job_page.render_public_job_page(
            detail,
            public_origin=ORIGIN,
        )
        self.assertIn("Applied AI Engineer — Model Evaluation", detail_page)
        self.assertNotIn(">Unknown<", detail_page)

    def test_location_filter_uses_candidate_eligibility_without_expanding_worldwide(self):
        brazil = self.load()[0]
        worldwide = deepcopy(brazil)
        worldwide.update(
            job_id=40_001,
            path="/job/opportunity-40001",
            source_location="Remote worldwide",
        )
        worldwide_arrangement = worldwide["enrichment"]["attributes"][
            "work_arrangement"
        ]
        worldwide_arrangement.update(
            workplace_mode="remote",
            location_scope="remote_worldwide",
            eligible_countries=[],
            eligible_regions=[],
            eligible_locations=[],
        )
        public_jobs_catalog.prepare_catalog_presentation(worldwide)

        americas = deepcopy(brazil)
        americas.update(
            job_id=40_002,
            path="/job/opportunity-40002",
            source_location="Remote — Americas",
        )
        americas_arrangement = americas["enrichment"]["attributes"][
            "work_arrangement"
        ]
        americas_arrangement.update(
            workplace_mode="remote",
            location_scope="remote_restricted",
            eligible_countries=[],
            eligible_regions=["Americas"],
            eligible_locations=[],
        )
        public_jobs_catalog.prepare_catalog_presentation(americas)

        united_states = deepcopy(brazil)
        united_states.update(
            job_id=40_003,
            path="/job/opportunity-40003",
            source_location="Remote — United States",
        )
        us_arrangement = united_states["enrichment"]["attributes"][
            "work_arrangement"
        ]
        us_arrangement.update(
            workplace_mode="remote",
            location_scope="remote_restricted",
            eligible_countries=["United States"],
            eligible_regions=[],
            eligible_locations=[],
        )
        public_jobs_catalog.prepare_catalog_presentation(united_states)

        jobs = [brazil, worldwide, americas, united_states]
        brazil_catalog = public_jobs_catalog.build_catalog(
            jobs,
            {"location": "Brazil"},
        )
        self.assertEqual(brazil_catalog["result_count"], 3)
        self.assertEqual(
            {job["job_id"] for job in brazil_catalog["jobs"]},
            {brazil["job_id"], worldwide["job_id"], americas["job_id"]},
        )
        brazil_option = next(
            item
            for item in brazil_catalog["facets"]["location"]
            if item["label"] == "Brazil"
        )
        self.assertEqual(brazil_option["count"], 3)
        self.assertNotIn(
            public_jobs_catalog.facet_value_key("Brazil"),
            worldwide["_catalog_filter_values"]["location"],
        )
        self.assertEqual(worldwide["catalog_location"], "Work from anywhere")
        self.assertNotIn(
            "Remote",
            {item["label"] for item in brazil_catalog["facets"]["location"]},
        )
        self.assertEqual(
            public_jobs_catalog.build_catalog(
                jobs,
                {"location": "EMEA"},
            )["result_count"],
            1,
        )

    def test_inactive_and_stale_live_feed_jobs_are_not_public_current_inventory(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("UPDATE jobs SET is_active=0 WHERE id=9003")
            connection.commit()
            self.assertEqual(public_jobs_catalog.load_public_jobs(connection, now=NOW), [])
            unavailable = public_job_page.load_public_job(connection, JOB_PATH, now=NOW)
            self.assertEqual(
                unavailable["public_state"],
                public_job_page.PUBLIC_JOB_STATE_TEMPORARILY_UNAVAILABLE,
            )

            connection.execute("UPDATE jobs SET is_active=1 WHERE id=9003")
            connection.execute("UPDATE canonical_opportunities SET is_active=0 WHERE id=9002")
            connection.commit()
            self.assertEqual(public_jobs_catalog.load_public_jobs(connection, now=NOW), [])

            connection.execute("UPDATE canonical_opportunities SET is_active=1 WHERE id=9002")
            connection.commit()
            stale_now = NOW + timedelta(hours=73)
            self.assertEqual(public_jobs_catalog.load_public_jobs(connection, now=stale_now), [])
            self.assertEqual(
                public_job_page.load_public_job(
                    connection,
                    JOB_PATH,
                    now=stale_now,
                )["public_state"],
                public_job_page.PUBLIC_JOB_STATE_TEMPORARILY_UNAVAILABLE,
            )
        finally:
            connection.close()

    def test_unavailable_job_is_noindex_nonactionable_and_restores_at_same_url(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("UPDATE jobs SET is_active = 0 WHERE id = 9003")
            connection.commit()
        finally:
            connection.close()

        integration = self.integration()
        unavailable = integration.handle(
            "GET",
            JOB_PATH,
            (("Host", "app.test"),),
        )
        unavailable_body = unavailable.body.decode("utf-8")
        self.assertEqual(unavailable.status, 200)
        self.assertEqual(
            dict(unavailable.headers)["X-Robots-Tag"],
            "noindex, follow",
        )
        self.assertIn("Opportunity unavailable", unavailable_body)
        self.assertIn(f"rel='canonical' href='{ORIGIN}{JOB_PATH}'", unavailable_body)
        self.assertIn("href='/jobs'>← Back to jobs</a>", unavailable_body)
        self.assertNotIn("return_to=", unavailable_body)
        self.assertNotIn("Apply on company site", unavailable_body)
        self.assertNotIn("application/ld+json", unavailable_body)

        connection = sqlite3.connect(self.path)
        try:
            connection.execute("UPDATE jobs SET is_active = 1 WHERE id = 9003")
            connection.commit()
        finally:
            connection.close()
        restored = integration.handle("GET", JOB_PATH, (("Host", "app.test"),))
        self.assertEqual(restored.status, 200)
        self.assertNotIn("X-Robots-Tag", dict(restored.headers))
        self.assertIn("Apply on company site", restored.body.decode("utf-8"))

    def test_catalog_route_is_public_searchable_and_filter_urls_are_noindex(self):
        integration = self.integration()
        self.assertTrue(integration.matches_route("/jobs"))

        response = integration.handle("GET", "/jobs", (("Host", "app.test"),))
        body = response.body.decode("utf-8")
        self.assertEqual(response.status, 200, body)
        self.assertEqual(dict(response.headers)["Cache-Control"], "public, max-age=300")
        self.assertIn("Browse current opportunities", body)
        self.assertIn("Showing 1–1 of 1 current opportunities", body)
        self.assertIn(f"href='{JOB_PATH}'", body)
        self.assertNotIn("return_to=", body)
        self.assertNotIn("name='robots'", body)
        self.assertNotIn("href='/jobs?", body)
        self.assertEqual(body.count("class='jobs-list'"), 1)

        filtered = integration.handle(
            "GET",
            "/jobs?q=Python&location=Brazil",
            (("Host", "app.test"),),
        )
        filtered_body = filtered.body.decode("utf-8")
        self.assertEqual(filtered.status, 200, filtered_body)
        self.assertIn("<meta name='robots' content='noindex,follow'>", filtered_body)
        self.assertNotIn("rel='canonical'", filtered_body)
        self.assertEqual(
            dict(filtered.headers)["X-Robots-Tag"],
            "noindex, follow",
        )
        self.assertIn("Showing 1–1 of 1 current opportunities", filtered_body)
        self.assertIn("<datalist id='jobs-location-options'>", filtered_body)
        self.assertIn("<span>Where can you work from?</span>", filtered_body)
        self.assertIn("placeholder='Country or region'", filtered_body)
        self.assertNotIn("<span>Work arrangement</span>", filtered_body)
        self.assertNotIn("name='arrangement'", filtered_body)
        self.assertNotIn("<select", filtered_body)

        rejected = integration.handle("POST", "/jobs", (("Host", "app.test"),))
        self.assertEqual(rejected.status, 405)
        malformed = integration.handle(
            "GET",
            "/jobs?q=%FF",
            (("Host", "app.test"),),
        )
        self.assertEqual(malformed.status, 400)
        self.assertEqual(
            dict(malformed.headers)["X-Robots-Tag"],
            "noindex, nofollow",
        )

    def test_query_normalization_redirects_without_false_filter_canonical(self):
        integration = self.integration()
        normalized = integration.handle(
            "GET",
            "/jobs?location=Brazil&q=%20Python%20&page=1",
            (("Host", "app.test"),),
        )
        self.assertEqual(normalized.status, 301)
        self.assertEqual(
            dict(normalized.headers)["Location"],
            "/jobs?q=Python&location=Brazil",
        )
        page_one = integration.handle(
            "GET",
            "/jobs?page=1",
            (("Host", "app.test"),),
        )
        self.assertEqual(page_one.status, 301)
        self.assertEqual(dict(page_one.headers)["Location"], "/jobs")
        for empty_target in ("/jobs?", "/jobs?q="):
            with self.subTest(empty_target=empty_target):
                empty = integration.handle(
                    "GET",
                    empty_target,
                    (("Host", "app.test"),),
                )
                self.assertEqual(empty.status, 301)
                self.assertEqual(dict(empty.headers)["Location"], "/jobs")

        filtered = integration.handle(
            "GET",
            "/jobs?q=Python&location=Brazil",
            (("Host", "app.test"),),
        )
        body = filtered.body.decode("utf-8")
        self.assertEqual(filtered.status, 200)
        self.assertIn("name='robots' content='noindex,follow'", body)
        self.assertNotIn("rel='canonical'", body)

    def test_unfiltered_pagination_is_indexable_and_self_canonical(self):
        base = self.load()[0]
        jobs = []
        for index in range(31):
            job = deepcopy(base)
            job["job_id"] = 80_000 + index
            job["canonical_opportunity_id"] = 80_000 + index
            job["path"] = f"/job/opportunity-{80_000 + index}"
            job["source_title"] = f"Role {index:02d}"
            public_jobs_catalog.prepare_catalog_presentation(job)
            jobs.append(job)
        catalog = public_jobs_catalog.build_catalog(jobs, {"page": 2})
        page = public_jobs_catalog.render_public_jobs_page(
            catalog,
            public_origin=ORIGIN,
        )
        self.assertNotIn("name='robots'", page)
        self.assertIn(
            "rel='canonical' href='https://app.test/jobs?page=2'",
            page,
        )
        self.assertIn("Page 2", page)

    def test_catalog_paginates_and_preserves_search_and_filter_state(self):
        base = self.load()[0]
        jobs = []
        first_seen = datetime(2026, 1, 1)
        for index in range(65):
            job = deepcopy(base)
            job["job_id"] = 10_000 + index
            job["path"] = f"/job/opportunity-{10_000 + index}"
            job["source_title"] = f"Python Engineer {index:02d}"
            job["source_updated_at"] = None
            job["job_first_seen_at"] = (first_seen + timedelta(days=index)).isoformat()
            public_jobs_catalog.prepare_catalog_presentation(job)
            jobs.append(job)

        catalog = public_jobs_catalog.build_catalog(
            jobs,
            {"q": "Python", "location": "Brazil", "page": "2"},
        )
        self.assertEqual(catalog["result_count"], 65)
        self.assertEqual(catalog["page_result_count"], 30)
        self.assertEqual(catalog["page"], 2)
        self.assertEqual(catalog["total_pages"], 3)
        self.assertEqual(catalog["first_result_number"], 31)
        self.assertEqual(catalog["last_result_number"], 60)
        self.assertEqual(catalog["jobs"][0]["source_title"], "Python Engineer 34")
        page = public_jobs_catalog.render_public_jobs_page(
            catalog,
            public_origin=ORIGIN,
            query_present=True,
        )
        self.assertIn("Page 2 of 3", page)
        self.assertIn("/jobs?q=Python&amp;location=Brazil", page)
        self.assertIn("/jobs?q=Python&amp;location=Brazil&amp;page=3", page)
        self.assertNotIn("return_to=", page)
        self.assertEqual(page.count("class='job-card'"), public_jobs_catalog.PAGE_SIZE)

    def test_recency_uses_source_update_then_first_seen_and_search_uses_relevance(self):
        base = self.load()[0]
        source_updated = deepcopy(base)
        source_updated.update(
            job_id=10_001,
            path="/job/opportunity-10001",
            source_title="Operations Specialist",
            source_expertise="Python workflows",
            source_updated_at="2026-04-01T00:00:00+00:00",
            job_first_seen_at="2025-01-01T00:00:00+00:00",
            job_last_seen_at="2026-08-15T00:00:00+00:00",
        )
        title_match = deepcopy(base)
        title_match.update(
            job_id=10_002,
            path="/job/opportunity-10002",
            source_title="Python Engineer",
            source_expertise="",
            source_updated_at=None,
            job_first_seen_at="2026-03-01T00:00:00+00:00",
            job_last_seen_at="2099-01-01T00:00:00+00:00",
        )
        for job in (source_updated, title_match):
            public_jobs_catalog.prepare_catalog_presentation(job)

        recent = public_jobs_catalog.build_catalog([title_match, source_updated])
        self.assertEqual(recent["jobs"][0]["job_id"], source_updated["job_id"])
        relevant = public_jobs_catalog.build_catalog(
            [source_updated, title_match],
            {"q": "Python"},
        )
        self.assertEqual(relevant["jobs"][0]["job_id"], title_match["job_id"])

        alphabetical_first = deepcopy(base)
        alphabetical_first.update(
            job_id=20_001,
            path="/job/opportunity-20001",
            source_title="Aardvark Role",
            source_updated_at=None,
            job_first_seen_at="2026-02-01T00:00:00+00:00",
        )
        later_identity = deepcopy(base)
        later_identity.update(
            job_id=20_002,
            path="/job/opportunity-20002",
            source_title="Zoology Role",
            source_updated_at=None,
            job_first_seen_at="2026-02-01T00:00:00+00:00",
        )
        for job in (alphabetical_first, later_identity):
            public_jobs_catalog.prepare_catalog_presentation(job)
        tied = public_jobs_catalog.build_catalog(
            [alphabetical_first, later_identity]
        )
        self.assertEqual(tied["jobs"][0]["job_id"], later_identity["job_id"])

    def test_default_order_interleaves_company_batches_and_keeps_company_recency(self):
        base = self.load()[0]
        jobs = []
        for company_index, company in enumerate(("Alpha", "Beta", "Gamma")):
            for position in range(3):
                job = deepcopy(base)
                job.update(
                    job_id=50_000 + company_index * 10 + position,
                    path=f"/job/{company.casefold()}-{50_000 + company_index * 10 + position}",
                    company_name=company,
                    company_slug=company.casefold(),
                    source_title=f"{company} role {position}",
                    source_updated_at=None,
                    job_first_seen_at=(
                        datetime(2026, 8, 10 - company_index)
                        - timedelta(days=position)
                    ).isoformat(),
                )
                public_jobs_catalog.prepare_catalog_presentation(job)
                jobs.append(job)

        ordered = public_jobs_catalog.build_catalog(jobs)["jobs"]
        companies = [job["company_name"] for job in ordered]
        self.assertEqual(companies, ["Alpha", "Beta", "Gamma"] * 3)
        for company in ("Alpha", "Beta", "Gamma"):
            company_dates = [
                job["job_first_seen_at"]
                for job in ordered
                if job["company_name"] == company
            ]
            self.assertEqual(company_dates, sorted(company_dates, reverse=True))

    def test_work_and_professional_field_filters_are_separate_multi_label_dimensions(self):
        jobs = self.load()
        catalog = public_jobs_catalog.build_catalog(jobs)
        work_labels = {item["label"] for item in catalog["facets"]["work"]}
        field_labels = {item["label"] for item in catalog["facets"]["field"]}
        self.assertEqual(
            work_labels,
            {"AI training & evaluation", "Software development"},
        )
        self.assertEqual(field_labels, {"Software engineering"})
        self.assertNotIn("AI & machine learning", work_labels)
        self.assertNotIn("Finance & mathematics", field_labels)
        self.assertEqual(
            public_jobs_catalog.build_catalog(
                jobs,
                {"work": "AI training & evaluation", "field": "Software engineering"},
            )["result_count"],
            1,
        )
        page = public_jobs_catalog.render_public_jobs_page(
            catalog,
            public_origin=ORIGIN,
        )
        self.assertNotIn("role-category-", page)
        self.assertNotIn("activity-ai-training", page)
        self.assertNotIn("language-english", page)
        self.assertIn("<span>Type of work</span>", page)
        self.assertIn("<span>Professional field</span>", page)

    def test_cards_summarize_broad_eligibility_and_omit_unknown_values(self):
        job = deepcopy(self.load()[0])
        arrangement = job["enrichment"]["attributes"]["work_arrangement"]
        arrangement.update(
            location_scope="remote_restricted",
            eligible_countries=[
                "Argentina",
                "Brazil",
                "Chile",
                "Colombia",
                "Peru",
            ],
            eligible_regions=[],
            eligible_locations=[],
        )
        job["source_location"] = "Remote — Argentina, Brazil, Chile, Colombia, Peru"
        requirements = job["enrichment"]["attributes"]["requirements"]
        requirements["languages"] = [
            {"language": "Unknown", "locale": None, "requirement_mode": "unknown"}
        ]
        job["canonical_language"] = "Unknown"
        job["enrichment"]["attributes"]["content"]["quick_take"] = "Unknown"
        public_jobs_catalog.prepare_catalog_presentation(job)

        card = public_jobs_catalog.render_job_card(job, return_to="/jobs")
        self.assertIn("Eligible in 5 countries", card)
        self.assertNotIn("Remote — Argentina", card)
        self.assertNotIn("Unknown", card)
        self.assertLessEqual(card.count("<li>"), 3)

    def test_oneforma_card_uses_job_variant_instead_of_project_language_packet(self):
        job = deepcopy(self.load()[0])
        job.update(
            source_location="Remote; Selected Locations",
            rich_source_type="oneforma-wordpress-marketplace",
            rich_metadata_json='{"variant_language":"Arabic - Saudi Arabia"}',
        )
        arrangement = job["enrichment"]["attributes"]["work_arrangement"]
        arrangement.update(
            workplace_mode="remote",
            location_scope="remote_restricted",
            eligible_countries=[],
            eligible_regions=[],
            eligible_locations=["Remote; Selected Locations"],
        )
        job["enrichment"]["attributes"]["requirements"]["languages"] = [
            {"language": language, "locale": None, "requirement_mode": "ambiguous"}
            for language in ("arabic", "chinese", "danish", "english")
        ]

        public_jobs_catalog.prepare_catalog_presentation(job)
        catalog = public_jobs_catalog.build_catalog([job])
        card = public_jobs_catalog.render_job_card(job, return_to="/jobs")

        self.assertEqual(job["catalog_location"], "Eligible in Saudi Arabia")
        self.assertEqual(
            {item["label"] for item in catalog["facets"]["location"]},
            {"Saudi Arabia"},
        )
        self.assertEqual(
            {item["label"] for item in catalog["facets"]["language"]},
            {"Arabic"},
        )
        self.assertIn("Eligible in Saudi Arabia", card)
        self.assertIn(">Arabic<", card)
        self.assertNotIn("Selected Locations", card)
        self.assertNotIn("Ambiguous", card)
        self.assertNotIn(">Chinese<", card)

    def test_location_facet_count_resolves_each_option_once(self):
        base = self.load()[0]
        jobs = []
        for index, country in enumerate(("Brazil", "Portugal", "United States")):
            job = deepcopy(base)
            job["job_id"] = 70_000 + index
            job["path"] = f"/job/opportunity-{70_000 + index}"
            arrangement = job["enrichment"]["attributes"]["work_arrangement"]
            arrangement.update(
                location_scope="remote_restricted",
                eligible_countries=[country],
                eligible_regions=[],
                eligible_locations=[],
            )
            public_jobs_catalog.prepare_catalog_presentation(job)
            jobs.append(job)

        calls = []
        original = public_jobs_catalog.location_filter_identity

        def counted(value):
            calls.append(value)
            return original(value)

        public_jobs_catalog.location_filter_identity = counted
        try:
            catalog = public_jobs_catalog.build_catalog(jobs)
        finally:
            public_jobs_catalog.location_filter_identity = original
        self.assertEqual(len(calls), len(catalog["facets"]["location"]))

    def test_work_fields_and_languages_each_support_multiple_labels(self):
        job = deepcopy(self.load()[0])
        role = job["enrichment"]["attributes"]["role"]
        role["work_activities"] = [
            "ai_training_evaluation",
            "audio_speech",
            "data_annotation",
        ]
        role["professional_domains"] = ["finance", "mathematics"]
        job["enrichment"]["attributes"]["requirements"]["languages"] = [
            {"language": "English", "locale": None, "requirement_mode": "all_required"},
            {"language": "Portuguese", "locale": None, "requirement_mode": "all_required"},
        ]
        public_jobs_catalog.prepare_catalog_presentation(job)
        catalog = public_jobs_catalog.build_catalog([job])
        self.assertEqual(
            {item["label"] for item in catalog["facets"]["work"]},
            {"AI training & evaluation", "Audio & speech", "Data annotation"},
        )
        self.assertEqual(
            {item["label"] for item in catalog["facets"]["field"]},
            {"Finance", "Mathematics"},
        )
        self.assertEqual(
            {item["label"] for item in catalog["facets"]["language"]},
            {"English", "Portuguese"},
        )
        for filters in (
            {"work": "Audio & speech"},
            {"field": "Finance"},
            {"field": "Mathematics"},
            {"language": "Portuguese"},
        ):
            self.assertEqual(
                public_jobs_catalog.build_catalog([job], filters)["result_count"],
                1,
            )

    def test_job_back_link_restores_validated_catalog_context(self):
        integration = self.integration()
        return_to = "/jobs?q=Python&location=Brazil&page=2"
        target = JOB_PATH + "?" + urlencode({"return_to": return_to})
        response = integration.handle("GET", target, (("Host", "app.test"),))
        body = response.body.decode("utf-8")
        self.assertEqual(response.status, 200, body)
        self.assertIn(
            "href='/jobs?q=Python&amp;location=Brazil&amp;page=2'>← Back to jobs</a>",
            body,
        )
        self.assertIn("<meta name='robots' content='noindex,follow'>", body)

        direct = integration.handle("GET", JOB_PATH, (("Host", "app.test"),))
        self.assertEqual(direct.status, 200)
        direct_body = direct.body.decode("utf-8")
        self.assertIn("href='/jobs'>← Back to jobs</a>", direct_body)
        self.assertNotIn("return_to=", direct_body)

        for unsafe in (
            "https://evil.test/jobs",
            "//evil.test/jobs",
            "/jobs?next=https://evil.test",
            "/find-matches",
        ):
            rejected = integration.handle(
                "GET",
                JOB_PATH + "?" + urlencode({"return_to": unsafe}),
                (("Host", "app.test"),),
            )
            self.assertEqual(rejected.status, 400, unsafe)


if __name__ == "__main__":
    unittest.main()
