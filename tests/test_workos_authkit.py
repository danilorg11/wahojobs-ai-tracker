from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

from tests.workos_authkit_test_support import (
    FakeWorkOSBoundary,
    INVITATION_KEY,
    MutableClock,
    NOW,
    build_m008,
    callback_target,
    completion_policy,
    connect,
    create_invitation,
    deliver,
    gateway,
    snapshot,
)
from wahojobs import accounts
from wahojobs.browser_session_lifecycle import (
    create_request_scoped_session_secret_vault,
    discard_request_scoped_session_secret_vault,
)
from wahojobs.workos_authkit import (
    WorkOSAuthKitAuthentication,
    WorkOSAuthKitUnavailable,
)


class WorkOSAuthKitGatewayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="wahojobs-workos-gateway-test-",
            ignore_cleanup_errors=True,
        )
        self.path = Path(self.directory.name) / "database.sqlite3"
        self.connection = build_m008(self.path)
        self.clock = MutableClock()
        self.boundary = FakeWorkOSBoundary()
        self.gateway = gateway(self.boundary, clock=self.clock)

    def tearDown(self):
        self.gateway.close()
        self.connection.close()
        self.directory.cleanup()

    def _complete_target(self, prepared, target, *, connection=None):
        connection = connection or self.connection
        vault = create_request_scoped_session_secret_vault()
        result = self.gateway.complete_authorization(
            connection,
            target,
            prepared.transaction_id,
            completion_policy(),
            vault,
        )
        return result, vault

    def _first_login(self, *, email=None):
        invitation = create_invitation(
            self.connection,
            email or self.boundary.email,
        )
        prepared = self.gateway.prepare_authorization(
            self.connection,
            invitation_credential=bytearray(
                invitation.invitation_token.encode("ascii")
            ),
        )
        target = callback_target(prepared)
        result, vault = self._complete_target(prepared, target)
        return invitation, prepared, target, result, vault

    def test_valid_invited_first_login_succeeds_exactly_once(self):
        invitation, prepared, target, result, vault = self._first_login()
        self.assertEqual(result.status, "issued")
        deliver(self.connection, result, vault)
        self.assertEqual(
            snapshot(self.connection),
            {
                "users": 1,
                "auth_identities": 1,
                "account_invitations": 1,
                "account_sessions": 1,
                "product_principals": 1,
                "principal_account_bindings": 1,
                "ownership_binding_events": 1,
            },
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT invitation_status FROM account_invitations WHERE invitation_id=?",
                (invitation.invitation.invitation_id,),
            ).fetchone()[0],
            "consumed",
        )
        replay, replay_vault = self._complete_target(prepared, target)
        self.assertEqual(replay.status, "authentication_denied")
        discard_request_scoped_session_secret_vault(replay_vault)
        self.assertEqual(self.boundary.exchange_count, 1)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
            1,
        )

    def test_invitation_email_uses_existing_exact_canonicalization(self):
        self.boundary.email = "person@example.test"
        _invitation, _prepared, _target, result, vault = self._first_login(
            email="  PERSON@EXAMPLE.TEST  "
        )
        self.assertEqual(result.status, "issued")
        deliver(self.connection, result, vault)
        self.assertEqual(
            self.connection.execute("SELECT verified_email FROM auth_identities").fetchone()[0],
            "person@example.test",
        )

    def test_invitation_and_verified_email_mismatch_is_atomic_denial(self):
        before = snapshot(self.connection)
        _invitation, _prepared, _target, result, vault = self._first_login(
            email="different@example.test"
        )
        self.assertEqual(result.status, "authentication_denied")
        discard_request_scoped_session_secret_vault(vault)
        after = snapshot(self.connection)
        self.assertEqual(after["users"], before["users"])
        self.assertEqual(after["auth_identities"], before["auth_identities"])
        self.assertEqual(after["account_sessions"], before["account_sessions"])
        self.assertEqual(
            self.connection.execute("SELECT invitation_status FROM account_invitations").fetchone()[0],
            "pending",
        )

    def test_malformed_expired_revoked_and_consumed_invitations_fail_before_redirect(self):
        malformed = bytearray(secrets.token_urlsafe(32).encode("ascii"))
        with self.assertRaises(WorkOSAuthKitUnavailable):
            self.gateway.prepare_authorization(
                self.connection,
                invitation_credential=malformed,
            )

        expired = create_invitation(
            self.connection,
            "expired@example.test",
            now=NOW - timedelta(hours=2),
            expires=NOW - timedelta(hours=1),
            suffix="expired",
        )
        with self.assertRaises(WorkOSAuthKitUnavailable):
            self.gateway.prepare_authorization(
                self.connection,
                invitation_credential=bytearray(expired.invitation_token.encode("ascii")),
            )

        revoked = create_invitation(
            self.connection,
            "revoked@example.test",
            suffix="revoked",
        )
        accounts.revoke_invitation(
            self.connection,
            invitation_id=revoked.invitation.invitation_id,
            now=NOW,
        )
        with self.assertRaises(WorkOSAuthKitUnavailable):
            self.gateway.prepare_authorization(
                self.connection,
                invitation_credential=bytearray(revoked.invitation_token.encode("ascii")),
            )

        consumed = create_invitation(
            self.connection,
            "consumed@example.test",
            suffix="consumed",
        )
        verifier = accounts.TrustedIdentityVerifier()
        service = accounts.AccountService(verifier)
        service.create_invited_user(
            self.connection,
            identity=verifier.from_validated_google_claims(
                provider_subject="consumed-google-subject",
                verified_email="consumed@example.test",
                email_verified=True,
                authenticated_at=NOW,
                metadata_version="google_oidc_v1",
            ),
            invitation_token=consumed.invitation_token,
            invitation_lookup_key=INVITATION_KEY,
            idempotency_key="consumed-google-user",
            now=NOW,
        )
        with self.assertRaises(WorkOSAuthKitUnavailable):
            self.gateway.prepare_authorization(
                self.connection,
                invitation_credential=bytearray(consumed.invitation_token.encode("ascii")),
            )
        self.assertEqual(self.boundary.authorization_count, 0)

    def test_uninvited_new_subject_gets_no_account_or_session(self):
        prepared = self.gateway.prepare_authorization(self.connection)
        result, vault = self._complete_target(prepared, callback_target(prepared))
        self.assertEqual(result.status, "authentication_denied")
        discard_request_scoped_session_secret_vault(vault)
        self.assertEqual(snapshot(self.connection)["users"], 0)
        self.assertEqual(snapshot(self.connection)["account_sessions"], 0)

    def test_returning_exact_subject_needs_no_invitation_and_email_does_not_link(self):
        _invitation, _prepared, _target, first, first_vault = self._first_login()
        self.assertEqual(first.status, "issued")
        deliver(self.connection, first, first_vault)
        self.boundary.email = "changed-address@example.test"

        prepared = self.gateway.prepare_authorization(self.connection)
        returning, vault = self._complete_target(prepared, callback_target(prepared))

        self.assertEqual(returning.status, "issued")
        deliver(self.connection, returning, vault)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM auth_identities").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
            2,
        )

    def test_new_subject_is_never_linked_by_existing_email(self):
        verifier = accounts.TrustedIdentityVerifier()
        service = accounts.AccountService(verifier)
        google_invitation = create_invitation(
            self.connection,
            self.boundary.email,
            suffix="google-collision",
        )
        service.create_invited_user(
            self.connection,
            identity=verifier.from_validated_google_claims(
                provider_subject="existing-google-subject",
                verified_email=self.boundary.email,
                email_verified=True,
                authenticated_at=NOW,
                metadata_version="google_oidc_v1",
            ),
            invitation_token=google_invitation.invitation_token,
            invitation_lookup_key=INVITATION_KEY,
            idempotency_key="existing-google-account",
            now=NOW,
        )
        workos_invitation = create_invitation(
            self.connection,
            self.boundary.email,
            suffix="workos-collision",
        )
        prepared = self.gateway.prepare_authorization(
            self.connection,
            invitation_credential=bytearray(workos_invitation.invitation_token.encode("ascii")),
        )
        result, vault = self._complete_target(prepared, callback_target(prepared))
        self.assertEqual(result.status, "authentication_denied")
        discard_request_scoped_session_secret_vault(vault)
        self.assertEqual(
            [
                tuple(row)
                for row in
                self.connection.execute(
                    "SELECT provider FROM auth_identities ORDER BY provider"
                )
            ],
            [("google",)],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT invitation_status FROM account_invitations WHERE invitation_id=?",
                (workos_invitation.invitation.invitation_id,),
            ).fetchone()[0],
            "pending",
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0],
            0,
        )

    def test_non_magic_or_unverified_provider_completion_is_denied(self):
        for field, value in (("method", "Password"), ("verified", False)):
            with self.subTest(field=field):
                setattr(self.boundary, field, value)
                invitation = create_invitation(
                    self.connection,
                    self.boundary.email,
                    suffix=field,
                )
                prepared = self.gateway.prepare_authorization(
                    self.connection,
                    invitation_credential=bytearray(invitation.invitation_token.encode("ascii")),
                )
                result, vault = self._complete_target(prepared, callback_target(prepared))
                self.assertEqual(result.status, "authentication_denied")
                discard_request_scoped_session_secret_vault(vault)
                setattr(self.boundary, field, "MagicAuth" if field == "method" else True)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)

    def test_provider_failure_consumes_transaction_without_wahojobs_mutation(self):
        self.boundary.fail_exchange = True
        invitation = create_invitation(self.connection, self.boundary.email)
        prepared = self.gateway.prepare_authorization(
            self.connection,
            invitation_credential=bytearray(invitation.invitation_token.encode("ascii")),
        )
        target = callback_target(prepared)
        before = snapshot(self.connection)
        first, first_vault = self._complete_target(prepared, target)
        self.assertEqual(first.status, "provider_unavailable")
        discard_request_scoped_session_secret_vault(first_vault)
        replay, replay_vault = self._complete_target(prepared, target)
        self.assertEqual(replay.status, "authentication_denied")
        discard_request_scoped_session_secret_vault(replay_vault)
        self.assertEqual(snapshot(self.connection), before)
        self.assertEqual(self.boundary.exchange_count, 1)

    def test_restart_during_unfinished_login_fails_safely(self):
        invitation = create_invitation(self.connection, self.boundary.email)
        prepared = self.gateway.prepare_authorization(
            self.connection,
            invitation_credential=bytearray(invitation.invitation_token.encode("ascii")),
        )
        target = callback_target(prepared)
        self.gateway.close()
        replacement_boundary = FakeWorkOSBoundary()
        self.gateway = gateway(replacement_boundary, clock=self.clock)
        result, vault = self._complete_target(prepared, target)
        self.assertEqual(result.status, "authentication_denied")
        discard_request_scoped_session_secret_vault(vault)
        self.assertEqual(replacement_boundary.exchange_count, 0)
        self.assertEqual(snapshot(self.connection)["users"], 0)

    def test_same_callback_concurrency_has_one_exchange_and_one_session(self):
        invitation = create_invitation(self.connection, self.boundary.email)
        prepared = self.gateway.prepare_authorization(
            self.connection,
            invitation_credential=bytearray(invitation.invitation_token.encode("ascii")),
        )
        target = callback_target(prepared)
        barrier = threading.Barrier(2)
        statuses = []
        lock = threading.Lock()

        def worker():
            connection = connect(self.path, timeout=5.0)
            vault = create_request_scoped_session_secret_vault()
            barrier.wait(timeout=5)
            result = self.gateway.complete_authorization(
                connection,
                target,
                prepared.transaction_id,
                completion_policy(),
                vault,
            )
            if result.status == "issued":
                deliver(connection, result, vault)
            else:
                discard_request_scoped_session_secret_vault(vault)
            with lock:
                statuses.append(result.status)
            connection.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(statuses.count("issued"), 1)
        self.assertEqual(statuses.count("authentication_denied"), 1)
        self.assertEqual(self.boundary.exchange_count, 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0], 1)

    def test_independent_callback_invitation_race_has_one_atomic_winner(self):
        barrier = threading.Barrier(2)
        self.gateway.close()
        self.boundary = FakeWorkOSBoundary(exchange_barrier=barrier)
        self.gateway = gateway(self.boundary, clock=self.clock)
        invitation = create_invitation(self.connection, self.boundary.email)
        prepared = [
            self.gateway.prepare_authorization(
                self.connection,
                invitation_credential=bytearray(invitation.invitation_token.encode("ascii")),
            )
            for _ in range(2)
        ]
        targets = [callback_target(item) for item in prepared]
        statuses = []
        lock = threading.Lock()

        def worker(index):
            connection = connect(self.path, timeout=5.0)
            vault = create_request_scoped_session_secret_vault()
            result = self.gateway.complete_authorization(
                connection,
                targets[index],
                prepared[index].transaction_id,
                completion_policy(),
                vault,
            )
            if result.status == "issued":
                deliver(connection, result, vault)
            else:
                discard_request_scoped_session_secret_vault(vault)
            with lock:
                statuses.append(result.status)
            connection.close()

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(statuses.count("issued"), 1)
        self.assertEqual(self.boundary.exchange_count, 2)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM auth_identities").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM account_sessions").fetchone()[0], 1)
        self.assertEqual(
            self.connection.execute("SELECT invitation_status FROM account_invitations").fetchone()[0],
            "consumed",
        )

    def test_account_creation_and_invitation_consumption_rollback_together(self):
        invitation = create_invitation(self.connection, self.boundary.email)
        verifier = accounts.TrustedIdentityVerifier()
        service = accounts.AccountService(verifier)
        identity = verifier.from_workos_authkit_authentication(
            provider_subject=self.boundary.subject,
            verified_email=self.boundary.email,
            authenticated_at=NOW,
            metadata_version="workos_authkit_magic_auth_v1",
        )

        def fail(point):
            if point == "after_identity_insert":
                raise RuntimeError("injected")

        with self.assertRaises(RuntimeError):
            service.create_invited_user(
                self.connection,
                identity=identity,
                invitation_token=invitation.invitation_token,
                invitation_lookup_key=INVITATION_KEY,
                idempotency_key="workos-atomic-rollback",
                now=NOW,
                failure_injector=fail,
            )
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM auth_identities").fetchone()[0], 0)
        self.assertEqual(
            self.connection.execute("SELECT invitation_status FROM account_invitations").fetchone()[0],
            "pending",
        )

    def test_server_observed_post_exchange_time_drives_identity_and_session(self):
        class AdvancingBoundary(FakeWorkOSBoundary):
            def exchange_code(inner_self, *, code, code_verifier):
                result = super().exchange_code(code=code, code_verifier=code_verifier)
                self.clock.advance(timedelta(seconds=3))
                return result

        self.gateway.close()
        self.boundary = AdvancingBoundary()
        self.gateway = gateway(self.boundary, clock=self.clock)
        _invitation, _prepared, _target, result, vault = self._first_login()
        self.assertEqual(result.status, "issued")
        deliver(self.connection, result, vault, now=NOW + timedelta(seconds=3))
        expected = (NOW + timedelta(seconds=3)).isoformat(timespec="seconds")
        self.assertEqual(
            self.connection.execute("SELECT last_authenticated_at FROM auth_identities").fetchone()[0],
            expected,
        )
        self.assertEqual(
            self.connection.execute("SELECT created_at FROM account_sessions").fetchone()[0],
            expected,
        )

    def test_sensitive_values_never_appear_in_representations_or_errors(self):
        invitation = create_invitation(self.connection, self.boundary.email)
        prepared = self.gateway.prepare_authorization(
            self.connection,
            invitation_credential=bytearray(invitation.invitation_token.encode("ascii")),
        )
        callback = callback_target(prepared)
        code = dict(
            item.split("=", 1)
            for item in urlsplit(callback).query.split("&")
        )["code"]
        exception = WorkOSAuthKitUnavailable()
        rendered = "\n".join(
            (
                repr(self.gateway),
                repr(prepared),
                repr(exception),
                str(exception),
            )
        )
        self.assertNotIn(invitation.invitation_token, rendered)
        self.assertNotIn(code, rendered)
        self.assertNotIn("person@example.test", rendered)


if __name__ == "__main__":
    unittest.main()
