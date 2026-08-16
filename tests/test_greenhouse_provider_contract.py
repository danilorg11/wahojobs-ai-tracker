import copy
import json
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from wahojobs.crawler.companies.invisible import crawl_invisible
from wahojobs.crawler.companies.meridial import (
    MERIDIAL_GREENHOUSE_CONFIG,
    crawl_meridial,
)
from wahojobs.crawler.providers import greenhouse
from wahojobs.crawler.types import JobCandidate, ProviderOutcome, evaluate_removal_authorization


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "greenhouse_provider_contract.json"
CONFIGURED_URL = (
    "https://boards-api.greenhouse.io/v1/boards/agency/departments/"
    "4012485101?render_as=tree"
)


def fixture_fetcher(payloads):
    remaining = copy.deepcopy(payloads)
    calls = []

    def fetch(url):
        calls.append(url)
        if not remaining:
            raise AssertionError(f"Unexpected Greenhouse request: {url}")
        return remaining.pop(0)

    fetch.calls = calls
    return fetch


class GreenhouseProviderContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def fetch(self, jobs_name="valid_jobs_inventory", tree_name="valid_department_hierarchy"):
        fetcher = fixture_fetcher(
            [self.fixtures[jobs_name], self.fixtures[tree_name]]
        )
        with patch.object(greenhouse, "request_json", side_effect=fetcher):
            result = crawl_meridial(CONFIGURED_URL)
        return result, fetcher.calls

    def test_valid_full_inventory_is_authoritative_and_preserves_mapping(self):
        result, calls = self.fetch()

        self.assertEqual(result.outcome, ProviderOutcome.SUCCESS)
        self.assertTrue(result.snapshot_complete)
        self.assertTrue(result.pagination_complete)
        self.assertFalse(result.used_sample_data)
        self.assertFalse(result.empty_snapshot_validated)
        self.assertEqual(result.payload_shape, greenhouse.GREENHOUSE_V1_PAYLOAD_SHAPE)
        self.assertEqual(result.raw_record_count, 4)
        self.assertEqual(result.normalized_record_count, 4)
        self.assertEqual(result.rejected_record_count, 0)
        self.assertTrue(evaluate_removal_authorization(result).authorized)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], greenhouse.build_jobs_url(MERIDIAL_GREENHOUSE_CONFIG))
        self.assertEqual(
            calls[1],
            greenhouse.build_department_tree_url(MERIDIAL_GREENHOUSE_CONFIG),
        )

        jobs = {job.external_id: job for job in result.jobs}
        self.assertEqual(jobs["1001"].title, "Python Specialist - Freelance AI Trainer Project")
        self.assertEqual(jobs["1001"].url, "https://job-boards.eu.greenhouse.io/agency/jobs/1001")
        self.assertEqual(jobs["1001"].location, "World Wide - Remote")
        self.assertEqual(
            jobs["1001"].department,
            "The Agency: Worldwide Sharing > Engineering & Technology",
        )
        self.assertEqual(jobs["1001"].expertise, "Engineering & Technology")
        self.assertIn("Engineering & Technology", jobs["1003"].department)
        self.assertIn("Language & Linguistics", jobs["1003"].department)
        self.assertIsNone(jobs["1004"].department)
        self.assertTrue(any("unassigned" in item for item in result.warnings))

    def test_schema_fingerprint_is_deterministic_and_versioned(self):
        first, _ = self.fetch()
        second, _ = self.fetch()
        self.assertEqual(first.schema_fingerprint, second.schema_fingerprint)
        self.assertEqual(first.schema_fingerprint, greenhouse.greenhouse_schema_fingerprint())
        self.assertTrue(
            first.schema_fingerprint.startswith("greenhouse-job-board-v1:sha256:")
        )

    def test_rich_metadata_is_preserved_in_source_records(self):
        jobs = copy.deepcopy(self.fixtures["valid_jobs_inventory"])
        jobs["jobs"][0].update(
            {
                "content": "<p>Python role description</p>",
                "internal_job_id": 9001,
                "requisition_id": "REQ-1001",
                "first_published": "2026-06-01T12:00:00-04:00",
                "application_deadline": "2026-08-01T12:00:00-04:00",
                "language": "en",
                "company_name": "Meridial",
                "metadata": [{"name": "Priority", "value": "High"}],
                "education": "education_required",
                "data_compliance": [{"type": "gdpr", "requires_consent": False}],
                "application_url": "https://apply.example.com/forms/1001",
                "compensation_usd": {"minimum": 20, "maximum": 40},
                "unknown_public_field": {"nested": [1, True, None]},
            }
        )
        fetcher = fixture_fetcher([jobs, self.fixtures["valid_department_hierarchy"]])
        with patch.object(greenhouse, "request_json", side_effect=fetcher):
            result = crawl_meridial(CONFIGURED_URL)

        records = {record.external_id: record for record in result.source_records}
        record = records["1001"]
        candidate = next(job for job in result.jobs if job.external_id == "1001")
        self.assertEqual(record.source_name, "Meridial")
        self.assertEqual(record.company_id, "meridial")
        self.assertEqual(record.board_token, "agency")
        self.assertEqual(record.greenhouse_job_id, 1001)
        self.assertEqual(record.description_html, "<p>Python role description</p>")
        self.assertEqual(record.updated_at, "2026-07-01T12:00:00-04:00")
        self.assertEqual(record.internal_job_id, 9001)
        self.assertEqual(record.requisition_id, "REQ-1001")
        self.assertEqual(record.first_published, "2026-06-01T12:00:00-04:00")
        self.assertEqual(record.application_deadline, "2026-08-01T12:00:00-04:00")
        self.assertEqual(record.language, "en")
        self.assertEqual(record.company_name, "Meridial")
        self.assertEqual(record.application_url, "https://apply.example.com/forms/1001")
        self.assertIn("Worldwide - Remote", record.additional_locations)
        self.assertEqual(json.loads(record.metadata_json)[0]["value"], "High")
        self.assertEqual(json.loads(record.education_json), "education_required")
        self.assertFalse(json.loads(record.compliance_json)[0]["requires_consent"])
        self.assertEqual(json.loads(record.compensation_json)["compensation_usd"]["minimum"], 20)
        self.assertEqual(
            json.loads(record.raw_public_payload_json)["unknown_public_field"]["nested"],
            [1, True, None],
        )
        self.assertEqual(record.departments[0].name, "Engineering & Technology")
        self.assertEqual(record.offices[0].name, "Worldwide - Remote")
        self.assertEqual(candidate.source_body, "<p>Python role description</p>")
        self.assertEqual(candidate.source_body_format, "text/html")
        self.assertEqual(candidate.source_updated_at, record.updated_at)
        self.assertEqual(candidate.source_metadata["requisition_id"], "REQ-1001")
        self.assertEqual(
            candidate.source_metadata["unknown_public_field"]["nested"],
            [1, True, None],
        )

        second, _ = self.fetch()
        jobs_again = copy.deepcopy(jobs)
        fetcher = fixture_fetcher([jobs_again, self.fixtures["valid_department_hierarchy"]])
        with patch.object(greenhouse, "request_json", side_effect=fetcher):
            deterministic = crawl_meridial(CONFIGURED_URL)
        self.assertEqual(
            record.raw_public_payload_json,
            deterministic.source_records[0].raw_public_payload_json,
        )
        self.assertEqual(second.outcome, ProviderOutcome.SUCCESS)

    def test_optional_public_metadata_wrong_types_make_snapshot_partial(self):
        cases = {
            "internal_job_id": True,
            "metadata": {},
            "data_compliance": {},
            "education": 123,
            "company_name": ["Meridial"],
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                jobs = copy.deepcopy(self.fixtures["valid_jobs_inventory"])
                jobs["jobs"][0][field] = value
                fetcher = fixture_fetcher([jobs, self.fixtures["valid_department_hierarchy"]])
                with patch.object(greenhouse, "request_json", side_effect=fetcher):
                    result = crawl_meridial(CONFIGURED_URL)
                self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
                self.assertFalse(evaluate_removal_authorization(result).authorized)

    def test_job_urls_are_bound_to_registry_board_host_and_id(self):
        config = MERIDIAL_GREENHOUSE_CONFIG
        valid = "https://job-boards.eu.greenhouse.io/agency/jobs/7001"
        self.assertIsNone(greenhouse.validate_job_url(valid, 7001, config))
        invalid = (
            "https://job-boards.greenhouse.io/customerio/jobs/7001",
            "https://evil.example/jobs/7001",
            "https://job-boards.greenhouse.io/agency",
            "https://job-boards.greenhouse.io/agency/jobs/another-id",
            "//job-boards.greenhouse.io/agency/jobs/7001",
            "http://job-boards.greenhouse.io/agency/jobs/7001",
            "https://agency@job-boards.greenhouse.io/agency/jobs/7001",
        )
        for url in invalid:
            with self.subTest(url=url):
                self.assertIsNotNone(greenhouse.validate_job_url(url, 7001, config))
        custom = greenhouse.GreenhouseBoardConfig(
            source_name="Example",
            company_id="example",
            board_token="example",
            allowed_job_hosts=("job-boards.greenhouse.io", "careers.example.com"),
        )
        self.assertIsNone(
            greenhouse.validate_job_url(
                "https://careers.example.com/example/jobs/7001", 7001, custom
            )
        )

    def test_cross_board_url_invalidates_the_snapshot(self):
        jobs = copy.deepcopy(self.fixtures["valid_jobs_inventory"])
        jobs["jobs"][0]["absolute_url"] = (
            "https://job-boards.greenhouse.io/customerio/jobs/1001"
        )
        fetcher = fixture_fetcher([jobs, self.fixtures["valid_department_hierarchy"]])
        with patch.object(greenhouse, "request_json", side_effect=fetcher):
            result = crawl_meridial(CONFIGURED_URL)
        self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
        self.assertFalse(evaluate_removal_authorization(result).authorized)

    def test_unknown_jobs_contracts_are_contract_drift(self):
        for name in (
            "invalid_root_envelope",
            "missing_jobs_key",
            "jobs_wrong_type",
            "application_level_error",
            "invalid_board",
        ):
            with self.subTest(name=name):
                fetcher = fixture_fetcher([self.fixtures[name]])
                with patch.object(greenhouse, "request_json", side_effect=fetcher):
                    result = crawl_meridial(CONFIGURED_URL)
                self.assertEqual(result.outcome, ProviderOutcome.CONTRACT_DRIFT)
                self.assertEqual(result.jobs, [])
                self.assertFalse(result.snapshot_complete)
                self.assertFalse(evaluate_removal_authorization(result).authorized)
                self.assertEqual(len(fetcher.calls), 1)

    def test_unknown_department_root_contract_is_contract_drift(self):
        fetcher = fixture_fetcher(
            [self.fixtures["valid_jobs_inventory"], self.fixtures["missing_jobs_key"]]
        )
        with patch.object(greenhouse, "request_json", side_effect=fetcher):
            result = crawl_meridial(CONFIGURED_URL)
        self.assertEqual(result.outcome, ProviderOutcome.CONTRACT_DRIFT)
        self.assertEqual(result.raw_record_count, 4)
        self.assertEqual(result.jobs, [])

    def test_malformed_inventory_records_make_snapshot_partial(self):
        names = (
            "missing_job_id",
            "missing_title",
            "missing_application_url",
            "invalid_application_url",
            "invalid_location_structure",
        )
        for name in names:
            with self.subTest(name=name):
                result, _ = self.fetch(name, "empty_department_hierarchy")
                self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
                self.assertFalse(result.snapshot_complete)
                self.assertFalse(result.pagination_complete)
                self.assertEqual(result.raw_record_count, 1)
                self.assertEqual(result.rejected_record_count, 1)
                self.assertFalse(evaluate_removal_authorization(result).authorized)

    def test_duplicate_and_conflicting_inventory_ids_are_partial(self):
        for name, warning in (
            ("duplicate_job_ids", "duplicate Greenhouse job id"),
            ("conflicting_duplicate_records", "conflicting duplicate Greenhouse job id"),
        ):
            with self.subTest(name=name):
                result, _ = self.fetch(name, "empty_department_hierarchy")
                self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
                self.assertEqual(result.rejected_record_count, 1)
                self.assertTrue(any(warning in item for item in result.warnings))

    def test_unknown_department_job_and_conflicting_enrichment_are_partial(self):
        result, _ = self.fetch(
            "valid_jobs_inventory",
            "department_job_absent_inventory",
        )
        self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
        self.assertTrue(any("unknown inventory job" in item for item in result.warnings))

        conflicting_tree = copy.deepcopy(self.fixtures["valid_department_hierarchy"])
        conflicting_tree["children"][0]["jobs"][0]["title"] = "Conflicting title"
        fetcher = fixture_fetcher(
            [self.fixtures["valid_jobs_inventory"], conflicting_tree]
        )
        with patch.object(greenhouse, "request_json", side_effect=fetcher):
            conflict = crawl_meridial(CONFIGURED_URL)
        self.assertEqual(conflict.outcome, ProviderOutcome.PARTIAL)
        self.assertTrue(any("conflicting title" in item for item in conflict.warnings))

    def test_inventory_job_absent_from_departments_is_preserved(self):
        result, _ = self.fetch(
            "inventory_job_absent_departments",
            "empty_department_hierarchy",
        )
        self.assertEqual(result.outcome, ProviderOutcome.SUCCESS)
        self.assertEqual([job.external_id for job in result.jobs], ["1004"])
        self.assertIsNone(result.jobs[0].department)
        self.assertTrue(any("unassigned" in item for item in result.warnings))
        self.assertTrue(evaluate_removal_authorization(result).authorized)

    def test_invalid_department_hierarchy_is_partial(self):
        result, _ = self.fetch(
            "valid_jobs_inventory",
            "invalid_department_hierarchy",
        )
        self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
        self.assertTrue(any("missing required fields" in item for item in result.warnings))
        self.assertFalse(evaluate_removal_authorization(result).authorized)

    def test_empty_inventory_is_partial_and_non_authoritative(self):
        result, _ = self.fetch("empty_jobs_inventory", "empty_department_hierarchy")
        self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
        self.assertEqual(result.jobs, [])
        self.assertFalse(result.empty_snapshot_validated)
        self.assertFalse(evaluate_removal_authorization(result).authorized)

    def test_meta_total_mismatch_and_record_bound_are_partial(self):
        mismatch = copy.deepcopy(self.fixtures["valid_jobs_inventory"])
        mismatch["meta"]["total"] = 99
        fetcher = fixture_fetcher([mismatch])
        with patch.object(greenhouse, "request_json", side_effect=fetcher):
            result = greenhouse.fetch_greenhouse_snapshot(MERIDIAL_GREENHOUSE_CONFIG)
        self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
        self.assertTrue(any("meta.total" in item for item in result.warnings))

        bounded = copy.deepcopy(MERIDIAL_GREENHOUSE_CONFIG)
        bounded = greenhouse.GreenhouseBoardConfig(
            source_name=bounded.source_name,
            board_token=bounded.board_token,
            api_host=bounded.api_host,
            root_department_id=bounded.root_department_id,
            max_records=2,
        )
        fetcher = fixture_fetcher([self.fixtures["valid_jobs_inventory"]])
        with patch.object(greenhouse, "request_json", side_effect=fetcher):
            result = greenhouse.fetch_greenhouse_snapshot(bounded)
        self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
        self.assertTrue(any("safety bound" in item for item in result.warnings))

    def test_transport_http_and_invalid_json_fail_as_provider_exceptions(self):
        failures = (
            HTTPError("https://example.test", 503, "Unavailable", {}, None),
            json.JSONDecodeError("bad JSON", "{", 1),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with (
                    patch.object(greenhouse, "request_json", side_effect=failure),
                    self.assertRaises(type(failure)),
                ):
                    crawl_meridial(CONFIGURED_URL)

    def test_additive_fields_are_diagnostic_but_not_contract_drift(self):
        jobs = copy.deepcopy(self.fixtures["valid_jobs_inventory"])
        jobs["request_id"] = "fixture"
        jobs["meta"]["trace_id"] = "trace"
        fetcher = fixture_fetcher([jobs, self.fixtures["valid_department_hierarchy"]])
        with patch.object(greenhouse, "request_json", side_effect=fetcher):
            result = crawl_meridial(CONFIGURED_URL)
        self.assertEqual(result.outcome, ProviderOutcome.SUCCESS)
        self.assertTrue(any("additive root" in item for item in result.warnings))
        self.assertTrue(any("meta includes additive" in item for item in result.warnings))

    def test_invisible_remains_legacy_non_authoritative(self):
        candidate = JobCandidate(
            external_id="invisible-1",
            title="Operations Specialist",
            location="Remote",
            url="https://boards.greenhouse.io/invisibletech/jobs/invisible-1",
        )
        with patch(
            "wahojobs.crawler.companies.invisible.fetch_greenhouse_jobs",
            return_value=[candidate],
        ):
            result = crawl_invisible(
                "https://boards-api.greenhouse.io/v1/boards/invisibletech/jobs"
            )
        self.assertEqual(result.outcome, ProviderOutcome.PARTIAL)
        self.assertFalse(result.snapshot_complete)
        self.assertFalse(result.pagination_complete)
        self.assertFalse(evaluate_removal_authorization(result).authorized)


if __name__ == "__main__":
    unittest.main()
