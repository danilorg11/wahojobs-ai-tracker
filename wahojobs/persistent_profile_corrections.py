"""Bounded authenticated corrections for one existing persistent profile.

The browser owns no durable identity in this flow.  Every draft and immutable
confirmation artifact is derived from an authenticated account-native profile,
kept process-local until append, and committed only through the accepted
``append_profile_revision`` repository service.
"""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import re
import sqlite3
import threading
import time

from wahojobs.accounts import SessionUnavailable, validate_session_csrf
from wahojobs.browser_session_authentication import (
    BrowserSessionAuthenticationUnavailable,
    DurableBrowserSessionAuthenticationGateway,
)
from wahojobs.persistent_profile_read_authorization import (
    DurablePersistentProfileReadAuthorizationGateway,
    PersistentProfileReadAuthorizationDecision,
)
from wahojobs.persistent_profiles import (
    AppendProfileRevisionCommand,
    ConfirmedAboutYouTextSourceDraft,
    IdentityFreeCanonicalProfileV1,
    MAX_SOURCES,
    PersistentProfileDomainError,
    TrustedPersistentProfileReference,
    TrustedPrincipalContext,
    UserConfirmedCorrectionSourceDraft,
)
from wahojobs.persistent_profiles_repository import (
    PersistentProfileRepositoryDefiniteRollback,
    PersistentProfileRepositoryOutcomeUncertain,
    append_profile_revision,
    read_current_profile,
)
from wahojobs.profiles.canonical import PROFILE_SOURCE_USER_CORRECTION
from wahojobs.profiles.canonical_v2 import (
    CanonicalProfileV2Error,
    canonical_profile_v2_json_bytes,
    merge_server_review_correction_v2,
    parse_canonical_profile_v2_json,
    project_v2_to_review_v1,
    validate_canonical_profile_v2,
)


PROFILE_CORRECTION_ROUTE = "/account/profile"
PROFILE_CORRECTION_PURPOSE = "persistent_profile_correction_v1"
PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS = 600
PROFILE_CORRECTION_ARTIFACT_CAPACITY = 64
PROFILE_CORRECTION_CLAIM_WAIT_SECONDS = 5.0
_PROFILE_CORRECTION_VAULT_CLOSE_WAIT_SECONDS = 1.0
PROFILE_CORRECTION_ACTOR_TYPE = "authenticated_user"
PROFILE_CORRECTION_REASON_CODE = "profile.correction"
PROFILE_CORRECTION_CSRF_MESSAGE_PREFIX = b"wahojobs.profile-correction.v1\x00"
PROFILE_CORRECTION_ACTION_CSRF_MESSAGE_PREFIX = (
    b"wahojobs.profile-correction-action.v1\x00"
)

CORRECTION_ACTIONS = frozenset({"start", "redraft", "confirm", "apply"})

_CORRECTION_UPDATE_FIELDS = frozenset(
    {
        "accessibility_constraints",
        "administrative_support_skills",
        "availability",
        "avoid_keywords",
        "certifications",
        "city",
        "contribution_type",
        "country",
        "credential_status",
        "degrees",
        "domain_specific_skills",
        "education_fields",
        "education_level",
        "education_status",
        "eligible_countries",
        "employment_types",
        "excluded_domains",
        "flexible",
        "geographic_restrictions",
        "hard_constraints",
        "industries",
        "institutions",
        "jurisdictions",
        "job_titles",
        "languages",
        "licenses",
        "no_degree",
        "no_experience",
        "no_specialized_credentials",
        "occupational_families",
        "phone_preference",
        "professional_domains",
        "region",
        "remote",
        "schedule",
        "security_clearances",
        "seniority",
        "skills",
        "soft_preferences",
        "software_tools",
        "specialties",
        "synchronous_preference",
        "target_opportunity_types",
        "technical_skills",
        "total_years",
        "work_authorization",
        "writing_research_skills",
    }
)

_OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CORRECTION_DRAFT_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{24}$")
_CONFIRMATION_IDENTITY = re.compile(r"^[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CORRECTION_GRANT_ISSUANCE = object()
_PREPARED_REVIEW_ISSUANCE = object()


def _configuration_error():
    return ValueError("invalid_persistent_profile_correction_configuration")


class ProfileCorrectionRequestContext:
    """Sealed correction request facts accepted by durable session auth."""

    __slots__ = ("method", "route", "_authentication_input", "_sealed")

    def __init__(self, method, authentication_input):
        if method not in {"GET", "HEAD", "POST"}:
            raise _configuration_error()
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "route", PROFILE_CORRECTION_ROUTE)
        object.__setattr__(self, "_authentication_input", authentication_input)
        object.__setattr__(self, "_sealed", True)

    def authentication_input_for_gateway(self):
        return self._authentication_input

    def __setattr__(self, _name, _value):
        raise AttributeError("profile_correction_request_context_is_immutable")

    def __repr__(self):
        return (
            "ProfileCorrectionRequestContext("
            f"method={self.method!r}, route='/account/profile', "
            "authentication_input=<redacted>)"
        )

    def __reduce_ex__(self, _protocol):
        raise TypeError("profile_correction_request_context_not_serializable")


class TrustedProfileCorrectionGrant:
    """Mutation-specific authority for one session and current profile base."""

    __slots__ = (
        "_account_id",
        "_base_profile_json",
        "_base_profile_sha256",
        "_base_revision_id",
        "_base_revision_number",
        "_environment_namespace",
        "_principal",
        "_profile",
        "_session_id",
        "_sealed",
    )

    def __new__(cls, *_args, **_kwargs):
        raise _configuration_error()

    @classmethod
    def _issue(
        cls,
        capability,
        *,
        account_id,
        session_id,
        environment_namespace,
        principal,
        profile,
        base_revision_id,
        base_revision_number,
        base_profile_v2,
    ):
        if (
            cls is not TrustedProfileCorrectionGrant
            or capability is not _CORRECTION_GRANT_ISSUANCE
            or type(account_id) is not str
            or type(session_id) is not str
            or type(environment_namespace) is not str
            or type(principal) is not TrustedPrincipalContext
            or type(profile) is not TrustedPersistentProfileReference
            or principal.principal_id != profile.principal_id
            or principal.environment_namespace != environment_namespace
            or profile.environment_namespace != environment_namespace
            or type(base_revision_id) is not str
            or type(base_revision_number) is not int
            or base_revision_number < 1
        ):
            raise _configuration_error()
        canonical = validate_canonical_profile_v2(base_profile_v2)
        if canonical["identity"]["profile_id"] != profile.profile_id:
            raise _configuration_error()
        encoded = canonical_profile_v2_json_bytes(canonical)
        instance = object.__new__(cls)
        for name, value in {
            "_account_id": account_id,
            "_session_id": session_id,
            "_environment_namespace": environment_namespace,
            "_principal": principal,
            "_profile": profile,
            "_base_revision_id": base_revision_id,
            "_base_revision_number": base_revision_number,
            "_base_profile_json": encoded,
            "_base_profile_sha256": hashlib.sha256(encoded).hexdigest(),
            "_sealed": True,
        }.items():
            object.__setattr__(instance, name, value)
        return instance

    def __setattr__(self, _name, _value):
        raise AttributeError("trusted_profile_correction_grant_is_immutable")

    def principal_for_repository(self):
        return self._principal

    def profile_for_repository(self):
        return self._profile

    def trusted_base_profile_v2(self):
        return parse_canonical_profile_v2_json(self._base_profile_json)

    @property
    def base_revision_id(self):
        return self._base_revision_id

    @property
    def base_revision_number(self):
        return self._base_revision_number

    def confirmation_binding(self):
        """Exact process-private binding used by draft confirmation machinery."""

        return (
            self._account_id,
            self._session_id,
            self._environment_namespace,
            self._principal.principal_id,
            self._profile.profile_id,
            self._base_revision_id,
            self._base_revision_number,
            PROFILE_CORRECTION_PURPOSE,
            self._base_profile_sha256,
            "active",
        )

    def actor_profile_binding(self):
        """Stable authenticated binding that intentionally excludes currentness."""

        return (
            self._account_id,
            self._session_id,
            self._environment_namespace,
            self._principal.principal_id,
            self._profile.profile_id,
            PROFILE_CORRECTION_PURPOSE,
        )

    def draft_binding(self, secret):
        if type(secret) is not bytes or len(secret) < 32:
            raise _configuration_error()
        material = _canonical_json_bytes(
            {
                "account_id": self._account_id,
                "base_profile_sha256": self._base_profile_sha256,
                "base_revision_id": self._base_revision_id,
                "base_revision_number": self._base_revision_number,
                "environment_namespace": self._environment_namespace,
                "principal_id": self._principal.principal_id,
                "profile_id": self._profile.profile_id,
                "purpose": PROFILE_CORRECTION_PURPOSE,
                "session_id": self._session_id,
            }
        )
        return hmac.digest(secret, material, "sha256").hex()

    def __repr__(self):
        return "TrustedProfileCorrectionGrant(<redacted>)"

    def __reduce_ex__(self, _protocol):
        raise TypeError("trusted_profile_correction_grant_not_serializable")


@dataclass(frozen=True, slots=True, repr=False, init=False)
class PreparedProfileCorrectionReview:
    """Sealed process-local full review material for browser confirmation."""

    _authority_binding: tuple = field(repr=False)
    _reviewed_profile_json: bytes = field(repr=False)
    _corrected_profile_v2_json: bytes = field(repr=False)
    _normalized_updates_json: bytes = field(repr=False)
    _proof: bytes = field(repr=False)

    def __init__(self, *_args, **_kwargs):
        raise _configuration_error()

    def reviewed_profile_for_browser(self):
        return IdentityFreeCanonicalProfileV1.from_json_bytes(
            self._reviewed_profile_json
        )

    def __repr__(self):
        return "PreparedProfileCorrectionReview(<redacted>)"

    def __reduce_ex__(self, _protocol):
        raise TypeError("prepared_profile_correction_review_not_serializable")


@dataclass(frozen=True, slots=True, repr=False)
class ProfileCorrectionAuthorityResult:
    state: str
    _grant: object | None = field(default=None, repr=False)

    def __post_init__(self):
        states = {
            "authorized",
            "empty",
            "authentication_required",
            "csrf_denied",
            "authorization_denied",
            "profile_unavailable",
            "schema_unavailable",
            "unavailable",
        }
        if self.state not in states:
            raise _configuration_error()
        if self.state == "authorized":
            if type(self._grant) is not TrustedProfileCorrectionGrant:
                raise _configuration_error()
        elif self._grant is not None:
            raise _configuration_error()

    def grant_for_service(self):
        return self._grant if self.state == "authorized" else None

    def __repr__(self):
        return f"ProfileCorrectionAuthorityResult(state={self.state!r}, grant=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class ConfirmedProfileCorrectionArtifactOffer:
    artifact_reference: str = field(repr=False)
    csrf_proof: str = field(repr=False)

    def __post_init__(self):
        if (
            type(self.artifact_reference) is not str
            or _OPAQUE_REFERENCE.fullmatch(self.artifact_reference) is None
            or type(self.csrf_proof) is not str
            or _OPAQUE_REFERENCE.fullmatch(self.csrf_proof) is None
        ):
            raise _configuration_error()

    def __repr__(self):
        return "ConfirmedProfileCorrectionArtifactOffer(<redacted>)"

    def __reduce_ex__(self, _protocol):
        raise TypeError("confirmed_profile_correction_offer_not_serializable")


@dataclass(frozen=True, slots=True)
class ProfileCorrectionOutcome:
    state: str
    replayed: bool = False

    def __post_init__(self):
        if self.state not in {
            "corrected",
            "stale",
            "conflict",
            "gone",
            "temporary_contention",
            "unavailable",
        } or type(self.replayed) is not bool:
            raise _configuration_error()
        if self.state != "corrected" and self.replayed:
            raise _configuration_error()


@dataclass(frozen=True, slots=True, repr=False)
class _ConfirmedCorrectionSnapshot:
    artifact_reference: str = field(repr=False)
    command: AppendProfileRevisionCommand = field(repr=False)
    account_id: str = field(repr=False)
    session_id: str = field(repr=False)
    environment_namespace: str = field(repr=False)
    principal_id: str = field(repr=False)
    profile_id: str = field(repr=False)
    base_revision_id: str = field(repr=False)
    base_revision_number: int
    base_profile_json: bytes = field(repr=False)
    purpose: str
    content_fingerprint: str = field(repr=False)

    def confirmation_binding(self):
        return (
            self.account_id,
            self.session_id,
            self.environment_namespace,
            self.principal_id,
            self.profile_id,
            self.base_revision_id,
            self.base_revision_number,
            self.purpose,
            hashlib.sha256(self.base_profile_json).hexdigest(),
            "active",
        )

    def actor_profile_binding(self):
        return (
            self.account_id,
            self.session_id,
            self.environment_namespace,
            self.principal_id,
            self.profile_id,
            self.purpose,
        )


@dataclass(slots=True, repr=False)
class _CorrectionArtifactRecord:
    snapshot: _ConfirmedCorrectionSnapshot = field(repr=False)
    deadline: float
    confirmation_identity: str | None = field(default=None, repr=False)
    state: str = "available"
    completed: ProfileCorrectionOutcome | None = None


class ConfirmedProfileCorrectionArtifactVault:
    """Bounded process-local ownership with serialized confirmation claims."""

    __slots__ = (
        "_active",
        "_closing",
        "_closed",
        "_condition",
        "_confirmation_references",
        "_monotonic",
        "_operations",
        "_records",
    )

    def __init__(self, *, monotonic=time.monotonic):
        if not callable(monotonic):
            raise _configuration_error()
        self._monotonic = monotonic
        self._condition = threading.Condition(threading.Lock())
        self._records = {}
        self._confirmation_references = {}
        self._operations = set()
        self._active = False
        self._closing = False
        self._closed = False

    def activate(self):
        with self._condition:
            if self._closing or self._closed:
                raise RuntimeError("confirmed_profile_correction_vault_unavailable")
            self._active = True
            return True

    def issue(self, reference, snapshot, *, confirmation_identity=None):
        if (
            type(reference) is not str
            or _OPAQUE_REFERENCE.fullmatch(reference) is None
            or type(snapshot) is not _ConfirmedCorrectionSnapshot
            or snapshot.artifact_reference != reference
            or (
                confirmation_identity is not None
                and (
                    type(confirmation_identity) is not str
                    or _CONFIRMATION_IDENTITY.fullmatch(confirmation_identity) is None
                )
            )
            or not _snapshot_integrity_valid(snapshot)
        ):
            raise _configuration_error()
        now = _trusted_monotonic(self._monotonic())
        with self._condition:
            if self._closing or self._closed or not self._active:
                raise RuntimeError("confirmed_profile_correction_vault_unavailable")
            self._purge_expired_locked(now)
            if reference in self._records:
                raise RuntimeError("confirmed_profile_correction_reference_conflict")
            if confirmation_identity in self._confirmation_references:
                raise RuntimeError("confirmed_profile_correction_confirmation_conflict")
            if len(self._records) >= PROFILE_CORRECTION_ARTIFACT_CAPACITY:
                raise RuntimeError("confirmed_profile_correction_vault_unavailable")
            self._records[reference] = _CorrectionArtifactRecord(
                snapshot,
                now + PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS,
                confirmation_identity,
            )
            if confirmation_identity is not None:
                self._confirmation_references[confirmation_identity] = reference
            return reference

    def recover_confirmation(self, confirmation_identity, binding):
        if (
            type(confirmation_identity) is not str
            or _CONFIRMATION_IDENTITY.fullmatch(confirmation_identity) is None
            or type(binding) is not tuple
            or len(binding) != 10
        ):
            raise _configuration_error()
        now = _trusted_monotonic(self._monotonic())
        with self._condition:
            if self._closing or self._closed or not self._active:
                return "unavailable", None
            self._purge_expired_locked(now)
            reference = self._confirmation_references.get(confirmation_identity)
            record = self._records.get(reference)
            if record is None:
                return "absent", None
            if (
                record.confirmation_identity != confirmation_identity
                or record.snapshot.confirmation_binding() != binding
                or not _snapshot_integrity_valid(record.snapshot)
            ):
                return "denied", None
            return "found", reference

    def _begin_consume(self):
        with self._condition:
            if self._closing or self._closed or not self._active:
                return None
            token = object()
            self._operations.add(token)
            self._condition.notify_all()
            return token

    def _finish_consume(self, token):
        with self._condition:
            if token not in self._operations:
                return False
            self._operations.remove(token)
            self._condition.notify_all()
            return True

    def claim(self, consume_token, reference, grant, operation):
        if (
            consume_token is None
            or type(reference) is not str
            or _OPAQUE_REFERENCE.fullmatch(reference) is None
            or type(grant) is not TrustedProfileCorrectionGrant
            or not callable(operation)
        ):
            return ProfileCorrectionOutcome("gone")
        deadline = _trusted_monotonic(self._monotonic()) + PROFILE_CORRECTION_CLAIM_WAIT_SECONDS
        with self._condition:
            while True:
                if consume_token not in self._operations:
                    return ProfileCorrectionOutcome("unavailable")
                if self._closing or self._closed or not self._active:
                    return ProfileCorrectionOutcome("unavailable")
                now = _trusted_monotonic(self._monotonic())
                self._purge_expired_locked(now)
                record = self._records.get(reference)
                if record is None:
                    return ProfileCorrectionOutcome("gone")
                if (
                    record.snapshot.actor_profile_binding()
                    != grant.actor_profile_binding()
                    or not _snapshot_integrity_valid(record.snapshot)
                ):
                    return ProfileCorrectionOutcome("gone")
                if record.state == "completed":
                    return ProfileCorrectionOutcome("corrected", replayed=True)
                if record.state == "stale":
                    return ProfileCorrectionOutcome("stale")
                if record.state == "conflict":
                    return ProfileCorrectionOutcome("conflict")
                if record.state == "in_flight":
                    remaining = deadline - now
                    if remaining <= 0:
                        return ProfileCorrectionOutcome("temporary_contention")
                    self._condition.wait(timeout=remaining)
                    continue
                if record.state not in {"available", "reconcile"}:
                    return ProfileCorrectionOutcome("gone")
                record.state = "in_flight"
                snapshot = record.snapshot
                break
        try:
            outcome = operation(snapshot)
            if type(outcome) is not ProfileCorrectionOutcome:
                raise RuntimeError("invalid_profile_correction_claim_outcome")
        except BaseException:
            with self._condition:
                current = self._records.get(reference)
                if current is record and current.state == "in_flight":
                    current.state = "reconcile"
                    self._condition.notify_all()
            raise
        with self._condition:
            current = self._records.get(reference)
            if current is not record or current.state != "in_flight":
                return ProfileCorrectionOutcome("unavailable")
            if outcome.state == "corrected":
                current.state = "completed"
                current.completed = outcome
            elif outcome.state in {"stale", "conflict"}:
                current.state = outcome.state
            else:
                current.state = "reconcile" if outcome.state == "unavailable" else "available"
            self._condition.notify_all()
        return outcome

    def close(self):
        deadline = time.monotonic() + _PROFILE_CORRECTION_VAULT_CLOSE_WAIT_SECONDS
        with self._condition:
            if self._closed:
                return True
            self._closing = True
            self._active = False
            self._condition.notify_all()
            while self._operations:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            self._records.clear()
            self._confirmation_references.clear()
            self._closed = True
            self._condition.notify_all()
            return True

    @property
    def closed(self):
        with self._condition:
            return self._closed

    def _purge_expired_locked(self, now):
        for reference, record in tuple(self._records.items()):
            if now < record.deadline or record.state == "in_flight":
                continue
            self._records.pop(reference, None)
            if record.confirmation_identity is not None:
                self._confirmation_references.pop(record.confirmation_identity, None)


class PersistentProfileCorrectionService:
    """Authorize, bind, validate, and append immutable profile corrections."""

    __slots__ = (
        "_append_revision",
        "_authentication_gateway",
        "_authorization_gateway",
        "_binding_secret",
        "_clock",
        "_closed",
        "_read_connection_provider",
        "_token_factory",
        "_vault",
        "_write_connection_provider",
    )

    def __init__(
        self,
        *,
        authentication_gateway,
        authorization_gateway,
        read_connection_provider,
        write_connection_provider,
        vault,
        clock,
        token_factory,
        binding_secret,
        append_revision=append_profile_revision,
    ):
        if (
            type(authentication_gateway) is not DurableBrowserSessionAuthenticationGateway
            or type(authorization_gateway)
            is not DurablePersistentProfileReadAuthorizationGateway
            or not callable(read_connection_provider)
            or not callable(write_connection_provider)
            or type(vault) is not ConfirmedProfileCorrectionArtifactVault
            or not callable(clock)
            or not callable(token_factory)
            or type(binding_secret) is not bytes
            or len(binding_secret) < 32
            or not callable(append_revision)
        ):
            raise _configuration_error()
        self._authentication_gateway = authentication_gateway
        self._authorization_gateway = authorization_gateway
        self._read_connection_provider = read_connection_provider
        self._write_connection_provider = write_connection_provider
        self._vault = vault
        self._clock = clock
        self._token_factory = token_factory
        self._binding_secret = bytes(binding_secret)
        self._append_revision = append_revision
        self._closed = False

    def activate(self):
        if self._closed:
            raise RuntimeError("profile_correction_service_unavailable")
        return self._vault.activate()

    def authorize_request(
        self,
        *,
        method,
        authentication_input,
        session_token,
        csrf_secret=None,
        action=None,
        action_proof=None,
    ):
        if self._closed or method not in {"GET", "HEAD", "POST"}:
            return ProfileCorrectionAuthorityResult("unavailable")
        if type(session_token) is not str or _OPAQUE_REFERENCE.fullmatch(session_token) is None:
            return ProfileCorrectionAuthorityResult("authentication_required")
        if (
            type(csrf_secret) is not str
            or _OPAQUE_REFERENCE.fullmatch(csrf_secret) is None
            or (
                method == "POST"
                and (
                    action not in CORRECTION_ACTIONS
                    or type(action_proof) is not str
                    or _OPAQUE_REFERENCE.fullmatch(action_proof) is None
                )
            )
        ):
            return ProfileCorrectionAuthorityResult("csrf_denied")
        try:
            with self._read_connection_provider() as connection:
                if (
                    not isinstance(connection, sqlite3.Connection)
                    or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
                    or connection.execute("PRAGMA query_only").fetchone()[0] != 1
                    or connection.in_transaction
                ):
                    return ProfileCorrectionAuthorityResult("schema_unavailable")
                connection.execute("BEGIN")
                try:
                    now = _trusted_utc(self._clock())
                    try:
                        session = validate_session_csrf(
                            connection,
                            session_token=session_token,
                            csrf_secret=csrf_secret,
                            now=now,
                        )
                    except SessionUnavailable:
                        return ProfileCorrectionAuthorityResult("csrf_denied")
                    if method == "POST":
                        expected_proof = profile_correction_action_csrf_proof(
                            csrf_secret,
                            action,
                        )
                        if not hmac.compare_digest(action_proof, expected_proof):
                            return ProfileCorrectionAuthorityResult("csrf_denied")
                    actor = self._authentication_gateway.authenticate_browser_request(
                        connection,
                        ProfileCorrectionRequestContext(method, authentication_input),
                        now=now,
                    )
                    if actor is None:
                        return ProfileCorrectionAuthorityResult("authentication_required")
                    account_reference = actor.account_reference_for_authorization()
                    if (
                        type(account_reference) is not tuple
                        or len(account_reference) != 2
                        or account_reference[0] != session.user_id
                    ):
                        return ProfileCorrectionAuthorityResult("unavailable")
                    decision = self._authorization_gateway.authorize_persistent_profile_read(
                        connection,
                        actor,
                    )
                    if type(decision) is not PersistentProfileReadAuthorizationDecision:
                        return ProfileCorrectionAuthorityResult("unavailable")
                    if decision.state == "denied":
                        return ProfileCorrectionAuthorityResult("authorization_denied")
                    if decision.state != "authorized":
                        return ProfileCorrectionAuthorityResult("unavailable")
                    principal = decision.grant_for_application().principal_for_repository()
                    try:
                        summary = read_current_profile(
                            connection,
                            principal,
                            include_structured_profile=True,
                        )
                    except PersistentProfileDomainError as exc:
                        reason = exc.reason_code
                        exc = None
                        if reason == "profile_not_found":
                            return ProfileCorrectionAuthorityResult("empty")
                        if reason == "schema_capability_unavailable":
                            return ProfileCorrectionAuthorityResult("schema_unavailable")
                        return ProfileCorrectionAuthorityResult("unavailable")
                    if summary.lifecycle_status != "active":
                        return ProfileCorrectionAuthorityResult("profile_unavailable")
                    trusted = summary.trusted_dict(include_structured_profile=True)
                    profile_v2 = trusted.get("structured_profile")
                    if (
                        trusted.get("structured_profile_included") is not True
                        or type(profile_v2) is not dict
                    ):
                        return ProfileCorrectionAuthorityResult("unavailable")
                    profile = TrustedPersistentProfileReference(
                        summary.profile_id,
                        principal.principal_id,
                        principal.environment_namespace,
                    )
                    grant = TrustedProfileCorrectionGrant._issue(
                        _CORRECTION_GRANT_ISSUANCE,
                        account_id=account_reference[0],
                        session_id=session.session_id,
                        environment_namespace=account_reference[1],
                        principal=principal,
                        profile=profile,
                        base_revision_id=summary.revision_id,
                        base_revision_number=summary.revision_number,
                        base_profile_v2=profile_v2,
                    )
                    return ProfileCorrectionAuthorityResult("authorized", grant)
                finally:
                    if connection.in_transaction:
                        connection.rollback()
        except BrowserSessionAuthenticationUnavailable:
            return ProfileCorrectionAuthorityResult("unavailable")
        except (CanonicalProfileV2Error, sqlite3.Error, ValueError, TypeError):
            return ProfileCorrectionAuthorityResult("unavailable")
        except Exception:
            return ProfileCorrectionAuthorityResult("unavailable")

    def draft_binding(self, grant):
        if type(grant) is not TrustedProfileCorrectionGrant:
            raise _configuration_error()
        return grant.draft_binding(self._binding_secret)

    def prepare_review_draft(self, grant):
        if type(grant) is not TrustedProfileCorrectionGrant:
            raise _configuration_error()
        draft, raw_about_you = _server_review_material(grant)
        ConfirmedAboutYouTextSourceDraft(raw_about_you, _trusted_utc(self._clock()))
        return draft, raw_about_you

    def prepare_initial_review(self, grant):
        """Seal the complete server projection used by a new correction run."""
        if self._closed or type(grant) is not TrustedProfileCorrectionGrant:
            raise RuntimeError("profile_correction_review_unavailable")
        try:
            reviewed_profile, _raw_about_you = _server_review_material(grant)
            return _issue_prepared_review(
                grant,
                reviewed_profile=reviewed_profile,
                corrected_profile_v2=grant.trusted_base_profile_v2(),
                normalized_updates=_complete_updates_for_review(reviewed_profile),
                binding_secret=self._binding_secret,
            )
        except (
            CanonicalProfileV2Error,
            PersistentProfileDomainError,
            TypeError,
            ValueError,
            KeyError,
            UnicodeError,
        ):
            raise RuntimeError("profile_correction_review_unavailable") from None

    def prepare_reviewed_correction(
        self,
        *,
        grant,
        preparation,
        reviewed_profile,
        normalized_updates,
    ):
        """Validate one local review result and seal its complete V2 projection."""
        if (
            self._closed
            or type(grant) is not TrustedProfileCorrectionGrant
            or type(preparation) is not PreparedProfileCorrectionReview
            or type(reviewed_profile) is not IdentityFreeCanonicalProfileV1
            or type(normalized_updates) is not dict
            or set(normalized_updates) != _CORRECTION_UPDATE_FIELDS
        ):
            raise RuntimeError("profile_correction_review_unavailable")
        try:
            current_review, current_v2, _current_updates = _prepared_review_material(
                preparation,
                grant=grant,
                binding_secret=self._binding_secret,
            )
            (
                expected_reviewed,
                _durable_reviewed,
                intermediate_v2,
                _complete_updates,
            ) = _server_authoritative_review_correction(
                current_v2,
                current_review,
                normalized_updates,
            )
            if _review_semantics_bytes(reviewed_profile) != _review_semantics_bytes(
                expected_reviewed
            ):
                raise ValueError("untrusted_profile_correction_draft")

            intermediate_review = IdentityFreeCanonicalProfileV1.from_mapping(
                project_v2_to_review_v1(intermediate_v2)
            )
            base_draft, _raw_about_you = _server_review_material(grant)
            (
                _expected_final_review,
                _durable_final_review,
                corrected_v2,
                _durable_final_updates,
            ) = _server_authoritative_review_correction(
                grant.trusted_base_profile_v2(),
                base_draft,
                _complete_updates_for_review(
                    intermediate_review,
                    rendered_defaults=True,
                ),
                complete_languages=True,
                trusted_complete_profile_v2=intermediate_v2,
            )
            authoritative_review = IdentityFreeCanonicalProfileV1.from_mapping(
                project_v2_to_review_v1(corrected_v2)
            )
            return _issue_prepared_review(
                grant,
                reviewed_profile=authoritative_review,
                corrected_profile_v2=corrected_v2,
                normalized_updates=_complete_updates_for_review(
                    authoritative_review
                ),
                binding_secret=self._binding_secret,
            )
        except (
            CanonicalProfileV2Error,
            PersistentProfileDomainError,
            TypeError,
            ValueError,
            KeyError,
            UnicodeError,
        ):
            raise RuntimeError("profile_correction_review_unavailable") from None

    def prepare_confirmation_form_fields(
        self,
        *,
        grant,
        preparation,
        reviewed_profile,
        draft_reference,
        review_token,
    ):
        """Return exactly the defaults a correction browser form would submit."""
        if (
            self._closed
            or type(grant) is not TrustedProfileCorrectionGrant
            or type(preparation) is not PreparedProfileCorrectionReview
            or type(reviewed_profile) is not IdentityFreeCanonicalProfileV1
            or type(draft_reference) is not str
            or _CORRECTION_DRAFT_REFERENCE.fullmatch(draft_reference) is None
            or type(review_token) is not str
            or _OPAQUE_REFERENCE.fullmatch(review_token) is None
        ):
            raise RuntimeError("profile_correction_confirmation_unavailable")
        try:
            authoritative_review, _corrected_v2, _updates = (
                _prepared_review_material(
                    preparation,
                    grant=grant,
                    binding_secret=self._binding_secret,
                )
            )
            if not hmac.compare_digest(
                authoritative_review.canonical_bytes,
                reviewed_profile.canonical_bytes,
            ):
                raise ValueError("untrusted_profile_correction_draft")
            return _server_rendered_review_form_fields(
                authoritative_review,
                draft_reference,
                review_token,
            )
        except (
            CanonicalProfileV2Error,
            PersistentProfileDomainError,
            TypeError,
            ValueError,
            KeyError,
            UnicodeError,
        ):
            raise RuntimeError(
                "profile_correction_confirmation_unavailable"
            ) from None

    def issue_confirmed_artifact(
        self,
        *,
        grant,
        csrf_secret,
        reviewed_profile,
        raw_about_you,
        normalized_updates,
        profile_confirmed,
        authentication_input,
        _confirmation_identity=None,
        _confirmation_witness=None,
        _confirmation_recovery_only=False,
        _prepared_review=None,
    ):
        del authentication_input
        confirmation_enabled = _confirmation_identity is not None
        if (
            self._closed
            or type(grant) is not TrustedProfileCorrectionGrant
            or type(reviewed_profile) is not IdentityFreeCanonicalProfileV1
            or type(raw_about_you) is not str
            or type(normalized_updates) is not dict
            or set(normalized_updates) != _CORRECTION_UPDATE_FIELDS
            or profile_confirmed is not True
            or type(csrf_secret) is not str
            or _OPAQUE_REFERENCE.fullmatch(csrf_secret) is None
            or type(_confirmation_recovery_only) is not bool
            or (
                _prepared_review is not None
                and type(_prepared_review) is not PreparedProfileCorrectionReview
            )
            or (
                confirmation_enabled
                and (
                    _CONFIRMATION_IDENTITY.fullmatch(_confirmation_identity or "") is None
                    or not all(
                        callable(getattr(_confirmation_witness, name, None))
                        for name in (
                            "mark_artifact_may_exist",
                            "mark_artifact_definitely_absent",
                            "record_authority_binding",
                            "record_valid_offer",
                        )
                    )
                )
            )
            or (
                not confirmation_enabled
                and (_confirmation_witness is not None or _confirmation_recovery_only)
            )
        ):
            raise RuntimeError("profile_correction_confirmation_unavailable")
        try:
            base_draft, expected_raw_about_you = _server_review_material(grant)
            if _prepared_review is None:
                (
                    expected_reviewed,
                    durable_reviewed,
                    corrected_v2,
                    durable_updates,
                ) = _server_authoritative_review_correction(
                    grant.trusted_base_profile_v2(),
                    base_draft,
                    normalized_updates,
                )
            else:
                durable_reviewed, corrected_v2, durable_updates = (
                    _prepared_review_material(
                        _prepared_review,
                        grant=grant,
                        binding_secret=self._binding_secret,
                    )
                )
                expected_updates = _server_visible_review_updates(
                    durable_reviewed
                )
                expected_reviewed = _apply_review_updates(
                    durable_reviewed,
                    expected_updates,
                )
                if _canonical_json_bytes(normalized_updates) != _canonical_json_bytes(
                    expected_updates
                ):
                    raise ValueError("untrusted_profile_correction_draft")
            if (
                raw_about_you != expected_raw_about_you
                or _review_semantics_bytes(reviewed_profile)
                != _review_semantics_bytes(expected_reviewed)
            ):
                raise ValueError("untrusted_profile_correction_draft")
            reviewed_profile = durable_reviewed
        except (
            ValueError,
            TypeError,
            KeyError,
            UnicodeError,
            CanonicalProfileV2Error,
            PersistentProfileDomainError,
        ):
            raise RuntimeError(
                "profile_correction_confirmation_unavailable"
            ) from None
        binding = grant.confirmation_binding()
        if confirmation_enabled:
            _confirmation_witness.record_authority_binding(binding)
        reference = None
        try:
            if _confirmation_recovery_only:
                state, reference = self._vault.recover_confirmation(
                    _confirmation_identity,
                    binding,
                )
                if state == "absent":
                    _confirmation_witness.mark_artifact_definitely_absent()
                    raise RuntimeError("profile_correction_confirmation_unavailable")
                if state != "found" or reference is None:
                    _confirmation_witness.mark_artifact_may_exist()
                    raise RuntimeError("profile_correction_confirmation_unavailable")
                _confirmation_witness.mark_artifact_may_exist()
                offer = ConfirmedProfileCorrectionArtifactOffer(
                    reference,
                    profile_correction_csrf_proof(csrf_secret, reference),
                )
                _confirmation_witness.record_valid_offer(offer)
                return offer
            reference = self._new_reference()
            snapshot = self._prepare_snapshot(
                grant,
                reference=reference,
                reviewed_profile=reviewed_profile,
                corrected_profile_v2=corrected_v2,
                raw_about_you=raw_about_you,
                normalized_updates=durable_updates,
            )
            self._vault.issue(
                reference,
                snapshot,
                confirmation_identity=_confirmation_identity,
            )
            if confirmation_enabled:
                _confirmation_witness.mark_artifact_may_exist()
            offer = ConfirmedProfileCorrectionArtifactOffer(
                reference,
                profile_correction_csrf_proof(csrf_secret, reference),
            )
            if confirmation_enabled:
                _confirmation_witness.record_valid_offer(offer)
            return offer
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception as exc:
            exc = None
            if confirmation_enabled and reference is not None:
                try:
                    _confirmation_witness.mark_artifact_may_exist()
                except Exception:
                    pass
            raise RuntimeError("profile_correction_confirmation_unavailable") from None

    @staticmethod
    def authenticate_completed_confirmation(*, grant, authority_binding):
        return (
            type(grant) is TrustedProfileCorrectionGrant
            and type(authority_binding) is tuple
            and len(authority_binding) == 10
            and grant.confirmation_binding() == authority_binding
        )

    def consume(self, *, grant, csrf_secret, artifact_reference, csrf_proof):
        if (
            self._closed
            or type(grant) is not TrustedProfileCorrectionGrant
            or type(csrf_secret) is not str
            or _OPAQUE_REFERENCE.fullmatch(csrf_secret) is None
            or type(artifact_reference) is not str
            or _OPAQUE_REFERENCE.fullmatch(artifact_reference) is None
            or type(csrf_proof) is not str
            or _OPAQUE_REFERENCE.fullmatch(csrf_proof) is None
            or not hmac.compare_digest(
                csrf_proof,
                profile_correction_csrf_proof(csrf_secret, artifact_reference),
            )
        ):
            return ProfileCorrectionOutcome("gone")
        consume_token = self._vault._begin_consume()
        if consume_token is None:
            return ProfileCorrectionOutcome("unavailable")
        try:
            outcome = self._vault.claim(
                consume_token,
                artifact_reference,
                grant,
                lambda snapshot: self._append_snapshot(snapshot, grant),
            )
            if outcome.state != "gone":
                return outcome
            return self._replay_completed_artifact(grant, artifact_reference)
        finally:
            self._vault._finish_consume(consume_token)

    def _prepare_snapshot(
        self,
        grant,
        *,
        reference,
        reviewed_profile,
        corrected_profile_v2,
        raw_about_you,
        normalized_updates,
    ):
        from wahojobs.persistent_profile_creation import (
            PROFILE_CREATE_NORMALIZER_VERSION,
            PROFILE_CREATE_REVIEWER_VERSION,
            prepare_reviewed_profile_source_bundle,
        )

        accepted_at = _trusted_utc(self._clock())
        bundle = prepare_reviewed_profile_source_bundle(
            reviewed_profile=reviewed_profile,
            raw_about_you=raw_about_you,
            normalized_updates=normalized_updates,
            confirmed_at=accepted_at,
            require_correction=True,
            partition_correction_sources=True,
        )
        profile = grant.profile_for_repository()
        correction_source_ordinals = tuple(range(2, len(bundle.sources) + 1))
        canonical_v2 = _bind_correction_source_ordinals(
            corrected_profile_v2,
            correction_source_ordinals,
        )
        if canonical_v2["identity"]["profile_id"] != profile.profile_id:
            raise CanonicalProfileV2Error("persistent_identity_mismatch")
        idempotency_key = _correction_idempotency_key(grant, reference)
        command = AppendProfileRevisionCommand.prepare(
            principal=grant.principal_for_repository(),
            profile=profile,
            expected_current_revision_number=grant.base_revision_number,
            revision_kind="correction",
            canonical_profile_v2=canonical_v2,
            sources=bundle.sources,
            correction_of_revision_id=grant.base_revision_id,
            normalizer_version=PROFILE_CREATE_NORMALIZER_VERSION,
            reviewer_version=PROFILE_CREATE_REVIEWER_VERSION,
            actor_type=PROFILE_CORRECTION_ACTOR_TYPE,
            reason_code=PROFILE_CORRECTION_REASON_CODE,
            idempotency_key=idempotency_key,
            accepted_at=accepted_at,
        )
        binding = grant.confirmation_binding()
        values = {
            "artifact_reference": reference,
            "command": command,
            "account_id": binding[0],
            "session_id": binding[1],
            "environment_namespace": binding[2],
            "principal_id": binding[3],
            "profile_id": binding[4],
            "base_revision_id": binding[5],
            "base_revision_number": binding[6],
            "base_profile_json": canonical_profile_v2_json_bytes(
                grant.trusted_base_profile_v2()
            ),
            "purpose": PROFILE_CORRECTION_PURPOSE,
        }
        fingerprint = _snapshot_fingerprint(**values)
        return _ConfirmedCorrectionSnapshot(
            **values,
            content_fingerprint=fingerprint,
        )

    def _append_snapshot(self, snapshot, grant):
        if (
            type(snapshot) is not _ConfirmedCorrectionSnapshot
            or not _snapshot_integrity_valid(snapshot)
            or snapshot.actor_profile_binding() != grant.actor_profile_binding()
            or snapshot.command.profile != grant.profile_for_repository()
            or snapshot.command.principal != grant.principal_for_repository()
            or snapshot.command.expected_current_revision_number
            != snapshot.base_revision_number
            or snapshot.command.correction_of_revision_id != snapshot.base_revision_id
        ):
            return ProfileCorrectionOutcome("unavailable")
        try:
            with self._write_connection_provider() as connection:
                if not isinstance(connection, sqlite3.Connection):
                    return ProfileCorrectionOutcome("unavailable")
                result = self._append_revision(connection, snapshot.command)
            if (
                getattr(result, "revision_kind", None) != "correction"
                or getattr(result, "profile_id", None) != snapshot.profile_id
                or type(getattr(result, "replayed", None)) is not bool
            ):
                return ProfileCorrectionOutcome("unavailable")
            return ProfileCorrectionOutcome(
                "corrected",
                replayed=result.replayed,
            )
        except PersistentProfileRepositoryDefiniteRollback as exc:
            reason = exc.reason_code
            exc = None
            return ProfileCorrectionOutcome(
                "temporary_contention" if reason == "temporary_contention" else "unavailable"
            )
        except PersistentProfileRepositoryOutcomeUncertain as exc:
            exc = None
            return ProfileCorrectionOutcome("unavailable")
        except PersistentProfileDomainError as exc:
            reason = exc.reason_code
            exc = None
            if reason == "stale_revision":
                return ProfileCorrectionOutcome("stale")
            if reason == "idempotency_conflict":
                return ProfileCorrectionOutcome("conflict")
            if reason == "temporary_contention":
                return ProfileCorrectionOutcome("temporary_contention")
            return ProfileCorrectionOutcome("unavailable")
        except sqlite3.Error:
            return ProfileCorrectionOutcome("unavailable")

    def _replay_completed_artifact(self, grant, reference):
        idempotency_key = _correction_idempotency_key(grant, reference)
        try:
            with self._write_connection_provider() as connection:
                if not isinstance(connection, sqlite3.Connection):
                    return ProfileCorrectionOutcome("unavailable")
                command = _rebuild_completed_command(
                    connection,
                    grant,
                    idempotency_key=idempotency_key,
                )
                if command is None:
                    return ProfileCorrectionOutcome("gone")
                result = self._append_revision(connection, command)
            if (
                getattr(result, "revision_kind", None) != "correction"
                or getattr(result, "profile_id", None)
                != grant.profile_for_repository().profile_id
                or getattr(result, "replayed", None) is not True
            ):
                return ProfileCorrectionOutcome("unavailable")
            return ProfileCorrectionOutcome("corrected", replayed=True)
        except PersistentProfileDomainError as exc:
            reason = exc.reason_code
            exc = None
            if reason == "idempotency_conflict":
                return ProfileCorrectionOutcome("conflict")
            if reason == "temporary_contention":
                return ProfileCorrectionOutcome("temporary_contention")
            return ProfileCorrectionOutcome("unavailable")
        except (sqlite3.Error, ValueError, TypeError, CanonicalProfileV2Error):
            return ProfileCorrectionOutcome("unavailable")

    def _new_reference(self):
        for _attempt in range(16):
            candidate = self._token_factory()
            if type(candidate) is str and _OPAQUE_REFERENCE.fullmatch(candidate) is not None:
                return candidate
        raise RuntimeError("profile_correction_confirmation_unavailable")

    def close(self):
        self._closed = True
        return self._vault.close()

    @property
    def closed(self):
        return self._closed and self._vault.closed


def _bind_correction_source_ordinals(profile_v2, correction_source_ordinals):
    """Bind server-built correction evidence rows to changed V2 fields."""
    if (
        type(correction_source_ordinals) is not tuple
        or not 1 <= len(correction_source_ordinals) < MAX_SOURCES
        or correction_source_ordinals
        != tuple(range(2, len(correction_source_ordinals) + 2))
    ):
        raise _configuration_error()
    corrected = validate_canonical_profile_v2(profile_v2)
    for source in corrected["provenance"]["field_sources"]:
        if source["source_kind"] == PROFILE_SOURCE_USER_CORRECTION:
            source["source_ordinals"] = list(correction_source_ordinals)
    return validate_canonical_profile_v2(corrected)


def _server_review_material(grant):
    if type(grant) is not TrustedProfileCorrectionGrant:
        raise _configuration_error()
    mapping = project_v2_to_review_v1(grant.trusted_base_profile_v2())
    draft = IdentityFreeCanonicalProfileV1.from_mapping(mapping)
    matcher = mapping.get("matcher_compatible_profile") or {}
    summary = str(
        matcher.get("summary") or mapping["identity"]["display_name"]
    ).strip()
    raw_about_you = "Current saved profile reviewed for correction."
    if summary:
        raw_about_you += "\n" + summary
    return draft, raw_about_you


def _issue_prepared_review(
    grant,
    *,
    reviewed_profile,
    corrected_profile_v2,
    normalized_updates,
    binding_secret,
):
    if (
        type(grant) is not TrustedProfileCorrectionGrant
        or type(reviewed_profile) is not IdentityFreeCanonicalProfileV1
        or type(normalized_updates) is not dict
        or set(normalized_updates) != _CORRECTION_UPDATE_FIELDS
        or type(binding_secret) is not bytes
        or len(binding_secret) < 32
    ):
        raise _configuration_error()
    corrected_v2 = validate_canonical_profile_v2(corrected_profile_v2)
    if (
        corrected_v2["identity"]["profile_id"]
        != grant.profile_for_repository().profile_id
    ):
        raise _configuration_error()
    projected = IdentityFreeCanonicalProfileV1.from_mapping(
        project_v2_to_review_v1(corrected_v2)
    )
    if not hmac.compare_digest(
        projected.canonical_bytes,
        reviewed_profile.canonical_bytes,
    ):
        raise _configuration_error()
    binding = grant.confirmation_binding()
    reviewed_json = reviewed_profile.canonical_bytes
    corrected_v2_json = canonical_profile_v2_json_bytes(corrected_v2)
    normalized_updates_json = _canonical_json_bytes(normalized_updates)
    proof = hmac.digest(
        binding_secret,
        _prepared_review_proof_payload(
            binding,
            reviewed_json,
            corrected_v2_json,
            normalized_updates_json,
        ),
        "sha256",
    )
    instance = object.__new__(PreparedProfileCorrectionReview)
    for name, value in {
        "_authority_binding": binding,
        "_reviewed_profile_json": reviewed_json,
        "_corrected_profile_v2_json": corrected_v2_json,
        "_normalized_updates_json": normalized_updates_json,
        "_proof": proof,
    }.items():
        object.__setattr__(instance, name, value)
    return instance


def _prepared_review_material(preparation, *, grant, binding_secret):
    if (
        type(preparation) is not PreparedProfileCorrectionReview
        or type(grant) is not TrustedProfileCorrectionGrant
        or type(binding_secret) is not bytes
        or len(binding_secret) < 32
        or type(preparation._authority_binding) is not tuple
        or type(preparation._reviewed_profile_json) is not bytes
        or type(preparation._corrected_profile_v2_json) is not bytes
        or type(preparation._normalized_updates_json) is not bytes
        or type(preparation._proof) is not bytes
        or len(preparation._proof) != 32
        or preparation._authority_binding != grant.confirmation_binding()
    ):
        raise _configuration_error()
    expected_proof = hmac.digest(
        binding_secret,
        _prepared_review_proof_payload(
            preparation._authority_binding,
            preparation._reviewed_profile_json,
            preparation._corrected_profile_v2_json,
            preparation._normalized_updates_json,
        ),
        "sha256",
    )
    if not hmac.compare_digest(preparation._proof, expected_proof):
        raise _configuration_error()
    reviewed_profile = IdentityFreeCanonicalProfileV1.from_json_bytes(
        preparation._reviewed_profile_json
    )
    corrected_v2 = parse_canonical_profile_v2_json(
        preparation._corrected_profile_v2_json
    )
    try:
        normalized_updates = json.loads(
            preparation._normalized_updates_json.decode("utf-8")
        )
    except (UnicodeError, json.JSONDecodeError):
        raise _configuration_error() from None
    if (
        type(normalized_updates) is not dict
        or set(normalized_updates) != _CORRECTION_UPDATE_FIELDS
        or corrected_v2["identity"]["profile_id"]
        != grant.profile_for_repository().profile_id
    ):
        raise _configuration_error()
    projected = IdentityFreeCanonicalProfileV1.from_mapping(
        project_v2_to_review_v1(corrected_v2)
    )
    if not hmac.compare_digest(
        projected.canonical_bytes,
        reviewed_profile.canonical_bytes,
    ):
        raise _configuration_error()
    return reviewed_profile, corrected_v2, normalized_updates


def _prepared_review_proof_payload(
    authority_binding,
    reviewed_profile_json,
    corrected_profile_v2_json,
    normalized_updates_json,
):
    return _canonical_json_bytes(
        {
            "authority_binding": list(authority_binding),
            "corrected_profile_v2_sha256": hashlib.sha256(
                corrected_profile_v2_json
            ).hexdigest(),
            "normalized_updates_sha256": hashlib.sha256(
                normalized_updates_json
            ).hexdigest(),
            "reviewed_profile_sha256": hashlib.sha256(
                reviewed_profile_json
            ).hexdigest(),
            "version": 1,
        }
    )


def _apply_review_updates(reviewed_profile, normalized_updates):
    from scripts.local_product_app import apply_identity_free_profile_review

    return apply_identity_free_profile_review(
        reviewed_profile,
        normalized_updates,
    )


def _server_rendered_review_form_fields(
    reviewed_profile,
    draft_reference,
    review_token,
):
    if type(reviewed_profile) is not IdentityFreeCanonicalProfileV1:
        raise _configuration_error()
    from scripts import local_product_app as review_support

    fields = review_support.profile_review_form_fields(
        reviewed_profile,
        draft_reference,
        review_token,
    )
    # Canonical V1/V2 still accept a small legacy education vocabulary that
    # the current HTML select cannot render.  A real browser submits the first
    # rendered option when none is selected, so confirmation must do the same.
    if fields["education_level"] not in review_support.EDUCATION_LEVELS:
        fields["education_level"] = review_support.EDUCATION_LEVELS[0]
    return fields


def _server_visible_review_updates(reviewed_profile):
    from scripts import local_product_app as review_support

    fields = _server_rendered_review_form_fields(
        reviewed_profile,
        "server_profile_correction",
        "s" * 43,
    )
    return review_support.profile_review_updates_from_form(
        {name: [value] for name, value in fields.items()},
        review_support.profile_review_language_slots(reviewed_profile),
    )


def _complete_updates_for_review(reviewed_profile, *, rendered_defaults=False):
    if (
        type(reviewed_profile) is not IdentityFreeCanonicalProfileV1
        or type(rendered_defaults) is not bool
    ):
        raise _configuration_error()
    from scripts import local_product_app as review_support

    if rendered_defaults:
        fields = _server_rendered_review_form_fields(
            reviewed_profile,
            "server_profile_correction",
            "s" * 43,
        )
    else:
        fields = review_support.profile_review_form_fields(
            reviewed_profile,
            "server_profile_correction",
            "s" * 43,
        )
    slots = review_support.profile_review_language_slots(reviewed_profile)
    updates = review_support.profile_review_updates_from_form(
        {name: [value] for name, value in fields.items()},
        slots,
    )
    authoritative = reviewed_profile.to_mapping()
    if not rendered_defaults:
        updates["education_level"] = authoritative["education"][
            "education_level"
        ]
        updates["languages"] = [
            {
                "language": item["language"],
                "proficiency": item.get("proficiency") or "unspecified",
                "locale": item.get("locale") or "",
            }
            for item in authoritative["languages"]
        ]
    else:
        updates["languages"] = list(updates["languages"]) + [
            {
                "language": item["language"],
                "proficiency": _rendered_language_proficiency(
                    item.get("proficiency"),
                    review_support.LANGUAGE_PROFICIENCIES,
                ),
                "locale": item.get("locale") or "",
            }
            for item in authoritative["languages"][slots:]
        ]
    if set(updates) != _CORRECTION_UPDATE_FIELDS:
        raise _configuration_error()
    return updates


def _rendered_language_proficiency(value, supported):
    value = value or "unspecified"
    if value in supported:
        return value
    return "professional" if value == "advanced" else "unspecified"


def _server_authoritative_review_correction(
    base_profile_v2,
    base_draft,
    normalized_updates,
    *,
    complete_languages=False,
    trusted_complete_profile_v2=None,
):
    """Validate browser review semantics and preserve the complete trusted V2."""
    if (
        type(base_draft) is not IdentityFreeCanonicalProfileV1
        or type(normalized_updates) is not dict
        or set(normalized_updates) != _CORRECTION_UPDATE_FIELDS
        or type(complete_languages) is not bool
        or (
            trusted_complete_profile_v2 is not None
            and not complete_languages
        )
    ):
        raise _configuration_error()
    from scripts import local_product_app as review_support

    base_profile_v2 = validate_canonical_profile_v2(base_profile_v2)
    slots = review_support.profile_review_language_slots(base_draft)
    baseline_updates = _server_default_review_updates(base_draft)
    expected_browser_review = _apply_review_updates(
        base_draft,
        normalized_updates,
    )

    base_languages = base_draft.to_mapping()["languages"]
    trusted_base_tail = [
        {
            "language": item["language"],
            "proficiency": _rendered_language_proficiency(
                item.get("proficiency"),
                review_support.LANGUAGE_PROFICIENCIES,
            ),
            "locale": item.get("locale") or "",
        }
        for item in base_languages[slots:]
    ]
    complete_baseline_updates = deepcopy(baseline_updates)
    complete_baseline_updates["languages"] = (
        list(complete_baseline_updates["languages"])
        + deepcopy(trusted_base_tail)
    )
    complete_corrected_updates = deepcopy(normalized_updates)
    complete_corrected_updates["languages"] = (
        list(complete_corrected_updates["languages"])
        + ([] if complete_languages else deepcopy(trusted_base_tail))
    )
    baseline_review = _apply_review_updates(
        base_draft,
        complete_baseline_updates,
    )
    durable_review = _apply_review_updates(
        base_draft,
        complete_corrected_updates,
    )
    corrected_v2 = merge_server_review_correction_v2(
        base_profile_v2,
        baseline_review.to_mapping(),
        durable_review.to_mapping(),
    )
    if trusted_complete_profile_v2 is None:
        corrected_v2 = _restore_trusted_language_tail(
            corrected_v2,
            base_profile_v2,
            slots,
        )
    else:
        trusted_complete = validate_canonical_profile_v2(
            trusted_complete_profile_v2
        )
        if (
            trusted_complete["identity"]["profile_id"]
            != corrected_v2["identity"]["profile_id"]
        ):
            raise _configuration_error()
        corrected_v2["languages"] = deepcopy(trusted_complete["languages"])
        corrected_v2 = validate_canonical_profile_v2(corrected_v2)
    authoritative_review = IdentityFreeCanonicalProfileV1.from_mapping(
        project_v2_to_review_v1(corrected_v2)
    )
    durable_updates = deepcopy(normalized_updates)
    if not complete_languages:
        durable_updates["languages"] = list(durable_updates["languages"]) + [
            {
                "language": item["language"],
                "proficiency": item.get("proficiency") or "unspecified",
                "locale": item.get("locale") or "",
            }
            for item in base_profile_v2["languages"][
                min(slots, len(base_profile_v2["languages"])) :
            ]
        ]
    return (
        expected_browser_review,
        authoritative_review,
        corrected_v2,
        durable_updates,
    )


def _restore_trusted_language_tail(corrected_v2, base_profile_v2, slots):
    """Restore exact non-rendered language records selected by the server."""
    corrected = validate_canonical_profile_v2(corrected_v2)
    base = validate_canonical_profile_v2(base_profile_v2)
    if type(slots) is not int or slots < 0:
        raise _configuration_error()
    trusted_tail_start = min(slots, len(base["languages"]))
    indexes = {
        item["language"]: index
        for index, item in enumerate(corrected["languages"])
    }
    for trusted in base["languages"][trusted_tail_start:]:
        index = indexes.get(trusted["language"])
        if index is not None:
            corrected["languages"][index] = deepcopy(trusted)
    return validate_canonical_profile_v2(corrected)


def _server_default_review_updates(base_draft):
    """Return the normalized submission produced by an unchanged review form."""
    if type(base_draft) is not IdentityFreeCanonicalProfileV1:
        raise _configuration_error()
    from scripts import local_product_app as review_support

    fields = _server_rendered_review_form_fields(
        base_draft,
        "server_profile_correction",
        "s" * 43,
    )
    return review_support.profile_review_updates_from_form(
        {name: [value] for name, value in fields.items()},
        review_support.profile_review_language_slots(base_draft),
    )


def _review_semantics_bytes(profile):
    if type(profile) is not IdentityFreeCanonicalProfileV1:
        raise _configuration_error()
    mapping = profile.to_mapping()
    mapping.pop("provenance", None)
    return _canonical_json_bytes(mapping)


def profile_correction_action_csrf_proof(csrf_secret, action):
    if (
        type(csrf_secret) is not str
        or _OPAQUE_REFERENCE.fullmatch(csrf_secret) is None
        or action not in CORRECTION_ACTIONS
    ):
        raise _configuration_error()
    digest = hmac.digest(
        csrf_secret.encode("ascii"),
        PROFILE_CORRECTION_ACTION_CSRF_MESSAGE_PREFIX + action.encode("ascii"),
        "sha256",
    )
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def profile_correction_csrf_proof(csrf_secret, artifact_reference):
    if (
        type(csrf_secret) is not str
        or _OPAQUE_REFERENCE.fullmatch(csrf_secret) is None
        or type(artifact_reference) is not str
        or _OPAQUE_REFERENCE.fullmatch(artifact_reference) is None
    ):
        raise _configuration_error()
    digest = hmac.digest(
        csrf_secret.encode("ascii"),
        PROFILE_CORRECTION_CSRF_MESSAGE_PREFIX + artifact_reference.encode("ascii"),
        "sha256",
    )
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _correction_idempotency_key(grant, artifact_reference):
    if (
        type(grant) is not TrustedProfileCorrectionGrant
        or type(artifact_reference) is not str
        or _OPAQUE_REFERENCE.fullmatch(artifact_reference) is None
    ):
        raise _configuration_error()
    account, session, environment, principal, profile, purpose = (
        grant.actor_profile_binding()
    )
    digest = hashlib.sha256(
        _canonical_json_bytes(
            {
                "account_id": account,
                "artifact_reference": artifact_reference,
                "environment_namespace": environment,
                "principal_id": principal,
                "profile_id": profile,
                "purpose": purpose,
                "session_id": session,
            }
        )
    ).hexdigest()
    return "profile-correction:" + digest


def _rebuild_completed_command(connection, grant, *, idempotency_key):
    principal = grant.principal_for_repository()
    profile = grant.profile_for_repository()
    rows = connection.execute(
        "SELECT revision_id, profile_id, principal_id, environment_namespace, "
        "revision_number, previous_revision_id, correction_of_revision_id, "
        "revision_kind, lifecycle_status, canonical_schema_version, "
        "structured_profile_json, source_count, normalizer_version, reviewer_version, "
        "actor_type, reason_code, request_fingerprint, created_at "
        "FROM product_profile_revisions WHERE principal_id=? AND idempotency_key=? "
        "ORDER BY revision_id LIMIT 2",
        (principal.principal_id, idempotency_key),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise PersistentProfileDomainError("internal_consistency_failure")
    row = rows[0]
    if (
        row[1] != profile.profile_id
        or row[2] != principal.principal_id
        or row[3] != principal.environment_namespace
        or type(row[4]) is not int
        or row[4] < 2
        or row[5] != row[6]
        or row[7] != "correction"
        or row[8] != "active"
        or row[9] != "canonical_profile_v2"
        or type(row[11]) is not int
        or row[11] < 1
        or row[14] != PROFILE_CORRECTION_ACTOR_TYPE
        or row[15] != PROFILE_CORRECTION_REASON_CODE
        or _SHA256.fullmatch(row[16] or "") is None
    ):
        raise PersistentProfileDomainError("internal_consistency_failure")
    base = connection.execute(
        "SELECT revision_number FROM product_profile_revisions "
        "WHERE revision_id=? AND profile_id=?",
        (row[6], row[1]),
    ).fetchone()
    if base is None or base[0] != row[4] - 1:
        raise PersistentProfileDomainError("internal_consistency_failure")
    source_rows = connection.execute(
        "SELECT source_ordinal, source_type, source_format, source_content, "
        "source_schema_version, parser_version, accepted_at "
        "FROM product_profile_sources WHERE revision_id=? ORDER BY source_ordinal",
        (row[0],),
    ).fetchall()
    if len(source_rows) != row[11] or [item[0] for item in source_rows] != list(
        range(1, len(source_rows) + 1)
    ):
        raise PersistentProfileDomainError("internal_consistency_failure")
    sources = []
    for source in source_rows:
        confirmed_at = _parse_timestamp(source[6])
        if source[1] == "confirmed_about_you_text" and source[2] == "text/plain":
            sources.append(
                ConfirmedAboutYouTextSourceDraft(
                    source[3],
                    confirmed_at,
                    source[4],
                    source[5],
                )
            )
        elif (
            source[1] == "user_confirmed_correction"
            and source[2] == "application/json"
        ):
            sources.append(
                UserConfirmedCorrectionSourceDraft(
                    source[3],
                    confirmed_at,
                    source[4],
                    source[5],
                )
            )
        else:
            raise PersistentProfileDomainError("internal_consistency_failure")
    command = AppendProfileRevisionCommand.prepare(
        principal=principal,
        profile=profile,
        expected_current_revision_number=row[4] - 1,
        revision_kind="correction",
        canonical_profile_v2=parse_canonical_profile_v2_json(row[10]),
        sources=tuple(sources),
        correction_of_revision_id=row[6],
        normalizer_version=row[12],
        reviewer_version=row[13],
        actor_type=row[14],
        reason_code=row[15],
        idempotency_key=idempotency_key,
        accepted_at=_parse_timestamp(row[17]),
    )
    if not hmac.compare_digest(command.request_fingerprint, row[16]):
        raise PersistentProfileDomainError("internal_consistency_failure")
    return command


def _snapshot_fingerprint(
    *,
    artifact_reference,
    command,
    account_id,
    session_id,
    environment_namespace,
    principal_id,
    profile_id,
    base_revision_id,
    base_revision_number,
    base_profile_json,
    purpose,
):
    if type(command) is not AppendProfileRevisionCommand:
        raise _configuration_error()
    return hashlib.sha256(
        _canonical_json_bytes(
            {
                "account_id": account_id,
                "artifact_reference": artifact_reference,
                "base_profile_sha256": hashlib.sha256(base_profile_json).hexdigest(),
                "base_revision_id": base_revision_id,
                "base_revision_number": base_revision_number,
                "command_request_fingerprint": command.request_fingerprint,
                "environment_namespace": environment_namespace,
                "principal_id": principal_id,
                "profile_id": profile_id,
                "purpose": purpose,
                "session_id": session_id,
            }
        )
    ).hexdigest()


def _snapshot_integrity_valid(snapshot):
    if type(snapshot) is not _ConfirmedCorrectionSnapshot:
        return False
    try:
        expected = _snapshot_fingerprint(
            artifact_reference=snapshot.artifact_reference,
            command=snapshot.command,
            account_id=snapshot.account_id,
            session_id=snapshot.session_id,
            environment_namespace=snapshot.environment_namespace,
            principal_id=snapshot.principal_id,
            profile_id=snapshot.profile_id,
            base_revision_id=snapshot.base_revision_id,
            base_revision_number=snapshot.base_revision_number,
            base_profile_json=snapshot.base_profile_json,
            purpose=snapshot.purpose,
        )
        base = parse_canonical_profile_v2_json(snapshot.base_profile_json)
        return (
            snapshot.purpose == PROFILE_CORRECTION_PURPOSE
            and base["identity"]["profile_id"] == snapshot.profile_id
            and snapshot.command.profile.profile_id == snapshot.profile_id
            and snapshot.command.principal.principal_id == snapshot.principal_id
            and snapshot.command.expected_current_revision_number
            == snapshot.base_revision_number
            and snapshot.command.correction_of_revision_id
            == snapshot.base_revision_id
            and hmac.compare_digest(snapshot.content_fingerprint, expected)
        )
    except Exception:
        return False


def _canonical_json_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _trusted_utc(value):
    if type(value) is not datetime or value.tzinfo is None:
        raise _configuration_error()
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _parse_timestamp(value):
    if type(value) is not str or len(value) != 25:
        raise ValueError("invalid_profile_correction_timestamp")
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S+00:00")
    return parsed.replace(tzinfo=timezone.utc)


def _trusted_monotonic(value):
    if (
        type(value) not in {int, float}
        or not math.isfinite(value)
        or value < 0
    ):
        raise _configuration_error()
    return float(value)


__all__ = [
    "CORRECTION_ACTIONS",
    "ConfirmedProfileCorrectionArtifactOffer",
    "ConfirmedProfileCorrectionArtifactVault",
    "PROFILE_CORRECTION_ARTIFACT_CAPACITY",
    "PROFILE_CORRECTION_ARTIFACT_LIFETIME_SECONDS",
    "PROFILE_CORRECTION_PURPOSE",
    "PROFILE_CORRECTION_ROUTE",
    "PersistentProfileCorrectionService",
    "ProfileCorrectionAuthorityResult",
    "ProfileCorrectionOutcome",
    "ProfileCorrectionRequestContext",
    "TrustedProfileCorrectionGrant",
    "profile_correction_action_csrf_proof",
    "profile_correction_csrf_proof",
]
