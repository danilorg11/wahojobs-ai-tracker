from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import shutil
import sqlite3
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

from scripts.workos_authkit_provider_migration import (
    apply_workos_authkit_provider_migration,
)
from tests.closed_schema_convergence_test_support import (
    apply_m007,
    build_fresh_m001_m006,
)
from wahojobs import accounts
from wahojobs.browser_session_lifecycle import (
    create_request_scoped_session_secret_vault,
    discard_request_scoped_session_secret_vault,
)
from wahojobs.trusted_login_completion import (
    create_workos_authkit_trusted_login_completion_policy,
    prepare_session_delivery,
)
from wahojobs.workos_authkit import (
    AUTHORIZATION_ENDPOINT,
    WorkOSAuthKitAuthentication,
    WorkOSAuthKitConfiguration,
    WorkOSAuthKitGateway,
    WorkOSAuthKitUnavailable,
)


NOW = datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)
PUBLIC_ORIGIN = "https://127.0.0.1:9443"
CLIENT_ID = "client_0123456789abcdef"
INVITATION_KEY = secrets.token_bytes(32)


class MutableClock:
    def __init__(self, value=NOW):
        self._value = value
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            return self._value

    def advance(self, delta):
        with self._lock:
            self._value += delta


class FakeWorkOSBoundary:
    """Deterministic token-free boundary; it performs no I/O."""

    def __init__(
        self,
        *,
        subject="user_0123456789abcdef",
        email="person@example.test",
        method="MagicAuth",
        verified=True,
        fail_exchange=False,
        exchange_barrier=None,
    ):
        self.subject = subject
        self.email = email
        self.method = method
        self.verified = verified
        self.fail_exchange = fail_exchange
        self.exchange_barrier = exchange_barrier
        self.authorization_count = 0
        self.exchange_count = 0
        self._lock = threading.Lock()

    def authorization_url(
        self,
        *,
        redirect_uri,
        state,
        code_challenge,
        client_id,
    ):
        with self._lock:
            self.authorization_count += 1
        return AUTHORIZATION_ENDPOINT + "?" + urlencode(
            {
                "provider": "authkit",
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "max_age": 0,
                "screen_hint": "sign-in",
                "client_id": client_id,
                "response_type": "code",
            }
        )

    def exchange_code(self, *, code, code_verifier):
        if not isinstance(code, str) or not isinstance(code_verifier, str):
            raise WorkOSAuthKitUnavailable()
        with self._lock:
            self.exchange_count += 1
        if self.exchange_barrier is not None:
            self.exchange_barrier.wait(timeout=5)
        if self.fail_exchange:
            raise WorkOSAuthKitUnavailable()
        return WorkOSAuthKitAuthentication(
            user_id=self.subject,
            email=self.email,
            email_verified=self.verified,
            authentication_method=self.method,
        )


def build_m008(path: Path) -> sqlite3.Connection:
    connection = build_fresh_m001_m006(path)
    apply_m007(connection, path)
    apply_workos_authkit_provider_migration(connection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def copy_database(template: Path, destination: Path) -> sqlite3.Connection:
    shutil.copyfile(template, destination)
    return connect(destination)


def connect(path: Path, *, timeout=2.0) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=timeout)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def configuration():
    return WorkOSAuthKitConfiguration(
        client_id=CLIENT_ID,
        redirect_uri=PUBLIC_ORIGIN + "/auth/workos/callback",
        environment_namespace="test",
    )


def completion_policy():
    return create_workos_authkit_trusted_login_completion_policy(
        environment_namespace="test",
        idle_ttl=timedelta(hours=1),
        absolute_ttl=timedelta(days=7),
    )


def gateway(boundary=None, *, clock=None):
    return WorkOSAuthKitGateway(
        boundary=boundary or FakeWorkOSBoundary(),
        configuration=configuration(),
        invitation_lookup_key=INVITATION_KEY,
        clock=clock or MutableClock(),
    )


def create_invitation(connection, email, *, now=NOW, expires=None, suffix=None):
    suffix = suffix or secrets.token_hex(8)
    return accounts.create_invitation(
        connection,
        email=email,
        lookup_key=INVITATION_KEY,
        expires_at=expires or now + timedelta(days=1),
        created_by="test_operator",
        idempotency_key="workos-test-invitation-" + suffix,
        now=now,
    )


def callback_target(prepared):
    state = parse_qs(urlsplit(prepared.authorization_url).query)["state"][0]
    code = secrets.token_urlsafe(32)
    return "/auth/workos/callback?" + urlencode({"code": code, "state": state})


def complete(gateway_object, connection, prepared, *, vault=None):
    vault = vault or create_request_scoped_session_secret_vault()
    result = gateway_object.complete_authorization(
        connection,
        callback_target(prepared),
        prepared.transaction_id,
        completion_policy(),
        vault,
    )
    return result, vault


def deliver(connection, completion, vault, *, now=NOW):
    lease = prepare_session_delivery(
        connection,
        completion,
        vault,
        now=now,
    )
    lease.acknowledge_delivery()
    return lease


def discard_if_needed(result, vault):
    if getattr(result, "status", None) != "issued":
        discard_request_scoped_session_secret_vault(vault)


def snapshot(connection):
    return {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "users",
            "auth_identities",
            "account_invitations",
            "account_sessions",
            "product_principals",
            "principal_account_bindings",
            "ownership_binding_events",
        )
    }
