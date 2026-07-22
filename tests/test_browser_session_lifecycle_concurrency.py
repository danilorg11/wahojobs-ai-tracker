from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading
import time
import unittest

from tests.browser_session_authentication_test_support import (
    browser_request,
    read_only_connection,
)
from tests.browser_session_lifecycle_test_support import (
    NOW,
    consume_issued,
    connect,
    create_command,
    create_browser_session,
    lifecycle_database,
    revoke_command,
    revoke_browser_session,
    rotate_command,
    rotate_browser_session,
    session_row,
    token_from_cookie_header,
)
from wahojobs.browser_session_authentication import (
    DurableBrowserSessionAuthenticationGateway,
)
from wahojobs.browser_session_lifecycle import (
    BrowserSessionLifecycleError,
)
from wahojobs.persistent_profiles_application import TrustedAuthenticatedBrowserActor


def _run(path, operation, *, timeout=5.0):
    connection = connect(path, timeout=timeout)
    try:
        return operation(connection)
    finally:
        connection.close()


def _status(operation):
    try:
        return operation().status
    except BrowserSessionLifecycleError as error:
        return error.code


class BrowserSessionLifecycleConcurrencyTests(unittest.TestCase):
    def test_simultaneous_consumption_has_one_success_and_one_completed_result(self):
        with lifecycle_database(suffix="concurrent-consume") as (
            _path,
            connection,
            created,
        ):
            issued = create_browser_session(connection, create_command(created))
            barrier = threading.Barrier(2)

            def consume_status():
                barrier.wait(timeout=5)
                try:
                    consume_issued(issued)
                    return "consumed"
                except BrowserSessionLifecycleError as error:
                    return error.code

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = sorted(
                    future.result()
                    for future in (
                        pool.submit(consume_status),
                        pool.submit(consume_status),
                    )
                )
            self.assertEqual(outcomes, ["already_completed", "consumed"])
            self.assertEqual(issued.status, "consumed")

    def test_simultaneous_exact_creation_produces_one_row_and_safe_replay(self):
        with lifecycle_database(suffix="concurrent-create") as (path, connection, created):
            command = create_command(created)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        _status,
                        lambda: _run(
                            path,
                            lambda candidate: create_browser_session(candidate, command),
                        ),
                    )
                    for _ in range(2)
                ]
                outcomes = sorted(future.result() for future in futures)
            self.assertEqual(outcomes, ["already_completed", "issued"])
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                1,
            )

    def test_changed_creation_under_same_key_has_one_winner_and_one_conflict(self):
        with lifecycle_database(suffix="concurrent-create-conflict") as (
            path,
            connection,
            created,
        ):
            commands = (
                create_command(created),
                create_command(created, idle_ttl=timedelta(hours=2)),
            )
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        _status,
                        lambda command=command: _run(
                            path,
                            lambda candidate: create_browser_session(candidate, command),
                        ),
                    )
                    for command in commands
                ]
                outcomes = sorted(future.result() for future in futures)
            self.assertEqual(outcomes, ["idempotency_conflict", "issued"])
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                1,
            )

    def test_simultaneous_exact_rotation_creates_one_replacement_edge(self):
        with lifecycle_database(suffix="concurrent-rotate") as (path, connection, created):
            create_browser_session(connection, create_command(created))
            predecessor = session_row(connection, key="browser-session-create-001")
            command = rotate_command(created.user.user_id, predecessor["session_id"])
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        _status,
                        lambda: _run(
                            path,
                            lambda candidate: rotate_browser_session(candidate, command),
                        ),
                    )
                    for _ in range(2)
                ]
                outcomes = sorted(future.result() for future in futures)
            self.assertEqual(outcomes, ["already_completed", "issued"])
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_session_rotations").fetchone()[0],
                1,
            )

    def test_competing_rotation_keys_create_no_duplicate_lineage(self):
        with lifecycle_database(suffix="concurrent-rotate-conflict") as (
            path,
            connection,
            created,
        ):
            create_browser_session(connection, create_command(created))
            predecessor = session_row(connection, key="browser-session-create-001")
            commands = (
                rotate_command(
                    created.user.user_id,
                    predecessor["session_id"],
                    key="browser-session-rotate-a",
                ),
                rotate_command(
                    created.user.user_id,
                    predecessor["session_id"],
                    key="browser-session-rotate-b",
                ),
            )
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        _status,
                        lambda command=command: _run(
                            path,
                            lambda candidate: rotate_browser_session(candidate, command),
                        ),
                    )
                    for command in commands
                ]
                outcomes = [future.result() for future in futures]
            self.assertEqual(outcomes.count("issued"), 1)
            self.assertEqual(outcomes.count("stale_session"), 1)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_session_rotations").fetchone()[0],
                1,
            )

    def test_rotation_versus_revocation_has_one_atomic_winner(self):
        with lifecycle_database(suffix="rotate-revoke") as (path, connection, created):
            create_browser_session(connection, create_command(created))
            target = session_row(connection, key="browser-session-create-001")
            rotate = rotate_command(created.user.user_id, target["session_id"])
            revoke = revoke_command(created.user.user_id, target["session_id"])
            with ThreadPoolExecutor(max_workers=2) as pool:
                rotate_future = pool.submit(
                    _status,
                    lambda: _run(
                        path,
                        lambda candidate: rotate_browser_session(candidate, rotate),
                    ),
                )
                revoke_future = pool.submit(
                    _status,
                    lambda: _run(
                        path,
                        lambda candidate: revoke_browser_session(candidate, revoke),
                    ),
                )
                outcomes = {rotate_future.result(), revoke_future.result()}
            self.assertEqual(len(outcomes & {"issued", "revoked"}), 1)
            self.assertTrue(outcomes & {"stale_session", "session_state_conflict"})
            self.assertLessEqual(
                connection.execute("SELECT COUNT(*) FROM account_session_rotations").fetchone()[0],
                1,
            )
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_two_exact_revocations_are_idempotent(self):
        with lifecycle_database(suffix="concurrent-revoke") as (path, connection, created):
            create_browser_session(connection, create_command(created))
            target = session_row(connection, key="browser-session-create-001")
            command = revoke_command(created.user.user_id, target["session_id"])
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        _status,
                        lambda: _run(
                            path,
                            lambda candidate: revoke_browser_session(candidate, command),
                        ),
                    )
                    for _ in range(2)
                ]
                outcomes = sorted(future.result() for future in futures)
            self.assertEqual(outcomes, ["already_completed", "revoked"])
            stored = session_row(connection, session_id=target["session_id"])
            self.assertEqual(stored["session_version"], 2)
            self.assertEqual(stored["revoke_reason"], "explicit_revoke")

    def test_account_suspension_waits_for_inflight_creation_transaction(self):
        with lifecycle_database(suffix="suspend-create") as (path, connection, created):
            entered = threading.Event()
            release = threading.Event()

            def create_operation(candidate):
                def pause(point):
                    if point == "after_idempotency_lookup":
                        entered.set()
                        release.wait(timeout=5)

                return create_browser_session(
                    candidate,
                    create_command(created),
                    _failure_injector=pause,
                ).status

            def suspend_operation(candidate):
                candidate.execute("BEGIN IMMEDIATE")
                candidate.execute(
                    "UPDATE users SET lifecycle_status = 'suspended', row_version = row_version + 1, "
                    "updated_at = ? WHERE user_id = ?",
                    ((NOW + timedelta(minutes=1)).isoformat(), created.user.user_id),
                )
                candidate.commit()
                return "suspended"

            with ThreadPoolExecutor(max_workers=2) as pool:
                create_future = pool.submit(_run, path, create_operation)
                self.assertTrue(entered.wait(timeout=5))
                suspend_future = pool.submit(_run, path, suspend_operation)
                time.sleep(0.05)
                self.assertFalse(suspend_future.done())
                release.set()
                self.assertEqual(create_future.result(), "issued")
                self.assertEqual(suspend_future.result(), "suspended")
            self.assertEqual(
                connection.execute("SELECT lifecycle_status FROM users").fetchone()[0],
                "suspended",
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                1,
            )

    def test_identity_corruption_serializes_with_creation_and_fails_closed(self):
        with lifecycle_database(suffix="identity-corruption") as (path, connection, created):
            blocker = connect(path)
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute("PRAGMA ignore_check_constraints = ON")
            blocker.execute(
                "UPDATE auth_identities SET last_authenticated_at = 'not-a-time' "
                "WHERE auth_identity_id = ?",
                (created.identity.auth_identity_id,),
            )
            outcome = None
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    _status,
                    lambda: _run(
                        path,
                        lambda candidate: create_browser_session(
                            candidate,
                            create_command(created),
                        ),
                    ),
                )
                time.sleep(0.05)
                self.assertFalse(future.done())
                blocker.commit()
                outcome = future.result()
            blocker.close()
            self.assertEqual(outcome, "internal_consistency_failure")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )

    def test_lock_contention_returns_sanitized_domain_error(self):
        with lifecycle_database(suffix="lock-contention") as (path, connection, created):
            connection.execute("BEGIN IMMEDIATE")
            try:
                outcome = _status(
                    lambda: _run(
                        path,
                        lambda candidate: create_browser_session(
                            candidate,
                            create_command(created),
                        ),
                        timeout=0.05,
                    )
                )
            finally:
                connection.rollback()
            self.assertEqual(outcome, "temporary_contention")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )

    def test_authentication_observes_only_pre_or_post_revocation_state(self):
        with lifecycle_database(suffix="auth-revoke") as (path, connection, created):
            issued = create_browser_session(connection, create_command(created))
            token = token_from_cookie_header(consume_issued(issued).set_cookie_header)
            target = session_row(connection, key="browser-session-create-001")
            command = revoke_command(created.user.user_id, target["session_id"])
            gateway = DurableBrowserSessionAuthenticationGateway(
                trusted_environment_namespace="test",
                clock=lambda: NOW + timedelta(minutes=6),
            )

            def authenticate():
                with read_only_connection(path) as candidate:
                    actor = gateway.authenticate_browser_request(
                        candidate,
                        browser_request(token),
                    )
                    return "actor" if actor is not None else "none"

            with ThreadPoolExecutor(max_workers=2) as pool:
                auth_future = pool.submit(authenticate)
                revoke_future = pool.submit(
                    _status,
                    lambda: _run(
                        path,
                        lambda candidate: revoke_browser_session(candidate, command),
                    ),
                )
                self.assertIn(auth_future.result(), {"actor", "none"})
                self.assertEqual(revoke_future.result(), "revoked")
            self.assertEqual(authenticate(), "none")

    def test_authentication_during_rotation_observes_only_valid_pre_or_post_state(self):
        for iteration in range(50):
            with lifecycle_database(suffix=f"auth-rotate-{iteration}") as (
                path,
                connection,
                created,
            ):
                issued = create_browser_session(connection, create_command(created))
                predecessor_token = token_from_cookie_header(
                    consume_issued(issued).set_cookie_header
                )
                predecessor = session_row(connection, key="browser-session-create-001")
                command = rotate_command(created.user.user_id, predecessor["session_id"])
                gateway = DurableBrowserSessionAuthenticationGateway(
                    trusted_environment_namespace="test",
                    clock=lambda: NOW + timedelta(minutes=6),
                )
                barrier = threading.Barrier(2)

                def authenticate_before_rotation(token):
                    with read_only_connection(path) as candidate:
                        candidate.execute("BEGIN")
                        candidate.execute(
                            "SELECT COUNT(*) FROM account_sessions"
                        ).fetchone()
                        barrier.wait(timeout=5)
                        return gateway.authenticate_browser_request(
                            candidate,
                            browser_request(token),
                        )

                def rotate_after_reader_snapshot():
                    barrier.wait(timeout=5)
                    return _run(
                        path,
                        lambda candidate: rotate_browser_session(candidate, command),
                    )

                with ThreadPoolExecutor(max_workers=2) as pool:
                    auth_future = pool.submit(
                        authenticate_before_rotation,
                        predecessor_token,
                    )
                    rotate_future = pool.submit(rotate_after_reader_snapshot)
                    before = auth_future.result()
                    replacement = rotate_future.result()
                self.assertIs(type(before), TrustedAuthenticatedBrowserActor)
                replacement_token = token_from_cookie_header(
                    consume_issued(
                        replacement,
                        now=NOW + timedelta(minutes=5),
                    ).set_cookie_header
                )

                def authenticate_after_rotation(token):
                    with read_only_connection(path) as candidate:
                        return gateway.authenticate_browser_request(
                            candidate,
                            browser_request(token),
                        )

                self.assertIsNone(authenticate_after_rotation(predecessor_token))
                self.assertIs(
                    type(authenticate_after_rotation(replacement_token)),
                    TrustedAuthenticatedBrowserActor,
                )


if __name__ == "__main__":
    unittest.main()
