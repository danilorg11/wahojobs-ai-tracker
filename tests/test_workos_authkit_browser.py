from __future__ import annotations

from io import BytesIO
from pathlib import Path
import secrets
import tempfile
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

from tests.workos_authkit_test_support import (
    FakeWorkOSBoundary,
    MutableClock,
    NOW,
    PUBLIC_ORIGIN,
    build_m008,
    completion_policy,
    connect,
    create_invitation,
    gateway,
)
from wahojobs import accounts
from wahojobs.workos_authkit_browser import (
    AUTHENTICATED_DESTINATION,
    LOGIN_CSRF_COOKIE_NAME,
    LOGIN_ROUTE,
    LOGOUT_ROUTE,
    SESSION_COOKIE_NAME,
    SESSION_CSRF_COOKIE_NAME,
    WORKOS_LOGIN_CALLBACK_ROUTE,
    WORKOS_LOGIN_START_ROUTE,
    WORKOS_TRANSACTION_COOKIE_NAME,
    WorkOSAuthKitBrowserIntegration,
)


AUTHORITY = "127.0.0.1:9443"


class _ProfileIntegration:
    def __init__(self):
        self.calls = []
        self.response = object()

    def matches_route(self, path):
        return path in {AUTHENTICATED_DESTINATION, "/find-matches"}

    def handle(self, method, target, headers, body_stream=None):
        self.calls.append((method, target, tuple(headers), body_stream))
        return self.response


def _header_values(response, name):
    return [value for key, value in response.headers if key.lower() == name.lower()]


def _cookie_pair(response, name):
    prefix = name + "="
    matches = [
        value.split(";", 1)[0]
        for value in _header_values(response, "Set-Cookie")
        if value.startswith(prefix)
    ]
    if len(matches) != 1:
        raise AssertionError(f"missing cookie {name}")
    return matches[0]


def _cookie_value(pair):
    return pair.split("=", 1)[1]


class WorkOSAuthKitBrowserTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="wahojobs-workos-browser-test-",
            ignore_cleanup_errors=True,
        )
        self.path = Path(self.directory.name) / "database.sqlite3"
        self.seed = build_m008(self.path)
        self.clock = MutableClock()
        self.boundary = FakeWorkOSBoundary()
        self.gateway = gateway(self.boundary, clock=self.clock)
        self.profile = _ProfileIntegration()
        self.browser = WorkOSAuthKitBrowserIntegration(
            public_origin=PUBLIC_ORIGIN,
            profile_integration=self.profile,
            connection_factory=lambda: connect(self.path, timeout=5.0),
            gateway=self.gateway,
            completion_policy=completion_policy(),
            clock=self.clock,
            token_factory=lambda: secrets.token_urlsafe(32),
        )

    def tearDown(self):
        self.browser.close()
        self.gateway.close()
        self.seed.close()
        self.directory.cleanup()

    @staticmethod
    def _get_headers(*, cookie=None):
        values = [("Host", AUTHORITY)]
        if cookie is not None:
            values.append(("Cookie", cookie))
        return tuple(values)

    @staticmethod
    def _post_headers(payload, *, cookie):
        return (
            ("Host", AUTHORITY),
            ("Origin", PUBLIC_ORIGIN),
            ("Sec-Fetch-Site", "same-origin"),
            ("Cookie", cookie),
            ("Content-Type", "application/x-www-form-urlencoded"),
            ("Content-Length", str(len(payload))),
        )

    def _start(self, *, invitation_token=None):
        login = self.browser.handle("GET", LOGIN_ROUTE, self._get_headers())
        self.assertEqual(login.status, 200)
        self.assertIn(b"one-time email code", login.body)
        self.assertNotIn(b"Google", login.body)
        login_cookie = _cookie_pair(login, LOGIN_CSRF_COOKIE_NAME)
        csrf = _cookie_value(login_cookie)
        login.acknowledge_delivery()

        form = {"csrf": csrf}
        if invitation_token is not None:
            form["invitation"] = invitation_token
        payload = urlencode(form).encode("ascii")
        start = self.browser.handle(
            "POST",
            WORKOS_LOGIN_START_ROUTE,
            self._post_headers(payload, cookie=login_cookie),
            BytesIO(payload),
        )
        self.assertEqual(start.status, 303)
        location = _header_values(start, "Location")
        self.assertEqual(len(location), 1)
        transaction_cookie = _cookie_pair(start, WORKOS_TRANSACTION_COOKIE_NAME)
        rendered = repr(start) + start.body.decode("utf-8") + "\n".join(
            value for _name, value in start.headers
        )
        if invitation_token is not None:
            self.assertNotIn(invitation_token, rendered)
        start.acknowledge_delivery()
        return location[0], transaction_cookie

    def _callback(self, authorization_url, transaction_cookie, *, code=None):
        state = parse_qs(urlsplit(authorization_url).query)["state"][0]
        code = code or secrets.token_urlsafe(32)
        target = WORKOS_LOGIN_CALLBACK_ROUTE + "?" + urlencode(
            {"code": code, "state": state}
        )
        response = self.browser.handle(
            "GET",
            target,
            self._get_headers(cookie=transaction_cookie),
        )
        return response, target, code

    def _successful_login(self):
        invitation = create_invitation(self.seed, self.boundary.email)
        authorization_url, transaction_cookie = self._start(
            invitation_token=invitation.invitation_token
        )
        response, target, code = self._callback(
            authorization_url,
            transaction_cookie,
        )
        self.assertEqual(response.status, 303)
        self.assertEqual(_header_values(response, "Location"), [AUTHENTICATED_DESTINATION])
        return response, target, code, transaction_cookie

    def test_success_uses_existing_session_delivery_and_replay_is_terminal(self):
        response, target, code, transaction_cookie = self._successful_login()
        session_pair = _cookie_pair(response, SESSION_COOKIE_NAME)
        csrf_pair = _cookie_pair(response, SESSION_CSRF_COOKIE_NAME)
        self.assertNotIn(code, repr(response))
        response.acknowledge_delivery()

        session = accounts.validate_session_csrf(
            self.seed,
            session_token=_cookie_value(session_pair),
            csrf_secret=_cookie_value(csrf_pair),
            now=NOW,
        )
        self.assertEqual(session.user_id, self.seed.execute("SELECT user_id FROM users").fetchone()[0])
        self.assertEqual(self.seed.execute("SELECT COUNT(*) FROM product_principals").fetchone()[0], 1)
        self.assertEqual(
            self.seed.execute("SELECT COUNT(*) FROM principal_account_bindings").fetchone()[0],
            1,
        )

        replay = self.browser.handle(
            "GET",
            target,
            self._get_headers(cookie=transaction_cookie),
        )
        self.assertEqual(replay.status, 401)
        replay.acknowledge_delivery()
        self.assertEqual(self.boundary.exchange_count, 1)
        self.assertEqual(self.seed.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0], 1)

    def test_cookie_delivery_failure_invokes_existing_compensation(self):
        response, _target, _code, _transaction_cookie = self._successful_login()
        response.fail_delivery()
        row = self.seed.execute(
            "SELECT revoked_at, revoke_reason, session_version FROM account_sessions"
        ).fetchone()
        self.assertIsNotNone(row[0])
        self.assertEqual(tuple(row[1:]), ("security_reset", 2))

    def test_existing_logout_routes_revoke_delivered_session(self):
        response, _target, _code, _transaction_cookie = self._successful_login()
        session_pair = _cookie_pair(response, SESSION_COOKIE_NAME)
        csrf_pair = _cookie_pair(response, SESSION_CSRF_COOKIE_NAME)
        response.acknowledge_delivery()
        cookie = session_pair + "; " + csrf_pair

        page = self.browser.handle("GET", LOGOUT_ROUTE, self._get_headers(cookie=cookie))
        self.assertEqual(page.status, 200)
        self.assertIn(b"Sign out?", page.body)
        page.acknowledge_delivery()

        payload = urlencode({"csrf": _cookie_value(csrf_pair)}).encode("ascii")
        logout = self.browser.handle(
            "POST",
            LOGOUT_ROUTE,
            self._post_headers(payload, cookie=cookie),
            BytesIO(payload),
        )
        self.assertEqual(logout.status, 303)
        self.assertEqual(_header_values(logout, "Location"), [LOGIN_ROUTE])
        logout.acknowledge_delivery()
        row = self.seed.execute(
            "SELECT revoked_at, revoke_reason FROM account_sessions"
        ).fetchone()
        self.assertIsNotNone(row[0])
        self.assertEqual(row[1], "user_logout")

    def test_provider_failure_is_sanitized_and_has_no_wahojobs_mutation(self):
        invitation = create_invitation(self.seed, self.boundary.email)
        authorization_url, transaction_cookie = self._start(
            invitation_token=invitation.invitation_token
        )
        self.boundary.fail_exchange = True
        response, _target, code = self._callback(authorization_url, transaction_cookie)
        rendered = repr(response) + response.body.decode("utf-8") + "\n".join(
            value for _name, value in response.headers
        )
        self.assertEqual(response.status, 503)
        self.assertIn("Sign-in not completed", rendered)
        self.assertNotIn(invitation.invitation_token, rendered)
        self.assertNotIn(code, rendered)
        response.acknowledge_delivery()
        self.assertEqual(self.seed.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
        self.assertEqual(self.seed.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0], 0)

    def test_only_expected_routes_are_owned_and_profile_routes_are_delegated(self):
        for route in (
            LOGIN_ROUTE,
            WORKOS_LOGIN_START_ROUTE,
            WORKOS_LOGIN_CALLBACK_ROUTE,
            LOGOUT_ROUTE,
            AUTHENTICATED_DESTINATION,
            "/find-matches",
        ):
            self.assertTrue(self.browser.matches_route(route))
        self.assertFalse(self.browser.matches_route("/auth/google/start"))
        self.assertFalse(self.browser.matches_route("/unknown"))

        delegated = self.browser.handle(
            "GET",
            AUTHENTICATED_DESTINATION,
            self._get_headers(),
        )
        self.assertIs(delegated, self.profile.response)
        self.assertEqual(self.profile.calls[0][0:2], ("GET", AUTHENTICATED_DESTINATION))

        rejected = self.browser.handle(
            "GET",
            WORKOS_LOGIN_START_ROUTE,
            self._get_headers(),
        )
        self.assertEqual(rejected.status, 405)
        self.assertEqual(_header_values(rejected, "Allow"), ["POST"])
        rejected.acknowledge_delivery()


if __name__ == "__main__":
    unittest.main()
