from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import inspect
import json
import logging
import sqlite3
import threading
import unittest
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qsl, urlencode, urlsplit

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
from tests.google_oidc_gateway_test_support import REDIRECT_URI
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
        telemetry_sink=None,
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
        if telemetry_sink is not None:
            gateway_module._configure_callback_failure_telemetry(
                harness.gateway,
                telemetry_sink,
            )
        prepared = prepare_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            invitation_credential=invitation_credential,
        )
        self.assertIs(type(prepared), PreparedDurableGoogleOidcAuthorization)
        return database, authority, harness, prepared

    @staticmethod
    def callback_url(*fields):
        return REDIRECT_URI + "?" + urlencode(fields)

    @staticmethod
    def mutation_snapshot(connection):
        transaction = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT lifecycle, row_version FROM "
                "google_oidc_authorization_transactions "
                "ORDER BY transaction_id"
            )
        )
        counts = tuple(
            (
                table,
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0],
            )
            for table in (
                "account_invitations",
                "users",
                "auth_identities",
                "product_principals",
                "principal_account_bindings",
                "account_sessions",
                "product_profiles",
            )
        )
        return transaction, counts

    def assert_callback_failure_event(self, events, expected_stage):
        self.assertEqual(len(events), 1)
        encoded = events[0]
        self.assertIs(type(encoded), str)
        self.assertLessEqual(
            len(encoded.encode("ascii")),
            gateway_module._GOOGLE_CALLBACK_FAILURE_EVENT_MAX_BYTES,
        )
        self.assertEqual(
            json.loads(encoded),
            {
                "frame": "google_callback_failure_stage_v1",
                "stage": expected_stage,
                "public_status": "authentication_denied",
            },
        )

    def test_callback_failure_stage_contract_is_closed_bounded_and_private(self):
        expected_stages = {
            "provider_authorization_error",
            "token_exchange_oauth_rejected",
            "token_response_rejected",
            "id_token_key_or_signature_rejected",
            "id_token_claims_rejected",
            "verified_email_rejected",
            "invitation_email_agreement_rejected",
            "account_completion_rejected",
        }
        stage_type = gateway_module._GoogleCallbackFailureStageV1
        self.assertEqual({stage.value for stage in stage_type}, expected_stages)
        self.assertEqual(
            set(gateway_module._GOOGLE_CALLBACK_FAILURE_EVENTS_V1),
            set(stage_type),
        )
        self.assertEqual(
            gateway_module._GOOGLE_CALLBACK_FAILURE_MAX_JSON_BYTES_V1,
            130,
        )
        self.assertEqual(
            gateway_module._GOOGLE_CALLBACK_FAILURE_MAX_LINE_BYTES_V1,
            131,
        )
        self.assertEqual(
            gateway_module._GOOGLE_CALLBACK_FAILURE_LINES_V1,
            frozenset(
                gateway_module._GOOGLE_CALLBACK_FAILURE_EVENTS_V1.values()
            ),
        )
        with self.assertRaises(TypeError):
            gateway_module._GOOGLE_CALLBACK_FAILURE_EVENTS_V1[
                next(iter(stage_type))
            ] = "mutable"
        for stage, encoded in (
            gateway_module._GOOGLE_CALLBACK_FAILURE_EVENTS_V1.items()
        ):
            with self.subTest(stage=stage.value):
                self.assertEqual(
                    json.loads(encoded),
                    {
                        "frame": "google_callback_failure_stage_v1",
                        "stage": stage.value,
                        "public_status": "authentication_denied",
                    },
                )
                self.assertLessEqual(
                    len(encoded.encode("ascii")),
                    gateway_module._GOOGLE_CALLBACK_FAILURE_EVENT_MAX_BYTES,
                )
        self.assertNotIn(
            "_GoogleCallbackFailureStageV1",
            gateway_module.__all__,
        )
        public = gateway_module._failure("authentication_denied")
        self.assertEqual(public.as_dict(), {"status": "authentication_denied"})
        self.assertFalse(expected_stages.intersection(repr(public).split()))

    def test_fixed_sink_boundary_excludes_every_callback_secret_class(self):
        class Capture:
            __slots__ = ("calls",)

            def __init__(self):
                self.calls = []

            def __call__(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        secret_sentinels = {
            "state": "SENTINEL_CALLBACK_STATE_VALUE",
            "authorization_code": "SENTINEL_AUTHORIZATION_CODE_VALUE",
            "callback_extension": "SENTINEL_CALLBACK_EXTENSION_VALUE",
            "cookie": "SENTINEL_COOKIE_VALUE",
            "pkce": "SENTINEL_PKCE_VALUE",
            "nonce": "SENTINEL_NONCE_VALUE",
            "oauth_error": "SENTINEL_OAUTH_ERROR_VALUE",
            "oauth_description": "SENTINEL_OAUTH_DESCRIPTION_VALUE",
            "token_response": "SENTINEL_TOKEN_RESPONSE_VALUE",
            "access_token": "SENTINEL_ACCESS_TOKEN_VALUE",
            "id_token": "SENTINEL_ID_TOKEN_VALUE",
            "id_token_claim": "SENTINEL_ID_TOKEN_CLAIM_VALUE",
            "verified_email": "sentinel-email@example.test",
            "invitation_credential": "SENTINEL_INVITATION_CREDENTIAL_VALUE",
            "invitation_reference": "SENTINEL_INVITATION_REFERENCE_VALUE",
            "lookup_key": "SENTINEL_INVITATION_LOOKUP_KEY_VALUE",
            "identity_subject": "SENTINEL_IDENTITY_SUBJECT_VALUE",
            "account_identifier": "SENTINEL_ACCOUNT_IDENTIFIER_VALUE",
            "principal_identifier": "SENTINEL_PRINCIPAL_IDENTIFIER_VALUE",
            "session_identifier": "SENTINEL_SESSION_IDENTIFIER_VALUE",
            "sqlite_message": "SENTINEL_SQLITE_EXCEPTION_MESSAGE",
        }
        capture = Capture()
        for stage in gateway_module._GoogleCallbackFailureStageV1:
            self.assertTrue(
                gateway_module._emit_google_callback_failure_stage_v1(
                    stage,
                    capture,
                )
            )

        self.assertEqual(
            len(capture.calls),
            len(gateway_module._GoogleCallbackFailureStageV1),
        )
        for args, kwargs in capture.calls:
            self.assertEqual(len(args), 1)
            self.assertEqual(kwargs, {})
            self.assertIn(
                args[0],
                gateway_module._GOOGLE_CALLBACK_FAILURE_LINES_V1,
            )
        retained = (
            repr(capture.calls)
            + repr(tuple(gateway_module._GoogleCallbackFailureStageV1))
            + repr(gateway_module._failure("authentication_denied"))
        )
        for category, sentinel in secret_sentinels.items():
            with self.subTest(category=category):
                self.assertNotIn(sentinel, retained)

        before = list(capture.calls)
        self.assertFalse(
            gateway_module._emit_google_callback_failure_stage_v1(
                "provider_authorization_error",
                capture,
            )
        )
        self.assertFalse(
            gateway_module._emit_google_callback_failure_stage_v1(
                object(),
                capture,
            )
        )
        self.assertEqual(capture.calls, before)

    def test_provider_token_and_id_token_denials_emit_one_exact_stage(self):
        cases = (
            "provider_authorization_error",
            "token_exchange_oauth_rejected",
            "token_response_rejected",
            "id_token_key_or_signature_rejected",
            "id_token_claims_rejected",
        )
        for expected_stage in cases:
            with self.subTest(stage=expected_stage):
                gateway_options = {}
                events = []
                if expected_stage == "token_exchange_oauth_rejected":
                    gateway_options["outcomes"] = ("authentication_denied",)
                database, authority, harness, prepared = self.prepare(
                    f"telemetry-{expected_stage}",
                    telemetry_sink=events.append,
                    **gateway_options,
                )
                if expected_stage == "provider_authorization_error":
                    callback = harness.transport.callback_for(
                        prepared,
                        error="access_denied",
                    )
                elif expected_stage == "token_response_rejected":
                    harness.transport.queue_token_response(
                        document={"error": "synthetic-token-response-rejection"},
                        status=200,
                    )
                    callback = harness.transport.callback_for(prepared)
                elif expected_stage == "id_token_key_or_signature_rejected":
                    callback = harness.transport.callback_for(
                        prepared,
                        raw_id_token="synthetic-malformed-id-token",
                    )
                elif expected_stage == "id_token_claims_rejected":
                    callback = harness.transport.callback_for(
                        prepared,
                        claims_overrides={"aud": "synthetic-wrong-audience"},
                    )
                else:
                    callback = harness.transport.callback_for(prepared)
                result = complete_durable_google_oidc_authorization(
                    database.connection,
                    harness.gateway,
                    authority,
                    callback,
                    completion_policy(),
                    self.vault(),
                )
                self.assertEqual(result.status, "authentication_denied")
                self.assertEqual(
                    result.as_dict(),
                    {"status": "authentication_denied"},
                )
                self.assert_callback_failure_event(events, expected_stage)
                self.assertNotIn(
                    "synthetic-",
                    events[0],
                )
                prepared.close()

    def test_trusted_account_completion_denial_emits_account_stage(self):
        events = []
        database, authority, harness, prepared = self.prepare(
            "telemetry-account-completion",
            telemetry_sink=events.append,
        )
        callback = harness.transport.callback_for(prepared)
        denied = gateway_module._failure("authentication_denied")
        with mock.patch.object(
            gateway_module,
            "complete_trusted_login",
            return_value=denied,
        ) as complete_login:
            result = complete_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
                callback,
                completion_policy(),
                self.vault(),
            )
        self.assertIs(result, denied)
        complete_login.assert_called_once()
        self.assert_callback_failure_event(
            events,
            "account_completion_rejected",
        )
        prepared.close()

    def test_infrastructure_failures_emit_no_authentication_denial_stage(self):
        cases = (
            ("token-transport", {"outcomes": ("provider_unavailable",)}, None),
            ("jwks-document", {}, {"document": {"keys": []}}),
        )
        for name, gateway_options, jwks_plan in cases:
            with self.subTest(case=name):
                events = []
                database, authority, harness, prepared = self.prepare(
                    f"telemetry-infrastructure-{name}",
                    telemetry_sink=events.append,
                    **gateway_options,
                )
                if jwks_plan is not None:
                    harness.transport.queue_jwks_response(**jwks_plan)
                callback = harness.transport.callback_for(prepared)
                result = complete_durable_google_oidc_authorization(
                    database.connection,
                    harness.gateway,
                    authority,
                    callback,
                    completion_policy(),
                    self.vault(),
                )
                self.assertEqual(result.status, "provider_unavailable")
                self.assertEqual(events, [])
                self.assertEqual(harness.transport.token_request_count, 1)
                self.assertEqual(
                    harness.transport.jwks_request_count,
                    int(jwks_plan is not None),
                )
                prepared.close()

    def test_telemetry_sink_failure_does_not_change_public_denial(self):
        calls = []

        def failing_sink(line):
            calls.append(line)
            raise RuntimeError("synthetic-telemetry-sink-failure")

        database, authority, harness, prepared = self.prepare(
            "telemetry-sink-failure",
            telemetry_sink=failing_sink,
        )
        callback = harness.transport.callback_for(
            prepared,
            error="synthetic-provider-denial",
        )
        result = complete_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            callback,
            completion_policy(),
            self.vault(),
        )
        self.assertEqual(result.as_dict(), {"status": "authentication_denied"})
        self.assertEqual(len(calls), 1)
        prepared.close()

    def test_telemetry_sink_baseexception_is_silent_and_does_not_change_denial(self):
        class SinkBaseFailure(BaseException):
            pass

        calls = []

        def failing_sink(line):
            calls.append(line)
            raise SinkBaseFailure("PRIVATE_SINK_BASEEXCEPTION_MESSAGE")

        database, authority, harness, prepared = self.prepare(
            "telemetry-sink-baseexception",
            telemetry_sink=failing_sink,
        )
        callback = harness.transport.callback_for(
            prepared,
            error="synthetic-provider-denial",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = complete_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
                callback,
                completion_policy(),
                self.vault(),
            )
        self.assertEqual(result.as_dict(), {"status": "authentication_denied"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            transaction_rows(database.connection)[0]["lifecycle"],
            "consumed",
        )
        prepared.close()

    def test_telemetry_sink_hostile_baseexception_attribute_hook_cannot_escape(self):
        class SecondaryControl(BaseException):
            pass

        class HostileControl(BaseException):
            attribute_hook_calls = {
                "__traceback__": 0,
                "__cause__": 0,
                "__context__": 0,
            }

            def __setattr__(self, name, value):
                if name in self.attribute_hook_calls:
                    self.attribute_hook_calls[name] += 1
                    raise SecondaryControl()
                return super().__setattr__(name, value)

        sink_calls = []

        def hostile_sink(line):
            sink_calls.append(line)
            raise HostileControl("PRIVATE_HOSTILE_SINK_MESSAGE")

        database, authority, harness, prepared = self.prepare(
            "telemetry-sink-hostile-baseexception",
            telemetry_sink=hostile_sink,
        )
        callback = harness.transport.callback_for(
            prepared,
            error="synthetic-provider-denial",
        )
        emitter_results = []
        original_emitter = (
            gateway_module._emit_google_callback_failure_stage_v1
        )

        def observed_emitter(stage, sink=None):
            result = original_emitter(stage, sink)
            emitter_results.append(result)
            return result

        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        stdout = io.StringIO()
        stderr = io.StringIO()
        root = logging.getLogger()
        handlers = (Capture(), Capture())
        for handler in handlers:
            root.addHandler(handler)
        try:
            with (
                mock.patch.object(
                    gateway_module,
                    "_emit_google_callback_failure_stage_v1",
                    side_effect=observed_emitter,
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                result = complete_durable_google_oidc_authorization(
                    database.connection,
                    harness.gateway,
                    authority,
                    callback,
                    completion_policy(),
                    self.vault(),
                )
        finally:
            for handler in handlers:
                root.removeHandler(handler)

        self.assertEqual(result.as_dict(), {"status": "authentication_denied"})
        self.assertEqual(emitter_results, [False])
        self.assertEqual(len(sink_calls), 1)
        self.assertIn(
            sink_calls[0],
            gateway_module._GOOGLE_CALLBACK_FAILURE_LINES_V1,
        )
        self.assertEqual(
            HostileControl.attribute_hook_calls,
            {
                "__traceback__": 0,
                "__cause__": 0,
                "__context__": 0,
            },
        )
        self.assertEqual(records, [])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        transaction = transaction_rows(database.connection)[0]
        self.assertEqual(
            (transaction["lifecycle"], transaction["row_version"]),
            ("consumed", 2),
        )
        self.assertEqual(harness.transport.token_request_count, 0)
        self.assertEqual(harness.transport.jwks_request_count, 0)
        prepared.close()

    def test_production_writer_write_and_flush_failures_do_not_change_denial(self):
        from scripts.durable_google_login_app import (
            _GoogleCallbackFailureStderrWriter,
        )

        class Stream:
            def __init__(self, failed_operation):
                self.failed_operation = failed_operation
                self.write_calls = 0
                self.flush_calls = 0

            def write(self, _line):
                self.write_calls += 1
                if self.failed_operation == "write":
                    raise RuntimeError("PRIVATE_TELEMETRY_WRITE_FAILURE")

            def flush(self):
                self.flush_calls += 1
                if self.failed_operation == "flush":
                    raise RuntimeError("PRIVATE_TELEMETRY_FLUSH_FAILURE")

        for failed_operation in ("write", "flush"):
            with self.subTest(failed_operation=failed_operation):
                stream = Stream(failed_operation)
                writer = _GoogleCallbackFailureStderrWriter(stream)
                database, authority, harness, prepared = self.prepare(
                    f"telemetry-writer-{failed_operation}",
                    telemetry_sink=writer,
                )
                callback = harness.transport.callback_for(
                    prepared,
                    error="synthetic-provider-denial",
                )
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    result = complete_durable_google_oidc_authorization(
                        database.connection,
                        harness.gateway,
                        authority,
                        callback,
                        completion_policy(),
                        self.vault(),
                    )
                self.assertEqual(
                    result.as_dict(),
                    {"status": "authentication_denied"},
                )
                self.assertEqual(stream.write_calls, 1)
                self.assertEqual(
                    stream.flush_calls,
                    int(failed_operation == "flush"),
                )
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(
                    transaction_rows(database.connection)[0]["lifecycle"],
                    "consumed",
                )
                prepared.close()

    def test_private_account_write_rejection_emits_account_stage(self):
        events = []
        database = self.database("telemetry-account-write")
        invitation = self.invitation(
            database,
            "telemetry-account-write",
            "synthetic-account-write@example.test",
        )
        authority = self.keep(key_authority())
        harness = self.keep(
            make_real_gateway(
                subject="synthetic-account-write-subject",
                invitation_lookup_key=bytearray(INVITATION_KEY),
            )
        )
        gateway_module._configure_callback_failure_telemetry(
            harness.gateway,
            events.append,
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
                "email": "synthetic-account-write@example.test",
                "email_verified": True,
            },
        )
        before = self.mutation_snapshot(database.connection)
        with mock.patch.object(
            accounts.AccountService,
            "_create_invited_user_for_google_oidc",
            return_value=(
                accounts._InvitedUserDenialReason
                .ACCOUNT_COMPLETION_REJECTED
            ),
        ) as create_user:
            result = complete_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
                callback,
                completion_policy(),
                self.vault(),
            )
        self.assertEqual(result.status, "authentication_denied")
        create_user.assert_called_once()
        self.assert_callback_failure_event(
            events,
            "account_completion_rejected",
        )
        after_transaction, after_counts = self.mutation_snapshot(
            database.connection
        )
        self.assertEqual(after_transaction, (("consumed", 2),))
        self.assertEqual(after_counts, before[1])
        prepared.close()

    def assert_preclaim_rejection(self, suffix, callback_factory):
        events = []
        database, authority, harness, prepared = self.prepare(
            suffix,
            telemetry_sink=events.append,
        )
        state = authorization_parameters(prepared)["state"]
        before = self.mutation_snapshot(database.connection)
        callback = callback_factory(harness, prepared, state)
        with (
            mock.patch.object(
                durable_gateway_module,
                "claim_google_oidc_authorization_transaction",
                wraps=(
                    durable_gateway_module
                    .claim_google_oidc_authorization_transaction
                ),
            ) as claim,
            mock.patch.object(
                durable_gateway_module,
                "_complete_claimed_authorization",
                wraps=durable_gateway_module._complete_claimed_authorization,
            ) as downstream,
        ):
            result = complete_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
                callback,
                completion_policy(),
                self.vault(),
            )
        self.assertEqual(result.status, "invalid_or_expired_transaction")
        claim.assert_not_called()
        downstream.assert_not_called()
        self.assertEqual(self.mutation_snapshot(database.connection), before)
        self.assertEqual(harness.transport.token_request_count, 0)
        self.assertEqual(harness.transport.jwks_request_count, 0)
        self.assertEqual(events, [])
        self.assertFalse(database.connection.in_transaction)

    def assert_success_extension_reaches_claim_and_is_discarded(
        self,
        suffix,
        name,
        value,
    ):
        database, authority, harness, prepared = self.prepare(suffix)
        state = authorization_parameters(prepared)["state"]
        callback = harness.transport.callback_for(
            prepared,
            code="bounded-extension-success-code",
            extra_pairs=((name, value),),
        )
        with (
            mock.patch.object(
                durable_gateway_module,
                "claim_google_oidc_authorization_transaction",
                wraps=(
                    durable_gateway_module
                    .claim_google_oidc_authorization_transaction
                ),
            ) as claim,
            mock.patch.object(
                durable_gateway_module,
                "_complete_claimed_authorization",
                wraps=durable_gateway_module._complete_claimed_authorization,
            ) as downstream,
        ):
            result = complete_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
                callback,
                completion_policy(),
                self.vault(),
            )
        self.assertEqual(result.status, "issued")
        claim.assert_called_once()
        self.assertEqual(claim.call_args.args[3], state)
        downstream.assert_called_once()
        authoritative_callback = downstream.call_args.args[2]
        authoritative_names = {
            field
            for field, _item in parse_qsl(
                urlsplit(authoritative_callback).query,
                keep_blank_values=True,
                strict_parsing=True,
            )
        }
        self.assertEqual(
            authoritative_names,
            {"code", "iss", "state"},
        )
        self.assertNotIn(name, authoritative_names)
        self.assertNotIn(value, authoritative_callback)
        self.assertNotIn(
            value,
            "\n".join(database.connection.iterdump()),
        )
        self.assertEqual(harness.transport.token_request_count, 1)
        self.assertEqual(harness.transport.jwks_request_count, 1)
        prepared.close()

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

    def test_callback_invitation_extension_is_ignored_without_replacing_bound_invitation(self):
        invitation = bytearray(
            ("inv_" + ("1" * 32) + "." + ("G" * 43)).encode("ascii")
        )
        bound_invitation = bytes(invitation)
        database, authority, harness, prepared = self.prepare(
            "invitation-callback-substitution",
            invitation_credential=invitation,
        )
        callback = harness.transport.callback_for(prepared)
        extension_value = "synthetic-untrusted-invitation-extension"
        substituted = callback + "&" + urlencode(
            (("invitation", extension_value),)
        )
        observed = {}

        def observe(*args, **kwargs):
            observed["callback_url"] = args[2]
            observed["invitation_credential"] = bytes(
                kwargs["invitation_credential"]
            )
            return gateway_module._failure("authentication_denied")

        with (
            mock.patch.object(
                durable_gateway_module,
                "claim_google_oidc_authorization_transaction",
                wraps=(
                    durable_gateway_module
                    .claim_google_oidc_authorization_transaction
                ),
            ) as claim,
            mock.patch.object(
                durable_gateway_module,
                "_complete_claimed_authorization",
                side_effect=observe,
            ) as downstream,
        ):
            result = complete_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
                substituted,
                completion_policy(),
                self.vault(),
            )
        self.assertEqual(result.status, "authentication_denied")
        claim.assert_called_once()
        downstream.assert_called_once()
        self.assertEqual(
            observed["invitation_credential"],
            bound_invitation,
        )
        self.assertEqual(
            {
                name
                for name, _value in parse_qsl(
                    urlsplit(observed["callback_url"]).query,
                    keep_blank_values=True,
                    strict_parsing=True,
                )
            },
            {"code", "iss", "state"},
        )
        self.assertNotIn(extension_value, observed["callback_url"])
        self.assertNotIn(
            extension_value,
            "\n".join(database.connection.iterdump()),
        )
        self.assertEqual(
            transaction_rows(database.connection)[0]["lifecycle"],
            "consumed",
        )

    def test_invited_identity_extensions_are_discarded_then_reuses_without_invitation(self):
        events = []
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
        gateway_module._configure_callback_failure_telemetry(
            harness.gateway,
            events.append,
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
        real_google_extensions = (
            ("authuser", "synthetic-real-shape-authuser"),
            ("prompt", "synthetic-real-shape-prompt"),
            ("scope", "synthetic-real-shape-scope"),
        )
        callback = harness.transport.callback_for(
            prepared,
            code="real-google-shape-invited-code",
            extra_pairs=real_google_extensions,
            claims_overrides={
                "email": email,
                "email_verified": True,
            },
        )
        original_validate = gateway_module._validated_code_id_token
        with (
            mock.patch.object(
                durable_gateway_module,
                "claim_google_oidc_authorization_transaction",
                wraps=(
                    durable_gateway_module
                    .claim_google_oidc_authorization_transaction
                ),
            ) as first_claim,
            mock.patch.object(
                durable_gateway_module,
                "_complete_claimed_authorization",
                wraps=durable_gateway_module._complete_claimed_authorization,
            ) as first_downstream,
            mock.patch.object(
                gateway_module,
                "_validated_code_id_token",
                side_effect=original_validate,
            ) as first_id_token_validation,
        ):
            first = complete_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
                callback,
                completion_policy(),
                self.vault(),
            )
        self.assertEqual(first.status, "issued")
        first_claim.assert_called_once()
        first_downstream.assert_called_once()
        first_id_token_validation.assert_called_once()
        first_authoritative_callback = first_downstream.call_args.args[2]
        self.assertEqual(
            {
                name
                for name, _value in parse_qsl(
                    urlsplit(first_authoritative_callback).query,
                    keep_blank_values=True,
                    strict_parsing=True,
                )
            },
            {"code", "iss", "state"},
        )
        for extension_name, extension_value in real_google_extensions:
            self.assertNotIn(extension_name, first_authoritative_callback)
            self.assertNotIn(extension_value, first_authoritative_callback)
            self.assertNotIn(
                extension_value,
                repr(first_id_token_validation.call_args),
            )
        self.assertEqual(harness.transport.token_request_count, 1)
        self.assertEqual(harness.transport.jwks_request_count, 1)
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
        future_extension = (
            "future_provider_extension",
            "synthetic-future-provider-extension",
        )
        later_callback = harness.transport.callback_for(
            later,
            code="later-invited-login",
            extra_pairs=(future_extension,),
            missing_claims=("email", "email_verified"),
        )
        with mock.patch.object(
            durable_gateway_module,
            "_complete_claimed_authorization",
            wraps=durable_gateway_module._complete_claimed_authorization,
        ) as later_downstream:
            second = complete_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
                later_callback,
                completion_policy(),
                self.vault(),
            )
        later_downstream.assert_called_once()
        later_authoritative_callback = later_downstream.call_args.args[2]
        self.assertNotIn(future_extension[0], later_authoritative_callback)
        self.assertNotIn(future_extension[1], later_authoritative_callback)
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
        self.assertEqual(events, [])
        self.assertEqual(harness.transport.token_request_count, 2)
        self.assertEqual(harness.transport.jwks_request_count, 1)
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
        database_text = "\n".join(database.connection.iterdump())
        for _extension_name, extension_value in (
            *real_google_extensions,
            future_extension,
        ):
            self.assertNotIn(extension_value, database_text)
            self.assertNotIn(extension_value, public)
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
            (
                "missing-invitation",
                None,
                "new@example.test",
                True,
                "invitation_email_agreement_rejected",
            ),
            (
                "unknown-invitation",
                "inv_" + ("9" * 32) + "." + ("Z" * 43),
                "new@example.test",
                True,
                "invitation_email_agreement_rejected",
            ),
            (
                "missing-email",
                "create",
                None,
                True,
                "verified_email_rejected",
            ),
            (
                "unverified-email",
                "create",
                "new@example.test",
                False,
                "verified_email_rejected",
            ),
            (
                "malformed-email",
                "create",
                "not-an-email",
                True,
                "verified_email_rejected",
            ),
            (
                "mismatched-email",
                "create",
                "other@example.test",
                True,
                "invitation_email_agreement_rejected",
            ),
            (
                "expired-invitation",
                "expired",
                "new@example.test",
                True,
                "invitation_email_agreement_rejected",
            ),
            (
                "revoked-invitation",
                "revoked",
                "new@example.test",
                True,
                "invitation_email_agreement_rejected",
            ),
            (
                "consumed-invitation",
                "consumed",
                "new@example.test",
                True,
                "invitation_email_agreement_rejected",
            ),
        )
        for name, invitation_mode, email, verified, expected_stage in scenarios:
            with self.subTest(name=name):
                events = []
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
                gateway_module._configure_callback_failure_telemetry(
                    harness.gateway,
                    events.append,
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
                self.assert_callback_failure_event(events, expected_stage)
                event = events[0]
                if email is not None:
                    self.assertNotIn(email, event)
                if credential is not None:
                    self.assertNotIn(credential, event)
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

    def test_bounded_error_extensions_are_discarded_then_denial_is_terminal(self):
        events = []
        database, authority, harness, prepared = self.prepare(
            "denial",
            telemetry_sink=events.append,
        )
        state = authorization_parameters(prepared)["state"]
        extension_pairs = (
            ("authuser", "synthetic-error-authuser"),
            ("hd", "synthetic-error-hd.invalid"),
            ("prompt", "synthetic-error-prompt"),
            ("scope", "synthetic-error-scope"),
        )
        callback = harness.transport.callback_for(
            prepared,
            error="access_denied",
            extra_pairs=(
                ("error_description", "authorization declined"),
                ("error_uri", "https://accounts.google.com/error"),
                *extension_pairs,
            ),
        )
        self.assertEqual(
            len(
                parse_qsl(
                    urlsplit(callback).query,
                    keep_blank_values=True,
                    strict_parsing=True,
                )
            ),
            gateway_module._CALLBACK_PARAMETER_LIMIT,
        )
        before_counts = self.mutation_snapshot(database.connection)[1]
        vault = self.vault()
        with (
            mock.patch.object(
                durable_gateway_module,
                "claim_google_oidc_authorization_transaction",
                wraps=(
                    durable_gateway_module
                    .claim_google_oidc_authorization_transaction
                ),
            ) as claim,
            mock.patch.object(
                durable_gateway_module,
                "_complete_claimed_authorization",
                wraps=durable_gateway_module._complete_claimed_authorization,
            ) as downstream,
        ):
            result = complete_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
                callback,
                completion_policy(),
                vault,
            )
        self.assertEqual(result.status, "authentication_denied")
        claim.assert_called_once()
        downstream.assert_called_once()
        self.assert_callback_failure_event(
            events,
            "provider_authorization_error",
        )
        authoritative_callback = downstream.call_args.args[2]
        authoritative_pairs = parse_qsl(
            urlsplit(authoritative_callback).query,
            keep_blank_values=True,
            strict_parsing=True,
        )
        self.assertEqual(len(authoritative_pairs), 5)
        authoritative_names = [name for name, _value in authoritative_pairs]
        self.assertEqual(
            sorted(authoritative_names),
            sorted(
                ("error", "error_description", "error_uri", "iss", "state")
            ),
        )
        authoritative_values = dict(authoritative_pairs)
        self.assertEqual(
            authoritative_values,
            {
                "error": "access_denied",
                "error_description": "authorization declined",
                "error_uri": "https://accounts.google.com/error",
                "iss": "https://accounts.google.com",
                "state": state,
            },
        )
        for extension_name, extension_value in extension_pairs:
            self.assertNotIn(extension_name, authoritative_names)
            self.assertNotIn(extension_value, repr(result))
            self.assertNotIn(
                extension_value,
                "\n".join(database.connection.iterdump()),
            )
        self.assertEqual(transaction_rows(database.connection)[0]["lifecycle"], "consumed")
        self.assertEqual(
            self.mutation_snapshot(database.connection)[1],
            before_counts,
        )
        self.assertEqual(harness.transport.token_request_count, 0)
        self.assertEqual(harness.transport.jwks_request_count, 0)
        replay = complete_durable_google_oidc_authorization(
            database.connection,
            harness.gateway,
            authority,
            callback,
            completion_policy(),
            vault,
        )
        self.assertEqual(replay.status, "invalid_or_expired_transaction")
        self.assertEqual(len(events), 1)
        self.assertEqual(harness.transport.token_request_count, 0)

    def test_real_google_six_field_success_reaches_claim_and_fake_provider(self):
        database, authority, harness, prepared = self.prepare(
            "exact-response-issuer"
        )
        state = authorization_parameters(prepared)["state"]
        code = "exact-response-issuer-code"
        harness.transport.callback_for(prepared, code=code)
        callback = self.callback_url(
            ("prompt", "synthetic-real-prompt"),
            ("state", state),
            ("authuser", "synthetic-real-authuser"),
            ("iss", "https://accounts.google.com"),
            ("scope", "synthetic-real-scope"),
            ("code", code),
        )
        self.assertEqual(
            {
                name
                for name, _value in parse_qsl(
                    urlsplit(callback).query,
                    keep_blank_values=True,
                    strict_parsing=True,
                )
            },
            {"authuser", "code", "iss", "prompt", "scope", "state"},
        )
        original_validate = gateway_module._validated_code_id_token
        with (
            mock.patch.object(
                durable_gateway_module,
                "claim_google_oidc_authorization_transaction",
                wraps=(
                    durable_gateway_module
                    .claim_google_oidc_authorization_transaction
                ),
            ) as claim,
            mock.patch.object(
                durable_gateway_module,
                "_complete_claimed_authorization",
                wraps=durable_gateway_module._complete_claimed_authorization,
            ) as downstream,
            mock.patch.object(
                gateway_module,
                "_validated_code_id_token",
                side_effect=original_validate,
            ) as validate_id_token,
        ):
            result = complete_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
                callback,
                completion_policy(),
                self.vault(),
            )
        self.assertEqual(result.status, "issued")
        claim.assert_called_once()
        self.assertEqual(claim.call_args.args[3], state)
        downstream.assert_called_once()
        validate_id_token.assert_called_once()
        authoritative_callback = downstream.call_args.args[2]
        self.assertEqual(
            {
                name
                for name, _value in parse_qsl(
                    urlsplit(authoritative_callback).query,
                    keep_blank_values=True,
                    strict_parsing=True,
                )
            },
            {"code", "iss", "state"},
        )
        for extension_value in (
            "synthetic-real-authuser",
            "synthetic-real-prompt",
            "synthetic-real-scope",
        ):
            self.assertNotIn(extension_value, authoritative_callback)
            self.assertNotIn(extension_value, repr(validate_id_token.call_args))
            self.assertNotIn(
                extension_value,
                "\n".join(database.connection.iterdump()),
            )
        self.assertEqual(
            transaction_rows(database.connection)[0]["lifecycle"],
            "consumed",
        )
        self.assertEqual(harness.transport.token_request_count, 1)
        self.assertEqual(harness.transport.jwks_request_count, 1)

    def test_google_hd_extension_reaches_claim_and_is_discarded(self):
        self.assert_success_extension_reaches_claim_and_is_discarded(
            "google-hd-extension",
            "hd",
            "synthetic-google-hd.invalid",
        )

    def test_generic_future_extension_reaches_claim_and_is_discarded(self):
        self.assert_success_extension_reaches_claim_and_is_discarded(
            "generic-future-extension",
            "future_provider_extension",
            "synthetic-generic-future-extension",
        )

    def test_missing_duplicate_blank_and_nonexact_response_issuers_stop_preclaim(self):
        issuer_cases = (
            ("missing", None),
            ("blank", ""),
            ("legacy_bare", "accounts.google.com"),
            ("http", "http://accounts.google.com"),
            ("trailing_slash", "https://accounts.google.com/"),
            ("explicit_port", "https://accounts.google.com:443"),
            ("google_subdomain", "https://login.accounts.google.com"),
            ("userinfo", "https://user@accounts.google.com"),
            ("path", "https://accounts.google.com/oidc"),
            ("query", "https://accounts.google.com?issuer=google"),
            ("fragment", "https://accounts.google.com#issuer"),
            ("mixed_case", "https://Accounts.Google.com"),
            ("control", "https://accounts.google.com\n"),
        )
        for name, issuer in issuer_cases:
            with self.subTest(case=name):
                self.assert_preclaim_rejection(
                    f"issuer-{name}",
                    lambda harness, prepared, _state, issuer=issuer: (
                        harness.transport.callback_for(
                            prepared,
                            code=f"issuer-{name}-code",
                            issuer=issuer,
                        )
                    ),
                )

        self.assert_preclaim_rejection(
            "issuer-duplicate",
            lambda harness, prepared, _state: (
                harness.transport.callback_for(
                    prepared,
                    code="issuer-duplicate-code",
                    extra_pairs=(("iss", "https://accounts.google.com"),),
                )
            ),
        )
        self.assert_preclaim_rejection(
            "issuer-malformed-percent",
            lambda _harness, _prepared, state: (
                REDIRECT_URI
                + f"?state={state}&code=issuer-malformed&iss=%"
            ),
        )
        self.assert_preclaim_rejection(
            "issuer-invalid-utf8",
            lambda _harness, _prepared, state: (
                REDIRECT_URI
                + f"?state={state}&code=issuer-utf8&iss=%FF"
            ),
        )

    def test_success_critical_shape_and_field_count_overflow_stop_preclaim(self):
        cases = (
            (
                "missing_state",
                lambda _harness, _prepared, _state: self.callback_url(
                    ("code", "missing-state"),
                    ("iss", "https://accounts.google.com"),
                ),
            ),
            (
                "missing_code",
                lambda _harness, _prepared, state: self.callback_url(
                    ("state", state),
                    ("iss", "https://accounts.google.com"),
                ),
            ),
            (
                "blank_state",
                lambda _harness, _prepared, _state: self.callback_url(
                    ("state", ""),
                    ("code", "blank-state"),
                    ("iss", "https://accounts.google.com"),
                ),
            ),
            (
                "blank_code",
                lambda _harness, _prepared, state: self.callback_url(
                    ("state", state),
                    ("code", ""),
                    ("iss", "https://accounts.google.com"),
                ),
            ),
            (
                "duplicate_state",
                lambda _harness, _prepared, state: self.callback_url(
                    ("state", state),
                    ("state", state),
                    ("code", "duplicate-state"),
                    ("iss", "https://accounts.google.com"),
                ),
            ),
            (
                "duplicate_code",
                lambda _harness, _prepared, state: self.callback_url(
                    ("state", state),
                    ("code", "duplicate-code-a"),
                    ("code", "duplicate-code-b"),
                    ("iss", "https://accounts.google.com"),
                ),
            ),
            (
                "field_count_overflow",
                lambda _harness, _prepared, state: self.callback_url(
                    ("state", state),
                    ("code", "field-count-overflow"),
                    ("iss", "https://accounts.google.com"),
                    *((f"extension_{index}", str(index)) for index in range(7)),
                ),
            ),
        )
        for name, callback_factory in cases:
            with self.subTest(case=name):
                self.assert_preclaim_rejection(
                    f"success-shape-{name}",
                    callback_factory,
                )

    def test_duplicate_decoded_extension_name_stops_preclaim(self):
        self.assert_preclaim_rejection(
            "duplicate-decoded-extension",
            lambda _harness, _prepared, state: (
                REDIRECT_URI
                + f"?state={state}&code=duplicate-extension"
                + "&iss=https%3A%2F%2Faccounts.google.com"
                + "&future_extension=first&%66uture_extension=second"
            ),
        )

    def test_error_response_issuer_failures_stop_before_claim(self):
        cases = (
            ("missing", None, ()),
            ("blank", "", ()),
            ("wrong", "accounts.google.com", ()),
            (
                "duplicate",
                "https://accounts.google.com",
                (("iss", "https://accounts.google.com"),),
            ),
        )
        for name, issuer, extra_pairs in cases:
            with self.subTest(case=name):
                self.assert_preclaim_rejection(
                    f"error-issuer-{name}",
                    lambda harness, prepared, _state,
                    issuer=issuer, extra_pairs=extra_pairs: (
                        harness.transport.callback_for(
                            prepared,
                            error="access_denied",
                            issuer=issuer,
                            extra_pairs=extra_pairs,
                        )
                    ),
                )

    def test_exact_issuer_unknown_state_reaches_distinct_lookup_miss(self):
        events = []
        database, authority, harness, _prepared = self.prepare(
            "unknown-state-lookup",
            telemetry_sink=events.append,
        )
        unknown_state = "A" * 43
        callback = self.callback_url(
            ("state", unknown_state),
            ("iss", "https://accounts.google.com"),
            ("code", "unknown-state-code"),
        )
        before = self.mutation_snapshot(database.connection)
        with mock.patch.object(
            durable_gateway_module,
            "claim_google_oidc_authorization_transaction",
            wraps=(
                durable_gateway_module
                .claim_google_oidc_authorization_transaction
            ),
        ) as claim:
            result = complete_durable_google_oidc_authorization(
                database.connection,
                harness.gateway,
                authority,
                callback,
                completion_policy(),
                self.vault(),
            )
        self.assertEqual(result.status, "invalid_or_expired_transaction")
        claim.assert_called_once()
        self.assertEqual(claim.call_args.args[3], unknown_state)
        self.assertEqual(self.mutation_snapshot(database.connection), before)
        self.assertEqual(harness.transport.token_request_count, 0)
        self.assertEqual(harness.transport.jwks_request_count, 0)
        self.assertEqual(events, [])

    def test_malformed_callback_fails_before_claim_and_preserves_prepared_row(self):
        events = []
        database, authority, harness, _prepared = self.prepare(
            "malformed",
            telemetry_sink=events.append,
        )
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
        self.assertEqual(events, [])

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
