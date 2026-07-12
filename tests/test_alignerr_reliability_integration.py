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
from wahojobs.canonical.service import sync_alignerr_canonical_opportunities
from wahojobs.crawler.providers import alignerr
from wahojobs.crawler.types import JobCandidate, ProviderOutcome
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import insert_job
from wahojobs.matching.opportunity_trust import TRUSTED, assess_opportunity_trust
from wahojobs.tracking.normalize import with_source_hash


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "alignerr_provider_contract.json"
API_URL = "https://www.alignerr.com/api/jobs"
SEED_TIME = "2026-07-01T12:00:00+00:00"


def fixture_fetcher(payloads):
    remaining = copy.deepcopy(payloads)

    def fetch(_url):
        if not remaining:
            raise AssertionError("Unexpected extra Alignerr request")
        return remaining.pop(0)

    return fetch


class AlignerrReliabilityIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "alignerr-reliability.sqlite"
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
              1, 'Alignerr', 'alignerr', ?, 'core', 'live_feed', 'count_live'
            )
            """,
            (API_URL,),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def provider_result(self, fixture_name):
        payload = self.fixtures[fixture_name]
        paginated = {
            "changing_total",
            "duplicate_page",
            "early_empty_page",
            "final_count_mismatch",
            "no_progress",
            "offset_not_advancing",
            "v2_multiple_pages",
        }
        pages = payload if fixture_name in paginated else [payload]
        with patch.object(
            alignerr,
            "request_json",
            side_effect=fixture_fetcher(pages),
        ):
            return alignerr.fetch_alignerr_snapshot(API_URL)

    @contextmanager
    def pipeline_context(self, crawler):
        @contextmanager
        def temporary_connection():
            conn = get_connection(self.db_path)
            try:
                yield conn
            finally:
                conn.close()

        with (
            patch.object(pipeline, "get_connection", temporary_connection),
            patch.dict(pipeline.CRAWLERS, {"alignerr": crawler}),
        ):
            yield

    def seed_job(self, external_id, title, *, active=True):
        item = with_source_hash(
            "alignerr",
            JobCandidate(
                external_id=external_id,
                title=title,
                location="Remote",
                url=f"https://www.alignerr.com/jobs/{external_id}",
                department="General",
                expertise="General",
            ),
        )
        job_id = insert_job(self.conn, 1, item, SEED_TIME)
        if not active:
            self.conn.execute(
                "UPDATE jobs SET is_active = 0, removed_at = ? WHERE id = ?",
                (SEED_TIME, job_id),
            )
        return job_id

    def latest_run(self):
        return self.conn.execute(
            "SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def state_snapshot(self):
        return {
            "jobs": [
                dict(row) for row in self.conn.execute("SELECT * FROM jobs ORDER BY id")
            ],
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

    def test_complete_v2_is_success_trusted_and_removes_missing_canonical(self):
        self.seed_job("job-a", "Python Expert")
        missing_id = self.seed_job("job-old", "Obsolete Specialist")
        sync_alignerr_canonical_opportunities(self.conn, 1)
        missing_canonical_id = self.conn.execute(
            "SELECT canonical_opportunity_id FROM jobs WHERE id = ?",
            (missing_id,),
        ).fetchone()[0]
        self.conn.commit()
        result = self.provider_result("v2_single_page")

        with self.pipeline_context(lambda _url: result):
            _, summary = pipeline.run_crawl("alignerr")

        run = self.latest_run()
        self.assertEqual(run["status"], "success")
        self.assertTrue(summary.removals_authorized)
        self.assertEqual(summary.jobs_removed, 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT is_active FROM jobs WHERE id = ?", (missing_id,)
            ).fetchone()[0],
            0,
        )
        canonical = self.conn.execute(
            "SELECT is_active, variant_count FROM canonical_opportunities WHERE id = ?",
            (missing_canonical_id,),
        ).fetchone()
        self.assertEqual((canonical["is_active"], canonical["variant_count"]), (0, 0))

        active = next(
            dict(row)
            for row in matcher.get_active_rows(self.conn)
            if row["title"] == "Python Expert"
        )
        self.assertEqual(active["source_run_id"], run["id"])
        trust = assess_opportunity_trust(
            active,
            "not_applicable",
            now=datetime.now(timezone.utc),
        )
        self.assertEqual(trust.status, TRUSTED)

    def test_partial_does_not_remove_or_renew_trust(self):
        observed_id = self.seed_job("job-a", "Python Expert")
        missing_id = self.seed_job("job-old", "Obsolete Specialist")
        sync_alignerr_canonical_opportunities(self.conn, 1)
        self.conn.execute(
            """
            INSERT INTO crawl_runs (
              id, company_id, status, started_at, finished_at, used_sample_data
            ) VALUES (50, 1, 'success', ?, ?, 0)
            """,
            (SEED_TIME, SEED_TIME),
        )
        self.conn.commit()
        partial = self.provider_result("premature_short_page")

        with self.pipeline_context(lambda _url: partial):
            _, summary = pipeline.run_crawl("alignerr")

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

    def test_contract_drift_and_transport_failure_preserve_last_snapshot(self):
        self.seed_job("job-a", "Python Expert")
        sync_alignerr_canonical_opportunities(self.conn, 1)
        self.conn.commit()
        before = self.state_snapshot()
        drift = self.provider_result("unknown_envelope")

        with self.pipeline_context(lambda _url: drift):
            pipeline.run_crawl("alignerr")
        self.assertEqual(self.latest_run()["status"], "contract_drift")
        self.assertEqual(self.state_snapshot(), before)

        def fail(_url):
            raise RuntimeError("transport failed")

        with self.pipeline_context(fail), self.assertRaisesRegex(
            RuntimeError, "transport failed"
        ):
            pipeline.run_crawl("alignerr")
        self.assertEqual(self.latest_run()["status"], "failed")
        self.assertEqual(self.state_snapshot(), before)

    def test_recovery_from_partial_and_drift_to_complete_snapshot(self):
        self.seed_job("job-a", "Python Expert")
        missing_id = self.seed_job("job-old", "Obsolete Specialist")
        sync_alignerr_canonical_opportunities(self.conn, 1)
        self.conn.commit()

        partial = self.provider_result("premature_short_page")
        with self.pipeline_context(lambda _url: partial):
            pipeline.run_crawl("alignerr")
        self.assertEqual(
            self.conn.execute(
                "SELECT is_active FROM jobs WHERE id = ?", (missing_id,)
            ).fetchone()[0],
            1,
        )

        drift = self.provider_result("unknown_envelope")
        with self.pipeline_context(lambda _url: drift):
            pipeline.run_crawl("alignerr")
        self.assertEqual(
            self.conn.execute(
                "SELECT is_active FROM jobs WHERE id = ?", (missing_id,)
            ).fetchone()[0],
            1,
        )

        complete = self.provider_result("v2_single_page")
        with self.pipeline_context(lambda _url: complete):
            _, summary = pipeline.run_crawl("alignerr")
        self.assertEqual(self.latest_run()["status"], "success")
        self.assertEqual(summary.jobs_removed, 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT is_active FROM jobs WHERE id = ?", (missing_id,)
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
