"""Explicit account-native first-profile creation for the durable browser flow.

The module owns no process startup behavior.  Runtime composition injects the
trusted session, ownership, connection, clock, and randomness authorities.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import re
import sqlite3
import threading
import time

from wahojobs.accounts import PublicSession, SessionUnavailable, validate_session_csrf
from wahojobs.browser_session_authentication import (
    BrowserSessionAuthenticationUnavailable,
    DurableBrowserSessionAuthenticationGateway,
)
from wahojobs.persistent_profile_read_authorization import (
    DurablePersistentProfileReadAuthorizationGateway,
    PersistentProfileReadAuthorizationDecision,
)
from wahojobs.persistent_profiles import (
    ConfirmedAboutYouTextSourceDraft,
    CreatePersistentProfileCommand,
    IdentityFreeCanonicalProfileV1,
    MAX_SOURCE_BYTES,
    MAX_SOURCES,
    PersistentProfileDomainError,
    TrustedPrincipalContext,
    UserConfirmedCorrectionSourceDraft,
    _create_canonical_profile_v2_draft,
)
from wahojobs.persistent_profiles_repository import (
    PersistentProfileRepository,
    PersistentProfileRepositoryDefiniteRollback,
    PersistentProfileRepositoryOutcomeUncertain,
    TrustedProfileCreateLineage,
    capture_profile_create_lineage,
)
from wahojobs.profiles.canonical_v2 import (
    CanonicalProfileV2Error,
    convert_v1_to_v2,
)


PROFILE_CREATE_ROUTE = "/account/profile"
PROFILE_CREATE_PURPOSE = "persistent_profile_create_v1"
PROFILE_CREATE_ARTIFACT_LIFETIME_SECONDS = 600
PROFILE_CREATE_ARTIFACT_CAPACITY = 64
PROFILE_CREATE_CSRF_MESSAGE_PREFIX = b"wahojobs.profile-create.v1\x00"
PROFILE_CREATE_NORMALIZER_VERSION = "baseline_v1"
PROFILE_CREATE_REVIEWER_VERSION = "about_you_review_v1"
PROFILE_CREATE_ACTOR_TYPE = "authenticated_user"
PROFILE_CREATE_REASON_CODE = "profile.create"

_OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_CONFIRMATION_IDENTITY = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_CREATE_STATES = frozenset(
    {
        "created",
        "conflict",
        "gone",
        "authentication_required",
        "csrf_denied",
        "authorization_denied",
        "temporary_contention",
        "unavailable",
    }
)
_CREATE_GRANT_ISSUANCE = object()
_ARTIFACT_CLAIM_ACCESS = object()
_CLAIM_CLEANUP_POLL_SECONDS = 0.05
_CLAIM_CLEANUP_PROBE_JOIN_SECONDS = 0.5
_CLAIM_CLEANUP_REQUEST_WAIT_SECONDS = 2.0
_CLAIM_CLEANUP_CLOSE_JOIN_SECONDS = 2.0
_CORRECTION_PARTITION_VERSION = "user_confirmed_correction_partition_v1"
_MAX_CORRECTION_SOURCE_PARTS = MAX_SOURCES - 1


def _configuration_error() -> ValueError:
    return ValueError("invalid_persistent_profile_creation_configuration")


class ProfileCreateRequestContext:
    """Sealed browser facts accepted only by durable session authentication."""

    __slots__ = ("method", "route", "_authentication_input", "_sealed")

    def __init__(self, authentication_input):
        object.__setattr__(self, "method", "POST")
        object.__setattr__(self, "route", PROFILE_CREATE_ROUTE)
        object.__setattr__(self, "_authentication_input", authentication_input)
        object.__setattr__(self, "_sealed", True)

    def authentication_input_for_gateway(self):
        return self._authentication_input

    def __setattr__(self, _name, _value):
        raise AttributeError("profile_create_request_context_is_immutable")

    def __repr__(self):
        return (
            "ProfileCreateRequestContext(method='POST', "
            "route='/account/profile', authentication_input=<redacted>)"
        )

    def __reduce_ex__(self, _protocol):
        raise TypeError("profile_create_request_context_not_serializable")


class TrustedProfileCreateGrant:
    """Mutation-specific authority bound to one account, session, and principal."""

    __slots__ = (
        "_account_id",
        "_environment_namespace",
        "_lineage",
        "_principal",
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
        lineage,
    ):
        if (
            cls is not TrustedProfileCreateGrant
            or capability is not _CREATE_GRANT_ISSUANCE
            or type(account_id) is not str
            or type(session_id) is not str
            or type(environment_namespace) is not str
            or type(principal) is not TrustedPrincipalContext
            or type(lineage) is not TrustedProfileCreateLineage
            or principal.environment_namespace != environment_namespace
            or lineage.account_id != account_id
            or lineage.environment_namespace != environment_namespace
            or lineage.principal_id != principal.principal_id
        ):
            raise _configuration_error()
        instance = object.__new__(cls)
        object.__setattr__(instance, "_account_id", account_id)
        object.__setattr__(instance, "_session_id", session_id)
        object.__setattr__(instance, "_environment_namespace", environment_namespace)
        object.__setattr__(instance, "_principal", principal)
        object.__setattr__(instance, "_lineage", lineage)
        object.__setattr__(instance, "_sealed", True)
        return instance

    def __setattr__(self, _name, _value):
        raise AttributeError("trusted_profile_create_grant_is_immutable")

    def principal_for_repository(self):
        return self._principal

    def artifact_binding(self):
        return self._lineage.artifact_binding(
            self._session_id,
            PROFILE_CREATE_PURPOSE,
        )

    def lineage_for_repository(self):
        return self._lineage

    def __repr__(self):
        return "TrustedProfileCreateGrant(<redacted>)"

    def __reduce_ex__(self, _protocol):
        raise TypeError("trusted_profile_create_grant_not_serializable")


@dataclass(frozen=True, slots=True, repr=False)
class ProfileCreateAuthorizationDecision:
    state: str
    _grant: object | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.state not in {"authorized", "denied", "unavailable"}:
            raise _configuration_error()
        if self.state == "authorized":
            if type(self._grant) is not TrustedProfileCreateGrant:
                raise _configuration_error()
        elif self._grant is not None:
            raise _configuration_error()

    def grant_for_service(self):
        return self._grant if self.state == "authorized" else None

    def __repr__(self):
        return f"ProfileCreateAuthorizationDecision(state={self.state!r}, grant=<redacted>)"


class DurablePersistentProfileCreateAuthorizationGateway:
    """Reissue an accepted account-native read resolution as a create grant."""

    __slots__ = ("_read_gateway",)

    def __init__(self, read_gateway):
        if type(read_gateway) is not DurablePersistentProfileReadAuthorizationGateway:
            raise _configuration_error()
        self._read_gateway = read_gateway

    def authorize_profile_create(self, connection, authenticated_actor, session):
        if not isinstance(connection, sqlite3.Connection) or type(session) is not PublicSession:
            return ProfileCreateAuthorizationDecision("unavailable")
        try:
            account_reference = authenticated_actor.account_reference_for_authorization()
            if (
                type(account_reference) is not tuple
                or len(account_reference) != 2
                or account_reference[0] != session.user_id
            ):
                return ProfileCreateAuthorizationDecision("unavailable")
            decision = self._read_gateway.authorize_persistent_profile_read(
                connection,
                authenticated_actor,
            )
            if type(decision) is not PersistentProfileReadAuthorizationDecision:
                return ProfileCreateAuthorizationDecision("unavailable")
            if decision.state == "denied":
                return ProfileCreateAuthorizationDecision("denied")
            if decision.state != "authorized":
                return ProfileCreateAuthorizationDecision("unavailable")
            read_grant = decision.grant_for_application()
            principal = read_grant.principal_for_repository()
            lineage = capture_profile_create_lineage(
                connection,
                account_id=session.user_id,
                environment_namespace=account_reference[1],
                principal_id=principal.principal_id,
            )
            return ProfileCreateAuthorizationDecision(
                "authorized",
                TrustedProfileCreateGrant._issue(
                    _CREATE_GRANT_ISSUANCE,
                    account_id=session.user_id,
                    session_id=session.session_id,
                    environment_namespace=account_reference[1],
                    principal=principal,
                    lineage=lineage,
                ),
            )
        except Exception:
            return ProfileCreateAuthorizationDecision("unavailable")

    def __repr__(self):
        return "DurablePersistentProfileCreateAuthorizationGateway(<configured>)"


@dataclass(frozen=True, slots=True, repr=False)
class ConfirmedProfileArtifactOffer:
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
        return "ConfirmedProfileArtifactOffer(<redacted>)"

    def __reduce_ex__(self, _protocol):
        raise TypeError("confirmed_profile_artifact_offer_not_serializable")


@dataclass(frozen=True, slots=True, repr=False)
class ProfileCreateOutcome:
    state: str

    def __post_init__(self):
        if self.state not in _PROFILE_CREATE_STATES:
            raise _configuration_error()

    def __repr__(self):
        return f"ProfileCreateOutcome(state={self.state!r})"


@dataclass(frozen=True, slots=True, repr=False, init=False)
class ReviewedProfileSourceBundle:
    """Sealed durable sources and V2 construction for one reviewed profile."""

    _reviewed_profile_json: bytes = field(repr=False)
    sources: tuple = field(repr=False)
    correction_json: str | None = field(repr=False)

    def __init__(self, *_args, **_kwargs):
        raise _configuration_error()

    def _source_ordinal_resolver(self, _field_path, source_kind, _explicit):
        if source_kind in {"parsed_free_text", "user_confirmation"}:
            return (1,)
        if source_kind == "user_correction" and self.correction_json is not None:
            return tuple(range(2, len(self.sources) + 1))
        raise ValueError("unexpected_profile_provenance")

    def build_canonical_v2(self, profile_id):
        reviewed_profile = IdentityFreeCanonicalProfileV1.from_json_bytes(
            self._reviewed_profile_json
        )
        return convert_v1_to_v2(
            reviewed_profile.bind_durable_profile_id(profile_id),
            persistent_profile_id=profile_id,
            source_ordinal_resolver=self._source_ordinal_resolver,
        )

    def __repr__(self):
        return (
            "ReviewedProfileSourceBundle("
            f"source_count={len(self.sources)}, content=<redacted>)"
        )

    def __reduce_ex__(self, _protocol):
        raise TypeError("reviewed_profile_source_bundle_not_serializable")


@dataclass(frozen=True, slots=True, repr=False)
class _ConfirmedProfileArtifactSnapshot:
    reviewed_profile_json: bytes = field(repr=False)
    raw_about_you: str = field(repr=False)
    correction_json: str | None = field(repr=False)
    confirmation_time: datetime = field(repr=False)
    accepted_at: datetime = field(repr=False)
    idempotency_key: str = field(repr=False)
    account_id: str = field(repr=False)
    session_id: str = field(repr=False)
    environment_namespace: str = field(repr=False)
    principal_id: str = field(repr=False)
    purpose: str
    command: CreatePersistentProfileCommand = field(repr=False)
    lineage: TrustedProfileCreateLineage = field(repr=False)
    content_fingerprint: str = field(repr=False)

    def binding(self):
        return self.lineage.artifact_binding(
            self.session_id,
            self.purpose,
        )


@dataclass(frozen=True, slots=True, repr=False)
class _ArtifactRecord:
    snapshot: _ConfirmedProfileArtifactSnapshot = field(repr=False)
    deadline: float
    confirmation_identity: str | None = field(default=None, repr=False)
    state: str = "available"
    claim_token: object | None = field(default=None, repr=False)
    recovery_state: str | None = field(default=None, repr=False)
    cleanup_owner: object | None = field(default=None, repr=False)


class _ClaimCleanupOwner:
    __slots__ = (
        "_active",
        "_operation_gate",
        "_reference",
        "_requested_target",
        "_safe",
        "_token",
        "_vault",
    )

    def __init__(self, vault, reference, token):
        self._vault = vault
        self._reference = reference
        self._token = token
        self._operation_gate = threading.Lock()
        self._safe = threading.Event()
        self._active = False
        self._requested_target = None

    @property
    def operation_gate(self):
        return self._operation_gate

    def request_target(self, target):
        if target not in {"available", "reconcile", "retired"}:
            raise _configuration_error()
        if self._requested_target != "retired":
            self._requested_target = target


class _ClaimCleanupCoordinator:
    __slots__ = (
        "_closed",
        "_condition",
        "_owners",
        "_probes",
        "_start_attempted",
        "_starting",
        "_started",
        "_thread",
        "_vault",
    )

    def __init__(self, vault):
        self._vault = vault
        self._condition = threading.Condition()
        self._owners = []
        self._probes = set()
        self._closed = False
        self._start_attempted = False
        self._starting = False
        self._started = False
        self._thread = threading.Thread(
            target=self._run,
            name="confirmed-profile-claim-cleanup",
            daemon=False,
        )

    def start(self):
        with self._condition:
            if self._closed:
                raise RuntimeError("confirmed_profile_artifact_vault_unavailable")
            if self._starting:
                raise RuntimeError("confirmed_profile_artifact_vault_unavailable")
            if self._start_attempted:
                if self._started and self._thread.is_alive():
                    return True
                raise RuntimeError(
                    "confirmed_profile_artifact_vault_unavailable"
                )
            self._start_attempted = True
            self._starting = True
        try:
            self._thread.start()
        except BaseException:
            observed = self._thread_start_observed(self._thread)
            with self._condition:
                self._starting = False
                self._started = observed
                self._condition.notify_all()
            raise
        with self._condition:
            self._starting = False
            self._started = self._thread_start_observed(self._thread)
            closed = self._closed
            self._condition.notify_all()
            alive = self._thread.is_alive()
        if closed or not self._started or not alive:
            raise RuntimeError("confirmed_profile_artifact_vault_unavailable")
        return True

    @staticmethod
    def _thread_start_observed(thread):
        started_event = getattr(thread, "_started", None)
        return bool(
            thread.ident is not None
            or (
                started_event is not None
                and callable(getattr(started_event, "is_set", None))
                and started_event.is_set()
            )
        )

    def register(self, owner):
        with self._condition:
            if self._closed or not self._started or not self._thread.is_alive():
                owner._safe.set()
                return False
            self._owners.append(owner)
            self._condition.notify()
            return True

    def wake(self):
        with self._condition:
            self._condition.notify()

    def close(self):
        deadline = time.monotonic() + _CLAIM_CLEANUP_CLOSE_JOIN_SECONDS
        self._closed = True
        acquired = self._condition.acquire(
            timeout=max(0.0, deadline - time.monotonic())
        )
        if not acquired:
            return False
        try:
            self._condition.notify_all()
            while self._starting:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            started = self._started or self._thread_start_observed(
                self._thread
            )
        finally:
            self._condition.release()
        if started or self._thread.is_alive():
            try:
                self._thread.join(
                    timeout=max(0.0, deadline - time.monotonic())
                )
            except RuntimeError:
                pass
        with self._condition:
            probes = tuple(self._probes)
        for probe in probes:
            if probe.ident is not None or probe.is_alive():
                try:
                    probe.join(timeout=max(0.0, deadline - time.monotonic()))
                except RuntimeError:
                    pass
        with self._condition:
            self._probes = {
                probe for probe in self._probes if probe.is_alive()
            }
            return (
                self._closed
                and not self._starting
                and not self._thread.is_alive()
                and not self._probes
            )

    @property
    def closed(self):
        with self._condition:
            return (
                self._closed
                and not self._starting
                and not self._thread.is_alive()
                and not any(probe.is_alive() for probe in self._probes)
            )

    @property
    def active(self):
        with self._condition:
            return (
                self._started
                and not self._starting
                and not self._closed
                and self._thread.is_alive()
            )

    def _select_ready_locked(self):
        for owner in self._owners:
            if not owner._active and not owner._operation_gate.locked():
                owner._active = True
                return owner
        return None

    def _wait_timeout_locked(self):
        if not self._owners:
            return None
        if any(not owner._active for owner in self._owners):
            return _CLAIM_CLEANUP_POLL_SECONDS
        return None

    def _resolve_owner(self, owner):
        with self._condition:
            owner._active = False
            self._owners = [
                candidate for candidate in self._owners if candidate is not owner
            ]
            owner._safe.set()
            self._condition.notify_all()

    @staticmethod
    def _probe_interruptions(callback):
        try:
            callback("claim.release_compare_transition")
        except BaseException:
            try:
                callback("claim.release_compare_transition_reentrant")
            except BaseException:
                try:
                    callback("claim.release_compare_transition_reentrant_again")
                except BaseException as exc:
                    exc = None

    def _run(self):
        running = True
        while running:
            owner = None
            closing = ()
            with self._condition:
                if self._closed:
                    closing = tuple(self._owners)
                    self._owners.clear()
                    running = False
                else:
                    owner = self._select_ready_locked()
                    if owner is None:
                        self._condition.wait(timeout=self._wait_timeout_locked())
            for candidate in closing:
                candidate._safe.set()
            if not running or owner is None:
                continue
            pending = self._vault._cleanup_owner_pending(owner)
            if pending and self._vault._failure_injector is not None:
                probe = None
                try:
                    probe = threading.Thread(
                        target=self._probe_interruptions,
                        args=(self._vault._hook,),
                        name="confirmed-profile-claim-transition",
                        daemon=False,
                    )
                    with self._condition:
                        self._probes = {
                            candidate
                            for candidate in self._probes
                            if candidate.is_alive()
                        }
                        self._probes.add(probe)
                    probe.start()
                    probe.join(timeout=_CLAIM_CLEANUP_PROBE_JOIN_SECONDS)
                except BaseException as exc:
                    if (
                        probe is not None
                        and not self._thread_start_observed(probe)
                        and not probe.is_alive()
                    ):
                        with self._condition:
                            self._probes.discard(probe)
                    exc = None
                with self._condition:
                    self._probes = {
                        candidate
                        for candidate in self._probes
                        if candidate.is_alive()
                    }
                probe = None
            if pending:
                self._vault._release_core(
                    owner._reference,
                    owner._token,
                    owner,
                )
            self._resolve_owner(owner)


class _ArtifactClaim:
    __slots__ = ("_owner", "_reference", "_token", "_vault")

    def __init__(self, vault, reference, token, owner):
        self._vault = vault
        self._reference = reference
        self._token = token
        self._owner = owner

    def snapshot_for_service(self, capability):
        if capability is not _ARTIFACT_CLAIM_ACCESS:
            raise _configuration_error()
        return self._vault._snapshot_for_claim(
            self._reference,
            self._token,
            self._owner,
        )

    def created(self):
        return self._vault._terminalize(
            self._reference,
            self._token,
            self._owner,
            "created",
        )

    def conflict(self):
        return self._vault._terminalize(
            self._reference,
            self._token,
            self._owner,
            "conflict",
        )

    def retire(self):
        return self._vault._release_owned(
            self._reference,
            self._token,
            self._owner,
            "retired",
        )

    def repository_invocation_started(self):
        return self._vault._mark_reconcile(
            self._reference,
            self._token,
            self._owner,
        )

    def release_definite_rollback(self):
        return self._vault._release_owned(
            self._reference,
            self._token,
            self._owner,
            "available",
        )

    def __repr__(self):
        return "_ArtifactClaim(<redacted>)"


class ConfirmedProfileArtifactVault:
    """Bounded process-local ownership for immutable confirmation artifacts."""

    __slots__ = (
        "_activation_state",
        "_cleanup_coordinator",
        "_closed",
        "_confirmation_references",
        "_failure_injector",
        "_lock",
        "_monotonic",
        "_records",
        "_token_factory",
    )

    def __init__(self, *, monotonic, token_factory, _failure_injector=None):
        if (
            not callable(monotonic)
            or not callable(token_factory)
            or (_failure_injector is not None and not callable(_failure_injector))
        ):
            raise _configuration_error()
        self._monotonic = monotonic
        self._token_factory = token_factory
        self._failure_injector = _failure_injector
        self._lock = threading.Lock()
        self._records = {}
        self._confirmation_references = {}
        self._closed = False
        self._activation_state = "dormant"
        self._cleanup_coordinator = _ClaimCleanupCoordinator(self)

    def activate(self):
        with self._lock:
            if self._closed:
                raise RuntimeError(
                    "confirmed_profile_artifact_vault_unavailable"
                )
            if self._activation_state == "active":
                if self._cleanup_coordinator.active:
                    return True
                raise RuntimeError(
                    "confirmed_profile_artifact_vault_unavailable"
                )
            if self._activation_state != "dormant":
                raise RuntimeError(
                    "confirmed_profile_artifact_vault_unavailable"
                )
            self._activation_state = "activating"
        try:
            started = self._cleanup_coordinator.start()
        except BaseException:
            with self._lock:
                if self._activation_state == "activating":
                    self._activation_state = "failed"
            raise
        with self._lock:
            if (
                not self._closed
                and started is True
                and self._cleanup_coordinator.active
            ):
                self._activation_state = "active"
                return True
            self._activation_state = (
                "closed" if self._closed else "failed"
            )
        self._cleanup_coordinator.close()
        raise RuntimeError("confirmed_profile_artifact_vault_unavailable")

    def _hook(self, boundary):
        if self._failure_injector is not None:
            self._failure_injector(boundary)

    def issue(
        self,
        snapshot,
        *,
        confirmation_identity=None,
        confirmation_witness=None,
    ):
        if (
            type(snapshot) is not _ConfirmedProfileArtifactSnapshot
            or not self._cleanup_coordinator.active
            or (
                confirmation_identity is not None
                and (
                    type(confirmation_identity) is not str
                    or _CONFIRMATION_IDENTITY.fullmatch(confirmation_identity)
                    is None
                    or not callable(
                        getattr(
                            confirmation_witness,
                            "mark_artifact_may_exist",
                            None,
                        )
                    )
                )
            )
            or (
                confirmation_identity is None
                and confirmation_witness is not None
            )
        ):
            raise _configuration_error()
        now = _trusted_monotonic(self._monotonic())
        with self._lock:
            if self._closed:
                raise RuntimeError("confirmed_profile_artifact_vault_unavailable")
            self._purge_expired_locked(now)
            if confirmation_identity is not None:
                existing = self._confirmation_reference_locked(
                    confirmation_identity
                )
                if existing is not None:
                    raise RuntimeError(
                        "confirmed_profile_artifact_confirmation_already_used"
                    )
            if len(self._records) >= PROFILE_CREATE_ARTIFACT_CAPACITY:
                raise RuntimeError("confirmed_profile_artifact_vault_unavailable")
            for _attempt in range(16):
                reference = self._token_factory()
                if (
                    type(reference) is str
                    and _OPAQUE_REFERENCE.fullmatch(reference) is not None
                    and reference not in self._records
                ):
                    if confirmation_identity is not None:
                        confirmation_witness.mark_artifact_may_exist()
                    self._records[reference] = _ArtifactRecord(
                        snapshot,
                        now + PROFILE_CREATE_ARTIFACT_LIFETIME_SECONDS,
                        confirmation_identity,
                    )
                    if confirmation_identity is not None:
                        self._remember_confirmation_locked(
                            confirmation_identity,
                            reference,
                        )
                    return reference
            raise RuntimeError("confirmed_profile_artifact_vault_unavailable")

    def recover_confirmation(self, confirmation_identity, binding):
        if (
            type(confirmation_identity) is not str
            or _CONFIRMATION_IDENTITY.fullmatch(confirmation_identity) is None
            or type(binding) is not tuple
            or len(binding) != 10
            or not self._cleanup_coordinator.active
        ):
            raise _configuration_error()
        now = _trusted_monotonic(self._monotonic())
        with self._lock:
            if self._closed:
                return "unavailable", None
            self._purge_expired_locked(now)
            reference = self._confirmation_reference_locked(
                confirmation_identity
            )
            if reference is None:
                return "absent", None
            record = self._records.get(reference)
            if record is None:
                return "gone", None
            if (
                record.confirmation_identity != confirmation_identity
                or record.snapshot.binding() != binding
                or not _snapshot_integrity_valid(record.snapshot)
            ):
                return "denied", None
            return "found", reference

    def _confirmation_reference_locked(self, confirmation_identity):
        reference = self._confirmation_references.get(
            confirmation_identity
        )
        if reference is not None:
            return reference
        for candidate, record in self._records.items():
            if record.confirmation_identity == confirmation_identity:
                self._remember_confirmation_locked(
                    confirmation_identity,
                    candidate,
                )
                return candidate
        return None

    def _remember_confirmation_locked(
        self,
        confirmation_identity,
        reference,
    ):
        self._confirmation_references[confirmation_identity] = reference
        while (
            len(self._confirmation_references)
            > PROFILE_CREATE_ARTIFACT_CAPACITY
        ):
            removed = False
            for candidate in tuple(self._confirmation_references):
                if any(
                    record.confirmation_identity == candidate
                    for record in self._records.values()
                ):
                    continue
                del self._confirmation_references[candidate]
                removed = True
                break
            if not removed:
                break

    def claim(self, reference, binding, operation):
        if (
            type(reference) is not str
            or _OPAQUE_REFERENCE.fullmatch(reference) is None
            or type(binding) is not tuple
            or len(binding) != 10
            or not callable(operation)
        ):
            return "gone", None
        now = _trusted_monotonic(self._monotonic())
        token = object()
        owner = _ClaimCleanupOwner(self, reference, token)
        claim = _ArtifactClaim(self, reference, token, owner)
        try:
            with owner.operation_gate:
                if not self._cleanup_coordinator.register(owner):
                    return "gone", None
                self._hook("claim.owner_constructed")
                with self._lock:
                    if self._closed:
                        return "gone", None
                    record = self._records.get(reference)
                    if record is None:
                        self._purge_expired_locked(now)
                        return "gone", None
                    if record.snapshot.binding() != binding:
                        return "gone", None
                    if record.state == "in_flight":
                        return "in_flight", None
                    if now >= record.deadline:
                        del self._records[reference]
                        return "gone", None
                    if record.state in {"created", "conflict"}:
                        return record.state, None
                    if record.state == "retired":
                        return "gone", None
                    if (
                        record.state not in {"available", "reconcile"}
                        or record.claim_token is not None
                        or record.cleanup_owner is not None
                    ):
                        return "gone", None
                    recovery_state = (
                        "reconcile" if record.state == "reconcile" else "available"
                    )
                    self._records[reference] = replace(
                        record,
                        state="in_flight",
                        claim_token=token,
                        recovery_state=recovery_state,
                        cleanup_owner=owner,
                    )
                self._hook("claim.published")
                self._hook("claim.consumer_handoff")
                result = operation(claim)
                self._hook("claim.consumer_returned")
                self._release_owned(reference, token, owner, None)
        except Exception:
            self._cleanup_coordinator.wake()
            owner._safe.wait(timeout=_CLAIM_CLEANUP_REQUEST_WAIT_SECONDS)
            raise
        self._cleanup_coordinator.wake()
        return "claimed", result

    def _snapshot_for_claim(self, reference, token, owner):
        with self._lock:
            record = self._records.get(reference)
            if (
                record is None
                or record.state != "in_flight"
                or record.claim_token is not token
                or record.cleanup_owner is not owner
            ):
                raise RuntimeError("confirmed_profile_artifact_claim_unavailable")
            return record.snapshot

    def _terminalize(self, reference, token, owner, state):
        if state not in {"created", "conflict"}:
            raise _configuration_error()
        self._hook("claim.terminalize_enter")
        with self._lock:
            record = self._records.get(reference)
            if (
                record is None
                or record.state != "in_flight"
                or record.claim_token is not token
                or record.cleanup_owner is not owner
            ):
                return False
            self._records[reference] = replace(
                record,
                state=state,
                claim_token=None,
                recovery_state=None,
                cleanup_owner=None,
            )
        self._hook("claim.terminalized")
        return True

    def _mark_reconcile(self, reference, token, owner):
        owner.request_target("reconcile")
        with self._lock:
            record = self._records.get(reference)
            if (
                record is None
                or record.state != "in_flight"
                or record.claim_token is not token
                or record.cleanup_owner is not owner
            ):
                return False
            self._records[reference] = replace(
                record,
                recovery_state="reconcile",
            )
            return True

    def _release_owned(
        self,
        reference,
        token,
        owner,
        target_state,
    ):
        if target_state not in {None, "available", "retired"}:
            raise _configuration_error()
        if target_state is not None:
            owner.request_target(target_state)
            self._hook("claim.release_target_requested")
        self._hook("claim.release_enter")
        if self._cleanup_owner_pending(owner):
            self._hook("claim.release_compare_transition")
        released = self._release_core(reference, token, owner)
        self._hook("claim.released")
        return released

    def _cleanup_owner_pending(self, owner):
        with self._lock:
            record = self._records.get(owner._reference)
            return (
                record is not None
                and record.state == "in_flight"
                and record.claim_token is owner._token
                and record.cleanup_owner is owner
            )

    def _release_core(self, reference, token, owner):
        with self._lock:
            record = self._records.get(reference)
            if (
                record is None
                or record.state != "in_flight"
                or record.claim_token is not token
                or record.cleanup_owner is not owner
            ):
                return True
            release_state = owner._requested_target
            if release_state != "retired" and record.recovery_state == "retired":
                release_state = "retired"
            if release_state is None:
                release_state = record.recovery_state
            if release_state not in {"available", "reconcile", "retired"}:
                release_state = "reconcile"
            self._records[reference] = replace(
                record,
                state=release_state,
                claim_token=None,
                recovery_state=None,
                cleanup_owner=None,
            )
            return True

    def _purge_expired_locked(self, now):
        for reference, record in tuple(self._records.items()):
            if now >= record.deadline and record.state != "in_flight":
                del self._records[reference]

    def close(self):
        with self._lock:
            self._closed = True
            self._activation_state = "closed"
            self._records.clear()
            self._confirmation_references.clear()
        return self._cleanup_coordinator.close()

    @property
    def closed(self):
        with self._lock:
            vault_closed = self._closed
        return vault_closed and self._cleanup_coordinator.closed

    def __repr__(self):
        with self._lock:
            state = "closed" if self._closed else "configured"
        return f"ConfirmedProfileArtifactVault(<{state}>)"


class ConfirmedProfileArtifactUnavailable(Exception):
    __slots__ = ("artifact_may_exist",)

    def __init__(self, *, artifact_may_exist=False):
        self.artifact_may_exist = artifact_may_exist is True
        super().__init__("Profile confirmation is temporarily unavailable.")


class PersistentProfileCreationService:
    """Authenticate, authorize, claim, construct, and atomically create once."""

    __slots__ = (
        "_authentication_gateway",
        "_authorization_gateway",
        "_clock",
        "_closed",
        "_read_connection_provider",
        "_repository",
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
        repository=None,
    ):
        if (
            type(authentication_gateway) is not DurableBrowserSessionAuthenticationGateway
            or type(authorization_gateway)
            is not DurablePersistentProfileCreateAuthorizationGateway
            or not callable(read_connection_provider)
            or not callable(write_connection_provider)
            or type(vault) is not ConfirmedProfileArtifactVault
            or not callable(clock)
            or not callable(token_factory)
            or (repository is not None and type(repository) is not PersistentProfileRepository)
        ):
            raise _configuration_error()
        self._authentication_gateway = authentication_gateway
        self._authorization_gateway = authorization_gateway
        self._read_connection_provider = read_connection_provider
        self._write_connection_provider = write_connection_provider
        self._vault = vault
        self._clock = clock
        self._token_factory = token_factory
        self._repository = repository or PersistentProfileRepository()
        self._closed = False

    def activate(self):
        if self._closed:
            raise ConfirmedProfileArtifactUnavailable()
        return self._vault.activate()

    def issue_confirmed_artifact(
        self,
        *,
        reviewed_profile,
        raw_about_you,
        normalized_updates,
        profile_confirmed,
        authentication_input,
        session_token,
        csrf_secret,
        _confirmation_identity=None,
        _confirmation_witness=None,
        _confirmation_recovery_only=False,
    ):
        confirmation_enabled = _confirmation_identity is not None
        if (
            self._closed
            or profile_confirmed is not True
            or type(_confirmation_recovery_only) is not bool
            or (
                confirmation_enabled
                and (
                    type(_confirmation_identity) is not str
                    or _CONFIRMATION_IDENTITY.fullmatch(
                        _confirmation_identity
                    )
                    is None
                    or not callable(
                        getattr(
                            _confirmation_witness,
                            "mark_artifact_may_exist",
                            None,
                        )
                    )
                    or not callable(
                        getattr(
                            _confirmation_witness,
                            "mark_artifact_definitely_absent",
                            None,
                        )
                    )
                    or not callable(
                        getattr(
                            _confirmation_witness,
                            "record_authority_binding",
                            None,
                        )
                    )
                    or not callable(
                        getattr(
                            _confirmation_witness,
                            "record_valid_offer",
                            None,
                        )
                    )
                )
            )
            or (
                not confirmation_enabled
                and (
                    _confirmation_witness is not None
                    or _confirmation_recovery_only
                )
            )
        ):
            raise ConfirmedProfileArtifactUnavailable()
        outcome, grant = self._request_authority(
            authentication_input=authentication_input,
            session_token=session_token,
            csrf_secret=csrf_secret,
            artifact_reference=None,
            proof=None,
        )
        if outcome != "authorized" or type(grant) is not TrustedProfileCreateGrant:
            raise ConfirmedProfileArtifactUnavailable()
        if confirmation_enabled:
            _confirmation_witness.record_authority_binding(
                grant.artifact_binding()
            )
        reference = None
        try:
            if _confirmation_recovery_only:
                recovery_state, reference = self._vault.recover_confirmation(
                    _confirmation_identity,
                    grant.artifact_binding(),
                )
                if recovery_state == "absent":
                    _confirmation_witness.mark_artifact_definitely_absent()
                    raise ConfirmedProfileArtifactUnavailable()
                if recovery_state != "found" or reference is None:
                    _confirmation_witness.mark_artifact_may_exist()
                    raise ConfirmedProfileArtifactUnavailable(
                        artifact_may_exist=True
                    )
                _confirmation_witness.mark_artifact_may_exist()
                proof = profile_create_csrf_proof(csrf_secret, reference)
                offer = ConfirmedProfileArtifactOffer(reference, proof)
                _confirmation_witness.record_valid_offer(offer)
                return offer
            snapshot = self._prepare_snapshot(
                grant,
                reviewed_profile=reviewed_profile,
                raw_about_you=raw_about_you,
                normalized_updates=normalized_updates,
            )
            reference = self._vault.issue(
                snapshot,
                confirmation_identity=_confirmation_identity,
                confirmation_witness=_confirmation_witness,
            )
            proof = profile_create_csrf_proof(csrf_secret, reference)
            offer = ConfirmedProfileArtifactOffer(reference, proof)
            if confirmation_enabled:
                _confirmation_witness.record_valid_offer(offer)
            return offer
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except ConfirmedProfileArtifactUnavailable:
            raise
        except Exception as exc:
            exc = None
            may_exist = reference is not None or bool(
                getattr(
                    _confirmation_witness,
                    "artifact_may_exist",
                    False,
                )
            )
            raise ConfirmedProfileArtifactUnavailable(
                artifact_may_exist=may_exist
            ) from None

    def consume(
        self,
        *,
        authentication_input,
        session_token,
        csrf_secret,
        artifact_reference,
        csrf_proof,
    ):
        if self._closed:
            return ProfileCreateOutcome("unavailable")
        outcome, grant = self._request_authority(
            authentication_input=authentication_input,
            session_token=session_token,
            csrf_secret=csrf_secret,
            artifact_reference=artifact_reference,
            proof=csrf_proof,
        )
        if outcome != "authorized":
            return ProfileCreateOutcome(outcome)
        claim_state, claim_result = self._vault.claim(
            artifact_reference,
            grant.artifact_binding(),
            lambda claim: self._consume_claim(claim, grant),
        )
        if claim_state == "created":
            return ProfileCreateOutcome("created")
        if claim_state == "conflict":
            return ProfileCreateOutcome("conflict")
        if claim_state == "in_flight":
            return ProfileCreateOutcome("temporary_contention")
        if claim_state != "claimed" or type(claim_result) is not ProfileCreateOutcome:
            return ProfileCreateOutcome("gone")
        return claim_result

    def _consume_claim(self, claim, grant):
        if type(claim) is not _ArtifactClaim or type(grant) is not TrustedProfileCreateGrant:
            raise PersistentProfileDomainError("internal_consistency_failure")
        try:
            snapshot = claim.snapshot_for_service(_ARTIFACT_CLAIM_ACCESS)
            command = _command_from_snapshot(snapshot, grant.principal_for_repository())
            with self._write_connection_provider() as connection:
                if not isinstance(connection, sqlite3.Connection):
                    raise PersistentProfileDomainError("internal_consistency_failure")
                if claim.repository_invocation_started() is not True:
                    raise RuntimeError("confirmed_profile_artifact_claim_unavailable")
                result = self._repository.create_account_native(
                    connection,
                    command,
                    account_lineage=snapshot.lineage,
                )
            if type(getattr(result, "replayed", None)) is not bool:
                return ProfileCreateOutcome("unavailable")
            if claim.created() is not True:
                raise RuntimeError("confirmed_profile_artifact_terminalization_failed")
            return ProfileCreateOutcome("created")
        except PersistentProfileRepositoryDefiniteRollback as exc:
            reason = exc.reason_code
            exc = None
            if claim.release_definite_rollback() is not True:
                return ProfileCreateOutcome("unavailable")
            if reason == "temporary_contention":
                return ProfileCreateOutcome("temporary_contention")
            return ProfileCreateOutcome("unavailable")
        except PersistentProfileRepositoryOutcomeUncertain as exc:
            exc = None
            return ProfileCreateOutcome("unavailable")
        except PersistentProfileDomainError as exc:
            reason = exc.reason_code
            exc = None
            if reason == "profile_already_exists":
                if claim.conflict() is not True:
                    return ProfileCreateOutcome("unavailable")
                return ProfileCreateOutcome("conflict")
            if reason == "temporary_contention":
                claim.release_definite_rollback()
                return ProfileCreateOutcome("temporary_contention")
            if claim.retire() is not True:
                return ProfileCreateOutcome("unavailable")
            return ProfileCreateOutcome("unavailable")

    def authenticate_completed_replay(
        self,
        *,
        authentication_input,
        session_token,
        csrf_secret,
        authority_binding,
    ):
        if (
            self._closed
            or type(authority_binding) is not tuple
            or len(authority_binding) != 10
        ):
            return False
        outcome, grant = self._request_authority(
            authentication_input=authentication_input,
            session_token=session_token,
            csrf_secret=csrf_secret,
            artifact_reference=None,
            proof=None,
        )
        return (
            outcome == "authorized"
            and type(grant) is TrustedProfileCreateGrant
            and grant.artifact_binding() == authority_binding
        )

    def _request_authority(
        self,
        *,
        authentication_input,
        session_token,
        csrf_secret,
        artifact_reference,
        proof,
    ):
        now = _trusted_utc(self._clock())
        try:
            with self._read_connection_provider() as connection:
                if (
                    not isinstance(connection, sqlite3.Connection)
                    or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
                    or connection.execute("PRAGMA query_only").fetchone()[0] != 1
                    or connection.in_transaction
                ):
                    return "unavailable", None
                connection.execute("BEGIN")
                try:
                    actor = self._authentication_gateway.authenticate_browser_request(
                        connection,
                        ProfileCreateRequestContext(authentication_input),
                        now=now,
                    )
                    if actor is None:
                        return "authentication_required", None
                    try:
                        session = validate_session_csrf(
                            connection,
                            session_token=session_token,
                            csrf_secret=csrf_secret,
                            now=now,
                        )
                    except SessionUnavailable:
                        return "csrf_denied", None
                    if artifact_reference is not None:
                        expected = profile_create_csrf_proof(
                            csrf_secret,
                            artifact_reference,
                        )
                        if (
                            type(proof) is not str
                            or _OPAQUE_REFERENCE.fullmatch(proof) is None
                            or not hmac.compare_digest(proof, expected)
                        ):
                            return "csrf_denied", None
                    decision = self._authorization_gateway.authorize_profile_create(
                        connection,
                        actor,
                        session,
                    )
                    if type(decision) is not ProfileCreateAuthorizationDecision:
                        return "unavailable", None
                    if decision.state == "denied":
                        return "authorization_denied", None
                    if decision.state != "authorized":
                        return "unavailable", None
                    return "authorized", decision.grant_for_service()
                finally:
                    if connection.in_transaction:
                        connection.rollback()
        except BrowserSessionAuthenticationUnavailable:
            return "unavailable", None
        except (sqlite3.Error, ValueError, TypeError):
            return "unavailable", None

    def _prepare_snapshot(self, grant, *, reviewed_profile, raw_about_you, normalized_updates):
        if (
            type(reviewed_profile) is not IdentityFreeCanonicalProfileV1
            or type(raw_about_you) is not str
            or type(normalized_updates) is not dict
        ):
            raise _configuration_error()
        reviewed_json = reviewed_profile.canonical_bytes
        parsed = reviewed_profile.to_mapping()
        if parsed["provenance"].get("reviewed") is not True:
            raise _configuration_error()
        accepted_at = _trusted_utc(self._clock())
        confirmation_time = accepted_at
        source_bundle = prepare_reviewed_profile_source_bundle(
            reviewed_profile,
            raw_about_you,
            normalized_updates,
            confirmation_time,
        )
        correction_json = source_bundle.correction_json
        idempotency_token = self._token_factory()
        if type(idempotency_token) is not str or _OPAQUE_REFERENCE.fullmatch(idempotency_token) is None:
            raise _configuration_error()
        binding = grant.artifact_binding()
        account_id, session_id, environment, principal_id = binding[:4]
        purpose = binding[-1]
        idempotency_key = "profile-create:" + idempotency_token
        command = _prepare_command(
            source_bundle=source_bundle,
            accepted_at=accepted_at,
            idempotency_key=idempotency_key,
            principal=grant.principal_for_repository(),
        )
        lineage = grant.lineage_for_repository()
        fingerprint = _artifact_content_fingerprint(
            reviewed_json=reviewed_json,
            raw_about_you=raw_about_you,
            correction_json=correction_json,
            accepted_at=accepted_at,
            confirmation_time=confirmation_time,
            idempotency_key=idempotency_key,
            account_id=account_id,
            session_id=session_id,
            environment=environment,
            principal_id=principal_id,
            purpose=purpose,
            command=command,
            lineage=lineage,
        )
        return _ConfirmedProfileArtifactSnapshot(
            reviewed_profile_json=reviewed_json,
            raw_about_you=raw_about_you,
            correction_json=correction_json,
            confirmation_time=confirmation_time,
            accepted_at=accepted_at,
            idempotency_key=idempotency_key,
            account_id=account_id,
            session_id=session_id,
            environment_namespace=environment,
            principal_id=principal_id,
            purpose=purpose,
            command=command,
            lineage=lineage,
            content_fingerprint=fingerprint,
        )

    def close(self):
        self._closed = True
        return self._vault.close()

    @property
    def closed(self):
        return self._closed and self._vault.closed

    def __repr__(self):
        return "PersistentProfileCreationService(<configured>)"


def profile_create_csrf_proof(csrf_secret, artifact_reference):
    if (
        type(csrf_secret) is not str
        or _OPAQUE_REFERENCE.fullmatch(csrf_secret) is None
        or type(artifact_reference) is not str
        or _OPAQUE_REFERENCE.fullmatch(artifact_reference) is None
    ):
        raise _configuration_error()
    digest = hmac.digest(
        csrf_secret.encode("ascii"),
        PROFILE_CREATE_CSRF_MESSAGE_PREFIX + artifact_reference.encode("ascii"),
        "sha256",
    )
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _correction_source_json(normalized_updates):
    return json.dumps(
        {
            "schema_version": "user_confirmed_correction_v1",
            "updates": normalized_updates,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _partitioned_correction_source_json(
    fragment,
    *,
    part,
    parts,
    complete_content_bytes,
    complete_content_sha256,
):
    return json.dumps(
        {
            "complete_content_bytes": complete_content_bytes,
            "complete_content_sha256": complete_content_sha256,
            "content_fragment": fragment,
            "part": part,
            "partition_version": _CORRECTION_PARTITION_VERSION,
            "parts": parts,
            "schema_version": "user_confirmed_correction_v1",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _largest_correction_fragment_end(
    content,
    start,
    *,
    complete_content_bytes,
    complete_content_sha256,
):
    """Choose one deterministic code-point boundary under the source-row cap."""
    lower = start + 1
    upper = len(content)
    accepted = None
    while lower <= upper:
        midpoint = (lower + upper) // 2
        candidate = _partitioned_correction_source_json(
            content[start:midpoint],
            part=_MAX_CORRECTION_SOURCE_PARTS,
            parts=_MAX_CORRECTION_SOURCE_PARTS,
            complete_content_bytes=complete_content_bytes,
            complete_content_sha256=complete_content_sha256,
        )
        try:
            fits = len(candidate.encode("utf-8")) <= MAX_SOURCE_BYTES
        except UnicodeEncodeError:
            fits = False
        if fits:
            accepted = midpoint
            lower = midpoint + 1
        else:
            upper = midpoint - 1
    if accepted is None:
        raise PersistentProfileDomainError("content_rejected")
    return accepted


def _prepare_user_confirmed_correction_sources(normalized_updates, confirmed_at):
    """Build bounded correction rows while preserving ordinary source bytes."""
    correction_json = _correction_source_json(normalized_updates)
    try:
        correction_bytes = correction_json.encode("utf-8")
    except UnicodeEncodeError:
        raise PersistentProfileDomainError("content_rejected") from None
    if len(correction_bytes) <= MAX_SOURCE_BYTES:
        return (
            correction_json,
            (UserConfirmedCorrectionSourceDraft(correction_json, confirmed_at),),
        )

    content_hash = hashlib.sha256(correction_bytes).hexdigest()
    fragments = []
    position = 0
    for _part in range(_MAX_CORRECTION_SOURCE_PARTS):
        if position == len(correction_json):
            break
        end = _largest_correction_fragment_end(
            correction_json,
            position,
            complete_content_bytes=len(correction_bytes),
            complete_content_sha256=content_hash,
        )
        fragments.append(correction_json[position:end])
        position = end
    if position != len(correction_json) or not fragments:
        raise PersistentProfileDomainError("content_rejected")

    part_count = len(fragments)
    contents = tuple(
        _partitioned_correction_source_json(
            fragment,
            part=index,
            parts=part_count,
            complete_content_bytes=len(correction_bytes),
            complete_content_sha256=content_hash,
        )
        for index, fragment in enumerate(fragments, start=1)
    )
    if (
        "".join(fragments) != correction_json
        or any(len(content.encode("utf-8")) > MAX_SOURCE_BYTES for content in contents)
    ):
        raise PersistentProfileDomainError("content_rejected")
    return (
        correction_json,
        tuple(
            UserConfirmedCorrectionSourceDraft(content, confirmed_at)
            for content in contents
        ),
    )


def prepare_reviewed_profile_source_bundle(
    reviewed_profile,
    raw_about_you,
    normalized_updates,
    confirmed_at,
    require_correction=False,
    partition_correction_sources=False,
):
    if (
        type(reviewed_profile) is not IdentityFreeCanonicalProfileV1
        or type(raw_about_you) is not str
        or type(normalized_updates) is not dict
        or type(require_correction) is not bool
        or type(partition_correction_sources) is not bool
    ):
        raise _configuration_error()
    parsed = reviewed_profile.to_mapping()
    if parsed["provenance"].get("reviewed") is not True:
        raise _configuration_error()
    has_correction = require_correction or any(
        type(detail) is dict and detail.get("source") == "user_correction"
        for detail in parsed["provenance"]["field_sources"].values()
    )
    correction_json = None
    correction_sources = ()
    if has_correction:
        if partition_correction_sources:
            correction_json, correction_sources = (
                _prepare_user_confirmed_correction_sources(
                    normalized_updates,
                    confirmed_at,
                )
            )
        else:
            correction_json = _correction_source_json(normalized_updates)
            correction_sources = (
                UserConfirmedCorrectionSourceDraft(
                    correction_json,
                    confirmed_at,
                ),
            )
    sources = [ConfirmedAboutYouTextSourceDraft(raw_about_you, confirmed_at)]
    sources.extend(correction_sources)
    bundle = object.__new__(ReviewedProfileSourceBundle)
    object.__setattr__(
        bundle,
        "_reviewed_profile_json",
        reviewed_profile.canonical_bytes,
    )
    object.__setattr__(bundle, "sources", tuple(sources))
    object.__setattr__(bundle, "correction_json", correction_json)
    return bundle


def _command_from_snapshot(snapshot, principal):
    if (
        type(snapshot) is not _ConfirmedProfileArtifactSnapshot
        or type(principal) is not TrustedPrincipalContext
        or type(snapshot.command) is not CreatePersistentProfileCommand
        or type(snapshot.lineage) is not TrustedProfileCreateLineage
        or principal.principal_id != snapshot.principal_id
        or principal.environment_namespace != snapshot.environment_namespace
        or snapshot.command.principal != principal
        or snapshot.lineage.principal_id != snapshot.principal_id
        or snapshot.lineage.account_id != snapshot.account_id
        or snapshot.lineage.environment_namespace != snapshot.environment_namespace
        or not _snapshot_integrity_valid(snapshot)
    ):
        raise PersistentProfileDomainError("internal_consistency_failure")
    return snapshot.command


def _prepare_command(
    *,
    source_bundle,
    accepted_at,
    idempotency_key,
    principal,
):
    try:
        if type(source_bundle) is not ReviewedProfileSourceBundle:
            raise TypeError("invalid_reviewed_profile_source_bundle")

        return CreatePersistentProfileCommand.prepare(
            principal=principal,
            canonical_profile_v2=_create_canonical_profile_v2_draft(
                source_bundle.build_canonical_v2
            ),
            sources=source_bundle.sources,
            normalizer_version=PROFILE_CREATE_NORMALIZER_VERSION,
            reviewer_version=PROFILE_CREATE_REVIEWER_VERSION,
            actor_type=PROFILE_CREATE_ACTOR_TYPE,
            reason_code=PROFILE_CREATE_REASON_CODE,
            idempotency_key=idempotency_key,
            accepted_at=accepted_at,
        )
    except (CanonicalProfileV2Error, ValueError, TypeError, KeyError, UnicodeError) as exc:
        exc = None
        raise PersistentProfileDomainError("content_rejected") from None


def _trusted_utc(value):
    if type(value) is not datetime or value.tzinfo is None:
        raise _configuration_error()
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _artifact_content_fingerprint(
    *,
    reviewed_json,
    raw_about_you,
    correction_json,
    accepted_at,
    confirmation_time,
    idempotency_key,
    account_id,
    session_id,
    environment,
    principal_id,
    purpose,
    command,
    lineage,
):
    payload = {
        "version": 1,
        "account_id": account_id,
        "session_id": session_id,
        "environment": environment,
        "principal_id": principal_id,
        "purpose": purpose,
        "binding_id": lineage.binding_id,
        "binding_version": lineage.binding_version,
        "latest_event_version": lineage.latest_event_version,
        "latest_event_id": lineage.latest_event_id,
        "lineage_sha256": lineage.lineage_sha256,
        "reviewed_profile_sha256": hashlib.sha256(reviewed_json).hexdigest(),
        "raw_about_you_sha256": hashlib.sha256(raw_about_you.encode("utf-8")).hexdigest(),
        "correction_sha256": (
            None
            if correction_json is None
            else hashlib.sha256(correction_json.encode("utf-8")).hexdigest()
        ),
        "accepted_at": accepted_at.isoformat(),
        "confirmed_at": confirmation_time.isoformat(),
        "normalizer_version": PROFILE_CREATE_NORMALIZER_VERSION,
        "reviewer_version": PROFILE_CREATE_REVIEWER_VERSION,
        "actor_type": PROFILE_CREATE_ACTOR_TYPE,
        "reason_code": PROFILE_CREATE_REASON_CODE,
        "idempotency_key_sha256": hashlib.sha256(
            idempotency_key.encode("ascii")
        ).hexdigest(),
        "command": {
            "profile_id": command.profile_id,
            "revision_id": command.revision_id,
            "source_ids": list(command.source_ids),
            "structured_profile_sha256": command.structured_profile_sha256,
            "semantic_profile_sha256": command.semantic_profile_sha256,
            "source_content_sha256s": list(command.source_content_sha256s),
            "source_bundle_sha256": command.source_bundle_sha256,
            "request_fingerprint": command.request_fingerprint,
        },
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _snapshot_integrity_valid(snapshot):
    try:
        expected = _artifact_content_fingerprint(
            reviewed_json=snapshot.reviewed_profile_json,
            raw_about_you=snapshot.raw_about_you,
            correction_json=snapshot.correction_json,
            accepted_at=snapshot.accepted_at,
            confirmation_time=snapshot.confirmation_time,
            idempotency_key=snapshot.idempotency_key,
            account_id=snapshot.account_id,
            session_id=snapshot.session_id,
            environment=snapshot.environment_namespace,
            principal_id=snapshot.principal_id,
            purpose=snapshot.purpose,
            command=snapshot.command,
            lineage=snapshot.lineage,
        )
        return hmac.compare_digest(snapshot.content_fingerprint, expected)
    except Exception:
        return False


def _trusted_monotonic(value):
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise _configuration_error()
    return float(value)
