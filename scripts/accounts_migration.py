import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.account_reconciliation import (  # noqa: E402
    EXPECTED_ACCOUNT_OBJECTS,
    MIGRATION_VERSION,
    reconcile_accounts,
)
from wahojobs.config import DB_PATH  # noqa: E402


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "wahojobs"
    / "db"
    / "migrations"
    / f"{MIGRATION_VERSION}.sql"
)
MIGRATION_001_VERSION = "001_pipeline_state"
MIGRATION_001_REQUIRED_OBJECTS = {
    ("table", "wahojobs_schema_migrations"),
    ("table", "user_pipeline_state"),
    ("table", "user_pipeline_transitions"),
    ("trigger", "trg_user_pipeline_transitions_no_update"),
    ("trigger", "trg_user_pipeline_transitions_no_delete"),
}
BASE_TABLES = {
    "companies",
    "jobs",
    "canonical_opportunities",
    "crawl_runs",
    "user_profiles",
    "user_pipeline_items",
    "applicant_status_updates",
}
PRESERVED_TABLES = tuple(sorted(BASE_TABLES | {"user_pipeline_state", "user_pipeline_transitions"}))


class AccountsMigrationError(RuntimeError):
    pass


def main():
    args = parse_args()
    db_path = Path(args.db).resolve()
    if db_path == Path(DB_PATH).resolve() and not args.allow_workspace_db:
        _exit(
            args,
            {
                "database_state": "workspace_database_blocked",
                "mode": "apply" if args.yes else "inspection",
                "applicable": False,
                "changed": False,
                "reason": (
                    "Workspace database access requires --allow-workspace-db after a separately reviewed authorization."
                ),
            },
            error=True,
        )
    if not db_path.exists():
        _exit(
            args,
            {
                "database_state": "nonexistent",
                "mode": "apply" if args.yes else "inspection",
                "applicable": False,
                "changed": False,
                "reason": "Database path does not exist; the account migration never creates the base schema.",
            },
            error=bool(args.yes),
        )

    read_only = not args.yes
    try:
        conn = connect(db_path, read_only=read_only)
        try:
            classification = classify_database(conn)
            if args.yes:
                result = apply_accounts_migration(conn, classification=classification)
            else:
                result = {
                    **classification,
                    "mode": "inspection",
                    "changed": False,
                    "migration_version": MIGRATION_VERSION,
                    "reconciliation": None,
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
    except AccountsMigrationError as exc:
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
        description="Inspect or explicitly install account/session migration 002."
    )
    parser.add_argument("--db", required=True, help="SQLite database path to inspect.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Explicitly apply migration 002. Without this flag the command is read-only.",
    )
    parser.add_argument(
        "--allow-workspace-db",
        action="store_true",
        help="Allow access to the configured workspace database after separate review.",
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
    objects = {
        (row["type"], row["name"])
        for row in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'index', 'trigger')"
        )
    }
    user_tables = {
        name for kind, name in objects if kind == "table" and not name.startswith("sqlite_")
    }
    if not user_tables:
        return _classification(
            "empty", False, "The SQLite file is empty; initialize and migrate the base schema separately."
        )
    if not BASE_TABLES.issubset(user_tables):
        missing = sorted(BASE_TABLES - user_tables)
        return _classification(
            "legacy_or_base_inconsistent",
            False,
            "The base schema is incomplete; missing: " + ", ".join(missing),
        )

    migration_table = ("table", "wahojobs_schema_migrations") in objects
    marker_001 = False
    marker_002 = False
    if migration_table:
        marker_001 = (
            conn.execute(
                "SELECT 1 FROM wahojobs_schema_migrations WHERE version = ?",
                (MIGRATION_001_VERSION,),
            ).fetchone()
            is not None
        )
        marker_002 = (
            conn.execute(
                "SELECT 1 FROM wahojobs_schema_migrations WHERE version = ?",
                (MIGRATION_VERSION,),
            ).fetchone()
            is not None
        )
    prerequisite_present = MIGRATION_001_REQUIRED_OBJECTS & objects
    if not marker_001:
        if prerequisite_present and prerequisite_present != MIGRATION_001_REQUIRED_OBJECTS:
            return _classification(
                "migration_001_inconsistent",
                False,
                "Migration 001 objects are partial or its marker is absent.",
            )
        return _classification(
            "migration_001_absent",
            False,
            "Migration 001 must be installed before account/session migration 002.",
        )
    if not MIGRATION_001_REQUIRED_OBJECTS.issubset(objects):
        missing = sorted(
            f"{kind}:{name}" for kind, name in MIGRATION_001_REQUIRED_OBJECTS - objects
        )
        return _classification(
            "migration_001_inconsistent",
            False,
            "Migration 001 marker exists but required objects are missing: " + ", ".join(missing),
        )

    present_accounts = EXPECTED_ACCOUNT_OBJECTS & objects
    if not present_accounts and not marker_002:
        return _classification("migration_001_present", True, "Migration 002 is ready to apply.")
    if present_accounts == EXPECTED_ACCOUNT_OBJECTS and marker_002:
        report = reconcile_accounts(conn)
        if report["blocking"]:
            return {
                **_classification(
                    "migration_002_inconsistent",
                    False,
                    "Migration 002 exists but account reconciliation reports blocking drift.",
                ),
                "reconciliation": report,
            }
        return {
            **_classification("already_migrated", False, "Migration 002 is already complete."),
            "reconciliation": report,
        }
    missing = sorted(f"{kind}:{name}" for kind, name in EXPECTED_ACCOUNT_OBJECTS - present_accounts)
    return _classification(
        "migration_002_partial_inconsistent",
        False,
        "Partial migration 002 detected; missing objects: " + (", ".join(missing) or "none"),
    )


def _classification(status, applicable, reason):
    return {
        "database_state": status,
        "applicable": applicable,
        "reason": reason,
    }


def apply_accounts_migration(conn, *, classification=None, failure_injector=None):
    if conn.in_transaction:
        raise AccountsMigrationError("Migration requires an idle, migration-owned connection.")
    classification = classification or classify_database(conn)
    if classification["database_state"] == "already_migrated":
        return {
            **classification,
            "mode": "apply",
            "changed": False,
            "migration_version": MIGRATION_VERSION,
        }
    if classification["database_state"] != "migration_001_present":
        raise AccountsMigrationError(classification["reason"])

    statements = list(iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8")))
    preserved_before = {table: table_count(conn, table) for table in PRESERVED_TABLES}
    conn.execute("BEGIN IMMEDIATE")
    try:
        inject_failure(failure_injector, "before_first_ddl")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AccountsMigrationError("Integrity check failed inside the migration transaction.")
        for index, statement in enumerate(statements, start=1):
            inject_failure(failure_injector, f"before_statement_{index}")
            if statement.lstrip().upper().startswith("CREATE TRIGGER"):
                inject_failure(failure_injector, "before_trigger_install")
            conn.execute(statement)
            inject_failure(failure_injector, f"after_statement_{index}")
            if index == 1:
                inject_failure(failure_injector, "after_first_ddl")
        inject_failure(failure_injector, "after_all_ddl")
        inject_failure(failure_injector, "before_marker_write")
        conn.execute(
            "INSERT INTO wahojobs_schema_migrations (version) VALUES (?)",
            (MIGRATION_VERSION,),
        )
        inject_failure(failure_injector, "after_marker_write")
        inject_failure(failure_injector, "before_reconciliation")
        report = reconcile_accounts(conn)
        if report["blocking"]:
            raise AccountsMigrationError(
                "Account reconciliation failed: " + ", ".join(report["blocking_reasons"])
            )
        inject_failure(failure_injector, "after_reconciliation_before_commit")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise AccountsMigrationError("Integrity check failed after installing migration 002.")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise AccountsMigrationError("Foreign-key check failed after installing migration 002.")
        preserved_after = {table: table_count(conn, table) for table in PRESERVED_TABLES}
        if preserved_after != preserved_before:
            raise AccountsMigrationError("Migration 002 changed pre-existing business row counts.")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "database_state": "migrated",
        "applicable": False,
        "reason": "Migration 002 applied successfully.",
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
        raise AccountsMigrationError("Migration SQL ends with an incomplete statement.")


def table_count(conn, table_name):
    return conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]


def inject_failure(callback, point):
    if callback is not None:
        callback(point)


def _exit(args, result, *, error):
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Accounts and Sessions Migration 002")
        print("===================================")
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
