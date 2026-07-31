from contextlib import ExitStack
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
from tests.google_oidc_authorization_transactions_test_support import (
    LOOKUP_KEY_MATERIAL,
    PROTECTION_KEY_MATERIAL,
)
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
from wahojobs.durable_google_login_runtime import (
    build_durable_google_login_runtime,
)


class DurableGoogleLoginBrowserIntegrationTests(unittest.TestCase):
    def start_application(self, stack):
        state = stack.enter_context(temporary_browser_login_state())
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

    def begin_login(self, state):
        cookies = {}
        login = https_request(state, "GET", "/login")
        self.assertEqual(login.status, 200)
        self.assertIn(b"Continue with Google", login.body)
        self.update_cookies(cookies, login)
        csrf = cookies["__Host-wahojobs_login_csrf"]

        start = self.request_form(
            state,
            "/auth/google/start",
            {"csrf": csrf},
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
