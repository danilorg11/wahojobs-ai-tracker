import queue
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from tests.persistent_profiles_repository_test_support import (
    NOW,
    append_command,
    connect_repository_database,
    create_command,
    development_context,
    install_repository_database,
    profile_counts,
    purge_command,
    reference,
)
from wahojobs.persistent_profiles import PersistentProfileDomainError
from wahojobs.persistent_profiles_repository import (
    append_profile_revision,
    create_persistent_profile,
    purge_persistent_profile,
    read_current_profile,
)


class PersistentProfilesRepositoryConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "concurrency.sqlite"
        connection = install_repository_database(self.path)
        self.principal = development_context(connection, "81")
        connection.close()

    def tearDown(self):
        self.temp.cleanup()

    def run_competing(self, operations, *, timeout=3.0):
        barrier = threading.Barrier(len(operations))
        output = queue.Queue()

        def worker(operation):
            connection = connect_repository_database(self.path, timeout=timeout)
            try:
                barrier.wait(timeout=5)
                result = operation(connection)
                output.put(("ok", result))
            except PersistentProfileDomainError as exc:
                output.put(("error", exc.reason_code))
            except BaseException as exc:
                output.put(("raw", type(exc).__name__))
            finally:
                connection.close()

        threads = [threading.Thread(target=worker, args=(operation,)) for operation in operations]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        return [output.get_nowait() for _ in operations]

    def create_profile(self):
        connection = connect_repository_database(self.path)
        try:
            command = create_command(self.principal)
            created = create_persistent_profile(connection, command)
            current = read_current_profile(
                connection,
                self.principal,
                profile_id=created.profile_id,
                include_structured_profile=True,
            )
            document = current.trusted_dict(include_structured_profile=True)[
                "structured_profile"
            ]
            return created, reference(created, self.principal), document
        finally:
            connection.close()

    def assert_no_raw(self, outcomes):
        self.assertFalse([outcome for outcome in outcomes if outcome[0] == "raw"])

    def test_simultaneous_exact_creation_replay(self):
        first = create_command(self.principal)
        second = create_command(self.principal)
        self.assertNotEqual(first.profile_id, second.profile_id)
        self.assertEqual(first.request_fingerprint, second.request_fingerprint)
        outcomes = self.run_competing(
            [
                lambda conn: create_persistent_profile(conn, first),
                lambda conn: create_persistent_profile(conn, second),
            ]
        )
        self.assert_no_raw(outcomes)
        self.assertEqual([kind for kind, _ in outcomes].count("ok"), 2)
        self.assertEqual(sorted(result.replayed for _, result in outcomes), [False, True])
        connection = connect_repository_database(self.path)
        self.assertEqual(profile_counts(connection), (1, 1, 1, 1))
        connection.close()

    def test_simultaneous_changed_creation_replay(self):
        first = create_command(self.principal)
        second = create_command(self.principal, reason_code="profile.import")
        outcomes = self.run_competing(
            [
                lambda conn: create_persistent_profile(conn, first),
                lambda conn: create_persistent_profile(conn, second),
            ]
        )
        self.assert_no_raw(outcomes)
        self.assertEqual(sorted(kind for kind, _ in outcomes), ["error", "ok"])
        self.assertIn(("error", "idempotency_conflict"), outcomes)

    def test_simultaneous_different_key_creation(self):
        first = create_command(self.principal, idempotency_key="profile-create-1001")
        second = create_command(self.principal, idempotency_key="profile-create-1002")
        outcomes = self.run_competing(
            [
                lambda conn: create_persistent_profile(conn, first),
                lambda conn: create_persistent_profile(conn, second),
            ]
        )
        self.assert_no_raw(outcomes)
        self.assertEqual(sorted(kind for kind, _ in outcomes), ["error", "ok"])
        self.assertIn(("error", "profile_already_exists"), outcomes)

    def test_simultaneous_exact_append_replay(self):
        _, profile_reference, document = self.create_profile()
        first = append_command(self.principal, profile_reference, document)
        second = append_command(self.principal, profile_reference, document)
        self.assertEqual(first.request_fingerprint, second.request_fingerprint)
        outcomes = self.run_competing(
            [
                lambda conn: append_profile_revision(conn, first),
                lambda conn: append_profile_revision(conn, second),
            ]
        )
        self.assert_no_raw(outcomes)
        self.assertEqual([kind for kind, _ in outcomes].count("ok"), 2)
        self.assertEqual(sorted(result.replayed for _, result in outcomes), [False, True])
        connection = connect_repository_database(self.path)
        self.assertEqual(profile_counts(connection), (1, 2, 2, 1))
        connection.close()

    def test_simultaneous_changed_append_replay(self):
        _, profile_reference, document = self.create_profile()
        first = append_command(self.principal, profile_reference, document)
        second = append_command(
            self.principal,
            profile_reference,
            document,
            accepted_at=NOW + timedelta(seconds=2),
        )
        outcomes = self.run_competing(
            [
                lambda conn: append_profile_revision(conn, first),
                lambda conn: append_profile_revision(conn, second),
            ]
        )
        self.assert_no_raw(outcomes)
        self.assertEqual(sorted(kind for kind, _ in outcomes), ["error", "ok"])
        self.assertIn(("error", "idempotency_conflict"), outcomes)

    def test_two_different_appends_have_one_stale_loser(self):
        _, profile_reference, document = self.create_profile()
        edit = append_command(
            self.principal,
            profile_reference,
            document,
            idempotency_key="profile-edit-2001",
        )
        archive = append_command(
            self.principal,
            profile_reference,
            document,
            revision_kind="archive",
            idempotency_key="profile-archive-2001",
        )
        outcomes = self.run_competing(
            [
                lambda conn: append_profile_revision(conn, edit),
                lambda conn: append_profile_revision(conn, archive),
            ]
        )
        self.assert_no_raw(outcomes)
        self.assertEqual(sorted(kind for kind, _ in outcomes), ["error", "ok"])
        self.assertIn(("error", "stale_revision"), outcomes)

    def test_append_versus_deletion_request_has_one_stale_loser(self):
        _, profile_reference, document = self.create_profile()
        edit = append_command(
            self.principal,
            profile_reference,
            document,
            idempotency_key="profile-edit-3001",
        )
        deletion = append_command(
            self.principal,
            profile_reference,
            document,
            revision_kind="deletion_request",
            idempotency_key="profile-delete-3001",
        )
        outcomes = self.run_competing(
            [
                lambda conn: append_profile_revision(conn, edit),
                lambda conn: append_profile_revision(conn, deletion),
            ]
        )
        self.assert_no_raw(outcomes)
        self.assertEqual(sorted(kind for kind, _ in outcomes), ["error", "ok"])
        self.assertIn(("error", "stale_revision"), outcomes)

    def test_read_versus_append_returns_complete_states(self):
        created, profile_reference, document = self.create_profile()
        append = append_command(self.principal, profile_reference, document)
        outcomes = self.run_competing(
            [
                lambda conn: read_current_profile(
                    conn, self.principal, profile_id=created.profile_id
                ),
                lambda conn: append_profile_revision(conn, append),
            ]
        )
        self.assert_no_raw(outcomes)
        self.assertEqual([kind for kind, _ in outcomes].count("ok"), 2)
        read = next(result for kind, result in outcomes if hasattr(result, "updated_at"))
        self.assertIn(read.revision_number, {1, 2})

    def test_read_and_append_versus_purge_never_return_partial_state(self):
        created, profile_reference, document = self.create_profile()
        connection = connect_repository_database(self.path)
        deletion = append_command(
            self.principal,
            profile_reference,
            document,
            revision_kind="deletion_request",
            idempotency_key="profile-delete-4001",
        )
        append_profile_revision(connection, deletion)
        connection.close()
        impossible_append = append_command(
            self.principal,
            profile_reference,
            document,
            expected_revision=2,
            idempotency_key="profile-edit-4001",
        )
        purge = purge_command(profile_reference)
        outcomes = self.run_competing(
            [
                lambda conn: read_current_profile(
                    conn, self.principal, profile_id=created.profile_id
                ),
                lambda conn: append_profile_revision(conn, impossible_append),
                lambda conn: purge_persistent_profile(conn, purge),
            ]
        )
        self.assert_no_raw(outcomes)
        self.assertTrue(
            all(
                kind == "ok" or result in {"lifecycle_conflict", "profile_not_found"}
                for kind, result in outcomes
            )
        )
        self.assertTrue(
            any(
                kind == "ok" and getattr(result, "outcome", None) == "absent_or_completed"
                for kind, result in outcomes
            )
        )
        completed_reads = [
            result for kind, result in outcomes if kind == "ok" and hasattr(result, "updated_at")
        ]
        if completed_reads:
            self.assertEqual(completed_reads[0].lifecycle_status, "deletion_requested")
        else:
            self.assertIn(("error", "profile_not_found"), outcomes)

    def test_lock_contention_is_sanitized(self):
        locker = connect_repository_database(self.path)
        locker.execute("BEGIN IMMEDIATE")
        connection = connect_repository_database(self.path, timeout=0.05)
        try:
            with self.assertRaises(PersistentProfileDomainError) as raised:
                create_persistent_profile(connection, create_command(self.principal))
            self.assertEqual(raised.exception.reason_code, "temporary_contention")
            self.assertIsNone(raised.exception.__cause__)
            self.assertIsNone(raised.exception.__context__)
        finally:
            connection.close()
            locker.rollback()
            locker.close()


if __name__ == "__main__":
    unittest.main()
