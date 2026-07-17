import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import scripts.profile_match_digest as matcher
import wahojobs.crawler.pipeline as pipeline
from wahojobs.canonical.service import sync_meridial_canonical_opportunities
from wahojobs.crawler.companies.meridial import crawl_meridial
from wahojobs.crawler.providers import greenhouse
from wahojobs.crawler.types import JobCandidate, ProviderOutcome
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import insert_job
from wahojobs.matching.opportunity_trust import TRUSTED, assess_opportunity_trust
from wahojobs.tracking.normalize import with_source_hash


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "greenhouse_provider_contract.json"
API_URL = (
    "https://boards-api.greenhouse.io/v1/boards/agency/departments/"
    "4012485101?render_as=tree"
)
SEED_TIME = "2026-07-01T12:00:00+00:00"
CLOSED_GREENHOUSE_ID = "4778238101"


def fixture_fetcher(payloads):
    remaining = copy.deepcopy(payloads)

    def fetch(_url):
        if not remaining:
            raise AssertionError("Unexpected extra Greenhouse request")
        value = remaining.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    return fetch


class MeridialReliabilityIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "meridial-reliability.sqlite"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        schema_path = (
            Path(__file__).resolve().parents[1] / "wahojobs" / "db" / "schema.sql"
        )
        self.conn.executescript(schema_path.read_text(encoding="utf-8"))
        self.conn.execute(
            """
            INSERT INTO companies (
              id, name, slug, careers_url,
              source_tier, inventory_model, market_count_policy
            ) VALUES (
              1, 'Meridial', 'meridial', ?, 'core', 'live_feed', 'count_live'
            )
            """,
            (API_URL,),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def provider_result(
        self,
        jobs_name="valid_jobs_inventory",
        tree_name="valid_department_hierarchy",
    ):
        fetcher = fixture_fetcher(
            [self.fixtures[jobs_name], self.fixtures[tree_name]]
        )
        with patch.object(greenhouse, "request_json", side_effect=fetcher):
            return crawl_meridial(API_URL)

    @contextmanager
    def pipeline_context(self, slug, crawler):
        @contextmanager
        def temporary_connection():
            conn = get_connection(self.db_path)
            try:
                yield conn
            finally:
                conn.close()

        with (
            patch.object(pipeline, "get_connection", temporary_connection),
            patch.dict(pipeline.CRAWLERS, {slug: crawler}),
        ):
            yield

    def seed_job(
        self,
        external_id,
        title,
        *,
        department="The Agency: Worldwide Sharing > General",
        expertise="General",
        location="World Wide - Remote",
    ):
        item = with_source_hash(
            "meridial",
            JobCandidate(
                external_id=str(external_id),
                title=title,
                location=location,
                url=(
                    "https://job-boards.eu.greenhouse.io/agency/jobs/"
                    f"{external_id}"
                ),
                department=department,
                expertise=expertise,
            ),
        )
        return insert_job(self.conn, 1, item, SEED_TIME)

    def latest_run(self, company_id=1):
        return self.conn.execute(
            "SELECT * FROM crawl_runs WHERE company_id = ? ORDER BY id DESC LIMIT 1",
            (company_id,),
        ).fetchone()

    def state_snapshot(self):
        return {
            "jobs": [dict(row) for row in self.conn.execute("SELECT * FROM jobs ORDER BY id")],
            "events": [
                dict(row)
                for row in self.conn.execute("SELECT * FROM job_events ORDER BY id")
            ],
            "canonical": [
                dict(row)
                for row in self.conn.execute(
                    "SELECT * FROM canonical_opportunities ORDER BY id"
                )
            ],
        }

    def test_complete_snapshot_is_success_trusted_and_removes_closed_job(self):
        unchanged_id = self.seed_job(
            "1001",
            "Python Specialist - Freelance AI Trainer Project",
            department="The Agency: Worldwide Sharing > Engineering & Technology",
            expertise="Engineering & Technology",
        )
        closed_id = self.seed_job(
            CLOSED_GREENHOUSE_ID,
            "Closed English Data Contributor - Freelance AI Trainer Project",
            department="The Agency: Worldwide Sharing > Language & Linguistics",
            expertise="Language & Linguistics",
        )
        sync_meridial_canonical_opportunities(self.conn, 1)
        closed_canonical_id = self.conn.execute(
            "SELECT canonical_opportunity_id FROM jobs WHERE id = ?",
            (closed_id,),
        ).fetchone()[0]
        self.conn.commit()
        result = self.provider_result()

        with self.pipeline_context("meridial", lambda _url: result):
            _, summary = pipeline.run_crawl("meridial")

        run = self.latest_run()
        self.assertEqual(run["status"], "success")
        self.assertTrue(summary.removals_authorized)
        self.assertEqual(summary.jobs_new, 3)
        self.assertEqual(summary.jobs_updated, 1)
        self.assertEqual(summary.jobs_removed, 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT is_active FROM jobs WHERE id = ?", (closed_id,)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM job_events WHERE job_id = ? AND event_type = 'removed'",
                (closed_id,),
            ).fetchone()[0],
            1,
        )
        canonical = self.conn.execute(
            "SELECT is_active, variant_count FROM canonical_opportunities WHERE id = ?",
            (closed_canonical_id,),
        ).fetchone()
        self.assertEqual((canonical["is_active"], canonical["variant_count"]), (0, 0))
        self.assertEqual(
            self.conn.execute(
                "SELECT is_active FROM jobs WHERE id = ?", (unchanged_id,)
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE external_id = '1002' AND is_active = 1"
            ).fetchone()[0],
            1,
        )

        active = next(
            dict(row)
            for row in matcher.get_active_rows(self.conn)
            if row["job_id"] == unchanged_id
        )
        self.assertEqual(active["source_run_id"], run["id"])
        trust = assess_opportunity_trust(
            active,
            "not_applicable",
            now=datetime.now(timezone.utc),
        )
        self.assertEqual(trust.status, TRUSTED)
        self.assertFalse(
            any(
                row["job_id"] == closed_id
                for row in matcher.get_active_rows(self.conn)
            )
        )

    def test_canonical_stays_active_when_an_observed_variant_remains(self):
        observed_id = self.seed_job(
            "1001",
            "Python Specialist - Freelance AI Trainer Project",
            department="The Agency: Worldwide Sharing > Engineering & Technology",
            expertise="Engineering & Technology",
        )
        missing_id = self.seed_job(
            CLOSED_GREENHOUSE_ID,
            "Python Specialist - Freelance AI Trainer Project",
            department="The Agency: Worldwide Sharing > Engineering & Technology",
            expertise="Engineering & Technology",
        )
        sync_meridial_canonical_opportunities(self.conn, 1)
        canonical_ids = {
            row[0]
            for row in self.conn.execute(
                "SELECT canonical_opportunity_id FROM jobs WHERE id IN (?, ?)",
                (observed_id, missing_id),
            )
        }
        self.assertEqual(len(canonical_ids), 1)
        canonical_id = canonical_ids.pop()
        self.conn.commit()

        result = self.provider_result()
        with self.pipeline_context("meridial", lambda _url: result):
            pipeline.run_crawl("meridial")

        canonical = self.conn.execute(
            "SELECT is_active, variant_count FROM canonical_opportunities WHERE id = ?",
            (canonical_id,),
        ).fetchone()
        self.assertEqual((canonical["is_active"], canonical["variant_count"]), (1, 1))

    def test_partial_and_malformed_inventory_do_not_remove_or_renew_freshness(self):
        observed_id = self.seed_job("1001", "Python Specialist")
        missing_id = self.seed_job(CLOSED_GREENHOUSE_ID, "Closed Specialist")
        sync_meridial_canonical_opportunities(self.conn, 1)
        self.conn.execute(
            """
            INSERT INTO crawl_runs (
              id, company_id, status, started_at, finished_at, used_sample_data
            ) VALUES (50, 1, 'success', ?, ?, 0)
            """,
            (SEED_TIME, SEED_TIME),
        )
        self.conn.commit()
        partial = self.provider_result("duplicate_job_ids", "empty_department_hierarchy")

        with self.pipeline_context("meridial", lambda _url: partial):
            _, summary = pipeline.run_crawl("meridial")

        self.assertEqual(self.latest_run()["status"], "partial")
        self.assertFalse(summary.removals_authorized)
        self.assertEqual(
            self.conn.execute(
                "SELECT is_active FROM jobs WHERE id = ?", (missing_id,)
            ).fetchone()[0],
            1,
        )
        active = next(
            dict(row)
            for row in matcher.get_active_rows(self.conn)
            if row["job_id"] == observed_id
        )
        self.assertEqual(active["source_run_id"], 50)

    def test_contract_drift_performs_no_lifecycle_or_canonical_writes(self):
        self.seed_job(CLOSED_GREENHOUSE_ID, "Closed Specialist")
        sync_meridial_canonical_opportunities(self.conn, 1)
        self.conn.commit()
        before = self.state_snapshot()
        fetcher = fixture_fetcher([self.fixtures["invalid_root_envelope"]])
        with patch.object(greenhouse, "request_json", side_effect=fetcher):
            drift = crawl_meridial(API_URL)

        with self.pipeline_context("meridial", lambda _url: drift):
            pipeline.run_crawl("meridial")

        self.assertEqual(self.latest_run()["status"], "contract_drift")
        self.assertEqual(self.state_snapshot(), before)

    def test_empty_inventory_is_partial_and_does_not_remove(self):
        missing_id = self.seed_job(CLOSED_GREENHOUSE_ID, "Closed Specialist")
        sync_meridial_canonical_opportunities(self.conn, 1)
        self.conn.commit()
        empty = self.provider_result("empty_jobs_inventory", "empty_department_hierarchy")

        with self.pipeline_context("meridial", lambda _url: empty):
            _, summary = pipeline.run_crawl("meridial")

        self.assertEqual(self.latest_run()["status"], "partial")
        self.assertFalse(summary.removals_authorized)
        self.assertEqual(
            self.conn.execute(
                "SELECT is_active FROM jobs WHERE id = ?", (missing_id,)
            ).fetchone()[0],
            1,
        )

    def test_failed_enrichment_cannot_cause_mass_removal(self):
        self.seed_job(CLOSED_GREENHOUSE_ID, "Closed Specialist")
        sync_meridial_canonical_opportunities(self.conn, 1)
        self.conn.commit()
        before = self.state_snapshot()
        fetcher = fixture_fetcher(
            [self.fixtures["valid_jobs_inventory"], RuntimeError("department fetch failed")]
        )

        def live_crawler(url):
            with patch.object(greenhouse, "request_json", side_effect=fetcher):
                return crawl_meridial(url)

        with (
            self.pipeline_context("meridial", live_crawler),
            self.assertRaisesRegex(RuntimeError, "department fetch failed"),
        ):
            pipeline.run_crawl("meridial")

        self.assertEqual(self.latest_run()["status"], "failed")
        self.assertEqual(self.state_snapshot(), before)

    def test_recovery_after_partial_and_drift_succeeds(self):
        missing_id = self.seed_job(CLOSED_GREENHOUSE_ID, "Closed Specialist")
        sync_meridial_canonical_opportunities(self.conn, 1)
        self.conn.commit()

        partial = self.provider_result("duplicate_job_ids", "empty_department_hierarchy")
        with self.pipeline_context("meridial", lambda _url: partial):
            pipeline.run_crawl("meridial")
        self.assertEqual(
            self.conn.execute(
                "SELECT is_active FROM jobs WHERE id = ?", (missing_id,)
            ).fetchone()[0],
            1,
        )

        fetcher = fixture_fetcher([self.fixtures["invalid_root_envelope"]])
        with patch.object(greenhouse, "request_json", side_effect=fetcher):
            drift = crawl_meridial(API_URL)
        with self.pipeline_context("meridial", lambda _url: drift):
            pipeline.run_crawl("meridial")
        self.assertEqual(
            self.conn.execute(
                "SELECT is_active FROM jobs WHERE id = ?", (missing_id,)
            ).fetchone()[0],
            1,
        )

        complete = self.provider_result()
        with self.pipeline_context("meridial", lambda _url: complete):
            _, summary = pipeline.run_crawl("meridial")
        self.assertEqual(self.latest_run()["status"], "success")
        self.assertEqual(summary.jobs_removed, 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT is_active FROM jobs WHERE id = ?", (missing_id,)
            ).fetchone()[0],
            0,
        )

    def test_disabled_registry_source_cannot_use_ordinary_dispatch(self):
        self.conn.execute(
            """
            INSERT INTO companies (
              id, name, slug, careers_url,
              source_tier, inventory_model, market_count_policy
            ) VALUES (
              2, 'Invisible Technologies', 'invisible',
              'https://boards-api.greenhouse.io/v1/boards/invisibletech/jobs',
              'experimental', 'corporate_careers', 'exclude_live_estimate'
            )
            """
        )
        invisible_item = with_source_hash(
            "invisible",
            JobCandidate(
                external_id="legacy-old",
                title="Legacy Invisible Role",
                location="Remote",
                url="https://boards.greenhouse.io/invisibletech/jobs/legacy-old",
            ),
        )
        old_id = insert_job(self.conn, 2, invisible_item, SEED_TIME)
        self.conn.commit()
        with patch(
            "wahojobs.crawler.companies.invisible.fetch_greenhouse_jobs"
        ) as fetch_jobs:
            with self.assertRaisesRegex(PermissionError, "not enabled"):
                pipeline.run_crawl("invisible")

        fetch_jobs.assert_not_called()
        self.assertIsNone(self.latest_run(2))
        self.assertEqual(
            self.conn.execute(
                "SELECT is_active FROM jobs WHERE id = ?", (old_id,)
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
