from __future__ import annotations

import inspect
import sqlite3
import threading
import unittest
from datetime import timedelta
from unittest import mock

from tests.google_oidc_authorization_transactions_test_support import (
    authorization_parameters,
    close_secret_vault,
    completion_policy,
    durable_transaction_database,
    key_authority,
    make_real_gateway,
    NOW,
    open_connection,
    request_secret_vault,
    sockets_blocked,
    transaction_rows,
    vault_entry_count,
)
import wahojobs.google_oidc_authorization_transaction_repository as repository
import wahojobs.google_oidc_durable_gateway as durable_gateway_module
import wahojobs.google_oidc_gateway as gateway_module
import wahojobs.google_oidc_transaction_protection as protection
from tests.accounts_test_support import INVITATION_KEY
from wahojobs import accounts
from wahojobs.google_oidc_authorization_transactions import (
    MAX_DURABLE_CONFIGURATION_CONTEXT_BYTES,
    PreparedDurableGoogleOidcAuthorization,
)
from wahojobs.google_oidc_durable_gateway import (
    complete_browser_bound_durable_google_oidc_authorization,
    complete_durable_google_oidc_authorization,
    prepare_durable_google_oidc_authorization,
)
from wahojobs.google_oidc_gateway import GoogleOidcGatewayFailure


class DurableGoogleOidcGatewayTests(unittest.TestCase):
    def setUp(self):
        self.socket_guard = sockets_blocked()
        self.socket_guard.__enter__()
        self.addCleanup(self.socket_guard.__exit__, None, None, None)
        self.resources = []

    def keep(self, value):
        self.resources.append(value)
        close = getattr(value, "close", None)
        if callable(close):
            self.addCleanup(close)
        return value

    def database(self, suffix):
        context = durable_transaction_database(suffix=suffix)
        value = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return value

    def vault(self):
        value = request_secret_vault()
        self.addCleanup(close_secret_vault, value)
        return value

    def invitation(
        self,
        database,
        suffix,
        email,
        *,
        now=NOW,
        expires_at=None,
    ):
        return accounts.create_invitation(
            database.connection,
            email=email,
            lookup_key=INVITATION_KEY,
            expires_at=expires_at or now + timedelta(days=7),
            created_by="b23b_test_operator",
            idempotency_key=f"b23b-invitation-{suffix}",
            now=now,
        )

    def prepare(
        self,
        suffix,
        *,
        invitation_credential=None,
        **gateway_options,
    ):
        database = self.database(suffix)
        authority = self.keep(key_authority())
        harness = self.keep(
            make_real_gateway(
                subject=database.subject,
                **gateway_options,
            )
        )
        prepared = prepare_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            invitation_credential=invitation_credential,
        )
        self.assertIs(type(prepared), PreparedDurableGoogleOidcAuthorization)
        return database, authority, harness, prepared

    def test_public_functions_accept_no_injected_protocol_or_repository(self):
        self.assertEqual(
            tuple(
                inspect.signature(
                    prepare_durable_google_oidc_authorization
                ).parameters
            ),
            (
                "connection",
                "gateway",
                "key_authority",
                "invitation_credential",
            ),
        )
        self.assertEqual(
            tuple(
                inspect.signature(
                    complete_browser_bound_durable_google_oidc_authorization
                ).parameters
            ),
            (
                "connection",
                "gateway",
                "key_authority",
                "callback_url",
                "browser_transaction_id",
                "completion_policy",
                "request_secret_vault",
            ),
        )
        self.assertEqual(
            tuple(
                inspect.signature(
                    complete_durable_google_oidc_authorization
                ).parameters
            ),
            (
                "connection",
                "gateway",
                "key_authority",
                "callback_url",
                "completion_policy",
                "request_secret_vault",
            ),
        )
        self.assertEqual(
            durable_gateway_module.__all__,
            (
                "complete_browser_bound_durable_google_oidc_authorization",
                "complete_durable_google_oidc_authorization",
                "prepare_durable_google_oidc_authorization",
            ),
        )

    def test_preparation_commits_and_rereads_before_url_is_issued(self):
        database = self.database("commit-before-url")
        authority = self.keep(key_authority())
        harness = self.keep(make_real_gateway(subject=database.subject))
        original_issue = repository._issue_prepared_authorization
        observations = []

        def observe(**values):
            observations.append(
                (
                    database.connection.in_transaction,
                    transaction_rows(database.connection)[0]["lifecycle"],
                )
            )
            return original_issue(**values)

        with mock.patch.object(
            repository,
            "_issue_prepared_authorization",
            side_effect=observe,
        ):
            prepared = prepare_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
            )
        self.assertIs(type(prepared), PreparedDurableGoogleOidcAuthorization)
        self.assertEqual(observations, [(False, "prepared")])
        self.assertIn("state", authorization_parameters(prepared))

    def test_real_provider_completion_delegates_unchanged_b2d1_and_replay_stops(self):
        database, authority, harness, prepared = self.prepare("complete")
        callback = harness.transport.callback_for(prepared)
        vault = self.vault()
        first = complete_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            callback,
            completion_policy(),
            vault,
        )
        self.assertEqual(first.status, "issued")
        self.assertEqual(vault_entry_count(vault), 1)
        self.assertEqual(transaction_rows(database.connection)[0]["lifecycle"], "consumed")
        replay = complete_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            callback,
            completion_policy(),
            vault,
        )
        self.assertIs(type(replay), GoogleOidcGatewayFailure)
        self.assertEqual(replay.status, "invalid_or_expired_transaction")
        self.assertEqual(harness.transport.token_request_count, 1)
        self.assertFalse(database.connection.in_transaction)

    def test_matching_browser_binding_is_checked_after_terminal_commit_without_lock(self):
        database, authority, harness, prepared = self.prepare(
            "browser-binding-match"
        )
        callback = harness.transport.callback_for(prepared)
        binding = prepared.transaction_id
        vault = self.vault()
        observations = []
        original_compare = durable_gateway_module._constant_time_equal

        def observe_compare(claimed, supplied):
            observer = open_connection(database.path)
            try:
                observer.execute("BEGIN IMMEDIATE")
                observer.rollback()
            finally:
                observer.close()
            observations.append(
                (
                    database.connection.in_transaction,
                    transaction_rows(database.connection)[0]["lifecycle"],
                    claimed,
                    supplied,
                )
            )
            return original_compare(claimed, supplied)

        with mock.patch.object(
            durable_gateway_module,
            "_constant_time_equal",
            side_effect=observe_compare,
        ):
            result = complete_browser_bound_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
                callback,
                binding,
                completion_policy(),
                vault,
            )

        self.assertEqual(result.status, "issued")
        self.assertEqual(vault_entry_count(vault), 1)
        self.assertEqual(harness.transport.token_request_count, 1)
        self.assertEqual(
            observations,
            [
                (
                    False,
                    "consumed",
                    binding.encode("ascii"),
                    binding.encode("ascii"),
                )
            ],
        )
        self.assertFalse(database.connection.in_transaction)

    def test_invalid_browser_bindings_terminally_fail_before_downstream_work(self):
        cases = (
            ("missing", None),
            ("empty", ""),
            ("wrong-type", bytearray(b"not-a-string")),
            ("non-ascii", "oidctx_" + ("\N{LATIN SMALL LETTER E WITH ACUTE}" * 32)),
            ("invalid-shape", "oidctx_" + ("g" * 32)),
        )
        for name, supplied_binding in cases:
            with self.subTest(case=name):
                database, authority, harness, prepared = self.prepare(
                    f"browser-binding-{name}"
                )
                callback = harness.transport.callback_for(prepared)
                vault = self.vault()
                with (
                    mock.patch.object(
                        durable_gateway_module,
                        "_constant_time_equal",
                        wraps=durable_gateway_module._constant_time_equal,
                    ) as compared,
                    mock.patch.object(
                        durable_gateway_module,
                        "_complete_durable_google_oidc_claimed",
                    ) as downstream,
                ):
                    result = (
                        complete_browser_bound_durable_google_oidc_authorization(
                            database.connection,
                            harness.gateway,
                            authority,
                            callback,
                            supplied_binding,
                            completion_policy(),
                            vault,
                        )
                    )
                self.assertIs(type(result), GoogleOidcGatewayFailure)
                self.assertEqual(
                    result.status,
                    "invalid_or_expired_transaction",
                )
                self.assertEqual(compared.call_count, 1)
                downstream.assert_not_called()
                self.assertEqual(
                    transaction_rows(database.connection)[0]["lifecycle"],
                    "consumed",
                )
                self.assertEqual(harness.transport.token_request_count, 0)
                self.assertEqual(vault_entry_count(vault), 0)
                self.assertFalse(database.connection.in_transaction)

    def test_mismatched_browser_binding_clears_claimed_secrets_and_is_redacted(self):
        database, authority, harness, prepared = self.prepare(
            "browser-binding-secrecy"
        )
        callback = harness.transport.callback_for(prepared)
        claimed_transaction_id = prepared.transaction_id
        mismatched_binding = "oidctx_" + (
            "0" * 32
            if claimed_transaction_id != "oidctx_" + ("0" * 32)
            else "1" * 32
        )
        captured_secret_buffers = []
        original_take = durable_gateway_module._take_claimed_material

        def capture_claimed_material(capsule):
            values = original_take(capsule)
            captured_secret_buffers.extend(
                values[name]
                for name in (
                    "state",
                    "nonce",
                    "pkce_verifier",
                    "b2d1_request_key",
                )
            )
            return values

        with (
            mock.patch.object(
                durable_gateway_module,
                "_take_claimed_material",
                side_effect=capture_claimed_material,
            ),
            mock.patch.object(
                durable_gateway_module,
                "_complete_durable_google_oidc_claimed",
            ) as downstream,
        ):
            result = complete_browser_bound_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
                callback,
                mismatched_binding,
                completion_policy(),
                self.vault(),
            )

        self.assertEqual(result.status, "invalid_or_expired_transaction")
        downstream.assert_not_called()
        self.assertEqual(len(captured_secret_buffers), 4)
        self.assertTrue(
            all(buffer == bytearray() for buffer in captured_secret_buffers)
        )
        public_projection = repr(result) + str(result) + repr(result.as_dict())
        self.assertNotIn(claimed_transaction_id, public_projection)
        self.assertNotIn(mismatched_binding, public_projection)
        self.assertEqual(harness.transport.token_request_count, 0)
        self.assertEqual(
            transaction_rows(database.connection)[0]["lifecycle"],
            "consumed",
        )
        self.assertFalse(database.connection.in_transaction)

    def test_process_reconstruction_and_retained_key_rotation_complete(self):
        database = self.database("reconstruction")
        first_authority = self.keep(
            key_authority(
                lookup_versions=(1,),
                protection_versions=(11,),
            )
        )
        first_gateway = self.keep(
            make_real_gateway(subject=database.subject)
        )
        prepared = prepare_durable_google_oidc_authorization(
            database.connection,
            first_gateway.gateway,
            first_authority,
        )
        callback = first_gateway.transport.callback_for(prepared)
        first_gateway.gateway.close()
        first_authority.close()

        rotated_authority = self.keep(
            key_authority(
                lookup_versions=(1, 2),
                protection_versions=(11, 12),
                active_lookup_version=2,
                active_protection_version=12,
            )
        )
        reconstructed = self.keep(
            make_real_gateway(subject=database.subject)
        )
        vault = self.vault()
        result = complete_durable_google_oidc_authorization(
            database.connection,
            reconstructed.gateway,
            rotated_authority,
            callback,
            completion_policy(),
            vault,
        )
        self.assertEqual(result.status, "issued")
        self.assertEqual(transaction_rows(database.connection)[0]["lifecycle"], "consumed")
        self.assertEqual(first_gateway.transport.token_request_count, 1)

    def test_invitation_reconstructs_into_only_private_one_shot_completion(self):
        invitation_text = "inv_" + ("e" * 32) + "." + ("F" * 43)
        invitation_bytes = invitation_text.encode("ascii")
        database = self.database("invitation-reconstruction")
        first_authority = self.keep(
            key_authority(
                lookup_versions=(1,),
                protection_versions=(11,),
            )
        )
        first_gateway = self.keep(
            make_real_gateway(subject=database.subject)
        )
        source = bytearray(invitation_bytes)
        prepared = prepare_durable_google_oidc_authorization(
            database.connection,
            first_gateway.gateway,
            first_authority,
            invitation_credential=source,
        )
        self.assertEqual(source, bytearray())
        row = transaction_rows(database.connection)[0]
        self.assertNotIn(invitation_bytes, row["protected_material"])
        self.assertNotIn(invitation_text, repr(row))
        callback = first_gateway.transport.callback_for(prepared)
        first_gateway.gateway.close()
        first_authority.close()

        rotated_authority = self.keep(
            key_authority(
                lookup_versions=(1, 2),
                protection_versions=(11, 12),
                active_lookup_version=2,
                active_protection_version=12,
            )
        )
        reconstructed = self.keep(
            make_real_gateway(subject=database.subject)
        )
        observed = []
        retained = []
        original = (
            durable_gateway_module._complete_durable_google_oidc_claimed
        )

        def observe(*args, **kwargs):
            invitation = kwargs["invitation_credential"]
            observed.append(None if invitation is None else bytes(invitation))
            retained.append(invitation)
            return original(*args, **kwargs)

        with mock.patch.object(
            durable_gateway_module,
            "_complete_durable_google_oidc_claimed",
            side_effect=observe,
        ) as completion:
            result = complete_durable_google_oidc_authorization(
                database.connection,
                reconstructed.gateway,
                rotated_authority,
                callback,
                completion_policy(),
                self.vault(),
            )
            replay = complete_durable_google_oidc_authorization(
                database.connection,
                reconstructed.gateway,
                rotated_authority,
                callback,
                completion_policy(),
                self.vault(),
            )
        self.assertEqual(result.status, "issued")
        self.assertEqual(replay.status, "invalid_or_expired_transaction")
        self.assertEqual(observed, [invitation_bytes])
        self.assertEqual(completion.call_count, 1)
        self.assertEqual(retained, [bytearray()])
        self.assertEqual(
            transaction_rows(database.connection)[0]["lifecycle"],
            "consumed",
        )
        public_text = repr(result) + str(result) + repr(replay)
        self.assertNotIn(invitation_text, public_text)

        plain_database = self.database("invitation-free-handoff")
        plain_authority = self.keep(key_authority())
        plain_gateway = self.keep(
            make_real_gateway(subject=plain_database.subject)
        )
        plain_prepared = prepare_durable_google_oidc_authorization(
            plain_database.connection,
            plain_gateway.gateway,
            plain_authority,
        )
        plain_callback = plain_gateway.transport.callback_for(
            plain_prepared
        )
        plain_observed = []

        def observe_plain(*args, **kwargs):
            plain_observed.append(kwargs["invitation_credential"])
            return original(*args, **kwargs)

        with mock.patch.object(
            durable_gateway_module,
            "_complete_durable_google_oidc_claimed",
            side_effect=observe_plain,
        ):
            plain_result = complete_durable_google_oidc_authorization(
                plain_database.connection,
                plain_gateway.gateway,
                plain_authority,
                plain_callback,
                completion_policy(),
                self.vault(),
            )
        self.assertEqual(plain_result.status, "issued")
        self.assertEqual(plain_observed, [None])
        plain_prepared.close()

    def test_callback_query_cannot_add_or_replace_bound_invitation(self):
        invitation = bytearray(
            ("inv_" + ("1" * 32) + "." + ("G" * 43)).encode("ascii")
        )
        database, authority, harness, prepared = self.prepare(
            "invitation-callback-substitution",
            invitation_credential=invitation,
        )
        callback = harness.transport.callback_for(prepared)
        substituted = callback + "&invitation=attacker-controlled"
        with mock.patch.object(
            durable_gateway_module,
            "_complete_claimed_authorization",
        ) as downstream:
            result = complete_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
                substituted,
                completion_policy(),
                self.vault(),
            )
        self.assertEqual(result.status, "invalid_or_expired_transaction")
        downstream.assert_not_called()
        self.assertEqual(
            transaction_rows(database.connection)[0]["lifecycle"],
            "prepared",
        )

    def test_invited_identity_is_atomic_then_resolves_without_invitation(self):
        database = self.database("invited-first-login")
        subject = "google-subject-invited-first-login-new"
        email = "invited-first-login@example.test"
        invitation = self.invitation(database, "first-login", email)
        authority = self.keep(key_authority())
        harness = self.keep(
            make_real_gateway(
                subject=subject,
                invitation_lookup_key=bytearray(INVITATION_KEY),
            )
        )
        protected = bytearray(invitation.invitation_token.encode("ascii"))
        before = {
            table: database.connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
            for table in (
                "users",
                "auth_identities",
                "account_lifecycle_events",
                "account_sessions",
                "product_principals",
                "principal_account_bindings",
                "ownership_binding_events",
                "legacy_owner_aliases",
                "product_profiles",
            )
        }
        prepared = prepare_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            invitation_credential=protected,
        )
        self.assertEqual(protected, bytearray())
        self.assertNotIn(
            invitation.invitation_token,
            prepared.authorization_url,
        )
        callback = harness.transport.callback_for(
            prepared,
            claims_overrides={
                "email": email,
                "email_verified": True,
            },
        )
        first = complete_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            callback,
            completion_policy(),
            self.vault(),
        )
        self.assertEqual(first.status, "issued")
        identity = database.connection.execute(
            "SELECT auth_identity_id, user_id, verified_email, email_verified "
            "FROM auth_identities WHERE provider = 'google' "
            "AND provider_subject = ?",
            (subject,),
        ).fetchone()
        self.assertIsNotNone(identity)
        account_id = identity["user_id"]
        self.assertEqual(identity["verified_email"], email)
        self.assertEqual(identity["email_verified"], 1)
        invitation_row = database.connection.execute(
            "SELECT invitation_status, consumed_by_user_id "
            "FROM account_invitations WHERE invitation_id = ?",
            (invitation.invitation.invitation_id,),
        ).fetchone()
        self.assertEqual(tuple(invitation_row), ("consumed", account_id))
        ownership_after_first = {
            table: tuple(
                tuple(row)
                for row in database.connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY 1'
                )
            )
            for table in (
                "product_principals",
                "principal_account_bindings",
                "ownership_binding_events",
            )
        }
        self.assertTrue(
            all(len(rows) == 1 for rows in ownership_after_first.values())
        )

        later = prepare_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
        )
        later_callback = harness.transport.callback_for(
            later,
            code="later-invited-login",
            missing_claims=("email", "email_verified"),
        )
        second = complete_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            later_callback,
            completion_policy(),
            self.vault(),
        )
        replay = complete_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            callback,
            completion_policy(),
            self.vault(),
        )
        self.assertEqual(second.status, "issued")
        self.assertEqual(replay.status, "invalid_or_expired_transaction")
        self.assertEqual(
            database.connection.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0],
            before["users"] + 1,
        )
        self.assertEqual(
            database.connection.execute(
                "SELECT COUNT(*) FROM auth_identities"
            ).fetchone()[0],
            before["auth_identities"] + 1,
        )
        self.assertEqual(
            database.connection.execute(
                "SELECT COUNT(*) FROM account_lifecycle_events"
            ).fetchone()[0],
            before["account_lifecycle_events"] + 1,
        )
        self.assertEqual(
            database.connection.execute(
                "SELECT COUNT(*) FROM account_sessions WHERE user_id = ?",
                (account_id,),
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            {
                table: tuple(
                    tuple(row)
                    for row in database.connection.execute(
                        f'SELECT * FROM "{table}" ORDER BY 1'
                    )
                )
                for table in ownership_after_first
            },
            ownership_after_first,
        )
        for table in ("legacy_owner_aliases", "product_profiles"):
            self.assertEqual(
                database.connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0],
                before[table],
            )
        public = repr(first) + repr(second) + repr(replay)
        self.assertNotIn(invitation.invitation_token, public)
        prepared.close()
        later.close()

    def test_existing_identity_never_consumes_a_presented_invitation(self):
        database = self.database("existing-with-invitation")
        invitation = self.invitation(
            database,
            "existing-identity",
            "unused-existing-invitation@example.test",
        )
        authority = self.keep(key_authority())
        harness = self.keep(
            make_real_gateway(
                subject=database.subject,
                invitation_lookup_key=bytearray(INVITATION_KEY),
            )
        )
        prepared = prepare_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            invitation_credential=bytearray(
                invitation.invitation_token.encode("ascii")
            ),
        )
        callback = harness.transport.callback_for(
            prepared,
            missing_claims=("email", "email_verified"),
        )
        result = complete_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            callback,
            completion_policy(),
            self.vault(),
        )
        self.assertEqual(result.status, "issued")
        self.assertEqual(
            tuple(
                database.connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
                for table in (
                    "product_principals",
                    "principal_account_bindings",
                    "ownership_binding_events",
                )
            ),
            (1, 1, 1),
        )
        row = database.connection.execute(
            "SELECT invitation_status, consumed_at, consumed_by_user_id "
            "FROM account_invitations WHERE invitation_id = ?",
            (invitation.invitation.invitation_id,),
        ).fetchone()
        self.assertEqual(tuple(row), ("pending", None, None))
        prepared.close()

    def test_post_provision_failure_preserves_resolvable_identity(self):
        database = self.database("post-provision-completion-failure")
        subject = "google-subject-post-provision-failure"
        email = "post-provision-failure@example.test"
        invitation = self.invitation(database, "post-provision", email)
        authority = self.keep(key_authority())
        harness = self.keep(
            make_real_gateway(
                subject=subject,
                invitation_lookup_key=bytearray(INVITATION_KEY),
            )
        )
        prepared = prepare_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            invitation_credential=bytearray(
                invitation.invitation_token.encode("ascii")
            ),
        )
        callback = harness.transport.callback_for(
            prepared,
            claims_overrides={
                "email": email,
                "email_verified": True,
            },
        )
        with mock.patch.object(
            gateway_module,
            "complete_trusted_login",
            side_effect=RuntimeError("b23b_trusted_completion_failure"),
        ):
            failed = complete_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
                callback,
                completion_policy(),
                self.vault(),
            )
        self.assertEqual(failed.status, "unavailable")
        identity = database.connection.execute(
            "SELECT user_id FROM auth_identities WHERE provider = 'google' "
            "AND provider_subject = ?",
            (subject,),
        ).fetchone()
        self.assertIsNotNone(identity)
        account_id = identity["user_id"]
        self.assertEqual(
            database.connection.execute(
                "SELECT COUNT(*) FROM account_sessions WHERE user_id = ?",
                (account_id,),
            ).fetchone()[0],
            0,
        )
        ownership_after_failure = {
            table: tuple(
                tuple(row)
                for row in database.connection.execute(
                    f'SELECT * FROM "{table}" ORDER BY 1'
                )
            )
            for table in (
                "product_principals",
                "principal_account_bindings",
                "ownership_binding_events",
            )
        }
        self.assertTrue(
            all(len(rows) == 1 for rows in ownership_after_failure.values())
        )

        later = prepare_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
        )
        later_callback = harness.transport.callback_for(
            later,
            code="post-provision-later-login",
            missing_claims=("email", "email_verified"),
        )
        recovered = complete_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            later_callback,
            completion_policy(),
            self.vault(),
        )
        self.assertEqual(recovered.status, "issued")
        self.assertEqual(
            database.connection.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            database.connection.execute(
                "SELECT COUNT(*) FROM auth_identities WHERE provider = "
                "'google' AND provider_subject = ?",
                (subject,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            database.connection.execute(
                "SELECT COUNT(*) FROM account_sessions WHERE user_id = ?",
                (account_id,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            {
                table: tuple(
                    tuple(row)
                    for row in database.connection.execute(
                        f'SELECT * FROM "{table}" ORDER BY 1'
                    )
                )
                for table in ownership_after_failure
            },
            ownership_after_failure,
        )
        prepared.close()
        later.close()

    def test_bootstrap_failure_after_provisioning_blocks_session_then_recovers(self):
        database = self.database("ownership-failure-after-provision")
        subject = "google-subject-ownership-failure-after-provision-new"
        email = "ownership-failure-after-provision@example.test"
        invitation = self.invitation(database, "ownership-failure", email)
        authority = self.keep(key_authority())
        harness = self.keep(
            make_real_gateway(
                subject=subject,
                invitation_lookup_key=bytearray(INVITATION_KEY),
            )
        )
        prepared = prepare_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            invitation_credential=bytearray(
                invitation.invitation_token.encode("ascii")
            ),
        )
        callback = harness.transport.callback_for(
            prepared,
            claims_overrides={"email": email, "email_verified": True},
        )
        failed_vault = self.vault()
        with mock.patch.object(
            gateway_module,
            "_ensure_account_native_principal_for_login",
            side_effect=RuntimeError("injected_ownership_unavailable"),
        ):
            failed = complete_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
                callback,
                completion_policy(),
                failed_vault,
            )
        self.assertEqual(failed.status, "unavailable")
        identity = database.connection.execute(
            "SELECT user_id FROM auth_identities WHERE provider='google' "
            "AND provider_subject=?",
            (subject,),
        ).fetchone()
        self.assertIsNotNone(identity)
        account_id = identity["user_id"]
        self.assertEqual(
            tuple(
                database.connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
                for table in (
                    "product_principals",
                    "principal_account_bindings",
                    "ownership_binding_events",
                    "account_sessions",
                )
            ),
            (0, 0, 0, 0),
        )
        self.assertEqual(vault_entry_count(failed_vault), 0)
        self.assertEqual(
            tuple(
                database.connection.execute(
                    "SELECT invitation_status, consumed_by_user_id FROM "
                    "account_invitations WHERE invitation_id=?",
                    (invitation.invitation.invitation_id,),
                ).fetchone()
            ),
            ("consumed", account_id),
        )

        later = prepare_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
        )
        recovered_vault = self.vault()
        recovered = complete_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            harness.transport.callback_for(
                later,
                code="ownership-failure-recovery",
                missing_claims=("email", "email_verified"),
            ),
            completion_policy(),
            recovered_vault,
        )
        self.assertEqual(recovered.status, "issued")
        self.assertEqual(
            tuple(
                database.connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
                for table in (
                    "product_principals",
                    "principal_account_bindings",
                    "ownership_binding_events",
                    "account_sessions",
                )
            ),
            (1, 1, 1, 1),
        )
        self.assertEqual(vault_entry_count(recovered_vault), 1)
        prepared.close()
        later.close()

    def test_new_identity_failures_leave_account_state_unchanged(self):
        scenarios = (
            ("missing-invitation", None, "new@example.test", True),
            (
                "unknown-invitation",
                "inv_" + ("9" * 32) + "." + ("Z" * 43),
                "new@example.test",
                True,
            ),
            ("missing-email", "create", None, True),
            ("unverified-email", "create", "new@example.test", False),
            ("malformed-email", "create", "not-an-email", True),
            ("mismatched-email", "create", "other@example.test", True),
            ("expired-invitation", "expired", "new@example.test", True),
            ("revoked-invitation", "revoked", "new@example.test", True),
            ("consumed-invitation", "consumed", "new@example.test", True),
        )
        for name, invitation_mode, email, verified in scenarios:
            with self.subTest(name=name):
                database = self.database(f"invite-failure-{name}")
                invitation = (
                    None
                    if invitation_mode not in {
                        "create",
                        "expired",
                        "revoked",
                        "consumed",
                    }
                    else self.invitation(
                        database,
                        f"failure-{name}",
                        "new@example.test",
                        now=(
                            NOW - timedelta(days=2)
                            if invitation_mode == "expired"
                            else NOW
                        ),
                        expires_at=(
                            NOW - timedelta(days=1)
                            if invitation_mode == "expired"
                            else None
                        ),
                    )
                )
                expected_invitation_status = "pending"
                if invitation_mode == "revoked":
                    accounts.revoke_invitation(
                        database.connection,
                        invitation_id=(
                            invitation.invitation.invitation_id
                        ),
                        now=NOW,
                    )
                    expected_invitation_status = "revoked"
                elif invitation_mode == "consumed":
                    verifier = accounts.TrustedIdentityVerifier()
                    service = accounts.AccountService(verifier)
                    consumed_identity = (
                        verifier.from_validated_google_claims(
                            provider_subject=(
                                "google-subject-consumed-invitation-owner"
                            ),
                            verified_email="new@example.test",
                            email_verified=True,
                            authenticated_at=NOW,
                            metadata_version="google_oidc_v1",
                        )
                    )
                    service.create_invited_user(
                        database.connection,
                        identity=consumed_identity,
                        invitation_token=invitation.invitation_token,
                        invitation_lookup_key=INVITATION_KEY,
                        idempotency_key="b23b-consumed-invitation-owner",
                        now=NOW,
                    )
                    expected_invitation_status = "consumed"
                credential = (
                    invitation_mode
                    if invitation is None
                    else invitation.invitation_token
                )
                before = tuple(
                    database.connection.execute(
                        f'SELECT COUNT(*) FROM "{table}"'
                    ).fetchone()[0]
                    for table in (
                        "users",
                        "auth_identities",
                        "account_lifecycle_events",
                        "account_sessions",
                    )
                )
                authority = self.keep(key_authority())
                harness = self.keep(
                    make_real_gateway(
                        subject=f"google-subject-{name}-new",
                        invitation_lookup_key=bytearray(INVITATION_KEY),
                    )
                )
                prepared = prepare_durable_google_oidc_authorization(
                    database.connection,
                    harness.gateway,
                    authority,
                    invitation_credential=(
                        None
                        if credential is None
                        else bytearray(credential.encode("ascii"))
                    ),
                )
                claims = {"email_verified": verified}
                missing = ()
                if email is None:
                    missing = ("email",)
                else:
                    claims["email"] = email
                callback = harness.transport.callback_for(
                    prepared,
                    claims_overrides=claims,
                    missing_claims=missing,
                )
                result = complete_durable_google_oidc_authorization(
                    database.connection,
                    harness.gateway,
                    authority,
                    callback,
                    completion_policy(),
                    self.vault(),
                )
                self.assertEqual(result.status, "authentication_denied")
                after = tuple(
                    database.connection.execute(
                        f'SELECT COUNT(*) FROM "{table}"'
                    ).fetchone()[0]
                    for table in (
                        "users",
                        "auth_identities",
                        "account_lifecycle_events",
                        "account_sessions",
                    )
                )
                self.assertEqual(after, before)
                if invitation is not None:
                    self.assertEqual(
                        database.connection.execute(
                            "SELECT invitation_status FROM "
                            "account_invitations WHERE invitation_id = ?",
                            (invitation.invitation.invitation_id,),
                        ).fetchone()[0],
                        expected_invitation_status,
                    )
                prepared.close()

    def test_private_handoff_clears_invitation_after_downstream_failure(self):
        invitation = bytearray(
            ("inv_" + ("4" * 32) + "." + ("Q" * 43)).encode("ascii")
        )
        retained = invitation
        with mock.patch.object(
            durable_gateway_module,
            "_complete_durable_google_oidc_claimed",
            side_effect=RuntimeError("closed_downstream_failure"),
        ) as downstream:
            with self.assertRaisesRegex(
                RuntimeError,
                "^closed_downstream_failure$",
            ):
                durable_gateway_module._complete_claimed_authorization(
                    object(),
                    object(),
                    "callback",
                    object(),
                    object(),
                    state=bytearray(b"state"),
                    nonce=bytearray(b"nonce"),
                    pkce_verifier=bytearray(b"verifier"),
                    b2d1_request_key=bytearray(b"request"),
                    invitation_credential=invitation,
                    created_at=object(),
                    expires_at=object(),
                    claimed_at=object(),
                )
        self.assertEqual(retained, bytearray())
        self.assertEqual(downstream.call_count, 1)
        self.assertEqual(
            downstream.call_args.kwargs["invitation_credential"],
            bytearray(),
        )

    def test_configuration_boundary_accepts_maximum_redirect_and_reconstructs(self):
        prefix = "https://maximum-redirect.test/"
        redirect_uri = prefix + ("r" * (2048 - len(prefix)))
        self.assertEqual(len(redirect_uri), 2048)
        database = self.database("maximum-redirect")
        authority = self.keep(key_authority())
        first = self.keep(
            make_real_gateway(
                subject=database.subject,
                redirect_uri=redirect_uri,
            )
        )
        reconstructed = self.keep(
            make_real_gateway(
                subject=database.subject,
                redirect_uri=redirect_uri,
            )
        )
        first_context = gateway_module._durable_google_oidc_context(
            first.gateway
        )
        reconstructed_context = gateway_module._durable_google_oidc_context(
            reconstructed.gateway
        )
        self.assertEqual(first_context, reconstructed_context)
        self.assertLessEqual(
            len(first_context[2]),
            MAX_DURABLE_CONFIGURATION_CONTEXT_BYTES,
        )
        first_fingerprint = protection._configuration_fingerprint(
            authority,
            authority.active_lookup_version,
            first_context[2],
        )
        self.assertEqual(
            first_fingerprint,
            protection._configuration_fingerprint(
                authority,
                authority.active_lookup_version,
                reconstructed_context[2],
            ),
        )
        prepared = prepare_durable_google_oidc_authorization(
            database.connection,
            first.gateway,
            authority,
        )
        self.assertIs(type(prepared), PreparedDurableGoogleOidcAuthorization)
        self.assertEqual(
            authorization_parameters(prepared)["redirect_uri"],
            redirect_uri,
        )

        with self.assertRaisesRegex(
            TypeError,
            "^google_oidc_redirect_uri_invalid$",
        ):
            make_real_gateway(
                subject=database.subject,
                redirect_uri=redirect_uri + "r",
            )

    def test_cross_configuration_is_invalidated_before_provider_activity(self):
        database, authority, owner, prepared = self.prepare("cross-config")
        callback = owner.transport.callback_for(prepared)
        foreign = self.keep(
            make_real_gateway(
                subject=database.subject,
                client_secret=bytearray(
                    b"different-test-client-secret-material"
                ),
            )
        )
        result = complete_durable_google_oidc_authorization(
            database.connection,
            foreign.gateway,
            authority,
            callback,
            completion_policy(),
            self.vault(),
        )
        self.assertEqual(result.status, "invalid_or_expired_transaction")
        self.assertEqual(transaction_rows(database.connection)[0]["lifecycle"], "invalidated")
        self.assertEqual(owner.transport.token_request_count, 0)
        self.assertEqual(foreign.transport.token_request_count, 0)

    def test_expiry_equality_and_clock_rollback_terminalize_without_provider(self):
        for suffix, movement, lifecycle in (
            ("expiry-equality", 600, "expired"),
            ("clock-rollback", -1, "invalidated"),
        ):
            with self.subTest(case=suffix):
                database, authority, harness, prepared = self.prepare(suffix)
                callback = harness.transport.callback_for(prepared)
                harness.clock.advance_wall(movement)
                result = complete_durable_google_oidc_authorization(
                    database.connection,
                    harness.gateway,
                    authority,
                    callback,
                    completion_policy(),
                    self.vault(),
                )
                self.assertEqual(
                    result.status,
                    "invalid_or_expired_transaction",
                )
                self.assertEqual(
                    transaction_rows(database.connection)[0]["lifecycle"],
                    lifecycle,
                )
                self.assertEqual(harness.transport.token_request_count, 0)

    def test_provider_denial_is_terminal_and_replay_never_reaches_provider(self):
        database, authority, harness, prepared = self.prepare("denial")
        callback = harness.transport.callback_for(
            prepared,
            error="access_denied",
        )
        vault = self.vault()
        result = complete_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            callback,
            completion_policy(),
            vault,
        )
        self.assertEqual(result.status, "authentication_denied")
        self.assertEqual(transaction_rows(database.connection)[0]["lifecycle"], "consumed")
        self.assertEqual(harness.transport.token_request_count, 0)
        replay = complete_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            callback,
            completion_policy(),
            vault,
        )
        self.assertEqual(replay.status, "invalid_or_expired_transaction")
        self.assertEqual(harness.transport.token_request_count, 0)

    def test_malformed_callback_fails_before_claim_and_preserves_prepared_row(self):
        database, authority, harness, _prepared = self.prepare("malformed")
        result = complete_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            "https://accounts-d.test.invalid/callback?code=x",
            completion_policy(),
            self.vault(),
        )
        self.assertEqual(result.status, "invalid_or_expired_transaction")
        self.assertEqual(transaction_rows(database.connection)[0]["lifecycle"], "prepared")
        self.assertEqual(harness.transport.token_request_count, 0)

    def test_provider_wait_holds_no_sqlite_write_transaction_or_lock(self):
        database, authority, harness, prepared = self.prepare(
            "no-write-lock",
            block=True,
        )
        callback = harness.transport.callback_for(prepared)
        vault = self.vault()
        worker_connection = sqlite3.connect(
            database.path,
            timeout=2.0,
            check_same_thread=False,
        )
        worker_connection.row_factory = sqlite3.Row
        worker_connection.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(worker_connection.close)
        outcome = []

        def complete():
            outcome.append(
                complete_durable_google_oidc_authorization(
                    worker_connection,
                    harness.gateway,
                    authority,
                    callback,
                    completion_policy(),
                    vault,
                )
            )

        thread = threading.Thread(target=complete)
        thread.start()
        self.addCleanup(lambda: thread.join(timeout=5))
        self.assertTrue(harness.transport.entered.wait(timeout=5))
        observer = open_connection(database.path)
        try:
            observer.execute("BEGIN IMMEDIATE")
            observer.rollback()
        finally:
            observer.close()
        harness.transport.release.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome[0].status, "issued")

    def test_identity_resolution_and_b2d1_begin_with_connection_idle(self):
        database, authority, harness, prepared = self.prepare(
            "idle-composition"
        )
        callback = harness.transport.callback_for(prepared)
        vault = self.vault()
        original_resolve = gateway_module._resolve_durable_identity
        original_bootstrap = (
            gateway_module._ensure_account_native_principal_for_login
        )
        original_complete = gateway_module.complete_trusted_login
        observations = []

        def resolve(connection, identity, now):
            observations.append(("identity", connection.in_transaction))
            return original_resolve(connection, identity, now)

        def bootstrap(connection, *args, **kwargs):
            observations.append(("ownership", connection.in_transaction))
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM account_sessions"
                ).fetchone()[0],
                0,
            )
            return original_bootstrap(connection, *args, **kwargs)

        def complete(connection, *args, **kwargs):
            observations.append(("b2d1", connection.in_transaction))
            self.assertEqual(
                tuple(
                    connection.execute(
                        f'SELECT COUNT(*) FROM "{table}"'
                    ).fetchone()[0]
                    for table in (
                        "product_principals",
                        "principal_account_bindings",
                        "ownership_binding_events",
                    )
                ),
                (1, 1, 1),
            )
            return original_complete(connection, *args, **kwargs)

        with (
            mock.patch.object(
                gateway_module,
                "_resolve_durable_identity",
                side_effect=resolve,
            ),
            mock.patch.object(
                gateway_module,
                "_ensure_account_native_principal_for_login",
                side_effect=bootstrap,
            ),
            mock.patch.object(
                gateway_module,
                "complete_trusted_login",
                side_effect=complete,
            ),
        ):
            result = complete_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
                callback,
                completion_policy(),
                vault,
            )
        self.assertEqual(result.status, "issued")
        self.assertEqual(
            observations,
            [
                ("identity", False),
                ("ownership", False),
                ("b2d1", False),
            ],
        )

    def test_missing_account_native_bootstrap_denies_before_session(self):
        database = self.database("missing-account-native-bootstrap")
        authority = self.keep(key_authority())
        harness = self.keep(
            make_real_gateway(
                subject=database.subject,
                configure_account_native_bootstrap=False,
            )
        )
        prepared = prepare_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
        )
        vault = self.vault()
        result = complete_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            harness.transport.callback_for(prepared),
            completion_policy(),
            vault,
        )
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(vault_entry_count(vault), 0)
        self.assertEqual(
            tuple(
                database.connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
                for table in (
                    "product_principals",
                    "principal_account_bindings",
                    "ownership_binding_events",
                    "account_sessions",
                )
            ),
            (0, 0, 0, 0),
        )
        prepared.close()

    def test_two_login_transactions_converge_before_independent_sessions(self):
        database = self.database("two-login-ownership-convergence")
        authority = self.keep(key_authority())
        harnesses = tuple(
            self.keep(
                make_real_gateway(
                    subject=database.subject,
                )
            )
            for _index in range(2)
        )
        prepared = tuple(
            prepare_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
            )
            for harness in harnesses
        )
        callbacks = tuple(
            harness.transport.callback_for(
                transaction,
                code=f"b24b-concurrent-{index}",
            )
            for index, (harness, transaction) in enumerate(
                zip(harnesses, prepared)
            )
        )
        original_bootstrap = (
            gateway_module._ensure_account_native_principal_for_login
        )
        start = threading.Barrier(2)
        bootstrap_results = []
        outcomes = [None, None]
        failures = [None, None]

        def synchronized_bootstrap(*args, **kwargs):
            start.wait(timeout=5)
            result = original_bootstrap(*args, **kwargs)
            bootstrap_results.append(result)
            return result

        def worker(index):
            connection = open_connection(database.path)
            vault = request_secret_vault()
            try:
                outcomes[index] = complete_durable_google_oidc_authorization(
                    connection,
                    harnesses[index].gateway,
                    authority,
                    callbacks[index],
                    completion_policy(),
                    vault,
                )
                self.assertEqual(vault_entry_count(vault), 1)
            except BaseException as exc:
                failures[index] = exc
            finally:
                close_secret_vault(vault)
                connection.close()

        with mock.patch.object(
            gateway_module,
            "_ensure_account_native_principal_for_login",
            side_effect=synchronized_bootstrap,
        ):
            threads = tuple(
                threading.Thread(target=worker, args=(index,))
                for index in range(2)
            )
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
            self.assertTrue(all(not thread.is_alive() for thread in threads))

        self.assertEqual(failures, [None, None])
        self.assertEqual([result.status for result in outcomes], ["issued", "issued"])
        self.assertEqual({result.created for result in bootstrap_results}, {False, True})
        self.assertEqual(
            len({result.principal_id for result in bootstrap_results}),
            1,
        )
        self.assertEqual(
            len({result.binding_id for result in bootstrap_results}),
            1,
        )
        self.assertEqual(
            len({result.initial_event_id for result in bootstrap_results}),
            1,
        )
        self.assertEqual(
            tuple(
                database.connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
                for table in (
                    "product_principals",
                    "principal_account_bindings",
                    "ownership_binding_events",
                    "account_sessions",
                )
            ),
            (1, 1, 1, 2),
        )
        for transaction in prepared:
            transaction.close()

    def test_control_flow_consumes_row_clears_gateway_and_propagates_exactly(self):
        database, authority, harness, prepared = self.prepare(
            "control-flow",
            outcomes=("keyboard_interrupt",),
        )
        callback = harness.transport.callback_for(prepared)
        with self.assertRaises(KeyboardInterrupt):
            complete_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
                callback,
                completion_policy(),
                self.vault(),
            )
        self.assertEqual(transaction_rows(database.connection)[0]["lifecycle"], "consumed")
        self.assertEqual(harness.transport.token_request_count, 1)
        self.assertEqual(
            prepare_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
            ).status,
            "unavailable",
        )


if __name__ == "__main__":
    unittest.main()
