from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path

from wahojobs.persistent_profile_schema import (
    MIGRATION_PATH as MIGRATION_004_PATH,
    MIGRATION_VERSION as MIGRATION_004_VERSION,
    PROFILE_INDEXES,
    PROFILE_TABLES,
    PROFILE_TRIGGERS,
    PROFILE_VIEWS,
    _capture_manifest,
    _finding_key,
    _install_manifest_prerequisites,
    _normalize_sql,
    _table_exists,
    attest_persistent_profile_schema,
    expected_persistent_profile_manifest,
    iter_sql_statements,
)


MIGRATION_VERSION = "005_persistent_profile_canonical_v2"
MIGRATION_PATH = (
    Path(__file__).resolve().parent
    / "db"
    / "migrations"
    / f"{MIGRATION_VERSION}.sql"
)
TEMPORARY_TABLES = (
    "product_profiles_m005_backup",
    "product_profile_revisions_m005_backup",
    "product_profile_sources_m005_backup",
)
FINDING_CONFLICTING = "conflicting"
FINDING_PARTIAL = "partial_inconsistent"
FINDING_SCHEMA_MISMATCH = "schema_mismatch"


@lru_cache(maxsize=1)
def expected_persistent_profile_canonical_v2_manifest() -> dict:
    """Build the exact final M004+M005 object manifest in memory."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        _install_manifest_prerequisites(conn)
        conn.executescript(MIGRATION_004_PATH.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO wahojobs_schema_migrations(version) VALUES (?)",
            (MIGRATION_004_VERSION,),
        )
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("PRAGMA defer_foreign_keys = ON")
        for statement in iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8")):
            conn.execute(statement)
        conn.commit()
        return _capture_manifest(conn)
    finally:
        conn.close()


def persistent_profile_state_counts(conn) -> dict[str, int | None]:
    counts: dict[str, int | None] = {}
    for name in (*PROFILE_TABLES, *PROFILE_VIEWS):
        object_type = "view" if name in PROFILE_VIEWS else "table"
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type=? AND name=?", (object_type, name)
        ).fetchone()
        counts[name] = (
            conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            if exists
            else None
        )
    return counts


def persistent_profile_state_is_empty(counts: dict[str, int | None]) -> bool:
    return all(value == 0 for value in counts.values())


def attest_persistent_profile_canonical_v2_schema(conn) -> dict:
    """Attest M005 exactly while recognizing a clean empty M004 prerequisite."""
    expected = expected_persistent_profile_canonical_v2_manifest()
    expected_m004 = expected_persistent_profile_manifest()
    actual = _capture_manifest(conn)
    findings = _manifest_findings(expected, actual)
    marker_004 = _marker_present(conn, MIGRATION_004_VERSION)
    marker_005 = _marker_present(conn, MIGRATION_VERSION)
    temporary_residue = tuple(
        name
        for name in TEMPORARY_TABLES
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )
    for name in temporary_residue:
        findings.append(
            _finding(
                "temporary_migration_residue",
                FINDING_PARTIAL,
                object=name,
                object_namespace="migration_005_temporary",
                expected_presence=False,
                actual_presence=True,
                actual_type="table",
                temporary_rebuild_residue=True,
            )
        )

    m004_attestation = None
    counts = persistent_profile_state_counts(conn)
    if not marker_004:
        state = "invalid_prerequisite"
    elif marker_005 and not findings:
        state = "correctly_installed"
    elif marker_005:
        if _manifests_equal(expected_m004, actual):
            findings.append(
                _finding(
                    "migration_marker_with_original_m004_schema",
                    FINDING_PARTIAL,
                    object_namespace="migration_lineage",
                    expected_presence=False,
                    actual_presence=True,
                )
            )
        state = _state_from_findings(findings)
    elif not findings:
        findings.append(
            _finding(
                "migration_marker_missing",
                FINDING_PARTIAL,
                migration=MIGRATION_VERSION,
                object_namespace="migration_lineage",
                expected_presence=True,
                actual_presence=False,
            )
        )
        state = FINDING_PARTIAL
    else:
        m004_attestation = attest_persistent_profile_schema(conn)
        if m004_attestation["state"] == "correctly_installed":
            state = (
                "pending"
                if persistent_profile_state_is_empty(counts)
                else "persistent_profile_state_not_empty"
            )
            findings = []
        elif _has_m005_footprint(
            expected,
            expected_m004,
            actual,
            findings,
            marker_005=marker_005,
            temporary_residue=temporary_residue,
        ):
            state = _state_from_findings(findings)
        else:
            state = "invalid_prerequisite"

    expected_objects = expected["objects"]
    actual_objects = actual["objects"]
    return {
        "state": state,
        "migration_version": MIGRATION_VERSION,
        "migration_004_marker_present": marker_004,
        "migration_marker_present": marker_005,
        "findings": sorted(findings, key=_finding_key),
        "finding_categories": sorted(
            {item["category"] for item in findings if item.get("category")}
        ),
        "blocking": state not in {"pending", "correctly_installed"},
        "applicable": state == "pending",
        "profile_state_counts": counts,
        "temporary_residue": list(temporary_residue),
        "expected_object_count": len(expected_objects),
        "present_expected_object_count": len(set(expected_objects) & set(actual_objects)),
        "expected_objects": [f"{kind}:{name}" for kind, name in sorted(expected_objects)],
        "present_objects": [f"{kind}:{name}" for kind, name in sorted(actual_objects)],
        "expected_statement_count": migration_statement_count(),
        "migration_004_attestation": m004_attestation,
    }


@lru_cache(maxsize=1)
def migration_statement_count() -> int:
    return sum(1 for _ in iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8")))


def _manifest_findings(expected: dict, actual: dict) -> list[dict]:
    findings: list[dict] = []
    expected_objects = expected["objects"]
    actual_objects = actual["objects"]
    expected_by_name = {name: kind for kind, name in expected_objects}
    actual_by_name = {name: kind for kind, name in actual_objects}
    for name, kind in sorted(expected_by_name.items()):
        actual_kind = actual_by_name.get(name)
        if actual_kind is None:
            findings.append(
                _finding(
                    "missing_object",
                    (
                        FINDING_SCHEMA_MISMATCH
                        if _is_automatic_index(name)
                        else FINDING_PARTIAL
                    ),
                    object=name,
                    object_namespace=_object_namespace(name),
                    expected_presence=True,
                    actual_presence=False,
                    expected_type=kind,
                )
            )
        elif actual_kind != kind:
            findings.append(
                _finding(
                    "same_name_conflicting_object",
                    FINDING_CONFLICTING,
                    object=name,
                    object_namespace=_object_namespace(name),
                    expected_presence=True,
                    actual_presence=True,
                    expected_type=kind,
                    actual_type=actual_kind,
                )
            )
    for name, kind in sorted(actual_by_name.items()):
        if name not in expected_by_name:
            findings.append(
                _finding(
                    "unexpected_persistent_profile_object",
                    (
                        FINDING_PARTIAL
                        if name in TEMPORARY_TABLES
                        else FINDING_SCHEMA_MISMATCH
                        if _is_automatic_index(name)
                        else FINDING_CONFLICTING
                    ),
                    object=name,
                    object_namespace=_object_namespace(name),
                    expected_presence=False,
                    actual_presence=True,
                    actual_type=kind,
                    temporary_rebuild_residue=name in TEMPORARY_TABLES,
                )
            )
    for key in sorted(set(expected["definitions"]) & set(actual["definitions"])):
        if _normalize_sql(expected["definitions"][key]) != _normalize_sql(actual["definitions"][key]):
            findings.append(
                _finding(
                    "schema_definition_mismatch",
                    FINDING_SCHEMA_MISMATCH,
                    object=key[1],
                    object_namespace=_object_namespace(key[1]),
                    expected_presence=True,
                    actual_presence=True,
                    expected_type=key[0],
                    actual_type=key[0],
                    definition_mismatch=True,
                )
            )
    for table in PROFILE_TABLES:
        if table not in expected["tables"] or table not in actual["tables"]:
            continue
        for field, reason in (
            ("columns", "table_column_definition_mismatch"),
            ("foreign_keys", "foreign_key_definition_mismatch"),
            ("indexes", "table_index_inventory_mismatch"),
        ):
            if expected["tables"][table][field] != actual["tables"][table][field]:
                findings.append(
                    _finding(
                        reason,
                        FINDING_SCHEMA_MISMATCH,
                        table=table,
                        object_namespace="persistent_profile",
                        expected_presence=True,
                        actual_presence=True,
                        expected_type="table",
                        actual_type="table",
                        definition_mismatch=True,
                    )
                )
    for index_name in sorted(set(expected["index_details"]) & set(actual["index_details"])):
        if expected["index_details"][index_name] != actual["index_details"][index_name]:
            findings.append(
                _finding(
                    "index_definition_mismatch",
                    FINDING_SCHEMA_MISMATCH,
                    index=index_name,
                    object_namespace=_object_namespace(index_name),
                    expected_presence=True,
                    actual_presence=True,
                    expected_type="index",
                    actual_type="index",
                    definition_mismatch=True,
                )
            )
    return findings


def _state_from_findings(findings: list[dict]) -> str:
    categories = {item.get("category") for item in findings}
    for category in (
        FINDING_CONFLICTING,
        FINDING_PARTIAL,
        FINDING_SCHEMA_MISMATCH,
    ):
        if category in categories:
            return category
    return FINDING_SCHEMA_MISMATCH


def _has_m005_footprint(
    expected: dict,
    expected_m004: dict,
    actual: dict,
    findings: list[dict],
    *,
    marker_005: bool,
    temporary_residue: tuple[str, ...],
) -> bool:
    if marker_005 or temporary_residue:
        return True
    if any(item.get("category") == FINDING_CONFLICTING for item in findings):
        return True
    for key, definition in actual["definitions"].items():
        if (
            key in expected["definitions"]
            and key in expected_m004["definitions"]
            and _normalize_sql(definition)
            == _normalize_sql(expected["definitions"][key])
            and _normalize_sql(definition)
            != _normalize_sql(expected_m004["definitions"][key])
        ):
            return True
    return False


def _manifests_equal(first: dict, second: dict) -> bool:
    return first == second


def _finding(reason: str, category: str, **details) -> dict:
    return {
        "reason": reason,
        "category": category,
        "expected_presence": details.pop("expected_presence", None),
        "actual_presence": details.pop("actual_presence", None),
        "expected_type": details.pop("expected_type", None),
        "actual_type": details.pop("actual_type", None),
        "definition_mismatch": details.pop("definition_mismatch", False),
        "temporary_rebuild_residue": details.pop(
            "temporary_rebuild_residue", False
        ),
        **details,
    }


def _object_namespace(name: str) -> str:
    if name in TEMPORARY_TABLES:
        return "migration_005_temporary"
    if _is_automatic_index(name):
        return "sqlite_automatic_index"
    return "persistent_profile"


def _is_automatic_index(name: str) -> bool:
    return name.startswith("sqlite_autoindex_")


def _marker_present(conn, version: str) -> bool:
    if not _table_exists(conn, "wahojobs_schema_migrations"):
        return False
    return conn.execute(
        "SELECT 1 FROM wahojobs_schema_migrations WHERE version=?", (version,)
    ).fetchone() is not None
