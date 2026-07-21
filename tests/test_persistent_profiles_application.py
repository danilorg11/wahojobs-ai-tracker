import copy
import hashlib
import pickle
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from tests.persistent_profiles_repository_test_support import (
    account_context,
    append_command,
    canonical_fixture,
    create_command,
    install_repository_database,
    profile_counts,
    purge_command,
    reference,
)
from wahojobs.persistent_profiles_application import (
    BrowserRequestContext,
    PersistentProfileApplicationService,
    PersistentProfilePageResult,
    _LEGACY_PROFILE_READ_GRANT_ISSUER,
    _TRUSTED_AUTHENTICATION_ACTOR_ISSUER,
)
from wahojobs.persistent_profiles_repository import (
    append_profile_revision,
    create_persistent_profile,
    purge_persistent_profile,
)


def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TrackingReadOnlyProvider:
    def __init__(self, path, *, timeout=0.2):
        self.path = Path(path)
        self.timeout = timeout
        self.opened = 0
        self.closed = 0

    def __call__(self):
        @contextmanager
        def owned_connection():
            connection = sqlite3.connect(
                f"file:{self.path.as_posix()}?mode=ro",
                uri=True,
                timeout=self.timeout,
            )
            self.opened += 1
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA query_only = ON")
                yield connection
            finally:
                connection.close()
                self.closed += 1

        return owned_connection()


class PersistentProfileApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "application.sqlite"
        self.writer = install_repository_database(self.path)
        self.principal = account_context(self.writer)
        self.writer.commit()
        self.actor = _TRUSTED_AUTHENTICATION_ACTOR_ISSUER.issue(
            "test-authenticated-actor"
        )
        self.grant = _LEGACY_PROFILE_READ_GRANT_ISSUER.issue(self.principal)
        self.provider = TrackingReadOnlyProvider(self.path)

    def tearDown(self):
        self.writer.close()
        self.temp.cleanup()

    def service(self, *, authenticate=None, authorize=None, provider=None):
        return PersistentProfileApplicationService(
            authenticate=authenticate or (lambda _request: self.actor),
            authorize=authorize or (lambda _actor: self.grant),
            connection_provider=provider or self.provider,
        )

    def read(self, service=None, *, before=None):
        return (service or self.service()).read_my_profile(
            BrowserRequestContext("GET", "/account/profile"),
            before_revision_number=before,
        )

    def create_profile(self):
        created = create_persistent_profile(self.writer, create_command(self.principal))
        self.writer.commit()
        return created

    def append(self, created, *, revision, kind="edit"):
        profile = canonical_fixture(created.profile_id)
        result = append_profile_revision(
            self.writer,
            append_command(
                self.principal,
                reference(created, self.principal),
                profile,
                expected_revision=revision,
                revision_kind=kind,
                idempotency_key=f"profile-{kind}-{revision:04d}",
            ),
        )
        self.writer.commit()
        return result

    def test_authentication_and_authorization_are_separate_and_precede_database_open(self):
        unauthenticated = self.service(authenticate=lambda _request: None)
        self.assertEqual(self.read(unauthenticated).state, "authentication_required")
        self.assertEqual(self.provider.opened, 0)

        unauthorized = self.service(authorize=lambda _actor: None)
        self.assertEqual(self.read(unauthorized).state, "authorization_denied")
        self.assertEqual(self.provider.opened, 0)

        self.assertEqual(
            self.read(self.service(authenticate=lambda _request: object())).state,
            "unavailable",
        )
        self.assertEqual(
            self.read(self.service(authorize=lambda _actor: object())).state,
            "unavailable",
        )
        self.assertEqual(self.provider.opened, 0)

    def test_gateway_failures_are_sanitized_and_open_no_database(self):
        marker = "sensitive-authentication-marker"

        def fail(_value):
            raise RuntimeError(marker)

        for service in (
            self.service(authenticate=fail),
            self.service(authorize=fail),
        ):
            result = self.read(service)
            self.assertEqual(result.state, "unavailable")
            self.assertNotIn(marker, repr(result))
        self.assertEqual(self.provider.opened, 0)

    def test_authorized_absent_profile_is_stable_and_read_only(self):
        before = (file_sha256(self.path), self.path.stat().st_size, profile_counts(self.writer))
        result = self.read()
        after = (file_sha256(self.path), self.path.stat().st_size, profile_counts(self.writer))
        self.assertEqual(result, PersistentProfilePageResult("empty"))
        self.assertEqual(before, after)
        self.assertEqual((self.provider.opened, self.provider.closed), (1, 1))

    def test_active_profile_uses_browser_safe_view_and_metadata_only_history(self):
        created = self.create_profile()
        result = self.read()
        self.assertEqual(result.state, "active")
        self.assertTrue(result.profile.structured_content_visible)
        self.assertEqual(result.profile.revision_number, 1)
        self.assertTrue(result.profile.field_groups)
        self.assertEqual(len(result.history), 1)
        self.assertEqual(result.history[0].revision_kind, "initial")
        rendered = repr(result)
        self.assertNotIn(created.profile_id, rendered)
        self.assertNotIn(created.revision_id, rendered)
        self.assertNotIn(self.principal.principal_id, rendered)
        self.assertFalse(hasattr(result, "connection"))
        self.assertFalse(hasattr(result.profile, "structured_profile"))

    def test_archived_and_deletion_requested_lifecycle_policies(self):
        created = self.create_profile()
        self.append(created, revision=1, kind="archive")
        archived = self.read()
        self.assertEqual(archived.state, "archived")
        self.assertTrue(archived.profile.structured_content_visible)
        self.assertTrue(archived.profile.field_groups)

        self.append(created, revision=2, kind="deletion_request")
        deleting = self.read()
        self.assertEqual(deleting.state, "deletion_requested")
        self.assertFalse(deleting.profile.structured_content_visible)
        self.assertEqual(deleting.profile.display_name, "")
        self.assertEqual(deleting.profile.field_groups, ())
        self.assertEqual(deleting.profile.revision_number, 3)
        self.assertEqual(len(deleting.history), 3)

    def test_history_is_bounded_newest_first_and_cursor_paginated(self):
        created = self.create_profile()
        for revision in range(1, 22):
            self.append(created, revision=revision)
        first = self.read()
        self.assertEqual(len(first.history), 20)
        self.assertEqual(first.history[0].revision_number, 22)
        self.assertEqual(first.history[-1].revision_number, 3)
        self.assertEqual(first.next_cursor, 3)

        second = self.read(before=first.next_cursor)
        self.assertEqual(
            [item.revision_number for item in second.history],
            [2, 1],
        )
        self.assertIsNone(second.next_cursor)
        self.assertTrue(all(not hasattr(item, "structured_profile") for item in second.history))

    def test_invalid_request_context_and_cursor_never_reach_gateways(self):
        calls = []
        service = self.service(authenticate=lambda request: calls.append(request))
        self.assertEqual(service.read_my_profile(object()).state, "unavailable")
        self.assertEqual(
            service.read_my_profile(
                BrowserRequestContext("GET", "/account/profile"),
                before_revision_number=True,
            ).state,
            "unavailable",
        )
        self.assertEqual(calls, [])
        for method, route in (("POST", "/account/profile"), ("GET", "/account/other")):
            with self.assertRaises(ValueError):
                BrowserRequestContext(method, route)

    def test_provider_capability_and_failures_are_generic(self):
        @contextmanager
        def not_query_only():
            connection = sqlite3.connect(self.path)
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                yield connection
            finally:
                connection.close()

        self.assertEqual(self.read(self.service(provider=not_query_only)).state, "schema_unavailable")

        def provider_failure():
            raise RuntimeError("private-database-path")

        result = self.read(self.service(provider=provider_failure))
        self.assertEqual(result.state, "unavailable")
        self.assertNotIn("private-database-path", repr(result))

    def test_schema_and_repository_failures_are_bounded(self):
        other = Path(self.temp.name) / "unmigrated.sqlite"
        sqlite3.connect(other).close()
        result = self.read(self.service(provider=TrackingReadOnlyProvider(other)))
        self.assertEqual(result.state, "schema_unavailable")

        self.create_profile()
        with mock.patch(
            "wahojobs.persistent_profiles_application.read_current_profile",
            side_effect=RuntimeError("structured-secret"),
        ):
            result = self.read()
        self.assertEqual(result.state, "unavailable")
        self.assertNotIn("structured-secret", repr(result))

        with mock.patch(
            "wahojobs.persistent_profiles_application._build_profile_view",
            side_effect=RuntimeError("view-model-secret"),
        ):
            result = self.read()
        self.assertEqual(result.state, "unavailable")
        self.assertNotIn("view-model-secret", repr(result))

    def test_owned_connection_closes_even_when_history_or_provider_exit_fails(self):
        self.create_profile()
        with mock.patch(
            "wahojobs.persistent_profiles_application.read_profile_history",
            side_effect=RuntimeError("history-secret"),
        ):
            result = self.read()
        self.assertEqual(result.state, "unavailable")
        self.assertEqual(self.provider.opened, self.provider.closed)

        @contextmanager
        def bad_exit():
            connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA query_only = ON")
            try:
                yield connection
            finally:
                connection.close()
                raise RuntimeError("close-secret")

        self.assertEqual(self.read(self.service(provider=bad_exit)).state, "unavailable")

    def test_trusted_contexts_are_immutable_opaque_and_nonserializable(self):
        for trusted, marker in (
            (self.actor, "test-authenticated-actor"),
            (self.grant, self.principal.principal_id),
        ):
            self.assertNotIn(marker, repr(trusted))
            self.assertNotIn(marker, str(trusted))
            with self.assertRaises(TypeError):
                pickle.dumps(trusted)
            with self.assertRaises(TypeError):
                copy.copy(trusted)
            with self.assertRaises(TypeError):
                copy.deepcopy(trusted)
            with self.assertRaises(TypeError):
                vars(trusted)
            with self.assertRaises(AttributeError):
                trusted._sealed = False

    def test_service_exposes_no_mutation_api_or_request_selected_identity(self):
        service = self.service()
        for name in (
            "create",
            "append",
            "edit",
            "archive",
            "reactivate",
            "delete",
            "purge",
            "reconcile",
        ):
            self.assertFalse(hasattr(service, name))
        self.assertEqual(
            set(BrowserRequestContext.__slots__),
            {"method", "route", "_authentication_input", "_sealed"},
        )
        self.assertFalse(any("principal" in name or "profile_id" in name for name in BrowserRequestContext.__slots__))

    def test_request_authentication_input_is_opaque_short_lived_and_not_in_results(self):
        marker = "trusted-test-session-marker"
        input_object = {"Authorization": marker}
        observed = []

        def authenticate(request):
            observed.append(request.authentication_input_for_gateway())
            self.assertNotIn(marker, repr(request))
            with self.assertRaises(TypeError):
                pickle.dumps(request)
            return self.actor

        result = self.service(authenticate=authenticate).read_my_profile(
            BrowserRequestContext(
                "GET",
                "/account/profile",
                input_object,
            )
        )
        self.assertEqual(result.state, "empty")
        self.assertEqual(observed, [input_object])
        self.assertNotIn(marker, repr(result))

    def test_simultaneous_reads_use_separate_complete_snapshots(self):
        self.create_profile()
        barrier = threading.Barrier(8)
        states = []
        failures = []

        def worker():
            try:
                barrier.wait(timeout=3)
                result = self.read()
                states.append((result.state, result.profile.revision_number))
            except Exception as exc:
                failures.append(type(exc).__name__)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(failures, [])
        self.assertEqual(states, [("active", 1)] * 8)
        self.assertEqual(self.provider.opened, self.provider.closed)

    def test_read_and_history_remain_one_snapshot_while_append_commits(self):
        created = self.create_profile()
        self.writer.execute("PRAGMA journal_mode = WAL")
        first_reads_complete = threading.Event()
        writer_complete = threading.Event()
        original = __import__(
            "wahojobs.persistent_profiles_application",
            fromlist=["read_profile_history"],
        ).read_profile_history

        def paused_history(*args, **kwargs):
            first_reads_complete.set()
            self.assertTrue(writer_complete.wait(timeout=3))
            return original(*args, **kwargs)

        observed = []

        def reader():
            observed.append(self.read())

        with mock.patch(
            "wahojobs.persistent_profiles_application.read_profile_history",
            side_effect=paused_history,
        ):
            thread = threading.Thread(target=reader)
            thread.start()
            self.assertTrue(first_reads_complete.wait(timeout=3))
            self.append(created, revision=1, kind="edit")
            writer_complete.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].profile.revision_number, 1)
        self.assertEqual(
            [item.revision_number for item in observed[0].history],
            [1],
        )
        self.assertEqual(self.read().profile.revision_number, 2)

    def test_reads_during_deletion_and_purge_are_complete_old_or_new_states(self):
        created = self.create_profile()
        self.writer.execute("PRAGMA journal_mode = WAL")

        def concurrent_read_and_write(write):
            first_read = threading.Event()
            write_complete = threading.Event()
            application = __import__(
                "wahojobs.persistent_profiles_application",
                fromlist=["read_current_profile"],
            )
            original = application.read_current_profile
            calls = 0

            def paused_current(*args, **kwargs):
                nonlocal calls
                value = original(*args, **kwargs)
                calls += 1
                if calls == 1:
                    first_read.set()
                    self.assertTrue(write_complete.wait(timeout=3))
                return value

            observed = []
            with mock.patch(
                "wahojobs.persistent_profiles_application.read_current_profile",
                side_effect=paused_current,
            ):
                thread = threading.Thread(target=lambda: observed.append(self.read()))
                thread.start()
                self.assertTrue(first_read.wait(timeout=3))
                write()
                write_complete.set()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(observed), 1)
            return observed[0]

        deleting = concurrent_read_and_write(
            lambda: self.append(created, revision=1, kind="deletion_request")
        )
        self.assertEqual(deleting.state, "active")
        self.assertEqual(deleting.profile.revision_number, 1)
        self.assertEqual(self.read().state, "deletion_requested")

        def purge():
            purge_persistent_profile(
                self.writer,
                purge_command(reference(created, self.principal)),
            )
            self.writer.commit()

        before_purge = concurrent_read_and_write(purge)
        self.assertEqual(before_purge.state, "deletion_requested")
        self.assertEqual(before_purge.profile.revision_number, 2)
        self.assertEqual(self.read().state, "empty")

    def test_lock_contention_is_generic_and_connection_recovers(self):
        self.create_profile()
        self.writer.execute("PRAGMA journal_mode = DELETE")
        self.writer.execute("BEGIN EXCLUSIVE")
        try:
            result = self.read(
                self.service(provider=TrackingReadOnlyProvider(self.path, timeout=0.01))
            )
        finally:
            self.writer.rollback()
        self.assertEqual(result.state, "temporary_contention")
        self.assertEqual(self.read().state, "active")


if __name__ == "__main__":
    unittest.main()
