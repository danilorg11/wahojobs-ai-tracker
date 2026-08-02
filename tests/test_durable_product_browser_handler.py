from email.message import Message
import io
from types import SimpleNamespace
import unittest

from wahojobs import durable_product_browser_handler as handler_module
from wahojobs.durable_product_browser_handler import (
    MAX_DURABLE_RESPONSE_BODY_BYTES,
    MAX_DURABLE_RESPONSE_HEADER_BYTES,
    MAX_DURABLE_RESPONSE_HEADERS,
    make_durable_product_browser_handler,
)


class _ReadCounter(io.BytesIO):
    def __init__(self, body=b""):
        super().__init__(body)
        self.read_count = 0

    def read(self, *args, **kwargs):
        self.read_count += 1
        return super().read(*args, **kwargs)


class _Writer:
    def __init__(self, events, *, failure=None):
        self.events = events
        self.failure = failure

    def write(self, payload):
        self.events.append(("write", payload))
        if self.failure is not None:
            raise self.failure
        return len(payload)


class _Integration:
    def __init__(self, response=None, *, failure=None, events=None):
        self.response = response
        self.failure = failure
        self.calls = []
        self.events = events

    def matches_route(self, _path):
        raise AssertionError("exclusive handler must not ask about local routes")

    def handle(self, method, target, headers, body_stream):
        if self.events is not None:
            self.events.append(("integration", method, target))
        self.calls.append((method, target, headers, body_stream))
        if self.failure is not None:
            raise self.failure
        return self.response


class _DeliveryResponse:
    def __init__(
        self,
        events,
        *,
        status=200,
        body=b"durable response",
        headers=None,
        acknowledge_failure=None,
        fail_failure=None,
    ):
        self.status = status
        self.body = body
        self.headers = headers or (
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(body))),
        )
        self.events = events
        self.acknowledge_failure = acknowledge_failure
        self.fail_failure = fail_failure

    def acknowledge_delivery(self):
        self.events.append(("acknowledge",))
        if self.acknowledge_failure is not None:
            raise self.acknowledge_failure

    def fail_delivery(self):
        self.events.append(("fail",))
        if self.fail_failure is not None:
            raise self.fail_failure


def _plain_response(*, status=200, body=b"ok", headers=None):
    return SimpleNamespace(
        status=status,
        body=body,
        headers=headers
        if headers is not None
        else (
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ),
    )


def _handler(
    integration,
    *,
    target="/account/profile",
    body=b"request-body",
    events=None,
    writer_failure=None,
    send_response_failure=None,
    send_header_failure=None,
    end_headers_failure=None,
):
    events = events if events is not None else []
    handler_type = make_durable_product_browser_handler(integration)
    instance = object.__new__(handler_type)
    instance.path = target
    instance.headers = Message()
    instance.headers["Host"] = "localhost:8443"
    instance.rfile = _ReadCounter(body)
    instance.wfile = _Writer(events, failure=writer_failure)
    instance._headers_buffer = []

    def send_response(status):
        events.append(("send_response", int(status)))
        if send_response_failure is not None:
            raise send_response_failure

    def send_header(name, value):
        events.append(("send_header", name, value))
        if send_header_failure is not None:
            raise send_header_failure

    def end_headers():
        events.append(("end_headers",))
        if end_headers_failure is not None:
            raise end_headers_failure

    instance.send_response = send_response
    instance.send_header = send_header
    instance.end_headers = end_headers
    return instance, handler_type, events


class DurableProductBrowserHandlerTests(unittest.TestCase):
    def test_every_method_and_target_is_forwarded_exclusively_with_unread_stream(self):
        integration = _Integration(_plain_response())
        cases = (
            ("GET", "/"),
            ("POST", "/action?account_id=browser-selected"),
            ("HEAD", "/find-matches?profile_id=browser-selected"),
            ("BREW", "/preview"),
        )

        for method, target in cases:
            with self.subTest(method=method, target=target):
                handler, handler_type, events = _handler(
                    integration,
                    target=target,
                    body=b"opaque-body",
                )
                headers = handler.headers
                stream = handler.rfile

                getattr(handler, f"do_{method}")()

                call = integration.calls[-1]
                self.assertEqual(call[:2], (method, target))
                self.assertIs(call[2], headers)
                self.assertIs(call[3], stream)
                self.assertEqual(stream.read_count, 0)
                writes = [event for event in events if event[0] == "write"]
                if method == "HEAD":
                    self.assertEqual(writes, [])
                else:
                    self.assertEqual(writes, [("write", b"ok")])
                self.assertIs(
                    handler_type._durable_google_login_browser_integration,
                    integration,
                )

        self.assertNotIn("local_product_app", handler_module.__dict__)

    def test_factory_rejects_invalid_integration_and_detachment_fails_closed(self):
        for integration in (None, object(), SimpleNamespace(handle=None)):
            with self.subTest(integration=type(integration).__name__):
                with self.assertRaisesRegex(
                    ValueError,
                    "invalid_durable_product_browser_integration",
                ):
                    make_durable_product_browser_handler(integration)

        integration = _Integration(_plain_response())
        handler, handler_type, events = _handler(integration)
        handler_type._durable_google_login_browser_integration = None

        handler.do_GET()

        self.assertEqual(integration.calls, [])
        self.assertIn(("send_response", 503), events)
        self.assertIn(
            ("send_header", "Cache-Control", "no-store"),
            events,
        )

    def test_ordinary_integration_failure_returns_fixed_no_store_503(self):
        marker = "private-integration-detail"
        integration = _Integration(failure=RuntimeError(marker))
        handler, _handler_type, events = _handler(integration)

        handler.do_POST()

        self.assertEqual(handler.rfile.read_count, 0)
        self.assertIn(("send_response", 503), events)
        self.assertIn(
            ("send_header", "Cache-Control", "no-store"),
            events,
        )
        rendered = repr(events)
        self.assertNotIn(marker, rendered)
        self.assertIn(
            ("write", b"The account page could not be loaded safely.\n"),
            events,
        )

    def test_head_suppresses_success_and_failure_bodies(self):
        for response, failure in (
            (_plain_response(body=b"secret-head-body"), None),
            (None, RuntimeError("private")),
        ):
            with self.subTest(failure=failure is not None):
                integration = _Integration(response, failure=failure)
                handler, _handler_type, events = _handler(integration)

                handler.do_HEAD()

                self.assertFalse(
                    any(event[0] == "write" for event in events)
                )
                expected = 503 if failure is not None else 200
                self.assertIn(("send_response", expected), events)

    def test_bounded_response_validation_rejects_invalid_shapes(self):
        valid_headers = (("Content-Length", "2"),)
        cases = (
            ("boolean-status", _plain_response(status=True)),
            ("low-status", _plain_response(status=99)),
            ("high-status", _plain_response(status=600)),
            (
                "non-bytes-body",
                SimpleNamespace(status=200, body="no", headers=valid_headers),
            ),
            (
                "oversized-body",
                _plain_response(body=b"x" * (MAX_DURABLE_RESPONSE_BODY_BYTES + 1)),
            ),
            (
                "non-tuple-headers",
                SimpleNamespace(status=200, body=b"ok", headers=[]),
            ),
            (
                "too-many-headers",
                _plain_response(
                    headers=tuple(
                        ("X-Test", "value")
                        for _index in range(MAX_DURABLE_RESPONSE_HEADERS + 1)
                    )
                ),
            ),
            ("malformed-header", _plain_response(headers=(("X-Test",),))),
            ("invalid-name", _plain_response(headers=(("Bad Name", "x"),))),
            (
                "newline-value",
                _plain_response(headers=(("X-Test", "one\r\ntwo"),)),
            ),
            (
                "non-latin-value",
                _plain_response(headers=(("X-Test", "snowman-\u2603"),)),
            ),
            (
                "oversized-headers",
                _plain_response(
                    headers=(("X-Test", "x" * MAX_DURABLE_RESPONSE_HEADER_BYTES),)
                ),
            ),
            (
                "ack-without-fail",
                SimpleNamespace(
                    status=200,
                    body=b"ok",
                    headers=valid_headers,
                    acknowledge_delivery=lambda: None,
                ),
            ),
        )

        for label, response in cases:
            with self.subTest(label=label):
                integration = _Integration(response)
                handler, _handler_type, events = _handler(integration)

                handler.do_GET()

                self.assertIn(("send_response", 503), events)
                self.assertIn(
                    ("send_header", "Cache-Control", "no-store"),
                    events,
                )
                self.assertNotIn(("write", b"ok"), events)

    def test_success_acknowledges_after_headers_and_before_body(self):
        events = []
        response = _DeliveryResponse(events)
        integration = _Integration(response, events=events)
        handler, _handler_type, _events = _handler(
            integration,
            events=events,
        )

        handler.do_GET()

        self.assertEqual(
            [event[0] for event in events],
            [
                "integration",
                "send_response",
                "send_header",
                "send_header",
                "end_headers",
                "acknowledge",
                "write",
            ],
        )
        self.assertNotIn(("fail",), events)

    def test_head_acknowledges_delivery_without_writing_body(self):
        events = []
        response = _DeliveryResponse(events, body=b"head-body")
        handler, _handler_type, _events = _handler(
            _Integration(response, events=events),
            events=events,
        )

        handler.do_HEAD()

        self.assertIn(("acknowledge",), events)
        self.assertNotIn(("fail",), events)
        self.assertFalse(any(event[0] == "write" for event in events))

    def test_failure_before_header_boundary_invokes_fail_not_acknowledge(self):
        boundaries = ("send_response", "send_header", "end_headers")
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                events = []
                response = _DeliveryResponse(events)
                options = {
                    "send_response_failure": None,
                    "send_header_failure": None,
                    "end_headers_failure": None,
                }
                options[f"{boundary}_failure"] = RuntimeError(
                    f"injected-{boundary}"
                )
                handler, _handler_type, _events = _handler(
                    _Integration(response, events=events),
                    events=events,
                    **options,
                )

                handler.do_GET()

                self.assertEqual(events.count(("fail",)), 1)
                self.assertNotIn(("acknowledge",), events)
                self.assertFalse(
                    any(event[0] == "write" for event in events)
                )

    def test_validation_failure_fails_delivery_before_safe_503(self):
        events = []
        response = _DeliveryResponse(events, status=700)
        handler, _handler_type, _events = _handler(
            _Integration(response, events=events),
            events=events,
        )

        handler.do_GET()

        self.assertEqual(events.count(("fail",)), 1)
        self.assertNotIn(("acknowledge",), events)
        self.assertLess(
            events.index(("fail",)),
            events.index(("send_response", 503)),
        )

    def test_post_boundary_failures_never_compensate_delivered_headers(self):
        for label, options in (
            (
                "acknowledge",
                {"acknowledge_failure": RuntimeError("injected-ack")},
            ),
            (
                "body",
                {"writer_failure": RuntimeError("injected-write")},
            ),
        ):
            with self.subTest(label=label):
                events = []
                writer_failure = options.pop("writer_failure", None)
                response = _DeliveryResponse(events, **options)
                handler, _handler_type, _events = _handler(
                    _Integration(response, events=events),
                    events=events,
                    writer_failure=writer_failure,
                )

                handler.do_GET()

                self.assertIn(("acknowledge",), events)
                self.assertNotIn(("fail",), events)

    def test_control_flow_from_integration_propagates_without_safe_response(self):
        injected = KeyboardInterrupt("stop")
        handler, _handler_type, events = _handler(
            _Integration(failure=injected)
        )

        with self.assertRaises(KeyboardInterrupt) as caught:
            handler.do_GET()

        self.assertIs(caught.exception, injected)
        self.assertFalse(any(event[0] == "send_response" for event in events))


if __name__ == "__main__":
    unittest.main()
