import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.config import DB_PATH
from wahojobs.pipeline_state import backfill_legacy_pipeline_state


MIGRATION_VERSION = "001_pipeline_state"
MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "wahojobs"
    / "db"
    / "migrations"
    / f"{MIGRATION_VERSION}.sql"
)
EXPECTED_MIGRATION_OBJECTS = {
    ("index", "idx_user_pipeline_items_pipeline_profile"),
    ("table", "user_pipeline_state"),
    ("table", "user_pipeline_transitions"),
    ("index", "idx_user_pipeline_transitions_pipeline_occurred"),
    ("index", "idx_user_pipeline_transitions_profile_occurred"),
    ("index", "idx_user_pipeline_transitions_undo"),
    ("index", "idx_user_pipeline_transitions_correction"),
    ("index", "idx_user_pipeline_transitions_occurred"),
    ("table", "wahojobs_schema_migrations"),
    ("trigger", "trg_user_pipeline_transitions_no_update"),
    ("trigger", "trg_user_pipeline_transitions_no_delete"),
}


class MigrationError(RuntimeError):
    pass


def main():
    args = parse_args()
    db_path = Path(args.db).resolve()
    workspace_db = Path(DB_PATH).resolve()
    if db_path == workspace_db and not args.allow_workspace_db:
        raise SystemExit(
            "Refusing to access the workspace database. Use a temporary copy, or pass "
            "--allow-workspace-db only during an explicitly reviewed production migration."
        )
    if not db_path.exists():
        raise SystemExit(f"Database does not exist; initialize the base schema first: {db_path}")

    dry_run = args.dry_run or not args.yes
    try:
        conn = connect(db_path, read_only=dry_run)
        try:
            before_integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if before_integrity != "ok":
                raise MigrationError(
                    f"Database integrity check failed before migration: {before_integrity}"
                )
            classification = classify_database(conn)
            if classification["status"] == "partial_inconsistent":
                raise MigrationError(classification["reason"])
            if classification["status"] == "uninitialized":
                summary = empty_summary(dry_run=dry_run)
            elif dry_run:
                summary = backfill_legacy_pipeline_state(conn, dry_run=True)
            else:
                summary = apply_pipeline_state_migration(conn, classification=classification)
            after_integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            projection_count = table_count(conn, "user_pipeline_state")
            transition_count = table_count(conn, "user_pipeline_transitions")
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise SystemExit(f"Invalid or unreadable SQLite database: {db_path}: {exc}") from None
    except MigrationError as exc:
        raise SystemExit(f"Pipeline-state migration refused: {exc}") from None

    print("Pipeline State Legacy Migration")
    print("===============================")
    print(f"Database: {db_path}")
    print(f"Mode: {'dry-run' if dry_run else 'apply'}")
    print(f"Status: {classification['status']}")
    print(f"Applicable: {'yes' if classification['applicable'] else 'no'}")
    print(f"Planned rows: {summary['planned']}")
    print(f"Migrated rows: {summary['migrated']}")
    print(f"Already initialized: {summary['already_initialized']}")
    print(f"Malformed reminder dates: {summary['malformed_reminders']}")
    print("Classifications: " + json.dumps(summary["classifications"], sort_keys=True))
    print(f"Projection rows after command: {projection_count}")
    print(f"Transition rows after command: {transition_count}")
    print(f"Integrity before/after: {before_integrity}/{after_integrity}")
    if dry_run:
        print("No schema objects, projection rows, or transition rows were written.")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Backfill normalized pipeline state and legacy baseline transitions."
    )
    parser.add_argument("--db", required=True, help="Path to a temporary or reviewed SQLite database.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect pending rows without installing schema objects or writing state.",
    )
    mode.add_argument(
        "--yes",
        action="store_true",
        help="Install the versioned migration and apply the backfill. Without this flag the command is read-only.",
    )
    parser.add_argument(
        "--allow-workspace-db",
        action="store_true",
        help="Allow the configured workspace database. Requires a separately reviewed migration run.",
    )
    return parser.parse_args()


def connect(path: Path, *, read_only: bool):
    if read_only:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def classify_database(conn) -> dict:
    objects = {
        (row["type"], row["name"])
        for row in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'index', 'trigger')"
        )
    }
    base_exists = ("table", "user_pipeline_items") in objects
    present = EXPECTED_MIGRATION_OBJECTS & objects
    if not base_exists:
        if present:
            return {
                "status": "partial_inconsistent",
                "applicable": False,
                "reason": "Pipeline migration objects exist without the legacy user_pipeline_items table.",
            }
        return {
            "status": "uninitialized",
            "applicable": False,
            "reason": "Initialize the legacy/base schema before running this migration.",
        }
    if not present:
        return {"status": "legacy", "applicable": True, "reason": ""}
    if present != EXPECTED_MIGRATION_OBJECTS:
        missing = sorted(name for kind, name in EXPECTED_MIGRATION_OBJECTS - present)
        extra_present = sorted(name for kind, name in present)
        return {
            "status": "partial_inconsistent",
            "applicable": False,
            "reason": (
                "Partial pipeline-state schema detected. Missing expected objects: "
                f"{', '.join(missing) or '-'}; present migration objects: "
                f"{', '.join(extra_present) or '-'}. Restore or review the database before retrying."
            ),
        }
    marker = conn.execute(
        "SELECT 1 FROM wahojobs_schema_migrations WHERE version = ?",
        (MIGRATION_VERSION,),
    ).fetchone()
    if marker is None:
        return {
            "status": "partial_inconsistent",
            "applicable": False,
            "reason": "Complete pipeline-state objects exist without the migration marker.",
        }
    unknown_markers = conn.execute(
        "SELECT version FROM wahojobs_schema_migrations WHERE version <> ? ORDER BY version",
        (MIGRATION_VERSION,),
    ).fetchall()
    if unknown_markers:
        return {
            "status": "partial_inconsistent",
            "applicable": False,
            "reason": "Unexpected migration marker(s): "
            + ", ".join(row["version"] for row in unknown_markers),
        }
    pending_rows = len(_planned_items(conn))
    if pending_rows:
        return {
            "status": "partial_inconsistent",
            "applicable": False,
            "reason": (
                f"Migration marker exists but {pending_rows} pipeline item(s) lack baseline projections."
            ),
        }
    return {"status": "already_migrated", "applicable": False, "reason": ""}


def _planned_items(conn):
    from wahojobs.pipeline_state import plan_legacy_backfill

    return plan_legacy_backfill(conn)


def apply_pipeline_state_migration(conn, *, classification=None, failure_injector=None):
    if conn.in_transaction:
        raise MigrationError("Migration requires an idle, migration-owned connection.")
    classification = classification or classify_database(conn)
    if classification["status"] == "already_migrated":
        summary = backfill_legacy_pipeline_state(conn, dry_run=True)
        summary["dry_run"] = False
        return summary
    if classification["status"] != "legacy":
        raise MigrationError(classification["reason"] or "Database is not migration-ready.")

    pipeline_count_before = table_count(conn, "user_pipeline_items")
    statements = list(iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8")))
    conn.execute("BEGIN IMMEDIATE")
    try:
        for index, statement in enumerate(statements, start=1):
            if statement.lstrip().upper().startswith("CREATE TRIGGER"):
                inject_failure(failure_injector, "before_trigger_install")
            conn.execute(statement)
            if index == 1:
                inject_failure(failure_injector, "after_first_ddl")
        inject_failure(failure_injector, "after_all_ddl_before_backfill")

        def after_item(index, total, _item):
            if index == 1:
                inject_failure(failure_injector, "after_first_baseline")
            midpoint = max(2, (total + 1) // 2)
            if total >= 3 and index == midpoint:
                inject_failure(failure_injector, "midway_backfill")

        summary = backfill_legacy_pipeline_state(
            conn,
            dry_run=False,
            on_item_migrated=after_item,
        )
        inject_failure(failure_injector, "before_reconciliation")
        reconcile_migration(conn, pipeline_count_before)
        inject_failure(failure_injector, "before_marker_write")
        conn.execute(
            "INSERT INTO wahojobs_schema_migrations (version) VALUES (?)",
            (MIGRATION_VERSION,),
        )
        inject_failure(failure_injector, "after_marker_write")
        reconcile_migration(conn, pipeline_count_before, require_marker=True)
        conn.commit()
        return summary
    except Exception:
        conn.rollback()
        raise


def reconcile_migration(conn, expected_pipeline_count: int, *, require_marker=False):
    projection_count = table_count(conn, "user_pipeline_state")
    transition_count = table_count(conn, "user_pipeline_transitions")
    baseline_count = conn.execute(
        "SELECT COUNT(*) FROM user_pipeline_transitions WHERE affected_dimension = 'baseline'"
    ).fetchone()[0]
    if projection_count != expected_pipeline_count:
        raise MigrationError(
            f"Projection reconciliation failed: expected {expected_pipeline_count}, found {projection_count}."
        )
    if transition_count != expected_pipeline_count or baseline_count != expected_pipeline_count:
        raise MigrationError(
            "Baseline reconciliation failed: expected one baseline transition per legacy pipeline item."
        )
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise MigrationError(f"Foreign-key reconciliation failed: {foreign_keys!r}")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise MigrationError(f"Integrity reconciliation failed: {integrity}")
    if require_marker:
        marker = conn.execute(
            "SELECT 1 FROM wahojobs_schema_migrations WHERE version = ?",
            (MIGRATION_VERSION,),
        ).fetchone()
        if marker is None:
            raise MigrationError("Migration marker reconciliation failed.")


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
        raise MigrationError("Migration SQL ends with an incomplete statement.")


def inject_failure(callback, point: str):
    if callback is not None:
        callback(point)


def empty_summary(*, dry_run):
    return {
        "dry_run": dry_run,
        "planned": 0,
        "migrated": 0,
        "already_initialized": 0,
        "classifications": {},
        "malformed_reminders": 0,
    }


def table_count(conn, table_name):
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    if exists is None:
        return 0
    return conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]


if __name__ == "__main__":
    main()
