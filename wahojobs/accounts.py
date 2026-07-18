from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import re
import secrets
import sqlite3
import unicodedata
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


PROVIDERS = {"google"}
LIFECYCLE_STATUSES = {
    "active",
    "suspended",
    "deletion_requested",
    "deactivated_pending_purge",
}
CONSENT_PURPOSES = {"profile_storage", "product_terms", "privacy_policy"}
CONSENT_ACTIONS = {"granted", "revoked"}
LIFECYCLE_TRANSITIONS = {
    ("active", "account_suspended"): "suspended",
    ("suspended", "account_reactivated"): "active",
}
TOKEN_HASH_VERSION = "sha256_v1"
INVITATION_HASH_VERSION = "hmac_sha256_v1"
MAX_METADATA_BYTES = 4096
MAX_METADATA_DEPTH = 8
MAX_METADATA_KEYS = 64
MAX_METADATA_LIST_ITEMS = 64
MAX_METADATA_KEY_LENGTH = 128
MAX_METADATA_STRING_LENGTH = 1024
SENSITIVE_METADATA_NAMES = {
    "authenticationheader",
    "authorization",
    "authorizationheader",
    "bearer",
    "password",
    "cookie",
    "credential",
    "csrf",
    "csrfmaterial",
    "databasepath",
    "email",
    "invitationhmac",
    "oauthclaim",
    "oauthclaims",
    "oauth",
    "providersubject",
    "rawclaim",
    "rawclaims",
    "rawhtml",
    "resume",
    "resumecontent",
    "secret",
    "sessiontoken",
    "sql",
    "token",
    "tokenhash",
    "tokenmaterial",
    "tokensecret",
    "tokenvalue",
}
SENSITIVE_METADATA_PREFIXES = (
    "authorization",
    "authenticationheader",
    "csrf",
    "email",
    "invitationhmac",
    "oauth",
    "providersubject",
    "rawclaim",
    "resume",
    "sessiontoken",
)
SAFE_REVOKE_REASONS = {
    "account_deactivation_requested",
    "account_suspended",
    "explicit_revoke",
    "security_reset",
    "session_rotated",
    "stale",
    "user_logout",
}


class AccountError(Exception):
    pass


class InvalidAccountInput(AccountError):
    pass


class AuthenticationUnavailable(AccountError):
    def __init__(self):
        super().__init__("Authentication could not be completed.")


class SessionUnavailable(AccountError):
    def __init__(self):
        super().__init__("Session is not available.")


class AccountStateConflict(AccountError):
    def __init__(self, message="Account state could not be changed."):
        super().__init__(message)


class StaleAccountVersion(AccountStateConflict):
    def __init__(self):
        super().__init__("Account state changed before this operation completed.")


class StaleSessionVersion(SessionUnavailable):
    pass


_IDENTITY_ATTESTATIONS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class VerifiedProviderIdentity:
    __slots__ = (
        "_provider",
        "_provider_subject",
        "_verified_email",
        "_email_verified",
        "_authenticated_at",
        "_metadata_version",
        "__weakref__",
    )

    def __new__(cls, *args, **kwargs):
        raise TypeError("Verified provider identities are created by a trusted verifier.")

    @property
    def provider(self):
        return self._provider

    @property
    def provider_subject(self):
        return self._provider_subject

    @property
    def verified_email(self):
        return self._verified_email

    @property
    def email_verified(self):
        return self._email_verified

    @property
    def authenticated_at(self):
        return self._authenticated_at

    @property
    def metadata_version(self):
        return self._metadata_version

    def __repr__(self):
        return (
            "VerifiedProviderIdentity(provider="
            f"{self.provider!r}, email_verified={self.email_verified!r})"
        )

    def __reduce__(self):
        raise TypeError("Verified provider identities cannot be serialized.")


class TrustedIdentityVerifier:
    __slots__ = ("_capability",)

    def __init__(self):
        self._capability = object()

    def from_validated_google_claims(
        self,
        *,
        provider_subject: str,
        verified_email: str | None,
        email_verified: bool,
        authenticated_at: datetime,
        metadata_version: str,
    ) -> VerifiedProviderIdentity:
        identity = object.__new__(VerifiedProviderIdentity)
        values = _validate_verified_identity_values(
            provider="google",
            provider_subject=provider_subject,
            verified_email=verified_email,
            email_verified=email_verified,
            authenticated_at=authenticated_at,
            metadata_version=metadata_version,
        )
        for field, value in values.items():
            object.__setattr__(identity, f"_{field}", value)
        snapshot = tuple(values.values())
        _IDENTITY_ATTESTATIONS[identity] = (self._capability, snapshot)
        return identity

    def _accepts(self, identity) -> bool:
        if type(identity) is not VerifiedProviderIdentity:
            return False
        attestation = _IDENTITY_ATTESTATIONS.get(identity)
        if attestation is None or attestation[0] is not self._capability:
            return False
        try:
            current = (
                identity.provider,
                identity.provider_subject,
                identity.verified_email,
                identity.email_verified,
                identity.authenticated_at,
                identity.metadata_version,
            )
        except AttributeError:
            return False
        return current == attestation[1]


class AccountService:
    __slots__ = ("_identity_verifier",)

    def __init__(self, identity_verifier: TrustedIdentityVerifier):
        if type(identity_verifier) is not TrustedIdentityVerifier:
            raise TypeError("AccountService requires a trusted identity verifier.")
        self._identity_verifier = identity_verifier

    def create_invited_user(self, conn, **kwargs):
        return _create_invited_user(
            conn, identity_verifier=self._identity_verifier, **kwargs
        )

    def link_verified_identity(self, conn, **kwargs):
        return _link_verified_identity(
            conn, identity_verifier=self._identity_verifier, **kwargs
        )


@dataclass(frozen=True)
class PublicUser:
    user_id: str
    lifecycle_status: str
    row_version: int
    created_at: str
    updated_at: str
    deletion_requested_at: str | None
    deactivated_at: str | None


@dataclass(frozen=True)
class PublicIdentity:
    auth_identity_id: str
    user_id: str
    provider: str
    email_verified: bool
    created_at: str
    last_authenticated_at: str
    disabled_at: str | None


@dataclass(frozen=True)
class PublicInvitation:
    invitation_id: str
    email_display_hint: str | None
    created_at: str
    expires_at: str
    invitation_status: str
    created_by: str


@dataclass(frozen=True)
class InvitationCreation:
    invitation: PublicInvitation
    invitation_token: str = field(repr=False)


@dataclass(frozen=True)
class CreatedUser:
    user: PublicUser
    identity: PublicIdentity
    invitation_id: str


@dataclass(frozen=True)
class PublicSession:
    session_id: str
    user_id: str
    created_at: str
    last_seen_at: str
    idle_expires_at: str
    absolute_expires_at: str
    rotated_at: str | None
    revoked_at: str | None
    parent_session_id: str | None
    replacement_session_id: str | None
    session_version: int


@dataclass(frozen=True)
class SessionCreation:
    session: PublicSession
    session_token: str = field(repr=False)
    csrf_secret: str = field(repr=False)


@dataclass(frozen=True)
class PublicConsent:
    consent_event_id: str
    user_id: str
    purpose: str
    policy_version: str
    action: str
    occurred_at: str
    source: str
    consent_version_before: int
    consent_version_after: int


@dataclass(frozen=True)
class PublicLifecycleEvent:
    lifecycle_event_id: str
    user_id: str
    event_type: str
    occurred_at: str
    source: str
    account_version_before: int
    account_version_after: int


@dataclass(frozen=True)
class AccountMutation:
    user: PublicUser
    event: PublicLifecycleEvent


@dataclass(frozen=True)
class PublicDeletionRequest:
    deletion_request_id: str
    user_id: str
    requested_at: str
    cooling_period_ends_at: str
    purge_eligible_at: str
    cancelled_at: str | None
    deactivated_at: str | None
    status: str
    request_source: str
    restore_lifecycle_status: str
    deactivation_evidence_recorded: bool


@dataclass(frozen=True)
class DeletionMutation:
    user: PublicUser
    request: PublicDeletionRequest
    event: PublicLifecycleEvent
    revoked_session_count: int
    replayed: bool = False


_SAVEPOINTS = itertools.count(1)


@contextmanager
def atomic(conn):
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise AccountStateConflict("Account storage requires foreign-key enforcement.")
    if conn.in_transaction:
        savepoint = f"accounts_{next(_SAVEPOINTS)}_{secrets.token_hex(8)}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def normalize_email(value: str) -> str:
    if type(value) is not str or len(value) > 320 or any(ord(char) < 32 for char in value):
        raise InvalidAccountInput("Email is not valid.")
    text = value.strip()
    if text.count("@") != 1 or any(ord(char) < 32 for char in text):
        raise InvalidAccountInput("Email is not valid.")
    local, domain = text.rsplit("@", 1)
    if not local or not domain or " " in local or " " in domain:
        raise InvalidAccountInput("Email is not valid.")
    try:
        ascii_domain = domain.casefold().encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidAccountInput("Email is not valid.") from exc
    return f"{local.casefold()}@{ascii_domain}"


def invitation_secret_hmac(
    secret: str,
    lookup_key: bytes,
    *,
    version: str = INVITATION_HASH_VERSION,
) -> str:
    if version != INVITATION_HASH_VERSION:
        raise InvalidAccountInput("Invitation lookup hash version is not supported.")
    key = _lookup_key(lookup_key)
    secret = _strict_text(secret, "invitation_secret", minimum=32, maximum=256)
    return hmac.new(key, secret.encode("utf-8"), hashlib.sha256).hexdigest()


def invited_email_hmac(
    email: str,
    lookup_key: bytes,
    *,
    version: str = INVITATION_HASH_VERSION,
) -> str:
    if version != INVITATION_HASH_VERSION:
        raise InvalidAccountInput("Invitation lookup hash version is not supported.")
    key = _lookup_key(lookup_key)
    normalized = normalize_email(email)
    return hmac.new(key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def create_invitation(
    conn,
    *,
    email: str,
    lookup_key: bytes,
    expires_at: datetime,
    created_by: str,
    idempotency_key: str,
    source_metadata: dict | None = None,
    now: datetime | None = None,
    failure_injector=None,
) -> InvitationCreation:
    now = _now(now)
    expires = _aware_datetime(expires_at, "expires_at")
    if expires <= now:
        raise InvalidAccountInput("Invitation expiry must be in the future.")
    created_by = _source_text(created_by, "created_by")
    idempotency_key = _idempotency_key(idempotency_key)
    metadata_json = _metadata_json(source_metadata)
    normalized = normalize_email(email)
    invitation_id = _random_id("inv")
    raw_secret = secrets.token_urlsafe(32)
    invitation_token = f"{invitation_id}.{raw_secret}"
    email_hash = invited_email_hmac(normalized, lookup_key)
    secret_hash = invitation_secret_hmac(raw_secret, lookup_key)
    fingerprint = _fingerprint(
        {
            "hash_version": INVITATION_HASH_VERSION,
            "invited_email_hmac": email_hash,
            "expires_at": _timestamp(expires),
            "created_by": created_by,
            "source_metadata": json.loads(metadata_json),
        }
    )
    try:
        with atomic(conn):
            existing = conn.execute(
                "SELECT * FROM account_invitations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                raise AuthenticationUnavailable()
            conn.execute(
                """
                INSERT INTO account_invitations (
                  invitation_id, invited_email_hmac, invitation_secret_hmac, hash_version,
                  email_display_hint, created_at, expires_at, invitation_status,
                  created_by, source_metadata_json, idempotency_key, request_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    invitation_id,
                    email_hash,
                    secret_hash,
                    INVITATION_HASH_VERSION,
                    _email_hint(normalized),
                    _timestamp(now),
                    _timestamp(expires),
                    created_by,
                    metadata_json,
                    idempotency_key,
                    fingerprint,
                ),
            )
            _inject(failure_injector, "after_invitation_insert")
            row = conn.execute(
                "SELECT * FROM account_invitations WHERE invitation_id = ?",
                (invitation_id,),
            ).fetchone()
            return InvitationCreation(_public_invitation(row), invitation_token)
    except sqlite3.IntegrityError as exc:
        raise AuthenticationUnavailable() from exc


def revoke_invitation(
    conn,
    *,
    invitation_id: str,
    now: datetime | None = None,
) -> PublicInvitation:
    try:
        invitation_id = _strict_id(invitation_id, "invitation_id", "inv_")
    except InvalidAccountInput as exc:
        raise AuthenticationUnavailable() from exc
    now = _now(now)
    try:
        with atomic(conn):
            cursor = conn.execute(
                """
                UPDATE account_invitations
                SET invitation_status = 'revoked', revoked_at = ?
                WHERE invitation_id = ? AND invitation_status = 'pending'
                """,
                (_timestamp(now), invitation_id),
            )
            if cursor.rowcount != 1:
                raise AuthenticationUnavailable()
            return _public_invitation(
                conn.execute(
                    "SELECT * FROM account_invitations WHERE invitation_id = ?",
                    (invitation_id,),
                ).fetchone()
            )
    except sqlite3.IntegrityError as exc:
        raise AuthenticationUnavailable() from exc


def _create_invited_user(
    conn,
    *,
    identity_verifier: TrustedIdentityVerifier,
    identity: VerifiedProviderIdentity,
    invitation_token: str,
    invitation_lookup_key: bytes,
    invitation_hash_version: str = INVITATION_HASH_VERSION,
    idempotency_key: str,
    now: datetime | None = None,
    failure_injector=None,
) -> CreatedUser:
    identity = _trusted_identity(identity, identity_verifier)
    if not identity.email_verified or identity.verified_email is None:
        raise AuthenticationUnavailable()
    now = _now(now)
    idempotency_key = _idempotency_key(idempotency_key)
    normalized_email = normalize_email(identity.verified_email)
    invitation_id, raw_secret = _invitation_token(invitation_token)
    try:
        secret_hash = invitation_secret_hmac(
            raw_secret, invitation_lookup_key, version=invitation_hash_version
        )
        email_hash = invited_email_hmac(
            normalized_email, invitation_lookup_key, version=invitation_hash_version
        )
    except InvalidAccountInput as exc:
        raise AuthenticationUnavailable() from exc
    user_id = _random_id("usr")
    identity_id = _random_id("auth")
    occurred_at = _timestamp(now)
    fingerprint = _fingerprint(
        {
            "provider": identity.provider,
            "provider_subject_digest": hashlib.sha256(identity.provider_subject.encode("utf-8")).hexdigest(),
            "invitation_id": invitation_id,
            "metadata_version": identity.metadata_version,
        }
    )
    try:
        with atomic(conn):
            invitation = conn.execute(
                """
                SELECT * FROM account_invitations
                WHERE invitation_id = ?
                """,
                (invitation_id,),
            ).fetchone()
            if invitation is None:
                raise AuthenticationUnavailable()
            secret_matches = hmac.compare_digest(
                invitation["invitation_secret_hmac"], secret_hash
            )
            email_matches = hmac.compare_digest(
                invitation["invited_email_hmac"], email_hash
            )
            if (
                invitation["hash_version"] != invitation_hash_version
                or not secret_matches
                or not email_matches
            ):
                raise AuthenticationUnavailable()
            existing_identity = conn.execute(
                "SELECT * FROM auth_identities WHERE link_idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if invitation["invitation_status"] == "consumed":
                if (
                    existing_identity is None
                    or existing_identity["request_fingerprint"] != fingerprint
                    or invitation["consumed_by_user_id"] != existing_identity["user_id"]
                ):
                    raise AuthenticationUnavailable()
                return CreatedUser(
                    _public_user(_user_row(conn, existing_identity["user_id"])),
                    _public_identity(existing_identity),
                    invitation_id,
                )
            if (
                invitation["invitation_status"] != "pending"
                or _parse_timestamp(invitation["expires_at"]) <= now
            ):
                raise AuthenticationUnavailable()
            conn.execute(
                """
                INSERT INTO users (
                  user_id, lifecycle_status, row_version, created_at, updated_at
                ) VALUES (?, 'active', 1, ?, ?)
                """,
                (user_id, occurred_at, occurred_at),
            )
            _inject(failure_injector, "after_user_insert")
            conn.execute(
                """
                INSERT INTO auth_identities (
                  auth_identity_id, user_id, provider, provider_subject,
                  verified_email, email_verified, created_at, last_authenticated_at,
                  link_idempotency_key, request_fingerprint
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    identity_id,
                    user_id,
                    identity.provider,
                    identity.provider_subject,
                    normalized_email,
                    occurred_at,
                    _timestamp(identity.authenticated_at),
                    idempotency_key,
                    fingerprint,
                ),
            )
            _inject(failure_injector, "after_identity_insert")
            event = _insert_lifecycle_event(
                conn,
                user_id=user_id,
                event_type="account_created",
                occurred_at=occurred_at,
                source="invited_provider_identity",
                version_before=0,
                version_after=1,
                metadata={},
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
            )
            _inject(failure_injector, "after_lifecycle_event_insert")
            cursor = conn.execute(
                """
                UPDATE account_invitations
                SET invitation_status = 'consumed', consumed_at = ?, consumed_by_user_id = ?
                WHERE invitation_id = ? AND invitation_status = 'pending'
                """,
                (occurred_at, user_id, invitation["invitation_id"]),
            )
            if cursor.rowcount != 1:
                raise AuthenticationUnavailable()
            _inject(failure_injector, "after_invitation_consumption")
            user = _public_user(_user_row(conn, user_id))
            public_identity = _public_identity(
                conn.execute(
                    "SELECT * FROM auth_identities WHERE auth_identity_id = ?", (identity_id,)
                ).fetchone()
            )
            del event
            return CreatedUser(user, public_identity, invitation["invitation_id"])
    except sqlite3.IntegrityError as exc:
        raise AuthenticationUnavailable() from exc


def _link_verified_identity(
    conn,
    *,
    identity_verifier: TrustedIdentityVerifier,
    user_id: str,
    identity: VerifiedProviderIdentity,
    idempotency_key: str,
    now: datetime | None = None,
) -> PublicIdentity:
    user_id = _strict_id(user_id, "user_id", "usr_")
    identity = _trusted_identity(identity, identity_verifier)
    idempotency_key = _idempotency_key(idempotency_key)
    now = _now(now)
    normalized_email = (
        normalize_email(identity.verified_email) if identity.verified_email is not None else None
    )
    fingerprint = _fingerprint(
        {
            "user_id": user_id,
            "provider": identity.provider,
            "provider_subject_digest": hashlib.sha256(identity.provider_subject.encode()).hexdigest(),
            "metadata_version": identity.metadata_version,
        }
    )
    identity_id = _random_id("auth")
    try:
        with atomic(conn):
            user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if user is None or user["lifecycle_status"] != "active":
                raise AuthenticationUnavailable()
            existing = conn.execute(
                "SELECT * FROM auth_identities WHERE link_idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != fingerprint or existing["user_id"] != user_id:
                    raise AuthenticationUnavailable()
                return _public_identity(existing)
            conn.execute(
                """
                INSERT INTO auth_identities (
                  auth_identity_id, user_id, provider, provider_subject,
                  verified_email, email_verified, created_at, last_authenticated_at,
                  link_idempotency_key, request_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity_id,
                    user_id,
                    identity.provider,
                    identity.provider_subject,
                    normalized_email,
                    int(identity.email_verified),
                    _timestamp(now),
                    _timestamp(identity.authenticated_at),
                    idempotency_key,
                    fingerprint,
                ),
            )
            return _public_identity(
                conn.execute(
                    "SELECT * FROM auth_identities WHERE auth_identity_id = ?", (identity_id,)
                ).fetchone()
            )
    except sqlite3.IntegrityError as exc:
        raise AuthenticationUnavailable() from exc


def create_session(
    conn,
    *,
    user_id: str,
    idle_ttl: timedelta,
    absolute_ttl: timedelta,
    idempotency_key: str,
    now: datetime | None = None,
    failure_injector=None,
) -> SessionCreation:
    user_id = _strict_id(user_id, "user_id", "usr_")
    idle_ttl = _positive_timedelta(idle_ttl, "idle_ttl")
    absolute_ttl = _positive_timedelta(absolute_ttl, "absolute_ttl")
    if idle_ttl > absolute_ttl:
        raise InvalidAccountInput("Idle expiry cannot exceed absolute expiry.")
    idempotency_key = _idempotency_key(idempotency_key)
    now = _now(now)
    fingerprint = _fingerprint(
        {
            "user_id": user_id,
            "idle_seconds": int(idle_ttl.total_seconds()),
            "absolute_seconds": int(absolute_ttl.total_seconds()),
        }
    )
    session_id = _random_id("ses")
    raw_token = secrets.token_urlsafe(32)
    raw_csrf = secrets.token_urlsafe(32)
    try:
        with atomic(conn):
            user = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if user is None or user["lifecycle_status"] != "active":
                raise SessionUnavailable()
            if conn.execute(
                "SELECT 1 FROM account_sessions WHERE creation_idempotency_key = ?",
                (idempotency_key,),
            ).fetchone():
                raise SessionUnavailable()
            _insert_session(
                conn,
                session_id=session_id,
                user_id=user_id,
                raw_token=raw_token,
                raw_csrf=raw_csrf,
                now=now,
                idle_expires_at=now + idle_ttl,
                absolute_expires_at=now + absolute_ttl,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
            )
            _inject(failure_injector, "after_session_insert")
            return SessionCreation(
                _public_session(_session_row(conn, session_id)), raw_token, raw_csrf
            )
    except sqlite3.IntegrityError as exc:
        raise SessionUnavailable() from exc


def resolve_session(
    conn,
    *,
    session_token: str,
    now: datetime | None = None,
) -> PublicSession:
    try:
        token = _session_token(session_token)
    except InvalidAccountInput as exc:
        raise SessionUnavailable() from exc
    now = _now(now)
    row = _active_session_for_token(conn, token, now)
    return _public_session(row)


def validate_session_csrf(
    conn,
    *,
    session_token: str,
    csrf_secret: str,
    now: datetime | None = None,
) -> PublicSession:
    try:
        token = _session_token(session_token)
        csrf = _csrf_secret(csrf_secret)
    except InvalidAccountInput as exc:
        raise SessionUnavailable() from exc
    now = _now(now)
    row = _active_session_for_token(conn, token, now)
    csrf_digest = _secret_hash(csrf)
    if not hmac.compare_digest(row["csrf_secret_hash"], csrf_digest):
        raise SessionUnavailable()
    return _public_session(row)


def rotate_session(
    conn,
    *,
    session_token: str,
    expected_session_version: int,
    idle_ttl: timedelta,
    idempotency_key: str,
    now: datetime | None = None,
    failure_injector=None,
) -> SessionCreation:
    _expected_version(expected_session_version, session=True)
    idle_ttl = _positive_timedelta(idle_ttl, "idle_ttl")
    idempotency_key = _idempotency_key(idempotency_key)
    now = _now(now)
    try:
        raw_token = _session_token(session_token)
    except InvalidAccountInput as exc:
        raise SessionUnavailable() from exc
    new_token = secrets.token_urlsafe(32)
    new_csrf = secrets.token_urlsafe(32)
    replacement_id = _random_id("ses")
    fingerprint = _fingerprint(
        {
            "old_token_digest": _secret_hash(raw_token),
            "expected_session_version": expected_session_version,
            "idle_seconds": int(idle_ttl.total_seconds()),
        }
    )
    try:
        with atomic(conn):
            old = _active_session_for_token(conn, raw_token, now)
            if old["session_version"] != expected_session_version:
                raise StaleSessionVersion()
            if conn.execute(
                "SELECT 1 FROM account_sessions WHERE creation_idempotency_key = ?",
                (idempotency_key,),
            ).fetchone():
                raise SessionUnavailable()
            absolute = _parse_timestamp(old["absolute_expires_at"])
            idle_expiry = min(now + idle_ttl, absolute)
            _insert_session(
                conn,
                session_id=replacement_id,
                user_id=old["user_id"],
                raw_token=new_token,
                raw_csrf=new_csrf,
                now=now,
                idle_expires_at=idle_expiry,
                absolute_expires_at=absolute,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
            )
            _inject(failure_injector, "after_replacement_insert")
            occurred = _timestamp(now)
            cursor = conn.execute(
                """
                UPDATE account_sessions
                SET rotated_at = ?, revoked_at = ?, revoke_reason = 'session_rotated',
                    session_version = session_version + 1
                WHERE session_id = ? AND user_id = ? AND session_version = ?
                  AND revoked_at IS NULL AND rotated_at IS NULL
                """,
                (
                    occurred,
                    occurred,
                    old["session_id"],
                    old["user_id"],
                    expected_session_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleSessionVersion()
            _inject(failure_injector, "after_old_session_revoke")
            rotation_id = _random_id("rot")
            conn.execute(
                """
                INSERT INTO account_session_rotations (
                  rotation_id, user_id, predecessor_session_id,
                  replacement_session_id, rotated_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    rotation_id,
                    old["user_id"],
                    old["session_id"],
                    replacement_id,
                    occurred,
                    occurred,
                ),
            )
            _inject(failure_injector, "after_rotation_edge_insert")
            predecessor = _session_row(conn, old["session_id"])
            if (
                predecessor["replacement_session_id"] != replacement_id
                or predecessor["rotated_at"] != occurred
                or predecessor["revoked_at"] != occurred
                or predecessor["session_version"] != expected_session_version + 1
            ):
                raise StaleSessionVersion()
            replacement = _session_row(conn, replacement_id)
            if replacement["parent_session_id"] != old["session_id"]:
                raise StaleSessionVersion()
            return SessionCreation(
                _public_session(replacement), new_token, new_csrf
            )
    except sqlite3.IntegrityError as exc:
        raise SessionUnavailable() from exc


def revoke_current_session(
    conn,
    *,
    session_token: str,
    expected_session_version: int,
    reason: str,
    now: datetime | None = None,
) -> PublicSession:
    _expected_version(expected_session_version, session=True)
    reason = _revoke_reason(reason)
    now = _now(now)
    try:
        token = _session_token(session_token)
    except InvalidAccountInput as exc:
        raise SessionUnavailable() from exc
    with atomic(conn):
        row = _active_session_for_token(conn, token, now)
        cursor = conn.execute(
            """
            UPDATE account_sessions
            SET revoked_at = ?, revoke_reason = ?, session_version = session_version + 1
            WHERE session_id = ? AND session_version = ? AND revoked_at IS NULL
            """,
            (_timestamp(now), reason, row["session_id"], expected_session_version),
        )
        if cursor.rowcount != 1:
            raise StaleSessionVersion()
        return _public_session(_session_row(conn, row["session_id"]))


def revoke_all_sessions(
    conn,
    *,
    user_id: str,
    expected_user_version: int,
    reason: str,
    now: datetime | None = None,
) -> int:
    user_id = _strict_id(user_id, "user_id", "usr_")
    _expected_version(expected_user_version)
    reason = _revoke_reason(reason)
    now = _now(now)
    with atomic(conn):
        user = _user_row(conn, user_id)
        if user["row_version"] != expected_user_version:
            raise StaleAccountVersion()
        return _revoke_all_sessions(conn, user_id, reason, now)


def suspend_user(
    conn,
    *,
    user_id: str,
    expected_version: int,
    source: str,
    idempotency_key: str,
    metadata: dict | None = None,
    now: datetime | None = None,
) -> AccountMutation:
    with atomic(conn):
        mutation = append_account_lifecycle_event(
            conn,
            user_id=user_id,
            event_type="account_suspended",
            expected_version=expected_version,
            source=source,
            idempotency_key=idempotency_key,
            metadata=metadata,
            now=now,
        )
        revoke_all_sessions(
            conn,
            user_id=user_id,
            expected_user_version=mutation.user.row_version,
            reason="account_suspended",
            now=_parse_timestamp(mutation.user.updated_at),
        )
        return mutation


def reactivate_user(
    conn,
    *,
    user_id: str,
    expected_version: int,
    source: str,
    idempotency_key: str,
    metadata: dict | None = None,
    now: datetime | None = None,
) -> AccountMutation:
    return append_account_lifecycle_event(
        conn,
        user_id=user_id,
        event_type="account_reactivated",
        expected_version=expected_version,
        source=source,
        idempotency_key=idempotency_key,
        metadata=metadata,
        now=now,
    )


def append_account_lifecycle_event(
    conn,
    *,
    user_id: str,
    event_type: str,
    expected_version: int,
    source: str,
    idempotency_key: str,
    metadata: dict | None = None,
    now: datetime | None = None,
) -> AccountMutation:
    user_id = _strict_id(user_id, "user_id", "usr_")
    event_type = _strict_text(event_type, "event_type", maximum=64)
    _expected_version(expected_version)
    source = _source_text(source, "source")
    idempotency_key = _idempotency_key(idempotency_key)
    metadata_json = _metadata_json(metadata)
    now = _now(now)
    fingerprint = _fingerprint(
        {
            "user_id": user_id,
            "event_type": event_type,
            "expected_version": expected_version,
            "source": source,
            "metadata": json.loads(metadata_json),
        }
    )
    try:
        with atomic(conn):
            existing = conn.execute(
                "SELECT * FROM account_lifecycle_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["request_fingerprint"] != fingerprint
                    or existing["user_id"] != user_id
                ):
                    raise AccountStateConflict()
                return AccountMutation(
                    _public_user(_user_row(conn, user_id)),
                    _public_lifecycle(existing),
                )
            user = _user_row(conn, user_id)
            if user["row_version"] != expected_version:
                raise StaleAccountVersion()
            target = LIFECYCLE_TRANSITIONS.get((user["lifecycle_status"], event_type))
            if target is None:
                raise AccountStateConflict()
            occurred_at = _timestamp(now)
            event = _insert_lifecycle_event(
                conn,
                user_id=user_id,
                event_type=event_type,
                occurred_at=occurred_at,
                source=source,
                version_before=expected_version,
                version_after=expected_version + 1,
                metadata=json.loads(metadata_json),
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
            )
            cursor = conn.execute(
                """
                UPDATE users SET lifecycle_status = ?, row_version = row_version + 1, updated_at = ?
                WHERE user_id = ? AND row_version = ? AND lifecycle_status = ?
                """,
                (target, occurred_at, user_id, expected_version, user["lifecycle_status"]),
            )
            if cursor.rowcount != 1:
                raise StaleAccountVersion()
            return AccountMutation(_public_user(_user_row(conn, user_id)), event)
    except sqlite3.IntegrityError as exc:
        raise AccountStateConflict() from exc


def append_consent_event(
    conn,
    *,
    user_id: str,
    purpose: str,
    policy_version: str,
    action: str,
    source: str,
    idempotency_key: str,
    metadata: dict | None = None,
    occurred_at: datetime | None = None,
) -> PublicConsent:
    user_id = _strict_id(user_id, "user_id", "usr_")
    if purpose not in CONSENT_PURPOSES:
        raise InvalidAccountInput("Unknown consent purpose.")
    if action not in CONSENT_ACTIONS:
        raise InvalidAccountInput("Unknown consent action.")
    policy_version = _strict_text(policy_version, "policy_version", maximum=128)
    source = _source_text(source, "source")
    idempotency_key = _idempotency_key(idempotency_key)
    metadata_json = _metadata_json(metadata)
    occurred_at = _now(occurred_at)
    fingerprint = _fingerprint(
        {
            "user_id": user_id,
            "purpose": purpose,
            "policy_version": policy_version,
            "action": action,
            "source": source,
            "metadata": json.loads(metadata_json),
        }
    )
    event_id = _random_id("cns")
    try:
        with atomic(conn):
            existing = conn.execute(
                "SELECT * FROM consent_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["request_fingerprint"] != fingerprint
                    or existing["user_id"] != user_id
                ):
                    raise AccountStateConflict()
                return _public_consent(existing)
            user = _user_row(conn, user_id)
            if user["lifecycle_status"] not in {"active", "suspended"}:
                raise AccountStateConflict()
            last = conn.execute(
                """
                SELECT * FROM consent_events WHERE user_id = ? AND purpose = ?
                ORDER BY consent_version_after DESC LIMIT 1
                """,
                (user_id, purpose),
            ).fetchone()
            if last is None and action != "granted":
                raise AccountStateConflict()
            if last is not None:
                if last["action"] == action or _parse_timestamp(last["occurred_at"]) >= occurred_at:
                    raise AccountStateConflict()
            version_before = last["consent_version_after"] if last is not None else 0
            conn.execute(
                """
                INSERT INTO consent_events (
                  consent_event_id, user_id, purpose, policy_version, action,
                  occurred_at, source, consent_version_before, consent_version_after,
                  metadata_json, idempotency_key, request_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    user_id,
                    purpose,
                    policy_version,
                    action,
                    _timestamp(occurred_at),
                    source,
                    version_before,
                    version_before + 1,
                    metadata_json,
                    idempotency_key,
                    fingerprint,
                ),
            )
            return _public_consent(
                conn.execute(
                    "SELECT * FROM consent_events WHERE consent_event_id = ?", (event_id,)
                ).fetchone()
            )
    except sqlite3.IntegrityError as exc:
        raise AccountStateConflict() from exc


def effective_consent(conn, *, user_id: str, purpose: str) -> PublicConsent | None:
    user_id = _strict_id(user_id, "user_id", "usr_")
    if purpose not in CONSENT_PURPOSES:
        raise InvalidAccountInput("Unknown consent purpose.")
    _user_row(conn, user_id)
    row = conn.execute(
        """
        SELECT * FROM consent_events WHERE user_id = ? AND purpose = ?
        ORDER BY consent_version_after DESC LIMIT 1
        """,
        (user_id, purpose),
    ).fetchone()
    return _public_consent(row) if row else None


def request_account_deletion(
    conn,
    *,
    user_id: str,
    expected_version: int,
    cooling_period: timedelta,
    purge_after: timedelta,
    request_source: str,
    idempotency_key: str,
    now: datetime | None = None,
    failure_injector=None,
) -> DeletionMutation:
    user_id = _strict_id(user_id, "user_id", "usr_")
    _expected_version(expected_version)
    cooling_period = _positive_timedelta(cooling_period, "cooling_period")
    purge_after = _positive_timedelta(purge_after, "purge_after")
    if purge_after < cooling_period:
        raise InvalidAccountInput("Purge deadline cannot precede the cooling period.")
    request_source = _source_text(request_source, "request_source")
    idempotency_key = _idempotency_key(idempotency_key)
    now = _now(now)
    request_id = _random_id("del")
    fingerprint = _fingerprint(
        {
            "user_id": user_id,
            "expected_version": expected_version,
            "cooling_seconds": int(cooling_period.total_seconds()),
            "purge_seconds": int(purge_after.total_seconds()),
            "request_source": request_source,
        }
    )
    try:
        with atomic(conn):
            existing = conn.execute(
                "SELECT * FROM account_deletion_requests WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["request_fingerprint"] != fingerprint
                    or existing["user_id"] != user_id
                ):
                    raise AccountStateConflict()
                return _deletion_replay(conn, existing)
            user = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if user is None:
                raise AccountStateConflict()
            current_request = conn.execute(
                """
                SELECT * FROM account_deletion_requests
                WHERE user_id = ? AND status IN ('pending_cooling', 'deactivated_pending_purge')
                ORDER BY requested_at DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if current_request is not None:
                if _compatible_pending_deletion(
                    current_request, cooling_period, purge_after, request_source
                ):
                    return _deletion_replay(conn, current_request)
                raise AccountStateConflict()
            if user["row_version"] != expected_version:
                raise StaleAccountVersion()
            if user["lifecycle_status"] not in {"active", "suspended"}:
                raise AccountStateConflict()
            occurred = _timestamp(now)
            event = _insert_lifecycle_event(
                conn,
                user_id=user_id,
                event_type="deletion_requested",
                occurred_at=occurred,
                source=request_source,
                version_before=expected_version,
                version_after=expected_version + 1,
                metadata={},
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
            )
            _inject(failure_injector, "after_deletion_event_insert")
            cursor = conn.execute(
                """
                UPDATE users
                SET lifecycle_status = 'deletion_requested', row_version = row_version + 1,
                    updated_at = ?, deletion_requested_at = ?
                WHERE user_id = ? AND row_version = ? AND lifecycle_status = ?
                """,
                (occurred, occurred, user_id, expected_version, user["lifecycle_status"]),
            )
            if cursor.rowcount != 1:
                raise StaleAccountVersion()
            _inject(failure_injector, "after_deletion_user_update")
            conn.execute(
                """
                INSERT INTO account_deletion_requests (
                  deletion_request_id, user_id, requested_at, cooling_period_ends_at,
                  purge_eligible_at, status, request_source, restore_lifecycle_status,
                  idempotency_key, request_fingerprint
                ) VALUES (?, ?, ?, ?, ?, 'pending_cooling', ?, ?, ?, ?)
                """,
                (
                    request_id,
                    user_id,
                    occurred,
                    _timestamp(now + cooling_period),
                    _timestamp(now + purge_after),
                    request_source,
                    user["lifecycle_status"],
                    idempotency_key,
                    fingerprint,
                ),
            )
            _inject(failure_injector, "after_deletion_request_insert")
            revoked = _revoke_all_sessions(
                conn, user_id, "account_deactivation_requested", now
            )
            _inject(failure_injector, "after_deletion_session_revocation")
            return DeletionMutation(
                _public_user(_user_row(conn, user_id)),
                _public_deletion(_deletion_row(conn, request_id)),
                event,
                revoked,
            )
    except sqlite3.IntegrityError as exc:
        raise AccountStateConflict() from exc


def cancel_deletion_request(
    conn,
    *,
    user_id: str,
    expected_version: int,
    source: str,
    idempotency_key: str,
    now: datetime | None = None,
) -> DeletionMutation:
    return _finish_deletion_request(
        conn,
        user_id=user_id,
        expected_version=expected_version,
        source=source,
        idempotency_key=idempotency_key,
        action="cancel",
        deactivation_evidence=None,
        now=now,
    )


def deactivate_account_after_cooling(
    conn,
    *,
    user_id: str,
    expected_version: int,
    source: str,
    idempotency_key: str,
    deactivation_evidence: dict | None = None,
    now: datetime | None = None,
) -> DeletionMutation:
    evidence = json.loads(_metadata_json(deactivation_evidence))
    return _finish_deletion_request(
        conn,
        user_id=user_id,
        expected_version=expected_version,
        source=source,
        idempotency_key=idempotency_key,
        action="deactivate",
        deactivation_evidence=evidence,
        now=now,
    )


def _finish_deletion_request(
    conn,
    *,
    user_id,
    expected_version,
    source,
    idempotency_key,
    action,
    deactivation_evidence,
    now,
):
    user_id = _strict_id(user_id, "user_id", "usr_")
    _expected_version(expected_version)
    source = _source_text(source, "source")
    idempotency_key = _idempotency_key(idempotency_key)
    now = _now(now)
    fingerprint = _fingerprint(
        {
            "user_id": user_id,
            "expected_version": expected_version,
            "source": source,
            "action": action,
            "deactivation_evidence": deactivation_evidence,
        }
    )
    try:
        with atomic(conn):
            existing_event = conn.execute(
                "SELECT * FROM account_lifecycle_events WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing_event is not None:
                if (
                    existing_event["request_fingerprint"] != fingerprint
                    or existing_event["user_id"] != user_id
                ):
                    raise AccountStateConflict()
                request = conn.execute(
                    "SELECT * FROM account_deletion_requests WHERE user_id = ? ORDER BY requested_at DESC LIMIT 1",
                    (user_id,),
                ).fetchone()
                if request is None:
                    raise AccountStateConflict()
                return DeletionMutation(
                    _public_user(_user_row(conn, user_id)),
                    _public_deletion(request),
                    _public_lifecycle(existing_event),
                    0,
                    True,
                )
            user = _user_row(conn, user_id)
            if user["row_version"] != expected_version:
                raise StaleAccountVersion()
            if user["lifecycle_status"] != "deletion_requested":
                raise AccountStateConflict()
            request = conn.execute(
                "SELECT * FROM account_deletion_requests WHERE user_id = ? AND status = 'pending_cooling'",
                (user_id,),
            ).fetchone()
            if request is None:
                raise AccountStateConflict()
            occurred = _timestamp(now)
            if action == "deactivate":
                if now < _parse_timestamp(request["cooling_period_ends_at"]):
                    raise AccountStateConflict()
                target = "deactivated_pending_purge"
                event_type = "account_deactivated_pending_purge"
                request_status = "deactivated_pending_purge"
                request_field = "deactivated_at"
                metadata = deactivation_evidence
            else:
                target = request["restore_lifecycle_status"]
                event_type = "deletion_cancelled"
                request_status = "cancelled"
                request_field = "cancelled_at"
                metadata = {"restored_status": target}
            event = _insert_lifecycle_event(
                conn,
                user_id=user_id,
                event_type=event_type,
                occurred_at=occurred,
                source=source,
                version_before=expected_version,
                version_after=expected_version + 1,
                metadata=metadata,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
            )
            cursor = conn.execute(
                f"""
                UPDATE account_deletion_requests
                SET status = ?, {request_field} = ?, deactivation_evidence_json = ?
                WHERE deletion_request_id = ? AND status = 'pending_cooling'
                """,
                (
                    request_status,
                    occurred,
                    _metadata_json(deactivation_evidence),
                    request["deletion_request_id"],
                ),
            )
            if cursor.rowcount != 1:
                raise AccountStateConflict()
            cursor = conn.execute(
                """
                UPDATE users
                SET lifecycle_status = ?, row_version = row_version + 1, updated_at = ?,
                    deletion_requested_at = CASE WHEN ? = 'cancelled' THEN NULL ELSE deletion_requested_at END,
                    deactivated_at = CASE WHEN ? = 'deactivated_pending_purge' THEN ? ELSE deactivated_at END
                WHERE user_id = ? AND row_version = ? AND lifecycle_status = 'deletion_requested'
                """,
                (
                    target,
                    occurred,
                    request_status,
                    request_status,
                    occurred,
                    user_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleAccountVersion()
            revoked = _revoke_all_sessions(
                conn, user_id, "account_deactivation_requested", now
            )
            return DeletionMutation(
                _public_user(_user_row(conn, user_id)),
                _public_deletion(_deletion_row(conn, request["deletion_request_id"])),
                event,
                revoked,
                False,
            )
    except sqlite3.IntegrityError as exc:
        raise AccountStateConflict() from exc


def _validate_verified_identity_values(
    *,
    provider,
    provider_subject,
    verified_email,
    email_verified,
    authenticated_at,
    metadata_version,
):
    if provider not in PROVIDERS or type(email_verified) is not bool:
        raise AuthenticationUnavailable()
    subject = _strict_text(provider_subject, "provider_subject", maximum=1024)
    if email_verified and verified_email is None:
        raise AuthenticationUnavailable()
    email = normalize_email(verified_email) if verified_email is not None else None
    authenticated = _aware_datetime(authenticated_at, "authenticated_at")
    version = _strict_text(metadata_version, "metadata_version", maximum=128)
    return {
        "provider": provider,
        "provider_subject": subject,
        "verified_email": email,
        "email_verified": email_verified,
        "authenticated_at": authenticated,
        "metadata_version": version,
    }


def _trusted_identity(identity, identity_verifier):
    if not identity_verifier._accepts(identity):
        raise AuthenticationUnavailable()
    _validate_verified_identity_values(
        provider=identity.provider,
        provider_subject=identity.provider_subject,
        verified_email=identity.verified_email,
        email_verified=identity.email_verified,
        authenticated_at=identity.authenticated_at,
        metadata_version=identity.metadata_version,
    )
    return identity


def _insert_session(
    conn,
    *,
    session_id,
    user_id,
    raw_token,
    raw_csrf,
    now,
    idle_expires_at,
    absolute_expires_at,
    idempotency_key,
    fingerprint,
):
    conn.execute(
        """
        INSERT INTO account_sessions (
          session_id, user_id, token_hash, token_hash_version, csrf_secret_hash,
          csrf_hash_version, created_at, last_seen_at, idle_expires_at,
          absolute_expires_at, session_version,
          creation_idempotency_key, request_fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            session_id,
            user_id,
            _secret_hash(raw_token),
            TOKEN_HASH_VERSION,
            _secret_hash(raw_csrf),
            TOKEN_HASH_VERSION,
            _timestamp(now),
            _timestamp(now),
            _timestamp(idle_expires_at),
            _timestamp(absolute_expires_at),
            idempotency_key,
            fingerprint,
        ),
    )


def _active_session_for_token(conn, raw_token, now):
    digest = _secret_hash(raw_token)
    row = conn.execute(
        """
        SELECT s.*, u.lifecycle_status
          , (SELECT edge.predecessor_session_id
             FROM account_session_rotations edge
             WHERE edge.replacement_session_id = s.session_id) AS parent_session_id
          , (SELECT edge.replacement_session_id
             FROM account_session_rotations edge
             WHERE edge.predecessor_session_id = s.session_id) AS replacement_session_id
        FROM account_sessions s JOIN users u ON u.user_id = s.user_id
        WHERE s.token_hash_version = ? AND s.token_hash = ?
        """,
        (TOKEN_HASH_VERSION, digest),
    ).fetchone()
    if row is None or not hmac.compare_digest(row["token_hash"], digest):
        raise SessionUnavailable()
    if (
        row["lifecycle_status"] != "active"
        or row["revoked_at"] is not None
        or row["rotated_at"] is not None
        or row["replacement_session_id"] is not None
        or now < _parse_timestamp(row["created_at"])
        or _parse_timestamp(row["idle_expires_at"]) <= now
        or _parse_timestamp(row["absolute_expires_at"]) <= now
    ):
        raise SessionUnavailable()
    return row


def _revoke_all_sessions(conn, user_id, reason, now):
    reason = _revoke_reason(reason)
    cursor = conn.execute(
        """
        UPDATE account_sessions
        SET revoked_at = ?, revoke_reason = ?, session_version = session_version + 1
        WHERE user_id = ? AND revoked_at IS NULL
        """,
        (_timestamp(now), reason, user_id),
    )
    return cursor.rowcount


def _insert_lifecycle_event(
    conn,
    *,
    user_id,
    event_type,
    occurred_at,
    source,
    version_before,
    version_after,
    metadata,
    idempotency_key,
    fingerprint,
):
    event_id = _random_id("life")
    conn.execute(
        """
        INSERT INTO account_lifecycle_events (
          lifecycle_event_id, user_id, event_type, occurred_at, source,
          account_version_before, account_version_after, metadata_json,
          idempotency_key, request_fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            user_id,
            event_type,
            occurred_at,
            source,
            version_before,
            version_after,
            _metadata_json(metadata),
            idempotency_key,
            fingerprint,
        ),
    )
    row = conn.execute(
        "SELECT * FROM account_lifecycle_events WHERE lifecycle_event_id = ?", (event_id,)
    ).fetchone()
    return _public_lifecycle(row)


def _user_row(conn, user_id):
    row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        raise AccountStateConflict()
    return row


def _session_row(conn, session_id):
    row = conn.execute(
        """
        SELECT s.*,
          (SELECT edge.predecessor_session_id
           FROM account_session_rotations edge
           WHERE edge.replacement_session_id = s.session_id) AS parent_session_id,
          (SELECT edge.replacement_session_id
           FROM account_session_rotations edge
           WHERE edge.predecessor_session_id = s.session_id) AS replacement_session_id
        FROM account_sessions s
        WHERE s.session_id = ?
        """,
        (session_id,),
    ).fetchone()
    if row is None:
        raise SessionUnavailable()
    return row


def _deletion_row(conn, request_id):
    row = conn.execute(
        "SELECT * FROM account_deletion_requests WHERE deletion_request_id = ?", (request_id,)
    ).fetchone()
    if row is None:
        raise AccountStateConflict()
    return row


def _public_user(row):
    return PublicUser(
        row["user_id"],
        row["lifecycle_status"],
        row["row_version"],
        row["created_at"],
        row["updated_at"],
        row["deletion_requested_at"],
        row["deactivated_at"],
    )


def _public_identity(row):
    return PublicIdentity(
        row["auth_identity_id"],
        row["user_id"],
        row["provider"],
        bool(row["email_verified"]),
        row["created_at"],
        row["last_authenticated_at"],
        row["disabled_at"],
    )


def _public_invitation(row):
    return PublicInvitation(
        row["invitation_id"],
        row["email_display_hint"],
        row["created_at"],
        row["expires_at"],
        row["invitation_status"],
        row["created_by"],
    )


def _public_session(row):
    return PublicSession(
        row["session_id"],
        row["user_id"],
        row["created_at"],
        row["last_seen_at"],
        row["idle_expires_at"],
        row["absolute_expires_at"],
        row["rotated_at"],
        row["revoked_at"],
        row["parent_session_id"],
        row["replacement_session_id"],
        row["session_version"],
    )


def _public_consent(row):
    return PublicConsent(
        row["consent_event_id"],
        row["user_id"],
        row["purpose"],
        row["policy_version"],
        row["action"],
        row["occurred_at"],
        row["source"],
        row["consent_version_before"],
        row["consent_version_after"],
    )


def _public_lifecycle(row):
    return PublicLifecycleEvent(
        row["lifecycle_event_id"],
        row["user_id"],
        row["event_type"],
        row["occurred_at"],
        row["source"],
        row["account_version_before"],
        row["account_version_after"],
    )


def _public_deletion(row):
    return PublicDeletionRequest(
        row["deletion_request_id"],
        row["user_id"],
        row["requested_at"],
        row["cooling_period_ends_at"],
        row["purge_eligible_at"],
        row["cancelled_at"],
        row["deactivated_at"],
        row["status"],
        row["request_source"],
        row["restore_lifecycle_status"],
        row["deactivation_evidence_json"] != "{}",
    )


def _metadata_json(metadata):
    if metadata is None:
        metadata = {}
    if type(metadata) is not dict:
        raise InvalidAccountInput("Metadata must be an object.")
    try:
        _validate_metadata_value(metadata, depth=0)
        encoded = json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except RecursionError as exc:
        raise InvalidAccountInput("Metadata exceeds the allowed depth.") from exc
    except (TypeError, ValueError) as exc:
        raise InvalidAccountInput("Metadata must be deterministic JSON.") from exc
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise InvalidAccountInput("Metadata exceeds the allowed size.")
    return encoded


def validate_account_metadata(metadata):
    return json.loads(_metadata_json(metadata))


def _validate_metadata_value(value, *, depth):
    if depth > MAX_METADATA_DEPTH:
        raise InvalidAccountInput("Metadata exceeds the allowed depth.")
    if type(value) is dict:
        if len(value) > MAX_METADATA_KEYS:
            raise InvalidAccountInput("Metadata contains too many keys.")
        for key, child in value.items():
            if type(key) is not str or not (1 <= len(key) <= MAX_METADATA_KEY_LENGTH):
                raise InvalidAccountInput("Metadata contains an invalid key.")
            normalized = _normalize_metadata_key(key)
            if _sensitive_metadata_name(normalized):
                raise InvalidAccountInput("Metadata contains privacy-sensitive fields.")
            _validate_metadata_value(child, depth=depth + 1)
        return
    if type(value) is list:
        if len(value) > MAX_METADATA_LIST_ITEMS:
            raise InvalidAccountInput("Metadata list exceeds the allowed length.")
        for child in value:
            _validate_metadata_value(child, depth=depth + 1)
        return
    if type(value) is str:
        if len(value) > MAX_METADATA_STRING_LENGTH:
            raise InvalidAccountInput("Metadata string exceeds the allowed length.")
        if re.search(r"(?i)\bbearer\s+[a-z0-9._~+/-]{8,}", value):
            raise InvalidAccountInput("Metadata contains privacy-sensitive content.")
        return
    if value is None or type(value) in {bool, int, float}:
        if type(value) is float and (value != value or value in {float("inf"), float("-inf")}):
            raise InvalidAccountInput("Metadata contains a non-finite number.")
        return
    raise InvalidAccountInput("Metadata contains an unsupported value.")


def _normalize_metadata_key(key):
    normalized = unicodedata.normalize("NFKC", key).casefold()
    return re.sub(r"[-_.\s/:]+", "", normalized)


def _sensitive_metadata_name(normalized):
    return (
        normalized in SENSITIVE_METADATA_NAMES
        or normalized.endswith("token")
        or any(normalized.startswith(prefix) for prefix in SENSITIVE_METADATA_PREFIXES)
    )


def _fingerprint(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _random_id(prefix):
    return f"{prefix}_{secrets.token_hex(16)}"


def _secret_hash(secret):
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _email_hint(normalized):
    local, domain = normalized.rsplit("@", 1)
    return f"{local[:1]}***@{domain}"


def _lookup_key(value):
    if type(value) is not bytes or len(value) < 32:
        raise InvalidAccountInput("Invitation lookup key must contain at least 256 bits.")
    return value


def _invitation_token(value):
    try:
        token = _strict_text(value, "invitation_token", minimum=70, maximum=512)
        invitation_id, secret = token.split(".", 1)
        invitation_id = _strict_id(invitation_id, "invitation_id", "inv_")
        secret = _strict_text(secret, "invitation_secret", minimum=32, maximum=256)
    except (InvalidAccountInput, ValueError) as exc:
        raise AuthenticationUnavailable() from exc
    return invitation_id, secret


def _session_token(value):
    return _strict_text(value, "session_token", minimum=32, maximum=1024)


def _csrf_secret(value):
    return _strict_text(value, "csrf_secret", minimum=32, maximum=1024)


def _revoke_reason(value):
    if value not in SAFE_REVOKE_REASONS:
        raise InvalidAccountInput("Session revoke reason is not supported.")
    return value


def _compatible_pending_deletion(request, cooling_period, purge_after, request_source):
    requested = _parse_timestamp(request["requested_at"])
    cooling = _parse_timestamp(request["cooling_period_ends_at"]) - requested
    purge = _parse_timestamp(request["purge_eligible_at"]) - requested
    return (
        cooling == cooling_period
        and purge == purge_after
        and request["request_source"] == request_source
    )


def _deletion_replay(conn, request):
    event = conn.execute(
        "SELECT * FROM account_lifecycle_events WHERE idempotency_key = ?",
        (request["idempotency_key"],),
    ).fetchone()
    if event is None:
        raise AccountStateConflict()
    return DeletionMutation(
        _public_user(_user_row(conn, request["user_id"])),
        _public_deletion(request),
        _public_lifecycle(event),
        0,
        True,
    )


def _strict_id(value, field, prefix):
    text = _strict_text(value, field, minimum=len(prefix) + 32, maximum=128)
    if not text.startswith(prefix):
        raise InvalidAccountInput(f"{field} is not valid.")
    return text


def _idempotency_key(value):
    return _strict_text(value, "idempotency_key", minimum=8, maximum=256)


def _source_text(value, field):
    text = _strict_text(value, field, maximum=128)
    if "@" in text:
        raise InvalidAccountInput(f"{field} must use a stable non-email actor identifier.")
    return text


def _strict_text(value, field, *, minimum=1, maximum):
    if type(value) is not str or value != value.strip() or not (minimum <= len(value) <= maximum):
        raise InvalidAccountInput(f"{field} is not valid.")
    if any(ord(char) < 32 for char in value):
        raise InvalidAccountInput(f"{field} is not valid.")
    return value


def _expected_version(value, *, session=False):
    if type(value) is not int or value < 1:
        if session:
            raise StaleSessionVersion()
        raise StaleAccountVersion()


def _positive_timedelta(value, field):
    if type(value) is not timedelta or value <= timedelta(0):
        raise InvalidAccountInput(f"{field} must be a positive timedelta.")
    return value


def _now(value):
    return _aware_datetime(value or datetime.now(timezone.utc), "now")


def _aware_datetime(value, field):
    if type(value) is not datetime or value.tzinfo is None:
        raise InvalidAccountInput(f"{field} must be a timezone-aware datetime.")
    return value.astimezone(timezone.utc)


def _timestamp(value):
    return _aware_datetime(value, "timestamp").replace(microsecond=0).isoformat()


def _parse_timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise AccountStateConflict("Persisted account state is invalid.") from exc
    if parsed.tzinfo is None:
        raise AccountStateConflict("Persisted account state is invalid.")
    return parsed.astimezone(timezone.utc)


def _inject(callback, point):
    if callback is not None:
        callback(point)
