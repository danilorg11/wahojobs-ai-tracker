import copy
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import scripts.profile_match_digest as matcher
import wahojobs.tracking.service as tracking
from wahojobs.crawler.companies.mercor import crawl_mercor
from wahojobs.crawler.providers import mercor
from wahojobs.crawler.types import ProviderOutcome, evaluate_removal_authorization
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import (
    create_crawl_run, finish_crawl_run, get_job_source_capture_evidence,
    install_base_schema, verify_job_source_acceptance_integrity,
)
from wahojobs.matching.opportunity_trust import assess_opportunity_trust


OLD = "2026-09-01T12:00:00+00:00"
NOW = "2026-09-04T12:00:01+00:00"
FIXTURE = Path(__file__).parent / "fixtures" / "mercor_observation_contract.json"


class MercorObservationSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.listing = json.loads(FIXTURE.read_text(encoding="utf-8"))["listing"]
        self.temporary = tempfile.TemporaryDirectory()
        self.conn = get_connection(Path(self.temporary.name) / "mercor.sqlite3")
        install_base_schema(self.conn)
        self.company_id = self.conn.execute(
            """INSERT INTO companies (name, slug, careers_url, source_tier,
               inventory_model, market_count_policy)
               VALUES ('Mercor', 'mercor', ?, 'core', 'live_feed', 'count_live')""",
            (mercor.MERCOR_ENDPOINT,),
        ).lastrowid
        self.network_guard = patch.object(mercor, "urlopen", side_effect=AssertionError("network forbidden"))
        self.network_guard.start()
        self.model_guard = patch.object(tracking, "tracking_openai_client", return_value=None)
        self.model_guard.start()

    def tearDown(self):
        self.model_guard.stop()
        self.network_guard.stop()
        self.conn.close()
        self.temporary.cleanup()

    def listing_for(self, identity, **changes):
        return dict(copy.deepcopy(self.listing), listingId=identity, **changes)

    def observe(self, listings, at=NOW, **result_changes):
        result = replace(mercor.parse_mercor_observations({"listings": listings}), **result_changes)
        run_id = create_crawl_run(self.conn, self.company_id, at)
        summary = tracking.track_crawl_result(self.conn, self.company_id, run_id, result, at)
        finish_crawl_run(self.conn, run_id, summary, at, status="partial", error_message="partial fixture")
        self.conn.commit()
        return summary, run_id

    def rows(self):
        return {row["external_id"]: dict(row) for row in self.conn.execute("SELECT * FROM jobs")}

    def trust(self, identity, at=NOW):
        job_id = self.rows()[identity]["id"]
        row = next(row for row in matcher.get_active_rows(self.conn) if row["job_id"] == job_id)
        return assess_opportunity_trust(dict(row), "compatible", now=datetime.fromisoformat(at))

    def test_partial_response_advances_only_observed_record_and_remains_partial(self):
        self.observe([self.listing_for("observed"), self.listing_for("absent")], OLD)
        before = self.rows()["absent"]
        summary, run_id = self.observe([self.listing_for("observed")])
        self.assertFalse(summary.removals_authorized)
        self.assertFalse(summary.snapshot_complete)
        self.assertFalse(summary.pagination_complete)
        self.assertEqual(summary.provider_outcome, ProviderOutcome.PARTIAL)
        self.assertEqual(summary.jobs_removed, 0)
        self.assertEqual(self.rows()["absent"], before)
        self.assertEqual(self.rows()["observed"]["last_seen_at"], NOW)
        observed = self.trust("observed")
        self.assertEqual(observed.status, "trusted")
        self.assertEqual(observed.source_run_id, run_id)
        self.assertEqual(observed.latest_successful_source_run_at, NOW)
        self.assertEqual(self.trust("absent").status, "stale_source")
        self.assertEqual(self.trust("observed", "2026-09-07T12:00:02+00:00").status, "stale_source")
        capture = get_job_source_capture_evidence(self.conn, self.rows()["observed"]["id"])
        self.assertEqual(capture["state"], "accepted_current")
        verify_job_source_acceptance_integrity(self.conn, self.rows()["observed"]["id"])

    def test_inactive_deleted_private_and_missing_status_are_not_positive_observations(self):
        identities = ["inactive", "deleted", "private", "missing"]
        self.observe([self.listing_for(identity) for identity in identities], OLD)
        before = self.rows()
        missing = self.listing_for("missing")
        missing.pop("status")
        summary, _ = self.observe([
            self.listing_for("inactive", status="inactive"),
            self.listing_for("deleted", deletedAt=NOW),
            self.listing_for("private", isPrivate=True), missing,
        ])
        self.assertEqual(summary.jobs_found, 0)
        self.assertEqual(summary.rejected_record_count, 4)
        self.assertFalse(summary.removals_authorized)
        self.assertEqual(self.rows(), before)
        self.assertTrue(all(self.trust(identity).status == "stale_source" for identity in identities))

    def test_duplicates_invalid_shapes_and_missing_deleted_field_fail_closed(self):
        absent_deleted = self.listing_for("missing-deleted")
        absent_deleted.pop("deletedAt")
        result = mercor.parse_mercor_observations({"listings": [
            self.listing_for("duplicate"), self.listing_for("duplicate", status="inactive"),
            absent_deleted, None, self.listing_for("bad/id"), self.listing_for("valid"),
        ]})
        self.assertEqual([job.external_id for job in result.jobs], ["valid"])
        self.assertEqual((result.raw_record_count, result.normalized_record_count, result.rejected_record_count), (6, 1, 5))
        self.assertFalse(evaluate_removal_authorization(result).authorized)

    def test_empty_or_claimed_complete_response_never_authorizes_removals(self):
        for payload in ({"listings": []}, {"listings": [self.listing], "total": 1, "hasMore": False}):
            result = mercor.parse_mercor_observations(payload)
            self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
            self.assertFalse(result.snapshot_complete)
            self.assertFalse(result.pagination_complete)
            self.assertFalse(evaluate_removal_authorization(result).authorized)

    def test_failed_retrieval_or_malformed_payload_does_not_mutate_records(self):
        self.observe([self.listing_for("observed")], OLD)
        before = self.rows()
        with patch.object(mercor, "urlopen", side_effect=OSError("fixture retrieval failed")):
            with self.assertRaisesRegex(OSError, "fixture retrieval failed"):
                crawl_mercor(mercor.MERCOR_ENDPOINT)
        for payload in ({}, {"listings": None}, []):
            with self.assertRaises(ValueError):
                mercor.parse_mercor_observations(payload)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.trust("observed").status, "stale_source")

    def test_fetch_wrapper_attests_only_the_expected_response_endpoint(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = mercor.MERCOR_ENDPOINT
        response.headers.get_content_charset.return_value = "utf-8"
        response.read.return_value = json.dumps({"listings": [self.listing]}).encode()
        with patch.object(mercor, "urlopen", return_value=response) as fetch:
            result = crawl_mercor(mercor.MERCOR_ENDPOINT)
            self.assertEqual(result.normalized_record_count, 1)
            self.assertFalse(result.snapshot_complete)
            fetch.assert_called_once()
            response.geturl.return_value = "https://example.test/login"
            with self.assertRaisesRegex(ValueError, "public endpoint"):
                crawl_mercor(mercor.MERCOR_ENDPOINT)
        with self.assertRaisesRegex(ValueError, "public endpoint"):
            crawl_mercor("https://example.test/listings")

    def test_failed_or_sample_run_cannot_supply_a_new_observation_clock(self):
        self.observe([self.listing_for("observed")], OLD)
        _, run_id = self.observe([self.listing_for("observed")])
        self.conn.execute("UPDATE crawl_runs SET status = 'failed' WHERE id = ?", (run_id,))
        self.assertEqual(self.trust("observed").status, "stale_source")
        self.conn.execute("UPDATE crawl_runs SET status = 'partial', used_sample_data = 1 WHERE id = ?", (run_id,))
        self.assertEqual(self.trust("observed").status, "stale_source")

    def test_inconsistent_attestation_is_rejected_before_record_promotion(self):
        self.observe([self.listing_for("observed")], OLD)
        before = self.rows()
        result = mercor.parse_mercor_observations({"listings": [self.listing_for("observed")]})
        job = result.jobs[0]
        attestation = job.record_promotion_attestation
        job = replace(job, record_promotion_attestation=replace(
            attestation, authority_evidence=dict(attestation.authority_evidence, is_private=True),
        ))
        result = replace(result, jobs=[job])
        run_id = create_crawl_run(self.conn, self.company_id, NOW)
        with self.assertRaisesRegex(ValueError, "evidence is inconsistent"):
            tracking.track_crawl_result(self.conn, self.company_id, run_id, result, NOW)
        self.assertEqual(self.rows(), before)

    def test_sample_and_degraded_content_do_not_renew_availability(self):
        self.observe([self.listing_for("observed")], OLD)
        self.observe([self.listing_for("observed")], used_sample_data=True)
        self.assertEqual(self.trust("observed").status, "stale_source")
        self.observe([self.listing_for("observed", description="Access denied")])
        self.assertEqual(self.trust("observed").status, "stale_source")

    def test_closed_record_only_reactivates_with_valid_positive_evidence(self):
        self.observe([self.listing_for("observed")], OLD)
        self.conn.execute("UPDATE jobs SET is_active = 0, removed_at = ?", (OLD,))
        self.conn.commit()
        summary, _ = self.observe([self.listing_for("observed", isPrivate=True)])
        self.assertEqual(summary.jobs_reactivated, 0)
        self.assertEqual(self.rows()["observed"]["is_active"], 0)
        summary, _ = self.observe([self.listing_for("observed")])
        self.assertEqual(summary.jobs_reactivated, 1)
        self.assertEqual(self.trust("observed").status, "trusted")


if __name__ == "__main__":
    unittest.main()
