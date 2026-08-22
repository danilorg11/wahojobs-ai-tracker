from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from tests.ai_profile_import_test_support import (
    NOW,
    database_counts,
    import_source_metadata,
    install_ai_profile_import_database,
    intake_grant,
)
from tests.browser_session_authentication_test_support import seed_browser_session
from wahojobs.ai_profile_import import (
    AI_PROFILE_IMPORT_ENTITLEMENT_CODE,
    AI_PROFILE_IMPORT_RESERVATION_LEASE,
    AIProfileImportError,
    AIProfileImportReservationRequest,
    AIProfileImportService,
)


class AIProfileImportEntitlementTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(
            prefix="wahojobs-ai-entitlement-", ignore_cleanup_errors=True
        )
        self.path = Path(self.directory.name) / "database.sqlite3"
        self.connection, self.session = install_ai_profile_import_database(self.path)
        self.grant = intake_grant(self.path, self.session)
        self.service = AIProfileImportService()

    def tearDown(self):
        self.connection.close()
        self.directory.cleanup()

    @staticmethod
    def metadata(origins=("resume",)):
        return import_source_metadata(origins)

    @classmethod
    def request(cls, key="ai-reservation-key-0001", origins=("resume",)):
        return AIProfileImportReservationRequest(key, cls.metadata(origins))

    def assert_code(self, code, callable_, *args, **kwargs):
        with self.assertRaises(AIProfileImportError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), code)

    def test_lazy_first_reservation_and_exact_retry_reuse_one_attempt(self):
        self.assertEqual(database_counts(self.connection)["ai_profile_import_entitlements"], 0)
        first = self.service.reserve(
            self.connection, self.grant, self.request(), now=NOW
        )
        replay = self.service.reserve(
            self.connection, self.grant, self.request(), now=NOW
        )
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.state, "reserved")
        self.assertEqual(replay.state, "reserved")
        self.assertEqual(first.authority.attempt_id, replay.authority.attempt_id)
        self.assertEqual(first.authority.reservation_id, replay.authority.reservation_id)
        self.assertRegex(first.authority.attempt_id, r"^aip_[0-9a-f]{32}$")
        self.assertRegex(first.authority.reservation_id, r"^air_[0-9a-f]{32}$")
        self.assertEqual(
            database_counts(self.connection),
            {
                "product_profiles": 0,
                "product_profile_revisions": 0,
                "product_profile_sources": 0,
                "ai_profile_import_entitlements": 1,
                "ai_profile_import_attempts": 1,
            },
        )
        entitlement = self.connection.execute(
            "SELECT environment_namespace,account_id,entitlement_code,state,lease_expires_at "
            "FROM ai_profile_import_entitlements"
        ).fetchone()
        self.assertEqual(entitlement[:4], (
            self.session["environment"],
            self.session["account_id"],
            AI_PROFILE_IMPORT_ENTITLEMENT_CODE,
            "reserved",
        ))
        self.assertEqual(
            entitlement[4],
            (NOW + AI_PROFILE_IMPORT_RESERVATION_LEASE).isoformat(timespec="seconds"),
        )
        self.assert_code(
            "idempotency_conflict",
            self.service.reserve,
            self.connection,
            self.grant,
            self.request(
                "ai-reservation-key-0001",
                ("linkedin_profile_export",),
            ),
            now=NOW,
        )
        self.assertEqual(database_counts(self.connection)["ai_profile_import_attempts"], 1)

    def test_competing_reservation_is_rejected_without_duplicate_attempt(self):
        self.service.reserve(self.connection, self.grant, self.request(), now=NOW)
        self.assert_code(
            "entitlement_reserved",
            self.service.reserve,
            self.connection,
            self.grant,
            self.request("ai-reservation-key-0002"),
            now=NOW,
        )
        self.assertEqual(database_counts(self.connection)["ai_profile_import_attempts"], 1)

    def test_degenerate_identifier_factory_fails_without_partial_entitlement(self):
        service = AIProfileImportService(token_hex=lambda _size: "0" * 32)
        self.assert_code(
            "internal_failure",
            service.reserve,
            self.connection,
            self.grant,
            self.request(),
            now=NOW,
        )
        self.assertEqual(database_counts(self.connection)["ai_profile_import_entitlements"], 0)
        self.assertEqual(database_counts(self.connection)["ai_profile_import_attempts"], 0)

    def test_release_and_cancellation_are_idempotent_and_do_not_consume(self):
        reservation = self.service.reserve(
            self.connection, self.grant, self.request(), now=NOW
        ).authority
        released = self.service.release(
            self.connection,
            self.grant,
            reservation,
            outcome_code="cancelled",
            now=NOW,
        )
        replay = self.service.release(
            self.connection,
            self.grant,
            reservation,
            outcome_code="cancelled",
            now=NOW,
        )
        self.assertFalse(released["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(
            tuple(self.connection.execute(
                "SELECT state,reservation_id,attempt_id,consumed_at "
                "FROM ai_profile_import_entitlements"
            ).fetchone()),
            ("available", None, None, None),
        )
        replacement = self.service.reserve(
            self.connection,
            self.grant,
            self.request("ai-reservation-key-0003"),
            now=NOW,
        )
        self.assertNotEqual(replacement.authority.attempt_id, reservation.attempt_id)

    def test_processing_failure_release_does_not_create_profile_or_consume(self):
        for ordinal, outcome_code in enumerate(
            ("processing_failed", "draft_expired", "abandoned"), start=1
        ):
            reservation = self.service.reserve(
                self.connection,
                self.grant,
                self.request(f"ai-failure-release-{ordinal:04d}"),
                now=NOW,
            ).authority
            self.service.release(
                self.connection,
                self.grant,
                reservation,
                outcome_code=outcome_code,
                now=NOW,
            )
        counts = database_counts(self.connection)
        self.assertEqual(counts["product_profiles"], 0)
        self.assertEqual(counts["product_profile_revisions"], 0)
        self.assertEqual(counts["product_profile_sources"], 0)
        self.assertEqual(
            {
                tuple(row)
                for row in self.connection.execute(
                    "SELECT state,result_code FROM ai_profile_import_attempts"
                ).fetchall()
            },
            {
                ("released", "processing_failed"),
                ("released", "draft_expired"),
                ("released", "abandoned"),
            },
        )

    def test_expired_lease_is_reclaimed_deterministically(self):
        original = self.service.reserve(
            self.connection, self.grant, self.request(), now=NOW
        ).authority
        after_lease = NOW + AI_PROFILE_IMPORT_RESERVATION_LEASE + timedelta(seconds=1)
        self.assert_code(
            "reservation_expired",
            self.service.reserve,
            self.connection,
            self.grant,
            self.request(),
            now=after_lease,
        )
        self.assertEqual(
            tuple(self.connection.execute(
                "SELECT state,result_code FROM ai_profile_import_attempts WHERE attempt_id=?",
                (original.attempt_id,),
            ).fetchone()),
            ("expired", "reservation_expired"),
        )
        replacement = self.service.reserve(
            self.connection,
            self.grant,
            self.request("ai-reservation-key-0004"),
            now=after_lease,
        )
        self.assertNotEqual(replacement.authority.attempt_id, original.attempt_id)
        self.assertEqual(database_counts(self.connection)["ai_profile_import_attempts"], 2)

    def test_stale_release_records_expiry_instead_of_cancellation(self):
        reservation = self.service.reserve(
            self.connection, self.grant, self.request(), now=NOW
        ).authority
        self.assert_code(
            "reservation_expired",
            self.service.release,
            self.connection,
            self.grant,
            reservation,
            outcome_code="cancelled",
            now=NOW + AI_PROFILE_IMPORT_RESERVATION_LEASE + timedelta(seconds=1),
        )
        self.assertEqual(
            tuple(self.connection.execute(
                "SELECT state,result_code FROM ai_profile_import_attempts"
            ).fetchone()),
            ("expired", "reservation_expired"),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT state FROM ai_profile_import_entitlements"
            ).fetchone()[0],
            "available",
        )

    def test_accounts_are_isolated_and_one_principal_cannot_use_another_reservation(self):
        first = self.service.reserve(
            self.connection, self.grant, self.request(), now=NOW
        ).authority
        second_session = seed_browser_session(self.connection, suffix="95")
        second_grant = intake_grant(self.path, second_session)
        second = self.service.reserve(
            self.connection,
            second_grant,
            self.request("ai-reservation-key-0095"),
            now=NOW,
        ).authority
        self.assertNotEqual(first.account_id, second.account_id)
        self.assertEqual(database_counts(self.connection)["ai_profile_import_entitlements"], 2)
        self.assert_code(
            "reservation_mismatch",
            self.service.release,
            self.connection,
            second_grant,
            first,
            outcome_code="cancelled",
            now=NOW,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM ai_profile_import_attempts WHERE state='reserved'"
            ).fetchone()[0],
            2,
        )

    def test_environment_is_part_of_the_entitlement_and_reservation_authority(self):
        reserved = self.service.reserve(
            self.connection, self.grant, self.request(), now=NOW
        ).authority
        self.assertEqual(reserved.environment_namespace, self.session["environment"])
        table_sql = self.connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='ai_profile_import_entitlements'"
        ).fetchone()[0]
        self.assertIn(
            "PRIMARY KEY (environment_namespace, account_id, entitlement_code)",
            table_sql,
        )
        # The same canonical account key in another namespace is a distinct row;
        # no runtime grant is issued for it by this test.
        timestamp = NOW.isoformat(timespec="seconds")
        self.connection.execute(
            "INSERT INTO ai_profile_import_entitlements "
            "(environment_namespace,account_id,entitlement_code,state,reservation_id,attempt_id,lease_expires_at,consumed_at,created_at,updated_at) "
            "VALUES ('test',?,?,'available',NULL,NULL,NULL,NULL,?,?)",
            (self.session["account_id"], AI_PROFILE_IMPORT_ENTITLEMENT_CODE, timestamp, timestamp),
        )
        self.connection.commit()
        self.assertEqual(database_counts(self.connection)["ai_profile_import_entitlements"], 2)

    def test_each_valid_bundle_shape_creates_one_attempt_regardless_of_document_count(self):
        cases = (
            (("resume",), "ai-bundle-resume-0001"),
            (("resume_docx",), "ai-bundle-docx-0001"),
            (("linkedin_profile_export",), "ai-bundle-linkedin-0001"),
            (("resume", "linkedin_profile_export"), "ai-bundle-combined-0001"),
        )
        for ordinal, (origins, key) in enumerate(cases, start=1):
            with self.subTest(origins=origins):
                directory = tempfile.TemporaryDirectory(
                    prefix="wahojobs-ai-bundle-", ignore_cleanup_errors=True
                )
                path = Path(directory.name) / "database.sqlite3"
                connection, session = install_ai_profile_import_database(
                    path, suffix=str(100 + ordinal)
                )
                try:
                    grant = intake_grant(path, session)
                    self.service.reserve(
                        connection, grant, self.request(key, origins), now=NOW
                    )
                    self.assertEqual(database_counts(connection)["ai_profile_import_attempts"], 1)
                    metadata = connection.execute(
                        "SELECT source_metadata_json FROM ai_profile_import_attempts"
                    ).fetchone()[0]
                    self.assertIn(f'"document_count":{len(origins)}', metadata)
                    columns = {
                        row[1]
                        for row in connection.execute(
                            "PRAGMA table_info(ai_profile_import_attempts)"
                        )
                    }
                    self.assertFalse(
                        columns
                        & {
                            "model_call_count",
                            "input_tokens",
                            "output_tokens",
                            "cost",
                        }
                    )
                finally:
                    connection.close()
                    directory.cleanup()


if __name__ == "__main__":
    unittest.main()
