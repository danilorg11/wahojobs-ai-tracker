from __future__ import annotations

import hashlib
import re

from wahojobs.ownership import (
    MIGRATION_VERSION,
    OWNERSHIP_INDEXES,
    OWNERSHIP_TABLES,
    OWNERSHIP_TRIGGERS,
)


FORWARD_COMPATIBLE_OWNERSHIP_OBJECTS = {
    ("index", "uq_product_principals_profile_environment"): "004_persistent_product_profiles",
}
_OWNERSHIP_AUTO_INDEXES = (
    "sqlite_autoindex_legacy_owner_aliases_1",
    "sqlite_autoindex_legacy_owner_aliases_2",
    "sqlite_autoindex_ownership_binding_events_1",
    "sqlite_autoindex_ownership_binding_events_2",
    "sqlite_autoindex_ownership_binding_events_3",
    "sqlite_autoindex_principal_account_bindings_1",
    "sqlite_autoindex_principal_account_bindings_2",
    "sqlite_autoindex_product_principals_1",
)
_EXPECTED_OWNERSHIP_OBJECTS = tuple(
    sorted(
        {("table", name) for name in OWNERSHIP_TABLES}
        | {("index", name) for name in OWNERSHIP_INDEXES}
        | {("index", name) for name in _OWNERSHIP_AUTO_INDEXES}
        | {("trigger", name) for name in OWNERSHIP_TRIGGERS}
    )
)
_EXPECTED_OWNERSHIP_MANIFEST_FINGERPRINT = (
    "4ce6a0509f0114e5e0021099996117cb0beea69a40e145985d71c4517a6dda69"
)


def expected_ownership_manifest() -> dict:
    """Return the immutable committed Migration-003 manifest contract."""
    return {
        "objects": _EXPECTED_OWNERSHIP_OBJECTS,
        "fingerprint": _EXPECTED_OWNERSHIP_MANIFEST_FINGERPRINT,
    }


def attest_ownership_schema(conn) -> dict:
    """Compare installed ownership objects with the complete canonical manifest."""
    expected = expected_ownership_manifest()
    actual = _capture_manifest(conn)
    findings: list[dict] = []

    expected_objects = expected["objects"]
    actual_objects = actual["objects"]
    expected_by_name = {name: kind for kind, name in expected_objects}
    actual_by_name = {name: kind for kind, name in actual_objects}

    for name, kind in sorted(expected_by_name.items()):
        actual_kind = actual_by_name.get(name)
        if actual_kind is None:
            findings.append(
                {"reason": "missing_object", "object": name, "expected_type": kind}
            )
        elif actual_kind != kind:
            findings.append(
                {
                    "reason": "same_name_conflicting_object",
                    "object": name,
                    "expected_type": kind,
                    "actual_type": actual_kind,
                }
            )

    for name, kind in sorted(actual_by_name.items()):
        if name not in expected_by_name:
            findings.append(
                {"reason": "unexpected_ownership_object", "object": name, "actual_type": kind}
            )

    if _manifest_fingerprint(actual) != expected["fingerprint"]:
        findings.append(
            {
                "reason": "schema_definition_mismatch",
                "object": "ownership_manifest",
                "object_type": "capability",
            }
        )

    marker_present = _migration_marker_present(conn)
    has_objects = bool(actual_objects)
    if not marker_present and not has_objects:
        state = "pending"
    elif any(item["reason"] == "same_name_conflicting_object" for item in findings):
        state = "same_name_conflicting_object"
    elif marker_present and not findings:
        state = "correctly_installed"
    elif not marker_present or len(actual_objects) < len(expected_objects):
        state = "partial_installation"
    else:
        state = "schema_definition_mismatch"

    return {
        "state": state,
        "migration_version": MIGRATION_VERSION,
        "migration_marker_present": marker_present,
        "findings": sorted(findings, key=_finding_key),
        "blocking": state != "correctly_installed",
        "expected_object_count": len(expected_objects),
        "present_expected_object_count": len(set(expected_objects) & set(actual_objects)),
        "expected_objects": [f"{kind}:{name}" for kind, name in sorted(expected_objects)],
        "present_objects": [f"{kind}:{name}" for kind, name in sorted(actual_objects)],
    }


def ownership_table_columns(conn, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_xinfo({quote_identifier(table)})")}


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _capture_manifest(conn) -> dict:
    raw_objects = {
        (row[0], row[1]): {"table": row[2], "sql": row[3]}
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger', 'view')"
        )
        if _is_ownership_object(row[0], row[1], row[2])
        and not _is_installed_forward_compatible_object(conn, row[0], row[1])
    }
    objects = tuple(sorted(raw_objects))
    definitions = {
        key: _normalize_sql(value["sql"])
        for key, value in raw_objects.items()
        if value["sql"] is not None
    }
    tables = {}
    index_details = {}
    for table in OWNERSHIP_TABLES:
        if not _table_exists(conn, table):
            continue
        columns = tuple(
            tuple(row[index] for index in range(7))
            for row in conn.execute(f"PRAGMA table_xinfo({quote_identifier(table)})")
        )
        foreign_keys = tuple(
            sorted(
                tuple(row[index] for index in range(8))
                for row in conn.execute(
                    f"PRAGMA foreign_key_list({quote_identifier(table)})"
                )
            )
        )
        indexes = []
        for row in conn.execute(f"PRAGMA index_list({quote_identifier(table)})"):
            index_name = row[1]
            if _is_installed_forward_compatible_object(conn, "index", index_name):
                continue
            detail = {
                "table": table,
                "unique": row[2],
                "origin": row[3],
                "partial": row[4],
                "columns": tuple(
                    tuple(item[index] for index in range(6))
                    for item in conn.execute(
                        f"PRAGMA index_xinfo({quote_identifier(index_name)})"
                    )
                ),
            }
            index_details[index_name] = detail
            indexes.append((index_name, row[2], row[3], row[4]))
        tables[table] = {
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


def _is_ownership_object(kind: str, name: str, table_name: str) -> bool:
    if name in OWNERSHIP_TABLES or name in OWNERSHIP_INDEXES or name in OWNERSHIP_TRIGGERS:
        return True
    if table_name in OWNERSHIP_TABLES:
        return True
    prefixes = (
        "idx_product_principals_",
        "idx_legacy_owner_aliases_",
        "idx_principal_account_bindings_",
        "idx_ownership_binding_events_",
        "trg_product_principals_",
        "trg_legacy_owner_aliases_",
        "trg_principal_account_bindings_",
        "trg_ownership_binding_events_",
    )
    return kind in {"index", "trigger"} and name.startswith(prefixes)


def _is_installed_forward_compatible_object(conn, kind: str, name: str) -> bool:
    required_marker = FORWARD_COMPATIBLE_OWNERSHIP_OBJECTS.get((kind, name))
    if required_marker is None or not _table_exists(conn, "wahojobs_schema_migrations"):
        return False
    return (
        conn.execute(
            "SELECT 1 FROM wahojobs_schema_migrations WHERE version = ?",
            (required_marker,),
        ).fetchone()
        is not None
    )


def _normalize_sql(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", value.strip().rstrip(";"))


def _manifest_fingerprint(value) -> str:
    canonical = (
        tuple(sorted(value["objects"])),
        tuple(sorted(value["definitions"].items())),
        tuple(
            (
                name,
                tuple((field, details[field]) for field in sorted(details)),
            )
            for name, details in sorted(value["tables"].items())
        ),
        tuple(
            (
                name,
                tuple((field, details[field]) for field in sorted(details)),
            )
            for name, details in sorted(value["index_details"].items())
        ),
    )
    return hashlib.sha256(repr(canonical).encode("utf-8")).hexdigest()


def _migration_marker_present(conn) -> bool:
    if not _table_exists(conn, "wahojobs_schema_migrations"):
        return False
    return (
        conn.execute(
            "SELECT 1 FROM wahojobs_schema_migrations WHERE version = ?",
            (MIGRATION_VERSION,),
        ).fetchone()
        is not None
    )


def _table_exists(conn, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _finding_key(item: dict) -> tuple:
    return tuple(str(item.get(key, "")) for key in sorted(item))
