import io
import logging
import threading
import unittest
import warnings
from datetime import datetime, timezone
from types import GetSetDescriptorType, MemberDescriptorType
from unittest import mock

import wahojobs.durable_google_login_browser as browser_module
from wahojobs.durable_google_login_browser import (
    AUTHENTICATED_DESTINATION,
    DurableGoogleLoginBrowserIntegration,
    DurableGoogleLoginBrowserResponse,
    GOOGLE_LOGIN_CALLBACK_ROUTE,
    GOOGLE_LOGIN_START_ROUTE,
    GOOGLE_TRANSACTION_COOKIE_NAME,
    LOGIN_CSRF_COOKIE_NAME,
    LOGIN_ROUTE,
    LOGOUT_ROUTE,
    SESSION_CSRF_COOKIE_NAME,
)


NOW = datetime(2026, 7, 25, 14, 0, 0, tzinfo=timezone.utc)
ORIGIN = "https://app.test"
AUTHORITY = "app.test"
LOGIN_CSRF = "l" * 43
SESSION_TOKEN = "s" * 43
SESSION_CSRF = "c" * 43
TRANSACTION_ID = "oidctx_" + ("a" * 32)
INVITATION_CREDENTIAL = "inv_" + ("b" * 32) + "." + ("C" * 43)
AUTHORIZATION_URL = (
    "https://accounts.google.com/o/oauth2/v2/auth?client_id=test-client&state=opaque"
)
SESSION_COOKIE = (
    f"wahojobs_session={SESSION_TOKEN}; Path=/; Max-Age=3600; "
    "Expires=Sat, 25 Jul 2026 15:00:00 GMT; Secure; HttpOnly; SameSite=Lax"
)


class FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakePreparedAuthorization:
    def __init__(self, events):
        self.events = events
        self.closed = False

    @property
    def transaction_id(self):
        self.events.append("transaction_id_read")
        return TRANSACTION_ID

    @property
    def authorization_url(self):
        self.events.append("authorization_url_read")
        return AUTHORIZATION_URL

    def close(self):
        self.closed = True
        self.events.append("prepared_closed")


class FakeCompletion:
    def __init__(self, status):
        self.status = status
        self.issued_session = (
            FakeIssuedSession() if status == "issued" else None
        )


class FakeIssuedSession:
    effective_expires_at = "2026-07-25T15:00:00+00:00"


class FakeDeliveryLease:
    def __init__(self, *, session_cookie=SESSION_COOKIE, csrf=SESSION_CSRF):
        self.set_cookie_header = session_cookie
        self.csrf_credential = csrf
        self.acknowledged = 0
        self.failed = 0

    def acknowledge_delivery(self):
        self.acknowledged += 1

    def fail_delivery(self):
        self.failed += 1


class RetainingBrowserFailure(Exception):
    __slots__ = ("retained",)

    def __init__(self, *values):
        super().__init__(*values)
        self.retained = values


class HostileBrowserFailureMeta(type):
    def __getattribute__(cls, name):
        if name == "__mro__":
            raise RuntimeError("hostile_exception_mro_hook")
        return super().__getattribute__(name)


class HostileRetainingBrowserFailure(
    Exception,
    metaclass=HostileBrowserFailureMeta,
):
    __slots__ = ("retained",)

    def __init__(self, *values):
        super().__init__(*values)
        self.retained = values
        self.add_note(values[0])


class FakeProfileIntegration:
    def __init__(self):
        self.calls = []
        self.response = object()

    @staticmethod
    def matches_route(path):
        return path == AUTHENTICATED_DESTINATION

    def handle(self, method, target, headers):
        self.calls.append((method, target, headers))
        return self.response


class BrowserHarness:
    def __init__(self):
        self.connections = []
        self.events = []
        self.prepare_calls = 0
        self.prepare_invitations = []
        self.prepare_invitation_buffers = []
        self.complete_calls = []
        self.vaults = []
        self.discarded = []
        self.delivery_leases = []
        self.completion_status = "issued"
        self.complete_error = None
        self.delivery_cookie = SESSION_COOKIE
        self.browser_time = NOW
        self.browser_clock_error = None
        self.delivery_times = []
        self.validate_calls = []
        self.revoke_calls = []
        self.validate_result = True
        self.revoke_result = True
        self.profile = FakeProfileIntegration()

    def connection_factory(self):
        connection = FakeConnection()
        self.connections.append(connection)
        return connection

    def prepare(
        self,
        connection,
        gateway,
        key_authority,
        *,
        invitation_credential=None,
    ):
        self.prepare_calls += 1
        self.prepare_invitations.append(
            None
            if invitation_credential is None
            else bytes(invitation_credential)
        )
        self.prepare_invitation_buffers.append(invitation_credential)
        self.events.append("prepare_returned")
        self.assert_dependencies(connection, gateway, key_authority)
        return FakePreparedAuthorization(self.events)

    def complete(
        self,
        connection,
        gateway,
        key_authority,
        callback_url,
        browser_transaction_id,
        completion_policy,
        vault,
    ):
        self.assert_dependencies(connection, gateway, key_authority)
        if completion_policy is not COMPLETION_POLICY or vault not in self.vaults:
            raise AssertionError("wrong completion dependencies")
        self.complete_calls.append((callback_url, browser_transaction_id))
        if self.complete_error is not None:
            raise self.complete_error
        self.events.append("complete_returned")
        return FakeCompletion(self.completion_status)

    def vault_factory(self):
        vault = object()
        self.vaults.append(vault)
        return vault

    def discard(self, vault):
        self.discarded.append(vault)

    def prepare_delivery(self, connection, completion, vault, *, now):
        self.delivery_times.append(now)
        if (
            connection not in self.connections
            or completion.status != "issued"
            or vault not in self.vaults
            or type(now) is not datetime
        ):
            raise AssertionError("wrong delivery dependencies")
        lease = FakeDeliveryLease(session_cookie=self.delivery_cookie)
        self.delivery_leases.append(lease)
        return lease

    def now(self):
        self.events.append("browser_clock_read")
        if self.browser_clock_error is not None:
            raise self.browser_clock_error
        return self.browser_time

    def validate_logout(
        self,
        connection,
        *,
        session_token,
        csrf_credential,
        now,
    ):
        self.validate_calls.append(
            (connection, session_token, csrf_credential, now)
        )
        return self.validate_result

    def revoke_logout(
        self,
        connection,
        *,
        session_token,
        csrf_credential,
        now,
    ):
        self.revoke_calls.append(
            (connection, session_token, csrf_credential, now)
        )
        return self.revoke_result

    @staticmethod
    def assert_dependencies(connection, gateway, key_authority):
        if (
            type(connection) is not FakeConnection
            or gateway is not GATEWAY
            or key_authority is not KEY_AUTHORITY
        ):
            raise AssertionError("wrong gateway dependencies")

    def integration(self):
        return DurableGoogleLoginBrowserIntegration(
            public_origin=ORIGIN,
            profile_integration=self.profile,
            connection_factory=self.connection_factory,
            gateway=GATEWAY,
            key_authority=KEY_AUTHORITY,
            completion_policy=COMPLETION_POLICY,
            request_secret_vault_factory=self.vault_factory,
            prepare_session_delivery=self.prepare_delivery,
            discard_request_secret_vault=self.discard,
            validate_logout=self.validate_logout,
            revoke_logout=self.revoke_logout,
            prepare_authorization=self.prepare,
            complete_authorization=self.complete,
            now=self.now,
            token_factory=lambda: LOGIN_CSRF,
        )


GATEWAY = object()
KEY_AUTHORITY = object()
COMPLETION_POLICY = object()


def headers(*, origin=False, cookie=None, body=None, extra=()):
    result = [("Host", AUTHORITY)]
    if origin:
        result.append(("Origin", ORIGIN))
        result.append(("Sec-Fetch-Site", "same-origin"))
    if cookie is not None:
        result.append(("Cookie", cookie))
    if body is not None:
        result.extend(
            (
                ("Content-Type", "application/x-www-form-urlencoded"),
                ("Content-Length", str(len(body))),
            )
        )
    result.extend(extra)
    return tuple(result)


def set_cookies(response):
    return tuple(value for name, value in response.headers if name == "Set-Cookie")


def raise_browser_failure(kind, protected, retained):
    if kind == "ordinary":
        error = RuntimeError(protected[0])
    elif kind == "custom":
        error = RetainingBrowserFailure(*protected)
    elif kind == "cause":
        cause = RetainingBrowserFailure(*protected)
        error = RuntimeError("closed browser dependency failure")
        error.retained = protected
        retained.append(cause)
        retained.append(error)
        raise error from cause
    elif kind == "context":
        error = RuntimeError("closed browser dependency failure")
        error.retained = protected
        try:
            raise RetainingBrowserFailure(*protected)
        except RetainingBrowserFailure as context:
            retained.append(context)
            retained.append(error)
            raise error
    elif kind == "notes":
        error = RuntimeError("closed browser dependency failure")
        error.retained = protected
        error.add_note(protected[0])
    elif kind == "hostile_sanitizer":
        error = HostileRetainingBrowserFailure(*protected)
    else:
        error = {
            "KeyboardInterrupt": KeyboardInterrupt,
            "SystemExit": SystemExit,
            "GeneratorExit": GeneratorExit,
        }[kind](protected[0])
        error.retained = protected
        error.add_note(protected[0])
    retained.append(error)
    raise error


def exception_graph_protected_hits(error, protected):
    pending = [error]
    seen = set()
    hits = []
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        for secret in protected:
            if value is secret or (
                type(value) is str
                and type(secret) is str
                and secret
                and secret in value
            ):
                hits.append(secret)
        if isinstance(value, BaseException):
            pending.extend(value.args)
            pending.extend(
                candidate
                for candidate in (value.__cause__, value.__context__)
                if candidate is not None
            )
            pending.extend(getattr(value, "__notes__", ()))
            pending.extend(getattr(value, "__dict__", {}).values())
            exception_mro = type.__getattribute__(type(value), "__mro__")
            for exception_type in exception_mro:
                if exception_type is BaseException:
                    continue
                namespace = type.__getattribute__(
                    exception_type,
                    "__dict__",
                )
                for name, descriptor in namespace.items():
                    if (
                        type(name) is str
                        and name not in {"__dict__", "__weakref__"}
                        and type(descriptor)
                        in {GetSetDescriptorType, MemberDescriptorType}
                    ):
                        try:
                            pending.append(
                                descriptor.__get__(value, type(value))
                            )
                        except (AttributeError, TypeError):
                            pass
            if value.__traceback__ is not None:
                pending.append(value.__traceback__)
        elif type(value) is dict:
            pending.extend(value.keys())
            pending.extend(value.values())
        elif type(value) in {tuple, list, set, frozenset}:
            pending.extend(value)
        elif type(value).__name__ == "traceback":
            frame = value.tb_frame
            module = frame.f_globals.get("__name__", "")
            if module.startswith("wahojobs."):
                pending.extend(frame.f_locals.values())
            if value.tb_next is not None:
                pending.append(value.tb_next)
    return hits


class DurableGoogleLoginBrowserTests(unittest.TestCase):
    def setUp(self):
        self.harness = BrowserHarness()
        self.integration = self.harness.integration()

    def test_login_page_issues_strict_host_only_csrf_and_secure_headers(self):
        response = self.integration.handle("GET", LOGIN_ROUTE, headers())
        self.assertEqual(response.status, 200)
        self.assertIn(b"Continue with Google", response.body)
        self.assertIn(b"name='invitation'", response.body)
        self.assertIn(LOGIN_CSRF.encode(), response.body)
        self.assertEqual(
            set_cookies(response),
            (
                f"{LOGIN_CSRF_COOKIE_NAME}={LOGIN_CSRF}; Path=/; Max-Age=600; "
                "Secure; HttpOnly; SameSite=Strict",
            ),
        )
        response_headers = dict(response.headers)
        self.assertEqual(response_headers["Cache-Control"], "no-store")
        self.assertIn("form-action 'self'", response_headers["Content-Security-Policy"])
        self.assertNotIn("Domain=", repr(response.headers))
        self.assertNotIn(LOGIN_CSRF, repr(response))

    def test_start_requires_exact_same_origin_form_and_commits_before_url_access(self):
        body = f"csrf={LOGIN_CSRF}".encode()
        response = self.integration.handle(
            "POST",
            GOOGLE_LOGIN_START_ROUTE,
            headers(
                origin=True,
                cookie=f"{LOGIN_CSRF_COOKIE_NAME}={LOGIN_CSRF}",
                body=body,
            ),
            io.BytesIO(body),
        )
        self.assertEqual(response.status, 303)
        self.assertEqual(dict(response.headers)["Location"], AUTHORIZATION_URL)
        self.assertEqual(self.harness.prepare_calls, 1)
        self.assertLess(
            self.harness.events.index("prepare_returned"),
            self.harness.events.index("authorization_url_read"),
        )
        cookies = set_cookies(response)
        self.assertIn(
            f"{GOOGLE_TRANSACTION_COOKIE_NAME}={TRANSACTION_ID}; Path=/; "
            "Max-Age=600; Secure; HttpOnly; SameSite=Lax",
            cookies,
        )
        self.assertTrue(self.harness.connections[0].closed)

    def test_start_binds_only_one_strict_optional_invitation_and_clears_it(self):
        body = (
            f"csrf={LOGIN_CSRF}&invitation={INVITATION_CREDENTIAL}"
        ).encode("ascii")
        response = self.integration.handle(
            "POST",
            GOOGLE_LOGIN_START_ROUTE,
            headers(
                origin=True,
                cookie=f"{LOGIN_CSRF_COOKIE_NAME}={LOGIN_CSRF}",
                body=body,
            ),
            io.BytesIO(body),
        )
        self.assertEqual(response.status, 303)
        self.assertEqual(
            self.harness.prepare_invitations,
            [INVITATION_CREDENTIAL.encode("ascii")],
        )
        self.assertEqual(
            self.harness.prepare_invitation_buffers,
            [bytearray()],
        )
        public = repr(response) + repr(response.headers) + str(response.body)
        self.assertNotIn(INVITATION_CREDENTIAL, public)
        self.assertNotIn(
            INVITATION_CREDENTIAL,
            dict(response.headers)["Location"],
        )

        invalid_values = (
            "not-an-invitation",
            "inv_" + ("b" * 32) + "." + ("C" * 42),
            INVITATION_CREDENTIAL + "x",
            "x" * 129,
        )
        for value in invalid_values:
            with self.subTest(value_length=len(value)):
                harness = BrowserHarness()
                integration = harness.integration()
                candidate = f"csrf={LOGIN_CSRF}&invitation={value}".encode(
                    "ascii"
                )
                rejected = integration.handle(
                    "POST",
                    GOOGLE_LOGIN_START_ROUTE,
                    headers(
                        origin=True,
                        cookie=(
                            f"{LOGIN_CSRF_COOKIE_NAME}={LOGIN_CSRF}"
                        ),
                        body=candidate,
                    ),
                    io.BytesIO(candidate),
                )
                self.assertEqual(rejected.status, 403)
                self.assertEqual(harness.prepare_calls, 0)
                self.assertNotIn(value, str(rejected.body))

        duplicate_harness = BrowserHarness()
        duplicate_integration = duplicate_harness.integration()
        duplicate = (
            f"csrf={LOGIN_CSRF}&invitation={INVITATION_CREDENTIAL}"
            f"&invitation={INVITATION_CREDENTIAL}"
        ).encode("ascii")
        rejected_duplicate = duplicate_integration.handle(
            "POST",
            GOOGLE_LOGIN_START_ROUTE,
            headers(
                origin=True,
                cookie=f"{LOGIN_CSRF_COOKIE_NAME}={LOGIN_CSRF}",
                body=duplicate,
            ),
            io.BytesIO(duplicate),
        )
        self.assertEqual(rejected_duplicate.status, 403)
        self.assertEqual(duplicate_harness.prepare_calls, 0)
        self.assertNotIn(
            INVITATION_CREDENTIAL,
            str(rejected_duplicate.body),
        )

    def test_start_rejects_csrf_query_duplicates_and_unbounded_bodies_without_prepare(self):
        requests = (
            (
                GOOGLE_LOGIN_START_ROUTE,
                f"csrf={'x' * 43}".encode(),
                headers(
                    origin=True,
                    cookie=f"{LOGIN_CSRF_COOKIE_NAME}={LOGIN_CSRF}",
                    body=f"csrf={'x' * 43}".encode(),
                ),
            ),
            (
                GOOGLE_LOGIN_START_ROUTE + "?return_to=https://evil.test",
                f"csrf={LOGIN_CSRF}".encode(),
                headers(
                    origin=True,
                    cookie=f"{LOGIN_CSRF_COOKIE_NAME}={LOGIN_CSRF}",
                    body=f"csrf={LOGIN_CSRF}".encode(),
                ),
            ),
            (
                GOOGLE_LOGIN_START_ROUTE,
                f"csrf={LOGIN_CSRF}&csrf={LOGIN_CSRF}".encode(),
                headers(
                    origin=True,
                    cookie=f"{LOGIN_CSRF_COOKIE_NAME}={LOGIN_CSRF}",
                    body=f"csrf={LOGIN_CSRF}&csrf={LOGIN_CSRF}".encode(),
                ),
            ),
            (
                GOOGLE_LOGIN_START_ROUTE,
                b"x" * 1025,
                headers(
                    origin=True,
                    cookie=f"{LOGIN_CSRF_COOKIE_NAME}={LOGIN_CSRF}",
                    body=b"x" * 1025,
                ),
            ),
        )
        for target, body, request_headers in requests:
            with self.subTest(target=target, length=len(body)):
                response = self.integration.handle(
                    "POST",
                    target,
                    request_headers,
                    io.BytesIO(body),
                )
                self.assertIn(response.status, {400, 403})
        self.assertEqual(self.harness.prepare_calls, 0)

    def test_form_content_type_is_ascii_case_insensitive_but_otherwise_exact(self):
        body = f"csrf={LOGIN_CSRF}".encode()
        valid = (
            "application/x-www-form-urlencoded",
            "Application/X-WWW-Form-Urlencoded",
            "APPLICATION/X-WWW-FORM-URLENCODED",
        )
        for content_type in valid:
            with self.subTest(valid=content_type):
                harness = BrowserHarness()
                integration = harness.integration()
                request_headers = tuple(
                    (
                        name,
                        content_type if name == "Content-Type" else value,
                    )
                    for name, value in headers(
                        origin=True,
                        cookie=f"{LOGIN_CSRF_COOKIE_NAME}={LOGIN_CSRF}",
                        body=body,
                    )
                )
                response = integration.handle(
                    "POST",
                    GOOGLE_LOGIN_START_ROUTE,
                    request_headers,
                    io.BytesIO(body),
                )
                self.assertEqual(response.status, 303)
                self.assertEqual(harness.prepare_calls, 1)

        invalid = (
            ("alternate", "text/plain", ()),
            ("malformed missing subtype", "application", ()),
            (
                "malformed extra separator",
                "application//x-www-form-urlencoded",
                (),
            ),
            (
                "unicode lookalike",
                "applicati\u0131n/x-www-form-urlencoded",
                (),
            ),
            (
                "forbidden parameter",
                "application/x-www-form-urlencoded; charset=utf-8",
                (),
            ),
            (
                "leading whitespace",
                " application/x-www-form-urlencoded",
                (),
            ),
            (
                "embedded whitespace",
                "application /x-www-form-urlencoded",
                (),
            ),
            (
                "trailing whitespace",
                "application/x-www-form-urlencoded ",
                (),
            ),
            (
                "duplicate",
                "application/x-www-form-urlencoded",
                (("Content-Type", "application/x-www-form-urlencoded"),),
            ),
        )
        for label, content_type, extra in invalid:
            with self.subTest(invalid=label):
                harness = BrowserHarness()
                integration = harness.integration()
                request_headers = tuple(
                    (
                        name,
                        content_type if name == "Content-Type" else value,
                    )
                    for name, value in headers(
                        origin=True,
                        cookie=f"{LOGIN_CSRF_COOKIE_NAME}={LOGIN_CSRF}",
                        body=body,
                        extra=extra,
                    )
                )
                response = integration.handle(
                    "POST",
                    GOOGLE_LOGIN_START_ROUTE,
                    request_headers,
                    io.BytesIO(body),
                )
                self.assertIn(response.status, {400, 403})
                self.assertEqual(harness.prepare_calls, 0)

    def test_content_type_matching_invokes_no_string_subclass_hooks(self):
        class HookedContentType(str):
            calls = []

            def encode(self, *_args, **_kwargs):
                self.calls.append("encode")
                raise AssertionError("content_type_encode_hook_invoked")

            def lower(self):
                self.calls.append("lower")
                raise AssertionError("content_type_lower_hook_invoked")

            def casefold(self):
                self.calls.append("casefold")
                raise AssertionError("content_type_casefold_hook_invoked")

        body = f"csrf={LOGIN_CSRF}".encode()
        hooked = HookedContentType("Application/X-WWW-Form-Urlencoded")
        request_headers = tuple(
            (name, hooked if name == "Content-Type" else value)
            for name, value in headers(
                origin=True,
                cookie=f"{LOGIN_CSRF_COOKIE_NAME}={LOGIN_CSRF}",
                body=body,
            )
        )
        response = self.integration.handle(
            "POST",
            GOOGLE_LOGIN_START_ROUTE,
            request_headers,
            io.BytesIO(body),
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(HookedContentType.calls, [])
        self.assertEqual(self.harness.prepare_calls, 0)

    def test_host_origin_proxy_absolute_target_and_wrong_methods_fail_before_services(self):
        cases = (
            ("GET", LOGIN_ROUTE, (), 400),
            ("GET", LOGIN_ROUTE, (("Host", "evil.test"),), 400),
            (
                "GET",
                LOGIN_ROUTE,
                headers(extra=(("Host", AUTHORITY),)),
                400,
            ),
            (
                "GET",
                LOGIN_ROUTE,
                headers(extra=(("X-Forwarded-Surprise", "app.test"),)),
                400,
            ),
            (
                "GET",
                LOGIN_ROUTE,
                headers(extra=(("Forwarded", "host=app.test"),)),
                400,
            ),
            ("GET", f"{ORIGIN}{LOGIN_ROUTE}", headers(), 400),
            ("GET", "/not-an-auth-route", (("Host", "evil.test"),), 400),
            ("GET", "/not-an-auth-route", headers(), 404),
            (
                "POST",
                GOOGLE_LOGIN_START_ROUTE,
                headers(
                    cookie=f"{LOGIN_CSRF_COOKIE_NAME}={LOGIN_CSRF}",
                    body=b"csrf=" + LOGIN_CSRF.encode(),
                ),
                403,
            ),
            (
                "POST",
                GOOGLE_LOGIN_START_ROUTE,
                headers(
                    cookie=f"{LOGIN_CSRF_COOKIE_NAME}={LOGIN_CSRF}",
                    body=b"csrf=" + LOGIN_CSRF.encode(),
                    extra=(("Origin", "https://evil.test"),),
                ),
                400,
            ),
            (
                "POST",
                GOOGLE_LOGIN_START_ROUTE,
                headers(
                    origin=True,
                    cookie=f"{LOGIN_CSRF_COOKIE_NAME}={LOGIN_CSRF}",
                    body=b"csrf=" + LOGIN_CSRF.encode(),
                    extra=(("Origin", ORIGIN),),
                ),
                400,
            ),
            ("POST", LOGIN_ROUTE, headers(), 405),
            ("HEAD", LOGIN_ROUTE, headers(), 405),
            ("HEAD", GOOGLE_LOGIN_CALLBACK_ROUTE, headers(), 405),
            ("HEAD", LOGOUT_ROUTE, headers(), 405),
            ("PUT", LOGOUT_ROUTE, headers(), 405),
        )
        for method, target, request_headers, expected in cases:
            with self.subTest(method=method, target=target):
                response = self.integration.handle(method, target, request_headers)
                self.assertEqual(response.status, expected)
        self.assertEqual(self.harness.prepare_calls, 0)
        self.assertEqual(self.harness.complete_calls, [])
        self.assertEqual(self.harness.revoke_calls, [])

    def test_callback_uses_browser_transaction_binding_and_prepares_delivery_lease(self):
        target = GOOGLE_LOGIN_CALLBACK_ROUTE + "?code=code&state=state"
        response = self.integration.handle(
            "GET",
            target,
            headers(cookie=f"{GOOGLE_TRANSACTION_COOKIE_NAME}={TRANSACTION_ID}"),
        )
        self.assertEqual(response.status, 303)
        self.assertEqual(dict(response.headers)["Location"], AUTHENTICATED_DESTINATION)
        self.assertEqual(
            self.harness.complete_calls,
            [(ORIGIN + target, TRANSACTION_ID)],
        )
        cookies = set_cookies(response)
        self.assertIn(SESSION_COOKIE, cookies)
        self.assertIn(
            f"{SESSION_CSRF_COOKIE_NAME}={SESSION_CSRF}; Path=/; Max-Age=3600; "
            "Expires=Sat, 25 Jul 2026 15:00:00 GMT; "
            "Secure; HttpOnly; SameSite=Strict",
            cookies,
        )
        self.assertTrue(
            any(
                cookie.startswith(f"{GOOGLE_TRANSACTION_COOKIE_NAME}=;")
                for cookie in cookies
            )
        )
        connection = self.harness.connections[-1]
        lease = self.harness.delivery_leases[-1]
        self.assertEqual(self.harness.delivery_times, [NOW])
        self.assertLess(
            self.harness.events.index("complete_returned"),
            self.harness.events.index("browser_clock_read"),
        )
        self.assertFalse(connection.closed)
        self.assertEqual(self.integration.active_request_count, 1)
        self.assertFalse(self.integration.close())
        response.acknowledge_delivery()
        self.assertEqual((lease.acknowledged, lease.failed), (1, 0))
        self.assertTrue(connection.closed)
        self.assertEqual(self.integration.active_request_count, 0)
        self.assertTrue(self.integration.close())
        self.assertEqual(response.headers, ())
        self.assertIsNone(response._delivery_lease)
        self.assertIsNone(response._owned_connection)

    def test_post_completion_clock_failure_compensates_before_failure_response(self):
        self.harness.browser_clock_error = RuntimeError(
            "private post-provider clock failure"
        )
        response = self.integration.handle(
            "GET",
            GOOGLE_LOGIN_CALLBACK_ROUTE + "?code=code&state=state",
            headers(cookie=f"{GOOGLE_TRANSACTION_COOKIE_NAME}={TRANSACTION_ID}"),
        )
        self.assertEqual(response.status, 503)
        self.assertEqual(
            self.harness.delivery_times,
            [datetime(2026, 7, 25, 14, 59, 59, tzinfo=timezone.utc)],
        )
        self.assertEqual(
            (
                self.harness.delivery_leases[-1].acknowledged,
                self.harness.delivery_leases[-1].failed,
            ),
            (0, 1),
        )
        self.assertEqual(self.harness.discarded, self.harness.vaults)
        self.assertTrue(self.harness.connections[-1].closed)

    def test_callback_without_binding_still_terminally_calls_gateway_and_clears_cookie(self):
        self.harness.completion_status = "invalid_or_expired_transaction"
        target = GOOGLE_LOGIN_CALLBACK_ROUTE + "?code=code&state=state"
        response = self.integration.handle("GET", target, headers())
        self.assertEqual(response.status, 400)
        self.assertEqual(self.harness.complete_calls, [(ORIGIN + target, None)])
        self.assertEqual(self.harness.discarded, self.harness.vaults)
        self.assertTrue(self.harness.connections[-1].closed)
        self.assertTrue(
            any(
                value.startswith(f"{GOOGLE_TRANSACTION_COOKIE_NAME}=;")
                for value in set_cookies(response)
            )
        )

    def test_callback_failure_is_generic_and_never_delivers_credentials(self):
        marker = "private-provider-marker"
        for status, expected_status in (
            ("authentication_denied", 401),
            ("provider_unavailable", 503),
            ("unavailable", 503),
            ("invalid_or_expired_transaction", 400),
            ("already_completed", 400),
        ):
            with self.subTest(status=status):
                self.harness.completion_status = status
                response = self.integration.handle(
                    "GET",
                    GOOGLE_LOGIN_CALLBACK_ROUTE + "?error=" + marker + "&state=x",
                    headers(
                        cookie=f"{GOOGLE_TRANSACTION_COOKIE_NAME}={TRANSACTION_ID}"
                    ),
                )
                self.assertEqual(response.status, expected_status)
                self.assertNotIn(marker.encode(), response.body)
                self.assertNotIn(SESSION_COOKIE.encode(), response.body)
        self.assertEqual(self.harness.delivery_leases, [])

    def test_invalid_delivery_output_fails_lease_discards_vault_and_closes_connection(self):
        self.harness.delivery_cookie = "wahojobs_session=unsafe"
        response = self.integration.handle(
            "GET",
            GOOGLE_LOGIN_CALLBACK_ROUTE + "?code=code&state=state",
            headers(cookie=f"{GOOGLE_TRANSACTION_COOKIE_NAME}={TRANSACTION_ID}"),
        )
        self.assertEqual(response.status, 503)
        self.assertEqual(self.harness.delivery_leases[-1].failed, 1)
        self.assertEqual(self.harness.discarded, self.harness.vaults)
        self.assertTrue(self.harness.connections[-1].closed)

    def test_control_flow_after_vault_creation_discards_and_closes_before_reraise(self):
        self.harness.complete_error = KeyboardInterrupt()
        try:
            self.integration.handle(
                "GET",
                GOOGLE_LOGIN_CALLBACK_ROUTE + "?code=code&state=state",
                headers(
                    cookie=f"{GOOGLE_TRANSACTION_COOKIE_NAME}={TRANSACTION_ID}"
                ),
            )
        except KeyboardInterrupt as exc:
            traceback = exc.__traceback__
            browser_frames = []
            while traceback is not None:
                if (
                    traceback.tb_frame.f_globals.get("__name__")
                    == "wahojobs.durable_google_login_browser"
                ):
                    browser_frames.append(dict(traceback.tb_frame.f_locals))
                traceback = traceback.tb_next
            self.assertEqual(len(browser_frames), 1)
            for name in ("self", "method", "target", "headers", "body_stream"):
                self.assertIsNone(browser_frames[0].get(name))
            rendered = repr(browser_frames[0])
            self.assertNotIn(TRANSACTION_ID, rendered)
            self.assertNotIn("code=code", rendered)
        else:
            self.fail("control flow did not propagate")
        self.assertEqual(self.harness.discarded, self.harness.vaults)
        self.assertTrue(self.harness.connections[-1].closed)

    def test_dependency_exception_graphs_are_scrubbed_at_four_browser_boundaries(self):
        failure_kinds = (
            "ordinary",
            "custom",
            "cause",
            "context",
            "notes",
            "hostile_sanitizer",
            "KeyboardInterrupt",
            "SystemExit",
            "GeneratorExit",
        )
        control_kinds = {
            "KeyboardInterrupt",
            "SystemExit",
            "GeneratorExit",
        }
        for boundary in ("start", "callback", "logout_page", "logout"):
            for kind in failure_kinds:
                with self.subTest(boundary=boundary, kind=kind):
                    harness = BrowserHarness()
                    integration = harness.integration()
                    secret = f"private-{boundary}-{kind}"
                    protected = [
                        secret,
                        LOGIN_CSRF,
                        SESSION_TOKEN,
                        SESSION_CSRF,
                        TRANSACTION_ID,
                        object(),
                    ]
                    retained = []

                    def fail(*args, **kwargs):
                        protected.extend(args)
                        protected.extend(kwargs.values())
                        raise_browser_failure(kind, protected, retained)

                    if boundary == "start":
                        integration._prepare_authorization = fail
                        body = f"csrf={LOGIN_CSRF}".encode()
                        invoke = lambda: integration.handle(
                            "POST",
                            GOOGLE_LOGIN_START_ROUTE,
                            headers(
                                origin=True,
                                cookie=(
                                    f"{LOGIN_CSRF_COOKIE_NAME}={LOGIN_CSRF}"
                                ),
                                body=body,
                                extra=(("X-Private-Request", secret),),
                            ),
                            io.BytesIO(body),
                        )
                        expected_status = 503
                    elif boundary == "callback":
                        integration._complete_authorization = fail
                        invoke = lambda: integration.handle(
                            "GET",
                            GOOGLE_LOGIN_CALLBACK_ROUTE
                            + f"?code={secret}&state=state",
                            headers(
                                cookie=(
                                    f"{GOOGLE_TRANSACTION_COOKIE_NAME}="
                                    f"{TRANSACTION_ID}"
                                ),
                                extra=(("X-Private-Request", secret),),
                            ),
                        )
                        expected_status = 503
                    else:
                        cookie = (
                            f"wahojobs_session={SESSION_TOKEN}; "
                            f"{SESSION_CSRF_COOKIE_NAME}={SESSION_CSRF}"
                        )
                        if boundary == "logout_page":
                            integration._validate_logout = fail
                            invoke = lambda: integration.handle(
                                "GET",
                                LOGOUT_ROUTE,
                                headers(
                                    cookie=cookie,
                                    extra=(("X-Private-Request", secret),),
                                ),
                            )
                            expected_status = 401
                        else:
                            integration._revoke_logout = fail
                            body = f"csrf={SESSION_CSRF}".encode()
                            invoke = lambda: integration.handle(
                                "POST",
                                LOGOUT_ROUTE,
                                headers(
                                    origin=True,
                                    cookie=cookie,
                                    body=body,
                                    extra=(("X-Private-Request", secret),),
                                ),
                                io.BytesIO(body),
                            )
                            expected_status = 403

                    log_output = io.StringIO()
                    log_handler = logging.StreamHandler(log_output)
                    root_logger = logging.getLogger()
                    root_logger.addHandler(log_handler)
                    try:
                        with warnings.catch_warnings(record=True) as warning_records:
                            warnings.simplefilter("always")
                            if kind in control_kinds:
                                try:
                                    invoke()
                                except BaseException as propagated:
                                    self.assertIs(propagated, retained[-1])
                                    public = b""
                                else:
                                    self.fail(
                                        "control-flow exception did not propagate"
                                    )
                            else:
                                response = invoke()
                                self.assertEqual(response.status, expected_status)
                                public = (
                                    response.body
                                    + repr(response.headers).encode("utf-8")
                                    + repr(response).encode("utf-8")
                                )
                    finally:
                        root_logger.removeHandler(log_handler)
                    public += repr(warning_records).encode("utf-8")
                    public += log_output.getvalue().encode("utf-8")
                    for value in protected:
                        if type(value) is str:
                            self.assertNotIn(value.encode("utf-8"), public)

                    self.assertTrue(retained)
                    for error in retained:
                        self.assertEqual(
                            exception_graph_protected_hits(error, protected),
                            [],
                        )
                        self.assertEqual(error.args, ())
                        if kind not in control_kinds:
                            self.assertIsNone(error.__traceback__)
                        self.assertIsNone(error.__cause__)
                        self.assertIsNone(error.__context__)
                        self.assertTrue(error.__suppress_context__)
                        self.assertEqual(getattr(error, "__notes__", ()), ())
                    self.assertTrue(harness.connections[-1].closed)
                    if boundary == "callback":
                        self.assertEqual(harness.discarded, harness.vaults)

    def test_other_caught_browser_dependencies_scrub_retained_exceptions(self):
        cases = ("token_factory", "header_iteration", "body_read")
        for case in cases:
            with self.subTest(case=case):
                harness = BrowserHarness()
                integration = harness.integration()
                protected = [f"private-{case}", object()]
                retained = []

                def fail(*args, **kwargs):
                    protected.extend(args)
                    protected.extend(kwargs.values())
                    raise_browser_failure("custom", protected, retained)

                if case == "token_factory":
                    integration._token_factory = fail
                    response = integration.handle(
                        "GET",
                        LOGIN_ROUTE,
                        headers(extra=(("X-Private-Request", protected[0]),)),
                    )
                    expected = 503
                elif case == "header_iteration":
                    class FailingHeaders:
                        raw_items = fail

                    response = integration.handle(
                        "GET",
                        LOGIN_ROUTE,
                        FailingHeaders(),
                    )
                    expected = 400
                else:
                    class FailingBody:
                        read = fail

                    body = f"csrf={LOGIN_CSRF}".encode()
                    protected.append(LOGIN_CSRF)
                    response = integration.handle(
                        "POST",
                        GOOGLE_LOGIN_START_ROUTE,
                        headers(
                            origin=True,
                            cookie=f"{LOGIN_CSRF_COOKIE_NAME}={LOGIN_CSRF}",
                            body=body,
                            extra=(("X-Private-Request", protected[0]),),
                        ),
                        FailingBody(),
                    )
                    expected = 403
                self.assertEqual(response.status, expected)
                self.assertTrue(retained)
                self.assertEqual(
                    exception_graph_protected_hits(retained[0], protected),
                    [],
                )

    def test_exception_sanitizer_fallback_fails_closed_at_start_boundary(self):
        protected = ["private-sanitizer-fallback", object()]
        failure = HostileRetainingBrowserFailure(*protected)

        def fail(*_args, **_kwargs):
            raise failure

        self.integration._prepare_authorization = fail
        body = f"csrf={LOGIN_CSRF}".encode()
        with mock.patch.object(
            browser_module,
            "_detach_browser_exception_graph",
            side_effect=RuntimeError("closed sanitizer fault"),
        ):
            response = self.integration.handle(
                "POST",
                GOOGLE_LOGIN_START_ROUTE,
                headers(
                    origin=True,
                    cookie=f"{LOGIN_CSRF_COOKIE_NAME}={LOGIN_CSRF}",
                    body=body,
                ),
                io.BytesIO(body),
            )

        self.assertEqual(response.status, 503)
        self.assertNotIn(protected[0].encode("utf-8"), response.body)
        self.assertEqual(
            exception_graph_protected_hits(failure, protected),
            [],
        )
        self.assertIsNone(failure.retained)
        self.assertEqual(failure.args, ())
        self.assertFalse(getattr(failure, "__notes__", ()))
        self.assertIsNone(failure.__traceback__)
        self.assertTrue(self.harness.connections[-1].closed)

    def test_delivery_failure_hook_compensates_and_closes_connection(self):
        response = self.integration.handle(
            "GET",
            GOOGLE_LOGIN_CALLBACK_ROUTE + "?code=code&state=state",
            headers(cookie=f"{GOOGLE_TRANSACTION_COOKIE_NAME}={TRANSACTION_ID}"),
        )
        connection = self.harness.connections[-1]
        lease = self.harness.delivery_leases[-1]
        response.fail_delivery()
        self.assertEqual((lease.acknowledged, lease.failed), (0, 1))
        self.assertTrue(connection.closed)
        self.assertEqual(response.headers, ())
        self.assertIsNone(response._delivery_lease)
        self.assertIsNone(response._owned_connection)
        with self.assertRaises(RuntimeError):
            response.acknowledge_delivery()

    def test_response_acknowledgement_failure_never_invokes_compensation(self):
        class FailingAcknowledgementLease(FakeDeliveryLease):
            def acknowledge_delivery(self):
                self.acknowledged += 1
                raise RuntimeError("post-delivery acknowledgement failed")

        connection = FakeConnection()
        lease = FailingAcknowledgementLease()
        response = DurableGoogleLoginBrowserResponse(
            status=303,
            body=b"",
            headers=(("Content-Length", "0"),),
            _delivery_lease=lease,
            _owned_connection=connection,
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "post-delivery acknowledgement failed",
        ):
            response.acknowledge_delivery()
        self.assertEqual((lease.acknowledged, lease.failed), (1, 0))
        self.assertTrue(connection.closed)
        self.assertEqual(response.headers, ())
        self.assertIsNone(response._delivery_lease)
        self.assertIsNone(response._owned_connection)
        with self.assertRaisesRegex(
            RuntimeError,
            "browser_response_delivery_already_terminal",
        ):
            response.fail_delivery()

    def test_logout_requires_durable_csrf_and_clears_exact_cookies(self):
        cookie = (
            f"{SESSION_COOKIE.split('=', 1)[0]}={SESSION_TOKEN}; "
            f"{SESSION_CSRF_COOKIE_NAME}={SESSION_CSRF}"
        )
        page = self.integration.handle(
            "GET",
            LOGOUT_ROUTE,
            headers(cookie=cookie),
        )
        self.assertEqual(page.status, 200)
        self.assertIn(SESSION_CSRF.encode(), page.body)
        self.assertEqual(len(self.harness.validate_calls), 1)

        body = f"csrf={SESSION_CSRF}".encode()
        response = self.integration.handle(
            "POST",
            LOGOUT_ROUTE,
            headers(origin=True, cookie=cookie, body=body),
            io.BytesIO(body),
        )
        self.assertEqual(response.status, 303)
        self.assertEqual(dict(response.headers)["Location"], LOGIN_ROUTE)
        self.assertEqual(len(self.harness.revoke_calls), 1)
        cookies = set_cookies(response)
        self.assertTrue(any(value.startswith("wahojobs_session=;") for value in cookies))
        self.assertIn(
            f"{SESSION_CSRF_COOKIE_NAME}=; Path=/; Max-Age=0; "
            "Expires=Thu, 01 Jan 1970 00:00:00 GMT; "
            "Secure; HttpOnly; SameSite=Strict",
            cookies,
        )

        rejected_body = f"csrf={'z' * 43}".encode()
        rejected = self.integration.handle(
            "POST",
            LOGOUT_ROUTE,
            headers(origin=True, cookie=cookie, body=rejected_body),
            io.BytesIO(rejected_body),
        )
        self.assertEqual(rejected.status, 403)
        self.assertEqual(len(self.harness.revoke_calls), 1)

    def test_profile_route_is_owned_and_delegated_without_legacy_fallthrough(self):
        response = self.integration.handle(
            "GET",
            AUTHENTICATED_DESTINATION + "?before=2",
            headers(),
        )
        self.assertIs(response, self.harness.profile.response)
        self.assertEqual(
            self.harness.profile.calls[0][:2],
            ("GET", AUTHENTICATED_DESTINATION + "?before=2"),
        )
        self.assertTrue(self.integration.matches_route(AUTHENTICATED_DESTINATION))

    def test_invalid_configuration_and_unknown_routes_fail_closed(self):
        with self.assertRaises(ValueError):
            DurableGoogleLoginBrowserIntegration(
                public_origin="http://app.test",
                profile_integration=self.harness.profile,
                connection_factory=self.harness.connection_factory,
                gateway=GATEWAY,
                key_authority=KEY_AUTHORITY,
                completion_policy=COMPLETION_POLICY,
                request_secret_vault_factory=self.harness.vault_factory,
                prepare_session_delivery=self.harness.prepare_delivery,
                discard_request_secret_vault=self.harness.discard,
                validate_logout=self.harness.validate_logout,
                revoke_logout=self.harness.revoke_logout,
                prepare_authorization=self.harness.prepare,
                complete_authorization=self.harness.complete,
            )
        response = self.integration.handle("GET", "/find-matches", headers())
        self.assertEqual(response.status, 404)
        self.assertFalse(self.integration.matches_route("/find-matches"))

    def test_close_gate_blocks_new_requests_and_retries_active_request(self):
        entered = threading.Event()
        release = threading.Event()
        original_handle = self.harness.profile.handle

        def blocking_handle(method, target, request_headers):
            entered.set()
            release.wait(2)
            return original_handle(method, target, request_headers)

        self.harness.profile.handle = blocking_handle
        responses = []
        worker = threading.Thread(
            target=lambda: responses.append(
                self.integration.handle(
                    "GET",
                    AUTHENTICATED_DESTINATION,
                    headers(),
                )
            )
        )
        worker.start()
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.integration.active_request_count, 1)
        self.assertFalse(self.integration.close())
        self.assertFalse(self.integration.matches_route(LOGIN_ROUTE))

        rejected = self.integration.handle(
            "GET",
            LOGIN_ROUTE,
            headers(),
        )
        self.assertEqual(rejected.status, 503)

        release.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.integration.active_request_count, 0)
        self.assertTrue(self.integration.close())
        self.assertTrue(self.integration.closed)
        self.assertTrue(self.integration.close())
        self.assertEqual(len(responses), 1)


if __name__ == "__main__":
    unittest.main()
