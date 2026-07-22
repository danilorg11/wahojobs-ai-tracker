import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.persistent_profile_read_authorization_test_support import (
    ReadOnlyAuthorizationProvider,
    install_authorization_database,
    seed_authorized_account,
)
from wahojobs import accounts
from wahojobs.browser_session_authentication import SESSION_COOKIE_NAME
from wahojobs.persistent_profiles_application import BrowserRequestContext


AUTHENTICATED_AT = datetime(2026, 7, 20, 13, 0, 0, tzinfo=timezone.utc)
REQUEST_AT = AUTHENTICATED_AT + timedelta(minutes=5)


def install_browser_authentication_database(path):
    return install_authorization_database(path)


def seed_browser_session(connection, *, suffix="42"):
    state = seed_authorized_account(connection, suffix=suffix)
    created = accounts.create_session(
        connection,
        user_id=state["account_id"],
        idle_ttl=timedelta(hours=2),
        absolute_ttl=timedelta(days=1),
        idempotency_key=f"browser-session-create-{suffix}",
        now=AUTHENTICATED_AT,
    )
    connection.commit()
    state.update(
        {
            "session_id": created.session.session_id,
            "session_token": created.session_token,
            "csrf_secret": created.csrf_secret,
        }
    )
    return state


def browser_request(token=None, *, extra_headers=()):
    headers = list(extra_headers)
    if token is not None:
        headers.append(("Cookie", f"{SESSION_COOKIE_NAME}={token}"))
    return BrowserRequestContext(
        "GET",
        "/account/profile",
        tuple(headers),
    )


def guarded_update(connection, trigger_names, operation):
    definitions = []
    for name in trigger_names:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (name,),
        ).fetchone()
        if row is None:
            raise AssertionError(f"missing test trigger: {name}")
        definitions.append(row[0])
    for name in trigger_names:
        connection.execute(f'DROP TRIGGER "{name}"')
    try:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        operation()
    finally:
        connection.execute("PRAGMA ignore_check_constraints = OFF")
        for definition in definitions:
            connection.execute(definition)
    connection.commit()


class TracedReadOnlyProvider(ReadOnlyAuthorizationProvider):
    def __init__(self, path, *, timeout=0.15):
        self.statements = []
        self.connection_ids = []
        super().__init__(path, trace=self.statements.append, timeout=timeout)

    def __call__(self):
        parent_scope = super().__call__()

        @contextmanager
        def scope():
            with parent_scope as connection:
                self.connection_ids.append(id(connection))
                yield connection

        return scope()


@contextmanager
def read_only_connection(path):
    connection = sqlite3.connect(
        f"file:{Path(path).as_posix()}?mode=ro",
        uri=True,
        timeout=0.15,
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA query_only = ON")
    try:
        yield connection
    finally:
        connection.close()
