from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from wahojobs import authenticated_profile_matches as matches_module
from wahojobs import public_job_page
from wahojobs.authenticated_profile_matches import (
    AuthenticatedProfileMatchesBrowserIntegration,
    AuthenticatedProfileMatchesService,
)
from wahojobs.matching.metadata_overlay import OpportunityMetadataOverlay
from wahojobs.opportunity_enrichment import blank_document, validate_enrichment_document


ORIGIN = "https://app.test"
OBSERVED_AT = "2026-08-16T12:30:00+00:00"
JOB_PATH = "/job/acme-ai-9003"


def seed_public_job(connection):
    schema = Path(__file__).resolve().parents[1] / "wahojobs" / "db" / "schema.sql"
    connection.executescript(schema.read_text(encoding="utf-8"))
    connection.execute(
        "INSERT INTO companies "
        "(id, name, slug, careers_url, source_tier, inventory_model, market_count_policy) "
        "VALUES (9001, 'Acme AI', 'acme-ai', 'https://careers.example.test/acme', "
        "'core', 'live_feed', 'count_live')"
    )
    connection.execute(
        """
        INSERT INTO canonical_opportunities (
          id, company_id, canonical_key, canonical_title, normalized_title,
          source_category, language, language_locale, first_seen_at,
          last_seen_at, is_active, variant_count
        ) VALUES (
          9002, 9001, 'applied-ai-engineer', 'Applied AI Engineer',
          'applied ai engineer', 'Software Engineering', NULL, NULL, ?, ?, 1, 1
        )
        """,
        (OBSERVED_AT, OBSERVED_AT),
    )
    connection.execute(
        """
        INSERT INTO jobs (
          id, company_id, canonical_opportunity_id, external_id, title,
          location, department, expertise, commitment, url, source_hash,
          opportunity_kind, availability_basis, include_in_live_market_estimate,
          first_seen_at, last_seen_at, is_active
        ) VALUES (
          9003, 9001, 9002, 'acme-9003', 'Applied AI Engineer — Model Evaluation',
          'Remote — Brazil', 'AI Engineering > Model Evaluation', 'Python and model evaluation',
          'Contract', 'https://apply.example.test/acme-9003', 'source-hash-9003',
          'live_posting', 'api_feed', 1, ?, ?, 1
        )
        """,
        (OBSERVED_AT, OBSERVED_AT),
    )
    source_body = "Build evaluation systems. " * 40
    connection.execute(
        """
        INSERT INTO job_source_contents (
          job_id, provider, source_type, source_url, external_id, body,
          body_format, metadata_json, material_content_sha256,
          source_updated_at, first_captured_at, last_captured_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)
        """,
        (
            9003,
            "acme_official_api",
            "official-job-detail-v1",
            "https://apply.example.test/acme-9003",
            "acme-9003",
            source_body,
            "text/plain",
            hashlib.sha256(source_body.encode("utf-8")).hexdigest(),
            "2026-08-15T16:00:00+00:00",
            OBSERVED_AT,
            OBSERVED_AT,
        ),
    )
    connection.execute(
        """
        INSERT INTO crawl_runs (
          id, company_id, status, started_at, finished_at, jobs_found_count,
          used_sample_data, error_message
        ) VALUES (9004, 9001, 'success', ?, ?, 1, 0, NULL)
        """,
        (OBSERVED_AT, OBSERVED_AT),
    )

    document = blank_document()
    document["source"] = {
        "company_name": "Acme AI",
        "company_slug": "acme-ai",
        "canonical_key": "applied-ai-engineer",
        "canonical_title": "Applied AI Engineer",
        "source_category": "Software Engineering",
        "source_tier": "core",
        "inventory_model": "live_feed",
        "market_count_policy": "count_live",
        "opportunity_kinds": ["live_posting"],
        "availability_bases": ["api_feed"],
        "include_in_live_market_estimate": True,
    }
    attributes = document["attributes"]
    attributes["role"].update(
        role_family="software_engineering",
        professional_domains=["technical"],
        work_activities=["ai_training_evaluation", "software_development"],
        specializations=["LLM evaluation"],
        seniority="mid",
    )
    attributes["work_arrangement"].update(
        workplace_mode="remote",
        location_scope="remote_restricted",
        eligible_countries=["Brazil"],
        engagement_type="contract",
        hours_per_week_min=20,
        hours_per_week_max=30,
        duration="Six months",
    )
    attributes["requirements"].update(
        languages=[
            {"language": "English", "locale": None, "requirement_mode": "single"}
        ],
        skills_required=["LLM evaluation", "Python"],
        skills_preferred=["Prompt design"],
        years_experience_min=2,
    )
    attributes["compensation"].update(
        disclosed=True,
        currency="USD",
        amount_min=35,
        amount_max=50,
        period="hour",
        amount_type="range",
    )
    attributes["application"].update(
        application_url="https://apply.example.test/acme-9003",
        assessment_required=True,
    )
    attributes["content"].update(
        quick_take="Automatic source-backed overview.",
        responsibilities=[
            "Build repeatable model-evaluation workflows.",
            "Review model behavior with domain experts.",
        ],
        candidate_profile="An engineer comfortable with Python and evaluation design.",
        benefits=["Flexible remote schedule"],
        caveats=[
            "The contract is currently scoped to candidates in Brazil.",
            "The listing indicates a degree is required (metadata field)",
        ],
    )
    validate_enrichment_document(document)
    connection.execute(
        """
        INSERT INTO opportunity_enrichments (
          canonical_opportunity_id, schema_version, taxonomy_version,
          extractor_version, input_sha256, status, automatic_document_json,
          model_provider, model_name, prompt_version, generated_at
        ) VALUES (9002, ?, ?, ?, ?, 'complete', ?, 'openai', 'fixture-model', ?, ?)
        """,
        (
            document["schema_version"],
            document["taxonomy_version"],
            "deterministic_plus_structured_llm_v1",
            "a" * 64,
            json.dumps(document, sort_keys=True, separators=(",", ":")),
            "fixture-prompt-v1",
            OBSERVED_AT,
        ),
    )
    connection.execute(
        """
        INSERT INTO opportunity_enrichment_overrides (
          canonical_opportunity_id, field_path, operation, value_json, actor,
          reason, provenance_json, automatic_input_sha256_at_override
        ) VALUES (9002, 'attributes.content.quick_take', 'set', ?,
          'fixture-reviewer', 'Clarify the candidate summary.', '{}', ?)
        """,
        (json.dumps("Reviewed effective Quick Take."), "a" * 64),
    )
    connection.commit()


class ReadOnlyProvider:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def __call__(self):
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


class PublicJobPageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "public-job.sqlite"
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            seed_public_job(connection)
        finally:
            connection.close()

    def load(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            return public_job_page.load_public_job(connection, JOB_PATH)
        finally:
            connection.close()

    def test_stable_path_loads_canonical_source_and_effective_enrichment(self):
        self.assertEqual(public_job_page.public_job_path("acme-ai", 9003), JOB_PATH)
        self.assertEqual(public_job_page.parse_public_job_path(JOB_PATH), ("acme-ai", 9003))
        self.assertIsNone(public_job_page.parse_public_job_path("/job/acme-ai"))

        job = self.load()
        self.assertEqual(job["canonical_title"], "Applied AI Engineer")
        self.assertEqual(job["source_title"], "Applied AI Engineer — Model Evaluation")
        self.assertEqual(job["source_department"], "AI Engineering > Model Evaluation")
        self.assertEqual(job["official_url"], "https://apply.example.test/acme-9003")
        self.assertEqual(job["careers_url"], "https://careers.example.test/acme")
        self.assertEqual(
            job["enrichment"]["attributes"]["content"]["quick_take"],
            "Reviewed effective Quick Take.",
        )
        self.assertEqual(job["enrichment_field_sources"]["attributes.content.quick_take"], "human_override")

    def test_logged_out_render_is_indexable_useful_and_omits_unknowns_and_json_ld(self):
        page = public_job_page.render_public_job_page(
            self.load(),
            public_origin=ORIGIN,
            authenticated=False,
            navigation="<nav><a href='/login'>Sign in</a></nav>",
        )
        for expected in (
            "Applied AI Engineer — Model Evaluation",
            "Acme AI",
            "Remote — Brazil",
            "USD 35–USD 50 per hour",
            "Reviewed effective Quick Take.",
            "What this opportunity is about",
            "What you&#x27;ll do",
            "What they&#x27;re looking for",
            "Important things to know",
            "AI training &amp; evaluation",
            "Assessment required",
            "What the employer highlights",
            "Flexible remote schedule",
            "The listing indicates a degree is required.",
            "Official source:",
            "Last verified:",
            "Apply on company site",
            "Create a profile or sign in",
            "Company representative? Contact Wahojobs if this listing needs an update or should be removed.",
            "Visit Acme AI careers",
        ):
            self.assertIn(expected, page)
        self.assertIn("rel='canonical' href='https://app.test/job/acme-ai-9003'", page)
        self.assertNotIn("application/ld+json", page)
        self.assertNotIn("JobPosting", page)
        self.assertNotIn(">Unknown<", page)
        self.assertNotIn("name='action'", page)
        self.assertNotIn("WahoJobs Quick Take", page)
        self.assertNotIn("Original listing details", page)
        self.assertNotIn("Role and work details", page)
        self.assertNotIn("Requirements and skills", page)
        self.assertNotIn("metadata field", page)
        self.assertNotIn("acme_official_api", page)
        self.assertNotIn("official-job-detail-v1", page)
        self.assertNotIn("provider:", page)
        self.assertNotIn("source type:", page)
        self.assertNotIn("English — Single", page)
        self.assertNotIn("AI Engineering &gt; Model Evaluation", page)
        self.assertNotIn("WahoJobs", page)
        self.assertIn("| Wahojobs</title>", page)
        self.assertEqual(page.count("class='content-section"), 4)
        self.assertEqual(page.count(">Remote — Brazil<"), 1)

    def test_api_endpoint_is_not_exposed_as_company_link(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE companies SET careers_url = ? WHERE id = 9001",
                ("https://boards-api.greenhouse.io/v1/boards/acme/jobs",),
            )
            connection.commit()
        finally:
            connection.close()

        job = self.load()
        self.assertIsNone(job["careers_url"])
        page = public_job_page.render_public_job_page(
            job,
            public_origin=ORIGIN,
            authenticated=False,
        )
        self.assertNotIn("<section class='company-strip'>", page)
        self.assertNotIn("Visit Acme AI careers", page)
        self.assertNotIn("boards-api.greenhouse.io", page)

    def test_api_source_urls_are_not_presented_as_application_or_original_listing_links(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE jobs SET url = ? WHERE id = 9003",
                ("https://jobs-api.example.test/v1/jobs/9003",),
            )
            connection.execute(
                "UPDATE job_source_contents SET source_url = ? WHERE job_id = 9003",
                ("https://jobs-api.example.test/api/jobs/9003",),
            )
            connection.commit()
        finally:
            connection.close()

        job = self.load()
        self.assertIsNone(job["official_url"])
        page = public_job_page.render_public_job_page(
            job,
            public_origin=ORIGIN,
            authenticated=False,
        )
        self.assertNotIn("jobs-api.example.test", page)
        self.assertNotIn("Apply on company site", page)

    def test_public_route_requires_no_session_and_uses_public_cache_policy(self):
        provider = ReadOnlyProvider(self.path)
        service = object.__new__(AuthenticatedProfileMatchesService)
        integration = AuthenticatedProfileMatchesBrowserIntegration(
            service,
            connection_provider=provider,
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

        self.assertTrue(integration.matches_route(JOB_PATH))
        response = integration.handle("GET", JOB_PATH, (("Host", "app.test"),))
        body = response.body.decode("utf-8")
        self.assertEqual(response.status, 200, body)
        self.assertEqual(dict(response.headers)["Cache-Control"], "public, max-age=300")
        self.assertIn("Reviewed effective Quick Take.", body)
        self.assertIn("Create a profile or sign in", body)
        self.assertNotIn("<meta name='robots' content='noindex", body)

        rejected = integration.handle("POST", JOB_PATH, (("Host", "app.test"),))
        self.assertEqual(rejected.status, 405)


if __name__ == "__main__":
    unittest.main()
