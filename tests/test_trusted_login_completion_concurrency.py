from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import sqlite3
import threading
import unittest

from tests.accounts_test_support import create_user
from tests.browser_session_lifecycle_test_support import (
    consume_issued,
    request_secret_vault,
    revoke_browser_session,
    revoke_command,
    rotate_browser_session,
    rotate_command,
)
from tests.trusted_login_completion_test_support import (
    NOW,
    close_secret_vault,
    complete_login,
    connect,
    consume_login,
    login_database,
    trusted_assertion,
    vault_entry_count,
)


class TrustedLoginCompletionConcurrencyTests(unittest.TestCase):
    def test_concurrent_exact_completion_issues_one_credential_pair(self):
        with login_database(suffix="concurrent-exact") as (path, connection, created):
            assertions = (trusted_assertion(created), trusted_assertion(created))
            barrier = threading.Barrier(2)

            def worker(assertion):
                candidate = connect(path, timeout=5.0)
                vault = None
                try:
                    barrier.wait(timeout=5.0)
                    result, vault = complete_login(candidate, assertion)
                    delivered = False
                    if result.status == "issued":
                        consume_login(result, vault)
                        delivered = True
                    entries = vault_entry_count(vault)
                    return result.status, delivered, entries
                finally:
                    if vault is not None:
                        close_secret_vault(vault)
                    candidate.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(worker, assertions))
            self.assertEqual(sorted(item[0] for item in outcomes), ["already_completed", "issued"])
            self.assertEqual(sum(item[1] for item in outcomes), 1)
            self.assertEqual(sum(item[2] for item in outcomes), 0)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                1,
            )

    def test_concurrent_changed_completion_conflicts_without_losing_credentials(self):
        with login_database(suffix="concurrent-changed") as (path, connection, created):
            assertions = (
                trusted_assertion(created),
                trusted_assertion(
                    created,
                    expires_at=NOW + timedelta(minutes=6),
                ),
            )
            barrier = threading.Barrier(2)

            def worker(assertion):
                candidate = connect(path, timeout=5.0)
                vault = None
                try:
                    barrier.wait(timeout=5.0)
                    result, vault = complete_login(candidate, assertion)
                    delivered = False
                    if result.status == "issued":
                        consume_login(result, vault)
                        delivered = True
                    return result.status, delivered, vault_entry_count(vault)
                finally:
                    if vault is not None:
                        close_secret_vault(vault)
                    candidate.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(worker, assertions))
            self.assertEqual(
                sorted(item[0] for item in outcomes),
                ["idempotency_conflict", "issued"],
            )
            self.assertEqual(sum(item[1] for item in outcomes), 1)
            self.assertEqual(sum(item[2] for item in outcomes), 0)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                1,
            )

    def test_account_suspension_committed_during_wait_denies_completion(self):
        with login_database(suffix="concurrent-suspend") as (path, connection, created):
            assertion = trusted_assertion(created)
            changed = threading.Event()
            release = threading.Event()

            def suspend():
                candidate = connect(path, timeout=5.0)
                try:
                    candidate.execute("BEGIN IMMEDIATE")
                    candidate.execute(
                        "UPDATE users SET lifecycle_status = 'suspended' WHERE user_id = ?",
                        (created.user.user_id,),
                    )
                    changed.set()
                    release.wait(timeout=5.0)
                    candidate.commit()
                finally:
                    candidate.close()

            def login():
                changed.wait(timeout=5.0)
                candidate = connect(path, timeout=5.0)
                vault = None
                try:
                    result, vault = complete_login(candidate, assertion)
                    return result.status, vault_entry_count(vault)
                finally:
                    if vault is not None:
                        close_secret_vault(vault)
                    candidate.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                suspended = pool.submit(suspend)
                completed = pool.submit(login)
                self.assertTrue(changed.wait(timeout=5.0))
                release.set()
                suspended.result(timeout=5.0)
                outcome = completed.result(timeout=5.0)
            self.assertEqual(outcome, ("authentication_denied", 0))
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )

    def test_identity_corruption_committed_during_wait_is_unavailable(self):
        with login_database(suffix="concurrent-identity") as (path, connection, created):
            assertion = trusted_assertion(created)
            changed = threading.Event()
            release = threading.Event()

            def corrupt():
                candidate = connect(path, timeout=5.0)
                try:
                    candidate.execute("BEGIN IMMEDIATE")
                    candidate.execute("PRAGMA ignore_check_constraints = ON")
                    candidate.execute(
                        "UPDATE auth_identities SET request_fingerprint = 'private-race-marker' "
                        "WHERE auth_identity_id = ?",
                        (created.identity.auth_identity_id,),
                    )
                    candidate.execute("PRAGMA ignore_check_constraints = OFF")
                    changed.set()
                    release.wait(timeout=5.0)
                    candidate.commit()
                finally:
                    candidate.close()

            def login():
                changed.wait(timeout=5.0)
                candidate = connect(path, timeout=5.0)
                vault = None
                try:
                    result, vault = complete_login(candidate, assertion)
                    return result.status, repr(result), vault_entry_count(vault)
                finally:
                    if vault is not None:
                        close_secret_vault(vault)
                    candidate.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                corrupted = pool.submit(corrupt)
                completed = pool.submit(login)
                self.assertTrue(changed.wait(timeout=5.0))
                release.set()
                corrupted.result(timeout=5.0)
                outcome = completed.result(timeout=5.0)
            self.assertEqual(outcome[0], "unavailable")
            self.assertNotIn("private-race-marker", outcome[1])
            self.assertEqual(outcome[2], 0)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )

    def test_identity_reassignment_race_is_rejected_before_completion_proceeds(self):
        with login_database(suffix="concurrent-reassignment") as (path, connection, created):
            _invitation, second = create_user(
                connection,
                suffix="concurrent-reassignment-target",
                now=NOW,
            )
            assertion = trusted_assertion(created)
            attempted = threading.Event()
            release = threading.Event()

            def reassign():
                candidate = connect(path, timeout=5.0)
                rejected = False
                try:
                    candidate.execute("BEGIN IMMEDIATE")
                    try:
                        candidate.execute(
                            "UPDATE auth_identities SET user_id = ? WHERE auth_identity_id = ?",
                            (
                                second.user.user_id,
                                created.identity.auth_identity_id,
                            ),
                        )
                    except sqlite3.IntegrityError:
                        rejected = True
                    attempted.set()
                    release.wait(timeout=5.0)
                    candidate.rollback()
                    return rejected
                finally:
                    candidate.close()

            def login():
                attempted.wait(timeout=5.0)
                candidate = connect(path, timeout=5.0)
                vault = None
                try:
                    result, vault = complete_login(candidate, assertion)
                    if result.status == "issued":
                        consume_login(result, vault)
                    return result.status, vault_entry_count(vault)
                finally:
                    if vault is not None:
                        close_secret_vault(vault)
                    candidate.close()

            with ThreadPoolExecutor(max_workers=2) as pool:
                reassigned = pool.submit(reassign)
                completed = pool.submit(login)
                self.assertTrue(attempted.wait(timeout=5.0))
                release.set()
                self.assertTrue(reassigned.result(timeout=5.0))
                outcome = completed.result(timeout=5.0)
            self.assertEqual(outcome, ("issued", 0))
            identity = connection.execute(
                "SELECT user_id FROM auth_identities WHERE auth_identity_id = ?",
                (created.identity.auth_identity_id,),
            ).fetchone()
            self.assertEqual(identity["user_id"], created.user.user_id)

    def test_replay_racing_rotation_and_revocation_delivers_no_replay_credential(self):
        for mutation in ("rotation", "revocation"):
            with self.subTest(mutation=mutation):
                with login_database(suffix=f"concurrent-{mutation}") as (
                    path,
                    connection,
                    created,
                ):
                    assertion = trusted_assertion(created)
                    first, first_vault = complete_login(connection, assertion)
                    consume_login(first, first_vault)
                    close_secret_vault(first_vault)
                    session = connection.execute("SELECT * FROM account_sessions").fetchone()
                    barrier = threading.Barrier(2)

                    def mutate():
                        candidate = connect(path, timeout=5.0)
                        vault = None
                        try:
                            barrier.wait(timeout=5.0)
                            if mutation == "rotation":
                                vault = request_secret_vault()
                                result = rotate_browser_session(
                                    candidate,
                                    rotate_command(
                                        created.user.user_id,
                                        session["session_id"],
                                        accepted_at=NOW + timedelta(minutes=2),
                                    ),
                                    secret_vault=vault,
                                    _clock=lambda: NOW + timedelta(minutes=2),
                                )
                                consume_issued(
                                    result,
                                    vault=vault,
                                    now=NOW + timedelta(minutes=2),
                                )
                                return result.status
                            result = revoke_browser_session(
                                candidate,
                                revoke_command(
                                    created.user.user_id,
                                    session["session_id"],
                                    accepted_at=NOW + timedelta(minutes=2),
                                ),
                                _clock=lambda: NOW + timedelta(minutes=2),
                            )
                            return result.status
                        finally:
                            if vault is not None:
                                close_secret_vault(vault)
                            candidate.close()

                    def replay():
                        candidate = connect(path, timeout=5.0)
                        vault = None
                        try:
                            barrier.wait(timeout=5.0)
                            result, vault = complete_login(
                                candidate,
                                assertion,
                                trusted_now=NOW + timedelta(minutes=3),
                            )
                            return result.status, vault_entry_count(vault)
                        finally:
                            if vault is not None:
                                close_secret_vault(vault)
                            candidate.close()

                    with ThreadPoolExecutor(max_workers=2) as pool:
                        mutation_future = pool.submit(mutate)
                        replay_future = pool.submit(replay)
                        mutation_status = mutation_future.result(timeout=10.0)
                        replay_status = replay_future.result(timeout=10.0)
                    self.assertIn(mutation_status, {"consumed", "revoked"})
                    self.assertEqual(replay_status, ("already_completed", 0))

    def test_lock_contention_is_sanitized_and_credential_free(self):
        with login_database(suffix="concurrent-lock") as (path, connection, created):
            assertion = trusted_assertion(created)
            connection.execute("BEGIN IMMEDIATE")
            candidate = connect(path, timeout=0.05)
            vault = None
            try:
                result, vault = complete_login(candidate, assertion)
                self.assertEqual(result.status, "unavailable")
                self.assertEqual(vault_entry_count(vault), 0)
                self.assertNotIn(str(path), repr(result))
            finally:
                if vault is not None:
                    close_secret_vault(vault)
                candidate.close()
                connection.rollback()
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
