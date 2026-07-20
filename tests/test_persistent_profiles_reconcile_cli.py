import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scripts.persistent_profiles_reconcile as cli
from tests.persistent_profiles_reconciliation_test_support import (
    corrupt_one,
    installed_database,
    seed_profile,
    sidecars,
)
from tests.persistent_profiles_test_support import install_persistent_profiles


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "persistent_profiles_reconcile.py"


class PersistentProfilesReconcileCliTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "profiles.sqlite"
        connection = installed_database(self.path)
        connection.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_cli(self, *arguments, hash_seed=None):
        env = os.environ.copy()
        if hash_seed is not None:
            env["PYTHONHASHSEED"] = str(hash_seed)
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *map(str, arguments)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=30,
            env=env,
            check=False,
        )

    def test_clean_json_and_human_smokes_use_exact_exit_contract(self):
        result = self.run_cli("--db", self.path, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "clean")
        self.assertEqual(payload["total_findings"], 0)
        self.assertNotIn(str(self.path), result.stdout)

        human = self.run_cli("--db", self.path)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("Status: clean", human.stdout)
        self.assertNotIn(str(self.path), human.stdout)

    def test_findings_json_summary_and_truncation_exit_one(self):
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        seed_profile(connection)
        corrupt_one(
            connection,
            "UPDATE product_profile_revisions SET structured_profile_sha256='BAD', "
            "idempotency_key='x', request_fingerprint='y'",
        )
        connection.close()

        result = self.run_cli(
            "--db",
            self.path,
            "--json",
            "--max-findings",
            "1",
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "findings")
        self.assertGreater(payload["total_findings"], 1)
        self.assertEqual(len(payload["findings"]), 1)
        self.assertTrue(payload["findings_truncated"])

        summary = self.run_cli(
            "--db", self.path, "--json", "--summary-only"
        )
        self.assertEqual(summary.returncode, 1, summary.stderr)
        summary_payload = json.loads(summary.stdout)
        self.assertEqual(summary_payload["findings"], [])
        self.assertEqual(
            summary_payload["total_findings"], payload["total_findings"]
        )

    def test_json_is_byte_deterministic_across_hash_seeds(self):
        first = self.run_cli("--db", self.path, "--json", hash_seed=1)
        second = self.run_cli("--db", self.path, "--json", hash_seed=999)
        self.assertEqual(first.returncode, 0)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(first.stdout.encode(), second.stdout.encode())

    def test_invalid_paths_and_invocations_are_sanitized_and_do_not_create_files(self):
        nonexistent = Path(self.temp_dir.name) / "missing.sqlite"
        directory = Path(self.temp_dir.name) / "directory"
        directory.mkdir()
        malformed = Path(self.temp_dir.name) / "malformed.sqlite"
        malformed.write_bytes(b"not sqlite and contains private marker")
        cases = (
            ("--db", nonexistent, "--json"),
            ("--db", directory, "--json"),
            ("--db", malformed, "--json"),
            ("--db", self.path, "--json", "--max-findings", "-1"),
            ("--db", self.path, "--json", "--max-findings", "10001"),
            ("--db", self.path, "--json", "--max-findings", "not-an-int"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 2)
                payload = json.loads(result.stdout)
                reason = payload.get("reason_code", payload.get("unavailable_reason"))
                self.assertIn(
                    reason,
                    {
                        "invalid_reconciliation_request",
                        "internal_consistency_failure",
                    },
                )
                self.assertNotIn(str(arguments[1]), result.stdout)
                self.assertNotIn("private marker", result.stdout)
                self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(nonexistent.exists())

    def test_invalid_arguments_never_echo_private_values(self):
        marker = "PRIVATE-ARGUMENT-MARKER"
        json_cases = (
            ("--db", self.path, "--json", f"--unknown-{marker}"),
            ("--db", self.path, "--json", "--unknown", marker),
            ("--db", self.path, "--json", "--max-findings", marker),
            ("--db", self.path, "--json", marker),
            ("--db", f"file:{marker}?mode=rw", "--json"),
        )
        for arguments in json_cases:
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stderr, "")
                self.assertEqual(len(result.stdout.strip().splitlines()), 1)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["status"], "unavailable")
                self.assertEqual(
                    payload["reason_code"],
                    "invalid_reconciliation_request",
                )
                self.assertNotIn(marker, result.stdout)

        human = self.run_cli(
            "--db",
            self.path,
            f"--unknown-{marker}",
        )
        self.assertEqual(human.returncode, 2)
        self.assertEqual(human.stderr, "")
        self.assertNotIn(marker, human.stdout)
        self.assertNotIn("usage:", human.stdout.lower())

    def test_rendering_serialization_and_stdout_failures_are_sanitized(self):
        marker = "PRIVATE-OUTPUT-MARKER"

        class ExplodingReport:
            status = "clean"

            def to_json(self):
                raise RuntimeError(marker)

        stdout = io.StringIO()
        with mock.patch.object(
            cli,
            "reconcile_persistent_profiles",
            return_value=ExplodingReport(),
        ), contextlib.redirect_stdout(stdout):
            code = cli.main(
                ["--db", str(self.path), "--json"],
                _workspace_path=Path(self.temp_dir.name) / "other.sqlite",
            )
        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["reason_code"], "internal_consistency_failure")
        self.assertNotIn(marker, stdout.getvalue())

        stdout = io.StringIO()
        with mock.patch.object(
            cli,
            "_render_human",
            side_effect=RuntimeError(marker),
        ), contextlib.redirect_stdout(stdout):
            code = cli.main(
                ["--db", str(self.path)],
                _workspace_path=Path(self.temp_dir.name) / "other.sqlite",
            )
        self.assertEqual(code, 2)
        self.assertIn("reconciliation unavailable", stdout.getvalue())
        self.assertNotIn(marker, stdout.getvalue())

        class FailingStdout:
            def write(self, _value):
                raise KeyboardInterrupt(marker)

        with mock.patch.object(cli.sys, "stdout", FailingStdout()):
            code = cli.main(
                ["--db", str(self.path), "--json"],
                _workspace_path=Path(self.temp_dir.name) / "other.sqlite",
            )
        self.assertEqual(code, 2)

    def test_output_failure_uses_one_fixed_fallback_attempt_and_closes_connections(self):
        marker = "PRIVATE-RECOVERABLE-OUTPUT-FAILURE"

        class FailOnceWriter:
            def __init__(self, failure_type=RuntimeError):
                self.failure_type = failure_type
                self.write_calls = 0
                self.flush_calls = 0
                self.captured = []

            def write(self, value):
                self.write_calls += 1
                if self.write_calls == 1:
                    raise self.failure_type(marker)
                self.captured.append(value)
                return len(value)

            def flush(self):
                self.flush_calls += 1

        class AlwaysFailWriter:
            def __init__(self, failure_type=RuntimeError):
                self.failure_type = failure_type
                self.write_calls = 0

            def write(self, _value):
                self.write_calls += 1
                raise self.failure_type(marker)

            def flush(self):
                raise AssertionError("flush must not follow a failed write")

        def invoke(writer, *, json_mode):
            opened = []

            def tracked_connect(*args, **kwargs):
                connection = sqlite3.connect(*args, **kwargs)
                opened.append(connection)
                return connection

            stderr = io.StringIO()
            arguments = ["--db", str(self.path)]
            if json_mode:
                arguments.append("--json")
            with mock.patch.object(cli.sys, "stdout", writer), contextlib.redirect_stderr(
                stderr
            ):
                code = cli.main(
                    arguments,
                    _workspace_path=Path(self.temp_dir.name) / "other.sqlite",
                    _connect=tracked_connect,
                )
            self.assertTrue(opened)
            for connection in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute("SELECT 1")
            return code, stderr.getvalue()

        expected_json = (
            '{"error_code":"internal_consistency_failure",'
            '"report_version":"persistent_profile_reconciliation_v1",'
            '"status":"unavailable"}\n'
        )
        for failure_type in (RuntimeError, KeyboardInterrupt):
            with self.subTest(mode="json", failure=failure_type.__name__):
                writer = FailOnceWriter(failure_type)
                code, stderr = invoke(writer, json_mode=True)
                self.assertEqual(code, 2)
                self.assertEqual(writer.write_calls, 2)
                self.assertEqual(writer.flush_calls, 1)
                self.assertEqual("".join(writer.captured), expected_json)
                self.assertEqual(
                    json.loads("".join(writer.captured)),
                    {
                        "error_code": "internal_consistency_failure",
                        "report_version": "persistent_profile_reconciliation_v1",
                        "status": "unavailable",
                    },
                )
                self.assertEqual(stderr, "")
                self.assertNotIn(marker, "".join(writer.captured))

        writer = FailOnceWriter()
        code, stderr = invoke(writer, json_mode=False)
        self.assertEqual(code, 2)
        self.assertEqual(writer.write_calls, 2)
        self.assertEqual(writer.flush_calls, 1)
        self.assertEqual(
            "".join(writer.captured),
            "Persistent-profile reconciliation unavailable.\n",
        )
        self.assertEqual(stderr, "")
        self.assertNotIn(marker, "".join(writer.captured))

        for json_mode in (True, False):
            with self.subTest(mode="permanent", json=json_mode):
                writer = AlwaysFailWriter()
                code, stderr = invoke(writer, json_mode=json_mode)
                self.assertEqual(code, 2)
                self.assertEqual(writer.write_calls, 2)
                self.assertEqual(stderr, "")

    def test_literal_filesystem_paths_are_encoded_without_uri_injection(self):
        names = (
            "literal%23db.sqlite",
            "literal#db.sqlite",
            "literal%db.sqlite",
            "literal space.sqlite",
            "literal-unicode-ç.sqlite",
            "literal&name=.sqlite",
        )
        paths = []
        for name in names:
            path = Path(self.temp_dir.name) / name
            connection = installed_database(path)
            connection.close()
            paths.append(path)

        decoy = Path(self.temp_dir.name) / "literal#db.sqlite"
        decoy.write_bytes(b"not a SQLite database")
        exact_percent = self.run_cli("--db", paths[0], "--json")
        self.assertEqual(exact_percent.returncode, 0, exact_percent.stderr)
        decoy.unlink()
        connection = installed_database(decoy)
        connection.close()
        for path in paths:
            with self.subTest(path=path.name):
                result = self.run_cli("--db", path, "--json")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["status"], "clean")
                self.assertEqual(sidecars(path), [])
        self.assertNotEqual(paths[0].read_bytes(), b"")
        self.assertNotEqual(decoy.read_bytes(), b"")

        relative = os.path.relpath(paths[0], ROOT)
        result = self.run_cli("--db", relative, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)

        invalid_names = (
            "literal?name.sqlite",
            "file:test.sqlite?mode=rw",
        )
        for name in invalid_names:
            candidate = Path(self.temp_dir.name) / name
            result = self.run_cli("--db", candidate, "--json")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr, "")
            self.assertFalse(candidate.exists())

    def test_path_identity_change_during_open_or_scan_is_unavailable(self):
        marker_path = self.path

        def changed_during_open(*args, **kwargs):
            connection = sqlite3.connect(*args, **kwargs)
            stat_result = marker_path.stat()
            os.utime(
                marker_path,
                ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1_000_000),
            )
            return connection

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(
                ["--db", str(self.path), "--json"],
                _workspace_path=Path(self.temp_dir.name) / "other.sqlite",
                _connect=changed_during_open,
            )
        self.assertEqual(code, 2)
        self.assertEqual(
            json.loads(stdout.getvalue())["reason_code"],
            "temporary_contention",
        )

        self.path = Path(self.temp_dir.name) / "scan-change.sqlite"
        connection = installed_database(self.path)
        connection.close()
        real_reconcile = cli.reconcile_persistent_profiles

        def changed_during_scan(connection, **kwargs):
            report = real_reconcile(connection, **kwargs)
            stat_result = self.path.stat()
            os.utime(
                self.path,
                ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1_000_000),
            )
            return report

        stdout = io.StringIO()
        with mock.patch.object(
            cli,
            "reconcile_persistent_profiles",
            side_effect=changed_during_scan,
        ), contextlib.redirect_stdout(stdout):
            code = cli.main(
                ["--db", str(self.path), "--json"],
                _workspace_path=Path(self.temp_dir.name) / "other.sqlite",
            )
        self.assertEqual(code, 2)
        self.assertEqual(
            json.loads(stdout.getvalue())["reason_code"],
            "temporary_contention",
        )

    def test_checkpointed_wal_mode_is_sidecar_free(self):
        connection = sqlite3.connect(self.path)
        self.assertEqual(
            connection.execute("PRAGMA journal_mode=WAL").fetchone()[0],
            "wal",
        )
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        connection.close()
        self.assertEqual(sidecars(self.path), [])
        before = self.path.read_bytes()
        result = self.run_cli("--db", self.path, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sidecars(self.path), [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_existing_wal_and_rollback_journal_are_rejected_unchanged(self):
        writer = sqlite3.connect(self.path)
        writer.execute("PRAGMA journal_mode=WAL").fetchone()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "DELETE FROM wahojobs_schema_migrations "
            "WHERE version='005_persistent_profile_canonical_v2'"
        )
        try:
            existing = sidecars(self.path)
            self.assertTrue(existing)
            before = {
                path.name: (path.stat().st_size, path.stat().st_mtime_ns)
                for path in existing
            }
            result = self.run_cli("--db", self.path, "--json")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(
                json.loads(result.stdout)["reason_code"],
                "temporary_contention",
            )
            self.assertEqual(
                {
                    path.name: (path.stat().st_size, path.stat().st_mtime_ns)
                    for path in existing
                },
                before,
            )
        finally:
            writer.rollback()
            writer.close()

        self.path = Path(self.temp_dir.name) / "synthetic-wal.sqlite"
        connection = installed_database(self.path)
        connection.close()
        synthetic_sidecars = {
            Path(str(self.path) + "-wal"): b"synthetic wal bytes",
            Path(str(self.path) + "-shm"): b"synthetic shm bytes",
        }
        for path, content in synthetic_sidecars.items():
            path.write_bytes(content)
        result = self.run_cli("--db", self.path, "--json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            json.loads(result.stdout)["reason_code"],
            "temporary_contention",
        )
        for path, content in synthetic_sidecars.items():
            self.assertEqual(path.read_bytes(), content)

        self.path = Path(self.temp_dir.name) / "rollback.sqlite"
        connection = installed_database(self.path)
        connection.close()
        journal = Path(str(self.path) + "-journal")
        journal.write_bytes(b"synthetic rollback journal")
        before_journal = journal.read_bytes()
        result = self.run_cli("--db", self.path, "--json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(
            json.loads(result.stdout)["reason_code"],
            "temporary_contention",
        )
        self.assertEqual(journal.read_bytes(), before_journal)

    def test_m004_only_database_is_unavailable(self):
        path = Path(self.temp_dir.name) / "m004.sqlite"
        connection = install_persistent_profiles(path)
        connection.close()
        result = self.run_cli("--db", path, "--json")
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(
            payload["unavailable_reason"], "schema_capability_unavailable"
        )

    def test_workspace_guard_blocks_aliases_and_explicit_flag_allows_read_only_scan(self):
        alias = Path(self.temp_dir.name) / "workspace-alias.sqlite"
        os.link(self.path, alias)
        for candidate in (self.path, alias):
            with self.subTest(candidate=candidate):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = cli.main(
                        ["--db", str(candidate), "--json"],
                        _workspace_path=self.path,
                    )
                self.assertEqual(code, 2)
                self.assertEqual(
                    json.loads(stdout.getvalue())["reason_code"],
                    "invalid_reconciliation_request",
                )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(
                [
                    "--db",
                    str(alias),
                    "--allow-workspace-db",
                    "--json",
                ],
                _workspace_path=self.path,
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "clean")
        self.assertEqual(sidecars(self.path), [])

    def test_owned_connection_closes_on_success_failure_and_interrupt(self):
        opened = []

        def tracked_connect(*args, **kwargs):
            connection = sqlite3.connect(*args, **kwargs)
            opened.append(connection)
            return connection

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(
                ["--db", str(self.path), "--json"],
                _workspace_path=Path(self.temp_dir.name) / "other.sqlite",
                _connect=tracked_connect,
            )
        self.assertEqual(code, 0)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[-1].execute("SELECT 1")

        def interrupted_connect(*_args, **_kwargs):
            raise KeyboardInterrupt

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(
                ["--db", str(self.path), "--json"],
                _workspace_path=Path(self.temp_dir.name) / "other.sqlite",
                _connect=interrupted_connect,
            )
        self.assertEqual(code, 2)
        self.assertEqual(
            json.loads(stdout.getvalue())["reason_code"],
            "internal_consistency_failure",
        )

    def test_locked_database_is_sanitized_and_leaves_no_sidecars(self):
        writer = sqlite3.connect(self.path, timeout=0.1)
        writer.execute("BEGIN EXCLUSIVE")
        try:
            result = self.run_cli("--db", self.path, "--json")
        finally:
            writer.rollback()
            writer.close()
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload.get("reason_code", payload.get("unavailable_reason")),
            "temporary_contention",
        )
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(sidecars(self.path), [])

    def test_importing_cli_does_not_execute_main(self):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import scripts.persistent_profiles_reconcile; print('imported')",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "imported")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
