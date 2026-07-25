from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.persistent_profiles_migration as migration_004  # noqa: E402
from wahojobs.google_oidc_authorization_transaction_schema import (  # noqa: E402
    EXPECTED_SCHEMA_FINGERPRINT,
    MIGRATION_PATH,
    MIGRATION_VERSION,
    PREREQUISITE_MIGRATION_VERSIONS,
    TRANSACTION_INDEXES,
    TRANSACTION_TABLE,
    TRANSACTION_TRIGGERS,
    attest_google_oidc_authorization_transaction_schema,
    expected_google_oidc_authorization_transaction_manifest,
    iter_sql_statements,
)
from wahojobs.google_oidc_authorization_transaction_reconciliation import (  # noqa: E402
    reconcile_google_oidc_authorization_transactions,
)


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_MAX_MIGRATION_AUTHORIZER_CALLS = 262_144
_MAX_SQLITE_FUNCTION_INVENTORY = 4_096
_MAX_SEALED_SCHEMA_OBJECTS = 8_192
_CALLBACK_CLEAR_PASSES = 8
_MIGRATION_MARKER_INSERT_SQL = (
    "INSERT INTO wahojobs_schema_migrations(version) VALUES ('"
    + MIGRATION_VERSION.replace("'", "''")
    + "')"
)
_MIGRATION_006_INDEX_NAMES = frozenset(TRANSACTION_INDEXES)
_MIGRATION_006_TRIGGER_NAMES = frozenset(TRANSACTION_TRIGGERS)
_MIGRATION_006_AUTOINDEX_PREFIX = (
    "sqlite_autoindex_" + TRANSACTION_TABLE + "_"
)


class GoogleOidcAuthorizationTransactionsMigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DatabaseFileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class LockedTargetComparison:
    locked_size: int
    locked_sha256: str
    requested_path: Path | None = None
    expected_identity: DatabaseFileIdentity | None = None
    backup_path: Path | None = None
    backup_identity: DatabaseFileIdentity | None = None
    backup_size: int | None = None
    backup_sha256: str | None = None


class _MigrationAuthorizer:
    __slots__ = ("_calls", "_phase", "_safe_functions")

    def __init__(self, safe_functions):
        self._calls = 0
        self._phase = "verification"
        self._safe_functions = frozenset(safe_functions)

    def permit_migration_statements(self) -> None:
        self._phase = "ddl"

    def permit_marker_insert(self) -> None:
        self._phase = "marker"

    def permit_verification_only(self) -> None:
        self._phase = "verification"

    def permit_commit(self) -> None:
        self._phase = "commit"

    def permit_rollback(self) -> None:
        self._phase = "rollback"

    def __call__(self, action, first, second, database, _source):
        self._calls += 1
        if self._calls > _MAX_MIGRATION_AUTHORIZER_CALLS:
            return sqlite3.SQLITE_DENY

        if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE}:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            return (
                sqlite3.SQLITE_OK
                if database in {"main", "temp", None}
                else sqlite3.SQLITE_DENY
            )
        if action == sqlite3.SQLITE_FUNCTION:
            permitted = (
                {
                    "glob",
                    "length",
                    "lower",
                    "strftime",
                    "substr",
                    "trim",
                    "typeof",
                }
                if self._phase == "ddl"
                else self._safe_functions
            )
            return (
                sqlite3.SQLITE_OK
                if type(second) is str and second in permitted
                else sqlite3.SQLITE_DENY
            )
        if action == sqlite3.SQLITE_PRAGMA:
            return (
                sqlite3.SQLITE_OK
                if (
                    type(first) is str
                    and first.lower()
                    in {
                        "collation_list",
                        "database_list",
                        "foreign_key_check",
                        "foreign_key_list",
                        "foreign_keys",
                        "function_list",
                        "index_info",
                        "index_list",
                        "index_xinfo",
                        "integrity_check",
                        "page_count",
                        "query_only",
                        "recursive_triggers",
                        "table_info",
                        "table_xinfo",
                    }
                    and (
                        second in {None, ""}
                        or first.lower()
                        in {
                            "foreign_key_list",
                            "index_info",
                            "index_list",
                            "index_xinfo",
                            "integrity_check",
                            "recursive_triggers",
                            "table_info",
                            "table_xinfo",
                        }
                    )
                )
                else sqlite3.SQLITE_DENY
            )
        if action == sqlite3.SQLITE_TRANSACTION:
            permitted = {
                "verification": "BEGIN",
                "commit": "COMMIT",
                "rollback": "ROLLBACK",
            }.get(self._phase)
            return (
                sqlite3.SQLITE_OK
                if first == permitted
                else sqlite3.SQLITE_DENY
            )
        if database != "main":
            return sqlite3.SQLITE_DENY
        if self._phase == "marker":
            return (
                sqlite3.SQLITE_OK
                if (
                    action == sqlite3.SQLITE_INSERT
                    and first == "wahojobs_schema_migrations"
                )
                else sqlite3.SQLITE_DENY
            )
        if self._phase != "ddl":
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_CREATE_TABLE:
            return (
                sqlite3.SQLITE_OK
                if first == TRANSACTION_TABLE
                else sqlite3.SQLITE_DENY
            )
        if action == sqlite3.SQLITE_CREATE_INDEX:
            return (
                sqlite3.SQLITE_OK
                if (
                    (
                        first in _MIGRATION_006_INDEX_NAMES
                        and second == TRANSACTION_TABLE
                    )
                    or (
                        first == _MIGRATION_006_AUTOINDEX_PREFIX + "1"
                        and second == TRANSACTION_TABLE
                    )
                )
                else sqlite3.SQLITE_DENY
            )
        if action == sqlite3.SQLITE_CREATE_TRIGGER:
            return (
                sqlite3.SQLITE_OK
                if (
                    first in _MIGRATION_006_TRIGGER_NAMES
                    and second == TRANSACTION_TABLE
                )
                else sqlite3.SQLITE_DENY
            )
        if action == sqlite3.SQLITE_REINDEX:
            return (
                sqlite3.SQLITE_OK
                if first in _MIGRATION_006_INDEX_NAMES
                else sqlite3.SQLITE_DENY
            )
        if action == sqlite3.SQLITE_INSERT:
            return (
                sqlite3.SQLITE_OK
                if first in {"sqlite_master", "sqlite_schema"}
                else sqlite3.SQLITE_DENY
            )
        if action == sqlite3.SQLITE_UPDATE:
            return (
                sqlite3.SQLITE_OK
                if (
                    first in {"sqlite_master", "sqlite_schema"}
                    and second
                    in {"type", "name", "tbl_name", "rootpage", "sql"}
                )
                else sqlite3.SQLITE_DENY
            )
        return sqlite3.SQLITE_DENY


def main(*, failure_injector=None, _connect=sqlite3.connect):
    args = parse_args()
    mode = "apply" if args.yes else "inspection"
    try:
        db_path = canonical_database_path(args.db)
    except GoogleOidcAuthorizationTransactionsMigrationError:
        _exit(
            args,
            _classification(
                "nonexistent",
                False,
                "Database path does not exist or is not a safe regular file; "
                "this migration never creates a database.",
                mode=mode,
            ),
            error=True,
        )
    workspace_database = migration_004.is_workspace_database_file(db_path)
    if workspace_database and not args.allow_workspace_db:
        _exit(
            args,
            _classification(
                "workspace_database_blocked",
                False,
                "Workspace database access requires --allow-workspace-db after separate review.",
                mode=mode,
            ),
            error=True,
        )
    initial_identity = database_file_identity(db_path)
    if initial_identity is None:
        _exit(
            args,
            _classification(
                "nonexistent",
                False,
                "Database path does not exist or is not a safe regular file; "
                "this migration never creates a database.",
                mode=mode,
            ),
            error=True,
        )
    sidecars = existing_sqlite_sidecars(db_path)
    if sidecars:
        _exit(
            args,
            {
                **_classification(
                    "sqlite_sidecar_present",
                    False,
                    "Migration 006 refuses a database with a WAL, SHM, or journal sidecar.",
                    mode=mode,
                ),
                "sidecar_suffixes": [path.name[len(db_path.name) :] for path in sidecars],
            },
            error=True,
        )

    backup_evidence = None
    if args.yes and workspace_database:
        try:
            backup_evidence = verify_external_backup_evidence(
                db_path,
                args.verified_backup,
                expected_target_identity=initial_identity,
                _connect=_connect,
            )
            try:
                inject_failure(
                    failure_injector, "after_preliminary_backup_validation"
                )
            except BaseException:
                _exit(
                    args,
                    _classification(
                        "verified_external_backup_required",
                        False,
                        "Preliminary backup validation did not complete safely.",
                        mode="apply",
                    ),
                    error=True,
                )
        except GoogleOidcAuthorizationTransactionsMigrationError as exc:
            _exit(
                args,
                _classification(
                    "verified_external_backup_required",
                    False,
                    str(exc),
                    mode="apply",
                ),
                error=True,
            )

    commit_state = {"committed": False, "rollback_failed": False}
    classification = None
    result = None
    conn = None
    try:
        inject_failure(failure_injector, "before_target_open")
        conn = open_canonical_sqlite_database(
            db_path,
            read_only=not args.yes,
            expected_identity=initial_identity,
            connect=_connect,
        )
        try:
            inject_failure(failure_injector, "after_target_open")
            if args.yes:
                _seal_migration_connection(conn)
            if (
                not args.allow_workspace_db
                and migration_004.is_workspace_database_file(
                    opened_database_path(conn)
                )
            ):
                raise GoogleOidcAuthorizationTransactionsMigrationError(
                    "Workspace database access requires --allow-workspace-db "
                    "after separate review."
                )
            classification = classify_database(conn)
            if args.yes:
                result = apply_google_oidc_authorization_transactions_migration(
                    conn,
                    classification=classification,
                    failure_injector=failure_injector,
                    requested_path=db_path,
                    expected_identity=initial_identity,
                    backup_evidence=backup_evidence,
                    commit_state=commit_state,
                )
            else:
                if (
                    not opened_database_matches(
                        conn, db_path, initial_identity
                    )
                    or existing_sqlite_sidecars(db_path)
                ):
                    raise GoogleOidcAuthorizationTransactionsMigrationError(
                        "Database identity changed during read-only inspection."
                    )
                result = {
                    **classification,
                    "mode": "inspection",
                    "changed": False,
                    "migration_version": MIGRATION_VERSION,
                }
        finally:
            if type(conn) is sqlite3.Connection:
                _remove_private_migration_authorizer(conn)
                sqlite3.Connection.close(conn)
            else:
                conn.close()
            conn = None

        if args.yes:
            read_only_verification = verify_committed_database_read_only(
                db_path,
                failure_injector=failure_injector,
                _connect=_connect,
            )
            result["post_commit_read_only_verification"] = read_only_verification
            if backup_evidence is not None:
                result["verified_external_backup"] = _public_backup_evidence(
                    backup_evidence
                )
    except sqlite3.DatabaseError:
        if commit_state["committed"]:
            _exit(
                args,
                _post_commit_verification_failure(),
                error=True,
            )
        _exit(
            args,
            _classification(
                "invalid_or_locked_sqlite",
                False,
                "SQLite inspection or migration failed.",
                mode=mode,
            ),
            error=True,
        )
    except GoogleOidcAuthorizationTransactionsMigrationError:
        if commit_state["committed"]:
            _exit(
                args,
                _post_commit_verification_failure(),
                error=True,
            )
        _exit(
            args,
            {
                **(
                    classification
                    if classification is not None
                    else _classification(
                        "migration_failed",
                        False,
                        "Migration 006 validation or apply failed before commit.",
                    )
                ),
                "mode": mode,
                "changed": False,
                "migration_version": MIGRATION_VERSION,
                "reason": (
                    "Migration 006 failed before commit and rollback could not "
                    "be verified."
                    if commit_state["rollback_failed"]
                    else "Migration 006 validation or apply failed before commit."
                ),
            },
            error=True,
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        if commit_state["committed"]:
            _exit(args, _post_commit_verification_failure(), error=True)
        _exit(
            args,
            {
                **(
                    classification
                    if classification is not None
                    else _classification(
                        "migration_failed",
                        False,
                        "Migration or inspection was interrupted before commit.",
                    )
                ),
                "mode": mode,
                "changed": False,
                "migration_version": MIGRATION_VERSION,
                "reason": "Migration or inspection was interrupted before commit.",
            },
            error=True,
        )
    except Exception:
        if commit_state["committed"]:
            _exit(args, _post_commit_verification_failure(), error=True)
        _exit(
            args,
            {
                **(
                    classification
                    if classification is not None
                    else _classification(
                        "migration_failed",
                        False,
                        "Migration or inspection failed before commit.",
                    )
                ),
                "mode": mode,
                "changed": False,
                "migration_version": MIGRATION_VERSION,
                "reason": "Migration or inspection failed before commit.",
            },
            error=True,
        )
    _exit(args, result, error=False)


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or explicitly install dormant Google OIDC authorization "
            "transaction migration 006."
        )
    )
    parser.add_argument("--db", required=True, help="SQLite database path to inspect.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Explicitly apply migration 006. Without this flag the command is read-only.",
    )
    parser.add_argument(
        "--allow-workspace-db",
        action="store_true",
        help="Allow workspace database access after a separately reviewed authorization.",
    )
    parser.add_argument(
        "--verified-backup",
        help=(
            "Existing external exact-copy backup evidence; required only for "
            "an authorized workspace apply."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit stable JSON output.")
    return parser.parse_args()


def classify_database(conn) -> dict:
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        return _classification(
            "integrity_invalid", False, "Database integrity check failed."
        )
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        return _classification(
            "foreign_key_invalid", False, "Database foreign-key validation failed."
        )
    attestation = attest_google_oidc_authorization_transaction_schema(conn)
    state_map = {
        "pending": (
            "pending",
            True,
            "Migration 006 is ready to apply.",
        ),
        "correctly_installed": (
            "exact_installed",
            False,
            "Migration 006 is exactly installed.",
        ),
        "partial_inconsistent": (
            "partial",
            False,
            "Migration 006 has an incomplete object or marker installation.",
        ),
        "conflicting": (
            "conflicting",
            False,
            "Migration 006 has a same-name or owned-namespace conflict.",
        ),
        "schema_mismatch": (
            "drifted",
            False,
            "Migration 006 definitions differ from the canonical schema.",
        ),
        "residue": (
            "residue",
            False,
            "Migration 006 owned objects exist in the temporary schema.",
        ),
        "invalid_prerequisite": (
            "invalid_prerequisite",
            False,
            "Migrations 001 through 005 must attest exactly before migration 006.",
        ),
    }
    database_state, applicable, reason = state_map.get(
        attestation["state"],
        (
            "drifted",
            False,
            "Migration 006 schema state is not recognized as safe.",
        ),
    )
    result = {
        **_classification(database_state, applicable, reason),
        "schema_attestation": attestation,
    }
    if attestation["state"] == "correctly_installed":
        result["authorization_transaction_count"] = conn.execute(
            f'SELECT COUNT(*) FROM "{TRANSACTION_TABLE}"'
        ).fetchone()[0]
    return result


def apply_google_oidc_authorization_transactions_migration(
    conn,
    *,
    classification=None,
    failure_injector=None,
    requested_path=None,
    expected_identity=None,
    backup_evidence=None,
    commit_state=None,
):
    if commit_state is not None:
        commit_state["committed"] = False
        commit_state["rollback_failed"] = False
    private_authorizer = None
    try:
        private_authorizer = _seal_migration_connection(conn)
        if conn.in_transaction:
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "Migration requires an idle, migration-owned connection."
            )
        _enable_and_verify_recursive_triggers(conn)
        _require_migration_connection_contract(conn)
        statements = list(
            iter_sql_statements(
                MIGRATION_PATH.read_text(encoding="utf-8")
            )
        )
        operations = operation_names(statements)
        expected_manifest = (
            expected_google_oidc_authorization_transaction_manifest()
        )
        classification = classify_database(conn)
        if classification["database_state"] == "exact_installed":
            return {
                **classification,
                "mode": "apply",
                "changed": False,
                "migration_version": MIGRATION_VERSION,
                "statement_count": len(statements),
                **failure_injection_accounting(statements),
            }
        if classification["database_state"] != "pending":
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                classification["reason"]
            )

        transaction_started = False
        rollback_failed = False
        try:
            inject_failure(failure_injector, "before_begin_immediate")
            private_authorizer = _seal_migration_connection(conn)
            _require_migration_connection_contract(conn)
            sqlite3.Connection.execute(conn, "BEGIN IMMEDIATE")
            transaction_started = True

            private_authorizer = _seal_migration_connection(conn)
            _enable_and_verify_recursive_triggers(conn)
            _require_migration_connection_contract(conn)
            locked = classify_database(conn)
            if locked["database_state"] != "pending":
                raise GoogleOidcAuthorizationTransactionsMigrationError(
                    "Migration 006 prerequisite state changed before the "
                    "write lock."
                )
            locked_live_fingerprint = (
                _serialized_main_database_fingerprint(conn)
            )
            if locked_live_fingerprint is None:
                raise GoogleOidcAuthorizationTransactionsMigrationError(
                    "Migration target live-image verification is "
                    "unavailable."
                )
            inject_failure(failure_injector, "after_begin_immediate")
            inject_failure(
                failure_injector,
                "before_locked_prerequisite_attestation",
            )
            inject_failure(
                failure_injector,
                "after_locked_prerequisite_attestation",
            )
            _inject_preseal_execution_failure_points(
                failure_injector,
                operations,
            )
            private_authorizer = _seal_migration_connection(conn)
            _enable_and_verify_recursive_triggers(conn)
            _require_migration_connection_contract(conn)
            preserved_before = _preserved_database_manifest(conn)
            locked_comparison, locked_classification = (
                _verify_locked_target_before_migration(
                    conn,
                    requested_path=(
                        Path(requested_path)
                        if requested_path is not None
                        else None
                    ),
                    expected_identity=expected_identity,
                    backup_evidence=backup_evidence,
                    locked_live_fingerprint=locked_live_fingerprint,
                )
            )
            private_authorizer = _seal_migration_connection(
                conn,
                final=True,
            )
            private_authorizer.permit_migration_statements()
            _verify_sealed_target_before_migration(
                conn,
                locked_comparison,
            )
            sqlite3.Connection.execute(conn, statements[0])

            for statement in statements[1:]:
                sqlite3.Connection.execute(conn, statement)

            private_authorizer.permit_marker_insert()
            sqlite3.Connection.execute(
                conn,
                _MIGRATION_MARKER_INSERT_SQL,
            )
            private_authorizer.permit_verification_only()
            attestation = _verify_sealed_migration_result(
                conn,
                locked_classification=locked_classification,
                expected_manifest=expected_manifest,
            )
            transaction_count = 0
            preserved_after = preserved_before

            if not conn.in_transaction:
                raise GoogleOidcAuthorizationTransactionsMigrationError(
                    "Migration transaction ended before commit."
                )
            private_authorizer.permit_commit()
            sqlite3.Connection.commit(conn)
            transaction_started = False
            if commit_state is not None:
                commit_state["committed"] = True
            inject_failure(failure_injector, "after_commit")
        except BaseException:
            if transaction_started:
                if private_authorizer is not None:
                    private_authorizer.permit_rollback()
                rollback_succeeded = _rollback_owned_transaction(conn)
                if not rollback_succeeded:
                    if commit_state is not None:
                        commit_state["rollback_failed"] = True
                    rollback_failed = True
            if not rollback_failed:
                raise
        if rollback_failed:
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "Migration 006 failed before commit and rollback could not "
                "be verified."
            )

        return {
            "database_state": "migrated",
            "applicable": False,
            "reason": (
                "Migration 006 applied atomically with empty dormant "
                "transaction state."
            ),
            "mode": "apply",
            "changed": True,
            "migration_version": MIGRATION_VERSION,
            "schema_attestation": attestation,
            "authorization_transaction_count": transaction_count,
            "empty_reconciliation_status": "clean",
            "preserved_object_count": len(preserved_after["objects"]),
            "preserved_table_count": len(preserved_after["row_counts"]),
            "statement_count": len(statements),
            **failure_injection_accounting(statements),
        }
    finally:
        if private_authorizer is not None:
            _remove_private_migration_authorizer(conn)


def verify_committed_database_read_only(
    db_path: Path,
    *,
    failure_injector=None,
    _connect=sqlite3.connect,
) -> dict:
    inject_failure(failure_injector, "post_commit_before_path_validation")
    db_path = canonical_database_path(db_path)
    identity = database_file_identity(db_path)
    if identity is None:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Post-commit database identity verification failed."
        )
    inject_failure(failure_injector, "post_commit_after_path_validation")
    inject_failure(failure_injector, "post_commit_before_sidecar_check")
    sidecars = existing_sqlite_sidecars(db_path)
    if sidecars:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "A SQLite sidecar remained after migration 006 commit."
        )
    inject_failure(failure_injector, "post_commit_after_sidecar_check")
    inject_failure(failure_injector, "post_commit_before_reopen")
    conn = open_canonical_sqlite_database(
        db_path,
        read_only=True,
        expected_identity=identity,
        connect=_connect,
        immutable=True,
    )
    try:
        inject_failure(failure_injector, "post_commit_after_reopen")
        inject_failure(
            failure_injector, "post_commit_before_opened_identity_check"
        )
        _require_stable_opened_database(conn, db_path, identity)
        inject_failure(
            failure_injector, "post_commit_after_opened_identity_check"
        )
        inject_failure(
            failure_injector, "post_commit_before_schema_verification"
        )
        classification = classify_database(conn)
        if classification["database_state"] != "exact_installed":
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "Read-only post-commit migration 006 verification failed."
            )
        inject_failure(
            failure_injector, "post_commit_after_schema_verification"
        )
        inject_failure(
            failure_injector, "post_commit_before_integrity_verification"
        )
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "Read-only post-commit integrity verification failed."
            )
        inject_failure(
            failure_injector, "post_commit_after_integrity_verification"
        )
        inject_failure(
            failure_injector, "post_commit_before_foreign_key_verification"
        )
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "Read-only post-commit foreign-key verification failed."
            )
        inject_failure(
            failure_injector, "post_commit_after_foreign_key_verification"
        )
        inject_failure(
            failure_injector, "post_commit_before_reconciliation"
        )
        reconciliation = reconcile_google_oidc_authorization_transactions(
            conn,
            accepted_lookup_key_versions=(1,),
            accepted_protection_key_versions=(1,),
            max_findings=0,
            summary_only=True,
            source_guarantees_no_sidecar_creation=True,
        )
        if (
            reconciliation.status != "clean"
            or not reconciliation.complete
            or reconciliation.total_findings != 0
        ):
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "Read-only post-commit empty reconciliation failed."
            )
        inject_failure(
            failure_injector, "post_commit_after_reconciliation"
        )
        inject_failure(
            failure_injector, "post_commit_before_final_identity_check"
        )
        _require_stable_opened_database(conn, db_path, identity)
        inject_failure(
            failure_injector, "post_commit_after_final_identity_check"
        )
        return {
            "database_state": classification["database_state"],
            "schema_fingerprint": classification["schema_attestation"][
                "actual_schema_fingerprint"
            ],
            "authorization_transaction_count": classification[
                "authorization_transaction_count"
            ],
            "empty_reconciliation_status": reconciliation.status,
            "integrity": "ok",
            "foreign_key_violations": 0,
        }
    finally:
        try:
            inject_failure(failure_injector, "post_commit_before_close")
        finally:
            conn.close()
        inject_failure(failure_injector, "post_commit_after_close")


def verify_external_backup_evidence(
    database_path: Path,
    backup_argument: str | None,
    *,
    expected_target_identity=None,
    _connect=sqlite3.connect,
) -> dict:
    if type(backup_argument) is not str or not backup_argument:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Workspace apply requires --verified-backup with an external exact copy."
        )
    database_path = canonical_database_path(database_path)
    try:
        backup_path = canonical_database_path(backup_argument)
    except GoogleOidcAuthorizationTransactionsMigrationError:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Verified external backup path does not exist."
        ) from None
    target_identity = database_file_identity(database_path)
    backup_identity = database_file_identity(backup_path)
    if (
        target_identity is None
        or backup_identity is None
        or (
            expected_target_identity is not None
            and target_identity != expected_target_identity
        )
    ):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration target identity changed before backup verification."
        )
    if _same_file(database_path, backup_path):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Verified external backup must be a distinct file."
        )
    if _is_within(backup_path, REPOSITORY_ROOT):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Verified external backup must be outside the repository."
        )
    if existing_sqlite_sidecars(backup_path):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Verified external backup must not have SQLite sidecars."
        )
    database_digest = _file_sha256(database_path)
    backup_digest = _file_sha256(backup_path)
    if database_path.stat().st_size != backup_path.stat().st_size:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Verified external backup size does not match the migration target."
        )
    if database_digest != backup_digest:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Verified external backup digest does not match the migration target."
        )
    conn = open_canonical_sqlite_database(
        backup_path,
        read_only=True,
        expected_identity=backup_identity,
        connect=_connect,
    )
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "Verified external backup failed its integrity check."
            )
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "Verified external backup failed its foreign-key check."
            )
        classification = classify_database(conn)
        if classification["database_state"] not in {"pending", "exact_installed"}:
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "Verified external backup does not match a safe M006 database state."
            )
        preserved_manifest = _preserved_database_manifest(conn)
        _require_stable_opened_database(conn, backup_path, backup_identity)
    finally:
        conn.close()
    if (
        database_file_identity(database_path) != target_identity
        or database_file_identity(backup_path) != backup_identity
        or existing_sqlite_sidecars(database_path)
        or existing_sqlite_sidecars(backup_path)
    ):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Database or verified backup identity changed during validation."
        )
    return {
        "verified": True,
        "external_to_repository": True,
        "identity_distinct": True,
        "size": backup_path.stat().st_size,
        "sha256": backup_digest,
        "database_state": classification["database_state"],
        "integrity": "ok",
        "foreign_key_violations": 0,
        "_backup_path": backup_path,
        "_backup_identity": backup_identity,
        "_target_identity": target_identity,
        "_preserved_manifest": preserved_manifest,
    }


def canonical_database_path(value) -> Path:
    try:
        expanded = Path(value).expanduser()
        lexical_absolute = Path(os.path.abspath(os.fspath(expanded)))
        if _path_contains_filesystem_alias(lexical_absolute):
            raise OSError
        path = lexical_absolute.resolve(strict=True)
        value_stat = path.stat()
        if (
            os.path.normcase(os.path.normpath(str(path)))
            != os.path.normcase(os.path.normpath(str(lexical_absolute)))
            or not path.is_file()
            or value_stat.st_nlink != 1
        ):
            raise OSError
    except (OSError, RuntimeError, TypeError, ValueError):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Database path is unavailable or unsafe."
        ) from None
    return path


def _path_contains_filesystem_alias(path: Path) -> bool:
    chain = [path, *path.parents]
    for component in reversed(chain):
        if component == component.parent:
            continue
        try:
            value = os.lstat(component)
        except OSError:
            return True
        if stat.S_ISLNK(value.st_mode):
            return True
        attributes = getattr(value, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if reparse_flag and attributes & reparse_flag:
            return True
    return False


def database_file_identity(path: Path) -> DatabaseFileIdentity | None:
    try:
        value = path.stat()
    except (OSError, ValueError):
        return None
    return DatabaseFileIdentity(
        device=value.st_dev,
        inode=value.st_ino,
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
    )


def sqlite_file_uri(
    path: Path,
    *,
    read_only: bool,
    immutable: bool = False,
) -> str:
    canonical = canonical_database_path(path)
    if immutable and not read_only:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Immutable SQLite opens must be read-only."
        )
    parts = urlsplit(canonical.as_uri())
    parameters = [("mode", "ro" if read_only else "rw")]
    if immutable:
        parameters.append(("immutable", "1"))
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(parameters),
            "",
        )
    )


def open_canonical_sqlite_database(
    path: Path,
    *,
    read_only: bool,
    expected_identity: DatabaseFileIdentity | None = None,
    connect=sqlite3.connect,
    timeout: float = 2.0,
    immutable: bool = False,
):
    canonical = canonical_database_path(path)
    identity = database_file_identity(canonical)
    if identity is None or (
        expected_identity is not None and identity != expected_identity
    ):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Database identity changed before open."
        )
    connection = None
    try:
        connection = connect(
            sqlite_file_uri(
                canonical,
                read_only=read_only,
                immutable=immutable,
            ),
            uri=True,
            timeout=timeout,
        )
        if not read_only:
            _clear_migration_connection_callbacks(connection)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA recursive_triggers = ON")
        if read_only:
            connection.execute("PRAGMA query_only = ON")
        if (
            connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
            or connection.execute("PRAGMA recursive_triggers").fetchone()[0] != 1
        ):
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "SQLite connection enforcement could not be established."
            )
        if not opened_database_matches(connection, canonical, identity):
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "Opened SQLite main database does not match the requested file."
            )
        return connection
    except BaseException:
        if connection is not None:
            try:
                connection.close()
            except BaseException:
                pass
        raise


def opened_database_path(connection) -> Path | None:
    try:
        rows = connection.execute("PRAGMA database_list").fetchall()
    except BaseException:
        return None
    main_rows = [row for row in rows if len(row) >= 3 and row[1] == "main"]
    if len(main_rows) != 1 or type(main_rows[0][2]) is not str:
        return None
    try:
        return Path(main_rows[0][2]).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None


def opened_database_matches(
    connection,
    requested_path: Path,
    expected_identity: DatabaseFileIdentity,
) -> bool:
    opened = opened_database_path(connection)
    if opened is None:
        return False
    try:
        requested = canonical_database_path(requested_path)
        same = os.path.samefile(opened, requested)
    except (GoogleOidcAuthorizationTransactionsMigrationError, OSError, ValueError):
        return False
    return (
        same
        and database_file_identity(opened) == expected_identity
        and database_file_identity(requested) == expected_identity
    )


def _require_stable_opened_database(
    connection,
    requested_path: Path,
    expected_identity: DatabaseFileIdentity,
) -> None:
    if (
        not opened_database_matches(
            connection, requested_path, expected_identity
        )
        or existing_sqlite_sidecars(requested_path)
    ):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Database identity or SQLite sidecar state changed."
        )


def _verify_locked_target_before_migration(
    connection,
    *,
    requested_path: Path | None,
    expected_identity: DatabaseFileIdentity | None,
    backup_evidence: dict | None,
    locked_live_fingerprint,
) -> tuple[LockedTargetComparison, dict]:
    locked_size, locked_digest = _require_exact_live_fingerprint(
        locked_live_fingerprint
    )
    if requested_path is not None:
        if type(expected_identity) is not DatabaseFileIdentity:
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "Migration target identity evidence is unavailable."
            )
        _require_stable_opened_database(
            connection, requested_path, expected_identity
        )
    authoritative_classification = classify_database(connection)
    if authoritative_classification["database_state"] != "pending":
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration 006 prerequisite state changed under the write lock."
        )
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration target failed its under-lock integrity check."
        )
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration target failed its under-lock foreign-key check."
        )
    authoritative_preserved_manifest = _preserved_database_manifest(connection)
    if backup_evidence is None:
        return (
            LockedTargetComparison(
                locked_size=locked_size,
                locked_sha256=locked_digest,
                requested_path=requested_path,
                expected_identity=expected_identity,
            ),
            authoritative_classification,
        )

    if type(backup_evidence) is not dict or requested_path is None:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Verified backup evidence is not authoritative."
        )
    try:
        backup_path = backup_evidence["_backup_path"]
        backup_identity = backup_evidence["_backup_identity"]
        target_identity = backup_evidence["_target_identity"]
        backup_size_evidence = backup_evidence["size"]
        backup_digest_evidence = backup_evidence["sha256"]
        backup_database_state = backup_evidence["database_state"]
        backup_preserved_manifest = backup_evidence[
            "_preserved_manifest"
        ]
    except (KeyError, TypeError):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Verified backup evidence is incomplete."
        ) from None
    if (
        type(backup_path) is not type(requested_path)
        or type(backup_identity) is not DatabaseFileIdentity
        or type(target_identity) is not DatabaseFileIdentity
        or type(backup_size_evidence) is not int
        or backup_size_evidence <= 0
        or type(backup_digest_evidence) is not str
        or re.fullmatch(
            r"[0-9a-f]{64}",
            backup_digest_evidence,
        )
        is None
        or backup_database_state != "pending"
        or database_file_identity(backup_path) != backup_identity
        or existing_sqlite_sidecars(backup_path)
        or database_file_identity(requested_path) != expected_identity
        or expected_identity != target_identity
    ):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Verified backup evidence changed before migration."
        )
    target_size = requested_path.stat().st_size
    backup_size = backup_path.stat().st_size
    target_digest = _file_sha256(requested_path)
    backup_digest = _file_sha256(backup_path)
    if (
        target_size != backup_size_evidence
        or backup_size != backup_size_evidence
        or target_digest != backup_digest_evidence
        or backup_digest != backup_digest_evidence
        or authoritative_preserved_manifest
        != backup_preserved_manifest
    ):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration target no longer exactly matches the verified backup."
        )
    _require_stable_opened_database(
        connection, requested_path, expected_identity
    )
    if (
        database_file_identity(backup_path) != backup_identity
        or existing_sqlite_sidecars(backup_path)
    ):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Verified backup evidence changed during under-lock comparison."
        )
    return (
        LockedTargetComparison(
            locked_size=locked_size,
            locked_sha256=locked_digest,
            requested_path=requested_path,
            expected_identity=expected_identity,
            backup_path=backup_path,
            backup_identity=backup_identity,
            backup_size=backup_size_evidence,
            backup_sha256=backup_digest_evidence,
        ),
        authoritative_classification,
    )


def _verify_sealed_target_before_migration(
    connection,
    comparison: LockedTargetComparison,
) -> None:
    if not connection.in_transaction:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration write transaction changed during sealing."
        )
    _require_migration_connection_contract(connection)
    _require_only_main_and_temp_databases(connection)

    expected_size = comparison.locked_size
    expected_digest = comparison.locked_sha256
    if comparison.requested_path is not None:
        if type(comparison.expected_identity) is not DatabaseFileIdentity:
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "Migration target identity evidence is unavailable."
            )
        _require_stable_opened_database(
            connection,
            comparison.requested_path,
            comparison.expected_identity,
        )
    if comparison.backup_path is not None:
        if (
            comparison.requested_path is None
            or type(comparison.backup_identity)
            is not DatabaseFileIdentity
            or type(comparison.backup_size) is not int
            or type(comparison.backup_sha256) is not str
            or database_file_identity(comparison.backup_path)
            != comparison.backup_identity
            or existing_sqlite_sidecars(comparison.backup_path)
            or comparison.backup_path.stat().st_size
            != comparison.backup_size
            or _file_sha256(comparison.backup_path)
            != comparison.backup_sha256
            or comparison.requested_path.stat().st_size
            != comparison.backup_size
            or _file_sha256(comparison.requested_path)
            != comparison.backup_sha256
        ):
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "Verified backup evidence changed during sealed "
                "comparison."
            )
        expected_size = comparison.backup_size
        expected_digest = comparison.backup_sha256

    live_fingerprint = _serialized_main_database_fingerprint(connection)
    if (
        type(live_fingerprint) is not tuple
        or len(live_fingerprint) != 2
        or type(live_fingerprint[0]) is not int
        or type(live_fingerprint[1]) is not str
    ):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration target live-image verification is unavailable."
        )
    live_size, live_digest = live_fingerprint
    if (
        live_size != expected_size
        or live_digest != expected_digest
    ):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration target live image changed before migration."
        )


def _serialized_main_database_fingerprint(connection):
    serialized_main = None
    try:
        serialized_main = sqlite3.Connection.serialize(
            connection,
            name="main",
        )
        if type(serialized_main) is not bytes:
            return None
        return (
            len(serialized_main),
            hashlib.sha256(serialized_main).hexdigest(),
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except BaseException as failure:
        serialized_main = None
        try:
            failure.__traceback__ = None
            failure.__cause__ = None
            failure.__context__ = None
        except BaseException:
            pass
        failure = None
        return None
    finally:
        serialized_main = None


def _require_exact_live_fingerprint(value) -> tuple[int, str]:
    if (
        type(value) is not tuple
        or len(value) != 2
        or type(value[0]) is not int
        or value[0] <= 0
        or type(value[1]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", value[1]) is None
    ):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration target live-image verification is unavailable."
        )
    return value


def _clear_migration_connection_callbacks(connection) -> None:
    if type(connection) is not sqlite3.Connection:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration requires an exact standard-library SQLite "
            "connection."
        )
    try:
        for _unused in range(_CALLBACK_CLEAR_PASSES):
            sqlite3.Connection.row_factory.__set__(connection, None)
            sqlite3.Connection.text_factory.__set__(connection, str)
            sqlite3.Connection.set_trace_callback(connection, None)
            sqlite3.Connection.set_progress_handler(connection, None, 0)
            sqlite3.Connection.set_authorizer(connection, None)
    except BaseException:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration connection callbacks could not be disabled."
        ) from None


def _stabilize_private_authorizer(
    connection,
    authorizer,
) -> None:
    try:
        for _unused in range(_CALLBACK_CLEAR_PASSES):
            sqlite3.Connection.row_factory.__set__(connection, None)
            sqlite3.Connection.text_factory.__set__(connection, str)
            sqlite3.Connection.set_trace_callback(connection, None)
            sqlite3.Connection.set_progress_handler(connection, None, 0)
            sqlite3.Connection.set_authorizer(connection, authorizer)
    except BaseException:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration connection callbacks could not be disabled."
        ) from None


def _require_pristine_function_inventory(connection) -> tuple:
    target_functions = _sqlite_function_inventory(connection)
    reference = sqlite3.Connection(":memory:")
    try:
        reference_functions = _sqlite_function_inventory(reference)
    finally:
        sqlite3.Connection.close(reference)
    if target_functions != reference_functions:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration connection function state is not pristine."
        )
    return target_functions


def _seal_migration_connection(
    connection,
    *,
    final=False,
) -> _MigrationAuthorizer:
    _clear_migration_connection_callbacks(connection)

    bootstrap_authorizer = _MigrationAuthorizer(())
    try:
        _stabilize_private_authorizer(connection, bootstrap_authorizer)
        try:
            sqlite3.Connection.create_collation(
                connection,
                "BINARY",
                None,
            )
        except BaseException:
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "Migration connection binary collation could not be "
                "restored."
            ) from None
        # Releasing the caller's BINARY callback can execute arbitrary object
        # finalizers. Clear everything those finalizers may have reinstalled
        # before issuing any more SQL. This runs on every seal so a hostile
        # BINARY callback cannot execute even during preliminary
        # classification before BEGIN IMMEDIATE.
        _clear_migration_connection_callbacks(connection)
        _stabilize_private_authorizer(
            connection,
            bootstrap_authorizer,
        )

        target_functions = _require_pristine_function_inventory(connection)
    except BaseException:
        _remove_private_migration_authorizer(connection)
        raise

    safe_functions = {
        row[0]
        for row in target_functions
        if len(row) >= 1 and type(row[0]) is str
    }
    authorizer = _MigrationAuthorizer(safe_functions)
    try:
        # This is the last callback-releasing sequence before verification
        # SQL. It is intentionally multi-pass: a displaced callback's
        # finalizer may try to register another callback.
        _stabilize_private_authorizer(connection, authorizer)
        _require_only_main_and_temp_databases(connection)
        denied = False
        try:
            sqlite3.Connection.execute(
                connection,
                "EXPLAIN DELETE FROM main.wahojobs_schema_migrations",
            ).fetchone()
        except sqlite3.DatabaseError:
            denied = True
        if not denied:
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "Migration private authorizer could not be verified."
            )
        if (
            connection.row_factory is not None
            or connection.text_factory is not str
        ):
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "Migration connection callback state changed during seal."
            )
        binary_probe = sqlite3.Connection.execute(
            connection,
            "SELECT CAST(1 AS INTEGER) "
            "WHERE CAST('a' AS TEXT) = "
            "CAST('a' AS TEXT) COLLATE BINARY",
        ).fetchone()
        if binary_probe != (1,):
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "Migration connection binary collation is unavailable."
            )
        return authorizer
    except BaseException:
        _remove_private_migration_authorizer(connection)
        raise


def _remove_private_migration_authorizer(connection) -> None:
    try:
        if type(connection) is sqlite3.Connection:
            sqlite3.Connection.set_authorizer(connection, None)
    except BaseException:
        pass


def _sqlite_function_inventory(connection) -> tuple:
    return tuple(
        sorted(
            _bounded_pragma_rows(
                connection,
                "PRAGMA function_list",
                _MAX_SQLITE_FUNCTION_INVENTORY,
            )
        )
    )


def _bounded_pragma_rows(connection, statement, limit) -> tuple:
    cursor = sqlite3.Connection.execute(connection, statement)
    try:
        rows = cursor.fetchmany(limit + 1)
    finally:
        cursor.close()
    if len(rows) > limit:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration connection callback inventory exceeded its bound."
        )
    return tuple(tuple(row) for row in rows)


def _require_only_main_and_temp_databases(connection) -> None:
    rows = _bounded_pragma_rows(
        connection,
        "PRAGMA database_list",
        3,
    )
    names = tuple(row[1] for row in rows if len(row) >= 3)
    if (
        len(names) != len(rows)
        or names.count("main") != 1
        or any(name not in {"main", "temp"} for name in names)
    ):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration connection has an unexpected attached database."
        )


def _verify_sealed_migration_result(
    connection,
    *,
    locked_classification,
    expected_manifest,
) -> dict:
    actual_manifest = _capture_sealed_m006_manifest(connection)
    expected_without_fingerprint = {
        key: expected_manifest[key]
        for key in (
            "objects",
            "definitions",
            "tables",
            "index_details",
        )
    }
    if (
        actual_manifest != expected_without_fingerprint
        or expected_manifest.get("fingerprint")
        != EXPECTED_SCHEMA_FINGERPRINT
    ):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration 006 sealed schema verification failed."
        )

    marker_cursor = sqlite3.Connection.execute(
        connection,
        "SELECT CAST(version AS BLOB) "
        "FROM main.wahojobs_schema_migrations",
    )
    try:
        marker_rows = marker_cursor.fetchmany(
            len(PREREQUISITE_MIGRATION_VERSIONS) + 2
        )
    finally:
        marker_cursor.close()
    expected_markers = tuple(
        sorted(
            version.encode("utf-8")
            for version in (
                *PREREQUISITE_MIGRATION_VERSIONS,
                MIGRATION_VERSION,
            )
        )
    )
    if (
        len(marker_rows) != len(expected_markers)
        or tuple(sorted(row[0] for row in marker_rows))
        != expected_markers
    ):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration 006 sealed marker verification failed."
        )

    unexpected_row = sqlite3.Connection.execute(
        connection,
        f'SELECT CAST(1 AS INTEGER) FROM "{TRANSACTION_TABLE}" LIMIT 1',
    ).fetchone()
    if unexpected_row is not None:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration 006 created unexpected authorization transaction "
            "rows."
        )
    return _sealed_installed_attestation(
        locked_classification["schema_attestation"],
        expected_manifest,
    )


def _capture_sealed_m006_manifest(connection) -> dict:
    cursor = sqlite3.Connection.execute(
        connection,
        "SELECT CAST(type AS BLOB), CAST(name AS BLOB), "
        "CAST(tbl_name AS BLOB), CAST(sql AS BLOB) "
        "FROM main.sqlite_schema",
    )
    try:
        rows = cursor.fetchmany(_MAX_SEALED_SCHEMA_OBJECTS + 1)
    finally:
        cursor.close()
    if len(rows) > _MAX_SEALED_SCHEMA_OBJECTS:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration 006 sealed schema inventory exceeded its bound."
        )

    raw_objects = []
    for row in rows:
        if len(row) != 4:
            raise GoogleOidcAuthorizationTransactionsMigrationError(
                "Migration 006 sealed schema metadata is invalid."
            )
        kind = _decode_schema_blob(row[0])
        name = _decode_schema_blob(row[1])
        table_name = _decode_schema_blob(row[2])
        sql = (
            None
            if row[3] is None
            else _decode_schema_blob(row[3])
        )
        if _is_migration_006_object(name, table_name):
            raw_objects.append((kind, name, table_name, sql))
    raw_objects.sort(key=lambda item: (item[0], item[1], item[2]))
    objects = tuple(
        (kind, name, table_name)
        for kind, name, table_name, _ in raw_objects
    )
    definitions = {
        name: _normalize_schema_sql(sql)
        for _, name, _, sql in raw_objects
        if sql is not None
    }

    table_identifier = _quote_identifier(TRANSACTION_TABLE)
    columns = tuple(
        tuple(row[index] for index in range(7))
        for row in sqlite3.Connection.execute(
            connection,
            f"PRAGMA main.table_xinfo({table_identifier})",
        )
    )
    foreign_keys = tuple(
        sorted(
            tuple(row[index] for index in range(8))
            for row in sqlite3.Connection.execute(
                connection,
                f"PRAGMA main.foreign_key_list({table_identifier})",
            )
        )
    )
    indexes = []
    index_details = {}
    for row in sqlite3.Connection.execute(
        connection,
        f"PRAGMA main.index_list({table_identifier})",
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
                for item in sqlite3.Connection.execute(
                    connection,
                    "PRAGMA main.index_xinfo("
                    + _quote_identifier(name)
                    + ")",
                )
            ),
        }
    return {
        "objects": objects,
        "definitions": definitions,
        "tables": {
            TRANSACTION_TABLE: {
                "columns": columns,
                "foreign_keys": foreign_keys,
                "indexes": tuple(sorted(indexes)),
            }
        },
        "index_details": index_details,
    }


def _decode_schema_blob(value) -> str:
    if type(value) is not bytes:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration 006 sealed schema metadata is invalid."
        )
    try:
        return value.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration 006 sealed schema metadata is invalid."
        ) from None


def _normalize_schema_sql(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().rstrip(";"))


def _sealed_installed_attestation(
    pending_attestation,
    expected_manifest,
) -> dict:
    result = dict(pending_attestation)
    expected_objects = list(result["expected_objects"])
    result.update(
        {
            "state": "correctly_installed",
            "present_migration_versions": sorted(
                (
                    *PREREQUISITE_MIGRATION_VERSIONS,
                    MIGRATION_VERSION,
                )
            ),
            "migration_marker_present": True,
            "marker_lineage_valid": True,
            "findings": [],
            "finding_categories": [],
            "blocking": False,
            "applicable": False,
            "actual_schema_fingerprint": EXPECTED_SCHEMA_FINGERPRINT,
            "schema_fingerprint_matches": True,
            "present_expected_object_count": len(
                expected_manifest["objects"]
            ),
            "present_objects": expected_objects,
            "temporary_owned_objects": [],
        }
    )
    return result


def _public_backup_evidence(evidence: dict) -> dict:
    return {
        key: value
        for key, value in evidence.items()
        if not key.startswith("_")
    }


def _post_commit_verification_failure() -> dict:
    return {
        "database_state": "migrated_verification_failed",
        "applicable": False,
        "mode": "apply",
        "changed": True,
        "durable_commit": True,
        "post_commit_read_only_verification": {
            "status": "failed",
        },
        "migration_version": MIGRATION_VERSION,
        "reason": (
            "Migration 006 commit is durable, but post-commit read-only "
            "verification failed."
        ),
    }


def _rollback_owned_transaction(connection) -> bool:
    try:
        sqlite3.Connection.rollback(connection)
    except BaseException:
        return False
    try:
        return not connection.in_transaction
    except BaseException:
        return False


def _enable_and_verify_recursive_triggers(connection) -> None:
    try:
        sqlite3.Connection.execute(
            connection,
            "PRAGMA recursive_triggers = ON",
        )
        enabled = sqlite3.Connection.execute(
            connection,
            "PRAGMA recursive_triggers",
        ).fetchone()
    except sqlite3.Error:
        enabled = None
    if enabled is None or len(enabled) != 1 or enabled[0] != 1:
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration requires recursive-trigger enforcement."
        )


def _require_migration_connection_contract(connection) -> None:
    try:
        foreign_keys = sqlite3.Connection.execute(
            connection,
            "PRAGMA foreign_keys",
        ).fetchone()
        recursive_triggers = sqlite3.Connection.execute(
            connection,
            "PRAGMA recursive_triggers",
        ).fetchone()
    except sqlite3.Error:
        foreign_keys = None
        recursive_triggers = None
    if (
        foreign_keys is None
        or len(foreign_keys) != 1
        or foreign_keys[0] != 1
        or recursive_triggers is None
        or len(recursive_triggers) != 1
        or recursive_triggers[0] != 1
    ):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration connection enforcement changed before mutation."
        )


def existing_sqlite_sidecars(path: Path) -> tuple[Path, ...]:
    result = []
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        candidate = Path(str(path) + suffix)
        try:
            if candidate.exists():
                result.append(candidate)
        except OSError:
            result.append(candidate)
    return tuple(result)


def operation_names(statements=None) -> tuple[str, ...]:
    statements = statements or list(
        iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8"))
    )
    names = []
    for statement in statements:
        words = statement.split()
        object_offset = 1
        if words[1].upper() == "UNIQUE":
            object_offset = 2
        object_type = words[object_offset].lower()
        object_name = words[object_offset + 1].split("(", 1)[0]
        names.append(_slug(f"create_{object_type}_{object_name}"))
    if len(names) != len(set(names)):
        raise GoogleOidcAuthorizationTransactionsMigrationError(
            "Migration 006 operation names must be unique."
        )
    return tuple(names)


def failure_injection_points(statements=None) -> tuple[str, ...]:
    statements = statements or list(
        iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8"))
    )
    points = [
        "before_begin_immediate",
        "after_begin_immediate",
        "before_locked_prerequisite_attestation",
        "after_locked_prerequisite_attestation",
        "before_locked_target_reverification",
    ]
    for index, operation in enumerate(operation_names(statements), start=1):
        points.extend(
            (
                f"before_operation_{index}_{operation}",
                f"after_operation_{index}_{operation}",
            )
        )
    for boundary in (
        "marker_write",
        "schema_attestation",
        "empty_reconciliation",
        "integrity_check",
        "foreign_key_check",
        "preserved_object_check",
    ):
        points.extend((f"before_{boundary}", f"after_{boundary}"))
    points.append("before_commit")
    return tuple(points)


def workspace_backup_race_points() -> tuple[str, ...]:
    return (
        "after_preliminary_backup_validation",
        "before_target_open",
        "after_target_open",
        "before_locked_target_reverification",
    )


def post_commit_failure_injection_points() -> tuple[str, ...]:
    return (
        "after_commit",
        "post_commit_before_path_validation",
        "post_commit_after_path_validation",
        "post_commit_before_sidecar_check",
        "post_commit_after_sidecar_check",
        "post_commit_before_reopen",
        "post_commit_after_reopen",
        "post_commit_before_opened_identity_check",
        "post_commit_after_opened_identity_check",
        "post_commit_before_schema_verification",
        "post_commit_after_schema_verification",
        "post_commit_before_integrity_verification",
        "post_commit_after_integrity_verification",
        "post_commit_before_foreign_key_verification",
        "post_commit_after_foreign_key_verification",
        "post_commit_before_reconciliation",
        "post_commit_after_reconciliation",
        "post_commit_before_final_identity_check",
        "post_commit_after_final_identity_check",
        "post_commit_before_close",
        "post_commit_after_close",
    )


def failure_injection_accounting(statements=None) -> dict:
    statements = statements or list(
        iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8"))
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


def _inject_preseal_execution_failure_points(callback, operations) -> None:
    inject_failure(callback, "before_locked_target_reverification")
    for index, operation in enumerate(operations, start=1):
        inject_failure(
            callback,
            f"before_operation_{index}_{operation}",
        )
        inject_failure(
            callback,
            f"after_operation_{index}_{operation}",
        )
    for boundary in (
        "marker_write",
        "schema_attestation",
        "empty_reconciliation",
        "integrity_check",
        "foreign_key_check",
        "preserved_object_check",
    ):
        inject_failure(callback, f"before_{boundary}")
        inject_failure(callback, f"after_{boundary}")
    inject_failure(callback, "before_commit")


def _preserved_database_manifest(conn) -> dict:
    objects = tuple(
        sorted(
            (row[0], row[1], row[2], row[3])
            for row in sqlite3.Connection.execute(
                conn,
                "SELECT type, name, tbl_name, sql FROM main.sqlite_master "
                "WHERE type IN ('table', 'index', 'trigger', 'view')"
            )
            if not _is_migration_006_object(row[1], row[2])
        )
    )
    row_counts = tuple(
        sorted(
            (
                row[0],
                sqlite3.Connection.execute(
                    conn,
                    f"SELECT COUNT(*) FROM {_quote_identifier(row[0])}"
                ).fetchone()[0],
            )
            for row in sqlite3.Connection.execute(
                conn,
                "SELECT name FROM main.sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' "
                "AND name NOT IN ('wahojobs_schema_migrations', '"
                + TRANSACTION_TABLE.replace("'", "''")
                + "')",
            )
        )
    )
    return {"objects": objects, "row_counts": row_counts}


def _is_migration_006_object(name: str, table_name: str) -> bool:
    return (
        name == TRANSACTION_TABLE
        or table_name == TRANSACTION_TABLE
        or name.startswith(
            (
                "idx_google_oidc_authorization_transactions_",
                "uq_google_oidc_authorization_transactions_",
                "trg_google_oidc_authorization_transactions_",
                "sqlite_autoindex_google_oidc_authorization_transactions_",
            )
        )
    )


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _same_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except (OSError, ValueError):
        return os.path.normcase(str(first.resolve())) == os.path.normcase(
            str(second.resolve())
        )


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False


def _slug(value: str) -> str:
    return "".join(
        character if character.isalnum() else "_" for character in value
    ).strip("_")


def _classification(state, applicable, reason, *, mode=None):
    result = {"database_state": state, "applicable": applicable, "reason": reason}
    if mode:
        result.update({"mode": mode, "changed": False})
    return result


def _exit(args, result, *, error):
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Google OIDC Authorization Transactions Migration 006")
        print("====================================================")
        print(f"Mode: {result.get('mode', 'inspection')}")
        print(f"Database state: {result.get('database_state', 'unknown')}")
        print(f"Applicable: {'yes' if result.get('applicable') else 'no'}")
        print(f"Changed: {'yes' if result.get('changed') else 'no'}")
        print(f"Reason: {result.get('reason', '')}")
    raise SystemExit(1 if error else 0)


if __name__ == "__main__":
    main()
