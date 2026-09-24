"""Labelled contract fixtures; live employer evidence stays outside Git."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from wahojobs.crawler.companies.dataannotation import crawl_dataannotation
from wahojobs.crawler.companies.dataforce import crawl_dataforce
from wahojobs.crawler.providers.dataannotation import (
    DOMAIN_PAGES, coding_role_evidence, fetch_dataannotation_jobs, parse_domain_page,
)
from wahojobs.crawler.providers.dataforce import validate_pagination
from wahojobs.crawler.types import CompanyCrawlResult, JobCandidate, ProviderOutcome
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import create_crawl_run, insert_job, install_base_schema
from wahojobs.source_capture import (
    SourceCaptureContext, prepare_record_promotion_attestation, prepare_source_capture,
)
from wahojobs.tracking.service import track_crawl_result
from wahojobs.tracking.normalize import with_source_hash


ROLE_URL = "https://www.dataannotation.tech/job-board/software-engineer"
ROLE_BODY = (
    '<html data-wf-item-slug="software-engineer"><div class="rd-title">'
    '&lt;h1&gt;Software Engineer&lt;/h1&gt;</div>'
    '<a href="https://app.dataannotation.tech/worker_signup?utm_role=software-engineer'
    '&amp;utm_content=role_bottom_software-engineer">Apply now</a></html>'
)
AT = "2026-09-24T00:49:42+00:00"


def partial(candidate):
    return CompanyCrawlResult(
        [candidate], False, "labelled fixture", "evergreen-application-pages",
        outcome=ProviderOutcome.PARTIAL, raw_record_count=1, normalized_record_count=1,
        payload_shape="dataannotation_coding_evergreen_record_v1",
        schema_fingerprint="dataannotation_coding_evergreen_record_v1",
    )


class DataAnnotationContractTests(unittest.TestCase):
    @patch("wahojobs.crawler.companies.dataannotation.fetch_dataannotation_jobs")
    def test_unattested_domains_do_not_enter_lifecycle(self, fetch_jobs):
        coding = parse_domain_page(DOMAIN_PAGES[0], ROLE_URL, ROLE_BODY)
        unqualified = JobCandidate(external_id="dataannotation::generalist", title="Generalist",
                                   location="Remote", url="https://www.dataannotation.tech/generalist")
        fetch_jobs.return_value = ([coding, unqualified], [])
        result = crawl_dataannotation("https://www.dataannotation.tech")
        self.assertEqual([job.external_id for job in result.jobs], ["dataannotation::coding"])
        self.assertEqual((result.raw_record_count, result.filtered_record_count), (2, 1))
        with tempfile.TemporaryDirectory() as directory:
            conn = get_connection(Path(directory) / "dataannotation-filter.sqlite")
            try:
                install_base_schema(conn)
                company_id = conn.execute(
                    "INSERT INTO companies(name,slug,careers_url,source_tier,inventory_model,market_count_policy) "
                    "VALUES('DataAnnotation','dataannotation','https://www.dataannotation.tech',"
                    "'core','evergreen_application','report_separately')"
                ).lastrowid
                old_id = insert_job(conn, company_id, with_source_hash("dataannotation", unqualified), AT)
                conn.execute("UPDATE jobs SET is_active=0, semantic_authority_state='versioned_accepted' WHERE id=?", (old_id,))
                summary = track_crawl_result(conn, company_id, create_crawl_run(conn, company_id, AT),
                                             result, AT, model_enrichment=False)
                self.assertEqual(summary.jobs_reactivated, 0)
                self.assertEqual(conn.execute("SELECT is_active FROM jobs WHERE id=?", (old_id,)).fetchone()[0], 0)
            finally:
                conn.close()

    @patch("wahojobs.crawler.providers.dataannotation.fetch_page")
    def test_unsupported_later_redirect_stops_and_retains_only_prior_page(self, fetch_page):
        fetch_page.side_effect = [
            {"ok": True, "text": ROLE_BODY, "url": ROLE_URL},
            ValueError("redirect left canonical scope"),
        ]
        jobs, skipped = fetch_dataannotation_jobs("https://www.dataannotation.tech")
        self.assertEqual([job.external_id for job in jobs], ["dataannotation::coding"])
        self.assertEqual(fetch_page.call_count, 2)
        self.assertIn("collection stopped", skipped[0])

    def test_role_specific_application_and_negative_pages(self):
        candidate = parse_domain_page(DOMAIN_PAGES[0], ROLE_URL, ROLE_BODY)
        self.assertEqual(candidate.title, "Software Engineer")
        self.assertEqual(candidate.external_id, "dataannotation::coding")
        self.assertEqual(candidate.opportunity_kind, "evergreen_application")
        self.assertFalse(candidate.include_in_live_market_estimate)
        for body in (
            ROLE_BODY.replace("Software Engineer", "Generic Landing"),
            ROLE_BODY.replace("utm_role=software-engineer", "utm_role=generalist"),
            ROLE_BODY.replace("app.dataannotation.tech", "example.test"),
            "<html>DataAnnotation coding Apply now</html>",
        ):
            with self.subTest(body=body[:70]), self.assertRaises(ValueError):
                coding_role_evidence(body, ROLE_URL)
        with self.assertRaises(ValueError):
            coding_role_evidence(ROLE_BODY, "https://www.dataannotation.tech/generalist")

    def test_attestation_rechecks_body_identity_and_partial_context(self):
        candidate = parse_domain_page(DOMAIN_PAGES[0], ROLE_URL, ROLE_BODY)
        result = partial(candidate)
        context = SourceCaptureContext.from_crawl_result(1, result)
        prepared = prepare_source_capture(candidate)
        attestation = prepare_record_promotion_attestation(
            candidate, prepared, context, provider="dataannotation",
            source_type=result.source_type,
        )
        self.assertEqual(attestation.contract_id, "dataannotation_coding_evergreen_record_v1")
        with self.assertRaises(ValueError):
            prepare_record_promotion_attestation(
                replace(candidate, title="Invented"), prepared, context,
                provider="dataannotation", source_type=result.source_type,
            )
        with self.assertRaises(ValueError):
            prepare_record_promotion_attestation(
                candidate, prepared, replace(context, provider_outcome="contract_drift"),
                provider="dataannotation", source_type=result.source_type,
            )

    def test_isolated_lifecycle_is_idempotent_without_absence_closure(self):
        candidate = parse_domain_page(DOMAIN_PAGES[0], ROLE_URL, ROLE_BODY)
        with tempfile.TemporaryDirectory() as directory:
            conn = get_connection(Path(directory) / "qualification.sqlite")
            try:
                install_base_schema(conn)
                company_id = conn.execute(
                    "INSERT INTO companies(name,slug,careers_url,source_tier,inventory_model,market_count_policy) "
                    "VALUES('DataAnnotation','dataannotation','https://www.dataannotation.tech',"
                    "'core','evergreen_application','exclude_from_live_count')"
                ).lastrowid
                first = track_crawl_result(conn, company_id, create_crawl_run(conn, company_id, AT),
                                           partial(candidate), AT, model_enrichment=False)
                again = track_crawl_result(conn, company_id, create_crawl_run(conn, company_id, AT),
                                           partial(candidate), AT, model_enrichment=False)
                failed = CompanyCrawlResult([], False, "fixture partial failure",
                                            "evergreen-application-pages", outcome=ProviderOutcome.PARTIAL)
                missing = track_crawl_result(conn, company_id, create_crawl_run(conn, company_id, AT),
                                             failed, AT, model_enrichment=False)
                self.assertEqual((first.jobs_new, again.jobs_new, missing.jobs_removed), (1, 0, 0))
                self.assertFalse(first.removals_authorized)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM canonical_opportunities").fetchone()[0], 1)
                self.assertEqual([row[0] for row in conn.execute(
                    "SELECT promotion_decision FROM job_source_content_captures ORDER BY id")],
                    ["promoted", "confirmed"])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM job_events WHERE event_type='discovered'").fetchone()[0], 1)
            finally:
                conn.close()


class DataForcePagerTests(unittest.TestCase):
    @patch("wahojobs.crawler.companies.dataforce.fetch_dataforce_jobs")
    def test_unqualified_index_cannot_reactivate_old_variant(self, fetch_jobs):
        candidate = JobCandidate(external_id="dataforce::project/fixture", title="Fixture role",
                                 location="Remote", url="https://dataforcecommunity.transperfect.com/project/fixture")
        fetch_jobs.return_value = [candidate]
        observed = crawl_dataforce("https://dataforcecommunity.transperfect.com/projects")
        self.assertEqual(observed.jobs, [])
        with tempfile.TemporaryDirectory() as directory:
            conn = get_connection(Path(directory) / "dataforce.sqlite")
            try:
                install_base_schema(conn)
                company_id = conn.execute(
                    "INSERT INTO companies(name,slug,careers_url,source_tier,inventory_model,market_count_policy) "
                    "VALUES('DataForce','dataforce','https://dataforcecommunity.transperfect.com/projects',"
                    "'core','live_feed','count_live')"
                ).lastrowid
                job_id = insert_job(conn, company_id, with_source_hash("dataforce", candidate), AT)
                conn.execute("UPDATE jobs SET is_active=0, semantic_authority_state='versioned_accepted' WHERE id=?", (job_id,))
                summary = track_crawl_result(conn, company_id, create_crawl_run(conn, company_id, AT),
                                             observed, AT, model_enrichment=False)
                self.assertEqual((summary.jobs_reactivated, summary.jobs_removed), (0, 0))
                self.assertEqual(conn.execute("SELECT is_active FROM jobs WHERE id=?", (job_id,)).fetchone()[0], 0)
            finally:
                conn.close()

    def test_generic_empty_and_out_of_range_pager_are_not_terminal_proof(self):
        generic = '<div class="view view-projects"><div class="view-empty">No projects</div>'
        with self.assertRaises(ValueError):
            validate_pagination(generic, 0)
        page_two_clamped = ('<option value="All" selected="selected">'
                            '<ul class="pagination js-pager__items">'
                            '<li class="page-item active"><span class="page-link">2</span></li></ul>')
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_pagination(page_two_clamped, 2)


if __name__ == "__main__":
    unittest.main()
