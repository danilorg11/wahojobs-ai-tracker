from __future__ import annotations

import re
import sqlite3
from functools import lru_cache
from pathlib import Path


MIGRATION_VERSION = "004_persistent_product_profiles"
MIGRATION_PATH = (
    Path(__file__).resolve().parent
    / "db"
    / "migrations"
    / f"{MIGRATION_VERSION}.sql"
)

PROFILE_TABLES = (
    "product_profiles",
    "product_profile_revisions",
    "product_profile_sources",
)
PROFILE_VIEWS = ("current_product_profiles",)
PROFILE_INDEXES = (
    "uq_product_principals_profile_environment",
    "idx_product_profiles_environment",
    "idx_product_profile_revisions_profile_history",
    "idx_product_profile_revisions_principal_history",
    "idx_product_profile_revisions_lifecycle",
    "idx_product_profile_sources_revision",
    "idx_product_profile_sources_profile",
)
PROFILE_TRIGGERS = (
    "trg_product_profiles_insert_guard",
    "trg_product_profiles_no_update",
    "trg_product_profiles_delete_guard",
    "trg_product_profile_sources_insert_guard",
    "trg_product_profile_sources_no_update",
    "trg_product_profile_sources_delete_guard",
    "trg_product_profile_revisions_insert_guard",
    "trg_product_profile_revisions_no_update",
    "trg_product_profile_revisions_delete_guard",
)
IDENTIFIER_PREFIXES = {
    "profile": "prf_",
    "revision": "pvr_",
    "source": "pfs_",
}
_IDENTIFIER_PATTERN = re.compile(r"^[0-9a-f]{32}$")
STRUCTURED_PROFILE_KEY_PATTERN = re.compile(r"^[a-z0-9_]{1,128}$")
STRUCTURED_PROFILE_DENIED_KEY_FORMS = frozenset(
    {
        "originaltext",
        "rawtext",
        "rawinput",
        "rawcontent",
        "aboutyou",
        "aboutyoutext",
        "sourcetext",
        "sourcecontent",
        "evidence",
        "evidencesnippet",
        "evidencesnippets",
        "resume",
        "resumecontent",
        "cv",
        "cvcontent",
        "applicationcontent",
        "rawapplicationcontent",
        "accountid",
        "userid",
        "principalid",
        "providerid",
        "providersubject",
        "sessionid",
        "sessiontoken",
        "token",
        "cookie",
        "authorization",
        "authorizationheader",
        "authenticationheader",
        "password",
        "secret",
        "credential",
        "bearer",
        "csrf",
        "csrfmaterial",
        "invitationhmac",
        "rawclaims",
        "email",
        "oauthsubject",
    }
)


def validate_persistent_profile_identifier(value: str, kind: str) -> str:
    """Validate B2 identifier syntax without claiming historical randomness."""
    prefix = IDENTIFIER_PREFIXES.get(kind)
    if prefix is None:
        raise ValueError("Unsupported persistent-profile identifier kind.")
    if type(value) is not str or not value.startswith(prefix):
        raise ValueError(f"Invalid {kind} identifier.")
    payload = value[len(prefix) :]
    if not _IDENTIFIER_PATTERN.fullmatch(payload):
        raise ValueError(f"Invalid {kind} identifier.")
    if len(set(payload)) == 1:
        raise ValueError(f"Invalid {kind} identifier.")
    return value


def validate_structured_profile_key(value: str) -> str:
    """Validate the durable canonical-profile object-key privacy contract."""
    if type(value) is not str or not STRUCTURED_PROFILE_KEY_PATTERN.fullmatch(value):
        raise ValueError("Invalid structured-profile object key.")
    if value.replace("_", "") in STRUCTURED_PROFILE_DENIED_KEY_FORMS:
        raise ValueError("Prohibited structured-profile object key.")
    return value


@lru_cache(maxsize=1)
def expected_persistent_profile_manifest() -> dict:
    """Build the canonical Migration-004 manifest in a disposable database."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        _install_manifest_prerequisites(conn)
        conn.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))
        return _capture_manifest(conn)
    finally:
        conn.close()


def attest_persistent_profile_schema(conn) -> dict:
    """Compare installed Migration-004 objects with the canonical manifest."""
    if _forward_migration_marker_present(conn):
        from wahojobs.persistent_profile_canonical_v2_schema import (
            attest_persistent_profile_canonical_v2_schema,
        )

        forward = attest_persistent_profile_canonical_v2_schema(conn)
        if forward["state"] == "correctly_installed":
            return {
                "state": "correctly_installed",
                "migration_version": MIGRATION_VERSION,
                "migration_marker_present": _migration_marker_present(conn),
                "findings": [],
                "blocking": False,
                "superseded_by_migration": forward["migration_version"],
                "forward_schema_attestation": forward,
                "expected_object_count": forward["expected_object_count"],
                "present_expected_object_count": forward["present_expected_object_count"],
                "expected_objects": forward["expected_objects"],
                "present_objects": forward["present_objects"],
                "expected_statement_count": migration_statement_count(),
            }
        return {
            "state": "forward_schema_invalid",
            "migration_version": MIGRATION_VERSION,
            "migration_marker_present": _migration_marker_present(conn),
            "findings": forward["findings"],
            "blocking": True,
            "forward_schema_attestation": forward,
            "expected_object_count": forward["expected_object_count"],
            "present_expected_object_count": forward["present_expected_object_count"],
            "expected_objects": forward["expected_objects"],
            "present_objects": forward["present_objects"],
            "expected_statement_count": migration_statement_count(),
        }
    expected = expected_persistent_profile_manifest()
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
                {
                    "reason": "unexpected_persistent_profile_object",
                    "object": name,
                    "actual_type": kind,
                }
            )

    for key in sorted(set(expected["definitions"]) & set(actual["definitions"])):
        if expected["definitions"][key] != actual["definitions"][key]:
            findings.append(
                {
                    "reason": "schema_definition_mismatch",
                    "object": key[1],
                    "object_type": key[0],
                }
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
                findings.append({"reason": reason, "table": table})

    for index_name in sorted(
        set(expected["index_details"]) & set(actual["index_details"])
    ):
        if expected["index_details"][index_name] != actual["index_details"][index_name]:
            findings.append(
                {"reason": "index_definition_mismatch", "index": index_name}
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
        "expected_statement_count": migration_statement_count(),
    }


def persistent_profile_table_columns(conn, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_xinfo({_quote(table)})")}


@lru_cache(maxsize=1)
def migration_statement_count() -> int:
    return sum(1 for _ in iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8")))


def iter_sql_statements(sql_text: str):
    buffer = []
    for line in sql_text.splitlines(keepends=True):
        buffer.append(line)
        candidate = "".join(buffer).strip()
        if candidate and sqlite3.complete_statement(candidate):
            yield candidate
            buffer = []
    remainder = "".join(buffer).strip()
    if remainder:
        raise ValueError("Migration 004 SQL ends with an incomplete statement.")


def _install_manifest_prerequisites(conn):
    conn.executescript(
        """
        CREATE TABLE users (
          user_id TEXT PRIMARY KEY,
          lifecycle_status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE product_principals (
          principal_id TEXT PRIMARY KEY,
          environment_namespace TEXT NOT NULL,
          principal_type TEXT NOT NULL,
          lifecycle_status TEXT NOT NULL,
          claim_policy TEXT NOT NULL,
          exclusive_account_binding INTEGER NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE principal_account_bindings (
          binding_id TEXT PRIMARY KEY,
          principal_id TEXT NOT NULL,
          user_id TEXT NOT NULL,
          environment_namespace TEXT NOT NULL,
          binding_role TEXT NOT NULL,
          binding_status TEXT NOT NULL
        );
        CREATE TABLE wahojobs_schema_migrations (
          version TEXT PRIMARY KEY,
          applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )


def _capture_manifest(conn) -> dict:
    raw_objects = {
        (row[0], row[1]): {"table": row[2], "sql": row[3]}
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger', 'view')"
        )
        if _is_profile_object(row[0], row[1], row[2])
    }
    objects = tuple(sorted(raw_objects))
    definitions = {
        key: _normalize_sql(value["sql"])
        for key, value in raw_objects.items()
        if value["sql"] is not None
    }
    tables = {}
    index_details = {}
    for table in PROFILE_TABLES:
        if not _table_exists(conn, table):
            continue
        columns = tuple(
            tuple(row[index] for index in range(7))
            for row in conn.execute(f"PRAGMA table_xinfo({_quote(table)})")
        )
        foreign_keys = tuple(
            sorted(
                tuple(row[index] for index in range(8))
                for row in conn.execute(f"PRAGMA foreign_key_list({_quote(table)})")
            )
        )
        indexes = []
        for row in conn.execute(f"PRAGMA index_list({_quote(table)})"):
            index_name = row[1]
            detail = {
                "table": table,
                "unique": row[2],
                "origin": row[3],
                "partial": row[4],
                "columns": tuple(
                    tuple(item[index] for index in range(6))
                    for item in conn.execute(f"PRAGMA index_xinfo({_quote(index_name)})")
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


def _is_profile_object(kind: str, name: str, table_name: str) -> bool:
    if (
        name in PROFILE_TABLES
        or name in PROFILE_VIEWS
        or name in PROFILE_INDEXES
        or name in PROFILE_TRIGGERS
    ):
        return True
    if table_name in PROFILE_TABLES:
        return True
    if kind in {"table", "view"} and (
        name.startswith("product_profile")
        or name.startswith("current_product_profile")
    ):
        return True
    prefixes = (
        "idx_product_profiles_",
        "idx_product_profile_revisions_",
        "idx_product_profile_sources_",
        "uq_product_principals_profile_",
        "trg_product_profiles_",
        "trg_product_profile_revisions_",
        "trg_product_profile_sources_",
    )
    return kind in {"index", "trigger"} and name.startswith(prefixes)


def _normalize_sql(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", value.strip().rstrip(";"))


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


def _forward_migration_marker_present(conn) -> bool:
    if not _table_exists(conn, "wahojobs_schema_migrations"):
        return False
    return (
        conn.execute(
            "SELECT 1 FROM wahojobs_schema_migrations WHERE version = ?",
            ("005_persistent_profile_canonical_v2",),
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


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _finding_key(item: dict) -> tuple:
    return tuple(str(item.get(key, "")) for key in sorted(item))
