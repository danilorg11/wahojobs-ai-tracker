from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import threading
import unittest
from unittest import mock

from tests.google_oidc_authorization_transactions_test_support import (
    ManualClock,
    NOW,
    authorization_parameters,
    durable_transaction_database,
    key_authority,
    open_connection,
    reconstructed_gateway,
    sockets_blocked,
    transaction_rows,
)
import wahojobs.google_oidc_authorization_transaction_repository as repository
from wahojobs.google_oidc_authorization_transaction_repository import (
    GoogleOidcAuthorizationTransactionRepositoryError,
    claim_google_oidc_authorization_transaction,
    cleanup_google_oidc_authorization_transactions,
    prepare_google_oidc_authorization_transaction,
)


def _create_terminal_row(connection, gateway, authority, lifecycle):
    with sockets_blocked():
        prepared = prepare_google_oidc_authorization_transaction(
            connection,
            gateway,
            authority,
        )
    transaction_id = prepared.transaction_id
    row = next(
        value
        for value in transaction_rows(connection)
        if value["transaction_id"] == transaction_id
    )
    terminal_at = (
        row["expires_at"] if lifecycle == "expired" else row["created_at"]
    )
    connection.execute(
        "UPDATE google_oidc_authorization_transactions "
        "SET lifecycle=?, claimed_at=?, terminal_at=?, row_version=2 "
        "WHERE transaction_id=?",
        (
            lifecycle,
            terminal_at if lifecycle == "consumed" else None,
            terminal_at,
            transaction_id,
        ),
    )
    connection.commit()
    prepared.close()
    return transaction_id


class GoogleOidcAuthorizationTransactionConcurrencyTests(unittest.TestCase):
    def test_two_threads_and_independent_connections_have_exactly_one_winner(self):
        with durable_transaction_database(suffix="concurrency-threads") as database:
            authority = key_authority()
            harness = reconstructed_gateway(
                subject="google-subject-concurrency-threads"
            )
            try:
                with sockets_blocked():
                    prepared = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                state = authorization_parameters(prepared)["state"]
                barrier = threading.Barrier(2)

                def attempt():
                    connection = open_connection(database.path, timeout=2.0)
                    try:
                        barrier.wait(timeout=5)
                        try:
                            claimed = claim_google_oidc_authorization_transaction(
                                connection,
                                harness.gateway,
                                authority,
                                state,
                            )
                            claimed.close()
                            return "won"
                        except GoogleOidcAuthorizationTransactionRepositoryError as exc:
                            return exc.reason_code
                    finally:
                        connection.close()

                with sockets_blocked(), concurrent.futures.ThreadPoolExecutor(
                    max_workers=2
                ) as executor:
                    outcomes = tuple(
                        future.result(timeout=10)
                        for future in (
                            executor.submit(attempt),
                            executor.submit(attempt),
                        )
                    )
                self.assertEqual(outcomes.count("won"), 1)
                self.assertEqual(
                    outcomes.count("invalid_or_expired_transaction"),
                    1,
                )
                self.assertEqual(
                    transaction_rows(database.connection)[0]["lifecycle"],
                    "consumed",
                )
                prepared.close()
            finally:
                harness.close()
                authority.close()

    def test_contention_does_not_consume_and_retry_can_win(self):
        with durable_transaction_database(suffix="concurrency-lock") as database:
            authority = key_authority()
            harness = reconstructed_gateway(
                subject="google-subject-concurrency-lock"
            )
            claimant = open_connection(database.path, timeout=0.05)
            locker = open_connection(database.path, timeout=2.0)
            try:
                with sockets_blocked():
                    prepared = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                state = authorization_parameters(prepared)["state"]
                locker.execute("BEGIN IMMEDIATE")
                with sockets_blocked(), self.assertRaises(
                    GoogleOidcAuthorizationTransactionRepositoryError
                ) as busy:
                    claim_google_oidc_authorization_transaction(
                        claimant,
                        harness.gateway,
                        authority,
                        state,
                    )
                self.assertEqual(busy.exception.reason_code, "temporary_contention")
                self.assertEqual(
                    transaction_rows(database.connection)[0]["lifecycle"],
                    "prepared",
                )
                locker.rollback()
                with sockets_blocked():
                    claimed = claim_google_oidc_authorization_transaction(
                        claimant,
                        harness.gateway,
                        authority,
                        state,
                    )
                self.assertEqual(
                    transaction_rows(database.connection)[0]["lifecycle"],
                    "consumed",
                )
                claimed.close()
                prepared.close()
            finally:
                if locker.in_transaction:
                    locker.rollback()
                locker.close()
                claimant.close()
                harness.close()
                authority.close()

    def test_two_reconstructed_subprocesses_have_exactly_one_winner(self):
        with durable_transaction_database(suffix="concurrency-process") as database:
            authority = key_authority()
            harness = reconstructed_gateway(
                subject="google-subject-concurrency-process"
            )
            try:
                with sockets_blocked():
                    prepared = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                state = authorization_parameters(prepared)["state"]
                program = r"""
import json
import sys
from tests.google_oidc_authorization_transactions_test_support import (
    key_authority, open_connection, reconstructed_gateway, sockets_blocked,
)
from wahojobs.google_oidc_authorization_transaction_repository import (
    GoogleOidcAuthorizationTransactionRepositoryError,
    claim_google_oidc_authorization_transaction,
)
connection = open_connection(sys.argv[1], timeout=3.0)
authority = key_authority()
harness = reconstructed_gateway(subject="google-subject-concurrency-process")
try:
    try:
        with sockets_blocked():
            claimed = claim_google_oidc_authorization_transaction(
                connection, harness.gateway, authority, sys.argv[2]
            )
        claimed.close()
        outcome = "won"
    except GoogleOidcAuthorizationTransactionRepositoryError as exc:
        outcome = exc.reason_code
    print(json.dumps({"outcome": outcome}, sort_keys=True))
finally:
    connection.close()
    harness.close()
    authority.close()
"""
                processes = tuple(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-B",
                            "-c",
                            program,
                            str(database.path),
                            state,
                        ],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    for _index in range(2)
                )
                outcomes = []
                for process in processes:
                    stdout, stderr = process.communicate(timeout=20)
                    self.assertEqual(process.returncode, 0, stderr)
                    self.assertEqual(stderr, "")
                    outcomes.append(json.loads(stdout)["outcome"])
                self.assertEqual(outcomes.count("won"), 1)
                self.assertEqual(
                    outcomes.count("invalid_or_expired_transaction"),
                    1,
                )
                self.assertEqual(
                    transaction_rows(database.connection)[0]["lifecycle"],
                    "consumed",
                )
                prepared.close()
            finally:
                harness.close()
                authority.close()

    def test_claim_cleanup_and_cleanup_cleanup_races_are_fail_closed(self):
        with durable_transaction_database(
            suffix="concurrency-claim-expiry"
        ) as database:
            clock = ManualClock(NOW)
            authority = key_authority()
            harness = reconstructed_gateway(
                clock=clock,
                subject="google-subject-concurrency-claim-expiry",
            )
            try:
                with sockets_blocked():
                    prepared = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                state = authorization_parameters(prepared)["state"]
                clock.advance(600)
                barrier = threading.Barrier(2)

                def claim_at_expiry():
                    connection = open_connection(database.path, timeout=3.0)
                    try:
                        barrier.wait(timeout=5)
                        try:
                            claimed = (
                                claim_google_oidc_authorization_transaction(
                                    connection,
                                    harness.gateway,
                                    authority,
                                    state,
                                )
                            )
                            claimed.close()
                            return "claimed"
                        except (
                            GoogleOidcAuthorizationTransactionRepositoryError
                        ) as exc:
                            return exc.reason_code
                    finally:
                        connection.close()

                def cleanup_at_expiry():
                    connection = open_connection(database.path, timeout=3.0)
                    try:
                        barrier.wait(timeout=5)
                        result = (
                            cleanup_google_oidc_authorization_transactions(
                                connection,
                                harness.gateway,
                                authority,
                                limit=1,
                                terminal_retention_seconds=1,
                            )
                        )
                        return result.expired_count, result.deleted_count
                    finally:
                        connection.close()

                with sockets_blocked(), concurrent.futures.ThreadPoolExecutor(
                    max_workers=2
                ) as executor:
                    claim_future = executor.submit(claim_at_expiry)
                    cleanup_future = executor.submit(cleanup_at_expiry)
                    claim_outcome = claim_future.result(timeout=15)
                    cleanup_outcome = cleanup_future.result(timeout=15)
                self.assertEqual(
                    claim_outcome,
                    "invalid_or_expired_transaction",
                )
                self.assertIn(cleanup_outcome, {(0, 0), (1, 0)})
                self.assertEqual(
                    transaction_rows(database.connection)[0]["lifecycle"],
                    "expired",
                )
                prepared.close()
            finally:
                harness.close()
                authority.close()

        with durable_transaction_database(
            suffix="concurrency-claim-delete"
        ) as database:
            clock = ManualClock(NOW)
            authority = key_authority()
            harness = reconstructed_gateway(
                clock=clock,
                subject="google-subject-concurrency-claim-delete",
            )
            try:
                with sockets_blocked():
                    prepared = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                    claimed = claim_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                        authorization_parameters(prepared)["state"],
                    )
                state = authorization_parameters(prepared)["state"]
                claimed.close()
                clock.advance(1)
                barrier = threading.Barrier(2)

                def replay_claim():
                    connection = open_connection(database.path, timeout=3.0)
                    try:
                        barrier.wait(timeout=5)
                        try:
                            material = (
                                claim_google_oidc_authorization_transaction(
                                    connection,
                                    harness.gateway,
                                    authority,
                                    state,
                                )
                            )
                            material.close()
                            return "claimed"
                        except (
                            GoogleOidcAuthorizationTransactionRepositoryError
                        ) as exc:
                            return exc.reason_code
                    finally:
                        connection.close()

                def delete_terminal():
                    connection = open_connection(database.path, timeout=3.0)
                    try:
                        barrier.wait(timeout=5)
                        result = (
                            cleanup_google_oidc_authorization_transactions(
                                connection,
                                harness.gateway,
                                authority,
                                limit=1,
                                terminal_retention_seconds=1,
                            )
                        )
                        return result.deleted_count
                    finally:
                        connection.close()

                with sockets_blocked(), concurrent.futures.ThreadPoolExecutor(
                    max_workers=2
                ) as executor:
                    replay_future = executor.submit(replay_claim)
                    delete_future = executor.submit(delete_terminal)
                    replay_outcome = replay_future.result(timeout=15)
                    deleted = delete_future.result(timeout=15)
                self.assertEqual(
                    replay_outcome,
                    "invalid_or_expired_transaction",
                )
                self.assertEqual(deleted, 1)
                self.assertEqual(transaction_rows(database.connection), ())
                prepared.close()
            finally:
                harness.close()
                authority.close()

        with durable_transaction_database(
            suffix="concurrency-cleanup-cleanup"
        ) as database:
            clock = ManualClock(NOW)
            authority = key_authority()
            harness = reconstructed_gateway(
                clock=clock,
                subject="google-subject-concurrency-cleanup-cleanup",
            )
            try:
                _create_terminal_row(
                    database.connection,
                    harness.gateway,
                    authority,
                    "invalidated",
                )
                clock.advance(1)
                barrier = threading.Barrier(2)

                def cleanup_attempt():
                    connection = open_connection(database.path, timeout=3.0)
                    try:
                        barrier.wait(timeout=5)
                        result = (
                            cleanup_google_oidc_authorization_transactions(
                                connection,
                                harness.gateway,
                                authority,
                                limit=1,
                                terminal_retention_seconds=1,
                            )
                        )
                        return result.deleted_count
                    finally:
                        connection.close()

                with sockets_blocked(), concurrent.futures.ThreadPoolExecutor(
                    max_workers=2
                ) as executor:
                    outcomes = tuple(
                        future.result(timeout=15)
                        for future in (
                            executor.submit(cleanup_attempt),
                            executor.submit(cleanup_attempt),
                        )
                    )
                self.assertEqual(sum(outcomes), 1)
                self.assertEqual(transaction_rows(database.connection), ())
            finally:
                harness.close()
                authority.close()

    def test_action_start_snapshot_and_retention_clock_are_immutable(self):
        with durable_transaction_database(
            suffix="concurrency-cleanup-action-start"
        ) as database:
            clock = ManualClock(NOW)
            authority = key_authority()
            harness = reconstructed_gateway(
                clock=clock,
                subject="google-subject-concurrency-cleanup-action-start",
            )
            entered = threading.Event()
            release = threading.Event()
            try:
                with sockets_blocked():
                    prepared = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                state = authorization_parameters(prepared)["state"]

                def boundary(name):
                    if name == "cleanup.after_expiry":
                        entered.set()
                        if not release.wait(timeout=10):
                            raise AssertionError("cleanup release timeout")

                def cleanup_attempt():
                    connection = open_connection(database.path, timeout=3.0)
                    try:
                        return (
                            cleanup_google_oidc_authorization_transactions(
                                connection,
                                harness.gateway,
                                authority,
                                limit=1,
                                terminal_retention_seconds=1,
                            )
                        )
                    finally:
                        connection.close()

                def claim_attempt():
                    connection = open_connection(database.path, timeout=3.0)
                    try:
                        material = (
                            claim_google_oidc_authorization_transaction(
                                connection,
                                harness.gateway,
                                authority,
                                state,
                            )
                        )
                        material.close()
                        return "claimed"
                    finally:
                        connection.close()

                with sockets_blocked(), mock.patch.object(
                    repository,
                    "_failure_boundary",
                    side_effect=boundary,
                ), concurrent.futures.ThreadPoolExecutor(
                    max_workers=2
                ) as executor:
                    cleanup_future = executor.submit(cleanup_attempt)
                    if not entered.wait(timeout=10):
                        cleanup_future.result(timeout=1)
                        self.fail("cleanup did not reach its snapshot boundary")
                    claim_future = executor.submit(claim_attempt)
                    release.set()
                    cleanup_result = cleanup_future.result(timeout=15)
                    claim_result = claim_future.result(timeout=15)
                self.assertEqual(
                    (
                        cleanup_result.expired_count,
                        cleanup_result.deleted_count,
                    ),
                    (0, 0),
                )
                self.assertEqual(claim_result, "claimed")
                self.assertEqual(
                    transaction_rows(database.connection)[0]["lifecycle"],
                    "consumed",
                )
                prepared.close()
            finally:
                release.set()
                harness.close()
                authority.close()

        with durable_transaction_database(
            suffix="concurrency-cleanup-threshold-cross"
        ) as database:
            clock = ManualClock(NOW)
            authority = key_authority()
            harness = reconstructed_gateway(
                clock=clock,
                subject="google-subject-concurrency-cleanup-threshold-cross",
            )
            try:
                _create_terminal_row(
                    database.connection,
                    harness.gateway,
                    authority,
                    "invalidated",
                )
                clock.advance(9)

                def cross_threshold(name):
                    if name == "cleanup.after_expiry":
                        clock.advance(2)

                with sockets_blocked(), mock.patch.object(
                    repository,
                    "_failure_boundary",
                    side_effect=cross_threshold,
                ):
                    result = (
                        cleanup_google_oidc_authorization_transactions(
                            database.connection,
                            harness.gateway,
                            authority,
                            limit=1,
                            terminal_retention_seconds=10,
                        )
                    )
                self.assertEqual(result.deleted_count, 0)
                self.assertEqual(result.skipped_too_recent, 1)
                self.assertEqual(len(transaction_rows(database.connection)), 1)
            finally:
                harness.close()
                authority.close()

    def test_cleanup_control_flow_and_commit_boundaries_are_exact(self):
        for exception_type in (
            KeyboardInterrupt,
            SystemExit,
            GeneratorExit,
        ):
            for boundary, durable_count in (
                ("cleanup.after_delete", 1),
                ("cleanup.after_commit", 0),
            ):
                with self.subTest(
                    exception=exception_type.__name__,
                    boundary=boundary,
                ), durable_transaction_database(
                    suffix=(
                        "concurrency-cleanup-control-"
                        f"{exception_type.__name__}-{boundary}"
                    )
                ) as database:
                    clock = ManualClock(NOW)
                    authority = key_authority()
                    harness = reconstructed_gateway(
                        clock=clock,
                        subject=(
                            "google-subject-cleanup-control-"
                            f"{exception_type.__name__}-{boundary}"
                        ),
                    )
                    control = exception_type("cleanup-control")

                    def interrupt(name):
                        if name == boundary:
                            raise control

                    try:
                        _create_terminal_row(
                            database.connection,
                            harness.gateway,
                            authority,
                            "invalidated",
                        )
                        clock.advance(1)
                        with sockets_blocked(), mock.patch.object(
                            repository,
                            "_failure_boundary",
                            side_effect=interrupt,
                        ):
                            try:
                                cleanup_google_oidc_authorization_transactions(
                                    database.connection,
                                    harness.gateway,
                                    authority,
                                    limit=1,
                                    terminal_retention_seconds=1,
                                )
                            except BaseException as observed:
                                self.assertIs(observed, control)
                            else:
                                self.fail("control flow was not preserved")
                        self.assertEqual(
                            len(transaction_rows(database.connection)),
                            durable_count,
                        )
                        self.assertFalse(database.connection.in_transaction)
                    finally:
                        harness.close()
                        authority.close()

        for boundary, durable_count in (
            ("cleanup.after_delete", 1),
            ("cleanup.after_commit", 0),
        ):
            with self.subTest(
                ordinary_exception=boundary
            ), durable_transaction_database(
                suffix=f"concurrency-cleanup-error-{boundary}"
            ) as database:
                clock = ManualClock(NOW)
                authority = key_authority()
                harness = reconstructed_gateway(
                    clock=clock,
                    subject=f"google-subject-cleanup-error-{boundary}",
                )

                def fail(name):
                    if name == boundary:
                        raise RuntimeError("cleanup-boundary-failure")

                try:
                    _create_terminal_row(
                        database.connection,
                        harness.gateway,
                        authority,
                        "invalidated",
                    )
                    clock.advance(1)
                    with sockets_blocked(), mock.patch.object(
                        repository,
                        "_failure_boundary",
                        side_effect=fail,
                    ), self.assertRaises(
                        GoogleOidcAuthorizationTransactionRepositoryError
                    ):
                        cleanup_google_oidc_authorization_transactions(
                            database.connection,
                            harness.gateway,
                            authority,
                            limit=1,
                            terminal_retention_seconds=1,
                        )
                    self.assertEqual(
                        len(transaction_rows(database.connection)),
                        durable_count,
                    )
                    self.assertFalse(database.connection.in_transaction)
                finally:
                    harness.close()
                    authority.close()

    def test_cleanup_subprocess_race_has_one_delete_and_releases_locks(self):
        with durable_transaction_database(
            suffix="concurrency-cleanup-process"
        ) as database:
            authority = key_authority()
            harness = reconstructed_gateway(
                subject="google-subject-concurrency-cleanup-process"
            )
            try:
                _create_terminal_row(
                    database.connection,
                    harness.gateway,
                    authority,
                    "invalidated",
                )
                program = r"""
import json
import sys
from tests.google_oidc_authorization_transactions_test_support import (
    ManualClock, NOW, key_authority, open_connection, reconstructed_gateway,
    sockets_blocked,
)
from wahojobs.google_oidc_authorization_transaction_repository import (
    cleanup_google_oidc_authorization_transactions,
)
connection = open_connection(sys.argv[1], timeout=3.0)
authority = key_authority()
clock = ManualClock(NOW)
clock.advance(1)
harness = reconstructed_gateway(clock=clock)
try:
    with sockets_blocked():
        result = cleanup_google_oidc_authorization_transactions(
            connection,
            harness.gateway,
            authority,
            limit=1,
            terminal_retention_seconds=1,
        )
    print(json.dumps({"deleted": result.deleted_count}, sort_keys=True))
finally:
    connection.close()
    harness.close()
    authority.close()
"""
                processes = tuple(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-B",
                            "-c",
                            program,
                            str(database.path),
                        ],
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env={
                            **os.environ,
                            "PYTHONDONTWRITEBYTECODE": "1",
                        },
                    )
                    for _index in range(2)
                )
                deleted = []
                for process in processes:
                    stdout, stderr = process.communicate(timeout=30)
                    self.assertEqual(process.returncode, 0, stderr)
                    self.assertEqual(stderr, "")
                    deleted.append(json.loads(stdout)["deleted"])
                    self.assertIsNotNone(process.poll())
                self.assertEqual(sum(deleted), 1)
                self.assertEqual(transaction_rows(database.connection), ())
                probe = open_connection(database.path, timeout=1.0)
                try:
                    probe.execute("BEGIN IMMEDIATE")
                    probe.rollback()
                finally:
                    probe.close()
            finally:
                harness.close()
                authority.close()

    def test_cleanup_ordering_is_insertion_and_hash_seed_independent(self):
        program = r"""
import json
import sys
from tests.google_oidc_authorization_transactions_test_support import (
    ManualClock, NOW, durable_transaction_database, key_authority,
    reconstructed_gateway, sockets_blocked, transaction_rows,
)
from wahojobs.google_oidc_authorization_transaction_repository import (
    cleanup_google_oidc_authorization_transactions,
    prepare_google_oidc_authorization_transaction,
)
order = tuple(sys.argv[1].split(","))
with durable_transaction_database(suffix="cleanup-hash-seed") as database:
    clock = ManualClock(NOW)
    authority = key_authority()
    harness = reconstructed_gateway(clock=clock)
    try:
        for lifecycle in order:
            with sockets_blocked():
                prepared = prepare_google_oidc_authorization_transaction(
                    database.connection, harness.gateway, authority
                )
            row = next(
                item for item in transaction_rows(database.connection)
                if item["transaction_id"] == prepared.transaction_id
            )
            terminal = row["created_at"]
            database.connection.execute(
                "UPDATE google_oidc_authorization_transactions "
                "SET lifecycle=?, claimed_at=?, terminal_at=?, row_version=2 "
                "WHERE transaction_id=?",
                (
                    lifecycle,
                    terminal if lifecycle == "consumed" else None,
                    terminal,
                    prepared.transaction_id,
                ),
            )
            database.connection.commit()
            prepared.close()
        clock.advance(1)
        with sockets_blocked():
            result = cleanup_google_oidc_authorization_transactions(
                database.connection,
                harness.gateway,
                authority,
                limit=1,
                terminal_retention_seconds=1,
            )
        print(json.dumps({
            "result": result.as_dict(),
            "remaining": [
                row["lifecycle"]
                for row in transaction_rows(database.connection)
            ],
        }, sort_keys=True))
    finally:
        harness.close()
        authority.close()
"""
        outputs = []
        for seed in ("1", "7", "31", "101"):
            for order in (
                "consumed,invalidated",
                "invalidated,consumed",
            ):
                environment = {
                    **os.environ,
                    "PYTHONHASHSEED": seed,
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
                result = subprocess.run(
                    [sys.executable, "-B", "-c", program, order],
                    text=True,
                    capture_output=True,
                    timeout=30,
                    check=False,
                    env=environment,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                outputs.append(result.stdout)
        self.assertEqual(len(set(outputs)), 1)


if __name__ == "__main__":
    unittest.main()
