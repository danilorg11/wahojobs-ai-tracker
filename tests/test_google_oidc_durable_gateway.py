from __future__ import annotations

import inspect
import sqlite3
import threading
import unittest
from unittest import mock

from tests.google_oidc_authorization_transactions_test_support import (
    authorization_parameters,
    close_secret_vault,
    completion_policy,
    durable_transaction_database,
    key_authority,
    make_real_gateway,
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

    def prepare(self, suffix, **gateway_options):
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
            ("connection", "gateway", "key_authority"),
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
        original_complete = gateway_module.complete_trusted_login
        observations = []

        def resolve(connection, identity, now):
            observations.append(("identity", connection.in_transaction))
            return original_resolve(connection, identity, now)

        def complete(connection, *args, **kwargs):
            observations.append(("b2d1", connection.in_transaction))
            return original_complete(connection, *args, **kwargs)

        with (
            mock.patch.object(
                gateway_module,
                "_resolve_durable_identity",
                side_effect=resolve,
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
            [("identity", False), ("b2d1", False)],
        )

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
