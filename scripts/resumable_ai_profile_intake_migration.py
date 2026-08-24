"""Atomically apply M011 to one explicit open SQLite database."""

from __future__ import annotations

import sqlite3

from wahojobs.resumable_ai_profile_intake_schema import (
    MIGRATION_PATH,
    MIGRATION_VERSION,
    attest_resumable_ai_profile_intake_schema,
)
from wahojobs.workos_authkit_schema import iter_sql_statements


class ResumableAIProfileIntakeMigrationError(Exception):
    __slots__ = ()

    def __init__(self):
        super().__init__("resumable_ai_profile_intake_migration_unavailable")


def apply_resumable_ai_profile_intake_migration(connection, *, failure_injector=None):
    """Install M011 on one exact M010 database without opening any path."""

    try:
        if (
            type(connection) is not sqlite3.Connection
            or connection.in_transaction
            or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
            or connection.execute("PRAGMA query_only").fetchone()[0] != 0
        ):
            raise ResumableAIProfileIntakeMigrationError()
        initial = attest_resumable_ai_profile_intake_schema(connection)
        if initial["state"] == "correctly_installed":
            return {"migration_version": MIGRATION_VERSION, "state": "correctly_installed", "applied": False}
        if initial["state"] != "resumable_ai_intake_pending":
            raise ResumableAIProfileIntakeMigrationError()
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
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ResumableAIProfileIntakeMigrationError()
        if connection.execute("SELECT COUNT(*) FROM ai_profile_intake_checkpoints").fetchone()[0] != 0:
            raise ResumableAIProfileIntakeMigrationError()
        final = attest_resumable_ai_profile_intake_schema(connection)
        if final["state"] != "correctly_installed":
            raise ResumableAIProfileIntakeMigrationError()
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
        raise ResumableAIProfileIntakeMigrationError() from None
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


__all__ = (
    "ResumableAIProfileIntakeMigrationError",
    "apply_resumable_ai_profile_intake_migration",
)
