"""Apply M008 to an explicitly supplied, already-open SQLite database."""

from __future__ import annotations

import sqlite3

from wahojobs.workos_authkit_schema import (
    MIGRATION_PATH,
    MIGRATION_VERSION,
    attest_workos_authkit_schema,
    iter_sql_statements,
)


class WorkOSAuthKitProviderMigrationError(Exception):
    """One sanitized failure for an unsafe or incomplete M008 attempt."""

    __slots__ = ()

    def __init__(self):
        super().__init__("workos_authkit_provider_migration_unavailable")


def apply_workos_authkit_provider_migration(
    connection: sqlite3.Connection,
    *,
    failure_injector=None,
) -> dict:
    """Atomically expand the identity provider check on one attested M007 DB."""

    before_rows = None
    after_rows = None
    try:
        if (
            type(connection) is not sqlite3.Connection
            or connection.in_transaction
            or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
            or connection.execute("PRAGMA query_only").fetchone()[0] != 0
        ):
            raise WorkOSAuthKitProviderMigrationError()
        initial = attest_workos_authkit_schema(connection)
        if initial["state"] == "correctly_installed":
            return {
                "migration_version": MIGRATION_VERSION,
                "state": "correctly_installed",
                "applied": False,
            }
        if initial["state"] != "provider_expansion_pending":
            raise WorkOSAuthKitProviderMigrationError()

        before_rows = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT auth_identity_id, user_id, provider, provider_subject, "
                "verified_email, email_verified, created_at, last_authenticated_at, "
                "disabled_at, link_idempotency_key, request_fingerprint "
                "FROM auth_identities ORDER BY auth_identity_id"
            ).fetchall()
        )
        _inject(failure_injector, "before_begin")
        connection.execute("BEGIN IMMEDIATE")
        for ordinal, statement in enumerate(
            iter_sql_statements(MIGRATION_PATH.read_text(encoding="utf-8")),
            start=1,
        ):
            connection.execute(statement)
            _inject(failure_injector, f"after_statement_{ordinal}")
        connection.execute(
            "INSERT INTO wahojobs_schema_migrations(version) VALUES (?)",
            (MIGRATION_VERSION,),
        )
        _inject(failure_injector, "after_marker")

        after_rows = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT auth_identity_id, user_id, provider, provider_subject, "
                "verified_email, email_verified, created_at, last_authenticated_at, "
                "disabled_at, link_idempotency_key, request_fingerprint "
                "FROM auth_identities ORDER BY auth_identity_id"
            ).fetchall()
        )
        if before_rows != after_rows:
            raise WorkOSAuthKitProviderMigrationError()
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise WorkOSAuthKitProviderMigrationError()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or tuple(integrity) != ("ok",):
            raise WorkOSAuthKitProviderMigrationError()
        final = attest_workos_authkit_schema(connection)
        if final["state"] != "correctly_installed":
            raise WorkOSAuthKitProviderMigrationError()
        _inject(failure_injector, "before_commit")
        connection.commit()
        return {
            "migration_version": MIGRATION_VERSION,
            "state": "correctly_installed",
            "applied": True,
        }
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        if type(connection) is sqlite3.Connection and connection.in_transaction:
            connection.rollback()
        raise
    except Exception as exc:
        if type(connection) is sqlite3.Connection and connection.in_transaction:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
        _detach_exception(exc)
        raise WorkOSAuthKitProviderMigrationError() from None
    finally:
        before_rows = None
        after_rows = None
        failure_injector = None
        connection = None


def _inject(callback, point):
    if callback is not None:
        callback(point)


def _detach_exception(exc):
    try:
        exc.__traceback__ = None
        exc.__cause__ = None
        exc.__context__ = None
    except (AttributeError, TypeError):
        pass


__all__ = [
    "WorkOSAuthKitProviderMigrationError",
    "apply_workos_authkit_provider_migration",
]
