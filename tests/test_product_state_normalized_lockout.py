import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import scripts.product_state as product_state


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "wahojobs" / "db" / "schema.sql"
MIGRATION = ROOT / "wahojobs" / "db" / "migrations" / "001_pipeline_state.sql"
sys.path.insert(0, str(ROOT / "scripts"))
import pipeline_state_migration as migration  # noqa: E402


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


class ProductStateNormalizedLockoutTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "normalized.sqlite"
        with self.connect() as conn:
            conn.executescript(SCHEMA.read_text(encoding="utf-8"))
            for statement in migration.iter_sql_statements(
                MIGRATION.read_text(encoding="utf-8")
            ):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO wahojobs_schema_migrations (version) VALUES (?)",
                (migration.MIGRATION_VERSION,),
            )
            conn.execute(
                "INSERT INTO user_profiles (profile_id,user_id,display_name) VALUES ('profile-a','user-a','Profile A')"
            )
        self.connection_patch = mock.patch.object(
            product_state, "get_connection", self.connect
        )
        self.connection_patch.start()

    def tearDown(self):
        self.connection_patch.stop()
        self.temp_dir.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.path, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def assert_locked_without_write(self, callback, command):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(SystemExit, f"{command} is disabled"):
            callback()
        self.assertEqual(self.path.read_bytes(), before)
        with self.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM user_pipeline_items").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM user_pipeline_transitions").fetchone()[0],
                0,
            )

    def test_all_known_legacy_pipeline_writers_are_locked_out(self):
        cases = (
            (
                "save-opportunity",
                lambda: product_state.save_opportunity(
                    SimpleNamespace(
                        profile="profile-a",
                        source="Fixture",
                        title="Fixture role",
                        url="https://example.test/fixture",
                        note="",
                    )
                ),
            ),
            (
                "update-status",
                lambda: product_state.update_pipeline_status(
                    SimpleNamespace(
                        pipeline_item_id="missing",
                        status="applied",
                        status_date="2026-07-12",
                        note="",
                    )
                ),
            ),
            (
                "remind-later",
                lambda: product_state.remind_later(
                    SimpleNamespace(
                        pipeline_item_id="missing",
                        reminder_date="2026-07-19",
                        note="",
                    )
                ),
            ),
            (
                "mark-not-interested",
                lambda: product_state.mark_not_interested(
                    SimpleNamespace(pipeline_item_id="missing", note="")
                ),
            ),
            (
                "import-pipeline",
                lambda: product_state.import_pipeline(
                    ROOT / "profiles" / "sample_user_pipeline.json"
                ),
            ),
        )
        for command, callback in cases:
            with self.subTest(command=command):
                self.assert_locked_without_write(callback, command)

    def test_legacy_schema_without_migration_marker_remains_supported(self):
        with self.connect() as conn:
            conn.execute("DROP TRIGGER trg_user_pipeline_transitions_no_update")
            conn.execute("DROP TRIGGER trg_user_pipeline_transitions_no_delete")
            conn.execute("DROP TABLE user_pipeline_transitions")
            conn.execute("DROP TABLE user_pipeline_state")
            conn.execute("DROP TABLE wahojobs_schema_migrations")
        product_state.save_opportunity(
            SimpleNamespace(
                profile="profile-a",
                source="Fixture",
                title="Legacy fixture role",
                url="https://example.test/legacy-fixture",
                note="",
            )
        )
        with self.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM user_pipeline_items").fetchone()[0],
                1,
            )


if __name__ == "__main__":
    unittest.main()
