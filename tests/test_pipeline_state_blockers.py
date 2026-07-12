import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from wahojobs import pipeline_state


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "wahojobs" / "db" / "schema.sql"
MIGRATION_DDL_PATH = ROOT / "wahojobs" / "db" / "migrations" / "001_pipeline_state.sql"
MIGRATION_SCRIPT = ROOT / "scripts" / "pipeline_state_migration.py"
sys.path.insert(0, str(ROOT / "scripts"))
import local_product_app  # noqa: E402
import pipeline_state_migration as migration  # noqa: E402
import product_state  # noqa: E402


class PipelineStateBlockerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def connect(self, path):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize_legacy(self, path):
        conn = self.connect(path)
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
        return conn

    def install_migration_for_service_test(self, conn):
        for statement in migration.iter_sql_statements(
            MIGRATION_DDL_PATH.read_text(encoding="utf-8")
        ):
            conn.execute(statement)
        conn.execute(
            "INSERT INTO wahojobs_schema_migrations (version) VALUES (?)",
            (migration.MIGRATION_VERSION,),
        )
        conn.commit()

    def insert_item(self, conn, item_id, status="saved", profile="profile-a"):
        conn.execute(
            """
            INSERT INTO user_pipeline_items (
              pipeline_item_id, user_id, profile_id, source, opportunity_title,
              opportunity_url, status, status_date, reminder_date, created_at, updated_at
            )
            VALUES (?, 'user-a', ?, 'Fixture', ?, ?, ?, '2026-07-12', '',
                    '2026-07-12T00:00:00+00:00', '2026-07-12T00:00:00+00:00')
            """,
            (item_id, profile, f"Opportunity {item_id}", f"https://example.test/{item_id}", status),
        )
        conn.commit()

    def initialize_projection(self, conn, item_id, status="saved", profile="profile-a"):
        return pipeline_state.initialize_projection(
            conn,
            pipeline_item_id=item_id,
            owner_profile_id=profile,
            workflow_status=status,
            workflow_status_provenance="known",
            visibility="visible",
            reminder_at=None,
            idempotency_key=f"initialize:{item_id}",
            actor_source="test",
        )

    def schema_snapshot(self, conn):
        return [
            tuple(row)
            for row in conn.execute(
                """
                SELECT type, name, tbl_name, sql
                FROM sqlite_master
                WHERE name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            )
        ]

    def base_row_snapshot(self, conn):
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM user_pipeline_items ORDER BY pipeline_item_id"
            )
        ]

    def test_runtime_initializers_do_not_install_pipeline_migration(self):
        db_path = self.temp_path / "runtime.sqlite"
        self._run_repository_initializer(db_path)
        before_objects = self._object_names(db_path)

        product_state.initialize_product_state_schema(db_path)
        after_product_state = self._object_names(db_path)
        self.assertEqual(before_objects, after_product_state)
        self.assertNotIn("user_pipeline_state", after_product_state)
        self.assertNotIn("user_pipeline_transitions", after_product_state)

        self._run_repository_initializer(db_path)
        after_repository = self._object_names(db_path)
        self.assertEqual(before_objects, after_repository)
        before_stat = db_path.stat()

        class FakeServer:
            def __init__(self, *_args, **_kwargs):
                pass

            def serve_forever(self):
                return None

            def server_close(self):
                return None

        initialize_product_state_schema = product_state.initialize_product_state_schema
        with mock.patch.object(
            local_product_app.product_state,
            "initialize_product_state_schema",
            side_effect=lambda: initialize_product_state_schema(db_path),
        ), mock.patch.object(local_product_app, "seed_local_product_profiles"), mock.patch.object(
            local_product_app,
            "ThreadingHTTPServer",
            FakeServer,
        ), mock.patch.object(
            local_product_app,
            "parse_args",
            return_value=SimpleNamespace(host="127.0.0.1", port=0, demo=False),
        ):
            local_product_app.main()
        after_local_app = self._object_names(db_path)
        self.assertEqual(before_objects, after_local_app)
        self.assertEqual(before_stat.st_size, db_path.stat().st_size)
        self.assertEqual(before_stat.st_mtime_ns, db_path.stat().st_mtime_ns)

    def test_version_sensitive_idempotency_and_invalid_versions(self):
        db_path = self.temp_path / "idempotency.sqlite"
        conn = self.initialize_legacy(db_path)
        self.install_migration_for_service_test(conn)
        self.insert_item(conn, "item-a")
        self.insert_item(conn, "item-b")
        self.initialize_projection(conn, "item-a")
        self.initialize_projection(conn, "item-b")
        first = pipeline_state.change_workflow_status(
            conn,
            pipeline_item_id="item-a",
            owner_profile_id="profile-a",
            workflow_status="applied",
            expected_version=1,
            idempotency_key="apply:item-a",
        )
        pipeline_state.change_workflow_status(
            conn,
            pipeline_item_id="item-a",
            owner_profile_id="profile-a",
            workflow_status="waiting",
            expected_version=2,
            idempotency_key="wait:item-a",
        )
        replay = pipeline_state.change_workflow_status(
            conn,
            pipeline_item_id="item-a",
            owner_profile_id="profile-a",
            workflow_status="applied",
            expected_version=1,
            idempotency_key="apply:item-a",
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(first.transition, replay.transition)
        self.assertEqual(
            pipeline_state.get_current_state(conn, "item-a", "profile-a")["workflow_status"],
            "waiting",
        )
        with self.assertRaises(pipeline_state.IdempotencyConflict):
            pipeline_state.change_workflow_status(
                conn,
                pipeline_item_id="item-a",
                owner_profile_id="profile-a",
                workflow_status="applied",
                expected_version=999,
                idempotency_key="apply:item-a",
            )
        with self.assertRaises(pipeline_state.IdempotencyConflict):
            pipeline_state.change_workflow_status(
                conn,
                pipeline_item_id="item-a",
                owner_profile_id="profile-a",
                workflow_status="applied",
                expected_version=1,
                idempotency_key="apply:item-a",
                metadata={"semantic_note": "different request"},
            )
        with self.assertRaises(pipeline_state.IdempotencyConflict):
            pipeline_state.change_workflow_status(
                conn,
                pipeline_item_id="item-b",
                owner_profile_id="profile-a",
                workflow_status="applied",
                expected_version=1,
                idempotency_key="apply:item-a",
            )
        for invalid in (0, -1, "1"):
            with self.assertRaises(pipeline_state.StaleStateVersion):
                pipeline_state.change_workflow_status(
                    conn,
                    pipeline_item_id="item-b",
                    owner_profile_id="profile-a",
                    workflow_status="applied",
                    expected_version=invalid,
                    idempotency_key=f"invalid:{invalid!r}",
                )
        self.assertNotEqual(
            pipeline_state.request_fingerprint({"operation": "initialize"}),
            pipeline_state.request_fingerprint(
                {"operation": "initialize", "expected_version": 0}
            ),
        )
        conn.close()

    def test_existing_key_owner_conflicts_are_private_and_non_mutating(self):
        db_path = self.temp_path / "owner-idempotency.sqlite"
        conn = self.initialize_legacy(db_path)
        self.install_migration_for_service_test(conn)
        self.insert_item(conn, "private-item-a", profile="profile-private-a")
        self.insert_item(conn, "private-item-b", profile="profile-private-b")
        self.initialize_projection(conn, "private-item-a", profile="profile-private-a")
        self.initialize_projection(conn, "private-item-b", profile="profile-private-b")
        original = pipeline_state.change_workflow_status(
            conn,
            pipeline_item_id="private-item-a",
            owner_profile_id="profile-private-a",
            workflow_status="applied",
            expected_version=1,
            idempotency_key="private-global-key",
        )
        before_state = pipeline_state.get_current_state(
            conn, "private-item-a", "profile-private-a"
        )
        before_count = conn.execute(
            "SELECT COUNT(*) FROM user_pipeline_transitions"
        ).fetchone()[0]
        stored = conn.execute(
            "SELECT * FROM user_pipeline_transitions WHERE idempotency_key = ?",
            ("private-global-key",),
        ).fetchone()

        conflicts = []
        for item, owner in (
            ("private-item-a", "profile-private-b"),
            ("private-item-b", "profile-private-b"),
        ):
            with self.assertRaises(pipeline_state.IdempotencyConflict) as raised:
                pipeline_state.change_workflow_status(
                    conn,
                    pipeline_item_id=item,
                    owner_profile_id=owner,
                    workflow_status="applied",
                    expected_version=1,
                    idempotency_key="private-global-key",
                )
            conflicts.append(raised.exception)

        forbidden = {
            stored["profile_id"],
            stored["pipeline_item_id"],
            stored["action_name"],
            stored["request_fingerprint"],
            stored["transition_id"],
            stored["after_state_json"],
        }
        for conflict in conflicts:
            public_error = " ".join(
                (str(conflict), repr(conflict.args), repr(vars(conflict)))
            )
            for detail in forbidden:
                self.assertNotIn(detail, public_error)
            self.assertEqual(vars(conflict), {})

        with self.assertRaises(pipeline_state.OwnershipError):
            pipeline_state.change_workflow_status(
                conn,
                pipeline_item_id="private-item-a",
                owner_profile_id="profile-private-b",
                workflow_status="waiting",
                expected_version=2,
                idempotency_key="fresh-wrong-owner-key",
            )
        self.assertIsNone(
            conn.execute(
                "SELECT 1 FROM user_pipeline_transitions WHERE idempotency_key = ?",
                ("fresh-wrong-owner-key",),
            ).fetchone()
        )
        self.assertEqual(
            pipeline_state.get_current_state(
                conn, "private-item-a", "profile-private-a"
            ),
            before_state,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM user_pipeline_transitions").fetchone()[0],
            before_count,
        )
        self.assertEqual(original.state, before_state)
        conn.close()

    def test_undo_and_correction_keys_include_their_target(self):
        db_path = self.temp_path / "compensating-idempotency.sqlite"
        conn = self.initialize_legacy(db_path)
        self.install_migration_for_service_test(conn)
        self.insert_item(conn, "undo-item")
        baseline = self.initialize_projection(conn, "undo-item")
        applied = self._workflow(conn, "undo-item", "applied", 1, "undo:apply")
        undo = pipeline_state.undo_transition(
            conn,
            pipeline_item_id="undo-item",
            owner_profile_id="profile-a",
            transition_id=applied.transition["transition_id"],
            expected_version=2,
            idempotency_key="undo:key",
        )
        replay = pipeline_state.undo_transition(
            conn,
            pipeline_item_id="undo-item",
            owner_profile_id="profile-a",
            transition_id=applied.transition["transition_id"],
            expected_version=2,
            idempotency_key="undo:key",
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(undo.transition, replay.transition)
        with self.assertRaises(pipeline_state.IdempotencyConflict):
            pipeline_state.undo_transition(
                conn,
                pipeline_item_id="undo-item",
                owner_profile_id="profile-a",
                transition_id=baseline.transition["transition_id"],
                expected_version=2,
                idempotency_key="undo:key",
            )

        self.insert_item(conn, "correction-item")
        correction_baseline = self.initialize_projection(conn, "correction-item")
        correction_applied = self._workflow(
            conn, "correction-item", "applied", 1, "correction:apply"
        )
        corrected = self._correction(
            conn,
            "correction-item",
            correction_applied,
            "saved",
            2,
            "correction:key",
        )
        corrected_replay = self._correction(
            conn,
            "correction-item",
            correction_applied,
            "saved",
            2,
            "correction:key",
        )
        self.assertTrue(corrected_replay.replayed)
        self.assertEqual(corrected.transition, corrected_replay.transition)
        with self.assertRaises(pipeline_state.IdempotencyConflict):
            self._correction(
                conn,
                "correction-item",
                correction_baseline,
                "saved",
                2,
                "correction:key",
            )
        conn.close()

    def test_concurrent_identical_requests_create_one_transition(self):
        db_path = self.temp_path / "concurrent.sqlite"
        conn = self.initialize_legacy(db_path)
        self.install_migration_for_service_test(conn)
        self.insert_item(conn, "concurrent-item")
        self.initialize_projection(conn, "concurrent-item")
        conn.close()

        barrier = threading.Barrier(2)
        results = []
        errors = []
        lock = threading.Lock()

        def worker():
            worker_conn = sqlite3.connect(db_path, timeout=10)
            worker_conn.row_factory = sqlite3.Row
            worker_conn.execute("PRAGMA foreign_keys = ON")
            try:
                barrier.wait(timeout=5)
                result = pipeline_state.change_workflow_status(
                    worker_conn,
                    pipeline_item_id="concurrent-item",
                    owner_profile_id="profile-a",
                    workflow_status="applied",
                    expected_version=1,
                    idempotency_key="concurrent:apply",
                )
                with lock:
                    results.append(result)
            except Exception as exc:
                with lock:
                    errors.append(exc)
            finally:
                worker_conn.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(result.replayed for result in results), [False, True])
        verification = self.connect(db_path)
        self.assertEqual(
            verification.execute(
                "SELECT COUNT(*) FROM user_pipeline_transitions WHERE pipeline_item_id = ?",
                ("concurrent-item",),
            ).fetchone()[0],
            2,
        )
        verification.close()

    def test_concurrent_different_owners_share_no_idempotent_result(self):
        db_path = self.temp_path / "concurrent-owner-conflict.sqlite"
        conn = self.initialize_legacy(db_path)
        self.install_migration_for_service_test(conn)
        self.insert_item(conn, "owner-race-a", profile="profile-race-a")
        self.insert_item(conn, "owner-race-b", profile="profile-race-b")
        self.initialize_projection(conn, "owner-race-a", profile="profile-race-a")
        self.initialize_projection(conn, "owner-race-b", profile="profile-race-b")
        conn.close()

        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def worker(item, owner):
            worker_conn = sqlite3.connect(db_path, timeout=10)
            worker_conn.row_factory = sqlite3.Row
            worker_conn.execute("PRAGMA foreign_keys = ON")
            try:
                barrier.wait(timeout=5)
                pipeline_state.change_workflow_status(
                    worker_conn,
                    pipeline_item_id=item,
                    owner_profile_id=owner,
                    workflow_status="applied",
                    expected_version=1,
                    idempotency_key="owner-race-global-key",
                )
                outcome = "success"
            except Exception as exc:
                outcome = type(exc).__name__
            finally:
                worker_conn.close()
            with lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(target=worker, args=("owner-race-a", "profile-race-a")),
            threading.Thread(target=worker, args=("owner-race-b", "profile-race-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(sorted(outcomes), ["IdempotencyConflict", "success"])

        verification = self.connect(db_path)
        versions = {
            row["pipeline_item_id"]: row["version"]
            for row in verification.execute(
                "SELECT pipeline_item_id, version FROM user_pipeline_state"
            )
        }
        self.assertEqual(sorted(versions.values()), [1, 2])
        self.assertEqual(
            verification.execute(
                "SELECT COUNT(*) FROM user_pipeline_transitions"
            ).fetchone()[0],
            3,
        )
        verification.close()

    def test_chain_aware_effective_funnel_history(self):
        db_path = self.temp_path / "chains.sqlite"
        conn = self.initialize_legacy(db_path)
        self.install_migration_for_service_test(conn)
        self.insert_item(conn, "chain")
        self.initialize_projection(conn, "chain")
        applied = self._workflow(conn, "chain", "applied", 1, "chain:applied")
        saved = self._correction(conn, "chain", applied, "saved", 2, "chain:saved")
        self.assertEqual(self._effective_ids(conn, "chain"), [])
        restored = self._correction(conn, "chain", saved, "applied", 3, "chain:restored")
        self.assertEqual(self._effective_ids(conn, "chain"), [restored.transition["transition_id"]])
        hidden = pipeline_state.hide_item(
            conn,
            pipeline_item_id="chain",
            owner_profile_id="profile-a",
            expected_version=4,
            idempotency_key="chain:hidden",
        )
        reminder_at = "2026-07-20T12:00:00+00:00"
        pipeline_state.set_reminder(
            conn,
            pipeline_item_id="chain",
            owner_profile_id="profile-a",
            reminder_at=reminder_at,
            expected_version=5,
            idempotency_key="chain:reminder",
        )
        saved_again = self._correction(
            conn,
            "chain",
            restored,
            "saved",
            6,
            "chain:saved-again",
            visibility="hidden",
            reminder_at=reminder_at,
        )
        restored_again = self._correction(
            conn,
            "chain",
            saved_again,
            "applied",
            7,
            "chain:restored-again",
            visibility="hidden",
            reminder_at=reminder_at,
        )
        self.assertEqual(
            self._effective_ids(conn, "chain"),
            [restored_again.transition["transition_id"]],
        )
        undone = pipeline_state.undo_transition(
            conn,
            pipeline_item_id="chain",
            owner_profile_id="profile-a",
            transition_id=restored_again.transition["transition_id"],
            expected_version=8,
            idempotency_key="chain:undo-correction",
        )
        self.assertEqual(undone.state["workflow_status"], "saved")
        self.assertEqual(self._effective_ids(conn, "chain"), [])
        self.assertEqual(hidden.state["visibility"], "hidden")
        self.assertEqual(undone.state["reminder_at"], reminder_at)
        later_apply = self._workflow(conn, "chain", "applied", 9, "chain:later-apply")
        self.assertEqual(
            self._effective_ids(conn, "chain"),
            [later_apply.transition["transition_id"]],
        )

        self.insert_item(conn, "independent")
        self.initialize_projection(conn, "independent")
        first_apply = self._workflow(conn, "independent", "applied", 1, "independent:first")
        pipeline_state.undo_transition(
            conn,
            pipeline_item_id="independent",
            owner_profile_id="profile-a",
            transition_id=first_apply.transition["transition_id"],
            expected_version=2,
            idempotency_key="independent:undo",
        )
        second_apply = self._workflow(conn, "independent", "applied", 3, "independent:second")
        self.assertEqual(
            self._effective_ids(conn, "independent"),
            [second_apply.transition["transition_id"]],
        )
        conn.close()

    def test_service_rejects_baseline_correction_and_competing_branches(self):
        db_path = self.temp_path / "branches.sqlite"
        conn = self.initialize_legacy(db_path)
        self.install_migration_for_service_test(conn)
        self.insert_item(conn, "branch")
        baseline = self.initialize_projection(conn, "branch")
        with self.assertRaises(pipeline_state.InvalidTransition):
            self._correction(conn, "branch", baseline, "applied", 1, "branch:baseline")
        applied = self._workflow(conn, "branch", "applied", 1, "branch:applied")
        self._correction(conn, "branch", applied, "saved", 2, "branch:first")
        with self.assertRaises(pipeline_state.InvalidTransition):
            self._correction(conn, "branch", applied, "saved", 3, "branch:competing")
        conn.close()

    def test_atomic_migration_rolls_back_every_injected_failure(self):
        points = (
            "after_first_ddl",
            "before_trigger_install",
            "after_all_ddl_before_backfill",
            "after_first_baseline",
            "midway_backfill",
            "before_reconciliation",
            "before_marker_write",
            "after_marker_write",
        )
        for point in points:
            with self.subTest(point=point):
                db_path = self.temp_path / f"failure-{point}.sqlite"
                conn = self.initialize_legacy(db_path)
                for number in range(4):
                    self.insert_item(conn, f"item-{number}")
                before_schema = self.schema_snapshot(conn)
                before_rows = self.base_row_snapshot(conn)

                def fail(reached):
                    if reached == point:
                        raise RuntimeError(f"injected:{point}")

                with self.assertRaisesRegex(RuntimeError, f"injected:{point}"):
                    migration.apply_pipeline_state_migration(conn, failure_injector=fail)
                self.assertEqual(before_schema, self.schema_snapshot(conn))
                self.assertEqual(before_rows, self.base_row_snapshot(conn))
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                conn.close()

    def test_cli_classifies_empty_nonexistent_invalid_and_partial_databases(self):
        empty = self.temp_path / "empty.sqlite"
        empty.touch()
        empty_before = empty.stat()
        result = self._run_cli(empty)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Status: uninitialized", result.stdout)
        self.assertIn("Applicable: no", result.stdout)
        self.assertIn("Planned rows: 0", result.stdout)
        self.assertEqual(empty_before.st_size, empty.stat().st_size)
        self.assertEqual(empty_before.st_mtime_ns, empty.stat().st_mtime_ns)
        self.assertFalse(any(self.temp_path.glob("empty.sqlite-*")))

        missing = self.temp_path / "missing.sqlite"
        result = self._run_cli(missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist", result.stderr)
        self.assertFalse(missing.exists())

        invalid = self.temp_path / "invalid.sqlite"
        invalid.write_bytes(b"not a sqlite database")
        invalid_before = invalid.read_bytes()
        result = self._run_cli(invalid)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid or unreadable SQLite database", result.stderr)
        self.assertEqual(invalid_before, invalid.read_bytes())

        partial = self.temp_path / "partial.sqlite"
        conn = self.initialize_legacy(partial)
        conn.execute(
            "CREATE UNIQUE INDEX idx_user_pipeline_items_pipeline_profile "
            "ON user_pipeline_items(pipeline_item_id, profile_id)"
        )
        conn.commit()
        conn.close()
        result = self._run_cli(partial)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Partial pipeline-state schema detected", result.stderr)

    def test_zero_row_apply_is_atomic_and_second_apply_is_noop(self):
        db_path = self.temp_path / "zero.sqlite"
        conn = self.initialize_legacy(db_path)
        conn.close()
        first = self._run_cli(db_path, "--yes")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("Status: legacy", first.stdout)
        self.assertIn("Migrated rows: 0", first.stdout)
        second = self._run_cli(db_path, "--yes")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("Status: already_migrated", second.stdout)
        self.assertIn("Migrated rows: 0", second.stdout)

    def test_workspace_guard_requires_separate_authorization(self):
        workspace_db = ROOT / "data" / "wahojobs.sqlite"
        direct = self._run_cli(workspace_db, "--yes")
        self.assertNotEqual(direct.returncode, 0)
        self.assertIn("Refusing to access the workspace database", direct.stderr)
        alternate = self._run_cli(
            ROOT / "data" / ".." / "data" / "wahojobs.sqlite",
            "--yes",
        )
        self.assertNotEqual(alternate.returncode, 0)
        self.assertIn("Refusing to access the workspace database", alternate.stderr)

    def test_migration_rejects_caller_owned_transaction_without_committing_it(self):
        db_path = self.temp_path / "caller-transaction.sqlite"
        conn = self.initialize_legacy(db_path)
        conn.execute(
            """
            INSERT INTO user_pipeline_items (
              pipeline_item_id, user_id, profile_id, source, opportunity_title,
              status, created_at, updated_at
            )
            VALUES ('caller-item', 'user-a', 'profile-a', 'Fixture', 'Caller item',
                    'saved', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )
        self.assertTrue(conn.in_transaction)
        with self.assertRaisesRegex(migration.MigrationError, "migration-owned connection"):
            migration.apply_pipeline_state_migration(conn)
        self.assertTrue(conn.in_transaction)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM user_pipeline_items WHERE pipeline_item_id = 'caller-item'"
            ).fetchone()[0],
            1,
        )
        conn.rollback()
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM user_pipeline_items WHERE pipeline_item_id = 'caller-item'"
            ).fetchone()[0],
            0,
        )
        conn.close()

    def _object_names(self, db_path):
        conn = self.connect(db_path)
        try:
            return {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'index', 'trigger')"
                )
            }
        finally:
            conn.close()

    def _workflow(self, conn, item, status, version, key):
        return pipeline_state.change_workflow_status(
            conn,
            pipeline_item_id=item,
            owner_profile_id="profile-a",
            workflow_status=status,
            expected_version=version,
            idempotency_key=key,
        )

    def _correction(
        self,
        conn,
        item,
        original,
        status,
        version,
        key,
        visibility="visible",
        reminder_at=None,
    ):
        return pipeline_state.correct_state(
            conn,
            pipeline_item_id=item,
            owner_profile_id="profile-a",
            corrected_state={
                "workflow_status": status,
                "workflow_status_provenance": "known",
                "visibility": visibility,
                "reminder_at": reminder_at,
            },
            correction_of_transition_id=original.transition["transition_id"],
            expected_version=version,
            idempotency_key=key,
        )

    def _effective_ids(self, conn, item):
        return [
            row["transition_id"]
            for row in pipeline_state.list_effective_funnel_transitions(
                conn, item, "profile-a"
            )
        ]

    def _run_cli(self, path, *extra):
        return subprocess.run(
            [sys.executable, "-B", str(MIGRATION_SCRIPT), "--db", str(path), *extra],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )

    def _run_repository_initializer(self, path):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                (
                    "import sys; from pathlib import Path; "
                    "from wahojobs.db.repository import initialize_database; "
                    "initialize_database(Path(sys.argv[1]))"
                ),
                str(path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
