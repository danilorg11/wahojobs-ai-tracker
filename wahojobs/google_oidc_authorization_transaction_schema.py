from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

from wahojobs.account_reconciliation import (
    attest_account_schema,
    expected_account_schema_fingerprints,
)
from wahojobs.ownership_schema import (
    attest_ownership_schema,
    expected_ownership_manifest,
)
from wahojobs.persistent_profile_canonical_v2_schema import (
    attest_persistent_profile_canonical_v2_schema,
    expected_persistent_profile_canonical_v2_manifest,
)
from wahojobs.persistent_profile_schema import (
    attest_persistent_profile_schema,
    iter_sql_statements,
)


MIGRATION_VERSION = "006_google_oidc_authorization_transactions"
MIGRATION_PATH = (
    Path(__file__).resolve().parent
    / "db"
    / "migrations"
    / f"{MIGRATION_VERSION}.sql"
)
MIGRATION_001_PATH = (
    Path(__file__).resolve().parent
    / "db"
    / "migrations"
    / "001_pipeline_state.sql"
)
MIGRATION_003_PATH = (
    Path(__file__).resolve().parent
    / "db"
    / "migrations"
    / "003_product_principals.sql"
)
TRANSACTION_TABLE = "google_oidc_authorization_transactions"
TRANSACTION_COLUMNS = (
    "transaction_id",
    "record_version",
    "provider",
    "environment_namespace",
    "configuration_fingerprint",
    "state_digest_version",
    "lookup_key_version",
    "state_lookup_digest",
    "created_at",
    "expires_at",
    "lifecycle",
    "claimed_at",
    "terminal_at",
    "row_version",
    "protection_envelope_version",
    "protection_key_version",
    "protection_nonce",
    "protected_material",
)
TRANSACTION_INDEXES = (
    "uq_google_oidc_authorization_transactions_state_lookup",
    "uq_google_oidc_authorization_transactions_protection_nonce",
    "idx_google_oidc_authorization_transactions_prepared_expiry",
    "idx_google_oidc_authorization_transactions_terminal_cleanup",
)
TRANSACTION_TRIGGERS = (
    "trg_google_oidc_authorization_transactions_insert_guard",
    "trg_google_oidc_authorization_transactions_update_guard",
    "trg_google_oidc_authorization_transactions_delete_guard",
)
M006_VERIFICATION_INDEX_LIST_TABLES = frozenset(
    {
        "google_oidc_authorization_transactions",
        "legacy_owner_aliases",
        "ownership_binding_events",
        "principal_account_bindings",
        "product_principals",
        "product_profile_revisions",
        "product_profile_sources",
        "product_profiles",
        "user_pipeline_items",
        "user_pipeline_state",
        "user_pipeline_transitions",
        "wahojobs_schema_migrations",
    }
)
PREREQUISITE_MIGRATION_VERSIONS = (
    "001_pipeline_state",
    "002_accounts_sessions",
    "003_product_principals",
    "004_persistent_product_profiles",
    "005_persistent_profile_canonical_v2",
)
EXPECTED_SCHEMA_FINGERPRINT = (
    "68e923ece8223ea606782905e61ef81b3030e531191400b49efe92daac88e3c0"
)
EXPECTED_PREREQUISITE_001_SCHEMA_FINGERPRINT = (
    "aed3746fed57653a70f8746dc52a374a0da303f8392dbea13f44517a6ab83358"
)
_MAX_PREREQUISITE_SCHEMA_OBJECTS = 4096
_MAX_PREREQUISITE_VIEWS = 512
_MAX_PREREQUISITE_AUTHORIZER_CALLS = 8192
_MAX_PREREQUISITE_EXPLAIN_ROWS = 8192
_MAX_PREREQUISITE_COLUMNS = 8192
_MAX_PREREQUISITE_SCHEMA_SQL_BYTES = 8 * 1024 * 1024
_SQLITE_ASCII_IDENTIFIER_TRANSLATION = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)

FINDING_PARTIAL = "partial_inconsistent"
FINDING_CONFLICTING = "conflicting"
FINDING_SCHEMA_MISMATCH = "schema_mismatch"
FINDING_RESIDUE = "residue"
FINDING_INVALID_PREREQUISITE = "invalid_prerequisite"


class _PrerequisiteClosureBudgetExceeded(RuntimeError):
    __slots__ = ()


def is_m006_verification_index_list_pragma(
    name,
    argument,
    database,
    source,
) -> bool:
    """Recognize only fixed M006 index-list introspection."""
    return (
        name == "index_list"
        and type(argument) is str
        and argument in M006_VERIFICATION_INDEX_LIST_TABLES
        and database in {None, "main"}
        and source is None
    )


def expected_google_oidc_authorization_transaction_manifest() -> dict:
    """Return the exact Migration-006 owned-object manifest."""
    return copy.deepcopy(_expected_manifest())


@lru_cache(maxsize=1)
def _expected_manifest() -> dict:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))
        manifest = _capture_manifest(conn, "main")
        fingerprint = _manifest_fingerprint(manifest)
        if fingerprint != EXPECTED_SCHEMA_FINGERPRINT:
            raise RuntimeError("migration_006_committed_schema_fingerprint_changed")
        return {**manifest, "fingerprint": fingerprint}
    finally:
        conn.close()


def google_oidc_authorization_transaction_schema_fingerprint(conn) -> str:
    """Fingerprint the committed main-schema objects owned by Migration 006."""
    return _manifest_fingerprint(_capture_manifest(conn, "main"))


def attest_google_oidc_authorization_transaction_schema(
    conn,
    *,
    _operation_budget=None,
) -> dict:
    """Attest exact M006 objects, marker lineage, and temp-schema cleanliness."""
    expected = _expected_manifest()
    actual = _capture_manifest(conn, "main")
    temporary = _capture_manifest(conn, "temp")
    expected_fingerprint = expected["fingerprint"]
    actual_fingerprint = _manifest_fingerprint(actual)
    marker_table_present, marker_table_exact, marker_versions = _migration_markers(
        conn
    )
    marker_present = MIGRATION_VERSION in marker_versions
    missing_prerequisites = tuple(
        version
        for version in PREREQUISITE_MIGRATION_VERSIONS
        if version not in marker_versions
    )
    accepted_marker_versions = set(PREREQUISITE_MIGRATION_VERSIONS) | {
        MIGRATION_VERSION
    }
    unexpected_marker_versions = tuple(
        sorted(marker_versions - accepted_marker_versions)
    )

    prerequisite_attestation = None
    prerequisite_schema_valid = False
    if marker_table_exact and not missing_prerequisites:
        prerequisite_attestation = _attest_prerequisite_schema(
            conn,
            _operation_budget=_operation_budget,
        )
        prerequisite_schema_valid = (
            prerequisite_attestation["state"] == "correctly_installed"
        )

    findings: list[dict] = []
    has_main_footprint = bool(actual["objects"])
    has_temp_residue = bool(temporary["objects"])
    has_footprint = has_main_footprint or has_temp_residue or marker_present

    if not marker_table_present:
        findings.append(
            _finding(
                "migration_marker_table_missing",
                FINDING_INVALID_PREREQUISITE,
                object="wahojobs_schema_migrations",
                schema="main",
            )
        )
    elif not marker_table_exact:
        findings.append(
            _finding(
                "migration_marker_table_schema_mismatch",
                FINDING_INVALID_PREREQUISITE,
                object="wahojobs_schema_migrations",
                schema="main",
            )
        )
    for version in missing_prerequisites:
        findings.append(
            _finding(
                "prerequisite_migration_marker_missing",
                FINDING_INVALID_PREREQUISITE,
                migration=version,
                schema="main",
            )
        )
    for version in unexpected_marker_versions:
        findings.append(
            _finding(
                "unexpected_migration_marker",
                FINDING_INVALID_PREREQUISITE,
                migration=version,
                schema="main",
            )
        )
    if prerequisite_attestation is not None and not prerequisite_schema_valid:
        for version in prerequisite_attestation["invalid_migrations"]:
            detail = prerequisite_attestation["migrations"][version]
            findings.append(
                _finding(
                    "prerequisite_schema_attestation_failed",
                    FINDING_INVALID_PREREQUISITE,
                    migration=version,
                    prerequisite_state=detail["state"],
                    schema="main",
                )
            )

    if has_temp_residue:
        for kind, name, table_name in temporary["objects"]:
            findings.append(
                _finding(
                    "temporary_owned_object",
                    FINDING_RESIDUE,
                    object=name,
                    object_type=kind,
                    table=table_name,
                    schema="temp",
                )
            )

    expected_by_name = {
        name: (kind, table_name) for kind, name, table_name in expected["objects"]
    }
    actual_by_name = {
        name: (kind, table_name) for kind, name, table_name in actual["objects"]
    }
    if has_main_footprint or marker_present:
        for name, (kind, table_name) in sorted(expected_by_name.items()):
            installed = actual_by_name.get(name)
            if installed is None:
                findings.append(
                    _finding(
                        "missing_owned_object",
                        FINDING_PARTIAL,
                        object=name,
                        expected_type=kind,
                        table=table_name,
                        schema="main",
                    )
                )
            elif installed != (kind, table_name):
                findings.append(
                    _finding(
                        "same_name_conflicting_object",
                        FINDING_CONFLICTING,
                        object=name,
                        expected_type=kind,
                        actual_type=installed[0],
                        table=installed[1],
                        schema="main",
                    )
                )
        for name, (kind, table_name) in sorted(actual_by_name.items()):
            if name not in expected_by_name:
                findings.append(
                    _finding(
                        "unexpected_owned_object",
                        FINDING_CONFLICTING,
                        object=name,
                        actual_type=kind,
                        table=table_name,
                        schema="main",
                    )
                )

        for name in sorted(
            set(expected["definitions"]) & set(actual["definitions"])
        ):
            if expected["definitions"][name] != actual["definitions"][name]:
                findings.append(
                    _finding(
                        "schema_definition_mismatch",
                        FINDING_SCHEMA_MISMATCH,
                        object=name,
                        schema="main",
                    )
                )
        if (
            TRANSACTION_TABLE in expected["tables"]
            and TRANSACTION_TABLE in actual["tables"]
        ):
            for field, reason in (
                ("columns", "table_column_definition_mismatch"),
                ("indexes", "table_index_inventory_mismatch"),
                ("foreign_keys", "foreign_key_definition_mismatch"),
            ):
                if field == "indexes" and not set(TRANSACTION_INDEXES).issubset(
                    actual_by_name
                ):
                    continue
                if (
                    expected["tables"][TRANSACTION_TABLE][field]
                    != actual["tables"][TRANSACTION_TABLE][field]
                ):
                    findings.append(
                        _finding(
                            reason,
                            FINDING_SCHEMA_MISMATCH,
                            object=TRANSACTION_TABLE,
                            schema="main",
                        )
                    )
        for name in sorted(
            set(expected["index_details"]) & set(actual["index_details"])
        ):
            if expected["index_details"][name] != actual["index_details"][name]:
                findings.append(
                    _finding(
                        "index_definition_mismatch",
                        FINDING_SCHEMA_MISMATCH,
                        object=name,
                        schema="main",
                    )
                )

    if marker_present and not has_main_footprint:
        findings.append(
            _finding(
                "migration_marker_without_owned_schema",
                FINDING_PARTIAL,
                migration=MIGRATION_VERSION,
                schema="main",
            )
        )
    if has_main_footprint and not marker_present:
        findings.append(
            _finding(
                "owned_schema_without_migration_marker",
                FINDING_PARTIAL,
                migration=MIGRATION_VERSION,
                schema="main",
            )
        )
    if (
        has_main_footprint
        and actual_fingerprint != expected_fingerprint
        and not any(
            item["category"]
            in {FINDING_PARTIAL, FINDING_CONFLICTING, FINDING_SCHEMA_MISMATCH}
            for item in findings
        )
    ):
        findings.append(
            _finding(
                "schema_fingerprint_mismatch",
                FINDING_SCHEMA_MISMATCH,
                object=TRANSACTION_TABLE,
                schema="main",
            )
        )

    categories = {item["category"] for item in findings}
    if FINDING_RESIDUE in categories:
        state = FINDING_RESIDUE
    elif FINDING_CONFLICTING in categories:
        state = FINDING_CONFLICTING
    elif FINDING_SCHEMA_MISMATCH in categories:
        state = FINDING_SCHEMA_MISMATCH
    elif FINDING_PARTIAL in categories:
        state = FINDING_PARTIAL
    elif FINDING_INVALID_PREREQUISITE in categories:
        state = FINDING_INVALID_PREREQUISITE
    elif not has_footprint:
        state = "pending"
    elif (
        marker_present
        and prerequisite_schema_valid
        and actual_fingerprint == expected_fingerprint
    ):
        state = "correctly_installed"
    else:
        state = FINDING_SCHEMA_MISMATCH

    sorted_findings = sorted(findings, key=_finding_key)
    return {
        "state": state,
        "migration_version": MIGRATION_VERSION,
        "prerequisite_migration_versions": list(PREREQUISITE_MIGRATION_VERSIONS),
        "present_migration_versions": sorted(marker_versions),
        "missing_prerequisite_migrations": list(missing_prerequisites),
        "unexpected_migration_versions": list(unexpected_marker_versions),
        "migration_marker_table_present": marker_table_present,
        "migration_marker_table_exact": marker_table_exact,
        "migration_marker_present": marker_present,
        "marker_lineage_valid": (
            marker_table_present
            and marker_table_exact
            and not missing_prerequisites
            and not unexpected_marker_versions
            and prerequisite_schema_valid
        ),
        "prerequisite_schema_attestation": prerequisite_attestation,
        "findings": sorted_findings,
        "finding_categories": sorted({item["category"] for item in sorted_findings}),
        "blocking": state not in {"pending", "correctly_installed"},
        "applicable": state == "pending",
        "expected_schema_fingerprint": expected_fingerprint,
        "actual_schema_fingerprint": actual_fingerprint,
        "schema_fingerprint_matches": actual_fingerprint == expected_fingerprint,
        "expected_object_count": len(expected["objects"]),
        "present_expected_object_count": len(
            set(expected["objects"]) & set(actual["objects"])
        ),
        "expected_objects": [
            f"main:{kind}:{name}" for kind, name, _ in expected["objects"]
        ],
        "present_objects": [
            f"main:{kind}:{name}" for kind, name, _ in actual["objects"]
        ],
        "temporary_owned_objects": [
            f"temp:{kind}:{name}" for kind, name, _ in temporary["objects"]
        ],
        "expected_statement_count": migration_statement_count(),
    }


def _attest_prerequisite_schema(conn, *, _operation_budget=None) -> dict:
    pipeline = _attest_pipeline_state_schema(conn)
    accounts = _attest_account_prerequisite_schema(conn)
    ownership = attest_ownership_schema(conn)
    profiles_004 = attest_persistent_profile_schema(conn)
    profiles_005 = attest_persistent_profile_canonical_v2_schema(conn)
    ownership_closure = _attest_m001_m003_ownership_closure(
        conn,
        _operation_budget=_operation_budget,
    )
    temporary = _temporary_prerequisite_owned_objects(conn)
    contamination = sorted(
        {
            tuple(sorted(item.items())): item
            for item in (*ownership_closure, *temporary)
        }.values(),
        key=_finding_key,
    )
    migrations = {
        "001_pipeline_state": pipeline,
        "002_accounts_sessions": accounts,
        "003_product_principals": ownership,
        "004_persistent_product_profiles": profiles_004,
        "005_persistent_profile_canonical_v2": profiles_005,
    }
    for item in contamination:
        version = item["migration"]
        detail = migrations[version]
        if detail.get("state") == "correctly_installed":
            migrations[version] = {
                **detail,
                "state": "residue",
                "blocking": True,
            }
    invalid = {
        version
        for version in PREREQUISITE_MIGRATION_VERSIONS
        if migrations[version].get("state") != "correctly_installed"
    }
    invalid.update(item["migration"] for item in contamination)
    invalid = tuple(
        version
        for version in PREREQUISITE_MIGRATION_VERSIONS
        if version in invalid
    )
    return {
        "state": (
            "correctly_installed"
            if not invalid
            else "schema_definition_mismatch"
        ),
        "invalid_migrations": list(invalid),
        "migrations": migrations,
        "ownership_closure_findings": contamination,
        "temporary_owned_objects": [
            item for item in contamination if item["schema"] == "temp"
        ],
        "blocking": bool(invalid),
    }


def _attest_account_prerequisite_schema(conn) -> dict:
    expected = expected_account_schema_fingerprints()
    expected_keys = set(expected)
    expected_names = {name for _, name in expected_keys}
    main_owned = []
    for row in conn.execute(
        "SELECT type, name, tbl_name, sql FROM main.sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger', 'view')"
    ):
        values = tuple(row)
        if _is_account_schema_object(*values):
            main_owned.append(values)
    actual_keys = {(kind, name) for kind, name, _, _ in main_owned}
    missing = tuple(sorted(expected_keys - actual_keys))
    unexpected = tuple(
        sorted(
            (kind, name)
            for kind, name, _, _ in main_owned
            if (kind, name) not in expected_keys
            and not (
                kind == "index"
                and name.startswith("sqlite_autoindex_")
            )
        )
    )
    conflicting = tuple(
        sorted(
            (kind, name)
            for kind, name, _, _ in main_owned
            if name in expected_names and (kind, name) not in expected_keys
        )
    )
    valid = (
        attest_account_schema(conn)
        and not missing
        and not unexpected
        and not conflicting
    )
    return {
        "state": (
            "correctly_installed"
            if valid
            else "schema_definition_mismatch"
        ),
        "blocking": not valid,
        "missing_objects": [
            f"{kind}:{name}" for kind, name in missing
        ],
        "unexpected_objects": [
            f"{kind}:{name}" for kind, name in unexpected
        ],
        "conflicting_objects": [
            f"{kind}:{name}" for kind, name in conflicting
        ],
    }


def _attest_pipeline_state_schema(conn) -> dict:
    expected = _expected_pipeline_state_manifest()
    actual = _capture_pipeline_state_manifest(conn)
    expected_fingerprint = _manifest_fingerprint(expected)
    actual_fingerprint = _manifest_fingerprint(actual)
    valid = actual == expected
    return {
        "state": (
            "correctly_installed"
            if valid
            else "schema_definition_mismatch"
        ),
        "blocking": not valid,
        "expected_schema_fingerprint": expected_fingerprint,
        "actual_schema_fingerprint": actual_fingerprint,
        "schema_fingerprint_matches": valid,
        "expected_object_count": len(expected["objects"]),
        "present_expected_object_count": len(
            set(expected["objects"]) & set(actual["objects"])
        ),
    }


@lru_cache(maxsize=1)
def _expected_pipeline_state_manifest() -> dict:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(
            """
            CREATE TABLE user_pipeline_items (
              pipeline_item_id TEXT NOT NULL,
              profile_id TEXT NOT NULL
            );
            """
        )
        conn.executescript(MIGRATION_001_PATH.read_text(encoding="utf-8"))
        manifest = _capture_pipeline_state_manifest(conn)
        if (
            _manifest_fingerprint(manifest)
            != EXPECTED_PREREQUISITE_001_SCHEMA_FINGERPRINT
        ):
            raise RuntimeError(
                "migration_001_committed_schema_fingerprint_changed"
            )
        return manifest
    finally:
        conn.close()


@lru_cache(maxsize=1)
def _expected_m001_m003_ownership_contract() -> dict:
    m001_records = tuple(_expected_pipeline_state_manifest()["objects"])
    expected_m003 = set(expected_ownership_manifest()["objects"])
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(MIGRATION_003_PATH.read_text(encoding="utf-8"))
        m003_records = tuple(
            sorted(
                (kind, name, table_name)
                for kind, name, table_name in conn.execute(
                    "SELECT type, name, tbl_name FROM main.sqlite_schema "
                    "WHERE type IN ('table', 'index', 'trigger', 'view')"
                )
                if (kind, name) in expected_m003
            )
        )
    finally:
        conn.close()
    if {(kind, name) for kind, name, _ in m003_records} != expected_m003:
        raise RuntimeError("migration_003_committed_object_inventory_changed")

    contracts = {
        "001_pipeline_state": _ownership_contract(m001_records),
        "003_product_principals": _ownership_contract(m003_records),
    }
    accepted_pairs = {
        (kind, name)
        for contract in contracts.values()
        for kind, name, _ in contract["records"]
    }
    accepted_pairs.update(
        (kind, _sqlite_identifier_fold(name))
        for kind, name in expected_account_schema_fingerprints()
    )
    accepted_pairs.update(
        (kind, _sqlite_identifier_fold(name))
        for kind, name in expected_persistent_profile_canonical_v2_manifest()[
            "objects"
        ]
    )
    return {
        "migrations": contracts,
        "accepted_main_pairs": frozenset(accepted_pairs),
    }


def _ownership_contract(records) -> dict:
    records = tuple(
        sorted(
            (
                kind,
                _sqlite_identifier_fold(name),
                _sqlite_identifier_fold(table_name),
            )
            for kind, name, table_name in records
        )
    )
    owned_tables = frozenset(
        name for kind, name, _ in records if kind == "table"
    )
    owned_relations = frozenset(
        name for kind, name, _ in records if kind in {"table", "view"}
    )
    accepted_names = frozenset(name for _, name, _ in records)
    reserved_prefixes = {name + "_" for name in accepted_names}
    reserved_index_families = tuple(
        sorted(f"idx_{table_name}_" for table_name in owned_tables)
    )
    reserved_trigger_families = tuple(
        sorted(f"trg_{table_name}_" for table_name in owned_tables)
    )
    reserved_automatic_index_families = tuple(
        sorted(
            family
            for table_name in owned_tables
            for family in (f"sqlite_autoindex_{table_name}_",)
            if any(
                kind == "index" and name.startswith(family)
                for kind, name, _ in records
            )
        )
    )
    reserved_prefixes.update(
        table_name + "_" for table_name in owned_tables
    )
    reserved_prefixes.update(reserved_index_families)
    reserved_prefixes.update(reserved_trigger_families)
    reserved_prefixes.update(reserved_automatic_index_families)
    return {
        "records": frozenset(records),
        "accepted_names": accepted_names,
        "owned_tables": owned_tables,
        "owned_relations": owned_relations,
        "reserved_automatic_index_families": (
            reserved_automatic_index_families
        ),
        "reserved_index_families": reserved_index_families,
        "reserved_trigger_families": reserved_trigger_families,
        "reserved_prefixes": tuple(sorted(reserved_prefixes)),
    }


def _sqlite_identifier_fold(value: str) -> str:
    return value.translate(_SQLITE_ASCII_IDENTIFIER_TRANSLATION)


def _new_prerequisite_closure_budget() -> dict:
    limits = {
        "schema_objects": _MAX_PREREQUISITE_SCHEMA_OBJECTS,
        "views": _MAX_PREREQUISITE_VIEWS,
        "authorizer_calls": _MAX_PREREQUISITE_AUTHORIZER_CALLS,
        "explain_rows": _MAX_PREREQUISITE_EXPLAIN_ROWS,
        "columns": _MAX_PREREQUISITE_COLUMNS,
        "schema_sql_bytes": _MAX_PREREQUISITE_SCHEMA_SQL_BYTES,
    }
    return {
        "limits": limits,
        "used": {resource: 0 for resource in limits},
    }


def _consume_prerequisite_closure_budget(
    budget,
    resource: str,
    amount: int,
):
    if type(amount) is not int or amount < 0:
        raise RuntimeError("prerequisite_closure_budget_invalid")
    used = budget["used"][resource]
    limit = budget["limits"][resource]
    if amount > limit - used:
        raise _PrerequisiteClosureBudgetExceeded(
            f"prerequisite_{resource}_aggregate_bound_exceeded"
        )
    budget["used"][resource] = used + amount


def _remaining_prerequisite_closure_budget(
    budget,
    resource: str,
) -> int:
    return budget["limits"][resource] - budget["used"][resource]


def _attest_m001_m003_ownership_closure(
    conn,
    *,
    _operation_budget=None,
) -> list[dict]:
    try:
        budget = _new_prerequisite_closure_budget()
        contract = _expected_m001_m003_ownership_contract()
        snapshot = _bounded_prerequisite_schema_snapshot(
            conn,
            contract,
            budget,
        )
        findings = _reserved_prerequisite_namespace_findings(
            snapshot,
            contract,
        )
        dependencies = _semantic_prerequisite_view_dependencies(
            snapshot,
            contract,
            budget,
            _operation_budget=_operation_budget,
        )
        findings.extend(dependencies)
        return sorted(findings, key=_finding_key)
    except _PrerequisiteClosureBudgetExceeded:
        if _operation_budget is not None:
            _operation_budget.mark_exhausted()
        return [
            _closure_inspection_failure(version)
            for version in (
                "001_pipeline_state",
                "003_product_principals",
            )
        ]
    except Exception:
        return [
            _closure_inspection_failure(version)
            for version in (
                "001_pipeline_state",
                "003_product_principals",
            )
        ]
    finally:
        snapshot = None
        contract = None
        budget = None


def _bounded_prerequisite_schema_snapshot(conn, contract, budget) -> dict:
    snapshot = {}
    prior_length_limit = conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
    bounded_length_limit = min(
        prior_length_limit,
        (3 * _MAX_PREREQUISITE_SCHEMA_SQL_BYTES) + 4096,
    )
    conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, bounded_length_limit)
    try:
        for schema in ("main", "temp"):
            remaining_objects = _remaining_prerequisite_closure_budget(
                budget,
                "schema_objects",
            )
            rows = []
            cursor = conn.cursor()
            cursor.row_factory = None
            try:
                cursor.execute(
                    f"SELECT CAST(type AS BLOB), CAST(name AS BLOB), "
                    "CAST(tbl_name AS BLOB), CAST(sql AS BLOB) "
                    f"FROM {schema}.sqlite_schema "
                    "ORDER BY rowid "
                    f"LIMIT {remaining_objects + 1}"
                )
                while True:
                    raw = cursor.fetchone()
                    if raw is None:
                        break
                    _consume_prerequisite_closure_budget(
                        budget,
                        "schema_objects",
                        1,
                    )
                    if (
                        type(raw) is not tuple
                        or len(raw) != 4
                        or any(
                            type(value) is not bytes
                            for value in raw[:3]
                        )
                        or (
                            raw[3] is not None
                            and type(raw[3]) is not bytes
                        )
                    ):
                        raise RuntimeError(
                            "prerequisite_schema_metadata_invalid"
                        )
                    kind, name, table_name = (
                        value.decode("utf-8", "strict")
                        for value in raw[:3]
                    )
                    if kind not in {
                        "table",
                        "index",
                        "trigger",
                        "view",
                    }:
                        raise RuntimeError(
                            "prerequisite_schema_metadata_invalid"
                        )
                    sql = None
                    if raw[3] is not None:
                        _consume_prerequisite_closure_budget(
                            budget,
                            "schema_sql_bytes",
                            len(raw[3]),
                        )
                        if kind == "view":
                            sql = raw[3].decode("utf-8", "strict")
                    if kind == "view" and not (
                        schema == "main"
                        and (
                            kind,
                            _sqlite_identifier_fold(name),
                        )
                        in contract["accepted_main_pairs"]
                    ):
                        _consume_prerequisite_closure_budget(
                            budget,
                            "views",
                            1,
                        )
                    rows.append((kind, name, table_name, sql))
            finally:
                cursor.close()

            columns = {}
            for kind, name, _, _ in rows:
                if (
                    kind != "table"
                    or _sqlite_identifier_fold(name).startswith("sqlite_")
                ):
                    continue
                remaining_columns = (
                    _remaining_prerequisite_closure_budget(
                        budget,
                        "columns",
                    )
                )
                cursor = conn.cursor()
                cursor.row_factory = None
                try:
                    cursor.execute(
                        f"PRAGMA {schema}.table_xinfo({_quote(name)})"
                    )
                    column_rows = cursor.fetchmany(
                        remaining_columns + 1
                    )
                finally:
                    cursor.close()
                if (
                    not column_rows
                    or any(
                        type(row) is not tuple
                        or len(row) < 2
                        or type(row[1]) not in {str, bytes}
                        for row in column_rows
                    )
                ):
                    raise RuntimeError(
                        "prerequisite_table_columns_invalid"
                    )
                names = []
                for row in column_rows:
                    value = row[1]
                    if type(value) is bytes:
                        value = value.decode("utf-8", "strict")
                    else:
                        value.encode("utf-8", "strict")
                    names.append(value)
                names = tuple(names)
                _consume_prerequisite_closure_budget(
                    budget,
                    "columns",
                    len(names),
                )
                columns[name] = names
            snapshot[schema] = {
                "objects": tuple(rows),
                "columns": columns,
            }
        return snapshot
    finally:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, prior_length_limit)


def _reserved_prerequisite_namespace_findings(
    snapshot,
    contract,
) -> list[dict]:
    findings = []
    accepted_main_pairs = contract["accepted_main_pairs"]
    for schema in ("main", "temp"):
        for kind, name, table_name, _ in snapshot[schema]["objects"]:
            canonical_name = _sqlite_identifier_fold(name)
            canonical_table = _sqlite_identifier_fold(table_name)
            pair = (kind, canonical_name)
            for version, details in contract["migrations"].items():
                accepted = (
                    schema == "main"
                    and (
                        (
                            kind,
                            canonical_name,
                            canonical_table,
                        )
                        in details["records"]
                        or pair in accepted_main_pairs
                    )
                )
                if accepted:
                    continue
                if (
                    canonical_name in details["accepted_names"]
                    or canonical_table in details["owned_relations"]
                    or any(
                        canonical_name.startswith(prefix)
                        for prefix in details["reserved_prefixes"]
                    )
                ):
                    findings.append(
                        {
                            "reason": "unexpected_prerequisite_owned_object",
                            "migration": version,
                            "schema": schema,
                            "object_type": kind,
                            "object": name,
                            "table": table_name,
                        }
                    )
    return findings


def _semantic_prerequisite_view_dependencies(
    snapshot,
    contract,
    budget,
    *,
    _operation_budget=None,
) -> list[dict]:
    views = tuple(
        (schema, name)
        for schema in ("main", "temp")
        for kind, name, _, _ in snapshot[schema]["objects"]
        if kind == "view"
        and not (
            schema == "main"
            and (
                kind,
                _sqlite_identifier_fold(name),
            )
            in contract["accepted_main_pairs"]
        )
    )
    if len(views) != budget["used"]["views"]:
        raise RuntimeError("prerequisite_view_inventory_changed")
    analysis = sqlite3.connect(":memory:")
    authorizer_installed = False
    progress_installed = False
    state = {
        "budget_exceeded": False,
        "denied": False,
        "dependencies": set(),
    }

    def progress():
        try:
            if _operation_budget is not None:
                _operation_budget.consume_progress()
        except Exception:
            state["denied"] = True
            return 1
        return 0

    try:
        if _operation_budget is not None:
            analysis.set_progress_handler(progress, 1_000)
            progress_installed = True
        _reconstruct_schema_only_database(
            analysis,
            snapshot,
            _operation_budget=_operation_budget,
        )
        analysis.execute("PRAGMA query_only = ON")

        def authorize(action, first, _second, database, _source):
            try:
                if _operation_budget is not None:
                    _operation_budget.consume_authorizer()
                _consume_prerequisite_closure_budget(
                    budget,
                    "authorizer_calls",
                    1,
                )
            except _PrerequisiteClosureBudgetExceeded:
                state["budget_exceeded"] = True
                state["denied"] = True
                return sqlite3.SQLITE_DENY
            except Exception:
                state["denied"] = True
                return sqlite3.SQLITE_DENY
            canonical_first = (
                _sqlite_identifier_fold(first)
                if type(first) is str
                else None
            )
            canonical_database = (
                _sqlite_identifier_fold(database)
                if type(database) is str
                else None
            )
            for version, details in contract["migrations"].items():
                if (
                    action == sqlite3.SQLITE_READ
                    and canonical_first in details["owned_relations"]
                    and canonical_database in {None, "", "main"}
                ):
                    state["dependencies"].add(version)
            return sqlite3.SQLITE_OK

        analysis.set_authorizer(authorize)
        authorizer_installed = True
        findings = []
        for schema, name in views:
            state["denied"] = False
            state["dependencies"].clear()
            try:
                cursor = analysis.execute(
                    f"EXPLAIN SELECT * FROM {schema}.{_quote(name)}"
                )
            except sqlite3.DatabaseError:
                if state["budget_exceeded"]:
                    raise _PrerequisiteClosureBudgetExceeded(
                        "prerequisite_view_inspection_bound_exceeded"
                    ) from None
                raise
            try:
                remaining_rows = _remaining_prerequisite_closure_budget(
                    budget,
                    "explain_rows",
                )
                if _operation_budget is not None:
                    remaining_rows = min(
                        remaining_rows,
                        _operation_budget.remaining_results(),
                    )
                rows = cursor.fetchmany(
                    remaining_rows + 1
                )
                if _operation_budget is not None:
                    _operation_budget.consume_result(len(rows))
            finally:
                cursor.close()
            if state["denied"]:
                if state["budget_exceeded"]:
                    raise _PrerequisiteClosureBudgetExceeded(
                        "prerequisite_view_inspection_bound_exceeded"
                    )
                raise RuntimeError("prerequisite_view_inspection_failed")
            _consume_prerequisite_closure_budget(
                budget,
                "explain_rows",
                len(rows),
            )
            for version in sorted(state["dependencies"]):
                findings.append(
                    {
                        "reason": (
                            "unexpected_prerequisite_view_dependency"
                        ),
                        "migration": version,
                        "schema": schema,
                        "object_type": "view",
                        "object": name,
                        "table": name,
                    }
                )
        return findings
    finally:
        try:
            if authorizer_installed:
                analysis.set_authorizer(None)
        finally:
            try:
                if progress_installed:
                    analysis.set_progress_handler(None, 0)
            finally:
                analysis.close()


def _reconstruct_schema_only_database(
    analysis,
    snapshot,
    *,
    _operation_budget=None,
):
    for schema in ("main", "temp"):
        for name, columns in sorted(snapshot[schema]["columns"].items()):
            column_sql = ", ".join(
                f"{_quote(column)} BLOB" for column in columns
            )
            temporary = "TEMP " if schema == "temp" else ""
            analysis.execute(
                f"CREATE {temporary}TABLE {_quote(name)} ({column_sql})"
            )
    for schema in ("main", "temp"):
        for kind, name, _, sql in snapshot[schema]["objects"]:
            if kind != "view":
                continue
            if type(sql) is not str:
                raise RuntimeError("prerequisite_view_definition_missing")
            statement = (
                sql
                if schema == "main"
                else _as_temporary_view_statement(sql)
            )
            analysis.execute(statement)
            installed = analysis.execute(
                f"SELECT 1 FROM {schema}.sqlite_schema "
                "WHERE type='view' AND name="
                f"{_quote_sql_text(name)}"
            ).fetchone()
            if installed is not None and _operation_budget is not None:
                _operation_budget.consume_result()
            if installed is None:
                raise RuntimeError(
                    "prerequisite_view_reconstruction_failed"
                )


def _as_temporary_view_statement(sql: str) -> str:
    stripped = sql.lstrip()
    leading = sql[: len(sql) - len(stripped)]
    if (
        len(stripped) <= 6
        or stripped[:6].casefold() != "create"
        or not stripped[6].isspace()
    ):
        raise RuntimeError("prerequisite_view_definition_invalid")
    return leading + stripped[:6] + " TEMP" + stripped[6:]


def _closure_inspection_failure(version: str) -> dict:
    return {
        "reason": "prerequisite_ownership_closure_inspection_failed",
        "migration": version,
        "schema": "main",
        "object_type": "view",
        "object": "prerequisite_ownership_closure",
        "table": "prerequisite_ownership_closure",
    }


def _capture_pipeline_state_manifest(conn) -> dict:
    pipeline_tables = (
        "user_pipeline_state",
        "user_pipeline_transitions",
        "wahojobs_schema_migrations",
    )
    raw_objects = []
    for row in conn.execute(
        "SELECT type, name, tbl_name, sql FROM main.sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger', 'view')"
    ):
        kind, name, table_name, sql = tuple(row)
        if _is_pipeline_state_object(kind, name, table_name):
            raw_objects.append((kind, name, table_name, sql))
    raw_objects.sort(key=lambda item: (item[0], item[1], item[2]))
    objects = tuple(
        (kind, name, table_name)
        for kind, name, table_name, _ in raw_objects
    )
    definitions = {
        name: _normalize_sql(sql)
        for _, name, _, sql in raw_objects
        if sql is not None
    }
    tables = {}
    index_details = {}
    for table in pipeline_tables:
        if not any(
            kind == "table" and name == table
            for kind, name, _, _ in raw_objects
        ):
            continue
        columns = tuple(
            tuple(row[index] for index in range(7))
            for row in conn.execute(
                f"PRAGMA main.table_xinfo({_quote(table)})"
            )
        )
        foreign_keys = tuple(
            sorted(
                tuple(row[index] for index in range(8))
                for row in conn.execute(
                    f"PRAGMA main.foreign_key_list({_quote(table)})"
                )
            )
        )
        indexes = []
        for row in conn.execute(f"PRAGMA main.index_list({_quote(table)})"):
            values = tuple(row)
            name = values[1]
            indexes.append((name, values[2], values[3], values[4]))
            index_details[name] = {
                "table": table,
                "unique": values[2],
                "origin": values[3],
                "partial": values[4],
                "columns": tuple(
                    tuple(item[index] for index in range(6))
                    for item in conn.execute(
                        f"PRAGMA main.index_xinfo({_quote(name)})"
                    )
                ),
            }
        tables[table] = {
            "columns": columns,
            "foreign_keys": foreign_keys,
            "indexes": tuple(sorted(indexes)),
        }
    base_index = "idx_user_pipeline_items_pipeline_profile"
    if any(name == base_index for _, name, _ in objects):
        row = None
        cursor = conn.cursor()
        cursor.row_factory = None
        try:
            cursor.execute(
                "PRAGMA main.index_list('user_pipeline_items')"
            )
            for _index in range(
                _MAX_PREREQUISITE_SCHEMA_OBJECTS + 1
            ):
                candidate = cursor.fetchone()
                if candidate is None:
                    break
                if _index == _MAX_PREREQUISITE_SCHEMA_OBJECTS:
                    raise RuntimeError(
                        "prerequisite_index_metadata_invalid"
                    )
                if (
                    type(candidate) is not tuple
                    or len(candidate) < 5
                ):
                    raise RuntimeError(
                        "prerequisite_index_metadata_invalid"
                    )
                if candidate[1] == base_index:
                    row = (
                        candidate[2],
                        candidate[3],
                        candidate[4],
                    )
                    break
        finally:
            cursor.close()
        if row is not None:
            index_details[base_index] = {
                "table": "user_pipeline_items",
                "unique": row[0],
                "origin": row[1],
                "partial": row[2],
                "columns": tuple(
                    tuple(item[index] for index in (0, 2, 3, 4, 5))
                    for item in conn.execute(
                        f"PRAGMA main.index_xinfo({_quote(base_index)})"
                    )
                ),
            }
    return {
        "objects": objects,
        "definitions": definitions,
        "tables": tables,
        "index_details": index_details,
    }


def _is_pipeline_state_object(
    kind: str,
    name: str,
    table_name: str,
) -> bool:
    tables = {
        "user_pipeline_state",
        "user_pipeline_transitions",
        "wahojobs_schema_migrations",
    }
    if name in tables or table_name in tables:
        return True
    if name == "idx_user_pipeline_items_pipeline_profile":
        return True
    return name.startswith(
        (
            "idx_user_pipeline_state_",
            "idx_user_pipeline_transitions_",
            "trg_user_pipeline_state_",
            "trg_user_pipeline_transitions_",
            "sqlite_autoindex_user_pipeline_state_",
            "sqlite_autoindex_user_pipeline_transitions_",
        )
    )


def _is_account_schema_object(
    kind: str,
    name: str,
    table_name: str,
    sql: str | None,
) -> bool:
    expected = expected_account_schema_fingerprints()
    expected_names = {item_name for _, item_name in expected}
    account_tables = {
        item_name
        for item_kind, item_name in expected
        if item_kind == "table"
    }
    if name in expected_names or table_name in account_tables:
        return True
    if any(
        name.startswith(
            (
                table + "_",
                "idx_" + table + "_",
                "uq_" + table + "_",
                "trg_" + table + "_",
                "sqlite_autoindex_" + table + "_",
            )
        )
        for table in account_tables
    ):
        return True
    return kind == "view" and any(
        _sql_mentions_identifier(sql, table) for table in account_tables
    )


def _temporary_prerequisite_owned_objects(conn) -> list[dict]:
    findings = []
    for row in conn.execute(
        "SELECT type, name, tbl_name, sql FROM temp.sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger', 'view')"
    ):
        kind, name, table_name, sql = tuple(row)
        migration = _prerequisite_object_migration(
            kind,
            name,
            table_name,
            sql,
        )
        if migration is not None:
            findings.append(
                {
                    "migration": migration,
                    "object_type": kind,
                    "object": name,
                    "table": table_name,
                    "schema": "temp",
                }
            )
    return sorted(findings, key=_finding_key)


def _prerequisite_object_migration(
    kind: str,
    name: str,
    table_name: str,
    sql: str | None,
) -> str | None:
    if _is_account_schema_object(kind, name, table_name, sql):
        return "002_accounts_sessions"

    profile_manifest = expected_persistent_profile_canonical_v2_manifest()
    profile_objects = set(profile_manifest["objects"])
    profile_names = {item_name for _, item_name in profile_objects}
    profile_tables = {
        item_name
        for item_kind, item_name in profile_objects
        if item_kind == "table"
    }
    if (
        name in profile_names
        or table_name in profile_tables
        or any(
            name.startswith(
                (
                    table + "_",
                    "idx_" + table + "_",
                    "uq_" + table + "_",
                    "trg_" + table + "_",
                    "sqlite_autoindex_" + table + "_",
                )
            )
            for table in profile_tables
        )
        or (
            kind == "view"
            and any(
                _sql_mentions_identifier(sql, table)
                for table in profile_tables
            )
        )
    ):
        return "005_persistent_profile_canonical_v2"
    return None


def _sql_mentions_identifier(sql: str | None, name: str) -> bool:
    if type(sql) is not str:
        return False
    return re.search(
        rf"(?<![a-z0-9_]){re.escape(name.casefold())}(?![a-z0-9_])",
        sql.casefold(),
    ) is not None


@lru_cache(maxsize=1)
def migration_statement_count() -> int:
    return sum(
        1 for _ in iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8"))
    )


def _capture_manifest(conn, schema: str) -> dict:
    if schema not in {"main", "temp"}:
        raise ValueError("unsupported_schema_namespace")
    raw_objects = []
    for row in conn.execute(
        f"SELECT type, name, tbl_name, sql FROM {schema}.sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger', 'view')"
    ):
        kind, name, table_name, sql = tuple(row)
        if _is_owned_object(kind, name, table_name):
            raw_objects.append((kind, name, table_name, sql))
    raw_objects.sort(key=lambda item: (item[0], item[1], item[2]))
    objects = tuple((kind, name, table_name) for kind, name, table_name, _ in raw_objects)
    definitions = {
        name: _normalize_sql(sql)
        for _, name, _, sql in raw_objects
        if sql is not None
    }
    tables = {}
    index_details = {}
    if any(
        kind == "table" and name == TRANSACTION_TABLE
        for kind, name, _, _ in raw_objects
    ):
        columns = tuple(
            tuple(row[index] for index in range(7))
            for row in conn.execute(
                f"PRAGMA {schema}.table_xinfo({_quote(TRANSACTION_TABLE)})"
            )
        )
        foreign_keys = tuple(
            sorted(
                tuple(row[index] for index in range(8))
                for row in conn.execute(
                    f"PRAGMA {schema}.foreign_key_list({_quote(TRANSACTION_TABLE)})"
                )
            )
        )
        indexes = []
        for row in conn.execute(
            f"PRAGMA {schema}.index_list({_quote(TRANSACTION_TABLE)})"
        ):
            values = tuple(row)
            name = values[1]
            indexes.append((name, values[2], values[3], values[4]))
            index_details[name] = {
                "table": TRANSACTION_TABLE,
                "unique": values[2],
                "origin": values[3],
                "partial": values[4],
                "columns": tuple(
                    tuple(item[index] for index in range(6))
                    for item in conn.execute(
                        f"PRAGMA {schema}.index_xinfo({_quote(name)})"
                    )
                ),
            }
        tables[TRANSACTION_TABLE] = {
            "columns": columns,
            "foreign_keys": foreign_keys,
            "indexes": tuple(sorted(indexes)),
        }
    return {
        "objects": objects,
        "definitions": definitions,
        "tables": tables,
        "index_details": index_details,
    }


def _manifest_fingerprint(manifest: dict) -> str:
    payload = {
        "objects": manifest["objects"],
        "definitions": sorted(manifest["definitions"].items()),
        "tables": manifest["tables"],
        "index_details": manifest["index_details"],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _is_owned_object(kind: str, name: str, table_name: str) -> bool:
    if name == TRANSACTION_TABLE or table_name == TRANSACTION_TABLE:
        return True
    prefixes = (
        "google_oidc_authorization_transaction",
        "idx_google_oidc_authorization_transaction",
        "uq_google_oidc_authorization_transaction",
        "trg_google_oidc_authorization_transaction",
        "sqlite_autoindex_google_oidc_authorization_transaction",
    )
    return name.startswith(prefixes)


def _migration_markers(conn) -> tuple[bool, bool, set[str]]:
    row = conn.execute(
        "SELECT sql FROM main.sqlite_master "
        "WHERE type='table' AND name='wahojobs_schema_migrations'"
    ).fetchone()
    if row is None:
        return False, False, set()
    expected_sql = _normalize_sql(
        """
        CREATE TABLE wahojobs_schema_migrations (
          version TEXT PRIMARY KEY,
          applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    expected_columns = (
        (0, "version", "TEXT", 0, None, 1, 0),
        (1, "applied_at", "TEXT", 1, "CURRENT_TIMESTAMP", 0, 0),
    )
    try:
        columns = tuple(
            tuple(item[index] for index in range(7))
            for item in conn.execute(
                'PRAGMA main.table_xinfo("wahojobs_schema_migrations")'
            )
        )
        marker_rows = conn.execute(
            "SELECT version FROM main.wahojobs_schema_migrations"
        ).fetchall()
        marker_values_exact = all(type(item[0]) is str for item in marker_rows)
        versions = {
            item[0] for item in marker_rows if type(item[0]) is str
        }
    except sqlite3.DatabaseError:
        return True, False, set()
    exact = (
        _normalize_sql(row[0]) == expected_sql
        and columns == expected_columns
        and marker_values_exact
        and len(versions) == len(marker_rows)
    )
    return True, exact, versions


def _normalize_sql(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", value.strip().rstrip(";"))


def _finding(reason: str, category: str, **details) -> dict:
    return {"reason": reason, "category": category, **details}


def _finding_key(item: dict) -> tuple:
    return tuple(str(item.get(key, "")) for key in sorted(item))


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
