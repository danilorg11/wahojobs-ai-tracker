from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.google_oidc_authorization_transactions_migration as migration_006  # noqa: E402
from wahojobs.closed_schema_convergence_schema import (  # noqa: E402
    COMPANY_COLUMNS,
    EXPECTED_SCHEMA_FINGERPRINT,
    JOB_COLUMNS,
    MIGRATION_PATH,
    MIGRATION_VERSION,
    PREREQUISITE_MIGRATION_VERSIONS,
    attest_closed_schema_convergence_schema,
    iter_sql_statements,
)
from wahojobs.database_lifetime_ownership import (  # noqa: E402
    ROLE_OFFLINE_OPERATOR,
    DatabaseLifetimeOwnershipError,
    acquire_database_lifetime_ownership,
    release_database_lifetime_ownership,
    require_database_lifetime_ownership,
)


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_M007_TABLES = frozenset(
    {
        "companies",
        "jobs",
        "companies_m007_backup",
        "jobs_m007_backup",
        "sqlite_sequence",
        "wahojobs_schema_migrations",
    }
)
_M007_INDEXES = frozenset(
    {
        "idx_jobs_company_active",
        "idx_jobs_first_seen_at",
        "idx_jobs_last_seen_at",
        "idx_jobs_live_market",
        "idx_jobs_canonical_opportunity",
    }
)
_M007_AUTOINDEX_PREFIXES = (
    "sqlite_autoindex_companies_",
    "sqlite_autoindex_jobs_",
    "sqlite_autoindex_companies_m007_backup_",
    "sqlite_autoindex_jobs_m007_backup_",
)
_MIGRATION_MARKER_INSERT_SQL = (
    "INSERT INTO wahojobs_schema_migrations(version) VALUES ('"
    + MIGRATION_VERSION.replace("'", "''")
    + "')"
)


class ClosedSchemaConvergenceMigrationError(RuntimeError):
    def __init__(self, message, *, category="migration_failed"):
        self.category = category
        super().__init__(message)


_BACKUP_EVIDENCE_PROTOCOL = "wahojobs-m007-verified-backup-v1"
_BACKUP_ERROR_CATEGORY = "verified_external_backup_invalid"


@dataclass(frozen=True, slots=True)
class _VerifiedBackupSeal:
    protocol: str
    target_path: Path
    target_identity: migration_006.DatabaseFileIdentity
    target_size: int
    target_sha256: str
    target_database_state: str
    target_schema_object_count: int
    target_schema_fingerprint: str
    target_migration_versions: tuple[str, ...]
    backup_path: Path
    backup_identity: migration_006.DatabaseFileIdentity
    backup_size: int
    backup_sha256: str
    binding: str


class _PinnedBackupFile:
    __slots__ = ("path", "identity", "handle")

    def __init__(self, *, path, identity, handle):
        self.path = path
        self.identity = identity
        self.handle = handle

    def close(self):
        handle = self.handle
        if handle is not None:
            handle.close()
            self.handle = None


class _M007Authorizer:
    def __init__(self):
        self.mutation_permitted = False

    def permit_mutation(self):
        self.mutation_permitted = True

    def __call__(self, action, first, second, database, _source):
        if action in {
            getattr(sqlite3, "SQLITE_ATTACH", -1),
            getattr(sqlite3, "SQLITE_DETACH", -2),
        }:
            return sqlite3.SQLITE_DENY
        if action == getattr(sqlite3, "SQLITE_PRAGMA", -3):
            pragma = "" if first is None else str(first).casefold()
            if pragma in {
                "database_list",
                "defer_foreign_keys",
                "foreign_key_check",
                "foreign_key_list",
                "foreign_keys",
                "index_list",
                "index_xinfo",
                "integrity_check",
                "journal_mode",
                "quick_check",
                "recursive_triggers",
                "table_info",
                "table_xinfo",
            }:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action in {
            getattr(sqlite3, "SQLITE_SELECT", -4),
            getattr(sqlite3, "SQLITE_READ", -5),
            getattr(sqlite3, "SQLITE_FUNCTION", -6),
            getattr(sqlite3, "SQLITE_TRANSACTION", -7),
            getattr(sqlite3, "SQLITE_RECURSIVE", -8),
        }:
            return sqlite3.SQLITE_OK
        if not self.mutation_permitted:
            return sqlite3.SQLITE_DENY

        if action in {
            getattr(sqlite3, "SQLITE_INSERT", -9),
            getattr(sqlite3, "SQLITE_UPDATE", -10),
            getattr(sqlite3, "SQLITE_DELETE", -11),
        }:
            return (
                sqlite3.SQLITE_OK
                if first in _M007_TABLES
                or first in {"sqlite_master", "sqlite_schema"}
                else sqlite3.SQLITE_DENY
            )
        if action in {
            getattr(sqlite3, "SQLITE_CREATE_TABLE", -12),
            getattr(sqlite3, "SQLITE_DROP_TABLE", -13),
        }:
            return sqlite3.SQLITE_OK if first in _M007_TABLES else sqlite3.SQLITE_DENY
        if action in {
            getattr(sqlite3, "SQLITE_CREATE_INDEX", -14),
            getattr(sqlite3, "SQLITE_DROP_INDEX", -15),
            getattr(sqlite3, "SQLITE_REINDEX", -16),
        }:
            allowed = (
                first in _M007_INDEXES
                or any(str(first).startswith(prefix) for prefix in _M007_AUTOINDEX_PREFIXES)
            )
            return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY


def main(*, failure_injector=None, _connect=sqlite3.connect):
    args = parse_args()
    mode = "apply" if args.yes else "inspection"
    ownership = None
    witness = None
    result = None
    classification = None
    error = False
    commit_state = {"committed": False, "rollback_failed": False}
    db_path = None
    try:
        db_path = migration_006.canonical_database_path(args.db)
        workspace_database = migration_006.migration_004.is_workspace_database_file(
            db_path
        )
        if workspace_database and not args.allow_workspace_db:
            raise ClosedSchemaConvergenceMigrationError(
                "workspace_database_blocked",
                category="workspace_database_blocked",
            )
        identity = migration_006.database_file_identity(db_path)
        if identity is None:
            raise ClosedSchemaConvergenceMigrationError(
                "unsafe_path", category="unsafe_path"
            )
        sidecars = existing_sqlite_sidecars(db_path)
        if sidecars:
            result = {
                **_classification(
                    "sqlite_sidecar_present",
                    False,
                    "Migration 007 refuses a database with SQLite sidecars.",
                    mode=mode,
                ),
                "sidecar_suffixes": [
                    path.name[len(db_path.name) :] for path in sidecars
                ],
                "migration_version": MIGRATION_VERSION,
            }
            error = True
        else:
            if args.yes:
                inject_failure(failure_injector, "before_ownership_acquisition")
                ownership = acquire_database_lifetime_ownership(
                    db_path,
                    role=ROLE_OFFLINE_OPERATOR,
                )
                inject_failure(failure_injector, "after_ownership_acquisition")
                require_database_lifetime_ownership(
                    ownership,
                    role=ROLE_OFFLINE_OPERATOR,
                    database_path=db_path,
                )

            inject_failure(failure_injector, "before_target_open")
            witness = migration_006.open_canonical_sqlite_database(
                db_path,
                read_only=True,
                expected_identity=identity,
                connect=_connect,
            )
            inject_failure(failure_injector, "after_target_open")
            if (
                not args.allow_workspace_db
                and migration_006.migration_004.is_workspace_database_file(
                    migration_006.opened_database_path(witness)
                )
            ):
                raise ClosedSchemaConvergenceMigrationError(
                    "workspace_database_blocked",
                    category="workspace_database_blocked",
                )
            classification = classify_database(witness)
            if args.yes:
                result = apply_closed_schema_convergence_migration(
                    witness,
                    requested_path=db_path,
                    expected_identity=identity,
                    ownership=ownership,
                    classification=classification,
                    verified_backup=(
                        args.verified_backup if workspace_database else None
                    ),
                    _connect=_connect,
                    failure_injector=failure_injector,
                    commit_state=commit_state,
                )
            else:
                if (
                    not migration_006.opened_database_matches(
                        witness, db_path, identity
                    )
                    or existing_sqlite_sidecars(db_path)
                ):
                    raise ClosedSchemaConvergenceMigrationError(
                        "Database identity changed during inspection."
                    )
                result = {
                    **classification,
                    "mode": "inspection",
                    "changed": False,
                    "migration_version": MIGRATION_VERSION,
                }

            witness.close()
            witness = None
            if args.yes and result.get("changed"):
                result["post_commit_read_only_verification"] = (
                    verify_committed_database_read_only(
                        db_path,
                        ownership=ownership,
                        expected_precommit_identity=identity,
                        failure_injector=failure_injector,
                        _connect=_connect,
                    )
                )
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        error = True
        result = (
            _post_commit_verification_failure()
            if commit_state["committed"]
            else {
                **_classification(
                    "migration_failed",
                    False,
                    "Migration or inspection was interrupted before commit.",
                    mode=mode,
                ),
                "migration_version": MIGRATION_VERSION,
            }
        )
    except DatabaseLifetimeOwnershipError as exc:
        error = True
        state = (
            "ownership_contended"
            if exc.category == "contention"
            else (
                "ownership_cleanup_incomplete"
                if exc.category == "cleanup_incomplete"
                else "ownership_unavailable"
            )
        )
        result = (
            _post_commit_verification_failure()
            if commit_state["committed"]
            else {
                **_classification(
                    state,
                    False,
                    "Offline-operator database ownership could not be established.",
                    mode=mode,
                ),
                "migration_version": MIGRATION_VERSION,
                "rollback_verified": not commit_state["rollback_failed"],
            }
        )
    except sqlite3.Error as exc:
        error = True
        state = _sqlite_failure_state(exc)
        result = (
            _post_commit_verification_failure()
            if commit_state["committed"]
            else {
                **_classification(
                    state,
                    False,
                    (
                        "SQLite target is busy or locked."
                        if state == "database_busy"
                        else "SQLite target validation or apply failed."
                    ),
                    mode=mode,
                ),
                "migration_version": MIGRATION_VERSION,
                "rollback_verified": not commit_state["rollback_failed"],
            }
        )
    except migration_006.GoogleOidcAuthorizationTransactionsMigrationError:
        error = True
        result = (
            _post_commit_verification_failure()
            if commit_state["committed"]
            else {
                **_classification(
                    "unsafe_path",
                    False,
                    "Database path or identity is unavailable or unsafe.",
                    mode=mode,
                ),
                "migration_version": MIGRATION_VERSION,
                "rollback_verified": not commit_state["rollback_failed"],
            }
        )
    except ClosedSchemaConvergenceMigrationError as exc:
        error = True
        preserved_classification = (
            classification
            if classification is not None
            and classification.get("database_state") == exc.category
            else _classification(
                exc.category,
                False,
                "Migration 007 validation or apply failed before commit.",
            )
        )
        result = (
            _post_commit_verification_failure()
            if commit_state["committed"]
            else {
                **preserved_classification,
                "mode": mode,
                "changed": False,
                "migration_version": MIGRATION_VERSION,
                "rollback_verified": not commit_state["rollback_failed"],
            }
        )
    except Exception:
        error = True
        result = (
            _post_commit_verification_failure()
            if commit_state["committed"]
            else {
                **_classification(
                    "migration_failed",
                    False,
                    "Migration or inspection failed before commit.",
                    mode=mode,
                ),
                "migration_version": MIGRATION_VERSION,
                "rollback_verified": not commit_state["rollback_failed"],
            }
        )
    finally:
        if witness is not None:
            try:
                witness.close()
            except BaseException:
                error = True
        if ownership is not None and db_path is not None:
            try:
                release_database_lifetime_ownership(
                    ownership,
                    role=ROLE_OFFLINE_OPERATOR,
                    database_path=db_path,
                )
            except BaseException:
                error = True
                result = (
                    _post_commit_verification_failure(cleanup=True)
                    if commit_state["committed"]
                    else {
                        **_classification(
                            "cleanup_incomplete",
                            False,
                            "Offline-operator ownership cleanup did not complete.",
                            mode=mode,
                        ),
                        "migration_version": MIGRATION_VERSION,
                    }
                )
    _exit(args, result, error=error)


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or explicitly apply closed-schema convergence migration 007."
        )
    )
    parser.add_argument("--db", required=True, help="Existing SQLite database path.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Explicitly apply migration 007. Without this flag the command is read-only.",
    )
    parser.add_argument(
        "--allow-workspace-db",
        action="store_true",
        help="Allow workspace database access after separate authorization.",
    )
    parser.add_argument(
        "--verified-backup",
        help="External exact-copy backup required for an authorized workspace apply.",
    )
    parser.add_argument("--json", action="store_true", help="Emit stable JSON output.")
    return parser.parse_args()


def classify_database(conn: sqlite3.Connection) -> dict:
    return _classify_database(conn, allow_transaction=False)


def _classify_database_with_raw_rows(
    conn: sqlite3.Connection,
    *,
    allow_transaction: bool,
) -> dict:
    previous_row_factory = conn.row_factory
    previous_text_factory = conn.text_factory
    try:
        conn.row_factory = None
        conn.text_factory = str
        return _classify_database(conn, allow_transaction=allow_transaction)
    finally:
        conn.row_factory = previous_row_factory
        conn.text_factory = previous_text_factory


def _classify_database(
    conn: sqlite3.Connection,
    *,
    allow_transaction: bool,
) -> dict:
    if type(conn) is not sqlite3.Connection or (
        conn.in_transaction and not allow_transaction
    ):
        raise ClosedSchemaConvergenceMigrationError(
            "Classification requires an idle SQLite connection."
        )
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()
    if (
        type(journal_mode) is not tuple
        or len(journal_mode) != 1
        or type(journal_mode[0]) is not str
        or journal_mode[0].casefold() != "delete"
    ):
        return _classification(
            "journal_mode_invalid",
            False,
            "Rollback-journal mode is required.",
        )
    quick = conn.execute("PRAGMA quick_check(1)").fetchone()
    if quick != ("ok",):
        return _classification(
            "quick_check_invalid", False, "Database quick check failed."
        )
    integrity = conn.execute("PRAGMA integrity_check").fetchone()
    if integrity != ("ok",):
        return _classification(
            "integrity_invalid", False, "Database integrity check failed."
        )
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        return _classification(
            "foreign_key_invalid",
            False,
            "Database foreign-key validation failed.",
        )
    attestation = attest_closed_schema_convergence_schema(conn)
    state_map = {
        "legacy_rebuild_pending": (
            "legacy_rebuild_pending",
            True,
            "Historical base tables are ready for atomic convergence.",
        ),
        "canonical_marker_pending": (
            "canonical_marker_pending",
            True,
            "Canonical schema is ready for the migration 007 marker.",
        ),
        "correctly_installed": (
            "exact_installed",
            False,
            "Migration 007 is exactly installed.",
        ),
        "partial_inconsistent": (
            "partial",
            False,
            "Migration 007 has an incomplete marker or schema state.",
        ),
        "schema_mismatch": (
            "drifted",
            False,
            "Closed schema differs from an accepted migration 007 source state.",
        ),
        "residue": (
            "residue",
            False,
            "Main or temporary migration 007 residue prevents migration.",
        ),
        "invalid_prerequisite": (
            "invalid_prerequisite",
            False,
            "Exact migrations 001 through 006 are required.",
        ),
    }
    database_state, applicable, reason = state_map.get(
        attestation["state"],
        ("drifted", False, "Closed schema state is not recognized as safe."),
    )
    if database_state == "legacy_rebuild_pending" and _canonical_orphan_exists(conn):
        database_state = "data_incompatible"
        applicable = False
        reason = "A job references a missing canonical opportunity."
    result = {
        **_classification(database_state, applicable, reason),
        "schema_attestation": attestation,
    }
    if database_state in {
        "legacy_rebuild_pending",
        "canonical_marker_pending",
        "exact_installed",
    }:
        result["table_counts"] = {
            "companies": _table_count(conn, "companies"),
            "jobs": _table_count(conn, "jobs"),
        }
    return result


def apply_closed_schema_convergence_migration(
    witness,
    *,
    requested_path,
    expected_identity,
    ownership,
    classification=None,
    verified_backup=None,
    _connect=sqlite3.connect,
    failure_injector=None,
    commit_state=None,
):
    if (
        type(witness) is not sqlite3.Connection
        or witness.in_transaction
        or not isinstance(requested_path, Path)
        or type(expected_identity) is not migration_006.DatabaseFileIdentity
    ):
        raise ClosedSchemaConvergenceMigrationError(
            "Migration requires an idle witness and explicit target identity."
        )
    target_path = migration_006.canonical_database_path(requested_path)
    if migration_006.database_file_identity(target_path) != expected_identity:
        raise ClosedSchemaConvergenceMigrationError(
            "Migration target identity changed before apply.",
            category="target_changed",
        )
    require_database_lifetime_ownership(
        ownership,
        role=ROLE_OFFLINE_OPERATOR,
        database_path=target_path,
    )
    _require_stable_owned_target(
        witness,
        target_path=target_path,
        expected_identity=expected_identity,
        ownership=ownership,
    )
    backup_seal = None
    if migration_006.migration_004.is_workspace_database_file(target_path):
        direct_classification = _classify_database_with_raw_rows(
            witness,
            allow_transaction=False,
        )
        backup_seal = _verify_external_backup_seal(
            target_path,
            verified_backup,
            expected_target_identity=expected_identity,
            expected_database_state=direct_classification["database_state"],
            _connect=_connect,
        )
        _verify_locked_backup(
            witness,
            requested_path=target_path,
            expected_identity=expected_identity,
            backup_seal=backup_seal,
            locked_classification=direct_classification,
            ownership=ownership,
        )
    inject_failure(failure_injector, "before_private_worker_open")
    if (
        migration_006.database_file_identity(target_path) != expected_identity
        or existing_sqlite_sidecars(target_path)
    ):
        raise ClosedSchemaConvergenceMigrationError(
            "Migration target changed before private worker open.",
            category="target_changed",
        )
    migration_006._retire_private_migration_candidate(
        target_path=target_path,
        target_identity=expected_identity,
    )
    worker = migration_006._open_exact_private_migration_worker(
        target_path,
        expected_identity,
    )
    try:
        inject_failure(failure_injector, "after_private_worker_open")
        return _apply_on_owned_connection(
            worker,
            requested_path=target_path,
            expected_identity=expected_identity,
            ownership=ownership,
            classification=classification,
            backup_seal=backup_seal,
            failure_injector=failure_injector,
            commit_state=commit_state,
        )
    finally:
        try:
            worker.set_authorizer(None)
        finally:
            worker.close()


def _apply_on_owned_connection(
    conn,
    *,
    requested_path,
    expected_identity,
    ownership,
    classification=None,
    backup_seal=None,
    failure_injector=None,
    commit_state=None,
):
    if commit_state is not None:
        commit_state["committed"] = False
        commit_state["rollback_failed"] = False
    if conn.in_transaction:
        raise ClosedSchemaConvergenceMigrationError(
            "Migration requires an idle migration-owned connection."
        )
    if conn.execute("PRAGMA foreign_keys").fetchone() != (1,):
        raise ClosedSchemaConvergenceMigrationError(
            "Migration requires foreign-key enforcement."
        )
    conn.execute("PRAGMA recursive_triggers = ON")
    if conn.execute("PRAGMA recursive_triggers").fetchone() != (1,):
        raise ClosedSchemaConvergenceMigrationError(
            "Migration requires recursive-trigger enforcement."
        )

    _require_stable_owned_target(
        conn,
        target_path=requested_path,
        expected_identity=expected_identity,
        ownership=ownership,
    )
    classification = classify_database(conn)
    if classification["database_state"] == "exact_installed":
        _require_stable_owned_target(
            conn,
            target_path=requested_path,
            expected_identity=expected_identity,
            ownership=ownership,
        )
        return {
            **classification,
            "mode": "apply",
            "changed": False,
            "migration_version": MIGRATION_VERSION,
            "migration_action": "none",
            **(
                {"verified_external_backup": _backup_evidence_summary(backup_seal)}
                if backup_seal is not None
                else {}
            ),
            **failure_injection_accounting(()),
        }
    if classification["database_state"] not in {
        "legacy_rebuild_pending",
        "canonical_marker_pending",
    }:
        raise ClosedSchemaConvergenceMigrationError(
            classification["reason"],
            category=classification["database_state"],
        )

    plan = classification["database_state"]
    statements = (
        tuple(iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8")))
        if plan == "legacy_rebuild_pending"
        else ()
    )
    transaction_started = False
    final_backup_pin = None
    authorizer = _M007Authorizer()
    try:
        inject_failure(failure_injector, "before_begin_immediate")
        conn.execute("BEGIN IMMEDIATE")
        transaction_started = True
        inject_failure(failure_injector, "after_begin_immediate")
        if plan == "legacy_rebuild_pending":
            inject_failure(failure_injector, "before_defer_foreign_keys")
            conn.execute("PRAGMA defer_foreign_keys = ON")
            if conn.execute("PRAGMA defer_foreign_keys").fetchone() != (1,):
                raise ClosedSchemaConvergenceMigrationError(
                    "Deferred foreign-key enforcement is unavailable."
                )
            inject_failure(failure_injector, "after_defer_foreign_keys")

        inject_failure(
            failure_injector, "before_locked_schema_classification"
        )
        locked = _classify_database(conn, allow_transaction=True)
        if locked["database_state"] != plan:
            raise ClosedSchemaConvergenceMigrationError(
                "Migration source state changed under the write lock."
            )
        inject_failure(
            failure_injector, "after_locked_schema_classification"
        )
        inject_failure(failure_injector, "before_locked_ownership_check")
        require_database_lifetime_ownership(
            ownership,
            role=ROLE_OFFLINE_OPERATOR,
            database_path=requested_path,
        )
        inject_failure(failure_injector, "after_locked_ownership_check")
        inject_failure(
            failure_injector, "before_locked_identity_sidecar_check"
        )
        if (
            migration_006.database_file_identity(requested_path)
            != expected_identity
            or not migration_006.opened_database_matches(
                conn, requested_path, expected_identity
            )
            or existing_sqlite_sidecars(requested_path)
        ):
            raise ClosedSchemaConvergenceMigrationError(
                "Migration target identity or sidecar state changed under lock.",
                category="target_changed",
            )
        inject_failure(
            failure_injector, "after_locked_identity_sidecar_check"
        )
        inject_failure(failure_injector, "before_locked_backup_check")
        _verify_locked_backup(
            conn,
            requested_path=requested_path,
            expected_identity=expected_identity,
            backup_seal=backup_seal,
            locked_classification=locked,
            ownership=ownership,
        )
        inject_failure(failure_injector, "after_locked_backup_check")
        _verify_locked_backup(
            conn,
            requested_path=requested_path,
            expected_identity=expected_identity,
            backup_seal=backup_seal,
            locked_classification=locked,
            ownership=ownership,
        )

        inject_failure(failure_injector, "before_preserved_manifest_capture")
        preserved_before = _preserved_manifest(conn)
        inject_failure(failure_injector, "after_preserved_manifest_capture")
        inject_failure(failure_injector, "before_sequence_capture")
        sequence_before = _sequence_rows(conn)
        inject_failure(failure_injector, "after_sequence_capture")
        company_count = _table_count(conn, "companies")
        job_count = _table_count(conn, "jobs")
        if plan == "legacy_rebuild_pending" and _canonical_orphan_exists(conn):
            raise ClosedSchemaConvergenceMigrationError(
                "A job references a missing canonical opportunity."
            )

        conn.set_authorizer(authorizer)
        authorizer.permit_mutation()
        sequence_restored = plan != "legacy_rebuild_pending"
        shadows_verified = plan != "legacy_rebuild_pending"
        for index, (statement, operation) in enumerate(
            zip(statements, operation_names(statements), strict=True),
            start=1,
        ):
            inject_failure(
                failure_injector,
                f"before_operation_{index}_{operation}",
            )
            conn.execute(statement)
            inject_failure(
                failure_injector,
                f"after_operation_{index}_{operation}",
            )
            if operation == "create_table_jobs_m007_backup":
                inject_failure(failure_injector, "before_shadow_verification")
                _require_tables_equivalent(
                    conn, "companies", "companies_m007_backup", COMPANY_COLUMNS
                )
                _require_tables_equivalent(
                    conn, "jobs", "jobs_m007_backup", JOB_COLUMNS
                )
                inject_failure(failure_injector, "after_shadow_verification")
            if operation == "create_index_idx_jobs_canonical_opportunity":
                inject_failure(failure_injector, "before_sequence_restoration")
                _restore_sequence_rows(conn, sequence_before)
                sequence_restored = True
                inject_failure(failure_injector, "after_sequence_restoration")
                inject_failure(failure_injector, "before_rebuilt_data_verification")
                _require_tables_equivalent(
                    conn, "companies", "companies_m007_backup", COMPANY_COLUMNS
                )
                _require_tables_equivalent(
                    conn, "jobs", "jobs_m007_backup", JOB_COLUMNS
                )
                shadows_verified = True
                inject_failure(failure_injector, "after_rebuilt_data_verification")
        if not sequence_restored or not shadows_verified:
            raise ClosedSchemaConvergenceMigrationError(
                "Migration 007 rebuild verification was not reached."
            )

        inject_failure(failure_injector, "before_marker_write")
        conn.execute(_MIGRATION_MARKER_INSERT_SQL)
        inject_failure(failure_injector, "after_marker_write")

        inject_failure(failure_injector, "before_closed_schema_attestation")
        final_attestation = attest_closed_schema_convergence_schema(conn)
        if final_attestation["state"] != "correctly_installed":
            raise ClosedSchemaConvergenceMigrationError(
                "Migration 007 final schema attestation failed."
            )
        inject_failure(failure_injector, "after_closed_schema_attestation")

        if _table_count(conn, "companies") != company_count or _table_count(
            conn, "jobs"
        ) != job_count:
            raise ClosedSchemaConvergenceMigrationError(
                "Migration 007 changed base table counts."
            )
        inject_failure(failure_injector, "before_sequence_authority_check")
        if _sequence_rows(conn) != sequence_before:
            raise ClosedSchemaConvergenceMigrationError(
                "Migration 007 changed AUTOINCREMENT authority."
            )
        inject_failure(failure_injector, "after_sequence_authority_check")
        inject_failure(failure_injector, "before_preserved_manifest_check")
        if _preserved_manifest(conn) != preserved_before:
            raise ClosedSchemaConvergenceMigrationError(
                "Migration 007 changed unrelated schema or table counts."
            )
        inject_failure(failure_injector, "after_preserved_manifest_check")
        inject_failure(failure_injector, "before_journal_mode_check")
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()
        if (
            type(journal_mode) is not tuple
            or len(journal_mode) != 1
            or type(journal_mode[0]) is not str
            or journal_mode[0].casefold() != "delete"
        ):
            raise ClosedSchemaConvergenceMigrationError(
                "Migration 007 journal mode changed."
            )
        inject_failure(failure_injector, "after_journal_mode_check")
        inject_failure(failure_injector, "before_quick_check")
        if conn.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
            raise ClosedSchemaConvergenceMigrationError(
                "Migration 007 quick check failed."
            )
        inject_failure(failure_injector, "after_quick_check")
        inject_failure(failure_injector, "before_integrity_check")
        if conn.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ClosedSchemaConvergenceMigrationError(
                "Migration 007 integrity check failed."
            )
        inject_failure(failure_injector, "after_integrity_check")
        inject_failure(failure_injector, "before_foreign_key_check")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ClosedSchemaConvergenceMigrationError(
                "Migration 007 foreign-key check failed."
            )
        inject_failure(failure_injector, "after_foreign_key_check")
        inject_failure(failure_injector, "before_final_ownership_check")
        require_database_lifetime_ownership(
            ownership,
            role=ROLE_OFFLINE_OPERATOR,
            database_path=requested_path,
        )
        inject_failure(failure_injector, "after_final_ownership_check")
        inject_failure(failure_injector, "before_final_backup_seal")
        final_backup_pin = _open_final_backup_seal(
            conn,
            requested_path=requested_path,
            expected_identity=expected_identity,
            backup_seal=backup_seal,
        )
        inject_failure(failure_injector, "after_final_backup_seal")
        inject_failure(
            failure_injector, "before_final_target_identity_check"
        )
        _verify_final_target_identity(
            conn,
            requested_path=requested_path,
            expected_identity=expected_identity,
        )
        inject_failure(
            failure_injector, "after_final_target_identity_check"
        )
        inject_failure(failure_injector, "before_commit")
        _verify_final_precommit_state(
            conn,
            requested_path=requested_path,
            expected_identity=expected_identity,
            backup_seal=backup_seal,
            backup_pin=final_backup_pin,
            ownership=ownership,
        )
        conn.commit()
        transaction_started = False
        if commit_state is not None:
            commit_state["committed"] = True
        cleanup_failure = _close_pinned_backup(final_backup_pin)
        if cleanup_failure is not None:
            raise ClosedSchemaConvergenceMigrationError(
                "Verified backup seal cleanup failed after commit.",
                category="cleanup_incomplete",
            ) from cleanup_failure
        final_backup_pin = None
        inject_failure(failure_injector, "after_commit")
    except BaseException as primary:
        if transaction_started:
            try:
                conn.rollback()
            except BaseException:
                if commit_state is not None:
                    commit_state["rollback_failed"] = True
        cleanup_failure = _close_pinned_backup(final_backup_pin)
        if cleanup_failure is not None:
            migration_006._retain_private_cleanup_failure(
                primary,
                stage="m007_backup_seal_close",
                cleanup=cleanup_failure,
            )
        else:
            final_backup_pin = None
        raise
    finally:
        try:
            conn.set_authorizer(None)
        except BaseException:
            pass

    return {
        "database_state": "migrated",
        "applicable": False,
        "reason": "Migration 007 established the canonical closed schema atomically.",
        "mode": "apply",
        "changed": True,
        "durable_commit": True,
        "migration_version": MIGRATION_VERSION,
        "migration_action": (
            "legacy_rebuild" if plan == "legacy_rebuild_pending" else "marker_only"
        ),
        "schema_attestation": final_attestation,
        "table_counts": {"companies": company_count, "jobs": job_count},
        "statement_count": len(statements),
        **(
            {"verified_external_backup": _backup_evidence_summary(backup_seal)}
            if backup_seal is not None
            else {}
        ),
        **failure_injection_accounting(statements),
    }


def verify_committed_database_read_only(
    db_path,
    *,
    ownership,
    expected_precommit_identity,
    failure_injector=None,
    _connect=sqlite3.connect,
) -> dict:
    inject_failure(failure_injector, "post_commit_before_path_validation")
    path = migration_006.canonical_database_path(db_path)
    identity = migration_006.database_file_identity(path)
    if (
        identity is None
        or not _same_file_object(identity, expected_precommit_identity)
    ):
        raise ClosedSchemaConvergenceMigrationError(
            "Post-commit database object identity changed."
        )
    inject_failure(failure_injector, "post_commit_after_path_validation")
    inject_failure(failure_injector, "post_commit_before_sidecar_check")
    if existing_sqlite_sidecars(path):
        raise ClosedSchemaConvergenceMigrationError(
            "Post-commit SQLite sidecar verification failed."
        )
    inject_failure(failure_injector, "post_commit_after_sidecar_check")
    inject_failure(failure_injector, "post_commit_before_ownership_check")
    require_database_lifetime_ownership(
        ownership,
        role=ROLE_OFFLINE_OPERATOR,
        database_path=path,
    )
    inject_failure(failure_injector, "post_commit_after_ownership_check")
    inject_failure(failure_injector, "post_commit_before_reopen")
    conn = migration_006.open_canonical_sqlite_database(
        path,
        read_only=True,
        immutable=True,
        expected_identity=identity,
        connect=_connect,
    )
    try:
        inject_failure(failure_injector, "post_commit_after_reopen")
        inject_failure(
            failure_injector, "post_commit_before_opened_identity_check"
        )
        if not migration_006.opened_database_matches(conn, path, identity):
            raise ClosedSchemaConvergenceMigrationError(
                "Post-commit opened database identity verification failed."
            )
        inject_failure(
            failure_injector, "post_commit_after_opened_identity_check"
        )
        inject_failure(
            failure_injector, "post_commit_before_journal_mode_verification"
        )
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()
        if (
            type(journal_mode) is not tuple
            or len(journal_mode) != 1
            or type(journal_mode[0]) is not str
            or journal_mode[0].casefold() != "delete"
        ):
            raise ClosedSchemaConvergenceMigrationError(
                "Post-commit journal mode verification failed."
            )
        inject_failure(
            failure_injector, "post_commit_after_journal_mode_verification"
        )
        inject_failure(
            failure_injector, "post_commit_before_schema_verification"
        )
        classification = classify_database(conn)
        if classification["database_state"] != "exact_installed":
            raise ClosedSchemaConvergenceMigrationError(
                "Post-commit closed-schema verification failed."
            )
        inject_failure(
            failure_injector, "post_commit_after_schema_verification"
        )
        inject_failure(
            failure_injector, "post_commit_before_quick_check_verification"
        )
        if conn.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
            raise ClosedSchemaConvergenceMigrationError(
                "Post-commit quick check verification failed."
            )
        inject_failure(
            failure_injector, "post_commit_after_quick_check_verification"
        )
        inject_failure(
            failure_injector, "post_commit_before_integrity_verification"
        )
        if conn.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ClosedSchemaConvergenceMigrationError(
                "Post-commit integrity verification failed."
            )
        inject_failure(
            failure_injector, "post_commit_after_integrity_verification"
        )
        inject_failure(
            failure_injector, "post_commit_before_foreign_key_verification"
        )
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ClosedSchemaConvergenceMigrationError(
                "Post-commit foreign-key verification failed."
            )
        inject_failure(
            failure_injector, "post_commit_after_foreign_key_verification"
        )
        inject_failure(
            failure_injector, "post_commit_before_final_identity_sidecar_check"
        )
        if (
            not migration_006.opened_database_matches(conn, path, identity)
            or not _same_file_object(
                migration_006.database_file_identity(path),
                expected_precommit_identity,
            )
            or existing_sqlite_sidecars(path)
        ):
            raise ClosedSchemaConvergenceMigrationError(
                "Post-commit identity verification failed."
            )
        inject_failure(
            failure_injector, "post_commit_after_final_identity_sidecar_check"
        )
        return {
            "database_state": "exact_installed",
            "schema_fingerprint": EXPECTED_SCHEMA_FINGERPRINT,
            "integrity": "ok",
            "quick_check": "ok",
            "journal_mode": "delete",
            "foreign_key_violations": 0,
            "sidecar_count": 0,
            "physical_identity_preserved": True,
        }
    finally:
        try:
            inject_failure(failure_injector, "post_commit_before_close")
        finally:
            conn.close()
        inject_failure(failure_injector, "post_commit_after_close")


def verify_external_backup_evidence(
    database_path,
    backup_argument,
    *,
    expected_target_identity,
    expected_database_state,
    _connect=sqlite3.connect,
) -> dict:
    return _backup_evidence_summary(
        _verify_external_backup_seal(
            database_path,
            backup_argument,
            expected_target_identity=expected_target_identity,
            expected_database_state=expected_database_state,
            _connect=_connect,
        )
    )


def _verify_external_backup_seal(
    database_path,
    backup_argument,
    *,
    expected_target_identity,
    expected_database_state,
    _connect=sqlite3.connect,
) -> _VerifiedBackupSeal:
    conn = None
    try:
        if (
            type(backup_argument) is not str
            or not backup_argument
            or type(expected_target_identity)
            is not migration_006.DatabaseFileIdentity
            or type(expected_database_state) is not str
        ):
            raise _backup_error()
        target = migration_006.canonical_database_path(database_path)
        backup = migration_006.canonical_database_path(backup_argument)
        _require_backup_path_policy(target, backup)
        target_identity = migration_006.database_file_identity(target)
        backup_identity = migration_006.database_file_identity(backup)
        if (
            target_identity != expected_target_identity
            or backup_identity is None
            or existing_sqlite_sidecars(target)
            or existing_sqlite_sidecars(backup)
        ):
            raise _backup_error()
        target_size, target_hash = _hash_exact_regular_file(
            target,
            expected_identity=target_identity,
        )
        backup_size, backup_hash = _hash_exact_regular_file(
            backup,
            expected_identity=backup_identity,
        )
        if target_size != backup_size or target_hash != backup_hash:
            raise _backup_error()
        conn = migration_006.open_canonical_sqlite_database(
            backup,
            read_only=True,
            immutable=True,
            expected_identity=backup_identity,
            connect=_connect,
        )
        classification = classify_database(conn)
        if classification["database_state"] != expected_database_state:
            raise _backup_error()
        state_binding = _classification_seal_binding(classification)
        conn.close()
        conn = None
        if (
            migration_006.database_file_identity(target) != target_identity
            or migration_006.database_file_identity(backup) != backup_identity
            or existing_sqlite_sidecars(target)
            or existing_sqlite_sidecars(backup)
        ):
            raise _backup_error()
        seal = _make_verified_backup_seal(
            target_path=target,
            target_identity=target_identity,
            target_size=target_size,
            target_sha256=target_hash,
            target_state_binding=state_binding,
            backup_path=backup,
            backup_identity=backup_identity,
            backup_size=backup_size,
            backup_sha256=backup_hash,
        )
        _require_verified_backup_seal(
            seal,
            requested_path=target,
            expected_identity=target_identity,
            classification=classification,
        )
        return seal
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except ClosedSchemaConvergenceMigrationError as exc:
        if exc.category == _BACKUP_ERROR_CATEGORY:
            raise
        raise _backup_error() from None
    except BaseException:
        raise _backup_error() from None
    finally:
        if conn is not None:
            try:
                conn.close()
            except BaseException:
                pass


def _backup_evidence_summary(seal) -> dict:
    if type(seal) is not _VerifiedBackupSeal:
        raise _backup_error()
    return {
        "verified": True,
        "external_to_repository": True,
        "identity_distinct": True,
        "size": seal.backup_size,
        "sha256": seal.backup_sha256,
        "database_state": seal.target_database_state,
    }


def _verify_locked_backup(
    conn,
    *,
    requested_path,
    expected_identity,
    backup_seal,
    locked_classification,
    ownership,
):
    require_database_lifetime_ownership(
        ownership,
        role=ROLE_OFFLINE_OPERATOR,
        database_path=requested_path,
    )
    if (
        migration_006.database_file_identity(requested_path)
        != expected_identity
        or not migration_006.opened_database_matches(
            conn, requested_path, expected_identity
        )
        or existing_sqlite_sidecars(requested_path)
    ):
        raise ClosedSchemaConvergenceMigrationError(
            "Migration target identity changed under lock.",
            category="target_changed",
        )
    if backup_seal is None:
        return
    seal = _require_verified_backup_seal(
        backup_seal,
        requested_path=requested_path,
        expected_identity=expected_identity,
        classification=locked_classification,
    )
    _require_backup_path_policy(requested_path, seal.backup_path)
    backup_size, backup_hash = _hash_exact_regular_file(
        seal.backup_path,
        expected_identity=seal.backup_identity,
    )
    if backup_size != seal.backup_size or backup_hash != seal.backup_sha256:
        raise _backup_error()
    image = sqlite3.Connection.serialize(conn, name="main")
    if (
        type(image) is not bytes
        or len(image) != seal.target_size
        or hashlib.sha256(image).hexdigest() != seal.target_sha256
    ):
        raise _backup_error()


def _open_final_backup_seal(
    conn,
    *,
    requested_path,
    expected_identity,
    backup_seal,
):
    if backup_seal is None:
        return None
    seal = _require_verified_backup_seal(
        backup_seal,
        requested_path=requested_path,
        expected_identity=expected_identity,
        classification=None,
    )
    _require_backup_path_policy(requested_path, seal.backup_path)
    pin, size, digest = _open_and_hash_exact_regular_file(
        seal.backup_path,
        expected_identity=seal.backup_identity,
    )
    if size != seal.backup_size or digest != seal.backup_sha256:
        cleanup_failure = _close_pinned_backup(pin)
        error = _backup_error()
        if cleanup_failure is not None:
            migration_006._retain_private_cleanup_failure(
                error,
                stage="m007_invalid_final_backup_seal_close",
                cleanup=cleanup_failure,
            )
        raise error
    return pin


def _verify_final_target_identity(
    conn,
    *,
    requested_path,
    expected_identity,
):
    try:
        canonical = migration_006.canonical_database_path(requested_path)
        current = migration_006.database_file_identity(canonical)
        opened = migration_006.opened_database_path(conn)
        same = opened is not None and os.path.samefile(opened, canonical)
    except (OSError, RuntimeError, ValueError):
        same = False
        current = None
    allowed_journal = os.path.normcase(str(requested_path) + "-journal")
    sidecars = existing_sqlite_sidecars(requested_path)
    if (
        not same
        or not _same_file_object(current, expected_identity)
        or any(
            os.path.normcase(str(sidecar)) != allowed_journal
            for sidecar in sidecars
        )
    ):
        raise ClosedSchemaConvergenceMigrationError(
            "Migration target changed before commit.",
            category="target_changed",
        )


def _verify_final_precommit_state(
    conn,
    *,
    requested_path,
    expected_identity,
    backup_seal,
    backup_pin,
    ownership,
):
    require_database_lifetime_ownership(
        ownership,
        role=ROLE_OFFLINE_OPERATOR,
        database_path=requested_path,
    )
    _verify_final_target_identity(
        conn,
        requested_path=requested_path,
        expected_identity=expected_identity,
    )
    if _classify_database(
        conn, allow_transaction=True
    )["database_state"] != "exact_installed":
        raise ClosedSchemaConvergenceMigrationError(
            "Migration target changed before commit.",
            category="target_changed",
        )
    if backup_seal is not None:
        seal = _require_verified_backup_seal(
            backup_seal,
            requested_path=requested_path,
            expected_identity=expected_identity,
            classification=None,
        )
        if (
            type(backup_pin) is not _PinnedBackupFile
            or backup_pin.path != seal.backup_path
            or backup_pin.identity != seal.backup_identity
        ):
            raise _backup_error()
        _require_backup_path_policy(requested_path, seal.backup_path)
        size, digest = _rehash_pinned_regular_file(backup_pin)
        if size != seal.backup_size or digest != seal.backup_sha256:
            raise _backup_error()
    elif backup_pin is not None:
        raise _backup_error()


def _backup_error():
    return ClosedSchemaConvergenceMigrationError(
        "Verified external backup evidence is invalid.",
        category=_BACKUP_ERROR_CATEGORY,
    )


def _require_backup_path_policy(target_path, backup_path) -> tuple[Path, Path]:
    try:
        target = migration_006.canonical_database_path(target_path)
        backup = migration_006.canonical_database_path(backup_path)
        if (
            os.path.samefile(target, backup)
            or migration_006._is_within(backup, REPOSITORY_ROOT)
            or existing_sqlite_sidecars(backup)
        ):
            raise OSError
        value = backup.stat()
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise OSError
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except BaseException:
        raise _backup_error() from None
    return target, backup


def _identity_from_stat(value) -> migration_006.DatabaseFileIdentity:
    return migration_006.DatabaseFileIdentity(
        device=value.st_dev,
        inode=value.st_ino,
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
    )


def _open_and_hash_exact_regular_file(
    path,
    *,
    expected_identity,
) -> tuple[_PinnedBackupFile, int, str]:
    handle = None
    try:
        if type(expected_identity) is not migration_006.DatabaseFileIdentity:
            raise _backup_error()
        canonical = migration_006.canonical_database_path(path)
        if (
            migration_006.database_file_identity(canonical) != expected_identity
            or existing_sqlite_sidecars(canonical)
        ):
            raise _backup_error()
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(canonical, flags)
        try:
            os.set_inheritable(descriptor, False)
            handle = os.fdopen(descriptor, "rb", closefd=True)
        except BaseException:
            os.close(descriptor)
            raise
        pin = _PinnedBackupFile(
            path=canonical,
            identity=expected_identity,
            handle=handle,
        )
        size, digest = _rehash_pinned_regular_file(pin)
        return pin, size, digest
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        if handle is not None:
            try:
                handle.close()
            except BaseException:
                pass
        raise
    except BaseException:
        if handle is not None:
            try:
                handle.close()
            except BaseException:
                pass
        raise _backup_error() from None


def _hash_exact_regular_file(path, *, expected_identity) -> tuple[int, str]:
    pin = None
    try:
        pin, size, digest = _open_and_hash_exact_regular_file(
            path,
            expected_identity=expected_identity,
        )
        cleanup_failure = _close_pinned_backup(pin)
        pin = None
        if cleanup_failure is not None:
            raise _backup_error()
        return size, digest
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        cleanup_failure = _close_pinned_backup(pin)
        if cleanup_failure is not None:
            pass
        raise
    except BaseException:
        cleanup_failure = _close_pinned_backup(pin)
        if cleanup_failure is not None:
            pass
        raise _backup_error() from None


def _rehash_pinned_regular_file(pin) -> tuple[int, str]:
    try:
        if (
            type(pin) is not _PinnedBackupFile
            or type(pin.identity) is not migration_006.DatabaseFileIdentity
            or not isinstance(pin.path, Path)
            or pin.handle is None
            or pin.handle.closed
        ):
            raise _backup_error()
        canonical = migration_006.canonical_database_path(pin.path)
        if canonical != pin.path or existing_sqlite_sidecars(canonical):
            raise _backup_error()
        descriptor = pin.handle.fileno()
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _identity_from_stat(before) != pin.identity
            or migration_006.database_file_identity(canonical) != pin.identity
        ):
            raise _backup_error()
        pin.handle.seek(0, os.SEEK_SET)
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = pin.handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        if (
            _identity_from_stat(after) != pin.identity
            or after.st_nlink != 1
            or size != pin.identity.size
            or migration_006.database_file_identity(canonical) != pin.identity
            or existing_sqlite_sidecars(canonical)
        ):
            raise _backup_error()
        return size, digest.hexdigest()
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except ClosedSchemaConvergenceMigrationError as exc:
        if exc.category == _BACKUP_ERROR_CATEGORY:
            raise
        raise _backup_error() from None
    except BaseException:
        raise _backup_error() from None


def _close_pinned_backup(pin):
    if pin is None:
        return None
    try:
        if type(pin) is not _PinnedBackupFile:
            raise TypeError("invalid pinned backup")
        pin.close()
    except BaseException as caught:
        return caught
    return None


def _classification_seal_binding(classification) -> tuple[str, int, str, tuple[str, ...]]:
    try:
        if type(classification) is not dict:
            raise TypeError
        state = classification["database_state"]
        attestation = classification["schema_attestation"]
        if type(attestation) is not dict:
            raise TypeError
        object_count = attestation["actual_schema_object_count"]
        fingerprint = attestation["actual_schema_fingerprint"]
        versions_value = attestation["present_migration_versions"]
        if (
            type(state) is not str
            or type(object_count) is not int
            or type(fingerprint) is not str
            or type(versions_value) is not list
            or any(type(item) is not str for item in versions_value)
        ):
            raise TypeError
        versions = tuple(versions_value)
    except (KeyError, TypeError, ValueError):
        raise _backup_error() from None
    return state, object_count, fingerprint, versions


def _normalized_path_text(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _identity_binding(identity) -> dict:
    if type(identity) is not migration_006.DatabaseFileIdentity:
        raise _backup_error()
    return {
        "device": identity.device,
        "inode": identity.inode,
        "size": identity.size,
        "mtime_ns": identity.mtime_ns,
    }


def _verified_backup_binding(
    *,
    target_path,
    target_identity,
    target_size,
    target_sha256,
    target_state_binding,
    backup_path,
    backup_identity,
    backup_size,
    backup_sha256,
) -> str:
    state, object_count, fingerprint, versions = target_state_binding
    payload = {
        "protocol": _BACKUP_EVIDENCE_PROTOCOL,
        "target": {
            "path": _normalized_path_text(target_path),
            "identity": _identity_binding(target_identity),
            "size": target_size,
            "sha256": target_sha256,
            "database_state": state,
            "schema_object_count": object_count,
            "schema_fingerprint": fingerprint,
            "migration_versions": list(versions),
        },
        "backup": {
            "path": _normalized_path_text(backup_path),
            "identity": _identity_binding(backup_identity),
            "size": backup_size,
            "sha256": backup_sha256,
        },
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _make_verified_backup_seal(
    *,
    target_path,
    target_identity,
    target_size,
    target_sha256,
    target_state_binding,
    backup_path,
    backup_identity,
    backup_size,
    backup_sha256,
):
    state, object_count, fingerprint, versions = target_state_binding
    binding = _verified_backup_binding(
        target_path=target_path,
        target_identity=target_identity,
        target_size=target_size,
        target_sha256=target_sha256,
        target_state_binding=target_state_binding,
        backup_path=backup_path,
        backup_identity=backup_identity,
        backup_size=backup_size,
        backup_sha256=backup_sha256,
    )
    return _VerifiedBackupSeal(
        protocol=_BACKUP_EVIDENCE_PROTOCOL,
        target_path=target_path,
        target_identity=target_identity,
        target_size=target_size,
        target_sha256=target_sha256,
        target_database_state=state,
        target_schema_object_count=object_count,
        target_schema_fingerprint=fingerprint,
        target_migration_versions=versions,
        backup_path=backup_path,
        backup_identity=backup_identity,
        backup_size=backup_size,
        backup_sha256=backup_sha256,
        binding=binding,
    )


def _require_verified_backup_seal(
    seal,
    *,
    requested_path,
    expected_identity,
    classification,
):
    try:
        target = migration_006.canonical_database_path(requested_path)
        if (
            type(seal) is not _VerifiedBackupSeal
            or type(seal.target_identity) is not migration_006.DatabaseFileIdentity
            or type(seal.backup_identity) is not migration_006.DatabaseFileIdentity
            or seal.protocol != _BACKUP_EVIDENCE_PROTOCOL
            or seal.target_path != target
            or seal.target_identity != expected_identity
        ):
            raise TypeError
        state_binding = (
            seal.target_database_state,
            seal.target_schema_object_count,
            seal.target_schema_fingerprint,
            seal.target_migration_versions,
        )
        if (
            classification is not None
            and _classification_seal_binding(classification) != state_binding
        ):
            raise TypeError
        expected_binding = _verified_backup_binding(
            target_path=seal.target_path,
            target_identity=seal.target_identity,
            target_size=seal.target_size,
            target_sha256=seal.target_sha256,
            target_state_binding=state_binding,
            backup_path=seal.backup_path,
            backup_identity=seal.backup_identity,
            backup_size=seal.backup_size,
            backup_sha256=seal.backup_sha256,
        )
        if seal.binding != expected_binding:
            raise TypeError
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except BaseException:
        raise _backup_error() from None
    return seal


def existing_sqlite_sidecars(path: Path) -> tuple[Path, ...]:
    database_name = path.name
    direct_names = {
        os.path.normcase(database_name + "-journal"),
        os.path.normcase(database_name + "-wal"),
        os.path.normcase(database_name + "-shm"),
    }
    master_prefix = os.path.normcase(database_name + "-mj")
    super_journal_prefix = os.path.normcase(
        database_name + "-super-journal"
    )
    candidates = []
    try:
        with os.scandir(path.parent) as entries:
            for entry in entries:
                comparable = os.path.normcase(entry.name)
                if (
                    comparable in direct_names
                    or comparable.startswith(master_prefix)
                    or comparable.startswith(super_journal_prefix)
                ):
                    candidates.append(path.parent / entry.name)
    except OSError:
        candidates.append(Path(str(path) + "-sidecar-scan-unavailable"))
    return tuple(
        sorted(
            set(candidates),
            key=lambda item: os.path.normcase(str(item)),
        )
    )


def _same_file_object(current, expected) -> bool:
    return (
        type(current) is migration_006.DatabaseFileIdentity
        and type(expected) is migration_006.DatabaseFileIdentity
        and current.device == expected.device
        and current.inode == expected.inode
    )


def _require_stable_owned_target(
    connection,
    *,
    target_path,
    expected_identity,
    ownership,
) -> None:
    require_database_lifetime_ownership(
        ownership,
        role=ROLE_OFFLINE_OPERATOR,
        database_path=target_path,
    )
    if existing_sqlite_sidecars(target_path):
        raise ClosedSchemaConvergenceMigrationError(
            "SQLite sidecar state prevents migration 007.",
            category="sqlite_sidecar_present",
        )
    if (
        migration_006.database_file_identity(target_path)
        != expected_identity
        or not migration_006.opened_database_matches(
            connection,
            target_path,
            expected_identity,
        )
    ):
        raise ClosedSchemaConvergenceMigrationError(
            "Migration target identity changed.",
            category="target_changed",
        )


def _sqlite_failure_state(error: sqlite3.Error) -> str:
    code = getattr(error, "sqlite_errorcode", None)
    if type(code) is int and (code & 0xFF) in {
        getattr(sqlite3, "SQLITE_BUSY", 5),
        getattr(sqlite3, "SQLITE_LOCKED", 6),
    }:
        return "database_busy"
    return "invalid_sqlite"


def operation_names(statements=None) -> tuple[str, ...]:
    statements = tuple(
        statements
        if statements is not None
        else iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8"))
    )
    names = []
    for statement in statements:
        words = statement.split()
        verb = words[0].lower()
        if verb == "insert":
            name = f"insert_{words[1].lower()}_{words[2]}"
        else:
            object_type = words[1].lower()
            offset = 2
            if object_type == "unique":
                object_type = words[2].lower()
                offset = 3
            name = f"{verb}_{object_type}_{words[offset]}"
        names.append(_slug(name))
    if len(names) != len(set(names)):
        raise ClosedSchemaConvergenceMigrationError(
            "Migration operation names must be unique."
        )
    return tuple(names)


def failure_injection_points(statements=None) -> tuple[str, ...]:
    statements = tuple(
        iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8"))
        if statements is None
        else statements
    )
    points = [
        "before_ownership_acquisition",
        "after_ownership_acquisition",
        "before_target_open",
        "after_target_open",
        "before_private_worker_open",
        "after_private_worker_open",
        "before_begin_immediate",
        "after_begin_immediate",
    ]
    if statements:
        points.extend(
            (
                "before_defer_foreign_keys",
                "after_defer_foreign_keys",
            )
        )
    for boundary in (
        "locked_schema_classification",
        "locked_ownership_check",
        "locked_identity_sidecar_check",
        "locked_backup_check",
        "preserved_manifest_capture",
        "sequence_capture",
    ):
        points.extend((f"before_{boundary}", f"after_{boundary}"))
    for index, operation in enumerate(operation_names(statements), start=1):
        points.extend(
            (
                f"before_operation_{index}_{operation}",
                f"after_operation_{index}_{operation}",
            )
        )
        if operation == "create_table_jobs_m007_backup":
            points.extend(
                (
                    "before_shadow_verification",
                    "after_shadow_verification",
                )
            )
        if operation == "create_index_idx_jobs_canonical_opportunity":
            points.extend(
                (
                    "before_sequence_restoration",
                    "after_sequence_restoration",
                    "before_rebuilt_data_verification",
                    "after_rebuilt_data_verification",
                )
            )
    for boundary in (
        "marker_write",
        "closed_schema_attestation",
        "sequence_authority_check",
        "preserved_manifest_check",
        "journal_mode_check",
        "quick_check",
        "integrity_check",
        "foreign_key_check",
        "final_ownership_check",
        "final_backup_seal",
        "final_target_identity_check",
    ):
        points.extend((f"before_{boundary}", f"after_{boundary}"))
    points.append("before_commit")
    return tuple(points)


def post_commit_failure_injection_points() -> tuple[str, ...]:
    return (
        "after_commit",
        "post_commit_before_path_validation",
        "post_commit_after_path_validation",
        "post_commit_before_sidecar_check",
        "post_commit_after_sidecar_check",
        "post_commit_before_ownership_check",
        "post_commit_after_ownership_check",
        "post_commit_before_reopen",
        "post_commit_after_reopen",
        "post_commit_before_opened_identity_check",
        "post_commit_after_opened_identity_check",
        "post_commit_before_journal_mode_verification",
        "post_commit_after_journal_mode_verification",
        "post_commit_before_schema_verification",
        "post_commit_after_schema_verification",
        "post_commit_before_quick_check_verification",
        "post_commit_after_quick_check_verification",
        "post_commit_before_integrity_verification",
        "post_commit_after_integrity_verification",
        "post_commit_before_foreign_key_verification",
        "post_commit_after_foreign_key_verification",
        "post_commit_before_final_identity_sidecar_check",
        "post_commit_after_final_identity_sidecar_check",
        "post_commit_before_close",
        "post_commit_after_close",
    )


def failure_injection_accounting(statements=None) -> dict:
    statements = tuple(
        iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8"))
        if statements is None
        else statements
    )
    return {
        "fault_injection_hook_count": len(failure_injection_points(statements)),
        "post_commit_fault_injection_hook_count": len(
            post_commit_failure_injection_points()
        ),
        "durable_state_checkpoint_count": len(statements) + 2,
    }


def inject_failure(callback, point):
    if callback is not None:
        callback(point)


def _canonical_orphan_exists(conn) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM jobs AS job "
            "LEFT JOIN canonical_opportunities AS opportunity "
            "ON opportunity.id = job.canonical_opportunity_id "
            "WHERE job.canonical_opportunity_id IS NOT NULL "
            "AND opportunity.id IS NULL LIMIT 1"
        ).fetchone()
        is not None
    )


def _sequence_rows(conn) -> tuple[tuple[int, str, int], ...]:
    rows = conn.execute(
        "SELECT rowid, name, seq FROM sqlite_sequence ORDER BY rowid"
    ).fetchall()
    result = []
    for row in rows:
        if (
            type(row) is not tuple
            or len(row) != 3
            or type(row[0]) is not int
            or row[0] <= 0
            or type(row[1]) is not str
            or not row[1]
            or type(row[2]) is not int
            or row[2] < 0
        ):
            raise ClosedSchemaConvergenceMigrationError(
                "AUTOINCREMENT authority is invalid."
            )
        result.append((row[0], row[1], row[2]))
    if len({row[1] for row in result}) != len(result):
        raise ClosedSchemaConvergenceMigrationError(
            "AUTOINCREMENT authority contains duplicate table names."
        )
    return tuple(result)


def _restore_sequence_rows(conn, original):
    original_map = {name: (rowid, value) for rowid, name, value in original}
    for table in ("companies", "jobs"):
        authority = original_map.get(table)
        maximum = conn.execute(
            f'SELECT COALESCE(MAX(id), 0) FROM "{table}"'
        ).fetchone()[0]
        if authority is not None and authority[1] < maximum:
            raise ClosedSchemaConvergenceMigrationError(
                "AUTOINCREMENT authority is below a retained identifier."
            )
        conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
        if authority is not None:
            conn.execute(
                "INSERT INTO sqlite_sequence(rowid, name, seq) VALUES (?, ?, ?)",
                (authority[0], table, authority[1]),
            )
    if _sequence_rows(conn) != original:
        raise ClosedSchemaConvergenceMigrationError(
            "AUTOINCREMENT authority could not be restored exactly."
        )


def _require_tables_equivalent(conn, first, second, columns):
    if _table_count(conn, first) != _table_count(conn, second):
        raise ClosedSchemaConvergenceMigrationError(
            "Rebuilt table row count differs from its source."
        )
    projection = ", ".join(_quote_identifier(column) for column in columns)
    first_name = _quote_identifier(first)
    second_name = _quote_identifier(second)
    for left, right in ((first_name, second_name), (second_name, first_name)):
        row = conn.execute(
            "SELECT 1 FROM (SELECT "
            + projection
            + " FROM "
            + left
            + " EXCEPT SELECT "
            + projection
            + " FROM "
            + right
            + ") LIMIT 1"
        ).fetchone()
        if row is not None:
            raise ClosedSchemaConvergenceMigrationError(
                "Rebuilt table values differ from their source."
            )


def _preserved_manifest(conn) -> dict:
    objects = tuple(
        row
        for row in conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE type IN ('table','index','trigger','view') "
            "ORDER BY type, name, tbl_name"
        )
        if row[1] not in {"companies", "jobs"}
        and row[2] not in {"companies", "jobs"}
        and not row[1].endswith("_m007_backup")
    )
    counts = []
    for (table,) in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        if table in {
            "companies",
            "jobs",
            "wahojobs_schema_migrations",
            "companies_m007_backup",
            "jobs_m007_backup",
        }:
            continue
        counts.append((table, _table_count(conn, table)))
    return {"objects": objects, "table_counts": tuple(counts)}


def _table_count(conn, table):
    return conn.execute(
        f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
    ).fetchone()[0]


def _quote_identifier(value):
    return '"' + str(value).replace('"', '""') + '"'


def _slug(value):
    return "".join(
        character if character.isalnum() else "_" for character in value
    ).strip("_")


def _classification(state, applicable, reason, *, mode=None):
    result = {
        "database_state": state,
        "applicable": applicable,
        "reason": reason,
    }
    if mode is not None:
        result.update({"mode": mode, "changed": False})
    return result


def _post_commit_verification_failure(*, cleanup=False):
    return {
        "database_state": (
            "migrated_cleanup_incomplete"
            if cleanup
            else "migrated_verification_failed"
        ),
        "applicable": False,
        "mode": "apply",
        "changed": True,
        "durable_commit": True,
        "migration_version": MIGRATION_VERSION,
        "reason": (
            "Migration 007 committed durably, but cleanup did not complete."
            if cleanup
            else "Migration 007 committed durably, but read-only verification failed."
        ),
    }


def _exit(args, result, *, error):
    if result is None:
        result = {
            **_classification(
                "migration_failed", False, "Migration 007 did not produce a result."
            ),
            "migration_version": MIGRATION_VERSION,
        }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Closed Schema Convergence Migration 007")
        print("=======================================")
        print(f"Database state: {result.get('database_state')}")
        print(f"Mode: {result.get('mode', 'inspection')}")
        print(f"Changed: {'yes' if result.get('changed') else 'no'}")
        print(f"Reason: {result.get('reason')}")
    raise SystemExit(1 if error else 0)


if __name__ == "__main__":
    main()
