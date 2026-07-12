import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import scripts.profile_match_digest as matcher
import wahojobs.crawler.pipeline as pipeline
import wahojobs.tracking.service as tracking_service
from wahojobs.canonical.service import sync_micro1_canonical_opportunities
from wahojobs.crawler.types import CompanyCrawlResult, JobCandidate, ProviderOutcome
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import create_crawl_run, insert_job
from wahojobs.tracking.normalize import with_source_hash
from wahojobs.tracking.service import track_crawl_result


NOW = "2026-07-11T12:00:00+00:00"


def candidate(external_id, title=None, location="Remote"):
    return JobCandidate(
        external_id=external_id,
        title=title or f"Role {external_id}",
        location=location,
        url=f"https://example.test/{external_id}",
        department="Generalist",
        expertise="Generalist",
    )


def result(
    jobs,
    *,
    outcome=ProviderOutcome.SUCCESS,
    snapshot_complete=True,
    pagination_complete=True,
    sample=False,
    empty_validated=False,
):
    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=sample,
        source_message="fixture result",
        source_type="fixture",
        outcome=outcome,
        snapshot_complete=snapshot_complete,
        pagination_complete=pagination_complete,
        empty_snapshot_validated=empty_validated,
        payload_shape="fixture:v1",
        raw_record_count=len(jobs),
        normalized_record_count=len(jobs),
        rejected_record_count=0,
        schema_fingerprint="fixture-v1",
    )


class LifecycleSnapshotSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "source-reliability.sqlite"
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
              1, 'micro1 fixture', 'micro1', 'https://example.test/api',
              'core', 'live_feed', 'count_live'
            )
            """
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def seed_job(self, external_id, *, title=None, active=True):
        item = with_source_hash("micro1", candidate(external_id, title))
        job_id = insert_job(self.conn, 1, item, NOW)
        if not active:
            self.conn.execute(
                "UPDATE jobs SET is_active = 0, removed_at = ? WHERE id = ?",
                (NOW, job_id),
            )
        return job_id

    def add_run(self):
        run_id = create_crawl_run(self.conn, 1, NOW)
        self.conn.commit()
        return run_id

    def latest_run(self):
        return self.conn.execute(
            "SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def pipeline_patches(self, crawler):
        @contextmanager
        def temporary_connection():
            conn = get_connection(self.db_path)
            try:
                yield conn
            finally:
                conn.close()

        return (
            patch.object(pipeline, "get_connection", temporary_connection),
            patch.dict(pipeline.CRAWLERS, {"micro1": crawler}),
        )

    def test_authoritative_snapshot_updates_reactivates_and_removes(self):
        observed_id = self.seed_job("observed", title="Old observed title")
        missing_id = self.seed_job("missing")
        reactivated_id = self.seed_job("reactivated", active=False)
        run_id = self.add_run()

        summary = track_crawl_result(
            self.conn,
            1,
            run_id,
            result(
                [
                    candidate("observed", "Updated observed title"),
                    candidate("reactivated"),
                ]
            ),
            NOW,
        )

        self.assertTrue(summary.removals_authorized)
        self.assertEqual(summary.jobs_reactivated, 1)
        self.assertEqual(summary.jobs_removed, 1)
        states = {
            row["id"]: (row["is_active"], row["title"])
            for row in self.conn.execute("SELECT id, is_active, title FROM jobs")
        }
        self.assertEqual(states[observed_id], (1, "Updated observed title"))
        self.assertEqual(states[missing_id][0], 0)
        self.assertEqual(states[reactivated_id][0], 1)

    def test_partial_sample_and_legacy_results_preserve_missing_jobs(self):
        for label, crawl_result in (
            (
                "partial",
                result(
                    [candidate("observed")],
                    outcome=ProviderOutcome.PARTIAL,
                    snapshot_complete=False,
                    pagination_complete=False,
                ),
            ),
            ("sample", result([candidate("observed")], sample=True)),
            (
                "legacy",
                CompanyCrawlResult(
                    [candidate("observed")], False, "legacy", "fixture"
                ),
            ),
        ):
            with self.subTest(label=label):
                self.conn.execute("DELETE FROM job_events")
                self.conn.execute("DELETE FROM jobs")
                self.conn.commit()
                self.seed_job("observed", title="Old title")
                missing_id = self.seed_job("missing")
                run_id = self.add_run()
                summary = track_crawl_result(
                    self.conn,
                    1,
                    run_id,
                    crawl_result,
                    NOW,
                )
                missing = self.conn.execute(
                    "SELECT is_active FROM jobs WHERE id = ?", (missing_id,)
                ).fetchone()
                self.assertFalse(summary.removals_authorized)
                self.assertEqual(missing["is_active"], 1)

    def test_contract_drift_performs_no_lifecycle_or_canonical_writes(self):
        job_id = self.seed_job("existing", title="Original")
        sync_micro1_canonical_opportunities(self.conn, 1)
        self.conn.commit()
        before_jobs = self.conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        before_canonical = self.conn.execute(
            "SELECT COUNT(*) AS count FROM canonical_opportunities"
        ).fetchone()["count"]
        run_id = self.add_run()

        summary = track_crawl_result(
            self.conn,
            1,
            run_id,
            result(
                [candidate("new")],
                outcome=ProviderOutcome.CONTRACT_DRIFT,
                snapshot_complete=False,
                pagination_complete=False,
            ),
            NOW,
        )

        after_jobs = self.conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        self.assertEqual(dict(before_jobs), dict(after_jobs))
        self.assertEqual(summary.jobs_found, 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM canonical_opportunities").fetchone()[0],
            before_canonical,
        )

    def test_empty_snapshot_only_removes_when_explicitly_validated(self):
        self.seed_job("existing")
        run_id = self.add_run()
        unvalidated = track_crawl_result(
            self.conn,
            1,
            run_id,
            result([], empty_validated=False),
            NOW,
        )
        self.assertFalse(unvalidated.removals_authorized)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM jobs WHERE is_active = 1").fetchone()[0],
            1,
        )

        validated_run = self.add_run()
        validated = track_crawl_result(
            self.conn,
            1,
            validated_run,
            result([], empty_validated=True),
            NOW,
        )
        self.assertTrue(validated.removals_authorized)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM jobs WHERE is_active = 1").fetchone()[0],
            0,
        )

    def test_run_statuses_only_authoritative_or_sample_use_success(self):
        cases = (
            ("authoritative", result([candidate("authoritative")]), "success", 0),
            (
                "partial",
                result(
                    [candidate("partial")],
                    outcome=ProviderOutcome.PARTIAL,
                    snapshot_complete=False,
                    pagination_complete=False,
                ),
                "partial",
                0,
            ),
            (
                "contract_drift",
                result(
                    [candidate("drift")],
                    outcome=ProviderOutcome.CONTRACT_DRIFT,
                    snapshot_complete=False,
                    pagination_complete=False,
                ),
                "contract_drift",
                0,
            ),
            ("sample", result([candidate("sample")], sample=True), "success", 1),
        )
        for label, crawl_result, expected_status, expected_sample in cases:
            with self.subTest(label=label):
                first, second = self.pipeline_patches(lambda _: crawl_result)
                with first, second:
                    pipeline.run_crawl("micro1")
                row = self.latest_run()
                self.assertEqual(row["status"], expected_status)
                self.assertEqual(row["used_sample_data"], expected_sample)

        first, second = self.pipeline_patches(
            lambda _: (_ for _ in ()).throw(RuntimeError("provider failed"))
        )
        with first, second, self.assertRaisesRegex(RuntimeError, "provider failed"):
            pipeline.run_crawl("micro1")
        self.assertEqual(self.latest_run()["status"], "failed")

        rows = matcher.get_active_rows(self.conn)
        authoritative_row = next(
            row for row in rows if row["title"] == "Role authoritative"
        )
        qualifying_run = self.conn.execute(
            """
            SELECT id FROM crawl_runs
            WHERE status = 'success' AND used_sample_data = 0
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        self.assertEqual(authoritative_row["source_run_id"], qualifying_run["id"])

    def test_tracking_exception_rolls_back_all_lifecycle_changes(self):
        self.seed_job("existing")
        self.conn.commit()
        crawl_result = result([candidate("new")])
        original_insert = tracking_service.insert_job

        def insert_then_fail(conn, company_id, item, now):
            original_insert(conn, company_id, item, now)
            raise RuntimeError("injected tracking failure")

        first, second = self.pipeline_patches(lambda _: crawl_result)
        with (
            first,
            second,
            patch.object(tracking_service, "insert_job", insert_then_fail),
            self.assertRaisesRegex(RuntimeError, "injected tracking failure"),
        ):
            pipeline.run_crawl("micro1")

        self.assertEqual(self.latest_run()["status"], "failed")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0], 0
        )

    def test_canonical_exception_rolls_back_raw_and_canonical_changes(self):
        self.seed_job("observed", title="Original title")
        self.seed_job("missing")
        sync_micro1_canonical_opportunities(self.conn, 1)
        self.conn.commit()
        before_jobs = [
            dict(row)
            for row in self.conn.execute("SELECT * FROM jobs ORDER BY id")
        ]
        before_canonical = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM canonical_opportunities ORDER BY id"
            )
        ]
        crawl_result = result(
            [candidate("observed", "Changed title"), candidate("new")]
        )
        original_sync = tracking_service.sync_micro1_canonical_opportunities

        def sync_then_fail(conn, company_id):
            original_sync(conn, company_id)
            raise RuntimeError("injected canonical failure")

        first, second = self.pipeline_patches(lambda _: crawl_result)
        with (
            first,
            second,
            patch.object(
                tracking_service,
                "sync_micro1_canonical_opportunities",
                sync_then_fail,
            ),
            self.assertRaisesRegex(RuntimeError, "injected canonical failure"),
        ):
            pipeline.run_crawl("micro1")

        self.assertEqual(self.latest_run()["status"], "failed")
        self.assertEqual(
            [dict(row) for row in self.conn.execute("SELECT * FROM jobs ORDER BY id")],
            before_jobs,
        )
        self.assertEqual(
            [
                dict(row)
                for row in self.conn.execute(
                    "SELECT * FROM canonical_opportunities ORDER BY id"
                )
            ],
            before_canonical,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0], 0
        )

    def test_canonical_rollup_exception_rolls_back_raw_and_canonical_changes(self):
        self.seed_job("observed", title="Original title")
        sync_micro1_canonical_opportunities(self.conn, 1)
        self.conn.commit()
        before_jobs = [
            dict(row) for row in self.conn.execute("SELECT * FROM jobs ORDER BY id")
        ]
        before_canonical = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM canonical_opportunities ORDER BY id"
            )
        ]
        crawl_result = result(
            [candidate("observed", "Changed title"), candidate("new")]
        )
        first, second = self.pipeline_patches(lambda _: crawl_result)

        with (
            first,
            second,
            patch(
                "wahojobs.canonical.service.refresh_canonical_rollups",
                side_effect=RuntimeError("injected rollup failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "injected rollup failure"),
        ):
            pipeline.run_crawl("micro1")

        self.assertEqual(self.latest_run()["status"], "failed")
        self.assertEqual(
            [dict(row) for row in self.conn.execute("SELECT * FROM jobs ORDER BY id")],
            before_jobs,
        )
        self.assertEqual(
            [
                dict(row)
                for row in self.conn.execute(
                    "SELECT * FROM canonical_opportunities ORDER BY id"
                )
            ],
            before_canonical,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0], 0
        )

    def test_partial_keeps_canonical_active_and_later_authoritative_run_recovers(self):
        observed_id = self.seed_job("observed")
        missing_id = self.seed_job("missing")
        sync_micro1_canonical_opportunities(self.conn, 1)
        self.conn.commit()

        partial_run = self.add_run()
        track_crawl_result(
            self.conn,
            1,
            partial_run,
            result(
                [candidate("observed")],
                outcome=ProviderOutcome.PARTIAL,
                snapshot_complete=False,
                pagination_complete=False,
            ),
            NOW,
        )
        sync_micro1_canonical_opportunities(self.conn, 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT is_active FROM jobs WHERE id = ?", (missing_id,)
            ).fetchone()[0],
            1,
        )
        self.assertTrue(
            all(
                row["is_active"] == 1
                for row in self.conn.execute(
                    "SELECT is_active FROM canonical_opportunities"
                )
            )
        )

        authoritative_run = self.add_run()
        summary = track_crawl_result(
            self.conn,
            1,
            authoritative_run,
            result([candidate("observed")]),
            NOW,
        )
        self.assertEqual(summary.jobs_removed, 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT is_active FROM jobs WHERE id = ?", (observed_id,)
            ).fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
