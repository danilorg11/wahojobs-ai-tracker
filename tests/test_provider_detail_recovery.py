"""Seven captured public pages; malformed/negative cases below are synthetic."""
from dataclasses import replace
from html import escape
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wahojobs.authenticated_source_detail import append_authenticated_source_detail
from wahojobs.crawler.provider_details import (
    DETAIL_KEY, DetailResponse, recover_detail, reprocess_saved_detail,
    validate_detail_url, _alignerr_applicant_location,
)
from wahojobs.crawler.providers.alignerr import parse_v2_record
from wahojobs.crawler.providers.micro1 import parse_micro1_job
from wahojobs.crawler.types import JobCandidate, CompanyCrawlResult, ProviderOutcome
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import (
    install_base_schema, insert_job, create_crawl_run, upsert_job_source_content,
    verify_job_source_acceptance_integrity,
)
from wahojobs.source_capture import SourceCaptureContext
from wahojobs.matching.locations import location_eligibility
from wahojobs.tracking.normalize import with_source_hash

ROOT = Path(__file__).parent / "fixtures/source_detail_recovery"
CASES = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))


def candidate(case):
    return JobCandidate(title=case["title"], external_id=case["external_id"],
                        url=case["url"], location="Remote", source_body="Listing teaser ...",
                        source_body_format="text/plain", source_metadata={"listing_fact": "retained"})


def response(case):
    return DetailResponse(case["url"], (ROOT / case["file"]).read_bytes(), case["observed_at"])


class ProviderDetailParsingTests(unittest.TestCase):
    def test_seven_captured_pages_recover_wording_and_keep_identity_provenance(self):
        for case in CASES:
            with self.subTest(rank=case["rank"]):
                original = candidate(case)
                recovered = recover_detail(case["provider"], original, response(case))
                self.assertEqual(recovered.external_id, original.external_id)
                self.assertEqual(recovered.url, original.url)
                self.assertEqual(recovered.source_metadata["listing_fact"], "retained")
                detail = recovered.source_metadata[DETAIL_KEY]
                self.assertEqual(detail["observed_at"], case["observed_at"])
                self.assertFalse(detail["application_acceptance_verified"])
                self.assertIn("Qualifications" if case["provider"] == "micro1" else
                              ("Requirements" if case["rank"] == 3 else "Who You Are"), recovered.source_body)
                if case["provider"] == "alignerr":
                    self.assertEqual(recovered.source_body, detail["record"]["longDescription"])
                else:
                    self.assertIn("Preferred Qualifications", recovered.source_body)
                    self.assertIn("Compensation is output-based", recovered.source_body)
                    self.assertIn("Application screening questions", recovered.source_body)
                    self.assertIn("OR minimum weekly commitment required", detail["display_text"])
                    self.assertIn("Portugal", detail["display_text"])
                    self.assertEqual(detail["record"]["ideal_hourly_rate"], {"max": 90, "min": 80})
                    self.assertIsNone(detail["record"]["location_type"])
                    self.assertEqual(recovered.location, "Remote")
                    self.assertEqual(detail["location_projection"]["source_quote"], "Location: Remote")

    def test_captured_country_alternatives_use_existing_gate_only_for_same_variant(self):
        case = next(c for c in CASES if c["rank"] == 4)
        recovered = recover_detail("alignerr", candidate(case), response(case))
        self.assertEqual(location_eligibility({"country": "Portugal"}, recovered.__dict__).status, "incompatible")
        for country in ("United States", "Canada", "United Kingdom", "Australia", "New Zealand"):
            self.assertEqual(location_eligibility({"country": country}, recovered.__dict__).status, "eligible")
        self.assertEqual(location_eligibility({}, recovered.__dict__).status, "unknown")
        other = CASES[0]
        other_recovered = recover_detail("alignerr", candidate(other), response(other))
        self.assertEqual(other_recovered.location, "Remote")
        self.assertEqual(other_recovered.source_metadata[DETAIL_KEY]["record"]["location"], "Vancouver")

    def test_synthetic_incidental_preferred_negative_and_ambiguous_locations_stay_raw(self):
        for body in (
            "## Nice to Have\n* Based in Canada",
            "## About Us\n* Based in Canada",
            "## Who You Are\n* Based in Canada preferred",
            "## Who You Are\n* Not based in Canada",
            "## Who You Are\n* Based in Canada or approved territories",
            "## Who You Are\n* Based in Canada\n* Based in Portugal",
            "## Who You Are\n* Canadian work authorization required",
        ):
            with self.subTest(body=body):
                self.assertEqual(_alignerr_applicant_location(body, "text/markdown", "Remote"), ("Remote", None))

    def test_listing_line_breaks_are_not_collapsed_and_missing_micro1_location_is_unknown(self):
        case = CASES[0]
        record = dict(id=case["external_id"], title=case["title"], applyUrl=case["url"],
                      location="Remote", category="STEM", description="Required:\nA or B\n\nPreferred:\nC")
        parsed, error = parse_v2_record(record)
        self.assertIsNone(error)
        self.assertEqual(parsed.source_body, record["description"])
        for location in (None, "", "Remote", "Portugal"):
            micro = parse_micro1_job({"job_id": "synthetic", "job_name": "Example", "apply_url": "https://jobs.micro1.ai/post/synthetic", "location_type": location})
            self.assertEqual(micro.location, location or None)

    def test_identity_wrong_host_error_empty_and_truncated_pages_are_rejected(self):
        for case in (CASES[0], CASES[-1]):
            for bad in (replace(response(case), status=404), replace(response(case), body=b""),
                        replace(response(case), body=response(case).body[:4000]),
                        replace(response(case), url="https://unexpected.example/job")):
                with self.subTest(rank=case["rank"], status=bad.status, size=len(bad.body)):
                    with self.assertRaises(ValueError):
                        recover_detail(case["provider"], candidate(case), bad)
            with self.assertRaises(ValueError):
                recover_detail(case["provider"], replace(candidate(case), title="Other title"), response(case))
        for url in ("http://www.alignerr.com/jobs/id", "https://www.alignerr.com.evil.test/jobs/id",
                    "https://user@www.alignerr.com/jobs/id", "https://www.alignerr.com/jobs/other"):
            with self.assertRaises(ValueError): validate_detail_url("alignerr", "id", url)

    def test_synthetic_missing_or_truncated_alignerr_long_field_cannot_use_teaser(self):
        case = CASES[0]
        for long in (None, "", "A role with requirements ..."):
            packet = {"props": {"pageProps": {"job": {"id": case["external_id"], "name": case["title"], "isActive": True,
                       "longDescription": long, "shortDescription": "teaser"}}}}
            body = ("<script id='__NEXT_DATA__'>"+json.dumps(packet)+"</script>").encode()
            with self.assertRaises(ValueError): recover_detail("alignerr", candidate(case), replace(response(case), body=body))

    def test_prepared_source_is_visible_only_when_signed_in_and_escaped_without_reparsing(self):
        case = CASES[-1]
        recovered = recover_detail("micro1", candidate(case), response(case))
        detail = recovered.source_metadata[DETAIL_KEY]
        detail["display_text"] += "\n<script>alert('source text')</script>"
        job = dict(company_slug="micro1", external_id=case["external_id"], rich_metadata_json=json.dumps(recovered.source_metadata))
        content = "<main><div class='job-description'>Original page</div></main>"
        self.assertEqual(append_authenticated_source_detail(content, job, authenticated=False), content)
        with patch("wahojobs.opportunity_enrichment.source_body_paragraphs", side_effect=AssertionError("request-time parsing")):
            rendered = append_authenticated_source_detail(content, job, authenticated=True)
        self.assertIn("Preferred Qualifications", rendered)
        self.assertIn("Compensation is output-based", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertNotIn("<script>", rendered)
        job["external_id"] = "other-variant"
        self.assertEqual(append_authenticated_source_detail(content, job, authenticated=True), content)


class ProviderDetailPersistenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.conn = get_connection(Path(temporary.name) / "test.sqlite")
        self.addCleanup(self.conn.close)
        install_base_schema(self.conn)
        case = self.case = CASES[0]
        company = self.conn.execute("INSERT INTO companies(name,slug,careers_url,source_tier,inventory_model,market_count_policy) VALUES ('Alignerr','alignerr','https://www.alignerr.com/jobs','core','live_feed','count_live')").lastrowid
        item = with_source_hash("alignerr", candidate(case))
        self.job_id = insert_job(self.conn, company, item, "2026-09-04T14:59:01+00:00")
        run = create_crawl_run(self.conn, company, "2026-09-04T14:59:01+00:00")
        result = CompanyCrawlResult(jobs=[item], used_sample_data=False, source_message="synthetic catalog context",
            source_type="alignerr-marketplace", outcome=ProviderOutcome.SUCCESS, snapshot_complete=True,
            pagination_complete=True, raw_record_count=1, normalized_record_count=1)
        upsert_job_source_content(self.conn,self.job_id,"alignerr","alignerr-marketplace",item,
            "2026-09-04T14:59:01+00:00",capture_context=SourceCaptureContext.from_crawl_result(run,result))
        self.conn.execute("UPDATE crawl_runs SET status='success',finished_at=started_at WHERE id=?",(run,))
        self.conn.commit()

    def test_reprocessing_preserves_lifecycle_and_original_snapshot_provenance(self):
        before = dict(self.conn.execute("SELECT * FROM jobs WHERE id=?",(self.job_id,)).fetchone())
        runs = [tuple(r) for r in self.conn.execute("SELECT * FROM crawl_runs")]
        with self.conn:
            result = reprocess_saved_detail(self.conn, self.job_id, response(self.case))
        self.assertTrue(result.accepted)
        after = dict(self.conn.execute("SELECT * FROM jobs WHERE id=?",(self.job_id,)).fetchone())
        for key in ("last_seen_at","first_seen_at","is_active","removed_at","source_hash"):
            self.assertEqual(before[key],after[key])
        self.assertEqual(runs,[tuple(r) for r in self.conn.execute("SELECT * FROM crawl_runs")])
        verify_job_source_acceptance_integrity(self.conn,self.job_id)
        source = self.conn.execute("SELECT * FROM job_source_contents WHERE job_id=?",(self.job_id,)).fetchone()
        detail = json.loads(source['metadata_json'])[DETAIL_KEY]
        self.assertEqual(detail['catalog_observed_at'],before['last_seen_at'])
        self.assertEqual(detail['observed_at'],self.case['observed_at'])
        self.assertIn('Who You Are',source['body'])

    def test_failed_empty_or_truncated_detail_never_replaces_complete_acceptance(self):
        with self.conn: reprocess_saved_detail(self.conn,self.job_id,response(self.case))
        before = self.conn.iterdump()
        before = '\n'.join(before)
        for bad in (replace(response(self.case),status=503),replace(response(self.case),body=b''),
                    replace(response(self.case),body=b'<html>Apply now</html>')):
            with self.assertRaises(ValueError): reprocess_saved_detail(self.conn,self.job_id,bad)
            self.assertEqual(before,'\n'.join(self.conn.iterdump()))

    def test_later_catalog_teaser_cannot_overwrite_accepted_complete_detail(self):
        with self.conn: reprocess_saved_detail(self.conn,self.job_id,response(self.case))
        before = self.conn.execute('SELECT body FROM job_source_contents WHERE job_id=?',(self.job_id,)).fetchone()[0]
        row = self.conn.execute('SELECT * FROM jobs WHERE id=?',(self.job_id,)).fetchone()
        teaser = replace(candidate(self.case),source_hash=row['source_hash'])
        context = SourceCaptureContext(None,'success',False,True,True,False,1,1,1,0,'synthetic catalog','synthetic')
        with self.conn:
            result = upsert_job_source_content(self.conn,self.job_id,'alignerr','alignerr-marketplace',
                teaser,'2026-09-05T14:00:00+00:00',capture_context=context)
        self.assertEqual(result.promotion_decision,'held_degraded')
        self.assertEqual(before,self.conn.execute('SELECT body FROM job_source_contents WHERE job_id=?',(self.job_id,)).fetchone()[0])
        verify_job_source_acceptance_integrity(self.conn,self.job_id)


if __name__ == '__main__':
    unittest.main()
