import hashlib
import hmac
import inspect
import json
import subprocess
import sys
import unittest
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.test_canonical_profile_v2 import load_cases, ordinal_resolver, persistent_id
from wahojobs.persistent_profiles import (
    ACTOR_TYPES,
    ERROR_REASON_CODES,
    IDEMPOTENCY_SCOPE_VERSION,
    LIFECYCLE_SOURCE_SCHEMA_VERSION,
    MAX_SOURCE_BYTES,
    MIGRATION_005_CAPABILITIES,
    REQUEST_FINGERPRINT_VERSION,
    SOURCE_BUNDLE_HASH_VERSION,
    AppendProfileRevisionCommand,
    ConfirmedAboutYouTextSourceDraft,
    CreatePersistentProfileCommand,
    CurrentProfileSummary,
    LifecycleActionSourceDraft,
    PersistentProfileDomainError,
    PersistentProfileSchemaCapabilities,
    ProfileCreatedResult,
    ProfileHistoryItem,
    ProfileRevisionResult,
    PurgePersistentProfileCommand,
    PurgeResult,
    TrustedPersistentProfileReference,
    TrustedPrincipalContext,
    TrustedPrivacyAdminContext,
    UserConfirmedCorrectionSourceDraft,
    canonical_utc_timestamp,
    classify_replay,
    generate_profile_id,
    generate_revision_id,
    generate_source_id,
    request_fingerprint,
    source_bundle_hash,
    source_bundle_manifest,
    source_content_hash,
    structured_profile_hash,
    validate_profile_id,
    validate_revision_id,
    validate_source_id,
)
from wahojobs.profiles.canonical_v2 import (
    SCHEMA_VERSION,
    canonical_profile_v2_json_bytes,
    convert_v1_to_v2,
    project_v2_to_matcher_v1,
    validate_canonical_profile_v2,
)


NOW = datetime(2026, 7, 19, 12, 30, 45, tzinfo=timezone.utc)
LATER = datetime(2026, 7, 19, 12, 31, 45, tzinfo=timezone.utc)
PROFILE_ID = "prf_0123456789abcdef0123456789abcdef"
REVISION_ID = "pvr_0123456789abcdef0123456789abcdef"
PRINCIPAL_ID = "prn_0123456789abcdef0123456789abcdef"
OTHER_PRINCIPAL_ID = "prn_1123456789abcdef0123456789abcdef"
IDEMPOTENCY_KEY = "profile-request-0001"


class PersistentProfilesDomainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_cases()

    def setUp(self):
        self.account_principal = TrustedPrincipalContext(
            principal_id=PRINCIPAL_ID,
            environment_namespace="production",
            principal_type="account_native",
            lifecycle_status="active",
            claim_policy="account_native",
            exclusive_account_binding=True,
            eligibility_mode="account_native",
            active_owner_binding=True,
        )
        self.development_principal = TrustedPrincipalContext(
            principal_id=OTHER_PRINCIPAL_ID,
            environment_namespace="test",
            principal_type="development",
            lifecycle_status="active",
            claim_policy="nonclaimable",
            exclusive_account_binding=False,
            eligibility_mode="development_test",
            active_owner_binding=None,
        )
        self.profile = convert_v1_to_v2(
            self.cases[0]["expected_canonical_profile"],
            persistent_profile_id=PROFILE_ID,
            source_ordinal_resolver=ordinal_resolver,
        )
        self.reference = TrustedPersistentProfileReference(
            profile_id=PROFILE_ID,
            principal_id=PRINCIPAL_ID,
            environment_namespace="production",
        )

    def about(self, content="abc", confirmed_at=NOW):
        return ConfirmedAboutYouTextSourceDraft(content=content, confirmed_at=confirmed_at)

    def correction(self, content='{"field":"language"}', confirmed_at=NOW):
        return UserConfirmedCorrectionSourceDraft(content=content, confirmed_at=confirmed_at)

    def create(self, **overrides):
        values = {
            "principal": self.account_principal,
            "canonical_profile_v2": self.profile,
            "sources": (self.about(),),
            "normalizer_version": "baseline_v1",
            "reviewer_version": None,
            "actor_type": "authenticated_user",
            "reason_code": "profile.create",
            "idempotency_key": IDEMPOTENCY_KEY,
            "accepted_at": NOW,
        }
        values.update(overrides)
        return CreatePersistentProfileCommand.prepare(**values)

    def append(self, **overrides):
        values = {
            "principal": self.account_principal,
            "profile": self.reference,
            "expected_current_revision_number": 1,
            "revision_kind": "edit",
            "canonical_profile_v2": self.profile,
            "sources": (self.about("updated"),),
            "correction_of_revision_id": None,
            "normalizer_version": "baseline_v1",
            "reviewer_version": None,
            "actor_type": "authenticated_user",
            "reason_code": "profile.edit",
            "idempotency_key": IDEMPOTENCY_KEY,
            "accepted_at": NOW,
        }
        values.update(overrides)
        return AppendProfileRevisionCommand.prepare(**values)

    def assert_domain_error(self, reason, callable_, *args, **kwargs):
        with self.assertRaises(PersistentProfileDomainError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.reason_code, reason)
        return raised.exception

    def test_account_native_context_contract(self):
        self.assertTrue(self.account_principal.authorizes_actor("authenticated_user"))
        self.assertFalse(self.account_principal.authorizes_actor("development_service"))
        self.assertEqual(self.account_principal.public_dict()["eligible"], True)

    def test_development_context_contract(self):
        self.assertTrue(self.development_principal.authorizes_actor("development_service"))
        self.assertTrue(self.development_principal.authorizes_actor("system"))
        self.assertFalse(self.development_principal.authorizes_actor("authenticated_user"))

    def test_principal_context_rejects_incoherent_eligibility(self):
        for change in (
            {"active_owner_binding": False},
            {"claim_policy": "nonclaimable"},
            {"exclusive_account_binding": False},
            {"principal_type": "development"},
        ):
            values = {
                "principal_id": PRINCIPAL_ID,
                "environment_namespace": "production",
                "principal_type": "account_native",
                "lifecycle_status": "active",
                "claim_policy": "account_native",
                "exclusive_account_binding": True,
                "eligibility_mode": "account_native",
                "active_owner_binding": True,
            }
            values.update(change)
            with self.subTest(change=change):
                self.assert_domain_error("ineligible_principal", TrustedPrincipalContext, **values)

    def test_development_context_is_limited_to_development_or_test(self):
        self.assert_domain_error(
            "ineligible_principal",
            TrustedPrincipalContext,
            principal_id=PRINCIPAL_ID,
            environment_namespace="production",
            principal_type="development",
            lifecycle_status="active",
            claim_policy="nonclaimable",
            exclusive_account_binding=False,
            eligibility_mode="development_test",
            active_owner_binding=None,
        )

    def test_trusted_contexts_are_immutable_hash_safe_and_redacted(self):
        hash(self.account_principal)
        with self.assertRaises(FrozenInstanceError):
            self.account_principal.principal_type = "development"
        rendered = repr(self.account_principal) + json.dumps(self.account_principal.public_dict())
        self.assertNotIn(PRINCIPAL_ID, rendered)
        self.assertNotIn("production", rendered)
        self.assertFalse(hasattr(TrustedPrincipalContext, "from_request"))

    def test_privacy_admin_is_separate_bounded_and_redacted(self):
        admin = TrustedPrivacyAdminContext("purge", "production")
        self.assertEqual(admin.public_dict(), {"operation_scope": "purge", "trusted": True})
        self.assertNotIn("production", repr(admin))
        self.assert_domain_error(
            "purge_not_allowed", TrustedPrivacyAdminContext, "arbitrary", "production"
        )

    def test_profile_reference_is_validated_and_redacted(self):
        self.assertEqual(self.reference.public_dict()["resource"], "persistent_profile")
        self.assertNotIn(PROFILE_ID, repr(self.reference))
        self.assert_domain_error(
            "invalid_command",
            TrustedPersistentProfileReference,
            "prf_bad",
            PRINCIPAL_ID,
            "production",
        )

    def test_capability_descriptor_matches_migration_005(self):
        self.assertEqual(MIGRATION_005_CAPABILITIES.migration_version, "005_persistent_profile_canonical_v2")
        self.assertEqual(MIGRATION_005_CAPABILITIES.canonical_versions, frozenset({SCHEMA_VERSION}))
        self.assertIn("confirmed_lifecycle_action", MIGRATION_005_CAPABILITIES.source_types)
        self.assertIn(
            LIFECYCLE_SOURCE_SCHEMA_VERSION,
            MIGRATION_005_CAPABILITIES.lifecycle_source_schema_versions,
        )

    def test_capability_rejection_is_structured(self):
        limited = PersistentProfileSchemaCapabilities(
            migration_version="test_contract_v1",
            canonical_versions=frozenset({"future_profile"}),
            source_types=frozenset({"confirmed_about_you_text"}),
            lifecycle_source_schema_versions=frozenset({"future_lifecycle"}),
        )
        self.assert_domain_error("schema_capability_unavailable", limited.require_canonical_v2)
        self.assert_domain_error(
            "schema_capability_unavailable",
            PersistentProfileSchemaCapabilities,
            migration_version="test_contract_v1",
            canonical_versions={SCHEMA_VERSION},
            source_types=frozenset({"confirmed_about_you_text"}),
            lifecycle_source_schema_versions=frozenset({LIFECYCLE_SOURCE_SCHEMA_VERSION}),
        )

    def test_about_source_preserves_exact_unicode_bytes(self):
        decomposed = self.about("Cafe\u0301\nnext")
        composed = self.about("Caf\u00e9\nnext")
        self.assertEqual(decomposed.content_bytes, "Cafe\u0301\nnext".encode("utf-8"))
        self.assertNotEqual(decomposed.content_bytes, composed.content_bytes)
        self.assertNotEqual(source_content_hash(decomposed), source_content_hash(composed))

    def test_source_control_policy_allows_tab_lf_cr_and_rejects_other_controls(self):
        self.about("a\tb\nc\rd")
        for value in ("a\x00b", "a\x08b", "a\x7fb", "a\x85b"):
            with self.subTest(value=repr(value)):
                self.assert_domain_error("content_rejected", self.about, value)

    def test_source_byte_limit_is_utf8_based(self):
        self.about("a" * MAX_SOURCE_BYTES)
        self.assert_domain_error("content_rejected", self.about, "a" * (MAX_SOURCE_BYTES + 1))
        self.assert_domain_error("content_rejected", self.about, "\u00e9" * (MAX_SOURCE_BYTES // 2 + 1))

    def test_correction_preserves_exact_json_and_rejects_nonobject_or_duplicates(self):
        source = self.correction('{ "field" : "value" }')
        self.assertEqual(source.content, '{ "field" : "value" }')
        for value in ('["value"]', '{"x":1,"x":2}', '{"x":NaN}', "not json"):
            with self.subTest(value=value):
                self.assert_domain_error("content_rejected", self.correction, value)

    def test_lifecycle_sources_have_exact_installed_bytes(self):
        expected = {
            "archive": b'{"action":"archive","schema_version":"confirmed_lifecycle_action_v1"}',
            "reactivate": b'{"action":"reactivate","schema_version":"confirmed_lifecycle_action_v1"}',
            "deletion_request": b'{"action":"deletion_request","schema_version":"confirmed_lifecycle_action_v1"}',
        }
        for action, content in expected.items():
            with self.subTest(action=action):
                source = LifecycleActionSourceDraft.for_action(action, confirmed_at=NOW)
                self.assertEqual(source.content_bytes, content)
                self.assertEqual(source.source_format, "application/json")
                self.assertEqual(source.source_schema_version, LIFECYCLE_SOURCE_SCHEMA_VERSION)

    def test_lifecycle_source_cannot_accept_arbitrary_json(self):
        with self.assertRaises(TypeError):
            LifecycleActionSourceDraft(content='{"action":"archive"}')
        self.assert_domain_error(
            "content_rejected",
            LifecycleActionSourceDraft.for_action,
            "erase",
            confirmed_at=NOW,
        )

    def test_source_public_forms_and_repr_hide_content(self):
        private = "private source text"
        source = self.about(private)
        rendered = repr(source) + json.dumps(source.public_dict())
        self.assertNotIn(private, rendered)
        self.assertFalse(source.public_dict()["content_included"])

    def test_identifier_generation_uses_128_bit_token_hex(self):
        values = iter(("0123456789abcdef0123456789abcdef",) * 3)
        with patch("wahojobs.persistent_profiles.secrets.token_hex", side_effect=lambda size: next(values)) as token:
            self.assertEqual(generate_profile_id(), PROFILE_ID)
            self.assertEqual(generate_revision_id(), REVISION_ID)
            self.assertEqual(generate_source_id(), "pfs_0123456789abcdef0123456789abcdef")
        self.assertEqual([call.args for call in token.call_args_list], [(16,), (16,), (16,)])

    def test_identifier_validation_rejects_malformed_degenerate_and_uppercase(self):
        invalid = (
            (validate_profile_id, "pvr_0123456789abcdef0123456789abcdef"),
            (validate_profile_id, "prf_" + "0" * 32),
            (validate_profile_id, "prf_" + "a" * 32),
            (validate_profile_id, "prf_0123456789ABCDEF0123456789ABCDEF"),
            (validate_revision_id, "pvr_short"),
            (validate_source_id, "pfs_0123456789abcdef0123456789abcdeg"),
        )
        for validator, value in invalid:
            with self.subTest(value=value):
                self.assert_domain_error("invalid_command", validator, value)

    def test_collision_callback_and_bounded_exhaustion(self):
        tokens = ["1" * 32, "0123456789abcdef0123456789abcdef"]
        with patch("wahojobs.persistent_profiles.secrets.token_hex", side_effect=tokens):
            self.assertEqual(generate_profile_id(is_available=lambda _value: True), PROFILE_ID)
        with patch(
            "wahojobs.persistent_profiles.secrets.token_hex",
            return_value="0123456789abcdef0123456789abcdef",
        ):
            self.assert_domain_error(
                "internal_consistency_failure",
                generate_profile_id,
                is_available=lambda _value: False,
                max_attempts=2,
            )

    def test_collision_callback_failure_is_sanitized(self):
        private = "private identity value"
        with patch(
            "wahojobs.persistent_profiles.secrets.token_hex",
            return_value="0123456789abcdef0123456789abcdef",
        ):
            error = self.assert_domain_error(
                "internal_consistency_failure",
                generate_profile_id,
                is_available=lambda _value: (_ for _ in ()).throw(RuntimeError(private)),
            )
        self.assertNotIn(private, str(error) + repr(error) + json.dumps(error.public_dict()))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_collision_callback_requires_an_actual_boolean(self):
        for malformed in (None, 0, 1, "", "yes", [], {}):
            with self.subTest(malformed=repr(malformed)), patch(
                "wahojobs.persistent_profiles.secrets.token_hex",
                return_value="0123456789abcdef0123456789abcdef",
            ):
                callback = Mock(return_value=malformed)
                error = self.assert_domain_error(
                    "internal_consistency_failure",
                    generate_profile_id,
                    is_available=callback,
                    max_attempts=2,
                )
                callback.assert_called_once_with(PROFILE_ID)
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)

    def test_collision_callback_does_not_catch_base_exception(self):
        with patch(
            "wahojobs.persistent_profiles.secrets.token_hex",
            return_value="0123456789abcdef0123456789abcdef",
        ), self.assertRaises(KeyboardInterrupt):
            generate_profile_id(
                is_available=lambda _value: (_ for _ in ()).throw(KeyboardInterrupt())
            )

    def test_mapped_domain_failures_discard_caught_exceptions(self):
        failures = (
            lambda: self.about("\ud800"),
            lambda: self.correction("not json"),
            lambda: self.create(canonical_profile_v2={}),
            lambda: CurrentProfileSummary.from_trusted(
                profile_id=PROFILE_ID,
                revision_id=REVISION_ID,
                revision_number=1,
                lifecycle_status="active",
                structured_profile_json=b"not-json",
                updated_at="2026-07-19T12:30:45+00:00",
            ),
        )
        for failure in failures:
            with self.subTest(failure=failure):
                with self.assertRaises(PersistentProfileDomainError) as raised:
                    failure()
                self.assertIsNone(raised.exception.__cause__)
                self.assertIsNone(raised.exception.__context__)

    def test_time_contract_requires_whole_second_utc(self):
        self.assertEqual(canonical_utc_timestamp(NOW), "2026-07-19T12:30:45+00:00")
        for value in (
            NOW.replace(tzinfo=None),
            NOW.replace(microsecond=1),
            NOW.astimezone(timezone(timedelta(hours=-3))),
        ):
            with self.subTest(value=value):
                self.assert_domain_error("invalid_command", canonical_utc_timestamp, value)

    def test_valid_create_rebinds_generated_profile_identity(self):
        with patch(
            "wahojobs.persistent_profiles.secrets.token_hex",
            return_value="1123456789abcdef0123456789abcdef",
        ):
            command = self.create()
        self.assertEqual(command.profile_id, "prf_1123456789abcdef0123456789abcdef")
        self.assertEqual(command.trusted_structured_profile()["identity"]["profile_id"], command.profile_id)
        self.assertNotEqual(command.profile_id, self.profile["identity"]["profile_id"])
        self.assertEqual(command.canonical_schema_version, SCHEMA_VERSION)

    def test_create_requires_about_source_and_rejects_lifecycle_source(self):
        self.assert_domain_error("invalid_command", self.create, sources=(self.correction(),))
        self.assert_domain_error(
            "invalid_command",
            self.create,
            sources=(LifecycleActionSourceDraft.for_action("archive", confirmed_at=NOW),),
        )

    def test_create_rejects_source_after_command_time(self):
        self.assert_domain_error("invalid_command", self.create, sources=(self.about(confirmed_at=LATER),))

    def test_create_rejects_v1_durable_content(self):
        self.assert_domain_error(
            "content_rejected",
            self.create,
            canonical_profile_v2=self.cases[0]["expected_canonical_profile"],
        )

    def test_create_defensively_copies_profile_and_sources(self):
        profile = deepcopy(self.profile)
        sources = [self.about("original")]
        command = self.create(canonical_profile_v2=profile, sources=sources)
        profile["identity"]["display_name"] = "mutated"
        sources.append(self.about("later"))
        self.assertNotEqual(command.trusted_structured_profile()["identity"]["display_name"], "mutated")
        self.assertEqual(len(command.sources), 1)
        with self.assertRaises(FrozenInstanceError):
            command.reason_code = "changed"

    def test_create_actor_must_match_trusted_principal_mode(self):
        self.assert_domain_error("ineligible_principal", self.create, actor_type="development_service")
        command = self.create(
            principal=self.development_principal,
            actor_type="development_service",
            canonical_profile_v2=self.profile,
        )
        self.assertEqual(command.actor_type, "development_service")

    def test_create_repr_and_public_dict_redact_ids_key_content_and_fingerprint(self):
        command = self.create(sources=(self.about("private raw source"),))
        rendered = repr(command) + json.dumps(command.public_dict())
        for private in (command.profile_id, PRINCIPAL_ID, IDEMPOTENCY_KEY, "private raw source", command.request_fingerprint):
            self.assertNotIn(private, rendered)

    def test_edit_and_correction_commands_are_coherent(self):
        edit = self.append()
        correction = self.append(
            revision_kind="correction",
            correction_of_revision_id=REVISION_ID,
            sources=(self.correction(),),
            reason_code="profile.correction",
        )
        self.assertEqual(edit.resulting_lifecycle, "preserve_current")
        self.assertEqual(correction.correction_of_revision_id, REVISION_ID)

    def test_correction_requires_target_and_correction_source(self):
        self.assert_domain_error(
            "invalid_command",
            self.append,
            revision_kind="correction",
            correction_of_revision_id=None,
            sources=(self.correction(),),
        )
        self.assert_domain_error(
            "invalid_command",
            self.append,
            revision_kind="correction",
            correction_of_revision_id=REVISION_ID,
            sources=(self.about(),),
        )

    def test_lifecycle_commands_require_exact_generated_source(self):
        for action, status in (
            ("archive", "archived"),
            ("reactivate", "active"),
            ("deletion_request", "deletion_requested"),
        ):
            with self.subTest(action=action):
                command = self.append(
                    revision_kind=action,
                    sources=(LifecycleActionSourceDraft.for_action(action, confirmed_at=NOW),),
                    reason_code=f"profile.{action}",
                )
                self.assertEqual(command.resulting_lifecycle, status)
                self.assertEqual(command.trusted_structured_profile(), self.profile)

    def test_lifecycle_commands_reject_wrong_mixed_or_extra_sources(self):
        archive = LifecycleActionSourceDraft.for_action("archive", confirmed_at=NOW)
        reactivate = LifecycleActionSourceDraft.for_action("reactivate", confirmed_at=NOW)
        self.assert_domain_error("lifecycle_conflict", self.append, revision_kind="archive", sources=(reactivate,))
        self.assert_domain_error("lifecycle_conflict", self.append, revision_kind="archive", sources=(archive, self.about()))
        self.assert_domain_error("lifecycle_conflict", self.append, revision_kind="edit", sources=(archive,))

    def test_append_rejects_profile_identity_or_principal_mismatch(self):
        changed = deepcopy(self.profile)
        changed["identity"]["profile_id"] = "prf_1123456789abcdef0123456789abcdef"
        self.assert_domain_error("invalid_command", self.append, canonical_profile_v2=changed)
        other_ref = TrustedPersistentProfileReference(PROFILE_ID, OTHER_PRINCIPAL_ID, "production")
        self.assert_domain_error("ineligible_principal", self.append, profile=other_ref)

    def test_append_expected_version_and_source_time_are_validated(self):
        self.assert_domain_error("invalid_command", self.append, expected_current_revision_number=0)
        self.assert_domain_error("invalid_command", self.append, sources=(self.about(confirmed_at=LATER),))

    def test_purge_is_a_pure_scoped_command(self):
        admin = TrustedPrivacyAdminContext("purge", "production")
        command = PurgePersistentProfileCommand.prepare(
            privacy_admin=admin,
            profile=self.reference,
            operation_key="privacy-purge-0001",
            accepted_at=NOW,
        )
        self.assertEqual(command.public_dict(), {"operation": "purge", "accepted_at": "2026-07-19T12:30:45+00:00"})
        self.assertNotIn(PROFILE_ID, repr(command))
        self.assertFalse(hasattr(command, "purge_receipt_id"))

    def test_purge_requires_purge_scope_and_matching_environment(self):
        self.assert_domain_error(
            "purge_not_allowed",
            PurgePersistentProfileCommand.prepare,
            privacy_admin=TrustedPrivacyAdminContext("deletion_request", "production"),
            profile=self.reference,
            operation_key="privacy-purge-0001",
            accepted_at=NOW,
        )
        self.assert_domain_error(
            "purge_not_allowed",
            PurgePersistentProfileCommand.prepare,
            privacy_admin=TrustedPrivacyAdminContext("purge", "test"),
            profile=self.reference,
            operation_key="privacy-purge-0001",
            accepted_at=NOW,
        )

    def test_source_hash_known_answer_and_version(self):
        self.assertEqual(
            source_content_hash(self.about("abc")),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )
        self.assertEqual(SOURCE_BUNDLE_HASH_VERSION, "persistent_profile_source_bundle_v1")
        self.assertEqual(
            source_bundle_hash((self.about("abc"),)),
            "b58c1fb74359246e77fb91529d265bcd63bf058840ff623c63cdce09d1e9064a",
        )

    def test_hash_algorithm_versions_are_part_of_bundle_and_request_preimages(self):
        bundle = source_bundle_hash((self.about("abc"),))
        with patch(
            "wahojobs.persistent_profiles.SOURCE_BUNDLE_HASH_VERSION",
            "persistent_profile_source_bundle_v2",
        ):
            self.assertNotEqual(source_bundle_hash((self.about("abc"),)), bundle)
        fingerprint = self.create().request_fingerprint
        with patch(
            "wahojobs.persistent_profiles.REQUEST_FINGERPRINT_VERSION",
            "persistent_profile_request_v2",
        ):
            self.assertNotEqual(self.create().request_fingerprint, fingerprint)

    def test_structured_hash_reuses_canonical_v2_serializer(self):
        expected = hashlib.sha256(canonical_profile_v2_json_bytes(self.profile)).hexdigest()
        self.assertEqual(structured_profile_hash(self.profile), expected)

    def test_source_bundle_manifest_is_unambiguous_and_ordered(self):
        sources = (self.about("one"), self.correction('{"two":2}'))
        manifest = source_bundle_manifest(sources)
        self.assertEqual([item["ordinal"] for item in manifest["sources"]], [1, 2])
        self.assertEqual(manifest["version"], SOURCE_BUNDLE_HASH_VERSION)
        rendered = json.dumps(manifest)
        self.assertNotIn('"one"', rendered)
        self.assertNotIn('"two"', rendered)
        self.assertEqual(manifest["sources"][0]["byte_length"], 3)

    def test_every_source_bundle_field_changes_hash(self):
        base = self.about("one")
        variants = (
            self.about("two"),
            ConfirmedAboutYouTextSourceDraft("one", NOW, source_schema_version="confirmed_about_you_text_v2"),
            ConfirmedAboutYouTextSourceDraft("one", NOW, parser_version="parser_v1"),
            self.about("one", confirmed_at=LATER),
            self.correction('{"one":1}'),
        )
        baseline = source_bundle_hash((base,))
        for variant in variants:
            with self.subTest(variant=repr(variant)):
                self.assertNotEqual(source_bundle_hash((variant,)), baseline)
        self.assertNotEqual(source_bundle_hash((base, self.correction())), source_bundle_hash((self.correction(), base)))

    def test_create_fingerprint_excludes_generated_profile_id(self):
        commands = []
        for token in (
            "1123456789abcdef0123456789abcdef",
            "2123456789abcdef0123456789abcdef",
        ):
            with patch("wahojobs.persistent_profiles.secrets.token_hex", return_value=token):
                commands.append(self.create())
        self.assertNotEqual(commands[0].profile_id, commands[1].profile_id)
        self.assertNotEqual(commands[0].structured_profile_sha256, commands[1].structured_profile_sha256)
        self.assertEqual(commands[0].request_fingerprint, commands[1].request_fingerprint)

    def test_create_fingerprint_changes_for_every_semantic_command_field(self):
        baseline = request_fingerprint(self.create())
        changed_profile = deepcopy(self.profile)
        changed_profile["identity"]["display_name"] = "Different profile"
        variants = (
            self.create(canonical_profile_v2=changed_profile),
            self.create(sources=(self.about("different"),)),
            self.create(normalizer_version="baseline_v2"),
            self.create(reviewer_version="reviewer_v1"),
            self.create(reason_code="profile.import"),
            self.create(idempotency_key="profile-request-0002"),
            self.create(accepted_at=LATER, sources=(self.about(confirmed_at=NOW),)),
        )
        for variant in variants:
            with self.subTest(value=repr(variant)):
                self.assertNotEqual(variant.request_fingerprint, baseline)

    def test_principal_and_actor_semantics_change_creation_fingerprint(self):
        account = self.create()
        development_service = self.create(
            principal=self.development_principal,
            actor_type="development_service",
        )
        development_system = self.create(
            principal=self.development_principal,
            actor_type="system",
        )
        self.assertNotEqual(account.request_fingerprint, development_service.request_fingerprint)
        self.assertNotEqual(development_service.request_fingerprint, development_system.request_fingerprint)

    def test_append_fingerprint_changes_for_relationship_version_kind_and_target(self):
        baseline = self.append().request_fingerprint
        changed_profile = deepcopy(self.profile)
        changed_profile["identity"]["display_name"] = "Changed display"
        variants = (
            self.append(expected_current_revision_number=2),
            self.append(canonical_profile_v2=changed_profile),
            self.append(sources=(self.about("different"),)),
            self.append(reason_code="profile.import"),
            self.append(accepted_at=LATER),
            self.append(
                revision_kind="correction",
                correction_of_revision_id=REVISION_ID,
                sources=(self.correction(),),
                reason_code="profile.correction",
            ),
        )
        for variant in variants:
            with self.subTest(value=repr(variant)):
                self.assertNotEqual(variant.request_fingerprint, baseline)

    def test_correction_target_and_purge_semantics_change_fingerprint(self):
        correction_one = self.append(
            revision_kind="correction",
            correction_of_revision_id=REVISION_ID,
            sources=(self.correction(),),
            reason_code="profile.correction",
        )
        correction_two = self.append(
            revision_kind="correction",
            correction_of_revision_id="pvr_1123456789abcdef0123456789abcdef",
            sources=(self.correction(),),
            reason_code="profile.correction",
        )
        self.assertNotEqual(correction_one.request_fingerprint, correction_two.request_fingerprint)
        admin = TrustedPrivacyAdminContext("purge", "production")
        purge_one = PurgePersistentProfileCommand.prepare(
            privacy_admin=admin,
            profile=self.reference,
            operation_key="privacy-purge-0001",
            accepted_at=NOW,
        )
        purge_two = PurgePersistentProfileCommand.prepare(
            privacy_admin=admin,
            profile=self.reference,
            operation_key="privacy-purge-0002",
            accepted_at=NOW,
        )
        purge_later = PurgePersistentProfileCommand.prepare(
            privacy_admin=admin,
            profile=self.reference,
            operation_key="privacy-purge-0001",
            accepted_at=LATER,
        )
        self.assertNotEqual(purge_one.request_fingerprint, purge_two.request_fingerprint)
        self.assertNotEqual(purge_one.request_fingerprint, purge_later.request_fingerprint)

    def test_request_fingerprint_and_replay_contract(self):
        command = self.create()
        self.assertRegex(command.request_fingerprint, r"^[0-9a-f]{64}$")
        self.assertEqual(request_fingerprint(command), command.request_fingerprint)
        self.assertEqual(classify_replay(command.request_fingerprint, command.request_fingerprint), "exact_replay")
        changed = self.create(reason_code="profile.import")
        self.assertEqual(classify_replay(command.request_fingerprint, changed.request_fingerprint), "changed_conflict")
        self.assertEqual(REQUEST_FINGERPRINT_VERSION, "persistent_profile_request_v1")
        self.assertEqual(IDEMPOTENCY_SCOPE_VERSION, "persistent_profile_principal_scope_v1")
        self.assertEqual(
            command.request_fingerprint,
            "fb79cb7884dc4d17f8f6a4fcbd91d4f63b4f63731f5d174bba2272d02037d291",
        )

    def test_replay_classification_uses_constant_time_digest_comparison(self):
        existing = "a" * 64
        candidate = "b" * 64
        with patch(
            "wahojobs.persistent_profiles.hmac.compare_digest",
            wraps=hmac.compare_digest,
        ) as compare:
            self.assertEqual(classify_replay(existing, candidate), "changed_conflict")
        compare.assert_called_once_with(existing, candidate)

    def test_result_models_redact_ids_and_content_by_default(self):
        created = ProfileCreatedResult(PROFILE_ID, REVISION_ID, 1, "active", "2026-07-19T12:30:45+00:00")
        revision = ProfileRevisionResult(PROFILE_ID, REVISION_ID, 2, "edit", "active", "2026-07-19T12:30:45+00:00")
        profile_bytes = canonical_profile_v2_json_bytes(self.profile)
        current = CurrentProfileSummary.from_trusted(
            profile_id=PROFILE_ID,
            revision_id=REVISION_ID,
            revision_number=2,
            lifecycle_status="active",
            structured_profile_json=profile_bytes,
            updated_at="2026-07-19T12:30:45+00:00",
        )
        history = ProfileHistoryItem.from_trusted(
            profile_id=PROFILE_ID,
            revision_id=REVISION_ID,
            revision_number=2,
            revision_kind="edit",
            lifecycle_status="active",
            created_at="2026-07-19T12:30:45+00:00",
            structured_profile_json=profile_bytes,
        )
        for result in (created, revision, current, history):
            rendered = repr(result) + json.dumps(result.public_dict())
            self.assertNotIn(PROFILE_ID, rendered)
            self.assertNotIn(REVISION_ID, rendered)
            self.assertNotIn(self.profile["identity"]["display_name"], rendered)
        self.assertFalse(current.public_dict()["structured_profile_included"])
        self.assertFalse(history.public_dict()["structured_profile_included"])

    def test_result_timestamps_require_real_canonical_utc_instants(self):
        profile_bytes = canonical_profile_v2_json_bytes(self.profile)
        invalid_timestamps = (
            "2026-02-30T12:30:45+00:00",
            "2026-07-19T24:00:00+00:00",
            "2026-07-19T12:60:00+00:00",
            "2026-07-19T12:30:60+00:00",
            "2026-07-19T12:30:45.1+00:00",
            "2026-07-19T12:30:45Z",
        )
        for timestamp in invalid_timestamps:
            constructors = (
                lambda timestamp=timestamp: ProfileCreatedResult(
                    PROFILE_ID, REVISION_ID, 1, "active", timestamp
                ),
                lambda timestamp=timestamp: ProfileRevisionResult(
                    PROFILE_ID, REVISION_ID, 2, "edit", "active", timestamp
                ),
                lambda timestamp=timestamp: CurrentProfileSummary.from_trusted(
                    profile_id=PROFILE_ID,
                    revision_id=REVISION_ID,
                    revision_number=2,
                    lifecycle_status="active",
                    structured_profile_json=profile_bytes,
                    updated_at=timestamp,
                ),
                lambda timestamp=timestamp: ProfileHistoryItem.from_trusted(
                    profile_id=PROFILE_ID,
                    revision_id=REVISION_ID,
                    revision_number=2,
                    revision_kind="edit",
                    lifecycle_status="active",
                    created_at=timestamp,
                    structured_profile_json=profile_bytes,
                ),
            )
            for constructor in constructors:
                with self.subTest(timestamp=timestamp, constructor=constructor):
                    error = self.assert_domain_error(
                        "internal_consistency_failure", constructor
                    )
                    self.assertIsNone(error.__cause__)
                    self.assertIsNone(error.__context__)

    def test_trusted_result_serialization_requires_explicit_content_opt_in(self):
        current = CurrentProfileSummary.from_trusted(
            profile_id=PROFILE_ID,
            revision_id=REVISION_ID,
            revision_number=1,
            lifecycle_status="active",
            structured_profile_json=canonical_profile_v2_json_bytes(self.profile),
            updated_at="2026-07-19T12:30:45+00:00",
        )
        self.assertNotIn("structured_profile", current.trusted_dict())
        self.assertEqual(
            current.trusted_dict(include_structured_profile=True)["structured_profile"],
            self.profile,
        )
        self.assertFalse(hasattr(current, "structured_profile_json"))
        omitted = CurrentProfileSummary.from_trusted(
            profile_id=PROFILE_ID,
            revision_id=REVISION_ID,
            revision_number=1,
            lifecycle_status="active",
            structured_profile_json=None,
            updated_at="2026-07-19T12:30:45+00:00",
        )
        omitted_trusted = omitted.trusted_dict(include_structured_profile=True)
        self.assertFalse(omitted_trusted["structured_profile_included"])
        self.assertNotIn("structured_profile", omitted_trusted)

    def test_purge_result_has_only_nonconfirming_outcome(self):
        result = PurgeResult()
        self.assertEqual(result.public_dict(), {"outcome": "absent_or_completed"})
        self.assertFalse(hasattr(result, "receipt_id"))
        self.assert_domain_error("internal_consistency_failure", PurgeResult, "completed")

    def test_all_domain_errors_are_bounded_stable_and_sanitized(self):
        private = "prn_deadbeefdeadbeefdeadbeefdeadbeef private@example.invalid token-secret"
        for reason in ERROR_REASON_CODES:
            with self.subTest(reason=reason):
                try:
                    raise RuntimeError(private)
                except RuntimeError as cause:
                    error = PersistentProfileDomainError(reason)
                    error.__cause__ = cause
                rendered = str(error) + repr(error) + json.dumps(error.public_dict())
                self.assertNotIn(private, rendered)
                self.assertNotIn("example.invalid", rendered)
                self.assertLess(len(rendered), 512)
                self.assertEqual(error.public_dict()["reason_code"], reason)

    def test_all_25_v2_fixtures_validate_hash_and_project_without_identity_leak(self):
        self.assertEqual(len(self.cases), 25)
        for index, case in enumerate(self.cases, start=1):
            with self.subTest(case=case["case_id"]):
                v2 = convert_v1_to_v2(
                    case["expected_canonical_profile"],
                    persistent_profile_id=persistent_id(index),
                    source_ordinal_resolver=ordinal_resolver,
                )
                self.assertEqual(validate_canonical_profile_v2(v2), v2)
                self.assertRegex(structured_profile_hash(v2), r"^[0-9a-f]{64}$")
                projected = project_v2_to_matcher_v1(v2, matcher_profile_id=case["archetype_id"])
                rendered = json.dumps(projected, sort_keys=True)
                self.assertNotIn(v2["identity"]["profile_id"], rendered)

    def test_command_constructors_do_not_accept_caller_ids_hashes_or_fingerprints(self):
        self.assertEqual(tuple(inspect.signature(CreatePersistentProfileCommand).parameters), ())
        parameters = set(inspect.signature(CreatePersistentProfileCommand.prepare).parameters)
        for prohibited in ("profile_id", "revision_id", "source_id", "structured_profile_sha256", "source_bundle_sha256", "request_fingerprint"):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, parameters)

    def test_malformed_container_types_return_sanitized_domain_errors(self):
        self.assert_domain_error(
            "ineligible_principal",
            TrustedPrincipalContext,
            principal_id=PRINCIPAL_ID,
            environment_namespace="production",
            principal_type=[],
            lifecycle_status="active",
            claim_policy="account_native",
            exclusive_account_binding=True,
            eligibility_mode="account_native",
            active_owner_binding=True,
        )
        self.assert_domain_error(
            "content_rejected",
            LifecycleActionSourceDraft.for_action,
            [],
            confirmed_at=NOW,
        )
        self.assert_domain_error("invalid_command", self.append, revision_kind=[])

    def test_module_import_has_no_database_network_or_file_write_side_effect(self):
        script = r'''
import builtins
import socket
import sqlite3

def blocked(*args, **kwargs):
    raise RuntimeError("side effect")

sqlite3.connect = blocked
socket.socket = blocked
original_open = builtins.open
def guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        raise RuntimeError("file write")
    return original_open(file, mode, *args, **kwargs)
builtins.open = guarded_open
import wahojobs.persistent_profiles as domain
print(domain.MIGRATION_VERSION)
'''
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "005_persistent_profile_canonical_v2")

    def test_normal_runtime_does_not_import_domain_module(self):
        references = []
        for root in (ROOT / "wahojobs", ROOT / "scripts"):
            for path in root.rglob("*.py"):
                if path.name == "persistent_profiles.py":
                    continue
                text = path.read_text(encoding="utf-8")
                if "wahojobs.persistent_profiles" in text:
                    references.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(
            sorted(references),
            [
                "scripts/local_product_app.py",
                "scripts/persistent_profiles_reconcile.py",
                "wahojobs/authenticated_profile_matches.py",
                "wahojobs/browser_session_authentication.py",
                "wahojobs/persistent_profile_corrections.py",
                "wahojobs/persistent_profile_creation.py",
                "wahojobs/persistent_profile_read_authorization.py",
                "wahojobs/persistent_profiles_application.py",
                "wahojobs/persistent_profiles_browser.py",
                "wahojobs/persistent_profiles_reconciliation.py",
                "wahojobs/persistent_profiles_repository.py",
            ],
        )

    def test_domain_module_contains_no_repository_or_database_implementation(self):
        text = (ROOT / "wahojobs" / "persistent_profiles.py").read_text(encoding="utf-8")
        for prohibited in ("import sqlite3", "sqlite3.connect", "INSERT INTO", "UPDATE product_", "DELETE FROM", "requests.", "urllib.request"):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, text)


if __name__ == "__main__":
    unittest.main()
