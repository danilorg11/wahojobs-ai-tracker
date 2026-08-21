"""Run the production-grade, guest-only public catalog origin."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import signal
import sys
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wahojobs.public_catalog_origin import (
    PublicCatalogOriginConfigurationError,
    PublicCatalogOriginIntegration,
    load_origin_auth_token,
    load_public_catalog_origin_configuration,
)


MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_RESPONSE_HEADERS = 128


class PublicCatalogOriginServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = False
    block_on_close = True


def make_handler(integration):
    class PublicCatalogOriginHandler(BaseHTTPRequestHandler):
        server_version = "WahojobsPublicCatalogOrigin/1"
        sys_version = ""

        def __getattr__(self, name):
            if isinstance(name, str) and name.startswith("do_"):
                method = name[3:]
                return lambda: self._dispatch(method)
            raise AttributeError(name)

        def _dispatch(self, method):
            started = time.monotonic()
            response = None
            status = 503
            route_class = integration.route_class(self.path)
            request_id = integration.request_id(self.headers) or secrets.token_urlsafe(18)
            try:
                response = integration.handle(
                    method,
                    self.path,
                    self.headers,
                    self.rfile,
                    loopback_peer=self.client_address[0] in {"127.0.0.1", "::1"},
                )
                status = _validated_status(response)
                body = _validated_body(response)
                headers = _validated_headers(response)
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("X-Wahojobs-Origin-Request-Id", request_id)
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(body)
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                raise
            except Exception:
                try:
                    body = b"Unavailable\n"
                    self.send_response(503)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Wahojobs-Origin-Request-Id", request_id)
                    self.end_headers()
                    if method != "HEAD":
                        self.wfile.write(body)
                except Exception:
                    pass
            finally:
                _log_access(
                    request_id=request_id,
                    method=method,
                    route_class=route_class,
                    status=status,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

        def log_message(self, _format, *_args):
            return

    return PublicCatalogOriginHandler


def _validated_status(response):
    status = getattr(response, "status", None)
    if type(status) is not int or not 100 <= status <= 599:
        raise ValueError
    return status


def _validated_body(response):
    body = getattr(response, "body", None)
    if type(body) is not bytes or len(body) > MAX_RESPONSE_BYTES:
        raise ValueError
    return body


def _validated_headers(response):
    headers = getattr(response, "headers", None)
    if type(headers) is not tuple or len(headers) > MAX_RESPONSE_HEADERS:
        raise ValueError
    result = []
    for header in headers:
        if type(header) is not tuple or len(header) != 2:
            raise ValueError
        key, value = header
        if (
            type(key) is not str
            or type(value) is not str
            or not key
            or "\r" in key
            or "\n" in key
            or "\r" in value
            or "\n" in value
        ):
            raise ValueError
        result.append((key, value))
    return tuple(result)


def _log_access(*, request_id, method, route_class, status, duration_ms):
    event = {
        "event": "public_catalog_origin_access",
        "request_id": request_id,
        "method": method if method in {"GET", "HEAD"} else "other",
        "route": route_class,
        "status": status,
        "duration_ms": max(0, min(duration_ms, 3_600_000)),
    }
    print(json.dumps(event, sort_keys=True, separators=(",", ":")), file=sys.stderr, flush=True)


def serve(configuration_path):
    configuration = load_public_catalog_origin_configuration(configuration_path)
    token = load_origin_auth_token()
    integration = PublicCatalogOriginIntegration(
        configuration, origin_auth_token=token
    )
    server = PublicCatalogOriginServer(
        configuration.bind_address, make_handler(integration)
    )
    stopping = threading.Event()

    def stop(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    _log_access(
        request_id="startup-" + secrets.token_urlsafe(12),
        method="GET",
        route_class="startup",
        status=200,
        duration_ms=0,
    )
    try:
        server.timeout = 0.25
        while not stopping.is_set():
            server.handle_request()
    finally:
        server.server_close()
        integration.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--configuration", required=True)
    arguments = parser.parse_args(argv)
    try:
        serve(arguments.configuration)
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except PublicCatalogOriginConfigurationError:
        print("Public catalog origin configuration is unavailable.", file=sys.stderr)
        return 2
    except Exception:
        print("Public catalog origin could not start safely.", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
