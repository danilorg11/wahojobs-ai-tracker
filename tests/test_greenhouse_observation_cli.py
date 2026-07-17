from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import scripts.greenhouse_pilot as pilot_script
from wahojobs.crawler.providers.greenhouse import GreenhouseSourceRecord
from wahojobs.crawler.greenhouse_observations import (
    BUNDLES_DIR_NAME,
    ObservationClockError,
    RECEIPTS_DIR_NAME,
    WORKING_DIR_NAME,
)
from wahojobs.crawler.types import CompanyCrawlResult, JobCandidate, ProviderOutcome


class GreenhouseObservationCliTests(unittest.TestCase):
    def complete_snapshot(self):
        url = "https://job-boards.greenhouse.io/gitlab/jobs/7001"
        candidate = JobCandidate(
            title="Software Test Engineer",
            location="Remote EMEA",
            url=url,
            external_id="7001",
            department="Engineering",
            expertise="Software Engineering",
        )
        source_record = GreenhouseSourceRecord(
            source_name="GitLab",
            company_id="gitlab",
            board_token="gitlab",
            greenhouse_job_id=7001,
            external_id="7001",
            title=candidate.title,
            url=url,
            application_url=None,
            location=candidate.location,
            additional_locations=(),
            description_html="<p>Test software systems.</p>",
            updated_at="2026-07-17T12:00:00Z",
            internal_job_id=None,
            requisition_id=None,
            first_published=None,
            application_deadline=None,
            language="en",
            company_name="GitLab",
            metadata_json="[]",
            education_json="null",
            compliance_json="[]",
            compensation_json="null",
            raw_public_payload_json="{}",
            departments=(),
            offices=(),
        )
        return CompanyCrawlResult(
            jobs=[candidate],
            used_sample_data=False,
            source_message="synthetic complete snapshot",
            source_type="greenhouse_api",
            outcome=ProviderOutcome.SUCCESS,
            snapshot_complete=True,
            pagination_complete=True,
            empty_snapshot_validated=False,
            payload_shape="greenhouse_jobs_v1",
            raw_record_count=1,
            normalized_record_count=1,
            rejected_record_count=0,
            schema_fingerprint="sha256:" + "1" * 64,
            source_records=(source_record,),
        )

    def run_cli(self, arguments, *, fetch=None, fetch_error=None, output_format="json"):
        argv = ["greenhouse_pilot.py", *arguments, "--format", output_format]
        stdout = io.StringIO()
        patches = [
            patch.object(sys, "argv", argv),
            patch.object(pilot_script, "run_temporary_canonicalization_probe", return_value={}),
        ]
        if fetch is not None or fetch_error is not None:
            patches.append(
                patch.object(
                    pilot_script,
                    "fetch_snapshot_sequence",
                    return_value=(fetch,) if fetch_error is None else None,
                    side_effect=fetch_error,
                )
            )
        with patches[0], patches[1]:
            if len(patches) == 3:
                with patches[2], redirect_stdout(stdout):
                    result = pilot_script.main()
            else:
                with redirect_stdout(stdout):
                    result = pilot_script.main()
        return result, stdout.getvalue()

    def test_ordinary_dry_run_does_not_create_a_ledger(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "not-requested"
            result, output = self.run_cli(
                ["--board", "greenhouse_gitlab", "--skip-coverage"],
                fetch=self.complete_snapshot(),
            )
            self.assertEqual(result, 0)
            self.assertFalse(directory.exists())
            self.assertNotIn("observation_ledger", json.loads(output))

    def test_explicit_record_is_one_nonproduction_bundle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "ledger"
            result, output = self.run_cli(
                [
                    "--board",
                    "greenhouse_gitlab",
                    "--skip-coverage",
                    "--record-observation-dir",
                    str(directory),
                ],
                fetch=self.complete_snapshot(),
            )
            report = json.loads(output)
            self.assertEqual(result, 0)
            self.assertTrue(report["observation_ledger"]["recorded"])
            self.assertEqual(len(report["observation_ledger"]["observation_ids"]), 1)
            paths = list((directory / BUNDLES_DIR_NAME).glob("*.json"))
            receipts = list((directory / RECEIPTS_DIR_NAME).glob("*.json"))
            self.assertEqual(len(paths), 1)
            self.assertEqual(len(receipts), 1)
            bundle = json.loads(paths[0].read_text(encoding="utf-8"))
            observation = bundle["observations"][0]
            self.assertEqual(observation["registry_id"], "greenhouse_gitlab")
            self.assertFalse(observation["product_enabled"])
            self.assertFalse(observation["production_crawl_enabled"])
            self.assertNotIn("production_ready", observation)
            self.assertEqual(bundle["invocation_status"], "complete_success")

    def test_fetch_failure_records_one_failed_observation_without_details(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "ledger"
            result, output = self.run_cli(
                [
                    "--board",
                    "greenhouse_gitlab",
                    "--skip-coverage",
                    "--record-observation-dir",
                    str(directory),
                ],
                fetch_error=RuntimeError("private upstream diagnostic"),
            )
            report = json.loads(output)
            self.assertEqual(result, 1)
            self.assertTrue(report["observation_ledger"]["recorded"])
            path = next((directory / BUNDLES_DIR_NAME).glob("*.json"))
            persisted = path.read_text(encoding="utf-8")
            observation = json.loads(persisted)["observations"][0]
            self.assertFalse(observation["technical_success"])
            self.assertEqual(observation["snapshot_outcome"], "failed")
            self.assertEqual(observation["metrics_status"], "unmeasured")
            self.assertNotIn("private upstream diagnostic", persisted)

    def test_secret_bearing_failure_is_sanitized_in_json_human_and_bundle(self):
        secret_values = (
            "Bearer secret-token-123",
            "password=hunter2",
            "Cookie: session=private-cookie",
            "https://example.test/jobs?signed=private-signature",
            "C:\\private\\wahojobs.sqlite",
            "<html><body>private payload</body></html>",
        )
        raw = "\n".join(secret_values) + ("X" * 10000)
        private_type = type("Private\nFailure", (RuntimeError,), {})
        cause = ValueError("nested cause " + raw)
        failure = private_type(raw, {"password": "argument-secret"})
        failure.__cause__ = cause

        for output_format in ("json", "human"):
            with self.subTest(output_format=output_format), tempfile.TemporaryDirectory() as temp_dir:
                directory = Path(temp_dir) / "ledger"
                result, output = self.run_cli(
                    [
                        "--board",
                        "greenhouse_gitlab",
                        "--skip-coverage",
                        "--record-observation-dir",
                        str(directory),
                    ],
                    fetch_error=failure,
                    output_format=output_format,
                )
                self.assertEqual(result, 1)
                self.assertLess(len(output), 5000)
                for secret in (*secret_values, "argument-secret", "nested cause"):
                    self.assertNotIn(secret, output)
                persisted = next((directory / BUNDLES_DIR_NAME).glob("*.json")).read_text(
                    encoding="utf-8"
                )
                for secret in (*secret_values, "argument-secret", "nested cause"):
                    self.assertNotIn(secret, persisted)
                observation = json.loads(persisted)["observations"][0]
                self.assertEqual(
                    observation["operational_failure_reasons"],
                    ["unexpected_board_failure"],
                )
                if output_format == "json":
                    report = json.loads(output)
                    board = report["board_failures"]["greenhouse_gitlab"]
                    self.assertEqual(
                        set(board),
                        {
                            "registry_id",
                            "board_identifier",
                            "attempted",
                            "success",
                            "failure_code",
                            "safe_message",
                        },
                    )
                    self.assertEqual(board["failure_code"], "unexpected_board_failure")
                    self.assertEqual(board["safe_message"], "The board could not be processed.")

    def test_configuration_and_recording_errors_do_not_expose_exception_text(self):
        secret = "Bearer private-config-token " + ("Y" * 10000)
        with patch.object(
            pilot_script,
            "load_source_registry",
            side_effect=ValueError(secret),
        ):
            result, output = self.run_cli([])
        self.assertEqual(result, 2)
        self.assertNotIn(secret, output)
        report = json.loads(output)
        self.assertEqual(report["error"], "registry_or_configuration_invalid")
        self.assertEqual(
            report["safe_message"], "The pilot configuration is invalid."
        )

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            pilot_script,
            "record_observation_bundle",
            side_effect=pilot_script.ObservationLedgerError(secret),
        ):
            result, output = self.run_cli(
                [
                    "--board",
                    "greenhouse_gitlab",
                    "--skip-coverage",
                    "--record-observation-dir",
                    str(Path(temp_dir) / "ledger"),
                ],
                fetch=self.complete_snapshot(),
            )
        self.assertEqual(result, 2)
        self.assertNotIn(secret, output)
        report = json.loads(output)
        self.assertEqual(
            report["observation_ledger"]["failure_code"],
            "observation_recording_failed",
        )

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            pilot_script,
            "record_observation_bundle",
            side_effect=ObservationClockError(secret),
        ):
            result, output = self.run_cli(
                [
                    "--board",
                    "greenhouse_gitlab",
                    "--skip-coverage",
                    "--record-observation-dir",
                    str(Path(temp_dir) / "ledger"),
                ],
                fetch=self.complete_snapshot(),
            )
        self.assertEqual(result, 2)
        self.assertNotIn(secret, output)
        report = json.loads(output)
        self.assertEqual(
            report["observation_ledger"]["failure_code"],
            "ledger_clock_not_monotonic",
        )

    def test_verify_and_evaluate_are_read_only_and_never_fetch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "ledger"
            result, _output = self.run_cli(
                [
                    "--board",
                    "greenhouse_gitlab",
                    "--skip-coverage",
                    "--record-observation-dir",
                    str(directory),
                ],
                fetch=self.complete_snapshot(),
            )
            self.assertEqual(result, 0)
            paths = sorted(directory.rglob("*.json"))
            before = [(path, path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
            with patch.object(
                pilot_script,
                "fetch_snapshot_sequence",
                side_effect=AssertionError("history mode attempted a fetch"),
            ):
                verify_result, verify_output = self.run_cli(
                    ["--history-dir", str(directory), "--verify-history"]
                )
                evaluate_result, evaluate_output = self.run_cli(
                    [
                        "--history-dir",
                        str(directory),
                        "--evaluate-readiness",
                    ]
                )
            self.assertEqual(verify_result, 0)
            self.assertTrue(json.loads(verify_output)["valid"])
            self.assertEqual(evaluate_result, 0)
            readiness = json.loads(evaluate_output)
            self.assertEqual(
                readiness["boards"]["greenhouse_gitlab"]["observation_count"], 1
            )
            self.assertFalse(readiness["production_ready"])
            self.assertEqual(
                before,
                [(path, path.read_bytes(), path.stat().st_mtime_ns) for path in paths],
            )

    def test_invalid_history_is_nonzero_json_and_no_fetch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            (directory / "broken.json").write_text("{}\n", encoding="utf-8")
            with patch.object(
                pilot_script,
                "fetch_snapshot_sequence",
                side_effect=AssertionError("verification attempted a fetch"),
            ):
                result, output = self.run_cli(
                    ["--history-dir", str(directory), "--verify-history"]
                )
            report = json.loads(output)
            self.assertEqual(result, 1)
            self.assertFalse(report["valid"])
            self.assertTrue(report["errors"])

    def test_invalid_history_output_does_not_expose_artifact_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "ledger"
            result, _output = self.run_cli(
                [
                    "--board",
                    "greenhouse_gitlab",
                    "--skip-coverage",
                    "--record-observation-dir",
                    str(directory),
                ],
                fetch=self.complete_snapshot(),
            )
            self.assertEqual(result, 0)
            secret = "Bearer-private-token-password-cookie"
            (directory / BUNDLES_DIR_NAME / f"{secret}.json").write_text(
                "{}", encoding="utf-8"
            )
            for output_format in ("json", "human"):
                result, output = self.run_cli(
                    ["--history-dir", str(directory), "--verify-history"],
                    output_format=output_format,
                )
                self.assertEqual(result, 1)
                self.assertNotIn(secret, output)
                self.assertIn("observation_history_invalid", output)

    def test_human_and_json_history_outputs_share_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            json_result, json_output = self.run_cli(
                ["--history-dir", str(directory), "--verify-history"]
            )
            human_result, human_output = self.run_cli(
                ["--history-dir", str(directory), "--verify-history"],
                output_format="human",
            )
            self.assertEqual(json_result, human_result)
            self.assertTrue(json.loads(json_output)["valid"])
            self.assertIn("History valid: yes", human_output)
            self.assertIn("Bundles verified: 0", human_output)

    def test_human_and_json_history_outputs_report_working_residue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "ledger"
            result, _output = self.run_cli(
                [
                    "--board",
                    "greenhouse_gitlab",
                    "--skip-coverage",
                    "--record-observation-dir",
                    str(directory),
                ],
                fetch=self.complete_snapshot(),
            )
            self.assertEqual(result, 0)
            residue = directory / WORKING_DIR_NAME / (
                "staging_bundle_bundle_"
                + "1" * 32
                + "_"
                + "2" * 32
                + ".tmp"
            )
            residue.write_text("partial staging", encoding="utf-8")
            json_result, json_output = self.run_cli(
                ["--history-dir", str(directory), "--verify-history"]
            )
            human_result, human_output = self.run_cli(
                ["--history-dir", str(directory), "--verify-history"],
                output_format="human",
            )
            report = json.loads(json_output)
            self.assertEqual((json_result, human_result), (0, 0))
            self.assertTrue(report["valid"])
            self.assertEqual(report["working_residue_count"], 1)
            self.assertIn("Working residue count: 1", human_output)
            self.assertIn("Non-authoritative working residue", human_output)

    def test_cli_rejects_fabricated_recording_time_and_mixed_modes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "ledger"
            result, output = self.run_cli(
                [
                    "--record-observation-dir",
                    str(directory),
                    "--evaluated-at",
                    "2026-07-17T12:00:00Z",
                ]
            )
            self.assertEqual(result, 2)
            self.assertEqual(
                json.loads(output)["error"], "registry_or_configuration_invalid"
            )
            self.assertFalse(directory.exists())

    def test_partial_board_failure_preserves_successful_board_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "ledger"
            secret = "Bearer secret-board-token password=private"

            def fetch(entry, _snapshots):
                if entry.registry_id == "greenhouse_gitlab":
                    raise RuntimeError(secret)
                return (self.complete_snapshot(),)

            argv = [
                "greenhouse_pilot.py",
                "--board",
                "greenhouse_gitlab",
                "--board",
                "greenhouse_customerio",
                "--board",
                "greenhouse_testlio",
                "--skip-coverage",
                "--record-observation-dir",
                str(directory),
                "--format",
                "json",
            ]
            stdout = io.StringIO()
            with (
                patch.object(sys, "argv", argv),
                patch.object(pilot_script, "fetch_snapshot_sequence", side_effect=fetch),
                patch.object(
                    pilot_script, "run_temporary_canonicalization_probe", return_value={}
                ),
                redirect_stdout(stdout),
            ):
                result = pilot_script.main()
            report = json.loads(stdout.getvalue())
            self.assertEqual(result, 1)
            self.assertEqual(
                {item["registry_id"] for item in report["sources"]},
                {"greenhouse_customerio", "greenhouse_testlio"},
            )
            self.assertEqual(set(report["board_failures"]), {"greenhouse_gitlab"})
            self.assertNotIn(secret, stdout.getvalue())
            self.assertEqual(
                report["board_failures"]["greenhouse_gitlab"]["failure_code"],
                "unexpected_board_failure",
            )
            self.assertEqual(report["observation_ledger"]["invocation_status"], "partial_success")
            bundle_path = next((directory / BUNDLES_DIR_NAME).glob("*.json"))
            self.assertNotIn(secret, bundle_path.read_text(encoding="utf-8"))
            observations = {
                item["registry_id"]: item
                for item in json.loads(bundle_path.read_text(encoding="utf-8"))["observations"]
            }
            self.assertTrue(observations["greenhouse_customerio"]["technical_success"])
            self.assertTrue(observations["greenhouse_testlio"]["technical_success"])
            self.assertFalse(observations["greenhouse_gitlab"]["technical_success"])

    def test_offline_modes_reject_fetch_options_before_side_effects(self):
        invalid_cases = (
            ("--verify-history", ["--snapshots", "3"]),
            ("--verify-history", ["--lifecycle-probe"]),
            ("--verify-history", ["--skip-coverage"]),
            ("--verify-history", ["--board", "greenhouse_gitlab"]),
            ("--verify-history", ["--registry", "other.json"]),
            ("--evaluate-readiness", ["--snapshots", "2"]),
            ("--evaluate-readiness", ["--lifecycle-probe"]),
            ("--evaluate-readiness", ["--skip-coverage"]),
            ("--evaluate-readiness", ["--board", "greenhouse_gitlab"]),
            ("--evaluate-readiness", ["--record-observation-dir", "record"]),
        )
        for mode, extra in invalid_cases:
            with self.subTest(mode=mode, extra=extra), tempfile.TemporaryDirectory() as temp_dir:
                history = Path(temp_dir) / "must-not-exist"
                with (
                    patch.object(
                        pilot_script,
                        "fetch_snapshot_sequence",
                        side_effect=AssertionError("offline mode fetched"),
                    ),
                    patch.object(
                        pilot_script,
                        "load_full_baseline_rows",
                        side_effect=AssertionError("offline mode opened SQLite"),
                    ),
                    patch.object(
                        pilot_script,
                        "verify_observation_history",
                        side_effect=AssertionError("invalid options reached verification"),
                    ),
                    patch.object(
                        pilot_script,
                        "evaluate_operational_readiness",
                        side_effect=AssertionError("invalid options reached evaluation"),
                    ),
                ):
                    result, output = self.run_cli(
                        ["--history-dir", str(history), mode, *extra]
                    )
                self.assertEqual(result, 2)
                self.assertEqual(
                    json.loads(output)["error"], "registry_or_configuration_invalid"
                )
                self.assertFalse(history.exists())

    def test_history_mode_requires_history_and_ordinary_history_is_rejected(self):
        cases = (
            ["--verify-history"],
            ["--evaluate-readiness"],
            ["--history-dir", "unused"],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result, output = self.run_cli(arguments)
                self.assertEqual(result, 2)
                self.assertEqual(
                    json.loads(output)["error"], "registry_or_configuration_invalid"
                )

    def test_human_recording_output_contains_receipt_and_board_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result, output = self.run_cli(
                [
                    "--board",
                    "greenhouse_gitlab",
                    "--skip-coverage",
                    "--record-observation-dir",
                    str(Path(temp_dir) / "ledger"),
                ],
                fetch=self.complete_snapshot(),
                output_format="human",
            )
            self.assertEqual(result, 0)
            for phrase in (
                "evidence path:",
                "receipt path:",
                "ledger ID:",
                "ledger sequence:",
                "run ID:",
                "bundle ID:",
                "receipt ID:",
                "bundle fingerprint:",
                "receipt fingerprint:",
                "invocation status: complete_success",
                "greenhouse_gitlab: success",
            ):
                self.assertIn(phrase, output)

    def test_readiness_human_output_uses_exact_acceptance_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "ledger"
            result, _ = self.run_cli(
                [
                    "--board",
                    "greenhouse_gitlab",
                    "--skip-coverage",
                    "--record-observation-dir",
                    str(directory),
                ],
                fetch=self.complete_snapshot(),
            )
            self.assertEqual(result, 0)
            result, output = self.run_cli(
                ["--history-dir", str(directory), "--evaluate-readiness"],
                output_format="human",
            )
            self.assertEqual(result, 0)
            self.assertIn("independent acceptance approved:", output)
            self.assertNotIn("\n  acceptance approved:", output)


if __name__ == "__main__":
    unittest.main()
