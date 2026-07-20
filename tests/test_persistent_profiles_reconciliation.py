import copy
import hashlib
import json
import pickle
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from tests.persistent_profiles_reconciliation_test_support import (
    append_revision,
    canonical_fixture,
    canonical_json_for_profile,
    corrupt_one,
    finding_codes,
    installed_database,
    query_only_fingerprint,
    relaxed_profile_guards,
    seed_many_profiles,
    seed_profile,
    sha256_text,
    sidecars,
)
from tests.persistent_profiles_repository_test_support import (
    account_context,
    create_command,
)
from wahojobs.persistent_profiles_repository import create_persistent_profile
from tests.persistent_profiles_test_support import install_persistent_profiles
from wahojobs.persistent_profiles import (
    MIGRATION_005_CAPABILITIES,
    PersistentProfileSchemaCapabilities,
)
from wahojobs.persistent_profile_canonical_v2_schema import (
    attest_persistent_profile_canonical_v2_schema,
)
from wahojobs.persistent_profiles_reconciliation import (
    ENTITY_KINDS,
    FINDING_CODES,
    FINDING_SPEC_BY_CODE,
    FINDING_TAXONOMY,
    MAX_FINDINGS,
    MAX_REPORT_BYTES,
    PREREQUISITE_ONLY,
    REPORT_VERSION,
    ROW_REACHABLE,
    SCHEMA_UNREACHABLE,
    PersistentProfileReconciliationError,
    PersistentProfileReconciliationFinding,
    _FindingCollector,
    _bounded_report,
    reconcile_persistent_profiles,
)
import wahojobs.persistent_profiles_reconciliation as reconciliation_module


ROOT = Path(__file__).resolve().parent.parent

DURABLE_CORRUPTION_REGRESSION_CODES = frozenset(
    {
        "foreign_key_violation",
        "row_read_failure",
        "invalid_profile_id",
        "missing_principal_relationship",
        "profile_environment_mismatch",
        "missing_current_revision",
        "invalid_profile_timestamp",
        "missing_revision_history",
        "missing_current_view_row",
        "orphan_revision",
        "revision_relationship_mismatch",
        "invalid_revision_id",
        "revision_number_gap",
        "invalid_revision_chain",
        "unexpected_initial_revision",
        "unsupported_revision_kind",
        "invalid_lifecycle_transition",
        "revision_after_deletion_request",
        "invalid_correction_target",
        "invalid_revision_timestamp",
        "malformed_structured_profile",
        "invalid_canonical_profile_v2",
        "structured_profile_identity_mismatch",
        "noncanonical_structured_profile",
        "malformed_structured_hash",
        "structured_hash_mismatch",
        "canonical_schema_version_mismatch",
        "malformed_idempotency_key",
        "malformed_request_fingerprint",
        "invalid_initial_revision",
        "invalid_source_id",
        "orphan_source",
        "source_relationship_mismatch",
        "source_ordinal_gap",
        "unsupported_source_type",
        "malformed_source_payload",
        "invalid_source_timestamp",
        "malformed_source_hash",
        "source_hash_mismatch",
        "invalid_source_for_revision_kind",
        "source_bundle_hash_mismatch",
        "source_count_mismatch",
    }
)


class _InterruptAfterBeginConnection(sqlite3.Connection):
    private_marker = ""
    interrupted = False

    def execute(self, sql, parameters=()):
        cursor = super().execute(sql, parameters)
        if sql == "BEGIN" and not self.interrupted:
            self.interrupted = True
            raise KeyboardInterrupt(self.private_marker)
        return cursor


class _OneTimeRollbackFailureConnection(sqlite3.Connection):
    private_marker = "PRIVATE-ROLLBACK-FAILURE"
    failure_type = KeyboardInterrupt

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rollback_calls = 0

    def rollback(self):
        self.rollback_calls += 1
        if self.rollback_calls == 1:
            raise self.failure_type(self.private_marker)
        return super().rollback()


class _OneTimeSqliteRollbackFailureConnection(_OneTimeRollbackFailureConnection):
    private_marker = "PRIVATE-SQLITE-ROLLBACK-FAILURE"
    failure_type = sqlite3.OperationalError


class _SqlRollbackFallbackConnection(sqlite3.Connection):
    private_marker = "PRIVATE-SQL-ROLLBACK-FALLBACK"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rollback_calls = 0
        self.sql_rollback_calls = 0

    def rollback(self):
        self.rollback_calls += 1
        raise sqlite3.OperationalError(self.private_marker)

    def execute(self, sql, parameters=()):
        if sql == "ROLLBACK":
            self.sql_rollback_calls += 1
        return super().execute(sql, parameters)


class _CallerTransactionRollbackTrapConnection(sqlite3.Connection):
    private_marker = "PRIVATE-CALLER-ROLLBACK-TRAP"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rollback_calls = 0
        self.fail_rollback = True

    def rollback(self):
        self.rollback_calls += 1
        if self.fail_rollback:
            raise KeyboardInterrupt(self.private_marker)
        return super().rollback()


class PersistentProfileReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_number = 0
        self.connection = None
        self.path = Path(self.temp_dir.name) / "profiles.sqlite"
        self.reset_database()

    def tearDown(self):
        if self.connection is not None:
            self.connection.close()
        self.temp_dir.cleanup()

    def seed(self):
        return seed_profile(self.connection)

    def reset_database(self):
        if self.connection is not None:
            self.connection.close()
        self.database_number += 1
        self.path = (
            Path(self.temp_dir.name)
            / f"profiles-{self.database_number}.sqlite"
        )
        self.connection = installed_database(self.path)

    def reconcile(self, **kwargs):
        return reconcile_persistent_profiles(self.connection, **kwargs)

    def replace_connection(self, factory):
        self.connection.close()
        self.connection = sqlite3.connect(self.path, factory=factory)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        return self.connection

    def assert_sanitized_unavailable(self, report, marker):
        self.assertEqual(report.status, "unavailable")
        self.assertEqual(
            report.unavailable_reason,
            "internal_consistency_failure",
        )
        representations = (report.to_json(), str(report), repr(report))
        self.assertTrue(all(marker not in value for value in representations))
        self.assertIsNone(getattr(report, "__cause__", None))
        self.assertIsNone(getattr(report, "__context__", None))
        self.assertIsNone(getattr(report, "__traceback__", None))

    def test_clean_empty_and_initial_profiles_are_deterministic_and_read_only(self):
        before = query_only_fingerprint(self.path)
        empty = self.reconcile()
        self.assertEqual(empty.status, "clean")
        self.assertEqual(empty.total_findings, 0)
        principal, created, _reference = self.seed()
        first = self.reconcile()
        second = self.reconcile()
        self.assertEqual(first.status, "clean")
        self.assertEqual(first.to_json_bytes(), second.to_json_bytes())
        self.assertEqual(first.report_version, REPORT_VERSION)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(len(first.to_json_bytes()), len(first.to_json()))
        self.assertEqual(copy.copy(first), first)
        self.assertEqual(copy.deepcopy(first), first)
        self.assertEqual(pickle.loads(pickle.dumps(first)), first)
        after = query_only_fingerprint(self.path)
        self.assertNotEqual(before, after)  # only the intentional seed changed it
        stable = query_only_fingerprint(self.path)
        self.reconcile()
        self.assertEqual(query_only_fingerprint(self.path), stable)
        self.assertEqual(sidecars(self.path), [])
        self.assertIsNotNone(principal)
        self.assertIsNotNone(created)

    def test_valid_revision_and_lifecycle_states_have_no_false_positives(self):
        principal, created, reference = self.seed()
        edit = append_revision(
            self.connection, principal, reference, revision_kind="edit"
        )
        correction = append_revision(
            self.connection,
            principal,
            reference,
            expected_revision=2,
            revision_kind="correction",
            correction_of_revision_id=edit.revision_id,
        )
        append_revision(
            self.connection,
            principal,
            reference,
            expected_revision=3,
            revision_kind="archive",
        )
        append_revision(
            self.connection,
            principal,
            reference,
            expected_revision=4,
            revision_kind="reactivate",
        )
        append_revision(
            self.connection,
            principal,
            reference,
            expected_revision=5,
            revision_kind="deletion_request",
        )
        report = self.reconcile()
        self.assertEqual(report.status, "clean")
        self.assertEqual(report.total_findings, 0)
        self.assertEqual(dict(report.inventory)["product_profile_revisions"], 6)
        self.assertIsNotNone(created)
        self.assertIsNotNone(correction)

    def test_permitted_account_and_binding_eligibility_drift_is_not_corruption(self):
        account = account_context(self.connection, "211")
        create_persistent_profile(
            self.connection,
            create_command(account, idempotency_key="profile-create-00000211"),
        )
        self.connection.execute(
            "UPDATE users SET lifecycle_status='suspended', row_version=row_version+1, "
            "updated_at='2026-07-20T12:05:00+00:00' WHERE user_id IN "
            "(SELECT user_id FROM principal_account_bindings WHERE principal_id=?)",
            (account.principal_id,),
        )
        self.connection.execute(
            "UPDATE principal_account_bindings SET binding_status='released', "
            "version=version+1, latest_event_version=latest_event_version+1, "
            "updated_at='2026-07-20T12:05:00+00:00' WHERE principal_id=?",
            (account.principal_id,),
        )
        self.connection.commit()
        report = self.reconcile()
        self.assertEqual(report.status, "clean")
        self.assertEqual(report.total_findings, 0)

    def test_invalid_requests_are_sanitized_and_detached(self):
        cases = (
            {"max_findings": -1},
            {"max_findings": MAX_FINDINGS + 1},
            {"max_findings": True},
            {"summary_only": 1},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(PersistentProfileReconciliationError) as caught:
                    self.reconcile(**kwargs)
                self.assertEqual(caught.exception.reason_code, "invalid_reconciliation_request")
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)
                self.assertNotIn(str(self.path), str(caught.exception))

    def test_schema_capability_prerequisites_are_unavailable_without_repair(self):
        self.connection.close()
        self.connection = None
        m004_path = Path(self.temp_dir.name) / "m004.sqlite"
        m004 = install_persistent_profiles(m004_path)
        report = reconcile_persistent_profiles(m004)
        self.assertEqual(report.status, "unavailable")
        self.assertEqual(report.unavailable_reason, "schema_capability_unavailable")
        m004.close()

        self.reset_database()
        self.connection.execute(
            "DELETE FROM wahojobs_schema_migrations "
            "WHERE version='005_persistent_profile_canonical_v2'"
        )
        self.connection.commit()
        report = self.reconcile()
        self.assertEqual(report.status, "unavailable")
        self.assertEqual(report.total_findings, 0)

    def test_weakened_or_unexpected_schema_and_foreign_keys_off_are_unavailable(self):
        self.connection.execute("DROP TRIGGER trg_product_profiles_no_update")
        self.connection.commit()
        self.assertEqual(self.reconcile().status, "unavailable")
        self.reset_database()
        self.connection.execute(
            "CREATE TABLE product_profiles_unexpected_object(value TEXT)"
        )
        self.connection.commit()
        self.assertEqual(self.reconcile().status, "unavailable")
        self.reset_database()
        self.connection.execute("PRAGMA foreign_keys = OFF")
        self.assertEqual(self.reconcile().unavailable_reason, "schema_capability_unavailable")

    def test_partial_table_and_altered_current_view_are_unavailable(self):
        self.connection.execute(
            "ALTER TABLE product_profiles ADD COLUMN unexpected_value TEXT"
        )
        self.connection.commit()
        self.assertEqual(self.reconcile().status, "unavailable")

        self.reset_database()
        self.connection.execute("DROP VIEW current_product_profiles")
        self.connection.execute(
            "CREATE VIEW current_product_profiles AS SELECT "
            "profile_id, principal_id, environment_namespace, initial_revision_id, "
            "created_at AS profile_created_at, NULL AS current_revision_id, "
            "NULL AS current_revision_number, NULL AS current_revision_kind, "
            "NULL AS lifecycle_status, NULL AS canonical_schema_version, "
            "NULL AS structured_profile_json, NULL AS structured_profile_sha256, "
            "NULL AS revised_at FROM product_profiles"
        )
        self.connection.commit()
        self.assertEqual(self.reconcile().status, "unavailable")

    def test_wrong_capability_descriptor_is_unavailable(self):
        capabilities = PersistentProfileSchemaCapabilities(
            migration_version=MIGRATION_005_CAPABILITIES.migration_version,
            canonical_versions=MIGRATION_005_CAPABILITIES.canonical_versions,
            source_types=frozenset({"confirmed_about_you_text"}),
            lifecycle_source_schema_versions=(
                MIGRATION_005_CAPABILITIES.lifecycle_source_schema_versions
            ),
        )
        report = reconcile_persistent_profiles(
            self.connection, _capabilities=capabilities
        )
        self.assertEqual(report.status, "unavailable")
        self.assertEqual(report.unavailable_reason, "schema_capability_unavailable")

    def test_closed_connection_and_injected_select_failure_are_sanitized(self):
        closed = sqlite3.connect(":memory:")
        closed.close()
        report = reconcile_persistent_profiles(closed)
        self.assertEqual(report.status, "unavailable")
        self.assertEqual(report.unavailable_reason, "internal_consistency_failure")

        self.connection.set_authorizer(
            lambda action, *_args: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_SELECT
                else sqlite3.SQLITE_OK
            )
        )
        report = self.reconcile()
        self.connection.set_authorizer(None)
        self.assertEqual(report.status, "unavailable")
        self.assertEqual(report.unavailable_reason, "internal_consistency_failure")

    def test_transaction_ownership_preserves_caller_commit_and_rollback(self):
        self.connection.execute("CREATE TEMP TABLE caller_work(value TEXT)")
        self.connection.execute("BEGIN")
        self.connection.execute("INSERT INTO caller_work VALUES ('rollback')")
        report = self.reconcile()
        self.assertEqual(report.status, "clean")
        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM caller_work").fetchone()[0], 1)
        self.connection.rollback()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM caller_work").fetchone()[0], 0)

        self.connection.execute("BEGIN")
        self.connection.execute("INSERT INTO caller_work VALUES ('commit')")
        self.assertEqual(self.reconcile().status, "clean")
        self.assertTrue(self.connection.in_transaction)
        self.connection.commit()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM caller_work").fetchone()[0], 1)

    def test_interrupt_after_owned_begin_is_sanitized_and_transaction_is_ended(self):
        marker = "PRIVATE-BEGIN-INTERRUPT"
        _InterruptAfterBeginConnection.private_marker = marker
        connection = sqlite3.connect(
            self.path,
            factory=_InterruptAfterBeginConnection,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            report = reconcile_persistent_profiles(connection)
            self.assertEqual(report.status, "unavailable")
            self.assertEqual(
                report.unavailable_reason,
                "internal_consistency_failure",
            )
            self.assertFalse(connection.in_transaction)
            self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
            self.assertNotIn(marker, report.to_json())
        finally:
            connection.close()

    def test_owned_cleanup_retries_keyboard_interrupt_and_sqlite_error(self):
        factories = (
            _OneTimeRollbackFailureConnection,
            _OneTimeSqliteRollbackFailureConnection,
        )
        for factory in factories:
            with self.subTest(factory=factory.__name__):
                self.reset_database()
                connection = self.replace_connection(factory)
                report = self.reconcile()
                self.assert_sanitized_unavailable(report, factory.private_marker)
                self.assertEqual(connection.rollback_calls, 2)
                self.assertFalse(connection.in_transaction)
                self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

    def test_owned_cleanup_uses_fixed_sql_rollback_after_bounded_retries(self):
        connection = self.replace_connection(_SqlRollbackFallbackConnection)
        report = self.reconcile()
        self.assert_sanitized_unavailable(report, connection.private_marker)
        self.assertEqual(connection.rollback_calls, 2)
        self.assertEqual(connection.sql_rollback_calls, 1)
        self.assertFalse(connection.in_transaction)
        self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

    def test_report_interrupt_and_rollback_interrupt_are_both_sanitized(self):
        connection = self.replace_connection(_OneTimeRollbackFailureConnection)
        marker = "PRIVATE-REPORT-CREATION-INTERRUPT"
        with mock.patch.object(
            reconciliation_module,
            "_bounded_report",
            side_effect=KeyboardInterrupt(marker),
        ):
            report = self.reconcile()
        self.assert_sanitized_unavailable(report, marker)
        self.assertNotIn(connection.private_marker, report.to_json())
        self.assertEqual(connection.rollback_calls, 2)
        self.assertFalse(connection.in_transaction)
        self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)

    def test_interrupts_in_major_scan_phases_are_sanitized_and_cleaned_up(self):
        phases = (
            "attest_persistent_profile_canonical_v2_schema",
            "_profile_inventory",
            "_scan_revisions_and_sources",
            "_bounded_report",
        )
        for phase in phases:
            with self.subTest(phase=phase):
                marker = f"PRIVATE-{phase}-INTERRUPT"
                with mock.patch.object(
                    reconciliation_module,
                    phase,
                    side_effect=KeyboardInterrupt(marker),
                ):
                    report = self.reconcile()
                self.assertEqual(report.status, "unavailable")
                self.assertFalse(self.connection.in_transaction)
                self.assertEqual(self.connection.execute("SELECT 1").fetchone()[0], 1)
                self.assertNotIn(marker, report.to_json())

    def test_interrupt_does_not_touch_caller_owned_write_transaction(self):
        self.connection.execute("CREATE TEMP TABLE caller_interrupt(value TEXT)")
        self.connection.execute("BEGIN")
        self.connection.execute("INSERT INTO caller_interrupt VALUES ('preserved')")
        marker = "PRIVATE-CALLER-INTERRUPT"
        with mock.patch.object(
            reconciliation_module,
            "_scan_snapshot",
            side_effect=KeyboardInterrupt(marker),
        ):
            report = self.reconcile()
        self.assertEqual(report.status, "unavailable")
        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(
            self.connection.execute(
                "SELECT value FROM caller_interrupt"
            ).fetchone()[0],
            "preserved",
        )
        self.assertNotIn(marker, report.to_json())
        self.connection.commit()
        self.assertFalse(self.connection.in_transaction)

    def test_cleanup_never_touches_caller_owned_read_or_write_transactions(self):
        connection = self.replace_connection(_CallerTransactionRollbackTrapConnection)
        connection.execute("CREATE TEMP TABLE caller_owned(value TEXT)")

        connection.execute("BEGIN")
        connection.execute("SELECT COUNT(*) FROM product_profiles").fetchone()
        self.assertEqual(self.reconcile().status, "clean")
        self.assertTrue(connection.in_transaction)
        self.assertEqual(connection.rollback_calls, 0)
        connection.fail_rollback = False
        connection.rollback()
        self.assertFalse(connection.in_transaction)

        connection.fail_rollback = True
        connection.rollback_calls = 0
        connection.execute("BEGIN")
        connection.execute("INSERT INTO caller_owned VALUES ('preserved')")
        self.assertEqual(self.reconcile().status, "clean")
        self.assertTrue(connection.in_transaction)
        self.assertEqual(connection.rollback_calls, 0)
        self.assertEqual(
            connection.execute("SELECT value FROM caller_owned").fetchone()[0],
            "preserved",
        )
        connection.commit()
        self.assertFalse(connection.in_transaction)

    def test_lock_contention_returns_unavailable_without_retry(self):
        self.connection.close()
        writer = sqlite3.connect(self.path, timeout=0.1)
        reader = sqlite3.connect(self.path, timeout=0.05)
        reader.execute("PRAGMA foreign_keys = ON")
        writer.execute("BEGIN EXCLUSIVE")
        try:
            report = reconcile_persistent_profiles(reader)
        finally:
            writer.rollback()
            writer.close()
            reader.close()
        self.connection = None
        self.assertEqual(report.status, "unavailable")
        self.assertEqual(report.unavailable_reason, "temporary_contention")

    def test_profile_and_revision_chain_corruption_is_specific_and_contained(self):
        _principal, _created, _reference = self.seed()
        corrupt_one(
            self.connection,
            "UPDATE product_profiles SET profile_id='invalid-profile'",
        )
        report = self.reconcile()
        self.assertIn("invalid_profile_id", finding_codes(report))
        self.assertIn("orphan_revision", finding_codes(report))
        self.assertNotIn("invalid-profile", report.to_json())

    def test_missing_principal_environment_and_history_are_detected(self):
        self.seed()
        with relaxed_profile_guards(self.connection):
            self.connection.execute(
                "UPDATE product_profiles SET principal_id='prn_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaab', "
                "environment_namespace='other'"
            )
            self.connection.execute("DELETE FROM product_profile_sources")
            self.connection.execute("DELETE FROM product_profile_revisions")
        report = self.reconcile()
        codes = finding_codes(report)
        self.assertIn("missing_principal_relationship", codes)
        self.assertIn("missing_revision_history", codes)
        self.assertIn("missing_current_revision", codes)
        self.assertIn("missing_current_view_row", codes)

    def test_revision_gap_initial_lifecycle_and_correction_findings(self):
        principal, _created, reference = self.seed()
        edit = append_revision(
            self.connection, principal, reference, revision_kind="edit"
        )
        append_revision(
            self.connection,
            principal,
            reference,
            expected_revision=2,
            revision_kind="correction",
            correction_of_revision_id=edit.revision_id,
        )
        with relaxed_profile_guards(self.connection):
            self.connection.execute(
                "UPDATE product_profile_revisions SET revision_number=5, "
                "revision_kind='initial', lifecycle_status='archived', "
                "correction_of_revision_id=NULL WHERE revision_number=3"
            )
        codes = finding_codes(self.reconcile())
        self.assertIn("revision_number_gap", codes)
        self.assertIn("unexpected_initial_revision", codes)
        self.assertIn("invalid_lifecycle_transition", codes)

    def test_revision_after_deletion_request_and_invalid_chain_are_detected(self):
        principal, _created, reference = self.seed()
        append_revision(self.connection, principal, reference, revision_kind="edit")
        with relaxed_profile_guards(self.connection):
            self.connection.execute(
                "UPDATE product_profile_revisions SET revision_kind='deletion_request', "
                "lifecycle_status='deletion_requested' WHERE revision_number=1"
            )
            self.connection.execute(
                "UPDATE product_profile_revisions SET previous_revision_id=NULL "
                "WHERE revision_number=2"
            )
        codes = finding_codes(self.reconcile())
        self.assertIn("revision_after_deletion_request", codes)
        self.assertIn("invalid_revision_chain", codes)

    def test_remaining_row_reachable_corruptions_are_emitted_by_real_scan(self):
        mutations = {
            "profile_environment_mismatch": (
                "UPDATE product_profiles SET environment_namespace='other'"
            ),
            "invalid_profile_timestamp": (
                "UPDATE product_profiles SET created_at='not-a-time'"
            ),
            "revision_relationship_mismatch": (
                "UPDATE product_profile_revisions SET principal_id="
                "'prn_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaab'"
            ),
            "invalid_revision_id": (
                "UPDATE product_profile_revisions SET revision_id='bad'"
            ),
            "invalid_initial_revision": (
                "UPDATE product_profile_revisions SET revision_kind='edit' "
                "WHERE revision_number=1"
            ),
            "unsupported_revision_kind": (
                "UPDATE product_profile_revisions SET revision_kind='unsupported'"
            ),
            "invalid_correction_target": (
                "UPDATE product_profile_revisions SET correction_of_revision_id=revision_id"
            ),
            "invalid_source_for_revision_kind": (
                "UPDATE product_profile_sources SET "
                "source_type='confirmed_lifecycle_action'"
            ),
        }
        for expected_code, sql in mutations.items():
            with self.subTest(code=expected_code):
                self.reset_database()
                self.seed()
                corrupt_one(self.connection, sql)
                report = self.reconcile()
                findings = [
                    finding
                    for finding in report.findings
                    if finding.code == expected_code
                ]
                self.assertTrue(findings, report.to_json())
                spec = FINDING_SPEC_BY_CODE[expected_code]
                self.assertEqual(findings[0].severity, spec.severity)
                self.assertIn(findings[0].entity_kind, spec.entity_kinds)

    def test_row_read_failure_is_bounded_and_does_not_stop_other_rows(self):
        self.seed()
        seed_profile(self.connection, "102")
        marker = "PRIVATE-ROW-FAILURE"
        with relaxed_profile_guards(self.connection):
            self.connection.execute(
                "UPDATE product_profile_revisions "
                "SET idempotency_key=CAST(? AS BLOB) "
                "WHERE rowid=(SELECT rowid FROM product_profile_revisions "
                "ORDER BY profile_id LIMIT 1)",
                (marker,),
            )
            self.connection.execute(
                "UPDATE product_profile_revisions SET created_at='invalid' "
                "WHERE rowid=(SELECT rowid FROM product_profile_revisions "
                "ORDER BY profile_id LIMIT 1 OFFSET 1)"
            )
        self.assertEqual(
            attest_persistent_profile_canonical_v2_schema(self.connection)["state"],
            "correctly_installed",
        )
        report = self.reconcile()
        self.assertIn("row_read_failure", finding_codes(report))
        self.assertIn("invalid_revision_timestamp", finding_codes(report))
        self.assertEqual(
            dict(report.finding_counts_by_code)["row_read_failure"],
            1,
        )
        self.assertEqual(dict(report.inventory)["product_profiles"], 2)
        self.assertNotIn(marker, report.to_json())

    def test_canonical_v2_corruption_matrix(self):
        mutations = {
            "malformed_structured_profile": "UPDATE product_profile_revisions SET structured_profile_json='{'",
            "invalid_canonical_profile_v2": (
                "UPDATE product_profile_revisions SET structured_profile_json="
                "'{\"identity\":{\"profile_id\":\"prf_0123456789abcdef0123456789abcdef\"},"
                "\"schema_version\":\"canonical_profile_v2\"}'"
            ),
            "structured_profile_identity_mismatch": (
                "UPDATE product_profile_revisions SET structured_profile_json="
                "replace(structured_profile_json, profile_id, 'prf_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaab')"
            ),
            "canonical_schema_version_mismatch": (
                "UPDATE product_profile_revisions SET canonical_schema_version='canonical_profile_v1'"
            ),
            "malformed_structured_hash": (
                "UPDATE product_profile_revisions SET structured_profile_sha256=upper(structured_profile_sha256)"
            ),
            "structured_hash_mismatch": (
                "UPDATE product_profile_revisions SET structured_profile_sha256="
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaab'"
            ),
        }
        for expected_code, sql in mutations.items():
            with self.subTest(code=expected_code):
                self.reset_database()
                self.seed()
                corrupt_one(self.connection, sql)
                report = self.reconcile()
                self.assertIn(expected_code, finding_codes(report))
                self.assertNotIn("Confirmed profile background", report.to_json())

    def test_noncanonical_and_duplicate_key_json_are_detected_without_content_leak(self):
        _principal, created, _reference = self.seed()
        canonical = canonical_json_for_profile(created.profile_id)
        pretty = json.dumps(json.loads(canonical), ensure_ascii=False, sort_keys=True, indent=2)
        with relaxed_profile_guards(self.connection):
            self.connection.execute(
                "UPDATE product_profile_revisions SET structured_profile_json=?, "
                "structured_profile_sha256=?",
                (pretty, sha256_text(canonical)),
            )
        self.assertIn("noncanonical_structured_profile", finding_codes(self.reconcile()))

        duplicate = canonical[:-1] + ',"schema_version":"canonical_profile_v2"}'
        with relaxed_profile_guards(self.connection):
            self.connection.execute(
                "UPDATE product_profile_revisions SET structured_profile_json=?",
                (duplicate,),
            )
        report = self.reconcile()
        self.assertIn("malformed_structured_profile", finding_codes(report))
        self.assertNotIn(created.profile_id, report.to_json())

    def test_source_corruption_matrix(self):
        mutations = {
            "invalid_source_id": "UPDATE product_profile_sources SET source_id='bad'",
            "source_ordinal_gap": "UPDATE product_profile_sources SET source_ordinal=2",
            "unsupported_source_type": "UPDATE product_profile_sources SET source_type='unknown'",
            "malformed_source_payload": "UPDATE product_profile_sources SET source_content=char(0)",
            "malformed_source_hash": "UPDATE product_profile_sources SET source_content_sha256=upper(source_content_sha256)",
            "source_hash_mismatch": (
                "UPDATE product_profile_sources SET source_content_sha256="
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaab'"
            ),
            "source_relationship_mismatch": (
                "UPDATE product_profile_sources SET profile_id="
                "'prf_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaab'"
            ),
        }
        for expected_code, sql in mutations.items():
            with self.subTest(code=expected_code):
                self.reset_database()
                self.seed()
                corrupt_one(self.connection, sql)
                self.assertIn(expected_code, finding_codes(self.reconcile()))

    def test_missing_extra_orphan_and_bundle_source_findings(self):
        self.seed()
        with relaxed_profile_guards(self.connection):
            self.connection.execute("DELETE FROM product_profile_sources")
        codes = finding_codes(self.reconcile())
        self.assertIn("source_count_mismatch", codes)
        self.assertIn("source_bundle_hash_mismatch", codes)

        self.reset_database()
        self.seed()
        with relaxed_profile_guards(self.connection):
            self.connection.execute("DELETE FROM product_profile_revisions")
        self.assertIn("orphan_source", finding_codes(self.reconcile()))

    def test_idempotency_and_timestamp_corruption(self):
        self.seed()
        with relaxed_profile_guards(self.connection):
            self.connection.execute(
                "UPDATE product_profile_revisions SET idempotency_key='short', "
                "request_fingerprint='ABC', created_at='2026-02-30T12:00:00+00:00'"
            )
            self.connection.execute(
                "UPDATE product_profile_sources SET accepted_at='2026-02-30T12:00:00+00:00'"
            )
        codes = finding_codes(self.reconcile())
        self.assertIn("malformed_idempotency_key", codes)
        self.assertIn("malformed_request_fingerprint", codes)
        self.assertIn("invalid_revision_timestamp", codes)
        self.assertIn("invalid_source_timestamp", codes)

    def test_request_fingerprint_checks_use_only_durable_properties(self):
        self.seed()
        alternate_valid_digest = "a" * 64
        with relaxed_profile_guards(self.connection):
            self.connection.execute(
                "UPDATE product_profile_revisions SET request_fingerprint=?",
                (alternate_valid_digest,),
            )
        report = self.reconcile()
        self.assertEqual(report.status, "clean")
        self.assertEqual(report.total_findings, 0)

    def test_foreign_key_and_independent_orphan_detection_both_run(self):
        self.seed()
        with relaxed_profile_guards(self.connection):
            self.connection.execute("DELETE FROM product_profile_revisions")
        report = self.reconcile()
        self.assertGreater(report.foreign_key_violation_count, 0)
        self.assertIn("foreign_key_violation", finding_codes(report))
        self.assertIn("orphan_source", finding_codes(report))

    def test_multiple_findings_truncation_and_summary_only_keep_exact_counts(self):
        self.seed()
        with relaxed_profile_guards(self.connection):
            self.connection.execute(
                "UPDATE product_profile_revisions SET revision_number=9, "
                "revision_kind='unknown', lifecycle_status='unknown', "
                "structured_profile_sha256='BAD', idempotency_key='x', "
                "request_fingerprint='y', created_at='invalid'"
            )
            self.connection.execute(
                "UPDATE product_profile_sources SET source_ordinal=9, "
                "source_type='unknown', source_content_sha256='BAD', accepted_at='invalid'"
            )
        full = self.reconcile(max_findings=2)
        summary = self.reconcile(summary_only=True)
        self.assertGreater(full.total_findings, 2)
        self.assertEqual(len(full.findings), 2)
        self.assertTrue(full.findings_truncated)
        self.assertEqual(summary.total_findings, full.total_findings)
        self.assertEqual(summary.finding_counts_by_code, full.finding_counts_by_code)
        self.assertEqual(summary.findings, ())
        self.assertTrue(summary.findings_truncated)
        self.assertLessEqual(len(full.to_json_bytes()), MAX_REPORT_BYTES)

    def test_report_size_bound_reduces_display_only_and_preserves_exact_totals(self):
        collector = _FindingCollector(MAX_FINDINGS)
        for ordinal in range(1, MAX_FINDINGS + 1):
            collector.add(
                "current_view_mismatch",
                "profile",
                profile_ordinal=ordinal,
            )
        with mock.patch.object(reconciliation_module, "MAX_REPORT_BYTES", 4_096):
            report = _bounded_report(
                collector,
                inventory={
                    "product_profiles": 10_000,
                    "product_profile_revisions": 10_000,
                    "product_profile_sources": 10_000,
                    "current_product_profiles": 10_000,
                    "distinct_principals": 10_000,
                },
                lifecycle_counts={"active": 10_000},
                revision_kind_counts={"initial": 10_000},
                source_type_counts={"confirmed_about_you_text": 10_000},
                foreign_key_violation_count=0,
            )
        self.assertEqual(report.total_findings, MAX_FINDINGS)
        self.assertEqual(dict(report.finding_counts_by_code)["current_view_mismatch"], MAX_FINDINGS)
        self.assertTrue(report.findings_truncated)
        self.assertLessEqual(len(report.to_json_bytes()), 4_096)

    def test_taxonomy_is_stable_privacy_safe_and_exhaustive(self):
        required = {
            "foreign_key_violation",
            "row_read_failure",
            "invalid_profile_id",
            "missing_principal_relationship",
            "revision_number_gap",
            "malformed_structured_profile",
            "structured_hash_mismatch",
            "orphan_source",
            "source_bundle_hash_mismatch",
            "malformed_idempotency_key",
            "current_view_mismatch",
        }
        self.assertEqual(len(FINDING_CODES), 52)
        self.assertTrue(required <= FINDING_CODES)
        self.assertEqual(
            {spec.code for spec in FINDING_TAXONOMY},
            FINDING_CODES,
        )
        self.assertEqual(
            {
                spec.code
                for spec in FINDING_TAXONOMY
                if spec.reachability == ROW_REACHABLE
            },
            DURABLE_CORRUPTION_REGRESSION_CODES,
        )
        self.assertEqual(
            sum(spec.reachability == ROW_REACHABLE for spec in FINDING_TAXONOMY),
            42,
        )
        self.assertEqual(
            sum(
                spec.reachability == SCHEMA_UNREACHABLE
                for spec in FINDING_TAXONOMY
            ),
            10,
        )
        self.assertEqual(
            sum(
                spec.reachability == PREREQUISITE_ONLY
                for spec in FINDING_TAXONOMY
            ),
            0,
        )
        documentation = (
            ROOT / "docs" / "persistent_profile_services.md"
        ).read_text(encoding="utf-8")
        implementation = (
            ROOT / "wahojobs" / "persistent_profiles_reconciliation.py"
        ).read_text(encoding="utf-8")
        for code in FINDING_CODES:
            spec = FINDING_SPEC_BY_CODE[code]
            self.assertEqual(spec.severity, "error")
            self.assertTrue(spec.entity_kinds <= ENTITY_KINDS)
            self.assertTrue(spec.meaning)
            if spec.reachability != ROW_REACHABLE:
                self.assertTrue(spec.reachability_reason)
            locator = sorted(spec.required_any_locators)[0]
            finding = PersistentProfileReconciliationFinding(
                code,
                sorted(spec.entity_kinds)[0],
                **{locator: 1},
            )
            payload = finding.to_dict()
            serialized = json.dumps(payload, sort_keys=True)
            self.assertNotIn("profile_id", payload)
            self.assertNotIn("revision_id", payload)
            self.assertNotIn("source_id", payload)
            self.assertNotIn("content", payload)
            self.assertIsNone(re.search(r"\b[0-9a-f]{32,64}\b", serialized))
            self.assertIn(code, documentation)
        self.assertIn("52 codes", documentation)
        self.assertRegex(documentation, r"Forty-two are\s+row-reachable")
        self.assertIn("Ten are", documentation)
        self.assertRegex(documentation, r"No finding is\s+prerequisite-only")
        emitted_literals = set(
            re.findall(r'collector\.add\(\s*"([a-z0-9_]+)"', implementation)
        )
        self.assertTrue(emitted_literals <= FINDING_CODES)

    def test_finding_model_rejects_private_free_form_values(self):
        marker = "PRIVATE-FINDING-MARKER"
        invalid_calls = (
            lambda: PersistentProfileReconciliationFinding(
                marker,
                "profile",
                profile_ordinal=1,
            ),
            lambda: PersistentProfileReconciliationFinding(
                "invalid_profile_id",
                marker,
                profile_ordinal=1,
            ),
            lambda: PersistentProfileReconciliationFinding(
                "invalid_profile_id",
                "profile",
                profile_ordinal=marker,
            ),
            lambda: PersistentProfileReconciliationFinding(
                "invalid_profile_id",
                "profile",
                profile_ordinal=1,
                severity=marker,
            ),
        )
        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises((TypeError, ValueError)) as caught:
                    call()
                self.assertNotIn(marker, str(caught.exception))
                self.assertNotIn(marker, repr(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)

        finding = PersistentProfileReconciliationFinding(
            "invalid_profile_id",
            "profile",
            profile_ordinal=1,
        )
        representations = (
            repr(finding),
            str(finding),
            repr(finding.to_dict()),
            repr(copy.copy(finding)),
            repr(copy.deepcopy(finding)),
            repr(pickle.loads(pickle.dumps(finding))),
        )
        self.assertTrue(all(marker not in value for value in representations))

    def test_schema_unreachable_taxonomy_is_attested(self):
        schema_codes = {
            spec.code
            for spec in FINDING_TAXONOMY
            if spec.reachability == SCHEMA_UNREACHABLE
        }
        self.assertEqual(
            schema_codes,
            {
                "duplicate_principal_profile",
                "foreign_current_revision",
                "stale_current_revision",
                "profile_lifecycle_mismatch",
                "duplicate_revision_number",
                "duplicate_source_ordinal",
                "idempotency_scope_conflict",
                "unexpected_current_view_row",
                "duplicate_current_view_row",
                "current_view_mismatch",
            },
        )
        for code in schema_codes:
            with self.subTest(code=code):
                self.assertTrue(FINDING_SPEC_BY_CODE[code].reachability_reason)
                self.reset_database()
                if code in {
                    "duplicate_principal_profile",
                    "duplicate_revision_number",
                    "duplicate_source_ordinal",
                    "idempotency_scope_conflict",
                }:
                    table = {
                        "duplicate_principal_profile": "product_profiles",
                        "duplicate_revision_number": "product_profile_revisions",
                        "duplicate_source_ordinal": "product_profile_sources",
                        "idempotency_scope_conflict": "product_profile_revisions",
                    }[code]
                    self.connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN taxonomy_drift TEXT"
                    )
                else:
                    self.connection.execute("DROP VIEW current_product_profiles")
                    self.connection.execute(
                        "CREATE VIEW current_product_profiles AS "
                        "SELECT profile_id FROM product_profiles"
                    )
                self.connection.commit()
                report = self.reconcile()
                self.assertEqual(report.status, "unavailable")
                self.assertEqual(
                    report.unavailable_reason,
                    "schema_capability_unavailable",
                )
                self.assertEqual(report.total_findings, 0)

    def test_realistic_thousand_profile_scan_is_exact_and_constant_query_scale(self):
        seed_many_profiles(self.connection, 1_000)
        statements = 0

        def trace(sql):
            nonlocal statements
            if sql.lstrip().upper().startswith(("SELECT", "WITH", "PRAGMA")):
                statements += 1

        self.connection.set_trace_callback(trace)
        started = time.perf_counter()
        report = self.reconcile(summary_only=True)
        elapsed = time.perf_counter() - started
        self.connection.set_trace_callback(None)
        self.assertEqual(report.status, "clean")
        self.assertEqual(dict(report.inventory)["product_profiles"], 1_000)
        self.assertEqual(dict(report.inventory)["product_profile_revisions"], 2_000)
        self.assertEqual(dict(report.inventory)["product_profile_sources"], 4_000)
        self.assertEqual(statements, 65)
        self.assertGreaterEqual(elapsed, 0.0)  # informational, not a platform gate

    def test_reconciliation_import_is_side_effect_free_and_runtime_isolated(self):
        source = (
            ROOT / "wahojobs" / "persistent_profiles_reconciliation.py"
        ).read_text(encoding="utf-8")
        for prohibited in (
            "sqlite3.connect(",
            "executescript(",
            "CREATE TEMP",
            "journal_mode=",
            "foreign_keys = OFF",
        ):
            self.assertNotIn(prohibited, source)
        runtime_files = (
            ROOT / "wahojobs" / "__init__.py",
            ROOT / "scripts" / "local_product_app.py",
            ROOT / "scripts" / "profile_match_digest.py",
            ROOT / "wahojobs" / "pipeline_actions.py",
            ROOT / "wahojobs" / "crawler" / "__init__.py",
        )
        for path in runtime_files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("persistent_profiles_reconciliation", text)

        script = r'''
import builtins
import socket
import sqlite3

def blocked(*args, **kwargs):
    raise RuntimeError("side effect")

sqlite3.connect = blocked
socket.socket = blocked
original_open = builtins.open
def guarded_open(file, mode="r", *args, **kwargs):
    if any(flag in mode for flag in ("w", "a", "x", "+")):
        raise RuntimeError("file write")
    return original_open(file, mode, *args, **kwargs)
builtins.open = guarded_open
import wahojobs.persistent_profiles_reconciliation as reconciliation
print(reconciliation.REPORT_VERSION)
'''
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), REPORT_VERSION)


if __name__ == "__main__":
    unittest.main()
