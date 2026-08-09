from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import timedelta
from email.message import Message
import hashlib
import hmac
from html.parser import HTMLParser
from http import HTTPStatus
import io
import json
import logging
from pathlib import Path
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import urlencode, urlsplit

from scripts import local_product_app
from scripts.local_product_app import (
    ActionError,
    ConfirmedProfileCreation,
    MatchRunRegistry,
    apply_identity_free_profile_review,
    confirm_profile_review,
    create_match_run,
    normalize_identity_free_profile_input,
    profile_draft_fingerprint,
    profile_review_form_fields,
    profile_review_language_slots,
    profile_review_updates_from_form,
)
from tests.accounts_test_support import INVITATION_KEY
from tests.browser_session_authentication_test_support import (
    REQUEST_AT,
    install_browser_authentication_database,
    seed_browser_session,
)
from tests.durable_google_login_browser_test_support import (
    cookie_header,
    cookie_values,
    form_body,
    https_request,
    loopback_and_in_memory_provider_only,
    provider_callback_for,
    running_https_browser_app,
    running_https_production_launcher_app,
    temporary_browser_login_state,
)
from tests.persistent_profile_read_authorization_test_support import (
    seed_authorized_account,
    transition_binding,
)
from tests.ownership_test_support import add_active_user, add_alias
from wahojobs import accounts, ownership
from wahojobs import persistent_profile_creation
from wahojobs import persistent_profiles as persistent_profiles_domain
from wahojobs.browser_session_authentication import (
    DurableBrowserSessionAuthenticationGateway,
)
from wahojobs.durable_google_login_runtime import build_durable_google_login_runtime
from wahojobs.persistent_profile_creation import (
    ConfirmedProfileArtifactUnavailable,
    ConfirmedProfileArtifactVault,
    DurablePersistentProfileCreateAuthorizationGateway,
    PersistentProfileCreationService,
    PROFILE_CREATE_ARTIFACT_CAPACITY,
    PROFILE_CREATE_ARTIFACT_LIFETIME_SECONDS,
    profile_create_csrf_proof,
)
from wahojobs.persistent_profile_read_authorization import (
    DurablePersistentProfileReadAuthorizationGateway,
)
from wahojobs.persistent_profiles_application import (
    PersistentProfileApplicationService,
)
from wahojobs.persistent_profiles import (
    IdentityFreeCanonicalProfileV1,
    IdentityFreeCanonicalProfileV2,
    PersistentProfileDomainError,
)
from wahojobs.persistent_profiles_browser import (
    PersistentProfileBrowserIntegration,
)
from wahojobs.persistent_profiles_repository import PersistentProfileRepository


PUBLIC_ORIGIN = "https://localhost:8443"
PUBLIC_AUTHORITY = "localhost:8443"
RAW_ABOUT_YOU = "Remote Python engineer seeking careful evaluation work."
EXPECTED_DISPLAY_NAME = "python / software engineering"
REVIEWER_PREVIEW_ID = "preview_profile_reviewer_supplied_7e91d5"
FORMER_SEMANTIC_PROFILE_ID = "prf_0123456789abcdef0123456789abcdef"
UNICODE_ABOUT_YOU = "Engenheira Python — café, Português e 👩🏽‍💻 — buscando avaliação cuidadosa."


class _TokenFactory:
    def __init__(self):
        self.value = 0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            self.value += 1
            return str(self.value).rjust(43, "0")


class _CollisionThenUniqueTokenFactory:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls <= 17:
            return "C" * 43
        return ("U" + str(self.calls).rjust(42, "0"))[-43:]


class _ReadProvider:
    def __init__(self, path, *, timeout=0.15):
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
    def __init__(
        self,
        path,
        *,
        timeout=0.15,
        fail_after_commit_once=False,
        before_open=None,
    ):
        self.path = Path(path)
        self.timeout = timeout
        self.fail_after_commit_once = fail_after_commit_once
        self.before_open = before_open

    def __call__(self):
        @contextmanager
        def scope():
            if self.before_open is not None:
                self.before_open()
            connection = sqlite3.connect(self.path, timeout=self.timeout)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            failed = False
            try:
                yield connection
            except BaseException:
                failed = True
                raise
            finally:
                connection.close()
            if not failed and self.fail_after_commit_once:
                self.fail_after_commit_once = False
                raise RuntimeError("injected_post_commit_connection_release")

        return scope()


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
                    connection.execute(
                        f'SELECT * FROM "{table}" ORDER BY rowid'
                    ).fetchall()
                ),
            )
            for table in tables
        )
    finally:
        connection.close()


def _profile_counts(path):
    connection = sqlite3.connect(path)
    try:
        return tuple(
            connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in (
                "product_profiles",
                "product_profile_revisions",
                "product_profile_sources",
            )
        )
    finally:
        connection.close()


def _seed_invited_match_inventory(connection, *, observed_at):
    observed = observed_at.isoformat()
    connection.execute(
        "INSERT INTO companies (id, name, slug, careers_url, source_tier, "
        "inventory_model, market_count_policy) VALUES "
        "(901, 'Configured Invited Inventory', 'configured-invited', "
        "'https://jobs.example.test', 'core', 'live_feed', 'count_live')"
    )
    connection.execute(
        "INSERT INTO canonical_opportunities ("
        "id, company_id, canonical_key, canonical_title, normalized_title, "
        "source_category, first_seen_at, last_seen_at, is_active, variant_count"
        ") VALUES (901, 901, 'invited-python-evaluator', "
        "'Distinctive Remote Python Evaluation Engineer', "
        "'distinctive remote python evaluation engineer', "
        "'Software Engineering', ?, ?, 1, 1)",
        (observed, observed),
    )
    connection.execute(
        "INSERT INTO crawl_runs (id, company_id, status, started_at, "
        "finished_at, used_sample_data) VALUES (901, 901, 'success', ?, ?, 0)",
        (observed, observed),
    )
    connection.execute(
        "INSERT INTO jobs ("
        "id, company_id, canonical_opportunity_id, external_id, title, location, "
        "department, expertise, commitment, url, source_hash, opportunity_kind, "
        "availability_basis, include_in_live_market_estimate, first_seen_at, "
        "last_seen_at, is_active, updated_at"
        ") VALUES (901, 901, 901, 'invited-python-evaluator', "
        "'Distinctive Remote Python Evaluation Engineer', 'Remote', "
        "'Software Engineering', 'Software Engineering', 'Contract', "
        "'https://jobs.example.test/distinctive-invited-python', "
        "'configured-invited-python-hash', 'live_posting', 'api_feed', 1, "
        "?, ?, 1, ?)",
        (observed, observed, observed),
    )
    connection.commit()


def _merge_response_cookies(values, response):
    for name, value in cookie_values(response).items():
        if value:
            values[name] = value
        else:
            values.pop(name, None)


class _SubmittedFormParser(HTMLParser):
    def __init__(self, form_id):
        super().__init__(convert_charrefs=True)
        self._form_id = form_id
        self._inside = False
        self._textarea = None
        self._textarea_parts = []
        self._select = None
        self._select_first = None
        self._selected_option = None
        self.fields = []
        self.action = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form" and attributes.get("id") == self._form_id:
            self._inside = True
            self.action = attributes.get("action")
            return
        if not self._inside:
            return
        if tag == "input":
            name = attributes.get("name")
            input_type = attributes.get("type", "text").lower()
            if not name or input_type in {"button", "reset", "submit"}:
                return
            if input_type in {"checkbox", "radio"} and "checked" not in attributes:
                return
            self.fields.append((name, attributes.get("value", "")))
        elif tag == "textarea":
            self._textarea = attributes.get("name")
            self._textarea_parts = []
        elif tag == "select":
            self._select = attributes.get("name")
            self._select_first = None
            self._selected_option = None
        elif tag == "option" and self._select is not None:
            value = attributes.get("value", "")
            if self._select_first is None:
                self._select_first = value
            if "selected" in attributes:
                self._selected_option = value

    def handle_data(self, data):
        if self._inside and self._textarea is not None:
            self._textarea_parts.append(data)

    def handle_endtag(self, tag):
        if not self._inside:
            return
        if tag == "textarea" and self._textarea is not None:
            self.fields.append(
                (self._textarea, "".join(self._textarea_parts))
            )
            self._textarea = None
            self._textarea_parts = []
        elif tag == "select" and self._select is not None:
            self.fields.append(
                (
                    self._select,
                    self._selected_option
                    if self._selected_option is not None
                    else (self._select_first or ""),
                )
            )
            self._select = None
            self._select_first = None
            self._selected_option = None
        elif tag == "form":
            self._inside = False


def _submitted_form(body, form_id):
    parser = _SubmittedFormParser(form_id)
    parser.feed(body.decode("utf-8"))
    parser.close()
    return parser.action, tuple(parser.fields)


class ReviewedProfileSourceBundleTests(unittest.TestCase):
    @staticmethod
    def _reviewed_profile(*, force_confirmation_sources=False):
        canonical = normalize_identity_free_profile_input(
            RAW_ABOUT_YOU,
            "short_paragraph",
        )
        fields = profile_review_form_fields(canonical, "run", "R" * 43)
        updates = profile_review_updates_from_form(
            {name: [value] for name, value in fields.items()},
            profile_review_language_slots(canonical),
        )
        reviewed = apply_identity_free_profile_review(canonical, updates)
        if force_confirmation_sources:
            mapping = reviewed.to_mapping()
            for detail in mapping["provenance"]["field_sources"].values():
                if detail["source"] == "user_correction":
                    detail["source"] = "user_confirmation"
            reviewed = IdentityFreeCanonicalProfileV1.from_mapping(mapping)
        return reviewed, updates

    def test_bundle_preserves_deterministic_sources_resolver_and_redaction(self):
        reviewed, updates = self._reviewed_profile()
        expected_correction = json.dumps(
            {
                "schema_version": "user_confirmed_correction_v1",
                "updates": updates,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        bundle = persistent_profile_creation.prepare_reviewed_profile_source_bundle(
            reviewed,
            RAW_ABOUT_YOU,
            updates,
            REQUEST_AT,
        )

        self.assertEqual(bundle.correction_json, expected_correction)
        self.assertEqual(
            tuple(source.source_type for source in bundle.sources),
            ("confirmed_about_you_text", "user_confirmed_correction"),
        )
        self.assertEqual(
            tuple(source.content for source in bundle.sources),
            (RAW_ABOUT_YOU, expected_correction),
        )
        self.assertEqual(
            tuple(source.confirmed_at for source in bundle.sources),
            (REQUEST_AT.isoformat(timespec="seconds"),) * 2,
        )

        real_convert = persistent_profile_creation.convert_v1_to_v2
        with mock.patch.object(
            persistent_profile_creation,
            "convert_v1_to_v2",
            wraps=real_convert,
        ) as convert:
            profile_v2 = bundle.build_canonical_v2(FORMER_SEMANTIC_PROFILE_ID)
        self.assertEqual(convert.call_count, 1)
        self.assertEqual(
            convert.call_args.args[0]["identity"]["profile_id"],
            FORMER_SEMANTIC_PROFILE_ID,
        )
        self.assertEqual(
            convert.call_args.kwargs["persistent_profile_id"],
            FORMER_SEMANTIC_PROFILE_ID,
        )
        resolver = convert.call_args.kwargs["source_ordinal_resolver"]
        self.assertEqual(resolver("ignored", "parsed_free_text", False), (1,))
        self.assertEqual(resolver("ignored", "user_confirmation", True), (1,))
        self.assertEqual(resolver("ignored", "user_correction", True), (2,))
        with self.assertRaisesRegex(ValueError, "unexpected_profile_provenance"):
            resolver("ignored", "external_source", True)
        self.assertEqual(
            profile_v2["identity"]["profile_id"],
            FORMER_SEMANTIC_PROFILE_ID,
        )
        rendered_v2 = json.dumps(profile_v2, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(RAW_ABOUT_YOU, rendered_v2)
        self.assertNotIn(expected_correction, rendered_v2)
        self.assertNotIn(RAW_ABOUT_YOU, repr(bundle))
        self.assertNotIn(expected_correction, repr(bundle))
        with self.assertRaises((AttributeError, TypeError)):
            bundle.correction_json = "changed"
        with self.assertRaisesRegex(
            TypeError,
            "reviewed_profile_source_bundle_not_serializable",
        ):
            bundle.__reduce_ex__(4)

    def test_require_correction_forces_second_source_without_remapping_provenance(self):
        reviewed, updates = self._reviewed_profile(
            force_confirmation_sources=True
        )
        ordinary = persistent_profile_creation.prepare_reviewed_profile_source_bundle(
            reviewed,
            RAW_ABOUT_YOU,
            updates,
            REQUEST_AT,
        )
        self.assertIsNone(ordinary.correction_json)
        self.assertEqual(
            tuple(source.source_type for source in ordinary.sources),
            ("confirmed_about_you_text",),
        )

        forced = persistent_profile_creation.prepare_reviewed_profile_source_bundle(
            reviewed,
            RAW_ABOUT_YOU,
            updates,
            REQUEST_AT,
            require_correction=True,
        )
        expected_correction = json.dumps(
            {
                "schema_version": "user_confirmed_correction_v1",
                "updates": updates,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        self.assertEqual(forced.correction_json, expected_correction)
        self.assertEqual(
            tuple(source.source_type for source in forced.sources),
            ("confirmed_about_you_text", "user_confirmed_correction"),
        )
        self.assertIs(
            type(forced.sources[1]),
            persistent_profiles_domain.UserConfirmedCorrectionSourceDraft,
        )
        profile_v2 = forced.build_canonical_v2(FORMER_SEMANTIC_PROFILE_ID)
        self.assertEqual(
            {
                tuple(source["source_ordinals"])
                for source in profile_v2["provenance"]["field_sources"]
            },
            {(1,)},
        )

    @staticmethod
    def _reassemble_partitioned_sources(sources):
        parts = [json.loads(source.content) for source in sources]
        expected_count = len(parts)
        expected_hash = parts[0]["complete_content_sha256"]
        expected_bytes = parts[0]["complete_content_bytes"]
        for index, part in enumerate(parts, start=1):
            if part["partition_version"] != "user_confirmed_correction_partition_v1":
                raise AssertionError("unexpected partition version")
            if (part["part"], part["parts"]) != (index, expected_count):
                raise AssertionError("unexpected partition ordering")
            if (
                part["complete_content_sha256"] != expected_hash
                or part["complete_content_bytes"] != expected_bytes
            ):
                raise AssertionError("inconsistent partition manifest")
        content = "".join(part["content_fragment"] for part in parts)
        encoded = content.encode("utf-8")
        if len(encoded) != expected_bytes:
            raise AssertionError("partition byte length mismatch")
        if hashlib.sha256(encoded).hexdigest() != expected_hash:
            raise AssertionError("partition digest mismatch")
        return content

    def test_correction_source_partition_preserves_exact_boundary_and_content(self):
        empty = persistent_profile_creation._correction_source_json({"payload": ""})
        overhead = len(empty.encode("utf-8"))
        exact_updates = {
            "payload": "x" * (persistent_profiles_domain.MAX_SOURCE_BYTES - overhead)
        }
        exact_json = persistent_profile_creation._correction_source_json(
            exact_updates
        )
        self.assertEqual(
            len(exact_json.encode("utf-8")),
            persistent_profiles_domain.MAX_SOURCE_BYTES,
        )
        exact_content, exact_sources = (
            persistent_profile_creation._prepare_user_confirmed_correction_sources(
                exact_updates,
                REQUEST_AT,
            )
        )
        self.assertEqual(exact_content, exact_json)
        self.assertEqual(len(exact_sources), 1)
        self.assertEqual(exact_sources[0].content, exact_json)

        over_updates = {"payload": exact_updates["payload"] + "x"}
        over_json, over_sources = (
            persistent_profile_creation._prepare_user_confirmed_correction_sources(
                over_updates,
                REQUEST_AT,
            )
        )
        self.assertEqual(len(over_json.encode("utf-8")), 32_769)
        self.assertGreater(len(over_sources), 1)
        self.assertLessEqual(len(over_sources), 15)
        self.assertTrue(
            all(
                len(source.content.encode("utf-8"))
                <= persistent_profiles_domain.MAX_SOURCE_BYTES
                for source in over_sources
            )
        )
        self.assertEqual(
            self._reassemble_partitioned_sources(over_sources),
            over_json,
        )

    def test_correction_source_partition_is_multibyte_and_escape_safe(self):
        updates = {"oversized_csv": ("café 👩🏽‍💻 \\\u0022, " * 3_000).rstrip(", ")}
        correction_json, sources = (
            persistent_profile_creation._prepare_user_confirmed_correction_sources(
                updates,
                REQUEST_AT,
            )
        )
        self.assertGreater(len(correction_json.encode("utf-8")), 32_768)
        self.assertGreater(len(sources), 1)
        self.assertEqual(
            self._reassemble_partitioned_sources(sources),
            correction_json,
        )
        self.assertEqual(json.loads(correction_json)["updates"], updates)

    def test_correction_source_partition_fails_closed_beyond_fifteen_rows(self):
        updates = {
            "payload": "x" * (persistent_profiles_domain.MAX_SOURCE_BYTES * 16)
        }
        with self.assertRaises(PersistentProfileDomainError) as raised:
            persistent_profile_creation._prepare_user_confirmed_correction_sources(
                updates,
                REQUEST_AT,
            )
        self.assertEqual(raised.exception.reason_code, "content_rejected")

    def test_source_bundle_partitioning_is_explicit_and_small_bytes_are_frozen(self):
        reviewed, updates = self._reviewed_profile()
        ordinary = persistent_profile_creation.prepare_reviewed_profile_source_bundle(
            reviewed,
            RAW_ABOUT_YOU,
            updates,
            REQUEST_AT,
            require_correction=True,
        )
        opted_in = persistent_profile_creation.prepare_reviewed_profile_source_bundle(
            reviewed,
            RAW_ABOUT_YOU,
            updates,
            REQUEST_AT,
            require_correction=True,
            partition_correction_sources=True,
        )
        self.assertEqual(
            tuple(source.content_bytes for source in opted_in.sources),
            tuple(source.content_bytes for source in ordinary.sources),
        )
        self.assertEqual(
            persistent_profiles_domain.source_bundle_hash(opted_in.sources),
            persistent_profiles_domain.source_bundle_hash(ordinary.sources),
        )

        oversized_updates = {
            "payload": "x" * persistent_profiles_domain.MAX_SOURCE_BYTES
        }
        with self.assertRaises(PersistentProfileDomainError) as rejected:
            persistent_profile_creation.prepare_reviewed_profile_source_bundle(
                reviewed,
                RAW_ABOUT_YOU,
                oversized_updates,
                REQUEST_AT,
                require_correction=True,
            )
        self.assertEqual(rejected.exception.reason_code, "content_rejected")

        partitioned = (
            persistent_profile_creation.prepare_reviewed_profile_source_bundle(
                reviewed,
                RAW_ABOUT_YOU,
                oversized_updates,
                REQUEST_AT,
                require_correction=True,
                partition_correction_sources=True,
            )
        )
        self.assertGreater(len(partitioned.sources), 2)
        self.assertLessEqual(len(partitioned.sources), 16)
        self.assertEqual(partitioned.sources[0].content, RAW_ABOUT_YOU)
        self.assertEqual(
            self._reassemble_partitioned_sources(partitioned.sources[1:]),
            partitioned.correction_json,
        )


class PersistentProfileCreationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wahojobs-b24d-")
        self.path = Path(self.temp.name) / "profiles.sqlite"
        writer = install_browser_authentication_database(self.path)
        self.session = seed_browser_session(writer, suffix="84")
        writer.close()
        self.now = REQUEST_AT
        self.monotonic = 100.0
        self.tokens = _TokenFactory()
        self.integrations = []
        self.integration = self._build_integration()
        self.reviewed_profile, self.updates = self._reviewed_profile()

    def tearDown(self):
        for integration in reversed(self.integrations):
            integration.close()
        self.temp.cleanup()

    def _build_integration(
        self,
        *,
        repository=None,
        write_provider=None,
        vault=None,
        path=None,
        session=None,
    ):
        path = Path(path or self.path)
        session = session or self.session
        authentication = DurableBrowserSessionAuthenticationGateway(
            trusted_environment_namespace=session["environment"],
            clock=lambda: self.now,
        )
        read_authorization = DurablePersistentProfileReadAuthorizationGateway()
        create_authorization = DurablePersistentProfileCreateAuthorizationGateway(
            read_authorization
        )
        artifact_vault = vault or ConfirmedProfileArtifactVault(
            monotonic=lambda: self.monotonic,
            token_factory=self.tokens,
        )
        creation = PersistentProfileCreationService(
            authentication_gateway=authentication,
            authorization_gateway=create_authorization,
            read_connection_provider=_ReadProvider(path),
            write_connection_provider=(
                write_provider or _WriteProvider(path, timeout=0.05)
            ),
            vault=artifact_vault,
            clock=lambda: self.now,
            token_factory=self.tokens,
            repository=repository,
        )
        read_service = PersistentProfileApplicationService(
            durable_authentication_gateway=authentication,
            durable_authorization_gateway=read_authorization,
            connection_provider=_ReadProvider(path),
        )
        integration = PersistentProfileBrowserIntegration(
            read_service,
            creation_service=creation,
            public_origin=PUBLIC_ORIGIN,
        )
        self.integrations.append(integration)
        integration.activate()
        return integration

    def _reviewed_profile(
        self,
        *,
        force_confirmation_sources=False,
        raw_about_you=RAW_ABOUT_YOU,
    ):
        canonical = normalize_identity_free_profile_input(
            raw_about_you,
            "short_paragraph",
        )
        fields = profile_review_form_fields(canonical, "run", "R" * 43)
        form = {name: [value] for name, value in fields.items()}
        updates = profile_review_updates_from_form(
            form,
            profile_review_language_slots(canonical),
        )
        reviewed = apply_identity_free_profile_review(canonical, updates)
        if force_confirmation_sources:
            reviewed_mapping = reviewed.to_mapping()
            for detail in reviewed_mapping["provenance"]["field_sources"].values():
                if detail["source"] == "user_correction":
                    detail["source"] = "user_confirmation"
            reviewed = IdentityFreeCanonicalProfileV1.from_mapping(
                reviewed_mapping
            )
        return reviewed, updates

    def _new_confirmation_submission(self):
        canonical = normalize_identity_free_profile_input(
            RAW_ABOUT_YOU,
            "short_paragraph",
        )
        registry = MatchRunRegistry()
        pending = registry.create(
            owner_profile_id="unused_local_owner",
            raw_input=RAW_ABOUT_YOU,
            input_style="short_paragraph",
            canonical_profile=canonical,
        )
        fields = profile_review_form_fields(
            canonical,
            pending.match_run_id,
            pending.review_token,
        )
        return registry, pending, {
            name: [value] for name, value in fields.items()
        }

    def _confirmation_artifact_references(self):
        vault = self.integration._creation_service._vault
        with vault._lock:
            return tuple(vault._records)

    def _recording_confirmation_sink(self, recovery_modes):
        def sink(**kwargs):
            recovery_modes.append(kwargs["_confirmation_recovery_only"])
            return self.integration.issue_confirmed_artifact(**kwargs)

        return sink

    def _cookie_headers(self, session=None):
        session = session or self.session
        return (
            ("Host", PUBLIC_AUTHORITY),
            (
                "Cookie",
                cookie_header(
                    {
                        "wahojobs_session": session["session_token"],
                        "__Host-wahojobs_session_csrf": session["csrf_secret"],
                    }
                ),
            ),
        )

    def _issue(
        self,
        *,
        integration=None,
        session=None,
        reviewed=None,
        updates=None,
        raw_about_you=RAW_ABOUT_YOU,
    ):
        integration = integration or self.integration
        session = session or self.session
        return integration.issue_confirmed_artifact(
            reviewed_profile=reviewed or self.reviewed_profile,
            raw_about_you=raw_about_you,
            normalized_updates=updates or self.updates,
            profile_confirmed=True,
            authentication_input=self._cookie_headers(session),
        )

    def _post(
        self,
        offer,
        *,
        integration=None,
        session=None,
        artifact=None,
        proof=None,
        target="/account/profile",
        origin=PUBLIC_ORIGIN,
        fetch_site="same-origin",
        content_type="application/x-www-form-urlencoded",
        content_length=None,
        extra_headers=(),
        body=None,
    ):
        integration = integration or self.integration
        session = session or self.session
        artifact = offer.artifact_reference if artifact is None else artifact
        proof = offer.csrf_proof if proof is None else proof
        body = body if body is not None else f"artifact={artifact}&csrf={proof}".encode("ascii")
        headers = list(self._cookie_headers(session))
        if origin is not None:
            headers.append(("Origin", origin))
        if fetch_site is not None:
            headers.append(("Sec-Fetch-Site", fetch_site))
        if content_type is not None:
            headers.append(("Content-Type", content_type))
        if content_length is not False:
            headers.append(
                (
                    "Content-Length",
                    str(len(body)) if content_length is None else content_length,
                )
            )
        headers.extend(extra_headers)
        return integration.handle(
            "POST",
            target,
            tuple(headers),
            io.BytesIO(body),
        )

    def _new_session(self, *, suffix):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            created = accounts.create_session(
                connection,
                user_id=self.session["account_id"],
                idle_ttl=timedelta(hours=2),
                absolute_ttl=timedelta(days=1),
                idempotency_key=f"b24d-session-{suffix}",
                now=self.now,
            )
            return {
                **self.session,
                "session_id": created.session.session_id,
                "session_token": created.session_token,
                "csrf_secret": created.csrf_secret,
            }
        finally:
            connection.close()

    def _new_account_session(self, *, suffix):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            state = seed_authorized_account(connection, suffix=suffix)
            created = accounts.create_session(
                connection,
                user_id=state["account_id"],
                idle_ttl=timedelta(hours=2),
                absolute_ttl=timedelta(days=1),
                idempotency_key=f"b24d-cross-account-session-{suffix}",
                now=self.now,
            )
            return {
                "account_id": state["account_id"],
                "environment": state["environment"],
                "session_id": created.session.session_id,
                "session_token": created.session_token,
                "csrf_secret": created.csrf_secret,
            }
        finally:
            connection.close()

    def test_confirmation_issues_one_immutable_bound_artifact(self):
        canonical = normalize_identity_free_profile_input(
            RAW_ABOUT_YOU,
            "short_paragraph",
        )
        registry = MatchRunRegistry()
        pending = registry.create(
            owner_profile_id="unused_local_owner",
            raw_input=RAW_ABOUT_YOU,
            input_style="short_paragraph",
            canonical_profile=canonical,
        )
        fields = profile_review_form_fields(
            canonical,
            pending.match_run_id,
            pending.review_token,
        )
        result = create_match_run(
            {name: [value] for name, value in fields.items()},
            registry,
            confirmed_profile_artifact_sink=self.integration.issue_confirmed_artifact,
            authentication_input=self._cookie_headers(),
        )
        self.assertIs(type(result), ConfirmedProfileCreation)
        self.assertTrue(result.match_run.profile_confirmed)
        self.assertIsNone(result.match_run.recommendation_context)
        self.assertRegex(result.artifact_offer.artifact_reference, r"^[A-Za-z0-9_-]{43}$")
        self.assertRegex(result.artifact_offer.csrf_proof, r"^[A-Za-z0-9_-]{43}$")
        self.assertNotIn(RAW_ABOUT_YOU, repr(result))
        with self.assertRaises((AttributeError, TypeError)):
            result.artifact_offer.artifact_reference = "x" * 43
        with self.assertRaises(ConfirmedProfileArtifactUnavailable):
            self.integration.issue_confirmed_artifact(
                reviewed_profile=self.reviewed_profile,
                raw_about_you=RAW_ABOUT_YOU,
                normalized_updates=self.updates,
                profile_confirmed=False,
                authentication_input=self._cookie_headers(),
            )
        with self.assertRaises((AttributeError, TypeError)):
            result.match_run.canonical_profile.canonical_bytes = b"{}"
        self.assertEqual(self._post(result.artifact_offer).status, 303)
        stored = self.integration.handle(
            "GET",
            "/account/profile",
            self._cookie_headers(),
        )
        self.assertIn(EXPECTED_DISPLAY_NAME.encode("utf-8"), stored.body)
        self.assertEqual(_profile_counts(self.path), (1, 1, 2))

    def test_identity_free_projection_and_prepare_id_authority_are_enforced(self):
        for projection_type in (
            IdentityFreeCanonicalProfileV1,
            IdentityFreeCanonicalProfileV2,
        ):
            for arguments in ((), (b"{}",), (object(), b"{}")):
                with self.subTest(
                    projection=projection_type.__name__,
                    arity=len(arguments),
                ):
                    with self.assertRaises(
                        (TypeError, PersistentProfileDomainError)
                    ):
                        projection_type(*arguments)

        identity_bearing = self.reviewed_profile.to_mapping()
        identity_bearing["identity"]["profile_id"] = (
            "reviewer_selected_preview_identity"
        )
        with self.assertRaises(PersistentProfileDomainError):
            IdentityFreeCanonicalProfileV1.from_mapping(identity_bearing)
        recursively_identity_bearing = self.reviewed_profile.to_mapping()
        recursively_identity_bearing["matcher_compatible_profile"][
            "profile_id"
        ] = "nested_preview_identity"
        with self.assertRaises(PersistentProfileDomainError):
            IdentityFreeCanonicalProfileV1.from_mapping(
                recursively_identity_bearing
            )
        self.assertNotIn(
            b"profile_id",
            self.reviewed_profile.canonical_bytes,
        )
        self.assertEqual(
            profile_draft_fingerprint(self.reviewed_profile),
            hashlib.sha256(
                self.reviewed_profile.canonical_bytes
            ).hexdigest(),
        )

        offer = self._issue()
        command = self.integration._creation_service._vault._records[
            offer.artifact_reference
        ].snapshot.command
        semantic_projection = IdentityFreeCanonicalProfileV2.from_canonical_bytes(
            command._structured_profile_json
        )
        self.assertNotIn(b"profile_id", semantic_projection.canonical_bytes)

        adversarial_vault = ConfirmedProfileArtifactVault(
            monotonic=lambda: self.monotonic,
            token_factory=_TokenFactory(),
        )
        adversarial = self._build_integration(vault=adversarial_vault)
        profile_id = mock.Mock(
            wraps=persistent_profiles_domain.generate_profile_id
        )
        revision_id = mock.Mock(
            wraps=persistent_profiles_domain.generate_revision_id
        )
        source_id = mock.Mock(
            wraps=persistent_profiles_domain.generate_source_id
        )
        real_convert = persistent_profile_creation.convert_v1_to_v2

        def independently_select_id(*args, **kwargs):
            converted = real_convert(*args, **kwargs)
            converted["identity"]["profile_id"] = "prf_" + "9" * 31 + "8"
            return converted

        before = _logical_snapshot(self.path)
        with (
            mock.patch.object(
                persistent_profiles_domain,
                "generate_profile_id",
                profile_id,
            ),
            mock.patch.object(
                persistent_profiles_domain,
                "generate_revision_id",
                revision_id,
            ),
            mock.patch.object(
                persistent_profiles_domain,
                "generate_source_id",
                source_id,
            ),
            mock.patch.object(
                persistent_profile_creation,
                "convert_v1_to_v2",
                side_effect=independently_select_id,
            ) as convert,
        ):
            with self.assertRaises(ConfirmedProfileArtifactUnavailable):
                self._issue(integration=adversarial)
        self.assertEqual(
            (profile_id.call_count, revision_id.call_count, source_id.call_count),
            (1, 1, 2),
        )
        self.assertEqual(convert.call_count, 1)
        self.assertEqual(adversarial_vault._records, {})
        self.assertEqual(_logical_snapshot(self.path), before)

    def test_confirmation_ordinary_failure_distinguishes_publication_boundary(self):
        registry, pending, form = self._new_confirmation_submission()
        baseline = set(self._confirmation_artifact_references())
        recovery_modes = []
        sink = self._recording_confirmation_sink(recovery_modes)
        with mock.patch.object(
            PersistentProfileCreationService,
            "_prepare_snapshot",
            side_effect=RuntimeError("definite-before-publication"),
        ):
            with self.assertRaises(ActionError) as raised:
                create_match_run(
                    form,
                    registry,
                    confirmed_profile_artifact_sink=sink,
                    authentication_input=self._cookie_headers(),
                )
        self.assertEqual(raised.exception.status, 503)
        self.assertIs(registry.confirmation_draft(pending.match_run_id), pending)
        with registry._condition:
            self.assertNotIn(pending.match_run_id, registry._confirmations)
        self.assertEqual(
            set(self._confirmation_artifact_references()),
            baseline,
        )
        completed = create_match_run(
            form,
            registry,
            confirmed_profile_artifact_sink=sink,
            authentication_input=self._cookie_headers(),
        )
        self.assertEqual(recovery_modes, [False, False])
        created = set(self._confirmation_artifact_references()) - baseline
        self.assertEqual(len(created), 1)
        self.assertEqual(
            completed.artifact_offer.artifact_reference,
            next(iter(created)),
        )

        registry, pending, form = self._new_confirmation_submission()
        baseline = set(self._confirmation_artifact_references())
        recovery_modes = []
        sink = self._recording_confirmation_sink(recovery_modes)
        with mock.patch.object(
            persistent_profile_creation,
            "profile_create_csrf_proof",
            side_effect=RuntimeError("uncertain-after-publication"),
        ):
            with self.assertRaises(ActionError) as raised:
                create_match_run(
                    form,
                    registry,
                    confirmed_profile_artifact_sink=sink,
                    authentication_input=self._cookie_headers(),
                )
        self.assertEqual(raised.exception.status, 503)
        published = set(self._confirmation_artifact_references()) - baseline
        self.assertEqual(len(published), 1)
        with registry._condition:
            state = registry._confirmations[pending.match_run_id]
            self.assertEqual(state.state, "maybe_issued")
            self.assertIsNone(state.completed_result)
        recovered = create_match_run(
            form,
            registry,
            confirmed_profile_artifact_sink=sink,
            authentication_input=self._cookie_headers(),
        )
        self.assertEqual(recovery_modes, [False, True])
        self.assertEqual(
            recovered.artifact_offer.artifact_reference,
            next(iter(published)),
        )
        self.assertEqual(
            set(self._confirmation_artifact_references()),
            baseline | published,
        )
        self.assertIs(
            create_match_run(
                form,
                registry,
                confirmed_profile_artifact_sink=sink,
                authentication_input=self._cookie_headers(),
                completed_profile_confirmation_authenticator=(
                    self.integration.authenticate_completed_profile_replay
                ),
            ),
            recovered,
        )
        self.assertEqual(recovery_modes, [False, True])

    def test_confirmation_invalid_offer_is_never_completed(self):
        registry, pending, form = self._new_confirmation_submission()
        baseline = set(self._confirmation_artifact_references())
        invalid_modes = []

        def invalid_sink(**kwargs):
            invalid_modes.append(kwargs["_confirmation_recovery_only"])
            return object()

        with self.assertRaises(ActionError) as raised:
            create_match_run(
                form,
                registry,
                confirmed_profile_artifact_sink=invalid_sink,
                authentication_input=self._cookie_headers(),
            )
        self.assertEqual(raised.exception.status, 503)
        self.assertEqual(invalid_modes, [False])
        with registry._condition:
            self.assertNotIn(pending.match_run_id, registry._confirmations)
        self.assertEqual(
            set(self._confirmation_artifact_references()),
            baseline,
        )
        self.assertIs(registry.confirmation_draft(pending.match_run_id), pending)

        recovery_modes = []
        sink = self._recording_confirmation_sink(recovery_modes)
        completed = create_match_run(
            form,
            registry,
            confirmed_profile_artifact_sink=sink,
            authentication_input=self._cookie_headers(),
        )
        self.assertEqual(recovery_modes, [False])
        created = set(self._confirmation_artifact_references()) - baseline
        self.assertEqual(len(created), 1)
        self.assertEqual(
            completed.artifact_offer.artifact_reference,
            next(iter(created)),
        )
        self.assertIs(
            create_match_run(
                form,
                registry,
                confirmed_profile_artifact_sink=sink,
                authentication_input=self._cookie_headers(),
                completed_profile_confirmation_authenticator=(
                    self.integration.authenticate_completed_profile_replay
                ),
            ),
            completed,
        )
        self.assertEqual(recovery_modes, [False])

    def test_confirmation_preserves_exact_controls_before_and_after_may_exist(self):
        for boundary in ("before", "after"):
            for control_type in (KeyboardInterrupt, SystemExit, GeneratorExit):
                with self.subTest(
                    boundary=boundary,
                    control=control_type.__name__,
                ):
                    registry, pending, form = self._new_confirmation_submission()
                    baseline = set(self._confirmation_artifact_references())
                    recovery_modes = []
                    sink = self._recording_confirmation_sink(recovery_modes)
                    control = control_type(
                        f"confirmation-{boundary}-{control_type.__name__}"
                    )
                    patcher = (
                        mock.patch.object(
                            PersistentProfileCreationService,
                            "_prepare_snapshot",
                            side_effect=control,
                        )
                        if boundary == "before"
                        else mock.patch.object(
                            persistent_profile_creation,
                            "profile_create_csrf_proof",
                            side_effect=control,
                        )
                    )
                    with patcher:
                        with self.assertRaises(control_type) as raised:
                            create_match_run(
                                form,
                                registry,
                                confirmed_profile_artifact_sink=sink,
                                authentication_input=self._cookie_headers(),
                            )
                    self.assertIs(raised.exception, control)
                    self.assertEqual(recovery_modes, [False])
                    self.assertIs(
                        registry.confirmation_draft(pending.match_run_id),
                        pending,
                    )
                    after_failure = set(
                        self._confirmation_artifact_references()
                    )
                    with registry._condition:
                        state = registry._confirmations.get(
                            pending.match_run_id
                        )
                        if boundary == "before":
                            self.assertIsNone(state)
                            self.assertEqual(after_failure, baseline)
                        else:
                            self.assertIsNotNone(state)
                            self.assertEqual(state.state, "maybe_issued")
                            self.assertIsNone(state.completed_result)
                            self.assertEqual(len(after_failure - baseline), 1)

                    completed = create_match_run(
                        form,
                        registry,
                        confirmed_profile_artifact_sink=sink,
                        authentication_input=self._cookie_headers(),
                    )
                    expected_modes = (
                        [False, False]
                        if boundary == "before"
                        else [False, True]
                    )
                    self.assertEqual(recovery_modes, expected_modes)
                    after_retry = set(
                        self._confirmation_artifact_references()
                    )
                    created = after_retry - baseline
                    self.assertEqual(len(created), 1)
                    self.assertEqual(
                        completed.artifact_offer.artifact_reference,
                        next(iter(created)),
                    )
                    self.assertIs(
                        create_match_run(
                            form,
                            registry,
                            confirmed_profile_artifact_sink=sink,
                            completed_profile_confirmation_authenticator=(
                                self.integration.authenticate_completed_profile_replay
                            ),
                            authentication_input=self._cookie_headers(),
                        ),
                        completed,
                    )
                    self.assertEqual(recovery_modes, expected_modes)
                    self.assertEqual(
                        set(self._confirmation_artifact_references()),
                        after_retry,
                    )

    def test_confirmation_response_delivery_retry_returns_cached_offer(self):
        registry, _pending, form = self._new_confirmation_submission()
        baseline = set(self._confirmation_artifact_references())
        recovery_modes = []
        sink = self._recording_confirmation_sink(recovery_modes)
        authenticator = mock.Mock(
            wraps=self.integration.authenticate_completed_profile_replay
        )
        completed = create_match_run(
            form,
            registry,
            confirmed_profile_artifact_sink=sink,
            completed_profile_confirmation_authenticator=authenticator,
            authentication_input=self._cookie_headers(),
        )
        self.assertIs(type(completed), ConfirmedProfileCreation)
        self.assertEqual(recovery_modes, [False])
        created = set(self._confirmation_artifact_references()) - baseline
        self.assertEqual(len(created), 1)

        delivery = mock.Mock()
        delivery_failure = OSError(
            "browser disconnected during response write"
        )
        delivery.wfile.write.side_effect = delivery_failure
        handler_type = local_product_app.make_handler()
        with self.assertRaises(OSError) as raised:
            handler_type.write_confirmed_profile_creation(
                delivery,
                completed.artifact_offer,
            )
        self.assertIs(raised.exception, delivery_failure)
        delivery.send_header.assert_any_call(
            "Referrer-Policy",
            "same-origin",
        )

        retried = create_match_run(
            form,
            registry,
            confirmed_profile_artifact_sink=sink,
            completed_profile_confirmation_authenticator=authenticator,
            authentication_input=self._cookie_headers(),
        )
        self.assertIs(retried, completed)
        self.assertIs(retried.artifact_offer, completed.artifact_offer)
        self.assertEqual(recovery_modes, [False])
        self.assertEqual(authenticator.call_count, 1)
        self.assertEqual(
            authenticator.call_args.kwargs["authority_binding"],
            completed._authority_binding,
        )
        self.assertEqual(
            set(self._confirmation_artifact_references()),
            baseline | created,
        )

    def test_completed_replay_rejects_stale_or_changed_authority_without_reissuance(self):
        def complete_for(session):
            registry, _pending, form = self._new_confirmation_submission()
            recovery_modes = []
            sink = self._recording_confirmation_sink(recovery_modes)
            completed = create_match_run(
                form,
                registry,
                confirmed_profile_artifact_sink=sink,
                completed_profile_confirmation_authenticator=(
                    self.integration.authenticate_completed_profile_replay
                ),
                authentication_input=self._cookie_headers(session),
            )
            self.assertIs(type(completed), ConfirmedProfileCreation)
            return registry, form, sink, recovery_modes

        def rejected(registry, form, sink, recovery_modes, headers):
            with self.assertRaises((ActionError, ValueError)):
                create_match_run(
                    form,
                    registry,
                    confirmed_profile_artifact_sink=sink,
                    completed_profile_confirmation_authenticator=(
                        self.integration.authenticate_completed_profile_replay
                    ),
                    authentication_input=headers,
                )
            self.assertEqual(recovery_modes, [False])

        registry, form, sink, modes = complete_for(self.session)
        changed_presentations = {
            "cross-session": self._cookie_headers(self._new_session(suffix="replay-cross")),
            "cross-account": self._cookie_headers(
                self._new_account_session(suffix="731")
            ),
            "invalid-host": (
                ("Host", "evil.example"),
                self._cookie_headers()[1],
            ),
            "invalid-proxy": self._cookie_headers()
            + (("Forwarded", "host=evil.example"),),
            "invalid-csrf": self._cookie_headers(
                {**self.session, "csrf_secret": "Z" * 43}
            ),
        }
        for label, headers in changed_presentations.items():
            with self.subTest(label=label):
                rejected(registry, form, sink, modes, headers)

        invalidated_sessions = (
            ("rotated", "session_rotated"),
            ("revoked", "security_reset"),
            ("logged-out", "user_logout"),
        )
        for label, reason in invalidated_sessions:
            with self.subTest(label=label):
                session = self._new_session(suffix=f"replay-{label}")
                registry, form, sink, modes = complete_for(session)
                connection = sqlite3.connect(self.path)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                try:
                    if label == "rotated":
                        accounts.rotate_session(
                            connection,
                            session_token=session["session_token"],
                            expected_session_version=1,
                            idle_ttl=timedelta(hours=1),
                            idempotency_key="b24d-completed-replay-rotation",
                            now=self.now,
                        )
                    else:
                        accounts.revoke_current_session(
                            connection,
                            session_token=session["session_token"],
                            expected_session_version=1,
                            reason=reason,
                            now=self.now,
                        )
                finally:
                    connection.close()
                rejected(
                    registry,
                    form,
                    sink,
                    modes,
                    self._cookie_headers(session),
                )

        expired = self._new_session(suffix="replay-expired")
        registry, form, sink, modes = complete_for(expired)
        self.now += timedelta(hours=3)
        rejected(registry, form, sink, modes, self._cookie_headers(expired))
        self.assertEqual(_profile_counts(self.path), (0, 0, 0))

    def test_concurrent_identical_confirmations_converge_and_reject_changes(self):
        registry, _pending, form = self._new_confirmation_submission()
        baseline = set(self._confirmation_artifact_references())
        owner_entered = threading.Event()
        release_owner = threading.Event()
        waiter_entered = threading.Event()
        sink_lock = threading.Lock()
        sink_modes = []
        outcomes = [None, None]
        failures = [None, None]

        def sink(**kwargs):
            with sink_lock:
                sink_modes.append(kwargs["_confirmation_recovery_only"])
            owner_entered.set()
            if not release_owner.wait(2.0):
                raise RuntimeError("confirmation owner was not released")
            return self.integration.issue_confirmed_artifact(**kwargs)

        def submit(index):
            try:
                outcomes[index] = create_match_run(
                    form,
                    registry,
                    confirmed_profile_artifact_sink=sink,
                    completed_profile_confirmation_authenticator=(
                        self.integration.authenticate_completed_profile_replay
                    ),
                    authentication_input=self._cookie_headers(),
                )
            except BaseException as exc:
                failures[index] = exc

        original_wait = registry._condition.wait
        observed_timeouts = []

        def observed_wait(*args, **kwargs):
            timeout = kwargs.get("timeout")
            if timeout is None and args:
                timeout = args[0]
            observed_timeouts.append(timeout)
            waiter_entered.set()
            return original_wait(*args, **kwargs)

        registry._condition.wait = observed_wait
        owner = threading.Thread(
            target=submit,
            args=(0,),
            name="b24d-confirmation-owner",
        )
        waiter = threading.Thread(
            target=submit,
            args=(1,),
            name="b24d-confirmation-waiter",
        )
        started = []
        try:
            owner.start()
            started.append(owner)
            self.assertTrue(owner_entered.wait(1.0))
            waiter.start()
            started.append(waiter)
            self.assertTrue(waiter_entered.wait(1.0))
        finally:
            release_owner.set()
            for thread in started:
                thread.join(3.0)
            registry._condition.wait = original_wait

        for thread in started:
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [None, None])
        self.assertIs(type(outcomes[0]), ConfirmedProfileCreation)
        self.assertIs(outcomes[0], outcomes[1])
        self.assertIs(
            outcomes[0].artifact_offer,
            outcomes[1].artifact_offer,
        )
        self.assertEqual(sink_modes, [False])
        self.assertTrue(observed_timeouts)
        self.assertTrue(all(timeout is not None for timeout in observed_timeouts))
        self.assertTrue(
            all(
                0
                < timeout
                <= local_product_app.PROFILE_CONFIRMATION_OWNER_WAIT_SECONDS
                for timeout in observed_timeouts
            )
        )
        created = set(self._confirmation_artifact_references()) - baseline
        self.assertEqual(len(created), 1)
        self.assertEqual(
            outcomes[0].artifact_offer.artifact_reference,
            next(iter(created)),
        )

        changed = {name: list(values) for name, values in form.items()}
        changed["city"] = ["changed-review-city"]
        with self.assertRaises(ActionError) as raised:
            create_match_run(
                changed,
                registry,
                confirmed_profile_artifact_sink=sink,
                completed_profile_confirmation_authenticator=(
                    self.integration.authenticate_completed_profile_replay
                ),
                authentication_input=self._cookie_headers(),
            )
        self.assertEqual(raised.exception.status, 403)

        stale = {name: list(values) for name, values in form.items()}
        fingerprint = stale["profile_draft_fingerprint"][0]
        replacement = "0" if fingerprint[0] != "0" else "1"
        stale["profile_draft_fingerprint"] = [
            replacement + fingerprint[1:]
        ]
        with self.assertRaises(ActionError) as raised:
            create_match_run(
                stale,
                registry,
                confirmed_profile_artifact_sink=sink,
                completed_profile_confirmation_authenticator=(
                    self.integration.authenticate_completed_profile_replay
                ),
                authentication_input=self._cookie_headers(),
            )
        self.assertEqual(raised.exception.status, 403)
        self.assertEqual(sink_modes, [False])
        self.assertEqual(
            set(self._confirmation_artifact_references()),
            baseline | created,
        )
        self.assertIs(
            create_match_run(
                form,
                registry,
                confirmed_profile_artifact_sink=sink,
                completed_profile_confirmation_authenticator=(
                    self.integration.authenticate_completed_profile_replay
                ),
                authentication_input=self._cookie_headers(),
            ),
            outcomes[0],
        )
        self.assertEqual(sink_modes, [False])
        with self.assertRaises((AttributeError, TypeError)):
            outcomes[0].artifact_offer.artifact_reference = "A" * 43

    def test_uncertain_confirmation_auth_failures_preserve_recovery_identity(self):
        for control_type in (None, KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(
                failure=(
                    "ordinary"
                    if control_type is None
                    else control_type.__name__
                )
            ):
                registry, pending, form = self._new_confirmation_submission()
                baseline = set(self._confirmation_artifact_references())
                recovery_modes = []
                sink = self._recording_confirmation_sink(recovery_modes)
                with mock.patch.object(
                    persistent_profile_creation,
                    "profile_create_csrf_proof",
                    side_effect=RuntimeError("uncertain-publication"),
                ):
                    with self.assertRaises(ActionError):
                        create_match_run(
                            form,
                            registry,
                            confirmed_profile_artifact_sink=sink,
                            authentication_input=self._cookie_headers(),
                        )
                published = (
                    set(self._confirmation_artifact_references()) - baseline
                )
                self.assertEqual(len(published), 1)

                if control_type is None:
                    authority_failure = mock.patch.object(
                        PersistentProfileCreationService,
                        "_request_authority",
                        return_value=("unavailable", None),
                    )
                    expected_exception = ActionError
                    injected = None
                else:
                    injected = control_type(
                        f"recovery-auth-{control_type.__name__}"
                    )
                    authority_failure = mock.patch.object(
                        PersistentProfileCreationService,
                        "_request_authority",
                        side_effect=injected,
                    )
                    expected_exception = control_type
                with authority_failure:
                    with self.assertRaises(expected_exception) as raised:
                        create_match_run(
                            form,
                            registry,
                            confirmed_profile_artifact_sink=sink,
                            authentication_input=self._cookie_headers(),
                        )
                if injected is not None:
                    self.assertIs(raised.exception, injected)
                else:
                    self.assertEqual(raised.exception.status, 503)
                with registry._condition:
                    state = registry._confirmations[pending.match_run_id]
                    self.assertEqual(state.state, "maybe_issued")
                    self.assertIsNone(state.completed_result)

                changed = {
                    name: list(values) for name, values in form.items()
                }
                changed["city"] = ["must-not-create-another-artifact"]
                with self.assertRaises(ActionError) as changed_rejection:
                    create_match_run(
                        changed,
                        registry,
                        confirmed_profile_artifact_sink=sink,
                        authentication_input=self._cookie_headers(),
                    )
                self.assertEqual(changed_rejection.exception.status, 403)

                completed = create_match_run(
                    form,
                    registry,
                    confirmed_profile_artifact_sink=sink,
                    authentication_input=self._cookie_headers(),
                )
                self.assertEqual(
                    completed.artifact_offer.artifact_reference,
                    next(iter(published)),
                )
                self.assertEqual(recovery_modes, [False, True, True])
                self.assertEqual(
                    set(self._confirmation_artifact_references()),
                    baseline | published,
                )

    def test_completed_confirmation_is_pinned_until_exact_retry(self):
        canonical = normalize_identity_free_profile_input(
            RAW_ABOUT_YOU,
            "short_paragraph",
        )
        registry = MatchRunRegistry(max_size=1)
        pending = registry.create(
            owner_profile_id="unused_local_owner",
            raw_input=RAW_ABOUT_YOU,
            input_style="short_paragraph",
            canonical_profile=canonical,
        )
        fields = profile_review_form_fields(
            canonical,
            pending.match_run_id,
            pending.review_token,
        )
        form = {name: [value] for name, value in fields.items()}
        recovery_modes = []
        sink = self._recording_confirmation_sink(recovery_modes)
        baseline = set(self._confirmation_artifact_references())
        completed = create_match_run(
            form,
            registry,
            confirmed_profile_artifact_sink=sink,
            authentication_input=self._cookie_headers(),
        )
        created = set(self._confirmation_artifact_references()) - baseline
        self.assertEqual(len(created), 1)
        with self.assertRaises(ActionError) as capacity:
            registry.create(
                owner_profile_id="unrelated",
                raw_input="unrelated exact draft",
                input_style="short_paragraph",
                canonical_profile=normalize_identity_free_profile_input(
                    "Unrelated accounting reviewer.",
                    "short_paragraph",
                ),
            )
        self.assertEqual(capacity.exception.status, 503)
        self.assertEqual(len(registry), 1)
        self.assertIs(
            create_match_run(
                form,
                registry,
                confirmed_profile_artifact_sink=sink,
                completed_profile_confirmation_authenticator=(
                    self.integration.authenticate_completed_profile_replay
                ),
                authentication_input=self._cookie_headers(),
            ),
            completed,
        )
        self.assertEqual(recovery_modes, [False])
        self.assertEqual(
            set(self._confirmation_artifact_references()),
            baseline | created,
        )

    def test_completed_confirmation_retention_expires_with_artifact_lifetime(self):
        self.assertEqual(
            local_product_app.PROFILE_CONFIRMATION_RETENTION_SECONDS,
            PROFILE_CREATE_ARTIFACT_LIFETIME_SECONDS,
        )
        clock = [100.0]
        canonical = normalize_identity_free_profile_input(
            RAW_ABOUT_YOU,
            "short_paragraph",
        )
        registry = MatchRunRegistry(
            max_size=1,
            _retention_clock=lambda: clock[0],
        )
        pending = registry.create(
            owner_profile_id="unused_local_owner",
            raw_input=RAW_ABOUT_YOU,
            input_style="short_paragraph",
            canonical_profile=canonical,
        )
        fields = profile_review_form_fields(
            canonical,
            pending.match_run_id,
            pending.review_token,
        )
        form = {name: [value] for name, value in fields.items()}
        recovery_modes = []
        sink = self._recording_confirmation_sink(recovery_modes)
        baseline = set(self._confirmation_artifact_references())
        completed = create_match_run(
            form,
            registry,
            confirmed_profile_artifact_sink=sink,
            authentication_input=self._cookie_headers(),
        )
        created = set(self._confirmation_artifact_references()) - baseline
        self.assertEqual(len(created), 1)

        clock[0] += PROFILE_CREATE_ARTIFACT_LIFETIME_SECONDS - 0.01
        with self.assertRaises(ActionError) as capacity:
            registry.create(
                owner_profile_id="blocked-before-expiry",
                raw_input="Unrelated accounting reviewer.",
                input_style="short_paragraph",
                canonical_profile=normalize_identity_free_profile_input(
                    "Unrelated accounting reviewer.",
                    "short_paragraph",
                ),
            )
        self.assertEqual(capacity.exception.status, 503)
        self.assertIs(
            create_match_run(
                form,
                registry,
                confirmed_profile_artifact_sink=sink,
                completed_profile_confirmation_authenticator=(
                    self.integration.authenticate_completed_profile_replay
                ),
                authentication_input=self._cookie_headers(),
            ),
            completed,
        )

        clock[0] += 0.02
        replacement = registry.create(
            owner_profile_id="allowed-after-expiry",
            raw_input="Unrelated accounting reviewer.",
            input_style="short_paragraph",
            canonical_profile=normalize_identity_free_profile_input(
                "Unrelated accounting reviewer.",
                "short_paragraph",
            ),
        )
        self.assertEqual(
            registry.get(replacement.match_run_id).match_run_id,
            replacement.match_run_id,
        )
        self.assertIsNone(registry.get(pending.match_run_id))
        with self.assertRaises(ActionError) as expired_retry:
            create_match_run(
                form,
                registry,
                confirmed_profile_artifact_sink=sink,
                authentication_input=self._cookie_headers(),
            )
        self.assertEqual(expired_retry.exception.status, 410)
        self.assertEqual(recovery_modes, [False])
        self.assertEqual(
            set(self._confirmation_artifact_references()),
            baseline | created,
        )

    def test_confirmation_auth_binding_is_strict_but_cookie_presentation_independent(self):
        registry, _pending, form = self._new_confirmation_submission()
        session_token = self.session["session_token"]
        csrf_secret = self.session["csrf_secret"]
        headers_a = (
            ("Host", PUBLIC_AUTHORITY),
            (
                "Cookie",
                "theme=dark; "
                f"wahojobs_session={session_token}; "
                f"__Host-wahojobs_session_csrf={csrf_secret}",
            ),
        )
        headers_b = (
            (
                "Cookie",
                f"__Host-wahojobs_session_csrf={csrf_secret}; "
                "theme=light; "
                f"wahojobs_session={session_token}",
            ),
            ("Host", PUBLIC_AUTHORITY),
        )
        recovery_modes = []
        sink = self._recording_confirmation_sink(recovery_modes)
        baseline = set(self._confirmation_artifact_references())
        with mock.patch.object(
            persistent_profile_creation,
            "profile_create_csrf_proof",
            side_effect=RuntimeError("uncertain-publication"),
        ):
            with self.assertRaises(ActionError):
                create_match_run(
                    form,
                    registry,
                    confirmed_profile_artifact_sink=sink,
                    authentication_input=headers_a,
                )
        published = set(self._confirmation_artifact_references()) - baseline
        self.assertEqual(len(published), 1)

        completed = create_match_run(
            form,
            registry,
            confirmed_profile_artifact_sink=sink,
            authentication_input=headers_b,
        )
        self.assertEqual(
            completed.artifact_offer.artifact_reference,
            next(iter(published)),
        )
        self.assertIs(
            create_match_run(
                form,
                registry,
                confirmed_profile_artifact_sink=sink,
                completed_profile_confirmation_authenticator=(
                    self.integration.authenticate_completed_profile_replay
                ),
                authentication_input=headers_a,
            ),
            completed,
        )
        self.assertEqual(recovery_modes, [False, True])

        valid_cookie = self._cookie_headers()[1][1]
        malformed_headers = (
            (
                ("Host", PUBLIC_AUTHORITY),
                ("Host", PUBLIC_AUTHORITY),
                ("Cookie", valid_cookie),
            ),
            (
                ("Host", PUBLIC_AUTHORITY),
                ("Cookie", valid_cookie),
                ("Cookie", "theme=dark"),
            ),
            (
                ("Host", PUBLIC_AUTHORITY),
                (
                    "Cookie",
                    f"wahojobs_session={session_token}; "
                    f"wahojobs_session={session_token}; "
                    f"__Host-wahojobs_session_csrf={csrf_secret}",
                ),
            ),
            (
                ("Host", PUBLIC_AUTHORITY),
                ("Cookie", valid_cookie + "; malformed"),
            ),
            (
                ("Host", PUBLIC_AUTHORITY),
                (
                    "Cookie",
                    "theme=dark;  "
                    f"wahojobs_session={session_token}; "
                    f"__Host-wahojobs_session_csrf={csrf_secret}",
                ),
            ),
            (
                ("Host", PUBLIC_AUTHORITY),
                ("Cookie", "theme=; " + valid_cookie),
            ),
            (
                ("Host", PUBLIC_AUTHORITY),
                (
                    "Cookie",
                    "wahojobs_session=short; "
                    f"__Host-wahojobs_session_csrf={csrf_secret}",
                ),
            ),
            (
                ("Host", PUBLIC_AUTHORITY),
                ("Cookie", valid_cookie + "; theme=bad\x00value"),
            ),
            (
                ("Host", PUBLIC_AUTHORITY),
                ("Cookie", valid_cookie + ";\ttheme=bad"),
            ),
            (
                ("Host", PUBLIC_AUTHORITY),
                ("Cookie", valid_cookie + "; theme=São"),
            ),
            (
                ("Host", PUBLIC_AUTHORITY),
                ("Cookie", valid_cookie),
                ("X-Forwarded-Host", "attacker.invalid"),
            ),
        )
        for headers in malformed_headers:
            with self.subTest(headers=headers):
                with self.assertRaises(ValueError):
                    create_match_run(
                        form,
                        registry,
                        confirmed_profile_artifact_sink=sink,
                        authentication_input=headers,
                    )
        self.assertEqual(recovery_modes, [False, True])
        self.assertEqual(
            set(self._confirmation_artifact_references()),
            baseline | published,
        )

    def test_confirmation_witness_construction_controls_always_settle_claim(self):
        for recovery_only in (False, True):
            for control_type in (KeyboardInterrupt, SystemExit, GeneratorExit):
                with self.subTest(
                    recovery_only=recovery_only,
                    control=control_type.__name__,
                ):
                    registry, pending, form = self._new_confirmation_submission()
                    baseline = set(self._confirmation_artifact_references())
                    recovery_modes = []
                    sink = self._recording_confirmation_sink(recovery_modes)
                    if recovery_only:
                        with mock.patch.object(
                            persistent_profile_creation,
                            "profile_create_csrf_proof",
                            side_effect=RuntimeError("uncertain-publication"),
                        ):
                            with self.assertRaises(ActionError):
                                create_match_run(
                                    form,
                                    registry,
                                    confirmed_profile_artifact_sink=sink,
                                    authentication_input=self._cookie_headers(),
                                )
                        self.assertEqual(
                            len(
                                set(self._confirmation_artifact_references())
                                - baseline
                            ),
                            1,
                        )

                    injected = control_type(
                        f"witness-construction-{control_type.__name__}"
                    )
                    with mock.patch.object(
                        local_product_app,
                        "_ConfirmationIssuanceWitness",
                        side_effect=injected,
                    ):
                        with self.assertRaises(control_type) as raised:
                            create_match_run(
                                form,
                                registry,
                                confirmed_profile_artifact_sink=sink,
                                authentication_input=self._cookie_headers(),
                            )
                    self.assertIs(raised.exception, injected)
                    with registry._condition:
                        state = registry._confirmations.get(pending.match_run_id)
                        if recovery_only:
                            self.assertIsNotNone(state)
                            self.assertEqual(state.state, "maybe_issued")
                        else:
                            self.assertIsNone(state)

                    completed = create_match_run(
                        form,
                        registry,
                        confirmed_profile_artifact_sink=sink,
                        authentication_input=self._cookie_headers(),
                    )
                    created = (
                        set(self._confirmation_artifact_references()) - baseline
                    )
                    self.assertEqual(len(created), 1)
                    self.assertEqual(
                        completed.artifact_offer.artifact_reference,
                        next(iter(created)),
                    )
                    self.assertEqual(
                        recovery_modes,
                        [False, True] if recovery_only else [False],
                    )

    def test_confirmation_claim_and_completion_controls_are_recoverable(self):
        real_state_type = local_product_app._ProfileConfirmationState

        for phase in ("claim", "completion"):
            for control_type in (KeyboardInterrupt, SystemExit, GeneratorExit):
                with self.subTest(
                    phase=phase,
                    control=control_type.__name__,
                ):
                    registry, pending, form = self._new_confirmation_submission()
                    baseline = set(self._confirmation_artifact_references())
                    recovery_modes = []
                    sink = self._recording_confirmation_sink(recovery_modes)
                    injected = control_type(
                        f"confirmation-{phase}-{control_type.__name__}"
                    )
                    armed = [True]

                    if phase == "claim":
                        class InterruptAfterPublication(dict):
                            def __setitem__(self, key, value):
                                super().__setitem__(key, value)
                                if armed[0]:
                                    armed[0] = False
                                    raise injected

                        registry._confirmations = InterruptAfterPublication()
                        patcher = mock.patch.object(
                            local_product_app,
                            "_ProfileConfirmationState",
                            real_state_type,
                        )
                    else:
                        class InterruptingState(real_state_type):
                            def __setattr__(self, name, value):
                                if (
                                    name == "state"
                                    and value == "completed"
                                    and armed[0]
                                ):
                                    super().__setattr__(name, value)
                                    armed[0] = False
                                    raise injected
                                super().__setattr__(name, value)

                        patcher = mock.patch.object(
                            local_product_app,
                            "_ProfileConfirmationState",
                            InterruptingState,
                        )

                    with patcher:
                        with self.assertRaises(control_type) as raised:
                            create_match_run(
                                form,
                                registry,
                                confirmed_profile_artifact_sink=sink,
                                authentication_input=self._cookie_headers(),
                            )
                    self.assertIs(raised.exception, injected)
                    self.assertFalse(armed[0])
                    completed = create_match_run(
                        form,
                        registry,
                        confirmed_profile_artifact_sink=sink,
                        authentication_input=self._cookie_headers(),
                        completed_profile_confirmation_authenticator=(
                            self.integration.authenticate_completed_profile_replay
                        ),
                    )
                    created = (
                        set(self._confirmation_artifact_references()) - baseline
                    )
                    self.assertEqual(len(created), 1)
                    self.assertEqual(
                        completed.artifact_offer.artifact_reference,
                        next(iter(created)),
                    )
                    self.assertIs(
                        create_match_run(
                            form,
                            registry,
                            confirmed_profile_artifact_sink=sink,
                            authentication_input=self._cookie_headers(),
                            completed_profile_confirmation_authenticator=(
                                self.integration.authenticate_completed_profile_replay
                            ),
                        ),
                        completed,
                    )
                    self.assertEqual(
                        recovery_modes,
                        [False],
                    )

    def test_confirmation_recovery_takeover_controls_remain_uncertain(self):
        real_state_type = local_product_app._ProfileConfirmationState

        for control_type in (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(control=control_type.__name__):
                registry, pending, form = self._new_confirmation_submission()
                baseline = set(self._confirmation_artifact_references())
                recovery_modes = []
                sink = self._recording_confirmation_sink(recovery_modes)
                with mock.patch.object(
                    persistent_profile_creation,
                    "profile_create_csrf_proof",
                    side_effect=RuntimeError("uncertain-before-recovery-takeover"),
                ):
                    with self.assertRaises(ActionError):
                        create_match_run(
                            form,
                            registry,
                            confirmed_profile_artifact_sink=sink,
                            authentication_input=self._cookie_headers(),
                        )
                with registry._condition:
                    original_state = registry._confirmations[
                        pending.match_run_id
                    ]
                    self.assertEqual(original_state.state, "maybe_issued")

                    injected = control_type(
                        f"recovery-takeover-{control_type.__name__}"
                    )
                    armed = [True]

                    class InterruptingRecoveryState(real_state_type):
                        def __setattr__(self, name, value):
                            if (
                                name == "state"
                                and value == "issuing"
                                and armed[0]
                            ):
                                super().__setattr__(name, value)
                                armed[0] = False
                                raise injected
                            super().__setattr__(name, value)

                    registry._confirmations[pending.match_run_id] = (
                        InterruptingRecoveryState(
                            original_run=original_state.original_run,
                            reviewed_run=original_state.reviewed_run,
                            original_draft_digest=(
                                original_state.original_draft_digest
                            ),
                            reviewed_request_digest=(
                                original_state.reviewed_request_digest
                            ),
                            confirmation_identity=(
                                original_state.confirmation_identity
                            ),
                            state=original_state.state,
                            owner=original_state.owner,
                            recovery_only=original_state.recovery_only,
                            completed_result=original_state.completed_result,
                        )
                    )

                with self.assertRaises(control_type) as raised:
                    create_match_run(
                        form,
                        registry,
                        confirmed_profile_artifact_sink=sink,
                        authentication_input=self._cookie_headers(),
                    )
                self.assertIs(raised.exception, injected)
                self.assertFalse(armed[0])
                with registry._condition:
                    recovered_state = registry._confirmations[
                        pending.match_run_id
                    ]
                    self.assertEqual(recovered_state.state, "maybe_issued")
                    self.assertIsNone(recovered_state.completed_result)
                completed = create_match_run(
                    form,
                    registry,
                    confirmed_profile_artifact_sink=sink,
                    authentication_input=self._cookie_headers(),
                )
                created = set(self._confirmation_artifact_references()) - baseline
                self.assertEqual(len(created), 1)
                self.assertEqual(
                    completed.artifact_offer.artifact_reference,
                    next(iter(created)),
                )
                self.assertEqual(recovery_modes, [False, True])

    def test_match_run_id_and_review_token_are_not_creation_references(self):
        offer = self._issue()
        registry = MatchRunRegistry()
        run = registry.create(
            owner_profile_id="unused",
            raw_input=RAW_ABOUT_YOU,
            input_style="short_paragraph",
            canonical_profile=self.reviewed_profile,
        )
        before = _logical_snapshot(self.path)
        malformed = self._post(offer, artifact=run.match_run_id)
        review_proof = profile_create_csrf_proof(
            self.session["csrf_secret"],
            run.review_token,
        )
        unknown = self._post(
            offer,
            artifact=run.review_token,
            proof=review_proof,
        )
        self.assertEqual((malformed.status, unknown.status), (400, 410))
        self.assertEqual(_logical_snapshot(self.path), before)

    def test_post_contract_rejects_malformed_and_identity_fields(self):
        offer = self._issue()
        valid = f"artifact={offer.artifact_reference}&csrf={offer.csrf_proof}".encode()
        cases = (
            ("query", {"target": "/account/profile?x=1"}),
            ("fragment", {"target": "/account/profile#x"}),
            ("media-parameter", {"content_type": "application/x-www-form-urlencoded; charset=utf-8"}),
            ("multiple-media", {"extra_headers": (("Content-Type", "application/x-www-form-urlencoded"),)}),
            ("missing-media", {"content_type": None}),
            ("missing-length", {"content_length": False}),
            ("duplicate-length", {"extra_headers": (("Content-Length", str(len(valid))),)}),
            ("leading-zero-length", {"content_length": "0" + str(len(valid))}),
            ("signed-length", {"content_length": "+" + str(len(valid))}),
            ("spaced-length", {"content_length": " " + str(len(valid))}),
            ("mismatched-length", {"content_length": str(len(valid) + 1)}),
            ("duplicate", {"body": valid + b"&artifact=" + offer.artifact_reference.encode()}),
            ("unknown", {"body": valid + b"&principal=server-private"}),
            ("additional", {"body": valid + b"&extra=1"}),
            ("percent", {"body": b"artifact=%30" + offer.artifact_reference[1:].encode() + b"&csrf=" + offer.csrf_proof.encode()}),
            ("missing", {"body": f"artifact={offer.artifact_reference}".encode()}),
            ("empty-artifact", {"body": f"artifact=&csrf={offer.csrf_proof}".encode()}),
            ("empty-csrf", {"body": f"artifact={offer.artifact_reference}&csrf=".encode()}),
            ("short-artifact", {"body": f"artifact={'A' * 42}&csrf={offer.csrf_proof}".encode()}),
            ("long-csrf", {"body": f"artifact={offer.artifact_reference}&csrf={'A' * 44}".encode()}),
            ("malformed-token", {"body": f"artifact={'!' * 43}&csrf={offer.csrf_proof}".encode()}),
            ("non-ascii-token", {"body": f"artifact={'é' * 43}&csrf={offer.csrf_proof}".encode("utf-8")}),
            ("alternate-base64-padding", {"body": valid + b"="}),
            ("value-over-128", {"body": f"artifact={'A' * 129}&csrf={offer.csrf_proof}".encode()}),
            ("invalid-utf8", {"body": b"artifact=" + b"\xff" * 43 + b"&csrf=" + offer.csrf_proof.encode()}),
            ("oversized", {"body": b"x" * 1025}),
            ("transfer", {"extra_headers": (("Transfer-Encoding", "chunked"),)}),
            ("forwarded", {"extra_headers": (("Forwarded", "host=evil.example"),)}),
            ("x-forwarded", {"extra_headers": (("X-Forwarded-Host", "evil.example"),)}),
            ("x-real-ip", {"extra_headers": (("X-Real-IP", "127.0.0.1"),)}),
        )
        before = _logical_snapshot(self.path)
        for label, kwargs in cases:
            with self.subTest(label=label):
                self.assertEqual(self._post(offer, **kwargs).status, 400)
        identity_fields = (
            "account",
            "account_id",
            "user_id",
            "session_id",
            "principal",
            "principal_id",
            "binding_id",
            "ownership_event_id",
            "environment",
            "profile_id",
            "revision_id",
            "source_id",
            "alias_id",
            "legacy_id",
        )
        for field in identity_fields:
            with self.subTest(identity_field=field):
                self.assertEqual(
                    self._post(
                        offer,
                        body=valid + f"&{field}=browser-selected".encode("ascii"),
                    ).status,
                    400,
                )
        unsupported = self.integration.handle(
            "PATCH",
            "/account/profile",
            self._cookie_headers(),
        )
        self.assertEqual(unsupported.status, 405)
        self.assertIn(("Allow", "GET, HEAD, POST"), unsupported.headers)
        self.assertEqual(_logical_snapshot(self.path), before)

    def test_same_origin_and_purpose_artifact_bound_csrf(self):
        offer = self._issue()
        before = _logical_snapshot(self.path)
        self.assertEqual(self._post(offer, origin="https://evil.example").status, 403)
        self.assertEqual(self._post(offer, fetch_site="cross-site").status, 403)
        self.assertEqual(
            self._post(offer, proof=self.session["csrf_secret"]).status,
            403,
        )
        other_reference = "Z" * 43
        self.assertEqual(
            self._post(
                offer,
                proof=profile_create_csrf_proof(
                    self.session["csrf_secret"],
                    other_reference,
                ),
            ).status,
            403,
        )
        other_purpose = hmac.digest(
            self.session["csrf_secret"].encode("ascii"),
            b"wahojobs.logout.v1\x00" + offer.artifact_reference.encode("ascii"),
            "sha256",
        )
        import base64

        other_purpose = base64.urlsafe_b64encode(other_purpose).rstrip(b"=").decode()
        self.assertEqual(self._post(offer, proof=other_purpose).status, 403)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with self.assertRaises(accounts.SessionUnavailable):
                accounts.validate_session_csrf(
                    connection,
                    session_token=self.session["session_token"],
                    csrf_secret=offer.csrf_proof,
                    now=self.now,
                )
        finally:
            connection.close()
        self.assertEqual(_logical_snapshot(self.path), before)

    def test_expired_revoked_logged_out_and_cross_session_reject_before_mutation(self):
        offer = self._issue()
        invalid = {**self.session, "session_token": "X" * 43}
        before = _logical_snapshot(self.path)
        self.assertEqual(self._post(offer, session=invalid).status, 401)
        self.assertEqual(_logical_snapshot(self.path), before)

        rotated = self._new_session(suffix="rotated")
        rotated_offer = self._issue(session=rotated)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            accounts.rotate_session(
                connection,
                session_token=rotated["session_token"],
                expected_session_version=1,
                idle_ttl=timedelta(hours=1),
                idempotency_key="b24d-rotated-session",
                now=self.now,
            )
        finally:
            connection.close()
        rotated_before = _logical_snapshot(self.path)
        self.assertEqual(self._post(rotated_offer, session=rotated).status, 401)
        self.assertEqual(_logical_snapshot(self.path), rotated_before)

        revoked = self._new_session(suffix="revoked")
        revoked_offer = self._issue(session=revoked)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            accounts.revoke_current_session(
                connection,
                session_token=revoked["session_token"],
                expected_session_version=1,
                reason="security_reset",
                now=self.now,
            )
        finally:
            connection.close()
        revoked_before = _logical_snapshot(self.path)
        self.assertEqual(self._post(revoked_offer, session=revoked).status, 401)
        self.assertEqual(_logical_snapshot(self.path), revoked_before)

        logged_out = self._new_session(suffix="logout")
        logged_out_offer = self._issue(session=logged_out)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            accounts.revoke_current_session(
                connection,
                session_token=logged_out["session_token"],
                expected_session_version=1,
                reason="user_logout",
                now=self.now,
            )
        finally:
            connection.close()
        logged_out_before = _logical_snapshot(self.path)
        self.assertEqual(self._post(logged_out_offer, session=logged_out).status, 401)
        self.assertEqual(_logical_snapshot(self.path), logged_out_before)

        expired = self._new_session(suffix="expired")
        expired_offer = self._issue(session=expired)
        self.now = self.now + timedelta(hours=3)
        expired_before = _logical_snapshot(self.path)
        self.assertEqual(self._post(expired_offer, session=expired).status, 401)
        self.assertEqual(_logical_snapshot(self.path), expired_before)
        self.assertEqual(_profile_counts(self.path), (0, 0, 0))

    def test_artifact_binding_expiry_capacity_tombstones_and_reconstruction(self):
        collision_tokens = _CollisionThenUniqueTokenFactory()
        collision_vault = ConfirmedProfileArtifactVault(
            monotonic=lambda: self.monotonic,
            token_factory=collision_tokens,
        )
        collision = self._build_integration(vault=collision_vault)
        first = self._issue(integration=collision)
        with self.assertRaises(ConfirmedProfileArtifactUnavailable):
            self._issue(
                integration=collision,
                raw_about_you=UNICODE_ABOUT_YOU,
            )
        self.assertEqual(collision_tokens.calls, 17)
        self.assertEqual(self._post(first, integration=collision).status, 303)
        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT source_content FROM product_profile_sources "
                    "WHERE source_ordinal=1"
                ).fetchone()[0],
                RAW_ABOUT_YOU,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM product_profile_sources "
                    "WHERE source_content=?",
                    (UNICODE_ABOUT_YOU,),
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()
        rebuilt = self._build_integration()
        self.assertEqual(self._post(first, integration=rebuilt).status, 410)

        self.monotonic = 101.0
        created_capacity = [
            self._issue(integration=collision)
            for _index in range(PROFILE_CREATE_ARTIFACT_CAPACITY - 1)
        ]
        self.monotonic = 699.0
        self.assertEqual(self._post(first, integration=collision).status, 303)
        with self.assertRaises(ConfirmedProfileArtifactUnavailable):
            self._issue(integration=collision)
        self.monotonic = 700.0
        created_replacement = self._issue(integration=collision)
        self.assertRegex(created_replacement.artifact_reference, r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(len(created_capacity), PROFILE_CREATE_ARTIFACT_CAPACITY - 1)

        self.monotonic = 800.0
        conflict_vault = ConfirmedProfileArtifactVault(
            monotonic=lambda: self.monotonic,
            token_factory=_TokenFactory(),
        )
        conflict = self._build_integration(vault=conflict_vault)
        conflict_offer = self._issue(integration=conflict)
        self.assertEqual(self._post(conflict_offer, integration=conflict).status, 409)
        self.monotonic = 801.0
        conflict_capacity = [
            self._issue(integration=conflict)
            for _index in range(PROFILE_CREATE_ARTIFACT_CAPACITY - 1)
        ]
        self.monotonic = 1399.0
        self.assertEqual(self._post(conflict_offer, integration=conflict).status, 409)
        with self.assertRaises(ConfirmedProfileArtifactUnavailable):
            self._issue(integration=conflict)
        self.monotonic = 1400.0
        self.assertRegex(
            self._issue(integration=conflict).artifact_reference,
            r"^[A-Za-z0-9_-]{43}$",
        )
        self.assertEqual(len(conflict_capacity), PROFILE_CREATE_ARTIFACT_CAPACITY - 1)

        entered = threading.Event()
        release = threading.Event()

        def before_open():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test_barrier_timeout")

        self.monotonic = 2000.0
        in_flight_vault = ConfirmedProfileArtifactVault(
            monotonic=lambda: self.monotonic,
            token_factory=_TokenFactory(),
        )
        in_flight = self._build_integration(
            vault=in_flight_vault,
            write_provider=_WriteProvider(self.path, before_open=before_open),
        )
        held = self._issue(integration=in_flight)
        self.monotonic = 2001.0
        held_capacity = [
            self._issue(integration=in_flight)
            for _index in range(PROFILE_CREATE_ARTIFACT_CAPACITY - 1)
        ]
        wrong_in_flight = self._new_session(suffix="in-flight-binding")
        held_result = []
        thread = threading.Thread(
            target=lambda: held_result.append(
                self._post(held, integration=in_flight).status
            ),
            name="b24d-capacity-in-flight",
        )
        thread.start()
        self.assertTrue(entered.wait(5))
        self.assertEqual(self._post(held, integration=in_flight).status, 503)
        with self.assertRaises(ConfirmedProfileArtifactUnavailable):
            self._issue(integration=in_flight)
        self.assertEqual(
            self._post(
                held,
                integration=in_flight,
                session=wrong_in_flight,
                proof=profile_create_csrf_proof(
                    wrong_in_flight["csrf_secret"],
                    held.artifact_reference,
                ),
            ).status,
            410,
        )
        self.monotonic = 2600.0
        with self.assertRaises(ConfirmedProfileArtifactUnavailable):
            self._issue(integration=in_flight)
        release.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(held_result, [409])
        self.assertRegex(
            self._issue(integration=in_flight).artifact_reference,
            r"^[A-Za-z0-9_-]{43}$",
        )
        self.assertEqual(len(held_capacity), PROFILE_CREATE_ARTIFACT_CAPACITY - 1)

        binding = self._build_integration()
        bound_offers = [self._issue(integration=binding) for _index in range(2)]
        second_session = self._new_session(suffix="binding")
        wrong_session_proof = profile_create_csrf_proof(
            second_session["csrf_secret"],
            bound_offers[0].artifact_reference,
        )
        self.assertEqual(
            self._post(
                bound_offers[0],
                integration=binding,
                session=second_session,
                proof=wrong_session_proof,
            ).status,
            410,
        )
        other_account = self._new_account_session(suffix="86")
        wrong_account_proof = profile_create_csrf_proof(
            other_account["csrf_secret"],
            bound_offers[1].artifact_reference,
        )
        before_wrong_account = _logical_snapshot(self.path)
        self.assertEqual(
            self._post(
                bound_offers[1],
                integration=binding,
                session=other_account,
                proof=wrong_account_proof,
            ).status,
            410,
        )
        self.assertEqual(_logical_snapshot(self.path), before_wrong_account)

    def test_canonical_v2_and_one_or_two_source_construction_are_exact(self):
        unexpected = self.reviewed_profile.to_mapping()
        first_provenance = next(iter(unexpected["provenance"]["field_sources"].values()))
        first_provenance["source"] = "unexpected_external_provenance"
        before_rejection = _logical_snapshot(self.path)
        with self.assertRaises(PersistentProfileDomainError):
            IdentityFreeCanonicalProfileV1.from_mapping(unexpected)
        self.assertEqual(_logical_snapshot(self.path), before_rejection)

        offer = self._issue()
        self.assertEqual(self._post(offer).status, 303)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            revision = connection.execute(
                "SELECT canonical_schema_version, structured_profile_json, "
                "normalizer_version, reviewer_version, actor_type, reason_code, "
                "idempotency_key FROM product_profile_revisions"
            ).fetchone()
            sources = connection.execute(
                "SELECT source_ordinal, source_type, source_format, source_schema_version, "
                "parser_version, source_content, CAST(source_content AS BLOB), "
                "source_content_sha256, accepted_at FROM product_profile_sources "
                "ORDER BY source_ordinal"
            ).fetchall()
            profile_v2 = json.loads(revision["structured_profile_json"])
            self.assertEqual(revision["canonical_schema_version"], "canonical_profile_v2")
            self.assertEqual(
                tuple(revision[name] for name in ("normalizer_version", "reviewer_version", "actor_type", "reason_code")),
                ("baseline_v1", "about_you_review_v1", "authenticated_user", "profile.create"),
            )
            self.assertRegex(revision["idempotency_key"], r"^profile-create:[A-Za-z0-9_-]{43}$")
            confirmed_at = self.now.isoformat(timespec="seconds")
            self.assertEqual(
                tuple(sources[0]),
                (
                    1,
                    "confirmed_about_you_text",
                    "text/plain",
                    "confirmed_about_you_text_v1",
                    None,
                    RAW_ABOUT_YOU,
                    RAW_ABOUT_YOU.encode("utf-8"),
                    hashlib.sha256(RAW_ABOUT_YOU.encode("utf-8")).hexdigest(),
                    confirmed_at,
                ),
            )
            expected_correction = json.dumps(
                {"schema_version": "user_confirmed_correction_v1", "updates": self.updates},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            self.assertEqual(
                tuple(sources[1]),
                (
                    2,
                    "user_confirmed_correction",
                    "application/json",
                    "user_confirmed_correction_v1",
                    None,
                    expected_correction,
                    expected_correction.encode("utf-8"),
                    hashlib.sha256(expected_correction.encode("utf-8")).hexdigest(),
                    confirmed_at,
                ),
            )
            ordinals = {
                tuple(item["source_ordinals"])
                for item in profile_v2["provenance"]["field_sources"]
            }
            self.assertTrue(ordinals <= {(1,), (2,)})
            self.assertNotIn(RAW_ABOUT_YOU, revision["structured_profile_json"])
            self.assertNotIn(expected_correction, revision["structured_profile_json"])
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            connection.close()

        second_path = Path(self.temp.name) / "one-source.sqlite"
        writer = install_browser_authentication_database(second_path)
        second_session = seed_browser_session(writer, suffix="85")
        writer.close()
        one_source_reviewed, one_source_updates = self._reviewed_profile(
            force_confirmation_sources=True
        )
        self.assertFalse(
            any(
                detail["source"] == "user_correction"
                for detail in one_source_reviewed["provenance"]["field_sources"].values()
            )
        )
        self.assertIs(type(one_source_updates), dict)
        authentication = DurableBrowserSessionAuthenticationGateway(
            trusted_environment_namespace=second_session["environment"],
            clock=lambda: self.now,
        )
        read_authorization = DurablePersistentProfileReadAuthorizationGateway()
        tokens = _TokenFactory()
        one_source_service = PersistentProfileCreationService(
            authentication_gateway=authentication,
            authorization_gateway=DurablePersistentProfileCreateAuthorizationGateway(
                read_authorization
            ),
            read_connection_provider=_ReadProvider(second_path),
            write_connection_provider=_WriteProvider(second_path),
            vault=ConfirmedProfileArtifactVault(
                monotonic=lambda: self.monotonic,
                token_factory=tokens,
            ),
            clock=lambda: self.now,
            token_factory=tokens,
        )
        one_source_read = PersistentProfileApplicationService(
            durable_authentication_gateway=authentication,
            durable_authorization_gateway=read_authorization,
            connection_provider=_ReadProvider(second_path),
        )
        one_source_integration = PersistentProfileBrowserIntegration(
            one_source_read,
            creation_service=one_source_service,
            public_origin=PUBLIC_ORIGIN,
        )
        self.integrations.append(one_source_integration)
        one_source_integration.activate()
        second_headers = (
            ("Host", PUBLIC_AUTHORITY),
            (
                "Cookie",
                cookie_header(
                    {
                        "wahojobs_session": second_session["session_token"],
                        "__Host-wahojobs_session_csrf": second_session["csrf_secret"],
                    }
                ),
            ),
        )
        one_profile_id = mock.Mock(
            wraps=persistent_profiles_domain.generate_profile_id
        )
        one_revision_id = mock.Mock(
            wraps=persistent_profiles_domain.generate_revision_id
        )
        one_source_id = mock.Mock(
            wraps=persistent_profiles_domain.generate_source_id
        )
        one_convert = mock.Mock(
            wraps=persistent_profile_creation.convert_v1_to_v2
        )
        with (
            mock.patch.object(
                persistent_profiles_domain,
                "generate_profile_id",
                one_profile_id,
            ),
            mock.patch.object(
                persistent_profiles_domain,
                "generate_revision_id",
                one_revision_id,
            ),
            mock.patch.object(
                persistent_profiles_domain,
                "generate_source_id",
                one_source_id,
            ),
            mock.patch.object(
                persistent_profile_creation,
                "convert_v1_to_v2",
                one_convert,
            ),
        ):
            second_offer = one_source_integration.issue_confirmed_artifact(
                reviewed_profile=one_source_reviewed,
                raw_about_you=UNICODE_ABOUT_YOU,
                normalized_updates=one_source_updates,
                profile_confirmed=True,
                authentication_input=second_headers,
            )
        self.assertEqual(
            (
                one_profile_id.call_count,
                one_revision_id.call_count,
                one_source_id.call_count,
            ),
            (1, 1, 1),
        )
        self.assertEqual(one_convert.call_count, 1)
        one_command = one_source_service._vault._records[
            second_offer.artifact_reference
        ].snapshot.command
        self.assertEqual(
            one_convert.call_args.kwargs["persistent_profile_id"],
            one_command.profile_id,
        )
        self.assertEqual(
            one_convert.call_args.args[0]["identity"]["profile_id"],
            one_command.profile_id,
        )
        second_body = (
            f"artifact={second_offer.artifact_reference}&csrf={second_offer.csrf_proof}"
        ).encode("ascii")
        second_response = one_source_integration.handle(
            "POST",
            "/account/profile",
            second_headers
            + (
                ("Origin", PUBLIC_ORIGIN),
                ("Sec-Fetch-Site", "same-origin"),
                ("Content-Type", "application/x-www-form-urlencoded"),
                ("Content-Length", str(len(second_body))),
            ),
            io.BytesIO(second_body),
        )
        self.assertEqual(second_response.status, 303)
        self.assertEqual(_profile_counts(second_path), (1, 1, 1))
        connection = sqlite3.connect(second_path)
        try:
            stored = connection.execute(
                "SELECT source_ordinal, source_type, source_format, source_schema_version, "
                "source_content, CAST(source_content AS BLOB), source_content_sha256, "
                "accepted_at FROM product_profile_sources"
            ).fetchone()
            self.assertEqual(
                tuple(stored),
                (
                    1,
                    "confirmed_about_you_text",
                    "text/plain",
                    "confirmed_about_you_text_v1",
                    UNICODE_ABOUT_YOU,
                    UNICODE_ABOUT_YOU.encode("utf-8"),
                    hashlib.sha256(UNICODE_ABOUT_YOU.encode("utf-8")).hexdigest(),
                    self.now.isoformat(timespec="seconds"),
                ),
            )
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            connection.close()

    def test_empty_get_create_redirect_and_stored_get(self):
        before = self.integration.handle(
            "GET",
            "/account/profile",
            self._cookie_headers(),
        )
        self.assertEqual(before.status, 200)
        self.assertIn(b"No persistent profile yet", before.body)
        offer = self._issue()
        created = self._post(
            offer,
            content_type="APPLICATION/X-WWW-FORM-URLENCODED",
        )
        self.assertEqual(created.status, 303)
        self.assertEqual(
            tuple(value for name, value in created.headers if name == "Location"),
            ("/find-matches",),
        )
        stored = self.integration.handle(
            "GET",
            "/account/profile",
            self._cookie_headers(),
        )
        self.assertEqual(stored.status, 200)
        self.assertNotIn(b"No persistent profile yet", stored.body)
        self.assertIn(EXPECTED_DISPLAY_NAME.encode("utf-8"), stored.body)

    def test_head_refresh_and_login_free_reads_remain_write_free(self):
        self.assertEqual(self._post(self._issue()).status, 303)
        before = _logical_snapshot(self.path)
        for method in ("GET", "HEAD", "GET", "HEAD"):
            response = self.integration.handle(
                method,
                "/account/profile",
                self._cookie_headers(),
            )
            self.assertEqual(response.status, 200)
        self.assertEqual(_logical_snapshot(self.path), before)

    def test_existing_profile_conflict_and_exact_artifact_replay(self):
        first = self._issue()
        second = self._issue()
        self.assertEqual(self._post(first).status, 303)
        after_create = _logical_snapshot(self.path)
        self.assertEqual(self._post(first).status, 303)
        self.assertEqual(self._post(second).status, 409)
        after_conflict = _logical_snapshot(self.path)
        self.assertEqual(self._post(second).status, 409)
        self.assertEqual(after_create, after_conflict)
        self.assertEqual(_logical_snapshot(self.path), after_create)

    def test_malformed_post_commit_result_remains_reconcilable_with_original_command(self):
        repository = PersistentProfileRepository()
        integration = self._build_integration(repository=repository)
        offer = self._issue(integration=integration)
        prepared = integration._creation_service._vault._records[
            offer.artifact_reference
        ].snapshot.command
        artifact_references = tuple(
            integration._creation_service._vault._records
        )
        token_count = self.tokens.value
        real_create = repository.create_account_native
        commands = []

        def malformed_once(connection, command, *, account_lineage):
            commands.append(command)
            result = real_create(
                connection,
                command,
                account_lineage=account_lineage,
            )
            if len(commands) == 1:
                return object()
            return result

        with mock.patch.object(
            repository,
            "create_account_native",
            side_effect=malformed_once,
        ):
            first = self._post(offer, integration=integration)
            committed = _logical_snapshot(self.path)
            self.assertEqual(first.status, 503)
            self.assertEqual(_profile_counts(self.path), (1, 1, 2))
            second = self._post(offer, integration=integration)

        self.assertEqual(second.status, 303)
        self.assertEqual(_logical_snapshot(self.path), committed)
        self.assertEqual(
            tuple(integration._creation_service._vault._records),
            artifact_references,
        )
        self.assertEqual(self.tokens.value, token_count)
        self.assertEqual(len(commands), 2)
        self.assertIs(commands[0], prepared)
        self.assertIs(commands[1], prepared)
        self.assertEqual(
            (
                commands[0].profile_id,
                commands[0].revision_id,
                commands[0].source_ids,
                commands[0].idempotency_scope()[1],
            ),
            (
                commands[1].profile_id,
                commands[1].revision_id,
                commands[1].source_ids,
                commands[1].idempotency_scope()[1],
            ),
        )

    def test_failure_and_uncertain_commit_preserve_atomicity_and_stable_retry(self):
        unicode_reviewed, unicode_updates = self._reviewed_profile(
            raw_about_you=UNICODE_ABOUT_YOU
        )
        unicode_updates = {**unicode_updates, "city": "São Paulo"}
        unicode_reviewed = apply_identity_free_profile_review(
            unicode_reviewed,
            unicode_updates,
        )
        fail_once = [True]
        rollback_error = RuntimeError("injected_before_commit")

        def fail_before_commit(boundary):
            if boundary == "create.after_profile_insert" and fail_once[0]:
                fail_once[0] = False
                raise rollback_error

        repository = PersistentProfileRepository(_failure_injector=fail_before_commit)
        vault = ConfirmedProfileArtifactVault(
            monotonic=lambda: self.monotonic,
            token_factory=_TokenFactory(),
        )
        failing = self._build_integration(
            repository=repository,
            vault=vault,
            write_provider=_WriteProvider(
                self.path,
                fail_after_commit_once=True,
            ),
        )
        nondurable_id = REVIEWER_PREVIEW_ID
        profile_id = mock.Mock(
            wraps=persistent_profiles_domain.generate_profile_id
        )
        revision_id = mock.Mock(
            wraps=persistent_profiles_domain.generate_revision_id
        )
        source_id = mock.Mock(
            wraps=persistent_profiles_domain.generate_source_id
        )

        def generated_identity_counts():
            return (
                profile_id.call_count,
                revision_id.call_count,
                source_id.call_count,
            )

        captured_logs = []

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                captured_logs.append(record.getMessage())

        capture_handler = CaptureHandler()
        logging.getLogger().addHandler(capture_handler)
        self.addCleanup(logging.getLogger().removeHandler, capture_handler)
        self.assertFalse(hasattr(persistent_profile_creation, "generate_profile_id"))
        self.assertFalse(
            hasattr(persistent_profile_creation, "_PROFILE_ID_CONVERSION_SENTINEL")
        )
        converted_profiles = []
        real_convert_v1_to_v2 = persistent_profile_creation.convert_v1_to_v2

        def observe_conversion(*args, **kwargs):
            converted = real_convert_v1_to_v2(
                *args,
                **kwargs,
            )
            converted_profiles.append(converted)
            return converted

        convert = mock.Mock(side_effect=observe_conversion)
        with (
            mock.patch.object(
                persistent_profiles_domain,
                "generate_profile_id",
                profile_id,
            ),
            mock.patch.object(
                persistent_profile_creation,
                "generate_profile_id",
                profile_id,
                create=True,
            ),
            mock.patch.object(
                persistent_profiles_domain,
                "generate_revision_id",
                revision_id,
            ),
            mock.patch.object(
                persistent_profiles_domain,
                "generate_source_id",
                source_id,
            ),
            mock.patch.object(
                persistent_profiles_domain.secrets,
                "token_hex",
                side_effect=(
                    "a" * 31 + "1",
                    "b" * 31 + "2",
                    "c" * 31 + "3",
                    "d" * 31 + "4",
                ),
            ) as token_hex,
            mock.patch.object(
                persistent_profile_creation,
                "convert_v1_to_v2",
                convert,
            ),
            mock.patch.object(
                repository,
                "create_account_native",
                wraps=repository.create_account_native,
            ) as create,
        ):
            offer = self._issue(
                integration=failing,
                reviewed=unicode_reviewed,
                updates=unicode_updates,
                raw_about_you=UNICODE_ABOUT_YOU,
            )
            self.assertEqual(generated_identity_counts(), (1, 1, 2))
            self.assertEqual(token_hex.call_count, 4)
            self.assertEqual(convert.call_count, 1)
            prepared_command = vault._records[offer.artifact_reference].snapshot.command
            self.assertEqual(
                convert.call_args.kwargs["persistent_profile_id"],
                prepared_command.profile_id,
            )
            self.assertEqual(
                convert.call_args.args[0]["identity"]["profile_id"],
                prepared_command.profile_id,
            )
            self.assertEqual(len(converted_profiles), 1)
            self.assertEqual(
                converted_profiles[0]["identity"]["profile_id"],
                prepared_command.profile_id,
            )
            self.assertEqual(
                json.dumps(
                    converted_profiles[0],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).count(prepared_command.profile_id),
                1,
            )
            self.assertNotIn(nondurable_id, repr(converted_profiles[0]))
            nondurable_selection = self._post(
                offer,
                integration=failing,
                body=urlencode(
                    {
                        "artifact": offer.artifact_reference,
                        "csrf": offer.csrf_proof,
                        "profile_id": nondurable_id,
                    }
                ).encode("ascii"),
            )
            self.assertEqual(nondurable_selection.status, 400)
            self.assertEqual(_profile_counts(self.path), (0, 0, 0))
            before = _logical_snapshot(self.path)
            rollback = self._post(offer, integration=failing)
            self.assertEqual(rollback.status, 503)
            self.assertEqual(_logical_snapshot(self.path), before)
            self.assertEqual(_profile_counts(self.path), (0, 0, 0))
            self.assertEqual(generated_identity_counts(), (1, 1, 2))
            uncertain = self._post(offer, integration=failing)
            self.assertEqual(uncertain.status, 503)
            committed = _logical_snapshot(self.path)
            self.assertEqual(_profile_counts(self.path), (1, 1, 2))
            self.assertEqual(generated_identity_counts(), (1, 1, 2))
            reconciled = self._post(offer, integration=failing)
            self.assertEqual(reconciled.status, 303)
            self.assertEqual(_logical_snapshot(self.path), committed)
            self.assertEqual(generated_identity_counts(), (1, 1, 2))
            replayed = self._post(offer, integration=failing)
            self.assertEqual(replayed.status, 303)
            self.assertEqual(_logical_snapshot(self.path), committed)
            self.assertEqual(generated_identity_counts(), (1, 1, 2))
            self.assertEqual(token_hex.call_count, 4)
            self.assertEqual(convert.call_count, 1)
            self.assertEqual(create.call_count, 3)
            commands = tuple(call.args[1] for call in create.call_args_list)
            lineages = tuple(
                call.kwargs["account_lineage"] for call in create.call_args_list
            )
            first_command = commands[0]
            self.assertIs(first_command, prepared_command)
            self.assertTrue(all(command is first_command for command in commands))
            self.assertTrue(all(lineage is lineages[0] for lineage in lineages))
            stable_ids = (
                first_command.profile_id,
                first_command.revision_id,
                first_command.source_ids,
            )
            self.assertNotEqual(first_command.profile_id, nondurable_id)
            self.assertEqual(
                first_command.trusted_structured_profile()["identity"]["profile_id"],
                first_command.profile_id,
            )
            trusted_profile = first_command.trusted_structured_profile()
            canonical_json = lambda value: json.dumps(
                value,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            canonical_unicode_json = lambda value: json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            expected_request_payload = {
                "operation": "create",
                "principal_id": first_command.principal.principal_id,
                "environment_namespace": (
                    first_command.principal.environment_namespace
                ),
                "canonical_schema_version": first_command.canonical_schema_version,
                "semantic_structured_profile_hash": (
                    first_command.semantic_profile_sha256
                ),
                "source_bundle_hash": first_command.source_bundle_sha256,
                "normalizer_version": first_command.normalizer_version,
                "reviewer_version": first_command.reviewer_version,
                "actor_type": first_command.actor_type,
                "reason_code": first_command.reason_code,
                "accepted_at": first_command.accepted_at,
                "idempotency_scope_version": "persistent_profile_principal_scope_v1",
                "idempotency_key_hash": hashlib.sha256(
                    first_command._idempotency_key.encode("utf-8")
                ).hexdigest(),
            }
            structured_preimage = canonical_unicode_json(trusted_profile)
            self.assertEqual(
                first_command._structured_profile_json,
                structured_preimage,
            )
            self.assertIn("São Paulo".encode("utf-8"), structured_preimage)
            self.assertNotEqual(
                structured_preimage,
                canonical_json(trusted_profile),
            )
            self.assertEqual(
                structured_preimage.count(
                    first_command.profile_id.encode("ascii")
                ),
                1,
            )
            self.assertEqual(
                first_command.structured_profile_sha256,
                hashlib.sha256(structured_preimage).hexdigest(),
            )
            semantic_projection = json.loads(
                json.dumps(trusted_profile, ensure_ascii=True)
            )
            del semantic_projection["identity"]["profile_id"]
            semantic_projection["provenance"]["field_sources"] = [
                detail
                for detail in semantic_projection["provenance"][
                    "field_sources"
                ]
                if detail.get("field_path") != "identity.profile_id"
            ]

            def contains_profile_id(value):
                if type(value) is dict:
                    return "profile_id" in value or any(
                        contains_profile_id(child)
                        for child in value.values()
                    )
                if type(value) is list:
                    return any(contains_profile_id(child) for child in value)
                return False

            self.assertFalse(contains_profile_id(semantic_projection))
            semantic_preimage = canonical_json(semantic_projection)
            self.assertIn(b"S\\u00e3o Paulo", semantic_preimage)
            self.assertEqual(
                first_command.semantic_profile_sha256,
                hashlib.sha256(semantic_preimage).hexdigest(),
            )
            source_preimages = tuple(
                source.content.encode("utf-8")
                for source in first_command.sources
            )
            source_hashes = tuple(
                hashlib.sha256(preimage).hexdigest()
                for preimage in source_preimages
            )
            self.assertEqual(
                first_command.source_content_sha256s,
                source_hashes,
            )
            bundle_manifest = {
                "version": "persistent_profile_source_bundle_v1",
                "sources": [
                    {
                        "ordinal": ordinal,
                        "source_type": source.source_type,
                        "source_format": source.source_format,
                        "source_schema_version": source.source_schema_version,
                        "parser_version": source.parser_version,
                        "confirmed_at": source.confirmed_at,
                        "byte_length": len(source_preimages[ordinal - 1]),
                        "source_content_hash": source_hashes[ordinal - 1],
                    }
                    for ordinal, source in enumerate(
                        first_command.sources,
                        start=1,
                    )
                ],
            }
            bundle_preimage = canonical_json(bundle_manifest)
            bundle_hash = hashlib.sha256(bundle_preimage).hexdigest()
            self.assertEqual(
                first_command.source_bundle_sha256,
                bundle_hash,
            )
            expected_request_payload[
                "semantic_structured_profile_hash"
            ] = hashlib.sha256(semantic_preimage).hexdigest()
            expected_request_payload["source_bundle_hash"] = bundle_hash
            request_preimage = canonical_json(
                {
                    "version": "persistent_profile_request_v1",
                    "request": expected_request_payload,
                }
            )
            self.assertEqual(
                first_command.request_fingerprint,
                hashlib.sha256(request_preimage).hexdigest(),
            )
            snapshot = vault._records[offer.artifact_reference].snapshot
            self.assertIs(snapshot.command, first_command)
            self.assertEqual(
                (
                    snapshot.command.profile_id,
                    snapshot.command.revision_id,
                    snapshot.command.source_ids,
                ),
                stable_ids,
            )
            artifact_payload = {
                "version": 1,
                "account_id": snapshot.account_id,
                "session_id": snapshot.session_id,
                "environment": snapshot.environment_namespace,
                "principal_id": snapshot.principal_id,
                "purpose": snapshot.purpose,
                "binding_id": snapshot.lineage.binding_id,
                "binding_version": snapshot.lineage.binding_version,
                "latest_event_version": snapshot.lineage.latest_event_version,
                "latest_event_id": snapshot.lineage.latest_event_id,
                "lineage_sha256": snapshot.lineage.lineage_sha256,
                "reviewed_profile_sha256": hashlib.sha256(
                    snapshot.reviewed_profile_json
                ).hexdigest(),
                "raw_about_you_sha256": hashlib.sha256(
                    snapshot.raw_about_you.encode("utf-8")
                ).hexdigest(),
                "correction_sha256": (
                    None
                    if snapshot.correction_json is None
                    else hashlib.sha256(
                        snapshot.correction_json.encode("utf-8")
                    ).hexdigest()
                ),
                "accepted_at": snapshot.accepted_at.isoformat(),
                "confirmed_at": snapshot.confirmation_time.isoformat(),
                "normalizer_version": "baseline_v1",
                "reviewer_version": "about_you_review_v1",
                "actor_type": "authenticated_user",
                "reason_code": "profile.create",
                "idempotency_key_sha256": hashlib.sha256(
                    snapshot.idempotency_key.encode("ascii")
                ).hexdigest(),
                "command": {
                    "profile_id": first_command.profile_id,
                    "revision_id": first_command.revision_id,
                    "source_ids": list(first_command.source_ids),
                    "structured_profile_sha256": hashlib.sha256(
                        structured_preimage
                    ).hexdigest(),
                    "semantic_profile_sha256": hashlib.sha256(
                        semantic_preimage
                    ).hexdigest(),
                    "source_content_sha256s": list(source_hashes),
                    "source_bundle_sha256": bundle_hash,
                    "request_fingerprint": hashlib.sha256(
                        request_preimage
                    ).hexdigest(),
                },
            }
            artifact_preimage = canonical_json(artifact_payload)
            self.assertEqual(
                snapshot.content_fingerprint,
                hashlib.sha256(artifact_preimage).hexdigest(),
            )
            identity_free_preimages = (
                unicode_reviewed.canonical_bytes,
                semantic_preimage,
                *source_preimages,
                bundle_preimage,
                request_preimage,
                artifact_preimage,
            )
            forbidden_identity_values = (
                nondurable_id,
                "preview_profile",
                FORMER_SEMANTIC_PROFILE_ID,
            )
            for preimage in identity_free_preimages:
                for forbidden in forbidden_identity_values:
                    self.assertNotIn(forbidden.encode("utf-8"), preimage)
                self.assertNotIn(b"Preview Profile", preimage)
            stored_get = failing.handle(
                "GET",
                "/account/profile",
                self._cookie_headers(),
            )
            response_material = repr(
                [
                    (response.status, response.headers, response.body)
                    for response in (
                        nondurable_selection,
                        rollback,
                        uncertain,
                        reconciled,
                        replayed,
                        stored_get,
                    )
                ]
            )
            command_material = repr(
                (
                    first_command,
                    first_command.public_dict(),
                    first_command.trusted_structured_profile(),
                    first_command._structured_profile_json,
                    first_command.structured_profile_sha256,
                    first_command.semantic_profile_sha256,
                    first_command.source_content_sha256s,
                    first_command.source_bundle_sha256,
                    first_command.request_fingerprint,
                    tuple(source.content_bytes for source in first_command.sources),
                    snapshot.content_fingerprint,
                    rollback_error,
                    captured_logs,
                )
            )
            for forbidden in forbidden_identity_values:
                self.assertNotIn(forbidden, response_material)
                self.assertNotIn(forbidden, command_material)
        connection = sqlite3.connect(self.path)
        try:
            stored = connection.execute(
                "SELECT p.profile_id, r.revision_id FROM product_profiles p "
                "JOIN product_profile_revisions r ON r.profile_id=p.profile_id"
            ).fetchone()
            stored_sources = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT source_id FROM product_profile_sources ORDER BY source_ordinal"
                )
            )
            self.assertEqual(
                tuple(stored),
                (first_command.profile_id, first_command.revision_id),
            )
            self.assertEqual(stored_sources, first_command.source_ids)
            durable_rows = tuple(
                connection.execute(f"SELECT * FROM {table}").fetchall()
                for table in (
                    "product_profiles",
                    "product_profile_revisions",
                    "product_profile_sources",
                )
            )
            for forbidden in forbidden_identity_values:
                self.assertNotIn(forbidden, repr(durable_rows))
        finally:
            connection.close()

        fatal_path = Path(self.temp.name) / "fatal-content.sqlite"
        writer = install_browser_authentication_database(fatal_path)
        fatal_session = seed_browser_session(writer, suffix="89")
        writer.close()
        fatal_repository = PersistentProfileRepository()
        fatal = self._build_integration(
            path=fatal_path,
            session=fatal_session,
            repository=fatal_repository,
        )
        fatal_offer = self._issue(integration=fatal, session=fatal_session)
        fatal_before = _logical_snapshot(fatal_path)
        with mock.patch.object(
            fatal_repository,
            "create_account_native",
            side_effect=PersistentProfileDomainError("invalid_command"),
        ):
            self.assertEqual(
                self._post(
                    fatal_offer,
                    integration=fatal,
                    session=fatal_session,
                ).status,
                503,
            )
        self.assertEqual(_logical_snapshot(fatal_path), fatal_before)
        self.assertEqual(_profile_counts(fatal_path), (0, 0, 0))
        self.assertEqual(
            self._post(
                fatal_offer,
                integration=fatal,
                session=fatal_session,
            ).status,
            410,
        )

        race_path = Path(self.temp.name) / "lineage-race.sqlite"
        writer = install_browser_authentication_database(race_path)
        race_session = seed_browser_session(writer, suffix="88")
        account_b = add_active_user(writer, suffix="b24d-lineage-b")
        writer.commit()
        writer.close()
        authorized = threading.Event()
        resume = threading.Event()

        def before_race_write():
            authorized.set()
            if not resume.wait(5):
                raise RuntimeError("test_barrier_timeout")

        race = self._build_integration(
            path=race_path,
            session=race_session,
            write_provider=_WriteProvider(
                race_path,
                before_open=before_race_write,
            ),
        )
        race_offer = self._issue(integration=race, session=race_session)
        race_responses = []
        race_thread = threading.Thread(
            target=lambda: race_responses.append(
                self._post(
                    race_offer,
                    integration=race,
                    session=race_session,
                )
            ),
            name="b24d-account-lineage-race",
        )
        race_thread.start()
        self.assertTrue(authorized.wait(5))
        transition_connection = sqlite3.connect(race_path)
        transition_connection.row_factory = sqlite3.Row
        transition_connection.execute("PRAGMA foreign_keys = ON")
        try:
            released = transition_binding(
                transition_connection,
                race_session,
                "released",
            )
            replacement = ownership.create_binding_with_initial_event(
                transition_connection,
                ownership.CreateBindingCommand(
                    principal_id=race_session["principal_id"],
                    user_id=account_b,
                    binding_role="owner",
                    actor_type="administrator",
                    reason_code="authorization_test",
                    approval_reference="b24d-lineage-transfer",
                    idempotency_key="b24d-lineage-transfer-to-b",
                    occurred_at="2026-07-21T12:00:01+00:00",
                    metadata={},
                ),
            )
        finally:
            transition_connection.close()
        after_transition = _logical_snapshot(race_path)
        self.assertEqual(_profile_counts(race_path), (0, 0, 0))
        resume.set()
        race_thread.join(timeout=5)
        self.assertFalse(race_thread.is_alive())
        self.assertEqual(len(race_responses), 1)
        self.assertEqual(race_responses[0].status, 503)
        self.assertEqual(_logical_snapshot(race_path), after_transition)
        self.assertEqual(_profile_counts(race_path), (0, 0, 0))
        self.assertEqual(
            self._post(
                race_offer,
                integration=race,
                session=race_session,
            ).status,
            404,
        )
        public = repr(race_responses[0].headers) + race_responses[0].body.decode(
            "utf-8"
        )
        for identifier in (
            race_session["account_id"],
            account_b,
            race_session["principal_id"],
            race_session["binding_id"],
            released.event_id,
            replacement.binding_id,
            replacement.event_id,
            race_session["session_id"],
        ):
            self.assertNotIn(identifier, public)

    def test_same_and_different_artifact_concurrency_converges(self):
        entered = threading.Event()
        release = threading.Event()
        first_boundary = [True]

        def blocking_hook(boundary):
            if boundary == "create.after_profile_insert" and first_boundary[0]:
                first_boundary[0] = False
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test_barrier_timeout")

        repository = PersistentProfileRepository(_failure_injector=blocking_hook)
        integration = self._build_integration(
            repository=repository,
            write_provider=_WriteProvider(self.path, timeout=0.02),
        )
        same = self._issue(integration=integration)
        different = self._issue(integration=integration)
        first_result = []

        def create_first():
            first_result.append(self._post(same, integration=integration).status)

        thread = threading.Thread(target=create_first, name="b24d-first-create")
        with mock.patch.object(
            repository,
            "create_account_native",
            wraps=repository.create_account_native,
        ) as create:
            try:
                thread.start()
                self.assertTrue(entered.wait(5))
                self.assertEqual(create.call_count, 1)
                self.assertEqual(self._post(same, integration=integration).status, 503)
                self.assertEqual(
                    create.call_count,
                    1,
                    "same-artifact contention must have one repository invocation owner",
                )
                different_during = self._post(different, integration=integration).status
                self.assertEqual(different_during, 503)
                self.assertEqual(create.call_count, 2)
            finally:
                release.set()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(first_result, [303])
            self.assertEqual(self._post(different, integration=integration).status, 409)
            self.assertEqual(create.call_count, 3)
        self.assertEqual(_profile_counts(self.path), (1, 1, 2))

    def test_zero_egress_response_redaction_and_exact_database_delta(self):
        connection = sqlite3.connect(self.path)
        try:
            add_alias(
                connection,
                self.session["principal_id"],
                suffix="84",
                environment=self.session["environment"],
                kind="legacy_user_id",
                value="legacy-owner-b24d-redaction",
                claimability="account_native",
            )
            connection.commit()
        finally:
            connection.close()
        before = _logical_snapshot(self.path)
        captured_logs = []

        class Capture(logging.Handler):
            def emit(self, record):
                captured_logs.append(self.format(record))

        handler = Capture()
        root = logging.getLogger()
        root.addHandler(handler)
        with mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("external_egress_forbidden"),
        ):
            try:
                offer = self._issue()
                response = self._post(offer)
            finally:
                root.removeHandler(handler)
        self.assertEqual(response.status, 303)
        self.assertFalse(any(name.lower() == "set-cookie" for name, _value in response.headers))
        connection = sqlite3.connect(self.path)
        try:
            identifiers = {
                "account": self.session["account_id"],
                "identity": connection.execute(
                    "SELECT auth_identity_id FROM auth_identities"
                ).fetchone()[0],
                "session": self.session["session_id"],
                "principal": self.session["principal_id"],
                "binding": self.session["binding_id"],
                "ownership_event": connection.execute(
                    "SELECT event_id FROM ownership_binding_events "
                    "WHERE binding_id=? ORDER BY event_version DESC LIMIT 1",
                    (self.session["binding_id"],),
                ).fetchone()[0],
                "profile": connection.execute(
                    "SELECT profile_id FROM product_profiles"
                ).fetchone()[0],
                "revision": connection.execute(
                    "SELECT revision_id FROM product_profile_revisions"
                ).fetchone()[0],
                "source": connection.execute(
                    "SELECT source_id FROM product_profile_sources "
                    "ORDER BY source_ordinal LIMIT 1"
                ).fetchone()[0],
                "alias": connection.execute(
                    "SELECT alias_id FROM legacy_owner_aliases"
                ).fetchone()[0],
                "legacy": connection.execute(
                    "SELECT alias_value FROM legacy_owner_aliases"
                ).fetchone()[0],
            }
            before_map = dict(before)
            after_map = dict(_logical_snapshot(self.path))
            changed_tables = {
                table
                for table in before_map
                if before_map[table] != after_map[table]
            }
            self.assertEqual(
                changed_tables,
                {
                    "product_profiles",
                    "product_profile_revisions",
                    "product_profile_sources",
                },
            )
            public = (
                repr(response.headers)
                + response.body.decode("utf-8")
                + "\n".join(captured_logs)
            )
            self.assertEqual(
                set(identifiers),
                {
                    "account",
                    "identity",
                    "session",
                    "principal",
                    "binding",
                    "ownership_event",
                    "profile",
                    "revision",
                    "source",
                    "alias",
                    "legacy",
                },
            )
            for identifier in identifiers.values():
                self.assertNotIn(identifier, public)
            for private_value in (
                offer.artifact_reference,
                offer.csrf_proof,
                RAW_ABOUT_YOU,
            ):
                self.assertNotIn(private_value, public)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            connection.close()

    def test_claim_handoff_terminalization_and_release_interruptions_are_exception_safe(self):
        class InterruptOnce:
            def __init__(self, boundary, exception):
                self.boundary = boundary
                self.exception = exception
                self.fired = False

            def __call__(self, boundary):
                if boundary == self.boundary and not self.fired:
                    self.fired = True
                    raise self.exception

        cases = (
            ("owner", "claim.owner_constructed", RuntimeError("ordinary-owner"), False),
            ("publication", "claim.published", KeyboardInterrupt("publication"), False),
            ("handoff", "claim.consumer_handoff", SystemExit("handoff"), False),
            (
                "consumer-returned",
                "claim.consumer_returned",
                KeyboardInterrupt("consumer-returned"),
                False,
            ),
            ("terminalization", "claim.terminalize_enter", GeneratorExit("terminal"), False),
            (
                "terminalized",
                "claim.terminalized",
                SystemExit("terminalized"),
                False,
            ),
            ("release", "claim.release_enter", KeyboardInterrupt("release"), True),
            ("released", "claim.released", SystemExit("released"), True),
        )

        def await_claim_cleanup(vault, reference):
            with vault._lock:
                record = vault._records.get(reference)
                owner = None if record is None else record.cleanup_owner
            if owner is not None:
                self.assertTrue(owner._safe.wait(3))

        for index, (label, boundary, exception, needs_rollback) in enumerate(cases, start=1):
            with self.subTest(boundary=label):
                path = Path(self.temp.name) / f"claim-{index}.sqlite"
                writer = install_browser_authentication_database(path)
                session = seed_browser_session(writer, suffix=str(90 + index))
                writer.close()
                interrupt = InterruptOnce(boundary, exception)
                vault = ConfirmedProfileArtifactVault(
                    monotonic=lambda: self.monotonic,
                    token_factory=_TokenFactory(),
                    _failure_injector=interrupt,
                )
                fail_once = [needs_rollback]

                def repository_hook(repository_boundary):
                    if (
                        repository_boundary == "create.after_profile_insert"
                        and fail_once[0]
                    ):
                        fail_once[0] = False
                        raise RuntimeError("ordinary-precommit-rollback")

                integration = self._build_integration(
                    path=path,
                    session=session,
                    vault=vault,
                    repository=PersistentProfileRepository(
                        _failure_injector=repository_hook
                    ),
                )
                offer = self._issue(integration=integration, session=session)
                initial_results = []
                initial_errors = []

                def run_initial_attempt():
                    try:
                        initial_results.append(
                            self._post(
                                offer,
                                integration=integration,
                                session=session,
                            )
                        )
                    except BaseException as exc:
                        initial_errors.append(exc)

                initial = threading.Thread(
                    target=run_initial_attempt,
                    name=f"b24d-claim-watchdog-{index}",
                    daemon=True,
                )
                initial.start()
                initial.join(timeout=5)
                self.assertFalse(initial.is_alive())
                if type(exception) is RuntimeError:
                    self.assertEqual(initial_errors, [])
                    self.assertEqual(
                        [response.status for response in initial_results],
                        [503],
                    )
                else:
                    self.assertEqual(initial_results, [])
                    self.assertEqual(len(initial_errors), 1)
                    self.assertIs(initial_errors[0], exception)
                self.assertTrue(interrupt.fired)
                await_claim_cleanup(vault, offer.artifact_reference)
                self.assertEqual(
                    self._post(
                        offer,
                        integration=integration,
                        session=session,
                    ).status,
                    303,
                )
                self.assertEqual(_profile_counts(path), (1, 1, 2))

        nested_path = Path(self.temp.name) / "claim-primary-preserved.sqlite"
        writer = install_browser_authentication_database(nested_path)
        nested_session = seed_browser_session(writer, suffix="97")
        writer.close()
        primary = KeyboardInterrupt("primary-handoff")
        secondary_interruptions = (
            KeyboardInterrupt("secondary-cleanup-1"),
            SystemExit("secondary-cleanup-2"),
            GeneratorExit("secondary-cleanup-3"),
        )
        cleanup_boundaries = (
            "claim.release_compare_transition",
            "claim.release_compare_transition_reentrant",
            "claim.release_compare_transition_reentrant_again",
        )
        interruption_by_boundary = dict(
            zip(cleanup_boundaries, secondary_interruptions, strict=True)
        )
        primary_caught = threading.Event()
        release_primary_catch = threading.Event()
        propagated = threading.Event()
        transition_entered = threading.Event()
        retry_hold_enabled = threading.Event()
        retry_handoff = threading.Event()
        release_retry = threading.Event()
        transition_calls = []
        transition_observations = []
        captured_old_owner = []
        primary_fired = False

        def nested_interrupt(boundary):
            nonlocal primary_fired
            if boundary == "claim.consumer_handoff" and not primary_fired:
                primary_fired = True
                raise primary
            if boundary == "claim.consumer_handoff" and retry_hold_enabled.is_set():
                retry_handoff.set()
                if not release_retry.wait(3):
                    raise RuntimeError("test_barrier_timeout")
            if (
                boundary not in interruption_by_boundary
                or threading.current_thread().name
                != "confirmed-profile-claim-transition"
            ):
                return
            record = nested_vault._records[nested_offer.artifact_reference]
            if not captured_old_owner:
                captured_old_owner.append(
                    (record.claim_token, record.cleanup_owner)
                )
            transition_calls.append((boundary, threading.current_thread().name))
            transition_observations.append(sys.exception())
            if boundary == cleanup_boundaries[0]:
                transition_entered.set()
                if not primary_caught.wait(3):
                    raise RuntimeError("primary_catch_not_observed")
            raise interruption_by_boundary[boundary]

        nested_vault = ConfirmedProfileArtifactVault(
            monotonic=lambda: self.monotonic,
            token_factory=_TokenFactory(),
            _failure_injector=nested_interrupt,
        )
        nested = self._build_integration(
            path=nested_path,
            session=nested_session,
            vault=nested_vault,
        )
        nested_offer = self._issue(integration=nested, session=nested_session)

        observed_primary = []
        exception_identity_during_catch = []
        catch_wait_results = []
        unexpected_initial_results = []

        def run_interrupted_request():
            try:
                unexpected_initial_results.append(
                    self._post(
                        nested_offer,
                        integration=nested,
                        session=nested_session,
                    )
                )
            except BaseException as exc:
                observed_primary.append(exc)
                exception_identity_during_catch.append(sys.exception())
                primary_caught.set()
                catch_wait_results.append(release_primary_catch.wait(3))
                exception_identity_during_catch.append(sys.exception())
            finally:
                propagated.set()

        request = threading.Thread(
            target=run_interrupted_request,
            name="b24d-interrupted-request",
        )
        request.start()
        self.assertTrue(primary_caught.wait(3))
        self.assertTrue(transition_entered.wait(3))
        cleanup_wait_results = []
        cleanup_records = []

        def observe_cleanup_completion():
            try:
                _old_token, old_owner = captured_old_owner[0]
                cleanup_wait_results.append(old_owner._safe.wait(3))
                with nested_vault._lock:
                    record = nested_vault._records[nested_offer.artifact_reference]
                    cleanup_records.append(
                        (
                            record.state,
                            record.claim_token,
                            record.recovery_state,
                            record.cleanup_owner,
                        )
                    )
            finally:
                release_primary_catch.set()

        observer = threading.Thread(
            target=observe_cleanup_completion,
            name="b24d-cleanup-observer",
        )
        observer.start()
        observer.join(timeout=5)
        request.join(timeout=5)
        self.assertFalse(observer.is_alive())
        self.assertFalse(request.is_alive())
        self.assertEqual(unexpected_initial_results, [])
        self.assertEqual(observed_primary, [primary])
        self.assertEqual(exception_identity_during_catch, [primary, primary])
        self.assertEqual(catch_wait_results, [True])
        self.assertTrue(propagated.is_set())
        self.assertEqual(cleanup_wait_results, [True])
        self.assertEqual(cleanup_records, [("available", None, None, None)])
        self.assertEqual(
            transition_calls,
            [
                (boundary, "confirmed-profile-claim-transition")
                for boundary in cleanup_boundaries
            ],
        )
        self.assertEqual(
            transition_observations,
            [None, secondary_interruptions[0], secondary_interruptions[1]],
        )
        self.assertIsNone(secondary_interruptions[0].__context__)
        self.assertIs(
            secondary_interruptions[1].__context__,
            secondary_interruptions[0],
        )
        self.assertIs(
            secondary_interruptions[2].__context__,
            secondary_interruptions[1],
        )
        recovered = nested_vault._records[nested_offer.artifact_reference]
        self.assertEqual(recovered.state, "available")
        self.assertIsNone(recovered.claim_token)
        self.assertIsNone(recovered.recovery_state)
        self.assertIsNone(recovered.cleanup_owner)
        self.assertEqual(_profile_counts(nested_path), (0, 0, 0))
        self.assertFalse(
            any(
                thread.is_alive()
                and thread.name == "confirmed-profile-claim-transition"
                for thread in threading.enumerate()
            )
        )

        retry_hold_enabled.set()
        retry_results = []
        retry_errors = []

        def run_legitimate_retry():
            try:
                retry_results.append(
                    self._post(
                        nested_offer,
                        integration=nested,
                        session=nested_session,
                    )
                )
            except BaseException as exc:
                retry_errors.append(exc)

        retry_thread = threading.Thread(
            target=run_legitimate_retry,
            name="b24d-legitimate-retry",
        )
        retry_thread.start()
        self.assertTrue(retry_handoff.wait(3))
        current = nested_vault._records[nested_offer.artifact_reference]
        old_token, old_owner = captured_old_owner[0]
        self.assertIsNot(current.claim_token, old_token)
        self.assertIsNot(current.cleanup_owner, old_owner)
        self.assertTrue(
            nested_vault._release_core(
                nested_offer.artifact_reference,
                old_token,
                old_owner,
            )
        )
        unchanged = nested_vault._records[nested_offer.artifact_reference]
        self.assertEqual(unchanged.state, "in_flight")
        self.assertIs(unchanged.claim_token, current.claim_token)
        self.assertIs(unchanged.cleanup_owner, current.cleanup_owner)
        release_retry.set()
        retry_thread.join(timeout=5)
        self.assertFalse(retry_thread.is_alive())
        self.assertEqual(retry_errors, [])
        self.assertEqual([response.status for response in retry_results], [303])
        self.assertEqual(
            transition_calls,
            [
                (boundary, "confirmed-profile-claim-transition")
                for boundary in cleanup_boundaries
            ],
        )
        self.assertEqual(_profile_counts(nested_path), (1, 1, 2))
        saved_monotonic = self.monotonic
        try:
            self.monotonic = (
                saved_monotonic + PROFILE_CREATE_ARTIFACT_LIFETIME_SECONDS
            )
            replacement_after_deadline = self._issue(
                integration=nested,
                session=nested_session,
            )
            self.assertNotEqual(
                replacement_after_deadline.artifact_reference,
                nested_offer.artifact_reference,
            )
        finally:
            self.monotonic = saved_monotonic

        retired_path = Path(self.temp.name) / "claim-fatal-retirement.sqlite"
        writer = install_browser_authentication_database(retired_path)
        retired_session = seed_browser_session(writer, suffix="99")
        writer.close()
        retirement_interrupt = GeneratorExit("fatal-retirement")
        retired_repository = PersistentProfileRepository()
        retirement_owners = []
        retirement_fired = False

        def interrupt_after_retirement_intent(boundary):
            nonlocal retirement_fired
            if boundary != "claim.release_target_requested" or retirement_fired:
                return
            retirement_fired = True
            record = retired_vault._records[retired_offer.artifact_reference]
            retirement_owners.append(record.cleanup_owner)
            raise retirement_interrupt

        retired_vault = ConfirmedProfileArtifactVault(
            monotonic=lambda: self.monotonic,
            token_factory=_TokenFactory(),
            _failure_injector=interrupt_after_retirement_intent,
        )
        retired = self._build_integration(
            path=retired_path,
            session=retired_session,
            repository=retired_repository,
            vault=retired_vault,
        )
        retired_offer = self._issue(
            integration=retired,
            session=retired_session,
        )
        with mock.patch.object(
            retired_repository,
            "create_account_native",
            side_effect=PersistentProfileDomainError("invalid_command"),
        ):
            with self.assertRaises(GeneratorExit) as raised:
                self._post(
                    retired_offer,
                    integration=retired,
                    session=retired_session,
                )
        self.assertIs(raised.exception, retirement_interrupt)
        self.assertTrue(retirement_fired)
        self.assertEqual(len(retirement_owners), 1)
        self.assertEqual(retirement_owners[0]._requested_target, "retired")
        self.assertTrue(retirement_owners[0]._safe.wait(3))
        retired_record = retired_vault._records[retired_offer.artifact_reference]
        self.assertEqual(retired_record.state, "retired")
        self.assertIsNone(retired_record.claim_token)
        self.assertIsNone(retired_record.recovery_state)
        self.assertIsNone(retired_record.cleanup_owner)
        self.assertEqual(_profile_counts(retired_path), (0, 0, 0))
        self.assertEqual(
            self._post(
                retired_offer,
                integration=retired,
                session=retired_session,
            ).status,
            410,
        )

        capacity_path = Path(self.temp.name) / "claim-capacity.sqlite"
        writer = install_browser_authentication_database(capacity_path)
        capacity_session = seed_browser_session(writer, suffix="98")
        writer.close()

        def interrupt_every_publication(boundary):
            if boundary == "claim.published":
                raise RuntimeError("ordinary-publication-interruption")

        self.monotonic = 3000.0
        capacity = self._build_integration(
            path=capacity_path,
            session=capacity_session,
            vault=ConfirmedProfileArtifactVault(
                monotonic=lambda: self.monotonic,
                token_factory=_TokenFactory(),
                _failure_injector=interrupt_every_publication,
            ),
        )
        offers = [
            self._issue(integration=capacity, session=capacity_session)
            for _index in range(PROFILE_CREATE_ARTIFACT_CAPACITY)
        ]
        for offer in offers:
            self.assertEqual(
                self._post(
                    offer,
                    integration=capacity,
                    session=capacity_session,
                ).status,
                503,
            )
        self.assertEqual(_profile_counts(capacity_path), (0, 0, 0))
        with self.assertRaises(ConfirmedProfileArtifactUnavailable):
            self._issue(integration=capacity, session=capacity_session)
        self.monotonic = 3600.0
        replacement = self._issue(integration=capacity, session=capacity_session)
        self.assertRegex(replacement.artifact_reference, r"^[A-Za-z0-9_-]{43}$")


class PersistentProfileCreationRuntimeIntegrationTests(unittest.TestCase):
    def test_production_activation_owns_claim_worker_before_start_and_never_leaks(self):
        from wahojobs import durable_google_login_runtime as runtime_module

        worker_name = "confirmed-profile-claim-cleanup"

        def live_workers():
            return tuple(
                thread
                for thread in threading.enumerate()
                if thread.name == worker_name and thread.is_alive()
            )

        self.assertEqual(live_workers(), ())
        cases = (RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit)
        with temporary_browser_login_state() as state:
            for exception_type in cases:
                with self.subTest(exception_type=exception_type.__name__):
                    injected = exception_type(
                        "injected_after_real_claim_coordinator_start"
                    )
                    registrations = []
                    started_workers = []
                    original_own = runtime_module._CleanupCoordinator.own
                    original_start = threading.Thread.start

                    def observe_own(
                        outer,
                        category,
                        resource,
                        action,
                        **options,
                    ):
                        token = original_own(
                            outer,
                            category,
                            resource,
                            action,
                            **options,
                        )
                        if category == "profile_integration":
                            registrations.append((outer, resource, token))
                        return token

                    def start_then_interrupt(worker, *args, **kwargs):
                        if worker.name != worker_name:
                            return original_start(worker, *args, **kwargs)
                        self.assertEqual(len(registrations), 1)
                        outer, integration, _token = registrations[0]
                        creation_service = object.__getattribute__(
                            integration,
                            "_creation_service",
                        )
                        vault = object.__getattribute__(
                            creation_service,
                            "_vault",
                        )
                        claim_coordinator = object.__getattribute__(
                            vault,
                            "_cleanup_coordinator",
                        )
                        self.assertIs(
                            object.__getattribute__(
                                claim_coordinator,
                                "_thread",
                            ),
                            worker,
                        )
                        self.assertIn(
                            "profile_integration",
                            outer.snapshot().unresolved_resources,
                        )
                        started_workers.append(worker)
                        original_start(worker, *args, **kwargs)
                        raise injected

                    with (
                        mock.patch.object(
                            runtime_module._CleanupCoordinator,
                            "own",
                            new=observe_own,
                        ),
                        mock.patch.object(
                            threading.Thread,
                            "start",
                            new=start_then_interrupt,
                        ),
                    ):
                        if exception_type is RuntimeError:
                            with self.assertRaises(
                                runtime_module.DurableGoogleLoginConfigurationError
                            ):
                                build_durable_google_login_runtime(
                                    state.configuration_path,
                                    _clock=state.clock,
                                    _gateway_factory=state.gateway_factory,
                                )
                        else:
                            with self.assertRaises(exception_type) as caught:
                                build_durable_google_login_runtime(
                                    state.configuration_path,
                                    _clock=state.clock,
                                    _gateway_factory=state.gateway_factory,
                                )
                            self.assertIs(caught.exception, injected)

                    self.assertEqual(len(registrations), 1)
                    self.assertEqual(len(started_workers), 1)
                    started_workers[0].join(timeout=1)
                    self.assertFalse(started_workers[0].is_alive())
                    self.assertTrue(
                        registrations[0][0].snapshot().cleanup_complete
                    )
                    self.assertTrue(registrations[0][1].closed)
                    self.assertEqual(live_workers(), ())

    def test_claim_worker_bounded_close_is_truthful_and_retryable(self):
        import time
        from wahojobs import durable_google_login_runtime as runtime_module

        entered = threading.Event()
        release = threading.Event()
        original_run = (
            persistent_profile_creation._ClaimCleanupCoordinator._run
        )
        runtime = None
        worker = None

        def held_run(coordinator):
            entered.set()
            release.wait(5)
            return original_run(coordinator)

        try:
            with temporary_browser_login_state() as state:
                with mock.patch.object(
                    persistent_profile_creation._ClaimCleanupCoordinator,
                    "_run",
                    new=held_run,
                ):
                    runtime = build_durable_google_login_runtime(
                        state.configuration_path,
                        _clock=state.clock,
                        _gateway_factory=state.gateway_factory,
                    )

                profile_integration = object.__getattribute__(
                    runtime,
                    "_profile_integration",
                )
                creation_service = object.__getattribute__(
                    profile_integration,
                    "_creation_service",
                )
                vault = object.__getattribute__(
                    creation_service,
                    "_vault",
                )
                claim_coordinator = object.__getattribute__(
                    vault,
                    "_cleanup_coordinator",
                )
                worker = object.__getattribute__(claim_coordinator, "_thread")
                self.assertTrue(entered.wait(1))
                self.assertTrue(worker.is_alive())

                with mock.patch.object(
                    persistent_profile_creation,
                    "_CLAIM_CLEANUP_CLOSE_JOIN_SECONDS",
                    0.05,
                ):
                    before = time.monotonic()
                    first = runtime.close()
                    elapsed = time.monotonic() - before

                self.assertLess(elapsed, 1.0)
                self.assertFalse(first.cleanup_complete)
                self.assertIn(
                    "profile_integration",
                    first.unresolved_resources,
                )
                self.assertTrue(worker.is_alive())
                self.assertFalse(vault.closed)
                self.assertFalse(profile_integration.closed)

                release.set()
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive())

                second = runtime.close()
                self.assertTrue(second.cleanup_complete)
                self.assertTrue(vault.closed)
                self.assertTrue(profile_integration.closed)
                self.assertFalse(
                    any(
                        thread.name == "confirmed-profile-claim-cleanup"
                        and thread.is_alive()
                        for thread in threading.enumerate()
                    )
                )
        finally:
            release.set()
            if worker is not None:
                worker.join(timeout=2)
            if runtime is not None:
                runtime.close(_preserve_primary=True)
            runtime_module._retry_unresolved_activation_handoffs()

    def test_claim_worker_start_close_race_is_bounded_and_retryable(self):
        import time

        entered = threading.Event()
        release = threading.Event()
        activation_failures = []
        activation = None
        vault = ConfirmedProfileArtifactVault(
            monotonic=lambda: 1.0,
            token_factory=_TokenFactory(),
        )
        original_start = threading.Thread.start

        def held_start(worker, *args, **kwargs):
            if worker.name != "confirmed-profile-claim-cleanup":
                return original_start(worker, *args, **kwargs)
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test_start_release_timeout")
            return original_start(worker, *args, **kwargs)

        def activate():
            try:
                vault.activate()
            except BaseException as exc:
                activation_failures.append(exc)

        try:
            with (
                mock.patch.object(
                    threading.Thread,
                    "start",
                    new=held_start,
                ),
                mock.patch.object(
                    persistent_profile_creation,
                    "_CLAIM_CLEANUP_CLOSE_JOIN_SECONDS",
                    0.05,
                ),
            ):
                activation = threading.Thread(
                    target=activate,
                    name="b24d-worker-start-race",
                    daemon=False,
                )
                activation.start()
                self.assertTrue(entered.wait(1))
                before = time.monotonic()
                self.assertFalse(vault.close())
                self.assertLess(time.monotonic() - before, 1.0)
                self.assertFalse(vault.closed)
                release.set()
                activation.join(2)
            self.assertFalse(activation.is_alive())
            self.assertEqual(len(activation_failures), 1)
            self.assertIs(type(activation_failures[0]), RuntimeError)
            self.assertTrue(vault.close())
            self.assertTrue(vault.closed)
            self.assertFalse(
                any(
                    thread.name == "confirmed-profile-claim-cleanup"
                    and thread.is_alive()
                    for thread in threading.enumerate()
                )
            )

            closed_before_activation = ConfirmedProfileArtifactVault(
                monotonic=lambda: 1.0,
                token_factory=_TokenFactory(),
            )
            self.assertTrue(closed_before_activation.close())
            with mock.patch.object(
                threading.Thread,
                "start",
                wraps=threading.Thread.start,
            ) as start:
                with self.assertRaises(RuntimeError):
                    closed_before_activation.activate()
            self.assertEqual(start.call_count, 0)
        finally:
            release.set()
            if activation is not None:
                activation.join(2)
            vault.close()

    def test_production_activation_rejects_coordinator_that_dies_during_start(self):
        from wahojobs import durable_google_login_runtime as runtime_module

        finished = threading.Event()
        original_start = threading.Thread.start

        def return_immediately(_coordinator):
            finished.set()

        def start_and_observe_exit(worker, *args, **kwargs):
            result = original_start(worker, *args, **kwargs)
            if worker.name == "confirmed-profile-claim-cleanup":
                self.assertTrue(finished.wait(1))
                worker.join(1)
            return result

        with temporary_browser_login_state() as state:
            with (
                mock.patch.object(
                    persistent_profile_creation._ClaimCleanupCoordinator,
                    "_run",
                    new=return_immediately,
                ),
                mock.patch.object(
                    threading.Thread,
                    "start",
                    new=start_and_observe_exit,
                ),
            ):
                with self.assertRaises(
                    runtime_module.DurableGoogleLoginConfigurationError
                ):
                    build_durable_google_login_runtime(
                        state.configuration_path,
                        _clock=state.clock,
                        _gateway_factory=state.gateway_factory,
                    )
        self.assertTrue(finished.is_set())
        self.assertFalse(
            any(
                thread.name == "confirmed-profile-claim-cleanup"
                and thread.is_alive()
                for thread in threading.enumerate()
            )
        )

    def test_routing_only_exclusive_handler_fails_closed_without_legacy_matching(self):
        class RoutingOnlyIntegration:
            def __init__(self):
                self.handled = []

            def matches_route(self, _path):
                return False

            def handle(self, method, target, _headers, _body_stream=None):
                self.handled.append((method, target))
                raise AssertionError("exclusive routing fallback invoked")

        integration = RoutingOnlyIntegration()
        handler_type = local_product_app.make_handler(
            durable_google_login_browser_integration=integration,
            exclusive_browser_integration=True,
        )
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                handler = object.__new__(handler_type)
                handler.path = "/find-matches"
                handler.headers = Message()
                handler.headers["Host"] = PUBLIC_AUTHORITY
                handler.rfile = io.BytesIO(b"input_text=must-not-run")
                handler.write_safe_browser_error = mock.Mock()
                with (
                    mock.patch.object(
                        local_product_app,
                        "render_preview_from_params",
                    ) as legacy_render,
                    mock.patch.object(
                        local_product_app,
                        "create_match_run",
                    ) as legacy_create,
                ):
                    getattr(handler, f"do_{method}")()
                handler.write_safe_browser_error.assert_called_once_with(
                    "Profile creation is temporarily unavailable.",
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                )
                legacy_render.assert_not_called()
                legacy_create.assert_not_called()
        self.assertEqual(integration.handled, [])

    def test_find_matches_get_and_initial_post_reject_invalid_authority(self):
        class RoutingIntegration:
            def matches_route(self, _path):
                return False

            def handle(self, *_args, **_kwargs):
                raise AssertionError("unexpected forced integration dispatch")

        class ReadCounter(io.BytesIO):
            def __init__(self, body):
                super().__init__(body)
                self.read_count = 0

            def read(self, *args, **kwargs):
                self.read_count += 1
                return super().read(*args, **kwargs)

        handler_type = local_product_app.make_handler(
            durable_google_login_browser_integration=RoutingIntegration(),
            exclusive_browser_integration=True,
            confirmed_profile_artifact_sink=lambda **_kwargs: None,
            completed_profile_confirmation_authenticator=lambda **_kwargs: False,
            profile_confirmation_public_origin=PUBLIC_ORIGIN,
        )

        def request(method, headers, body=b""):
            handler = object.__new__(handler_type)
            handler.path = "/find-matches"
            handler.headers = Message()
            for name, value in headers:
                handler.headers[name] = value
            handler.rfile = ReadCounter(body)
            handler.write_safe_browser_error = mock.Mock()
            handler.write_html = mock.Mock()
            handler.redirect = mock.Mock()
            getattr(handler, f"do_{method}")()
            return handler

        for label, headers in (
            ("get-host", (("Host", "evil.example"),)),
            (
                "get-proxy",
                (("Host", PUBLIC_AUTHORITY), ("Forwarded", "host=evil.example")),
            ),
        ):
            with self.subTest(label=label):
                handler = request("GET", headers)
                handler.write_safe_browser_error.assert_called_once_with(
                    "This profile request is not valid.",
                    status=HTTPStatus.BAD_REQUEST,
                )

        body = urlencode(
            {"input_text": RAW_ABOUT_YOU, "input_style": "short_paragraph"}
        ).encode("ascii")
        base = (
            ("Origin", PUBLIC_ORIGIN),
            ("Sec-Fetch-Site", "same-origin"),
            ("Content-Type", "application/x-www-form-urlencoded"),
            ("Content-Length", str(len(body))),
        )
        for label, authority in (
            ("post-host", (("Host", "evil.example"),)),
            (
                "post-proxy",
                (("Host", PUBLIC_AUTHORITY), ("X-Forwarded-Host", "evil.example")),
            ),
        ):
            with self.subTest(label=label):
                handler = request("POST", authority + base, body)
                handler.write_safe_browser_error.assert_called_once_with(
                    "This profile request is not valid.",
                    status=HTTPStatus.BAD_REQUEST,
                )
                self.assertEqual(handler.rfile.read_count, 0)

    def test_find_matches_strict_form_buffer_rejects_bad_encoding_and_reads_once(self):
        class RoutingIntegration:
            def matches_route(self, _path):
                return False

            def handle(self, *_args, **_kwargs):
                raise AssertionError("unexpected forced integration dispatch")

        class ReadCounter(io.BytesIO):
            def __init__(self, body):
                super().__init__(body)
                self.read_count = 0

            def read(self, *args, **kwargs):
                self.read_count += 1
                return super().read(*args, **kwargs)

        handler_type = local_product_app.make_handler(
            durable_google_login_browser_integration=RoutingIntegration(),
            exclusive_browser_integration=True,
            confirmed_profile_artifact_sink=lambda **_kwargs: None,
            completed_profile_confirmation_authenticator=lambda **_kwargs: False,
            profile_confirmation_public_origin=PUBLIC_ORIGIN,
        )

        def submit(body):
            handler = object.__new__(handler_type)
            handler.path = "/find-matches"
            handler.headers = Message()
            for name, value in (
                ("Host", PUBLIC_AUTHORITY),
                ("Origin", PUBLIC_ORIGIN),
                ("Sec-Fetch-Site", "same-origin"),
                ("Content-Type", "application/x-www-form-urlencoded"),
                ("Content-Length", str(len(body))),
            ):
                handler.headers[name] = value
            handler.rfile = ReadCounter(body)
            handler.write_safe_browser_error = mock.Mock()
            handler.write_html = mock.Mock()
            handler.redirect = mock.Mock()
            handler.do_POST()
            return handler

        for label, body in (
            (
                "invalid-percent",
                b"input_text=bad%ZZ&input_style=short_paragraph",
            ),
            (
                "invalid-utf8",
                b"input_text=bad%FF&input_style=short_paragraph",
            ),
        ):
            with self.subTest(label=label):
                handler = submit(body)
                handler.write_safe_browser_error.assert_called_once_with(
                    "This profile request is not valid.",
                    status=HTTPStatus.BAD_REQUEST,
                )
                self.assertEqual(handler.rfile.read_count, 1)

        valid = urlencode(
            {"input_text": RAW_ABOUT_YOU, "input_style": "short_paragraph"}
        ).encode("ascii")
        handler = submit(valid)
        self.assertEqual(handler.rfile.read_count, 1)
        handler.write_safe_browser_error.assert_not_called()
        handler.write_html.assert_not_called()
        handler.redirect.assert_called_once()
        self.assertEqual(handler.redirect.call_args.args[0], "/find-matches")

    def test_invited_login_create_reconstruct_and_later_login_reuse_profile(self):
        email = "b24d-invited@example.test"
        with ExitStack() as stack:
            state = stack.enter_context(
                temporary_browser_login_state(
                    seed_existing_identity=False,
                    enable_invited_provisioning=True,
                )
            )
            stack.enter_context(loopback_and_in_memory_provider_only())
            connection = sqlite3.connect(state.database_path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                invitation = accounts.create_invitation(
                    connection,
                    email=email,
                    lookup_key=INVITATION_KEY,
                    expires_at=state.clock() + timedelta(days=7),
                    created_by="b24d_operator",
                    idempotency_key="b24d-invitation",
                    now=state.clock(),
                )
                _seed_invited_match_inventory(
                    connection,
                    observed_at=state.clock(),
                )
            finally:
                connection.close()

            runtime = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            cookies = {}
            artifact_reference = None
            csrf_proof = None
            try:
                with running_https_production_launcher_app(runtime):
                    login = https_request(state, "GET", "/login")
                    _merge_response_cookies(cookies, login)
                    body = form_body(
                        csrf=cookies["__Host-wahojobs_login_csrf"],
                        invitation=invitation.invitation_token,
                    )
                    start = https_request(
                        state,
                        "POST",
                        "/auth/google/start",
                        headers=(
                            ("Origin", state.public_origin),
                            ("Sec-Fetch-Site", "same-origin"),
                            ("Content-Type", "application/x-www-form-urlencoded"),
                            ("Cookie", cookie_header(cookies)),
                        ),
                        body=body,
                    )
                    _merge_response_cookies(cookies, start)
                    provider_url = start.header_values("Location")[0]
                    callback_url = provider_callback_for(
                        state,
                        provider_url,
                        code="b24d-first-login",
                        claims_overrides={"email": email, "email_verified": True},
                    )
                    callback_parts = urlsplit(callback_url)
                    callback = https_request(
                        state,
                        "GET",
                        callback_parts.path + "?" + callback_parts.query,
                        headers=(("Cookie", cookie_header(cookies)),),
                    )
                    self.assertEqual(callback.status, 303)
                    _merge_response_cookies(cookies, callback)
                    empty = https_request(
                        state,
                        "GET",
                        "/account/profile",
                        headers=(("Cookie", cookie_header(cookies)),),
                    )
                    self.assertIn(b"No persistent profile yet", empty.body)
                    self.assertEqual(
                        empty.header_values("Referrer-Policy"),
                        ("no-referrer",),
                    )

                    with (
                        mock.patch.object(
                            local_product_app,
                            "build_current_structured_preview_context",
                            side_effect=AssertionError("matching_path_must_remain_dormant"),
                        ),
                    ):
                        opened = https_request(
                            state,
                            "GET",
                            "/find-matches",
                            headers=(("Cookie", cookie_header(cookies)),),
                        )
                        self.assertEqual(opened.status, 200)
                        self.assertIn(b"find-matches-form", opened.body)
                        self.assertEqual(
                            opened.header_values("Referrer-Policy"),
                            ("same-origin",),
                        )

                        initial = https_request(
                            state,
                            "POST",
                            "/find-matches",
                            headers=(
                                ("Origin", state.public_origin),
                                ("Sec-Fetch-Site", "same-origin"),
                                ("Content-Type", "application/x-www-form-urlencoded"),
                                ("Cookie", cookie_header(cookies)),
                            ),
                            body=urlencode(
                                {
                                    "input_text": RAW_ABOUT_YOU,
                                    "input_style": "short_paragraph",
                                }
                            ).encode("utf-8"),
                        )
                        self.assertEqual(initial.status, 303)
                        review_location = initial.header_values("Location")
                        self.assertEqual(len(review_location), 1)
                        self.assertRegex(
                            review_location[0],
                            r"^/find-matches\?run=[A-Za-z0-9_-]+&review=1$",
                        )

                        review = https_request(
                            state,
                            "GET",
                            review_location[0],
                            headers=(("Cookie", cookie_header(cookies)),),
                        )
                        self.assertEqual(review.status, 200)
                        self.assertEqual(
                            review.header_values("Referrer-Policy"),
                            ("same-origin",),
                        )
                        review_action, review_fields = _submitted_form(
                            review.body,
                            "profile-review-form",
                        )
                        self.assertEqual(review_action, "/find-matches")
                        review_values = dict(review_fields)
                        self.assertEqual(
                            set(review_values).intersection(
                                {
                                    "profile_id",
                                    "revision_id",
                                    "source_id",
                                    "principal_id",
                                }
                            ),
                            set(),
                        )
                        self.assertRegex(
                            review_values["profile_draft_fingerprint"],
                            r"^[0-9a-f]{64}$",
                        )
                        expected_draft = normalize_identity_free_profile_input(
                            RAW_ABOUT_YOU,
                            "short_paragraph",
                        )
                        self.assertEqual(
                            review_values["profile_draft_fingerprint"],
                            hashlib.sha256(
                                expected_draft.canonical_bytes
                            ).hexdigest(),
                        )
                        self.assertNotIn(
                            expected_draft.canonical_bytes,
                            review.body,
                        )
                        self.assertNotIn(
                            RAW_ABOUT_YOU.encode("utf-8"),
                            review_values["profile_draft_fingerprint"].encode(
                                "ascii"
                            ),
                        )
                        for forbidden in (
                            REVIEWER_PREVIEW_ID,
                            "preview_profile",
                            FORMER_SEMANTIC_PROFILE_ID,
                            "Preview Profile",
                        ):
                            self.assertNotIn(
                                forbidden.encode("utf-8"),
                                review.body,
                            )
                        review_fields = tuple(review_fields) + (
                            ("credentials_confirmed", "1"),
                        )
                        profile_integration = object.__getattribute__(
                            runtime,
                            "_profile_integration",
                        )
                        creation_service = object.__getattribute__(
                            profile_integration,
                            "_creation_service",
                        )
                        artifact_vault = object.__getattribute__(
                            creation_service,
                            "_vault",
                        )
                        artifact_count = len(artifact_vault._records)
                        for identity_field in (
                            "profile_id",
                            "identity",
                            "persistent_profile_id",
                        ):
                            with self.subTest(identity_field=identity_field):
                                rejected_identity = https_request(
                                    state,
                                    "POST",
                                    "/find-matches",
                                    headers=(
                                        ("Origin", state.public_origin),
                                        ("Sec-Fetch-Site", "same-origin"),
                                        (
                                            "Content-Type",
                                            "application/x-www-form-urlencoded",
                                        ),
                                        ("Cookie", cookie_header(cookies)),
                                    ),
                                    body=urlencode(
                                        review_fields
                                        + ((identity_field, REVIEWER_PREVIEW_ID),)
                                    ).encode("utf-8"),
                                )
                                self.assertEqual(rejected_identity.status, 400)
                                self.assertEqual(
                                    len(artifact_vault._records),
                                    artifact_count,
                                )
                                self.assertFalse(
                                    rejected_identity.header_values("Set-Cookie")
                                )
                                rejected_public = (
                                    repr(rejected_identity.headers)
                                    + rejected_identity.body.decode("utf-8")
                                )
                                self.assertNotIn(
                                    REVIEWER_PREVIEW_ID,
                                    rejected_public,
                                )
                        self.assertEqual(_profile_counts(state.database_path), (0, 0, 0))
                        confirmation = https_request(
                            state,
                            "POST",
                            "/find-matches",
                            headers=(
                                ("Origin", state.public_origin),
                                ("Sec-Fetch-Site", "same-origin"),
                                ("Content-Type", "application/x-www-form-urlencoded"),
                                ("Cookie", cookie_header(cookies)),
                            ),
                            body=urlencode(review_fields).encode("utf-8"),
                        )
                    self.assertEqual(confirmation.status, 200)
                    self.assertEqual(
                        confirmation.header_values("Content-Type"),
                        ("text/html; charset=utf-8",),
                    )
                    self.assertEqual(
                        confirmation.header_values("Cache-Control"),
                        ("no-store",),
                    )
                    self.assertEqual(
                        confirmation.header_values("Content-Length"),
                        (str(len(confirmation.body)),),
                    )
                    self.assertEqual(
                        confirmation.header_values("Content-Security-Policy"),
                        (
                            "default-src 'none'; style-src 'unsafe-inline'; "
                            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
                        ),
                    )
                    self.assertEqual(
                        confirmation.header_values("Referrer-Policy"),
                        ("same-origin",),
                    )
                    self.assertEqual(
                        confirmation.header_values("X-Content-Type-Options"),
                        ("nosniff",),
                    )
                    self.assertFalse(confirmation.header_values("Set-Cookie"))
                    self.assertEqual(
                        confirmation.body.count(
                            b"<form method='post' action='/account/profile'>"
                        ),
                        1,
                    )
                    hidden_fields = re.findall(
                        rb"<input type='hidden' name='([^']+)' value='([^']*)'>",
                        confirmation.body,
                    )
                    self.assertEqual(len(hidden_fields), 2)
                    hidden = dict(hidden_fields)
                    self.assertEqual(set(hidden), {b"artifact", b"csrf"})
                    self.assertTrue(
                        all(
                            re.fullmatch(rb"[A-Za-z0-9_-]{43}", value)
                            for value in hidden.values()
                        )
                    )
                    artifact_reference = hidden[b"artifact"].decode("ascii")
                    csrf_proof = hidden[b"csrf"].decode("ascii")
                    create_body = form_body(
                        artifact=artifact_reference,
                        csrf=csrf_proof,
                    )
                    created = https_request(
                        state,
                        "POST",
                        "/account/profile",
                        headers=(
                            ("Origin", state.public_origin),
                            ("Sec-Fetch-Site", "same-origin"),
                            ("Content-Type", "application/x-www-form-urlencoded"),
                            ("Cookie", cookie_header(cookies)),
                        ),
                        body=create_body,
                    )
                    self.assertEqual(created.status, 303)
                    self.assertEqual(created.header_values("Location"), ("/find-matches",))
                    matches = https_request(
                        state,
                        "GET",
                        created.header_values("Location")[0],
                        headers=(("Cookie", cookie_header(cookies)),),
                    )
                    self.assertEqual(matches.status, 200)
                    self.assertIn(
                        b"Distinctive Remote Python Evaluation Engineer",
                        matches.body,
                    )
                    self.assertIn(
                        b"href='https://jobs.example.test/distinctive-invited-python'",
                        matches.body,
                    )
                    self.assertIn(
                        b"target='_blank' rel='noopener noreferrer'",
                        matches.body,
                    )
                    stored = https_request(
                        state,
                        "GET",
                        "/account/profile",
                        headers=(("Cookie", cookie_header(cookies)),),
                    )
                    self.assertIn(
                        EXPECTED_DISPLAY_NAME.encode("utf-8"),
                        stored.body,
                    )
            finally:
                report = runtime.close()
                self.assertTrue(report.cleanup_complete)
                state.close_harnesses()

            reconstructed = build_durable_google_login_runtime(
                state.configuration_path,
                _clock=state.clock,
                _gateway_factory=state.gateway_factory,
            )
            try:
                with running_https_production_launcher_app(reconstructed):
                    old_body = form_body(
                        artifact=artifact_reference,
                        csrf=csrf_proof,
                    )
                    gone = https_request(
                        state,
                        "POST",
                        "/account/profile",
                        headers=(
                            ("Origin", state.public_origin),
                            ("Sec-Fetch-Site", "same-origin"),
                            ("Content-Type", "application/x-www-form-urlencoded"),
                            ("Cookie", cookie_header(cookies)),
                        ),
                        body=old_body,
                    )
                    self.assertEqual(gone.status, 410)
                    login = https_request(state, "GET", "/login")
                    later_cookies = {}
                    _merge_response_cookies(later_cookies, login)
                    start_body = form_body(
                        csrf=later_cookies["__Host-wahojobs_login_csrf"]
                    )
                    start = https_request(
                        state,
                        "POST",
                        "/auth/google/start",
                        headers=(
                            ("Origin", state.public_origin),
                            ("Sec-Fetch-Site", "same-origin"),
                            ("Content-Type", "application/x-www-form-urlencoded"),
                            ("Cookie", cookie_header(later_cookies)),
                        ),
                        body=start_body,
                    )
                    _merge_response_cookies(later_cookies, start)
                    callback_url = provider_callback_for(
                        state,
                        start.header_values("Location")[0],
                        code="b24d-later-login",
                    )
                    parts = urlsplit(callback_url)
                    callback = https_request(
                        state,
                        "GET",
                        parts.path + "?" + parts.query,
                        headers=(("Cookie", cookie_header(later_cookies)),),
                    )
                    self.assertEqual(callback.status, 303)
                    _merge_response_cookies(later_cookies, callback)
                    stored = https_request(
                        state,
                        "GET",
                        "/account/profile",
                        headers=(("Cookie", cookie_header(later_cookies)),),
                    )
                    self.assertEqual(stored.status, 200)
                    self.assertIn(
                        EXPECTED_DISPLAY_NAME.encode("utf-8"),
                        stored.body,
                    )
                    regenerated = https_request(
                        state,
                        "GET",
                        "/find-matches",
                        headers=(("Cookie", cookie_header(later_cookies)),),
                    )
                    self.assertEqual(regenerated.status, 200)
                    self.assertIn(
                        b"Distinctive Remote Python Evaluation Engineer",
                        regenerated.body,
                    )
            finally:
                report = reconstructed.close()
                self.assertTrue(report.cleanup_complete)
                state.close_harnesses()

            connection = sqlite3.connect(state.database_path)
            try:
                self.assertEqual(
                    tuple(
                        connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                        for table in (
                            "users",
                            "auth_identities",
                            "product_principals",
                            "principal_account_bindings",
                            "ownership_binding_events",
                            "product_profiles",
                            "product_profile_revisions",
                        )
                    ),
                    (1, 1, 1, 1, 1, 1, 1),
                )
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
