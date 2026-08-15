"""Run the explicit local HTTPS WorkOS AuthKit Staging rehearsal."""

from __future__ import annotations

import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path
import socket
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from wahojobs.durable_product_browser_handler import (
    make_durable_product_browser_handler,
)
from wahojobs.workos_authkit_staging import (
    WorkOSAuthKitStagingError,
    build_workos_authkit_staging_runtime,
    load_workos_authkit_staging_configuration,
)


_TLS_HANDSHAKE_TIMEOUT_SECONDS = 5.0
_REQUEST_TIMEOUT_SECONDS = 30.0


class _LocalAuthKitHttpsServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, handler, tls_context):
        if not callable(getattr(tls_context, "wrap_socket", None)):
            raise WorkOSAuthKitStagingError("runtime_unavailable")
        self._tls_context = tls_context
        super().__init__(address, handler)

    def get_request(self):
        raw_socket = None
        try:
            raw_socket, address = self.socket.accept()
            raw_socket.settimeout(_TLS_HANDSHAKE_TIMEOUT_SECONDS)
            wrapped = self._tls_context.wrap_socket(
                raw_socket,
                server_side=True,
            )
            raw_socket = None
            wrapped.settimeout(_REQUEST_TIMEOUT_SECONDS)
            return wrapped, address
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            if raw_socket is not None:
                raw_socket.close()
            raise
        except Exception:
            if raw_socket is not None:
                raw_socket.close()
            raise

    def server_close(self):
        try:
            super().server_close()
        finally:
            self._tls_context = None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the explicit local HTTPS WorkOS AuthKit Staging rehearsal."
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Absolute path to the external permission-restricted JSON configuration.",
    )
    return parser.parse_args(argv)


def run_staging_rehearsal(
    configuration_path,
    *,
    runtime_builder=build_workos_authkit_staging_runtime,
    tls_scope_factory=None,
    server_factory=None,
    ready=None,
):
    """Build, serve, and cleanly close one explicit local rehearsal."""

    if not callable(runtime_builder):
        raise WorkOSAuthKitStagingError("runtime_unavailable")
    if tls_scope_factory is None:
        tls_scope_factory = _existing_ephemeral_tls_scope
    if server_factory is None:
        server_factory = _LocalAuthKitHttpsServer
    if ready is None:
        ready = lambda origin: print(
            f"WorkOS AuthKit Staging listening at {origin}",
            flush=True,
        )
    if not all(callable(item) for item in (tls_scope_factory, server_factory, ready)):
        raise WorkOSAuthKitStagingError("runtime_unavailable")

    configuration = None
    runtime = None
    tls_scope = None
    server = None
    cleanup_failed = False
    try:
        configuration = load_workos_authkit_staging_configuration(configuration_path)
        runtime = runtime_builder(configuration)
        configuration.clear_secrets()
        configuration = None
        try:
            tls_scope = tls_scope_factory()
            build_context = getattr(tls_scope, "build_context", None)
            if not callable(build_context):
                raise TypeError("invalid_tls_scope")
            tls_context = build_context()
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception as exc:
            _detach_exception(exc)
            raise WorkOSAuthKitStagingError("tls_unavailable") from None
        handler = make_durable_product_browser_handler(runtime.browser_integration)
        try:
            server = server_factory(runtime.bind_address, handler, tls_context)
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception as exc:
            _detach_exception(exc)
            raise WorkOSAuthKitStagingError("listener_unavailable") from None
        ready(runtime.public_origin)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        return True
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except WorkOSAuthKitStagingError:
        raise
    except (OSError, socket.error, ValueError, TypeError) as exc:
        _detach_exception(exc)
        raise WorkOSAuthKitStagingError("runtime_unavailable") from None
    except Exception as exc:
        _detach_exception(exc)
        raise WorkOSAuthKitStagingError("runtime_unavailable") from None
    finally:
        if configuration is not None:
            configuration.clear_secrets()
        for resource, operation in (
            (server, "server_close"),
            (runtime, "close"),
            (tls_scope, "close"),
        ):
            if resource is None:
                continue
            try:
                close = getattr(resource, operation, None)
                if callable(close) and close() is False:
                    cleanup_failed = True
            except BaseException as exc:
                cleanup_failed = True
                _detach_exception(exc)
        if cleanup_failed and sys.exc_info()[0] is None:
            raise WorkOSAuthKitStagingError("shutdown_incomplete")


def main(argv=None):
    arguments = parse_args(argv)
    try:
        run_staging_rehearsal(arguments.config)
    except WorkOSAuthKitStagingError as exc:
        print(
            f"WorkOS AuthKit Staging failed: {exc.code}",
            file=sys.stderr,
        )
        return 2
    print("WorkOS AuthKit Staging stopped cleanly.", flush=True)
    return 0


def _existing_ephemeral_tls_scope():
    from scripts.durable_google_login_app import _ephemeral_tls_context

    return _ephemeral_tls_context()


def _detach_exception(exc):
    try:
        exc.__traceback__ = None
        exc.__cause__ = None
        exc.__context__ = None
    except (AttributeError, TypeError):
        pass


if __name__ == "__main__":
    raise SystemExit(main())
