from contextlib import contextmanager, ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
from pathlib import Path
import re
import socket
import sqlite3
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

from scripts import local_product_app as local_product
from scripts import profile_to_matches_preview as profile_preview
from tests.test_canonical_profile_v2 import (
    load_cases,
    ordinal_resolver,
    persistent_id,
)
from tests.durable_google_login_browser_test_support import (
    cookie_header,
    cookie_values,
    form_body,
    https_request,
    loopback_and_in_memory_provider_only,
    provider_callback_for,
    running_https_production_launcher_app,
    temporary_browser_login_state,
)
from wahojobs import authenticated_profile_matches as matches_module
from wahojobs.authenticated_profile_matches import (
    AuthenticatedProfileMatchesBrowserIntegration,
    AuthenticatedProfileMatchesService,
    MatchesAuthorityResult,
)
from wahojobs.browser_session_authentication import (
    DurableBrowserSessionAuthenticationGateway,
)
from wahojobs.durable_google_login_runtime import (
    build_durable_google_login_runtime,
)
from wahojobs.matching.metadata_overlay import OpportunityMetadataOverlay
from wahojobs.persistent_profile_read_authorization import (
    DurablePersistentProfileReadAuthorizationGateway,
)
from wahojobs.profiles.canonical_v2 import convert_v1_to_v2


NOW = datetime(2026, 7, 25, 14, 0, 0, tzinfo=timezone.utc)
ORIGIN = "https://app.test"
SESSION_A = "a" * 43
SESSION_B = "b" * 43
SESSION_UNAUTHORIZED = "u" * 43
CSRF = "c" * 43
BINDING_A = "1" * 64
BINDING_B = "2" * 64
ORDINARY_FORM_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


def _unused_loopback_port():
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]
    finally:
        connection.close()


def _merge_cookies(cookies, response):
    for name, value in cookie_values(response).items():
        if value:
            cookies[name] = value
        else:
            cookies.pop(name, None)


def _all_table_counts(path):
    connection = sqlite3.connect(path)
    try:
        tables = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        return tuple(
            (
                table,
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0],
            )
            for table in tables
        )
    finally:
        connection.close()


def _seed_configured_inventory(path, *, observed_at):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        schema_path = (
            Path(__file__).resolve().parents[1] / "wahojobs" / "db" / "schema.sql"
        )
        connection.executescript(schema_path.read_text(encoding="utf-8"))
        observed = observed_at.isoformat()
        connection.execute(
            "INSERT INTO companies "
            "(id, name, slug, careers_url, source_tier, inventory_model, "
            "market_count_policy) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                7001,
                "Configured Production Inventory",
                "configured-production",
                "https://jobs.example.test/",
                "core",
                "live_feed",
                "count_live",
            ),
        )
        connection.execute(
            "INSERT INTO canonical_opportunities "
            "(id, company_id, canonical_key, canonical_title, normalized_title, "
            "source_category, language, language_locale, first_seen_at, "
            "last_seen_at, is_active, variant_count) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, 1, 1)",
            (
                7002,
                7001,
                "distinctive-bilingual-data-annotation-reviewer",
                "Distinctive Bilingual Data Annotation Reviewer",
                "distinctive bilingual data annotation reviewer",
                "Generalist",
                observed,
                observed,
            ),
        )
        connection.execute(
            "INSERT INTO jobs "
            "(id, company_id, canonical_opportunity_id, external_id, title, "
            "location, department, expertise, commitment, url, source_hash, "
            "opportunity_kind, availability_basis, "
            "include_in_live_market_estimate, first_seen_at, last_seen_at, "
            "is_active, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 1, ?)",
            (
                7003,
                7001,
                7002,
                "configured-distinctive-7003",
                "Distinctive Bilingual Data Annotation Reviewer",
                "Remote",
                "Generalist",
                "Generalist",
                "Freelance",
                "https://jobs.example.test/distinctive-bilingual-reviewer",
                "configured-source-hash-7003",
                "live_posting",
                "api_feed",
                observed,
                observed,
                observed,
            ),
        )
        connection.execute(
            "INSERT INTO crawl_runs "
            "(id, company_id, status, started_at, finished_at, jobs_found_count, "
            "used_sample_data, error_message) "
            "VALUES (?, ?, 'success', ?, ?, 1, 0, NULL)",
            (7004, 7001, observed, observed),
        )
        connection.commit()
    finally:
        connection.close()


class _ConfiguredReadOnlyProvider:
    def __init__(self, path):
        self.path = Path(path)
        self.calls = 0
        self.active_connection = None

    @contextmanager
    def __call__(self):
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro",
            uri=True,
            timeout=0.15,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA query_only = ON")
        self.calls += 1
        self.active_connection = connection
        try:
            yield connection
        finally:
            self.active_connection = None
            connection.close()


class _ReadProbe(io.BytesIO):
    def __init__(self, body, events):
        super().__init__(body)
        self.events = events
        self.read_count = 0

    def read(self, *args, **kwargs):
        self.events.append("body_read")
        self.read_count += 1
        return super().read(*args, **kwargs)


class AuthenticatedProfileMatchesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = load_cases()[12]
        cls.profile_v2 = convert_v1_to_v2(
            fixture["expected_canonical_profile"],
            persistent_profile_id=persistent_id(13),
            source_ordinal_resolver=ordinal_resolver,
        )

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database_path = Path(temporary.name) / "configured.sqlite"
        connection = sqlite3.connect(self.database_path)
        connection.execute("CREATE TABLE configured_marker (value TEXT NOT NULL)")
        connection.execute(
            "INSERT INTO configured_marker (value) VALUES ('configured-runtime')"
        )
        connection.commit()
        connection.close()
        self.provider = _ConfiguredReadOnlyProvider(self.database_path)
        self.artifacts = []
        self.replays = []

    def _integration(self, *, ephemeral_identity_factory=None):
        service = object.__new__(AuthenticatedProfileMatchesService)
        options = {}
        if ephemeral_identity_factory is not None:
            options["ephemeral_identity_factory"] = ephemeral_identity_factory
        integration = AuthenticatedProfileMatchesBrowserIntegration(
            service,
            connection_provider=self.provider,
            metadata_overlay=OpportunityMetadataOverlay(
                path=self.database_path.with_suffix(".overlay.json"),
                records_by_key={},
            ),
            confirmed_profile_artifact_sink=self.artifacts.append,
            completed_profile_confirmation_authenticator=self.replays.append,
            public_origin=ORIGIN,
            now=lambda: NOW,
            **options,
        )
        return service, integration

    @staticmethod
    def _authority(*, profile_v2=None, binding=BINDING_A):
        state = "profile" if profile_v2 is not None else "empty"
        return MatchesAuthorityResult(
            state,
            matches_module._AuthorizedMatchesState(
                state,
                draft_binding=binding,
                profile_v2=profile_v2,
            ),
        )

    @staticmethod
    def _headers(session=SESSION_A, *, post_body=None):
        headers = [
            ("Host", "app.test"),
            ("Cookie", f"wahojobs_session={session}"),
        ]
        if post_body is not None:
            headers = [
                ("Host", "app.test"),
                ("Origin", ORIGIN),
                ("Sec-Fetch-Site", "same-origin"),
                (
                    "Cookie",
                    f"wahojobs_session={session}; "
                    f"__Host-wahojobs_session_csrf={CSRF}",
                ),
                ("Content-Type", "application/x-www-form-urlencoded"),
                ("Content-Length", str(len(post_body))),
            ]
        return tuple(headers)

    @staticmethod
    def _row(
        *,
        job_id=901,
        title="Distinctive Python Backend AI Coding Evaluator",
        url="https://jobs.example.test/distinctive-python",
        observed_at=None,
    ):
        observed = (observed_at or NOW).isoformat()
        return {
            "job_id": job_id,
            "title": title,
            "canonical_title": None,
            "source": "Configured Inventory",
            "source_slug": "configured",
            "source_tier": "core",
            "location": "Remote",
            "url": url,
            "department": "Software Engineering",
            "expertise": "Software Engineering",
            "source_category": "Software Engineering",
            "commitment": "Freelance",
            "opportunity_kind": "live_posting",
            "availability_basis": "api_feed",
            "inventory_model": "live_feed",
            "market_count_policy": "count_live",
            "include_in_live_market_estimate": 1,
            "canonical_opportunity_id": job_id,
            "canonical_is_active": True,
            "job_is_active": True,
            "job_last_seen_at": observed,
            "latest_successful_source_run_at": observed,
            "source_run_started_at": observed,
            "source_run_id": job_id,
            "source_run_qualifies": True,
            "language": None,
            "language_locale": None,
            "required_languages": None,
        }

    def _query_rows(self, rows, calls):
        def query(connection):
            self.assertIs(connection, self.provider.active_connection)
            self.assertEqual(
                connection.execute("PRAGMA foreign_keys").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("PRAGMA query_only").fetchone()[0],
                1,
            )
            self.assertTrue(connection.in_transaction)
            calls.append(connection)
            return deepcopy(rows)

        return query

    @staticmethod
    def _body(response):
        return response.body.decode("utf-8")

    def test_production_launcher_profile_action_regenerates_configured_matches_without_writes(self):
        with ExitStack() as stack:
            state = stack.enter_context(
                temporary_browser_login_state(port=_unused_loopback_port())
            )
            stack.enter_context(loopback_and_in_memory_provider_only())
            _seed_configured_inventory(
                state.database_path,
                observed_at=state.clock(),
            )
            runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            self.addCleanup(state.close_harnesses)
            try:
                with running_https_production_launcher_app(runtime):
                    unauthenticated = https_request(
                        state,
                        "GET",
                        "/find-matches",
                    )
                    self.assertEqual(unauthenticated.status, 401)
                    cookies = {}
                    login = https_request(state, "GET", "/login")
                    self.assertEqual(login.status, 200)
                    _merge_cookies(cookies, login)
                    start = https_request(
                        state,
                        "POST",
                        "/auth/google/start",
                        headers=(
                            ("Origin", state.public_origin),
                            ("Sec-Fetch-Site", "same-origin"),
                            (
                                "Content-Type",
                                "application/x-www-form-urlencoded",
                            ),
                            ("Cookie", cookie_header(cookies)),
                        ),
                        body=form_body(
                            csrf=cookies["__Host-wahojobs_login_csrf"]
                        ),
                    )
                    self.assertEqual(start.status, 303)
                    _merge_cookies(cookies, start)
                    callback_url = provider_callback_for(
                        state,
                        start.header_values("Location")[0],
                        code="authenticated-matches-production-journey",
                    )
                    callback_parts = urlsplit(callback_url)
                    callback = https_request(
                        state,
                        "GET",
                        callback_parts.path + "?" + callback_parts.query,
                        headers=(("Cookie", cookie_header(cookies)),),
                    )
                    self.assertEqual(callback.status, 303)
                    self.assertEqual(
                        callback.header_values("Location"),
                        ("/account/profile",),
                    )
                    _merge_cookies(cookies, callback)

                    profile = https_request(
                        state,
                        "GET",
                        "/account/profile",
                        headers=(("Cookie", cookie_header(cookies)),),
                    )
                    self.assertEqual(profile.status, 200)
                    self.assertIn(
                        b"href='/find-matches'>Find matches</a>",
                        profile.body,
                    )
                    durable_counts = _all_table_counts(state.database_path)

                    first = https_request(
                        state,
                        "GET",
                        "/find-matches",
                        headers=(("Cookie", cookie_header(cookies)),),
                    )
                    counts_after_first = _all_table_counts(state.database_path)
                    head = https_request(
                        state,
                        "HEAD",
                        "/find-matches",
                        headers=(("Cookie", cookie_header(cookies)),),
                    )
                    counts_after_head = _all_table_counts(state.database_path)
                    regenerated = https_request(
                        state,
                        "GET",
                        "/find-matches",
                        headers=(("Cookie", cookie_header(cookies)),),
                    )
                    counts_after_regeneration = _all_table_counts(
                        state.database_path
                    )

                    first_session = cookies["wahojobs_session"]
                    logout = https_request(
                        state,
                        "POST",
                        "/logout",
                        headers=(
                            ("Origin", state.public_origin),
                            ("Sec-Fetch-Site", "same-origin"),
                            ("Content-Type", "application/x-www-form-urlencoded"),
                            ("Cookie", cookie_header(cookies)),
                        ),
                        body=form_body(
                            csrf=cookies["__Host-wahojobs_session_csrf"]
                        ),
                    )
                    self.assertEqual(logout.status, 303)
                    _merge_cookies(cookies, logout)
                    logged_out = https_request(
                        state,
                        "GET",
                        "/find-matches",
                        headers=(("Cookie", f"wahojobs_session={first_session}"),),
                    )
                    self.assertEqual(logged_out.status, 401)

                    expired_cookies = {}
                    login_again = https_request(state, "GET", "/login")
                    _merge_cookies(expired_cookies, login_again)
                    start_again = https_request(
                        state,
                        "POST",
                        "/auth/google/start",
                        headers=(
                            ("Origin", state.public_origin),
                            ("Sec-Fetch-Site", "same-origin"),
                            ("Content-Type", "application/x-www-form-urlencoded"),
                            ("Cookie", cookie_header(expired_cookies)),
                        ),
                        body=form_body(
                            csrf=expired_cookies["__Host-wahojobs_login_csrf"]
                        ),
                    )
                    _merge_cookies(expired_cookies, start_again)
                    expired_callback_url = provider_callback_for(
                        state,
                        start_again.header_values("Location")[0],
                        code="authenticated-matches-expired-session",
                    )
                    expired_parts = urlsplit(expired_callback_url)
                    expired_callback = https_request(
                        state,
                        "GET",
                        expired_parts.path + "?" + expired_parts.query,
                        headers=(("Cookie", cookie_header(expired_cookies)),),
                    )
                    self.assertEqual(expired_callback.status, 303)
                    _merge_cookies(expired_cookies, expired_callback)
                    state.clock.advance(3_600)
                    expired = https_request(
                        state,
                        "GET",
                        "/find-matches",
                        headers=(("Cookie", cookie_header(expired_cookies)),),
                    )
                    self.assertEqual(expired.status, 401)

                self.assertEqual((first.status, head.status, regenerated.status), (200, 200, 200))
                self.assertEqual(head.body, b"")
                self.assertEqual(
                    (
                        durable_counts,
                        counts_after_first,
                        counts_after_head,
                        counts_after_regeneration,
                    ),
                    (durable_counts,) * 4,
                )
                self.assertEqual(first.body, regenerated.body)
                body = first.body.decode("utf-8")
                self.assertIn(
                    "Distinctive Bilingual Data Annotation Reviewer",
                    body,
                )
                self.assertIn(
                    "href='/job/configured-production-7003'",
                    body,
                )
                self.assertNotIn("href='https://jobs.example.test/distinctive-bilingual-reviewer'", body)
                self.assertEqual(
                    re.findall(r"href='([^']+)'", body),
                    [
                        "/account/profile",
                        "/logout",
                        "/job/configured-production-7003",
                    ],
                )
                self.assertNotIn(state.profile_id, body)
                for forbidden in (
                    "My Jobs",
                    "/action",
                    "tracker",
                    "demo persona",
                    "javascript:",
                ):
                    self.assertNotIn(forbidden, body)
            finally:
                report = runtime.close()
                self.assertTrue(report.cleanup_complete)

    def test_saved_v2_profile_uses_only_configured_inventory_and_renders_safe_minimal_results(self):
        safe = self._row()
        unsafe = self._row(
            job_id=902,
            title="Unsafe Protocol Python Evaluator",
            url="javascript:alert(1)",
        )
        query_calls = []
        authority = self._authority(profile_v2=self.profile_v2)
        real_projection = matches_module.project_v2_to_matcher_v1
        fallback_error = AssertionError("local/default inventory fallback used")

        with (
            mock.patch.object(matches_module.secrets, "token_hex", return_value="d" * 32),
            mock.patch.object(
                AuthenticatedProfileMatchesService,
                "resolve",
                return_value=authority,
            ) as resolve,
            mock.patch.object(
                profile_preview,
                "query_preview_rows",
                side_effect=self._query_rows([safe, unsafe], query_calls),
            ),
            mock.patch.object(
                matches_module,
                "project_v2_to_matcher_v1",
                wraps=real_projection,
            ) as project,
            mock.patch.object(
                profile_preview,
                "load_preview_rows",
                side_effect=fallback_error,
            ) as local_loader,
            mock.patch.object(
                profile_preview,
                "get_connection",
                side_effect=fallback_error,
            ) as default_connection,
            mock.patch.object(
                profile_preview,
                "load_overlay",
                side_effect=fallback_error,
            ) as default_overlay,
            mock.patch.object(
                local_product,
                "get_connection",
                side_effect=fallback_error,
            ) as local_connection,
            mock.patch.object(
                local_product,
                "load_preview_tracked",
                side_effect=fallback_error,
            ) as tracked_loader,
            mock.patch.object(
                local_product,
                "render_preview_from_params",
                side_effect=fallback_error,
            ) as legacy_renderer,
        ):
            _service, integration = self._integration()
            response = integration.handle(
                "GET",
                "/find-matches",
                self._headers(),
            )

        body = self._body(response)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.provider.calls, 1)
        self.assertEqual(len(query_calls), 1)
        resolve.assert_called_once()
        project.assert_called_once()
        self.assertEqual(project.call_args.args[0], self.profile_v2)
        self.assertEqual(
            project.call_args.kwargs["matcher_profile_id"],
            "matcher-" + ("d" * 32),
        )
        self.assertNotIn(self.profile_v2["identity"]["profile_id"], body)
        self.assertIn("Distinctive Python Backend AI Coding Evaluator", body)
        self.assertIn("href='/job/configured-901'", body)
        self.assertNotIn("href='https://jobs.example.test/distinctive-python'", body)
        self.assertNotIn("javascript:", body)
        self.assertNotIn("Unsafe Protocol Python Evaluator", body)
        self.assertEqual(
            re.findall(r"href='([^']+)'", body),
            [
                "/account/profile",
                "/logout",
                "/job/configured-901",
            ],
        )
        for forbidden in ("My Jobs", "/action", "tracker", "demo persona"):
            self.assertNotIn(forbidden, body)
        for fallback in (
            local_loader,
            default_connection,
            default_overlay,
            local_connection,
            tracked_loader,
            legacy_renderer,
        ):
            fallback.assert_not_called()

    def test_empty_and_untrusted_configured_inventory_are_reported_honestly(self):
        stale = self._row(
            observed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        cases = (
            (
                [],
                "No current opportunities are available",
                "configured opportunity inventory is empty",
            ),
            (
                [stale],
                "No sufficiently trusted matches are available",
                "No alternate inventory was used",
            ),
        )
        authority = self._authority(profile_v2=self.profile_v2)

        for rows, heading, detail in cases:
            with self.subTest(heading=heading):
                query_calls = []
                _service, integration = self._integration(
                    ephemeral_identity_factory=lambda: "ephemeral_matcher"
                )
                with (
                    mock.patch.object(
                        AuthenticatedProfileMatchesService,
                        "resolve",
                        return_value=authority,
                    ),
                    mock.patch.object(
                        profile_preview,
                        "query_preview_rows",
                        side_effect=self._query_rows(rows, query_calls),
                    ),
                ):
                    response = integration.handle(
                        "GET",
                        "/find-matches",
                        self._headers(),
                    )

                body = self._body(response)
                self.assertEqual(response.status, 200)
                self.assertEqual(len(query_calls), 1)
                self.assertIn(heading, body)
                self.assertIn(detail, body)
                self.assertNotIn("View opportunity", body)

    def test_persistent_get_and_head_leave_configured_database_byte_identical(self):
        rows = [self._row()]
        query_calls = []
        authority = self._authority(profile_v2=self.profile_v2)
        before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        _service, integration = self._integration(
            ephemeral_identity_factory=lambda: "ephemeral_matcher"
        )

        with (
            mock.patch.object(
                AuthenticatedProfileMatchesService,
                "resolve",
                return_value=authority,
            ),
            mock.patch.object(
                profile_preview,
                "query_preview_rows",
                side_effect=self._query_rows(rows, query_calls),
            ),
        ):
            get_response = integration.handle(
                "GET",
                "/find-matches",
                self._headers(),
            )
            head_response = integration.handle(
                "HEAD",
                "/find-matches",
                self._headers(),
            )

        after = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        self.assertEqual((get_response.status, head_response.status), (200, 200))
        self.assertEqual(before, after)
        self.assertEqual(len(query_calls), 2)
        self.assertEqual(self.provider.calls, 2)
        check = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                check.execute("SELECT value FROM configured_marker").fetchone()[0],
                "configured-runtime",
            )
        finally:
            check.close()

    def test_service_rejects_non_query_only_connection_before_authentication(self):
        @contextmanager
        def read_write_provider():
            connection = sqlite3.connect(self.database_path)
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                yield connection
            finally:
                connection.close()

        authentication = DurableBrowserSessionAuthenticationGateway(
            trusted_environment_namespace="test",
            clock=lambda: NOW,
        )
        service = AuthenticatedProfileMatchesService(
            authentication_gateway=authentication,
            authorization_gateway=DurablePersistentProfileReadAuthorizationGateway(),
            connection_provider=read_write_provider,
            clock=lambda: NOW,
            binding_secret=b"binding-secret-for-matches-tests-0001",
        )
        before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()

        with mock.patch.object(
            DurableBrowserSessionAuthenticationGateway,
            "authenticate_browser_request",
            side_effect=AssertionError("authentication must not run"),
        ) as authenticate:
            result = service.resolve(
                method="GET",
                authentication_input=self._headers(),
                session_token=SESSION_A,
            )

        self.assertEqual(result.state, "schema_unavailable")
        authenticate.assert_not_called()
        self.assertEqual(
            hashlib.sha256(self.database_path.read_bytes()).hexdigest(),
            before,
        )

    def test_host_proxy_and_browser_selected_identity_fail_before_body_or_action(self):
        _service, integration = self._integration()
        encoded = urlencode(
            {
                "input_text": "Remote bilingual reviewer",
                "input_style": "short_paragraph",
            }
        ).encode("ascii")
        boundary_cases = (
            (("Host", "attacker.test"),),
            (
                ("Host", "app.test"),
                ("X-Forwarded-Host", "attacker.test"),
            ),
        )
        for headers in boundary_cases:
            with self.subTest(headers=headers):
                stream = _ReadProbe(encoded, [])
                with mock.patch.object(
                    AuthenticatedProfileMatchesService,
                    "resolve",
                    side_effect=AssertionError("authority must not run"),
                ) as resolve:
                    response = integration.handle(
                        "POST",
                        "/find-matches",
                        headers,
                        stream,
                    )
                self.assertEqual(response.status, 400)
                self.assertEqual(stream.read_count, 0)
                resolve.assert_not_called()

        authority = self._authority(profile_v2=self.profile_v2)
        selected_identity_targets = (
            "/find-matches?profile_id=prf_0123456789abcdef0123456789abcdef",
            "/find-matches?account_id=acct_browser_selected",
            "/find-matches?principal_id=prn_browser_selected",
            "/find-matches?matcher_id=browser-selected",
            "/find-matches?persona=demo",
            "/find-matches?legacy_username=local_user",
        )
        with mock.patch.object(
            AuthenticatedProfileMatchesService,
            "resolve",
            return_value=authority,
        ) as resolve:
            for target in selected_identity_targets:
                with self.subTest(target=target):
                    response = integration.handle(
                        "GET",
                        target,
                        self._headers(),
                    )
                    self.assertEqual(response.status, 400)
        self.assertEqual(resolve.call_count, 0)

    def test_post_authorizes_before_body_and_drafts_are_bound_to_authority(self):
        _service, integration = self._integration()
        authority_a = self._authority(binding=BINDING_A)
        authority_b = self._authority(binding=BINDING_B)
        events = []

        def resolve(**kwargs):
            session = kwargs["session_token"]
            events.append(f"authority:{session[0]}")
            if session == SESSION_UNAUTHORIZED:
                return MatchesAuthorityResult("authentication_required")
            if session == SESSION_A:
                return authority_a
            if session == SESSION_B:
                return authority_b
            raise AssertionError("unexpected session")

        raw_input = (
            "I speak English and Spanish and have software testing and "
            "Python review experience."
        )
        encoded = urlencode(
            {
                "input_text": raw_input,
                "input_style": "short_paragraph",
            }
        ).encode("ascii")
        unauthorized_stream = _ReadProbe(encoded, events)
        authorized_stream = _ReadProbe(encoded, events)
        real_normalize = local_product.normalize_identity_free_profile_input

        def normalize(*args, **kwargs):
            events.append("draft_action")
            return real_normalize(*args, **kwargs)

        with (
            mock.patch.object(
                AuthenticatedProfileMatchesService,
                "resolve",
                side_effect=resolve,
            ),
            mock.patch.object(
                local_product,
                "normalize_identity_free_profile_input",
                side_effect=normalize,
            ) as normalize_call,
        ):
            empty_entry = integration.handle(
                "GET",
                "/find-matches",
                self._headers(SESSION_A),
            )
            self.assertEqual(empty_entry.status, 200)
            self.assertEqual(
                dict(empty_entry.headers)["Referrer-Policy"],
                "same-origin",
            )
            self.assertEqual(
                dict(empty_entry.headers)["Content-Security-Policy"],
                ORDINARY_FORM_CONTENT_SECURITY_POLICY,
            )

            unauthorized = integration.handle(
                "POST",
                "/find-matches",
                self._headers(SESSION_UNAUTHORIZED, post_body=encoded),
                unauthorized_stream,
            )
            self.assertEqual(unauthorized.status, 401)
            self.assertEqual(unauthorized_stream.read_count, 0)
            normalize_call.assert_not_called()

            events.clear()
            created = integration.handle(
                "POST",
                "/find-matches",
                self._headers(SESSION_A, post_body=encoded),
                authorized_stream,
            )
            self.assertEqual(created.status, 303)
            self.assertEqual(events[:3], ["authority:a", "body_read", "draft_action"])
            location = dict(created.headers)["Location"]
            run_id = parse_qs(urlsplit(location).query)["run"][0]
            run = integration._registry.get(run_id)
            self.assertEqual(run.owner_profile_id, BINDING_A)

            wrong_authority = integration.handle(
                "GET",
                location,
                self._headers(SESSION_B),
            )
            correct_authority = integration.handle(
                "GET",
                location,
                self._headers(SESSION_A),
            )
            edit_authority = integration.handle(
                "GET",
                "/find-matches?" + urlencode(
                    {"run": run_id, "edit_text": "1"}
                ),
                self._headers(SESSION_A),
            )

        self.assertEqual(wrong_authority.status, 410)
        self.assertEqual(
            dict(wrong_authority.headers)["Referrer-Policy"],
            "no-referrer",
        )
        self.assertNotIn(raw_input, self._body(wrong_authority))
        self.assertEqual(correct_authority.status, 200)
        self.assertEqual(
            dict(correct_authority.headers)["Referrer-Policy"],
            "same-origin",
        )
        self.assertEqual(
            dict(correct_authority.headers)["Content-Security-Policy"],
            ORDINARY_FORM_CONTENT_SECURITY_POLICY,
        )
        self.assertIn("Make sure we understood you", self._body(correct_authority))
        self.assertEqual(edit_authority.status, 200)
        self.assertEqual(
            dict(edit_authority.headers)["Referrer-Policy"],
            "same-origin",
        )
        self.assertEqual(
            dict(edit_authority.headers)["Content-Security-Policy"],
            ORDINARY_FORM_CONTENT_SECURITY_POLICY,
        )
        self.assertEqual(self.artifacts, [])
        self.assertEqual(self.replays, [])


if __name__ == "__main__":
    unittest.main()
