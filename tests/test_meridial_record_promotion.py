import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wahojobs.crawler.companies.meridial import crawl_meridial
from wahojobs.crawler import greenhouse_pilot
from wahojobs.crawler.providers import greenhouse
from wahojobs.crawler.types import (
    BODY_OBSERVATION_EXPLICITLY_EMPTY,
    BODY_OBSERVATION_NOT_OBSERVED,
    BODY_OBSERVATION_PRESENT,
    MERIDIAL_GREENHOUSE_RECORD_CONTRACT_ID,
    CompanyCrawlResult,
    JobCandidate,
    ProviderOutcome,
    crawl_run_status_for_result,
    evaluate_removal_authorization,
)
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import (
    create_crawl_run,
    insert_job,
    install_base_schema,
    upsert_job_source_content,
    verify_job_source_acceptance_integrity,
)
from wahojobs.source_capture import (
    MERIDIAL_GREENHOUSE_ENDPOINT,
    PROMOTION_DECISION_CONFIRMED,
    PROMOTION_DECISION_HELD_DEGRADED,
    PROMOTION_DECISION_HELD_SOURCE_CONFLICT,
    PROMOTION_DECISION_PROMOTED,
    REASON_BODY_NOT_OBSERVED_CANNOT_REPLACE_BODY,
    REASON_EXPLICIT_EMPTY_CANNOT_REPLACE_BODY,
    REASON_SOURCE_TIMESTAMP_CONFLICT,
    REASON_SOURCE_TIMESTAMP_INVALID,
    REASON_SOURCE_TIMESTAMP_REGRESSED,
    SEMANTIC_AUTHORITY_PENDING,
    SOURCE_PROMOTION_POLICY_VERSION,
    SourceCaptureContext,
)
from wahojobs.tracking.normalize import with_source_hash


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "greenhouse_provider_contract.json"
)
CONFIGURED_URL = "https://www.meridial.ai/projects"
FIRST_OBSERVED_AT = "2026-08-30T12:00:00+00:00"


def fixture_fetcher(payloads):
    remaining = copy.deepcopy(payloads)

    def fetch(_url):
        if not remaining:
            raise AssertionError("Unexpected extra Greenhouse request")
        return remaining.pop(0)

    return fetch


class MeridialRecordPromotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "meridial-promotion.sqlite"
        self.conn = get_connection(self.database)
        install_base_schema(self.conn)
        self.company_id = self.conn.execute(
            """
            INSERT INTO companies (
              name, slug, careers_url,
              source_tier, inventory_model, market_count_policy
            ) VALUES (
              'Meridial', 'meridial', ?, 'core', 'live_feed', 'count_live'
            )
            """,
            (CONFIGURED_URL,),
        ).lastrowid

    def tearDown(self):
        self.conn.close()
        self.temporary.cleanup()

    def result(self, jobs_payload=None, tree_payload=None):
        jobs_payload = jobs_payload or self.healthy_jobs_payload()
        tree_payload = tree_payload or self.fixtures["valid_department_hierarchy"]
        with patch.object(
            greenhouse,
            "request_json",
            side_effect=fixture_fetcher([jobs_payload, tree_payload]),
        ):
            return crawl_meridial(CONFIGURED_URL)

    def healthy_jobs_payload(self, *, content="<p>Original body.</p>", updated_at=None):
        payload = copy.deepcopy(self.fixtures["valid_jobs_inventory"])
        payload["jobs"][0]["content"] = content
        if updated_at is not None:
            payload["jobs"][0]["updated_at"] = updated_at
        return payload

    def observe(self, crawl_result, *, index=0, job_id=None, observed_at=FIRST_OBSERVED_AT):
        candidate = with_source_hash("meridial", crawl_result.jobs[index])
        if job_id is None:
            job_id = insert_job(
                self.conn,
                self.company_id,
                candidate,
                observed_at,
                semantic_authority_state=SEMANTIC_AUTHORITY_PENDING,
            )
        crawl_run_id = create_crawl_run(self.conn, self.company_id, observed_at)
        persisted = upsert_job_source_content(
            self.conn,
            job_id,
            "meridial",
            crawl_result.source_type,
            candidate,
            observed_at,
            capture_context=SourceCaptureContext.from_crawl_result(
                crawl_run_id,
                crawl_result,
            ),
        )
        run_status = crawl_run_status_for_result(
            crawl_result,
            evaluate_removal_authorization(crawl_result),
        )
        self.conn.execute(
            """
            UPDATE crawl_runs
            SET status = ?, used_sample_data = ?, finished_at = ?
            WHERE id = ?
            """,
            (
                run_status,
                int(crawl_result.used_sample_data),
                observed_at,
                crawl_run_id,
            ),
        )
        capture = self.conn.execute(
            "SELECT * FROM job_source_content_captures WHERE id = ?",
            (persisted.capture_id,),
        ).fetchone()
        accepted = self.conn.execute(
            "SELECT * FROM job_source_contents WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        return job_id, capture, accepted

    def test_healthy_record_reconfirms_and_only_newer_changed_material_promotes(self):
        initial = self.result()
        job_id, first, accepted = self.observe(initial)
        self.assertEqual(first["promotion_decision"], PROMOTION_DECISION_PROMOTED)
        self.assertEqual(first["body_observation"], BODY_OBSERVATION_PRESENT)
        self.assertEqual(
            first["record_promotion_contract_id"],
            MERIDIAL_GREENHOUSE_RECORD_CONTRACT_ID,
        )
        evidence = json.loads(first["authority_evidence_json"])
        self.assertEqual(evidence["authoritative_endpoint"], MERIDIAL_GREENHOUSE_ENDPOINT)
        self.assertEqual(evidence["greenhouse_job_id"], 1001)
        self.assertEqual(accepted["body"], "<p>Original body.</p>")

        _, identical, _accepted = self.observe(
            initial,
            job_id=job_id,
            observed_at="2026-08-30T13:00:00+00:00",
        )
        self.assertEqual(identical["promotion_decision"], PROMOTION_DECISION_CONFIRMED)

        newer_payload = self.healthy_jobs_payload(
            content="<p>Newer authoritative body.</p>",
            updated_at="2026-07-02T12:00:00-04:00",
        )
        _, newer, accepted = self.observe(
            self.result(newer_payload),
            job_id=job_id,
            observed_at="2026-08-30T14:00:00+00:00",
        )
        self.assertEqual(newer["promotion_decision"], PROMOTION_DECISION_PROMOTED)
        self.assertEqual(accepted["body"], "<p>Newer authoritative body.</p>")

        invalid_payload = self.healthy_jobs_payload(
            content="<p>Unparseable timestamp body.</p>",
            updated_at="not-a-timestamp",
        )
        _, invalid, accepted = self.observe(
            self.result(invalid_payload),
            job_id=job_id,
            observed_at="2026-08-30T14:30:00+00:00",
        )
        self.assertEqual(
            invalid["promotion_decision"],
            PROMOTION_DECISION_HELD_SOURCE_CONFLICT,
        )
        self.assertEqual(
            json.loads(invalid["decision_reasons_json"]),
            [REASON_SOURCE_TIMESTAMP_INVALID],
        )
        self.assertEqual(accepted["body"], "<p>Newer authoritative body.</p>")

        regressed_payload = self.healthy_jobs_payload(
            content="<p>Regressed body.</p>",
            updated_at="2026-07-01T12:00:00-04:00",
        )
        _, regressed, accepted = self.observe(
            self.result(regressed_payload),
            job_id=job_id,
            observed_at="2026-08-30T15:00:00+00:00",
        )
        self.assertEqual(
            regressed["promotion_decision"],
            PROMOTION_DECISION_HELD_SOURCE_CONFLICT,
        )
        self.assertEqual(
            json.loads(regressed["decision_reasons_json"]),
            [REASON_SOURCE_TIMESTAMP_REGRESSED],
        )
        self.assertEqual(accepted["body"], "<p>Newer authoritative body.</p>")

        equal_payload = self.healthy_jobs_payload(
            content="<p>Conflicting body.</p>",
            updated_at="2026-07-02T12:00:00-04:00",
        )
        _, conflict, accepted = self.observe(
            self.result(equal_payload),
            job_id=job_id,
            observed_at="2026-08-30T16:00:00+00:00",
        )
        self.assertEqual(
            conflict["promotion_decision"],
            PROMOTION_DECISION_HELD_SOURCE_CONFLICT,
        )
        self.assertEqual(
            json.loads(conflict["decision_reasons_json"]),
            [REASON_SOURCE_TIMESTAMP_CONFLICT],
        )
        self.assertEqual(accepted["body"], "<p>Newer authoritative body.</p>")

    def test_omitted_and_explicit_empty_content_are_distinct_and_preserve_lkg_body(self):
        job_id, _capture, _accepted = self.observe(self.result())

        omitted_payload = self.healthy_jobs_payload(
            updated_at="2026-07-02T12:00:00-04:00"
        )
        del omitted_payload["jobs"][0]["content"]
        omitted_result = self.result(omitted_payload)
        self.assertEqual(
            omitted_result.jobs[0].record_promotion_attestation.body_observation,
            BODY_OBSERVATION_NOT_OBSERVED,
        )
        _, omitted, accepted = self.observe(
            omitted_result,
            job_id=job_id,
            observed_at="2026-08-30T13:00:00+00:00",
        )
        self.assertEqual(omitted["body_observation"], BODY_OBSERVATION_NOT_OBSERVED)
        self.assertEqual(omitted["promotion_decision"], PROMOTION_DECISION_HELD_DEGRADED)
        self.assertEqual(
            json.loads(omitted["decision_reasons_json"]),
            [REASON_BODY_NOT_OBSERVED_CANNOT_REPLACE_BODY],
        )

        empty_payload = self.healthy_jobs_payload(
            content=None,
            updated_at="2026-07-03T12:00:00-04:00",
        )
        empty_result = self.result(empty_payload)
        self.assertEqual(
            empty_result.jobs[0].record_promotion_attestation.body_observation,
            BODY_OBSERVATION_EXPLICITLY_EMPTY,
        )
        _, empty, accepted = self.observe(
            empty_result,
            job_id=job_id,
            observed_at="2026-08-30T14:00:00+00:00",
        )
        self.assertEqual(empty["body_observation"], BODY_OBSERVATION_EXPLICITLY_EMPTY)
        self.assertEqual(empty["promotion_decision"], PROMOTION_DECISION_HELD_DEGRADED)
        self.assertEqual(
            json.loads(empty["decision_reasons_json"]),
            [REASON_EXPLICIT_EMPTY_CANNOT_REPLACE_BODY],
        )
        self.assertEqual(accepted["body"], "<p>Original body.</p>")

    def test_partial_tree_failure_and_count_drop_records_promote_without_removal_authority(self):
        partial_payload = self.healthy_jobs_payload()
        partial_payload["jobs"].append({"id": 9999})
        partial_payload["meta"]["total"] = len(partial_payload["jobs"])
        partial_jobs = self.result(partial_payload)
        self.assertEqual(partial_jobs.outcome, ProviderOutcome.PARTIAL)
        self.assertGreater(partial_jobs.rejected_record_count, 0)
        self.assertFalse(evaluate_removal_authorization(partial_jobs).authorized)
        _job_id, partial_capture, _accepted = self.observe(partial_jobs)
        self.assertEqual(
            partial_capture["promotion_decision"],
            PROMOTION_DECISION_PROMOTED,
        )

        tree_jobs = self.healthy_jobs_payload()
        tree_jobs["jobs"][1]["content"] = "<p>Body survives tree failure.</p>"
        tree_failure = self.result(
            tree_jobs,
            self.fixtures["invalid_department_hierarchy"],
        )
        self.assertEqual(tree_failure.outcome, ProviderOutcome.PARTIAL)
        self.assertFalse(evaluate_removal_authorization(tree_failure).authorized)
        _job_id, tree_capture, tree_accepted = self.observe(tree_failure, index=1)
        self.assertEqual(tree_capture["promotion_decision"], PROMOTION_DECISION_PROMOTED)
        self.assertEqual(tree_accepted["body"], "<p>Body survives tree failure.</p>")

        count_jobs = self.healthy_jobs_payload()
        count_jobs["jobs"][2]["content"] = "<p>Body survives count anomaly.</p>"
        complete = self.result(count_jobs)
        entry = SimpleNamespace(
            last_accepted_complete_count=10,
            count_drop_policy=SimpleNamespace(
                minimum_previous_count=2,
                minimum_retained_fraction=0.8,
            ),
        )
        anomalous = greenhouse_pilot.apply_count_drop_policy(entry, complete)
        self.assertEqual(anomalous.outcome, ProviderOutcome.ANOMALOUS)
        self.assertFalse(evaluate_removal_authorization(anomalous).authorized)
        _job_id, anomalous_capture, anomalous_accepted = self.observe(
            anomalous,
            index=2,
        )
        self.assertEqual(
            anomalous_capture["promotion_decision"],
            PROMOTION_DECISION_PROMOTED,
        )
        self.assertEqual(
            anomalous_accepted["body"],
            "<p>Body survives count anomaly.</p>",
        )

    def test_empty_inventory_cannot_promote_or_authorize_removals(self):
        empty = self.result(
            self.fixtures["empty_jobs_inventory"],
            self.fixtures["empty_department_hierarchy"],
        )
        self.assertEqual(empty.jobs, [])
        self.assertEqual(empty.outcome, ProviderOutcome.PARTIAL)
        self.assertFalse(evaluate_removal_authorization(empty).authorized)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM job_source_content_captures"
            ).fetchone()[0],
            0,
        )

    def test_attestation_replays_without_provider_code_and_tampering_fails_closed(self):
        job_id, _capture, _accepted = self.observe(self.result())
        with patch.object(
            greenhouse,
            "greenhouse_schema_fingerprint",
            side_effect=AssertionError("provider code must not run during replay"),
        ):
            verified, _semantic_hash = verify_job_source_acceptance_integrity(
                self.conn,
                job_id,
            )
        self.assertIsNotNone(verified)

        accepted_capture_id = verified["id"]
        self.conn.execute(
            """
            UPDATE job_source_content_captures
            SET authority_evidence_json = '{}'
            WHERE id = ?
            """,
            (accepted_capture_id,),
        )
        with self.assertRaisesRegex(RuntimeError, "provenance is inconsistent"):
            verify_job_source_acceptance_integrity(self.conn, job_id)

    def test_attested_capture_is_atomic_and_generic_v1_history_still_replays(self):
        result = self.result()
        candidate = with_source_hash("meridial", result.jobs[0])
        job_id = insert_job(
            self.conn,
            self.company_id,
            candidate,
            FIRST_OBSERVED_AT,
            semantic_authority_state=SEMANTIC_AUTHORITY_PENDING,
        )
        crawl_run_id = create_crawl_run(
            self.conn,
            self.company_id,
            FIRST_OBSERVED_AT,
        )
        context = SourceCaptureContext.from_crawl_result(crawl_run_id, result)
        with patch(
            "wahojobs.db.repository._promote_job_semantic_material",
            side_effect=RuntimeError("injected promotion failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected promotion failure"):
                upsert_job_source_content(
                    self.conn,
                    job_id,
                    "meridial",
                    result.source_type,
                    candidate,
                    FIRST_OBSERVED_AT,
                    capture_context=context,
                )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM job_source_content_captures WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT * FROM job_source_contents WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        )

        generic = with_source_hash(
            "meridial",
            JobCandidate(
                external_id="legacy-generic",
                title="Legacy Generic Record",
                location="Remote",
                url="https://example.test/legacy-generic",
                source_body="Legacy generic body",
                source_body_format="text/plain",
                source_metadata={"legacy": True},
            ),
        )
        generic_job_id = insert_job(
            self.conn,
            self.company_id,
            generic,
            "2026-08-30T13:00:00+00:00",
            semantic_authority_state=SEMANTIC_AUTHORITY_PENDING,
        )
        generic_result = CompanyCrawlResult(
            jobs=[generic],
            used_sample_data=False,
            source_message="legacy fixture",
            source_type="legacy-fixture",
            outcome=ProviderOutcome.SUCCESS,
            snapshot_complete=True,
            pagination_complete=True,
            raw_record_count=1,
            normalized_record_count=1,
        )
        generic_run_id = create_crawl_run(
            self.conn,
            self.company_id,
            "2026-08-30T13:00:00+00:00",
        )
        with patch(
            "wahojobs.db.repository.SOURCE_PROMOTION_POLICY_VERSION",
            "job_source_promotion_v1",
        ):
            persisted = upsert_job_source_content(
                self.conn,
                generic_job_id,
                "meridial",
                generic_result.source_type,
                generic,
                "2026-08-30T13:00:00+00:00",
                capture_context=SourceCaptureContext.from_crawl_result(
                    generic_run_id,
                    generic_result,
                ),
            )
        self.conn.execute(
            """
            UPDATE crawl_runs
            SET status = 'success', finished_at = '2026-08-30T13:00:00+00:00'
            WHERE id = ?
            """,
            (generic_run_id,),
        )
        legacy_capture = self.conn.execute(
            "SELECT * FROM job_source_content_captures WHERE id = ?",
            (persisted.capture_id,),
        ).fetchone()
        self.assertEqual(legacy_capture["promotion_policy_version"], "job_source_promotion_v1")
        self.assertEqual(legacy_capture["record_promotion_contract_id"], "")
        self.assertEqual(legacy_capture["authority_evidence_json"], "{}")
        verified, _semantic_hash = verify_job_source_acceptance_integrity(
            self.conn,
            generic_job_id,
        )
        self.assertEqual(verified["id"], persisted.capture_id)
        self.assertEqual(SOURCE_PROMOTION_POLICY_VERSION, "job_source_promotion_v2")


if __name__ == "__main__":
    unittest.main()
