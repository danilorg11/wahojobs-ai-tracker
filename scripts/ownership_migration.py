import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.account_reconciliation import reconcile_accounts  # noqa: E402
from wahojobs.config import DB_PATH  # noqa: E402
from wahojobs.ownership import (  # noqa: E402
    MIGRATION_VERSION,
)
from wahojobs.ownership_reconciliation import reconcile_ownership  # noqa: E402
from wahojobs.ownership_schema import attest_ownership_schema  # noqa: E402
from wahojobs.pipeline_reconciliation import reconcile_pipeline_state  # noqa: E402


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "wahojobs"
    / "db"
    / "migrations"
    / f"{MIGRATION_VERSION}.sql"
)
PREREQUISITE_VERSIONS = ("001_pipeline_state", "002_accounts_sessions")
class OwnershipMigrationError(RuntimeError):
    pass


def main():
    args = parse_args()
    db_path = Path(args.db).resolve()
    workspace_path = Path(DB_PATH).resolve()
    if db_path == workspace_path and not args.allow_workspace_db:
        _exit(
            args,
            {
                "database_state": "workspace_database_blocked",
                "mode": "apply" if args.yes else "inspection",
                "applicable": False,
                "changed": False,
                "reason": "Workspace database access requires --allow-workspace-db after separate review.",
            },
            error=True,
        )
    if not db_path.is_file():
        _exit(
            args,
            {
                "database_state": "nonexistent",
                "mode": "apply" if args.yes else "inspection",
                "applicable": False,
                "changed": False,
                "reason": "Database path does not exist; this migration never creates a base database.",
            },
            error=bool(args.yes),
        )

    try:
        conn = connect(db_path, read_only=not args.yes)
        try:
            classification = classify_database(conn)
            if args.yes:
                result = apply_ownership_migration(conn, classification=classification)
            else:
                result = {
                    **classification,
                    "mode": "inspection",
                    "changed": False,
                    "migration_version": MIGRATION_VERSION,
                }
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        _exit(
            args,
            {
                "database_state": "invalid_sqlite",
                "mode": "apply" if args.yes else "inspection",
                "applicable": False,
                "changed": False,
                "reason": f"Invalid or unreadable SQLite database: {type(exc).__name__}.",
            },
            error=True,
        )
    except OwnershipMigrationError as exc:
        _exit(
            args,
            {
                **locals().get("classification", {}),
                "mode": "apply",
                "changed": False,
                "migration_version": MIGRATION_VERSION,
                "reason": str(exc),
            },
            error=True,
        )
    _exit(args, result, error=False)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Inspect or explicitly install dormant product-principal migration 003."
    )
    parser.add_argument("--db", required=True, help="SQLite database path to inspect.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Explicitly apply migration 003. Without this flag the command is read-only.",
    )
    parser.add_argument(
        "--allow-workspace-db",
        action="store_true",
        help="Allow workspace database access after a separately reviewed authorization.",
    )
    parser.add_argument("--json", action="store_true", help="Emit stable JSON output.")
    return parser.parse_args()


def connect(path: Path, *, read_only: bool):
    if read_only:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def classify_database(conn) -> dict:
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        return _classification(
            "invalid_sqlite", False, f"Database integrity check failed: {integrity}."
        )
    objects = _objects(conn)
    markers = _migration_markers(conn, objects)
    missing_prerequisites = [item for item in PREREQUISITE_VERSIONS if item not in markers]
    if missing_prerequisites:
        return _classification(
            "prerequisite_migration_absent",
            False,
            "Migration 003 requires migrations 001 and 002; missing: "
            + ", ".join(missing_prerequisites),
        )
    pipeline_report = reconcile_pipeline_state(conn)
    accounts_report = reconcile_accounts(conn)
    if pipeline_report["blocking"] or accounts_report["blocking"]:
        return {
            **_classification(
                "prerequisite_reconciliation_blocking",
                False,
                "Migrations 001 and 002 must reconcile cleanly before migration 003.",
            ),
            "prerequisite_reconciliation": {
                "pipeline": pipeline_report,
                "accounts": accounts_report,
            },
        }

    attestation = attest_ownership_schema(conn)
    if attestation["state"] == "pending":
        return {
            **_classification("pending", True, "Migration 003 is ready to apply."),
            "schema_attestation": attestation,
            "prerequisite_reconciliation": {
                "pipeline": pipeline_report,
                "accounts": accounts_report,
            },
        }
    if attestation["state"] == "correctly_installed":
        report = reconcile_ownership(conn)
        if report["blocking"]:
            return {
                **_classification(
                    "migration_003_inconsistent",
                    False,
                    "Migration 003 exists but ownership reconciliation reports blocking drift.",
                ),
                "schema_attestation": attestation,
                "reconciliation": report,
            }
        return {
            **_classification("already_migrated", False, "Migration 003 is already complete."),
            "schema_attestation": attestation,
            "reconciliation": report,
        }
    state_map = {
        "same_name_conflicting_object": "unexpected_object_conflict",
        "partial_installation": "migration_003_partial_inconsistent",
        "schema_definition_mismatch": "migration_003_schema_definition_mismatch",
    }
    return {
        **_classification(
            state_map.get(attestation["state"], "migration_003_schema_definition_mismatch"),
            False,
            "Migration 003 schema does not match the canonical manifest.",
        ),
        "schema_attestation": attestation,
    }


def apply_ownership_migration(conn, *, classification=None, failure_injector=None):
    if conn.in_transaction:
        raise OwnershipMigrationError("Migration requires an idle, migration-owned connection.")
    classification = classification or classify_database(conn)
    if classification["database_state"] == "already_migrated":
        return {
            **classification,
            "mode": "apply",
            "changed": False,
            "migration_version": MIGRATION_VERSION,
        }
    if classification["database_state"] != "pending":
        raise OwnershipMigrationError(classification["reason"])

    statements = list(iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8")))
    preserved_tables = tuple(
        sorted(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name <> 'wahojobs_schema_migrations'"
            )
        )
    )
    preserved_before = {table: table_count(conn, table) for table in preserved_tables}
    conn.execute("BEGIN IMMEDIATE")
    try:
        inject_failure(failure_injector, "before_first_ddl")
        for index, statement in enumerate(statements, start=1):
            inject_failure(failure_injector, f"before_statement_{index}")
            conn.execute(statement)
            inject_failure(failure_injector, f"after_statement_{index}")
        inject_failure(failure_injector, "after_all_ddl")
        inject_failure(failure_injector, "before_marker_write")
        conn.execute(
            "INSERT INTO wahojobs_schema_migrations (version) VALUES (?)",
            (MIGRATION_VERSION,),
        )
        inject_failure(failure_injector, "after_marker_write")
        inject_failure(failure_injector, "before_schema_attestation")
        attestation = attest_ownership_schema(conn)
        if attestation["state"] != "correctly_installed":
            raise OwnershipMigrationError("Migration 003 schema attestation failed.")
        inject_failure(failure_injector, "after_schema_attestation")
        inject_failure(failure_injector, "before_reconciliation")
        report = reconcile_ownership(conn)
        if report["blocking"]:
            raise OwnershipMigrationError(
                "Ownership reconciliation failed: " + ", ".join(report["blocking_reasons"])
            )
        inject_failure(failure_injector, "after_reconciliation_before_commit")
        inject_failure(failure_injector, "before_integrity_check")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise OwnershipMigrationError("Integrity check failed after installing migration 003.")
        inject_failure(failure_injector, "after_integrity_check")
        inject_failure(failure_injector, "before_foreign_key_check")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise OwnershipMigrationError("Foreign-key check failed after installing migration 003.")
        inject_failure(failure_injector, "after_foreign_key_check")
        inject_failure(failure_injector, "before_preserved_count_check")
        preserved_after = {table: table_count(conn, table) for table in preserved_tables}
        if preserved_after != preserved_before:
            raise OwnershipMigrationError("Migration 003 changed pre-existing business row counts.")
        inject_failure(failure_injector, "after_preserved_count_check")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "database_state": "migrated",
        "applicable": False,
        "reason": "Migration 003 applied successfully with no principals or bindings created.",
        "mode": "apply",
        "changed": True,
        "migration_version": MIGRATION_VERSION,
        "reconciliation": report,
        "preserved_table_counts": preserved_after,
    }


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
        raise OwnershipMigrationError("Migration SQL ends with an incomplete statement.")


def _objects(conn):
    return {
        (row["type"], row["name"])
        for row in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'index', 'trigger', 'view')"
        )
    }


def _migration_markers(conn, objects):
    if ("table", "wahojobs_schema_migrations") not in objects:
        return set()
    return {row[0] for row in conn.execute("SELECT version FROM wahojobs_schema_migrations")}


def _classification(status, applicable, reason):
    return {"database_state": status, "applicable": applicable, "reason": reason}


def table_count(conn, table_name):
    return conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]


def inject_failure(callback, point):
    if callback is not None:
        callback(point)


def _exit(args, result, *, error):
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Product Principal Ownership Migration 003")
        print("=========================================")
        print(f"Mode: {result.get('mode', 'inspection')}")
        print(f"Database state: {result.get('database_state', 'unknown')}")
        print(f"Applicable: {'yes' if result.get('applicable') else 'no'}")
        print(f"Changed: {'yes' if result.get('changed') else 'no'}")
        print(f"Reason: {result.get('reason', '')}")
        report = result.get("reconciliation")
        if report is not None:
            print(f"Reconciliation: {'blocking' if report['blocking'] else 'clean'}")
    raise SystemExit(1 if error else 0)


if __name__ == "__main__":
    main()
