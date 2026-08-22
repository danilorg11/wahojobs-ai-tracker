"""Atomically apply dormant M010 to one explicit open SQLite database."""

from __future__ import annotations

import sqlite3

from wahojobs.ai_profile_import_schema import (
    MIGRATION_PATH,
    MIGRATION_VERSION,
    attest_ai_profile_import_schema,
)
from wahojobs.workos_authkit_schema import iter_sql_statements


class AIProfileImportMigrationError(Exception):
    __slots__ = ()

    def __init__(self):
        super().__init__("ai_profile_import_migration_unavailable")


def apply_ai_profile_import_migration(connection, *, failure_injector=None):
    """Install M010 on one exact M009 database without opening any path."""

    legacy_alter_table = None
    try:
        if (
            type(connection) is not sqlite3.Connection
            or connection.in_transaction
            or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
            or connection.execute("PRAGMA query_only").fetchone()[0] != 0
        ):
            raise AIProfileImportMigrationError()
        initial = attest_ai_profile_import_schema(connection)
        if initial["state"] == "correctly_installed":
            return {"migration_version": MIGRATION_VERSION, "state": "correctly_installed", "applied": False}
        if initial["state"] != "ai_profile_import_pending":
            raise AIProfileImportMigrationError()

        legacy_alter_table = connection.execute("PRAGMA legacy_alter_table").fetchone()[0]
        connection.execute("PRAGMA legacy_alter_table = ON")
        _inject(failure_injector, "before_begin")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("PRAGMA defer_foreign_keys = ON")
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
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise AIProfileImportMigrationError()
        counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("ai_profile_import_entitlements", "ai_profile_import_attempts")
        )
        if counts != (0, 0):
            raise AIProfileImportMigrationError()
        final = attest_ai_profile_import_schema(connection)
        if final["state"] != "correctly_installed":
            raise AIProfileImportMigrationError()
        _inject(failure_injector, "before_commit")
        connection.commit()
        return {"migration_version": MIGRATION_VERSION, "state": "correctly_installed", "applied": True}
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
        raise AIProfileImportMigrationError() from None
    finally:
        if type(connection) is sqlite3.Connection and legacy_alter_table is not None:
            try:
                connection.execute(f"PRAGMA legacy_alter_table = {1 if legacy_alter_table else 0}")
            except sqlite3.Error:
                pass
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


__all__ = ("AIProfileImportMigrationError", "apply_ai_profile_import_migration")
