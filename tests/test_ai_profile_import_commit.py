from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.ai_profile_import_test_support import (
    NOW,
    confirmed_review,
    database_counts,
    import_source_metadata,
    install_ai_profile_import_database,
    intake_grant,
)
from tests.browser_session_authentication_test_support import seed_browser_session
from tests.persistent_profile_read_authorization_test_support import transition_binding
from tests.persistent_profiles_repository_test_support import (
    append_command,
    canonical_fixture,
    create_command,
    purge_command,
    reference,
)
from wahojobs.ai_profile_import import (
    AI_PROFILE_IMPORT_RESERVATION_LEASE,
    AIProfileImportError,
    AIProfileImportReservationAuthority,
    AIProfileImportReservationRequest,
    AIProfileImportService,
    AIProfileImportSourceMetadata,
    ConfirmedAIProfileImport,
    prepare_confirmed_ai_profile_import,
)
from wahojobs.persistent_profiles import (
    MIGRATION_010_CAPABILITIES,
    AppendProfileRevisionCommand,
    CreatePersistentProfileCommand,
    PersistentProfileDomainError,
    UserConfirmedAIImportSourceDraft,
)
from wahojobs.persistent_profiles_repository import (
    PersistentProfileRepository,
    _ai_profile_import_repository,
)
from wahojobs.profile_intake.contracts import DocumentKind
from wahojobs.profiles.normalizer import signals_for_domains
from wahojobs.profiles.preference_model import (
    empty_profile_preferences_v1,
    preference_model_to_legacy_preferences,
)


class AIProfileImportCommitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="wahojobs-ai-commit-", ignore_cleanup_errors=True
        )
        self.path = Path(self.directory.name) / "database.sqlite3"
        self.connection, self.session = install_ai_profile_import_database(self.path)
        self.grant = intake_grant(self.path, self.session)
        self.service = AIProfileImportService()
        self.metadata = import_source_metadata()

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def reserve(self, *, key="ai-confirmation-key-0001", service=None):
        return (service or self.service).reserve(
            self.connection,
            self.grant,
            AIProfileImportReservationRequest(key, self.metadata),
            now=NOW,
        ).authority

    def confirmed(self, *, conflict=False, preference_model=None):
        return prepare_confirmed_ai_profile_import(
            confirmed_review(
                conflict=conflict,
                preference_model=preference_model,
            ),
            self.metadata,
        )

    def assert_code(self, code, callable_, *args, **kwargs):
        with self.assertRaises(AIProfileImportError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), code)
        self.assertNotIn("Synthetic Candidate", repr(raised.exception))

    def test_success_is_one_atomic_profile_and_entitlement_commit(self):
        reservation = self.reserve()
        result = self.service.commit_confirmed_ai_profile_import(
            self.connection,
            self.grant,
            reservation,
            self.confirmed(),
            now=NOW,
        )
        self.assertFalse(result.replayed)
        self.assertEqual(
            database_counts(self.connection),
            {
                "product_profiles": 1,
                "product_profile_revisions": 1,
                "product_profile_sources": 1,
                "ai_profile_import_entitlements": 1,
                "ai_profile_import_attempts": 1,
            },
        )
        source = self.connection.execute(
            "SELECT source_type,source_format,source_schema_version,source_content "
            "FROM product_profile_sources"
        ).fetchone()
        self.assertEqual(
            tuple(source[:3]),
            (
                "user_confirmed_ai_import",
                "application/json",
                "user_confirmed_ai_import_v1",
            ),
        )
        self.assertEqual(
            set(json.loads(source[3])),
            {
                "schema_version",
                "bundle_origins",
                "document_count",
                "parser_versions",
                "model",
                "prompt_version",
                "extraction_schema_version",
                "review_schema_version",
            },
        )
        entitlement = self.connection.execute(
            "SELECT state,attempt_id,reservation_id,consumed_at FROM ai_profile_import_entitlements"
        ).fetchone()
        self.assertEqual(entitlement[0], "consumed")
        self.assertEqual(entitlement[1], reservation.attempt_id)
        self.assertEqual(entitlement[2], reservation.reservation_id)
        self.assertIsNotNone(entitlement[3])
        attempt = self.connection.execute(
            "SELECT state,result_code,result_profile_id,result_revision_id "
            "FROM ai_profile_import_attempts"
        ).fetchone()
        self.assertEqual(tuple(attempt), ("succeeded", "success", result.profile_id, result.revision_id))

    def test_exact_success_retry_replays_original_result_without_new_rows(self):
        reservation = self.reserve()
        confirmed = self.confirmed()
        first = self.service.commit_confirmed_ai_profile_import(
            self.connection, self.grant, reservation, confirmed, now=NOW
        )
        replay = self.service.commit_confirmed_ai_profile_import(
            self.connection,
            self.grant,
            reservation,
            confirmed,
            now=NOW + timedelta(seconds=1),
        )
        self.assertTrue(replay.replayed)
        self.assertEqual((replay.profile_id, replay.revision_id), (first.profile_id, first.revision_id))
        self.assertEqual(database_counts(self.connection)["product_profiles"], 1)
        self.assertEqual(database_counts(self.connection)["product_profile_revisions"], 1)
        self.assertEqual(database_counts(self.connection)["product_profile_sources"], 1)
        self.assertEqual(database_counts(self.connection)["ai_profile_import_attempts"], 1)
        changed_review = confirmed_review()
        changed_fact = replace(changed_review.facts[0], value="Different Confirmed Name")
        changed_review = replace(
            changed_review,
            facts=(changed_fact, *changed_review.facts[1:]),
        )
        changed = prepare_confirmed_ai_profile_import(changed_review, self.metadata)
        self.assert_code(
            "idempotency_conflict",
            self.service.commit_confirmed_ai_profile_import,
            self.connection,
            self.grant,
            reservation,
            changed,
            now=NOW + timedelta(seconds=2),
        )

    def test_different_attempt_after_consumption_is_rejected(self):
        reservation = self.reserve()
        self.service.commit_confirmed_ai_profile_import(
            self.connection, self.grant, reservation, self.confirmed(), now=NOW
        )
        self.assert_code(
            "entitlement_consumed",
            self.service.reserve,
            self.connection,
            self.grant,
            AIProfileImportReservationRequest(
                "ai-confirmation-key-0002", self.metadata
            ),
            now=NOW + timedelta(seconds=1),
        )
        self.assertEqual(database_counts(self.connection)["ai_profile_import_attempts"], 1)

    def test_expired_wrong_account_and_stale_lineage_fail_closed(self):
        expired = self.reserve()
        self.assert_code(
            "reservation_expired",
            self.service.commit_confirmed_ai_profile_import,
            self.connection,
            self.grant,
            expired,
            self.confirmed(),
            now=NOW + AI_PROFILE_IMPORT_RESERVATION_LEASE + timedelta(seconds=1),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT state FROM ai_profile_import_entitlements"
            ).fetchone()[0],
            "available",
        )

        fresh = self.service.reserve(
            self.connection,
            self.grant,
            AIProfileImportReservationRequest(
                "ai-confirmation-key-0003", self.metadata
            ),
            now=NOW + AI_PROFILE_IMPORT_RESERVATION_LEASE + timedelta(seconds=1),
        ).authority
        other_session = seed_browser_session(self.connection, suffix="96")
        other_grant = intake_grant(self.path, other_session)
        self.assert_code(
            "reservation_mismatch",
            self.service.commit_confirmed_ai_profile_import,
            self.connection,
            other_grant,
            fresh,
            self.confirmed(),
            now=NOW + AI_PROFILE_IMPORT_RESERVATION_LEASE + timedelta(seconds=1),
        )
        transition_binding(self.connection, self.session, "suspended")
        self.assert_code(
            "ownership_stale",
            self.service.commit_confirmed_ai_profile_import,
            self.connection,
            self.grant,
            fresh,
            self.confirmed(),
            now=NOW + AI_PROFILE_IMPORT_RESERVATION_LEASE + timedelta(seconds=1),
        )
        self.assertEqual(database_counts(self.connection)["product_profiles"], 0)

    def test_unresolved_review_is_rejected_before_durable_commit(self):
        review = confirmed_review()
        pending = replace(review.facts[0], decision="pending")
        unresolved = replace(review, facts=(pending, *review.facts[1:]))
        self.assert_code(
            "review_unresolved",
            prepare_confirmed_ai_profile_import,
            unresolved,
            self.metadata,
        )
        conflict_review = confirmed_review(
            origins=(DocumentKind.RESUME, DocumentKind.LINKEDIN_PROFILE_EXPORT),
            conflict=True,
        )
        combined_metadata = import_source_metadata(
            ("resume", "linkedin_profile_export")
        )
        accepted = []
        for fact in conflict_review.facts:
            if fact.conflict_group == "location.city":
                accepted.append(replace(fact, decision="accept"))
            else:
                accepted.append(fact)
        forged = replace(conflict_review, facts=tuple(accepted))
        self.assert_code(
            "review_unresolved",
            prepare_confirmed_ai_profile_import,
            forged,
            combined_metadata,
        )

    def test_confirmed_mapping_recomputes_server_signals_and_user_preferences(self):
        reservation = self.reserve()
        preference_model = empty_profile_preferences_v1()
        preference_model["employment_relationships"] = [
            "independent_contractor",
            "employee",
        ]
        preference_model["workloads"] = ["part_time", "full_time"]
        preference_model["engagement_terms"] = ["fixed_term"]
        preference_model["schedule"]["flexibility_modes"] = ["flexible"]
        preference_model["accepted_phone_voice_modes"] = ["non_phone"]
        preference_model["job_interests"] = [
            "software_engineering",
            "data_annotation",
        ]
        preference_model["accepted_career_levels"] = ["senior", "mid"]
        preference_model["compensation"] = {
            "minimum_kind": "preferred",
            "amount": "00025.00",
            "currency": "brl",
            "period": "hour",
        }
        confirmed = self.confirmed(preference_model=preference_model)
        result = self.service.commit_confirmed_ai_profile_import(
            self.connection, self.grant, reservation, confirmed, now=NOW
        )
        structured = json.loads(
            self.connection.execute(
                "SELECT structured_profile_json FROM product_profile_revisions WHERE revision_id=?",
                (result.revision_id,),
            ).fetchone()[0]
        )
        model = structured["preferences"]["preference_model"]
        self.assertEqual(model["employment_relationships"], ["employee", "independent_contractor"])
        self.assertEqual(model["workloads"], ["full_time", "part_time"])
        self.assertEqual(
            model["compensation"],
            {
                "minimum_kind": "preferred",
                "amount": "25",
                "currency": "BRL",
                "period": "hour",
            },
        )
        legacy = preference_model_to_legacy_preferences(model)
        self.assertEqual(
            {key: structured["preferences"][key] for key in legacy},
            legacy,
        )
        self.assertIn("freelance", structured["preferences"]["employment_types"])
        self.assertIn("full-time", structured["preferences"]["employment_types"])
        self.assertIn("part-time", structured["preferences"]["employment_types"])
        self.assertFalse(structured["preferences"]["remote"])
        expected = signals_for_domains([], ["Python"], [])
        self.assertEqual(
            confirmed.reviewed_profile.to_mapping()["derived_matcher_signals"]["signals"],
            expected,
        )
        self.assertEqual(
            [
                {"keywords": item["keywords"], "points": item["points"]}
                for item in structured["derived_matcher_signals"]["signals"]
            ],
            [
                {"keywords": item["keywords"], "points": item["points"]}
                for item in expected
            ],
        )
        self.assertEqual(structured["derived_matcher_signals"]["derived_domains"], [])
        self.assertEqual(structured["identity"]["display_name"], "Synthetic Candidate")
        self.assertEqual(structured["location"]["city"], "Lisbon")
        self.assertNotIn("remote", structured["provenance"]["missing_fields"])
        self.assertNotIn("availability", structured["provenance"]["missing_fields"])
        preference_sources = [
            item
            for item in structured["provenance"]["field_sources"]
            if item["field_path"].startswith("preferences.preference_model.")
        ]
        self.assertTrue(preference_sources)
        self.assertTrue(all(item["explicit"] for item in preference_sources))

    def test_durable_rows_retain_only_bounded_content_free_metadata(self):
        sentinels = (
            "resume-secret-sentinel",
            "candidate@example.invalid",
            "+1-202-555-0199",
            "742 Evergreen Terrace",
            "evidence-secret-sentinel",
            "ignore previous instructions",
            "raw-model-response-sentinel",
        )
        with self.assertRaises(AIProfileImportError):
            import_source_metadata(model="candidate@example.invalid")
        with self.assertRaises(AIProfileImportError):
            AIProfileImportSourceMetadata("{}")
        with self.assertRaises(AIProfileImportError):
            AIProfileImportReservationAuthority()
        with self.assertRaises(AIProfileImportError):
            ConfirmedAIProfileImport()
        reservation = self.reserve()
        self.service.commit_confirmed_ai_profile_import(
            self.connection, self.grant, reservation, self.confirmed(), now=NOW
        )
        retained = "\n".join(
            str(value)
            for row in self.connection.execute(
                "SELECT source_content FROM product_profile_sources UNION ALL "
                "SELECT source_metadata_json FROM ai_profile_import_attempts UNION ALL "
                "SELECT entitlement_code FROM ai_profile_import_entitlements"
            )
            for value in row
        )
        lowered = retained.casefold()
        for sentinel in sentinels:
            self.assertNotIn(sentinel.casefold(), lowered)
        attempt_columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(ai_profile_import_attempts)")
        }
        self.assertFalse(
            attempt_columns
            & {
                "raw_document",
                "resume_text",
                "evidence",
                "prompt",
                "provider_response",
                "filename",
            }
        )

    def test_manual_profile_wins_without_consuming_entitlement_or_ai_source(self):
        reservation = self.reserve()
        manual = create_command(
            self.grant.principal_for_repository(),
            idempotency_key="manual-profile-wins-0001",
            accepted_at=NOW,
        )
        PersistentProfileRepository().create_account_native(
            self.connection,
            manual,
            account_lineage=self.grant.lineage_for_repository(),
        )
        self.assert_code(
            "profile_already_exists",
            self.service.commit_confirmed_ai_profile_import,
            self.connection,
            self.grant,
            reservation,
            self.confirmed(),
            now=NOW,
        )
        self.assertEqual(database_counts(self.connection)["product_profiles"], 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT source_type FROM product_profile_sources"
            ).fetchone()[0],
            "confirmed_about_you_text",
        )
        self.assertEqual(
            tuple(self.connection.execute(
                "SELECT state,reservation_id,attempt_id,consumed_at FROM ai_profile_import_entitlements"
            ).fetchone()),
            ("available", None, None, None),
        )
        self.assertEqual(
            tuple(self.connection.execute(
                "SELECT state,result_code FROM ai_profile_import_attempts"
            ).fetchone()),
            ("failed", "profile_already_exists"),
        )

    def test_default_create_repository_cannot_bypass_ai_import_capability(self):
        with self.assertRaises(PersistentProfileDomainError) as repository_error:
            PersistentProfileRepository(capabilities=MIGRATION_010_CAPABILITIES)
        self.assertEqual(
            repository_error.exception.reason_code,
            "schema_capability_unavailable",
        )
        self.assert_code(
            "invalid_request",
            AIProfileImportService,
            repository=PersistentProfileRepository(),
        )
        source = UserConfirmedAIImportSourceDraft.from_metadata(
            self.metadata.to_mapping(), confirmed_at=NOW
        )
        command = CreatePersistentProfileCommand.prepare(
            principal=self.grant.principal_for_repository(),
            canonical_profile_v2=canonical_fixture(),
            sources=(source,),
            normalizer_version="ai_profile_intake_v1",
            reviewer_version="ai_profile_review_draft_v2",
            actor_type="authenticated_user",
            reason_code="profile.ai_import",
            idempotency_key="direct-ai-create-bypass-0001",
            accepted_at=NOW,
            capabilities=MIGRATION_010_CAPABILITIES,
        )
        with self.assertRaises(PersistentProfileDomainError) as raised:
            PersistentProfileRepository().create_account_native(
                self.connection,
                command,
                account_lineage=self.grant.lineage_for_repository(),
            )
        self.assertEqual(raised.exception.reason_code, "schema_capability_unavailable")
        self.assertEqual(database_counts(self.connection)["product_profiles"], 0)
        self.assertEqual(database_counts(self.connection)["ai_profile_import_entitlements"], 0)

    def test_ai_import_source_cannot_be_used_for_a_later_revision(self):
        reservation = self.reserve()
        result = self.service.commit_confirmed_ai_profile_import(
            self.connection, self.grant, reservation, self.confirmed(), now=NOW
        )
        principal = self.grant.principal_for_repository()
        current = PersistentProfileRepository().read_current(
            self.connection,
            principal,
            profile_id=result.profile_id,
            include_structured_profile=True,
        )
        source = UserConfirmedAIImportSourceDraft.from_metadata(
            self.metadata.to_mapping(), confirmed_at=NOW + timedelta(seconds=1)
        )
        with self.assertRaises(PersistentProfileDomainError) as raised:
            AppendProfileRevisionCommand.prepare(
                principal=principal,
                profile=reference(result, principal),
                expected_current_revision_number=1,
                revision_kind="edit",
                canonical_profile_v2=current.trusted_dict(
                    include_structured_profile=True
                )["structured_profile"],
                sources=(source,),
                correction_of_revision_id=None,
                normalizer_version="ai_profile_intake_v1",
                reviewer_version="ai_profile_review_draft_v2",
                actor_type="authenticated_user",
                reason_code="profile.ai_import",
                idempotency_key="ai-reimport-forbidden-0001",
                accepted_at=NOW + timedelta(seconds=1),
                capabilities=MIGRATION_010_CAPABILITIES,
            )
        self.assertEqual(raised.exception.reason_code, "invalid_command")
        self.assertEqual(database_counts(self.connection)["product_profile_revisions"], 1)

    def test_manual_creation_remains_entitlement_free_and_loses_after_ai_create(self):
        manual = create_command(
            self.grant.principal_for_repository(),
            idempotency_key="manual-entitlement-free-0001",
            accepted_at=NOW,
        )
        PersistentProfileRepository().create_account_native(
            self.connection,
            manual,
            account_lineage=self.grant.lineage_for_repository(),
        )
        self.assertEqual(database_counts(self.connection)["ai_profile_import_entitlements"], 0)

        other_directory = tempfile.TemporaryDirectory(
            prefix="wahojobs-ai-wins-", ignore_cleanup_errors=True
        )
        other_path = Path(other_directory.name) / "database.sqlite3"
        connection, session = install_ai_profile_import_database(other_path, suffix="97")
        try:
            grant = intake_grant(other_path, session)
            metadata = import_source_metadata()
            service = AIProfileImportService()
            reservation = service.reserve(
                connection,
                grant,
                AIProfileImportReservationRequest("ai-wins-race-key-0001", metadata),
                now=NOW,
            ).authority
            confirmed = prepare_confirmed_ai_profile_import(confirmed_review(), metadata)
            service.commit_confirmed_ai_profile_import(
                connection, grant, reservation, confirmed, now=NOW
            )
            losing_manual = create_command(
                grant.principal_for_repository(),
                idempotency_key="manual-loses-race-0001",
                accepted_at=NOW,
            )
            with self.assertRaises(PersistentProfileDomainError) as raised:
                PersistentProfileRepository().create_account_native(
                    connection,
                    losing_manual,
                    account_lineage=grant.lineage_for_repository(),
                )
            self.assertEqual(raised.exception.reason_code, "profile_already_exists")
            self.assertEqual(database_counts(connection)["product_profiles"], 1)
        finally:
            connection.close()
            other_directory.cleanup()

    def test_existing_profile_privacy_purge_is_not_blocked_by_success_receipt(self):
        reservation = self.reserve()
        result = self.service.commit_confirmed_ai_profile_import(
            self.connection, self.grant, reservation, self.confirmed(), now=NOW
        )
        principal = self.grant.principal_for_repository()
        repository = PersistentProfileRepository()
        current = repository.read_current(
            self.connection,
            principal,
            profile_id=result.profile_id,
            include_structured_profile=True,
        )
        profile_reference = reference(result, principal)
        deletion = append_command(
            principal,
            profile_reference,
            current.trusted_dict(include_structured_profile=True)["structured_profile"],
            revision_kind="deletion_request",
            idempotency_key="ai-profile-privacy-delete-0001",
            accepted_at=NOW + timedelta(seconds=1),
        )
        repository.append(self.connection, deletion)
        purged = repository.purge(
            self.connection,
            purge_command(
                profile_reference,
                accepted_at=NOW + timedelta(seconds=2),
            ),
        )
        self.assertEqual(purged.outcome, "absent_or_completed")
        self.assertEqual(database_counts(self.connection)["product_profiles"], 0)
        self.assertEqual(database_counts(self.connection)["product_profile_revisions"], 0)
        self.assertEqual(database_counts(self.connection)["product_profile_sources"], 0)
        self.assertEqual(
            tuple(self.connection.execute(
                "SELECT state,result_profile_id,result_revision_id FROM ai_profile_import_attempts"
            ).fetchone()),
            ("succeeded", result.profile_id, result.revision_id),
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_two_concurrent_confirmations_produce_one_create_and_one_exact_replay(self):
        reservation = self.reserve()
        confirmed = self.confirmed()

        def commit_one():
            connection = sqlite3.connect(self.path, timeout=10.0)
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                return AIProfileImportService().commit_confirmed_ai_profile_import(
                    connection, self.grant, reservation, confirmed, now=NOW
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(lambda _index: commit_one(), range(2)))
        self.assertEqual({item.replayed for item in results}, {False, True})
        self.assertEqual(len({item.profile_id for item in results}), 1)
        self.assertEqual(len({item.revision_id for item in results}), 1)
        self.assertEqual(database_counts(self.connection)["product_profiles"], 1)
        self.assertEqual(database_counts(self.connection)["ai_profile_import_attempts"], 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT state FROM ai_profile_import_entitlements"
            ).fetchone()[0],
            "consumed",
        )

    def test_every_fault_boundary_rolls_back_profile_and_consumption(self):
        service_points = (
            "before_profile_create",
            "after_profile_create",
            "during_attempt_result_update",
            "after_entitlement_transition",
            "before_commit",
        )
        cases = tuple((point, None) for point in service_points) + (
            (None, "create.after_profile_insert"),
        )
        for ordinal, (service_point, repository_point) in enumerate(cases, start=1):
            with self.subTest(service_point=service_point, repository_point=repository_point):
                directory = tempfile.TemporaryDirectory(
                    prefix="wahojobs-ai-fault-", ignore_cleanup_errors=True
                )
                path = Path(directory.name) / "database.sqlite3"
                connection, session = install_ai_profile_import_database(
                    path, suffix=str(110 + ordinal)
                )
                try:
                    grant = intake_grant(path, session)
                    metadata = import_source_metadata()

                    def fail_service(boundary):
                        if boundary == service_point:
                            raise RuntimeError("content-must-not-escape")

                    def fail_repository(boundary):
                        if boundary == repository_point:
                            raise RuntimeError("content-must-not-escape")

                    repository = _ai_profile_import_repository(
                        failure_injector=(
                            fail_repository if repository_point else None
                        ),
                    )
                    service = AIProfileImportService(
                        repository=repository,
                        failure_injector=fail_service if service_point else None,
                    )
                    reservation = service.reserve(
                        connection,
                        grant,
                        AIProfileImportReservationRequest(
                            f"ai-fault-boundary-{ordinal:04d}", metadata
                        ),
                        now=NOW,
                    ).authority
                    confirmed = prepare_confirmed_ai_profile_import(
                        confirmed_review(), metadata
                    )
                    with self.assertRaises(AIProfileImportError) as raised:
                        service.commit_confirmed_ai_profile_import(
                            connection, grant, reservation, confirmed, now=NOW
                        )
                    self.assertEqual(raised.exception.code, "internal_failure")
                    self.assertNotIn("content-must-not-escape", str(raised.exception))
                    self.assertEqual(
                        database_counts(connection),
                        {
                            "product_profiles": 0,
                            "product_profile_revisions": 0,
                            "product_profile_sources": 0,
                            "ai_profile_import_entitlements": 1,
                            "ai_profile_import_attempts": 1,
                        },
                    )
                    self.assertEqual(
                        tuple(connection.execute(
                            "SELECT state,consumed_at FROM ai_profile_import_entitlements"
                        ).fetchone()),
                        ("reserved", None),
                    )
                    self.assertEqual(
                        tuple(connection.execute(
                            "SELECT state,result_code,result_profile_id,result_revision_id "
                            "FROM ai_profile_import_attempts"
                        ).fetchone()),
                        ("reserved", None, None, None),
                    )
                finally:
                    connection.close()
                    directory.cleanup()


if __name__ == "__main__":
    unittest.main()
