import copy
import json
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from wahojobs.crawler.providers import alignerr
from wahojobs.crawler.types import ProviderOutcome, evaluate_removal_authorization


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "alignerr_provider_contract.json"
API_URL = "https://www.alignerr.com/api/jobs"


class FakeHeaders:
    @staticmethod
    def get_content_charset():
        return "utf-8"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload.encode("utf-8")
        self.headers = FakeHeaders()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


def fixture_fetcher(payloads):
    payloads = copy.deepcopy(payloads)
    calls = []

    def fetch(url):
        calls.append(url)
        if not payloads:
            raise AssertionError(f"Unexpected extra Alignerr request: {url}")
        return payloads.pop(0)

    fetch.calls = calls
    return fetch


class AlignerrProviderContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def fetch_fixture(self, name):
        payload = self.fixtures[name]
        paginated_cases = {
            "changing_total",
            "duplicate_page",
            "early_empty_page",
            "final_count_mismatch",
            "no_progress",
            "offset_not_advancing",
            "v2_multiple_pages",
        }
        fetcher = fixture_fetcher(payload if name in paginated_cases else [payload])
        with patch.object(alignerr, "request_json", side_effect=fetcher):
            result = alignerr.fetch_alignerr_snapshot(API_URL)
        return result, fetcher.calls

    def test_valid_legacy_v1_is_complete_and_preserves_mapping(self):
        result, calls = self.fetch_fixture("legacy_v1_valid")

        self.assertEqual(result.outcome, ProviderOutcome.SUCCESS)
        self.assertTrue(result.snapshot_complete)
        self.assertTrue(result.pagination_complete)
        self.assertEqual(result.raw_record_count, 3)
        self.assertEqual(result.normalized_record_count, 2)
        self.assertEqual(result.rejected_record_count, 0)
        self.assertEqual([job.external_id for job in result.jobs], ["job-a", "job-b"])
        self.assertEqual(result.jobs[0].title, "Python Expert")
        self.assertEqual(result.jobs[0].department, "CODING")
        self.assertEqual(result.jobs[0].commitment, "CONTRACT")
        self.assertEqual(result.jobs[1].url, "https://www.alignerr.com/jobs/job-b")
        self.assertEqual(len(calls), 1)
        self.assertTrue(evaluate_removal_authorization(result).authorized)

    def test_valid_v2_single_page_maps_current_fields_and_application_url(self):
        result, calls = self.fetch_fixture("v2_single_page")

        self.assertEqual(result.outcome, ProviderOutcome.SUCCESS)
        self.assertTrue(result.snapshot_complete)
        self.assertTrue(result.pagination_complete)
        self.assertEqual(result.raw_record_count, 2)
        self.assertEqual(result.normalized_record_count, 2)
        self.assertEqual(result.jobs[0].external_id, "job-a")
        self.assertEqual(result.jobs[0].title, "Python Expert")
        self.assertEqual(result.jobs[0].location, "United States")
        self.assertEqual(result.jobs[0].department, "Coding")
        self.assertEqual(result.jobs[0].expertise, "Coding")
        self.assertIsNone(result.jobs[0].commitment)
        self.assertEqual(result.jobs[0].url, "https://www.alignerr.com/jobs/job-a")
        self.assertEqual(result.jobs[0].source_body, "Evaluate Python responses.")
        self.assertEqual(result.jobs[0].source_body_format, "text/plain")
        self.assertEqual(result.jobs[0].source_metadata["pay"], "$30-50/hr")
        self.assertIn("limit=120", calls[0])
        self.assertIn("offset=0", calls[0])
        self.assertTrue(evaluate_removal_authorization(result).authorized)

    def test_valid_v2_multiple_pages_fetches_every_offset_in_order(self):
        result, calls = self.fetch_fixture("v2_multiple_pages")

        self.assertEqual(result.outcome, ProviderOutcome.SUCCESS)
        self.assertEqual([job.external_id for job in result.jobs], ["job-a", "job-b", "job-c"])
        self.assertEqual(result.raw_record_count, 3)
        self.assertEqual(result.normalized_record_count, 3)
        self.assertEqual(len(calls), 2)
        self.assertIn("limit=2", calls[1])
        self.assertIn("offset=2", calls[1])

    def test_schema_fingerprint_is_stable_and_versioned(self):
        single, _ = self.fetch_fixture("v2_single_page")
        multiple, _ = self.fetch_fixture("v2_multiple_pages")
        legacy, _ = self.fetch_fixture("legacy_v1_valid")

        self.assertEqual(single.schema_fingerprint, multiple.schema_fingerprint)
        self.assertNotEqual(single.schema_fingerprint, legacy.schema_fingerprint)
        self.assertTrue(single.schema_fingerprint.startswith("alignerr-v2:sha256:"))

    def test_unknown_or_application_error_envelopes_are_contract_drift(self):
        for name in ("unknown_envelope", "application_error"):
            with self.subTest(name=name):
                result, _ = self.fetch_fixture(name)
                self.assertEqual(result.outcome, ProviderOutcome.CONTRACT_DRIFT)
                self.assertEqual(result.jobs, [])
                self.assertFalse(result.snapshot_complete)
                self.assertFalse(evaluate_removal_authorization(result).authorized)

    def test_missing_jobs_and_wrong_jobs_type_are_contract_drift(self):
        for name in ("missing_jobs", "jobs_wrong_type"):
            with self.subTest(name=name):
                result, _ = self.fetch_fixture(name)
                self.assertEqual(result.outcome, ProviderOutcome.CONTRACT_DRIFT)
                self.assertIn("jobs", result.source_message)

    def test_missing_invalid_or_negative_pagination_fields_are_contract_drift(self):
        for name in (
            "pagination_missing_limit",
            "pagination_wrong_total",
            "pagination_negative_offset",
        ):
            with self.subTest(name=name):
                result, _ = self.fetch_fixture(name)
                self.assertEqual(result.outcome, ProviderOutcome.CONTRACT_DRIFT)
                self.assertFalse(evaluate_removal_authorization(result).authorized)

    def test_invalid_pagination_ranges_are_contract_drift(self):
        base = self.fixtures["zero_result"]
        variants = (
            {**base, "limit": 0},
            {**base, "limit": alignerr.MAX_PAGE_SIZE + 1},
            {**base, "total": -1},
            {**base, "offset": True},
        )
        for payload in variants:
            with self.subTest(payload=payload):
                fetcher = fixture_fetcher([payload])
                with patch.object(alignerr, "request_json", side_effect=fetcher):
                    result = alignerr.fetch_alignerr_snapshot(API_URL)
                self.assertEqual(result.outcome, ProviderOutcome.CONTRACT_DRIFT)

    def test_malformed_records_make_known_snapshot_partial(self):
        for name in (
            "record_missing_title",
            "record_invalid_title_type",
            "record_invalid_apply_url",
        ):
            with self.subTest(name=name):
                result, _ = self.fetch_fixture(name)
                self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
                self.assertEqual(result.rejected_record_count, 1)
                self.assertFalse(result.snapshot_complete)
                self.assertFalse(result.pagination_complete)
                self.assertFalse(evaluate_removal_authorization(result).authorized)

    def test_duplicate_page_is_partial(self):
        result, _ = self.fetch_fixture("duplicate_page")
        self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
        self.assertTrue(any("Duplicate page" in warning for warning in result.warnings))

    def test_duplicate_ids_are_rejected_and_partial(self):
        result, _ = self.fetch_fixture("duplicate_ids")
        self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
        self.assertEqual(result.rejected_record_count, 1)
        self.assertTrue(any("Duplicate job id" in warning for warning in result.warnings))

    def test_no_progress_page_is_partial(self):
        result, _ = self.fetch_fixture("no_progress")
        self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
        self.assertTrue(any("no unique-record progress" in warning for warning in result.warnings))

    def test_early_empty_and_premature_short_pages_are_partial(self):
        expectations = {
            "early_empty_page": "Unexpected empty page",
            "premature_short_page": "Premature short page",
            "final_count_mismatch": "Premature short page",
        }
        for name, expected_warning in expectations.items():
            with self.subTest(name=name):
                result, _ = self.fetch_fixture(name)
                self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
                self.assertTrue(
                    any(expected_warning in warning for warning in result.warnings)
                )

    def test_changing_total_and_nonadvancing_offset_are_partial(self):
        expectations = {
            "changing_total": "Declared total changed",
            "offset_not_advancing": "Requested offset",
        }
        for name, expected_warning in expectations.items():
            with self.subTest(name=name):
                result, _ = self.fetch_fixture(name)
                self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
                self.assertTrue(
                    any(expected_warning in warning for warning in result.warnings)
                )

    def test_later_page_contract_change_is_contract_drift(self):
        first = self.fixtures["v2_multiple_pages"][0]
        second = self.fixtures["missing_jobs"]
        fetcher = fixture_fetcher([first, second])
        with patch.object(alignerr, "request_json", side_effect=fetcher):
            result = alignerr.fetch_alignerr_snapshot(API_URL)

        self.assertEqual(result.outcome, ProviderOutcome.CONTRACT_DRIFT)
        self.assertEqual(result.jobs, [])

    def test_zero_result_is_complete_but_non_authoritative(self):
        result, _ = self.fetch_fixture("zero_result")

        self.assertEqual(result.outcome, ProviderOutcome.SUCCESS)
        self.assertTrue(result.snapshot_complete)
        self.assertTrue(result.pagination_complete)
        self.assertFalse(result.empty_snapshot_validated)
        self.assertFalse(evaluate_removal_authorization(result).authorized)

    def test_total_smaller_than_unique_records_is_partial(self):
        payload = copy.deepcopy(self.fixtures["v2_single_page"])
        payload["total"] = 1
        fetcher = fixture_fetcher([payload])
        with patch.object(alignerr, "request_json", side_effect=fetcher):
            result = alignerr.fetch_alignerr_snapshot(API_URL)

        self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
        self.assertTrue(any("exceeding declared total" in item for item in result.warnings))

    def test_additive_fields_are_diagnostic_without_causing_drift(self):
        payload = copy.deepcopy(self.fixtures["v2_single_page"])
        payload["requestId"] = "fixture-request"
        payload["jobs"][0]["newOptionalField"] = "value"
        fetcher = fixture_fetcher([payload])
        with patch.object(alignerr, "request_json", side_effect=fetcher):
            result = alignerr.fetch_alignerr_snapshot(API_URL)

        self.assertEqual(result.outcome, ProviderOutcome.SUCCESS)
        self.assertTrue(evaluate_removal_authorization(result).authorized)
        self.assertTrue(any("envelope field" in item for item in result.warnings))
        self.assertTrue(any("record field" in item for item in result.warnings))

    def test_safety_sensitive_additive_fields_do_not_receive_authority(self):
        envelope = copy.deepcopy(self.fixtures["v2_single_page"])
        envelope["status"] = "ok"
        fetcher = fixture_fetcher([envelope])
        with patch.object(alignerr, "request_json", side_effect=fetcher):
            envelope_result = alignerr.fetch_alignerr_snapshot(API_URL)
        self.assertEqual(envelope_result.outcome, ProviderOutcome.CONTRACT_DRIFT)

        record = copy.deepcopy(self.fixtures["v2_single_page"])
        record["jobs"][0]["isActive"] = False
        fetcher = fixture_fetcher([record])
        with patch.object(alignerr, "request_json", side_effect=fetcher):
            record_result = alignerr.fetch_alignerr_snapshot(API_URL)
        self.assertEqual(record_result.outcome, ProviderOutcome.PARTIAL)
        self.assertFalse(evaluate_removal_authorization(record_result).authorized)

    def test_invalid_json_raises_provider_failure(self):
        with patch.object(
            alignerr,
            "urlopen",
            return_value=FakeResponse(self.fixtures["invalid_json"]),
        ):
            with self.assertRaises(json.JSONDecodeError):
                alignerr.request_json(API_URL)

    def test_http_error_raises_provider_failure(self):
        error = HTTPError(API_URL, 503, "unavailable", {}, None)
        with patch.object(alignerr, "urlopen", side_effect=error):
            with self.assertRaises(HTTPError):
                alignerr.request_json(API_URL)

    def test_pagination_page_bound_returns_partial_without_extra_request(self):
        pages = []
        for offset in range(3):
            pages.append(
                {
                    "jobs": [
                        {
                            "id": f"job-{offset}",
                            "title": f"Role {offset}",
                            "location": "Remote",
                            "applyUrl": f"/jobs/job-{offset}",
                            "category": "General",
                        }
                    ],
                    "limit": 1,
                    "offset": offset,
                    "total": 3,
                }
            )
        fetcher = fixture_fetcher(pages)
        with (
            patch.object(alignerr, "MAX_PAGES", 2),
            patch.object(alignerr, "request_json", side_effect=fetcher),
        ):
            result = alignerr.fetch_alignerr_snapshot(API_URL)

        self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
        self.assertEqual(len(fetcher.calls), 2)
        self.assertTrue(any("safety bound" in warning for warning in result.warnings))

    def test_declared_record_bound_returns_partial(self):
        payload = copy.deepcopy(self.fixtures["v2_single_page"])
        payload["total"] = alignerr.MAX_RECORDS + 1
        fetcher = fixture_fetcher([payload])
        with patch.object(alignerr, "request_json", side_effect=fetcher):
            result = alignerr.fetch_alignerr_snapshot(API_URL)

        self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
        self.assertEqual(result.jobs, [])
        self.assertEqual(len(fetcher.calls), 1)


if __name__ == "__main__":
    unittest.main()
