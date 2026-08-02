"""Exclusive HTTP adapter for the durable candidate-product integration.

This module deliberately knows nothing about the local product application.  The
injected integration owns every method and target; the adapter only transfers an
unread request stream and writes a bounded, validated response.
"""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
import re


MAX_DURABLE_RESPONSE_BODY_BYTES = 1_048_576
MAX_DURABLE_RESPONSE_HEADERS = 128
MAX_DURABLE_RESPONSE_HEADER_BYTES = 16_384

_HTTP_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_FORBIDDEN_HEADER_VALUE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_CONTROL_FLOW = (KeyboardInterrupt, SystemExit, GeneratorExit)
_SAFE_FAILURE_BODY = b"The account page could not be loaded safely.\n"
_SAFE_FAILURE_HEADERS = (
    ("Content-Type", "text/plain; charset=utf-8"),
    ("Content-Length", str(len(_SAFE_FAILURE_BODY))),
    ("Cache-Control", "no-store"),
    (
        "Content-Security-Policy",
        "default-src 'none'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'",
    ),
    ("Referrer-Policy", "no-referrer"),
    ("X-Content-Type-Options", "nosniff"),
)


@dataclass(frozen=True, slots=True)
class _ValidatedDurableResponse:
    status: int
    body: bytes
    headers: tuple[tuple[str, str], ...]
    acknowledge_delivery: object | None
    fail_delivery: object | None


def make_durable_product_browser_handler(durable_integration):
    """Return a handler class whose complete route space belongs to integration."""

    if not callable(getattr(durable_integration, "handle", None)):
        raise ValueError("invalid_durable_product_browser_integration")

    class DurableProductBrowserHandler(BaseHTTPRequestHandler):
        _durable_google_login_browser_integration = durable_integration

        def __getattr__(self, name):
            if type(name) is str and name.startswith("do_"):
                method = name[3:]
                if _HTTP_TOKEN.fullmatch(method) is not None:
                    return lambda: self._dispatch_durable_request(method)
            raise AttributeError(name)

        def _dispatch_durable_request(self, method):
            integration = type(
                self
            )._durable_google_login_browser_integration
            if integration is None:
                self._write_safe_failure(head=method == "HEAD")
                return

            response = None
            try:
                response = integration.handle(
                    method,
                    self.path,
                    self.headers,
                    self.rfile,
                )
            except _CONTROL_FLOW:
                raise
            except Exception:
                self._write_safe_failure(head=method == "HEAD")
                return

            try:
                validated = _validate_durable_response(response)
            except _CONTROL_FLOW:
                _fail_unvalidated_delivery(response)
                raise
            except Exception:
                _fail_unvalidated_delivery(response)
                self._write_safe_failure(head=method == "HEAD")
                return

            self._write_durable_response(
                validated,
                head=method == "HEAD",
            )

        def _write_durable_response(self, response, *, head):
            headers_complete = False
            try:
                self.send_response(response.status)
                for name, value in response.headers:
                    self.send_header(name, value)
                self.end_headers()
                headers_complete = True
            except _CONTROL_FLOW:
                if not headers_complete:
                    _call_delivery_failure(response.fail_delivery)
                self._clear_pending_headers()
                raise
            except Exception:
                if not headers_complete:
                    _call_delivery_failure(response.fail_delivery)
                self._clear_pending_headers()
                return

            if response.acknowledge_delivery is not None:
                try:
                    response.acknowledge_delivery()
                except _CONTROL_FLOW:
                    raise
                except Exception:
                    return

            if head:
                return
            try:
                self.wfile.write(response.body)
            except _CONTROL_FLOW:
                raise
            except Exception:
                return

        def _write_safe_failure(self, *, head):
            self._clear_pending_headers()
            try:
                self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
                for name, value in _SAFE_FAILURE_HEADERS:
                    self.send_header(name, value)
                self.end_headers()
                if not head:
                    self.wfile.write(_SAFE_FAILURE_BODY)
            except _CONTROL_FLOW:
                self._clear_pending_headers()
                raise
            except Exception:
                self._clear_pending_headers()

        def _clear_pending_headers(self):
            try:
                pending = getattr(self, "_headers_buffer", None)
            except BaseException:
                return
            if type(pending) is list:
                try:
                    pending.clear()
                except BaseException:
                    return

        def log_message(self, _format, *_args):
            return

    DurableProductBrowserHandler.__name__ = "DurableProductBrowserHandler"
    DurableProductBrowserHandler.__qualname__ = "DurableProductBrowserHandler"
    return DurableProductBrowserHandler


def _validate_durable_response(response):
    status = getattr(response, "status", None)
    body = getattr(response, "body", None)
    headers = getattr(response, "headers", None)
    if (
        type(status) is not int
        or not 100 <= status <= 599
        or type(body) is not bytes
        or len(body) > MAX_DURABLE_RESPONSE_BODY_BYTES
        or type(headers) is not tuple
        or len(headers) > MAX_DURABLE_RESPONSE_HEADERS
    ):
        raise ValueError("invalid_durable_product_browser_response")

    header_bytes = 0
    for header in headers:
        if type(header) is not tuple or len(header) != 2:
            raise ValueError("invalid_durable_product_browser_response")
        name, value = header
        if (
            type(name) is not str
            or _HTTP_TOKEN.fullmatch(name) is None
            or type(value) is not str
            or _FORBIDDEN_HEADER_VALUE.search(value) is not None
        ):
            raise ValueError("invalid_durable_product_browser_response")
        try:
            header_bytes += len(name.encode("ascii"))
            header_bytes += len(value.encode("latin-1"))
        except UnicodeError:
            raise ValueError(
                "invalid_durable_product_browser_response"
            ) from None
        header_bytes += 4
        if header_bytes > MAX_DURABLE_RESPONSE_HEADER_BYTES:
            raise ValueError("invalid_durable_product_browser_response")

    acknowledge = getattr(response, "acknowledge_delivery", None)
    fail = getattr(response, "fail_delivery", None)
    if (acknowledge is None) != (fail is None) or (
        acknowledge is not None
        and (not callable(acknowledge) or not callable(fail))
    ):
        raise ValueError("invalid_durable_product_browser_response")

    return _ValidatedDurableResponse(
        status=status,
        body=body,
        headers=headers,
        acknowledge_delivery=acknowledge,
        fail_delivery=fail,
    )


def _fail_unvalidated_delivery(response):
    try:
        fail = getattr(response, "fail_delivery", None)
    except BaseException:
        return
    _call_delivery_failure(fail)


def _call_delivery_failure(fail):
    if not callable(fail):
        return
    try:
        fail()
    except BaseException:
        return


__all__ = [
    "MAX_DURABLE_RESPONSE_BODY_BYTES",
    "MAX_DURABLE_RESPONSE_HEADER_BYTES",
    "MAX_DURABLE_RESPONSE_HEADERS",
    "make_durable_product_browser_handler",
]
