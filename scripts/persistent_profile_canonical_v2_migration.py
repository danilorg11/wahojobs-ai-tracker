import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.persistent_profiles_migration as migration_004  # noqa: E402
from wahojobs.persistent_profile_canonical_v2_schema import (  # noqa: E402
    MIGRATION_PATH,
    MIGRATION_VERSION,
    TEMPORARY_TABLES,
    attest_persistent_profile_canonical_v2_schema,
    migration_statement_count,
    persistent_profile_state_counts,
    persistent_profile_state_is_empty,
)
from wahojobs.persistent_profile_schema import PROFILE_TABLES, iter_sql_statements  # noqa: E402


class PersistentProfileCanonicalV2MigrationError(RuntimeError):
    pass


def main():
    args = parse_args()
    db_path = Path(args.db).resolve()
    if migration_004.is_workspace_database_file(db_path) and not args.allow_workspace_db:
        _exit(args, migration_004.workspace_database_blocked_result(apply=args.yes), error=True)
    if not db_path.is_file():
        _exit(
            args,
            _classification(
                "nonexistent",
                False,
                "Database path does not exist; this migration never creates a base database.",
                mode="apply" if args.yes else "inspection",
            ),
            error=bool(args.yes),
        )

    try:
        conn = migration_004.connect(db_path, read_only=not args.yes)
        try:
            if (
                not args.allow_workspace_db
                and migration_004.is_workspace_database_file(
                    migration_004.opened_database_path(conn)
                )
            ):
                _exit(
                    args,
                    migration_004.workspace_database_blocked_result(apply=args.yes),
                    error=True,
                )
            classification = classify_database(conn)
            result = (
                apply_persistent_profile_canonical_v2_migration(
                    conn
                )
                if args.yes
                else {
                    **classification,
                    "mode": "inspection",
                    "changed": False,
                    "migration_version": MIGRATION_VERSION,
                }
            )
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        _exit(
            args,
            _classification(
                "invalid_or_locked_sqlite",
                False,
                f"SQLite inspection or migration failed: {type(exc).__name__}.",
                mode="apply" if args.yes else "inspection",
            ),
            error=True,
        )
    except PersistentProfileCanonicalV2MigrationError as exc:
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
        description="Inspect or explicitly install dormant persistent-profile migration 005."
    )
    parser.add_argument("--db", required=True, help="SQLite database path to inspect.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Explicitly apply migration 005. Without this flag the command is read-only.",
    )
    parser.add_argument(
        "--allow-workspace-db",
        action="store_true",
        help="Allow workspace database access after a separately reviewed authorization.",
    )
    parser.add_argument("--json", action="store_true", help="Emit stable JSON output.")
    return parser.parse_args()


def classify_database(conn) -> dict:
    profile_counts = persistent_profile_state_counts(conn)
    profile_state_queryable = all(value is not None for value in profile_counts.values())
    if profile_state_queryable and not persistent_profile_state_is_empty(profile_counts):
        return {
            **_classification(
                "persistent_profile_state_not_empty",
                False,
                "Migration 005 refuses nonempty persistent-profile state.",
            ),
            "profile_state_counts": profile_counts,
        }

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        return _classification(
            "integrity_invalid", False, "Database integrity check failed."
        )
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        return _classification(
            "foreign_key_invalid", False, "Database foreign-key validation failed."
        )

    attestation = attest_persistent_profile_canonical_v2_schema(conn)
    if attestation["state"] == "pending":
        prerequisite = migration_004.classify_database(conn)
        if prerequisite["database_state"] == "already_migrated":
            return {
                **_classification("pending", True, "Migration 005 is ready to apply."),
                "schema_attestation": attestation,
                "migration_004": prerequisite,
            }
        return {
            **_classification(
                "invalid_prerequisite",
                False,
                "Migrations 001 through 004 must attest and reconcile cleanly first.",
            ),
            "migration_004": prerequisite,
            "schema_attestation": attestation,
        }
    if attestation["state"] == "persistent_profile_state_not_empty":
        return {
            **_classification(
                "persistent_profile_state_not_empty",
                False,
                "Migration 005 refuses nonempty persistent-profile state.",
            ),
            "schema_attestation": attestation,
        }
    if attestation["state"] == "correctly_installed":
        return {
            **_classification("already_migrated", False, "Migration 005 is already complete."),
            "schema_attestation": attestation,
        }
    state_map = {
        "partial_inconsistent": "migration_005_partial_inconsistent",
        "conflicting": "migration_005_conflicting",
        "schema_mismatch": "migration_005_schema_mismatch",
        "invalid_prerequisite": "invalid_prerequisite",
    }
    return {
        **_classification(
            state_map.get(attestation["state"], "migration_005_schema_mismatch"),
            False,
            "Migration 005 schema does not match a valid M004-only or final M005 state.",
        ),
        "schema_attestation": attestation,
    }


def apply_persistent_profile_canonical_v2_migration(
    conn, *, failure_injector=None
):
    if conn.in_transaction:
        raise PersistentProfileCanonicalV2MigrationError(
            "Migration requires an idle, migration-owned connection."
        )
    transaction_started = False
    statements = list(iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8")))
    try:
        inject_failure(failure_injector, "before_initial_prerequisite_attestation")
        classification = classify_database(conn)
        inject_failure(failure_injector, "after_initial_prerequisite_attestation")
        if classification["database_state"] == "already_migrated":
            return {
                **classification,
                "mode": "apply",
                "changed": False,
                "migration_version": MIGRATION_VERSION,
                **failure_injection_accounting(statements),
            }
        if classification["database_state"] != "pending":
            raise PersistentProfileCanonicalV2MigrationError(classification["reason"])

        inject_failure(failure_injector, "before_initial_empty_state_check")
        initial_counts = persistent_profile_state_counts(conn)
        if not persistent_profile_state_is_empty(initial_counts):
            raise PersistentProfileCanonicalV2MigrationError(
                "persistent_profile_state_not_empty"
            )
        inject_failure(failure_injector, "after_initial_empty_state_check")
        preserved_tables = _preserved_tables(conn)
        preserved_before = {table: _table_count(conn, table) for table in preserved_tables}

        inject_failure(failure_injector, "before_begin_immediate")
        conn.execute("BEGIN IMMEDIATE")
        transaction_started = True
        inject_failure(failure_injector, "after_begin_immediate")
        inject_failure(failure_injector, "before_defer_foreign_keys")
        conn.execute("PRAGMA defer_foreign_keys = ON")
        inject_failure(failure_injector, "after_defer_foreign_keys")

        inject_failure(failure_injector, "before_transactional_empty_state_check")
        locked_counts = persistent_profile_state_counts(conn)
        if not persistent_profile_state_is_empty(locked_counts):
            raise PersistentProfileCanonicalV2MigrationError(
                "persistent_profile_state_not_empty"
            )
        inject_failure(failure_injector, "after_transactional_empty_state_check")

        old_tables_checked = False
        operations = operation_names(statements)
        for index, (statement, operation) in enumerate(
            zip(statements, operations, strict=True), start=1
        ):
            if statement.startswith("DROP TABLE product_profile_sources_m005_backup"):
                inject_failure(failure_injector, "before_old_table_empty_verification")
                backup_counts = {table: _table_count(conn, table) for table in TEMPORARY_TABLES}
                if any(backup_counts.values()):
                    raise PersistentProfileCanonicalV2MigrationError(
                        "persistent_profile_state_not_empty"
                    )
                old_tables_checked = True
                inject_failure(failure_injector, "after_old_table_empty_verification")
            inject_failure(failure_injector, f"before_operation_{index}_{operation}")
            conn.execute(statement)
            inject_failure(failure_injector, f"after_operation_{index}_{operation}")
        if not old_tables_checked:
            raise PersistentProfileCanonicalV2MigrationError(
                "Migration 005 did not verify renamed empty tables."
            )

        inject_failure(failure_injector, "before_marker_write")
        conn.execute(
            "INSERT INTO wahojobs_schema_migrations(version) VALUES (?)",
            (MIGRATION_VERSION,),
        )
        inject_failure(failure_injector, "after_marker_write")

        inject_failure(failure_injector, "before_combined_schema_attestation")
        attestation = attest_persistent_profile_canonical_v2_schema(conn)
        if attestation["state"] != "correctly_installed":
            raise PersistentProfileCanonicalV2MigrationError(
                "Migration 005 combined schema attestation failed."
            )
        inject_failure(failure_injector, "after_combined_schema_attestation")

        inject_failure(failure_injector, "before_final_empty_state_validation")
        final_counts = persistent_profile_state_counts(conn)
        if not persistent_profile_state_is_empty(final_counts):
            raise PersistentProfileCanonicalV2MigrationError(
                "Migration 005 created unexpected persistent-profile rows."
            )
        inject_failure(failure_injector, "after_final_empty_state_validation")

        inject_failure(failure_injector, "before_integrity_check")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise PersistentProfileCanonicalV2MigrationError(
                "Integrity check failed after installing migration 005."
            )
        inject_failure(failure_injector, "after_integrity_check")
        inject_failure(failure_injector, "before_foreign_key_check")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise PersistentProfileCanonicalV2MigrationError(
                "Foreign-key check failed after installing migration 005."
            )
        inject_failure(failure_injector, "after_foreign_key_check")

        inject_failure(failure_injector, "before_preservation_check")
        preserved_after = {table: _table_count(conn, table) for table in preserved_tables}
        if preserved_after != preserved_before:
            raise PersistentProfileCanonicalV2MigrationError(
                "Migration 005 changed pre-existing business row counts."
            )
        inject_failure(failure_injector, "after_preservation_check")
        conn.commit()
        transaction_started = False
    except Exception:
        if transaction_started:
            conn.rollback()
        raise

    return {
        "database_state": "migrated",
        "applicable": False,
        "reason": "Migration 005 applied successfully to empty dormant profile state.",
        "mode": "apply",
        "changed": True,
        "migration_version": MIGRATION_VERSION,
        "schema_attestation": attestation,
        "profile_state_counts": final_counts,
        "preserved_table_counts": preserved_after,
        "statement_count": len(statements),
        **failure_injection_accounting(statements),
    }


def operation_names(statements=None) -> tuple[str, ...]:
    statements = statements or list(
        iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8"))
    )
    names = []
    for statement in statements:
        words = statement.split()
        verb = words[0].lower()
        if verb == "alter":
            name = f"rename_{words[2]}"
        else:
            object_type = words[1].lower()
            offset = 2
            if object_type == "unique":
                object_type = words[2].lower()
                offset = 3
            name = f"{verb}_{object_type}_{words[offset]}"
        names.append(_slug(name))
    if len(names) != len(set(names)):
        raise PersistentProfileCanonicalV2MigrationError(
            "Migration operation names must be unique."
        )
    return tuple(names)


def failure_injection_points(statements=None) -> tuple[str, ...]:
    statements = statements or list(
        iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8"))
    )
    points = [
        "before_initial_prerequisite_attestation",
        "after_initial_prerequisite_attestation",
        "before_initial_empty_state_check",
        "after_initial_empty_state_check",
        "before_begin_immediate",
        "after_begin_immediate",
        "before_defer_foreign_keys",
        "after_defer_foreign_keys",
        "before_transactional_empty_state_check",
        "after_transactional_empty_state_check",
    ]
    operations = operation_names(statements)
    for index, operation in enumerate(operations, start=1):
        if operation == "drop_table_product_profile_sources_m005_backup":
            points.extend(
                ("before_old_table_empty_verification", "after_old_table_empty_verification")
            )
        points.extend(
            (f"before_operation_{index}_{operation}", f"after_operation_{index}_{operation}")
        )
    for boundary in (
        "marker_write",
        "combined_schema_attestation",
        "final_empty_state_validation",
        "integrity_check",
        "foreign_key_check",
        "preservation_check",
    ):
        points.extend((f"before_{boundary}", f"after_{boundary}"))
    return tuple(points)


def failure_injection_accounting(statements=None) -> dict:
    statements = statements or list(
        iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8"))
    )
    return {
        "fault_injection_hook_count": len(failure_injection_points(statements)),
        "durable_state_checkpoint_count": len(statements) + 2,
    }


def inject_failure(callback, point):
    if callback is not None:
        callback(point)


def _preserved_tables(conn) -> tuple[str, ...]:
    return tuple(
        sorted(
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name <> 'wahojobs_schema_migrations'"
            )
            if row[0] not in PROFILE_TABLES and row[0] not in TEMPORARY_TABLES
        )
    )


def _table_count(conn, table_name):
    return conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]


def _slug(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value).strip("_")


def _classification(state, applicable, reason, *, mode=None):
    result = {"database_state": state, "applicable": applicable, "reason": reason}
    if mode:
        result.update({"mode": mode, "changed": False})
    return result


def _exit(args, result, *, error):
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Persistent Profile Canonical V2 Migration 005")
        print("=============================================")
        print(f"Mode: {result.get('mode', 'inspection')}")
        print(f"Database state: {result.get('database_state', 'unknown')}")
        print(f"Applicable: {'yes' if result.get('applicable') else 'no'}")
        print(f"Changed: {'yes' if result.get('changed') else 'no'}")
        print(f"Reason: {result.get('reason', '')}")
    raise SystemExit(1 if error else 0)


if __name__ == "__main__":
    main()
