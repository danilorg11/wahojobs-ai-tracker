import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.account_reconciliation import reconcile_accounts  # noqa: E402
from wahojobs.config import DB_PATH  # noqa: E402
from wahojobs.ownership_reconciliation import reconcile_ownership  # noqa: E402
from wahojobs.ownership_schema import attest_ownership_schema  # noqa: E402
from wahojobs.persistent_profile_schema import (  # noqa: E402
    MIGRATION_PATH,
    MIGRATION_VERSION,
    PROFILE_TABLES,
    attest_persistent_profile_schema,
    iter_sql_statements,
)
from wahojobs.pipeline_reconciliation import reconcile_pipeline_state  # noqa: E402


PREREQUISITE_VERSIONS = (
    "001_pipeline_state",
    "002_accounts_sessions",
    "003_product_principals",
)


class PersistentProfilesMigrationError(RuntimeError):
    pass


def main():
    args = parse_args()
    db_path = Path(args.db).resolve()
    if is_workspace_database_file(db_path) and not args.allow_workspace_db:
        _exit(
            args,
            workspace_database_blocked_result(apply=args.yes),
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
            if (
                not args.allow_workspace_db
                and is_workspace_database_file(opened_database_path(conn))
            ):
                _exit(
                    args,
                    workspace_database_blocked_result(apply=args.yes),
                    error=True,
                )
            classification = classify_database(conn)
            if args.yes:
                result = apply_persistent_profiles_migration(
                    conn, classification=classification
                )
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
    except PersistentProfilesMigrationError as exc:
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
        description="Inspect or explicitly install dormant persistent-profile migration 004."
    )
    parser.add_argument("--db", required=True, help="SQLite database path to inspect.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Explicitly apply migration 004. Without this flag the command is read-only.",
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


def workspace_database_blocked_result(*, apply: bool) -> dict:
    return {
        "database_state": "workspace_database_blocked",
        "mode": "apply" if apply else "inspection",
        "applicable": False,
        "changed": False,
        "reason": "Workspace database access requires --allow-workspace-db after separate review.",
    }


def is_workspace_database_file(candidate, *, workspace_path=None) -> bool:
    """Identify the configured workspace database by path or existing file identity."""
    candidate = Path(candidate)
    workspace = Path(DB_PATH if workspace_path is None else workspace_path)
    candidate_resolved = _resolved_path(candidate)
    workspace_resolved = _resolved_path(workspace)
    if os.path.normcase(str(candidate_resolved)) == os.path.normcase(
        str(workspace_resolved)
    ):
        return True
    if not candidate.exists() or not workspace.exists():
        return False
    try:
        return os.path.samefile(candidate, workspace)
    except (OSError, ValueError):
        try:
            candidate_stat = candidate.stat()
            workspace_stat = workspace.stat()
        except OSError:
            return False
        candidate_identity = (candidate_stat.st_dev, candidate_stat.st_ino)
        workspace_identity = (workspace_stat.st_dev, workspace_stat.st_ino)
        return candidate_stat.st_ino != 0 and candidate_identity == workspace_identity


def opened_database_path(conn) -> Path:
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return Path(row[2])
    return Path()


def _resolved_path(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return Path(os.path.abspath(os.fspath(path)))


def classify_database(conn) -> dict:
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        return _classification(
            "invalid_sqlite", False, f"Database integrity check failed: {integrity}."
        )
    objects = _objects(conn)
    markers = _migration_markers(conn, objects)
    missing = [version for version in PREREQUISITE_VERSIONS if version not in markers]
    if missing:
        return _classification(
            "prerequisite_migration_absent",
            False,
            "Migration 004 requires migrations 001, 002, and 003; missing: "
            + ", ".join(missing),
        )

    pipeline_report = reconcile_pipeline_state(conn)
    accounts_report = reconcile_accounts(conn)
    ownership_attestation = attest_ownership_schema(conn)
    ownership_report = reconcile_ownership(conn)
    if (
        pipeline_report["blocking"]
        or accounts_report["blocking"]
        or ownership_attestation["state"] != "correctly_installed"
        or ownership_report["blocking"]
    ):
        return {
            **_classification(
                "prerequisite_reconciliation_blocking",
                False,
                "Migrations 001, 002, and 003 must attest and reconcile cleanly before migration 004.",
            ),
            "prerequisite_reconciliation": {
                "pipeline": pipeline_report,
                "accounts": accounts_report,
                "ownership_schema": ownership_attestation,
                "ownership": ownership_report,
            },
        }

    attestation = attest_persistent_profile_schema(conn)
    prerequisites = {
        "pipeline": pipeline_report,
        "accounts": accounts_report,
        "ownership_schema": ownership_attestation,
        "ownership": ownership_report,
    }
    if attestation["state"] == "pending":
        return {
            **_classification("pending", True, "Migration 004 is ready to apply."),
            "schema_attestation": attestation,
            "prerequisite_reconciliation": prerequisites,
        }
    if attestation["state"] == "correctly_installed":
        return {
            **_classification(
                "already_migrated", False, "Migration 004 is already complete."
            ),
            "schema_attestation": attestation,
            "profile_table_counts": profile_table_counts(conn),
            "prerequisite_reconciliation": prerequisites,
        }
    state_map = {
        "same_name_conflicting_object": "unexpected_object_conflict",
        "partial_installation": "migration_004_partial_inconsistent",
        "schema_definition_mismatch": "migration_004_schema_definition_mismatch",
    }
    return {
        **_classification(
            state_map.get(
                attestation["state"], "migration_004_schema_definition_mismatch"
            ),
            False,
            "Migration 004 schema does not match the canonical manifest.",
        ),
        "schema_attestation": attestation,
    }


def apply_persistent_profiles_migration(
    conn, *, classification=None, failure_injector=None
):
    if conn.in_transaction:
        raise PersistentProfilesMigrationError(
            "Migration requires an idle, migration-owned connection."
        )
    classification = classification or classify_database(conn)
    if classification["database_state"] == "already_migrated":
        return {
            **classification,
            "mode": "apply",
            "changed": False,
            "migration_version": MIGRATION_VERSION,
            **failure_injection_accounting(),
        }
    if classification["database_state"] != "pending":
        raise PersistentProfilesMigrationError(classification["reason"])

    statements = list(iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8")))
    preserved_tables = tuple(
        sorted(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
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
        attestation = attest_persistent_profile_schema(conn)
        if attestation["state"] != "correctly_installed":
            raise PersistentProfilesMigrationError(
                "Migration 004 schema attestation failed."
            )
        inject_failure(failure_injector, "after_schema_attestation")
        inject_failure(failure_injector, "before_empty_state_validation")
        profile_counts = profile_table_counts(conn)
        if any(profile_counts.values()):
            raise PersistentProfilesMigrationError(
                "Migration 004 installation created unexpected profile rows."
            )
        inject_failure(failure_injector, "after_empty_state_validation")
        inject_failure(failure_injector, "before_integrity_check")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise PersistentProfilesMigrationError(
                "Integrity check failed after installing migration 004."
            )
        inject_failure(failure_injector, "after_integrity_check")
        inject_failure(failure_injector, "before_foreign_key_check")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise PersistentProfilesMigrationError(
                "Foreign-key check failed after installing migration 004."
            )
        inject_failure(failure_injector, "after_foreign_key_check")
        inject_failure(failure_injector, "before_preserved_count_check")
        preserved_after = {table: table_count(conn, table) for table in preserved_tables}
        if preserved_after != preserved_before:
            raise PersistentProfilesMigrationError(
                "Migration 004 changed pre-existing business row counts."
            )
        inject_failure(failure_injector, "after_preserved_count_check")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "database_state": "migrated",
        "applicable": False,
        "reason": "Migration 004 applied successfully with no persistent profiles created.",
        "mode": "apply",
        "changed": True,
        "migration_version": MIGRATION_VERSION,
        "schema_attestation": attestation,
        "profile_table_counts": profile_counts,
        "preserved_table_counts": preserved_after,
        "statement_count": len(statements),
        **failure_injection_accounting(len(statements)),
    }


def profile_table_counts(conn):
    return {
        table: table_count(conn, table) if _table_exists(conn, table) else 0
        for table in PROFILE_TABLES
    }


def table_count(conn, table_name):
    return conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]


def inject_failure(callback, point):
    if callback is not None:
        callback(point)


def failure_injection_state_map(statement_count=None) -> dict[str, str]:
    statement_count = statement_count or len(
        list(iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8")))
    )
    checkpoints = {"before_first_ddl": "ddl_0"}
    for index in range(1, statement_count + 1):
        checkpoints[f"before_statement_{index}"] = f"ddl_{index - 1}"
        checkpoints[f"after_statement_{index}"] = f"ddl_{index}"
    checkpoints["after_all_ddl"] = f"ddl_{statement_count}"
    checkpoints["before_marker_write"] = f"ddl_{statement_count}"
    for point in (
        "after_marker_write",
        "before_schema_attestation",
        "after_schema_attestation",
        "before_empty_state_validation",
        "after_empty_state_validation",
        "before_integrity_check",
        "after_integrity_check",
        "before_foreign_key_check",
        "after_foreign_key_check",
        "before_preserved_count_check",
        "after_preserved_count_check",
    ):
        checkpoints[point] = "marker_written"
    return checkpoints


def failure_injection_points(statement_count=None) -> tuple[str, ...]:
    return tuple(failure_injection_state_map(statement_count))


def failure_injection_accounting(statement_count=None) -> dict:
    checkpoints = failure_injection_state_map(statement_count)
    return {
        "fault_injection_hook_count": len(checkpoints),
        "durable_state_checkpoint_count": len(set(checkpoints.values())),
    }


def _objects(conn):
    return {
        (row[0], row[1])
        for row in conn.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger', 'view')"
        )
    }


def _migration_markers(conn, objects):
    if ("table", "wahojobs_schema_migrations") not in objects:
        return set()
    return {row[0] for row in conn.execute("SELECT version FROM wahojobs_schema_migrations")}


def _classification(status, applicable, reason):
    return {"database_state": status, "applicable": applicable, "reason": reason}


def _table_exists(conn, name):
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _exit(args, result, *, error):
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Persistent Product Profiles Migration 004")
        print("=========================================")
        print(f"Mode: {result.get('mode', 'inspection')}")
        print(f"Database state: {result.get('database_state', 'unknown')}")
        print(f"Applicable: {'yes' if result.get('applicable') else 'no'}")
        print(f"Changed: {'yes' if result.get('changed') else 'no'}")
        print(f"Reason: {result.get('reason', '')}")
    raise SystemExit(1 if error else 0)


if __name__ == "__main__":
    main()
