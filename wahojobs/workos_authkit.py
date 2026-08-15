"""Small, process-local WorkOS AuthKit Magic Auth gateway.

Importing this module constructs no provider client, opens no database, starts no
thread, and performs no network or filesystem operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
from urllib.parse import parse_qsl, urlsplit

from wahojobs.account_reconciliation import (
    attest_account_schema,
    authoritative_account_row_valid,
    authoritative_auth_identity_row_valid,
)
from wahojobs.accounts import (
    AccountService,
    CreatedUser,
    TrustedIdentityVerifier,
    normalize_email,
)
from wahojobs.ownership import (
    AccountNativePrincipalBootstrapResult,
    ensure_account_native_principal,
    validate_environment_namespace,
)
from wahojobs.trusted_login_completion import (
    complete_trusted_login,
    issue_workos_authkit_trusted_authentication,
)
from wahojobs.workos_authkit_schema import attest_workos_authkit_schema


PROVIDER = "workos_authkit"
AUTHENTICATION_METHOD = "MagicAuth"
METADATA_VERSION = "workos_authkit_magic_auth_v1"
CALLBACK_PATH = "/auth/workos/callback"
AUTHORIZATION_ENDPOINT = "https://api.workos.com/user_management/authorize"
EXCHANGE_TIMEOUT_SECONDS = 5.0
EXCHANGE_MAX_RETRIES = 0
DEFAULT_TRANSACTION_TTL = timedelta(minutes=10)
DEFAULT_MAX_TRANSACTIONS = 128

_CLIENT_ID = re.compile(r"^client_[A-Za-z0-9]{8,192}$")
_WORKOS_USER_ID = re.compile(r"^user_[A-Za-z0-9]{8,192}$")
_OPAQUE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_TRANSACTION_ID = re.compile(r"^wtx_[0-9a-f]{32}$")
_AUTHORIZATION_CODE = re.compile(r"^[A-Za-z0-9._~-]{8,2048}$")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_OUTCOMES = frozenset(
    {
        "authentication_denied",
        "provider_unavailable",
        "invalid_or_expired_transaction",
        "unavailable",
    }
)


class WorkOSAuthKitUnavailable(Exception):
    """One detail-free provider or composition failure."""

    __slots__ = ()

    def __init__(self):
        super().__init__("workos_authkit_unavailable")


class _AuthenticationDenied(Exception):
    __slots__ = ()


class _IdentityMissing(Exception):
    __slots__ = ()


@dataclass(frozen=True, slots=True, repr=False)
class WorkOSAuthKitConfiguration:
    client_id: str
    redirect_uri: str
    environment_namespace: str
    transaction_ttl: timedelta = DEFAULT_TRANSACTION_TTL
    max_transactions: int = DEFAULT_MAX_TRANSACTIONS

    def __post_init__(self):
        parsed = None
        try:
            parsed = urlsplit(self.redirect_uri)
            validate_environment_namespace(self.environment_namespace)
        except Exception:
            raise ValueError("invalid_workos_authkit_configuration") from None
        if (
            type(self.client_id) is not str
            or _CLIENT_ID.fullmatch(self.client_id) is None
            or type(self.redirect_uri) is not str
            or parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != CALLBACK_PATH
            or parsed.query
            or parsed.fragment
            or type(self.transaction_ttl) is not timedelta
            or not timedelta(minutes=1)
            <= self.transaction_ttl
            <= timedelta(minutes=15)
            or type(self.max_transactions) is not int
            or not 1 <= self.max_transactions <= 1024
        ):
            raise ValueError("invalid_workos_authkit_configuration")

    def __repr__(self):
        return "WorkOSAuthKitConfiguration(<configured>)"


@dataclass(frozen=True, slots=True, repr=False)
class WorkOSAuthKitAuthentication:
    """Token-free projection returned by a trusted WorkOS boundary."""

    user_id: str
    email: str
    email_verified: bool
    authentication_method: str

    def __repr__(self):
        return "WorkOSAuthKitAuthentication(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class PreparedWorkOSAuthKitAuthorization:
    transaction_id: str
    authorization_url: str

    def __repr__(self):
        return "PreparedWorkOSAuthKitAuthorization(<redacted>)"


@dataclass(frozen=True, slots=True)
class WorkOSAuthKitFailure:
    status: str

    def __post_init__(self):
        if self.status not in _OUTCOMES:
            raise ValueError("invalid_workos_authkit_failure")


class WorkOSSDKBoundary:
    """Narrow use of the hash-locked WorkOS 10.2.0 client."""

    __slots__ = ("_client",)

    def __init__(self, client):
        user_management = getattr(client, "user_management", None)
        if not callable(getattr(user_management, "get_authorization_url", None)) or not callable(
            getattr(user_management, "authenticate_with_code", None)
        ):
            raise WorkOSAuthKitUnavailable()
        self._client = client

    def authorization_url(
        self,
        *,
        redirect_uri,
        state,
        code_challenge,
        client_id,
    ):
        try:
            return self._client.user_management.get_authorization_url(
                provider="authkit",
                redirect_uri=redirect_uri,
                state=state,
                code_challenge=code_challenge,
                code_challenge_method="S256",
                max_age=0,
                screen_hint="sign-in",
                client_id=client_id,
            )
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception as exc:
            _detach_exception(exc)
            raise WorkOSAuthKitUnavailable() from None

    def exchange_code(self, *, code, code_verifier):
        response = None
        try:
            response = self._client.user_management.authenticate_with_code(
                code=code,
                code_verifier=code_verifier,
                request_options={
                    "timeout": EXCHANGE_TIMEOUT_SECONDS,
                    "max_retries": EXCHANGE_MAX_RETRIES,
                },
            )
            user = response.user
            method = response.authentication_method
            if hasattr(method, "value"):
                method = method.value
            if (
                getattr(response, "organization_id", None) is not None
                or getattr(response, "impersonator", None) is not None
                or getattr(response, "oauth_tokens", None) is not None
            ):
                raise WorkOSAuthKitUnavailable()
            return WorkOSAuthKitAuthentication(
                user_id=user.id,
                email=user.email,
                email_verified=user.email_verified,
                authentication_method=method,
            )
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except WorkOSAuthKitUnavailable:
            raise
        except Exception as exc:
            _detach_exception(exc)
            raise WorkOSAuthKitUnavailable() from None
        finally:
            if response is not None:
                for name in (
                    "access_token",
                    "refresh_token",
                    "authkit_authorization_code",
                    "oauth_tokens",
                ):
                    try:
                        setattr(response, name, None)
                    except (AttributeError, TypeError):
                        pass
            response = None
            code = None
            code_verifier = None

    def __repr__(self):
        return "WorkOSSDKBoundary(<configured>)"


def create_workos_sdk_boundary(*, api_key: str, client_id: str) -> WorkOSSDKBoundary:
    """Explicitly construct the real SDK boundary; imports remain inert."""

    client = None
    try:
        if (
            type(api_key) is not str
            or not 16 <= len(api_key) <= 512
            or any(ord(char) < 33 for char in api_key)
            or type(client_id) is not str
            or _CLIENT_ID.fullmatch(client_id) is None
        ):
            raise WorkOSAuthKitUnavailable()
        from workos import WorkOSClient

        client = WorkOSClient(
            api_key=api_key,
            client_id=client_id,
            request_timeout=int(EXCHANGE_TIMEOUT_SECONDS),
            max_retries=EXCHANGE_MAX_RETRIES,
        )
        return WorkOSSDKBoundary(client)
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except WorkOSAuthKitUnavailable:
        raise
    except Exception as exc:
        _detach_exception(exc)
        raise WorkOSAuthKitUnavailable() from None
    finally:
        api_key = None
        client_id = None
        client = None


class _Transaction:
    __slots__ = (
        "state",
        "code_verifier",
        "invitation",
        "created_at",
        "expires_at",
        "request_key",
    )

    def __init__(
        self,
        *,
        state,
        code_verifier,
        invitation,
        created_at,
        expires_at,
        request_key,
    ):
        self.state = bytearray(state.encode("ascii"))
        self.code_verifier = bytearray(code_verifier.encode("ascii"))
        self.invitation = invitation
        self.created_at = created_at
        self.expires_at = expires_at
        self.request_key = request_key

    def clear(self):
        _clear_buffer(self.state)
        _clear_buffer(self.code_verifier)
        _clear_buffer(self.invitation)
        self.state = None
        self.code_verifier = None
        self.invitation = None
        self.created_at = None
        self.expires_at = None
        self.request_key = None


class WorkOSAuthKitGateway:
    """Own a bounded process-local transaction registry and trusted completion."""

    __slots__ = (
        "_boundary",
        "_configuration",
        "_invitation_key",
        "_identity_verifier",
        "_account_service",
        "_clock",
        "_transactions",
        "_lock",
        "_closed",
    )

    def __init__(
        self,
        *,
        boundary,
        configuration: WorkOSAuthKitConfiguration,
        invitation_lookup_key: bytes,
        clock=None,
    ):
        if (
            type(configuration) is not WorkOSAuthKitConfiguration
            or not callable(getattr(boundary, "authorization_url", None))
            or not callable(getattr(boundary, "exchange_code", None))
            or type(invitation_lookup_key) is not bytes
            or not 32 <= len(invitation_lookup_key) <= 512
        ):
            raise WorkOSAuthKitUnavailable()
        if clock is None:
            clock = lambda: datetime.now(timezone.utc)
        if not callable(clock):
            raise WorkOSAuthKitUnavailable()
        self._boundary = boundary
        self._configuration = configuration
        self._invitation_key = bytearray(invitation_lookup_key)
        self._identity_verifier = TrustedIdentityVerifier()
        self._account_service = AccountService(self._identity_verifier)
        self._clock = clock
        self._transactions = {}
        self._lock = threading.Lock()
        self._closed = False

    @property
    def pending_transaction_count(self):
        with self._lock:
            return len(self._transactions)

    @property
    def redirect_uri(self):
        return self._configuration.redirect_uri

    def prepare_authorization(self, connection, *, invitation_credential=None):
        transaction = None
        invitation = None
        url = None
        try:
            self._require_connection(connection)
            now = _canonical_now(self._clock)
            if invitation_credential is not None:
                if type(invitation_credential) is not bytearray:
                    raise _AuthenticationDenied()
                invitation = bytearray(invitation_credential)
                try:
                    invitation_text = bytes(invitation).decode("ascii", "strict")
                except UnicodeError:
                    raise _AuthenticationDenied() from None
                if not self._account_service.pending_invitation_is_valid(
                    connection,
                    invitation_token=invitation_text,
                    invitation_lookup_key=bytes(self._invitation_key),
                    now=now,
                ):
                    raise _AuthenticationDenied()
                invitation_text = None

            state = secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(32)
            transaction_id = "wtx_" + secrets.token_hex(16)
            request_key = "workos-authkit-" + secrets.token_urlsafe(24)
            if (
                _OPAQUE.fullmatch(state) is None
                or _OPAQUE.fullmatch(verifier) is None
                or _TRANSACTION_ID.fullmatch(transaction_id) is None
            ):
                raise WorkOSAuthKitUnavailable()
            challenge = _base64url_sha256(verifier)
            url = self._boundary.authorization_url(
                redirect_uri=self._configuration.redirect_uri,
                state=state,
                code_challenge=challenge,
                client_id=self._configuration.client_id,
            )
            if not _valid_authorization_url(
                url,
                configuration=self._configuration,
                state=state,
                challenge=challenge,
            ):
                raise WorkOSAuthKitUnavailable()
            transaction = _Transaction(
                state=state,
                code_verifier=verifier,
                invitation=invitation,
                created_at=now,
                expires_at=now + self._configuration.transaction_ttl,
                request_key=request_key,
            )
            invitation = None
            with self._lock:
                if self._closed:
                    raise WorkOSAuthKitUnavailable()
                self._prune_locked(now)
                if len(self._transactions) >= self._configuration.max_transactions:
                    raise WorkOSAuthKitUnavailable()
                self._transactions[transaction_id] = transaction
            transaction = None
            return PreparedWorkOSAuthKitAuthorization(transaction_id, url)
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except _AuthenticationDenied:
            raise WorkOSAuthKitUnavailable() from None
        except WorkOSAuthKitUnavailable:
            raise
        except Exception as exc:
            _detach_exception(exc)
            raise WorkOSAuthKitUnavailable() from None
        finally:
            if transaction is not None:
                transaction.clear()
            _clear_buffer(invitation)
            invitation_credential = None
            connection = None
            url = None

    def complete_authorization(
        self,
        connection,
        callback_target,
        browser_transaction_id,
        completion_policy,
        request_secret_vault,
    ):
        transaction = None
        callback = None
        authentication = None
        verified_identity = None
        resolved = None
        proof = None
        try:
            self._require_connection(connection)
            callback = _validated_callback(callback_target)
            now = _canonical_now(self._clock)
            transaction = self._claim_transaction(
                browser_transaction_id,
                callback["state"],
                now,
            )
            if callback["code"] is None:
                raise _AuthenticationDenied()
            if _AUTHORIZATION_CODE.fullmatch(callback["code"]) is None:
                raise _AuthenticationDenied()
            verifier = bytes(transaction.code_verifier).decode("ascii", "strict")
            authentication = self._boundary.exchange_code(
                code=callback["code"],
                code_verifier=verifier,
            )
            callback["code"] = None
            verifier = None
            completed_at = _canonical_now(self._clock)
            if completed_at < now or completed_at >= transaction.expires_at:
                raise _AuthenticationDenied()
            values = _validated_authentication(authentication)
            normalized_email = normalize_email(values["email"])
            verified_identity = (
                self._identity_verifier.from_workos_authkit_authentication(
                    provider_subject=values["user_id"],
                    verified_email=normalized_email,
                    authenticated_at=completed_at,
                    metadata_version=METADATA_VERSION,
                )
            )
            try:
                resolved = _resolve_durable_identity(
                    connection,
                    values["user_id"],
                    completed_at,
                )
            except _IdentityMissing:
                if transaction.invitation is None:
                    raise _AuthenticationDenied() from None
                invitation_text = bytes(transaction.invitation).decode(
                    "ascii",
                    "strict",
                )
                created = self._account_service.create_invited_user_for_workos_authkit(
                    connection,
                    identity=verified_identity,
                    invitation_token=invitation_text,
                    invitation_lookup_key=bytes(self._invitation_key),
                    idempotency_key=transaction.request_key,
                    now=completed_at,
                )
                invitation_text = None
                if type(created) is not CreatedUser:
                    raise _AuthenticationDenied()
                created = None
                resolved = _resolve_durable_identity(
                    connection,
                    values["user_id"],
                    completed_at,
                )
            if connection.in_transaction:
                raise WorkOSAuthKitUnavailable()
            ownership = ensure_account_native_principal(
                connection,
                user_id=resolved["account_id"],
                environment_namespace=(
                    self._configuration.environment_namespace
                ),
                occurred_at=completed_at.isoformat(timespec="seconds"),
            )
            if (
                type(ownership) is not AccountNativePrincipalBootstrapResult
                or connection.in_transaction
            ):
                raise WorkOSAuthKitUnavailable()
            ownership = None
            proof = issue_workos_authkit_trusted_authentication(
                account_id=resolved["account_id"],
                identity_id=resolved["identity_id"],
                completed_at=completed_at,
                environment_namespace=(
                    self._configuration.environment_namespace
                ),
            )
            return complete_trusted_login(
                connection,
                proof,
                completion_policy,
                request_secret_vault,
                trusted_now=completed_at,
                idempotency_key=transaction.request_key,
            )
        except _AuthenticationDenied:
            return WorkOSAuthKitFailure("authentication_denied")
        except WorkOSAuthKitUnavailable as exc:
            _detach_exception(exc)
            return WorkOSAuthKitFailure("provider_unavailable")
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            _detach_exception(exc)
            return WorkOSAuthKitFailure("unavailable")
        except Exception as exc:
            _detach_exception(exc)
            return WorkOSAuthKitFailure("unavailable")
        finally:
            if transaction is not None:
                transaction.clear()
            if type(callback) is dict:
                for name in tuple(callback):
                    callback[name] = None
                callback.clear()
            if type(callback_target) is bytearray:
                _clear_buffer(callback_target)
            callback_target = None
            browser_transaction_id = None
            completion_policy = None
            request_secret_vault = None
            authentication = None
            verified_identity = None
            resolved = None
            proof = None
            connection = None

    def close(self):
        with self._lock:
            if self._closed:
                return True
            self._closed = True
            transactions = tuple(self._transactions.values())
            self._transactions.clear()
        for transaction in transactions:
            transaction.clear()
        _clear_buffer(self._invitation_key)
        self._invitation_key = None
        self._boundary = None
        self._account_service = None
        self._identity_verifier = None
        self._clock = None
        return True

    def _claim_transaction(self, transaction_id, state, now):
        if (
            type(transaction_id) is not str
            or _TRANSACTION_ID.fullmatch(transaction_id) is None
            or type(state) is not str
            or _OPAQUE.fullmatch(state) is None
        ):
            raise _AuthenticationDenied()
        with self._lock:
            if self._closed:
                raise _AuthenticationDenied()
            self._prune_locked(now)
            transaction = self._transactions.get(transaction_id)
            if transaction is None:
                raise _AuthenticationDenied()
            expected = bytes(transaction.state).decode("ascii", "strict")
            if not hmac.compare_digest(expected, state):
                raise _AuthenticationDenied()
            transaction = self._transactions.pop(transaction_id)
        return transaction

    def _prune_locked(self, now):
        expired = [
            key
            for key, transaction in self._transactions.items()
            if transaction.expires_at <= now
        ]
        for key in expired:
            self._transactions.pop(key).clear()

    @staticmethod
    def _require_connection(connection):
        if (
            type(connection) is not sqlite3.Connection
            or connection.in_transaction
            or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
            or connection.execute("PRAGMA query_only").fetchone()[0] != 0
            or not attest_account_schema(connection)
            or attest_workos_authkit_schema(connection)["state"]
            != "correctly_installed"
        ):
            raise WorkOSAuthKitUnavailable()

    def __repr__(self):
        with self._lock:
            state = "closed" if self._closed else "configured"
        return f"WorkOSAuthKitGateway(<{state}>)"


def _validated_authentication(authentication):
    if type(authentication) is not WorkOSAuthKitAuthentication:
        raise _AuthenticationDenied()
    if (
        type(authentication.user_id) is not str
        or _WORKOS_USER_ID.fullmatch(authentication.user_id) is None
        or type(authentication.email) is not str
        or authentication.email_verified is not True
        or authentication.authentication_method != AUTHENTICATION_METHOD
    ):
        raise _AuthenticationDenied()
    normalize_email(authentication.email)
    return {
        "user_id": authentication.user_id,
        "email": authentication.email,
    }


def _resolve_durable_identity(connection, subject, now):
    rows = _rows(
        connection,
        "SELECT auth_identity_id, user_id, provider, provider_subject, "
        "verified_email, email_verified, created_at, last_authenticated_at, "
        "disabled_at, link_idempotency_key, request_fingerprint "
        "FROM auth_identities WHERE provider = ? AND provider_subject = ? "
        "ORDER BY auth_identity_id LIMIT 2",
        (PROVIDER, subject),
    )
    if not rows:
        raise _IdentityMissing()
    if len(rows) != 1:
        raise WorkOSAuthKitUnavailable()
    identity = rows[0]
    if (
        not authoritative_auth_identity_row_valid(identity)
        or identity["provider"] != PROVIDER
        or identity["provider_subject"] != subject
        or identity["disabled_at"] is not None
    ):
        raise _AuthenticationDenied()
    accounts = _rows(
        connection,
        "SELECT user_id, lifecycle_status, row_version, created_at, updated_at, "
        "deletion_requested_at, deactivated_at FROM users WHERE user_id = ? LIMIT 2",
        (identity["user_id"],),
    )
    if len(accounts) != 1:
        raise WorkOSAuthKitUnavailable()
    account = accounts[0]
    if not authoritative_account_row_valid(
        account,
        expected_user_id=identity["user_id"],
    ):
        raise WorkOSAuthKitUnavailable()
    if account["lifecycle_status"] != "active":
        raise _AuthenticationDenied()
    identity_created = _database_time(identity["created_at"])
    account_created = _database_time(account["created_at"])
    account_updated = _database_time(account["updated_at"])
    last_authenticated = _database_time(identity["last_authenticated_at"])
    if (
        account_created > now
        or account_updated > now
        or identity_created < account_created
        or identity_created > now
        or last_authenticated < identity_created
        or last_authenticated > now
    ):
        raise WorkOSAuthKitUnavailable()
    matches = _rows(
        connection,
        "SELECT auth_identity_id FROM auth_identities WHERE "
        "(provider = ? AND provider_subject = ?) OR (user_id = ? AND provider = ?) "
        "LIMIT 3",
        (PROVIDER, subject, identity["user_id"], PROVIDER),
    )
    if len(matches) != 1 or matches[0]["auth_identity_id"] != identity["auth_identity_id"]:
        raise WorkOSAuthKitUnavailable()
    return {
        "account_id": account["user_id"],
        "identity_id": identity["auth_identity_id"],
    }


def _validated_callback(target):
    if type(target) is not str or not 1 <= len(target.encode("utf-8")) <= 8192:
        raise _AuthenticationDenied()
    if _INVALID_PERCENT_ESCAPE.search(target) is not None:
        raise _AuthenticationDenied()
    try:
        parsed = urlsplit(target)
    except ValueError:
        raise _AuthenticationDenied() from None
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.path != CALLBACK_PATH
        or parsed.fragment
        or not parsed.query
    ):
        raise _AuthenticationDenied()
    try:
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=4,
        )
    except (UnicodeError, ValueError):
        raise _AuthenticationDenied() from None
    values = {}
    for key, value in pairs:
        if key in values:
            raise _AuthenticationDenied()
        values[key] = value
    if set(values) == {"state", "code"}:
        code = values["code"]
    elif "state" in values and "error" in values and set(values) <= {
        "state",
        "error",
        "error_description",
    }:
        code = None
    else:
        raise _AuthenticationDenied()
    if _OPAQUE.fullmatch(values["state"]) is None:
        raise _AuthenticationDenied()
    return {"state": values["state"], "code": code}


def _valid_authorization_url(value, *, configuration, state, challenge):
    if type(value) is not str or len(value) > 8192:
        return False
    try:
        parsed = urlsplit(value)
        expected = urlsplit(AUTHORIZATION_ENDPOINT)
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=32,
        )
    except (UnicodeError, ValueError):
        return False
    if len({key for key, _value in pairs}) != len(pairs):
        return False
    values = dict(pairs)
    return (
        parsed.scheme == expected.scheme
        and parsed.netloc == expected.netloc
        and parsed.path == expected.path
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and values.get("provider") == "authkit"
        and values.get("redirect_uri") == configuration.redirect_uri
        and values.get("state") == state
        and values.get("code_challenge") == challenge
        and values.get("code_challenge_method") == "S256"
        and values.get("max_age") == "0"
        and values.get("screen_hint") == "sign-in"
        and values.get("client_id") == configuration.client_id
        and values.get("response_type") == "code"
        and "organization_id" not in values
        and "connection_id" not in values
    )


def _canonical_now(provider):
    value = provider()
    if type(value) is not datetime or value.tzinfo is None:
        raise WorkOSAuthKitUnavailable()
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _database_time(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise WorkOSAuthKitUnavailable() from None
    if parsed.tzinfo is None or parsed.microsecond != 0:
        raise WorkOSAuthKitUnavailable()
    return parsed.astimezone(timezone.utc)


def _base64url_sha256(value):
    digest = hashlib.sha256(value.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _rows(connection, sql, parameters):
    cursor = connection.execute(sql, parameters)
    names = tuple(item[0] for item in cursor.description)
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _clear_buffer(value):
    if type(value) is bytearray:
        for index in range(len(value)):
            value[index] = 0
        value.clear()


def _detach_exception(exc):
    try:
        exc.__traceback__ = None
        exc.__cause__ = None
        exc.__context__ = None
    except (AttributeError, TypeError):
        pass


__all__ = [
    "AUTHENTICATION_METHOD",
    "AUTHORIZATION_ENDPOINT",
    "CALLBACK_PATH",
    "EXCHANGE_MAX_RETRIES",
    "EXCHANGE_TIMEOUT_SECONDS",
    "METADATA_VERSION",
    "PROVIDER",
    "PreparedWorkOSAuthKitAuthorization",
    "WorkOSAuthKitAuthentication",
    "WorkOSAuthKitConfiguration",
    "WorkOSAuthKitFailure",
    "WorkOSAuthKitGateway",
    "WorkOSAuthKitUnavailable",
    "WorkOSSDKBoundary",
    "create_workos_sdk_boundary",
]
