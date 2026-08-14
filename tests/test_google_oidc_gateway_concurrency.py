from concurrent.futures import ThreadPoolExecutor
import inspect
import threading
import unittest
from unittest import mock

import wahojobs.google_oidc_gateway as gateway_module

from tests.browser_session_lifecycle_test_support import (
    connect,
    consume_issued,
)
from tests.google_oidc_gateway_test_support import (
    PRIMARY_SIGNING_FIXTURE,
    REDIRECT_URI,
    ROTATED_SIGNING_FIXTURE,
    close_secret_vault,
    completion_policy,
    gateway_database,
    make_fake_gateway,
    make_real_gateway,
    request_secret_vault,
    seed_existing_google_identity,
    sockets_blocked,
    vault_entry_count,
)


class GoogleOidcGatewayConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self._socket_guard = sockets_blocked()
        self._socket_guard.__enter__()
        self.addCleanup(
            self._socket_guard.__exit__,
            None,
            None,
            None,
        )

    def _complete(
        self,
        harness,
        connection,
        prepared,
        policy,
        callback=None,
    ):
        vault = request_secret_vault()
        try:
            callback = callback or harness.transport.callback_for(prepared)
            result = harness.gateway.complete_authorization(
                connection,
                prepared.transaction,
                callback,
                policy,
                vault,
            )
            delivered = result.status == "issued"
            if delivered:
                consume_issued(
                    result.issued_session,
                    vault=vault,
                    now=harness.clock(),
                )
            return (
                result.status,
                delivered,
                vault_entry_count(vault),
                repr(result),
            )
        finally:
            close_secret_vault(vault)

    def test_concurrent_callback_replay_has_one_winner_and_one_provider_call(self):
        with gateway_database(suffix="oidc-concurrent-replay") as database:
            harness = make_fake_gateway(
                subject=database.subject,
                block=True,
            )
            policy = completion_policy()
            prepared = harness.gateway.prepare_authorization()
            transaction = prepared.transaction
            callback = harness.transport.callback_for(prepared)

            def worker():
                connection = connect(database.path, timeout=5.0)
                try:
                    return self._complete(
                        harness,
                        connection,
                        prepared,
                        policy,
                        callback,
                    )
                finally:
                    connection.close()

            try:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    winner = pool.submit(worker)
                    self.assertTrue(
                        harness.transport.entered.wait(timeout=5.0)
                    )
                    replay_futures = [pool.submit(worker) for _ in range(7)]
                    try:
                        replay_outcomes = [
                            future.result(timeout=5.0)
                            for future in replay_futures
                        ]
                    finally:
                        harness.transport.release.set()
                    winner_outcome = winner.result(timeout=10.0)

                self.assertEqual(winner_outcome[:3], ("issued", True, 0))
                self.assertEqual(
                    [outcome[0] for outcome in replay_outcomes],
                    ["invalid_or_expired_transaction"] * 7,
                )
                self.assertTrue(
                    all(
                        outcome[1:3] == (False, 0)
                        for outcome in replay_outcomes
                    )
                )
                self.assertEqual(harness.transport.call_count, 1)
                self.assertEqual(transaction.status, "consumed")
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM account_sessions"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM account_sessions "
                        "WHERE revoked_at IS NULL"
                    ).fetchone()[0],
                    1,
                )
            finally:
                harness.transport.release.set()
                harness.close()

    def test_concurrent_terminal_denial_physically_emits_once_and_replay_emits_nothing(self):
        from scripts.durable_google_login_app import (
            _GoogleCallbackFailureStderrWriter,
        )

        class Stream:
            def __init__(self):
                self.write_calls = []
                self.flush_calls = 0

            def write(self, value):
                self.write_calls.append(value)

            def flush(self):
                self.flush_calls += 1

        with gateway_database(suffix="oidc-concurrent-denial-telemetry") as database:
            stream = Stream()
            writer = _GoogleCallbackFailureStderrWriter(stream)
            harness = make_fake_gateway(
                subject=database.subject,
                outcomes=("authentication_denied",),
                block=True,
            )
            gateway_module._configure_callback_failure_telemetry(
                harness.gateway,
                writer,
            )
            policy = completion_policy()
            prepared = harness.gateway.prepare_authorization()
            callback = harness.transport.callback_for(prepared)

            def worker():
                connection = connect(database.path, timeout=5.0)
                try:
                    return self._complete(
                        harness,
                        connection,
                        prepared,
                        policy,
                        callback,
                    )
                finally:
                    connection.close()

            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    winner = pool.submit(worker)
                    self.assertTrue(
                        harness.transport.entered.wait(timeout=5.0)
                    )
                    replay = pool.submit(worker)
                    replay_outcome = replay.result(timeout=5.0)
                    harness.transport.release.set()
                    winner_outcome = winner.result(timeout=10.0)

                self.assertEqual(
                    winner_outcome[:3],
                    ("authentication_denied", False, 0),
                )
                self.assertEqual(
                    replay_outcome[:3],
                    ("invalid_or_expired_transaction", False, 0),
                )
                self.assertEqual(harness.transport.call_count, 1)
                self.assertEqual(prepared.transaction.status, "consumed")
                expected_line = (
                    gateway_module._GOOGLE_CALLBACK_FAILURE_EVENTS_V1[
                        gateway_module._GoogleCallbackFailureStageV1
                        .TOKEN_EXCHANGE_OAUTH_REJECTED
                    ]
                    + "\n"
                )
                self.assertEqual(
                    stream.write_calls,
                    [expected_line],
                )
                self.assertEqual(stream.flush_calls, 1)

                later_replay = worker()
                self.assertEqual(
                    later_replay[:3],
                    ("invalid_or_expired_transaction", False, 0),
                )
                self.assertEqual(stream.write_calls, [expected_line])
                self.assertEqual(stream.flush_calls, 1)
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM account_sessions"
                    ).fetchone()[0],
                    0,
                )
            finally:
                harness.transport.release.set()
                harness.close()

    def test_durable_suspension_or_disablement_before_b2d1_denies_login(self):
        for mutation in ("suspend_account", "disable_identity"):
            with self.subTest(mutation=mutation):
                with gateway_database(
                    suffix=f"oidc-before-b2d1-{mutation}"
                ) as database:
                    harness = make_fake_gateway(
                        subject=database.subject,
                        block=True,
                    )
                    policy = completion_policy()
                    prepared = harness.gateway.prepare_authorization()
                    callback = harness.transport.callback_for(prepared)

                    def worker():
                        connection = connect(database.path, timeout=5.0)
                        try:
                            return self._complete(
                                harness,
                                connection,
                                prepared,
                                policy,
                                callback,
                            )
                        finally:
                            connection.close()

                    try:
                        with ThreadPoolExecutor(max_workers=1) as pool:
                            completion = pool.submit(worker)
                            try:
                                self.assertTrue(
                                    harness.transport.entered.wait(
                                        timeout=5.0
                                    )
                                )
                                mutation_connection = connect(
                                    database.path,
                                    timeout=5.0,
                                )
                                try:
                                    if mutation == "suspend_account":
                                        mutation_connection.execute(
                                            "UPDATE users SET "
                                            "lifecycle_status = 'suspended', "
                                            "row_version = row_version + 1, "
                                            "updated_at = ? WHERE user_id = ?",
                                            (
                                                harness.clock().isoformat(),
                                                database.account_id,
                                            ),
                                        )
                                    else:
                                        mutation_connection.execute(
                                            "UPDATE auth_identities SET "
                                            "disabled_at = ? "
                                            "WHERE auth_identity_id = ?",
                                            (
                                                harness.clock().isoformat(),
                                                database.identity_id,
                                            ),
                                        )
                                    mutation_connection.commit()
                                finally:
                                    mutation_connection.close()
                            finally:
                                harness.transport.release.set()
                            outcome = completion.result(timeout=10.0)

                        self.assertEqual(
                            outcome[:3],
                            ("authentication_denied", False, 0),
                        )
                        self.assertEqual(
                            harness.transport.call_count,
                            1,
                        )
                        self.assertEqual(
                            prepared.transaction.status,
                            "consumed",
                        )
                        self.assertEqual(
                            database.connection.execute(
                                "SELECT COUNT(*) FROM account_sessions"
                            ).fetchone()[0],
                            0,
                        )
                    finally:
                        harness.transport.release.set()
                        harness.close()

    def test_gateway_close_cancels_inflight_provider_success(self):
        with gateway_database(suffix="oidc-provider-versus-close") as database:
            harness = make_fake_gateway(
                subject=database.subject,
                block=True,
            )
            policy = completion_policy()
            prepared = harness.gateway.prepare_authorization()
            callback = harness.transport.callback_for(prepared)

            def worker():
                connection = connect(database.path, timeout=5.0)
                try:
                    return self._complete(
                        harness,
                        connection,
                        prepared,
                        policy,
                        callback,
                    )
                finally:
                    connection.close()

            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    completion = pool.submit(worker)
                    try:
                        self.assertTrue(
                            harness.transport.entered.wait(timeout=5.0)
                        )
                        harness.gateway.close()
                    finally:
                        harness.transport.release.set()
                    outcome = completion.result(timeout=10.0)

                self.assertEqual(
                    outcome[:3],
                    ("invalid_or_expired_transaction", False, 0),
                )
                self.assertEqual(harness.transport.call_count, 1)
                self.assertEqual(prepared.transaction.status, "consumed")
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM account_sessions"
                    ).fetchone()[0],
                    0,
                )
            finally:
                harness.transport.release.set()
                harness.close()

    def test_provider_failure_consumes_attempt_but_fresh_transaction_succeeds(self):
        with gateway_database(suffix="oidc-failure-then-fresh") as database:
            harness = make_fake_gateway(
                subject=database.subject,
                outcomes=("provider_unavailable", "success"),
            )
            policy = completion_policy()
            try:
                first = harness.gateway.prepare_authorization()
                first_outcome = self._complete(
                    harness,
                    database.connection,
                    first,
                    policy,
                )
                self.assertEqual(
                    first_outcome[:3],
                    ("provider_unavailable", False, 0),
                )
                self.assertEqual(first.transaction.status, "consumed")
                self.assertEqual(harness.transport.call_count, 1)
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM account_sessions"
                    ).fetchone()[0],
                    0,
                )

                second = harness.gateway.prepare_authorization()
                self.assertIsNot(second.transaction, first.transaction)
                second_outcome = self._complete(
                    harness,
                    database.connection,
                    second,
                    policy,
                )
                self.assertEqual(
                    second_outcome[:3],
                    ("issued", True, 0),
                )
                self.assertEqual(second.transaction.status, "consumed")
                self.assertEqual(harness.transport.call_count, 2)
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM account_sessions "
                        "WHERE revoked_at IS NULL"
                    ).fetchone()[0],
                    1,
                )
            finally:
                harness.close()

    def test_foreign_gateway_failure_does_not_consume_independent_state(self):
        with gateway_database(suffix="oidc-independent-a") as database:
            second_suffix = "oidc-independent-b"
            second = seed_existing_google_identity(
                database.connection,
                suffix=second_suffix,
            )
            harness_a = make_fake_gateway(subject=database.subject)
            harness_b = make_fake_gateway(
                subject=f"google-subject-{second_suffix}"
            )
            policy = completion_policy()
            try:
                prepared_a = harness_a.gateway.prepare_authorization()
                prepared_b = harness_b.gateway.prepare_authorization()
                foreign_vault = request_secret_vault()
                try:
                    foreign = harness_b.gateway.complete_authorization(
                        database.connection,
                        prepared_a.transaction,
                        REDIRECT_URI,
                        policy,
                        foreign_vault,
                    )
                    self.assertEqual(
                        foreign.status,
                        "invalid_or_expired_transaction",
                    )
                    self.assertEqual(vault_entry_count(foreign_vault), 0)
                finally:
                    close_secret_vault(foreign_vault)

                self.assertEqual(prepared_a.transaction.status, "fresh")
                self.assertEqual(prepared_b.transaction.status, "fresh")
                self.assertEqual(harness_a.transport.call_count, 0)
                self.assertEqual(harness_b.transport.call_count, 0)

                outcome_a = self._complete(
                    harness_a,
                    database.connection,
                    prepared_a,
                    policy,
                )
                self.assertEqual(outcome_a[:3], ("issued", True, 0))
                harness_a.close()

                self.assertEqual(prepared_b.transaction.status, "fresh")
                outcome_b = self._complete(
                    harness_b,
                    database.connection,
                    prepared_b,
                    policy,
                )
                self.assertEqual(outcome_b[:3], ("issued", True, 0))
                self.assertEqual(harness_a.transport.call_count, 1)
                self.assertEqual(harness_b.transport.call_count, 1)

                active_by_account = {
                    row["user_id"]: row["active_count"]
                    for row in database.connection.execute(
                        "SELECT user_id, COUNT(*) AS active_count "
                        "FROM account_sessions WHERE revoked_at IS NULL "
                        "GROUP BY user_id"
                    ).fetchall()
                }
                self.assertEqual(
                    active_by_account,
                    {
                        database.account_id: 1,
                        second.user.user_id: 1,
                    },
                )
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM account_sessions"
                    ).fetchone()[0],
                    2,
                )
            finally:
                harness_a.close()
                harness_b.close()


class AtomicDelegationCommitTests(unittest.TestCase):
    def setUp(self):
        self._socket_guard = sockets_blocked()
        self._socket_guard.__enter__()
        self.addCleanup(
            self._socket_guard.__exit__,
            None,
            None,
            None,
        )

    @staticmethod
    def _complete_raw(harness, database_path, prepared, callback):
        connection = connect(database_path, timeout=5.0)
        vault = request_secret_vault()
        try:
            return harness.gateway.complete_authorization(
                connection,
                prepared.transaction,
                callback,
                completion_policy(),
                vault,
            )
        finally:
            close_secret_vault(vault)
            connection.close()

    def test_close_or_expiry_immediately_before_commit_blocks_b2d1(self):
        original_commit = gateway_module._commit_claimed_delegation
        original_issue = (
            gateway_module.TrustedExternalIdentityAuthentication._issue
        )
        for scenario in ("close", "expiry"):
            with self.subTest(scenario=scenario):
                with gateway_database(
                    suffix=f"oidc-precommit-{scenario}"
                ) as database:
                    harness = make_fake_gateway(subject=database.subject)
                    prepared = harness.gateway.prepare_authorization()
                    callback = harness.transport.callback_for(prepared)
                    commit_entered = threading.Event()
                    release_commit = threading.Event()
                    delegated_result = object()

                    def pause_before_commit(*args, **kwargs):
                        commit_entered.set()
                        if not release_commit.wait(timeout=5.0):
                            raise AssertionError("precommit_release_timeout")
                        return original_commit(*args, **kwargs)

                    try:
                        with (
                            mock.patch.object(
                                gateway_module,
                                "_commit_claimed_delegation",
                                side_effect=pause_before_commit,
                            ),
                            mock.patch.object(
                                gateway_module,
                                "complete_trusted_login",
                                return_value=delegated_result,
                            ) as delegate,
                            mock.patch.object(
                                gateway_module.TrustedExternalIdentityAuthentication,
                                "_issue",
                                side_effect=original_issue,
                            ) as proof_issue,
                            ThreadPoolExecutor(max_workers=1) as pool,
                        ):
                            completion = pool.submit(
                                self._complete_raw,
                                harness,
                                database.path,
                                prepared,
                                callback,
                            )
                            try:
                                self.assertTrue(
                                    commit_entered.wait(timeout=5.0)
                                )
                                if scenario == "close":
                                    harness.gateway.close()
                                else:
                                    harness.clock.advance(600)
                            finally:
                                release_commit.set()
                            outcome = completion.result(timeout=10.0)

                        self.assertEqual(
                            outcome.status,
                            "invalid_or_expired_transaction",
                        )
                        delegate.assert_not_called()
                        proof_issue.assert_not_called()
                        self.assertEqual(
                            prepared.transaction.status,
                            "consumed",
                        )
                        self.assertEqual(harness.transport.call_count, 1)
                        self.assertEqual(
                            database.connection.execute(
                                "SELECT COUNT(*) FROM account_sessions"
                            ).fetchone()[0],
                            0,
                        )
                    finally:
                        release_commit.set()
                        harness.close()

    def test_close_or_clock_advance_after_commit_cannot_cancel_winner(self):
        original_commit = gateway_module._commit_claimed_delegation
        original_issue = (
            gateway_module.TrustedExternalIdentityAuthentication._issue
        )
        for scenario in ("close", "clock_advance"):
            with self.subTest(scenario=scenario):
                with gateway_database(
                    suffix=f"oidc-postcommit-{scenario}"
                ) as database:
                    harness = make_fake_gateway(subject=database.subject)
                    prepared = harness.gateway.prepare_authorization()
                    callback = harness.transport.callback_for(prepared)
                    committed_at = harness.clock()
                    commit_returned = threading.Event()
                    release_capsule = threading.Event()
                    delegated_result = object()

                    def pause_after_commit(*args, **kwargs):
                        capsule = original_commit(*args, **kwargs)
                        commit_returned.set()
                        if not release_capsule.wait(timeout=5.0):
                            raise AssertionError("postcommit_release_timeout")
                        return capsule

                    try:
                        with (
                            mock.patch.object(
                                gateway_module,
                                "_commit_claimed_delegation",
                                side_effect=pause_after_commit,
                            ),
                            mock.patch.object(
                                gateway_module,
                                "complete_trusted_login",
                                return_value=delegated_result,
                            ) as delegate,
                            mock.patch.object(
                                gateway_module.TrustedExternalIdentityAuthentication,
                                "_issue",
                                side_effect=original_issue,
                            ) as proof_issue,
                            ThreadPoolExecutor(max_workers=1) as pool,
                        ):
                            completion = pool.submit(
                                self._complete_raw,
                                harness,
                                database.path,
                                prepared,
                                callback,
                            )
                            try:
                                self.assertTrue(
                                    commit_returned.wait(timeout=5.0)
                                )
                                self.assertEqual(
                                    prepared.transaction.status,
                                    "consumed",
                                )
                                if scenario == "close":
                                    harness.gateway.close()
                                else:
                                    harness.clock.advance(3_600)
                            finally:
                                release_capsule.set()
                            outcome = completion.result(timeout=10.0)

                        self.assertIs(outcome, delegated_result)
                        delegate.assert_called_once()
                        proof_issue.assert_called_once()
                        self.assertEqual(
                            delegate.call_args.kwargs["trusted_now"],
                            committed_at,
                        )
                        self.assertEqual(
                            prepared.transaction.status,
                            "consumed",
                        )
                        self.assertEqual(harness.transport.call_count, 1)
                    finally:
                        release_capsule.set()
                        harness.close()

    def test_commit_detaches_callback_and_surplus_before_delegation(self):
        with gateway_database(suffix="oidc-commit-detaches-surplus") as database:
            harness = make_real_gateway(subject=database.subject)
            prepared = harness.gateway.prepare_authorization()
            callback = harness.transport.callback_for(
                prepared,
                code="atomic-boundary-code-marker",
            )
            original_issue = (
                gateway_module.TrustedExternalIdentityAuthentication._issue
            )
            snapshots = {}
            delegated_result = object()
            surplus_names = (
                "gateway_object",
                "gateway_record",
                "transaction",
                "transaction_record",
                "callback_url",
                "claim_attempt",
                "projection",
                "verified_identity",
                "resolved",
                "capsule",
                "capsule_values",
            )

            def capture_issue(_cls, *args, **kwargs):
                caller = inspect.currentframe().f_back
                try:
                    snapshots["proof"] = {
                        name: caller.f_locals.get(name)
                        for name in surplus_names
                    }
                finally:
                    caller = None
                return original_issue(*args, **kwargs)

            def capture_delegate(*_args, **_kwargs):
                caller = inspect.currentframe().f_back
                try:
                    snapshots["b2d1"] = {
                        name: caller.f_locals.get(name)
                        for name in surplus_names
                    }
                finally:
                    caller = None
                return delegated_result

            try:
                with (
                    mock.patch.object(
                        gateway_module.TrustedExternalIdentityAuthentication,
                        "_issue",
                        new=classmethod(capture_issue),
                    ),
                    mock.patch.object(
                        gateway_module,
                        "complete_trusted_login",
                        new=capture_delegate,
                    ),
                ):
                    outcome = self._complete_raw(
                        harness,
                        database.path,
                        prepared,
                        callback,
                    )

                self.assertIs(outcome, delegated_result)
                self.assertEqual(set(snapshots), {"proof", "b2d1"})
                for stage, snapshot in snapshots.items():
                    with self.subTest(stage=stage):
                        self.assertEqual(
                            snapshot,
                            {name: None for name in surplus_names},
                        )
                self.assertEqual(prepared.transaction.status, "consumed")
            finally:
                harness.close()

    def test_replay_after_commit_gets_no_capsule_or_b2d1_authority(self):
        with gateway_database(suffix="oidc-committed-owner-only") as database:
            harness = make_fake_gateway(subject=database.subject)
            prepared = harness.gateway.prepare_authorization()
            callback = harness.transport.callback_for(prepared)
            original_commit = gateway_module._commit_claimed_delegation
            original_issue = (
                gateway_module.TrustedExternalIdentityAuthentication._issue
            )
            commit_returned = threading.Event()
            release_capsule = threading.Event()
            captured_capsules = []
            commit_calls = []
            delegated_result = object()

            def capture_committed_capsule(*args, **kwargs):
                capsule = original_commit(*args, **kwargs)
                commit_calls.append(args[4])
                captured_capsules.append(capsule)
                commit_returned.set()
                if not release_capsule.wait(timeout=5.0):
                    raise AssertionError("owner_capsule_release_timeout")
                return capsule

            try:
                with (
                    mock.patch.object(
                        gateway_module,
                        "_commit_claimed_delegation",
                        side_effect=capture_committed_capsule,
                    ),
                    mock.patch.object(
                        gateway_module,
                        "complete_trusted_login",
                        return_value=delegated_result,
                    ) as delegate,
                    mock.patch.object(
                        gateway_module.TrustedExternalIdentityAuthentication,
                        "_issue",
                        side_effect=original_issue,
                    ) as proof_issue,
                    ThreadPoolExecutor(max_workers=1) as pool,
                ):
                    winner = pool.submit(
                        self._complete_raw,
                        harness,
                        database.path,
                        prepared,
                        callback,
                    )
                    try:
                        self.assertTrue(commit_returned.wait(timeout=5.0))
                        replay = self._complete_raw(
                            harness,
                            database.path,
                            prepared,
                            callback,
                        )
                        self.assertEqual(
                            replay.status,
                            "invalid_or_expired_transaction",
                        )
                        self.assertEqual(len(commit_calls), 1)
                        self.assertEqual(len(captured_capsules), 1)
                        self.assertFalse(captured_capsules[0]._used)
                        with self.assertRaises(
                            gateway_module._InvalidTransaction
                        ):
                            captured_capsules[0].take(object())
                        self.assertFalse(captured_capsules[0]._used)
                        delegate.assert_not_called()
                        proof_issue.assert_not_called()
                    finally:
                        release_capsule.set()
                    winner_outcome = winner.result(timeout=10.0)

                self.assertIs(winner_outcome, delegated_result)
                delegate.assert_called_once()
                proof_issue.assert_called_once()
                self.assertTrue(captured_capsules[0]._used)
                self.assertEqual(len(commit_calls), 1)
                self.assertEqual(harness.transport.call_count, 1)
                self.assertEqual(prepared.transaction.status, "consumed")
            finally:
                release_capsule.set()
                harness.close()

    def test_b2d1_denial_and_failure_leave_transaction_consumed(self):
        original_issue = (
            gateway_module.TrustedExternalIdentityAuthentication._issue
        )
        for status in ("authentication_denied", "unavailable"):
            with self.subTest(status=status):
                with gateway_database(
                    suffix=f"oidc-b2d1-{status}"
                ) as database:
                    harness = make_fake_gateway(subject=database.subject)
                    prepared = harness.gateway.prepare_authorization()
                    callback = harness.transport.callback_for(prepared)
                    boundary_result = mock.NonCallableMock(status=status)
                    try:
                        with (
                            mock.patch.object(
                                gateway_module,
                                "complete_trusted_login",
                                return_value=boundary_result,
                            ) as delegate,
                            mock.patch.object(
                                gateway_module.TrustedExternalIdentityAuthentication,
                                "_issue",
                                side_effect=original_issue,
                            ) as proof_issue,
                        ):
                            outcome = self._complete_raw(
                                harness,
                                database.path,
                                prepared,
                                callback,
                            )

                        self.assertIs(outcome, boundary_result)
                        delegate.assert_called_once()
                        proof_issue.assert_called_once()
                        self.assertEqual(
                            prepared.transaction.status,
                            "consumed",
                        )
                        self.assertEqual(harness.transport.call_count, 1)
                        self.assertEqual(
                            database.connection.execute(
                                "SELECT COUNT(*) FROM account_sessions"
                            ).fetchone()[0],
                            0,
                        )
                    finally:
                        harness.close()


class GoogleOidcGatewayJwksGenerationConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self._socket_guard = sockets_blocked()
        self._socket_guard.__enter__()
        self.addCleanup(
            self._socket_guard.__exit__,
            None,
            None,
            None,
        )

    def _complete(self, harness, connection, prepared, callback):
        vault = request_secret_vault()
        try:
            result = harness.gateway.complete_authorization(
                connection,
                prepared.transaction,
                callback,
                completion_policy(),
                vault,
            )
            if result.status == "issued":
                consume_issued(
                    result.issued_session,
                    vault=vault,
                    now=harness.clock(),
                )
            return result.status, vault_entry_count(vault)
        finally:
            close_secret_vault(vault)

    def _complete_from_path(
        self,
        harness,
        database_path,
        prepared,
        callback,
    ):
        connection = connect(database_path, timeout=5.0)
        try:
            return self._complete(
                harness,
                connection,
                prepared,
                callback,
            )
        finally:
            connection.close()

    def _warm_primary(self, harness, connection, code):
        prepared = harness.gateway.prepare_authorization()
        callback = harness.transport.callback_for(
            prepared,
            code=code,
            signing_fixture=PRIMARY_SIGNING_FIXTURE,
        )
        self.assertEqual(
            self._complete(harness, connection, prepared, callback),
            ("issued", 0),
        )
        self.assertEqual(harness.transport.jwks_request_count, 1)

    @staticmethod
    def _decode_probe(parties):
        from joserfc import jwt as real_jwt
        from joserfc.errors import InvalidKeyIdError

        original_decode = real_jwt.decode
        old_key_barrier = threading.Barrier(parties)
        probe_lock = threading.Lock()
        calls_by_thread = {}
        first_unknown_by_thread = {}

        def instrumented_decode(value, key_set, *args, **kwargs):
            thread_id = threading.get_ident()
            with probe_lock:
                calls_by_thread.setdefault(thread_id, []).append(key_set)
            try:
                return original_decode(value, key_set, *args, **kwargs)
            except InvalidKeyIdError:
                wait_for_peers = False
                with probe_lock:
                    if thread_id not in first_unknown_by_thread:
                        first_unknown_by_thread[thread_id] = key_set
                        wait_for_peers = True
                if wait_for_peers:
                    old_key_barrier.wait(timeout=5.0)
                raise

        return (
            real_jwt,
            instrumented_decode,
            calls_by_thread,
            first_unknown_by_thread,
            probe_lock,
        )

    @staticmethod
    def _probe_snapshot(calls, failures, probe_lock):
        with probe_lock:
            return (
                {
                    thread_id: tuple(sequence)
                    for thread_id, sequence in calls.items()
                },
                dict(failures),
            )

    def test_simultaneous_rotated_kid_uses_one_generation_refresh(self):
        with gateway_database(suffix="oidc-jwks-one-flight") as database:
            harness = make_real_gateway(subject=database.subject)
            try:
                self._warm_primary(
                    harness,
                    database.connection,
                    "jwks-one-flight-warm",
                )
                harness.transport.use_jwks_fixtures(
                    ROTATED_SIGNING_FIXTURE
                )
                prepared = tuple(
                    harness.gateway.prepare_authorization()
                    for _index in range(2)
                )
                callbacks = tuple(
                    harness.transport.callback_for(
                        item,
                        code=f"jwks-one-flight-rotated-{index}",
                        signing_fixture=ROTATED_SIGNING_FIXTURE,
                    )
                    for index, item in enumerate(prepared)
                )
                (
                    real_jwt,
                    instrumented_decode,
                    calls,
                    failures,
                    probe_lock,
                ) = self._decode_probe(2)

                with (
                    mock.patch.object(
                        real_jwt,
                        "decode",
                        new=instrumented_decode,
                    ),
                    ThreadPoolExecutor(max_workers=2) as pool,
                ):
                    futures = tuple(
                        pool.submit(
                            self._complete_from_path,
                            harness,
                            database.path,
                            item,
                            callback,
                        )
                        for item, callback in zip(prepared, callbacks)
                    )
                    outcomes = tuple(
                        future.result(timeout=10.0)
                        for future in futures
                    )

                sequences, first_unknown = self._probe_snapshot(
                    calls,
                    failures,
                    probe_lock,
                )
                self.assertEqual(outcomes, (("issued", 0), ("issued", 0)))
                self.assertEqual(harness.transport.token_request_count, 3)
                self.assertEqual(harness.transport.jwks_request_count, 2)
                self.assertEqual(len(sequences), 2)
                self.assertEqual(
                    sorted(len(sequence) for sequence in sequences.values()),
                    [2, 2],
                )
                self.assertTrue(
                    all(len(sequence) <= 2 for sequence in sequences.values())
                )
                self.assertEqual(
                    len(
                        {
                            id(sequence[0])
                            for sequence in sequences.values()
                        }
                    ),
                    1,
                )
                self.assertEqual(
                    len(
                        {
                            id(sequence[1])
                            for sequence in sequences.values()
                        }
                    ),
                    1,
                )
                self.assertNotEqual(
                    id(next(iter(sequences.values()))[0]),
                    id(next(iter(sequences.values()))[1]),
                )
                self.assertEqual(len(first_unknown), 2)
                self.assertTrue(
                    all(
                        first_unknown[thread_id] is sequence[0]
                        for thread_id, sequence in sequences.items()
                    )
                )
            finally:
                harness.close()

    def test_unknown_kid_in_newest_generation_fails_after_one_retry(self):
        with gateway_database(suffix="oidc-jwks-newest-unknown") as database:
            harness = make_real_gateway(subject=database.subject)
            try:
                self._warm_primary(
                    harness,
                    database.connection,
                    "jwks-newest-unknown-warm",
                )
                prepared = harness.gateway.prepare_authorization()
                callback = harness.transport.callback_for(
                    prepared,
                    code="jwks-newest-still-unknown",
                    signing_fixture=ROTATED_SIGNING_FIXTURE,
                )
                (
                    real_jwt,
                    instrumented_decode,
                    calls,
                    failures,
                    probe_lock,
                ) = self._decode_probe(1)
                with mock.patch.object(
                    real_jwt,
                    "decode",
                    new=instrumented_decode,
                ):
                    outcome = self._complete(
                        harness,
                        database.connection,
                        prepared,
                        callback,
                    )

                sequences, first_unknown = self._probe_snapshot(
                    calls,
                    failures,
                    probe_lock,
                )
                self.assertEqual(outcome, ("authentication_denied", 0))
                self.assertEqual(harness.transport.jwks_request_count, 2)
                self.assertEqual(len(sequences), 1)
                sequence = next(iter(sequences.values()))
                self.assertEqual(len(sequence), 2)
                self.assertIs(
                    next(iter(first_unknown.values())),
                    sequence[0],
                )
                self.assertIsNot(sequence[0], sequence[1])

                primary = harness.gateway.prepare_authorization()
                primary_callback = harness.transport.callback_for(
                    primary,
                    code="jwks-newest-primary-control",
                    signing_fixture=PRIMARY_SIGNING_FIXTURE,
                )
                self.assertEqual(
                    self._complete(
                        harness,
                        database.connection,
                        primary,
                        primary_callback,
                    ),
                    ("issued", 0),
                )
                self.assertEqual(harness.transport.jwks_request_count, 2)
            finally:
                harness.close()

    def test_failed_refresh_releases_waiter_and_preserves_old_cache(self):
        with gateway_database(suffix="oidc-jwks-failed-flight") as database:
            harness = make_real_gateway(subject=database.subject)
            harness.transport.jwks_release.set()
            try:
                self._warm_primary(
                    harness,
                    database.connection,
                    "jwks-failed-flight-warm",
                )
                harness.transport.queue_jwks_response(
                    document={"error": "temporarily_unavailable"},
                    status=503,
                )
                harness.transport.block_next_jwks()
                prepared = tuple(
                    harness.gateway.prepare_authorization()
                    for _index in range(2)
                )
                callbacks = tuple(
                    harness.transport.callback_for(
                        item,
                        code=f"jwks-failed-flight-rotated-{index}",
                        signing_fixture=ROTATED_SIGNING_FIXTURE,
                    )
                    for index, item in enumerate(prepared)
                )
                waiter_entered = threading.Event()
                cache_type = gateway_module._GoogleOidcJwksCache
                original_wait = cache_type._wait_for_flight_locked

                def observe_waiter(cache, flight):
                    waiter_entered.set()
                    return original_wait(cache, flight)

                (
                    real_jwt,
                    instrumented_decode,
                    calls,
                    failures,
                    probe_lock,
                ) = self._decode_probe(2)
                with (
                    mock.patch.object(
                        real_jwt,
                        "decode",
                        new=instrumented_decode,
                    ),
                    mock.patch.object(
                        cache_type,
                        "_wait_for_flight_locked",
                        new=observe_waiter,
                    ),
                    ThreadPoolExecutor(max_workers=2) as pool,
                ):
                    futures = tuple(
                        pool.submit(
                            self._complete_from_path,
                            harness,
                            database.path,
                            item,
                            callback,
                        )
                        for item, callback in zip(prepared, callbacks)
                    )
                    try:
                        self.assertTrue(
                            harness.transport.jwks_entered.wait(timeout=5.0)
                        )
                        self.assertTrue(waiter_entered.wait(timeout=5.0))
                    finally:
                        harness.transport.jwks_release.set()
                    outcomes = tuple(
                        future.result(timeout=10.0)
                        for future in futures
                    )

                sequences, first_unknown = self._probe_snapshot(
                    calls,
                    failures,
                    probe_lock,
                )
                self.assertEqual(
                    outcomes,
                    (
                        ("provider_unavailable", 0),
                        ("provider_unavailable", 0),
                    ),
                )
                self.assertEqual(harness.transport.jwks_request_count, 2)
                self.assertEqual(len(sequences), 2)
                self.assertEqual(
                    sorted(len(sequence) for sequence in sequences.values()),
                    [1, 1],
                )
                self.assertEqual(len(first_unknown), 2)
                self.assertEqual(
                    len(
                        {
                            id(sequence[0])
                            for sequence in sequences.values()
                        }
                    ),
                    1,
                )

                primary = harness.gateway.prepare_authorization()
                primary_callback = harness.transport.callback_for(
                    primary,
                    code="jwks-failed-flight-primary-control",
                    signing_fixture=PRIMARY_SIGNING_FIXTURE,
                )
                self.assertEqual(
                    self._complete(
                        harness,
                        database.connection,
                        primary,
                        primary_callback,
                    ),
                    ("issued", 0),
                )
                self.assertEqual(harness.transport.jwks_request_count, 2)
            finally:
                harness.transport.jwks_release.set()
                harness.close()

    def test_rotated_refreshes_are_independent_between_gateway_instances(self):
        with (
            gateway_database(suffix="oidc-jwks-independent-a") as first_db,
            gateway_database(suffix="oidc-jwks-independent-b") as second_db,
        ):
            first = make_real_gateway(subject=first_db.subject)
            second = make_real_gateway(subject=second_db.subject)
            try:
                self._warm_primary(
                    first,
                    first_db.connection,
                    "jwks-independent-a-warm",
                )
                self._warm_primary(
                    second,
                    second_db.connection,
                    "jwks-independent-b-warm",
                )
                first.transport.use_jwks_fixtures(
                    ROTATED_SIGNING_FIXTURE
                )
                second.transport.use_jwks_fixtures(
                    ROTATED_SIGNING_FIXTURE
                )
                first_prepared = first.gateway.prepare_authorization()
                second_prepared = second.gateway.prepare_authorization()
                first_callback = first.transport.callback_for(
                    first_prepared,
                    code="jwks-independent-a-rotated",
                    signing_fixture=ROTATED_SIGNING_FIXTURE,
                )
                second_callback = second.transport.callback_for(
                    second_prepared,
                    code="jwks-independent-b-rotated",
                    signing_fixture=ROTATED_SIGNING_FIXTURE,
                )
                (
                    real_jwt,
                    instrumented_decode,
                    calls,
                    failures,
                    probe_lock,
                ) = self._decode_probe(2)
                with (
                    mock.patch.object(
                        real_jwt,
                        "decode",
                        new=instrumented_decode,
                    ),
                    ThreadPoolExecutor(max_workers=2) as pool,
                ):
                    first_future = pool.submit(
                        self._complete_from_path,
                        first,
                        first_db.path,
                        first_prepared,
                        first_callback,
                    )
                    second_future = pool.submit(
                        self._complete_from_path,
                        second,
                        second_db.path,
                        second_prepared,
                        second_callback,
                    )
                    outcomes = (
                        first_future.result(timeout=10.0),
                        second_future.result(timeout=10.0),
                    )

                sequences, first_unknown = self._probe_snapshot(
                    calls,
                    failures,
                    probe_lock,
                )
                self.assertEqual(outcomes, (("issued", 0), ("issued", 0)))
                self.assertEqual(first.transport.jwks_request_count, 2)
                self.assertEqual(second.transport.jwks_request_count, 2)
                self.assertEqual(len(sequences), 2)
                self.assertEqual(
                    sorted(len(sequence) for sequence in sequences.values()),
                    [2, 2],
                )
                self.assertEqual(len(first_unknown), 2)
                self.assertEqual(
                    len(
                        {
                            id(sequence[0])
                            for sequence in sequences.values()
                        }
                    ),
                    2,
                )
                self.assertEqual(
                    len(
                        {
                            id(sequence[1])
                            for sequence in sequences.values()
                        }
                    ),
                    2,
                )
            finally:
                first.close()
                second.close()


if __name__ == "__main__":
    unittest.main()
