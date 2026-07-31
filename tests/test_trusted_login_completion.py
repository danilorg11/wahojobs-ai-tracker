import base64
import ast
from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import replace
from datetime import timedelta
import gc
import inspect
import json
import pickle
import sqlite3
import threading
import unittest
from unittest import mock
import weakref

from tests.accounts_test_support import create_user
from tests.browser_session_lifecycle_test_support import (
    consume_issued,
    recursively_reachable_objects,
    request_secret_vault,
    revoke_browser_session,
    revoke_command,
    rotate_browser_session,
    rotate_command,
    token_from_cookie_header,
    vault_entry_count,
    vault_is_closed_and_empty,
)
from tests.browser_session_authentication_test_support import (
    browser_request,
    read_only_connection,
)
from tests.trusted_login_completion_test_support import (
    NOW,
    TRUSTED_NOW,
    close_secret_vault,
    completion_policy,
    connect,
    complete_login,
    consume_login,
    finalize_login,
    login_database,
    session_policy,
    trusted_assertion,
)
from wahojobs.browser_session_authentication import (
    DurableBrowserSessionAuthenticationGateway,
)
import wahojobs.browser_session_lifecycle as lifecycle
from wahojobs.browser_session_lifecycle import BrowserSessionLifecycleError
import wahojobs.trusted_login_completion as completion
from wahojobs.trusted_login_completion import (
    TrustedExternalIdentityAuthentication,
    TrustedLoginCompletionPolicy,
    TrustedLoginCompletionResult,
    TrustedLoginSessionPolicy,
)


class TrustedExternalAuthenticationContractTests(unittest.TestCase):
    def test_test_issuer_creates_exact_redacted_bounded_assertion(self):
        with login_database(suffix="assertion-valid") as (_path, _connection, created):
            assertion = trusted_assertion(created)
            self.assertIs(type(assertion), TrustedExternalIdentityAuthentication)
            self.assertFalse(hasattr(assertion, "__dict__"))
            self.assertEqual(
                repr(assertion),
                "TrustedExternalIdentityAuthentication(<redacted>)",
            )
            self.assertEqual(str(assertion), repr(assertion))
            text = repr(assertion)
            self.assertNotIn(created.user.user_id, text)
            self.assertNotIn(created.identity.auth_identity_id, text)
            for forbidden in (
                "password",
                "oauth_token",
                "authorization_code",
                "cookie",
                "provider_secret",
                "email",
                "display_name",
                "provider_subject",
            ):
                self.assertFalse(hasattr(assertion, forbidden))

    def test_direct_dictionary_copy_pickle_replace_subclass_and_duck_forgery_fail(self):
        with login_database(suffix="assertion-forgery") as (_path, _connection, created):
            assertion = trusted_assertion(created)
            for operation in (
                lambda: TrustedExternalIdentityAuthentication(),
                lambda: TrustedExternalIdentityAuthentication(
                    **{
                        "account_id": created.user.user_id,
                        "identity_id": created.identity.auth_identity_id,
                    }
                ),
                lambda: copy.copy(assertion),
                lambda: copy.deepcopy(assertion),
                lambda: pickle.dumps(assertion),
                lambda: replace(assertion),
                lambda: type(
                    "ForgedTrustedExternalIdentityAuthentication",
                    (TrustedExternalIdentityAuthentication,),
                    {},
                ),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaises(TypeError):
                        operation()
            with self.assertRaises(TypeError):
                json.dumps(assertion)

            class DuckAssertion:
                account_id = created.user.user_id
                identity_id = created.identity.auth_identity_id

            result, vault = complete_login(_connection, DuckAssertion())
            self.assertEqual(result.status, "authentication_denied")
            self.assertEqual(vault_entry_count(vault), 0)
            close_secret_vault(vault)

    def test_assertion_immutability_and_registry_reject_bypass(self):
        with login_database(suffix="assertion-seal") as (_path, connection, created):
            assertion = trusted_assertion(created)
            with self.assertRaises(AttributeError):
                assertion.account_id = "usr_" + "1" * 32
            object.__setattr__(assertion, "_account_id", "usr_" + "1" * 32)
            tampered, tampered_vault = complete_login(connection, assertion)
            self.assertEqual(tampered.status, "authentication_denied")
            self.assertEqual(vault_entry_count(tampered_vault), 0)
            close_secret_vault(tampered_vault)
            bypass = object.__new__(TrustedExternalIdentityAuthentication)
            result, vault = complete_login(connection, bypass)
            self.assertEqual(result.status, "authentication_denied")
            close_secret_vault(vault)

    def test_canonical_time_future_and_expiry_rules(self):
        with login_database(suffix="assertion-time") as (_path, connection, created):
            with self.assertRaises(TypeError):
                trusted_assertion(created, authenticated_at=NOW.replace(microsecond=1))
            with self.assertRaises(TypeError):
                trusted_assertion(created, expires_at=NOW)

            future = trusted_assertion(
                created,
                authenticated_at=NOW + timedelta(minutes=2),
                expires_at=NOW + timedelta(minutes=3),
            )
            result, vault = complete_login(connection, future)
            self.assertEqual(result.status, "authentication_denied")
            close_secret_vault(vault)

            stale = trusted_assertion(created, expires_at=TRUSTED_NOW)
            result, vault = complete_login(connection, stale)
            self.assertEqual(result.status, "authentication_denied")
            close_secret_vault(vault)

            result, vault = complete_login(
                connection,
                trusted_assertion(created),
                trusted_now=TRUSTED_NOW.replace(microsecond=1),
            )
            self.assertEqual(result.status, "unavailable")
            close_secret_vault(vault)

    def test_session_policy_is_exact_bounded_immutable_and_redacted(self):
        policy = session_policy()
        self.assertIs(type(policy), TrustedLoginSessionPolicy)
        self.assertFalse(hasattr(policy, "__dict__"))
        self.assertEqual(repr(policy), "TrustedLoginSessionPolicy(<configured>)")
        with self.assertRaises(AttributeError):
            policy.idle_ttl = timedelta(days=1)
        for operation in (
            lambda: copy.copy(policy),
            lambda: copy.deepcopy(policy),
            lambda: pickle.dumps(policy),
            lambda: type("ForgedPolicy", (TrustedLoginSessionPolicy,), {}),
        ):
            with self.assertRaises(TypeError):
                operation()
        with self.assertRaises(TypeError):
            session_policy(idle_ttl=timedelta(seconds=59))
        with self.assertRaises(TypeError):
            session_policy(idle_ttl=timedelta(days=8), absolute_ttl=timedelta(days=7))

    def test_completion_policy_is_sealed_bounded_immutable_and_redacted(self):
        policy = completion_policy()
        self.assertIs(type(policy), TrustedLoginCompletionPolicy)
        self.assertFalse(hasattr(policy, "__dict__"))
        self.assertEqual(repr(policy), "TrustedLoginCompletionPolicy(<configured>)")
        self.assertEqual(str(policy), repr(policy))
        for operation in (
            lambda: TrustedLoginCompletionPolicy(),
            lambda: TrustedLoginCompletionPolicy(expected_provider="google"),
            lambda: copy.copy(policy),
            lambda: copy.deepcopy(policy),
            lambda: pickle.dumps(policy),
            lambda: replace(policy),
            lambda: type("ForgedCompletionPolicy", (TrustedLoginCompletionPolicy,), {}),
            lambda: completion_policy(expected_provider="github"),
            lambda: completion_policy(environment_namespace="unconfigured"),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(TypeError):
                    operation()
        with self.assertRaises(TypeError):
            json.dumps(policy)
        with self.assertRaises(AttributeError):
            policy.expected_provider = "google"
        object.__setattr__(
            policy,
            "_expected_assurance_policy_version",
            "unexpected_policy_v999",
        )
        with login_database(suffix="completion-policy-tamper") as (
            _path,
            connection,
            created,
        ):
            result, vault = complete_login(
                connection,
                trusted_assertion(created),
                policy=policy,
            )
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(vault_entry_count(vault), 0)
            close_secret_vault(vault)


class TrustedLoginCompletionTests(unittest.TestCase):
    def test_unexpected_assurance_policy_is_denied_before_command_or_credentials(self):
        with login_database(suffix="unexpected-assurance-policy") as (
            _path,
            connection,
            created,
        ):
            assertion = trusted_assertion(
                created,
                assurance_policy_version="unexpected_policy_v999",
            )
            with mock.patch(
                "wahojobs.trusted_login_completion._issue_create_session_command_and_execute"
            ) as issuer, mock.patch(
                "wahojobs.browser_session_lifecycle._generate_credential"
            ) as generator:
                result, vault = complete_login(connection, assertion)
            self.assertEqual(result.status, "authentication_denied")
            issuer.assert_not_called()
            generator.assert_not_called()
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )
            self.assertEqual(vault_entry_count(vault), 0)
            close_secret_vault(vault)

    def test_trusted_policy_facts_are_bound_into_idempotent_replay(self):
        scenarios = (
            (
                "assurance",
                trusted_assertion,
                lambda: completion_policy(
                    expected_assurance_policy_version="unexpected_policy_v999"
                ),
            ),
            (
                "environment",
                lambda created: trusted_assertion(
                    created,
                    environment_namespace="private_beta",
                ),
                lambda: completion_policy(environment_namespace="private_beta"),
            ),
            (
                "completion_policy_version",
                trusted_assertion,
                lambda: completion_policy(
                    completion_policy_version="trusted_login_completion_v2"
                ),
            ),
        )
        for scenario, assertion_factory, policy_factory in scenarios:
            with self.subTest(scenario=scenario):
                with login_database(suffix=f"policy-binding-{scenario}") as (
                    _path,
                    connection,
                    created,
                ):
                    first, first_vault = complete_login(
                        connection,
                        trusted_assertion(created),
                    )
                    self.assertEqual(first.status, "issued")
                    with mock.patch(
                        "wahojobs.trusted_login_completion._issue_create_session_command_and_execute"
                    ) as issuer, mock.patch(
                        "wahojobs.browser_session_lifecycle._generate_credential"
                    ) as generator:
                        conflict, conflict_vault = complete_login(
                            connection,
                            assertion_factory(created),
                            policy=policy_factory(),
                        )
                    self.assertEqual(conflict.status, "idempotency_conflict")
                    issuer.assert_not_called()
                    generator.assert_not_called()
                    self.assertEqual(vault_entry_count(conflict_vault), 0)
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM account_sessions"
                        ).fetchone()[0],
                        1,
                    )
                    close_secret_vault(first_vault)
                    close_secret_vault(conflict_vault)

    def test_future_closed_provider_domain_change_is_idempotency_conflict(self):
        with login_database(suffix="policy-binding-provider") as (
            _path,
            connection,
            created,
        ):
            first, first_vault = complete_login(
                connection,
                trusted_assertion(created),
            )
            self.assertEqual(first.status, "issued")
            with mock.patch.object(
                completion,
                "PROVIDERS",
                {"google", "github"},
            ), mock.patch(
                "wahojobs.trusted_login_completion._issue_create_session_command_and_execute"
            ) as issuer, mock.patch(
                "wahojobs.browser_session_lifecycle._generate_credential"
            ) as generator:
                conflict, conflict_vault = complete_login(
                    connection,
                    trusted_assertion(created),
                    policy=completion_policy(expected_provider="github"),
                )
            self.assertEqual(conflict.status, "idempotency_conflict")
            issuer.assert_not_called()
            generator.assert_not_called()
            self.assertEqual(vault_entry_count(conflict_vault), 0)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM account_sessions"
                ).fetchone()[0],
                1,
            )
            close_secret_vault(first_vault)
            close_secret_vault(conflict_vault)

    def test_one_time_top_level_finalization_failure_retries_exact_issuance(self):
        with login_database(suffix="top-level-finalization-retry") as (
            _path,
            connection,
            created,
        ):
            original = completion.finalize_pending_issued_session
            injected = KeyboardInterrupt("private-finalization-marker")
            calls = []

            def fail_once(*args, **kwargs):
                calls.append("attempt")
                if len(calls) == 1:
                    raise injected
                return original(*args, **kwargs)

            with mock.patch(
                "wahojobs.trusted_login_completion.finalize_pending_issued_session",
                side_effect=fail_once,
            ), mock.patch(
                "wahojobs.browser_session_lifecycle._generate_credential",
                wraps=__import__(
                    "wahojobs.browser_session_lifecycle",
                    fromlist=["_generate_credential"],
                )._generate_credential,
            ) as generator:
                result, vault = complete_login(
                    connection,
                    trusted_assertion(created),
                )
            self.assertEqual(result.status, "issued")
            self.assertEqual(len(calls), 2)
            self.assertIsNone(injected.__traceback__)
            self.assertIsNone(injected.__cause__)
            self.assertIsNone(injected.__context__)
            self.assertEqual(generator.call_count, 2)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                1,
            )
            self.assertEqual(vault_entry_count(vault), 1)
            consume_login(result, vault)
            replay, replay_vault = complete_login(
                connection,
                trusted_assertion(created),
            )
            self.assertEqual(replay.status, "already_completed")
            self.assertEqual(vault_entry_count(replay_vault), 0)
            close_secret_vault(vault)
            close_secret_vault(replay_vault)

    def test_finalization_control_flow_exceptions_propagate_after_safety(self):
        token = base64.urlsafe_b64encode(b"E" * 32).rstrip(b"=").decode("ascii")
        csrf = base64.urlsafe_b64encode(b"F" * 32).rstrip(b"=").decode("ascii")
        for index, exception_type in enumerate((SystemExit, GeneratorExit), start=1):
            with self.subTest(exception_type=exception_type.__name__):
                with login_database(suffix=f"finalization-control-{index}") as (
                    path,
                    connection,
                    created,
                ):
                    vault = request_secret_vault()
                    injected = exception_type("private-control-flow-marker")
                    with mock.patch(
                        "wahojobs.trusted_login_completion.finalize_pending_issued_session",
                        side_effect=injected,
                    ) as finalizer, mock.patch(
                        "wahojobs.browser_session_lifecycle._generate_credential",
                        side_effect=(token, csrf),
                    ):
                        with self.assertRaises(exception_type) as raised:
                            complete_login(
                                connection,
                                trusted_assertion(created),
                                vault=vault,
                            )
                    self.assertIs(raised.exception, injected)
                    self.assertEqual(finalizer.call_count, 1)
                    self.assertTrue(vault_is_closed_and_empty(vault))
                    row = connection.execute(
                        "SELECT session_version, revoked_at, revoke_reason "
                        "FROM account_sessions"
                    ).fetchone()
                    self.assertEqual(row["session_version"], 2)
                    self.assertIsNotNone(row["revoked_at"])
                    self.assertEqual(row["revoke_reason"], "security_reset")
                    gateway = DurableBrowserSessionAuthenticationGateway(
                        trusted_environment_namespace="test",
                        clock=lambda: TRUSTED_NOW,
                    )
                    with read_only_connection(path) as reader:
                        reader.execute("BEGIN")
                        try:
                            actor = gateway.authenticate_browser_request(
                                reader,
                                browser_request(token),
                                now=TRUSTED_NOW,
                            )
                        finally:
                            reader.rollback()
                    self.assertIsNone(actor)
                    close_secret_vault(vault)

    def test_real_lower_finalizer_control_flow_propagates_once_after_safety(self):
        token = base64.urlsafe_b64encode(b"K" * 32).rstrip(b"=").decode("ascii")
        csrf = base64.urlsafe_b64encode(b"L" * 32).rstrip(b"=").decode("ascii")
        for index, exception_type in enumerate((SystemExit, GeneratorExit), start=1):
            with self.subTest(exception_type=exception_type.__name__):
                with login_database(suffix=f"lower-finalization-control-{index}") as (
                    path,
                    connection,
                    created,
                ):
                    vault = request_secret_vault()
                    injected = exception_type("private-lower-control-flow-marker")
                    calls = []
                    captured = {}

                    def fail_at_lower_boundary(*_args, **_kwargs):
                        calls.append("attempt")
                        entry = next(iter(vault._entries.values()))
                        captured["token"] = entry.token_buffer
                        captured["csrf"] = entry.csrf_buffer
                        raise injected

                    returned = None
                    with mock.patch(
                        "wahojobs.browser_session_lifecycle._finalize_pending_issued_session",
                        side_effect=fail_at_lower_boundary,
                    ), mock.patch(
                        "wahojobs.browser_session_lifecycle._generate_credential",
                        side_effect=(token, csrf),
                    ):
                        with self.assertRaises(exception_type) as raised:
                            returned, _returned_vault = complete_login(
                                connection,
                                trusted_assertion(created),
                                vault=vault,
                            )
                    self.assertIsNone(returned)
                    self.assertIs(raised.exception, injected)
                    self.assertEqual(calls, ["attempt"])
                    self.assertTrue(vault_is_closed_and_empty(vault))
                    self.assertTrue(all(value == 0 for value in captured["token"]))
                    self.assertTrue(all(value == 0 for value in captured["csrf"]))
                    row = connection.execute("SELECT * FROM account_sessions").fetchone()
                    self.assertEqual(row["session_version"], 2)
                    self.assertIsNotNone(row["revoked_at"])
                    self.assertEqual(row["revoke_reason"], "security_reset")
                    gateway = DurableBrowserSessionAuthenticationGateway(
                        trusted_environment_namespace="test",
                        clock=lambda: TRUSTED_NOW,
                    )
                    with read_only_connection(path) as reader:
                        reader.execute("BEGIN")
                        try:
                            actor = gateway.authenticate_browser_request(
                                reader,
                                browser_request(token),
                                now=TRUSTED_NOW,
                            )
                        finally:
                            reader.rollback()
                    self.assertIsNone(actor)
                    close_secret_vault(vault)

    def test_real_lower_finalizer_ordinary_failure_retains_bounded_retry(self):
        with login_database(suffix="lower-finalization-ordinary-retry") as (
            _path,
            connection,
            created,
        ):
            original = lifecycle._finalize_pending_issued_session
            calls = []

            def fail_once(*args, **kwargs):
                calls.append("attempt")
                if len(calls) == 1:
                    raise RuntimeError("private-lower-finalization-marker")
                return original(*args, **kwargs)

            with mock.patch(
                "wahojobs.browser_session_lifecycle._finalize_pending_issued_session",
                side_effect=fail_once,
            ):
                result, vault = complete_login(
                    connection,
                    trusted_assertion(created),
                )
            self.assertEqual(result.status, "issued")
            self.assertEqual(calls, ["attempt", "attempt"])
            self.assertEqual(vault_entry_count(vault), 1)
            consume_login(result, vault)
            close_secret_vault(vault)

    def test_control_flow_during_compensation_is_forced_safe_then_propagated(self):
        token = base64.urlsafe_b64encode(b"G" * 32).rstrip(b"=").decode("ascii")
        csrf = base64.urlsafe_b64encode(b"H" * 32).rstrip(b"=").decode("ascii")
        with login_database(suffix="compensation-control-flow") as (
            path,
            connection,
            created,
        ):
            vault = request_secret_vault()
            injected = SystemExit("private-compensation-control-marker")
            original_force = completion.force_compensate_undelivered_issued_session
            with mock.patch(
                "wahojobs.trusted_login_completion.finalize_pending_issued_session",
                side_effect=RuntimeError("private-finalization-marker"),
            ) as finalizer, mock.patch(
                "wahojobs.trusted_login_completion.compensate_undelivered_issued_session",
                side_effect=injected,
            ) as compensator, mock.patch(
                "wahojobs.trusted_login_completion.force_compensate_undelivered_issued_session",
                wraps=original_force,
            ) as forced, mock.patch(
                "wahojobs.browser_session_lifecycle._generate_credential",
                side_effect=(token, csrf),
            ):
                with self.assertRaises(SystemExit) as raised:
                    complete_login(
                        connection,
                        trusted_assertion(created),
                        vault=vault,
                    )
            self.assertIs(raised.exception, injected)
            self.assertEqual(finalizer.call_count, 2)
            self.assertEqual(compensator.call_count, 1)
            self.assertEqual(forced.call_count, 1)
            self.assertTrue(vault_is_closed_and_empty(vault))
            row = connection.execute("SELECT * FROM account_sessions").fetchone()
            self.assertEqual(row["session_version"], 2)
            self.assertEqual(row["revoke_reason"], "security_reset")
            gateway = DurableBrowserSessionAuthenticationGateway(
                trusted_environment_namespace="test",
                clock=lambda: TRUSTED_NOW,
            )
            with read_only_connection(path) as reader:
                reader.execute("BEGIN")
                try:
                    actor = gateway.authenticate_browser_request(
                        reader,
                        browser_request(token),
                        now=TRUSTED_NOW,
                    )
                finally:
                    reader.rollback()
            self.assertIsNone(actor)
            close_secret_vault(vault)

    def test_control_flow_during_cleanup_emergency_clears_then_propagates(self):
        with login_database(suffix="cleanup-control-flow") as (
            _path,
            connection,
            created,
        ):
            vault = request_secret_vault()
            captured = {}
            injected = GeneratorExit("private-cleanup-control-marker")

            def fail_after_creation(point):
                if point == "after_session_creation":
                    entry = next(iter(vault._entries.values()))
                    captured["token"] = entry.token_buffer
                    captured["csrf"] = entry.csrf_buffer
                    raise RuntimeError("private-provider-context-marker")

            original_emergency = (
                completion.emergency_terminalize_request_scoped_secret_vault
            )
            with mock.patch(
                "wahojobs.trusted_login_completion.abort_request_scoped_secret_vault",
                side_effect=injected,
            ) as cleanup, mock.patch(
                "wahojobs.trusted_login_completion.emergency_terminalize_request_scoped_secret_vault",
                wraps=original_emergency,
            ) as emergency:
                with self.assertRaises(GeneratorExit) as raised:
                    complete_login(
                        connection,
                        trusted_assertion(created),
                        vault=vault,
                        _failure_injector=fail_after_creation,
                    )
            self.assertIs(raised.exception, injected)
            self.assertEqual(cleanup.call_count, 1)
            self.assertEqual(emergency.call_count, 1)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM account_sessions"
                ).fetchone()[0],
                0,
            )
            self.assertTrue(vault_is_closed_and_empty(vault))
            self.assertTrue(all(value == 0 for value in captured["token"]))
            self.assertTrue(all(value == 0 for value in captured["csrf"]))
            close_secret_vault(vault)

    def test_permanent_top_level_finalization_failure_is_compensated(self):
        token = base64.urlsafe_b64encode(b"T" * 32).rstrip(b"=").decode("ascii")
        csrf = base64.urlsafe_b64encode(b"S" * 32).rstrip(b"=").decode("ascii")
        marker = "private-permanent-finalization-marker"
        with login_database(suffix="top-level-finalization-compensation") as (
            path,
            connection,
            created,
        ):
            with mock.patch(
                "wahojobs.trusted_login_completion.finalize_pending_issued_session",
                side_effect=RuntimeError(marker),
            ) as finalize_call, mock.patch(
                "wahojobs.browser_session_lifecycle._generate_credential",
                side_effect=(token, csrf),
            ):
                result, vault = complete_login(
                    connection,
                    trusted_assertion(created),
                )
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(finalize_call.call_count, 2)
            self.assertTrue(vault_is_closed_and_empty(vault))
            row = connection.execute("SELECT * FROM account_sessions").fetchone()
            self.assertEqual(row["session_version"], 2)
            self.assertIsNone(row["rotated_at"])
            self.assertIsNotNone(row["revoked_at"])
            self.assertEqual(row["revoke_reason"], "security_reset")
            self.assertTrue(row["creation_idempotency_key"].endswith(".undelivered"))

            gateway = DurableBrowserSessionAuthenticationGateway(
                trusted_environment_namespace="test",
                clock=lambda: TRUSTED_NOW,
            )
            with read_only_connection(path) as reader:
                reader.execute("BEGIN")
                try:
                    authenticated = gateway.authenticate_browser_request(
                        reader,
                        browser_request(token),
                        now=TRUSTED_NOW,
                    )
                finally:
                    reader.rollback()
            self.assertIsNone(authenticated)

            with mock.patch(
                "wahojobs.browser_session_lifecycle._generate_credential"
            ) as generator:
                replay, replay_vault = complete_login(
                    connection,
                    trusted_assertion(created),
                )
            self.assertEqual(replay.status, "idempotency_conflict")
            generator.assert_not_called()
            self.assertEqual(vault_entry_count(replay_vault), 0)
            reached = recursively_reachable_objects(result)
            for private in (
                marker,
                token,
                csrf,
                created.user.user_id,
                created.identity.auth_identity_id,
                row["session_id"],
            ):
                self.assertNotIn(private, repr(result))
                self.assertNotIn(private, reached)
            close_secret_vault(vault)
            close_secret_vault(replay_vault)

    def test_compensation_and_cleanup_each_retry_one_independent_failure(self):
        with login_database(suffix="compensation-cleanup-retry") as (
            _path,
            connection,
            created,
        ):
            original_compensate = completion.compensate_undelivered_issued_session
            original_abort = completion.abort_request_scoped_secret_vault
            compensation_calls = []
            cleanup_calls = []

            def compensate_fail_once(*args, **kwargs):
                compensation_calls.append("attempt")
                if len(compensation_calls) == 1:
                    raise RuntimeError("private-compensation-marker")
                return original_compensate(*args, **kwargs)

            def cleanup_fail_once(*args, **kwargs):
                cleanup_calls.append("attempt")
                if len(cleanup_calls) == 1:
                    raise RuntimeError("private-compensation-cleanup-marker")
                return original_abort(*args, **kwargs)

            with mock.patch(
                "wahojobs.trusted_login_completion.finalize_pending_issued_session",
                side_effect=RuntimeError("private-finalization-marker"),
            ), mock.patch(
                "wahojobs.trusted_login_completion.compensate_undelivered_issued_session",
                side_effect=compensate_fail_once,
            ), mock.patch(
                "wahojobs.trusted_login_completion.abort_request_scoped_secret_vault",
                side_effect=cleanup_fail_once,
            ):
                result, vault = complete_login(
                    connection,
                    trusted_assertion(created),
                )
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(len(compensation_calls), 2)
            self.assertEqual(len(cleanup_calls), 2)
            self.assertTrue(vault_is_closed_and_empty(vault))
            row = connection.execute("SELECT * FROM account_sessions").fetchone()
            self.assertEqual(row["session_version"], 2)
            self.assertEqual(row["revoke_reason"], "security_reset")
            self.assertTrue(row["creation_idempotency_key"].endswith(".undelivered"))
            self.assertNotIn("private", repr(result))
            close_secret_vault(vault)

    def test_both_ordinary_compensations_fail_emergency_is_exact_and_verified(self):
        token = base64.urlsafe_b64encode(b"I" * 32).rstrip(b"=").decode("ascii")
        csrf = base64.urlsafe_b64encode(b"J" * 32).rstrip(b"=").decode("ascii")
        with login_database(suffix="compensation-emergency") as (
            path,
            connection,
            created,
        ):
            delivered, delivered_vault = complete_login(
                connection,
                trusted_assertion(created),
                key="trusted-login-unrelated-session",
            )
            delivered_response = consume_login(delivered, delivered_vault)
            delivered_token = token_from_cookie_header(
                delivered_response.set_cookie_header
            )
            original_force = completion.force_compensate_undelivered_issued_session
            with mock.patch(
                "wahojobs.trusted_login_completion.finalize_pending_issued_session",
                side_effect=RuntimeError("private-finalization-marker"),
            ) as finalizer, mock.patch(
                "wahojobs.trusted_login_completion.compensate_undelivered_issued_session",
                side_effect=RuntimeError("private-compensation-marker"),
            ) as compensator, mock.patch(
                "wahojobs.trusted_login_completion.force_compensate_undelivered_issued_session",
                wraps=original_force,
            ) as forced, mock.patch(
                "wahojobs.browser_session_lifecycle._generate_credential",
                side_effect=(token, csrf),
            ):
                failed, failed_vault = complete_login(
                    connection,
                    trusted_assertion(created),
                )
            self.assertEqual(failed.status, "unavailable")
            self.assertEqual(finalizer.call_count, 2)
            self.assertEqual(compensator.call_count, 2)
            self.assertEqual(forced.call_count, 1)
            self.assertTrue(vault_is_closed_and_empty(failed_vault))
            rows = connection.execute("SELECT * FROM account_sessions").fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                sum(
                    row["session_version"] == 1
                    and row["revoked_at"] is None
                    for row in rows
                ),
                1,
            )
            self.assertEqual(
                sum(
                    row["session_version"] == 2
                    and row["revoke_reason"] == "security_reset"
                    and row["creation_idempotency_key"].endswith(".undelivered")
                    for row in rows
                ),
                1,
            )
            gateway = DurableBrowserSessionAuthenticationGateway(
                trusted_environment_namespace="test",
                clock=lambda: TRUSTED_NOW,
            )
            with read_only_connection(path) as reader:
                reader.execute("BEGIN")
                try:
                    failed_actor = gateway.authenticate_browser_request(
                        reader,
                        browser_request(token),
                        now=TRUSTED_NOW,
                    )
                    delivered_actor = gateway.authenticate_browser_request(
                        reader,
                        browser_request(delivered_token),
                        now=TRUSTED_NOW,
                    )
                finally:
                    reader.rollback()
            self.assertIsNone(failed_actor)
            self.assertIsNotNone(delivered_actor)

            replay, replay_vault = complete_login(
                connection,
                trusted_assertion(created),
            )
            fresh, fresh_vault = complete_login(
                connection,
                trusted_assertion(created),
                key="trusted-login-after-safe-compensation",
            )
            self.assertEqual(replay.status, "idempotency_conflict")
            self.assertEqual(vault_entry_count(replay_vault), 0)
            self.assertEqual(fresh.status, "issued")
            self.assertEqual(vault_entry_count(fresh_vault), 1)
            close_secret_vault(delivered_vault)
            close_secret_vault(failed_vault)
            close_secret_vault(replay_vault)
            close_secret_vault(fresh_vault)

    def test_lower_ordinary_compensation_failure_cannot_disable_terminal_recovery(self):
        token = base64.urlsafe_b64encode(b"M" * 32).rstrip(b"=").decode("ascii")
        csrf = base64.urlsafe_b64encode(b"N" * 32).rstrip(b"=").decode("ascii")
        with login_database(suffix="lower-compensation-independence") as (
            path,
            connection,
            created,
        ):
            delivered, delivered_vault = complete_login(
                connection,
                trusted_assertion(created),
                key="trusted-login-independent-delivered",
            )
            delivered_response = consume_login(delivered, delivered_vault)
            delivered_token = token_from_cookie_header(
                delivered_response.set_cookie_header
            )
            delivered_before = dict(
                connection.execute("SELECT * FROM account_sessions").fetchone()
            )
            captured = {}

            def fail_ordinary(_connection, _result, vault, *_args, **_kwargs):
                entry = next(iter(vault._entries.values()))
                captured.setdefault("token", entry.token_buffer)
                captured.setdefault("csrf", entry.csrf_buffer)
                raise RuntimeError("private-lower-compensation-marker")

            original_terminal = lifecycle._force_compensate_undelivered_issued_session
            with mock.patch(
                "wahojobs.browser_session_lifecycle._finalize_pending_issued_session",
                side_effect=RuntimeError("private-lower-finalization-marker"),
            ) as finalizer, mock.patch(
                "wahojobs.browser_session_lifecycle._compensate_undelivered_issued_session",
                side_effect=fail_ordinary,
            ) as ordinary, mock.patch(
                "wahojobs.browser_session_lifecycle._force_compensate_undelivered_issued_session",
                wraps=original_terminal,
            ) as terminal, mock.patch(
                "wahojobs.browser_session_lifecycle._generate_credential",
                side_effect=(token, csrf),
            ):
                failed, failed_vault = complete_login(
                    connection,
                    trusted_assertion(created),
                )
            self.assertEqual(failed.status, "unavailable")
            self.assertEqual(finalizer.call_count, 2)
            self.assertEqual(ordinary.call_count, 2)
            self.assertEqual(terminal.call_count, 1)
            self.assertTrue(vault_is_closed_and_empty(failed_vault))
            self.assertTrue(all(value == 0 for value in captured["token"]))
            self.assertTrue(all(value == 0 for value in captured["csrf"]))

            rows = connection.execute("SELECT * FROM account_sessions").fetchall()
            delivered_after = next(
                dict(row)
                for row in rows
                if row["session_id"] == delivered_before["session_id"]
            )
            failed_row = next(
                row for row in rows if row["session_id"] != delivered_before["session_id"]
            )
            self.assertEqual(delivered_after, delivered_before)
            self.assertEqual(failed_row["session_version"], 2)
            self.assertIsNotNone(failed_row["revoked_at"])
            self.assertEqual(failed_row["revoke_reason"], "security_reset")
            self.assertTrue(
                failed_row["creation_idempotency_key"].endswith(".undelivered")
            )

            gateway = DurableBrowserSessionAuthenticationGateway(
                trusted_environment_namespace="test",
                clock=lambda: TRUSTED_NOW,
            )
            with read_only_connection(path) as reader:
                reader.execute("BEGIN")
                try:
                    failed_actor = gateway.authenticate_browser_request(
                        reader,
                        browser_request(token),
                        now=TRUSTED_NOW,
                    )
                    delivered_actor = gateway.authenticate_browser_request(
                        reader,
                        browser_request(delivered_token),
                        now=TRUSTED_NOW,
                    )
                finally:
                    reader.rollback()
            self.assertIsNone(failed_actor)
            self.assertIsNotNone(delivered_actor)

            replay, replay_vault = complete_login(
                connection,
                trusted_assertion(created),
            )
            fresh, fresh_vault = complete_login(
                connection,
                trusted_assertion(created),
                key="trusted-login-after-independent-terminal-recovery",
            )
            self.assertEqual(replay.status, "idempotency_conflict")
            self.assertEqual(vault_entry_count(replay_vault), 0)
            self.assertEqual(fresh.status, "issued")
            close_secret_vault(delivered_vault)
            close_secret_vault(failed_vault)
            close_secret_vault(replay_vault)
            close_secret_vault(fresh_vault)

    def test_lower_false_compensation_success_is_rejected_by_durable_reread(self):
        with login_database(suffix="lower-compensation-false-success") as (
            _path,
            connection,
            created,
        ):
            claims = []

            def false_success(*_args, **_kwargs):
                claims.append("revoked")
                return lifecycle.BrowserSessionMutationResult._issue(
                    lifecycle._RESULT_ISSUANCE_CAPABILITY,
                    "revoked",
                )

            original_terminal = lifecycle._force_compensate_undelivered_issued_session
            with mock.patch(
                "wahojobs.browser_session_lifecycle._finalize_pending_issued_session",
                side_effect=RuntimeError("private-lower-finalization-marker"),
            ), mock.patch(
                "wahojobs.browser_session_lifecycle._compensate_undelivered_issued_session",
                side_effect=false_success,
            ), mock.patch(
                "wahojobs.browser_session_lifecycle._force_compensate_undelivered_issued_session",
                wraps=original_terminal,
            ) as terminal:
                result, vault = complete_login(
                    connection,
                    trusted_assertion(created),
                )
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(claims, ["revoked", "revoked"])
            self.assertEqual(terminal.call_count, 1)
            row = connection.execute("SELECT * FROM account_sessions").fetchone()
            self.assertEqual(row["session_version"], 2)
            self.assertIsNotNone(row["revoked_at"])
            self.assertEqual(row["revoke_reason"], "security_reset")
            self.assertTrue(vault_is_closed_and_empty(vault))
            close_secret_vault(vault)

    def test_result_construction_failure_compensates_finalized_issuance(self):
        with login_database(suffix="result-construction-compensation") as (
            _path,
            connection,
            created,
        ):
            original = TrustedLoginCompletionResult._issue

            def fail_issued(
                capability,
                status,
                issued_session=None,
                request_secret_vault=None,
            ):
                if status == "issued":
                    raise RuntimeError("private-result-construction-marker")
                return original(
                    capability,
                    status,
                    issued_session,
                    request_secret_vault,
                )

            with mock.patch.object(
                TrustedLoginCompletionResult,
                "_issue",
                side_effect=fail_issued,
            ):
                result, vault = complete_login(
                    connection,
                    trusted_assertion(created),
                )
            self.assertEqual(result.status, "unavailable")
            self.assertTrue(vault_is_closed_and_empty(vault))
            row = connection.execute("SELECT * FROM account_sessions").fetchone()
            self.assertEqual(row["session_version"], 2)
            self.assertEqual(row["revoke_reason"], "security_reset")
            self.assertTrue(row["creation_idempotency_key"].endswith(".undelivered"))
            self.assertNotIn("private", repr(result))
            close_secret_vault(vault)

    def test_one_time_vault_cleanup_failure_retries_and_clears_buffers(self):
        with login_database(suffix="vault-cleanup-retry") as (
            _path,
            connection,
            created,
        ):
            vault = request_secret_vault()
            captured = {}

            def fail_after_creation(point):
                if point == "after_session_creation":
                    entry = next(iter(vault._entries.values()))
                    captured["token"] = entry.token_buffer
                    captured["csrf"] = entry.csrf_buffer
                    raise RuntimeError("private-provider-context-marker")

            original = completion.abort_request_scoped_secret_vault
            injected = RuntimeError("private-vault-cleanup-marker")
            calls = []

            def fail_once(*args, **kwargs):
                calls.append("attempt")
                if len(calls) == 1:
                    raise injected
                return original(*args, **kwargs)

            with mock.patch(
                "wahojobs.trusted_login_completion.abort_request_scoped_secret_vault",
                side_effect=fail_once,
            ):
                result, _ = complete_login(
                    connection,
                    trusted_assertion(created),
                    vault=vault,
                    _failure_injector=fail_after_creation,
                )
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(len(calls), 2)
            self.assertIsNone(injected.__traceback__)
            self.assertIsNone(injected.__cause__)
            self.assertIsNone(injected.__context__)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )
            self.assertTrue(vault_is_closed_and_empty(vault))
            self.assertTrue(all(value == 0 for value in captured["token"]))
            self.assertTrue(all(value == 0 for value in captured["csrf"]))
            self.assertNotIn("private", repr(result))
            close_secret_vault(vault)

    def test_both_ordinary_cleanups_fail_emergency_terminalizes_and_zeroizes(self):
        with login_database(suffix="vault-emergency-terminalization") as (
            _path,
            connection,
            created,
        ):
            vault = request_secret_vault()
            captured = {}

            def fail_after_creation(point):
                if point == "after_session_creation":
                    entry = next(iter(vault._entries.values()))
                    captured["token"] = entry.token_buffer
                    captured["csrf"] = entry.csrf_buffer
                    raise RuntimeError("private-provider-context-marker")

            original_emergency = (
                completion.emergency_terminalize_request_scoped_secret_vault
            )
            with mock.patch(
                "wahojobs.trusted_login_completion.abort_request_scoped_secret_vault",
                side_effect=RuntimeError("private-cleanup-marker"),
            ) as cleanup, mock.patch(
                "wahojobs.trusted_login_completion.emergency_terminalize_request_scoped_secret_vault",
                wraps=original_emergency,
            ) as emergency:
                result, _ = complete_login(
                    connection,
                    trusted_assertion(created),
                    vault=vault,
                    _failure_injector=fail_after_creation,
                )
            self.assertEqual(result.status, "unavailable")
            self.assertEqual(cleanup.call_count, 2)
            self.assertEqual(emergency.call_count, 1)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM account_sessions"
                ).fetchone()[0],
                0,
            )
            self.assertTrue(vault_is_closed_and_empty(vault))
            self.assertEqual(vault_entry_count(vault), 0)
            self.assertTrue(all(value == 0 for value in captured["token"]))
            self.assertTrue(all(value == 0 for value in captured["csrf"]))
            original_emergency(
                vault,
                completion._RESPONSE_COMPOSITION_CAPABILITY,
            )
            self.assertTrue(vault_is_closed_and_empty(vault))
            close_secret_vault(vault)

    def test_top_level_completion_issues_one_session_and_one_shot_credentials(self):
        with login_database(suffix="completion-valid") as (_path, connection, created):
            result, vault = complete_login(connection, trusted_assertion(created))
            self.assertEqual(result.status, "issued")
            self.assertIs(type(result), TrustedLoginCompletionResult)
            self.assertEqual(result.issued_session.status, "issued")
            self.assertFalse(connection.in_transaction)
            self.assertEqual(vault_entry_count(vault), 1)
            rows = connection.execute("SELECT * FROM account_sessions").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertNotEqual(
                rows[0]["creation_idempotency_key"],
                "trusted-login-completion-001",
            )
            self.assertRegex(
                rows[0]["creation_idempotency_key"],
                r"^b2d1\.[0-9a-f]{64}\.[0-9a-f]{64}$",
            )
            response = consume_login(result, vault)
            self.assertRegex(token_from_cookie_header(response.set_cookie_header), r"^[A-Za-z0-9_-]{43}$")
            self.assertRegex(response.csrf_credential, r"^[A-Za-z0-9_-]{43}$")
            self.assertEqual(result.issued_session.status, "consumed")
            close_secret_vault(vault)

    def test_result_is_sealed_and_its_public_surface_is_sanitized(self):
        token = base64.urlsafe_b64encode(b"L" * 32).rstrip(b"=").decode("ascii")
        csrf = base64.urlsafe_b64encode(b"C" * 32).rstrip(b"=").decode("ascii")
        with login_database(suffix="completion-result") as (_path, connection, created):
            assertion = trusted_assertion(created)
            with mock.patch(
                "wahojobs.browser_session_lifecycle._generate_credential",
                side_effect=(token, csrf),
            ):
                result, vault = complete_login(connection, assertion)
            self.assertEqual(repr(result), "TrustedLoginCompletionResult(status='issued')")
            self.assertNotIn(token, repr(result))
            self.assertNotIn(csrf, repr(result))
            self.assertFalse(hasattr(result, "__dict__"))
            self.assertNotIn("authority", repr(result).lower())
            self.assertNotIn("vault", repr(result).lower())
            self.assertNotIn("session", repr(result).lower())
            for operation in (
                lambda: TrustedLoginCompletionResult(),
                lambda: copy.copy(result),
                lambda: copy.deepcopy(result),
                lambda: pickle.dumps(result),
                lambda: replace(result),
                lambda: type("ForgedCompletionResult", (TrustedLoginCompletionResult,), {}),
            ):
                with self.assertRaises(TypeError):
                    operation()
            with self.assertRaises(TypeError):
                json.dumps(result)
            with self.assertRaises(AttributeError):
                result.status = "unavailable"
            response = consume_login(result, vault)
            self.assertIn(token, response.set_cookie_header)
            self.assertEqual(response.csrf_credential, csrf)
            close_secret_vault(vault)

    def test_account_and_identity_eligibility_denials_issue_no_command_or_credential(self):
        scenarios = (
            "missing_account",
            "inactive_account",
            "missing_identity",
            "cross_account_identity",
            "provider_mismatch",
            "identity_established_after_authentication",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                with login_database(suffix=f"denial-{scenario}") as (
                    _path,
                    connection,
                    created,
                ):
                    assertion = trusted_assertion(created)
                    if scenario == "missing_account":
                        assertion = trusted_assertion(
                            created,
                            account_id="usr_" + "1" * 32,
                            identity_id="auth_" + "1" * 32,
                        )
                    elif scenario == "inactive_account":
                        connection.execute(
                            "UPDATE users SET lifecycle_status = 'suspended' WHERE user_id = ?",
                            (created.user.user_id,),
                        )
                        connection.commit()
                    elif scenario == "missing_identity":
                        connection.execute(
                            "DELETE FROM auth_identities WHERE auth_identity_id = ?",
                            (created.identity.auth_identity_id,),
                        )
                        connection.commit()
                    elif scenario == "cross_account_identity":
                        _invitation, second = create_user(
                            connection,
                            suffix="cross-login",
                            now=NOW,
                        )
                        assertion = trusted_assertion(
                            created,
                            identity_id=second.identity.auth_identity_id,
                        )
                    elif scenario == "provider_mismatch":
                        object.__setattr__(assertion, "_provider", "private-provider-marker")
                    else:
                        later = NOW + timedelta(seconds=30)
                        connection.execute(
                            "UPDATE auth_identities SET created_at = ?, last_authenticated_at = ? "
                            "WHERE auth_identity_id = ?",
                            (
                                later.isoformat(),
                                later.isoformat(),
                                created.identity.auth_identity_id,
                            ),
                        )
                        connection.commit()
                    with mock.patch(
                        "wahojobs.trusted_login_completion._issue_create_session_command_and_execute",
                        wraps=__import__(
                            "wahojobs.trusted_login_completion",
                            fromlist=["_issue_create_session_command_and_execute"],
                        )._issue_create_session_command_and_execute,
                    ) as issuer, mock.patch(
                        "wahojobs.browser_session_lifecycle._generate_credential"
                    ) as generator:
                        result, vault = complete_login(connection, assertion)
                    self.assertEqual(result.status, "authentication_denied")
                    issuer.assert_not_called()
                    generator.assert_not_called()
                    self.assertEqual(vault_entry_count(vault), 0)
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                        0,
                    )
                    close_secret_vault(vault)

    def test_malformed_account_and_identity_state_are_sanitized_unavailable(self):
        scenarios = ("account", "identity", "identity_time")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                with login_database(suffix=f"malformed-{scenario}") as (
                    _path,
                    connection,
                    created,
                ):
                    connection.execute("PRAGMA ignore_check_constraints = ON")
                    if scenario == "account":
                        connection.execute(
                            "UPDATE users SET row_version = 0 WHERE user_id = ?",
                            (created.user.user_id,),
                        )
                    elif scenario == "identity":
                        connection.execute(
                            "UPDATE auth_identities SET request_fingerprint = 'private-row-marker' "
                            "WHERE auth_identity_id = ?",
                            (created.identity.auth_identity_id,),
                        )
                    else:
                        connection.execute(
                            "UPDATE auth_identities SET last_authenticated_at = ? "
                            "WHERE auth_identity_id = ?",
                            (
                                (NOW - timedelta(minutes=1)).isoformat(),
                                created.identity.auth_identity_id,
                            ),
                        )
                    connection.execute("PRAGMA ignore_check_constraints = OFF")
                    connection.commit()
                    with mock.patch(
                        "wahojobs.browser_session_lifecycle._generate_credential"
                    ) as generator:
                        result, vault = complete_login(
                            connection,
                            trusted_assertion(created),
                        )
                    self.assertEqual(result.status, "unavailable")
                    self.assertNotIn("private", repr(result))
                    generator.assert_not_called()
                    self.assertEqual(vault_entry_count(vault), 0)
                    close_secret_vault(vault)

    def test_environment_coherence_is_derived_only_from_explicit_trusted_composition(self):
        with login_database(suffix="environment-coherence") as (_path, connection, created):
            assertion = trusted_assertion(created, environment_namespace="private_beta")
            with mock.patch(
                "wahojobs.browser_session_lifecycle._generate_credential"
            ) as generator:
                result, vault = complete_login(
                    connection,
                    assertion,
                    policy=completion_policy(environment_namespace="test"),
                )
            self.assertEqual(result.status, "authentication_denied")
            generator.assert_not_called()
            close_secret_vault(vault)

    def test_exact_replay_returns_no_new_session_credential_or_vault_entry(self):
        with login_database(suffix="completion-replay") as (_path, connection, created):
            assertion = trusted_assertion(created)
            first, first_vault = complete_login(connection, assertion)
            first_response = consume_login(first, first_vault)
            token = token_from_cookie_header(first_response.set_cookie_header)
            csrf = first_response.csrf_credential
            replay_vault = request_secret_vault()
            with mock.patch(
                "wahojobs.browser_session_lifecycle._generate_credential"
            ) as generator:
                replay, _ = complete_login(
                    connection,
                    assertion,
                    vault=replay_vault,
                )
            self.assertEqual(replay.status, "already_completed")
            generator.assert_not_called()
            self.assertEqual(vault_entry_count(replay_vault), 0)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                1,
            )
            self.assertNotIn(token, repr(replay))
            self.assertNotIn(csrf, repr(replay))
            with self.assertRaises(BrowserSessionLifecycleError):
                consume_login(replay, replay_vault)
            close_secret_vault(first_vault)
            close_secret_vault(replay_vault)

    def test_changed_assertion_policy_and_account_conflict_under_same_key(self):
        scenarios = ("assertion", "policy", "account")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                with login_database(suffix=f"conflict-{scenario}") as (
                    _path,
                    connection,
                    created,
                ):
                    first, first_vault = complete_login(
                        connection,
                        trusted_assertion(created),
                    )
                    self.assertEqual(first.status, "issued")
                    if scenario == "assertion":
                        changed_assertion = trusted_assertion(
                            created,
                            expires_at=NOW + timedelta(minutes=6),
                        )
                        changed_policy = completion_policy()
                    elif scenario == "policy":
                        changed_assertion = trusted_assertion(created)
                        changed_policy = completion_policy(
                            idle_ttl=timedelta(hours=2)
                        )
                    else:
                        _invitation, second = create_user(
                            connection,
                            suffix="conflict-other-account",
                            now=NOW,
                        )
                        changed_assertion = trusted_assertion(second)
                        changed_policy = completion_policy()
                    with mock.patch(
                        "wahojobs.trusted_login_completion._issue_create_session_command_and_execute"
                    ) as issuer, mock.patch(
                        "wahojobs.browser_session_lifecycle._generate_credential"
                    ) as generator:
                        conflict, conflict_vault = complete_login(
                            connection,
                            changed_assertion,
                            policy=changed_policy,
                        )
                    self.assertEqual(conflict.status, "idempotency_conflict")
                    issuer.assert_not_called()
                    generator.assert_not_called()
                    self.assertEqual(vault_entry_count(conflict_vault), 0)
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                        1,
                    )
                    close_secret_vault(first_vault)
                    close_secret_vault(conflict_vault)

    def test_replay_after_rotation_and_revocation_stays_credential_free(self):
        for operation in ("rotate", "revoke"):
            with self.subTest(operation=operation):
                with login_database(suffix=f"replay-after-{operation}") as (
                    _path,
                    connection,
                    created,
                ):
                    assertion = trusted_assertion(created)
                    first, first_vault = complete_login(connection, assertion)
                    session = connection.execute("SELECT * FROM account_sessions").fetchone()
                    if operation == "rotate":
                        mutation_vault = request_secret_vault()
                        rotated = rotate_browser_session(
                            connection,
                            rotate_command(
                                created.user.user_id,
                                session["session_id"],
                                accepted_at=NOW + timedelta(minutes=2),
                            ),
                            secret_vault=mutation_vault,
                            _clock=lambda: NOW + timedelta(minutes=2),
                        )
                        consume_issued(
                            rotated,
                            vault=mutation_vault,
                            now=NOW + timedelta(minutes=2),
                        )
                        close_secret_vault(mutation_vault)
                    else:
                        revoke_browser_session(
                            connection,
                            revoke_command(
                                created.user.user_id,
                                session["session_id"],
                                accepted_at=NOW + timedelta(minutes=2),
                            ),
                            _clock=lambda: NOW + timedelta(minutes=2),
                        )
                    replay, replay_vault = complete_login(
                        connection,
                        assertion,
                        trusted_now=NOW + timedelta(minutes=3),
                    )
                    self.assertEqual(replay.status, "already_completed")
                    self.assertEqual(vault_entry_count(replay_vault), 0)
                    close_secret_vault(first_vault)
                    close_secret_vault(replay_vault)

    def test_caller_transaction_commit_and_finalization_enable_consumption(self):
        with login_database(suffix="completion-nested-commit") as (
            _path,
            connection,
            created,
        ):
            connection.execute(
                "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                ("Caller", "trusted-login-caller", "https://example.test/careers"),
            )
            result, vault = complete_login(connection, trusted_assertion(created))
            self.assertTrue(connection.in_transaction)
            self.assertEqual(result.status, "pending_commit")
            self.assertEqual(vault_entry_count(vault), 1)
            with self.assertRaises(BrowserSessionLifecycleError):
                consume_login(result, vault)
            connection.commit()
            finalized = finalize_login(connection, result, vault)
            self.assertEqual(finalized.status, "issued")
            consume_login(finalized, vault)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM companies WHERE slug = 'trusted-login-caller'"
                ).fetchone()[0],
                1,
            )
            close_secret_vault(vault)

    def test_nested_post_commit_finalization_retries_once(self):
        with login_database(suffix="nested-finalization-retry") as (
            _path,
            connection,
            created,
        ):
            connection.execute(
                "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                ("Caller", "nested-finalization-retry", "https://example.test/careers"),
            )
            pending, vault = complete_login(connection, trusted_assertion(created))
            self.assertEqual(pending.status, "pending_commit")
            connection.commit()
            original = completion.finalize_pending_issued_session
            calls = []

            def fail_once(*args, **kwargs):
                calls.append("attempt")
                if len(calls) == 1:
                    raise sqlite3.OperationalError("private-finalization-marker")
                return original(*args, **kwargs)

            with mock.patch(
                "wahojobs.trusted_login_completion.finalize_pending_issued_session",
                side_effect=fail_once,
            ):
                finalized = finalize_login(connection, pending, vault)
            self.assertEqual(finalized.status, "issued")
            self.assertEqual(len(calls), 2)
            self.assertEqual(vault_entry_count(vault), 1)
            consume_login(finalized, vault)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM companies "
                    "WHERE slug = 'nested-finalization-retry'"
                ).fetchone()[0],
                1,
            )
            close_secret_vault(vault)

    def test_permanent_nested_finalization_failure_is_compensated(self):
        with login_database(suffix="nested-finalization-compensation") as (
            _path,
            connection,
            created,
        ):
            connection.execute("BEGIN")
            pending, vault = complete_login(connection, trusted_assertion(created))
            self.assertEqual(pending.status, "pending_commit")
            connection.commit()
            with mock.patch(
                "wahojobs.trusted_login_completion.finalize_pending_issued_session",
                side_effect=RuntimeError("private-nested-finalization-marker"),
            ) as finalize_call:
                failed = finalize_login(connection, pending, vault)
            self.assertEqual(failed.status, "unavailable")
            self.assertEqual(finalize_call.call_count, 2)
            self.assertTrue(vault_is_closed_and_empty(vault))
            row = connection.execute("SELECT * FROM account_sessions").fetchone()
            self.assertEqual(row["session_version"], 2)
            self.assertEqual(row["revoke_reason"], "security_reset")
            self.assertTrue(row["creation_idempotency_key"].endswith(".undelivered"))
            retry, retry_vault = complete_login(
                connection,
                trusted_assertion(created),
            )
            self.assertEqual(retry.status, "idempotency_conflict")
            self.assertEqual(vault_entry_count(retry_vault), 0)
            close_secret_vault(vault)
            close_secret_vault(retry_vault)

    def test_nested_permanent_failure_uses_verified_emergency_compensation(self):
        with login_database(suffix="nested-compensation-emergency") as (
            _path,
            connection,
            created,
        ):
            connection.execute(
                "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                (
                    "Caller",
                    "nested-compensation-emergency",
                    "https://example.test/careers",
                ),
            )
            pending, vault = complete_login(
                connection,
                trusted_assertion(created),
            )
            self.assertEqual(pending.status, "pending_commit")
            connection.commit()
            original_force = completion.force_compensate_undelivered_issued_session
            with mock.patch(
                "wahojobs.trusted_login_completion.finalize_pending_issued_session",
                side_effect=RuntimeError("private-finalization-marker"),
            ) as finalizer, mock.patch(
                "wahojobs.trusted_login_completion.compensate_undelivered_issued_session",
                side_effect=RuntimeError("private-compensation-marker"),
            ) as compensator, mock.patch(
                "wahojobs.trusted_login_completion.force_compensate_undelivered_issued_session",
                wraps=original_force,
            ) as forced:
                failed = finalize_login(connection, pending, vault)
            self.assertEqual(failed.status, "unavailable")
            self.assertEqual(finalizer.call_count, 2)
            self.assertEqual(compensator.call_count, 2)
            self.assertEqual(forced.call_count, 1)
            self.assertTrue(vault_is_closed_and_empty(vault))
            row = connection.execute("SELECT * FROM account_sessions").fetchone()
            self.assertEqual(row["session_version"], 2)
            self.assertEqual(row["revoke_reason"], "security_reset")
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM companies "
                    "WHERE slug = 'nested-compensation-emergency'"
                ).fetchone()[0],
                1,
            )
            close_secret_vault(vault)

    def test_nested_lower_control_flow_failure_is_safe_before_exact_propagation(self):
        with login_database(suffix="nested-lower-control-flow") as (
            _path,
            connection,
            created,
        ):
            connection.execute(
                "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                (
                    "Caller",
                    "nested-lower-control-flow",
                    "https://example.test/careers",
                ),
            )
            pending, vault = complete_login(
                connection,
                trusted_assertion(created),
            )
            entry = next(iter(vault._entries.values()))
            token_buffer = entry.token_buffer
            csrf_buffer = entry.csrf_buffer
            connection.commit()
            injected = SystemExit("private-nested-lower-control-marker")
            with mock.patch(
                "wahojobs.browser_session_lifecycle._finalize_pending_issued_session",
                side_effect=injected,
            ) as lower_finalizer:
                with self.assertRaises(SystemExit) as raised:
                    finalize_login(connection, pending, vault)
            self.assertIs(raised.exception, injected)
            self.assertEqual(lower_finalizer.call_count, 1)
            self.assertTrue(vault_is_closed_and_empty(vault))
            self.assertTrue(all(value == 0 for value in token_buffer))
            self.assertTrue(all(value == 0 for value in csrf_buffer))
            row = connection.execute("SELECT * FROM account_sessions").fetchone()
            self.assertEqual(row["session_version"], 2)
            self.assertIsNotNone(row["revoked_at"])
            self.assertEqual(row["revoke_reason"], "security_reset")
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM companies "
                    "WHERE slug = 'nested-lower-control-flow'"
                ).fetchone()[0],
                1,
            )
            close_secret_vault(vault)

    def test_caller_rollback_then_finalization_discards_pending_vault_state(self):
        with login_database(suffix="completion-nested-rollback") as (
            _path,
            connection,
            created,
        ):
            connection.execute(
                "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                ("Caller", "trusted-login-rollback", "https://example.test/careers"),
            )
            result, vault = complete_login(connection, trusted_assertion(created))
            self.assertEqual(result.status, "pending_commit")
            connection.rollback()
            finalized = finalize_login(connection, result, vault)
            self.assertEqual(finalized.status, "unavailable")
            self.assertEqual(vault_entry_count(vault), 0)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )
            close_secret_vault(vault)

    def test_caller_rollback_cleanup_failure_once_retries_to_terminal_vault(self):
        with login_database(suffix="nested-rollback-cleanup-retry") as (
            _path,
            connection,
            created,
        ):
            connection.execute(
                "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                ("Caller", "nested-cleanup-retry", "https://example.test/careers"),
            )
            pending, vault = complete_login(connection, trusted_assertion(created))
            self.assertEqual(pending.status, "pending_commit")
            connection.rollback()
            original = completion.abort_request_scoped_secret_vault
            calls = []

            def fail_once(*args, **kwargs):
                calls.append("attempt")
                if len(calls) == 1:
                    raise RuntimeError("private-rollback-cleanup-marker")
                return original(*args, **kwargs)

            with mock.patch(
                "wahojobs.trusted_login_completion.abort_request_scoped_secret_vault",
                side_effect=fail_once,
            ):
                finalized = finalize_login(connection, pending, vault)
            self.assertEqual(finalized.status, "unavailable")
            self.assertEqual(len(calls), 2)
            self.assertTrue(vault_is_closed_and_empty(vault))
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )
            close_secret_vault(vault)

    def test_caller_rollback_uses_emergency_vault_terminalization(self):
        with login_database(suffix="nested-rollback-emergency-cleanup") as (
            _path,
            connection,
            created,
        ):
            connection.execute(
                "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                (
                    "Caller",
                    "nested-rollback-emergency-cleanup",
                    "https://example.test/careers",
                ),
            )
            pending, vault = complete_login(
                connection,
                trusted_assertion(created),
            )
            entry = next(iter(vault._entries.values()))
            token_buffer = entry.token_buffer
            csrf_buffer = entry.csrf_buffer
            connection.rollback()
            original_emergency = (
                completion.emergency_terminalize_request_scoped_secret_vault
            )
            with mock.patch(
                "wahojobs.trusted_login_completion.abort_request_scoped_secret_vault",
                side_effect=RuntimeError("private-cleanup-marker"),
            ) as cleanup, mock.patch(
                "wahojobs.trusted_login_completion.emergency_terminalize_request_scoped_secret_vault",
                wraps=original_emergency,
            ) as emergency:
                failed = finalize_login(connection, pending, vault)
            self.assertEqual(failed.status, "unavailable")
            self.assertEqual(cleanup.call_count, 2)
            self.assertEqual(emergency.call_count, 1)
            self.assertTrue(vault_is_closed_and_empty(vault))
            self.assertTrue(all(value == 0 for value in token_buffer))
            self.assertTrue(all(value == 0 for value in csrf_buffer))
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM account_sessions"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM companies "
                    "WHERE slug = 'nested-rollback-emergency-cleanup'"
                ).fetchone()[0],
                0,
            )
            close_secret_vault(vault)

    def test_failure_after_nested_session_creation_rolls_back_only_service_scope(self):
        with login_database(suffix="completion-nested-failure") as (
            _path,
            connection,
            created,
        ):
            connection.execute(
                "INSERT INTO companies(name, slug, careers_url) VALUES (?, ?, ?)",
                ("Caller", "trusted-login-preserved", "https://example.test/careers"),
            )

            def fail(point):
                if point == "after_session_creation":
                    raise RuntimeError("private failure marker")

            result, vault = complete_login(
                connection,
                trusted_assertion(created),
                _failure_injector=fail,
            )
            self.assertEqual(result.status, "unavailable")
            self.assertTrue(connection.in_transaction)
            self.assertEqual(vault_entry_count(vault), 0)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM companies WHERE slug = 'trusted-login-preserved'"
                ).fetchone()[0],
                1,
            )
            connection.commit()
            close_secret_vault(vault)

    def test_schema_failure_lock_contention_and_injected_exception_are_private(self):
        with login_database(suffix="completion-failures") as (path, connection, created):
            assertion = trusted_assertion(created)
            connection.execute(
                "DELETE FROM wahojobs_schema_migrations WHERE version = '002_accounts_sessions'"
            )
            connection.commit()
            result, vault = complete_login(connection, assertion)
            self.assertEqual(result.status, "unavailable")
            self.assertNotIn(str(path), repr(result))
            close_secret_vault(vault)

        with login_database(suffix="completion-injected") as (_path, connection, created):
            marker = "private-id-email@example.test-token-marker"

            def fail(_point):
                raise RuntimeError(marker)

            result, vault = complete_login(
                connection,
                trusted_assertion(created),
                _failure_injector=fail,
            )
            self.assertEqual(result.status, "unavailable")
            self.assertNotIn(marker, repr(result))
            self.assertNotIn(created.user.user_id, repr(result))
            close_secret_vault(vault)

    def test_service_opens_no_auxiliary_connection_and_leaves_connection_open(self):
        with login_database(suffix="completion-no-aux") as (_path, connection, created):
            with mock.patch(
                "wahojobs.trusted_login_completion.sqlite3.connect",
                side_effect=AssertionError("auxiliary connection forbidden"),
            ) as connect_call:
                result, vault = complete_login(connection, trusted_assertion(created))
            self.assertEqual(result.status, "issued")
            connect_call.assert_not_called()
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
            close_secret_vault(vault)

    def test_temporary_database_integrity_foreign_keys_and_sidecars_remain_clean(self):
        with login_database(suffix="completion-integrity") as (path, connection, created):
            result, vault = complete_login(connection, trusted_assertion(created))
            self.assertEqual(result.status, "issued")
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse(path.with_name(path.name + suffix).exists())
            close_secret_vault(vault)


class TrustedLoginRuntimeDeliveryTests(unittest.TestCase):
    def _complete_with_runtime_composition(
        self,
        connection,
        created,
        *,
        key,
    ):
        policy = completion.create_trusted_login_completion_policy(
            environment_namespace="test",
            idle_ttl=timedelta(hours=1),
            absolute_ttl=timedelta(days=7),
        )
        vault = lifecycle.create_request_scoped_session_secret_vault()
        result = completion.complete_trusted_login(
            connection,
            trusted_assertion(created),
            policy,
            vault,
            trusted_now=TRUSTED_NOW,
            idempotency_key=key,
        )
        self.assertEqual(result.status, "issued")
        return policy, vault, result

    def test_runtime_policy_factory_fixes_google_authority_and_validates_lifetime(self):
        policy = completion.create_trusted_login_completion_policy(
            environment_namespace="test",
            idle_ttl=timedelta(hours=1),
            absolute_ttl=timedelta(days=7),
        )
        values = policy._values_for_service(
            completion._COMPLETION_POLICY_SERVICE_CAPABILITY
        )
        self.assertEqual(values["expected_provider"], "google")
        self.assertEqual(
            values["expected_assurance_policy_version"],
            "google_oidc_v1",
        )
        self.assertEqual(
            values["completion_policy_version"],
            "trusted_login_completion_v1",
        )
        self.assertEqual(values["environment_namespace"], "test")
        self.assertEqual(values["idle_ttl"], timedelta(hours=1))
        self.assertEqual(values["absolute_ttl"], timedelta(days=7))
        self.assertEqual(
            repr(policy),
            "TrustedLoginCompletionPolicy(<configured>)",
        )

        for arguments in (
            {
                "environment_namespace": "production",
                "idle_ttl": timedelta(hours=1),
                "absolute_ttl": timedelta(days=7),
            },
            {
                "environment_namespace": "test",
                "idle_ttl": timedelta(days=2),
                "absolute_ttl": timedelta(days=1),
            },
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(TypeError) as caught:
                    completion.create_trusted_login_completion_policy(
                        **arguments
                    )
                self.assertEqual(
                    str(caught.exception),
                    "trusted_login_completion_configuration_invalid",
                )

    def test_runtime_completion_prepares_and_acknowledges_one_delivery(self):
        with login_database(suffix="runtime-delivery-ack") as (
            _path,
            connection,
            created,
        ):
            _policy, vault, result = (
                self._complete_with_runtime_composition(
                    connection,
                    created,
                    key="runtime-delivery-ack-001",
                )
            )
            lease = completion.prepare_session_delivery(
                connection,
                result,
                vault,
                now=TRUSTED_NOW,
            )
            self.assertEqual(lease.status, "prepared")
            self.assertEqual(
                result.issued_session.status,
                "delivery_pending",
            )
            self.assertRegex(
                lease.set_cookie_header,
                r"^wahojobs_session=[A-Za-z0-9_-]{43}; ",
            )
            self.assertRegex(
                lease.csrf_credential,
                r"^[A-Za-z0-9_-]{43}$",
            )

            lease.acknowledge_delivery()

            self.assertEqual(lease.status, "acknowledged")
            self.assertEqual(result.issued_session.status, "consumed")
            self.assertTrue(vault_is_closed_and_empty(vault))
            stored = connection.execute(
                "SELECT * FROM account_sessions"
            ).fetchone()
            self.assertEqual(stored["session_version"], 1)
            self.assertIsNone(stored["revoked_at"])
            with self.assertRaises(
                BrowserSessionLifecycleError
            ) as repeated:
                completion.prepare_session_delivery(
                    connection,
                    result,
                    vault,
                    now=TRUSTED_NOW,
                )
            self.assertEqual(
                repeated.exception.code,
                "already_completed",
            )

    def test_baseexception_before_header_acceptance_fails_exact_delivery(self):
        with login_database(suffix="runtime-delivery-control-flow") as (
            _path,
            connection,
            created,
        ):
            _policy, vault, result = (
                self._complete_with_runtime_composition(
                    connection,
                    created,
                    key="runtime-delivery-control-flow-001",
                )
            )
            lease = completion.prepare_session_delivery(
                connection,
                result,
                vault,
                now=TRUSTED_NOW,
            )
            stored_before = dict(
                connection.execute(
                    "SELECT * FROM account_sessions"
                ).fetchone()
            )

            def interrupted_delivery():
                try:
                    raise GeneratorExit(
                        "simulated end_headers interruption"
                    )
                except BaseException:
                    lease.fail_delivery()
                    raise

            with self.assertRaises(GeneratorExit) as caught:
                interrupted_delivery()

            self.assertEqual(
                str(caught.exception),
                "simulated end_headers interruption",
            )
            self.assertEqual(lease.status, "failed")
            self.assertEqual(
                result.issued_session.status,
                "terminal_failed",
            )
            self.assertTrue(vault_is_closed_and_empty(vault))
            stored_after = connection.execute(
                "SELECT * FROM account_sessions WHERE session_id = ?",
                (stored_before["session_id"],),
            ).fetchone()
            self.assertEqual(stored_after["session_version"], 2)
            self.assertEqual(
                stored_after["revoke_reason"],
                "security_reset",
            )


class TrustedLoginCompletionResultAuthorityTests(unittest.TestCase):
    @staticmethod
    def _copy_slots(value, expected_type):
        copied = object.__new__(expected_type)
        for slot in expected_type.__slots__:
            if slot == "__weakref__":
                continue
            try:
                item = getattr(value, slot)
            except AttributeError:
                continue
            object.__setattr__(copied, slot, item)
        return copied

    def _complete(self, connection, created, key):
        result, vault = complete_login(
            connection,
            trusted_assertion(created),
            key=key,
        )
        self.assertEqual(result.status, "issued")
        self.assertEqual(result.issued_session.status, "issued")
        self.assertEqual(vault_entry_count(vault), 1)
        return result, vault

    def _abandon(self, connection, result, vault):
        lease = completion.prepare_session_delivery(
            connection,
            result,
            vault,
            now=TRUSTED_NOW,
        )
        self.assertEqual(lease.status, "prepared")
        lease.fail_delivery()
        self.assertEqual(lease.status, "failed")
        self.assertEqual(result.issued_session.status, "terminal_failed")
        self.assertTrue(vault_is_closed_and_empty(vault))

    def test_no_post_completion_result_registry_or_public_identity_api_exists(self):
        source = inspect.getsource(completion)
        self.assertNotIn("_ISSUED_RESULTS", source)
        self.assertFalse(hasattr(completion, "_ISSUED_RESULTS"))
        mutable_registry_types = (
            dict,
            list,
            set,
            weakref.WeakKeyDictionary,
            weakref.WeakSet,
        )
        result_registries = [
            name
            for name, value in vars(completion).items()
            if "result" in name.casefold()
            and isinstance(value, mutable_registry_types)
        ]
        self.assertEqual(result_registries, [])
        public_identity_apis = [
            name
            for name, value in vars(completion).items()
            if not name.startswith("_")
            and callable(value)
            and "result" in name.casefold()
            and any(
                term in name.casefold()
                for term in ("enumerate", "lookup", "registry", "search")
            )
        ]
        self.assertEqual(public_identity_apis, [])
        self.assertNotIn("__del__", TrustedLoginCompletionResult.__dict__)
        self.assertNotIn(
            "__del__",
            completion._TrustedLoginCompletionAuthority.__dict__,
        )
        self.assertNotIn(
            "__del__",
            completion._TrustedLoginCompletionAuthoritySeal.__dict__,
        )
        self.assertNotIn("weakref.finalize", source)

        self.assertIsInstance(
            completion._ISSUED_ASSERTIONS,
            weakref.WeakKeyDictionary,
        )
        self.assertIsInstance(
            completion._ISSUED_COMPLETION_POLICIES,
            weakref.WeakKeyDictionary,
        )
        self.assertIsInstance(completion._VALIDATED_LOGINS, weakref.WeakSet)
        self.assertIsInstance(lifecycle._ISSUED_COMMANDS, weakref.WeakSet)
        post_completion_source = (
            inspect.getsource(completion.prepare_session_delivery)
            + inspect.getsource(completion.finalize_pending_trusted_login)
            + inspect.getsource(TrustedLoginCompletionResult)
        )
        for accepted_registry in (
            "_ISSUED_ASSERTIONS",
            "_ISSUED_COMPLETION_POLICIES",
            "_VALIDATED_LOGINS",
            "_ISSUED_COMMANDS",
        ):
            self.assertNotIn(accepted_registry, post_completion_source)

    def test_source_has_no_prohibited_broad_exception_handlers(self):
        from scripts import durable_google_login_app

        violations = []
        for module in (completion, durable_google_login_app):
            tree = ast.parse(inspect.getsource(module))
            aliases = {"BaseException"}
            for node in ast.walk(tree):
                if (
                    isinstance(node, (ast.Assign, ast.AnnAssign))
                    and isinstance(
                        getattr(node, "value", None),
                        ast.Name,
                    )
                    and node.value.id in aliases
                ):
                    targets = (
                        node.targets
                        if isinstance(node, ast.Assign)
                        else (node.target,)
                    )
                    aliases.update(
                        target.id
                        for target in targets
                        if isinstance(target, ast.Name)
                    )

            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                if node.type is None:
                    violations.append((module.__name__, node.lineno))
                    continue
                candidates = (
                    node.type.elts
                    if isinstance(node.type, ast.Tuple)
                    else (node.type,)
                )
                if any(
                    isinstance(candidate, ast.Name)
                    and candidate.id in aliases
                    for candidate in candidates
                ):
                    violations.append((module.__name__, node.lineno))
        self.assertEqual(violations, [])

    def test_result_repr_preserves_named_control_flow_identity(self):
        result = object.__new__(TrustedLoginCompletionResult)

        def ordinary_status(_self):
            raise RuntimeError("ordinary_status_failure")

        with mock.patch.object(
            TrustedLoginCompletionResult,
            "status",
            new=property(ordinary_status),
        ):
            self.assertEqual(
                repr(result),
                "TrustedLoginCompletionResult(status='invalid')",
            )

        for exception_type in (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(exception_type=exception_type.__name__):
                injected = exception_type(
                    "PRIVATE_RESULT_REPR_CONTROL_CANARY"
                )

                def control_status(_self, injected=injected):
                    raise injected

                with mock.patch.object(
                    TrustedLoginCompletionResult,
                    "status",
                    new=property(control_status),
                ):
                    with self.assertRaises(exception_type) as caught:
                        repr(result)
                self.assertIs(caught.exception, injected)

    def test_prepare_delivery_preserves_named_control_flow_identity(self):
        result = object.__new__(TrustedLoginCompletionResult)
        ordinary = RuntimeError("PRIVATE_PREPARE_DELIVERY_FAILURE")
        with mock.patch.object(
            TrustedLoginCompletionResult,
            "_prepare_delivery",
            side_effect=ordinary,
        ):
            with self.assertRaises(
                BrowserSessionLifecycleError
            ) as caught:
                completion.prepare_session_delivery(
                    None,
                    result,
                    None,
                    now=TRUSTED_NOW,
                )
        self.assertEqual(
            caught.exception.code,
            "internal_consistency_failure",
        )

        for exception_type in (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            with self.subTest(exception_type=exception_type.__name__):
                injected = exception_type(
                    "PRIVATE_PREPARE_DELIVERY_CONTROL_CANARY"
                )
                with mock.patch.object(
                    TrustedLoginCompletionResult,
                    "_prepare_delivery",
                    side_effect=injected,
                ):
                    with self.assertRaises(exception_type) as caught:
                        completion.prepare_session_delivery(
                            None,
                            result,
                            None,
                            now=TRUSTED_NOW,
                        )
                self.assertIs(caught.exception, injected)

    def test_copied_slots_and_transferred_authorities_cannot_mutate_real_owners(self):
        with login_database(suffix="result-authority-forgery") as (
            _path,
            connection,
            created,
        ):
            first, first_vault = self._complete(
                connection,
                created,
                "result-authority-forgery-001",
            )
            second, second_vault = self._complete(
                connection,
                created,
                "result-authority-forgery-002",
            )
            stored_before = [
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM account_sessions ORDER BY session_id"
                ).fetchall()
            ]

            copied_result = self._copy_slots(
                first,
                TrustedLoginCompletionResult,
            )
            copied_session = self._copy_slots(
                first.issued_session,
                lifecycle.IssuedBrowserSession,
            )
            session_forgery = self._copy_slots(
                first,
                TrustedLoginCompletionResult,
            )
            object.__setattr__(
                session_forgery,
                "_issued_session",
                copied_session,
            )
            result_authority_transfer = self._copy_slots(
                first,
                TrustedLoginCompletionResult,
            )
            object.__setattr__(
                result_authority_transfer,
                "_authority",
                second._authority,
            )
            session_authority_transfer = self._copy_slots(
                first,
                TrustedLoginCompletionResult,
            )
            object.__setattr__(
                session_authority_transfer,
                "_issued_session",
                second.issued_session,
            )
            authority_type = type(first._authority)
            copied_authority = self._copy_slots(
                first._authority,
                authority_type,
            )
            copied_authority_forgery = self._copy_slots(
                first,
                TrustedLoginCompletionResult,
            )
            object.__setattr__(
                copied_authority_forgery,
                "_authority",
                copied_authority,
            )

            for label, forged, vault in (
                ("copied_result", copied_result, first_vault),
                ("copied_session", session_forgery, first_vault),
                (
                    "result_authority_transfer",
                    result_authority_transfer,
                    first_vault,
                ),
                (
                    "session_authority_transfer",
                    session_authority_transfer,
                    first_vault,
                ),
                (
                    "copied_authority",
                    copied_authority_forgery,
                    first_vault,
                ),
            ):
                with self.subTest(label=label):
                    with self.assertRaises(
                        BrowserSessionLifecycleError
                    ) as rejected:
                        completion.prepare_session_delivery(
                            connection,
                            forged,
                            vault,
                            now=TRUSTED_NOW,
                        )
                    self.assertEqual(
                        rejected.exception.code,
                        "session_state_conflict",
                    )
                    self.assertIsNone(rejected.exception.__cause__)
                    self.assertIsNone(rejected.exception.__context__)

            with self.assertRaises(
                BrowserSessionLifecycleError
            ) as wrong_vault:
                completion.prepare_session_delivery(
                    connection,
                    first,
                    second_vault,
                    now=TRUSTED_NOW,
                )
            self.assertEqual(
                wrong_vault.exception.code,
                "session_state_conflict",
            )
            self.assertEqual(first.status, "issued")
            self.assertEqual(first.issued_session.status, "issued")
            self.assertEqual(second.status, "issued")
            self.assertEqual(second.issued_session.status, "issued")
            self.assertEqual(vault_entry_count(first_vault), 1)
            self.assertEqual(vault_entry_count(second_vault), 1)
            self.assertEqual(
                [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT * FROM account_sessions ORDER BY session_id"
                    ).fetchall()
                ],
                stored_before,
            )

            self._abandon(connection, second, second_vault)
            self.assertEqual(first.issued_session.status, "issued")
            self.assertEqual(vault_entry_count(first_vault), 1)
            self._abandon(connection, first, first_vault)

    def test_multiple_results_remain_independent_under_arbitrary_terminal_order(self):
        with login_database(suffix="result-authority-order") as (
            _path,
            connection,
            created,
        ):
            issued = [
                self._complete(
                    connection,
                    created,
                    f"result-authority-order-00{index}",
                )
                for index in range(1, 4)
            ]
            leases = [
                completion.prepare_session_delivery(
                    connection,
                    result,
                    vault,
                    now=TRUSTED_NOW,
                )
                for result, vault in issued
            ]
            for (result, _vault), lease in zip(issued, leases):
                self.assertEqual(lease.status, "prepared")
                self.assertEqual(
                    result.issued_session.status,
                    "delivery_pending",
                )

            leases[1].fail_delivery()
            self.assertEqual(leases[1].status, "failed")
            self.assertEqual(leases[0].status, "prepared")
            self.assertEqual(leases[2].status, "prepared")
            leases[0].acknowledge_delivery()
            self.assertEqual(leases[0].status, "acknowledged")
            self.assertEqual(leases[2].status, "prepared")

            with self.assertRaises(
                BrowserSessionLifecycleError
            ) as acknowledge_then_compensate:
                leases[0].fail_delivery()
            self.assertEqual(
                acknowledge_then_compensate.exception.code,
                "already_completed",
            )
            with self.assertRaises(
                BrowserSessionLifecycleError
            ) as compensate_then_acknowledge:
                leases[1].acknowledge_delivery()
            self.assertEqual(
                compensate_then_acknowledge.exception.code,
                "already_completed",
            )

            with self.assertRaises(
                BrowserSessionLifecycleError
            ) as repeated_acquisition:
                completion.prepare_session_delivery(
                    connection,
                    issued[0][0],
                    issued[0][1],
                    now=TRUSTED_NOW,
                )
            self.assertEqual(
                repeated_acquisition.exception.code,
                "already_completed",
            )
            self.assertEqual(leases[2].status, "prepared")
            self.assertEqual(
                issued[2][0].issued_session.status,
                "delivery_pending",
            )
            leases[2].fail_delivery()

            stored = connection.execute(
                "SELECT session_version, revoked_at FROM account_sessions"
            ).fetchall()
            self.assertEqual(len(stored), 3)
            self.assertEqual(
                sum(
                    row["session_version"] == 1
                    and row["revoked_at"] is None
                    for row in stored
                ),
                1,
            )
            self.assertEqual(
                sum(
                    row["session_version"] == 2
                    and row["revoked_at"] is not None
                    for row in stored
                ),
                2,
            )

    def test_deletion_and_forced_gc_have_no_authority_or_cleanup_side_effect(self):
        with login_database(suffix="result-authority-gc") as (
            _path,
            connection,
            created,
        ):
            deleted, deleted_vault = self._complete(
                connection,
                created,
                "result-authority-gc-001",
            )
            deleted_session = deleted.issued_session
            deleted_reference = weakref.ref(deleted)
            del deleted
            for _attempt in range(3):
                gc.collect()
            self.assertIsNone(deleted_reference())
            self.assertEqual(deleted_session.status, "issued")
            self.assertEqual(vault_entry_count(deleted_vault), 1)
            stored = connection.execute(
                "SELECT session_version, revoked_at FROM account_sessions"
            ).fetchone()
            self.assertEqual(stored["session_version"], 1)
            self.assertIsNone(stored["revoked_at"])

            other, other_vault = self._complete(
                connection,
                created,
                "result-authority-gc-002",
            )
            self._abandon(connection, other, other_vault)
            self.assertEqual(deleted_session.status, "issued")
            self.assertEqual(vault_entry_count(deleted_vault), 1)

            deleted_lease = lifecycle.prepare_issued_session_delivery(
                connection,
                deleted_session,
                deleted_vault,
                lifecycle._RESPONSE_COMPOSITION_CAPABILITY,
                now=TRUSTED_NOW,
            )
            deleted_lease.fail_delivery()
            self.assertEqual(deleted_session.status, "terminal_failed")
            self.assertTrue(vault_is_closed_and_empty(deleted_vault))

    def test_reconstructed_composition_uses_an_independent_connection_without_state(self):
        with login_database(suffix="result-authority-reconstruction") as (
            path,
            connection,
            created,
        ):
            result, vault = self._complete(
                connection,
                created,
                "result-authority-reconstruction-001",
            )
            reconstructed_prepare = getattr(
                __import__(
                    "wahojobs.trusted_login_completion",
                    fromlist=("prepare_session_delivery",),
                ),
                "prepare_session_delivery",
            )
            independent = connect(path)
            try:
                lease = reconstructed_prepare(
                    independent,
                    result,
                    vault,
                    now=TRUSTED_NOW,
                )
                lease.acknowledge_delivery()
                self.assertEqual(result.issued_session.status, "consumed")
                stored = independent.execute(
                    "SELECT session_version, revoked_at FROM account_sessions"
                ).fetchone()
                self.assertEqual(stored["session_version"], 1)
                self.assertIsNone(stored["revoked_at"])
            finally:
                independent.close()

    def test_pending_result_finalization_is_owner_bound_and_one_shot(self):
        with login_database(suffix="result-authority-pending") as (
            _path,
            connection,
            created,
        ):
            connection.execute("BEGIN")
            pending, vault = complete_login(
                connection,
                trusted_assertion(created),
                key="result-authority-pending-001",
            )
            self.assertEqual(pending.status, "pending_commit")
            self.assertEqual(
                pending.issued_session.status,
                "pending_commit",
            )
            connection.commit()

            forged = self._copy_slots(
                pending,
                TrustedLoginCompletionResult,
            )
            rejected = finalize_login(connection, forged, vault)
            self.assertEqual(rejected.status, "unavailable")
            self.assertEqual(pending.status, "pending_commit")
            self.assertEqual(
                pending.issued_session.status,
                "pending_commit",
            )
            self.assertEqual(vault_entry_count(vault), 1)

            finalized = finalize_login(connection, pending, vault)
            self.assertEqual(finalized.status, "issued")
            self.assertIs(
                finalized.issued_session,
                pending.issued_session,
            )
            replay = finalize_login(connection, pending, vault)
            self.assertEqual(replay.status, "unavailable")
            self.assertEqual(finalized.issued_session.status, "issued")
            self.assertEqual(vault_entry_count(vault), 1)

            lease = completion.prepare_session_delivery(
                connection,
                finalized,
                vault,
                now=TRUSTED_NOW,
            )
            lease.acknowledge_delivery()
            self.assertEqual(finalized.issued_session.status, "consumed")
            self.assertTrue(vault_is_closed_and_empty(vault))

    def test_delivery_authority_claim_is_thread_safe_and_one_shot(self):
        with login_database(suffix="result-authority-thread") as (
            path,
            connection,
            created,
        ):
            result, vault = self._complete(
                connection,
                created,
                "result-authority-thread-001",
            )
            threaded_connection = sqlite3.connect(
                path,
                timeout=2.0,
                check_same_thread=False,
            )
            threaded_connection.row_factory = sqlite3.Row
            threaded_connection.execute("PRAGMA foreign_keys = ON")
            barrier = threading.Barrier(3)

            def acquire():
                barrier.wait()
                try:
                    return (
                        "prepared",
                        completion.prepare_session_delivery(
                            threaded_connection,
                            result,
                            vault,
                            now=TRUSTED_NOW,
                        ),
                    )
                except BrowserSessionLifecycleError as exc:
                    return ("rejected", exc.code)

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(acquire) for _index in range(2)]
                    barrier.wait()
                    outcomes = [future.result(timeout=10.0) for future in futures]
                prepared = [
                    value
                    for status, value in outcomes
                    if status == "prepared"
                ]
                rejected = [
                    value
                    for status, value in outcomes
                    if status == "rejected"
                ]
                self.assertEqual(len(prepared), 1)
                self.assertEqual(rejected, ["already_completed"])
                prepared[0].fail_delivery()
                self.assertEqual(
                    result.issued_session.status,
                    "terminal_failed",
                )
                self.assertTrue(vault_is_closed_and_empty(vault))
            finally:
                threaded_connection.close()


if __name__ == "__main__":
    unittest.main()
