from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import hashlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

from scripts import local_product_app as review_support
from scripts.local_product_app import (
    apply_identity_free_profile_review,
    profile_review_form_fields,
    profile_review_language_slots,
    profile_review_updates_from_form,
)
from tests.browser_session_authentication_test_support import (
    REQUEST_AT,
    install_browser_authentication_database,
    seed_browser_session,
)
from tests.persistent_profiles_repository_test_support import (
    append_command,
    create_command,
    reference,
)
from tests.test_canonical_profile_v2 import load_cases, ordinal_resolver
from wahojobs import accounts
from wahojobs import persistent_profile_corrections as corrections_module
from wahojobs.authenticated_profile_matches import (
    AuthenticatedProfileMatchesBrowserIntegration,
    AuthenticatedProfileMatchesService,
)
from wahojobs.browser_session_authentication import (
    DurableBrowserSessionAuthenticationGateway,
)
from wahojobs.persistent_profile_corrections import (
    PROFILE_CORRECTION_ARTIFACT_CAPACITY,
    PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS,
    PROFILE_CORRECTION_PURPOSE,
    ConfirmedProfileCorrectionArtifactVault,
    PersistentProfileCorrectionService,
    profile_correction_action_csrf_proof,
)
from wahojobs.persistent_profile_creation import profile_create_csrf_proof
from wahojobs.persistent_profile_read_authorization import (
    DurablePersistentProfileReadAuthorizationGateway,
)
from wahojobs.persistent_profiles_application import (
    PersistentProfileApplicationService,
)
from wahojobs.persistent_profiles_browser import (
    MAX_PROFILE_CORRECTION_BODY_BYTES,
    PersistentProfileBrowserIntegration,
)
from wahojobs.persistent_profiles import IdentityFreeCanonicalProfileV1
from wahojobs.persistent_profiles_repository import (
    PersistentProfileRepository,
    PersistentProfileRepositoryOutcomeUncertain,
    append_profile_revision,
    create_persistent_profile,
    read_current_profile,
)
from wahojobs.matching.metadata_overlay import OpportunityMetadataOverlay
from wahojobs.profiles.canonical_v2 import (
    canonical_profile_v2_json_bytes,
    convert_v1_to_v2,
    validate_canonical_profile_v2,
)
from wahojobs.profiles.canonical import (
    PROFILE_SOURCE_EXTERNAL,
    field_sources_for_profile,
    validate_canonical_profile,
)


PUBLIC_ORIGIN = "https://app.test"
PUBLIC_AUTHORITY = "app.test"
ORDINARY_FORM_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


@contextmanager
def sockets_blocked():
    def deny(*_args, **_kwargs):
        raise AssertionError("live_socket_access_forbidden")

    with (
        mock.patch.object(socket, "socket", deny),
        mock.patch.object(socket, "create_connection", deny),
        mock.patch.object(socket, "getaddrinfo", deny),
    ):
        yield


class _ReadProvider:
    def __init__(self, path, *, timeout=3.0):
        self.path = Path(path)
        self.timeout = timeout

    def __call__(self):
        @contextmanager
        def scope():
            connection = sqlite3.connect(
                f"file:{self.path.as_posix()}?mode=ro",
                uri=True,
                timeout=self.timeout,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA query_only = ON")
            try:
                yield connection
            finally:
                connection.close()

        return scope()


class _WriteProvider:
    def __init__(self, path, *, timeout=3.0):
        self.path = Path(path)
        self.timeout = timeout

    def __call__(self):
        @contextmanager
        def scope():
            connection = sqlite3.connect(self.path, timeout=self.timeout)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                yield connection
            finally:
                connection.close()

        return scope()


class _TokenFactory:
    def __init__(self):
        self.value = 0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            self.value += 1
            return str(self.value).rjust(43, "0")


class _ReadProbe(io.BytesIO):
    def __init__(self, body, events=None):
        super().__init__(body)
        self.events = events if events is not None else []
        self.read_count = 0

    def read(self, *args, **kwargs):
        self.events.append("body_read")
        self.read_count += 1
        return super().read(*args, **kwargs)


def _logical_snapshot(path):
    connection = sqlite3.connect(path)
    try:
        tables = tuple(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        return tuple(
            (
                table,
                tuple(
                    tuple(row)
                    for row in connection.execute(
                        f'SELECT * FROM "{table}" ORDER BY rowid'
                    )
                ),
            )
            for table in tables
        )
    finally:
        connection.close()


class _MarkupParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.links = []
        self._form = None
        self._textarea_name = None
        self._textarea_parts = []
        self._select_name = None
        self._select_first = None
        self._select_selected = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "a" and attributes.get("href") is not None:
            self.links.append(attributes["href"])
        if tag == "form":
            self._form = {
                "id": attributes.get("id"),
                "action": attributes.get("action"),
                "fields": [],
            }
            return
        if self._form is None:
            return
        if tag == "input":
            name = attributes.get("name")
            input_type = attributes.get("type", "text").lower()
            if not name or input_type in {"button", "reset", "submit"}:
                return
            if input_type in {"checkbox", "radio"} and "checked" not in attributes:
                return
            self._form["fields"].append((name, attributes.get("value", "")))
        elif tag == "textarea":
            self._textarea_name = attributes.get("name")
            self._textarea_parts = []
        elif tag == "select":
            self._select_name = attributes.get("name")
            self._select_first = None
            self._select_selected = None
        elif tag == "option" and self._select_name is not None:
            value = attributes.get("value", "")
            if self._select_first is None:
                self._select_first = value
            if "selected" in attributes:
                self._select_selected = value

    def handle_data(self, data):
        if self._form is not None and self._textarea_name is not None:
            self._textarea_parts.append(data)

    def handle_endtag(self, tag):
        if self._form is None:
            return
        if tag == "textarea" and self._textarea_name is not None:
            self._form["fields"].append(
                (self._textarea_name, "".join(self._textarea_parts))
            )
            self._textarea_name = None
            self._textarea_parts = []
        elif tag == "select" and self._select_name is not None:
            self._form["fields"].append(
                (
                    self._select_name,
                    self._select_selected
                    if self._select_selected is not None
                    else (self._select_first or ""),
                )
            )
            self._select_name = None
            self._select_first = None
            self._select_selected = None
        elif tag == "form":
            self.forms.append(self._form)
            self._form = None



class PersistentProfileCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wahojobs-corrections-")
        self.path = Path(self.temp.name) / "profiles.sqlite"
        writer = install_browser_authentication_database(self.path)
        try:
            self.session = seed_browser_session(writer, suffix="84")
            self.created = create_persistent_profile(
                writer,
                create_command(
                    self.session["principal"],
                    idempotency_key="profile-correction-fixture-create-0001",
                ),
            )
        finally:
            writer.close()
        self.now = REQUEST_AT
        self.monotonic = 100.0
        self.registry_time = 200.0
        self.tokens = _TokenFactory()
        self.services = []
        self.browsers = []
        self.service = self._build_service()

    def tearDown(self):
        for browser in reversed(self.browsers):
            if not browser.closed:
                browser.close()
        for service in reversed(self.services):
            if not service.closed:
                service.close()
        self.temp.cleanup()

    def _build_service(self, *, vault=None, token_factory=None, append_revision=None):
        artifact_vault = vault or ConfirmedProfileCorrectionArtifactVault(
            monotonic=lambda: self.monotonic,
        )
        arguments = {
            "authentication_gateway": DurableBrowserSessionAuthenticationGateway(
                trusted_environment_namespace=self.session["environment"],
                clock=lambda: self.now,
            ),
            "authorization_gateway": DurablePersistentProfileReadAuthorizationGateway(),
            "read_connection_provider": _ReadProvider(self.path),
            "write_connection_provider": _WriteProvider(self.path),
            "vault": artifact_vault,
            "clock": lambda: self.now,
            "token_factory": token_factory or self.tokens,
            "binding_secret": b"profile-correction-test-binding-key" * 2,
        }
        if append_revision is not None:
            arguments["append_revision"] = append_revision
        service = PersistentProfileCorrectionService(**arguments)
        service.activate()
        self.services.append(service)
        return service

    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=3.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _headers(session):
        return (
            (
                "Cookie",
                f"wahojobs_session={session['session_token']}; "
                f"__Host-wahojobs_session_csrf={session['csrf_secret']}",
            ),
        )

    def _grant(self, *, service=None, session=None):
        service = service or self.service
        session = session or self.session
        result = service.authorize_request(
            method="GET",
            authentication_input=self._headers(session),
            session_token=session["session_token"],
            csrf_secret=session["csrf_secret"],
        )
        self.assertEqual(result.state, "authorized")
        grant = result.grant_for_service()
        self.assertIsNotNone(grant)
        return grant

    def _new_session(self, suffix):
        connection = self._connection()
        try:
            created = accounts.create_session(
                connection,
                user_id=self.session["account_id"],
                idle_ttl=timedelta(hours=2),
                absolute_ttl=timedelta(days=1),
                idempotency_key=f"profile-correction-session-{suffix}",
                now=self.now,
            )
            connection.commit()
            return {
                **self.session,
                "session_id": created.session.session_id,
                "session_token": created.session_token,
                "csrf_secret": created.csrf_secret,
            }
        finally:
            connection.close()

    def _review(self, grant, *, service=None, city="Recife"):
        service = service or self.service
        draft, raw_about_you = service.prepare_review_draft(grant)
        fields = profile_review_form_fields(draft, "correction", "R" * 43)
        form = {name: [value] for name, value in fields.items()}
        form["city"] = [city]
        updates = profile_review_updates_from_form(
            form,
            profile_review_language_slots(draft),
        )
        reviewed = apply_identity_free_profile_review(draft, updates)
        self.assertEqual(reviewed["location"]["city"], city)
        return draft, reviewed, raw_about_you, updates

    def _grant_for_v2(self, profile_v2, *, anchor=None):
        anchor = anchor or self._grant()
        return corrections_module.TrustedProfileCorrectionGrant._issue(
            corrections_module._CORRECTION_GRANT_ISSUANCE,
            account_id=self.session["account_id"],
            session_id=self.session["session_id"],
            environment_namespace=self.session["environment"],
            principal=anchor.principal_for_repository(),
            profile=anchor.profile_for_repository(),
            base_revision_id=anchor.base_revision_id,
            base_revision_number=anchor.base_revision_number,
            base_profile_v2=profile_v2,
        )

    @staticmethod
    def _default_review_submission(grant):
        draft, raw_about_you = corrections_module._server_review_material(grant)
        updates = corrections_module._server_default_review_updates(draft)
        reviewed = apply_identity_free_profile_review(draft, updates)
        return draft, raw_about_you, updates, reviewed

    def _issue_exact_review(self, grant, raw_about_you, updates, reviewed):
        return self.service.issue_confirmed_artifact(
            grant=grant,
            csrf_secret=self.session["csrf_secret"],
            reviewed_profile=reviewed,
            raw_about_you=raw_about_you,
            normalized_updates=updates,
            profile_confirmed=True,
            authentication_input=self._headers(self.session),
        )

    def _issue(self, grant, *, service=None, session=None, city="Recife"):
        service = service or self.service
        session = session or self.session
        draft, reviewed, raw_about_you, updates = self._review(
            grant,
            service=service,
            city=city,
        )
        offer = service.issue_confirmed_artifact(
            grant=grant,
            csrf_secret=session["csrf_secret"],
            reviewed_profile=reviewed,
            raw_about_you=raw_about_you,
            normalized_updates=updates,
            profile_confirmed=True,
            authentication_input=self._headers(session),
        )
        return offer, draft, reviewed, raw_about_you, updates

    def _consume(self, grant, offer, *, service=None, session=None):
        service = service or self.service
        session = session or self.session
        return service.consume(
            grant=grant,
            csrf_secret=session["csrf_secret"],
            artifact_reference=offer.artifact_reference,
            csrf_proof=offer.csrf_proof,
        )

    def _profile_counts(self):
        connection = self._connection()
        try:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "product_profiles",
                    "product_profile_revisions",
                    "product_profile_sources",
                    "current_product_profiles",
                )
            )
        finally:
            connection.close()

    def _current(self):
        connection = self._connection()
        try:
            return read_current_profile(
                connection,
                self.session["principal"],
                include_structured_profile=True,
            )
        finally:
            connection.close()

    def _install_current_v2(self, profile_v2, *, idempotency_key):
        connection = self._connection()
        try:
            result = append_profile_revision(
                connection,
                append_command(
                    self.session["principal"],
                    reference(self.created, self.session["principal"]),
                    profile_v2,
                    expected_revision=1,
                    revision_kind="edit",
                    idempotency_key=idempotency_key,
                ),
            )
        finally:
            connection.close()
        self.assertEqual(result.revision_number, 2)
        return result

    def _fixture_v2(
        self,
        *,
        languages=None,
        education_level=None,
        display_name=None,
    ):
        v1 = deepcopy(load_cases()[0]["expected_canonical_profile"])
        if languages is not None:
            v1["languages"] = [
                {
                    "language": language,
                    "proficiency": "professional",
                    "locale": "",
                    "evidence": [],
                    "confidence": "medium" if index % 2 else "low",
                }
                for index, language in enumerate(languages)
            ]
        if education_level is not None:
            v1["education"]["education_level"] = education_level
        if display_name is not None:
            v1["identity"]["display_name"] = display_name
        v1["provenance"]["field_sources"] = field_sources_for_profile(
            v1,
            PROFILE_SOURCE_EXTERNAL,
            explicit=True,
        )
        validate_canonical_profile(v1)
        return convert_v1_to_v2(
            v1,
            persistent_profile_id=self.created.profile_id,
            source_ordinal_resolver=ordinal_resolver,
        )

    @staticmethod
    def _run_concurrently(*operations):
        barrier = threading.Barrier(len(operations))
        outcomes = []
        failures = []
        lock = threading.Lock()

        def run(operation):
            try:
                barrier.wait(timeout=10)
                outcome = operation()
                with lock:
                    outcomes.append(outcome)
            except BaseException as exc:
                with lock:
                    failures.append(exc)

        threads = [threading.Thread(target=run, args=(operation,)) for operation in operations]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        if any(thread.is_alive() for thread in threads):
            failures.append(AssertionError("correction concurrency did not terminate"))
        return outcomes, failures

    def _build_browser(self, *, correction_registry=None, with_matches=False):
        authentication = DurableBrowserSessionAuthenticationGateway(
            trusted_environment_namespace=self.session["environment"],
            clock=lambda: self.now,
        )
        authorization = DurablePersistentProfileReadAuthorizationGateway()
        read_provider = _ReadProvider(self.path)
        application = PersistentProfileApplicationService(
            durable_authentication_gateway=authentication,
            durable_authorization_gateway=authorization,
            connection_provider=read_provider,
        )
        matches = None
        if with_matches:
            matches_service = AuthenticatedProfileMatchesService(
                authentication_gateway=authentication,
                authorization_gateway=authorization,
                connection_provider=read_provider,
                clock=lambda: self.now,
                binding_secret=b"profile-correction-matches-binding-key" * 2,
            )
            matches = AuthenticatedProfileMatchesBrowserIntegration(
                matches_service,
                connection_provider=read_provider,
                metadata_overlay=OpportunityMetadataOverlay(
                    path=self.path.with_suffix(".overlay.json"),
                    records_by_key={},
                ),
                confirmed_profile_artifact_sink=lambda **_kwargs: None,
                completed_profile_confirmation_authenticator=lambda **_kwargs: False,
                public_origin=PUBLIC_ORIGIN,
                now=lambda: self.now,
                ephemeral_identity_factory=lambda: "matcher-" + ("d" * 32),
            )
        browser = PersistentProfileBrowserIntegration(
            application,
            correction_service=self.service,
            correction_registry=correction_registry,
            matches_integration=matches,
            public_origin=PUBLIC_ORIGIN,
        )
        self.assertTrue(browser.activate())
        self.browsers.append(browser)
        return browser

    @staticmethod
    def _response_header(response, name):
        values = tuple(
            value
            for candidate, value in response.headers
            if candidate.lower() == name.lower()
        )
        if len(values) != 1:
            raise AssertionError(f"expected one {name} response header, got {values!r}")
        return values[0]

    @staticmethod
    def _browser_headers(
        session,
        *,
        body=None,
        host=PUBLIC_AUTHORITY,
        origin=PUBLIC_ORIGIN,
        extra=(),
        include_session=True,
        include_csrf=True,
        content_length=None,
    ):
        cookies = []
        if include_session:
            cookies.append(f"wahojobs_session={session['session_token']}")
        if include_csrf:
            cookies.append(
                f"__Host-wahojobs_session_csrf={session['csrf_secret']}"
            )
        headers = [("Host", host)]
        if body is not None:
            headers.extend(
                (
                    ("Origin", origin),
                    ("Sec-Fetch-Site", "same-origin"),
                    ("Content-Type", "application/x-www-form-urlencoded"),
                    (
                        "Content-Length",
                        str(len(body)) if content_length is None else content_length,
                    ),
                )
            )
        if cookies:
            headers.append(("Cookie", "; ".join(cookies)))
        headers.extend(extra)
        return tuple(headers)

    @staticmethod
    def _markup(response):
        parser = _MarkupParser()
        parser.feed(response.body.decode("utf-8"))
        parser.close()
        return parser

    def _form(self, response, *required_fields):
        required = set(required_fields)
        candidates = [
            form
            for form in self._markup(response).forms
            if required <= {name for name, _value in form["fields"]}
        ]
        self.assertEqual(len(candidates), 1)
        return candidates[0]

    def _assert_self_only_form_policy(self, response):
        policy = self._response_header(response, "Content-Security-Policy")
        self.assertEqual(policy, ORDINARY_FORM_CONTENT_SECURITY_POLICY)
        self.assertNotIn("accounts.google.com", policy)

    @staticmethod
    def _set_form_field(fields, name, value):
        return [item for item in fields if item[0] != name] + [(name, value)]

    def _post_form(self, browser, target, fields, *, session=None, events=None):
        session = session or self.session
        body = urlencode(fields).encode("ascii")
        probe = _ReadProbe(body, events)
        response = browser.handle(
            "POST",
            target,
            self._browser_headers(session, body=body),
            probe,
        )
        return response, probe

    def _start_browser_correction(self, browser, *, session=None):
        session = session or self.session
        start = browser.handle(
            "GET",
            "/account/profile?correction=start",
            self._browser_headers(session),
        )
        self.assertEqual(start.status, 200)
        self.assertEqual(
            self._response_header(start, "Referrer-Policy"),
            "same-origin",
        )
        self._assert_self_only_form_policy(start)
        form = self._form(start, "intent")
        response, probe = self._post_form(
            browser,
            form["action"],
            form["fields"],
            session=session,
        )
        self.assertEqual((response.status, probe.read_count), (303, 1))
        return self._response_header(response, "Location")

    def _browser_apply_offer(self, browser, *, changes=()):
        review_target = self._start_browser_correction(browser)
        review = browser.handle(
            "GET",
            review_target,
            self._browser_headers(self.session),
        )
        self.assertEqual(review.status, 200)
        self.assertEqual(
            self._response_header(review, "Referrer-Policy"),
            "same-origin",
        )
        self._assert_self_only_form_policy(review)
        edit_links = [
            href
            for href in self._markup(review).links
            if "correction=edit" in href
        ]
        self.assertEqual(len(edit_links), 1)
        edit = browser.handle(
            "GET",
            edit_links[0],
            self._browser_headers(self.session),
        )
        self.assertEqual(edit.status, 200)
        self.assertEqual(
            self._response_header(edit, "Referrer-Policy"),
            "same-origin",
        )
        self._assert_self_only_form_policy(edit)
        edit_form = self._form(edit, "edit_run_id", "review_token", "schema_version")
        fields = list(edit_form["fields"])
        fields = self._set_form_field(fields, "credentials_confirmed", "1")
        for name, value in changes:
            fields = self._set_form_field(fields, name, value)
        redrafted, probe = self._post_form(browser, edit_form["action"], fields)
        self.assertEqual((redrafted.status, probe.read_count), (303, 1))
        reviewed = browser.handle(
            "GET",
            self._response_header(redrafted, "Location"),
            self._browser_headers(self.session),
        )
        self.assertEqual(reviewed.status, 200)
        self.assertEqual(
            self._response_header(reviewed, "Referrer-Policy"),
            "same-origin",
        )
        self._assert_self_only_form_policy(reviewed)
        confirm_form = self._form(reviewed, "draft", "review_token")
        confirm_fields = self._set_form_field(
            list(confirm_form["fields"]),
            "confirmed",
            "1",
        )
        confirmed, probe = self._post_form(
            browser,
            confirm_form["action"],
            confirm_fields,
        )
        self.assertEqual((confirmed.status, probe.read_count), (200, 1))
        self.assertEqual(
            self._response_header(confirmed, "Referrer-Policy"),
            "same-origin",
        )
        self._assert_self_only_form_policy(confirmed)
        return self._form(confirmed, "artifact", "csrf")

    def _seed_inventory(self):
        connection = self._connection()
        try:
            observed = self.now.isoformat()
            connection.execute(
                "INSERT INTO companies "
                "(id, name, slug, careers_url, source_tier, inventory_model, "
                "market_count_policy) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    8101,
                    "Configured Correction Inventory",
                    "configured-correction",
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
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 1, 1)",
                (
                    8102,
                    8101,
                    "distinctive-portuguese-data-annotation-reviewer",
                    "Distinctive Portuguese Data Annotation Reviewer",
                    "distinctive portuguese data annotation reviewer",
                    "Generalist",
                    "Portuguese",
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
                    8103,
                    8101,
                    8102,
                    "configured-portuguese-8103",
                    "Distinctive Portuguese Data Annotation Reviewer",
                    "Remote",
                    "Generalist",
                    "Generalist",
                    "Freelance",
                    "https://jobs.example.test/portuguese-reviewer",
                    "configured-source-hash-8103",
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
                (8104, 8101, observed, observed),
            )
            connection.commit()
        finally:
            connection.close()

    def _inventory_snapshot(self):
        connection = self._connection()
        try:
            return tuple(
                (
                    table,
                    tuple(
                        tuple(row)
                        for row in connection.execute(
                            f'SELECT * FROM "{table}" ORDER BY id'
                        )
                    ),
                )
                for table in (
                    "companies",
                    "canonical_opportunities",
                    "jobs",
                    "crawl_runs",
                )
            )
        finally:
            connection.close()

    def test_browser_existing_entry_absent_creation_only_and_get_head_are_safe(self):
        grant = self._grant()
        offer, *_rest = self._issue(
            grant,
            city="<script>alert(1)</script>",
        )
        self.assertEqual(self._consume(grant, offer).state, "corrected")
        current = self._current()
        before_counts = self._profile_counts()
        before_bytes = hashlib.sha256(self.path.read_bytes()).hexdigest()
        browser = self._build_browser()

        get_response = browser.handle(
            "GET",
            "/account/profile",
            self._browser_headers(self.session),
        )
        head_response = browser.handle(
            "HEAD",
            "/account/profile",
            self._browser_headers(self.session),
        )
        after_bytes = hashlib.sha256(self.path.read_bytes()).hexdigest()
        body = get_response.body.decode("utf-8")

        self.assertEqual((get_response.status, head_response.status), (200, 200))
        self.assertEqual(
            self._response_header(get_response, "Referrer-Policy"),
            "no-referrer",
        )
        self.assertEqual(head_response.body, b"")
        self.assertEqual(
            self._response_header(head_response, "Content-Length"),
            self._response_header(get_response, "Content-Length"),
        )
        self.assertEqual((before_bytes, before_counts), (after_bytes, self._profile_counts()))
        self.assertIn("Find matches", body)
        self.assertIn("Update profile", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertEqual(
            set(self._markup(get_response).links),
            {
                "/account/profile",
                "/logout",
                "/find-matches",
                "/account/profile?correction=start",
            },
        )
        for durable_value in (
            self.session["account_id"],
            self.session["principal_id"],
            self.session["session_id"],
            self.session["session_token"],
            current.profile_id,
            current.revision_id,
        ):
            self.assertNotIn(durable_value, body)
        for forbidden in (
            "Revision history",
            "Current revision",
            "/preview",
            "/tracker",
            "/action",
            "My Jobs",
            "demo persona",
            "rollback",
            "reactivate",
            "archive profile",
            "delete profile",
        ):
            self.assertNotIn(forbidden.lower(), body.lower())

        writer = self._connection()
        try:
            absent_session = seed_browser_session(writer, suffix="90")
        finally:
            writer.close()
        absent_counts = self._profile_counts()
        absent = browser.handle(
            "GET",
            "/account/profile",
            self._browser_headers(absent_session),
        )
        unavailable_correction = browser.handle(
            "GET",
            "/account/profile?correction=start",
            self._browser_headers(absent_session),
        )
        absent_body = absent.body.decode("utf-8")
        self.assertEqual((absent.status, unavailable_correction.status), (200, 409))
        self.assertIn("No persistent profile yet", absent_body)
        self.assertIn("Create profile", absent_body)
        self.assertNotIn("Update profile", absent_body)
        self.assertNotIn("correction=", absent_body)
        self.assertEqual(self._profile_counts(), absent_counts)

    def test_form_bearing_correction_stages_use_same_origin_policy(self):
        browser = self._build_browser()
        self._browser_apply_offer(browser, changes=(("city", "Recife"),))

    def test_browser_authority_order_zero_early_reads_and_one_valid_read(self):
        browser = self._build_browser()
        body = urlencode((("intent", "update_profile"),)).encode("ascii")
        valid_target = (
            "/account/profile?action=start&proof="
            + profile_correction_action_csrf_proof(
                self.session["csrf_secret"],
                "start",
            )
        )
        real_authorize = PersistentProfileCorrectionService.authorize_request

        def request(target, headers):
            events = []
            probe = _ReadProbe(body, events)

            def traced_authorize(instance, *args, **kwargs):
                events.append("authority")
                return real_authorize(instance, *args, **kwargs)

            with mock.patch.object(
                PersistentProfileCorrectionService,
                "authorize_request",
                new=traced_authorize,
            ):
                response = browser.handle("POST", target, headers, probe)
            return response, probe, events

        pre_authority_cases = (
            (
                "malformed target",
                "/account/profile?action=start&proof=short",
                self._browser_headers(self.session, body=body),
                400,
            ),
            (
                "host",
                valid_target,
                self._browser_headers(self.session, body=body, host="evil.test"),
                400,
            ),
            (
                "proxy",
                valid_target,
                self._browser_headers(
                    self.session,
                    body=body,
                    extra=(("X-Forwarded-Host", PUBLIC_AUTHORITY),),
                ),
                400,
            ),
            (
                "origin",
                valid_target,
                self._browser_headers(
                    self.session,
                    body=body,
                    origin="https://evil.test",
                ),
                403,
            ),
            (
                "session cookie",
                valid_target,
                self._browser_headers(
                    self.session,
                    body=body,
                    include_session=False,
                ),
                401,
            ),
        )
        for label, target, headers, expected_status in pre_authority_cases:
            with self.subTest(label=label):
                response, probe, events = request(target, headers)
                self.assertEqual(response.status, expected_status)
                self.assertEqual((probe.read_count, events), (0, []))

        invalid_session = {**self.session, "session_token": "x" * 43}
        invalid_session_target = (
            "/account/profile?action=start&proof="
            + profile_correction_action_csrf_proof(
                invalid_session["csrf_secret"],
                "start",
            )
        )
        authority_cases = (
            (
                "invalid durable session",
                invalid_session_target,
                self._browser_headers(invalid_session, body=body),
                403,
            ),
            (
                "action csrf",
                "/account/profile?action=start&proof=" + ("q" * 43),
                self._browser_headers(self.session, body=body),
                403,
            ),
            (
                "form envelope after authority",
                valid_target,
                self._browser_headers(
                    self.session,
                    body=body,
                    content_length="01",
                ),
                400,
            ),
        )
        for label, target, headers, expected_status in authority_cases:
            with self.subTest(label=label):
                response, probe, events = request(target, headers)
                self.assertEqual(response.status, expected_status)
                self.assertEqual((probe.read_count, events), (0, ["authority"]))

        accepted, probe, events = request(
            valid_target,
            self._browser_headers(self.session, body=body),
        )
        self.assertEqual(accepted.status, 303)
        self.assertEqual((probe.read_count, events), (1, ["authority", "body_read"]))

    def test_browser_rejects_identity_base_and_bound_reference_abuse(self):
        registry = review_support.MatchRunRegistry(
            max_size=PROFILE_CORRECTION_ARTIFACT_CAPACITY,
            absolute_ttl_seconds=PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS,
            _retention_clock=lambda: self.registry_time,
        )
        browser = self._build_browser(correction_registry=registry)
        review_target = self._start_browser_correction(browser)
        review = browser.handle(
            "GET",
            review_target,
            self._browser_headers(self.session),
        )
        edit_target = next(
            href for href in self._markup(review).links if "correction=edit" in href
        )
        edit = browser.handle(
            "GET",
            edit_target,
            self._browser_headers(self.session),
        )
        edit_form = self._form(edit, "edit_run_id", "review_token", "schema_version")
        baseline = self._profile_counts()
        for injected_name, injected_value in (
            ("profile_id", "prf_browser_selected_identity"),
            ("base_revision_id", "pvr_browser_selected_base"),
            ("profile_draft_fingerprint", "f" * 64),
        ):
            with self.subTest(injected=injected_name):
                injected = list(edit_form["fields"]) + [
                    (injected_name, injected_value)
                ]
                rejected, probe = self._post_form(
                    browser,
                    edit_form["action"],
                    injected,
                )
                self.assertEqual((rejected.status, probe.read_count), (400, 1))
                self.assertEqual(self._profile_counts(), baseline)

        tampered_review = review_target.rsplit("token=", 1)[0] + "token=" + ("z" * 43)
        self.assertEqual(
            browser.handle(
                "GET",
                tampered_review,
                self._browser_headers(self.session),
            ).status,
            410,
        )
        self.registry_time += PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS + 1
        self.assertEqual(
            browser.handle(
                "GET",
                review_target,
                self._browser_headers(self.session),
            ).status,
            410,
        )

        session_two = self._new_session("91")
        account_two_writer = self._connection()
        try:
            account_two = seed_browser_session(account_two_writer, suffix="92")
            create_persistent_profile(
                account_two_writer,
                create_command(
                    account_two["principal"],
                    idempotency_key="profile-correction-cross-account-create-0001",
                ),
            )
        finally:
            account_two_writer.close()
        bound_draft = self._start_browser_correction(browser)
        for label, outsider in (
            ("cross session", session_two),
            ("cross account", account_two),
        ):
            with self.subTest(bound_draft=label):
                self.assertEqual(
                    browser.handle(
                        "GET",
                        bound_draft,
                        self._browser_headers(outsider),
                    ).status,
                    410,
                )

        apply_form = self._browser_apply_offer(
            browser,
            changes=(("city", "Recife"),),
        )
        artifact_fields = list(apply_form["fields"])
        artifact_reference = dict(artifact_fields)["artifact"]
        artifact_baseline = self._profile_counts()

        malformed = self._set_form_field(artifact_fields, "artifact", "short")
        response, probe = self._post_form(browser, apply_form["action"], malformed)
        self.assertEqual((response.status, probe.read_count), (400, 1))

        tampered_reference = (
            ("A" if artifact_reference[0] != "A" else "B")
            + artifact_reference[1:]
        )
        tampered = self._set_form_field(
            artifact_fields,
            "artifact",
            tampered_reference,
        )
        response, probe = self._post_form(browser, apply_form["action"], tampered)
        self.assertEqual((response.status, probe.read_count), (410, 1))

        wrong_purpose = self._set_form_field(
            artifact_fields,
            "csrf",
            profile_create_csrf_proof(
                self.session["csrf_secret"],
                artifact_reference,
            ),
        )
        response, probe = self._post_form(
            browser,
            apply_form["action"],
            wrong_purpose,
        )
        self.assertEqual((response.status, probe.read_count), (410, 1))

        for label, outsider in (
            ("cross session", session_two),
            ("cross account", account_two),
        ):
            with self.subTest(bound_artifact=label):
                target = (
                    "/account/profile?action=apply&proof="
                    + profile_correction_action_csrf_proof(
                        outsider["csrf_secret"],
                        "apply",
                    )
                )
                response, probe = self._post_form(
                    browser,
                    target,
                    artifact_fields,
                    session=outsider,
                )
                self.assertEqual((response.status, probe.read_count), (410, 1))

        self.monotonic += PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS + 1
        expired, probe = self._post_form(
            browser,
            apply_form["action"],
            artifact_fields,
        )
        self.assertEqual((expired.status, probe.read_count), (410, 1))
        self.assertEqual(self._profile_counts(), artifact_baseline)

    def test_browser_correction_changes_configured_matches_without_inventory_mutation(self):
        self._seed_inventory()
        browser = self._build_browser(with_matches=True)
        inventory_before = self._inventory_snapshot()
        profile_before = self._profile_counts()
        connection = self._connection()
        try:
            tables_before = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            )
            non_profile_counts_before = {
                table: connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
                for table in tables_before
                if "match" in table.lower() or "pipeline" in table.lower()
            }
        finally:
            connection.close()

        before_matches = browser.handle(
            "GET",
            "/find-matches",
            self._browser_headers(self.session),
        )
        before_body = before_matches.body.decode("utf-8")
        self.assertEqual(before_matches.status, 200)
        self.assertNotIn("Distinctive Portuguese Data Annotation Reviewer", before_body)

        apply_form = self._browser_apply_offer(
            browser,
            changes=(
                ("language_2", "Portuguese"),
                ("language_proficiency_2", "fluent"),
                ("language_locale_2", ""),
            ),
        )
        applied, probe = self._post_form(
            browser,
            apply_form["action"],
            apply_form["fields"],
        )
        self.assertEqual((applied.status, probe.read_count), (303, 1))
        self.assertEqual(self._response_header(applied, "Location"), "/find-matches")

        current = self._current()
        trusted = current.trusted_dict(include_structured_profile=True)
        corrected_v2 = trusted["structured_profile"]
        self.assertEqual(validate_canonical_profile_v2(corrected_v2), corrected_v2)
        self.assertEqual(corrected_v2["identity"]["profile_id"], self.created.profile_id)
        self.assertIn(
            "Portuguese",
            [language["language"] for language in corrected_v2["languages"]],
        )
        self.assertEqual(self._profile_counts(), (1, 2, 3, 1))

        after_matches = browser.handle(
            "GET",
            "/find-matches",
            self._browser_headers(self.session),
        )
        after_body = after_matches.body.decode("utf-8")
        self.assertEqual(after_matches.status, 200)
        self.assertIn("Distinctive Portuguese Data Annotation Reviewer", after_body)
        self.assertIn(
            "href='/job/opportunity-8102'",
            after_body,
        )
        self.assertNotIn(
            "href='https://jobs.example.test/portuguese-reviewer'",
            after_body,
        )
        self.assertNotIn(self.created.profile_id, after_body)
        self.assertNotIn("matcher-" + ("d" * 32), after_body)
        self.assertEqual(self._inventory_snapshot(), inventory_before)

        connection = self._connection()
        try:
            tables_after = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            )
            non_profile_counts_after = {
                table: connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
                for table in tables_after
                if "match" in table.lower() or "pipeline" in table.lower()
            }
        finally:
            connection.close()
        self.assertEqual(tables_after, tables_before)
        self.assertEqual(non_profile_counts_after, non_profile_counts_before)
        self.assertFalse(any("match_run" in table.lower() for table in tables_after))
        self.assertEqual(
            tuple(after - before for before, after in zip(profile_before, self._profile_counts())),
            (0, 1, 2, 0),
        )

    def test_authority_and_review_draft_bind_every_server_selected_dimension(self):
        grant = self._grant()
        base = grant.trusted_base_profile_v2()
        binding = grant.confirmation_binding()

        self.assertEqual(validate_canonical_profile_v2(base), base)
        self.assertEqual(base["identity"]["profile_id"], self.created.profile_id)
        self.assertEqual(
            binding,
            (
                self.session["account_id"],
                self.session["session_id"],
                self.session["environment"],
                self.session["principal_id"],
                self.created.profile_id,
                self.created.revision_id,
                1,
                PROFILE_CORRECTION_PURPOSE,
                hashlib.sha256(canonical_profile_v2_json_bytes(base)).hexdigest(),
                "active",
            ),
        )
        draft_binding = self.service.draft_binding(grant)
        self.assertRegex(draft_binding, re.compile(r"^[0-9a-f]{64}$"))
        self.assertEqual(self.service.draft_binding(grant), draft_binding)

        draft, raw_about_you = self.service.prepare_review_draft(grant)
        IdentityFreeCanonicalProfileV1.from_json_bytes(draft.canonical_bytes)
        rendered = draft.canonical_bytes.decode("utf-8") + raw_about_you + repr(draft)
        self.assertNotIn("profile_id", rendered)
        for binding_index in (0, 1, 2, 3, 4, 5):
            self.assertNotIn(str(binding[binding_index]), rendered)

        second_session = self._new_session("85")
        second_grant = self._grant(session=second_session)
        self.assertNotEqual(second_grant.confirmation_binding(), binding)
        self.assertNotEqual(self.service.draft_binding(second_grant), draft_binding)
        self.assertEqual(second_grant.confirmation_binding()[0], binding[0])
        self.assertEqual(second_grant.confirmation_binding()[3:7], binding[3:7])

    def test_get_and_head_authority_require_the_durable_session_csrf_pair(self):
        for method in ("GET", "HEAD"):
            with self.subTest(method=method, csrf="missing"):
                missing = self.service.authorize_request(
                    method=method,
                    authentication_input=self._headers(self.session),
                    session_token=self.session["session_token"],
                )
                self.assertEqual(missing.state, "csrf_denied")
                self.assertIsNone(missing.grant_for_service())
            with self.subTest(method=method, csrf="wrong"):
                wrong = self.service.authorize_request(
                    method=method,
                    authentication_input=self._headers(self.session),
                    session_token=self.session["session_token"],
                    csrf_secret="w" * 43,
                )
                self.assertEqual(wrong.state, "csrf_denied")
                self.assertIsNone(wrong.grant_for_service())
            with self.subTest(method=method, csrf="valid"):
                valid = self.service.authorize_request(
                    method=method,
                    authentication_input=self._headers(self.session),
                    session_token=self.session["session_token"],
                    csrf_secret=self.session["csrf_secret"],
                )
                self.assertEqual(valid.state, "authorized")
                self.assertIsNotNone(valid.grant_for_service())

    def test_actual_artifact_issue_noop_preserves_all_canonical_fixture_semantics(self):
        anchor = self._grant()
        for index, case in enumerate(load_cases(), start=1):
            with self.subTest(case_id=case["case_id"]):
                base_v2 = convert_v1_to_v2(
                    case["expected_canonical_profile"],
                    persistent_profile_id=self.created.profile_id,
                    source_ordinal_resolver=ordinal_resolver,
                )
                grant = self._grant_for_v2(base_v2, anchor=anchor)
                _draft, raw_about_you, updates, reviewed = (
                    self._default_review_submission(grant)
                )
                offer = self._issue_exact_review(
                    grant,
                    raw_about_you,
                    updates,
                    reviewed,
                )
                command = self.service._vault._records[
                    offer.artifact_reference
                ].snapshot.command
                corrected_v2 = command.trusted_structured_profile()
                original_semantics = deepcopy(base_v2)
                corrected_semantics = deepcopy(corrected_v2)
                original_semantics.pop("provenance")
                corrected_semantics.pop("provenance")
                self.assertEqual(corrected_semantics, original_semantics)
                self.assertEqual(
                    corrected_v2["identity"]["profile_id"],
                    self.created.profile_id,
                )
                self.assertTrue(
                    all(
                        source["source_ordinals"] == [1]
                        for source in corrected_v2["provenance"]["field_sources"]
                    )
                )

    def test_actual_issue_preserves_hidden_language_tail_and_relationship_aliases(self):
        v1 = deepcopy(load_cases()[0]["expected_canonical_profile"])
        language_names = (
            "Dutch",
            "English",
            "French",
            "German",
            "Italian",
            "Norwegian",
            "Polish",
            "Portuguese",
            "Spanish",
            "Swedish",
        )
        v1["languages"] = [
            {
                "language": language,
                "proficiency": "professional",
                "locale": "",
                "evidence": [],
                "confidence": "medium" if index % 2 else "low",
            }
            for index, language in enumerate(language_names)
        ]
        v1["education"]["education_level"] = "no_degree"
        v1["constraints"]["hard_constraints"] = ["weekends unavailable"]
        v1["constraints"]["excluded_domains"] = ["gambling"]
        v1["constraints"]["avoid_keywords"] = ["night shift"]
        v1["location"]["restrictions"] = ["Brazil only"]
        v1["location"]["geographic_work_restrictions"] = []
        v1["experience"]["recent_roles"] = ["Quality reviewer"]
        v1["experience"]["job_titles"] = []
        v1["provenance"]["field_sources"] = field_sources_for_profile(
            v1,
            PROFILE_SOURCE_EXTERNAL,
            explicit=True,
        )
        validate_canonical_profile(v1)
        base_v2 = convert_v1_to_v2(
            v1,
            persistent_profile_id=self.created.profile_id,
            source_ordinal_resolver=ordinal_resolver,
        )
        self.assertEqual(len(base_v2["languages"]), 10)
        anchor = self._grant()
        grant = self._grant_for_v2(base_v2, anchor=anchor)
        draft, raw_about_you, updates, reviewed = self._default_review_submission(grant)

        no_op = self._issue_exact_review(
            grant,
            raw_about_you,
            updates,
            reviewed,
        )
        no_op_v2 = self.service._vault._records[
            no_op.artifact_reference
        ].snapshot.command.trusted_structured_profile()
        base_semantics = deepcopy(base_v2)
        no_op_semantics = deepcopy(no_op_v2)
        base_semantics.pop("provenance")
        no_op_semantics.pop("provenance")
        self.assertEqual(no_op_semantics, base_semantics)
        self.assertEqual(len(no_op_v2["languages"]), 10)

        city_updates = deepcopy(updates)
        city_updates["city"] = "Recife"
        city_reviewed = apply_identity_free_profile_review(draft, city_updates)
        city_offer = self._issue_exact_review(
            grant,
            raw_about_you,
            city_updates,
            city_reviewed,
        )
        city_v2 = self.service._vault._records[
            city_offer.artifact_reference
        ].snapshot.command.trusted_structured_profile()
        self.assertEqual(city_v2["location"]["city"], "Recife")
        for section in (
            "identity",
            "languages",
            "education",
            "credentials",
            "experience",
            "skills",
            "preferences",
            "constraints",
            "derived_matcher_signals",
        ):
            self.assertEqual(city_v2[section], base_v2[section])
        expected_location = deepcopy(base_v2["location"])
        expected_location["city"] = "Recife"
        self.assertEqual(city_v2["location"], expected_location)

        language_updates = deepcopy(updates)
        original_first = language_updates["languages"][0]["proficiency"]
        language_updates["languages"][0]["proficiency"] = (
            "fluent" if original_first != "fluent" else "intermediate"
        )
        language_reviewed = apply_identity_free_profile_review(
            draft,
            language_updates,
        )
        language_offer = self._issue_exact_review(
            grant,
            raw_about_you,
            language_updates,
            language_reviewed,
        )
        language_command = self.service._vault._records[
            language_offer.artifact_reference
        ].snapshot.command
        language_v2 = language_command.trusted_structured_profile()
        self.assertEqual(len(language_v2["languages"]), 10)
        self.assertEqual(language_v2["languages"][8:], base_v2["languages"][8:])
        correction_source = json.loads(language_command.sources[1].content)
        self.assertEqual(len(correction_source["updates"]["languages"]), 10)
        self.assertEqual(
            correction_source["updates"]["languages"][8:],
            [
                {
                    "language": item["language"],
                    "locale": item.get("locale") or "",
                    "proficiency": item.get("proficiency") or "unspecified",
                }
                for item in base_v2["languages"][8:]
            ],
        )
        encoded_correction_source = json.dumps(
            correction_source,
            ensure_ascii=False,
            sort_keys=True,
        )
        for durable_value in (
            self.session["account_id"],
            self.session["principal_id"],
            self.session["session_id"],
            self.created.profile_id,
            self.created.revision_id,
        ):
            self.assertNotIn(durable_value, encoded_correction_source)
        for section in (
            "identity",
            "location",
            "education",
            "credentials",
            "experience",
            "skills",
            "preferences",
            "constraints",
        ):
            self.assertEqual(language_v2[section], base_v2[section])
        self.assertEqual(
            {
                record["field_path"].split(".", 1)[0].split("[", 1)[0]: tuple(
                    record["source_ordinals"]
                )
                for record in language_v2["provenance"]["field_sources"]
            }["languages"],
            (2,),
        )

    def test_browser_language_rename_reorder_reviews_and_applies_all_ten(self):
        language_names = (
            "Dutch",
            "English",
            "French",
            "German",
            "Italian",
            "Norwegian",
            "Polish",
            "Portuguese",
            "Spanish",
            "Swedish",
        )
        display_name = "Durable multilingual reviewer"
        base_v2 = self._fixture_v2(
            languages=language_names,
            display_name=display_name,
        )
        self._install_current_v2(
            base_v2,
            idempotency_key="profile-correction-browser-language-base-0001",
        )
        browser = self._build_browser()

        review_target = self._start_browser_correction(browser)
        review = browser.handle(
            "GET",
            review_target,
            self._browser_headers(self.session),
        )
        edit_target = next(
            href for href in self._markup(review).links if "correction=edit" in href
        )
        edit = browser.handle(
            "GET",
            edit_target,
            self._browser_headers(self.session),
        )
        edit_form = self._form(
            edit,
            "edit_run_id",
            "review_token",
            "schema_version",
        )
        fields = self._set_form_field(
            list(edit_form["fields"]),
            "credentials_confirmed",
            "1",
        )
        self.assertEqual(dict(fields)["language_0"], "Dutch")
        fields = self._set_form_field(fields, "language_0", "Zulu")
        redrafted, probe = self._post_form(
            browser,
            edit_form["action"],
            fields,
        )
        self.assertEqual((redrafted.status, probe.read_count), (303, 1))

        reviewed = browser.handle(
            "GET",
            self._response_header(redrafted, "Location"),
            self._browser_headers(self.session),
        )
        self.assertEqual(reviewed.status, 200)
        reviewed_body = reviewed.body.decode("utf-8")
        self.assertIn(f"<h1>{display_name}</h1>", reviewed_body)
        self.assertNotIn("Dutch", reviewed_body)
        for language in (*language_names[1:], "Zulu"):
            self.assertIn(language, reviewed_body)
        confirm_form = self._form(reviewed, "draft", "review_token")
        reviewed_run = browser._correction_registry.peek(
            dict(confirm_form["fields"])["draft"]
        )
        self.assertEqual(
            [item["language"] for item in reviewed_run.canonical_profile["languages"]],
            [*language_names[1:], "Zulu"],
        )
        confirm_fields = self._set_form_field(
            list(confirm_form["fields"]),
            "confirmed",
            "1",
        )
        confirmed, probe = self._post_form(
            browser,
            confirm_form["action"],
            confirm_fields,
        )
        self.assertEqual((confirmed.status, probe.read_count), (200, 1))
        apply_form = self._form(confirmed, "artifact", "csrf")
        applied, probe = self._post_form(
            browser,
            apply_form["action"],
            apply_form["fields"],
        )
        self.assertEqual((applied.status, probe.read_count), (303, 1))

        corrected_v2 = self._current().trusted_dict(
            include_structured_profile=True
        )["structured_profile"]
        self.assertEqual(
            [item["language"] for item in corrected_v2["languages"]],
            [*language_names[1:], "Zulu"],
        )
        base_by_language = {
            item["language"]: item for item in base_v2["languages"]
        }
        for item in corrected_v2["languages"]:
            if item["language"] != "Zulu":
                self.assertEqual(item, base_by_language[item["language"]])
        for section in (
            "identity",
            "location",
            "education",
            "credentials",
            "experience",
            "skills",
            "preferences",
            "constraints",
        ):
            self.assertEqual(corrected_v2[section], base_v2[section])

    def test_browser_legacy_education_city_edit_preserves_full_language_root(self):
        language_names = (
            "Dutch",
            "English",
            "French",
            "German",
            "Italian",
            "Norwegian",
            "Polish",
            "Portuguese",
            "Spanish",
            "Swedish",
        )
        base_v2 = self._fixture_v2(
            languages=language_names,
            education_level="technical",
            display_name="Legacy technical reviewer",
        )
        base_v2["languages"][0]["proficiency"] = "advanced"
        validate_canonical_profile_v2(base_v2)
        self._install_current_v2(
            base_v2,
            idempotency_key="profile-correction-browser-legacy-base-0001",
        )
        browser = self._build_browser()
        apply_form = self._browser_apply_offer(
            browser,
            changes=(("city", "Recife"),),
        )
        applied, probe = self._post_form(
            browser,
            apply_form["action"],
            apply_form["fields"],
        )
        self.assertEqual((applied.status, probe.read_count), (303, 1))

        current = self._current()
        corrected_v2 = current.trusted_dict(include_structured_profile=True)[
            "structured_profile"
        ]
        expected_location = deepcopy(base_v2["location"])
        expected_location["city"] = "Recife"
        self.assertEqual(corrected_v2["location"], expected_location)
        self.assertEqual(corrected_v2["education"], base_v2["education"])
        self.assertEqual(corrected_v2["education"]["education_level"], "technical")
        self.assertEqual(corrected_v2["languages"], base_v2["languages"])
        language_source = next(
            source
            for source in corrected_v2["provenance"]["field_sources"]
            if source["field_path"].startswith("languages[")
        )
        self.assertEqual(language_source["source_ordinals"], [1])
        connection = self._connection()
        try:
            source_json = connection.execute(
                "SELECT source_content FROM product_profile_sources "
                "WHERE revision_id=? AND source_type='user_confirmed_correction'",
                (current.revision_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        source_updates = json.loads(source_json)["updates"]
        self.assertEqual(source_updates["education_level"], "technical")
        self.assertEqual(
            source_updates["languages"],
            [
                {
                    "language": item["language"],
                    "locale": item.get("locale") or "",
                    "proficiency": item.get("proficiency") or "unspecified",
                }
                for item in corrected_v2["languages"]
            ],
        )
        self.assertEqual(source_updates["languages"][0]["proficiency"], "advanced")

    def test_browser_city_edit_preserves_hidden_legacy_language_records_and_source(self):
        language_names = (
            "Dutch",
            "English",
            "French",
            "German",
            "Italian",
            "Norwegian",
            "Polish",
            "Portuguese",
            "Spanish",
            "Swedish",
        )
        base_v2 = self._fixture_v2(
            languages=language_names,
            display_name="Hidden legacy language reviewer",
        )
        base_v2["languages"][8]["proficiency"] = "advanced"
        base_v2["languages"][9]["proficiency"] = "conversational"
        validate_canonical_profile_v2(base_v2)
        self._install_current_v2(
            base_v2,
            idempotency_key=(
                "profile-correction-browser-hidden-language-base-0001"
            ),
        )
        browser = self._build_browser()
        apply_form = self._browser_apply_offer(
            browser,
            changes=(("city", "Recife"),),
        )
        applied, probe = self._post_form(
            browser,
            apply_form["action"],
            apply_form["fields"],
        )
        self.assertEqual((applied.status, probe.read_count), (303, 1))

        current = self._current()
        corrected_v2 = current.trusted_dict(include_structured_profile=True)[
            "structured_profile"
        ]
        self.assertEqual(corrected_v2["languages"], base_v2["languages"])
        self.assertEqual(
            [
                (item["language"], item["proficiency"], item["confidence"])
                for item in corrected_v2["languages"]
            ],
            [
                (item["language"], item["proficiency"], item["confidence"])
                for item in base_v2["languages"]
            ],
        )
        self.assertEqual(
            corrected_v2["location"]["city"],
            "Recife",
        )
        self.assertTrue(
            all(
                source["source_ordinals"] == [1]
                for source in corrected_v2["provenance"]["field_sources"]
                if source["field_path"].startswith("languages[")
            )
        )

        connection = self._connection()
        try:
            source_json = connection.execute(
                "SELECT source_content FROM product_profile_sources "
                "WHERE revision_id=? AND source_type='user_confirmed_correction'",
                (current.revision_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        source_updates = json.loads(source_json)["updates"]
        self.assertEqual(
            source_updates["languages"],
            [
                {
                    "language": item["language"],
                    "locale": item.get("locale") or "",
                    "proficiency": item.get("proficiency") or "unspecified",
                }
                for item in corrected_v2["languages"]
            ],
        )
        self.assertEqual(
            [item["proficiency"] for item in source_updates["languages"][8:]],
            ["advanced", "conversational"],
        )

    def test_browser_accepts_large_valid_server_rendered_correction_form(self):
        def values(prefix, count):
            return [
                f"{prefix} {index:02d} " + ("?" * 112)
                for index in range(count)
            ]

        v1 = deepcopy(load_cases()[0]["expected_canonical_profile"])
        for field, prefix, count in (
            ("hard_constraints", "Hard", 18),
            ("soft_preferences", "Soft", 17),
            ("avoid_keywords", "Avoid", 17),
            ("excluded_domains", "Excluded", 17),
            ("accessibility_constraints", "Access", 17),
        ):
            v1["constraints"][field] = values(prefix, count)
        for field in ("region", "city", "work_authorization"):
            v1["location"][field] = "?" * 3796
        v1["provenance"]["field_sources"] = field_sources_for_profile(
            v1,
            PROFILE_SOURCE_EXTERNAL,
            explicit=True,
        )
        validate_canonical_profile(v1)
        large_v2 = convert_v1_to_v2(
            v1,
            persistent_profile_id=self.created.profile_id,
            source_ordinal_resolver=ordinal_resolver,
        )
        self._install_current_v2(
            large_v2,
            idempotency_key="large-valid-current-profile-0000000001",
        )

        browser = self._build_browser()
        review_target = self._start_browser_correction(browser)
        review = browser.handle(
            "GET",
            review_target,
            self._browser_headers(self.session),
        )
        self.assertEqual(review.status, 200)
        edit_links = [
            href
            for href in self._markup(review).links
            if "correction=edit" in href
        ]
        self.assertEqual(len(edit_links), 1)
        edit = browser.handle(
            "GET",
            edit_links[0],
            self._browser_headers(self.session),
        )
        self.assertEqual(edit.status, 200)
        edit_form = self._form(
            edit,
            "edit_run_id",
            "review_token",
            "schema_version",
        )
        fields = self._set_form_field(
            list(edit_form["fields"]),
            "credentials_confirmed",
            "1",
        )
        fields = self._set_form_field(fields, "total_years", "7")
        body = urlencode(fields).encode("ascii")
        self.assertGreater(len(body), 65_536)
        self.assertLessEqual(len(body), MAX_PROFILE_CORRECTION_BODY_BYTES)

        probe = _ReadProbe(body)
        redrafted = browser.handle(
            "POST",
            edit_form["action"],
            self._browser_headers(self.session, body=body),
            probe,
        )
        self.assertEqual((redrafted.status, probe.read_count), (303, 1))
        reviewed = browser.handle(
            "GET",
            self._response_header(redrafted, "Location"),
            self._browser_headers(self.session),
        )
        self.assertEqual(reviewed.status, 200)
        confirm_form = self._form(reviewed, "draft", "review_token")
        confirm_fields = self._set_form_field(
            list(confirm_form["fields"]),
            "confirmed",
            "1",
        )
        confirmed, probe = self._post_form(
            browser,
            confirm_form["action"],
            confirm_fields,
        )
        self.assertEqual((confirmed.status, probe.read_count), (200, 1))
        apply_form = self._form(confirmed, "artifact", "csrf")
        applied, probe = self._post_form(
            browser,
            apply_form["action"],
            apply_form["fields"],
        )
        self.assertEqual((applied.status, probe.read_count), (303, 1))
        self.assertEqual(self._response_header(applied, "Location"), "/find-matches")
        corrected_v2 = self._current().trusted_dict(
            include_structured_profile=True
        )["structured_profile"]
        self.assertEqual(corrected_v2["experience"]["total_years"], 7)
        self.assertEqual(corrected_v2["location"], large_v2["location"])
        self.assertEqual(corrected_v2["constraints"], large_v2["constraints"])

    def test_browser_city_edit_preserves_large_alias_expanded_profile(self):
        def values(prefix):
            return [
                f"{prefix} {index:02d} " + ("?" * 112)
                for index in range(40)
            ]

        v1 = deepcopy(load_cases()[0]["expected_canonical_profile"])
        for field, prefix in (
            ("hard_constraints", "Hard"),
            ("soft_preferences", "Soft"),
            ("avoid_keywords", "Avoid"),
            ("excluded_domains", "Excluded"),
            ("accessibility_constraints", "Access"),
        ):
            v1["constraints"][field] = values(prefix)
        v1["provenance"]["field_sources"] = field_sources_for_profile(
            v1,
            PROFILE_SOURCE_EXTERNAL,
            explicit=True,
        )
        validate_canonical_profile(v1)
        base_v2 = convert_v1_to_v2(
            v1,
            persistent_profile_id=self.created.profile_id,
            source_ordinal_resolver=ordinal_resolver,
        )
        self.assertGreater(len(canonical_profile_v2_json_bytes(base_v2)), 65_536)
        self._install_current_v2(
            base_v2,
            idempotency_key="large-alias-expanded-current-profile-0001",
        )

        browser = self._build_browser()
        apply_form = self._browser_apply_offer(
            browser,
            changes=(("city", "Recife"),),
        )
        applied, probe = self._post_form(
            browser,
            apply_form["action"],
            apply_form["fields"],
        )
        self.assertEqual((applied.status, probe.read_count), (303, 1))
        self.assertEqual(self._response_header(applied, "Location"), "/find-matches")

        corrected_v2 = self._current().trusted_dict(
            include_structured_profile=True
        )["structured_profile"]
        self.assertEqual(validate_canonical_profile_v2(corrected_v2), corrected_v2)
        expected_location = deepcopy(base_v2["location"])
        expected_location["city"] = "Recife"
        self.assertEqual(corrected_v2["location"], expected_location)
        for root in (
            "identity",
            "languages",
            "education",
            "credentials",
            "experience",
            "skills",
            "preferences",
            "constraints",
            "derived_matcher_signals",
        ):
            self.assertEqual(corrected_v2[root], base_v2[root])

    def test_large_correction_sources_partition_and_replay_after_reconstruction(self):
        def values(prefix):
            rows = []
            for index in range(40):
                leading = f"{prefix} {index:02d} "
                rows.append(leading + ("?" * (128 - len(leading))))
            return rows

        v1 = deepcopy(load_cases()[0]["expected_canonical_profile"])
        for field, prefix in (
            ("hard_constraints", "Hard"),
            ("soft_preferences", "Soft"),
            ("avoid_keywords", "Avoid"),
            ("excluded_domains", "Excluded"),
            ("accessibility_constraints", "Access"),
        ):
            v1["constraints"][field] = values(prefix)
        v1["location"]["work_authorization"] = "W" * 4_096
        v1["location"]["region"] = "R" * 1_500
        v1["provenance"]["field_sources"] = field_sources_for_profile(
            v1,
            PROFILE_SOURCE_EXTERNAL,
            explicit=True,
        )
        validate_canonical_profile(v1)
        large_v2 = convert_v1_to_v2(
            v1,
            persistent_profile_id=self.created.profile_id,
            source_ordinal_resolver=ordinal_resolver,
        )
        self._install_current_v2(
            large_v2,
            idempotency_key="partitioned-correction-current-profile-0001",
        )

        grant = self._grant()
        offer, _draft, _reviewed, _raw_about_you, updates = self._issue(
            grant,
            city="Partition City",
        )
        record = self.service._vault._records[offer.artifact_reference]
        command = record.snapshot.command
        correction_sources = command.sources[1:]
        self.assertGreater(len(correction_sources), 1)
        self.assertLessEqual(len(correction_sources), 15)
        self.assertTrue(
            all(
                len(source.content.encode("utf-8")) <= 32_768
                for source in correction_sources
            )
        )

        parts = [json.loads(source.content) for source in correction_sources]
        full_content = "".join(part["content_fragment"] for part in parts)
        full_bytes = full_content.encode("utf-8")
        self.assertEqual(
            [(part["part"], part["parts"]) for part in parts],
            [(index, len(parts)) for index in range(1, len(parts) + 1)],
        )
        self.assertTrue(
            all(
                part["partition_version"]
                == "user_confirmed_correction_partition_v1"
                and part["complete_content_bytes"] == len(full_bytes)
                and part["complete_content_sha256"]
                == hashlib.sha256(full_bytes).hexdigest()
                for part in parts
            )
        )
        self.assertEqual(json.loads(full_content)["updates"], updates)

        correction_ordinals = list(range(2, len(command.sources) + 1))
        structured = command.trusted_structured_profile()
        changed_sources = [
            source
            for source in structured["provenance"]["field_sources"]
            if source["source_kind"] == "user_correction"
        ]
        self.assertTrue(changed_sources)
        self.assertTrue(
            all(
                source["source_ordinals"] == correction_ordinals
                for source in changed_sources
            )
        )
        self.assertTrue(
            all(
                source["source_ordinals"] == [1]
                for source in structured["provenance"]["field_sources"]
                if source["source_kind"] != "user_correction"
            )
        )
        self.assertEqual(structured["constraints"], large_v2["constraints"])

        before = self._profile_counts()
        accepted = self._consume(grant, offer)
        self.assertEqual((accepted.state, accepted.replayed), ("corrected", False))
        after = self._profile_counts()
        self.assertEqual(after[1] - before[1], 1)
        self.assertEqual(after[2] - before[2], len(command.sources))
        current = self._current()
        self.assertEqual(
            current.trusted_dict(include_structured_profile=True)[
                "structured_profile"
            ]["constraints"],
            large_v2["constraints"],
        )

        self.service.close()
        rebuilt = self._build_service()
        current_grant = self._grant(service=rebuilt)
        replay = rebuilt.consume(
            grant=current_grant,
            csrf_secret=self.session["csrf_secret"],
            artifact_reference=offer.artifact_reference,
            csrf_proof=offer.csrf_proof,
        )
        self.assertEqual((replay.state, replay.replayed), ("corrected", True))
        self.assertEqual(self._profile_counts(), after)

    def test_malformed_current_v2_is_not_accepted_as_correction_authority(self):
        connection = self._connection()
        try:
            trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_product_profile_revisions_no_update'"
            ).fetchone()[0]
            malformed = "{"
            connection.execute("DROP TRIGGER trg_product_profile_revisions_no_update")
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE product_profile_revisions SET structured_profile_json=?, "
                "structured_profile_sha256=? WHERE revision_id=?",
                (
                    malformed,
                    hashlib.sha256(malformed.encode("utf-8")).hexdigest(),
                    self.created.revision_id,
                ),
            )
            connection.execute("PRAGMA ignore_check_constraints = OFF")
            connection.execute(trigger_sql)
            connection.commit()
        finally:
            connection.close()

        result = self.service.authorize_request(
            method="GET",
            authentication_input=self._headers(self.session),
            session_token=self.session["session_token"],
            csrf_secret=self.session["csrf_secret"],
        )
        self.assertEqual(result.state, "unavailable")
        self.assertIsNone(result.grant_for_service())

    def test_artifact_contains_complete_sources_and_appends_exactly_once(self):
        grant = self._grant()
        offer, _draft, _reviewed, raw_about_you, updates = self._issue(grant)
        record = self.service._vault._records[offer.artifact_reference]
        command = record.snapshot.command

        self.assertEqual(command.revision_kind, "correction")
        self.assertEqual(command.expected_current_revision_number, 1)
        self.assertEqual(command.correction_of_revision_id, self.created.revision_id)
        self.assertEqual(command.profile.profile_id, self.created.profile_id)
        self.assertEqual(
            tuple(source.source_type for source in command.sources),
            ("confirmed_about_you_text", "user_confirmed_correction"),
        )
        self.assertEqual(command.sources[0].content, raw_about_you)
        correction_source = json.loads(command.sources[1].content)
        self.assertEqual(correction_source["schema_version"], "user_confirmed_correction_v1")
        self.assertEqual(correction_source["updates"], updates)
        corrected_v2 = command.trusted_structured_profile()
        self.assertEqual(validate_canonical_profile_v2(corrected_v2), corrected_v2)
        self.assertEqual(corrected_v2["identity"]["profile_id"], self.created.profile_id)
        self.assertEqual(corrected_v2["location"]["city"], "Recife")

        self.assertEqual(self._profile_counts(), (1, 1, 1, 1))
        outcome = self._consume(grant, offer)
        self.assertEqual((outcome.state, outcome.replayed), ("corrected", False))
        self.assertEqual(self._profile_counts(), (1, 2, 3, 1))

        current = self._current()
        trusted = current.trusted_dict(include_structured_profile=True)
        self.assertEqual(current.revision_number, 2)
        self.assertEqual(trusted["structured_profile"]["location"]["city"], "Recife")
        self.assertEqual(trusted["structured_profile"]["identity"]["profile_id"], self.created.profile_id)
        connection = self._connection()
        try:
            revision = connection.execute(
                "SELECT revision_kind, previous_revision_id, correction_of_revision_id "
                "FROM product_profile_revisions WHERE revision_number=2"
            ).fetchone()
            source_types = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT source_type FROM product_profile_sources "
                    "WHERE revision_id=? ORDER BY source_ordinal",
                    (current.revision_id,),
                )
            )
        finally:
            connection.close()
        self.assertEqual(tuple(revision), ("correction", self.created.revision_id, self.created.revision_id))
        self.assertEqual(source_types, ("confirmed_about_you_text", "user_confirmed_correction"))

        replay = self._consume(grant, offer)
        self.assertEqual((replay.state, replay.replayed), ("corrected", True))
        self.assertEqual(self._profile_counts(), (1, 2, 3, 1))

    def test_exact_replay_converges_after_service_and_vault_reconstruction(self):
        grant = self._grant()
        offer, *_rest = self._issue(grant)
        accepted = self._consume(grant, offer)
        self.assertEqual((accepted.state, accepted.replayed), ("corrected", False))
        committed_counts = self._profile_counts()

        self.service.close()
        rebuilt = self._build_service()
        current_grant = self._grant(service=rebuilt)
        self.assertEqual(current_grant.base_revision_number, 2)
        replay = rebuilt.consume(
            grant=current_grant,
            csrf_secret=self.session["csrf_secret"],
            artifact_reference=offer.artifact_reference,
            csrf_proof=offer.csrf_proof,
        )
        self.assertEqual((replay.state, replay.replayed), ("corrected", True))
        self.assertEqual(self._profile_counts(), committed_counts)

    def test_cross_session_is_denied_and_changed_replay_conflicts(self):
        grant = self._grant()
        offer, *_rest = self._issue(grant, city="Recife")
        second_session = self._new_session("86")
        second_grant = self._grant(session=second_session)

        denied = self._consume(grant=second_grant, offer=offer, session=second_session)
        self.assertEqual((denied.state, denied.replayed), ("gone", False))
        self.assertEqual(self._profile_counts(), (1, 1, 1, 1))

        accepted = self._consume(grant, offer)
        self.assertEqual(accepted.state, "corrected")
        committed = self._profile_counts()

        changed_service = self._build_service(
            token_factory=lambda: offer.artifact_reference,
        )
        current_grant = self._grant(service=changed_service)
        changed_offer, *_rest = self._issue(
            current_grant,
            service=changed_service,
            city="Salvador",
        )
        self.assertEqual(changed_offer.artifact_reference, offer.artifact_reference)
        conflict = self._consume(
            current_grant,
            changed_offer,
            service=changed_service,
        )
        self.assertEqual((conflict.state, conflict.replayed), ("conflict", False))
        self.assertEqual(self._profile_counts(), committed)
        self.assertEqual(
            self._current().trusted_dict(include_structured_profile=True)["structured_profile"]["location"]["city"],
            "Recife",
        )

    def test_stale_base_cannot_replace_the_winning_correction(self):
        grant = self._grant()
        first, *_rest = self._issue(grant, city="Recife")
        second, *_rest = self._issue(grant, city="Salvador")

        winner = self._consume(grant, first)
        self.assertEqual((winner.state, winner.replayed), ("corrected", False))
        committed = self._profile_counts()
        stale = self._consume(grant, second)
        self.assertEqual((stale.state, stale.replayed), ("stale", False))
        self.assertEqual(self._profile_counts(), committed)
        self.assertEqual(self._consume(grant, second).state, "stale")
        current = self._current().trusted_dict(include_structured_profile=True)
        self.assertEqual(current["structured_profile"]["location"]["city"], "Recife")

    def test_concurrent_same_artifact_converges_on_one_revision(self):
        grant = self._grant()
        offer, *_rest = self._issue(grant)
        outcomes, failures = self._run_concurrently(
            lambda: self._consume(grant, offer),
            lambda: self._consume(grant, offer),
        )

        self.assertEqual(failures, [])
        self.assertEqual(
            sorted((outcome.state, outcome.replayed) for outcome in outcomes),
            [("corrected", False), ("corrected", True)],
        )
        self.assertEqual(self._profile_counts(), (1, 2, 3, 1))

    def test_concurrent_different_artifacts_have_one_winner_and_one_stale_result(self):
        append_lock = threading.Lock()

        def serialized_append(connection, command):
            with append_lock:
                return append_profile_revision(connection, command)

        service = self._build_service(append_revision=serialized_append)
        grant = self._grant(service=service)
        first, *_rest = self._issue(grant, service=service, city="Recife")
        second, *_rest = self._issue(grant, service=service, city="Salvador")
        outcomes, failures = self._run_concurrently(
            lambda: self._consume(grant, first, service=service),
            lambda: self._consume(grant, second, service=service),
        )

        self.assertEqual(failures, [])
        self.assertEqual(sorted(outcome.state for outcome in outcomes), ["corrected", "stale"])
        self.assertEqual(self._profile_counts(), (1, 2, 3, 1))
        city = self._current().trusted_dict(include_structured_profile=True)["structured_profile"]["location"]["city"]
        self.assertIn(city, {"Recife", "Salvador"})

    def test_vault_ttl_and_capacity_are_bounded_without_durable_mutation(self):
        grant = self._grant()
        expired, *_rest = self._issue(grant)
        self.monotonic += PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS + 1
        gone = self._consume(grant, expired)
        self.assertEqual(gone.state, "gone")
        self.assertEqual(self._profile_counts(), (1, 1, 1, 1))

        offers = [self._issue(grant)[0] for _index in range(PROFILE_CORRECTION_ARTIFACT_CAPACITY)]
        self.assertEqual(len({offer.artifact_reference for offer in offers}), PROFILE_CORRECTION_ARTIFACT_CAPACITY)
        self.assertEqual(len(self.service._vault._records), PROFILE_CORRECTION_ARTIFACT_CAPACITY)
        with self.assertRaisesRegex(RuntimeError, "profile_correction_confirmation_unavailable"):
            self._issue(grant)

        self.monotonic += PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS + 1
        replacement, *_rest = self._issue(grant)
        self.assertIsNotNone(replacement.artifact_reference)
        self.assertEqual(len(self.service._vault._records), 1)
        self.assertEqual(self._profile_counts(), (1, 1, 1, 1))

    def test_definite_append_failure_rolls_back_and_same_artifact_retries(self):
        fail_once = [True]

        def inject(boundary):
            if boundary == "append.after_source_insert" and fail_once[0]:
                fail_once[0] = False
                raise RuntimeError("injected_definite_rollback")

        repository = PersistentProfileRepository(_failure_injector=inject)
        service = self._build_service(append_revision=repository.append)
        grant = self._grant(service=service)
        offer, *_rest = self._issue(grant, service=service)

        unavailable = self._consume(grant, offer, service=service)
        self.assertEqual(unavailable.state, "unavailable")
        self.assertEqual(self._profile_counts(), (1, 1, 1, 1))
        retry = self._consume(grant, offer, service=service)
        self.assertEqual((retry.state, retry.replayed), ("corrected", False))
        self.assertEqual(self._profile_counts(), (1, 2, 3, 1))

    def test_uncertain_append_reconciles_as_exact_idempotent_replay(self):
        uncertain_once = [True]

        def uncertain_append(connection, command):
            result = append_profile_revision(connection, command)
            if uncertain_once[0]:
                uncertain_once[0] = False
                raise PersistentProfileRepositoryOutcomeUncertain()
            return result

        service = self._build_service(append_revision=uncertain_append)
        grant = self._grant(service=service)
        offer, *_rest = self._issue(grant, service=service)

        unavailable = self._consume(grant, offer, service=service)
        self.assertEqual(unavailable.state, "unavailable")
        committed = self._profile_counts()
        self.assertEqual(committed, (1, 2, 3, 1))
        fresh_grant = self._grant(service=service)
        self.assertEqual(fresh_grant.base_revision_number, 2)
        retry = self._consume(fresh_grant, offer, service=service)
        self.assertEqual((retry.state, retry.replayed), ("corrected", True))
        self.assertEqual(self._profile_counts(), committed)

    def test_correction_stage_get_head_parity_and_restart_lost_draft_are_write_free(self):
        registry = review_support.MatchRunRegistry(
            max_size=PROFILE_CORRECTION_ARTIFACT_CAPACITY,
            absolute_ttl_seconds=PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS,
            _retention_clock=lambda: self.registry_time,
        )
        browser = self._build_browser(correction_registry=registry)
        durable_before = _logical_snapshot(self.path)

        def assert_get_head_parity(target):
            get_response = browser.handle(
                "GET",
                target,
                self._browser_headers(self.session),
            )
            head_response = browser.handle(
                "HEAD",
                target,
                self._browser_headers(self.session),
            )
            self.assertEqual((get_response.status, head_response.status), (200, 200))
            self.assertEqual(head_response.body, b"")
            self.assertEqual(
                self._response_header(get_response, "Content-Length"),
                self._response_header(head_response, "Content-Length"),
            )
            self.assertEqual(_logical_snapshot(self.path), durable_before)
            return get_response

        assert_get_head_parity("/account/profile?correction=start")
        review_target = self._start_browser_correction(browser)
        self.assertEqual(_logical_snapshot(self.path), durable_before)
        review = assert_get_head_parity(review_target)
        edit_target = next(
            href for href in self._markup(review).links if "correction=edit" in href
        )
        assert_get_head_parity(edit_target)

        restarted_registry = review_support.MatchRunRegistry(
            max_size=PROFILE_CORRECTION_ARTIFACT_CAPACITY,
            absolute_ttl_seconds=PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS,
            _retention_clock=lambda: self.registry_time,
        )
        restarted_browser = self._build_browser(
            correction_registry=restarted_registry
        )
        lost = restarted_browser.handle(
            "GET",
            review_target,
            self._browser_headers(self.session),
        )
        self.assertEqual(lost.status, 410)
        self.assertEqual(_logical_snapshot(self.path), durable_before)

    def test_restart_lost_unapplied_artifact_has_no_durable_fallback(self):
        grant = self._grant()
        offer, *_rest = self._issue(grant)
        durable_before = _logical_snapshot(self.path)

        self.assertTrue(self.service.close())
        rebuilt = self._build_service()
        fresh_grant = self._grant(service=rebuilt)
        outcome = self._consume(
            fresh_grant,
            offer,
            service=rebuilt,
        )

        self.assertEqual((outcome.state, outcome.replayed), ("gone", False))
        self.assertEqual(_logical_snapshot(self.path), durable_before)
        self.assertEqual(self._profile_counts(), (1, 1, 1, 1))

    def test_expired_revoked_and_logged_out_sessions_reject_before_body_read(self):
        browser = self._build_browser()
        body = urlencode((("intent", "update_profile"),)).encode("ascii")

        def revoke(session, reason):
            connection = self._connection()
            try:
                version = connection.execute(
                    "SELECT session_version FROM account_sessions "
                    "WHERE session_id=?",
                    (session["session_id"],),
                ).fetchone()[0]
                accounts.revoke_current_session(
                    connection,
                    session_token=session["session_token"],
                    expected_session_version=version,
                    reason=reason,
                    now=self.now,
                )
                connection.commit()
            finally:
                connection.close()

        def assert_rejected(session):
            target = (
                "/account/profile?action=start&proof="
                + profile_correction_action_csrf_proof(
                    session["csrf_secret"],
                    "start",
                )
            )
            durable_before = _logical_snapshot(self.path)
            probe = _ReadProbe(body)
            response = browser.handle(
                "POST",
                target,
                self._browser_headers(session, body=body),
                probe,
            )
            self.assertEqual((response.status, probe.read_count), (403, 0))
            self.assertEqual(_logical_snapshot(self.path), durable_before)
            rendered = response.body.decode("utf-8")
            for private_value in (
                session["account_id"],
                session["principal_id"],
                session["session_id"],
                session["session_token"],
            ):
                self.assertNotIn(private_value, rendered)

        revoked = self._new_session("93")
        revoke(revoked, "explicit_revoke")
        with self.subTest(session_state="revoked"):
            assert_rejected(revoked)

        logged_out = self._new_session("94")
        revoke(logged_out, "user_logout")
        with self.subTest(session_state="logged_out"):
            assert_rejected(logged_out)

        expired = self._new_session("95")
        self.now += timedelta(hours=3)
        with self.subTest(session_state="expired"):
            assert_rejected(expired)

    def test_full_logical_delta_and_rejected_or_stale_attempts_are_atomic(self):
        grant = self._grant()
        winner, *_rest = self._issue(grant, city="Recife")
        stale_offer, *_rest = self._issue(grant, city="Salvador")
        before = dict(_logical_snapshot(self.path))

        accepted = self._consume(grant, winner)
        self.assertEqual((accepted.state, accepted.replayed), ("corrected", False))
        after = dict(_logical_snapshot(self.path))
        changed_tables = {
            table for table in before if before[table] != after[table]
        }
        self.assertEqual(
            changed_tables,
            {
                "product_profile_revisions",
                "product_profile_sources",
            },
        )
        self.assertEqual(self._profile_counts(), (1, 2, 3, 1))

        committed = _logical_snapshot(self.path)
        stale = self._consume(grant, stale_offer)
        self.assertEqual((stale.state, stale.replayed), ("stale", False))
        self.assertEqual(_logical_snapshot(self.path), committed)

        current_grant = self._grant()
        tampered_reference = (
            ("A" if winner.artifact_reference[0] != "A" else "B")
            + winner.artifact_reference[1:]
        )
        rejected = self.service.consume(
            grant=current_grant,
            csrf_secret=self.session["csrf_secret"],
            artifact_reference=tampered_reference,
            csrf_proof=corrections_module.profile_correction_csrf_proof(
                self.session["csrf_secret"],
                tampered_reference,
            ),
        )
        self.assertEqual((rejected.state, rejected.replayed), ("gone", False))
        self.assertEqual(_logical_snapshot(self.path), committed)

        changed_service = self._build_service(
            token_factory=lambda: winner.artifact_reference,
        )
        changed_grant = self._grant(service=changed_service)
        changed_offer, *_rest = self._issue(
            changed_grant,
            service=changed_service,
            city="Fortaleza",
        )
        conflict = self._consume(
            changed_grant,
            changed_offer,
            service=changed_service,
        )
        self.assertEqual((conflict.state, conflict.replayed), ("conflict", False))
        self.assertEqual(_logical_snapshot(self.path), committed)

    def test_all_correction_stages_escape_and_redact_and_legacy_routes_stay_absent(self):
        hostile = '<img src=x onerror="alert(1)">'
        grant = self._grant()
        offer, *_rest = self._issue(grant, city=hostile)
        self.assertEqual(self._consume(grant, offer).state, "corrected")
        browser = self._build_browser()

        start = browser.handle(
            "GET",
            "/account/profile?correction=start",
            self._browser_headers(self.session),
        )
        start_form = self._form(start, "intent")
        started, start_probe = self._post_form(
            browser,
            start_form["action"],
            start_form["fields"],
        )
        self.assertEqual((started.status, start_probe.read_count), (303, 1))
        review = browser.handle(
            "GET",
            self._response_header(started, "Location"),
            self._browser_headers(self.session),
        )
        edit_target = next(
            href for href in self._markup(review).links if "correction=edit" in href
        )
        edit = browser.handle(
            "GET",
            edit_target,
            self._browser_headers(self.session),
        )
        edit_form = self._form(
            edit,
            "edit_run_id",
            "review_token",
            "schema_version",
        )
        edit_fields = self._set_form_field(
            list(edit_form["fields"]),
            "credentials_confirmed",
            "1",
        )
        redrafted, redraft_probe = self._post_form(
            browser,
            edit_form["action"],
            edit_fields,
        )
        self.assertEqual((redrafted.status, redraft_probe.read_count), (303, 1))
        reviewed = browser.handle(
            "GET",
            self._response_header(redrafted, "Location"),
            self._browser_headers(self.session),
        )
        confirm_form = self._form(reviewed, "draft", "review_token")
        confirm_fields = self._set_form_field(
            list(confirm_form["fields"]),
            "confirmed",
            "1",
        )
        apply_page, confirm_probe = self._post_form(
            browser,
            confirm_form["action"],
            confirm_fields,
        )
        self.assertEqual((apply_page.status, confirm_probe.read_count), (200, 1))

        connection = self._connection()
        try:
            private_values = {
                value
                for table in (
                    "accounts",
                    "auth_identities",
                    "account_sessions",
                    "product_principals",
                    "principal_account_bindings",
                    "ownership_binding_events",
                    "product_profiles",
                    "product_profile_revisions",
                    "product_profile_sources",
                )
                for column in (
                    row[1]
                    for row in connection.execute(f'PRAGMA table_info("{table}")')
                    if row[1].endswith("_id")
                    or "sha256" in row[1]
                    or "fingerprint" in row[1]
                )
                for row in connection.execute(
                    f'SELECT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL'
                )
                for value in (row[0],)
                if type(value) is str and len(value) >= 16
            }
        finally:
            connection.close()
        private_values.update(
            {
                self.session["session_token"],
                self.session["csrf_secret"],
            }
        )
        public = "\n".join(
            response.body.decode("utf-8")
            for response in (
                start,
                started,
                review,
                edit,
                redrafted,
                reviewed,
                apply_page,
            )
        )
        self.assertNotIn(hostile, public)
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;", public)
        for private_value in private_values:
            self.assertNotIn(private_value, public)
        for private_label in (
            "profile_id",
            "revision_id",
            "source_type",
            "source_ordinal",
            "request_fingerprint",
            "structured_profile_sha256",
            "source_bundle_sha256",
            "correction_of_revision_id",
        ):
            self.assertNotIn(private_label, public)
        for forbidden in (
            "/preview",
            "/tracker",
            "/dashboard",
            "/action",
            "My Jobs",
            "demo persona",
            "archive profile",
            "reactivate",
            "delete profile",
            "rollback",
        ):
            self.assertNotIn(forbidden.lower(), public.lower())

        legacy_body = urlencode((("intent", "update_profile"),)).encode("ascii")
        for target in (
            "/preview",
            "/tracker",
            "/dashboard",
            "/action",
            "/local-product",
        ):
            with self.subTest(legacy_target=target):
                self.assertFalse(browser.matches_route(target))
                get_response = browser.handle(
                    "GET",
                    target,
                    self._browser_headers(self.session),
                )
                self.assertEqual(get_response.status, 400)
                probe = _ReadProbe(legacy_body)
                post_response = browser.handle(
                    "POST",
                    target,
                    self._browser_headers(self.session, body=legacy_body),
                    probe,
                )
                self.assertEqual((post_response.status, probe.read_count), (400, 0))

    def test_correction_has_zero_egress_and_later_session_reads_same_updated_profile(self):
        browser = self._build_browser(with_matches=True)
        durable_before = dict(_logical_snapshot(self.path))

        with sockets_blocked():
            apply_form = self._browser_apply_offer(
                browser,
                changes=(("city", "Recife"),),
            )
            applied, probe = self._post_form(
                browser,
                apply_form["action"],
                apply_form["fields"],
            )
            self.assertEqual((applied.status, probe.read_count), (303, 1))
            self.assertEqual(self._response_header(applied, "Location"), "/find-matches")
            matches = browser.handle(
                "GET",
                "/find-matches",
                self._browser_headers(self.session),
            )
        self.assertEqual(matches.status, 200)

        durable_after = dict(_logical_snapshot(self.path))
        self.assertEqual(
            {
                table
                for table in durable_before
                if durable_before[table] != durable_after[table]
            },
            {
                "product_profile_revisions",
                "product_profile_sources",
            },
        )
        current = self._current()
        current_v2 = current.trusted_dict(include_structured_profile=True)[
            "structured_profile"
        ]
        self.assertEqual((current.profile_id, current.revision_number), (self.created.profile_id, 2))
        self.assertEqual(current_v2["location"]["city"], "Recife")

        later_session = self._new_session("96")
        later_grant = self._grant(session=later_session)
        self.assertEqual(later_grant.profile_for_repository().profile_id, self.created.profile_id)
        self.assertEqual(later_grant.base_revision_number, 2)
        later_page = browser.handle(
            "GET",
            "/account/profile",
            self._browser_headers(later_session),
        )
        later_body = later_page.body.decode("utf-8")
        self.assertEqual(later_page.status, 200)
        self.assertIn("Recife", later_body)
        self.assertNotIn(self.created.profile_id, later_body)
        self.assertNotIn(current.revision_id, later_body)

    def test_close_is_bounded_truthful_for_append_and_reconstruction(self):
        append_entered = threading.Event()
        append_release = threading.Event()
        append_results = []
        append_failures = []
        waiter_results = []
        waiter_failures = []

        def blocked_append(connection, command):
            append_entered.set()
            if not append_release.wait(5):
                raise RuntimeError("profile_correction_append_release_timeout")
            return append_profile_revision(connection, command)

        service = self._build_service(append_revision=blocked_append)
        grant = self._grant(service=service)
        offer, *_rest = self._issue(grant, service=service)

        def consume_append():
            try:
                append_results.append(
                    self._consume(grant, offer, service=service)
                )
            except BaseException as exc:
                append_failures.append(exc)

        def consume_waiter():
            try:
                waiter_results.append(
                    self._consume(grant, offer, service=service)
                )
            except BaseException as exc:
                waiter_failures.append(exc)

        append_worker = threading.Thread(
            target=consume_append,
            name="profile-correction-close-append",
        )
        waiter_worker = threading.Thread(
            target=consume_waiter,
            name="profile-correction-close-waiter",
        )
        try:
            append_worker.start()
            self.assertTrue(append_entered.wait(2))
            waiter_worker.start()
            operation_deadline = time.monotonic() + 2
            with service._vault._condition:
                while len(service._vault._operations) != 2:
                    remaining = operation_deadline - time.monotonic()
                    self.assertGreater(remaining, 0)
                    service._vault._condition.wait(timeout=remaining)
            with mock.patch.object(
                corrections_module,
                "_PROFILE_CORRECTION_VAULT_CLOSE_WAIT_SECONDS",
                0.05,
            ):
                before = time.monotonic()
                self.assertFalse(service.close())
                elapsed = time.monotonic() - before
            self.assertLess(elapsed, 1.0)
            self.assertFalse(service.closed)
            with service._vault._condition:
                self.assertTrue(service._vault._closing)
                self.assertFalse(service._vault._closed)
                self.assertEqual(len(service._vault._operations), 1)
                record = service._vault._records.get(
                    offer.artifact_reference
                )
                self.assertIsNotNone(record)
                self.assertEqual(record.state, "in_flight")
            self.assertEqual(self._profile_counts(), (1, 1, 1, 1))
        finally:
            append_release.set()
            append_worker.join(timeout=5)
            waiter_worker.join(timeout=5)
        self.assertFalse(append_worker.is_alive())
        self.assertFalse(waiter_worker.is_alive())
        self.assertEqual(append_failures, [])
        self.assertEqual(waiter_failures, [])
        self.assertEqual(len(waiter_results), 1)
        self.assertEqual(
            (waiter_results[0].state, waiter_results[0].replayed),
            ("unavailable", False),
        )
        self.assertEqual(len(append_results), 1)
        self.assertEqual(
            (append_results[0].state, append_results[0].replayed),
            ("corrected", False),
        )
        self.assertEqual(self._profile_counts(), (1, 2, 3, 1))
        self.assertTrue(service.close())
        self.assertTrue(service.closed)
        with service._vault._condition:
            self.assertEqual(service._vault._operations, set())
            self.assertEqual(service._vault._records, {})

        replay_entered = threading.Event()
        replay_release = threading.Event()
        replay_results = []
        replay_failures = []

        def blocked_replay(connection, command):
            replay_entered.set()
            if not replay_release.wait(5):
                raise RuntimeError("profile_correction_replay_release_timeout")
            return append_profile_revision(connection, command)

        rebuilt = self._build_service(append_revision=blocked_replay)
        fresh_grant = self._grant(service=rebuilt)
        committed = self._profile_counts()

        def consume_replay():
            try:
                replay_results.append(
                    self._consume(fresh_grant, offer, service=rebuilt)
                )
            except BaseException as exc:
                replay_failures.append(exc)

        replay_worker = threading.Thread(
            target=consume_replay,
            name="profile-correction-close-replay",
        )
        try:
            replay_worker.start()
            self.assertTrue(replay_entered.wait(2))
            with mock.patch.object(
                corrections_module,
                "_PROFILE_CORRECTION_VAULT_CLOSE_WAIT_SECONDS",
                0.05,
            ):
                before = time.monotonic()
                self.assertFalse(rebuilt.close())
                elapsed = time.monotonic() - before
            self.assertLess(elapsed, 1.0)
            self.assertFalse(rebuilt.closed)
            with rebuilt._vault._condition:
                self.assertTrue(rebuilt._vault._closing)
                self.assertFalse(rebuilt._vault._closed)
                self.assertEqual(len(rebuilt._vault._operations), 1)
                self.assertEqual(rebuilt._vault._records, {})
            self.assertEqual(self._profile_counts(), committed)
        finally:
            replay_release.set()
            replay_worker.join(timeout=5)
        self.assertFalse(replay_worker.is_alive())
        self.assertEqual(replay_failures, [])
        self.assertEqual(len(replay_results), 1)
        self.assertEqual(
            (replay_results[0].state, replay_results[0].replayed),
            ("corrected", True),
        )
        self.assertEqual(self._profile_counts(), committed)
        self.assertTrue(rebuilt.close())
        self.assertTrue(rebuilt.closed)
        with rebuilt._vault._condition:
            self.assertEqual(rebuilt._vault._operations, set())
            self.assertEqual(rebuilt._vault._records, {})


if __name__ == "__main__":
    unittest.main()
