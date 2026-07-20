"""Pure persistent-profile domain foundations for future repository services.

This module is deliberately dormant. It performs no I/O, owns no database
connection, and is not imported by normal product runtime paths. Future trusted
boundaries may construct these immutable commands and pass them to a separately
approved repository service.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
from types import MappingProxyType
from typing import Callable

from wahojobs.profiles.canonical_v2 import (
    CanonicalProfileV2Error,
    SCHEMA_VERSION as CANONICAL_PROFILE_V2,
    canonical_profile_v2_json_bytes,
    parse_canonical_profile_v2_json,
    validate_canonical_profile_v2,
)


MIGRATION_VERSION = "005_persistent_profile_canonical_v2"
SOURCE_CONTENT_HASH_VERSION = "persistent_profile_source_content_v1"
STRUCTURED_PROFILE_HASH_VERSION = "persistent_profile_structured_profile_v1"
SOURCE_BUNDLE_HASH_VERSION = "persistent_profile_source_bundle_v1"
REQUEST_FINGERPRINT_VERSION = "persistent_profile_request_v1"
IDEMPOTENCY_SCOPE_VERSION = "persistent_profile_principal_scope_v1"
LIFECYCLE_SOURCE_SCHEMA_VERSION = "confirmed_lifecycle_action_v1"

MAX_SOURCE_BYTES = 32_768
MAX_SOURCES = 16
MAX_ID_GENERATION_ATTEMPTS = 16

_PROFILE_ID_PATTERN = re.compile(r"^prf_[0-9a-f]{32}$")
_REVISION_ID_PATTERN = re.compile(r"^pvr_[0-9a-f]{32}$")
_SOURCE_ID_PATTERN = re.compile(r"^pfs_[0-9a-f]{32}$")
_PRINCIPAL_ID_PATTERN = re.compile(r"^prn_[0-9a-f]{32}$")
_ENVIRONMENT_PATTERN = re.compile(r"^[a-z0-9_.-]{1,64}$")
_VERSION_PATTERN = re.compile(r"^[a-z0-9_.-]{1,64}$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z0-9_.-]{1,128}$")
_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{16,256}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SEMANTIC_PROFILE_ID = "prf_0123456789abcdef0123456789abcdef"

PRINCIPAL_TYPES = frozenset({"account_native", "development"})
PRINCIPAL_LIFECYCLE_STATUSES = frozenset({"active"})
CLAIM_POLICIES = frozenset({"account_native", "nonclaimable"})
PRINCIPAL_ELIGIBILITY_MODES = frozenset({"account_native", "development_test"})
PRIVACY_OPERATION_SCOPES = frozenset({"deletion_request", "purge"})
ACTOR_TYPES = frozenset({"authenticated_user", "development_service", "system"})
REVISION_KINDS = frozenset(
    {"edit", "correction", "archive", "reactivate", "deletion_request"}
)
LIFECYCLE_ACTIONS = frozenset({"archive", "reactivate", "deletion_request"})
SOURCE_TYPES = frozenset(
    {
        "confirmed_about_you_text",
        "user_confirmed_correction",
        "confirmed_lifecycle_action",
    }
)

ERROR_REASON_CODES = frozenset(
    {
        "invalid_command",
        "ineligible_principal",
        "profile_already_exists",
        "profile_not_found",
        "lifecycle_conflict",
        "stale_revision",
        "idempotency_conflict",
        "content_rejected",
        "purge_not_allowed",
        "temporary_contention",
        "internal_consistency_failure",
        "schema_capability_unavailable",
    }
)
_PUBLIC_ERROR_MESSAGES = MappingProxyType(
    {
        "invalid_command": "The profile request is invalid.",
        "ineligible_principal": "The profile request is not eligible.",
        "profile_already_exists": "The profile request conflicts with existing state.",
        "profile_not_found": "The profile is not available.",
        "lifecycle_conflict": "The profile lifecycle does not allow this request.",
        "stale_revision": "The profile changed before this request completed.",
        "idempotency_conflict": "The request conflicts with an earlier request.",
        "content_rejected": "The profile content could not be accepted.",
        "purge_not_allowed": "The privacy operation is not allowed.",
        "temporary_contention": "The profile request could not be completed yet.",
        "internal_consistency_failure": "The profile request could not be completed.",
        "schema_capability_unavailable": "The required profile capability is unavailable.",
    }
)


class PersistentProfileDomainError(Exception):
    """Stable, bounded, identity-free domain failure."""

    __slots__ = ("reason_code",)

    def __init__(self, reason_code: str):
        if reason_code not in ERROR_REASON_CODES:
            reason_code = "internal_consistency_failure"
        self.reason_code = reason_code
        super().__init__(_PUBLIC_ERROR_MESSAGES[reason_code])

    def public_dict(self) -> dict:
        return {
            "error": "persistent_profile_domain_error",
            "reason_code": self.reason_code,
            "message": _PUBLIC_ERROR_MESSAGES[self.reason_code],
        }

    def __repr__(self) -> str:
        return f"PersistentProfileDomainError(reason_code={self.reason_code!r})"


def _fail(reason_code: str):
    raise PersistentProfileDomainError(reason_code)


def _is_degenerate_payload(value: str, prefix_length: int) -> bool:
    payload = value[prefix_length:]
    return not payload or payload == "0" * len(payload) or len(set(payload)) == 1


def _validate_prefixed_id(value, pattern: re.Pattern, prefix_length: int) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _fail("invalid_command")
    if _is_degenerate_payload(value, prefix_length):
        _fail("invalid_command")
    return value


def validate_profile_id(value) -> str:
    return _validate_prefixed_id(value, _PROFILE_ID_PATTERN, 4)


def validate_revision_id(value) -> str:
    return _validate_prefixed_id(value, _REVISION_ID_PATTERN, 4)


def validate_source_id(value) -> str:
    return _validate_prefixed_id(value, _SOURCE_ID_PATTERN, 4)


def _validate_principal_id(value) -> str:
    return _validate_prefixed_id(value, _PRINCIPAL_ID_PATTERN, 4)


def _generate_id(
    prefix: str,
    validator: Callable[[str], str],
    *,
    is_available: Callable[[str], bool] | None = None,
    max_attempts: int = MAX_ID_GENERATION_ATTEMPTS,
) -> str:
    if type(max_attempts) is not int or not 1 <= max_attempts <= 128:
        _fail("invalid_command")
    if is_available is not None and not callable(is_available):
        _fail("invalid_command")
    for _ in range(max_attempts):
        candidate = prefix + secrets.token_hex(16)
        try:
            validator(candidate)
        except PersistentProfileDomainError:
            continue
        if is_available is None:
            return candidate
        callback_failed = False
        try:
            available = is_available(candidate)
        except Exception:
            callback_failed = True
            available = None
        if callback_failed or type(available) is not bool:
            _fail("internal_consistency_failure")
        if available:
            return candidate
    _fail("internal_consistency_failure")


def generate_profile_id(*, is_available=None, max_attempts=MAX_ID_GENERATION_ATTEMPTS) -> str:
    return _generate_id(
        "prf_", validate_profile_id, is_available=is_available, max_attempts=max_attempts
    )


def generate_revision_id(*, is_available=None, max_attempts=MAX_ID_GENERATION_ATTEMPTS) -> str:
    return _generate_id(
        "pvr_", validate_revision_id, is_available=is_available, max_attempts=max_attempts
    )


def generate_source_id(*, is_available=None, max_attempts=MAX_ID_GENERATION_ATTEMPTS) -> str:
    return _generate_id(
        "pfs_", validate_source_id, is_available=is_available, max_attempts=max_attempts
    )


def _validate_environment(value) -> str:
    if type(value) is not str or _ENVIRONMENT_PATTERN.fullmatch(value) is None:
        _fail("invalid_command")
    return value


def _validate_version(value, *, optional=False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or _VERSION_PATTERN.fullmatch(value) is None:
        _fail("invalid_command")
    return value


def _validate_reason_code(value) -> str:
    if type(value) is not str or _REASON_CODE_PATTERN.fullmatch(value) is None:
        _fail("invalid_command")
    return value


def _validate_idempotency_key(value) -> str:
    if type(value) is not str or _IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
        _fail("invalid_command")
    return value


def canonical_utc_timestamp(value: datetime) -> str:
    """Return the installed whole-second UTC representation."""
    if type(value) is not datetime or value.tzinfo is None:
        _fail("invalid_command")
    offset_failed = False
    try:
        offset = value.utcoffset()
    except Exception:
        offset_failed = True
        offset = None
    if offset_failed:
        _fail("invalid_command")
    if offset != timedelta(0) or value.microsecond != 0:
        _fail("invalid_command")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _timestamp_not_after(left: str, right: str) -> bool:
    return left <= right


@dataclass(frozen=True, repr=False)
class TrustedPrincipalContext:
    principal_id: str = field(repr=False)
    environment_namespace: str = field(repr=False)
    principal_type: str
    lifecycle_status: str
    claim_policy: str
    exclusive_account_binding: bool
    eligibility_mode: str
    active_owner_binding: bool | None = field(default=None, repr=False)

    def __post_init__(self):
        _validate_principal_id(self.principal_id)
        _validate_environment(self.environment_namespace)
        if type(self.principal_type) is not str or self.principal_type not in PRINCIPAL_TYPES:
            _fail("ineligible_principal")
        if (
            type(self.lifecycle_status) is not str
            or self.lifecycle_status not in PRINCIPAL_LIFECYCLE_STATUSES
        ):
            _fail("ineligible_principal")
        if type(self.claim_policy) is not str or self.claim_policy not in CLAIM_POLICIES:
            _fail("ineligible_principal")
        if type(self.exclusive_account_binding) is not bool:
            _fail("ineligible_principal")
        if (
            type(self.eligibility_mode) is not str
            or self.eligibility_mode not in PRINCIPAL_ELIGIBILITY_MODES
        ):
            _fail("ineligible_principal")
        if self.active_owner_binding is not None and type(self.active_owner_binding) is not bool:
            _fail("ineligible_principal")
        if self.eligibility_mode == "account_native":
            if not (
                self.principal_type == "account_native"
                and self.claim_policy == "account_native"
                and self.exclusive_account_binding
                and self.active_owner_binding is True
            ):
                _fail("ineligible_principal")
        else:
            if not (
                self.principal_type == "development"
                and self.claim_policy == "nonclaimable"
                and not self.exclusive_account_binding
                and self.environment_namespace in {"development", "test"}
                and self.active_owner_binding in {False, None}
            ):
                _fail("ineligible_principal")

    def authorizes_actor(self, actor_type: str) -> bool:
        if self.eligibility_mode == "account_native":
            return actor_type == "authenticated_user"
        return actor_type in {"development_service", "system"}

    def public_dict(self) -> dict:
        return {
            "principal_type": self.principal_type,
            "lifecycle_status": self.lifecycle_status,
            "eligibility_mode": self.eligibility_mode,
            "eligible": True,
        }

    def __repr__(self) -> str:
        return (
            "TrustedPrincipalContext("
            f"principal_type={self.principal_type!r}, "
            f"eligibility_mode={self.eligibility_mode!r}, eligible=True)"
        )


@dataclass(frozen=True, repr=False)
class TrustedPrivacyAdminContext:
    operation_scope: str
    environment_namespace: str = field(repr=False)

    def __post_init__(self):
        if type(self.operation_scope) is not str or self.operation_scope not in PRIVACY_OPERATION_SCOPES:
            _fail("purge_not_allowed")
        _validate_environment(self.environment_namespace)

    def public_dict(self) -> dict:
        return {"operation_scope": self.operation_scope, "trusted": True}

    def __repr__(self) -> str:
        return f"TrustedPrivacyAdminContext(operation_scope={self.operation_scope!r})"


@dataclass(frozen=True, repr=False)
class TrustedPersistentProfileReference:
    profile_id: str = field(repr=False)
    principal_id: str = field(repr=False)
    environment_namespace: str = field(repr=False)

    def __post_init__(self):
        validate_profile_id(self.profile_id)
        _validate_principal_id(self.principal_id)
        _validate_environment(self.environment_namespace)

    def public_dict(self) -> dict:
        return {"resource": "persistent_profile", "trusted": True}

    def __repr__(self) -> str:
        return "TrustedPersistentProfileReference(resource='persistent_profile')"


@dataclass(frozen=True)
class PersistentProfileSchemaCapabilities:
    migration_version: str
    canonical_versions: frozenset[str]
    source_types: frozenset[str]
    lifecycle_source_schema_versions: frozenset[str]

    def __post_init__(self):
        _validate_version(self.migration_version)
        if type(self.canonical_versions) is not frozenset or not self.canonical_versions or any(
            type(value) is not str for value in self.canonical_versions
        ):
            _fail("schema_capability_unavailable")
        if (
            type(self.source_types) is not frozenset
            or not self.source_types
            or any(type(value) is not str or value not in SOURCE_TYPES for value in self.source_types)
        ):
            _fail("schema_capability_unavailable")
        if (
            type(self.lifecycle_source_schema_versions) is not frozenset
            or not self.lifecycle_source_schema_versions
            or any(
            type(value) is not str for value in self.lifecycle_source_schema_versions
            )
        ):
            _fail("schema_capability_unavailable")

    def require_canonical_v2(self):
        if CANONICAL_PROFILE_V2 not in self.canonical_versions:
            _fail("schema_capability_unavailable")

    def require_source(self, source) -> None:
        if source.source_type not in self.source_types:
            _fail("schema_capability_unavailable")
        if (
            source.source_type == "confirmed_lifecycle_action"
            and source.source_schema_version not in self.lifecycle_source_schema_versions
        ):
            _fail("schema_capability_unavailable")


MIGRATION_005_CAPABILITIES = PersistentProfileSchemaCapabilities(
    migration_version=MIGRATION_VERSION,
    canonical_versions=frozenset({CANONICAL_PROFILE_V2}),
    source_types=SOURCE_TYPES,
    lifecycle_source_schema_versions=frozenset({LIFECYCLE_SOURCE_SCHEMA_VERSION}),
)


def _validate_source_content(content: str, *, require_json_object: bool) -> bytes:
    if type(content) is not str:
        _fail("content_rejected")
    encoding_failed = False
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError:
        encoding_failed = True
        encoded = b""
    if encoding_failed:
        _fail("content_rejected")
    if not 1 <= len(encoded) <= MAX_SOURCE_BYTES:
        _fail("content_rejected")
    for char in content:
        codepoint = ord(char)
        if (0 <= codepoint <= 31 and codepoint not in {9, 10, 13}) or 127 <= codepoint <= 159:
            _fail("content_rejected")
    if require_json_object:
        parsing_failed = False
        try:
            parsed = json.loads(
                content,
                object_pairs_hook=_unique_json_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            parsing_failed = True
            parsed = None
        if parsing_failed:
            _fail("content_rejected")
        if type(parsed) is not dict:
            _fail("content_rejected")
    return encoded


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


@dataclass(frozen=True, repr=False)
class ConfirmedAboutYouTextSourceDraft:
    content: str = field(repr=False)
    confirmed_at: datetime | str = field(repr=False)
    source_schema_version: str = "confirmed_about_you_text_v1"
    parser_version: str | None = None
    source_type: str = field(init=False, default="confirmed_about_you_text")
    source_format: str = field(init=False, default="text/plain")

    def __post_init__(self):
        _validate_source_content(self.content, require_json_object=False)
        object.__setattr__(self, "confirmed_at", canonical_utc_timestamp(self.confirmed_at))
        _validate_version(self.source_schema_version)
        _validate_version(self.parser_version, optional=True)

    @property
    def content_bytes(self) -> bytes:
        return self.content.encode("utf-8")

    def public_dict(self) -> dict:
        return _source_public_dict(self)

    def __repr__(self) -> str:
        return _source_repr(self)


@dataclass(frozen=True, repr=False)
class UserConfirmedCorrectionSourceDraft:
    content: str = field(repr=False)
    confirmed_at: datetime | str = field(repr=False)
    source_schema_version: str = "user_confirmed_correction_v1"
    parser_version: str | None = None
    source_type: str = field(init=False, default="user_confirmed_correction")
    source_format: str = field(init=False, default="application/json")

    def __post_init__(self):
        _validate_source_content(self.content, require_json_object=True)
        object.__setattr__(self, "confirmed_at", canonical_utc_timestamp(self.confirmed_at))
        _validate_version(self.source_schema_version)
        _validate_version(self.parser_version, optional=True)

    @property
    def content_bytes(self) -> bytes:
        return self.content.encode("utf-8")

    def public_dict(self) -> dict:
        return _source_public_dict(self)

    def __repr__(self) -> str:
        return _source_repr(self)


@dataclass(frozen=True, repr=False, init=False)
class LifecycleActionSourceDraft:
    action: str
    confirmed_at: str = field(repr=False)
    content: str = field(repr=False)
    source_type: str = "confirmed_lifecycle_action"
    source_format: str = "application/json"
    source_schema_version: str = LIFECYCLE_SOURCE_SCHEMA_VERSION
    parser_version: None = None

    def __init__(self, *args, **kwargs):
        raise TypeError("Lifecycle action sources use for_action().")

    @classmethod
    def for_action(cls, action: str, *, confirmed_at: datetime):
        if type(action) is not str or action not in LIFECYCLE_ACTIONS:
            _fail("content_rejected")
        timestamp = canonical_utc_timestamp(confirmed_at)
        content = json.dumps(
            {"action": action, "schema_version": LIFECYCLE_SOURCE_SCHEMA_VERSION},
            sort_keys=True,
            separators=(",", ":"),
        )
        _validate_source_content(content, require_json_object=True)
        instance = object.__new__(cls)
        object.__setattr__(instance, "action", action)
        object.__setattr__(instance, "confirmed_at", timestamp)
        object.__setattr__(instance, "content", content)
        object.__setattr__(instance, "source_type", "confirmed_lifecycle_action")
        object.__setattr__(instance, "source_format", "application/json")
        object.__setattr__(instance, "source_schema_version", LIFECYCLE_SOURCE_SCHEMA_VERSION)
        object.__setattr__(instance, "parser_version", None)
        return instance

    @property
    def content_bytes(self) -> bytes:
        return self.content.encode("utf-8")

    def public_dict(self) -> dict:
        return {**_source_public_dict(self), "action": self.action}

    def __repr__(self) -> str:
        return f"LifecycleActionSourceDraft(action={self.action!r}, content=<redacted>)"


SourceDraft = (
    ConfirmedAboutYouTextSourceDraft
    | UserConfirmedCorrectionSourceDraft
    | LifecycleActionSourceDraft
)


def _source_public_dict(source: SourceDraft) -> dict:
    return {
        "source_type": source.source_type,
        "source_format": source.source_format,
        "source_schema_version": source.source_schema_version,
        "confirmed_at": source.confirmed_at,
        "content_length": len(source.content_bytes),
        "content_included": False,
    }


def _source_repr(source: SourceDraft) -> str:
    return f"{type(source).__name__}(source_type={source.source_type!r}, content=<redacted>)"


def _validate_sources(sources, capabilities) -> tuple[SourceDraft, ...]:
    if type(capabilities) is not PersistentProfileSchemaCapabilities:
        _fail("schema_capability_unavailable")
    if type(sources) not in {tuple, list} or not 1 <= len(sources) <= MAX_SOURCES:
        _fail("invalid_command")
    result = tuple(sources)
    for source in result:
        if type(source) not in {
            ConfirmedAboutYouTextSourceDraft,
            UserConfirmedCorrectionSourceDraft,
            LifecycleActionSourceDraft,
        }:
            _fail("invalid_command")
        capabilities.require_source(source)
    return result


def source_content_hash(source: SourceDraft) -> str:
    if type(source) not in {
        ConfirmedAboutYouTextSourceDraft,
        UserConfirmedCorrectionSourceDraft,
        LifecycleActionSourceDraft,
    }:
        _fail("invalid_command")
    return hashlib.sha256(source.content_bytes).hexdigest()


def _validated_profile_bytes(profile_v2: dict) -> bytes:
    validation_failed = False
    try:
        profile_bytes = canonical_profile_v2_json_bytes(profile_v2)
    except CanonicalProfileV2Error:
        validation_failed = True
        profile_bytes = b""
    if validation_failed:
        _fail("content_rejected")
    return profile_bytes


def structured_profile_hash(profile_v2: dict) -> str:
    return hashlib.sha256(_validated_profile_bytes(profile_v2)).hexdigest()


def _semantic_profile_hash_from_bytes(profile_bytes: bytes) -> str:
    validation_failed = False
    try:
        profile = parse_canonical_profile_v2_json(profile_bytes)
        profile["identity"]["profile_id"] = _SEMANTIC_PROFILE_ID
        semantic_bytes = canonical_profile_v2_json_bytes(profile)
    except CanonicalProfileV2Error:
        validation_failed = True
        semantic_bytes = b""
    if validation_failed:
        _fail("content_rejected")
    return hashlib.sha256(semantic_bytes).hexdigest()


def source_bundle_manifest(sources) -> dict:
    source_tuple = _validate_sources(sources, MIGRATION_005_CAPABILITIES)
    return {
        "version": SOURCE_BUNDLE_HASH_VERSION,
        "sources": [
            {
                "ordinal": ordinal,
                "source_type": source.source_type,
                "source_format": source.source_format,
                "source_schema_version": source.source_schema_version,
                "parser_version": source.parser_version,
                "confirmed_at": source.confirmed_at,
                "byte_length": len(source.content_bytes),
                "source_content_hash": source_content_hash(source),
            }
            for ordinal, source in enumerate(source_tuple, start=1)
        ],
    }


def _canonical_json_bytes(value: dict) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def source_bundle_hash(sources) -> str:
    return hashlib.sha256(_canonical_json_bytes(source_bundle_manifest(sources))).hexdigest()


def _idempotency_key_hash(key: str) -> str:
    return hashlib.sha256(_validate_idempotency_key(key).encode("utf-8")).hexdigest()


def _validate_actor(principal: TrustedPrincipalContext, actor_type: str) -> str:
    if (
        type(actor_type) is not str
        or actor_type not in ACTOR_TYPES
        or not principal.authorizes_actor(actor_type)
    ):
        _fail("ineligible_principal")
    return actor_type


def _validated_profile_for_create(profile_v2: dict, profile_id: str) -> bytes:
    validation_failed = False
    try:
        validated = validate_canonical_profile_v2(profile_v2)
        rebound = deepcopy(validated)
        rebound["identity"]["profile_id"] = profile_id
        profile_bytes = canonical_profile_v2_json_bytes(rebound)
    except CanonicalProfileV2Error:
        validation_failed = True
        profile_bytes = b""
    if validation_failed:
        _fail("content_rejected")
    return profile_bytes


def _validated_profile_for_reference(profile_v2: dict, reference) -> bytes:
    validation_failed = False
    try:
        validated = validate_canonical_profile_v2(profile_v2)
    except CanonicalProfileV2Error:
        validation_failed = True
        validated = None
    if validation_failed:
        _fail("content_rejected")
    if validated["identity"]["profile_id"] != reference.profile_id:
        _fail("invalid_command")
    return canonical_profile_v2_json_bytes(validated)


def _profile_from_bytes(value: bytes) -> dict:
    validation_failed = False
    try:
        profile = parse_canonical_profile_v2_json(value)
    except CanonicalProfileV2Error:
        validation_failed = True
        profile = None
    if validation_failed:
        _fail("internal_consistency_failure")
    return profile


def _request_digest(payload: dict) -> str:
    envelope = {"version": REQUEST_FINGERPRINT_VERSION, "request": payload}
    return hashlib.sha256(_canonical_json_bytes(envelope)).hexdigest()


@dataclass(frozen=True, repr=False, init=False)
class CreatePersistentProfileCommand:
    principal: TrustedPrincipalContext
    _profile_id: str = field(repr=False)
    _structured_profile_json: bytes = field(repr=False)
    _sources: tuple[SourceDraft, ...] = field(repr=False)
    canonical_schema_version: str
    normalizer_version: str | None
    reviewer_version: str | None
    actor_type: str
    reason_code: str
    _idempotency_key: str = field(repr=False)
    accepted_at: str
    structured_profile_sha256: str
    source_bundle_sha256: str
    request_fingerprint: str = field(repr=False)

    @classmethod
    def prepare(
        cls,
        *,
        principal: TrustedPrincipalContext,
        canonical_profile_v2: dict,
        sources,
        normalizer_version: str | None,
        reviewer_version: str | None,
        actor_type: str,
        reason_code: str,
        idempotency_key: str,
        accepted_at: datetime,
        capabilities: PersistentProfileSchemaCapabilities = MIGRATION_005_CAPABILITIES,
    ):
        if type(principal) is not TrustedPrincipalContext:
            _fail("ineligible_principal")
        if type(capabilities) is not PersistentProfileSchemaCapabilities:
            _fail("schema_capability_unavailable")
        capabilities.require_canonical_v2()
        source_tuple = _validate_sources(sources, capabilities)
        if any(source.source_type == "confirmed_lifecycle_action" for source in source_tuple):
            _fail("invalid_command")
        if not any(source.source_type == "confirmed_about_you_text" for source in source_tuple):
            _fail("invalid_command")
        timestamp = canonical_utc_timestamp(accepted_at)
        if any(not _timestamp_not_after(source.confirmed_at, timestamp) for source in source_tuple):
            _fail("invalid_command")
        actor = _validate_actor(principal, actor_type)
        normalizer = _validate_version(normalizer_version, optional=True)
        reviewer = _validate_version(reviewer_version, optional=True)
        reason = _validate_reason_code(reason_code)
        key = _validate_idempotency_key(idempotency_key)
        profile_id = generate_profile_id()
        profile_bytes = _validated_profile_for_create(canonical_profile_v2, profile_id)
        profile_hash = hashlib.sha256(profile_bytes).hexdigest()
        semantic_hash = _semantic_profile_hash_from_bytes(profile_bytes)
        bundle_hash = source_bundle_hash(source_tuple)
        payload = {
            "operation": "create",
            "principal_id": principal.principal_id,
            "environment_namespace": principal.environment_namespace,
            "canonical_schema_version": CANONICAL_PROFILE_V2,
            "semantic_structured_profile_hash": semantic_hash,
            "source_bundle_hash": bundle_hash,
            "normalizer_version": normalizer,
            "reviewer_version": reviewer,
            "actor_type": actor,
            "reason_code": reason,
            "accepted_at": timestamp,
            "idempotency_scope_version": IDEMPOTENCY_SCOPE_VERSION,
            "idempotency_key_hash": _idempotency_key_hash(key),
        }
        instance = object.__new__(cls)
        for name, value in {
            "principal": principal,
            "_profile_id": profile_id,
            "_structured_profile_json": profile_bytes,
            "_sources": source_tuple,
            "canonical_schema_version": CANONICAL_PROFILE_V2,
            "normalizer_version": normalizer,
            "reviewer_version": reviewer,
            "actor_type": actor,
            "reason_code": reason,
            "_idempotency_key": key,
            "accepted_at": timestamp,
            "structured_profile_sha256": profile_hash,
            "source_bundle_sha256": bundle_hash,
            "request_fingerprint": _request_digest(payload),
        }.items():
            object.__setattr__(instance, name, value)
        return instance

    @property
    def profile_id(self) -> str:
        return self._profile_id

    @property
    def sources(self) -> tuple[SourceDraft, ...]:
        return self._sources

    def trusted_structured_profile(self) -> dict:
        return _profile_from_bytes(self._structured_profile_json)

    def idempotency_scope(self) -> tuple[str, str]:
        return (self.principal.principal_id, self._idempotency_key)

    def public_dict(self) -> dict:
        return {
            "operation": "create",
            "canonical_schema_version": self.canonical_schema_version,
            "source_count": len(self._sources),
            "accepted_at": self.accepted_at,
        }

    def __repr__(self) -> str:
        return (
            "CreatePersistentProfileCommand("
            f"source_count={len(self._sources)}, accepted_at={self.accepted_at!r})"
        )


@dataclass(frozen=True, repr=False, init=False)
class AppendProfileRevisionCommand:
    principal: TrustedPrincipalContext
    profile: TrustedPersistentProfileReference
    expected_current_revision_number: int
    revision_kind: str
    correction_of_revision_id: str | None = field(repr=False)
    _structured_profile_json: bytes = field(repr=False)
    _sources: tuple[SourceDraft, ...] = field(repr=False)
    canonical_schema_version: str
    normalizer_version: str | None
    reviewer_version: str | None
    actor_type: str
    reason_code: str
    _idempotency_key: str = field(repr=False)
    accepted_at: str
    resulting_lifecycle: str
    structured_profile_sha256: str
    source_bundle_sha256: str
    request_fingerprint: str = field(repr=False)

    @classmethod
    def prepare(
        cls,
        *,
        principal: TrustedPrincipalContext,
        profile: TrustedPersistentProfileReference,
        expected_current_revision_number: int,
        revision_kind: str,
        canonical_profile_v2: dict,
        sources,
        correction_of_revision_id: str | None,
        normalizer_version: str | None,
        reviewer_version: str | None,
        actor_type: str,
        reason_code: str,
        idempotency_key: str,
        accepted_at: datetime,
        capabilities: PersistentProfileSchemaCapabilities = MIGRATION_005_CAPABILITIES,
    ):
        if type(principal) is not TrustedPrincipalContext or type(profile) is not TrustedPersistentProfileReference:
            _fail("invalid_command")
        if (
            principal.principal_id != profile.principal_id
            or principal.environment_namespace != profile.environment_namespace
        ):
            _fail("ineligible_principal")
        if type(expected_current_revision_number) is not int or expected_current_revision_number < 1:
            _fail("invalid_command")
        if type(revision_kind) is not str or revision_kind not in REVISION_KINDS:
            _fail("invalid_command")
        if type(capabilities) is not PersistentProfileSchemaCapabilities:
            _fail("schema_capability_unavailable")
        capabilities.require_canonical_v2()
        source_tuple = _validate_sources(sources, capabilities)
        timestamp = canonical_utc_timestamp(accepted_at)
        if any(not _timestamp_not_after(source.confirmed_at, timestamp) for source in source_tuple):
            _fail("invalid_command")
        if revision_kind in LIFECYCLE_ACTIONS:
            if not (
                len(source_tuple) == 1
                and type(source_tuple[0]) is LifecycleActionSourceDraft
                and source_tuple[0].action == revision_kind
                and correction_of_revision_id is None
            ):
                _fail("lifecycle_conflict")
            resulting_lifecycle = {
                "archive": "archived",
                "reactivate": "active",
                "deletion_request": "deletion_requested",
            }[revision_kind]
        else:
            if any(type(source) is LifecycleActionSourceDraft for source in source_tuple):
                _fail("lifecycle_conflict")
            if revision_kind == "correction":
                if correction_of_revision_id is None or not any(
                    source.source_type == "user_confirmed_correction" for source in source_tuple
                ):
                    _fail("invalid_command")
                validate_revision_id(correction_of_revision_id)
            elif correction_of_revision_id is not None:
                _fail("invalid_command")
            resulting_lifecycle = "preserve_current"
        profile_bytes = _validated_profile_for_reference(canonical_profile_v2, profile)
        actor = _validate_actor(principal, actor_type)
        normalizer = _validate_version(normalizer_version, optional=True)
        reviewer = _validate_version(reviewer_version, optional=True)
        reason = _validate_reason_code(reason_code)
        key = _validate_idempotency_key(idempotency_key)
        profile_hash = hashlib.sha256(profile_bytes).hexdigest()
        bundle_hash = source_bundle_hash(source_tuple)
        payload = {
            "operation": "append_revision",
            "principal_id": principal.principal_id,
            "environment_namespace": principal.environment_namespace,
            "profile_id": profile.profile_id,
            "expected_current_revision_number": expected_current_revision_number,
            "revision_kind": revision_kind,
            "correction_of_revision_id": correction_of_revision_id,
            "resulting_lifecycle": resulting_lifecycle,
            "canonical_schema_version": CANONICAL_PROFILE_V2,
            "structured_profile_hash": profile_hash,
            "source_bundle_hash": bundle_hash,
            "normalizer_version": normalizer,
            "reviewer_version": reviewer,
            "actor_type": actor,
            "reason_code": reason,
            "accepted_at": timestamp,
            "idempotency_scope_version": IDEMPOTENCY_SCOPE_VERSION,
            "idempotency_key_hash": _idempotency_key_hash(key),
        }
        instance = object.__new__(cls)
        for name, value in {
            "principal": principal,
            "profile": profile,
            "expected_current_revision_number": expected_current_revision_number,
            "revision_kind": revision_kind,
            "correction_of_revision_id": correction_of_revision_id,
            "_structured_profile_json": profile_bytes,
            "_sources": source_tuple,
            "canonical_schema_version": CANONICAL_PROFILE_V2,
            "normalizer_version": normalizer,
            "reviewer_version": reviewer,
            "actor_type": actor,
            "reason_code": reason,
            "_idempotency_key": key,
            "accepted_at": timestamp,
            "resulting_lifecycle": resulting_lifecycle,
            "structured_profile_sha256": profile_hash,
            "source_bundle_sha256": bundle_hash,
            "request_fingerprint": _request_digest(payload),
        }.items():
            object.__setattr__(instance, name, value)
        return instance

    @property
    def sources(self) -> tuple[SourceDraft, ...]:
        return self._sources

    def trusted_structured_profile(self) -> dict:
        return _profile_from_bytes(self._structured_profile_json)

    def idempotency_scope(self) -> tuple[str, str]:
        return (self.principal.principal_id, self._idempotency_key)

    def public_dict(self) -> dict:
        return {
            "operation": "append_revision",
            "revision_kind": self.revision_kind,
            "source_count": len(self._sources),
            "accepted_at": self.accepted_at,
            "resulting_lifecycle": self.resulting_lifecycle,
        }

    def __repr__(self) -> str:
        return (
            "AppendProfileRevisionCommand("
            f"revision_kind={self.revision_kind!r}, source_count={len(self._sources)}, "
            f"accepted_at={self.accepted_at!r})"
        )


@dataclass(frozen=True, repr=False, init=False)
class PurgePersistentProfileCommand:
    privacy_admin: TrustedPrivacyAdminContext
    profile: TrustedPersistentProfileReference
    _operation_key: str = field(repr=False)
    accepted_at: str
    request_fingerprint: str = field(repr=False)

    @classmethod
    def prepare(
        cls,
        *,
        privacy_admin: TrustedPrivacyAdminContext,
        profile: TrustedPersistentProfileReference,
        operation_key: str,
        accepted_at: datetime,
    ):
        if type(privacy_admin) is not TrustedPrivacyAdminContext or privacy_admin.operation_scope != "purge":
            _fail("purge_not_allowed")
        if type(profile) is not TrustedPersistentProfileReference:
            _fail("invalid_command")
        if privacy_admin.environment_namespace != profile.environment_namespace:
            _fail("purge_not_allowed")
        key = _validate_idempotency_key(operation_key)
        timestamp = canonical_utc_timestamp(accepted_at)
        payload = {
            "operation": "purge",
            "profile_id": profile.profile_id,
            "principal_id": profile.principal_id,
            "environment_namespace": profile.environment_namespace,
            "privacy_operation_scope": privacy_admin.operation_scope,
            "operation_key_hash": _idempotency_key_hash(key),
            "accepted_at": timestamp,
            "idempotency_scope_version": IDEMPOTENCY_SCOPE_VERSION,
        }
        instance = object.__new__(cls)
        object.__setattr__(instance, "privacy_admin", privacy_admin)
        object.__setattr__(instance, "profile", profile)
        object.__setattr__(instance, "_operation_key", key)
        object.__setattr__(instance, "accepted_at", timestamp)
        object.__setattr__(instance, "request_fingerprint", _request_digest(payload))
        return instance

    def public_dict(self) -> dict:
        return {"operation": "purge", "accepted_at": self.accepted_at}

    def __repr__(self) -> str:
        return f"PurgePersistentProfileCommand(accepted_at={self.accepted_at!r})"


def classify_replay(existing_fingerprint: str, candidate_fingerprint: str) -> str:
    for value in (existing_fingerprint, candidate_fingerprint):
        if type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None:
            _fail("invalid_command")
    return (
        "exact_replay"
        if hmac.compare_digest(existing_fingerprint, candidate_fingerprint)
        else "changed_conflict"
    )


def request_fingerprint(command) -> str:
    if type(command) not in {
        CreatePersistentProfileCommand,
        AppendProfileRevisionCommand,
        PurgePersistentProfileCommand,
    }:
        _fail("invalid_command")
    return command.request_fingerprint


@dataclass(frozen=True, repr=False)
class ProfileCreatedResult:
    profile_id: str = field(repr=False)
    revision_id: str = field(repr=False)
    revision_number: int
    lifecycle_status: str
    created_at: str
    replayed: bool = False

    def __post_init__(self):
        validate_profile_id(self.profile_id)
        validate_revision_id(self.revision_id)
        _validate_result_common(self.revision_number, self.lifecycle_status, self.created_at)
        if type(self.replayed) is not bool:
            _fail("internal_consistency_failure")

    def public_dict(self) -> dict:
        return {
            "status": "created",
            "revision_number": self.revision_number,
            "lifecycle_status": self.lifecycle_status,
            "created_at": self.created_at,
            "replayed": self.replayed,
        }

    def trusted_dict(self) -> dict:
        return {**self.public_dict(), "profile_id": self.profile_id, "revision_id": self.revision_id}

    def __repr__(self) -> str:
        return f"ProfileCreatedResult(status='created', revision_number={self.revision_number})"


@dataclass(frozen=True, repr=False)
class ProfileRevisionResult:
    profile_id: str = field(repr=False)
    revision_id: str = field(repr=False)
    revision_number: int
    revision_kind: str
    lifecycle_status: str
    created_at: str
    replayed: bool = False

    def __post_init__(self):
        validate_profile_id(self.profile_id)
        validate_revision_id(self.revision_id)
        if type(self.revision_kind) is not str or self.revision_kind not in REVISION_KINDS:
            _fail("internal_consistency_failure")
        _validate_result_common(self.revision_number, self.lifecycle_status, self.created_at)
        if type(self.replayed) is not bool:
            _fail("internal_consistency_failure")

    def public_dict(self) -> dict:
        return {
            "status": "revision_recorded",
            "revision_number": self.revision_number,
            "revision_kind": self.revision_kind,
            "lifecycle_status": self.lifecycle_status,
            "created_at": self.created_at,
            "replayed": self.replayed,
        }

    def trusted_dict(self) -> dict:
        return {**self.public_dict(), "profile_id": self.profile_id, "revision_id": self.revision_id}

    def __repr__(self) -> str:
        return (
            "ProfileRevisionResult("
            f"revision_kind={self.revision_kind!r}, revision_number={self.revision_number})"
        )


@dataclass(frozen=True, repr=False, init=False)
class CurrentProfileSummary:
    profile_id: str = field(repr=False)
    revision_id: str = field(repr=False)
    revision_number: int
    lifecycle_status: str
    _structured_profile_json: bytes = field(repr=False)
    updated_at: str

    @classmethod
    def from_trusted(
        cls,
        *,
        profile_id: str,
        revision_id: str,
        revision_number: int,
        lifecycle_status: str,
        structured_profile_json: bytes,
        updated_at: str,
    ):
        validate_profile_id(profile_id)
        validate_revision_id(revision_id)
        _validate_result_common(revision_number, lifecycle_status, updated_at)
        content = bytes(structured_profile_json)
        profile = _profile_from_bytes(content)
        if profile["identity"]["profile_id"] != profile_id:
            _fail("internal_consistency_failure")
        instance = object.__new__(cls)
        object.__setattr__(instance, "profile_id", profile_id)
        object.__setattr__(instance, "revision_id", revision_id)
        object.__setattr__(instance, "revision_number", revision_number)
        object.__setattr__(instance, "lifecycle_status", lifecycle_status)
        object.__setattr__(instance, "_structured_profile_json", content)
        object.__setattr__(instance, "updated_at", updated_at)
        return instance

    def public_dict(self) -> dict:
        return {
            "status": "available",
            "revision_number": self.revision_number,
            "lifecycle_status": self.lifecycle_status,
            "updated_at": self.updated_at,
            "structured_profile_included": False,
        }

    def trusted_dict(self, *, include_structured_profile=False) -> dict:
        result = {**self.public_dict(), "profile_id": self.profile_id, "revision_id": self.revision_id}
        if include_structured_profile:
            result["structured_profile"] = _profile_from_bytes(self._structured_profile_json)
            result["structured_profile_included"] = True
        return result

    def __repr__(self) -> str:
        return f"CurrentProfileSummary(revision_number={self.revision_number}, content=<redacted>)"


@dataclass(frozen=True, repr=False, init=False)
class ProfileHistoryItem:
    profile_id: str = field(repr=False)
    revision_id: str = field(repr=False)
    revision_number: int
    revision_kind: str
    lifecycle_status: str
    created_at: str
    _structured_profile_json: bytes | None = field(default=None, repr=False)

    @classmethod
    def from_trusted(
        cls,
        *,
        profile_id: str,
        revision_id: str,
        revision_number: int,
        revision_kind: str,
        lifecycle_status: str,
        created_at: str,
        structured_profile_json: bytes | None = None,
    ):
        validate_profile_id(profile_id)
        validate_revision_id(revision_id)
        if type(revision_kind) is not str or revision_kind not in REVISION_KINDS | {"initial"}:
            _fail("internal_consistency_failure")
        _validate_result_common(revision_number, lifecycle_status, created_at)
        content = None if structured_profile_json is None else bytes(structured_profile_json)
        if content is not None:
            profile = _profile_from_bytes(content)
            if profile["identity"]["profile_id"] != profile_id:
                _fail("internal_consistency_failure")
        instance = object.__new__(cls)
        object.__setattr__(instance, "profile_id", profile_id)
        object.__setattr__(instance, "revision_id", revision_id)
        object.__setattr__(instance, "revision_number", revision_number)
        object.__setattr__(instance, "revision_kind", revision_kind)
        object.__setattr__(instance, "lifecycle_status", lifecycle_status)
        object.__setattr__(instance, "created_at", created_at)
        object.__setattr__(instance, "_structured_profile_json", content)
        return instance

    def public_dict(self) -> dict:
        return {
            "revision_number": self.revision_number,
            "revision_kind": self.revision_kind,
            "lifecycle_status": self.lifecycle_status,
            "created_at": self.created_at,
            "structured_profile_included": False,
        }

    def trusted_dict(self, *, include_structured_profile=False) -> dict:
        result = {**self.public_dict(), "profile_id": self.profile_id, "revision_id": self.revision_id}
        if include_structured_profile and self._structured_profile_json is not None:
            result["structured_profile"] = _profile_from_bytes(self._structured_profile_json)
            result["structured_profile_included"] = True
        return result

    def __repr__(self) -> str:
        return (
            "ProfileHistoryItem("
            f"revision_kind={self.revision_kind!r}, revision_number={self.revision_number}, "
            "content=<redacted>)"
        )


@dataclass(frozen=True)
class PurgeResult:
    outcome: str = "absent_or_completed"

    def __post_init__(self):
        if self.outcome != "absent_or_completed":
            _fail("internal_consistency_failure")

    def public_dict(self) -> dict:
        return {"outcome": "absent_or_completed"}


def _validate_result_common(revision_number, lifecycle_status, timestamp):
    if type(revision_number) is not int or revision_number < 1:
        _fail("internal_consistency_failure")
    if (
        type(lifecycle_status) is not str
        or lifecycle_status not in {"active", "archived", "deletion_requested"}
    ):
        _fail("internal_consistency_failure")
    if type(timestamp) is not str or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00", timestamp
    ):
        _fail("internal_consistency_failure")
    parsing_failed = False
    try:
        parsed = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        parsing_failed = True
        parsed = None
    if parsing_failed or parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        _fail("internal_consistency_failure")
    if parsed.microsecond != 0 or canonical_utc_timestamp(parsed) != timestamp:
        _fail("internal_consistency_failure")
