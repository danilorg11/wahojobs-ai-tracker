import unittest

from wahojobs.crawler.types import (
    LEGACY_CONTRACT_WARNING,
    CompanyCrawlResult,
    JobCandidate,
    ProviderOutcome,
    crawl_run_status_for_result,
    evaluate_removal_authorization,
)
from wahojobs.tracking.service import result_warnings


def candidate(external_id="job-1"):
    return JobCandidate(
        title="Example role",
        location="Remote",
        url=f"https://example.test/{external_id}",
        external_id=external_id,
    )


def authoritative_result(jobs=None, **overrides):
    jobs = [candidate()] if jobs is None else jobs
    values = {
        "jobs": jobs,
        "used_sample_data": False,
        "source_message": "fixture",
        "source_type": "fixture",
        "outcome": ProviderOutcome.SUCCESS,
        "snapshot_complete": True,
        "pagination_complete": True,
        "raw_record_count": len(jobs),
        "normalized_record_count": len(jobs),
    }
    values.update(overrides)
    return CompanyCrawlResult(**values)


class ProviderResultContractTests(unittest.TestCase):
    def test_legacy_positional_constructor_is_non_authoritative(self):
        result = CompanyCrawlResult([candidate()], False, "legacy", "fixture")
        authorization = evaluate_removal_authorization(result)

        self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
        self.assertFalse(result.snapshot_complete)
        self.assertFalse(result.pagination_complete)
        self.assertFalse(authorization.authorized)
        self.assertEqual(crawl_run_status_for_result(result, authorization), "partial")
        self.assertIn(LEGACY_CONTRACT_WARNING, result_warnings(result))

    def test_complete_non_sample_result_authorizes_removals(self):
        result = authoritative_result()
        authorization = evaluate_removal_authorization(result)

        self.assertTrue(authorization.authorized)
        self.assertEqual(crawl_run_status_for_result(result, authorization), "success")

    def test_partial_and_contract_drift_are_non_success_statuses(self):
        for outcome, expected_status in (
            (ProviderOutcome.PARTIAL, "partial"),
            (ProviderOutcome.CONTRACT_DRIFT, "contract_drift"),
        ):
            with self.subTest(outcome=outcome):
                result = CompanyCrawlResult(
                    [candidate()],
                    False,
                    "fixture",
                    "fixture",
                    outcome=outcome,
                    normalized_record_count=1,
                )
                authorization = evaluate_removal_authorization(result)
                self.assertFalse(authorization.authorized)
                self.assertEqual(
                    crawl_run_status_for_result(result, authorization),
                    expected_status,
                )

    def test_sample_result_preserves_success_status_but_cannot_remove(self):
        result = authoritative_result(used_sample_data=True)
        authorization = evaluate_removal_authorization(result)

        self.assertFalse(authorization.authorized)
        self.assertEqual(crawl_run_status_for_result(result, authorization), "success")

    def test_contract_drift_is_never_recorded_as_success_even_if_sampled(self):
        result = authoritative_result(
            used_sample_data=True,
            outcome=ProviderOutcome.CONTRACT_DRIFT,
        )
        authorization = evaluate_removal_authorization(result)

        self.assertFalse(authorization.authorized)
        self.assertEqual(
            crawl_run_status_for_result(result, authorization),
            "contract_drift",
        )

    def test_empty_snapshot_requires_explicit_validation(self):
        result = authoritative_result(jobs=[])
        authorization = evaluate_removal_authorization(result)
        self.assertFalse(authorization.authorized)
        self.assertEqual(crawl_run_status_for_result(result, authorization), "partial")

        validated = authoritative_result(jobs=[], empty_snapshot_validated=True)
        validated_authorization = evaluate_removal_authorization(validated)
        self.assertTrue(validated_authorization.authorized)
        self.assertEqual(
            crawl_run_status_for_result(validated, validated_authorization),
            "success",
        )

    def test_normalized_count_must_match_job_records(self):
        result = authoritative_result(jobs=[], normalized_record_count=2)
        authorization = evaluate_removal_authorization(result)

        self.assertFalse(authorization.authorized)
        self.assertTrue(
            any("does not match" in reason for reason in authorization.skip_reasons)
        )

    def test_negative_diagnostic_counts_are_invalid(self):
        with self.assertRaisesRegex(ValueError, "rejected_record_count"):
            authoritative_result(rejected_record_count=-1)


if __name__ == "__main__":
    unittest.main()
