from __future__ import annotations

from io import BytesIO
from pathlib import Path
import json
import socket
import sqlite3
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

from tests.browser_session_authentication_test_support import (
    REQUEST_AT,
    install_browser_authentication_database,
    seed_browser_session,
)
from tests.test_profile_intake_browser import _ReadProvider, _TokenFactory
from tests.test_profile_intake_foundation import _docx_bytes, _pdf_bytes
from wahojobs.browser_session_authentication import (
    DurableBrowserSessionAuthenticationGateway,
)
from wahojobs.persistent_profile_read_authorization import (
    DurablePersistentProfileReadAuthorizationGateway,
)
from wahojobs.profile_intake.browser import (
    MAX_MULTIPART_BODY_BYTES,
    ProfileIntakeBrowserIntegration,
    _preference_form_values_for_model,
)
from wahojobs.profile_intake.contracts import (
    AI_EXTRACTION_SCHEMA_VERSION,
    DocumentFormat,
    DocumentKind,
    LanguageValue,
    ModelEvidenceBlock,
    ModelEvidencePacket,
    ProfileIntakeError,
    validate_ai_profile_extraction,
)
from wahojobs.profile_intake.review_draft import (
    ValidatedProfileSource,
    reconcile_profile_extractions,
)
from wahojobs.profile_intake.runtime import (
    PROFILE_INTAKE_REVIEW_ROUTE,
    PROFILE_INTAKE_ROUTE,
    IntakeDraftVault,
    ProfileIntakeAuthorityService,
    ProfileIntakeProcessingService,
    editable_profile_review,
    profile_intake_csrf_proof,
    review_value_for_form,
    update_editable_review,
)


PUBLIC_ORIGIN = "https://localhost:8443"
PUBLIC_AUTHORITY = "localhost:8443"
RESUME_REFERENCE = "doc_11111111111111111111111111111111"
LINKEDIN_REFERENCE = "doc_22222222222222222222222222222222"


def _raw_fact(path, value, *, explicit=True, confidence=0.9, block="b001"):
    return {
        "field_path": path,
        "value": value,
        "evidence_block_references": [block],
        "confidence": confidence,
        "explicit": explicit,
    }


def _validated_source(kind, reference, *facts):
    packet = ModelEvidencePacket(
        document_reference=reference,
        document_kind=kind,
        document_format=DocumentFormat.PDF,
        blocks=(ModelEvidenceBlock("b001", "Synthetic professional evidence"),),
    )
    payload_facts = []
    for fact in facts:
        item = dict(fact)
        item["source_document_reference"] = reference
        payload_facts.append(item)
    extraction = validate_ai_profile_extraction(
        {
            "schema_version": AI_EXTRACTION_SCHEMA_VERSION,
            "document_reference": reference,
            "facts": payload_facts,
        },
        packet,
    )
    return ValidatedProfileSource(document_kind=kind, extraction=extraction)


class _BundleAdapter:
    def __init__(self, facts_by_kind=None, *, fail_call=None):
        self.facts_by_kind = facts_by_kind or {}
        self.fail_call = fail_call
        self.calls = []

    def extract(self, evidence):
        if type(evidence) is not ModelEvidencePacket:
            raise AssertionError("raw evidence crossed the model boundary")
        self.calls.append(evidence)
        if self.fail_call == len(self.calls):
            raise ProfileIntakeError("openai_transport_error")
        facts = self.facts_by_kind.get(
            evidence.document_kind,
            (_raw_fact("experience.job_titles", "Software Engineer"),),
        )
        payload_facts = []
        for fact in facts:
            item = dict(fact)
            item["source_document_reference"] = evidence.document_reference
            payload_facts.append(item)
        return validate_ai_profile_extraction(
            {
                "schema_version": AI_EXTRACTION_SCHEMA_VERSION,
                "document_reference": evidence.document_reference,
                "facts": payload_facts,
            },
            evidence,
        )


class _NoReadStream:
    def read(self, _size=-1):
        raise AssertionError("oversized aggregate body must not be read")


class ReconciliationTests(unittest.TestCase):
    def test_identical_values_deduplicate_merge_sources_and_prefer_explicit_evidence(self):
        resume = _validated_source(
            DocumentKind.RESUME,
            RESUME_REFERENCE,
            _raw_fact("location.city", "Lisbon"),
            _raw_fact("skills.normalized", "Python"),
            _raw_fact("experience.job_titles", "Software Engineer", explicit=False),
        )
        linkedin = _validated_source(
            DocumentKind.LINKEDIN_PROFILE_EXPORT,
            LINKEDIN_REFERENCE,
            _raw_fact("location.city", "lisbon"),
            _raw_fact("skills.normalized", "python"),
            _raw_fact("experience.job_titles", "Software Engineer", explicit=True),
        )
        draft = reconcile_profile_extractions((resume, linkedin))
        facts = {
            (fact.field_path, str(fact.value).casefold()): fact
            for fact in (*draft.prefilled_facts, *draft.suggested_facts)
        }
        self.assertEqual(len([key for key in facts if key[0] == "location.city"]), 1)
        self.assertEqual(len([key for key in facts if key[0] == "skills.normalized"]), 1)
        title = facts[("experience.job_titles", "software engineer")]
        self.assertTrue(title.explicit)
        self.assertFalse(title.requires_confirmation)
        self.assertEqual(
            {item.document_kind for item in title.source_attributions},
            {DocumentKind.RESUME, DocumentKind.LINKEDIN_PROFILE_EXPORT},
        )
        self.assertEqual(
            {item.evidence_block_references for item in title.source_attributions},
            {("b001",)},
        )

    def test_complementary_lists_union_and_same_block_ids_remain_scoped(self):
        resume = _validated_source(
            DocumentKind.RESUME,
            RESUME_REFERENCE,
            _raw_fact("skills.normalized", "Python"),
        )
        linkedin = _validated_source(
            DocumentKind.LINKEDIN_PROFILE_EXPORT,
            LINKEDIN_REFERENCE,
            _raw_fact("skills.normalized", "SQL"),
        )
        draft = reconcile_profile_extractions((resume, linkedin))
        skills = [fact for fact in draft.prefilled_facts if fact.field_path == "skills.normalized"]
        self.assertEqual({fact.value for fact in skills}, {"Python", "SQL"})
        scoped = {
            (item.document_reference, item.evidence_block_references)
            for fact in skills
            for item in fact.source_attributions
        }
        self.assertEqual(
            scoped,
            {(RESUME_REFERENCE, ("b001",)), (LINKEDIN_REFERENCE, ("b001",))},
        )

    def test_singleton_and_language_qualifier_conflicts_require_confirmation(self):
        resume = _validated_source(
            DocumentKind.RESUME,
            RESUME_REFERENCE,
            _raw_fact("location.city", "Lisbon"),
            _raw_fact("experience.seniority", "senior", explicit=False),
            _raw_fact(
                "languages",
                {"language": "English", "proficiency": "fluent", "locale": None},
            ),
        )
        linkedin = _validated_source(
            DocumentKind.LINKEDIN_PROFILE_EXPORT,
            LINKEDIN_REFERENCE,
            _raw_fact("location.city", "Porto"),
            _raw_fact("experience.seniority", "mid", explicit=False),
            _raw_fact(
                "languages",
                {"language": "English", "proficiency": "basic", "locale": None},
            ),
        )
        draft = reconcile_profile_extractions((resume, linkedin))
        conflicts = [fact for fact in draft.suggested_facts if fact.conflict_group]
        self.assertEqual(
            {fact.conflict_group for fact in conflicts},
            {"location.city", "experience.seniority", "languages:english"},
        )
        kinds = {issue.kind for issue in draft.issues}
        self.assertEqual(
            kinds,
            {"conflicting_source_values", "conflicting_language_detail"},
        )

    def test_reconciliation_is_order_independent_and_preferences_remain_user_only(self):
        resume = _validated_source(
            DocumentKind.RESUME,
            RESUME_REFERENCE,
            _raw_fact("experience.recent_roles", "Remote Engineer"),
        )
        linkedin = _validated_source(
            DocumentKind.LINKEDIN_PROFILE_EXPORT,
            LINKEDIN_REFERENCE,
            _raw_fact("skills.normalized", "Python"),
        )
        forward = reconcile_profile_extractions((resume, linkedin))
        reverse = reconcile_profile_extractions((linkedin, resume))
        self.assertEqual(forward, reverse)
        self.assertIn("remote", forward.missing_user_fields)
        self.assertNotIn("preferences.remote", {fact.field_path for fact in forward.prefilled_facts})

    def test_conflict_review_can_accept_one_edit_or_remove_all(self):
        draft = reconcile_profile_extractions(
            (
                _validated_source(
                    DocumentKind.RESUME,
                    RESUME_REFERENCE,
                    _raw_fact("location.city", "Lisbon"),
                ),
                _validated_source(
                    DocumentKind.LINKEDIN_PROFILE_EXPORT,
                    LINKEDIN_REFERENCE,
                    _raw_fact("location.city", "Porto"),
                ),
            )
        )
        review = editable_profile_review(draft)
        values = tuple(
            "Coimbra" if index == 0 else review_value_for_form(fact.value)
            for index, fact in enumerate(review.facts)
        )
        decisions = tuple("accept" if index == 0 else "reject" for index in range(len(review.facts)))
        inputs = {name: "" for name in review.missing_user_fields}
        updated = update_editable_review(review, values, decisions, inputs)
        self.assertEqual(sum(fact.decision == "accept" for fact in updated.facts), 1)
        self.assertEqual(updated.facts[0].value, "Coimbra")
        with self.assertRaisesRegex(ProfileIntakeError, "invalid_review_submission"):
            update_editable_review(
                review,
                values,
                tuple("accept" for _ in review.facts),
                inputs,
            )


class MultiDocumentBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wahojobs-intake-bundle-")
        self.path = Path(self.temp.name) / "intake.sqlite"
        writer = install_browser_authentication_database(self.path)
        self.session = seed_browser_session(writer, suffix="93")
        writer.close()
        self.now = REQUEST_AT
        self.monotonic = 500.0
        self.tokens = _TokenFactory()
        self.adapter = _BundleAdapter()
        self.integration = self._build(self.adapter)
        self.extra_integrations = []

    def tearDown(self):
        for integration in self.extra_integrations:
            integration.close()
        self.integration.close()
        self.temp.cleanup()

    def _build(self, adapter):
        authority = ProfileIntakeAuthorityService(
            authentication_gateway=DurableBrowserSessionAuthenticationGateway(
                trusted_environment_namespace="private_beta",
                clock=lambda: self.now,
            ),
            authorization_gateway=DurablePersistentProfileReadAuthorizationGateway(),
            read_connection_provider=_ReadProvider(self.path),
            clock=lambda: self.now,
        )
        processing = ProfileIntakeProcessingService(
            adapter=adapter,
            vault=IntakeDraftVault(
                monotonic=lambda: self.monotonic,
                token_factory=self.tokens,
            ),
            clock=lambda: self.now,
        )
        integration = ProfileIntakeBrowserIntegration(
            authority,
            processing,
            public_origin=PUBLIC_ORIGIN,
        )
        self.assertTrue(integration.activate())
        return integration

    def _headers(self, *, content_type, content_length):
        return (
            ("Host", PUBLIC_AUTHORITY),
            ("Origin", PUBLIC_ORIGIN),
            ("Sec-Fetch-Site", "same-origin"),
            (
                "Cookie",
                f"wahojobs_session={self.session['session_token']}; "
                f"__Host-wahojobs_session_csrf={self.session['csrf_secret']}",
            ),
            ("Content-Type", content_type),
            ("Content-Length", str(content_length)),
        )

    def _multipart(self, files, *, include_csrf=True, extra_parts=()):
        boundary = "WahoJobsSyntheticBundleBoundary93"
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

        if include_csrf:
            proof = profile_intake_csrf_proof(self.session["csrf_secret"], "upload")
            part(b'Content-Disposition: form-data; name="csrf"', proof.encode())
        for role, document, mime, filename in files:
            headers = (
                f'Content-Disposition: form-data; name="{role}"; filename="{filename}"\r\n'.encode()
                + f"Content-Type: {mime}".encode()
            )
            part(headers, document)
        for name, value in extra_parts:
            part(f'Content-Disposition: form-data; name="{name}"'.encode(), value)
        pieces.append(f"--{boundary}--\r\n".encode())
        body = b"".join(pieces)
        return self._headers(
            content_type=f"multipart/form-data; boundary={boundary}",
            content_length=len(body),
        ), body

    def _upload(self, files, *, integration=None, **kwargs):
        headers, body = self._multipart(files, **kwargs)
        return (integration or self.integration).handle(
            "POST", PROFILE_INTAKE_ROUTE, headers, BytesIO(body)
        )

    def _reference(self, response):
        return parse_qs(urlsplit(dict(response.headers)["Location"]).query)["draft"][0]

    def _grant(self, integration=None):
        integration = integration or self.integration
        headers = tuple(
            item
            for item in self._headers(content_type="x", content_length=1)
            if item[0] not in {"Origin", "Sec-Fetch-Site", "Content-Type", "Content-Length"}
        )
        outcome = integration._authority.authorize(
            method="GET",
            route=PROFILE_INTAKE_REVIEW_ROUTE,
            authentication_input=headers,
            session_token=self.session["session_token"],
            csrf_secret=self.session["csrf_secret"],
        )
        self.assertEqual(outcome.state, "authorized")
        return outcome.grant_for_service()

    @staticmethod
    def _pdf_file(role, text="Synthetic Software Engineer at Example Systems"):
        return role, _pdf_bytes(text), "application/pdf", "ignored-personal-name.pdf"

    @staticmethod
    def _docx_file(role="resume", text="Synthetic Software Engineer at Example Systems"):
        return (
            role,
            _docx_bytes(paragraphs=tuple(text.splitlines())),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "ignored-personal-name.docx",
        )

    def test_all_valid_one_and_two_document_combinations(self):
        empty_linkedin = (
            "linkedin_profile_export",
            b"",
            "application/octet-stream",
            "",
        )
        combinations = (
            (self._pdf_file("resume"), empty_linkedin),
            (self._docx_file(),),
            (self._pdf_file("linkedin_profile_export"),),
            (self._pdf_file("resume"), self._pdf_file("linkedin_profile_export")),
            (self._docx_file(), self._pdf_file("linkedin_profile_export")),
        )
        with mock.patch.object(
            socket.socket,
            "connect",
            side_effect=AssertionError("ordinary intake tests must not use network"),
        ):
            responses = [self._upload(files) for files in combinations]
        self.assertTrue(all(response.status == 303 for response in responses))
        self.assertEqual(len(self.adapter.calls), 7)
        self.assertTrue(all(type(call) is ModelEvidencePacket for call in self.adapter.calls))

    def test_invalid_file_combinations_and_forged_role_fail_closed(self):
        resume = self._pdf_file("resume")
        linkedin = self._pdf_file("linkedin_profile_export")
        self.assertEqual(self._upload(()).status, 400)
        empty_resume = ("resume", b"", "application/octet-stream", "")
        empty_linkedin = (
            "linkedin_profile_export",
            b"",
            "application/octet-stream",
            "",
        )
        self.assertEqual(self._upload((empty_resume, empty_linkedin)).status, 400)
        self.assertEqual(self._upload((resume, resume)).status, 400)
        self.assertEqual(self._upload((linkedin, linkedin)).status, 400)
        self.assertEqual(
            self._upload((self._docx_file("linkedin_profile_export"),)).status,
            415,
        )
        self.assertEqual(self._upload((self._pdf_file("portfolio"),)).status, 400)
        self.assertEqual(
            self._upload(
                (resume,),
                extra_parts=(("document_origin", b"linkedin_profile_export"),),
            ).status,
            400,
        )
        self.assertEqual(self.adapter.calls, [])

    def test_aggregate_limit_rejects_before_read(self):
        headers = self._headers(
            content_type="multipart/form-data; boundary=x",
            content_length=MAX_MULTIPART_BODY_BYTES + 1,
        )
        response = self.integration.handle(
            "POST", PROFILE_INTAKE_ROUTE, headers, _NoReadStream()
        )
        self.assertEqual(response.status, 413)
        self.assertEqual(self.adapter.calls, [])

    def test_two_sources_are_minimized_independently_and_create_one_private_draft(self):
        resume = self._docx_file(
            text="Email: synthetic.resume@example.test\nSynthetic Software Engineer"
        )
        linkedin = self._pdf_file(
            "linkedin_profile_export",
            "Phone: +1 202 555 0199\nSynthetic Software Engineer",
        )
        response = self._upload((linkedin, resume))
        self.assertEqual(response.status, 303)
        self.assertEqual(len(self.adapter.calls), 2)
        model_text = " ".join(
            block.text for packet in self.adapter.calls for block in packet.blocks
        )
        self.assertNotIn("synthetic.resume@example.test", model_text)
        self.assertNotIn("202 555 0199", model_text)
        self.assertEqual(len(self.integration._processing.vault._records), 1)
        reference = self._reference(response)
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(len(snapshot.document.documents), 2)
        self.assertEqual({item.origin for item in snapshot.document.documents}, {"resume", "linkedin_profile_export"})
        serialized = json.dumps(str(snapshot))
        for forbidden in (
            "synthetic.resume@example.test",
            "202 555 0199",
            "ignored-personal-name",
            "EvidencePacket",
            "profile_id",
            "matcher_signals",
            "entitlement",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_either_adapter_failure_leaves_no_partial_draft_or_profile_write(self):
        before = self._profile_counts()
        files = (self._docx_file(), self._pdf_file("linkedin_profile_export"))
        for fail_call, expected_calls in ((1, 1), (2, 2)):
            adapter = _BundleAdapter(fail_call=fail_call)
            integration = self._build(adapter)
            self.extra_integrations.append(integration)
            response = self._upload(files, integration=integration)
            self.assertEqual(response.status, 503)
            self.assertEqual(len(adapter.calls), expected_calls)
            self.assertEqual(len(integration._processing.vault._records), 0)
        malformed_pdf = (
            "linkedin_profile_export",
            b"not a pdf",
            "application/pdf",
            "ignored.pdf",
        )
        self.assertEqual(
            self._upload((self._docx_file(), malformed_pdf)).status,
            415,
        )
        self.assertEqual(len(self.integration._processing.vault._records), 0)
        self.assertEqual(self._profile_counts(), before)

    def test_combined_review_shows_attribution_conflict_and_can_resolve_then_cancel(self):
        adapter = _BundleAdapter(
            {
                DocumentKind.RESUME: (
                    _raw_fact("location.city", "Lisbon"),
                    _raw_fact("skills.normalized", "Python"),
                ),
                DocumentKind.LINKEDIN_PROFILE_EXPORT: (
                    _raw_fact("location.city", "Porto"),
                    _raw_fact("skills.normalized", "Python"),
                ),
            }
        )
        integration = self._build(adapter)
        self.extra_integrations.append(integration)
        response = self._upload(
            (self._pdf_file("resume"), self._pdf_file("linkedin_profile_export")),
            integration=integration,
        )
        reference = self._reference(response)
        target = PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference})
        get_headers = tuple(
            item
            for item in self._headers(content_type="x", content_length=1)
            if item[0] not in {"Origin", "Sec-Fetch-Site", "Content-Type", "Content-Length"}
        )
        page = integration.handle("GET", target, get_headers)
        self.assertEqual(page.status, 200)
        self.assertIn(b"Sources disagree", page.body)
        self.assertIn(b"Found in both", page.body)
        self.assertIn(b"Found in your resume", page.body)
        self.assertIn(b"Found in your LinkedIn profile", page.body)
        self.assertNotIn(b"doc_", page.body)

        snapshot = integration._processing.vault.get(reference, self._grant(integration))
        form = {
            "action": "update",
            "version": "1",
            "csrf": profile_intake_csrf_proof(
                self.session["csrf_secret"],
                "update",
                draft_reference=reference,
                version=1,
            ),
        }
        accepted = False
        for index, fact in enumerate(snapshot.review.facts):
            form[f"fact_{index}_value"] = review_value_for_form(fact.value)
            if fact.conflict_group and not accepted:
                form[f"fact_{index}_decision"] = "accept"
                accepted = True
            elif fact.suggested:
                form[f"fact_{index}_decision"] = "reject"
            else:
                form[f"fact_{index}_decision"] = "keep"
        for name in snapshot.review.missing_user_fields:
            form["missing_" + name] = ""
        form.update(_preference_form_values_for_model(snapshot.review.preference_model))
        body = urlencode(form).encode()
        headers = self._headers(
            content_type="application/x-www-form-urlencoded",
            content_length=len(body),
        )
        updated = integration.handle("POST", target, headers, BytesIO(body))
        self.assertEqual(updated.status, 303)
        self.assertEqual(len(integration._processing.vault._records), 1)

        cancel = urlencode(
            {
                "action": "cancel",
                "version": "2",
                "csrf": profile_intake_csrf_proof(
                    self.session["csrf_secret"],
                    "cancel",
                    draft_reference=reference,
                    version=2,
                ),
            }
        ).encode()
        cancelled = integration.handle(
            "POST",
            target,
            self._headers(
                content_type="application/x-www-form-urlencoded",
                content_length=len(cancel),
            ),
            BytesIO(cancel),
        )
        self.assertEqual(cancelled.status, 303)
        self.assertEqual(len(integration._processing.vault._records), 0)

    def test_review_groups_repeated_found_facts_without_changing_individual_fields(self):
        adapter = _BundleAdapter(
            {
                DocumentKind.RESUME: (
                    _raw_fact("experience.job_titles", "Support Specialist"),
                    _raw_fact("experience.job_titles", "Search Evaluator"),
                    _raw_fact("skills.normalized", "Zendesk"),
                    _raw_fact("skills.normalized", "Data Annotation"),
                    _raw_fact(
                        "experience.occupational_families",
                        "Customer Support",
                        explicit=False,
                    ),
                    _raw_fact(
                        "experience.professional_domains",
                        "AI Training",
                        explicit=False,
                    ),
                )
            }
        )
        integration = self._build(adapter)
        self.extra_integrations.append(integration)
        response = self._upload((self._pdf_file("resume"),), integration=integration)
        reference = self._reference(response)
        target = PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference})
        get_headers = tuple(
            item
            for item in self._headers(content_type="x", content_length=1)
            if item[0]
            not in {"Origin", "Sec-Fetch-Site", "Content-Type", "Content-Length"}
        )
        page = integration.handle("GET", target, get_headers)
        self.assertEqual(page.status, 200)
        self.assertEqual(page.body.count(b"class='profile-group fact-card fact-group-card'"), 2)
        self.assertEqual(page.body.count(b"<h3>Job titles</h3>"), 1)
        self.assertEqual(page.body.count(b"<h3>Skills</h3>"), 1)
        self.assertEqual(page.body.count(b"<span>2 items</span>"), 2)
        self.assertIn(b"Type of work", page.body)
        self.assertIn(b"Areas of experience", page.body)
        self.assertNotIn(b"Occupational Families", page.body)
        self.assertNotIn(b"Professional Domains", page.body)

        snapshot = integration._processing.vault.get(
            reference, self._grant(integration)
        )
        grouped_indexes = [
            index
            for index, fact in enumerate(snapshot.review.facts)
            if fact.review_field in {"job_titles", "skills"}
        ]
        self.assertEqual(len(grouped_indexes), 4)
        for index in grouped_indexes:
            self.assertIn(f"name='fact_{index}_value'".encode(), page.body)
            self.assertIn(f"name='fact_{index}_decision'".encode(), page.body)

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
