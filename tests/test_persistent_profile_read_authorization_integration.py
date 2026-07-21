import http.client
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from scripts.local_product_app import make_handler
from tests.ownership_test_support import (
    add_activation_event,
    add_active_user,
    add_binding,
    add_principal,
)
from tests.persistent_profile_read_authorization_test_support import (
    ReadOnlyAuthorizationProvider,
    file_fingerprint,
    install_authorization_database,
    seed_authorized_account,
    set_principal_status,
    suspend_account,
    transition_binding,
    trusted_actor,
)
from tests.persistent_profiles_repository_test_support import (
    append_command,
    canonical_fixture,
    create_command,
    profile_counts,
    reference,
)
from wahojobs.persistent_profile_read_authorization import (
    DurablePersistentProfileReadAuthorizationGateway,
)
from wahojobs.persistent_profiles_application import (
    PersistentProfileApplicationService,
    _TRUSTED_AUTHENTICATION_ACTOR_ISSUER,
)
from wahojobs.persistent_profiles_browser import PersistentProfileBrowserIntegration
from wahojobs.persistent_profiles_repository import (
    append_profile_revision,
    create_persistent_profile,
)


class PersistentProfileReadAuthorizationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "authorization-browser.sqlite"
        self.writer = install_authorization_database(self.path)
        self.state = seed_authorized_account(self.writer)
        self.writer.commit()
        self.actor = trusted_actor(self.state)
        self.gateway = DurablePersistentProfileReadAuthorizationGateway()
        self.provider = ReadOnlyAuthorizationProvider(self.path)

    def tearDown(self):
        self.writer.close()
        self.temp.cleanup()

    def integration(self, *, actor=None, provider=None):
        service = PersistentProfileApplicationService(
            authenticate=lambda _request: actor or self.actor,
            durable_authorization_gateway=self.gateway,
            connection_provider=provider or self.provider,
        )
        return PersistentProfileBrowserIntegration(service)

    def request(self, method, target, *, integration=None, headers=None):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(persistent_profile_browser_integration=integration),
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            connection.request(method, target, headers=headers or {})
            response = connection.getresponse()
            body = response.read()
            result = response.status, dict(response.getheaders()), body
            connection.close()
            return result
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def create_profile(self):
        created = create_persistent_profile(
            self.writer,
            create_command(self.state["principal"]),
        )
        self.writer.commit()
        return created

    def append(self, created, revision, kind):
        result = append_profile_revision(
            self.writer,
            append_command(
                self.state["principal"],
                reference(created, self.state["principal"]),
                canonical_fixture(created.profile_id),
                expected_revision=revision,
                revision_kind=kind,
                idempotency_key=f"authorization-browser-{kind}-{revision:04d}",
            ),
        )
        self.writer.commit()
        return result

    def test_route_remains_absent_without_explicit_composition(self):
        status, _headers, _body = self.request("GET", "/account/profile")
        self.assertEqual(status, 404)
        self.assertEqual(self.provider.opened, 0)

    def test_durable_and_legacy_authorization_cannot_be_composed_together(self):
        with self.assertRaises(ValueError):
            PersistentProfileApplicationService(
                authenticate=lambda _request: self.actor,
                authorize=lambda _actor: object(),
                durable_authorization_gateway=self.gateway,
                connection_provider=self.provider,
            )

    def test_composed_request_uses_supplied_connection_without_auxiliary_connect(self):
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = ON")

        @contextmanager
        def supplied_connection():
            try:
                yield connection
            finally:
                connection.close()

        integration = self.integration(provider=supplied_connection)
        with mock.patch(
            "sqlite3.connect",
            side_effect=AssertionError("auxiliary connection attempted"),
        ) as connect:
            status, _headers, body = self.request(
                "GET",
                "/account/profile",
                integration=integration,
            )
        self.assertEqual(status, 200)
        self.assertIn(b"No persistent profile yet", body)
        self.assertEqual(connect.call_count, 0)

    def test_authorized_account_without_profile_receives_stable_empty_state(self):
        status, headers, body = self.request(
            "GET",
            "/account/profile",
            integration=self.integration(),
        )
        self.assertEqual(status, 200)
        self.assertIn(b"No persistent profile yet", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.provider.opened, self.provider.closed)
        self.assertEqual(profile_counts(self.writer), (0, 0, 0, 0))

    def test_active_archived_and_deletion_requested_profiles_keep_b2c1_policy(self):
        created = self.create_profile()
        status, _headers, active = self.request(
            "GET", "/account/profile", integration=self.integration()
        )
        self.assertEqual(status, 200)
        self.assertIn(b"Professional domains", active)

        self.append(created, 1, "archive")
        status, _headers, archived = self.request(
            "GET", "/account/profile", integration=self.integration()
        )
        self.assertEqual(status, 200)
        self.assertIn(b"archived and remains read-only", archived)
        self.assertIn(b"Professional domains", archived)

        self.append(created, 2, "deletion_request")
        status, _headers, deleting = self.request(
            "GET", "/account/profile", integration=self.integration()
        )
        self.assertEqual(status, 200)
        self.assertIn(b"Profile content is hidden", deleting)
        self.assertNotIn(b"Professional domains", deleting)

    def test_durable_denials_are_generic_and_do_not_open_another_profile(self):
        self.create_profile()
        suspend_account(self.writer, self.state)
        status, _headers, body = self.request(
            "GET", "/account/profile", integration=self.integration()
        )
        self.assertEqual(status, 404)
        self.assertIn(b"Profile not found", body)
        self.assertNotIn(self.state["principal_id"].encode(), body)

    def test_active_account_without_binding_is_generic_not_found(self):
        account_id = add_active_user(self.writer, "browser-unbound")
        self.writer.commit()
        actor = _TRUSTED_AUTHENTICATION_ACTOR_ISSUER.issue(
            "browser-unbound-actor",
            account_id=account_id,
            environment_namespace=self.state["environment"],
        )
        status, _headers, body = self.request(
            "GET", "/account/profile", integration=self.integration(actor=actor)
        )
        self.assertEqual(status, 404)
        self.assertIn(b"Profile not found", body)

    def test_suspended_binding_is_generic_not_found(self):
        transition_binding(self.writer, self.state, "suspended")
        status, _headers, body = self.request(
            "GET", "/account/profile", integration=self.integration()
        )
        self.assertEqual(status, 404)
        self.assertIn(b"Profile not found", body)

    def test_released_binding_is_generic_not_found(self):
        transition_binding(self.writer, self.state, "released")
        status, _headers, body = self.request(
            "GET", "/account/profile", integration=self.integration()
        )
        self.assertEqual(status, 404)
        self.assertIn(b"Profile not found", body)

    def test_wrong_environment_is_generic_not_found(self):
        wrong_actor = trusted_actor(self.state, environment="production")
        status, _headers, body = self.request(
            "GET", "/account/profile", integration=self.integration(actor=wrong_actor)
        )
        self.assertEqual(status, 404)
        self.assertIn(b"Profile not found", body)

    def test_inactive_principal_is_generic_not_found(self):
        set_principal_status(self.writer, self.state, "suspended")
        status, _headers, body = self.request(
            "GET", "/account/profile", integration=self.integration()
        )
        self.assertEqual(status, 404)
        self.assertIn(b"Profile not found", body)

    def test_multiple_owner_bindings_are_sanitized_unavailable(self):
        other_principal = add_principal(
            self.writer,
            suffix="86",
            environment=self.state["environment"],
            principal_type="account_native",
            status="active",
            claim_policy="account_native",
            exclusive=1,
        )
        other_binding = add_binding(
            self.writer,
            other_principal,
            self.state["account_id"],
            suffix="86",
            environment=self.state["environment"],
        )
        add_activation_event(
            self.writer,
            other_principal,
            self.state["account_id"],
            other_binding,
            suffix="86",
            environment=self.state["environment"],
        )
        self.writer.commit()
        status, _headers, body = self.request(
            "GET", "/account/profile", integration=self.integration()
        )
        self.assertEqual(status, 503)
        self.assertIn(b"temporarily unavailable", body)

    def test_schema_unavailable_is_sanitized_and_server_remains_usable(self):
        self.writer.execute("DROP INDEX idx_principal_account_bindings_user_status")
        self.writer.commit()
        integration = self.integration()
        for _ in range(2):
            status, _headers, body = self.request(
                "GET", "/account/profile", integration=integration
            )
            self.assertEqual(status, 503)
            self.assertIn(b"temporarily unavailable", body)

    def test_lock_contention_is_sanitized(self):
        self.writer.execute("BEGIN EXCLUSIVE")
        try:
            provider = ReadOnlyAuthorizationProvider(self.path, timeout=0.01)
            status, _headers, body = self.request(
                "GET",
                "/account/profile",
                integration=self.integration(provider=provider),
            )
        finally:
            self.writer.rollback()
        self.assertEqual(status, 503)
        self.assertIn(b"temporarily unavailable", body)

    def test_request_identity_forgery_is_rejected_before_authentication(self):
        calls = []
        service = PersistentProfileApplicationService(
            authenticate=lambda request: calls.append(request) or self.actor,
            durable_authorization_gateway=self.gateway,
            connection_provider=self.provider,
        )
        integration = PersistentProfileBrowserIntegration(service)
        for target in (
            "/account/profile?account_id=forged",
            "/account/profile?principal_id=forged",
            "/account/profile?profile_id=forged",
            "/account/profile?environment=production",
            "/account/profile?scope=write",
        ):
            status, _headers, body = self.request("GET", target, integration=integration)
            self.assertEqual(status, 400)
            self.assertIn(b"request is not valid", body)
        self.assertEqual(calls, [])
        self.assertEqual(self.provider.opened, 0)

    def test_composed_request_executes_no_sqlite_write_and_preserves_file(self):
        self.create_profile()
        actions = []
        statements = []

        def authorizer(action, _one, _two, _db, _source):
            actions.append(action)
            return sqlite3.SQLITE_OK

        provider = ReadOnlyAuthorizationProvider(
            self.path,
            authorizer=authorizer,
            trace=statements.append,
        )
        before = file_fingerprint(self.path)
        status, _headers, _body = self.request(
            "GET", "/account/profile", integration=self.integration(provider=provider)
        )
        self.assertEqual(status, 200)
        self.assertEqual(file_fingerprint(self.path), before)
        forbidden = {
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_TABLE,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_TABLE,
            sqlite3.SQLITE_ALTER_TABLE,
        }
        self.assertTrue(actions)
        self.assertTrue(forbidden.isdisjoint(actions))
        prohibited_sql = (
            "INSERT ",
            "UPDATE ",
            "DELETE ",
            "REPLACE ",
            "CREATE ",
            "DROP ",
            "ALTER ",
            "VACUUM",
        )
        self.assertTrue(statements)
        self.assertTrue(
            all(
                not statement.lstrip().upper().startswith(prohibited_sql)
                for statement in statements
            )
        )
        self.assertEqual(provider.opened, provider.closed)

    def test_authorization_and_profile_read_share_one_snapshot_during_revocation(self):
        created = self.create_profile()
        self.writer.execute("PRAGMA journal_mode = WAL")
        authorized = threading.Event()
        revoked = threading.Event()
        import wahojobs.persistent_profile_read_authorization as authorization

        original = authorization._binding_lineage_current

        def paused_lineage(*args, **kwargs):
            result = original(*args, **kwargs)
            authorized.set()
            self.assertTrue(revoked.wait(timeout=3))
            return result

        observed = []
        with mock.patch(
            "wahojobs.persistent_profile_read_authorization._binding_lineage_current",
            side_effect=paused_lineage,
        ):
            thread = threading.Thread(
                target=lambda: observed.append(
                    self.request("GET", "/account/profile", integration=self.integration())
                )
            )
            thread.start()
            self.assertTrue(authorized.wait(timeout=3))
            transition_binding(self.writer, self.state, "released")
            revoked.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(observed[0][0], 200)
        self.assertIn(b"Professional domains", observed[0][2])
        self.assertNotIn(created.profile_id.encode(), observed[0][2])

        status, _headers, body = self.request(
            "GET", "/account/profile", integration=self.integration()
        )
        self.assertEqual(status, 404)
        self.assertIn(b"Profile not found", body)


if __name__ == "__main__":
    unittest.main()
