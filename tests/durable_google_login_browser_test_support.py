"""Temporary-only fixtures for the dedicated durable-login browser surface."""

from __future__ import annotations

from contextlib import contextmanager, ExitStack
import ctypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
import http.client
import hashlib
import io
import ipaddress
import json
import multiprocessing
import os
from pathlib import Path
import socket
import ssl
import sqlite3
import sys
import tempfile
import threading
from unittest import mock
from urllib.parse import urlencode, urlsplit

from requests.adapters import HTTPAdapter

import scripts.google_oidc_authorization_transactions_migration as migration_006
from scripts.durable_google_login_app import (
    _DrainingThreadingHTTPServer,
    _ServeOutcome,
    _SignalShutdownState,
    _UnpublishedRequestHandler,
    _ephemeral_tls_context,
    _serve_in_thread,
)
from scripts.local_product_app import make_handler
from tests.accounts_test_support import (
    INVITATION_KEY,
    NOW as ACCOUNT_CREATED_AT,
)
from tests.google_oidc_authorization_transactions_test_support import (
    LOOKUP_KEY_MATERIAL,
    PROTECTION_KEY_MATERIAL,
)
import tests.google_oidc_gateway_test_support as google_support
from tests.google_oidc_gateway_test_support import (
    CLIENT_ID,
    CLIENT_SECRET,
    ManualClock,
    NOW,
    make_real_gateway,
    seed_existing_google_identity,
)
from tests.ownership_test_support import (
    add_activation_event,
    add_binding,
    add_principal,
)
from tests.persistent_profile_canonical_v2_test_support import (
    install_canonical_v2_profiles,
)
from tests.persistent_profiles_repository_test_support import create_command
from wahojobs.persistent_profiles import TrustedPrincipalContext
from wahojobs.persistent_profiles_repository import (
    create_persistent_profile,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SUFFIX = "durable-browser-login"
FIXTURE_SUBJECT = f"google-subject-{FIXTURE_SUFFIX}"
FIXTURE_PRINCIPAL_SUFFIX = "71"


@dataclass(slots=True)
class TemporaryBrowserLoginState:
    directory: Path
    database_path: Path
    configuration_path: Path
    public_origin: str
    redirect_uri: str
    subject: str
    account_id: str
    principal_id: str
    profile_id: str
    clock: ManualClock
    gateway_harnesses: list = field(default_factory=list, repr=False)
    gateway_options: dict = field(default_factory=dict, repr=False)

    def gateway_factory(self, configuration, client_secret):
        harness = make_real_gateway(
            clock=self.clock,
            client_id=configuration.google_client_id,
            client_secret=client_secret,
            redirect_uri=configuration.google_redirect_uri,
            subject=self.subject,
            invitation_lookup_key=configuration.invitation_lookup_key,
            **self.gateway_options,
        )
        self.gateway_harnesses.append(harness)
        return harness.gateway

    @property
    def gateway_harness(self):
        if len(self.gateway_harnesses) != 1:
            raise AssertionError("exactly_one_gateway_harness_required")
        return self.gateway_harnesses[0]

    def close_harnesses(self):
        for harness in reversed(self.gateway_harnesses):
            harness.close()
        self.gateway_harnesses.clear()


@dataclass(frozen=True, slots=True)
class BrowserHttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header_values(self, name):
        lowered = name.lower()
        return tuple(
            value
            for candidate, value in self.headers
            if candidate.lower() == lowered
        )


@contextmanager
def temporary_browser_login_state(
    *,
    port=8443,
    mutate_configuration=None,
    install_migrations=True,
    seed_existing_identity=True,
    enable_invited_provisioning=False,
):
    with tempfile.TemporaryDirectory(
        prefix="wahojobs-durable-browser-login-"
    ) as raw_directory:
        directory = Path(raw_directory).resolve()
        if directory == ROOT or ROOT in directory.parents:
            raise AssertionError("fixture_directory_inside_repository")
        if any((parent / ".git").exists() for parent in (directory, *directory.parents)):
            raise AssertionError("fixture_directory_inside_checkout")

        database_path = directory / "browser-login.sqlite"
        account_id = ""
        principal_id = ""
        profile_id = ""
        if install_migrations:
            connection = install_canonical_v2_profiles(database_path)
            try:
                migration_006.apply_google_oidc_authorization_transactions_migration(
                    connection,
                    requested_path=database_path,
                    expected_identity=migration_006.database_file_identity(
                        database_path
                    ),
                )
                connection.row_factory = sqlite3.Row
                connection.text_factory = str
                if seed_existing_identity:
                    created = seed_existing_google_identity(
                        connection,
                        suffix=FIXTURE_SUFFIX,
                        created_at=ACCOUNT_CREATED_AT,
                    )
                    account_id = created.user.user_id
                    principal_id = add_principal(
                        connection,
                        suffix=FIXTURE_PRINCIPAL_SUFFIX,
                        environment="test",
                        principal_type="account_native",
                        status="active",
                        claim_policy="account_native",
                        exclusive=1,
                    )
                    binding_id = add_binding(
                        connection,
                        principal_id,
                        account_id,
                        suffix=FIXTURE_PRINCIPAL_SUFFIX,
                        environment="test",
                    )
                    add_activation_event(
                        connection,
                        principal_id,
                        account_id,
                        binding_id,
                        suffix=FIXTURE_PRINCIPAL_SUFFIX,
                        environment="test",
                    )
                    connection.commit()
                    principal = TrustedPrincipalContext(
                        principal_id=principal_id,
                        environment_namespace="test",
                        principal_type="account_native",
                        lifecycle_status="active",
                        claim_policy="account_native",
                        exclusive_account_binding=True,
                        eligibility_mode="account_native",
                        active_owner_binding=True,
                    )
                    created_profile = create_persistent_profile(
                        connection,
                        create_command(
                            principal,
                            idempotency_key=(
                                "profile-create-browser-login"
                            ),
                            source_text=(
                                "Existing profile for the durable browser "
                                "login fixture."
                            ),
                        ),
                    )
                    profile_id = created_profile.profile_id
            finally:
                connection.close()
        else:
            with database_path.open("xb"):
                pass

        client_secret_path = directory / "google-client-secret.bin"
        invitation_lookup_key_path = (
            directory / "invitation-lookup.key"
            if enable_invited_provisioning
            else None
        )
        lookup_path = directory / "lookup-1.key"
        protection_path = directory / "protection-11.key"
        _write_secret(client_secret_path, CLIENT_SECRET)
        if invitation_lookup_key_path is not None:
            _write_secret(invitation_lookup_key_path, INVITATION_KEY)
        _write_secret(lookup_path, LOOKUP_KEY_MATERIAL[1])
        _write_secret(protection_path, PROTECTION_KEY_MATERIAL[11])

        public_origin = f"https://localhost:{port}"
        redirect_uri = public_origin + "/auth/google/callback"
        document = {
            "version": 1,
            "environment": "test",
            "database_path": str(database_path),
            "bind_host": "127.0.0.1",
            "bind_port": port,
            "public_origin": public_origin,
            "google_redirect_uri": redirect_uri,
            "google_client_id": CLIENT_ID,
            "google_client_secret_file": str(client_secret_path),
            "oidc_lookup_keys": [
                {"version": 1, "file": str(lookup_path)}
            ],
            "oidc_lookup_active_version": 1,
            "oidc_protection_keys": [
                {"version": 11, "file": str(protection_path)}
            ],
            "oidc_protection_active_version": 11,
            "session_idle_ttl_seconds": 3_600,
            "session_absolute_ttl_seconds": 604_800,
            "allowed_post_login_paths": ["/account/profile"],
        }
        if invitation_lookup_key_path is not None:
            document["account_invitation_lookup_key_file"] = str(
                invitation_lookup_key_path
            )
        if mutate_configuration is not None:
            mutate_configuration(document)
        configuration_path = directory / "runtime.json"
        with configuration_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                document,
                handle,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")

        state = TemporaryBrowserLoginState(
            directory=directory,
            database_path=database_path,
            configuration_path=configuration_path,
            public_origin=public_origin,
            redirect_uri=redirect_uri,
            subject=FIXTURE_SUBJECT,
            account_id=account_id,
            principal_id=principal_id,
            profile_id=profile_id,
            clock=ManualClock(NOW),
        )
        try:
            yield state
        finally:
            state.close_harnesses()


@contextmanager
def running_https_browser_app(runtime):
    configuration = runtime.configuration
    handler = make_handler(
        durable_google_login_browser_integration=runtime.browser_integration,
        exclusive_browser_integration=True,
    )
    server = None
    thread = None
    try:
        with _ephemeral_tls_context() as context:
            server = _DrainingThreadingHTTPServer(
                (configuration.bind_host, configuration.bind_port),
                _UnpublishedRequestHandler,
                bind_and_activate=False,
            )
            signal_state = _SignalShutdownState()
            outcome = _ServeOutcome()
            server.set_shutdown_notification(
                lambda: signal_state.requested
            )
            server.set_tls_context(context)
            server.publish_handler(handler)
            server.server_bind()
            server.server_activate()
            server.set_serve_lifecycle(outcome, signal_state)
            if outcome.begin_starting() is not True:
                raise AssertionError("browser_test_server_start_failed")
            thread = threading.Thread(
                target=_serve_in_thread,
                args=(server, outcome, signal_state),
                name="durable-browser-login-test-server",
                daemon=False,
            )
            thread.start()
            if outcome.wait_for_startup(1, signal_state) != "serving":
                raise AssertionError("browser_test_server_start_failed")
            if server.claim_serving_readiness() is not True:
                raise AssertionError("browser_test_server_start_failed")
            try:
                yield server
            finally:
                server.begin_shutdown()
                server.close_pending_handshakes()
                server.close_accepted_sockets()
                server.drain_request_threads(5)
                server.close_listener()
                thread.join(timeout=5)
                if thread.is_alive():
                    raise AssertionError(
                        "browser_test_server_did_not_stop"
                    )
                server.detach_route_integration()
                server.server_close()
    except (
        KeyboardInterrupt,
        SystemExit,
        GeneratorExit,
    ):
        if server is not None:
            server.begin_shutdown()
            server.close_pending_handshakes()
            server.close_accepted_sockets()
            server.close_listener()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        raise
    except Exception:
        if server is not None:
            server.begin_shutdown()
            server.close_pending_handshakes()
            server.close_accepted_sockets()
            server.close_listener()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        raise


def https_request(
    state,
    method,
    target,
    *,
    headers=(),
    body=None,
):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = http.client.HTTPSConnection(
        "127.0.0.1",
        urlsplit(state.public_origin).port,
        context=context,
        timeout=5,
    )
    request_headers = {
        "Host": urlsplit(state.public_origin).netloc,
        **dict(headers),
    }
    try:
        connection.request(
            method,
            target,
            body=body,
            headers=request_headers,
        )
        response = connection.getresponse()
        return BrowserHttpResponse(
            status=response.status,
            headers=tuple(response.getheaders()),
            body=response.read(),
        )
    finally:
        connection.close()


def cookie_values(response):
    result = {}
    for header in response.header_values("Set-Cookie"):
        pair = header.split(";", 1)[0]
        name, value = pair.split("=", 1)
        result[name] = value
    return result


def cookie_header(values):
    return "; ".join(f"{name}={value}" for name, value in values.items())


def form_body(**values):
    return urlencode(values).encode("utf-8")


class _PreparedAuthorizationUrl:
    __slots__ = ("authorization_url",)

    def __init__(self, authorization_url):
        self.authorization_url = authorization_url


def provider_callback_for(
    state,
    authorization_url,
    *,
    code="browser-code",
    claims_overrides=None,
    missing_claims=(),
):
    return state.gateway_harness.transport.callback_for(
        _PreparedAuthorizationUrl(authorization_url),
        code=code,
        base_uri=state.redirect_uri,
        claims_overrides=claims_overrides,
        missing_claims=missing_claims,
    )


@contextmanager
def loopback_and_in_memory_provider_only():
    original_socket_type = socket.socket
    original_create_connection = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo
    original_clock_now = google_support._gateway._clock_now
    original_monotonic_now = google_support._gateway._monotonic_now

    def allowed_host(value):
        if value == "localhost":
            return True
        try:
            return ipaddress.ip_address(value).is_loopback
        except (TypeError, ValueError):
            return False

    def allowed_address(value):
        return (
            type(value) is tuple
            and len(value) >= 2
            and allowed_host(value[0])
        )

    class GuardedSocket(original_socket_type):
        def connect(self, address):
            if not allowed_address(address):
                raise AssertionError("non_loopback_socket_forbidden")
            return super().connect(address)

        def connect_ex(self, address):
            if not allowed_address(address):
                raise AssertionError("non_loopback_socket_forbidden")
            return super().connect_ex(address)

        def sendto(self, data, *args):
            address = args[-1] if args else None
            if not allowed_address(address):
                raise AssertionError("non_loopback_socket_forbidden")
            return super().sendto(data, *args)

        def sendmsg(self, buffers, ancdata=(), flags=0, address=None):
            if address is not None and not allowed_address(address):
                raise AssertionError("non_loopback_socket_forbidden")
            parent = getattr(super(), "sendmsg", None)
            if parent is None:
                raise AssertionError("socket_sendmsg_unavailable")
            if address is None:
                return parent(buffers, ancdata, flags)
            return parent(buffers, ancdata, flags, address)

    def guarded_create_connection(address, *args, **kwargs):
        if not allowed_address(address):
            raise AssertionError("non_loopback_socket_forbidden")
        return original_create_connection(address, *args, **kwargs)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not allowed_host(host):
            raise AssertionError("non_loopback_dns_forbidden")
        return original_getaddrinfo(host, *args, **kwargs)

    def clock_now(configuration):
        clock = google_support._registered_clock(configuration)
        return clock() if clock is not None else original_clock_now(configuration)

    def monotonic_now(configuration):
        clock = google_support._registered_clock(configuration)
        return (
            clock.monotonic()
            if clock is not None
            else original_monotonic_now(configuration)
        )

    with (
        mock.patch.object(socket, "socket", GuardedSocket),
        mock.patch.object(
            socket,
            "create_connection",
            guarded_create_connection,
        ),
        mock.patch.object(socket, "getaddrinfo", guarded_getaddrinfo),
        mock.patch.object(
            HTTPAdapter,
            "send",
            new=google_support._route_http_adapter_send,
        ),
        mock.patch.object(
            google_support._gateway,
            "_clock_now",
            new=clock_now,
        ),
        mock.patch.object(
            google_support._gateway,
            "_monotonic_now",
            new=monotonic_now,
        ),
    ):
        yield


def _write_secret(path, payload):
    with path.open("xb") as handle:
        handle.write(payload)
    os.chmod(path, 0o600)


def process_native_handle_count():
    if os.name == "nt":
        count = ctypes.c_ulong()
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetProcessHandleCount.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        )
        kernel32.GetProcessHandleCount.restype = ctypes.c_int
        if not kernel32.GetProcessHandleCount(
            kernel32.GetCurrentProcess(),
            ctypes.byref(count),
        ):
            raise AssertionError("process_handle_count_unavailable")
        return int(count.value)
    descriptor_root = Path("/proc/self/fd")
    if descriptor_root.is_dir():
        return len(tuple(descriptor_root.iterdir()))
    return None


_B21_PROTOCOL_MAX_BYTES = 65_536
_B21_WORKER_TIMEOUT_SECONDS = 30
_B21_SPAWN_WARM_LOCK = threading.Lock()
_B21_SPAWN_WARMED = False


def _b21_spawn_warm_worker(connection):
    connection.close()


def _b21_reap_process(process, *, terminate):
    if terminate and process.is_alive():
        process.terminate()
    process.join(_B21_WORKER_TIMEOUT_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(_B21_WORKER_TIMEOUT_SECONDS)
    if process.is_alive() or process.exitcode is None:
        raise AssertionError("b21_worker_not_reaped")
    process.close()
    return True


def warm_spawn_process_accounting():
    """Establish the spawn helper's process-global handles before a delta."""

    global _B21_SPAWN_WARMED
    with _B21_SPAWN_WARM_LOCK:
        if _B21_SPAWN_WARMED:
            return
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=_b21_spawn_warm_worker,
            args=(child,),
            name="durable-google-login-b21-spawn-warmup",
            daemon=False,
        )
        started = False
        try:
            process.start()
            started = True
            child.close()
            parent.close()
            process.join(_B21_WORKER_TIMEOUT_SECONDS)
            if process.exitcode != 0:
                raise AssertionError("b21_spawn_warmup_failed")
            _B21_SPAWN_WARMED = True
        finally:
            parent.close()
            child.close()
            if started:
                _b21_reap_process(process, terminate=True)


def _b21_send_message(connection, document):
    if type(document) is not dict:
        raise AssertionError("invalid_b21_worker_protocol")
    payload = json.dumps(
        document,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if not 1 <= len(payload) <= _B21_PROTOCOL_MAX_BYTES:
        raise AssertionError("invalid_b21_worker_protocol")
    connection.send_bytes(payload)


def _b21_receive_message(connection, *, timeout=_B21_WORKER_TIMEOUT_SECONDS):
    if not connection.poll(timeout):
        raise AssertionError("b21_worker_protocol_timeout")
    payload = connection.recv_bytes(_B21_PROTOCOL_MAX_BYTES)
    try:
        document = json.loads(payload.decode("ascii", "strict"))
    except (UnicodeError, ValueError):
        raise AssertionError("invalid_b21_worker_protocol") from None
    if type(document) is not dict:
        raise AssertionError("invalid_b21_worker_protocol")
    return document


class FreshBrowserLoginWorker:
    """One spawn-only worker carrying JSON over one explicitly owned pipe."""

    __slots__ = ("_connection", "_process", "_terminal")

    def __init__(self, request):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(
            target=_fresh_browser_login_worker_main,
            args=(child,),
            name="durable-google-login-b21-worker",
            daemon=False,
        )
        self._connection = parent
        self._process = process
        self._terminal = False
        try:
            process.start()
        except BaseException:
            parent.close()
            child.close()
            raise
        child.close()
        try:
            _b21_send_message(parent, request)
        except BaseException:
            self.kill_and_reap()
            raise

    @property
    def pid(self):
        return self._process.pid

    def receive(self):
        if self._terminal:
            raise AssertionError("b21_worker_already_terminal")
        return _b21_receive_message(self._connection)

    def expect_phase(self, name):
        message = self.receive()
        if (
            set(message) != {"kind", "name"}
            or message["kind"] != "phase"
            or message["name"] != name
        ):
            raise AssertionError("unexpected_b21_worker_phase")

    def continue_from_phase(self):
        if self._terminal:
            raise AssertionError("b21_worker_already_terminal")
        _b21_send_message(
            self._connection,
            {"kind": "command", "name": "continue"},
        )

    def finish_and_reap(self):
        if self._terminal:
            raise AssertionError("b21_worker_already_terminal")
        try:
            result = self.receive()
            if result.get("kind") != "complete":
                raise AssertionError("b21_worker_failed")
            self._process.join(_B21_WORKER_TIMEOUT_SECONDS)
            if self._process.is_alive() or self._process.exitcode != 0:
                raise AssertionError("b21_worker_did_not_exit_cleanly")
            return result
        finally:
            self._finish_handles(force=True)

    def kill_and_reap(self):
        if self._terminal:
            return
        self._finish_handles(force=True)

    def assert_terminal_resources(self):
        if (
            not self._terminal
            or not self._connection.closed
            or not getattr(self._process, "_closed", False)
            or getattr(self._process, "_popen", None) is not None
        ):
            raise AssertionError("b21_worker_resources_not_terminal")
        try:
            self._process.sentinel
        except ValueError:
            return True
        raise AssertionError("b21_worker_process_handle_not_terminal")

    def _finish_handles(self, *, force):
        if self._terminal:
            return
        self._terminal = True
        try:
            self._connection.close()
        finally:
            _b21_reap_process(self._process, terminate=force)


def _b21_response_values(response, name):
    lowered = name.casefold()
    return tuple(
        value
        for candidate, value in response.headers
        if candidate.casefold() == lowered
    )


def _b21_response_cookies(response):
    result = {}
    for header in _b21_response_values(response, "Set-Cookie"):
        pair = header.split(";", 1)[0]
        name, value = pair.split("=", 1)
        result[name] = value
    return result


def _b21_epoch_fingerprint(epoch):
    return hashlib.sha256(epoch.proof).hexdigest()


def _b21_issuance_fingerprint(record):
    return hashlib.sha256(record._proof).hexdigest()


def _fresh_browser_login_worker_main(connection):
    runtime = None
    harnesses = []
    egress_stack = ExitStack()
    original_boundary = None
    original_open = None
    failure = False
    try:
        request = _b21_receive_message(connection)
        allowed = {
            "role",
            "configuration_path",
            "subject",
            "pause_at",
            "provider_url",
            "transaction_cookie",
            "expected_seed",
        }
        if (
            set(request) != allowed
            or request["role"] not in {"start", "callback"}
            or type(request["configuration_path"]) is not str
            or type(request["subject"]) is not str
            or request["pause_at"] not in {
                None,
                "prepare.after_insert",
                "prepare.after_commit",
            }
            or type(request["expected_seed"]) is not str
            or os.environ.get("PYTHONHASHSEED") != request["expected_seed"]
            or os.environ.get("PYTHONDONTWRITEBYTECODE") != "1"
            or (
                request["role"] == "start"
                and (
                    request["provider_url"] is not None
                    or request["transaction_cookie"] is not None
                )
            )
            or (
                request["role"] == "callback"
                and (
                    type(request["provider_url"]) is not str
                    or type(request["transaction_cookie"]) is not str
                    or request["pause_at"] is not None
                )
            )
        ):
            raise AssertionError("invalid_b21_worker_request")
        egress_stack.enter_context(
            loopback_and_in_memory_provider_only()
        )

        from wahojobs import durable_google_login_runtime as runtime_module
        from wahojobs import (
            google_oidc_authorization_transaction_repository as repository,
        )

        clock = ManualClock(NOW)
        issuance_fingerprints = []
        original_open = (
            runtime_module._RuntimeDatabaseConnections
            ._finish_connection_open
        )

        def observed_open(manager, **arguments):
            lease = original_open(manager, **arguments)
            record = object.__getattribute__(lease, "_record")
            issuance_fingerprints.append(
                _b21_issuance_fingerprint(record)
            )
            return lease

        runtime_module._RuntimeDatabaseConnections._finish_connection_open = (
            observed_open
        )

        def gateway_factory(configuration, client_secret):
            harness = make_real_gateway(
                clock=clock,
                client_id=configuration.google_client_id,
                client_secret=client_secret,
                redirect_uri=configuration.google_redirect_uri,
                subject=request["subject"],
            )
            harnesses.append(harness)
            return harness.gateway

        pause_at = request["pause_at"]
        if pause_at is not None:
            original_boundary = repository._failure_boundary

            def boundary(name):
                if name != pause_at:
                    return
                _b21_send_message(
                    connection,
                    {"kind": "phase", "name": name},
                )
                command = _b21_receive_message(connection)
                if command != {"kind": "command", "name": "continue"}:
                    raise AssertionError("invalid_b21_worker_command")

            repository._failure_boundary = boundary

        runtime = runtime_module.build_durable_google_login_runtime(
            request["configuration_path"],
            _clock=clock,
            _gateway_factory=gateway_factory,
        )
        manager = object.__getattribute__(runtime, "_connections")
        epoch = object.__getattribute__(manager, "_process_epoch")
        epoch_fingerprint = _b21_epoch_fingerprint(epoch)
        browser = runtime.browser_integration
        authority = urlsplit(runtime.configuration.public_origin).netloc

        with loopback_and_in_memory_provider_only():
            if request["role"] == "start":
                login = browser.handle(
                    "GET",
                    "/login",
                    (("Host", authority),),
                )
                login_cookies = _b21_response_cookies(login)
                csrf = login_cookies["__Host-wahojobs_login_csrf"]
                login.acknowledge_delivery()
                body = form_body(csrf=csrf)
                start = browser.handle(
                    "POST",
                    "/auth/google/start",
                    (
                        ("Host", authority),
                        ("Origin", runtime.configuration.public_origin),
                        ("Sec-Fetch-Site", "same-origin"),
                        (
                            "Content-Type",
                            "application/x-www-form-urlencoded",
                        ),
                        ("Content-Length", str(len(body))),
                        (
                            "Cookie",
                            cookie_header(
                                {
                                    "__Host-wahojobs_login_csrf": csrf,
                                }
                            ),
                        ),
                    ),
                    io.BytesIO(body),
                )
                locations = _b21_response_values(start, "Location")
                start_cookies = _b21_response_cookies(start)
                if start.status != 303 or len(locations) != 1:
                    raise AssertionError("b21_start_failed")
                handoff = {
                    "provider_url": locations[0],
                    "transaction_cookie": start_cookies[
                        "__Host-wahojobs_google_tx"
                    ],
                }
                start.acknowledge_delivery()
                provider_calls = harnesses[0].transport.call_count
                token_requests = harnesses[0].transport.token_request_count
                jwks_requests = harnesses[0].transport.jwks_request_count
                result_details = handoff
            else:
                provider_url = request["provider_url"]
                transaction_cookie = request["transaction_cookie"]
                if (
                    type(provider_url) is not str
                    or type(transaction_cookie) is not str
                ):
                    raise AssertionError("invalid_b21_callback_handoff")
                callback_url = harnesses[0].transport.callback_for(
                    _PreparedAuthorizationUrl(provider_url),
                    code="b21-restart-code",
                    base_uri=runtime.configuration.public_origin
                    + "/auth/google/callback",
                )
                callback_parts = urlsplit(callback_url)
                callback = browser.handle(
                    "GET",
                    callback_parts.path + "?" + callback_parts.query,
                    (
                        ("Host", authority),
                        (
                            "Cookie",
                            cookie_header(
                                {
                                    "__Host-wahojobs_google_tx": (
                                        transaction_cookie
                                    )
                                }
                            ),
                        ),
                    ),
                )
                cookie_headers = _b21_response_values(
                    callback,
                    "Set-Cookie",
                )
                locations = _b21_response_values(callback, "Location")
                result_details = {
                    "status": callback.status,
                    "profile_location": locations
                    == ("/account/profile",),
                    "session_cookie_count": sum(
                        value.startswith("wahojobs_session=")
                        for value in cookie_headers
                    ),
                    "csrf_cookie_count": sum(
                        value.startswith(
                            "__Host-wahojobs_session_csrf="
                        )
                        for value in cookie_headers
                    ),
                    "transaction_clear_count": sum(
                        value.startswith(
                            "__Host-wahojobs_google_tx=;"
                        )
                        for value in cookie_headers
                    ),
                }
                callback.acknowledge_delivery()
                provider_calls = harnesses[0].transport.call_count
                token_requests = harnesses[0].transport.token_request_count
                jwks_requests = harnesses[0].transport.jwks_request_count

        report = runtime.close()
        runtime = None
        manager_closed = manager.closed
        manager_records = len(
            object.__getattribute__(manager, "_records")
        )
        for harness in reversed(harnesses):
            harness.close()
        harnesses.clear()
        _b21_send_message(
            connection,
            {
                "kind": "complete",
                "pid": os.getpid(),
                "start_method": multiprocessing.get_start_method(),
                "seed": os.environ.get("PYTHONHASHSEED"),
                "dont_write_bytecode": bool(sys.dont_write_bytecode),
                "epoch_fingerprint": epoch_fingerprint,
                "issuance_fingerprints": issuance_fingerprints,
                "cleanup_complete": report.cleanup_complete,
                "manager_closed": manager_closed,
                "manager_records": manager_records,
                "provider_calls": provider_calls,
                "token_requests": token_requests,
                "jwks_requests": jwks_requests,
                **result_details,
            },
        )
    except BaseException:
        failure = True
        try:
            _b21_send_message(connection, {"kind": "failure"})
        except BaseException:
            pass
    finally:
        if original_boundary is not None:
            try:
                repository._failure_boundary = original_boundary
            except BaseException:
                failure = True
        if original_open is not None:
            try:
                runtime_module._RuntimeDatabaseConnections._finish_connection_open = (
                    original_open
                )
            except BaseException:
                failure = True
        if runtime is not None:
            try:
                runtime.close(_preserve_primary=True)
            except BaseException:
                failure = True
        for harness in reversed(harnesses):
            try:
                harness.close()
            except BaseException:
                failure = True
        try:
            egress_stack.close()
        except BaseException:
            failure = True
        try:
            connection.close()
        except BaseException:
            failure = True
    if failure:
        raise SystemExit(2)


__all__ = (
    "BrowserHttpResponse",
    "FreshBrowserLoginWorker",
    "TemporaryBrowserLoginState",
    "cookie_header",
    "cookie_values",
    "form_body",
    "https_request",
    "loopback_and_in_memory_provider_only",
    "process_native_handle_count",
    "provider_callback_for",
    "running_https_browser_app",
    "temporary_browser_login_state",
    "warm_spawn_process_accounting",
)
