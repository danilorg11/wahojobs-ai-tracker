import copy
from dataclasses import replace
import json
import pickle
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.accounts_test_support import install_accounts
from tests.ownership_test_support import (
    EMPTY_JSON,
    NOW_TEXT,
    add_activation_event,
    add_active_user,
    add_binding,
    add_principal,
)
from tests.persistent_profile_read_authorization_test_support import (
    ReadOnlyAuthorizationProvider,
    deactivate_account,
    file_fingerprint,
    install_authorization_database,
    request_account_deletion,
    seed_authorized_account,
    set_principal_status,
    suspend_account,
    transition_binding,
    trusted_actor,
)
from wahojobs.account_reconciliation import (
    expected_account_schema_fingerprints,
    reconcile_accounts,
)
from wahojobs.ownership import (
    BindingEventCommand,
    append_binding_event,
    event_request_fingerprint,
)
from wahojobs.ownership_reconciliation import reconcile_ownership
from wahojobs.ownership_schema import expected_ownership_manifest
from wahojobs.persistent_profile_read_authorization import (
    DurablePersistentProfileReadAuthorizationGateway,
    MAX_AUTHORIZATION_EVENTS_PER_BINDING,
    PersistentProfileReadAuthorizationDecision,
)
from wahojobs.persistent_profiles_application import (
    TrustedAuthenticatedBrowserActor,
    TrustedProfileReadGrant,
    _TRUSTED_AUTHENTICATION_ACTOR_ISSUER,
)


class PersistentProfileReadAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "authorization.sqlite"
        self.writer = install_authorization_database(self.path)
        self.state = seed_authorized_account(self.writer)
        self.writer.commit()
        self.provider = ReadOnlyAuthorizationProvider(self.path)
        self.gateway = DurablePersistentProfileReadAuthorizationGateway()
        self.actor = trusted_actor(self.state)

    def tearDown(self):
        self.writer.close()
        self.temp.cleanup()

    def authorize(self, actor=None, provider=None):
        provider = provider or self.provider
        with provider() as connection:
            return self.gateway.authorize_persistent_profile_read(
                connection,
                actor or self.actor,
            )

    def fresh_decision(self, mutate):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fresh-authorization.sqlite"
            writer = install_authorization_database(path)
            try:
                state = seed_authorized_account(writer)
                writer.commit()
                mutate(writer, state)
                writer.commit()
                provider = ReadOnlyAuthorizationProvider(path)
                with provider() as connection:
                    return self.gateway.authorize_persistent_profile_read(
                        connection,
                        trusted_actor(state),
                    )
            finally:
                writer.close()

    def fresh_parity(self, mutate):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fresh-parity.sqlite"
            writer = install_authorization_database(path)
            try:
                state = seed_authorized_account(writer)
                writer.commit()
                mutate(writer, state)
                writer.commit()
                report = reconcile_ownership(writer)
                provider = ReadOnlyAuthorizationProvider(path)
                with provider() as connection:
                    decision = self.gateway.authorize_persistent_profile_read(
                        connection,
                        trusted_actor(state),
                    )
                return report, decision
            finally:
                writer.close()

    def fresh_account_parity(self, mutate):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fresh-account-parity.sqlite"
            writer = install_authorization_database(path)
            try:
                state = seed_authorized_account(writer)
                writer.commit()
                mutate(writer, state)
                writer.commit()
                report = reconcile_accounts(writer)
                provider = ReadOnlyAuthorizationProvider(path)
                with provider() as connection:
                    decision = self.gateway.authorize_persistent_profile_read(
                        connection,
                        trusted_actor(state),
                    )
                return report, decision
            finally:
                writer.close()

    def alter_schema_definition(self, object_type, object_name, old, new):
        row = self.writer.execute(
            "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
            (object_type, object_name),
        ).fetchone()
        self.assertIsNotNone(row)
        altered = row[0].replace(old, new)
        self.assertNotEqual(altered, row[0])
        version = self.writer.execute("PRAGMA schema_version").fetchone()[0]
        self.writer.execute("PRAGMA writable_schema = ON")
        self.writer.execute(
            "UPDATE sqlite_master SET sql = ? WHERE type = ? AND name = ?",
            (altered, object_type, object_name),
        )
        self.writer.execute("PRAGMA writable_schema = OFF")
        self.writer.execute(f"PRAGMA schema_version = {version + 1}")
        self.writer.commit()

    def add_support_binding(self, suffix, *, suspended_at=None):
        principal_id = add_principal(
            self.writer,
            suffix=str(suffix),
            environment=self.state["environment"],
            principal_type="account_native",
            status="active",
            claim_policy="account_native",
            exclusive=1,
        )
        binding_id = f"pab_{suffix:032x}"
        if suspended_at is not None:
            self.writer.execute("PRAGMA ignore_check_constraints = ON")
        try:
            self.writer.execute(
                "INSERT INTO principal_account_bindings "
                "(binding_id, principal_id, user_id, environment_namespace, "
                "binding_role, binding_status, version, latest_event_version, "
                "created_at, updated_at, suspended_at, provenance_json) "
                "VALUES (?, ?, ?, ?, 'support', 'active', 1, 1, ?, ?, ?, ?)",
                (
                    binding_id,
                    principal_id,
                    self.state["account_id"],
                    self.state["environment"],
                    NOW_TEXT,
                    NOW_TEXT,
                    suspended_at,
                    EMPTY_JSON,
                ),
            )
        finally:
            if suspended_at is not None:
                self.writer.execute("PRAGMA ignore_check_constraints = OFF")
        add_activation_event(
            self.writer,
            principal_id,
            self.state["account_id"],
            binding_id,
            suffix=str(suffix),
            environment=self.state["environment"],
        )
        return principal_id, binding_id

    def corrupt_binding_suspended_at(self, binding_id):
        trigger_sql = self.writer.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'trg_principal_account_bindings_update_guard'"
        ).fetchone()[0]
        self.writer.execute("DROP TRIGGER trg_principal_account_bindings_update_guard")
        self.writer.execute("PRAGMA ignore_check_constraints = ON")
        self.writer.execute(
            "UPDATE principal_account_bindings SET suspended_at = ? WHERE binding_id = ?",
            (NOW_TEXT, binding_id),
        )
        self.writer.execute("PRAGMA ignore_check_constraints = OFF")
        self.writer.execute(trigger_sql)

    def guarded_update(self, trigger_names, operation):
        trigger_sql = [
            self.writer.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (name,),
            ).fetchone()[0]
            for name in trigger_names
        ]
        for name in trigger_names:
            self.writer.execute(f'DROP TRIGGER "{name}"')
        operation()
        for statement in trigger_sql:
            self.writer.execute(statement)
        self.writer.commit()

    def rewrite_event(self, *, occurred_at=None, request_fingerprint=None):
        row = self.writer.execute(
            "SELECT principal_id, binding_id, user_id, event_version, event_type, "
            "prior_status, resulting_status, actor_type, reason_code, "
            "approval_reference, occurred_at, metadata_json, request_fingerprint "
            "FROM ownership_binding_events WHERE binding_id = ?",
            (self.state["binding_id"],),
        ).fetchone()
        event_time = occurred_at or row[10]
        fingerprint = request_fingerprint
        if fingerprint is None:
            fingerprint = event_request_fingerprint(
                principal_id=row[0],
                binding_id=row[1],
                user_id=row[2],
                expected_event_version=row[3],
                event_type=row[4],
                prior_status=row[5],
                resulting_status=row[6],
                actor_type=row[7],
                reason_code=row[8],
                approval_reference=row[9],
                occurred_at=event_time,
                metadata=json.loads(row[11]),
            )

        def update():
            self.writer.execute(
                "UPDATE ownership_binding_events SET occurred_at = ?, "
                "request_fingerprint = ? WHERE binding_id = ?",
                (event_time, fingerprint, self.state["binding_id"]),
            )

        self.guarded_update(("trg_ownership_binding_events_no_update",), update)

    def assert_m003_rejection_blocks_grant(self, reason):
        report = reconcile_ownership(self.writer)
        self.assertIn(reason, report["blocking_reasons"])
        issuer = mock.Mock()
        issuer.issue.side_effect = AssertionError("grant issuer reached")
        with mock.patch(
            "wahojobs.persistent_profiles_application."
            "_DURABLE_PROFILE_READ_GRANT_ISSUER",
            issuer,
        ):
            decision = self.authorize()
        self.assertEqual(decision.state, "unavailable")
        issuer.issue.assert_not_called()

    def assert_unavailable_without_grant(self):
        issuer = mock.Mock()
        issuer.issue.side_effect = AssertionError("grant issuer reached")
        with mock.patch(
            "wahojobs.persistent_profiles_application."
            "_DURABLE_PROFILE_READ_GRANT_ISSUER",
            issuer,
        ):
            decision = self.authorize()
        self.assertEqual(decision.state, "unavailable")
        issuer.issue.assert_not_called()

    def extend_binding_events(self, state, total):
        self.assertGreaterEqual(total, 1)
        for version in range(2, total + 1):
            append_binding_event(
                self.writer,
                BindingEventCommand(
                    principal_id=state["principal_id"],
                    binding_id=state["binding_id"],
                    user_id=state["account_id"],
                    expected_event_version=version,
                    event_type="administrative_correction",
                    prior_status="active",
                    resulting_status="active",
                    actor_type="administrator",
                    reason_code="authorization_event_bound",
                    approval_reference="authorization-event-bound-review",
                    idempotency_key=f"authorization-event-bound-{state['binding_id'][-6:]}-{version:04d}",
                    occurred_at=NOW_TEXT,
                    metadata={},
                ),
            )
        self.writer.commit()

    def test_valid_account_native_owner_receives_exact_read_grant_without_profile(self):
        decision = self.authorize()
        self.assertEqual(decision.state, "authorized")
        grant = decision.grant_for_application()
        self.assertIs(type(grant), TrustedProfileReadGrant)
        principal = grant.principal_for_repository()
        self.assertEqual(principal.principal_id, self.state["principal_id"])
        self.assertEqual(principal.environment_namespace, self.state["environment"])
        self.assertEqual(self.gateway.scope, "persistent_profile_read")
        self.assertEqual(grant.scope, "persistent_profile_read")
        self.assertIs(grant.allows_mutation, False)

    def test_valid_lineage_has_m003_b2c2_parity(self):
        report = reconcile_ownership(self.writer)
        self.assertFalse(report["blocking"])
        self.assertEqual(self.authorize().state, "authorized")

    def test_m003_fingerprint_mismatch_never_reaches_grant_issuance(self):
        self.rewrite_event(request_fingerprint="0" * 64)
        self.assert_m003_rejection_blocks_grant(
            "ownership_event_request_fingerprint_mismatch"
        )

    def test_m003_event_time_boundary_never_reaches_grant_issuance(self):
        self.rewrite_event(occurred_at="2000-01-01T00:00:00+00:00")
        self.assert_m003_rejection_blocks_grant("event_timestamp_errors")

    def test_m003_principal_provenance_privacy_never_reaches_grant_issuance(self):
        def update():
            self.writer.execute(
                "UPDATE product_principals SET provenance_json = ? "
                "WHERE principal_id = ?",
                ('{"password":"private-marker"}', self.state["principal_id"]),
            )

        self.guarded_update(("trg_product_principals_update_guard",), update)
        self.assert_m003_rejection_blocks_grant("malformed_principal_provenance")

    def test_m003_account_principal_and_binding_time_boundaries_fail_closed(self):
        later = "2026-07-18T12:00:00+00:00"

        def account_after_event(writer, state):
            trigger = writer.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                "AND name = 'trg_users_created_at_immutable'"
            ).fetchone()[0]
            writer.execute("DROP TRIGGER trg_users_created_at_immutable")
            writer.execute(
                "UPDATE users SET created_at = ?, updated_at = ? WHERE user_id = ?",
                (later, later, state["account_id"]),
            )
            writer.execute(trigger)

        def principal_after_event(writer, state):
            names = (
                "trg_product_principals_identity_immutable",
                "trg_product_principals_update_guard",
            )
            triggers = [
                writer.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                    (name,),
                ).fetchone()[0]
                for name in names
            ]
            for name in names:
                writer.execute(f'DROP TRIGGER "{name}"')
            writer.execute(
                "UPDATE product_principals SET created_at = ?, updated_at = ? "
                "WHERE principal_id = ?",
                (later, later, state["principal_id"]),
            )
            for trigger in triggers:
                writer.execute(trigger)

        def binding_after_event(writer, state):
            trigger = writer.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                "AND name = 'trg_principal_account_bindings_update_guard'"
            ).fetchone()[0]
            writer.execute("DROP TRIGGER trg_principal_account_bindings_update_guard")
            writer.execute(
                "UPDATE principal_account_bindings SET created_at = ?, updated_at = ? "
                "WHERE binding_id = ?",
                (later, later, state["binding_id"]),
            )
            writer.execute(trigger)

        for label, mutate in (
            ("account_identity", account_after_event),
            ("principal", principal_after_event),
            ("binding", binding_after_event),
        ):
            with self.subTest(boundary=label):
                report, decision = self.fresh_parity(mutate)
                self.assertTrue(report["blocking"])
                self.assertEqual(decision.state, "unavailable")

    def test_m003_event_chronology_has_b2c2_parity(self):
        transition_binding(self.writer, self.state, "suspended")
        row = self.writer.execute(
            "SELECT principal_id, binding_id, user_id, event_version, event_type, "
            "prior_status, resulting_status, actor_type, reason_code, "
            "approval_reference, metadata_json FROM ownership_binding_events "
            "WHERE binding_id = ? AND event_version = 2",
            (self.state["binding_id"],),
        ).fetchone()
        occurred_at = "2000-01-01T00:00:00+00:00"
        fingerprint = event_request_fingerprint(
            principal_id=row[0],
            binding_id=row[1],
            user_id=row[2],
            expected_event_version=row[3],
            event_type=row[4],
            prior_status=row[5],
            resulting_status=row[6],
            actor_type=row[7],
            reason_code=row[8],
            approval_reference=row[9],
            occurred_at=occurred_at,
            metadata=json.loads(row[10]),
        )

        def update():
            self.writer.execute(
                "UPDATE ownership_binding_events SET occurred_at = ?, "
                "request_fingerprint = ? WHERE binding_id = ? AND event_version = 2",
                (occurred_at, fingerprint, self.state["binding_id"]),
            )

        self.guarded_update(("trg_ownership_binding_events_no_update",), update)
        self.assert_m003_rejection_blocks_grant("event_timestamp_errors")

    def test_malformed_supporting_identity_blocks_grant_with_m002_parity(self):
        trigger = "trg_auth_identities_immutable_identity"

        def update():
            self.writer.execute("PRAGMA ignore_check_constraints = ON")
            self.writer.execute(
                "UPDATE auth_identities SET created_at = 'invalid' WHERE user_id = ?",
                (self.state["account_id"],),
            )
            self.writer.execute("PRAGMA ignore_check_constraints = OFF")

        self.guarded_update((trigger,), update)
        report = reconcile_accounts(self.writer)
        self.assertTrue(report["checks"]["malformed_auth_identities"])
        self.assert_unavailable_without_grant()

    def test_incompatible_identity_storage_class_blocks_grant(self):
        trigger = "trg_auth_identities_immutable_identity"

        def update():
            self.writer.execute(
                "UPDATE auth_identities SET auth_identity_id = ? WHERE user_id = ?",
                (sqlite3.Binary(b"private-identity-marker"), self.state["account_id"]),
            )

        self.guarded_update((trigger,), update)
        report = reconcile_accounts(self.writer)
        self.assertTrue(report["checks"]["malformed_auth_identities"])
        self.assert_unavailable_without_grant()

    def test_missing_supporting_identity_blocks_grant(self):
        self.writer.execute(
            "DELETE FROM auth_identities WHERE user_id = ?",
            (self.state["account_id"],),
        )
        self.writer.commit()
        self.assert_unavailable_without_grant()

    def test_identity_linked_to_another_account_cannot_support_actor(self):
        other_account = add_active_user(self.writer, "authorization-other-identity")
        self.writer.execute(
            "DELETE FROM auth_identities WHERE user_id = ?",
            (other_account,),
        )
        trigger_sql = self.writer.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'trg_auth_identities_immutable_identity'"
        ).fetchone()[0]
        self.writer.execute("DROP TRIGGER trg_auth_identities_immutable_identity")
        self.writer.execute(
            "UPDATE auth_identities SET user_id = ? WHERE user_id = ?",
            (other_account, self.state["account_id"]),
        )
        self.writer.execute(trigger_sql)
        self.writer.commit()
        self.assert_unavailable_without_grant()

    def test_identity_inventory_overflow_fails_without_prefix_authorization(self):
        self.writer.execute("PRAGMA ignore_check_constraints = ON")
        try:
            for ordinal in range(1, 17):
                self.writer.execute(
                    "INSERT INTO auth_identities "
                    "(auth_identity_id, user_id, provider, provider_subject, "
                    "verified_email, email_verified, created_at, last_authenticated_at, "
                    "disabled_at, link_idempotency_key, request_fingerprint) "
                    "VALUES (?, ?, ?, ?, NULL, 0, ?, ?, NULL, ?, ?)",
                    (
                        f"auth_{ordinal + 10_000:032x}",
                        self.state["account_id"],
                        f"test_provider_{ordinal:02d}",
                        f"test-subject-{ordinal:02d}",
                        NOW_TEXT,
                        NOW_TEXT,
                        f"authorization-identity-overflow-{ordinal:02d}",
                        f"{ordinal:064x}",
                    ),
                )
        finally:
            self.writer.execute("PRAGMA ignore_check_constraints = OFF")
        self.writer.commit()
        self.assert_unavailable_without_grant()

    def test_supporting_identity_corruption_matrix_has_m002_b2c2_parity(self):
        def corrupt(field, value, *, immutable=False):
            def mutate(writer, state):
                trigger_sql = None
                if immutable:
                    trigger_sql = writer.execute(
                        "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                        "AND name = 'trg_auth_identities_immutable_identity'"
                    ).fetchone()[0]
                    writer.execute("DROP TRIGGER trg_auth_identities_immutable_identity")
                writer.execute("PRAGMA ignore_check_constraints = ON")
                writer.execute(
                    f'UPDATE auth_identities SET "{field}" = ? WHERE user_id = ?',
                    (value, state["account_id"]),
                )
                writer.execute("PRAGMA ignore_check_constraints = OFF")
                if trigger_sql is not None:
                    writer.execute(trigger_sql)

            return mutate

        cases = (
            ("created_at", "2000-01-01T00:00:00+00:00", False),
            ("last_authenticated_at", "invalid", False),
            ("disabled_at", "2000-01-01T00:00:00+00:00", False),
            ("email_verified", 2, False),
            ("provider", "unsupported", True),
            ("auth_identity_id", "invalid", False),
            ("link_idempotency_key", sqlite3.Binary(b"private-marker"), False),
            ("request_fingerprint", "A" * 64, False),
        )
        for field, value, immutable in cases:
            with self.subTest(field=field):
                report, decision = self.fresh_account_parity(
                    corrupt(field, value, immutable=immutable)
                )
                self.assertTrue(report["checks"]["malformed_auth_identities"])
                self.assertEqual(decision.state, "unavailable")

    def test_nonwinning_principal_environment_mismatch_blocks_grant(self):
        principal_id, _binding_id = self.add_support_binding(1_010)

        def update():
            self.writer.execute(
                "UPDATE product_principals SET environment_namespace = 'production', "
                "version = version + 1 WHERE principal_id = ?",
                (principal_id,),
            )

        self.guarded_update(
            (
                "trg_product_principals_identity_immutable",
                "trg_product_principals_update_guard",
            ),
            update,
        )
        report = reconcile_ownership(self.writer)
        self.assertIn("binding_environment_mismatches", report["blocking_reasons"])
        self.assert_unavailable_without_grant()

    def test_nonwinning_event_environment_mismatch_blocks_grant(self):
        _principal_id, binding_id = self.add_support_binding(1_011)

        def update():
            self.writer.execute(
                "UPDATE ownership_binding_events SET environment_namespace = 'production' "
                "WHERE binding_id = ?",
                (binding_id,),
            )

        self.guarded_update(("trg_ownership_binding_events_no_update",), update)
        report = reconcile_ownership(self.writer)
        self.assertIn("event_environment_mismatches", report["blocking_reasons"])
        self.assert_unavailable_without_grant()

    def test_nonwinning_active_binding_to_dormant_principal_blocks_grant(self):
        principal_id, _binding_id = self.add_support_binding(1_012)
        set_principal_status(
            self.writer,
            {"principal_id": principal_id},
            "dormant",
        )
        report = reconcile_ownership(self.writer)
        self.assertIn(
            "bindings_to_unavailable_principals",
            report["blocking_reasons"],
        )
        self.assert_unavailable_without_grant()

    def test_missing_nonwinning_principal_blocks_grant(self):
        principal_id, _binding_id = self.add_support_binding(1_013)
        trigger_sql = self.writer.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'trg_product_principals_no_delete'"
        ).fetchone()[0]
        self.writer.commit()
        self.writer.execute("PRAGMA foreign_keys = OFF")
        self.writer.execute("DROP TRIGGER trg_product_principals_no_delete")
        self.writer.execute(
            "DELETE FROM product_principals WHERE principal_id = ?",
            (principal_id,),
        )
        self.writer.execute(trigger_sql)
        self.writer.commit()
        self.writer.execute("PRAGMA foreign_keys = ON")
        report = reconcile_ownership(self.writer)
        self.assertIn("orphan_bindings", report["blocking_reasons"])
        self.assert_unavailable_without_grant()

    def test_malformed_nonwinning_principal_blocks_grant(self):
        principal_id, _binding_id = self.add_support_binding(1_014)

        def update():
            self.writer.execute("PRAGMA ignore_check_constraints = ON")
            self.writer.execute(
                "UPDATE product_principals SET updated_at = 'invalid', "
                "version = version + 1 WHERE principal_id = ?",
                (principal_id,),
            )
            self.writer.execute("PRAGMA ignore_check_constraints = OFF")

        self.guarded_update(("trg_product_principals_update_guard",), update)
        self.assert_unavailable_without_grant()

    def test_valid_released_support_lineage_does_not_block_winner(self):
        principal_id, binding_id = self.add_support_binding(1_015)
        transition_binding(
            self.writer,
            {
                "principal_id": principal_id,
                "binding_id": binding_id,
                "account_id": self.state["account_id"],
            },
            "released",
        )
        self.assertFalse(reconcile_ownership(self.writer)["blocking"])
        self.assertEqual(self.authorize().state, "authorized")

    def test_exact_event_inventory_cap_authorizes(self):
        self.extend_binding_events(
            self.state,
            MAX_AUTHORIZATION_EVENTS_PER_BINDING,
        )
        self.assertFalse(reconcile_ownership(self.writer)["blocking"])
        self.assertEqual(self.authorize().state, "authorized")

    def test_event_inventory_overflow_fails_without_grant(self):
        self.extend_binding_events(
            self.state,
            MAX_AUTHORIZATION_EVENTS_PER_BINDING + 1,
        )
        self.assertFalse(reconcile_ownership(self.writer)["blocking"])
        self.assert_unavailable_without_grant()

    def test_malformed_overflow_event_cannot_authorize_from_prefix(self):
        self.extend_binding_events(
            self.state,
            MAX_AUTHORIZATION_EVENTS_PER_BINDING + 1,
        )

        def update():
            self.writer.execute(
                "UPDATE ownership_binding_events SET request_fingerprint = ? "
                "WHERE binding_id = ? AND event_version = ?",
                (
                    "0" * 64,
                    self.state["binding_id"],
                    MAX_AUTHORIZATION_EVENTS_PER_BINDING + 1,
                ),
            )

        self.guarded_update(("trg_ownership_binding_events_no_update",), update)
        self.assert_unavailable_without_grant()

    def test_large_event_inventory_overflow_fails_without_grant(self):
        self.extend_binding_events(self.state, 201)
        self.assertFalse(reconcile_ownership(self.writer)["blocking"])
        self.assert_unavailable_without_grant()

    def test_malformed_final_accepted_event_blocks_grant(self):
        self.extend_binding_events(
            self.state,
            MAX_AUTHORIZATION_EVENTS_PER_BINDING,
        )

        def update():
            self.writer.execute(
                "UPDATE ownership_binding_events SET request_fingerprint = ? "
                "WHERE binding_id = ? AND event_version = ?",
                (
                    "0" * 64,
                    self.state["binding_id"],
                    MAX_AUTHORIZATION_EVENTS_PER_BINDING,
                ),
            )

        self.guarded_update(("trg_ownership_binding_events_no_update",), update)
        self.assert_unavailable_without_grant()

    def test_nonwinning_event_overflow_blocks_grant(self):
        principal_id, binding_id = self.add_support_binding(1_016)
        self.extend_binding_events(
            {
                "principal_id": principal_id,
                "binding_id": binding_id,
                "account_id": self.state["account_id"],
            },
            MAX_AUTHORIZATION_EVENTS_PER_BINDING + 1,
        )
        self.assert_unavailable_without_grant()

    def test_valid_winner_last_in_binding_order_still_authorizes(self):
        self.add_support_binding(1)
        self.writer.commit()
        ordered = self.writer.execute(
            "SELECT binding_id FROM principal_account_bindings "
            "WHERE user_id = ? ORDER BY binding_id",
            (self.state["account_id"],),
        ).fetchall()
        self.assertEqual(ordered[-1][0], self.state["binding_id"])
        self.assertEqual(self.authorize().state, "authorized")

    def test_actor_account_reference_is_bounded_redacted_and_optional_for_legacy_tests(self):
        private = self.state["account_id"]
        self.assertNotIn(private, repr(self.actor))
        self.assertNotIn(private, str(self.actor))
        with self.assertRaises(AttributeError):
            self.actor._account_id = private
        for operation in (
            lambda: copy.copy(self.actor),
            lambda: copy.deepcopy(self.actor),
            lambda: pickle.dumps(self.actor),
        ):
            with self.assertRaises(TypeError):
                operation()
        legacy_actor = _TRUSTED_AUTHENTICATION_ACTOR_ISSUER.issue(
            "legacy-test-actor"
        )
        self.assertEqual(self.authorize(legacy_actor).state, "unavailable")

    def test_actor_and_durable_grant_value_construction_are_sealed(self):
        copied_actor = {
            "actor_key": "copied-request",
            "account_id": self.state["account_id"],
            "environment_namespace": self.state["environment"],
        }
        for operation in (
            lambda: TrustedAuthenticatedBrowserActor("direct"),
            lambda: TrustedAuthenticatedBrowserActor(**copied_actor),
            lambda: replace(self.actor),
        ):
            with self.assertRaises((TypeError, ValueError)) as caught:
                operation()
            self.assertNotIn(self.state["account_id"], str(caught.exception))

        principal = self.authorize().grant_for_application().principal_for_repository()
        copied_grant = {"principal": principal, "scope": "persistent_profile_write"}
        for operation in (
            lambda: TrustedProfileReadGrant(principal),
            lambda: TrustedProfileReadGrant(**copied_grant),
            lambda: replace(self.authorize().grant_for_application()),
        ):
            with self.assertRaises((TypeError, ValueError)):
                operation()

    def test_cold_and_repeated_authorization_create_no_auxiliary_connection(self):
        with self.provider() as connection, mock.patch(
            "sqlite3.connect",
            side_effect=AssertionError("auxiliary connection attempted"),
        ) as connect:
            self.assertTrue(expected_account_schema_fingerprints())
            self.assertTrue(expected_ownership_manifest()["fingerprint"])
            gateway = DurablePersistentProfileReadAuthorizationGateway()
            first = gateway.authorize_persistent_profile_read(connection, self.actor)
            second = gateway.authorize_persistent_profile_read(connection, self.actor)
        self.assertEqual((first.state, second.state), ("authorized", "authorized"))
        self.assertEqual(connect.call_count, 0)

    def test_schema_and_malformed_failures_create_no_auxiliary_connection(self):
        self.writer.execute("DROP INDEX idx_principal_account_bindings_user_status")
        self.writer.commit()
        with self.provider() as connection, mock.patch(
            "sqlite3.connect",
            side_effect=AssertionError("auxiliary connection attempted"),
        ) as connect:
            decision = self.gateway.authorize_persistent_profile_read(
                connection, self.actor
            )
        self.assertEqual(decision.state, "unavailable")
        self.assertEqual(connect.call_count, 0)

    def test_malformed_row_failure_creates_no_auxiliary_connection(self):
        self.writer.execute("PRAGMA ignore_check_constraints = ON")
        self.writer.execute(
            "UPDATE users SET row_version = 0 WHERE user_id = ?",
            (self.state["account_id"],),
        )
        self.writer.execute("PRAGMA ignore_check_constraints = OFF")
        self.writer.commit()
        with self.provider() as connection, mock.patch(
            "sqlite3.connect",
            side_effect=AssertionError("auxiliary connection attempted"),
        ) as connect:
            decision = self.gateway.authorize_persistent_profile_read(
                connection, self.actor
            )
        self.assertEqual(decision.state, "unavailable")
        self.assertEqual(connect.call_count, 0)

    def test_malformed_account_rows_fail_unavailable(self):
        def corrupt_version(writer, state):
            writer.execute("PRAGMA ignore_check_constraints = ON")
            writer.execute(
                "UPDATE users SET row_version = 0 WHERE user_id = ?",
                (state["account_id"],),
            )
            writer.execute("PRAGMA ignore_check_constraints = OFF")

        def corrupt_lifecycle_time(writer, state):
            writer.execute("PRAGMA ignore_check_constraints = ON")
            writer.execute(
                "UPDATE users SET deletion_requested_at = ? WHERE user_id = ?",
                (NOW_TEXT, state["account_id"]),
            )
            writer.execute("PRAGMA ignore_check_constraints = OFF")

        self.assertEqual(self.fresh_decision(corrupt_version).state, "unavailable")
        self.assertEqual(self.fresh_decision(corrupt_lifecycle_time).state, "unavailable")

    def test_malformed_principal_timestamp_fails_unavailable(self):
        def corrupt(writer, state):
            writer.execute("PRAGMA ignore_check_constraints = ON")
            writer.execute(
                "UPDATE product_principals SET updated_at = 'invalid', "
                "version = version + 1 WHERE principal_id = ?",
                (state["principal_id"],),
            )
            writer.execute("PRAGMA ignore_check_constraints = OFF")

        self.assertEqual(self.fresh_decision(corrupt).state, "unavailable")

    def test_malformed_nonwinning_binding_fails_unavailable(self):
        self.add_support_binding(1_000, suspended_at=NOW_TEXT)
        self.writer.commit()
        self.assertEqual(self.authorize().state, "unavailable")

    def test_valid_and_malformed_released_history_are_distinguished(self):
        principal_id, binding_id = self.add_support_binding(1_001)
        state = {
            "principal_id": principal_id,
            "binding_id": binding_id,
            "account_id": self.state["account_id"],
        }
        transition_binding(self.writer, state, "released")
        self.assertEqual(self.authorize().state, "authorized")
        self.corrupt_binding_suspended_at(binding_id)
        self.writer.commit()
        self.assertEqual(self.authorize().state, "unavailable")

    def test_malformed_inactive_principal_in_historical_lineage_fails_unavailable(self):
        principal_id, binding_id = self.add_support_binding(1_002)
        transition_binding(
            self.writer,
            {
                "principal_id": principal_id,
                "binding_id": binding_id,
                "account_id": self.state["account_id"],
            },
            "released",
        )
        self.writer.execute("PRAGMA ignore_check_constraints = ON")
        self.writer.execute(
            "UPDATE product_principals SET lifecycle_status = 'retired', "
            "updated_at = 'invalid', version = version + 1 WHERE principal_id = ?",
            (principal_id,),
        )
        self.writer.execute("PRAGMA ignore_check_constraints = OFF")
        self.writer.commit()
        self.assertEqual(self.authorize().state, "unavailable")

    def test_binding_inventory_cap_is_exact_and_fails_closed(self):
        for suffix in range(1_100, 1_163):
            self.add_support_binding(suffix)
        self.writer.commit()
        self.assertEqual(self.authorize().state, "authorized")
        self.add_support_binding(1_163)
        self.writer.commit()
        self.assertEqual(self.authorize().state, "unavailable")

    def test_malformed_overflow_row_cannot_authorize_from_valid_prefix(self):
        for suffix in range(1_200, 1_263):
            self.add_support_binding(suffix)
        self.add_support_binding(1_263, suspended_at=NOW_TEXT)
        self.writer.commit()
        self.assertEqual(self.authorize().state, "unavailable")

    def test_missing_account_and_wrong_environment_are_generic_denials(self):
        absent = _TRUSTED_AUTHENTICATION_ACTOR_ISSUER.issue(
            "absent-account",
            account_id="usr_ffffffffffffffffffffffffffffffff",
            environment_namespace=self.state["environment"],
        )
        self.assertEqual(self.authorize(absent).state, "denied")
        self.assertEqual(
            self.authorize(trusted_actor(self.state, environment="production")).state,
            "denied",
        )

    def test_active_account_without_binding_is_denied(self):
        account_id = add_active_user(self.writer, "authorization-unbound")
        self.writer.commit()
        actor = _TRUSTED_AUTHENTICATION_ACTOR_ISSUER.issue(
            "unbound-account",
            account_id=account_id,
            environment_namespace=self.state["environment"],
        )
        self.assertEqual(self.authorize(actor).state, "denied")

    def test_suspended_account_is_denied(self):
        suspend_account(self.writer, self.state)
        self.assertEqual(self.authorize().state, "denied")

    def test_deletion_requested_account_is_denied(self):
        request_account_deletion(self.writer, self.state)
        self.assertEqual(self.authorize().state, "denied")

    def test_deactivated_account_is_denied(self):
        deactivate_account(self.writer, self.state)
        self.assertEqual(self.authorize().state, "denied")

    def test_suspended_binding_is_denied(self):
        transition_binding(self.writer, self.state, "suspended")
        self.assertEqual(self.authorize().state, "denied")

    def test_released_binding_is_denied(self):
        transition_binding(self.writer, self.state, "released")
        self.assertEqual(self.authorize().state, "denied")

    def test_nonactive_principal_lifecycles_are_denied(self):
        for status in ("dormant", "suspended", "retired"):
            with self.subTest(status=status):
                decision = self.fresh_decision(
                    lambda writer, state, value=status: set_principal_status(
                        writer, state, value
                    )
                )
                self.assertEqual(decision.state, "denied")

    def test_multiple_active_owner_bindings_are_unavailable_not_first_row_selected(self):
        other_principal = add_principal(
            self.writer,
            suffix="84",
            environment=self.state["environment"],
            principal_type="account_native",
            status="active",
            claim_policy="account_native",
            exclusive=1,
        )
        other_binding = add_binding(
            self.writer,
            other_principal,
            self.state["account_id"],
            suffix="84",
            environment=self.state["environment"],
        )
        add_activation_event(
            self.writer,
            other_principal,
            self.state["account_id"],
            other_binding,
            suffix="84",
            environment=self.state["environment"],
        )
        self.writer.commit()
        self.assertEqual(self.authorize().state, "unavailable")

    def test_released_binding_plus_one_active_owner_is_unambiguous(self):
        transition_binding(self.writer, self.state, "released")
        other_principal = add_principal(
            self.writer,
            suffix="85",
            environment=self.state["environment"],
            principal_type="account_native",
            status="active",
            claim_policy="account_native",
            exclusive=1,
        )
        other_binding = add_binding(
            self.writer,
            other_principal,
            self.state["account_id"],
            suffix="85",
            environment=self.state["environment"],
        )
        add_activation_event(
            self.writer,
            other_principal,
            self.state["account_id"],
            other_binding,
            suffix="85",
            environment=self.state["environment"],
        )
        self.writer.commit()
        decision = self.authorize()
        self.assertEqual(decision.state, "authorized")
        self.assertEqual(
            decision.grant_for_application().principal_for_repository().principal_id,
            other_principal,
        )

    def test_malformed_nonselected_binding_lineage_is_unavailable(self):
        transition_binding(self.writer, self.state, "released")
        self.writer.execute(
            "UPDATE principal_account_bindings SET version = 3, "
            "latest_event_version = 3 WHERE binding_id = ?",
            (self.state["binding_id"],),
        )
        other_principal = add_principal(
            self.writer,
            suffix="87",
            environment=self.state["environment"],
            principal_type="account_native",
            status="active",
            claim_policy="account_native",
            exclusive=1,
        )
        other_binding = add_binding(
            self.writer,
            other_principal,
            self.state["account_id"],
            suffix="87",
            environment=self.state["environment"],
        )
        add_activation_event(
            self.writer,
            other_principal,
            self.state["account_id"],
            other_binding,
            suffix="87",
            environment=self.state["environment"],
        )
        self.writer.commit()
        report = reconcile_ownership(self.writer)
        self.assertTrue(report["blocking"])
        self.assertIn(
            "ownership_binding_projection_mismatch",
            report["blocking_reasons"],
        )
        self.assertEqual(self.authorize().state, "unavailable")

    def test_special_principal_types_are_denied_by_default(self):
        claim_policy = {
            "legacy_profile": "manual_approval",
            "development": "nonclaimable",
            "sample": "nonclaimable",
            "system": "nonclaimable",
        }

        def make_special(writer, state, principal_type):
            trigger_sql = writer.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                "AND name = 'trg_product_principals_identity_immutable'"
            ).fetchone()[0]
            writer.execute("DROP TRIGGER trg_product_principals_identity_immutable")
            writer.execute(
                "UPDATE product_principals SET principal_type = ?, claim_policy = ?, "
                "version = version + 1 WHERE principal_id = ?",
                (
                    principal_type,
                    claim_policy[principal_type],
                    state["principal_id"],
                ),
            )
            writer.execute(trigger_sql)

        for principal_type in claim_policy:
            with self.subTest(principal_type=principal_type):
                decision = self.fresh_decision(
                    lambda writer, state, value=principal_type: make_special(
                        writer, state, value
                    )
                )
                self.assertEqual(decision.state, "denied")

    def test_missing_ownership_index_is_unavailable(self):
        self.writer.execute("DROP INDEX idx_principal_account_bindings_user_status")
        self.writer.commit()
        self.assertEqual(self.authorize().state, "unavailable")

    def test_m002_only_and_missing_m003_marker_are_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            m002_path = Path(tmp) / "m002.sqlite"
            m002_writer = install_accounts(m002_path)
            m002_writer.commit()
            m002_provider = ReadOnlyAuthorizationProvider(m002_path)
            with m002_provider() as connection:
                decision = self.gateway.authorize_persistent_profile_read(
                    connection,
                    self.actor,
                )
            self.assertEqual(decision.state, "unavailable")
            m002_writer.close()

        self.writer.execute(
            "DELETE FROM wahojobs_schema_migrations WHERE version = ?",
            ("003_product_principals",),
        )
        self.writer.commit()
        self.assertEqual(self.authorize().state, "unavailable")

    def test_weakened_binding_constraint_is_unavailable(self):
        self.alter_schema_definition(
            "table",
            "principal_account_bindings",
            "CHECK (binding_status IN ('active', 'suspended', 'released'))",
            "CHECK (binding_status IN ('active', 'suspended', 'released', 'other'))",
        )
        self.assertEqual(self.authorize().state, "unavailable")

    def test_altered_principal_constraint_is_unavailable(self):
        self.alter_schema_definition(
            "table",
            "product_principals",
            "principal_type IN ('legacy_profile', 'account_native', 'development', "
            "'sample', 'system')",
            "principal_type IN ('legacy_profile', 'account_native', 'development', "
            "'sample', 'system', 'other')",
        )
        self.assertEqual(self.authorize().state, "unavailable")

    def test_wrong_capability_descriptor_is_unavailable(self):
        with mock.patch(
            "wahojobs.persistent_profile_read_authorization._expected_accounts_manifest",
            return_value={},
        ):
            self.assertEqual(self.authorize().state, "unavailable")

    def test_foreign_keys_query_only_and_exact_actor_type_fail_closed(self):
        class ActorSubclass(TrustedAuthenticatedBrowserActor):
            pass

        with self.assertRaises(ValueError):
            ActorSubclass(
                "forged-subclass",
                account_id=self.state["account_id"],
                environment_namespace=self.state["environment"],
            )
        self.assertEqual(
            self.gateway.authorize_persistent_profile_read(self.writer, self.actor).state,
            "unavailable",
        )
        connection = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro",
            uri=True,
        )
        try:
            connection.execute("PRAGMA query_only = ON")
            self.assertEqual(
                self.gateway.authorize_persistent_profile_read(
                    connection, self.actor
                ).state,
                "unavailable",
            )
        finally:
            connection.close()

    def test_actor_reference_failure_is_sanitized(self):
        marker = "private-actor-reference-marker"
        with mock.patch.object(
            TrustedAuthenticatedBrowserActor,
            "account_reference_for_authorization",
            side_effect=RuntimeError(marker),
        ):
            decision = self.authorize()
        self.assertEqual(decision.state, "unavailable")
        self.assertNotIn(marker, repr(decision))

    def test_decision_and_grant_are_private_and_not_forgeable_from_account_id(self):
        decision = self.authorize()
        text = repr(decision) + str(decision)
        for private in (
            self.state["account_id"],
            self.state["binding_id"],
            self.state["principal_id"],
        ):
            self.assertNotIn(private, text)
        for operation in (
            lambda: copy.copy(decision),
            lambda: copy.deepcopy(decision),
            lambda: pickle.dumps(decision),
        ):
            with self.assertRaises(TypeError):
                operation()
        with self.assertRaises(TypeError):
            vars(decision)
        with self.assertRaises(ValueError):
            PersistentProfileReadAuthorizationDecision(
                "authorized", self.state["account_id"]
            )
        with self.assertRaises(ValueError):
            TrustedProfileReadGrant(self.state["account_id"])

    def test_gateway_preserves_caller_transaction_and_writes_nothing(self):
        before = file_fingerprint(self.path)
        with self.provider() as connection:
            connection.execute("BEGIN")
            decision = self.gateway.authorize_persistent_profile_read(connection, self.actor)
            self.assertTrue(connection.in_transaction)
            connection.rollback()
        self.assertEqual(decision.state, "authorized")
        self.assertEqual(file_fingerprint(self.path), before)
        self.assertEqual(self.provider.opened, self.provider.closed)

    def test_private_faults_become_unavailable_without_exception_retention(self):
        marker = "private-authorization-fault-marker"
        with mock.patch(
            "wahojobs.persistent_profile_read_authorization._authorization_schema_available",
            side_effect=RuntimeError(marker),
        ):
            decision = self.authorize()
        self.assertEqual(decision.state, "unavailable")
        self.assertNotIn(marker, repr(decision))
        self.assertNotIn(marker, str(decision))

    def test_lookup_and_result_construction_faults_are_sanitized(self):
        marker = "private-durable-read-fault-marker"
        for target in ("_rows", "_authorized"):
            with self.subTest(target=target), mock.patch(
                f"wahojobs.persistent_profile_read_authorization.{target}",
                side_effect=RuntimeError(marker),
            ):
                decision = self.authorize()
                self.assertEqual(decision.state, "unavailable")
                self.assertNotIn(marker, repr(decision))
                self.assertNotIn(marker, str(decision))

    def test_authorization_module_contains_no_profile_queries_or_mutation_sql(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "wahojobs"
            / "persistent_profile_read_authorization.py"
        ).read_text(encoding="utf-8")
        for prohibited in (
            "FROM product_profiles",
            "FROM product_profile_revisions",
            "FROM product_profile_sources",
            "FROM current_product_profiles",
            "INSERT INTO",
            "UPDATE ",
            "DELETE FROM",
            "CREATE TABLE",
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, source)


if __name__ == "__main__":
    unittest.main()
