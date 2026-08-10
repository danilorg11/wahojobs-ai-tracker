import hashlib
import http.client
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from http.server import ThreadingHTTPServer

from scripts.local_product_app import make_handler
from tests.persistent_profiles_repository_test_support import (
    account_context,
    append_command,
    canonical_fixture,
    create_command,
    install_repository_database,
    profile_counts,
    reference,
)
from wahojobs.persistent_profiles_application import (
    PersistentProfileApplicationService,
    PersistentProfileFieldGroup,
    PersistentProfileHistoryView,
    PersistentProfilePageResult,
    PersistentProfileView,
    _LEGACY_PROFILE_READ_GRANT_ISSUER,
    _TRUSTED_AUTHENTICATION_ACTOR_ISSUER,
)
from wahojobs.persistent_profiles_browser import (
    FIND_MATCHES_ROUTE,
    MAX_PROFILE_BROWSER_RESPONSE_BYTES,
    PersistentProfileBrowserIntegration,
    PersistentProfileBrowserResponse,
    _create_response_for_outcome,
    render_persistent_profile_page,
)
from wahojobs.persistent_profiles_repository import (
    append_profile_revision,
    create_persistent_profile,
)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BrowserReadOnlyProvider:
    def __init__(self, path, *, authorizer=None, timeout=0.15):
        self.path = Path(path)
        self.authorizer = authorizer
        self.timeout = timeout
        self.opened = 0
        self.closed = 0

    def __call__(self):
        @contextmanager
        def connection_scope():
            connection = sqlite3.connect(
                f"file:{self.path.as_posix()}?mode=ro",
                uri=True,
                timeout=self.timeout,
            )
            self.opened += 1
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA query_only = ON")
                if self.authorizer is not None:
                    connection.set_authorizer(self.authorizer)
                yield connection
            finally:
                connection.close()
                self.closed += 1

        return connection_scope()


class FakeMatchesIntegration:
    def __init__(self):
        self.calls = []
        self.close_calls = 0
        self.response = PersistentProfileBrowserResponse(
            200,
            b"matches",
            (("Content-Length", "7"),),
        )
        self._closed = False

    @staticmethod
    def matches_route(path):
        return path == FIND_MATCHES_ROUTE

    def handle(
        self,
        method,
        target,
        authentication_input=None,
        body_stream=None,
    ):
        self.calls.append(
            (method, target, authentication_input, body_stream)
        )
        return self.response

    def close(self):
        self.close_calls += 1
        self._closed = True
        return True

    @property
    def closed(self):
        return self._closed


class PersistentProfileBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "browser.sqlite"
        self.writer = install_repository_database(self.path)
        self.principal = account_context(self.writer)
        self.writer.commit()
        self.actor = _TRUSTED_AUTHENTICATION_ACTOR_ISSUER.issue("browser-test-actor")
        self.grant = _LEGACY_PROFILE_READ_GRANT_ISSUER.issue(self.principal)
        self.provider = BrowserReadOnlyProvider(self.path)
        self.auth_calls = 0
        self.authorization_calls = 0

    def tearDown(self):
        self.writer.close()
        self.temp.cleanup()

    def authenticate(self, _request):
        self.auth_calls += 1
        return self.actor

    def authorize(self, _actor):
        self.authorization_calls += 1
        return self.grant

    def integration(
        self,
        *,
        authenticate=None,
        authorize=None,
        provider=None,
        matches_integration=None,
    ):
        service = PersistentProfileApplicationService(
            authenticate=authenticate or self.authenticate,
            authorize=authorize or self.authorize,
            connection_provider=provider or self.provider,
        )
        return PersistentProfileBrowserIntegration(
            service,
            matches_integration=matches_integration,
        )

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
            result = (response.status, dict(response.getheaders()), body)
            connection.close()
            return result
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def create_profile(self):
        result = create_persistent_profile(self.writer, create_command(self.principal))
        self.writer.commit()
        return result

    def append(self, created, revision, kind):
        result = append_profile_revision(
            self.writer,
            append_command(
                self.principal,
                reference(created, self.principal),
                canonical_fixture(created.profile_id),
                expected_revision=revision,
                revision_kind=kind,
                idempotency_key=f"browser-{kind}-{revision:04d}",
            ),
        )
        self.writer.commit()
        return result

    def test_integration_disabled_leaves_route_absent_and_normal_health_unchanged(self):
        status, _headers, _body = self.request("GET", "/account/profile")
        self.assertEqual(status, 404)
        status, _headers, body = self.request("GET", "/health")
        self.assertEqual((status, body), (200, b"ok\n"))
        self.assertEqual((self.auth_calls, self.provider.opened), (0, 0))

    def test_get_head_and_write_method_contract(self):
        integration = self.integration()
        get_status, get_headers, get_body = self.request(
            "GET", "/account/profile", integration=integration
        )
        self.assertEqual(get_status, 200)
        self.assertIn(b"No persistent profile yet", get_body)
        self.assertIn(b"href='/find-matches'>Create profile</a>", get_body)
        self.assertIn(b"href='/logout'", get_body)
        self.assertEqual(get_headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(get_headers["Cache-Control"], "no-store")
        self.assertEqual(
            get_headers["Content-Security-Policy"],
            "default-src 'none'; style-src 'unsafe-inline'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
        self.assertNotIn(
            "accounts.google.com",
            get_headers["Content-Security-Policy"],
        )

        head_status, head_headers, head_body = self.request(
            "HEAD", "/account/profile", integration=integration
        )
        self.assertEqual(head_status, 200)
        self.assertEqual(head_body, b"")
        self.assertGreater(int(head_headers["Content-Length"]), 0)

        calls_before = self.auth_calls
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            status, headers, body = self.request(
                method, "/account/profile", integration=integration
            )
            self.assertEqual(status, 405)
            self.assertEqual(headers["Allow"], "GET, HEAD")
            self.assertIn(b"read-only", body)
        self.assertEqual(self.auth_calls, calls_before)

    def test_optional_matches_integration_delegates_and_closes_with_profile_shell(self):
        matches = FakeMatchesIntegration()
        integration = self.integration(matches_integration=matches)
        request_headers = (("Host", "app.test"),)
        body_stream = object()

        self.assertTrue(integration.matches_route(FIND_MATCHES_ROUTE))
        response = integration.handle(
            "POST",
            FIND_MATCHES_ROUTE + "?review=1",
            request_headers,
            body_stream,
        )
        self.assertIs(response, matches.response)
        self.assertEqual(
            matches.calls,
            [
                (
                    "POST",
                    FIND_MATCHES_ROUTE + "?review=1",
                    request_headers,
                    body_stream,
                )
            ],
        )

        self.assertTrue(integration.close())
        self.assertTrue(integration.closed)
        self.assertEqual(matches.close_calls, 1)
        unavailable = integration.handle("GET", FIND_MATCHES_ROUTE)
        self.assertEqual(unavailable.status, 503)
        self.assertEqual(len(matches.calls), 1)

    def test_absent_matches_integration_fails_closed(self):
        integration = self.integration()
        self.assertFalse(integration.matches_route(FIND_MATCHES_ROUTE))
        response = integration.handle("GET", FIND_MATCHES_ROUTE)
        self.assertEqual(response.status, 404)
        self.assertIn(b"Page not found", response.body)

    def test_matches_integration_can_be_attached_exactly_once_before_use(self):
        integration = self.integration()
        matches = FakeMatchesIntegration()

        self.assertTrue(integration.attach_matches_integration(matches))
        self.assertTrue(integration.matches_route(FIND_MATCHES_ROUTE))
        with self.assertRaisesRegex(
            ValueError,
            "invalid_persistent_profile_browser_configuration",
        ):
            integration.attach_matches_integration(FakeMatchesIntegration())

        self.assertTrue(integration.close())
        self.assertEqual(matches.close_calls, 1)

    def test_successful_profile_create_redirects_to_matches(self):
        response = _create_response_for_outcome("created")
        self.assertEqual(response.status, 303)
        self.assertEqual(dict(response.headers)["Location"], FIND_MATCHES_ROUTE)

    def test_authentication_and_authorization_states_are_generic(self):
        marker = "private-gateway-marker"
        states = (
            (lambda _request: None, self.authorize, 401, b"Authentication required"),
            (self.authenticate, lambda _actor: None, 404, b"Profile not found"),
            (lambda _request: object(), self.authorize, 503, b"temporarily unavailable"),
        )
        for authenticate, authorize, expected_status, expected_text in states:
            with self.subTest(expected_status=expected_status):
                status, _headers, body = self.request(
                    "GET",
                    "/account/profile",
                    integration=self.integration(
                        authenticate=authenticate,
                        authorize=authorize,
                    ),
                )
                self.assertEqual(status, expected_status)
                self.assertIn(expected_text, body)
                self.assertNotIn(marker.encode(), body)

        def fail(_value):
            raise RuntimeError(marker)

        for integration in (
            self.integration(authenticate=fail),
            self.integration(authorize=fail),
        ):
            status, headers, body = self.request(
                "GET", "/account/profile", integration=integration
            )
            self.assertEqual(status, 503)
            self.assertNotIn(marker.encode(), body)
            self.assertNotIn(marker, repr(headers))

    def test_authentication_page_links_to_login_without_identity_or_redirect_input(self):
        result = PersistentProfilePageResult("authentication_required")
        page, status = render_persistent_profile_page(result)
        self.assertEqual(int(status), 401)
        self.assertIn("href='/login'", page)
        self.assertIn("Continue to sign in", page)
        self.assertNotIn("return_to", page)
        self.assertNotIn("Sign-in integration is not available", page)

    def test_trusted_gateway_can_consume_request_credentials_without_request_selected_identity(self):
        token = "trusted-test-session-token"
        claimed_identity = "forged-browser-identity"

        def authenticate(request):
            headers = request.authentication_input_for_gateway()
            if headers.get("Authorization") == f"Bearer {token}":
                return self.actor
            return None

        integration = self.integration(authenticate=authenticate)
        status, _headers, body = self.request(
            "GET",
            "/account/profile",
            integration=integration,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Claimed-Principal": claimed_identity,
            },
        )
        self.assertEqual(status, 200)
        self.assertIn(b"No persistent profile yet", body)
        self.assertNotIn(token.encode(), body)
        self.assertNotIn(claimed_identity.encode(), body)

        status, _headers, body = self.request(
            "GET",
            "/account/profile",
            integration=integration,
            headers={"X-Claimed-Principal": claimed_identity},
        )
        self.assertEqual(status, 401)
        self.assertNotIn(claimed_identity.encode(), body)

    def test_request_identity_overrides_and_invalid_cursors_are_rejected_before_auth(self):
        integration = self.integration()
        targets = (
            "/account/profile?principal_id=forged",
            "/account/profile?profile_id=forged",
            "/account/profile?account_id=forged",
            "/account/profile?environment=production",
            "/account/profile?scope=admin",
            "/account/profile?before=+2",
            "/account/profile?before=-2",
            "/account/profile?before=2.0",
            "/account/profile?before=2e1",
            "/account/profile?before=0002",
            "/account/profile?before=2&before=3",
        )
        for target in targets:
            with self.subTest(target=target):
                status, _headers, body = self.request("GET", target, integration=integration)
                self.assertEqual(status, 400)
                self.assertIn(b"request is not valid", body)
        self.assertEqual(self.auth_calls, 0)
        self.assertEqual(self.provider.opened, 0)

    def test_active_profile_hides_revision_history_and_durable_identifiers(self):
        created = self.create_profile()
        self.append(created, 1, "edit")
        status, _headers, body = self.request(
            "GET", "/account/profile", integration=self.integration()
        )
        text = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("Account profile", text)
        self.assertIn("Professional domains", text)
        self.assertNotIn("Revision history", text)
        self.assertNotIn("Revision 2", text)
        self.assertNotIn("Revision 1", text)
        self.assertNotIn("Older revisions", text)
        for secret in (
            created.profile_id,
            created.revision_id,
            self.principal.principal_id,
            "structured_profile_json",
            "source_payload",
        ):
            self.assertNotIn(secret, text)

    def test_archived_and_deletion_requested_presentation(self):
        created = self.create_profile()
        self.append(created, 1, "archive")
        status, _headers, archived = self.request(
            "GET", "/account/profile", integration=self.integration()
        )
        self.assertEqual(status, 200)
        self.assertIn(b"archived and remains read-only", archived)
        self.assertIn(b"Professional domains", archived)
        self.assertNotIn(b"Reactivate", archived)

        self.append(created, 2, "deletion_request")
        status, _headers, deleting = self.request(
            "GET", "/account/profile", integration=self.integration()
        )
        self.assertEqual(status, 200)
        self.assertIn(b"Profile content is hidden", deleting)
        self.assertNotIn(b"Professional domains", deleting)
        self.assertNotIn(b"Delete", deleting)
        self.assertNotIn(b"Purge", deleting)

    def test_malformed_durable_profile_and_schema_failure_are_generic(self):
        created = self.create_profile()
        trigger_sql = self.writer.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_product_profile_revisions_no_update'"
        ).fetchone()[0]
        self.writer.execute("DROP TRIGGER trg_product_profile_revisions_no_update")
        self.writer.execute("PRAGMA ignore_check_constraints = ON")
        self.writer.execute(
            "UPDATE product_profile_revisions SET structured_profile_json='{' "
            "WHERE revision_id=?",
            (created.revision_id,),
        )
        self.writer.execute("PRAGMA ignore_check_constraints = OFF")
        self.writer.execute(trigger_sql)
        self.writer.commit()
        status, _headers, body = self.request(
            "GET", "/account/profile", integration=self.integration()
        )
        self.assertEqual(status, 503)
        self.assertIn(b"could not be loaded safely", body)
        self.assertNotIn(created.revision_id.encode(), body)
        self.assertNotIn(b"structured_profile_json", body)

        other = Path(self.temp.name) / "missing-schema.sqlite"
        sqlite3.connect(other).close()
        status, _headers, body = self.request(
            "GET",
            "/account/profile",
            integration=self.integration(provider=BrowserReadOnlyProvider(other)),
        )
        self.assertEqual(status, 503)
        self.assertIn(b"temporarily unavailable", body)

    def test_history_cursor_is_bounded_without_exposing_revision_history(self):
        created = self.create_profile()
        for revision in range(1, 22):
            self.append(created, revision, "edit")
        status, _headers, first = self.request(
            "GET", "/account/profile", integration=self.integration()
        )
        self.assertEqual(status, 200)
        self.assertNotIn(b"history-item", first)
        self.assertNotIn(b"Older revisions", first)
        status, _headers, second = self.request(
            "GET", "/account/profile?before=3", integration=self.integration()
        )
        self.assertEqual(status, 200)
        self.assertEqual(second, first)
        self.assertNotIn(b"Revision history", second)
        self.assertNotIn(b"Older revisions", second)

    def test_wrong_principal_is_empty_and_cannot_observe_another_profile(self):
        created = self.create_profile()
        other = account_context(self.writer, suffix="77")
        self.writer.commit()
        other_grant = _LEGACY_PROFILE_READ_GRANT_ISSUER.issue(other)
        integration = self.integration(authorize=lambda _actor: other_grant)
        status, _headers, body = self.request(
            "GET", "/account/profile", integration=integration
        )
        self.assertEqual(status, 200)
        self.assertIn(b"No persistent profile yet", body)
        self.assertNotIn(created.profile_id.encode(), body)

    def test_html_escapes_hostile_content_and_strips_bidi_controls(self):
        hostile = "<script>alert('x')</script> & \"quoted\" \u202eabc"
        result = PersistentProfilePageResult(
            "active",
            profile=PersistentProfileView(
                display_name=hostile,
                lifecycle_status="active",
                revision_number=1,
                updated_at="2026-07-20T12:00:00Z",
                field_groups=(PersistentProfileFieldGroup("Skills", (hostile,)),),
                structured_content_visible=True,
            ),
            history=(
                PersistentProfileHistoryView(
                    1, "initial", "active", "2026-07-20T12:00:00Z"
                ),
            ),
        )
        page, status = render_persistent_profile_page(result)
        self.assertEqual(int(status), 200)
        self.assertIn("href='/find-matches'>Find matches</a>", page)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&amp;", page)
        self.assertIn("&quot;quoted&quot;", page)
        self.assertNotIn("\u202e", page)

    def test_oversized_valid_view_returns_one_generic_bounded_response(self):
        values = tuple("x" * 4096 for _ in range(32))
        groups = tuple(
            PersistentProfileFieldGroup(f"Group {index}", values) for index in range(8)
        )
        result = PersistentProfilePageResult(
            "active",
            profile=PersistentProfileView(
                display_name="Bounded profile",
                lifecycle_status="active",
                revision_number=1,
                updated_at="2026-07-20T12:00:00Z",
                field_groups=groups,
                structured_content_visible=True,
            ),
        )
        service = PersistentProfileApplicationService(
            authenticate=self.authenticate,
            authorize=self.authorize,
            connection_provider=self.provider,
        )
        integration = PersistentProfileBrowserIntegration(service)
        with mock.patch.object(
            PersistentProfileApplicationService,
            "read_my_profile",
            return_value=result,
        ):
            response = integration.handle("GET", "/account/profile")
        self.assertEqual(response.status, 503)
        self.assertLess(len(response.body), MAX_PROFILE_BROWSER_RESPONSE_BYTES)
        self.assertIn(b"could not be displayed safely", response.body)
        self.assertNotIn(b"x" * 128, response.body)

    def test_empty_profile_rendering_and_html_renderer_failure_are_bounded(self):
        result = PersistentProfilePageResult(
            "active",
            profile=PersistentProfileView(
                display_name="Profile",
                lifecycle_status="active",
                revision_number=1,
                updated_at="2026-07-20T12:00:00Z",
                field_groups=(),
                structured_content_visible=True,
            ),
        )
        page, status = render_persistent_profile_page(result)
        self.assertEqual(int(status), 200)
        self.assertIn("No additional profile details are available", page)
        self.assertNotIn("Revision history", page)

        service = PersistentProfileApplicationService(
            authenticate=self.authenticate,
            authorize=self.authorize,
            connection_provider=self.provider,
        )
        integration = PersistentProfileBrowserIntegration(service)
        with mock.patch.object(
            PersistentProfileApplicationService,
            "read_my_profile",
            return_value=result,
        ), mock.patch(
            "wahojobs.persistent_profiles_browser.render_persistent_profile_page",
            side_effect=RuntimeError("renderer-secret"),
        ):
            response = integration.handle("GET", "/account/profile")
        self.assertEqual(response.status, 503)
        self.assertNotIn(b"renderer-secret", response.body)

    def test_invalid_injected_response_is_sanitized_by_existing_server(self):
        marker = "invalid-integration-marker"

        class InvalidIntegration:
            @staticmethod
            def matches_route(path):
                return path == "/account/profile"

            @staticmethod
            def handle(_method, _target, _authentication_input=None):
                return type(
                    "BadResponse",
                    (),
                    {"status": 200, "headers": (), "body": marker},
                )()

        status, _headers, body = self.request(
            "GET", "/account/profile", integration=InvalidIntegration()
        )
        self.assertEqual(status, 503)
        self.assertNotIn(marker.encode(), body)
        status, _headers, health = self.request("GET", "/health")
        self.assertEqual((status, health), (200, b"ok\n"))

    def test_response_body_write_failure_is_contained_after_safe_headers(self):
        class FailingWriter:
            @staticmethod
            def write(_body):
                raise OSError("private-transport-marker")

        handler_type = make_handler(
            persistent_profile_browser_integration=self.integration()
        )
        handler = object.__new__(handler_type)
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = FailingWriter()
        response = PersistentProfileBrowserResponse(
            200,
            b"safe body",
            (
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", "9"),
            ),
        )
        handler.write_profile_browser_response(response)
        handler.send_response.assert_called_once_with(200)
        handler.end_headers.assert_called_once_with()

    def test_browser_read_uses_no_sqlite_write_actions_and_preserves_file(self):
        self.create_profile()
        actions = []

        def authorizer(action, _one, _two, _db, _source):
            actions.append(action)
            return sqlite3.SQLITE_OK

        provider = BrowserReadOnlyProvider(self.path, authorizer=authorizer)
        before = (
            sha256(self.path),
            self.path.stat().st_size,
            self.path.stat().st_mtime_ns,
            profile_counts(self.writer),
        )
        status, _headers, _body = self.request(
            "GET",
            "/account/profile",
            integration=self.integration(provider=provider),
        )
        after = (
            sha256(self.path),
            self.path.stat().st_size,
            self.path.stat().st_mtime_ns,
            profile_counts(self.writer),
        )
        self.assertEqual(status, 200)
        self.assertEqual(before, after)
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
        self.assertEqual(provider.opened, provider.closed)
        self.assertFalse(Path(str(self.path) + "-wal").exists())
        self.assertFalse(Path(str(self.path) + "-shm").exists())
        self.assertFalse(Path(str(self.path) + "-journal").exists())


if __name__ == "__main__":
    unittest.main()
