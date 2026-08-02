"""Run the explicitly configured durable Google-login browser surface."""

from __future__ import annotations

import argparse
import _socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import functools
from http.server import ThreadingHTTPServer
import ipaddress
import itertools
import os
from pathlib import Path
import signal
import socket
from socketserver import BaseServer
import ssl
import stat
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


_ORIGINAL_TCP_SERVER_GET_REQUEST = ThreadingHTTPServer.get_request
_ORIGINAL_SSL_CONTEXT_TYPE = ssl.SSLContext
_REQUEST_DRAIN_SECONDS = 2.0
_SERVE_THREAD_DRAIN_SECONDS = 2.0
_SERVE_STARTUP_SECONDS = 1.0
_SERVE_POLL_SECONDS = 0.05
_TLS_HANDSHAKE_SECONDS = 1.0
_MAX_TRACKED_ACCEPTED_SOCKETS = 64
_MISSING_PROFILE_CREATION_CAPABILITY = object()
_FIND_MATCHES_ROUTE = "/find-matches"
_SIGNAL_EXIT_STATUS = {
    "sigint": 130,
    "sigterm": 143,
    "sigbreak": 149,
}
_SHUTDOWN_RESOURCE_CATEGORIES = frozenset(
    {
        "secret_buffers",
        "google_gateway",
        "key_authority",
        "protection_authority",
        "lookup_authority",
        "database_descriptor",
        "database_attestation_connection",
        "database_connections",
        "profile_integration",
        "browser_integration",
        "inactive_server",
        "tls_workspace",
        "listener_socket",
        "route_integration",
        "request_threads",
        "accepted_sockets",
        "server_shutdown",
        "serve_thread",
        "signal_handlers",
    }
)


class _ServerCleanupFailure(Exception):
    __slots__ = ()


class _UnpublishedRequestHandler:
    _durable_google_login_browser_integration = None

    def __init__(self, *_args, **_kwargs):
        raise RuntimeError("durable_login_routes_not_published")


@dataclass(frozen=True, slots=True, repr=False)
class _ShutdownResult:
    ready_state_reached: bool
    shutdown_requested: bool
    resources_closed: tuple[str, ...]
    unresolved_resource_categories: tuple[str, ...]
    cleanup_complete: bool
    cleanup_failure_categories: tuple[str, ...]
    signal_category: str | None

    def __post_init__(self):
        category_fields = (
            self.resources_closed,
            self.unresolved_resource_categories,
            self.cleanup_failure_categories,
        )
        if (
            type(self.ready_state_reached) is not bool
            or type(self.shutdown_requested) is not bool
            or type(self.cleanup_complete) is not bool
            or any(type(field) is not tuple for field in category_fields)
            or any(
                type(category) is not str
                or category not in _SHUTDOWN_RESOURCE_CATEGORIES
                for field in category_fields
                for category in field
            )
            or len(self.resources_closed)
            != len(set(self.resources_closed))
            or len(self.unresolved_resource_categories)
            != len(set(self.unresolved_resource_categories))
            or set(self.resources_closed).intersection(
                self.unresolved_resource_categories
            )
            or self.cleanup_complete
            is not (not self.unresolved_resource_categories)
            or (
                self.signal_category is not None
                and self.signal_category not in _SIGNAL_EXIT_STATUS
            )
        ):
            raise RuntimeError("invalid_shutdown_result")

    def __repr__(self):
        return (
            "_ShutdownResult("
            f"ready={self.ready_state_reached}, "
            f"requested={self.shutdown_requested}, "
            f"closed={len(self.resources_closed)}, "
            f"unresolved={len(self.unresolved_resource_categories)}, "
            f"complete={self.cleanup_complete}, "
            f"failures={len(self.cleanup_failure_categories)}, "
            f"signal={self.signal_category!r})"
        )

    __str__ = __repr__


class _SignalShutdownState:
    __slots__ = (
        "_category",
        "_installed",
        "_lock",
        "_previous",
        "_raw_signal",
    )

    def __init__(self):
        self._category = None
        self._installed = False
        self._lock = threading.Lock()
        self._previous = ()
        self._raw_signal = None

    @property
    def event(self):
        return self

    @property
    def category(self):
        category = self._category
        if category is not None:
            return category
        raw_signal = self._raw_signal
        if isinstance(raw_signal, str):
            return raw_signal
        if raw_signal is not None:
            category = _signal_category(raw_signal)
            self._category = category
        return category

    @property
    def requested(self):
        return self._raw_signal is not None

    def is_set(self):
        return self._raw_signal is not None

    def wait(self, timeout=None):
        if self._raw_signal is not None:
            return True
        if timeout is not None and timeout > 0:
            time.sleep(float(timeout))
        return self._raw_signal is not None

    def request(self, category):
        if category not in _SIGNAL_EXIT_STATUS:
            raise RuntimeError("unsupported_shutdown_signal")
        if self._raw_signal is None:
            self._raw_signal = category
            self._category = category

    def install(self):
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("signal_installation_requires_main_thread")
        supported = (
            ("sigint", signal.SIGINT),
            ("sigterm", signal.SIGTERM),
        )
        if hasattr(signal, "SIGBREAK"):
            supported += (("sigbreak", signal.SIGBREAK),)
        with self._lock:
            if self._installed:
                return True
        try:
            for category, number in supported:
                previous = (number, signal.getsignal(number))
                with self._lock:
                    self._previous += (previous,)
                    self._installed = True
                signal.signal(number, self._handle)
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            primary = exc
            self._restore_preserving_primary()
            exc = None
            raise primary from None
        except Exception as exc:
            _sanitize_launcher_exception(exc)
            self._restore_preserving_primary()
            exc = None
            raise RuntimeError("signal_installation_failed") from None
        return True

    def _handle(self, number, _frame):
        if self._raw_signal is not None:
            return
        self._raw_signal = number
        self._category = _signal_category(self._raw_signal)

    def restore(self):
        with self._lock:
            previous = self._previous
            installed = self._installed
        if not installed:
            return True
        complete = self._restore_entries(previous)
        if complete:
            with self._lock:
                self._previous = ()
                self._installed = False
        return complete

    def _restore_preserving_primary(self):
        try:
            self.restore()
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            _sanitize_launcher_exception(exc)
            exc = None
        except Exception as exc:
            _sanitize_launcher_exception(exc)
            exc = None

    @staticmethod
    def _restore_entries(previous):
        complete = True
        first_control = None
        for number, handler in reversed(previous):
            try:
                signal.signal(number, handler)
            except (
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                if first_control is None:
                    first_control = exc
                _sanitize_launcher_exception(exc)
                exc = None
                complete = False
            except Exception as exc:
                _sanitize_launcher_exception(exc)
                exc = None
                complete = False
        if first_control is not None:
            propagated = first_control
            first_control = None
            raise propagated from None
        return complete

    def __repr__(self):
        return "_SignalShutdownState(<sealed>)"


class _ServeOutcome:
    __slots__ = (
        "_condition",
        "_ready_state_reached",
        "_state",
        "control",
        "done",
        "failed",
        "lock",
        "started",
    )

    def __init__(self):
        self.control = None
        self.done = threading.Event()
        self.failed = False
        self.lock = threading.RLock()
        self._condition = threading.Condition(self.lock)
        self._ready_state_reached = False
        self._state = "created"
        self.started = threading.Event()

    @property
    def state(self):
        with self.lock:
            return self._state

    @property
    def ready_state_reached(self):
        with self.lock:
            return self._ready_state_reached

    def begin_starting(self):
        with self._condition:
            if self._state != "created":
                return False
            self._state = "starting"
            self._condition.notify_all()
            return True

    def publish_thread_entry(self):
        self.started.set()

    def publish_serving_checkpoint(self, signal_state):
        with self._condition:
            if self._state != "starting":
                return False
            self._state = "serving"
            if signal_state.requested:
                self._state = "stopping"
            self._condition.notify_all()
            return self._state == "serving"

    def wait_for_startup(self, timeout, signal_state):
        if type(timeout) not in {int, float} or timeout <= 0:
            raise RuntimeError("invalid_serve_startup_timeout")
        deadline = time.monotonic() + float(timeout)
        with self._condition:
            while self._state == "starting":
                if signal_state.requested:
                    self._state = "stopping"
                    self._condition.notify_all()
                    return self._state
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.failed = True
                    self._state = "failed"
                    if signal_state.requested:
                        self.failed = False
                        self._state = "stopping"
                    self._condition.notify_all()
                    return self._state
                self._condition.wait(min(_SERVE_POLL_SECONDS, remaining))
            return self._state

    def claim_ready(self, signal_state):
        with self._condition:
            if self._state != "serving" or self._ready_state_reached:
                return False
            self._ready_state_reached = True
            if signal_state.requested:
                self._ready_state_reached = False
                self._state = "stopping"
                self._condition.notify_all()
                return False
            self._condition.notify_all()
            return True

    def wait_for_readiness_decision(self, timeout, signal_state):
        if type(timeout) not in {int, float} or timeout <= 0:
            raise RuntimeError("invalid_readiness_decision_timeout")
        deadline = time.monotonic() + float(timeout)
        with self._condition:
            while (
                self._state == "serving"
                and not self._ready_state_reached
            ):
                if signal_state.requested:
                    self._state = "stopping"
                    self._condition.notify_all()
                    return self._state
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.failed = True
                    self._state = "failed"
                    if signal_state.requested:
                        self.failed = False
                        self._state = "stopping"
                    self._condition.notify_all()
                    return self._state
                self._condition.wait(
                    min(_SERVE_POLL_SECONDS, remaining)
                )
            if self._ready_state_reached:
                return "ready"
            return self._state

    def request_stop(self):
        with self._condition:
            if self._state in {"created", "starting", "serving"}:
                self._state = "stopping"
                self._condition.notify_all()
            return self._state in {"stopping", "failed", "stopped"}

    def publish_failure(self, exc, signal_state=None):
        with self._condition:
            if self._state == "stopping":
                self._state = "stopped"
            elif self._state not in {"failed", "stopped"}:
                if isinstance(
                    exc,
                    (KeyboardInterrupt, SystemExit, GeneratorExit),
                ):
                    self.control = exc
                self.failed = True
                self._state = "failed"
                if signal_state is not None and signal_state.requested:
                    self.control = None
                    self.failed = False
                    self._state = "stopped"
            self._condition.notify_all()
        self.done.set()

    def publish_success(self, signal_state=None):
        with self._condition:
            if self._state == "stopping":
                self._state = "stopped"
            elif self._state != "stopped":
                self.failed = True
                self._state = "failed"
                if signal_state is not None and signal_state.requested:
                    self.failed = False
                    self._state = "stopped"
            self._condition.notify_all()
        self.done.set()

    def finish_unstarted(self):
        with self._condition:
            if self._state in {"created", "starting", "stopping"}:
                if self._state == "stopping":
                    self._state = "stopped"
                else:
                    self.failed = True
                    self._state = "failed"
                self._condition.notify_all()
        self.done.set()
        return self.state in {"failed", "stopped"}

    def terminal(self):
        with self.lock:
            return self._state in {"failed", "stopped"}

    def __repr__(self):
        with self.lock:
            state = self._state
            ready = self._ready_state_reached
        return f"_ServeOutcome(state={state!r}, ready={ready})"


class _ServerOwnership:
    __slots__ = (
        "_high_level_terminal",
        "_listener",
        "_listener_terminal",
        "_lock",
        "_server",
        "_state",
    )

    def __init__(self):
        listener = socket.socket.__new__(socket.socket)
        listener._io_refs = 0
        listener._closed = False
        self._high_level_terminal = False
        self._listener = listener
        self._listener_terminal = False
        self._lock = threading.Lock()
        self._server = None
        self._state = "empty"
        listener = None

    def acquire(self, server):
        if server is None:
            raise RuntimeError("invalid_constructed_server")
        self.acquire_server(server)
        listener = getattr(server, "socket", None)
        return self.attach_listener(server, listener)

    def acquire_server(self, server):
        if server is None:
            raise RuntimeError("invalid_constructed_server")
        with self._lock:
            if self._state != "empty":
                raise RuntimeError("server_ownership_already_acquired")
            self._server = server
            self._state = "acquiring"
        return True

    def materialize_listener(
        self,
        server,
        address_family,
        socket_type,
        protocol=0,
    ):
        with self._lock:
            listener = self._listener
            if (
                self._server is not server
                or self._state != "acquiring"
                or listener is None
                or not _socket_is_closed(listener)
            ):
                raise RuntimeError(
                    "server_listener_materialization_failed"
                )
        server._owned_server_socket = listener
        _socket.socket.__init__(
            listener,
            address_family,
            socket_type,
            protocol,
        )
        return listener

    def attach_listener(self, server, listener):
        if listener is None or not callable(getattr(listener, "close", None)):
            raise RuntimeError("invalid_constructed_server")
        with self._lock:
            if (
                self._server is server
                and self._listener is listener
                and self._state != "acquiring"
            ):
                return True
            if self._server is not server or self._state != "acquiring":
                raise RuntimeError("server_ownership_acquisition_failed")
            if (
                self._listener is not listener
                and not _socket_is_closed(self._listener)
            ):
                raise RuntimeError("server_listener_already_owned")
            self._listener = listener
            self._state = "inactive"
        return True

    def _listener_locked(self):
        listener = self._listener
        server = self._server
        if (
            server is not None
            and (listener is None or _socket_is_closed(listener))
        ):
            try:
                candidate = getattr(server, "socket", None)
            except Exception as exc:
                _sanitize_launcher_exception(exc)
                exc = None
                candidate = None
            if (
                candidate is not None
                and candidate is not listener
                and callable(getattr(candidate, "close", None))
                and not _socket_is_closed(candidate)
            ):
                listener = candidate
                self._listener = candidate
                if self._state == "acquiring":
                    self._state = "inactive"
        return listener

    def abandon_socketless_construction(self, server):
        with self._lock:
            listener = self._listener_locked()
            if (
                self._server is not server
                or self._state != "acquiring"
                or (
                    listener is not None
                    and not _socket_is_closed(listener)
                )
            ):
                return False
            self._listener = None
            self._server = None
            self._high_level_terminal = True
            self._listener_terminal = True
            self._state = "terminal"
        return True

    def owns(self, server):
        with self._lock:
            return self._server is server and self._state != "empty"

    def owns_listener(self, server, listener):
        with self._lock:
            owned_listener = self._listener_locked()
            return (
                self._server is server
                and owned_listener is listener
                and self._state != "empty"
            )

    def transition(self, expected, target):
        with self._lock:
            if self._state != expected:
                raise RuntimeError("invalid_server_ownership_transition")
            self._state = target
        return True

    def begin_stopping(self):
        with self._lock:
            if self._state not in {"empty", "terminal"}:
                self._state = "stopping"
        return True

    def close_high_level(self):
        with self._lock:
            server = self._server
            terminal = self._high_level_terminal
            listener = self._listener_locked()
            listener_terminal = (
                self._listener_terminal
                or listener is None
                or _socket_is_closed(listener)
            )
        if server is None or terminal:
            return True
        try:
            published_listener = getattr(server, "socket", None)
        except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
            _sanitize_launcher_exception(exc)
            exc = None
            published_listener = None
        except Exception as exc:
            _sanitize_launcher_exception(exc)
            exc = None
            published_listener = None
        if (
            listener is not None
            and listener_terminal
            and published_listener is listener
        ):
            server.socket = _ClosedServerSocket()
        published_listener = None
        server.server_close()
        with self._lock:
            if self._server is server:
                self._high_level_terminal = True
                if self._listener_terminal:
                    self._state = "terminal"
        return True

    def high_level_terminal(self):
        with self._lock:
            return self._server is None or self._high_level_terminal

    def close_listener(self):
        with self._lock:
            listener = self._listener_locked()
            terminal = self._listener_terminal
        if listener is None or terminal or _socket_is_closed(listener):
            with self._lock:
                self._listener_terminal = True
                if self._high_level_terminal:
                    self._state = "terminal"
            return True
        result = _close_socket_independently(listener)
        if result:
            with self._lock:
                if self._listener is listener:
                    self._listener_terminal = True
                    if self._high_level_terminal:
                        self._state = "terminal"
        return result

    def listener_terminal(self):
        with self._lock:
            listener = self._listener_locked()
            terminal = self._listener_terminal
        if terminal or listener is None or _socket_is_closed(listener):
            with self._lock:
                self._listener_terminal = True
                if self._high_level_terminal:
                    self._state = "terminal"
            return True
        return False

    def __repr__(self):
        with self._lock:
            state = self._state
        return f"_ServerOwnership(<{state}>)"


class _TlsWorkspaceOwnership:
    __slots__ = (
        "_lock",
        "_scope",
        "_scope_terminal",
        "_server",
        "_server_policy_terminal",
    )

    def __init__(self):
        self._lock = threading.Lock()
        self._scope = None
        self._scope_terminal = False
        self._server = None
        self._server_policy_terminal = False

    def acquire_scope(self, scope):
        if scope is None:
            raise RuntimeError("invalid_tls_workspace")
        with self._lock:
            if self._scope is scope:
                return True
            if self._scope is not None or self._scope_terminal:
                raise RuntimeError("tls_workspace_already_owned")
            self._scope = scope
        return True

    def owns_scope(self, scope):
        with self._lock:
            return self._scope is scope and not self._scope_terminal

    def attach_server(self, server):
        if server is None:
            raise RuntimeError("invalid_tls_server")
        with self._lock:
            if self._server is server:
                return True
            if self._server is not None or self._server_policy_terminal:
                raise RuntimeError("tls_server_already_owned")
            self._server = server
        return True

    def close(self):
        with self._lock:
            server = self._server
            server_policy_terminal = self._server_policy_terminal
            scope = self._scope
            scope_terminal = self._scope_terminal
        if not server_policy_terminal:
            if server is None or _release_server_tls_context(server):
                with self._lock:
                    if self._server is server:
                        self._server_policy_terminal = True
                        self._server = None
                server_policy_terminal = True
        if not server_policy_terminal:
            return False
        if not scope_terminal:
            if scope is None or _close_tls_scope(scope):
                with self._lock:
                    if self._scope is scope:
                        self._scope_terminal = True
                        self._scope = None
                scope_terminal = True
        return server_policy_terminal and scope_terminal

    def terminal(self):
        with self._lock:
            return (
                self._server_policy_terminal
                and self._scope_terminal
            )

    def __repr__(self):
        with self._lock:
            terminal = (
                self._server_policy_terminal
                and self._scope_terminal
            )
        return (
            "_TlsWorkspaceOwnership("
            f"<{'terminal' if terminal else 'owned'}>)"
        )


class _ClosedServerSocket:
    __slots__ = ()

    def close(self):
        return None

    def fileno(self):
        return -1

    def __repr__(self):
        return "_ClosedServerSocket(<terminal>)"


def _publish_call_result(offer, callback, arguments):
    if (
        type(offer) is not list
        or offer
        or not callable(callback)
        or type(arguments) is not tuple
    ):
        raise RuntimeError("invalid_owned_call_publication")
    publication = next(
        map(
            offer.append,
            itertools.starmap(callback, (arguments,)),
        )
    )
    if publication is not None or len(offer) != 1:
        raise RuntimeError("owned_call_publication_failed")
    return True


class _PendingTlsHandshake:
    __slots__ = (
        "_accepted_descriptor_offer",
        "_accepted_descriptor_parameters",
        "_lock",
        "_producer_epoch",
        "_raw_socket_offer",
        "_secondary_socket",
        "_socket",
        "_state",
        "_wrapped_descriptor_offer",
        "_wrapped_descriptor_parameters",
        "_wrapped_socket_offer",
    )

    def __init__(self, request=None):
        if request is not None and not callable(
            getattr(request, "close", None)
        ):
            raise RuntimeError("invalid_accepted_socket")
        self._accepted_descriptor_offer = []
        self._accepted_descriptor_parameters = None
        self._lock = threading.Lock()
        self._producer_epoch = 0
        if request is None:
            raw_socket = socket.socket.__new__(socket.socket)
            raw_socket._io_refs = 0
            raw_socket._closed = False
            self._raw_socket_offer = [raw_socket]
            raw_socket = None
        else:
            self._raw_socket_offer = []
        self._secondary_socket = None
        self._socket = request
        self._state = "accepting" if request is None else "owned"
        self._wrapped_descriptor_offer = []
        self._wrapped_descriptor_parameters = None
        self._wrapped_socket_offer = []

    def accept_from(self, listener):
        accept = getattr(listener, "_accept", None)
        if not callable(accept):
            raise OSError("owned_accept_unavailable")
        descriptor_parameters = (
            listener.family,
            listener.type,
            listener.proto,
        )
        force_blocking = (
            socket.getdefaulttimeout() is None
            and listener.gettimeout()
        )
        with self._lock:
            if (
                self._state != "accepting"
                or self._accepted_descriptor_offer
                or self._accepted_descriptor_parameters is not None
                or len(self._raw_socket_offer) != 1
                or self._raw_socket_offer[0].fileno() != -1
                or self._socket is not None
            ):
                raise OSError("accepted_socket_not_awaiting_adoption")
            self._accepted_descriptor_parameters = (
                descriptor_parameters
            )
            self._producer_epoch += 1
            self._state = "accept_io"
        _publish_call_result(
            self._accepted_descriptor_offer,
            accept,
            (),
        )
        descriptor_record = self._accepted_descriptor_offer[0]
        if (
            type(descriptor_record) is not tuple
            or len(descriptor_record) != 2
            or type(descriptor_record[0]) is not int
            or descriptor_record[0] < 0
        ):
            raise OSError("accepted_socket_invalid")
        descriptor, client_address = descriptor_record
        request = self._raw_socket_offer[0]
        _socket.socket.__init__(
            request,
            *descriptor_parameters,
            descriptor,
        )
        with self._lock:
            if (
                len(self._raw_socket_offer) != 1
                or self._raw_socket_offer[0] is not request
                or len(self._accepted_descriptor_offer) != 1
                or self._accepted_descriptor_offer[0]
                is not descriptor_record
                or request.fileno() != descriptor
            ):
                raise OSError(
                    "accepted_socket_not_awaiting_adoption"
                )
            self._accepted_descriptor_offer.clear()
            self._accepted_descriptor_parameters = None
            cancelled = self._state != "accept_io"
        if cancelled:
            raise OSError("accepted_socket_not_awaiting_adoption")
        if (
            request is None
            or not callable(getattr(request, "close", None))
        ):
            raise OSError("accepted_socket_invalid")
        if force_blocking:
            request.setblocking(True)
        with self._lock:
            if (
                self._state != "accept_io"
                or len(self._raw_socket_offer) != 1
                or self._raw_socket_offer[0] is not request
                or self._accepted_descriptor_offer
                or self._accepted_descriptor_parameters is not None
            ):
                raise OSError("accepted_socket_not_awaiting_adoption")
            self._socket = request
            self._raw_socket_offer.clear()
            self._state = "owned"
        return request, client_address

    def adopt_raw(self, request):
        if request is None or not callable(getattr(request, "close", None)):
            raise RuntimeError("invalid_accepted_socket")
        with self._lock:
            if (
                self._state != "accepting"
                or self._accepted_descriptor_offer
                or self._accepted_descriptor_parameters is not None
                or (
                    self._raw_socket_offer
                    and any(
                        not _socket_is_closed(candidate)
                        for candidate in self._raw_socket_offer
                    )
                )
                or self._socket is not None
                or self._secondary_socket is not None
            ):
                raise OSError("accepted_socket_not_awaiting_adoption")
            self._producer_epoch += 1
            self._raw_socket_offer.clear()
            self._accepted_descriptor_parameters = None
            self._socket = request
            self._state = "owned"
        return True

    def cancel_accept(self):
        with self._lock:
            if self._state in {
                "accept_io",
                "accept_cancel_pending",
            }:
                self._state = "cancelled"
        return True

    def begin_handshake(self):
        with self._lock:
            if self._state != "owned":
                raise OSError("tls_handshake_not_owned")
            self._state = "handshaking"
        return True

    def wrap(self, context):
        with self._lock:
            if self._state != "handshaking":
                raise OSError("tls_handshake_not_owned")
            request = self._socket
            owned_context = isinstance(
                context,
                _ORIGINAL_SSL_CONTEXT_TYPE,
            )
            if owned_context:
                socket_class = context.sslsocket_class
                if (
                    not isinstance(socket_class, type)
                    or not issubclass(socket_class, ssl.SSLSocket)
                ):
                    raise OSError("invalid_tls_socket_class")
            self._producer_epoch += 1
            if owned_context:
                wrapped_shell = socket_class.__new__(socket_class)
                wrapped_shell._io_refs = 0
                wrapped_shell._closed = False
                wrapped_shell._sslobj = None
                self._wrapped_socket_offer.append(wrapped_shell)
                wrapped_shell = None
            self._state = (
                "tls_materializing"
                if owned_context
                else "wrapping"
            )
        wrapped = None
        producer = None
        try:
            if owned_context:
                self._materialize_owned_tls_socket(
                    context,
                    request,
                )
            else:
                producer = functools.partial(
                    context.wrap_socket,
                    request,
                    server_side=True,
                    do_handshake_on_connect=False,
                )
                _publish_call_result(
                    self._wrapped_socket_offer,
                    producer,
                    (),
                )
            wrapped = self._wrapped_socket_offer[0]
            self._adopt_wrapped_socket(request, wrapped)
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            self._cancel_wrap_result_preserving_primary(request, wrapped)
            raise
        except Exception:
            self._cancel_wrap_result_preserving_primary(request, wrapped)
            raise
        finally:
            producer = None
        return wrapped

    def _materialize_owned_tls_socket(self, context, request):
        if (
            request.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
            != socket.SOCK_STREAM
            or context.check_hostname
        ):
            raise OSError("invalid_tls_handshake_socket")
        timeout = request.gettimeout()
        descriptor_parameters = (
            request.family,
            request.type,
            request.proto,
        )
        with self._lock:
            if (
                self._state not in {
                    "tls_materializing",
                    "tls_materialize_cancel_pending",
                }
                or len(self._wrapped_socket_offer) != 1
                or self._wrapped_descriptor_offer
                or self._wrapped_descriptor_parameters is not None
            ):
                raise OSError("tls_handshake_not_owned")
            wrapped = self._wrapped_socket_offer[0]
            self._wrapped_descriptor_parameters = (
                descriptor_parameters
            )
        _publish_call_result(
            self._wrapped_descriptor_offer,
            _socket.socket.detach,
            (request,),
        )
        descriptor = self._wrapped_descriptor_offer[0]
        if type(descriptor) is not int or descriptor < 0:
            raise OSError("invalid_tls_handshake_socket")
        _socket.socket.__init__(
            wrapped,
            *descriptor_parameters,
            descriptor,
        )
        with self._lock:
            if (
                self._state not in {
                    "tls_materializing",
                    "tls_materialize_cancel_pending",
                }
                or len(self._wrapped_descriptor_offer) != 1
                or self._wrapped_descriptor_offer[0] != descriptor
                or wrapped.fileno() != descriptor
            ):
                raise OSError("tls_handshake_socket_closed")
            self._wrapped_descriptor_offer.clear()
            self._wrapped_descriptor_parameters = None
            cancelled = (
                self._state == "tls_materialize_cancel_pending"
            )
            if cancelled:
                self._state = "cancelled"
        if cancelled:
            raise OSError("tls_handshake_socket_closed")
        wrapped._context = context
        wrapped._session = None
        wrapped._closed = False
        wrapped.server_side = True
        wrapped.server_hostname = None
        wrapped.do_handshake_on_connect = False
        wrapped.suppress_ragged_eofs = True
        wrapped.settimeout(timeout)
        try:
            wrapped.getpeername()
        except OSError as exc:
            _sanitize_launcher_exception(exc)
            exc = None
            raise OSError("tls_handshake_socket_not_connected") from None
        wrapped._connected = True
        wrapped._sslobj = context._wrap_socket(
            wrapped,
            True,
            None,
            owner=wrapped,
            session=None,
        )
        with self._lock:
            if self._state == "tls_materialize_cancel_pending":
                self._state = "cancelled"
                cancelled = True
            elif self._state == "tls_materializing":
                self._state = "wrapping"
                cancelled = False
            else:
                raise OSError("tls_handshake_socket_closed")
        if cancelled:
            raise OSError("tls_handshake_socket_closed")
        return True

    def _adopt_wrapped_socket(self, request, wrapped):
        if wrapped is None or not callable(getattr(wrapped, "close", None)):
            raise OSError("invalid_tls_handshake_socket")
        with self._lock:
            offered = (
                len(self._wrapped_socket_offer) == 1
                and self._wrapped_socket_offer[0] is wrapped
            )
            if (
                not offered
                or self._state != "wrapping"
                or self._socket is not request
            ):
                if self._socket is None:
                    self._socket = wrapped
                elif self._socket is not wrapped:
                    self._secondary_socket = wrapped
                if (
                    self._socket is wrapped
                    or self._secondary_socket is wrapped
                ):
                    self._wrapped_socket_offer.clear()
                self._state = "cancelled"
                invalidated = True
            else:
                if wrapped is not request:
                    self._secondary_socket = wrapped
                self._wrapped_socket_offer.clear()
                self._state = "handshaking"
                invalidated = False
        if invalidated:
            try:
                self.close()
            except (
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                _sanitize_launcher_exception(exc)
                exc = None
            except Exception as exc:
                _sanitize_launcher_exception(exc)
                exc = None
            raise OSError("tls_handshake_socket_closed")
        return True

    def _cancel_wrap_result_preserving_primary(self, request, wrapped):
        tracked = wrapped is None
        try:
            with self._lock:
                if wrapped is not None:
                    if (
                        self._socket is wrapped
                        or self._secondary_socket is wrapped
                    ):
                        tracked = True
                    elif self._secondary_socket is None:
                        self._secondary_socket = wrapped
                        tracked = True
                    elif self._socket is None:
                        self._socket = wrapped
                        tracked = True
                if self._state in {
                    "wrapping",
                    "wrap_cancel_pending",
                    "tls_materializing",
                    "tls_materialize_cancel_pending",
                    "handshaking",
                }:
                    self._state = "cancelled"
        except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
            _sanitize_launcher_exception(exc)
            exc = None
        except Exception as exc:
            _sanitize_launcher_exception(exc)
            exc = None
        if wrapped is not None and not tracked:
            _close_socket_preserving_primary(wrapped)
        try:
            self.close()
        except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
            _sanitize_launcher_exception(exc)
            exc = None
        except Exception as exc:
            _sanitize_launcher_exception(exc)
            exc = None
        request = None
        wrapped = None

    def mark_ready(self, request):
        with self._lock:
            active_request = (
                self._secondary_socket
                if self._secondary_socket is not None
                else self._socket
            )
            if (
                self._state != "handshake_io"
                or active_request is not request
                or _socket_is_closed(request)
            ):
                raise OSError("tls_handshake_socket_closed")
            self._state = "ready"
        return True

    def claim_handshake_io(self, request):
        with self._lock:
            active_request = (
                self._secondary_socket
                if self._secondary_socket is not None
                else self._socket
            )
            if (
                self._state != "handshaking"
                or active_request is not request
                or _socket_is_closed(request)
            ):
                raise OSError("tls_handshake_socket_closed")
            self._state = "handshake_io"
        return True

    def transfer(self):
        with self._lock:
            if self._state != "ready":
                raise OSError("tls_handshake_transfer_failed")
            request = (
                self._secondary_socket
                if self._secondary_socket is not None
                else self._socket
            )
            if request is None or _socket_is_closed(request):
                raise OSError("tls_handshake_transfer_failed")
            self._state = "transfer_pending"
            return request

    def acknowledge_transfer(self, request):
        with self._lock:
            active_request = (
                self._secondary_socket
                if self._secondary_socket is not None
                else self._socket
            )
            if (
                self._state != "transfer_pending"
                or active_request is not request
                or _socket_is_closed(request)
            ):
                raise OSError("tls_handshake_transfer_failed")
            self._state = "request"
        return True

    def owns_request(self, request):
        with self._lock:
            active_request = (
                self._secondary_socket
                if self._secondary_socket is not None
                else self._socket
            )
            return (
                self._state in {"transfer_pending", "request"}
                and active_request is request
            )

    def owns_socket(self, request):
        with self._lock:
            return (
                self._socket is request
                or self._secondary_socket is request
                or any(
                    candidate is request
                    for candidate in self._raw_socket_offer
                )
                or any(
                    candidate is request
                    for candidate in self._wrapped_socket_offer
                )
            )

    def _normalize_descriptor_offer_locked(self, *, wrapped):
        if type(wrapped) is not bool:
            raise OSError("invalid_socket_descriptor_offer")
        if wrapped:
            descriptor_offer = self._wrapped_descriptor_offer
            socket_offer = self._wrapped_socket_offer
            descriptor_parameters = (
                self._wrapped_descriptor_parameters
            )
            invalid_error = "tls_handshake_socket_closed"
        else:
            descriptor_offer = self._accepted_descriptor_offer
            socket_offer = self._raw_socket_offer
            descriptor_parameters = (
                self._accepted_descriptor_parameters
            )
            invalid_error = "accepted_socket_ownership_failed"
        if not descriptor_offer:
            if wrapped:
                self._wrapped_descriptor_parameters = None
            else:
                self._accepted_descriptor_parameters = None
            return True
        if self._state in {
            "accept_io",
            "accept_cancel_pending",
            "tls_materializing",
            "tls_materialize_cancel_pending",
        }:
            raise OSError(invalid_error)
        if len(descriptor_offer) != 1 or len(socket_offer) != 1:
            raise OSError(invalid_error)
        descriptor_record = descriptor_offer[0]
        if wrapped:
            descriptor = descriptor_record
        elif (
            type(descriptor_record) is tuple
            and len(descriptor_record) == 2
        ):
            descriptor = descriptor_record[0]
        else:
            raise OSError(invalid_error)
        if type(descriptor) is not int or descriptor < 0:
            raise OSError(invalid_error)
        request = socket_offer[0]
        current_descriptor = request.fileno()
        if current_descriptor != descriptor:
            if (
                current_descriptor != -1
                or type(descriptor_parameters) is not tuple
                or len(descriptor_parameters) != 3
            ):
                raise OSError(invalid_error)
            _socket.socket.__init__(
                request,
                *descriptor_parameters,
                descriptor,
            )
            if request.fileno() != descriptor:
                raise OSError(invalid_error)
        descriptor_offer.clear()
        if wrapped:
            self._wrapped_descriptor_parameters = None
        else:
            self._accepted_descriptor_parameters = None
        return True

    def close(self):
        with self._lock:
            state = self._state
            if (
                state == "closed"
                and not self._accepted_descriptor_offer
                and self._accepted_descriptor_parameters is None
                and not self._raw_socket_offer
                and not self._wrapped_descriptor_offer
                and self._wrapped_descriptor_parameters is None
                and not self._wrapped_socket_offer
                and self._socket is None
                and self._secondary_socket is None
            ):
                return True
            if state == "accept_io":
                self._state = "accept_cancel_pending"
                return False
            if state == "tls_materializing":
                self._state = "tls_materialize_cancel_pending"
                return False
            if state in {
                "accept_cancel_pending",
                "tls_materialize_cancel_pending",
            }:
                return False
            if state in {"wrapping", "wrap_cancel_pending"}:
                self._state = "wrap_cancel_pending"
            else:
                self._state = "closing"
        first_control = None
        failed = False
        for wrapped_descriptor in (False, True):
            try:
                with self._lock:
                    self._normalize_descriptor_offer_locked(
                        wrapped=wrapped_descriptor,
                    )
            except (
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                if first_control is None:
                    first_control = exc
                _sanitize_launcher_exception(exc)
                exc = None
                failed = True
            except Exception as exc:
                _sanitize_launcher_exception(exc)
                exc = None
                failed = True
        with self._lock:
            blocked_descriptor_sockets = ()
            if self._accepted_descriptor_offer:
                blocked_descriptor_sockets += tuple(
                    self._raw_socket_offer
                )
            if self._wrapped_descriptor_offer:
                blocked_descriptor_sockets += tuple(
                    self._wrapped_socket_offer
                )
            requests = tuple(
                request
                for request in (
                    self._socket,
                    self._secondary_socket,
                    *self._raw_socket_offer,
                    *self._wrapped_socket_offer,
                )
                if request is not None
                and not any(
                    candidate is request
                    for candidate in blocked_descriptor_sockets
                )
            )
        seen = set()
        for request in requests:
            identity = id(request)
            if identity in seen:
                continue
            seen.add(identity)
            try:
                terminal = _close_socket_independently(request)
            except (
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                if first_control is None:
                    first_control = exc
                _sanitize_launcher_exception(exc)
                exc = None
                terminal = _socket_is_closed(request)
                failed = True
            except Exception as exc:
                _sanitize_launcher_exception(exc)
                exc = None
                terminal = _socket_is_closed(request)
                failed = True
            if terminal:
                with self._lock:
                    if self._socket is request:
                        self._socket = None
                    if self._secondary_socket is request:
                        self._secondary_socket = None
                    self._raw_socket_offer[:] = [
                        candidate
                        for candidate in self._raw_socket_offer
                        if candidate is not request
                    ]
                    self._wrapped_socket_offer[:] = [
                        candidate
                        for candidate in self._wrapped_socket_offer
                        if candidate is not request
                    ]
            request = None
        with self._lock:
            complete = (
                self._state != "wrap_cancel_pending"
                and not self._accepted_descriptor_offer
                and self._accepted_descriptor_parameters is None
                and not self._raw_socket_offer
                and not self._wrapped_descriptor_offer
                and self._wrapped_descriptor_parameters is None
                and not self._wrapped_socket_offer
                and self._socket is None
                and self._secondary_socket is None
            )
            if complete:
                self._state = "closed"
        requests = None
        blocked_descriptor_sockets = None
        seen = None
        if first_control is not None:
            propagated = first_control
            first_control = None
            raise propagated from None
        if failed and complete:
            raise _ServerCleanupFailure()
        return complete

    def terminal(self):
        with self._lock:
            state = self._state
            producer_epoch = self._producer_epoch
            primary = self._socket
            secondary = self._secondary_socket
            raw_offers = tuple(self._raw_socket_offer)
            wrapped_offers = tuple(self._wrapped_socket_offer)
            accepted_descriptor_parameters = (
                self._accepted_descriptor_parameters
            )
            wrapped_descriptor_parameters = (
                self._wrapped_descriptor_parameters
            )
            descriptor_pending = bool(
                self._accepted_descriptor_offer
            )
            descriptor_parameters_pending = (
                accepted_descriptor_parameters is not None
            )
            wrapped_descriptor_pending = bool(
                self._wrapped_descriptor_offer
            )
            wrapped_descriptor_parameters_pending = (
                wrapped_descriptor_parameters is not None
            )
        if (
            state == "closed"
            and not descriptor_pending
            and not descriptor_parameters_pending
            and not wrapped_descriptor_pending
            and not wrapped_descriptor_parameters_pending
            and not raw_offers
            and not wrapped_offers
            and primary is None
            and secondary is None
        ):
            return True
        if state in {
            "accept_io",
            "accept_cancel_pending",
            "wrapping",
            "wrap_cancel_pending",
            "tls_materializing",
            "tls_materialize_cancel_pending",
        } or descriptor_pending or wrapped_descriptor_pending:
            return False
        primary_closed = primary is None or _socket_is_closed(primary)
        secondary_closed = (
            secondary is None or _socket_is_closed(secondary)
        )
        raw_closed = all(
            _socket_is_closed(request) for request in raw_offers
        )
        wrapped_closed = all(
            _socket_is_closed(request) for request in wrapped_offers
        )
        with self._lock:
            if (
                self._producer_epoch != producer_epoch
                or self._state != state
                or self._socket is not primary
                or self._secondary_socket is not secondary
                or len(self._raw_socket_offer) != len(raw_offers)
                or any(
                    current is not captured
                    for current, captured in zip(
                        self._raw_socket_offer,
                        raw_offers,
                        strict=True,
                    )
                )
                or len(self._wrapped_socket_offer)
                != len(wrapped_offers)
                or any(
                    current is not captured
                    for current, captured in zip(
                        self._wrapped_socket_offer,
                        wrapped_offers,
                        strict=True,
                    )
                )
                or self._accepted_descriptor_offer
                or self._wrapped_descriptor_offer
                or self._accepted_descriptor_parameters
                is not accepted_descriptor_parameters
                or self._wrapped_descriptor_parameters
                is not wrapped_descriptor_parameters
            ):
                return False
            self._accepted_descriptor_parameters = None
            self._wrapped_descriptor_parameters = None
            if self._socket is primary and primary_closed:
                self._socket = None
            if self._secondary_socket is secondary and secondary_closed:
                self._secondary_socket = None
            if raw_closed:
                self._raw_socket_offer.clear()
            if wrapped_closed:
                self._wrapped_socket_offer.clear()
            if (
                not self._accepted_descriptor_offer
                and self._accepted_descriptor_parameters is None
                and not self._raw_socket_offer
                and not self._wrapped_descriptor_offer
                and self._wrapped_descriptor_parameters is None
                and not self._wrapped_socket_offer
                and self._socket is None
                and self._secondary_socket is None
            ):
                self._state = "closed"
                return True
        return False

    def __repr__(self):
        with self._lock:
            state = self._state
        return f"_PendingTlsHandshake(<{state}>)"


class _DrainingThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = False

    @property
    def socket(self):
        return self._owned_server_socket

    @socket.setter
    def socket(self, listener):
        self._owned_server_socket = listener
        ownership = getattr(self, "_construction_ownership", None)
        if ownership is not None:
            ownership.attach_listener(self, listener)

    def __init__(
        self,
        server_address,
        RequestHandlerClass,
        bind_and_activate=True,
        *,
        _construction_ownership=None,
    ):
        if (
            _construction_ownership is not None
            and not isinstance(
                _construction_ownership,
                _ServerOwnership,
            )
        ):
            raise RuntimeError("invalid_server_construction_owner")
        self._owned_server_socket = None
        self._construction_ownership = _construction_ownership
        self._lifecycle_lock = threading.Lock()
        self._stopping = False
        self._serve_active = False
        self._serve_thread = None
        self._serve_thread_ident = None
        self._serve_outcome = None
        self._signal_state = None
        self._tls_context = None
        self._tls_handshake_timeout = None
        self._unregistered_handshake = None
        self._pending_handshakes = set()
        self._accepted_sockets = set()
        self._request_threads = set()
        self._published_handler = None
        self._shutdown_requested = None
        if _construction_ownership is not None:
            _construction_ownership.acquire_server(self)
        try:
            BaseServer.__init__(
                self,
                server_address,
                RequestHandlerClass,
            )
            if _construction_ownership is not None:
                listener = (
                    _construction_ownership.materialize_listener(
                        self,
                        self.address_family,
                        self.socket_type,
                    )
                )
            else:
                listener = socket.socket(
                    self.address_family,
                    self.socket_type,
                )
            self.socket = listener
            listener = None
            if bind_and_activate:
                self.server_bind()
                self.server_activate()
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            listener = getattr(self, "socket", None)
            if _construction_ownership is not None:
                if listener is None:
                    _construction_ownership.abandon_socketless_construction(
                        self
                    )
                elif not _construction_ownership.owns_listener(
                    self,
                    listener,
                ):
                    try:
                        _construction_ownership.attach_listener(
                            self,
                            listener,
                        )
                    except (
                        KeyboardInterrupt,
                        SystemExit,
                        GeneratorExit,
                    ) as exc:
                        _sanitize_launcher_exception(exc)
                        exc = None
                    except Exception as exc:
                        _sanitize_launcher_exception(exc)
                        exc = None
            self._construction_ownership = None
            _close_socket_preserving_primary(listener)
            raise
        except Exception:
            listener = getattr(self, "socket", None)
            if _construction_ownership is not None:
                if listener is None:
                    _construction_ownership.abandon_socketless_construction(
                        self
                    )
                elif not _construction_ownership.owns_listener(
                    self,
                    listener,
                ):
                    try:
                        _construction_ownership.attach_listener(
                            self,
                            listener,
                        )
                    except (
                        KeyboardInterrupt,
                        SystemExit,
                        GeneratorExit,
                    ) as exc:
                        _sanitize_launcher_exception(exc)
                        exc = None
                    except Exception as exc:
                        _sanitize_launcher_exception(exc)
                        exc = None
            self._construction_ownership = None
            _close_socket_preserving_primary(listener)
            raise
        if _construction_ownership is not None:
            if not _construction_ownership.owns_listener(self, self.socket):
                _construction_ownership.attach_listener(
                    self,
                    self.socket,
                )
        self._construction_ownership = None

    def handle_error(self, _request, _client_address):
        return None

    def serve_forever(self, *args, **kwargs):
        current = threading.current_thread()
        with self._lifecycle_lock:
            if (
                self._stopping
                or self._serve_active
                or self._serve_outcome is None
                or self._signal_state is None
                or (
                    self._serve_thread is not None
                    and self._serve_thread is not current
                )
            ):
                raise RuntimeError("serve_lifecycle_not_ready")
            if self._serve_thread is None:
                self._serve_thread = current
            self._serve_active = True
            self._serve_thread_ident = threading.get_ident()
        try:
            return super().serve_forever(*args, **kwargs)
        finally:
            with self._lifecycle_lock:
                self._serve_active = False
                self._serve_thread_ident = None
            current = None

    def service_actions(self):
        with self._lifecycle_lock:
            self._reap_request_threads_locked()
            outcome = self._serve_outcome
            signal_state = self._signal_state
            eligible = (
                self._serve_active
                and not self._stopping
                and not self._shutdown_is_requested()
                and not _socket_is_closed(self.socket)
                and self._published_handler is not None
            )
        if (
            eligible
            and outcome.publish_serving_checkpoint(signal_state)
            and outcome.wait_for_readiness_decision(
                _SERVE_STARTUP_SECONDS,
                signal_state,
            )
            == "failed"
        ):
            raise RuntimeError("serve_readiness_decision_failed")

    def _admission_ready_locked(self):
        outcome = self._serve_outcome
        return (
            self._serve_active
            and not self._stopping
            and not self._shutdown_is_requested()
            and outcome is not None
            and outcome.ready_state_reached
            and not _socket_is_closed(self.socket)
            and self._published_handler is not None
        )

    def set_serve_lifecycle(self, outcome, signal_state):
        if (
            not isinstance(outcome, _ServeOutcome)
            or not isinstance(signal_state, _SignalShutdownState)
        ):
            raise RuntimeError("invalid_serve_lifecycle")
        with self._lifecycle_lock:
            if (
                self._serve_active
                or self._stopping
                or self._serve_outcome is not None
                or self._signal_state is not None
            ):
                raise RuntimeError("serve_lifecycle_already_configured")
            self._serve_outcome = outcome
            self._signal_state = signal_state
        return True

    def set_serve_thread(self, thread):
        if (
            not callable(getattr(thread, "start", None))
            or not callable(getattr(thread, "is_alive", None))
            or not callable(getattr(thread, "join", None))
        ):
            raise RuntimeError("invalid_serve_thread")
        with self._lifecycle_lock:
            if self._serve_active or self._serve_thread is not None:
                raise RuntimeError("serve_thread_already_configured")
            self._serve_thread = thread
        return True

    def claim_serving_readiness(self):
        with self._lifecycle_lock:
            if (
                not self._serve_active
                or self._stopping
                or self._shutdown_is_requested()
                or _socket_is_closed(self.socket)
                or self._published_handler is None
                or self._serve_outcome is None
                or self._signal_state is None
            ):
                return False
            return self._serve_outcome.claim_ready(self._signal_state)

    def set_tls_context(
        self,
        context,
        *,
        handshake_timeout=_TLS_HANDSHAKE_SECONDS,
    ):
        if (
            not callable(getattr(context, "wrap_socket", None))
            or type(handshake_timeout) not in {int, float}
            or handshake_timeout <= 0
            or handshake_timeout > _SERVE_THREAD_DRAIN_SECONDS
        ):
            raise RuntimeError("invalid_tls_handshake_configuration")
        with self._lifecycle_lock:
            if (
                self._serve_active
                or self._stopping
                or self._tls_context is not None
            ):
                raise RuntimeError("tls_context_already_configured")
            self._tls_context = context
            self._tls_handshake_timeout = float(handshake_timeout)
        return True

    def release_tls_context(self):
        with self._lifecycle_lock:
            serve_thread = self._serve_thread
            if (
                self._serve_active
                or (
                    serve_thread is not None
                    and serve_thread.is_alive()
                )
                or self._unregistered_handshake is not None
                or self._pending_handshakes
                or self._accepted_sockets
                or self._request_threads
                or not _socket_is_closed(self.socket)
            ):
                return False
            self._tls_context = None
            self._tls_handshake_timeout = None
        return True

    def tls_context_released(self):
        with self._lifecycle_lock:
            return (
                self._tls_context is None
                and self._tls_handshake_timeout is None
            )

    def get_request(self):
        lease = _PendingTlsHandshake()
        request = None
        client_address = None
        original_timeout = None
        wrapped = None
        try:
            with self._lifecycle_lock:
                if not self._admission_ready_locked():
                    raise OSError("server_not_ready")
                if self._serve_thread_ident != threading.get_ident():
                    raise OSError("invalid_accept_owner")
                if (
                    self._unregistered_handshake is not None
                    or len(self._pending_handshakes)
                    + len(self._accepted_sockets)
                    >= _MAX_TRACKED_ACCEPTED_SOCKETS
                ):
                    raise OSError("accepted_socket_capacity_reached")
                self._pending_handshakes.add(lease)
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            _close_accepted_owner_preserving_primary(lease)
            raise
        except OSError:
            _close_accepted_owner_preserving_primary(lease)
            raise
        except Exception as exc:
            _sanitize_launcher_exception(exc)
            exc = None
            _close_accepted_owner_preserving_primary(lease)
            raise OSError("accepted_socket_ownership_failed") from None
        try:
            try:
                if (
                    ThreadingHTTPServer.get_request
                    is _ORIGINAL_TCP_SERVER_GET_REQUEST
                ):
                    request, client_address = lease.accept_from(
                        self.socket
                    )
                else:
                    request, client_address = super().get_request()
                    lease.adopt_raw(request)
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                lease.cancel_accept()
                _adopt_raw_socket_preserving_primary(lease, request)
                raise
            except Exception as exc:
                _sanitize_launcher_exception(exc)
                exc = None
                lease.cancel_accept()
                _adopt_raw_socket_preserving_primary(lease, request)
                raise OSError("accepted_socket_ownership_failed") from None
            with self._lifecycle_lock:
                stopping = (
                    lease not in self._pending_handshakes
                    or not self._admission_ready_locked()
                    or self._tls_context is None
                )
                tls_context = self._tls_context
                handshake_timeout = self._tls_handshake_timeout
                if not stopping:
                    lease.begin_handshake()
            if stopping:
                raise OSError("server_shutdown_in_progress")
            original_timeout = request.gettimeout()
            wrapped = lease.wrap(tls_context)
            wrapped.settimeout(handshake_timeout)
            with self._lifecycle_lock:
                if (
                    lease not in self._pending_handshakes
                    or not self._admission_ready_locked()
                ):
                    stopping = True
                else:
                    stopping = False
                    lease.claim_handshake_io(wrapped)
            if stopping:
                raise OSError("server_shutdown_in_progress")
            wrapped.do_handshake()
            wrapped.settimeout(original_timeout)
            lease.mark_ready(wrapped)
            with self._lifecycle_lock:
                if (
                    lease not in self._pending_handshakes
                    or not self._admission_ready_locked()
                ):
                    stopping = True
                else:
                    request = lease.transfer()
                    self._accepted_sockets.add(lease)
                    lease.acknowledge_transfer(request)
                    self._pending_handshakes.discard(lease)
                    stopping = False
            if stopping:
                raise OSError("server_shutdown_in_progress")
            wrapped = None
            return request, client_address
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            _close_pending_handshake_preserving_primary(self, lease)
            raise
        except Exception as exc:
            _sanitize_launcher_exception(exc)
            exc = None
            _close_pending_handshake_preserving_primary(self, lease)
            raise OSError("tls_handshake_failed") from None
        finally:
            original_timeout = None
            wrapped = None

    def process_request(self, request, client_address):
        start_error = None
        with self._lifecycle_lock:
            self._reap_request_threads_locked()
            if not self._admission_ready_locked():
                stopping = True
            else:
                stopping = False
                request_owner = self._accepted_owner_for_request_locked(
                    request
                )
                if request_owner is None:
                    request_owner = request
                    self._accepted_sockets.add(request_owner)
                thread = threading.Thread(
                    target=self._tracked_process_request,
                    args=(request, client_address, request_owner),
                    name="wahojobs-durable-login-request",
                    daemon=False,
                )
                self._request_threads.add(thread)
            if not stopping:
                try:
                    thread.start()
                except (
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    start_error = exc
                except Exception as exc:
                    start_error = exc
                if start_error is not None:
                    try:
                        thread_is_alive = thread.is_alive()
                    except (
                        KeyboardInterrupt,
                        SystemExit,
                        GeneratorExit,
                    ) as exc:
                        _sanitize_launcher_exception(exc)
                        exc = None
                        thread_is_alive = True
                    except Exception as exc:
                        _sanitize_launcher_exception(exc)
                        exc = None
                        thread_is_alive = True
                    if not thread_is_alive:
                        self._request_threads.discard(thread)
        if start_error is not None:
            terminal = _close_accepted_owner_preserving_primary(
                request_owner
            )
            if terminal:
                with self._lifecycle_lock:
                    self._accepted_sockets.discard(request_owner)
            propagated = start_error
            start_error = None
            raise propagated from None
        if stopping:
            with self._lifecycle_lock:
                request_owner = self._accepted_owner_for_request_locked(
                    request
                )
            if request_owner is None:
                request_owner = request
            terminal = _close_accepted_owner(request_owner)
            if terminal:
                with self._lifecycle_lock:
                    self._accepted_sockets.discard(request_owner)
            return

    def _accepted_owner_for_request_locked(self, request):
        for owner in self._accepted_sockets:
            if owner is request:
                return owner
            if (
                isinstance(owner, _PendingTlsHandshake)
                and owner.owns_request(request)
            ):
                return owner
        return None

    def _tracked_process_request(
        self,
        request,
        client_address,
        request_owner=None,
    ):
        current = threading.current_thread()
        if request_owner is None:
            with self._lifecycle_lock:
                request_owner = self._accepted_owner_for_request_locked(
                    request
                )
            if request_owner is None:
                request_owner = request
        try:
            try:
                self.process_request_thread(request, client_address)
            except (
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                self._publish_request_control_failure(exc)
                exc = None
            except Exception as exc:
                _sanitize_launcher_exception(exc)
                exc = None
        finally:
            try:
                terminal = _close_accepted_owner(request_owner)
            except (
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                self._publish_request_control_failure(exc)
                exc = None
                terminal = _accepted_owner_terminal(request_owner)
            except Exception as exc:
                _sanitize_launcher_exception(exc)
                exc = None
                terminal = _accepted_owner_terminal(request_owner)
            with self._lifecycle_lock:
                if terminal:
                    self._accepted_sockets.discard(request_owner)
                    self._accepted_sockets.discard(request)
            request = None
            request_owner = None
            client_address = None
            current = None

    def _reap_request_threads_locked(self):
        for thread in tuple(self._request_threads):
            try:
                alive = thread.is_alive()
            except (
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                _sanitize_launcher_exception(exc)
                exc = None
                alive = True
            except Exception as exc:
                _sanitize_launcher_exception(exc)
                exc = None
                alive = True
            if not alive:
                self._request_threads.discard(thread)
            thread = None
        return not self._request_threads

    def _publish_request_control_failure(self, exc):
        _sanitize_launcher_exception(exc)
        with self._lifecycle_lock:
            outcome = self._serve_outcome
            signal_state = self._signal_state
        if outcome is not None:
            outcome.publish_failure(exc, signal_state)
        self.begin_shutdown()

    def shutdown_request(self, request):
        with self._lifecycle_lock:
            request_owner = self._accepted_owner_for_request_locked(request)
        if request_owner is None:
            request_owner = request
        terminal = _close_accepted_owner(request_owner)
        if terminal:
            with self._lifecycle_lock:
                self._accepted_sockets.discard(request_owner)
                self._accepted_sockets.discard(request)
        return terminal

    def set_shutdown_notification(self, requested):
        if not callable(requested):
            raise RuntimeError("invalid_shutdown_notification")
        with self._lifecycle_lock:
            if self._serve_active:
                raise RuntimeError("shutdown_notification_already_active")
            self._shutdown_requested = requested
        return True

    def _shutdown_is_requested(self):
        requested = self._shutdown_requested
        if requested is None:
            return False
        try:
            return requested() is True
        except Exception:
            return True

    def publish_handler(self, handler):
        if not isinstance(handler, type):
            raise RuntimeError("invalid_durable_login_handler")
        with self._lifecycle_lock:
            if self._stopping or self._published_handler is not None:
                raise RuntimeError("durable_login_handler_publication_failed")
            self.RequestHandlerClass = handler
            self._published_handler = handler
        return True

    def detach_route_integration(self):
        with self._lifecycle_lock:
            handler = self._published_handler
            self._published_handler = None
            self.RequestHandlerClass = _UnpublishedRequestHandler
        if handler is not None and hasattr(
            handler,
            "_durable_google_login_browser_integration",
        ):
            handler._durable_google_login_browser_integration = None
        handler = None
        return True

    def begin_shutdown(self):
        with self._lifecycle_lock:
            self._stopping = True
            serve_active = self._serve_active
            outcome = self._serve_outcome
        if serve_active:
            setattr(self, "_BaseServer__shutdown_request", True)
        if outcome is not None:
            outcome.request_stop()
        return True

    def close_listener(self):
        listener = self.socket
        if listener is None:
            return True
        listener.close()
        return _socket_is_closed(listener)

    def close_pending_handshakes(self):
        return self._close_connection_resources(
            include_established=False,
        )

    def close_accepted_sockets(self):
        return self._close_connection_resources(
            include_established=True,
        )

    def _close_connection_resources(self, *, include_established):
        with self._lifecycle_lock:
            unregistered = getattr(
                self,
                "_unregistered_handshake",
                None,
            )
            leases = tuple(self._pending_handshakes)
            if unregistered is not None:
                leases += (unregistered,)
            sockets = (
                tuple(self._accepted_sockets)
                if include_established
                else ()
            )
        first_control = None
        failed = False
        for lease in leases:
            try:
                terminal = _close_accepted_owner(lease)
            except (
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                if first_control is None:
                    first_control = exc
                _sanitize_launcher_exception(exc)
                exc = None
                terminal = _accepted_owner_terminal(lease)
                failed = True
            except Exception as exc:
                _sanitize_launcher_exception(exc)
                exc = None
                terminal = _accepted_owner_terminal(lease)
                failed = True
            if terminal:
                with self._lifecycle_lock:
                    self._pending_handshakes.discard(lease)
                    if (
                        getattr(
                            self,
                            "_unregistered_handshake",
                            None,
                        )
                        is lease
                    ):
                        self._unregistered_handshake = None
            lease = None
        for request_owner in sockets:
            try:
                terminal = _close_accepted_owner(request_owner)
            except (
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                if first_control is None:
                    first_control = exc
                _sanitize_launcher_exception(exc)
                exc = None
                terminal = _accepted_owner_terminal(request_owner)
                failed = True
            except Exception as exc:
                _sanitize_launcher_exception(exc)
                exc = None
                terminal = _accepted_owner_terminal(request_owner)
                failed = True
            if terminal:
                with self._lifecycle_lock:
                    self._accepted_sockets.discard(request_owner)
            request_owner = None
        if first_control is not None:
            propagated = first_control
            first_control = None
            raise propagated from None
        with self._lifecycle_lock:
            complete = (
                getattr(self, "_unregistered_handshake", None) is None
                and not self._pending_handshakes
                and (
                    not include_established
                    or not self._accepted_sockets
                )
            )
        if failed and complete:
            raise _ServerCleanupFailure()
        return complete

    def drain_request_threads(self, timeout=_REQUEST_DRAIN_SECONDS):
        if type(timeout) not in {int, float} or timeout < 0:
            raise RuntimeError("invalid_request_drain_timeout")
        deadline = time.monotonic() + float(timeout)
        with self._lifecycle_lock:
            self._reap_request_threads_locked()
            threads = tuple(self._request_threads)
        first_control = None
        failed = False
        for thread in threads:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                if thread is not threading.current_thread():
                    thread.join(remaining)
            except (
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                if first_control is None:
                    first_control = exc
                _sanitize_launcher_exception(exc)
                exc = None
                failed = True
            except Exception as exc:
                _sanitize_launcher_exception(exc)
                exc = None
                failed = True
            if not thread.is_alive():
                with self._lifecycle_lock:
                    self._request_threads.discard(thread)
            thread = None
        if first_control is not None:
            propagated = first_control
            first_control = None
            raise propagated from None
        with self._lifecycle_lock:
            self._reap_request_threads_locked()
            complete = not self._request_threads
        if failed and complete:
            raise _ServerCleanupFailure()
        return complete

    def resource_counts(self):
        with self._lifecycle_lock:
            self._reap_request_threads_locked()
            unregistered = getattr(
                self,
                "_unregistered_handshake",
                None,
            )
            serve_thread = self._serve_thread
            pending_owners = tuple(self._pending_handshakes)
            if unregistered is not None:
                pending_owners += (unregistered,)
            connection_owner_ids = {
                id(owner)
                for owner in pending_owners
            }
            connection_owner_ids.update(
                id(owner) for owner in self._accepted_sockets
            )
            return {
                "listener": 0 if _socket_is_closed(self.socket) else 1,
                "accepted_sockets": len(connection_owner_ids),
                "pending_handshakes": len(
                    {id(owner) for owner in pending_owners}
                ),
                "request_threads": len(self._request_threads),
                "serve_threads": (
                    1
                    if (
                        serve_thread is not None
                        and serve_thread.is_alive()
                    )
                    or (
                        serve_thread is None
                        and self._serve_active
                    )
                    else 0
                ),
                "route_integrations": (
                    0 if self._published_handler is None else 1
                ),
            }


def _signal_category(number):
    if number == signal.SIGINT:
        return "sigint"
    if number == signal.SIGTERM:
        return "sigterm"
    if hasattr(signal, "SIGBREAK") and number == signal.SIGBREAK:
        return "sigbreak"
    return None


def _sanitize_launcher_exception(exc):
    for name, replacement in (
        ("args", ()),
        ("__traceback__", None),
        ("__cause__", None),
        ("__context__", None),
        ("__suppress_context__", True),
    ):
        try:
            BaseException.__dict__[name].__set__(exc, replacement)
        except Exception:
            pass
    if isinstance(exc, SystemExit):
        try:
            exc.code = None
        except Exception:
            pass


def _socket_is_closed(value):
    try:
        return value is None or value.fileno() < 0
    except Exception:
        return False


def _close_socket_independently(value):
    if value is None or _socket_is_closed(value):
        return True
    first_control = None
    failed = False
    shutdown = getattr(value, "shutdown", None)
    if callable(shutdown):
        try:
            shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            first_control = exc
            _sanitize_launcher_exception(exc)
            exc = None
            failed = True
        except Exception as exc:
            _sanitize_launcher_exception(exc)
            exc = None
            failed = True
    try:
        value.close()
    except (
        KeyboardInterrupt,
        SystemExit,
        GeneratorExit,
    ) as exc:
        if first_control is None:
            first_control = exc
        _sanitize_launcher_exception(exc)
        exc = None
        failed = True
    except Exception as exc:
        _sanitize_launcher_exception(exc)
        exc = None
        failed = True
    terminal = _socket_is_closed(value)
    if first_control is not None:
        propagated = first_control
        first_control = None
        raise propagated from None
    if failed and terminal:
        raise _ServerCleanupFailure()
    return terminal


def _close_socket_preserving_primary(value):
    try:
        _close_socket_independently(value)
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        _sanitize_launcher_exception(exc)
        exc = None
    except Exception as exc:
        _sanitize_launcher_exception(exc)
        exc = None


def _adopt_raw_socket_preserving_primary(lease, request):
    if lease is None or request is None:
        return False
    try:
        if lease.owns_socket(request):
            return True
        lease.adopt_raw(request)
        return True
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        _sanitize_launcher_exception(exc)
        exc = None
    except Exception as exc:
        _sanitize_launcher_exception(exc)
        exc = None
    try:
        tracked = lease.owns_socket(request)
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        _sanitize_launcher_exception(exc)
        exc = None
        tracked = False
    except Exception as exc:
        _sanitize_launcher_exception(exc)
        exc = None
        tracked = False
    if not tracked:
        _close_socket_preserving_primary(request)
    return tracked


def _close_pending_handshake_preserving_primary(server, lease):
    if lease is None:
        return True
    try:
        terminal = _close_accepted_owner(lease)
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        _sanitize_launcher_exception(exc)
        exc = None
        terminal = _accepted_owner_terminal(lease)
    except Exception as exc:
        _sanitize_launcher_exception(exc)
        exc = None
        terminal = _accepted_owner_terminal(lease)
    if not terminal:
        try:
            with server._lifecycle_lock:
                if (
                    getattr(
                        server,
                        "_unregistered_handshake",
                        None,
                    )
                    is lease
                    or lease in server._pending_handshakes
                    or lease in server._accepted_sockets
                ):
                    return False
                if server._unregistered_handshake is None:
                    server._unregistered_handshake = lease
                else:
                    server._pending_handshakes.add(lease)
        except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
            _sanitize_launcher_exception(exc)
            exc = None
            try:
                if (
                    lease not in server._pending_handshakes
                    and server._unregistered_handshake is None
                ):
                    server._unregistered_handshake = lease
            except Exception:
                pass
        except Exception as exc:
            _sanitize_launcher_exception(exc)
            exc = None
            try:
                if (
                    lease not in server._pending_handshakes
                    and server._unregistered_handshake is None
                ):
                    server._unregistered_handshake = lease
            except Exception:
                pass
        return False
    if terminal:
        with server._lifecycle_lock:
            server._pending_handshakes.discard(lease)
            server._accepted_sockets.discard(lease)
            if (
                getattr(server, "_unregistered_handshake", None)
                is lease
            ):
                server._unregistered_handshake = None
    return terminal


def _close_accepted_owner(owner):
    if isinstance(owner, _PendingTlsHandshake):
        return owner.close()
    return _close_socket_independently(owner)


def _close_accepted_owner_preserving_primary(owner):
    try:
        return _close_accepted_owner(owner)
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        _sanitize_launcher_exception(exc)
        exc = None
    except Exception as exc:
        _sanitize_launcher_exception(exc)
        exc = None
    return _accepted_owner_terminal(owner)


def _accepted_owner_terminal(owner):
    if isinstance(owner, _PendingTlsHandshake):
        return owner.terminal()
    return _socket_is_closed(owner)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Run the dedicated local HTTPS durable Google-login browser app."
        )
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Absolute path to the strict nonsecret runtime configuration.",
    )
    return parser.parse_args(argv)


def _confirmed_profile_artifact_sink(
    browser_integration,
    *,
    required,
):
    if type(required) is not bool:
        raise RuntimeError("profile_creation_capability_invalid")
    candidate = getattr(
        browser_integration,
        "issue_confirmed_profile_artifact",
        _MISSING_PROFILE_CREATION_CAPABILITY,
    )
    if candidate is _MISSING_PROFILE_CREATION_CAPABILITY:
        if required:
            raise RuntimeError("profile_creation_capability_unavailable")
        return None
    if not callable(candidate):
        raise RuntimeError("profile_creation_capability_invalid")
    return candidate


def _completed_profile_confirmation_authenticator(browser_integration):
    candidate = getattr(
        browser_integration,
        "authenticate_completed_profile_replay",
        _MISSING_PROFILE_CREATION_CAPABILITY,
    )
    if candidate is _MISSING_PROFILE_CREATION_CAPABILITY or not callable(candidate):
        raise RuntimeError("profile_creation_capability_invalid")
    return candidate


def _require_profile_matches_route(browser_integration):
    candidate = getattr(
        browser_integration,
        "matches_route",
        _MISSING_PROFILE_CREATION_CAPABILITY,
    )
    if candidate is _MISSING_PROFILE_CREATION_CAPABILITY:
        raise RuntimeError("profile_matching_capability_unavailable")
    if not callable(candidate):
        raise RuntimeError("profile_matching_capability_invalid")
    try:
        owned = candidate(_FIND_MATCHES_ROUTE)
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except Exception:
        raise RuntimeError("profile_matching_capability_invalid") from None
    if owned is not True:
        raise RuntimeError("profile_matching_capability_unavailable")
    return True


def _construct_production_handler(
    browser_integration,
    public_origin,
    *,
    require_profile_creation,
):
    from wahojobs.durable_product_browser_handler import (
        make_durable_product_browser_handler,
    )

    artifact_sink = _confirmed_profile_artifact_sink(
        browser_integration,
        required=require_profile_creation,
    )
    if artifact_sink is not None:
        _completed_profile_confirmation_authenticator(
            browser_integration
        )
    if require_profile_creation:
        _require_profile_matches_route(browser_integration)
    return make_durable_product_browser_handler(browser_integration)


def main(
    argv=None,
    *,
    _runtime_builder=None,
    _server_factory=None,
    _tls_context_factory=None,
    _checkpoint_observer=None,
    _shutdown_result_observer=None,
):
    args = parse_args(argv)
    from wahojobs.durable_google_login_runtime import (
        _CleanupCoordinator,
        _new_activation_handoff_reservation,
        _release_activation_handoff_reservation_preserving_primary,
        _reserve_activation_handoff,
        _retain_unresolved_activation_handoff,
        _retry_unresolved_activation_handoffs,
        prepare_durable_google_login_activation,
    )

    handoff_reservation = None
    try:
        handoff_reservation = _new_activation_handoff_reservation()
        _reserve_activation_handoff(handoff_reservation)
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        if handoff_reservation is not None:
            _release_activation_handoff_reservation_preserving_primary(
                handoff_reservation
            )
        raise
    except Exception as exc:
        _sanitize_launcher_exception(exc)
        exc = None
        if handoff_reservation is not None:
            _release_activation_handoff_reservation_preserving_primary(
                handoff_reservation
            )
        print(
            "Durable Google login could not start safely.",
            file=sys.stderr,
        )
        return 2

    coordinator = None
    signal_state = None
    try:
        coordinator = _CleanupCoordinator()
        signal_state = _SignalShutdownState()
        coordinator.own(
            "signal_handlers",
            signal_state,
            _restore_signal_handlers,
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        try:
            if coordinator is not None:
                _retain_unresolved_activation_handoff(
                    coordinator,
                    handoff_reservation,
                )
            else:
                _release_activation_handoff_reservation_preserving_primary(
                    handoff_reservation
                )
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            _sanitize_launcher_exception(exc)
            exc = None
        except Exception as exc:
            _sanitize_launcher_exception(exc)
            exc = None
        raise
    except Exception as exc:
        _sanitize_launcher_exception(exc)
        exc = None
        try:
            if coordinator is not None:
                _retain_unresolved_activation_handoff(
                    coordinator,
                    handoff_reservation,
                )
                _retry_unresolved_activation_handoffs()
            else:
                _release_activation_handoff_reservation_preserving_primary(
                    handoff_reservation
                )
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as cleanup_exc:
            _sanitize_launcher_exception(cleanup_exc)
            cleanup_exc = None
        except Exception as cleanup_exc:
            _sanitize_launcher_exception(cleanup_exc)
            cleanup_exc = None
        print(
            "Durable Google login could not start safely.",
            file=sys.stderr,
        )
        return 2

    tls_scope = None
    tls_ownership = None
    tls_context = None
    pending = None
    runtime = None
    server = None
    server_ownership = None
    serve_outcome = None
    primary_control = None
    primary_failed = False
    ready_reached = False
    runtime_uses_coordinator = False
    cleanup_report = coordinator.snapshot()

    def prepare_tls_workspace():
        nonlocal tls_ownership, tls_scope
        _emit_checkpoint(_checkpoint_observer, "configuration_resolved")
        if tls_ownership is not None:
            raise RuntimeError("tls_workspace_already_owned")
        tls_ownership = _TlsWorkspaceOwnership()
        coordinator.own(
            "tls_workspace",
            tls_ownership,
            _close_owned_tls_workspace,
            probe=_owned_tls_workspace_terminal,
            dependencies=(
                "serve_thread",
                "server_shutdown",
                "accepted_sockets",
                "request_threads",
                "listener_socket",
            ),
        )
        tls_scope = _construct_owned_tls_scope(
            (
                _ephemeral_tls_context
                if _tls_context_factory is None
                else _tls_context_factory
            ),
            tls_ownership,
        )
        prepare = getattr(tls_scope, "prepare_workspace", None)
        if callable(prepare):
            prepare()
        _emit_checkpoint(_checkpoint_observer, "tls_workspace_ready")

    try:
        if _runtime_builder is None:
            pending = prepare_durable_google_login_activation(
                args.config,
                _pre_secret_preparer=prepare_tls_workspace,
                _cleanup_coordinator=coordinator,
                _checkpoint=_checkpoint_observer,
                _handoff_reservation=handoff_reservation,
            )
            configuration = pending.configuration
            browser_integration = pending.browser_integration
        else:
            runtime = _runtime_builder(args.config)
            coordinator.own(
                "browser_integration",
                runtime,
                _close_legacy_runtime,
                dependencies=(
                    "route_integration",
                    "request_threads",
                ),
            )
            configuration = runtime.configuration
            browser_integration = runtime.browser_integration
            prepare_tls_workspace()
        _emit_checkpoint(_checkpoint_observer, "runtime_prepared")

        handler = _construct_production_handler(
            browser_integration,
            configuration.public_origin,
            require_profile_creation=_runtime_builder is None,
        )
        if _server_factory is None:
            server_factory = _DrainingThreadingHTTPServer
        else:
            server_factory = _server_factory
        server_ownership = _ServerOwnership()
        coordinator.own(
            "listener_socket",
            server_ownership,
            _close_owned_server_listener,
            probe=_owned_server_listener_terminal,
        )
        coordinator.own(
            "inactive_server",
            server_ownership,
            _close_owned_server_high_level,
            probe=_owned_server_high_level_terminal,
        )
        server = _construct_owned_server(
            server_factory,
            (configuration.bind_host, configuration.bind_port),
            _UnpublishedRequestHandler,
            server_ownership,
        )
        tls_ownership.attach_server(server)
        _emit_checkpoint(_checkpoint_observer, "inactive_server")
        set_shutdown_notification = getattr(
            server,
            "set_shutdown_notification",
            None,
        )
        if callable(set_shutdown_notification):
            if (
                set_shutdown_notification(
                    lambda: signal_state.requested
                )
                is not True
            ):
                raise RuntimeError("shutdown_notification_failed")

        tls_context = _build_tls_context(tls_scope)
        _emit_checkpoint(_checkpoint_observer, "tls_context")
        set_tls_context = getattr(server, "set_tls_context", None)
        if (
            not callable(set_tls_context)
            or set_tls_context(
                tls_context,
                handshake_timeout=_TLS_HANDSHAKE_SECONDS,
            )
            is not True
        ):
            raise RuntimeError("tls_server_configuration_failed")
        server_ownership.transition(
            "inactive",
            "tls_configured",
        )
        tls_context = None
        _emit_checkpoint(_checkpoint_observer, "tls_wrapped")

        coordinator.own(
            "route_integration",
            server,
            _detach_server_handler,
            dependencies=("request_threads",),
        )
        if _publish_server_handler(server, handler) is not True:
            raise RuntimeError("route_publication_failed")
        coordinator.own(
            "request_threads",
            server,
            _drain_server_request_threads,
            probe=_server_request_threads_terminal,
        )
        coordinator.own(
            "accepted_sockets",
            server,
            _close_server_accepted_sockets,
            probe=_server_accepted_sockets_terminal,
        )
        coordinator.own(
            "server_shutdown",
            server,
            _begin_server_shutdown,
        )
        _emit_checkpoint(_checkpoint_observer, "routes_published")

        if pending is not None:
            runtime = pending.complete_activation()
            runtime_uses_coordinator = True
            pending = None
        _emit_checkpoint(_checkpoint_observer, "final_reverification")

        signal_state.install()
        _emit_checkpoint(_checkpoint_observer, "signals_installed")
        if not signal_state.requested:
            server.server_bind()
            server_ownership.transition("tls_configured", "bound")
            _emit_checkpoint(_checkpoint_observer, "bound")
        if not signal_state.requested:
            server.server_activate()
            server_ownership.transition("bound", "active")
            _emit_checkpoint(_checkpoint_observer, "activated")
        if not signal_state.requested:
            _emit_checkpoint(_checkpoint_observer, "before_ready")
        if not signal_state.requested:
            serve_outcome = _ServeOutcome()
            set_serve_lifecycle = getattr(
                server,
                "set_serve_lifecycle",
                None,
            )
            if (
                not callable(set_serve_lifecycle)
                or set_serve_lifecycle(serve_outcome, signal_state)
                is not True
            ):
                raise RuntimeError("serve_lifecycle_configuration_failed")
            serve_thread = threading.Thread(
                target=_serve_in_thread,
                args=(server, serve_outcome, signal_state),
                name="wahojobs-durable-login-serve",
                daemon=False,
            )
            coordinator.own(
                "serve_thread",
                (server, serve_thread, serve_outcome),
                _stop_serve_thread,
                probe=_serve_thread_terminal,
            )
            set_serve_thread = getattr(
                server,
                "set_serve_thread",
                None,
            )
            if (
                callable(set_serve_thread)
                and set_serve_thread(serve_thread) is not True
            ):
                raise RuntimeError("serve_thread_registration_failed")
            if serve_outcome.begin_starting() is not True:
                raise RuntimeError("serve_lifecycle_start_failed")
            serve_thread.start()
            startup_state = _wait_for_serve_startup(
                server,
                serve_outcome,
                signal_state,
            )
            if startup_state == "serving":
                _emit_checkpoint(
                    _checkpoint_observer,
                    "ready_commit",
                )
                readiness_claimed = (
                    server.claim_serving_readiness() is True
                )
                readiness_state = serve_outcome.state
                if readiness_claimed:
                    server_ownership.transition("active", "serving")
                    ready_reached = True
                    _emit_checkpoint(_checkpoint_observer, "ready")
                elif readiness_state == "failed":
                    (
                        primary_control,
                        primary_failed,
                    ) = _consume_serve_outcome(
                        serve_outcome,
                        primary_control,
                        primary_failed,
                    )
                elif (
                    readiness_state in {"stopping", "stopped"}
                    or signal_state.requested
                ):
                    serve_outcome.request_stop()
                    server_ownership.begin_stopping()
                    _begin_server_shutdown(server)
                else:
                    (
                        primary_control,
                        primary_failed,
                    ) = _consume_serve_outcome(
                        serve_outcome,
                        primary_control,
                        True,
                    )
            elif startup_state == "failed":
                (
                    primary_control,
                    primary_failed,
                ) = _consume_serve_outcome(
                    serve_outcome,
                    primary_control,
                    primary_failed,
                )
            elif not signal_state.requested:
                primary_failed = True
        if ready_reached:
            print("Wahojobs durable Google login")
            print(f"Open: {configuration.public_origin}/login")
            print("Press Ctrl+C to stop.")
            while (
                not signal_state.event.wait(0.05)
                and not serve_outcome.done.is_set()
            ):
                pass
            if signal_state.requested:
                serve_outcome.request_stop()
                server_ownership.begin_stopping()
                _begin_server_shutdown(server)
            if serve_outcome.done.is_set():
                (
                    primary_control,
                    primary_failed,
                ) = _consume_serve_outcome(
                    serve_outcome,
                    primary_control,
                    primary_failed,
                    shutdown_requested=signal_state.requested,
                )
    except KeyboardInterrupt as exc:
        signal_state.request("sigint")
        _sanitize_launcher_exception(exc)
        exc = None
    except (SystemExit, GeneratorExit) as exc:
        primary_control = exc
        _sanitize_launcher_exception(exc)
        exc = None
    except Exception as exc:
        primary_failed = True
        _sanitize_launcher_exception(exc)
        exc = None
    finally:
        tls_context = None
        for _attempt in range(2):
            try:
                if runtime is not None and runtime_uses_coordinator:
                    cleanup_report = runtime.close(
                        _preserve_primary=True,
                    )
                elif pending is not None:
                    cleanup_report = pending.close(
                        _preserve_primary=True,
                    )
                else:
                    cleanup_report = coordinator.cleanup(
                        preserve_primary=True,
                    )
            except (
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                _sanitize_launcher_exception(exc)
                exc = None
                cleanup_report = coordinator.snapshot()
            except Exception as exc:
                _sanitize_launcher_exception(exc)
                exc = None
                cleanup_report = coordinator.snapshot()
            if cleanup_report.cleanup_complete:
                break
        if not cleanup_report.cleanup_complete:
            cleanup_authority = (
                runtime
                if runtime is not None and runtime_uses_coordinator
                else (
                    pending
                    if pending is not None
                    else coordinator
                )
            )
            try:
                _retain_unresolved_activation_handoff(
                    cleanup_authority,
                    handoff_reservation,
                )
            except (
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                _sanitize_launcher_exception(exc)
                exc = None
            except Exception as exc:
                _sanitize_launcher_exception(exc)
                exc = None
            cleanup_authority = None
        else:
            released = (
                _release_activation_handoff_reservation_preserving_primary(
                    handoff_reservation
                )
            )
            if not released:
                try:
                    _retry_unresolved_activation_handoffs()
                    cleanup_report = coordinator.snapshot()
                except (
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    _sanitize_launcher_exception(exc)
                    exc = None
                except Exception as exc:
                    _sanitize_launcher_exception(exc)
                    exc = None
                _release_activation_handoff_reservation_preserving_primary(
                    handoff_reservation
                )

    if serve_outcome is not None:
        ready_reached = serve_outcome.ready_state_reached
    result = _ShutdownResult(
        ready_state_reached=ready_reached,
        shutdown_requested=signal_state.requested,
        resources_closed=cleanup_report.closed_resources,
        unresolved_resource_categories=(
            cleanup_report.unresolved_resources
        ),
        cleanup_complete=cleanup_report.cleanup_complete,
        cleanup_failure_categories=cleanup_report.failure_categories,
        signal_category=signal_state.category,
    )
    if _shutdown_result_observer is not None:
        try:
            _shutdown_result_observer(result)
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            _sanitize_launcher_exception(exc)
            exc = None
        except Exception as exc:
            _sanitize_launcher_exception(exc)
            exc = None

    if primary_control is not None:
        propagated = primary_control
        primary_control = None
        raise propagated from None
    if primary_failed:
        print(
            "Durable Google login could not start safely.",
            file=sys.stderr,
        )
    if not result.cleanup_complete:
        return 3
    if primary_failed:
        return 2
    if signal_state.category is not None:
        print("Stopped durable Google login.")
        return _SIGNAL_EXIT_STATUS[signal_state.category]
    if signal_state.requested:
        return 2
    return 0


def _emit_checkpoint(observer, category):
    if observer is not None:
        observer(category)


def _restore_signal_handlers(state):
    return state.restore()


def _close_tls_scope(scope):
    close = getattr(scope, "close", None)
    if callable(close):
        return close() is not False
    exit_scope = getattr(scope, "__exit__", None)
    if callable(exit_scope):
        exit_scope(None, None, None)
        return True
    return True


def _close_owned_tls_workspace(ownership):
    return ownership.close()


def _owned_tls_workspace_terminal(ownership):
    return ownership.terminal()


def _release_server_tls_context(server):
    release = getattr(server, "release_tls_context", None)
    if callable(release):
        return release() is not False
    return True


def _construct_owned_tls_scope(factory, ownership):
    scope = None
    try:
        if factory is _ephemeral_tls_context:
            scope = factory(_construction_ownership=ownership)
        else:
            scope = factory()
        if not ownership.owns_scope(scope):
            ownership.acquire_scope(scope)
        return scope
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        _ensure_tls_scope_ownership_preserving_primary(ownership, scope)
        raise
    except Exception:
        _ensure_tls_scope_ownership_preserving_primary(ownership, scope)
        raise


def _ensure_tls_scope_ownership_preserving_primary(ownership, scope):
    if scope is None or ownership.owns_scope(scope):
        return
    try:
        ownership.acquire_scope(scope)
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        _sanitize_launcher_exception(exc)
        exc = None
    except Exception as exc:
        _sanitize_launcher_exception(exc)
        exc = None
    if ownership.owns_scope(scope):
        return
    try:
        _close_tls_scope(scope)
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        _sanitize_launcher_exception(exc)
        exc = None
    except Exception as exc:
        _sanitize_launcher_exception(exc)
        exc = None


def _close_legacy_runtime(runtime):
    result = runtime.close()
    return result is not False


def _build_tls_context(scope):
    build = getattr(scope, "build_context", None)
    if callable(build):
        return build()
    return scope.__enter__()


def _close_server_high_level(server):
    server.server_close()
    return True


def _construct_owned_server(factory, address, handler, ownership):
    server = None
    try:
        if factory is _DrainingThreadingHTTPServer:
            server = factory(
                address,
                handler,
                False,
                _construction_ownership=ownership,
            )
        else:
            server = factory(address, handler, False)
        if not ownership.owns(server):
            ownership.acquire(server)
        return server
    except (
        KeyboardInterrupt,
        SystemExit,
        GeneratorExit,
    ):
        _ensure_server_ownership_preserving_primary(
            ownership,
            server,
        )
        raise
    except Exception:
        _ensure_server_ownership_preserving_primary(
            ownership,
            server,
        )
        raise


def _ensure_server_ownership_preserving_primary(ownership, server):
    if server is None or ownership.owns(server):
        return
    try:
        ownership.acquire(server)
        return
    except (
        KeyboardInterrupt,
        SystemExit,
        GeneratorExit,
    ) as exc:
        _sanitize_launcher_exception(exc)
        exc = None
    except Exception as exc:
        _sanitize_launcher_exception(exc)
        exc = None
    if ownership.owns(server):
        return
    try:
        listener = getattr(server, "socket", None)
    except (
        KeyboardInterrupt,
        SystemExit,
        GeneratorExit,
    ) as exc:
        _sanitize_launcher_exception(exc)
        exc = None
        listener = None
    except Exception as exc:
        _sanitize_launcher_exception(exc)
        exc = None
        listener = None
    try:
        server.server_close()
    except (
        KeyboardInterrupt,
        SystemExit,
        GeneratorExit,
    ) as exc:
        _sanitize_launcher_exception(exc)
        exc = None
    except Exception as exc:
        _sanitize_launcher_exception(exc)
        exc = None
    if listener is not None and not _socket_is_closed(listener):
        _close_socket_preserving_primary(listener)


def _server_high_level_terminal(server):
    return False


def _close_owned_server_high_level(ownership):
    return ownership.close_high_level()


def _owned_server_high_level_terminal(ownership):
    return ownership.high_level_terminal()


def _close_owned_server_listener(ownership):
    return ownership.close_listener()


def _owned_server_listener_terminal(ownership):
    return ownership.listener_terminal()


def _close_server_listener(server):
    close_listener = getattr(server, "close_listener", None)
    if callable(close_listener):
        return close_listener() is not False
    listener = getattr(server, "socket", None)
    if listener is None or not callable(getattr(listener, "close", None)):
        return True
    return _close_socket_independently(listener)


def _server_listener_closed(server):
    listener = getattr(server, "socket", None)
    if listener is None or not callable(getattr(listener, "fileno", None)):
        return True
    return _socket_is_closed(listener)


def _publish_server_handler(server, handler):
    publish = getattr(server, "publish_handler", None)
    if callable(publish):
        return publish(handler)
    server.RequestHandlerClass = handler
    return True


def _detach_server_handler(server):
    detach = getattr(server, "detach_route_integration", None)
    if callable(detach):
        return detach() is not False
    handler = getattr(server, "RequestHandlerClass", None)
    server.RequestHandlerClass = _UnpublishedRequestHandler
    if handler is not None and hasattr(
        handler,
        "_durable_google_login_browser_integration",
    ):
        handler._durable_google_login_browser_integration = None
    handler = None
    return True


def _drain_server_request_threads(server):
    drain = getattr(server, "drain_request_threads", None)
    if callable(drain):
        return drain(_REQUEST_DRAIN_SECONDS) is not False
    return True


def _server_request_threads_terminal(server):
    return _server_resource_counts(server)["request_threads"] == 0


def _close_server_accepted_sockets(server):
    close = getattr(server, "close_accepted_sockets", None)
    if callable(close):
        return close() is not False
    return True


def _server_accepted_sockets_terminal(server):
    counts = _server_resource_counts(server)
    return (
        counts["accepted_sockets"] == 0
        and counts["pending_handshakes"] == 0
    )


def _begin_server_shutdown(server):
    begin = getattr(server, "begin_shutdown", None)
    if callable(begin):
        return begin() is not False
    return True


def _serve_in_thread(server, outcome, signal_state=None):
    outcome.publish_thread_entry()
    try:
        server.serve_forever(poll_interval=_SERVE_POLL_SECONDS)
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        _sanitize_launcher_exception(exc)
        outcome.publish_failure(exc, signal_state)
        exc = None
    except Exception as exc:
        _sanitize_launcher_exception(exc)
        outcome.publish_failure(exc, signal_state)
        exc = None
    else:
        outcome.publish_success(signal_state)
    finally:
        server = None
        outcome = None
        signal_state = None


def _stop_serve_thread(resource):
    if type(resource) is not tuple or len(resource) != 3:
        return False
    server, thread, outcome = resource
    first_control = None
    failed = False
    outcome.request_stop()
    if thread.is_alive():
        try:
            _begin_server_shutdown(server)
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            first_control = exc
            _sanitize_launcher_exception(exc)
            exc = None
            failed = True
        except Exception as exc:
            _sanitize_launcher_exception(exc)
            exc = None
            failed = True
        try:
            close_pending = getattr(
                server,
                "close_pending_handshakes",
                None,
            )
            if callable(close_pending) and close_pending() is False:
                failed = True
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            if first_control is None:
                first_control = exc
            _sanitize_launcher_exception(exc)
            exc = None
            failed = True
        except Exception as exc:
            _sanitize_launcher_exception(exc)
            exc = None
            failed = True
        try:
            thread.join(_SERVE_THREAD_DRAIN_SECONDS)
        except (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ) as exc:
            if first_control is None:
                first_control = exc
            _sanitize_launcher_exception(exc)
            exc = None
            failed = True
        except Exception as exc:
            _sanitize_launcher_exception(exc)
            exc = None
            failed = True
    if not thread.is_alive() and not outcome.done.is_set():
        outcome.finish_unstarted()
    pending_complete = True
    counts = _server_resource_counts(server)
    if counts["pending_handshakes"]:
        pending_complete = False
    terminal = (
        not thread.is_alive()
        and outcome.terminal()
        and pending_complete
    )
    server = None
    thread = None
    outcome = None
    if first_control is not None:
        propagated = first_control
        first_control = None
        raise propagated from None
    if failed and terminal:
        raise _ServerCleanupFailure()
    return terminal


def _serve_thread_terminal(resource):
    if type(resource) is not tuple or len(resource) != 3:
        return False
    server, thread, outcome = resource
    counts = _server_resource_counts(server)
    return (
        not thread.is_alive()
        and outcome.terminal()
        and counts["pending_handshakes"] == 0
    )


def _wait_for_serve_startup(server, outcome, signal_state):
    state = outcome.wait_for_startup(
        _SERVE_STARTUP_SECONDS,
        signal_state,
    )
    if state in {"stopping", "failed"}:
        outcome.request_stop()
        _begin_server_shutdown(server)
    return state


def _consume_serve_outcome(
    outcome,
    primary_control,
    primary_failed,
    *,
    shutdown_requested=False,
):
    with outcome.lock:
        if outcome.control is not None and primary_control is None:
            primary_control = outcome.control
            outcome.control = None
        elif outcome.failed:
            primary_failed = True
        elif outcome.done.is_set() and not shutdown_requested:
            primary_failed = True
    return primary_control, primary_failed


def _server_resource_counts(server):
    counts = getattr(server, "resource_counts", None)
    if callable(counts):
        result = counts()
        if type(result) is dict:
            return {
                "listener": int(result.get("listener", 0)),
                "accepted_sockets": int(
                    result.get("accepted_sockets", 0)
                ),
                "pending_handshakes": int(
                    result.get("pending_handshakes", 0)
                ),
                "request_threads": int(result.get("request_threads", 0)),
                "serve_threads": int(result.get("serve_threads", 0)),
                "route_integrations": int(
                    result.get("route_integrations", 0)
                ),
            }
    return {
        "listener": 0 if _server_listener_closed(server) else 1,
        "accepted_sockets": 0,
        "pending_handshakes": 0,
        "request_threads": 0,
        "serve_threads": 0,
        "route_integrations": 0,
    }


def _cleanup_tls_temporary_preserving_primary(temporary):
    if temporary is None:
        return True
    try:
        temporary.cleanup()
        return True
    except (KeyboardInterrupt, SystemExit, GeneratorExit) as exc:
        _sanitize_launcher_exception(exc)
        exc = None
        return False
    except Exception as exc:
        _sanitize_launcher_exception(exc)
        exc = None
        return False


class _EphemeralTlsContext:
    __slots__ = (
        "_context",
        "_directory",
        "_lock",
        "_state",
        "_temporary",
    )

    def __init__(self, *, _construction_ownership=None):
        if (
            _construction_ownership is not None
            and not isinstance(
                _construction_ownership,
                _TlsWorkspaceOwnership,
            )
        ):
            raise RuntimeError("invalid_tls_construction_owner")
        self._temporary = None
        self._directory = None
        self._context = None
        self._lock = threading.Lock()
        self._state = "new"
        if _construction_ownership is not None:
            _construction_ownership.acquire_scope(self)

    def prepare_workspace(self):
        with self._lock:
            if self._state == "workspace":
                return True
            if self._state != "new":
                raise RuntimeError("tls_workspace_state_invalid")
            self._state = "preparing"
        temporary = None
        directory = None
        try:
            temporary = tempfile.TemporaryDirectory(
                prefix="wahojobs-durable-login-tls-"
            )
            directory = Path(temporary.name).resolve()
            if directory == ROOT or ROOT in directory.parents:
                raise RuntimeError("unsafe_tls_temporary_directory")
            with self._lock:
                self._temporary = temporary
                self._directory = directory
                self._state = "workspace"
            temporary = None
            directory = None
            return True
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self._lock:
                owned_temporary = self._temporary
                owned_directory = self._directory
            if owned_temporary is not None:
                temporary = owned_temporary
                if owned_directory is not None:
                    directory = owned_directory
            cleanup_complete = _cleanup_tls_temporary_preserving_primary(
                temporary
            )
            with self._lock:
                self._temporary = None if cleanup_complete else temporary
                self._directory = None if cleanup_complete else directory
                self._state = (
                    "closed" if cleanup_complete else "unresolved"
                )
            raise
        except Exception:
            with self._lock:
                owned_temporary = self._temporary
                owned_directory = self._directory
            if owned_temporary is not None:
                temporary = owned_temporary
                if owned_directory is not None:
                    directory = owned_directory
            cleanup_complete = _cleanup_tls_temporary_preserving_primary(
                temporary
            )
            with self._lock:
                self._temporary = None if cleanup_complete else temporary
                self._directory = None if cleanup_complete else directory
                self._state = (
                    "closed" if cleanup_complete else "unresolved"
                )
            raise

    def build_context(self):
        with self._lock:
            state = self._state
        if state == "new":
            self.prepare_workspace()
        with self._lock:
            if self._state != "workspace":
                raise RuntimeError("tls_workspace_state_invalid")
            self._state = "building"
            temporary = self._temporary
            directory = self._directory
        try:
            (
                certificate_path,
                certificate_identity,
                key_path,
                key_identity,
            ) = _write_ephemeral_certificate(directory)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            with self._lock:
                if self._state != "building":
                    raise RuntimeError("tls_workspace_state_invalid")
                self._context = context
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.options |= ssl.OP_NO_COMPRESSION
            _attest_ephemeral_tls_file(
                certificate_path,
                certificate_identity,
            )
            _attest_ephemeral_tls_file(key_path, key_identity)
            context.load_cert_chain(
                certfile=str(certificate_path),
                keyfile=str(key_path),
            )
            _attest_ephemeral_tls_file(
                certificate_path,
                certificate_identity,
            )
            _attest_ephemeral_tls_file(key_path, key_identity)
            key_path.unlink()
            certificate_path.unlink()
            temporary.cleanup()
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            cleanup_complete = _cleanup_tls_temporary_preserving_primary(
                temporary
            )
            self._clear_failed_build(cleanup_complete)
            raise
        except Exception:
            cleanup_complete = _cleanup_tls_temporary_preserving_primary(
                temporary
            )
            self._clear_failed_build(cleanup_complete)
            raise
        with self._lock:
            self._temporary = None
            self._directory = None
            self._state = "built"
        certificate_path = None
        certificate_identity = None
        key_path = None
        key_identity = None
        temporary = None
        directory = None
        return context

    def __enter__(self):
        return self.build_context()

    def close(self):
        with self._lock:
            if self._state == "closed":
                return True
            temporary = self._temporary
            self._state = "closing"
        if temporary is not None:
            try:
                temporary.cleanup()
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                with self._lock:
                    self._state = "unresolved"
                raise
            except Exception:
                with self._lock:
                    self._state = "unresolved"
                raise
        with self._lock:
            self._temporary = None
            self._directory = None
            self._context = None
            self._state = "closed"
        return True

    def _clear_failed_build(self, cleanup_complete):
        with self._lock:
            if cleanup_complete:
                self._temporary = None
                self._directory = None
            self._context = None
            self._state = "closed" if cleanup_complete else "unresolved"

    def __exit__(self, _exc_type, _exc, _traceback):
        self.close()
        return False

    def __repr__(self):
        with self._lock:
            state = self._state
        return f"_EphemeralTlsContext(<{state}>)"


def _ephemeral_tls_context(*, _construction_ownership=None):
    return _EphemeralTlsContext(
        _construction_ownership=_construction_ownership
    )


def _write_ephemeral_certificate(directory):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    certificate_path = directory / "localhost-cert.pem"
    key_path = directory / "localhost-key.pem"
    certificate_bytes = None
    key_bytes = None
    key_buffer = None
    private_key = None
    try:
        private_key = rsa.generate_private_key(
            public_exponent=65_537,
            key_size=2_048,
        )
        now = datetime.now(timezone.utc)
        subject = issuer = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]
        )
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(
                (now - timedelta(minutes=5)).replace(tzinfo=None)
            )
            .not_valid_after(
                (now + timedelta(hours=24)).replace(tzinfo=None)
            )
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("localhost"),
                        x509.IPAddress(
                            ipaddress.ip_address("127.0.0.1")
                        ),
                    ]
                ),
                critical=False,
            )
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None),
                critical=True,
            )
            .sign(private_key, hashes.SHA256())
        )
        certificate_bytes = certificate.public_bytes(
            serialization.Encoding.PEM
        )
        key_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        key_buffer = bytearray(key_bytes)
        key_bytes = None
        certificate_identity = _write_ephemeral_tls_file(
            certificate_path,
            certificate_bytes,
        )
        key_identity = _write_ephemeral_tls_file(
            key_path,
            key_buffer,
        )
        if certificate_identity[:2] == key_identity[:2]:
            raise RuntimeError("unsafe_tls_file_identity")
        return (
            certificate_path,
            certificate_identity,
            key_path,
            key_identity,
        )
    finally:
        certificate_bytes = None
        key_bytes = None
        _clear_mutable_tls_buffer(key_buffer)
        key_buffer = None
        private_key = None


def _write_ephemeral_tls_file(path, payload):
    if not isinstance(path, Path) or type(payload) not in (bytes, bytearray):
        raise RuntimeError("invalid_ephemeral_tls_file")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    handle = None
    view = None
    chunk = None
    try:
        descriptor = os.open(path, flags, 0o600)
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "wb", buffering=0)
        descriptor = None
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            chunk = view[offset:]
            try:
                written = handle.write(chunk)
            finally:
                chunk.release()
                chunk = None
            if (
                type(written) is not int
                or written <= 0
                or written > len(view) - offset
            ):
                raise RuntimeError("ephemeral_tls_file_write_failed")
            offset += written
        metadata = os.fstat(handle.fileno())
        identity = _ephemeral_tls_file_identity(metadata)
        _require_ephemeral_tls_file_metadata(
            metadata,
            expected_size=len(view),
        )
        return identity
    finally:
        if chunk is not None:
            chunk.release()
        if view is not None:
            view.release()
        if handle is not None:
            handle.close()
        if descriptor is not None:
            os.close(descriptor)


def _attest_ephemeral_tls_file(path, expected_identity):
    if (
        not isinstance(path, Path)
        or type(expected_identity) is not tuple
        or len(expected_identity) != 7
    ):
        raise RuntimeError("unsafe_tls_file_identity")
    metadata = path.lstat()
    _require_ephemeral_tls_file_metadata(
        metadata,
        expected_size=expected_identity[2],
    )
    actual_identity = _ephemeral_tls_file_identity(metadata)
    if (
        actual_identity[:4] != expected_identity[:4]
        or actual_identity[5:] != expected_identity[5:]
        or (
            os.name != "nt"
            and actual_identity[4] != expected_identity[4]
        )
    ):
        raise RuntimeError("unsafe_tls_file_identity")


def _ephemeral_tls_file_identity(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_nlink,
    )


def _require_ephemeral_tls_file_metadata(metadata, *, expected_size):
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size != expected_size
        or (
            os.name != "nt"
            and stat.S_IMODE(metadata.st_mode) != 0o600
        )
    ):
        raise RuntimeError("unsafe_tls_file_identity")


def _clear_mutable_tls_buffer(value):
    if type(value) is bytearray:
        for index in range(len(value)):
            value[index] = 0
        value.clear()


def _main_cli():
    try:
        return main()
    except SystemExit as exc:
        try:
            code = exc.code
        except Exception:
            code = None
        _sanitize_launcher_exception(exc)
        if type(code) is int and code in {0, 2}:
            return code
        return 2
    except KeyboardInterrupt as exc:
        _sanitize_launcher_exception(exc)
        exc = None
        return _SIGNAL_EXIT_STATUS["sigint"]
    except GeneratorExit as exc:
        _sanitize_launcher_exception(exc)
        exc = None
        return 2


if __name__ == "__main__":
    raise SystemExit(_main_cli())
