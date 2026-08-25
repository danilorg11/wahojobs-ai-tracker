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
    managed_education_fact_indexes,
    managed_review_collection_fact_indexes,
    profile_intake_csrf_proof,
    review_value_for_form,
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
    ):
        self.path = Path(path)
        self.providers = providers
        self.fail = fail
        self.conflict = conflict
        self.include_total_years = include_total_years
        self.include_education = include_education
        self.include_internal_classifications = include_internal_classifications
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
            _fact(reference, block, "identity.display_name", "Synthetic Candidate"),
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

    def _save_body(self, reference, *, decisions=None, changes=None):
        return self._review_body(
            reference,
            action="save",
            decisions=decisions,
            changes=changes,
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
            ):
                continue
            form[f"fact_{index}_value"] = changes.get(
                index, review_value_for_form(fact.value)
            )
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
            PROFILE_INTAKE_REVIEW_COLLECTIONS["skills"]["limit"],
            MAX_SKILL_ENTRIES,
        )
        self.assertEqual(
            PROFILE_INTAKE_REVIEW_COLLECTIONS["job_titles"]["limit"],
            CANONICAL_PROFILE_V2_LIMITS["string_list_items"],
        )
        self.assertEqual(
            PROFILE_INTAKE_REVIEW_COLLECTIONS["languages"]["limit"],
            MAX_LANGUAGES,
        )

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
        reference = self._reference(self._upload())
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        suggestion_index = next(
            index
            for index, fact in enumerate(snapshot.review.facts)
            if fact.suggested and fact.decision == "pending"
        )
        form = {
            name: values[0]
            for name, values in parse_qs(
                self._review_body(
                    reference,
                    action="autosave",
                    changes={0: "Saved With Pending Suggestion"},
                ).decode("ascii"),
                keep_blank_values=True,
            ).items()
        }
        form.pop(f"fact_{suggestion_index}_value")
        saved = self._post_review(reference, urlencode(form).encode("ascii"))
        self.assertEqual(saved.status, 204)
        updated = self.integration._processing.vault.get(reference, self._grant())
        self.assertEqual(updated.review.facts[0].value, "Saved With Pending Suggestion")
        self.assertEqual(updated.review.facts[suggestion_index].decision, "pending")
        with self._database() as connection:
            payload = json.loads(
                connection.execute(
                    "SELECT review_payload_json FROM ai_profile_intake_checkpoints"
                ).fetchone()[0]
            )
        self.assertEqual(payload["facts"][suggestion_index]["decision"], "pending")

        final_form = {
            name: values[0]
            for name, values in parse_qs(
                self._review_body(reference, action="save").decode("ascii"),
                keep_blank_values=True,
            ).items()
        }
        final_form.pop(f"fact_{suggestion_index}_value")
        rejected = self._post_review(
            reference,
            urlencode(final_form).encode("ascii"),
        )
        self.assertEqual(rejected.status, 400)
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
            "preference_schedule_working_days_weekdays": "selected",
            "preference_schedule_time_of_day_business_hours": "selected",
            "preference_schedule_flexibility_modes_flexible": "selected",
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
        self.assertIn(
            b"name='preference_job_interests_customer_support' value='selected'>",
            page.body,
        )
        self.assertIn(
            b"name='preference_job_interests_ai_training' value='selected'>",
            page.body,
        )

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
            b"name='review_collection_skills_1_remove' value='remove' checked",
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

    def test_typed_collections_reject_duplicates_invalid_values_limits_and_paths(self):
        reference = self._reference(self._upload())
        invalid_overrides = (
            {"review_collection_skills_1_value": "python"},
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
        serialized = json.dumps(payload["education_entries"], sort_keys=True).casefold()
        for forbidden in (
            "evidence",
            "document_reference",
            "filename",
            "prompt",
            "provider",
        ):
            self.assertNotIn(forbidden, serialized)

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
        self.assertTrue(
            any(
                source["source_kind"] == "user_confirmation"
                and source["field_path"].startswith("education.entries[")
                for source in profile["provenance"]["field_sources"]
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
                        "review_collection_skills_1_value": "SQL"
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
                        "review_collection_skills_1_value": "Excel"
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
