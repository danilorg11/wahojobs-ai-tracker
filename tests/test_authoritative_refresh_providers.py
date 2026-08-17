import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from wahojobs.crawler.companies.dataforce import crawl_dataforce
from wahojobs.crawler.companies.mindrift import crawl_mindrift
from wahojobs.crawler.companies.oneforma import crawl_oneforma
from wahojobs.crawler.companies.turing import crawl_turing
from wahojobs.crawler.companies.welocalize import crawl_welocalize
from wahojobs.crawler.providers import dataforce, oneforma, turing, workable_markdown
from wahojobs.crawler.types import (
    JobCandidate,
    ProviderOutcome,
    evaluate_removal_authorization,
)
from wahojobs.tracking import service as tracking_service


def candidate(external_id):
    return JobCandidate(
        external_id=external_id,
        title=f"Role {external_id}",
        location="Remote",
        url=f"https://example.test/jobs/{external_id}",
    )


class AuthoritativeCompanyContractTests(unittest.TestCase):
    def assert_authoritative(self, result, expected_count):
        self.assertEqual(result.outcome, ProviderOutcome.SUCCESS)
        self.assertTrue(result.snapshot_complete)
        self.assertTrue(result.pagination_complete)
        self.assertEqual(result.normalized_record_count, expected_count)
        self.assertTrue(evaluate_removal_authorization(result).authorized)

    @patch("wahojobs.crawler.companies.welocalize.fetch_lever_postings")
    def test_welocalize_full_lever_list_is_authoritative(self, fetch_postings):
        fetch_postings.return_value = [
            {
                "id": "in-scope",
                "text": "AI Data Rater",
                "hostedUrl": "https://jobs.example.test/in-scope",
                "categories": {
                    "department": "Welo Data - AI Services",
                    "location": "Remote",
                },
            },
            {
                "id": "out-of-scope",
                "text": "Corporate Role",
                "hostedUrl": "https://jobs.example.test/out-of-scope",
                "categories": {"department": "Corporate"},
            },
        ]

        result = crawl_welocalize("https://example.test/lever")

        self.assert_authoritative(result, 1)
        self.assertEqual(result.raw_record_count, 2)

    def test_paginated_company_crawlers_declare_the_common_contract(self):
        cases = (
            (
                "wahojobs.crawler.companies.turing.fetch_turing_jobs",
                crawl_turing,
            ),
            (
                "wahojobs.crawler.companies.mindrift.fetch_workable_jobs",
                crawl_mindrift,
            ),
            (
                "wahojobs.crawler.companies.oneforma.fetch_oneforma_jobs",
                crawl_oneforma,
            ),
            (
                "wahojobs.crawler.companies.dataforce.fetch_dataforce_jobs",
                crawl_dataforce,
            ),
        )
        for patch_target, crawler in cases:
            with self.subTest(provider=patch_target), patch(
                patch_target,
                return_value=[candidate("one"), candidate("two")],
            ):
                result = crawler("https://example.test/source")
                self.assert_authoritative(result, 2)
                self.assertEqual(result.raw_record_count, 2)

    @patch("wahojobs.crawler.companies.turing.fetch_turing_jobs", return_value=[])
    def test_empty_snapshot_is_not_implicitly_authorized(self, _fetch_jobs):
        result = crawl_turing("https://example.test/turing")

        self.assertEqual(result.outcome, ProviderOutcome.SUCCESS)
        self.assertTrue(result.snapshot_complete)
        self.assertFalse(evaluate_removal_authorization(result).authorized)


class ProviderPaginationSafetyTests(unittest.TestCase):
    @patch("wahojobs.crawler.providers.turing.fetch_page")
    def test_turing_requires_exact_stable_total(self, fetch_page):
        fetch_page.side_effect = [
            {
                "success": True,
                "totalCount": 2,
                "jobs": [{"id": "1", "jobCode": "T-1", "title": "Role 1"}],
            },
            {"success": True, "totalCount": 2, "jobs": []},
        ]

        with self.assertRaisesRegex(ValueError, "before totalCount"):
            turing.fetch_turing_jobs("https://example.test/turing")

    @patch("wahojobs.crawler.providers.workable_markdown.time.sleep")
    @patch("wahojobs.crawler.providers.workable_markdown.fetch_api_page")
    def test_workable_follows_tokens_until_exact_total(self, fetch_page, _sleep):
        fetch_page.side_effect = [
            {
                "total": 2,
                "results": [{"shortcode": "one"}],
                "nextPage": "token-2",
            },
            {
                "total": 2,
                "results": [{"shortcode": "two"}],
                "nextPage": None,
            },
        ]

        rows = workable_markdown.fetch_all_api_rows("https://example.test/workable")

        self.assertEqual([row["shortcode"] for row in rows], ["one", "two"])

    @patch("wahojobs.crawler.providers.workable_markdown.fetch_api_page")
    def test_workable_rejects_premature_token_exhaustion(self, fetch_page):
        fetch_page.return_value = {
            "total": 2,
            "results": [{"shortcode": "one"}],
            "nextPage": None,
        }

        with self.assertRaisesRegex(ValueError, "before total"):
            workable_markdown.fetch_all_api_rows("https://example.test/workable")

    @patch("wahojobs.crawler.providers.oneforma.fetch_page")
    def test_oneforma_requires_stable_declared_page_count(self, fetch_page):
        fetch_page.side_effect = [
            ([{"id": 1}], 2),
            ([{"id": 2}], 3),
        ]

        with self.assertRaisesRegex(ValueError, "page count changed"):
            oneforma.fetch_all_posts("https://example.test/oneforma")

    @patch("wahojobs.crawler.providers.dataforce.parse_jobs_page")
    @patch("wahojobs.crawler.providers.dataforce.fetch_page", return_value="page")
    def test_dataforce_requires_empty_end_page(self, _fetch_page, parse_page):
        parse_page.side_effect = [[candidate("one")], []]

        jobs = dataforce.fetch_dataforce_jobs("https://example.test/dataforce")

        self.assertEqual([job.external_id for job in jobs], ["one"])

    @patch("wahojobs.crawler.providers.dataforce.parse_jobs_page")
    @patch("wahojobs.crawler.providers.dataforce.fetch_page", return_value="page")
    def test_dataforce_cap_exhaustion_is_not_complete(self, _fetch_page, parse_page):
        parse_page.side_effect = [
            [candidate(f"job-{index}")] for index in range(dataforce.MAX_PAGES)
        ]

        with self.assertRaisesRegex(RuntimeError, "safety cap"):
            dataforce.fetch_dataforce_jobs("https://example.test/dataforce")


class MindriftGuardBaselineTests(unittest.TestCase):
    NOW = datetime(2026, 8, 17, 2, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE crawl_runs (
              id INTEGER PRIMARY KEY,
              company_id INTEGER NOT NULL,
              status TEXT NOT NULL,
              started_at TEXT NOT NULL,
              finished_at TEXT,
              used_sample_data INTEGER NOT NULL DEFAULT 0,
              error_message TEXT
            )
            """
        )

    def tearDown(self):
        self.connection.close()

    def add_success(self, observed_at):
        self.connection.execute(
            """
            INSERT INTO crawl_runs (
              id, company_id, status, started_at, finished_at,
              used_sample_data, error_message
            ) VALUES (1, 7, 'success', ?, ?, 0, NULL)
            """,
            (observed_at.isoformat(), observed_at.isoformat()),
        )

    def test_stale_success_does_not_veto_certified_recovery_snapshot(self):
        self.add_success(self.NOW - timedelta(hours=73))

        self.assertFalse(
            tracking_service.has_fresh_mindrift_guard_baseline(
                self.connection,
                7,
                self.NOW.isoformat(),
            )
        )

    def test_fresh_success_remains_a_guard_baseline(self):
        self.add_success(self.NOW - timedelta(hours=72))

        self.assertTrue(
            tracking_service.has_fresh_mindrift_guard_baseline(
                self.connection,
                7,
                self.NOW.isoformat(),
            )
        )

    def test_unparseable_time_fails_closed(self):
        self.add_success(self.NOW)

        self.assertTrue(
            tracking_service.has_fresh_mindrift_guard_baseline(
                self.connection,
                7,
                "not-a-timestamp",
            )
        )


if __name__ == "__main__":
    unittest.main()
