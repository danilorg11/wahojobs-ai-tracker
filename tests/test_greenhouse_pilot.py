import argparse
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import scripts.greenhouse_pilot as pilot_script
from wahojobs.crawler import greenhouse_pilot
from wahojobs.crawler import pipeline
from wahojobs.crawler.providers import greenhouse
from wahojobs.crawler.source_registry import load_source_registry
from wahojobs.crawler.types import ProviderOutcome, evaluate_removal_authorization


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "greenhouse_provider_contract.json"
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "wahojobs" / "db" / "schema.sql"


class GreenhousePilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        cls.entries = {entry.company_id: entry for entry in load_source_registry()}

    def result_for(self, company_id, payload=None):
        entry = self.entries[company_id]
        payload = copy.deepcopy(payload or self.payload(entry.board_identifier))
        responses = [payload]
        if entry.root_department_id is not None:
            responses.append(copy.deepcopy(self.fixtures["empty_department_hierarchy"]))
        with patch.object(greenhouse, "request_json", side_effect=responses):
            return greenhouse.fetch_greenhouse_snapshot(entry.greenhouse_config())

    def payload(self, token, *, future_role=False, count=3, location=None):
        titles = [
            "QA Analyst (Future Roles)" if future_role else "Support Coordinator",
            "Data Quality Analyst",
            "Software Test Engineer",
        ]
        jobs = []
        for offset in range(1, count + 1):
            title = titles[(offset - 1) % len(titles)]
            if count > len(titles):
                title = f"{title} {offset}"
            job_id = 7000 + offset
            jobs.append(
                {
                    "id": job_id,
                    "title": title,
                    "absolute_url": f"https://job-boards.greenhouse.io/{token}/jobs/{job_id}",
                    "location": {"name": location or ("Remote EMEA" if offset == 1 else "Global")},
                    "updated_at": f"2026-07-{10 + ((offset - 1) % 9):02d}T12:00:00-04:00",
                    "content": f"<p>Description for {title}</p>",
                    "internal_job_id": 9000 + offset,
                    "requisition_id": f"REQ-{offset}",
                    "first_published": "2026-07-01T12:00:00-04:00",
                    "language": "en",
                    "metadata": [{"name": "Team", "value": "Operations"}],
                    "education": "education_optional",
                    "data_compliance": [{"type": "gdpr", "requires_consent": False}],
                    "departments": [
                        {
                            "id": 50,
                            "name": "Operations",
                            "child_ids": [],
                            "parent_id": None,
                        }
                    ],
                    "offices": [
                        {
                            "id": 60,
                            "name": "Remote",
                            "location": "Distributed",
                            "child_ids": [],
                            "parent_id": None,
                        }
                    ],
                }
            )
        return {"jobs": jobs, "meta": {"total": len(jobs)}}

    def test_complete_snapshot_preserves_rich_scoped_source_records(self):
        result = self.result_for("gitlab")

        self.assertEqual(result.outcome, ProviderOutcome.SUCCESS)
        self.assertEqual(len(result.source_records), 3)
        record = result.source_records[0]
        self.assertEqual(record.source_name, "GitLab")
        self.assertEqual(record.company_id, "gitlab")
        self.assertEqual(record.board_token, "gitlab")
        self.assertIn("Description for", record.description_html)
        self.assertEqual(record.internal_job_id, 9001)
        self.assertEqual(record.requisition_id, "REQ-1")
        self.assertEqual(json.loads(record.metadata_json)[0]["value"], "Operations")
        dumped = json.loads(greenhouse_pilot.dump_source_records(result))
        self.assertEqual(dumped[0]["raw_public_payload"]["language"], "en")

    def test_temporary_lifecycle_closes_only_after_complete_and_isolates_sources(self):
        gitlab = self.result_for("gitlab")
        customerio = self.result_for("customerio")
        entries = (self.entries["gitlab"], self.entries["customerio"])
        report = greenhouse_pilot.run_temporary_lifecycle_probe(
            entries,
            {
                "greenhouse_gitlab": gitlab,
                "greenhouse_customerio": customerio,
            },
        )

        for registry_id in ("greenhouse_gitlab", "greenhouse_customerio"):
            row = report[registry_id]
            self.assertEqual(row["partial_removed"], 0)
            self.assertEqual(row["complete_removed"], 1)
            self.assertTrue(row["other_sources_unchanged"])
            self.assertTrue(row["closure_safe"])

    def test_partial_empty_duplicate_and_unsafe_snapshots_never_remove_prior_rows(self):
        entry = self.entries["gitlab"]
        complete = self.result_for("gitlab")
        duplicate = copy.deepcopy(self.payload("gitlab"))
        duplicate["jobs"][1] = copy.deepcopy(duplicate["jobs"][0])
        unsafe = copy.deepcopy(self.payload("gitlab"))
        unsafe["jobs"][0]["absolute_url"] = "https://job-boards.greenhouse.io/gitlab"
        cross_board = copy.deepcopy(self.payload("gitlab"))
        cross_board["jobs"][0]["absolute_url"] = (
            "https://job-boards.greenhouse.io/customerio/jobs/7001"
        )
        malformed = copy.deepcopy(self.payload("gitlab"))
        malformed["jobs"][0].pop("title")
        anomalies = [
            greenhouse_pilot.derived_snapshot(complete, remove_count=1, complete=False),
            self.result_for("gitlab", {"jobs": [], "meta": {"total": 0}}),
            self.result_for("gitlab", duplicate),
            self.result_for("gitlab", unsafe),
            self.result_for("gitlab", cross_board),
            self.result_for("gitlab", malformed),
            self.result_for("gitlab", {"error": "Board unavailable"}),
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            conn = sqlite3.connect(Path(temp_dir) / "pilot.sqlite")
            conn.row_factory = sqlite3.Row
            try:
                conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
                company_id = greenhouse_pilot.seed_temporary_companies(conn, (entry,))[
                    entry.registry_id
                ]
                greenhouse_pilot.track_snapshot(conn, company_id, complete, 1)
                for sequence, anomaly in enumerate(anomalies, start=2):
                    self.assertFalse(evaluate_removal_authorization(anomaly).authorized)
                    summary = greenhouse_pilot.track_snapshot(conn, company_id, anomaly, sequence)
                    self.assertEqual(summary.jobs_removed, 0)
                    self.assertEqual(greenhouse_pilot.active_count(conn, company_id), 3)
            finally:
                conn.close()

    def test_count_drop_policy_blocks_catastrophic_nonempty_closures_and_allows_recovery(self):
        entry = self.entries["gitlab"]
        initial = self.result_for("gitlab", self.payload("gitlab", count=10))
        one = self.result_for("gitlab", self.payload("gitlab", count=1))
        forty = self.result_for("gitlab", self.payload("gitlab", count=40))
        eighty = self.result_for("gitlab", self.payload("gitlab", count=80))

        self.assertEqual(
            greenhouse_pilot.apply_count_drop_policy(
                entry, one, previous_accepted_count=10
            ).outcome,
            ProviderOutcome.ANOMALOUS,
        )
        self.assertEqual(
            greenhouse_pilot.apply_count_drop_policy(
                entry, forty, previous_accepted_count=100
            ).outcome,
            ProviderOutcome.ANOMALOUS,
        )
        self.assertEqual(
            greenhouse_pilot.apply_count_drop_policy(
                entry, eighty, previous_accepted_count=100
            ).outcome,
            ProviderOutcome.SUCCESS,
        )
        self.assertEqual(
            greenhouse_pilot.apply_count_drop_policy(entry, one).outcome,
            ProviderOutcome.SUCCESS,
        )

        anomalous = greenhouse_pilot.apply_count_drop_policy(
            entry, one, previous_accepted_count=10
        )
        recovery = self.result_for("gitlab", self.payload("gitlab", count=8))
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = sqlite3.connect(Path(temp_dir) / "count-drop.sqlite")
            conn.row_factory = sqlite3.Row
            try:
                conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
                company_id = greenhouse_pilot.seed_temporary_companies(conn, (entry,))[
                    entry.registry_id
                ]
                greenhouse_pilot.track_snapshot(conn, company_id, initial, 1)
                anomaly_summary = greenhouse_pilot.track_snapshot(conn, company_id, anomalous, 2)
                self.assertEqual(anomaly_summary.jobs_removed, 0)
                self.assertEqual(greenhouse_pilot.active_count(conn, company_id), 10)
                recovery_summary = greenhouse_pilot.track_snapshot(conn, company_id, recovery, 3)
                self.assertEqual(recovery_summary.jobs_removed, 2)
                self.assertEqual(greenhouse_pilot.active_count(conn, company_id), 8)
            finally:
                conn.close()

    def test_meridial_ordinary_pipeline_uses_last_successful_snapshot_count(self):
        initial = self.result_for("meridial", self.payload("agency", count=10))
        one = self.result_for("meridial", self.payload("agency", count=1))
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = sqlite3.connect(Path(temp_dir) / "pipeline-count-drop.sqlite")
            conn.row_factory = sqlite3.Row
            try:
                conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
                conn.execute(
                    """
                    INSERT INTO companies (
                      name, slug, careers_url, source_tier, inventory_model,
                      market_count_policy
                    ) VALUES (
                      'Meridial', 'meridial', 'https://job-boards.greenhouse.io/agency',
                      'core', 'live_feed', 'count_live'
                    )
                    """
                )
                conn.commit()
                with (
                    patch.object(pipeline, "get_connection", return_value=conn),
                    patch.dict(
                        pipeline.CRAWLERS,
                        {"meridial": Mock(side_effect=(initial, one))},
                    ),
                ):
                    pipeline.run_crawl("meridial")
                    _company, second = pipeline.run_crawl("meridial")
                self.assertEqual(second.provider_outcome, ProviderOutcome.ANOMALOUS)
                self.assertEqual(second.jobs_removed, 0)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM jobs WHERE is_active = 1").fetchone()[0],
                    10,
                )
                statuses = [
                    row[0]
                    for row in conn.execute("SELECT status FROM crawl_runs ORDER BY id")
                ]
                self.assertEqual(statuses, ["success", "partial"])
            finally:
                conn.close()

    def test_repeated_fetches_do_not_fabricate_historical_readiness(self):
        entry = self.entries["gitlab"]
        complete = self.result_for("gitlab")
        with patch.object(
            greenhouse_pilot,
            "fetch_greenhouse_snapshot",
            return_value=complete,
        ):
            pass
        metrics = greenhouse_pilot.snapshot_metrics(
            entry,
            (complete, complete, complete),
            coverage_has_new_leakage=False,
        )
        self.assertEqual(metrics["technical_status"]["fetch_attempts_this_invocation"], 3)
        self.assertEqual(
            metrics["technical_status"]["readiness_observations_recorded_this_invocation"],
            0,
        )
        self.assertEqual(metrics["enablement"]["historical_readiness_streak"], 0)
        self.assertIn(
            "three_consecutive_complete_snapshots_not_recorded",
            metrics["enablement"]["production_readiness_failures"],
        )

    def test_metrics_distinguish_identity_title_repetition_and_canonical_yield(self):
        entry = self.entries["testlio"]
        result = self.result_for("testlio", self.payload("testlio", future_role=True))
        metrics = greenhouse_pilot.snapshot_metrics(
            entry,
            (result,),
            relevant_external_ids=("7001",),
            coverage_has_new_leakage=False,
        )

        self.assertEqual(metrics["raw_record_count"], 3)
        self.assertEqual(metrics["accepted_source_record_count"], 3)
        self.assertEqual(metrics["relevant_posting_count"], 1)
        self.assertEqual(
            metrics["relevant_posting_count_status"], "measured_persona_admission"
        )
        self.assertEqual(metrics["stable_identity_count"], 3)
        self.assertEqual(metrics["stable_identity_rate"], 1.0)
        self.assertEqual(metrics["exact_title_unique_count"], 3)
        self.assertEqual(metrics["normalized_title_unique_count"], 3)
        self.assertEqual(metrics["normalized_title_repetition_rate"], 0.0)
        self.assertIsNone(metrics["canonical_count"])
        self.assertIsNone(metrics["canonical_yield"])
        self.assertEqual(metrics["canonical_yield_status"], "unmeasured")
        self.assertEqual(metrics["application_model"]["future_role_count"], 1)
        self.assertFalse(metrics["enablement"]["production_ready"])

    def test_meridial_canonical_yield_is_measured_only_in_temporary_database(self):
        entry = self.entries["meridial"]
        result = self.result_for("meridial")
        measured = greenhouse_pilot.run_temporary_canonicalization_probe(
            (entry,), {entry.registry_id: result}
        )[entry.registry_id]
        self.assertEqual(measured["canonical_yield_status"], "measured_temporary_database")
        self.assertIsInstance(measured["canonical_count"], int)
        self.assertTrue(measured["canonicalization_input_fingerprint"].startswith("sha256:"))
        self.assertTrue(measured["canonicalization_output_fingerprint"].startswith("sha256:"))

    def test_matcher_rows_keep_regions_constrained_and_future_roles_separate(self):
        entry = self.entries["testlio"]
        result = self.result_for("testlio", self.payload("testlio", future_role=True))
        rows = pilot_script.rows_from_snapshots(
            (entry,),
            {entry.registry_id: result},
            evaluated_at=pilot_script.parse_evaluated_at("2026-07-16T12:00:00Z"),
        )

        regional = next(row for row in rows if row["external_id"] == "7001")
        self.assertEqual(regional["location"], "Remote EMEA")
        self.assertEqual(regional["applicant_location_requirements"], "Remote EMEA")
        self.assertEqual(regional["market_count_policy"], "report_separately")
        self.assertEqual(regional["opportunity_kind"], "application_portal")
        self.assertEqual(regional["include_in_live_market_estimate"], 0)
        self.assertEqual(rows[1]["market_count_policy"], "count_live")

    def test_testlio_airport_projects_remain_local_field_work(self):
        entry = self.entries["testlio"]
        payload = self.payload("testlio", count=1, location="San Francisco, United States")
        payload["jobs"][0]["title"] = "Airport Device Testing Project"
        payload["jobs"][0]["content"] = "<p>On-site field testing at the airport.</p>"
        result = self.result_for("testlio", payload)
        row = pilot_script.rows_from_snapshots(
            (entry,),
            {entry.registry_id: result},
            evaluated_at=pilot_script.parse_evaluated_at("2026-07-16T12:00:00Z"),
        )[0]
        self.assertEqual(row["opportunity_kind"], "local_field_project")
        self.assertEqual(row["market_count_policy"], "report_separately")
        self.assertEqual(row["include_in_live_market_estimate"], 0)

    def test_persona_comparison_uses_full_baseline_and_reports_every_board(self):
        entries = tuple(
            self.entries[company_id]
            for company_id in ("meridial", "gitlab", "customerio", "testlio")
        )
        results = {entry.registry_id: self.result_for(entry.company_id) for entry in entries}
        control_rows = pilot_script.rows_from_snapshots(
            (self.entries["meridial"],),
            {"greenhouse_meridial": results["greenhouse_meridial"]},
            evaluated_at=pilot_script.parse_evaluated_at("2026-07-16T12:00:00Z"),
        )
        report = pilot_script.compare_persona_coverage(
            entries,
            results,
            evaluated_at=pilot_script.parse_evaluated_at("2026-07-16T12:00:00Z"),
            baseline_rows=control_rows,
        )

        self.assertEqual(report["persona_count"], 28)
        self.assertEqual(
            report["comparison_basis"],
            "full_read_only_product_inventory_vs_each_pilot_and_combined",
        )
        self.assertEqual(
            set(report["per_board"]),
            {"greenhouse_gitlab", "greenhouse_customerio", "greenhouse_testlio"},
        )
        self.assertEqual(len(report["persona_deltas"]), 28)
        for row in report["persona_deltas"]:
            self.assertEqual(set(row["per_board_result_delta"]), set(report["per_board"]))
            self.assertIn("region_rejected_count", row)
            self.assertIn("region_rejected_delta", row)
        self.assertEqual(
            set(report["relevant_external_ids_by_registry"]),
            {entry.registry_id for entry in entries},
        )
        self.assertEqual(report["new_eligibility_leakage_count"], 0)
        self.assertFalse(report["matching_or_ranking_changed"])

    def test_cli_exit_codes_distinguish_technical_success_readiness_and_config(self):
        entry = self.entries["gitlab"]
        complete = self.result_for("gitlab")
        partial = greenhouse_pilot.derived_snapshot(complete, remove_count=1, complete=False)

        with self.cli_args(), patch.object(
            pilot_script, "fetch_snapshot_sequence", return_value=(complete,)
        ):
            self.assertEqual(pilot_script.main(), 0)
        with self.cli_args(), patch.object(
            pilot_script, "fetch_snapshot_sequence", return_value=(partial,)
        ):
            self.assertEqual(pilot_script.main(), 1)
        anomalous = greenhouse_pilot.apply_count_drop_policy(
            entry,
            self.result_for("gitlab", self.payload("gitlab", count=1)),
            previous_accepted_count=10,
        )
        with self.cli_args(), patch.object(
            pilot_script, "fetch_snapshot_sequence", return_value=(anomalous,)
        ):
            self.assertEqual(pilot_script.main(), 1)
        contract_invalid = self.result_for(
            "gitlab", {"status": 404, "message": "Board not found"}
        )
        with self.cli_args(), patch.object(
            pilot_script, "fetch_snapshot_sequence", return_value=(contract_invalid,)
        ):
            self.assertEqual(pilot_script.main(), 1)
        with self.cli_args(require_production_ready=True), patch.object(
            pilot_script, "fetch_snapshot_sequence", return_value=(complete,)
        ):
            self.assertEqual(pilot_script.main(), 1)

        with tempfile.TemporaryDirectory() as temp_dir:
            invalid = Path(temp_dir) / "invalid.json"
            invalid.write_text('{"registry_version": 999, "sources": []}', encoding="utf-8")
            with self.cli_args(registry=invalid):
                self.assertEqual(pilot_script.main(), 2)

    def test_human_and_json_outputs_share_the_same_explicit_status_contract(self):
        entry = self.entries["gitlab"]
        result = self.result_for("gitlab")
        source = greenhouse_pilot.snapshot_metrics(entry, (result,))
        report = {
            "evaluated_at": "2026-07-16T12:00:00+00:00",
            "technical_dry_run_passed": True,
            "production_enablement_changed": False,
            "sources": [source],
            "persona_coverage": None,
        }

        human = pilot_script.render_human_report(report)
        self.assertIn("Technical dry run passed: yes", human)
        self.assertIn("connector technically valid: yes", human)
        self.assertIn("product enabled: no", human)
        self.assertIn("production crawl enabled: no", human)
        self.assertIn("company terms status:", human)
        self.assertIn("raw / accepted / relevant: 3 / 3 / unmeasured", human)

        output = io.StringIO()
        with redirect_stdout(output):
            pilot_script.emit_report(report, "json")
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["technical_dry_run_passed"])
        self.assertFalse(parsed["sources"][0]["enablement"]["product_enabled"])
        self.assertFalse(
            parsed["sources"][0]["enablement"]["production_crawl_enabled"]
        )

    def cli_args(self, *, registry=None, require_production_ready=False):
        args = argparse.Namespace(
            registry=registry or Path("wahojobs/crawler/source_registry.json"),
            boards=["greenhouse_gitlab"],
            snapshots=1,
            lifecycle_probe=False,
            skip_coverage=True,
            evaluated_at="2026-07-16T12:00:00Z",
            require_production_ready=require_production_ready,
            baseline_db=Path("unused.sqlite"),
        )
        return patch.object(pilot_script, "parse_args", return_value=args)

    def test_disabled_invisible_cannot_be_selected_for_dry_run(self):
        with self.assertRaises(SystemExit):
            pilot_script.select_entries(
                tuple(self.entries.values()),
                ["greenhouse_invisible"],
            )


if __name__ == "__main__":
    unittest.main()
