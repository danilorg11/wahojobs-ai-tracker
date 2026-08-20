from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import unittest

from scripts.workos_authkit_staging_app import run_staging_rehearsal
from scripts.public_job_identity_migration import (
    apply_public_job_identity_migration,
)
from tests.closed_schema_convergence_test_support import (
    apply_m007,
    build_fresh_m001_m006,
)
from tests.workos_authkit_test_support import FakeWorkOSBoundary, build_m008
from wahojobs.workos_authkit_staging import (
    STAGING_PUBLIC_ORIGIN,
    STAGING_REDIRECT_URI,
    WorkOSAuthKitStagingError,
    apply_m008_to_explicit_database,
    build_workos_authkit_staging_runtime,
    load_workos_authkit_staging_configuration,
)
from wahojobs.workos_authkit_schema import attest_workos_authkit_schema
from wahojobs.public_job_identity import PublicJobIdAllocator, allocate_public_job


ROOT = Path(__file__).resolve().parents[1]
CLIENT_ID = "client_0123456789abcdef"
CANARY_PUBLIC_JOB_ID = "j" + "33" * 16


class WorkOSAuthKitStagingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="wahojobs-authkit-staging-test-"
        )
        self.root = Path(self.directory.name).resolve()
        self.database_path = self.root / "private-beta.sqlite3"
        connection = build_m008(self.database_path)
        connection.close()
        self.api_key = "sk_test_" + secrets.token_urlsafe(32)
        self.invitation_key = secrets.token_bytes(32)
        self.config_path = self.root / "authkit-staging.json"
        self._write_configuration()

    def tearDown(self):
        self.directory.cleanup()

    def _document(self):
        return {
            "version": 1,
            "environment_namespace": "private_beta",
            "database_path": str(self.database_path),
            "public_origin": STAGING_PUBLIC_ORIGIN,
            "redirect_uri": STAGING_REDIRECT_URI,
            "workos_client_id": CLIENT_ID,
            "workos_api_key": self.api_key,
            "wahojobs_invitation_lookup_key_base64": base64.b64encode(
                self.invitation_key
            ).decode("ascii"),
            "session_idle_ttl_seconds": 3600,
            "session_absolute_ttl_seconds": 28800,
        }

    def _write_configuration(self, document=None, *, raw=None):
        if raw is None:
            raw = json.dumps(
                self._document() if document is None else document,
                separators=(",", ":"),
            )
        self.config_path.write_text(raw, encoding="utf-8")
        if os.name != "nt":
            self.config_path.chmod(0o600)

    def test_exact_external_configuration_loads_and_repr_is_redacted(self):
        configuration = load_workos_authkit_staging_configuration(
            str(self.config_path)
        )
        rendered = repr(configuration)
        self.assertEqual(configuration.database_path, self.database_path)
        self.assertEqual(configuration.public_origin, STAGING_PUBLIC_ORIGIN)
        self.assertEqual(configuration.redirect_uri, STAGING_REDIRECT_URI)
        self.assertEqual(configuration.environment_namespace, "private_beta")
        self.assertEqual(configuration.session_idle_ttl, timedelta(hours=1))
        self.assertEqual(configuration.session_absolute_ttl, timedelta(hours=8))
        self.assertFalse(configuration.public_job_canary_gate.enabled)
        self.assertEqual(
            configuration.public_job_canary_gate.public_job_ids,
            frozenset(),
        )
        self.assertNotIn(self.api_key, rendered)
        self.assertNotIn(base64.b64encode(self.invitation_key).decode("ascii"), rendered)
        configuration.clear_secrets()
        self.assertIsNone(configuration.workos_api_key)
        self.assertIsNone(configuration.invitation_lookup_key)

    def test_configuration_is_strict_and_has_no_fallbacks(self):
        invalid_documents = []
        for field, value in (
            ("environment_namespace", "staging"),
            ("public_origin", "https://localhost:8443"),
            ("redirect_uri", "https://127.0.0.1:8443/auth/google/callback"),
            ("workos_client_id", "not-a-client"),
            ("workos_api_key", "short"),
            ("wahojobs_invitation_lookup_key_base64", "not-base64"),
            ("session_idle_ttl_seconds", True),
            ("session_absolute_ttl_seconds", 59),
        ):
            document = self._document()
            document[field] = value
            invalid_documents.append(document)
        unknown = self._document()
        unknown["fallback_database"] = "forbidden"
        invalid_documents.append(unknown)
        missing = self._document()
        missing.pop("database_path")
        invalid_documents.append(missing)
        for invalid_allowlist in (
            None,
            "*",
            ["*"],
            ["/job/dormant-m009-canary"],
            [9902],
            [CANARY_PUBLIC_JOB_ID, CANARY_PUBLIC_JOB_ID],
            {"public_job_id": CANARY_PUBLIC_JOB_ID},
        ):
            document = self._document()
            document["public_job_canary_ids"] = invalid_allowlist
            invalid_documents.append(document)

        for document in invalid_documents:
            with self.subTest(fields=tuple(document)):
                self._write_configuration(document)
                with self.assertRaises(WorkOSAuthKitStagingError):
                    load_workos_authkit_staging_configuration(str(self.config_path))

        duplicate = json.dumps(self._document())[:-1] + ',"version":1}'
        self._write_configuration(raw=duplicate)
        with self.assertRaises(WorkOSAuthKitStagingError):
            load_workos_authkit_staging_configuration(str(self.config_path))
        with self.assertRaises(WorkOSAuthKitStagingError):
            load_workos_authkit_staging_configuration("relative.json")

    def test_explicit_empty_or_exact_id_canary_allowlist_loads_strictly(self):
        empty = self._document()
        empty["public_job_canary_ids"] = []
        self._write_configuration(empty)
        configuration = load_workos_authkit_staging_configuration(
            str(self.config_path)
        )
        self.assertFalse(configuration.public_job_canary_gate.enabled)
        configuration.clear_secrets()

        enabled = self._document()
        enabled["public_job_canary_ids"] = [CANARY_PUBLIC_JOB_ID]
        self._write_configuration(enabled)
        configuration = load_workos_authkit_staging_configuration(
            str(self.config_path)
        )
        self.assertTrue(configuration.public_job_canary_gate.enabled)
        self.assertEqual(
            configuration.public_job_canary_gate.public_job_ids,
            frozenset({CANARY_PUBLIC_JOB_ID}),
        )
        configuration.clear_secrets()

    def test_nonempty_canary_allowlist_requires_exact_m009_before_provider(self):
        document = self._document()
        document["public_job_canary_ids"] = [CANARY_PUBLIC_JOB_ID]
        self._write_configuration(document)
        configuration = load_workos_authkit_staging_configuration(
            str(self.config_path)
        )
        provider_calls = []

        def forbidden_provider(**_kwargs):
            provider_calls.append(True)
            raise AssertionError("provider_must_not_be_constructed")

        with self.assertRaises(WorkOSAuthKitStagingError) as caught:
            build_workos_authkit_staging_runtime(
                configuration,
                sdk_boundary_factory=forbidden_provider,
            )
        self.assertEqual(caught.exception.code, "database_m008_required")
        self.assertEqual(provider_calls, [])

    def test_runtime_rejects_m007_before_provider_construction(self):
        m007_path = self.root / "m007.sqlite3"
        connection = build_fresh_m001_m006(m007_path)
        apply_m007(connection, m007_path)
        connection.close()
        document = self._document()
        document["database_path"] = str(m007_path)
        self._write_configuration(document)
        configuration = load_workos_authkit_staging_configuration(
            str(self.config_path)
        )
        provider_calls = []

        def forbidden_provider(**_kwargs):
            provider_calls.append(True)
            raise AssertionError("provider_must_not_be_constructed")

        with self.assertRaises(WorkOSAuthKitStagingError) as caught:
            build_workos_authkit_staging_runtime(
                configuration,
                sdk_boundary_factory=forbidden_provider,
            )
        self.assertEqual(caught.exception.code, "database_m008_required")
        self.assertEqual(provider_calls, [])
        self.assertIsNone(configuration.workos_api_key)
        self.assertIsNone(configuration.invitation_lookup_key)

    def test_runtime_composes_existing_routes_with_fake_provider_and_closes(self):
        configuration = load_workos_authkit_staging_configuration(
            str(self.config_path)
        )
        boundary = FakeWorkOSBoundary()
        captured = []

        def fake_sdk_boundary_factory(*, api_key, client_id):
            captured.append((len(api_key), client_id))
            return boundary

        runtime = build_workos_authkit_staging_runtime(
            configuration,
            sdk_boundary_factory=fake_sdk_boundary_factory,
        )
        try:
            for route in (
                "/login",
                "/auth/workos/start",
                "/auth/workos/callback",
                "/logout",
                "/account/profile",
                "/find-matches",
                "/tracker",
                "/action",
            ):
                self.assertTrue(runtime.browser_integration.matches_route(route))
            self.assertFalse(runtime.browser_integration.matches_route("/auth/google/start"))
            self.assertEqual(captured, [(len(self.api_key), CLIENT_ID)])
            self.assertEqual(boundary.authorization_count, 0)
            self.assertEqual(boundary.exchange_count, 0)
            self.assertNotIn(self.api_key, repr(runtime))
        finally:
            self.assertTrue(runtime.close())
        self.assertTrue(runtime.close())

    def test_exact_m009_runtime_is_compatible_but_canary_gate_stays_disabled(self):
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            apply_public_job_identity_migration(connection)
            connection.execute(
                "INSERT INTO companies "
                "(id,name,slug,careers_url,source_tier,inventory_model,"
                "market_count_policy) VALUES "
                "(9901,'Disposable Canary','disposable-canary',"
                "'https://example.test/','core','live_feed','count_live')"
            )
            connection.execute(
                "INSERT INTO canonical_opportunities "
                "(id,company_id,canonical_key,canonical_title,normalized_title,"
                "source_category,first_seen_at,last_seen_at,is_active,variant_count) "
                "VALUES (9902,9901,'disposable-canary','Disposable Canary',"
                "'disposable canary','Generalist','2026-08-20T00:00:00+00:00',"
                "'2026-08-20T00:00:00+00:00',1,0)"
            )
            allocation = allocate_public_job(
                connection,
                allocator=PublicJobIdAllocator(
                    "disposable-staging-test",
                    random_source=lambda size: bytes.fromhex("33" * size),
                ),
                company_slug="disposable-canary",
                canonical_title="Disposable Canary",
                canonical_opportunity_id=9902,
                primary_path="/job/dormant-m009-canary",
                now=datetime(2026, 8, 20, tzinfo=timezone.utc),
            )
            connection.commit()
        finally:
            connection.close()
        configuration = load_workos_authkit_staging_configuration(
            str(self.config_path)
        )
        runtime = build_workos_authkit_staging_runtime(
            configuration,
            sdk_boundary_factory=lambda **_kwargs: FakeWorkOSBoundary(),
        )
        try:
            self.assertTrue(runtime.browser_integration.matches_route("/find-matches"))
            self.assertTrue(
                runtime.browser_integration.matches_route("/job/opportunity-7002")
            )
            self.assertFalse(
                runtime.browser_integration.matches_route("/job/dormant-m009-canary")
            )
        finally:
            self.assertTrue(runtime.close())

        document = self._document()
        document["public_job_canary_ids"] = [allocation.public_job_id]
        self._write_configuration(document)
        configuration = load_workos_authkit_staging_configuration(
            str(self.config_path)
        )
        runtime = build_workos_authkit_staging_runtime(
            configuration,
            sdk_boundary_factory=lambda **_kwargs: FakeWorkOSBoundary(),
        )
        try:
            self.assertTrue(
                runtime.browser_integration.matches_route(
                    "/job/dormant-m009-canary"
                )
            )
        finally:
            self.assertTrue(runtime.close())

    def test_launcher_owns_start_and_shutdown_without_network_or_provider(self):
        events = []

        class FakeIntegration:
            def handle(self, _method, _target, _headers, _stream):
                raise AssertionError("no_request_expected")

        class FakeRuntime:
            browser_integration = FakeIntegration()
            bind_address = ("127.0.0.1", 8443)
            public_origin = STAGING_PUBLIC_ORIGIN

            def close(self):
                events.append("runtime_closed")
                return True

        class FakeTlsScope:
            def build_context(self):
                events.append("tls_built")
                return object()

            def close(self):
                events.append("tls_closed")
                return True

        class FakeServer:
            def __init__(self, address, _handler, _context):
                events.append(("server_created", address))

            def serve_forever(self, *, poll_interval):
                events.append(("served", poll_interval))

            def server_close(self):
                events.append("server_closed")
                return True

        def fake_runtime_builder(configuration):
            events.append(("runtime_built", configuration.bind_address))
            return FakeRuntime()

        result = run_staging_rehearsal(
            str(self.config_path),
            runtime_builder=fake_runtime_builder,
            tls_scope_factory=FakeTlsScope,
            server_factory=FakeServer,
            ready=lambda origin: events.append(("ready", origin)),
        )
        self.assertTrue(result)
        self.assertEqual(
            events,
            [
                ("runtime_built", ("127.0.0.1", 8443)),
                "tls_built",
                ("server_created", ("127.0.0.1", 8443)),
                ("ready", STAGING_PUBLIC_ORIGIN),
                ("served", 0.2),
                "server_closed",
                "runtime_closed",
                "tls_closed",
            ],
        )

    def test_tls_failure_is_sanitized_and_closes_the_runtime(self):
        events = []

        class FakeIntegration:
            def handle(self, _method, _target, _headers, _stream):
                raise AssertionError("no_request_expected")

        class FakeRuntime:
            browser_integration = FakeIntegration()
            bind_address = ("127.0.0.1", 8443)
            public_origin = STAGING_PUBLIC_ORIGIN

            def close(self):
                events.append("runtime_closed")
                return True

        def fail_tls():
            raise RuntimeError(self.api_key)

        with self.assertRaises(WorkOSAuthKitStagingError) as caught:
            run_staging_rehearsal(
                str(self.config_path),
                runtime_builder=lambda _configuration: FakeRuntime(),
                tls_scope_factory=fail_tls,
                server_factory=lambda *_args: None,
                ready=lambda _origin: None,
            )
        self.assertEqual(caught.exception.code, "tls_unavailable")
        self.assertNotIn(self.api_key, repr(caught.exception) + str(caught.exception))
        self.assertEqual(events, ["runtime_closed"])

    def test_existing_ephemeral_tls_scope_builds_and_cleans_without_listener(self):
        from scripts.durable_google_login_app import _ephemeral_tls_context

        scope = _ephemeral_tls_context()
        try:
            context = scope.build_context()
            self.assertIsInstance(context, ssl.SSLContext)
            self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        finally:
            self.assertTrue(scope.close())

    def test_explicit_offline_command_applies_m008_and_is_idempotent(self):
        path = self.root / "migration-target.sqlite3"
        connection = build_fresh_m001_m006(path)
        apply_m007(connection, path)
        connection.close()
        first = apply_m008_to_explicit_database(str(path))
        second = apply_m008_to_explicit_database(str(path))
        self.assertTrue(first["applied"])
        self.assertFalse(second["applied"])
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            self.assertEqual(
                attest_workos_authkit_schema(connection)["state"],
                "correctly_installed",
            )
        finally:
            connection.close()

    def test_imports_are_inert_and_do_not_construct_provider_or_runtime(self):
        source = r'''
import os
from pathlib import Path
import http.server
import socket
import sqlite3
import sys
import threading
import types

events = []

def poison(name):
    def fail(*_args, **_kwargs):
        events.append(name)
        raise AssertionError(name)
    return fail

sqlite3.connect = poison("database")
socket.socket = poison("socket")
threading.Thread = poison("thread")
provider = types.ModuleType("workos")
provider.WorkOSClient = poison("provider")
sys.modules["workos"] = provider

before = set(Path.cwd().iterdir())
import wahojobs.workos_authkit_staging
import wahojobs.public_job_identity_schema
import wahojobs.public_job_canary
import scripts.public_job_identity_migration
import scripts.workos_authkit_staging_app
import scripts.workos_authkit_staging_migrate
after = set(Path.cwd().iterdir())
print(events, before == after)
'''
        with tempfile.TemporaryDirectory(
            prefix="wahojobs-authkit-staging-import-"
        ) as directory:
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(ROOT)
            result = subprocess.run(
                [sys.executable, "-B", "-c", source],
                cwd=directory,
                env=environment,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[] True")

    def test_direct_cli_entrypoints_are_explicit_and_importable(self):
        for script, required in (
            ("scripts/workos_authkit_staging_app.py", "--config"),
            ("scripts/workos_authkit_staging_migrate.py", "--database"),
        ):
            with self.subTest(script=script):
                result = subprocess.run(
                    [sys.executable, "-B", script, "--help"],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(required, result.stdout)


if __name__ == "__main__":
    unittest.main()
