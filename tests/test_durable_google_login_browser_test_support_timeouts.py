import http.client
import socket
import ssl
import threading
import time
from types import SimpleNamespace, TracebackType
import unittest
from unittest import mock

from scripts.durable_google_login_app import _ephemeral_tls_context
import tests.durable_google_login_browser_test_support as support


_ORIGINAL_HTTPS_CONNECTION = http.client.HTTPSConnection
_HTTP_RESPONSE = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Length: 2\r\n"
    b"Connection: close\r\n"
    b"X-Synthetic-Response: complete\r\n"
    b"\r\n"
    b"OK"
)


class _RecordingConnectionFactory:
    def __init__(self):
        self.calls = 0
        self.connect_calls = 0
        self.instances = []
        self.connected_sockets = []

    def __call__(self, *args, **kwargs):
        self.calls += 1
        patched_value = support.http.client.HTTPSConnection
        try:
            support.http.client.HTTPSConnection = _ORIGINAL_HTTPS_CONNECTION
            connection = _ORIGINAL_HTTPS_CONNECTION(*args, **kwargs)
        finally:
            support.http.client.HTTPSConnection = patched_value
        original_connect = connection.connect

        def observed_connect():
            self.connect_calls += 1
            try:
                return original_connect()
            finally:
                if connection.sock is not None:
                    self.connected_sockets.append(connection.sock)

        connection.connect = observed_connect
        self.instances.append(connection)
        return connection


class _SyntheticLocalTlsServer:
    def __init__(
        self,
        *,
        response_delay_seconds=0,
        complete_tls_handshake=True,
        hold_partial_body=False,
    ):
        self.response_delay_seconds = response_delay_seconds
        self.complete_tls_handshake = complete_tls_handshake
        self.hold_partial_body = hold_partial_body
        self.accept_count = 0
        self.request_count = 0
        self.request_received_at = None
        self.response_started_at = None
        self._accepted_socket = None
        self._error = None
        self._listener = None
        self._release = threading.Event()
        self._stopping = threading.Event()
        self._terminal = threading.Event()
        self._thread = None
        self._tls_owner = None
        self._tls_context = None

    @property
    def state(self):
        return SimpleNamespace(
            public_origin=f"https://localhost:{self._listener.getsockname()[1]}"
        )

    def __enter__(self):
        try:
            self._tls_owner = _ephemeral_tls_context()
            self._tls_context = self._tls_owner.__enter__()
            self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._listener.bind(("127.0.0.1", 0))
            self._listener.listen(1)
            self._listener.settimeout(1)
            self._thread = threading.Thread(
                target=self._serve,
                name="local-tls-timeout-contract-server",
                daemon=False,
            )
            self._thread.start()
            return self
        except BaseException:
            self._close()
            raise

    def __exit__(self, _exc_type, _exc, _traceback):
        self._close()
        return False

    def _serve(self):
        accepted_socket = None
        try:
            accepted_socket, _address = self._listener.accept()
            self._accepted_socket = accepted_socket
            self.accept_count += 1
            if not self.complete_tls_handshake:
                self._release.wait()
                return

            accepted_socket = self._tls_context.wrap_socket(
                accepted_socket,
                server_side=True,
            )
            self._accepted_socket = accepted_socket
            request = bytearray()
            while b"\r\n\r\n" not in request:
                chunk = accepted_socket.recv(4096)
                if not chunk:
                    raise RuntimeError("synthetic_request_incomplete")
                request.extend(chunk)
                if len(request) > 65_536:
                    raise RuntimeError("synthetic_request_too_large")
            self.request_count += 1
            self.request_received_at = time.monotonic()

            if self.hold_partial_body:
                accepted_socket.sendall(_HTTP_RESPONSE[:-1])
                self.response_started_at = time.monotonic()
                self._release.wait()
                return

            if self.response_delay_seconds is None:
                self._release.wait()
                return
            if self._release.wait(self.response_delay_seconds):
                return
            accepted_socket.sendall(_HTTP_RESPONSE)
            self.response_started_at = time.monotonic()
        except BaseException as exc:
            if not self._stopping.is_set():
                self._error = exc
        finally:
            if accepted_socket is not None:
                try:
                    accepted_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                accepted_socket.close()
            self._terminal.set()

    def _close(self):
        self._stopping.set()
        self._release.set()
        if self._listener is not None:
            self._listener.close()
        if self._thread is not None:
            self._thread.join(timeout=3)
        if self._tls_owner is not None:
            self._tls_owner.__exit__(None, None, None)

    def assert_clean(self, testcase):
        testcase.assertIsNone(self._error)
        testcase.assertTrue(self._terminal.is_set())
        testcase.assertIsNotNone(self._thread)
        testcase.assertFalse(self._thread.is_alive())
        testcase.assertEqual(self._listener.fileno(), -1)
        if self._accepted_socket is not None:
            testcase.assertEqual(self._accepted_socket.fileno(), -1)
        testcase.assertEqual(repr(self._tls_owner), "_EphemeralTlsContext(<closed>)")


class DurableGoogleLoginBrowserTestSupportTimeoutTests(unittest.TestCase):
    def setUp(self):
        self.connection_factory = _RecordingConnectionFactory()

    def _request(self, state, target="/synthetic", **kwargs):
        with mock.patch.object(
            support.http.client,
            "HTTPSConnection",
            self.connection_factory,
        ):
            return support.https_request(state, "GET", target, **kwargs)

    def _assert_single_closed_client(self, *, expected_socket_timeout):
        self.assertEqual(self.connection_factory.calls, 1)
        self.assertEqual(self.connection_factory.connect_calls, 1)
        self.assertEqual(len(self.connection_factory.instances), 1)
        connection = self.connection_factory.instances[0]
        self.assertIsNone(connection.sock)
        self.assertEqual(len(self.connection_factory.connected_sockets), 1)
        for connected_socket in self.connection_factory.connected_sockets:
            self.assertEqual(
                connected_socket.gettimeout(),
                expected_socket_timeout,
            )
            self.assertEqual(connected_socket.fileno(), -1)

    def _assert_secret_free_timeout_graph(
        self,
        exception,
        *,
        expected_stage,
        expected_timeout,
        sentinels,
    ):
        self.assertEqual(exception.stage, expected_stage)
        self.assertEqual(exception.timeout_seconds, expected_timeout)
        self.assertIsNone(exception.__cause__)
        self.assertIsNone(exception.__context__)

        direct_values = (
            str(exception),
            repr(exception),
            repr(exception.args),
            repr(exception.__dict__),
            repr(getattr(exception, "__notes__", ())),
        )
        for sentinel in sentinels:
            for value in direct_values:
                self.assertNotIn(sentinel, value)

        helper_frames = []
        traceback = exception.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            normalized_path = frame.f_code.co_filename.replace("\\", "/")
            if normalized_path.endswith(
                "/tests/durable_google_login_browser_test_support.py"
            ):
                helper_frames.append(frame)
            traceback = traceback.tb_next

        self.assertEqual(
            tuple(frame.f_code.co_name for frame in helper_frames),
            ("https_request", "_raise_local_https_timeout"),
        )
        scrubbed_names = {
            "body",
            "connect_tls_write_timeout_seconds",
            "headers",
            "method",
            "outcome",
            "response_read_timeout_seconds",
            "state",
            "target",
        }
        forbidden_names = {
            "connection",
            "context",
            "request_headers",
            "request_socket",
            "response",
        }
        forbidden_types = (
            BaseException,
            TracebackType,
            http.client.HTTPConnection,
            http.client.HTTPResponse,
            socket.socket,
            ssl.SSLSocket,
        )
        for frame in helper_frames:
            for name in scrubbed_names.intersection(frame.f_locals):
                self.assertIsNone(frame.f_locals[name], name)
            self.assertFalse(forbidden_names.intersection(frame.f_locals))
            for name, value in frame.f_locals.items():
                self.assertNotIsInstance(value, forbidden_types, name)
                rendered = repr(value)
                for sentinel in sentinels:
                    self.assertNotIn(sentinel, rendered, name)

    def test_fast_response_uses_split_default_timeout_contract(self):
        self.assertEqual(
            support.LOCAL_HTTPS_CONNECT_TLS_WRITE_TIMEOUT_SECONDS,
            5,
        )
        self.assertEqual(
            support.LOCAL_HTTPS_RESPONSE_READ_TIMEOUT_SECONDS,
            15,
        )
        server = _SyntheticLocalTlsServer()
        with server:
            response = self._request(server.state)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.body, b"OK")
            self._assert_single_closed_client(expected_socket_timeout=15)
        server.assert_clean(self)
        self.assertEqual(server.accept_count, 1)
        self.assertEqual(server.request_count, 1)

    def test_status_line_after_old_budget_succeeds_before_new_budget(self):
        server = _SyntheticLocalTlsServer(response_delay_seconds=6.25)
        with server:
            response = self._request(server.state)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.body, b"OK")
            self._assert_single_closed_client(expected_socket_timeout=15)
        server.assert_clean(self)
        self.assertEqual(server.accept_count, 1)
        self.assertEqual(server.request_count, 1)
        self.assertGreaterEqual(
            server.response_started_at - server.request_received_at,
            6,
        )

    def test_missing_status_line_has_bounded_secret_safe_response_timeout(self):
        secret_target = (
            "/callback?state=timeout-state-secret&code=timeout-code-secret"
        )
        server = _SyntheticLocalTlsServer(response_delay_seconds=None)
        with server:
            started = time.monotonic()
            with self.assertRaises(support.LocalHttpsRequestTimeout) as caught:
                self._request(
                    server.state,
                    secret_target,
                    headers=(("Cookie", "session=timeout-cookie-secret"),),
                    response_read_timeout_seconds=0.2,
                )
            elapsed = time.monotonic() - started
            self.assertEqual(
                caught.exception.stage,
                "response status/header/body read",
            )
            self.assertEqual(caught.exception.timeout_seconds, 0.2)
            diagnostic = str(caught.exception)
            self.assertIn("response status/header/body read", diagnostic)
            self.assertIn("0.2 seconds", diagnostic)
            for secret in (
                secret_target,
                "timeout-state-secret",
                "timeout-code-secret",
                "timeout-cookie-secret",
            ):
                self.assertNotIn(secret, diagnostic)
            self.assertLess(elapsed, 2)
            self._assert_single_closed_client(expected_socket_timeout=0.2)
        server.assert_clean(self)
        self.assertEqual(server.accept_count, 1)
        self.assertEqual(server.request_count, 1)

    def test_response_body_read_remains_bounded(self):
        server = _SyntheticLocalTlsServer(hold_partial_body=True)
        with server:
            with self.assertRaises(support.LocalHttpsRequestTimeout) as caught:
                self._request(
                    server.state,
                    response_read_timeout_seconds=0.2,
                )
            self.assertEqual(
                caught.exception.stage,
                "response status/header/body read",
            )
            self._assert_single_closed_client(expected_socket_timeout=0.2)
        server.assert_clean(self)
        self.assertEqual(server.accept_count, 1)
        self.assertEqual(server.request_count, 1)
        self.assertIsNotNone(server.response_started_at)

    def test_tls_handshake_stall_keeps_short_phase_budget(self):
        server = _SyntheticLocalTlsServer(complete_tls_handshake=False)
        with server:
            started = time.monotonic()
            with self.assertRaises(support.LocalHttpsRequestTimeout) as caught:
                self._request(
                    server.state,
                    connect_tls_write_timeout_seconds=0.2,
                )
            elapsed = time.monotonic() - started
            self.assertEqual(
                caught.exception.stage,
                "connect/TLS/request-write",
            )
            self.assertEqual(caught.exception.timeout_seconds, 0.2)
            self.assertIn("0.2 seconds", str(caught.exception))
            self.assertLess(elapsed, 2)
            self._assert_single_closed_client(expected_socket_timeout=0.2)
        server.assert_clean(self)
        self.assertEqual(server.accept_count, 1)
        self.assertEqual(server.request_count, 0)

    def test_connect_timeout_exception_graph_does_not_retain_request_secrets(self):
        sentinels = (
            "connect-target-query-sentinel",
            "connect-header-value-sentinel",
            "connect-cookie-value-sentinel",
            "connect-request-body-sentinel",
        )
        server = _SyntheticLocalTlsServer(complete_tls_handshake=False)
        with server:
            caught = None
            try:
                self._request(
                    server.state,
                    "/callback?state=" + sentinels[0],
                    headers=(
                        ("X-Synthetic-Secret", sentinels[1]),
                        ("Cookie", "session=" + sentinels[2]),
                    ),
                    body=sentinels[3].encode("ascii"),
                    connect_tls_write_timeout_seconds=0.2,
                )
            except support.LocalHttpsRequestTimeout as exc:
                caught = exc
            self.assertIsNotNone(caught)
            self._assert_secret_free_timeout_graph(
                caught,
                expected_stage="connect/TLS/request-write",
                expected_timeout=0.2,
                sentinels=sentinels,
            )
            self._assert_single_closed_client(expected_socket_timeout=0.2)
        server.assert_clean(self)
        self.assertEqual(server.accept_count, 1)
        self.assertEqual(server.request_count, 0)

    def test_response_timeout_exception_graph_does_not_retain_request_secrets(self):
        sentinels = (
            "response-target-query-sentinel",
            "response-header-value-sentinel",
            "response-cookie-value-sentinel",
            "response-request-body-sentinel",
        )
        server = _SyntheticLocalTlsServer(response_delay_seconds=None)
        with server:
            caught = None
            try:
                self._request(
                    server.state,
                    "/callback?code=" + sentinels[0],
                    headers=(
                        ("X-Synthetic-Secret", sentinels[1]),
                        ("Cookie", "session=" + sentinels[2]),
                    ),
                    body=sentinels[3].encode("ascii"),
                    response_read_timeout_seconds=0.2,
                )
            except support.LocalHttpsRequestTimeout as exc:
                caught = exc
            self.assertIsNotNone(caught)
            self._assert_secret_free_timeout_graph(
                caught,
                expected_stage="response status/header/body read",
                expected_timeout=0.2,
                sentinels=sentinels,
            )
            self._assert_single_closed_client(expected_socket_timeout=0.2)
        server.assert_clean(self)
        self.assertEqual(server.accept_count, 1)
        self.assertEqual(server.request_count, 1)

    def test_response_phase_rejects_socket_contract_failures(self):
        request_socket = _SettableSocket()
        connection = SimpleNamespace(sock=None)
        with self.assertRaisesRegex(
            RuntimeError,
            "local_https_socket_missing_before_response",
        ):
            support._apply_local_https_response_timeout(
                connection,
                request_socket,
                0.2,
            )

        connection.sock = _SettableSocket()
        with self.assertRaisesRegex(
            RuntimeError,
            "local_https_socket_replaced_before_response",
        ):
            support._apply_local_https_response_timeout(
                connection,
                request_socket,
                0.2,
            )

        request_socket = _SettableSocket(fail=True)
        connection.sock = request_socket
        with self.assertRaisesRegex(
            RuntimeError,
            "local_https_response_read_timeout_not_applied",
        ):
            support._apply_local_https_response_timeout(
                connection,
                request_socket,
                0.2,
            )


class _SettableSocket:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.timeout = None

    def settimeout(self, timeout):
        if self.fail:
            raise OSError("controlled_settimeout_failure")
        self.timeout = timeout


if __name__ == "__main__":
    unittest.main()
