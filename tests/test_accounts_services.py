import base64
import concurrent.futures
import hashlib
import sqlite3
import tempfile
import unittest
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

from tests.accounts_test_support import (
    ACCOUNT_SERVICE,
    IDENTITY_VERIFIER,
    INVITATION_KEY,
    NOW,
    connect,
    create_user,
    install_accounts,
    trusted_identity,
)
from wahojobs import accounts


class AccountsServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "accounts.sqlite"
        self.conn = install_accounts(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def create_user(self, suffix="one"):
        return create_user(self.conn, suffix)

    def create_session(self, user_id, suffix="one", *, now=NOW):
        return accounts.create_session(
            self.conn,
            user_id=user_id,
            idle_ttl=timedelta(hours=1),
            absolute_ttl=timedelta(days=7),
            idempotency_key=f"session-create-{suffix}",
            now=now,
        )


class IdentityInvitationTests(AccountsServiceTestCase):
    def invite(self, email, suffix, *, expires_at=None):
        return accounts.create_invitation(
            self.conn,
            email=email,
            lookup_key=INVITATION_KEY,
            expires_at=expires_at or NOW + timedelta(days=1),
            created_by="test_admin",
            idempotency_key=f"invite-{suffix}",
            now=NOW,
        )

    def test_random_ids_and_public_results_hide_identity_and_invitation_secrets(self):
        invitation, created = self.create_user()
        self.assertRegex(created.user.user_id, r"^usr_[0-9a-f]{32}$")
        self.assertRegex(created.identity.auth_identity_id, r"^auth_[0-9a-f]{32}$")
        self.assertRegex(invitation.invitation_id, r"^inv_[0-9a-f]{32}$")
        self.assertNotIn("provider_subject", asdict(created.identity))
        self.assertNotIn("verified_email", asdict(created.identity))
        self.assertNotIn("hmac", repr(invitation).lower())
        creation = self.invite("repr@example.test", "repr")
        self.assertNotIn(creation.invitation_token, repr(creation))
        self.assertEqual(created.user.lifecycle_status, "active")

    def test_verifier_bound_identity_rejects_forgery_other_verifier_and_mutation(self):
        creation = self.invite("person@example.test", "person")
        with self.assertRaises(accounts.AuthenticationUnavailable):
            ACCOUNT_SERVICE.create_invited_user(
                self.conn,
                identity={"provider": "google", "provider_subject": "browser-claim"},
                invitation_token=creation.invitation_token,
                invitation_lookup_key=INVITATION_KEY,
                idempotency_key="user-browser-claim",
                now=NOW,
            )
        with self.assertRaises(TypeError):
            accounts.VerifiedProviderIdentity()
        other_verifier = accounts.TrustedIdentityVerifier()
        other_identity = other_verifier.from_validated_google_claims(
            provider_subject="subject",
            verified_email="person@example.test",
            email_verified=True,
            authenticated_at=NOW,
            metadata_version="google_oidc_v1",
        )
        with self.assertRaises(accounts.AuthenticationUnavailable):
            ACCOUNT_SERVICE.create_invited_user(
                self.conn,
                identity=other_identity,
                invitation_token=creation.invitation_token,
                invitation_lookup_key=INVITATION_KEY,
                idempotency_key="user-other-verifier",
                now=NOW,
            )
        identity = trusted_identity("subject", "person@example.test")
        object.__setattr__(identity, "_provider_subject", "altered")
        with self.assertRaises(accounts.AuthenticationUnavailable):
            ACCOUNT_SERVICE.create_invited_user(
                self.conn,
                identity=identity,
                invitation_token=creation.invitation_token,
                invitation_lookup_key=INVITATION_KEY,
                idempotency_key="user-altered-identity",
                now=NOW,
            )
        self.assertNotIn("subject", repr(other_identity))
        self.assertNotIn("person@example", repr(other_identity))

    def test_timing_safe_invitation_token_expiry_revocation_and_replay(self):
        creation = self.invite("valid@example.test", "valid")
        identity = trusted_identity("valid-subject", "valid@example.test")
        calls = []
        original = accounts.hmac.compare_digest

        def compared(left, right):
            calls.append((len(left), len(right)))
            return original(left, right)

        accounts.hmac.compare_digest = compared
        try:
            created = ACCOUNT_SERVICE.create_invited_user(
                self.conn,
                identity=identity,
                invitation_token=creation.invitation_token,
                invitation_lookup_key=INVITATION_KEY,
                idempotency_key="valid-user-create",
                now=NOW,
            )
            replay = ACCOUNT_SERVICE.create_invited_user(
                self.conn,
                identity=identity,
                invitation_token=creation.invitation_token,
                invitation_lookup_key=INVITATION_KEY,
                idempotency_key="valid-user-create",
                now=NOW,
            )
            wrong = creation.invitation_token[:-1] + (
                "A" if creation.invitation_token[-1] != "A" else "B"
            )
            with self.assertRaises(accounts.AuthenticationUnavailable):
                ACCOUNT_SERVICE.create_invited_user(
                    self.conn,
                    identity=identity,
                    invitation_token=wrong,
                    invitation_lookup_key=INVITATION_KEY,
                    idempotency_key="wrong-secret",
                    now=NOW,
                )
        finally:
            accounts.hmac.compare_digest = original
        self.assertGreaterEqual(len(calls), 6)
        self.assertEqual(created, replay)

        with self.assertRaises(accounts.AuthenticationUnavailable):
            ACCOUNT_SERVICE.create_invited_user(
                self.conn, identity=identity, invitation_token="inv_" + "0" * 32 + "." + "x" * 43,
                invitation_lookup_key=INVITATION_KEY, idempotency_key="wrong-id", now=NOW,
            )

        expired = self.invite(
            "expired@example.test", "expired", expires_at=NOW + timedelta(minutes=1)
        )
        with self.assertRaises(accounts.AuthenticationUnavailable):
            ACCOUNT_SERVICE.create_invited_user(
                self.conn, identity=trusted_identity("expired", "expired@example.test"),
                invitation_token=expired.invitation_token, invitation_lookup_key=INVITATION_KEY,
                idempotency_key="expired-user", now=NOW + timedelta(minutes=2),
            )
        revoked = self.invite("revoked@example.test", "revoked")
        accounts.revoke_invitation(
            self.conn, invitation_id=revoked.invitation.invitation_id, now=NOW
        )
        with self.assertRaises(accounts.AuthenticationUnavailable):
            ACCOUNT_SERVICE.create_invited_user(
                self.conn, identity=trusted_identity("revoked", "revoked@example.test"),
                invitation_token=revoked.invitation_token, invitation_lookup_key=INVITATION_KEY,
                idempotency_key="revoked-user", now=NOW,
            )

    def test_identity_provider_cardinality_and_subject_immutability(self):
        _, created = self.create_user("identity-owner")
        with self.assertRaises(accounts.AuthenticationUnavailable):
            ACCOUNT_SERVICE.link_verified_identity(
                self.conn,
                user_id=created.user.user_id,
                identity=trusted_identity("second-subject", "second@example.test"),
                idempotency_key="link-second-google",
                now=NOW,
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE auth_identities SET provider_subject='changed' WHERE user_id=?",
                (created.user.user_id,),
            )

    def test_invitation_failures_are_publicly_indistinguishable(self):
        creation = self.invite("uniform@example.test", "uniform")
        identity = trusted_identity("uniform-subject", "uniform@example.test")
        errors = []

        def capture(**overrides):
            arguments = {
                "identity": identity,
                "invitation_token": creation.invitation_token,
                "invitation_lookup_key": INVITATION_KEY,
                "idempotency_key": "uniform-consume-key",
                "now": NOW,
                **overrides,
            }
            try:
                ACCOUNT_SERVICE.create_invited_user(self.conn, **arguments)
            except Exception as exc:
                errors.append((type(exc), str(exc), vars(exc)))
            else:
                self.fail("Expected invitation authentication to fail")

        capture(invitation_token="malformed")
        capture(invitation_token="inv_" + "f" * 32 + "." + "x" * 43)
        capture(invitation_lookup_key=b"wrong-test-key-material-at-least-32-bytes")
        capture(invitation_hash_version="hmac_sha256_v2")
        accounts.revoke_invitation(
            self.conn, invitation_id=creation.invitation.invitation_id, now=NOW
        )
        capture()
        self.assertEqual({item[0] for item in errors}, {accounts.AuthenticationUnavailable})
        self.assertEqual(
            {item[1] for item in errors}, {"Authentication could not be completed."}
        )
        self.assertEqual({tuple(item[2]) for item in errors}, {()})

    def test_concurrent_invitation_consumption_creates_one_user(self):
        creation = self.invite("race@example.test", "race")
        self.conn.close()

        def attempt(index):
            conn = connect(self.db_path)
            try:
                return ACCOUNT_SERVICE.create_invited_user(
                    conn,
                    identity=trusted_identity("race-subject", "race@example.test"),
                    invitation_token=creation.invitation_token,
                    invitation_lookup_key=INVITATION_KEY,
                    idempotency_key=f"race-user-{index}",
                    now=NOW,
                )
            except accounts.AuthenticationUnavailable:
                return None
            finally:
                conn.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (1, 2)))
        self.conn = connect(self.db_path)
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)


class SessionTests(AccountsServiceTestCase):
    def test_token_entropy_hash_only_resolution_and_hash_invisibility(self):
        _, created = self.create_user()
        session = self.create_session(created.user.user_id)
        padded = session.session_token + "=" * (-len(session.session_token) % 4)
        self.assertGreaterEqual(len(base64.urlsafe_b64decode(padded)), 32)
        row = self.conn.execute("SELECT * FROM account_sessions").fetchone()
        self.assertNotEqual(row["token_hash"], session.session_token)
        self.assertEqual(
            row["token_hash"], hashlib.sha256(session.session_token.encode()).hexdigest()
        )
        self.assertEqual(row["token_hash_version"], "sha256_v1")
        self.assertFalse({"token_hash", "csrf_secret_hash"} & set(asdict(session.session)))
        resolved = accounts.resolve_session(
            self.conn, session_token=session.session_token, now=NOW + timedelta(minutes=1)
        )
        self.assertEqual(resolved.session_id, session.session.session_id)
        with self.assertRaises(accounts.SessionUnavailable):
            accounts.resolve_session(self.conn, session_token="x" * 43, now=NOW)

    def test_idle_absolute_revoked_and_inactive_accounts_do_not_resolve(self):
        _, created = self.create_user()
        session = self.create_session(created.user.user_id)
        with self.assertRaises(accounts.SessionUnavailable):
            accounts.resolve_session(
                self.conn, session_token=session.session_token, now=NOW + timedelta(hours=2)
            )

        absolute = accounts.create_session(
            self.conn,
            user_id=created.user.user_id,
            idle_ttl=timedelta(days=2),
            absolute_ttl=timedelta(days=2),
            idempotency_key="session-absolute",
            now=NOW,
        )
        with self.assertRaises(accounts.SessionUnavailable):
            accounts.resolve_session(
                self.conn, session_token=absolute.session_token, now=NOW + timedelta(days=3)
            )
        revoked = accounts.create_session(
            self.conn,
            user_id=created.user.user_id,
            idle_ttl=timedelta(hours=1),
            absolute_ttl=timedelta(days=1),
            idempotency_key="session-revoked",
            now=NOW,
        )
        accounts.revoke_current_session(
            self.conn,
            session_token=revoked.session_token,
            expected_session_version=1,
            reason="user_logout",
            now=NOW,
        )
        with self.assertRaises(accounts.SessionUnavailable):
            accounts.resolve_session(self.conn, session_token=revoked.session_token, now=NOW)

        active = accounts.create_session(
            self.conn,
            user_id=created.user.user_id,
            idle_ttl=timedelta(hours=1),
            absolute_ttl=timedelta(days=1),
            idempotency_key="session-suspend",
            now=NOW,
        )
        accounts.suspend_user(
            self.conn,
            user_id=created.user.user_id,
            expected_version=1,
            source="test_admin",
            idempotency_key="suspend-user-one",
            now=NOW,
        )
        with self.assertRaises(accounts.SessionUnavailable):
            accounts.resolve_session(self.conn, session_token=active.session_token, now=NOW)

    def test_rotation_is_atomic_preserves_absolute_deadline_and_invalidates_old_token(self):
        _, created = self.create_user()
        original = self.create_session(created.user.user_id)
        rotated = accounts.rotate_session(
            self.conn,
            session_token=original.session_token,
            expected_session_version=1,
            idle_ttl=timedelta(hours=2),
            idempotency_key="rotate-session-one",
            now=NOW + timedelta(minutes=10),
        )
        old = self.conn.execute(
            "SELECT * FROM account_session_rotations WHERE predecessor_session_id = ?",
            (original.session.session_id,),
        ).fetchone()
        self.assertEqual(old["replacement_session_id"], rotated.session.session_id)
        self.assertEqual(rotated.session.parent_session_id, original.session.session_id)
        self.assertEqual(rotated.session.absolute_expires_at, original.session.absolute_expires_at)
        with self.assertRaises(accounts.SessionUnavailable):
            accounts.rotate_session(
                self.conn,
                session_token=original.session_token,
                expected_session_version=1,
                idle_ttl=timedelta(hours=2),
                idempotency_key="rotate-session-one",
                now=NOW + timedelta(minutes=10),
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0], 2)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM account_session_rotations").fetchone()[0],
            1,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE account_session_rotations SET created_at=created_at "
                "WHERE rotation_id=?",
                (old["rotation_id"],),
            )
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "DELETE FROM account_session_rotations WHERE rotation_id=?",
                (old["rotation_id"],),
            )
        self.conn.rollback()
        with self.assertRaises(accounts.SessionUnavailable):
            accounts.resolve_session(self.conn, session_token=original.session_token, now=NOW)
        self.assertEqual(
            accounts.resolve_session(
                self.conn, session_token=rotated.session_token, now=NOW + timedelta(minutes=11)
            ).session_id,
            rotated.session.session_id,
        )

    def test_concurrent_rotation_creates_one_replacement_lineage(self):
        _, created = self.create_user()
        original = self.create_session(created.user.user_id)
        token = original.session_token
        self.conn.close()

        def rotate(index):
            conn = connect(self.db_path)
            try:
                return accounts.rotate_session(
                    conn,
                    session_token=token,
                    expected_session_version=1,
                    idle_ttl=timedelta(hours=1),
                    idempotency_key=f"rotate-concurrent-{index}",
                    now=NOW + timedelta(minutes=1),
                )
            except accounts.SessionUnavailable:
                return None
            finally:
                conn.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(rotate, (1, 2)))
        self.conn = connect(self.db_path)
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0], 2)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM account_session_rotations WHERE predecessor_session_id = ?",
                (original.session.session_id,),
            ).fetchone()[0],
            1,
        )

    def test_revoke_all_concurrent_sessions_and_stale_versions(self):
        _, created = self.create_user()
        first = self.create_session(created.user.user_id, "first")
        second = self.create_session(created.user.user_id, "second")
        count = accounts.revoke_all_sessions(
            self.conn,
            user_id=created.user.user_id,
            expected_user_version=1,
            reason="security_reset",
            now=NOW,
        )
        self.assertEqual(count, 2)
        for token in (first.session_token, second.session_token):
            with self.assertRaises(accounts.SessionUnavailable):
                accounts.resolve_session(self.conn, session_token=token, now=NOW)
        with self.assertRaises(accounts.StaleAccountVersion):
            accounts.revoke_all_sessions(
                self.conn,
                user_id=created.user.user_id,
                expected_user_version=2,
                reason="stale",
                now=NOW,
            )

    def test_duplicate_session_creation_and_rotation_failure_boundaries_leave_no_partial_rows(self):
        _, created = self.create_user()
        original = self.create_session(created.user.user_id)
        with self.assertRaises(accounts.SessionUnavailable):
            accounts.create_session(
                self.conn,
                user_id=created.user.user_id,
                idle_ttl=timedelta(hours=1),
                absolute_ttl=timedelta(days=7),
                idempotency_key="session-create-one",
                now=NOW,
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0], 1)

        def fail_create(point):
            if point == "after_session_insert":
                raise RuntimeError(point)

        with self.assertRaises(RuntimeError):
            accounts.create_session(
                self.conn,
                user_id=created.user.user_id,
                idle_ttl=timedelta(hours=1),
                absolute_ttl=timedelta(days=7),
                idempotency_key="session-create-forced-failure",
                now=NOW,
                failure_injector=fail_create,
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0], 1)
        with self.assertRaises(accounts.StaleSessionVersion):
            accounts.rotate_session(
                self.conn,
                session_token=original.session_token,
                expected_session_version=2,
                idle_ttl=timedelta(hours=1),
                idempotency_key="rotation-stale-version",
                now=NOW + timedelta(minutes=1),
            )

        for index, point in enumerate(
            ("after_replacement_insert", "after_old_session_revoke", "after_rotation_edge_insert")
        ):
            with self.subTest(point=point):

                def fail(current):
                    if current == point:
                        raise RuntimeError(point)

                with self.assertRaises(RuntimeError):
                    accounts.rotate_session(
                        self.conn,
                        session_token=original.session_token,
                        expected_session_version=1,
                        idle_ttl=timedelta(hours=1),
                        idempotency_key=f"rotation-failure-{index}",
                        now=NOW + timedelta(minutes=1),
                        failure_injector=fail,
                    )
                self.assertEqual(
                    self.conn.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0], 1
                )
                self.assertEqual(
                    self.conn.execute("SELECT COUNT(*) FROM account_session_rotations").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    accounts.resolve_session(
                        self.conn, session_token=original.session_token, now=NOW + timedelta(minutes=2)
                    ).session_id,
                    original.session.session_id,
                )

    def test_two_connection_revoke_all_race_is_idempotently_safe(self):
        _, created = self.create_user()
        self.create_session(created.user.user_id, "race-first")
        self.create_session(created.user.user_id, "race-second")
        self.conn.close()

        def revoke(reason):
            conn = connect(self.db_path)
            try:
                return accounts.revoke_all_sessions(
                    conn,
                    user_id=created.user.user_id,
                    expected_user_version=1,
                    reason=reason,
                    now=NOW,
                )
            finally:
                conn.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            counts = list(pool.map(revoke, ("security_reset", "security_reset")))
        self.conn = connect(self.db_path)
        self.assertEqual(sorted(counts), [0, 2])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM account_sessions WHERE revoked_at IS NULL"
            ).fetchone()[0],
            0,
        )

    def test_account_lifecycle_check_blocks_otherwise_active_session_for_every_inactive_status(self):
        for index, status in enumerate(
            ("suspended", "deletion_requested", "deactivated_pending_purge")
        ):
            with self.subTest(status=status):
                _, created = self.create_user(f"inactive-{index}")
                session = self.create_session(created.user.user_id, f"inactive-{index}")
                deletion_requested_at = (
                    NOW.isoformat()
                    if status in {"deletion_requested", "deactivated_pending_purge"}
                    else None
                )
                deactivated_at = (
                    NOW.isoformat() if status == "deactivated_pending_purge" else None
                )
                self.conn.execute(
                    "UPDATE users SET lifecycle_status=?, deletion_requested_at=?, deactivated_at=? "
                    "WHERE user_id=?",
                    (
                        status,
                        deletion_requested_at,
                        deactivated_at,
                        created.user.user_id,
                    ),
                )
                self.conn.commit()
                with self.assertRaises(accounts.SessionUnavailable):
                    accounts.resolve_session(
                        self.conn, session_token=session.session_token, now=NOW
                    )


class ConsentLifecycleDeletionTests(AccountsServiceTestCase):
    def test_consent_is_append_only_effective_and_chain_validated(self):
        _, created = self.create_user()
        granted = accounts.append_consent_event(
            self.conn,
            user_id=created.user.user_id,
            purpose="privacy_policy",
            policy_version="2026-07",
            action="granted",
            source="account_setup",
            idempotency_key="consent-grant-privacy",
            occurred_at=NOW,
        )
        self.assertEqual(
            accounts.effective_consent(
                self.conn, user_id=created.user.user_id, purpose="privacy_policy"
            ),
            granted,
        )
        revoked = accounts.append_consent_event(
            self.conn,
            user_id=created.user.user_id,
            purpose="privacy_policy",
            policy_version="2026-07",
            action="revoked",
            source="account_settings",
            idempotency_key="consent-revoke-privacy",
            occurred_at=NOW + timedelta(minutes=1),
        )
        self.assertEqual(
            accounts.effective_consent(
                self.conn, user_id=created.user.user_id, purpose="privacy_policy"
            ),
            revoked,
        )
        with self.assertRaises(accounts.AccountStateConflict):
            accounts.append_consent_event(
                self.conn,
                user_id=created.user.user_id,
                purpose="privacy_policy",
                policy_version="2026-07",
                action="revoked",
                source="account_settings",
                idempotency_key="consent-repeat-revoke",
                occurred_at=NOW + timedelta(minutes=2),
            )
        for statement in (
            "UPDATE consent_events SET source='changed' WHERE consent_event_id=?",
            "DELETE FROM consent_events WHERE consent_event_id=?",
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(statement, (granted.consent_event_id,))

    def test_lifecycle_transitions_versions_append_only_and_stale_guard(self):
        _, created = self.create_user()
        suspended = accounts.suspend_user(
            self.conn,
            user_id=created.user.user_id,
            expected_version=1,
            source="test_admin",
            idempotency_key="lifecycle-suspend",
            now=NOW,
        )
        self.assertEqual((suspended.user.lifecycle_status, suspended.user.row_version), ("suspended", 2))
        with self.assertRaises(accounts.StaleAccountVersion):
            accounts.reactivate_user(
                self.conn,
                user_id=created.user.user_id,
                expected_version=1,
                source="test_admin",
                idempotency_key="lifecycle-stale-reactivate",
                now=NOW,
            )
        active = accounts.reactivate_user(
            self.conn,
            user_id=created.user.user_id,
            expected_version=2,
            source="test_admin",
            idempotency_key="lifecycle-reactivate",
            now=NOW + timedelta(minutes=1),
        )
        self.assertEqual((active.user.lifecycle_status, active.user.row_version), ("active", 3))
        versions = [
            (row[0], row[1])
            for row in self.conn.execute(
                "SELECT account_version_before, account_version_after "
                "FROM account_lifecycle_events ORDER BY account_version_after"
            )
        ]
        self.assertEqual(versions, [(0, 1), (1, 2), (2, 3)])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM account_lifecycle_events")

    def test_deletion_request_revokes_sessions_cancel_restores_and_deactivation_waits(self):
        _, created = self.create_user()
        session = self.create_session(created.user.user_id)
        requested = accounts.request_account_deletion(
            self.conn,
            user_id=created.user.user_id,
            expected_version=1,
            cooling_period=timedelta(days=7),
            purge_after=timedelta(days=30),
            request_source="account_settings",
            idempotency_key="deletion-request-one",
            now=NOW,
        )
        self.assertEqual(requested.user.lifecycle_status, "deletion_requested")
        self.assertEqual(requested.request.status, "pending_cooling")
        self.assertEqual(requested.revoked_session_count, 1)
        with self.assertRaises(accounts.SessionUnavailable):
            accounts.resolve_session(self.conn, session_token=session.session_token, now=NOW)
        pending_replay = accounts.request_account_deletion(
            self.conn,
            user_id=created.user.user_id,
            expected_version=2,
            cooling_period=timedelta(days=7),
            purge_after=timedelta(days=30),
            request_source="account_settings",
            idempotency_key="deletion-request-duplicate",
            now=NOW,
        )
        self.assertTrue(pending_replay.replayed)
        self.assertEqual(
            pending_replay.request.deletion_request_id,
            requested.request.deletion_request_id,
        )
        with self.assertRaises(accounts.AccountStateConflict):
            accounts.deactivate_account_after_cooling(
                self.conn,
                user_id=created.user.user_id,
                expected_version=2,
                source="retention_worker",
                idempotency_key="deletion-deactivate-early",
                deactivation_evidence={"evidence": "marker_only"},
                now=NOW + timedelta(days=1),
            )
        cancelled = accounts.cancel_deletion_request(
            self.conn,
            user_id=created.user.user_id,
            expected_version=2,
            source="account_settings",
            idempotency_key="deletion-cancel-one",
            now=NOW + timedelta(days=2),
        )
        self.assertEqual((cancelled.user.lifecycle_status, cancelled.request.status), ("active", "cancelled"))

        second = accounts.request_account_deletion(
            self.conn,
            user_id=created.user.user_id,
            expected_version=3,
            cooling_period=timedelta(days=7),
            purge_after=timedelta(days=30),
            request_source="account_settings",
            idempotency_key="deletion-request-two",
            now=NOW + timedelta(days=3),
        )
        deactivated = accounts.deactivate_account_after_cooling(
            self.conn,
            user_id=created.user.user_id,
            expected_version=second.user.row_version,
            source="retention_worker",
            idempotency_key="deletion-deactivate-two",
            deactivation_evidence={"evidence": "lifecycle_marker_recorded"},
            now=NOW + timedelta(days=11),
        )
        self.assertEqual(
            (deactivated.user.lifecycle_status, deactivated.request.status),
            ("deactivated_pending_purge", "deactivated_pending_purge"),
        )
        self.assertTrue(deactivated.request.deactivation_evidence_recorded)
        self.assertFalse(hasattr(deactivated.request, "purged_at"))

    def test_deletion_failure_boundaries_rollback_projection_event_request_and_sessions(self):
        points = (
            "after_deletion_user_update",
            "after_deletion_event_insert",
            "after_deletion_request_insert",
            "after_deletion_session_revocation",
        )
        for index, point in enumerate(points):
            with self.subTest(point=point):
                _, created = self.create_user(f"deletion-failure-{index}")
                session = self.create_session(
                    created.user.user_id, f"deletion-failure-{index}"
                )

                def fail(current):
                    if current == point:
                        raise RuntimeError(point)

                with self.assertRaises(RuntimeError):
                    accounts.request_account_deletion(
                        self.conn,
                        user_id=created.user.user_id,
                        expected_version=1,
                        cooling_period=timedelta(days=7),
                        purge_after=timedelta(days=30),
                        request_source="account_settings",
                        idempotency_key=f"deletion-failure-request-{index}",
                        now=NOW,
                        failure_injector=fail,
                    )
                user = self.conn.execute(
                    "SELECT lifecycle_status,row_version FROM users WHERE user_id=?",
                    (created.user.user_id,),
                ).fetchone()
                self.assertEqual(tuple(user), ("active", 1))
                self.assertEqual(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM account_deletion_requests WHERE user_id=?",
                        (created.user.user_id,),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM account_lifecycle_events WHERE user_id=?",
                        (created.user.user_id,),
                    ).fetchone()[0],
                    1,
                )
                accounts.resolve_session(self.conn, session_token=session.session_token, now=NOW)

    def test_cancel_deletion_restores_suspended_status_without_restoring_sessions(self):
        _, created = self.create_user("suspended-delete")
        accounts.suspend_user(
            self.conn,
            user_id=created.user.user_id,
            expected_version=1,
            source="test_admin",
            idempotency_key="suspended-before-delete",
            now=NOW,
        )
        requested = accounts.request_account_deletion(
            self.conn,
            user_id=created.user.user_id,
            expected_version=2,
            cooling_period=timedelta(days=7),
            purge_after=timedelta(days=30),
            request_source="account_settings",
            idempotency_key="suspended-delete-request",
            now=NOW,
        )
        cancelled = accounts.cancel_deletion_request(
            self.conn,
            user_id=created.user.user_id,
            expected_version=requested.user.row_version,
            source="account_settings",
            idempotency_key="suspended-delete-cancel",
            now=NOW + timedelta(days=1),
        )
        self.assertEqual(cancelled.user.lifecycle_status, "suspended")

    def test_concurrent_deletion_requests_produce_one_pending_request(self):
        _, created = self.create_user("deletion-race")
        self.conn.close()

        def request(index):
            conn = connect(self.db_path)
            try:
                return accounts.request_account_deletion(
                    conn,
                    user_id=created.user.user_id,
                    expected_version=1,
                    cooling_period=timedelta(days=7),
                    purge_after=timedelta(days=30),
                    request_source="account_settings",
                    idempotency_key=f"deletion-race-request-{index}",
                    now=NOW,
                )
            except accounts.AccountStateConflict:
                return None
            finally:
                conn.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(request, (1, 2)))
        self.conn = connect(self.db_path)
        self.assertEqual(sum(result is not None for result in results), 2)
        self.assertEqual(
            len({result.request.deletion_request_id for result in results}),
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM account_deletion_requests WHERE status='pending_cooling'"
            ).fetchone()[0],
            1,
        )

    def test_metadata_is_bounded_deterministic_and_rejects_sensitive_keys(self):
        _, created = self.create_user("metadata")
        with self.assertRaises(accounts.InvalidAccountInput):
            accounts.append_consent_event(
                self.conn,
                user_id=created.user.user_id,
                purpose="product_terms",
                policy_version="v1",
                action="granted",
                source="test",
                idempotency_key="metadata-sensitive",
                metadata={"raw_token": "not-allowed"},
                occurred_at=NOW,
            )
        with self.assertRaises(accounts.InvalidAccountInput):
            accounts.append_consent_event(
                self.conn,
                user_id=created.user.user_id,
                purpose="product_terms",
                policy_version="v1",
                action="granted",
                source="test",
                idempotency_key="metadata-too-large",
                metadata={"note": "x" * 5000},
                occurred_at=NOW,
            )


class SecurityContractTests(AccountsServiceTestCase):
    def direct_session_values(
        self,
        *,
        session_id,
        user_id,
        token_seed,
        csrf_seed,
        created_at=NOW,
    ):
        created = created_at.isoformat()
        return (
            session_id,
            user_id,
            hashlib.sha256(token_seed.encode()).hexdigest(),
            accounts.TOKEN_HASH_VERSION,
            hashlib.sha256(csrf_seed.encode()).hexdigest(),
            accounts.TOKEN_HASH_VERSION,
            created,
            created,
            (created_at + timedelta(hours=1)).isoformat(),
            (created_at + timedelta(days=1)).isoformat(),
            1,
            f"direct-session-{session_id}",
            "a" * 64,
        )

    def insert_direct_session(self, values):
        self.conn.execute(
            """
            INSERT INTO account_sessions (
              session_id,user_id,token_hash,token_hash_version,csrf_secret_hash,
              csrf_hash_version,created_at,last_seen_at,idle_expires_at,
              absolute_expires_at,session_version,
              creation_idempotency_key,request_fingerprint
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            values,
        )

    def mark_direct_rotated(self, session_id, at):
        timestamp = at.isoformat()
        self.conn.execute(
            "UPDATE account_sessions SET rotated_at=?, revoked_at=?, "
            "revoke_reason='session_rotated', session_version=session_version+1 "
            "WHERE session_id=?",
            (timestamp, timestamp, session_id),
        )

    def insert_rotation_edge(self, suffix, user_id, predecessor_id, replacement_id, at):
        self.conn.execute(
            """
            INSERT INTO account_session_rotations (
              rotation_id,user_id,predecessor_session_id,replacement_session_id,
              rotated_at,created_at
            ) VALUES (?,?,?,?,?,?)
            """,
            (
                "rot_" + suffix * 32,
                user_id,
                predecessor_id,
                replacement_id,
                at.isoformat(),
                at.isoformat(),
            ),
        )

    def test_csrf_is_session_bound_and_time_checked(self):
        _, first_user = self.create_user("csrf-first")
        _, second_user = self.create_user("csrf-second")
        first = self.create_session(first_user.user.user_id, "csrf-first")
        second = self.create_session(second_user.user.user_id, "csrf-second")
        validated = accounts.validate_session_csrf(
            self.conn,
            session_token=first.session_token,
            csrf_secret=first.csrf_secret,
            now=NOW,
        )
        self.assertEqual(validated.session_id, first.session.session_id)
        for token, csrf, at in (
            (first.session_token, second.csrf_secret, NOW),
            (second.session_token, first.csrf_secret, NOW),
            (first.session_token, first.csrf_secret, NOW - timedelta(seconds=1)),
            (first.session_token, first.csrf_secret, NOW + timedelta(hours=1)),
        ):
            with self.subTest(token=token[:4], at=at):
                with self.assertRaises(accounts.SessionUnavailable):
                    accounts.validate_session_csrf(
                        self.conn, session_token=token, csrf_secret=csrf, now=at
                    )
        row = self.conn.execute(
            "SELECT * FROM account_sessions WHERE session_id=?",
            (first.session.session_id,),
        ).fetchone()
        self.assertNotIn(first.session_token, tuple(row))
        self.assertNotIn(first.csrf_secret, tuple(row))
        self.assertNotIn(first.session_token, repr(first))
        self.assertNotIn(first.csrf_secret, repr(first))

    def test_public_session_errors_do_not_enumerate_account_or_session_state(self):
        errors = []

        def capture(callback):
            try:
                callback()
            except Exception as exc:
                errors.append((type(exc), str(exc), vars(exc)))
            else:
                self.fail("Expected the public session operation to fail")

        unknown_user_id = "usr_" + "f" * 32
        capture(
            lambda: accounts.create_session(
                self.conn,
                user_id=unknown_user_id,
                idle_ttl=timedelta(hours=1),
                absolute_ttl=timedelta(days=1),
                idempotency_key="enumeration-unknown-user",
                now=NOW,
            )
        )
        for index, status in enumerate(
            ("suspended", "deletion_requested", "deactivated_pending_purge")
        ):
            _, created = self.create_user(f"enumeration-{status}")
            if status == "suspended":
                accounts.suspend_user(
                    self.conn,
                    user_id=created.user.user_id,
                    expected_version=1,
                    source="test_admin",
                    idempotency_key=f"enumeration-suspend-{index}",
                    now=NOW,
                )
            else:
                requested = accounts.request_account_deletion(
                    self.conn,
                    user_id=created.user.user_id,
                    expected_version=1,
                    cooling_period=timedelta(days=1),
                    purge_after=timedelta(days=2),
                    request_source="account_settings",
                    idempotency_key=f"enumeration-delete-{index}",
                    now=NOW,
                )
                if status == "deactivated_pending_purge":
                    accounts.deactivate_account_after_cooling(
                        self.conn,
                        user_id=created.user.user_id,
                        expected_version=requested.user.row_version,
                        source="retention_worker",
                        idempotency_key=f"enumeration-deactivate-{index}",
                        deactivation_evidence={"review": "recorded"},
                        now=NOW + timedelta(days=1),
                    )
            capture(
                lambda user_id=created.user.user_id, index=index: accounts.create_session(
                    self.conn,
                    user_id=user_id,
                    idle_ttl=timedelta(hours=1),
                    absolute_ttl=timedelta(days=1),
                    idempotency_key=f"enumeration-inactive-{index}",
                    now=NOW,
                )
            )

        _, active = self.create_user("enumeration-session")
        session = self.create_session(active.user.user_id, "enumeration-session")
        accounts.revoke_current_session(
            self.conn,
            session_token=session.session_token,
            expected_session_version=1,
            reason="user_logout",
            now=NOW,
        )
        for token, at in (
            ("x" * 43, NOW),
            ("malformed", NOW),
            (session.session_token, NOW),
        ):
            capture(lambda token=token, at=at: accounts.resolve_session(
                self.conn, session_token=token, now=at
            ))

        self.assertTrue(errors)
        self.assertEqual({item[0] for item in errors}, {accounts.SessionUnavailable})
        self.assertEqual({item[1] for item in errors}, {"Session is not available."})
        self.assertEqual({tuple(item[2]) for item in errors}, {()})

    def test_recursive_metadata_contract_rejects_sensitive_and_oversized_values(self):
        unsafe_keys = (
            "Authorization-Header",
            "csrf_material",
            "invitation_hmac",
            "resume_content",
            "provider.subject",
            "database/path",
            "raw claims",
            "authentication:header",
            "RAW_TOKEN",
            "access-token",
            "ｃｓｒｆ_material",
        )
        for key in unsafe_keys:
            with self.subTest(key=key):
                with self.assertRaises(accounts.InvalidAccountInput) as caught:
                    accounts.validate_account_metadata({"outer": [{key: "private"}]})
                self.assertNotIn("private", str(caught.exception))
                self.assertNotIn(key, str(caught.exception))

        deeply_nested = {}
        cursor = deeply_nested
        for _ in range(accounts.MAX_METADATA_DEPTH + 2):
            cursor["safe"] = {}
            cursor = cursor["safe"]
        rejected = (
            deeply_nested,
            {f"field_{index}": index for index in range(accounts.MAX_METADATA_KEYS + 1)},
            {"items": list(range(accounts.MAX_METADATA_LIST_ITEMS + 1))},
            {"x" * (accounts.MAX_METADATA_KEY_LENGTH + 1): "value"},
            {"note": "x" * (accounts.MAX_METADATA_STRING_LENGTH + 1)},
            {"note": "Bearer abcdefghijklmnopqrstuvwxyz"},
        )
        for value in rejected:
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(accounts.InvalidAccountInput):
                    accounts.validate_account_metadata(value)
        safe = accounts.validate_account_metadata(
            {
                "review": {"outcome": "approved", "count": 2},
                "tags": ["beta"],
                "token_count": 128,
            }
        )
        self.assertEqual(safe["review"]["outcome"], "approved")

    def test_direct_sql_rejects_hash_time_version_and_cross_user_lineage_drift(self):
        _, first_user = self.create_user("sql-first")
        _, second_user = self.create_user("sql-second")
        first = self.create_session(first_user.user.user_id, "sql-first")
        second = self.create_session(second_user.user.user_id, "sql-second")

        statements = (
            (
                "UPDATE account_invitations SET invitation_secret_hmac=? WHERE invitation_id=(SELECT invitation_id FROM account_invitations LIMIT 1)",
                ("g" * 64,),
            ),
            (
                "UPDATE account_sessions SET token_hash=? WHERE session_id=?",
                ("z" * 64, first.session.session_id),
            ),
            (
                "UPDATE account_sessions SET csrf_secret_hash=? WHERE session_id=?",
                ("A" * 64, first.session.session_id),
            ),
            (
                "UPDATE account_sessions SET csrf_secret_hash=(SELECT csrf_secret_hash FROM account_sessions WHERE session_id=?) WHERE session_id=?",
                (first.session.session_id, second.session.session_id),
            ),
            (
                "UPDATE auth_identities SET request_fingerprint=? WHERE user_id=?",
                ("x" * 64, first_user.user.user_id),
            ),
            (
                "UPDATE account_sessions SET created_at=? WHERE session_id=?",
                ("2026-07-17 12:00:00+00:00", first.session.session_id),
            ),
            (
                "UPDATE account_sessions SET created_at=? WHERE session_id=?",
                ("2026-99-99T12:00:00+00:00", first.session.session_id),
            ),
            (
                "UPDATE account_sessions SET revoked_at=? WHERE session_id=?",
                ((NOW - timedelta(seconds=1)).isoformat(), first.session.session_id),
            ),
            (
                "UPDATE users SET created_at=?, updated_at=? WHERE user_id=?",
                (
                    (NOW + timedelta(seconds=1)).isoformat(),
                    (NOW + timedelta(seconds=1)).isoformat(),
                    first_user.user.user_id,
                ),
            ),
        )
        for sql, parameters in statements:
            with self.subTest(sql=sql):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.conn.execute(sql, parameters)
                self.conn.rollback()

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_direct_session(
                self.direct_session_values(
                    session_id="ses_" + "2" * 32,
                    user_id=first_user.user.user_id,
                    token_seed="before-owner-token",
                    csrf_seed="before-owner-csrf",
                    created_at=NOW - timedelta(minutes=1),
                )
            )
        self.conn.rollback()

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO account_lifecycle_events (
                  lifecycle_event_id,user_id,event_type,occurred_at,source,
                  account_version_before,account_version_after,metadata_json,
                  idempotency_key,request_fingerprint
                ) VALUES (?,?,?,?,?,2,3,'{}',?,?)
                """,
                (
                    "life_" + "3" * 32,
                    first_user.user.user_id,
                    "account_suspended",
                    NOW.isoformat(),
                    "test_admin",
                    "lifecycle-gap-key",
                    "a" * 64,
                ),
            )
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO consent_events (
                  consent_event_id,user_id,purpose,policy_version,action,occurred_at,
                  source,consent_version_before,consent_version_after,metadata_json,
                  idempotency_key,request_fingerprint
                ) VALUES (?,?,?,?,?,?,?,1,2,'{}',?,?)
                """,
                (
                    "cns_" + "4" * 32,
                    first_user.user.user_id,
                    "profile_storage",
                    "v1",
                    "granted",
                    NOW.isoformat(),
                    "test",
                    "consent-gap-key",
                    "a" * 64,
                ),
            )
        self.conn.rollback()

        with self.assertRaises(accounts.SessionUnavailable):
            accounts.create_session(
                self.conn,
                user_id=first_user.user.user_id,
                idle_ttl=timedelta(hours=1),
                absolute_ttl=timedelta(days=1),
                idempotency_key="session-before-owner-service",
                now=NOW - timedelta(minutes=1),
            )

    def test_rotation_edges_are_authoritative_nonforking_and_acyclic(self):
        session_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(account_sessions)")
        }
        self.assertNotIn("parent_session_id", session_columns)
        self.assertNotIn("replacement_session_id", session_columns)

        _, first_user = self.create_user("edge-first")
        _, second_user = self.create_user("edge-second")
        first = self.create_session(first_user.user.user_id, "edge-first")
        other = self.create_session(second_user.user.user_id, "edge-second")
        self.mark_direct_rotated(first.session.session_id, NOW + timedelta(minutes=1))
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_rotation_edge(
                "1",
                first_user.user.user_id,
                first.session.session_id,
                other.session.session_id,
                NOW + timedelta(minutes=1),
            )
        self.conn.rollback()

        _, self_user = self.create_user("edge-self")
        self_edge = self.create_session(self_user.user.user_id, "edge-self")
        self.mark_direct_rotated(self_edge.session.session_id, NOW + timedelta(minutes=1))
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_rotation_edge(
                "2",
                self_user.user.user_id,
                self_edge.session.session_id,
                self_edge.session.session_id,
                NOW + timedelta(minutes=1),
            )
        self.conn.rollback()

        _, chain_user = self.create_user("edge-chain")
        original = self.create_session(chain_user.user.user_id, "edge-chain")
        replacement = accounts.rotate_session(
            self.conn,
            session_token=original.session_token,
            expected_session_version=1,
            idle_ttl=timedelta(hours=1),
            idempotency_key="edge-chain-first",
            now=NOW + timedelta(minutes=1),
        )
        extra = accounts.create_session(
            self.conn,
            user_id=chain_user.user.user_id,
            idle_ttl=timedelta(hours=1),
            absolute_ttl=timedelta(days=1),
            idempotency_key="edge-chain-extra",
            now=NOW + timedelta(minutes=2),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_rotation_edge(
                "3",
                chain_user.user.user_id,
                original.session.session_id,
                extra.session.session_id,
                NOW + timedelta(minutes=2),
            )
        self.conn.rollback()

        second_predecessor = accounts.create_session(
            self.conn,
            user_id=chain_user.user.user_id,
            idle_ttl=timedelta(hours=1),
            absolute_ttl=timedelta(days=1),
            idempotency_key="edge-chain-second-predecessor",
            now=NOW + timedelta(minutes=2),
        )
        self.mark_direct_rotated(
            second_predecessor.session.session_id, NOW + timedelta(minutes=3)
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_rotation_edge(
                "4",
                chain_user.user.user_id,
                second_predecessor.session.session_id,
                replacement.session.session_id,
                NOW + timedelta(minutes=3),
            )
        self.conn.rollback()

        final = accounts.rotate_session(
            self.conn,
            session_token=replacement.session_token,
            expected_session_version=1,
            idle_ttl=timedelta(minutes=30),
            idempotency_key="edge-chain-second",
            now=NOW + timedelta(minutes=3),
        )
        self.mark_direct_rotated(final.session.session_id, NOW + timedelta(minutes=4))
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_rotation_edge(
                "5",
                chain_user.user.user_id,
                final.session.session_id,
                original.session.session_id,
                NOW + timedelta(minutes=4),
            )
        self.conn.rollback()

        _, time_user = self.create_user("edge-time")
        older = self.create_session(time_user.user.user_id, "edge-time-older")
        newer = accounts.create_session(
            self.conn,
            user_id=time_user.user.user_id,
            idle_ttl=timedelta(hours=1),
            absolute_ttl=timedelta(days=1),
            idempotency_key="edge-time-newer",
            now=NOW + timedelta(minutes=2),
        )
        self.mark_direct_rotated(newer.session.session_id, NOW + timedelta(minutes=3))
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_rotation_edge(
                "6",
                time_user.user.user_id,
                newer.session.session_id,
                older.session.session_id,
                NOW + timedelta(minutes=3),
            )
        self.conn.rollback()

        self.mark_direct_rotated(older.session.session_id, NOW + timedelta(minutes=1))
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_rotation_edge(
                "7",
                time_user.user.user_id,
                older.session.session_id,
                newer.session.session_id,
                NOW + timedelta(minutes=1),
            )
        self.conn.rollback()

        active_predecessor = self.create_session(time_user.user.user_id, "edge-active")
        active_replacement = accounts.create_session(
            self.conn,
            user_id=time_user.user.user_id,
            idle_ttl=timedelta(hours=1),
            absolute_ttl=timedelta(days=1),
            idempotency_key="edge-active-replacement",
            now=NOW + timedelta(minutes=1),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_rotation_edge(
                "8",
                time_user.user.user_id,
                active_predecessor.session.session_id,
                active_replacement.session.session_id,
                NOW + timedelta(minutes=1),
            )
        self.conn.rollback()

    def test_consent_and_lifecycle_events_cannot_predate_user_creation(self):
        user_id = "usr_" + "7" * 32
        created = NOW.isoformat()
        self.conn.execute(
            "INSERT INTO users(user_id,lifecycle_status,row_version,created_at,updated_at) "
            "VALUES(?,'active',1,?,?)",
            (user_id, created, created),
        )
        self.conn.commit()

        lifecycle_sql = """
            INSERT INTO account_lifecycle_events (
              lifecycle_event_id,user_id,event_type,occurred_at,source,
              account_version_before,account_version_after,metadata_json,
              idempotency_key,request_fingerprint
            ) VALUES (?,?, 'account_created',?,'audit',0,1,'{}',?,?)
        """
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                lifecycle_sql,
                (
                    "life_" + "7" * 32,
                    user_id,
                    (NOW - timedelta(seconds=1)).isoformat(),
                    "lifecycle-before-user",
                    "a" * 64,
                ),
            )
        self.conn.rollback()
        self.conn.execute(
            lifecycle_sql,
            ("life_" + "7" * 32, user_id, created, "lifecycle-at-user", "a" * 64),
        )
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO account_lifecycle_events (
                  lifecycle_event_id,user_id,event_type,occurred_at,source,
                  account_version_before,account_version_after,metadata_json,
                  idempotency_key,request_fingerprint
                ) VALUES (?,?, 'account_suspended',?,'audit',1,2,'{}',?,?)
                """,
                (
                    "life_" + "8" * 32,
                    user_id,
                    (NOW - timedelta(seconds=1)).isoformat(),
                    "later-lifecycle-before-user",
                    "d" * 64,
                ),
            )
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO account_lifecycle_events (
                  lifecycle_event_id,user_id,event_type,occurred_at,source,
                  account_version_before,account_version_after,metadata_json,
                  idempotency_key,request_fingerprint
                ) VALUES (?,?, 'account_suspended','2026-07-17 12:00:00','audit',1,2,'{}',?,?)
                """,
                ("life_" + "9" * 32, user_id, "malformed-lifecycle-time", "e" * 64),
            )
        self.conn.rollback()
        self.conn.execute(
            """
            INSERT INTO account_lifecycle_events (
              lifecycle_event_id,user_id,event_type,occurred_at,source,
              account_version_before,account_version_after,metadata_json,
              idempotency_key,request_fingerprint
            ) VALUES (?,?, 'account_suspended',?,'audit',1,2,'{}',?,?)
            """,
            (
                "life_" + "a" * 32,
                user_id,
                (NOW + timedelta(seconds=1)).isoformat(),
                "lifecycle-after-user",
                "1" * 64,
            ),
        )
        self.conn.rollback()

        consent_sql = """
            INSERT INTO consent_events (
              consent_event_id,user_id,purpose,policy_version,action,occurred_at,
              source,consent_version_before,consent_version_after,metadata_json,
              idempotency_key,request_fingerprint
            ) VALUES (?,?,'profile_storage','v1',?,?,?,0,1,'{}',?,?)
        """
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                consent_sql,
                (
                    "cns_" + "7" * 32,
                    user_id,
                    "granted",
                    (NOW - timedelta(seconds=1)).isoformat(),
                    "audit",
                    "consent-before-user",
                    "b" * 64,
                ),
            )
        self.conn.rollback()
        self.conn.execute(
            consent_sql,
            (
                "cns_" + "7" * 32,
                user_id,
                "granted",
                created,
                "audit",
                "consent-at-user",
                "b" * 64,
            ),
        )
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO consent_events (
                  consent_event_id,user_id,purpose,policy_version,action,occurred_at,
                  source,consent_version_before,consent_version_after,metadata_json,
                  idempotency_key,request_fingerprint
                ) VALUES (?,?,'profile_storage','v1','revoked',?,'audit',1,2,'{}',?,?)
                """,
                (
                    "cns_" + "9" * 32,
                    user_id,
                    (NOW - timedelta(seconds=1)).isoformat(),
                    "later-consent-before-user",
                    "f" * 64,
                ),
            )
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO consent_events (
                  consent_event_id,user_id,purpose,policy_version,action,occurred_at,
                  source,consent_version_before,consent_version_after,metadata_json,
                  idempotency_key,request_fingerprint
                ) VALUES (?,?,'profile_storage','v1','revoked','2026-07-17 12:00:00',
                          'audit',1,2,'{}',?,?)
                """,
                ("cns_" + "a" * 32, user_id, "malformed-consent-time", "0" * 64),
            )
        self.conn.rollback()
        self.conn.execute(
            """
            INSERT INTO consent_events (
              consent_event_id,user_id,purpose,policy_version,action,occurred_at,
              source,consent_version_before,consent_version_after,metadata_json,
              idempotency_key,request_fingerprint
            ) VALUES (?,?,'profile_storage','v1','revoked',?,'audit',1,2,'{}',?,?)
            """,
            (
                "cns_" + "8" * 32,
                user_id,
                (NOW + timedelta(seconds=1)).isoformat(),
                "consent-after-user",
                "c" * 64,
            ),
        )
        self.conn.rollback()

        invitation = accounts.create_invitation(
            self.conn,
            email="temporal-boundary@example.test",
            lookup_key=INVITATION_KEY,
            expires_at=NOW + timedelta(days=1),
            created_by="audit",
            idempotency_key="temporal-boundary-invitation",
            now=NOW - timedelta(minutes=2),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                UPDATE account_invitations
                SET invitation_status='consumed', consumed_at=?, consumed_by_user_id=?
                WHERE invitation_id=?
                """,
                (
                    (NOW - timedelta(seconds=1)).isoformat(),
                    user_id,
                    invitation.invitation.invitation_id,
                ),
            )
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """
                INSERT INTO account_deletion_requests (
                  deletion_request_id,user_id,requested_at,cooling_period_ends_at,
                  purge_eligible_at,status,request_source,restore_lifecycle_status,
                  deactivation_evidence_json,idempotency_key,request_fingerprint
                ) VALUES (?,?,?,?,?,'pending_cooling','audit','active','{}',?,?)
                """,
                (
                    "del_" + "7" * 32,
                    user_id,
                    (NOW - timedelta(seconds=1)).isoformat(),
                    (NOW + timedelta(days=1)).isoformat(),
                    (NOW + timedelta(days=2)).isoformat(),
                    "deletion-before-user",
                    "2" * 64,
                ),
            )
        self.conn.rollback()

    def test_deletion_replays_without_erasure_claims_or_duplicate_mutations(self):
        _, created = self.create_user("deletion-replay-contract")
        session = self.create_session(created.user.user_id, "deletion-replay-contract")
        arguments = {
            "user_id": created.user.user_id,
            "expected_version": 1,
            "cooling_period": timedelta(days=7),
            "purge_after": timedelta(days=30),
            "request_source": "account_settings",
            "idempotency_key": "deletion-replay-contract",
            "now": NOW,
        }
        first = accounts.request_account_deletion(self.conn, **arguments)
        exact = accounts.request_account_deletion(self.conn, **arguments)
        fresh = accounts.request_account_deletion(
            self.conn,
            **{
                **arguments,
                "expected_version": 2,
                "idempotency_key": "deletion-replay-fresh-key",
            },
        )
        self.assertTrue(exact.replayed)
        self.assertTrue(fresh.replayed)
        self.assertEqual(first.request.deletion_request_id, exact.request.deletion_request_id)
        self.assertEqual(first.request.deletion_request_id, fresh.request.deletion_request_id)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM account_deletion_requests").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM account_lifecycle_events WHERE event_type='deletion_requested'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT session_version FROM account_sessions WHERE session_id=?",
                (session.session.session_id,),
            ).fetchone()[0],
            2,
        )
        with self.assertRaises(accounts.AccountStateConflict):
            accounts.request_account_deletion(
                self.conn,
                **{
                    **arguments,
                    "expected_version": 2,
                    "purge_after": timedelta(days=31),
                },
            )
        deactivated = accounts.deactivate_account_after_cooling(
            self.conn,
            user_id=created.user.user_id,
            expected_version=2,
            source="retention_worker",
            idempotency_key="deletion-replay-deactivate",
            deactivation_evidence={"retention_review": "recorded"},
            now=NOW + timedelta(days=7),
        )
        replay = accounts.deactivate_account_after_cooling(
            self.conn,
            user_id=created.user.user_id,
            expected_version=2,
            source="retention_worker",
            idempotency_key="deletion-replay-deactivate",
            deactivation_evidence={"retention_review": "recorded"},
            now=NOW + timedelta(days=8),
        )
        self.assertTrue(replay.replayed)
        public_text = repr(deactivated).lower()
        self.assertNotIn("purged", public_text)
        self.assertNotIn("completed", public_text)
        self.assertEqual(deactivated.user.lifecycle_status, "deactivated_pending_purge")
        self.assertEqual(deactivated.request.status, "deactivated_pending_purge")


class TransactionIsolationTests(AccountsServiceTestCase):
    def test_mutation_refuses_connection_without_foreign_key_enforcement(self):
        self.conn.close()
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 0)
        with self.assertRaises(accounts.AccountStateConflict):
            accounts.create_invitation(
                self.conn,
                email="unsafe-connection@example.test",
                lookup_key=INVITATION_KEY,
                expires_at=NOW + timedelta(days=1),
                created_by="test_admin",
                idempotency_key="invite-without-foreign-keys",
                now=NOW,
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM account_invitations").fetchone()[0], 0)

    def test_outer_transaction_and_savepoint_preserve_caller_work(self):
        self.conn.execute("CREATE TABLE caller_work(value TEXT)")
        self.conn.commit()
        self.conn.execute("BEGIN")
        self.conn.execute("INSERT INTO caller_work VALUES ('before')")
        accounts.create_invitation(
            self.conn,
            email="outer@example.test",
            lookup_key=INVITATION_KEY,
            expires_at=NOW + timedelta(days=1),
            created_by="test_admin",
            idempotency_key="invite-outer-transaction",
            now=NOW,
        )
        other = connect(self.db_path)
        self.assertEqual(other.execute("SELECT COUNT(*) FROM caller_work").fetchone()[0], 0)
        other.close()
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM caller_work").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM account_invitations").fetchone()[0], 0)

        self.conn.execute("BEGIN")
        self.conn.execute("SAVEPOINT caller_owned")
        self.conn.execute("INSERT INTO caller_work VALUES ('kept')")

        def fail(point):
            if point == "after_invitation_insert":
                raise RuntimeError("forced")

        with self.assertRaises(RuntimeError):
            accounts.create_invitation(
                self.conn,
                email="failure@example.test",
                lookup_key=INVITATION_KEY,
                expires_at=NOW + timedelta(days=1),
                created_by="test_admin",
                idempotency_key="invite-forced-failure",
                now=NOW,
                failure_injector=fail,
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM caller_work").fetchone()[0], 1)
        self.conn.execute("RELEASE SAVEPOINT caller_owned")
        self.conn.commit()

    def test_forced_user_creation_failures_leave_no_partial_rows(self):
        points = (
            "after_user_insert",
            "after_identity_insert",
            "after_lifecycle_event_insert",
            "after_invitation_consumption",
        )
        for index, point in enumerate(points):
            with self.subTest(point=point):
                email = f"failure-{index}@example.test"
                invitation_creation = accounts.create_invitation(
                    self.conn,
                    email=email,
                    lookup_key=INVITATION_KEY,
                    expires_at=NOW + timedelta(days=1),
                    created_by="test_admin",
                    idempotency_key=f"invite-user-failure-{index}",
                    now=NOW,
                )

                def fail(current):
                    if current == point:
                        raise RuntimeError(point)

                with self.assertRaises(RuntimeError):
                    ACCOUNT_SERVICE.create_invited_user(
                        self.conn,
                        identity=trusted_identity(f"failure-subject-{index}", email),
                        invitation_token=invitation_creation.invitation_token,
                        invitation_lookup_key=INVITATION_KEY,
                        idempotency_key=f"create-user-failure-{index}",
                        now=NOW,
                        failure_injector=fail,
                    )
                self.assertEqual(
                    self.conn.execute(
                        "SELECT invitation_status FROM account_invitations WHERE invitation_id = ?",
                        (invitation_creation.invitation.invitation_id,),
                    ).fetchone()[0],
                    "pending",
                )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM auth_identities").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM account_lifecycle_events").fetchone()[0], 0)

    def test_locked_database_fails_without_partial_write(self):
        locker = connect(self.db_path)
        worker = connect(self.db_path)
        locker.execute("BEGIN IMMEDIATE")
        locker.execute("INSERT INTO companies(name,slug,careers_url) VALUES ('Lock','lock','https://lock.test')")
        with self.assertRaises(sqlite3.OperationalError):
            accounts.create_invitation(
                worker,
                email="locked@example.test",
                lookup_key=INVITATION_KEY,
                expires_at=NOW + timedelta(days=1),
                created_by="test_admin",
                idempotency_key="invite-locked-db",
                now=NOW,
            )
        locker.rollback()
        self.assertEqual(worker.execute("SELECT COUNT(*) FROM account_invitations").fetchone()[0], 0)
        locker.close()
        worker.close()


if __name__ == "__main__":
    unittest.main()
