from __future__ import annotations

from datetime import timedelta
from io import BytesIO
from pathlib import Path
import re
import sqlite3
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

from tests.browser_session_authentication_test_support import (
    AUTHENTICATED_AT,
    REQUEST_AT,
    install_browser_authentication_database,
    seed_browser_session,
)
from tests.persistent_profile_read_authorization_test_support import (
    transition_binding,
)
from tests.test_profile_intake_foundation import _docx_bytes, _pdf_bytes
from wahojobs import accounts
from wahojobs.browser_session_authentication import (
    DurableBrowserSessionAuthenticationGateway,
)
from wahojobs.persistent_profile_read_authorization import (
    DurablePersistentProfileReadAuthorizationGateway,
)
from wahojobs.persistent_profiles_application import PersistentProfilePageResult
from wahojobs.persistent_profiles_browser import render_persistent_profile_page
from wahojobs.profile_intake.browser import (
    MAX_MULTIPART_BODY_BYTES,
    ProfileIntakeBrowserIntegration,
    _CLASSIFICATION_DESCRIPTIONS,
    _SKIP_SUGGESTION_VALUE,
    _preference_form_values_for_model,
    _review_fact_value_control,
    _failure,
)
from wahojobs.profile_intake.contracts import (
    AI_EXTRACTION_SCHEMA_VERSION,
    DocumentFormat,
    DocumentKind,
    ModelEvidencePacket,
    ProfileIntakeError,
    _FIELD_SPECS,
    validate_ai_profile_extraction,
)
from wahojobs.profile_intake.runtime import (
    PROFILE_INTAKE_REVIEW_ROUTE,
    PROFILE_INTAKE_ROUTE,
    IntakeDraftVault,
    ProfileIntakeAuthorityService,
    ProfileIntakeProcessingService,
    profile_intake_csrf_proof,
    review_value_for_form,
)
from wahojobs.profiles.preference_model import (
    ISO_4217_CURRENCIES,
    empty_profile_preferences_v1,
    profile_preference_control_catalog_v1,
)


PUBLIC_ORIGIN = "https://localhost:8443"
PUBLIC_AUTHORITY = "localhost:8443"


class _ReadProvider:
    def __init__(self, path):
        self.path = Path(path)

    def __call__(self):
        from contextlib import contextmanager

        @contextmanager
        def scope():
            connection = sqlite3.connect(
                f"file:{self.path.as_posix()}?mode=ro",
                uri=True,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA query_only = ON")
            try:
                yield connection
            finally:
                connection.close()

        return scope()


class _TokenFactory:
    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1
        return ("D" + str(self.count).rjust(42, "0"))[-43:]


class _RecordingAdapter:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def extract(self, evidence):
        if type(evidence) is not ModelEvidencePacket:
            raise AssertionError("raw evidence crossed the model boundary")
        self.calls.append(evidence)
        if self.fail:
            raise ProfileIntakeError("openai_transport_error")
        reference = evidence.document_reference
        available = tuple(block.reference for block in evidence.blocks)
        if not available:
            raise ProfileIntakeError("no_model_safe_evidence")
        block = available[0]
        return validate_ai_profile_extraction(
            {
                "schema_version": AI_EXTRACTION_SCHEMA_VERSION,
                "document_reference": reference,
                "facts": [
                    {
                        "field_path": "experience.job_titles",
                        "value": "Software Engineer",
                        "source_document_reference": reference,
                        "evidence_block_references": [block],
                        "confidence": 0.98,
                        "explicit": True,
                    },
                    {
                        "field_path": "experience.seniority",
                        "value": "senior",
                        "source_document_reference": reference,
                        "evidence_block_references": [block],
                        "confidence": 0.75,
                        "explicit": False,
                    },
                ],
            },
            evidence,
        )


class _NoReadStream:
    def read(self, _size=-1):
        raise AssertionError("oversized request body must not be read")


class _OverreadStream:
    def read(self, size=-1):
        return b"X" * (size + 1)


class _BlockingAdapter(_RecordingAdapter):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def extract(self, evidence):
        self.entered.set()
        if not self.release.wait(5):
            raise ProfileIntakeError("synthetic_timeout")
        return super().extract(evidence)


class _ExplodingAdapter:
    def extract(self, _evidence):
        raise RuntimeError("synthetic.person@example.test private resume body")


class ProfileIntakeBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wahojobs-intake-browser-")
        self.path = Path(self.temp.name) / "intake.sqlite"
        writer = install_browser_authentication_database(self.path)
        self.session = seed_browser_session(writer, suffix="91")
        writer.close()
        self.now = REQUEST_AT
        self.monotonic = 100.0
        self.tokens = _TokenFactory()
        self.adapter = _RecordingAdapter()
        self.integration = self._build(self.adapter)

    def tearDown(self):
        self.integration.close()
        self.temp.cleanup()

    def _build(self, adapter):
        authentication = DurableBrowserSessionAuthenticationGateway(
            trusted_environment_namespace="private_beta",
            clock=lambda: self.now,
        )
        authorization = DurablePersistentProfileReadAuthorizationGateway()
        authority = ProfileIntakeAuthorityService(
            authentication_gateway=authentication,
            authorization_gateway=authorization,
            read_connection_provider=_ReadProvider(self.path),
            clock=lambda: self.now,
        )
        vault = IntakeDraftVault(
            monotonic=lambda: self.monotonic,
            token_factory=self.tokens,
        )
        processing = ProfileIntakeProcessingService(
            adapter=adapter,
            vault=vault,
            clock=lambda: self.now,
        )
        integration = ProfileIntakeBrowserIntegration(
            authority,
            processing,
            public_origin=PUBLIC_ORIGIN,
        )
        self.assertTrue(integration.activate())
        return integration

    def _headers(
        self,
        *,
        session=None,
        origin=True,
        content_type=None,
        content_length=None,
    ):
        session = session or self.session
        values = [
            ("Host", PUBLIC_AUTHORITY),
            (
                "Cookie",
                f"wahojobs_session={session['session_token']}; "
                f"__Host-wahojobs_session_csrf={session['csrf_secret']}",
            ),
        ]
        if origin is True:
            values.extend((("Origin", PUBLIC_ORIGIN), ("Sec-Fetch-Site", "same-origin")))
        elif type(origin) is str:
            values.append(("Origin", origin))
        if content_type is not None:
            values.append(("Content-Type", content_type))
        if content_length is not None:
            values.append(("Content-Length", str(content_length)))
        return tuple(values)

    def _multipart(self, document, *, kind="resume", mime=None, csrf=None, duplicate=False):
        boundary = "WahoJobsSyntheticBoundary91"
        if mime is None:
            mime = (
                "application/pdf"
                if b"%PDF-" in document[:1024]
                else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )
        csrf = csrf or profile_intake_csrf_proof(self.session["csrf_secret"], "upload")
        pieces = []

        def part(headers, value):
            pieces.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    headers,
                    b"\r\n\r\n",
                    value,
                    b"\r\n",
                ]
            )

        part(b'Content-Disposition: form-data; name="csrf"', csrf.encode())
        role = "linkedin_profile_export" if kind == "linkedin_profile_export" else "resume"
        file_headers = (
            f'Content-Disposition: form-data; name="{role}"; filename="synthetic.bin"\r\n'.encode()
            + b"Content-Type: "
            + mime.encode()
        )
        part(file_headers, document)
        if duplicate:
            part(file_headers, document)
        pieces.append(f"--{boundary}--\r\n".encode())
        body = b"".join(pieces)
        headers = self._headers(
            content_type=f"multipart/form-data; boundary={boundary}",
            content_length=len(body),
        )
        return headers, body

    def _upload(self, document, **kwargs):
        headers, body = self._multipart(document, **kwargs)
        return self.integration.handle(
            "POST",
            PROFILE_INTAKE_ROUTE,
            headers,
            BytesIO(body),
        )

    def _grant(self, session=None):
        session = session or self.session
        outcome = self.integration._authority.authorize(
            method="GET",
            route=PROFILE_INTAKE_REVIEW_ROUTE,
            authentication_input=self._headers(session=session, origin=False),
            session_token=session["session_token"],
            csrf_secret=session["csrf_secret"],
        )
        self.assertEqual(outcome.state, "authorized")
        return outcome.grant_for_service()

    def _reference(self, response):
        location = dict(response.headers)["Location"]
        return parse_qs(urlsplit(location).query)["draft"][0]

    def test_upload_authentication_origin_and_csrf_fail_closed(self):
        response = self.integration.handle(
            "GET",
            PROFILE_INTAKE_ROUTE,
            (("Host", PUBLIC_AUTHORITY),),
        )
        self.assertEqual(response.status, 401)
        invalid = dict(self.session)
        invalid["session_token"] = "X" * 43
        response = self.integration.handle(
            "GET", PROFILE_INTAKE_ROUTE, self._headers(session=invalid, origin=False)
        )
        self.assertEqual(response.status, 401)

        document = _docx_bytes(paragraphs=("Synthetic software engineer profile",))
        headers, body = self._multipart(document)
        wrong_origin = tuple(
            (name, "https://attacker.example" if name == "Origin" else value)
            for name, value in headers
        )
        self.assertEqual(
            self.integration.handle("POST", PROFILE_INTAKE_ROUTE, wrong_origin, BytesIO(body)).status,
            403,
        )
        response = self._upload(document, csrf="Z" * 43)
        self.assertEqual(response.status, 403)
        self.assertEqual(len(self.integration._processing.vault._records), 0)

    def test_account_principal_and_current_ownership_lineage_are_revalidated(self):
        writer = sqlite3.connect(self.path)
        writer.row_factory = sqlite3.Row
        writer.execute("PRAGMA foreign_keys = ON")
        try:
            other = seed_browser_session(writer, suffix="92")
        finally:
            writer.close()
        mismatch = self.integration._authority.authorize(
            method="GET",
            route=PROFILE_INTAKE_ROUTE,
            authentication_input=self._headers(session=other, origin=False),
            session_token=self.session["session_token"],
            csrf_secret=self.session["csrf_secret"],
        )
        self.assertEqual(mismatch.state, "unavailable")

        document = _docx_bytes(paragraphs=("Synthetic Software Engineer Example Systems",))
        reference = self._reference(self._upload(document))
        writer = sqlite3.connect(self.path)
        writer.row_factory = sqlite3.Row
        writer.execute("PRAGMA foreign_keys = ON")
        try:
            transition_binding(writer, self.session, "suspended")
        finally:
            writer.close()
        response = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE + "?draft=" + reference,
            self._headers(origin=False),
        )
        self.assertIn(response.status, {404, 503})
        self.assertNotIn(self.session["principal_id"].encode(), response.body)

    def test_upload_page_is_private_and_preserves_manual_path(self):
        response = self.integration.handle(
            "GET", PROFILE_INTAKE_ROUTE, self._headers(origin=False)
        )
        self.assertEqual(response.status, 200)
        headers = dict(response.headers)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Robots-Tag"], "noindex, nofollow")
        self.assertIn(b"Start with what you already have", response.body)
        self.assertIn(b"Choose one or add both", response.body)
        self.assertIn(b"Either option works on its own", response.body)
        self.assertEqual(response.body.count(b"type='file'"), 2)
        self.assertIn(b"aria-label='Documents to use for your profile'", response.body)
        self.assertIn(b"href='/find-matches'", response.body)
        self.assertIn(b"Create it manually instead", response.body)
        self.assertIn(b"LinkedIn profile PDF", response.body)
        self.assertIn(b"Building your profile", response.body)
        self.assertIn(b"No profile is created before you review and confirm it", response.body)
        self.assertIn(b"role='status'", response.body)
        self.assertIn(b"aria-live='polite'", response.body)
        self.assertIn(b"aria-atomic='true'", response.body)
        self.assertIn(b"Reading your documents", response.body)
        self.assertIn(b"Organizing experience and skills", response.body)
        self.assertIn(b"Preparing your review", response.body)
        self.assertIn(b"setAttribute('aria-busy','true')", response.body)
        self.assertNotIn(b"percent", response.body.lower())
        self.assertIn(b"@media (max-width: 700px)", response.body)
        self.assertIn(b"@media (prefers-reduced-motion: reduce)", response.body)
        self.assertIn("script-src 'sha256-", headers["Content-Security-Policy"])
        self.assertNotIn(self.session["account_id"].encode(), response.body)

        content, status = render_persistent_profile_page(
            PersistentProfilePageResult("empty"),
            intake_enabled=True,
        )
        self.assertEqual(status, 200)
        self.assertIn("/account/profile/intake", content)
        self.assertIn("/find-matches", content)
        self.assertIn("Build my profile from documents", content)
        self.assertIn("Create profile manually", content)
        self.assertIn(
            "class='primary-link profile-entry-primary' href='/account/profile/intake'",
            content,
        )
        self.assertIn(
            "class='secondary-link' href='/find-matches'",
            content,
        )
        self.assertNotIn("persistent profile", content.lower())
        self.assertLess(
            content.index("Build my profile from documents"),
            content.index("Create profile manually"),
        )

    def test_existing_profile_state_routes_to_profile_and_matches_without_restart_copy(self):
        response = _failure("existing_profile")
        self.assertEqual(response.status, 409)
        self.assertIn(b"Your profile is already set up", response.body)
        self.assertIn(b"href='/account/profile'>View my profile</a>", response.body)
        self.assertIn(b"href='/find-matches'>See my matches</a>", response.body)
        self.assertNotIn(b"Start again", response.body)
        self.assertNotIn(b"Create manually", response.body)

    def test_every_single_choice_classification_has_accessible_definition(self):
        for field_path, spec in _FIELD_SPECS.items():
            if spec.kind != "enum" or spec.multiple:
                continue
            with self.subTest(field_path=field_path):
                self.assertLessEqual(spec.allowed, set(_CLASSIFICATION_DESCRIPTIONS))

    def test_insufficient_classification_choice_is_clear_and_preserves_raw_value(self):
        fact = SimpleNamespace(
            field_path="education.completion_status",
            suggested=True,
            decision="pending",
            conflict_group=None,
        )
        for raw_value in ("not_specified", "unknown"):
            with self.subTest(raw_value=raw_value):
                markup = _review_fact_value_control(
                    7,
                    fact,
                    raw_value,
                    "Education Status",
                )
                self.assertEqual(markup.count("<strong>Not enough information</strong>"), 1)
                self.assertIn(
                    f"name='fact_7_value' value='{raw_value}'",
                    markup,
                )
                self.assertNotIn("<strong>Not Specified</strong>", markup)
                self.assertNotIn("<strong>Unknown</strong>", markup)

    def test_classification_choice_confirms_or_leaves_out_without_contract_change(self):
        response = self._upload(
            _docx_bytes(paragraphs=("Synthetic Senior Software Engineer",))
        )
        reference = self._reference(response)
        target = PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference})
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        index = next(
            index
            for index, fact in enumerate(snapshot.review.facts)
            if fact.field_path == "experience.seniority"
        )
        fact = snapshot.review.facts[index]
        self.assertTrue(fact.suggested)
        self.assertEqual(fact.decision, "pending")

        page = self.integration.handle("GET", target, self._headers(origin=False))
        self.assertEqual(page.status, 200)
        self.assertIn(
            f"type='hidden' name='fact_{index}_decision' value='accept'".encode(),
            page.body,
        )
        self.assertNotIn(
            f"<select name='fact_{index}_decision'>".encode(), page.body
        )
        self.assertIn(b"Leave this suggestion out", page.body)
        self.assertIn(b"More classifications", page.body)
        self.assertIn(b"class='suggestion-tag'>Suggested", page.body)
        self.assertEqual(page.body.count(b"<strong>Entry-level</strong>"), 1)
        self.assertEqual(page.body.count(b"<strong>Mid-level</strong>"), 1)
        self.assertIn(b"<strong>Advanced specialist</strong>", page.body)
        self.assertIn(b"<strong>Senior</strong>", page.body)
        self.assertIn(
            f"name='fact_{index}_value' value='entry-level'".encode(), page.body
        )
        self.assertNotIn(
            f"name='fact_{index}_value' value='junior'".encode(), page.body
        )
        self.assertIn(f"name='fact_{index}_value' value='mid'".encode(), page.body)
        self.assertNotIn(
            f"name='fact_{index}_value' value='mid-level'".encode(), page.body
        )

        for legacy_value, expected_label in (
            ("junior", "Entry-level"),
            ("mid-level", "Mid-level"),
        ):
            grouped = _review_fact_value_control(
                index, fact, legacy_value, "Seniority"
            )
            self.assertEqual(grouped.count(f"<strong>{expected_label}</strong>"), 1)
            self.assertIn(
                f"name='fact_{index}_value' value='{legacy_value}'", grouped
            )
            self.assertIn("class='suggestion-tag'>Suggested", grouped)

        def submission(current, *, value, decision):
            form = {
                "action": "update",
                "version": str(current.version),
                "csrf": profile_intake_csrf_proof(
                    self.session["csrf_secret"],
                    "update",
                    draft_reference=reference,
                    version=current.version,
                ),
            }
            for fact_index, current_fact in enumerate(current.review.facts):
                form[f"fact_{fact_index}_value"] = (
                    value
                    if fact_index == index
                    else review_value_for_form(current_fact.value)
                )
                form[f"fact_{fact_index}_decision"] = (
                    decision
                    if fact_index == index
                    else ("keep" if not current_fact.suggested else "accept")
                )
            for name in current.review.missing_user_fields:
                form["missing_" + name] = dict(current.review.user_inputs).get(
                    name, ""
                )
            form.update(
                _preference_form_values_for_model(
                    current.review.preference_model
                )
            )
            body = urlencode(form).encode()
            return self.integration.handle(
                "POST",
                target,
                self._headers(
                    content_type="application/x-www-form-urlencoded",
                    content_length=len(body),
                ),
                BytesIO(body),
            )

        accepted = submission(snapshot, value="lead", decision="accept")
        self.assertEqual(accepted.status, 303)
        accepted_snapshot = self.integration._processing.vault.get(
            reference, self._grant()
        )
        self.assertEqual(accepted_snapshot.review.facts[index].value, "lead")
        self.assertEqual(accepted_snapshot.review.facts[index].decision, "accept")

        left_out = submission(
            accepted_snapshot,
            value=_SKIP_SUGGESTION_VALUE,
            decision="accept",
        )
        self.assertEqual(left_out.status, 303)
        rejected_snapshot = self.integration._processing.vault.get(
            reference, self._grant()
        )
        self.assertEqual(rejected_snapshot.review.facts[index].value, "lead")
        self.assertEqual(rejected_snapshot.review.facts[index].decision, "reject")

        explicit_index = next(
            fact_index
            for fact_index, current_fact in enumerate(rejected_snapshot.review.facts)
            if not current_fact.suggested
        )
        invalid_form = {
            "action": "update",
            "version": str(rejected_snapshot.version),
            "csrf": profile_intake_csrf_proof(
                self.session["csrf_secret"],
                "update",
                draft_reference=reference,
                version=rejected_snapshot.version,
            ),
        }
        for fact_index, current_fact in enumerate(rejected_snapshot.review.facts):
            invalid_form[f"fact_{fact_index}_value"] = (
                _SKIP_SUGGESTION_VALUE
                if fact_index == explicit_index
                else review_value_for_form(current_fact.value)
            )
            invalid_form[f"fact_{fact_index}_decision"] = current_fact.decision
        for name in rejected_snapshot.review.missing_user_fields:
            invalid_form["missing_" + name] = dict(
                rejected_snapshot.review.user_inputs
            ).get(name, "")
        invalid_form.update(
            _preference_form_values_for_model(
                rejected_snapshot.review.preference_model
            )
        )
        invalid_body = urlencode(invalid_form).encode()
        invalid = self.integration.handle(
            "POST",
            target,
            self._headers(
                content_type="application/x-www-form-urlencoded",
                content_length=len(invalid_body),
            ),
            BytesIO(invalid_body),
        )
        self.assertEqual(invalid.status, 400)

    def test_oversized_content_length_is_rejected_before_body_read(self):
        headers = self._headers(
            content_type="multipart/form-data; boundary=x",
            content_length=MAX_MULTIPART_BODY_BYTES + 1,
        )
        response = self.integration.handle(
            "POST", PROFILE_INTAKE_ROUTE, headers, _NoReadStream()
        )
        self.assertEqual(response.status, 413)
        self.assertEqual(self.adapter.calls, [])

        short_headers = self._headers(
            content_type="multipart/form-data; boundary=x",
            content_length=128,
        )
        self.assertEqual(
            self.integration.handle(
                "POST", PROFILE_INTAKE_ROUTE, short_headers, _OverreadStream()
            ).status,
            400,
        )

    def test_malformed_duplicate_and_mime_mismatch_are_rejected(self):
        malformed = b"not multipart"
        headers = self._headers(
            content_type="multipart/form-data; boundary=synthetic",
            content_length=len(malformed),
        )
        self.assertEqual(
            self.integration.handle("POST", PROFILE_INTAKE_ROUTE, headers, BytesIO(malformed)).status,
            400,
        )
        document = _docx_bytes(paragraphs=("Synthetic software engineer profile",))
        self.assertEqual(self._upload(document, duplicate=True).status, 400)
        self.assertEqual(self._upload(document, mime="application/pdf").status, 415)
        self.assertEqual(self._upload(document, kind="linkedin_profile_export").status, 415)
        self.assertEqual(self.adapter.calls, [])

    def test_pdf_docx_and_linkedin_pdf_success_use_minimized_model_evidence(self):
        docx = _docx_bytes(
            paragraphs=(
                "Email: synthetic.person@example.test",
                "Software Engineer at Example Systems, 2019-2024",
            )
        )
        pdf = _pdf_bytes("Synthetic Software Engineer at Example Systems 2019-2024")
        with mock.patch.object(
            socket.socket,
            "connect",
            side_effect=AssertionError("ordinary intake tests must not use network"),
        ):
            responses = (
                self._upload(docx),
                self._upload(pdf),
                self._upload(pdf, kind="linkedin_profile_export"),
            )
        self.assertTrue(all(response.status == 303 for response in responses))
        self.assertEqual(len(self.adapter.calls), 3)
        self.assertNotIn("synthetic.person@example.test", self.adapter.calls[0].blocks[0].text)
        self.assertTrue(all(type(call) is ModelEvidencePacket for call in self.adapter.calls))
        snapshot = self.integration._processing.vault.get(
            self._reference(responses[-1]), self._grant()
        )
        self.assertEqual(snapshot.document.origin, "linkedin_profile_export")
        self.assertEqual(snapshot.document.format, "pdf")

    def test_failed_parse_or_adapter_creates_no_draft_and_no_profile_write(self):
        before = self._profile_counts()
        self.assertEqual(self._upload(b"not a document").status, 415)
        self.assertEqual(len(self.integration._processing.vault._records), 0)
        failing = self._build(_RecordingAdapter(fail=True))
        try:
            document = _docx_bytes(paragraphs=("Synthetic software engineer profile",))
            headers, body = self._multipart(document)
            response = failing.handle("POST", PROFILE_INTAKE_ROUTE, headers, BytesIO(body))
            self.assertEqual(response.status, 503)
            self.assertEqual(len(failing._processing.vault._records), 0)
        finally:
            failing.close()
        exploding = self._build(_ExplodingAdapter())
        try:
            headers, body = self._multipart(document)
            response = exploding.handle(
                "POST", PROFILE_INTAKE_ROUTE, headers, BytesIO(body)
            )
            self.assertEqual(response.status, 503)
            self.assertNotIn(b"synthetic.person@example.test", response.body)
            self.assertNotIn(b"private resume body", response.body)
            self.assertEqual(len(exploding._processing.vault._records), 0)
        finally:
            exploding.close()
        self.assertEqual(self._profile_counts(), before)

    def test_one_lineage_cannot_generate_concurrently(self):
        adapter = _BlockingAdapter()
        service = ProfileIntakeProcessingService(
            adapter=adapter,
            vault=IntakeDraftVault(
                monotonic=lambda: self.monotonic,
                token_factory=self.tokens,
            ),
            clock=lambda: self.now,
        )
        grant = self._grant()
        document = _docx_bytes(paragraphs=("Synthetic Software Engineer Example Systems",))
        errors = []

        def first():
            try:
                service.process(
                    grant,
                    document,
                    document_kind=DocumentKind.RESUME,
                    document_format=DocumentFormat.DOCX,
                )
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=first)
        worker.start()
        self.assertTrue(adapter.entered.wait(5))
        with self.assertRaisesRegex(ProfileIntakeError, "profile_intake_in_flight"):
            service.process(
                grant,
                document,
                document_kind=DocumentKind.RESUME,
                document_format=DocumentFormat.DOCX,
            )
        adapter.release.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

    def test_valid_generation_creates_one_minimized_bound_draft(self):
        document = _docx_bytes(paragraphs=("Synthetic Software Engineer Example Systems",))
        response = self._upload(document)
        self.assertEqual(response.status, 303)
        self.assertEqual(len(self.integration._processing.vault._records), 1)
        record = next(iter(self.integration._processing.vault._records.values()))
        snapshot = record.snapshot
        names = set(snapshot.__dataclass_fields__)
        self.assertEqual(
            names,
            {
                "review",
                "review_step",
                "document",
                "diagnostics",
                "created_at",
                "expires_at_monotonic",
                "absolute_expires_at_monotonic",
                "last_renewed_at_monotonic",
                "version",
            },
        )
        rendered = repr(snapshot)
        self.assertNotIn("Synthetic Software", rendered)
        self.assertNotIn("evidence", names)
        self.assertNotIn("binary", names)
        self.assertNotIn("profile_id", repr(snapshot.review))
        self.assertNotIn("matcher", repr(snapshot.review))

    def test_vault_binding_expiry_capacity_unknown_and_cancellation(self):
        document = _docx_bytes(paragraphs=("Synthetic Software Engineer Example Systems",))
        response = self._upload(document)
        reference = self._reference(response)
        grant = self._grant()
        original_snapshot = self.integration._processing.vault.get(reference, grant)
        self.assertRegex(reference, r"^[A-Za-z0-9_-]{43}$")
        self.assertIsNotNone(original_snapshot)
        self.assertIsNone(self.integration._processing.vault.get("X" * 43, grant))

        writer = sqlite3.connect(self.path)
        writer.row_factory = sqlite3.Row
        writer.execute("PRAGMA foreign_keys = ON")
        try:
            other = accounts.create_session(
                writer,
                user_id=self.session["account_id"],
                idle_ttl=timedelta(hours=2),
                absolute_ttl=timedelta(days=1),
                idempotency_key="intake-second-browser-session",
                now=AUTHENTICATED_AT,
            )
            writer.commit()
        finally:
            writer.close()
        other_session = dict(self.session)
        other_session.update(
            session_id=other.session.session_id,
            session_token=other.session_token,
            csrf_secret=other.csrf_secret,
        )
        other_grant = self._grant(other_session)
        self.assertIsNone(self.integration._processing.vault.get(reference, other_grant))

        tiny = IntakeDraftVault(
            monotonic=lambda: self.monotonic,
            token_factory=self.tokens,
            capacity=1,
        )
        tiny.issue(
            grant,
            original_snapshot.review,
            original_snapshot.document,
            original_snapshot.diagnostics,
            created_at=self.now,
        )
        with self.assertRaisesRegex(ProfileIntakeError, "draft_vault_capacity"):
            tiny.issue(
                other_grant,
                original_snapshot.review,
                original_snapshot.document,
                original_snapshot.diagnostics,
                created_at=self.now,
            )
        tiny.close()
        self.assertTrue(tiny.closed)
        self.monotonic += 1801
        self.assertIsNone(self.integration._processing.vault.get(reference, grant))

    def test_review_lifetime_is_sliding_rate_limited_bound_and_absolute_capped(self):
        document = _docx_bytes(
            paragraphs=("Synthetic Software Engineer Example Systems",)
        )
        reference = self._reference(self._upload(document))
        grant = self._grant()
        issued = self.integration._processing.vault.get(reference, grant)
        self.assertEqual(issued.expires_at_monotonic - self.monotonic, 1800)
        self.assertEqual(
            issued.absolute_expires_at_monotonic - self.monotonic,
            7200,
        )

        self.monotonic += 299
        state, limited = self.integration._processing.renew(
            reference,
            grant,
            expected_version=issued.version,
        )
        self.assertEqual(state, "rate_limited")
        self.assertEqual(limited[0].expires_at_monotonic, issued.expires_at_monotonic)

        self.monotonic += 1
        state, renewed = self.integration._processing.renew(
            reference,
            grant,
            expected_version=issued.version,
        )
        self.assertEqual(state, "renewed")
        self.assertEqual(renewed[1:], (1800, 6900))
        renewed_snapshot = renewed[0]
        self.assertGreater(
            renewed_snapshot.expires_at_monotonic,
            issued.expires_at_monotonic,
        )

        self.monotonic = 100.0 + 601
        self.assertIsNotNone(
            self.integration._processing.vault.get(reference, grant)
        )
        previous_expiry = renewed_snapshot.expires_at_monotonic
        state, updated = self.integration._processing.update(
            reference,
            grant,
            expected_version=renewed_snapshot.version,
            review=renewed_snapshot.review,
        )
        self.assertEqual(state, "updated")
        self.assertGreater(updated.expires_at_monotonic, previous_expiry)
        self.assertEqual(updated.version, 2)

        for elapsed in range(900, 7200, 300):
            self.monotonic = 100.0 + elapsed
            state, value = self.integration._processing.renew(
                reference,
                grant,
                expected_version=2,
            )
            self.assertIn(state, {"renewed", "rate_limited"})
            self.assertLessEqual(
                value[0].expires_at_monotonic,
                value[0].absolute_expires_at_monotonic,
            )
        self.monotonic = 100.0 + 7200
        self.assertIsNone(self.integration._processing.vault.get(reference, grant))

    def test_review_renewal_endpoint_is_content_free_same_origin_and_version_bound(self):
        document = _docx_bytes(
            paragraphs=("Synthetic Software Engineer Example Systems",)
        )
        reference = self._reference(self._upload(document))
        target = PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference})
        page = self.integration.handle("GET", target, self._headers(origin=False))
        self.assertEqual(page.status, 200)
        policy = dict(page.headers)["Content-Security-Policy"]
        self.assertIn("connect-src 'self'", policy)
        self.assertIn(b"id='profile-review-renewal'", page.body)
        self.assertIn(b"document.visibilityState!=='visible'", page.body)
        self.assertIn(b"aria-live='polite'", page.body)
        self.assertNotIn(b"document_reference", page.body)

        self.monotonic += 301
        form = {
            "action": "renew",
            "version": "1",
            "csrf": profile_intake_csrf_proof(
                self.session["csrf_secret"],
                "renew",
                draft_reference=reference,
                version=1,
            ),
        }

        def request(values, *, origin=True, session=None):
            body = urlencode(values).encode()
            return self.integration.handle(
                "POST",
                target,
                self._headers(
                    origin=origin,
                    session=session,
                    content_type="application/x-www-form-urlencoded",
                    content_length=len(body),
                ),
                BytesIO(body),
            )

        renewed = request(form)
        self.assertEqual(renewed.status, 204)
        renewal_headers = dict(renewed.headers)
        self.assertEqual(renewal_headers["X-Wahojobs-Review-Idle-Seconds"], "1800")
        self.assertLessEqual(
            int(renewal_headers["X-Wahojobs-Review-Absolute-Seconds"]),
            7200,
        )
        self.assertEqual(renewed.body, b"")
        self.assertEqual(request(form).status, 204)
        self.assertEqual(request(form, origin=False).status, 403)
        self.assertEqual(request({**form, "csrf": "Z" * 43}).status, 403)
        self.assertEqual(request({**form, "unexpected": "1"}).status, 400)

        snapshot = self.integration._processing.vault.get(reference, self._grant())
        self.integration._processing.update(
            reference,
            self._grant(),
            expected_version=1,
            review=snapshot.review,
        )
        self.assertEqual(request(form).status, 409)

        writer = sqlite3.connect(self.path)
        writer.row_factory = sqlite3.Row
        writer.execute("PRAGMA foreign_keys = ON")
        try:
            other = seed_browser_session(writer, suffix="93")
        finally:
            writer.close()
        other_form = {
            "action": "renew",
            "version": "2",
            "csrf": profile_intake_csrf_proof(
                other["csrf_secret"],
                "renew",
                draft_reference=reference,
                version=2,
            ),
        }
        self.assertEqual(request(other_form, session=other).status, 410)

    def test_review_is_editable_replay_safe_and_cancelled_without_save(self):
        before = self._profile_counts()
        document = _docx_bytes(paragraphs=("Synthetic Software Engineer Example Systems",))
        response = self._upload(document)
        reference = self._reference(response)
        review_target = PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference})
        page = self.integration.handle("GET", review_target, self._headers(origin=False))
        self.assertEqual(page.status, 200)
        self.assertIn(b"Found in your resume", page.body)
        self.assertIn(b"Confirm our suggestions", page.body)
        self.assertIn(b"A few details only you can answer", page.body)
        self.assertIn(b"aria-label='Profile review steps'", page.body)
        self.assertIn(b"href='#review-found'", page.body)
        self.assertIn(b"href='#review-suggestions'", page.body)
        self.assertIn(b"href='#review-preferences'", page.body)
        self.assertIn(b"href='#review-finish'", page.body)
        self.assertIn(b"aria-labelledby='review-found-title'", page.body)
        self.assertIn(b"aria-labelledby='review-suggestions-title'", page.body)
        self.assertIn(b"aria-labelledby='review-preferences-title'", page.body)
        self.assertIn(b"aria-labelledby='review-finish-title'", page.body)
        self.assertIn(b"Leave this suggestion out", page.body)
        self.assertIn(b"data-review-collection='skills'", page.body)
        self.assertIn(b"Add another skill", page.body)
        self.assertIn(b"class='review-collection-item skill-token", page.body)
        self.assertIn(b"aria-label='Remove this skill'", page.body)
        self.assertIn(b"Keep item", page.body)
        self.assertNotIn(b"Use this suggestion?", page.body)
        self.assertNotIn(b"Document-supported prefill", page.body)
        self.assertNotIn(b"matcher decisions", page.body)
        self.assertNotIn(b"confidence", page.body.lower())
        self.assertNotIn(b"Save profile", page.body)

        grant = self._grant()
        snapshot = self.integration._processing.vault.get(reference, grant)
        form = {
            "action": "update",
            "version": str(snapshot.version),
            "csrf": profile_intake_csrf_proof(
                self.session["csrf_secret"], "update", draft_reference=reference, version=snapshot.version
            ),
        }
        for index, fact in enumerate(snapshot.review.facts):
            form[f"fact_{index}_value"] = (
                "Platform Engineer" if index == 0 else review_value_for_form(fact.value)
            )
            form[f"fact_{index}_decision"] = "keep" if not fact.suggested else "accept"
        for name in snapshot.review.missing_user_fields:
            form["missing_" + name] = ""
        form.update(_preference_form_values_for_model(snapshot.review.preference_model))
        encoded = urlencode(form).encode()
        headers = self._headers(
            content_type="application/x-www-form-urlencoded",
            content_length=len(encoded),
        )
        updated = self.integration.handle("POST", review_target, headers, BytesIO(encoded))
        self.assertEqual(updated.status, 303)
        new_snapshot = self.integration._processing.vault.get(reference, grant)
        self.assertEqual(new_snapshot.version, 2)
        self.assertEqual(new_snapshot.review.facts[0].value, "Platform Engineer")
        self.assertEqual(new_snapshot.review.facts[1].decision, "accept")
        self.assertNotIn("remote", dict(new_snapshot.review.user_inputs))
        self.assertEqual(
            new_snapshot.review.preference_model["compensation"]["minimum_kind"],
            "none",
        )
        self.assertEqual(
            self.integration.handle("POST", review_target, headers, BytesIO(encoded)).status,
            409,
        )

        cancel = urlencode(
            {
                "action": "cancel",
                "version": "2",
                "csrf": profile_intake_csrf_proof(
                    self.session["csrf_secret"], "cancel", draft_reference=reference, version=2
                ),
            }
        ).encode()
        cancel_headers = self._headers(
            content_type="application/x-www-form-urlencoded",
            content_length=len(cancel),
        )
        cancelled = self.integration.handle("POST", review_target, cancel_headers, BytesIO(cancel))
        self.assertEqual(cancelled.status, 303)
        self.assertEqual(dict(cancelled.headers)["Location"], "/account/profile")
        self.assertEqual(
            self.integration.handle("GET", review_target, self._headers(origin=False)).status,
            410,
        )
        self.assertEqual(self._profile_counts(), before)

    def test_structured_preferences_are_accessible_multiselect_and_fail_closed(self):
        document = _docx_bytes(paragraphs=("Synthetic Software Engineer Example Systems",))
        response = self._upload(document)
        reference = self._reference(response)
        target = PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference})
        page = self.integration.handle("GET", target, self._headers(origin=False))
        self.assertEqual(page.status, 200)
        self.assertIn(b"What are you looking for?", page.body)
        self.assertIn(b"type='checkbox'", page.body)
        self.assertIn(b"type='radio'", page.body)
        self.assertIn(b"class='preference-disclosure", page.body)
        self.assertIn(b"class='choice-definitions-disclosure'", page.body)
        self.assertIn(b"aria-describedby=", page.body)
        self.assertIn(b"Choose all that apply", page.body)
        self.assertIn(b"Each group is separate", page.body)
        self.assertIn(b"Leave a group blank", page.body)
        self.assertNotIn(b"Leave every option blank", page.body)
        self.assertIn(b"More work preferences", page.body)
        self.assertIn(b"More job areas <span>(16)</span>", page.body)
        self.assertIn(b"A few details only you can answer", page.body)
        self.assertIn(b"Independent contractor / freelance", page.body)
        self.assertIn(b"Preferred minimum", page.body)
        self.assertIn(b"Strict minimum", page.body)
        self.assertNotIn(b"compensation-guide", page.body)
        self.assertEqual(page.body.count(b"Your target. You may choose to lower it"), 1)
        self.assertEqual(page.body.count(b"Your firm floor. Wahojobs will not suggest"), 1)
        self.assertIn(b"class='choice-more-selected'>Selections made", page.body)
        self.assertIn(b"class='disclosure-selection-state'>Selections made", page.body)
        self.assertIn(b".job-interest-group > .choice-grid", page.body)
        self.assertIn(b":has(input:checked)", page.body)
        self.assertIn(b"summary:focus-visible", page.body)
        self.assertIn(b"optgroup label='Common currencies'", page.body)
        self.assertIn(b"optgroup label='All other currencies'", page.body)
        rendered_currencies = {
            item.decode("ascii")
            for item in re.findall(rb"<option value='([A-Z]{3})'", page.body)
        }
        self.assertEqual(rendered_currencies, ISO_4217_CURRENCIES)
        self.assertEqual(
            len(re.findall(rb"<option value='[A-Z]{3}'", page.body)),
            len(ISO_4217_CURRENCIES),
        )
        catalog = profile_preference_control_catalog_v1()
        for dimension in catalog["dimensions"]:
            for choice in dimension["choices"]:
                field_name = (
                    "preference_"
                    + "_".join((*dimension["path"], choice["code"]))
                )
                self.assertIn(f"name='{field_name}'".encode("ascii"), page.body)
        self.assertNotIn(b"missing_employment_types", page.body)
        self.assertNotIn(b"onmouseover", page.body.lower())
        for technical_label in (
            b"Eligible Countries",
            b"Geographic Restrictions",
            b"Hard Constraints",
            b"Soft Preferences",
            b"Avoid Keywords",
            b"non-relaxable",
            b"non-comparable",
        ):
            self.assertNotIn(technical_label.lower(), page.body.lower())
        self.assertIn(b"Where can you work?", page.body)
        self.assertIn(b"Anything you cannot do?", page.body)

        snapshot = self.integration._processing.vault.get(reference, self._grant())
        model = empty_profile_preferences_v1()
        model["employment_relationships"] = ["employee", "independent_contractor"]
        model["workloads"] = ["full_time", "part_time"]
        model["engagement_terms"] = ["fixed_term"]
        model["schedule"]["coordination_modes"] = ["asynchronous", "synchronous"]
        model["accepted_phone_voice_modes"] = ["non_phone", "phone"]
        model["job_interests"] = ["customer_support", "software_engineering"]
        model["accepted_career_levels"] = ["entry", "senior"]
        model["compensation"] = {
            "minimum_kind": "strict",
            "amount": "5000.00",
            "currency": "brl",
            "period": "month",
        }
        form = {
            "action": "update",
            "version": str(snapshot.version),
            "csrf": profile_intake_csrf_proof(
                self.session["csrf_secret"],
                "update",
                draft_reference=reference,
                version=snapshot.version,
            ),
        }
        for index, fact in enumerate(snapshot.review.facts):
            form[f"fact_{index}_value"] = review_value_for_form(fact.value)
            form[f"fact_{index}_decision"] = "keep" if not fact.suggested else "accept"
        for name in snapshot.review.missing_user_fields:
            form["missing_" + name] = ""
        form.update(_preference_form_values_for_model(model))
        body = urlencode(form).encode()
        updated = self.integration.handle(
            "POST",
            target,
            self._headers(
                content_type="application/x-www-form-urlencoded",
                content_length=len(body),
            ),
            BytesIO(body),
        )
        self.assertEqual(updated.status, 303)
        saved = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(
            saved.review.preference_model["employment_relationships"],
            ["employee", "independent_contractor"],
        )
        self.assertEqual(
            saved.review.preference_model["workloads"],
            ["full_time", "part_time"],
        )
        self.assertEqual(
            saved.review.preference_model["compensation"],
            {
                "minimum_kind": "strict",
                "amount": "5000",
                "currency": "BRL",
                "period": "month",
            },
        )
        refreshed = self.integration.handle(
            "GET", target, self._headers(origin=False)
        )
        self.assertEqual(refreshed.status, 200)
        self.assertIn(b"<strong>Selected:</strong> Employee, Independent contractor / freelance", refreshed.body)
        self.assertIn(
            b"class='preference-disclosure more-preference-disclosure' open",
            refreshed.body,
        )
        self.assertIn("Selections made — open to review".encode("utf-8"), refreshed.body)

        stale_form = dict(form)
        stale_form["version"] = str(saved.version)
        stale_form["csrf"] = profile_intake_csrf_proof(
            self.session["csrf_secret"],
            "update",
            draft_reference=reference,
            version=saved.version,
        )
        stale_form["preference_compensation_amount"] = ""
        bad_body = urlencode(stale_form).encode()
        rejected = self.integration.handle(
            "POST",
            target,
            self._headers(
                content_type="application/x-www-form-urlencoded",
                content_length=len(bad_body),
            ),
            BytesIO(bad_body),
        )
        self.assertEqual(rejected.status, 400)

        legacy_form = dict(stale_form)
        legacy_form["preference_compensation_amount"] = "5000"
        legacy_form["missing_employment_types"] = "freelance"
        bad_body = urlencode(legacy_form).encode()
        rejected = self.integration.handle(
            "POST",
            target,
            self._headers(
                content_type="application/x-www-form-urlencoded",
                content_length=len(bad_body),
            ),
            BytesIO(bad_body),
        )
        self.assertEqual(rejected.status, 400)

    def test_review_rejects_invalid_values_and_never_places_content_in_url(self):
        document = _docx_bytes(paragraphs=("Synthetic Software Engineer Example Systems",))
        response = self._upload(document)
        location = dict(response.headers)["Location"]
        self.assertNotIn("Software", location)
        self.assertNotIn("Example", location)
        reference = self._reference(response)
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        form = {
            "action": "update",
            "version": "1",
            "csrf": profile_intake_csrf_proof(
                self.session["csrf_secret"], "update", draft_reference=reference, version=1
            ),
        }
        for index, fact in enumerate(snapshot.review.facts):
            form[f"fact_{index}_value"] = "invalid" if fact.field_path == "experience.seniority" else review_value_for_form(fact.value)
            form[f"fact_{index}_decision"] = "keep" if not fact.suggested else "accept"
        for name in snapshot.review.missing_user_fields:
            form["missing_" + name] = ""
        form.update(_preference_form_values_for_model(snapshot.review.preference_model))
        body = urlencode(form).encode()
        response = self.integration.handle(
            "POST",
            PROFILE_INTAKE_REVIEW_ROUTE + "?draft=" + reference,
            self._headers(content_type="application/x-www-form-urlencoded", content_length=len(body)),
            BytesIO(body),
        )
        self.assertEqual(response.status, 400)

        for index, fact in enumerate(snapshot.review.facts):
            form[f"fact_{index}_value"] = review_value_for_form(fact.value)
        form["matcher_signals"] = "forbidden"
        body = urlencode(form).encode()
        response = self.integration.handle(
            "POST",
            PROFILE_INTAKE_REVIEW_ROUTE + "?draft=" + reference,
            self._headers(
                content_type="application/x-www-form-urlencoded",
                content_length=len(body),
            ),
            BytesIO(body),
        )
        self.assertEqual(response.status, 400)

    def _profile_counts(self):
        connection = sqlite3.connect(self.path)
        try:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "product_profiles",
                    "product_profile_revisions",
                    "product_profile_sources",
                )
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
