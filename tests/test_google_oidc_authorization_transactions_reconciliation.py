from __future__ import annotations

import contextlib
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

import scripts.google_oidc_authorization_transactions_reconcile as cli
import scripts.google_oidc_authorization_transactions_migration as migration
from tests.google_oidc_authorization_transactions_test_support import (
    ManualClock,
    NOW,
    durable_transaction_database,
    key_authority,
    reconstructed_gateway,
    sockets_blocked,
    transaction_rows,
)
from tests.persistent_profile_canonical_v2_test_support import (
    install_canonical_v2_profiles,
)
import wahojobs.google_oidc_authorization_transaction_reconciliation as reconciliation
import wahojobs.google_oidc_authorization_transaction_schema as transaction_schema
from wahojobs.google_oidc_authorization_transaction_reconciliation import (
    GoogleOidcAuthorizationTransactionReconciliationError,
    reconcile_google_oidc_authorization_transactions as _reconcile,
)
from wahojobs.google_oidc_authorization_transaction_repository import (
    prepare_google_oidc_authorization_transaction,
)


def reconcile_google_oidc_authorization_transactions(*args, **kwargs):
    kwargs.setdefault("source_guarantees_no_sidecar_creation", True)
    return _reconcile(*args, **kwargs)


class GoogleOidcAuthorizationTransactionReconciliationTests(unittest.TestCase):
    def test_clean_snapshot_is_read_only_and_does_not_decrypt(self):
        with durable_transaction_database(suffix="reconciliation-clean") as database:
            authority = key_authority()
            harness = reconstructed_gateway(
                subject="google-subject-reconciliation-clean"
            )
            try:
                with sockets_blocked():
                    prepared = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                before = _database_fingerprint(database.path)
                with mock.patch.object(
                    reconciliation,
                    "_clock_now",
                    return_value=NOW,
                ), mock.patch(
                    "wahojobs.google_oidc_transaction_protection._unprotect_material",
                    side_effect=AssertionError("reconciliation_must_not_decrypt"),
                ):
                    report = reconcile_google_oidc_authorization_transactions(
                        database.connection,
                        accepted_lookup_key_versions=(1,),
                        accepted_protection_key_versions=(11,),
                    )
                self.assertEqual(report.status, "clean")
                self.assertEqual(report.total_findings, 0)
                self.assertEqual(dict(report.inventory)["transaction_count"], 1)
                self.assertEqual(
                    report.accepted_lookup_key_versions,
                    (1,),
                )
                self.assertEqual(
                    report.accepted_protection_key_versions,
                    (11,),
                )
                self.assertFalse(database.connection.in_transaction)
                self.assertEqual(_database_fingerprint(database.path), before)
                prepared.close()
            finally:
                harness.close()
                authority.close()

    def test_unknown_rotation_versions_and_expired_prepared_rows_are_sanitized(self):
        with durable_transaction_database(suffix="reconciliation-findings") as database:
            clock = ManualClock(NOW)
            authority = key_authority(
                lookup_versions=(1, 2),
                protection_versions=(11, 12),
                active_lookup_version=2,
                active_protection_version=12,
            )
            harness = reconstructed_gateway(
                clock=clock,
                subject="google-subject-reconciliation-findings",
            )
            try:
                with sockets_blocked():
                    prepared = prepare_google_oidc_authorization_transaction(
                        database.connection,
                        harness.gateway,
                        authority,
                    )
                row = transaction_rows(database.connection)[0]
                state = prepared.authorization_url
                with mock.patch.object(
                    reconciliation,
                    "_clock_now",
                    return_value=NOW.replace() + reconciliation.timedelta(
                        seconds=600
                    ),
                ):
                    report = reconcile_google_oidc_authorization_transactions(
                        database.connection,
                        accepted_lookup_key_versions=(1,),
                        accepted_protection_key_versions=(11,),
                    )
                codes = dict(report.finding_counts_by_code)
                self.assertEqual(report.status, "findings")
                self.assertIn("unknown_lookup_key_version", codes)
                self.assertIn("unknown_protection_key_version", codes)
                self.assertIn("prepared_already_expired", codes)
                rendered = report.to_json()
                self.assertNotIn(row["transaction_id"], rendered)
                self.assertNotIn(row["state_lookup_digest"].hex(), rendered)
                self.assertNotIn(row["protected_material"].hex(), rendered)
                self.assertNotIn(state, rendered)
                self.assertTrue(
                    all(
                        finding.transaction_ordinal == 1
                        for finding in report.findings
                    )
                )
                prepared.close()
            finally:
                harness.close()
                authority.close()

    def test_prerequisite_dependency_blocker_is_bounded_and_sanitized(self):
        with durable_transaction_database(
            suffix="reconciliation-prerequisite-closure"
        ) as database:
            secret = "closure_secret_marker_2af167c1"
            database.connection.execute(
                "CREATE VIEW audit_semantic_dependency AS "
                f'SELECT count(*) AS "{secret}" '
                'FROM "UsEr_PiPeLiNe_StAtE" '
                f"/* {secret} */"
            )
            database.connection.commit()
            before = _database_fingerprint(database.path)
            report = reconcile_google_oidc_authorization_transactions(
                database.connection,
                accepted_lookup_key_versions=(1,),
                accepted_protection_key_versions=(11,),
            )
            rendered = report.to_json()
            human = report.to_human()
            self.assertEqual(report.status, "unavailable")
            self.assertEqual(
                report.unavailable_reason,
                "schema_capability_unavailable",
            )
            self.assertLessEqual(
                len(report.to_json_bytes()),
                reconciliation.MAX_REPORT_BYTES,
            )
            for value in (
                secret,
                "audit_semantic_dependency",
                "CREATE VIEW",
                "user_pipeline_state",
            ):
                self.assertNotIn(value, rendered)
                self.assertNotIn(value, human)
            self.assertFalse(database.connection.in_transaction)
            self.assertEqual(_database_fingerprint(database.path), before)

    def test_reserved_family_blocker_gates_reconciliation_and_is_sanitized(
        self,
    ):
        with durable_transaction_database(
            suffix="reconciliation-reserved-family"
        ) as database:
            object_name = (
                "TrG_UsEr_PiPeLiNe_StAtE_reconciliation_boundary"
            )
            database.connection.executescript(
                "CREATE TABLE audit_reconciliation_namespace_target("
                "value TEXT"
                "); "
                f'CREATE TRIGGER "{object_name}" '
                "AFTER INSERT ON audit_reconciliation_namespace_target "
                "BEGIN SELECT 1; END;"
            )
            database.connection.commit()
            before = _database_fingerprint(database.path)
            report = reconcile_google_oidc_authorization_transactions(
                database.connection,
                accepted_lookup_key_versions=(1,),
                accepted_protection_key_versions=(11,),
            )
            self.assertEqual(report.status, "unavailable")
            self.assertEqual(
                report.unavailable_reason,
                "schema_capability_unavailable",
            )
            self.assertNotIn(object_name, report.to_json())
            self.assertNotIn(object_name, report.to_human())
            self.assertFalse(database.connection.in_transaction)
            self.assertEqual(_database_fingerprint(database.path), before)

    def test_bounded_summary_and_schema_gate(self):
        with durable_transaction_database(suffix="reconciliation-bounds") as database:
            authority = key_authority()
            harness = reconstructed_gateway(
                subject="google-subject-reconciliation-bounds"
            )
            prepared_values = []
            try:
                with sockets_blocked():
                    for _index in range(3):
                        prepared_values.append(
                            prepare_google_oidc_authorization_transaction(
                                database.connection,
                                harness.gateway,
                                authority,
                            )
                        )
                with mock.patch.object(
                    reconciliation,
                    "_clock_now",
                    return_value=NOW + reconciliation.timedelta(seconds=600),
                ):
                    bounded = reconcile_google_oidc_authorization_transactions(
                        database.connection,
                        accepted_lookup_key_versions=(1,),
                        accepted_protection_key_versions=(11,),
                        max_findings=1,
                    )
                    summary = reconcile_google_oidc_authorization_transactions(
                        database.connection,
                        accepted_lookup_key_versions=(1,),
                        accepted_protection_key_versions=(11,),
                        summary_only=True,
                    )
                self.assertEqual(bounded.total_findings, 3)
                self.assertEqual(len(bounded.findings), 1)
                self.assertTrue(bounded.findings_truncated)
                self.assertEqual(summary.total_findings, 3)
                self.assertEqual(summary.findings, ())
                self.assertTrue(summary.findings_truncated)

                database.connection.execute(
                    "DROP TRIGGER "
                    "trg_google_oidc_authorization_transactions_delete_guard"
                )
                database.connection.commit()
                unavailable = reconcile_google_oidc_authorization_transactions(
                    database.connection,
                    accepted_lookup_key_versions=(1,),
                    accepted_protection_key_versions=(11,),
                )
                self.assertEqual(unavailable.status, "unavailable")
                self.assertEqual(
                    unavailable.unavailable_reason,
                    "schema_capability_unavailable",
                )
            finally:
                for prepared in prepared_values:
                    prepared.close()
                harness.close()
                authority.close()

    def test_invalid_inventory_and_limits_are_rejected(self):
        with durable_transaction_database(suffix="reconciliation-invalid") as database:
            cases = (
                {"accepted_lookup_key_versions": ()},
                {"accepted_lookup_key_versions": (1, 1)},
                {"accepted_protection_key_versions": (0,)},
                {"max_findings": -1},
                {"summary_only": 1},
            )
            for values in cases:
                with self.subTest(values=values), self.assertRaises(
                    GoogleOidcAuthorizationTransactionReconciliationError
                ) as failure:
                    reconcile_google_oidc_authorization_transactions(
                        database.connection,
                        **values,
                    )
                self.assertEqual(
                    failure.exception.reason_code,
                    "invalid_reconciliation_request",
                )

    def test_operation_row_bound_and_accounting_are_enforced(self):
        with durable_transaction_database(
            suffix="reconciliation-operation-bound"
        ) as database:
            with mock.patch.object(
                reconciliation,
                "_clock_now",
                return_value=NOW,
            ):
                empty = reconcile_google_oidc_authorization_transactions(
                    database.connection,
                    accepted_protection_key_versions=(11,),
                )
            self.assertEqual(
                (
                    empty.rows_observed,
                    empty.rows_inspected,
                    empty.rows_structurally_valid,
                    empty.rows_invalid,
                    empty.rows_omitted,
                    empty.rows_known_remaining,
                ),
                (0, 0, 0, 0, 0, 0),
            )

            _insert_reconciliation_rows(
                database.connection,
                (
                    _reconciliation_row(index)
                    for index in range(
                        1,
                        reconciliation.MAX_RECONCILIATION_ROWS + 1,
                    )
                ),
            )
            with mock.patch.object(
                reconciliation,
                "_clock_now",
                return_value=NOW,
            ):
                exact = reconcile_google_oidc_authorization_transactions(
                    database.connection,
                    accepted_protection_key_versions=(11,),
                )
            self.assertEqual(exact.status, "clean")
            self.assertTrue(exact.complete)
            self.assertFalse(exact.blocking)
            self.assertEqual(
                exact.rows_observed,
                reconciliation.MAX_RECONCILIATION_ROWS,
            )
            self.assertEqual(
                exact.rows_structurally_valid,
                reconciliation.MAX_RECONCILIATION_ROWS,
            )
            self.assertEqual(exact.rows_invalid, 0)
            self.assertTrue(exact.row_total_exact)

            _insert_reconciliation_rows(
                database.connection,
                (
                    _reconciliation_row(index)
                    for index in range(
                        reconciliation.MAX_RECONCILIATION_ROWS + 1,
                        (3 * reconciliation.MAX_RECONCILIATION_ROWS) + 1,
                    )
                ),
            )
            inspected = 0
            original = reconciliation._prepare_projected_row

            def tracked_prepare(row):
                nonlocal inspected
                inspected += 1
                return original(row)

            with mock.patch.object(
                reconciliation,
                "_clock_now",
                return_value=NOW,
            ), mock.patch.object(
                reconciliation,
                "_prepare_projected_row",
                side_effect=tracked_prepare,
            ):
                over = reconcile_google_oidc_authorization_transactions(
                    database.connection,
                    accepted_protection_key_versions=(11,),
                )
            self.assertEqual(over.status, "incomplete")
            self.assertFalse(over.complete)
            self.assertTrue(over.blocking)
            self.assertTrue(over.row_scan_truncated)
            self.assertFalse(over.row_total_exact)
            self.assertEqual(
                over.rows_observed,
                reconciliation.MAX_RECONCILIATION_ROWS + 1,
            )
            self.assertEqual(
                over.rows_inspected,
                0,
            )
            self.assertEqual(over.rows_structurally_valid, 0)
            self.assertEqual(over.rows_invalid, 0)
            self.assertEqual(inspected, 0)
            self.assertIsNone(over.rows_omitted)
            self.assertEqual(over.rows_known_remaining, 1)
            self.assertIsNone(over.total_findings)
            self.assertFalse(over.finding_total_exact)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = cli.main(
                    [
                        "--db",
                        str(database.path),
                        "--json",
                        "--protection-key-version",
                        "11",
                    ],
                    _workspace_path=database.path.parent / "other.sqlite",
                )
            self.assertEqual(exit_code, 2)
            self.assertEqual(
                json.loads(stdout.getvalue())["status"],
                "incomplete",
            )

    def test_invalid_row_accounting_is_independent_of_row_position(self):
        outputs = []
        for suffix, row_count, malformed_index in (
            ("first", 20, 1),
            ("last", 20, 20),
            (
                "many-before",
                reconciliation.MAX_RECONCILIATION_ROWS,
                reconciliation.MAX_RECONCILIATION_ROWS,
            ),
        ):
            with durable_transaction_database(
                suffix=f"reconciliation-invalid-{suffix}"
            ) as database:
                database.connection.execute(
                    "PRAGMA ignore_check_constraints = ON"
                )
                rows = [
                    _reconciliation_row(
                        index,
                        provider=(
                            "invalid-provider"
                            if index == malformed_index
                            else "google"
                        ),
                    )
                    for index in range(1, row_count + 1)
                ]
                _insert_reconciliation_rows(database.connection, rows)
                with mock.patch.object(
                    reconciliation,
                    "_clock_now",
                    return_value=NOW,
                ):
                    report = (
                        reconcile_google_oidc_authorization_transactions(
                            database.connection,
                            accepted_protection_key_versions=(11,),
                        )
                    )
                self.assertEqual(report.rows_observed, row_count)
                self.assertEqual(
                    report.rows_structurally_valid,
                    row_count - 1,
                )
                self.assertEqual(report.rows_invalid, 1)
                self.assertEqual(
                    dict(report.finding_counts_by_code)[
                        "unexpected_provider"
                    ],
                    1,
                )
                outputs.append(
                    (
                        report.finding_counts_by_code,
                        report.complete,
                        report.blocking,
                    )
                )
        self.assertEqual(len(set(outputs)), 1)

    def test_aggregate_schema_and_snapshot_budgets_fail_closed(self):
        with durable_transaction_database(
            suffix="reconciliation-aggregate-budget"
        ) as database:
            contract = reconciliation.GOOGLE_OIDC_RECONCILIATION_BUDGET
            for resource, replacement in (
                ("max_result_rows", 1),
                ("max_snapshot_pages", 0),
            ):
                with self.subTest(resource=resource), mock.patch.object(
                    reconciliation,
                    "GOOGLE_OIDC_RECONCILIATION_BUDGET",
                    reconciliation.replace(
                        contract,
                        **{resource: replacement},
                    ),
                ):
                    report = (
                        reconcile_google_oidc_authorization_transactions(
                            database.connection,
                            accepted_protection_key_versions=(11,),
                        )
                    )
                self.assertEqual(report.status, "incomplete")
                self.assertFalse(report.complete)
                self.assertTrue(report.blocking)
                self.assertEqual(
                    report.incomplete_reason,
                    "operation_budget_exceeded",
                )

    def test_budget_exhaustion_preserves_known_partial_accounting(self):
        with durable_transaction_database(
            suffix="reconciliation-partial-accounting"
        ) as database:
            def exhaust_after_partial_scan(
                _connection,
                budget,
                _lookup_versions,
                _protection_versions,
                _displayed_limit,
            ):
                budget.start_transaction_scan()
                for _index in range(3):
                    budget.observe_transaction_row()
                budget.finish_transaction_scan(has_more=False)
                budget.observe_inspected_row(
                    valid=True,
                    lifecycle="prepared",
                )
                budget.observe_inspected_row(
                    valid=False,
                    lifecycle=None,
                )
                budget.consume_result()
                budget.observe_finding("unknown_lookup_key_version")
                budget.exhausted = True
                raise reconciliation._OperationBudgetExceeded()

            with mock.patch.object(
                reconciliation,
                "_scan_snapshot",
                side_effect=exhaust_after_partial_scan,
            ):
                report = reconcile_google_oidc_authorization_transactions(
                    database.connection,
                    accepted_protection_key_versions=(11,),
                )

            self.assertEqual(report.status, "incomplete")
            self.assertFalse(report.complete)
            self.assertTrue(report.blocking)
            self.assertEqual(report.rows_observed, 3)
            self.assertEqual(report.rows_inspected, 2)
            self.assertEqual(report.rows_structurally_valid, 1)
            self.assertEqual(report.rows_invalid, 1)
            self.assertEqual(report.rows_omitted, 1)
            self.assertTrue(report.row_total_exact)
            self.assertTrue(report.row_scan_truncated)
            self.assertEqual(report.findings_observed, 1)
            self.assertEqual(report.findings_retained, 0)
            self.assertEqual(report.findings_known_omitted, 1)
            self.assertIsNone(report.findings_omitted)
            self.assertFalse(report.finding_total_exact)
            self.assertTrue(report.finding_retention_truncated)
            self.assertEqual(
                dict(report.finding_counts_by_code),
                {"unknown_lookup_key_version": 1},
            )

        base_contract = reconciliation.GOOGLE_OIDC_RECONCILIATION_BUDGET
        with mock.patch.object(
            reconciliation,
            "GOOGLE_OIDC_RECONCILIATION_BUDGET",
            reconciliation.replace(
                base_contract,
                max_result_rows=0,
            ),
        ):
            row_budget = reconciliation._OperationBudget()
            row_budget.start_transaction_scan()
            owned = sqlite3.connect(":memory:")
            cursor = reconciliation._BudgetedCursor(
                owned.execute("SELECT 1"),
                row_budget,
            )
            try:
                with self.assertRaises(
                    reconciliation._OperationBudgetExceeded
                ):
                    reconciliation._bounded_cursor_rows(
                        cursor,
                        reconciliation.MAX_RECONCILIATION_ROWS,
                        observe=row_budget.observe_transaction_row,
                    )
            finally:
                cursor.close()
                owned.close()
            row_report = reconciliation._incomplete_report(
                "operation_budget_exceeded",
                (1,),
                (1,),
                budget=row_budget,
            )
            self.assertEqual(row_report.rows_observed, 1)
            self.assertTrue(row_report.row_scan_truncated)
            self.assertFalse(row_report.row_total_exact)

            finding_budget = reconciliation._OperationBudget()
            collector = reconciliation._Collector(finding_budget)
            with self.assertRaises(
                reconciliation._OperationBudgetExceeded
            ):
                collector.add("unknown_lookup_key_version")
            finding_report = reconciliation._incomplete_report(
                "operation_budget_exceeded",
                (1,),
                (1,),
                budget=finding_budget,
            )
            self.assertEqual(finding_report.findings_observed, 1)
            self.assertEqual(finding_report.findings_known_omitted, 1)
            self.assertEqual(
                dict(finding_report.finding_counts_by_code),
                {"unknown_lookup_key_version": 1},
            )

    def test_schema_and_duplicate_group_budget_edges_are_aggregate(self):
        with durable_transaction_database(
            suffix="reconciliation-schema-budget-edges"
        ) as database:
            base_contract = reconciliation.GOOGLE_OIDC_RECONCILIATION_BUDGET

            def attest_with_result_limit(limit):
                contract = reconciliation.replace(
                    base_contract,
                    max_result_rows=limit,
                )
                with mock.patch.object(
                    reconciliation,
                    "GOOGLE_OIDC_RECONCILIATION_BUDGET",
                    contract,
                ):
                    budget = reconciliation._OperationBudget()
                    owned = reconciliation._new_private_connection()
                    attestation = None
                    try:
                        database.connection.backup(owned)
                        reconciliation._configure_owned_connection(
                            owned,
                            budget,
                        )
                        try:
                            attestation = (
                                reconciliation
                                .attest_google_oidc_authorization_transaction_schema(
                                    reconciliation._BudgetedConnection(
                                        owned,
                                        budget,
                                    ),
                                    _operation_budget=budget,
                                )
                            )
                        except reconciliation._OperationBudgetExceeded:
                            pass
                    finally:
                        reconciliation._close_owned_connection(owned)
                return budget, attestation

            baseline_budget, baseline = attest_with_result_limit(
                base_contract.max_result_rows
            )
            self.assertEqual(
                baseline["state"],
                "correctly_installed",
            )
            schema_result_count = baseline_budget.result_rows
            self.assertGreater(schema_result_count, 1)

            exact_budget, exact = attest_with_result_limit(
                schema_result_count
            )
            self.assertFalse(exact_budget.exhausted)
            self.assertEqual(exact["state"], "correctly_installed")
            self.assertEqual(
                exact_budget.result_rows,
                schema_result_count,
            )

            over_budget, _over = attest_with_result_limit(
                schema_result_count - 1
            )
            self.assertTrue(over_budget.exhausted)

            database.connection.execute(
                "CREATE VIEW reconciliation_budget_dependency AS "
                'SELECT * FROM "UsEr_PiPeLiNe_StAtE"'
            )
            database.connection.commit()
            nested_budget, nested_attestation = attest_with_result_limit(
                base_contract.max_result_rows
            )
            self.assertNotEqual(
                nested_attestation["state"],
                "correctly_installed",
            )
            self.assertGreater(
                nested_budget.authorizer_calls,
                baseline_budget.authorizer_calls,
            )
            self.assertGreater(
                nested_budget.result_rows,
                baseline_budget.result_rows,
            )
            nested_exact_budget, _nested_exact = attest_with_result_limit(
                nested_budget.result_rows
            )
            self.assertFalse(nested_exact_budget.exhausted)
            nested_over_budget, _nested_over = attest_with_result_limit(
                nested_budget.result_rows - 1
            )
            self.assertTrue(nested_over_budget.exhausted)
            with mock.patch.object(
                transaction_schema,
                "_MAX_PREREQUISITE_AUTHORIZER_CALLS",
                0,
            ):
                local_authorizer_over = (
                    reconcile_google_oidc_authorization_transactions(
                        database.connection,
                        accepted_protection_key_versions=(11,),
                    )
                )
            self.assertEqual(local_authorizer_over.status, "incomplete")
            self.assertEqual(
                local_authorizer_over.incomplete_reason,
                "operation_budget_exceeded",
            )
            self.assertTrue(local_authorizer_over.blocking)

            with mock.patch.object(
                reconciliation,
                "GOOGLE_OIDC_RECONCILIATION_BUDGET",
                reconciliation.replace(
                    base_contract,
                    max_result_rows=2,
                ),
            ):
                group_budget = reconciliation._OperationBudget()
                groups = {}
                reconciliation._observe_group(
                    groups,
                    b"group-1",
                    b"metadata-1",
                    b"row-1",
                    group_budget,
                )
                reconciliation._observe_group(
                    groups,
                    b"group-2",
                    b"metadata-2",
                    b"row-2",
                    group_budget,
                )
                self.assertEqual(group_budget.result_rows, 2)
                with self.assertRaises(
                    reconciliation._OperationBudgetExceeded
                ):
                    reconciliation._observe_group(
                        groups,
                        b"group-3",
                        b"metadata-3",
                        b"row-3",
                        group_budget,
                    )
                self.assertTrue(group_budget.exhausted)

        with durable_transaction_database(
            suffix="reconciliation-public-group-budget"
        ) as database:
            for over_limit in (False, True):
                def group_edge(_rows, _collector, budget):
                    budget.result_rows = (
                        budget.contract.max_result_rows - 2
                    )
                    groups = {}
                    for index in range(2 + int(over_limit)):
                        reconciliation._observe_group(
                            groups,
                            f"group-{index}".encode("ascii"),
                            f"metadata-{index}".encode("ascii"),
                            f"row-{index}".encode("ascii"),
                            budget,
                        )

                with self.subTest(over_limit=over_limit), mock.patch.object(
                    reconciliation,
                    "_scan_reuse_groups",
                    side_effect=group_edge,
                ):
                    report = (
                        reconcile_google_oidc_authorization_transactions(
                            database.connection,
                            accepted_protection_key_versions=(11,),
                        )
                    )
                if over_limit:
                    self.assertEqual(report.status, "incomplete")
                    self.assertFalse(report.complete)
                    self.assertTrue(report.blocking)
                    self.assertEqual(
                        report.incomplete_reason,
                        "operation_budget_exceeded",
                    )
                else:
                    self.assertEqual(report.status, "clean")
                    self.assertTrue(report.complete)

    def test_finding_retention_exact_and_plus_one_fail_closed(self):
        with durable_transaction_database(
            suffix="reconciliation-finding-budget"
        ) as database:
            rows = [
                _reconciliation_row(index)
                for index in range(
                    1,
                    reconciliation.MAX_RECONCILIATION_ROWS + 1,
                )
            ]
            _insert_reconciliation_rows(database.connection, rows)
            expired_now = NOW + reconciliation.timedelta(seconds=600)
            with mock.patch.object(
                reconciliation,
                "_clock_now",
                return_value=expired_now,
            ):
                exact = reconcile_google_oidc_authorization_transactions(
                    database.connection,
                    accepted_protection_key_versions=(11,),
                    max_findings=reconciliation.MAX_FINDINGS,
                )
            self.assertEqual(
                exact.total_findings,
                reconciliation.MAX_FINDINGS,
            )
            self.assertEqual(
                exact.findings_retained,
                reconciliation.MAX_FINDINGS,
            )
            self.assertFalse(exact.finding_retention_truncated)
            self.assertTrue(exact.complete)

        with durable_transaction_database(
            suffix="reconciliation-finding-budget-over"
        ) as database:
            rows = [
                _reconciliation_row(
                    index,
                    protection_key_version=12 if index == 1 else 11,
                )
                for index in range(
                    1,
                    reconciliation.MAX_RECONCILIATION_ROWS + 1,
                )
            ]
            _insert_reconciliation_rows(database.connection, rows)
            with mock.patch.object(
                reconciliation,
                "_clock_now",
                return_value=expired_now,
            ):
                over = reconcile_google_oidc_authorization_transactions(
                    database.connection,
                    accepted_protection_key_versions=(11,),
                    max_findings=reconciliation.MAX_FINDINGS,
                )
            self.assertEqual(
                over.total_findings,
                reconciliation.MAX_FINDINGS + 1,
            )
            self.assertEqual(
                over.findings_retained,
                reconciliation.MAX_FINDINGS,
            )
            self.assertEqual(over.findings_omitted, 1)
            self.assertTrue(over.finding_retention_truncated)
            self.assertFalse(over.complete)
            self.assertTrue(over.blocking)
            self.assertEqual(over.status, "incomplete")

    def test_shared_output_budget_includes_newline_and_has_valid_fallback(self):
        with durable_transaction_database(
            suffix="reconciliation-output-budget"
        ) as database:
            _insert_reconciliation_rows(
                database.connection,
                (
                    _reconciliation_row(
                        index,
                        protection_key_version=12,
                    )
                    for index in range(1, 3)
                ),
            )
            with mock.patch.object(
                reconciliation,
                "_clock_now",
                return_value=NOW,
            ):
                human_report = (
                    reconcile_google_oidc_authorization_transactions(
                        database.connection,
                        accepted_protection_key_versions=(11,),
                    )
                )
            self.assertGreater(
                len(reconciliation._render_human_bytes(human_report)),
                len(reconciliation._render_json_bytes(human_report)),
            )
            human_limit = len(
                reconciliation._render_human_bytes(human_report)
            )
            human_exact = reconciliation._apply_output_budget(
                human_report,
                human_limit,
            )
            self.assertFalse(human_exact.output_rendering_truncated)
            human_over = reconciliation._apply_output_budget(
                human_report,
                human_limit - 1,
            )
            self.assertTrue(human_over.output_rendering_truncated)
            self.assertLessEqual(
                len(reconciliation._render_human_bytes(human_over)),
                human_limit - 1,
            )

            _insert_reconciliation_rows(
                database.connection,
                (
                    _reconciliation_row(
                        index,
                        protection_key_version=12,
                    )
                    for index in range(3, 21)
                ),
            )
            with mock.patch.object(
                reconciliation,
                "_clock_now",
                return_value=NOW,
            ):
                report = reconcile_google_oidc_authorization_transactions(
                    database.connection,
                    accepted_protection_key_versions=(11,),
                )
            json_bytes = reconciliation._render_json_bytes(report)
            human_bytes = reconciliation._render_human_bytes(report)
            self.assertGreater(len(json_bytes), len(human_bytes))
            exact_limit = len(json_bytes)
            exact = reconciliation._apply_output_budget(
                report,
                exact_limit,
            )
            self.assertFalse(exact.output_rendering_truncated)
            over = reconciliation._apply_output_budget(
                report,
                exact_limit - 1,
            )
            self.assertEqual(over.status, "incomplete")
            self.assertFalse(over.complete)
            self.assertTrue(over.blocking)
            self.assertTrue(over.output_rendering_truncated)
            self.assertEqual(over.findings_retained, 0)
            self.assertEqual(
                over.findings_known_omitted,
                report.findings_observed,
            )
            self.assertEqual(over.to_json_bytes()[-1:], b"\n")
            self.assertTrue(over.to_human().endswith("\n"))
            self.assertNotEqual(over.to_human()[-2:], "\n\n")
            parsed = json.loads(over.to_json())
            self.assertTrue(parsed["output_rendering_truncated"])
            self.assertTrue(parsed["blocking"])
            self.assertLessEqual(
                len(reconciliation._render_json_bytes(over)),
                exact_limit - 1,
            )

    def test_integrity_limit_uses_a_real_plus_one_sentinel(self):
        class IntegrityCursor:
            def __init__(self, count, budget):
                self.remaining = count
                self.budget = budget

            def fetchone(self):
                if self.remaining == 0:
                    return None
                self.remaining -= 1
                self.budget.consume_result()
                return (0,)

            def close(self):
                return None

        with durable_transaction_database(
            suffix="reconciliation-integrity-sentinel"
        ) as database:
            for count, expected_status in (
                (100, "findings"),
                (101, "incomplete"),
            ):
                with self.subTest(count=count):
                    owned = reconciliation._new_private_connection()
                    budget = reconciliation._OperationBudget()
                    try:
                        database.connection.backup(owned)
                        reconciliation._configure_owned_connection(
                            owned,
                            budget,
                        )
                        bounded = reconciliation._BudgetedConnection(
                            owned,
                            budget,
                        )

                        class IntegrityConnection:
                            def execute(self, sql, parameters=()):
                                if "pragma_integrity_check" in sql:
                                    self_sql.append(sql)
                                    return IntegrityCursor(count, budget)
                                return bounded.execute(sql, parameters)

                        self_sql = []
                        report = reconciliation._scan_snapshot(
                            IntegrityConnection(),
                            budget,
                            (1,),
                            (11,),
                            reconciliation.DEFAULT_MAX_FINDINGS,
                        )
                    finally:
                        reconciliation._close_owned_connection(owned)
                    self.assertEqual(len(self_sql), 1)
                    self.assertIn("pragma_integrity_check(101)", self_sql[0])
                    self.assertEqual(report.status, expected_status)
                    self.assertEqual(
                        report.finding_total_exact,
                        count == 100,
                    )
                    if count == 101:
                        self.assertFalse(report.complete)
                        self.assertTrue(report.blocking)
                        self.assertEqual(
                            report.incomplete_reason,
                            "integrity_check_limit_exceeded",
                        )

    def test_findings_are_insertion_rowid_and_hash_seed_independent(self):
        rendered = []
        logical_rows = (
            _reconciliation_row(
                1,
                environment_namespace="test",
                protected_material=b"copied-protected-material-value",
            ),
            _reconciliation_row(
                2,
                environment_namespace="development",
                protected_material=b"copied-protected-material-value",
            ),
        )
        for suffix, rows in (
            ("forward", logical_rows),
            ("reverse", tuple(reversed(logical_rows))),
        ):
            with durable_transaction_database(
                suffix=f"reconciliation-order-{suffix}"
            ) as database:
                _insert_reconciliation_rows(database.connection, rows)
                database.connection.execute(
                    "PRAGMA reverse_unordered_selects = "
                    + ("OFF" if suffix == "forward" else "ON")
                )
                with mock.patch.object(
                    reconciliation,
                    "_clock_now",
                    return_value=NOW,
                ):
                    report = (
                        reconcile_google_oidc_authorization_transactions(
                            database.connection,
                            accepted_protection_key_versions=(11,),
                        )
                    )
                alternate_sql = (
                    reconciliation._transaction_scan_sql().replace(
                        (
                            " LIMIT "
                            f"{reconciliation.MAX_RECONCILIATION_ROWS + 1}"
                        ),
                        (
                            " ORDER BY transaction_id DESC LIMIT "
                            f"{reconciliation.MAX_RECONCILIATION_ROWS + 1}"
                        ),
                    )
                )
                with mock.patch.object(
                    reconciliation,
                    "_clock_now",
                    return_value=NOW,
                ), mock.patch.object(
                    reconciliation,
                    "_transaction_scan_sql",
                    return_value=alternate_sql,
                ):
                    alternate = (
                        reconcile_google_oidc_authorization_transactions(
                            database.connection,
                            accepted_protection_key_versions=(11,),
                        )
                    )
                self.assertEqual(
                    (report.to_json_bytes(), report.to_human()),
                    (
                        alternate.to_json_bytes(),
                        alternate.to_human(),
                    ),
                )
                rendered.append(
                    (report.to_json_bytes(), report.to_human())
                )
        self.assertEqual(rendered[0], rendered[1])

        script = textwrap.dedent(
            """
            from pathlib import Path
            import tempfile
            from tests.persistent_profile_canonical_v2_test_support import install_canonical_v2_profiles
            import scripts.google_oidc_authorization_transactions_migration as migration
            import wahojobs.google_oidc_authorization_transaction_reconciliation as reconciliation

            sql = "INSERT INTO google_oidc_authorization_transactions VALUES (" + ",".join(["?"] * 18) + ")"
            def row(index, environment):
                return (
                    f"oidctx_{index:032x}", 1, "google", environment,
                    index.to_bytes(32, "big"), 1, 1,
                    (index + 10000).to_bytes(32, "big"),
                    "2026-07-24T03:00:00+00:00",
                    "2026-07-24T03:10:00+00:00",
                    "prepared", None, None, 1, 1, 11,
                    index.to_bytes(12, "big"),
                    b"copied-protected-material-value",
                )
            with tempfile.TemporaryDirectory() as directory:
                connection = install_canonical_v2_profiles(Path(directory) / "state.sqlite")
                migration.apply_google_oidc_authorization_transactions_migration(connection)
                connection.executemany(sql, (row(2, "development"), row(1, "test")))
                connection.commit()
                reconciliation._clock_now = lambda: reconciliation.datetime(2026, 7, 24, 3, 0, tzinfo=reconciliation.timezone.utc)
                report = reconciliation.reconcile_google_oidc_authorization_transactions(
                    connection,
                    accepted_protection_key_versions=(11,),
                    source_guarantees_no_sidecar_creation=True,
                )
                print(report.to_json(), end="")
                print("---HUMAN---")
                print(report.to_human(), end="")
                connection.close()
            """
        )
        seeded = []
        for seed in ("1", "7", "31", "101"):
            environment = dict(os.environ)
            environment["PYTHONHASHSEED"] = seed
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [sys.executable, "-B", "-c", script],
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            seeded.append(result.stdout)
        self.assertEqual(len(set(seeded)), 1)

    def test_caller_callbacks_are_never_invoked_and_state_is_preserved(self):
        with durable_transaction_database(
            suffix="reconciliation-callback-boundary"
        ) as database:
            connection = database.connection
            calls = {
                "trace": 0,
                "progress": 0,
                "authorizer": 0,
                "function": 0,
                "collation": 0,
            }
            attack = True
            reentrant = False

            def attempt_mutation(kind):
                nonlocal reentrant
                calls[kind] += 1
                if attack and not reentrant:
                    reentrant = True
                    try:
                        connection.execute(
                            "DELETE FROM google_oidc_authorization_transactions"
                        )
                        connection.commit()
                    except BaseException:
                        pass
                    finally:
                        reentrant = False

            def trace(_statement):
                attempt_mutation("trace")

            def progress():
                attempt_mutation("progress")
                return 0

            def authorize(*_arguments):
                attempt_mutation("authorizer")
                return sqlite3.SQLITE_OK

            def hostile_row_factory(_cursor, _row):
                raise AssertionError("caller_row_factory_executed")

            def hostile_text_factory(_value):
                raise AssertionError("caller_text_factory_executed")

            def application_function():
                calls["function"] += 1
                return 1

            def application_collation(left, right):
                calls["collation"] += 1
                return (left > right) - (left < right)

            connection.create_function(
                "caller_application_function",
                0,
                application_function,
            )
            connection.create_collation(
                "CALLER_APPLICATION_COLLATION",
                application_collation,
            )
            connection.set_trace_callback(trace)
            connection.set_progress_handler(progress, 1)
            connection.set_authorizer(authorize)
            connection.row_factory = hostile_row_factory
            connection.text_factory = hostile_text_factory
            before = _database_fingerprint(database.path)

            report = reconcile_google_oidc_authorization_transactions(
                connection,
                accepted_protection_key_versions=(11,),
            )
            self.assertEqual(report.status, "clean")
            self.assertEqual(
                calls,
                {
                    "trace": 0,
                    "progress": 0,
                    "authorizer": 0,
                    "function": 0,
                    "collation": 0,
                },
            )
            self.assertIs(connection.row_factory, hostile_row_factory)
            self.assertIs(connection.text_factory, hostile_text_factory)
            self.assertFalse(connection.in_transaction)
            self.assertEqual(_database_fingerprint(database.path), before)

            attack = False
            connection.row_factory = None
            connection.text_factory = str
            connection.execute(
                "SELECT caller_application_function(), "
                "'a' = 'A' COLLATE CALLER_APPLICATION_COLLATION"
            ).fetchone()
            self.assertGreater(calls["trace"], 0)
            self.assertGreater(calls["progress"], 0)
            self.assertGreater(calls["authorizer"], 0)
            self.assertEqual(calls["function"], 1)
            self.assertGreater(calls["collation"], 0)
            connection.set_trace_callback(None)
            connection.set_progress_handler(None, 0)
            connection.set_authorizer(None)

    def test_active_transaction_and_control_flow_fail_closed_without_sql(self):
        with durable_transaction_database(
            suffix="reconciliation-active-transaction"
        ) as database:
            calls = 0

            def trace(_statement):
                nonlocal calls
                calls += 1

            database.connection.execute("BEGIN")
            database.connection.set_trace_callback(trace)
            report = reconcile_google_oidc_authorization_transactions(
                database.connection,
                accepted_protection_key_versions=(11,),
            )
            self.assertEqual(report.status, "unavailable")
            self.assertTrue(report.blocking)
            self.assertEqual(
                report.unavailable_reason,
                "inspection_boundary_unavailable",
            )
            self.assertTrue(database.connection.in_transaction)
            self.assertEqual(calls, 0)
            database.connection.set_trace_callback(None)
            database.connection.rollback()

            calls = 0
            database.connection.set_trace_callback(trace)
            for interruption in (
                KeyboardInterrupt(),
                SystemExit(),
                GeneratorExit(),
            ):
                with self.subTest(
                    interruption=type(interruption).__name__
                ), mock.patch.object(
                    reconciliation,
                    "_bounded_backup",
                    side_effect=interruption,
                ):
                    interrupted = (
                        reconcile_google_oidc_authorization_transactions(
                            database.connection,
                            accepted_protection_key_versions=(11,),
                        )
                    )
                self.assertEqual(interrupted.status, "unavailable")
                self.assertTrue(interrupted.blocking)
                self.assertFalse(database.connection.in_transaction)
                self.assertEqual(calls, 0)
            database.connection.set_trace_callback(None)

    def test_global_adapter_and_converter_registries_are_not_used_or_changed(self):
        adapters_before = dict(sqlite3.adapters)
        converters_before = dict(sqlite3.converters)
        calls = {"adapter": 0, "converter": 0}

        def hostile_text_adapter(_value):
            calls["adapter"] += 1
            raise AssertionError("caller_adapter_executed")

        def hostile_converter(_value):
            calls["converter"] += 1
            raise AssertionError("caller_converter_executed")

        with durable_transaction_database(
            suffix="reconciliation-adapter-converter"
        ) as database:
            try:
                sqlite3.register_adapter(str, hostile_text_adapter)
                for private_type in reconciliation._new_private_sql_types():
                    sqlite3.register_adapter(
                        private_type,
                        hostile_text_adapter,
                    )
                sqlite3.register_converter(
                    "RECONCILIATION_PROBE",
                    hostile_converter,
                )
                installed_adapters = dict(sqlite3.adapters)
                installed_converters = dict(sqlite3.converters)
                report = reconcile_google_oidc_authorization_transactions(
                    database.connection,
                    accepted_protection_key_versions=(11,),
                )
                self.assertEqual(report.status, "clean")
                self.assertEqual(calls, {"adapter": 0, "converter": 0})
                self.assertEqual(sqlite3.adapters, installed_adapters)
                self.assertEqual(
                    sqlite3.converters,
                    installed_converters,
                )
            finally:
                sqlite3.adapters.clear()
                sqlite3.adapters.update(adapters_before)
                sqlite3.converters.clear()
                sqlite3.converters.update(converters_before)

    def test_path_memory_read_only_and_temp_boundaries(self):
        with durable_transaction_database(
            suffix="reconciliation-owned-snapshot"
        ) as database:
            memory = sqlite3.connect(":memory:")
            database.connection.backup(memory)
            report = reconcile_google_oidc_authorization_transactions(
                memory,
                accepted_protection_key_versions=(11,),
            )
            self.assertEqual(report.status, "clean")
            memory.close()

            uri = (
                database.path.resolve().as_uri()
                + "?mode=ro&immutable=1"
            )
            read_only = sqlite3.connect(uri, uri=True)
            report = reconcile_google_oidc_authorization_transactions(
                read_only,
                accepted_protection_key_versions=(11,),
            )
            self.assertEqual(report.status, "clean")
            read_only.close()

            database.connection.execute(
                "CREATE TEMP TABLE reconciliation_temp_probe(value)"
            )
            report = reconcile_google_oidc_authorization_transactions(
                database.connection,
                accepted_protection_key_versions=(11,),
            )
            self.assertEqual(report.status, "unavailable")
            self.assertEqual(
                report.unavailable_reason,
                "schema_capability_unavailable",
            )
            self.assertIsNotNone(
                database.connection.execute(
                    "SELECT 1 FROM temp.reconciliation_temp_probe"
                )
            )

    def test_reuse_metadata_signature_covers_each_required_field(self):
        values, storage, lengths = _reconciliation_metadata(
            _reconciliation_row(1)
        )
        material_signature = reconciliation._reuse_metadata(
            values,
            storage,
            lengths,
            excluded=frozenset({"protected_material"}),
        )
        changes = {
            "transaction_id": "oidctx_" + ("f" * 32),
            "record_version": 2,
            "provider": "other",
            "environment_namespace": "development",
            "configuration_fingerprint": b"z" * 32,
            "state_digest_version": 2,
            "lookup_key_version": 2,
            "state_lookup_digest": b"s" * 32,
            "created_at": (
                NOW + reconciliation.timedelta(seconds=60)
            ).isoformat(),
            "expires_at": (
                NOW + reconciliation.timedelta(seconds=660)
            ).isoformat(),
            "lifecycle": "invalidated",
            "claimed_at": NOW.isoformat(),
            "terminal_at": NOW.isoformat(),
            "row_version": 2,
            "protection_envelope_version": 2,
            "protection_key_version": 12,
            "protection_nonce": b"n" * 12,
        }
        for field, changed in changes.items():
            with self.subTest(field=field):
                changed_values = dict(values)
                changed_storage = dict(storage)
                changed_lengths = dict(lengths)
                changed_values[field] = changed
                changed_storage[field] = _sqlite_storage(changed)
                changed_lengths[field] = _sqlite_length(changed)
                self.assertNotEqual(
                    reconciliation._reuse_metadata(
                        changed_values,
                        changed_storage,
                        changed_lengths,
                        excluded=frozenset({"protected_material"}),
                    ),
                    material_signature,
                )

        for field, changed in (
            ("storage", "text"),
            ("length", lengths["protected_material"] + 1),
        ):
            with self.subTest(excluded_material_metadata=field):
                changed_storage = dict(storage)
                changed_lengths = dict(lengths)
                if field == "storage":
                    changed_storage["protected_material"] = changed
                else:
                    changed_lengths["protected_material"] = changed
                self.assertNotEqual(
                    reconciliation._reuse_metadata(
                        values,
                        changed_storage,
                        changed_lengths,
                        excluded=frozenset({"protected_material"}),
                    ),
                    material_signature,
                )

        nonce_signature = reconciliation._reuse_metadata(
            values,
            storage,
            lengths,
            excluded=frozenset({"protection_nonce"}),
        )
        changed_values = dict(values)
        changed_lengths = dict(lengths)
        changed_values["protected_material"] = b"m" * 17
        changed_lengths["protected_material"] = 17
        self.assertNotEqual(
            reconciliation._reuse_metadata(
                changed_values,
                storage,
                changed_lengths,
                excluded=frozenset({"protection_nonce"}),
            ),
            nonce_signature,
        )
        for field, changed in (
            ("storage", "text"),
            ("length", lengths["protection_nonce"] + 1),
        ):
            with self.subTest(excluded_nonce_metadata=field):
                changed_storage = dict(storage)
                changed_lengths = dict(lengths)
                if field == "storage":
                    changed_storage["protection_nonce"] = changed
                else:
                    changed_lengths["protection_nonce"] = changed
                self.assertNotEqual(
                    reconciliation._reuse_metadata(
                        values,
                        changed_storage,
                        changed_lengths,
                        excluded=frozenset({"protection_nonce"}),
                    ),
                    nonce_signature,
                )

    def test_semantic_reuse_matrix_is_blocking_sanitized_and_scope_limited(self):
        scenarios = (
            (
                "identity-and-unique-index-fields",
                {},
            ),
            (
                "environment",
                {"environment_namespace": "development"},
            ),
            (
                "configuration",
                {"configuration_fingerprint": b"z" * 32},
            ),
            (
                "lookup-version",
                {"lookup_key_version": 2},
            ),
            (
                "protection-version",
                {"protection_key_version": 12},
            ),
            (
                "chronology",
                {
                    "created_at": (
                        NOW + reconciliation.timedelta(seconds=60)
                    ).isoformat(),
                    "expires_at": (
                        NOW + reconciliation.timedelta(seconds=660)
                    ).isoformat(),
                },
            ),
            (
                "lifecycle",
                {},
            ),
        )
        for suffix, second_changes in scenarios:
            with self.subTest(suffix=suffix), durable_transaction_database(
                suffix=f"reconciliation-reuse-{suffix}"
            ) as database:
                material = b"exact-copied-protected-material"
                first = _reconciliation_row(
                    1,
                    protected_material=material,
                )
                baseline_second = _reconciliation_row(
                    2,
                    configuration_fingerprint=first[4],
                    protected_material=material,
                    created_at=first[8],
                    expires_at=first[9],
                )
                second_options = {
                    "configuration_fingerprint": first[4],
                    "protected_material": material,
                    "created_at": first[8],
                    "expires_at": first[9],
                }
                second_options.update(second_changes)
                second = _reconciliation_row(2, **second_options)
                semantic_second = second
                if suffix == "lifecycle":
                    semantic_second_values = list(second)
                    semantic_second_values[10] = "invalidated"
                    semantic_second_values[12] = NOW.isoformat()
                    semantic_second_values[13] = 2
                    semantic_second = tuple(semantic_second_values)
                baseline_values = _reconciliation_metadata(
                    baseline_second
                )
                semantic_values = _reconciliation_metadata(
                    semantic_second
                )
                baseline_signature = reconciliation._reuse_metadata(
                    *baseline_values,
                    excluded=frozenset({"protected_material"}),
                )
                semantic_signature = reconciliation._reuse_metadata(
                    *semantic_values,
                    excluded=frozenset({"protected_material"}),
                )
                if suffix == "identity-and-unique-index-fields":
                    self.assertEqual(
                        semantic_signature,
                        baseline_signature,
                    )
                else:
                    self.assertNotEqual(
                        semantic_signature,
                        baseline_signature,
                    )
                _insert_reconciliation_rows(
                    database.connection,
                    (first, second),
                )
                if suffix == "lifecycle":
                    database.connection.execute(
                        "UPDATE google_oidc_authorization_transactions "
                        "SET lifecycle = 'invalidated', terminal_at = ?, "
                        "row_version = 2 WHERE transaction_id = ?",
                        (NOW.isoformat(), second[0]),
                    )
                    database.connection.commit()
                with mock.patch.object(
                    reconciliation,
                    "_clock_now",
                    return_value=NOW,
                ):
                    report = (
                        reconcile_google_oidc_authorization_transactions(
                            database.connection,
                            accepted_lookup_key_versions=(1, 2),
                            accepted_protection_key_versions=(11, 12),
                        )
                    )
                codes = set(dict(report.finding_counts_by_code))
                reuse_codes = codes & {
                    "duplicate_protection_nonce",
                    "protected_material_reuse",
                    "protected_material_metadata_conflict",
                    "nonce_protected_material_reuse",
                    "protection_nonce_metadata_conflict",
                }
                self.assertEqual(
                    reuse_codes,
                    {
                        "protected_material_reuse",
                        "protected_material_metadata_conflict",
                    },
                )
                self.assertEqual(report.status, "findings")
                self.assertTrue(report.blocking)
                self.assertEqual(
                    report.semantic_integrity,
                    "contradictory",
                )
                self.assertFalse(
                    report.cryptographic_authenticity_verified
                )
                self.assertFalse(report.runtime_safety_established)
                rendered = report.to_json()
                human = report.to_human()
                encoded_material = base64.urlsafe_b64encode(
                    material
                ).decode("ascii")
                for private_value in (
                    first[0],
                    second[0],
                    material.decode("ascii"),
                    material.hex(),
                    encoded_material,
                    first[7].hex(),
                    first[16].hex(),
                ):
                    self.assertNotIn(private_value, rendered)
                    self.assertNotIn(private_value, human)

        with durable_transaction_database(
            suffix="reconciliation-nonce-reuse"
        ) as database:
            nonce = b"n" * 12
            _insert_reconciliation_rows(
                database.connection,
                (
                    _reconciliation_row(
                        1,
                        protection_key_version=11,
                        protection_nonce=nonce,
                        protected_material=b"a" * 17,
                    ),
                    _reconciliation_row(
                        2,
                        protection_key_version=12,
                        protection_nonce=nonce,
                        protected_material=b"b" * 33,
                    ),
                ),
            )
            with mock.patch.object(
                reconciliation,
                "_clock_now",
                return_value=NOW,
            ):
                report = reconcile_google_oidc_authorization_transactions(
                    database.connection,
                    accepted_protection_key_versions=(11, 12),
                )
            codes = set(dict(report.finding_counts_by_code))
            self.assertIn("duplicate_protection_nonce", codes)
            self.assertIn("protection_nonce_metadata_conflict", codes)

        with durable_transaction_database(
            suffix="reconciliation-nonce-material-reuse"
        ) as database:
            nonce = b"p" * 12
            material = b"same-nonce-and-protected-material"
            _insert_reconciliation_rows(
                database.connection,
                (
                    _reconciliation_row(
                        1,
                        protection_key_version=11,
                        protection_nonce=nonce,
                        protected_material=material,
                    ),
                    _reconciliation_row(
                        2,
                        protection_key_version=12,
                        protection_nonce=nonce,
                        protected_material=material,
                    ),
                ),
            )
            with mock.patch.object(
                reconciliation,
                "_clock_now",
                return_value=NOW,
            ):
                report = reconcile_google_oidc_authorization_transactions(
                    database.connection,
                    accepted_protection_key_versions=(11, 12),
                )
            self.assertIn(
                "nonce_protected_material_reuse",
                dict(report.finding_counts_by_code),
            )

        with durable_transaction_database(
            suffix="reconciliation-reuse-control"
        ) as database:
            _insert_reconciliation_rows(
                database.connection,
                (
                    _reconciliation_row(1),
                    _reconciliation_row(2),
                ),
            )
            with mock.patch.object(
                reconciliation,
                "_clock_now",
                return_value=NOW,
            ):
                report = reconcile_google_oidc_authorization_transactions(
                    database.connection,
                    accepted_protection_key_versions=(11,),
                )
            self.assertEqual(report.status, "clean")
            self.assertEqual(report.semantic_integrity, "unverified")
            self.assertFalse(report.runtime_safety_established)

    def test_cli_is_stable_read_only_and_guards_workspace_and_sidecars(self):
        with durable_transaction_database(suffix="reconciliation-cli") as database:
            before = _database_fingerprint(database.path)
            command = [
                sys.executable,
                "-B",
                str(
                    Path(__file__).resolve().parent.parent
                    / "scripts"
                    / "google_oidc_authorization_transactions_reconcile.py"
                ),
                "--db",
                str(database.path),
                "--lookup-key-version",
                "1",
                "--protection-key-version",
                "11",
                "--json",
            ]
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, b"")
            self.assertTrue(result.stdout.endswith(b"\n"))
            self.assertFalse(result.stdout.endswith(b"\r\n"))
            self.assertEqual(json.loads(result.stdout)["status"], "clean")
            self.assertEqual(_database_fingerprint(database.path), before)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = cli.main(
                    command[3:],
                    _workspace_path=database.path,
                )
            self.assertEqual(code, 2)
            self.assertEqual(
                json.loads(stdout.getvalue())["reason_code"],
                "invalid_reconciliation_request",
            )

            sidecar = Path(str(database.path) + "-journal")
            sidecar.write_bytes(b"synthetic journal")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = cli.main(
                    command[3:],
                    _workspace_path=database.path.parent / "other.sqlite",
                )
            self.assertEqual(code, 2)
            self.assertEqual(
                json.loads(stdout.getvalue())["reason_code"],
                "temporary_contention",
            )
            self.assertEqual(sidecar.read_bytes(), b"synthetic journal")

    def test_cli_immutable_wal_read_creates_no_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = directory / "checkpointed-wal.sqlite"
            connection = install_canonical_v2_profiles(path)
            try:
                migration.apply_google_oidc_authorization_transactions_migration(
                    connection
                )
                cursor = connection.execute("PRAGMA journal_mode = WAL")
                try:
                    self.assertEqual(cursor.fetchone()[0], "wal")
                finally:
                    cursor.close()
                connection.commit()
            finally:
                connection.close()
            sidecars = tuple(
                Path(str(path) + suffix)
                for suffix in ("-journal", "-wal", "-shm")
            )
            self.assertFalse(any(sidecar.exists() for sidecar in sidecars))
            identity = cli._file_identity(path)

            ordinary_read_only = sqlite3.connect(
                path.resolve().as_uri() + "?mode=ro",
                uri=True,
            )
            try:
                undeclared = (
                    reconciliation
                    .reconcile_google_oidc_authorization_transactions(
                        ordinary_read_only,
                        accepted_protection_key_versions=(11,),
                    )
                )
                self.assertEqual(undeclared.status, "unavailable")
                self.assertEqual(
                    undeclared.unavailable_reason,
                    "inspection_boundary_unavailable",
                )
                self.assertFalse(
                    any(sidecar.exists() for sidecar in sidecars)
                )
            finally:
                ordinary_read_only.close()

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = cli.main(
                    ["--db", str(path), "--json"],
                    _workspace_path=directory / "other.sqlite",
                )

            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["status"], "clean")
            self.assertEqual(cli._file_identity(path), identity)
            self.assertFalse(any(sidecar.exists() for sidecar in sidecars))

    def test_cli_path_identity_matrix_uses_only_the_shared_canonical_opener(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            names = [
                "literal%23.sqlite",
                "literal space.sqlite",
                "literal#name.sqlite",
                "literal-café.sqlite",
            ]
            if os.name != "nt":
                names.append("literal?name.sqlite")
            for name in names:
                with self.subTest(name=name):
                    path = directory / name
                    connection = install_canonical_v2_profiles(path)
                    migration.apply_google_oidc_authorization_transactions_migration(
                        connection
                    )
                    connection.close()
                    opened_uris = []

                    def tracked_connect(*args, **kwargs):
                        opened_uris.append(args[0])
                        return sqlite3.connect(*args, **kwargs)

                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        code = cli.main(
                            ["--db", str(path), "--json"],
                            _workspace_path=directory / "other.sqlite",
                            _connect=tracked_connect,
                        )
                    self.assertEqual(code, 0)
                    self.assertEqual(
                        json.loads(stdout.getvalue())["status"],
                        "clean",
                    )
                    self.assertEqual(len(opened_uris), 2)
                    self.assertTrue(
                        all("mode=ro" in uri for uri in opened_uris)
                    )
                    self.assertIn("immutable=1", opened_uris[0])
                    self.assertNotIn("immutable=1", opened_uris[1])
                    self.assertNotIn("#", opened_uris[0])
                    if "%" in name:
                        self.assertIn("%2523", opened_uris[0])
                    if " " in name:
                        self.assertIn("%20", opened_uris[0])
                    if "café" in name:
                        self.assertIn("caf%C3%A9", opened_uris[0])

            target = directory / "identity-target.sqlite"
            target_connection = install_canonical_v2_profiles(target)
            migration.apply_google_oidc_authorization_transactions_migration(
                target_connection
            )
            target_connection.close()
            decoy = directory / "identity-decoy.sqlite"
            decoy_connection = install_canonical_v2_profiles(decoy)
            migration.apply_google_oidc_authorization_transactions_migration(
                decoy_connection
            )
            decoy_connection.close()

            def wrong_main(_uri, **kwargs):
                return sqlite3.connect(
                    decoy,
                    timeout=kwargs.get("timeout", 2.0),
                )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = cli.main(
                    ["--db", str(target), "--json"],
                    _workspace_path=directory / "other.sqlite",
                    _connect=wrong_main,
                )
            self.assertEqual(code, 2)
            self.assertEqual(
                json.loads(stdout.getvalue())["reason_code"],
                "temporary_contention",
            )

            changed_once = False

            def change_during_open(*args, **kwargs):
                nonlocal changed_once
                connection = sqlite3.connect(*args, **kwargs)
                if not changed_once:
                    stat_result = target.stat()
                    os.utime(
                        target,
                        ns=(
                            stat_result.st_atime_ns,
                            stat_result.st_mtime_ns + 10_000_000,
                        ),
                    )
                    changed_once = True
                return connection

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = cli.main(
                    ["--db", str(target), "--json"],
                    _workspace_path=directory / "other.sqlite",
                    _connect=change_during_open,
                )
            self.assertEqual(code, 2)
            self.assertEqual(
                json.loads(stdout.getvalue())["reason_code"],
                "temporary_contention",
            )

            alias = directory / "identity-alias.sqlite"
            try:
                os.link(target, alias)
            except OSError:
                alias = None
            if alias is not None:
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = cli.main(
                        ["--db", str(alias), "--json"],
                        _workspace_path=target,
                    )
                self.assertEqual(code, 2)
                self.assertEqual(
                    json.loads(stdout.getvalue())["reason_code"],
                    "invalid_reconciliation_request",
                )

    def test_cli_source_identity_race_after_reconciliation_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            target = directory / "identity-race-target.sqlite"
            target_connection = install_canonical_v2_profiles(target)
            migration.apply_google_oidc_authorization_transactions_migration(
                target_connection
            )
            target_connection.close()
            replacement = directory / "identity-race-replacement.sqlite"
            replacement_connection = install_canonical_v2_profiles(
                replacement
            )
            migration.apply_google_oidc_authorization_transactions_migration(
                replacement_connection
            )
            replacement_connection.close()
            identity_before = cli._file_identity(target)
            original_reconcile = (
                cli.reconcile_google_oidc_authorization_transactions
            )

            def replace_or_mutate_source(connection, **kwargs):
                report = original_reconcile(connection, **kwargs)
                try:
                    os.replace(replacement, target)
                except OSError:
                    stat_result = target.stat()
                    os.utime(
                        target,
                        ns=(
                            stat_result.st_atime_ns,
                            stat_result.st_mtime_ns + 10_000_000,
                        ),
                    )
                return report

            stdout = io.StringIO()
            with mock.patch.object(
                cli,
                "reconcile_google_oidc_authorization_transactions",
                side_effect=replace_or_mutate_source,
            ), contextlib.redirect_stdout(stdout):
                code = cli.main(
                    ["--db", str(target), "--json"],
                    _workspace_path=directory / "other.sqlite",
                )

            self.assertEqual(code, 2)
            self.assertEqual(
                json.loads(stdout.getvalue())["reason_code"],
                "temporary_contention",
            )
            self.assertNotEqual(cli._file_identity(target), identity_before)


def _database_fingerprint(path):
    stat = path.stat()
    return (
        stat.st_size,
        stat.st_mtime_ns,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _reconciliation_row(
    index,
    *,
    provider="google",
    environment_namespace="test",
    configuration_fingerprint=None,
    lookup_key_version=1,
    protection_key_version=11,
    protection_nonce=None,
    protected_material=None,
    created_at=None,
    expires_at=None,
):
    return (
        f"oidctx_{index:032x}",
        1,
        provider,
        environment_namespace,
        configuration_fingerprint
        or hashlib.sha256(f"configuration-{index}".encode("ascii")).digest(),
        1,
        lookup_key_version,
        hashlib.sha256(f"state-{index}".encode("ascii")).digest(),
        created_at or NOW.isoformat(),
        expires_at
        or (NOW + reconciliation.timedelta(seconds=600)).isoformat(),
        "prepared",
        None,
        None,
        1,
        1,
        protection_key_version,
        protection_nonce or index.to_bytes(12, "big"),
        protected_material
        or hashlib.sha256(f"protected-{index}".encode("ascii")).digest(),
    )


def _insert_reconciliation_rows(connection, rows):
    connection.executemany(
        "INSERT INTO google_oidc_authorization_transactions VALUES ("
        + ",".join(("?",) * 18)
        + ")",
        rows,
    )
    connection.commit()


def _reconciliation_metadata(row):
    values = dict(zip(reconciliation._COLUMNS, row))
    storage = {
        name: _sqlite_storage(value)
        for name, value in values.items()
    }
    lengths = {
        name: _sqlite_length(value)
        for name, value in values.items()
    }
    return values, storage, lengths


def _sqlite_storage(value):
    if value is None:
        return "null"
    if type(value) is str:
        return "text"
    if type(value) is bytes:
        return "blob"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "real"
    raise AssertionError("unsupported reconciliation fixture value")


def _sqlite_length(value):
    if value is None:
        return None
    if type(value) is bytes:
        return len(value)
    return len(str(value).encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
