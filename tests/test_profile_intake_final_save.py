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
from wahojobs.browser_session_authentication import (
    DurableBrowserSessionAuthenticationGateway,
)
from wahojobs.persistent_profile_read_authorization import (
    DurablePersistentProfileReadAuthorizationGateway,
)
from wahojobs.persistent_profiles_repository import PersistentProfileRepository
from wahojobs.profile_intake.browser import (
    ProfileIntakeBrowserIntegration,
    _preference_form_values_for_model,
)
from wahojobs.profile_intake.contracts import (
    AI_EXTRACTION_SCHEMA_VERSION,
    DocumentKind,
    ModelEvidencePacket,
    ProfileIntakeError,
    validate_ai_profile_extraction,
)
from wahojobs.profile_intake.finalization import ProfileIntakeFinalizationService
from wahojobs.profile_intake.runtime import (
    PROFILE_INTAKE_REVIEW_ROUTE,
    PROFILE_INTAKE_ROUTE,
    IntakeDraftVault,
    ProfileIntakeAuthorityService,
    ProfileIntakeProcessingService,
    profile_intake_csrf_proof,
    review_value_for_form,
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
    def __init__(self, path, providers, *, fail=False, conflict=False):
        self.path = Path(path)
        self.providers = providers
        self.fail = fail
        self.conflict = conflict
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

    def reserve(self, grant, document, diagnostics):
        authority, _lifetime = self.delegate.reserve(grant, document, diagnostics)
        return authority, 30

    def release(self, grant, authority, *, outcome_code):
        return self.delegate.release(grant, authority, outcome_code=outcome_code)

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
        snapshot = self.integration._processing.vault.get(reference, self._grant())
        form = {
            "action": "save",
            "version": str(snapshot.version),
            "csrf": profile_intake_csrf_proof(
                self.session["csrf_secret"],
                "save",
                draft_reference=reference,
                version=snapshot.version,
            ),
        }
        decisions = decisions or {}
        changes = changes or {}
        for index, fact in enumerate(snapshot.review.facts):
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

    def _database(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

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
        self.assertLessEqual(snapshot.expires_at_monotonic - self.monotonic, 600)
        page = self.integration.handle(
            "GET",
            PROFILE_INTAKE_REVIEW_ROUTE + "?" + urlencode({"draft": reference}),
            self._headers(origin=False),
        )
        self.assertEqual(page.status, 200)
        self.assertIn(b"<button type='submit'>Find my matches</button>", page.body)
        self.assertIn(
            b"Your profile is saved only when you choose Find my matches",
            page.body,
        )
        self.assertNotIn(row["attempt_id"].encode(), page.body)
        self.assertNotIn(row["reservation_id"].encode(), page.body)
        self.assertNotIn(b"private-sentinel", page.body)

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

    def test_cancel_and_expiry_release_without_consuming(self):
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
        self.now += timedelta(seconds=1)
        second = self._reference(self._upload())
        self.monotonic += 601
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
            self.assertEqual(tuple(entitlement), ("available", None))

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
        replay = self._save(reference, body)
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
