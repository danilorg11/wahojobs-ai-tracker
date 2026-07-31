"""Development/test-only controlled browser demo for durable Google login."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import hmac
import html
from http import HTTPStatus
from pathlib import Path
import re
import socket
import sys
import threading
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


FIXTURE_APPROVAL_ROUTE = "/__fixture/google/approve"
FIXTURE_COMPLETE_ROUTE = "/__fixture/google/complete"
_FIXTURE_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_FIXTURE_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Run a temporary, no-egress durable Google-login browser fixture."
        )
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run the full flow automatically and exit.",
    )
    parser.add_argument(
        "--restart-before-callback",
        action="store_true",
        help="In smoke mode, reconstruct the runtime before callback.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    port = _reserve_loopback_port()
    from tests.durable_google_login_browser_test_support import (
        loopback_and_in_memory_provider_only,
        temporary_browser_login_state,
    )

    with ExitStack() as stack:
        state = stack.enter_context(
            temporary_browser_login_state(port=port)
        )
        stack.enter_context(loopback_and_in_memory_provider_only())
        if args.smoke:
            return _run_smoke(
                state,
                restart_before_callback=args.restart_before_callback,
            )
        runtime, server, thread = _start_fixture_server(state)
        stack.callback(_stop_fixture_server, runtime, server, thread)
        print("Wahojobs controlled durable-login demo")
        print(f"Open: {state.public_origin}/login")
        print("The certificate and provider are temporary local fixtures.")
        print("No non-loopback network connection is permitted.")
        print("Press Ctrl+C to stop and remove the temporary demo directory.")
        try:
            thread.join()
        except KeyboardInterrupt:
            return 0
    return 0


class _ControlledProviderBridge:
    __slots__ = ("_delegate", "_state", "_authorization_store", "_lock")

    def __init__(self, delegate, state, authorization_store):
        self._delegate = delegate
        self._state = state
        self._authorization_store = authorization_store
        self._lock = threading.Lock()

    def matches_route(self, path):
        return path in {FIXTURE_APPROVAL_ROUTE, FIXTURE_COMPLETE_ROUTE} or (
            self._delegate.matches_route(path)
        )

    def handle(self, method, target, headers, body_stream=None):
        parsed = urlsplit(target)
        fixture_route = parsed.path in {
            FIXTURE_APPROVAL_ROUTE,
            FIXTURE_COMPLETE_ROUTE,
        }
        if fixture_route and not _trusted_fixture_request(
            self._state,
            target,
            headers,
        ):
            return _fixture_html_response(
                HTTPStatus.BAD_REQUEST,
                "Fixture request rejected",
                "<p>Start the controlled login again.</p>",
            )
        if parsed.path == FIXTURE_APPROVAL_ROUTE:
            if method not in {"GET", "HEAD"} or parsed.query:
                return _fixture_method_or_request_failure(method)
            return _fixture_html_response(
                HTTPStatus.OK,
                "Controlled Google approval",
                "<p>This test-only page represents Google consent.</p>"
                f"<p><a href='{FIXTURE_COMPLETE_ROUTE}'>"
                "Approve fixture login</a></p>",
            )
        if parsed.path == FIXTURE_COMPLETE_ROUTE:
            if method != "GET" or parsed.query:
                return _fixture_method_or_request_failure(method)
            with self._lock:
                authorization_url = (
                    self._authorization_store.pop()
                    if self._authorization_store
                    else None
                )
            if authorization_url is None:
                return _fixture_html_response(
                    HTTPStatus.BAD_REQUEST,
                    "Approval unavailable",
                    "<p>Start a new login.</p>",
                )
            from tests.durable_google_login_browser_test_support import (
                provider_callback_for,
            )

            callback_url = provider_callback_for(
                self._state,
                authorization_url,
            )
            callback = urlsplit(callback_url)
            return _fixture_redirect(
                callback.path + "?" + callback.query
            )

        response = self._delegate.handle(
            method,
            target,
            headers,
            body_stream,
        )
        locations = [
            value
            for name, value in response.headers
            if name.lower() == "location"
        ]
        if (
            parsed.path == "/auth/google/start"
            and response.status == HTTPStatus.SEE_OTHER
            and len(locations) == 1
            and locations[0].startswith(
                "https://accounts.google.com/o/oauth2/v2/auth?"
            )
        ):
            with self._lock:
                self._authorization_store[:] = [locations[0]]
            headers_without_location = tuple(
                (name, value)
                for name, value in response.headers
                if name.lower() != "location"
            )
            from wahojobs.durable_google_login_browser import (
                DurableGoogleLoginBrowserResponse,
            )

            return DurableGoogleLoginBrowserResponse(
                status=response.status,
                body=response.body,
                headers=(
                    *headers_without_location,
                    ("Location", FIXTURE_APPROVAL_ROUTE),
                ),
            )
        return response


def _start_fixture_server(state, authorization_store=None):
    from scripts.durable_google_login_app import (
        _DrainingThreadingHTTPServer,
        _ephemeral_tls_context,
    )
    from scripts.local_product_app import make_handler
    from wahojobs.durable_google_login_runtime import (
        build_durable_google_login_runtime,
    )

    runtime = None
    server = None
    tls_scope = None
    try:
        runtime = build_durable_google_login_runtime(
            state.configuration_path,
            _clock=state.clock,
            _gateway_factory=state.gateway_factory,
        )
        if authorization_store is None:
            authorization_store = []
        bridge = _ControlledProviderBridge(
            runtime.browser_integration,
            state,
            authorization_store,
        )
        handler = make_handler(
            durable_google_login_browser_integration=bridge,
            exclusive_browser_integration=True,
        )
        tls_scope = _ephemeral_tls_context()
        tls_context = tls_scope.__enter__()
        server = _DrainingThreadingHTTPServer(
            (
                runtime.configuration.bind_host,
                runtime.configuration.bind_port,
            ),
            handler,
            bind_and_activate=False,
        )
        server._wahojobs_tls_scope = tls_scope
        server.socket = tls_context.wrap_socket(
            server.socket,
            server_side=True,
        )
        server.server_bind()
        server.server_activate()
        thread = threading.Thread(
            target=server.serve_forever,
            name="durable-google-login-fixture-demo",
        )
        thread.start()
        return runtime, server, thread
    except BaseException:
        if server is not None:
            try:
                server.server_close()
            except BaseException:
                pass
        if tls_scope is not None:
            try:
                tls_scope.__exit__(None, None, None)
            except BaseException:
                pass
        if runtime is not None:
            try:
                runtime.close()
            except BaseException:
                pass
        raise


def _stop_fixture_server(runtime, server, thread):
    failures = []
    for operation in (
        server.shutdown,
        server.server_close,
        lambda: thread.join(timeout=5),
    ):
        try:
            operation()
        except BaseException as exc:
            failures.append(exc)
    tls_scope = getattr(server, "_wahojobs_tls_scope", None)
    if tls_scope is not None:
        server._wahojobs_tls_scope = None
        try:
            tls_scope.__exit__(None, None, None)
        except BaseException as exc:
            failures.append(exc)
    try:
        runtime.close()
    except BaseException as exc:
        failures.append(exc)
    if thread.is_alive():
        failures.append(RuntimeError("fixture_demo_server_did_not_stop"))
    if failures:
        raise failures[0]


def _run_smoke(state, *, restart_before_callback):
    from tests.durable_google_login_browser_test_support import (
        cookie_header,
        cookie_values,
        form_body,
        https_request,
    )

    authorization_store = []
    runtime, server, thread = _start_fixture_server(
        state,
        authorization_store,
    )
    cookies = {}
    try:
        login = https_request(state, "GET", "/login")
        _require_status(login, 200)
        _merge_cookies(cookies, cookie_values(login))
        start_body = form_body(
            csrf=cookies["__Host-wahojobs_login_csrf"]
        )
        start = https_request(
            state,
            "POST",
            "/auth/google/start",
            headers=(
                ("Origin", state.public_origin),
                ("Sec-Fetch-Site", "same-origin"),
                ("Content-Type", "application/x-www-form-urlencoded"),
                ("Content-Length", str(len(start_body))),
                ("Cookie", cookie_header(cookies)),
            ),
            body=start_body,
        )
        _require_status(start, 303)
        _merge_cookies(cookies, cookie_values(start))
        if start.header_values("Location") != (FIXTURE_APPROVAL_ROUTE,):
            raise RuntimeError("fixture_approval_redirect_missing")

        approval = https_request(
            state,
            "GET",
            FIXTURE_APPROVAL_ROUTE,
            headers=(("Cookie", cookie_header(cookies)),),
        )
        _require_status(approval, 200)

        if restart_before_callback:
            _stop_fixture_server(runtime, server, thread)
            runtime = server = thread = None
            state.close_harnesses()
            runtime, server, thread = _start_fixture_server(
                state,
                authorization_store,
            )

        complete = https_request(
            state,
            "GET",
            FIXTURE_COMPLETE_ROUTE,
            headers=(("Cookie", cookie_header(cookies)),),
        )
        _require_status(complete, 303)
        callback_target = complete.header_values("Location")[0]
        callback = https_request(
            state,
            "GET",
            callback_target,
            headers=(("Cookie", cookie_header(cookies)),),
        )
        _require_status(callback, 303)
        _merge_cookies(cookies, cookie_values(callback))

        for _attempt in range(2):
            profile = https_request(
                state,
                "GET",
                "/account/profile",
                headers=(("Cookie", cookie_header(cookies)),),
            )
            _require_status(profile, 200)

        logout_page = https_request(
            state,
            "GET",
            "/logout",
            headers=(("Cookie", cookie_header(cookies)),),
        )
        _require_status(logout_page, 200)
        logout_body = form_body(
            csrf=cookies["__Host-wahojobs_session_csrf"]
        )
        logout = https_request(
            state,
            "POST",
            "/logout",
            headers=(
                ("Origin", state.public_origin),
                ("Sec-Fetch-Site", "same-origin"),
                ("Content-Type", "application/x-www-form-urlencoded"),
                ("Content-Length", str(len(logout_body))),
                ("Cookie", cookie_header(cookies)),
            ),
            body=logout_body,
        )
        _require_status(logout, 303)
        old_session = cookies["wahojobs_session"]
        _merge_cookies(cookies, cookie_values(logout))
        rejected = https_request(
            state,
            "GET",
            "/account/profile",
            headers=(
                ("Cookie", f"wahojobs_session={old_session}"),
            ),
        )
        _require_status(rejected, 401)
        print(
            "fixture smoke passed: login, approval, callback, profile, "
            "refresh, logout, post-logout rejection"
        )
        return 0
    finally:
        if runtime is not None:
            _stop_fixture_server(runtime, server, thread)


def _fixture_html_response(status, title, content):
    from wahojobs.durable_google_login_browser import (
        DurableGoogleLoginBrowserResponse,
    )

    body = (
        "<!doctype html><html lang='en'><head>"
        "<meta charset='utf-8'><title>"
        + html.escape(title, quote=True)
        + "</title></head><body><main><h1>"
        + html.escape(title, quote=True)
        + "</h1>"
        + content
        + "</main></body></html>"
    ).encode("utf-8")
    return DurableGoogleLoginBrowserResponse(
        status=int(status),
        body=body,
        headers=(
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
            (
                "Content-Security-Policy",
                "default-src 'none'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'",
            ),
            ("Referrer-Policy", "no-referrer"),
            ("X-Content-Type-Options", "nosniff"),
            ("Cache-Control", "no-store"),
        ),
    )


def _fixture_redirect(location):
    from wahojobs.durable_google_login_browser import (
        DurableGoogleLoginBrowserResponse,
    )

    return DurableGoogleLoginBrowserResponse(
        status=int(HTTPStatus.SEE_OTHER),
        body=b"",
        headers=(
            ("Content-Length", "0"),
            ("Cache-Control", "no-store"),
            ("Location", location),
        ),
    )


def _fixture_method_or_request_failure(method):
    status = (
        HTTPStatus.METHOD_NOT_ALLOWED
        if method not in {"GET", "HEAD"}
        else HTTPStatus.BAD_REQUEST
    )
    return _fixture_html_response(
        status,
        "Fixture request rejected",
        "<p>Start the controlled login again.</p>",
    )


def _trusted_fixture_request(state, target, headers):
    if type(target) is not str:
        return False
    try:
        encoded_target = target.encode("ascii")
        parsed = urlsplit(target)
    except (UnicodeError, ValueError):
        return False
    if (
        not encoded_target
        or len(encoded_target) > 8_192
        or not target.startswith("/")
        or target.startswith("//")
        or "\\" in target
        or _FIXTURE_CONTROL_CHARACTERS.search(target) is not None
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
    ):
        return False
    try:
        raw_headers = (
            tuple(headers.raw_items())
            if hasattr(headers, "raw_items")
            else tuple(headers.items())
            if hasattr(headers, "items")
            else tuple(headers)
        )
    except Exception:
        return False
    if len(raw_headers) > 64:
        return False
    hosts = []
    origins = []
    for item in raw_headers:
        if type(item) is not tuple or len(item) != 2:
            return False
        name, value = item
        if (
            type(name) is not str
            or _FIXTURE_HEADER_NAME.fullmatch(name) is None
            or type(value) is not str
            or _FIXTURE_CONTROL_CHARACTERS.search(value) is not None
        ):
            return False
        lowered = name.lower()
        if lowered == "forwarded" or lowered.startswith("x-forwarded-"):
            return False
        if lowered == "host":
            hosts.append(value)
        elif lowered == "origin":
            origins.append(value)
    authority = urlsplit(state.public_origin).netloc
    if len(hosts) != 1 or not _constant_ascii_equal(hosts[0], authority):
        return False
    return len(origins) <= 1 and (
        not origins
        or _constant_ascii_equal(origins[0], state.public_origin)
    )


def _constant_ascii_equal(left, right):
    try:
        return hmac.compare_digest(
            left.encode("ascii", "strict"),
            right.encode("ascii", "strict"),
        )
    except (AttributeError, UnicodeError):
        return False


def _merge_cookies(current, updates):
    for name, value in updates.items():
        if value:
            current[name] = value
        else:
            current.pop(name, None)


def _require_status(response, expected):
    if response.status != expected:
        raise RuntimeError("fixture_demo_flow_failed")


def _reserve_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


if __name__ == "__main__":
    raise SystemExit(main())
