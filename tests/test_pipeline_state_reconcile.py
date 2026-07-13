import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from wahojobs import (
    pipeline_actions,
    pipeline_reconciliation,
    pipeline_state,
    pipeline_transition_metadata,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "wahojobs" / "db" / "schema.sql"
MIGRATION = ROOT / "wahojobs" / "db" / "migrations" / "001_pipeline_state.sql"
SCRIPT = ROOT / "scripts" / "pipeline_state_reconcile.py"
sys.path.insert(0, str(ROOT / "scripts"))
import pipeline_state_migration as migration  # noqa: E402


class PipelineReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "reconcile.sqlite"
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        for statement in migration.iter_sql_statements(MIGRATION.read_text(encoding="utf-8")):
            self.conn.execute(statement)
        self.conn.execute(
            "INSERT INTO wahojobs_schema_migrations (version) VALUES (?)",
            (migration.MIGRATION_VERSION,),
        )
        self.conn.execute(
            "INSERT INTO user_profiles (profile_id,user_id,display_name) VALUES ('profile-a','user-a','Profile A')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def create(self, action="applied"):
        return pipeline_actions.perform_pipeline_action(
            self.conn,
            action=action,
            owner_profile_id="profile-a",
            idempotency_key=f"reconcile-{action}-000000001",
            expected_version=0,
            match_run_id="run-reconcile",
            source="Fixture",
            title=f"Reconcile {action}",
            url=f"https://example.test/reconcile/{action}",
        )

    def replace_transition_metadata(self, transition_id, metadata):
        self.replace_transition_fields(
            transition_id, metadata_json=json.dumps(metadata, sort_keys=True)
        )

    def replace_transition_fields(self, existing_transition_id, **fields):
        self.conn.commit()
        self.conn.execute("DROP TRIGGER trg_user_pipeline_transitions_no_update")
        assignments = ", ".join(f"{field}=?" for field in fields)
        self.conn.execute(
            f"UPDATE user_pipeline_transitions SET {assignments} WHERE transition_id=?",
            (*fields.values(), existing_transition_id),
        )
        self.conn.executescript(
            """
            CREATE TRIGGER trg_user_pipeline_transitions_no_update
            BEFORE UPDATE ON user_pipeline_transitions
            BEGIN SELECT RAISE(ABORT, 'user_pipeline_transitions is append-only'); END;
            """
        )
        self.conn.commit()

    def test_clean_reconciliation_and_json_cli_are_read_only(self):
        self.create()
        self.conn.commit()
        before = self.path.read_bytes()
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertFalse(report["blocking"])
        self.assertEqual(report["blocking_reasons"], [])
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--db", str(self.path), "--json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["blocking"])
        self.assertTrue(payload["fully_reconciled"])
        self.assertTrue(payload["safe_for_normalized_reads"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_compatibility_mirror_drift_remains_operationally_blocking_but_read_safe(self):
        result = self.create("applied")
        item_id = result.pipeline_item["pipeline_item_id"]
        self.conn.execute(
            "UPDATE user_pipeline_items SET status='saved', reminder_date='2099-01-01' "
            "WHERE pipeline_item_id=?",
            (item_id,),
        )
        self.conn.commit()

        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertTrue(report["blocking"])
        self.assertFalse(report["fully_reconciled"])
        self.assertTrue(report["safe_for_normalized_reads"])
        self.assertEqual(report["normalized_read_blocking_reasons"], [])
        self.assertEqual(
            report["compatibility_mirror_drift_reasons"],
            ["legacy_status_mismatches", "reminder_mirror_mismatches"],
        )
        self.assertTrue(pipeline_reconciliation.is_safe_for_normalized_reads(report))

        before = self.path.read_bytes()
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--db", str(self.path), "--json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["checks"]["legacy_status_mismatches"])
        self.assertTrue(payload["checks"]["reminder_mirror_mismatches"])
        self.assertTrue(payload["safe_for_normalized_reads"])

        human = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--db", str(self.path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(human.returncode, 1, human.stderr)
        self.assertIn("Fully reconciled: no", human.stdout)
        self.assertIn("Safe for normalized reads: yes", human.stdout)
        self.assertIn("legacy_status_mismatches", human.stdout)
        self.assertIn("reminder_mirror_mismatches", human.stdout)
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_migration_and_partial_objects_are_blocking(self):
        legacy = Path(self.temp_dir.name) / "legacy.sqlite"
        conn = sqlite3.connect(legacy)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        report = pipeline_reconciliation.reconcile_pipeline_state(conn)
        self.assertTrue(report["blocking"])
        self.assertIn("migration_schema_incomplete", report["blocking_reasons"])
        self.assertFalse(report["safe_for_normalized_reads"])
        conn.execute("CREATE TABLE user_pipeline_state (pipeline_item_id TEXT)")
        report = pipeline_reconciliation.reconcile_pipeline_state(conn)
        self.assertTrue(report["blocking"])
        conn.close()

    def test_detects_missing_projection(self):
        self.conn.execute(
            """
            INSERT INTO user_pipeline_items (
              pipeline_item_id,user_id,profile_id,source,opportunity_title,status
            ) VALUES ('missing','user-a','profile-a','Fixture','Missing','saved')
            """
        )
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertTrue(report["blocking"])
        self.assertEqual(report["checks"]["missing_projections"][0]["pipeline_item_id"], "missing")
        self.assertFalse(report["safe_for_normalized_reads"])

    def test_detects_version_latest_state_chain_and_mirror_drift(self):
        result = self.create("applied")
        item_id = result.pipeline_item["pipeline_item_id"]
        self.conn.execute(
            "UPDATE user_pipeline_state SET version=9, workflow_status='saved' WHERE pipeline_item_id=?",
            (item_id,),
        )
        self.conn.execute(
            "UPDATE user_pipeline_items SET status='not_interested', reminder_date='2099-01-01' WHERE pipeline_item_id=?",
            (item_id,),
        )
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertTrue(report["checks"]["projection_version_mismatches"])
        self.assertTrue(report["checks"]["latest_transition_state_mismatches"])
        self.assertTrue(report["checks"]["legacy_status_mismatches"])
        self.assertTrue(report["checks"]["reminder_mirror_mismatches"])

    def test_detects_owner_drift_and_missing_applicant_expectation(self):
        result = self.create("applied")
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("DROP TRIGGER trg_user_pipeline_transitions_no_update")
        self.conn.execute(
            "UPDATE user_pipeline_transitions SET profile_id='profile-b' WHERE transition_id=?",
            (result.transition["transition_id"],),
        )
        self.conn.executescript(
            """
            CREATE TRIGGER trg_user_pipeline_transitions_no_update
            BEFORE UPDATE ON user_pipeline_transitions
            BEGIN
              SELECT RAISE(ABORT, 'user_pipeline_transitions is append-only');
            END;
            """
        )
        self.conn.execute(
            "DELETE FROM applicant_status_updates WHERE update_id=?",
            (result.applicant_update["update_id"],),
        )
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertTrue(report["checks"]["owner_mismatches"])
        self.assertTrue(report["checks"]["applicant_update_expectation_mismatches"])

    def test_detects_visible_unknown_workflow(self):
        result = self.create("save")
        self.conn.execute(
            """
            UPDATE user_pipeline_state
            SET workflow_status=NULL, workflow_status_provenance='unknown_legacy', reminder_at=NULL
            WHERE pipeline_item_id=?
            """,
            (result.pipeline_item["pipeline_item_id"],),
        )
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertTrue(report["checks"]["visible_unresolved_workflows"])

    def test_detects_orphan_and_duplicate_projection_in_malformed_external_schema(self):
        result = self.create("save")
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("ALTER TABLE user_pipeline_state RENAME TO malformed_state_source")
        self.conn.execute(
            "CREATE TABLE user_pipeline_state AS SELECT * FROM malformed_state_source"
        )
        self.conn.execute(
            "INSERT INTO user_pipeline_state SELECT * FROM malformed_state_source"
        )
        self.conn.execute(
            """
            INSERT INTO user_pipeline_state (
              pipeline_item_id,workflow_status,workflow_status_provenance,visibility,
              reminder_at,version,created_at,updated_at
            ) VALUES ('orphan','saved','known','visible',NULL,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            """
        )
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertEqual(
            report["checks"]["duplicate_projections"][0]["pipeline_item_id"],
            result.pipeline_item["pipeline_item_id"],
        )
        self.assertEqual(report["checks"]["orphan_projections"][0]["pipeline_item_id"], "orphan")

    def test_detects_duplicate_ledger_keys_noncontiguous_chain_and_invalid_reference(self):
        self.create("save")
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self.conn.execute("ALTER TABLE user_pipeline_transitions RENAME TO malformed_transition_source")
        self.conn.execute(
            "CREATE TABLE user_pipeline_transitions AS SELECT * FROM malformed_transition_source"
        )
        self.conn.execute(
            "INSERT INTO user_pipeline_transitions SELECT * FROM malformed_transition_source"
        )
        self.conn.execute(
            """
            UPDATE user_pipeline_transitions
            SET state_version_before=8, state_version_after=9,
                undo_of_transition_id='missing-transition'
            WHERE rowid=(SELECT MAX(rowid) FROM user_pipeline_transitions)
            """
        )
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertTrue(report["checks"]["duplicate_transition_ids"])
        self.assertTrue(report["checks"]["duplicate_idempotency_keys"])
        self.assertTrue(report["checks"]["non_contiguous_version_chains"])
        self.assertTrue(report["checks"]["invalid_transition_references"])

    def test_reconciliation_compares_complete_latest_applicant_expectation(self):
        result = self.create("applied")
        update_id = result.applicant_update["update_id"]
        fields = {
            "status": "rejected",
            "notes": "changed",
            "source": "changed",
            "evidence_type": "changed",
            "confidence_level": "changed",
            "profile_id": "changed",
            "opportunity_title": "changed",
            "reported_at": "2099-01-01T00:00:00+00:00",
        }
        assignments = ", ".join(f"{field}=?" for field in fields)
        self.conn.execute(
            f"UPDATE applicant_status_updates SET {assignments} WHERE update_id=?",
            (*fields.values(), update_id),
        )
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        mismatch = report["checks"]["applicant_update_expectation_mismatches"]
        self.assertTrue(report["blocking"])
        self.assertEqual(mismatch[0]["reason"], "applicant_content_mismatch")
        self.assertTrue(set(fields).issubset(mismatch[0]["fields"]))

    def test_latest_legitimate_applicant_dedup_expectation_wins(self):
        first = self.create("applied")
        item_id = first.pipeline_item["pipeline_item_id"]
        corrected = pipeline_state.correct_state(
            self.conn,
            pipeline_item_id=item_id,
            owner_profile_id="profile-a",
            corrected_state={
                **first.state,
                "workflow_status": "saved",
                "workflow_status_provenance": "known",
            },
            correction_of_transition_id=first.transition["transition_id"],
            expected_version=first.state["version"],
            idempotency_key="reconcile-correction-000001",
            actor_source="test",
        )
        second = pipeline_actions.perform_pipeline_action(
            self.conn,
            action="applied",
            owner_profile_id="profile-a",
            idempotency_key="reconcile-second-apply-0001",
            expected_version=corrected.state["version"],
            match_run_id="run-reconcile",
            pipeline_item_id=item_id,
        )
        self.assertEqual(first.applicant_update["update_id"], second.applicant_update["update_id"])
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertEqual(report["checks"]["applicant_update_expectation_mismatches"], [])

    def test_malformed_applicant_snapshot_is_blocking(self):
        result = self.create("applied")
        self.conn.commit()
        self.conn.execute("DROP TRIGGER trg_user_pipeline_transitions_no_update")
        metadata = json.loads(
            self.conn.execute(
                "SELECT metadata_json FROM user_pipeline_transitions WHERE transition_id=?",
                (result.transition["transition_id"],),
            ).fetchone()[0]
        )
        metadata["pipeline_action"].pop("result_snapshot")
        self.conn.execute(
            "UPDATE user_pipeline_transitions SET metadata_json=? WHERE transition_id=?",
            (json.dumps(metadata, sort_keys=True), result.transition["transition_id"]),
        )
        self.conn.executescript(
            """
            CREATE TRIGGER trg_user_pipeline_transitions_no_update
            BEFORE UPDATE ON user_pipeline_transitions
            BEGIN SELECT RAISE(ABORT, 'user_pipeline_transitions is append-only'); END;
            """
        )
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertEqual(
            report["checks"]["applicant_update_expectation_mismatches"][0]["reason"],
            "malformed_terminal_operation_metadata",
        )

    def test_applicant_expectations_are_bound_to_terminal_action_and_receipt(self):
        applied = self.create("applied")
        started = pipeline_actions.perform_pipeline_action(
            self.conn,
            action="assessment_started",
            owner_profile_id="profile-a",
            idempotency_key="binding-assessment-started-001",
            expected_version=applied.state["version"],
            match_run_id="run-reconcile",
            pipeline_item_id=applied.pipeline_item["pipeline_item_id"],
        )
        metadata = copy.deepcopy(started.transition["metadata"])
        metadata["pipeline_action"]["result_snapshot"]["applicant_update"] = copy.deepcopy(
            applied.applicant_update
        )
        metadata["pipeline_action"]["applicant_receipt"]["applicant_update"] = {
            field: applied.applicant_update[field]
            for field in pipeline_actions.deterministic_applicant_fields()
        }
        self.replace_transition_metadata(started.transition["transition_id"], metadata)
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        reasons = {
            row["reason"]
            for row in report["checks"]["applicant_update_expectation_mismatches"]
        }
        self.assertTrue(report["blocking"])
        self.assertIn("applicant_action_binding_mismatch", reasons)
        self.assertTrue(report["checks"]["invalid_terminal_operation_metadata"])

    def test_applicant_receipt_identity_and_presence_tampering_is_blocking(self):
        cases = {
            "wrong_update_id": lambda metadata: (
                metadata["pipeline_action"]["result_snapshot"]["applicant_update"].__setitem__(
                    "update_id", "applicant-update::wrong"
                ),
                metadata["pipeline_action"]["applicant_receipt"]["applicant_update"].__setitem__(
                    "update_id", "applicant-update::wrong"
                ),
            ),
            "wrong_profile": lambda metadata: (
                metadata["pipeline_action"]["result_snapshot"]["applicant_update"].__setitem__(
                    "profile_id", "profile-wrong"
                ),
                metadata["pipeline_action"]["applicant_receipt"]["applicant_update"].__setitem__(
                    "profile_id", "profile-wrong"
                ),
            ),
            "wrong_opportunity": lambda metadata: (
                metadata["pipeline_action"]["result_snapshot"]["applicant_update"].__setitem__(
                    "opportunity_title", "Wrong opportunity"
                ),
                metadata["pipeline_action"]["applicant_receipt"]["applicant_update"].__setitem__(
                    "opportunity_title", "Wrong opportunity"
                ),
            ),
            "receipt_result_disagreement": lambda metadata: metadata["pipeline_action"][
                "applicant_receipt"
            ]["applicant_update"].__setitem__("notes", "different"),
            "missing_receipt": lambda metadata: metadata["pipeline_action"].__setitem__(
                "applicant_receipt", None
            ),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                result = pipeline_actions.perform_pipeline_action(
                    self.conn,
                    action="applied",
                    owner_profile_id="profile-a",
                    idempotency_key=f"receipt-{label}-000000001",
                    expected_version=0,
                    match_run_id="run-reconcile",
                    source="Fixture",
                    title=f"Receipt {label}",
                    url=f"https://example.test/receipt/{label}",
                )
                metadata = copy.deepcopy(result.transition["metadata"])
                mutate(metadata)
                self.replace_transition_metadata(result.transition["transition_id"], metadata)
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertTrue(report["blocking"])
        self.assertGreaterEqual(
            len(report["checks"]["applicant_update_expectation_mismatches"]),
            len(cases),
        )

    def test_accepted_operation_cannot_reference_a_valid_rejected_applicant_row(self):
        def create_terminal(label, terminal):
            result = pipeline_actions.perform_pipeline_action(
                self.conn,
                action="applied",
                owner_profile_id="profile-a",
                idempotency_key=f"terminal-{label}-applied-000001",
                expected_version=0,
                match_run_id="run-reconcile",
                source="Fixture",
                title=f"Terminal {label}",
                url=f"https://example.test/terminal/{label}",
            )
            for action in ("assessment_started", "assessment_completed", terminal):
                result = pipeline_actions.perform_pipeline_action(
                    self.conn,
                    action=action,
                    owner_profile_id="profile-a",
                    idempotency_key=f"terminal-{label}-{action}-000001",
                    expected_version=result.state["version"],
                    match_run_id="run-reconcile",
                    pipeline_item_id=result.pipeline_item["pipeline_item_id"],
                )
            return result

        accepted = create_terminal("accepted", "accepted")
        rejected = create_terminal("rejected", "rejected")
        metadata = copy.deepcopy(accepted.transition["metadata"])
        metadata["pipeline_action"]["result_snapshot"]["applicant_update"] = copy.deepcopy(
            rejected.applicant_update
        )
        metadata["pipeline_action"]["applicant_receipt"]["applicant_update"] = {
            field: rejected.applicant_update[field]
            for field in pipeline_actions.deterministic_applicant_fields()
        }
        self.replace_transition_metadata(accepted.transition["transition_id"], metadata)
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertTrue(report["blocking"])
        self.assertTrue(report["checks"]["applicant_update_expectation_mismatches"])

    def test_protected_metadata_contracts_fail_closed_and_cli_exits_nonzero(self):
        donor = self.create("applied")

        noop_mutations = {
            "missing_pipeline_action": lambda metadata: metadata.pop("pipeline_action"),
            "missing_fingerprint": lambda metadata: metadata["pipeline_action"].pop(
                "fingerprint"
            ),
            "missing_result_snapshot": lambda metadata: metadata["pipeline_action"].pop(
                "result_snapshot"
            ),
            "missing_applicant_receipt": lambda metadata: metadata["pipeline_action"].pop(
                "applicant_receipt"
            ),
            "forbidden_applicant_receipt": lambda metadata: metadata["pipeline_action"].__setitem__(
                "applicant_receipt",
                copy.deepcopy(donor.transition["metadata"]["pipeline_action"]["applicant_receipt"]),
            ),
        }
        for label, mutate in noop_mutations.items():
            result = pipeline_actions.perform_pipeline_action(
                self.conn,
                action="save",
                owner_profile_id="profile-a",
                idempotency_key=f"protected-noop-{label}-00001",
                expected_version=0,
                match_run_id="run-reconcile",
                source="Fixture",
                title=f"Protected no-op {label}",
                url=f"https://example.test/protected-noop/{label}",
            )
            metadata = copy.deepcopy(result.transition["metadata"])
            mutate(metadata)
            self.replace_transition_metadata(result.transition["transition_id"], metadata)

        initialization_mutations = {
            "missing_first_action": lambda metadata: metadata.pop("first_requested_action"),
            "missing_fingerprint": lambda metadata: metadata.pop("product_action_fingerprint"),
            "missing_kind": lambda metadata: metadata.pop("initialization_kind"),
        }
        for label, mutate in initialization_mutations.items():
            result = pipeline_actions.perform_pipeline_action(
                self.conn,
                action="save",
                owner_profile_id="profile-a",
                idempotency_key=f"protected-init-{label}-0000001",
                expected_version=0,
                match_run_id="run-reconcile",
                source="Fixture",
                title=f"Protected init {label}",
                url=f"https://example.test/protected-init/{label}",
            )
            history = pipeline_state.list_transition_history(
                self.conn, result.pipeline_item["pipeline_item_id"], "profile-a"
            )
            metadata = copy.deepcopy(history[0]["metadata"])
            mutate(metadata)
            self.replace_transition_metadata(history[0]["transition_id"], metadata)

        wrong_position = pipeline_actions.perform_pipeline_action(
            self.conn,
            action="save",
            owner_profile_id="profile-a",
            idempotency_key="protected-init-wrong-position-0001",
            expected_version=0,
            match_run_id="run-reconcile",
            source="Fixture",
            title="Protected init wrong position",
            url="https://example.test/protected-init/wrong-position",
        )
        wrong_history = pipeline_state.list_transition_history(
            self.conn, wrong_position.pipeline_item["pipeline_item_id"], "profile-a"
        )
        self.replace_transition_metadata(
            wrong_history[-1]["transition_id"], copy.deepcopy(wrong_history[0]["metadata"])
        )

        for label, mutation in (
            ("empty", lambda metadata: metadata.clear()),
            ("wrong_identity", lambda metadata: metadata.__setitem__("legacy_snapshot", False)),
            ("incompatible", lambda metadata: metadata.__setitem__("raw_legacy_status", "applied")),
        ):
            item_id = f"legacy-protected-{label}"
            self.conn.execute(
                """
                INSERT INTO user_pipeline_items (
                  pipeline_item_id,user_id,profile_id,source,opportunity_title,status,status_date
                ) VALUES (?, 'user-a','profile-a','Legacy',?,'saved','2026-07-01')
                """,
                (item_id, f"Legacy {label}"),
            )
            state, metadata, _ = pipeline_state.legacy_projection(
                {"status": "saved", "reminder_date": ""}
            )
            initialized = pipeline_state.initialize_projection(
                self.conn,
                pipeline_item_id=item_id,
                owner_profile_id="profile-a",
                workflow_status=state["workflow_status"],
                workflow_status_provenance=state["workflow_status_provenance"],
                visibility=state["visibility"],
                reminder_at=state["reminder_at"],
                idempotency_key=f"legacy-baseline:v1:{item_id}",
                action_name="legacy_snapshot",
                actor_source="legacy_migration",
                metadata=metadata,
            )
            mutation(metadata)
            self.replace_transition_metadata(initialized.transition["transition_id"], metadata)

        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertTrue(report["blocking"])
        self.assertGreaterEqual(len(report["checks"]["invalid_terminal_operation_metadata"]), 5)
        self.assertGreaterEqual(len(report["checks"]["invalid_initialization_transitions"]), 7)
        self.conn.commit()
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--db", str(self.path), "--json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["blocking"])
        self.assertTrue(payload["checks"]["invalid_terminal_operation_metadata"])
        human = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--db", str(self.path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(human.returncode, 1)
        self.assertIn("Invalid Terminal Operation Metadata", human.stdout)

    def test_noop_action_state_binding_rejects_coherent_relabels(self):
        def create_item(action, label):
            return pipeline_actions.perform_pipeline_action(
                self.conn,
                action=action,
                owner_profile_id="profile-a",
                idempotency_key=f"noop-binding-{label}-create-00001",
                expected_version=0,
                match_run_id="run-reconcile",
                source="Fixture",
                title=f"No-op binding {label}",
                url=f"https://example.test/noop-binding/{label}",
            )

        def repeat(result, action, label, **kwargs):
            return pipeline_actions.perform_pipeline_action(
                self.conn,
                action=action,
                owner_profile_id="profile-a",
                idempotency_key=f"noop-binding-{label}-repeat-0001",
                expected_version=result.state["version"],
                match_run_id="run-reconcile",
                pipeline_item_id=result.pipeline_item["pipeline_item_id"],
                **kwargs,
            )

        def relabel(result, action, *, reminder_at=None):
            metadata = copy.deepcopy(result.transition["metadata"])
            product = metadata["pipeline_action"]
            request = product["operation_request"]
            product["action"] = action
            request["action"] = action
            request["reminder_at"] = reminder_at
            request["requested_effect"] = pipeline_transition_metadata.requested_effect(
                action, reminder_at
            )
            effect = pipeline_transition_metadata.APPLICANT_EFFECTS.get(action)
            request["applicant_payload"] = (
                {
                    "status": effect["status"],
                    "evidence_type": effect["evidence_type"],
                    "confidence_level": effect["confidence_level"],
                    "note": request["note"],
                }
                if effect is not None
                else None
            )
            product["fingerprint"] = pipeline_transition_metadata.request_fingerprint(
                request
            )
            self.replace_transition_fields(
                result.transition["transition_id"],
                metadata_json=json.dumps(metadata, sort_keys=True),
                action_name=f"product_noop_{action}",
            )

        saved = create_item("save", "saved-as-applied")
        relabel(saved, "applied")

        applied = create_item("applied", "applied-as-started")
        applied_repeat = repeat(applied, "applied", "applied-as-started")
        relabel(applied_repeat, "assessment_started")

        reminder = create_item("applied", "wrong-reminder")
        reminder = repeat(
            reminder,
            "remind_later",
            "set-reminder",
            reminder_at="2026-09-01T12:00:00+00:00",
        )
        reminder_repeat = repeat(
            reminder,
            "remind_later",
            "wrong-reminder",
            reminder_at="2026-09-01T12:00:00+00:00",
        )
        relabel(reminder_repeat, "remind_later", reminder_at="2026-10-01T12:00:00+00:00")

        visible = create_item("save", "visible-as-hidden")
        relabel(visible, "not_interested")

        visible_applied = create_item("applied", "applied-as-show")
        visible_applied_repeat = repeat(
            visible_applied, "applied", "applied-as-show"
        )
        relabel(visible_applied_repeat, "show_again")

        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        reasons = {
            row.get("reason")
            for row in report["checks"]["invalid_terminal_operation_metadata"]
        }
        self.assertTrue(report["blocking"])
        self.assertEqual(reasons, {"noop_state_binding_mismatch"})

    def test_valid_legacy_accepted_noops_share_contract_and_reconcile_cleanly(self):
        counter = 0

        def create(action, label):
            nonlocal counter
            counter += 1
            return pipeline_actions.perform_pipeline_action(
                self.conn,
                action=action,
                owner_profile_id="profile-a",
                idempotency_key=f"valid-noop-create-{counter:02d}-0000001",
                expected_version=0,
                match_run_id="run-reconcile",
                source="Fixture",
                title=f"Valid no-op {label}",
                url=f"https://example.test/valid-noop/{label}",
            )

        def act(result, action, **kwargs):
            nonlocal counter
            counter += 1
            return pipeline_actions.perform_pipeline_action(
                self.conn,
                action=action,
                owner_profile_id="profile-a",
                idempotency_key=f"valid-noop-action-{counter:02d}-000001",
                expected_version=result.state["version"],
                match_run_id="run-reconcile",
                pipeline_item_id=result.pipeline_item["pipeline_item_id"],
                **kwargs,
            )

        saved = create("save", "saved")
        act(saved, "show_again")
        applied = create("applied", "applied")
        applied = act(applied, "applied")
        started = act(applied, "assessment_started")
        started = act(started, "assessment_started")
        completed = act(started, "assessment_completed")
        completed = act(completed, "assessment_completed")
        accepted = act(completed, "accepted")
        act(accepted, "accepted")
        rejected_base = create("applied", "rejected")
        rejected_base = act(rejected_base, "assessment_started")
        rejected_base = act(rejected_base, "assessment_completed")
        rejected = act(rejected_base, "rejected")
        act(rejected, "rejected")
        reminder_base = create("applied", "reminder")
        reminded = act(
            reminder_base,
            "remind_later",
            reminder_at="2026-09-01T12:00:00+00:00",
        )
        act(reminded, "remind_later", reminder_at="2026-09-01T12:00:00+00:00")
        hidden = create("not_interested", "hidden")
        act(hidden, "not_interested")
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertFalse(report["blocking"], report["blocking_reasons"])

    def test_user_initialization_is_bound_to_terminal_action_fingerprint_and_key(self):
        self.conn.execute(
            "INSERT INTO user_profiles (profile_id,user_id,display_name) VALUES ('profile-b','user-b','Profile B')"
        )

        def create_save(label, owner="profile-a"):
            return pipeline_actions.perform_pipeline_action(
                self.conn,
                action="save",
                owner_profile_id=owner,
                idempotency_key=f"init-binding-{label}-000000001",
                expected_version=0,
                match_run_id="run-reconcile",
                source="Fixture",
                title=f"Init binding {label}",
                url=f"https://example.test/init-binding/{label}",
            )

        def history_for(result, owner="profile-a"):
            return pipeline_state.list_transition_history(
                self.conn, result.pipeline_item["pipeline_item_id"], owner
            )

        def replace_with_structurally_valid_wrong_key(result, wrong_key):
            history = history_for(result)
            initialization = history[0]
            terminal = history[-1]
            replacement_id = pipeline_state.stable_transition_id(
                result.pipeline_item["pipeline_item_id"], wrong_key
            )
            self.replace_transition_fields(
                initialization["transition_id"],
                transition_id=replacement_id,
                idempotency_key=wrong_key,
            )
            metadata = copy.deepcopy(terminal["metadata"])
            metadata["pipeline_action"]["result_snapshot"][
                "preparatory_transition_ids"
            ] = [replacement_id]
            request_fingerprint = pipeline_transition_metadata.request_fingerprint(
                {
                    "operation": "operation_noop",
                    "pipeline_item_id": terminal["pipeline_item_id"],
                    "owner_profile_id": terminal["profile_id"],
                    "action_name": terminal["action_name"],
                    "expected_version": terminal["state_version_before"],
                    "actor_source": terminal["actor_source"],
                    "metadata": metadata,
                }
            )
            self.replace_transition_fields(
                terminal["transition_id"],
                metadata_json=json.dumps(metadata, sort_keys=True),
                request_fingerprint=request_fingerprint,
            )

        unsupported = create_save("unsupported")
        unsupported_history = history_for(unsupported)
        metadata = copy.deepcopy(unsupported_history[0]["metadata"])
        metadata["first_requested_action"] = "assessment_started"
        self.replace_transition_metadata(unsupported_history[0]["transition_id"], metadata)

        wrong_action = create_save("wrong-action")
        wrong_history = history_for(wrong_action)
        metadata = copy.deepcopy(wrong_history[0]["metadata"])
        metadata["first_requested_action"] = "applied"
        self.replace_transition_metadata(wrong_history[0]["transition_id"], metadata)

        wrong_fingerprint = create_save("wrong-fingerprint")
        fingerprint_history = history_for(wrong_fingerprint)
        metadata = copy.deepcopy(fingerprint_history[0]["metadata"])
        metadata["product_action_fingerprint"] = "0" * 64
        self.replace_transition_metadata(fingerprint_history[0]["transition_id"], metadata)

        missing_link = create_save("missing-link")
        missing_history = history_for(missing_link)
        terminal_metadata = copy.deepcopy(missing_history[-1]["metadata"])
        terminal_metadata["pipeline_action"]["result_snapshot"][
            "preparatory_transition_ids"
        ] = []
        self.replace_transition_metadata(missing_history[-1]["transition_id"], terminal_metadata)

        wrong_key = create_save("wrong-key")
        replace_with_structurally_valid_wrong_key(
            wrong_key,
            pipeline_transition_metadata.INTERNAL_IDEMPOTENCY_PREFIX + "0" * 64,
        )

        other_action_key = create_save("other-action-key")
        other_action_history = history_for(other_action_key)
        other_action_terminal = other_action_history[-1]
        applied_fingerprint = pipeline_transition_metadata.request_fingerprint(
            {
                **other_action_terminal["metadata"]["pipeline_action"][
                    "operation_request"
                ],
                "action": "applied",
            }
        )
        replace_with_structurally_valid_wrong_key(
            other_action_key,
            pipeline_transition_metadata.derive_internal_idempotency_key(
                caller_key=other_action_terminal["idempotency_key"],
                operation_fingerprint=applied_fingerprint,
                step="initialize",
                pipeline_item_id=other_action_key.pipeline_item["pipeline_item_id"],
            ),
        )

        other_item_key = create_save("other-item-key")
        other_item_history = history_for(other_item_key)
        other_item_terminal = other_item_history[-1]
        replace_with_structurally_valid_wrong_key(
            other_item_key,
            pipeline_transition_metadata.derive_internal_idempotency_key(
                caller_key=other_item_terminal["idempotency_key"],
                operation_fingerprint=other_item_terminal["metadata"][
                    "pipeline_action"
                ]["fingerprint"],
                step="initialize",
                pipeline_item_id="pipeline-item-from-another-request",
            ),
        )

        ambiguous = create_save("ambiguous")
        ambiguous_repeat = pipeline_actions.perform_pipeline_action(
            self.conn,
            action="show_again",
            owner_profile_id="profile-a",
            idempotency_key="init-binding-ambiguous-repeat-001",
            expected_version=ambiguous.state["version"],
            match_run_id="run-reconcile",
            pipeline_item_id=ambiguous.pipeline_item["pipeline_item_id"],
        )
        ambiguous_history = history_for(ambiguous)
        metadata = copy.deepcopy(ambiguous_repeat.transition["metadata"])
        metadata["pipeline_action"]["result_snapshot"][
            "preparatory_transition_ids"
        ] = [ambiguous_history[0]["transition_id"]]
        self.replace_transition_metadata(ambiguous_repeat.transition["transition_id"], metadata)

        cross_item = create_save("cross-item")
        cross_owner = create_save("cross-owner", owner="profile-b")
        cross_item_history = history_for(cross_item)
        cross_owner_history = history_for(cross_owner, owner="profile-b")
        metadata = copy.deepcopy(cross_item_history[-1]["metadata"])
        metadata["pipeline_action"]["result_snapshot"][
            "preparatory_transition_ids"
        ] = []
        self.replace_transition_metadata(cross_item_history[-1]["transition_id"], metadata)
        metadata = copy.deepcopy(cross_owner_history[-1]["metadata"])
        metadata["pipeline_action"]["result_snapshot"][
            "preparatory_transition_ids"
        ] = [cross_item_history[0]["transition_id"]]
        self.replace_transition_metadata(cross_owner_history[-1]["transition_id"], metadata)

        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        reasons = {
            row.get("reason")
            for row in report["checks"]["invalid_initialization_transitions"]
        }
        self.assertTrue(report["blocking"])
        self.assertTrue(
            {
                "user_initialization_action_binding_mismatch",
                "user_initialization_fingerprint_mismatch",
                "user_initialization_terminal_link_missing",
                "user_initialization_terminal_link_ambiguous",
                "user_initialization_internal_key_mismatch",
            }.issubset(reasons),
            reasons,
        )
        self.assertTrue(
            {
                "result_snapshot_item_mismatch",
                "result_snapshot_transition_identity_mismatch",
            }
            & reasons,
            reasons,
        )

    def test_valid_supported_creation_bindings_reconcile_cleanly(self):
        for index, action in enumerate(("save", "applied", "not_interested"), 1):
            pipeline_actions.perform_pipeline_action(
                self.conn,
                action=action,
                owner_profile_id="profile-a",
                idempotency_key=f"valid-creation-binding-{index}-00001",
                expected_version=0,
                match_run_id="run-reconcile",
                source="Fixture",
                title=f"Valid creation binding {action}",
                url=f"https://example.test/valid-creation/{action}",
            )
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertFalse(report["blocking"], report["blocking_reasons"])

    def test_human_cli_prints_safe_distinct_reason_codes(self):
        def create_save(label, note=""):
            return pipeline_actions.perform_pipeline_action(
                self.conn,
                action="save",
                owner_profile_id="profile-a",
                idempotency_key=f"human-reason-{label}-000001",
                expected_version=0,
                match_run_id="run-reconcile",
                source="Fixture",
                title=f"Human reason {label}",
                url=f"https://example.test/human-reason/{label}",
                note=note,
            )

        applied = self.create("applied")
        started = pipeline_actions.perform_pipeline_action(
            self.conn,
            action="assessment_started",
            owner_profile_id="profile-a",
            idempotency_key="human-reason-started-000001",
            expected_version=applied.state["version"],
            match_run_id="run-reconcile",
            pipeline_item_id=applied.pipeline_item["pipeline_item_id"],
        )
        metadata = copy.deepcopy(started.transition["metadata"])
        metadata["pipeline_action"]["result_snapshot"]["applicant_update"] = copy.deepcopy(
            applied.applicant_update
        )
        metadata["pipeline_action"]["applicant_receipt"]["applicant_update"] = {
            field: applied.applicant_update[field]
            for field in pipeline_actions.deterministic_applicant_fields()
        }
        self.replace_transition_metadata(started.transition["transition_id"], metadata)

        noop = create_save("noop")
        noop_metadata = copy.deepcopy(noop.transition["metadata"])
        product = noop_metadata["pipeline_action"]
        request = product["operation_request"]
        product["action"] = "applied"
        request["action"] = "applied"
        request["requested_effect"] = pipeline_transition_metadata.requested_effect(
            "applied", None
        )
        effect = pipeline_transition_metadata.APPLICANT_EFFECTS["applied"]
        request["applicant_payload"] = {
            "status": effect["status"],
            "evidence_type": effect["evidence_type"],
            "confidence_level": effect["confidence_level"],
            "note": request["note"],
        }
        product["fingerprint"] = pipeline_transition_metadata.request_fingerprint(request)
        self.replace_transition_fields(
            noop.transition["transition_id"],
            metadata_json=json.dumps(noop_metadata, sort_keys=True),
            action_name="product_noop_applied",
        )

        action_binding = create_save("action-binding")
        action_history = pipeline_state.list_transition_history(
            self.conn, action_binding.pipeline_item["pipeline_item_id"], "profile-a"
        )
        action_metadata = copy.deepcopy(action_history[0]["metadata"])
        action_metadata["first_requested_action"] = "assessment_started"
        self.replace_transition_metadata(action_history[0]["transition_id"], action_metadata)

        missing_link = create_save("missing-link")
        missing_history = pipeline_state.list_transition_history(
            self.conn, missing_link.pipeline_item["pipeline_item_id"], "profile-a"
        )
        missing_metadata = copy.deepcopy(missing_history[-1]["metadata"])
        missing_metadata["pipeline_action"]["result_snapshot"][
            "preparatory_transition_ids"
        ] = []
        self.replace_transition_metadata(missing_history[-1]["transition_id"], missing_metadata)

        ambiguous = create_save("ambiguous")
        ambiguous_repeat = pipeline_actions.perform_pipeline_action(
            self.conn,
            action="show_again",
            owner_profile_id="profile-a",
            idempotency_key="human-reason-ambiguous-repeat-001",
            expected_version=ambiguous.state["version"],
            match_run_id="run-reconcile",
            pipeline_item_id=ambiguous.pipeline_item["pipeline_item_id"],
        )
        ambiguous_history = pipeline_state.list_transition_history(
            self.conn, ambiguous.pipeline_item["pipeline_item_id"], "profile-a"
        )
        ambiguous_metadata = copy.deepcopy(ambiguous_repeat.transition["metadata"])
        ambiguous_metadata["pipeline_action"]["result_snapshot"][
            "preparatory_transition_ids"
        ] = [ambiguous_history[0]["transition_id"]]
        self.replace_transition_metadata(
            ambiguous_repeat.transition["transition_id"], ambiguous_metadata
        )

        initialization = create_save(
            "initialization", note="SECRET_NOTE_MUST_NOT_APPEAR"
        )
        history = pipeline_state.list_transition_history(
            self.conn, initialization.pipeline_item["pipeline_item_id"], "profile-a"
        )
        self.replace_transition_fields(
            history[0]["transition_id"],
            idempotency_key=(
                pipeline_transition_metadata.INTERNAL_IDEMPOTENCY_PREFIX + "0" * 64
            ),
        )
        self.conn.commit()
        before = self.path.read_bytes()
        json_result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--db", str(self.path), "--json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        human_result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--db", str(self.path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(json_result.returncode, 1)
        self.assertEqual(human_result.returncode, 1)
        payload = json.loads(json_result.stdout)
        json_reasons = {
            row.get("reason")
            for rows in payload["checks"].values()
            for row in rows
            if isinstance(row, dict)
        }
        for reason in (
            "applicant_action_binding_mismatch",
            "noop_state_binding_mismatch",
            "user_initialization_action_binding_mismatch",
            "user_initialization_internal_key_mismatch",
            "user_initialization_terminal_link_missing",
            "user_initialization_terminal_link_ambiguous",
        ):
            self.assertIn(reason, json_reasons)
            self.assertIn(reason, human_result.stdout)
        self.assertIn(initialization.pipeline_item["pipeline_item_id"], human_result.stdout)
        self.assertIn(history[0]["transition_id"], human_result.stdout)
        self.assertNotIn("SECRET_NOTE_MUST_NOT_APPEAR", human_result.stdout)
        self.assertNotIn('"result_snapshot":', human_result.stdout)
        self.assertNotIn('"applicant_receipt":', human_result.stdout)
        self.assertNotRegex(human_result.stdout, r"\b[0-9a-f]{64}\b")
        self.assertEqual(self.path.read_bytes(), before)

    def test_clean_cli_output_is_deterministic_and_read_only(self):
        self.create("applied")
        self.conn.commit()
        before = self.path.read_bytes()
        outputs = {}
        for mode, extra in (("human", []), ("json", ["--json"])):
            runs = [
                subprocess.run(
                    [sys.executable, "-B", str(SCRIPT), "--db", str(self.path), *extra],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                for _ in range(2)
            ]
            self.assertEqual([run.returncode for run in runs], [0, 0])
            self.assertEqual(runs[0].stdout, runs[1].stdout)
            self.assertEqual(runs[0].stderr, runs[1].stderr)
            outputs[mode] = runs[0].stdout
        self.assertIn("Blocking drift: none", outputs["human"])
        self.assertFalse(json.loads(outputs["json"])["blocking"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_complete_chain_semantics_detect_distinct_malformed_categories(self):
        result = self.create("applied")
        item_id = result.pipeline_item["pipeline_item_id"]
        history = self.conn.execute(
            "SELECT * FROM user_pipeline_transitions WHERE pipeline_item_id=? ORDER BY state_version_after",
            (item_id,),
        ).fetchall()
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute("DROP TRIGGER trg_user_pipeline_transitions_no_update")
        malformed_before = json.loads(history[-1]["before_state_json"])
        malformed_before["visibility"] = "hidden"
        self.conn.execute(
            """
            UPDATE user_pipeline_transitions
            SET before_state_json=?, correction_of_transition_id=?
            WHERE transition_id=?
            """,
            (
                json.dumps(malformed_before, sort_keys=True),
                history[0]["transition_id"],
                history[-1]["transition_id"],
            ),
        )
        self.conn.executescript(
            """
            CREATE TRIGGER trg_user_pipeline_transitions_no_update
            BEFORE UPDATE ON user_pipeline_transitions
            BEGIN SELECT RAISE(ABORT, 'user_pipeline_transitions is append-only'); END;
            """
        )
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertTrue(report["checks"]["transition_before_state_mismatches"])
        self.assertEqual(
            report["checks"]["invalid_correction_references"][0]["reason"],
            "user_initialization_reference",
        )

    def test_malformed_json_and_impossible_dimension_changes_fail_closed(self):
        result = self.create("applied")
        item_id = result.pipeline_item["pipeline_item_id"]
        history = self.conn.execute(
            "SELECT * FROM user_pipeline_transitions WHERE pipeline_item_id=? ORDER BY state_version_after",
            (item_id,),
        ).fetchall()
        self.conn.commit()
        self.conn.execute("DROP TRIGGER trg_user_pipeline_transitions_no_update")
        self.conn.execute(
            "UPDATE user_pipeline_transitions SET before_state_json='{' WHERE transition_id=?",
            (history[-1]["transition_id"],),
        )
        self.conn.executescript(
            """
            CREATE TRIGGER trg_user_pipeline_transitions_no_update
            BEFORE UPDATE ON user_pipeline_transitions
            BEGIN SELECT RAISE(ABORT, 'user_pipeline_transitions is append-only'); END;
            """
        )
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertTrue(report["checks"]["malformed_transition_states"])

        other = pipeline_actions.perform_pipeline_action(
            self.conn,
            action="applied",
            owner_profile_id="profile-a",
            idempotency_key="reconcile-other-applied-0001",
            expected_version=0,
            match_run_id="run-reconcile",
            source="Fixture",
            title="Other reconcile applied",
            url="https://example.test/reconcile/other-applied",
        )
        other_history = self.conn.execute(
            "SELECT * FROM user_pipeline_transitions WHERE pipeline_item_id=? ORDER BY state_version_after",
            (other.pipeline_item["pipeline_item_id"],),
        ).fetchall()
        self.conn.commit()
        self.conn.execute("DROP TRIGGER trg_user_pipeline_transitions_no_update")
        after = json.loads(other_history[-1]["after_state_json"])
        after["visibility"] = "hidden"
        self.conn.execute(
            "UPDATE user_pipeline_transitions SET after_state_json=? WHERE transition_id=?",
            (json.dumps(after, sort_keys=True), other_history[-1]["transition_id"]),
        )
        self.conn.executescript(
            """
            CREATE TRIGGER trg_user_pipeline_transitions_no_update
            BEFORE UPDATE ON user_pipeline_transitions
            BEGIN SELECT RAISE(ABORT, 'user_pipeline_transitions is append-only'); END;
            """
        )
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertTrue(report["checks"]["invalid_transition_dimensions"])
        self.assertTrue(report["checks"]["latest_transition_state_mismatches"])

    def test_reference_policy_detects_invalid_undo_branching_and_cycles(self):
        first = self.create("applied")
        first_history = self.conn.execute(
            "SELECT * FROM user_pipeline_transitions WHERE pipeline_item_id=? ORDER BY state_version_after",
            (first.pipeline_item["pipeline_item_id"],),
        ).fetchall()
        second = pipeline_actions.perform_pipeline_action(
            self.conn,
            action="applied",
            owner_profile_id="profile-a",
            idempotency_key="reference-second-applied-0001",
            expected_version=0,
            match_run_id="run-reconcile",
            source="Fixture",
            title="Reference second",
            url="https://example.test/reference-second",
        )
        second = pipeline_actions.perform_pipeline_action(
            self.conn,
            action="assessment_started",
            owner_profile_id="profile-a",
            idempotency_key="reference-second-started-001",
            expected_version=second.state["version"],
            match_run_id="run-reconcile",
            pipeline_item_id=second.pipeline_item["pipeline_item_id"],
        )
        second = pipeline_actions.perform_pipeline_action(
            self.conn,
            action="assessment_completed",
            owner_profile_id="profile-a",
            idempotency_key="reference-second-complete-01",
            expected_version=second.state["version"],
            match_run_id="run-reconcile",
            pipeline_item_id=second.pipeline_item["pipeline_item_id"],
        )
        second_history = self.conn.execute(
            "SELECT * FROM user_pipeline_transitions WHERE pipeline_item_id=? ORDER BY state_version_after",
            (second.pipeline_item["pipeline_item_id"],),
        ).fetchall()
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute("DROP TRIGGER trg_user_pipeline_transitions_no_update")
        self.conn.execute(
            "UPDATE user_pipeline_transitions SET undo_of_transition_id=? WHERE transition_id=?",
            (first_history[0]["transition_id"], first_history[-1]["transition_id"]),
        )
        branch_parent = second_history[1]["transition_id"]
        self.conn.execute(
            "UPDATE user_pipeline_transitions SET correction_of_transition_id=? WHERE transition_id IN (?,?)",
            (
                branch_parent,
                second_history[2]["transition_id"],
                second_history[3]["transition_id"],
            ),
        )
        self.conn.execute(
            "UPDATE user_pipeline_transitions SET correction_of_transition_id=? WHERE transition_id=?",
            (second_history[3]["transition_id"], branch_parent),
        )
        self.conn.executescript(
            """
            CREATE TRIGGER trg_user_pipeline_transitions_no_update
            BEFORE UPDATE ON user_pipeline_transitions
            BEGIN SELECT RAISE(ABORT, 'user_pipeline_transitions is append-only'); END;
            """
        )
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertEqual(
            report["checks"]["invalid_undo_references"][0]["reason"],
            "user_initialization_reference",
        )
        self.assertTrue(report["checks"]["branching_transition_references"])
        self.assertTrue(report["checks"]["cyclic_transition_references"])

    def test_transition_class_counts_separate_baselines_initializations_and_noops(self):
        self.create("save")
        report = pipeline_reconciliation.reconcile_pipeline_state(self.conn)
        self.assertEqual(
            report["transition_classes"],
            {
                "migration_baselines": 0,
                "user_initializations": 1,
                "operation_noops": 1,
            },
        )


if __name__ == "__main__":
    unittest.main()
