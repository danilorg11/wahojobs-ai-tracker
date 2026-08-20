"""Atomically apply M009 to an explicitly supplied open SQLite database.

This is a callable migration boundary, not a command-line activation surface.
It creates no database, opens no configured path, allocates no public identity,
and performs no route activation.
"""

from __future__ import annotations

import sqlite3

from wahojobs.public_job_identity import reconcile_public_job_identity
from wahojobs.public_job_identity_schema import (
    MIGRATION_PATH,
    MIGRATION_VERSION,
    attest_public_job_identity_schema,
)
from wahojobs.workos_authkit_schema import iter_sql_statements


class PublicJobIdentityMigrationError(Exception):
    """One sanitized failure for an unsafe or incomplete M009 attempt."""

    __slots__ = ()

    def __init__(self):
        super().__init__("public_job_identity_migration_unavailable")


def apply_public_job_identity_migration(
    connection: sqlite3.Connection,
    *,
    failure_injector=None,
) -> dict:
    """Install empty M009 authorities atomically on one exact M008 database."""

    try:
        if (
            type(connection) is not sqlite3.Connection
            or connection.in_transaction
            or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
            or connection.execute("PRAGMA query_only").fetchone()[0] != 0
        ):
            raise PublicJobIdentityMigrationError()
        initial = attest_public_job_identity_schema(connection)
        if initial["state"] == "correctly_installed":
            return {
                "migration_version": MIGRATION_VERSION,
                "state": "correctly_installed",
                "applied": False,
            }
        if initial["state"] != "public_job_identity_pending":
            raise PublicJobIdentityMigrationError()

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

        counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "public_job_identities",
                "public_job_paths",
                "public_job_bindings",
            )
        )
        if counts != (0, 0, 0) or reconcile_public_job_identity(connection):
            raise PublicJobIdentityMigrationError()
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise PublicJobIdentityMigrationError()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or tuple(integrity) != ("ok",):
            raise PublicJobIdentityMigrationError()
        final = attest_public_job_identity_schema(connection)
        if final["state"] != "correctly_installed":
            raise PublicJobIdentityMigrationError()
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
        raise PublicJobIdentityMigrationError() from None
    finally:
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
    "PublicJobIdentityMigrationError",
    "apply_public_job_identity_migration",
]
