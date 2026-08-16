import unittest
from unittest.mock import patch

from wahojobs.crawler.providers.micro1 import fetch_micro1_snapshot
from wahojobs.crawler.types import ProviderOutcome, evaluate_removal_authorization


def job(external_id, *, include_required_fields=True):
    record = {
        "job_id": external_id,
        "job_name": f"Role {external_id}",
        "apply_url": f"https://jobs.micro1.ai/post/{external_id}",
        "location_type": "Remote",
        "job_description": "Review model answers and explain technical defects.",
        "skills": ["Python", "Code review"],
    }
    if not include_required_fields:
        record.pop("apply_url")
    return record


def page(records, total):
    return {
        "status": True,
        "statusCode": 200,
        "message": "Success",
        "total": total,
        "data": records,
    }


class Micro1ProviderContractTests(unittest.TestCase):
    @patch("wahojobs.crawler.providers.micro1.fetch_page")
    def test_complete_paginated_inventory_is_authoritative(self, fetch_page):
        records = [job(f"job-{index}") for index in range(101)]
        fetch_page.side_effect = [page(records[:100], 101), page(records[100:], 101)]

        result = fetch_micro1_snapshot("https://example.test/micro1")
        authorization = evaluate_removal_authorization(result)

        self.assertEqual(result.outcome, ProviderOutcome.SUCCESS)
        self.assertTrue(result.snapshot_complete)
        self.assertTrue(result.pagination_complete)
        self.assertEqual(result.raw_record_count, 101)
        self.assertEqual(result.normalized_record_count, 101)
        self.assertEqual(result.rejected_record_count, 0)
        self.assertEqual(
            result.jobs[0].source_body,
            "Review model answers and explain technical defects.",
        )
        self.assertEqual(result.jobs[0].source_metadata["skills"], ["Python", "Code review"])
        self.assertTrue(authorization.authorized)

    @patch("wahojobs.crawler.providers.micro1.fetch_page")
    def test_truncated_pagination_remains_partial(self, fetch_page):
        records = [job(f"job-{index}") for index in range(100)]
        fetch_page.side_effect = [page(records, 101), page([], 101)]

        result = fetch_micro1_snapshot("https://example.test/micro1")

        self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
        self.assertFalse(result.snapshot_complete)
        self.assertFalse(result.pagination_complete)
        self.assertFalse(evaluate_removal_authorization(result).authorized)

    @patch("wahojobs.crawler.providers.micro1.fetch_page")
    def test_changing_total_is_contract_drift(self, fetch_page):
        records = [job(f"job-{index}") for index in range(101)]
        fetch_page.side_effect = [page(records[:100], 101), page(records[100:], 102)]

        result = fetch_micro1_snapshot("https://example.test/micro1")

        self.assertEqual(result.outcome, ProviderOutcome.CONTRACT_DRIFT)
        self.assertFalse(evaluate_removal_authorization(result).authorized)

    @patch("wahojobs.crawler.providers.micro1.fetch_page")
    def test_rejected_or_duplicate_records_are_contract_drift(self, fetch_page):
        cases = (
            [job("missing", include_required_fields=False)],
            [job("duplicate"), job("duplicate")],
        )
        for records in cases:
            with self.subTest(records=records):
                fetch_page.reset_mock()
                fetch_page.side_effect = [page(records, len(records))]

                result = fetch_micro1_snapshot("https://example.test/micro1")

                self.assertEqual(result.outcome, ProviderOutcome.CONTRACT_DRIFT)
                self.assertFalse(evaluate_removal_authorization(result).authorized)

    @patch("wahojobs.crawler.providers.micro1.fetch_page")
    def test_explicit_empty_inventory_is_authoritative(self, fetch_page):
        fetch_page.return_value = page([], 0)

        result = fetch_micro1_snapshot("https://example.test/micro1")

        self.assertEqual(result.outcome, ProviderOutcome.SUCCESS)
        self.assertTrue(result.empty_snapshot_validated)
        self.assertTrue(evaluate_removal_authorization(result).authorized)


if __name__ == "__main__":
    unittest.main()
