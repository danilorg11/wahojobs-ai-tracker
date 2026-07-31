import dataclasses
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from tests.ownership_test_support import (
    NOW_TEXT,
    add_activation_event,
    add_active_user,
    add_alias,
    add_binding,
    add_principal,
    database_snapshot,
    install_ownership,
)
from wahojobs import accounts, ownership
from wahojobs.ownership_reconciliation import reconcile_ownership


class OwnershipSecurityBlockerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "security.sqlite"
        self.conn = install_ownership(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_cross_kind_alias_family_coherence_and_environment_isolation(self):
        first = add_principal(self.conn, suffix="1")
        second = add_principal(self.conn, suffix="2")
        for suffix, kind in (("1", "profile_id"), ("2", "pipeline_owner"), ("3", "legacy_user_id")):
            add_alias(
                self.conn,
                first,
                suffix=suffix,
                kind=kind,
                value="same-historical-owner",
            )
        with self.assertRaises(sqlite3.IntegrityError):
            add_alias(
                self.conn,
                second,
                suffix="4",
                kind="applicant_user_id",
                value="same-historical-owner",
            )

        other_environment = add_principal(
            self.conn, suffix="5", environment="other"
        )
        add_alias(
            self.conn,
            other_environment,
            suffix="5",
            environment="other",
            kind="pipeline_owner",
            value="same-historical-owner",
        )
        add_alias(
            self.conn,
            second,
            suffix="6",
            kind="anonymous_user_key",
            value="same-historical-owner",
        )
        families = {
            row[0]
            for row in self.conn.execute(
                "SELECT DISTINCT alias_family FROM legacy_owner_aliases"
            )
        }
        self.assertEqual(families, {"owner_resource", "anonymous"})

    def test_local_user_is_development_nonclaimable_and_alias_controls_are_sql_enforced(self):
        development = add_principal(
            self.conn,
            suffix="10",
            environment="development",
            principal_type="development",
            claim_policy="nonclaimable",
        )
        add_alias(
            self.conn,
            development,
            suffix="10",
            environment="development",
            value="local_user",
            claimability="nonclaimable",
        )
        ordinary = add_principal(self.conn, suffix="11")
        with self.assertRaises(sqlite3.IntegrityError):
            add_alias(self.conn, ordinary, suffix="11", value="local_user")
        with self.assertRaises(sqlite3.IntegrityError):
            add_alias(
                self.conn,
                development,
                suffix="12",
                environment="development",
                kind="anonymous_user_key",
                value="local_user",
                claimability="nonclaimable",
            )

        for codepoint in (*range(0x20), *range(0x7F, 0xA0)):
            with self.subTest(codepoint=codepoint), self.assertRaises(sqlite3.IntegrityError):
                add_alias(
                    self.conn,
                    ordinary,
                    suffix=str(100 + codepoint),
                    value=f"left{chr(codepoint)}right",
                )
        add_alias(
            self.conn,
            ordinary,
            suffix="200",
            value="Usuário-Histórico-日本語",
        )
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.execute(
                "INSERT INTO legacy_owner_aliases "
                "(alias_id, principal_id, environment_namespace, alias_kind, alias_family, "
                "alias_value, claimability, discovered_from, created_at, provenance_json) "
                "VALUES (?, ?, 'test', 'profile_id', 'anonymous', 'caller-family', "
                "'manual_approval', 'manual_review', ?, '{}')",
                ("loa_" + f"{201:032x}", ordinary, NOW_TEXT),
            )

    def test_sql_rejects_degenerate_ids_for_every_ownership_object(self):
        user_id = add_active_user(self.conn)
        principal_id = add_principal(self.conn, suffix="20")
        binding_id = add_binding(self.conn, principal_id, user_id, suffix="20")
        for character in "0f":
            with self.subTest(object="principal", character=character), self.assertRaises(sqlite3.IntegrityError):
                self._insert_principal(f"prn_{character * 32}", {})
            with self.subTest(object="alias", character=character), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(
                    "INSERT INTO legacy_owner_aliases "
                    "(alias_id, principal_id, environment_namespace, alias_kind, alias_value, "
                    "claimability, discovered_from, created_at, provenance_json) "
                    "VALUES (?, ?, 'test', 'profile_id', ?, 'manual_approval', 'manual_review', ?, '{}')",
                    (f"loa_{character * 32}", principal_id, f"alias-{character}", NOW_TEXT),
                )
            with self.subTest(object="binding", character=character), self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute(
                    "INSERT INTO principal_account_bindings "
                    "(binding_id, principal_id, user_id, environment_namespace, binding_role, "
                    "binding_status, version, latest_event_version, created_at, updated_at, provenance_json) "
                    "VALUES (?, ?, ?, 'test', 'delegated', 'active', 1, 1, ?, ?, '{}')",
                    (f"pab_{character * 32}", principal_id, user_id, NOW_TEXT, NOW_TEXT),
                )
            with self.subTest(object="event", character=character), self.assertRaises(sqlite3.IntegrityError):
                self._insert_activation_event(
                    f"obe_{character * 32}", principal_id, user_id, binding_id, f"degenerate-key-{character}-0000"
                )

    def test_domain_and_sql_metadata_policy_share_sensitive_vocabulary(self):
        for index, key in enumerate(sorted(ownership.OWNERSHIP_SENSITIVE_METADATA_NAMES), 300):
            metadata = {"outer": {key: "private"}}
            with self.subTest(layer="domain", key=key), self.assertRaises(
                ownership.OwnershipValidationError
            ):
                ownership.canonical_metadata(metadata)
            with self.subTest(layer="sql", key=key), self.assertRaises(
                sqlite3.IntegrityError
            ):
                self._insert_principal(f"prn_{index:032x}", metadata)

        for key in ("Raw Application.Content", "SQL/Query", "AUTHORIZATION_header"):
            with self.subTest(key=key), self.assertRaises(sqlite3.IntegrityError):
                self._insert_principal(
                    f"prn_{(800 + len(key)):032x}", {"nested": {key: "private"}}
                )
        safe_id = "prn_" + f"{900:032x}"
        self._insert_principal(
            safe_id,
            {"review": {"approved": True}, "notes": ["dormant", "manual"]},
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE product_principals SET provenance_json=?, version=2, updated_at=? "
                "WHERE principal_id=?",
                ('{"nested":{"token":"private"}}', NOW_TEXT, safe_id),
            )

    def test_sql_metadata_guards_cover_alias_binding_and_event_documents(self):
        user_id = add_active_user(self.conn)
        principal_id = add_principal(self.conn, suffix="30")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO legacy_owner_aliases "
                "(alias_id, principal_id, environment_namespace, alias_kind, alias_value, "
                "claimability, discovered_from, created_at, provenance_json) "
                "VALUES (?, ?, 'test', 'profile_id', 'metadata-alias', 'manual_approval', "
                "'manual_review', ?, ?)",
                ("loa_" + f"{30:032x}", principal_id, NOW_TEXT, '{"OAuth Claim":"private"}'),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO principal_account_bindings "
                "(binding_id, principal_id, user_id, environment_namespace, binding_role, "
                "binding_status, version, latest_event_version, created_at, updated_at, provenance_json) "
                "VALUES (?, ?, ?, 'test', 'owner', 'active', 1, 1, ?, ?, ?)",
                ("pab_" + f"{30:032x}", principal_id, user_id, NOW_TEXT, NOW_TEXT, '{"email_address":"private"}'),
            )
        binding_id = add_binding(self.conn, principal_id, user_id, suffix="31")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO ownership_binding_events "
                "(event_id, principal_id, user_id, binding_id, environment_namespace, event_version, "
                "event_type, prior_status, resulting_status, actor_type, reason_code, approval_reference, "
                "idempotency_key, request_fingerprint, occurred_at, metadata_json) "
                "VALUES (?, ?, ?, ?, 'test', 1, 'binding_activated', NULL, 'active', "
                "'administrator', 'manual_approval', NULL, 'sensitive-event-key', ?, ?, ?)",
                (
                    "obe_" + f"{31:032x}",
                    principal_id,
                    user_id,
                    binding_id,
                    "a" * 64,
                    NOW_TEXT,
                    '{"raw_application_content":"private"}',
                ),
            )
        add_activation_event(
            self.conn, principal_id, user_id, binding_id, suffix="31"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE principal_account_bindings SET binding_status='suspended', "
                "version=2, latest_event_version=2, updated_at=?, suspended_at=?, "
                "provenance_json=? WHERE binding_id=?",
                (
                    NOW_TEXT,
                    NOW_TEXT,
                    '{"nested":{"session_token":"private"}}',
                    binding_id,
                ),
            )

    def test_sql_metadata_bounds_and_shapes_match_domain_contract(self):
        invalid_documents = [
            "not-json",
            "[]",
            json.dumps({"notes": "x" * 1025}),
            json.dumps({"notes": "Bearer abcdefghijklmnop"}),
            json.dumps({"ключ": "value"}, ensure_ascii=False),
            json.dumps({f"key-{index}": index for index in range(65)}),
            json.dumps({f"group-{index}": list(range(64)) for index in range(8)}),
        ]
        deep = {"leaf": True}
        for _ in range(9):
            deep = {"nested": deep}
        invalid_documents.append(json.dumps(deep))
        invalid_documents.append(json.dumps({"padding": "x" * 4096}))
        for index, encoded in enumerate(invalid_documents, 1000):
            with self.subTest(index=index), self.assertRaises(sqlite3.DatabaseError):
                self.conn.execute(
                    "INSERT INTO product_principals "
                    "(principal_id, environment_namespace, principal_type, lifecycle_status, "
                    "claim_policy, exclusive_account_binding, version, created_at, updated_at, provenance_json) "
                    "VALUES (?, 'test', 'legacy_profile', 'active', 'manual_approval', 1, 1, ?, ?, ?)",
                    (f"prn_{index:032x}", NOW_TEXT, NOW_TEXT, encoded),
                )

        safe_id = f"prn_{2000:032x}"
        self.conn.execute(
            "INSERT INTO product_principals "
            "(principal_id, environment_namespace, principal_type, lifecycle_status, "
            "claim_policy, exclusive_account_binding, version, created_at, updated_at, provenance_json) "
            "VALUES (?, 'test', 'legacy_profile', 'active', 'manual_approval', 1, 1, ?, ?, ?)",
            (
                safe_id,
                NOW_TEXT,
                NOW_TEXT,
                json.dumps({"review_notes": "Aprovação manual 日本語"}, ensure_ascii=False),
            ),
        )
    def test_event_service_exact_replay_changed_replay_and_projection_atomicity(self):
        user_id = add_active_user(self.conn)
        principal_id = add_principal(self.conn, suffix="40")
        create = ownership.CreateBindingCommand(
            principal_id=principal_id,
            user_id=user_id,
            binding_role="owner",
            actor_type="administrator",
            reason_code="manual_approval",
            approval_reference="review-40",
            idempotency_key="create-binding-security-40",
            occurred_at=NOW_TEXT,
            metadata={"review": "approved"},
        )
        original = ownership.create_binding_with_initial_event(self.conn, create)
        replay = ownership.create_binding_with_initial_event(self.conn, create)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.event_id, original.event_id)
        self.assertNotIn(user_id, json.dumps(dataclasses.asdict(replay), sort_keys=True))
        with self.assertRaises(ownership.OwnershipIdempotencyConflict) as conflict:
            ownership.create_binding_with_initial_event(
                self.conn, dataclasses.replace(create, reason_code="different_reason")
            )
        self.assertEqual(str(conflict.exception), "Ownership state could not be changed.")
        for private in (principal_id, user_id, original.binding_id, create.idempotency_key):
            self.assertNotIn(private, str(conflict.exception))

        suspend = ownership.BindingEventCommand(
            principal_id=principal_id,
            binding_id=original.binding_id,
            user_id=user_id,
            expected_event_version=2,
            event_type="binding_suspended",
            prior_status="active",
            resulting_status="suspended",
            actor_type="administrator",
            reason_code="manual_review",
            approval_reference="review-41",
            idempotency_key="suspend-binding-security-40",
            occurred_at=NOW_TEXT,
            metadata={},
        )
        suspended = ownership.append_binding_event(self.conn, suspend)
        self.assertFalse(suspended.replayed)
        replay_after_later_transition = ownership.create_binding_with_initial_event(
            self.conn, create
        )
        self.assertEqual(replay_after_later_transition.event_id, original.event_id)
        status = self.conn.execute(
            "SELECT binding_status FROM principal_account_bindings WHERE binding_id=?",
            (original.binding_id,),
        ).fetchone()[0]
        self.assertEqual(status, "suspended")
        self.assertEqual(ownership.append_binding_event(self.conn, suspend).event_id, suspended.event_id)
        with self.assertRaises(ownership.OwnershipIdempotencyConflict):
            ownership.append_binding_event(
                self.conn, dataclasses.replace(suspend, expected_event_version=3)
            )

    def test_event_service_failure_and_caller_savepoint_preserve_unrelated_work(self):
        user_id = add_active_user(self.conn)
        principal_id = add_principal(self.conn, suffix="50")
        create = ownership.CreateBindingCommand(
            principal_id=principal_id,
            user_id=user_id,
            binding_role="owner",
            actor_type="administrator",
            reason_code="manual_approval",
            approval_reference=None,
            idempotency_key="create-binding-security-50",
            occurred_at=NOW_TEXT,
        )
        original = ownership.create_binding_with_initial_event(self.conn, create)
        self.conn.execute("CREATE TABLE caller_work (value TEXT)")
        self.conn.commit()
        self.conn.execute("BEGIN")
        self.conn.execute("INSERT INTO caller_work VALUES ('preserve-me')")
        command = ownership.BindingEventCommand(
            principal_id=principal_id,
            binding_id=original.binding_id,
            user_id=user_id,
            expected_event_version=2,
            event_type="binding_suspended",
            prior_status="active",
            resulting_status="suspended",
            actor_type="administrator",
            reason_code="manual_review",
            approval_reference=None,
            idempotency_key="failure-binding-security-50",
            occurred_at=NOW_TEXT,
        )

        for failure_point in ("after_event_insert", "after_projection_update"):
            def fail(point):
                if point == failure_point:
                    raise RuntimeError("injected")

            with self.subTest(failure_point=failure_point), self.assertRaises(RuntimeError):
                ownership.append_binding_event(self.conn, command, failure_injector=fail)
            self.assertTrue(self.conn.in_transaction)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM caller_work").fetchone()[0], 1)
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM ownership_binding_events WHERE idempotency_key=?",
                    (command.idempotency_key,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                self.conn.execute(
                    "SELECT binding_status FROM principal_account_bindings WHERE binding_id=?",
                    (original.binding_id,),
                ).fetchone()[0],
                "active",
            )
        ownership.append_binding_event(self.conn, command)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(
            self.conn.execute(
                "SELECT binding_status FROM principal_account_bindings WHERE binding_id=?",
                (original.binding_id,),
            ).fetchone()[0],
            "suspended",
        )
        self.conn.rollback()
        self.assertEqual(
            self.conn.execute(
                "SELECT binding_status FROM principal_account_bindings WHERE binding_id=?",
                (original.binding_id,),
            ).fetchone()[0],
            "active",
        )

    def test_initial_binding_failure_never_leaves_projection_or_event(self):
        user_id = add_active_user(self.conn)
        principal_id = add_principal(self.conn, suffix="52")
        command = ownership.CreateBindingCommand(
            principal_id=principal_id,
            user_id=user_id,
            binding_role="owner",
            actor_type="administrator",
            reason_code="manual_approval",
            approval_reference=None,
            idempotency_key="create-binding-failure-security-52",
            occurred_at=NOW_TEXT,
        )
        for failure_point in ("after_binding_insert", "after_initial_event_insert"):
            def fail(point):
                if point == failure_point:
                    raise RuntimeError("injected")

            with self.subTest(failure_point=failure_point), self.assertRaises(RuntimeError):
                ownership.create_binding_with_initial_event(
                    self.conn, command, failure_injector=fail
                )
            self.assertEqual(self._binding_event_counts(), (0, 0))

    def test_create_binding_unique_race_recovers_as_replay_or_generic_conflict(self):
        user_id = add_active_user(self.conn)
        principal_id = add_principal(self.conn, suffix="55", exclusive=0)
        command = ownership.CreateBindingCommand(
            principal_id=principal_id,
            user_id=user_id,
            binding_role="owner",
            actor_type="administrator",
            reason_code="manual_approval",
            approval_reference=None,
            idempotency_key="create-binding-race-security-55",
            occurred_at=NOW_TEXT,
        )
        original = ownership.create_binding_with_initial_event(self.conn, command)
        counts = self._binding_event_counts()
        original_lookup = ownership._existing_event

        def miss_once():
            calls = 0

            def lookup(conn, principal, key):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return None
                return original_lookup(conn, principal, key)

            return lookup

        with mock.patch.object(ownership, "_existing_event", side_effect=miss_once()):
            replay = ownership.create_binding_with_initial_event(self.conn, command)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.event_id, original.event_id)
        self.assertEqual(self._binding_event_counts(), counts)

        changed = dataclasses.replace(command, binding_role="delegated")
        with mock.patch.object(ownership, "_existing_event", side_effect=miss_once()):
            with self.assertRaises(ownership.OwnershipIdempotencyConflict):
                ownership.create_binding_with_initial_event(self.conn, changed)
        self.assertEqual(self._binding_event_counts(), counts)

    def test_account_native_bootstrap_is_atomic_idempotent_and_reconstructible(self):
        user_id = add_active_user(self.conn, suffix="bootstrap")
        before = database_snapshot(self.conn)

        created = ownership.ensure_account_native_principal(
            self.conn,
            user_id=user_id,
            environment_namespace="private_beta",
            occurred_at=NOW_TEXT,
        )
        self.assertTrue(created.created)
        self.assertRegex(created.principal_id, r"^prn_[0-9a-f]{32}$")
        self.assertRegex(created.binding_id, r"^pab_[0-9a-f]{32}$")
        self.assertRegex(created.initial_event_id, r"^obe_[0-9a-f]{32}$")
        self.assertEqual(created.environment_namespace, "private_beta")
        self.assertNotIn(user_id, repr(created))
        self.assertNotIn("user_id", {field.name for field in dataclasses.fields(created)})

        principal = self.conn.execute(
            "SELECT principal_type, lifecycle_status, claim_policy, "
            "exclusive_account_binding, version, provenance_json "
            "FROM product_principals WHERE principal_id=?",
            (created.principal_id,),
        ).fetchone()
        self.assertEqual(
            tuple(principal),
            ("account_native", "active", "account_native", 1, 1, "{}"),
        )
        binding = self.conn.execute(
            "SELECT principal_id, user_id, environment_namespace, binding_role, "
            "binding_status, version, latest_event_version, provenance_json "
            "FROM principal_account_bindings WHERE binding_id=?",
            (created.binding_id,),
        ).fetchone()
        self.assertEqual(
            tuple(binding),
            (
                created.principal_id,
                user_id,
                "private_beta",
                "owner",
                "active",
                1,
                1,
                "{}",
            ),
        )
        event = self.conn.execute(
            "SELECT principal_id, user_id, binding_id, environment_namespace, "
            "event_version, event_type, prior_status, resulting_status, actor_type, "
            "reason_code, approval_reference, idempotency_key, request_fingerprint, "
            "occurred_at, metadata_json FROM ownership_binding_events WHERE event_id=?",
            (created.initial_event_id,),
        ).fetchone()
        self.assertEqual(tuple(event[:12]), (
            created.principal_id,
            user_id,
            created.binding_id,
            "private_beta",
            1,
            "binding_activated",
            None,
            "active",
            "system",
            "account_native_bootstrap",
            None,
            "account-native-bootstrap-v1",
        ))
        ownership.validate_event_request_fingerprint(
            event[12],
            principal_id=event[0],
            binding_id=event[2],
            user_id=event[1],
            expected_event_version=event[4],
            event_type=event[5],
            prior_status=event[6],
            resulting_status=event[7],
            actor_type=event[8],
            reason_code=event[9],
            approval_reference=event[10],
            occurred_at=event[13],
            metadata=json.loads(event[14]),
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM legacy_owner_aliases").fetchone()[0], 0)
        ownership_text = json.dumps(
            {
                table: [tuple(row) for row in self.conn.execute(f"SELECT * FROM {table}")]
                for table in ownership.OWNERSHIP_TABLES
            },
            sort_keys=True,
        )
        for private_value in (
            "person-bootstrap@example.test",
            "google-subject-bootstrap",
            "local_user",
        ):
            self.assertNotIn(private_value, ownership_text)

        after_create = database_snapshot(self.conn)
        for table, fingerprint in before.items():
            if table not in ownership.OWNERSHIP_TABLES:
                self.assertEqual(after_create[table], fingerprint, table)
        replay = ownership.ensure_account_native_principal(
            self.conn,
            user_id=user_id,
            environment_namespace="private_beta",
            occurred_at=NOW_TEXT,
        )
        self.assertFalse(replay.created)
        self.assertEqual(
            (replay.principal_id, replay.binding_id, replay.initial_event_id),
            (created.principal_id, created.binding_id, created.initial_event_id),
        )
        self.assertEqual(database_snapshot(self.conn), after_create)

        self.conn.close()
        self.conn = self._connect(self.path)
        reconstructed = ownership.ensure_account_native_principal(
            self.conn,
            user_id=user_id,
            environment_namespace="private_beta",
            occurred_at=NOW_TEXT,
        )
        self.assertFalse(reconstructed.created)
        self.assertEqual(reconstructed.principal_id, created.principal_id)
        self.assertEqual(database_snapshot(self.conn), after_create)
        self._assert_database_clean(self.conn)

    def test_account_native_bootstrap_interruptions_leave_no_partial_lineage(self):
        user_id = add_active_user(self.conn, suffix="bootstrap-failure")
        exception_types = (RuntimeError, KeyboardInterrupt, SystemExit, GeneratorExit)
        failure_points = (
            "after_principal_insert",
            "after_binding_insert",
            "after_initial_event_insert",
        )
        for failure_point in failure_points:
            for exception_type in exception_types:
                def fail(point, target=failure_point, raised=exception_type):
                    if point == target:
                        raise raised("injected bootstrap interruption")

                with self.subTest(point=failure_point, exception=exception_type.__name__):
                    with self.assertRaises(exception_type):
                        ownership.ensure_account_native_principal(
                            self.conn,
                            user_id=user_id,
                            environment_namespace="private_beta",
                            occurred_at=NOW_TEXT,
                            failure_injector=fail,
                        )
                    self.assertFalse(self.conn.in_transaction)
                    self.assertEqual(self._ownership_counts(), (0, 0, 0, 0))
                    self._assert_database_clean(self.conn)

        recovered = ownership.ensure_account_native_principal(
            self.conn,
            user_id=user_id,
            environment_namespace="private_beta",
            occurred_at=NOW_TEXT,
        )
        self.assertTrue(recovered.created)
        self.assertEqual(self._ownership_counts(), (1, 0, 1, 1))

    def test_account_native_bootstrap_two_connections_converge(self):
        user_id = add_active_user(self.conn, suffix="bootstrap-race")
        self.conn.commit()
        start = threading.Barrier(3)

        def worker():
            connection = self._connect(self.path, timeout=5.0)
            try:
                start.wait(timeout=5)
                return ownership.ensure_account_native_principal(
                    connection,
                    user_id=user_id,
                    environment_namespace="private_beta",
                    occurred_at=NOW_TEXT,
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(worker) for _ in range(2)]
            start.wait(timeout=5)
            results = [future.result(timeout=15) for future in futures]

        self.assertEqual({result.created for result in results}, {False, True})
        self.assertEqual({result.principal_id for result in results}, {results[0].principal_id})
        self.assertEqual({result.binding_id for result in results}, {results[0].binding_id})
        self.assertEqual({result.initial_event_id for result in results}, {results[0].initial_event_id})
        self.assertEqual(self._ownership_counts(), (1, 0, 1, 1))
        self._assert_database_clean(self.conn)

    def test_account_native_bootstrap_fails_closed_for_invalid_or_historical_state(self):
        with self.assertRaises(ownership.OwnershipValidationError):
            ownership.ensure_account_native_principal(
                self.conn,
                user_id="not-an-account",
                environment_namespace="private_beta",
                occurred_at=NOW_TEXT,
            )
        with self.assertRaises(ownership.OwnershipStateConflict):
            ownership.ensure_account_native_principal(
                self.conn,
                user_id="usr_0123456789abcdef0123456789abcdef",
                environment_namespace="private_beta",
                occurred_at=NOW_TEXT,
            )
        self.assertEqual(self._ownership_counts(), (0, 0, 0, 0))

        inactive_path = Path(self.tmp.name) / "inactive.sqlite"
        inactive = install_ownership(inactive_path)
        try:
            inactive_user = add_active_user(inactive, suffix="bootstrap-inactive")
            accounts.suspend_user(
                inactive,
                user_id=inactive_user,
                expected_version=1,
                source="test_admin",
                idempotency_key="bootstrap-account-suspend",
            )
            with self.assertRaises(ownership.OwnershipStateConflict):
                ownership.ensure_account_native_principal(
                    inactive,
                    user_id=inactive_user,
                    environment_namespace="private_beta",
                    occurred_at=NOW_TEXT,
                )
            self.assertEqual(self._ownership_counts(inactive), (0, 0, 0, 0))
        finally:
            inactive.close()

        suspended_path = Path(self.tmp.name) / "suspended.sqlite"
        suspended = install_ownership(suspended_path)
        try:
            suspended_user = add_active_user(suspended, suffix="bootstrap-suspended")
            initial = ownership.ensure_account_native_principal(
                suspended,
                user_id=suspended_user,
                environment_namespace="private_beta",
                occurred_at=NOW_TEXT,
            )
            ownership.append_binding_event(
                suspended,
                ownership.BindingEventCommand(
                    principal_id=initial.principal_id,
                    binding_id=initial.binding_id,
                    user_id=suspended_user,
                    expected_event_version=2,
                    event_type="binding_suspended",
                    prior_status="active",
                    resulting_status="suspended",
                    actor_type="administrator",
                    reason_code="manual_review",
                    approval_reference=None,
                    idempotency_key="bootstrap-binding-suspend",
                    occurred_at=NOW_TEXT,
                    metadata={},
                ),
            )
            counts = self._ownership_counts(suspended)
            with self.assertRaises(ownership.OwnershipStateConflict):
                ownership.ensure_account_native_principal(
                    suspended,
                    user_id=suspended_user,
                    environment_namespace="private_beta",
                    occurred_at=NOW_TEXT,
                )
            self.assertEqual(self._ownership_counts(suspended), counts)
            ownership.append_binding_event(
                suspended,
                ownership.BindingEventCommand(
                    principal_id=initial.principal_id,
                    binding_id=initial.binding_id,
                    user_id=suspended_user,
                    expected_event_version=3,
                    event_type="binding_released",
                    prior_status="suspended",
                    resulting_status="released",
                    actor_type="administrator",
                    reason_code="manual_review",
                    approval_reference=None,
                    idempotency_key="bootstrap-binding-release",
                    occurred_at=NOW_TEXT,
                    metadata={},
                ),
            )
            counts = self._ownership_counts(suspended)
            with self.assertRaises(ownership.OwnershipStateConflict):
                ownership.ensure_account_native_principal(
                    suspended,
                    user_id=suspended_user,
                    environment_namespace="private_beta",
                    occurred_at=NOW_TEXT,
                )
            self.assertEqual(self._ownership_counts(suspended), counts)
        finally:
            suspended.close()

        ambiguous_path = Path(self.tmp.name) / "ambiguous.sqlite"
        ambiguous = install_ownership(ambiguous_path)
        try:
            ambiguous_user = add_active_user(ambiguous, suffix="bootstrap-ambiguous")
            for suffix in (701, 702):
                principal_id = add_principal(
                    ambiguous,
                    suffix=str(suffix),
                    environment="private_beta",
                    principal_type="account_native",
                    claim_policy="account_native",
                )
                binding_id = add_binding(
                    ambiguous,
                    principal_id,
                    ambiguous_user,
                    suffix=str(suffix),
                    environment="private_beta",
                )
                add_activation_event(
                    ambiguous,
                    principal_id,
                    ambiguous_user,
                    binding_id,
                    suffix=str(suffix),
                    environment="private_beta",
                )
            counts = self._ownership_counts(ambiguous)
            with self.assertRaises(ownership.OwnershipStateConflict):
                ownership.ensure_account_native_principal(
                    ambiguous,
                    user_id=ambiguous_user,
                    environment_namespace="private_beta",
                    occurred_at=NOW_TEXT,
                )
            self.assertEqual(self._ownership_counts(ambiguous), counts)
        finally:
            ambiguous.close()

        projection_path = Path(self.tmp.name) / "projection.sqlite"
        projection = install_ownership(projection_path)
        try:
            projection_user = add_active_user(projection, suffix="bootstrap-projection")
            projected = ownership.ensure_account_native_principal(
                projection,
                user_id=projection_user,
                environment_namespace="private_beta",
                occurred_at=NOW_TEXT,
            )
            update_guard = projection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_principal_account_bindings_update_guard'"
            ).fetchone()[0]
            projection.execute("DROP TRIGGER trg_principal_account_bindings_update_guard")
            projection.execute(
                "UPDATE principal_account_bindings SET binding_status='suspended', "
                "version=2, latest_event_version=2, updated_at=?, suspended_at=? "
                "WHERE binding_id=?",
                (NOW_TEXT, NOW_TEXT, projected.binding_id),
            )
            projection.execute(update_guard)
            projection.commit()
            counts = self._ownership_counts(projection)
            with self.assertRaises(ownership.OwnershipStateConflict):
                ownership.ensure_account_native_principal(
                    projection,
                    user_id=projection_user,
                    environment_namespace="private_beta",
                    occurred_at=NOW_TEXT,
                )
            self.assertEqual(self._ownership_counts(projection), counts)
            self._assert_database_clean(projection)
        finally:
            projection.close()

        attestation_path = Path(self.tmp.name) / "attestation.sqlite"
        attestation = install_ownership(attestation_path)
        try:
            attestation_user = add_active_user(attestation, suffix="bootstrap-attestation")
            attestation.execute("DROP TRIGGER trg_ownership_binding_events_no_update")
            with self.assertRaises(ownership.OwnershipStateConflict):
                ownership.ensure_account_native_principal(
                    attestation,
                    user_id=attestation_user,
                    environment_namespace="private_beta",
                    occurred_at=NOW_TEXT,
                )
            self.assertEqual(self._ownership_counts(attestation), (0, 0, 0, 0))
        finally:
            attestation.close()

    def test_reconciliation_recomputes_event_fingerprints(self):
        user_id = add_active_user(self.conn)
        principal_id = add_principal(self.conn, suffix="60")
        binding_id = add_binding(self.conn, principal_id, user_id, suffix="60")
        event_id = add_activation_event(
            self.conn, principal_id, user_id, binding_id, suffix="60"
        )
        self.conn.execute("DROP TRIGGER trg_ownership_binding_events_no_update")
        self.conn.execute(
            "UPDATE ownership_binding_events SET request_fingerprint=? WHERE event_id=?",
            (hashlib.sha256(b"different").hexdigest(), event_id),
        )
        report = reconcile_ownership(self.conn)
        self.assertIn(
            "ownership_event_request_fingerprint_mismatch", report["blocking_reasons"]
        )

    def _insert_principal(self, principal_id, metadata):
        self.conn.execute(
            "INSERT INTO product_principals "
            "(principal_id, environment_namespace, principal_type, lifecycle_status, "
            "claim_policy, exclusive_account_binding, version, created_at, updated_at, provenance_json) "
            "VALUES (?, 'test', 'legacy_profile', 'active', 'manual_approval', 1, 1, ?, ?, ?)",
            (principal_id, NOW_TEXT, NOW_TEXT, json.dumps(metadata, ensure_ascii=True)),
        )

    def _insert_activation_event(self, event_id, principal_id, user_id, binding_id, key):
        fingerprint = ownership.event_request_fingerprint(
            principal_id=principal_id,
            binding_id=binding_id,
            user_id=user_id,
            expected_event_version=1,
            event_type="binding_activated",
            prior_status=None,
            resulting_status="active",
            actor_type="administrator",
            reason_code="manual_approval",
            approval_reference=None,
            occurred_at=NOW_TEXT,
            metadata={},
        )
        self.conn.execute(
            "INSERT INTO ownership_binding_events "
            "(event_id, principal_id, user_id, binding_id, environment_namespace, event_version, "
            "event_type, prior_status, resulting_status, actor_type, reason_code, approval_reference, "
            "idempotency_key, request_fingerprint, occurred_at, metadata_json) "
            "VALUES (?, ?, ?, ?, 'test', 1, 'binding_activated', NULL, 'active', "
            "'administrator', 'manual_approval', NULL, ?, ?, ?, '{}')",
            (event_id, principal_id, user_id, binding_id, key, fingerprint, NOW_TEXT),
        )

    def _binding_event_counts(self):
        return (
            self.conn.execute("SELECT COUNT(*) FROM principal_account_bindings").fetchone()[0],
            self.conn.execute("SELECT COUNT(*) FROM ownership_binding_events").fetchone()[0],
        )

    @staticmethod
    def _connect(path, *, timeout=3.0):
        connection = sqlite3.connect(path, timeout=timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _ownership_counts(self, connection=None):
        connection = connection or self.conn
        return tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ownership.OWNERSHIP_TABLES
        )

    def _assert_database_clean(self, connection):
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(list(connection.execute("PRAGMA foreign_key_check")), [])


if __name__ == "__main__":
    unittest.main()
