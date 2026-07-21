import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from tests.persistent_profiles_repository_test_support import (
    account_context,
    install_repository_database,
)
from wahojobs import accounts, ownership
from wahojobs.persistent_profiles_application import (
    _TRUSTED_AUTHENTICATION_ACTOR_ISSUER,
)


LATER = "2026-07-21T12:00:00+00:00"


class ReadOnlyAuthorizationProvider:
    def __init__(self, path, *, authorizer=None, trace=None, timeout=0.15):
        self.path = Path(path)
        self.authorizer = authorizer
        self.trace = trace
        self.timeout = timeout
        self.opened = 0
        self.closed = 0

    def __call__(self):
        @contextmanager
        def scope():
            connection = sqlite3.connect(
                f"file:{self.path.as_posix()}?mode=ro",
                uri=True,
                timeout=self.timeout,
            )
            self.opened += 1
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA query_only = ON")
                if self.authorizer is not None:
                    connection.set_authorizer(self.authorizer)
                if self.trace is not None:
                    connection.set_trace_callback(self.trace)
                yield connection
            finally:
                connection.close()
                self.closed += 1

        return scope()


def install_authorization_database(path):
    return install_repository_database(path)


def seed_authorized_account(connection, *, suffix="42", environment="private_beta"):
    if environment != "private_beta":
        raise ValueError("authorization test account environment is fixed")
    principal = account_context(connection, suffix=suffix)
    binding = connection.execute(
        "SELECT binding_id, user_id FROM principal_account_bindings "
        "WHERE principal_id = ?",
        (principal.principal_id,),
    ).fetchone()
    return {
        "principal": principal,
        "principal_id": principal.principal_id,
        "binding_id": binding[0],
        "account_id": binding[1],
        "environment": environment,
    }


def trusted_actor(state, *, actor_key="authorization-test-actor", environment=None):
    return _TRUSTED_AUTHENTICATION_ACTOR_ISSUER.issue(
        actor_key,
        account_id=state["account_id"],
        environment_namespace=environment or state["environment"],
    )


def suspend_account(connection, state):
    return accounts.suspend_user(
        connection,
        user_id=state["account_id"],
        expected_version=1,
        source="test_admin",
        idempotency_key=f"authorization-account-suspend-{state['binding_id'][-4:]}",
        now=datetime.fromisoformat(LATER),
    )


def request_account_deletion(connection, state):
    return accounts.request_account_deletion(
        connection,
        user_id=state["account_id"],
        expected_version=1,
        cooling_period=timedelta(minutes=1),
        purge_after=timedelta(minutes=2),
        request_source="user_request",
        idempotency_key=f"authorization-delete-{state['binding_id'][-4:]}",
        now=datetime.fromisoformat(LATER),
    )


def deactivate_account(connection, state):
    request_account_deletion(connection, state)
    return accounts.deactivate_account_after_cooling(
        connection,
        user_id=state["account_id"],
        expected_version=2,
        source="test_admin",
        idempotency_key=f"authorization-deactivate-{state['binding_id'][-4:]}",
        deactivation_evidence={},
        now=datetime.fromisoformat(LATER) + timedelta(minutes=3),
    )


def transition_binding(connection, state, resulting_status):
    event_type = {
        "suspended": "binding_suspended",
        "released": "binding_released",
    }[resulting_status]
    command = ownership.BindingEventCommand(
        principal_id=state["principal_id"],
        binding_id=state["binding_id"],
        user_id=state["account_id"],
        expected_event_version=2,
        event_type=event_type,
        prior_status="active",
        resulting_status=resulting_status,
        actor_type="administrator",
        reason_code="authorization_test",
        approval_reference="authorization-review",
        idempotency_key=f"authorization-binding-{resulting_status}-{state['binding_id'][-4:]}",
        occurred_at=LATER,
        metadata={},
    )
    return ownership.append_binding_event(connection, command)


def set_principal_status(connection, state, status):
    connection.execute(
        "UPDATE product_principals SET lifecycle_status = ?, version = version + 1, "
        "updated_at = ? WHERE principal_id = ?",
        (status, LATER, state["principal_id"]),
    )
    connection.commit()


def file_fingerprint(path):
    path = Path(path)
    stat = path.stat()
    return (
        stat.st_size,
        stat.st_mtime_ns,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )
