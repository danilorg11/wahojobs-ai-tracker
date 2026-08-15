from dataclasses import asdict
from contextlib import redirect_stderr, redirect_stdout
import io
import inspect
import json
import os
from pathlib import Path
import pickle
import select
import shutil
import signal
import socket
import sqlite3
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from types import (
    CodeType,
    FrameType,
    FunctionType,
    GetSetDescriptorType,
    MemberDescriptorType,
    MethodType,
    ModuleType,
    SimpleNamespace,
    TracebackType,
)
import unittest
from unittest import mock
import warnings
from urllib.parse import urlsplit

from tests.durable_google_login_browser_test_support import (
    cookie_header,
    form_body,
    loopback_and_in_memory_provider_only,
    provider_callback_for,
    temporary_browser_login_state,
)
from wahojobs.closed_schema_authority import CURRENT_CLOSED_SCHEMA_MARKERS
from wahojobs.durable_google_login_runtime import (
    DurableGoogleLoginConfigurationError,
    build_durable_google_login_runtime,
    load_durable_google_login_configuration,
)


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_CONFIGURATION_FIELDS = (
    "version",
    "environment",
    "database_path",
    "bind_host",
    "bind_port",
    "public_origin",
    "google_redirect_uri",
    "google_client_id",
    "google_client_secret_file",
    "oidc_lookup_keys",
    "oidc_lookup_active_version",
    "oidc_protection_keys",
    "oidc_protection_active_version",
    "session_idle_ttl_seconds",
    "session_absolute_ttl_seconds",
    "allowed_post_login_paths",
)


def _configuration_document(state):
    return json.loads(
        state.configuration_path.read_text(encoding="utf-8")
    )


def _write_configuration(state, document):
    state.configuration_path.write_text(
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def _replace_regular_file(path, content=None):
    replacement = path.with_name(path.name + ".replacement")
    if content is None:
        shutil.copy2(path, replacement)
    else:
        replacement.write_bytes(content)
    os.replace(replacement, path)


def _sqlite_sidecar_paths(path):
    names = {
        path.name + "-journal",
        path.name + "-wal",
        path.name + "-shm",
    }
    return tuple(
        candidate
        for candidate in path.parent.iterdir()
        if (
            candidate.name in names
            or candidate.name.startswith(path.name + "-mj")
            or candidate.name.startswith(path.name + "-super-journal")
        )
    )


def _retained_canary_hits(root, canary):
    pending = [root]
    seen = set()
    hits = []
    encoded = canary.encode("utf-8")
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if type(value) is str:
            if canary in value:
                hits.append(value)
            continue
        if type(value) in {bytes, bytearray}:
            if encoded in value:
                hits.append("<bytes>")
            continue
        if value is None or type(value) in {bool, int, float, complex}:
            continue
        if isinstance(value, BaseException):
            for name in ("args", "__cause__", "__context__", "__traceback__"):
                try:
                    pending.append(
                        BaseException.__dict__[name].__get__(value)
                    )
                except BaseException:
                    pass
        if type(value) is TracebackType:
            pending.extend((value.tb_next, value.tb_frame))
            continue
        if type(value) is FrameType:
            filename = value.f_code.co_filename.replace("\\", "/")
            if filename.endswith(
                "/wahojobs/durable_google_login_runtime.py"
            ) or filename.endswith(
                "/wahojobs/durable_google_login_browser.py"
            ) or filename.endswith(
                "/scripts/durable_google_login_app.py"
            ):
                pending.extend(tuple(value.f_locals.values()))
            continue
        if type(value) is dict:
            pending.extend(value.keys())
            pending.extend(value.values())
            continue
        if type(value) in {tuple, list, set, frozenset}:
            pending.extend(value)
            continue
        if type(value) is MethodType:
            pending.extend((value.__self__, value.__func__))
            continue
        if type(value) is FunctionType:
            if value.__closure__ is not None:
                for cell in value.__closure__:
                    try:
                        pending.append(cell.cell_contents)
                    except ValueError:
                        pass
            continue
        if type(value) in {CodeType, ModuleType}:
            # Executable and module-namespace metadata contain source literals,
            # not values retained by the live runtime graph under inspection.
            continue
        try:
            attributes = object.__getattribute__(value, "__dict__")
        except BaseException:
            attributes = None
        if type(attributes) is dict:
            pending.extend(attributes.values())
        try:
            mro = type.__getattribute__(type(value), "__mro__")
        except BaseException:
            mro = ()
        for value_type in mro:
            try:
                namespace = type.__getattribute__(value_type, "__dict__")
            except BaseException:
                continue
            for name, descriptor in namespace.items():
                if (
                    type(name) is str
                    and type(descriptor)
                    in {GetSetDescriptorType, MemberDescriptorType}
                    and name not in {"__dict__", "__weakref__"}
                ):
                    try:
                        pending.append(
                            descriptor.__get__(value, type(value))
                        )
                    except BaseException:
                        pass
    return hits


def _assert_canary_absent(test_case, value, canary):
    test_case.assertNotIn(canary, repr(value))
    test_case.assertNotIn(canary, str(value))
    test_case.assertEqual(_retained_canary_hits(value, canary), [])


class DurableGoogleLoginBrowserRuntimeIsolationTests(unittest.TestCase):
    def run_python(self, source, *, cwd=ROOT):
        return subprocess.run(
            [sys.executable, "-B", "-c", source],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    def arm_server_admission(
        self,
        module,
        server,
        *,
        signal_state=None,
    ):
        outcome = module._ServeOutcome()
        if signal_state is None:
            signal_state = module._SignalShutdownState()
        server.publish_handler(type("NoRequestHandler", (), {}))
        server.set_serve_lifecycle(outcome, signal_state)
        self.assertTrue(outcome.begin_starting())
        with server._lifecycle_lock:
            server._serve_active = True
            server._serve_thread_ident = threading.get_ident()
        self.assertTrue(
            outcome.publish_serving_checkpoint(signal_state)
        )
        self.assertTrue(server.claim_serving_readiness())
        return outcome, signal_state

    @staticmethod
    def disarm_server_admission(server, outcome):
        outcome.request_stop()
        outcome.publish_success()
        with server._lifecycle_lock:
            server._serve_active = False
            server._serve_thread_ident = None

    def start_real_tls_server(self, module, *, handshake_timeout=0.2):
        tls_scope = module._EphemeralTlsContext()
        self.assertTrue(tls_scope.prepare_workspace())
        tls_context = tls_scope.build_context()
        signal_state = module._SignalShutdownState()
        server = module._DrainingThreadingHTTPServer(
            ("127.0.0.1", 0),
            module._UnpublishedRequestHandler,
            False,
        )
        outcome = module._ServeOutcome()
        serve_thread = None
        try:
            server.set_shutdown_notification(
                lambda: signal_state.requested
            )
            server.set_tls_context(
                tls_context,
                handshake_timeout=handshake_timeout,
            )
            server.publish_handler(type("NoRequestHandler", (), {}))
            server.server_bind()
            server.server_activate()
            server.set_serve_lifecycle(outcome, signal_state)
            self.assertTrue(outcome.begin_starting())
            serve_thread = threading.Thread(
                target=module._serve_in_thread,
                args=(server, outcome),
                daemon=False,
            )
            serve_thread.start()
            self.assertEqual(
                outcome.wait_for_startup(1, signal_state),
                "serving",
            )
            self.assertTrue(server.claim_serving_readiness())
            return SimpleNamespace(
                module=module,
                outcome=outcome,
                server=server,
                signal_state=signal_state,
                serve_thread=serve_thread,
                tls_scope=tls_scope,
            )
        except BaseException:
            server.begin_shutdown()
            server.close_pending_handshakes()
            server.close_accepted_sockets()
            server.close_listener()
            server.server_close()
            if serve_thread is not None:
                serve_thread.join(2)
            tls_scope.close()
            raise

    def stop_real_tls_server(self, harness):
        server = harness.server
        server.begin_shutdown()
        server.close_listener()
        server.close_pending_handshakes()
        server.close_accepted_sockets()
        server.drain_request_threads(2)
        server.detach_route_integration()
        server.server_close()
        harness.serve_thread.join(2)
        counts = server.resource_counts()
        thread_alive = harness.serve_thread.is_alive()
        tls_closed = harness.tls_scope.close()
        return counts, thread_alive, tls_closed

    def wait_for_server_count(
        self,
        server,
        category,
        expected,
        *,
        timeout=2,
    ):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if server.resource_counts()[category] == expected:
                return True
            time.sleep(0.005)
        return server.resource_counts()[category] == expected

    def test_import_has_no_environment_file_database_network_or_route_side_effect(self):
        source = rf"""
import builtins
import http.client
import io
import os
from pathlib import Path
import socket
import sqlite3
import sys
import urllib.request
sys.path.insert(0, {str(ROOT)!r})
def blocked(*_args, **_kwargs):
    raise RuntimeError("import side effect")
sqlite3.connect = blocked
socket.socket = blocked
socket.create_connection = blocked
socket.getaddrinfo = blocked
http.client.HTTPConnection.connect = blocked
http.client.HTTPSConnection.connect = blocked
urllib.request.urlopen = blocked
builtins.open = blocked
io.open = blocked
Path.open = blocked
os.getenv = blocked
class GuardedEnvironment(dict):
    def _blocked(self, *_args, **_kwargs):
        raise RuntimeError("environment read")
    __contains__ = _blocked
    __getitem__ = _blocked
    __iter__ = _blocked
    __len__ = _blocked
    get = _blocked
    items = _blocked
    keys = _blocked
    values = _blocked
os.environ = GuardedEnvironment()
import wahojobs.durable_google_login_runtime
print(
    "wahojobs.durable_google_login_browser" in sys.modules,
    "wahojobs.google_oidc_gateway" in sys.modules,
    "scripts.local_product_app" in sys.modules,
)
"""
        result = self.run_python(source)
        self.assertEqual(result.stdout.strip(), "False False False")

    def test_strict_configuration_publishes_only_minimal_serving_fields(self):
        with temporary_browser_login_state() as state:
            configuration = load_durable_google_login_configuration(
                state.configuration_path
            )
            rendered = repr(configuration)
            self.assertEqual(
                tuple(configuration.__slots__),
                ("bind_host", "bind_port", "public_origin"),
            )
            self.assertEqual(configuration.bind_host, "127.0.0.1")
            self.assertEqual(configuration.bind_port, 8443)
            self.assertEqual(configuration.public_origin, state.public_origin)
            self.assertEqual(
                set(asdict(configuration)),
                {"bind_host", "bind_port", "public_origin"},
            )
            self.assertNotIn(str(state.database_path), rendered)
            self.assertNotIn(str(state.directory), rendered)
            self.assertFalse(hasattr(configuration, "environment"))
            self.assertFalse(hasattr(configuration, "database_path"))
            self.assertFalse(
                hasattr(configuration, "google_client_secret_file")
            )
            self.assertEqual(
                pickle.loads(pickle.dumps(configuration)),
                configuration,
            )

    def test_complete_explicit_activation_constructs_and_closes_runtime(self):
        with temporary_browser_login_state() as state:
            runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            try:
                self.assertEqual(
                    runtime.configuration.public_origin,
                    state.public_origin,
                )
                self.assertTrue(
                    runtime.browser_integration.matches_route("/login")
                )
                self.assertTrue(
                    runtime.browser_integration.matches_route(
                        "/account/profile"
                    )
                )
            finally:
                runtime.close()
            self.assertIn("closed", repr(runtime))

    def test_ttl_authority_accepts_exact_boundaries_and_ordering(self):
        scenarios = (
            ("minimum_equal", 60, 60),
            ("one_below_absolute", 60, 61),
            ("idle_maximum", 2_592_000, 2_592_000),
            ("independent_maxima", 2_592_000, 7_776_000),
        )
        for name, idle, absolute in scenarios:
            with self.subTest(name=name):
                with temporary_browser_login_state(
                    mutate_configuration=lambda document, idle=idle, absolute=absolute: (
                        document.__setitem__(
                            "session_idle_ttl_seconds",
                            idle,
                        ),
                        document.__setitem__(
                            "session_absolute_ttl_seconds",
                            absolute,
                        ),
                    )
                ) as state:
                    configuration = (
                        load_durable_google_login_configuration(
                            state.configuration_path
                        )
                    )
                    self.assertEqual(configuration.bind_port, 8443)

    def test_invalid_ttls_fail_before_database_secret_gateway_tls_or_server(self):
        from scripts import durable_google_login_app
        import wahojobs.durable_google_login_runtime as runtime_module

        scenarios = (
            ("idle_below", 59, 60),
            ("idle_above", 2_592_001, 2_592_001),
            ("absolute_below", 60, 59),
            ("absolute_above", 60, 7_776_001),
            ("idle_above_absolute", 61, 60),
            ("idle_bool", True, 60),
            ("absolute_bool", 60, False),
            ("idle_string", "60", 60),
            ("absolute_float", 60, 60.0),
        )
        for name, idle, absolute in scenarios:
            with self.subTest(name=name):
                with temporary_browser_login_state(
                    mutate_configuration=lambda document, idle=idle, absolute=absolute: (
                        document.__setitem__(
                            "session_idle_ttl_seconds",
                            idle,
                        ),
                        document.__setitem__(
                            "session_absolute_ttl_seconds",
                            absolute,
                        ),
                    )
                ) as state:
                    events = []
                    original_reference = (
                        runtime_module._validated_file_reference
                    )

                    def observe_reference(value, **options):
                        events.append(dict(options))
                        return original_reference(value, **options)

                    with (
                        mock.patch.object(
                            runtime_module,
                            "_validated_file_reference",
                            side_effect=observe_reference,
                        ),
                        mock.patch.object(
                            runtime_module,
                            "_database_target_from_reference",
                            side_effect=AssertionError(
                                "database_target_forbidden"
                            ),
                        ),
                        mock.patch.object(
                            runtime_module,
                            "_read_mutable_file",
                            side_effect=AssertionError(
                                "secret_read_forbidden"
                            ),
                        ),
                    ):
                        with self.assertRaises(
                            DurableGoogleLoginConfigurationError
                        ):
                            build_durable_google_login_runtime(
                                state.configuration_path,
                                _gateway_factory=lambda *_args: (
                                    events.append("gateway")
                                ),
                                _browser_integration_factory=lambda **_values: (
                                    events.append("browser")
                                ),
                            )
                    self.assertEqual(events, [{"configuration": True}])

                    launcher_events = []
                    with mock.patch("builtins.print"):
                        result = durable_google_login_app.main(
                            ["--config", str(state.configuration_path)],
                            _runtime_builder=(
                                build_durable_google_login_runtime
                            ),
                            _tls_context_factory=lambda: (
                                launcher_events.append("tls")
                            ),
                            _server_factory=lambda *_args, **_kwargs: (
                                launcher_events.append("server")
                            ),
                        )
                    self.assertEqual(result, 2)
                    self.assertEqual(launcher_events, [])

    def test_ttl_exact_type_contract_rejects_subclasses_and_coercion(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        class IntegerSubclass(int):
            pass

        class Coercible:
            def __int__(self):
                raise AssertionError("integer_coercion_forbidden")

        with temporary_browser_login_state() as state:
            baseline = _configuration_document(state)
            scenarios = (
                (
                    "idle_subclass",
                    "session_idle_ttl_seconds",
                    IntegerSubclass(60),
                ),
                (
                    "absolute_subclass",
                    "session_absolute_ttl_seconds",
                    IntegerSubclass(60),
                ),
                ("idle_coercible", "session_idle_ttl_seconds", Coercible()),
                (
                    "absolute_coercible",
                    "session_absolute_ttl_seconds",
                    Coercible(),
                ),
                ("idle_none", "session_idle_ttl_seconds", None),
                ("absolute_list", "session_absolute_ttl_seconds", [60]),
            )
            for name, field, value in scenarios:
                with self.subTest(name=name):
                    document = dict(baseline)
                    document[field] = value
                    with self.assertRaises(
                        DurableGoogleLoginConfigurationError
                    ):
                        runtime_module._validated_configuration(document)

    def test_all_pure_cross_fields_fail_before_referenced_file_access(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        mutations = (
            lambda document: document.__setitem__(
                "allowed_post_login_paths",
                ["/not-owned"],
            ),
            lambda document: document.__setitem__(
                "google_redirect_uri",
                "https://localhost:8443/not-callback",
            ),
            lambda document: document.__setitem__(
                "oidc_lookup_active_version",
                99,
            ),
            lambda document: document.__setitem__(
                "oidc_lookup_keys",
                [
                    {
                        "version": 2,
                        "file": document["oidc_lookup_keys"][0]["file"]
                        + ".two",
                    },
                    document["oidc_lookup_keys"][0],
                ],
            ),
        )
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                with temporary_browser_login_state(
                    mutate_configuration=mutation
                ) as state:
                    with mock.patch.object(
                        runtime_module,
                        "_database_target_from_reference",
                        side_effect=AssertionError(
                            "file_resolution_forbidden"
                        ),
                    ) as database_target:
                        with self.assertRaises(
                            DurableGoogleLoginConfigurationError
                        ):
                            build_durable_google_login_runtime(
                                state.configuration_path,
                                _clock=state.clock,
                                _gateway_factory=state.gateway_factory,
                            )
                        self.assertEqual(state.gateway_harnesses, [])
                    database_target.assert_not_called()

    def test_configuration_failures_retain_no_parsed_private_canaries(self):
        canary = "PRIVATE_CONFIGURATION_CANARY_91d3"
        with temporary_browser_login_state() as state:
            baseline = _configuration_document(state)
            scenarios = {
                "database_path": lambda document: document.__setitem__(
                    "database_path",
                    str(state.directory / canary / ".." / "database.sqlite"),
                ),
                "secret_path": lambda document: document.__setitem__(
                    "google_client_secret_file",
                    str(state.directory / canary / ".." / "secret.bin"),
                ),
                "client_id": lambda document: (
                    document.__setitem__("google_client_id", canary),
                    document.__setitem__("session_idle_ttl_seconds", 59),
                ),
                "origin": lambda document: document.__setitem__(
                    "public_origin",
                    "https://" + canary + ":8443",
                ),
                "redirect": lambda document: document.__setitem__(
                    "google_redirect_uri",
                    "https://localhost:8443/" + canary,
                ),
                "bind": lambda document: document.__setitem__(
                    "bind_host",
                    canary,
                ),
                "key_metadata": lambda document: document[
                    "oidc_lookup_keys"
                ][0].__setitem__("version", canary),
            }
            for name, mutation in scenarios.items():
                with self.subTest(name=name):
                    document = json.loads(json.dumps(baseline))
                    mutation(document)
                    _write_configuration(state, document)
                    try:
                        load_durable_google_login_configuration(
                            state.configuration_path
                        )
                    except DurableGoogleLoginConfigurationError as error:
                        _assert_canary_absent(self, error, canary)
                        self.assertEqual(
                            str(error),
                            "Durable Google login configuration is unavailable.",
                        )
                    else:
                        self.fail("configuration_failure_required")

    def test_malformed_json_exception_graph_drops_raw_document(self):
        canary = "PRIVATE_MALFORMED_JSON_CANARY_4ab1"
        with temporary_browser_login_state() as state:
            state.configuration_path.write_text(
                '{"private":"' + canary + '",',
                encoding="utf-8",
            )
            try:
                load_durable_google_login_configuration(
                    state.configuration_path
                )
            except DurableGoogleLoginConfigurationError as error:
                _assert_canary_absent(self, error, canary)
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)
            else:
                self.fail("malformed_configuration_must_fail")

    def test_runtime_construction_scrubs_retaining_exception_graphs(self):
        canary = "PRIVATE_RUNTIME_EXCEPTION_CANARY_22fe"

        class RetainingError(RuntimeError):
            __slots__ = ("retained",)

        original = RetainingError(canary)
        original.retained = {"path": canary}
        original.add_note(canary)
        original.__cause__ = ValueError(canary)
        original.__context__ = LookupError(canary)

        def failing_gateway(_configuration, _secret):
            raise original

        with temporary_browser_login_state() as state:
            try:
                build_durable_google_login_runtime(
                    state.configuration_path,
                    _clock=state.clock,
                    _gateway_factory=failing_gateway,
                )
            except DurableGoogleLoginConfigurationError as public:
                _assert_canary_absent(self, public, canary)
            else:
                self.fail("runtime_construction_failure_required")
        _assert_canary_absent(self, original, canary)
        self.assertEqual(original.args, ())
        self.assertIsNone(original.retained)
        self.assertFalse(getattr(original, "__notes__", ()))
        self.assertIsNone(original.__traceback__)
        self.assertIsNone(original.__cause__)
        self.assertIsNone(original.__context__)

    def test_hostile_exception_descriptors_metaclass_and_outputs_fail_closed(self):
        canary = "PRIVATE_HOSTILE_EXCEPTION_CANARY_9d71"

        class HostileMeta(type):
            def __getattribute__(cls, name):
                if name in {"__dict__", "__mro__"}:
                    raise RuntimeError("hostile_metaclass")
                return super().__getattribute__(name)

        class HostileDescriptor:
            def __get__(self, _instance, _owner):
                raise RuntimeError("hostile_descriptor")

        class HostileError(RuntimeError, metaclass=HostileMeta):
            __slots__ = ("retained",)
            hostile = HostileDescriptor()

        original = HostileError(canary)
        original.retained = canary
        original.private = canary
        original.add_note(canary)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with temporary_browser_login_state() as state:
            with (
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                warnings.catch_warnings(record=True) as recorded,
            ):
                warnings.simplefilter("always")
                try:
                    build_durable_google_login_runtime(
                        state.configuration_path,
                        _clock=state.clock,
                        _gateway_factory=lambda *_args: (
                            (_ for _ in ()).throw(original)
                        ),
                    )
                except DurableGoogleLoginConfigurationError as public:
                    _assert_canary_absent(self, public, canary)
                else:
                    self.fail("hostile_exception_must_fail_closed")
        _assert_canary_absent(self, original, canary)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn(
            canary,
            "".join(str(item.message) for item in recorded),
        )

    def test_configuration_sanitizer_failure_still_fails_closed(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        canary = "PRIVATE_SANITIZER_FAILURE_CANARY_0fa3"
        original = RuntimeError(canary)
        original.private = canary
        sanitization_error = RuntimeError(canary)
        sanitization_error.private = canary

        with temporary_browser_login_state() as state:
            with mock.patch.object(
                runtime_module,
                "_clear_exception_graph",
                side_effect=sanitization_error,
            ):
                try:
                    build_durable_google_login_runtime(
                        state.configuration_path,
                        _clock=state.clock,
                        _gateway_factory=lambda *_args: (_ for _ in ()).throw(
                            original
                        ),
                    )
                except DurableGoogleLoginConfigurationError as public:
                    _assert_canary_absent(self, public, canary)
                else:
                    self.fail("sanitizer_failure_must_fail_closed")
        _assert_canary_absent(self, original, canary)
        _assert_canary_absent(self, sanitization_error, canary)

    def test_configuration_control_flow_identity_has_no_sensitive_frames(self):
        canary = "PRIVATE_CONTROL_FLOW_CANARY_118c"
        for exception_type in (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(exception_type=exception_type.__name__):
                injected = exception_type(canary)
                injected.private = canary
                injected.add_note(canary)
                with temporary_browser_login_state() as state:
                    try:
                        build_durable_google_login_runtime(
                            state.configuration_path,
                            _clock=state.clock,
                            _gateway_factory=lambda *_args, injected=injected: (
                                (_ for _ in ()).throw(injected)
                            ),
                        )
                    except exception_type as propagated:
                        self.assertIs(propagated, injected)
                        _assert_canary_absent(self, propagated, canary)
                        self.assertIsNone(propagated.__cause__)
                        self.assertIsNone(propagated.__context__)
                    else:
                        self.fail("control_flow_must_propagate")

    def test_every_required_configuration_field_is_mandatory(self):
        self.assertEqual(len(REQUIRED_CONFIGURATION_FIELDS), 16)
        self.assertEqual(
            len(set(REQUIRED_CONFIGURATION_FIELDS)),
            len(REQUIRED_CONFIGURATION_FIELDS),
        )
        for field in REQUIRED_CONFIGURATION_FIELDS:
            with self.subTest(field=field):
                with temporary_browser_login_state(
                    mutate_configuration=(
                        lambda value, field=field: value.pop(field)
                    )
                ) as state:
                    with self.assertRaises(
                        DurableGoogleLoginConfigurationError
                    ):
                        load_durable_google_login_configuration(
                            state.configuration_path
                        )

    def test_unknown_partial_duplicate_production_and_boolean_version_fail(self):
        mutations = {
            "unknown": lambda value: value.__setitem__("surprise", True),
            "partial": lambda value: value.__setitem__(
                "google_client_secret_file",
                "",
            ),
            "production": lambda value: value.__setitem__(
                "environment",
                "production",
            ),
            "boolean_version": lambda value: value.__setitem__(
                "version",
                True,
            ),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                with temporary_browser_login_state(
                    mutate_configuration=mutation
                ) as state:
                    with self.assertRaises(
                        DurableGoogleLoginConfigurationError
                    ):
                        load_durable_google_login_configuration(
                            state.configuration_path
                        )

        with temporary_browser_login_state() as state:
            document = json.loads(
                state.configuration_path.read_text(encoding="utf-8")
            )
            serialized = json.dumps(document, separators=(",", ":"))
            duplicated = serialized[:-1] + ',"version":1}'
            state.configuration_path.write_text(
                duplicated,
                encoding="utf-8",
            )
            with self.assertRaises(DurableGoogleLoginConfigurationError):
                load_durable_google_login_configuration(
                    state.configuration_path
                )

    def test_unmigrated_database_fails_before_gateway_or_socket_construction(self):
        with temporary_browser_login_state(
            install_migrations=False
        ) as state:
            gateway_called = False

            def forbidden_gateway(*_args, **_kwargs):
                nonlocal gateway_called
                gateway_called = True
                raise AssertionError("gateway_must_not_be_constructed")

            with mock.patch(
                "socket.socket",
                side_effect=AssertionError("socket_must_not_be_constructed"),
            ):
                with self.assertRaises(
                    DurableGoogleLoginConfigurationError
                ):
                    build_durable_google_login_runtime(
                        state.configuration_path,
                        _gateway_factory=forbidden_gateway,
                    )
            self.assertFalse(gateway_called)

    def test_configuration_never_falls_back_to_default_database(self):
        with temporary_browser_login_state() as state:
            document = json.loads(
                state.configuration_path.read_text(encoding="utf-8")
            )
            document.pop("database_path")
            state.configuration_path.write_text(
                json.dumps(document),
                encoding="utf-8",
            )
            with mock.patch(
                "wahojobs.durable_google_login_runtime.sqlite3.connect",
                side_effect=AssertionError("database_fallback_forbidden"),
            ) as connect:
                with self.assertRaises(
                    DurableGoogleLoginConfigurationError
                ):
                    build_durable_google_login_runtime(
                        state.configuration_path
                    )
            connect.assert_not_called()

    def test_database_target_path_identity_and_sidecar_policy(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        with temporary_browser_login_state() as state:
            document = _configuration_document(state)
            document["database_path"] = str(
                state.directory
                / "unused"
                / ".."
                / state.database_path.name
            )
            _write_configuration(state, document)
            with self.assertRaises(DurableGoogleLoginConfigurationError):
                load_durable_google_login_configuration(
                    state.configuration_path
                )

        with temporary_browser_login_state() as state:
            document = _configuration_document(state)
            document["database_path"] = state.database_path.name
            _write_configuration(state, document)
            with self.assertRaises(DurableGoogleLoginConfigurationError):
                load_durable_google_login_configuration(
                    state.configuration_path
                )

        with temporary_browser_login_state() as state:
            document = _configuration_document(state)
            document["database_path"] = str(ROOT / "README.md")
            _write_configuration(state, document)
            with self.assertRaises(DurableGoogleLoginConfigurationError):
                load_durable_google_login_configuration(
                    state.configuration_path
                )

        with temporary_browser_login_state() as state:
            alias = state.directory / "database-hardlink.sqlite"
            os.link(state.database_path, alias)
            with self.assertRaises(DurableGoogleLoginConfigurationError):
                load_durable_google_login_configuration(
                    state.configuration_path
                )

        for suffix in ("-journal", "-wal", "-shm", "-mj-test"):
            with self.subTest(sidecar=suffix):
                with temporary_browser_login_state() as state:
                    sidecar = Path(str(state.database_path) + suffix)
                    sidecar.write_bytes(b"active-sidecar")
                    with self.assertRaises(
                        DurableGoogleLoginConfigurationError
                    ):
                        load_durable_google_login_configuration(
                            state.configuration_path
                        )

        if os.name == "nt":
            with temporary_browser_login_state() as state:
                sidecar = state.database_path.with_name(
                    state.database_path.name.upper() + "-WAL"
                )
                sidecar.write_bytes(b"case-variant-active-sidecar")
                with self.assertRaises(
                    DurableGoogleLoginConfigurationError
                ):
                    load_durable_google_login_configuration(
                        state.configuration_path
                    )

    @unittest.skipUnless(
        os.name == "nt",
        "NTFS alternate data streams are Windows-specific.",
    )
    def test_windows_alternate_data_stream_paths_are_rejected_early(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        for role in ("database", "secret", "configuration"):
            with self.subTest(role=role):
                with temporary_browser_login_state() as state:
                    host = state.directory / f"{role}-ads-host.bin"
                    host.write_bytes(b"ordinary-host")
                    stream = Path(str(host) + f":{role}.bin")
                    if role == "database":
                        stream.write_bytes(state.database_path.read_bytes())
                    elif role == "secret":
                        stream.write_bytes(
                            (
                                state.directory
                                / "google-client-secret.bin"
                            ).read_bytes()
                        )
                    else:
                        stream.write_bytes(
                            state.configuration_path.read_bytes()
                        )

                    if role == "configuration":
                        with self.assertRaises(
                            DurableGoogleLoginConfigurationError
                        ):
                            load_durable_google_login_configuration(stream)
                        continue

                    document = _configuration_document(state)
                    document[
                        (
                            "database_path"
                            if role == "database"
                            else "google_client_secret_file"
                        )
                    ] = str(stream)
                    _write_configuration(state, document)
                    with mock.patch.object(
                        runtime_module,
                        "_resolve_configuration_files",
                        side_effect=AssertionError(
                            "ads_file_resolution_forbidden"
                        ),
                    ) as resolve:
                        with self.assertRaises(
                            DurableGoogleLoginConfigurationError
                        ):
                            load_durable_google_login_configuration(
                                state.configuration_path
                            )
                    resolve.assert_not_called()

    def test_database_target_reparse_branch_is_rejected(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        with temporary_browser_login_state() as state:
            original_lstat = runtime_module.os.lstat

            class ReparseMetadata:
                def __init__(self, metadata):
                    self._metadata = metadata
                    self.st_file_attributes = (
                        getattr(metadata, "st_file_attributes", 0)
                        | getattr(
                            stat,
                            "FILE_ATTRIBUTE_REPARSE_POINT",
                            0x400,
                        )
                    )

                def __getattr__(self, name):
                    return getattr(self._metadata, name)

            def marked_lstat(candidate):
                metadata = original_lstat(candidate)
                if Path(candidate) == state.database_path:
                    return ReparseMetadata(metadata)
                return metadata

            with mock.patch.object(
                runtime_module.os,
                "lstat",
                side_effect=marked_lstat,
            ):
                with self.assertRaises(
                    DurableGoogleLoginConfigurationError
                ):
                    load_durable_google_login_configuration(
                        state.configuration_path
                    )

    def test_database_target_rejects_active_rollback_and_wal_writers(self):
        for mode in ("reserved_only", "rollback", "wal"):
            with self.subTest(mode=mode):
                with temporary_browser_login_state() as state:
                    writer = sqlite3.connect(
                        state.database_path,
                        timeout=2.0,
                    )
                    try:
                        if mode == "wal":
                            self.assertEqual(
                                writer.execute(
                                    "PRAGMA journal_mode=WAL"
                                ).fetchone()[0],
                                "wal",
                            )
                        writer.execute("BEGIN IMMEDIATE")
                        if mode != "reserved_only":
                            writer.execute(
                                "CREATE TABLE active_writer_probe(value TEXT)"
                            )
                            self.assertTrue(
                                _sqlite_sidecar_paths(state.database_path)
                            )
                        else:
                            self.assertEqual(
                                _sqlite_sidecar_paths(state.database_path),
                                (),
                            )
                        with self.assertRaises(
                            DurableGoogleLoginConfigurationError
                        ):
                            build_durable_google_login_runtime(
                                state.configuration_path,
                                _clock=state.clock,
                                _gateway_factory=state.gateway_factory,
                            )
                        self.assertEqual(state.gateway_harnesses, [])
                    finally:
                        writer.rollback()
                        writer.close()

    def test_database_schema_contract_is_exact_and_read_only(self):
        scenarios = {
            "missing_marker": (
                "DELETE FROM wahojobs_schema_migrations "
                "WHERE version='006_google_oidc_authorization_transactions'"
            ),
            "forged_marker": (
                "UPDATE wahojobs_schema_migrations "
                "SET version='999_forged' "
                "WHERE version='006_google_oidc_authorization_transactions'"
            ),
            "later_marker": (
                "INSERT INTO wahojobs_schema_migrations(version) "
                "VALUES ('007_unapproved')"
            ),
            "partial_schema": (
                "DROP TRIGGER "
                "trg_google_oidc_authorization_transactions_delete_guard"
            ),
            "extra_schema": "CREATE TABLE unrelated_extra(value TEXT)",
        }
        for name, sql in scenarios.items():
            with self.subTest(name=name):
                with temporary_browser_login_state() as state:
                    connection = sqlite3.connect(state.database_path)
                    try:
                        connection.execute(sql)
                        connection.commit()
                    finally:
                        connection.close()
                    before = state.database_path.read_bytes()
                    gateway_called = False

                    def forbidden_gateway(*_args):
                        nonlocal gateway_called
                        gateway_called = True
                        raise AssertionError("gateway_forbidden")

                    with self.assertRaises(
                        DurableGoogleLoginConfigurationError
                    ):
                        build_durable_google_login_runtime(
                            state.configuration_path,
                            _clock=state.clock,
                            _gateway_factory=forbidden_gateway,
                        )
                    self.assertFalse(gateway_called)
                    self.assertEqual(
                        state.database_path.read_bytes(),
                        before,
                    )
                    self.assertEqual(
                        _sqlite_sidecar_paths(state.database_path),
                        (),
                    )

        with self.subTest(name="literal_whitespace_drift"):
            with temporary_browser_login_state() as state:
                connection = sqlite3.connect(state.database_path)
                try:
                    trigger_name = "trg_product_principals_insert_guard"
                    stored_sql = connection.execute(
                        "SELECT sql FROM sqlite_schema "
                        "WHERE type='trigger' AND name=?",
                        (trigger_name,),
                    ).fetchone()[0]
                    self.assertIn("*[^ -~]*", stored_sql)
                    drifted_sql = stored_sql.replace(
                        "*[^ -~]*",
                        "*[^\t-~]*",
                        1,
                    )
                    connection.execute("PRAGMA writable_schema = ON")
                    connection.execute(
                        "UPDATE sqlite_schema SET sql=? "
                        "WHERE type='trigger' AND name=?",
                        (drifted_sql, trigger_name),
                    )
                    connection.execute("PRAGMA writable_schema = OFF")
                    connection.commit()
                finally:
                    connection.close()
                before = state.database_path.read_bytes()
                with self.assertRaises(
                    DurableGoogleLoginConfigurationError
                ):
                    build_durable_google_login_runtime(
                        state.configuration_path,
                        _clock=state.clock,
                        _gateway_factory=state.gateway_factory,
                    )
                self.assertEqual(state.gateway_harnesses, [])
                self.assertEqual(state.database_path.read_bytes(), before)
                self.assertEqual(
                    _sqlite_sidecar_paths(state.database_path),
                    (),
                )

    def test_database_replacement_fails_at_each_startup_stage(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        stages = ("after_validation", "during_attestation", "after_attestation")
        for stage in stages:
            with self.subTest(stage=stage):
                with temporary_browser_login_state() as state:
                    gateway_called = False

                    def forbidden_gateway(*_args):
                        nonlocal gateway_called
                        gateway_called = True
                        raise AssertionError("gateway_forbidden")

                    if stage == "after_validation":
                        original = (
                            runtime_module._database_target_from_reference
                        )

                        def inject(reference):
                            target = original(reference)
                            _replace_regular_file(state.database_path)
                            return target

                        patcher = mock.patch.object(
                            runtime_module,
                            "_database_target_from_reference",
                            side_effect=inject,
                        )
                    elif stage == "during_attestation":
                        original = runtime_module._open_database_connection

                        def inject(target, **options):
                            if options.get("verify_schema") is False:
                                _replace_regular_file(state.database_path)
                            return original(target, **options)

                        patcher = mock.patch.object(
                            runtime_module,
                            "_open_database_connection",
                            side_effect=inject,
                        )
                    else:
                        original = runtime_module._attest_existing_database

                        def inject(target, *, cleanup_coordinator):
                            original(
                                target,
                                cleanup_coordinator=cleanup_coordinator,
                            )
                            _replace_regular_file(state.database_path)

                        patcher = mock.patch.object(
                            runtime_module,
                            "_attest_existing_database",
                            side_effect=inject,
                        )
                    with patcher:
                        with self.assertRaises(
                            DurableGoogleLoginConfigurationError
                        ):
                            build_durable_google_login_runtime(
                                state.configuration_path,
                                _clock=state.clock,
                                _gateway_factory=forbidden_gateway,
                            )
                    self.assertFalse(gateway_called)
                    self.assertEqual(
                        _sqlite_sidecar_paths(state.database_path),
                        (),
                    )

    def test_final_database_attestation_rejects_late_startup_drift(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        for mode in ("sidecar", "reserved_writer", "same_inode_schema"):
            with self.subTest(mode=mode):
                with temporary_browser_login_state() as state:
                    original = runtime_module._attest_existing_database
                    calls = 0
                    writer = None
                    injected_sidecar = None

                    def inject(
                        target,
                        *,
                        cleanup_coordinator,
                        lifetime_ownership=None,
                    ):
                        nonlocal calls, writer, injected_sidecar
                        calls += 1
                        if calls == 2:
                            if mode == "sidecar":
                                injected_sidecar = Path(
                                    str(state.database_path) + "-wal"
                                )
                                injected_sidecar.write_bytes(b"late-sidecar")
                            elif mode == "reserved_writer":
                                writer = sqlite3.connect(
                                    state.database_path,
                                    timeout=2.0,
                                )
                                writer.execute("BEGIN IMMEDIATE")
                                self.assertEqual(
                                    _sqlite_sidecar_paths(
                                        state.database_path
                                    ),
                                    (),
                                )
                            else:
                                connection = sqlite3.connect(
                                    state.database_path
                                )
                                try:
                                    connection.execute(
                                        "CREATE TABLE late_extra(value TEXT)"
                                    )
                                    connection.commit()
                                finally:
                                    connection.close()
                        return original(
                            target,
                            cleanup_coordinator=cleanup_coordinator,
                            lifetime_ownership=lifetime_ownership,
                        )

                    try:
                        with mock.patch.object(
                            runtime_module,
                            "_attest_existing_database",
                            side_effect=inject,
                        ):
                            with self.assertRaises(
                                DurableGoogleLoginConfigurationError
                            ):
                                build_durable_google_login_runtime(
                                    state.configuration_path,
                                    _clock=state.clock,
                                    _gateway_factory=state.gateway_factory,
                                )
                        self.assertEqual(calls, 2)
                    finally:
                        if writer is not None:
                            writer.rollback()
                            writer.close()
                        if (
                            injected_sidecar is not None
                            and injected_sidecar.exists()
                        ):
                            injected_sidecar.unlink()
                    self.assertEqual(
                        _sqlite_sidecar_paths(state.database_path),
                        (),
                    )

    def test_runtime_connections_reject_replacement_before_and_between_opens(self):
        with temporary_browser_login_state() as state:
            runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            try:
                replacement_bytes = state.database_path.read_bytes()
                _replace_regular_file(state.database_path)
                with self.assertRaises(
                    DurableGoogleLoginConfigurationError
                ):
                    runtime.open_writable_connection()
                with self.assertRaises(
                    DurableGoogleLoginConfigurationError
                ):
                    with runtime.read_only_connection_provider():
                        self.fail("replacement_connection_forbidden")
                self.assertEqual(
                    state.database_path.read_bytes(),
                    replacement_bytes,
                )
                self.assertEqual(
                    _sqlite_sidecar_paths(state.database_path),
                    (),
                )
            finally:
                runtime.close()

        with temporary_browser_login_state() as state:
            runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            try:
                first = runtime.open_writable_connection()
                try:
                    self.assertEqual(
                        first.execute(
                            "SELECT COUNT(*) "
                            "FROM wahojobs_schema_migrations"
                        ).fetchone()[0],
                        len(CURRENT_CLOSED_SCHEMA_MARKERS),
                    )
                finally:
                    first.close()
                _replace_regular_file(state.database_path)
                with self.assertRaises(
                    DurableGoogleLoginConfigurationError
                ):
                    runtime.open_writable_connection()
            finally:
                runtime.close()

    @unittest.skipUnless(
        os.name == "nt",
        "Windows raw descriptor pinning is platform-specific.",
    )
    def test_windows_connection_open_pins_physical_database_target(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        with temporary_browser_login_state() as state:
            original_connect = runtime_module.sqlite3.connect
            attempts = 0
            blocked = 0

            def attempted_swap(*args, **kwargs):
                nonlocal attempts, blocked
                attempts += 1
                try:
                    _replace_regular_file(state.database_path)
                except PermissionError:
                    blocked += 1
                    replacement = state.database_path.with_name(
                        state.database_path.name + ".replacement"
                    )
                    if replacement.exists():
                        replacement.unlink()
                else:
                    raise AssertionError("database_pin_did_not_block_swap")
                return original_connect(*args, **kwargs)

            with mock.patch.object(
                runtime_module.sqlite3,
                "connect",
                side_effect=attempted_swap,
            ):
                runtime = build_durable_google_login_runtime(
                    state.configuration_path,
                    _clock=state.clock,
                    _gateway_factory=state.gateway_factory,
                )
            try:
                self.assertGreaterEqual(attempts, 2)
                self.assertEqual(blocked, attempts)
            finally:
                runtime.close()

    def test_secret_files_enforce_external_identity_size_and_material_policy(self):
        from wahojobs.durable_google_login_runtime import (
            _absolute_regular_file,
        )

        with self.assertRaises(DurableGoogleLoginConfigurationError):
            _absolute_regular_file(ROOT / "README.md", secret=True)

        for filename, content in (
            ("google-client-secret.bin", b"x" * 15),
            ("lookup-1.key", b"k" * 31),
            ("protection-11.key", b"p" * 33),
        ):
            with self.subTest(filename=filename):
                with temporary_browser_login_state() as state:
                    (state.directory / filename).write_bytes(content)
                    with self.assertRaises(
                        DurableGoogleLoginConfigurationError
                    ):
                        build_durable_google_login_runtime(
                            state.configuration_path,
                            _clock=state.clock,
                            _gateway_factory=state.gateway_factory,
                        )

        with temporary_browser_login_state() as state:
            client_secret = state.directory / "google-client-secret.bin"
            alias = state.directory / "google-client-secret-alias.bin"
            os.link(client_secret, alias)
            with self.assertRaises(DurableGoogleLoginConfigurationError):
                load_durable_google_login_configuration(
                    state.configuration_path
                )

        with temporary_browser_login_state(
            mutate_configuration=lambda document: document[
                "oidc_protection_keys"
            ][0].__setitem__(
                "file",
                document["oidc_lookup_keys"][0]["file"],
            )
        ) as state:
            with self.assertRaises(DurableGoogleLoginConfigurationError):
                load_durable_google_login_configuration(
                    state.configuration_path
                )

        with temporary_browser_login_state() as state:
            lookup = state.directory / "lookup-1.key"
            protection = state.directory / "protection-11.key"
            protection.write_bytes(lookup.read_bytes())
            with self.assertRaises(DurableGoogleLoginConfigurationError):
                build_durable_google_login_runtime(
                    state.configuration_path,
                    _clock=state.clock,
                    _gateway_factory=state.gateway_factory,
                )

        if os.name != "nt":
            with temporary_browser_login_state() as state:
                secret = state.directory / "google-client-secret.bin"
                secret.chmod(0o640)
                with self.assertRaises(
                    DurableGoogleLoginConfigurationError
                ):
                    load_durable_google_login_configuration(
                        state.configuration_path
                    )

    def test_secret_replacements_before_during_and_after_read_fail_closed(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        with temporary_browser_login_state() as state:
            original = runtime_module._load_authority_material

            def replace_before_open(configuration):
                _replace_regular_file(
                    state.directory / "google-client-secret.bin",
                    b"z" * 32,
                )
                return original(configuration)

            with mock.patch.object(
                runtime_module,
                "_load_authority_material",
                side_effect=replace_before_open,
            ):
                with self.assertRaises(
                    DurableGoogleLoginConfigurationError
                ):
                    build_durable_google_login_runtime(
                        state.configuration_path,
                        _clock=state.clock,
                        _gateway_factory=state.gateway_factory,
                    )
            self.assertEqual(state.gateway_harnesses, [])

        for name, replacement in (
            ("same_length", b"z" * 32),
            ("identical_content", None),
            ("disappearance", b""),
        ):
            with self.subTest(name=name):
                with temporary_browser_login_state() as state:
                    secret_path = (
                        state.directory / "google-client-secret.bin"
                    )
                    original_content = secret_path.read_bytes()
                    identity = (
                        secret_path.stat().st_dev,
                        secret_path.stat().st_ino,
                    )
                    original_fdopen = runtime_module.os.fdopen
                    acted = False

                    class ClosingMutation:
                        def __init__(self, handle):
                            self._handle = handle

                        def __getattr__(self, attribute):
                            return getattr(self._handle, attribute)

                        def close(self):
                            nonlocal acted
                            self._handle.close()
                            if acted:
                                return
                            acted = True
                            if name == "disappearance":
                                secret_path.unlink()
                            else:
                                _replace_regular_file(
                                    secret_path,
                                    (
                                        original_content
                                        if replacement is None
                                        else replacement
                                    ),
                                )

                    def wrapped_fdopen(descriptor, *args, **kwargs):
                        handle = original_fdopen(
                            descriptor,
                            *args,
                            **kwargs,
                        )
                        metadata = os.fstat(handle.fileno())
                        if (metadata.st_dev, metadata.st_ino) == identity:
                            return ClosingMutation(handle)
                        return handle

                    with mock.patch.object(
                        runtime_module.os,
                        "fdopen",
                        side_effect=wrapped_fdopen,
                    ):
                        with self.assertRaises(
                            DurableGoogleLoginConfigurationError
                        ):
                            build_durable_google_login_runtime(
                                state.configuration_path,
                                _clock=state.clock,
                                _gateway_factory=state.gateway_factory,
                            )
                    self.assertTrue(acted)
                    self.assertEqual(state.gateway_harnesses, [])

        with temporary_browser_login_state() as state:
            secret_path = state.directory / "google-client-secret.bin"
            identity = (
                secret_path.stat().st_dev,
                secret_path.stat().st_ino,
            )
            original_content = secret_path.read_bytes()
            original_fdopen = runtime_module.os.fdopen
            attempted = False
            captured_during = []

            class ReadingMutation:
                def __init__(self, handle):
                    self._handle = handle

                def __getattr__(self, attribute):
                    return getattr(self._handle, attribute)

                def readinto(self, buffer):
                    nonlocal attempted
                    count = self._handle.readinto(buffer)
                    if not attempted:
                        attempted = True
                        captured_during.append(buffer.obj)
                        _replace_regular_file(
                            secret_path,
                            original_content,
                        )
                    return count

            def wrapped_fdopen(descriptor, *args, **kwargs):
                handle = original_fdopen(descriptor, *args, **kwargs)
                metadata = os.fstat(handle.fileno())
                if (metadata.st_dev, metadata.st_ino) == identity:
                    return ReadingMutation(handle)
                return handle

            with mock.patch.object(
                runtime_module.os,
                "fdopen",
                side_effect=wrapped_fdopen,
            ):
                with self.assertRaises(
                    DurableGoogleLoginConfigurationError
                ):
                    build_durable_google_login_runtime(
                        state.configuration_path,
                        _clock=state.clock,
                        _gateway_factory=state.gateway_factory,
                    )
            self.assertTrue(attempted)
            self.assertEqual(len(captured_during), 1)
            self.assertEqual(len(captured_during[0]), 0)
            self.assertEqual(state.gateway_harnesses, [])

    def test_partial_secret_loading_clears_every_prior_mutable_buffer(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        scenarios = (
            ("oidc_lookup_keys", 2, "lookup-2.key", 3, b"l" * 32),
            (
                "oidc_protection_keys",
                12,
                "protection-12.key",
                4,
                b"p" * 32,
            ),
        )
        for ring_name, version, filename, failure_call, material in scenarios:
            with self.subTest(ring=ring_name):
                with temporary_browser_login_state() as state:
                    target = state.directory / filename
                    target.write_bytes(material)
                    if os.name != "nt":
                        target.chmod(0o600)
                    document = _configuration_document(state)
                    document[ring_name].append(
                        {"version": version, "file": str(target)}
                    )
                    _write_configuration(state, document)

                    original = runtime_module._read_mutable_file
                    captured = []
                    calls = 0

                    def injected_read(reference, *, minimum, maximum):
                        nonlocal calls
                        calls += 1
                        if calls == failure_call:
                            _replace_regular_file(target, b"q" * 32)
                        buffer = original(
                            reference,
                            minimum=minimum,
                            maximum=maximum,
                        )
                        captured.append(buffer)
                        return buffer

                    with mock.patch.object(
                        runtime_module,
                        "_read_mutable_file",
                        side_effect=injected_read,
                    ):
                        with self.assertRaises(
                            DurableGoogleLoginConfigurationError
                        ):
                            build_durable_google_login_runtime(
                                state.configuration_path,
                                _clock=state.clock,
                                _gateway_factory=state.gateway_factory,
                            )
                    self.assertEqual(calls, failure_call)
                    self.assertEqual(len(captured), failure_call - 1)
                    self.assertEqual(
                        [len(buffer) for buffer in captured],
                        [0] * (failure_call - 1),
                    )
                    self.assertEqual(state.gateway_harnesses, [])

    def test_secret_reparse_branch_and_final_pre_activation_check(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        with temporary_browser_login_state() as state:
            secret_path = state.directory / "google-client-secret.bin"
            original_lstat = runtime_module.os.lstat

            class ReparseMetadata:
                def __init__(self, metadata):
                    self._metadata = metadata
                    self.st_file_attributes = (
                        getattr(metadata, "st_file_attributes", 0) | 0x400
                    )

                def __getattr__(self, name):
                    return getattr(self._metadata, name)

            def marked_lstat(candidate):
                metadata = original_lstat(candidate)
                if Path(candidate) == secret_path:
                    return ReparseMetadata(metadata)
                return metadata

            with mock.patch.object(
                runtime_module.os,
                "lstat",
                side_effect=marked_lstat,
            ):
                with self.assertRaises(
                    DurableGoogleLoginConfigurationError
                ):
                    load_durable_google_login_configuration(
                        state.configuration_path
                    )

        with temporary_browser_login_state() as state:
            closed = []

            class Gateway:
                def close(self):
                    closed.append("gateway")

            class Browser:
                def matches_route(self, _path):
                    return True

            def gateway_factory(_configuration, secret):
                secret.clear()
                return Gateway()

            original_final_check = (
                runtime_module._reverify_secret_file_references
            )

            def replace_at_final_check(configuration):
                _replace_regular_file(
                    state.directory / "google-client-secret.bin",
                    b"z" * 32,
                )
                return original_final_check(configuration)

            with mock.patch.object(
                runtime_module,
                "_reverify_secret_file_references",
                side_effect=replace_at_final_check,
            ):
                with self.assertRaises(
                    DurableGoogleLoginConfigurationError
                ):
                    build_durable_google_login_runtime(
                        state.configuration_path,
                        _clock=state.clock,
                        _gateway_factory=gateway_factory,
                        _browser_integration_factory=lambda **_values: (
                            Browser()
                        ),
                    )
            self.assertEqual(closed, ["gateway"])

    def test_construction_paths_are_absent_from_reachable_runtime_graph(self):
        canary = "PRIVATE_SECRET_PATH_CANARY_776a"
        database_canary = "SEALED_DATABASE_AUTHORITY_CANARY_4c19"
        with temporary_browser_login_state() as state:
            document = _configuration_document(state)
            renamed_database = state.database_path.with_name(
                database_canary + ".sqlite"
            )
            state.database_path.replace(renamed_database)
            state.database_path = renamed_database
            document["database_path"] = str(renamed_database)
            for field, filename in (
                (
                    "google_client_secret_file",
                    canary + "-client.bin",
                ),
            ):
                old = Path(document[field])
                new = old.with_name(filename)
                old.replace(new)
                document[field] = str(new)
            for ring_name in ("oidc_lookup_keys", "oidc_protection_keys"):
                for index, item in enumerate(document[ring_name]):
                    old = Path(item["file"])
                    new = old.with_name(
                        f"{canary}-{ring_name}-{index}.key"
                    )
                    old.replace(new)
                    item["file"] = str(new)
            _write_configuration(state, document)
            runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            browser = runtime.browser_integration
            connections = object.__getattribute__(
                runtime,
                "_connections",
            )
            target = object.__getattribute__(connections, "_target")
            try:
                _assert_canary_absent(self, runtime, canary)
                _assert_canary_absent(
                    self,
                    runtime.configuration,
                    database_canary,
                )
                self.assertEqual(
                    type(target).__name__,
                    "_DatabaseTargetAuthority",
                )
                self.assertNotIn(database_canary, repr(target))
                self.assertNotIn(
                    database_canary,
                    repr(runtime.configuration),
                )
            finally:
                runtime.close()
            self.assertIsNone(
                object.__getattribute__(connections, "_target")
            )
            _assert_canary_absent(self, browser, canary)
            _assert_canary_absent(self, browser, database_canary)

    def test_ephemeral_tls_files_are_identity_bound_and_removed_before_use(self):
        from scripts import durable_google_login_app

        original_temporary_directory = tempfile.TemporaryDirectory
        original_tls_write = (
            durable_google_login_app._write_ephemeral_tls_file
        )
        directories = []
        written_modes = []

        def tracked_temporary_directory(*args, **kwargs):
            temporary = original_temporary_directory(*args, **kwargs)
            directories.append(Path(temporary.name))
            return temporary

        def tracked_tls_write(path, payload):
            identity = original_tls_write(path, payload)
            written_modes.append(stat.S_IMODE(path.stat().st_mode))
            return identity

        with (
            mock.patch.object(
                durable_google_login_app.tempfile,
                "TemporaryDirectory",
                side_effect=tracked_temporary_directory,
            ),
            mock.patch.object(
                durable_google_login_app,
                "_write_ephemeral_tls_file",
                side_effect=tracked_tls_write,
            ),
        ):
            with durable_google_login_app._ephemeral_tls_context() as context:
                self.assertIsInstance(context, durable_google_login_app.ssl.SSLContext)
                self.assertEqual(len(directories), 1)
                self.assertFalse(directories[0].exists())
        self.assertFalse(directories[0].exists())
        if os.name != "nt":
            self.assertEqual(written_modes, [0o600, 0o600])

        replaced_directories = []

        class ReplacingTlsContext:
            minimum_version = None
            options = 0

            def load_cert_chain(self, *, certfile, keyfile):
                key_path = Path(keyfile)
                _replace_regular_file(
                    key_path,
                    key_path.read_bytes(),
                )

        def tracked_replacement_directory(*args, **kwargs):
            temporary = original_temporary_directory(*args, **kwargs)
            replaced_directories.append(Path(temporary.name))
            return temporary

        with (
            mock.patch.object(
                durable_google_login_app.tempfile,
                "TemporaryDirectory",
                side_effect=tracked_replacement_directory,
            ),
            mock.patch.object(
                durable_google_login_app.ssl,
                "SSLContext",
                return_value=ReplacingTlsContext(),
            ),
        ):
            with self.assertRaises(RuntimeError):
                durable_google_login_app._ephemeral_tls_context().__enter__()
        self.assertEqual(len(replaced_directories), 1)
        self.assertFalse(replaced_directories[0].exists())

        failed_directories = []
        failed_key_buffers = []

        def tracked_failure_directory(*args, **kwargs):
            temporary = original_temporary_directory(*args, **kwargs)
            failed_directories.append(Path(temporary.name))
            return temporary

        def failing_key_write(path, payload):
            if path.name == "localhost-key.pem":
                failed_key_buffers.append(payload)
                raise RuntimeError("injected_tls_key_write_failure")
            return original_tls_write(path, payload)

        with (
            mock.patch.object(
                durable_google_login_app.tempfile,
                "TemporaryDirectory",
                side_effect=tracked_failure_directory,
            ),
            mock.patch.object(
                durable_google_login_app,
                "_write_ephemeral_tls_file",
                side_effect=failing_key_write,
            ),
        ):
            with self.assertRaises(RuntimeError):
                durable_google_login_app._ephemeral_tls_context().__enter__()
        self.assertEqual(len(failed_directories), 1)
        self.assertFalse(failed_directories[0].exists())
        self.assertEqual(len(failed_key_buffers), 1)
        self.assertEqual(len(failed_key_buffers[0]), 0)

    def test_sqlite_uri_escapes_hash_and_percent_in_database_filename(self):
        from wahojobs.durable_google_login_runtime import (
            _open_writable_connection,
            _read_only_connection_scope,
            _sqlite_file_uri,
        )

        with temporary_browser_login_state() as state:
            escaped_path = state.database_path.with_name(
                "browser#login%state.sqlite"
            )
            state.database_path.replace(escaped_path)

            uri = _sqlite_file_uri(escaped_path, mode="rw")
            self.assertIn("%23", uri)
            self.assertIn("%25", uri)
            self.assertNotIn("#", uri)

            connection = _open_writable_connection(escaped_path)
            try:
                self.assertGreater(
                    connection.execute("PRAGMA schema_version").fetchone()[0],
                    0,
                )
                resolved = connection.execute(
                    "PRAGMA database_list"
                ).fetchone()[2]
                self.assertEqual(Path(resolved).resolve(), escaped_path)
            finally:
                connection.close()

            with _read_only_connection_scope(escaped_path) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA query_only").fetchone()[0],
                    1,
                )
                self.assertGreater(
                    connection.execute("PRAGMA schema_version").fetchone()[0],
                    0,
                )

    def test_launcher_starts_tls_configured_inactive_server_in_strict_order(
        self,
    ):
        from scripts import durable_google_login_app

        events = []
        configuration_path = str(ROOT / "unused-runtime.json")

        class BrowserIntegration:
            def matches_route(self, _path):
                return False

            def handle(self, **_request):
                raise AssertionError("request_not_expected")

        class Runtime:
            configuration = SimpleNamespace(
                bind_host="127.0.0.1",
                bind_port=8443,
                public_origin="https://localhost:8443",
            )
            browser_integration = BrowserIntegration()

            def close(self):
                events.append("runtime_close")

        def runtime_builder(path):
            events.append(("runtime_builder", path))
            return Runtime()

        class Socket:
            def __init__(self, name):
                self.name = name
                self.closed = False

            def close(self):
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 31

        class TlsContext:
            def wrap_socket(self, *_args, **_kwargs):
                raise AssertionError(
                    "listener_must_not_perform_implicit_tls_handshake"
                )

        class TlsScope:
            def __enter__(self):
                events.append("tls_enter")
                return TlsContext()

            def __exit__(self, exc_type, _exc, _traceback):
                events.append(
                    ("tls_exit", None if exc_type is None else exc_type)
                )
                return False

        class Server:
            def __init__(self, address, _handler, bind_and_activate):
                events.append(
                    (
                        "server_construct",
                        address,
                        bind_and_activate,
                    )
                )
                self.socket = Socket("plain-socket")
                self.outcome = None

            def set_shutdown_notification(self, requested):
                self.requested = requested
                events.append("shutdown_notification")
                return True

            def set_tls_context(self, context, *, handshake_timeout):
                self.context = context
                self.handshake_timeout = handshake_timeout
                events.append("tls_configured")
                return True

            def set_serve_lifecycle(self, outcome, signal_state):
                self.outcome = outcome
                self.signal_state = signal_state
                events.append("serve_lifecycle")
                return True

            def claim_serving_readiness(self):
                return self.outcome.claim_ready(self.signal_state)

            def server_bind(self):
                events.append("server_bind")

            def server_activate(self):
                events.append("server_activate")

            def serve_forever(self, *, poll_interval):
                self.poll_interval = poll_interval
                events.append("serve_forever")
                self.outcome.publish_serving_checkpoint(
                    self.signal_state
                )
                deadline = time.monotonic() + 1
                while (
                    not self.outcome.ready_state_reached
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.001)

            def server_close(self):
                events.append("server_close")
                self.socket.close()

        with mock.patch("builtins.print"):
            result = durable_google_login_app.main(
                ["--config", configuration_path],
                _runtime_builder=runtime_builder,
                _server_factory=Server,
                _tls_context_factory=TlsScope,
            )

        self.assertEqual(result, 2)
        self.assertEqual(
            events,
            [
                ("runtime_builder", configuration_path),
                (
                    "server_construct",
                    ("127.0.0.1", 8443),
                    False,
                ),
                "shutdown_notification",
                "tls_enter",
                "tls_configured",
                "server_bind",
                "server_activate",
                "serve_lifecycle",
                "serve_forever",
                "server_close",
                ("tls_exit", None),
                "runtime_close",
            ],
        )

    def test_launcher_server_suppresses_request_exception_tracebacks(self):
        from scripts import durable_google_login_app

        class ForbiddenErrorStream:
            def write(self, _value):
                raise AssertionError("request_traceback_must_not_be_written")

            def flush(self):
                return None

        server = object.__new__(
            durable_google_login_app._DrainingThreadingHTTPServer
        )
        try:
            raise RuntimeError("SESSION_TOKEN_must_not_be_disclosed")
        except RuntimeError:
            with mock.patch.object(sys, "stderr", ForbiddenErrorStream()):
                server.handle_error(None, ("127.0.0.1", 1))

    def test_launcher_attempts_all_cleanup_after_serve_failure(self):
        from scripts import durable_google_login_app
        from wahojobs import durable_google_login_runtime

        events = []
        runtimes = []
        servers = []

        class BrowserIntegration:
            def matches_route(self, _path):
                return False

            def handle(self, **_request):
                raise AssertionError("request_not_expected")

        class Runtime:
            configuration = SimpleNamespace(
                bind_host="127.0.0.1",
                bind_port=8443,
                public_origin="https://localhost:8443",
            )
            browser_integration = BrowserIntegration()

            def __init__(self):
                self.allow_close = False
                runtimes.append(self)

            def close(self, *, _preserve_primary=False):
                events.append("runtime_close")
                if not self.allow_close:
                    raise RuntimeError("runtime_close_failed")
                return True

        class TlsContext:
            def wrap_socket(self, *_args, **_kwargs):
                raise AssertionError(
                    "listener_must_not_perform_implicit_tls_handshake"
                )

        class TlsScope:
            def __enter__(self):
                events.append("tls_enter")
                return TlsContext()

            def __exit__(self, exc_type, _exc, _traceback):
                events.append(("tls_exit", exc_type))
                return False

        class Server:
            def __init__(self, _address, _handler, bind_and_activate):
                events.append(("server_construct", bind_and_activate))
                self.socket = Socket()
                self.allow_close = False
                servers.append(self)

            def set_shutdown_notification(self, requested):
                self.requested = requested
                events.append("shutdown_notification")
                return True

            def set_tls_context(self, context, *, handshake_timeout):
                self.context = context
                self.handshake_timeout = handshake_timeout
                events.append("tls_configured")
                return True

            def set_serve_lifecycle(self, outcome, signal_state):
                self.outcome = outcome
                self.signal_state = signal_state
                events.append("serve_lifecycle")
                return True

            def server_bind(self):
                events.append("server_bind")

            def server_activate(self):
                events.append("server_activate")

            def serve_forever(self, *, poll_interval):
                self.poll_interval = poll_interval
                events.append("serve_forever")
                raise RuntimeError("serve_failed")

            def server_close(self):
                events.append("server_close")
                if not self.allow_close:
                    raise RuntimeError("server_close_failed")
                return True

        class Socket:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 37

        with mock.patch("builtins.print"):
            result = durable_google_login_app.main(
                ["--config", str(ROOT / "unused-runtime.json")],
                _runtime_builder=lambda _path: Runtime(),
                _server_factory=Server,
                _tls_context_factory=TlsScope,
            )

        self.assertEqual(result, 3)
        self.assertEqual(
            events,
            [
                ("server_construct", False),
                "shutdown_notification",
                "tls_enter",
                "tls_configured",
                "server_bind",
                "server_activate",
                "serve_lifecycle",
                "serve_forever",
                "server_close",
                ("tls_exit", None),
                "runtime_close",
                "server_close",
                "runtime_close",
            ],
        )
        self.assertEqual(len(runtimes), 1)
        self.assertEqual(len(servers), 1)
        runtimes[0].allow_close = True
        servers[0].allow_close = True
        self.assertTrue(
            durable_google_login_runtime
            ._retry_unresolved_activation_handoffs()
        )

    def test_invalid_runtime_builder_aborts_before_tls_or_server(self):
        from scripts import durable_google_login_app

        events = []

        def invalid_runtime_builder(_path):
            events.append("runtime_builder")
            raise DurableGoogleLoginConfigurationError()

        def forbidden_tls_factory():
            events.append("tls_factory")
            raise AssertionError("tls_must_not_be_constructed")

        def forbidden_server_factory(*_args, **_kwargs):
            events.append("server_factory")
            raise AssertionError("server_must_not_be_constructed")

        with mock.patch("builtins.print"):
            result = durable_google_login_app.main(
                ["--config", str(ROOT / "invalid-runtime.json")],
                _runtime_builder=invalid_runtime_builder,
                _server_factory=forbidden_server_factory,
                _tls_context_factory=forbidden_tls_factory,
            )

        self.assertEqual(result, 2)
        self.assertEqual(events, ["runtime_builder"])

    def test_cleanup_coordinator_failure_matrix_is_reverse_retryable_and_control_safe(
        self,
    ):
        from wahojobs.durable_google_login_runtime import (
            _CleanupCoordinator,
        )

        categories = (
            "google_gateway",
            "key_authority",
            "database_connections",
        )
        exception_types = (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        )

        for failed_category in categories:
            for exception_type in exception_types:
                with self.subTest(
                    failed_category=failed_category,
                    exception_type=exception_type.__name__,
                ):
                    events = []
                    injected = exception_type(
                        "PRIVATE_CLEANUP_FAILURE_CANARY"
                    )

                    class Resource:
                        def __init__(self, category):
                            self.category = category
                            self.calls = 0

                        def close(self):
                            self.calls += 1
                            events.append(self.category)
                            if (
                                self.category == failed_category
                                and self.calls == 1
                            ):
                                raise injected

                    coordinator = _CleanupCoordinator()
                    resources = {}
                    for category in categories:
                        resource = Resource(category)
                        resources[category] = resource
                        coordinator.own(
                            category,
                            resource,
                            lambda owned: owned.close(),
                        )

                    if exception_type is RuntimeError:
                        first = coordinator.cleanup()
                    else:
                        with self.assertRaises(exception_type) as caught:
                            coordinator.cleanup()
                        self.assertIs(caught.exception, injected)
                        first = coordinator.snapshot()

                    self.assertEqual(events, list(reversed(categories)))
                    self.assertFalse(first.cleanup_complete)
                    self.assertEqual(
                        first.unresolved_resources,
                        (failed_category,),
                    )
                    self.assertIn(
                        failed_category,
                        first.failure_categories,
                    )

                    second = coordinator.cleanup()
                    self.assertTrue(second.cleanup_complete)
                    self.assertEqual(
                        resources[failed_category].calls,
                        2,
                    )
                    for category in categories:
                        if category != failed_category:
                            self.assertEqual(resources[category].calls, 1)

    def test_cleanup_coordinator_has_one_concurrent_owner(self):
        from wahojobs.durable_google_login_runtime import (
            _CleanupCoordinator,
        )

        coordinator = _CleanupCoordinator()
        entered = threading.Event()
        release = threading.Event()
        calls = []
        reports = []

        class Resource:
            def close(self):
                calls.append("close")
                entered.set()
                release.wait(2)

        coordinator.own(
            "google_gateway",
            Resource(),
            lambda owned: owned.close(),
        )

        def clean():
            reports.append(coordinator.cleanup())

        first = threading.Thread(target=clean)
        second = threading.Thread(target=clean)
        first.start()
        self.assertTrue(entered.wait(2))
        second.start()
        time.sleep(0.05)
        release.set()
        first.join(2)
        second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(calls, ["close"])
        self.assertEqual(len(reports), 2)
        self.assertTrue(all(report.cleanup_complete for report in reports))

    def test_cleanup_dependencies_defer_only_live_requests_not_close_failures(
        self,
    ):
        from wahojobs.durable_google_login_runtime import (
            _CleanupCoordinator,
        )

        for exception_type in (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(exception_type=exception_type.__name__):
                events = []
                injected = exception_type(
                    "PRIVATE_BROWSER_CLEANUP_CONTROL"
                )
                browser_calls = 0

                def close_browser(_resource):
                    nonlocal browser_calls
                    browser_calls += 1
                    events.append("browser_integration")
                    if browser_calls == 1:
                        raise injected
                    return True

                coordinator = _CleanupCoordinator()
                for category in (
                    "google_gateway",
                    "key_authority",
                    "database_connections",
                ):
                    coordinator.own(
                        category,
                        object(),
                        lambda _resource, category=category: (
                            events.append(category) or True
                        ),
                        dependencies=("browser_integration",),
                    )
                coordinator.own(
                    "browser_integration",
                    object(),
                    close_browser,
                )

                if exception_type is RuntimeError:
                    first = coordinator.cleanup()
                else:
                    with self.assertRaises(exception_type) as caught:
                        coordinator.cleanup()
                    self.assertIs(caught.exception, injected)
                    first = coordinator.snapshot()
                self.assertEqual(
                    events,
                    [
                        "browser_integration",
                        "database_connections",
                        "key_authority",
                        "google_gateway",
                    ],
                )
                self.assertEqual(
                    first.unresolved_resources,
                    ("browser_integration",),
                )
                self.assertTrue(coordinator.cleanup().cleanup_complete)
                self.assertEqual(browser_calls, 2)

        active = True
        events = []
        coordinator = _CleanupCoordinator()
        for category in (
            "google_gateway",
            "key_authority",
            "database_connections",
        ):
            coordinator.own(
                category,
                object(),
                lambda _resource, category=category: (
                    events.append(category) or True
                ),
                dependencies=("browser_integration",),
            )
        coordinator.own(
            "browser_integration",
            object(),
            lambda _resource: (
                events.append("browser_integration") or not active
            ),
        )
        self.assertFalse(coordinator.cleanup().cleanup_complete)
        self.assertEqual(events, ["browser_integration"])
        active = False
        self.assertTrue(coordinator.cleanup().cleanup_complete)
        self.assertEqual(
            events,
            [
                "browser_integration",
                "browser_integration",
                "database_connections",
                "key_authority",
                "google_gateway",
            ],
        )

    def test_cleanup_coordinator_multiple_failures_retry_without_duplicates(
        self,
    ):
        from wahojobs.durable_google_login_runtime import (
            _CleanupCoordinator,
        )

        calls = {}
        coordinator = _CleanupCoordinator()
        categories = (
            "google_gateway",
            "key_authority",
            "database_connections",
        )
        for category in categories:
            calls[category] = 0

            def close(_resource, category=category):
                calls[category] += 1
                if calls[category] == 1:
                    raise RuntimeError("PRIVATE_MULTI_CLOSE_FAILURE")

            coordinator.own(category, object(), close)
        first = coordinator.cleanup()
        self.assertFalse(first.cleanup_complete)
        self.assertEqual(
            set(first.unresolved_resources),
            set(categories),
        )
        self.assertEqual(
            set(first.failure_categories),
            set(categories),
        )
        self.assertTrue(coordinator.cleanup().cleanup_complete)
        self.assertEqual(calls, {category: 2 for category in categories})

    def test_runtime_close_retries_only_failed_gateway(self):
        calls = []

        with temporary_browser_login_state() as state:
            runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            gateway_type = type(state.gateway_harness.gateway)
            real_close = gateway_type.close

            def flaky_close(gateway):
                calls.append("gateway")
                if len(calls) == 1:
                    raise RuntimeError(
                        "PRIVATE_GATEWAY_CLOSE_FAILURE"
                    )
                return real_close(gateway)

            with mock.patch.object(gateway_type, "close", flaky_close):
                first = runtime.close()
                self.assertFalse(first.cleanup_complete)
                self.assertEqual(
                    first.unresolved_resources,
                    ("google_gateway",),
                )
                self.assertIsNone(
                    object.__getattribute__(runtime, "_connections")
                )
                self.assertIsNone(
                    object.__getattribute__(runtime, "_key_authority")
                )

                second = runtime.close()
                self.assertTrue(second.cleanup_complete)
            self.assertEqual(calls, ["gateway", "gateway"])
            self.assertIn("closed", repr(runtime))

    def test_runtime_shutdown_admission_is_monotonic_after_partial_close(
        self,
    ):
        close_calls = 0

        with temporary_browser_login_state() as state:
            runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            gateway_type = type(state.gateway_harness.gateway)
            real_close = gateway_type.close

            def flaky_close(gateway):
                nonlocal close_calls
                close_calls += 1
                if close_calls == 1:
                    raise RuntimeError("PRIVATE_PARTIAL_CLOSE")
                return real_close(gateway)

            with mock.patch.object(gateway_type, "close", flaky_close):
                self.assertFalse(runtime.close().cleanup_complete)
                for operation in (
                    lambda: runtime.browser_integration,
                    runtime.open_writable_connection,
                    runtime.read_only_connection_provider,
                ):
                    with self.assertRaises(
                        DurableGoogleLoginConfigurationError
                    ):
                        operation()
                self.assertIn("closing", repr(runtime))
                self.assertTrue(runtime.close().cleanup_complete)
            self.assertEqual(close_calls, 2)

    def test_lookup_and_protection_cleanup_retry_independently(self):
        import wahojobs.google_oidc_transaction_protection as protection

        for failure_position in ("lookup", "protection"):
            for exception_type in (
                RuntimeError,
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ):
                with self.subTest(
                    failure_position=failure_position,
                    exception_type=exception_type.__name__,
                ):
                    with temporary_browser_login_state() as state:
                        runtime = build_durable_google_login_runtime(
                            state.configuration_path,
                            _clock=state.clock,
                            _gateway_factory=state.gateway_factory,
                        )
                        original = protection._clear_key_ring
                        calls = 0
                        injected = exception_type(
                            "PRIVATE_KEY_RING_CLOSE_FAILURE"
                        )

                        def flaky_clear(ring):
                            nonlocal calls
                            calls += 1
                            if (
                                failure_position == "lookup"
                                and calls == 1
                            ) or (
                                failure_position == "protection"
                                and calls == 2
                            ):
                                raise injected
                            return original(ring)

                        with mock.patch.object(
                            protection,
                            "_clear_key_ring",
                            side_effect=flaky_clear,
                        ):
                            if exception_type is RuntimeError:
                                report = runtime.close()
                            else:
                                with self.assertRaises(
                                    exception_type
                                ) as caught:
                                    runtime.close()
                                self.assertIs(
                                    caught.exception,
                                    injected,
                                )
                                report = (
                                    runtime._cleanup_coordinator.snapshot()
                                )
                            self.assertFalse(report.cleanup_complete)
                            failed_category = (
                                "lookup_authority"
                                if failure_position == "lookup"
                                else "protection_authority"
                            )
                            other_category = (
                                "protection_authority"
                                if failure_position == "lookup"
                                else "lookup_authority"
                            )
                            self.assertEqual(
                                report.unresolved_resources,
                                (failed_category,),
                            )
                            self.assertTrue(
                                runtime._cleanup_coordinator.is_terminal(
                                    other_category
                                )
                            )
                            self.assertTrue(
                                runtime.close().cleanup_complete
                            )
                            self.assertEqual(calls, 3)
                            calls_after_retry = calls
                            self.assertTrue(
                                runtime.close().cleanup_complete
                            )
                            self.assertEqual(calls, calls_after_retry)
                            self.assertIn("closed", repr(runtime))

    def test_database_rollback_and_close_failures_are_independent_and_retryable(
        self,
    ):
        import wahojobs.durable_google_login_runtime as runtime_module

        exception_types = (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        )
        for failure_position in ("rollback", "close", "both"):
            for exception_type in exception_types:
                with self.subTest(
                    failure_position=failure_position,
                    exception_type=exception_type.__name__,
                ):
                    with tempfile.TemporaryDirectory(
                        prefix="wahojobs-db-cleanup-probe-"
                    ) as directory:
                        path = Path(directory) / "cleanup.sqlite"
                        actual = sqlite3.connect(path)
                        actual.execute("CREATE TABLE probe(value INTEGER)")
                        actual.commit()
                        actual.execute("BEGIN IMMEDIATE")
                        injected = exception_type(
                            "PRIVATE_DATABASE_CLEANUP_CANARY"
                        )

                        class Connection:
                            def __init__(self):
                                self.rollback_calls = 0
                                self.close_calls = 0
                                self.closed = False

                            @property
                            def in_transaction(self):
                                return (
                                    False
                                    if self.closed
                                    else actual.in_transaction
                                )

                            def rollback(self):
                                self.rollback_calls += 1
                                if (
                                    failure_position in {"rollback", "both"}
                                    and self.rollback_calls == 1
                                ):
                                    raise injected
                                actual.rollback()

                            def close(self):
                                self.close_calls += 1
                                if (
                                    failure_position in {"close", "both"}
                                    and self.close_calls == 1
                                ):
                                    raise injected
                                actual.close()
                                self.closed = True

                        connection = Connection()
                        terminal, failed, control = (
                            runtime_module._cleanup_database_connection_independently(
                                connection,
                                rollback=True,
                            )
                        )
                        self.assertTrue(failed)
                        self.assertEqual(connection.close_calls, 1)
                        if exception_type is RuntimeError:
                            self.assertIsNone(control)
                        else:
                            self.assertIs(control, injected)

                        if not terminal:
                            terminal, _failed, _control = (
                                runtime_module._cleanup_database_connection_independently(
                                    connection,
                                    rollback=True,
                                )
                            )
                        self.assertTrue(terminal)
                        verifier = sqlite3.connect(path)
                        try:
                            self.assertEqual(
                                verifier.execute(
                                    "PRAGMA quick_check(1)"
                                ).fetchone(),
                                ("ok",),
                            )
                        finally:
                            verifier.close()
                        self.assertFalse(
                            Path(str(path) + "-journal").exists()
                        )

    def test_attestation_primary_failure_retains_connection_for_retry(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        for exception_type in (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(exception_type=exception_type.__name__):
                with temporary_browser_login_state() as state:
                    configuration = (
                        runtime_module._load_construction_configuration(
                            state.configuration_path
                        )
                    )
                    coordinator = runtime_module._CleanupCoordinator()
                    original_cleanup = (
                        runtime_module._cleanup_database_connection_independently
                    )
                    cleanup_calls = 0
                    connections = []

                    def flaky_cleanup(connection, *, rollback):
                        nonlocal cleanup_calls
                        cleanup_calls += 1
                        connections.append(connection)
                        if cleanup_calls == 1:
                            return False, True, None
                        return original_cleanup(
                            connection,
                            rollback=rollback,
                        )

                    injected = exception_type(
                        "PRIVATE_ATTESTATION_PRIMARY"
                    )
                    with (
                        mock.patch.object(
                            runtime_module,
                            "_attest_closed_database_schema",
                            side_effect=injected,
                        ),
                        mock.patch.object(
                            runtime_module,
                            "_cleanup_database_connection_independently",
                            side_effect=flaky_cleanup,
                        ),
                    ):
                        with self.assertRaises(exception_type) as caught:
                            runtime_module._attest_existing_database(
                                configuration.database_target,
                                cleanup_coordinator=coordinator,
                            )
                        self.assertIs(caught.exception, injected)
                        self.assertEqual(
                            coordinator.snapshot().unresolved_resources,
                            ("database_attestation_connection",),
                        )
                        self.assertTrue(
                            coordinator.cleanup().cleanup_complete
                        )
                    self.assertEqual(cleanup_calls, 2)
                    self.assertTrue(
                        runtime_module._database_connection_is_closed(
                            connections[0]
                        )
                    )
                    self.assertEqual(
                        _sqlite_sidecar_paths(state.database_path),
                        (),
                    )
                    configuration = None

    def test_read_only_scope_preserves_primary_and_retries_connection(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        for exception_type in (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(exception_type=exception_type.__name__):
                with temporary_browser_login_state() as state:
                    configuration = (
                        runtime_module._load_construction_configuration(
                            state.configuration_path
                        )
                    )
                    original_cleanup = (
                        runtime_module._cleanup_database_connection_independently
                    )
                    cleanup_calls = 0
                    connections = []

                    def flaky_cleanup(connection, *, rollback):
                        nonlocal cleanup_calls
                        cleanup_calls += 1
                        connections.append(connection)
                        if cleanup_calls == 1:
                            return False, True, None
                        return original_cleanup(
                            connection,
                            rollback=rollback,
                        )

                    injected = exception_type(
                        "PRIVATE_READ_ONLY_PRIMARY"
                    )
                    with mock.patch.object(
                        runtime_module,
                        "_cleanup_database_connection_independently",
                        side_effect=flaky_cleanup,
                    ):
                        with self.assertRaises(exception_type) as caught:
                            with runtime_module._read_only_connection_scope(
                                configuration.database_target
                            ) as connection:
                                connection.execute("BEGIN")
                                raise injected
                    self.assertIs(caught.exception, injected)
                    self.assertEqual(cleanup_calls, 2)
                    self.assertTrue(
                        runtime_module._database_connection_is_closed(
                            connections[0]
                        )
                    )
                    self.assertEqual(
                        _sqlite_sidecar_paths(state.database_path),
                        (),
                    )
                    configuration = None

    def test_real_server_cleanup_tracks_sockets_and_bounded_request_threads(
        self,
    ):
        from scripts import durable_google_login_app

        entered = threading.Event()
        release = threading.Event()

        class HandshakeSocket:
            def __init__(self, raw):
                self.raw = raw

            def do_handshake(self):
                return None

            def __getattr__(self, name):
                return getattr(self.raw, name)

        class TlsContext:
            @staticmethod
            def wrap_socket(
                request,
                *,
                server_side,
                do_handshake_on_connect,
            ):
                self.assertTrue(server_side)
                self.assertFalse(do_handshake_on_connect)
                return HandshakeSocket(request)

        class BlockingHandler:
            def __init__(self, _request, _address, _server):
                entered.set()
                release.wait(2)

        server = durable_google_login_app._DrainingThreadingHTTPServer(
            ("127.0.0.1", 0),
            durable_google_login_app._UnpublishedRequestHandler,
            False,
        )
        outcome = durable_google_login_app._ServeOutcome()
        shutdown_state = durable_google_login_app._SignalShutdownState()
        client = None
        serve_thread = None
        try:
            server.set_shutdown_notification(
                lambda: shutdown_state.requested
            )
            server.set_tls_context(TlsContext())
            server.publish_handler(BlockingHandler)
            server.server_bind()
            server.server_activate()
            server.set_serve_lifecycle(outcome, shutdown_state)
            self.assertTrue(outcome.begin_starting())
            address = server.server_address
            serve_thread = threading.Thread(
                target=durable_google_login_app._serve_in_thread,
                args=(server, outcome),
                daemon=False,
            )
            serve_thread.start()
            self.assertEqual(
                outcome.wait_for_startup(1, shutdown_state),
                "serving",
            )
            self.assertTrue(server.claim_serving_readiness())
            client = socket.create_connection(address, timeout=2)
            self.assertTrue(entered.wait(2))
            counts = server.resource_counts()
            self.assertEqual(counts["accepted_sockets"], 1)
            self.assertEqual(counts["request_threads"], 1)

            shutdown_state._handle(signal.SIGINT, None)
            self.assertTrue(shutdown_state.requested)
            self.assertEqual(shutdown_state.category, "sigint")
            server.begin_shutdown()
            self.assertTrue(server.close_listener())
            self.assertTrue(server.close_accepted_sockets())
            self.assertFalse(server.drain_request_threads(0.01))
            self.assertEqual(
                server.resource_counts()["request_threads"],
                1,
            )

            release.set()
            self.assertTrue(server.drain_request_threads(2))
            self.assertTrue(server.detach_route_integration())
            server.server_close()
            serve_thread.join(2)
            self.assertFalse(serve_thread.is_alive())
            self.assertEqual(
                server.resource_counts(),
                {
                    "listener": 0,
                    "accepted_sockets": 0,
                    "pending_handshakes": 0,
                    "request_threads": 0,
                    "serve_threads": 0,
                    "route_integrations": 0,
                },
            )
        finally:
            release.set()
            if client is not None:
                client.close()
            try:
                server.begin_shutdown()
            except Exception:
                pass
            server.close_accepted_sockets()
            server.drain_request_threads(2)
            server.close_listener()
            server.server_close()
            if serve_thread is not None:
                serve_thread.join(2)

    def test_failed_request_socket_close_remains_owned_until_retry(self):
        from scripts import durable_google_login_app

        class Request:
            def __init__(self):
                self.close_calls = 0
                self.closed = False

            def shutdown(self, _how):
                raise OSError("already_shutting_down")

            def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError("PRIVATE_SOCKET_CLOSE_FAILURE")
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 19

        request = Request()
        server = object.__new__(
            durable_google_login_app._DrainingThreadingHTTPServer
        )
        server._lifecycle_lock = threading.Lock()
        server._pending_handshakes = set()
        server._accepted_sockets = {request}
        server._request_threads = {threading.current_thread()}
        server._shutdown_requested = None
        with mock.patch.object(
            durable_google_login_app._DrainingThreadingHTTPServer,
            "process_request_thread",
            return_value=None,
        ):
            server._tracked_process_request(
                request,
                ("127.0.0.1", 1),
            )
        self.assertEqual(len(server._accepted_sockets), 1)
        self.assertFalse(request.closed)
        self.assertTrue(server.close_accepted_sockets())
        self.assertEqual(len(server._accepted_sockets), 0)
        self.assertTrue(request.closed)
        self.assertEqual(request.close_calls, 2)

    def test_shutdown_race_socket_close_failure_stays_owned_for_retry(self):
        from scripts import durable_google_login_app

        class Request:
            def __init__(self):
                self.close_calls = 0
                self.closed = False

            def shutdown(self, _how):
                raise OSError("already_shutting_down")

            def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError("PRIVATE_SOCKET_CLOSE_FAILURE")
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 79

        request = Request()
        server = object.__new__(
            durable_google_login_app._DrainingThreadingHTTPServer
        )
        server._lifecycle_lock = threading.Lock()
        server._stopping = True
        server._serve_active = False
        server._serve_outcome = None
        server._pending_handshakes = set()
        server._unregistered_handshake = None
        server._accepted_sockets = {request}
        server._request_threads = set()
        server._shutdown_requested = None
        server._database_lifetime_guard = None
        server._database_lifetime_guard_required = False
        server._database_lifetime_validation_lock = threading.Lock()
        server._database_lifetime_valid = True
        server.process_request(request, ("127.0.0.1", 1))
        self.assertIn(request, server._accepted_sockets)
        self.assertFalse(request.closed)
        self.assertTrue(server.close_accepted_sockets())
        self.assertNotIn(request, server._accepted_sockets)
        self.assertTrue(request.closed)
        self.assertEqual(request.close_calls, 2)

    def test_request_thread_control_publishes_one_sanitized_terminal_failure(
        self,
    ):
        from scripts import durable_google_login_app

        class Request:
            def __init__(self):
                self.closed = False

            def shutdown(self, _how):
                return None

            def close(self):
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 83

        request = Request()
        signal_state = durable_google_login_app._SignalShutdownState()
        outcome = durable_google_login_app._ServeOutcome()
        self.assertTrue(outcome.begin_starting())
        self.assertTrue(
            outcome.publish_serving_checkpoint(signal_state)
        )
        self.assertTrue(outcome.claim_ready(signal_state))
        server = object.__new__(
            durable_google_login_app._DrainingThreadingHTTPServer
        )
        server._lifecycle_lock = threading.Lock()
        server._stopping = False
        server._serve_active = False
        server._serve_outcome = outcome
        server._signal_state = signal_state
        server._pending_handshakes = set()
        server._unregistered_handshake = None
        server._accepted_sockets = {request}
        server._request_threads = set()
        server._shutdown_requested = None
        injected = SystemExit("PRIVATE_REQUEST_THREAD_CONTROL")
        worker = threading.Thread(
            target=server._tracked_process_request,
            args=(request, ("127.0.0.1", 1)),
            daemon=False,
        )
        server._request_threads.add(worker)
        with mock.patch.object(
            durable_google_login_app._DrainingThreadingHTTPServer,
            "process_request_thread",
            side_effect=injected,
        ):
            worker.start()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertIs(outcome.control, injected)
        self.assertEqual(outcome.state, "failed")
        self.assertTrue(outcome.done.is_set())
        self.assertTrue(server._stopping)
        self.assertTrue(request.closed)
        self.assertEqual(server._accepted_sockets, set())
        with server._lifecycle_lock:
            self.assertTrue(
                server._reap_request_threads_locked()
            )
        self.assertEqual(server._request_threads, set())
        self.assertTrue(server.drain_request_threads(0))
        self.assertEqual(server._request_threads, set())
        self.assertEqual(injected.args, ())
        self.assertIsNone(injected.code)

    def test_signal_handlers_choose_first_signal_restore_and_map_exit_status(
        self,
    ):
        from scripts import durable_google_login_app

        supported = [
            ("sigint", signal.SIGINT, 130),
            ("sigterm", signal.SIGTERM, 143),
        ]
        if hasattr(signal, "SIGBREAK"):
            supported.append(("sigbreak", signal.SIGBREAK, 149))

        for expected_category, number, expected_status in supported:
            with self.subTest(signal=expected_category):
                reports = []
                prior = signal.getsignal(number)
                cleanup_signal_sent = False
                cleanup_signal = (
                    signal.SIGTERM
                    if number != signal.SIGTERM
                    else signal.SIGINT
                )

                class Socket:
                    def __init__(self):
                        self.closed = False

                    def close(self):
                        self.closed = True

                    def fileno(self):
                        return -1 if self.closed else 10

                class Integration:
                    def matches_route(self, _path):
                        return False

                    def handle(self, **_request):
                        raise AssertionError("request_not_expected")

                class Runtime:
                    configuration = SimpleNamespace(
                        bind_host="127.0.0.1",
                        bind_port=8443,
                        public_origin="https://localhost:8443",
                    )
                    browser_integration = Integration()

                    def close(self):
                        nonlocal cleanup_signal_sent
                        if not cleanup_signal_sent:
                            cleanup_signal_sent = True
                            signal.raise_signal(cleanup_signal)
                        return None

                class TlsContext:
                    @staticmethod
                    def wrap_socket(*_args, **_kwargs):
                        raise AssertionError(
                            "no_handshake_expected_before_signal"
                        )

                class TlsScope:
                    def prepare_workspace(self):
                        return True

                    def build_context(self):
                        return TlsContext()

                    def close(self):
                        return True

                class Server:
                    def __init__(
                        self,
                        _address,
                        handler,
                        bind_and_activate,
                    ):
                        if (
                            handler
                            is not durable_google_login_app._UnpublishedRequestHandler
                            or bind_and_activate is not False
                        ):
                            raise AssertionError(
                                "server_must_start_unpublished"
                            )
                        self.socket = Socket()
                        self.RequestHandlerClass = handler

                    def set_shutdown_notification(self, requested):
                        self.requested = requested
                        return True

                    def set_tls_context(
                        self,
                        context,
                        *,
                        handshake_timeout,
                    ):
                        self.context = context
                        self.handshake_timeout = handshake_timeout
                        return True

                    def server_bind(self):
                        return None

                    def server_activate(self):
                        return None

                    def server_close(self):
                        self.socket.close()

                def checkpoint(category):
                    if category == "signals_installed":
                        signal.raise_signal(number)

                def observe(report):
                    reports.append(report)
                    raise RuntimeError(
                        "PRIVATE_SHUTDOWN_OBSERVER_FAILURE"
                    )

                with mock.patch("builtins.print"):
                    status = durable_google_login_app.main(
                        [
                            "--config",
                            str(ROOT / "unused-runtime.json"),
                        ],
                        _runtime_builder=lambda _path: Runtime(),
                        _server_factory=Server,
                        _tls_context_factory=TlsScope,
                        _checkpoint_observer=checkpoint,
                        _shutdown_result_observer=observe,
                    )
                self.assertEqual(status, expected_status)
                self.assertEqual(signal.getsignal(number), prior)
                self.assertEqual(len(reports), 1)
                report = reports[0]
                self.assertTrue(report.shutdown_requested)
                self.assertTrue(report.cleanup_complete)
                self.assertEqual(
                    report.signal_category,
                    expected_category,
                )
                self.assertTrue(cleanup_signal_sent)
                self.assertNotIn(
                    "unused-runtime",
                    repr(report),
                )

    def test_cli_boundary_maps_named_controls_without_traceback_or_canary(
        self,
    ):
        from scripts import durable_google_login_app

        controls = (
            (KeyboardInterrupt("PRIVATE_CLI_INTERRUPT"), 130),
            (SystemExit("PRIVATE_CLI_EXIT"), 2),
            (GeneratorExit("PRIVATE_CLI_GENERATOR"), 2),
        )
        for injected, expected_status in controls:
            with self.subTest(control=type(injected).__name__):
                error = io.StringIO()
                with (
                    mock.patch.object(
                        durable_google_login_app,
                        "main",
                        side_effect=injected,
                    ),
                    redirect_stderr(error),
                ):
                    self.assertEqual(
                        durable_google_login_app._main_cli(),
                        expected_status,
                    )
                self.assertEqual(error.getvalue(), "")
                self.assertNotIn("PRIVATE_CLI", repr(injected))

    def test_start_then_raise_request_thread_remains_owned_until_join(self):
        from scripts import durable_google_login_app

        entered = threading.Event()
        release = threading.Event()
        real_thread_type = threading.Thread

        class StartThenRaiseThread:
            def __init__(self, **kwargs):
                self._thread = real_thread_type(**kwargs)

            def start(self):
                self._thread.start()
                if not entered.wait(1):
                    raise AssertionError("request_thread_did_not_enter")
                raise RuntimeError("PRIVATE_START_AFTER_LAUNCH_FAILURE")

            def is_alive(self):
                return self._thread.is_alive()

            def join(self, timeout=None):
                return self._thread.join(timeout)

        server = durable_google_login_app._DrainingThreadingHTTPServer(
            ("127.0.0.1", 0),
            durable_google_login_app._UnpublishedRequestHandler,
            False,
        )
        request, peer = socket.socketpair()
        outcome = None
        try:
            outcome, _ = self.arm_server_admission(
                durable_google_login_app,
                server,
            )

            def process_request_thread(_request, _client_address):
                entered.set()
                release.wait(2)

            server.process_request_thread = process_request_thread
            with server._lifecycle_lock:
                server._accepted_sockets.add(request)
            with (
                mock.patch.object(
                    durable_google_login_app.threading,
                    "Thread",
                    StartThenRaiseThread,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "PRIVATE_START_AFTER_LAUNCH_FAILURE",
                ),
            ):
                server.process_request(
                    request,
                    ("127.0.0.1", 43210),
                )
            self.assertEqual(
                server.resource_counts()["request_threads"],
                1,
            )
            self.assertFalse(server.drain_request_threads(0.01))
            release.set()
            self.assertTrue(server.drain_request_threads(1))
            self.assertEqual(
                server.resource_counts()["request_threads"],
                0,
            )
        finally:
            release.set()
            if outcome is not None:
                self.disarm_server_admission(server, outcome)
            server.close_accepted_sockets()
            server.drain_request_threads(1)
            peer.close()
            server.server_close()

    def test_request_threads_self_deregister_after_every_completion_kind(self):
        from scripts import durable_google_login_app

        completion_cases = (
            ("normal", None),
            (
                "ordinary",
                RuntimeError("PRIVATE_REQUEST_COMPLETION_FAILURE"),
            ),
            (
                "keyboard_interrupt",
                KeyboardInterrupt("PRIVATE_REQUEST_COMPLETION_INTERRUPT"),
            ),
            (
                "system_exit",
                SystemExit("PRIVATE_REQUEST_COMPLETION_EXIT"),
            ),
            (
                "generator_exit",
                GeneratorExit("PRIVATE_REQUEST_COMPLETION_GENERATOR"),
            ),
        )
        for category, injected in completion_cases:
            with self.subTest(category=category):
                server = (
                    durable_google_login_app
                    ._DrainingThreadingHTTPServer(
                        ("127.0.0.1", 0),
                        durable_google_login_app
                        ._UnpublishedRequestHandler,
                        False,
                    )
                )
                request, peer = socket.socketpair()
                outcome = None
                completed = threading.Event()
                try:
                    outcome, _ = self.arm_server_admission(
                        durable_google_login_app,
                        server,
                    )

                    def complete_request(
                        _request,
                        _client_address,
                    ):
                        try:
                            if injected is not None:
                                raise injected
                        finally:
                            completed.set()

                    server.process_request_thread = complete_request
                    with server._lifecycle_lock:
                        server._accepted_sockets.add(request)
                    server.process_request(
                        request,
                        ("127.0.0.1", 43210),
                    )
                    self.assertTrue(completed.wait(1))
                    self.assertTrue(
                        self.wait_for_server_count(
                            server,
                            "request_threads",
                            0,
                            timeout=1,
                        )
                    )
                    self.assertEqual(
                        server.resource_counts()["accepted_sockets"],
                        0,
                    )
                    if isinstance(
                        injected,
                        (
                            KeyboardInterrupt,
                            SystemExit,
                            GeneratorExit,
                        ),
                    ):
                        self.assertIs(outcome.control, injected)
                        self.assertEqual(outcome.state, "failed")
                        self.assertTrue(outcome.done.is_set())
                        self.assertTrue(server._stopping)
                    else:
                        self.assertIsNone(outcome.control)
                        self.assertEqual(outcome.state, "serving")
                        self.assertFalse(outcome.done.is_set())
                    if injected is not None:
                        self.assertNotIn(
                            "PRIVATE_REQUEST_COMPLETION",
                            repr(injected),
                        )
                finally:
                    if outcome is not None:
                        self.disarm_server_admission(server, outcome)
                    server.close_accepted_sockets()
                    server.drain_request_threads(1)
                    peer.close()
                    server.server_close()

    def test_request_thread_self_deregistration_races_concurrent_drain(self):
        from scripts import durable_google_login_app

        entered = threading.Event()
        release = threading.Event()
        join_entered = threading.Event()
        real_thread_type = threading.Thread
        drain_results = []
        drain_failures = []

        class JoinObservedThread(real_thread_type):
            def join(self, timeout=None):
                join_entered.set()
                return super().join(timeout)

        server = durable_google_login_app._DrainingThreadingHTTPServer(
            ("127.0.0.1", 0),
            durable_google_login_app._UnpublishedRequestHandler,
            False,
        )
        request, peer = socket.socketpair()
        outcome = None
        drainer = None
        try:
            outcome, _ = self.arm_server_admission(
                durable_google_login_app,
                server,
            )

            def process_request_thread(_request, _client_address):
                entered.set()
                release.wait(2)

            server.process_request_thread = process_request_thread
            with server._lifecycle_lock:
                server._accepted_sockets.add(request)
            with mock.patch.object(
                durable_google_login_app.threading,
                "Thread",
                JoinObservedThread,
            ):
                server.process_request(
                    request,
                    ("127.0.0.1", 43210),
                )
            self.assertTrue(entered.wait(1))

            def drain():
                try:
                    drain_results.append(server.drain_request_threads(1))
                except BaseException as exc:
                    drain_failures.append(exc)

            drainer = real_thread_type(target=drain, daemon=False)
            drainer.start()
            self.assertTrue(join_entered.wait(1))
            release.set()
            drainer.join(2)
            self.assertFalse(drainer.is_alive())
            self.assertEqual(drain_failures, [])
            self.assertEqual(drain_results, [True])
            self.assertEqual(
                server.resource_counts()["request_threads"],
                0,
            )
            self.assertEqual(
                server.resource_counts()["accepted_sockets"],
                0,
            )
        finally:
            release.set()
            if drainer is not None:
                drainer.join(1)
            if outcome is not None:
                self.disarm_server_admission(server, outcome)
            server.close_accepted_sockets()
            server.drain_request_threads(1)
            peer.close()
            server.server_close()

    def test_native_start_then_raise_thread_self_deregisters_on_return(self):
        from scripts import durable_google_login_app

        entered = threading.Event()
        release = threading.Event()
        real_thread_type = threading.Thread

        class NativeStartThenRaiseThread(real_thread_type):
            def start(self):
                super().start()
                if not entered.wait(1):
                    raise AssertionError("request_thread_did_not_enter")
                raise RuntimeError("PRIVATE_NATIVE_START_FAILURE")

        server = durable_google_login_app._DrainingThreadingHTTPServer(
            ("127.0.0.1", 0),
            durable_google_login_app._UnpublishedRequestHandler,
            False,
        )
        request, peer = socket.socketpair()
        outcome = None
        try:
            outcome, _ = self.arm_server_admission(
                durable_google_login_app,
                server,
            )

            def process_request_thread(_request, _client_address):
                entered.set()
                release.wait(2)

            server.process_request_thread = process_request_thread
            with server._lifecycle_lock:
                server._accepted_sockets.add(request)
            with (
                mock.patch.object(
                    durable_google_login_app.threading,
                    "Thread",
                    NativeStartThenRaiseThread,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "PRIVATE_NATIVE_START_FAILURE",
                ),
            ):
                server.process_request(
                    request,
                    ("127.0.0.1", 43210),
                )
            with server._lifecycle_lock:
                tracked = tuple(server._request_threads)
            self.assertEqual(len(tracked), 1)
            self.assertIsInstance(
                tracked[0],
                NativeStartThenRaiseThread,
            )
            self.assertTrue(tracked[0].is_alive())
            release.set()
            self.assertTrue(
                self.wait_for_server_count(
                    server,
                    "request_threads",
                    0,
                    timeout=1,
                )
            )
            tracked[0].join(1)
            self.assertFalse(tracked[0].is_alive())
            self.assertTrue(server.drain_request_threads(0))
            self.assertEqual(
                server.resource_counts()["accepted_sockets"],
                0,
            )
        finally:
            release.set()
            if outcome is not None:
                self.disarm_server_admission(server, outcome)
            server.close_accepted_sockets()
            server.drain_request_threads(1)
            peer.close()
            server.server_close()

    def test_tls_wrap_does_not_block_concurrent_owned_socket_close(self):
        from scripts import durable_google_login_app

        entered = threading.Event()
        release = threading.Event()
        result = []

        class Socket:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 47

        class Context:
            @staticmethod
            def wrap_socket(
                request,
                *,
                server_side,
                do_handshake_on_connect,
            ):
                entered.set()
                release.wait(2)
                return request

        request = Socket()
        lease = durable_google_login_app._PendingTlsHandshake(request)
        self.assertTrue(lease.begin_handshake())

        def wrap():
            try:
                lease.wrap(Context())
            except BaseException as exc:
                result.append(exc)

        worker = threading.Thread(target=wrap, daemon=False)
        try:
            worker.start()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self.assertFalse(lease.close())
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(request.closed)
            self.assertTrue(worker.is_alive())
        finally:
            release.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], OSError)
        self.assertTrue(lease.terminal())

    def test_cancelled_wrap_retains_distinct_wrapped_socket_for_retry(self):
        from scripts import durable_google_login_app

        entered = threading.Event()
        release = threading.Event()
        result = []

        class RawSocket:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 101

        class WrappedSocket:
            def __init__(self):
                self.closed = False
                self.close_calls = 0

            def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError(
                        "PRIVATE_WRAPPED_CLOSE_FAILURE"
                    )
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 103

        wrapped = WrappedSocket()

        class Context:
            @staticmethod
            def wrap_socket(
                _request,
                *,
                server_side,
                do_handshake_on_connect,
            ):
                entered.set()
                release.wait(2)
                return wrapped

        raw = RawSocket()
        lease = durable_google_login_app._PendingTlsHandshake(raw)
        self.assertTrue(lease.begin_handshake())
        server = durable_google_login_app._DrainingThreadingHTTPServer(
            ("127.0.0.1", 0),
            durable_google_login_app._UnpublishedRequestHandler,
            False,
        )
        with server._lifecycle_lock:
            server._pending_handshakes.add(lease)

        def wrap():
            try:
                lease.wrap(Context())
            except BaseException as exc:
                result.append(exc)

        worker = threading.Thread(target=wrap, daemon=False)
        try:
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(server.close_pending_handshakes())
            self.assertTrue(raw.closed)
            self.assertEqual(
                server.resource_counts()["pending_handshakes"],
                1,
            )
            release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(result), 1)
            self.assertIsInstance(result[0], OSError)
            self.assertTrue(wrapped.closed)
            self.assertEqual(wrapped.close_calls, 2)
            self.assertTrue(lease.terminal())
            self.assertTrue(server.close_pending_handshakes())
            self.assertTrue(wrapped.closed)
            self.assertEqual(wrapped.close_calls, 2)
            self.assertEqual(
                server.resource_counts()["pending_handshakes"],
                0,
            )
        finally:
            release.set()
            worker.join(1)
            server.close_pending_handshakes()
            server.server_close()

    def test_cancelled_wrap_exception_becomes_terminal_after_retry(self):
        from scripts import durable_google_login_app

        entered = threading.Event()
        release = threading.Event()
        result = []

        class RawSocket:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 107

        class Context:
            @staticmethod
            def wrap_socket(
                _request,
                *,
                server_side,
                do_handshake_on_connect,
            ):
                entered.set()
                release.wait(2)
                raise OSError("PRIVATE_WRAP_CANCELLED")

        raw = RawSocket()
        lease = durable_google_login_app._PendingTlsHandshake(raw)
        self.assertTrue(lease.begin_handshake())
        server = durable_google_login_app._DrainingThreadingHTTPServer(
            ("127.0.0.1", 0),
            durable_google_login_app._UnpublishedRequestHandler,
            False,
        )
        with server._lifecycle_lock:
            server._pending_handshakes.add(lease)

        def wrap():
            try:
                lease.wrap(Context())
            except BaseException as exc:
                result.append(exc)

        worker = threading.Thread(target=wrap, daemon=False)
        try:
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(server.close_pending_handshakes())
            self.assertTrue(raw.closed)
            release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(result), 1)
            self.assertIsInstance(result[0], OSError)
            self.assertTrue(server.close_pending_handshakes())
            self.assertTrue(lease.terminal())
            self.assertEqual(
                server.resource_counts()["pending_handshakes"],
                0,
            )
        finally:
            release.set()
            worker.join(1)
            server.close_pending_handshakes()
            server.server_close()

    def test_unresolved_cleanup_control_blocks_route_and_runtime_dependents(
        self,
    ):
        from wahojobs.durable_google_login_runtime import _CleanupCoordinator

        events = []
        drain_calls = 0
        coordinator = _CleanupCoordinator()
        coordinator.own(
            "browser_integration",
            object(),
            lambda _resource: events.append("runtime_close") or True,
            dependencies=("request_threads",),
        )

        def drain(_resource):
            nonlocal drain_calls
            drain_calls += 1
            events.append("request_drain")
            if drain_calls == 1:
                raise KeyboardInterrupt(
                    "PRIVATE_REQUEST_DRAIN_INTERRUPT"
                )
            return True

        coordinator.own(
            "request_threads",
            object(),
            drain,
            probe=lambda _resource: False,
        )
        injected = None
        try:
            coordinator.cleanup()
        except KeyboardInterrupt as exc:
            injected = exc
        self.assertIsNotNone(injected)
        self.assertEqual(events, ["request_drain"])
        self.assertNotIn(
            "PRIVATE_REQUEST_DRAIN_INTERRUPT",
            repr(injected),
        )
        first = coordinator.snapshot()
        self.assertIn("request_threads", first.unresolved_resources)
        self.assertIn(
            "browser_integration",
            first.unresolved_resources,
        )

        second = coordinator.cleanup()
        self.assertTrue(second.cleanup_complete)
        self.assertEqual(
            events,
            ["request_drain", "request_drain", "runtime_close"],
        )

    def test_signal_notification_is_lock_free_first_wins_and_subprocess_safe(
        self,
    ):
        from scripts import durable_google_login_app

        state = durable_google_login_app._SignalShutdownState()
        state._lock.acquire()
        worker = threading.Thread(
            target=state._handle,
            args=(signal.SIGINT, None),
        )
        try:
            worker.start()
            worker.join(1)
            self.assertFalse(worker.is_alive())
        finally:
            state._lock.release()
            worker.join(1)
        state._handle(signal.SIGTERM, None)
        self.assertTrue(state.event.is_set())
        self.assertEqual(state.category, "sigint")

        source = """
import signal
from scripts.durable_google_login_app import _SignalShutdownState
state = _SignalShutdownState()
state.install()
signal.raise_signal(signal.SIGTERM)
print(state.category, state.requested)
state.restore()
"""
        completed = self.run_python(source)
        self.assertEqual(completed.stdout.strip(), "sigterm True")

    def test_reentrant_signal_notification_preserves_first_arrival_and_status(
        self,
    ):
        from scripts import durable_google_login_app

        supported = [
            ("sigint", signal.SIGINT, 130),
            ("sigterm", signal.SIGTERM, 143),
        ]
        if hasattr(signal, "SIGBREAK"):
            supported.append(("sigbreak", signal.SIGBREAK, 149))
        original_signal_category = (
            durable_google_login_app._signal_category
        )

        for (
            first_category,
            first_number,
            first_status,
        ) in supported:
            for (
                second_category,
                second_number,
                _second_status,
            ) in supported:
                with self.subTest(
                    first=first_category,
                    reentrant=second_category,
                ):
                    state = (
                        durable_google_login_app
                        ._SignalShutdownState()
                    )
                    reentered = False

                    def reentrant_category(number):
                        nonlocal reentered
                        if not reentered:
                            reentered = True
                            state._handle(second_number, None)
                        return original_signal_category(number)

                    with mock.patch.object(
                        durable_google_login_app,
                        "_signal_category",
                        side_effect=reentrant_category,
                    ):
                        state._handle(first_number, None)
                    self.assertTrue(reentered)
                    self.assertTrue(state.requested)
                    self.assertTrue(state.event.is_set())
                    self.assertEqual(state.category, first_category)
                    self.assertEqual(
                        durable_google_login_app
                        ._SIGNAL_EXIT_STATUS[state.category],
                        first_status,
                    )

        for category, number, expected_status in supported:
            with self.subTest(repeated=category):
                state = (
                    durable_google_login_app
                    ._SignalShutdownState()
                )
                state._handle(number, None)
                state._handle(number, None)
                self.assertEqual(state.category, category)
                self.assertEqual(
                    durable_google_login_app
                    ._SIGNAL_EXIT_STATUS[state.category],
                    expected_status,
                )

            with self.subTest(unsupported_after=category):
                state = (
                    durable_google_login_app
                    ._SignalShutdownState()
                )
                state._handle(number, None)
                state._handle(0, None)
                self.assertTrue(state.requested)
                self.assertEqual(state.category, category)
                self.assertEqual(
                    durable_google_login_app
                    ._SIGNAL_EXIT_STATUS[state.category],
                    expected_status,
                )

    def test_real_subprocess_signal_path_removes_tls_workspace(self):
        before = {
            path.resolve()
            for path in Path(tempfile.gettempdir()).glob(
                "wahojobs-durable-login-tls-*"
            )
        }
        with temporary_browser_login_state() as state:
            source = f"""
import signal
from scripts.durable_google_login_app import main
def checkpoint(category):
    if category == "ready":
        signal.raise_signal(signal.SIGTERM)
status = main(
    ["--config", {str(state.configuration_path)!r}],
    _checkpoint_observer=checkpoint,
)
print("EXIT", status)
"""
            completed = self.run_python(source)
        after = {
            path.resolve()
            for path in Path(tempfile.gettempdir()).glob(
                "wahojobs-durable-login-tls-*"
            )
        }
        self.assertEqual(after, before)
        self.assertIn("Stopped durable Google login.", completed.stdout)
        self.assertIn("EXIT 143", completed.stdout)
        self.assertEqual(completed.stderr, "")

    def test_real_pre_ready_signal_emits_no_ready_marker_and_removes_tls(
        self,
    ):
        before = {
            path.resolve()
            for path in Path(tempfile.gettempdir()).glob(
                "wahojobs-durable-login-tls-*"
            )
        }
        with temporary_browser_login_state() as state:
            source = f"""
import signal
from scripts.durable_google_login_app import main
def checkpoint(category):
    if category == "signals_installed":
        signal.raise_signal(signal.SIGTERM)
def observe(report):
    print(
        "REPORT",
        report.ready_state_reached,
        report.cleanup_complete,
        report.signal_category,
    )
status = main(
    ["--config", {str(state.configuration_path)!r}],
    _checkpoint_observer=checkpoint,
    _shutdown_result_observer=observe,
)
print("EXIT", status)
"""
            completed = self.run_python(source)
        after = {
            path.resolve()
            for path in Path(tempfile.gettempdir()).glob(
                "wahojobs-durable-login-tls-*"
            )
        }
        self.assertEqual(after, before)
        self.assertIn("REPORT False True sigterm", completed.stdout)
        self.assertIn("EXIT 143", completed.stdout)
        self.assertIn("Stopped durable Google login.", completed.stdout)
        self.assertNotIn("Wahojobs durable Google login", completed.stdout)
        self.assertNotIn("Open:", completed.stdout)
        self.assertNotIn("Press Ctrl+C", completed.stdout)
        self.assertEqual(completed.stderr, "")

    def test_real_signal_with_raw_stalled_tls_client_closes_every_resource(
        self,
    ):
        before = {
            path.resolve()
            for path in Path(tempfile.gettempdir()).glob(
                "wahojobs-durable-login-tls-*"
            )
        }
        with temporary_browser_login_state() as state:
            source = f"""
import signal
import socket
import time
from scripts import durable_google_login_app as app
servers = []
clients = []
def server_factory(*args, **kwargs):
    server = app._DrainingThreadingHTTPServer(*args, **kwargs)
    servers.append(server)
    return server
def checkpoint(category):
    if category != "ready":
        return
    client = socket.create_connection(
        servers[0].server_address,
        timeout=2,
    )
    clients.append(client)
    deadline = time.monotonic() + 2
    while (
        servers[0].resource_counts()["pending_handshakes"] != 1
        and time.monotonic() < deadline
    ):
        time.sleep(0.005)
    print(
        "PENDING",
        servers[0].resource_counts()["pending_handshakes"],
    )
    signal.raise_signal(signal.SIGTERM)
def observe(report):
    print(
        "REPORT",
        report.ready_state_reached,
        report.cleanup_complete,
        report.signal_category,
    )
status = app.main(
    ["--config", {str(state.configuration_path)!r}],
    _server_factory=server_factory,
    _checkpoint_observer=checkpoint,
    _shutdown_result_observer=observe,
)
for client in clients:
    client.close()
counts = servers[0].resource_counts()
print(
    "COUNTS",
    counts["listener"],
    counts["accepted_sockets"],
    counts["pending_handshakes"],
    counts["request_threads"],
    counts["serve_threads"],
    counts["route_integrations"],
)
print("EXIT", status)
"""
            completed = self.run_python(source)
        after = {
            path.resolve()
            for path in Path(tempfile.gettempdir()).glob(
                "wahojobs-durable-login-tls-*"
            )
        }
        self.assertEqual(after, before)
        self.assertIn("PENDING 1", completed.stdout)
        self.assertIn("REPORT True True sigterm", completed.stdout)
        self.assertIn("COUNTS 0 0 0 0 0 0", completed.stdout)
        self.assertIn("EXIT 143", completed.stdout)
        self.assertEqual(completed.stderr, "")

    def test_partial_signal_installation_restoration_is_retryable(self):
        from scripts import durable_google_login_app

        state = durable_google_login_app._SignalShutdownState()
        calls = 0
        supported_count = 3 if hasattr(signal, "SIGBREAK") else 2

        def fake_signal(_number, _handler):
            nonlocal calls
            calls += 1
            if calls in {2, 3}:
                raise RuntimeError("PRIVATE_SIGNAL_INSTALL_FAILURE")
            return None

        with (
            mock.patch.object(
                durable_google_login_app.signal,
                "getsignal",
                return_value=signal.SIG_DFL,
            ),
            mock.patch.object(
                durable_google_login_app.signal,
                "signal",
                side_effect=fake_signal,
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "signal_installation_failed",
            ):
                state.install()
            self.assertTrue(state._installed)
            self.assertTrue(state.restore())
        self.assertFalse(state._installed)
        self.assertEqual(state._previous, ())
        self.assertGreaterEqual(calls, supported_count + 2)

    def test_tls_workspace_is_owned_before_preparation_failure(self):
        from scripts import durable_google_login_app

        close_calls = 0

        class TlsScope:
            def prepare_workspace(self):
                raise RuntimeError("PRIVATE_TLS_PREPARE_FAILURE")

            def close(self):
                nonlocal close_calls
                close_calls += 1
                if close_calls == 1:
                    raise RuntimeError("PRIVATE_TLS_CLOSE_FAILURE")
                return True

        with temporary_browser_login_state() as state:
            with mock.patch("builtins.print"):
                status = durable_google_login_app.main(
                    ["--config", str(state.configuration_path)],
                    _tls_context_factory=TlsScope,
                )
        self.assertEqual(status, 2)
        self.assertEqual(close_calls, 2)

    def test_constructed_server_is_owned_before_socket_inspection(self):
        from scripts import durable_google_login_app

        constructed = []

        class Server:
            def __init__(self):
                self.close_calls = 0
                constructed.append(self)

            @property
            def socket(self):
                raise RuntimeError("PRIVATE_SOCKET_INSPECTION_FAILURE")

            def server_close(self):
                self.close_calls += 1

        ownership = durable_google_login_app._ServerOwnership()
        with self.assertRaisesRegex(
            RuntimeError,
            "PRIVATE_SOCKET_INSPECTION_FAILURE",
        ):
            durable_google_login_app._construct_owned_server(
                lambda *_args: Server(),
                ("127.0.0.1", 0),
                object,
                ownership,
            )
        self.assertEqual(len(constructed), 1)
        self.assertTrue(ownership.owns(constructed[0]))
        self.assertTrue(ownership.close_high_level())
        self.assertTrue(ownership.close_listener())
        self.assertEqual(constructed[0].close_calls, 1)
        self.assertTrue(ownership.high_level_terminal())
        self.assertTrue(ownership.listener_terminal())

    def test_constructor_failure_retains_listener_after_local_close_failure(
        self,
    ):
        from scripts import durable_google_login_app

        class Listener:
            def __init__(self):
                self.closed = False
                self.close_calls = 0

            def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError("PRIVATE_LOCAL_CLOSE_FAILURE")
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 97

        listener = Listener()

        def fail_after_socket(_owner, server, *_args, **_kwargs):
            server.socket = listener
            raise RuntimeError("PRIVATE_SUPER_CONSTRUCTION_FAILURE")

        ownership = durable_google_login_app._ServerOwnership()
        with (
            mock.patch.object(
                durable_google_login_app._ServerOwnership,
                "materialize_listener",
                fail_after_socket,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "PRIVATE_SUPER_CONSTRUCTION_FAILURE",
            ),
        ):
            durable_google_login_app._construct_owned_server(
                durable_google_login_app._DrainingThreadingHTTPServer,
                ("127.0.0.1", 0),
                durable_google_login_app._UnpublishedRequestHandler,
                ownership,
            )
        self.assertEqual(listener.close_calls, 1)
        self.assertFalse(listener.closed)
        self.assertTrue(ownership.close_high_level())
        self.assertTrue(ownership.close_listener())
        self.assertEqual(listener.close_calls, 2)
        self.assertTrue(listener.closed)
        self.assertTrue(ownership.high_level_terminal())
        self.assertTrue(ownership.listener_terminal())

    def test_constructed_server_is_owned_before_shutdown_hook_failure(self):
        from scripts import durable_google_login_app

        reports = []
        servers = []

        class Integration:
            def matches_route(self, _path):
                return False

            def handle(self, **_request):
                raise AssertionError("request_not_expected")

        class Runtime:
            configuration = SimpleNamespace(
                bind_host="127.0.0.1",
                bind_port=8443,
                public_origin="https://localhost:8443",
            )
            browser_integration = Integration()

            def close(self):
                return True

        class TlsScope:
            def prepare_workspace(self):
                return True

            def close(self):
                return True

        class Listener:
            def __init__(self):
                self.closed = False
                self.close_calls = 0

            def close(self):
                if not self.closed:
                    self.close_calls += 1
                    self.closed = True

            def fileno(self):
                return -1 if self.closed else 41

        class Server:
            def __init__(self, _address, _handler, bind_and_activate):
                if bind_and_activate is not False:
                    raise AssertionError("server_must_start_inactive")
                self.socket = Listener()
                self.server_close_calls = 0
                servers.append(self)

            def set_shutdown_notification(self, _requested):
                raise RuntimeError("PRIVATE_SHUTDOWN_HOOK_FAILURE")

            def server_close(self):
                self.server_close_calls += 1
                self.socket.close()

        with mock.patch("builtins.print"):
            status = durable_google_login_app.main(
                ["--config", str(ROOT / "unused-runtime.json")],
                _runtime_builder=lambda _path: Runtime(),
                _server_factory=Server,
                _tls_context_factory=TlsScope,
                _shutdown_result_observer=reports.append,
            )

        self.assertEqual(status, 2)
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].server_close_calls, 1)
        self.assertTrue(servers[0].socket.closed)
        self.assertEqual(servers[0].socket.close_calls, 1)
        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0].cleanup_complete)
        self.assertFalse(reports[0].ready_state_reached)
        self.assertIn("inactive_server", reports[0].resources_closed)
        self.assertIn("listener_socket", reports[0].resources_closed)

    def test_signal_hook_failure_cleans_owned_server_and_listener(self):
        from scripts import durable_google_login_app

        reports = []
        servers = []

        class Integration:
            def matches_route(self, _path):
                return False

            def handle(self, **_request):
                raise AssertionError("request_not_expected")

        class Runtime:
            configuration = SimpleNamespace(
                bind_host="127.0.0.1",
                bind_port=8443,
                public_origin="https://localhost:8443",
            )
            browser_integration = Integration()

            def close(self):
                return True

        class TlsContext:
            @staticmethod
            def wrap_socket(*_args, **_kwargs):
                raise AssertionError("handshake_not_expected")

        class TlsScope:
            def prepare_workspace(self):
                return True

            def build_context(self):
                return TlsContext()

            def close(self):
                return True

        def server_factory(*args, **kwargs):
            server = (
                durable_google_login_app
                ._DrainingThreadingHTTPServer(*args, **kwargs)
            )
            servers.append(server)
            return server

        with (
            mock.patch.object(
                durable_google_login_app._SignalShutdownState,
                "install",
                side_effect=RuntimeError(
                    "PRIVATE_SIGNAL_HOOK_FAILURE"
                ),
            ),
            mock.patch("builtins.print"),
        ):
            status = durable_google_login_app.main(
                ["--config", str(ROOT / "unused-runtime.json")],
                _runtime_builder=lambda _path: Runtime(),
                _server_factory=server_factory,
                _tls_context_factory=TlsScope,
                _shutdown_result_observer=reports.append,
            )

        self.assertEqual(status, 2)
        self.assertEqual(len(servers), 1)
        self.assertEqual(
            servers[0].resource_counts(),
            {
                "listener": 0,
                "accepted_sockets": 0,
                "pending_handshakes": 0,
                "request_threads": 0,
                "serve_threads": 0,
                "route_integrations": 0,
            },
        )
        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0].cleanup_complete)
        self.assertFalse(reports[0].ready_state_reached)

    def test_bind_and_activation_failures_close_owned_listener_without_ready(
        self,
    ):
        from scripts import durable_google_login_app

        class Integration:
            def matches_route(self, _path):
                return False

            def handle(self, **_request):
                raise AssertionError("request_not_expected")

        class Runtime:
            configuration = SimpleNamespace(
                bind_host="127.0.0.1",
                bind_port=0,
                public_origin="https://localhost:8443",
            )
            browser_integration = Integration()

            def close(self):
                return True

        class TlsContext:
            @staticmethod
            def wrap_socket(*_args, **_kwargs):
                raise AssertionError("handshake_not_expected")

        class TlsScope:
            def prepare_workspace(self):
                return True

            def build_context(self):
                return TlsContext()

            def close(self):
                return True

        for failure_position in ("bind", "activate"):
            with self.subTest(failure_position=failure_position):
                reports = []
                servers = []

                class FailingServer(
                    durable_google_login_app
                    ._DrainingThreadingHTTPServer
                ):
                    def server_bind(self):
                        super().server_bind()
                        if failure_position == "bind":
                            raise RuntimeError(
                                "PRIVATE_BIND_FAILURE"
                            )

                    def server_activate(self):
                        super().server_activate()
                        if failure_position == "activate":
                            raise RuntimeError(
                                "PRIVATE_ACTIVATION_FAILURE"
                            )

                def server_factory(*args, **kwargs):
                    server = FailingServer(*args, **kwargs)
                    servers.append(server)
                    return server

                with mock.patch("builtins.print"):
                    status = durable_google_login_app.main(
                        [
                            "--config",
                            str(ROOT / "unused-runtime.json"),
                        ],
                        _runtime_builder=lambda _path: Runtime(),
                        _server_factory=server_factory,
                        _tls_context_factory=TlsScope,
                        _shutdown_result_observer=reports.append,
                    )
                self.assertEqual(status, 2)
                self.assertEqual(len(servers), 1)
                self.assertEqual(len(reports), 1)
                self.assertFalse(reports[0].ready_state_reached)
                self.assertTrue(reports[0].cleanup_complete)
                self.assertEqual(
                    servers[0].resource_counts(),
                    {
                        "listener": 0,
                        "accepted_sockets": 0,
                        "pending_handshakes": 0,
                        "request_threads": 0,
                        "serve_threads": 0,
                        "route_integrations": 0,
                    },
                )

    def test_launcher_startup_barrier_timeout_fails_closed_without_ready(
        self,
    ):
        from scripts import durable_google_login_app

        reports = []
        servers = []

        class Integration:
            def matches_route(self, _path):
                return False

            def handle(self, **_request):
                raise AssertionError("request_not_expected")

        class Runtime:
            configuration = SimpleNamespace(
                bind_host="127.0.0.1",
                bind_port=0,
                public_origin="https://localhost:8443",
            )
            browser_integration = Integration()

            def close(self):
                return True

        class TlsContext:
            @staticmethod
            def wrap_socket(*_args, **_kwargs):
                raise AssertionError("handshake_not_expected")

        class TlsScope:
            def prepare_workspace(self):
                return True

            def build_context(self):
                return TlsContext()

            def close(self):
                return True

        class NoCheckpointServer(
            durable_google_login_app._DrainingThreadingHTTPServer
        ):
            def service_actions(self):
                return None

        def server_factory(*args, **kwargs):
            server = NoCheckpointServer(*args, **kwargs)
            servers.append(server)
            return server

        with (
            mock.patch.object(
                durable_google_login_app,
                "_SERVE_STARTUP_SECONDS",
                0.03,
            ),
            mock.patch("builtins.print"),
        ):
            status = durable_google_login_app.main(
                ["--config", str(ROOT / "unused-runtime.json")],
                _runtime_builder=lambda _path: Runtime(),
                _server_factory=server_factory,
                _tls_context_factory=TlsScope,
                _shutdown_result_observer=reports.append,
            )
        self.assertEqual(status, 2)
        self.assertEqual(len(reports), 1)
        self.assertFalse(reports[0].ready_state_reached)
        self.assertTrue(reports[0].cleanup_complete)
        self.assertEqual(len(servers), 1)
        self.assertEqual(
            servers[0].resource_counts(),
            {
                "listener": 0,
                "accepted_sockets": 0,
                "pending_handshakes": 0,
                "request_threads": 0,
                "serve_threads": 0,
                "route_integrations": 0,
            },
        )

    def test_shutdown_before_ready_prevents_checkpoint_and_marker(self):
        from scripts import durable_google_login_app

        entered = threading.Event()
        release = threading.Event()

        class ControlledServer(
            durable_google_login_app._DrainingThreadingHTTPServer
        ):
            def serve_forever(self, *args, **kwargs):
                entered.set()
                release.wait(1)
                return super().serve_forever(*args, **kwargs)

        server = ControlledServer(
            ("127.0.0.1", 0),
            durable_google_login_app._UnpublishedRequestHandler,
            False,
        )
        outcome = durable_google_login_app._ServeOutcome()
        signal_state = durable_google_login_app._SignalShutdownState()
        serve_thread = None
        try:
            server.publish_handler(type("NoRequestHandler", (), {}))
            server.server_bind()
            server.server_activate()
            server.set_serve_lifecycle(outcome, signal_state)
            self.assertTrue(outcome.begin_starting())
            serve_thread = threading.Thread(
                target=durable_google_login_app._serve_in_thread,
                args=(server, outcome),
                daemon=False,
            )
            serve_thread.start()
            self.assertTrue(entered.wait(1))
            self.assertTrue(server.begin_shutdown())
            self.assertEqual(outcome.state, "stopping")
            release.set()
            serve_thread.join(1)
            self.assertFalse(serve_thread.is_alive())
            self.assertEqual(outcome.state, "stopped")
            self.assertFalse(outcome.ready_state_reached)
            self.assertFalse(
                outcome.publish_serving_checkpoint(signal_state)
            )
            self.assertFalse(outcome.claim_ready(signal_state))
        finally:
            release.set()
            server.begin_shutdown()
            server.close_listener()
            server.server_close()
            if serve_thread is not None:
                serve_thread.join(1)

    def test_serve_exit_racing_ready_has_no_false_ready_outcome(self):
        from scripts import durable_google_login_app

        for _iteration in range(64):
            barrier = threading.Barrier(3)
            signal_state = (
                durable_google_login_app._SignalShutdownState()
            )
            outcome = durable_google_login_app._ServeOutcome()

            class RacingServer:
                def serve_forever(self, *, poll_interval):
                    self.poll_interval = poll_interval
                    outcome.publish_serving_checkpoint(signal_state)
                    barrier.wait()

            self.assertTrue(outcome.begin_starting())
            serve_thread = threading.Thread(
                target=durable_google_login_app._serve_in_thread,
                args=(RacingServer(), outcome),
            )
            claims = []

            def claim():
                barrier.wait()
                claims.append(outcome.claim_ready(signal_state))

            claim_thread = threading.Thread(target=claim)
            serve_thread.start()
            self.assertEqual(
                outcome.wait_for_startup(1, signal_state),
                "serving",
            )
            claim_thread.start()
            barrier.wait()
            serve_thread.join(1)
            claim_thread.join(1)
            self.assertFalse(serve_thread.is_alive())
            self.assertFalse(claim_thread.is_alive())
            self.assertEqual(outcome.state, "failed")
            self.assertEqual(outcome.ready_state_reached, claims[0])
            if not claims[0]:
                self.assertFalse(outcome.ready_state_reached)

    def test_ready_claim_is_one_shot_and_never_republishes_after_stop(
        self,
    ):
        from scripts import durable_google_login_app

        signal_state = durable_google_login_app._SignalShutdownState()
        outcome = durable_google_login_app._ServeOutcome()
        markers = []
        self.assertTrue(outcome.begin_starting())
        self.assertTrue(
            outcome.publish_serving_checkpoint(signal_state)
        )
        if outcome.claim_ready(signal_state):
            markers.append("ready")
        if outcome.claim_ready(signal_state):
            markers.append("ready")
        self.assertEqual(markers, ["ready"])
        self.assertTrue(outcome.request_stop())
        self.assertFalse(
            outcome.publish_serving_checkpoint(signal_state)
        )
        if outcome.claim_ready(signal_state):
            markers.append("ready")
        outcome.publish_success(signal_state)
        if outcome.claim_ready(signal_state):
            markers.append("ready")
        self.assertEqual(markers, ["ready"])
        self.assertEqual(outcome.state, "stopped")

    def test_serve_readiness_linearizes_checkpoint_signal_failure_and_stop(
        self,
    ):
        from scripts import durable_google_login_app

        signal_state = durable_google_login_app._SignalShutdownState()
        outcome = durable_google_login_app._ServeOutcome()
        self.assertEqual(outcome.state, "created")
        self.assertTrue(outcome.begin_starting())
        outcome.publish_thread_entry()
        self.assertTrue(outcome.started.is_set())
        self.assertFalse(outcome.claim_ready(signal_state))
        self.assertFalse(outcome.ready_state_reached)
        self.assertTrue(
            outcome.publish_serving_checkpoint(signal_state)
        )
        self.assertEqual(outcome.state, "serving")
        self.assertTrue(outcome.claim_ready(signal_state))
        self.assertTrue(outcome.ready_state_reached)
        self.assertTrue(outcome.request_stop())
        self.assertEqual(outcome.state, "stopping")
        self.assertFalse(outcome.claim_ready(signal_state))
        self.assertTrue(outcome.ready_state_reached)
        outcome.publish_success()
        self.assertEqual(outcome.state, "stopped")

        signal_state = durable_google_login_app._SignalShutdownState()
        signalled = durable_google_login_app._ServeOutcome()
        self.assertTrue(signalled.begin_starting())
        signal_state.request("sigterm")
        self.assertFalse(
            signalled.publish_serving_checkpoint(signal_state)
        )
        self.assertFalse(signalled.claim_ready(signal_state))
        self.assertEqual(signalled.state, "stopping")
        self.assertFalse(signalled.ready_state_reached)

        failed = durable_google_login_app._ServeOutcome()
        self.assertTrue(failed.begin_starting())
        failure = RuntimeError("PRIVATE_FIRST_LOOP_FAILURE")
        failed.publish_failure(failure)
        self.assertEqual(failed.state, "failed")
        self.assertFalse(
            failed.publish_serving_checkpoint(
                durable_google_login_app._SignalShutdownState()
            )
        )
        self.assertFalse(
            failed.claim_ready(
                durable_google_login_app._SignalShutdownState()
            )
        )
        self.assertFalse(failed.ready_state_reached)

        exited = durable_google_login_app._ServeOutcome()
        self.assertTrue(exited.begin_starting())
        self.assertTrue(
            exited.publish_serving_checkpoint(
                durable_google_login_app._SignalShutdownState()
            )
        )
        exited.publish_success()
        self.assertEqual(exited.state, "failed")
        self.assertFalse(
            exited.claim_ready(
                durable_google_login_app._SignalShutdownState()
            )
        )

        timed_out = durable_google_login_app._ServeOutcome()
        self.assertTrue(timed_out.begin_starting())
        self.assertEqual(
            timed_out.wait_for_startup(
                0.01,
                durable_google_login_app._SignalShutdownState(),
            ),
            "failed",
        )
        self.assertFalse(timed_out.ready_state_reached)

        signal_before_failure = (
            durable_google_login_app._SignalShutdownState()
        )
        failure_after_signal = durable_google_login_app._ServeOutcome()
        self.assertTrue(failure_after_signal.begin_starting())
        self.assertTrue(
            failure_after_signal.publish_serving_checkpoint(
                signal_before_failure
            )
        )
        signal_before_failure.request("sigterm")
        induced_control = SystemExit("PRIVATE_INDUCED_SERVE_EXIT")
        failure_after_signal.publish_failure(
            induced_control,
            signal_before_failure,
        )
        self.assertEqual(failure_after_signal.state, "stopped")
        self.assertFalse(failure_after_signal.failed)
        self.assertIsNone(failure_after_signal.control)

        signal_before_return = (
            durable_google_login_app._SignalShutdownState()
        )
        return_after_signal = durable_google_login_app._ServeOutcome()
        self.assertTrue(return_after_signal.begin_starting())
        self.assertTrue(
            return_after_signal.publish_serving_checkpoint(
                signal_before_return
            )
        )
        signal_before_return.request("sigint")
        return_after_signal.publish_success(signal_before_return)
        self.assertEqual(return_after_signal.state, "stopped")
        self.assertFalse(return_after_signal.failed)

    def test_signal_racing_ready_has_one_valid_linearization(self):
        from scripts import durable_google_login_app

        for _iteration in range(64):
            signal_state = durable_google_login_app._SignalShutdownState()
            outcome = durable_google_login_app._ServeOutcome()
            self.assertTrue(outcome.begin_starting())
            self.assertTrue(
                outcome.publish_serving_checkpoint(signal_state)
            )
            barrier = threading.Barrier(3)
            claims = []

            def claim():
                barrier.wait()
                claims.append(outcome.claim_ready(signal_state))

            def request():
                barrier.wait()
                signal_state.request("sigint")

            claim_thread = threading.Thread(target=claim)
            signal_thread = threading.Thread(target=request)
            claim_thread.start()
            signal_thread.start()
            barrier.wait()
            claim_thread.join(1)
            signal_thread.join(1)
            self.assertFalse(claim_thread.is_alive())
            self.assertFalse(signal_thread.is_alive())
            self.assertEqual(len(claims), 1)
            if claims[0]:
                self.assertTrue(outcome.ready_state_reached)
                self.assertEqual(outcome.state, "serving")
            else:
                self.assertFalse(outcome.ready_state_reached)
                self.assertEqual(outcome.state, "stopping")
            self.assertTrue(signal_state.requested)

    def test_real_serve_checkpoint_does_not_require_a_request(self):
        from scripts import durable_google_login_app

        entered = threading.Event()
        release = threading.Event()
        first_iteration_released = threading.Event()

        class ControlledServer(
            durable_google_login_app._DrainingThreadingHTTPServer
        ):
            def serve_forever(self, *args, **kwargs):
                entered.set()
                release.wait(1)
                return super().serve_forever(*args, **kwargs)

            def service_actions(self):
                super().service_actions()
                first_iteration_released.set()

        server = ControlledServer(
            ("127.0.0.1", 0),
            durable_google_login_app._UnpublishedRequestHandler,
            False,
        )
        outcome = durable_google_login_app._ServeOutcome()
        signal_state = durable_google_login_app._SignalShutdownState()
        serve_thread = None
        try:
            server.set_shutdown_notification(
                lambda: signal_state.requested
            )
            server.publish_handler(type("NoRequestHandler", (), {}))
            server.server_bind()
            server.server_activate()
            server.set_serve_lifecycle(outcome, signal_state)
            self.assertTrue(outcome.begin_starting())
            serve_thread = threading.Thread(
                target=durable_google_login_app._serve_in_thread,
                args=(server, outcome),
                daemon=False,
            )
            serve_thread.start()
            self.assertTrue(entered.wait(1))
            self.assertEqual(outcome.state, "starting")
            self.assertFalse(outcome.ready_state_reached)
            self.assertFalse(outcome.claim_ready(signal_state))
            with self.assertRaisesRegex(OSError, "server_not_ready"):
                server.get_request()

            release.set()
            self.assertEqual(
                outcome.wait_for_startup(1, signal_state),
                "serving",
            )
            self.assertFalse(first_iteration_released.is_set())
            self.assertTrue(server.claim_serving_readiness())
            self.assertTrue(first_iteration_released.wait(1))
            self.assertTrue(outcome.ready_state_reached)
            self.assertEqual(
                server.resource_counts()["serve_threads"],
                1,
            )
        finally:
            release.set()
            server.begin_shutdown()
            server.close_listener()
            server.server_close()
            if serve_thread is not None:
                serve_thread.join(1)
        self.assertFalse(serve_thread.is_alive())
        self.assertEqual(outcome.state, "stopped")

    def test_immediate_and_first_iteration_serve_failures_prevent_ready(self):
        from scripts import durable_google_login_app

        class ImmediateFailureServer:
            def serve_forever(self, *, poll_interval):
                self.poll_interval = poll_interval
                raise RuntimeError("PRIVATE_IMMEDIATE_SERVE_FAILURE")

        immediate = durable_google_login_app._ServeOutcome()
        self.assertTrue(immediate.begin_starting())
        durable_google_login_app._serve_in_thread(
            ImmediateFailureServer(),
            immediate,
        )
        self.assertEqual(immediate.state, "failed")
        self.assertFalse(immediate.ready_state_reached)
        self.assertTrue(immediate.done.is_set())

        class FirstIterationFailureServer(
            durable_google_login_app._DrainingThreadingHTTPServer
        ):
            def service_actions(self):
                raise RuntimeError("PRIVATE_FIRST_ITERATION_FAILURE")

        first = FirstIterationFailureServer(
            ("127.0.0.1", 0),
            durable_google_login_app._UnpublishedRequestHandler,
            False,
        )
        first_outcome = durable_google_login_app._ServeOutcome()
        signal_state = durable_google_login_app._SignalShutdownState()
        first_thread = None
        try:
            first.publish_handler(type("NoRequestHandler", (), {}))
            first.server_bind()
            first.server_activate()
            first.set_serve_lifecycle(first_outcome, signal_state)
            self.assertTrue(first_outcome.begin_starting())
            first_thread = threading.Thread(
                target=durable_google_login_app._serve_in_thread,
                args=(first, first_outcome),
                daemon=False,
            )
            first_thread.start()
            self.assertTrue(first_outcome.done.wait(1))
            self.assertEqual(first_outcome.state, "failed")
            self.assertFalse(first_outcome.ready_state_reached)
        finally:
            first.begin_shutdown()
            first.close_listener()
            first.server_close()
            if first_thread is not None:
                first_thread.join(1)
        self.assertFalse(first_thread.is_alive())

    def test_tls_handshake_failures_close_without_request_thread(self):
        from scripts import durable_google_login_app

        class HandshakeSocket:
            def __init__(self, failure):
                self.failure = failure
                self.closed = False
                self.timeout = None
                self.handshake_calls = 0

            def gettimeout(self):
                return self.timeout

            def settimeout(self, timeout):
                self.timeout = timeout

            def do_handshake(self):
                self.handshake_calls += 1
                raise self.failure

            def shutdown(self, _how):
                return None

            def close(self):
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 43

        class TlsContext:
            @staticmethod
            def wrap_socket(
                request,
                *,
                server_side,
                do_handshake_on_connect,
            ):
                self.assertTrue(server_side)
                self.assertFalse(do_handshake_on_connect)
                return request

        failures = (
            OSError("PRIVATE_MALFORMED_TLS_RECORD"),
            socket.timeout("PRIVATE_TLS_TIMEOUT"),
            RuntimeError("PRIVATE_TLS_HANDSHAKE_FAILURE"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                server = (
                    durable_google_login_app
                    ._DrainingThreadingHTTPServer(
                        ("127.0.0.1", 0),
                        durable_google_login_app
                        ._UnpublishedRequestHandler,
                        False,
                    )
                )
                request = HandshakeSocket(failure)
                outcome = None
                try:
                    server.set_tls_context(
                        TlsContext(),
                        handshake_timeout=0.05,
                    )
                    outcome, _signal_state = (
                        self.arm_server_admission(
                            durable_google_login_app,
                            server,
                        )
                    )
                    with mock.patch(
                        "socketserver.TCPServer.get_request",
                        return_value=(
                            request,
                            ("127.0.0.1", 43210),
                        ),
                    ):
                        with self.assertRaisesRegex(
                            OSError,
                            "tls_handshake_failed",
                        ):
                            server.get_request()
                    self.assertTrue(request.closed)
                    self.assertEqual(request.handshake_calls, 1)
                    self.assertEqual(
                        server.resource_counts()["pending_handshakes"],
                        0,
                    )
                    self.assertEqual(
                        server.resource_counts()["accepted_sockets"],
                        0,
                    )
                    self.assertEqual(
                        server.resource_counts()["request_threads"],
                        0,
                    )
                finally:
                    if outcome is not None:
                        self.disarm_server_admission(
                            server,
                            outcome,
                        )
                    server.server_close()

    def test_tls_wrap_failure_closes_owned_socket_without_request_thread(
        self,
    ):
        from scripts import durable_google_login_app

        class Request:
            def __init__(self):
                self.closed = False

            def gettimeout(self):
                return None

            def shutdown(self, _how):
                return None

            def close(self):
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 97

        class FailingTlsContext:
            @staticmethod
            def wrap_socket(
                _request,
                *,
                server_side,
                do_handshake_on_connect,
            ):
                self.assertTrue(server_side)
                self.assertFalse(do_handshake_on_connect)
                raise RuntimeError("PRIVATE_TLS_WRAP_FAILURE")

        server = durable_google_login_app._DrainingThreadingHTTPServer(
            ("127.0.0.1", 0),
            durable_google_login_app._UnpublishedRequestHandler,
            False,
        )
        request = Request()
        outcome = None
        try:
            server.set_tls_context(FailingTlsContext())
            outcome, _signal_state = self.arm_server_admission(
                durable_google_login_app,
                server,
            )
            with mock.patch(
                "socketserver.TCPServer.get_request",
                return_value=(
                    request,
                    ("127.0.0.1", 43216),
                ),
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "tls_handshake_failed",
                ):
                    server.get_request()
            self.assertTrue(request.closed)
            counts = server.resource_counts()
            self.assertEqual(counts["pending_handshakes"], 0)
            self.assertEqual(counts["accepted_sockets"], 0)
            self.assertEqual(counts["request_threads"], 0)
        finally:
            if outcome is not None:
                self.disarm_server_admission(server, outcome)
            server.server_close()

    def test_real_raw_tls_stall_expires_at_configured_timeout(self):
        from scripts import durable_google_login_app

        harness = self.start_real_tls_server(
            durable_google_login_app,
            handshake_timeout=0.2,
        )
        client = None
        try:
            started = time.monotonic()
            client = socket.create_connection(
                harness.server.server_address,
                timeout=1,
            )
            self.assertTrue(
                self.wait_for_server_count(
                    harness.server,
                    "pending_handshakes",
                    1,
                )
            )
            self.assertTrue(
                self.wait_for_server_count(
                    harness.server,
                    "pending_handshakes",
                    0,
                )
            )
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 0.1)
            self.assertLess(elapsed, 1.5)
            counts = harness.server.resource_counts()
            self.assertEqual(counts["accepted_sockets"], 0)
            self.assertEqual(counts["request_threads"], 0)
            self.assertTrue(harness.serve_thread.is_alive())
        finally:
            if client is not None:
                client.close()
            counts, thread_alive, tls_closed = (
                self.stop_real_tls_server(harness)
            )
        self.assertFalse(thread_alive)
        self.assertTrue(tls_closed)
        self.assertEqual(
            counts,
            {
                "listener": 0,
                "accepted_sockets": 0,
                "pending_handshakes": 0,
                "request_threads": 0,
                "serve_threads": 0,
                "route_integrations": 0,
            },
        )

    def test_real_partial_tls_record_times_out_without_request_thread(self):
        from scripts import durable_google_login_app

        harness = self.start_real_tls_server(
            durable_google_login_app,
            handshake_timeout=0.2,
        )
        client = None
        try:
            started = time.monotonic()
            client = socket.create_connection(
                harness.server.server_address,
                timeout=1,
            )
            client.sendall(
                b"\x16\x03\x03\x00\x10\x01\x00\x00"
            )
            self.assertTrue(
                self.wait_for_server_count(
                    harness.server,
                    "pending_handshakes",
                    1,
                )
            )
            self.assertTrue(
                self.wait_for_server_count(
                    harness.server,
                    "pending_handshakes",
                    0,
                )
            )
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 0.1)
            self.assertLess(elapsed, 1.5)
            self.assertEqual(
                harness.server.resource_counts()["request_threads"],
                0,
            )
            self.assertTrue(harness.serve_thread.is_alive())
        finally:
            if client is not None:
                client.close()
            counts, thread_alive, tls_closed = (
                self.stop_real_tls_server(harness)
            )
        self.assertFalse(thread_alive)
        self.assertTrue(tls_closed)
        self.assertEqual(counts["pending_handshakes"], 0)
        self.assertEqual(counts["accepted_sockets"], 0)

    def test_real_malformed_tls_handshake_closes_without_request_thread(
        self,
    ):
        from scripts import durable_google_login_app

        harness = self.start_real_tls_server(
            durable_google_login_app,
            handshake_timeout=0.5,
        )
        client = None
        try:
            client = socket.create_connection(
                harness.server.server_address,
                timeout=1,
            )
            client.sendall(b"GET /not-tls HTTP/1.0\r\n\r\n")
            client.settimeout(1)
            try:
                received = client.recv(1)
            except (ConnectionAbortedError, ConnectionResetError):
                received = b""
            self.assertEqual(received, b"")
            self.assertTrue(
                self.wait_for_server_count(
                    harness.server,
                    "pending_handshakes",
                    0,
                )
            )
            counts = harness.server.resource_counts()
            self.assertEqual(counts["accepted_sockets"], 0)
            self.assertEqual(counts["request_threads"], 0)
            self.assertTrue(harness.serve_thread.is_alive())
        finally:
            if client is not None:
                client.close()
            counts, thread_alive, tls_closed = (
                self.stop_real_tls_server(harness)
            )
        self.assertFalse(thread_alive)
        self.assertTrue(tls_closed)
        self.assertEqual(counts["pending_handshakes"], 0)

    def test_multiple_real_stalled_tls_clients_shutdown_bounded(self):
        from scripts import durable_google_login_app

        harness = self.start_real_tls_server(
            durable_google_login_app,
            handshake_timeout=1.0,
        )
        clients = []
        stopped = False
        try:
            clients.append(
                socket.create_connection(
                    harness.server.server_address,
                    timeout=1,
                )
            )
            self.assertTrue(
                self.wait_for_server_count(
                    harness.server,
                    "pending_handshakes",
                    1,
                )
            )
            for _index in range(5):
                clients.append(
                    socket.create_connection(
                        harness.server.server_address,
                        timeout=1,
                    )
                )
            counts = harness.server.resource_counts()
            self.assertEqual(counts["pending_handshakes"], 1)
            self.assertLessEqual(
                counts["accepted_sockets"],
                durable_google_login_app
                ._MAX_TRACKED_ACCEPTED_SOCKETS,
            )
            started = time.monotonic()
            final_counts, thread_alive, tls_closed = (
                self.stop_real_tls_server(harness)
            )
            stopped = True
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2)
            self.assertFalse(thread_alive)
            self.assertTrue(tls_closed)
            self.assertEqual(
                final_counts,
                {
                    "listener": 0,
                    "accepted_sockets": 0,
                    "pending_handshakes": 0,
                    "request_threads": 0,
                    "serve_threads": 0,
                    "route_integrations": 0,
                },
            )
        finally:
            for client in clients:
                client.close()
            if not stopped:
                self.stop_real_tls_server(harness)

    def test_no_accept_attempt_begins_after_shutdown(self):
        from scripts import durable_google_login_app

        class TlsContext:
            @staticmethod
            def wrap_socket(*_args, **_kwargs):
                raise AssertionError("handshake_not_expected")

        server = durable_google_login_app._DrainingThreadingHTTPServer(
            ("127.0.0.1", 0),
            durable_google_login_app._UnpublishedRequestHandler,
            False,
        )
        outcome = None
        try:
            server.set_tls_context(TlsContext())
            outcome, _signal_state = self.arm_server_admission(
                durable_google_login_app,
                server,
            )
            self.assertTrue(server.begin_shutdown())
            with mock.patch(
                "socketserver.TCPServer.get_request",
            ) as accept:
                with self.assertRaisesRegex(
                    OSError,
                    "server_not_ready",
                ):
                    server.get_request()
            accept.assert_not_called()
            self.assertEqual(
                server.resource_counts()["pending_handshakes"],
                0,
            )
        finally:
            if outcome is not None:
                self.disarm_server_admission(server, outcome)
            server.server_close()

    def test_shutdown_or_signal_closes_stalled_tls_handshake(self):
        from scripts import durable_google_login_app

        for category in (None, "sigterm"):
            with self.subTest(category=category):
                entered = threading.Event()
                released = threading.Event()
                result = []

                class StalledSocket:
                    def __init__(self):
                        self.closed = False
                        self.timeout = None

                    def gettimeout(self):
                        return self.timeout

                    def settimeout(self, timeout):
                        self.timeout = timeout

                    def do_handshake(self):
                        entered.set()
                        released.wait(1)
                        if self.closed:
                            raise OSError("closed_during_handshake")

                    def shutdown(self, _how):
                        released.set()

                    def close(self):
                        self.closed = True
                        released.set()

                    def fileno(self):
                        return -1 if self.closed else 47

                class TlsContext:
                    @staticmethod
                    def wrap_socket(
                        request,
                        *,
                        server_side,
                        do_handshake_on_connect,
                    ):
                        return request

                server = (
                    durable_google_login_app
                    ._DrainingThreadingHTTPServer(
                        ("127.0.0.1", 0),
                        durable_google_login_app
                        ._UnpublishedRequestHandler,
                        False,
                    )
                )
                request = StalledSocket()
                signal_state = (
                    durable_google_login_app._SignalShutdownState()
                )
                server.set_shutdown_notification(
                    lambda: signal_state.requested
                )
                server.set_tls_context(
                    TlsContext(),
                    handshake_timeout=0.05,
                )
                outcome, _ = self.arm_server_admission(
                    durable_google_login_app,
                    server,
                    signal_state=signal_state,
                )

                def accept():
                    try:
                        with server._lifecycle_lock:
                            server._serve_thread_ident = (
                                threading.get_ident()
                            )
                        with mock.patch(
                            "socketserver.TCPServer.get_request",
                            return_value=(
                                request,
                                ("127.0.0.1", 43211),
                            ),
                        ):
                            server.get_request()
                    except BaseException as exc:
                        result.append(exc)

                handshake_thread = threading.Thread(
                    target=accept,
                    daemon=False,
                )
                try:
                    handshake_thread.start()
                    self.assertTrue(entered.wait(1))
                    counts = server.resource_counts()
                    self.assertEqual(counts["pending_handshakes"], 1)
                    self.assertEqual(counts["accepted_sockets"], 1)
                    if category is not None:
                        signal_state.request(category)
                    server.begin_shutdown()
                    self.assertTrue(server.close_pending_handshakes())
                    handshake_thread.join(1)
                    self.assertFalse(handshake_thread.is_alive())
                    self.assertTrue(request.closed)
                    self.assertEqual(
                        server.resource_counts()["pending_handshakes"],
                        0,
                    )
                    self.assertEqual(
                        server.resource_counts()["request_threads"],
                        0,
                    )
                    self.assertEqual(len(result), 1)
                    self.assertIsInstance(result[0], OSError)
                    self.assertNotIn(
                        "closed_during_handshake",
                        repr(result[0]),
                    )
                finally:
                    released.set()
                    server.begin_shutdown()
                    server.close_pending_handshakes()
                    self.disarm_server_admission(server, outcome)
                    server.server_close()
                    handshake_thread.join(1)

    def test_handshake_close_failure_is_retained_and_retried(self):
        from scripts import durable_google_login_app

        class FlakySocket:
            def __init__(self):
                self.closed = False
                self.close_calls = 0
                self.timeout = None

            def gettimeout(self):
                return self.timeout

            def settimeout(self, timeout):
                self.timeout = timeout

            def do_handshake(self):
                raise OSError("PRIVATE_BAD_TLS")

            def shutdown(self, _how):
                return None

            def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError("PRIVATE_SOCKET_CLOSE_FAILURE")
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 53

        class TlsContext:
            @staticmethod
            def wrap_socket(
                request,
                *,
                server_side,
                do_handshake_on_connect,
            ):
                return request

        server = durable_google_login_app._DrainingThreadingHTTPServer(
            ("127.0.0.1", 0),
            durable_google_login_app._UnpublishedRequestHandler,
            False,
        )
        request = FlakySocket()
        outcome = None
        try:
            server.set_tls_context(
                TlsContext(),
                handshake_timeout=0.05,
            )
            outcome, _signal_state = self.arm_server_admission(
                durable_google_login_app,
                server,
            )
            with mock.patch(
                "socketserver.TCPServer.get_request",
                return_value=(
                    request,
                    ("127.0.0.1", 43212),
                ),
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "tls_handshake_failed",
                ):
                    server.get_request()
            self.assertFalse(request.closed)
            self.assertEqual(
                server.resource_counts()["pending_handshakes"],
                1,
            )
            self.assertTrue(server.close_pending_handshakes())
            self.assertTrue(request.closed)
            self.assertEqual(request.close_calls, 2)
            self.assertEqual(
                server.resource_counts()["pending_handshakes"],
                0,
            )
            self.assertTrue(server.close_pending_handshakes())
            self.assertEqual(request.close_calls, 2)
        finally:
            if outcome is not None:
                self.disarm_server_admission(server, outcome)
            server.server_close()

    def test_successful_handshake_racing_shutdown_is_not_transferred(self):
        from scripts import durable_google_login_app

        class RacingSocket:
            def __init__(self):
                self.closed = False
                self.timeout = None

            def gettimeout(self):
                return self.timeout

            def settimeout(self, timeout):
                self.timeout = timeout

            def do_handshake(self):
                server.begin_shutdown()

            def shutdown(self, _how):
                return None

            def close(self):
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 59

        class TlsContext:
            @staticmethod
            def wrap_socket(
                request,
                *,
                server_side,
                do_handshake_on_connect,
            ):
                return request

        server = durable_google_login_app._DrainingThreadingHTTPServer(
            ("127.0.0.1", 0),
            durable_google_login_app._UnpublishedRequestHandler,
            False,
        )
        request = RacingSocket()
        outcome = None
        try:
            server.set_tls_context(TlsContext())
            outcome, _signal_state = self.arm_server_admission(
                durable_google_login_app,
                server,
            )
            with mock.patch(
                "socketserver.TCPServer.get_request",
                return_value=(
                    request,
                    ("127.0.0.1", 43213),
                ),
            ):
                with self.assertRaises(OSError):
                    server.get_request()
            self.assertTrue(request.closed)
            counts = server.resource_counts()
            self.assertEqual(counts["pending_handshakes"], 0)
            self.assertEqual(counts["accepted_sockets"], 0)
            self.assertEqual(counts["request_threads"], 0)
        finally:
            if outcome is not None:
                self.disarm_server_admission(server, outcome)
            server.server_close()

    def test_pending_registration_failure_retains_local_socket_owner(self):
        from scripts import durable_google_login_app

        class Request:
            def __init__(self):
                self.closed = False
                self.close_calls = 0

            def shutdown(self, _how):
                raise OSError("already_shutting_down")

            def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError("PRIVATE_FIRST_CLOSE_FAILURE")
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 89

        class FailingPendingSet(set):
            def add(self, _value):
                raise MemoryError("PRIVATE_PENDING_SET_ALLOCATION")

        class TlsContext:
            @staticmethod
            def wrap_socket(*_args, **_kwargs):
                raise AssertionError("handshake_must_not_start")

        server = durable_google_login_app._DrainingThreadingHTTPServer(
            ("127.0.0.1", 0),
            durable_google_login_app._UnpublishedRequestHandler,
            False,
        )
        request = Request()
        outcome = None
        try:
            server.set_tls_context(TlsContext())
            outcome, _signal_state = self.arm_server_admission(
                durable_google_login_app,
                server,
            )
            server._pending_handshakes = FailingPendingSet()
            with mock.patch(
                "socketserver.TCPServer.get_request",
                return_value=(
                    request,
                    ("127.0.0.1", 43215),
                ),
            ) as accept:
                with self.assertRaisesRegex(
                    OSError,
                    "accepted_socket_ownership_failed",
                ):
                    server.get_request()
            accept.assert_not_called()
            self.assertFalse(request.closed)
            self.assertEqual(request.close_calls, 0)
            self.assertEqual(
                server.resource_counts()["pending_handshakes"],
                0,
            )
            self.assertTrue(server.close_pending_handshakes())
            self.assertFalse(request.closed)
            self.assertEqual(request.close_calls, 0)
            self.assertEqual(
                server.resource_counts()["pending_handshakes"],
                0,
            )
        finally:
            if outcome is not None:
                self.disarm_server_admission(server, outcome)
            server.server_close()

    def test_post_accept_registration_failure_retains_raw_socket_owner(self):
        from scripts import durable_google_login_app

        class Request:
            def __init__(self):
                self.closed = False
                self.close_calls = 0

            def shutdown(self, _how):
                raise OSError("already_shutting_down")

            def close(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError("PRIVATE_FIRST_CLOSE_FAILURE")
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 93

        class RegistrationFailureLock:
            def __init__(self):
                self._lock = threading.Lock()
                self.entries = 0

            def __enter__(self):
                self.entries += 1
                if self.entries == 3:
                    raise RuntimeError(
                        "PRIVATE_REGISTRATION_LOCK_FAILURE"
                    )
                self._lock.acquire()
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                self._lock.release()
                return False

        class TlsContext:
            @staticmethod
            def wrap_socket(*_args, **_kwargs):
                raise AssertionError("handshake_must_not_start")

        server = durable_google_login_app._DrainingThreadingHTTPServer(
            ("127.0.0.1", 0),
            durable_google_login_app._UnpublishedRequestHandler,
            False,
        )
        request = Request()
        outcome = None
        real_lock = None
        try:
            server.set_tls_context(TlsContext())
            outcome, _signal_state = self.arm_server_admission(
                durable_google_login_app,
                server,
            )
            real_lock = server._lifecycle_lock
            server._lifecycle_lock = RegistrationFailureLock()
            with mock.patch(
                "socketserver.TCPServer.get_request",
                return_value=(
                    request,
                    ("127.0.0.1", 43216),
                ),
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "tls_handshake_failed",
                ):
                    server.get_request()
            server._lifecycle_lock = real_lock
            real_lock = None
            self.assertFalse(request.closed)
            self.assertEqual(request.close_calls, 1)
            self.assertEqual(
                server.resource_counts()["pending_handshakes"],
                1,
            )
            self.assertTrue(server.close_pending_handshakes())
            self.assertTrue(request.closed)
            self.assertEqual(request.close_calls, 2)
        finally:
            if real_lock is not None:
                server._lifecycle_lock = real_lock
            if outcome is not None:
                self.disarm_server_admission(server, outcome)
            server.close_pending_handshakes()
            server.server_close()

    def test_accepted_socket_tracking_is_bounded_before_handshake(self):
        from scripts import durable_google_login_app

        class Request:
            def __init__(self):
                self.closed = False

            def shutdown(self, _how):
                return None

            def close(self):
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 71

        class ForbiddenTlsContext:
            @staticmethod
            def wrap_socket(*_args, **_kwargs):
                raise AssertionError(
                    "capacity_rejection_must_precede_handshake"
                )

        server = durable_google_login_app._DrainingThreadingHTTPServer(
            ("127.0.0.1", 0),
            durable_google_login_app._UnpublishedRequestHandler,
            False,
        )
        request = Request()
        outcome = None
        try:
            server.set_tls_context(ForbiddenTlsContext())
            outcome, _signal_state = self.arm_server_admission(
                durable_google_login_app,
                server,
            )
            with server._lifecycle_lock:
                server._accepted_sockets.update(
                    object()
                    for _index in range(
                        durable_google_login_app
                        ._MAX_TRACKED_ACCEPTED_SOCKETS
                    )
                )
            with mock.patch(
                "socketserver.TCPServer.get_request",
                return_value=(
                    request,
                    ("127.0.0.1", 43214),
                ),
            ) as accept:
                with self.assertRaisesRegex(
                    OSError,
                    "accepted_socket_capacity_reached",
                ):
                    server.get_request()
            accept.assert_not_called()
            self.assertFalse(request.closed)
            self.assertEqual(
                server.resource_counts()["pending_handshakes"],
                0,
            )
            self.assertEqual(
                server.resource_counts()["accepted_sockets"],
                durable_google_login_app
                ._MAX_TRACKED_ACCEPTED_SOCKETS,
            )
        finally:
            with server._lifecycle_lock:
                server._accepted_sockets.clear()
            if outcome is not None:
                self.disarm_server_admission(server, outcome)
            server.server_close()

    def test_server_and_concrete_listener_cleanup_are_independent_retryable(
        self,
    ):
        from scripts import durable_google_login_app

        exception_factories = (
            lambda: RuntimeError("PRIVATE_SERVER_CLOSE_FAILURE"),
            lambda: KeyboardInterrupt("PRIVATE_SERVER_CLOSE_INTERRUPT"),
            lambda: SystemExit("PRIVATE_SERVER_CLOSE_EXIT"),
            lambda: GeneratorExit("PRIVATE_SERVER_CLOSE_GENERATOR"),
        )
        for exception_factory in exception_factories:
            with self.subTest(
                exception_type=type(exception_factory()).__name__,
            ):
                injected = exception_factory()

                class Listener:
                    def __init__(self):
                        self.closed = False
                        self.close_calls = 0

                    def shutdown(self, _how):
                        return None

                    def close(self):
                        self.close_calls += 1
                        self.closed = True

                    def fileno(self):
                        return -1 if self.closed else 61

                class Server:
                    def __init__(self):
                        self.socket = Listener()
                        self.close_calls = 0

                    def server_close(self):
                        self.close_calls += 1
                        if self.close_calls == 1:
                            raise injected

                server = Server()
                listener = server.socket
                ownership = durable_google_login_app._ServerOwnership()
                self.assertTrue(ownership.acquire(server))
                with self.assertRaises(type(injected)) as caught:
                    ownership.close_high_level()
                self.assertIs(caught.exception, injected)
                self.assertFalse(ownership.high_level_terminal())
                self.assertTrue(ownership.close_listener())
                self.assertTrue(ownership.listener_terminal())
                self.assertTrue(listener.closed)
                self.assertTrue(ownership.close_high_level())
                self.assertTrue(ownership.high_level_terminal())
                self.assertEqual(server.close_calls, 2)
                self.assertEqual(listener.close_calls, 1)
                self.assertTrue(ownership.close_listener())
                self.assertEqual(listener.close_calls, 1)

    def test_concrete_listener_close_failure_is_unresolved_until_retry(self):
        from scripts import durable_google_login_app

        for exception_type in (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(exception_type=exception_type.__name__):
                injected = exception_type(
                    "PRIVATE_LISTENER_CLOSE_FAILURE"
                )

                class Listener:
                    def __init__(self):
                        self.closed = False
                        self.close_calls = 0

                    def shutdown(self, _how):
                        return None

                    def close(self):
                        self.close_calls += 1
                        if self.close_calls == 1:
                            raise injected
                        self.closed = True

                    def fileno(self):
                        return -1 if self.closed else 67

                class Server:
                    def __init__(self):
                        self.socket = Listener()

                    def server_close(self):
                        return None

                server = Server()
                ownership = durable_google_login_app._ServerOwnership()
                self.assertTrue(ownership.acquire(server))
                if exception_type is RuntimeError:
                    self.assertFalse(ownership.close_listener())
                else:
                    with self.assertRaises(exception_type) as caught:
                        ownership.close_listener()
                    self.assertIs(caught.exception, injected)
                self.assertFalse(ownership.listener_terminal())
                self.assertTrue(ownership.close_listener())
                self.assertTrue(ownership.listener_terminal())
                self.assertEqual(server.socket.close_calls, 2)
                self.assertTrue(ownership.close_listener())
                self.assertEqual(server.socket.close_calls, 2)

    def test_request_thread_cannot_use_authority_after_terminal_cleanup(
        self,
    ):
        from wahojobs.durable_google_login_runtime import (
            _CleanupCoordinator,
        )

        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        successful_uses = []
        rejected_uses = []

        class Authority:
            def __init__(self):
                self.lock = threading.Lock()
                self.terminal = False

            def use(self):
                with self.lock:
                    if self.terminal:
                        rejected_uses.append(threading.get_ident())
                        raise RuntimeError("authority_terminal")
                    successful_uses.append(threading.get_ident())

            def close(self):
                with self.lock:
                    self.terminal = True
                return True

        authority = Authority()

        def request():
            authority.use()
            entered.set()
            release.wait(1)
            authority.use()
            finished.set()

        request_thread = threading.Thread(
            target=request,
            daemon=False,
        )
        coordinator = _CleanupCoordinator()
        coordinator.own(
            "browser_integration",
            authority,
            lambda resource: resource.close(),
            dependencies=("request_threads",),
        )
        coordinator.own(
            "request_threads",
            request_thread,
            lambda thread: not thread.is_alive(),
        )
        request_thread.start()
        self.assertTrue(entered.wait(1))
        first = coordinator.cleanup()
        self.assertFalse(first.cleanup_complete)
        self.assertEqual(
            first.unresolved_resources,
            ("browser_integration", "request_threads"),
        )
        self.assertFalse(authority.terminal)
        release.set()
        request_thread.join(1)
        self.assertFalse(request_thread.is_alive())
        self.assertTrue(finished.is_set())
        second = coordinator.cleanup()
        self.assertTrue(second.cleanup_complete)
        self.assertTrue(authority.terminal)
        self.assertEqual(len(successful_uses), 2)

        denied = threading.Event()
        unauthorized_success = []

        def use_after_terminal():
            try:
                authority.use()
            except RuntimeError:
                denied.set()
            else:
                unauthorized_success.append(threading.get_ident())

        denied_thread = threading.Thread(
            target=use_after_terminal,
            daemon=False,
        )
        denied_thread.start()
        denied_thread.join(1)
        self.assertFalse(denied_thread.is_alive())
        self.assertTrue(denied.is_set())
        self.assertEqual(unauthorized_success, [])
        self.assertEqual(len(successful_uses), 2)
        self.assertEqual(len(rejected_uses), 1)

    def test_serve_thread_join_timeout_remains_owned_until_retry(self):
        from scripts import durable_google_login_app

        class Server:
            def __init__(self):
                self.shutdown_calls = 0
                self.pending_close_calls = 0

            def begin_shutdown(self):
                self.shutdown_calls += 1
                return True

            def close_pending_handshakes(self):
                self.pending_close_calls += 1
                return True

            def resource_counts(self):
                return {
                    "listener": 0,
                    "accepted_sockets": 0,
                    "pending_handshakes": 0,
                    "request_threads": 0,
                    "serve_threads": 0,
                    "route_integrations": 0,
                }

        class Thread:
            def __init__(self):
                self.alive = True
                self.join_calls = 0

            def is_alive(self):
                return self.alive

            def join(self, _timeout):
                self.join_calls += 1
                if self.join_calls == 2:
                    self.alive = False

        server = Server()
        thread = Thread()
        outcome = durable_google_login_app._ServeOutcome()
        self.assertTrue(outcome.begin_starting())
        resource = (server, thread, outcome)
        self.assertFalse(
            durable_google_login_app._stop_serve_thread(resource)
        )
        self.assertTrue(thread.is_alive())
        self.assertEqual(outcome.state, "stopping")
        self.assertTrue(
            durable_google_login_app._stop_serve_thread(resource)
        )
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome.state, "stopped")
        self.assertEqual(thread.join_calls, 2)
        self.assertEqual(server.shutdown_calls, 2)
        self.assertEqual(server.pending_close_calls, 2)

    def test_route_mutation_and_unstarted_serve_thread_are_owned(self):
        from scripts import durable_google_login_app

        for failure_position in ("route_publish", "thread_start"):
            with self.subTest(failure_position=failure_position):
                events = []
                reports = []
                test_case = self

                class Integration:
                    def matches_route(self, _path):
                        return False

                    def handle(self, *_args, **_kwargs):
                        raise AssertionError("request_not_expected")

                class Runtime:
                    configuration = SimpleNamespace(
                        bind_host="127.0.0.1",
                        bind_port=8443,
                        public_origin="https://localhost:8443",
                    )
                    browser_integration = Integration()

                    def close(self):
                        events.append("runtime_close")

                class Socket:
                    def __init__(self):
                        self.closed = False

                    def close(self):
                        self.closed = True

                    def fileno(self):
                        return -1 if self.closed else 29

                class TlsContext:
                    @staticmethod
                    def wrap_socket(*_args, **_kwargs):
                        raise AssertionError(
                            "no_handshake_expected_during_activation"
                        )

                class TlsScope:
                    def prepare_workspace(self):
                        events.append("tls_prepare")
                        return True

                    def build_context(self):
                        return TlsContext()

                    def close(self):
                        events.append("tls_close")
                        return True

                class Server:
                    def __init__(
                        self,
                        _address,
                        handler,
                        bind_and_activate,
                    ):
                        test_case.assertFalse(bind_and_activate)
                        self.socket = Socket()
                        self.RequestHandlerClass = handler
                        self.route = False

                    def set_shutdown_notification(self, requested):
                        self.requested = requested
                        return True

                    def set_tls_context(
                        self,
                        context,
                        *,
                        handshake_timeout,
                    ):
                        self.context = context
                        self.handshake_timeout = handshake_timeout
                        events.append("tls_configure")
                        return True

                    def set_serve_lifecycle(
                        self,
                        outcome,
                        signal_state,
                    ):
                        self.outcome = outcome
                        self.signal_state = signal_state
                        return True

                    def claim_serving_readiness(self):
                        return self.outcome.claim_ready(
                            self.signal_state
                        )

                    def publish_handler(self, handler):
                        self.RequestHandlerClass = handler
                        self.route = True
                        events.append("route_publish")
                        return failure_position != "route_publish"

                    def detach_route_integration(self):
                        handler = self.RequestHandlerClass
                        if hasattr(
                            handler,
                            "_durable_google_login_browser_integration",
                        ):
                            handler._durable_google_login_browser_integration = (
                                None
                            )
                        self.route = False
                        events.append("route_detach")
                        return True

                    def server_bind(self):
                        events.append("bind")

                    def server_activate(self):
                        events.append("activate")

                    def begin_shutdown(self):
                        events.append("server_shutdown")
                        return True

                    def close_accepted_sockets(self):
                        return True

                    def close_pending_handshakes(self):
                        return True

                    def drain_request_threads(self, _timeout):
                        return True

                    def close_listener(self):
                        self.socket.close()
                        return True

                    def server_close(self):
                        self.socket.close()

                    def resource_counts(self):
                        return {
                            "listener": (
                                0 if self.socket.closed else 1
                            ),
                            "accepted_sockets": 0,
                            "pending_handshakes": 0,
                            "request_threads": 0,
                            "serve_threads": 0,
                            "route_integrations": (
                                1 if self.route else 0
                            ),
                        }

                class FailingThread:
                    def __init__(self, **_kwargs):
                        events.append("thread_construct")

                    def start(self):
                        events.append("thread_start")
                        raise RuntimeError("PRIVATE_THREAD_START_FAILURE")

                    def is_alive(self):
                        events.append("thread_terminal_probe")
                        return False

                    def join(self, _timeout):
                        raise AssertionError("unstarted_thread_not_joined")

                thread_patch = (
                    mock.patch.object(
                        durable_google_login_app.threading,
                        "Thread",
                        FailingThread,
                    )
                    if failure_position == "thread_start"
                    else mock.patch.object(
                        durable_google_login_app,
                        "_ServeOutcome",
                        durable_google_login_app._ServeOutcome,
                    )
                )
                with thread_patch, mock.patch("builtins.print"):
                    status = durable_google_login_app.main(
                        ["--config", str(ROOT / "unused-runtime.json")],
                        _runtime_builder=lambda _path: Runtime(),
                        _server_factory=Server,
                        _tls_context_factory=TlsScope,
                        _shutdown_result_observer=reports.append,
                    )
                self.assertEqual(status, 2)
                self.assertEqual(len(reports), 1)
                self.assertFalse(reports[0].ready_state_reached)
                self.assertTrue(reports[0].cleanup_complete)
                self.assertIn("route_detach", events)
                self.assertIn("runtime_close", events)
                self.assertIn("tls_close", events)
                if failure_position == "route_publish":
                    self.assertNotIn("bind", events)
                else:
                    self.assertLess(
                        events.index("thread_construct"),
                        events.index("thread_start"),
                    )
                    self.assertIn(
                        "serve_thread",
                        reports[0].resources_closed,
                    )
                    self.assertIn("thread_terminal_probe", events)

    def test_activation_failure_matrix_reaches_ready_only_at_ready_observer(
        self,
    ):
        from scripts import durable_google_login_app

        checkpoints = (
            "configuration_validated",
            "database_lifetime_owned",
            "database_attested",
            "configuration_resolved",
            "tls_workspace_ready",
            "secrets_loaded",
            "gateway_constructed",
            "key_authority_constructed",
            "connections_constructed",
            "profile_integration_activated",
            "browser_constructed",
            "runtime_prepared",
            "inactive_server",
            "tls_context",
            "tls_wrapped",
            "routes_published",
            "final_reverification",
            "signals_installed",
            "bound",
            "activated",
            "before_ready",
            "ready_commit",
            "ready",
        )

        class Socket:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 23

        for failed_checkpoint in checkpoints:
            with self.subTest(checkpoint=failed_checkpoint):
                events = []
                reports = []
                servers = []
                tls_scopes = []
                test_case = self

                class TlsContext:
                    @staticmethod
                    def wrap_socket(*_args, **_kwargs):
                        raise AssertionError(
                            "no_handshake_expected_during_activation"
                        )

                class TlsScope:
                    def __init__(self):
                        self.closed = False
                        tls_scopes.append(self)

                    def prepare_workspace(self):
                        events.append("tls_workspace")
                        return True

                    def build_context(self):
                        events.append("tls_context")
                        return TlsContext()

                    def close(self):
                        self.closed = True
                        events.append("tls_close")
                        return True

                class Server:
                    def __init__(
                        self,
                        _address,
                        handler,
                        bind_and_activate,
                    ):
                        test_case.assertIs(
                            handler,
                            durable_google_login_app._UnpublishedRequestHandler,
                        )
                        test_case.assertFalse(bind_and_activate)
                        self.socket = Socket()
                        self.RequestHandlerClass = handler
                        self.stop = threading.Event()
                        self.route = False
                        self.outcome = None
                        servers.append(self)
                        events.append("server_construct")

                    def set_shutdown_notification(self, requested):
                        self.requested = requested
                        return True

                    def set_tls_context(
                        self,
                        context,
                        *,
                        handshake_timeout,
                    ):
                        self.context = context
                        self.handshake_timeout = handshake_timeout
                        events.append("tls_configure")
                        return True

                    def set_serve_lifecycle(
                        self,
                        outcome,
                        signal_state,
                    ):
                        self.outcome = outcome
                        self.signal_state = signal_state
                        return True

                    def claim_serving_readiness(self):
                        return self.outcome.claim_ready(
                            self.signal_state
                        )

                    def publish_handler(self, handler):
                        self.RequestHandlerClass = handler
                        self.route = True
                        events.append("route_publish")
                        return True

                    def detach_route_integration(self):
                        handler = self.RequestHandlerClass
                        if hasattr(
                            handler,
                            "_durable_google_login_browser_integration",
                        ):
                            handler._durable_google_login_browser_integration = (
                                None
                            )
                        self.route = False
                        return True

                    def server_bind(self):
                        events.append("bind")

                    def server_activate(self):
                        events.append("activate")

                    def serve_forever(self, *, poll_interval):
                        self.poll_interval = poll_interval
                        events.append("serve")
                        self.outcome.publish_serving_checkpoint(
                            self.signal_state
                        )
                        self.stop.wait(2)

                    def begin_shutdown(self):
                        self.stop.set()
                        return True

                    def close_accepted_sockets(self):
                        return True

                    def close_pending_handshakes(self):
                        return True

                    def drain_request_threads(self, _timeout):
                        return True

                    def close_listener(self):
                        self.socket.close()
                        return True

                    def server_close(self):
                        self.socket.close()

                    def resource_counts(self):
                        return {
                            "listener": (
                                0 if self.socket.closed else 1
                            ),
                            "accepted_sockets": 0,
                            "pending_handshakes": 0,
                            "request_threads": 0,
                            "serve_threads": (
                                0 if self.stop.is_set() else 1
                            ),
                            "route_integrations": (
                                1 if self.route else 0
                            ),
                        }

                observed = []

                def checkpoint(category):
                    observed.append(category)
                    if category == failed_checkpoint:
                        raise RuntimeError(
                            "PRIVATE_ACTIVATION_CHECKPOINT_FAILURE"
                        )

                with temporary_browser_login_state() as state:
                    with mock.patch("builtins.print"):
                        status = durable_google_login_app.main(
                            [
                                "--config",
                                str(state.configuration_path),
                            ],
                            _server_factory=Server,
                            _tls_context_factory=TlsScope,
                            _checkpoint_observer=checkpoint,
                            _shutdown_result_observer=reports.append,
                        )
                self.assertEqual(status, 2)
                self.assertEqual(observed[-1], failed_checkpoint)
                self.assertNotIn(
                    "ready",
                    observed[:-1],
                )
                self.assertEqual(len(reports), 1)
                self.assertEqual(
                    reports[0].ready_state_reached,
                    failed_checkpoint == "ready",
                )
                self.assertTrue(reports[0].cleanup_complete)
                if "route_publish" in events:
                    self.assertLess(
                        events.index("tls_configure"),
                        events.index("route_publish"),
                    )
                self.assertTrue(
                    all(server.socket.closed for server in servers)
                )
                self.assertTrue(
                    all(scope.closed for scope in tls_scopes)
                )

    def test_shutdown_result_is_bounded_immutable_and_validated(self):
        from dataclasses import FrozenInstanceError
        from scripts import durable_google_login_app

        result = durable_google_login_app._ShutdownResult(
            ready_state_reached=True,
            shutdown_requested=True,
            resources_closed=("listener_socket",),
            unresolved_resource_categories=(),
            cleanup_complete=True,
            cleanup_failure_categories=("listener_socket",),
            signal_category="sigint",
        )
        rendered = repr(result)
        self.assertNotIn("listener_socket", rendered)
        self.assertNotIn("8443", rendered)
        with self.assertRaises(FrozenInstanceError):
            result.cleanup_complete = False
        with self.assertRaisesRegex(
            RuntimeError,
            "invalid_shutdown_result",
        ):
            durable_google_login_app._ShutdownResult(
                ready_state_reached=False,
                shutdown_requested=False,
                resources_closed=(
                    "PRIVATE_PATH_C:\\secret",
                ),
                unresolved_resource_categories=(),
                cleanup_complete=True,
                cleanup_failure_categories=(),
                signal_category=None,
            )

    def test_production_activation_checkpoints_enforce_prebind_publication_order(
        self,
    ):
        from scripts import durable_google_login_app

        checkpoints = []
        events = []

        class Socket:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 11

        class TlsContext:
            @staticmethod
            def wrap_socket(*_args, **_kwargs):
                raise AssertionError(
                    "no_handshake_expected_during_activation"
                )

        class TlsScope:
            def prepare_workspace(self):
                events.append("tls_workspace")

            def build_context(self):
                events.append("tls_context")
                return TlsContext()

            def close(self):
                events.append("tls_close")
                return True

        class Server:
            def __init__(self, _address, handler, bind_and_activate):
                events.append(("server_construct", handler))
                if bind_and_activate is not False:
                    raise AssertionError("server_must_start_inactive")
                self.socket = Socket()
                self.RequestHandlerClass = handler
                self.stop = threading.Event()
                self.outcome = None

            def set_shutdown_notification(self, requested):
                self.requested = requested
                return True

            def set_tls_context(
                self,
                context,
                *,
                handshake_timeout,
            ):
                self.context = context
                self.handshake_timeout = handshake_timeout
                events.append("tls_configure")
                return True

            def set_serve_lifecycle(self, outcome, signal_state):
                self.outcome = outcome
                self.signal_state = signal_state
                return True

            def claim_serving_readiness(self):
                return self.outcome.claim_ready(self.signal_state)

            def publish_handler(self, handler):
                events.append("route_publish")
                self.RequestHandlerClass = handler
                return True

            def server_bind(self):
                events.append("bind")

            def server_activate(self):
                events.append("activate")

            def serve_forever(self, *, poll_interval):
                self.poll_interval = poll_interval
                events.append("serve")
                self.outcome.publish_serving_checkpoint(
                    self.signal_state
                )
                deadline = time.monotonic() + 1
                while (
                    not self.stop.is_set()
                    and not self.outcome.ready_state_reached
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.001)

            def begin_shutdown(self):
                self.stop.set()
                return True

            def close_pending_handshakes(self):
                return True

            def close_accepted_sockets(self):
                return True

            def drain_request_threads(self, _timeout):
                return True

            def server_close(self):
                events.append("server_close")
                self.socket.close()

        with temporary_browser_login_state() as state:
            with mock.patch("builtins.print"):
                result = durable_google_login_app.main(
                    ["--config", str(state.configuration_path)],
                    _server_factory=Server,
                    _tls_context_factory=TlsScope,
                    _checkpoint_observer=checkpoints.append,
                )
        self.assertEqual(result, 2)
        expected = (
            "configuration_validated",
            "database_lifetime_owned",
            "database_attested",
            "configuration_resolved",
            "tls_workspace_ready",
            "secrets_loaded",
            "gateway_constructed",
            "key_authority_constructed",
            "connections_constructed",
            "profile_integration_activated",
            "browser_constructed",
            "runtime_prepared",
            "inactive_server",
            "tls_context",
            "tls_wrapped",
            "routes_published",
            "final_reverification",
            "signals_installed",
            "bound",
            "activated",
            "before_ready",
            "ready_commit",
            "ready",
        )
        self.assertEqual(tuple(checkpoints), expected)
        constructed_handler = next(
            value[1]
            for value in events
            if type(value) is tuple and value[0] == "server_construct"
        )
        self.assertIs(
            constructed_handler,
            durable_google_login_app._UnpublishedRequestHandler,
        )
        self.assertLess(
            events.index("tls_configure"),
            events.index("route_publish"),
        )
        self.assertLess(
            checkpoints.index("final_reverification"),
            checkpoints.index("bound"),
        )

    def test_listener_handoff_interruptions_retain_the_concrete_owner(self):
        from scripts import durable_google_login_app

        exception_types = (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        )

        class Listener:
            def __init__(self):
                self.closed = False
                self.close_calls = 0

            def close(self):
                self.close_calls += 1
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 101

        class Server:
            def __init__(self, *_args):
                self.socket = Listener()
                self.close_calls = 0

            def server_close(self):
                self.close_calls += 1

        for exception_type in exception_types:
            with self.subTest(exception_type=exception_type.__name__):
                injected = exception_type(
                    "PRIVATE_LISTENER_ATTACHMENT_CONTROL"
                )

                class InterruptingOwnership(
                    durable_google_login_app._ServerOwnership
                ):
                    def __init__(self):
                        super().__init__()
                        self.interrupted = False

                    def attach_listener(self, server, listener):
                        result = super().attach_listener(server, listener)
                        if not self.interrupted:
                            self.interrupted = True
                            raise injected
                        return result

                ownership = InterruptingOwnership()
                constructed = []

                def factory(*args):
                    server = Server(*args)
                    constructed.append(server)
                    return server

                with self.assertRaises(exception_type) as caught:
                    durable_google_login_app._construct_owned_server(
                        factory,
                        ("127.0.0.1", 0),
                        object,
                        ownership,
                    )
                self.assertIs(caught.exception, injected)
                self.assertEqual(len(constructed), 1)
                server = constructed[0]
                listener = server.socket
                self.assertTrue(ownership.owns(server))
                self.assertTrue(
                    ownership.owns_listener(server, listener)
                )
                self.assertTrue(ownership.close_listener())
                self.assertTrue(listener.closed)
                self.assertTrue(ownership.close_high_level())
                self.assertTrue(ownership.listener_terminal())
                self.assertTrue(ownership.high_level_terminal())
                self.assertEqual(listener.close_calls, 1)

        server = Server()
        listener = server.socket
        ownership = durable_google_login_app._ServerOwnership()
        ownership.acquire_server(server)
        self.assertFalse(ownership._listener_terminal)
        self.assertTrue(ownership.close_listener())
        self.assertTrue(listener.closed)
        self.assertTrue(ownership.close_high_level())
        self.assertTrue(ownership.listener_terminal())
        self.assertTrue(ownership.high_level_terminal())

    def test_cleanup_coordinator_interrupt_boundaries_are_reclaimable(self):
        from wahojobs import durable_google_login_runtime

        coordinator_type = (
            durable_google_login_runtime._CleanupCoordinator
        )
        cleanup_code = coordinator_type.cleanup.__code__
        source = Path(
            durable_google_login_runtime.__file__
        ).read_text(encoding="utf-8").splitlines()
        start = cleanup_code.co_firstlineno - 1
        end = coordinator_type._entry_locked.__code__.co_firstlineno - 1

        def find_line(text, after=start):
            return next(
                index + 1
                for index in range(after, end)
                if text in source[index]
            )

        owner_line = find_line(
            "self._normalize_interrupted_entries_locked()",
            find_line("self._owner_active = True"),
        )
        action_return_line = find_line(
            "blocks_dependents = not terminal"
        )
        terminal_line = find_line("if terminal:", action_return_line)
        unresolved_line = find_line(
            'entry.state = "unresolved"',
            terminal_line,
        )
        scenarios = (
            ("owner_publication", owner_line, True),
            ("action_return", action_return_line, True),
            ("terminal_commit", terminal_line, True),
            ("unresolved_commit", unresolved_line, False),
        )
        exception_types = (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        )

        for name, target_line, first_result in scenarios:
            for exception_type in exception_types:
                with self.subTest(
                    name=name,
                    exception_type=exception_type.__name__,
                ):
                    coordinator = coordinator_type()
                    calls = []

                    def close(_resource):
                        calls.append("close")
                        if len(calls) == 1:
                            return first_result
                        return True

                    coordinator.own(
                        "google_gateway",
                        object(),
                        close,
                    )
                    injected = exception_type(
                        "PRIVATE_COORDINATOR_BOUNDARY"
                    )
                    fired = False

                    def trace(frame, event, _arg):
                        nonlocal fired
                        if (
                            not fired
                            and event == "line"
                            and frame.f_code is cleanup_code
                            and frame.f_lineno == target_line
                        ):
                            fired = True
                            sys.settrace(None)
                            raise injected
                        return trace

                    sys.settrace(trace)
                    try:
                        if exception_type is RuntimeError:
                            first = coordinator.cleanup()
                        else:
                            with self.assertRaises(
                                exception_type
                            ) as caught:
                                coordinator.cleanup()
                            self.assertIs(caught.exception, injected)
                            first = coordinator.snapshot()
                    finally:
                        sys.settrace(None)
                    self.assertTrue(fired)
                    self.assertFalse(coordinator._owner_active)
                    self.assertNotEqual(
                        coordinator._entries[0].state,
                        "closing",
                    )
                    if first_result:
                        self.assertTrue(first.cleanup_complete)
                        self.assertEqual(calls, ["close"])
                    else:
                        self.assertFalse(first.cleanup_complete)
                        self.assertTrue(
                            coordinator.cleanup().cleanup_complete
                        )
                        self.assertEqual(calls, ["close", "close"])

        coordinator = coordinator_type()
        calls = []
        coordinator.own(
            "google_gateway",
            object(),
            lambda _resource: calls.append("close") or True,
        )
        entry = coordinator._entries[0]
        entry.state = "closing"
        coordinator._owner_active = True
        coordinator._owner_thread = threading.current_thread()
        report = coordinator.cleanup()
        self.assertTrue(report.cleanup_complete)
        self.assertEqual(calls, ["close"])
        self.assertFalse(coordinator._owner_active)
        self.assertIsNone(coordinator._owner_thread)
        self.assertEqual(entry.state, "terminal")
        self.assertIsNone(entry.resource)
        self.assertIsNone(entry.action)

        coordinator = coordinator_type()
        coordinator.own(
            "google_gateway",
            object(),
            lambda _resource: True,
        )
        entry = coordinator._entries[0]
        entry.state = "terminalizing"
        self.assertTrue(coordinator.cleanup().cleanup_complete)
        self.assertEqual(entry.state, "terminal")
        self.assertIsNone(entry.resource)
        self.assertIsNone(entry.action)
        self.assertIsNone(entry.probe)

    def test_pending_runtime_handoff_interruptions_cleanup_deterministically(
        self,
    ):
        from wahojobs import durable_google_login_runtime

        pending_type = (
            durable_google_login_runtime
            ._PendingDurableGoogleLoginActivation
        )
        activation_code = pending_type.complete_activation.__code__
        source = Path(
            durable_google_login_runtime.__file__
        ).read_text(encoding="utf-8").splitlines()
        start = activation_code.co_firstlineno - 1
        end = pending_type.close.__code__.co_firstlineno - 1

        def find_line(text, after=start):
            return next(
                index + 1
                for index in range(after, end)
                if text in source[index]
            )

        offer_line = find_line(
            "configuration = self._configuration"
        )
        runtime_line = find_line(
            "with self._condition:",
            find_line(
                "cleanup_coordinator=self._cleanup_coordinator"
            ),
        )
        accepted_line = find_line(
            "self._condition.notify_all()",
            find_line('self._state = "accepted"'),
        )
        commit_line = find_line("self._configuration = None")
        phases = (
            ("offered", offer_line),
            ("runtime_constructed", runtime_line),
            ("accepted", accepted_line),
            ("before_commit", commit_line),
        )
        exception_types = (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        )

        for phase, target_line in phases:
            for exception_type in exception_types:
                with self.subTest(
                    phase=phase,
                    exception_type=exception_type.__name__,
                ):
                    with temporary_browser_login_state() as state:
                        pending = (
                            durable_google_login_runtime
                            .prepare_durable_google_login_activation(
                                state.configuration_path,
                                _clock=state.clock,
                                _gateway_factory=state.gateway_factory,
                            )
                        )
                        injected = exception_type(
                            "PRIVATE_ACTIVATION_HANDOFF_CONTROL"
                        )
                        fired = False

                        def trace(frame, event, _arg):
                            nonlocal fired
                            if (
                                not fired
                                and event == "line"
                                and frame.f_code is activation_code
                                and frame.f_lineno == target_line
                            ):
                                fired = True
                                sys.settrace(None)
                                raise injected
                            return trace

                        sys.settrace(trace)
                        try:
                            if exception_type is RuntimeError:
                                with self.assertRaises(
                                    DurableGoogleLoginConfigurationError
                                ):
                                    pending.complete_activation()
                            else:
                                with self.assertRaises(
                                    exception_type
                                ) as caught:
                                    pending.complete_activation()
                                self.assertIs(
                                    caught.exception,
                                    injected,
                                )
                        finally:
                            sys.settrace(None)
                        self.assertTrue(fired)
                        self.assertNotIn(
                            pending._state,
                            {"offered", "accepted"},
                        )
                        first = pending.close(
                            _preserve_primary=True
                        )
                        second = pending.close(
                            _preserve_primary=True
                        )
                        self.assertTrue(first.cleanup_complete)
                        self.assertTrue(second.cleanup_complete)
                        self.assertIsNone(pending._activation_owner)

        coordinator = (
            durable_google_login_runtime._CleanupCoordinator()
        )
        close_calls = []
        coordinator.own(
            "signal_handlers",
            object(),
            lambda _resource: close_calls.append("close") or True,
        )
        with temporary_browser_login_state() as state:
            pending = (
                durable_google_login_runtime
                .prepare_durable_google_login_activation(
                    state.configuration_path,
                    _clock=state.clock,
                    _gateway_factory=state.gateway_factory,
                    _cleanup_coordinator=coordinator,
                )
            )
            runtime = pending.complete_activation()
            self.assertEqual(pending._state, "committed")
            self.assertTrue(
                pending.close(_preserve_primary=True).cleanup_complete
            )
            self.assertTrue(
                runtime.close(_preserve_primary=True).cleanup_complete
            )
            self.assertEqual(close_calls, ["close"])

    def test_runtime_worker_return_handoff_closes_on_interruption(self):
        from wahojobs import durable_google_login_runtime

        worker = (
            durable_google_login_runtime
            ._build_durable_google_login_runtime_worker
        )
        worker_code = worker.__code__
        source = Path(
            durable_google_login_runtime.__file__
        ).read_text(encoding="utf-8").splitlines()
        start = worker_code.co_firstlineno - 1
        target_line = next(
            index + 1
            for index in range(start, start + 60)
            if "pending_outcome._clear_value()" in source[index]
        )
        pending_target_line = next(
            index + 1
            for index in range(start, start + 60)
            if "pending = _worker_outcome_value" in source[index]
        )

        for exception_type in (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(exception_type=exception_type.__name__):
                close_calls = []

                class Runtime:
                    def close(self, *, _preserve_primary=False):
                        close_calls.append(_preserve_primary)
                        return True

                runtime = Runtime()

                class Pending:
                    def complete_activation(self):
                        return runtime

                    def close(self, *, _preserve_primary=False):
                        raise AssertionError(
                            "committed_runtime_is_cleanup_owner"
                        )

                def prepare(*args):
                    args[-1]._publish("ok", Pending())

                injected = exception_type(
                    "PRIVATE_RUNTIME_RETURN_HANDOFF"
                )
                fired = False

                def trace(frame, event, _arg):
                    nonlocal fired
                    if (
                        not fired
                        and event == "line"
                        and frame.f_code is worker_code
                        and frame.f_lineno == target_line
                    ):
                        fired = True
                        sys.settrace(None)
                        raise injected
                    return trace

                with mock.patch.object(
                    durable_google_login_runtime,
                    "_prepare_durable_google_login_activation_worker",
                    side_effect=prepare,
                ):
                    outcome = (
                        durable_google_login_runtime
                        ._ConfigurationWorkerOutcome(
                            durable_google_login_runtime
                            ._WORKER_OUTCOME_CAPABILITY,
                            "pending",
                        )
                    )
                    sys.settrace(trace)
                    try:
                        with self.assertRaises(
                            exception_type
                        ) as caught:
                            worker(
                                None,
                                None,
                                None,
                                None,
                                outcome,
                            )
                        self.assertIs(caught.exception, injected)
                    finally:
                        sys.settrace(None)
                self.assertTrue(fired)
                self.assertEqual(close_calls, [True])

        for exception_type in (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(
                boundary="pending_return",
                exception_type=exception_type.__name__,
            ):
                close_calls = []

                class Pending:
                    def complete_activation(self):
                        raise AssertionError(
                            "runtime_construction_must_not_begin"
                        )

                    def close(self, *, _preserve_primary=False):
                        close_calls.append(_preserve_primary)
                        return True

                def prepare(*args):
                    args[-1]._publish("ok", Pending())

                injected = exception_type(
                    "PRIVATE_PENDING_RETURN_HANDOFF"
                )
                fired = False

                def trace(frame, event, _arg):
                    nonlocal fired
                    if (
                        not fired
                        and event == "line"
                        and frame.f_code is worker_code
                        and frame.f_lineno == pending_target_line
                    ):
                        fired = True
                        sys.settrace(None)
                        raise injected
                    return trace

                with mock.patch.object(
                    durable_google_login_runtime,
                    "_prepare_durable_google_login_activation_worker",
                    side_effect=prepare,
                ):
                    outcome = (
                        durable_google_login_runtime
                        ._ConfigurationWorkerOutcome(
                            durable_google_login_runtime
                            ._WORKER_OUTCOME_CAPABILITY,
                            "pending",
                        )
                    )
                    sys.settrace(trace)
                    try:
                        with self.assertRaises(
                            exception_type
                        ) as caught:
                            worker(
                                None,
                                None,
                                None,
                                None,
                                outcome,
                            )
                        self.assertIs(caught.exception, injected)
                    finally:
                        sys.settrace(None)
                self.assertTrue(fired)
                self.assertEqual(close_calls, [True])

    def test_socket_lease_handoffs_retain_raw_and_wrapped_owners(self):
        from scripts import durable_google_login_app

        class OwnedSocket:
            def __init__(self, descriptor):
                self.descriptor = descriptor
                self.closed = False
                self.close_calls = 0
                self.timeout = None

            def shutdown(self, _how):
                return None

            def close(self):
                self.close_calls += 1
                self.closed = True

            def fileno(self):
                return -1 if self.closed else self.descriptor

            def gettimeout(self):
                return self.timeout

            def settimeout(self, value):
                self.timeout = value

            def do_handshake(self):
                return None

        exception_types = (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        )
        for exception_type in exception_types:
            with self.subTest(
                boundary="wrap_adoption",
                exception_type=exception_type.__name__,
            ):
                raw = OwnedSocket(111)
                wrapped = OwnedSocket(113)
                injected = exception_type(
                    "PRIVATE_WRAP_ADOPTION_CONTROL"
                )

                class InterruptingLease(
                    durable_google_login_app._PendingTlsHandshake
                ):
                    def _adopt_wrapped_socket(
                        self,
                        request,
                        result,
                    ):
                        super()._adopt_wrapped_socket(
                            request,
                            result,
                        )
                        raise injected

                class Context:
                    @staticmethod
                    def wrap_socket(*_args, **_kwargs):
                        return wrapped

                lease = InterruptingLease(raw)
                lease.begin_handshake()
                with self.assertRaises(exception_type) as caught:
                    lease.wrap(Context())
                self.assertIs(caught.exception, injected)
                self.assertTrue(raw.closed)
                self.assertTrue(wrapped.closed)
                self.assertTrue(lease.terminal())

            with self.subTest(
                boundary="transfer_acknowledgement",
                exception_type=exception_type.__name__,
            ):
                raw = OwnedSocket(117)
                wrapped = OwnedSocket(119)

                class Context:
                    @staticmethod
                    def wrap_socket(*_args, **_kwargs):
                        return wrapped

                lease = (
                    durable_google_login_app._PendingTlsHandshake(
                        raw
                    )
                )
                lease.begin_handshake()
                result = lease.wrap(Context())
                lease.claim_handshake_io(result)
                lease.mark_ready(result)
                request = lease.transfer()
                self.assertIs(request, wrapped)
                self.assertTrue(lease.owns_request(request))
                self.assertTrue(lease.owns_socket(raw))
                self.assertTrue(lease.owns_socket(wrapped))
                try:
                    raise exception_type(
                        "PRIVATE_TRANSFER_ACK_CONTROL"
                    )
                except exception_type:
                    pass
                self.assertTrue(lease.close())
                self.assertTrue(raw.closed)
                self.assertTrue(wrapped.closed)
                self.assertTrue(lease.terminal())

        raw = OwnedSocket(121)
        wrapped = OwnedSocket(123)

        class Context:
            @staticmethod
            def wrap_socket(*_args, **_kwargs):
                return wrapped

        lease = durable_google_login_app._PendingTlsHandshake(raw)
        lease.begin_handshake()
        result = lease.wrap(Context())
        lease.claim_handshake_io(result)
        lease.mark_ready(result)
        request = lease.transfer()
        lease.acknowledge_transfer(request)
        server = object.__new__(
            durable_google_login_app._DrainingThreadingHTTPServer
        )
        server._lifecycle_lock = threading.Lock()
        server._pending_handshakes = set()
        server._unregistered_handshake = None
        server._accepted_sockets = {lease}
        self.assertTrue(server.close_accepted_sockets())
        self.assertEqual(server._accepted_sockets, set())
        self.assertTrue(lease.terminal())
        self.assertTrue(raw.closed)
        self.assertTrue(wrapped.closed)

    def test_tls_workspace_and_policy_release_are_owned_and_retryable(self):
        from scripts import durable_google_login_app

        prepare_code = (
            durable_google_login_app
            ._EphemeralTlsContext
            .prepare_workspace
            .__code__
        )
        source = Path(
            durable_google_login_app.__file__
        ).read_text(encoding="utf-8").splitlines()
        start = prepare_code.co_firstlineno - 1
        end = (
            durable_google_login_app
            ._EphemeralTlsContext
            .build_context
            .__code__
            .co_firstlineno
            - 1
        )

        def find_line(text):
            return next(
                index + 1
                for index in range(start, end)
                if text in source[index]
            )

        boundaries = (
            (
                "post_create",
                find_line(
                    "directory = Path(temporary.name).resolve()"
                ),
            ),
            (
                "during_publication",
                find_line("self._directory = directory"),
            ),
        )
        exception_types = (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        )
        real_temporary_directory = tempfile.TemporaryDirectory
        for boundary, target_line in boundaries:
            for exception_type in exception_types:
                with self.subTest(
                    boundary=boundary,
                    exception_type=exception_type.__name__,
                ):
                    created = []

                    def temporary_factory(*args, **kwargs):
                        temporary = real_temporary_directory(
                            *args,
                            **kwargs,
                        )
                        created.append(Path(temporary.name))
                        return temporary

                    scope = (
                        durable_google_login_app
                        ._EphemeralTlsContext()
                    )
                    injected = exception_type(
                        "PRIVATE_TLS_WORKSPACE_CONTROL"
                    )
                    fired = False

                    def trace(frame, event, _arg):
                        nonlocal fired
                        if (
                            not fired
                            and event == "line"
                            and frame.f_code is prepare_code
                            and frame.f_lineno == target_line
                        ):
                            fired = True
                            sys.settrace(None)
                            raise injected
                        return trace

                    with mock.patch.object(
                        durable_google_login_app.tempfile,
                        "TemporaryDirectory",
                        side_effect=temporary_factory,
                    ):
                        sys.settrace(trace)
                        try:
                            with self.assertRaises(
                                exception_type
                            ) as caught:
                                scope.prepare_workspace()
                            self.assertIs(
                                caught.exception,
                                injected,
                            )
                        finally:
                            sys.settrace(None)
                    self.assertTrue(fired)
                    self.assertEqual(len(created), 1)
                    self.assertFalse(created[0].exists())
                    self.assertTrue(scope.close())
                    self.assertIsNone(scope._temporary)
                    self.assertIsNone(scope._directory)

        server = (
            durable_google_login_app
            ._DrainingThreadingHTTPServer(
                ("127.0.0.1", 0),
                durable_google_login_app
                ._UnpublishedRequestHandler,
                False,
            )
        )
        left, right = socket.socketpair()

        class Scope:
            def __init__(self):
                self.close_calls = 0

            def close(self):
                self.close_calls += 1
                return True

        scope = Scope()
        ownership = durable_google_login_app._TlsWorkspaceOwnership()
        class Context:
            @staticmethod
            def wrap_socket(*_args, **_kwargs):
                raise AssertionError("handshake_not_expected")

        context = ssl_context = Context()
        try:
            ownership.acquire_scope(scope)
            ownership.attach_server(server)
            server.set_tls_context(context)
            with server._lifecycle_lock:
                server._accepted_sockets.add(left)
            self.assertFalse(ownership.close())
            self.assertIs(server._tls_context, ssl_context)
            self.assertEqual(scope.close_calls, 0)
            self.assertTrue(server.close_accepted_sockets())
            self.assertTrue(server.close_listener())
            server.server_close()
            self.assertTrue(ownership.close())
            self.assertTrue(ownership.terminal())
            self.assertIsNone(server._tls_context)
            self.assertIsNone(server._tls_handshake_timeout)
            self.assertEqual(scope.close_calls, 1)
            self.assertTrue(ownership.close())
            self.assertEqual(scope.close_calls, 1)
        finally:
            left.close()
            right.close()
            server.close_accepted_sockets()
            server.close_listener()
            server.server_close()

    def test_socket_producer_offers_survive_pre_adoption_control(self):
        from scripts import durable_google_login_app

        exception_types = (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        )
        source = Path(
            durable_google_login_app.__file__
        ).read_text(encoding="utf-8").splitlines()
        accept_code = (
            durable_google_login_app
            ._PendingTlsHandshake
            .accept_from
            .__code__
        )
        wrap_code = (
            durable_google_login_app
            ._PendingTlsHandshake
            .wrap
            .__code__
        )

        def line_for(code, text):
            start = code.co_firstlineno - 1
            return next(
                index + 1
                for index in range(start, start + 120)
                if text in source[index]
            )

        accept_boundaries = (
            (
                "descriptor_offer",
                line_for(
                    accept_code,
                    (
                        "descriptor_record = "
                        "self._accepted_descriptor_offer[0]"
                    ),
                ),
            ),
            (
                "raw_socket_offer",
                line_for(
                    accept_code,
                    "request = self._raw_socket_offer[0]",
                ),
            ),
        )
        for boundary, target_line in accept_boundaries:
            for exception_type in exception_types:
                with self.subTest(
                    boundary=boundary,
                    exception_type=exception_type.__name__,
                ):
                    left, right = socket.socketpair()
                    descriptor = left.detach()

                    class Listener:
                        family = right.family
                        type = right.type
                        proto = right.proto

                        @staticmethod
                        def _accept():
                            return descriptor, (
                                "127.0.0.1",
                                43219,
                            )

                        @staticmethod
                        def gettimeout():
                            return None

                    lease = (
                        durable_google_login_app
                        ._PendingTlsHandshake()
                    )
                    injected = exception_type(
                        "PRIVATE_ACCEPT_PUBLICATION_CONTROL"
                    )
                    fired = False

                    def trace(frame, event, _arg):
                        nonlocal fired
                        if (
                            not fired
                            and event == "line"
                            and frame.f_code is accept_code
                            and frame.f_lineno == target_line
                        ):
                            fired = True
                            sys.settrace(None)
                            raise injected
                        return trace

                    sys.settrace(trace)
                    try:
                        with self.assertRaises(
                            exception_type
                        ) as caught:
                            lease.accept_from(Listener())
                        self.assertIs(caught.exception, injected)
                    finally:
                        sys.settrace(None)
                    lease.cancel_accept()
                    self.assertFalse(lease.terminal())
                    self.assertEqual(
                        len(lease._accepted_descriptor_offer),
                        1,
                    )
                    self.assertEqual(
                        lease._raw_socket_offer[0].fileno(),
                        -1,
                    )
                    with mock.patch.object(
                        durable_google_login_app.socket,
                        "close",
                        side_effect=AssertionError(
                            "numeric_descriptor_close_forbidden"
                        ),
                    ) as numeric_close:
                        self.assertTrue(lease.close())
                    numeric_close.assert_not_called()
                    self.assertTrue(lease.terminal())
                    self.assertTrue(fired)
                    right.settimeout(1)
                    self.assertEqual(right.recv(1), b"")
                    right.close()

        class OwnedSocket:
            def __init__(self, descriptor):
                self.descriptor = descriptor
                self.closed = False

            def shutdown(self, _how):
                return None

            def close(self):
                self.closed = True

            def fileno(self):
                return -1 if self.closed else self.descriptor

        wrap_line = line_for(
            wrap_code,
            "wrapped = self._wrapped_socket_offer[0]",
        )
        for exception_type in exception_types:
            with self.subTest(
                boundary="wrapped_socket_offer",
                exception_type=exception_type.__name__,
            ):
                raw = OwnedSocket(131)
                wrapped = OwnedSocket(133)

                class Context:
                    @staticmethod
                    def wrap_socket(*_args, **_kwargs):
                        return wrapped

                lease = (
                    durable_google_login_app
                    ._PendingTlsHandshake(raw)
                )
                lease.begin_handshake()
                injected = exception_type(
                    "PRIVATE_WRAP_PUBLICATION_CONTROL"
                )
                fired = False

                def trace(frame, event, _arg):
                    nonlocal fired
                    if (
                        not fired
                        and event == "line"
                        and frame.f_code is wrap_code
                        and frame.f_lineno == wrap_line
                    ):
                        fired = True
                        sys.settrace(None)
                        raise injected
                    return trace

                sys.settrace(trace)
                try:
                    with self.assertRaises(
                        exception_type
                    ) as caught:
                        lease.wrap(Context())
                    self.assertIs(caught.exception, injected)
                finally:
                    sys.settrace(None)
                self.assertTrue(fired)
                self.assertTrue(raw.closed)
                self.assertTrue(wrapped.closed)
                self.assertTrue(lease.terminal())

        materialize_code = (
            durable_google_login_app
            ._PendingTlsHandshake
            ._materialize_owned_tls_socket
            .__code__
        )
        materialize_start = materialize_code.co_firstlineno - 1
        descriptor_line = next(
            index + 1
            for index in range(
                materialize_start,
                materialize_start + 180,
            )
            if (
                "descriptor = self._wrapped_descriptor_offer[0]"
                in source[index]
            )
        )
        ssl_object_assignment = next(
            index
            for index in range(
                materialize_start,
                materialize_start + 180,
            )
            if (
                "wrapped._sslobj = context._wrap_socket("
                in source[index]
            )
        )
        ssl_object_line = next(
            index + 1
            for index in range(
                ssl_object_assignment + 1,
                materialize_start + 180,
            )
            if source[index].strip() == "with self._lock:"
        )
        for boundary, target_line in (
            ("tls_descriptor_offer", descriptor_line),
            ("tls_object_offer", ssl_object_line),
        ):
            for exception_type in exception_types:
                with self.subTest(
                    boundary=boundary,
                    exception_type=exception_type.__name__,
                ):
                    request, peer = socket.socketpair()
                    lease = (
                        durable_google_login_app
                        ._PendingTlsHandshake(request)
                    )
                    lease.begin_handshake()
                    context = ssl.SSLContext(
                        ssl.PROTOCOL_TLS_SERVER
                    )
                    injected = exception_type(
                        "PRIVATE_TLS_MATERIALIZATION_CONTROL"
                    )
                    fired = False

                    def trace(frame, event, _arg):
                        nonlocal fired
                        if (
                            not fired
                            and event == "line"
                            and frame.f_code is materialize_code
                            and frame.f_lineno == target_line
                        ):
                            fired = True
                            sys.settrace(None)
                            raise injected
                        return trace

                    sys.settrace(trace)
                    try:
                        if boundary == "tls_descriptor_offer":
                            with (
                                mock.patch.object(
                                    durable_google_login_app
                                    ._PendingTlsHandshake,
                                    "_cancel_wrap_result_preserving_primary",
                                    return_value=None,
                                ),
                                self.assertRaises(
                                    exception_type
                                ) as caught,
                            ):
                                lease.wrap(context)
                        else:
                            with self.assertRaises(
                                exception_type
                            ) as caught:
                                lease.wrap(context)
                        self.assertIs(caught.exception, injected)
                    finally:
                        sys.settrace(None)
                    self.assertTrue(fired)
                    if boundary == "tls_descriptor_offer":
                        with lease._lock:
                            lease._state = "cancelled"
                        self.assertFalse(lease.terminal())
                        self.assertEqual(
                            len(lease._wrapped_descriptor_offer),
                            1,
                        )
                        self.assertEqual(
                            lease._wrapped_socket_offer[0].fileno(),
                            -1,
                        )
                        with mock.patch.object(
                            durable_google_login_app.socket,
                            "close",
                            side_effect=AssertionError(
                                "numeric_descriptor_close_forbidden"
                            ),
                        ) as numeric_close:
                            self.assertTrue(lease.close())
                        numeric_close.assert_not_called()
                    self.assertTrue(lease.terminal())
                    peer.settimeout(1)
                    self.assertEqual(peer.recv(1), b"")
                    peer.close()
                    context = None

        pending_socket_source = "\n".join(
            source[
                durable_google_login_app
                ._PendingTlsHandshake
                .__init__
                .__code__
                .co_firstlineno
                - 1:
                durable_google_login_app
                ._PendingTlsHandshake
                .terminal
                .__code__
                .co_firstlineno
                + 100
            ]
        )
        self.assertNotIn("socket.close(", pending_socket_source)

        accept_initialization = next(
            index
            for index in range(
                accept_code.co_firstlineno - 1,
                accept_code.co_firstlineno + 120,
            )
            if "_socket.socket.__init__(" in source[index]
        )
        accepted_before_retirement_line = next(
            index + 1
            for index in range(
                accept_initialization + 1,
                accept_initialization + 20,
            )
            if source[index].strip() == "with self._lock:"
        )
        real_close_socket = (
            durable_google_login_app._close_socket_independently
        )
        for exception_type in exception_types:
            with self.subTest(
                boundary="ambiguous_object_close_descriptor_reuse",
                exception_type=exception_type.__name__,
            ):
                left, peer = socket.socketpair()
                accepted_descriptor = left.detach()

                class ReuseListener:
                    family = peer.family
                    type = peer.type
                    proto = peer.proto

                    @staticmethod
                    def _accept():
                        return accepted_descriptor, (
                            "127.0.0.1",
                            43220,
                        )

                    @staticmethod
                    def gettimeout():
                        return None

                lease = (
                    durable_google_login_app
                    ._PendingTlsHandshake()
                )
                adoption_control = exception_type(
                    "PRIVATE_ACCEPT_ADOPTION_CONTROL"
                )
                close_control = exception_type(
                    "PRIVATE_AMBIGUOUS_CLOSE_CONTROL"
                )
                fired = False

                def adoption_trace(frame, event, _arg):
                    nonlocal fired
                    if (
                        not fired
                        and event == "line"
                        and frame.f_code is accept_code
                        and frame.f_lineno
                        == accepted_before_retirement_line
                    ):
                        fired = True
                        sys.settrace(None)
                        raise adoption_control
                    return adoption_trace

                sys.settrace(adoption_trace)
                try:
                    with self.assertRaises(
                        exception_type
                    ) as caught:
                        lease.accept_from(ReuseListener())
                    self.assertIs(caught.exception, adoption_control)
                finally:
                    sys.settrace(None)
                self.assertTrue(fired)
                lease.cancel_accept()
                self.assertFalse(lease.terminal())
                self.assertEqual(
                    lease._raw_socket_offer[0].fileno(),
                    accepted_descriptor,
                )

                replacement, replacement_peer = socket.socketpair()
                replacement_timeout = replacement.gettimeout()
                replacement_type = replacement.getsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_TYPE,
                )
                replacement_generations = {
                    accepted_descriptor: replacement,
                }
                live_close_calls = 0

                def close_then_interrupt(candidate):
                    nonlocal live_close_calls
                    self.assertEqual(
                        candidate.fileno(),
                        accepted_descriptor,
                    )
                    self.assertTrue(real_close_socket(candidate))
                    live_close_calls += 1
                    raise close_control

                def stale_numeric_close(descriptor):
                    replacement_generations[descriptor].close()
                    raise AssertionError(
                        "reused_descriptor_was_closed"
                    )

                try:
                    with (
                        mock.patch.object(
                            durable_google_login_app,
                            "_close_socket_independently",
                            side_effect=close_then_interrupt,
                        ) as stable_object_close,
                        mock.patch.object(
                            durable_google_login_app.socket,
                            "close",
                            side_effect=stale_numeric_close,
                        ) as numeric_close,
                    ):
                        if exception_type is RuntimeError:
                            with self.assertRaises(
                                durable_google_login_app
                                ._ServerCleanupFailure
                            ):
                                lease.close()
                        else:
                            with self.assertRaises(
                                exception_type
                            ) as caught:
                                lease.close()
                            self.assertIs(
                                caught.exception,
                                close_control,
                            )
                    numeric_close.assert_not_called()
                    self.assertEqual(
                        stable_object_close.call_count,
                        1,
                    )
                    self.assertEqual(live_close_calls, 1)
                    self.assertTrue(lease.terminal())
                    self.assertTrue(lease.close())
                    self.assertEqual(live_close_calls, 1)
                    self.assertIs(
                        replacement_generations[
                            accepted_descriptor
                        ],
                        replacement,
                    )
                    self.assertEqual(
                        replacement.gettimeout(),
                        replacement_timeout,
                    )
                    self.assertEqual(
                        replacement.getsockopt(
                            socket.SOL_SOCKET,
                            socket.SO_TYPE,
                        ),
                        replacement_type,
                    )
                    replacement.sendall(b"x")
                    self.assertEqual(
                        replacement_peer.recv(1),
                        b"x",
                    )
                    replacement_peer.sendall(b"y")
                    self.assertEqual(replacement.recv(1), b"y")
                    peer.settimeout(1)
                    self.assertEqual(peer.recv(1), b"")
                finally:
                    lease.close()
                    replacement.close()
                    replacement_peer.close()
                    peer.close()

        tls_initialization = next(
            index
            for index in range(
                materialize_code.co_firstlineno - 1,
                materialize_code.co_firstlineno + 180,
            )
            if "_socket.socket.__init__(" in source[index]
        )
        tls_before_retirement_line = next(
            index + 1
            for index in range(
                tls_initialization + 1,
                tls_initialization + 20,
            )
            if source[index].strip() == "with self._lock:"
        )
        for exception_type in exception_types:
            with self.subTest(
                boundary="tls_ambiguous_close_descriptor_reuse",
                exception_type=exception_type.__name__,
            ):
                request, peer = socket.socketpair()
                lease = (
                    durable_google_login_app
                    ._PendingTlsHandshake(request)
                )
                lease.begin_handshake()
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                adoption_control = exception_type(
                    "PRIVATE_TLS_ADOPTION_CONTROL"
                )
                close_control = exception_type(
                    "PRIVATE_TLS_AMBIGUOUS_CLOSE_CONTROL"
                )
                fired = False

                def adoption_trace(frame, event, _arg):
                    nonlocal fired
                    if (
                        not fired
                        and event == "line"
                        and frame.f_code is materialize_code
                        and frame.f_lineno
                        == tls_before_retirement_line
                    ):
                        fired = True
                        sys.settrace(None)
                        raise adoption_control
                    return adoption_trace

                sys.settrace(adoption_trace)
                try:
                    with (
                        mock.patch.object(
                            durable_google_login_app
                            ._PendingTlsHandshake,
                            "_cancel_wrap_result_preserving_primary",
                            return_value=None,
                        ),
                        self.assertRaises(
                            exception_type
                        ) as caught,
                    ):
                        lease.wrap(context)
                    self.assertIs(caught.exception, adoption_control)
                finally:
                    sys.settrace(None)
                self.assertTrue(fired)
                with lease._lock:
                    lease._state = "cancelled"
                detached_descriptor = (
                    lease._wrapped_descriptor_offer[0]
                )
                self.assertEqual(
                    lease._wrapped_socket_offer[0].fileno(),
                    detached_descriptor,
                )
                self.assertFalse(lease.terminal())

                replacement, replacement_peer = socket.socketpair()
                replacement_timeout = replacement.gettimeout()
                replacement_type = replacement.getsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_TYPE,
                )
                replacement_generations = {
                    detached_descriptor: replacement,
                }
                live_close_calls = 0

                def close_then_interrupt(candidate):
                    nonlocal live_close_calls
                    descriptor = candidate.fileno()
                    terminal = real_close_socket(candidate)
                    self.assertTrue(terminal)
                    if descriptor == detached_descriptor:
                        live_close_calls += 1
                        raise close_control
                    self.assertLess(descriptor, 0)
                    return terminal

                def stale_numeric_close(descriptor):
                    replacement_generations[descriptor].close()
                    raise AssertionError(
                        "reused_tls_descriptor_was_closed"
                    )

                try:
                    with (
                        mock.patch.object(
                            durable_google_login_app,
                            "_close_socket_independently",
                            side_effect=close_then_interrupt,
                        ) as stable_object_close,
                        mock.patch.object(
                            durable_google_login_app.socket,
                            "close",
                            side_effect=stale_numeric_close,
                        ) as numeric_close,
                    ):
                        if exception_type is RuntimeError:
                            with self.assertRaises(
                                durable_google_login_app
                                ._ServerCleanupFailure
                            ):
                                lease.close()
                        else:
                            with self.assertRaises(
                                exception_type
                            ) as caught:
                                lease.close()
                            self.assertIs(
                                caught.exception,
                                close_control,
                            )
                    numeric_close.assert_not_called()
                    self.assertEqual(
                        stable_object_close.call_count,
                        2,
                    )
                    self.assertEqual(live_close_calls, 1)
                    self.assertTrue(lease.terminal())
                    self.assertTrue(lease.close())
                    self.assertEqual(live_close_calls, 1)
                    self.assertIs(
                        replacement_generations[
                            detached_descriptor
                        ],
                        replacement,
                    )
                    self.assertEqual(
                        replacement.gettimeout(),
                        replacement_timeout,
                    )
                    self.assertEqual(
                        replacement.getsockopt(
                            socket.SOL_SOCKET,
                            socket.SO_TYPE,
                        ),
                        replacement_type,
                    )
                    replacement.sendall(b"t")
                    self.assertEqual(
                        replacement_peer.recv(1),
                        b"t",
                    )
                    replacement_peer.sendall(b"u")
                    self.assertEqual(replacement.recv(1), b"u")
                    peer.settimeout(1)
                    self.assertEqual(peer.recv(1), b"")
                finally:
                    lease.close()
                    replacement.close()
                    replacement_peer.close()
                    peer.close()
                    context = None

        normalize_code = (
            durable_google_login_app
            ._PendingTlsHandshake
            ._normalize_descriptor_offer_locked
            .__code__
        )
        normalize_start = normalize_code.co_firstlineno - 1
        normalize_adopted_line = next(
            index + 1
            for index in range(
                normalize_start,
                normalize_start + 100,
            )
            if "if request.fileno() != descriptor:" in source[index]
        )
        descriptor_retired_index = next(
            index
            for index in range(
                normalize_start,
                normalize_start + 100,
            )
            if source[index].strip() == "descriptor_offer.clear()"
        )
        normalize_retired_line = next(
            index + 1
            for index in range(
                descriptor_retired_index + 1,
                descriptor_retired_index + 10,
            )
            if source[index].strip() == "if wrapped:"
        )

        def unresolved_descriptor_lease(kind):
            request, peer = socket.socketpair()
            parameters = (
                request.family,
                request.type,
                request.proto,
            )
            descriptor = request.detach()
            if kind == "raw":
                lease = (
                    durable_google_login_app
                    ._PendingTlsHandshake()
                )
                with lease._lock:
                    lease._accepted_descriptor_parameters = (
                        parameters
                    )
                    lease._accepted_descriptor_offer.append(
                        (
                            descriptor,
                            ("127.0.0.1", 43221),
                        )
                    )
                    lease._state = "cancelled"
                context = None
            else:
                lease = (
                    durable_google_login_app
                    ._PendingTlsHandshake(
                        socket.socket(
                            *parameters,
                            fileno=descriptor,
                        )
                    )
                )
                context = ssl.SSLContext(
                    ssl.PROTOCOL_TLS_SERVER
                )
                socket_class = context.sslsocket_class
                wrapped = socket_class.__new__(socket_class)
                wrapped._io_refs = 0
                wrapped._closed = False
                wrapped._sslobj = None
                source_socket = lease._socket
                detached = (
                    durable_google_login_app
                    ._socket
                    .socket
                    .detach(source_socket)
                )
                with lease._lock:
                    lease._wrapped_socket_offer.append(wrapped)
                    lease._wrapped_descriptor_parameters = (
                        parameters
                    )
                    lease._wrapped_descriptor_offer.append(
                        detached
                    )
                    lease._state = "cancelled"
                descriptor = detached
            return lease, peer, descriptor, context

        for kind in ("raw", "tls"):
            for boundary, target_line in (
                ("adopted_before_retirement", normalize_adopted_line),
                ("retired_before_parameter_commit", normalize_retired_line),
            ):
                for exception_type in exception_types:
                    with self.subTest(
                        kind=kind,
                        boundary=boundary,
                        exception_type=exception_type.__name__,
                    ):
                        (
                            lease,
                            peer,
                            descriptor,
                            context,
                        ) = unresolved_descriptor_lease(kind)
                        injected = exception_type(
                            "PRIVATE_DESCRIPTOR_NORMALIZE_CONTROL"
                        )
                        fired = False

                        def normalize_trace(frame, event, _arg):
                            nonlocal fired
                            if (
                                not fired
                                and event == "line"
                                and frame.f_code is normalize_code
                                and frame.f_lineno == target_line
                            ):
                                fired = True
                                sys.settrace(None)
                                raise injected
                            return normalize_trace

                        sys.settrace(normalize_trace)
                        try:
                            if exception_type is RuntimeError:
                                self.assertFalse(lease.close())
                            else:
                                with self.assertRaises(
                                    exception_type
                                ) as caught:
                                    lease.close()
                                self.assertIs(
                                    caught.exception,
                                    injected,
                                )
                        finally:
                            sys.settrace(None)
                        self.assertTrue(fired)
                        if (
                            boundary
                            == "adopted_before_retirement"
                        ):
                            descriptor_offer = (
                                lease._accepted_descriptor_offer
                                if kind == "raw"
                                else lease._wrapped_descriptor_offer
                            )
                            socket_offer = (
                                lease._raw_socket_offer
                                if kind == "raw"
                                else lease._wrapped_socket_offer
                            )
                            self.assertEqual(
                                len(descriptor_offer),
                                1,
                            )
                            self.assertEqual(
                                socket_offer[0].fileno(),
                                descriptor,
                            )
                            self.assertFalse(lease.terminal())
                        else:
                            self.assertTrue(lease.terminal())
                        self.assertTrue(lease.close())
                        self.assertTrue(lease.terminal())
                        peer.settimeout(1)
                        self.assertEqual(peer.recv(1), b"")
                        peer.close()
                        context = None

    def test_tls_context_is_scope_owned_during_configuration_failure(self):
        from scripts import durable_google_login_app

        exception_types = (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        )
        for exception_type in exception_types:
            with self.subTest(exception_type=exception_type.__name__):
                scope = (
                    durable_google_login_app
                    ._EphemeralTlsContext()
                )
                self.assertTrue(scope.prepare_workspace())
                directory = scope._directory
                injected = exception_type(
                    "PRIVATE_TLS_CONTEXT_CONFIGURATION_CONTROL"
                )

                class Context:
                    @property
                    def minimum_version(self):
                        return None

                    @minimum_version.setter
                    def minimum_version(self, _value):
                        self_test.assertIs(scope._context, self)
                        raise injected

                self_test = self
                context = Context()
                with (
                    mock.patch.object(
                        durable_google_login_app.ssl,
                        "SSLContext",
                        return_value=context,
                    ),
                    self.assertRaises(exception_type) as caught,
                ):
                    scope.build_context()
                self.assertIs(caught.exception, injected)
                self.assertIsNone(scope._context)
                self.assertIsNone(scope._temporary)
                self.assertIsNone(scope._directory)
                self.assertFalse(directory.exists())
                self.assertTrue(scope.close())

    def test_cleanup_owner_release_survives_repeated_control(self):
        from wahojobs import durable_google_login_runtime

        coordinator_type = (
            durable_google_login_runtime._CleanupCoordinator
        )
        release_code = (
            coordinator_type._release_cleanup_owner.__code__
        )
        real_normalize = (
            coordinator_type
            ._normalize_interrupted_entries_locked
        )
        exception_types = (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        )
        for exception_type in exception_types:
            with self.subTest(exception_type=exception_type.__name__):
                coordinator = coordinator_type()
                calls = []
                coordinator.own(
                    "google_gateway",
                    object(),
                    lambda _resource: calls.append("close") or True,
                )
                injected = exception_type(
                    "PRIVATE_REPEATED_RELEASE_CONTROL"
                )
                interruptions = 0

                def interrupting_normalize(active):
                    nonlocal interruptions
                    if (
                        interruptions < 6
                        and sys._getframe(1).f_code is release_code
                    ):
                        interruptions += 1
                        raise injected
                    return real_normalize(active)

                with mock.patch.object(
                    coordinator_type,
                    "_normalize_interrupted_entries_locked",
                    interrupting_normalize,
                ):
                    if exception_type is RuntimeError:
                        report = coordinator.cleanup()
                    else:
                        with self.assertRaises(
                            exception_type
                        ) as caught:
                            coordinator.cleanup()
                        self.assertIs(caught.exception, injected)
                        report = coordinator.snapshot()
                self.assertEqual(interruptions, 6)
                self.assertEqual(calls, ["close"])
                self.assertTrue(report.cleanup_complete)
                self.assertFalse(coordinator._owner_active)
                self.assertIsNone(coordinator._owner_thread)
                self.assertIsNone(coordinator._owner_token)
                self.assertEqual(
                    coordinator._entries[0].state,
                    "terminal",
                )

    def test_incomplete_worker_handoff_retains_retry_authority(self):
        from wahojobs import durable_google_login_runtime

        class Resource:
            def __init__(self, terminal_call):
                self.calls = 0
                self.terminal_call = terminal_call

            def close(self, *, _preserve_primary):
                self.calls += 1
                return self.calls >= self.terminal_call

        transient = Resource(3)
        outcome = (
            durable_google_login_runtime
            ._ConfigurationWorkerOutcome(
                durable_google_login_runtime
                ._WORKER_OUTCOME_CAPABILITY,
                "ok",
                transient,
            )
        )
        self.assertTrue(
            durable_google_login_runtime
            ._close_worker_outcome_value_preserving_primary(
                outcome
            )
        )
        self.assertEqual(transient.calls, 3)
        self.assertIsNone(
            object.__getattribute__(
                outcome,
                "_ConfigurationWorkerOutcome__value",
            )
        )

        unresolved = Resource(6)
        outcome = (
            durable_google_login_runtime
            ._ConfigurationWorkerOutcome(
                durable_google_login_runtime
                ._WORKER_OUTCOME_CAPABILITY,
                "ok",
                unresolved,
            )
        )
        self.assertFalse(
            durable_google_login_runtime
            ._close_worker_outcome_value_preserving_primary(
                outcome
            )
        )
        self.assertEqual(unresolved.calls, 4)
        retained_lease = (
            durable_google_login_runtime
            ._UNRESOLVED_HANDOFFS[id(unresolved)]
        )
        self.assertTrue(
            retained_lease.owns(unresolved)
        )
        self.assertNotIn(
            "Resource",
            repr(retained_lease),
        )
        outcome._replace("failure")
        self.assertTrue(
            durable_google_login_runtime
            ._retry_unresolved_activation_handoffs()
        )
        self.assertEqual(unresolved.calls, 6)
        self.assertNotIn(
            id(unresolved),
            durable_google_login_runtime
            ._UNRESOLVED_HANDOFFS,
        )

    def test_request_thread_remains_tracked_until_native_return(self):
        from scripts import durable_google_login_app

        target_returned = threading.Event()
        release_native_return = threading.Event()
        real_thread_type = threading.Thread

        class PostTargetPauseThread(real_thread_type):
            def run(self):
                super().run()
                target_returned.set()
                release_native_return.wait(2)

        server = durable_google_login_app._DrainingThreadingHTTPServer(
            ("127.0.0.1", 0),
            durable_google_login_app._UnpublishedRequestHandler,
            False,
        )
        request, peer = socket.socketpair()
        outcome = None
        try:
            outcome, _ = self.arm_server_admission(
                durable_google_login_app,
                server,
            )
            server.process_request_thread = (
                lambda _request, _client_address: None
            )
            with server._lifecycle_lock:
                server._accepted_sockets.add(request)
            with mock.patch.object(
                durable_google_login_app.threading,
                "Thread",
                PostTargetPauseThread,
            ):
                server.process_request(
                    request,
                    ("127.0.0.1", 43210),
                )
            self.assertTrue(target_returned.wait(1))
            self.assertEqual(
                server.resource_counts()["accepted_sockets"],
                0,
            )
            self.assertEqual(
                server.resource_counts()["request_threads"],
                1,
            )
            self.assertFalse(server.drain_request_threads(0.01))
            with server._lifecycle_lock:
                tracked = tuple(server._request_threads)
            self.assertEqual(len(tracked), 1)
            self.assertTrue(tracked[0].is_alive())
            release_native_return.set()
            tracked[0].join(1)
            self.assertFalse(tracked[0].is_alive())
            self.assertEqual(
                server.resource_counts()["request_threads"],
                0,
            )
        finally:
            release_native_return.set()
            if outcome is not None:
                self.disarm_server_admission(server, outcome)
            server.close_accepted_sockets()
            server.drain_request_threads(1)
            peer.close()
            server.server_close()

    def test_default_listener_exists_inside_pre_registered_owner(self):
        from scripts import durable_google_login_app

        exception_types = (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        )
        for exception_type in exception_types:
            with self.subTest(exception_type=exception_type.__name__):
                injected = exception_type(
                    "PRIVATE_LISTENER_MATERIALIZATION_CONTROL"
                )

                class InterruptingOwnership(
                    durable_google_login_app._ServerOwnership
                ):
                    def materialize_listener(self, *args, **kwargs):
                        listener = super().materialize_listener(
                            *args,
                            **kwargs,
                        )
                        self.materialized_listener = listener
                        raise injected

                ownership = InterruptingOwnership()
                with self.assertRaises(exception_type) as caught:
                    durable_google_login_app._DrainingThreadingHTTPServer(
                        ("127.0.0.1", 0),
                        durable_google_login_app
                        ._UnpublishedRequestHandler,
                        False,
                        _construction_ownership=ownership,
                    )
                self.assertIs(caught.exception, injected)
                listener = ownership.materialized_listener
                self.assertIs(ownership._listener, listener)
                self.assertTrue(
                    ownership.owns_listener(
                        ownership._server,
                        listener,
                    )
                )
                self.assertTrue(ownership.close_listener())
                self.assertLess(listener.fileno(), 0)
                self.assertTrue(ownership.close_high_level())
                self.assertTrue(ownership.listener_terminal())
                self.assertTrue(ownership.high_level_terminal())

    def test_default_socket_materialization_defers_concurrent_close(self):
        from scripts import durable_google_login_app

        source = Path(
            durable_google_login_app.__file__
        ).read_text(encoding="utf-8").splitlines()

        def line_number(text, start):
            return next(
                index + 1
                for index in range(start - 1, len(source))
                if text in source[index]
            )

        def run_with_barrier(target, line, action):
            reached = threading.Event()
            release = threading.Event()
            failures = []

            def trace(frame, event, _arg):
                if (
                    frame.f_code is target
                    and event == "line"
                    and frame.f_lineno == line
                ):
                    reached.set()
                    release.wait(2)
                return trace

            def worker():
                try:
                    sys.settrace(trace)
                    action()
                except (
                    Exception,
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    failures.append(exc)
                finally:
                    sys.settrace(None)

            thread = threading.Thread(target=worker, daemon=False)
            thread.start()
            self.assertTrue(reached.wait(1))
            return thread, release, failures

        listener = socket.socket()
        client = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client.connect(listener.getsockname())
        raw_lease = durable_google_login_app._PendingTlsHandshake()
        accept_line = line_number(
            "descriptor_record = self._accepted_descriptor_offer[0]",
            raw_lease.accept_from.__code__.co_firstlineno,
        )

        def accept_action():
            try:
                raw_lease.accept_from(listener)
            finally:
                raw_lease.cancel_accept()
                raw_lease.close()

        thread, release, failures = run_with_barrier(
            raw_lease.accept_from.__code__,
            accept_line,
            accept_action,
        )
        try:
            self.assertFalse(raw_lease.close())
            self.assertFalse(raw_lease.terminal())
            release.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], OSError)
            self.assertTrue(raw_lease.terminal())
            client.settimeout(1)
            self.assertEqual(client.recv(1), b"")
        finally:
            release.set()
            thread.join(2)
            raw_lease.close()
            client.close()
            listener.close()

        request, peer = socket.socketpair()
        tls_lease = durable_google_login_app._PendingTlsHandshake(
            request
        )
        tls_lease.begin_handshake()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_line = line_number(
            "descriptor = self._wrapped_descriptor_offer[0]",
            tls_lease._materialize_owned_tls_socket
            .__code__.co_firstlineno,
        )
        thread, release, failures = run_with_barrier(
            tls_lease._materialize_owned_tls_socket.__code__,
            tls_line,
            lambda: tls_lease.wrap(context),
        )
        try:
            self.assertFalse(tls_lease.close())
            self.assertFalse(tls_lease.terminal())
            release.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], OSError)
            self.assertTrue(tls_lease.terminal())
            peer.settimeout(1)
            self.assertEqual(peer.recv(1), b"")
        finally:
            release.set()
            thread.join(2)
            tls_lease.close()
            peer.close()
            context = None

        class PythonDetachTrap(socket.socket):
            def detach(self):
                raise AssertionError("python_detach_must_not_run")

        request, peer = socket.socketpair()
        trapped = PythonDetachTrap(fileno=request.detach())
        request = None
        tls_lease = durable_google_login_app._PendingTlsHandshake(
            trapped
        )
        tls_lease.begin_handshake()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            wrapped = tls_lease.wrap(context)
            self.assertIsInstance(wrapped, ssl.SSLSocket)
            self.assertTrue(tls_lease.close())
            self.assertTrue(tls_lease.terminal())
        finally:
            tls_lease.close()
            peer.close()
            context = None

        terminal_code = (
            durable_google_login_app
            ._PendingTlsHandshake
            .terminal
            .__code__
        )
        terminal_probe_line = line_number(
            (
                "primary_closed = primary is None "
                "or _socket_is_closed(primary)"
            ),
            terminal_code.co_firstlineno,
        )

        raw_source, raw_peer = socket.socketpair()
        raw_parameters = (
            raw_source.family,
            raw_source.type,
            raw_source.proto,
        )
        raw_descriptor = raw_source.detach()
        raw_lease = durable_google_login_app._PendingTlsHandshake()
        raw_accept_entered = threading.Event()
        raw_accept_release = threading.Event()
        raw_accept_results = []
        raw_accept_failures = []
        raw_terminal_results = []
        raw_accept_thread = None

        class StaleSnapshotListener:
            family, type, proto = raw_parameters

            @staticmethod
            def _accept():
                raw_accept_entered.set()
                if not raw_accept_release.wait(2):
                    raise RuntimeError("raw_accept_barrier_timeout")
                return raw_descriptor, ("127.0.0.1", 43222)

            @staticmethod
            def gettimeout():
                return None

        def raw_accept_worker():
            try:
                raw_accept_results.append(
                    raw_lease.accept_from(StaleSnapshotListener())
                )
            except (
                Exception,
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                raw_accept_failures.append(exc)

        (
            raw_terminal_thread,
            raw_terminal_release,
            raw_terminal_failures,
        ) = run_with_barrier(
            terminal_code,
            terminal_probe_line,
            lambda: raw_terminal_results.append(
                raw_lease.terminal()
            ),
        )
        try:
            raw_accept_thread = threading.Thread(
                target=raw_accept_worker,
                daemon=False,
            )
            raw_accept_thread.start()
            self.assertTrue(raw_accept_entered.wait(1))
            raw_terminal_release.set()
            raw_terminal_thread.join(2)
            self.assertFalse(raw_terminal_thread.is_alive())
            self.assertEqual(raw_terminal_failures, [])
            self.assertEqual(raw_terminal_results, [False])
            with raw_lease._lock:
                self.assertNotEqual(raw_lease._state, "closed")
                self.assertEqual(
                    len(raw_lease._raw_socket_offer),
                    1,
                )
                self.assertIsNotNone(
                    raw_lease._accepted_descriptor_parameters
                )
            raw_accept_release.set()
            raw_accept_thread.join(2)
            self.assertFalse(raw_accept_thread.is_alive())
            self.assertEqual(raw_accept_failures, [])
            self.assertEqual(len(raw_accept_results), 1)
            self.assertTrue(
                raw_lease.owns_socket(raw_accept_results[0][0])
            )
            self.assertTrue(raw_lease.close())
            self.assertTrue(raw_lease.terminal())
            raw_peer.settimeout(1)
            self.assertEqual(raw_peer.recv(1), b"")
        finally:
            raw_terminal_release.set()
            raw_accept_release.set()
            raw_terminal_thread.join(2)
            if raw_accept_thread is not None:
                raw_accept_thread.join(2)
            raw_lease.cancel_accept()
            for _attempt in range(3):
                try:
                    if raw_lease.close():
                        break
                except (
                    Exception,
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ):
                    pass
            with raw_lease._lock:
                raw_pending = (
                    raw_lease._accepted_descriptor_offer[0][0]
                    if raw_lease._accepted_descriptor_offer
                    else None
                )
                raw_candidates = tuple(
                    candidate
                    for candidate in (
                        raw_lease._socket,
                        raw_lease._secondary_socket,
                        *raw_lease._raw_socket_offer,
                    )
                    if candidate is not None
                )
            if raw_pending is not None and not any(
                candidate.fileno() == raw_pending
                for candidate in raw_candidates
            ):
                try:
                    socket.socket(
                        *raw_parameters,
                        fileno=raw_pending,
                    ).close()
                except OSError:
                    pass
            raw_peer.close()

        tls_request, tls_peer = socket.socketpair()
        tls_parameters = (
            tls_request.family,
            tls_request.type,
            tls_request.proto,
        )
        tls_lease = durable_google_login_app._PendingTlsHandshake(
            tls_request
        )
        tls_lease.begin_handshake()
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_detached = threading.Event()
        tls_publication_release = threading.Event()
        tls_detached_offers = []
        tls_wrap_results = []
        tls_wrap_failures = []
        tls_terminal_results = []
        tls_wrap_thread = None
        real_publish_call_result = (
            durable_google_login_app._publish_call_result
        )

        def controlled_publish(offer, callback, arguments):
            if offer is tls_lease._wrapped_descriptor_offer:
                publication = callback(*arguments)
                tls_detached_offers.append(publication)
                tls_detached.set()
                if not tls_publication_release.wait(2):
                    raise RuntimeError(
                        "tls_publication_barrier_timeout"
                    )
                offer.append(publication)
                return True
            return real_publish_call_result(
                offer,
                callback,
                arguments,
            )

        def tls_wrap_worker():
            try:
                tls_wrap_results.append(
                    tls_lease.wrap(tls_context)
                )
            except (
                Exception,
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                tls_wrap_failures.append(exc)

        (
            tls_terminal_thread,
            tls_terminal_release,
            tls_terminal_failures,
        ) = run_with_barrier(
            terminal_code,
            terminal_probe_line,
            lambda: tls_terminal_results.append(
                tls_lease.terminal()
            ),
        )
        try:
            with mock.patch.object(
                durable_google_login_app,
                "_publish_call_result",
                side_effect=controlled_publish,
            ):
                tls_wrap_thread = threading.Thread(
                    target=tls_wrap_worker,
                    daemon=False,
                )
                tls_wrap_thread.start()
                self.assertTrue(tls_detached.wait(1))
                tls_terminal_release.set()
                tls_terminal_thread.join(2)
                self.assertFalse(tls_terminal_thread.is_alive())
                self.assertEqual(tls_terminal_failures, [])
                self.assertEqual(tls_terminal_results, [False])
                with tls_lease._lock:
                    self.assertNotEqual(tls_lease._state, "closed")
                    self.assertEqual(
                        len(tls_lease._wrapped_socket_offer),
                        1,
                    )
                    self.assertIsNotNone(
                        tls_lease._wrapped_descriptor_parameters
                    )
                tls_publication_release.set()
                tls_wrap_thread.join(2)
            self.assertFalse(tls_wrap_thread.is_alive())
            self.assertEqual(tls_wrap_failures, [])
            self.assertEqual(len(tls_wrap_results), 1)
            self.assertTrue(
                tls_lease.owns_socket(tls_wrap_results[0])
            )
            self.assertTrue(tls_lease.close())
            self.assertTrue(tls_lease.terminal())
            tls_peer.settimeout(1)
            self.assertEqual(tls_peer.recv(1), b"")
        finally:
            tls_terminal_release.set()
            tls_publication_release.set()
            tls_terminal_thread.join(2)
            if tls_wrap_thread is not None:
                tls_wrap_thread.join(2)
            for _attempt in range(3):
                try:
                    if tls_lease.close():
                        break
                except (
                    Exception,
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ):
                    pass
            for failure in tls_wrap_failures:
                durable_google_login_app._sanitize_launcher_exception(
                    failure
                )
            with tls_lease._lock:
                tls_pending = (
                    tls_lease._wrapped_descriptor_offer[0]
                    if tls_lease._wrapped_descriptor_offer
                    else None
                )
                tls_candidates = tuple(
                    candidate
                    for candidate in (
                        tls_lease._socket,
                        tls_lease._secondary_socket,
                        *tls_lease._wrapped_socket_offer,
                    )
                    if candidate is not None
                )
            if (
                tls_pending is not None
                and tls_pending in tls_detached_offers
                and not any(
                    candidate.fileno() == tls_pending
                    for candidate in tls_candidates
                )
            ):
                try:
                    socket.socket(
                        *tls_parameters,
                        fileno=tls_pending,
                    ).close()
                except OSError:
                    pass
            tls_peer.close()
            tls_context = None

    def test_activation_publication_gate_prevents_unresolved_overlap(self):
        from wahojobs import durable_google_login_runtime

        class Resource:
            def __init__(self):
                self.close_calls = 0
                self.finish = False

            def close(self, *, _preserve_primary=False):
                self.close_calls += 1
                return self.finish

        resource = Resource()
        first_entered = threading.Event()
        release_first = threading.Event()
        second_worker_entered = threading.Event()
        calls = []
        errors = []

        def fake_run(_worker, _arguments, outcome):
            calls.append(threading.current_thread())
            if len(calls) == 1:
                first_entered.set()
                release_first.wait(2)
                durable_google_login_runtime\
                    ._retain_unresolved_activation_handoff(resource)
                outcome._publish("failure")
                return None
            second_worker_entered.set()
            outcome._publish("ok", object())
            return None

        def build():
            try:
                durable_google_login_runtime\
                    .build_durable_google_login_runtime("unused")
            except (
                Exception,
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                errors.append(exc)

        first = threading.Thread(target=build, daemon=False)
        second = threading.Thread(target=build, daemon=False)
        try:
            with mock.patch.object(
                durable_google_login_runtime,
                "_run_configuration_worker",
                side_effect=fake_run,
            ):
                first.start()
                self.assertTrue(first_entered.wait(1))
                second.start()
                self.assertFalse(second_worker_entered.wait(0.05))
                release_first.set()
                first.join(2)
                second.join(2)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertFalse(second_worker_entered.is_set())
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(errors), 2)
            self.assertTrue(
                all(
                    isinstance(
                        error,
                        DurableGoogleLoginConfigurationError,
                    )
                    for error in errors
                )
            )
            with durable_google_login_runtime\
                    ._UNRESOLVED_HANDOFF_LOCK:
                self.assertEqual(
                    len(
                        durable_google_login_runtime
                        ._UNRESOLVED_HANDOFFS
                    ),
                    1,
                )
        finally:
            release_first.set()
            first.join(2)
            second.join(2)
            resource.finish = True
            self.assertTrue(
                durable_google_login_runtime
                ._retry_unresolved_activation_handoffs()
            )

        nested_errors = []

        def reentrant_run(_worker, _arguments, outcome):
            try:
                durable_google_login_runtime\
                    .load_durable_google_login_configuration("unused")
            except DurableGoogleLoginConfigurationError as exc:
                nested_errors.append(exc)
            outcome._publish("failure")

        started = time.monotonic()
        with (
            mock.patch.object(
                durable_google_login_runtime,
                "_run_configuration_worker",
                side_effect=reentrant_run,
            ),
            self.assertRaises(DurableGoogleLoginConfigurationError),
        ):
            durable_google_login_runtime\
                .build_durable_google_login_runtime("unused")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(len(nested_errors), 1)

        gate_type = (
            durable_google_login_runtime._ActivationPublicationGate
        )
        gate_source = Path(
            durable_google_login_runtime.__file__
        ).read_text(encoding="utf-8").splitlines()
        enter_code = gate_type.__enter__.__code__
        enter_owner_line = next(
            index + 1
            for index in range(
                enter_code.co_firstlineno - 1,
                enter_code.co_firstlineno + 80,
            )
            if "self.__owner = caller" in gate_source[index]
        )
        exception_types = (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        )
        for exception_type in exception_types:
            with self.subTest(
                boundary="claim_after_token",
                exception_type=exception_type.__name__,
            ):
                gate = gate_type(0.05)
                injected = exception_type(
                    "PRIVATE_ACTIVATION_GATE_CLAIM_CONTROL"
                )
                fired = False

                def trace(frame, event, _arg):
                    nonlocal fired
                    if (
                        not fired
                        and event == "line"
                        and frame.f_code is enter_code
                        and frame.f_lineno == enter_owner_line
                    ):
                        fired = True
                        sys.settrace(None)
                        raise injected
                    return trace

                sys.settrace(trace)
                try:
                    with self.assertRaises(
                        exception_type
                    ) as caught:
                        with gate:
                            self.fail("gate_body_not_expected")
                    self.assertIs(caught.exception, injected)
                finally:
                    sys.settrace(None)
                self.assertTrue(fired)
                with gate:
                    pass

        exit_code = gate_type.__exit__.__code__
        exit_release_line = next(
            index + 1
            for index in range(
                exit_code.co_firstlineno - 1,
                exit_code.co_firstlineno + 80,
            )
            if "released = self._release(" in gate_source[index]
        )
        for exception_type in exception_types:
            with self.subTest(
                boundary="exit_before_release",
                exception_type=exception_type.__name__,
            ):
                gate = gate_type(0.05)
                injected = exception_type(
                    "PRIVATE_ACTIVATION_GATE_EXIT_CONTROL"
                )
                fired = False

                def trace(frame, event, _arg):
                    nonlocal fired
                    if (
                        not fired
                        and event == "line"
                        and frame.f_code is exit_code
                        and frame.f_lineno == exit_release_line
                    ):
                        fired = True
                        sys.settrace(None)
                        raise injected
                    return trace

                sys.settrace(trace)
                try:
                    with gate:
                        pass
                finally:
                    sys.settrace(None)
                self.assertTrue(fired)
                with gate:
                    pass

        release_code = gate_type._release.__code__
        release_owner_line = next(
            index + 1
            for index in range(
                release_code.co_firstlineno - 1,
                release_code.co_firstlineno + 80,
            )
            if "self.__owner = None" in gate_source[index]
        )

        class PublishedResource:
            def __init__(self):
                self.close_calls = 0
                self.finish = False

            def close(self, *, _preserve_primary=False):
                self.close_calls += 1
                return self.finish

        for exception_type in exception_types:
            with self.subTest(
                boundary="release_after_token",
                exception_type=exception_type.__name__,
            ):
                published = PublishedResource()
                injected = exception_type(
                    "PRIVATE_ACTIVATION_GATE_RELEASE_CONTROL"
                )
                fired = False

                def publish_ok(_worker, _arguments, outcome):
                    outcome._publish("ok", published)

                def trace(frame, event, _arg):
                    nonlocal fired
                    if (
                        not fired
                        and event == "line"
                        and frame.f_code is release_code
                        and frame.f_lineno == release_owner_line
                    ):
                        fired = True
                        sys.settrace(None)
                        raise injected
                    return trace

                sys.settrace(trace)
                try:
                    with mock.patch.object(
                        durable_google_login_runtime,
                        "_run_configuration_worker",
                        side_effect=publish_ok,
                    ):
                        delivered = (
                            durable_google_login_runtime
                            .build_durable_google_login_runtime(
                                "unused"
                            )
                        )
                        self.assertIs(delivered, published)
                        published.finish = True
                        self.assertTrue(delivered.close())
                finally:
                    sys.settrace(None)
                self.assertTrue(fired)
                with durable_google_login_runtime\
                        ._UNRESOLVED_HANDOFF_LOCK:
                    self.assertFalse(
                        durable_google_login_runtime
                        ._UNRESOLVED_HANDOFFS
                    )

        build_code = (
            durable_google_login_runtime
            .build_durable_google_login_runtime
            .__code__
        )
        committed_return_line = next(
            index + 1
            for index in range(
                build_code.co_firstlineno - 1,
                build_code.co_firstlineno + 120,
            )
            if gate_source[index].strip() == "return result"
        )
        for exception_type in exception_types:
            with self.subTest(
                boundary="committed_return",
                exception_type=exception_type.__name__,
            ):
                published = PublishedResource()
                injected = exception_type(
                    "PRIVATE_COMMITTED_RETURN_CONTROL"
                )
                fired = False

                def publish_ok(_worker, _arguments, outcome):
                    outcome._publish("ok", published)

                def trace(frame, event, _arg):
                    nonlocal fired
                    if (
                        not fired
                        and event == "line"
                        and frame.f_code is build_code
                        and frame.f_lineno == committed_return_line
                    ):
                        fired = True
                        sys.settrace(None)
                        raise injected
                    return trace

                sys.settrace(trace)
                try:
                    with mock.patch.object(
                        durable_google_login_runtime,
                        "_run_configuration_worker",
                        side_effect=publish_ok,
                    ):
                        delivered = (
                            durable_google_login_runtime
                            .build_durable_google_login_runtime(
                                "unused"
                            )
                        )
                finally:
                    sys.settrace(None)
                self.assertTrue(fired)
                self.assertIs(delivered, published)
                published.finish = True
                self.assertTrue(delivered.close())
                with durable_google_login_runtime\
                        ._UNRESOLVED_HANDOFF_LOCK:
                    self.assertFalse(
                        durable_google_login_runtime
                        ._UNRESOLVED_HANDOFFS
                    )

        retain_code = (
            durable_google_login_runtime
            ._retain_unresolved_activation_handoff
            .__code__
        )
        retain_boundaries = tuple(
            index + 1
            for index in range(
                retain_code.co_firstlineno - 1,
                retain_code.co_firstlineno + 100,
            )
            if (
                "_UNRESOLVED_HANDOFFS[identifier] = lease"
                in gate_source[index]
                or "retained = lease" in gate_source[index]
            )
        )
        self.assertEqual(len(retain_boundaries), 2)
        for target_line in retain_boundaries:
            for exception_type in exception_types:
                with self.subTest(
                    boundary=f"retain_{target_line}",
                    exception_type=exception_type.__name__,
                ):
                    published = PublishedResource()
                    published.finish = True
                    injected = exception_type(
                        "PRIVATE_HANDOFF_RETENTION_CONTROL"
                    )
                    fired = False

                    def trace(frame, event, _arg):
                        nonlocal fired
                        if (
                            not fired
                            and event == "line"
                            and frame.f_code is retain_code
                            and frame.f_lineno == target_line
                        ):
                            fired = True
                            sys.settrace(None)
                            raise injected
                        return trace

                    sys.settrace(trace)
                    try:
                        lease = (
                            durable_google_login_runtime
                            ._retain_unresolved_activation_handoff(
                                published
                            )
                        )
                    finally:
                        sys.settrace(None)
                    self.assertTrue(fired)
                    self.assertTrue(lease.owns(published))
                    self.assertTrue(lease.close())
                    self.assertTrue(
                        durable_google_login_runtime
                        ._forget_unresolved_activation_handoff(
                            lease
                        )
                    )

        published = PublishedResource()
        published.finish = True
        class RepeatedFailureRegistry(dict):
            def __init__(self):
                super().__init__()
                self.failures = 0

            def __setitem__(self, key, value):
                if self.failures < 4:
                    self.failures += 1
                    raise RuntimeError(
                        "PRIVATE_REPEATED_RETENTION_FAILURE"
                    )
                return super().__setitem__(key, value)

        failing_registry = RepeatedFailureRegistry()
        with mock.patch.object(
            durable_google_login_runtime,
            "_UNRESOLVED_HANDOFFS",
            failing_registry,
        ):
            lease = (
                durable_google_login_runtime
                ._retain_unresolved_activation_handoff(published)
            )
        self.assertEqual(failing_registry.failures, 4)
        self.assertTrue(
            any(
                candidate is lease
                for candidate in (
                    durable_google_login_runtime
                    ._EMERGENCY_HANDOFF_LEASES
                )
            )
        )
        self.assertTrue(lease.owns(published))
        self.assertTrue(lease.close())
        reset_code = lease.reset_terminal.__code__
        reset_line = next(
            index + 1
            for index in range(
                reset_code.co_firstlineno - 1,
                reset_code.co_firstlineno + 40,
            )
            if 'self.__state = "vacant"' in gate_source[index]
        )
        reset_injected = RuntimeError(
            "PRIVATE_EMERGENCY_LEASE_RESET_FAILURE"
        )
        reset_fired = False

        def reset_trace(frame, event, _arg):
            nonlocal reset_fired
            if (
                not reset_fired
                and event == "line"
                and frame.f_code is reset_code
                and frame.f_lineno == reset_line
            ):
                reset_fired = True
                sys.settrace(None)
                raise reset_injected
            return reset_trace

        sys.settrace(reset_trace)
        try:
            with self.assertRaises(RuntimeError) as caught:
                durable_google_login_runtime\
                    ._forget_unresolved_activation_handoff(lease)
            self.assertIs(caught.exception, reset_injected)
        finally:
            sys.settrace(None)
        self.assertTrue(reset_fired)
        self.assertTrue(lease.terminal())

        replacement = PublishedResource()
        replacement.finish = True
        failing_registry = RepeatedFailureRegistry()
        with mock.patch.object(
            durable_google_login_runtime,
            "_UNRESOLVED_HANDOFFS",
            failing_registry,
        ):
            replacement_lease = (
                durable_google_login_runtime
                ._retain_unresolved_activation_handoff(replacement)
            )
        self.assertIs(replacement_lease, lease)
        self.assertTrue(replacement_lease.owns(replacement))
        self.assertTrue(replacement_lease.close())
        self.assertTrue(
            durable_google_login_runtime
            ._forget_unresolved_activation_handoff(
                replacement_lease
            )
        )
        self.assertFalse(replacement_lease.active())

        class RejectingRegistry(dict):
            def __init__(self):
                super().__init__()
                self.failures = 0

            def __setitem__(self, _key, _value):
                self.failures += 1
                raise RuntimeError(
                    "PRIVATE_FALLBACK_REGISTRY_FAILURE"
                )

        def emergency_pool(capacity):
            return tuple(
                durable_google_login_runtime
                ._ActivationHandoffCleanupLease(
                    None,
                    _capability=(
                        durable_google_login_runtime
                        ._HANDOFF_LEASE_POOL_CAPABILITY
                    ),
                )
                for _index in range(capacity)
            )

        constructor_calls = []

        def capacity_zero_worker(*_arguments):
            constructor_calls.append("constructed")

        with (
            mock.patch.object(
                durable_google_login_runtime,
                "_EMERGENCY_HANDOFF_LEASES",
                (),
            ),
            mock.patch.object(
                durable_google_login_runtime,
                "_UNRESOLVED_HANDOFFS",
                {},
            ),
            mock.patch.object(
                durable_google_login_runtime,
                "_run_configuration_worker",
                side_effect=capacity_zero_worker,
            ),
            self.assertRaises(
                DurableGoogleLoginConfigurationError
            ),
        ):
            durable_google_login_runtime\
                .build_durable_google_login_runtime("unused")
        self.assertEqual(constructor_calls, [])

        for capacity in (1, 64):
            with self.subTest(
                boundary="fallback_capacity_equality_and_plus_one",
                capacity=capacity,
            ):
                pool = emergency_pool(capacity)
                registry = RejectingRegistry()
                reservations = [
                    durable_google_login_runtime
                    ._new_activation_handoff_reservation()
                    for _index in range(capacity)
                ]
                rejected = (
                    durable_google_login_runtime
                    ._new_activation_handoff_reservation()
                )
                resources = []
                with (
                    mock.patch.object(
                        durable_google_login_runtime,
                        "_EMERGENCY_HANDOFF_LEASES",
                        pool,
                    ),
                    mock.patch.object(
                        durable_google_login_runtime,
                        "_UNRESOLVED_HANDOFFS",
                        registry,
                    ),
                ):
                    for reservation in reservations:
                        self.assertTrue(
                            durable_google_login_runtime
                            ._reserve_activation_handoff(
                                reservation
                            )
                        )
                    rejected_constructor_calls = []
                    with self.assertRaises(
                        DurableGoogleLoginConfigurationError
                    ):
                        durable_google_login_runtime\
                            ._reserve_activation_handoff(rejected)
                        rejected_constructor_calls.append(
                            "constructed"
                        )
                    self.assertEqual(
                        rejected_constructor_calls,
                        [],
                    )
                    resources = [
                        PublishedResource()
                        for _index in range(capacity)
                    ]
                    leases = [
                        durable_google_login_runtime
                        ._retain_unresolved_activation_handoff(
                            resource,
                            reservation,
                        )
                        for resource, reservation in zip(
                            resources,
                            reservations,
                            strict=True,
                        )
                    ]
                    self.assertEqual(
                        len({id(lease) for lease in leases}),
                        capacity,
                    )
                    self.assertEqual(
                        sum(lease.active() for lease in pool),
                        capacity,
                    )
                    self.assertTrue(
                        all(
                            lease.owns(resource)
                            for lease, resource in zip(
                                leases,
                                resources,
                                strict=True,
                            )
                        )
                    )
                    self.assertEqual(
                        registry.failures,
                        capacity * 4,
                    )
                    self.assertTrue(
                        all(
                            "PublishedResource" not in repr(lease)
                            for lease in leases
                        )
                    )
                    for resource in resources:
                        resource.finish = True
                    self.assertTrue(
                        durable_google_login_runtime
                        ._retry_unresolved_activation_handoffs()
                    )
                    self.assertEqual(
                        [resource.close_calls for resource in resources],
                        [1] * capacity,
                    )
                    self.assertFalse(
                        any(lease.active() for lease in pool)
                    )
                    for reservation in reservations:
                        self.assertTrue(
                            durable_google_login_runtime
                            ._release_activation_handoff_reservation(
                                reservation
                            )
                        )

        concurrent_capacity = 64
        concurrent_pool = emergency_pool(concurrent_capacity)
        concurrent_registry = RejectingRegistry()
        concurrent_barrier = threading.Barrier(
            concurrent_capacity + 2
        )
        concurrent_successes = []
        concurrent_rejections = []
        concurrent_errors = []

        def reserve_concurrently(index):
            reservation = (
                durable_google_login_runtime
                ._new_activation_handoff_reservation()
            )
            try:
                concurrent_barrier.wait(2)
                durable_google_login_runtime\
                    ._reserve_activation_handoff(reservation)
                resource = PublishedResource()
                lease = (
                    durable_google_login_runtime
                    ._retain_unresolved_activation_handoff(
                        resource,
                        reservation,
                    )
                )
                concurrent_successes.append(
                    (index, reservation, resource, lease)
                )
            except DurableGoogleLoginConfigurationError:
                concurrent_rejections.append(index)
            except (
                Exception,
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                concurrent_errors.append(exc)

        concurrent_threads = [
            threading.Thread(
                target=reserve_concurrently,
                args=(index,),
                daemon=False,
            )
            for index in range(concurrent_capacity + 1)
        ]
        with (
            mock.patch.object(
                durable_google_login_runtime,
                "_EMERGENCY_HANDOFF_LEASES",
                concurrent_pool,
            ),
            mock.patch.object(
                durable_google_login_runtime,
                "_UNRESOLVED_HANDOFFS",
                concurrent_registry,
            ),
        ):
            for thread in concurrent_threads:
                thread.start()
            concurrent_barrier.wait(2)
            for thread in concurrent_threads:
                thread.join(4)
            self.assertFalse(
                any(thread.is_alive() for thread in concurrent_threads)
            )
            self.assertEqual(concurrent_errors, [])
            self.assertEqual(
                len(concurrent_successes),
                concurrent_capacity,
            )
            self.assertEqual(len(concurrent_rejections), 1)
            self.assertEqual(
                len(
                    {
                        id(item[3])
                        for item in concurrent_successes
                    }
                ),
                concurrent_capacity,
            )
            self.assertEqual(
                sum(lease.active() for lease in concurrent_pool),
                concurrent_capacity,
            )
            for _, _, resource, _ in concurrent_successes:
                resource.finish = True
            self.assertTrue(
                durable_google_login_runtime
                ._retry_unresolved_activation_handoffs()
            )
            self.assertEqual(
                [
                    item[2].close_calls
                    for item in concurrent_successes
                ],
                [1] * concurrent_capacity,
            )
            self.assertFalse(
                any(lease.active() for lease in concurrent_pool)
            )
            for _, reservation, _, _ in concurrent_successes:
                self.assertTrue(
                    durable_google_login_runtime
                    ._release_activation_handoff_reservation(
                        reservation
                    )
                )

        drift_capacity = 64
        drift_pool = emergency_pool(drift_capacity)
        drift_registry = RejectingRegistry()
        drift_owner_reservation = (
            durable_google_login_runtime
            ._new_activation_handoff_reservation()
        )
        drift_resource = PublishedResource()
        drift_transient_reservations = []
        drift_owner_lease = None
        drift_probe_reservation = None
        with (
            mock.patch.object(
                durable_google_login_runtime,
                "_EMERGENCY_HANDOFF_LEASES",
                drift_pool,
            ),
            mock.patch.object(
                durable_google_login_runtime,
                "_UNRESOLVED_HANDOFFS",
                drift_registry,
            ),
        ):
            try:
                self.assertTrue(
                    durable_google_login_runtime
                    ._reserve_activation_handoff(
                        drift_owner_reservation
                    )
                )
                drift_owner_lease = (
                    durable_google_login_runtime
                    ._retain_unresolved_activation_handoff(
                        drift_resource,
                        drift_owner_reservation,
                    )
                )
                self.assertTrue(
                    drift_owner_lease.owns(drift_resource)
                )
                self.assertEqual(
                    sum(lease.active() for lease in drift_pool),
                    1,
                )
                for _iteration in range(drift_capacity * 2):
                    transient_reservation = (
                        durable_google_login_runtime
                        ._new_activation_handoff_reservation()
                    )
                    drift_transient_reservations.append(
                        transient_reservation
                    )
                    self.assertTrue(
                        durable_google_login_runtime
                        ._reserve_activation_handoff(
                            transient_reservation
                        )
                    )
                    retained = (
                        durable_google_login_runtime
                        ._retain_unresolved_activation_handoff(
                            drift_resource,
                            transient_reservation,
                        )
                    )
                    self.assertIs(retained, drift_owner_lease)
                    self.assertFalse(
                        durable_google_login_runtime
                        ._activation_handoff_reservation_is_reserved(
                            transient_reservation
                        )
                    )
                    self.assertIsNone(
                        transient_reservation._binding()
                    )
                    self.assertEqual(
                        sum(
                            lease.active()
                            for lease in drift_pool
                        ),
                        1,
                    )
                reservation_type = type(
                    drift_owner_reservation
                )
                clear_code = reservation_type._clear.__code__
                clear_assignment = next(
                    index
                    for index in range(
                        clear_code.co_firstlineno - 1,
                        clear_code.co_firstlineno + 40,
                    )
                    if (
                        "self.__binding = None"
                        in gate_source[index]
                    )
                )
                cleared_return_line = next(
                    index + 1
                    for index in range(
                        clear_assignment + 1,
                        clear_code.co_firstlineno + 40,
                    )
                    if gate_source[index].strip() == "return True"
                )
                for exception_type in exception_types:
                    with self.subTest(
                        boundary=(
                            "existing_owner_after_"
                            "transient_token_clear"
                        ),
                        exception_type=exception_type.__name__,
                    ):
                        transient_reservation = (
                            durable_google_login_runtime
                            ._new_activation_handoff_reservation()
                        )
                        drift_transient_reservations.append(
                            transient_reservation
                        )
                        self.assertTrue(
                            durable_google_login_runtime
                            ._reserve_activation_handoff(
                                transient_reservation
                            )
                        )
                        injected = exception_type(
                            "PRIVATE_TRANSIENT_TOKEN_CLEAR_CONTROL"
                        )
                        clear_fired = False

                        def clear_trace(frame, event, _arg):
                            nonlocal clear_fired
                            if (
                                not clear_fired
                                and event == "line"
                                and frame.f_code is clear_code
                                and frame.f_lineno
                                == cleared_return_line
                                and frame.f_locals.get("self")
                                is transient_reservation
                            ):
                                clear_fired = True
                                sys.settrace(None)
                                raise injected
                            return clear_trace

                        sys.settrace(clear_trace)
                        try:
                            retained = (
                                durable_google_login_runtime
                                ._retain_unresolved_activation_handoff(
                                    drift_resource,
                                    transient_reservation,
                                )
                            )
                        finally:
                            sys.settrace(None)
                        self.assertTrue(clear_fired)
                        self.assertIs(
                            retained,
                            drift_owner_lease,
                        )
                        self.assertIsNone(
                            transient_reservation._binding()
                        )
                        self.assertFalse(
                            durable_google_login_runtime
                            ._activation_handoff_reservation_is_reserved(
                                transient_reservation
                            )
                        )
                drift_probe_reservation = (
                    durable_google_login_runtime
                    ._new_activation_handoff_reservation()
                )
                self.assertTrue(
                    durable_google_login_runtime
                    ._reserve_activation_handoff(
                        drift_probe_reservation
                    )
                )
                self.assertTrue(
                    durable_google_login_runtime
                    ._release_activation_handoff_reservation(
                        drift_probe_reservation
                    )
                )
                self.assertFalse(
                    durable_google_login_runtime
                    ._activation_handoff_reservation_is_reserved(
                        drift_probe_reservation
                    )
                )
            finally:
                drift_resource.finish = True
                durable_google_login_runtime\
                    ._retry_unresolved_activation_handoffs()
                for reservation in drift_transient_reservations:
                    durable_google_login_runtime\
                        ._release_activation_handoff_reservation_preserving_primary(
                            reservation
                        )
                if drift_probe_reservation is not None:
                    durable_google_login_runtime\
                        ._release_activation_handoff_reservation_preserving_primary(
                            drift_probe_reservation
                        )
                durable_google_login_runtime\
                    ._release_activation_handoff_reservation_preserving_primary(
                        drift_owner_reservation
                    )
        self.assertEqual(drift_resource.close_calls, 1)
        self.assertFalse(
            any(lease.active() for lease in drift_pool)
        )
        self.assertTrue(
            all("<vacant>" in repr(lease) for lease in drift_pool)
        )

        stale_pool = emergency_pool(2)
        stale_registry = RejectingRegistry()
        stale_original = PublishedResource()
        stale_replacement = PublishedResource()
        stale_reservation = (
            durable_google_login_runtime
            ._new_activation_handoff_reservation()
        )
        stale_generation_checked = threading.Event()
        stale_generation_resume = threading.Event()
        stale_results = []
        stale_errors = []
        lease_type = (
            durable_google_login_runtime
            ._ActivationHandoffCleanupLease
        )
        original_owns_generation = lease_type._owns_generation

        def pause_after_generation_check(
            lease,
            resource,
            generation,
        ):
            owned = original_owns_generation(
                lease,
                resource,
                generation,
            )
            if (
                lease is stale_pool[0]
                and resource is stale_original
                and owned
            ):
                stale_generation_checked.set()
                if not stale_generation_resume.wait(2):
                    raise RuntimeError(
                        "stale_generation_resume_timeout"
                    )
            return owned

        def close_stale_generation():
            try:
                stale_results.append(
                    durable_google_login_runtime
                    ._close_activation_handoff_preserving_primary(
                        stale_original,
                        None,
                        stale_reservation,
                    )
                )
            except (
                Exception,
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ) as exc:
                stale_errors.append(exc)

        with (
            mock.patch.object(
                durable_google_login_runtime,
                "_EMERGENCY_HANDOFF_LEASES",
                stale_pool,
            ),
            mock.patch.object(
                durable_google_login_runtime,
                "_UNRESOLVED_HANDOFFS",
                stale_registry,
            ),
            mock.patch.object(
                lease_type,
                "_owns_generation",
                pause_after_generation_check,
            ),
        ):
            self.assertTrue(stale_pool[0].offer(stale_original))
            self.assertTrue(
                durable_google_login_runtime
                ._reserve_activation_handoff(stale_reservation)
            )
            stale_thread = threading.Thread(
                target=close_stale_generation,
                daemon=False,
            )
            stale_thread.start()
            self.assertTrue(stale_generation_checked.wait(2))
            stale_original.finish = True
            self.assertTrue(
                stale_pool[0].close(
                    _expected_resource=stale_original
                )
            )
            self.assertTrue(stale_pool[0].reset_terminal())
            self.assertTrue(stale_pool[0].offer(stale_replacement))
            stale_generation_resume.set()
            stale_thread.join(4)
            self.assertFalse(stale_thread.is_alive())
            self.assertEqual(stale_errors, [])
            self.assertEqual(stale_results, [False])
            self.assertEqual(stale_original.close_calls, 1)
            self.assertEqual(stale_replacement.close_calls, 0)
            self.assertTrue(
                stale_pool[0].owns(stale_replacement)
            )
            self.assertIsNone(stale_reservation._binding())
            stale_replacement.finish = True
            self.assertTrue(
                stale_pool[0].close(
                    _expected_resource=stale_replacement
                )
            )
            self.assertTrue(stale_pool[0].reset_terminal())
            self.assertFalse(
                any(lease.active() for lease in stale_pool)
            )

        lease_type = (
            durable_google_login_runtime
            ._ActivationHandoffCleanupLease
        )
        reserve_code = lease_type.reserve.__code__
        reserve_line = tuple(
            index + 1
            for index in range(
                reserve_code.co_firstlineno - 1,
                reserve_code.co_firstlineno + 80,
            )
            if 'self.__state = "reserved"' in gate_source[index]
        )[-1]
        cancel_code = lease_type.cancel_reserved.__code__
        cancel_line = next(
            index + 1
            for index in range(
                cancel_code.co_firstlineno - 1,
                cancel_code.co_firstlineno + 60,
            )
            if (
                "cleared = reservation._clear("
                in gate_source[index]
            )
        )
        offer_code = lease_type.offer_reserved.__code__
        offer_line = next(
            index + 1
            for index in range(
                offer_code.co_firstlineno - 1,
                offer_code.co_firstlineno + 70,
            )
            if (
                'self.__state = "cleanup_unresolved"'
                in gate_source[index]
                and index
                > next(
                    candidate
                    for candidate in range(
                        offer_code.co_firstlineno - 1,
                        offer_code.co_firstlineno + 70,
                    )
                    if "self.__resource = resource"
                    in gate_source[candidate]
                )
            )
        )
        for exception_type in exception_types:
            with self.subTest(
                boundary="reservation_publication_control",
                exception_type=exception_type.__name__,
            ):
                pool = emergency_pool(1)
                reservation = (
                    durable_google_login_runtime
                    ._new_activation_handoff_reservation()
                )
                injected = exception_type(
                    "PRIVATE_RESERVATION_PUBLICATION_CONTROL"
                )
                fired = False

                def reserve_trace(frame, event, _arg):
                    nonlocal fired
                    if (
                        not fired
                        and event == "line"
                        and frame.f_code is reserve_code
                        and frame.f_lineno == reserve_line
                    ):
                        fired = True
                        sys.settrace(None)
                        raise injected
                    return reserve_trace

                with mock.patch.object(
                    durable_google_login_runtime,
                    "_EMERGENCY_HANDOFF_LEASES",
                    pool,
                ):
                    sys.settrace(reserve_trace)
                    try:
                        with self.assertRaises(
                            exception_type
                        ) as caught:
                            durable_google_login_runtime\
                                ._reserve_activation_handoff(
                                    reservation
                                )
                        self.assertIs(caught.exception, injected)
                    finally:
                        sys.settrace(None)
                    self.assertTrue(fired)
                    self.assertTrue(
                        durable_google_login_runtime
                        ._reserve_activation_handoff(reservation)
                    )

                    cancel_injected = exception_type(
                        "PRIVATE_RESERVATION_RETIREMENT_CONTROL"
                    )
                    cancel_fired = False

                    def cancel_trace(frame, event, _arg):
                        nonlocal cancel_fired
                        if (
                            not cancel_fired
                            and event == "line"
                            and frame.f_code is cancel_code
                            and frame.f_lineno == cancel_line
                        ):
                            cancel_fired = True
                            sys.settrace(None)
                            raise cancel_injected
                        return cancel_trace

                    sys.settrace(cancel_trace)
                    try:
                        with self.assertRaises(
                            exception_type
                        ) as caught:
                            durable_google_login_runtime\
                                ._release_activation_handoff_reservation(
                                    reservation
                                )
                        self.assertIs(
                            caught.exception,
                            cancel_injected,
                        )
                    finally:
                        sys.settrace(None)
                    self.assertTrue(cancel_fired)
                    self.assertTrue(
                        durable_google_login_runtime
                        ._release_activation_handoff_reservation(
                            reservation
                        )
                    )
                    self.assertFalse(pool[0].active())

            with self.subTest(
                boundary="reserved_resource_offer_control",
                exception_type=exception_type.__name__,
            ):
                pool = emergency_pool(1)
                registry = RejectingRegistry()
                reservation = (
                    durable_google_login_runtime
                    ._new_activation_handoff_reservation()
                )
                resource = PublishedResource()
                injected = exception_type(
                    "PRIVATE_RESERVED_OFFER_CONTROL"
                )
                fired = False

                def offer_trace(frame, event, _arg):
                    nonlocal fired
                    if (
                        not fired
                        and event == "line"
                        and frame.f_code is offer_code
                        and frame.f_lineno == offer_line
                    ):
                        fired = True
                        sys.settrace(None)
                        raise injected
                    return offer_trace

                with (
                    mock.patch.object(
                        durable_google_login_runtime,
                        "_EMERGENCY_HANDOFF_LEASES",
                        pool,
                    ),
                    mock.patch.object(
                        durable_google_login_runtime,
                        "_UNRESOLVED_HANDOFFS",
                        registry,
                    ),
                ):
                    self.assertTrue(
                        durable_google_login_runtime
                        ._reserve_activation_handoff(reservation)
                    )
                    sys.settrace(offer_trace)
                    try:
                        lease = (
                            durable_google_login_runtime
                            ._retain_unresolved_activation_handoff(
                                resource,
                                reservation,
                            )
                        )
                    finally:
                        sys.settrace(None)
                    self.assertTrue(fired)
                    self.assertIs(lease, pool[0])
                    self.assertTrue(lease.owns(resource))
                    resource.finish = True
                    self.assertTrue(
                        durable_google_login_runtime
                        ._retry_unresolved_activation_handoffs()
                    )
                    self.assertEqual(resource.close_calls, 1)
                    self.assertTrue(
                        durable_google_login_runtime
                        ._release_activation_handoff_reservation(
                            reservation
                        )
                    )

        for exception_type in exception_types:
            with self.subTest(
                boundary="handoff_forget_preserves_primary",
                exception_type=exception_type.__name__,
            ):
                published = PublishedResource()
                published.finish = True
                injected = exception_type(
                    "PRIVATE_HANDOFF_FORGET_CONTROL"
                )
                with mock.patch.object(
                    durable_google_login_runtime,
                    "_forget_unresolved_activation_handoff",
                    side_effect=injected,
                ):
                    self.assertFalse(
                        durable_google_login_runtime
                        ._close_activation_handoff_preserving_primary(
                            published,
                            None,
                        )
                    )
                self.assertTrue(
                    durable_google_login_runtime
                    ._retry_unresolved_activation_handoffs()
                )

    def test_existing_emergency_owner_disposition_commits_every_post_clear_boundary_without_capacity_drift(
        self,
    ):
        from wahojobs import durable_google_login_runtime

        exception_types = (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        )
        capacity = 64
        repetitions = capacity + 2
        lease_type = (
            durable_google_login_runtime
            ._ActivationHandoffCleanupLease
        )
        reservation_type = (
            durable_google_login_runtime
            ._ActivationHandoffReservation
        )
        cancel_code = lease_type.cancel_reserved.__code__
        clear_code = reservation_type._clear.__code__
        source = Path(
            durable_google_login_runtime.__file__
        ).read_text(encoding="utf-8").splitlines()
        generation_lines = tuple(
            index + 1
            for index in range(
                cancel_code.co_firstlineno - 1,
                cancel_code.co_firstlineno + 100,
            )
            if (
                "self.__generation = generation + 1"
                in source[index]
            )
        )

        class PublishedResource:
            def __init__(self):
                self.close_calls = 0
                self.finish = False

            def close(self, *, _preserve_primary=False):
                self.close_calls += 1
                return self.finish

        class RejectingRegistry(dict):
            def __init__(self):
                super().__init__()
                self.failures = 0

            def __setitem__(self, _key, _value):
                self.failures += 1
                raise RuntimeError(
                    "PRIVATE_POST_CLEAR_REGISTRY_FAILURE"
                )

        def emergency_pool():
            return tuple(
                lease_type(
                    None,
                    _capability=(
                        durable_google_login_runtime
                        ._HANDOFF_LEASE_POOL_CAPABILITY
                    ),
                )
                for _index in range(capacity)
            )

        def create_existing_owner(pool, registry):
            owner_reservation = (
                durable_google_login_runtime
                ._new_activation_handoff_reservation()
            )
            resource = PublishedResource()
            self.assertTrue(
                durable_google_login_runtime
                ._reserve_activation_handoff(owner_reservation)
            )
            owner = (
                durable_google_login_runtime
                ._retain_unresolved_activation_handoff(
                    resource,
                    owner_reservation,
                )
            )
            self.assertIs(owner, pool[0])
            self.assertTrue(owner.owns(resource))
            self.assertEqual(registry.failures, 4)
            return owner_reservation, resource, owner

        def retire_existing_owner(
            pool,
            owner_reservation,
            resource,
        ):
            resource.finish = True
            self.assertTrue(
                durable_google_login_runtime
                ._retry_unresolved_activation_handoffs()
            )
            self.assertEqual(resource.close_calls, 1)
            self.assertTrue(
                durable_google_login_runtime
                ._release_activation_handoff_reservation(
                    owner_reservation
                )
            )
            self.assertIsNone(owner_reservation._binding())
            self.assertFalse(any(lease.active() for lease in pool))
            self.assertTrue(
                all("<vacant>" in repr(lease) for lease in pool)
            )

        calibration_pool = emergency_pool()
        calibration_registry = RejectingRegistry()
        post_clear_lines = []
        with (
            mock.patch.object(
                durable_google_login_runtime,
                "_EMERGENCY_HANDOFF_LEASES",
                calibration_pool,
            ),
            mock.patch.object(
                durable_google_login_runtime,
                "_UNRESOLVED_HANDOFFS",
                calibration_registry,
            ),
        ):
            (
                calibration_owner_reservation,
                calibration_resource,
                calibration_owner,
            ) = create_existing_owner(
                calibration_pool,
                calibration_registry,
            )
            calibration_reservation = (
                durable_google_login_runtime
                ._new_activation_handoff_reservation()
            )
            self.assertTrue(
                durable_google_login_runtime
                ._reserve_activation_handoff(
                    calibration_reservation
                )
            )
            clear_returned = False

            def record_post_clear(frame, event, _arg):
                nonlocal clear_returned
                if (
                    frame.f_code is clear_code
                    and frame.f_locals.get("self")
                    is calibration_reservation
                    and event == "return"
                ):
                    clear_returned = True
                elif (
                    clear_returned
                    and frame.f_code is cancel_code
                    and frame.f_locals.get("reservation")
                    is calibration_reservation
                    and event == "line"
                ):
                    post_clear_lines.append(frame.f_lineno)
                return record_post_clear

            previous_trace = sys.gettrace()
            sys.settrace(record_post_clear)
            try:
                retained = (
                    durable_google_login_runtime
                    ._retain_unresolved_activation_handoff(
                        calibration_resource,
                        calibration_reservation,
                    )
                )
            finally:
                sys.settrace(previous_trace)
            self.assertIs(retained, calibration_owner)
            self.assertTrue(post_clear_lines)
            self.assertIsNone(calibration_reservation._binding())
            retire_existing_owner(
                calibration_pool,
                calibration_owner_reservation,
                calibration_resource,
            )

        post_clear_counts = {}
        post_clear_boundaries = []
        for line in post_clear_lines:
            occurrence = post_clear_counts.get(line, 0) + 1
            post_clear_counts[line] = occurrence
            post_clear_boundaries.append((line, occurrence))
        boundaries = tuple(
            ("post_clear", line, occurrence)
            for line, occurrence in post_clear_boundaries
        ) + tuple(
            ("generation_fence", line, 1)
            for line in generation_lines[:1]
        )

        for boundary_kind, target_line, target_occurrence in boundaries:
            for exception_type in exception_types:
                with self.subTest(
                    boundary=boundary_kind,
                    line=target_line,
                    occurrence=target_occurrence,
                    exception_type=exception_type.__name__,
                ):
                    pool = emergency_pool()
                    registry = RejectingRegistry()
                    probes = []
                    with (
                        mock.patch.object(
                            durable_google_login_runtime,
                            "_EMERGENCY_HANDOFF_LEASES",
                            pool,
                        ),
                        mock.patch.object(
                            durable_google_login_runtime,
                            "_UNRESOLVED_HANDOFFS",
                            registry,
                        ),
                    ):
                        (
                            owner_reservation,
                            resource,
                            owner,
                        ) = create_existing_owner(pool, registry)
                        try:
                            for iteration in range(repetitions):
                                transient = (
                                    durable_google_login_runtime
                                    ._new_activation_handoff_reservation()
                                )
                                self.assertTrue(
                                    durable_google_login_runtime
                                    ._reserve_activation_handoff(
                                        transient
                                    )
                                )
                                transient_binding = (
                                    transient._binding()
                                )
                                self.assertIsNotNone(
                                    transient_binding
                                )
                                self.assertIsNot(
                                    transient_binding[0],
                                    owner,
                                )
                                injected = exception_type(
                                    "PRIVATE_POST_CLEAR_COMMIT_CONTROL"
                                )
                                fired = 0
                                clear_returned = False
                                line_occurrences = {}

                                def interrupt_boundary(
                                    frame,
                                    event,
                                    _arg,
                                ):
                                    nonlocal clear_returned, fired
                                    if (
                                        frame.f_code is clear_code
                                        and frame.f_locals.get(
                                            "self"
                                        )
                                        is transient
                                        and event == "return"
                                    ):
                                        clear_returned = True
                                    if (
                                        frame.f_code
                                        is not cancel_code
                                        or frame.f_locals.get(
                                            "reservation"
                                        )
                                        is not transient
                                        or event != "line"
                                    ):
                                        return interrupt_boundary
                                    line = frame.f_lineno
                                    line_occurrences[line] = (
                                        line_occurrences.get(
                                            line,
                                            0,
                                        )
                                        + 1
                                    )
                                    post_clear_target = (
                                        boundary_kind
                                        == "post_clear"
                                        and clear_returned
                                        and line == target_line
                                        and line_occurrences[line]
                                        == target_occurrence
                                    )
                                    generation_target = (
                                        boundary_kind
                                        == "generation_fence"
                                        and not clear_returned
                                        and line == target_line
                                        and line_occurrences[line]
                                        == target_occurrence
                                    )
                                    if (
                                        fired == 0
                                        and (
                                            post_clear_target
                                            or generation_target
                                        )
                                    ):
                                        fired = 1
                                        sys.settrace(None)
                                        raise injected
                                    return interrupt_boundary

                                previous_trace = sys.gettrace()
                                sys.settrace(interrupt_boundary)
                                try:
                                    retained = (
                                        durable_google_login_runtime
                                        ._retain_unresolved_activation_handoff(
                                            resource,
                                            transient,
                                        )
                                    )
                                finally:
                                    sys.settrace(previous_trace)
                                self.assertEqual(fired, 1)
                                self.assertIs(retained, owner)
                                self.assertTrue(owner.owns(resource))
                                self.assertEqual(
                                    resource.close_calls,
                                    0,
                                )
                                self.assertIsNone(
                                    transient._binding()
                                )
                                self.assertFalse(
                                    durable_google_login_runtime
                                    ._activation_handoff_reservation_is_reserved(
                                        transient
                                    )
                                )
                                self.assertEqual(
                                    registry.failures,
                                    4,
                                )
                                self.assertEqual(
                                    sum(
                                        lease.active()
                                        for lease in pool
                                    ),
                                    1,
                                )
                                self.assertTrue(
                                    all(
                                        "<vacant>"
                                        in repr(lease)
                                        for lease in pool
                                        if lease is not owner
                                    )
                                )
                                self.assertIsNone(
                                    injected.__traceback__
                                )
                                self.assertIsNone(
                                    injected.__context__
                                )
                                self.assertIsNone(
                                    injected.__cause__
                                )
                                probe = (
                                    durable_google_login_runtime
                                    ._new_activation_handoff_reservation()
                                )
                                self.assertTrue(
                                    durable_google_login_runtime
                                    ._reserve_activation_handoff(
                                        probe
                                    )
                                )
                                self.assertTrue(
                                    durable_google_login_runtime
                                    ._release_activation_handoff_reservation(
                                        probe
                                    )
                                )
                                self.assertIsNone(
                                    probe._binding()
                                )

                            for _index in range(capacity - 1):
                                probe = (
                                    durable_google_login_runtime
                                    ._new_activation_handoff_reservation()
                                )
                                self.assertTrue(
                                    durable_google_login_runtime
                                    ._reserve_activation_handoff(
                                        probe
                                    )
                                )
                                probes.append(probe)
                            self.assertEqual(
                                len(
                                    {
                                        id(probe._binding()[0])
                                        for probe in probes
                                    }
                                ),
                                capacity - 1,
                            )
                            overflow = (
                                durable_google_login_runtime
                                ._new_activation_handoff_reservation()
                            )
                            with self.assertRaises(
                                DurableGoogleLoginConfigurationError
                            ):
                                durable_google_login_runtime\
                                    ._reserve_activation_handoff(
                                        overflow
                                    )
                            self.assertIsNone(overflow._binding())
                            for probe in probes:
                                self.assertTrue(
                                    durable_google_login_runtime
                                    ._release_activation_handoff_reservation(
                                        probe
                                    )
                                )
                                self.assertIsNone(
                                    probe._binding()
                                )
                            probes.clear()
                            self.assertTrue(owner.owns(resource))
                            self.assertEqual(
                                sum(
                                    lease.active()
                                    for lease in pool
                                ),
                                1,
                            )
                            self.assertTrue(
                                all(
                                    "<vacant>" in repr(lease)
                                    for lease in pool
                                    if lease is not owner
                                )
                            )
                        finally:
                            sys.settrace(previous_trace)
                            for probe in probes:
                                durable_google_login_runtime\
                                    ._release_activation_handoff_reservation_preserving_primary(
                                        probe
                                    )
                            retire_existing_owner(
                                pool,
                                owner_reservation,
                                resource,
                            )
        self.assertTrue(generation_lines)

    def test_reservation_binding_clear_serializes_same_object_rebind_without_capacity_drift(
        self,
    ):
        from wahojobs import durable_google_login_runtime

        capacity = 64
        repetitions = capacity + 2
        exception_types = (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        )
        lease_type = (
            durable_google_login_runtime
            ._ActivationHandoffCleanupLease
        )
        reservation_type = (
            durable_google_login_runtime
            ._ActivationHandoffReservation
        )
        bind_code = reservation_type._bind.__code__
        binding_code = reservation_type._binding.__code__
        clear_code = reservation_type._clear.__code__
        repr_code = reservation_type.__repr__.__code__
        source = Path(
            durable_google_login_runtime.__file__
        ).read_text(encoding="utf-8").splitlines()

        def source_line(code, text, *, after=None):
            start = code.co_firstlineno - 1
            stop = min(len(source), start + 50)
            return next(
                index + 1
                for index in range(start, stop)
                if text in source[index]
                and (after is None or index + 1 > after)
            )

        bind_with_line = source_line(
            bind_code,
            "with self.__binding_lock:",
        )
        bind_read_line = source_line(
            bind_code,
            "binding = self.__binding",
        )
        bind_assignment_line = source_line(
            bind_code,
            "self.__binding = (lease, generation)",
        )
        bind_return_line = source_line(
            bind_code,
            "return True",
            after=bind_assignment_line,
        )
        binding_with_line = source_line(
            binding_code,
            "with self.__binding_lock:",
        )
        binding_return_line = source_line(
            binding_code,
            "return self.__binding",
        )
        clear_with_line = source_line(
            clear_code,
            "with self.__binding_lock:",
        )
        clear_read_line = source_line(
            clear_code,
            "binding = self.__binding",
        )
        clear_assignment_line = source_line(
            clear_code,
            "self.__binding = None",
        )
        clear_return_line = source_line(
            clear_code,
            "return True",
            after=clear_assignment_line,
        )
        repr_with_line = source_line(
            repr_code,
            "with self.__binding_lock:",
        )
        repr_read_line = source_line(
            repr_code,
            'state = "vacant"',
        )

        class PublishedResource:
            def __init__(self):
                self.close_calls = 0
                self.finish = False

            def close(self, *, _preserve_primary=False):
                self.close_calls += 1
                return self.finish

        class RejectingRegistry(dict):
            def __init__(self):
                super().__init__()
                self.failures = 0

            def __setitem__(self, _key, _value):
                self.failures += 1
                raise RuntimeError(
                    "PRIVATE_BINDING_RACE_REGISTRY_FAILURE"
                )

        pool = tuple(
            lease_type(
                None,
                _capability=(
                    durable_google_login_runtime
                    ._HANDOFF_LEASE_POOL_CAPABILITY
                ),
            )
            for _index in range(capacity)
        )
        registry = RejectingRegistry()
        owner_reservation = (
            durable_google_login_runtime
            ._new_activation_handoff_reservation()
        )
        race_reservation = (
            durable_google_login_runtime
            ._new_activation_handoff_reservation()
        )
        protected_resource = PublishedResource()
        unrelated_resource = PublishedResource()
        race_threads = []
        observed_reservations = [
            owner_reservation,
            race_reservation,
        ]

        def binding_lock(reservation):
            return getattr(
                reservation,
                "_ActivationHandoffReservation__binding_lock",
                None,
            )

        def assert_no_lease_waiters():
            for lease in pool:
                condition = getattr(
                    lease,
                    "_ActivationHandoffCleanupLease__condition",
                )
                with condition:
                    self.assertEqual(len(condition._waiters), 0)

        def run_serialized_race(iteration):
            self.assertIsNone(race_reservation._binding())
            self.assertTrue(
                durable_google_login_runtime
                ._reserve_activation_handoff(race_reservation)
            )
            original_binding = race_reservation._binding()
            self.assertIs(original_binding[0], pool[1])
            self.assertEqual(
                original_binding[1],
                (iteration * 4) + 1,
            )
            paused = threading.Event()
            resume = threading.Event()
            cancellation_b_attempted = threading.Event()
            cancellation_b_done = threading.Event()
            rebind_started = threading.Event()
            rebind_done = threading.Event()
            results = {}
            errors = {}

            def cancel_a():
                def pause_before_atomic_clear(frame, event, _arg):
                    if (
                        event == "line"
                        and frame.f_code is clear_code
                        and frame.f_lineno == clear_assignment_line
                        and frame.f_locals.get("self")
                        is race_reservation
                        and frame.f_locals.get("binding")
                        is original_binding
                    ):
                        sys.settrace(None)
                        paused.set()
                        if not resume.wait(4):
                            raise RuntimeError(
                                "binding_clear_resume_timeout"
                            )
                    return pause_before_atomic_clear

                sys.settrace(pause_before_atomic_clear)
                try:
                    results["a"] = (
                        durable_google_login_runtime
                        ._release_activation_handoff_reservation(
                            race_reservation
                        )
                    )
                except (
                    Exception,
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    errors["a"] = exc
                finally:
                    sys.settrace(None)

            def cancel_b():
                def record_binding_lock_attempt(frame, event, _arg):
                    if (
                        event == "line"
                        and frame.f_code is binding_code
                        and frame.f_lineno == binding_with_line
                        and frame.f_locals.get("self")
                        is race_reservation
                    ):
                        sys.settrace(None)
                        cancellation_b_attempted.set()
                    return record_binding_lock_attempt

                sys.settrace(record_binding_lock_attempt)
                try:
                    results["b"] = (
                        durable_google_login_runtime
                        ._release_activation_handoff_reservation(
                            race_reservation
                        )
                    )
                except (
                    Exception,
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    errors["b"] = exc
                finally:
                    sys.settrace(None)
                    cancellation_b_done.set()

            def rebind():
                rebind_started.set()
                try:
                    if not cancellation_b_done.wait(4):
                        raise RuntimeError(
                            "binding_cancel_b_timeout"
                        )
                    results["rebound"] = (
                        durable_google_login_runtime
                        ._reserve_activation_handoff(
                            race_reservation
                        )
                    )
                    results["replacement_binding"] = (
                        race_reservation._binding()
                    )
                except (
                    Exception,
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                ) as exc:
                    errors["rebind"] = exc
                finally:
                    rebind_done.set()

            threads = [
                threading.Thread(
                    target=cancel_a,
                    name=f"binding-clear-a-{iteration}",
                    daemon=False,
                ),
                threading.Thread(
                    target=cancel_b,
                    name=f"binding-clear-b-{iteration}",
                    daemon=False,
                ),
                threading.Thread(
                    target=rebind,
                    name=f"binding-rebind-{iteration}",
                    daemon=False,
                ),
            ]
            race_threads.extend(threads)
            try:
                threads[0].start()
                self.assertTrue(paused.wait(4))
                lock = binding_lock(race_reservation)
                if lock is not None:
                    self.assertTrue(lock.locked())
                self.assertEqual(
                    getattr(
                        original_binding[0],
                        (
                            "_ActivationHandoffCleanupLease"
                            "__generation"
                        ),
                    ),
                    (iteration * 4) + 2,
                )
                self.assertEqual(
                    getattr(
                        original_binding[0],
                        "_ActivationHandoffCleanupLease__state",
                    ),
                    "vacant",
                )
                threads[1].start()
                self.assertTrue(cancellation_b_attempted.wait(4))
                threads[2].start()
                self.assertTrue(rebind_started.wait(4))
                if lock is None:
                    self.assertTrue(rebind_done.wait(4))
                else:
                    self.assertTrue(lock.locked())
                    self.assertFalse(cancellation_b_done.is_set())
                    self.assertFalse(rebind_done.is_set())
            finally:
                resume.set()
                for thread in threads:
                    if thread.ident is not None:
                        thread.join(4)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, {})
            self.assertEqual(results.get("a"), True)
            self.assertEqual(results.get("b"), True)
            self.assertEqual(results.get("rebound"), True)
            replacement_binding = results["replacement_binding"]
            self.assertIsNot(replacement_binding, original_binding)
            self.assertIs(
                race_reservation._binding(),
                replacement_binding,
            )
            self.assertIs(replacement_binding[0], pool[1])
            self.assertEqual(
                replacement_binding[1],
                (iteration * 4) + 3,
            )
            self.assertTrue(
                durable_google_login_runtime
                ._activation_handoff_reservation_is_reserved(
                    race_reservation
                )
            )
            self.assertEqual(protected_resource.close_calls, 0)
            self.assertEqual(unrelated_resource.close_calls, 0)
            self.assertTrue(
                durable_google_login_runtime
                ._release_activation_handoff_reservation(
                    race_reservation
                )
            )
            self.assertIsNone(race_reservation._binding())
            lock = binding_lock(race_reservation)
            if lock is not None:
                self.assertFalse(lock.locked())
            self.assertEqual(
                sum(lease.active() for lease in pool),
                1,
            )

        def run_control_boundary(
            boundary,
            code,
            target_line,
            operation,
            exception_type,
        ):
            reservation = (
                durable_google_login_runtime
                ._new_activation_handoff_reservation()
            )
            observed_reservations.append(reservation)
            if operation in {"binding", "clear", "repr"}:
                self.assertTrue(
                    durable_google_login_runtime
                    ._reserve_activation_handoff(reservation)
                )
            expected_binding = reservation._binding()
            injected = exception_type(
                "PRIVATE_BINDING_SYNCHRONIZATION_CONTROL"
            )
            fired = False
            caught = None

            def interrupt_boundary(frame, event, _arg):
                nonlocal fired
                if (
                    not fired
                    and event == "line"
                    and frame.f_code is code
                    and frame.f_lineno == target_line
                    and frame.f_locals.get("self") is reservation
                ):
                    fired = True
                    sys.settrace(None)
                    raise injected
                return interrupt_boundary

            previous_trace = sys.gettrace()
            sys.settrace(interrupt_boundary)
            try:
                try:
                    if operation == "bind":
                        durable_google_login_runtime\
                            ._reserve_activation_handoff(
                                reservation
                            )
                    elif operation == "binding":
                        reservation._binding()
                    elif operation == "clear":
                        durable_google_login_runtime\
                            ._release_activation_handoff_reservation(
                                reservation
                            )
                    else:
                        repr(reservation)
                except exception_type as exc:
                    caught = exc
            finally:
                sys.settrace(previous_trace)
            self.assertTrue(
                fired,
                f"{boundary}:{exception_type.__name__}",
            )
            self.assertIs(caught, injected)
            self.assertIsNone(caught.__cause__)
            self.assertIsNone(caught.__context__)
            lock = binding_lock(reservation)
            self.assertIsNotNone(lock)
            self.assertFalse(lock.locked())
            current = reservation._binding()
            if operation == "clear" and current is not None:
                self.assertIs(current, expected_binding)
            self.assertTrue(
                durable_google_login_runtime
                ._release_activation_handoff_reservation(
                    reservation
                )
            )
            self.assertIsNone(reservation._binding())
            self.assertTrue(
                durable_google_login_runtime
                ._reserve_activation_handoff(reservation)
            )
            self.assertTrue(
                durable_google_login_runtime
                ._activation_handoff_reservation_is_reserved(
                    reservation
                )
            )
            self.assertTrue(
                durable_google_login_runtime
                ._release_activation_handoff_reservation(
                    reservation
                )
            )
            self.assertIsNone(reservation._binding())
            self.assertFalse(lock.locked())
            self.assertEqual(
                sum(lease.active() for lease in pool),
                1,
            )

        boundaries = (
            ("bind_before_lock", bind_code, bind_with_line, "bind"),
            ("bind_after_lock", bind_code, bind_read_line, "bind"),
            (
                "bind_before_publication",
                bind_code,
                bind_assignment_line,
                "bind",
            ),
            (
                "bind_after_publication",
                bind_code,
                bind_return_line,
                "bind",
            ),
            (
                "binding_before_lock",
                binding_code,
                binding_with_line,
                "binding",
            ),
            (
                "binding_after_lock",
                binding_code,
                binding_return_line,
                "binding",
            ),
            (
                "clear_before_lock",
                clear_code,
                clear_with_line,
                "clear",
            ),
            (
                "clear_after_lock",
                clear_code,
                clear_read_line,
                "clear",
            ),
            (
                "clear_before_mutation",
                clear_code,
                clear_assignment_line,
                "clear",
            ),
            (
                "clear_after_mutation",
                clear_code,
                clear_return_line,
                "clear",
            ),
            (
                "repr_before_lock",
                repr_code,
                repr_with_line,
                "repr",
            ),
            (
                "repr_after_lock",
                repr_code,
                repr_read_line,
                "repr",
            ),
        )

        probes = []
        overflow = None
        fresh = None
        with (
            mock.patch.object(
                durable_google_login_runtime,
                "_EMERGENCY_HANDOFF_LEASES",
                pool,
            ),
            mock.patch.object(
                durable_google_login_runtime,
                "_UNRESOLVED_HANDOFFS",
                registry,
            ),
        ):
            try:
                self.assertTrue(
                    durable_google_login_runtime
                    ._reserve_activation_handoff(owner_reservation)
                )
                owner = (
                    durable_google_login_runtime
                    ._retain_unresolved_activation_handoff(
                        protected_resource,
                        owner_reservation,
                    )
                )
                self.assertIs(owner, pool[0])
                self.assertTrue(owner.owns(protected_resource))
                self.assertEqual(registry.failures, 4)

                for iteration in range(repetitions):
                    run_serialized_race(iteration)

                for (
                    boundary,
                    code,
                    target_line,
                    operation,
                ) in boundaries:
                    for exception_type in exception_types:
                        with self.subTest(
                            boundary=boundary,
                            exception_type=exception_type.__name__,
                        ):
                            run_control_boundary(
                                boundary,
                                code,
                                target_line,
                                operation,
                                exception_type,
                            )

                for _index in range(capacity - 1):
                    probe = (
                        durable_google_login_runtime
                        ._new_activation_handoff_reservation()
                    )
                    probes.append(probe)
                    observed_reservations.append(probe)
                    self.assertTrue(
                        durable_google_login_runtime
                        ._reserve_activation_handoff(probe)
                    )
                owner_binding = owner_reservation._binding()
                self.assertIsNotNone(owner_binding)
                self.assertEqual(
                    len(
                        {
                            id(owner_binding[0]),
                            *(
                                id(probe._binding()[0])
                                for probe in probes
                            ),
                        }
                    ),
                    capacity,
                )
                overflow = (
                    durable_google_login_runtime
                    ._new_activation_handoff_reservation()
                )
                observed_reservations.append(overflow)
                with self.assertRaises(
                    DurableGoogleLoginConfigurationError
                ):
                    durable_google_login_runtime\
                        ._reserve_activation_handoff(overflow)
                self.assertIsNone(overflow._binding())
                for probe in probes:
                    self.assertTrue(
                        durable_google_login_runtime
                        ._release_activation_handoff_reservation(
                            probe
                        )
                    )
                    self.assertIsNone(probe._binding())
                probes.clear()
                fresh = (
                    durable_google_login_runtime
                    ._new_activation_handoff_reservation()
                )
                observed_reservations.append(fresh)
                self.assertTrue(
                    durable_google_login_runtime
                    ._reserve_activation_handoff(fresh)
                )
                self.assertTrue(
                    durable_google_login_runtime
                    ._release_activation_handoff_reservation(
                        fresh
                    )
                )
                self.assertIsNone(fresh._binding())
            finally:
                for thread in race_threads:
                    if thread.ident is not None:
                        thread.join(4)
                for reservation in (
                    race_reservation,
                    *probes,
                ):
                    durable_google_login_runtime\
                        ._release_activation_handoff_reservation_preserving_primary(
                            reservation
                        )
                protected_resource.finish = True
                durable_google_login_runtime\
                    ._retry_unresolved_activation_handoffs()
                durable_google_login_runtime\
                    ._release_activation_handoff_reservation_preserving_primary(
                        owner_reservation
                    )

        self.assertFalse(any(thread.is_alive() for thread in race_threads))
        self.assertEqual(protected_resource.close_calls, 1)
        self.assertEqual(unrelated_resource.close_calls, 0)
        self.assertFalse(any(lease.active() for lease in pool))
        self.assertTrue(
            all("<vacant>" in repr(lease) for lease in pool)
        )
        self.assertTrue(
            all(
                reservation._binding() is None
                for reservation in observed_reservations
            )
        )
        self.assertTrue(
            all(
                binding_lock(reservation) is not None
                and not binding_lock(reservation).locked()
                for reservation in observed_reservations
            )
        )
        assert_no_lease_waiters()

    def _assert_dispatcher_exact_token_survives_same_reservation_reuse(
        self,
        dispatcher_name,
    ):
        from wahojobs import durable_google_login_runtime

        capacity = 64
        repetitions = capacity + 2
        lease_type = (
            durable_google_login_runtime
            ._ActivationHandoffCleanupLease
        )

        class PublishedResource:
            def __init__(self):
                self.close_calls = 0
                self.finish = False

            def close(self, *, _preserve_primary=False):
                self.close_calls += 1
                return self.finish

        class RejectingRegistry(dict):
            def __init__(self):
                super().__init__()
                self.failures = 0

            def __setitem__(self, _key, _value):
                self.failures += 1
                raise RuntimeError(
                    "PRIVATE_EXACT_TOKEN_REGISTRY_FAILURE"
                )

        pool = tuple(
            lease_type(
                None,
                _capability=(
                    durable_google_login_runtime
                    ._HANDOFF_LEASE_POOL_CAPABILITY
                ),
            )
            for _index in range(capacity)
        )
        registry = RejectingRegistry()
        protected_resource = PublishedResource()
        unrelated_resource = PublishedResource()
        owner_reservation = (
            durable_google_login_runtime
            ._new_activation_handoff_reservation()
        )
        race_reservation = (
            durable_google_login_runtime
            ._new_activation_handoff_reservation()
        )
        if dispatcher_name == "release":
            dispatcher = (
                durable_google_login_runtime
                ._release_activation_handoff_reservation
            )
            target_needles = (
                "return binding[0].cancel_reserved",
            )
        else:
            dispatcher = (
                durable_google_login_runtime
                ._dispose_unused_activation_handoff_reservation_preserving_primary
            )
            target_needles = (
                (
                    "_dispose_unused_activation_handoff_"
                    "reservation_exact_preserving_primary("
                ),
                "disposition = lease._dispose_reserved(",
            )
        dispatcher_code = dispatcher.__code__
        source = Path(
            durable_google_login_runtime.__file__
        ).read_text(encoding="utf-8").splitlines()
        start = dispatcher_code.co_firstlineno - 1
        stop = min(len(source), start + 100)
        dispatch_line = next(
            index + 1
            for index in range(start, stop)
            if any(
                needle in source[index]
                for needle in target_needles
            )
        )
        race_threads = []
        resume_events = []
        observed_reservations = [
            owner_reservation,
            race_reservation,
        ]
        probes = []
        overflow_before = None
        overflow_after = None
        overflow_refilled = None
        fresh = None

        def reservation_lock(reservation):
            return getattr(
                reservation,
                "_ActivationHandoffReservation__binding_lock",
            )

        def assert_waiters_empty():
            for lease in pool:
                condition = getattr(
                    lease,
                    "_ActivationHandoffCleanupLease__condition",
                )
                with condition:
                    self.assertEqual(len(condition._waiters), 0)

        def dispatch(reservation):
            if dispatcher_name == "release":
                return dispatcher(reservation)
            return dispatcher(
                reservation,
                protected_resource,
            )

        def legitimately_dispose_current():
            if dispatcher_name == "release":
                return (
                    durable_google_login_runtime
                    ._release_activation_handoff_reservation(
                        race_reservation
                    )
                )
            return (
                durable_google_login_runtime
                ._dispose_unused_activation_handoff_reservation_preserving_primary(
                    race_reservation,
                    protected_resource,
                )
                is durable_google_login_runtime
                ._HANDOFF_RESERVATION_RELEASED
            )

        with (
            mock.patch.object(
                durable_google_login_runtime,
                "_EMERGENCY_HANDOFF_LEASES",
                pool,
            ),
            mock.patch.object(
                durable_google_login_runtime,
                "_UNRESOLVED_HANDOFFS",
                registry,
            ),
        ):
            try:
                self.assertTrue(
                    durable_google_login_runtime
                    ._reserve_activation_handoff(owner_reservation)
                )
                owner = (
                    durable_google_login_runtime
                    ._retain_unresolved_activation_handoff(
                        protected_resource,
                        owner_reservation,
                    )
                )
                self.assertIs(owner, pool[0])
                self.assertTrue(owner.owns(protected_resource))
                self.assertEqual(registry.failures, 4)

                for iteration in range(repetitions):
                    self.assertIsNone(race_reservation._binding())
                    self.assertTrue(
                        durable_google_login_runtime
                        ._reserve_activation_handoff(
                            race_reservation
                        )
                    )
                    token_one = race_reservation._binding()
                    self.assertIs(token_one[0], pool[1])
                    self.assertEqual(
                        token_one[1],
                        (iteration * 4) + 1,
                    )
                    captured = threading.Event()
                    resume = threading.Event()
                    resume_events.append(resume)
                    result = {}
                    errors = []

                    def stale_dispatch(
                        expected_token=token_one,
                        captured_event=captured,
                        resume_event=resume,
                        result_target=result,
                        error_target=errors,
                    ):
                        def pause_after_dispatch_snapshot(
                            frame,
                            event,
                            _arg,
                        ):
                            if (
                                event == "line"
                                and frame.f_code is dispatcher_code
                                and frame.f_lineno == dispatch_line
                                and frame.f_locals.get("binding")
                                is expected_token
                            ):
                                sys.settrace(None)
                                captured_event.set()
                                if not resume_event.wait(4):
                                    raise RuntimeError(
                                        "exact_token_resume_timeout"
                                    )
                            return pause_after_dispatch_snapshot

                        sys.settrace(
                            pause_after_dispatch_snapshot
                        )
                        try:
                            result_target["value"] = dispatch(
                                race_reservation
                            )
                        except (
                            Exception,
                            KeyboardInterrupt,
                            SystemExit,
                            GeneratorExit,
                        ) as exc:
                            error_target.append(exc)
                        finally:
                            sys.settrace(None)

                    thread = threading.Thread(
                        target=stale_dispatch,
                        name=(
                            f"exact-token-{dispatcher_name}-"
                            f"{iteration}"
                        ),
                        daemon=False,
                    )
                    race_threads.append(thread)
                    try:
                        thread.start()
                        self.assertTrue(captured.wait(4))
                        self.assertIs(
                            race_reservation._binding(),
                            token_one,
                        )
                        self.assertTrue(
                            durable_google_login_runtime
                            ._release_activation_handoff_reservation(
                                race_reservation
                            )
                        )
                        self.assertIsNone(
                            race_reservation._binding()
                        )
                        self.assertTrue(
                            durable_google_login_runtime
                            ._reserve_activation_handoff(
                                race_reservation
                            )
                        )
                        token_three = (
                            race_reservation._binding()
                        )
                        self.assertIsNot(token_three, token_one)
                        self.assertIs(token_three[0], token_one[0])
                        self.assertEqual(
                            token_three[1],
                            (iteration * 4) + 3,
                        )

                        if iteration == repetitions - 1:
                            for _index in range(capacity - 2):
                                probe = (
                                    durable_google_login_runtime
                                    ._new_activation_handoff_reservation()
                                )
                                probes.append(probe)
                                observed_reservations.append(probe)
                                self.assertTrue(
                                    durable_google_login_runtime
                                    ._reserve_activation_handoff(
                                        probe
                                    )
                                )
                            occupied = {
                                id(
                                    owner_reservation
                                    ._binding()[0]
                                ),
                                id(token_three[0]),
                                *(
                                    id(probe._binding()[0])
                                    for probe in probes
                                ),
                            }
                            self.assertEqual(
                                len(occupied),
                                capacity,
                            )
                            overflow_before = (
                                durable_google_login_runtime
                                ._new_activation_handoff_reservation()
                            )
                            observed_reservations.append(
                                overflow_before
                            )
                            with self.assertRaises(
                                DurableGoogleLoginConfigurationError
                            ):
                                durable_google_login_runtime\
                                    ._reserve_activation_handoff(
                                        overflow_before
                                    )
                            self.assertIsNone(
                                overflow_before._binding()
                            )
                    finally:
                        resume.set()
                        if thread.ident is not None:
                            thread.join(4)

                    self.assertFalse(thread.is_alive())
                    self.assertEqual(errors, [])
                    if dispatcher_name == "release":
                        self.assertIs(result.get("value"), False)
                    else:
                        self.assertIs(
                            result.get("value"),
                            durable_google_login_runtime
                            ._HANDOFF_RESERVATION_CONFLICT,
                        )
                    self.assertIs(
                        race_reservation._binding(),
                        token_three,
                    )
                    self.assertTrue(
                        token_three[0].reserved_by(
                            race_reservation,
                            token_three,
                        )
                    )
                    self.assertTrue(
                        owner.owns(protected_resource)
                    )
                    self.assertEqual(
                        protected_resource.close_calls,
                        0,
                    )
                    self.assertEqual(
                        unrelated_resource.close_calls,
                        0,
                    )

                    if iteration == repetitions - 1:
                        overflow_after = (
                            durable_google_login_runtime
                            ._new_activation_handoff_reservation()
                        )
                        observed_reservations.append(
                            overflow_after
                        )
                        with self.assertRaises(
                            DurableGoogleLoginConfigurationError
                        ):
                            durable_google_login_runtime\
                                ._reserve_activation_handoff(
                                    overflow_after
                                )
                        self.assertIsNone(
                            overflow_after._binding()
                        )

                    self.assertTrue(
                        legitimately_dispose_current()
                    )
                    self.assertIsNone(
                        race_reservation._binding()
                    )

                    if iteration == repetitions - 1:
                        fresh = (
                            durable_google_login_runtime
                            ._new_activation_handoff_reservation()
                        )
                        observed_reservations.append(fresh)
                        self.assertTrue(
                            durable_google_login_runtime
                            ._reserve_activation_handoff(fresh)
                        )
                        self.assertIs(
                            fresh._binding()[0],
                            pool[1],
                        )
                        overflow_refilled = (
                            durable_google_login_runtime
                            ._new_activation_handoff_reservation()
                        )
                        observed_reservations.append(
                            overflow_refilled
                        )
                        with self.assertRaises(
                            DurableGoogleLoginConfigurationError
                        ):
                            durable_google_login_runtime\
                                ._reserve_activation_handoff(
                                    overflow_refilled
                                )
                        self.assertIsNone(
                            overflow_refilled._binding()
                        )
                        self.assertTrue(
                            durable_google_login_runtime
                            ._release_activation_handoff_reservation(
                                fresh
                            )
                        )
                        self.assertIsNone(fresh._binding())
                        for probe in probes:
                            self.assertTrue(
                                durable_google_login_runtime
                                ._release_activation_handoff_reservation(
                                    probe
                                )
                            )
                            self.assertIsNone(probe._binding())
                        probes.clear()
            finally:
                for resume in resume_events:
                    resume.set()
                for thread in race_threads:
                    if thread.ident is not None:
                        thread.join(4)
                for reservation in (
                    race_reservation,
                    *probes,
                ):
                    durable_google_login_runtime\
                        ._release_activation_handoff_reservation_preserving_primary(
                            reservation
                        )
                protected_resource.finish = True
                durable_google_login_runtime\
                    ._retry_unresolved_activation_handoffs()
                durable_google_login_runtime\
                    ._release_activation_handoff_reservation_preserving_primary(
                        owner_reservation
                    )

        self.assertFalse(
            any(thread.is_alive() for thread in race_threads)
        )
        self.assertEqual(protected_resource.close_calls, 1)
        self.assertEqual(unrelated_resource.close_calls, 0)
        self.assertEqual(registry, {})
        self.assertFalse(any(lease.active() for lease in pool))
        self.assertTrue(
            all("<vacant>" in repr(lease) for lease in pool)
        )
        self.assertTrue(
            all(
                reservation._binding() is None
                for reservation in observed_reservations
            )
        )
        self.assertTrue(
            all(
                not reservation_lock(reservation).locked()
                for reservation in observed_reservations
            )
        )
        assert_waiters_empty()

    def test_release_dispatcher_preserves_exact_token_across_same_reservation_reuse(
        self,
    ):
        self._assert_dispatcher_exact_token_survives_same_reservation_reuse(
            "release"
        )

    def test_dispose_dispatcher_preserves_exact_token_across_same_reservation_reuse(
        self,
    ):
        self._assert_dispatcher_exact_token_survives_same_reservation_reuse(
            "dispose"
        )

    def test_incomplete_launcher_cleanup_retains_retry_authority(self):
        from scripts import durable_google_login_app
        from wahojobs import durable_google_login_runtime

        coordinator_type = (
            durable_google_login_runtime._CleanupCoordinator
        )
        created = []
        real_constructor = coordinator_type

        def construct():
            coordinator = real_constructor()
            created.append(coordinator)
            return coordinator

        incomplete = durable_google_login_runtime._CleanupReport(
            (),
            ("signal_handlers",),
            False,
            ("signal_handlers",),
        )
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            with (
                mock.patch.object(
                    durable_google_login_runtime,
                    "_CleanupCoordinator",
                    side_effect=construct,
                ),
                mock.patch.object(
                    coordinator_type,
                    "cleanup",
                    return_value=incomplete,
                ),
                mock.patch("builtins.print"),
            ):
                status = durable_google_login_app.main(
                    ["--config", str(missing)]
                )
        self.assertEqual(status, 3)
        self.assertEqual(len(created), 1)
        coordinator = created[0]
        with durable_google_login_runtime._UNRESOLVED_HANDOFF_LOCK:
            leases = tuple(
                durable_google_login_runtime
                ._UNRESOLVED_HANDOFFS.values()
            )
        self.assertEqual(len(leases), 1)
        self.assertTrue(leases[0].owns(coordinator))
        self.assertTrue(
            durable_google_login_runtime
            ._retry_unresolved_activation_handoffs()
        )
        with durable_google_login_runtime._UNRESOLVED_HANDOFF_LOCK:
            self.assertFalse(
                durable_google_login_runtime
                ._UNRESOLVED_HANDOFFS
            )

    def test_ordinary_launcher_import_remains_authentication_dormant(self):
        source = r"""
import sys
import scripts.local_product_app
print(
    "wahojobs.durable_google_login_runtime" in sys.modules,
    "wahojobs.durable_google_login_browser" in sys.modules,
    "wahojobs.google_oidc_gateway" in sys.modules,
)
"""
        result = self.run_python(source)
        self.assertEqual(result.stdout.strip(), "False False False")

    def test_migration_inventory_still_ends_at_m008(self):
        migrations = sorted(
            (ROOT / "wahojobs" / "db" / "migrations").glob("*.sql")
        )
        self.assertEqual(
            [path.name for path in migrations],
            [
                "001_pipeline_state.sql",
                "002_accounts_sessions.sql",
                "003_product_principals.sql",
                "004_persistent_product_profiles.sql",
                "005_persistent_profile_canonical_v2.sql",
                "006_google_oidc_authorization_transactions.sql",
                "007_closed_schema_convergence.sql",
                "008_workos_authkit_provider.sql",
            ],
        )


class DurableGoogleLoginB21DatabaseOwnershipTests(unittest.TestCase):
    _B21_REAPED_STATUS_UNKNOWN = object()

    class _B21PipeEndpoint:
        __slots__ = ("_stream", "_identity")

        def __init__(self, descriptor, mode):
            self._stream = os.fdopen(
                descriptor,
                mode,
                buffering=0,
                closefd=True,
            )
            self._identity = object()

        @property
        def identity(self):
            return self._identity

        @property
        def closed(self):
            return self._stream.closed

        def fileno(self):
            return self._stream.fileno()

        def __index__(self):
            return self.fileno()

        def close(self):
            self._stream.close()

    @classmethod
    def _b21_pipe(cls):
        reader_descriptor, writer_descriptor = os.pipe()
        reader = None
        try:
            reader = cls._B21PipeEndpoint(reader_descriptor, "rb")
            writer = cls._B21PipeEndpoint(writer_descriptor, "wb")
        except BaseException:
            if reader is None:
                os.close(reader_descriptor)
            else:
                reader.close()
            os.close(writer_descriptor)
            raise
        return reader, writer

    @staticmethod
    def _target(runtime_module, state):
        configuration = runtime_module._load_construction_configuration(
            state.configuration_path
        )
        return configuration.database_target

    @staticmethod
    def _response_values(response, name):
        lowered = name.lower()
        return tuple(
            value
            for candidate, value in response.headers
            if candidate.lower() == lowered
        )

    @classmethod
    def _prepared_callback_request(cls, runtime, state):
        browser = runtime.browser_integration
        origin = runtime.configuration.public_origin
        authority = urlsplit(origin).netloc
        with loopback_and_in_memory_provider_only():
            login = browser.handle(
                "GET",
                "/login",
                (("Host", authority),),
            )
            login_cookies = {}
            for value in cls._response_values(login, "Set-Cookie"):
                name, content = value.split(";", 1)[0].split("=", 1)
                login_cookies[name] = content
            csrf = login_cookies["__Host-wahojobs_login_csrf"]
            login.acknowledge_delivery()

            body = form_body(csrf=csrf)
            start = browser.handle(
                "POST",
                "/auth/google/start",
                (
                    ("Host", authority),
                    ("Origin", origin),
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
            locations = cls._response_values(start, "Location")
            start_cookies = {}
            for value in cls._response_values(start, "Set-Cookie"):
                name, content = value.split(";", 1)[0].split("=", 1)
                start_cookies[name] = content
            if start.status != 303 or len(locations) != 1:
                raise AssertionError("b21_pending_start_failed")
            transaction_cookie = start_cookies[
                "__Host-wahojobs_google_tx"
            ]
            start.acknowledge_delivery()

            callback_url = provider_callback_for(
                state,
                locations[0],
                code="b21-authority-code",
            )
            callback_parts = urlsplit(callback_url)
        return (
            browser,
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

    @classmethod
    def _pending_delivery_response(cls, runtime, state):
        browser, target, headers = cls._prepared_callback_request(
            runtime,
            state,
        )
        with loopback_and_in_memory_provider_only():
            callback = browser.handle(
                "GET",
                target,
                headers,
            )
        if callback.status != 303:
            raise AssertionError("b21_pending_callback_failed")
        return callback

    @staticmethod
    def _executed_source_line(function, needle, *, occurrence=1, offset=0):
        lines, first_line = inspect.getsourcelines(function)
        matches = [
            first_line + index
            for index, line in enumerate(lines)
            if needle in line
        ]
        if len(matches) < occurrence:
            raise AssertionError(
                f"b21_executed_boundary_missing:{function.__name__}:{needle}"
            )
        return matches[occurrence - 1] + offset

    @staticmethod
    def _interrupt_executed_line(
        function,
        target_line,
        operation,
        injected,
    ):
        fired = False
        captured = {}

        def trace_target(frame, event, _argument):
            nonlocal fired
            if (
                not fired
                and event == "line"
                and frame.f_lineno == target_line
            ):
                fired = True
                captured.update(frame.f_locals)
                sys.settrace(None)
                raise injected
            return trace_target

        def trace_calls(frame, event, _argument):
            if event == "call" and frame.f_code is function.__code__:
                return trace_target
            return None

        previous = sys.gettrace()
        sys.settrace(trace_calls)
        try:
            result = None
            caught = None
            try:
                result = operation()
            except BaseException as exc:
                caught = exc
                exc = None
            return result, caught, captured, fired
        finally:
            sys.settrace(previous)

    @staticmethod
    def _run_isolated_python(source):
        return subprocess.run(
            [sys.executable, "-B", "-c", source],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )

    @staticmethod
    def _b21_child_failure_envelope(
        error,
        *,
        stage,
        cleanup_failures=(),
    ):
        details = "".join(traceback.format_exception(error))[-12288:]
        for cleanup_stage, cleanup_error in cleanup_failures:
            cleanup_details = "".join(
                traceback.format_exception(cleanup_error)
            )[-4096:]
            details = (
                details
                + "\ncleanup_stage="
                + str(cleanup_stage)[:256]
                + "\n"
                + cleanup_details
            )[-12288:]
        return {
            "ok": False,
            "stage": str(stage)[:256],
            "type": type(error).__name__[:256],
            "message": str(error)[:2048],
            "traceback": details,
        }

    @staticmethod
    def _b21_encode_child_envelope(envelope):
        document = json.dumps(
            envelope,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        if not document or len(document) > 32768:
            raise ValueError("b21_child_document_size")
        return document

    @staticmethod
    def _b21_strict_json_value(document):
        if type(document) is not bytes:
            raise AssertionError("b21_child_document_type")

        def reject_duplicate_members(pairs):
            value = {}
            for name, member in pairs:
                if name in value:
                    raise ValueError("b21_child_duplicate_member")
                value[name] = member
            return value

        def reject_nonstandard_constant(_value):
            raise ValueError("b21_child_nonstandard_constant")

        try:
            text = document.decode("utf-8", "strict")
            decoder = json.JSONDecoder(
                object_pairs_hook=reject_duplicate_members,
                parse_constant=reject_nonstandard_constant,
            )
            value, end = decoder.raw_decode(text)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            raise AssertionError("b21_child_document_invalid") from error
        if end != len(text):
            raise AssertionError("b21_child_document_trailing_material")
        return value

    @classmethod
    def _b21_decode_exact_json_object(
        cls,
        document,
        *,
        field_types,
    ):
        value = cls._b21_strict_json_value(document)
        if type(value) is not dict or set(value) != set(field_types):
            raise AssertionError("b21_child_object_shape")
        if any(
            type(value[name]) is not expected_type
            for name, expected_type in field_types.items()
        ):
            raise AssertionError("b21_child_object_type")
        return value

    @classmethod
    def _b21_decode_reload_summary(cls, document, *, expected_seed):
        value = cls._b21_strict_json_value(document)
        if type(value) is not dict or set(value) != {"child", "parent"}:
            raise AssertionError("reload_summary_shape")
        child_types = {
            name: bool
            for name in (
                "callbacks_share_globals",
                "registered_once",
                "callback_preserved",
                "callback_fifo",
                "callback_locks_distinct",
                "sole_reset_lock_authoritative",
                "interposed_publication_once",
                "interposed_publication_authoritative",
                "child_lock_replaced",
                "child_epoch_fresh",
                "child_runtime_fresh",
                "child_runtime_cleanup",
            )
        }
        child_types["hash_seed"] = type(expected_seed)
        parent_types = {
            name: bool
            for name in (
                "reload_epoch_replaced",
                "reload_lock_replaced",
                "parent_epoch_unchanged",
                "parent_lock_unchanged",
            )
        }
        child = value["child"]
        parent = value["parent"]
        if (
            type(child) is not dict
            or set(child) != set(child_types)
            or any(
                type(child[name]) is not expected_type
                for name, expected_type in child_types.items()
            )
            or child["hash_seed"] != expected_seed
        ):
            raise AssertionError("reload_summary_child_shape")
        if (
            type(parent) is not dict
            or set(parent) != set(parent_types)
            or any(
                type(parent[name]) is not expected_type
                for name, expected_type in parent_types.items()
            )
        ):
            raise AssertionError("reload_summary_parent_shape")
        return child, parent

    @classmethod
    def _b21_decode_child_envelope(
        cls,
        document,
        *,
        outcome_types,
    ):
        if (
            type(document) is not bytes
            or not document
            or len(document) > 32768
        ):
            raise AssertionError("b21_child_document_size")
        envelope = cls._b21_strict_json_value(document)
        if type(envelope) is not dict or type(envelope.get("ok")) is not bool:
            raise AssertionError("b21_child_envelope_invalid")
        if envelope["ok"] is True:
            if set(envelope) != {"ok", "stage", "outcome"}:
                raise AssertionError("b21_child_success_shape")
            if envelope["stage"] != "complete":
                raise AssertionError("b21_child_success_stage")
            outcome = envelope["outcome"]
            if type(outcome) is not dict or set(outcome) != set(outcome_types):
                raise AssertionError("b21_child_success_outcome_shape")
            if any(
                type(outcome[name]) is not expected_type
                for name, expected_type in outcome_types.items()
            ):
                raise AssertionError("b21_child_success_outcome_type")
            return True, outcome
        if set(envelope) != {
            "ok",
            "stage",
            "type",
            "message",
            "traceback",
        }:
            raise AssertionError("b21_child_failure_shape")
        if (
            type(envelope["stage"]) is not str
            or not envelope["stage"]
            or type(envelope["type"]) is not str
            or not envelope["type"]
            or type(envelope["message"]) is not str
            or type(envelope["traceback"]) is not str
        ):
            raise AssertionError("b21_child_failure_type")
        return False, envelope

    @staticmethod
    def _b21_waitpid_until(pid, *, deadline):
        while True:
            try:
                waited, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return (
                    DurableGoogleLoginB21DatabaseOwnershipTests
                    ._B21_REAPED_STATUS_UNKNOWN
                )
            except InterruptedError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("b21_child_reap_timeout")
                continue
            if waited == pid:
                return status
            if waited != 0:
                raise AssertionError("b21_child_wrong_pid")
            if time.monotonic() >= deadline:
                raise TimeoutError("b21_child_reap_timeout")

    @classmethod
    def _b21_waitpid_bounded(cls, pid, *, timeout):
        return cls._b21_waitpid_until(
            pid,
            deadline=time.monotonic() + timeout,
        )

    @staticmethod
    def _b21_probe_exact_child(pid, *, deadline):
        while True:
            try:
                waited, status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return (
                    True,
                    DurableGoogleLoginB21DatabaseOwnershipTests
                    ._B21_REAPED_STATUS_UNKNOWN,
                )
            except InterruptedError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("b21_child_reap_timeout")
                continue
            if waited == pid:
                return True, status
            if waited == 0:
                return False, None
            raise AssertionError("b21_child_wrong_pid")

    @classmethod
    def _b21_terminate_and_reap(
        cls,
        pid,
        *,
        deadline=None,
        timeout=10,
    ):
        if deadline is None:
            deadline = time.monotonic() + timeout
        reaped, status = cls._b21_probe_exact_child(
            pid,
            deadline=deadline,
        )
        if reaped:
            return status
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        reaped, status = cls._b21_probe_exact_child(
            pid,
            deadline=deadline,
        )
        if reaped:
            return status
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return cls._b21_waitpid_until(pid, deadline=deadline)

    @staticmethod
    def _b21_read_pipe_bounded(
        descriptor,
        *,
        timeout,
        maximum_size=32768,
    ):
        if type(maximum_size) is not int or maximum_size < 1:
            raise AssertionError("b21_child_document_size")
        chunks = []
        size = 0
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("b21_child_pipe_timeout")
            try:
                readable, _writable, _exceptional = select.select(
                    [descriptor],
                    [],
                    [],
                    remaining,
                )
            except InterruptedError:
                continue
            if not readable:
                raise TimeoutError("b21_child_pipe_timeout")
            requested = min(4096, maximum_size + 1 - size)
            try:
                chunk = os.read(descriptor, requested)
            except InterruptedError:
                continue
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > maximum_size:
                raise AssertionError("b21_child_document_too_large")
            chunks.append(chunk)

    @classmethod
    def _b21_close_endpoints_and_reap(
        cls,
        *,
        pid,
        reaped,
        endpoints,
        timeout=10,
    ):
        deadline = time.monotonic() + timeout
        failures = []
        seen = set()
        for stage, endpoint in endpoints:
            if endpoint is None or endpoint.identity in seen:
                continue
            seen.add(endpoint.identity)
            for attempt in range(2):
                if endpoint.closed:
                    break
                try:
                    endpoint.close()
                except BaseException as error:
                    failures.append(
                        (f"{stage}_attempt_{attempt + 1}", error)
                    )
            if not endpoint.closed:
                unresolved = AssertionError(
                    "b21_child_endpoint_unresolved"
                )
                unresolved.endpoint_owner = endpoint
                failures.append(
                    (
                        f"{stage}_unresolved",
                        unresolved,
                    )
                )
        status = None
        if pid not in {None, 0} and not reaped:
            for attempt in range(1, 3):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    status = cls._b21_terminate_and_reap(
                        pid,
                        deadline=deadline,
                    )
                except BaseException as error:
                    failures.append(
                        (f"child_reap_attempt_{attempt}", error)
                    )
                if status is not None:
                    break
            if status is None and time.monotonic() < deadline:
                try:
                    reaped_child, status = cls._b21_probe_exact_child(
                        pid,
                        deadline=deadline,
                    )
                    if not reaped_child:
                        try:
                            os.kill(pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                        reaped_child, status = (
                            cls._b21_probe_exact_child(
                                pid,
                                deadline=deadline,
                            )
                        )
                        if not reaped_child:
                            try:
                                os.kill(pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            status = cls._b21_waitpid_until(
                                pid,
                                deadline=deadline,
                            )
                except BaseException as error:
                    failures.append(("child_reap_final", error))
            if status is None:
                failures.append(
                    (
                        "child_reap_unresolved",
                        TimeoutError("b21_child_reap_timeout"),
                    )
                )
            elif status is cls._B21_REAPED_STATUS_UNKNOWN:
                failures.append(
                    (
                        "child_reap_status_unavailable",
                        ChildProcessError(
                            "b21_child_reaped_without_status"
                        ),
                    )
                )
        return status, tuple(failures)

    @staticmethod
    def _b21_preserve_primary_cleanup_failures(failures):
        if not failures:
            return
        primary = sys.exception()
        if primary is None:
            primary = failures[0][1]
            remaining_failures = failures[1:]
        else:
            remaining_failures = failures
        for stage, error in remaining_failures:
            primary.add_note(
                "b21_cleanup_failure:"
                + str(stage)[:256]
                + ":"
                + type(error).__name__[:256]
                + ":"
                + str(error)[:1024]
            )
        if sys.exception() is None:
            raise primary

    @staticmethod
    def _b21_close_runtime_owner(runtime_owner, *, stage):
        failures = []
        for attempt in range(2):
            try:
                report = runtime_owner.close(_preserve_primary=True)
                cleanup_complete = (
                    type(getattr(report, "cleanup_complete", None)) is bool
                    and report.cleanup_complete is True
                )
            except BaseException as error:
                failures.append(
                    (f"{stage}_attempt_{attempt + 1}", error)
                )
                continue
            if cleanup_complete:
                return None, tuple(failures)
            failures.append(
                (
                    f"{stage}_attempt_{attempt + 1}",
                    AssertionError("b21_runtime_cleanup_incomplete"),
                )
            )
        return runtime_owner, tuple(failures)

    @staticmethod
    def _b21_success_after_harness_cleanup(outcome, harnesses):
        if len({id(harness) for harness in harnesses}) != len(harnesses):
            raise AssertionError("b21_child_provider_owner_duplicate")
        while harnesses:
            harness = harnesses[-1]
            gateway_record = None
            if hasattr(harness, "gateway"):
                gateway_record = object.__getattribute__(
                    harness.gateway,
                    "_record",
                )
            harness.close()
            if (
                gateway_record is not None
                and (
                    not gateway_record.closed
                    or gateway_record.provider_adapter is not None
                    or gateway_record.cache is not None
                    or gateway_record.configuration_record is not None
                )
            ):
                raise AssertionError(
                    "b21_child_gateway_cleanup_incomplete"
                )
            if (
                hasattr(harness, "transport")
                and harness.transport is not None
                and not object.__getattribute__(
                    harness.transport,
                    "_closed",
                )
            ):
                raise AssertionError(
                    "b21_child_provider_cleanup_incomplete"
                )
            harnesses.pop()
        if "provider_cleanup_complete" in outcome:
            outcome["provider_cleanup_complete"] = True
        return {
            "ok": True,
            "stage": "complete",
            "outcome": outcome,
        }

    def _b21_assert_success_requires_child_exit(self):
        reader = None
        writer = None
        control_reader = None
        control_writer = None
        pid = None
        reaped = False
        outcome_types = {"probe": bool}
        try:
            reader, writer = self._b21_pipe()
            control_reader, control_writer = self._b21_pipe()
            pid = os.fork()
            if pid == 0:
                reader.close()
                control_writer.close()
                document = self._b21_encode_child_envelope(
                    {
                        "ok": True,
                        "stage": "complete",
                        "outcome": {"probe": True},
                    }
                )
                offset = 0
                while offset < len(document):
                    written = os.write(writer, document[offset:])
                    if written <= 0:
                        os._exit(2)
                    offset += written
                writer.close()
                os.read(control_reader, 1)
                os._exit(0)
            writer.close()
            writer = None
            control_reader.close()
            control_reader = None
            document = self._b21_read_pipe_bounded(reader, timeout=10)
            success, outcome = self._b21_decode_child_envelope(
                document,
                outcome_types=outcome_types,
            )
            self.assertTrue(success)
            self.assertEqual(outcome, {"probe": True})
            with self.assertRaisesRegex(
                TimeoutError,
                "b21_child_reap_timeout",
            ):
                self._b21_waitpid_bounded(pid, timeout=0.25)
            status = self._b21_terminate_and_reap(pid)
            reaped = True
            self.assertIs(type(status), int)
            self.assertLess(
                os.waitstatus_to_exitcode(status),
                0,
            )
        finally:
            _status, cleanup_failures = (
                self._b21_close_endpoints_and_reap(
                    pid=pid,
                    reaped=reaped,
                    endpoints=(
                        ("result_reader_close", reader),
                        ("result_writer_close", writer),
                        ("control_reader_close", control_reader),
                        ("control_writer_close", control_writer),
                    ),
                )
            )
            self._b21_preserve_primary_cleanup_failures(
                cleanup_failures
            )

    def test_b21_epoch_publication_race_returns_one_authoritative_epoch(
        self,
    ):
        import wahojobs.durable_google_login_runtime as runtime_module

        with temporary_browser_login_state() as state:
            target = self._target(runtime_module, state)
            saved_epoch = runtime_module._DATABASE_PROCESS_EPOCH
            saved_lock = (
                runtime_module
                ._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK
            )
            saved_pid = (
                runtime_module
                ._DATABASE_PROCESS_EPOCH_PUBLICATION_PID
            )
            entered = 0
            entered_lock = threading.Lock()
            both_entered = threading.Event()
            release = threading.Event()
            returned = []
            failures = []

            def proof():
                nonlocal entered
                with entered_lock:
                    entered += 1
                    value = bytes([entered]) * 32
                    if entered == 2:
                        both_entered.set()
                if not release.wait(5):
                    raise AssertionError("epoch_candidate_release_timeout")
                return value

            def initialize():
                try:
                    epoch = (
                        runtime_module._current_database_process_epoch()
                    )
                    manager = (
                        runtime_module._RuntimeDatabaseConnections(target)
                    )
                    returned.append((epoch, manager))
                except BaseException as exc:
                    failures.append(exc)

            try:
                runtime_module._DATABASE_PROCESS_EPOCH = None
                runtime_module._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK = (
                    threading.Lock()
                )
                runtime_module._DATABASE_PROCESS_EPOCH_PUBLICATION_PID = (
                    os.getpid()
                )
                with mock.patch.object(
                    runtime_module,
                    "_new_database_process_proof",
                    side_effect=proof,
                ):
                    threads = [
                        threading.Thread(
                            target=initialize,
                            daemon=False,
                        )
                        for _index in range(2)
                    ]
                    for thread in threads:
                        thread.start()
                    self.assertTrue(both_entered.wait(5))
                    publication_lock = (
                        runtime_module
                        ._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK
                    )
                    self.assertTrue(
                        publication_lock.acquire(blocking=False)
                    )
                    publication_lock.release()
                    release.set()
                    for thread in threads:
                        thread.join(5)
                    self.assertTrue(
                        all(not thread.is_alive() for thread in threads)
                    )
                self.assertEqual(failures, [])
                self.assertEqual(len(returned), 2)
                winner = returned[0][0]
                self.assertIs(returned[1][0], winner)
                self.assertIs(
                    runtime_module._DATABASE_PROCESS_EPOCH,
                    winner,
                )
                for epoch, manager in returned:
                    self.assertIs(
                        object.__getattribute__(
                            manager,
                            "_process_epoch",
                        ),
                        epoch,
                    )
                    self.assertTrue(manager.close())
                    self.assertTrue(manager.closed)
            finally:
                release.set()
                runtime_module._DATABASE_PROCESS_EPOCH = saved_epoch
                runtime_module._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK = (
                    saved_lock
                )
                runtime_module._DATABASE_PROCESS_EPOCH_PUBLICATION_PID = (
                    saved_pid
                )

    def test_b21_epoch_proof_exceptions_preserve_publication_and_lock(
        self,
    ):
        import wahojobs.durable_google_login_runtime as runtime_module

        saved_epoch = runtime_module._DATABASE_PROCESS_EPOCH
        saved_lock = (
            runtime_module._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK
        )
        saved_pid = runtime_module._DATABASE_PROCESS_EPOCH_PUBLICATION_PID
        source = Path(runtime_module.__file__).read_text(
            encoding="utf-8"
        ).splitlines()
        epoch_code = (
            runtime_module._current_database_process_epoch.__code__
        )
        publication_line = next(
            index + 1
            for index in range(
                epoch_code.co_firstlineno - 1,
                epoch_code.co_firstlineno + 100,
            )
            if source[index].strip()
            == "_DATABASE_PROCESS_EPOCH = candidate"
        )
        try:
            for exception_type in (
                RuntimeError,
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ):
                with self.subTest(exception_type=exception_type.__name__):
                    runtime_module._DATABASE_PROCESS_EPOCH = None
                    runtime_module._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK = (
                        threading.Lock()
                    )
                    runtime_module._DATABASE_PROCESS_EPOCH_PUBLICATION_PID = (
                        os.getpid()
                    )
                    injected = exception_type(
                        "PRIVATE_B21_EPOCH_PROOF"
                    )
                    with mock.patch.object(
                        runtime_module,
                        "_new_database_process_proof",
                        side_effect=injected,
                    ):
                        with self.assertRaises(exception_type) as caught:
                            (
                                runtime_module
                                ._current_database_process_epoch()
                            )
                    self.assertIs(caught.exception, injected)
                    self.assertIsNone(
                        runtime_module._DATABASE_PROCESS_EPOCH
                    )
                    publication_lock = (
                        runtime_module
                        ._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK
                    )
                    self.assertTrue(
                        publication_lock.acquire(blocking=False)
                    )
                    publication_lock.release()

                    winner = (
                        runtime_module._current_database_process_epoch()
                    )
                    with mock.patch.object(
                        runtime_module,
                        "_new_database_process_proof",
                        side_effect=AssertionError(
                            "published_epoch_must_not_regenerate"
                        ),
                    ):
                        self.assertIs(
                            runtime_module
                            ._current_database_process_epoch(),
                            winner,
                        )
                    with self.assertRaises(AttributeError):
                        winner.pid = os.getpid()
                    self.assertIs(
                        runtime_module._DATABASE_PROCESS_EPOCH,
                        winner,
                    )

                    runtime_module._DATABASE_PROCESS_EPOCH = None
                    injected_publication = exception_type(
                        "PRIVATE_B21_EPOCH_PUBLICATION"
                    )
                    fired = False

                    def trace(frame, event, _argument):
                        nonlocal fired
                        if (
                            not fired
                            and event == "line"
                            and frame.f_code is epoch_code
                            and frame.f_lineno == publication_line
                        ):
                            fired = True
                            sys.settrace(None)
                            raise injected_publication
                        return trace

                    sys.settrace(trace)
                    try:
                        with self.assertRaises(
                            exception_type
                        ) as caught:
                            (
                                runtime_module
                                ._current_database_process_epoch()
                            )
                        self.assertIs(
                            caught.exception,
                            injected_publication,
                        )
                    finally:
                        sys.settrace(None)
                    self.assertTrue(fired)
                    self.assertIsNone(
                        runtime_module._DATABASE_PROCESS_EPOCH
                    )
                    self.assertTrue(
                        publication_lock.acquire(blocking=False)
                    )
                    publication_lock.release()
                    replacement = (
                        runtime_module._current_database_process_epoch()
                    )
                    self.assertIs(
                        runtime_module._DATABASE_PROCESS_EPOCH,
                        replacement,
                    )
        finally:
            runtime_module._DATABASE_PROCESS_EPOCH = saved_epoch
            runtime_module._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK = (
                saved_lock
            )
            runtime_module._DATABASE_PROCESS_EPOCH_PUBLICATION_PID = (
                saved_pid
            )

    def test_b21_connection_proof_generation_never_holds_manager_condition(
        self,
    ):
        import wahojobs.durable_google_login_runtime as runtime_module

        for exception_type in (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(exception_type=exception_type.__name__):
                with temporary_browser_login_state() as state:
                    target = self._target(runtime_module, state)
                    manager = (
                        runtime_module._RuntimeDatabaseConnections(target)
                    )
                    injected = exception_type(
                        "PRIVATE_B21_CONNECTION_PROOF"
                    )
                    with mock.patch.object(
                        runtime_module,
                        "_new_database_connection_proof",
                        side_effect=injected,
                    ):
                        with self.assertRaises(exception_type) as caught:
                            manager.open_writable_connection()
                    self.assertIs(caught.exception, injected)
                    self.assertEqual(
                        object.__getattribute__(manager, "_records"),
                        {},
                    )
                    self.assertEqual(
                        object.__getattribute__(
                            manager,
                            "_next_generation",
                        ),
                        1,
                    )
                    self.assertTrue(manager.close())

        with temporary_browser_login_state() as state:
            target = self._target(runtime_module, state)
            manager = runtime_module._RuntimeDatabaseConnections(target)
            entered = threading.Event()
            release = threading.Event()
            close_finished = threading.Event()
            opener_outcome = []
            close_outcome = []
            original = runtime_module._new_database_connection_proof

            def blocked_proof():
                entered.set()
                if not release.wait(5):
                    raise AssertionError(
                        "connection_proof_release_timeout"
                    )
                return original()

            def opener():
                try:
                    manager.open_writable_connection()
                except BaseException as exc:
                    opener_outcome.append(exc)

            def closer():
                try:
                    close_outcome.append(manager.close())
                finally:
                    close_finished.set()

            with mock.patch.object(
                runtime_module,
                "_new_database_connection_proof",
                side_effect=blocked_proof,
            ):
                open_thread = threading.Thread(
                    target=opener,
                    daemon=False,
                )
                close_thread = threading.Thread(
                    target=closer,
                    daemon=False,
                )
                open_thread.start()
                self.assertTrue(entered.wait(5))
                close_thread.start()
                try:
                    self.assertTrue(close_finished.wait(5))
                finally:
                    release.set()
                open_thread.join(10)
                close_thread.join(10)
            self.assertFalse(open_thread.is_alive())
            self.assertFalse(close_thread.is_alive())
            self.assertEqual(close_outcome, [True])
            self.assertEqual(len(opener_outcome), 1)
            self.assertIsInstance(
                opener_outcome[0],
                DurableGoogleLoginConfigurationError,
            )
            self.assertEqual(
                object.__getattribute__(manager, "_records"),
                {},
            )
            self.assertTrue(manager.closed)

    def test_b21_foreign_thread_response_rejects_before_delivery_lock(
        self,
    ):
        import wahojobs.durable_google_login_runtime as runtime_module

        with temporary_browser_login_state() as state:
            runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            response = None
            try:
                response = self._pending_delivery_response(
                    runtime,
                    state,
                )
                manager = object.__getattribute__(
                    runtime,
                    "_connections",
                )
                owner = response._owned_connection
                wrapper = response._delivery_lease
                request_release = response._request_release
                browser = runtime.browser_integration
                record = object.__getattribute__(owner, "_record")
                token = object.__getattribute__(owner, "_token")
                self.assertIsNotNone(request_release)
                self.assertEqual(browser.active_request_count, 1)
                self.assertFalse(manager.close())
                self.assertEqual(
                    object.__getattribute__(record, "_state"),
                    "close_pending",
                )

                response_lock = object.__getattribute__(
                    response,
                    "_delivery_lock",
                )
                finished = threading.Event()
                failures = []

                def unauthorized():
                    try:
                        response.acknowledge_delivery()
                    except BaseException as exc:
                        failures.append(exc)
                    finally:
                        finished.set()

                response_lock.acquire()
                thread = threading.Thread(
                    target=unauthorized,
                    daemon=False,
                )
                thread.start()
                try:
                    self.assertTrue(finished.wait(5))
                finally:
                    response_lock.release()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(failures), 1)
                self.assertIsInstance(
                    failures[0],
                    DurableGoogleLoginConfigurationError,
                )
                self.assertIs(response._owned_connection, owner)
                self.assertIs(response._delivery_lease, wrapper)
                self.assertIs(response._request_release, request_release)
                self.assertEqual(response._delivery_state, ["pending"])
                self.assertEqual(browser.active_request_count, 1)
                self.assertIs(
                    object.__getattribute__(record, "_borrower_token"),
                    token,
                )
                self.assertEqual(
                    object.__getattribute__(record, "_state"),
                    "close_pending",
                )

                close_calls = []
                original_cleanup = (
                    runtime_module
                    ._cleanup_database_connection_independently
                )

                def count_cleanup(connection, *, rollback):
                    close_calls.append(connection)
                    return original_cleanup(
                        connection,
                        rollback=rollback,
                    )

                with mock.patch.object(
                    runtime_module,
                    "_cleanup_database_connection_independently",
                    side_effect=count_cleanup,
                ):
                    response.fail_delivery()
                self.assertIsNone(response._request_release)
                self.assertEqual(browser.active_request_count, 0)
                response = None
                self.assertEqual(len(close_calls), 1)
                self.assertEqual(
                    object.__getattribute__(manager, "_records"),
                    {},
                )
                self.assertTrue(manager.close())
                report = runtime.close()
                runtime = None
                self.assertTrue(report.cleanup_complete)
            finally:
                if response is not None:
                    try:
                        response.fail_delivery()
                    except BaseException:
                        pass
                if runtime is not None:
                    runtime.close(_preserve_primary=True)

    def test_b21_foreign_thread_delivery_wrapper_rejects_before_raw_lock(
        self,
    ):
        import wahojobs.durable_google_login_browser as browser_module

        with temporary_browser_login_state() as state:
            runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            response = None
            try:
                response = self._pending_delivery_response(
                    runtime,
                    state,
                )
                wrapper = response._delivery_lease
                self.assertIs(
                    type(wrapper),
                    browser_module
                    ._AuthorityFencedSessionDeliveryLease,
                )
                raw = object.__getattribute__(
                    wrapper,
                    "_AuthorityFencedSessionDeliveryLease__lease",
                )
                raw_lock = object.__getattribute__(raw, "_lock")
                owner = response._owned_connection
                manager = object.__getattribute__(
                    runtime,
                    "_connections",
                )
                manager_condition = object.__getattribute__(
                    manager,
                    "_condition",
                )
                record = object.__getattribute__(owner, "_record")
                token = object.__getattribute__(owner, "_token")
                state_before = object.__getattribute__(
                    record,
                    "_state",
                )
                finished = threading.Event()
                failures = []

                def unauthorized():
                    try:
                        wrapper.fail_delivery()
                    except BaseException as exc:
                        failures.append(exc)
                    finally:
                        finished.set()

                manager_condition.acquire()
                raw_lock.acquire()
                thread = threading.Thread(
                    target=unauthorized,
                    daemon=False,
                )
                thread.start()
                try:
                    self.assertTrue(finished.wait(5))
                finally:
                    raw_lock.release()
                    manager_condition.release()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(failures), 1)
                self.assertIsInstance(
                    failures[0],
                    DurableGoogleLoginConfigurationError,
                )
                self.assertIs(response._delivery_lease, wrapper)
                self.assertIs(response._owned_connection, owner)
                self.assertEqual(
                    object.__getattribute__(raw, "_status"),
                    "prepared",
                )
                self.assertEqual(
                    object.__getattribute__(record, "_state"),
                    state_before,
                )
                self.assertIs(
                    object.__getattribute__(record, "_borrower_token"),
                    token,
                )
                response.fail_delivery()
                response = None
                self.assertEqual(
                    object.__getattribute__(manager, "_records"),
                    {},
                )
                report = runtime.close()
                runtime = None
                self.assertTrue(report.cleanup_complete)
            finally:
                if response is not None:
                    try:
                        response.fail_delivery()
                    except BaseException:
                        pass
                if runtime is not None:
                    runtime.close(_preserve_primary=True)

    def test_b21_response_validation_interruptions_preserve_pending_owner(
        self,
    ):
        import wahojobs.durable_google_login_runtime as runtime_module

        for exception_type in (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(exception_type=exception_type.__name__):
                with temporary_browser_login_state() as state:
                    runtime = build_durable_google_login_runtime(
                        state.configuration_path,
                        _clock=state.clock,
                        _gateway_factory=state.gateway_factory,
                    )
                    response = None
                    try:
                        response = self._pending_delivery_response(
                            runtime,
                            state,
                        )
                        browser = runtime.browser_integration
                        manager = object.__getattribute__(
                            runtime,
                            "_connections",
                        )
                        wrapper = response._delivery_lease
                        owner = response._owned_connection
                        request_release = response._request_release
                        record = object.__getattribute__(owner, "_record")
                        token = object.__getattribute__(owner, "_token")
                        self.assertFalse(manager.close())
                        self.assertEqual(
                            object.__getattribute__(record, "_state"),
                            "close_pending",
                        )
                        injected = exception_type(
                            "PRIVATE_B21_DELIVERY_VALIDATION"
                        )
                        with mock.patch.object(
                            runtime_module._RuntimeDatabaseConnections,
                            "_validate_delivery_authority",
                            side_effect=injected,
                        ):
                            with self.assertRaises(
                                exception_type
                            ) as caught:
                                response.fail_delivery()
                        self.assertIs(caught.exception, injected)
                        self.assertIs(response._delivery_lease, wrapper)
                        self.assertIs(response._owned_connection, owner)
                        self.assertIs(
                            response._request_release,
                            request_release,
                        )
                        self.assertEqual(
                            response._delivery_state,
                            ["pending"],
                        )
                        self.assertEqual(browser.active_request_count, 1)
                        self.assertIs(
                            object.__getattribute__(
                                record,
                                "_borrower_token",
                            ),
                            token,
                        )
                        self.assertEqual(
                            object.__getattribute__(record, "_state"),
                            "close_pending",
                        )

                        response.fail_delivery()
                        self.assertIsNone(response._request_release)
                        self.assertEqual(browser.active_request_count, 0)
                        response = None
                        self.assertEqual(
                            object.__getattribute__(manager, "_records"),
                            {},
                        )
                        self.assertEqual(
                            object.__getattribute__(
                                record,
                                "_connection_offer",
                            ),
                            [],
                        )
                        for field_name in (
                            "_borrower_thread",
                            "_borrower_token",
                            "_release_thread",
                            "_release_token",
                            "_cleanup_thread",
                            "_cleanup_token",
                        ):
                            self.assertIsNone(
                                object.__getattribute__(
                                    record,
                                    field_name,
                                )
                            )
                        report = runtime.close()
                        runtime = None
                        self.assertTrue(report.cleanup_complete)
                    finally:
                        if response is not None:
                            try:
                                response.fail_delivery()
                            except BaseException:
                                pass
                        if runtime is not None:
                            runtime.close(_preserve_primary=True)

    def test_b21_native_and_connection_publication_interruptions_keep_exact_owner(
        self,
    ):
        import wahojobs.durable_google_login_runtime as runtime_module

        for boundary in ("native", "connection"):
            for exception_type in (
                RuntimeError,
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ):
                with self.subTest(
                    boundary=boundary,
                    exception_type=exception_type.__name__,
                ):
                    with temporary_browser_login_state() as state:
                        target = self._target(runtime_module, state)
                        manager = runtime_module._RuntimeDatabaseConnections(
                            target
                        )
                        injected = exception_type(
                            "PRIVATE_B21_PUBLICATION_BOUNDARY"
                        )
                        if boundary == "native":
                            original = (
                                runtime_module
                                ._publish_database_descriptor_handle
                            )

                            def interrupt(offer, path):
                                original(offer, path)
                                raise injected

                            patcher = mock.patch.object(
                                runtime_module,
                                "_publish_database_descriptor_handle",
                                side_effect=interrupt,
                            )
                        else:
                            original = (
                                runtime_module._publish_database_call_result
                            )

                            def interrupt(offer, callback, arguments):
                                original(offer, callback, arguments)
                                raise injected

                            patcher = mock.patch.object(
                                runtime_module,
                                "_publish_database_call_result",
                                side_effect=interrupt,
                            )
                        with patcher:
                            with self.assertRaises(
                                exception_type
                            ) as caught:
                                manager.open_writable_connection()
                        self.assertIs(caught.exception, injected)
                        self.assertEqual(
                            object.__getattribute__(manager, "_records"),
                            {},
                        )
                        self.assertTrue(manager.close())
                        self.assertTrue(manager.closed)
                        probe = sqlite3.connect(state.database_path)
                        try:
                            self.assertGreater(
                                probe.execute(
                                    "PRAGMA schema_version"
                                ).fetchone()[0],
                                0,
                            )
                        finally:
                            probe.close()

    def test_b21_open_registration_and_lease_return_interruptions_are_reclaimed(
        self,
    ):
        import wahojobs.durable_google_login_runtime as runtime_module

        source = Path(runtime_module.__file__).read_text(
            encoding="utf-8"
        ).splitlines()
        code = (
            runtime_module._RuntimeDatabaseConnections
            ._finish_connection_open.__code__
        )
        targets = {
            "after_registration": next(
                index + 1
                for index in range(
                    code.co_firstlineno - 1,
                    code.co_firstlineno + 130,
                )
                if source[index].strip() == "record.open("
            ),
            "after_lease_publication": next(
                index + 1
                for index in range(
                    code.co_firstlineno - 1,
                    code.co_firstlineno + 130,
                )
                if source[index].strip() == "return lease"
            ),
        }
        self.assertNotIn(
            "os",
            runtime_module._publish_database_descriptor_handle
            .__code__.co_names,
        )
        for boundary, target_line in targets.items():
            for exception_type in (
                RuntimeError,
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ):
                with self.subTest(
                    boundary=boundary,
                    exception_type=exception_type.__name__,
                ):
                    with temporary_browser_login_state() as state:
                        target = self._target(runtime_module, state)
                        manager = (
                            runtime_module._RuntimeDatabaseConnections(
                                target
                            )
                        )
                        injected = exception_type(
                            "PRIVATE_B21_OPEN_HANDOFF"
                        )
                        fired = False

                        def trace(frame, event, _argument):
                            nonlocal fired
                            if (
                                not fired
                                and event == "line"
                                and frame.f_code is code
                                and frame.f_lineno == target_line
                            ):
                                fired = True
                                sys.settrace(None)
                                raise injected
                            return trace

                        sys.settrace(trace)
                        try:
                            with self.assertRaises(
                                exception_type
                            ) as caught:
                                manager.open_writable_connection()
                            self.assertIs(caught.exception, injected)
                        finally:
                            sys.settrace(None)
                        self.assertTrue(fired)
                        self.assertTrue(manager.close())
                        self.assertTrue(manager.closed)
                        self.assertEqual(
                            object.__getattribute__(
                                manager,
                                "_records",
                            ),
                            {},
                        )

    def test_b21_shutdown_linearizes_with_open_and_published_lease(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        with temporary_browser_login_state() as state:
            target = self._target(runtime_module, state)
            manager = runtime_module._RuntimeDatabaseConnections(target)
            entered = threading.Event()
            release = threading.Event()
            original = runtime_module._DatabaseConnectionOwnership.open
            outcomes = []

            def blocked_open(owner, *args, **kwargs):
                result = original(owner, *args, **kwargs)
                entered.set()
                if not release.wait(5):
                    raise AssertionError("b21_open_release_timeout")
                return result

            def opener():
                try:
                    manager.open_writable_connection()
                except BaseException as exc:
                    outcomes.append(exc)

            with mock.patch.object(
                runtime_module._DatabaseConnectionOwnership,
                "open",
                new=blocked_open,
            ):
                thread = threading.Thread(target=opener, daemon=False)
                thread.start()
                self.assertTrue(entered.wait(5))
                self.assertFalse(manager.close())
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(outcomes), 1)
            self.assertIsInstance(
                outcomes[0],
                DurableGoogleLoginConfigurationError,
            )
            self.assertTrue(manager.close())
            self.assertTrue(manager.closed)

        with temporary_browser_login_state() as state:
            target = self._target(runtime_module, state)
            manager = runtime_module._RuntimeDatabaseConnections(target)
            lease = manager.open_writable_connection()
            lease.execute("BEGIN IMMEDIATE")
            self.assertFalse(manager.close())
            self.assertEqual(
                tuple(lease.execute("SELECT 1").fetchone()),
                (1,),
            )
            self.assertTrue(lease.close())
            self.assertTrue(manager.close())
            self.assertTrue(manager.closed)

    def test_b21_release_rollback_and_shutdown_have_one_close_claim(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        with temporary_browser_login_state() as state:
            target = self._target(runtime_module, state)
            manager = runtime_module._RuntimeDatabaseConnections(target)
            borrower_ready = threading.Event()
            release_borrower = threading.Event()
            cleanup_entered = threading.Event()
            cleanup_release = threading.Event()
            cleanup_calls = []
            failures = []
            original = (
                runtime_module._cleanup_database_connection_independently
            )

            def blocked_cleanup(connection, *, rollback):
                cleanup_calls.append(connection)
                cleanup_entered.set()
                if not cleanup_release.wait(5):
                    raise AssertionError("b21_cleanup_release_timeout")
                return original(connection, rollback=rollback)

            def borrower():
                try:
                    lease = manager.open_writable_connection()
                    lease.execute("BEGIN IMMEDIATE")
                    borrower_ready.set()
                    if not release_borrower.wait(5):
                        raise AssertionError("b21_borrower_release_timeout")
                    lease.close()
                except BaseException as exc:
                    failures.append(exc)

            with mock.patch.object(
                runtime_module,
                "_cleanup_database_connection_independently",
                side_effect=blocked_cleanup,
            ):
                thread = threading.Thread(target=borrower, daemon=False)
                thread.start()
                self.assertTrue(borrower_ready.wait(5))
                self.assertFalse(manager.close())
                release_borrower.set()
                self.assertTrue(cleanup_entered.wait(5))
                self.assertFalse(manager.close())
                cleanup_release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(len(cleanup_calls), 1)
            self.assertTrue(manager.close())
            self.assertTrue(manager.closed)

    def test_b21_foreign_thread_and_stale_lease_token_fail_before_sqlite(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        with temporary_browser_login_state() as state:
            target = self._target(runtime_module, state)
            manager = runtime_module._RuntimeDatabaseConnections(target)
            lease = manager.open_writable_connection()
            cursor = lease.execute("SELECT 1")
            self.assertFalse(hasattr(cursor, "connection"))
            self.assertFalse(hasattr(lease, "commit"))
            self.assertFalse(hasattr(lease, "rollback"))
            self.assertFalse(hasattr(lease, "interrupt"))
            with self.assertRaises(
                DurableGoogleLoginConfigurationError
            ):
                lease._borrow_internal_connection(object())
            failures = []

            def unauthorized():
                for operation in (
                    lambda: lease.execute("SELECT 1"),
                    cursor.fetchone,
                    cursor.close,
                    lease.close,
                ):
                    try:
                        operation()
                    except BaseException as exc:
                        failures.append(exc)

            thread = threading.Thread(target=unauthorized, daemon=False)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(failures), 4)
            self.assertTrue(
                all(
                    isinstance(
                        item,
                        DurableGoogleLoginConfigurationError,
                    )
                    for item in failures
                )
            )
            record = object.__getattribute__(lease, "_record")
            stale = runtime_module._DatabaseConnectionLease(
                manager,
                record,
                object(),
                threading.current_thread(),
            )
            with self.assertRaises(
                DurableGoogleLoginConfigurationError
            ):
                stale.execute("SELECT 1")
            self.assertEqual(
                tuple(lease.execute("SELECT 1").fetchone()),
                (1,),
            )
            self.assertEqual(tuple(cursor.fetchone()), (1,))
            self.assertTrue(cursor.close())
            self.assertTrue(lease.close())
            self.assertTrue(manager.close())
            with self.assertRaises(
                DurableGoogleLoginConfigurationError
            ):
                stale.execute("SELECT 1")

    def test_b21_terminal_descriptor_owner_ignores_reused_numeric_descriptor(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        with temporary_browser_login_state() as state:
            target = self._target(runtime_module, state)
            owner = runtime_module._open_pinned_database_target(target)
            descriptor = owner.descriptor_for_validation()
            self.assertTrue(owner.close())
            unrelated = open(
                state.configuration_path,
                "rb",
                buffering=0,
            )
            try:
                self.assertEqual(unrelated.fileno(), descriptor)
                self.assertTrue(owner.close())
                unrelated.seek(0)
                self.assertTrue(unrelated.read(1))
            finally:
                unrelated.close()
            self.assertTrue(owner.terminal)

        for exception_type in (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(
                ambiguous_close=exception_type.__name__,
            ):
                with temporary_browser_login_state() as state:
                    target = self._target(runtime_module, state)
                    owner = (
                        runtime_module._open_pinned_database_target(
                            target
                        )
                    )
                    handle = object.__getattribute__(
                        owner,
                        "_handle_offer",
                    )[0]
                    descriptor = handle.fileno()
                    injected = exception_type(
                        "PRIVATE_B21_DESCRIPTOR_CLOSE"
                    )

                    class CloseThenRaise:
                        @property
                        def closed(self):
                            return handle.closed

                        def fileno(self):
                            return handle.fileno()

                        def close(self):
                            handle.close()
                            raise injected

                    with object.__getattribute__(owner, "_lock"):
                        object.__getattribute__(
                            owner,
                            "_handle_offer",
                        )[0] = CloseThenRaise()
                    expected = (
                        runtime_module._DatabaseCleanupFailure
                        if exception_type is RuntimeError
                        else exception_type
                    )
                    with self.assertRaises(expected) as caught:
                        owner.close()
                    if exception_type is not RuntimeError:
                        self.assertIs(caught.exception, injected)
                    self.assertTrue(owner.terminal)
                    unrelated = open(
                        state.configuration_path,
                        "rb",
                        buffering=0,
                    )
                    try:
                        self.assertEqual(
                            unrelated.fileno(),
                            descriptor,
                        )
                        self.assertTrue(owner.close())
                        self.assertTrue(unrelated.read(1))
                    finally:
                        unrelated.close()

    def test_b21_cleanup_interruptions_quarantine_then_retry_exact_owner(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        for phase in ("before_cleanup", "after_cleanup"):
            for exception_type in (
                RuntimeError,
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ):
                with self.subTest(
                    phase=phase,
                    exception_type=exception_type.__name__,
                ):
                    with temporary_browser_login_state() as state:
                        target = self._target(runtime_module, state)
                        manager = runtime_module._RuntimeDatabaseConnections(
                            target
                        )
                        lease = manager.open_writable_connection()
                        lease.execute("BEGIN IMMEDIATE")
                        injected = exception_type(
                            "PRIVATE_B21_CLEANUP_BOUNDARY"
                        )
                        original = (
                            runtime_module
                            ._cleanup_database_connection_independently
                        )
                        calls = 0

                        def interrupt(connection, *, rollback):
                            nonlocal calls
                            calls += 1
                            if calls == 1 and phase == "before_cleanup":
                                raise injected
                            result = original(
                                connection,
                                rollback=rollback,
                            )
                            if calls == 1:
                                raise injected
                            return result

                        with mock.patch.object(
                            runtime_module,
                            "_cleanup_database_connection_independently",
                            side_effect=interrupt,
                        ):
                            with self.assertRaises(
                                exception_type
                            ) as caught:
                                lease.close()
                            self.assertIs(caught.exception, injected)
                            self.assertFalse(manager.closed)
                            for _attempt in range(3):
                                try:
                                    if manager.close():
                                        break
                                except (
                                    runtime_module
                                    ._DatabaseCleanupFailure
                                ):
                                    pass
                        self.assertEqual(
                            calls,
                            2 if phase == "before_cleanup" else 1,
                        )
                        self.assertTrue(manager.closed)
                        with self.assertRaises(
                            DurableGoogleLoginConfigurationError
                        ):
                            manager.open_writable_connection()

    def test_b21_release_claim_and_terminal_commit_interruptions_are_retryable(
        self,
    ):
        import wahojobs.durable_google_login_runtime as runtime_module

        source = Path(runtime_module.__file__).read_text(
            encoding="utf-8"
        ).splitlines()
        code = (
            runtime_module._RuntimeDatabaseConnections
            ._release_connection_lease.__code__
        )
        targets = {
            "after_release_claim": next(
                index + 1
                for index in range(
                    code.co_firstlineno - 1,
                    code.co_firstlineno + 150,
                )
                if source[index].strip()
                == "terminal = record.cleanup_owned_resources()"
            ),
            "after_cleanup_before_commit": next(
                index + 1
                for index in range(
                    code.co_firstlineno - 1,
                    code.co_firstlineno + 150,
                )
                if source[index].strip() == "if record is not None:"
            ),
        }
        for boundary, target_line in targets.items():
            for exception_type in (
                RuntimeError,
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ):
                with self.subTest(
                    boundary=boundary,
                    exception_type=exception_type.__name__,
                ):
                    with temporary_browser_login_state() as state:
                        target = self._target(runtime_module, state)
                        manager = (
                            runtime_module._RuntimeDatabaseConnections(
                                target
                            )
                        )
                        lease = manager.open_writable_connection()
                        lease.execute("BEGIN IMMEDIATE")
                        injected = exception_type(
                            "PRIVATE_B21_RELEASE_STATE"
                        )
                        fired = False

                        def trace(frame, event, _argument):
                            nonlocal fired
                            if (
                                not fired
                                and event == "line"
                                and frame.f_code is code
                                and frame.f_lineno == target_line
                            ):
                                fired = True
                                sys.settrace(None)
                                raise injected
                            return trace

                        sys.settrace(trace)
                        try:
                            with self.assertRaises(
                                exception_type
                            ) as caught:
                                lease.close()
                            self.assertIs(caught.exception, injected)
                        finally:
                            sys.settrace(None)
                        self.assertTrue(fired)
                        self.assertTrue(lease.close())
                        self.assertTrue(manager.close())
                        self.assertTrue(manager.closed)

    def test_b21_manager_close_claim_interruption_is_reclaimable(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        source = Path(runtime_module.__file__).read_text(
            encoding="utf-8"
        ).splitlines()
        code = runtime_module._RuntimeDatabaseConnections.close.__code__
        target_line = tuple(
            index + 1
            for index in range(
                code.co_firstlineno - 1,
                code.co_firstlineno + 180,
            )
            if source[index].strip()
            == "record._cleanup_thread = caller"
        )[-1]
        for exception_type in (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(exception_type=exception_type.__name__):
                with temporary_browser_login_state() as state:
                    target = self._target(runtime_module, state)
                    manager = (
                        runtime_module._RuntimeDatabaseConnections(target)
                    )
                    lease = manager.open_writable_connection()
                    lease.execute("BEGIN IMMEDIATE")
                    original = (
                        runtime_module
                        ._cleanup_database_connection_independently
                    )
                    first = True

                    def fail_once(connection, *, rollback):
                        nonlocal first
                        if first:
                            first = False
                            raise RuntimeError(
                                "PRIVATE_B21_MAKE_UNRESOLVED"
                            )
                        return original(connection, rollback=rollback)

                    with mock.patch.object(
                        runtime_module,
                        "_cleanup_database_connection_independently",
                        side_effect=fail_once,
                    ):
                        with self.assertRaises(RuntimeError):
                            lease.close()
                    injected = exception_type("PRIVATE_B21_CLOSE_CLAIM")
                    fired = False

                    def trace(frame, event, _argument):
                        nonlocal fired
                        if (
                            not fired
                            and event == "line"
                            and frame.f_code is code
                            and frame.f_lineno == target_line
                        ):
                            fired = True
                            sys.settrace(None)
                            raise injected
                        return trace

                    sys.settrace(trace)
                    try:
                        expected = (
                            runtime_module._DatabaseCleanupFailure
                            if exception_type is RuntimeError
                            else exception_type
                        )
                        with self.assertRaises(expected) as caught:
                            manager.close()
                        if exception_type is not RuntimeError:
                            self.assertIs(caught.exception, injected)
                    finally:
                        sys.settrace(None)
                    self.assertTrue(fired)
                    self.assertTrue(manager.close())
                    self.assertTrue(manager.closed)
                    self.assertTrue(lease.close())

    def test_b21_read_only_adoption_interruptions_release_exact_lease(self):
        import wahojobs.durable_google_login_runtime as runtime_module

        for scope_kind in ("standalone", "managed"):
            for exception_type in (
                RuntimeError,
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ):
                with self.subTest(
                    scope=scope_kind,
                    exception_type=exception_type.__name__,
                ):
                    with temporary_browser_login_state() as state:
                        target = self._target(runtime_module, state)
                        manager = None
                        captured = []
                        injected = exception_type(
                            "PRIVATE_B21_READ_ONLY_ADOPTION"
                        )
                        original_borrow = (
                            runtime_module._DatabaseConnectionLease
                            ._borrow_internal_connection
                        )

                        def interrupt_borrow(lease, capability):
                            original_borrow(lease, capability)
                            captured.append(lease)
                            raise injected

                        if scope_kind == "standalone":
                            scope = (
                                runtime_module._read_only_connection_scope(
                                    target
                                )
                            )
                        else:
                            manager = (
                                runtime_module
                                ._RuntimeDatabaseConnections(target)
                            )
                            scope = (
                                runtime_module
                                ._managed_read_only_connection_scope(
                                    manager
                                )
                            )
                        with mock.patch.object(
                            runtime_module._DatabaseConnectionLease,
                            "_borrow_internal_connection",
                            new=interrupt_borrow,
                        ):
                            with self.assertRaises(
                                exception_type
                            ) as caught:
                                with scope:
                                    self.fail("scope_yielded")
                            self.assertIs(caught.exception, injected)
                        self.assertEqual(len(captured), 1)
                        self.assertTrue(captured[0].closed)
                        if manager is not None:
                            self.assertTrue(manager.close())
                            self.assertTrue(manager.closed)

    def test_b21_process_epoch_is_nonserializable_and_stale_identity_rejected(
        self,
    ):
        import wahojobs.durable_google_login_runtime as runtime_module

        current = runtime_module._current_database_process_epoch()
        stale = runtime_module._DatabaseProcessEpoch(os.getpid())
        self.assertIsNot(current, stale)
        with self.assertRaises(
            DurableGoogleLoginConfigurationError
        ):
            runtime_module._require_current_database_process(stale)
        with self.assertRaises(TypeError):
            pickle.dumps(current)

    def test_b21_response_publication_interruptions_retain_stable_owner(self):
        import wahojobs.durable_google_login_browser as browser_module
        import wahojobs.durable_google_login_runtime as runtime_module

        boundaries = (
            (
                browser_module
                ._BrowserRequestDeliveryOwner
                ._acquire_connection_owner,
                "return connection_owner",
                1,
                "connection_owner_published",
            ),
            (
                browser_module._BrowserRequestDeliveryOwner._borrow_connection,
                "return connection",
                1,
                "raw_connection_published",
            ),
            (
                browser_module._BrowserRequestDeliveryOwner._acquire_delivery,
                "raw_lease = offer[0]",
                1,
                "raw_delivery_published",
            ),
            (
                browser_module._BrowserRequestDeliveryOwner._offer_delivery,
                "return True",
                1,
                "offer_committed",
            ),
            (
                browser_module._BrowserRequestDeliveryOwner._bind_response,
                "return True",
                1,
                "response_adopted",
            ),
            (
                browser_module.DurableGoogleLoginBrowserIntegration._complete_login,
                "return payload",
                1,
                "response_return",
            ),
        )
        for function, needle, occurrence, boundary in boundaries:
            target_line = self._executed_source_line(
                function,
                needle,
                occurrence=occurrence,
            )
            for exception_type in (
                RuntimeError,
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ):
                with self.subTest(
                    boundary=boundary,
                    exception_type=exception_type.__name__,
                ):
                    with temporary_browser_login_state() as state:
                        runtime = build_durable_google_login_runtime(
                            state.configuration_path,
                            _clock=state.clock,
                            _gateway_factory=state.gateway_factory,
                        )
                        try:
                            browser, target, headers = (
                                self._prepared_callback_request(
                                    runtime,
                                    state,
                                )
                            )
                            manager = object.__getattribute__(
                                runtime,
                                "_connections",
                            )
                            close_calls = []
                            original_cleanup = (
                                runtime_module
                                ._cleanup_database_connection_independently
                            )
                            def count_cleanup(connection, *, rollback):
                                close_calls.append(connection)
                                return original_cleanup(
                                    connection,
                                    rollback=rollback,
                                )

                            injected = exception_type(
                                "PRIVATE_B21_RESPONSE_PUBLICATION"
                            )

                            def operation():
                                with loopback_and_in_memory_provider_only():
                                    return browser.handle(
                                        "GET",
                                        target,
                                        headers,
                                    )

                            with mock.patch.object(
                                runtime_module,
                                "_cleanup_database_connection_independently",
                                side_effect=count_cleanup,
                            ):
                                result, caught, captured, fired = (
                                    self._interrupt_executed_line(
                                        function,
                                        target_line,
                                        operation,
                                        injected,
                                    )
                                )
                            self.assertTrue(fired)
                            captured_owner = captured.get(
                                "request_owner",
                                captured.get(
                                    "request_release",
                                    captured.get("self"),
                                ),
                            )
                            self.assertIsInstance(
                                captured_owner,
                                browser_module
                                ._BrowserRequestDeliveryOwner,
                            )
                            if exception_type is RuntimeError:
                                self.assertIsNone(caught)
                                self.assertEqual(result.status, 503)
                            else:
                                self.assertIs(caught, injected)
                            self.assertTrue(captured_owner._is_terminal())
                            self.assertEqual(len(close_calls), 1)
                            captured_connection = captured.get("connection")
                            if captured_connection is not None:
                                self.assertIs(
                                    close_calls[0],
                                    captured_connection,
                                )
                            captured_lease = captured.get("lease")
                            if (
                                captured_lease is not None
                                and type(captured_lease)
                                is browser_module
                                ._AuthorityFencedSessionDeliveryLease
                            ):
                                raw_lease = object.__getattribute__(
                                    captured_lease,
                                    (
                                        "_AuthorityFencedSessionDeliveryLease"
                                        "__lease"
                                    ),
                                )
                                self.assertEqual(
                                    object.__getattribute__(
                                        raw_lease,
                                        "_status",
                                    ),
                                    "failed",
                                )
                            captured_response = captured.get(
                                "response",
                                captured.get("payload"),
                            )
                            if isinstance(
                                captured_response,
                                browser_module
                                .DurableGoogleLoginBrowserResponse,
                            ):
                                self.assertEqual(
                                    captured_response._delivery_state,
                                    ["complete"],
                                )
                                self.assertEqual(
                                    captured_response.headers,
                                    (),
                                )
                                self.assertIsNone(
                                    captured_response._delivery_lease
                                )
                                self.assertIsNone(
                                    captured_response._owned_connection
                                )
                                self.assertIsNone(
                                    captured_response._request_release
                                )
                            self.assertEqual(
                                object.__getattribute__(manager, "_records"),
                                {},
                            )
                            self.assertEqual(browser.active_request_count, 0)
                            self.assertEqual(
                                object.__getattribute__(
                                    browser,
                                    "_request_owners",
                                ),
                                {},
                            )
                            report = runtime.close()
                            runtime = None
                            self.assertTrue(report.cleanup_complete)
                        finally:
                            if runtime is not None:
                                runtime.close(_preserve_primary=True)

        from wahojobs.browser_session_lifecycle import (
            SessionDeliveryLease,
        )

        for exception_type in (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(
                boundary="raw_delivery_offer_before_adoption",
                exception_type=exception_type.__name__,
            ):
                with temporary_browser_login_state() as state:
                    runtime = build_durable_google_login_runtime(
                        state.configuration_path,
                        _clock=state.clock,
                        _gateway_factory=state.gateway_factory,
                    )
                    try:
                        browser, target, headers = (
                            self._prepared_callback_request(
                                runtime,
                                state,
                            )
                        )
                        manager = object.__getattribute__(
                            runtime,
                            "_connections",
                        )
                        close_calls = []
                        original_cleanup = (
                            runtime_module
                            ._cleanup_database_connection_independently
                        )
                        adoption_error = exception_type(
                            "PRIVATE_B21_DELIVERY_ADOPTION"
                        )
                        compensation_error = exception_type(
                            "PRIVATE_B21_DELIVERY_COMPENSATION"
                        )

                        def count_cleanup(connection, *, rollback):
                            close_calls.append(connection)
                            return original_cleanup(
                                connection,
                                rollback=rollback,
                            )

                        def operation():
                            with loopback_and_in_memory_provider_only():
                                return browser.handle(
                                    "GET",
                                    target,
                                    headers,
                                )

                        with mock.patch.object(
                            runtime_module,
                            "_cleanup_database_connection_independently",
                            side_effect=count_cleanup,
                        ):
                            with mock.patch.object(
                                browser_module,
                                "_adopt_session_delivery_lease",
                                side_effect=adoption_error,
                            ):
                                with mock.patch.object(
                                    SessionDeliveryLease,
                                    "fail_delivery",
                                    side_effect=compensation_error,
                                ):
                                    caught = None
                                    try:
                                        operation()
                                    except BaseException as exc:
                                        caught = exc
                                        exc = None
                                    self.assertIsNotNone(caught)
                                    entries = object.__getattribute__(
                                        browser,
                                        "_request_owners",
                                    )
                                    self.assertEqual(len(entries), 1)
                                    stable_owner = next(
                                        iter(entries.values())
                                    )[0]
                                    delivery_offer = (
                                        object.__getattribute__(
                                            stable_owner,
                                            (
                                                "_BrowserRequestDeliveryOwner"
                                                "__delivery_offer"
                                            ),
                                        )
                                    )
                                    self.assertEqual(
                                        len(delivery_offer),
                                        1,
                                    )
                                    raw_lease = delivery_offer[0]
                                    self.assertEqual(
                                        object.__getattribute__(
                                            raw_lease,
                                            "_status",
                                        ),
                                        "prepared",
                                    )
                                    self.assertFalse(
                                        stable_owner._is_terminal()
                                    )
                                    self.assertEqual(close_calls, [])
                                    self.assertEqual(
                                        len(
                                            object.__getattribute__(
                                                manager,
                                                "_records",
                                            )
                                        ),
                                        1,
                                    )
                            self.assertTrue(
                                stable_owner._request_abort()
                            )
                        self.assertEqual(
                            object.__getattribute__(raw_lease, "_status"),
                            "failed",
                        )
                        self.assertTrue(stable_owner._is_terminal())
                        self.assertEqual(len(close_calls), 1)
                        self.assertEqual(
                            object.__getattribute__(manager, "_records"),
                            {},
                        )
                        self.assertEqual(
                            object.__getattribute__(
                                browser,
                                "_request_owners",
                            ),
                            {},
                        )
                        report = runtime.close()
                        runtime = None
                        self.assertTrue(report.cleanup_complete)
                    finally:
                        if runtime is not None:
                            runtime.close(_preserve_primary=True)

    def test_b21_dead_request_thread_nonempty_delivery_owner_is_reclaimable(
        self,
    ):
        import wahojobs.durable_google_login_browser as browser_module
        import wahojobs.durable_google_login_runtime as runtime_module
        from wahojobs.browser_session_lifecycle import (
            SessionDeliveryLease,
        )

        for exception_type in (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(exception_type=exception_type.__name__):
                with temporary_browser_login_state() as state:
                    runtime = build_durable_google_login_runtime(
                        state.configuration_path,
                        _clock=state.clock,
                        _gateway_factory=state.gateway_factory,
                    )
                    worker = None
                    try:
                        browser = runtime.browser_integration
                        manager = object.__getattribute__(
                            runtime,
                            "_connections",
                        )
                        adoption_reached = threading.Event()
                        compensation_reached = threading.Event()
                        worker_finished = threading.Event()
                        worker_outcome = {}
                        adoption_error = exception_type(
                            "PRIVATE_B21_DEAD_REQUEST_ADOPTION"
                        )
                        compensation_error = exception_type(
                            "PRIVATE_B21_DEAD_REQUEST_COMPENSATION"
                        )

                        def interrupt_adoption(*_arguments, **_keywords):
                            adoption_reached.set()
                            raise adoption_error

                        def interrupt_compensation(
                            *_arguments,
                            **_keywords,
                        ):
                            compensation_reached.set()
                            raise compensation_error

                        def worker_operation():
                            try:
                                with mock.patch.object(
                                    browser_module,
                                    "_adopt_session_delivery_lease",
                                    side_effect=interrupt_adoption,
                                ):
                                    with mock.patch.object(
                                        SessionDeliveryLease,
                                        "fail_delivery",
                                        side_effect=interrupt_compensation,
                                    ):
                                        self._pending_delivery_response(
                                            runtime,
                                            state,
                                        )
                            except BaseException as exc:
                                worker_outcome["error"] = exc
                            finally:
                                worker_finished.set()

                        worker = threading.Thread(
                            target=worker_operation,
                            name=(
                                "b21-dead-request-owner-"
                                + exception_type.__name__
                            ),
                            daemon=False,
                        )
                        worker.start()
                        self.assertTrue(adoption_reached.wait(60))
                        self.assertTrue(compensation_reached.wait(60))
                        self.assertTrue(worker_finished.wait(60))
                        worker.join(10)
                        self.assertFalse(worker.is_alive())
                        self.assertIsNotNone(worker_outcome.get("error"))

                        entries = object.__getattribute__(
                            browser,
                            "_request_owners",
                        )
                        self.assertEqual(len(entries), 1)
                        stable_owner = next(iter(entries.values()))[0]
                        self.assertFalse(stable_owner._is_terminal())
                        self.assertIs(
                            object.__getattribute__(
                                stable_owner,
                                "_BrowserRequestDeliveryOwner__thread",
                            ),
                            worker,
                        )
                        self.assertEqual(browser.active_request_count, 1)
                        connection_offer = object.__getattribute__(
                            stable_owner,
                            (
                                "_BrowserRequestDeliveryOwner"
                                "__connection_offer"
                            ),
                        )
                        raw_connection_offer = object.__getattribute__(
                            stable_owner,
                            (
                                "_BrowserRequestDeliveryOwner"
                                "__raw_connection_offer"
                            ),
                        )
                        delivery_offer = object.__getattribute__(
                            stable_owner,
                            (
                                "_BrowserRequestDeliveryOwner"
                                "__delivery_offer"
                            ),
                        )
                        self.assertEqual(len(connection_offer), 1)
                        self.assertEqual(len(raw_connection_offer), 1)
                        self.assertEqual(len(delivery_offer), 1)
                        connection_owner = connection_offer[0]
                        connection = raw_connection_offer[0]
                        raw_lease = delivery_offer[0]
                        record = object.__getattribute__(
                            connection_owner,
                            "_record",
                        )
                        borrower_token = object.__getattribute__(
                            connection_owner,
                            "_token",
                        )
                        self.assertIs(
                            object.__getattribute__(
                                raw_lease,
                                "_connection",
                            ),
                            connection,
                        )
                        self.assertEqual(
                            object.__getattribute__(raw_lease, "_status"),
                            "prepared",
                        )
                        self.assertIs(
                            object.__getattribute__(
                                record,
                                "_borrower_token",
                            ),
                            borrower_token,
                        )
                        self.assertIs(
                            object.__getattribute__(
                                record,
                                "_browser_cleanup_delegate",
                            ),
                            stable_owner,
                        )
                        self.assertEqual(
                            len(
                                object.__getattribute__(
                                    manager,
                                    "_records",
                                )
                            ),
                            1,
                        )
                        self.assertEqual(
                            object.__getattribute__(record, "_state"),
                            "leased",
                        )

                        connection_close_calls = []
                        delivery_close_calls = []
                        request_release_transitions = []
                        original_cleanup = (
                            runtime_module
                            ._cleanup_database_connection_independently
                        )
                        original_fail_delivery = (
                            SessionDeliveryLease.fail_delivery
                        )
                        original_release = (
                            browser_module
                            .DurableGoogleLoginBrowserIntegration
                            ._release_request_owner
                        )

                        def count_cleanup(
                            candidate,
                            *,
                            rollback,
                        ):
                            connection_close_calls.append(candidate)
                            return original_cleanup(
                                candidate,
                                rollback=rollback,
                            )

                        def count_release(integration, owner):
                            condition = integration._lifecycle_condition
                            with condition:
                                entry = integration._request_owners.get(
                                    owner._issuance
                                )
                                pending = (
                                    entry is owner._registry_entry
                                    and entry[0] is owner
                                    and entry[1] is False
                                )
                            released = original_release(
                                integration,
                                owner,
                            )
                            if pending and released:
                                request_release_transitions.append(owner)
                            return released

                        def count_fail_delivery(candidate):
                            delivery_close_calls.append(candidate)
                            return original_fail_delivery(candidate)

                        with mock.patch.object(
                            runtime_module,
                            "_cleanup_database_connection_independently",
                            side_effect=count_cleanup,
                        ):
                            with mock.patch.object(
                                browser_module
                                .DurableGoogleLoginBrowserIntegration,
                                "_release_request_owner",
                                new=count_release,
                            ):
                                with mock.patch.object(
                                    SessionDeliveryLease,
                                    "fail_delivery",
                                    new=count_fail_delivery,
                                ):
                                    self.assertFalse(manager.close())
                                    self.assertEqual(
                                        object.__getattribute__(
                                            record,
                                            "_state",
                                        ),
                                        "close_pending",
                                    )
                                    self.assertEqual(
                                        connection_close_calls,
                                        [],
                                    )
                                    self.assertEqual(
                                        delivery_close_calls,
                                        [],
                                    )
                                    self.assertTrue(browser.close())

                        self.assertEqual(
                            object.__getattribute__(raw_lease, "_status"),
                            "failed",
                        )
                        self.assertIsNone(
                            object.__getattribute__(
                                raw_lease,
                                "_connection",
                            )
                        )
                        self.assertTrue(connection_owner.closed)
                        self.assertEqual(
                            connection_close_calls,
                            [connection],
                        )
                        self.assertEqual(
                            delivery_close_calls,
                            [raw_lease],
                        )
                        self.assertEqual(
                            request_release_transitions,
                            [stable_owner],
                        )
                        self.assertTrue(stable_owner._is_terminal())
                        self.assertEqual(browser.active_request_count, 0)
                        self.assertEqual(
                            object.__getattribute__(
                                browser,
                                "_request_owners",
                            ),
                            {},
                        )
                        self.assertEqual(
                            object.__getattribute__(manager, "_records"),
                            {},
                        )
                        self.assertEqual(
                            object.__getattribute__(record, "_state"),
                            "terminal",
                        )
                        for field_name in (
                            "_borrower_thread",
                            "_borrower_token",
                            "_release_thread",
                            "_release_token",
                            "_cleanup_thread",
                            "_cleanup_token",
                            "_browser_cleanup_claim",
                            "_connection_identity",
                        ):
                            self.assertIsNone(
                                object.__getattribute__(
                                    record,
                                    field_name,
                                )
                            )
                        self.assertTrue(
                            object.__getattribute__(
                                record,
                                "_descriptor_owner",
                            ).terminal
                        )
                        self.assertIsNone(
                            object.__getattribute__(
                                record,
                                "_browser_cleanup_delegate",
                            )
                        )
                        self.assertEqual(
                            object.__getattribute__(
                                record,
                                "_browser_cleanup_mode",
                            ),
                            "acknowledged",
                        )
                        for field_name in (
                            "_connection",
                            "_issued_session",
                            "_secret_vault",
                            "_trusted_now",
                            "_set_cookie_header",
                            "_csrf_credential",
                        ):
                            self.assertIsNone(
                                object.__getattribute__(
                                    raw_lease,
                                    field_name,
                                )
                            )
                        for field_name in (
                            "__connection_offer",
                            "__raw_connection_offer",
                            "__delivery_offer",
                        ):
                            self.assertEqual(
                                object.__getattribute__(
                                    stable_owner,
                                    (
                                        "_BrowserRequestDeliveryOwner"
                                        + field_name
                                    ),
                                ),
                                [],
                            )
                        for field_name in (
                            "__integration",
                            "__delivery_bundle",
                            "__response",
                            "__abandoned_cleanup_thread",
                            "__abandoned_cleanup_token",
                        ):
                            self.assertIsNone(
                                object.__getattribute__(
                                    stable_owner,
                                    (
                                        "_BrowserRequestDeliveryOwner"
                                        + field_name
                                    ),
                                )
                            )
                        report = runtime.close()
                        runtime = None
                        self.assertTrue(report.cleanup_complete)
                    finally:
                        if worker is not None and worker.is_alive():
                            worker.join(10)
                        if runtime is not None:
                            runtime.close(_preserve_primary=True)

    def test_b21_dead_request_cleanup_claim_interruptions_are_retryable(
        self,
    ):
        import wahojobs.durable_google_login_browser as browser_module
        import wahojobs.durable_google_login_runtime as runtime_module
        from wahojobs.browser_session_lifecycle import (
            SessionDeliveryLease,
        )

        owner_type = browser_module._BrowserRequestDeliveryOwner
        manager_type = runtime_module._RuntimeDatabaseConnections
        connection_lease_type = runtime_module._DatabaseConnectionLease
        boundaries = (
            (
                owner_type._reclaim_abandoned_request_attempt,
                "self.__abort_requested = True",
                1,
                "browser_attempt_claim",
            ),
            (
                manager_type._claim_abandoned_browser_cleanup,
                "return claim",
                1,
                "database_cleanup_claim",
            ),
            (
                owner_type._reclaim_abandoned_request_attempt,
                "delivery_complete = (",
                1,
                "delivery_compensation_acknowledgement",
            ),
            (
                manager_type._cleanup_claimed_record,
                "if failure is not None:",
                1,
                "database_terminal_commit",
            ),
            (
                connection_lease_type
                ._wahojobs_acknowledge_abandoned_browser_cleanup,
                "if acknowledged:",
                1,
                "manager_acknowledgement",
            ),
            (
                connection_lease_type
                ._wahojobs_acknowledge_abandoned_browser_cleanup,
                "return acknowledged",
                1,
                "lease_retirement",
            ),
            (
                browser_module
                .DurableGoogleLoginBrowserIntegration
                ._release_request_owner,
                "condition.notify_all()",
                1,
                "request_release_acknowledgement",
            ),
            (
                owner_type._reclaim_abandoned_request_attempt,
                "terminal = self._is_terminal()",
                1,
                "owner_terminal_commit",
            ),
            (
                manager_type._abandon_abandoned_browser_cleanup,
                "record._cleanup_thread = None",
                1,
                "manager_claim_abandonment",
            ),
            (
                owner_type._reclaim_abandoned_request,
                "self.__abandoned_cleanup_thread = None",
                1,
                "browser_attempt_release",
            ),
        )

        for function, needle, occurrence, boundary in boundaries:
            target_line = self._executed_source_line(
                function,
                needle,
                occurrence=occurrence,
            )
            for exception_type in (
                RuntimeError,
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ):
                with self.subTest(
                    boundary=boundary,
                    exception_type=exception_type.__name__,
                ):
                    with temporary_browser_login_state() as state:
                        runtime = build_durable_google_login_runtime(
                            state.configuration_path,
                            _clock=state.clock,
                            _gateway_factory=state.gateway_factory,
                        )
                        producer = None
                        coordinator = None
                        keep_coordinator_alive = threading.Event()
                        try:
                            browser = runtime.browser_integration
                            manager = object.__getattribute__(
                                runtime,
                                "_connections",
                            )
                            adoption_reached = threading.Event()
                            compensation_reached = threading.Event()
                            producer_finished = threading.Event()
                            producer_outcome = {}

                            def interrupt_adoption(
                                *_arguments,
                                **_keywords,
                            ):
                                adoption_reached.set()
                                raise RuntimeError(
                                    "PRIVATE_B21_DEAD_REQUEST_ADOPTION"
                                )

                            def interrupt_compensation(
                                *_arguments,
                                **_keywords,
                            ):
                                compensation_reached.set()
                                raise RuntimeError(
                                    "PRIVATE_B21_DEAD_REQUEST_COMPENSATION"
                                )

                            def produce_dead_owner():
                                try:
                                    with mock.patch.object(
                                        browser_module,
                                        "_adopt_session_delivery_lease",
                                        side_effect=interrupt_adoption,
                                    ):
                                        with mock.patch.object(
                                            SessionDeliveryLease,
                                            "fail_delivery",
                                            side_effect=(
                                                interrupt_compensation
                                            ),
                                        ):
                                            self._pending_delivery_response(
                                                runtime,
                                                state,
                                            )
                                except BaseException as exc:
                                    producer_outcome["error"] = exc
                                finally:
                                    producer_finished.set()

                            producer = threading.Thread(
                                target=produce_dead_owner,
                                name=(
                                    "b21-dead-owner-producer-"
                                    + boundary
                                    + "-"
                                    + exception_type.__name__
                                ),
                                daemon=False,
                            )
                            producer.start()
                            self.assertTrue(adoption_reached.wait(60))
                            self.assertTrue(compensation_reached.wait(60))
                            self.assertTrue(producer_finished.wait(60))
                            producer.join(10)
                            self.assertFalse(producer.is_alive())
                            self.assertIsNotNone(
                                producer_outcome.get("error")
                            )

                            entries = object.__getattribute__(
                                browser,
                                "_request_owners",
                            )
                            self.assertEqual(len(entries), 1)
                            stable_owner = next(iter(entries.values()))[0]
                            self.assertIs(
                                object.__getattribute__(
                                    stable_owner,
                                    (
                                        "_BrowserRequestDeliveryOwner"
                                        "__thread"
                                    ),
                                ),
                                producer,
                            )
                            connection_owner = object.__getattribute__(
                                stable_owner,
                                (
                                    "_BrowserRequestDeliveryOwner"
                                    "__connection_offer"
                                ),
                            )[0]
                            connection = object.__getattribute__(
                                stable_owner,
                                (
                                    "_BrowserRequestDeliveryOwner"
                                    "__raw_connection_offer"
                                ),
                            )[0]
                            raw_lease = object.__getattribute__(
                                stable_owner,
                                (
                                    "_BrowserRequestDeliveryOwner"
                                    "__delivery_offer"
                                ),
                            )[0]
                            record = object.__getattribute__(
                                connection_owner,
                                "_record",
                            )
                            borrower_token = object.__getattribute__(
                                connection_owner,
                                "_token",
                            )
                            self.assertEqual(
                                object.__getattribute__(record, "_state"),
                                "leased",
                            )
                            self.assertIs(
                                object.__getattribute__(
                                    record,
                                    "_borrower_token",
                                ),
                                borrower_token,
                            )

                            delivery_calls = []
                            connection_calls = []
                            release_transitions = []
                            original_delivery = (
                                SessionDeliveryLease.fail_delivery
                            )
                            original_cleanup = (
                                runtime_module
                                ._cleanup_database_connection_independently
                            )
                            original_release = (
                                browser_module
                                .DurableGoogleLoginBrowserIntegration
                                ._release_request_owner
                            )
                            force_cleanup_release = boundary in {
                                "manager_claim_abandonment",
                                "browser_attempt_release",
                            }
                            action_error = RuntimeError(
                                "PRIVATE_B21_CLEANUP_ACTION"
                            )

                            def count_delivery(candidate):
                                delivery_calls.append(candidate)
                                if (
                                    force_cleanup_release
                                    and len(delivery_calls) == 1
                                ):
                                    raise action_error
                                return original_delivery(candidate)

                            def count_cleanup(candidate, *, rollback):
                                connection_calls.append(candidate)
                                return original_cleanup(
                                    candidate,
                                    rollback=rollback,
                                )

                            def count_release(integration, owner):
                                condition = integration._lifecycle_condition
                                with condition:
                                    entry = integration._request_owners.get(
                                        owner._issuance
                                    )
                                    pending = (
                                        entry is owner._registry_entry
                                        and entry[0] is owner
                                        and entry[1] is False
                                    )
                                released = False
                                try:
                                    released = original_release(
                                        integration,
                                        owner,
                                    )
                                    return released
                                finally:
                                    with condition:
                                        entry = (
                                            integration
                                            ._request_owners
                                            .get(owner._issuance)
                                        )
                                        acknowledged = (
                                            entry is owner._registry_entry
                                            and entry[0] is owner
                                            and entry[1] is True
                                        )
                                    if (
                                        pending
                                        and acknowledged
                                        and owner
                                        not in release_transitions
                                    ):
                                        release_transitions.append(owner)

                            injected = exception_type(
                                "PRIVATE_B21_DEAD_CLEANUP_ATTEMPT"
                            )
                            attempt_finished = threading.Event()
                            attempt_outcome = {}

                            def interrupted_cleanup_attempt():
                                try:
                                    (
                                        result,
                                        caught,
                                        captured,
                                        fired,
                                    ) = self._interrupt_executed_line(
                                        function,
                                        target_line,
                                        browser.close,
                                        injected,
                                    )
                                    attempt_outcome.update(
                                        result=result,
                                        caught=caught,
                                        captured=captured,
                                        fired=fired,
                                    )
                                finally:
                                    attempt_finished.set()
                                    keep_coordinator_alive.wait(60)

                            with mock.patch.object(
                                SessionDeliveryLease,
                                "fail_delivery",
                                new=count_delivery,
                            ):
                                with mock.patch.object(
                                    runtime_module,
                                    (
                                        "_cleanup_database_connection_"
                                        "independently"
                                    ),
                                    side_effect=count_cleanup,
                                ):
                                    with mock.patch.object(
                                        browser_module
                                        .DurableGoogleLoginBrowserIntegration,
                                        "_release_request_owner",
                                        new=count_release,
                                    ):
                                        coordinator = threading.Thread(
                                            target=(
                                                interrupted_cleanup_attempt
                                            ),
                                            name=(
                                                "b21-dead-owner-cleanup-"
                                                + boundary
                                                + "-"
                                                + exception_type.__name__
                                            ),
                                            daemon=False,
                                        )
                                        coordinator.start()
                                        self.assertTrue(
                                            attempt_finished.wait(60)
                                        )
                                        self.assertTrue(
                                            coordinator.is_alive()
                                        )
                                        self.assertTrue(
                                            attempt_outcome.get("fired")
                                        )
                                        expected_error = (
                                            action_error
                                            if boundary
                                            == "manager_claim_abandonment"
                                            else injected
                                        )
                                        self.assertIs(
                                            attempt_outcome.get("caught"),
                                            expected_error,
                                        )
                                        self.assertIsNone(
                                            expected_error.__cause__
                                        )
                                        self.assertIsNone(
                                            expected_error.__context__
                                        )

                                        self.assertIsNone(
                                            object.__getattribute__(
                                                stable_owner,
                                                (
                                                    "_BrowserRequestDeliveryOwner"
                                                    "__abandoned_cleanup_thread"
                                                ),
                                            )
                                        )
                                        self.assertIsNone(
                                            object.__getattribute__(
                                                stable_owner,
                                                (
                                                    "_BrowserRequestDeliveryOwner"
                                                    "__abandoned_cleanup_token"
                                                ),
                                            )
                                        )
                                        if (
                                            object.__getattribute__(
                                                record,
                                                "_state",
                                            )
                                            != "terminal"
                                        ):
                                            self.assertIsNone(
                                                object.__getattribute__(
                                                    record,
                                                    "_cleanup_thread",
                                                )
                                            )
                                            self.assertIsNone(
                                                object.__getattribute__(
                                                    record,
                                                    "_cleanup_token",
                                                )
                                            )
                                            self.assertIsNone(
                                                object.__getattribute__(
                                                    record,
                                                    "_browser_cleanup_claim",
                                                )
                                            )
                                            self.assertIs(
                                                object.__getattribute__(
                                                    record,
                                                    (
                                                        "_browser_cleanup_"
                                                        "delegate"
                                                    ),
                                                ),
                                                stable_owner,
                                            )
                                        self.assertTrue(browser.close())

                            keep_coordinator_alive.set()
                            coordinator.join(10)
                            self.assertFalse(coordinator.is_alive())
                            self.assertEqual(
                                delivery_calls,
                                (
                                    [raw_lease, raw_lease]
                                    if force_cleanup_release
                                    else [raw_lease]
                                ),
                            )
                            self.assertEqual(
                                connection_calls,
                                [connection],
                            )
                            self.assertEqual(
                                release_transitions,
                                [stable_owner],
                            )
                            self.assertEqual(
                                object.__getattribute__(
                                    raw_lease,
                                    "_status",
                                ),
                                "failed",
                            )
                            for field_name in (
                                "_connection",
                                "_issued_session",
                                "_secret_vault",
                                "_trusted_now",
                                "_set_cookie_header",
                                "_csrf_credential",
                            ):
                                self.assertIsNone(
                                    object.__getattribute__(
                                        raw_lease,
                                        field_name,
                                    )
                                )
                            self.assertTrue(connection_owner.closed)
                            self.assertTrue(
                                object.__getattribute__(
                                    record,
                                    "_descriptor_owner",
                                ).terminal
                            )
                            self.assertEqual(
                                object.__getattribute__(
                                    manager,
                                    "_records",
                                ),
                                {},
                            )
                            self.assertEqual(
                                object.__getattribute__(record, "_state"),
                                "terminal",
                            )
                            self.assertEqual(
                                object.__getattribute__(
                                    record,
                                    "_browser_cleanup_mode",
                                ),
                                "acknowledged",
                            )
                            self.assertEqual(
                                object.__getattribute__(
                                    record,
                                    "_connection_offer",
                                ),
                                [],
                            )
                            for field_name in (
                                "_borrower_thread",
                                "_borrower_token",
                                "_release_thread",
                                "_release_token",
                                "_cleanup_thread",
                                "_cleanup_token",
                                "_browser_cleanup_claim",
                                "_browser_cleanup_delegate",
                                "_connection_identity",
                            ):
                                self.assertIsNone(
                                    object.__getattribute__(
                                        record,
                                        field_name,
                                    )
                                )
                            self.assertTrue(stable_owner._is_terminal())
                            for field_name in (
                                "__integration",
                                "__process_guard",
                                "__response",
                                "__delivery_bundle",
                                "__abandoned_cleanup_thread",
                                "__abandoned_cleanup_token",
                            ):
                                self.assertIsNone(
                                    object.__getattribute__(
                                        stable_owner,
                                        (
                                            "_BrowserRequestDeliveryOwner"
                                            + field_name
                                        ),
                                    )
                                )
                            for field_name in (
                                "__connection_offer",
                                "__raw_connection_offer",
                                "__delivery_offer",
                            ):
                                self.assertEqual(
                                    object.__getattribute__(
                                        stable_owner,
                                        (
                                            "_BrowserRequestDeliveryOwner"
                                            + field_name
                                        ),
                                    ),
                                    [],
                                )
                            self.assertEqual(browser.active_request_count, 0)
                            self.assertEqual(
                                object.__getattribute__(
                                    browser,
                                    "_request_owners",
                                ),
                                {},
                            )
                            report = runtime.close()
                            runtime = None
                            self.assertTrue(report.cleanup_complete)
                        finally:
                            keep_coordinator_alive.set()
                            if coordinator is not None:
                                coordinator.join(10)
                            if producer is not None and producer.is_alive():
                                producer.join(10)
                            if runtime is not None:
                                runtime.close(_preserve_primary=True)

    def test_b21_dead_request_stale_process_rejection_precedes_owner_lock(
        self,
    ):
        import wahojobs.durable_google_login_runtime as runtime_module

        scenarios = (
            (
                "stale_owner_pid",
                None,
                None,
            ),
            (
                "stale_process_epoch",
                DurableGoogleLoginConfigurationError,
                "Durable Google login configuration is unavailable.",
            ),
        )
        for scenario, expected_error, expected_message in scenarios:
            with self.subTest(scenario=scenario):
                with temporary_browser_login_state() as state:
                    runtime = build_durable_google_login_runtime(
                        state.configuration_path,
                        _clock=state.clock,
                        _gateway_factory=state.gateway_factory,
                    )
                    response = None
                    stable_owner = None
                    original_pid = None
                    original_epoch = None
                    producer = None
                    lock_holder = None
                    reclaim_probe = None
                    release_owner_lock = threading.Event()
                    try:
                        browser = runtime.browser_integration
                        manager = object.__getattribute__(
                            runtime,
                            "_connections",
                        )
                        producer_finished = threading.Event()
                        producer_outcome = {}

                        def produce_pending_response():
                            try:
                                producer_outcome["response"] = (
                                    self._pending_delivery_response(
                                        runtime,
                                        state,
                                    )
                                )
                            except BaseException as exc:
                                producer_outcome["error"] = exc
                            finally:
                                producer_finished.set()

                        producer = threading.Thread(
                            target=produce_pending_response,
                            name=(
                                "b21-stale-process-owner-producer-"
                                + scenario
                            ),
                            daemon=False,
                        )
                        producer.start()
                        self.assertTrue(producer_finished.wait(60))
                        producer.join(10)
                        self.assertFalse(producer.is_alive())
                        self.assertNotIn("error", producer_outcome)
                        response = producer_outcome["response"]
                        stable_owner = response._delivery_owner
                        self.assertIsNotNone(stable_owner)
                        self.assertIs(
                            object.__getattribute__(
                                stable_owner,
                                "_BrowserRequestDeliveryOwner__thread",
                            ),
                            producer,
                        )
                        owner_lock = object.__getattribute__(
                            stable_owner,
                            "_BrowserRequestDeliveryOwner__lock",
                        )
                        self.assertIs(response._delivery_lock, owner_lock)
                        record_entries = object.__getattribute__(
                            manager,
                            "_records",
                        )
                        self.assertEqual(len(record_entries), 1)
                        record = next(iter(record_entries.values()))
                        record_state = object.__getattribute__(
                            record,
                            "_state",
                        )
                        borrower_token = object.__getattribute__(
                            record,
                            "_borrower_token",
                        )
                        delivery_offer = object.__getattribute__(
                            stable_owner,
                            (
                                "_BrowserRequestDeliveryOwner"
                                "__delivery_offer"
                            ),
                        )
                        self.assertEqual(len(delivery_offer), 1)
                        raw_lease = delivery_offer[0]
                        delivery_status = object.__getattribute__(
                            raw_lease,
                            "_status",
                        )
                        registry_entry = stable_owner._registry_entry
                        raw_lock = object.__getattribute__(
                            raw_lease,
                            "_lock",
                        )
                        manager_condition = object.__getattribute__(
                            manager,
                            "_condition",
                        )
                        browser_condition = object.__getattribute__(
                            browser,
                            "_lifecycle_condition",
                        )
                        protected_locks = (
                            owner_lock,
                            raw_lock,
                            manager_condition,
                            browser_condition,
                        )
                        self.assertEqual(
                            len({id(lock) for lock in protected_locks}),
                            len(protected_locks),
                        )
                        protected_locks_held = threading.Event()

                        def hold_protected_locks():
                            acquired_locks = []
                            try:
                                for lock in protected_locks:
                                    lock.acquire()
                                    acquired_locks.append(lock)
                                protected_locks_held.set()
                                release_owner_lock.wait(60)
                            finally:
                                for lock in reversed(acquired_locks):
                                    lock.release()

                        lock_holder = threading.Thread(
                            target=hold_protected_locks,
                            name=(
                                "b21-stale-process-owner-lock-"
                                + scenario
                            ),
                            daemon=False,
                        )
                        lock_holder.start()
                        self.assertTrue(protected_locks_held.wait(60))
                        self.assertTrue(owner_lock.locked())
                        self.assertTrue(raw_lock.locked())
                        self.assertTrue(
                            object.__getattribute__(
                                manager_condition,
                                "_lock",
                            ).locked()
                        )
                        self.assertTrue(
                            object.__getattribute__(
                                browser_condition,
                                "_lock",
                            ).locked()
                        )

                        if scenario == "stale_owner_pid":
                            original_pid = object.__getattribute__(
                                stable_owner,
                                "_BrowserRequestDeliveryOwner__pid",
                            )
                            object.__setattr__(
                                stable_owner,
                                "_BrowserRequestDeliveryOwner__pid",
                                original_pid + 1,
                            )
                        else:
                            original_epoch = (
                                runtime_module._DATABASE_PROCESS_EPOCH
                            )
                            runtime_module._DATABASE_PROCESS_EPOCH = (
                                runtime_module._DatabaseProcessEpoch(
                                    os.getpid()
                                )
                            )

                        reclaim_finished = threading.Event()
                        reclaim_outcome = {}

                        def reclaim_from_stale_process():
                            try:
                                reclaim_outcome["result"] = (
                                    stable_owner
                                    ._reclaim_abandoned_request()
                                )
                            except BaseException as exc:
                                reclaim_outcome["error"] = exc
                            finally:
                                reclaim_finished.set()

                        reclaim_probe = threading.Thread(
                            target=reclaim_from_stale_process,
                            name=(
                                "b21-stale-process-reclaim-probe-"
                                + scenario
                            ),
                            daemon=False,
                        )
                        reclaim_probe.start()
                        self.assertTrue(reclaim_finished.wait(10))
                        self.assertTrue(lock_holder.is_alive())
                        self.assertTrue(owner_lock.locked())
                        self.assertTrue(raw_lock.locked())
                        self.assertTrue(
                            object.__getattribute__(
                                manager_condition,
                                "_lock",
                            ).locked()
                        )
                        self.assertTrue(
                            object.__getattribute__(
                                browser_condition,
                                "_lock",
                            ).locked()
                        )
                        reclaim_probe.join(10)
                        self.assertFalse(reclaim_probe.is_alive())
                        if expected_error is None:
                            self.assertNotIn("error", reclaim_outcome)
                            self.assertIs(
                                reclaim_outcome.get("result"),
                                False,
                            )
                        else:
                            self.assertNotIn("result", reclaim_outcome)
                            error = reclaim_outcome.get("error")
                            self.assertIs(type(error), expected_error)
                            self.assertEqual(
                                str(error),
                                expected_message,
                            )

                        self.assertFalse(
                            object.__getattribute__(
                                stable_owner,
                                "_BrowserRequestDeliveryOwner__terminal",
                            )
                        )
                        self.assertIs(
                            response._delivery_owner,
                            stable_owner,
                        )
                        self.assertEqual(
                            response._delivery_state,
                            ["pending"],
                        )
                        self.assertIs(
                            browser._request_owners[
                                stable_owner._issuance
                            ],
                            registry_entry,
                        )
                        self.assertFalse(registry_entry[1])
                        self.assertIs(
                            object.__getattribute__(
                                manager,
                                "_records",
                            ).get(record._issuance),
                            record,
                        )
                        self.assertEqual(
                            object.__getattribute__(record, "_state"),
                            record_state,
                        )
                        self.assertIs(
                            object.__getattribute__(
                                record,
                                "_borrower_token",
                            ),
                            borrower_token,
                        )
                        self.assertEqual(
                            object.__getattribute__(
                                raw_lease,
                                "_status",
                            ),
                            delivery_status,
                        )

                        if original_pid is not None:
                            object.__setattr__(
                                stable_owner,
                                (
                                    "_BrowserRequestDeliveryOwner"
                                    "__pid"
                                ),
                                original_pid,
                            )
                            original_pid = None
                        if original_epoch is not None:
                            runtime_module._DATABASE_PROCESS_EPOCH = (
                                original_epoch
                            )
                            original_epoch = None
                        release_owner_lock.set()
                        lock_holder.join(10)
                        self.assertFalse(lock_holder.is_alive())

                        self.assertFalse(stable_owner._is_terminal())
                        self.assertEqual(
                            browser.active_request_count,
                            1,
                        )
                        self.assertTrue(browser.close())
                        self.assertTrue(stable_owner._is_terminal())
                        self.assertIsNone(response._delivery_owner)
                        self.assertEqual(
                            response._delivery_state,
                            ["complete"],
                        )
                        self.assertEqual(
                            browser.active_request_count,
                            0,
                        )
                        self.assertEqual(
                            object.__getattribute__(
                                manager,
                                "_records",
                            ),
                            {},
                        )
                        report = runtime.close()
                        runtime = None
                        self.assertTrue(report.cleanup_complete)
                    finally:
                        if (
                            stable_owner is not None
                            and original_pid is not None
                        ):
                            object.__setattr__(
                                stable_owner,
                                (
                                    "_BrowserRequestDeliveryOwner"
                                    "__pid"
                                ),
                                original_pid,
                            )
                        if original_epoch is not None:
                            runtime_module._DATABASE_PROCESS_EPOCH = (
                                original_epoch
                            )
                        release_owner_lock.set()
                        for thread in (
                            reclaim_probe,
                            lock_holder,
                            producer,
                        ):
                            if (
                                thread is not None
                                and thread.is_alive()
                            ):
                                thread.join(10)
                        if runtime is not None:
                            runtime.close(_preserve_primary=True)

    def test_b21_dead_request_unregistered_connection_handoff_is_exact(
        self,
    ):
        import wahojobs.durable_google_login_browser as browser_module
        import wahojobs.durable_google_login_runtime as runtime_module

        for exception_type in (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(exception_type=exception_type.__name__):
                with temporary_browser_login_state() as state:
                    runtime = build_durable_google_login_runtime(
                        state.configuration_path,
                        _clock=state.clock,
                        _gateway_factory=state.gateway_factory,
                    )
                    worker = None
                    try:
                        browser = runtime.browser_integration
                        manager = object.__getattribute__(
                            runtime,
                            "_connections",
                        )
                        owner_offer = []
                        finished = threading.Event()
                        outcome = {}
                        injected = exception_type(
                            "PRIVATE_B21_UNREGISTERED_BROWSER_HANDOFF"
                        )

                        def interrupt_registration(
                            _lease,
                            *_arguments,
                            **_keywords,
                        ):
                            raise injected

                        def worker_operation():
                            owner = (
                                browser_module
                                ._BrowserRequestDeliveryOwner(browser)
                            )
                            owner_offer.append(owner)
                            try:
                                self.assertTrue(
                                    browser._register_request_owner(owner)
                                )
                                with mock.patch.object(
                                    runtime_module
                                    ._DatabaseConnectionLease,
                                    (
                                        "_wahojobs_register_browser_"
                                        "cleanup"
                                    ),
                                    new=interrupt_registration,
                                ):
                                    owner._acquire_connection_owner(
                                        manager.open_writable_connection
                                    )
                            except BaseException as exc:
                                outcome["error"] = exc
                            finally:
                                finished.set()

                        worker = threading.Thread(
                            target=worker_operation,
                            name=(
                                "b21-unregistered-browser-owner-"
                                + exception_type.__name__
                            ),
                            daemon=False,
                        )
                        worker.start()
                        self.assertTrue(finished.wait(60))
                        worker.join(10)
                        self.assertFalse(worker.is_alive())
                        self.assertIs(outcome.get("error"), injected)
                        self.assertEqual(len(owner_offer), 1)
                        stable_owner = owner_offer[0]
                        connection_offer = object.__getattribute__(
                            stable_owner,
                            (
                                "_BrowserRequestDeliveryOwner"
                                "__connection_offer"
                            ),
                        )
                        self.assertEqual(len(connection_offer), 1)
                        connection_owner = connection_offer[0]
                        record = object.__getattribute__(
                            connection_owner,
                            "_record",
                        )
                        connection = object.__getattribute__(
                            record,
                            "_connection_identity",
                        )
                        self.assertIsNotNone(connection)
                        self.assertIsNone(
                            object.__getattribute__(
                                record,
                                "_browser_cleanup_mode",
                            )
                        )
                        self.assertEqual(
                            object.__getattribute__(record, "_state"),
                            "leased",
                        )
                        close_calls = []
                        original_cleanup = (
                            runtime_module
                            ._cleanup_database_connection_independently
                        )

                        def count_cleanup(candidate, *, rollback):
                            close_calls.append(candidate)
                            return original_cleanup(
                                candidate,
                                rollback=rollback,
                            )

                        with mock.patch.object(
                            runtime_module,
                            "_cleanup_database_connection_independently",
                            side_effect=count_cleanup,
                        ):
                            self.assertTrue(browser.close())
                            self.assertTrue(connection_owner.closed)
                            self.assertEqual(
                                object.__getattribute__(
                                    record,
                                    "_browser_cleanup_mode",
                                ),
                                "manager",
                            )
                            self.assertIsNone(
                                object.__getattribute__(
                                    record,
                                    "_borrower_thread",
                                )
                            )
                            self.assertIsNone(
                                object.__getattribute__(
                                    record,
                                    "_borrower_token",
                                )
                            )
                            self.assertEqual(
                                object.__getattribute__(record, "_state"),
                                "unresolved",
                            )
                            self.assertEqual(close_calls, [])
                            report = runtime.close()
                            runtime = None
                        self.assertTrue(report.cleanup_complete)
                        self.assertEqual(close_calls, [connection])
                        self.assertEqual(
                            object.__getattribute__(manager, "_records"),
                            {},
                        )
                        self.assertEqual(
                            object.__getattribute__(record, "_state"),
                            "terminal",
                        )
                        self.assertTrue(
                            object.__getattribute__(
                                record,
                                "_descriptor_owner",
                            ).terminal
                        )
                        for field_name in (
                            "_connection_identity",
                            "_browser_cleanup_borrower_token",
                            "_browser_cleanup_capability",
                            "_browser_cleanup_claim",
                            "_browser_cleanup_delegate",
                            "_browser_cleanup_identity",
                            "_browser_cleanup_mode",
                        ):
                            self.assertIsNone(
                                object.__getattribute__(
                                    record,
                                    field_name,
                                )
                            )
                        self.assertTrue(stable_owner._is_terminal())
                        self.assertEqual(browser.active_request_count, 0)
                        self.assertEqual(
                            object.__getattribute__(
                                browser,
                                "_request_owners",
                            ),
                            {},
                        )
                    finally:
                        if worker is not None and worker.is_alive():
                            worker.join(10)
                        if runtime is not None:
                            runtime.close(_preserve_primary=True)

    def test_b21_committed_manager_handoff_retires_after_interruption(
        self,
    ):
        import wahojobs.durable_google_login_browser as browser_module
        import wahojobs.durable_google_login_runtime as runtime_module

        lease_type = runtime_module._DatabaseConnectionLease
        manager_type = runtime_module._RuntimeDatabaseConnections
        boundaries = (
            (
                manager_type
                ._relinquish_unregistered_browser_cleanup,
                "record._borrower_thread = None",
                1,
                "manager_borrower_thread_clear",
            ),
            (
                manager_type
                ._relinquish_unregistered_browser_cleanup,
                "record._borrower_token = None",
                1,
                "manager_borrower_token_clear",
            ),
            (
                manager_type
                ._relinquish_unregistered_browser_cleanup,
                "if record._state in {",
                1,
                "manager_state_selection",
            ),
            (
                manager_type
                ._relinquish_unregistered_browser_cleanup,
                "record._state = (",
                1,
                "manager_state_publication",
            ),
            (
                manager_type
                ._relinquish_unregistered_browser_cleanup,
                "self._condition.notify_all()",
                1,
                "manager_handoff_notification",
            ),
            (
                lease_type
                ._wahojobs_relinquish_unregistered_browser_cleanup,
                "if relinquished:",
                1,
                "post_manager_commit",
            ),
            (
                lease_type
                ._wahojobs_relinquish_unregistered_browser_cleanup,
                "self._retire()",
                2,
                "pre_retirement",
            ),
            (
                lease_type._retire,
                "self._released = True",
                1,
                "retirement_publication",
            ),
            (
                lease_type._retire,
                "self._record = None",
                1,
                "record_retirement",
            ),
            (
                lease_type._retire,
                "self._token = None",
                1,
                "borrower_token_retirement",
            ),
            (
                lease_type._retire,
                "self._borrower_thread = None",
                1,
                "borrower_thread_retirement",
            ),
        )

        for function, needle, occurrence, boundary in boundaries:
            target_line = self._executed_source_line(
                function,
                needle,
                occurrence=occurrence,
            )
            for exception_type in (
                RuntimeError,
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ):
                with self.subTest(
                    boundary=boundary,
                    exception_type=exception_type.__name__,
                ):
                    with temporary_browser_login_state() as state:
                        runtime = build_durable_google_login_runtime(
                            state.configuration_path,
                            _clock=state.clock,
                            _gateway_factory=state.gateway_factory,
                        )
                        worker = None
                        try:
                            browser = runtime.browser_integration
                            manager = object.__getattribute__(
                                runtime,
                                "_connections",
                            )
                            owner_offer = []
                            worker_finished = threading.Event()
                            worker_outcome = {}
                            registration_error = RuntimeError(
                                "PRIVATE_B21_MANAGER_HANDOFF_REGISTRATION"
                            )

                            def interrupt_registration(
                                _lease,
                                *_arguments,
                                **_keywords,
                            ):
                                raise registration_error

                            def create_unregistered_owner():
                                owner = (
                                    browser_module
                                    ._BrowserRequestDeliveryOwner(browser)
                                )
                                owner_offer.append(owner)
                                try:
                                    self.assertTrue(
                                        browser._register_request_owner(
                                            owner
                                        )
                                    )
                                    with mock.patch.object(
                                        lease_type,
                                        (
                                            "_wahojobs_register_browser_"
                                            "cleanup"
                                        ),
                                        new=interrupt_registration,
                                    ):
                                        owner._acquire_connection_owner(
                                            manager.open_writable_connection
                                        )
                                except BaseException as exc:
                                    worker_outcome["error"] = exc
                                finally:
                                    worker_finished.set()

                            worker = threading.Thread(
                                target=create_unregistered_owner,
                                name=(
                                    "b21-manager-handoff-owner-"
                                    + boundary
                                    + "-"
                                    + exception_type.__name__
                                ),
                                daemon=False,
                            )
                            worker.start()
                            self.assertTrue(worker_finished.wait(60))
                            worker.join(10)
                            self.assertFalse(worker.is_alive())
                            self.assertIs(
                                worker_outcome.get("error"),
                                registration_error,
                            )
                            self.assertEqual(len(owner_offer), 1)
                            stable_owner = owner_offer[0]
                            connection_offer = object.__getattribute__(
                                stable_owner,
                                (
                                    "_BrowserRequestDeliveryOwner"
                                    "__connection_offer"
                                ),
                            )
                            self.assertEqual(len(connection_offer), 1)
                            connection_owner = connection_offer[0]
                            record = object.__getattribute__(
                                connection_owner,
                                "_record",
                            )
                            lease_token = object.__getattribute__(
                                connection_owner,
                                "_token",
                            )
                            connection = object.__getattribute__(
                                record,
                                "_connection_identity",
                            )
                            self.assertIsNotNone(connection)

                            close_calls = []
                            release_transitions = []
                            original_cleanup = (
                                runtime_module
                                ._cleanup_database_connection_independently
                            )
                            original_release = (
                                browser_module
                                .DurableGoogleLoginBrowserIntegration
                                ._release_request_owner
                            )

                            def count_cleanup(candidate, *, rollback):
                                close_calls.append(candidate)
                                return original_cleanup(
                                    candidate,
                                    rollback=rollback,
                                )

                            def count_release(integration, owner):
                                before = (
                                    integration._request_owner_released(
                                        owner
                                    )
                                )
                                released = original_release(
                                    integration,
                                    owner,
                                )
                                after = (
                                    integration._request_owner_released(
                                        owner
                                    )
                                )
                                if (
                                    not before
                                    and after
                                    and owner
                                    not in release_transitions
                                ):
                                    release_transitions.append(owner)
                                return released

                            injected = exception_type(
                                "PRIVATE_B21_MANAGER_HANDOFF_RETIREMENT"
                            )
                            with mock.patch.object(
                                runtime_module,
                                (
                                    "_cleanup_database_connection_"
                                    "independently"
                                ),
                                side_effect=count_cleanup,
                            ):
                                with mock.patch.object(
                                    browser_module
                                    .DurableGoogleLoginBrowserIntegration,
                                    "_release_request_owner",
                                    new=count_release,
                                ):
                                    (
                                        result,
                                        caught,
                                        _captured,
                                        fired,
                                    ) = self._interrupt_executed_line(
                                        function,
                                        target_line,
                                        browser.close,
                                        injected,
                                    )
                                    self.assertTrue(fired)
                                    self.assertIsNone(result)
                                    self.assertIs(caught, injected)
                                    self.assertIsNone(
                                        injected.__cause__
                                    )
                                    self.assertIsNone(
                                        injected.__context__
                                    )
                                    self.assertEqual(close_calls, [])
                                    self.assertEqual(
                                        release_transitions,
                                        [],
                                    )
                                    self.assertEqual(
                                        browser.active_request_count,
                                        1,
                                    )
                                    self.assertEqual(
                                        len(connection_offer),
                                        1,
                                    )
                                    self.assertIs(
                                        connection_offer[0],
                                        connection_owner,
                                    )
                                    self.assertEqual(
                                        object.__getattribute__(
                                            record,
                                            "_browser_cleanup_mode",
                                        ),
                                        "manager",
                                    )
                                    self.assertIsNone(
                                        object.__getattribute__(
                                            record,
                                            "_browser_cleanup_delegate",
                                        )
                                    )
                                    self.assertIs(
                                        object.__getattribute__(
                                            record,
                                            (
                                                "_browser_cleanup_"
                                                "borrower_token"
                                            ),
                                        ),
                                        lease_token,
                                    )
                                    retained_thread = (
                                        object.__getattribute__(
                                            record,
                                            "_borrower_thread",
                                        )
                                    )
                                    retained_token = (
                                        object.__getattribute__(
                                            record,
                                            "_borrower_token",
                                        )
                                    )
                                    self.assertTrue(
                                        (
                                            retained_thread is worker
                                            and retained_token
                                            is lease_token
                                        )
                                        or (
                                            retained_thread is None
                                            and retained_token
                                            is lease_token
                                        )
                                        or (
                                            retained_thread is None
                                            and retained_token is None
                                        )
                                    )
                                    self.assertIn(
                                        object.__getattribute__(
                                            record,
                                            "_state",
                                        ),
                                        {"leased", "unresolved"},
                                    )

                                    self.assertTrue(browser.close())
                                    self.assertTrue(
                                        connection_owner.closed
                                    )
                                    self.assertEqual(
                                        browser.active_request_count,
                                        0,
                                    )
                                    self.assertEqual(
                                        release_transitions,
                                        [stable_owner],
                                    )
                                    self.assertEqual(close_calls, [])
                                    report = runtime.close()
                                    runtime = None

                            self.assertTrue(report.cleanup_complete)
                            self.assertEqual(close_calls, [connection])
                            self.assertEqual(
                                object.__getattribute__(
                                    manager,
                                    "_records",
                                ),
                                {},
                            )
                            self.assertEqual(
                                object.__getattribute__(
                                    record,
                                    "_state",
                                ),
                                "terminal",
                            )
                            self.assertTrue(
                                object.__getattribute__(
                                    record,
                                    "_descriptor_owner",
                                ).terminal
                            )
                            for field_name in (
                                "_browser_cleanup_borrower_token",
                                "_browser_cleanup_capability",
                                "_browser_cleanup_claim",
                                "_browser_cleanup_delegate",
                                "_browser_cleanup_identity",
                                "_browser_cleanup_mode",
                                "_connection_identity",
                            ):
                                self.assertIsNone(
                                    object.__getattribute__(
                                        record,
                                        field_name,
                                    )
                                )
                            self.assertTrue(
                                stable_owner._is_terminal()
                            )
                            self.assertEqual(
                                object.__getattribute__(
                                    browser,
                                    "_request_owners",
                                ),
                                {},
                            )
                        finally:
                            if worker is not None and worker.is_alive():
                                worker.join(10)
                            if runtime is not None:
                                runtime.close(
                                    _preserve_primary=True
                                )

    def test_b21_browser_cleanup_terminal_truth_survives_close_error(
        self,
    ):
        import wahojobs.durable_google_login_runtime as runtime_module

        for exception_type in (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(exception_type=exception_type.__name__):
                with temporary_browser_login_state() as state:
                    runtime = build_durable_google_login_runtime(
                        state.configuration_path,
                        _clock=state.clock,
                        _gateway_factory=state.gateway_factory,
                    )
                    worker = None
                    try:
                        manager = object.__getattribute__(
                            runtime,
                            "_connections",
                        )
                        delegate = object()
                        delegate_identity = object()
                        capability = object()
                        offered = {}
                        finished = threading.Event()

                        def create_registered_lease():
                            try:
                                lease = manager.open_writable_connection()
                                connection = (
                                    manager._connection_for_lease(lease)
                                )
                                self.assertTrue(
                                    lease
                                    ._wahojobs_register_browser_cleanup(
                                        delegate,
                                        delegate_identity,
                                        capability,
                                    )
                                )
                                offered.update(
                                    lease=lease,
                                    connection=connection,
                                )
                            finally:
                                finished.set()

                        worker = threading.Thread(
                            target=create_registered_lease,
                            name=(
                                "b21-terminal-truth-"
                                + exception_type.__name__
                            ),
                            daemon=False,
                        )
                        worker.start()
                        self.assertTrue(finished.wait(60))
                        worker.join(10)
                        self.assertFalse(worker.is_alive())
                        lease = offered["lease"]
                        connection = offered["connection"]
                        record = object.__getattribute__(lease, "_record")
                        claim = (
                            lease
                            ._wahojobs_claim_abandoned_browser_cleanup(
                                delegate,
                                delegate_identity,
                                capability,
                                connection,
                            )
                        )
                        self.assertIsNot(claim, False)
                        injected = exception_type(
                            "PRIVATE_B21_TERMINAL_CLOSE_RESULT"
                        )
                        cleanup_results = []
                        original_cleanup = (
                            runtime_module
                            ._cleanup_database_connection_independently
                        )

                        def close_then_raise(candidate, *, rollback):
                            result = original_cleanup(
                                candidate,
                                rollback=rollback,
                            )
                            cleanup_results.append((candidate, result))
                            raise injected

                        caught = None
                        with mock.patch.object(
                            runtime_module,
                            "_cleanup_database_connection_independently",
                            side_effect=close_then_raise,
                        ):
                            try:
                                (
                                    lease
                                    ._wahojobs_finish_abandoned_browser_cleanup(
                                        delegate,
                                        delegate_identity,
                                        capability,
                                        claim,
                                    )
                                )
                            except BaseException as exc:
                                caught = exc
                                exc = None
                        self.assertIsNotNone(caught)
                        if exception_type is RuntimeError:
                            self.assertIsInstance(
                                caught,
                                runtime_module._DatabaseCleanupFailure,
                            )
                        else:
                            self.assertIs(caught, injected)
                        self.assertEqual(
                            cleanup_results,
                            [
                                (
                                    connection,
                                    (True, False, None),
                                )
                            ],
                        )
                        self.assertEqual(
                            object.__getattribute__(record, "_state"),
                            "terminal",
                        )
                        self.assertEqual(
                            object.__getattribute__(manager, "_records"),
                            {},
                        )
                        self.assertEqual(
                            object.__getattribute__(
                                record,
                                "_connection_offer",
                            ),
                            [],
                        )
                        for field_name in (
                            "_borrower_thread",
                            "_borrower_token",
                            "_release_thread",
                            "_release_token",
                            "_cleanup_thread",
                            "_cleanup_token",
                        ):
                            self.assertIsNone(
                                object.__getattribute__(
                                    record,
                                    field_name,
                                )
                            )
                        self.assertIs(
                            object.__getattribute__(
                                record,
                                "_connection_identity",
                            ),
                            connection,
                        )
                        self.assertTrue(
                            object.__getattribute__(
                                record,
                                "_descriptor_owner",
                            ).terminal
                        )
                        self.assertTrue(
                            lease
                            ._wahojobs_acknowledge_abandoned_browser_cleanup(
                                delegate,
                                delegate_identity,
                                capability,
                            )
                        )
                        self.assertTrue(lease.closed)
                        self.assertIsNone(
                            object.__getattribute__(
                                record,
                                "_connection_identity",
                            )
                        )
                        self.assertIsNone(
                            object.__getattribute__(
                                record,
                                "_browser_cleanup_delegate",
                            )
                        )
                        self.assertIsNone(
                            object.__getattribute__(
                                record,
                                "_browser_cleanup_claim",
                            )
                        )
                        self.assertEqual(
                            object.__getattribute__(
                                record,
                                "_browser_cleanup_mode",
                            ),
                            "acknowledged",
                        )
                        report = runtime.close()
                        runtime = None
                        self.assertTrue(report.cleanup_complete)
                        self.assertEqual(
                            cleanup_results,
                            [
                                (
                                    connection,
                                    (True, False, None),
                                )
                            ],
                        )
                    finally:
                        if worker is not None and worker.is_alive():
                            worker.join(10)
                        if runtime is not None:
                            runtime.close(_preserve_primary=True)

    def test_b21_handle_handoff_interruptions_release_exact_request_once(self):
        import wahojobs.durable_google_login_browser as browser_module
        import wahojobs.durable_google_login_runtime as runtime_module

        handle_function = (
            browser_module.DurableGoogleLoginBrowserIntegration.handle
        )
        handle_source = "".join(inspect.getsourcelines(handle_function)[0])
        self.assertNotIn("release_request = False", handle_source)
        self.assertNotIn("self._release_active_request", handle_source)
        boundaries = (
            (
                browser_module
                .DurableGoogleLoginBrowserIntegration
                ._register_request_owner,
                "return True",
                "request_registry_published",
                False,
            ),
            (
                handle_function,
                "request_owner._complete_handle(response)",
                "response_bound",
                True,
            ),
            (
                handle_function,
                "return response",
                "response_return",
                True,
            ),
        )
        for function, needle, boundary, has_database_owner in boundaries:
            target_line = self._executed_source_line(
                function,
                needle,
            )
            for exception_type in (
                RuntimeError,
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ):
                with self.subTest(
                    boundary=boundary,
                    exception_type=exception_type.__name__,
                ):
                    with temporary_browser_login_state() as state:
                        runtime = build_durable_google_login_runtime(
                            state.configuration_path,
                            _clock=state.clock,
                            _gateway_factory=state.gateway_factory,
                        )
                        try:
                            browser, target, headers = (
                                self._prepared_callback_request(
                                    runtime,
                                    state,
                                )
                            )
                            manager = object.__getattribute__(
                                runtime,
                                "_connections",
                            )
                            close_calls = []
                            original_cleanup = (
                                runtime_module
                                ._cleanup_database_connection_independently
                            )
                            release_transitions = []
                            original_release = (
                                browser_module
                                .DurableGoogleLoginBrowserIntegration
                                ._release_request_owner
                            )

                            def count_cleanup(connection, *, rollback):
                                close_calls.append(connection)
                                return original_cleanup(
                                    connection,
                                    rollback=rollback,
                                )

                            def count_release(integration, owner):
                                condition = integration._lifecycle_condition
                                with condition:
                                    entry = integration._request_owners.get(
                                        owner._issuance
                                    )
                                    pending = (
                                        entry is owner._registry_entry
                                        and entry[0] is owner
                                        and entry[1] is False
                                    )
                                released = original_release(
                                    integration,
                                    owner,
                                )
                                with condition:
                                    entry = integration._request_owners.get(
                                        owner._issuance
                                    )
                                    transitioned = (
                                        pending
                                        and entry is owner._registry_entry
                                        and entry[0] is owner
                                        and entry[1] is True
                                    )
                                if transitioned:
                                    release_transitions.append(owner)
                                return released

                            injected = exception_type(
                                "PRIVATE_B21_REQUEST_HANDOFF"
                            )

                            def operation():
                                with loopback_and_in_memory_provider_only():
                                    return browser.handle(
                                        "GET",
                                        target,
                                        headers,
                                    )

                            with mock.patch.object(
                                runtime_module,
                                "_cleanup_database_connection_independently",
                                side_effect=count_cleanup,
                            ):
                                with mock.patch.object(
                                    browser_module
                                    .DurableGoogleLoginBrowserIntegration,
                                    "_release_request_owner",
                                    new=count_release,
                                ):
                                    result, caught, captured, fired = (
                                        self._interrupt_executed_line(
                                            function,
                                            target_line,
                                            operation,
                                            injected,
                                        )
                                    )
                            self.assertTrue(fired)
                            self.assertIsNone(result)
                            self.assertIs(caught, injected)
                            self.assertIsNone(caught.__cause__)
                            self.assertIsNone(caught.__context__)
                            self.assertEqual(
                                _retained_canary_hits(
                                    caught,
                                    "PRIVATE_B21_REQUEST_HANDOFF",
                                ),
                                [],
                            )
                            request_owner = captured.get(
                                "request_owner",
                                captured.get("owner"),
                            )
                            self.assertIsInstance(
                                request_owner,
                                browser_module
                                ._BrowserRequestDeliveryOwner,
                            )
                            self.assertTrue(request_owner._is_terminal())
                            response = captured.get("response")
                            if has_database_owner:
                                self.assertIsInstance(
                                    response,
                                    browser_module
                                    .DurableGoogleLoginBrowserResponse,
                                )
                                self.assertIsNone(
                                    response._delivery_owner
                                )
                                self.assertIsNone(
                                    response._delivery_lease
                                )
                                self.assertIsNone(
                                    response._owned_connection
                                )
                                self.assertIsNone(
                                    response._request_release
                                )
                                self.assertEqual(
                                    response._delivery_state,
                                    ["complete"],
                                )
                                before_retry = (
                                    len(close_calls),
                                    browser.active_request_count,
                                    len(
                                        object.__getattribute__(
                                            manager,
                                            "_records",
                                        )
                                    ),
                                )
                                with self.assertRaisesRegex(
                                    RuntimeError,
                                    "already_terminal",
                                ):
                                    response.fail_delivery()
                                self.assertEqual(
                                    (
                                        len(close_calls),
                                        browser.active_request_count,
                                        len(
                                            object.__getattribute__(
                                                manager,
                                                "_records",
                                            )
                                        ),
                                    ),
                                    before_retry,
                                )
                            else:
                                self.assertIsNone(response)
                            self.assertEqual(
                                len(close_calls),
                                1 if has_database_owner else 0,
                            )
                            self.assertEqual(
                                release_transitions,
                                [request_owner],
                            )
                            self.assertEqual(
                                object.__getattribute__(manager, "_records"),
                                {},
                            )
                            self.assertEqual(browser.active_request_count, 0)
                            self.assertEqual(
                                object.__getattribute__(
                                    browser,
                                    "_request_owners",
                                ),
                                {},
                            )
                            report = runtime.close()
                            runtime = None
                            self.assertTrue(report.cleanup_complete)
                        finally:
                            if runtime is not None:
                                runtime.close(_preserve_primary=True)

        with temporary_browser_login_state() as state:
            runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            browser = runtime.browser_integration
            manager = object.__getattribute__(runtime, "_connections")
            target_line = self._executed_source_line(
                browser_module
                .DurableGoogleLoginBrowserIntegration
                ._register_request_owner,
                "return True",
            )
            outcome = {}
            completed = threading.Event()
            abort_injected = RuntimeError(
                "PRIVATE_B21_EMPTY_OWNER_ABORT"
            )

            def worker_operation():
                try:
                    injected = GeneratorExit(
                        "PRIVATE_B21_EMPTY_OWNER_REGISTRATION"
                    )

                    def operation():
                        return browser.handle(
                            "GET",
                            "/login",
                            (
                                (
                                    "Host",
                                    urlsplit(
                                        runtime.configuration.public_origin
                                    ).netloc,
                                ),
                            ),
                        )

                    with mock.patch.object(
                        browser_module._BrowserRequestDeliveryOwner,
                        "_request_abort",
                        side_effect=abort_injected,
                    ):
                        result, caught, captured, fired = (
                            self._interrupt_executed_line(
                                browser_module
                                .DurableGoogleLoginBrowserIntegration
                                ._register_request_owner,
                                target_line,
                                operation,
                                injected,
                            )
                        )
                    outcome.update(
                        result=result,
                        caught=caught,
                        captured=captured,
                        fired=fired,
                        error=None,
                    )
                except BaseException as exc:
                    outcome["error"] = exc
                finally:
                    completed.set()

            worker = threading.Thread(
                target=worker_operation,
                name="b21-empty-owner-worker",
            )
            try:
                worker.start()
                self.assertTrue(completed.wait(60))
                worker.join(10)
                self.assertFalse(worker.is_alive())
                self.assertIsNone(outcome.get("error"))
                self.assertTrue(outcome["fired"])
                self.assertIsNone(outcome["result"])
                self.assertIsInstance(
                    outcome["caught"],
                    GeneratorExit,
                )
                request_owner = outcome["captured"]["owner"]
                entries = object.__getattribute__(
                    browser,
                    "_request_owners",
                )
                self.assertEqual(len(entries), 1)
                self.assertIs(
                    entries[request_owner._issuance],
                    request_owner._registry_entry,
                )
                self.assertFalse(entries[request_owner._issuance][1])
                self.assertEqual(browser.active_request_count, 1)
                self.assertEqual(
                    object.__getattribute__(manager, "_records"),
                    {},
                )
                self.assertTrue(browser.close())
                self.assertTrue(request_owner._is_terminal())
                self.assertEqual(browser.active_request_count, 0)
                self.assertEqual(
                    object.__getattribute__(
                        browser,
                        "_request_owners",
                    ),
                    {},
                )
                self.assertEqual(
                    object.__getattribute__(manager, "_records"),
                    {},
                )
                report = runtime.close()
                runtime = None
                self.assertTrue(report.cleanup_complete)
            finally:
                if worker.is_alive():
                    worker.join(10)
                if runtime is not None:
                    runtime.close(_preserve_primary=True)

    def test_b21_delivery_retirement_interruptions_keep_cleanup_actionable(self):
        import wahojobs.durable_google_login_browser as browser_module
        import wahojobs.durable_google_login_runtime as runtime_module

        owner_type = browser_module._BrowserRequestDeliveryOwner
        boundaries = (
            (
                owner_type._advance_delivery,
                "self.__delivery_complete = True",
                3,
                0,
                "delivery_acknowledgement",
                False,
            ),
            (
                owner_type._publish_delivery_terminal,
                "self.__response_scrubbed = True",
                1,
                1,
                "public_terminal_publication",
                False,
            ),
            (
                owner_type._advance_connection_close,
                "result = close()",
                1,
                0,
                "before_connection_close",
                False,
            ),
            (
                owner_type._advance_connection_close,
                "closed = False",
                1,
                0,
                "after_connection_close",
                False,
            ),
            (
                owner_type._advance_request_release,
                "released = integration._release_request_owner(self)",
                1,
                0,
                "before_request_release",
                False,
            ),
            (
                owner_type._advance_request_release,
                "released = (",
                1,
                0,
                "after_request_release",
                False,
            ),
            (
                owner_type._retire_if_complete,
                'object.__setattr__(response, "_request_release", None)',
                1,
                1,
                "before_terminal_commit",
                False,
            ),
            (
                owner_type._retire_if_complete,
                "terminal = True",
                2,
                0,
                "after_terminal_commit",
                False,
            ),
            (
                owner_type._prune_terminal,
                "if not integration._prune_request_owner(self):",
                1,
                0,
                "before_registry_prune",
                True,
            ),
            (
                browser_module
                .DurableGoogleLoginBrowserIntegration
                ._prune_request_owner,
                "del self._request_owners[owner._issuance]",
                1,
                0,
                "before_registry_delete",
                True,
            ),
            (
                browser_module
                .DurableGoogleLoginBrowserIntegration
                ._prune_request_owner,
                "condition.notify_all()",
                1,
                0,
                "after_registry_delete",
                True,
            ),
            (
                owner_type._prune_terminal,
                "self.__integration = None",
                1,
                0,
                "after_registry_prune",
                True,
            ),
        )
        for (
            function,
            needle,
            occurrence,
            offset,
            boundary,
            coordinator_recovery,
        ) in boundaries:
            target_line = self._executed_source_line(
                function,
                needle,
                occurrence=occurrence,
                offset=offset,
            )
            for exception_type in (
                RuntimeError,
                KeyboardInterrupt,
                SystemExit,
                GeneratorExit,
            ):
                with self.subTest(
                    boundary=boundary,
                    exception_type=exception_type.__name__,
                ):
                    with temporary_browser_login_state() as state:
                        runtime = build_durable_google_login_runtime(
                            state.configuration_path,
                            _clock=state.clock,
                            _gateway_factory=state.gateway_factory,
                        )
                        response = None
                        try:
                            response = self._pending_delivery_response(
                                runtime,
                                state,
                            )
                            browser = runtime.browser_integration
                            manager = object.__getattribute__(
                                runtime,
                                "_connections",
                            )
                            owner = response._owned_connection
                            wrapper = response._delivery_lease
                            stable_owner = response._delivery_owner
                            close_calls = []
                            original_cleanup = (
                                runtime_module
                                ._cleanup_database_connection_independently
                            )

                            def count_cleanup(connection, *, rollback):
                                close_calls.append(connection)
                                return original_cleanup(
                                    connection,
                                    rollback=rollback,
                                )

                            injected = exception_type(
                                "PRIVATE_B21_DELIVERY_RETIREMENT"
                            )
                            with mock.patch.object(
                                runtime_module,
                                "_cleanup_database_connection_independently",
                                side_effect=count_cleanup,
                            ):
                                result, caught, _captured, fired = (
                                    self._interrupt_executed_line(
                                        function,
                                        target_line,
                                        response.fail_delivery,
                                        injected,
                                    )
                                )
                                self.assertTrue(fired)
                                self.assertIsNone(result)
                                self.assertIs(caught, injected)
                                current_response_owner = (
                                    response._delivery_owner
                                )
                                self.assertIn(
                                    current_response_owner,
                                    (None, stable_owner),
                                )
                                if coordinator_recovery:
                                    self.assertTrue(browser.close())
                                else:
                                    try:
                                        response.fail_delivery()
                                    except RuntimeError as retry:
                                        self.assertIn(
                                            "already_terminal",
                                            str(retry),
                                        )
                                    self.assertTrue(browser.close())
                                for field_name in (
                                    "__integration",
                                    "__process_guard",
                                    "__response",
                                    "__delivery_bundle",
                                ):
                                    self.assertIsNone(
                                        object.__getattribute__(
                                            stable_owner,
                                            (
                                                "_BrowserRequestDeliveryOwner"
                                                + field_name
                                            ),
                                        )
                                    )
                                for field_name in (
                                    "__connection_offer",
                                    "__raw_connection_offer",
                                    "__delivery_offer",
                                ):
                                    self.assertEqual(
                                        object.__getattribute__(
                                            stable_owner,
                                            (
                                                "_BrowserRequestDeliveryOwner"
                                                + field_name
                                            ),
                                        ),
                                        [],
                                    )
                                with self.assertRaisesRegex(
                                    RuntimeError,
                                    "already_terminal",
                                ):
                                    response.fail_delivery()
                                response = None
                            raw = object.__getattribute__(
                                wrapper,
                                "_AuthorityFencedSessionDeliveryLease__lease",
                            )
                            self.assertEqual(
                                object.__getattribute__(raw, "_status"),
                                "failed",
                            )
                            self.assertTrue(owner.closed)
                            self.assertEqual(len(close_calls), 1)
                            self.assertEqual(
                                object.__getattribute__(manager, "_records"),
                                {},
                            )
                            self.assertEqual(browser.active_request_count, 0)
                            self.assertEqual(
                                object.__getattribute__(
                                    browser,
                                    "_request_owners",
                                ),
                                {},
                            )
                            self.assertTrue(stable_owner._is_terminal())
                            report = runtime.close()
                            runtime = None
                            self.assertTrue(report.cleanup_complete)
                        finally:
                            if response is not None:
                                try:
                                    response.fail_delivery()
                                except BaseException:
                                    pass
                            if runtime is not None:
                                runtime.close(_preserve_primary=True)

        with temporary_browser_login_state() as state:
            runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            browser = runtime.browser_integration
            manager = object.__getattribute__(runtime, "_connections")
            original_cleanup = (
                runtime_module._cleanup_database_connection_independently
            )
            close_calls = []
            outcome = {}
            completed = threading.Event()

            def count_cleanup(connection, *, rollback):
                close_calls.append(connection)
                return original_cleanup(connection, rollback=rollback)

            def worker_operation():
                response = None
                try:
                    response = self._pending_delivery_response(
                        runtime,
                        state,
                    )
                    stable_owner = response._delivery_owner
                    with mock.patch.object(
                        runtime_module,
                        "_cleanup_database_connection_independently",
                        side_effect=count_cleanup,
                    ):
                        with mock.patch.object(
                            browser_module
                            .DurableGoogleLoginBrowserIntegration,
                            "_prune_request_owner",
                            return_value=False,
                        ):
                            response.fail_delivery()
                    outcome.update(
                        response=response,
                        owner=stable_owner,
                        error=None,
                    )
                except BaseException as exc:
                    outcome["error"] = exc
                finally:
                    completed.set()

            worker = threading.Thread(
                target=worker_operation,
                name="b21-terminal-owner-worker",
            )
            try:
                worker.start()
                self.assertTrue(completed.wait(60))
                worker.join(10)
                self.assertFalse(worker.is_alive())
                self.assertIsNone(outcome.get("error"))
                stable_owner = outcome["owner"]
                response = outcome["response"]
                entries = object.__getattribute__(
                    browser,
                    "_request_owners",
                )
                self.assertEqual(len(entries), 1)
                self.assertIs(
                    entries[stable_owner._issuance],
                    stable_owner._registry_entry,
                )
                self.assertTrue(
                    entries[stable_owner._issuance][1]
                )
                self.assertTrue(stable_owner._is_terminal())
                self.assertEqual(
                    object.__getattribute__(manager, "_records"),
                    {},
                )
                self.assertEqual(len(close_calls), 1)
                self.assertTrue(browser.close())
                self.assertEqual(
                    object.__getattribute__(
                        browser,
                        "_request_owners",
                    ),
                    {},
                )
                self.assertIsNone(response._delivery_owner)
                report = runtime.close()
                runtime = None
                self.assertTrue(report.cleanup_complete)
            finally:
                if worker.is_alive():
                    worker.join(10)
                if runtime is not None:
                    runtime.close(_preserve_primary=True)

    def test_b21_explicit_reload_callbacks_share_current_epoch_state(self):
        source = r"""
import importlib
import json
import os
import sys

callbacks = []
had_registration = hasattr(os, "register_at_fork")
original_registration = getattr(os, "register_at_fork", None)

def capture_registration(**callbacks_by_phase):
    callback = callbacks_by_phase.get("after_in_child")
    if callback is not None:
        callbacks.append(callback)

os.register_at_fork = capture_registration
try:
    import wahojobs.durable_google_login_runtime as runtime
    first_callback = runtime._DATABASE_PROCESS_EPOCH_AT_FORK_CALLBACK
    first_epoch = runtime._current_database_process_epoch()
    first_lock = runtime._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK
    interposed_epochs = []
    interposed_locks = []

    def publish_between_registrations():
        interposed_epochs.append(runtime._current_database_process_epoch())
        interposed_locks.append(
            runtime._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK
        )

    os.register_at_fork(after_in_child=publish_between_registrations)
    runtime = importlib.reload(runtime)
    second_callback = runtime._DATABASE_PROCESS_EPOCH_AT_FORK_CALLBACK
    second_epoch = runtime._current_database_process_epoch()
    second_lock = runtime._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK
    database_callbacks = [
        callback
        for callback in callbacks
        if callback.__name__
        == "_database_process_epoch_after_fork_callback"
    ]
    if len(database_callbacks) != 1:
        raise AssertionError("reload_callback_count")
    registered_callback = database_callbacks[0]
    relevant_callbacks = [
        callback
        for callback in callbacks
        if callback is registered_callback
        or callback is publish_between_registrations
    ]
    if relevant_callbacks != [
        registered_callback,
        publish_between_registrations,
    ]:
        raise AssertionError("reload_callback_order")
    if (
        registered_callback is not first_callback
        or registered_callback is not second_callback
        or registered_callback.__globals__ is not runtime.__dict__
    ):
        raise AssertionError("reload_callback_identity")
    registered_callback()
    reset_left_epoch_vacant = runtime._DATABASE_PROCESS_EPOCH is None
    reset_lock = runtime._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK
    publish_between_registrations()
    final_epoch = runtime._current_database_process_epoch()
    outcome = {
        "epochs_replaced_on_reload": first_epoch is not second_epoch,
        "locks_replaced_on_reload": first_lock is not second_lock,
        "registered_once": len(database_callbacks) == 1,
        "callback_preserved": (
            first_callback is second_callback
            and registered_callback is first_callback
        ),
        "registration_marked": (
            runtime._DATABASE_PROCESS_EPOCH_AT_FORK_REGISTERED is True
        ),
        "reset_once": (
            reset_left_epoch_vacant
            and reset_lock is not second_lock
        ),
        "interposed_once": (
            len(interposed_epochs) == 1
            and len(interposed_locks) == 1
        ),
        "interposed_authoritative": (
            len(interposed_epochs) == 1
            and interposed_epochs[0] is final_epoch
            and interposed_epochs[0] is runtime._DATABASE_PROCESS_EPOCH
            and interposed_locks[0] is reset_lock
        ),
        "final_authoritative": (
            final_epoch is runtime._DATABASE_PROCESS_EPOCH
            and final_epoch.pid == os.getpid()
        ),
        "hash_seed": os.environ.get("PYTHONHASHSEED"),
    }
    sys.stdout.write(
        json.dumps(
            outcome,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
finally:
    if had_registration:
        os.register_at_fork = original_registration
    else:
        del os.register_at_fork
"""
        result = self._run_isolated_python(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        outcome = self._b21_decode_exact_json_object(
            result.stdout.encode("utf-8", "strict"),
            field_types={
                "epochs_replaced_on_reload": bool,
                "locks_replaced_on_reload": bool,
                "registered_once": bool,
                "callback_preserved": bool,
                "registration_marked": bool,
                "reset_once": bool,
                "interposed_once": bool,
                "interposed_authoritative": bool,
                "final_authoritative": bool,
                "hash_seed": type(os.environ.get("PYTHONHASHSEED")),
            },
        )
        self.assertEqual(
            outcome.pop("hash_seed"),
            os.environ.get("PYTHONHASHSEED"),
        )
        self.assertTrue(all(outcome.values()), outcome)

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "fork"),
        "requires_posix_fork",
    )
    def test_b21_explicit_reload_duplicate_at_fork_callbacks_are_harmless(
        self,
    ):
        from tests.google_oidc_gateway_test_support import (
            ManualClock,
            NOW,
            make_real_gateway,
        )

        reload_primary = RuntimeError(
            "reload_real_provider_body_failure"
        )
        reload_cleanup = GeneratorExit(
            "reload_real_provider_cleanup_failure"
        )
        reload_harness = make_real_gateway(clock=ManualClock(NOW))
        reload_harnesses = [reload_harness]
        reload_transport_type = type(reload_harness.transport)
        reload_transport_close = reload_transport_type.close
        reload_success = None
        observed_reload_cleanup = None

        def fail_reload_provider_close(_transport):
            raise reload_cleanup

        with mock.patch.object(
            reload_transport_type,
            "close",
            new=fail_reload_provider_close,
        ):
            try:
                try:
                    raise reload_primary
                except BaseException:
                    try:
                        reload_success = (
                            self._b21_success_after_harness_cleanup(
                                {},
                                reload_harnesses,
                            )
                        )
                    except BaseException as cleanup_error:
                        observed_reload_cleanup = cleanup_error
                    if observed_reload_cleanup is not None:
                        self._b21_preserve_primary_cleanup_failures(
                            (
                                (
                                    "reload_provider_cleanup",
                                    observed_reload_cleanup,
                                ),
                            )
                        )
                    raise
            except BaseException as caught_primary:
                self.assertIs(caught_primary, reload_primary)
        self.assertIs(observed_reload_cleanup, reload_cleanup)
        self.assertIsNone(reload_success)
        self.assertEqual(reload_harnesses, [reload_harness])
        self.assertTrue(
            any(
                (
                    "b21_cleanup_failure:"
                    "reload_provider_cleanup:GeneratorExit"
                )
                in note
                for note in getattr(reload_primary, "__notes__", ())
            )
        )
        self.assertEqual(
            self._b21_success_after_harness_cleanup(
                {},
                reload_harnesses,
            ),
            {
                "ok": True,
                "stage": "complete",
                "outcome": {},
            },
        )
        self.assertEqual(reload_harnesses, [])
        self.assertIs(
            reload_transport_type.close,
            reload_transport_close,
        )

        source = r"""
import importlib
import io
import json
import os
import select
import signal
import sys
import time
import traceback

outcome_types = {
    "callbacks_share_globals": bool,
    "registered_once": bool,
    "callback_preserved": bool,
    "callback_fifo": bool,
    "callback_locks_distinct": bool,
    "sole_reset_lock_authoritative": bool,
    "interposed_publication_once": bool,
    "interposed_publication_authoritative": bool,
    "child_lock_replaced": bool,
    "child_epoch_fresh": bool,
    "child_runtime_fresh": bool,
    "child_runtime_cleanup": bool,
    "hash_seed": type(os.environ.get("PYTHONHASHSEED")),
}

def failure_envelope(error, stage, cleanup_failures=()):
    details = "".join(
        traceback.format_exception(error)
    )[-12288:]
    for cleanup_stage, cleanup_error in cleanup_failures:
        details = (
            details
            + "\ncleanup_stage="
            + str(cleanup_stage)[:256]
            + "\n"
            + "".join(
                traceback.format_exception(cleanup_error)
            )[-4096:]
        )[-12288:]
    return {
        "ok": False,
        "stage": str(stage)[:256],
        "type": type(error).__name__[:256],
        "message": str(error)[:2048],
        "traceback": details,
    }

def encode_envelope(envelope):
    document = json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    if not document or len(document) > 32768:
        raise ValueError("reload_child_document_too_large")
    return document

def strict_json_value(document):
    def reject_duplicate_members(pairs):
        value = {}
        for name, member in pairs:
            if name in value:
                raise ValueError("reload_child_duplicate_member")
            value[name] = member
        return value

    def reject_nonstandard_constant(_value):
        raise ValueError("reload_child_nonstandard_constant")

    try:
        text = document.decode("utf-8", "strict")
        decoder = json.JSONDecoder(
            object_pairs_hook=reject_duplicate_members,
            parse_constant=reject_nonstandard_constant,
        )
        value, end = decoder.raw_decode(text)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        raise AssertionError("reload_child_invalid_result") from error
    if end != len(text):
        raise AssertionError("reload_child_trailing_material")
    return value


def decode_envelope(document):
    if (
        type(document) is not bytes
        or not document
        or len(document) > 32768
    ):
        raise AssertionError("reload_child_document_size")
    envelope = strict_json_value(document)
    if type(envelope) is not dict or type(envelope.get("ok")) is not bool:
        raise AssertionError("reload_child_invalid_envelope")
    if envelope["ok"] is True:
        if set(envelope) != {"ok", "stage", "outcome"}:
            raise AssertionError("reload_child_success_shape")
        if envelope["stage"] != "complete":
            raise AssertionError("reload_child_success_stage")
        outcome = envelope["outcome"]
        if type(outcome) is not dict or set(outcome) != set(outcome_types):
            raise AssertionError("reload_child_success_outcome_shape")
        if any(
            type(outcome[name]) is not expected_type
            for name, expected_type in outcome_types.items()
        ):
            raise AssertionError("reload_child_success_outcome_type")
        return True, outcome
    if set(envelope) != {
        "ok",
        "stage",
        "type",
        "message",
        "traceback",
    }:
        raise AssertionError("reload_child_failure_shape")
    if (
        type(envelope["stage"]) is not str
        or not envelope["stage"]
        or type(envelope["type"]) is not str
        or not envelope["type"]
        or type(envelope["message"]) is not str
        or type(envelope["traceback"]) is not str
    ):
        raise AssertionError("reload_child_failure_type")
    return False, envelope


class PipeEndpoint:
    __slots__ = ("stream", "identity")

    def __init__(self, descriptor, mode):
        self.stream = io.FileIO(descriptor, mode, closefd=True)
        self.identity = object()

    @property
    def closed(self):
        return self.stream.closed

    def fileno(self):
        return self.stream.fileno()

    def __index__(self):
        return self.fileno()

    def close(self):
        self.stream.close()

REAPED_STATUS_UNKNOWN = object()


def make_pipe():
    reader_descriptor, writer_descriptor = os.pipe()
    reader = None
    try:
        reader = PipeEndpoint(reader_descriptor, "rb")
        writer = PipeEndpoint(writer_descriptor, "wb")
    except BaseException:
        if reader is None:
            os.close(reader_descriptor)
        else:
            reader.close()
        os.close(writer_descriptor)
        raise
    return reader, writer


def waitpid_until(child_pid, deadline):
    while True:
        try:
            waited, status = os.waitpid(child_pid, os.WNOHANG)
        except ChildProcessError:
            return REAPED_STATUS_UNKNOWN
        except InterruptedError:
            if time.monotonic() >= deadline:
                raise TimeoutError("reload_child_reap_timeout")
            continue
        if waited == child_pid:
            return status
        if waited != 0:
            raise AssertionError("reload_child_wrong_pid")
        if time.monotonic() >= deadline:
            raise TimeoutError("reload_child_reap_timeout")


def waitpid_bounded(child_pid, timeout):
    return waitpid_until(child_pid, time.monotonic() + timeout)


def probe_exact_child(child_pid, deadline):
    while True:
        try:
            waited, status = os.waitpid(child_pid, os.WNOHANG)
        except ChildProcessError:
            return True, REAPED_STATUS_UNKNOWN
        except InterruptedError:
            if time.monotonic() >= deadline:
                raise TimeoutError("reload_child_reap_timeout")
            continue
        if waited == child_pid:
            return True, status
        if waited == 0:
            return False, None
        raise AssertionError("reload_child_wrong_pid")


def terminate_and_reap(child_pid, deadline=None, timeout=10):
    if deadline is None:
        deadline = time.monotonic() + timeout
    reaped_child, status = probe_exact_child(child_pid, deadline)
    if reaped_child:
        return status
    try:
        os.kill(child_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    reaped_child, status = probe_exact_child(child_pid, deadline)
    if reaped_child:
        return status
    try:
        os.kill(child_pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return waitpid_until(child_pid, deadline)


def read_pipe_bounded(descriptor, timeout, maximum_size=32768):
    chunks = []
    size = 0
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("reload_child_pipe_timeout")
        try:
            readable, _writable, _exceptional = select.select(
                [descriptor],
                [],
                [],
                remaining,
            )
        except InterruptedError:
            continue
        if not readable:
            raise TimeoutError("reload_child_pipe_timeout")
        requested = min(4096, maximum_size + 1 - size)
        try:
            chunk = os.read(descriptor, requested)
        except InterruptedError:
            continue
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > maximum_size:
            raise AssertionError("reload_child_document_too_large")
        chunks.append(chunk)


def close_endpoints_and_reap(
    child_pid,
    child_reaped,
    endpoints,
    timeout=10,
):
    deadline = time.monotonic() + timeout
    failures = []
    seen = set()
    for cleanup_stage, endpoint in endpoints:
        if endpoint is None or endpoint.identity in seen:
            continue
        seen.add(endpoint.identity)
        for attempt in range(2):
            if endpoint.closed:
                break
            try:
                endpoint.close()
            except BaseException as cleanup_error:
                failures.append(
                    (
                        cleanup_stage
                        + "_attempt_"
                        + str(attempt + 1),
                        cleanup_error,
                    )
                )
        if not endpoint.closed:
            unresolved = AssertionError(
                "reload_child_endpoint_unresolved"
            )
            unresolved.endpoint_owner = endpoint
            failures.append(
                (
                    cleanup_stage + "_unresolved",
                    unresolved,
                )
            )
    status = None
    if child_pid not in {None, 0} and not child_reaped:
        for attempt in range(1, 3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                status = terminate_and_reap(
                    child_pid,
                    deadline=deadline,
                )
            except BaseException as cleanup_error:
                failures.append(
                    (
                        "child_reap_attempt_" + str(attempt),
                        cleanup_error,
                    )
                )
            if status is not None:
                break
        if status is None and time.monotonic() < deadline:
            try:
                reaped_child, status = probe_exact_child(
                    child_pid,
                    deadline,
                )
                if not reaped_child:
                    try:
                        os.kill(child_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    reaped_child, status = probe_exact_child(
                        child_pid,
                        deadline,
                    )
                    if not reaped_child:
                        try:
                            os.kill(child_pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        status = waitpid_until(
                            child_pid,
                            deadline,
                        )
            except BaseException as cleanup_error:
                failures.append(
                    ("child_reap_final", cleanup_error)
                )
        if status is None:
            failures.append(
                (
                    "child_reap_unresolved",
                    TimeoutError("reload_child_reap_timeout"),
                )
            )
        elif status is REAPED_STATUS_UNKNOWN:
            failures.append(
                (
                    "child_reap_status_unavailable",
                    ChildProcessError(
                        "reload_child_reaped_without_status"
                    ),
                )
            )
    return status, tuple(failures)


def preserve_primary_cleanup_failures(failures):
    if not failures:
        return
    primary = sys.exception()
    if primary is None:
        primary = failures[0][1]
        remaining_failures = failures[1:]
    else:
        remaining_failures = failures
    for cleanup_stage, cleanup_error in remaining_failures:
        primary.add_note(
            "reload_cleanup_failure:"
            + str(cleanup_stage)[:256]
            + ":"
            + type(cleanup_error).__name__[:256]
            + ":"
            + str(cleanup_error)[:1024]
        )
    if sys.exception() is None:
        raise primary

def already_reaped(_child_pid, _options):
    raise ChildProcessError("reload_child_already_reaped")

lost_status_signals = []
saved_waitpid = os.waitpid
saved_kill = os.kill
os.waitpid = already_reaped
os.kill = lambda child_pid, number: lost_status_signals.append(
    (child_pid, number)
)
try:
    lost_status, lost_status_failures = close_endpoints_and_reap(
        769,
        False,
        (),
    )
finally:
    os.waitpid = saved_waitpid
    os.kill = saved_kill
if (
    lost_status is not REAPED_STATUS_UNKNOWN
    or lost_status_signals
    or [stage for stage, _error in lost_status_failures]
    != ["child_reap_status_unavailable"]
    or type(lost_status_failures[0][1]) is not ChildProcessError
):
    raise AssertionError("reload_child_lost_status_fence")

valid_outcome = {
    name: (
        os.environ.get("PYTHONHASHSEED")
        if name == "hash_seed"
        else True
    )
    for name in outcome_types
}
valid_success = {
    "ok": True,
    "stage": "complete",
    "outcome": valid_outcome,
}
valid_failure = {
    "ok": False,
    "stage": "probe",
    "type": "RuntimeError",
    "message": "probe",
    "traceback": "probe",
}
valid_success_document = encode_envelope(valid_success)
duplicate_outer_document = valid_success_document.replace(
    b'{"ok":true,',
    b'{"ok":false,"ok":true,',
    1,
)
duplicate_nested_document = valid_success_document.replace(
    b'"callbacks_share_globals":true',
    (
        b'"callbacks_share_globals":false,'
        b'"callbacks_share_globals":true'
    ),
    1,
)
invalid_envelopes = (
    True,
    False,
    None,
    {},
    {"ok": 1, "stage": "complete", "outcome": valid_outcome},
    {**valid_success, "type": "RuntimeError"},
    {**valid_success, "unexpected": False},
    {"ok": True, "stage": "complete"},
    {"ok": True, "stage": "wrong", "outcome": valid_outcome},
    {"ok": True, "stage": "complete", "outcome": {}},
    {**valid_failure, "outcome": valid_outcome},
    {key: value for key, value in valid_failure.items() if key != "type"},
    {**valid_failure, "stage": None},
)
invalid_documents = (
    b"",
    b"{",
    b"x" * 32769,
    duplicate_outer_document,
    duplicate_nested_document,
    b" " + valid_success_document,
    valid_success_document + b"\n",
    valid_success_document + b"{}",
    *(
        json.dumps(value).encode("ascii")
        for value in invalid_envelopes
    ),
)
for invalid_document in invalid_documents:
    try:
        decode_envelope(invalid_document)
    except AssertionError:
        pass
    else:
        raise AssertionError("reload_child_invalid_envelope_accepted")
failure_kind, failure_value = decode_envelope(
    encode_envelope(valid_failure)
)
if failure_kind is not False or failure_value != valid_failure:
    raise AssertionError("reload_child_failure_variant")

for endpoint_error in (
    RuntimeError("reload_endpoint_runtime"),
    KeyboardInterrupt("reload_endpoint_keyboard"),
    SystemExit(0),
    SystemExit("reload_endpoint_system_exit"),
    GeneratorExit("reload_endpoint_generator"),
):
    first_endpoint, second_endpoint = make_pipe()
    original_endpoint_close = PipeEndpoint.close
    original_terminate_and_reap = terminate_and_reap
    interrupted = [False]
    reap_calls = []

    def interrupt_endpoint_close(
        endpoint,
        *,
        target=first_endpoint,
        injected=endpoint_error,
        original_close=original_endpoint_close,
    ):
        if endpoint is target and not interrupted[0]:
            interrupted[0] = True
            raise injected
        return original_close(endpoint)

    def record_reap(child_pid, *, deadline=None, timeout=10):
        reap_calls.append((child_pid, deadline, timeout))
        return 37

    PipeEndpoint.close = interrupt_endpoint_close
    terminate_and_reap = record_reap
    try:
        endpoint_status, endpoint_failures = close_endpoints_and_reap(
            778,
            False,
            (
                ("first_endpoint", first_endpoint),
                ("second_endpoint", second_endpoint),
            ),
        )
    finally:
        PipeEndpoint.close = original_endpoint_close
        terminate_and_reap = original_terminate_and_reap
    if (
        endpoint_status != 37
        or len(reap_calls) != 1
        or reap_calls[0][0] != 778
        or type(reap_calls[0][1]) is not float
        or reap_calls[0][2] != 10
        or not first_endpoint.closed
        or not second_endpoint.closed
        or len(endpoint_failures) != 1
        or endpoint_failures[0][0] != "first_endpoint_attempt_1"
        or endpoint_failures[0][1] is not endpoint_error
    ):
        raise AssertionError("reload_endpoint_cleanup_probe")

saved_terminate_and_reap = terminate_and_reap
saved_probe_exact_child = probe_exact_child
saved_waitpid_until = waitpid_until
saved_kill = os.kill
reap_errors = iter(
    (
        RuntimeError("reload_reap_runtime"),
        GeneratorExit("reload_reap_generator"),
    )
)
reap_deadlines = []
final_reap_order = []

def fail_bounded_reap(child_pid, deadline=None, timeout=10):
    reap_deadlines.append(deadline)
    raise next(reap_errors)

def record_final_probe(child_pid, deadline):
    final_reap_order.append(("probe", child_pid, deadline))
    return False, None

def record_final_signal(child_pid, number):
    final_reap_order.append(("signal", child_pid, number))

def record_final_wait(child_pid, deadline):
    final_reap_order.append(("wait", child_pid, deadline))
    return 43

terminate_and_reap = fail_bounded_reap
probe_exact_child = record_final_probe
waitpid_until = record_final_wait
os.kill = record_final_signal
try:
    persistent_status, persistent_failures = close_endpoints_and_reap(
        780,
        False,
        (),
    )
finally:
    terminate_and_reap = saved_terminate_and_reap
    probe_exact_child = saved_probe_exact_child
    waitpid_until = saved_waitpid_until
    os.kill = saved_kill
if (
    persistent_status != 43
    or len(reap_deadlines) != 2
    or reap_deadlines[0] != reap_deadlines[1]
    or [item[:2] for item in final_reap_order]
    != [
        ("probe", 780),
        ("signal", 780),
        ("probe", 780),
        ("signal", 780),
        ("wait", 780),
    ]
    or final_reap_order[0][2] != reap_deadlines[0]
    or final_reap_order[1][2] != signal.SIGTERM
    or final_reap_order[2][2] != reap_deadlines[0]
    or final_reap_order[3][2] != signal.SIGKILL
    or final_reap_order[4][2] != reap_deadlines[0]
    or [stage for stage, _error in persistent_failures]
    != ["child_reap_attempt_1", "child_reap_attempt_2"]
):
    raise AssertionError("reload_reap_final_probe")

for primary_error, cleanup_error in (
    (
        RuntimeError("reload_body_runtime"),
        GeneratorExit("reload_cleanup_generator"),
    ),
    (
        KeyboardInterrupt("reload_body_keyboard"),
        RuntimeError("reload_cleanup_runtime"),
    ),
    (
        SystemExit(0),
        KeyboardInterrupt("reload_cleanup_keyboard"),
    ),
    (
        SystemExit("reload_body_system_exit"),
        RuntimeError("reload_cleanup_runtime"),
    ),
    (
        GeneratorExit("reload_body_generator"),
        SystemExit("reload_cleanup_system_exit"),
    ),
):
    try:
        try:
            raise primary_error
        except BaseException:
            preserve_primary_cleanup_failures(
                (("reload_cleanup_probe", cleanup_error),)
            )
            raise
    except BaseException as caught:
        if caught is not primary_error:
            raise AssertionError("reload_cleanup_primary_replaced")
    else:
        raise AssertionError("reload_cleanup_primary_suppressed")
    if not any(
        "reload_cleanup_failure:reload_cleanup_probe:"
        + type(cleanup_error).__name__
        in note
        for note in getattr(primary_error, "__notes__", ())
    ):
        raise AssertionError("reload_cleanup_evidence_missing")

callbacks = []
callback_execution = []
callback_locks = []
publication_epochs = []
publication_locks = []
real_register = os.register_at_fork

def register(**callbacks_by_phase):
    callback = callbacks_by_phase.get("after_in_child")
    forwarded = dict(callbacks_by_phase)
    if (
        callback is not None
        and callback.__name__
        == "_database_process_epoch_after_fork_callback"
    ):
        tag = len(callbacks)
        callbacks.append(callback)

        def tagged_callback(callback=callback, tag=tag):
            callback()
            callback_execution.append(tag)
            callback_locks.append(
                runtime._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK
            )

        forwarded["after_in_child"] = tagged_callback
    real_register(**forwarded)

os.register_at_fork = register
import wahojobs.durable_google_login_runtime as runtime
first_callback = runtime._DATABASE_PROCESS_EPOCH_AT_FORK_CALLBACK
pre_reload_epoch = runtime._current_database_process_epoch()
pre_reload_lock = runtime._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK

def publish_between_registrations():
    publication_epochs.append(runtime._current_database_process_epoch())
    publication_locks.append(
        runtime._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK
    )

real_register(after_in_child=publish_between_registrations)
runtime = importlib.reload(runtime)
second_callback = runtime._DATABASE_PROCESS_EPOCH_AT_FORK_CALLBACK
database_callbacks = [
    callback
    for callback in callbacks
    if callback.__name__
    == "_database_process_epoch_after_fork_callback"
]
if len(database_callbacks) != 1:
    raise AssertionError("reload_callback_count")
if (
    database_callbacks[0] is not first_callback
    or database_callbacks[0] is not second_callback
):
    raise AssertionError("reload_callback_identity")
parent_epoch = runtime._current_database_process_epoch()
parent_lock = runtime._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK
reader, writer = make_pipe()
pid = None
reaped = False
try:
    pid = os.fork()
    if pid == 0:
        reader.close()
        child_runtime = None
        stage = "child_epoch"
        primary_error = None
        primary_stage = None
        cleanup_failures = []
        try:
            child_lock = runtime._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK
            fresh = runtime._current_database_process_epoch()
            child_runtime_epoch = None
            child_cleanup_complete = False
            stage = "fixture_import"
            from tests.durable_google_login_browser_test_support import (
                temporary_browser_login_state,
            )
            stage = "fixture_create"
            with temporary_browser_login_state() as state:
                stage = "runtime_build"
                child_runtime = runtime.build_durable_google_login_runtime(
                    state.configuration_path,
                    _clock=state.clock,
                    _gateway_factory=state.gateway_factory,
                )
                child_runtime_epoch = object.__getattribute__(
                    child_runtime,
                    "_process_epoch",
                )
                stage = "runtime_close"
                cleanup_report = child_runtime.close()
                child_cleanup_complete = (
                    type(
                        getattr(
                            cleanup_report,
                            "cleanup_complete",
                            None,
                        )
                    )
                    is bool
                    and cleanup_report.cleanup_complete is True
                )
                if not child_cleanup_complete:
                    raise AssertionError(
                        "reload_child_runtime_cleanup_incomplete"
                    )
                child_runtime = None
            stage = "assertions"
            outcome = {
                "callbacks_share_globals": all(
                    callback.__globals__ is runtime.__dict__
                    for callback in database_callbacks
                ),
                "registered_once": len(database_callbacks) == 1,
                "callback_preserved": (
                    first_callback is second_callback
                    and database_callbacks[0] is first_callback
                ),
                "callback_fifo": callback_execution == [0],
                "callback_locks_distinct": (
                    len(callback_locks) == 1
                    and callback_locks[0] is not parent_lock
                ),
                "sole_reset_lock_authoritative": (
                    len(callback_locks) == 1
                    and child_lock is callback_locks[0]
                ),
                "interposed_publication_once": (
                    len(publication_epochs) == 1
                    and len(publication_locks) == 1
                ),
                "interposed_publication_authoritative": (
                    len(publication_epochs) == 1
                    and publication_epochs[0] is fresh
                    and publication_epochs[0]
                    is runtime._DATABASE_PROCESS_EPOCH
                    and publication_locks[0] is child_lock
                ),
                "child_lock_replaced": child_lock is not parent_lock,
                "child_epoch_fresh": (
                    fresh is runtime._DATABASE_PROCESS_EPOCH
                    and fresh is not parent_epoch
                    and fresh.pid == os.getpid()
                ),
                "child_runtime_fresh": child_runtime_epoch is fresh,
                "child_runtime_cleanup": child_cleanup_complete,
                "hash_seed": os.environ.get("PYTHONHASHSEED"),
            }
            if not all(
                value
                for key, value in outcome.items()
                if key != "hash_seed"
            ):
                raise AssertionError("reload_child_invariant")
        except BaseException as error:
            primary_error = error
            primary_stage = stage
        finally:
            if child_runtime is not None:
                for attempt in range(2):
                    try:
                        cleanup_report = child_runtime.close(
                            _preserve_primary=True
                        )
                        cleanup_complete = (
                            type(
                                getattr(
                                    cleanup_report,
                                    "cleanup_complete",
                                    None,
                                )
                            )
                            is bool
                            and cleanup_report.cleanup_complete is True
                        )
                    except BaseException as cleanup_error:
                        cleanup_failures.append(
                            (
                                "runtime_cleanup_attempt_"
                                + str(attempt + 1),
                                cleanup_error,
                            )
                        )
                        continue
                    if cleanup_complete:
                        child_runtime = None
                        break
                    cleanup_failures.append(
                        (
                            "runtime_cleanup_attempt_"
                            + str(attempt + 1),
                            AssertionError(
                                "reload_child_runtime_cleanup_incomplete"
                            ),
                        )
                    )
        if primary_error is None and cleanup_failures:
            primary_stage, primary_error = cleanup_failures.pop(0)
        envelope = (
            {
                "ok": True,
                "stage": "complete",
                "outcome": outcome,
            }
            if primary_error is None
            else failure_envelope(
                primary_error,
                primary_stage,
                cleanup_failures,
            )
        )
        exit_code = 0 if primary_error is None else 1
        try:
            document = encode_envelope(envelope)
        except BaseException as serialization_error:
            exit_code = 2
            if primary_error is None:
                document = encode_envelope(
                    failure_envelope(
                        serialization_error,
                        "result_serialization",
                    )
                )
            else:
                document = encode_envelope(
                    failure_envelope(
                        primary_error,
                        primary_stage,
                        (
                            *cleanup_failures,
                            (
                                "result_serialization",
                                serialization_error,
                            ),
                        ),
                    )
                )
        try:
            offset = 0
            while offset < len(document):
                written = os.write(writer, document[offset:])
                if written <= 0:
                    raise OSError("reload_child_pipe_write_failed")
                offset += written
        except BaseException:
            exit_code = 2
        try:
            writer.close()
        except BaseException:
            exit_code = 2
        os._exit(exit_code)
    writer.close()
    writer = None
    document = read_pipe_bounded(reader, 30)
    reader.close()
    reader = None
    status = waitpid_bounded(pid, 10)
    reaped = True
    if type(status) is not int:
        raise AssertionError("reload_child_status_unavailable")
    exit_code = os.waitstatus_to_exitcode(status)
    if not document:
        raise AssertionError(
            f"reload_child_empty_result:exit={exit_code}"
        )
    try:
        success, envelope = decode_envelope(document)
    except BaseException as error:
        raise AssertionError(
            "reload_child_invalid_result:"
            f"exit={exit_code}:type={type(error).__name__}"
        ) from error
    if success is not True:
        raise AssertionError(
            "reload_child_failure:"
            f"exit={exit_code}:stage={envelope.get('stage')}:"
            f"type={envelope.get('type')}:"
            f"message={envelope.get('message')}:"
            f"traceback={envelope.get('traceback')}"
        )
    if exit_code != 0:
        raise AssertionError(
            f"reload_child_status:exit={exit_code}"
        )
    child = envelope
    parent = {
        "reload_epoch_replaced": parent_epoch is not pre_reload_epoch,
        "reload_lock_replaced": parent_lock is not pre_reload_lock,
        "parent_epoch_unchanged": (
            runtime._current_database_process_epoch() is parent_epoch
        ),
        "parent_lock_unchanged": (
            runtime._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK is parent_lock
        ),
    }
    os.write(
        1,
        json.dumps(
            {"child": child, "parent": parent},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii"),
    )
finally:
    _status, cleanup_failures = close_endpoints_and_reap(
        pid,
        reaped,
        (
            ("reader_close", reader),
            ("writer_close", writer),
        ),
    )
    preserve_primary_cleanup_failures(cleanup_failures)
"""
        result = self._run_isolated_python(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary_document = result.stdout.encode("utf-8")
        expected_seed = os.environ.get("PYTHONHASHSEED")
        child, parent = self._b21_decode_reload_summary(
            summary_document,
            expected_seed=expected_seed,
        )
        summary_value = self._b21_strict_json_value(summary_document)
        parent_fields = tuple(sorted(summary_value["parent"]))
        parent_cases = [
            (
                "valid_exact_schema",
                dict(summary_value["parent"]),
                None,
            ),
            ("empty", {}, "reload_summary_parent_shape"),
            (
                "whole_parent_wrong_type_list",
                [],
                "reload_summary_parent_shape",
            ),
            (
                "extra_key",
                {
                    **summary_value["parent"],
                    "unexpected": True,
                },
                "reload_summary_parent_shape",
            ),
        ]
        for field in parent_fields:
            parent_cases.extend(
                (
                    (
                        "missing_" + field,
                        {
                            name: value
                            for name, value in summary_value["parent"].items()
                            if name != field
                        },
                        "reload_summary_parent_shape",
                    ),
                    (
                        "wrong_value_type_" + field,
                        {
                            **summary_value["parent"],
                            field: "true",
                        },
                        "reload_summary_parent_shape",
                    ),
                    (
                        "integer_as_boolean_" + field,
                        {
                            **summary_value["parent"],
                            field: 1,
                        },
                        "reload_summary_parent_shape",
                    ),
                )
            )
        for case_name, candidate_parent, expected_error in parent_cases:
            candidate_document = json.dumps(
                {
                    **summary_value,
                    "parent": candidate_parent,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            with self.subTest(
                parent_case=case_name,
                parent_object=candidate_parent,
                parent_type=type(candidate_parent).__name__,
            ):
                if expected_error is None:
                    candidate_child, decoded_parent = (
                        self._b21_decode_reload_summary(
                            candidate_document,
                            expected_seed=expected_seed,
                        )
                    )
                    self.assertEqual(candidate_child, summary_value["child"])
                    self.assertEqual(decoded_parent, candidate_parent)
                else:
                    with self.assertRaises(AssertionError) as caught:
                        self._b21_decode_reload_summary(
                            candidate_document,
                            expected_seed=expected_seed,
                        )
                    self.assertEqual(str(caught.exception), expected_error)

        contradictory_field = "parent_epoch_unchanged"
        contradictory_parent_members = []
        for field in parent_fields:
            contradictory_parent_members.append(
                json.dumps(field).encode("ascii")
                + b":"
                + json.dumps(
                    summary_value["parent"][field],
                    separators=(",", ":"),
                ).encode("ascii")
            )
            if field == contradictory_field:
                contradictory_parent_members.append(
                    json.dumps(field).encode("ascii") + b":false"
                )
        contradictory_document = (
            b'{"child":'
            + json.dumps(
                summary_value["child"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            + b',"parent":{'
            + b",".join(contradictory_parent_members)
            + b"}}"
        )
        with self.subTest(
            parent_case="contradictory_duplicate_boolean",
            parent_object=(
                '{"parent_epoch_unchanged":true,'
                '"parent_epoch_unchanged":false}'
            ),
            parent_type="JSON object with duplicate Boolean member",
        ):
            with self.assertRaises(AssertionError) as caught:
                self._b21_decode_reload_summary(
                    contradictory_document,
                    expected_seed=expected_seed,
                )
            self.assertEqual(
                str(caught.exception),
                "b21_child_document_invalid",
            )

        child_cases = (
            (
                "extra_key",
                {
                    **summary_value["child"],
                    "failure": "contradictory",
                },
            ),
            (
                "integer_as_boolean",
                {
                    **summary_value["child"],
                    "child_runtime_cleanup": 1,
                },
            ),
        )
        for case_name, candidate_child in child_cases:
            with self.subTest(child_case=case_name):
                with self.assertRaises(AssertionError) as caught:
                    self._b21_decode_reload_summary(
                        json.dumps(
                            {
                                **summary_value,
                                "child": candidate_child,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("ascii"),
                        expected_seed=expected_seed,
                    )
                self.assertEqual(
                    str(caught.exception),
                    "reload_summary_child_shape",
                )
        self.assertTrue(
            all(
                value
                for name, value in child.items()
                if name != "hash_seed"
            ),
            summary_value,
        )
        self.assertTrue(all(parent.values()), summary_value)

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "fork"),
        "requires_posix_fork",
    )
    def test_b21_forked_child_rejects_pending_response_and_delivery_locks(
        self,
    ):
        import wahojobs.durable_google_login_runtime as runtime_module
        from tests.google_oidc_gateway_test_support import (
            ManualClock,
            NOW,
            make_real_gateway,
        )

        inherited_outcome_types = {
            name: bool
            for name in (
                "response_rejected",
                "delivery_rejected",
                "lease_rejected",
                "lease_close_rejected",
                "manager_rejected",
                "cleanup_claim_rejected",
                "cleanup_finish_rejected",
                "cleanup_abandon_rejected",
                "cleanup_relinquish_rejected",
                "cleanup_probe_rejected",
                "cleanup_ack_rejected",
                "owner_reclaim_returned_false",
                "owner_lock_inherited_held",
                "fresh_epoch",
                "response_lease_unchanged",
                "response_owner_unchanged",
                "response_release_unchanged",
                "response_state_unchanged",
                "raw_state_unchanged",
                "borrower_unchanged",
                "record_state_unchanged",
                "cleanup_delegate_unchanged",
                "cleanup_identity_unchanged",
                "cleanup_capability_unchanged",
                "cleanup_mode_unchanged",
                "connection_identity_unchanged",
            )
        }
        pending_fresh_outcome_types = {
            "fresh_runtime": bool,
            "provider_cleanup_complete": bool,
        }

        with (
            temporary_browser_login_state() as state,
            temporary_browser_login_state(port=8444) as fresh_state,
        ):
            runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            response = None
            phase_one_reader = None
            phase_one_writer = None
            phase_two_reader = None
            phase_two_writer = None
            control_reader = None
            control_writer = None
            pid = None
            reaped = False
            acquired = []
            try:
                response = self._pending_delivery_response(
                    runtime,
                    state,
                )
                wrapper = response._delivery_lease
                owner = response._owned_connection
                manager = object.__getattribute__(
                    runtime,
                    "_connections",
                )
                raw = object.__getattribute__(
                    wrapper,
                    (
                        "_AuthorityFencedSessionDeliveryLease"
                        "__lease"
                    ),
                )
                parent_epoch = object.__getattribute__(
                    manager,
                    "_process_epoch",
                )
                request_release = response._request_release
                delivery_owner = response._delivery_owner
                record = object.__getattribute__(owner, "_record")
                borrower_token = object.__getattribute__(owner, "_token")
                record_state = object.__getattribute__(record, "_state")
                cleanup_delegate = object.__getattribute__(
                    record,
                    "_browser_cleanup_delegate",
                )
                cleanup_identity = object.__getattribute__(
                    record,
                    "_browser_cleanup_identity",
                )
                cleanup_capability = object.__getattribute__(
                    record,
                    "_browser_cleanup_capability",
                )
                cleanup_mode = object.__getattribute__(
                    record,
                    "_browser_cleanup_mode",
                )
                connection_identity = object.__getattribute__(
                    record,
                    "_connection_identity",
                )
                raw_status = object.__getattribute__(raw, "_status")
                response_lock = object.__getattribute__(
                    response,
                    "_delivery_lock",
                )
                owner_lock = object.__getattribute__(
                    delivery_owner,
                    "_BrowserRequestDeliveryOwner__lock",
                )
                self.assertIs(response_lock, owner_lock)
                raw_lock = object.__getattribute__(raw, "_lock")
                manager_condition = object.__getattribute__(
                    manager,
                    "_condition",
                )
                epoch_lock = (
                    runtime_module
                    ._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK
                )
                for lock in (
                    owner_lock,
                    raw_lock,
                    manager_condition,
                    epoch_lock,
                ):
                    lock.acquire()
                    acquired.append(lock)

                phase_one_reader, phase_one_writer = self._b21_pipe()
                phase_two_reader, phase_two_writer = self._b21_pipe()
                control_reader, control_writer = self._b21_pipe()
                pid = os.fork()
                if pid == 0:
                    phase_one_reader.close()
                    phase_two_reader.close()
                    control_writer.close()
                    phase_one_sent = False
                    inherited_outcome = {
                        "response_rejected": False,
                        "delivery_rejected": False,
                        "lease_rejected": False,
                        "lease_close_rejected": False,
                        "manager_rejected": False,
                        "cleanup_claim_rejected": False,
                        "cleanup_finish_rejected": False,
                        "cleanup_abandon_rejected": False,
                        "cleanup_relinquish_rejected": False,
                        "cleanup_probe_rejected": False,
                        "cleanup_ack_rejected": False,
                        "owner_reclaim_returned_false": False,
                        "owner_lock_inherited_held": owner_lock.locked(),
                        "fresh_epoch": False,
                        "response_lease_unchanged": False,
                        "response_owner_unchanged": False,
                        "response_release_unchanged": False,
                        "response_state_unchanged": False,
                        "raw_state_unchanged": False,
                        "borrower_unchanged": False,
                        "record_state_unchanged": False,
                        "cleanup_delegate_unchanged": False,
                        "cleanup_identity_unchanged": False,
                        "cleanup_capability_unchanged": False,
                        "cleanup_mode_unchanged": False,
                        "connection_identity_unchanged": False,
                    }
                    child_runtime = None
                    child_lease = None
                    child_harnesses = []
                    stage = "inherited_authority"
                    primary_error = None
                    primary_stage = None
                    cleanup_failures = []
                    success_envelope = None
                    try:
                        for name, operation in (
                            (
                                "response_rejected",
                                response.acknowledge_delivery,
                            ),
                            (
                                "delivery_rejected",
                                wrapper.fail_delivery,
                            ),
                            (
                                "lease_rejected",
                                lambda: owner.execute("SELECT 1"),
                            ),
                            (
                                "lease_close_rejected",
                                owner.close,
                            ),
                            (
                                "manager_rejected",
                                manager.close,
                            ),
                            (
                                "cleanup_claim_rejected",
                                lambda: owner
                                ._wahojobs_claim_abandoned_browser_cleanup(
                                    cleanup_delegate,
                                    cleanup_identity,
                                    cleanup_capability,
                                    connection_identity,
                                ),
                            ),
                            (
                                "cleanup_finish_rejected",
                                lambda: owner
                                ._wahojobs_finish_abandoned_browser_cleanup(
                                    cleanup_delegate,
                                    cleanup_identity,
                                    cleanup_capability,
                                    object(),
                                ),
                            ),
                            (
                                "cleanup_abandon_rejected",
                                lambda: owner
                                ._wahojobs_abandon_abandoned_browser_cleanup(
                                    cleanup_delegate,
                                    cleanup_identity,
                                    cleanup_capability,
                                ),
                            ),
                            (
                                "cleanup_relinquish_rejected",
                                lambda: owner
                                ._wahojobs_relinquish_unregistered_browser_cleanup(
                                    cleanup_delegate,
                                    cleanup_identity,
                                    cleanup_capability,
                                ),
                            ),
                            (
                                "cleanup_probe_rejected",
                                lambda: owner
                                ._wahojobs_browser_cleanup_is_closed(
                                    cleanup_identity,
                                    cleanup_capability,
                                ),
                            ),
                            (
                                "cleanup_ack_rejected",
                                lambda: owner
                                ._wahojobs_acknowledge_abandoned_browser_cleanup(
                                    cleanup_delegate,
                                    cleanup_identity,
                                    cleanup_capability,
                                ),
                            ),
                        ):
                            try:
                                operation()
                            except DurableGoogleLoginConfigurationError:
                                inherited_outcome[name] = True
                        inherited_outcome[
                            "owner_reclaim_returned_false"
                        ] = (
                            delivery_owner._reclaim_abandoned_request()
                            is False
                        )

                        child_epoch = (
                            runtime_module
                            ._current_database_process_epoch()
                        )
                        inherited_outcome["fresh_epoch"] = (
                            child_epoch is not parent_epoch
                            and child_epoch.pid == os.getpid()
                        )
                        inherited_outcome[
                            "response_lease_unchanged"
                        ] = (response._delivery_lease is wrapper)
                        inherited_outcome[
                            "response_owner_unchanged"
                        ] = (response._owned_connection is owner)
                        inherited_outcome[
                            "response_release_unchanged"
                        ] = (response._request_release is request_release)
                        inherited_outcome[
                            "response_state_unchanged"
                        ] = (response._delivery_state == ["pending"])
                        inherited_outcome["raw_state_unchanged"] = (
                            object.__getattribute__(raw, "_status")
                            == raw_status
                        )
                        inherited_outcome["borrower_unchanged"] = (
                            object.__getattribute__(
                                record,
                                "_borrower_token",
                            )
                            is borrower_token
                        )
                        inherited_outcome["record_state_unchanged"] = (
                            object.__getattribute__(record, "_state")
                            == record_state
                        )
                        inherited_outcome[
                            "cleanup_delegate_unchanged"
                        ] = (
                            object.__getattribute__(
                                record,
                                "_browser_cleanup_delegate",
                            )
                            is cleanup_delegate
                        )
                        inherited_outcome[
                            "cleanup_identity_unchanged"
                        ] = (
                            object.__getattribute__(
                                record,
                                "_browser_cleanup_identity",
                            )
                            is cleanup_identity
                        )
                        inherited_outcome[
                            "cleanup_capability_unchanged"
                        ] = (
                            object.__getattribute__(
                                record,
                                "_browser_cleanup_capability",
                            )
                            is cleanup_capability
                        )
                        inherited_outcome["cleanup_mode_unchanged"] = (
                            object.__getattribute__(
                                record,
                                "_browser_cleanup_mode",
                            )
                            == cleanup_mode
                        )
                        inherited_outcome[
                            "connection_identity_unchanged"
                        ] = (
                            object.__getattribute__(
                                record,
                                "_connection_identity",
                            )
                            is connection_identity
                        )
                        stage = "inherited_outcome_publication"
                        inherited_document = json.dumps(
                            inherited_outcome,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("ascii")
                        inherited_offset = 0
                        while inherited_offset < len(inherited_document):
                            written = os.write(
                                phase_one_writer,
                                inherited_document[inherited_offset:],
                            )
                            if written <= 0:
                                raise OSError(
                                    "inherited_outcome_pipe_write_failed"
                                )
                            inherited_offset += written
                        phase_one_sent = True
                        phase_one_writer.close()
                        phase_one_writer = None
                        stage = "control_wait"
                        if os.read(control_reader, 1) != b"c":
                            raise AssertionError(
                                "b21_fork_control_missing"
                            )

                        child_clock = ManualClock(NOW)

                        def child_gateway_factory(
                            configuration,
                            client_secret,
                        ):
                            harness = make_real_gateway(
                                clock=child_clock,
                                client_id=(
                                    configuration.google_client_id
                                ),
                                client_secret=client_secret,
                                redirect_uri=(
                                    configuration.google_redirect_uri
                                ),
                                subject=fresh_state.subject,
                            )
                            child_harnesses.append(harness)
                            return harness.gateway

                        stage = "fresh_runtime_build"
                        child_runtime = (
                            build_durable_google_login_runtime(
                                fresh_state.configuration_path,
                                _clock=child_clock,
                                _gateway_factory=(
                                    child_gateway_factory
                                ),
                            )
                        )
                        stage = "fresh_lease_open"
                        child_lease = (
                            child_runtime.open_writable_connection()
                        )
                        child_cursor = child_lease.execute("SELECT 1")
                        child_result = tuple(child_cursor.fetchone())
                        child_cursor.close()
                        if child_result != (1,):
                            raise AssertionError(
                                "pending_fresh_child_result"
                            )
                        stage = "fresh_lease_close"
                        lease_to_close = child_lease
                        lease_to_close.close()
                        child_lease = None
                        stage = "fresh_runtime_close"
                        runtime_to_close = child_runtime
                        cleanup_report = runtime_to_close.close()
                        cleanup_complete = (
                            type(
                                getattr(
                                    cleanup_report,
                                    "cleanup_complete",
                                    None,
                                )
                            )
                            is bool
                            and cleanup_report.cleanup_complete is True
                        )
                        if not cleanup_complete:
                            raise AssertionError(
                                "pending_fresh_runtime_cleanup_incomplete"
                            )
                        child_runtime = None
                        fresh_outcome = {
                            "fresh_runtime": True,
                            "provider_cleanup_complete": False,
                        }
                        stage = "provider_harness_cleanup"
                        success_envelope = (
                            self._b21_success_after_harness_cleanup(
                                fresh_outcome,
                                child_harnesses,
                            )
                        )
                    except BaseException as error:
                        primary_error = error
                        primary_stage = stage
                    finally:
                        if child_lease is not None:
                            lease_to_close = child_lease
                            try:
                                lease_to_close.close()
                            except BaseException as cleanup_error:
                                cleanup_failures.append(
                                    (
                                        "fresh_lease_cleanup",
                                        cleanup_error,
                                    )
                                )
                            else:
                                child_lease = None
                        if child_runtime is not None:
                            (
                                child_runtime,
                                runtime_cleanup_failures,
                            ) = self._b21_close_runtime_owner(
                                child_runtime,
                                stage="fresh_runtime_cleanup",
                            )
                            cleanup_failures.extend(
                                runtime_cleanup_failures
                            )
                        if child_harnesses:
                            try:
                                self._b21_success_after_harness_cleanup(
                                    {},
                                    child_harnesses,
                                )
                            except BaseException as cleanup_error:
                                cleanup_failures.append(
                                    (
                                        "provider_harness_cleanup",
                                        cleanup_error,
                                    )
                                )
                    if primary_error is None and cleanup_failures:
                        (
                            primary_stage,
                            primary_error,
                        ) = cleanup_failures.pop(0)
                    if primary_error is None:
                        if success_envelope is None:
                            primary_error = AssertionError(
                                "pending_fresh_child_success_unpublished"
                            )
                            primary_stage = "success_publication"
                            envelope = self._b21_child_failure_envelope(
                                primary_error,
                                stage=primary_stage,
                            )
                            exit_code = 1
                        else:
                            envelope = success_envelope
                            exit_code = 0
                    else:
                        envelope = self._b21_child_failure_envelope(
                            primary_error,
                            stage=primary_stage,
                            cleanup_failures=cleanup_failures,
                        )
                        exit_code = 1
                    try:
                        if phase_one_sent:
                            try:
                                document = self._b21_encode_child_envelope(
                                    envelope
                                )
                            except BaseException as serialization_error:
                                exit_code = 2
                                if primary_error is None:
                                    document = (
                                        self._b21_encode_child_envelope(
                                            self._b21_child_failure_envelope(
                                                serialization_error,
                                                stage=(
                                                    "result_serialization"
                                                ),
                                            )
                                        )
                                    )
                                else:
                                    document = (
                                        self._b21_encode_child_envelope(
                                            self._b21_child_failure_envelope(
                                                primary_error,
                                                stage=primary_stage,
                                                cleanup_failures=(
                                                    *cleanup_failures,
                                                    (
                                                        (
                                                            "result_"
                                                            "serialization"
                                                        ),
                                                        serialization_error,
                                                    ),
                                                ),
                                            )
                                        )
                                    )
                            offset = 0
                            while offset < len(document):
                                written = os.write(
                                    phase_two_writer,
                                    document[offset:],
                                )
                                if written <= 0:
                                    raise OSError(
                                        "pending_child_pipe_write_failed"
                                    )
                                offset += written
                        elif phase_one_writer is not None:
                            os.write(phase_one_writer, b"failure")
                    except BaseException:
                        exit_code = 2
                    for descriptor_name in (
                        "phase_one_writer",
                        "phase_two_writer",
                        "control_reader",
                    ):
                        descriptor = locals()[descriptor_name]
                        if descriptor is not None:
                            try:
                                descriptor.close()
                            except BaseException:
                                exit_code = 2
                    os._exit(exit_code)

                phase_one_writer.close()
                phase_one_writer = None
                phase_two_writer.close()
                phase_two_writer = None
                control_reader.close()
                control_reader = None
                document = self._b21_read_pipe_bounded(
                    phase_one_reader,
                    timeout=15,
                )
                phase_one_reader.close()
                phase_one_reader = None
                self.assertNotEqual(document, b"failure")
                inherited_outcome = self._b21_decode_exact_json_object(
                    document,
                    field_types=inherited_outcome_types,
                )
                self.assertTrue(
                    all(inherited_outcome.values()),
                    inherited_outcome,
                )

                for lock in reversed(acquired):
                    lock.release()
                acquired.clear()
                response.fail_delivery()
                response = None
                self.assertEqual(
                    object.__getattribute__(manager, "_records"),
                    {},
                )
                os.write(control_writer, b"c")
                control_writer.close()
                control_writer = None
                document = self._b21_read_pipe_bounded(
                    phase_two_reader,
                    timeout=15,
                )
                phase_two_reader.close()
                phase_two_reader = None
                status = self._b21_waitpid_bounded(pid, timeout=10)
                reaped = True
                self.assertIs(type(status), int)
                exit_code = os.waitstatus_to_exitcode(status)
                success, fresh_outcome = (
                    self._b21_decode_child_envelope(
                        document,
                        outcome_types=pending_fresh_outcome_types,
                    )
                )
                if success is not True:
                    self.fail(
                        "pending_fresh_child_failure:"
                        f"exit={exit_code}:"
                        f"stage={fresh_outcome['stage']}:"
                        f"type={fresh_outcome['type']}:"
                        f"message={fresh_outcome['message']}:"
                        f"traceback={fresh_outcome['traceback']}"
                    )
                self.assertEqual(exit_code, 0)
                self.assertEqual(
                    fresh_outcome,
                    {
                        "fresh_runtime": True,
                        "provider_cleanup_complete": True,
                    },
                )
            finally:
                cleanup_failures = []
                for lock in reversed(acquired):
                    try:
                        lock.release()
                    except BaseException as cleanup_error:
                        cleanup_failures.append(
                            ("inherited_lock_release", cleanup_error)
                        )
                _status, endpoint_failures = (
                    self._b21_close_endpoints_and_reap(
                        pid=pid,
                        reaped=reaped,
                        endpoints=(
                            ("phase_one_writer_close", phase_one_writer),
                            ("phase_one_reader_close", phase_one_reader),
                            ("phase_two_writer_close", phase_two_writer),
                            ("phase_two_reader_close", phase_two_reader),
                            ("control_writer_close", control_writer),
                            ("control_reader_close", control_reader),
                        ),
                    )
                )
                cleanup_failures.extend(endpoint_failures)
                if response is not None:
                    try:
                        response.fail_delivery()
                    except BaseException as cleanup_error:
                        cleanup_failures.append(
                            ("response_cleanup", cleanup_error)
                        )
                runtime, runtime_failures = (
                    self._b21_close_runtime_owner(
                        runtime,
                        stage="parent_runtime_cleanup",
                    )
                )
                cleanup_failures.extend(runtime_failures)
                self._b21_preserve_primary_cleanup_failures(
                    cleanup_failures
                )

    @unittest.skipUnless(
        os.name == "posix" and hasattr(os, "fork"),
        "requires_posix_fork",
    )
    def test_b21_forked_child_rejects_inherited_authority_then_builds_fresh(
        self,
    ):
        import wahojobs.durable_google_login_runtime as runtime_module
        from tests.google_oidc_gateway_test_support import (
            ManualClock,
            NOW,
            make_real_gateway,
        )

        self._b21_assert_success_requires_child_exit()

        lost_status_signals = []
        with mock.patch.object(
            os,
            "waitpid",
            side_effect=ChildProcessError(
                "b21_child_already_reaped"
            ),
        ), mock.patch.object(
            os,
            "kill",
            side_effect=lambda pid, number: (
                lost_status_signals.append((pid, number))
            ),
        ):
            lost_status, lost_status_failures = (
                self._b21_close_endpoints_and_reap(
                    pid=770,
                    reaped=False,
                    endpoints=(),
                )
            )
        self.assertIs(
            lost_status,
            self._B21_REAPED_STATUS_UNKNOWN,
        )
        self.assertEqual(lost_status_signals, [])
        self.assertEqual(
            tuple(stage for stage, _error in lost_status_failures),
            ("child_reap_status_unavailable",),
        )
        self.assertIs(
            type(lost_status_failures[0][1]),
            ChildProcessError,
        )

        call_order = []
        waitpid_results = iter(((0, 0), (0, 0)))

        def ordered_waitpid(pid, options):
            call_order.append(("probe", pid, options))
            return next(waitpid_results)

        def ordered_kill(pid, number):
            call_order.append(("signal", pid, number))

        wait_until_results = iter((17,))

        def ordered_wait_until(pid, *, deadline):
            call_order.append(("wait", pid, deadline))
            result = next(wait_until_results)
            if isinstance(result, BaseException):
                raise result
            return result

        with mock.patch.object(
            os,
            "waitpid",
            side_effect=ordered_waitpid,
        ), mock.patch.object(
            os,
            "kill",
            side_effect=ordered_kill,
        ), mock.patch.object(
            type(self),
            "_b21_waitpid_until",
            side_effect=ordered_wait_until,
        ), mock.patch.object(
            time,
            "monotonic",
            side_effect=(0.0,),
        ):
            self.assertEqual(
                self._b21_terminate_and_reap(771, timeout=10),
                17,
            )
        self.assertEqual(
            [
                item[:3]
                for item in call_order
                if item[0] in {"probe", "signal"}
            ],
            [
                ("probe", 771, os.WNOHANG),
                ("signal", 771, signal.SIGTERM),
                ("probe", 771, os.WNOHANG),
                ("signal", 771, signal.SIGKILL),
            ],
        )
        self.assertEqual(
            [item for item in call_order if item[0] == "wait"],
            [("wait", 771, 10.0)],
        )

        exit_between_probe_and_signal = []

        def exited_kill(pid, number):
            exit_between_probe_and_signal.append(
                ("signal", pid, number)
            )
            raise ProcessLookupError

        with mock.patch.object(
            os,
            "waitpid",
            side_effect=((0, 0), (774, 19)),
        ) as waitpid_probe, mock.patch.object(
            os,
            "kill",
            side_effect=exited_kill,
        ), mock.patch.object(
            type(self),
            "_b21_waitpid_until",
            return_value=19,
        ) as bounded_wait, mock.patch.object(
            time,
            "monotonic",
            side_effect=(0.0,),
        ):
            self.assertEqual(
                self._b21_terminate_and_reap(774, timeout=10),
                19,
            )
        self.assertEqual(
            waitpid_probe.call_args_list,
            [
                mock.call(774, os.WNOHANG),
                mock.call(774, os.WNOHANG),
            ],
        )
        bounded_wait.assert_not_called()
        self.assertEqual(
            exit_between_probe_and_signal,
            [("signal", 774, signal.SIGTERM)],
        )

        interrupted_clock = iter((0.0, 0.1, 0.2, 1.0))
        with mock.patch.object(
            os,
            "waitpid",
            side_effect=InterruptedError,
        ), mock.patch.object(
            time,
            "monotonic",
            side_effect=lambda: next(interrupted_clock),
        ):
            with self.assertRaisesRegex(
                TimeoutError,
                "b21_child_reap_timeout",
            ):
                self._b21_waitpid_bounded(772, timeout=0.5)

        for close_error_type in (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            close_error = close_error_type(
                "b21_endpoint_close_failure"
            )
            reap_calls = []
            first_endpoint, second_endpoint = self._b21_pipe()
            endpoint_close = type(first_endpoint).close
            first_close_attempted = False

            def interrupt_first_endpoint(endpoint):
                nonlocal first_close_attempted
                if endpoint is first_endpoint and not first_close_attempted:
                    first_close_attempted = True
                    raise close_error
                return endpoint_close(endpoint)

            with self.subTest(endpoint_error=close_error_type.__name__), (
                mock.patch.object(
                    type(first_endpoint),
                    "close",
                    new=interrupt_first_endpoint,
                )
            ), mock.patch.object(
                type(self),
                "_b21_terminate_and_reap",
                side_effect=lambda pid, deadline=None, timeout=10: (
                    reap_calls.append((pid, deadline, timeout)) or 23
                ),
            ):
                status, cleanup_failures = (
                    self._b21_close_endpoints_and_reap(
                        pid=773,
                        reaped=False,
                        endpoints=(
                            ("first_endpoint", first_endpoint),
                            ("second_endpoint", second_endpoint),
                        ),
                    )
                )
            self.assertTrue(first_endpoint.closed)
            self.assertTrue(second_endpoint.closed)
            self.assertEqual(status, 23)
            self.assertEqual(len(reap_calls), 1)
            self.assertEqual(reap_calls[0][0], 773)
            self.assertIs(type(reap_calls[0][1]), float)
            self.assertEqual(reap_calls[0][2], 10)
            self.assertEqual(
                cleanup_failures,
                (("first_endpoint_attempt_1", close_error),),
            )

        first_close_error = RuntimeError(
            "b21_first_endpoint_close_failure"
        )
        second_close_error = GeneratorExit(
            "b21_second_endpoint_close_failure"
        )
        repeated_reap_calls = []
        first_endpoint, second_endpoint = self._b21_pipe()
        endpoint_close = type(first_endpoint).close
        interrupted_endpoints = set()

        def interrupt_each_endpoint(endpoint):
            if endpoint.identity not in interrupted_endpoints:
                interrupted_endpoints.add(endpoint.identity)
                if endpoint is first_endpoint:
                    raise first_close_error
                if endpoint is second_endpoint:
                    endpoint_close(endpoint)
                    raise second_close_error
            return endpoint_close(endpoint)

        with mock.patch.object(
            type(first_endpoint),
            "close",
            new=interrupt_each_endpoint,
        ), mock.patch.object(
            type(self),
            "_b21_terminate_and_reap",
            side_effect=lambda pid, deadline=None, timeout=10: (
                repeated_reap_calls.append((pid, deadline, timeout)) or 29
            ),
        ):
            status, cleanup_failures = (
                self._b21_close_endpoints_and_reap(
                    pid=775,
                    reaped=False,
                    endpoints=(
                        ("first_endpoint", first_endpoint),
                        ("second_endpoint", second_endpoint),
                    ),
                )
            )
        self.assertTrue(first_endpoint.closed)
        self.assertTrue(second_endpoint.closed)
        self.assertEqual(status, 29)
        self.assertEqual(len(repeated_reap_calls), 1)
        self.assertEqual(repeated_reap_calls[0][0], 775)
        self.assertIs(type(repeated_reap_calls[0][1]), float)
        self.assertEqual(repeated_reap_calls[0][2], 10)
        self.assertEqual(
            cleanup_failures,
            (
                ("first_endpoint_attempt_1", first_close_error),
                ("second_endpoint_attempt_1", second_close_error),
            ),
        )

        real_reader, real_writer = self._b21_pipe()
        real_pid = os.fork()
        if real_pid == 0:
            try:
                real_reader.close()
                real_writer.close()
                signal.pause()
            finally:
                os._exit(125)
        real_status = None
        real_endpoint_close = type(real_reader).close
        real_close_interrupted = False

        def interrupt_real_endpoint_once(endpoint):
            nonlocal real_close_interrupted
            if endpoint is real_reader and not real_close_interrupted:
                real_close_interrupted = True
                raise GeneratorExit(
                    "b21_real_endpoint_close_interrupted"
                )
            return real_endpoint_close(endpoint)

        try:
            with mock.patch.object(
                type(real_reader),
                "close",
                new=interrupt_real_endpoint_once,
            ):
                real_status, real_cleanup_failures = (
                    self._b21_close_endpoints_and_reap(
                        pid=real_pid,
                        reaped=False,
                        endpoints=(
                            ("real_reader", real_reader),
                            ("real_writer", real_writer),
                        ),
                    )
                )
            self.assertTrue(real_close_interrupted)
            self.assertTrue(real_reader.closed)
            self.assertTrue(real_writer.closed)
            self.assertIs(type(real_status), int)
            self.assertTrue(
                os.WIFSIGNALED(real_status)
                or os.WIFEXITED(real_status)
            )
            self.assertEqual(
                tuple(
                    stage
                    for stage, _error in real_cleanup_failures
                ),
                ("real_reader_attempt_1",),
            )
            with self.assertRaises(ChildProcessError):
                os.waitpid(real_pid, os.WNOHANG)
        finally:
            if real_status is None:
                self._b21_close_endpoints_and_reap(
                    pid=real_pid,
                    reaped=False,
                    endpoints=(
                        ("real_reader_final", real_reader),
                        ("real_writer_final", real_writer),
                    ),
                )

        wait_reader, wait_writer = self._b21_pipe()
        wait_pid = os.fork()
        if wait_pid == 0:
            try:
                wait_reader.close()
                wait_writer.close()
                signal.pause()
            finally:
                os._exit(126)
        wait_status = None
        wait_calls = []
        original_waitpid = os.waitpid
        remaining_interruptions = 32

        def interrupt_real_waitpid_exactly(pid, options):
            nonlocal remaining_interruptions
            wait_calls.append((pid, options))
            if remaining_interruptions:
                remaining_interruptions -= 1
                raise InterruptedError(
                    "b21_real_exact_pid_wait_interrupted"
                )
            return original_waitpid(pid, options)

        try:
            with mock.patch.object(
                os,
                "waitpid",
                side_effect=interrupt_real_waitpid_exactly,
            ):
                wait_status, wait_cleanup_failures = (
                    self._b21_close_endpoints_and_reap(
                        pid=wait_pid,
                        reaped=False,
                        endpoints=(
                            ("wait_reader", wait_reader),
                            ("wait_writer", wait_writer),
                        ),
                    )
                )
            self.assertEqual(remaining_interruptions, 0)
            self.assertTrue(wait_reader.closed)
            self.assertTrue(wait_writer.closed)
            self.assertIs(type(wait_status), int)
            self.assertEqual(wait_cleanup_failures, ())
            self.assertGreater(len(wait_calls), 32)
            self.assertTrue(
                all(
                    pid == wait_pid and options == os.WNOHANG
                    for pid, options in wait_calls
                )
            )
            with self.assertRaises(ChildProcessError):
                original_waitpid(wait_pid, os.WNOHANG)
        finally:
            if wait_status is None:
                self._b21_close_endpoints_and_reap(
                    pid=wait_pid,
                    reaped=False,
                    endpoints=(
                        ("wait_reader_final", wait_reader),
                        ("wait_writer_final", wait_writer),
                    ),
                )

        for reap_error_type in (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            reap_error = reap_error_type(
                "b21_reap_boundary_failure"
            )
            with self.subTest(
                reap_error=reap_error_type.__name__
            ), mock.patch.object(
                type(self),
                "_b21_terminate_and_reap",
                side_effect=(reap_error, 31),
            ) as bounded_reap:
                status, cleanup_failures = (
                    self._b21_close_endpoints_and_reap(
                        pid=776,
                        reaped=False,
                        endpoints=(),
                    )
                )
            self.assertEqual(status, 31)
            self.assertEqual(bounded_reap.call_count, 2)
            reap_deadlines = tuple(
                call.kwargs["deadline"]
                for call in bounded_reap.call_args_list
            )
            self.assertEqual(
                reap_deadlines,
                (reap_deadlines[0], reap_deadlines[0]),
            )
            self.assertEqual(
                cleanup_failures,
                (("child_reap_attempt_1", reap_error),),
            )

        persistent_reap_failures = (
            RuntimeError("b21_reap_runtime"),
            GeneratorExit("b21_reap_generator"),
        )
        final_reap_order = []

        def final_probe(pid, *, deadline):
            final_reap_order.append(("probe", pid, deadline))
            return False, None

        def final_kill(pid, number):
            final_reap_order.append(("signal", pid, number))

        def final_wait(pid, *, deadline):
            final_reap_order.append(("wait", pid, deadline))
            return 41

        with mock.patch.object(
            type(self),
            "_b21_terminate_and_reap",
            side_effect=persistent_reap_failures,
        ) as bounded_reap, mock.patch.object(
            type(self),
            "_b21_probe_exact_child",
            side_effect=final_probe,
        ), mock.patch.object(
            os,
            "kill",
            side_effect=final_kill,
        ), mock.patch.object(
            type(self),
            "_b21_waitpid_until",
            side_effect=final_wait,
        ):
            status, cleanup_failures = (
                self._b21_close_endpoints_and_reap(
                    pid=779,
                    reaped=False,
                    endpoints=(),
                )
            )
        self.assertEqual(status, 41)
        self.assertEqual(bounded_reap.call_count, 2)
        attempt_deadlines = tuple(
            call.kwargs["deadline"]
            for call in bounded_reap.call_args_list
        )
        self.assertEqual(
            attempt_deadlines,
            (attempt_deadlines[0], attempt_deadlines[0]),
        )
        self.assertEqual(
            [item[:2] for item in final_reap_order],
            [
                ("probe", 779),
                ("signal", 779),
                ("probe", 779),
                ("signal", 779),
                ("wait", 779),
            ],
        )
        self.assertTrue(
            all(
                item[2] == attempt_deadlines[0]
                for item in (
                    final_reap_order[0],
                    final_reap_order[2],
                    final_reap_order[4],
                )
            )
        )
        self.assertEqual(
            final_reap_order[1][2],
            signal.SIGTERM,
        )
        self.assertEqual(
            final_reap_order[3][2],
            signal.SIGKILL,
        )
        self.assertEqual(
            cleanup_failures,
            (
                ("child_reap_attempt_1", persistent_reap_failures[0]),
                ("child_reap_attempt_2", persistent_reap_failures[1]),
            ),
        )

        primary_failure = RuntimeError("b21_primary_failure")
        supplemental_failure = GeneratorExit(
            "b21_supplemental_cleanup_failure"
        )
        with self.assertRaises(RuntimeError) as caught:
            try:
                raise primary_failure
            except BaseException:
                self._b21_preserve_primary_cleanup_failures(
                    (("supplemental_cleanup", supplemental_failure),)
                )
                raise
        self.assertIs(caught.exception, primary_failure)
        self.assertTrue(
            any(
                "b21_cleanup_failure:supplemental_cleanup:GeneratorExit"
                in note
                for note in getattr(primary_failure, "__notes__", ())
            )
        )
        first_cleanup_failure = RuntimeError(
            "b21_first_cleanup_failure"
        )
        later_cleanup_failure = KeyboardInterrupt(
            "b21_later_cleanup_failure"
        )
        with self.assertRaises(RuntimeError) as caught:
            self._b21_preserve_primary_cleanup_failures(
                (
                    ("first_cleanup", first_cleanup_failure),
                    ("later_cleanup", later_cleanup_failure),
                )
            )
        self.assertIs(caught.exception, first_cleanup_failure)
        self.assertTrue(
            any(
                "b21_cleanup_failure:later_cleanup:KeyboardInterrupt"
                in note
                for note in getattr(
                    first_cleanup_failure,
                    "__notes__",
                    (),
                )
            )
        )

        class IncompleteCleanupReport:
            cleanup_complete = False

        class CompleteCleanupReport:
            cleanup_complete = True

        class RetryableRuntime:
            def __init__(self, outcomes):
                self.outcomes = list(outcomes)
                self.calls = 0

            def close(self, *, _preserve_primary):
                self.calls += 1
                outcome = self.outcomes.pop(0)
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

        retryable_runtime = RetryableRuntime(
            (IncompleteCleanupReport(), CompleteCleanupReport())
        )
        retained_runtime, cleanup_failures = (
            self._b21_close_runtime_owner(
                retryable_runtime,
                stage="runtime_probe",
            )
        )
        self.assertIsNone(retained_runtime)
        self.assertEqual(retryable_runtime.calls, 2)
        self.assertEqual(len(cleanup_failures), 1)
        unresolved_runtime = RetryableRuntime(
            (IncompleteCleanupReport(), IncompleteCleanupReport())
        )
        retained_runtime, cleanup_failures = (
            self._b21_close_runtime_owner(
                unresolved_runtime,
                stage="runtime_probe",
            )
        )
        self.assertIs(retained_runtime, unresolved_runtime)
        self.assertEqual(len(cleanup_failures), 2)

        for cleanup_error_type in (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            cleanup_error = cleanup_error_type(
                "b21_runtime_cleanup_failure"
            )
            retryable_runtime = RetryableRuntime(
                (cleanup_error, CompleteCleanupReport())
            )
            with self.subTest(
                runtime_cleanup_error=cleanup_error_type.__name__
            ):
                retained_runtime, cleanup_failures = (
                    self._b21_close_runtime_owner(
                        retryable_runtime,
                        stage="runtime_probe",
                    )
                )
                self.assertIsNone(retained_runtime)
                self.assertEqual(
                    cleanup_failures,
                    (("runtime_probe_attempt_1", cleanup_error),),
                )

        for provider_error_type in (
            RuntimeError,
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            provider_error = provider_error_type(
                "b21_provider_harness_close_failure"
            )
            harness = make_real_gateway(clock=ManualClock(NOW))
            harnesses = [harness]
            transport_type = type(harness.transport)
            original_transport_close = transport_type.close
            transport_close_calls = []

            def close_transport_then_fail(transport):
                transport_close_calls.append(transport)
                original_transport_close(transport)
                raise provider_error

            success_envelope = None
            with self.subTest(
                provider_cleanup_error=provider_error_type.__name__
            ), mock.patch.object(
                transport_type,
                "close",
                new=close_transport_then_fail,
            ):
                with self.assertRaises(provider_error_type) as caught:
                    success_envelope = (
                        self._b21_success_after_harness_cleanup(
                            {},
                            harnesses,
                        )
                    )
                self.assertIs(caught.exception, provider_error)
                self.assertIsNone(success_envelope)
                self.assertEqual(harnesses, [harness])
                self.assertEqual(
                    transport_close_calls,
                    [harness.transport],
                )
            self.assertEqual(
                self._b21_success_after_harness_cleanup(
                    {},
                    harnesses,
                ),
                {
                    "ok": True,
                    "stage": "complete",
                    "outcome": {},
                },
            )
            self.assertEqual(harnesses, [])

        zero_exit_harness = make_real_gateway(clock=ManualClock(NOW))
        zero_exit_harnesses = [zero_exit_harness]
        zero_exit_transport_type = type(zero_exit_harness.transport)
        zero_exit_original_close = zero_exit_transport_type.close

        def close_transport_then_exit_zero(transport):
            zero_exit_original_close(transport)
            raise SystemExit(0)

        zero_exit_success = None
        with mock.patch.object(
            zero_exit_transport_type,
            "close",
            new=close_transport_then_exit_zero,
        ):
            with self.assertRaises(SystemExit) as caught:
                zero_exit_success = (
                    self._b21_success_after_harness_cleanup(
                        {},
                        zero_exit_harnesses,
                    )
                )
        self.assertEqual(caught.exception.code, 0)
        self.assertIsNone(zero_exit_success)
        self.assertEqual(zero_exit_harnesses, [zero_exit_harness])
        self.assertEqual(
            self._b21_success_after_harness_cleanup(
                {},
                zero_exit_harnesses,
            ),
            {
                "ok": True,
                "stage": "complete",
                "outcome": {},
            },
        )

        for cleanup_error in (
            RuntimeError("b21_real_provider_cleanup_runtime"),
            KeyboardInterrupt("b21_real_provider_cleanup_keyboard"),
            SystemExit(0),
            SystemExit("b21_real_provider_cleanup_system_exit"),
            GeneratorExit("b21_real_provider_cleanup_generator"),
        ):
            primary_error = RuntimeError("b21_real_provider_body_failure")
            harness = make_real_gateway(clock=ManualClock(NOW))
            harnesses = [harness]
            transport_type = type(harness.transport)
            original_transport_close = transport_type.close
            success_envelope = None
            observed_cleanup = None

            def fail_before_transport_close(
                _transport,
                *,
                injected=cleanup_error,
            ):
                raise injected

            with self.subTest(
                combined_provider_cleanup=(
                    type(cleanup_error).__name__,
                    getattr(cleanup_error, "code", None),
                )
            ), mock.patch.object(
                transport_type,
                "close",
                new=fail_before_transport_close,
            ):
                try:
                    try:
                        raise primary_error
                    except BaseException:
                        try:
                            success_envelope = (
                                self._b21_success_after_harness_cleanup(
                                    {},
                                    harnesses,
                                )
                            )
                        except BaseException as caught_cleanup:
                            observed_cleanup = caught_cleanup
                        if observed_cleanup is not None:
                            self._b21_preserve_primary_cleanup_failures(
                                (
                                    (
                                        "provider_harness_cleanup",
                                        observed_cleanup,
                                    ),
                                )
                            )
                        raise
                except BaseException as caught_primary:
                    self.assertIs(caught_primary, primary_error)
            self.assertIs(observed_cleanup, cleanup_error)
            self.assertIsNone(success_envelope)
            self.assertEqual(harnesses, [harness])
            self.assertFalse(harness.transport._closed)
            self.assertTrue(
                any(
                    (
                        "b21_cleanup_failure:"
                        "provider_harness_cleanup:"
                        + type(cleanup_error).__name__
                    )
                    in note
                    for note in getattr(primary_error, "__notes__", ())
                )
            )
            self.assertEqual(
                self._b21_success_after_harness_cleanup({}, harnesses),
                {
                    "ok": True,
                    "stage": "complete",
                    "outcome": {},
                },
            )
            self.assertEqual(harnesses, [])
            self.assertTrue(harness.transport._closed)
            self.assertIs(
                transport_type.close,
                original_transport_close,
            )

        fresh_outcome_types = {
            "runtime_rejected": bool,
            "runtime_lock_rejected": bool,
            "lease_rejected": bool,
            "lease_close_rejected": bool,
            "manager_close_rejected": bool,
            "fresh_connection_post_fork": bool,
            "fresh_database_identity": bool,
            "fresh_exact_ownership": bool,
            "fresh_process_epoch": bool,
            "inherited_sqlite_untouched": bool,
            "fresh_active_requests_zero": bool,
            "cleanup_failure_blocks_success": bool,
            "provider_cleanup_complete": bool,
            "fresh": bool,
        }
        valid_probe_outcome = {
            name: True
            for name in fresh_outcome_types
        }
        valid_probe_success = {
            "ok": True,
            "stage": "complete",
            "outcome": valid_probe_outcome,
        }
        valid_probe_failure = {
            "ok": False,
            "stage": "probe",
            "type": "RuntimeError",
            "message": "probe",
            "traceback": "probe",
        }
        valid_probe_document = self._b21_encode_child_envelope(
            valid_probe_success
        )
        first_outcome_name = next(iter(fresh_outcome_types))
        encoded_outcome_member = (
            b'"'
            + first_outcome_name.encode("ascii")
            + b'":true'
        )
        invalid_probe_values = (
            True,
            False,
            None,
            {},
            {"ok": 1, "stage": "complete", "outcome": valid_probe_outcome},
            {**valid_probe_success, "type": "RuntimeError"},
            {**valid_probe_success, "unexpected": False},
            {"ok": True, "stage": "complete"},
            {"ok": True, "stage": "wrong", "outcome": valid_probe_outcome},
            {"ok": True, "stage": "complete", "outcome": {}},
            {**valid_probe_failure, "outcome": valid_probe_outcome},
            {
                key: value
                for key, value in valid_probe_failure.items()
                if key != "type"
            },
            {**valid_probe_failure, "stage": None},
        )
        invalid_probe_documents = (
            b"",
            b"{",
            b"x" * 32769,
            valid_probe_document.replace(
                b'{"ok":true,',
                b'{"ok":false,"ok":true,',
                1,
            ),
            valid_probe_document.replace(
                encoded_outcome_member,
                (
                    b'"'
                    + first_outcome_name.encode("ascii")
                    + b'":false,'
                    + encoded_outcome_member
                ),
                1,
            ),
            b" " + valid_probe_document,
            valid_probe_document + b"\n",
            valid_probe_document + b"{}",
            *(
                json.dumps(value).encode("ascii")
                for value in invalid_probe_values
            ),
        )
        for invalid_document in invalid_probe_documents:
            with self.assertRaises(AssertionError):
                self._b21_decode_child_envelope(
                    invalid_document,
                    outcome_types=fresh_outcome_types,
                )
        failure_kind, failure_value = self._b21_decode_child_envelope(
            self._b21_encode_child_envelope(valid_probe_failure),
            outcome_types=fresh_outcome_types,
        )
        self.assertIs(failure_kind, False)
        self.assertEqual(failure_value, valid_probe_failure)

        for payload_size, expect_oversize in (
            (32768, False),
            (32769, True),
        ):
            remaining_payload = bytearray(b"x" * payload_size)
            read_requests = []

            def bounded_read(_descriptor, requested):
                read_requests.append(requested)
                if not remaining_payload:
                    return b""
                chunk = bytes(remaining_payload[:requested])
                del remaining_payload[:requested]
                return chunk

            with mock.patch.object(
                select,
                "select",
                return_value=([991], [], []),
            ), mock.patch.object(
                os,
                "read",
                side_effect=bounded_read,
            ):
                if expect_oversize:
                    with self.assertRaisesRegex(
                        AssertionError,
                        "b21_child_document_too_large",
                    ):
                        self._b21_read_pipe_bounded(
                            991,
                            timeout=1,
                        )
                else:
                    self.assertEqual(
                        len(
                            self._b21_read_pipe_bounded(
                                991,
                                timeout=1,
                            )
                        ),
                        32768,
                    )
            consumed_before = 0
            for requested in read_requests:
                self.assertLessEqual(
                    requested,
                    min(4096, 32768 + 1 - consumed_before),
                )
                consumed_before += min(
                    requested,
                    max(0, payload_size - consumed_before),
                )
            self.assertEqual(read_requests[-1], 1)

        with (
            temporary_browser_login_state() as state,
            temporary_browser_login_state(port=8444) as fresh_state,
        ):
            runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            lease = runtime.open_writable_connection()
            manager = object.__getattribute__(runtime, "_connections")
            begin_cursor = lease.execute("BEGIN IMMEDIATE")
            begin_cursor.close()
            self.assertTrue(lease.in_transaction)
            rollback_cursor = lease.execute("ROLLBACK")
            rollback_cursor.close()
            self.assertFalse(lease.in_transaction)
            parent_pid = os.getpid()
            parent_epoch = object.__getattribute__(
                runtime,
                "_process_epoch",
            )
            parent_global_epoch = runtime_module._DATABASE_PROCESS_EPOCH
            parent_publication_lock = (
                runtime_module._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK
            )
            parent_runtime_lock = object.__getattribute__(
                runtime,
                "_lock",
            )
            parent_manager_condition = manager._condition
            parent_manager_identity = manager._identity
            parent_record = lease._record
            parent_record_issuance = parent_record._issuance
            parent_record_generation = parent_record._generation
            parent_borrower_token = parent_record._borrower_token
            parent_connection = parent_record._connection_identity
            parent_browser = runtime.browser_integration
            parent_active_requests = parent_browser.active_request_count
            database_path = fresh_state.database_path.resolve(strict=True)
            database_stat = database_path.stat()
            database_identity = (
                database_stat.st_dev,
                database_stat.st_ino,
            )
            child_connect_events = []
            inherited_manager_lookups = []
            inherited_authorizer_calls = []
            inherited_connection_cleanup = []
            original_connect = runtime_module.sqlite3.connect
            original_connection_for_lease = (
                runtime_module._RuntimeDatabaseConnections
                ._connection_for_lease
            )
            original_statement_authorized = (
                runtime_module._DatabaseConnectionOwnership
                .statement_authorized
            )
            original_connection_cleanup = (
                runtime_module._cleanup_database_connection_independently
            )

            def tracked_connect(database, *arguments, **keywords):
                event = {
                    "pid": os.getpid(),
                    "database": database,
                    "managed": (
                        type(keywords.get("factory"))
                        is runtime_module._DatabaseConnectionOwnership
                    ),
                    "connection": None,
                }
                if os.getpid() != parent_pid:
                    child_connect_events.append(event)
                connection = original_connect(
                    database,
                    *arguments,
                    **keywords,
                )
                event["connection"] = connection
                return connection

            def tracked_connection_for_lease(
                candidate_manager,
                candidate_lease,
            ):
                if (
                    os.getpid() != parent_pid
                    and candidate_manager is manager
                ):
                    inherited_manager_lookups.append(candidate_lease)
                return original_connection_for_lease(
                    candidate_manager,
                    candidate_lease,
                )

            def tracked_statement_authorized(candidate_record):
                if (
                    os.getpid() != parent_pid
                    and candidate_record is parent_record
                ):
                    inherited_authorizer_calls.append(candidate_record)
                return original_statement_authorized(candidate_record)

            def tracked_connection_cleanup(connection, *, rollback):
                if (
                    os.getpid() != parent_pid
                    and connection is parent_connection
                ):
                    inherited_connection_cleanup.append(connection)
                return original_connection_cleanup(
                    connection,
                    rollback=rollback,
                )

            runtime_module.sqlite3.connect = tracked_connect
            (
                runtime_module._RuntimeDatabaseConnections
                ._connection_for_lease
            ) = tracked_connection_for_lease
            (
                runtime_module._DatabaseConnectionOwnership
                .statement_authorized
            ) = tracked_statement_authorized
            runtime_module._cleanup_database_connection_independently = (
                tracked_connection_cleanup
            )
            result_reader, result_writer = self._b21_pipe()
            control_reader, control_writer = self._b21_pipe()
            pid = None
            reaped = False
            inherited_locks_held = False
            try:
                parent_runtime_lock.acquire()
                parent_manager_condition.acquire()
                inherited_locks_held = True
                pid = os.fork()
                if pid == 0:
                    result_reader.close()
                    control_writer.close()
                    outcome = {
                        "runtime_rejected": False,
                        "runtime_lock_rejected": False,
                        "lease_rejected": False,
                        "lease_close_rejected": False,
                        "manager_close_rejected": False,
                        "fresh_connection_post_fork": False,
                        "fresh_database_identity": False,
                        "fresh_exact_ownership": False,
                        "fresh_process_epoch": False,
                        "inherited_sqlite_untouched": False,
                        "fresh_active_requests_zero": False,
                        "cleanup_failure_blocks_success": False,
                        "provider_cleanup_complete": False,
                        "fresh": False,
                    }
                    child_runtime = None
                    fresh_lease = None
                    child_harnesses = []
                    stage = "inherited_authority"
                    primary_error = None
                    primary_stage = None
                    cleanup_failures = []
                    success_envelope = None
                    try:
                        try:
                            runtime.configuration
                        except DurableGoogleLoginConfigurationError:
                            outcome["runtime_rejected"] = True
                        try:
                            runtime.browser_integration
                        except DurableGoogleLoginConfigurationError:
                            outcome["runtime_lock_rejected"] = True
                        try:
                            lease.execute("SELECT 1")
                        except DurableGoogleLoginConfigurationError:
                            outcome["lease_rejected"] = True
                        try:
                            lease.close()
                        except DurableGoogleLoginConfigurationError:
                            outcome["lease_close_rejected"] = True
                        try:
                            manager.close()
                        except DurableGoogleLoginConfigurationError:
                            outcome["manager_close_rejected"] = True
                        stage = "rejection_publication"
                        rejected_document = b"rejected"
                        rejected_offset = 0
                        while rejected_offset < len(rejected_document):
                            written = os.write(
                                result_writer,
                                rejected_document[rejected_offset:],
                            )
                            if written <= 0:
                                raise OSError(
                                    "fork_rejection_pipe_write_failed"
                                )
                            rejected_offset += written
                        stage = "control_wait"
                        ready, _, _ = select.select(
                            [control_reader],
                            [],
                            [],
                            10,
                        )
                        if not ready or os.read(control_reader, 32) != b"continue":
                            raise AssertionError("fork_control_timeout")

                        child_clock = ManualClock(NOW)
                        stage = "provider_cleanup_failure_control"
                        provider_failure_results = []
                        for provider_error in (
                            RuntimeError(
                                "b21_provider_cleanup_runtime"
                            ),
                            KeyboardInterrupt(
                                "b21_provider_cleanup_keyboard"
                            ),
                            SystemExit(0),
                            SystemExit(
                                "b21_provider_cleanup_system_exit"
                            ),
                            GeneratorExit(
                                "b21_provider_cleanup_generator"
                            ),
                        ):
                            provider_body_error = RuntimeError(
                                "b21_provider_body_failure"
                            )
                            provider_probe = make_real_gateway(
                                clock=child_clock,
                                redirect_uri=state.redirect_uri,
                                subject=state.subject,
                            )
                            child_harnesses.append(provider_probe)
                            provider_probe_harnesses = child_harnesses
                            provider_probe_transport_type = type(
                                provider_probe.transport
                            )
                            provider_probe_original_close = (
                                provider_probe_transport_type.close
                            )

                            def close_provider_then_fail(
                                transport,
                                *,
                                original_close=(
                                    provider_probe_original_close
                                ),
                                injected_error=provider_error,
                            ):
                                original_close(transport)
                                raise injected_error

                            provider_probe_envelope = None
                            observed_provider_error = None
                            with mock.patch.object(
                                provider_probe_transport_type,
                                "close",
                                new=close_provider_then_fail,
                            ):
                                try:
                                    try:
                                        raise provider_body_error
                                    except BaseException:
                                        try:
                                            provider_probe_envelope = (
                                                self
                                                ._b21_success_after_harness_cleanup(
                                                    {},
                                                    provider_probe_harnesses,
                                                )
                                            )
                                        except BaseException as error:
                                            observed_provider_error = error
                                        if observed_provider_error is not None:
                                            self._b21_preserve_primary_cleanup_failures(
                                                (
                                                    (
                                                        "provider_harness_cleanup",
                                                        observed_provider_error,
                                                    ),
                                                )
                                            )
                                        raise
                                except BaseException as body_error:
                                    provider_failure_results.append(
                                        body_error is provider_body_error
                                        and observed_provider_error
                                        is provider_error
                                        and provider_probe_envelope is None
                                        and provider_probe_harnesses
                                        == [provider_probe]
                                        and any(
                                            (
                                                "b21_cleanup_failure:"
                                                "provider_harness_cleanup:"
                                                + type(
                                                    provider_error
                                                ).__name__
                                            )
                                            in note
                                            for note in getattr(
                                                provider_body_error,
                                                "__notes__",
                                                (),
                                            )
                                        )
                                    )
                            retry_envelope = (
                                self
                                ._b21_success_after_harness_cleanup(
                                    {},
                                    provider_probe_harnesses,
                                )
                            )
                            if (
                                retry_envelope
                                != {
                                    "ok": True,
                                    "stage": "complete",
                                    "outcome": {},
                                }
                                or provider_probe_harnesses
                            ):
                                raise AssertionError(
                                    "provider_cleanup_failure_retry"
                                )
                        outcome["cleanup_failure_blocks_success"] = (
                            provider_failure_results
                            == [True, True, True, True, True]
                        )

                        def child_gateway_factory(
                            configuration,
                            client_secret,
                        ):
                            harness = make_real_gateway(
                                clock=child_clock,
                                client_id=configuration.google_client_id,
                                client_secret=client_secret,
                                redirect_uri=configuration.google_redirect_uri,
                                subject=fresh_state.subject,
                            )
                            child_harnesses.append(harness)
                            return harness.gateway

                        stage = "fresh_runtime_build"
                        child_runtime = build_durable_google_login_runtime(
                            fresh_state.configuration_path,
                            _clock=child_clock,
                            _gateway_factory=child_gateway_factory,
                        )
                        fresh_manager = object.__getattribute__(
                            child_runtime,
                            "_connections",
                        )
                        stage = "fresh_lease_open"
                        fresh_lease = (
                            child_runtime.open_writable_connection()
                        )
                        fresh_record = fresh_lease._record
                        fresh_connection = (
                            fresh_record._connection_identity
                        )
                        database_cursor = fresh_lease.execute(
                            "PRAGMA database_list"
                        )
                        database_rows = tuple(
                            tuple(row) for row in database_cursor.fetchall()
                        )
                        database_cursor.close()
                        main_paths = tuple(
                            Path(row[2]).resolve(strict=True)
                            for row in database_rows
                            if row[1] == "main"
                        )
                        select_cursor = fresh_lease.execute("SELECT 1")
                        select_result = tuple(select_cursor.fetchone())
                        select_cursor.close()
                        fresh_browser = child_runtime.browser_integration
                        managed_connects = tuple(
                            event
                            for event in child_connect_events
                            if event["managed"]
                        )
                        outcome["fresh_connection_post_fork"] = (
                            len(managed_connects) == 3
                            and all(
                                event["pid"] == os.getpid()
                                and event["connection"] is not None
                                for event in managed_connects
                            )
                            and any(
                                event["connection"]
                                is fresh_connection
                                for event in managed_connects
                            )
                        )
                        outcome["fresh_database_identity"] = (
                            main_paths == (database_path,)
                            and (
                                database_path.stat().st_dev,
                                database_path.stat().st_ino,
                            )
                            == database_identity
                        )
                        outcome["fresh_exact_ownership"] = (
                            fresh_manager is not manager
                            and fresh_record is not parent_record
                            and fresh_connection is not parent_connection
                            and fresh_record._manager is fresh_manager
                            and fresh_record._borrower_token
                            is fresh_lease._token
                            and select_result == (1,)
                        )
                        outcome["fresh_process_epoch"] = (
                            fresh_manager._process_epoch.pid
                            == os.getpid()
                            and fresh_manager._process_epoch
                            is object.__getattribute__(
                                child_runtime,
                                "_process_epoch",
                            )
                            and fresh_manager._process_epoch
                            is runtime_module._DATABASE_PROCESS_EPOCH
                            and fresh_manager._process_epoch
                            is not parent_epoch
                        )
                        outcome["inherited_sqlite_untouched"] = not (
                            inherited_manager_lookups
                            or inherited_authorizer_calls
                            or inherited_connection_cleanup
                        )
                        outcome["fresh_active_requests_zero"] = (
                            fresh_browser.active_request_count == 0
                        )
                        stage = "fresh_lease_close"
                        lease_to_close = fresh_lease
                        lease_to_close.close()
                        fresh_lease = None
                        stage = "fresh_runtime_close"
                        runtime_to_close = child_runtime
                        cleanup_report = runtime_to_close.close()
                        outcome["fresh"] = (
                            type(
                                getattr(
                                    cleanup_report,
                                    "cleanup_complete",
                                    None,
                                )
                            )
                            is bool
                            and cleanup_report.cleanup_complete is True
                        )
                        if not outcome["fresh"]:
                            raise AssertionError(
                                "fresh_runtime_cleanup_incomplete"
                            )
                        child_runtime = None
                        stage = "provider_harness_cleanup"
                        success_envelope = (
                            self._b21_success_after_harness_cleanup(
                                outcome,
                                child_harnesses,
                            )
                        )
                    except BaseException as error:
                        primary_error = error
                        primary_stage = stage
                    finally:
                        if fresh_lease is not None:
                            lease_to_close = fresh_lease
                            try:
                                lease_to_close.close()
                            except BaseException as cleanup_error:
                                cleanup_failures.append(
                                    (
                                        "fresh_lease_cleanup",
                                        cleanup_error,
                                    )
                                )
                            else:
                                fresh_lease = None
                        if child_runtime is not None:
                            (
                                child_runtime,
                                runtime_cleanup_failures,
                            ) = self._b21_close_runtime_owner(
                                child_runtime,
                                stage="fresh_runtime_cleanup",
                            )
                            cleanup_failures.extend(
                                runtime_cleanup_failures
                            )
                        if child_harnesses:
                            try:
                                self._b21_success_after_harness_cleanup(
                                    {},
                                    child_harnesses,
                                )
                            except BaseException as cleanup_error:
                                cleanup_failures.append(
                                    (
                                        "provider_harness_cleanup",
                                        cleanup_error,
                                    )
                                )
                    if primary_error is None and cleanup_failures:
                        (
                            primary_stage,
                            primary_error,
                        ) = cleanup_failures.pop(0)
                    if primary_error is None:
                        if success_envelope is None:
                            primary_error = AssertionError(
                                "fresh_child_success_unpublished"
                            )
                            primary_stage = "success_publication"
                            envelope = self._b21_child_failure_envelope(
                                primary_error,
                                stage=primary_stage,
                            )
                            exit_code = 1
                        else:
                            envelope = success_envelope
                            exit_code = 0
                    else:
                        envelope = self._b21_child_failure_envelope(
                            primary_error,
                            stage=primary_stage,
                            cleanup_failures=cleanup_failures,
                        )
                        exit_code = 1
                    try:
                        document = self._b21_encode_child_envelope(
                            envelope
                        )
                    except BaseException as serialization_error:
                        exit_code = 2
                        if primary_error is None:
                            document = self._b21_encode_child_envelope(
                                self._b21_child_failure_envelope(
                                    serialization_error,
                                    stage="result_serialization",
                                )
                            )
                        else:
                            document = self._b21_encode_child_envelope(
                                self._b21_child_failure_envelope(
                                    primary_error,
                                    stage=primary_stage,
                                    cleanup_failures=(
                                        *cleanup_failures,
                                        (
                                            "result_serialization",
                                            serialization_error,
                                        ),
                                    ),
                                )
                            )
                    try:
                        offset = 0
                        while offset < len(document):
                            written = os.write(
                                result_writer,
                                document[offset:],
                            )
                            if written <= 0:
                                raise OSError(
                                    "fresh_child_pipe_write_failed"
                                )
                            offset += written
                    except BaseException:
                        exit_code = 2
                    try:
                        control_reader.close()
                    except BaseException:
                        exit_code = 2
                    try:
                        result_writer.close()
                    except BaseException:
                        exit_code = 2
                    os._exit(exit_code)

                result_writer.close()
                result_writer = None
                control_reader.close()
                control_reader = None
                ready, _, _ = select.select([result_reader], [], [], 10)
                self.assertTrue(ready)
                self.assertEqual(os.read(result_reader, 32), b"rejected")
                os.write(control_writer, b"continue")
                control_writer.close()
                control_writer = None
                payload = self._b21_read_pipe_bounded(
                    result_reader,
                    timeout=10,
                )
                status = self._b21_waitpid_bounded(pid, timeout=10)
                reaped = True
                self.assertIs(type(status), int)
                exit_code = os.waitstatus_to_exitcode(status)
                success, child_outcome = (
                    self._b21_decode_child_envelope(
                        payload,
                        outcome_types=fresh_outcome_types,
                    )
                )
                if success is not True:
                    self.fail(
                        "fresh_child_failure:"
                        f"exit={exit_code}:"
                        f"stage={child_outcome['stage']}:"
                        f"type={child_outcome['type']}:"
                        f"message={child_outcome['message']}:"
                        f"traceback={child_outcome['traceback']}"
                    )
                self.assertEqual(exit_code, 0)
                parent_manager_condition.release()
                parent_runtime_lock.release()
                inherited_locks_held = False
                self.assertEqual(
                    child_outcome,
                    {
                        "runtime_rejected": True,
                        "runtime_lock_rejected": True,
                        "lease_rejected": True,
                        "lease_close_rejected": True,
                        "manager_close_rejected": True,
                        "fresh_connection_post_fork": True,
                        "fresh_database_identity": True,
                        "fresh_exact_ownership": True,
                        "fresh_process_epoch": True,
                        "inherited_sqlite_untouched": True,
                        "fresh_active_requests_zero": True,
                        "cleanup_failure_blocks_success": True,
                        "provider_cleanup_complete": True,
                        "fresh": True,
                    },
                )
                self.assertEqual(os.getpid(), parent_pid)
                self.assertIs(
                    object.__getattribute__(runtime, "_process_epoch"),
                    parent_epoch,
                )
                self.assertIs(
                    runtime_module._DATABASE_PROCESS_EPOCH,
                    parent_global_epoch,
                )
                self.assertIs(
                    runtime_module._DATABASE_PROCESS_EPOCH_PUBLICATION_LOCK,
                    parent_publication_lock,
                )
                self.assertIs(
                    object.__getattribute__(runtime, "_lock"),
                    parent_runtime_lock,
                )
                self.assertIs(manager._condition, parent_manager_condition)
                self.assertIs(manager._identity, parent_manager_identity)
                self.assertIs(lease._record, parent_record)
                self.assertIs(parent_record._issuance, parent_record_issuance)
                self.assertEqual(
                    parent_record._generation,
                    parent_record_generation,
                )
                self.assertIs(
                    parent_record._borrower_token,
                    parent_borrower_token,
                )
                self.assertIs(
                    parent_record._connection_identity,
                    parent_connection,
                )
                self.assertIs(runtime.browser_integration, parent_browser)
                self.assertEqual(
                    parent_browser.active_request_count,
                    parent_active_requests,
                )
                self.assertFalse(lease.in_transaction)
                parent_cursor = lease.execute("SELECT 1")
                self.assertEqual(tuple(parent_cursor.fetchone()), (1,))
                parent_cursor.close()
            finally:
                cleanup_failures = []
                if inherited_locks_held:
                    for stage, lock in (
                        (
                            "parent_manager_condition_release",
                            parent_manager_condition,
                        ),
                        ("parent_runtime_lock_release", parent_runtime_lock),
                    ):
                        try:
                            lock.release()
                        except BaseException as cleanup_error:
                            cleanup_failures.append(
                                (stage, cleanup_error)
                            )
                runtime_module.sqlite3.connect = original_connect
                (
                    runtime_module._RuntimeDatabaseConnections
                    ._connection_for_lease
                ) = original_connection_for_lease
                (
                    runtime_module._DatabaseConnectionOwnership
                    .statement_authorized
                ) = original_statement_authorized
                runtime_module._cleanup_database_connection_independently = (
                    original_connection_cleanup
                )
                _status, endpoint_failures = (
                    self._b21_close_endpoints_and_reap(
                        pid=pid,
                        reaped=reaped,
                        endpoints=(
                            ("result_reader_close", result_reader),
                            ("result_writer_close", result_writer),
                            ("control_reader_close", control_reader),
                            ("control_writer_close", control_writer),
                        ),
                    )
                )
                cleanup_failures.extend(endpoint_failures)
                try:
                    lease.close()
                except BaseException as cleanup_error:
                    cleanup_failures.append(
                        ("parent_lease_cleanup", cleanup_error)
                    )
                try:
                    with manager._condition:
                        self.assertEqual(manager._records, {})
                except BaseException as cleanup_error:
                    cleanup_failures.append(
                        ("parent_manager_terminal_check", cleanup_error)
                    )
                runtime, runtime_failures = (
                    self._b21_close_runtime_owner(
                        runtime,
                        stage="parent_runtime_cleanup",
                    )
                )
                cleanup_failures.extend(runtime_failures)
                self._b21_preserve_primary_cleanup_failures(
                    cleanup_failures
                )


if __name__ == "__main__":
    unittest.main()
