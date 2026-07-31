import copy
import hashlib
import json
import pickle
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import fields
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tests.persistent_profiles_repository_test_support import (
    NOW,
    account_context,
    append_command,
    canonical_fixture,
    connect_repository_database,
    create_command,
    development_context,
    install_repository_database,
    profile_counts,
    purge_command,
    reference,
)
from tests.persistent_profiles_test_support import install_persistent_profiles
from wahojobs.persistent_profiles import (
    ConfirmedAboutYouTextSourceDraft,
    CreatePersistentProfileCommand,
    MIGRATION_005_CAPABILITIES,
    PersistentProfileDomainError,
    PersistentProfileSchemaCapabilities,
    TrustedPrincipalContext,
)
from wahojobs.profiles.canonical_v2 import canonical_profile_v2_json_bytes
import wahojobs.persistent_profiles_repository as repository_module
from wahojobs.persistent_profiles_repository import (
    APPEND_FAILURE_BOUNDARIES,
    CREATE_FAILURE_BOUNDARIES,
    PURGE_FAILURE_BOUNDARIES,
    MAX_HISTORY_RESPONSE_BYTES,
    PersistentProfileRepository,
    _serialize_history_page_for_size,
    append_profile_revision,
    create_persistent_profile,
    purge_persistent_profile,
    read_current_profile,
    read_profile_history,
)


class PersistentProfilesRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "profiles.sqlite"
        self.connection = install_repository_database(self.path)
        self.principal = development_context(self.connection)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def assert_reason(self, reason, callable_, *args, **kwargs):
        with self.assertRaises(PersistentProfileDomainError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.reason_code, reason)
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        return raised.exception

    def create(self, **kwargs):
        command = create_command(self.principal, **kwargs)
        return command, create_persistent_profile(self.connection, command)

    def current_document(self, created):
        return read_current_profile(
            self.connection,
            self.principal,
            profile_id=created.profile_id,
            include_structured_profile=True,
        ).trusted_dict(include_structured_profile=True)["structured_profile"]

    def replace_revision_fields(self, revision_id, **fields):
        allowed = {
            "canonical_schema_version",
            "structured_profile_json",
            "structured_profile_sha256",
        }
        self.assertTrue(fields)
        self.assertLessEqual(set(fields), allowed)
        trigger_sql = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_product_profile_revisions_no_update'"
        ).fetchone()[0]
        self.connection.execute("DROP TRIGGER trg_product_profile_revisions_no_update")
        self.connection.execute("PRAGMA ignore_check_constraints = ON")
        assignments = ", ".join(f"{field}=?" for field in fields)
        self.connection.execute(
            f"UPDATE product_profile_revisions SET {assignments} WHERE revision_id=?",
            (*fields.values(), revision_id),
        )
        self.connection.execute("PRAGMA ignore_check_constraints = OFF")
        self.connection.execute(trigger_sql)
        self.connection.commit()

    def test_complete_schema_and_foreign_keys_are_required(self):
        command = create_command(self.principal)
        self.connection.execute("PRAGMA foreign_keys = OFF")
        self.assert_reason(
            "schema_capability_unavailable",
            create_persistent_profile,
            self.connection,
            command,
        )

    def test_m004_only_partial_and_weakened_schemas_are_refused(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            m004 = install_persistent_profiles(Path(temp_dir) / "m004.sqlite")
            principal = development_context(m004, "91")
            self.assert_reason(
                "schema_capability_unavailable",
                create_persistent_profile,
                m004,
                create_command(principal),
            )
            m004.close()

        command = create_command(self.principal)
        self.connection.execute("DROP VIEW current_product_profiles")
        self.connection.commit()
        self.assert_reason(
            "schema_capability_unavailable",
            create_persistent_profile,
            self.connection,
            command,
        )

    def test_missing_m005_marker_is_refused(self):
        command = create_command(self.principal)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute(
            "DELETE FROM wahojobs_schema_migrations "
            "WHERE version='005_persistent_profile_canonical_v2'"
        )
        self.connection.commit()
        self.assert_reason(
            "schema_capability_unavailable",
            create_persistent_profile,
            self.connection,
            command,
        )

    def test_wrong_capability_descriptor_is_rejected(self):
        wrong = PersistentProfileSchemaCapabilities(
            migration_version="005_persistent_profile_canonical_v2",
            canonical_versions=MIGRATION_005_CAPABILITIES.canonical_versions,
            source_types=MIGRATION_005_CAPABILITIES.source_types,
            lifecycle_source_schema_versions=frozenset({"other_v1"}),
        )
        repository = PersistentProfileRepository(capabilities=wrong)
        self.assert_reason(
            "schema_capability_unavailable",
            repository.create,
            self.connection,
            create_command(self.principal),
        )

    def test_create_stores_exact_content_hashes_sources_and_current_view(self):
        command, created = self.create()
        self.assertEqual(profile_counts(self.connection), (1, 1, 1, 1))
        row = self.connection.execute(
            "SELECT structured_profile_sha256, source_bundle_sha256, request_fingerprint, "
            "revision_number, revision_kind, lifecycle_status "
            "FROM product_profile_revisions"
        ).fetchone()
        self.assertEqual(
            tuple(row),
            (
                command.structured_profile_sha256,
                command.source_bundle_sha256,
                command.request_fingerprint,
                1,
                "initial",
                "active",
            ),
        )
        stored_source = self.connection.execute(
            "SELECT source_content, source_content_sha256, source_ordinal "
            "FROM product_profile_sources"
        ).fetchone()
        self.assertEqual(stored_source[0], command.sources[0].content)
        self.assertEqual(
            stored_source[1], hashlib.sha256(command.sources[0].content_bytes).hexdigest()
        )
        self.assertEqual(stored_source[2], 1)
        self.assertFalse(created.replayed)

    def test_create_accepts_ordered_multi_source_bundle_and_rolls_back_second_source_failure(self):
        first = ConfirmedAboutYouTextSourceDraft("Primary confirmed text.", NOW)
        second = ConfirmedAboutYouTextSourceDraft("Secondary confirmed text.", NOW)
        command = CreatePersistentProfileCommand.prepare(
            principal=self.principal,
            canonical_profile_v2=canonical_fixture(),
            sources=(first, second),
            normalizer_version="baseline_v1",
            reviewer_version=None,
            actor_type="development_service",
            reason_code="profile.create",
            idempotency_key="profile-create-multi",
            accepted_at=NOW,
        )
        calls = 0

        def fail_on_second_source(boundary):
            nonlocal calls
            if boundary == "create.after_source_insert":
                calls += 1
                if calls == 2:
                    raise RuntimeError("private")

        repository = PersistentProfileRepository(_failure_injector=fail_on_second_source)
        self.assert_reason(
            "internal_consistency_failure",
            repository.create,
            self.connection,
            command,
        )
        self.assertEqual(calls, 2)
        self.assertEqual(profile_counts(self.connection), (0, 0, 0, 0))

    def test_create_exact_replay_changed_replay_and_different_key(self):
        command, created = self.create()
        stored_revision = self.connection.execute(
            "SELECT revision_id, profile_id, structured_profile_sha256, "
            "source_bundle_sha256, request_fingerprint FROM product_profile_revisions"
        ).fetchone()
        stored_sources = self.connection.execute(
            "SELECT source_id, source_content_sha256 FROM product_profile_sources "
            "ORDER BY source_ordinal"
        ).fetchall()
        self.assertEqual(created.profile_id, command.profile_id)
        self.assertEqual(created.revision_id, command.revision_id)
        self.assertEqual(
            tuple(stored_revision),
            (
                command.revision_id,
                command.profile_id,
                command.structured_profile_sha256,
                command.source_bundle_sha256,
                command.request_fingerprint,
            ),
        )
        self.assertEqual(
            tuple(tuple(row) for row in stored_sources),
            tuple(zip(command.source_ids, command.source_content_sha256s)),
        )
        replay = create_persistent_profile(self.connection, command)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.trusted_dict()["profile_id"], created.profile_id)
        regenerated = create_command(
            self.principal,
            idempotency_key="profile-create-0001",
        )
        self.assertNotEqual(regenerated.profile_id, command.profile_id)
        self.assertNotEqual(regenerated.revision_id, command.revision_id)
        self.assertNotEqual(regenerated.source_ids, command.source_ids)
        self.assertEqual(regenerated.request_fingerprint, command.request_fingerprint)
        regenerated_replay = create_persistent_profile(self.connection, regenerated)
        self.assertTrue(regenerated_replay.replayed)
        self.assertEqual(regenerated_replay.profile_id, command.profile_id)
        self.assertEqual(regenerated_replay.revision_id, command.revision_id)
        changed = create_command(
            self.principal,
            idempotency_key="profile-create-0001",
            reason_code="profile.import",
        )
        self.assert_reason(
            "idempotency_conflict",
            create_persistent_profile,
            self.connection,
            changed,
        )
        different = create_command(
            self.principal, idempotency_key="profile-create-0002"
        )
        self.assert_reason(
            "profile_already_exists",
            create_persistent_profile,
            self.connection,
            different,
        )
        self.assertEqual(profile_counts(self.connection), (1, 1, 1, 1))

    def test_account_native_eligibility_is_revalidated_durably(self):
        account = account_context(self.connection, "52")
        create_persistent_profile(self.connection, create_command(account))
        self.connection.execute(
            "UPDATE users SET lifecycle_status='suspended', row_version=row_version+1, "
            "updated_at='2026-07-20T12:05:00+00:00'"
        )
        self.connection.commit()
        other = account_context(self.connection, "53")
        self.connection.execute(
            "UPDATE users SET lifecycle_status='suspended', row_version=row_version+1, "
            "updated_at='2026-07-20T12:05:00+00:00' WHERE user_id IN "
            "(SELECT user_id FROM principal_account_bindings WHERE principal_id=?)",
            (other.principal_id,),
        )
        self.connection.commit()
        self.assert_reason(
            "ineligible_principal",
            create_persistent_profile,
            self.connection,
            create_command(other, idempotency_key="profile-create-0053"),
        )

    def test_append_edit_replay_stale_and_changed_replay(self):
        _, created = self.create()
        document = self.current_document(created)
        profile_reference = reference(created, self.principal)
        command = append_command(
            self.principal, profile_reference, document, idempotency_key="profile-edit-0001"
        )
        revised = append_profile_revision(self.connection, command)
        replay = append_profile_revision(self.connection, command)
        self.assertEqual(revised.revision_number, 2)
        self.assertTrue(replay.replayed)
        stale = append_command(
            self.principal,
            profile_reference,
            document,
            expected_revision=1,
            idempotency_key="profile-edit-0002",
        )
        self.assert_reason(
            "stale_revision", append_profile_revision, self.connection, stale
        )
        changed = append_command(
            self.principal,
            profile_reference,
            document,
            expected_revision=2,
            idempotency_key="profile-edit-0001",
        )
        self.assert_reason(
            "idempotency_conflict", append_profile_revision, self.connection, changed
        )
        self.assertEqual(profile_counts(self.connection), (1, 2, 2, 1))

    def test_correction_archive_reactivate_and_deletion_request(self):
        _, created = self.create()
        document = self.current_document(created)
        profile_reference = reference(created, self.principal)
        first_revision = created.revision_id
        correction = append_command(
            self.principal,
            profile_reference,
            document,
            revision_kind="correction",
            correction_of_revision_id=first_revision,
            idempotency_key="profile-correct-0001",
        )
        corrected = append_profile_revision(self.connection, correction)
        archive = append_command(
            self.principal,
            profile_reference,
            document,
            expected_revision=2,
            revision_kind="archive",
            idempotency_key="profile-archive-0001",
        )
        archived = append_profile_revision(self.connection, archive)
        reactivate = append_command(
            self.principal,
            profile_reference,
            document,
            expected_revision=3,
            revision_kind="reactivate",
            idempotency_key="profile-reactivate-0001",
        )
        active = append_profile_revision(self.connection, reactivate)
        deletion = append_command(
            self.principal,
            profile_reference,
            document,
            expected_revision=4,
            revision_kind="deletion_request",
            idempotency_key="profile-delete-0001",
        )
        deleted = append_profile_revision(self.connection, deletion)
        self.assertEqual(
            [corrected.lifecycle_status, archived.lifecycle_status, active.lifecycle_status, deleted.lifecycle_status],
            ["active", "archived", "active", "deletion_requested"],
        )
        later = append_command(
            self.principal,
            profile_reference,
            document,
            expected_revision=5,
            idempotency_key="profile-edit-later",
        )
        self.assert_reason(
            "lifecycle_conflict", append_profile_revision, self.connection, later
        )

    def test_deletion_request_survives_eligibility_drift(self):
        account = account_context(self.connection, "62")
        command = create_command(account, idempotency_key="profile-create-0062")
        created = create_persistent_profile(self.connection, command)
        profile_reference = reference(created, account)
        document = read_current_profile(
            self.connection,
            account,
            profile_id=created.profile_id,
            include_structured_profile=True,
        ).trusted_dict(include_structured_profile=True)["structured_profile"]
        self.connection.execute(
            "UPDATE principal_account_bindings SET binding_status='released', "
            "version=version+1, latest_event_version=latest_event_version+1, "
            "updated_at='2026-07-20T12:05:00+00:00' WHERE principal_id=?",
            (account.principal_id,),
        )
        self.connection.commit()
        edit = append_command(
            account,
            profile_reference,
            document,
            idempotency_key="profile-edit-0062",
        )
        self.assert_reason(
            "ineligible_principal", append_profile_revision, self.connection, edit
        )
        deletion = append_command(
            account,
            profile_reference,
            document,
            revision_kind="deletion_request",
            idempotency_key="profile-delete-0062",
        )
        self.assertEqual(
            append_profile_revision(self.connection, deletion).lifecycle_status,
            "deletion_requested",
        )

    def test_reads_are_relationship_scoped_paginated_and_read_only(self):
        _, created = self.create()
        document = self.current_document(created)
        profile_reference = reference(created, self.principal)
        for revision in range(1, 4):
            append_profile_revision(
                self.connection,
                append_command(
                    self.principal,
                    profile_reference,
                    document,
                    expected_revision=revision,
                    idempotency_key=f"profile-edit-{revision:04d}",
                    accepted_at=NOW + timedelta(seconds=revision),
                ),
            )
        wrong = development_context(self.connection, "72")
        before = (self.path.stat().st_size, self.path.stat().st_mtime_ns)
        page = read_profile_history(
            self.connection,
            self.principal,
            profile_id=created.profile_id,
            page_size=2,
        )
        next_page = read_profile_history(
            self.connection,
            self.principal,
            profile_id=created.profile_id,
            page_size=2,
            before_revision_number=page[-1].revision_number,
        )
        self.assertEqual([item.revision_number for item in page], [4, 3])
        self.assertEqual([item.revision_number for item in next_page], [2, 1])
        self.assert_reason(
            "profile_not_found",
            read_current_profile,
            self.connection,
            wrong,
            profile_id=created.profile_id,
        )
        after = (self.path.stat().st_size, self.path.stat().st_mtime_ns)
        self.assertEqual(before, after)
        self.assertEqual(list(self.path.parent.glob(self.path.name + "-*")), [])

    def test_history_limits_and_malformed_durable_content_are_sanitized(self):
        _, created = self.create()
        self.assert_reason(
            "invalid_command",
            read_profile_history,
            self.connection,
            self.principal,
            profile_id=created.profile_id,
            page_size=101,
        )
        trigger_sql = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_product_profile_revisions_no_update'"
        ).fetchone()[0]
        self.connection.execute("DROP TRIGGER trg_product_profile_revisions_no_update")
        self.connection.execute("PRAGMA ignore_check_constraints = ON")
        self.connection.execute(
            "UPDATE product_profile_revisions SET structured_profile_json='{'"
        )
        self.connection.execute("PRAGMA ignore_check_constraints = OFF")
        self.connection.execute(trigger_sql)
        self.connection.commit()
        self.assert_reason(
            "internal_consistency_failure",
            read_current_profile,
            self.connection,
            self.principal,
            profile_id=created.profile_id,
        )

    def test_current_read_always_validates_canonical_bytes_identity_schema_and_hash(self):
        _, created = self.create()
        original = self.connection.execute(
            "SELECT canonical_schema_version, structured_profile_json, "
            "structured_profile_sha256 FROM product_profile_revisions "
            "WHERE revision_id=?",
            (created.revision_id,),
        ).fetchone()
        valid = read_current_profile(
            self.connection,
            self.principal,
            profile_id=created.profile_id,
            include_structured_profile=False,
        )
        self.assertFalse(valid.public_dict()["structured_profile_included"])
        included = read_current_profile(
            self.connection,
            self.principal,
            profile_id=created.profile_id,
            include_structured_profile=True,
        ).trusted_dict(include_structured_profile=True)
        self.assertTrue(included["structured_profile_included"])

        other_profile = canonical_fixture(
            "prf_1234567890abcdef1234567890abcdef"
        )
        other_bytes = canonical_profile_v2_json_bytes(other_profile)
        noncanonical = json.dumps(
            json.loads(original[1]), ensure_ascii=False, indent=2
        )
        identity = json.dumps(
            json.loads(original[1])["identity"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        duplicate_key_json = '{"identity":' + identity + "," + original[1][1:]
        cases = (
            {"structured_profile_sha256": hashlib.sha256(b"wrong").hexdigest()},
            {"structured_profile_json": original[1] + " "},
            {
                "structured_profile_json": "{",
                "structured_profile_sha256": hashlib.sha256(b"{").hexdigest(),
            },
            {
                "structured_profile_json": "{}",
                "structured_profile_sha256": hashlib.sha256(b"{}").hexdigest(),
            },
            {
                "structured_profile_json": duplicate_key_json,
                "structured_profile_sha256": hashlib.sha256(
                    duplicate_key_json.encode("utf-8")
                ).hexdigest(),
            },
            {"canonical_schema_version": "canonical_profile_v1"},
            {
                "structured_profile_json": other_bytes.decode("utf-8"),
                "structured_profile_sha256": hashlib.sha256(other_bytes).hexdigest(),
            },
            {
                "structured_profile_json": noncanonical,
                "structured_profile_sha256": original[2],
            },
            {"structured_profile_sha256": "not-a-hash"},
            {"structured_profile_sha256": original[2].upper()},
        )
        for index, changes in enumerate(cases):
            with self.subTest(index=index):
                self.replace_revision_fields(created.revision_id, **changes)
                for include in (False, True):
                    self.assert_reason(
                        "internal_consistency_failure",
                        read_current_profile,
                        self.connection,
                        self.principal,
                        profile_id=created.profile_id,
                        include_structured_profile=include,
                    )
                self.replace_revision_fields(
                    created.revision_id,
                    canonical_schema_version=original[0],
                    structured_profile_json=original[1],
                    structured_profile_sha256=original[2],
                )

    def test_current_read_content_omission_cannot_be_escalated_later(self):
        _, created = self.create()
        omitted = read_current_profile(
            self.connection,
            self.principal,
            profile_id=created.profile_id,
            include_structured_profile=False,
        )
        included = read_current_profile(
            self.connection,
            self.principal,
            profile_id=created.profile_id,
            include_structured_profile=True,
        )
        self.assertIsNot(omitted, included)

        copies = (
            omitted,
            copy.copy(omitted),
            copy.deepcopy(omitted),
            pickle.loads(pickle.dumps(omitted)),
        )
        for candidate in copies:
            with self.subTest(copy_type=type(candidate).__name__):
                state = vars(candidate)
                dataclass_state = {
                    item.name: getattr(candidate, item.name) for item in fields(candidate)
                }
                self.assertEqual(state, candidate.__dict__)
                self.assertEqual(state, dataclass_state)
                self.assertIsNone(state["_structured_profile_json"])
                self.assertFalse(any(isinstance(value, (bytes, dict, list)) for value in state.values()))
                slot_values = [
                    getattr(candidate, name)
                    for name in getattr(type(candidate), "__slots__", ())
                    if hasattr(candidate, name)
                ]
                self.assertFalse(
                    any(isinstance(value, (bytes, dict, list)) for value in slot_values)
                )
                rendered = repr(candidate) + str(candidate)
                self.assertNotIn("Confirmed profile background", rendered)
                public = candidate.public_dict()
                self.assertNotIn("structured_profile", public)
                self.assertNotIn("structured_profile", json.loads(json.dumps(public)))
                trusted_default = candidate.trusted_dict()
                self.assertFalse(trusted_default["structured_profile_included"])
                self.assertNotIn("structured_profile", trusted_default)
                trusted = candidate.trusted_dict(include_structured_profile=True)
                self.assertFalse(trusted["structured_profile_included"])
                self.assertNotIn("structured_profile", trusted)
                self.assertNotIn(
                    "structured_profile", json.loads(json.dumps(trusted))
                )

        self.assertNotIn("structured_profile", included.public_dict())
        first_document = included.trusted_dict(
            include_structured_profile=True
        )["structured_profile"]
        original_display_name = first_document["identity"]["display_name"]
        first_document["identity"]["display_name"] = "Changed outside repository"
        second_document = included.trusted_dict(
            include_structured_profile=True
        )["structured_profile"]
        self.assertEqual(
            second_document["identity"]["display_name"], original_display_name
        )
        reread = read_current_profile(
            self.connection,
            self.principal,
            profile_id=created.profile_id,
            include_structured_profile=True,
        ).trusted_dict(include_structured_profile=True)["structured_profile"]
        self.assertEqual(reread["identity"]["display_name"], original_display_name)

        history_omitted = read_profile_history(
            self.connection,
            self.principal,
            profile_id=created.profile_id,
            include_structured_profile=False,
        )[0].trusted_dict(include_structured_profile=True)
        self.assertFalse(history_omitted["structured_profile_included"])
        self.assertNotIn("structured_profile", history_omitted)

    def test_current_read_omission_policy_covers_lifecycle_and_relationship_states(self):
        _, created = self.create()
        document = self.current_document(created)
        profile_reference = reference(created, self.principal)

        def assert_omitted(expected_lifecycle):
            result = read_current_profile(
                self.connection,
                self.principal,
                profile_id=created.profile_id,
                include_structured_profile=False,
            )
            self.assertEqual(result.lifecycle_status, expected_lifecycle)
            trusted = result.trusted_dict(include_structured_profile=True)
            self.assertFalse(trusted["structured_profile_included"])
            self.assertNotIn("structured_profile", trusted)

        assert_omitted("active")
        append_profile_revision(
            self.connection,
            append_command(
                self.principal,
                profile_reference,
                document,
                revision_kind="archive",
                idempotency_key="omission-archive-0001",
            ),
        )
        assert_omitted("archived")
        append_profile_revision(
            self.connection,
            append_command(
                self.principal,
                profile_reference,
                document,
                expected_revision=2,
                revision_kind="reactivate",
                idempotency_key="omission-reactivate-0001",
            ),
        )
        append_profile_revision(
            self.connection,
            append_command(
                self.principal,
                profile_reference,
                document,
                expected_revision=3,
                revision_kind="deletion_request",
                idempotency_key="omission-delete-0001",
            ),
        )
        assert_omitted("deletion_requested")

        wrong = development_context(self.connection, "93")
        self.assert_reason(
            "profile_not_found",
            read_current_profile,
            self.connection,
            wrong,
            profile_id=created.profile_id,
        )
        missing_profile_id = created.profile_id[:-1] + (
            "0" if created.profile_id[-1] != "0" else "1"
        )
        self.assert_reason(
            "profile_not_found",
            read_current_profile,
            self.connection,
            self.principal,
            profile_id=missing_profile_id,
        )

    def test_history_validates_every_selected_revision_even_when_content_is_omitted(self):
        _, created = self.create()
        document = self.current_document(created)
        profile_reference = reference(created, self.principal)
        for revision in range(1, 4):
            append_profile_revision(
                self.connection,
                append_command(
                    self.principal,
                    profile_reference,
                    document,
                    expected_revision=revision,
                    idempotency_key=f"history-integrity-{revision:04d}",
                    accepted_at=NOW + timedelta(seconds=revision),
                ),
            )
        rows = self.connection.execute(
            "SELECT revision_id, revision_number, canonical_schema_version, "
            "structured_profile_json, structured_profile_sha256 "
            "FROM product_profile_revisions ORDER BY revision_number DESC"
        ).fetchall()
        for position in (0, 1, 3):
            with self.subTest(position=position):
                row = rows[position]
                self.replace_revision_fields(
                    row[0],
                    structured_profile_sha256=hashlib.sha256(
                        f"wrong-{position}".encode("ascii")
                    ).hexdigest(),
                )
                for include in (False, True):
                    self.assert_reason(
                        "internal_consistency_failure",
                        read_profile_history,
                        self.connection,
                        self.principal,
                        profile_id=created.profile_id,
                        page_size=4,
                        include_structured_profile=include,
                    )
                self.replace_revision_fields(
                    row[0],
                    canonical_schema_version=row[2],
                    structured_profile_json=row[3],
                    structured_profile_sha256=row[4],
                )

        for position in (0, 1, 3):
            with self.subTest(malformed_position=position):
                row = rows[position]
                self.replace_revision_fields(
                    row[0],
                    structured_profile_json="{",
                    structured_profile_sha256=hashlib.sha256(b"{").hexdigest(),
                )
                for include in (False, True):
                    self.assert_reason(
                        "internal_consistency_failure",
                        read_profile_history,
                        self.connection,
                        self.principal,
                        profile_id=created.profile_id,
                        page_size=4,
                        include_structured_profile=include,
                    )
                self.replace_revision_fields(
                    row[0],
                    canonical_schema_version=row[2],
                    structured_profile_json=row[3],
                    structured_profile_sha256=row[4],
                )

        selected = rows[1]
        other_bytes = canonical_profile_v2_json_bytes(
            canonical_fixture("prf_1234567890abcdef1234567890abcdef")
        )
        selected_cases = (
            {
                "structured_profile_json": "{}",
                "structured_profile_sha256": hashlib.sha256(b"{}").hexdigest(),
            },
            {"canonical_schema_version": "canonical_profile_v1"},
            {
                "structured_profile_json": other_bytes.decode("utf-8"),
                "structured_profile_sha256": hashlib.sha256(other_bytes).hexdigest(),
            },
        )
        for case_index, changes in enumerate(selected_cases):
            with self.subTest(selected_invalid_case=case_index):
                self.replace_revision_fields(selected[0], **changes)
                for include in (False, True):
                    self.assert_reason(
                        "internal_consistency_failure",
                        read_profile_history,
                        self.connection,
                        self.principal,
                        profile_id=created.profile_id,
                        page_size=4,
                        include_structured_profile=include,
                    )
                self.replace_revision_fields(
                    selected[0],
                    canonical_schema_version=selected[2],
                    structured_profile_json=selected[3],
                    structured_profile_sha256=selected[4],
                )

        oldest = rows[-1]
        self.replace_revision_fields(
            oldest[0],
            structured_profile_json="{",
            structured_profile_sha256=hashlib.sha256(b"{").hexdigest(),
        )
        first_page = read_profile_history(
            self.connection,
            self.principal,
            profile_id=created.profile_id,
            page_size=3,
        )
        self.assertEqual([item.revision_number for item in first_page], [4, 3, 2])
        self.assert_reason(
            "internal_consistency_failure",
            read_profile_history,
            self.connection,
            self.principal,
            profile_id=created.profile_id,
            page_size=3,
            before_revision_number=2,
        )

    def test_history_response_size_uses_exact_compact_utf8_array(self):
        class Item:
            def __init__(self, payload):
                self.payload = payload

            def trusted_dict(self, *, include_structured_profile=False):
                return self.payload

        empty = _serialize_history_page_for_size(
            (), include_structured_profile=False
        )
        self.assertEqual(empty, b"[]")
        exact_overhead = len(b'[{"padding":""}]')
        exact = Item({"padding": "x" * (MAX_HISTORY_RESPONSE_BYTES - exact_overhead)})
        self.assertEqual(
            len(
                _serialize_history_page_for_size(
                    (exact,), include_structured_profile=False
                )
            ),
            MAX_HISTORY_RESPONSE_BYTES,
        )
        too_large = Item(
            {"padding": "x" * (MAX_HISTORY_RESPONSE_BYTES - exact_overhead + 1)}
        )
        self.assertEqual(
            len(
                _serialize_history_page_for_size(
                    (too_large,), include_structured_profile=False
                )
            ),
            MAX_HISTORY_RESPONSE_BYTES + 1,
        )
        base_items = [Item({"x": ""}) for _ in range(100)]
        base_sum = sum(
            len(
                _serialize_history_page_for_size(
                    (item,), include_structured_profile=False
                )
            )
            - 2
            for item in base_items
        )
        padding = MAX_HISTORY_RESPONSE_BYTES - 50 - base_sum
        base_items[0] = Item({"x": "x" * padding})
        individual_sum = sum(
            len(
                json.dumps(
                    item.payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            for item in base_items
        )
        serialized = _serialize_history_page_for_size(
            base_items, include_structured_profile=False
        )
        self.assertLessEqual(individual_sum, MAX_HISTORY_RESPONSE_BYTES)
        self.assertEqual(len(serialized), individual_sum + 101)
        self.assertGreater(len(serialized), MAX_HISTORY_RESPONSE_BYTES)
        unicode_page = _serialize_history_page_for_size(
            (Item({"text": "Português 日本語"}),),
            include_structured_profile=False,
        )
        self.assertEqual(
            unicode_page,
            json.dumps(
                [{"text": "Português 日本語"}],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )

    def test_history_size_boundary_preserves_cursor_without_omission(self):
        _, created = self.create()
        document = self.current_document(created)
        profile_reference = reference(created, self.principal)
        for revision in range(1, 4):
            append_profile_revision(
                self.connection,
                append_command(
                    self.principal,
                    profile_reference,
                    document,
                    expected_revision=revision,
                    idempotency_key=f"history-size-{revision:04d}",
                    accepted_at=NOW + timedelta(seconds=revision),
                ),
            )
        all_items = read_profile_history(
            self.connection,
            self.principal,
            profile_id=created.profile_id,
            page_size=4,
        )
        boundary = len(
            _serialize_history_page_for_size(
                all_items[:2], include_structured_profile=False
            )
        )
        with mock.patch.object(
            repository_module, "MAX_HISTORY_RESPONSE_BYTES", boundary
        ):
            revisions = []
            cursor = None
            while True:
                page = read_profile_history(
                    self.connection,
                    self.principal,
                    profile_id=created.profile_id,
                    page_size=4,
                    before_revision_number=cursor,
                )
                if not page:
                    break
                revisions.extend(item.revision_number for item in page)
                cursor = page[-1].revision_number
        self.assertEqual(revisions, [4, 3, 2, 1])

    def test_history_rejects_a_single_item_larger_than_the_response_limit(self):
        _, created = self.create()
        with mock.patch.object(
            repository_module, "MAX_HISTORY_RESPONSE_BYTES", 1
        ):
            self.assert_reason(
                "internal_consistency_failure",
                read_profile_history,
                self.connection,
                self.principal,
                profile_id=created.profile_id,
                page_size=1,
            )

    def test_top_level_and_nested_transaction_ownership(self):
        self.connection.execute("CREATE TABLE caller_work(value TEXT)")
        self.connection.commit()
        command = create_command(self.principal)
        self.connection.execute("BEGIN")
        self.connection.execute("INSERT INTO caller_work VALUES ('keep')")
        created = create_persistent_profile(self.connection, command)
        self.assertTrue(self.connection.in_transaction)
        self.connection.rollback()
        self.assertEqual(profile_counts(self.connection), (0, 0, 0, 0))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM caller_work").fetchone()[0], 0)
        created = create_persistent_profile(self.connection, command)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(created.revision_number, 1)

    def test_outer_transaction_failure_preserves_unrelated_work(self):
        self.connection.execute("CREATE TABLE caller_work(value TEXT)")
        self.connection.commit()
        self.connection.execute("BEGIN")
        self.connection.execute("INSERT INTO caller_work VALUES ('keep')")
        repository = PersistentProfileRepository(
            _failure_injector=lambda point: (_ for _ in ()).throw(RuntimeError("private"))
            if point == "create.after_profile_insert"
            else None
        )
        self.assert_reason(
            "internal_consistency_failure",
            repository.create,
            self.connection,
            create_command(self.principal),
        )
        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM caller_work").fetchone()[0], 1)
        self.assertEqual(profile_counts(self.connection), (0, 0, 0, 0))
        self.connection.commit()

    def test_creation_failure_boundaries_rollback_every_write(self):
        exercised = []
        dynamic = [
            "create.after_profile_insert",
            "create.after_source_insert",
            "create.after_revision_insert",
            "create.after_view_verification",
            "create.before_finish",
        ]
        for index, boundary in enumerate(dynamic, start=1):
            with self.subTest(boundary=boundary):
                repository = PersistentProfileRepository(
                    _failure_injector=lambda point, target=boundary: (_ for _ in ()).throw(RuntimeError("private"))
                    if point == target
                    else None
                )
                command = create_command(
                    self.principal, idempotency_key=f"profile-failure-{index:04d}"
                )
                self.assert_reason(
                    "internal_consistency_failure",
                    repository.create,
                    self.connection,
                    command,
                )
                self.assertEqual(profile_counts(self.connection), (0, 0, 0, 0))
                exercised.append(boundary)
        self.assertEqual(len(exercised), len(CREATE_FAILURE_BOUNDARIES))

    def test_append_failure_boundaries_rollback_and_preserve_current(self):
        _, created = self.create()
        document = self.current_document(created)
        profile_reference = reference(created, self.principal)
        boundaries = [
            "append.after_source_insert",
            "append.after_revision_insert",
            "append.after_view_verification",
            "append.before_finish",
        ]
        for index, boundary in enumerate(boundaries, start=1):
            with self.subTest(boundary=boundary):
                repository = PersistentProfileRepository(
                    _failure_injector=lambda point, target=boundary: (_ for _ in ()).throw(RuntimeError("private"))
                    if point == target
                    else None
                )
                command = append_command(
                    self.principal,
                    profile_reference,
                    document,
                    idempotency_key=f"append-failure-{index:04d}",
                )
                self.assert_reason(
                    "internal_consistency_failure",
                    repository.append,
                    self.connection,
                    command,
                )
                self.assertEqual(profile_counts(self.connection), (1, 1, 1, 1))
        self.assertEqual(len(boundaries), len(APPEND_FAILURE_BOUNDARIES))

    def test_purge_requires_deletion_state_is_replay_safe_and_leaves_no_receipt(self):
        _, created = self.create()
        profile_reference = reference(created, self.principal)
        command = purge_command(profile_reference)
        self.assert_reason(
            "purge_not_allowed", purge_persistent_profile, self.connection, command
        )
        document = self.current_document(created)
        deletion = append_command(
            self.principal,
            profile_reference,
            document,
            revision_kind="deletion_request",
            idempotency_key="profile-delete-purge",
        )
        append_profile_revision(self.connection, deletion)
        self.assertEqual(purge_persistent_profile(self.connection, command).outcome, "absent_or_completed")
        self.assertEqual(profile_counts(self.connection), (0, 0, 0, 0))
        self.assertEqual(purge_persistent_profile(self.connection, command).outcome, "absent_or_completed")
        names = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master")}
        self.assertFalse(any("purge" in name for name in names))

    def test_purge_failure_boundaries_restore_all_cascades(self):
        for index, boundary in enumerate(PURGE_FAILURE_BOUNDARIES, start=1):
            with self.subTest(boundary=boundary):
                if profile_counts(self.connection)[0] == 0:
                    _, created = self.create(idempotency_key=f"create-purge-{index:04d}")
                    profile_reference = reference(created, self.principal)
                    document = self.current_document(created)
                    append_profile_revision(
                        self.connection,
                        append_command(
                            self.principal,
                            profile_reference,
                            document,
                            revision_kind="deletion_request",
                            idempotency_key=f"delete-purge-{index:04d}",
                        ),
                    )
                repository = PersistentProfileRepository(
                    _failure_injector=lambda point, target=boundary: (_ for _ in ()).throw(RuntimeError("private"))
                    if point == target
                    else None
                )
                self.assert_reason(
                    "internal_consistency_failure",
                    repository.purge,
                    self.connection,
                    purge_command(profile_reference),
                )
                self.assertEqual(profile_counts(self.connection), (1, 2, 2, 1))
                purge_persistent_profile(self.connection, purge_command(profile_reference))

    def test_raw_sqlite_failures_are_detached_and_redacted(self):
        command = create_command(self.principal)
        self.connection.close()
        error = self.assert_reason(
            "internal_consistency_failure",
            create_persistent_profile,
            self.connection,
            command,
        )
        rendered = repr(error) + str(error) + json.dumps(error.public_dict())
        self.assertNotIn(str(self.path), rendered)
        self.assertNotIn(command.profile_id, rendered)
        self.connection = connect_repository_database(self.path)

    def test_repository_import_is_dormant_and_normal_runtime_has_no_import(self):
        root = Path(__file__).resolve().parents[1]
        local_app = (root / "scripts" / "local_product_app.py").read_text(
            encoding="utf-8"
        )
        package_init = (root / "wahojobs" / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("persistent_profiles_repository", local_app)
        self.assertNotIn("persistent_profiles_repository", package_init)
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
import wahojobs.persistent_profiles_repository as repository
print(repository.MAX_HISTORY_PAGE_SIZE)
'''
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "100")


if __name__ == "__main__":
    unittest.main()
