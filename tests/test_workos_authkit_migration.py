from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.workos_authkit_provider_migration import (
    WorkOSAuthKitProviderMigrationError,
    apply_workos_authkit_provider_migration,
)
from tests.closed_schema_convergence_test_support import (
    apply_m007,
    build_fresh_m001_m006,
)
from tests.workos_authkit_test_support import INVITATION_KEY, NOW
from wahojobs import accounts
from wahojobs.account_reconciliation import attest_account_schema
from wahojobs.closed_schema_authority import current_closed_schema_is_exact
from wahojobs.workos_authkit_schema import (
    EXPECTED_MIGRATION_VERSIONS,
    EXPECTED_SCHEMA_FINGERPRINT,
    attest_workos_authkit_schema,
    migration_statement_count,
)


class WorkOSAuthKitProviderMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="wahojobs-m008-test-",
            ignore_cleanup_errors=True,
        )
        self.path = Path(self.directory.name) / "database.sqlite3"
        self.connection = build_fresh_m001_m006(self.path)
        apply_m007(self.connection, self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    def _google_user(self):
        verifier = accounts.TrustedIdentityVerifier()
        service = accounts.AccountService(verifier)
        invitation = accounts.create_invitation(
            self.connection,
            email="preserved@example.test",
            lookup_key=INVITATION_KEY,
            expires_at=NOW + timedelta(days=1),
            created_by="test_operator",
            idempotency_key="m008-preserved-invitation",
            now=NOW,
        )
        identity = verifier.from_validated_google_claims(
            provider_subject="preserved-google-subject",
            verified_email="preserved@example.test",
            email_verified=True,
            authenticated_at=NOW,
            metadata_version="google_oidc_v1",
        )
        return service.create_invited_user(
            self.connection,
            identity=identity,
            invitation_token=invitation.invitation_token,
            invitation_lookup_key=INVITATION_KEY,
            idempotency_key="m008-preserved-user",
            now=NOW,
        )

    def test_exact_m007_is_applicable_and_m008_is_exact(self):
        before = attest_workos_authkit_schema(self.connection)
        self.assertEqual(before["state"], "provider_expansion_pending")
        self.assertTrue(before["applicable"])
        self.assertTrue(current_closed_schema_is_exact(self.connection))

        result = apply_workos_authkit_provider_migration(self.connection)

        self.assertEqual(
            result,
            {
                "migration_version": "008_workos_authkit_provider",
                "state": "correctly_installed",
                "applied": True,
            },
        )
        report = attest_workos_authkit_schema(self.connection)
        self.assertEqual(report["state"], "correctly_installed")
        self.assertEqual(report["actual_schema_fingerprint"], EXPECTED_SCHEMA_FINGERPRINT)
        self.assertEqual(report["present_migration_versions"], list(EXPECTED_MIGRATION_VERSIONS))
        self.assertTrue(attest_account_schema(self.connection))
        self.assertTrue(current_closed_schema_is_exact(self.connection))
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_google_rows_and_every_unrelated_schema_object_are_preserved(self):
        created = self._google_user()
        row_before = tuple(
            self.connection.execute(
                "SELECT * FROM auth_identities WHERE auth_identity_id = ?",
                (created.identity.auth_identity_id,),
            ).fetchone()
        )
        schema_before = {
            (row[0], row[1]): row[2]
            for row in self.connection.execute(
                "SELECT type, name, sql FROM sqlite_schema "
                "WHERE type IN ('table','index','trigger','view')"
            )
            if row[1] != "auth_identities"
            and not (row[0] == "index" and row[1].startswith("sqlite_autoindex_auth_identities_"))
        }

        apply_workos_authkit_provider_migration(self.connection)

        row_after = tuple(
            self.connection.execute(
                "SELECT * FROM auth_identities WHERE auth_identity_id = ?",
                (created.identity.auth_identity_id,),
            ).fetchone()
        )
        schema_after = {
            (row[0], row[1]): row[2]
            for row in self.connection.execute(
                "SELECT type, name, sql FROM sqlite_schema "
                "WHERE type IN ('table','index','trigger','view')"
            )
            if row[1] != "auth_identities"
            and not (row[0] == "index" and row[1].startswith("sqlite_autoindex_auth_identities_"))
        }
        self.assertEqual(row_after, row_before)
        self.assertEqual(schema_after, schema_before)
        self.assertEqual(row_after[2], "google")

    def test_provider_domain_uniqueness_and_immutability_are_preserved(self):
        created = self._google_user()
        apply_workos_authkit_provider_migration(self.connection)
        values = (
            "auth_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            created.user.user_id,
            "workos_authkit",
            "user_aaaaaaaaaaaaaaaa",
            "preserved@example.test",
            1,
            NOW.isoformat(timespec="seconds"),
            NOW.isoformat(timespec="seconds"),
            "m008-workos-identity-key",
            "a" * 64,
        )
        self.connection.execute(
            "INSERT INTO auth_identities "
            "(auth_identity_id,user_id,provider,provider_subject,verified_email,"
            "email_verified,created_at,last_authenticated_at,link_idempotency_key,"
            "request_fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE auth_identities SET provider='google' WHERE auth_identity_id=?",
                (values[0],),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO auth_identities "
                "(auth_identity_id,user_id,provider,provider_subject,verified_email,"
                "email_verified,created_at,last_authenticated_at,link_idempotency_key,"
                "request_fingerprint) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "auth_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    created.user.user_id,
                    "unsupported",
                    "subject",
                    None,
                    0,
                    NOW.isoformat(timespec="seconds"),
                    NOW.isoformat(timespec="seconds"),
                    "m008-invalid-provider-key",
                    "b" * 64,
                ),
            )

    def test_failure_rolls_back_table_marker_and_rows(self):
        created = self._google_user()
        before = tuple(self.connection.execute("SELECT * FROM auth_identities").fetchone())

        def fail(point):
            if point == "after_statement_5":
                raise RuntimeError("injected")

        with self.assertRaises(WorkOSAuthKitProviderMigrationError):
            apply_workos_authkit_provider_migration(
                self.connection,
                failure_injector=fail,
            )

        self.assertEqual(attest_workos_authkit_schema(self.connection)["state"], "provider_expansion_pending")
        self.assertEqual(tuple(self.connection.execute("SELECT * FROM auth_identities").fetchone()), before)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM wahojobs_schema_migrations WHERE version='008_workos_authkit_provider'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(created.identity.provider, "google")

    def test_exact_reapplication_is_a_noop(self):
        first = apply_workos_authkit_provider_migration(self.connection)
        second = apply_workos_authkit_provider_migration(self.connection)
        self.assertTrue(first["applied"])
        self.assertFalse(second["applied"])
        self.assertEqual(second["state"], "correctly_installed")

    def test_drift_and_residue_are_rejected_without_mutation(self):
        self.connection.execute("CREATE TABLE auth_identities_m008_backup(value TEXT)")
        self.connection.commit()
        self.assertEqual(attest_workos_authkit_schema(self.connection)["state"], "residue")
        with self.assertRaises(WorkOSAuthKitProviderMigrationError):
            apply_workos_authkit_provider_migration(self.connection)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM wahojobs_schema_migrations WHERE version='008_workos_authkit_provider'"
            ).fetchone()[0],
            0,
        )

    def test_migration_sql_is_complete_and_import_does_not_apply_it(self):
        self.assertGreater(migration_statement_count(), 5)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM wahojobs_schema_migrations WHERE version='008_workos_authkit_provider'"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
