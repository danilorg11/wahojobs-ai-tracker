from contextlib import ExitStack
import multiprocessing
import sqlite3
import threading
import unittest
from urllib.parse import urlsplit

from tests.durable_google_login_browser_test_support import (
    cookie_header,
    cookie_values,
    form_body,
    https_request,
    loopback_and_in_memory_provider_only,
    provider_callback_for,
    running_https_browser_app,
    temporary_browser_login_state,
)
from wahojobs.durable_google_login_runtime import (
    build_durable_google_login_runtime,
)


def _process_browser_bound_callback(
    database_path,
    authorization_url,
    browser_transaction_id,
    subject,
    start_event,
    output,
):
    from tests.google_oidc_authorization_transactions_test_support import (
        completion_policy,
        key_authority,
        make_real_gateway,
        open_connection,
        request_secret_vault,
        sockets_blocked,
    )
    from wahojobs.google_oidc_durable_gateway import (
        complete_browser_bound_durable_google_oidc_authorization,
    )
    from wahojobs.browser_session_lifecycle import (
        discard_request_scoped_session_secret_vault,
    )
    from wahojobs.trusted_login_completion import prepare_session_delivery

    class Prepared:
        pass

    connection = open_connection(database_path, timeout=5.0)
    harness = make_real_gateway(subject=subject)
    authority = key_authority()
    vault = request_secret_vault()
    try:
        prepared = Prepared()
        prepared.authorization_url = authorization_url
        callback_url = harness.transport.callback_for(
            prepared,
            code="process-browser-code",
        )
        start_event.wait(10)
        with sockets_blocked():
            result = (
                complete_browser_bound_durable_google_oidc_authorization(
                    connection,
                    harness.gateway,
                    authority,
                    callback_url,
                    browser_transaction_id,
                    completion_policy(),
                    vault,
                )
            )
        cookie_emitted = False
        if result.status == "issued":
            lease = prepare_session_delivery(
                connection,
                result,
                vault,
                now=harness.clock(),
            )
            cookie_emitted = lease.set_cookie_header.startswith(
                "wahojobs_session="
            )
            lease.acknowledge_delivery()
        else:
            discard_request_scoped_session_secret_vault(vault)
        output.put(
            (
                result.status,
                harness.transport.token_request_count,
                connection.in_transaction,
                cookie_emitted,
            )
        )
    finally:
        try:
            discard_request_scoped_session_secret_vault(vault)
        except Exception:
            pass
        authority.close()
        harness.close()
        connection.close()


class DurableGoogleLoginBrowserConcurrencyTests(unittest.TestCase):
    def begin_login(self, state):
        login = https_request(state, "GET", "/login")
        cookies = cookie_values(login)
        body = form_body(csrf=cookies["__Host-wahojobs_login_csrf"])
        start = https_request(
            state,
            "POST",
            "/auth/google/start",
            headers=(
                ("Origin", state.public_origin),
                ("Sec-Fetch-Site", "same-origin"),
                ("Content-Type", "application/x-www-form-urlencoded"),
                ("Content-Length", str(len(body))),
                ("Cookie", cookie_header(cookies)),
            ),
            body=body,
        )
        self.assertEqual(start.status, 303)
        for name, value in cookie_values(start).items():
            if value:
                cookies[name] = value
            else:
                cookies.pop(name, None)
        return cookies, start.header_values("Location")[0]

    def test_two_threaded_http_callbacks_create_one_session_and_cookie(self):
        with ExitStack() as stack:
            state = stack.enter_context(temporary_browser_login_state())
            runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            stack.callback(runtime.close)
            stack.enter_context(loopback_and_in_memory_provider_only())
            stack.enter_context(running_https_browser_app(runtime))
            cookies, provider_url = self.begin_login(state)
            callback_url = provider_callback_for(state, provider_url)
            parts = urlsplit(callback_url)
            target = parts.path + "?" + parts.query

            barrier = threading.Barrier(3)
            results = []
            errors = []

            def worker():
                try:
                    barrier.wait(timeout=5)
                    response = https_request(
                        state,
                        "GET",
                        target,
                        headers=(("Cookie", cookie_header(cookies)),),
                    )
                    results.append(response)
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(sorted(item.status for item in results), [303, 400])
            self.assertEqual(
                sum(
                    any(
                        header.startswith("wahojobs_session=")
                        for header in response.header_values("Set-Cookie")
                    )
                    for response in results
                ),
                1,
            )
            self.assertEqual(
                state.gateway_harness.transport.token_request_count,
                1,
            )

            connection = sqlite3.connect(state.database_path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM account_sessions"
                    ).fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_two_processes_have_one_terminal_claim_and_zero_provider_on_mismatch(self):
        from tests.google_oidc_authorization_transactions_test_support import (
            authorization_parameters,
            durable_transaction_database,
            key_authority,
            reconstructed_gateway,
            sockets_blocked,
        )
        from wahojobs.google_oidc_durable_gateway import (
            prepare_durable_google_oidc_authorization,
        )

        with durable_transaction_database() as database:
            harness = reconstructed_gateway(subject=database.subject)
            authority = key_authority()
            try:
                with sockets_blocked():
                    prepared = prepare_durable_google_oidc_authorization(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                authorization_parameters(prepared)
                authorization_url = prepared.authorization_url
                transaction_id = prepared.transaction_id
                prepared.close()
            finally:
                authority.close()
                harness.close()

            context = multiprocessing.get_context("spawn")
            start_event = context.Event()
            output = context.Queue()
            processes = [
                context.Process(
                    target=_process_browser_bound_callback,
                    args=(
                        str(database.path),
                        authorization_url,
                        transaction_id,
                        database.subject,
                        start_event,
                        output,
                    ),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            start_event.set()
            observed = [output.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(timeout=20)
            self.assertTrue(
                all(process.exitcode == 0 for process in processes),
                [(process.pid, process.exitcode) for process in processes],
            )
            self.assertEqual(
                sorted(item[0] for item in observed),
                ["invalid_or_expired_transaction", "issued"],
            )
            self.assertEqual(sum(item[1] for item in observed), 1)
            self.assertTrue(all(item[2] is False for item in observed))
            self.assertEqual(sum(item[3] for item in observed), 1)
            self.assertEqual(
                database.connection.execute(
                    "SELECT lifecycle FROM "
                    "google_oidc_authorization_transactions"
                ).fetchone()[0],
                "consumed",
            )
            self.assertEqual(
                database.connection.execute(
                    "SELECT COUNT(*) FROM account_sessions"
                ).fetchone()[0],
                1,
            )
            output.close()
            output.join_thread()

    def test_provider_wait_holds_no_authorization_transaction_write_lock(self):
        with ExitStack() as stack:
            state = stack.enter_context(temporary_browser_login_state())
            state.gateway_options["block"] = True
            runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            stack.callback(runtime.close)
            stack.enter_context(loopback_and_in_memory_provider_only())
            stack.enter_context(running_https_browser_app(runtime))
            cookies, provider_url = self.begin_login(state)
            callback_url = provider_callback_for(state, provider_url)
            parts = urlsplit(callback_url)
            target = parts.path + "?" + parts.query
            result = []
            failure = []

            def callback_worker():
                try:
                    result.append(
                        https_request(
                            state,
                            "GET",
                            target,
                            headers=(
                                ("Cookie", cookie_header(cookies)),
                            ),
                        )
                    )
                except BaseException as exc:
                    failure.append(exc)

            thread = threading.Thread(target=callback_worker)
            thread.start()
            self.assertTrue(
                state.gateway_harness.transport.entered.wait(5),
                "provider_fixture_was_not_reached",
            )
            second = sqlite3.connect(state.database_path, timeout=1.0)
            try:
                second.execute("BEGIN IMMEDIATE")
                second.rollback()
            finally:
                second.close()
            state.gateway_harness.transport.release.set()
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failure, [])
            self.assertEqual([item.status for item in result], [303])


if __name__ == "__main__":
    unittest.main()
