import copy
import sqlite3
import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path

from wahojobs import pipeline_state
from wahojobs import pipeline_actions


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "wahojobs" / "db" / "schema.sql"
MIGRATION = ROOT / "wahojobs" / "db" / "migrations" / "001_pipeline_state.sql"
sys.path.insert(0, str(ROOT / "scripts"))
import pipeline_state_migration as migration  # noqa: E402


class PipelineActionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "actions.sqlite"
        self.conn = self.connect()
        self.conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        for statement in migration.iter_sql_statements(MIGRATION.read_text(encoding="utf-8")):
            self.conn.execute(statement)
        self.conn.execute(
            "INSERT INTO wahojobs_schema_migrations (version) VALUES (?)",
            (migration.MIGRATION_VERSION,),
        )
        self.add_profile("profile-a", "user-a")
        self.add_profile("profile-b", "user-b")
        self.conn.commit()
        self.key_counter = 0

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def connect(self, timeout=5):
        conn = sqlite3.connect(self.db_path, timeout=timeout, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def add_profile(self, profile_id, user_id):
        self.conn.execute(
            """
            INSERT INTO user_profiles (profile_id, user_id, display_name)
            VALUES (?, ?, ?)
            """,
            (profile_id, user_id, profile_id),
        )

    def key(self, label="request"):
        self.key_counter += 1
        return f"{label}-000000000000-{self.key_counter:04d}"

    def create(self, action="save", **overrides):
        values = {
            "action": action,
            "owner_profile_id": "profile-a",
            "idempotency_key": self.key(action),
            "expected_version": 0,
            "match_run_id": "match-run-a",
            "source": "Fixture Source",
            "title": f"Fixture {self.key_counter + 1}",
            "url": f"https://example.test/{self.key_counter + 1}",
        }
        values.update(overrides)
        return pipeline_actions.perform_pipeline_action(self.conn, **values)

    def act(self, result, action, **overrides):
        values = {
            "action": action,
            "owner_profile_id": "profile-a",
            "idempotency_key": self.key(action),
            "expected_version": result.state["version"],
            "match_run_id": "match-run-a",
            "pipeline_item_id": result.pipeline_item["pipeline_item_id"],
        }
        values.update(overrides)
        return pipeline_actions.perform_pipeline_action(self.conn, **values)

    def counts(self):
        return tuple(
            self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "user_pipeline_items",
                "user_pipeline_state",
                "user_pipeline_transitions",
                "applicant_status_updates",
            )
        )

    def replace_transition_metadata(self, transition_id, metadata):
        self.conn.commit()
        self.conn.execute("DROP TRIGGER trg_user_pipeline_transitions_no_update")
        self.conn.execute(
            "UPDATE user_pipeline_transitions SET metadata_json=? WHERE transition_id=?",
            (pipeline_state.canonical_json(metadata), transition_id),
        )
        self.conn.executescript(
            """
            CREATE TRIGGER trg_user_pipeline_transitions_no_update
            BEFORE UPDATE ON user_pipeline_transitions
            BEGIN SELECT RAISE(ABORT, 'user_pipeline_transitions is append-only'); END;
            """
        )
        self.conn.commit()

    def test_mirror_precedence_and_unresolved_error(self):
        hidden = pipeline_actions.legacy_compatibility_from_state(
            {
                "workflow_status": "applied",
                "workflow_status_provenance": "known",
                "visibility": "hidden",
                "reminder_at": "2026-07-20T12:00:00+00:00",
            }
        )
        self.assertEqual(hidden, {"status": "not_interested", "reminder_date": "2026-07-20"})
        known = pipeline_actions.legacy_compatibility_from_state(
            {
                "workflow_status": "applied",
                "workflow_status_provenance": "known",
                "visibility": "visible",
                "reminder_at": "2026-07-20T12:00:00+00:00",
            }
        )
        self.assertEqual(known["status"], "applied")
        unknown_reminder = pipeline_actions.legacy_compatibility_from_state(
            {
                "workflow_status": None,
                "workflow_status_provenance": "unknown_legacy",
                "visibility": "visible",
                "reminder_at": "2026-07-20T12:00:00+00:00",
            }
        )
        self.assertEqual(unknown_reminder["status"], "remind_later")
        with self.assertRaises(pipeline_actions.UnresolvedLegacyWorkflow):
            pipeline_actions.legacy_compatibility_from_state(
                {
                    "workflow_status": None,
                    "workflow_status_provenance": "unknown_legacy",
                    "visibility": "visible",
                    "reminder_at": None,
                }
            )

    def test_untracked_save_has_user_creation_and_terminal_operation_noop(self):
        result = self.create("save")
        self.assertEqual(result.state["workflow_status"], "saved")
        self.assertEqual(result.state["version"], 2)
        self.assertEqual(self.counts(), (1, 1, 2, 0))
        history = pipeline_state.list_transition_history(
            self.conn, result.pipeline_item["pipeline_item_id"], "profile-a"
        )
        self.assertEqual(
            [row["action_name"] for row in history],
            ["user_created", "product_noop_save"],
        )
        self.assertEqual(history[0]["metadata"]["transition_class"], "user_initialization")
        self.assertEqual(history[1]["metadata"]["transition_class"], "operation_noop")
        self.assertFalse(history[0]["metadata"]["legacy_snapshot"])

    def test_untracked_applied_is_saved_then_applied_with_applicant_update(self):
        result = self.create(
            "applied",
            opportunity_external_id="source-job-123",
            canonical_id=42,
        )
        self.assertEqual(result.state["workflow_status"], "applied")
        self.assertEqual(result.state["version"], 2)
        self.assertEqual(self.counts(), (1, 1, 2, 1))
        history = pipeline_state.list_transition_history(
            self.conn, result.pipeline_item["pipeline_item_id"], "profile-a"
        )
        self.assertEqual([row["action_name"] for row in history], ["user_created", "product_applied"])
        self.assertEqual(result.applicant_update["status"], "applied")
        self.assertEqual(result.applicant_update["previous_status"], "saved")
        self.assertEqual(result.applicant_update["opportunity_external_id"], "")
        self.assertIsNone(result.applicant_update["canonical_id"])

    def test_untracked_hidden_restores_saved(self):
        hidden = self.create("not_interested")
        self.assertEqual(hidden.state["workflow_status"], "saved")
        self.assertEqual(hidden.state["visibility"], "hidden")
        self.assertEqual(hidden.compatibility_state["status"], "not_interested")
        shown = self.act(hidden, "show_again")
        self.assertEqual(shown.state["workflow_status"], "saved")
        self.assertEqual(shown.state["visibility"], "visible")
        self.assertEqual(shown.compatibility_state["status"], "saved")

    def test_untracked_creation_rejects_unsupported_first_actions_without_writes(self):
        for action, overrides in (
            ("assessment_started", {}),
            ("assessment_completed", {}),
            ("accepted", {}),
            ("rejected", {}),
            ("remind_later", {"reminder_at": "2026-09-01T12:00:00+00:00"}),
            ("show_again", {}),
            ("show_again_as_saved", {}),
        ):
            with self.subTest(action=action):
                before = self.counts()
                with self.assertRaises(pipeline_state.InvalidTransition):
                    self.create(action, **overrides)
                self.assertEqual(self.counts(), before)

    def test_repeated_saved_action_preserves_hidden_visibility_and_reminder(self):
        saved = self.create("save")
        reminded = self.act(
            saved,
            "remind_later",
            reminder_at="2026-09-01T12:00:00+00:00",
        )
        hidden = self.act(reminded, "not_interested")
        repeated = self.act(hidden, "save")
        self.assertEqual(repeated.state["workflow_status"], "saved")
        self.assertEqual(repeated.state["visibility"], "hidden")
        self.assertEqual(repeated.state["reminder_at"], "2026-09-01T12:00:00+00:00")
        self.assertEqual(repeated.compatibility_state["status"], "not_interested")

    def test_workflow_visibility_and_reminder_dimensions_are_preserved(self):
        applied = self.create("applied")
        reminded = self.act(
            applied,
            "remind_later",
            reminder_at="2026-07-25T09:00:00+00:00",
        )
        self.assertEqual(reminded.state["workflow_status"], "applied")
        self.assertEqual(reminded.compatibility_state["status"], "applied")
        self.assertEqual(reminded.compatibility_state["reminder_date"], "2026-07-25")
        hidden = self.act(reminded, "not_interested")
        shown = self.act(hidden, "show_again")
        self.assertEqual(shown.state["workflow_status"], "applied")
        self.assertEqual(shown.state["reminder_at"], "2026-07-25T09:00:00+00:00")

        second_applied = self.create(
            "applied",
            title="Assessment visibility fixture",
            url="https://example.test/assessment-visibility",
        )
        started = self.act(second_applied, "assessment_started")
        hidden_started = self.act(started, "not_interested")
        shown_started = self.act(hidden_started, "show_again")
        self.assertEqual(shown_started.state["workflow_status"], "assessment_started")

    def test_assessment_completed_accepted_and_rejected_applicant_parity(self):
        applied = self.create("applied")
        started = self.act(applied, "assessment_started")
        completed = self.act(started, "assessment_completed")
        accepted = self.act(completed, "accepted")
        self.assertEqual(accepted.state["workflow_status"], "accepted")
        statuses = [
            row[0]
            for row in self.conn.execute(
                "SELECT status FROM applicant_status_updates ORDER BY id"
            )
        ]
        self.assertEqual(statuses, ["applied", "assessment_started", "assessment_completed", "accepted"])

        other = self.create("applied", title="Rejected fixture", url="https://example.test/rejected")
        other = self.act(other, "assessment_started")
        other = self.act(other, "assessment_completed")
        rejected = self.act(other, "rejected")
        self.assertEqual(rejected.applicant_update["status"], "rejected")

    def insert_unknown(self, *, visibility, reminder_at):
        item_id = f"unknown-{self.key_counter + 1}"
        legacy_status = "not_interested" if visibility == "hidden" else "remind_later"
        reminder_date = reminder_at[:10] if reminder_at else ""
        self.conn.execute(
            """
            INSERT INTO user_pipeline_items (
              pipeline_item_id, user_id, profile_id, source, opportunity_title,
              opportunity_url, status, status_date, reminder_date, notes, last_user_action, is_sample
            ) VALUES (?, 'user-a', 'profile-a', 'Legacy', 'Unknown item', '', ?,
                      '2026-07-01', ?, '', '', 0)
            """,
            (item_id, legacy_status, reminder_date),
        )
        pipeline_state.initialize_projection(
            self.conn,
            pipeline_item_id=item_id,
            owner_profile_id="profile-a",
            workflow_status=None,
            workflow_status_provenance="unknown_legacy",
            visibility=visibility,
            reminder_at=reminder_at,
            idempotency_key=self.key("legacy-baseline"),
            action_name="legacy_snapshot",
            actor_source="migration",
            metadata={"initialization_kind": "legacy_snapshot"},
        )
        return item_id

    def insert_known(self, status, *, reminder_at=None, visibility="visible", is_sample=0):
        item_id = f"known-{status}-{self.key_counter + 1}"
        mirror = pipeline_actions.legacy_compatibility_from_state(
            {
                "workflow_status": status,
                "workflow_status_provenance": "known",
                "visibility": visibility,
                "reminder_at": reminder_at,
            }
        )
        self.conn.execute(
            """
            INSERT INTO user_pipeline_items (
              pipeline_item_id, user_id, profile_id, source, opportunity_title,
              opportunity_url, status, status_date, reminder_date, notes,
              last_user_action, is_sample
            ) VALUES (?, 'user-a', 'profile-a', 'Fixture', ?, ?, ?,
                      '2026-07-01', ?, 'Original note', '', ?)
            """,
            (
                item_id,
                f"Known {status}",
                f"https://example.test/{item_id}",
                mirror["status"],
                mirror["reminder_date"],
                is_sample,
            ),
        )
        initialized = pipeline_state.initialize_projection(
            self.conn,
            pipeline_item_id=item_id,
            owner_profile_id="profile-a",
            workflow_status=status,
            workflow_status_provenance="known",
            visibility=visibility,
            reminder_at=reminder_at,
            idempotency_key=self.key(f"baseline-{status}"),
            actor_source="test",
        )
        return pipeline_actions.PipelineActionResult(
            pipeline_item={"pipeline_item_id": item_id},
            state=initialized.state,
            transition=initialized.transition,
            compatibility_state=mirror,
            applicant_update=None,
            created=False,
        )

    def test_workflow_actions_cover_every_valid_supported_start(self):
        cases = [
            ("recommended", "save", "saved"),
            ("recommended", "applied", "applied"),
            ("saved", "applied", "applied"),
            ("applied", "assessment_started", "assessment_started"),
            ("waiting", "assessment_started", "assessment_started"),
            ("assessment_invited", "assessment_started", "assessment_started"),
            ("assessment_started", "assessment_completed", "assessment_completed"),
            ("assessment_completed", "accepted", "accepted"),
            ("assessment_completed", "rejected", "rejected"),
        ]
        for index, (before, action, after) in enumerate(cases):
            starting = self.insert_known(
                before,
                reminder_at=f"2026-08-{index + 1:02d}T12:00:00+00:00",
                is_sample=1,
            )
            result = self.act(starting, action, note=f"Action {index}")
            self.assertEqual(result.state["workflow_status"], after)
            self.assertEqual(
                result.state["reminder_at"],
                f"2026-08-{index + 1:02d}T12:00:00+00:00",
            )
            item = self.conn.execute(
                "SELECT * FROM user_pipeline_items WHERE pipeline_item_id=?",
                (starting.pipeline_item["pipeline_item_id"],),
            ).fetchone()
            self.assertEqual(item["status"], after)
            self.assertEqual(item["is_sample"], 1)
            self.assertIn("Original note", item["notes"])
            self.assertIn(f"Action {index}", item["notes"])

    def test_hidden_unknown_requires_explicit_resolution_and_preserves_reminder(self):
        item_id = self.insert_unknown(
            visibility="hidden", reminder_at="2026-07-29T10:00:00+00:00"
        )
        with self.assertRaises(pipeline_actions.UnresolvedLegacyWorkflow):
            pipeline_actions.perform_pipeline_action(
                self.conn,
                action="show_again",
                owner_profile_id="profile-a",
                idempotency_key=self.key("show"),
                expected_version=1,
                match_run_id="run-a",
                pipeline_item_id=item_id,
            )
        shown = pipeline_actions.perform_pipeline_action(
            self.conn,
            action="show_again_as_saved",
            owner_profile_id="profile-a",
            idempotency_key=self.key("resolve-show"),
            expected_version=1,
            match_run_id="run-a",
            pipeline_item_id=item_id,
        )
        self.assertEqual(shown.state["workflow_status"], "saved")
        self.assertEqual(shown.state["workflow_status_provenance"], "known")
        self.assertEqual(shown.state["visibility"], "visible")
        self.assertEqual(shown.state["reminder_at"], "2026-07-29T10:00:00+00:00")
        history = pipeline_state.list_transition_history(self.conn, item_id, "profile-a")
        self.assertEqual(len(history), 3)
        self.assertEqual(history[-2]["action_name"], "resolve_unknown_workflow_as_saved")
        self.assertEqual(history[-1]["action_name"], "product_show_again_after_resolution")
        self.assertEqual(self.counts()[-1], 0)

    def test_reminder_unknown_stays_unresolved_until_explicit_workflow_action(self):
        item_id = self.insert_unknown(
            visibility="visible", reminder_at="2026-07-20T10:00:00+00:00"
        )
        reminded = pipeline_actions.perform_pipeline_action(
            self.conn,
            action="remind_later",
            owner_profile_id="profile-a",
            idempotency_key=self.key("reminder"),
            expected_version=1,
            match_run_id="run-a",
            pipeline_item_id=item_id,
            reminder_at="2026-08-01T10:00:00+00:00",
        )
        self.assertIsNone(reminded.state["workflow_status"])
        self.assertEqual(reminded.state["workflow_status_provenance"], "unknown_legacy")
        resolved = pipeline_actions.perform_pipeline_action(
            self.conn,
            action="applied",
            owner_profile_id="profile-a",
            idempotency_key=self.key("resolve-applied"),
            expected_version=2,
            match_run_id="run-a",
            pipeline_item_id=item_id,
        )
        self.assertEqual(resolved.state["workflow_status"], "applied")
        self.assertEqual(resolved.state["workflow_status_provenance"], "known")
        self.assertEqual(resolved.state["reminder_at"], "2026-08-01T10:00:00+00:00")

    def test_visible_unknown_without_reminder_is_invariant_failure(self):
        item_id = self.insert_unknown(visibility="hidden", reminder_at=None)
        self.conn.execute(
            "UPDATE user_pipeline_state SET visibility='visible' WHERE pipeline_item_id=?",
            (item_id,),
        )
        self.conn.execute(
            "UPDATE user_pipeline_items SET status='saved' WHERE pipeline_item_id=?",
            (item_id,),
        )
        self.conn.commit()
        with self.assertRaises(pipeline_actions.PipelineInvariantError):
            pipeline_actions.perform_pipeline_action(
                self.conn,
                action="save",
                owner_profile_id="profile-a",
                idempotency_key=self.key("invalid"),
                expected_version=1,
                match_run_id="run-a",
                pipeline_item_id=item_id,
            )

    def test_complete_operation_replay_is_exact_and_does_not_touch_timestamps(self):
        key = self.key("exact-replay")
        first = self.create(
            "applied",
            idempotency_key=key,
            title="Exact replay fixture",
            url="https://example.test/exact-replay",
        )
        counts = self.counts()
        item_before = dict(self.conn.execute(
            "SELECT * FROM user_pipeline_items WHERE pipeline_item_id=?",
            (first.pipeline_item["pipeline_item_id"],),
        ).fetchone())
        applicant_before = dict(self.conn.execute(
            "SELECT * FROM applicant_status_updates WHERE update_id=?",
            (first.applicant_update["update_id"],),
        ).fetchone())
        replay = self.create(
            "applied",
            idempotency_key=key,
            title="Exact replay fixture",
            url="https://example.test/exact-replay",
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.state, first.state)
        self.assertEqual(replay.compatibility_state, first.compatibility_state)
        self.assertEqual(self.counts(), counts)
        self.assertEqual(dict(self.conn.execute(
            "SELECT * FROM user_pipeline_items WHERE pipeline_item_id=?",
            (first.pipeline_item["pipeline_item_id"],),
        ).fetchone()), item_before)
        self.assertEqual(dict(self.conn.execute(
            "SELECT * FROM applicant_status_updates WHERE update_id=?",
            (first.applicant_update["update_id"],),
        ).fetchone()), applicant_before)

    def test_replay_after_later_transition_returns_original_result_without_reverting(self):
        key = self.key("historical-replay")
        applied = self.create(
            "applied",
            idempotency_key=key,
            title="Historical replay",
            url="https://example.test/historical-replay",
        )
        started = self.act(applied, "assessment_started")
        replay = self.create(
            "applied",
            idempotency_key=key,
            title="Historical replay",
            url="https://example.test/historical-replay",
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.state["workflow_status"], "applied")
        current = pipeline_state.get_current_state(
            self.conn, applied.pipeline_item["pipeline_item_id"], "profile-a"
        )
        self.assertEqual(current["workflow_status"], "assessment_started")
        self.assertEqual(current["version"], started.state["version"])

    def test_reused_key_payload_changes_are_private_conflicts(self):
        key = self.key("global-conflict")
        first = self.create("save", idempotency_key=key)
        variants = [
            {"action": "applied"},
            {"owner_profile_id": "profile-b"},
            {"match_run_id": "other-run"},
            {"expected_version": 1},
            {"title": "Changed opportunity"},
            {"note": "Changed semantic note"},
        ]
        for changes in variants:
            values = {
                "action": "save",
                "owner_profile_id": "profile-a",
                "idempotency_key": key,
                "expected_version": 0,
                "match_run_id": "match-run-a",
                "source": "Fixture Source",
                "title": first.pipeline_item["opportunity_title"],
                "url": first.pipeline_item["opportunity_url"],
            }
            values.update(changes)
            with self.assertRaises(pipeline_state.IdempotencyConflict) as raised:
                pipeline_actions.perform_pipeline_action(self.conn, **values)
            self.assertEqual(
                str(raised.exception),
                "Idempotency key was already used for a different mutation.",
            )

    def test_cross_owner_and_stale_version_are_rejected(self):
        saved = self.create("save")
        with self.assertRaises(pipeline_state.OwnershipError):
            pipeline_actions.perform_pipeline_action(
                self.conn,
                action="applied",
                owner_profile_id="profile-b",
                idempotency_key=self.key("owner"),
                expected_version=1,
                match_run_id="run-b",
                pipeline_item_id=saved.pipeline_item["pipeline_item_id"],
            )
        with self.assertRaises(pipeline_state.StaleStateVersion):
            self.act(saved, "applied", expected_version=99)

    def test_failure_injection_rolls_back_every_boundary(self):
        points = [
            "after_pipeline_item_insert",
            "after_projection_initialization",
            "after_first_normalized_transition",
            "after_legacy_status_mirror",
            "after_reminder_mirror",
            "after_notes_timestamp_update",
            "after_applicant_update",
            "after_reconciliation_validation",
            "before_outer_transaction_release",
        ]
        for point in points:
            before = self.counts()
            with self.assertRaisesRegex(RuntimeError, point):
                self.create(
                    "applied",
                    title=f"Failure {point}",
                    url=f"https://example.test/failure/{point}",
                    failure_injector=lambda actual, expected=point: (
                        (_ for _ in ()).throw(RuntimeError(expected))
                        if actual == expected
                        else None
                    ),
                )
            self.assertEqual(self.counts(), before, point)

        item_id = self.insert_unknown(visibility="hidden", reminder_at=None)
        before = self.counts()
        with self.assertRaisesRegex(RuntimeError, "after_second_normalized_transition"):
            pipeline_actions.perform_pipeline_action(
                self.conn,
                action="show_again_as_saved",
                owner_profile_id="profile-a",
                idempotency_key=self.key("unknown-failure"),
                expected_version=1,
                match_run_id="run-a",
                pipeline_item_id=item_id,
                failure_injector=lambda point: (
                    (_ for _ in ()).throw(RuntimeError(point))
                    if point == "after_second_normalized_transition"
                    else None
                ),
            )
        self.assertEqual(self.counts(), before)
        state = pipeline_state.get_current_state(self.conn, item_id, "profile-a")
        self.assertIsNone(state["workflow_status"])
        self.assertEqual(state["visibility"], "hidden")

    def test_outer_transaction_and_savepoint_preserve_unrelated_work(self):
        self.conn.execute(
            "INSERT INTO user_profiles (profile_id,user_id,display_name) VALUES ('caller','caller','Caller')"
        )
        self.create("save", title="Nested transaction", url="https://example.test/nested")
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM user_profiles WHERE profile_id='caller'"
        ).fetchone())
        self.assertEqual(self.counts(), (0, 0, 0, 0))

        self.conn.execute("SAVEPOINT caller_scope")
        self.conn.execute(
            "INSERT INTO user_profiles (profile_id,user_id,display_name) VALUES ('caller2','caller2','Caller2')"
        )
        result = self.create("save", title="Caller savepoint", url="https://example.test/caller-savepoint")
        self.conn.execute("ROLLBACK TO SAVEPOINT caller_scope")
        self.conn.execute("RELEASE SAVEPOINT caller_scope")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM user_pipeline_items WHERE pipeline_item_id=?",
            (result.pipeline_item["pipeline_item_id"],),
        ).fetchone())

    def test_nested_failure_rolls_back_only_action_savepoint(self):
        self.conn.execute(
            "INSERT INTO user_profiles (profile_id,user_id,display_name) VALUES ('caller3','caller3','Caller3')"
        )
        with self.assertRaisesRegex(RuntimeError, "after_pipeline_item_insert"):
            self.create(
                "save",
                title="Nested failure",
                url="https://example.test/nested-failure",
                failure_injector=lambda point: (
                    (_ for _ in ()).throw(RuntimeError(point))
                    if point == "after_pipeline_item_insert"
                    else None
                ),
            )
        self.assertTrue(self.conn.in_transaction)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM user_profiles WHERE profile_id='caller3'"
        ).fetchone())
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.conn.commit()
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM user_profiles WHERE profile_id='caller3'"
        ).fetchone())

    def test_concurrent_exact_request_replays_without_duplicates(self):
        self.conn.commit()
        key = self.key("concurrent-request")
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker():
            conn = self.connect(timeout=10)
            try:
                barrier.wait()
                result = pipeline_actions.perform_pipeline_action(
                    conn,
                    action="save",
                    owner_profile_id="profile-a",
                    idempotency_key=key,
                    expected_version=0,
                    match_run_id="run-concurrent",
                    source="Fixture Source",
                    title="Concurrent fixture",
                    url="https://example.test/concurrent",
                )
                results.append(result)
            except Exception as exc:  # pragma: no cover - assertion reports details
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(result.replayed for result in results), [False, True])
        self.assertEqual(self.counts(), (1, 1, 2, 0))

    def test_concurrent_conflicting_request_has_one_winner_and_no_orphans(self):
        self.conn.commit()
        key = self.key("concurrent-conflict")
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker(action):
            conn = self.connect(timeout=10)
            try:
                barrier.wait()
                results.append(
                    pipeline_actions.perform_pipeline_action(
                        conn,
                        action=action,
                        owner_profile_id="profile-a",
                        idempotency_key=key,
                        expected_version=0,
                        match_run_id="run-conflict",
                        source="Fixture Source",
                        title="Concurrent conflict fixture",
                        url="https://example.test/concurrent-conflict",
                    )
                )
            except Exception as exc:  # pragma: no cover - assertion reports details
                errors.append(exc)
            finally:
                conn.close()

        threads = [
            threading.Thread(target=worker, args=("save",)),
            threading.Thread(target=worker, args=("not_interested",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], pipeline_state.IdempotencyConflict)
        self.assertEqual(self.counts()[0:2], (1, 1))
        self.assertIn(self.counts()[2], {1, 2})

    def test_database_failures_between_applicant_mirror_and_projection_rollback_all(self):
        saved = self.create("save")
        self.conn.commit()
        before = self.counts()
        item_before = dict(self.conn.execute(
            "SELECT * FROM user_pipeline_items WHERE pipeline_item_id=?",
            (saved.pipeline_item["pipeline_item_id"],),
        ).fetchone())
        self.conn.execute(
            """
            CREATE TRIGGER fail_projection_update
            BEFORE UPDATE ON user_pipeline_state
            BEGIN SELECT RAISE(ABORT, 'projection failure'); END
            """
        )
        self.conn.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "projection failure"):
            self.act(saved, "applied")
        self.assertEqual(self.counts(), before)
        self.assertEqual(dict(self.conn.execute(
            "SELECT * FROM user_pipeline_items WHERE pipeline_item_id=?",
            (saved.pipeline_item["pipeline_item_id"],),
        ).fetchone()), item_before)
        self.conn.execute("DROP TRIGGER fail_projection_update")
        self.conn.commit()

        self.conn.execute(
            """
            CREATE TRIGGER fail_legacy_mirror
            BEFORE UPDATE OF status ON user_pipeline_items
            BEGIN SELECT RAISE(ABORT, 'mirror failure'); END
            """
        )
        self.conn.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "mirror failure"):
            self.act(saved, "applied")
        self.assertEqual(self.counts(), before)

    def test_locked_connection_and_invalid_transition_leave_state_unchanged(self):
        self.conn.commit()
        locker = self.connect()
        contender = self.connect(timeout=0.05)
        locker.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                pipeline_actions.perform_pipeline_action(
                    contender,
                    action="save",
                    owner_profile_id="profile-a",
                    idempotency_key=self.key("locked"),
                    expected_version=0,
                    match_run_id="run-a",
                    source="Fixture",
                    title="Locked",
                    url="https://example.test/locked",
                )
        finally:
            locker.rollback()
            locker.close()
            contender.close()
        self.assertEqual(self.counts(), (0, 0, 0, 0))

        saved = self.create("save")
        before = self.counts()
        with self.assertRaises(pipeline_state.InvalidTransition):
            self.act(saved, "assessment_completed")
        self.assertEqual(self.counts(), before)

    def test_expected_version_validation_is_strict_and_precedes_all_writes(self):
        class IntSubclass(int):
            pass

        invalid = [True, False, "1", 1.0, Decimal("1"), -1, None, IntSubclass(1), [], {}]
        for index, value in enumerate(invalid):
            before = self.counts()
            with self.assertRaises(pipeline_state.InvalidExpectedVersion):
                pipeline_actions.perform_pipeline_action(
                    self.conn,
                    action="save",
                    owner_profile_id="profile-a",
                    idempotency_key=f"invalid-version-{index:02d}-00000001",
                    expected_version=value,
                    match_run_id="run-validation",
                    source="Fixture",
                    title=f"Invalid version {index}",
                    url=f"https://example.test/invalid-version/{index}",
                )
            self.assertEqual(self.counts(), before)

        absent = pipeline_actions.perform_pipeline_action(
            self.conn,
            action="save",
            owner_profile_id="profile-a",
            idempotency_key="absent-version-create-000001",
            match_run_id="run-validation",
            source="Fixture",
            title="Absent creation version",
            url="https://example.test/absent-version",
        )
        self.assertTrue(absent.created)
        with self.assertRaises(pipeline_state.InvalidExpectedVersion):
            pipeline_actions.perform_pipeline_action(
                self.conn,
                action="applied",
                owner_profile_id="profile-a",
                idempotency_key="absent-version-existing-00001",
                match_run_id="run-validation",
                pipeline_item_id=absent.pipeline_item["pipeline_item_id"],
            )
        applied = self.act(absent, "applied")
        self.assertEqual(applied.state["workflow_status"], "applied")

    def test_internal_idempotency_namespace_is_reserved_and_deterministic(self):
        reserved = pipeline_actions.INTERNAL_IDEMPOTENCY_PREFIX + "caller"
        before = self.counts()
        with self.assertRaises(pipeline_actions.PipelineActionValidationError):
            self.create("save", idempotency_key=reserved)
        self.assertEqual(self.counts(), before)

        fingerprint_a = pipeline_state.request_fingerprint({"operation": "a"})
        fingerprint_b = pipeline_state.request_fingerprint({"operation": "b"})
        arguments = {
            "caller_key": "caller-key-000000000001",
            "operation_fingerprint": fingerprint_a,
            "step": "initialize",
            "pipeline_item_id": "item-a",
        }
        first = pipeline_actions._derived_idempotency_key(**arguments)
        self.assertEqual(first, pipeline_actions._derived_idempotency_key(**arguments))
        self.assertTrue(first.startswith(pipeline_actions.INTERNAL_IDEMPOTENCY_PREFIX))
        self.assertNotIn(arguments["caller_key"], first)
        self.assertNotEqual(
            first,
            pipeline_actions._derived_idempotency_key(**{**arguments, "step": "resolve"}),
        )
        self.assertNotEqual(
            first,
            pipeline_actions._derived_idempotency_key(
                **{**arguments, "operation_fingerprint": fingerprint_b}
            ),
        )

        failure_key = "rollback-internal-key-0000001"
        with self.assertRaises(RuntimeError):
            self.create(
                "applied",
                idempotency_key=failure_key,
                title="Internal rollback",
                url="https://example.test/internal-rollback",
                failure_injector=lambda point: (
                    (_ for _ in ()).throw(RuntimeError(point))
                    if point == "after_projection_initialization"
                    else None
                ),
            )
        retry = self.create(
            "applied",
            idempotency_key=failure_key,
            title="Internal rollback",
            url="https://example.test/internal-rollback",
        )
        self.assertEqual(retry.state["workflow_status"], "applied")

    def test_replay_uses_complete_immutable_result_snapshot(self):
        key = "complete-result-snapshot-00001"
        first = self.create(
            "applied",
            idempotency_key=key,
            title="Snapshot fixture",
            url="https://example.test/snapshot-fixture",
        )
        original = {
            "pipeline_item": first.pipeline_item,
            "state": first.state,
            "compatibility": first.compatibility_state,
            "applicant": first.applicant_update,
            "created": first.created,
        }
        update_id = first.applicant_update["update_id"]
        self.conn.execute(
            """
            UPDATE applicant_status_updates
            SET user_id='changed', anonymous_user_key='changed', profile_id='changed',
                source='changed', opportunity_title='changed', opportunity_url='changed',
                opportunity_external_id='changed', canonical_id=999, status='rejected',
                previous_status='changed', status_date='2099-01-01',
                reported_at='2099-01-01T00:00:00+00:00', evidence_type='changed',
                confidence_level='changed', notes='changed', is_sample=1
            WHERE update_id=?
            """,
            (update_id,),
        )
        self.conn.execute(
            "UPDATE user_pipeline_items SET status='rejected', notes='changed', updated_at='2099-01-01' WHERE pipeline_item_id=?",
            (first.pipeline_item["pipeline_item_id"],),
        )
        self.conn.execute(
            "UPDATE user_pipeline_state SET workflow_status='rejected' WHERE pipeline_item_id=?",
            (first.pipeline_item["pipeline_item_id"],),
        )
        self.conn.commit()
        before = {
            table: [tuple(row) for row in self.conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in (
                "user_pipeline_items",
                "user_pipeline_state",
                "user_pipeline_transitions",
                "applicant_status_updates",
            )
        }
        replay = self.create(
            "applied",
            idempotency_key=key,
            title="Snapshot fixture",
            url="https://example.test/snapshot-fixture",
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.pipeline_item, original["pipeline_item"])
        self.assertEqual(replay.state, original["state"])
        self.assertEqual(replay.compatibility_state, original["compatibility"])
        self.assertEqual(replay.applicant_update, original["applicant"])
        self.assertEqual(replay.created, original["created"])
        after = {
            table: [tuple(row) for row in self.conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in before
        }
        self.assertEqual(after, before)

    def test_legacy_accepted_repeated_actions_record_noop_without_compatibility_writes(self):
        starts = [
            ("saved", "save"),
            ("applied", "applied"),
            ("assessment_started", "assessment_started"),
            ("assessment_completed", "assessment_completed"),
            ("accepted", "accepted"),
            ("rejected", "rejected"),
        ]
        for index, (status, action) in enumerate(starts):
            starting = self.insert_known(status, is_sample=1)
            item_id = starting.pipeline_item["pipeline_item_id"]
            before_item = dict(self.conn.execute(
                "SELECT * FROM user_pipeline_items WHERE pipeline_item_id=?", (item_id,)
            ).fetchone())
            applicant_count = self.counts()[-1]
            result = self.act(starting, action, note=f"Repeated {index}")
            self.assertEqual(result.state["version"], starting.state["version"] + 1)
            self.assertEqual(result.transition["before_state"], result.transition["after_state"])
            self.assertEqual(result.transition["metadata"]["transition_class"], "operation_noop")
            self.assertEqual(dict(self.conn.execute(
                "SELECT * FROM user_pipeline_items WHERE pipeline_item_id=?", (item_id,)
            ).fetchone()), before_item)
            self.assertEqual(self.counts()[-1], applicant_count)
            self.assertEqual(
                pipeline_state.list_effective_funnel_transitions(self.conn, item_id, "profile-a"),
                [],
            )
            replay = pipeline_actions.perform_pipeline_action(
                self.conn,
                action=action,
                owner_profile_id="profile-a",
                idempotency_key=result.transition["idempotency_key"],
                expected_version=starting.state["version"],
                match_run_id="match-run-a",
                pipeline_item_id=item_id,
                note=f"Repeated {index}",
            )
            self.assertTrue(replay.replayed)
            if index == 0:
                second_noop = pipeline_actions.perform_pipeline_action(
                    self.conn,
                    action=action,
                    owner_profile_id="profile-a",
                    idempotency_key="second-fresh-noop-00000001",
                    expected_version=result.state["version"],
                    match_run_id="match-run-a",
                    pipeline_item_id=item_id,
                    note="Second repeated save",
                )
                self.assertEqual(second_noop.state["version"], result.state["version"] + 1)
                self.assertEqual(
                    dict(self.conn.execute(
                        "SELECT * FROM user_pipeline_items WHERE pipeline_item_id=?",
                        (item_id,),
                    ).fetchone()),
                    before_item,
                )

        reminded = self.insert_known(
            "applied", reminder_at="2026-09-01T12:00:00+00:00"
        )
        reminder_noop = self.act(
            reminded,
            "remind_later",
            reminder_at="2026-09-01T12:00:00+00:00",
        )
        self.assertEqual(reminder_noop.transition["metadata"]["transition_class"], "operation_noop")
        hidden = self.insert_known("saved", visibility="hidden")
        hidden_noop = self.act(hidden, "not_interested")
        self.assertEqual(hidden_noop.transition["metadata"]["transition_class"], "operation_noop")
        visible_saved = self.insert_known("saved")
        show_noop = self.act(visible_saved, "show_again")
        self.assertEqual(show_noop.transition["metadata"]["transition_class"], "operation_noop")
        visible_applied = self.insert_known("applied")
        with self.assertRaises(pipeline_state.InvalidTransition):
            self.act(visible_applied, "show_again")

    def test_user_initialization_is_separate_from_migration_baselines(self):
        saved = self.create("save", title="Initialization save", url="https://example.test/init-save")
        applied = self.create("applied", title="Initialization apply", url="https://example.test/init-apply")
        hidden = self.create("not_interested", title="Initialization hide", url="https://example.test/init-hide")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM user_pipeline_transitions WHERE affected_dimension='baseline'").fetchone()[0],
            0,
        )
        for result, final_action in (
            (saved, "product_noop_save"),
            (applied, "product_applied"),
            (hidden, "product_not_interested"),
        ):
            history = pipeline_state.list_transition_history(
                self.conn, result.pipeline_item["pipeline_item_id"], "profile-a"
            )
            self.assertEqual(history[0]["action_name"], "user_created")
            self.assertEqual(history[0]["metadata"]["transition_class"], "user_initialization")
            self.assertEqual(history[-1]["action_name"], final_action)

    def test_repeated_action_concurrency_has_one_noop_and_one_replay(self):
        for status, action in (("saved", "save"), ("applied", "applied")):
            starting = self.insert_known(status)
            item_id = starting.pipeline_item["pipeline_item_id"]
            self.conn.commit()
            barrier = threading.Barrier(2)
            results = []
            errors = []

            def worker():
                conn = self.connect(timeout=10)
                try:
                    barrier.wait()
                    results.append(
                        pipeline_actions.perform_pipeline_action(
                            conn,
                            action=action,
                            owner_profile_id="profile-a",
                            idempotency_key=f"repeated-race-{status}-000001",
                            expected_version=starting.state["version"],
                            match_run_id="run-repeat-race",
                            pipeline_item_id=item_id,
                        )
                    )
                except Exception as exc:  # pragma: no cover - assertion reports details
                    errors.append(exc)
                finally:
                    conn.close()

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(sorted(result.replayed for result in results), [False, True])
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM user_pipeline_transitions WHERE pipeline_item_id=?",
                    (item_id,),
                ).fetchone()[0],
                2,
            )

    def test_initialization_and_operation_noop_cannot_be_corrected_or_undone(self):
        created = self.create(
            "save", title="Protected transitions", url="https://example.test/protected"
        )
        history = pipeline_state.list_transition_history(
            self.conn, created.pipeline_item["pipeline_item_id"], "profile-a"
        )
        for transition in history:
            with self.assertRaises(pipeline_state.InvalidTransition):
                pipeline_state.undo_transition(
                    self.conn,
                    pipeline_item_id=created.pipeline_item["pipeline_item_id"],
                    owner_profile_id="profile-a",
                    transition_id=transition["transition_id"],
                    expected_version=created.state["version"],
                    idempotency_key=self.key("protected-undo"),
                )
            with self.assertRaises(pipeline_state.InvalidTransition):
                pipeline_state.correct_state(
                    self.conn,
                    pipeline_item_id=created.pipeline_item["pipeline_item_id"],
                    owner_profile_id="profile-a",
                    corrected_state=created.state,
                    correction_of_transition_id=transition["transition_id"],
                    expected_version=created.state["version"],
                    idempotency_key=self.key("protected-correction"),
                )

    def test_replay_fails_closed_for_malformed_result_snapshot(self):
        key = "malformed-result-replay-00001"
        created = self.create(
            "save",
            idempotency_key=key,
            title="Malformed replay",
            url="https://example.test/malformed-replay",
        )
        self.conn.commit()
        self.conn.execute("DROP TRIGGER trg_user_pipeline_transitions_no_update")
        metadata = created.transition["metadata"]
        metadata["pipeline_action"]["result_snapshot"].pop("compatibility_state")
        self.conn.execute(
            "UPDATE user_pipeline_transitions SET metadata_json=? WHERE transition_id=?",
            (
                pipeline_state.canonical_json(metadata),
                created.transition["transition_id"],
            ),
        )
        self.conn.executescript(
            """
            CREATE TRIGGER trg_user_pipeline_transitions_no_update
            BEFORE UPDATE ON user_pipeline_transitions
            BEGIN SELECT RAISE(ABORT, 'user_pipeline_transitions is append-only'); END;
            """
        )
        before = self.counts()
        with self.assertRaisesRegex(
            pipeline_actions.PipelineInvariantError,
            "result snapshot is malformed",
        ):
            self.create(
                "save",
                idempotency_key=key,
                title="Malformed replay",
                url="https://example.test/malformed-replay",
            )
        self.assertEqual(self.counts(), before)

    def test_replay_rejects_typed_cross_field_and_applicant_identity_tampering(self):
        donor = self.create(
            "applied",
            title="Applicant donor",
            url="https://example.test/applicant-donor",
        )

        def run_case(label, action, mutate):
            key = f"typed-replay-{label}-0000000001"
            title = f"Typed replay {label}"
            url = f"https://example.test/typed-replay/{label}"
            original = self.create(
                action,
                idempotency_key=key,
                title=title,
                url=url,
            )
            metadata = copy.deepcopy(original.transition["metadata"])
            mutate(metadata, donor)
            self.replace_transition_metadata(original.transition["transition_id"], metadata)
            before = self.counts()
            with self.assertRaisesRegex(
                pipeline_actions.PipelineInvariantError,
                "result snapshot is malformed",
            ) as raised:
                self.create(
                    action,
                    idempotency_key=key,
                    title=title,
                    url=url,
                )
            self.assertNotIn(label, str(raised.exception))
            self.assertEqual(self.counts(), before)

        cases = {
            "changed_update_id": (
                "applied",
                lambda metadata, _donor: metadata["pipeline_action"]["result_snapshot"][
                    "applicant_update"
                ].__setitem__("update_id", "applicant-update::tampered"),
            ),
            "valid_other_update_id": (
                "applied",
                lambda metadata, other: metadata["pipeline_action"]["result_snapshot"][
                    "applicant_update"
                ].__setitem__("update_id", other.applicant_update["update_id"]),
            ),
            "list_notes": (
                "applied",
                lambda metadata, _donor: metadata["pipeline_action"]["result_snapshot"][
                    "compatibility_state"
                ].__setitem__("notes", []),
            ),
            "boolean_version": (
                "applied",
                lambda metadata, _donor: metadata["pipeline_action"]["result_snapshot"][
                    "normalized_state"
                ].__setitem__("version", True),
            ),
            "string_is_sample": (
                "applied",
                lambda metadata, _donor: metadata["pipeline_action"]["result_snapshot"][
                    "compatibility_state"
                ].__setitem__("is_sample", "0"),
            ),
            "malformed_timestamp": (
                "applied",
                lambda metadata, _donor: metadata["pipeline_action"]["result_snapshot"][
                    "compatibility_state"
                ].__setitem__("updated_at", "not-a-timestamp"),
            ),
            "numeric_pipeline_item_id": (
                "applied",
                lambda metadata, _donor: metadata["pipeline_action"]["result_snapshot"][
                    "pipeline_item"
                ].__setitem__("pipeline_item_id", 123),
            ),
            "unknown_workflow": (
                "applied",
                lambda metadata, _donor: metadata["pipeline_action"]["result_snapshot"][
                    "normalized_state"
                ].__setitem__("workflow_status", "unknown"),
            ),
            "unknown_provenance": (
                "applied",
                lambda metadata, _donor: metadata["pipeline_action"]["result_snapshot"][
                    "normalized_state"
                ].__setitem__("workflow_status_provenance", "unknown"),
            ),
            "unknown_visibility": (
                "applied",
                lambda metadata, _donor: metadata["pipeline_action"]["result_snapshot"][
                    "normalized_state"
                ].__setitem__("visibility", "unknown"),
            ),
            "missing_nested_field": (
                "applied",
                lambda metadata, _donor: metadata["pipeline_action"]["result_snapshot"][
                    "pipeline_item"
                ].pop("source"),
            ),
            "unexpected_nested_field": (
                "applied",
                lambda metadata, _donor: metadata["pipeline_action"]["result_snapshot"][
                    "compatibility_state"
                ].__setitem__("unexpected", "value"),
            ),
            "forbidden_applicant_result": (
                "save",
                lambda metadata, other: metadata["pipeline_action"]["result_snapshot"].__setitem__(
                    "applicant_update", copy.deepcopy(other.applicant_update)
                ),
            ),
            "missing_applicant_result": (
                "applied",
                lambda metadata, _donor: metadata["pipeline_action"]["result_snapshot"].__setitem__(
                    "applicant_update", None
                ),
            ),
            "receipt_result_disagreement": (
                "applied",
                lambda metadata, _donor: metadata["pipeline_action"]["applicant_receipt"][
                    "applicant_update"
                ].__setitem__("notes", "different"),
            ),
            "terminal_state_disagreement": (
                "applied",
                lambda metadata, _donor: metadata["pipeline_action"]["result_snapshot"][
                    "normalized_state"
                ].__setitem__("visibility", "hidden"),
            ),
            "compatibility_disagreement": (
                "applied",
                lambda metadata, _donor: metadata["pipeline_action"]["result_snapshot"][
                    "compatibility_state"
                ].__setitem__("status", "saved"),
            ),
        }
        for label, (action, mutate) in cases.items():
            with self.subTest(label=label):
                run_case(label, action, mutate)

    def test_replay_rejects_invalid_metadata_json_with_generic_integrity_error(self):
        key = "invalid-json-replay-000000001"
        result = self.create(
            "save",
            idempotency_key=key,
            title="Invalid JSON replay",
            url="https://example.test/invalid-json-replay",
        )
        self.conn.commit()
        self.conn.execute("DROP TRIGGER trg_user_pipeline_transitions_no_update")
        self.conn.execute(
            "UPDATE user_pipeline_transitions SET metadata_json='{' WHERE transition_id=?",
            (result.transition["transition_id"],),
        )
        self.conn.executescript(
            """
            CREATE TRIGGER trg_user_pipeline_transitions_no_update
            BEFORE UPDATE ON user_pipeline_transitions
            BEGIN SELECT RAISE(ABORT, 'user_pipeline_transitions is append-only'); END;
            """
        )
        self.conn.commit()
        before = self.counts()
        with self.assertRaisesRegex(
            pipeline_actions.PipelineInvariantError,
            "result snapshot is malformed",
        ):
            self.create(
                "save",
                idempotency_key=key,
                title="Invalid JSON replay",
                url="https://example.test/invalid-json-replay",
            )
        self.assertEqual(self.counts(), before)

    def test_additional_exact_concurrency_scenarios_do_not_leave_losers(self):
        def race(kwargs):
            self.conn.commit()
            barrier = threading.Barrier(2)
            results = []
            errors = []

            def worker():
                conn = self.connect(timeout=10)
                try:
                    barrier.wait()
                    results.append(pipeline_actions.perform_pipeline_action(conn, **kwargs))
                except Exception as exc:  # pragma: no cover - assertion reports details
                    errors.append(exc)
                finally:
                    conn.close()

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(sorted(result.replayed for result in results), [False, True])
            return results[0]

        applied = race(
            {
                "action": "applied",
                "owner_profile_id": "profile-a",
                "idempotency_key": "race-untracked-applied-00001",
                "expected_version": 0,
                "match_run_id": "run-race",
                "source": "Fixture",
                "title": "Race untracked applied",
                "url": "https://example.test/race-untracked-applied",
            }
        )
        self.assertEqual(applied.state["workflow_status"], "applied")

        unknown = self.insert_unknown(visibility="hidden", reminder_at=None)
        shown = race(
            {
                "action": "show_again_as_saved",
                "owner_profile_id": "profile-a",
                "idempotency_key": "race-hidden-unknown-show-001",
                "expected_version": 1,
                "match_run_id": "run-race",
                "pipeline_item_id": unknown,
            }
        )
        self.assertEqual(shown.state["workflow_status"], "saved")
        self.assertEqual(shown.state["visibility"], "visible")


if __name__ == "__main__":
    unittest.main()
