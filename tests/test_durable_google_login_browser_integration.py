from contextlib import ExitStack
from datetime import timedelta
import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import socket
import sqlite3
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import urlsplit

from scripts.durable_google_login_fixture_demo import (
    FIXTURE_COMPLETE_ROUTE,
    _ControlledProviderBridge,
)
from tests.accounts_test_support import INVITATION_KEY
from tests.google_oidc_authorization_transactions_test_support import (
    LOOKUP_KEY_MATERIAL,
    PROTECTION_KEY_MATERIAL,
)
import tests.durable_google_login_browser_test_support as browser_test_support
from tests.durable_google_login_browser_test_support import (
    FreshBrowserLoginWorker,
    cookie_header,
    cookie_values,
    form_body,
    https_request,
    loopback_and_in_memory_provider_only,
    process_native_handle_count,
    provider_callback_for,
    running_https_browser_app,
    temporary_browser_login_state,
    warm_spawn_process_accounting,
)
from tests.google_oidc_gateway_test_support import (
    ManualClock,
    NOW,
    make_real_gateway,
)
from wahojobs.durable_google_login_runtime import (
    build_durable_google_login_runtime,
)
from wahojobs import accounts


class _B22PreparedAuthorizationUrl:
    __slots__ = ("authorization_url",)

    def __init__(self, authorization_url):
        self.authorization_url = authorization_url


def _b22_response_values(response, name):
    lowered = name.casefold()
    return tuple(
        value
        for candidate, value in response.headers
        if candidate.casefold() == lowered
    )


def _b22_response_cookies(response):
    result = {}
    for header in _b22_response_values(response, "Set-Cookie"):
        pair = header.split(";", 1)[0]
        name, value = pair.split("=", 1)
        result[name] = value
    return result


def _b22_response_summary(response):
    cookie_headers = _b22_response_values(response, "Set-Cookie")
    locations = _b22_response_values(response, "Location")
    return {
        "status": response.status,
        "profile_location": locations == ("/account/profile",),
        "session_cookie_count": sum(
            value.startswith("wahojobs_session=")
            for value in cookie_headers
        ),
        "csrf_cookie_count": sum(
            value.startswith("__Host-wahojobs_session_csrf=")
            for value in cookie_headers
        ),
        "transaction_clear_count": sum(
            value.startswith("__Host-wahojobs_google_tx=;")
            for value in cookie_headers
        ),
        "generic_failure": (
            (
                response.status == 400
                and b"Sign-in not completed" in response.body
                and b"This sign-in request is no longer valid"
                in response.body
            )
            or (
                response.status == 503
                and b"Sign-in temporarily unavailable" in response.body
                and b"Sign-in could not be completed safely"
                in response.body
            )
        ),
    }


def _b22_begin_login(browser, *, authority, public_origin):
    login = browser.handle(
        "GET",
        "/login",
        (("Host", authority),),
    )
    login_cookies = _b22_response_cookies(login)
    csrf = login_cookies["__Host-wahojobs_login_csrf"]
    login.acknowledge_delivery()
    body = form_body(csrf=csrf)
    start = browser.handle(
        "POST",
        "/auth/google/start",
        (
            ("Host", authority),
            ("Origin", public_origin),
            ("Sec-Fetch-Site", "same-origin"),
            ("Content-Type", "application/x-www-form-urlencoded"),
            ("Content-Length", str(len(body))),
            (
                "Cookie",
                cookie_header(
                    {"__Host-wahojobs_login_csrf": csrf}
                ),
            ),
        ),
        io.BytesIO(body),
    )
    locations = _b22_response_values(start, "Location")
    start_cookies = _b22_response_cookies(start)
    if start.status != 303 or len(locations) != 1:
        raise AssertionError("b22_start_failed")
    result = (
        locations[0],
        start_cookies["__Host-wahojobs_google_tx"],
    )
    start.acknowledge_delivery()
    return result


def _b22_complete_login(
    browser,
    harness,
    *,
    authority,
    public_origin,
    provider_url,
    transaction_cookie,
    code,
):
    callback_url = harness.transport.callback_for(
        _B22PreparedAuthorizationUrl(provider_url),
        code=code,
        base_uri=public_origin + "/auth/google/callback",
    )
    callback_parts = urlsplit(callback_url)
    callback = browser.handle(
        "GET",
        callback_parts.path + "?" + callback_parts.query,
        (
            ("Host", authority),
            (
                "Cookie",
                cookie_header(
                    {
                        "__Host-wahojobs_google_tx": (
                            transaction_cookie
                        )
                    }
                ),
            ),
        ),
    )
    summary = _b22_response_summary(callback)
    callback.acknowledge_delivery()
    return summary


def _b22_database_snapshot(database_path):
    connection = sqlite3.connect(database_path, timeout=2.0)
    try:
        rows = connection.execute(
            "SELECT transaction_id, lifecycle, row_version, "
            "lookup_key_version, protection_key_version FROM "
            "google_oidc_authorization_transactions "
            "ORDER BY transaction_id"
        ).fetchall()
        session_count = connection.execute(
            "SELECT COUNT(*) FROM account_sessions"
        ).fetchone()[0]
    finally:
        connection.close()
    return {
        "rows": [list(row) for row in rows],
        "session_count": session_count,
    }


def _b22_rotated_browser_worker_main(connection):
    runtime = None
    manager = None
    harnesses = []
    egress_stack = ExitStack()
    egress_closed = False
    result = None
    failure = None
    cleanup = None
    try:
        request = browser_test_support._b21_receive_message(connection)
        if (
            set(request)
            != {
                "mode",
                "configuration_path",
                "database_path",
                "subject",
                "provider_url",
                "transaction_cookie",
                "expected_seed",
            }
            or request["mode"]
            not in {"complete_and_start", "callback_only"}
            or any(
                type(request[name]) is not str
                for name in (
                    "configuration_path",
                    "database_path",
                    "subject",
                    "provider_url",
                    "transaction_cookie",
                    "expected_seed",
                )
            )
            or os.environ.get("PYTHONHASHSEED")
            != request["expected_seed"]
            or os.environ.get("PYTHONDONTWRITEBYTECODE") != "1"
        ):
            raise AssertionError("invalid_b22_worker_request")

        egress_stack.enter_context(
            loopback_and_in_memory_provider_only()
        )
        clock = ManualClock(NOW)

        def gateway_factory(configuration, client_secret):
            harness = make_real_gateway(
                clock=clock,
                client_id=configuration.google_client_id,
                client_secret=client_secret,
                redirect_uri=configuration.google_redirect_uri,
                subject=request["subject"],
            )
            harnesses.append(harness)
            return harness.gateway

        runtime = build_durable_google_login_runtime(
            request["configuration_path"],
            _clock=clock,
            _gateway_factory=gateway_factory,
        )
        manager = object.__getattribute__(runtime, "_connections")
        epoch = object.__getattribute__(manager, "_process_epoch")
        browser = runtime.browser_integration
        public_origin = runtime.configuration.public_origin
        authority = urlsplit(public_origin).netloc
        old_callback = _b22_complete_login(
            browser,
            harnesses[0],
            authority=authority,
            public_origin=public_origin,
            provider_url=request["provider_url"],
            transaction_cookie=request["transaction_cookie"],
            code="b22-old-code",
        )
        result = {
            "old_callback": old_callback,
            "after_old_provider_calls": (
                harnesses[0].transport.call_count
            ),
            "after_old_token_requests": (
                harnesses[0].transport.token_request_count
            ),
            "after_old_jwks_requests": (
                harnesses[0].transport.jwks_request_count
            ),
        }
        if request["mode"] == "complete_and_start":
            provider_url, transaction_cookie = _b22_begin_login(
                browser,
                authority=authority,
                public_origin=public_origin,
            )
            after_new_start = _b22_database_snapshot(
                request["database_path"]
            )
            after_new_start_provider_calls = (
                harnesses[0].transport.call_count
            )
            after_new_start_token_requests = (
                harnesses[0].transport.token_request_count
            )
            after_new_start_jwks_requests = (
                harnesses[0].transport.jwks_request_count
            )
            new_callback = _b22_complete_login(
                browser,
                harnesses[0],
                authority=authority,
                public_origin=public_origin,
                provider_url=provider_url,
                transaction_cookie=transaction_cookie,
                code="b22-new-code",
            )
            result.update(
                {
                    "after_new_start": after_new_start,
                    "after_new_start_provider_calls": (
                        after_new_start_provider_calls
                    ),
                    "after_new_start_token_requests": (
                        after_new_start_token_requests
                    ),
                    "after_new_start_jwks_requests": (
                        after_new_start_jwks_requests
                    ),
                    "new_transaction_cookie": transaction_cookie,
                    "new_callback": new_callback,
                }
            )
        result.update(
            {
                "final_database": _b22_database_snapshot(
                    request["database_path"]
                ),
                "provider_calls": harnesses[0].transport.call_count,
                "token_requests": (
                    harnesses[0].transport.token_request_count
                ),
                "jwks_requests": (
                    harnesses[0].transport.jwks_request_count
                ),
                "pid": os.getpid(),
                "start_method": multiprocessing.get_start_method(),
                "epoch_fingerprint": hashlib.sha256(
                    epoch.proof
                ).hexdigest(),
            }
        )
    except BaseException as error:
        failure = error
    finally:
        if runtime is not None:
            try:
                report = runtime.close(_preserve_primary=failure is not None)
                if report.cleanup_complete is not True:
                    raise AssertionError("b22_runtime_cleanup_incomplete")
                runtime = None
            except BaseException as error:
                if failure is None:
                    failure = error
        for harness in reversed(harnesses):
            try:
                harness.close()
            except BaseException as error:
                if failure is None:
                    failure = error
        harnesses.clear()
        try:
            egress_stack.close()
            egress_closed = True
        except BaseException as error:
            if failure is None:
                failure = error
        if manager is not None:
            try:
                cleanup = {
                    "cleanup_complete": failure is None,
                    "manager_closed": manager.closed,
                    "manager_records": len(
                        object.__getattribute__(manager, "_records")
                    ),
                    "egress_closed": egress_closed,
                }
            except BaseException as error:
                if failure is None:
                    failure = error
        try:
            if failure is None:
                browser_test_support._b21_send_message(
                    connection,
                    {"kind": "complete", **result, **cleanup},
                )
            else:
                browser_test_support._b21_send_message(
                    connection,
                    {
                        "kind": "failure",
                        "exception_type": type(failure).__name__,
                    },
                )
        except BaseException as error:
            if failure is None:
                failure = error
        try:
            connection.close()
        except BaseException as error:
            if failure is None:
                failure = error
    if failure is not None:
        raise SystemExit(2)


class _B22FreshBrowserLoginWorker(FreshBrowserLoginWorker):
    __slots__ = ()

    def __init__(self, request):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(
            target=_b22_rotated_browser_worker_main,
            args=(child,),
            name="durable-google-login-b22-worker",
            daemon=False,
        )
        self._connection = parent
        self._process = process
        self._terminal = False
        try:
            process.start()
        except BaseException:
            parent.close()
            child.close()
            raise
        child.close()
        try:
            browser_test_support._b21_send_message(parent, request)
        except BaseException:
            self.kill_and_reap()
            raise


class DurableGoogleLoginBrowserIntegrationTests(unittest.TestCase):
    def start_application(self, stack, **state_options):
        state = stack.enter_context(
            temporary_browser_login_state(**state_options)
        )
        runtime = build_durable_google_login_runtime(
            state.configuration_path,
            _clock=state.clock,
            _gateway_factory=state.gateway_factory,
        )
        stack.callback(runtime.close)
        stack.enter_context(loopback_and_in_memory_provider_only())
        stack.enter_context(running_https_browser_app(runtime))
        return state

    def request_form(self, state, path, values, cookies):
        body = form_body(**values)
        return https_request(
            state,
            "POST",
            path,
            headers=(
                ("Origin", state.public_origin),
                ("Sec-Fetch-Site", "same-origin"),
                ("Content-Type", "application/x-www-form-urlencoded"),
                ("Content-Length", str(len(body))),
                ("Cookie", cookie_header(cookies)),
            ),
            body=body,
        )

    @staticmethod
    def update_cookies(cookies, response):
        for name, value in cookie_values(response).items():
            if value:
                cookies[name] = value
            else:
                cookies.pop(name, None)

    def begin_login(self, state, *, invitation=None):
        cookies = {}
        login = https_request(state, "GET", "/login")
        self.assertEqual(login.status, 200)
        self.assertIn(b"Continue with Google", login.body)
        self.update_cookies(cookies, login)
        csrf = cookies["__Host-wahojobs_login_csrf"]

        values = {"csrf": csrf}
        if invitation is not None:
            values["invitation"] = invitation
        start = self.request_form(
            state,
            "/auth/google/start",
            values,
            cookies,
        )
        self.assertEqual(start.status, 303)
        self.assertEqual(len(start.header_values("Location")), 1)
        provider_url = start.header_values("Location")[0]
        self.assertTrue(
            provider_url.startswith(
                "https://accounts.google.com/o/oauth2/v2/auth?"
            )
        )
        self.update_cookies(cookies, start)
        self.assertNotIn("__Host-wahojobs_login_csrf", cookies)
        self.assertRegex(
            cookies["__Host-wahojobs_google_tx"],
            r"^oidctx_[0-9a-f]{32}$",
        )
        return cookies, provider_url, start

    def test_invited_first_login_restarts_then_later_login_reuses_account(self):
        email = "private-beta-invite@example.test"
        preserved_tables = (
            "product_principals",
            "legacy_owner_aliases",
            "principal_account_bindings",
            "ownership_binding_events",
            "product_profiles",
            "product_profile_revisions",
            "product_profile_sources",
            "user_pipeline_items",
            "user_pipeline_state",
            "user_pipeline_transitions",
        )
        with ExitStack() as stack:
            state = stack.enter_context(
                temporary_browser_login_state(
                    seed_existing_identity=False,
                    enable_invited_provisioning=True,
                )
            )
            stack.enter_context(loopback_and_in_memory_provider_only())
            connection = sqlite3.connect(state.database_path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                invitation = accounts.create_invitation(
                    connection,
                    email=email,
                    lookup_key=INVITATION_KEY,
                    expires_at=NOW + timedelta(days=7),
                    created_by="b23b_integration_operator",
                    idempotency_key="b23b-integration-invitation",
                    now=NOW,
                )
                preserved_before = {
                    table: connection.execute(
                        f'SELECT COUNT(*) FROM "{table}"'
                    ).fetchone()[0]
                    for table in preserved_tables
                }
            finally:
                connection.close()

            first_runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            with running_https_browser_app(first_runtime):
                cookies, provider_url, start = self.begin_login(
                    state,
                    invitation=invitation.invitation_token,
                )
            first_transaction_cookie = cookies[
                "__Host-wahojobs_google_tx"
            ]
            self.assertNotIn(invitation.invitation_token, provider_url)
            self.assertNotIn(
                invitation.invitation_token,
                repr(start.headers) + repr(start.body),
            )
            first_cleanup = first_runtime.close()
            self.assertTrue(first_cleanup.cleanup_complete)
            state.close_harnesses()

            second_runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            try:
                with running_https_browser_app(second_runtime):
                    callback_url = provider_callback_for(
                        state,
                        provider_url,
                        code="b23b-first-login",
                        claims_overrides={
                            "email": email,
                            "email_verified": True,
                        },
                    )
                    callback_parts = urlsplit(callback_url)
                    callback_path = (
                        callback_parts.path + "?" + callback_parts.query
                    )
                    callback = https_request(
                        state,
                        "GET",
                        callback_path,
                        headers=(("Cookie", cookie_header(cookies)),),
                    )
                    self.assertEqual(callback.status, 303)
                    self.assertEqual(
                        callback.header_values("Location"),
                        ("/account/profile",),
                    )
                    callback_public = (
                        repr(callback.headers) + repr(callback.body)
                    )
                    self.assertNotIn(
                        invitation.invitation_token,
                        callback_public,
                    )
                    self.update_cookies(cookies, callback)
                    self.assertRegex(
                        cookies["wahojobs_session"],
                        r"^[A-Za-z0-9_-]{43}$",
                    )
                    self.assertRegex(
                        cookies["__Host-wahojobs_session_csrf"],
                        r"^[A-Za-z0-9_-]{43}$",
                    )

                    later_cookies, later_provider_url, _later_start = (
                        self.begin_login(state)
                    )
                    later_callback_url = provider_callback_for(
                        state,
                        later_provider_url,
                        code="b23b-later-login",
                        missing_claims=("email", "email_verified"),
                    )
                    later_parts = urlsplit(later_callback_url)
                    later = https_request(
                        state,
                        "GET",
                        later_parts.path + "?" + later_parts.query,
                        headers=(
                            (
                                "Cookie",
                                cookie_header(later_cookies),
                            ),
                        ),
                    )
                    self.assertEqual(later.status, 303)
                    self.assertEqual(
                        later.header_values("Location"),
                        ("/account/profile",),
                    )

                    replay = https_request(
                        state,
                        "GET",
                        callback_path,
                        headers=(
                            (
                                "Cookie",
                                cookie_header(
                                    {
                                        "__Host-wahojobs_google_tx": (
                                            first_transaction_cookie
                                        )
                                    }
                                ),
                            ),
                        ),
                    )
                    self.assertIn(replay.status, {400, 410})

                observations = tuple(
                    harness.transport.observations
                    for harness in state.gateway_harnesses
                )
                connection = sqlite3.connect(state.database_path)
                connection.row_factory = sqlite3.Row
                try:
                    identity_rows = connection.execute(
                        "SELECT auth_identity_id, user_id, verified_email, "
                        "email_verified FROM auth_identities "
                        "WHERE provider = 'google' AND provider_subject = ?",
                        (state.subject,),
                    ).fetchall()
                    self.assertEqual(len(identity_rows), 1)
                    identity = identity_rows[0]
                    account_id = identity["user_id"]
                    self.assertEqual(identity["verified_email"], email)
                    self.assertEqual(identity["email_verified"], 1)
                    self.assertEqual(
                        connection.execute(
                            "SELECT lifecycle_status FROM users "
                            "WHERE user_id = ?",
                            (account_id,),
                        ).fetchone()[0],
                        "active",
                    )
                    self.assertEqual(
                        tuple(
                            connection.execute(
                                "SELECT invitation_status, "
                                "consumed_by_user_id FROM "
                                "account_invitations WHERE invitation_id = ?",
                                (invitation.invitation.invitation_id,),
                            ).fetchone()
                        ),
                        ("consumed", account_id),
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM users"
                        ).fetchone()[0],
                        1,
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM auth_identities"
                        ).fetchone()[0],
                        1,
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM account_lifecycle_events "
                            "WHERE user_id = ?",
                            (account_id,),
                        ).fetchone()[0],
                        1,
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM account_sessions "
                            "WHERE user_id = ?",
                            (account_id,),
                        ).fetchone()[0],
                        2,
                    )
                    self.assertEqual(
                        {
                            table: connection.execute(
                                f'SELECT COUNT(*) FROM "{table}"'
                            ).fetchone()[0]
                            for table in preserved_tables
                        },
                        preserved_before,
                    )
                finally:
                    connection.close()
                secret_text = invitation.invitation_token
                self.assertNotIn(
                    secret_text,
                    repr(observations)
                    + repr(cookies)
                    + repr(later_cookies),
                )
            finally:
                second_cleanup = second_runtime.close()
                self.assertTrue(second_cleanup.cleanup_complete)
                state.close_harnesses()
            self.assertNotIn(
                invitation.invitation_token.encode("ascii"),
                state.database_path.read_bytes(),
            )

    def test_login_profile_refresh_logout_and_post_logout_rejection(self):
        with ExitStack() as stack:
            state = self.start_application(stack)
            cookies, provider_url, start = self.begin_login(state)

            transaction_headers = start.header_values("Set-Cookie")
            self.assertTrue(
                any(
                    "__Host-wahojobs_google_tx=" in value
                    and "Secure" in value
                    and "HttpOnly" in value
                    and "SameSite=Lax" in value
                    and "Path=/" in value
                    and "Max-Age=600" in value
                    and "Domain=" not in value
                    for value in transaction_headers
                )
            )

            callback_url = provider_callback_for(state, provider_url)
            callback_parts = urlsplit(callback_url)
            callback = https_request(
                state,
                "GET",
                callback_parts.path + "?" + callback_parts.query,
                headers=(
                    ("Cookie", cookie_header(cookies)),
                ),
            )
            self.assertEqual(callback.status, 303)
            self.assertEqual(
                callback.header_values("Location"),
                ("/account/profile",),
            )
            callback_cookie_headers = callback.header_values("Set-Cookie")
            self.assertTrue(
                any(
                    value.startswith("wahojobs_session=")
                    and "Secure" in value
                    and "HttpOnly" in value
                    and "SameSite=Lax" in value
                    and "Path=/" in value
                    and "Domain=" not in value
                    for value in callback_cookie_headers
                )
            )
            self.assertTrue(
                any(
                    value.startswith(
                        "__Host-wahojobs_session_csrf="
                    )
                    and "Secure" in value
                    and "HttpOnly" in value
                    and "SameSite=Strict" in value
                    and "Path=/" in value
                    and "Domain=" not in value
                    for value in callback_cookie_headers
                )
            )
            self.update_cookies(cookies, callback)
            self.assertNotIn("__Host-wahojobs_google_tx", cookies)
            self.assertRegex(
                cookies["wahojobs_session"],
                r"^[A-Za-z0-9_-]{43}$",
            )
            self.assertRegex(
                cookies["__Host-wahojobs_session_csrf"],
                r"^[A-Za-z0-9_-]{43}$",
            )

            for _attempt in range(2):
                profile = https_request(
                    state,
                    "GET",
                    "/account/profile",
                    headers=(("Cookie", cookie_header(cookies)),),
                )
                self.assertEqual(profile.status, 200)
                self.assertIn(b"My persistent profile", profile.body)
                self.assertIn(b"/logout", profile.body)

            logout_page = https_request(
                state,
                "GET",
                "/logout",
                headers=(("Cookie", cookie_header(cookies)),),
            )
            self.assertEqual(logout_page.status, 200)
            self.assertIn(b"Sign out?", logout_page.body)
            self.assertIn(
                cookies["__Host-wahojobs_session_csrf"].encode("ascii"),
                logout_page.body,
            )

            logout = self.request_form(
                state,
                "/logout",
                {
                    "csrf": cookies[
                        "__Host-wahojobs_session_csrf"
                    ]
                },
                cookies,
            )
            self.assertEqual(logout.status, 303)
            self.assertEqual(logout.header_values("Location"), ("/login",))
            self.update_cookies(cookies, logout)
            self.assertNotIn("wahojobs_session", cookies)
            self.assertNotIn(
                "__Host-wahojobs_session_csrf",
                cookies,
            )

            rejected = https_request(
                state,
                "GET",
                "/account/profile",
                headers=(
                    (
                        "Cookie",
                        "wahojobs_session="
                        + callback_cookie_value(
                            callback,
                            "wahojobs_session",
                        ),
                    ),
                ),
            )
            self.assertEqual(rejected.status, 401)
            self.assertIn(b"/login", rejected.body)

            connection = sqlite3.connect(state.database_path)
            try:
                session = connection.execute(
                    "SELECT revoked_at, revoke_reason, session_version "
                    "FROM account_sessions"
                ).fetchone()
            finally:
                connection.close()
            self.assertIsNotNone(session[0])
            self.assertEqual(session[1], "user_logout")
            self.assertEqual(session[2], 2)

    def test_swapped_browser_binding_terminally_consumes_before_provider(self):
        with ExitStack() as stack:
            state = self.start_application(stack)
            cookies, provider_url, _start = self.begin_login(state)
            callback_url = provider_callback_for(state, provider_url)
            callback_parts = urlsplit(callback_url)
            cookies["__Host-wahojobs_google_tx"] = (
                "oidctx_" + "0" * 32
            )

            swapped = https_request(
                state,
                "GET",
                callback_parts.path + "?" + callback_parts.query,
                headers=(("Cookie", cookie_header(cookies)),),
            )
            self.assertEqual(swapped.status, 400)
            self.assertEqual(state.gateway_harness.transport.call_count, 0)

            connection = sqlite3.connect(state.database_path)
            try:
                lifecycle = connection.execute(
                    "SELECT lifecycle FROM "
                    "google_oidc_authorization_transactions"
                ).fetchone()[0]
                session_count = connection.execute(
                    "SELECT COUNT(*) FROM account_sessions"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(lifecycle, "consumed")
            self.assertEqual(session_count, 0)

            replay = https_request(
                state,
                "GET",
                callback_parts.path + "?" + callback_parts.query,
                headers=(("Cookie", cookie_header(cookies)),),
            )
            self.assertIn(replay.status, {400, 410})
            self.assertEqual(state.gateway_harness.transport.call_count, 0)

    def test_restart_with_retained_key_rotation_completes_in_flight_login(self):
        with ExitStack() as stack:
            state = stack.enter_context(temporary_browser_login_state())
            stack.enter_context(loopback_and_in_memory_provider_only())

            first_runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            with running_https_browser_app(first_runtime):
                cookies, provider_url, _start = self.begin_login(state)
            first_runtime.close()
            state.close_harnesses()

            lookup_two = state.directory / "lookup-2.key"
            protection_twelve = state.directory / "protection-12.key"
            for path, payload in (
                (lookup_two, LOOKUP_KEY_MATERIAL[2]),
                (protection_twelve, PROTECTION_KEY_MATERIAL[12]),
            ):
                with path.open("xb") as handle:
                    handle.write(payload)
                os.chmod(path, 0o600)
            document = json.loads(
                state.configuration_path.read_text(encoding="utf-8")
            )
            document["oidc_lookup_keys"].append(
                {"version": 2, "file": str(lookup_two)}
            )
            document["oidc_lookup_active_version"] = 2
            document["oidc_protection_keys"].append(
                {"version": 12, "file": str(protection_twelve)}
            )
            document["oidc_protection_active_version"] = 12
            state.configuration_path.write_text(
                json.dumps(
                    document,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )

            second_runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            stack.callback(second_runtime.close)
            callback_url = provider_callback_for(state, provider_url)
            parts = urlsplit(callback_url)
            with running_https_browser_app(second_runtime):
                response = https_request(
                    state,
                    "GET",
                    parts.path + "?" + parts.query,
                    headers=(("Cookie", cookie_header(cookies)),),
                )
            self.assertEqual(response.status, 303)
            self.assertEqual(
                response.header_values("Location"),
                ("/account/profile",),
            )
            self.assertIn(
                "wahojobs_session",
                cookie_values(response),
            )

    def test_fixture_rejects_untrusted_request_before_authorization_pop(self):
        public_origin = "https://127.0.0.1:8443"
        authority = "127.0.0.1:8443"
        untrusted_requests = (
            ("missing Host", FIXTURE_COMPLETE_ROUTE, ()),
            (
                "mismatched Host",
                FIXTURE_COMPLETE_ROUTE,
                (("Host", "attacker.invalid"),),
            ),
            (
                "duplicate Host",
                FIXTURE_COMPLETE_ROUTE,
                (("Host", authority), ("Host", authority)),
            ),
            (
                "Forwarded",
                FIXTURE_COMPLETE_ROUTE,
                (
                    ("Host", authority),
                    ("Forwarded", "host=attacker.invalid"),
                ),
            ),
            (
                "X-Forwarded-Host",
                FIXTURE_COMPLETE_ROUTE,
                (
                    ("Host", authority),
                    ("X-Forwarded-Host", "attacker.invalid"),
                ),
            ),
            (
                "X-Forwarded-Proto",
                FIXTURE_COMPLETE_ROUTE,
                (("Host", authority), ("X-Forwarded-Proto", "http")),
            ),
            (
                "absolute-form target",
                public_origin + FIXTURE_COMPLETE_ROUTE,
                (("Host", authority),),
            ),
        )

        for label, target, headers in untrusted_requests:
            with self.subTest(label=label):
                authorization_store = ["https://provider.invalid/authorize"]
                bridge = _ControlledProviderBridge(
                    _RejectUnexpectedDelegate(),
                    SimpleNamespace(public_origin=public_origin),
                    authorization_store,
                )

                response = bridge.handle("GET", target, headers)

                self.assertEqual(response.status, 400)
                self.assertEqual(
                    authorization_store,
                    ["https://provider.invalid/authorize"],
                )

    def test_no_egress_guard_rejects_udp_destination_apis(self):
        with loopback_and_in_memory_provider_only():
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                with self.assertRaisesRegex(
                    AssertionError,
                    "^non_loopback_socket_forbidden$",
                ):
                    udp.sendto(b"blocked", ("203.0.113.1", 9))
                with self.assertRaisesRegex(
                    AssertionError,
                    "^non_loopback_socket_forbidden$",
                ):
                    udp.sendmsg(
                        (b"blocked",),
                        address=("203.0.113.1", 9),
                    )

    def test_no_egress_guard_preserves_connected_sendmsg_call_shape(self):
        class RecordingSocket:
            def __init__(self, *_args, **_kwargs):
                self.calls = []

            def sendmsg(self, buffers, ancdata=(), flags=0):
                self.calls.append((buffers, ancdata, flags))
                return 7

        with mock.patch.object(socket, "socket", RecordingSocket):
            with loopback_and_in_memory_provider_only():
                guarded = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                result = guarded.sendmsg((b"trusted",))

        self.assertEqual(result, 7)
        self.assertEqual(
            guarded.calls,
            [((b"trusted",), (), 0)],
        )


class DurableGoogleLoginB21RestartTests(unittest.TestCase):
    @staticmethod
    def _active_child_pids():
        return {
            child.pid
            for child in multiprocessing.active_children()
            if child.pid is not None
        }

    def _assert_scoped_children_reaped(self, baseline, *owned_pids):
        current = self._active_child_pids()
        self.assertTrue(current.issubset(baseline))
        for pid in owned_pids:
            self.assertNotIn(pid, current)

    def _worker_request(
        self,
        state,
        *,
        role,
        pause_at=None,
        provider_url=None,
        transaction_cookie=None,
    ):
        seed = os.environ.get("PYTHONHASHSEED")
        self.assertIn(seed, {"0", "1", "42", "8675309"})
        self.assertEqual(os.environ.get("PYTHONDONTWRITEBYTECODE"), "1")
        return {
            "role": role,
            "configuration_path": str(state.configuration_path),
            "subject": state.subject,
            "pause_at": pause_at,
            "provider_url": provider_url,
            "transaction_cookie": transaction_cookie,
            "expected_seed": seed,
        }

    def _probe_database(
        self,
        state,
        *,
        transaction_count,
        lifecycle=None,
        row_version=None,
        session_count=0,
    ):
        connection = sqlite3.connect(state.database_path, timeout=2.0)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone(),
                ("ok",),
            )
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(),
                [],
            )
            rows = connection.execute(
                "SELECT lifecycle, row_version FROM "
                "google_oidc_authorization_transactions"
            ).fetchall()
            self.assertEqual(len(rows), transaction_count)
            if lifecycle is not None:
                self.assertEqual(rows, [(lifecycle, row_version)])
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM account_sessions"
                ).fetchone()[0],
                session_count,
            )
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE wahojobs_schema_migrations SET version=version "
                "WHERE version='006_google_oidc_authorization_transactions'"
            )
            connection.rollback()
        finally:
            connection.close()
        for suffix in ("-journal", "-wal", "-shm"):
            self.assertFalse(Path(str(state.database_path) + suffix).exists())

    @staticmethod
    def _b22_write_configuration(state, document):
        with state.configuration_path.open(
            "w",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            json.dump(
                document,
                handle,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")

    def _b22_use_version_one_for_both_rings(self, state):
        document = json.loads(
            state.configuration_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            document["oidc_lookup_keys"],
            [
                {
                    "version": 1,
                    "file": str(state.directory / "lookup-1.key"),
                }
            ],
        )
        self.assertEqual(len(document["oidc_protection_keys"]), 1)
        document["oidc_protection_keys"][0]["version"] = 1
        document["oidc_lookup_active_version"] = 1
        document["oidc_protection_active_version"] = 1
        self._b22_write_configuration(state, document)

    def _b22_activate_version_two(
        self,
        state,
        *,
        retain_lookup_one,
        retain_protection_one,
    ):
        lookup_two = state.directory / "lookup-2.key"
        protection_two = state.directory / "protection-2.key"
        for path, payload in (
            (lookup_two, LOOKUP_KEY_MATERIAL[2]),
            (protection_two, PROTECTION_KEY_MATERIAL[12]),
        ):
            with path.open("xb") as handle:
                handle.write(payload)
            os.chmod(path, 0o600)
        document = json.loads(
            state.configuration_path.read_text(encoding="utf-8")
        )
        lookup_one = document["oidc_lookup_keys"][0]
        protection_one = document["oidc_protection_keys"][0]
        document["oidc_lookup_keys"] = [
            *([lookup_one] if retain_lookup_one else []),
            {"version": 2, "file": str(lookup_two)},
        ]
        document["oidc_lookup_active_version"] = 2
        document["oidc_protection_keys"] = [
            *([protection_one] if retain_protection_one else []),
            {"version": 2, "file": str(protection_two)},
        ]
        document["oidc_protection_active_version"] = 2
        self._b22_write_configuration(state, document)

    @staticmethod
    def _b22_transaction_rows(state):
        connection = sqlite3.connect(state.database_path, timeout=2.0)
        try:
            return connection.execute(
                "SELECT transaction_id, lifecycle, row_version, "
                "lookup_key_version, protection_key_version FROM "
                "google_oidc_authorization_transactions "
                "ORDER BY transaction_id"
            ).fetchall()
        finally:
            connection.close()

    def _b22_prepare_version_one_transaction(self, state):
        first = FreshBrowserLoginWorker(
            self._worker_request(
                state,
                role="start",
                pause_at="prepare.after_commit",
            )
        )
        try:
            first.expect_phase("prepare.after_commit")
            rows = self._b22_transaction_rows(state)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1:], ("prepared", 1, 1, 1))
            first.continue_from_phase()
            started = first.finish_and_reap()
        finally:
            first.kill_and_reap()
        self.assertTrue(first.assert_terminal_resources())
        self.assertEqual(started["transaction_cookie"], rows[0][0])
        self.assertTrue(started["cleanup_complete"])
        self.assertTrue(started["manager_closed"])
        self.assertEqual(started["manager_records"], 0)
        self.assertEqual(started["start_method"], "spawn")
        self.assertEqual(started["seed"], os.environ["PYTHONHASHSEED"])
        self.assertTrue(started["dont_write_bytecode"])
        self.assertEqual(started["provider_calls"], 0)
        self.assertEqual(started["token_requests"], 0)
        self.assertEqual(started["jwks_requests"], 0)
        return started

    @staticmethod
    def _b22_worker_request(state, started, *, mode):
        return {
            "mode": mode,
            "configuration_path": str(state.configuration_path),
            "database_path": str(state.database_path),
            "subject": state.subject,
            "provider_url": started["provider_url"],
            "transaction_cookie": started["transaction_cookie"],
            "expected_seed": os.environ["PYTHONHASHSEED"],
        }

    def _assert_b22_success_response(self, summary):
        self.assertEqual(summary["status"], 303)
        self.assertTrue(summary["profile_location"])
        self.assertEqual(summary["session_cookie_count"], 1)
        self.assertEqual(summary["csrf_cookie_count"], 1)
        self.assertEqual(summary["transaction_clear_count"], 1)
        self.assertFalse(summary["generic_failure"])

    def _assert_b22_worker_cleanup(self, completed):
        self.assertEqual(completed["start_method"], "spawn")
        self.assertTrue(completed["cleanup_complete"])
        self.assertTrue(completed["manager_closed"])
        self.assertEqual(completed["manager_records"], 0)
        self.assertTrue(completed["egress_closed"])

    def test_b21_fresh_interpreter_restart_commits_then_completes_callback(self):
        warm_spawn_process_accounting()
        child_baseline = self._active_child_pids()
        with temporary_browser_login_state() as state:
            temporary_directory = state.directory
            handle_baseline = process_native_handle_count()
            first = FreshBrowserLoginWorker(
                self._worker_request(
                    state,
                    role="start",
                    pause_at="prepare.after_commit",
                )
            )
            try:
                first.expect_phase("prepare.after_commit")
                self._probe_database(
                    state,
                    transaction_count=1,
                    lifecycle="prepared",
                    row_version=1,
                )
                first.continue_from_phase()
                started = first.finish_and_reap()
            finally:
                first.kill_and_reap()
            self.assertTrue(first.assert_terminal_resources())

            self.assertTrue(started["cleanup_complete"])
            self.assertTrue(started["manager_closed"])
            self.assertEqual(started["manager_records"], 0)
            self.assertEqual(started["start_method"], "spawn")
            self.assertEqual(started["seed"], os.environ["PYTHONHASHSEED"])
            self.assertTrue(started["dont_write_bytecode"])
            self.assertEqual(started["provider_calls"], 0)
            self.assertEqual(started["token_requests"], 0)
            self.assertEqual(started["jwks_requests"], 0)
            self.assertGreaterEqual(len(started["issuance_fingerprints"]), 1)

            provider_url = started.pop("provider_url")
            transaction_cookie = started.pop("transaction_cookie")
            second = FreshBrowserLoginWorker(
                self._worker_request(
                    state,
                    role="callback",
                    provider_url=provider_url,
                    transaction_cookie=transaction_cookie,
                )
            )
            provider_url = None
            transaction_cookie = None
            try:
                completed = second.finish_and_reap()
            finally:
                second.kill_and_reap()
            self.assertTrue(second.assert_terminal_resources())

            self.assertNotEqual(
                started["epoch_fingerprint"],
                completed["epoch_fingerprint"],
            )
            self.assertTrue(
                set(started["issuance_fingerprints"]).isdisjoint(
                    completed["issuance_fingerprints"]
                )
            )
            self.assertGreaterEqual(
                len(completed["issuance_fingerprints"]),
                1,
            )
            self.assertEqual(completed["seed"], os.environ["PYTHONHASHSEED"])
            self.assertEqual(completed["start_method"], "spawn")
            self.assertTrue(completed["dont_write_bytecode"])
            self.assertTrue(completed["cleanup_complete"])
            self.assertTrue(completed["manager_closed"])
            self.assertEqual(completed["manager_records"], 0)
            self.assertEqual(completed["status"], 303)
            self.assertTrue(completed["profile_location"])
            self.assertEqual(completed["provider_calls"], 1)
            self.assertEqual(completed["token_requests"], 1)
            self.assertEqual(completed["jwks_requests"], 1)
            self.assertEqual(completed["session_cookie_count"], 1)
            self.assertEqual(completed["csrf_cookie_count"], 1)
            self.assertEqual(completed["transaction_clear_count"], 1)
            self._probe_database(
                state,
                transaction_count=1,
                lifecycle="consumed",
                row_version=2,
                session_count=1,
            )
            self._assert_scoped_children_reaped(
                child_baseline,
                started["pid"],
                completed["pid"],
            )
        self.assertFalse(temporary_directory.exists())
        self.assertLessEqual(
            process_native_handle_count(),
            handle_baseline,
        )

    def test_b21_process_exit_before_authorization_commit_recovers_database(self):
        warm_spawn_process_accounting()
        child_baseline = self._active_child_pids()
        with temporary_browser_login_state() as state:
            temporary_directory = state.directory
            handle_baseline = process_native_handle_count()
            worker = FreshBrowserLoginWorker(
                self._worker_request(
                    state,
                    role="start",
                    pause_at="prepare.after_insert",
                )
            )
            worker_pid = worker.pid
            try:
                worker.expect_phase("prepare.after_insert")
                worker.kill_and_reap()
            finally:
                worker.kill_and_reap()
            self.assertTrue(worker.assert_terminal_resources())
            self._probe_database(state, transaction_count=0)
            self._assert_scoped_children_reaped(
                child_baseline,
                worker_pid,
            )
            self.assertEqual(
                process_native_handle_count(),
                handle_baseline,
            )
        self.assertFalse(temporary_directory.exists())

    def test_b21_process_exit_after_commit_preserves_one_prepared_transaction(self):
        warm_spawn_process_accounting()
        child_baseline = self._active_child_pids()
        with temporary_browser_login_state() as state:
            temporary_directory = state.directory
            handle_baseline = process_native_handle_count()
            worker = FreshBrowserLoginWorker(
                self._worker_request(
                    state,
                    role="start",
                    pause_at="prepare.after_commit",
                )
            )
            worker_pid = worker.pid
            try:
                worker.expect_phase("prepare.after_commit")
                worker.kill_and_reap()
            finally:
                worker.kill_and_reap()
            self.assertTrue(worker.assert_terminal_resources())
            self._probe_database(
                state,
                transaction_count=1,
                lifecycle="prepared",
                row_version=1,
            )
            self._assert_scoped_children_reaped(
                child_baseline,
                worker_pid,
            )
            self.assertEqual(
                process_native_handle_count(),
                handle_baseline,
            )
        self.assertFalse(temporary_directory.exists())

    def test_b22_fresh_process_retained_keys_complete_old_and_new_logins(self):
        warm_spawn_process_accounting()
        child_baseline = self._active_child_pids()
        with temporary_browser_login_state() as state:
            temporary_directory = state.directory
            handle_baseline = process_native_handle_count()
            self._b22_use_version_one_for_both_rings(state)
            started = self._b22_prepare_version_one_transaction(state)
            self._assert_scoped_children_reaped(
                child_baseline,
                started["pid"],
            )

            self._b22_activate_version_two(
                state,
                retain_lookup_one=True,
                retain_protection_one=True,
            )
            second = _B22FreshBrowserLoginWorker(
                self._b22_worker_request(
                    state,
                    started,
                    mode="complete_and_start",
                )
            )
            try:
                completed = second.finish_and_reap()
            finally:
                second.kill_and_reap()
            self.assertTrue(second.assert_terminal_resources())

            self.assertNotEqual(started["pid"], completed["pid"])
            self.assertNotEqual(
                started["epoch_fingerprint"],
                completed["epoch_fingerprint"],
            )
            self._assert_b22_worker_cleanup(completed)
            self._assert_b22_success_response(completed["old_callback"])
            self.assertEqual(completed["after_old_provider_calls"], 1)
            self.assertEqual(completed["after_old_token_requests"], 1)
            self.assertEqual(completed["after_old_jwks_requests"], 1)

            after_new_start = completed["after_new_start"]
            self.assertEqual(after_new_start["session_count"], 1)
            self.assertEqual(len(after_new_start["rows"]), 2)
            self.assertEqual(
                completed["after_new_start_provider_calls"],
                1,
            )
            self.assertEqual(
                completed["after_new_start_token_requests"],
                1,
            )
            self.assertEqual(
                completed["after_new_start_jwks_requests"],
                1,
            )
            old_rows = [
                row
                for row in after_new_start["rows"]
                if row[0] == started["transaction_cookie"]
            ]
            prepared_rows = [
                row
                for row in after_new_start["rows"]
                if row[1] == "prepared"
            ]
            self.assertEqual(
                [row[1:] for row in old_rows],
                [["consumed", 2, 1, 1]],
            )
            self.assertEqual(
                [row[1:] for row in prepared_rows],
                [["prepared", 1, 2, 2]],
            )
            self.assertEqual(
                [row[0] for row in prepared_rows],
                [completed["new_transaction_cookie"]],
            )

            self._assert_b22_success_response(completed["new_callback"])
            self.assertEqual(completed["provider_calls"], 2)
            self.assertEqual(completed["token_requests"], 2)
            self.assertEqual(completed["jwks_requests"], 1)
            final_database = completed["final_database"]
            self.assertEqual(final_database["session_count"], 2)
            self.assertEqual(
                sorted(row[1:] for row in final_database["rows"]),
                [
                    ["consumed", 2, 1, 1],
                    ["consumed", 2, 2, 2],
                ],
            )
            self._probe_database(
                state,
                transaction_count=2,
                session_count=2,
            )
            self._assert_scoped_children_reaped(
                child_baseline,
                started["pid"],
                completed["pid"],
            )
        self.assertFalse(temporary_directory.exists())
        self.assertLessEqual(
            process_native_handle_count(),
            handle_baseline,
        )

    def test_b22_missing_each_retained_key_fails_closed(self):
        warm_spawn_process_accounting()
        child_baseline = self._active_child_pids()
        scenarios = (
            ("lookup", False, True, 400),
            ("protection", True, False, 503),
        )
        for name, retain_lookup, retain_protection, status in scenarios:
            with self.subTest(missing=name):
                with temporary_browser_login_state() as state:
                    temporary_directory = state.directory
                    handle_baseline = process_native_handle_count()
                    self._b22_use_version_one_for_both_rings(state)
                    started = self._b22_prepare_version_one_transaction(
                        state
                    )
                    self._assert_scoped_children_reaped(
                        child_baseline,
                        started["pid"],
                    )
                    self._b22_activate_version_two(
                        state,
                        retain_lookup_one=retain_lookup,
                        retain_protection_one=retain_protection,
                    )
                    second = _B22FreshBrowserLoginWorker(
                        self._b22_worker_request(
                            state,
                            started,
                            mode="callback_only",
                        )
                    )
                    try:
                        completed = second.finish_and_reap()
                    finally:
                        second.kill_and_reap()
                    self.assertTrue(second.assert_terminal_resources())

                    self.assertNotEqual(started["pid"], completed["pid"])
                    self.assertNotEqual(
                        started["epoch_fingerprint"],
                        completed["epoch_fingerprint"],
                    )
                    self._assert_b22_worker_cleanup(completed)
                    rejected = completed["old_callback"]
                    self.assertEqual(rejected["status"], status)
                    self.assertTrue(rejected["generic_failure"])
                    self.assertFalse(rejected["profile_location"])
                    self.assertEqual(rejected["session_cookie_count"], 0)
                    self.assertEqual(rejected["csrf_cookie_count"], 0)
                    self.assertEqual(
                        rejected["transaction_clear_count"],
                        1,
                    )
                    self.assertEqual(completed["provider_calls"], 0)
                    self.assertEqual(completed["token_requests"], 0)
                    self.assertEqual(completed["jwks_requests"], 0)
                    self.assertEqual(
                        completed["final_database"]["session_count"],
                        0,
                    )
                    self.assertEqual(
                        [
                            (row[0], row[3], row[4])
                            for row in completed["final_database"]["rows"]
                        ],
                        [(started["transaction_cookie"], 1, 1)],
                    )
                    self._probe_database(
                        state,
                        transaction_count=1,
                        session_count=0,
                    )
                    self._assert_scoped_children_reaped(
                        child_baseline,
                        started["pid"],
                        completed["pid"],
                    )
                self.assertFalse(temporary_directory.exists())
                self.assertLessEqual(
                    process_native_handle_count(),
                    handle_baseline,
                )


class _RejectUnexpectedDelegate:
    def handle(self, *_args, **_kwargs):
        raise AssertionError("fixture_request_reached_delegate")


def callback_cookie_value(response, name):
    values = cookie_values(response)
    if name not in values:
        raise AssertionError(f"missing_cookie_{name}")
    return values[name]


if __name__ == "__main__":
    unittest.main()
