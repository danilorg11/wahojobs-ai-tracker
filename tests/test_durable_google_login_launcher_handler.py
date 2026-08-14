from contextlib import redirect_stderr, redirect_stdout
import io
import inspect
import logging
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import durable_google_login_app as launcher
from wahojobs import durable_google_login_runtime as runtime_module
import wahojobs.google_oidc_gateway as gateway_module


class _CompleteDurableIntegration:
    def handle(self, *_args, **_kwargs):
        raise AssertionError("construction_must_not_dispatch")

    def issue_confirmed_profile_artifact(self, **_kwargs):
        raise AssertionError("construction_must_not_issue")

    def authenticate_completed_profile_replay(self, **_kwargs):
        raise AssertionError("construction_must_not_authenticate_replay")

    @staticmethod
    def matches_route(path):
        return path == "/find-matches"


class _RecordingStream:
    def __init__(self, *, write_failure=None, flush_failure=None):
        self.write_failure = write_failure
        self.flush_failure = flush_failure
        self.write_calls = []
        self.flush_calls = 0

    def write(self, value):
        self.write_calls.append(value)
        if self.write_failure is not None:
            raise self.write_failure
        return len(value)

    def flush(self):
        self.flush_calls += 1
        if self.flush_failure is not None:
            raise self.flush_failure

    def text(self):
        return "".join(self.write_calls)


class DurableGoogleLoginLauncherHandlerTests(unittest.TestCase):
    def test_production_construction_uses_durable_only_handler_and_keeps_detachment(self):
        integration = _CompleteDurableIntegration()

        handler = launcher._construct_production_handler(
            integration,
            "https://localhost:8443",
            require_profile_creation=True,
        )

        self.assertEqual(
            handler.__module__,
            "wahojobs.durable_product_browser_handler",
        )
        self.assertIs(
            handler._durable_google_login_browser_integration,
            integration,
        )
        handler._durable_google_login_browser_integration = None
        self.assertIsNone(
            handler._durable_google_login_browser_integration
        )

    def test_required_profile_capabilities_fail_closed(self):
        class HandleOnly:
            @staticmethod
            def handle(*_args, **_kwargs):
                return None

        class ArtifactOnly(HandleOnly):
            @staticmethod
            def issue_confirmed_profile_artifact(**_kwargs):
                return None

        class ArtifactAndReplay(ArtifactOnly):
            @staticmethod
            def authenticate_completed_profile_replay(**_kwargs):
                return False

        cases = (
            (
                "missing-artifact",
                HandleOnly(),
                "profile_creation_capability_unavailable",
            ),
            (
                "invalid-artifact",
                type(
                    "InvalidArtifact",
                    (HandleOnly,),
                    {"issue_confirmed_profile_artifact": None},
                )(),
                "profile_creation_capability_invalid",
            ),
            (
                "missing-replay",
                ArtifactOnly(),
                "profile_creation_capability_invalid",
            ),
            (
                "invalid-replay",
                type(
                    "InvalidReplay",
                    (ArtifactOnly,),
                    {"authenticate_completed_profile_replay": None},
                )(),
                "profile_creation_capability_invalid",
            ),
            (
                "missing-matches",
                ArtifactAndReplay(),
                "profile_matching_capability_unavailable",
            ),
            (
                "invalid-matches",
                type(
                    "InvalidMatches",
                    (ArtifactAndReplay,),
                    {"matches_route": None},
                )(),
                "profile_matching_capability_invalid",
            ),
            (
                "unowned-matches",
                type(
                    "UnownedMatches",
                    (ArtifactAndReplay,),
                    {"matches_route": staticmethod(lambda _path: False)},
                )(),
                "profile_matching_capability_unavailable",
            ),
            (
                "non-boolean-matches",
                type(
                    "NonBooleanMatches",
                    (ArtifactAndReplay,),
                    {"matches_route": staticmethod(lambda _path: 1)},
                )(),
                "profile_matching_capability_unavailable",
            ),
            (
                "failed-matches",
                type(
                    "FailedMatches",
                    (ArtifactAndReplay,),
                    {
                        "matches_route": staticmethod(
                            lambda _path: (_ for _ in ()).throw(
                                RuntimeError("private")
                            )
                        )
                    },
                )(),
                "profile_matching_capability_invalid",
            ),
        )

        for label, integration, reason in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(RuntimeError, reason):
                    launcher._construct_production_handler(
                        integration,
                        "https://localhost:8443",
                        require_profile_creation=True,
                    )

    def test_nonproduction_injected_runtime_keeps_optional_capability_contract(self):
        class RoutingOnlyIntegration:
            @staticmethod
            def handle(*_args, **_kwargs):
                return None

        handler = launcher._construct_production_handler(
            RoutingOnlyIntegration(),
            "https://localhost:8443",
            require_profile_creation=False,
        )
        self.assertEqual(
            handler.__module__,
            "wahojobs.durable_product_browser_handler",
        )

        class PartialCreationIntegration(RoutingOnlyIntegration):
            @staticmethod
            def issue_confirmed_profile_artifact(**_kwargs):
                return None

        with self.assertRaisesRegex(
            RuntimeError,
            "profile_creation_capability_invalid",
        ):
            launcher._construct_production_handler(
                PartialCreationIntegration(),
                "https://localhost:8443",
                require_profile_creation=False,
            )

    def test_callback_failure_writer_is_one_bounded_flushed_line_without_logging(self):
        stream = _RecordingStream()
        writer = launcher._GoogleCallbackFailureStderrWriter(stream)
        self.assertFalse(hasattr(writer, "__dict__"))
        self.assertEqual(
            repr(writer),
            "_GoogleCallbackFailureStderrWriter(<configured>)",
        )
        line = gateway_module._GOOGLE_CALLBACK_FAILURE_EVENTS_V1[
            gateway_module._GoogleCallbackFailureStageV1
            .INVITATION_EMAIL_AGREEMENT_REJECTED
        ]
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        root = logging.getLogger()
        handlers = (Capture(), Capture())
        for handler in handlers:
            root.addHandler(handler)
        try:
            self.assertTrue(writer(line))
        finally:
            for handler in handlers:
                root.removeHandler(handler)

        self.assertEqual(stream.write_calls, [line + "\n"])
        self.assertEqual(stream.flush_calls, 1)
        self.assertEqual(records, [])
        self.assertLessEqual(
            len(stream.write_calls[0].encode("ascii")),
            256,
        )
        self.assertFalse(writer("not-a-fixed-event"))
        self.assertFalse(writer(object()))
        self.assertEqual(stream.write_calls, [line + "\n"])
        self.assertEqual(stream.flush_calls, 1)

    def test_callback_failure_writer_serializes_concurrent_complete_lines(self):
        class InterleavingProbe(_RecordingStream):
            def __init__(self):
                super().__init__()
                self.active = 0
                self.maximum_active = 0
                self.measurement_lock = threading.Lock()

            def write(self, value):
                with self.measurement_lock:
                    self.active += 1
                    self.maximum_active = max(
                        self.maximum_active,
                        self.active,
                    )
                try:
                    time.sleep(0.01)
                    return super().write(value)
                finally:
                    with self.measurement_lock:
                        self.active -= 1

        stream = InterleavingProbe()
        writer = launcher._GoogleCallbackFailureStderrWriter(stream)
        lines = tuple(gateway_module._GOOGLE_CALLBACK_FAILURE_LINES_V1)[:2]
        barrier = threading.Barrier(3)
        results = []

        def invoke(line):
            barrier.wait()
            results.append(writer(line))

        threads = tuple(
            threading.Thread(target=invoke, args=(line,))
            for line in lines
        )
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(results, [True, True])
        self.assertEqual(stream.maximum_active, 1)
        self.assertEqual(set(stream.write_calls), {line + "\n" for line in lines})
        self.assertEqual(stream.flush_calls, 2)

    def test_callback_failure_writer_and_gateway_isolate_all_sink_failures(self):
        class SinkBaseFailure(BaseException):
            pass

        class PartialWriteStream(_RecordingStream):
            def __init__(self):
                super().__init__()
                self.fail_next_write = True

            def write(self, value):
                if self.fail_next_write:
                    self.fail_next_write = False
                    self.write_calls.append(value[:13])
                    raise SinkBaseFailure("PRIVATE_PARTIAL_WRITE_FAILURE")
                return super().write(value)

        class FailFirstConcurrentStream(_RecordingStream):
            def __init__(self):
                super().__init__()
                self.first_write_entered = threading.Event()
                self.allow_first_failure = threading.Event()

            def write(self, value):
                self.write_calls.append(value)
                if len(self.write_calls) == 1:
                    self.first_write_entered.set()
                    if not self.allow_first_failure.wait(timeout=2):
                        raise AssertionError("bounded writer probe timed out")
                    raise SinkBaseFailure("PRIVATE_CONCURRENT_WRITE_FAILURE")
                return len(value)

        class SecondaryControl(BaseException):
            pass

        class HostileControl(BaseException):
            attribute_hook_calls = {
                "__traceback__": 0,
                "__cause__": 0,
                "__context__": 0,
            }

            def __setattr__(self, name, value):
                if name in self.attribute_hook_calls:
                    self.attribute_hook_calls[name] += 1
                    raise SecondaryControl()
                return super().__setattr__(name, value)

        stage = (
            gateway_module._GoogleCallbackFailureStageV1
            .PROVIDER_AUTHORIZATION_ERROR
        )
        line = gateway_module._GOOGLE_CALLBACK_FAILURE_EVENTS_V1[stage]
        payload = line + "\n"
        captured_stdout = io.StringIO()
        captured_stderr = io.StringIO()
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        signature = inspect.signature(
            launcher._GoogleCallbackFailureStderrWriter
        )
        self.assertEqual(tuple(signature.parameters), ("stream",))
        ownership_probe = launcher._GoogleCallbackFailureStderrWriter(
            _RecordingStream()
        )
        private_lock = object.__getattribute__(
            ownership_probe,
            "_GoogleCallbackFailureStderrWriter__lock",
        )
        self.assertIs(type(private_lock), type(threading.Lock()))
        private_lock = None
        ownership_probe = None

        root = logging.getLogger()
        handlers = (Capture(), Capture())
        for handler in handlers:
            root.addHandler(handler)
        try:
            with (
                redirect_stdout(captured_stdout),
                redirect_stderr(captured_stderr),
            ):
                failure_types = (
                    RuntimeError,
                    SinkBaseFailure,
                    KeyboardInterrupt,
                    SystemExit,
                    GeneratorExit,
                )
                for operation, failure_type in (
                    (operation, failure_type)
                    for operation in ("write", "flush")
                    for failure_type in failure_types
                ):
                    label = f"direct-{operation}-{failure_type.__name__}"
                    with self.subTest(case=label):
                        failure = failure_type(
                            f"PRIVATE_{operation.upper()}_FAILURE"
                        )
                        stream = _RecordingStream(
                            write_failure=(
                                failure if operation == "write" else None
                            ),
                            flush_failure=(
                                failure if operation == "flush" else None
                            ),
                        )
                        writer = launcher._GoogleCallbackFailureStderrWriter(
                            stream
                        )
                        with self.assertRaises(failure_type):
                            writer(line)
                        stream.write_failure = None
                        stream.flush_failure = None
                        self.assertTrue(writer(line))
                        self.assertEqual(stream.write_calls, [payload, payload])
                        self.assertEqual(
                            stream.flush_calls,
                            1 if operation == "write" else 2,
                        )

                for operation, failure_type in (
                    (operation, failure_type)
                    for operation in ("write", "flush")
                    for failure_type in failure_types
                ):
                    label = f"gateway-{operation}-{failure_type.__name__}"
                    with self.subTest(case=label):
                        failure = failure_type(
                            f"PRIVATE_GATEWAY_{operation.upper()}_FAILURE"
                        )
                        stream = _RecordingStream(
                            write_failure=(
                                failure if operation == "write" else None
                            ),
                            flush_failure=(
                                failure if operation == "flush" else None
                            ),
                        )
                        writer = launcher._GoogleCallbackFailureStderrWriter(
                            stream
                        )
                        self.assertFalse(
                            gateway_module
                            ._emit_google_callback_failure_stage_v1(
                                stage,
                                writer,
                            )
                        )
                        stream.write_failure = None
                        stream.flush_failure = None
                        self.assertTrue(
                            gateway_module
                            ._emit_google_callback_failure_stage_v1(
                                stage,
                                writer,
                            )
                        )
                        self.assertEqual(stream.write_calls, [payload, payload])
                        self.assertEqual(
                            stream.flush_calls,
                            1 if operation == "write" else 2,
                        )

                partial_stream = PartialWriteStream()
                partial_writer = launcher._GoogleCallbackFailureStderrWriter(
                    partial_stream
                )
                with self.assertRaises(SinkBaseFailure):
                    partial_writer(line)
                self.assertTrue(partial_writer(line))
                self.assertEqual(
                    partial_stream.write_calls,
                    [payload[:13], payload],
                )
                self.assertEqual(partial_stream.flush_calls, 1)

                for operation in ("write", "flush"):
                    with self.subTest(case=f"hostile-{operation}"):
                        hostile = HostileControl(
                            f"PRIVATE_HOSTILE_{operation.upper()}_FAILURE"
                        )
                        hostile_stream = _RecordingStream(
                            write_failure=(
                                hostile if operation == "write" else None
                            ),
                            flush_failure=(
                                hostile if operation == "flush" else None
                            ),
                        )
                        hostile_writer = (
                            launcher._GoogleCallbackFailureStderrWriter(
                                hostile_stream
                            )
                        )
                        self.assertFalse(
                            gateway_module
                            ._emit_google_callback_failure_stage_v1(
                                stage,
                                hostile_writer,
                            )
                        )
                        hostile_stream.write_failure = None
                        hostile_stream.flush_failure = None
                        self.assertTrue(hostile_writer(line))

                concurrent_stream = FailFirstConcurrentStream()
                concurrent_writer = (
                    launcher._GoogleCallbackFailureStderrWriter(
                        concurrent_stream
                    )
                )
                concurrent_results = []

                def fail_first():
                    try:
                        concurrent_writer(line)
                    except SinkBaseFailure:
                        concurrent_results.append("failed")

                def succeed_second():
                    concurrent_results.append(concurrent_writer(line))

                first = threading.Thread(target=fail_first)
                second = threading.Thread(target=succeed_second)
                first.start()
                self.assertTrue(
                    concurrent_stream.first_write_entered.wait(timeout=2)
                )
                second.start()
                concurrent_stream.allow_first_failure.set()
                first.join(timeout=2)
                second.join(timeout=2)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertCountEqual(concurrent_results, ["failed", True])
                self.assertEqual(
                    concurrent_stream.write_calls,
                    [payload, payload],
                )
                self.assertEqual(concurrent_stream.flush_calls, 1)

                self.assertEqual(
                    HostileControl.attribute_hook_calls,
                    {
                        "__traceback__": 0,
                        "__cause__": 0,
                        "__context__": 0,
                    },
                )
        finally:
            for handler in handlers:
                root.removeHandler(handler)

        self.assertEqual(captured_stdout.getvalue(), "")
        self.assertEqual(captured_stderr.getvalue(), "")
        self.assertEqual(records, [])

    def test_activation_boundary_installs_only_the_explicit_sink(self):
        configured = []

        class Pending:
            def _configure_callback_failure_telemetry(self, sink):
                configured.append(sink)
                return True

        pending = Pending()
        sink = object().__repr__

        def publish(_worker, _arguments, outcome):
            outcome._publish("ok", pending)

        with mock.patch.object(
            runtime_module,
            "_run_configuration_worker",
            side_effect=publish,
        ) as worker:
            result = runtime_module.prepare_durable_google_login_activation(
                "synthetic-config-not-read.json",
                _callback_failure_telemetry_sink=sink,
            )

        self.assertIs(result, pending)
        self.assertEqual(configured, [sink])
        worker.assert_called_once()

        with (
            mock.patch.object(
                runtime_module,
                "_run_configuration_worker",
            ) as forbidden_worker,
            self.assertRaises(
                runtime_module.DurableGoogleLoginConfigurationError
            ),
        ):
            runtime_module.prepare_durable_google_login_activation(
                "synthetic-config-not-read.json",
                _callback_failure_telemetry_sink=object(),
            )
        forbidden_worker.assert_not_called()

    def test_production_main_wires_and_flushes_callback_telemetry_before_clean_stop(self):
        emitted_stage = (
            gateway_module._GoogleCallbackFailureStageV1
            .TOKEN_EXCHANGE_OAUTH_REJECTED
        )
        expected_line = gateway_module._GOOGLE_CALLBACK_FAILURE_EVENTS_V1[
            emitted_stage
        ]
        stderr = _RecordingStream()
        stdout = io.StringIO()
        observed = {}

        class BrowserIntegration(_CompleteDurableIntegration):
            pass

        class Runtime:
            def __init__(self, coordinator):
                self._coordinator = coordinator

            @staticmethod
            def require_database_lifetime_ownership():
                return True

            def close(self, *, _preserve_primary=False):
                return self._coordinator.cleanup(
                    preserve_primary=_preserve_primary
                )

        class Pending:
            configuration = SimpleNamespace(
                bind_host="127.0.0.1",
                bind_port=8443,
                public_origin="https://localhost:8443",
            )
            browser_integration = BrowserIntegration()

            def __init__(self, coordinator):
                self._coordinator = coordinator

            def complete_activation(self):
                return Runtime(self._coordinator)

            def close(self, *, _preserve_primary=False):
                return self._coordinator.cleanup(
                    preserve_primary=_preserve_primary
                )

        class Socket:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

            def fileno(self):
                return -1 if self.closed else 41

        class TlsContext:
            pass

        class TlsScope:
            def __enter__(self):
                return TlsContext()

            def __exit__(self, _exc_type, _exc, _traceback):
                return False

        class Server:
            def __init__(self, _address, _handler, bind_and_activate):
                self.socket = Socket()
                self.outcome = None
                self.signal_state = None
                self.assert_false = bind_and_activate

            def set_shutdown_notification(self, requested):
                self.requested = requested
                return True

            def set_tls_context(self, context, *, handshake_timeout):
                self.context = context
                self.handshake_timeout = handshake_timeout
                return True

            def set_serve_lifecycle(self, outcome, signal_state):
                self.outcome = outcome
                self.signal_state = signal_state
                return True

            def claim_serving_readiness(self):
                return self.outcome.claim_ready(self.signal_state)

            def set_database_lifetime_guard(self, guard):
                self.guard = guard
                return True

            def server_bind(self):
                return None

            def server_activate(self):
                return None

            def serve_forever(self, *, poll_interval):
                self.poll_interval = poll_interval
                self.outcome.publish_serving_checkpoint(
                    self.signal_state
                )
                while not self.outcome.ready_state_reached:
                    pass
                self.signal_state.request("sigint")

            def server_close(self):
                self.socket.close()

        def prepare(_path, **kwargs):
            observed.update(kwargs)
            kwargs["_pre_secret_preparer"]()
            sink = kwargs["_callback_failure_telemetry_sink"]
            self.assertTrue(
                gateway_module._emit_google_callback_failure_stage_v1(
                    emitted_stage,
                    sink,
                )
            )
            self.assertEqual(stderr.flush_calls, 1)
            return Pending(kwargs["_cleanup_coordinator"])

        root = logging.getLogger()
        ambient = (logging.NullHandler(), logging.NullHandler())
        for handler in ambient:
            root.addHandler(handler)
        try:
            with (
                mock.patch.object(
                    runtime_module,
                    "prepare_durable_google_login_activation",
                    side_effect=prepare,
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = launcher.main(
                    ["--config", "synthetic-disposable-config.json"],
                    _server_factory=Server,
                    _tls_context_factory=TlsScope,
                )
        finally:
            for handler in ambient:
                root.removeHandler(handler)

        self.assertEqual(result, 130)
        self.assertIn("_callback_failure_telemetry_sink", observed)
        self.assertEqual(stderr.write_calls, [expected_line + "\n"])
        self.assertEqual(stderr.flush_calls, 1)
        self.assertEqual(
            stdout.getvalue(),
            "Wahojobs durable Google login\n"
            "Open: https://localhost:8443/login\n"
            "Press Ctrl+C to stop.\n"
            "Stopped durable Google login.\n",
        )
        self.assertNotIn("TOKEN", stderr.text())
        self.assertNotIn("provider", stderr.text().casefold())


if __name__ == "__main__":
    unittest.main()
