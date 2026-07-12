import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from wahojobs import pipeline_state


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "wahojobs" / "db" / "schema.sql"
MIGRATION_DDL_PATH = ROOT / "wahojobs" / "db" / "migrations" / "001_pipeline_state.sql"
MIGRATION_SCRIPT = ROOT / "scripts" / "pipeline_state_migration.py"
sys.path.insert(0, str(ROOT / "scripts"))
import pipeline_state_migration as migration  # noqa: E402


class PipelineStateFoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "pipeline-state.sqlite"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for statement in migration.iter_sql_statements(
            MIGRATION_DDL_PATH.read_text(encoding="utf-8")
        ):
            self.conn.execute(statement)
        self.conn.execute(
            "INSERT INTO wahojobs_schema_migrations (version) VALUES (?)",
            (migration.MIGRATION_VERSION,),
        )
        self.conn.commit()
        self.counter = 0

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def insert_item(
        self,
        *,
        status="saved",
        reminder_date="",
        profile_id="profile-a",
        pipeline_item_id=None,
    ):
        self.counter += 1
        pipeline_item_id = pipeline_item_id or f"pipeline-{self.counter:03d}"
        self.conn.execute(
            """
            INSERT INTO user_pipeline_items (
              pipeline_item_id,
              user_id,
              profile_id,
              source,
              opportunity_title,
              opportunity_url,
              status,
              status_date,
              user_priority,
              reminder_date,
              notes,
              last_user_action,
              is_sample
            )
            VALUES (?, 'user-a', ?, 'Fixture Source', ?, ?, ?, '2026-07-12',
                    'medium', ?, '', '', 0)
            """,
            (
                pipeline_item_id,
                profile_id,
                f"Opportunity {self.counter}",
                f"https://example.test/{self.counter}",
                status,
                reminder_date,
            ),
        )
        self.conn.commit()
        return pipeline_item_id

    def initialize(self, pipeline_item_id, *, workflow_status="saved", profile_id="profile-a"):
        return pipeline_state.initialize_projection(
            self.conn,
            pipeline_item_id=pipeline_item_id,
            owner_profile_id=profile_id,
            workflow_status=workflow_status,
            workflow_status_provenance="known",
            visibility="visible",
            reminder_at=None,
            idempotency_key=f"initialize:{pipeline_item_id}",
            actor_source="test",
        )

    def transition_count(self, pipeline_item_id=None):
        if pipeline_item_id:
            return self.conn.execute(
                "SELECT COUNT(*) FROM user_pipeline_transitions WHERE pipeline_item_id = ?",
                (pipeline_item_id,),
            ).fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM user_pipeline_transitions"
        ).fetchone()[0]

    def test_schema_has_projection_ledger_foreign_keys_indexes_and_append_only_guards(self):
        tables = {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertIn("user_pipeline_state", tables)
        self.assertIn("user_pipeline_transitions", tables)

        projection_fks = self.conn.execute(
            "PRAGMA foreign_key_list(user_pipeline_state)"
        ).fetchall()
        transition_fks = self.conn.execute(
            "PRAGMA foreign_key_list(user_pipeline_transitions)"
        ).fetchall()
        self.assertTrue(
            any(row["table"] == "user_pipeline_items" for row in projection_fks)
        )
        self.assertTrue(
            any(row["table"] == "user_pipeline_items" for row in transition_fks)
        )
        self.assertEqual(
            sum(row["table"] == "user_pipeline_transitions" for row in transition_fks),
            2,
        )

        indexes = {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        self.assertTrue(
            {
                "idx_user_pipeline_transitions_pipeline_occurred",
                "idx_user_pipeline_transitions_profile_occurred",
                "idx_user_pipeline_transitions_undo",
                "idx_user_pipeline_transitions_correction",
                "idx_user_pipeline_transitions_occurred",
                "idx_user_pipeline_items_pipeline_profile",
            }.issubset(indexes)
        )

        item = self.insert_item()
        initialized = self.initialize(item)
        transition_id = initialized.transition["transition_id"]
        state_json = pipeline_state.canonical_json(
            {
                key: initialized.state[key]
                for key in (
                    "workflow_status",
                    "workflow_status_provenance",
                    "visibility",
                    "reminder_at",
                )
            }
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO user_pipeline_transitions (
                  transition_id, pipeline_item_id, profile_id, affected_dimension,
                  action_name, before_state_json, after_state_json, occurred_at,
                  actor_source, idempotency_key, request_fingerprint,
                  state_version_before, state_version_after, metadata_json
                )
                VALUES ('forged-owner', ?, 'profile-b', 'visibility', 'hide', ?, ?,
                        '2026-07-12T00:00:00+00:00', 'test', 'forged-owner',
                        'forged', 1, 2, '{}')
                """,
                (item, state_json, state_json),
            )
        self.conn.rollback()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.conn.execute(
                "UPDATE user_pipeline_transitions SET action_name = 'changed' WHERE transition_id = ?",
                (transition_id,),
            )
        self.conn.rollback()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            self.conn.execute(
                "DELETE FROM user_pipeline_transitions WHERE transition_id = ?",
                (transition_id,),
            )
        self.conn.rollback()
        self.assertEqual(self.transition_count(item), 1)

    def test_initializes_every_workflow_status_and_new_pipeline_items(self):
        for status in sorted(pipeline_state.WORKFLOW_STATUSES):
            with self.subTest(status=status):
                item = self.insert_item(status=status)
                result = self.initialize(item, workflow_status=status)
                self.assertEqual(result.state["workflow_status"], status)
                self.assertEqual(result.state["workflow_status_provenance"], "known")
                self.assertEqual(result.state["visibility"], "visible")
                self.assertEqual(result.state["version"], 1)
                self.assertEqual(result.transition["affected_dimension"], "baseline")

    def test_legacy_backfill_maps_every_workflow_status_without_fabricating_events(self):
        items = {
            status: self.insert_item(status=status)
            for status in sorted(pipeline_state.WORKFLOW_STATUSES)
        }
        result = pipeline_state.backfill_legacy_pipeline_state(
            self.conn, dry_run=False
        )
        self.assertEqual(result["migrated"], len(items))
        for status, item in items.items():
            with self.subTest(status=status):
                state = pipeline_state.get_current_state(
                    self.conn, item, "profile-a"
                )
                self.assertEqual(state["workflow_status"], status)
                self.assertEqual(
                    state["workflow_status_provenance"], "inferred_legacy"
                )
                history = pipeline_state.list_transition_history(
                    self.conn, item, "profile-a"
                )
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0]["action_name"], "legacy_snapshot")
                self.assertTrue(history[0]["metadata"]["legacy_snapshot"])

    def test_legacy_backfill_preserves_uncertainty_and_is_idempotent(self):
        ordinary = self.insert_item(status="applied", reminder_date="2026-07-20")
        hidden = self.insert_item(status="not_interested")
        reminder = self.insert_item(status="remind_later", reminder_date="2026-07-21")
        unknown = self.insert_item(status="mystery_state")
        malformed = self.insert_item(status="saved", reminder_date="next someday")
        existing = self.insert_item(status="saved")
        self.initialize(existing)

        before_projection_count = self.conn.execute(
            "SELECT COUNT(*) FROM user_pipeline_state"
        ).fetchone()[0]
        dry_run = pipeline_state.backfill_legacy_pipeline_state(
            self.conn, dry_run=True
        )
        self.assertEqual(dry_run["planned"], 5)
        self.assertEqual(dry_run["migrated"], 0)
        self.assertEqual(dry_run["malformed_reminders"], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM user_pipeline_state").fetchone()[0],
            before_projection_count,
        )

        applied = pipeline_state.backfill_legacy_pipeline_state(
            self.conn, dry_run=False
        )
        self.assertEqual(applied["migrated"], 5)
        self.assertEqual(self.transition_count(), 6)

        ordinary_state = pipeline_state.get_current_state(
            self.conn, ordinary, "profile-a"
        )
        self.assertEqual(ordinary_state["workflow_status"], "applied")
        self.assertEqual(
            ordinary_state["workflow_status_provenance"], "inferred_legacy"
        )
        self.assertEqual(ordinary_state["reminder_at"], "2026-07-20")

        hidden_state = pipeline_state.get_current_state(
            self.conn, hidden, "profile-a"
        )
        self.assertIsNone(hidden_state["workflow_status"])
        self.assertEqual(hidden_state["workflow_status_provenance"], "unknown_legacy")
        self.assertEqual(hidden_state["visibility"], "hidden")

        reminder_state = pipeline_state.get_current_state(
            self.conn, reminder, "profile-a"
        )
        self.assertIsNone(reminder_state["workflow_status"])
        self.assertEqual(reminder_state["reminder_at"], "2026-07-21")

        unknown_history = pipeline_state.list_transition_history(
            self.conn, unknown, "profile-a"
        )
        self.assertEqual(
            unknown_history[0]["metadata"]["raw_legacy_status"], "mystery_state"
        )
        malformed_history = pipeline_state.list_transition_history(
            self.conn, malformed, "profile-a"
        )
        self.assertIsNone(malformed_history[0]["after_state"]["reminder_at"])
        self.assertFalse(
            malformed_history[0]["metadata"]["legacy_reminder_valid"]
        )

        second = pipeline_state.backfill_legacy_pipeline_state(
            self.conn, dry_run=False
        )
        self.assertEqual(second["planned"], 0)
        self.assertEqual(second["migrated"], 0)
        self.assertEqual(self.transition_count(), 6)

    def test_workflow_progression_visibility_and_reminders_are_independent(self):
        accepted_item = self.insert_item()
        self.initialize(accepted_item)
        applied = pipeline_state.change_workflow_status(
            self.conn,
            pipeline_item_id=accepted_item,
            owner_profile_id="profile-a",
            workflow_status="applied",
            expected_version=1,
            idempotency_key="accepted:applied",
        )
        started = pipeline_state.change_workflow_status(
            self.conn,
            pipeline_item_id=accepted_item,
            owner_profile_id="profile-a",
            workflow_status="assessment_started",
            expected_version=2,
            idempotency_key="accepted:started",
        )
        completed = pipeline_state.change_workflow_status(
            self.conn,
            pipeline_item_id=accepted_item,
            owner_profile_id="profile-a",
            workflow_status="assessment_completed",
            expected_version=3,
            idempotency_key="accepted:completed",
        )
        accepted = pipeline_state.change_workflow_status(
            self.conn,
            pipeline_item_id=accepted_item,
            owner_profile_id="profile-a",
            workflow_status="accepted",
            expected_version=4,
            idempotency_key="accepted:accepted",
        )
        self.assertEqual(applied.state["workflow_status"], "applied")
        self.assertEqual(started.state["workflow_status"], "assessment_started")
        self.assertEqual(completed.state["workflow_status"], "assessment_completed")
        self.assertEqual(accepted.state["workflow_status"], "accepted")

        rejected_item = self.insert_item()
        self.initialize(rejected_item)
        for version, target in enumerate(
            ("applied", "assessment_started", "assessment_completed", "rejected"),
            start=1,
        ):
            result = pipeline_state.change_workflow_status(
                self.conn,
                pipeline_item_id=rejected_item,
                owner_profile_id="profile-a",
                workflow_status=target,
                expected_version=version,
                idempotency_key=f"rejected:{target}",
            )
        self.assertEqual(result.state["workflow_status"], "rejected")

        independent_item = self.insert_item()
        self.initialize(independent_item)
        hidden = pipeline_state.hide_item(
            self.conn,
            pipeline_item_id=independent_item,
            owner_profile_id="profile-a",
            expected_version=1,
            idempotency_key="independent:hide",
        )
        self.assertEqual(hidden.state["workflow_status"], "saved")
        self.assertEqual(hidden.state["visibility"], "hidden")
        shown = pipeline_state.show_item(
            self.conn,
            pipeline_item_id=independent_item,
            owner_profile_id="profile-a",
            expected_version=2,
            idempotency_key="independent:show",
        )
        reminder = pipeline_state.set_reminder(
            self.conn,
            pipeline_item_id=independent_item,
            owner_profile_id="profile-a",
            reminder_at="2026-07-20T12:00:00+00:00",
            expected_version=3,
            idempotency_key="independent:reminder-1",
        )
        changed = pipeline_state.set_reminder(
            self.conn,
            pipeline_item_id=independent_item,
            owner_profile_id="profile-a",
            reminder_at="2026-07-22T12:00:00+00:00",
            expected_version=4,
            idempotency_key="independent:reminder-2",
        )
        cleared = pipeline_state.clear_reminder(
            self.conn,
            pipeline_item_id=independent_item,
            owner_profile_id="profile-a",
            expected_version=5,
            idempotency_key="independent:reminder-clear",
        )
        self.assertEqual(shown.state["visibility"], "visible")
        self.assertEqual(reminder.state["workflow_status"], "saved")
        self.assertEqual(changed.state["reminder_at"], "2026-07-22T12:00:00+00:00")
        self.assertIsNone(cleared.state["reminder_at"])

        legacy = self.conn.execute(
            "SELECT status, reminder_date FROM user_pipeline_items WHERE pipeline_item_id = ?",
            (independent_item,),
        ).fetchone()
        self.assertEqual(dict(legacy), {"status": "saved", "reminder_date": ""})
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM applicant_status_updates").fetchone()[0],
            0,
        )

    def test_idempotency_conflicts_stale_versions_and_ownership(self):
        item = self.insert_item()
        self.initialize(item)
        first = pipeline_state.change_workflow_status(
            self.conn,
            pipeline_item_id=item,
            owner_profile_id="profile-a",
            workflow_status="applied",
            expected_version=1,
            idempotency_key="workflow:apply",
        )
        replay = pipeline_state.change_workflow_status(
            self.conn,
            pipeline_item_id=item,
            owner_profile_id="profile-a",
            workflow_status="applied",
            expected_version=1,
            idempotency_key="workflow:apply",
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.state, first.state)
        self.assertEqual(replay.transition, first.transition)
        self.assertEqual(self.transition_count(item), 2)

        with self.assertRaises(pipeline_state.IdempotencyConflict):
            pipeline_state.set_reminder(
                self.conn,
                pipeline_item_id=item,
                owner_profile_id="profile-a",
                reminder_at="2026-07-20",
                expected_version=2,
                idempotency_key="workflow:apply",
            )
        with self.assertRaises(pipeline_state.IdempotencyConflict):
            pipeline_state.change_workflow_status(
                self.conn,
                pipeline_item_id=item,
                owner_profile_id="profile-a",
                workflow_status="applied",
                expected_version=999,
                idempotency_key="workflow:apply",
            )
        with self.assertRaises(pipeline_state.StaleStateVersion):
            pipeline_state.change_workflow_status(
                self.conn,
                pipeline_item_id=item,
                owner_profile_id="profile-a",
                workflow_status="assessment_started",
                expected_version=1,
                idempotency_key="workflow:stale",
            )
        with self.assertRaises(pipeline_state.OwnershipError):
            pipeline_state.hide_item(
                self.conn,
                pipeline_item_id=item,
                owner_profile_id="profile-b",
                expected_version=2,
                idempotency_key="workflow:foreign",
            )
        self.assertEqual(pipeline_state.get_current_state(self.conn, item, "profile-a")["version"], 2)
        self.assertEqual(self.transition_count(item), 2)

    def test_two_connections_cannot_overwrite_a_newer_projection(self):
        item = self.insert_item()
        self.initialize(item)
        second = sqlite3.connect(self.db_path)
        second.row_factory = sqlite3.Row
        second.execute("PRAGMA foreign_keys = ON")
        try:
            self.assertEqual(
                pipeline_state.get_current_state(self.conn, item, "profile-a")["version"],
                1,
            )
            self.assertEqual(
                pipeline_state.get_current_state(second, item, "profile-a")["version"],
                1,
            )
            pipeline_state.change_workflow_status(
                self.conn,
                pipeline_item_id=item,
                owner_profile_id="profile-a",
                workflow_status="applied",
                expected_version=1,
                idempotency_key="concurrency:first",
            )
            with self.assertRaises(pipeline_state.StaleStateVersion):
                pipeline_state.change_workflow_status(
                    second,
                    pipeline_item_id=item,
                    owner_profile_id="profile-a",
                    workflow_status="applied",
                    expected_version=1,
                    idempotency_key="concurrency:stale",
                )
            self.assertEqual(
                pipeline_state.get_current_state(second, item, "profile-a")["version"],
                2,
            )
            self.assertEqual(self.transition_count(item), 2)
        finally:
            second.close()

    def test_insert_and_projection_failures_are_atomic(self):
        initialization_failure = self.insert_item()
        self.conn.execute(
            """
            CREATE TRIGGER fail_pipeline_baseline_insert
            BEFORE INSERT ON user_pipeline_transitions
            WHEN NEW.affected_dimension = 'baseline'
            BEGIN
              SELECT RAISE(ABORT, 'forced baseline failure');
            END
            """
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced baseline failure"):
            self.initialize(initialization_failure)
        self.conn.execute("DROP TRIGGER fail_pipeline_baseline_insert")
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM user_pipeline_state WHERE pipeline_item_id = ?",
                (initialization_failure,),
            ).fetchone()
        )
        self.assertEqual(self.transition_count(initialization_failure), 0)

        insert_failure = self.insert_item()
        self.initialize(insert_failure)
        self.conn.execute(
            """
            CREATE TRIGGER fail_pipeline_transition_insert
            BEFORE INSERT ON user_pipeline_transitions
            WHEN NEW.affected_dimension = 'workflow'
            BEGIN
              SELECT RAISE(ABORT, 'forced transition failure');
            END
            """
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced transition failure"):
            pipeline_state.change_workflow_status(
                self.conn,
                pipeline_item_id=insert_failure,
                owner_profile_id="profile-a",
                workflow_status="applied",
                expected_version=1,
                idempotency_key="atomic:insert",
            )
        self.conn.execute("DROP TRIGGER fail_pipeline_transition_insert")
        self.assertEqual(
            pipeline_state.get_current_state(self.conn, insert_failure, "profile-a")["workflow_status"],
            "saved",
        )
        self.assertEqual(self.transition_count(insert_failure), 1)

        update_failure = self.insert_item()
        self.initialize(update_failure)
        self.conn.execute(
            """
            CREATE TRIGGER fail_pipeline_projection_update
            BEFORE UPDATE ON user_pipeline_state
            BEGIN
              SELECT RAISE(ABORT, 'forced projection failure');
            END
            """
        )
        before_count = self.transition_count(update_failure)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced projection failure"):
            pipeline_state.change_workflow_status(
                self.conn,
                pipeline_item_id=update_failure,
                owner_profile_id="profile-a",
                workflow_status="applied",
                expected_version=1,
                idempotency_key="atomic:update",
            )
        self.conn.execute("DROP TRIGGER fail_pipeline_projection_update")
        self.assertEqual(
            pipeline_state.get_current_state(self.conn, update_failure, "profile-a")["version"],
            1,
        )
        self.assertEqual(self.transition_count(update_failure), before_count)

    def test_undo_correction_and_effective_funnel_history(self):
        item = self.insert_item()
        self.initialize(item)
        applied = pipeline_state.change_workflow_status(
            self.conn,
            pipeline_item_id=item,
            owner_profile_id="profile-a",
            workflow_status="applied",
            expected_version=1,
            idempotency_key="undo:apply",
        )
        undone = pipeline_state.undo_transition(
            self.conn,
            pipeline_item_id=item,
            owner_profile_id="profile-a",
            transition_id=applied.transition["transition_id"],
            expected_version=2,
            idempotency_key="undo:apply:undo",
        )
        replay = pipeline_state.undo_transition(
            self.conn,
            pipeline_item_id=item,
            owner_profile_id="profile-a",
            transition_id=applied.transition["transition_id"],
            expected_version=2,
            idempotency_key="undo:apply:undo",
        )
        self.assertEqual(undone.state["workflow_status"], "saved")
        self.assertTrue(replay.replayed)
        self.assertEqual(self.transition_count(item), 3)
        self.assertEqual(
            pipeline_state.list_effective_funnel_transitions(
                self.conn, item, "profile-a"
            ),
            [],
        )

        reapplied = pipeline_state.change_workflow_status(
            self.conn,
            pipeline_item_id=item,
            owner_profile_id="profile-a",
            workflow_status="applied",
            expected_version=3,
            idempotency_key="undo:reapply",
        )
        started = pipeline_state.change_workflow_status(
            self.conn,
            pipeline_item_id=item,
            owner_profile_id="profile-a",
            workflow_status="assessment_started",
            expected_version=4,
            idempotency_key="undo:start",
        )
        with self.assertRaises(pipeline_state.InvalidTransition):
            pipeline_state.undo_transition(
                self.conn,
                pipeline_item_id=item,
                owner_profile_id="profile-a",
                transition_id=reapplied.transition["transition_id"],
                expected_version=5,
                idempotency_key="undo:older",
            )

        corrected_state = {
            "workflow_status": "applied",
            "workflow_status_provenance": "known",
            "visibility": "visible",
            "reminder_at": None,
        }
        corrected = pipeline_state.correct_state(
            self.conn,
            pipeline_item_id=item,
            owner_profile_id="profile-a",
            corrected_state=corrected_state,
            correction_of_transition_id=started.transition["transition_id"],
            expected_version=5,
            idempotency_key="undo:correct-start",
            metadata={"reason": "User corrected an accidental assessment status."},
        )
        corrected_replay = pipeline_state.correct_state(
            self.conn,
            pipeline_item_id=item,
            owner_profile_id="profile-a",
            corrected_state=corrected_state,
            correction_of_transition_id=started.transition["transition_id"],
            expected_version=5,
            idempotency_key="undo:correct-start",
            metadata={"reason": "User corrected an accidental assessment status."},
        )
        self.assertEqual(corrected.state["workflow_status"], "applied")
        self.assertTrue(corrected_replay.replayed)
        history = pipeline_state.list_transition_history(self.conn, item, "profile-a")
        self.assertIn(
            started.transition["transition_id"],
            {row["correction_of_transition_id"] for row in history},
        )
        effective = pipeline_state.list_effective_funnel_transitions(
            self.conn, item, "profile-a"
        )
        self.assertEqual(
            [row["transition_id"] for row in effective],
            [reapplied.transition["transition_id"]],
        )

    def test_visibility_and_reminder_undo_restore_only_the_affected_projection(self):
        item = self.insert_item()
        self.initialize(item)
        hidden = pipeline_state.hide_item(
            self.conn,
            pipeline_item_id=item,
            owner_profile_id="profile-a",
            expected_version=1,
            idempotency_key="dimension:hide",
        )
        shown = pipeline_state.undo_transition(
            self.conn,
            pipeline_item_id=item,
            owner_profile_id="profile-a",
            transition_id=hidden.transition["transition_id"],
            expected_version=2,
            idempotency_key="dimension:hide:undo",
        )
        self.assertEqual(shown.state["visibility"], "visible")
        self.assertEqual(shown.state["workflow_status"], "saved")

        reminder = pipeline_state.set_reminder(
            self.conn,
            pipeline_item_id=item,
            owner_profile_id="profile-a",
            reminder_at="2026-07-20",
            expected_version=3,
            idempotency_key="dimension:reminder",
        )
        cleared = pipeline_state.undo_transition(
            self.conn,
            pipeline_item_id=item,
            owner_profile_id="profile-a",
            transition_id=reminder.transition["transition_id"],
            expected_version=4,
            idempotency_key="dimension:reminder:undo",
        )
        self.assertIsNone(cleared.state["reminder_at"])
        self.assertEqual(cleared.state["workflow_status"], "saved")

    def test_backfill_rolls_back_every_item_when_one_transition_insert_fails(self):
        first = self.insert_item(status="saved")
        second = self.insert_item(status="applied")
        self.conn.execute(
            f"""
            CREATE TRIGGER fail_second_legacy_snapshot
            BEFORE INSERT ON user_pipeline_transitions
            WHEN NEW.pipeline_item_id = '{second}'
            BEGIN
              SELECT RAISE(ABORT, 'forced legacy failure');
            END
            """
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "forced legacy failure"):
            pipeline_state.backfill_legacy_pipeline_state(self.conn, dry_run=False)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM user_pipeline_state").fetchone()[0],
            0,
        )
        self.assertEqual(self.transition_count(), 0)
        self.assertEqual(
            {first, second},
            {
                row["pipeline_item_id"]
                for row in self.conn.execute(
                    "SELECT pipeline_item_id FROM user_pipeline_items"
                )
            },
        )

    def test_migration_cli_dry_run_is_read_only_and_apply_is_resumable(self):
        item = self.insert_item(status="not_interested")
        self.conn.executescript(
            """
            DROP TRIGGER trg_user_pipeline_transitions_no_update;
            DROP TRIGGER trg_user_pipeline_transitions_no_delete;
            DROP TABLE user_pipeline_transitions;
            DROP TABLE user_pipeline_state;
            DROP TABLE wahojobs_schema_migrations;
            DROP INDEX idx_user_pipeline_items_pipeline_profile;
            """
        )
        self.conn.close()

        before = self.db_path.stat()
        dry_run = subprocess.run(
            [
                sys.executable,
                "-B",
                str(MIGRATION_SCRIPT),
                "--db",
                str(self.db_path),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        after_dry_run = self.db_path.stat()
        self.assertIn("Mode: dry-run", dry_run.stdout)
        self.assertIn("Planned rows: 1", dry_run.stdout)
        self.assertEqual(before.st_size, after_dry_run.st_size)
        self.assertEqual(before.st_mtime_ns, after_dry_run.st_mtime_ns)

        applied = subprocess.run(
            [
                sys.executable,
                "-B",
                str(MIGRATION_SCRIPT),
                "--db",
                str(self.db_path),
                "--yes",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Migrated rows: 1", applied.stdout)

        resumed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(MIGRATION_SCRIPT),
                "--db",
                str(self.db_path),
                "--yes",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Planned rows: 0", resumed.stdout)
        self.assertIn("Migrated rows: 0", resumed.stdout)

        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        state = pipeline_state.get_current_state(self.conn, item, "profile-a")
        self.assertEqual(state["visibility"], "hidden")
        self.assertIsNone(state["workflow_status"])
        self.assertEqual(self.transition_count(item), 1)


if __name__ == "__main__":
    unittest.main()
