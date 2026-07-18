import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.ownership_test_support import (
    EMPTY_JSON,
    NOW_TEXT,
    add_activation_event,
    add_active_user,
    add_alias,
    add_binding,
    add_principal,
    install_ownership,
)


class OwnershipSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ownership.sqlite"
        self.conn = install_ownership(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_valid_principal_alias_binding_and_event(self):
        user_id = add_active_user(self.conn)
        principal_id = add_principal(self.conn)
        alias_id = add_alias(self.conn, principal_id)
        binding_id = add_binding(self.conn, principal_id, user_id)
        event_id = add_activation_event(self.conn, principal_id, user_id, binding_id)
        self.assertTrue(alias_id.startswith("loa_"))
        self.assertTrue(event_id.startswith("obe_"))
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_alias_is_exact_unique_append_only_and_namespace_scoped(self):
        principal_id = add_principal(self.conn)
        alias_id = add_alias(self.conn, principal_id, value="Mixed_Case")
        with self.assertRaises(sqlite3.IntegrityError):
            add_alias(self.conn, principal_id, suffix="2", value="Mixed_Case")
        with self.assertRaises(sqlite3.IntegrityError):
            add_alias(self.conn, principal_id, suffix="3", environment="other", value="other")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE legacy_owner_aliases SET alias_value='changed' WHERE alias_id=?",
                (alias_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM legacy_owner_aliases WHERE alias_id=?", (alias_id,))
        stored = self.conn.execute(
            "SELECT alias_value FROM legacy_owner_aliases WHERE alias_id=?", (alias_id,)
        ).fetchone()[0]
        self.assertEqual(stored, "Mixed_Case")

    def test_nonclaimable_principals_cannot_receive_active_binding(self):
        user_id = add_active_user(self.conn)
        principal_id = add_principal(
            self.conn,
            principal_type="development",
            claim_policy="nonclaimable",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            add_binding(self.conn, principal_id, user_id)

    def test_exclusive_principal_rejects_duplicate_active_owner(self):
        first_user = add_active_user(self.conn, "first")
        second_user = add_active_user(self.conn, "second")
        principal_id = add_principal(self.conn)
        add_binding(self.conn, principal_id, first_user, suffix="1")
        with self.assertRaises(sqlite3.IntegrityError):
            add_binding(self.conn, principal_id, second_user, suffix="2")

    def test_binding_foreign_keys_and_relations_are_enforced(self):
        user_id = add_active_user(self.conn)
        principal_id = add_principal(self.conn)
        with self.assertRaises(sqlite3.IntegrityError):
            add_binding(self.conn, "prn_" + "f" * 32, user_id)
        with self.assertRaises(sqlite3.IntegrityError):
            add_binding(self.conn, principal_id, "usr_" + "f" * 32)

    def test_event_chain_relation_time_idempotency_and_append_only_guards(self):
        user_id = add_active_user(self.conn)
        other_user = add_active_user(self.conn, "other")
        principal_id = add_principal(self.conn)
        binding_id = add_binding(self.conn, principal_id, user_id)
        event_id = add_activation_event(self.conn, principal_id, user_id, binding_id)

        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_event(
                suffix="2", principal_id=principal_id, user_id=user_id, binding_id=binding_id,
                version=3, event_type="binding_suspended", prior="active", result="suspended",
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_event(
                suffix="3", principal_id=principal_id, user_id=other_user, binding_id=binding_id,
                version=2, event_type="binding_suspended", prior="active", result="suspended",
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_event(
                suffix="4", principal_id=principal_id, user_id=user_id, binding_id=binding_id,
                version=2, event_type="binding_suspended", prior="active", result="suspended",
                occurred_at="2026-07-16T12:00:00+00:00",
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_event(
                suffix="5", principal_id=principal_id, user_id=user_id, binding_id=binding_id,
                version=2, event_type="binding_suspended", prior="active", result="suspended",
                idempotency_key="binding-activation-1",
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE ownership_binding_events SET reason_code='changed' WHERE event_id=?",
                (event_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM ownership_binding_events WHERE event_id=?", (event_id,))

    def test_malformed_ids_timestamps_and_metadata_are_rejected(self):
        invalid_rows = (
            ("prn_short", NOW_TEXT, EMPTY_JSON),
            ("prn_" + "1" * 32, NOW_TEXT, EMPTY_JSON),
            ("prn_" + "2" * 31 + "1", "2026-07-17", EMPTY_JSON),
            ("prn_" + "3" * 31 + "1", NOW_TEXT, "[]"),
            ("prn_" + "4" * 31 + "1", NOW_TEXT, "{" + " " * 4096 + "}"),
        )
        for principal_id, created_at, metadata in invalid_rows:
            with self.subTest(principal_id=principal_id), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(
                    "INSERT INTO product_principals "
                    "(principal_id, environment_namespace, principal_type, lifecycle_status, "
                    "claim_policy, exclusive_account_binding, version, created_at, updated_at, "
                    "provenance_json) "
                    "VALUES (?, 'test', 'legacy_profile', 'active', 'manual_approval', 1, 1, ?, ?, ?)",
                    (principal_id, created_at, created_at, metadata),
                )

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO product_principals "
                "(principal_id, environment_namespace, principal_type, lifecycle_status, "
                "claim_policy, exclusive_account_binding, version, created_at, updated_at, "
                "provenance_json) "
                "VALUES (?, 'test', 'legacy_profile', 'active', 'manual_approval', 1, 2, ?, ?, ?)",
                ("prn_" + "5" * 31 + "1", NOW_TEXT, NOW_TEXT, EMPTY_JSON),
            )

    def _insert_event(
        self,
        *,
        suffix,
        principal_id,
        user_id,
        binding_id,
        version,
        event_type,
        prior,
        result,
        occurred_at=NOW_TEXT,
        idempotency_key=None,
    ):
        event_id = f"obe_{int(suffix):032x}"
        from wahojobs.ownership import event_request_fingerprint

        key = idempotency_key or f"event-idempotency-{suffix}"
        fingerprint = event_request_fingerprint(
            principal_id=principal_id,
            binding_id=binding_id,
            user_id=user_id,
            expected_event_version=version,
            event_type=event_type,
            prior_status=prior,
            resulting_status=result,
            actor_type="administrator",
            reason_code="manual_approval",
            approval_reference=None,
            occurred_at=occurred_at,
            metadata={},
        )
        self.conn.execute(
            "INSERT INTO ownership_binding_events "
            "(event_id, principal_id, user_id, binding_id, environment_namespace, event_version, "
            "event_type, prior_status, resulting_status, actor_type, reason_code, approval_reference, "
            "idempotency_key, request_fingerprint, occurred_at, metadata_json) "
            "VALUES (?, ?, ?, ?, 'test', ?, ?, ?, ?, 'administrator', 'manual_approval', NULL, ?, ?, ?, ?)",
            (
                event_id,
                principal_id,
                user_id,
                binding_id,
                version,
                event_type,
                prior,
                result,
                key,
                fingerprint,
                occurred_at,
                EMPTY_JSON,
            ),
        )


if __name__ == "__main__":
    unittest.main()
