import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError

from wahojobs.crawler.greenhouse_observations import (
    BUNDLES_DIR_NAME,
    MAX_BOUNDED_METRIC_ENTRIES,
    MAX_CODES_PER_BOARD,
    RECEIPTS_DIR_NAME,
    WORKING_DIR_NAME,
    ObservationClockError,
    ObservationConflictError,
    ObservationIncompletePublicationError,
    ObservationLedgerError,
    build_observation_bundle,
    build_observation_receipt,
    canonical_json,
    compute_bundle_sha256,
    compute_receipt_sha256,
    evaluate_operational_readiness,
    load_verified_ledger,
    load_verified_history,
    record_observation_bundle,
    safe_failure_diagnostic,
    validate_observation_bundle,
    verify_observation_history,
)
from wahojobs.crawler.source_registry import REGISTRY_PATH, load_source_registry


UTC = timezone.utc


class GreenhouseObservationLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry_entries = tuple(load_source_registry())
        cls.entry = next(
            item for item in cls.registry_entries if item.registry_id == "greenhouse_gitlab"
        )

    def report(self, entry, observed_at, *, success=True, attempts=1):
        outcome = "success" if success else "anomalous"
        lifecycle = (
            {
                "temporary_database": True,
                "initial_active": 2,
                "partial_removed": 0,
                "active_after_partial": 2,
                "complete_removed": 1,
                "active_after_complete": 1,
                "other_sources_unchanged": True,
                "closure_safe": True,
            }
            if success
            else {"not_run": True, "closure_safe": False}
        )
        source = {
            "registry_id": entry.registry_id,
            "raw_record_count": 2,
            "accepted_source_record_count": 2,
            "stable_identity_count": 2,
            "stable_identity_rate": 1.0,
            "safe_url_count": 2,
            "safe_url_rate": 1.0,
            "exact_title_unique_count": 2,
            "normalized_title_unique_count": 2,
            "normalized_title_repetition_rate": 0.0,
            "metadata_completeness": {
                "source_record_rate": 1.0,
                "description_rate": 1.0,
                "location_rate": 1.0,
                "departments_present_rate": 1.0,
                "offices_present_rate": 1.0,
                "updated_at_rate": 1.0,
            },
            "technical_status": {
                "connector_technically_valid": True,
                "snapshot_structurally_complete": True,
                "snapshot_count_anomaly_safe": success,
                "outcome": outcome,
                "closure_authorized": success,
                "fetch_attempts_this_invocation": attempts,
                "accepted_attempts_this_invocation": attempts if success else 0,
                "prior_accepted_count_used_for_latest_anomaly_check": 100,
            },
            "closure_safety": lifecycle,
            "canonical_yield_status": "unmeasured",
            "canonical_count": None,
            "canonical_yield": None,
            "canonical_consolidation_count": None,
            "canonical_duplicate_count": None,
            "canonicalization_input_fingerprint": None,
            "canonicalization_output_fingerprint": None,
            "relevant_posting_count": None,
            "enablement": {
                "production_readiness_failures": (
                    [] if success else ["current_snapshot_not_complete"]
                ),
                "production_ready": False,
            },
        }
        return {
            "report_version": "greenhouse_registry_pilot_v2",
            "evaluated_at": observed_at.isoformat(),
            "boards_evaluated": [entry.registry_id],
            "sources": [source],
            "persona_coverage": None,
            "technical_dry_run_passed": success,
            "production_enablement_changed": False,
        }

    def record(self, directory, observed_at, *, entry=None, success=True, attempts=1, **kwargs):
        entry = entry or self.entry
        kwargs.setdefault("published_at", observed_at + timedelta(minutes=2))
        return record_observation_bundle(
            directory,
            report=self.report(entry, observed_at, success=success, attempts=attempts),
            entries=(entry,),
            registry_entries=tuple(
                entry if item.registry_id == entry.registry_id else item
                for item in self.registry_entries
            ),
            started_at=observed_at - timedelta(minutes=1),
            completed_at=observed_at + timedelta(minutes=1),
            registry_path=REGISTRY_PATH,
            **kwargs,
        )

    def load_one(self, directory, *, entries=None):
        history = load_verified_history(
            directory,
            registry_entries=entries or self.registry_entries,
        )
        self.assertEqual(len(history), 1)
        return history[0]

    def rewrite(self, path, payload):
        Path(path).write_text(canonical_json(payload) + "\n", encoding="utf-8")

    def test_valid_bundle_round_trip_and_deterministic_fingerprint(self):
        observed_at = datetime(2026, 7, 17, 12, tzinfo=UTC)
        report = self.report(self.entry, observed_at)
        values = {
            "report": report,
            "entries": (self.entry,),
            "started_at": observed_at - timedelta(minutes=1),
            "completed_at": observed_at + timedelta(minutes=1),
            "registry_sha256": "a" * 64,
            "previous_bundle_sha256": None,
            "ledger_sequence": 1,
            "ledger_id": "ledger_" + "0" * 32,
            "run_id": "run_" + "1" * 32,
            "bundle_id": "bundle_" + "2" * 32,
        }
        left = build_observation_bundle(**values)
        right = build_observation_bundle(**values)
        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(left["bundle_content_sha256"], compute_bundle_sha256(left))
        validate_observation_bundle(left, registry_entries=self.registry_entries)
        self.assertEqual(len(left["observations"]), 1)

    def test_safe_failure_diagnostics_are_stable_and_allowlisted(self):
        cases = (
            (TimeoutError("private timeout token"), "network_timeout"),
            (
                HTTPError(
                    "https://example.test/?token=private",
                    500,
                    "private upstream message",
                    None,
                    None,
                ),
                "http_failure",
            ),
            (ValueError("private response payload"), "contract_validation_failure"),
            (RuntimeError("private unknown failure"), "unexpected_board_failure"),
        )
        for error, expected in cases:
            with self.subTest(expected=expected):
                diagnostic = safe_failure_diagnostic(error)
                self.assertEqual(diagnostic["failure_code"], expected)
                self.assertEqual(
                    set(diagnostic), {"failure_code", "safe_message"}
                )
                rendered = json.dumps(diagnostic)
                self.assertNotIn("private", rendered)
                self.assertLessEqual(len(diagnostic["failure_code"]), 96)
                self.assertLessEqual(len(diagnostic["safe_message"]), 512)

    def test_strict_schema_types_timestamps_counts_and_statuses(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorded = self.record(Path(temp_dir), datetime(2026, 7, 17, 12, tzinfo=UTC))
            original = self.load_one(temp_dir)
        mutations = []
        unknown = copy.deepcopy(original)
        unknown["unknown"] = True
        mutations.append(unknown)
        nested_unknown = copy.deepcopy(original)
        nested_unknown["observations"][0]["metadata_completeness"]["unknown"] = 1.0
        mutations.append(nested_unknown)
        boolean_count = copy.deepcopy(original)
        boolean_count["observations"][0]["raw_record_count"] = True
        mutations.append(boolean_count)
        malformed_time = copy.deepcopy(original)
        malformed_time["started_at"] = "2026-07-17T12:00:00+00:00"
        mutations.append(malformed_time)
        reversed_time = copy.deepcopy(original)
        reversed_time["completed_at"] = "2026-07-17T11:00:00.000000Z"
        mutations.append(reversed_time)
        outside_time = copy.deepcopy(original)
        outside_time["observations"][0]["observed_at"] = (
            "2026-07-18T12:00:00.000000Z"
        )
        mutations.append(outside_time)
        impossible = copy.deepcopy(original)
        impossible["observations"][0]["safe_url_count"] = 3
        mutations.append(impossible)
        invalid_enum = copy.deepcopy(original)
        invalid_enum["observations"][0]["snapshot_outcome"] = "complete"
        mutations.append(invalid_enum)
        contradictory = copy.deepcopy(original)
        contradictory["observations"][0]["technical_success"] = False
        mutations.append(contradictory)
        production_contradiction = copy.deepcopy(original)
        production_contradiction["observations"][0]["production_ready"] = True
        mutations.append(production_contradiction)
        unsupported = copy.deepcopy(original)
        unsupported["schema_version"] = "greenhouse_pilot_observation_bundle_v3"
        mutations.append(unsupported)
        for payload in mutations:
            with self.subTest(keys=sorted(payload)):
                with self.assertRaises(ObservationLedgerError):
                    validate_observation_bundle(
                        payload,
                        registry_entries=self.registry_entries,
                    )

    def test_first_write_is_create_only_and_replays_are_rejected(self):
        observed_at = datetime(2026, 7, 17, 12, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            recorded = self.record(
                directory,
                observed_at,
                run_id="run_" + "1" * 32,
                bundle_id="bundle_" + "2" * 32,
            )
            original = recorded.path.read_bytes()
            with self.assertRaises(ObservationConflictError):
                self.record(
                    directory,
                    observed_at + timedelta(hours=1),
                    run_id="run_" + "1" * 32,
                    bundle_id="bundle_" + "3" * 32,
                )
            with self.assertRaises(ObservationConflictError):
                self.record(
                    directory,
                    observed_at + timedelta(hours=2),
                    run_id="run_" + "4" * 32,
                    bundle_id="bundle_" + "2" * 32,
                )
            with self.assertRaises(ObservationConflictError):
                self.record(
                    directory,
                    observed_at + timedelta(hours=3),
                    run_id="run_" + "5" * 32,
                    bundle_id="bundle_" + "6" * 32,
                    receipt_id=recorded.receipt_id,
                )
            self.assertEqual(recorded.path.read_bytes(), original)
            self.assertEqual(len(list((directory / BUNDLES_DIR_NAME).glob("*.json"))), 1)
            self.assertEqual(len(list((directory / RECEIPTS_DIR_NAME).glob("*.json"))), 1)

    def test_duplicate_observation_id_in_one_bundle_is_rejected(self):
        customerio = next(
            item
            for item in self.registry_entries
            if item.registry_id == "greenhouse_customerio"
        )
        observed_at = datetime(2026, 7, 17, 12, tzinfo=UTC)
        report = self.report(self.entry, observed_at)
        report["sources"].extend(self.report(customerio, observed_at)["sources"])
        report["boards_evaluated"].append(customerio.registry_id)
        bundle = build_observation_bundle(
            report=report,
            entries=(self.entry, customerio),
            started_at=observed_at - timedelta(minutes=1),
            completed_at=observed_at + timedelta(minutes=1),
            registry_sha256="a" * 64,
            previous_bundle_sha256=None,
            ledger_sequence=1,
            run_id="run_" + "1" * 32,
            bundle_id="bundle_" + "2" * 32,
        )
        bundle["observations"][1]["observation_id"] = bundle["observations"][0][
            "observation_id"
        ]
        bundle["bundle_content_sha256"] = compute_bundle_sha256(bundle)
        with self.assertRaises(ObservationLedgerError):
            validate_observation_bundle(
                bundle, registry_entries=self.registry_entries
            )

    def test_repeated_fetch_attempts_contribute_one_board_observation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self.record(
                Path(temp_dir),
                datetime(2026, 7, 17, 12, tzinfo=UTC),
                attempts=3,
            )
            bundle = self.load_one(temp_dir)
            self.assertEqual(len(bundle["observations"]), 1)
            readiness = evaluate_operational_readiness(
                temp_dir,
                registry_entries=self.registry_entries,
                requested_registry_ids=[self.entry.registry_id],
            )
            self.assertEqual(
                readiness["boards"][self.entry.registry_id]["observation_count"], 1
            )

    def test_failure_injection_before_publication_preserves_prior_history(self):
        failure_points = (
            "after_lock_acquisition",
            "before_final_serialization",
            "during_file_write",
            "before_fsync",
            "before_atomic_publication",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "ledger"
            first = self.record(directory, datetime(2026, 7, 17, 12, tzinfo=UTC))
            original = first.path.read_bytes()
            for offset, failure_point in enumerate(failure_points, start=1):
                def fail(point, target=failure_point):
                    if point == target:
                        raise RuntimeError(target)

                with self.subTest(point=failure_point):
                    with self.assertRaises(RuntimeError):
                        self.record(
                            directory,
                            datetime(2026, 7, 17, 12 + offset, tzinfo=UTC),
                            failure_injector=fail,
                        )
                    self.assertEqual(first.path.read_bytes(), original)
                    self.assertEqual(
                        len(list((directory / BUNDLES_DIR_NAME).glob("*.json"))), 1
                    )
                    self.assertFalse(list((directory / BUNDLES_DIR_NAME).glob("*.tmp")))
                    self.assertFalse(list((directory / RECEIPTS_DIR_NAME).glob("*.tmp")))
                    self.assertTrue(
                        verify_observation_history(
                            directory, registry_entries=self.registry_entries
                        )["valid"]
                    )

    def test_failure_before_directory_and_after_publication_are_safe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "ledger"

            def before_directory(point):
                if point == "before_directory_creation":
                    raise RuntimeError(point)

            with self.assertRaises(RuntimeError):
                self.record(
                    directory,
                    datetime(2026, 7, 17, 12, tzinfo=UTC),
                    failure_injector=before_directory,
                )
            self.assertFalse(directory.exists())

            def after_publication(point):
                if point == "after_publication_before_reporting":
                    raise RuntimeError(point)

            with self.assertRaises(RuntimeError):
                self.record(
                    directory,
                    datetime(2026, 7, 17, 13, tzinfo=UTC),
                    failure_injector=after_publication,
                )
            verification = verify_observation_history(
                directory, registry_entries=self.registry_entries
            )
            self.assertTrue(verification["valid"])
            self.assertEqual(verification["bundle_count"], 1)

    def test_concurrent_writers_are_serialized_without_partial_history(self):
        entered = threading.Event()
        release = threading.Event()
        errors = []
        results = []
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)

            def pause(point):
                if point == "after_lock_acquisition":
                    entered.set()
                    release.wait(timeout=5)

            def first_writer():
                try:
                    self.record(
                        directory,
                        datetime(2026, 7, 17, 12, tzinfo=UTC),
                        failure_injector=pause,
                    )
                    results.append("first")
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            def second_writer():
                try:
                    self.record(directory, datetime(2026, 7, 17, 13, tzinfo=UTC))
                    results.append("second")
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            thread = threading.Thread(target=first_writer)
            second = threading.Thread(target=second_writer)
            thread.start()
            self.assertTrue(entered.wait(timeout=5))
            second.start()
            release.set()
            thread.join(timeout=5)
            second.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(sorted(results), ["first", "second"])
            self.assertEqual(len(list((directory / BUNDLES_DIR_NAME).glob("*.json"))), 2)
            self.assertEqual(len(list((directory / RECEIPTS_DIR_NAME).glob("*.json"))), 2)
            self.assertTrue(
                verify_observation_history(
                    directory, registry_entries=self.registry_entries
                )["valid"]
            )

    def test_tampering_duplicates_renames_and_broken_chain_are_detected(self):
        scenarios = ("field", "removed", "extra", "fingerprint", "renamed", "copied")
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp_dir:
                directory = Path(temp_dir)
                recorded = self.record(
                    directory, datetime(2026, 7, 17, 12, tzinfo=UTC)
                )
                payload = json.loads(recorded.path.read_text(encoding="utf-8"))
                if scenario == "field":
                    payload["observations"][0]["raw_record_count"] = 1
                    self.rewrite(recorded.path, payload)
                elif scenario == "removed":
                    payload.pop("tool_version")
                    self.rewrite(recorded.path, payload)
                elif scenario == "extra":
                    payload["extra"] = "value"
                    self.rewrite(recorded.path, payload)
                elif scenario == "fingerprint":
                    payload["bundle_content_sha256"] = "0" * 64
                    self.rewrite(recorded.path, payload)
                elif scenario == "renamed":
                    recorded.path.rename(directory / "renamed.json")
                else:
                    shutil.copyfile(recorded.path, directory / "copy.json")
                self.assertFalse(
                    verify_observation_history(
                        directory, registry_entries=self.registry_entries
                    )["valid"]
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            self.record(directory, datetime(2026, 7, 17, 0, tzinfo=UTC))
            self.record(directory, datetime(2026, 7, 17, 12, tzinfo=UTC))
            self.record(directory, datetime(2026, 7, 18, 0, tzinfo=UTC))
            sorted((directory / BUNDLES_DIR_NAME).glob("*.json"))[1].unlink()
            self.assertFalse(
                verify_observation_history(
                    directory, registry_entries=self.registry_entries
                )["valid"]
            )

    def test_readiness_requires_three_distinct_runs_spanning_24_hours(self):
        cases = (
            ((), 0, False),
            ((0,), 1, False),
            ((0, 12), 2, False),
            ((0, 6, 12), 3, False),
            ((0, 12, 24), 3, True),
        )
        start = datetime(2026, 7, 17, 0, tzinfo=UTC)
        for offsets, expected_streak, expected_ready in cases:
            with self.subTest(offsets=offsets), tempfile.TemporaryDirectory() as temp_dir:
                for hours in offsets:
                    self.record(Path(temp_dir), start + timedelta(hours=hours))
                report = evaluate_operational_readiness(
                    temp_dir,
                    registry_entries=self.registry_entries,
                    requested_registry_ids=[self.entry.registry_id],
                )
                board = report["boards"][self.entry.registry_id]
                self.assertEqual(board["technical_snapshot_streak"], expected_streak)
                self.assertEqual(board["operational_snapshot_ready"], expected_ready)
                self.assertFalse(board["production_ready"])
                self.assertFalse(board["product_enabled"])
                self.assertFalse(board["production_crawl_enabled"])

    def test_failures_anomalies_parser_and_contract_changes_break_streak(self):
        start = datetime(2026, 7, 17, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            self.record(directory, start)
            self.record(directory, start + timedelta(hours=12))
            self.record(directory, start + timedelta(hours=18), success=False)
            self.record(directory, start + timedelta(hours=24))
            self.record(directory, start + timedelta(hours=36))
            board = evaluate_operational_readiness(
                directory,
                registry_entries=self.registry_entries,
                requested_registry_ids=[self.entry.registry_id],
            )["boards"][self.entry.registry_id]
            self.assertEqual(board["technical_snapshot_streak"], 2)
            self.assertFalse(board["operational_snapshot_ready"])

        for changed_entry in (
            replace(self.entry, parser_version="greenhouse-legacy"),
            replace(self.entry, target_languages=("multilingual",)),
        ):
            with self.subTest(change=changed_entry), tempfile.TemporaryDirectory() as temp_dir:
                directory = Path(temp_dir)
                self.record(directory, start)
                self.record(directory, start + timedelta(hours=12))
                self.record(
                    directory,
                    start + timedelta(hours=24),
                    entry=changed_entry,
                )
                current_entries = tuple(
                    changed_entry if item.registry_id == self.entry.registry_id else item
                    for item in self.registry_entries
                )
                board = evaluate_operational_readiness(
                    directory,
                    registry_entries=current_entries,
                    requested_registry_ids=[self.entry.registry_id],
                )["boards"][self.entry.registry_id]
                self.assertEqual(board["technical_snapshot_streak"], 1)
                self.assertFalse(board["operational_snapshot_ready"])

    def test_mixed_board_subsets_share_one_valid_history(self):
        customerio = next(
            item
            for item in self.registry_entries
            if item.registry_id == "greenhouse_customerio"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            self.record(directory, datetime(2026, 7, 17, 12, tzinfo=UTC))
            self.record(
                directory,
                datetime(2026, 7, 17, 13, tzinfo=UTC),
                entry=customerio,
            )
            history = load_verified_history(
                directory, registry_entries=self.registry_entries
            )
            self.assertEqual(len(history), 2)
            self.assertEqual(
                [item["requested_registry_ids"] for item in history],
                [["greenhouse_gitlab"], ["greenhouse_customerio"]],
            )

    def test_bundle_receipt_contract_and_readiness_are_history_derived(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorded = self.record(
                Path(temp_dir), datetime(2026, 7, 17, 12, tzinfo=UTC)
            )
            history = load_verified_ledger(
                temp_dir, registry_entries=self.registry_entries
            )
            self.assertEqual(history.ledger_id, recorded.ledger_id)
            self.assertEqual(len(history.bundles), 1)
            self.assertEqual(len(history.receipts), 1)
            bundle = history.bundles[0]
            receipt = history.receipts[0]
            self.assertNotIn("production_ready", bundle)
            self.assertNotIn("production_ready", bundle["observations"][0])
            self.assertEqual(receipt["bundle_sha256"], bundle["bundle_content_sha256"])
            self.assertEqual(receipt["ledger_id"], bundle["ledger_id"])
            readiness = evaluate_operational_readiness(
                temp_dir,
                registry_entries=self.registry_entries,
                requested_registry_ids=[self.entry.registry_id],
            )["boards"][self.entry.registry_id]
            self.assertFalse(readiness["operational_snapshot_ready"])
            self.assertIn("independent_acceptance_approved", readiness)
            self.assertNotIn("acceptance_approved", readiness)

    def test_partial_publication_fails_closed_without_automatic_recovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)

            def fail_receipt(point):
                if point == "before_receipt_publication":
                    raise PermissionError("synthetic receipt publication failure")

            with self.assertRaises(ObservationIncompletePublicationError):
                self.record(
                    directory,
                    datetime(2026, 7, 17, 12, tzinfo=UTC),
                    failure_injector=fail_receipt,
                )
            self.assertEqual(len(list((directory / BUNDLES_DIR_NAME).glob("*.json"))), 1)
            self.assertEqual(len(list((directory / RECEIPTS_DIR_NAME).glob("*.json"))), 0)
            self.assertFalse(
                verify_observation_history(
                    directory, registry_entries=self.registry_entries
                )["valid"]
            )
            readiness = evaluate_operational_readiness(
                directory,
                registry_entries=self.registry_entries,
                requested_registry_ids=[self.entry.registry_id],
            )
            self.assertFalse(readiness["history_verification"]["valid"])
            with self.assertRaises(ObservationLedgerError):
                self.record(directory, datetime(2026, 7, 17, 13, tzinfo=UTC))

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)

            def fail_bundle(point):
                if point == "before_bundle_publication":
                    raise PermissionError("synthetic bundle publication failure")

            with self.assertRaises(PermissionError):
                self.record(
                    directory,
                    datetime(2026, 7, 17, 12, tzinfo=UTC),
                    failure_injector=fail_bundle,
                )
            self.assertFalse(list((directory / BUNDLES_DIR_NAME).glob("*.json")))
            self.assertFalse(list((directory / RECEIPTS_DIR_NAME).glob("*.json")))
            self.assertTrue(
                verify_observation_history(
                    directory, registry_entries=self.registry_entries
                )["valid"]
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            self.record(directory, datetime(2026, 7, 17, 12, tzinfo=UTC))
            shutil.rmtree(directory / RECEIPTS_DIR_NAME)
            with self.assertRaises(ObservationLedgerError):
                self.record(directory, datetime(2026, 7, 17, 13, tzinfo=UTC))
            self.assertFalse((directory / RECEIPTS_DIR_NAME).exists())

    def test_bundle_and_receipt_deletions_are_detected(self):
        deletion_cases = (
            (BUNDLES_DIR_NAME, 0),
            (BUNDLES_DIR_NAME, 1),
            (BUNDLES_DIR_NAME, -1),
            (RECEIPTS_DIR_NAME, 0),
            (RECEIPTS_DIR_NAME, 1),
            (RECEIPTS_DIR_NAME, -1),
        )
        start = datetime(2026, 7, 17, 0, tzinfo=UTC)
        for directory_name, index in deletion_cases:
            with self.subTest(directory=directory_name, index=index), tempfile.TemporaryDirectory() as temp_dir:
                directory = Path(temp_dir)
                for hours in (0, 12, 24, 36):
                    self.record(directory, start + timedelta(hours=hours))
                paths = sorted((directory / directory_name).glob("*.json"))
                paths[index].unlink()
                verification = verify_observation_history(
                    directory, registry_entries=self.registry_entries
                )
                self.assertFalse(verification["valid"])

    def test_newest_failure_cannot_be_hidden_by_single_artifact_deletion(self):
        start = datetime(2026, 7, 17, 0, tzinfo=UTC)
        for directory_name in (BUNDLES_DIR_NAME, RECEIPTS_DIR_NAME):
            with self.subTest(directory=directory_name), tempfile.TemporaryDirectory() as temp_dir:
                directory = Path(temp_dir)
                for hours in (0, 12, 24):
                    self.record(directory, start + timedelta(hours=hours))
                self.record(directory, start + timedelta(hours=36), success=False)
                before = evaluate_operational_readiness(
                    directory,
                    registry_entries=self.registry_entries,
                    requested_registry_ids=[self.entry.registry_id],
                )["boards"][self.entry.registry_id]
                self.assertFalse(before["operational_snapshot_ready"])
                sorted((directory / directory_name).glob("*.json"))[-1].unlink()
                after = evaluate_operational_readiness(
                    directory,
                    registry_entries=self.registry_entries,
                    requested_registry_ids=[self.entry.registry_id],
                )
                self.assertFalse(after["history_verification"]["valid"])
                self.assertFalse(after["production_ready"])

    def test_dual_tail_deletion_is_documented_local_ledger_limitation(self):
        start = datetime(2026, 7, 17, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            for hours in (0, 12, 24):
                self.record(directory, start + timedelta(hours=hours))
            self.record(directory, start + timedelta(hours=36), success=False)
            sorted((directory / BUNDLES_DIR_NAME).glob("*.json"))[-1].unlink()
            sorted((directory / RECEIPTS_DIR_NAME).glob("*.json"))[-1].unlink()
            # Without an external witness, deleting the complete newest pair is a
            # rollback to a formerly valid local prefix and is intentionally not
            # claimed as detectable.
            verification = verify_observation_history(
                directory, registry_entries=self.registry_entries
            )
            self.assertTrue(verification["valid"])
            self.assertEqual(verification["bundle_count"], 3)

    def test_subset_fork_and_ledger_splice_are_rejected(self):
        start = datetime(2026, 7, 17, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as source_temp, tempfile.TemporaryDirectory() as subset_temp:
            source = Path(source_temp)
            subset = Path(subset_temp)
            for hours in (0, 12, 24, 36):
                self.record(source, start + timedelta(hours=hours))
            (subset / BUNDLES_DIR_NAME).mkdir()
            (subset / RECEIPTS_DIR_NAME).mkdir()
            shutil.copyfile(source / ".ledger-lock", subset / ".ledger-lock")
            for path in sorted((source / BUNDLES_DIR_NAME).glob("*.json"))[1:]:
                shutil.copyfile(path, subset / BUNDLES_DIR_NAME / path.name)
            for path in sorted((source / RECEIPTS_DIR_NAME).glob("*.json"))[1:]:
                shutil.copyfile(path, subset / RECEIPTS_DIR_NAME / path.name)
            self.assertFalse(
                verify_observation_history(
                    subset, registry_entries=self.registry_entries
                )["valid"]
            )

        with tempfile.TemporaryDirectory() as first_temp, tempfile.TemporaryDirectory() as second_temp:
            first = Path(first_temp)
            second = Path(second_temp)
            self.record(first, start)
            self.record(second, start)
            for name in (BUNDLES_DIR_NAME, RECEIPTS_DIR_NAME):
                foreign = next((second / name).glob("*.json"))
                shutil.copyfile(foreign, first / name / ("foreign-" + foreign.name))
            self.assertFalse(
                verify_observation_history(
                    first, registry_entries=self.registry_entries
                )["valid"]
            )

    def test_strict_chronological_append_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            self.record(directory, datetime(2026, 7, 17, 12, tzinfo=UTC))
            for value in (
                datetime(2026, 7, 17, 11, tzinfo=UTC),
                datetime(2026, 7, 17, 12, 2, tzinfo=UTC),
            ):
                with self.subTest(value=value), self.assertRaises(ObservationLedgerError):
                    self.record(directory, value)
            self.record(directory, datetime(2026, 7, 17, 12, 3, tzinfo=UTC))
            self.assertTrue(
                verify_observation_history(
                    directory, registry_entries=self.registry_entries
                )["valid"]
            )

            bundle_path = sorted((directory / BUNDLES_DIR_NAME).glob("*.json"))[1]
            receipt_path = sorted((directory / RECEIPTS_DIR_NAME).glob("*.json"))[1]
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            first = load_verified_history(
                directory, registry_entries=self.registry_entries
            )[0]
            bundle["started_at"] = first["completed_at"]
            bundle["bundle_content_sha256"] = compute_bundle_sha256(bundle)
            receipt["bundle_sha256"] = bundle["bundle_content_sha256"]
            receipt["receipt_content_sha256"] = compute_receipt_sha256(receipt)
            self.rewrite(bundle_path, bundle)
            self.rewrite(receipt_path, receipt)
            self.assertFalse(
                verify_observation_history(
                    directory, registry_entries=self.registry_entries
                )["valid"]
            )

    def test_append_plan_rejects_clock_rollback_before_staging(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            first = self.record(
                directory,
                datetime(2026, 7, 17, 12, tzinfo=UTC),
                published_at=datetime(2026, 7, 17, 15, tzinfo=UTC),
            )
            prior_bundle = first.path.read_bytes()
            prior_receipt = first.receipt_path.read_bytes()
            before = evaluate_operational_readiness(
                directory,
                registry_entries=self.registry_entries,
                requested_registry_ids=[self.entry.registry_id],
            )
            for publication in (
                datetime(2026, 7, 17, 14, tzinfo=UTC),
                datetime(2026, 7, 17, 15, tzinfo=UTC),
            ):
                with self.subTest(publication=publication), self.assertRaises(
                    ObservationClockError
                ):
                    self.record(
                        directory,
                        datetime(2026, 7, 17, 13, tzinfo=UTC),
                        published_at=publication,
                    )
                self.assertEqual(len(list((directory / BUNDLES_DIR_NAME).glob("*.json"))), 1)
                self.assertEqual(len(list((directory / RECEIPTS_DIR_NAME).glob("*.json"))), 1)
                self.assertFalse(list((directory / WORKING_DIR_NAME).iterdir()))
                self.assertEqual(first.path.read_bytes(), prior_bundle)
                self.assertEqual(first.receipt_path.read_bytes(), prior_receipt)
                self.assertEqual(
                    evaluate_operational_readiness(
                        directory,
                        registry_entries=self.registry_entries,
                        requested_registry_ids=[self.entry.registry_id],
                    ),
                    before,
                )

            second = self.record(
                directory,
                datetime(2026, 7, 17, 13, tzinfo=UTC),
                published_at=datetime(2026, 7, 17, 16, tzinfo=UTC),
            )
            self.assertEqual(second.ledger_sequence, 2)
            self.assertTrue(
                verify_observation_history(
                    directory, registry_entries=self.registry_entries
                )["valid"]
            )

    def test_receipt_before_completion_is_rejected_without_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            with self.assertRaises(ObservationClockError):
                self.record(
                    directory,
                    datetime(2026, 7, 17, 12, tzinfo=UTC),
                    published_at=datetime(2026, 7, 17, 12, tzinfo=UTC),
                )
            self.assertFalse(list((directory / BUNDLES_DIR_NAME).glob("*.json")))
            self.assertFalse(list((directory / RECEIPTS_DIR_NAME).glob("*.json")))
            self.assertFalse(list((directory / WORKING_DIR_NAME).iterdir()))

    def test_receipt_mismatch_fork_cycle_and_sequence_gap_are_rejected(self):
        scenarios = ("mismatch", "fork", "cycle", "sequence", "publication")
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp_dir:
                directory = Path(temp_dir)
                self.record(directory, datetime(2026, 7, 17, 12, tzinfo=UTC))
                self.record(directory, datetime(2026, 7, 17, 13, tzinfo=UTC))
                receipt_path = sorted((directory / RECEIPTS_DIR_NAME).glob("*.json"))[1]
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if scenario == "mismatch":
                    receipt["bundle_sha256"] = "a" * 64
                elif scenario == "fork":
                    receipt["previous_receipt_sha256"] = None
                elif scenario == "cycle":
                    receipt["previous_receipt_sha256"] = receipt["receipt_content_sha256"]
                elif scenario == "sequence":
                    receipt["ledger_sequence"] = 3
                else:
                    first_receipt = json.loads(
                        sorted((directory / RECEIPTS_DIR_NAME).glob("*.json"))[0].read_text(
                            encoding="utf-8"
                        )
                    )
                    receipt["published_at"] = first_receipt["published_at"]
                receipt["receipt_content_sha256"] = compute_receipt_sha256(receipt)
                self.rewrite(receipt_path, receipt)
                self.assertFalse(
                    verify_observation_history(
                        directory, registry_entries=self.registry_entries
                    )["valid"]
                )

    def test_evidence_bounds_reject_extremes_and_allow_bounded_content(self):
        observed_at = datetime(2026, 7, 17, 12, tzinfo=UTC)
        report = self.report(self.entry, observed_at)
        report["sources"][0]["enablement"]["production_readiness_failures"] = [
            f"reason_{index:04d}" for index in range(5000)
        ]
        with self.assertRaises(ObservationLedgerError):
            build_observation_bundle(
                report=report,
                entries=(self.entry,),
                started_at=observed_at - timedelta(minutes=1),
                completed_at=observed_at + timedelta(minutes=1),
                registry_sha256="a" * 64,
                previous_bundle_sha256=None,
                ledger_sequence=1,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = self.record(Path(temp_dir), observed_at)
            payload = json.loads(bundle.path.read_text(encoding="utf-8"))
        reasons = payload["observations"][0]["operational_failure_reasons"]
        payload["observations"][0]["operational_failure_reasons"] = [
            f"code_{index:03d}" for index in range(MAX_CODES_PER_BOARD + 1)
        ]
        payload["bundle_content_sha256"] = compute_bundle_sha256(payload)
        with self.assertRaises(ObservationLedgerError):
            validate_observation_bundle(payload, registry_entries=self.registry_entries)
        payload["observations"][0]["operational_failure_reasons"] = reasons

        coverage = payload["observations"][0]["coverage"]
        coverage.update(
            {
                "status": "measured_28_persona_comparison",
                "persona_count": 28,
                "relevant_posting_count": 0,
                "new_eligibility_leakage_count": 0,
                "current_comparison_no_new_leakage": True,
                "admitted_result_delta": 0,
                "strong_family_delta": 0,
                "company_diversity_delta": 0,
                "regional_rejection_count": 0,
                "regional_rejection_delta": 0,
                "qualification_rejection_count": 0,
                "qualification_rejection_delta": 0,
                "new_titles": [
                    ("x" * 508) + f"{index:04d}"
                    for index in range(MAX_BOUNDED_METRIC_ENTRIES)
                ],
                "new_companies": [],
            }
        )
        payload["bundle_content_sha256"] = compute_bundle_sha256(payload)
        validate_observation_bundle(payload, registry_entries=self.registry_entries)
        coverage["new_titles"] = [
            ("é" * 508) + f"{index:04d}"
            for index in range(MAX_BOUNDED_METRIC_ENTRIES)
        ]
        payload["bundle_content_sha256"] = compute_bundle_sha256(payload)
        with self.assertRaisesRegex(ObservationLedgerError, "size limit"):
            validate_observation_bundle(payload, registry_entries=self.registry_entries)

        payload["observations"][0]["coverage"] = copy.deepcopy(
            self.load_coverage_unmeasured()
        )
        payload["requested_registry_ids"] = [
            f"greenhouse_board_{index:03d}"
            for index in range(65)
        ]
        payload["bundle_content_sha256"] = compute_bundle_sha256(payload)
        with self.assertRaisesRegex(ObservationLedgerError, "maximum of 64"):
            validate_observation_bundle(payload, registry_entries=self.registry_entries)

        report = self.report(self.entry, observed_at)
        with self.assertRaises(ObservationLedgerError):
            build_observation_bundle(
                report=report,
                entries=(self.entry,),
                started_at=observed_at - timedelta(minutes=1),
                completed_at=observed_at + timedelta(minutes=1),
                registry_sha256="a" * 64,
                previous_bundle_sha256=None,
                ledger_sequence=1,
                bundle_id="bundle_../../unsafe",
            )

    def load_coverage_unmeasured(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorded = self.record(
                Path(temp_dir), datetime(2026, 7, 17, 12, tzinfo=UTC)
            )
            payload = json.loads(recorded.path.read_text(encoding="utf-8"))
            return payload["observations"][0]["coverage"]

    def test_failed_board_breaks_only_its_own_readiness_streak(self):
        customerio = next(
            item
            for item in self.registry_entries
            if item.registry_id == "greenhouse_customerio"
        )
        start = datetime(2026, 7, 17, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            for index, hours in enumerate((0, 12, 24, 36)):
                observed_at = start + timedelta(hours=hours)
                gitlab_success = index < 3
                report = self.report(
                    self.entry, observed_at, success=gitlab_success
                )
                customer_report = self.report(customerio, observed_at, success=True)
                report["sources"] = (
                    report["sources"] if gitlab_success else []
                ) + customer_report["sources"]
                report["boards_evaluated"] = [
                    self.entry.registry_id,
                    customerio.registry_id,
                ]
                report["board_failures"] = (
                    {}
                    if gitlab_success
                    else {
                        self.entry.registry_id: {
                            "error_type": "RuntimeError",
                            "detail": "not persisted",
                        }
                    }
                )
                report["technical_dry_run_passed"] = gitlab_success
                record_observation_bundle(
                    directory,
                    report=report,
                    entries=(self.entry, customerio),
                    registry_entries=self.registry_entries,
                    started_at=observed_at - timedelta(minutes=1),
                    completed_at=observed_at + timedelta(minutes=1),
                    published_at=observed_at + timedelta(minutes=2),
                    registry_path=REGISTRY_PATH,
                )
            readiness = evaluate_operational_readiness(
                directory,
                registry_entries=self.registry_entries,
                requested_registry_ids=[self.entry.registry_id, customerio.registry_id],
            )["boards"]
            self.assertEqual(
                readiness[self.entry.registry_id]["technical_snapshot_streak"], 0
            )
            self.assertFalse(
                readiness[self.entry.registry_id]["operational_snapshot_ready"]
            )
            self.assertEqual(
                readiness[customerio.registry_id]["technical_snapshot_streak"], 4
            )
            self.assertTrue(
                readiness[customerio.registry_id]["operational_snapshot_ready"]
            )

    def test_filesystem_entries_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "file"
            target.write_text("not a directory", encoding="utf-8")
            with self.assertRaises(ObservationLedgerError):
                self.record(target, datetime(2026, 7, 17, 12, tzinfo=UTC))

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            self.record(directory, datetime(2026, 7, 17, 12, tzinfo=UTC))
            (directory / BUNDLES_DIR_NAME / ".stale.tmp").write_text("partial")
            self.assertFalse(
                verify_observation_history(
                    directory, registry_entries=self.registry_entries
                )["valid"]
            )

    def test_working_residue_is_bounded_non_authoritative_and_writer_cleanable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            self.record(directory, datetime(2026, 7, 17, 12, tzinfo=UTC))
            working = directory / WORKING_DIR_NAME
            for index in range(2):
                name = (
                    "staging_bundle_bundle_"
                    + f"{index + 1:032x}"
                    + "_"
                    + f"{index + 11:032x}"
                    + ".tmp"
                )
                (working / name).write_bytes(b"partial private staging data")
            verification = verify_observation_history(
                directory, registry_entries=self.registry_entries
            )
            self.assertTrue(verification["valid"])
            self.assertEqual(verification["working_residue_count"], 2)
            self.assertIsNotNone(verification["working_residue_fingerprint"])

            self.record(directory, datetime(2026, 7, 17, 13, tzinfo=UTC))
            self.assertFalse(list(working.iterdir()))
            self.assertTrue(
                verify_observation_history(
                    directory, registry_entries=self.registry_entries
                )["valid"]
            )

        for name, make_directory in (
            ("malformed.tmp", False),
            ("unexpected-directory", True),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp_dir:
                directory = Path(temp_dir)
                self.record(directory, datetime(2026, 7, 17, 12, tzinfo=UTC))
                path = directory / WORKING_DIR_NAME / name
                if make_directory:
                    path.mkdir()
                else:
                    path.write_text("unknown", encoding="utf-8")
                self.assertFalse(
                    verify_observation_history(
                        directory, registry_entries=self.registry_entries
                    )["valid"]
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            self.record(directory, datetime(2026, 7, 17, 12, tzinfo=UTC))
            source = next((directory / BUNDLES_DIR_NAME).glob("*.json"))
            duplicate = directory / BUNDLES_DIR_NAME / "hard-link.json"
            try:
                os.link(source, duplicate)
            except OSError as exc:  # pragma: no cover - host capability
                self.skipTest(f"Hard links unavailable on this host: {exc}")
            self.assertFalse(
                verify_observation_history(
                    directory, registry_entries=self.registry_entries
                )["valid"]
            )

    def test_symlinked_root_bundle_and_receipt_are_rejected_when_supported(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            target = base / "target"
            target.mkdir()
            link = base / "linked-ledger"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Symlink creation unavailable on this host: {exc}")
            self.assertFalse(
                verify_observation_history(
                    link, registry_entries=self.registry_entries
                )["valid"]
            )

        for directory_name in (BUNDLES_DIR_NAME, RECEIPTS_DIR_NAME):
            with self.subTest(directory=directory_name), tempfile.TemporaryDirectory() as temp_dir:
                directory = Path(temp_dir)
                self.record(directory, datetime(2026, 7, 17, 12, tzinfo=UTC))
                original = next((directory / directory_name).glob("*.json"))
                backup = directory.parent / (directory_name + "-artifact.json")
                shutil.copyfile(original, backup)
                original.unlink()
                original.symlink_to(backup)
                self.assertFalse(
                    verify_observation_history(
                        directory, registry_entries=self.registry_entries
                    )["valid"]
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            self.record(directory, datetime(2026, 7, 17, 12, tzinfo=UTC))
            target = directory.parent / "working-target.tmp"
            target.write_text("private staging data", encoding="utf-8")
            link = directory / WORKING_DIR_NAME / (
                "staging_bundle_bundle_"
                + "1" * 32
                + "_"
                + "2" * 32
                + ".tmp"
            )
            link.symlink_to(target)
            self.assertFalse(
                verify_observation_history(
                    directory, registry_entries=self.registry_entries
                )["valid"]
            )

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not authoritative on Windows")
    def test_permission_failures_are_reported_without_mutation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            self.record(directory, datetime(2026, 7, 17, 12, tzinfo=UTC))
            bundles = directory / BUNDLES_DIR_NAME
            bundles.chmod(0o500)
            try:
                with self.assertRaises((OSError, ObservationLedgerError)):
                    self.record(directory, datetime(2026, 7, 17, 13, tzinfo=UTC))
            finally:
                bundles.chmod(0o700)


class GreenhouseObservationMultiprocessTests(unittest.TestCase):
    report = GreenhouseObservationLedgerTests.report
    record = GreenhouseObservationLedgerTests.record

    @classmethod
    def setUpClass(cls):
        GreenhouseObservationLedgerTests.setUpClass()
        cls.registry_entries = GreenhouseObservationLedgerTests.registry_entries
        cls.entry = GreenhouseObservationLedgerTests.entry

    def child_command(self, directory, observed_at, *, pause=False, marker=None):
        code = """
from datetime import datetime
from pathlib import Path
import sys, time
from tests.test_greenhouse_observation_ledger import GreenhouseObservationLedgerTests
GreenhouseObservationLedgerTests.setUpClass()
case = GreenhouseObservationLedgerTests()
marker = Path(sys.argv[3]) if sys.argv[3] != '-' else None
def hook(point):
    if point == 'after_lock_acquisition' and marker is not None:
        marker.write_text('locked', encoding='utf-8')
        time.sleep(0.6)
case.record(
    Path(sys.argv[1]),
    datetime.fromisoformat(sys.argv[2]),
    failure_injector=hook if marker is not None else None,
)
"""
        return [
            sys.executable,
            "-B",
            "-c",
            code,
            str(directory),
            observed_at.isoformat(),
            str(marker) if pause and marker is not None else "-",
        ]

    def crash_command(self, directory, observed_at, point, exit_code):
        code = """
import os, sys
from datetime import datetime
from pathlib import Path
from tests.test_greenhouse_observation_ledger import GreenhouseObservationLedgerTests
GreenhouseObservationLedgerTests.setUpClass()
case = GreenhouseObservationLedgerTests()
def hook(value):
    if value == sys.argv[3]:
        os._exit(int(sys.argv[4]))
case.record(
    Path(sys.argv[1]),
    datetime.fromisoformat(sys.argv[2]),
    failure_injector=hook,
)
"""
        return [
            sys.executable,
            "-B",
            "-c",
            code,
            str(directory),
            observed_at.isoformat(),
            point,
            str(exit_code),
        ]

    def test_real_subprocess_writers_serialize(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            directory = root / "ledger"
            marker = root / "writer.locked"
            first = subprocess.Popen(
                self.child_command(
                    directory,
                    datetime(2026, 7, 17, 12, tzinfo=UTC),
                    pause=True,
                    marker=marker,
                ),
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.time() + 5
            while not marker.exists() and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            second = subprocess.Popen(
                self.child_command(
                    directory, datetime(2026, 7, 17, 13, tzinfo=UTC)
                ),
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            first_output = first.communicate(timeout=10)
            second_output = second.communicate(timeout=10)
            self.assertEqual((first.returncode, first_output), (0, ("", "")))
            self.assertEqual((second.returncode, second_output), (0, ("", "")))
            history = load_verified_ledger(
                directory, registry_entries=self.registry_entries
            )
            self.assertEqual(len(history.bundles), 2)
            self.assertEqual(len({item["run_id"] for item in history.bundles}), 2)
            self.assertEqual(len({item["bundle_id"] for item in history.bundles}), 2)
            self.assertEqual(len({item["receipt_id"] for item in history.receipts}), 2)

    def test_verify_and_evaluate_wait_for_cross_process_writer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            directory = root / "ledger"
            marker = root / "writer.locked"
            writer = subprocess.Popen(
                self.child_command(
                    directory,
                    datetime(2026, 7, 17, 12, tzinfo=UTC),
                    pause=True,
                    marker=marker,
                ),
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.time() + 5
            while not marker.exists() and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            results = {}

            def verify_reader():
                results["verify"] = verify_observation_history(
                    directory, registry_entries=self.registry_entries
                )

            def readiness_reader():
                results["readiness"] = evaluate_operational_readiness(
                    directory,
                    registry_entries=self.registry_entries,
                    requested_registry_ids=[self.entry.registry_id],
                )

            threads = [threading.Thread(target=verify_reader), threading.Thread(target=readiness_reader)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
            output = writer.communicate(timeout=10)
            self.assertEqual((writer.returncode, output), (0, ("", "")))
            self.assertTrue(results["verify"]["valid"])
            self.assertTrue(results["readiness"]["history_verification"]["valid"])

    def test_process_death_releases_lock(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            directory = root / "ledger"
            marker = root / "crashed.locked"
            code = """
import os, sys
from pathlib import Path
from wahojobs.crawler.greenhouse_observations import _ledger_lock, _prepare_ledger_root
directory = Path(sys.argv[1])
marker = Path(sys.argv[2])
_prepare_ledger_root(directory)
with _ledger_lock(directory, create=True):
    marker.write_text('locked', encoding='utf-8')
    os._exit(17)
"""
            process = subprocess.run(
                [sys.executable, "-B", "-c", code, str(directory), str(marker)],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(process.returncode, 17)
            self.assertTrue(marker.exists())
            self.record(directory, datetime(2026, 7, 17, 12, tzinfo=UTC))
            self.assertTrue(
                verify_observation_history(
                    directory, registry_entries=self.registry_entries
                )["valid"]
            )

    def test_prepublication_process_death_leaves_nonblocking_working_residue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "ledger"
            first = self.record(
                directory, datetime(2026, 7, 17, 12, tzinfo=UTC)
            )
            prior_bundle = first.path.read_bytes()
            prior_receipt = first.receipt_path.read_bytes()
            before = evaluate_operational_readiness(
                directory,
                registry_entries=self.registry_entries,
                requested_registry_ids=[self.entry.registry_id],
            )
            crashed = subprocess.run(
                self.crash_command(
                    directory,
                    datetime(2026, 7, 17, 13, tzinfo=UTC),
                    "before_bundle_publication",
                    23,
                ),
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(crashed.returncode, 23)
            self.assertEqual(len(list((directory / BUNDLES_DIR_NAME).glob("*.json"))), 1)
            self.assertEqual(len(list((directory / RECEIPTS_DIR_NAME).glob("*.json"))), 1)
            residue = list((directory / WORKING_DIR_NAME).iterdir())
            self.assertEqual(len(residue), 1)
            verification = verify_observation_history(
                directory, registry_entries=self.registry_entries
            )
            self.assertTrue(verification["valid"])
            self.assertTrue(verification["working_residue_present"])
            self.assertEqual(verification["working_residue_count"], 1)
            self.assertTrue(verification["warnings"])
            after_crash = evaluate_operational_readiness(
                directory,
                registry_entries=self.registry_entries,
                requested_registry_ids=[self.entry.registry_id],
            )
            self.assertEqual(after_crash["boards"], before["boards"])
            self.assertEqual(first.path.read_bytes(), prior_bundle)
            self.assertEqual(first.receipt_path.read_bytes(), prior_receipt)

            second = self.record(
                directory, datetime(2026, 7, 17, 14, tzinfo=UTC)
            )
            self.assertEqual(second.ledger_sequence, 2)
            self.assertFalse(list((directory / WORKING_DIR_NAME).iterdir()))
            self.assertEqual(first.path.read_bytes(), prior_bundle)
            self.assertEqual(first.receipt_path.read_bytes(), prior_receipt)
            self.assertTrue(
                verify_observation_history(
                    directory, registry_entries=self.registry_entries
                )["valid"]
            )

    def test_real_process_crash_publication_boundaries(self):
        cases = (
            ("after_bundle_publication", 24, 1, 0, False),
            ("after_receipt_publication", 25, 1, 1, True),
        )
        for point, exit_code, bundles, receipts, valid in cases:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as temp_dir:
                directory = Path(temp_dir) / "ledger"
                crashed = subprocess.run(
                    self.crash_command(
                        directory,
                        datetime(2026, 7, 17, 12, tzinfo=UTC),
                        point,
                        exit_code,
                    ),
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(crashed.returncode, exit_code)
                self.assertEqual(len(list((directory / BUNDLES_DIR_NAME).glob("*.json"))), bundles)
                self.assertEqual(len(list((directory / RECEIPTS_DIR_NAME).glob("*.json"))), receipts)
                self.assertEqual(
                    verify_observation_history(
                        directory, registry_entries=self.registry_entries
                    )["valid"],
                    valid,
                )


if __name__ == "__main__":
    unittest.main()
