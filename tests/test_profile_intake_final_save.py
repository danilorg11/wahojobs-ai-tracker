from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

from tests.ai_profile_import_test_support import (
    NOW,
    database_counts,
    install_ai_profile_import_database,
    intake_grant,
)
from tests.browser_session_authentication_test_support import (
    install_browser_authentication_database,
    seed_browser_session,
)
from tests.test_profile_intake_foundation import _docx_bytes, _pdf_bytes
from tests.persistent_profiles_repository_test_support import create_command
from wahojobs import accounts
from wahojobs.authenticated_profile_matches import AuthenticatedProfileMatchesService
from wahojobs.ai_profile_import import AI_PROFILE_IMPORT_RESERVATION_LEASE
from wahojobs.browser_session_authentication import (
    DurableBrowserSessionAuthenticationGateway,
)
from wahojobs.persistent_profile_read_authorization import (
    DurablePersistentProfileReadAuthorizationGateway,
)
from wahojobs.persistent_profiles_repository import PersistentProfileRepository
from wahojobs.profile_intake.browser import (
    ProfileIntakeBrowserIntegration,
    _collection_form_values_for_review,
    _education_form_values_for_review,
    _preference_form_values_for_model,
)
from wahojobs.profile_intake.contracts import (
    AI_EXTRACTION_SCHEMA_VERSION,
    DocumentKind,
    INTERNAL_INFERRED_CLASSIFICATION_FIELD_PATHS,
    LanguageValue,
    ModelEvidencePacket,
    ProfileIntakeError,
    validate_ai_profile_extraction,
)
from wahojobs.profile_intake.finalization import ProfileIntakeFinalizationService
from wahojobs.profile_intake.runtime import (
    PROFILE_INTAKE_REVIEW_COLLECTIONS,
    PROFILE_INTAKE_REVIEW_ROUTE,
    PROFILE_INTAKE_ROUTE,
    IntakeDraftVault,
    ProfileIntakeAuthorityService,
    ProfileIntakeProcessingService,
    education_entry_field_authorities,
    education_entry_values,
    hydrate_profile_intake_checkpoint,
    managed_education_fact_indexes,
    managed_review_collection_fact_indexes,
    profile_intake_csrf_proof,
    review_collection_entries,
    review_value_for_form,
    serialize_profile_intake_checkpoint,
    update_editable_review,
)
from wahojobs.profiles.canonical_v2 import (
    CANONICAL_PROFILE_V2_LIMITS,
    MAX_LANGUAGES,
    MAX_SKILL_ENTRIES,
)


PUBLIC_ORIGIN = "https://localhost:8443"
PUBLIC_AUTHORITY = "localhost:8443"


class _ConnectionProvider:
    def __init__(self, path, *, read_only):
        self.path = Path(path)
        self.read_only = read_only
        self.active = 0
        self.calls = 0

    def __call__(self):
        @contextmanager
        def scope():
            target = (
                f"file:{self.path.as_posix()}?mode=ro"
                if self.read_only
                else str(self.path)
            )
            connection = sqlite3.connect(target, uri=self.read_only, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            if self.read_only:
                connection.execute("PRAGMA query_only = ON")
            self.calls += 1
            self.active += 1
            try:
                yield connection
            finally:
                self.active -= 1
                connection.close()

        return scope()


class _FinalSaveAdapter:
    def __init__(
        self,
        path,
        providers,
        *,
        fail=False,
        conflict=False,
        include_total_years=False,
        include_education=False,
        include_internal_classifications=False,
        include_background_review=False,
        include_second_job_title=False,
        infer_display_name=False,
        include_country=False,
        include_languages=False,
    ):
        self.path = Path(path)
        self.providers = providers
        self.fail = fail
        self.conflict = conflict
        self.include_total_years = include_total_years
        self.include_education = include_education
        self.include_internal_classifications = include_internal_classifications
        self.include_background_review = include_background_review
        self.include_second_job_title = include_second_job_title
        self.infer_display_name = infer_display_name
        self.include_country = include_country
        self.include_languages = include_languages
        self.calls = []
        self.attempt_counts_during_model = []

    def extract(self, evidence):
        if type(evidence) is not ModelEvidencePacket:
            raise AssertionError("raw evidence crossed model boundary")
        if any(provider.active for provider in self.providers):
            raise AssertionError("model work ran inside a database scope")
        with sqlite3.connect(self.path) as connection:
            self.attempt_counts_during_model.append(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_profile_import_attempts"
                ).fetchone()[0]
            )
        self.calls.append(evidence.document_kind)
        if self.fail:
            raise ProfileIntakeError("openai_transport_error")
        block = evidence.blocks[0].reference
        reference = evidence.document_reference
        city = (
            "Porto"
            if self.conflict
            and evidence.document_kind is DocumentKind.LINKEDIN_PROFILE_EXPORT
            else "Lisbon"
        )
        facts = [
            _fact(
                reference,
                block,
                "identity.display_name",
                "Synthetic Candidate",
                explicit=not self.infer_display_name,
            ),
            _fact(reference, block, "location.city", city),
            _fact(reference, block, "skills.normalized", "Python"),
            _fact(
                reference,
                block,
                "experience.seniority",
                "senior",
                explicit=False,
            ),
        ]
        if self.include_country:
            facts.append(_fact(reference, block, "location.country", "Brazil"))
        if self.include_languages:
            facts.extend(
                (
                    _fact(
                        reference,
                        block,
                        "languages",
                        {
                            "language": "Portuguese",
                            "proficiency": "native",
                            "locale": "Brazil",
                        },
                    ),
                    _fact(
                        reference,
                        block,
                        "languages",
                        {
                            "language": "English",
                            "proficiency": "professional",
                            "locale": None,
                        },
                    ),
                )
            )
        if self.include_total_years:
            facts.append(
                _fact(reference, block, "experience.total_years", 6)
            )
        if self.include_education:
            facts.extend(
                (
                    _fact(reference, block, "education.education_level", "bachelor", explicit=False),
                    _fact(reference, block, "education.degrees", "Bachelor of Business Administration"),
                    _fact(reference, block, "education.fields_or_domains", "Business Administration"),
                    _fact(reference, block, "education.institutions", "Faculdade Horizonte Paulista"),
                    _fact(reference, block, "education.graduation_years", 2016),
                    _fact(reference, block, "education.completion_status", "completed", explicit=False),
                )
            )
        if self.include_internal_classifications:
            facts.extend(
                (
                    _fact(
                        reference,
                        block,
                        "experience.occupational_families",
                        "Customer Support",
                        explicit=False,
                    ),
                    _fact(
                        reference,
                        block,
                        "experience.professional_domains",
                        "AI Training",
                        explicit=False,
                    ),
                    _fact(
                        reference,
                        block,
                        "experience.contribution_type",
                        "individual contributor",
                        explicit=False,
                    ),
                )
            )
        if self.include_background_review:
            facts.extend(
                (
                    _fact(reference, block, "experience.job_titles", "Customer Support Specialist"),
                    _fact(reference, block, "experience.recent_roles", "Customer Support Specialist"),
                    _fact(reference, block, "experience.specialties", "Customer experience", explicit=False),
                    _fact(reference, block, "experience.specialties", "Search quality", explicit=False),
                    _fact(reference, block, "experience.industries", "Business services", explicit=False),
                    _fact(reference, block, "experience.industries", "Technology services", explicit=False),
                    _fact(reference, block, "preferences.remote", True),
                )
            )
            if self.include_second_job_title:
                facts.append(
                    _fact(
                        reference,
                        block,
                        "experience.job_titles",
                        "Search Quality Evaluator",
                    )
                )
        if evidence.document_kind is DocumentKind.LINKEDIN_PROFILE_EXPORT:
            facts.append(_fact(reference, block, "skills.normalized", "SQL"))
        return validate_ai_profile_extraction(
            {
                "schema_version": AI_EXTRACTION_SCHEMA_VERSION,
                "document_reference": reference,
                "facts": facts,
            },
            evidence,
        )


def _fact(reference, block, path, value, *, explicit=True):
    return {
        "field_path": path,
        "value": value,
        "source_document_reference": reference,
        "evidence_block_references": [block],
        "confidence": 0.95 if explicit else 0.75,
        "explicit": explicit,
    }


class _OneShotSaveFailure:
    def __init__(self, boundary):
        self.boundary = boundary
        self.fired = False

    def __call__(self, boundary):
        if boundary == self.boundary and not self.fired:
            self.fired = True
            raise RuntimeError("synthetic post-commit response loss")


class _ShortLeaseFinalizer:
    def __init__(self, delegate):
        self.delegate = delegate

    def preflight(self, grant):
        return self.delegate.preflight(grant)

    def checkpoint_summary(self, grant):
        return self.delegate.checkpoint_summary(grant)

    def reserve(self, grant, review, document, diagnostics):
        authority, _lifetime = self.delegate.reserve(
            grant, review, document, diagnostics
        )
        return authority, 30

    def renew(self, grant, authority):
        return self.delegate.renew(grant, authority)

    def resume(self, grant, checkpoint_id, *, expected_version):
        return self.delegate.resume(
            grant,
            checkpoint_id,
            expected_version=expected_version,
        )

    def save_checkpoint(self, grant, authority, review, *, review_step="review-found"):
        return self.delegate.save_checkpoint(
            grant,
            authority,
            review,
            review_step=review_step,
        )

    def release(self, grant, authority, *, outcome_code):
        return self.delegate.release(grant, authority, outcome_code=outcome_code)

    def discard_checkpoint(self, grant, authority):
        return self.delegate.discard_checkpoint(grant, authority)

    def discard_saved_checkpoint(self, grant, checkpoint):
        return self.delegate.discard_saved_checkpoint(grant, checkpoint)

    def prepare(self, review, authority):
        return self.delegate.prepare(review, authority)

    def commit(self, grant, authority, confirmed):
        return self.delegate.commit(grant, authority, confirmed)


class ProfileIntakeFinalSaveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="wahojobs-intake-final-", ignore_cleanup_errors=True
        )
        self.path = Path(self.directory.name) / "database.sqlite3"
        writer, self.session = install_ai_profile_import_database(self.path, suffix="97")
        writer.close()
        self.now = NOW
        self.monotonic = 100.0
        self.read_provider = _ConnectionProvider(self.path, read_only=True)
        self.write_provider = _ConnectionProvider(self.path, read_only=False)
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
        )
        self.integration = self._build(self.adapter)

    def tearDown(self):
        self.integration.close()
        self.directory.cleanup()

    def _build(self, adapter, *, save_failure_injector=None, finalizer_override=None):
        authentication = DurableBrowserSessionAuthenticationGateway(
            trusted_environment_namespace=self.session["environment"],
            clock=lambda: self.now,
        )
        authorization = DurablePersistentProfileReadAuthorizationGateway()
        authority = ProfileIntakeAuthorityService(
            authentication_gateway=authentication,
            authorization_gateway=authorization,
            read_connection_provider=self.read_provider,
            clock=lambda: self.now,
        )
        finalizer = finalizer_override or ProfileIntakeFinalizationService(
            read_connection_provider=self.read_provider,
            write_connection_provider=self.write_provider,
            clock=lambda: self.now,
        )
        processing = ProfileIntakeProcessingService(
            adapter=adapter,
            vault=IntakeDraftVault(monotonic=lambda: self.monotonic),
            clock=lambda: self.now,
            durable_finalizer=finalizer,
            save_failure_injector=save_failure_injector,
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
        content_type=None,
        content_length=None,
        origin=True,
        session=None,
    ):
        session = session or self.session
        headers = [
            ("Host", PUBLIC_AUTHORITY),
            (
                "Cookie",
                f"wahojobs_session={session['session_token']}; "
                f"__Host-wahojobs_session_csrf={session['csrf_secret']}",
            ),
        ]
        if origin:
            headers.extend((("Origin", PUBLIC_ORIGIN), ("Sec-Fetch-Site", "same-origin")))
        if content_type is not None:
            headers.append(("Content-Type", content_type))
        if content_length is not None:
            headers.append(("Content-Length", str(content_length)))
        return tuple(headers)

    def _multipart(self, roles, *, session=None):
        session = session or self.session
        boundary = "WahoJobsSlice4B97"
        pieces = []

        def add(name, value, *, filename=None, mime=None):
            disposition = f'Content-Disposition: form-data; name="{name}"'
            if filename is not None:
                disposition += f'; filename="{filename}"'
            pieces.extend(
                [
                    f"--{boundary}\r\n{disposition}\r\n".encode(),
                    (f"Content-Type: {mime}\r\n".encode() if mime else b""),
                    b"\r\n",
                    value,
                    b"\r\n",
                ]
            )

        add(
            "csrf",
            profile_intake_csrf_proof(
                session["csrf_secret"], "upload"
            ).encode(),
        )
        for role in roles:
            if role == "resume_docx":
                add(
                    "resume",
                    _docx_bytes(
                        paragraphs=(
                            "Synthetic Candidate",
                            "Platform engineer in Lisbon",
                        )
                    ),
                    filename="private-sentinel-resume.docx",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            else:
                add(
                    role,
                    _pdf_bytes("Synthetic Candidate platform engineer in Lisbon"),
                    filename="private-sentinel-profile.pdf",
                    mime="application/pdf",
                )
        pieces.append(f"--{boundary}--\r\n".encode())
        body = b"".join(pieces)
        return body, f"multipart/form-data; boundary={boundary}"

    def _upload(self, roles=("resume_docx",), *, integration=None, session=None):
        integration = integration or self.integration
        body, content_type = self._multipart(roles, session=session)
        return integration.handle(
            "POST",
            PROFILE_INTAKE_ROUTE,
            self._headers(
                content_type=content_type,
                content_length=len(body),
                session=session,
            ),
            BytesIO(body),
        )

    def _reference(self, response):
        self.assertEqual(response.status, 303)
        location = dict(response.headers)["Location"]
        return parse_qs(urlsplit(location).query)["draft"][0]

    def _grant(self):
        return intake_grant(self.path, self.session, now=self.now)

    def _save_body(
        self,
        reference,
        *,
        decisions=None,
        changes=None,
        confirm_background=True,
    ):
        return self._review_body(
            reference,
            action="save",
            decisions=decisions,
            changes=changes,
            confirm_background=confirm_background,
        )

    def _review_body(
        self,
        reference,
        *,
        action,
        integration=None,
        session=None,
        decisions=None,
        changes=None,
        preference_overrides=None,
        review_step=None,
        collection_overrides=None,
        use_collections=False,
        education_overrides=None,
        use_education=False,
        confirm_background=None,
        confirm_profile_basics=False,
        reset_section=None,
    ):
        integration = integration or self.integration
        session = session or self.session
        grant = intake_grant(self.path, session, now=self.now)
        snapshot = integration._processing.vault.get(reference, grant)
        form = {
            "action": action,
            "version": str(snapshot.version),
            "csrf": profile_intake_csrf_proof(
                session["csrf_secret"],
                action,
                draft_reference=reference,
                version=snapshot.version,
            ),
            "review_step": review_step or snapshot.review_step,
        }
        if confirm_background is None:
            confirm_background = action == "save"
        if confirm_background:
            form["review_confirm_step"] = "review-suggestions"
        if confirm_profile_basics:
            if confirm_background:
                raise AssertionError("only one review step can be confirmed per request")
            form["review_confirm_step"] = "review-found"
        if reset_section is not None:
            if (
                action != "autosave"
                or confirm_background
                or confirm_profile_basics
            ):
                raise AssertionError("section reset must be an isolated autosave")
            form["reset_section"] = reset_section
        decisions = decisions or {}
        changes = changes or {}
        managed_indexes = (
            managed_review_collection_fact_indexes(snapshot.review)
            if use_collections
            else frozenset()
        )
        if use_education:
            managed_indexes = managed_indexes | managed_education_fact_indexes(
                snapshot.review
            )
        for index, fact in enumerate(snapshot.review.facts):
            if (
                index in managed_indexes
                or fact.field_path in INTERNAL_INFERRED_CLASSIFICATION_FIELD_PATHS
                or fact.field_path
                in {"experience.industries", "experience.seniority"}
                or fact.field_path.startswith("preferences.")
            ):
                continue
            form[f"fact_{index}_value"] = changes.get(
                index, review_value_for_form(fact.value)
            )
            if fact.field_path == "experience.total_years" and fact.conflict_group is None:
                continue
            if fact.conflict_group is None and (
                not fact.suggested
                or fact.field_path.startswith(("identity.", "location."))
            ):
                continue
            form[f"fact_{index}_decision"] = decisions.get(
                index,
                "accept" if fact.suggested else "keep",
            )
        for name in snapshot.review.missing_user_fields:
            form["missing_" + name] = ""
        form.update(_preference_form_values_for_model(snapshot.review.preference_model))
        if use_collections:
            form.update(_collection_form_values_for_review(snapshot.review))
            form.update(collection_overrides or {})
        if use_education:
            form.update(_education_form_values_for_review(snapshot.review))
            form.update(education_overrides or {})
        form.update(preference_overrides or {})
        return urlencode(form).encode()

    def _save(self, reference, body):
        target = PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference})
        return self.integration.handle(
            "POST",
            target,
            self._headers(
                content_type="application/x-www-form-urlencoded",
                content_length=len(body),
            ),
            BytesIO(body),
        )

    def _post_review(self, reference, body, *, integration=None, session=None):
        integration = integration or self.integration
        target = PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference})
        return integration.handle(
            "POST",
            target,
            self._headers(
                content_type="application/x-www-form-urlencoded",
                content_length=len(body),
                session=session,
            ),
            BytesIO(body),
        )

    def _entry_action(self, action, *, integration=None, session=None):
        integration = integration or self.integration
        session = session or self.session
        body = urlencode(
            {
                "action": action,
                "csrf": profile_intake_csrf_proof(
                    session["csrf_secret"],
                    action,
                ),
            }
        ).encode()
        return integration.handle(
            "POST",
            PROFILE_INTAKE_ROUTE,
            self._headers(
                content_type="application/x-www-form-urlencoded",
                content_length=len(body),
                session=session,
            ),
            BytesIO(body),
        )

    def _database(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def test_review_collection_limits_remain_coherent_with_canonical_v2(self):
        self.assertEqual(
            PROFILE_INTAKE_REVIEW_COLLECTIONS["skills"]["add_limit"],
            MAX_SKILL_ENTRIES,
        )
        self.assertEqual(
            PROFILE_INTAKE_REVIEW_COLLECTIONS["job_titles"]["add_limit"],
            CANONICAL_PROFILE_V2_LIMITS["string_list_items"],
        )
        self.assertEqual(
            PROFILE_INTAKE_REVIEW_COLLECTIONS["industries"]["add_limit"],
            CANONICAL_PROFILE_V2_LIMITS["string_list_items"],
        )
        self.assertEqual(
            PROFILE_INTAKE_REVIEW_COLLECTIONS["languages"]["limit"],
            MAX_LANGUAGES,
        )

    def test_profile_basics_render_as_direct_fields_and_get_confirms_nothing(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_country=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        before = self.integration._processing.vault.get(reference, self._grant())
        page = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference}),
            self._headers(origin=False),
        )
        self.assertEqual(page.status, 200)
        name_index = next(
            index
            for index, fact in enumerate(before.review.facts)
            if fact.field_path == "identity.display_name"
        )
        country_index = next(
            index
            for index, fact in enumerate(before.review.facts)
            if fact.field_path == "location.country"
        )
        self.assertIn(
            f"name='fact_{name_index}_value' value='Synthetic Candidate' maxlength='160' required".encode(),
            page.body,
        )
        self.assertIn(
            f"name='fact_{country_index}_value' value='Brazil'".encode(),
            page.body,
        )
        self.assertNotIn(f"name='fact_{name_index}_decision'".encode(), page.body)
        self.assertNotIn(f"name='fact_{country_index}_decision'".encode(), page.body)
        self.assertNotIn(b"Include this in your profile?", page.body)
        self.assertNotIn(b">Include it<", page.body)
        self.assertNotIn(b">Leave it out<", page.body)
        after = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(after.version, before.version)
        self.assertEqual(after.review, before.review)

    def test_profile_basic_corrections_resume_and_persist_as_user_corrections(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_country=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        indexes = {
            fact.field_path: index for index, fact in enumerate(snapshot.review.facts)
        }
        saved = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                changes={
                    indexes["identity.display_name"]: "Marina Example",
                    indexes["location.country"]: "Portugal",
                },
                confirm_background=False,
            ),
        )
        self.assertEqual(saved.status, 204)
        updated = self.integration._processing.vault.get(reference, self._grant())
        edited = {
            fact.field_path: fact
            for fact in updated.review.facts
            if fact.field_path in {"identity.display_name", "location.country"}
        }
        self.assertEqual(edited["identity.display_name"].value, "Marina Example")
        self.assertEqual(edited["location.country"].value, "Portugal")
        self.assertTrue(edited["identity.display_name"].candidate_edited)
        self.assertTrue(edited["location.country"].candidate_edited)
        with self._database() as connection:
            checkpoint = json.loads(
                connection.execute(
                    "SELECT review_payload_json FROM ai_profile_intake_checkpoints"
                ).fetchone()[0]
            )
        checkpoint_edits = {
            item["field_path"]: item.get("candidate_edited", False)
            for item in checkpoint["facts"]
        }
        self.assertTrue(checkpoint_edits["identity.display_name"])
        self.assertTrue(checkpoint_edits["location.country"])

        extraction_calls = tuple(self.adapter.calls)
        self.integration.close()
        self.integration = self._build(self.adapter)
        resumed_reference = self._reference(self._entry_action("continue"))
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        resumed = self.integration._processing.vault.get(
            resumed_reference, self._grant()
        )
        self.assertTrue(
            next(
                fact.candidate_edited
                for fact in resumed.review.facts
                if fact.field_path == "identity.display_name"
            )
        )
        finalized = self._post_review(
            resumed_reference,
            self._save_body(resumed_reference),
        )
        self.assertEqual(finalized.status, 303)
        with self._database() as connection:
            profile = json.loads(
                connection.execute(
                    "SELECT structured_profile_json FROM product_profile_revisions"
                ).fetchone()[0]
            )
        self.assertEqual(profile["identity"]["display_name"], "Marina Example")
        self.assertEqual(profile["location"]["country"], "Portugal")
        sources = {
            item["field_path"]: item
            for item in profile["provenance"]["field_sources"]
        }
        # Canonical V2 deliberately omits identity.* field-source rows, while
        # the checkpoint keeps the correction authority needed before Save.
        self.assertNotIn("identity.display_name", sources)
        self.assertEqual(
            sources["location.country"]["source_kind"], "user_correction"
        )
        self.assertEqual(
            sources["location.city"]["source_kind"], "resume_extraction"
        )

    def test_required_name_rejects_blank_while_optional_location_can_be_cleared(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_country=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        indexes = {
            fact.field_path: index for index, fact in enumerate(snapshot.review.facts)
        }
        blank_name = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                changes={indexes["identity.display_name"]: ""},
                confirm_background=False,
            ),
        )
        self.assertEqual(blank_name.status, 400)
        self.assertEqual(
            self.integration._processing.vault.get(reference, self._grant()).version,
            snapshot.version,
        )
        cleared = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                changes={indexes["location.country"]: ""},
                confirm_background=False,
            ),
        )
        self.assertEqual(cleared.status, 204)
        current = self.integration._processing.vault.get(reference, self._grant())
        country = current.review.facts[indexes["location.country"]]
        self.assertEqual(country.decision, "remove")

    def test_step_one_continue_confirms_only_visible_inferred_basics(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            infer_display_name=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        before = self.integration._processing.vault.get(reference, self._grant())
        name_index = next(
            index
            for index, fact in enumerate(before.review.facts)
            if fact.field_path == "identity.display_name"
        )
        seniority_index = next(
            index
            for index, fact in enumerate(before.review.facts)
            if fact.field_path == "experience.seniority"
        )
        self.assertEqual(before.review.facts[name_index].decision, "pending")
        page = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference}),
            self._headers(origin=False),
        )
        self.assertIn(b"data-profile-basics-pending=true", page.body)
        self.assertNotIn(f"name='fact_{name_index}_decision'".encode(), page.body)
        self.assertEqual(
            self.integration._processing.vault.get(reference, self._grant()).version,
            before.version,
        )
        confirmed = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                confirm_background=False,
                confirm_profile_basics=True,
            ),
        )
        self.assertEqual(confirmed.status, 204)
        after = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(after.review.facts[name_index].decision, "accept")
        self.assertFalse(after.review.facts[name_index].candidate_edited)
        self.assertNotEqual(after.review.facts[seniority_index].decision, "accept")

    def test_preflight_is_read_only_and_old_schema_fails_closed_without_migration(self):
        response = self.integration.handle(
            "GET", PROFILE_INTAKE_ROUTE, self._headers(origin=False)
        )
        self.assertEqual(response.status, 200)
        with self._database() as connection:
            self.assertEqual(database_counts(connection)["ai_profile_import_entitlements"], 0)
            self.assertEqual(database_counts(connection)["ai_profile_import_attempts"], 0)

        old_path = Path(self.directory.name) / "old.sqlite3"
        writer = install_browser_authentication_database(old_path)
        old_session = seed_browser_session(writer, suffix="98")
        writer.close()
        old_read = _ConnectionProvider(old_path, read_only=True)
        old_write = _ConnectionProvider(old_path, read_only=False)
        previous_session = self.session
        previous_read, previous_write = self.read_provider, self.write_provider
        try:
            self.session = old_session
            self.read_provider, self.write_provider = old_read, old_write
            old = self._build(_FinalSaveAdapter(old_path, (old_read, old_write)))
            self.addCleanup(old.close)
            unavailable = old.handle(
                "GET", PROFILE_INTAKE_ROUTE, self._headers(origin=False)
            )
            self.assertEqual(unavailable.status, 503)
            with sqlite3.connect(old_path) as connection:
                markers = {
                    row[0]
                    for row in connection.execute(
                        "SELECT version FROM wahojobs_schema_migrations"
                    )
                }
                self.assertNotIn("010_ai_profile_import", markers)
        finally:
            self.session = previous_session
            self.read_provider, self.write_provider = previous_read, previous_write

    def test_reservation_starts_after_model_and_bounds_the_vault_without_leaking_ids(self):
        reference = self._reference(self._upload())
        self.assertEqual(self.adapter.attempt_counts_during_model, [0])
        with self._database() as connection:
            self.assertEqual(database_counts(connection)["ai_profile_import_attempts"], 1)
            row = connection.execute(
                "SELECT attempt_id,reservation_id,state FROM ai_profile_import_attempts"
            ).fetchone()
            self.assertEqual(row["state"], "reserved")
        grant = self._grant()
        snapshot = self.integration._processing.vault.get(reference, grant)
        self.assertLessEqual(snapshot.expires_at_monotonic - self.monotonic, 1800)
        page = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference}),
            self._headers(origin=False),
        )
        self.assertEqual(page.status, 200)
        self.assertIn(b"<button type='submit'>Find my matches</button>", page.body)
        self.assertIn(
            b"Your progress is saved as you review. Your profile is created only when you choose Find my matches",
            page.body,
        )
        self.assertNotIn(row["attempt_id"].encode(), page.body)
        self.assertNotIn(row["reservation_id"].encode(), page.body)
        self.assertNotIn(b"private-sentinel", page.body)

    def test_review_renewal_stays_inside_durable_lease_and_does_not_consume(self):
        reference = self._reference(self._upload())
        grant = self._grant()
        snapshot = self.integration._processing.vault.get(reference, grant)
        with self._database() as connection:
            initial_attempt = connection.execute(
                "SELECT lease_expires_at,state FROM ai_profile_import_attempts"
            ).fetchone()
            initial_entitlement = connection.execute(
                "SELECT state,lease_expires_at,consumed_at FROM ai_profile_import_entitlements"
            ).fetchone()
        self.assertEqual(initial_attempt["state"], "reserved")
        self.assertEqual(tuple(initial_entitlement), (
            "reserved",
            (self.now + AI_PROFILE_IMPORT_RESERVATION_LEASE).isoformat(timespec="seconds"),
            None,
        ))
        self.assertLessEqual(
            snapshot.absolute_expires_at_monotonic - self.monotonic,
            2 * 60 * 60 - 30,
        )
        self.assertGreater(
            snapshot.absolute_expires_at_monotonic - self.monotonic,
            7100,
        )

        self.monotonic += 301
        self.now += timedelta(seconds=301)
        renewal = urlencode(
            {
                "action": "renew",
                "version": str(snapshot.version),
                "csrf": profile_intake_csrf_proof(
                    self.session["csrf_secret"],
                    "renew",
                    draft_reference=reference,
                    version=snapshot.version,
                ),
            }
        ).encode()
        target = PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference})
        response = self.integration.handle(
            "POST",
            target,
            self._headers(
                content_type="application/x-www-form-urlencoded",
                content_length=len(renewal),
            ),
            BytesIO(renewal),
        )
        self.assertEqual(response.status, 204)
        renewed = self.integration._processing.vault.get(reference, self._grant())
        self.assertGreater(renewed.expires_at_monotonic, snapshot.expires_at_monotonic)
        self.assertLessEqual(
            renewed.expires_at_monotonic,
            renewed.absolute_expires_at_monotonic,
        )
        with self._database() as connection:
            attempt = connection.execute(
                "SELECT lease_expires_at,state FROM ai_profile_import_attempts"
            ).fetchone()
            entitlement = connection.execute(
                "SELECT state,lease_expires_at,consumed_at FROM ai_profile_import_entitlements"
            ).fetchone()
        self.assertGreater(attempt["lease_expires_at"], initial_attempt["lease_expires_at"])
        self.assertEqual(attempt["state"], "reserved")
        self.assertEqual(entitlement["state"], "reserved")
        self.assertEqual(entitlement["lease_expires_at"], attempt["lease_expires_at"])
        self.assertIsNone(entitlement["consumed_at"])

    def test_validated_autosave_updates_checkpoint_and_unchanged_save_keeps_expiry(self):
        reference = self._reference(self._upload())
        page = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference}),
            self._headers(origin=False),
        )
        self.assertEqual(page.status, 200)
        self.assertIn(b"Saving\xe2\x80\xa6", page.body)
        self.assertIn(b"Progress saved", page.body)
        self.assertIn(b"profile-review-save-retry", page.body)
        self.assertIn(b"pagehide", page.body)
        self.assertIn(b"fetch(form.getAttribute('action')", page.body)
        self.assertIn(b"fetch(renew.getAttribute('action')", page.body)
        self.assertIn(b"name='review_step' value='review-found'", page.body)
        self.assertIn(b"reviewSteps", page.body)
        self.assertIn(b"review-progress a[href^=\"#review-\"]", page.body)
        self.assertNotIn(b"fetch(form.action", page.body)
        self.assertNotIn(b"fetch(renew.action", page.body)

        body = self._review_body(
            reference,
            action="autosave",
            changes={0: "Saved Synthetic Candidate"},
            preference_overrides={
                "preference_employment_relationships_mode": "preferences",
                "preference_employment_relationships_independent_contractor": "selected"
            },
        )
        saved = self._post_review(reference, body)
        self.assertEqual(saved.status, 204)
        headers = dict(saved.headers)
        self.assertEqual(headers["X-Wahojobs-Review-Version"], "2")
        for name in (
            "X-Wahojobs-CSRF-Save",
            "X-Wahojobs-CSRF-Autosave",
            "X-Wahojobs-CSRF-Renew",
            "X-Wahojobs-CSRF-Discard",
        ):
            self.assertRegex(headers[name], r"^[A-Za-z0-9_-]{43}$")
        with self._database() as connection:
            row = connection.execute(
                "SELECT row_version,review_payload_json,review_saved_at,expires_at "
                "FROM ai_profile_intake_checkpoints"
            ).fetchone()
            self.assertEqual(row["row_version"], 2)
            payload = json.loads(row["review_payload_json"])
            self.assertEqual(payload["facts"][0]["value"], "Saved Synthetic Candidate")
            self.assertEqual(
                payload["preference_model"]["employment_relationships"],
                ["independent_contractor"],
            )
            saved_times = (row["review_saved_at"], row["expires_at"])

        self.now += timedelta(hours=1)
        unchanged = self._post_review(
            reference,
            self._review_body(reference, action="autosave"),
        )
        self.assertEqual(unchanged.status, 204)
        self.assertEqual(
            dict(unchanged.headers)["X-Wahojobs-Review-Version"],
            "2",
        )
        with self._database() as connection:
            row = connection.execute(
                "SELECT row_version,review_saved_at,expires_at "
                "FROM ai_profile_intake_checkpoints"
            ).fetchone()
            self.assertEqual(
                (row["row_version"], row["review_saved_at"], row["expires_at"]),
                (2, *saved_times),
            )

    def test_autosave_preserves_unresolved_suggestion_without_confirming_it(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_background_review=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        identity_index = next(
            index
            for index, fact in enumerate(snapshot.review.facts)
            if fact.field_path == "identity.display_name"
        )
        suggestion_index = next(
            index
            for index, fact in enumerate(snapshot.review.facts)
            if fact.field_path == "experience.specialties"
            and fact.suggested
            and fact.decision == "pending"
        )
        saved = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                changes={identity_index: "Saved With Pending Suggestion"},
                use_collections=True,
                confirm_background=False,
            ),
        )
        self.assertEqual(saved.status, 204)
        updated = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(
            updated.review.facts[identity_index].value,
            "Saved With Pending Suggestion",
        )
        self.assertEqual(updated.review.facts[suggestion_index].decision, "pending")
        with self._database() as connection:
            payload = json.loads(
                connection.execute(
                    "SELECT review_payload_json FROM ai_profile_intake_checkpoints"
                ).fetchone()[0]
            )
        self.assertEqual(payload["facts"][suggestion_index]["decision"], "pending")

        rejected = self._post_review(
            reference,
            self._review_body(
                reference,
                action="save",
                use_collections=True,
                confirm_background=False,
            ),
        )
        self.assertEqual(rejected.status, 409)
        with self._database() as connection:
            self.assertEqual(database_counts(connection)["product_profiles"], 0)

    def test_review_step_autosave_is_allowlisted_and_semantically_isolated(self):
        reference = self._reference(self._upload())
        semantic_baseline = None
        for step in (
            "review-found",
            "review-suggestions",
            "review-preferences",
            "review-finish",
        ):
            saved = self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    review_step=step,
                ),
            )
            self.assertEqual(saved.status, 204)
            snapshot = self.integration._processing.vault.get(
                reference,
                self._grant(),
            )
            self.assertEqual(snapshot.review_step, step)
            with self._database() as connection:
                payload = json.loads(
                    connection.execute(
                        "SELECT review_payload_json FROM ai_profile_intake_checkpoints"
                    ).fetchone()[0]
                )
            self.assertEqual(payload.pop("review_step"), step)
            if semantic_baseline is None:
                semantic_baseline = payload
            else:
                self.assertEqual(payload, semantic_baseline)

        fallback = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                review_step="https://example.invalid/not-a-review-step",
            ),
        )
        self.assertEqual(fallback.status, 204)
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(snapshot.review_step, "review-found")
        self.integration.close()
        self.integration = self._build(self.adapter)
        continued = self._entry_action("continue")
        self.assertTrue(dict(continued.headers)["Location"].endswith("#review-found"))
        self.assertEqual(tuple(self.adapter.calls), (DocumentKind.RESUME,))

    def test_process_restart_cross_session_continue_restores_without_extraction(self):
        reference = self._reference(self._upload())
        saved = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                changes={0: "Resumable Synthetic Candidate"},
                preference_overrides={
                    "preference_employment_relationships_mode": "preferences",
                    "preference_employment_relationships_independent_contractor": "selected"
                },
                review_step="review-preferences",
            ),
        )
        self.assertEqual(saved.status, 204)
        extraction_calls = tuple(self.adapter.calls)
        self.integration.close()
        self.monotonic += 3_601
        self.now += timedelta(hours=1)
        with self._database() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            session = accounts.create_session(
                connection,
                user_id=self.session["account_id"],
                idle_ttl=timedelta(hours=3),
                absolute_ttl=timedelta(days=1),
                idempotency_key="slice-5b-cross-session-resume-0001",
                now=self.now - timedelta(minutes=1),
            )
            connection.commit()
        resumed_session = dict(self.session)
        resumed_session.update(
            session_id=session.session.session_id,
            session_token=session.session_token,
            csrf_secret=session.csrf_secret,
        )
        self.integration = self._build(self.adapter)
        entry = self.integration.handle(
            "GET",
            PROFILE_INTAKE_ROUTE,
            self._headers(origin=False, session=resumed_session),
        )
        self.assertEqual(entry.status, 200)
        self.assertIn(b"Continue building your profile", entry.body)
        self.assertIn(b"Last saved 1 hour ago", entry.body)
        self.assertIn(b"saved for 7 days", entry.body)
        with self._database() as connection:
            checkpoint_id = connection.execute(
                "SELECT checkpoint_id FROM ai_profile_intake_checkpoints"
            ).fetchone()[0]
        self.assertNotIn(checkpoint_id.encode("ascii"), entry.body)
        self.assertNotIn(b"private-sentinel", entry.body)
        continued = self._entry_action("continue", session=resumed_session)
        self.assertTrue(
            dict(continued.headers)["Location"].endswith("#review-preferences")
        )
        resumed_reference = self._reference(continued)
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        resumed_grant = intake_grant(self.path, resumed_session, now=self.now)
        snapshot = self.integration._processing.vault.get(
            resumed_reference,
            resumed_grant,
        )
        self.assertIsNone(snapshot.document)
        self.assertIsNone(snapshot.diagnostics)
        self.assertEqual(snapshot.review_step, "review-preferences")
        self.assertEqual(snapshot.review.facts[0].value, "Resumable Synthetic Candidate")
        self.assertEqual(
            snapshot.review.preference_model["employment_relationships"],
            ["independent_contractor"],
        )
        with self._database() as connection:
            self.assertEqual(
                tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT state FROM ai_profile_import_attempts ORDER BY created_at,attempt_id"
                    )
                ),
                ("expired", "reserved"),
            )

    def test_v2_schedule_and_compensation_expectations_autosave_resume_and_finalize(self):
        reference = self._reference(self._upload())
        overrides = {
            "preference_schedule_working_days_mode": "preferences",
            "preference_schedule_working_days_weekdays": "selected",
            "preference_schedule_time_of_day_mode": "preferences",
            "preference_schedule_time_of_day_business_hours": "selected",
            "preference_schedule_flexibility_modes_mode": "preferences",
            "preference_schedule_flexibility_modes_flexible": "selected",
            "preference_schedule_coordination_modes_mode": "preferences",
            "preference_schedule_coordination_modes_asynchronous": "selected",
            "preference_compensation_0_minimum_kind": "preferred",
            "preference_compensation_0_amount": "30.00",
            "preference_compensation_0_currency": "USD",
            "preference_compensation_0_period": "hour",
            "preference_compensation_1_minimum_kind": "strict",
            "preference_compensation_1_amount": "90000",
            "preference_compensation_1_currency": "USD",
            "preference_compensation_1_period": "year",
        }
        autosaved = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                preference_overrides=overrides,
                review_step="review-preferences",
            ),
        )
        self.assertEqual(autosaved.status, 204)
        extraction_calls = tuple(self.adapter.calls)
        with self._database() as connection:
            payload = json.loads(
                connection.execute(
                    "SELECT review_payload_json FROM ai_profile_intake_checkpoints"
                ).fetchone()[0]
            )
        model = payload["preference_model"]
        self.assertEqual(model["schema_version"], "profile_preferences_v2")
        self.assertEqual(model["schedule"]["working_days"], ["weekdays"])
        self.assertEqual(model["schedule"]["time_of_day"], ["business_hours"])
        self.assertEqual(len(model["compensation_expectations"]), 2)

        self.integration.close()
        self.integration = self._build(self.adapter)
        resumed_reference = self._reference(self._entry_action("continue"))
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        resumed = self.integration._processing.vault.get(
            resumed_reference,
            self._grant(),
        )
        self.assertEqual(resumed.review.preference_model, model)
        saved = self._post_review(
            resumed_reference,
            self._review_body(resumed_reference, action="save"),
        )
        self.assertEqual(saved.status, 303)
        with self._database() as connection:
            profile = json.loads(
                connection.execute(
                    "SELECT structured_profile_json FROM product_profile_revisions"
                ).fetchone()[0]
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_profile_intake_checkpoints"
                ).fetchone()[0],
                0,
            )
        persisted = profile["preferences"]["preference_model"]
        self.assertEqual(persisted, model)
        self.assertIn("weekdays", profile["preferences"]["schedule"])
        self.assertIn("business hours", profile["preferences"]["schedule"])
        self.assertIn("USD 30 per hour", profile["preferences"]["rate_pay_preference"])
        self.assertIn("USD 90000 per year", profile["preferences"]["rate_pay_preference"])

    def test_no_preference_clears_dimension_and_resumes_unrestricted(self):
        reference = self._reference(self._upload())
        initial = self.integration._processing.vault.get(reference, self._grant())
        incomplete = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                preference_overrides={
                    "preference_workloads_mode": "preferences",
                },
                review_step="review-preferences",
            ),
        )
        self.assertEqual(incomplete.status, 400)
        unchanged = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(unchanged.version, initial.version)
        self.assertEqual(unchanged.review.preference_model["workloads"], [])

        selected = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                preference_overrides={
                    "preference_workloads_mode": "preferences",
                    "preference_workloads_part_time": "selected",
                },
                review_step="review-preferences",
            ),
        )
        self.assertEqual(selected.status, 204)
        self.assertEqual(
            self.integration._processing.vault.get(
                reference,
                self._grant(),
            ).review.preference_model["workloads"],
            ["part_time"],
        )

        unrestricted_form = {
            name: values[0]
            for name, values in parse_qs(
                self._review_body(
                    reference,
                    action="autosave",
                    review_step="review-preferences",
                ).decode("ascii"),
                keep_blank_values=True,
            ).items()
        }
        unrestricted_form["preference_workloads_mode"] = "unrestricted"
        unrestricted_form.pop("preference_workloads_part_time")
        cleared = self._post_review(
            reference,
            urlencode(unrestricted_form).encode("ascii"),
        )
        self.assertEqual(cleared.status, 204)
        extraction_calls = tuple(self.adapter.calls)
        with self._database() as connection:
            payload = json.loads(
                connection.execute(
                    "SELECT review_payload_json FROM ai_profile_intake_checkpoints"
                ).fetchone()[0]
            )
        self.assertEqual(payload["preference_model"]["workloads"], [])

        self.integration.close()
        self.integration = self._build(self.adapter)
        resumed_reference = self._reference(self._entry_action("continue"))
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        resumed = self.integration._processing.vault.get(
            resumed_reference,
            self._grant(),
        )
        self.assertEqual(resumed.review.preference_model["workloads"], [])

    def test_integer_total_years_autosaves_resumes_and_persists_canonically(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_total_years=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        total_years = next(
            fact.value
            for fact in snapshot.review.facts
            if fact.field_path == "experience.total_years"
        )
        self.assertIs(type(total_years), int)
        self.assertEqual(total_years, 6)

        autosaved = self._post_review(
            reference,
            self._review_body(reference, action="autosave"),
        )
        self.assertEqual(autosaved.status, 204)
        extraction_calls = tuple(self.adapter.calls)
        self.integration.close()
        self.integration = self._build(self.adapter)
        resumed_reference = self._reference(self._entry_action("continue"))
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        resumed = self.integration._processing.vault.get(
            resumed_reference,
            self._grant(),
        )
        resumed_total_years = next(
            fact.value
            for fact in resumed.review.facts
            if fact.field_path == "experience.total_years"
        )
        self.assertIs(type(resumed_total_years), int)
        self.assertEqual(resumed_total_years, 6)

        saved = self._save(
            resumed_reference,
            self._save_body(resumed_reference),
        )
        self.assertEqual(saved.status, 303)
        with self._database() as connection:
            profile = json.loads(
                connection.execute(
                    "SELECT structured_profile_json FROM product_profile_revisions"
                ).fetchone()[0]
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_profile_intake_checkpoints"
                ).fetchone()[0],
                0,
            )
        self.assertIs(type(profile["experience"]["total_years"]), int)
        self.assertEqual(profile["experience"]["total_years"], 6)

    def test_internal_experience_classifications_stay_internal_and_do_not_select_interests(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_internal_classifications=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        page = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference}),
            self._headers(origin=False),
        )
        self.assertEqual(page.status, 200)
        self.assertNotIn(b"Type of work", page.body)
        self.assertNotIn(b"Areas of experience", page.body)
        self.assertNotIn(b"Role responsibility", page.body)
        self.assertNotIn(b"Suggested from your experience", page.body)
        self.assertNotIn(b"Choose any that you want to add to your Job Interests.", page.body)
        self.assertNotIn(
            b"name='preference_job_interests_customer_support' value='selected'>",
            page.body,
        )
        self.assertNotIn(
            b"name='preference_job_interests_ai_training' value='selected'>",
            page.body,
        )
        self.assertIn(b"How your preferences affect matches", page.body)
        self.assertIn(b"Your background works differently.", page.body)

        initial = self.integration._processing.vault.get(reference, self._grant())
        internal_index = next(
            index
            for index, fact in enumerate(initial.review.facts)
            if fact.field_path == "experience.occupational_families"
        )
        injected_form = {
            name: values[0]
            for name, values in parse_qs(
                self._review_body(reference, action="autosave").decode("ascii"),
                keep_blank_values=True,
            ).items()
        }
        injected_form[f"fact_{internal_index}_value"] = "Customer Support"
        injected_form[f"fact_{internal_index}_decision"] = "accept"
        self.assertEqual(
            self._post_review(
                reference,
                urlencode(injected_form).encode("ascii"),
            ).status,
            400,
        )
        self.assertEqual(
            self.integration._processing.vault.get(
                reference,
                self._grant(),
            ).version,
            initial.version,
        )

        autosaved = self._post_review(
            reference,
            self._review_body(reference, action="autosave"),
        )
        self.assertEqual(autosaved.status, 204)
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(snapshot.review.preference_model["job_interests"], [])
        internal = {
            fact.field_path: (fact.value, fact.decision)
            for fact in snapshot.review.facts
            if fact.field_path
            in {
                "experience.occupational_families",
                "experience.professional_domains",
                "experience.contribution_type",
            }
        }
        self.assertEqual(
            internal,
            {
                "experience.occupational_families": ("Customer Support", "accept"),
                "experience.professional_domains": ("AI Training", "accept"),
                "experience.contribution_type": ("individual contributor", "accept"),
            },
        )

        extraction_calls = tuple(self.adapter.calls)
        self.integration.close()
        self.integration = self._build(self.adapter)
        resumed_reference = self._reference(self._entry_action("continue"))
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        resumed = self.integration._processing.vault.get(
            resumed_reference,
            self._grant(),
        )
        self.assertEqual(resumed.review.preference_model["job_interests"], [])
        saved = self._save(
            resumed_reference,
            self._save_body(resumed_reference),
        )
        self.assertEqual(saved.status, 303)
        with self._database() as connection:
            profile = json.loads(
                connection.execute(
                    "SELECT structured_profile_json FROM product_profile_revisions"
                ).fetchone()[0]
            )
        self.assertEqual(
            profile["experience"]["occupational_families"],
            ["Customer Support"],
        )
        self.assertEqual(
            profile["experience"]["professional_domains"],
            ["AI Training"],
        )
        self.assertEqual(
            profile["experience"]["contribution_type"],
            "individual contributor",
        )
        self.assertEqual(
            profile["preferences"]["preference_model"]["job_interests"],
            [],
        )
        inferred_sources = {
            source["field_path"]: source
            for source in profile["provenance"]["field_sources"]
            if source["field_path"].startswith(
                (
                    "experience.occupational_families",
                    "experience.professional_domains",
                    "experience.contribution_type",
                )
            )
        }
        self.assertEqual(
            set(inferred_sources),
            {
                "experience.occupational_families[0]",
                "experience.professional_domains[0]",
                "experience.contribution_type",
            },
        )
        for source in inferred_sources.values():
            self.assertEqual(source["source_kind"], "resume_extraction")
            self.assertFalse(source["explicit"])

    def test_typed_collections_autosave_resume_and_persist_without_fabricated_evidence(self):
        reference = self._reference(self._upload())
        page = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference}),
            self._headers(origin=False),
        )
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        skill_index = next(
            index
            for index, fact in enumerate(snapshot.review.facts)
            if fact.field_path == "skills.normalized"
        )
        self.assertIn(b"data-review-collection='skills'", page.body)
        self.assertNotIn(f"name='fact_{skill_index}_value'".encode(), page.body)

        added = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                use_collections=True,
                collection_overrides={
                    "review_collection_skills_1_value": "SQL",
                    "review_collection_job_titles_0_value": "Customer Support Specialist",
                    "review_collection_languages_0_language": "Portuguese",
                    "review_collection_languages_0_proficiency": "",
                    "review_collection_languages_0_locale": "Brazil",
                },
            ),
        )
        self.assertEqual(added.status, 204)
        extraction_calls = tuple(self.adapter.calls)
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(
            [(fact.collection_id, fact.decision) for fact in snapshot.review.user_facts],
            [("skills", "keep"), ("job_titles", "keep"), ("languages", "keep")],
        )
        language = snapshot.review.user_facts[2].value
        self.assertEqual((language.language, language.proficiency, language.locale), (
            "Portuguese", None, "Brazil"
        ))
        with self._database() as connection:
            payload = json.loads(
                connection.execute(
                    "SELECT review_payload_json FROM ai_profile_intake_checkpoints"
                ).fetchone()[0]
            )
        self.assertEqual(len(payload["user_facts"]), 3)
        retained = json.dumps(payload["user_facts"], sort_keys=True).casefold()
        for forbidden in (
            "source_origins",
            "source_attributions",
            "evidence",
            "document_reference",
            "filename",
        ):
            self.assertNotIn(forbidden, retained)

        self.integration.close()
        self.integration = self._build(self.adapter)
        resumed_reference = self._reference(self._entry_action("continue"))
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        resumed = self.integration._processing.vault.get(
            resumed_reference, self._grant()
        )
        self.assertEqual(
            [fact.value if type(fact.value) is str else fact.value.language
             for fact in resumed.review.user_facts],
            ["SQL", "Customer Support Specialist", "Portuguese"],
        )

        edited = self._post_review(
            resumed_reference,
            self._review_body(
                resumed_reference,
                action="autosave",
                use_collections=True,
                collection_overrides={
                    "review_collection_skills_0_value": "Python programming",
                    "review_collection_skills_1_remove": "remove",
                    "review_collection_job_titles_0_value": "Senior Customer Support Specialist",
                },
            ),
        )
        self.assertEqual(edited.status, 204)
        self.integration.close()
        self.integration = self._build(self.adapter)
        final_reference = self._reference(self._entry_action("continue"))
        restored = self.integration._processing.vault.get(
            final_reference, self._grant()
        )
        self.assertEqual(
            next(
                fact.value
                for fact in restored.review.facts
                if fact.field_path == "skills.normalized"
            ),
            "Python programming",
        )
        self.assertEqual(restored.review.user_facts[0].decision, "remove")
        self.assertEqual(
            restored.review.user_facts[1].value,
            "Senior Customer Support Specialist",
        )
        restored_page = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": final_reference}),
            self._headers(origin=False),
        )
        self.assertIn(
            b"name='review_collection_skills_1_remove' value='remove' checked data-collection-remove",
            restored_page.body,
        )
        restore_form = {
            name: values[0]
            for name, values in parse_qs(
                self._review_body(
                    final_reference,
                    action="autosave",
                    use_collections=True,
                ).decode("ascii"),
                keep_blank_values=True,
            ).items()
        }
        restore_form.pop("review_collection_skills_1_remove")
        self.assertEqual(
            self._post_review(
                final_reference,
                urlencode(restore_form).encode("ascii"),
            ).status,
            204,
        )
        self.assertEqual(
            self.integration._processing.vault.get(
                final_reference, self._grant()
            ).review.user_facts[0].decision,
            "keep",
        )
        removed_again = self._post_review(
            final_reference,
            self._review_body(
                final_reference,
                action="autosave",
                use_collections=True,
                collection_overrides={
                    "review_collection_skills_1_remove": "remove"
                },
            ),
        )
        self.assertEqual(removed_again.status, 204)

        saved = self._post_review(
            final_reference,
            self._review_body(
                final_reference,
                action="save",
                use_collections=True,
            ),
        )
        self.assertEqual(saved.status, 303)
        with self._database() as connection:
            profile = json.loads(
                connection.execute(
                    "SELECT structured_profile_json FROM product_profile_revisions"
                ).fetchone()[0]
            )
        self.assertEqual(profile["skills"]["normalized"], ["Python programming"])
        self.assertNotIn("SQL", profile["skills"]["normalized"])
        self.assertIn(
            "Senior Customer Support Specialist",
            profile["experience"]["job_titles"],
        )
        portuguese = next(
            item for item in profile["languages"] if item["language"] == "Portuguese"
        )
        self.assertEqual(portuguese.get("proficiency"), "unknown")
        self.assertFalse(portuguese["proficiency_explicit"])
        self.assertNotIn("evidence", portuguese)
        self.assertTrue(
            any(
                source["source_kind"] == "user_confirmation"
                and source["field_path"].startswith("languages[")
                for source in profile["provenance"]["field_sources"]
            )
        )

    def test_background_expertise_autosaves_resumes_and_finalizes_through_existing_fields(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_total_years=True,
            include_background_review=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        original_preferences = snapshot.review.preference_model
        page = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference}),
            self._headers(origin=False),
        )
        self.assertEqual(page.status, 200)
        self.assertEqual(page.body.count(b"Skills and areas of expertise</h3>"), 1)
        self.assertIn(b"Customer experience", page.body)
        self.assertIn(b"Search quality", page.body)
        self.assertIn(b"Approx. years of professional experience", page.body)
        self.assertNotIn(b"Overall career stage", page.body)
        self.assertNotIn(b"Industry background", page.body)
        self.assertNotIn(b"Business services", page.body)
        self.assertNotIn(b"Technology services", page.body)
        self.assertNotIn(b"Suggested from your background", page.body)
        self.assertNotIn(b"Use this stage", page.body)
        self.assertNotIn(b">Keep</span>", page.body)
        self.assertNotIn(b"Add this to your profile?", page.body)
        after_get = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(after_get.version, snapshot.version)
        self.assertEqual(
            {
                fact.value: fact.decision
                for fact in after_get.review.facts
                if fact.field_path == "experience.specialties"
            },
            {"Customer experience": "pending", "Search quality": "pending"},
        )
        preference_index = next(
            index
            for index, fact in enumerate(snapshot.review.facts)
            if fact.field_path == "preferences.remote"
        )
        self.assertNotIn(f"name='fact_{preference_index}_value'".encode(), page.body)

        unsafe_edit = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                use_collections=True,
                collection_overrides={
                    "review_collection_skills_1_value": "Client experience",
                },
            ),
        )
        self.assertEqual(unsafe_edit.status, 400)

        ordinary_autosave = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                use_collections=True,
                confirm_background=False,
            ),
        )
        self.assertEqual(ordinary_autosave.status, 204)
        after_ordinary_autosave = self.integration._processing.vault.get(
            reference,
            self._grant(),
        )
        self.assertEqual(
            {
                fact.value: fact.decision
                for fact in after_ordinary_autosave.review.facts
                if fact.field_path == "experience.specialties"
            },
            {"Customer experience": "pending", "Search quality": "pending"},
        )
        self.assertEqual(
            next(
                fact.decision
                for fact in after_ordinary_autosave.review.facts
                if fact.field_path == "experience.industries"
            ),
            "reject",
        )
        self.assertEqual(
            next(
                fact.decision
                for fact in after_ordinary_autosave.review.facts
                if fact.field_path == "experience.seniority"
            ),
            "reject",
        )

        saved_progress = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                use_collections=True,
                collection_overrides={
                    "review_collection_skills_2_remove": "remove",
                    "review_collection_skills_3_value": "SEO",
                },
                confirm_background=True,
            ),
        )
        self.assertEqual(saved_progress.status, 204)
        extraction_calls = tuple(self.adapter.calls)

        self.integration.close()
        self.integration = self._build(self.adapter)
        resumed_reference = self._reference(self._entry_action("continue"))
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        resumed = self.integration._processing.vault.get(
            resumed_reference,
            self._grant(),
        )
        self.assertEqual(resumed.review.preference_model, original_preferences)
        self.assertEqual(
            [(fact.field_path, fact.value, fact.decision) for fact in resumed.review.user_facts],
            [("skills.normalized", "SEO", "keep")],
        )
        self.assertEqual(
            {
                fact.value: fact.decision
                for fact in resumed.review.facts
                if fact.field_path == "experience.specialties"
            },
            {"Customer experience": "accept", "Search quality": "reject"},
        )
        self.assertEqual(
            next(
                fact.decision
                for fact in resumed.review.facts
                if fact.field_path == "experience.industries"
            ),
            "reject",
        )
        self.assertEqual(
            next(
                fact.decision
                for fact in resumed.review.facts
                if fact.field_path == "experience.seniority"
            ),
            "reject",
        )

        finalized = self._post_review(
            resumed_reference,
            self._review_body(
                resumed_reference,
                action="save",
                use_collections=True,
            ),
        )
        self.assertEqual(finalized.status, 303)
        with self._database() as connection:
            profile = json.loads(
                connection.execute(
                    "SELECT structured_profile_json FROM product_profile_revisions"
                ).fetchone()[0]
            )
        self.assertEqual(profile["skills"]["normalized"], ["Python", "SEO"])
        self.assertEqual(profile["experience"]["specialties"], ["Customer experience"])
        self.assertEqual(profile["experience"].get("industries", []), [])
        self.assertEqual(profile["experience"]["seniority"], "unknown")
        self.assertEqual(profile["experience"]["total_years"], 6)
        self.assertEqual(
            profile["experience"]["job_titles"],
            ["Customer Support Specialist"],
        )
        self.assertEqual(
            profile["experience"]["recent_roles"],
            ["Customer Support Specialist"],
        )
        self.assertEqual(
            profile["preferences"]["preference_model"],
            original_preferences,
        )
        self.assertTrue(
            any(
                source["source_kind"] == "user_confirmation"
                and source["field_path"] == "skills.normalized[1]"
                for source in profile["provenance"]["field_sources"]
            )
        )
        self.assertTrue(
            any(
                source["source_kind"] == "resume_extraction"
                and source["field_path"] == "experience.specialties[0]"
                for source in profile["provenance"]["field_sources"]
            )
        )
        self.assertFalse(
            any(
                source["source_kind"] == "user_confirmation"
                and source["field_path"] == "experience.specialties[0]"
                for source in profile["provenance"]["field_sources"]
            )
        )
        self.assertFalse(
            any(
                source["field_path"] == "preferences.remote"
                and source["source_kind"] == "resume_extraction"
                for source in profile["provenance"]["field_sources"]
            )
        )

    def test_removed_expertise_is_hidden_and_single_undo_restores_server_owned_state(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_background_review=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        initial = self.integration._processing.vault.get(reference, self._grant())
        initial_fields = _collection_form_values_for_review(initial.review)
        search_key = next(
            name
            for name, value in initial_fields.items()
            if name.startswith("review_collection_skills_")
            and name.endswith("_value")
            and value == "Search quality"
        )
        search_index = int(search_key.split("_")[-2])

        removed = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                use_collections=True,
                collection_overrides={
                    f"review_collection_skills_{search_index}_remove": "remove"
                },
                confirm_background=False,
            ),
        )
        self.assertEqual(removed.status, 204)
        after_remove = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(
            next(
                fact.decision
                for fact in after_remove.review.facts
                if fact.field_path == "experience.specialties"
                and fact.value == "Search quality"
            ),
            "reject",
        )
        page = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference}),
            self._headers(origin=False),
        )
        skills_start = page.body.index(b"data-review-collection='skills'")
        skills_end = page.body.index(b"</fieldset>", skills_start)
        skills_markup = page.body[skills_start:skills_end]
        hidden_item = (
            f"data-expertise-item data-index='{search_index}' hidden aria-hidden=true"
        ).encode()
        self.assertIn(hidden_item, skills_markup)
        self.assertNotIn(b"Restore", skills_markup)
        self.assertIn(b"data-expertise-undo", skills_markup)
        self.assertIn(b"Reset to Wahojobs suggestions", skills_markup)
        self.assertIn(
            b"data-section-reset-open aria-expanded='false' "
            b"aria-controls='expertise-reset-confirm'",
            skills_markup,
        )

        extraction_calls = tuple(self.adapter.calls)
        self.integration.close()
        self.integration = self._build(self.adapter)
        resumed_reference = self._reference(self._entry_action("continue"))
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        resumed_page = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE
            + "?"
            + urlencode({"draft": resumed_reference}),
            self._headers(origin=False),
        )
        self.assertIn(hidden_item, resumed_page.body)

        undo_form = {
            name: values[0]
            for name, values in parse_qs(
                self._review_body(
                    resumed_reference,
                    action="autosave",
                    use_collections=True,
                    confirm_background=False,
                ).decode("ascii"),
                keep_blank_values=True,
            ).items()
        }
        undo_form.pop(f"review_collection_skills_{search_index}_remove")
        self.assertEqual(
            self._post_review(
                resumed_reference,
                urlencode(undo_form).encode("ascii"),
            ).status,
            204,
        )
        after_undo = self.integration._processing.vault.get(
            resumed_reference, self._grant()
        )
        restored = next(
            fact
            for fact in after_undo.review.facts
            if fact.field_path == "experience.specialties"
            and fact.value == "Search quality"
        )
        self.assertEqual(restored.decision, "accept")
        self.assertFalse(restored.explicit)
        self.assertFalse(restored.candidate_edited)
        self.assertEqual(
            list(_collection_form_values_for_review(after_undo.review).values()).count(
                "Search quality"
            ),
            1,
        )

    def test_dummy_expertise_removals_stay_clean_and_reset_restores_checkpoint_baseline(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_total_years=True,
            include_background_review=True,
            include_country=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        initial = self.integration._processing.vault.get(reference, self._grant())
        initial_name = next(
            fact.value
            for fact in initial.review.facts
            if fact.field_path == "identity.display_name"
        )
        initial_preferences = initial.review.preference_model
        initial_fields = _collection_form_values_for_review(initial.review)
        initial_values = {
            value
            for name, value in initial_fields.items()
            if name.startswith("review_collection_skills_")
            and name.endswith("_value")
        }
        next_index = 1 + max(
            int(name.split("_")[-2])
            for name in initial_fields
            if name.startswith("review_collection_skills_")
            and name.endswith("_value")
        )
        additions = ("lalala", "test thing", "dummy expertise", "SEO")
        added = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                use_collections=True,
                collection_overrides={
                    f"review_collection_skills_{next_index + offset}_value": value
                    for offset, value in enumerate(additions)
                },
                confirm_background=False,
            ),
        )
        self.assertEqual(added.status, 204)
        removed = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                use_collections=True,
                collection_overrides={
                    f"review_collection_skills_{next_index + offset}_remove": "remove"
                    for offset in range(3)
                },
                confirm_background=False,
            ),
        )
        self.assertEqual(removed.status, 204)
        page = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference}),
            self._headers(origin=False),
        )
        skills_start = page.body.index(b"data-review-collection='skills'")
        skills_end = page.body.index(b"</fieldset>", skills_start)
        skills_markup = page.body[skills_start:skills_end]
        for offset, value in enumerate(additions[:3]):
            self.assertIn(
                (
                    f"data-expertise-item data-index='{next_index + offset}' "
                    "hidden aria-hidden=true"
                ).encode(),
                skills_markup,
            )
            self.assertIn(value.encode(), skills_markup)
        self.assertNotIn(b"Restore", skills_markup)
        self.assertIn(b"value='SEO'", skills_markup)

        extraction_calls = tuple(self.adapter.calls)
        self.integration.close()
        self.integration = self._build(self.adapter)
        reference = self._reference(self._entry_action("continue"))
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        resumed_page = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference}),
            self._headers(origin=False),
        )
        resumed_skills_start = resumed_page.body.index(
            b"data-review-collection='skills'"
        )
        resumed_skills_end = resumed_page.body.index(
            b"</fieldset>", resumed_skills_start
        )
        resumed_skills_markup = resumed_page.body[
            resumed_skills_start:resumed_skills_end
        ]
        for offset in range(3):
            self.assertIn(
                (
                    f"data-expertise-item data-index='{next_index + offset}' "
                    "hidden aria-hidden=true"
                ).encode(),
                resumed_skills_markup,
            )
        self.assertIn(b"value='SEO'", resumed_skills_markup)
        self.assertNotIn(b"Restore", resumed_skills_markup)

        with self._database() as connection:
            checkpoint = json.loads(
                connection.execute(
                    "SELECT review_payload_json FROM ai_profile_intake_checkpoints"
                ).fetchone()[0]
            )
        self.assertEqual(
            {
                item["value"]
                for item in checkpoint["reset_baseline"]
                if item["section_id"] == "expertise"
            },
            initial_values,
        )
        calls_before_reset = tuple(self.adapter.calls)
        reset = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                use_collections=True,
                reset_section="expertise",
                confirm_background=False,
            ),
        )
        self.assertEqual(reset.status, 204)
        self.assertEqual(tuple(self.adapter.calls), calls_before_reset)
        after_reset = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(
            {
                value
                for name, value in _collection_form_values_for_review(
                    after_reset.review
                ).items()
                if name.startswith("review_collection_skills_")
                and name.endswith("_value")
                and not name.replace("_value", "_remove")
                in _collection_form_values_for_review(after_reset.review)
            },
            initial_values,
        )
        self.assertTrue(
            all(
                fact.decision == "remove"
                for fact in after_reset.review.user_facts
                if fact.collection_id == "skills"
            )
        )
        self.assertEqual(
            next(
                fact.value
                for fact in after_reset.review.facts
                if fact.field_path == "identity.display_name"
            ),
            initial_name,
        )
        self.assertEqual(
            next(
                fact.value
                for fact in after_reset.review.facts
                if fact.field_path == "experience.total_years"
            ),
            6,
        )
        self.assertEqual(after_reset.review.preference_model, initial_preferences)
        self.assertEqual(
            {
                fact.value: fact.decision
                for fact in after_reset.review.facts
                if fact.field_path == "experience.specialties"
            },
            {"Customer experience": "pending", "Search quality": "pending"},
        )
        finalized = self._post_review(
            reference,
            self._review_body(
                reference,
                action="save",
                use_collections=True,
            ),
        )
        self.assertEqual(finalized.status, 303)
        with self._database() as connection:
            profile = json.loads(
                connection.execute(
                    "SELECT structured_profile_json FROM product_profile_revisions"
                ).fetchone()[0]
            )
        self.assertEqual(
            set(profile["skills"]["normalized"]),
            {"Python"},
        )
        self.assertEqual(
            set(profile["experience"]["specialties"]),
            {"Customer experience", "Search quality"},
        )
        self.assertNotIn(
            "SEO",
            profile["skills"]["normalized"] + profile["experience"]["specialties"],
        )

    def test_readding_rejected_specialty_stays_user_authored_and_renders_once(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_background_review=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        initial = self.integration._processing.vault.get(reference, self._grant())
        fields = _collection_form_values_for_review(initial.review)
        search_key = next(
            name
            for name, value in fields.items()
            if name.endswith("_value") and value == "Search quality"
        )
        search_index = int(search_key.split("_")[-2])
        next_index = 1 + max(
            int(name.split("_")[-2])
            for name in fields
            if name.startswith("review_collection_skills_")
            and name.endswith("_value")
        )
        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    collection_overrides={
                        f"review_collection_skills_{search_index}_remove": "remove"
                    },
                    confirm_background=False,
                ),
            ).status,
            204,
        )
        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    collection_overrides={
                        f"review_collection_skills_{next_index}_value": "Search quality"
                    },
                    confirm_background=False,
                ),
            ).status,
            204,
        )
        review = self.integration._processing.vault.get(reference, self._grant()).review
        self.assertEqual(
            next(
                fact.decision
                for fact in review.facts
                if fact.field_path == "experience.specialties"
                and fact.value == "Search quality"
            ),
            "reject",
        )
        user_copy = next(
            fact
            for fact in review.user_facts
            if fact.collection_id == "skills" and fact.value == "Search quality"
        )
        self.assertEqual((user_copy.field_path, user_copy.decision), (
            "skills.normalized", "keep"
        ))
        visible_fields = _collection_form_values_for_review(review)
        self.assertEqual(list(visible_fields.values()).count("Search quality"), 1)

        finalized = self._post_review(
            reference,
            self._review_body(
                reference,
                action="save",
                use_collections=True,
            ),
        )
        self.assertEqual(finalized.status, 303)
        with self._database() as connection:
            profile = json.loads(
                connection.execute(
                    "SELECT structured_profile_json FROM product_profile_revisions"
                ).fetchone()[0]
            )
        self.assertIn("Search quality", profile["skills"]["normalized"])
        self.assertNotIn("Search quality", profile["experience"]["specialties"])
        self.assertTrue(
            any(
                source["source_kind"] == "user_confirmation"
                and source["field_path"].startswith("skills.normalized[")
                for source in profile["provenance"]["field_sources"]
            )
        )

    def test_work_history_current_state_undo_readd_and_reset_preserve_both_role_paths(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_total_years=True,
            include_background_review=True,
            include_second_job_title=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        initial = self.integration._processing.vault.get(reference, self._grant())
        initial_preferences = initial.review.preference_model
        self.assertEqual(
            [
                entry["value"]
                for entry in review_collection_entries(initial.review, "job_titles")
                if entry["decision"] != "remove"
            ],
            ["Customer Support Specialist", "Search Quality Evaluator"],
        )
        self.assertEqual(
            [
                (
                    initial.review.facts[entry.fact_index].field_path,
                    entry.value,
                    entry.decision,
                )
                for entry in initial.review.reset_baseline
                if entry.section_id == "work_history"
            ],
            [
                (
                    "experience.job_titles",
                    "Customer Support Specialist",
                    "keep",
                ),
                ("experience.job_titles", "Search Quality Evaluator", "keep"),
                (
                    "experience.recent_roles",
                    "Customer Support Specialist",
                    "keep",
                ),
            ],
        )
        initial_fields = _collection_form_values_for_review(initial.review)
        next_index = 1 + max(
            int(name.split("_")[-2])
            for name in initial_fields
            if name.startswith("review_collection_job_titles_")
            and name.endswith("_value")
        )
        additions = ("lalala", "lili", "test job", "dummy role")
        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    collection_overrides={
                        f"review_collection_job_titles_{next_index + offset}_value": value
                        for offset, value in enumerate(additions)
                    },
                    confirm_background=False,
                ),
            ).status,
            204,
        )
        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    collection_overrides={
                        f"review_collection_job_titles_{next_index + offset}_remove": "remove"
                        for offset in range(len(additions))
                    },
                    confirm_background=False,
                ),
            ).status,
            204,
        )
        page = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference}),
            self._headers(origin=False),
        )
        work_start = page.body.index(b"data-review-collection='job_titles'")
        work_end = page.body.index(b"</fieldset>", work_start)
        work_markup = page.body[work_start:work_end]
        for offset, value in enumerate(additions):
            self.assertIn(
                (
                    f"data-index='{next_index + offset}' hidden aria-hidden=true"
                ).encode(),
                work_markup,
            )
            self.assertIn(value.encode(), work_markup)
        self.assertNotIn(b"Keep item", work_markup)
        self.assertNotIn(b"Restore", work_markup)
        self.assertIn(b"data-work-history-undo", work_markup)
        self.assertIn(
            b"Reset to Wahojobs suggestions",
            work_markup,
        )
        self.assertIn(b"class='collection-remove-action'", work_markup)
        self.assertNotIn(b"class='collection-remove'><input", work_markup)

        # The browser-local Undo submits the current collection with the latest
        # remove marker cleared. The durable authority remains the same closed form.
        undo_form = {
            name: values[0]
            for name, values in parse_qs(
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    confirm_background=False,
                ).decode("ascii"),
                keep_blank_values=True,
            ).items()
        }
        latest_remove = (
            f"review_collection_job_titles_{next_index + len(additions) - 1}_remove"
        )
        undo_form.pop(latest_remove)
        self.assertEqual(
            self._post_review(
                reference,
                urlencode(undo_form).encode("ascii"),
            ).status,
            204,
        )
        after_undo = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(
            next(
                fact.decision
                for fact in after_undo.review.user_facts
                if fact.collection_id == "job_titles" and fact.value == "dummy role"
            ),
            "keep",
        )
        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    collection_overrides={latest_remove: "remove"},
                    confirm_background=False,
                ),
            ).status,
            204,
        )

        # Removing a deduplicated extracted title rejects both compatibility
        # members. Re-adding the same words creates one evidence-free user fact.
        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    collection_overrides={
                        "review_collection_job_titles_0_remove": "remove"
                    },
                    confirm_background=False,
                ),
            ).status,
            204,
        )
        readd_index = next_index + len(additions)
        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    collection_overrides={
                        f"review_collection_job_titles_{readd_index}_value": (
                            "Customer Support Specialist"
                        )
                    },
                    confirm_background=False,
                ),
            ).status,
            204,
        )
        readded = self.integration._processing.vault.get(reference, self._grant())
        original_customer = [
            fact
            for fact in readded.review.facts
            if fact.field_path
            in {"experience.job_titles", "experience.recent_roles"}
            and fact.value == "Customer Support Specialist"
        ]
        self.assertEqual([fact.decision for fact in original_customer], ["remove", "remove"])
        user_customer = next(
            fact
            for fact in readded.review.user_facts
            if fact.collection_id == "job_titles"
            and fact.value == "Customer Support Specialist"
        )
        self.assertEqual(
            (user_customer.field_path, user_customer.decision),
            ("experience.job_titles", "keep"),
        )
        self.assertEqual(
            list(_collection_form_values_for_review(readded.review).values()).count(
                "Customer Support Specialist"
            ),
            1,
        )

        extraction_calls = tuple(self.adapter.calls)
        self.integration.close()
        self.integration = self._build(self.adapter)
        reference = self._reference(self._entry_action("continue"))
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        resumed = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(resumed.review.preference_model, initial_preferences)
        unrelated_before_reset = tuple(
            (fact.field_path, fact.value, fact.decision, fact.candidate_edited)
            for fact in resumed.review.facts
            if fact.field_path
            not in {"experience.job_titles", "experience.recent_roles"}
        )
        with self._database() as connection:
            checkpoint = json.loads(
                connection.execute(
                    "SELECT review_payload_json FROM ai_profile_intake_checkpoints"
                ).fetchone()[0]
            )
        self.assertEqual(
            [
                item["value"]
                for item in checkpoint["reset_baseline"]
                if item["section_id"] == "work_history"
            ],
            [
                "Customer Support Specialist",
                "Search Quality Evaluator",
                "Customer Support Specialist",
            ],
        )

        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    reset_section="work_history",
                    confirm_background=False,
                ),
            ).status,
            204,
        )
        reset = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        self.assertEqual(
            [
                entry["value"]
                for entry in review_collection_entries(reset.review, "job_titles")
                if entry["decision"] != "remove"
            ],
            ["Customer Support Specialist", "Search Quality Evaluator"],
        )
        self.assertTrue(
            all(
                fact.decision == "remove"
                for fact in reset.review.user_facts
                if fact.collection_id == "job_titles"
            )
        )
        self.assertEqual(reset.review.preference_model, initial_preferences)
        self.assertEqual(
            tuple(
                (fact.field_path, fact.value, fact.decision, fact.candidate_edited)
                for fact in reset.review.facts
                if fact.field_path
                not in {"experience.job_titles", "experience.recent_roles"}
            ),
            unrelated_before_reset,
        )

        saved = self._post_review(
            reference,
            self._review_body(
                reference,
                action="save",
                use_collections=True,
            ),
        )
        self.assertEqual(saved.status, 303)
        with self._database() as connection:
            profile = json.loads(
                connection.execute(
                    "SELECT structured_profile_json FROM product_profile_revisions"
                ).fetchone()[0]
            )
        self.assertEqual(
            profile["experience"]["job_titles"],
            ["Customer Support Specialist", "Search Quality Evaluator"],
        )
        self.assertEqual(
            profile["experience"]["recent_roles"],
            ["Customer Support Specialist"],
        )
        for value in additions:
            self.assertNotIn(value, profile["experience"]["job_titles"])

    def test_all_suggested_fact_sections_reset_from_one_checkpoint_baseline_in_isolation(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_total_years=True,
            include_education=True,
            include_background_review=True,
            include_second_job_title=True,
            include_country=True,
            include_languages=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        initial = self.integration._processing.vault.get(reference, self._grant())
        review_target = PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference})
        initial_page = self.integration.handle(
            "GET", review_target, self._headers(origin=False)
        )
        self.assertEqual(initial_page.status, 200)
        self.assertEqual(initial_page.body.count(b"Reset to Wahojobs suggestions"), 6)
        for title in (
            b"Reset profile basics?",
            b"Reset work history?",
            b"Reset education?",
            b"Reset languages?",
            b"Reset skills &amp; expertise?",
            b"Reset professional experience?",
        ):
            self.assertIn(title, initial_page.body)
        step_three = initial_page.body[
            initial_page.body.index(b"id='review-preferences'"):
            initial_page.body.index(b"id='review-finish'")
        ]
        self.assertNotIn(b"Reset to Wahojobs suggestions", step_three)
        self.assertEqual(
            self.integration._processing.vault.get(reference, self._grant()).version,
            initial.version,
        )
        initial_preferences = initial.review.preference_model
        initial_reset_baseline = initial.review.reset_baseline
        initial_facts = {
            (index, fact.field_path): (
                fact.value,
                fact.decision,
                fact.source_attributions,
            )
            for index, fact in enumerate(initial.review.facts)
        }
        indexes = {
            path: next(
                index
                for index, fact in enumerate(initial.review.facts)
                if fact.field_path == path
            )
            for path in (
                "identity.display_name",
                "location.city",
                "experience.total_years",
                "experience.seniority",
                "experience.industries",
            )
        }
        collection_fields = _collection_form_values_for_review(initial.review)
        language_next = 1 + max(
            int(name.split("_")[-2])
            for name in collection_fields
            if name.startswith("review_collection_languages_")
        )
        title_next = 1 + max(
            int(name.split("_")[-2])
            for name in collection_fields
            if name.startswith("review_collection_job_titles_")
        )
        skill_next = 1 + max(
            int(name.split("_")[-2])
            for name in collection_fields
            if name.startswith("review_collection_skills_")
        )
        education_fields = _education_form_values_for_review(initial.review)
        education_next = 1 + max(
            int(name.split("_")[2])
            for name in education_fields
            if name.startswith("review_education_")
        )
        changed = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                use_collections=True,
                use_education=True,
                confirm_background=False,
                changes={
                    indexes["identity.display_name"]: "Marina Example",
                    indexes["location.city"]: "Porto",
                    indexes["experience.total_years"]: "9",
                },
                collection_overrides={
                    "review_collection_job_titles_0_value": "Support Operations Lead",
                    f"review_collection_job_titles_{title_next}_value": "AI Quality Reviewer",
                    "review_collection_languages_0_language": "Portuguese (Brazil)",
                    "review_collection_languages_0_proficiency": "fluent",
                    "review_collection_languages_0_locale": "Brazilian Portuguese",
                    f"review_collection_languages_{language_next}_language": "Spanish",
                    f"review_collection_languages_{language_next}_proficiency": "intermediate",
                    f"review_collection_languages_{language_next}_locale": "",
                    "review_collection_skills_0_remove": "remove",
                    f"review_collection_skills_{skill_next}_value": "SEO",
                },
                education_overrides={
                    "review_education_0_qualification": "Edited degree",
                    f"review_education_{education_next}_kind": "technical",
                    f"review_education_{education_next}_qualification": "Data course",
                    f"review_education_{education_next}_field": "Data quality",
                    f"review_education_{education_next}_institution": "Synthetic Institute",
                    f"review_education_{education_next}_status": "in_progress",
                    f"review_education_{education_next}_completion_year": "",
                },
                preference_overrides={
                    "preference_employment_relationships_mode": "preferences",
                    "preference_employment_relationships_independent_contractor": "selected",
                },
            ),
        )
        self.assertEqual(changed.status, 204)

        cleared_location = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                use_collections=True,
                use_education=True,
                confirm_background=False,
                changes={indexes["location.city"]: ""},
            ),
        )
        self.assertEqual(cleared_location.status, 204)
        extraction_calls = tuple(self.adapter.calls)

        self.integration.close()
        self.integration = self._build(self.adapter)
        reference = self._reference(self._entry_action("continue"))
        resumed = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        self.assertEqual(resumed.review.reset_baseline, initial_reset_baseline)
        self.assertEqual(
            resumed.review.preference_model["employment_relationships"],
            ["independent_contractor"],
        )

        def reset(section_id):
            response = self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    use_education=True,
                    reset_section=section_id,
                    confirm_background=False,
                ),
            )
            self.assertEqual(response.status, 204)
            self.assertEqual(tuple(self.adapter.calls), extraction_calls)
            return self.integration._processing.vault.get(reference, self._grant())

        reset_about = reset("profile_basics")
        for path in ("identity.display_name", "location.city", "location.country"):
            fact = next(item for item in reset_about.review.facts if item.field_path == path)
            original = next(
                value
                for (_index, field_path), value in initial_facts.items()
                if field_path == path
            )
            self.assertEqual((fact.value, fact.decision), original[:2])
            self.assertFalse(fact.candidate_edited)
            self.assertEqual(
                tuple(item.document_kind for item in fact.source_attributions),
                tuple(item.document_kind for item in original[2]),
            )
        self.assertEqual(
            next(
                fact.value
                for fact in reset_about.review.facts
                if fact.field_path == "experience.total_years"
            ),
            9,
        )

        reset_work = reset("work_history")
        self.assertEqual(
            [
                entry["value"]
                for entry in review_collection_entries(reset_work.review, "job_titles")
                if entry["decision"] != "remove"
            ],
            ["Customer Support Specialist", "Search Quality Evaluator"],
        )

        reset_education = reset("education")
        active_education = [
            entry
            for entry in education_entry_values(reset_education.review)
            if entry["decision"] != "remove"
        ]
        self.assertEqual(len(active_education), 1)
        self.assertEqual(
            active_education[0]["value"],
            {
                "kind": "bachelor",
                "qualification": "Bachelor of Business Administration",
                "field": "Business Administration",
                "institution": "Faculdade Horizonte Paulista",
                "status": "completed",
                "completion_year": 2016,
            },
        )
        self.assertEqual(active_education[0]["origin"], "document")
        self.assertEqual(
            reset_education.review.education_entries[0].candidate_edited_components,
            (),
        )
        self.assertEqual(
            len(
                {
                    json.dumps(entry["value"], sort_keys=True)
                    for entry in active_education
                }
            ),
            len(active_education),
        )

        reset_languages = reset("languages")
        active_languages = [
            entry
            for entry in review_collection_entries(reset_languages.review, "languages")
            if entry["decision"] != "remove"
        ]
        self.assertEqual(
            {entry["value"] for entry in active_languages},
            {
                LanguageValue("Portuguese", "native", "Brazil"),
                LanguageValue("English", "professional", None),
            },
        )
        self.assertEqual(
            len({entry["value"] for entry in active_languages}),
            len(active_languages),
        )

        reset_skills = reset("expertise")
        self.assertEqual(
            {
                entry["value"]
                for entry in review_collection_entries(reset_skills.review, "skills")
                if entry["decision"] != "remove"
            },
            {"Python", "Customer experience", "Search quality"},
        )
        hidden_before_years = {
            path: (
                reset_skills.review.facts[indexes[path]].value,
                reset_skills.review.facts[indexes[path]].decision,
            )
            for path in ("experience.seniority", "experience.industries")
        }
        reset_years = reset("professional_experience")
        self.assertEqual(
            reset_years.review.facts[indexes["experience.total_years"]].value,
            6,
        )
        self.assertEqual(
            {
                path: (
                    reset_years.review.facts[indexes[path]].value,
                    reset_years.review.facts[indexes[path]].decision,
                )
                for path in ("experience.seniority", "experience.industries")
            },
            hidden_before_years,
        )
        self.assertEqual(reset_years.review.preference_model, resumed.review.preference_model)
        with self._database() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM ai_profile_import_entitlements"
                ).fetchone()[0],
                "reserved",
            )
        saved = self._post_review(
            reference,
            self._review_body(
                reference,
                action="save",
                use_collections=True,
                use_education=True,
            ),
        )
        self.assertEqual(saved.status, 303)
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        with self._database() as connection:
            profile = json.loads(
                connection.execute(
                    "SELECT structured_profile_json FROM product_profile_revisions"
                ).fetchone()[0]
            )
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM ai_profile_import_entitlements"
                ).fetchone()[0],
                "consumed",
            )
        self.assertEqual(profile["identity"]["display_name"], "Synthetic Candidate")
        self.assertEqual(profile["location"]["city"], "Lisbon")
        self.assertEqual(profile["experience"]["total_years"], 6)
        self.assertEqual(
            profile["experience"]["job_titles"],
            ["Customer Support Specialist", "Search Quality Evaluator"],
        )
        self.assertEqual(len(profile["education"]["entries"]), 1)
        reset_education_sources = {
            item["field_path"]: item
            for item in profile["provenance"]["field_sources"]
            if item["field_path"].startswith("education.entries[")
        }
        self.assertTrue(reset_education_sources)
        self.assertEqual(
            {item["source_kind"] for item in reset_education_sources.values()},
            {"resume_extraction"},
        )
        self.assertFalse(
            reset_education_sources["education.entries[0].kind"]["explicit"]
        )
        self.assertFalse(
            reset_education_sources["education.entries[0].status"]["explicit"]
        )
        self.assertEqual(
            {item["language"] for item in profile["languages"]},
            {"English", "Portuguese"},
        )
        self.assertNotIn("SEO", profile["skills"]["normalized"])

    def test_removed_extracted_and_user_job_titles_remain_absent_after_reload_and_save(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_background_review=True,
            include_second_job_title=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        initial = self.integration._processing.vault.get(reference, self._grant())
        title_fields = {
            name: value
            for name, value in _collection_form_values_for_review(initial.review).items()
            if name.startswith("review_collection_job_titles_")
        }
        search_key = next(
            name
            for name, value in title_fields.items()
            if name.endswith("_value") and value == "Search Quality Evaluator"
        )
        search_index = int(search_key.split("_")[-2])
        next_index = 1 + max(
            int(name.split("_")[-2])
            for name in title_fields
            if name.endswith("_value")
        )
        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    collection_overrides={
                        f"review_collection_job_titles_{next_index}_value": "lalala"
                    },
                    confirm_background=False,
                ),
            ).status,
            204,
        )
        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    collection_overrides={
                        f"review_collection_job_titles_{search_index}_remove": "remove",
                        f"review_collection_job_titles_{next_index}_remove": "remove",
                    },
                    confirm_background=False,
                ),
            ).status,
            204,
        )
        extraction_calls = tuple(self.adapter.calls)
        self.integration.close()
        self.integration = self._build(self.adapter)
        reference = self._reference(self._entry_action("continue"))
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        page = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference}),
            self._headers(origin=False),
        )
        work_start = page.body.index(b"data-review-collection='job_titles'")
        work_end = page.body.index(b"</fieldset>", work_start)
        work_markup = page.body[work_start:work_end]
        self.assertIn(
            f"data-index='{search_index}' hidden aria-hidden=true".encode(),
            work_markup,
        )
        self.assertIn(
            f"data-index='{next_index}' hidden aria-hidden=true".encode(),
            work_markup,
        )
        self.assertNotIn(b"Keep item", work_markup)
        saved = self._post_review(
            reference,
            self._review_body(
                reference,
                action="save",
                use_collections=True,
            ),
        )
        self.assertEqual(saved.status, 303)
        with self._database() as connection:
            profile = json.loads(
                connection.execute(
                    "SELECT structured_profile_json FROM product_profile_revisions"
                ).fetchone()[0]
            )
        self.assertEqual(
            profile["experience"]["job_titles"],
            ["Customer Support Specialist"],
        )
        self.assertEqual(
            profile["experience"]["recent_roles"],
            ["Customer Support Specialist"],
        )
        self.assertNotIn("lalala", profile["experience"]["job_titles"])

    def test_removed_education_and_languages_use_hidden_current_state_presentation(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_education=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    use_education=True,
                    collection_overrides={
                        "review_collection_languages_0_language": "Portuguese",
                        "review_collection_languages_0_proficiency": "native",
                        "review_collection_languages_0_locale": "Brazil",
                    },
                    education_overrides={
                        "review_education_1_kind": "technical",
                        "review_education_1_qualification": "Synthetic Data Course",
                        "review_education_1_field": "Data Quality",
                        "review_education_1_institution": "Synthetic Institute",
                        "review_education_1_status": "in_progress",
                        "review_education_1_completion_year": "2027",
                    },
                    confirm_background=False,
                ),
            ).status,
            204,
        )
        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    use_education=True,
                    collection_overrides={
                        "review_collection_languages_0_remove": "remove",
                    },
                    education_overrides={
                        "review_education_0_remove": "remove",
                        "review_education_1_remove": "remove",
                    },
                    confirm_background=False,
                ),
            ).status,
            204,
        )
        page = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference}),
            self._headers(origin=False),
        )
        language_start = page.body.index(b"data-review-collection='languages'")
        language_end = page.body.index(b"</fieldset>", language_start)
        language_markup = page.body[language_start:language_end]
        education_start = page.body.index(b"data-review-collection='education'")
        education_end = page.body.index(b"</fieldset>", education_start)
        education_markup = page.body[education_start:education_end]
        self.assertIn(b"data-index='0' hidden aria-hidden=true", language_markup)
        self.assertIn(b"data-index='0' hidden aria-hidden=true", education_markup)
        self.assertIn(b"Nothing listed yet", language_markup)
        self.assertIn(b"Nothing listed yet", education_markup)
        self.assertNotIn(b"Keep item", language_markup)
        self.assertNotIn(b"Keep entry", education_markup)
        self.assertNotIn(b"Restore", language_markup + education_markup)
        self.assertIn(b"class='collection-remove-action'", language_markup)
        self.assertIn(b"class='collection-remove-action'", education_markup)
        saved = self._post_review(
            reference,
            self._review_body(
                reference,
                action="save",
                use_collections=True,
                use_education=True,
                confirm_background=False,
            ),
        )
        self.assertEqual(saved.status, 303)
        with self._database() as connection:
            profile = json.loads(
                connection.execute(
                    "SELECT structured_profile_json FROM product_profile_revisions"
                ).fetchone()[0]
            )
        self.assertNotIn("entries", profile["education"])

    def test_real_review_route_reloads_preserve_durable_state_without_new_import(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_total_years=True,
            include_background_review=True,
            include_country=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        target = PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference})
        extraction_calls = tuple(self.adapter.calls)

        initial = self.integration._processing.vault.get(reference, self._grant())
        initial_version = initial.version
        initial_decisions = tuple(
            (fact.field_path, fact.value, fact.decision)
            for fact in initial.review.facts
        )
        first_get = self.integration.handle(
            "GET",
            target,
            self._headers(origin=False),
        )
        self.assertEqual(first_get.status, 200)
        after_first_get = self.integration._processing.vault.get(
            reference, self._grant()
        )
        self.assertEqual(after_first_get.version, initial_version)
        self.assertEqual(
            tuple(
                (fact.field_path, fact.value, fact.decision)
                for fact in after_first_get.review.facts
            ),
            initial_decisions,
        )

        indexes = {
            fact.field_path: index
            for index, fact in enumerate(after_first_get.review.facts)
        }
        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    changes={indexes["identity.display_name"]: "Marina Reload Example"},
                    review_step="review-found",
                    confirm_background=False,
                ),
            ).status,
            204,
        )
        step_one_reload = self.integration.handle(
            "GET", target, self._headers(origin=False)
        )
        self.assertEqual(step_one_reload.status, 200)
        self.assertIn(b"value='Marina Reload Example'", step_one_reload.body)

        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    review_step="review-suggestions",
                    confirm_background=False,
                    confirm_profile_basics=True,
                ),
            ).status,
            204,
        )
        untouched_step_two = self.integration.handle(
            "GET", target, self._headers(origin=False)
        )
        self.assertEqual(untouched_step_two.status, 200)
        self.assertIn(b"value='review-suggestions'", untouched_step_two.body)
        self.assertIn(b"Search quality", untouched_step_two.body)

        current = self.integration._processing.vault.get(reference, self._grant())
        collection_fields = _collection_form_values_for_review(current.review)
        search_key = next(
            name
            for name, value in collection_fields.items()
            if name.startswith("review_collection_skills_")
            and name.endswith("_value")
            and value == "Search quality"
        )
        search_index = int(search_key.split("_")[-2])
        next_index = 1 + max(
            int(name.split("_")[-2])
            for name in collection_fields
            if name.startswith("review_collection_skills_")
            and name.endswith("_value")
        )
        years_index = next(
            index
            for index, fact in enumerate(current.review.facts)
            if fact.field_path == "experience.total_years"
        )
        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    changes={years_index: "7"},
                    collection_overrides={
                        f"review_collection_skills_{search_index}_remove": "remove",
                        f"review_collection_skills_{next_index}_value": "SEO",
                    },
                    review_step="review-suggestions",
                    confirm_background=False,
                ),
            ).status,
            204,
        )
        edited_step_two = self.integration.handle(
            "GET", target, self._headers(origin=False)
        )
        self.assertEqual(edited_step_two.status, 200)
        self.assertIn(
            (
                f"data-expertise-item data-index='{search_index}' "
                "hidden aria-hidden=true"
            ).encode(),
            edited_step_two.body,
        )
        self.assertIn(b"value='SEO'", edited_step_two.body)
        self.assertIn(b"value='7'", edited_step_two.body)

        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    collection_overrides={
                        f"review_collection_skills_{next_index}_remove": "remove"
                    },
                    review_step="review-suggestions",
                    confirm_background=False,
                ),
            ).status,
            204,
        )
        removed_user_skill_reload = self.integration.handle(
            "GET", target, self._headers(origin=False)
        )
        self.assertEqual(removed_user_skill_reload.status, 200)
        self.assertIn(
            (
                f"data-expertise-item data-index='{next_index}' "
                "hidden aria-hidden=true"
            ).encode(),
            removed_user_skill_reload.body,
        )

        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    changes={years_index: ""},
                    review_step="review-suggestions",
                    confirm_background=False,
                ),
            ).status,
            204,
        )
        cleared_years_reload = self.integration.handle(
            "GET", target, self._headers(origin=False)
        )
        self.assertEqual(cleared_years_reload.status, 200)
        self.assertIn(
            f"name='fact_{years_index}_value' value=''".encode(),
            cleared_years_reload.body,
        )
        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    changes={years_index: "7"},
                    review_step="review-suggestions",
                    confirm_background=False,
                ),
            ).status,
            204,
        )

        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    reset_section="expertise",
                    review_step="review-suggestions",
                    confirm_background=False,
                ),
            ).status,
            204,
        )
        reset_reload = self.integration.handle(
            "GET", target, self._headers(origin=False)
        )
        self.assertEqual(reset_reload.status, 200)
        after_reset = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(
            {
                entry["value"]
                for entry in review_collection_entries(after_reset.review, "skills")
                if entry["decision"] in {"keep", "pending"}
            },
            {"Python", "Customer experience", "Search quality"},
        )
        self.assertEqual(
            next(
                fact.value
                for fact in after_reset.review.facts
                if fact.field_path == "experience.total_years"
            ),
            7,
        )
        self.assertEqual(
            next(
                fact.value
                for fact in after_reset.review.facts
                if fact.field_path == "identity.display_name"
            ),
            "Marina Reload Example",
        )

        self.assertEqual(
            self._post_review(
                reference,
                self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    review_step="review-preferences",
                    confirm_background=True,
                ),
            ).status,
            204,
        )
        step_three_reload = self.integration.handle(
            "GET", target, self._headers(origin=False)
        )
        self.assertEqual(step_three_reload.status, 200)
        self.assertIn(b"value='review-preferences'", step_three_reload.body)
        self.assertIn(b"What are you looking for?", step_three_reload.body)
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        with self._database() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_profile_import_attempts"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_profile_intake_checkpoints"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                tuple(
                    connection.execute(
                        "SELECT state,consumed_at FROM ai_profile_import_entitlements"
                    ).fetchone()
                ),
                ("reserved", None),
            )

        finalized = self._post_review(
            reference,
            self._review_body(
                reference,
                action="save",
                use_collections=True,
            ),
        )
        self.assertEqual(finalized.status, 303)
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        with self._database() as connection:
            profile = json.loads(
                connection.execute(
                    "SELECT structured_profile_json FROM product_profile_revisions"
                ).fetchone()[0]
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_profile_intake_checkpoints"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM ai_profile_import_entitlements"
                ).fetchone()[0],
                "consumed",
            )
        self.assertEqual(profile["identity"]["display_name"], "Marina Reload Example")
        self.assertEqual(profile["experience"]["total_years"], 7)
        self.assertEqual(profile["skills"]["normalized"], ["Python"])
        self.assertEqual(
            set(profile["experience"]["specialties"]),
            {"Customer experience", "Search quality"},
        )
        self.assertNotIn("SEO", profile["skills"]["normalized"])

    def test_existing_confirmed_hidden_background_checkpoint_values_are_preserved(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_background_review=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        hidden_paths = {"experience.industries", "experience.seniority"}
        confirmed_review = update_editable_review(
            snapshot.review,
            tuple(review_value_for_form(fact.value) for fact in snapshot.review.facts),
            tuple(
                "accept" if fact.field_path in hidden_paths else fact.decision
                for fact in snapshot.review.facts
            ),
            dict(snapshot.review.user_inputs),
        )
        state, updated = self.integration._processing.update(
            reference,
            self._grant(),
            expected_version=snapshot.version,
            review=confirmed_review,
        )
        self.assertEqual(state, "updated")

        autosaved = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                use_collections=True,
                confirm_background=False,
            ),
        )
        self.assertEqual(autosaved.status, 204)
        preserved = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(preserved.version, updated.version + 1)
        self.assertEqual(
            {
                fact.field_path: fact.decision
                for fact in preserved.review.facts
                if fact.field_path in hidden_paths
            },
            {
                "experience.industries": "accept",
                "experience.seniority": "accept",
            },
        )

        finalized = self._post_review(
            reference,
            self._review_body(
                reference,
                action="save",
                use_collections=True,
            ),
        )
        self.assertEqual(finalized.status, 303)
        with self._database() as connection:
            profile = json.loads(
                connection.execute(
                    "SELECT structured_profile_json FROM product_profile_revisions"
                ).fetchone()[0]
            )
        self.assertEqual(
            profile["experience"]["industries"],
            ["Business services", "Technology services"],
        )
        self.assertEqual(profile["experience"]["seniority"], "senior")

    def test_typed_collections_reject_duplicates_invalid_values_limits_and_paths(self):
        reference = self._reference(self._upload())
        invalid_overrides = (
            {
                "review_collection_skills_1_value": "python",
            },
            {
                "review_collection_languages_0_language": "Portuguese",
                "review_collection_languages_0_proficiency": "expert",
                "review_collection_languages_0_locale": "",
            },
            {
                "review_collection_languages_0_language": "Portuguese",
                "review_collection_languages_0_proficiency": "",
                "review_collection_languages_0_locale": "Brazil",
                "review_collection_languages_1_language": "portuguese",
                "review_collection_languages_1_proficiency": "native",
                "review_collection_languages_1_locale": "Portugal",
            },
            {"review_collection_skills_0_field_path": "constraints.work_authorization"},
            {"review_collection_job_titles_0_value": "x" * 513},
            {
                f"review_collection_skills_{index}_value": f"Skill {index}"
                for index in range(1, 97)
            },
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=tuple(overrides)[:2]):
                body = self._review_body(
                    reference,
                    action="autosave",
                    use_collections=True,
                    collection_overrides=overrides,
                )
                self.assertEqual(self._post_review(reference, body).status, 400)
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(snapshot.version, 1)
        self.assertEqual(snapshot.review.user_facts, ())

    def test_partial_collection_item_fails_closed_without_replacing_valid_checkpoint(self):
        reference = self._reference(self._upload())
        valid = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                use_collections=True,
                changes={0: "Last Valid Candidate"},
            ),
        )
        self.assertEqual(valid.status, 204)
        before = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(before.version, 2)
        with self._database() as connection:
            row = connection.execute(
                "SELECT row_version,review_payload_json FROM ai_profile_intake_checkpoints"
            ).fetchone()
            durable_before = (row["row_version"], row["review_payload_json"])

        partial = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                use_collections=True,
                collection_overrides={
                    "review_collection_languages_0_language": "",
                    "review_collection_languages_0_proficiency": "native",
                    "review_collection_languages_0_locale": "",
                },
            ),
        )
        self.assertEqual(partial.status, 400)
        after = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(after.version, 2)
        self.assertEqual(after.review.facts[0].value, "Last Valid Candidate")
        with self._database() as connection:
            row = connection.execute(
                "SELECT row_version,review_payload_json FROM ai_profile_intake_checkpoints"
            ).fetchone()
            self.assertEqual(
                (row["row_version"], row["review_payload_json"]),
                durable_before,
            )

    def test_structured_education_autosaves_resumes_and_persists_legacy_shadows(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_education=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(len(snapshot.review.education_entries), 1)
        paired_indexes = managed_education_fact_indexes(snapshot.review)
        self.assertEqual(len(paired_indexes), 6)
        page = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference}),
            self._headers(origin=False),
        )
        self.assertIn(b"data-review-collection='education'", page.body)
        self.assertIn(b"Bachelor of Business Administration", page.body)
        completion_index = next(
            index
            for index, fact in enumerate(snapshot.review.facts)
            if fact.field_path == "education.completion_status"
        )
        self.assertNotIn(
            f"name='fact_{completion_index}_value'".encode(),
            page.body,
        )

        added = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                use_education=True,
                education_overrides={
                    "review_education_0_institution": "Faculdade Horizonte",
                    "review_education_1_kind": "technical",
                    "review_education_1_qualification": "Data Analytics Certificate",
                    "review_education_1_field": "Data Analytics",
                    "review_education_1_institution": "Instituto Futuro",
                    "review_education_1_status": "in_progress",
                    "review_education_1_completion_year": "2027",
                },
            ),
        )
        self.assertEqual(added.status, 204)
        extraction_calls = tuple(self.adapter.calls)
        with self._database() as connection:
            payload = json.loads(
                connection.execute(
                    "SELECT review_payload_json FROM ai_profile_intake_checkpoints"
                ).fetchone()[0]
            )
        self.assertEqual(len(payload["education_entries"]), 2)
        self.assertEqual(
            payload["education_entries"][0]["extraction_components"],
            [
                "completion_year",
                "field",
                "institution",
                "kind",
                "qualification",
                "status",
            ],
        )
        self.assertEqual(
            payload["education_entries"][0]["candidate_edited_components"],
            ["institution"],
        )
        self.assertEqual(payload["education_entries"][1]["extraction_components"], [])
        serialized = json.dumps(payload["education_entries"], sort_keys=True).casefold()
        for forbidden in (
            "evidence",
            "document_reference",
            "filename",
            "prompt",
            "provider",
        ):
            self.assertNotIn(forbidden, serialized)
        legacy_payload = json.loads(json.dumps(payload))
        for item in legacy_payload["education_entries"]:
            item.pop("extraction_components", None)
            item.pop("candidate_edited_components", None)
        legacy_payload_json = json.dumps(
            legacy_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        legacy_review = hydrate_profile_intake_checkpoint(legacy_payload_json)
        self.assertTrue(
            all(
                detail["source_kind"] == "user_confirmation"
                for fields in education_entry_field_authorities(legacy_review)
                for detail in fields.values()
            )
        )
        self.assertEqual(
            serialize_profile_intake_checkpoint(
                legacy_review,
                review_step=legacy_payload["review_step"],
            ),
            legacy_payload_json,
        )
        tampered_payload = json.loads(json.dumps(payload))
        tampered_payload["education_entries"][0]["candidate_edited_components"] = [
            "arbitrary_field_path"
        ]
        with self.assertRaises(ProfileIntakeError):
            hydrate_profile_intake_checkpoint(
                json.dumps(
                    tampered_payload,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )

        removed = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                use_education=True,
                education_overrides={"review_education_1_remove": "remove"},
            ),
        )
        self.assertEqual(removed.status, 204)
        self.integration.close()
        self.integration = self._build(self.adapter)
        resumed_reference = self._reference(self._entry_action("continue"))
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        resumed = self.integration._processing.vault.get(
            resumed_reference,
            self._grant(),
        )
        self.assertEqual(len(resumed.review.education_entries), 2)
        self.assertEqual(resumed.review.education_entries[1].decision, "remove")
        self.assertEqual(
            resumed.review.education_entries[0].institution,
            "Faculdade Horizonte",
        )
        self.assertEqual(
            resumed.review.education_entries[0].candidate_edited_components,
            ("institution",),
        )

        final_form = {
            name: values[0]
            for name, values in parse_qs(
                self._review_body(
                    resumed_reference,
                    action="save",
                    use_education=True,
                ).decode("ascii"),
                keep_blank_values=True,
            ).items()
        }
        final_form.pop("review_education_1_remove")
        saved = self._post_review(
            resumed_reference,
            urlencode(final_form).encode("ascii"),
        )
        self.assertEqual(saved.status, 303)
        with self._database() as connection:
            profile = json.loads(
                connection.execute(
                    "SELECT structured_profile_json FROM product_profile_revisions"
                ).fetchone()[0]
            )
        self.assertEqual(len(profile["education"]["entries"]), 2)
        self.assertEqual(profile["education"]["completion_status"], "unknown")
        self.assertEqual(profile["education"]["education_level"], "not_specified")
        self.assertEqual(profile["education"]["graduation_years"], [2016, 2027])
        self.assertEqual(
            profile["education"]["institutions"],
            ["Faculdade Horizonte", "Instituto Futuro"],
        )
        self.assertEqual(
            set(profile["education"]["entries"][1]),
            {"kind", "qualification", "field", "institution", "status", "completion_year"},
        )
        education_sources = {
            source["field_path"]: source
            for source in profile["provenance"]["field_sources"]
            if source["field_path"].startswith("education.entries[")
        }
        self.assertEqual(
            education_sources["education.entries[0].institution"]["source_kind"],
            "user_correction",
        )
        for field_name in (
            "kind",
            "qualification",
            "field",
            "status",
            "completion_year",
        ):
            self.assertEqual(
                education_sources[f"education.entries[0].{field_name}"]["source_kind"],
                "resume_extraction",
            )
        for field_name in (
            "kind",
            "qualification",
            "field",
            "institution",
            "status",
            "completion_year",
        ):
            self.assertEqual(
                education_sources[f"education.entries[1].{field_name}"]["source_kind"],
                "user_confirmation",
            )
        self.assertFalse(
            education_sources["education.entries[0].kind"]["explicit"]
        )
        self.assertFalse(
            education_sources["education.entries[0].status"]["explicit"]
        )

    def test_untouched_structured_education_keeps_document_field_authority(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_education=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        extraction_calls = tuple(self.adapter.calls)
        saved = self._post_review(
            reference,
            self._review_body(
                reference,
                action="save",
                use_education=True,
            ),
        )
        self.assertEqual(saved.status, 303)
        self.assertEqual(tuple(self.adapter.calls), extraction_calls)
        with self._database() as connection:
            profile = json.loads(
                connection.execute(
                    "SELECT structured_profile_json FROM product_profile_revisions"
                ).fetchone()[0]
            )
        sources = {
            item["field_path"]: item
            for item in profile["provenance"]["field_sources"]
            if item["field_path"].startswith("education.entries[")
        }
        self.assertEqual(len(profile["education"]["entries"]), 1)
        self.assertEqual({item["source_kind"] for item in sources.values()}, {"resume_extraction"})
        self.assertNotIn("user_confirmation", {item["source_kind"] for item in sources.values()})
        self.assertFalse(sources["education.entries[0].kind"]["explicit"])
        self.assertFalse(sources["education.entries[0].status"]["explicit"])
        self.assertTrue(sources["education.entries[0].qualification"]["explicit"])
        self.assertTrue(
            all(
                set(item)
                == {
                    "field_path",
                    "path_version",
                    "source_ordinals",
                    "source_kind",
                    "explicit",
                }
                for item in sources.values()
            )
        )

    def test_structured_education_rejects_duplicates_invalid_values_limits_and_paths(self):
        self.integration.close()
        self.adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            include_education=True,
        )
        self.integration = self._build(self.adapter)
        reference = self._reference(self._upload())
        base_second = {
            "review_education_1_kind": "bachelor",
            "review_education_1_qualification": "bachelor of business administration",
            "review_education_1_field": "business administration",
            "review_education_1_institution": "faculdade horizonte paulista",
            "review_education_1_status": "completed",
            "review_education_1_completion_year": "2016",
        }
        oversize = {}
        for index in range(1, 25):
            oversize.update(
                {
                    f"review_education_{index}_kind": "technical",
                    f"review_education_{index}_qualification": f"Course {index}",
                    f"review_education_{index}_field": "Data Analytics",
                    f"review_education_{index}_institution": "Instituto Futuro",
                    f"review_education_{index}_status": "in_progress",
                    f"review_education_{index}_completion_year": "2027",
                }
            )
        invalid_overrides = (
            base_second,
            {**base_second, "review_education_1_kind": "university"},
            {**base_second, "review_education_1_completion_year": "1899"},
            {**base_second, "review_education_1_field_path": "constraints.hard_constraints"},
            {"review_education_0_institution": "synthetic.private@example.test"},
            oversize,
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=tuple(overrides)[:2]):
                response = self._post_review(
                    reference,
                    self._review_body(
                        reference,
                        action="autosave",
                        use_education=True,
                        education_overrides=overrides,
                    ),
                )
                self.assertEqual(response.status, 400)
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(snapshot.version, 1)
        self.assertEqual(len(snapshot.review.education_entries), 1)

    def test_stale_resumed_tab_cannot_overwrite_newer_checkpoint(self):
        first_reference = self._reference(self._upload())
        second_adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
        )
        second = self._build(second_adapter)
        try:
            second_reference = self._reference(
                self._entry_action("continue", integration=second)
            )
            first = self._post_review(
                first_reference,
                self._review_body(
                    first_reference,
                    action="autosave",
                    use_collections=True,
                    collection_overrides={
                        "review_collection_skills_1_value": "SQL",
                    },
                ),
            )
            self.assertEqual(first.status, 204)
            stale = self._post_review(
                second_reference,
                self._review_body(
                    second_reference,
                    action="autosave",
                    integration=second,
                    use_collections=True,
                    collection_overrides={
                        "review_collection_skills_1_value": "Excel",
                    },
                ),
                integration=second,
            )
            self.assertEqual(stale.status, 409)
            self.assertIn(b"Newer progress is available", stale.body)
            self.assertIsNone(
                second._processing.vault.get(second_reference, self._grant())
            )
            with self._database() as connection:
                payload = json.loads(
                    connection.execute(
                        "SELECT review_payload_json FROM ai_profile_intake_checkpoints"
                    ).fetchone()[0]
                )
            self.assertEqual(
                payload["user_facts"][0]["value"],
                "SQL",
            )
        finally:
            second.close()

    def test_explicit_entry_discard_is_required_and_removes_saved_progress(self):
        self._reference(self._upload())
        self.integration.close()
        with self._database() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_profile_intake_checkpoints"
                ).fetchone()[0],
                1,
            )
        self.integration = self._build(self.adapter)
        entry = self.integration.handle(
            "GET",
            PROFILE_INTAKE_ROUTE,
            self._headers(origin=False),
        )
        self.assertEqual(entry.status, 200)
        self.assertIn(b"Discard saved progress and start over", entry.body)
        discarded = self._entry_action("discard_saved")
        self.assertEqual(discarded.status, 303)
        self.assertEqual(dict(discarded.headers)["Location"], PROFILE_INTAKE_ROUTE)
        with self._database() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_profile_intake_checkpoints"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                tuple(
                    connection.execute(
                        "SELECT state,consumed_at FROM ai_profile_import_entitlements"
                    ).fetchone()
                ),
                ("available", None),
            )
        fresh = self.integration.handle(
            "GET",
            PROFILE_INTAKE_ROUTE,
            self._headers(origin=False),
        )
        self.assertEqual(fresh.status, 200)
        self.assertIn(b"Start with what you already have", fresh.body)

    def test_expired_checkpoint_cannot_continue_and_falls_back_to_fresh_entry(self):
        self._reference(self._upload())
        self.integration.close()
        self.monotonic += 7 * 24 * 60 * 60 + 1
        self.now += timedelta(days=7, seconds=1)
        with self._database() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            session = accounts.create_session(
                connection,
                user_id=self.session["account_id"],
                idle_ttl=timedelta(hours=3),
                absolute_ttl=timedelta(days=1),
                idempotency_key="slice-5b-expired-checkpoint-session-0001",
                now=self.now - timedelta(minutes=1),
            )
            connection.commit()
        later_session = dict(self.session)
        later_session.update(
            session_id=session.session.session_id,
            session_token=session.session_token,
            csrf_secret=session.csrf_secret,
        )
        self.integration = self._build(self.adapter)
        expired = self._entry_action("continue", session=later_session)
        self.assertEqual(expired.status, 410)
        self.assertIn(b"Saved progress expired", expired.body)
        fresh = self.integration.handle(
            "GET",
            PROFILE_INTAKE_ROUTE,
            self._headers(origin=False, session=later_session),
        )
        self.assertEqual(fresh.status, 200)
        self.assertIn(b"Start with what you already have", fresh.body)

    def test_final_save_after_autosave_still_atomically_deletes_checkpoint(self):
        reference = self._reference(self._upload())
        autosaved = self._post_review(
            reference,
            self._review_body(
                reference,
                action="autosave",
                changes={0: "Autosaved Final Candidate"},
            ),
        )
        self.assertEqual(autosaved.status, 204)
        saved = self._save(reference, self._save_body(reference))
        self.assertEqual(saved.status, 303)
        self.assertEqual(dict(saved.headers)["Location"], "/find-matches")
        with self._database() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_profile_intake_checkpoints"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM ai_profile_import_entitlements"
                ).fetchone()[0],
                "consumed",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM product_profiles"
                ).fetchone()[0],
                1,
            )

    def test_model_failure_has_no_reservation_or_draft(self):
        failing = self._build(
            _FinalSaveAdapter(
                self.path,
                (self.read_provider, self.write_provider),
                fail=True,
            )
        )
        self.addCleanup(failing.close)
        response = self._upload(integration=failing)
        self.assertEqual(response.status, 503)
        self.assertEqual(len(failing._processing.vault._records), 0)
        with self._database() as connection:
            self.assertEqual(database_counts(connection)["ai_profile_import_entitlements"], 0)
            self.assertEqual(database_counts(connection)["ai_profile_import_attempts"], 0)

    def test_explicit_cancel_discards_while_local_expiry_keeps_checkpoint(self):
        reference = self._reference(self._upload())
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        cancel = urlencode(
            {
                "action": "cancel",
                "version": str(snapshot.version),
                "csrf": profile_intake_csrf_proof(
                    self.session["csrf_secret"],
                    "cancel",
                    draft_reference=reference,
                    version=snapshot.version,
                ),
            }
        ).encode()
        target = PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference})
        cancelled = self.integration.handle(
            "POST",
            target,
            self._headers(
                content_type="application/x-www-form-urlencoded",
                content_length=len(cancel),
            ),
            BytesIO(cancel),
        )
        self.assertEqual(cancelled.status, 303)
        with self._database() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM ai_profile_import_entitlements"
                ).fetchone()[0],
                "available",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_profile_intake_checkpoints"
                ).fetchone()[0],
                0,
            )
        self.now += timedelta(seconds=1)
        second = self._reference(self._upload())
        self.monotonic += 1801
        expired = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": second}),
            self._headers(origin=False),
        )
        self.assertEqual(expired.status, 410)
        with self._database() as connection:
            entitlement = connection.execute(
                "SELECT state,consumed_at FROM ai_profile_import_entitlements"
            ).fetchone()
            self.assertEqual(tuple(entitlement), ("reserved", None))
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_profile_intake_checkpoints"
                ).fetchone()[0],
                1,
            )

    def test_idle_abandonment_keeps_checkpoint_and_reacquires_on_continue(self):
        self._reference(self._upload())
        self.monotonic += 1801
        self.now += timedelta(seconds=1801)
        restarted = self.integration.handle(
            "GET",
            PROFILE_INTAKE_ROUTE,
            self._headers(origin=False),
        )
        self.assertEqual(restarted.status, 200)
        self.assertIn(b"Continue building your profile", restarted.body)
        self.assertIn(b"Your progress is saved for 7 days", restarted.body)
        continued = self._entry_action("continue")
        self._reference(continued)
        with self._database() as connection:
            self.assertEqual(
                tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT state FROM ai_profile_import_attempts ORDER BY created_at,attempt_id"
                    )
                ),
                ("expired", "reserved"),
            )
            self.assertEqual(
                tuple(
                    connection.execute(
                        "SELECT state,consumed_at FROM ai_profile_import_entitlements"
                    ).fetchone()
                ),
                ("reserved", None),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_profile_intake_checkpoints"
                ).fetchone()[0],
                1,
            )

    def test_resume_save_is_atomic_and_redirects_to_normal_matches(self):
        reference = self._reference(self._upload(("resume_docx",)))
        body = self._save_body(reference, changes={0: "Corrected Synthetic Candidate"})
        response = self._save(reference, body)
        self.assertEqual(response.status, 303)
        self.assertEqual(dict(response.headers)["Location"], "/find-matches")
        with self._database() as connection:
            self.assertEqual(
                database_counts(connection),
                {
                    "product_profiles": 1,
                    "product_profile_revisions": 1,
                    "product_profile_sources": 1,
                    "ai_profile_import_entitlements": 1,
                    "ai_profile_import_attempts": 1,
                },
            )
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM ai_profile_import_entitlements"
                ).fetchone()[0],
                "consumed",
            )
            source = connection.execute(
                "SELECT source_type,source_content FROM product_profile_sources"
            ).fetchone()
            self.assertEqual(source["source_type"], "user_confirmed_ai_import")
            self.assertEqual(json.loads(source["source_content"])["bundle_origins"], ["resume"])
            profile = json.loads(
                connection.execute(
                    "SELECT structured_profile_json FROM product_profile_revisions"
                ).fetchone()[0]
            )
            self.assertEqual(profile["identity"]["display_name"], "Corrected Synthetic Candidate")
            self.assertTrue(
                any(
                    "Python" in signal["keywords"]
                    for signal in profile["derived_matcher_signals"]["signals"]
                )
            )
        matches = AuthenticatedProfileMatchesService(
            authentication_gateway=DurableBrowserSessionAuthenticationGateway(
                trusted_environment_namespace=self.session["environment"],
                clock=lambda: self.now,
            ),
            authorization_gateway=DurablePersistentProfileReadAuthorizationGateway(),
            connection_provider=self.read_provider,
            clock=lambda: self.now,
            binding_secret=b"slice-4b-ordinary-matches-path-0001",
        ).resolve(
            method="GET",
            authentication_input=self._headers(origin=False),
            session_token=self.session["session_token"],
        )
        self.assertEqual(matches.state, "profile")

    def test_linkedin_and_combined_each_create_one_profile_and_consume_once(self):
        for index, roles in enumerate(
            (("linkedin_profile_export",), ("resume_docx", "linkedin_profile_export")),
            start=1,
        ):
            with self.subTest(roles=roles):
                path = Path(self.directory.name) / f"bundle-{index}.sqlite3"
                writer, session = install_ai_profile_import_database(path, suffix=f"8{index}")
                writer.close()
                old = (self.path, self.session, self.read_provider, self.write_provider, self.integration)
                self.path, self.session = path, session
                self.read_provider = _ConnectionProvider(path, read_only=True)
                self.write_provider = _ConnectionProvider(path, read_only=False)
                adapter = _FinalSaveAdapter(path, (self.read_provider, self.write_provider))
                integration = self._build(adapter)
                self.integration = integration
                try:
                    reference = self._reference(self._upload(roles))
                    result = self._save(reference, self._save_body(reference))
                    self.assertEqual(result.status, 303)
                    with self._database() as connection:
                        counts = database_counts(connection)
                        self.assertEqual(counts["product_profiles"], 1)
                        self.assertEqual(counts["ai_profile_import_attempts"], 1)
                        self.assertEqual(
                            connection.execute(
                                "SELECT state FROM ai_profile_import_entitlements"
                            ).fetchone()[0],
                            "consumed",
                        )
                        metadata = json.loads(
                            connection.execute(
                                "SELECT source_content FROM product_profile_sources"
                            ).fetchone()[0]
                        )
                        expected = [
                            "resume" if role == "resume_docx" else role
                            for role in roles
                        ]
                        self.assertEqual(metadata["bundle_origins"], expected)
                finally:
                    integration.close()
                    self.path, self.session, self.read_provider, self.write_provider, self.integration = old

    def test_pending_suggestion_conflict_and_stale_review_fail_before_commit(self):
        conflict_adapter = _FinalSaveAdapter(
            self.path,
            (self.read_provider, self.write_provider),
            conflict=True,
        )
        self.integration.close()
        self.integration = self._build(conflict_adapter)
        reference = self._reference(
            self._upload(("resume_docx", "linkedin_profile_export"))
        )
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        pending = {
            index: "pending"
            for index, fact in enumerate(snapshot.review.facts)
            if fact.suggested
        }
        unresolved = self._save(reference, self._save_body(reference, decisions=pending))
        self.assertEqual(unresolved.status, 409)
        with self._database() as connection:
            self.assertEqual(database_counts(connection)["product_profiles"], 0)
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM ai_profile_import_entitlements"
                ).fetchone()[0],
                "reserved",
            )
        resolved = {}
        accepted_groups = set()
        for index, fact in enumerate(snapshot.review.facts):
            if not fact.suggested:
                continue
            if fact.conflict_group is None or fact.conflict_group not in accepted_groups:
                resolved[index] = "accept"
                if fact.conflict_group is not None:
                    accepted_groups.add(fact.conflict_group)
            else:
                resolved[index] = "reject"
        stale_form = parse_qs(
            self._save_body(reference, decisions=resolved).decode(),
            keep_blank_values=True,
        )
        stale_form["version"] = ["2"]
        stale_form["csrf"] = [
            profile_intake_csrf_proof(
                self.session["csrf_secret"],
                "save",
                draft_reference=reference,
                version=2,
            )
        ]
        stale_body = urlencode({key: values[0] for key, values in stale_form.items()}).encode()
        self.assertEqual(self._save(reference, stale_body).status, 409)

    def test_exact_retry_and_post_commit_response_loss_create_no_second_state(self):
        failure = _OneShotSaveFailure("after_durable_commit")
        self.integration.close()
        self.integration = self._build(self.adapter, save_failure_injector=failure)
        reference = self._reference(self._upload())
        body = self._save_body(reference)
        first = self._save(reference, body)
        self.assertEqual(first.status, 503)
        with self._database() as connection:
            self.assertEqual(database_counts(connection)["product_profiles"], 1)
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM ai_profile_import_entitlements"
                ).fetchone()[0],
                "consumed",
            )
        alternate_step = body.replace(
            b"review_step=review-found",
            b"review_step=review-finish",
        )
        self.assertNotEqual(alternate_step, body)
        replay = self._save(reference, alternate_step)
        self.assertEqual(replay.status, 303)
        repeated = self._save(reference, body)
        self.assertEqual(repeated.status, 303)
        record = self.integration._processing.vault._records[reference]
        self.assertEqual(record.state, "completed")
        self.assertIsNone(record.snapshot)
        self.assertIsNone(record.durable_authority)
        self.assertIsNone(record.confirmation_fingerprint)
        self.assertNotIn("Synthetic Candidate", repr(record))
        with self._database() as connection:
            counts = database_counts(connection)
            self.assertEqual(counts["product_profiles"], 1)
            self.assertEqual(counts["ai_profile_import_attempts"], 1)

    def test_changed_review_cannot_reuse_success_authority(self):
        failure = _OneShotSaveFailure("before_vault_completion")
        self.integration.close()
        self.integration = self._build(self.adapter, save_failure_injector=failure)
        reference = self._reference(self._upload())
        body = self._save_body(reference)
        self.assertEqual(self._save(reference, body).status, 503)
        changed = body.replace(b"Synthetic+Candidate", b"Different+Candidate")
        self.assertEqual(self._save(reference, changed).status, 409)
        self.assertEqual(self._save(reference, body).status, 303)
        with self._database() as connection:
            self.assertEqual(database_counts(connection)["product_profiles"], 1)

    def test_success_preflight_blocks_second_initial_import_without_new_attempt(self):
        reference = self._reference(self._upload())
        self.assertEqual(self._save(reference, self._save_body(reference)).status, 303)
        second = self.integration.handle(
            "GET", PROFILE_INTAKE_ROUTE, self._headers(origin=False)
        )
        self.assertEqual(second.status, 409)
        with self._database() as connection:
            self.assertEqual(database_counts(connection)["ai_profile_import_attempts"], 1)

    def test_reconciliation_failure_and_insufficient_lease_publish_no_draft(self):
        with mock.patch(
            "wahojobs.profile_intake.runtime.reconcile_profile_extractions",
            side_effect=ProfileIntakeError("invalid_profile_extraction"),
        ):
            failed = self._upload()
        self.assertEqual(failed.status, 503)
        with self._database() as connection:
            self.assertEqual(database_counts(connection)["ai_profile_import_attempts"], 0)

        delegate = ProfileIntakeFinalizationService(
            read_connection_provider=self.read_provider,
            write_connection_provider=self.write_provider,
            clock=lambda: self.now,
        )
        short = self._build(
            self.adapter,
            finalizer_override=_ShortLeaseFinalizer(delegate),
        )
        self.addCleanup(short.close)
        unavailable = self._upload(integration=short)
        self.assertEqual(unavailable.status, 503)
        self.assertEqual(len(short._processing.vault._records), 0)
        with self._database() as connection:
            entitlement = connection.execute(
                "SELECT state,consumed_at FROM ai_profile_import_entitlements"
            ).fetchone()
            self.assertEqual(tuple(entitlement), ("available", None))

    def test_two_sessions_can_preflight_but_only_one_receives_a_bundle_draft(self):
        with self._database() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            created = accounts.create_session(
                connection,
                user_id=self.session["account_id"],
                idle_ttl=timedelta(hours=2),
                absolute_ttl=timedelta(days=1),
                idempotency_key="slice-4b-second-browser-session",
                now=NOW - timedelta(minutes=5),
            )
            connection.commit()
        second_session = dict(self.session)
        second_session.update(
            session_id=created.session.session_id,
            session_token=created.session_token,
            csrf_secret=created.csrf_secret,
        )
        finalizer = ProfileIntakeFinalizationService(
            read_connection_provider=self.read_provider,
            write_connection_provider=self.write_provider,
            clock=lambda: self.now,
        )
        self.assertEqual(finalizer.preflight(self._grant()), "eligible")
        self.assertEqual(
            finalizer.preflight(intake_grant(self.path, second_session, now=self.now)),
            "eligible",
        )
        winner = self._upload(("resume_docx",), session=self.session)
        loser = self._upload(("resume_docx",), session=second_session)
        self.assertEqual(winner.status, 303)
        self.assertEqual(loser.status, 409)
        self.assertEqual(len(self.integration._processing.vault._records), 1)
        with self._database() as connection:
            self.assertEqual(database_counts(connection)["ai_profile_import_attempts"], 1)
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM ai_profile_import_entitlements"
                ).fetchone()[0],
                "reserved",
            )

    def test_manual_profile_winning_after_draft_routes_safely_and_does_not_consume(self):
        reference = self._reference(self._upload())
        grant = self._grant()
        command = create_command(
            grant.principal_for_repository(),
            idempotency_key="slice-4b-manual-wins-0001",
            accepted_at=self.now,
        )
        with self._database() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            PersistentProfileRepository().create_account_native(
                connection,
                command,
                account_lineage=grant.lineage_for_repository(),
            )
        response = self._save(reference, self._save_body(reference))
        self.assertEqual(response.status, 303)
        self.assertEqual(dict(response.headers)["Location"], "/find-matches")
        with self._database() as connection:
            entitlement = connection.execute(
                "SELECT state,consumed_at FROM ai_profile_import_entitlements"
            ).fetchone()
            self.assertEqual(tuple(entitlement), ("available", None))
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM product_profile_sources "
                    "WHERE source_type='user_confirmed_ai_import'"
                ).fetchone()[0],
                0,
            )

    def test_browser_cannot_forge_durable_authority_fields(self):
        reference = self._reference(self._upload())
        body = self._save_body(reference) + b"&attempt_id=aip_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        response = self._save(reference, body)
        self.assertEqual(response.status, 400)
        with self._database() as connection:
            self.assertEqual(database_counts(connection)["product_profiles"], 0)
            self.assertEqual(
                connection.execute(
                    "SELECT state FROM ai_profile_import_entitlements"
                ).fetchone()[0],
                "reserved",
            )

    def test_privacy_boundaries_retain_no_document_or_provider_content(self):
        reference = self._reference(self._upload())
        body = self._save_body(reference)
        self.assertEqual(self._save(reference, body).status, 303)
        sentinels = (
            "private-sentinel",
            "person@example.test",
            "+1 202 555 0100",
            "10 Private Street",
            "ignore previous instructions",
            "raw provider output",
        )
        with self._database() as connection:
            durable = " ".join(
                str(value)
                for table in (
                    "ai_profile_import_attempts",
                    "ai_profile_import_entitlements",
                    "product_profile_sources",
                )
                for row in connection.execute(f"SELECT * FROM {table}")
                for value in row
            )
        record = repr(self.integration._processing.vault._records[reference])
        for sentinel in sentinels:
            self.assertNotIn(sentinel, durable)
            self.assertNotIn(sentinel, record)
        self.assertNotIn("aip_", body.decode())
        self.assertNotIn("air_", body.decode())


if __name__ == "__main__":
    unittest.main()
