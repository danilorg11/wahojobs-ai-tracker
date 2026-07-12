import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from wahojobs import pipeline_actions, pipeline_records, pipeline_state


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "wahojobs" / "db" / "schema.sql"
MIGRATION = ROOT / "wahojobs" / "db" / "migrations" / "001_pipeline_state.sql"
sys.path.insert(0, str(ROOT / "scripts"))
import pipeline_state_migration as migration  # noqa: E402


class PipelineRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "records.sqlite"
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

    def create(self):
        return pipeline_actions.perform_pipeline_action(
            self.conn,
            action="applied",
            owner_profile_id="profile-a",
            idempotency_key="record-create-0000000001",
            expected_version=0,
            match_run_id="run-record",
            source="Fixture",
            title="Record fixture",
            url="https://example.test/record",
        )

    def test_loader_returns_cohesive_normalized_and_compatibility_record(self):
        created = self.create()
        record = pipeline_records.load_pipeline_record(
            self.conn,
            created.pipeline_item["pipeline_item_id"],
            owner_profile_id="profile-a",
            mutation_grade=True,
        )
        payload = record.as_dict()
        self.assertEqual(payload["persisted_owner"], {"user_id": "user-a", "profile_id": "profile-a"})
        self.assertEqual(payload["normalized_state"]["workflow_status"], "applied")
        self.assertEqual(payload["normalized_state"]["version"], 2)
        self.assertEqual(payload["compatibility"]["status"], "applied")
        self.assertTrue(payload["compatibility"]["matches_normalized"])
        self.assertEqual(payload["diagnostics"]["invariants"], [])

    def test_loader_rejects_cross_owner(self):
        created = self.create()
        with self.assertRaises(pipeline_state.OwnershipError):
            pipeline_records.load_pipeline_record(
                self.conn,
                created.pipeline_item["pipeline_item_id"],
                owner_profile_id="profile-b",
            )

    def test_loader_reports_missing_projection_without_repairing(self):
        self.conn.execute(
            """
            INSERT INTO user_pipeline_items (
              pipeline_item_id,user_id,profile_id,source,opportunity_title,status
            ) VALUES ('missing','user-a','profile-a','Fixture','Missing','saved')
            """
        )
        before = self.conn.total_changes
        record = pipeline_records.load_pipeline_record(self.conn, "missing")
        self.assertIn("missing_projection", record.diagnostics["invariants"])
        self.assertIsNone(record.normalized_state)
        self.assertEqual(self.conn.total_changes, before)
        with self.assertRaises(pipeline_records.PipelineRecordInvariant):
            pipeline_records.load_pipeline_record(
                self.conn, "missing", mutation_grade=True
            )

    def test_loader_does_not_install_migration_objects(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite"
        legacy = sqlite3.connect(legacy_path)
        legacy.row_factory = sqlite3.Row
        legacy.executescript(SCHEMA.read_text(encoding="utf-8"))
        before = {
            row["name"]
            for row in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        with self.assertRaises(pipeline_records.PipelineRecordInvariant):
            pipeline_records.load_pipeline_record(legacy, "anything")
        after = {
            row["name"]
            for row in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertEqual(after, before)
        legacy.close()

    def test_loader_exposes_unresolved_and_rejects_visible_unknown_without_reminder(self):
        self.conn.execute(
            """
            INSERT INTO user_pipeline_items (
              pipeline_item_id,user_id,profile_id,source,opportunity_title,status,reminder_date
            ) VALUES ('unknown','user-a','profile-a','Fixture','Unknown','remind_later','2026-07-25')
            """
        )
        pipeline_state.initialize_projection(
            self.conn,
            pipeline_item_id="unknown",
            owner_profile_id="profile-a",
            workflow_status=None,
            workflow_status_provenance="unknown_legacy",
            visibility="visible",
            reminder_at="2026-07-25T10:00:00+00:00",
            idempotency_key="unknown-baseline-00000001",
        )
        record = pipeline_records.load_pipeline_record(
            self.conn, "unknown", mutation_grade=True
        )
        self.assertTrue(record.diagnostics["unresolved_workflow"])
        self.assertTrue(record.diagnostics["mutation_grade"])

        self.conn.execute(
            "UPDATE user_pipeline_state SET reminder_at=NULL WHERE pipeline_item_id='unknown'"
        )
        record = pipeline_records.load_pipeline_record(self.conn, "unknown")
        self.assertIn("visible_unknown_without_reminder", record.diagnostics["invariants"])
        with self.assertRaises(pipeline_records.PipelineRecordInvariant):
            pipeline_records.load_pipeline_record(
                self.conn, "unknown", mutation_grade=True
            )

    def test_loader_keeps_same_opportunity_isolated_by_persisted_profile(self):
        self.conn.execute(
            "INSERT INTO user_profiles (profile_id,user_id,display_name) VALUES ('profile-b','user-b','Profile B')"
        )
        first = pipeline_actions.perform_pipeline_action(
            self.conn,
            action="save",
            owner_profile_id="profile-a",
            idempotency_key="identity-profile-a-00000001",
            expected_version=0,
            match_run_id="run-a",
            source="Fixture",
            title="Shared opportunity",
            url="https://example.test/shared",
        )
        second = pipeline_actions.perform_pipeline_action(
            self.conn,
            action="save",
            owner_profile_id="profile-b",
            idempotency_key="identity-profile-b-00000001",
            expected_version=0,
            match_run_id="run-b",
            source="Fixture",
            title="Shared opportunity",
            url="https://example.test/shared",
        )
        self.assertNotEqual(
            first.pipeline_item["pipeline_item_id"], second.pipeline_item["pipeline_item_id"]
        )
        first_record = pipeline_records.load_pipeline_record(
            self.conn, first.pipeline_item["pipeline_item_id"], mutation_grade=True
        )
        second_record = pipeline_records.load_pipeline_record(
            self.conn, second.pipeline_item["pipeline_item_id"], mutation_grade=True
        )
        self.assertEqual(first_record.persisted_owner["profile_id"], "profile-a")
        self.assertEqual(second_record.persisted_owner["profile_id"], "profile-b")


if __name__ == "__main__":
    unittest.main()
