import http.client
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from scripts.local_product_app import make_handler
from tests.browser_session_authentication_test_support import (
    AUTHENTICATED_AT,
    REQUEST_AT,
    TracedReadOnlyProvider,
    install_browser_authentication_database,
    seed_browser_session,
)
from tests.persistent_profile_read_authorization_test_support import file_fingerprint
from wahojobs import accounts
from wahojobs.browser_session_authentication import (
    DurableBrowserSessionAuthenticationGateway,
    SESSION_COOKIE_NAME,
)
from wahojobs.persistent_profile_read_authorization import (
    DurablePersistentProfileReadAuthorizationGateway,
)
from wahojobs.persistent_profiles_application import PersistentProfileApplicationService
from wahojobs.persistent_profiles_browser import PersistentProfileBrowserIntegration


class BrowserSessionAuthenticationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "browser-session-integration.sqlite"
        self.writer = install_browser_authentication_database(self.path)
        self.state = seed_browser_session(self.writer)
        self.authentication = DurableBrowserSessionAuthenticationGateway(
            trusted_environment_namespace=self.state["environment"],
            clock=lambda: REQUEST_AT,
        )
        self.authorization = DurablePersistentProfileReadAuthorizationGateway()
        self.provider = TracedReadOnlyProvider(self.path)

    def tearDown(self):
        self.writer.close()
        self.temp.cleanup()

    def integration(self, *, provider=None):
        service = PersistentProfileApplicationService(
            durable_authentication_gateway=self.authentication,
            durable_authorization_gateway=self.authorization,
            connection_provider=provider or self.provider,
        )
        return PersistentProfileBrowserIntegration(service)

    def request(self, *, token=None, integration=None):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(persistent_profile_browser_integration=integration or self.integration()),
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            headers = {}
            if token is not None:
                headers["Cookie"] = f"{SESSION_COOKIE_NAME}={token}"
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            connection.request("GET", "/account/profile", headers=headers)
            response = connection.getresponse()
            result = response.status, dict(response.getheaders()), response.read()
            connection.close()
            return result
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_valid_cookie_composes_authentication_authorization_and_empty_profile(self):
        status, headers, body = self.request(token=self.state["session_token"])
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(b"No persistent profile yet", body)
        self.assertEqual(self.provider.opened, 1)
        self.assertEqual(self.provider.closed, 1)
        self.assertEqual(len(set(self.provider.connection_ids)), 1)
        statements = tuple(item.strip().upper() for item in self.provider.statements)
        self.assertEqual(sum(item == "BEGIN" for item in statements), 1)
        self.assertEqual(sum(item == "ROLLBACK" for item in statements), 1)

    def test_missing_unknown_expired_and_revoked_sessions_require_authentication(self):
        status, _headers, body = self.request()
        self.assertEqual(status, 401)
        self.assertIn(b"Authentication required", body)

        status, _headers, body = self.request(token="A" * 43)
        self.assertEqual(status, 401)
        self.assertIn(b"Authentication required", body)

        expired_gateway = DurableBrowserSessionAuthenticationGateway(
            trusted_environment_namespace=self.state["environment"],
            clock=lambda: AUTHENTICATED_AT + timedelta(hours=2),
        )
        expired = PersistentProfileBrowserIntegration(
            PersistentProfileApplicationService(
                durable_authentication_gateway=expired_gateway,
                durable_authorization_gateway=self.authorization,
                connection_provider=self.provider,
            )
        )
        status, _headers, body = self.request(
            token=self.state["session_token"],
            integration=expired,
        )
        self.assertEqual(status, 401)
        self.assertIn(b"Authentication required", body)

        accounts.revoke_current_session(
            self.writer,
            session_token=self.state["session_token"],
            expected_session_version=1,
            reason="user_logout",
            now=REQUEST_AT,
        )
        self.writer.commit()
        status, _headers, body = self.request(token=self.state["session_token"])
        self.assertEqual(status, 401)
        self.assertIn(b"Authentication required", body)

    def test_malformed_durable_state_is_generic_unavailable(self):
        self.writer.execute("PRAGMA ignore_check_constraints = ON")
        self.writer.execute(
            "UPDATE account_sessions SET session_version = 0 WHERE session_id = ?",
            (self.state["session_id"],),
        )
        self.writer.execute("PRAGMA ignore_check_constraints = OFF")
        self.writer.commit()
        status, _headers, body = self.request(token=self.state["session_token"])
        self.assertEqual(status, 503)
        self.assertIn(b"temporarily unavailable", body)
        self.assertNotIn(self.state["session_id"].encode(), body)

    def test_composed_browser_flow_is_read_only_and_opens_no_auxiliary_connection(self):
        actions = []

        def authorizer(action, _one, _two, _db, _source):
            actions.append(action)
            return sqlite3.SQLITE_OK

        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = ON")
        connection.set_authorizer(authorizer)

        @contextmanager
        def provider():
            try:
                yield connection
            finally:
                connection.close()

        before = file_fingerprint(self.path)
        with mock.patch(
            "sqlite3.connect",
            side_effect=AssertionError("auxiliary connection attempted"),
        ) as connect:
            status, _headers, _body = self.request(
                token=self.state["session_token"],
                integration=self.integration(provider=provider),
            )
        self.assertEqual(status, 200)
        self.assertEqual(connect.call_count, 0)
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

    def test_authentication_and_authorization_share_one_snapshot_across_revocation(self):
        self.writer.execute("PRAGMA journal_mode = WAL")
        authenticated = threading.Event()
        revoked = threading.Event()
        original = DurablePersistentProfileReadAuthorizationGateway.authorize_persistent_profile_read

        def paused_authorization(gateway, connection, actor):
            authenticated.set()
            self.assertTrue(revoked.wait(timeout=3))
            return original(gateway, connection, actor)

        observed = []
        with mock.patch.object(
            DurablePersistentProfileReadAuthorizationGateway,
            "authorize_persistent_profile_read",
            new=paused_authorization,
        ):
            thread = threading.Thread(
                target=lambda: observed.append(self.request(token=self.state["session_token"]))
            )
            thread.start()
            self.assertTrue(authenticated.wait(timeout=3))
            accounts.revoke_current_session(
                self.writer,
                session_token=self.state["session_token"],
                expected_session_version=1,
                reason="security_reset",
                now=REQUEST_AT,
            )
            self.writer.commit()
            revoked.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(observed[0][0], 200)
        self.assertIn(b"No persistent profile yet", observed[0][2])
        self.assertEqual(self.request(token=self.state["session_token"])[0], 401)

    def test_durable_mode_is_explicit_and_has_no_legacy_fallback(self):
        with self.assertRaises(ValueError):
            PersistentProfileApplicationService(
                authenticate=lambda _request: object(),
                durable_authentication_gateway=self.authentication,
                durable_authorization_gateway=self.authorization,
                connection_provider=self.provider,
            )
        with self.assertRaises(ValueError):
            PersistentProfileApplicationService(
                durable_authentication_gateway=self.authentication,
                connection_provider=self.provider,
            )


if __name__ == "__main__":
    unittest.main()
