"""A population anomaly withholds closure without discarding exact positives."""
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from scripts.profile_match_digest import get_active_rows
from wahojobs.crawler import pipeline
from wahojobs.crawler.companies.mindrift import crawl_mindrift
from wahojobs.crawler.providers import workable_markdown as workable
from wahojobs.crawler.staged_observation import decode_result
from wahojobs.crawler.types import ProviderOutcome
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import install_base_schema, verify_job_source_acceptance_integrity
from wahojobs.matching.opportunity_trust import assess_opportunity_trust
from wahojobs.mindrift_observation import CONTRACT, COUNT_DROP_WARNING, ENDPOINT
from wahojobs.tracking.service import MindriftCountDropRejected

OLD = "2026-09-24T06:00:00+00:00"
NOW = "2026-09-26T06:00:00+00:00"


def row(identity, **changes):
    return dict(shortcode=identity, title="AI Reviewer " + identity,
        state="published", isInternal=False, department=["AI"],
        location={"country": "Brazil"}, remote=True,
        description="Evaluate model responses and describe factual errors.", **changes)


def snapshot(rows):
    with patch.object(workable, "verify_public_markdown_feeds"), \
            patch.object(workable, "fetch_all_api_rows", return_value=rows):
        return crawl_mindrift(ENDPOINT)


class MindriftPositiveObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "fixture.sqlite3"
        with closing(get_connection(self.db)) as conn:
            install_base_schema(conn)
            conn.execute("""INSERT INTO companies
                (name, slug, careers_url, source_tier, inventory_model, market_count_policy)
                VALUES ('Mindrift', 'mindrift', ?, 'core', 'live_feed', 'count_live')""", (ENDPOINT,))
            conn.commit()
        self.network = patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def publish(self, result, at):
        with patch.dict(pipeline.CRAWLERS, mindrift=lambda _: result), \
                patch.object(pipeline, "utc_now", return_value=at):
            return pipeline.run_crawl("mindrift", db_path=self.db)[1]

    def inventory(self):
        with closing(get_connection(self.db)) as conn:
            return {r["external_id"]: dict(r) for r in conn.execute("SELECT * FROM jobs")}

    def trust(self, at=NOW):
        with closing(get_connection(self.db)) as conn:
            identities = {r["id"]: r["external_id"] for r in conn.execute("SELECT id, external_id FROM jobs")}
            return {identities[r["job_id"]]: assess_opportunity_trust(dict(r), "compatible",
                now=datetime.fromisoformat(at)) for r in get_active_rows(conn)}

    def seed_legacy(self):
        original = snapshot([row(f"OLD{index}") for index in range(117)])
        # The installed inventory predates individual Workable attestations.
        original = replace(original, jobs=[replace(j, record_promotion_attestation=None)
            for j in original.jobs], payload_shape="", schema_fingerprint="")
        self.publish(original, OLD)

    def dropped(self):
        # Exact retained failure dimensions: 50 overlaps, 42 new, 67 omissions.
        return snapshot([row(f"OLD{index}") for index in range(50)]
            + [row(f"NEW{index}") for index in range(42)])

    def test_count_drop_publishes_exact_positives_without_renewing_or_closing_absences(self):
        self.seed_legacy()
        before = self.inventory()
        result = self.dropped()
        summary = self.publish(result, NOW)
        self.assertTrue(result.snapshot_complete)  # immutable captured evidence
        self.assertEqual(result.outcome, ProviderOutcome.SUCCESS)
        self.assertEqual(summary.provider_outcome, ProviderOutcome.PARTIAL)
        self.assertFalse(summary.snapshot_complete)
        self.assertTrue(summary.pagination_complete)
        self.assertFalse(summary.removals_authorized)
        self.assertEqual((summary.jobs_found, summary.jobs_new, summary.jobs_removed), (92, 42, 0))
        self.assertIn(COUNT_DROP_WARNING, summary.warnings)
        after = self.inventory()
        for index in range(50, 117):
            self.assertEqual(after[f"OLD{index}"], before[f"OLD{index}"])
        clocks = self.trust()
        self.assertEqual(clocks["OLD0"].latest_successful_source_run_at, NOW)
        self.assertEqual(clocks["NEW0"].latest_successful_source_run_at, NOW)
        self.assertEqual(clocks["OLD116"].latest_successful_source_run_at, OLD)
        later = self.trust("2026-09-27T06:00:01+00:00")
        self.assertEqual(later["OLD116"].status, "stale_source")
        self.assertEqual(later["OLD0"].status, "trusted")
        with closing(get_connection(self.db)) as conn:
            terminal = conn.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(terminal["status"], "partial")
            self.assertIn(COUNT_DROP_WARNING, terminal["error_message"])
            self.assertEqual(terminal["jobs_removed_count"], 0)
            verify_job_source_acceptance_integrity(conn, after["OLD0"]["id"])
            verify_job_source_acceptance_integrity(conn, after["NEW0"]["id"])

    def test_normal_complete_snapshot_retains_absence_authority(self):
        self.publish(snapshot([row("ONE"), row("TWO")]), OLD)
        summary = self.publish(snapshot([row("ONE")]), NOW)
        self.assertEqual(summary.provider_outcome, ProviderOutcome.SUCCESS)
        self.assertTrue(summary.removals_authorized)
        self.assertEqual(summary.jobs_removed, 1)
        self.assertEqual(self.inventory()["TWO"]["is_active"], 0)

    def test_old_unattested_count_drop_still_rejects_without_mutation(self):
        self.seed_legacy()
        before = self.inventory()
        result = self.dropped()
        result = replace(result, jobs=[replace(j, record_promotion_attestation=None) for j in result.jobs])
        with self.assertRaises(MindriftCountDropRejected):
            self.publish(result, NOW)
        self.assertEqual(self.inventory(), before)

    def test_tampered_public_identity_or_projection_rolls_back_all_positives(self):
        self.seed_legacy()
        before = self.inventory()
        result = self.dropped()
        candidate = result.jobs[-1]
        bad = replace(candidate, title="Unobserved title")
        with self.assertRaisesRegex(ValueError, "normalized candidate disagree"):
            self.publish(replace(result, jobs=[*result.jobs[:-1], bad]), NOW)
        self.assertEqual(self.inventory(), before)
        evidence = candidate.record_promotion_attestation.authority_evidence
        bad = replace(candidate, record_promotion_attestation=replace(
            candidate.record_promotion_attestation,
            authority_evidence=dict(evidence, record=dict(evidence["record"], isInternal=True))))
        with self.assertRaisesRegex(ValueError, "public published observation"):
            self.publish(replace(result, jobs=[*result.jobs[:-1], bad]), NOW)
        self.assertEqual(self.inventory(), before)

    def test_failed_terminal_cannot_supply_individual_freshness(self):
        self.seed_legacy()
        self.publish(self.dropped(), NOW)
        with closing(get_connection(self.db)) as conn:
            conn.execute("UPDATE crawl_runs SET status='failed' WHERE id=(SELECT max(id) FROM crawl_runs)")
            conn.commit()
        self.assertEqual(self.trust()["OLD0"].latest_successful_source_run_at, OLD)

    def test_attestation_survives_staged_codec_and_requires_explicit_public_status(self):
        from dataclasses import asdict
        result = snapshot([row("ONE")])
        self.assertEqual(decode_result(json.loads(json.dumps(asdict(result)))), result)
        self.assertEqual(result.jobs[0].record_promotion_attestation.contract_id, CONTRACT)
        missing = row("ONE")
        missing.pop("isInternal")
        with self.assertRaisesRegex(ValueError, "explicit public identity"):
            snapshot([missing])
        with patch.object(workable, "verify_public_markdown_feeds") as probe:
            with self.assertRaisesRegex(ValueError, "configured public Workable endpoint"):
                workable.fetch_workable_jobs("https://example.test", "toloka-ai")
            probe.assert_not_called()


class MindriftStagedReportingTests(unittest.TestCase):
    from tests.test_daily_source_coverage import CoverageIntegrationTests
    setUp = CoverageIntegrationTests.setUp
    publish = MindriftPositiveObservationTests.publish
    seed_legacy = MindriftPositiveObservationTests.seed_legacy
    dropped = MindriftPositiveObservationTests.dropped
    inventory = MindriftPositiveObservationTests.inventory

    def test_daily_staged_publication_and_reconstruction_keep_exact_positive_authority(self):
        from wahojobs import daily_inventory as daily
        from tests.test_daily_source_coverage import Transport, offline
        from tests.evidence_maintenance_support import BytesResponse
        self.seed_legacy()
        before = self.inventory()
        raw = [row(f"OLD{index}") for index in range(50)] + [row(f"NEW{index}") for index in range(42)]

        class MindriftTransport(Transport):
            def open(self, request, timeout):
                if request.get_method() == "POST":
                    self.calls.append(("mindrift", request.full_url, timeout))
                    return BytesResponse(json.dumps(dict(total=92, results=raw,
                        nextPage=None)).encode(), request.full_url)
                return super().open(request, timeout)

        for source, settings in self.config["sources"].items():
            settings["enabled"] = source == "mindrift"
        at = datetime.fromisoformat(NOW)
        target = self.root / "state/runs/mindrift-positives"
        with offline(at, MindriftTransport()):
            daily.collect_phase(self.config, "mindrift-positives", "prepare")
            daily.collect_phase(self.config, "mindrift-positives", "collect-mindrift")
            daily.collect_phase(self.config, "mindrift-positives", "backup")
            daily.collect_phase(self.config, "mindrift-positives", "publish-mindrift")
        stored = daily.read_json(target / "mindrift.json")
        database_before_reconstruction = self.db.read_bytes()
        with offline(at + timedelta(minutes=20), MindriftTransport()):
            reconstructed = daily.reconstruct_source_receipt(self.config,
                dict(run_id="mindrift-positives", started_at=NOW, ended_at=NOW, trigger="timer"), "mindrift")
        self.assertEqual(self.db.read_bytes(), database_before_reconstruction)
        for report in (stored, reconstructed):
            self.assertTrue(report["qualifying_observation"], report)
            self.assertEqual(report["outcome"], "partial_individual")
            self.assertEqual(report["last_qualifying_verification"], NOW)
            self.assertEqual((report["observed"], report["new"], report["confirmed_closed"]), (92, 42, 0))
            self.assertIn(COUNT_DROP_WARNING, report["coverage_warnings"])
            self.assertEqual(sorted((c["verified_at"], c["records"]) for c in report["cohorts"]),
                [(OLD, 67), (NOW, 92)])
        self.assertEqual(self.inventory()["OLD116"], before["OLD116"])


if __name__ == "__main__":
    unittest.main()
