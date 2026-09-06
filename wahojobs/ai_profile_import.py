"""Durable one-free-import authority and atomic AI profile-create core.

This module has no browser route and opens no database.  Callers supply one
already-open mutation connection and a sealed intake grant issued by the
existing authenticated PB-OWN-1 authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
import sqlite3

from wahojobs.resumable_ai_profile_intake_schema import (
    attest_resumable_ai_profile_intake_schema,
)
from wahojobs.persistent_profiles import (
    AI_IMPORT_SOURCE_SCHEMA_VERSION,
    MIGRATION_010_CAPABILITIES,
    CreatePersistentProfileCommand,
    IdentityFreeCanonicalProfileV1,
    PersistentProfileDomainError,
    TrustedPrincipalContext,
    UserConfirmedAIImportSourceDraft,
    _create_canonical_profile_v2_draft,
    canonical_utc_timestamp,
)
from wahojobs.persistent_profiles_repository import (
    PersistentProfileRepository,
    TrustedProfileCreateLineage,
    _ai_profile_import_repository,
    capture_profile_create_lineage,
)
from wahojobs.profile_intake.contracts import (
    DocumentKind,
    LanguageValue,
    ProfileIntakeError,
)
from wahojobs.profile_intake.review_draft import REVIEW_DRAFT_SCHEMA_VERSION
from wahojobs.profile_intake.runtime import (
    PROFILE_INTAKE_DEFAULT_REVIEW_STEP,
    PROFILE_INTAKE_PURPOSE,
    EditableProfileReview,
    EditableEducationEntry,
    EditableUserFact,
    SafeDocumentBundleMetadata,
    SafeModelDiagnostics,
    TrustedProfileIntakeGrant,
    hydrate_profile_intake_checkpoint,
    education_entry_field_authorities,
    education_entry_values,
    managed_education_fact_indexes,
    profile_intake_checkpoint_review_step,
    review_value_for_form,
    serialize_profile_intake_checkpoint,
    update_editable_review,
    review_collection_entries,
    reviewed_display_name,
    valid_review_display_name,
)
from wahojobs.profiles.canonical import (
    PROFILE_SOURCE_RESUME,
    PROFILE_SOURCE_USER_CONFIRMATION,
    PROFILE_SOURCE_USER_CORRECTION,
    PROFILE_SOURCES,
    SCHEMA_VERSION as CANONICAL_PROFILE_V1,
    UNKNOWN,
    field_sources_for_profile,
)
from wahojobs.profiles.canonical_v2 import (
    CanonicalProfileV2Error,
    MAX_DYNAMIC_LABEL_LENGTH,
    _validate_string_list,
    add_user_confirmed_education_entries_v1,
    add_user_confirmed_preference_model_v1,
    add_user_confirmed_preference_model_v2,
    convert_v1_to_v2,
)
from wahojobs.profiles.education_entries import (
    EducationEntryContractError,
    canonicalize_education_entries_v1,
    project_education_entries_to_legacy,
)
from wahojobs.profiles.countries import normalize_country
from wahojobs.profiles.normalizer import signals_for_domains, skills_block
from wahojobs.profiles.preference_model import (
    ProfilePreferenceModelError,
    V2_SCHEMA_VERSION as PREFERENCE_V2_SCHEMA_VERSION,
    preference_model_to_legacy_preferences,
    validate_profile_preferences,
)


AI_PROFILE_IMPORT_ENTITLEMENT_CODE = "ai_profile_import_v1"
AI_PROFILE_IMPORT_RESERVATION_LEASE = timedelta(minutes=12)
AI_PROFILE_IMPORT_RESERVATION_RENEWAL_INTERVAL = timedelta(minutes=5)
AI_PROFILE_IMPORT_RESERVATION_GENERATION_MAX = timedelta(hours=2)
AI_PROFILE_INTAKE_CHECKPOINT_RETENTION = timedelta(days=7)
AI_PROFILE_INTAKE_CHECKPOINT_SCHEMA_VERSION = "profile_intake_checkpoint_v1"
AI_PROFILE_IMPORT_NORMALIZER_VERSION = "ai_profile_intake_v1"
AI_PROFILE_IMPORT_REVIEWER_VERSION = REVIEW_DRAFT_SCHEMA_VERSION
AI_PROFILE_IMPORT_REASON_CODE = "profile.ai_import"
AI_PROFILE_IMPORT_ACTOR_TYPE = "authenticated_user"

_ATTEMPT = re.compile(r"^aip_[0-9a-f]{32}$")
_RESERVATION = re.compile(r"^air_[0-9a-f]{32}$")
_CHECKPOINT = re.compile(r"^aic_[0-9a-f]{32}$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9._:-]{16,256}$")
_AUTHORITY_ISSUER = object()
_USER_LIST_FIELDS = frozenset(
    {
        "eligible_countries",
        "geographic_restrictions",
        "employment_types",
        "schedule",
        "target_opportunity_types",
        "work_preferences",
        "hard_constraints",
        "soft_preferences",
        "avoid_keywords",
        "excluded_domains",
        "accessibility_constraints",
    }
)
_RELEASE_CODES = frozenset(
    {"cancelled", "processing_failed", "draft_expired", "abandoned"}
)
_PUBLIC_CODES = frozenset(
    {
        "invalid_request",
        "idempotency_conflict",
        "entitlement_reserved",
        "entitlement_consumed",
        "reservation_expired",
        "reservation_mismatch",
        "attempt_released",
        "attempt_failed",
        "checkpoint_exists",
        "checkpoint_expired",
        "checkpoint_missing",
        "checkpoint_stale",
        "checkpoint_tampered",
        "profile_already_exists",
        "ownership_stale",
        "review_unresolved",
        "content_rejected",
        "temporary_contention",
        "schema_unavailable",
        "internal_failure",
    }
)


class AIProfileImportError(Exception):
    """Stable content-free failure for the durable import core."""

    __slots__ = ("code",)

    def __init__(self, code):
        self.code = (
            code
            if type(code) is str and code in _PUBLIC_CODES
            else "internal_failure"
        )
        super().__init__(self.code)

    def __repr__(self):
        return f"AIProfileImportError(code={self.code!r})"


@dataclass(frozen=True, slots=True, repr=False, init=False)
class AIProfileImportSourceMetadata:
    """Content-free bundle provenance shared by reservation and source row."""

    canonical_json: str = field(repr=False)
    _issuer: object = field(repr=False, compare=False)

    def __init__(self, *_args, **_kwargs):
        raise AIProfileImportError("invalid_request")

    @classmethod
    def from_runtime(cls, document, diagnostics):
        if type(document) is not SafeDocumentBundleMetadata or (
            diagnostics is not None
            and (
                type(diagnostics) is not tuple
                or any(type(item) is not SafeModelDiagnostics for item in diagnostics)
                or len(diagnostics) != len(document.documents)
                or any(type(item.document_kind) is not str for item in diagnostics)
                or any(
                    type(value) is not str
                    for item in diagnostics
                    for value in (
                        item.model,
                        item.prompt_version,
                        item.schema_version,
                    )
                )
                or sorted(item.document_kind for item in diagnostics)
                != sorted(item.origin for item in document.documents)
                or any(
                    item.success is not True or item.failure_code is not None
                    for item in diagnostics
                )
            )
        ):
            raise AIProfileImportError("invalid_request")
        ordered = tuple(
            sorted(
                document.documents,
                key=lambda item: 0 if item.origin == "resume" else 1,
            )
        )
        origins = [item.origin for item in ordered]
        models = sorted({item.model for item in diagnostics or ()})
        prompts = sorted({item.prompt_version for item in diagnostics or ()})
        schemas = sorted({item.schema_version for item in diagnostics or ()})
        payload = {
            "schema_version": AI_IMPORT_SOURCE_SCHEMA_VERSION,
            "bundle_origins": origins,
            "document_count": len(origins),
            "parser_versions": [
                f"{item.parser}:{item.parser_version}" for item in ordered
            ],
            "model": models[0] if len(models) == 1 else "unavailable" if not models else "multiple",
            "prompt_version": prompts[0] if len(prompts) == 1 else "unavailable" if not prompts else "multiple",
            "extraction_schema_version": schemas[0] if len(schemas) == 1 else "ai_profile_extraction_v1",
            "review_schema_version": REVIEW_DRAFT_SCHEMA_VERSION,
        }
        return cls._from_mapping(payload)

    @classmethod
    def _from_mapping(cls, payload):
        try:
            draft = UserConfirmedAIImportSourceDraft.from_metadata(
                payload,
                confirmed_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
            )
        except PersistentProfileDomainError:
            raise AIProfileImportError("content_rejected") from None
        instance = object.__new__(cls)
        object.__setattr__(instance, "canonical_json", draft.content)
        object.__setattr__(instance, "_issuer", _AUTHORITY_ISSUER)
        return instance

    def to_mapping(self):
        if getattr(self, "_issuer", None) is not _AUTHORITY_ISSUER:
            raise AIProfileImportError("invalid_request")
        try:
            payload = json.loads(self.canonical_json)
            draft = UserConfirmedAIImportSourceDraft.from_metadata(
                payload,
                confirmed_at=datetime(2000, 1, 1, tzinfo=timezone.utc),
            )
        except (
            json.JSONDecodeError,
            UnicodeError,
            TypeError,
            ValueError,
            PersistentProfileDomainError,
        ):
            raise AIProfileImportError("invalid_request") from None
        if not hmac.compare_digest(draft.content, self.canonical_json):
            raise AIProfileImportError("invalid_request")
        return payload

    @property
    def origins(self):
        return tuple(self.to_mapping()["bundle_origins"])

    def __repr__(self):
        return "AIProfileImportSourceMetadata(content=<redacted>)"

    def __reduce_ex__(self, _protocol):
        raise TypeError("ai_profile_import_source_metadata_not_serializable")


@dataclass(frozen=True, slots=True, repr=False)
class AIProfileImportReservationRequest:
    idempotency_key: str = field(repr=False)
    source_metadata: AIProfileImportSourceMetadata

    def __post_init__(self):
        if (
            type(self.idempotency_key) is not str
            or _IDEMPOTENCY.fullmatch(self.idempotency_key) is None
            or type(self.source_metadata) is not AIProfileImportSourceMetadata
            or getattr(self.source_metadata, "_issuer", None) is not _AUTHORITY_ISSUER
        ):
            raise AIProfileImportError("invalid_request")


@dataclass(frozen=True, slots=True, repr=False, init=False)
class AIProfileImportReservationAuthority:
    attempt_id: str = field(repr=False)
    reservation_id: str = field(repr=False)
    environment_namespace: str = field(repr=False)
    account_id: str = field(repr=False)
    principal_id: str = field(repr=False)
    request_fingerprint: str = field(repr=False)
    lease_expires_at: str
    _issuer: object = field(repr=False, compare=False)

    def __init__(self, *_args, **_kwargs):
        raise AIProfileImportError("invalid_request")

    @classmethod
    def _issue(cls, capability, **values):
        if capability is not _AUTHORITY_ISSUER:
            raise AIProfileImportError("invalid_request")
        instance = object.__new__(cls)
        for name, value in {**values, "_issuer": _AUTHORITY_ISSUER}.items():
            object.__setattr__(instance, name, value)
        if (
            _ATTEMPT.fullmatch(instance.attempt_id) is None
            or _RESERVATION.fullmatch(instance.reservation_id) is None
            or len(set(instance.attempt_id[4:])) == 1
            or len(set(instance.reservation_id[4:])) == 1
            or len(instance.request_fingerprint) != 64
        ):
            raise AIProfileImportError("internal_failure")
        return instance

    def __repr__(self):
        return "AIProfileImportReservationAuthority(<redacted>)"

    def __reduce_ex__(self, _protocol):
        raise TypeError("ai_profile_import_reservation_not_serializable")


@dataclass(frozen=True, slots=True)
class AIProfileImportReservationResult:
    state: str
    authority: AIProfileImportReservationAuthority
    replayed: bool


@dataclass(frozen=True, slots=True, repr=False, init=False)
class AIProfileIntakeCheckpointAuthority:
    checkpoint_id: str = field(repr=False)
    row_version: int
    expires_at: str
    reservation_generation: int
    _issuer: object = field(repr=False, compare=False)

    def __init__(self, *_args, **_kwargs):
        raise AIProfileImportError("invalid_request")

    @classmethod
    def _issue(cls, capability, *, checkpoint_id, row_version, expires_at, reservation_generation):
        if (
            capability is not _AUTHORITY_ISSUER
            or type(checkpoint_id) is not str
            or _CHECKPOINT.fullmatch(checkpoint_id) is None
            or len(set(checkpoint_id[4:])) == 1
            or type(row_version) is not int
            or row_version < 1
            or type(reservation_generation) is not int
            or reservation_generation < 1
            or type(expires_at) is not str
        ):
            raise AIProfileImportError("invalid_request")
        instance = object.__new__(cls)
        object.__setattr__(instance, "checkpoint_id", checkpoint_id)
        object.__setattr__(instance, "row_version", row_version)
        object.__setattr__(instance, "expires_at", expires_at)
        object.__setattr__(instance, "reservation_generation", reservation_generation)
        object.__setattr__(instance, "_issuer", _AUTHORITY_ISSUER)
        return instance

    def __repr__(self):
        return "AIProfileIntakeCheckpointAuthority(<redacted>)"

    def __reduce_ex__(self, _protocol):
        raise TypeError("ai_profile_intake_checkpoint_authority_not_serializable")


@dataclass(frozen=True, slots=True, repr=False)
class AIProfileIntakeCheckpointResult:
    checkpoint: AIProfileIntakeCheckpointAuthority
    reservation: AIProfileImportReservationAuthority
    review: EditableProfileReview = field(repr=False)
    source_metadata: AIProfileImportSourceMetadata = field(repr=False)
    review_step: str
    replayed_reservation: bool

    def __repr__(self):
        return (
            "AIProfileIntakeCheckpointResult("
            f"checkpoint={self.checkpoint!r}, reservation=<redacted>, "
            "review=<redacted>, source_metadata=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class AIProfileIntakeCheckpointSummary:
    checkpoint: AIProfileIntakeCheckpointAuthority
    created_at: str
    review_saved_at: str


@dataclass(frozen=True, slots=True)
class AIProfileImportPreflightResult:
    """Advisory, read-only eligibility state for the authenticated runtime."""

    state: str

    def __post_init__(self):
        if self.state not in {
            "eligible",
            "profile_exists",
            "entitlement_consumed",
            "entitlement_reserved",
            "checkpoint_available",
        }:
            raise AIProfileImportError("invalid_request")


_EDUCATION_AUTHORITY_FIELDS = frozenset(
    {
        "kind",
        "qualification",
        "field",
        "institution",
        "status",
        "completion_year",
    }
)
_EDUCATION_REVIEW_SOURCE_KINDS = frozenset(
    {
        PROFILE_SOURCE_RESUME,
        PROFILE_SOURCE_USER_CORRECTION,
        PROFILE_SOURCE_USER_CONFIRMATION,
    }
)


def _canonical_education_field_authorities(entries, value):
    """Validate the closed field authority plan aligned to canonical entries."""

    canonical_entries = canonicalize_education_entries_v1(entries)
    if type(value) not in {list, tuple} or len(value) != len(canonical_entries):
        raise AIProfileImportError("invalid_request")
    result = []
    for fields in value:
        if type(fields) is not dict or set(fields) != _EDUCATION_AUTHORITY_FIELDS:
            raise AIProfileImportError("invalid_request")
        canonical_fields = {}
        for field_name in sorted(_EDUCATION_AUTHORITY_FIELDS):
            detail = fields[field_name]
            if (
                type(detail) is not dict
                or set(detail) != {"source_kind", "explicit"}
                or detail["source_kind"] not in _EDUCATION_REVIEW_SOURCE_KINDS
                or detail["source_kind"] not in PROFILE_SOURCES
                or type(detail["explicit"]) is not bool
            ):
                raise AIProfileImportError("invalid_request")
            canonical_fields[field_name] = {
                "source_kind": detail["source_kind"],
                "explicit": detail["explicit"],
            }
        result.append(canonical_fields)
    return result


@dataclass(frozen=True, slots=True, repr=False, init=False)
class ConfirmedAIProfileImport:
    reviewed_profile: IdentityFreeCanonicalProfileV1 = field(repr=False)
    source_metadata: AIProfileImportSourceMetadata
    confirmation_fingerprint: str = field(repr=False)
    _preference_model_json: bytes = field(repr=False)
    _education_entries_json: bytes = field(repr=False)
    _education_field_authorities_json: bytes = field(repr=False)
    _unpaired_education_json: bytes = field(repr=False)
    _issuer: object = field(repr=False, compare=False)

    def __init__(self, *_args, **_kwargs):
        raise AIProfileImportError("invalid_request")

    @classmethod
    def _issue(
        cls,
        capability,
        reviewed_profile,
        source_metadata,
        fingerprint,
        preference_model,
        education_entries,
        education_field_authorities,
        unpaired_education,
    ):
        if (
            capability is not _AUTHORITY_ISSUER
            or type(reviewed_profile) is not IdentityFreeCanonicalProfileV1
            or type(source_metadata) is not AIProfileImportSourceMetadata
            or getattr(source_metadata, "_issuer", None) is not _AUTHORITY_ISSUER
            or type(fingerprint) is not str
            or len(fingerprint) != 64
        ):
            raise AIProfileImportError("invalid_request")
        try:
            preference_model = validate_profile_preferences(preference_model)
            preference_json = json.dumps(
                preference_model,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            canonical_entries = canonicalize_education_entries_v1(education_entries)
            education_json = json.dumps(
                canonical_entries,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            canonical_authorities = _canonical_education_field_authorities(
                canonical_entries,
                education_field_authorities,
            )
            education_authorities_json = json.dumps(
                canonical_authorities,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            projected = project_education_entries_to_legacy(
                canonical_entries,
                unpaired_education,
            )
            if projected != reviewed_profile.to_mapping()["education"]:
                raise AIProfileImportError("invalid_request")
            unpaired_json = json.dumps(
                unpaired_education,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        except (
            EducationEntryContractError,
            ProfilePreferenceModelError,
            TypeError,
            UnicodeError,
            ValueError,
        ):
            raise AIProfileImportError("invalid_request") from None
        instance = object.__new__(cls)
        object.__setattr__(instance, "reviewed_profile", reviewed_profile)
        object.__setattr__(instance, "source_metadata", source_metadata)
        object.__setattr__(instance, "confirmation_fingerprint", fingerprint)
        object.__setattr__(instance, "_preference_model_json", preference_json)
        object.__setattr__(instance, "_education_entries_json", education_json)
        object.__setattr__(
            instance,
            "_education_field_authorities_json",
            education_authorities_json,
        )
        object.__setattr__(instance, "_unpaired_education_json", unpaired_json)
        object.__setattr__(instance, "_issuer", _AUTHORITY_ISSUER)
        return instance

    def preference_model_for_service(self):
        if getattr(self, "_issuer", None) is not _AUTHORITY_ISSUER:
            raise AIProfileImportError("invalid_request")
        try:
            return validate_profile_preferences(
                json.loads(self._preference_model_json.decode("ascii"))
            )
        except (ProfilePreferenceModelError, UnicodeError, ValueError, TypeError):
            raise AIProfileImportError("invalid_request") from None

    def education_entries_for_service(self):
        if getattr(self, "_issuer", None) is not _AUTHORITY_ISSUER:
            raise AIProfileImportError("invalid_request")
        try:
            return canonicalize_education_entries_v1(
                json.loads(self._education_entries_json.decode("ascii"))
            )
        except (EducationEntryContractError, UnicodeError, ValueError, TypeError):
            raise AIProfileImportError("invalid_request") from None

    def education_field_authorities_for_service(self):
        if getattr(self, "_issuer", None) is not _AUTHORITY_ISSUER:
            raise AIProfileImportError("invalid_request")
        try:
            return _canonical_education_field_authorities(
                self.education_entries_for_service(),
                json.loads(self._education_field_authorities_json.decode("ascii")),
            )
        except (UnicodeError, ValueError, TypeError):
            raise AIProfileImportError("invalid_request") from None

    def unpaired_education_for_service(self):
        if getattr(self, "_issuer", None) is not _AUTHORITY_ISSUER:
            raise AIProfileImportError("invalid_request")
        try:
            value = json.loads(self._unpaired_education_json.decode("ascii"))
            project_education_entries_to_legacy(
                self.education_entries_for_service(),
                value,
            )
            return value
        except (EducationEntryContractError, UnicodeError, ValueError, TypeError):
            raise AIProfileImportError("invalid_request") from None

    def __repr__(self):
        return "ConfirmedAIProfileImport(content=<redacted>)"

    def __reduce_ex__(self, _protocol):
        raise TypeError("confirmed_ai_profile_import_not_serializable")


@dataclass(frozen=True, slots=True)
class AIProfileImportCommitResult:
    profile_id: str
    revision_id: str
    replayed: bool


class AIProfileImportService:
    """Caller-connection reservation and atomic commit authority."""

    __slots__ = ("_repository", "_token_hex", "_failure_injector")

    def __init__(self, *, repository=None, token_hex=None, failure_injector=None):
        self._repository = repository or _ai_profile_import_repository()
        self._token_hex = token_hex or secrets.token_hex
        self._failure_injector = failure_injector
        if (
            type(self._repository) is not PersistentProfileRepository
            or not self._repository._authorizes_ai_profile_import()
            or not callable(self._token_hex)
        ):
            raise AIProfileImportError("invalid_request")
        if failure_injector is not None and not callable(failure_injector):
            raise AIProfileImportError("invalid_request")

    def preflight(self, connection, grant, *, now):
        """Read eligibility without creating an entitlement or reservation."""

        now = _trusted_time(now)
        try:
            if (
                type(connection) is not sqlite3.Connection
                or connection.in_transaction
                or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
                or connection.execute("PRAGMA query_only").fetchone()[0] != 1
            ):
                raise AIProfileImportError("schema_unavailable")
            _require_m011(connection)
            authority = _grant_authority(connection, grant, now)
            if connection.execute(
                "SELECT 1 FROM product_profiles WHERE principal_id=? AND environment_namespace=?",
                (authority[3], authority[2]),
            ).fetchone() is not None:
                return AIProfileImportPreflightResult("profile_exists")
            checkpoint = connection.execute(
                "SELECT expires_at FROM ai_profile_intake_checkpoints "
                "WHERE environment_namespace=? AND account_id=? AND state='active'",
                (authority[2], authority[0]),
            ).fetchone()
            if checkpoint is not None and checkpoint[0] > canonical_utc_timestamp(now):
                return AIProfileImportPreflightResult("checkpoint_available")
            entitlement = connection.execute(
                "SELECT state,lease_expires_at FROM ai_profile_import_entitlements "
                "WHERE environment_namespace=? AND account_id=? AND entitlement_code=?",
                (authority[2], authority[0], AI_PROFILE_IMPORT_ENTITLEMENT_CODE),
            ).fetchone()
            if entitlement is None or entitlement[0] == "available":
                return AIProfileImportPreflightResult("eligible")
            if entitlement[0] == "consumed":
                return AIProfileImportPreflightResult("entitlement_consumed")
            if (
                entitlement[0] == "reserved"
                and entitlement[1] > canonical_utc_timestamp(now)
            ):
                return AIProfileImportPreflightResult("entitlement_reserved")
            if entitlement[0] == "reserved":
                return AIProfileImportPreflightResult("eligible")
            raise AIProfileImportError("schema_unavailable")
        except AIProfileImportError:
            raise
        except sqlite3.Error as exc:
            _detach(exc)
            raise AIProfileImportError("schema_unavailable") from None

    def inspect_checkpoint(self, connection, grant, *, now):
        """Return only bounded resume metadata under current ownership authority."""

        now = _trusted_time(now)
        try:
            if (
                type(connection) is not sqlite3.Connection
                or connection.in_transaction
                or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
                or connection.execute("PRAGMA query_only").fetchone()[0] != 1
            ):
                raise AIProfileImportError("schema_unavailable")
            _require_m011(connection)
            authority = _grant_authority(connection, grant, now)
            _require_profile_absent(connection, authority)
            row = connection.execute(
                "SELECT checkpoint_id,row_version,reservation_generation,expires_at,"
                "created_at,review_saved_at FROM ai_profile_intake_checkpoints "
                "WHERE environment_namespace=? AND account_id=? AND state='active'",
                (authority[2], authority[0]),
            ).fetchone()
            if row is None or row[3] <= canonical_utc_timestamp(now):
                return None
            full = _checkpoint_row(connection, row[0])
            _require_checkpoint_match(full, authority, expected_version=row[1])
            _validated_checkpoint_content(full)
            return AIProfileIntakeCheckpointSummary(
                _checkpoint_authority(row[0], row[1], row[3], row[2]),
                row[4],
                row[5],
            )
        except AIProfileImportError:
            raise
        except sqlite3.Error as exc:
            _detach(exc)
            raise AIProfileImportError("schema_unavailable") from None

    def reserve(self, connection, grant, request, *, now):
        if type(request) is not AIProfileImportReservationRequest:
            raise AIProfileImportError("invalid_request")
        now = _trusted_time(now)
        request.source_metadata.to_mapping()
        result = None

        def operation():
            nonlocal result
            _require_m011(connection)
            authority = _grant_authority(connection, grant, now)
            _require_profile_absent(connection, authority)
            result = _reserve_in_transaction(
                connection,
                authority,
                request,
                now,
                self._token_hex,
            )

        _atomic(connection, operation)
        if isinstance(result, AIProfileImportError):
            raise result
        return result

    def create_checkpoint(self, connection, grant, review, source_metadata, *, now):
        """Persist one minimized review and acquire its first short reservation."""

        if (
            type(review) is not EditableProfileReview
            or type(source_metadata) is not AIProfileImportSourceMetadata
            or getattr(source_metadata, "_issuer", None) is not _AUTHORITY_ISSUER
        ):
            raise AIProfileImportError("invalid_request")
        try:
            payload_json = serialize_profile_intake_checkpoint(review)
        except ProfileIntakeError as exc:
            raise AIProfileImportError(
                "content_rejected" if exc.code != "checkpoint_too_large" else "invalid_request"
            ) from None
        if tuple(source.document_kind.value for source in review.sources) != source_metadata.origins:
            raise AIProfileImportError("invalid_request")
        now = _trusted_time(now)
        outcome = None

        def operation():
            nonlocal outcome
            _require_m011(connection)
            authority = _grant_authority(connection, grant, now)
            _require_profile_absent(connection, authority)
            existing = connection.execute(
                "SELECT checkpoint_id,expires_at,reservation_generation FROM ai_profile_intake_checkpoints "
                "WHERE environment_namespace=? AND account_id=?",
                (authority[2], authority[0]),
            ).fetchone()
            if existing is not None:
                if existing[1] > canonical_utc_timestamp(now):
                    raise AIProfileImportError("checkpoint_exists")
                _discard_checkpoint_in_transaction(
                    connection, authority, existing[0], existing[2], now, expired=True
                )
            checkpoint_id = _new_id(
                connection,
                "aic",
                "ai_profile_intake_checkpoints",
                "checkpoint_id",
                self._token_hex,
            )
            timestamp = canonical_utc_timestamp(now)
            expires_at = canonical_utc_timestamp(now + AI_PROFILE_INTAKE_CHECKPOINT_RETENTION)
            digest = hashlib.sha256(payload_json.encode("ascii")).hexdigest()
            lineage = authority[4]
            connection.execute(
                "INSERT INTO ai_profile_intake_checkpoints "
                "(checkpoint_id,environment_namespace,account_id,principal_id,checkpoint_schema_version,"
                "review_schema_version,state,row_version,review_payload_json,review_payload_sha256,"
                "source_metadata_json,binding_id,binding_version,latest_event_version,latest_event_id,"
                "lineage_sha256,reservation_generation,created_at,review_saved_at,updated_at,expires_at) "
                "VALUES (?,?,?,?,?,?,'active',1,?,?,?,?,?,?,?,?,1,?,?,?,?)",
                (
                    checkpoint_id,
                    authority[2],
                    authority[0],
                    authority[3],
                    AI_PROFILE_INTAKE_CHECKPOINT_SCHEMA_VERSION,
                    REVIEW_DRAFT_SCHEMA_VERSION,
                    payload_json,
                    digest,
                    source_metadata.canonical_json,
                    lineage.binding_id,
                    lineage.binding_version,
                    lineage.latest_event_version,
                    lineage.latest_event_id,
                    lineage.lineage_sha256,
                    timestamp,
                    timestamp,
                    timestamp,
                    expires_at,
                ),
            )
            request = _checkpoint_reservation_request(checkpoint_id, 1, source_metadata)
            reserved = _reserve_in_transaction(
                connection, authority, request, now, self._token_hex
            )
            if isinstance(reserved, AIProfileImportError):
                raise reserved
            outcome = AIProfileIntakeCheckpointResult(
                _checkpoint_authority(checkpoint_id, 1, expires_at, 1),
                reserved.authority,
                review,
                source_metadata,
                PROFILE_INTAKE_DEFAULT_REVIEW_STEP,
                reserved.replayed,
            )

        _atomic(connection, operation)
        return outcome

    def resume_checkpoint(self, connection, grant, checkpoint_id, *, expected_version, now):
        """Rehydrate a checkpoint and reacquire authority without extraction."""

        if (
            type(checkpoint_id) is not str
            or _CHECKPOINT.fullmatch(checkpoint_id) is None
            or type(expected_version) is not int
            or expected_version < 1
        ):
            raise AIProfileImportError("invalid_request")
        now = _trusted_time(now)
        outcome = None

        def operation():
            nonlocal outcome
            _require_m011(connection)
            authority = _grant_authority(connection, grant, now)
            row = _checkpoint_row(connection, checkpoint_id)
            _require_checkpoint_match(row, authority, expected_version=expected_version)
            if row[7] <= canonical_utc_timestamp(now):
                _discard_checkpoint_in_transaction(
                    connection, authority, row[0], row[6], now, expired=True
                )
                outcome = AIProfileImportError("checkpoint_expired")
                return
            review, source_metadata, review_step = _validated_checkpoint_content(row)
            _require_profile_absent(connection, authority)
            generation = row[6]
            version = row[2]
            reserved = None
            for _attempt in range(2):
                request = _checkpoint_reservation_request(
                    checkpoint_id, generation, source_metadata
                )
                reserved = _reserve_in_transaction(
                    connection, authority, request, now, self._token_hex
                )
                if not isinstance(reserved, AIProfileImportError):
                    break
                if reserved.code not in {
                    "reservation_expired", "attempt_released", "attempt_failed"
                }:
                    raise reserved
                generation += 1
                version += 1
                timestamp = canonical_utc_timestamp(now)
                connection.execute(
                    "UPDATE ai_profile_intake_checkpoints SET row_version=?,reservation_generation=?,updated_at=? "
                    "WHERE checkpoint_id=? AND row_version=?",
                    (version, generation, timestamp, checkpoint_id, version - 1),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise AIProfileImportError("checkpoint_stale")
            if isinstance(reserved, AIProfileImportError):
                raise reserved
            outcome = AIProfileIntakeCheckpointResult(
                _checkpoint_authority(checkpoint_id, version, row[7], generation),
                reserved.authority,
                review,
                source_metadata,
                review_step,
                reserved.replayed,
            )

        _atomic(connection, operation)
        if isinstance(outcome, AIProfileImportError):
            raise outcome
        return outcome

    def update_checkpoint(
        self,
        connection,
        grant,
        checkpoint,
        review,
        *,
        review_step=PROFILE_INTAKE_DEFAULT_REVIEW_STEP,
        now,
    ):
        """Optimistically save one validated review change and refresh seven days."""

        if (
            type(checkpoint) is not AIProfileIntakeCheckpointAuthority
            or getattr(checkpoint, "_issuer", None) is not _AUTHORITY_ISSUER
            or type(review) is not EditableProfileReview
        ):
            raise AIProfileImportError("invalid_request")
        try:
            payload_json = serialize_profile_intake_checkpoint(
                review,
                review_step=review_step,
            )
        except ProfileIntakeError:
            raise AIProfileImportError("content_rejected") from None
        now = _trusted_time(now)
        outcome = None

        def operation():
            nonlocal outcome
            _require_m011(connection)
            authority = _grant_authority(connection, grant, now)
            row = _checkpoint_row(connection, checkpoint.checkpoint_id)
            _require_checkpoint_match(row, authority, expected_version=checkpoint.row_version)
            if row[7] <= canonical_utc_timestamp(now):
                raise AIProfileImportError("checkpoint_expired")
            _stored_review, source_metadata, _stored_step = _validated_checkpoint_content(row)
            if tuple(source.document_kind.value for source in review.sources) != source_metadata.origins:
                raise AIProfileImportError("checkpoint_tampered")
            if hmac.compare_digest(row[3], payload_json):
                outcome = _checkpoint_authority(
                    row[0], row[2], row[7], row[6]
                )
                return
            timestamp = canonical_utc_timestamp(now)
            expires_at = canonical_utc_timestamp(now + AI_PROFILE_INTAKE_CHECKPOINT_RETENTION)
            next_version = row[2] + 1
            connection.execute(
                "UPDATE ai_profile_intake_checkpoints SET row_version=?,review_payload_json=?,"
                "review_payload_sha256=?,review_saved_at=?,updated_at=?,expires_at=? "
                "WHERE checkpoint_id=? AND row_version=?",
                (
                    next_version,
                    payload_json,
                    hashlib.sha256(payload_json.encode("ascii")).hexdigest(),
                    timestamp,
                    timestamp,
                    expires_at,
                    checkpoint.checkpoint_id,
                    row[2],
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise AIProfileImportError("checkpoint_stale")
            outcome = _checkpoint_authority(
                row[0], next_version, expires_at, row[6]
            )

        _atomic(connection, operation)
        return outcome

    def renew_checkpoint_reservation(
        self, connection, grant, checkpoint, reservation, *, now
    ):
        """Extend one live short lease, rate-limited and capped per generation."""

        if type(checkpoint) is not AIProfileIntakeCheckpointAuthority:
            raise AIProfileImportError("invalid_request")
        now = _trusted_time(now)
        outcome = None

        def operation():
            nonlocal outcome
            _require_m011(connection)
            authority = _grant_authority(connection, grant, now)
            row = _checkpoint_row(connection, checkpoint.checkpoint_id)
            _require_checkpoint_match(row, authority, expected_version=checkpoint.row_version)
            _validated_checkpoint_content(row)
            _require_reservation_binding(reservation, authority)
            attempt = _attempt_row(connection, reservation.attempt_id)
            _require_attempt_match(attempt, reservation, authority)
            timestamp = canonical_utc_timestamp(now)
            if attempt[2] != "reserved" or attempt[7] <= timestamp:
                if attempt[2] == "reserved":
                    _expire_attempt(connection, attempt[0], attempt[1], authority, now)
                outcome = AIProfileImportError("reservation_expired")
                return
            created_at = datetime.fromisoformat(attempt[18])
            updated_at = datetime.fromisoformat(attempt[19])
            if now - updated_at < AI_PROFILE_IMPORT_RESERVATION_RENEWAL_INTERVAL:
                outcome = AIProfileImportReservationResult(
                    "reserved",
                    _reservation_authority(
                        attempt[0], attempt[1], authority, attempt[12], attempt[7]
                    ),
                    True,
                )
                return
            generation_end = created_at + AI_PROFILE_IMPORT_RESERVATION_GENERATION_MAX
            lease_end = min(now + AI_PROFILE_IMPORT_RESERVATION_LEASE, generation_end)
            if lease_end <= datetime.fromisoformat(attempt[7]):
                outcome = AIProfileImportReservationResult(
                    "reserved",
                    _reservation_authority(
                        attempt[0], attempt[1], authority, attempt[12], attempt[7]
                    ),
                    True,
                )
                return
            lease = canonical_utc_timestamp(lease_end)
            connection.execute(
                "UPDATE ai_profile_import_attempts SET lease_expires_at=?,updated_at=? "
                "WHERE attempt_id=? AND state='reserved' AND lease_expires_at=?",
                (lease, timestamp, attempt[0], attempt[7]),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise AIProfileImportError("reservation_mismatch")
            connection.execute(
                "UPDATE ai_profile_import_entitlements SET lease_expires_at=?,updated_at=? "
                "WHERE environment_namespace=? AND account_id=? AND entitlement_code=? "
                "AND state='reserved' AND attempt_id=? AND reservation_id=?",
                (
                    lease,
                    timestamp,
                    authority[2],
                    authority[0],
                    AI_PROFILE_IMPORT_ENTITLEMENT_CODE,
                    attempt[0],
                    attempt[1],
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise AIProfileImportError("reservation_mismatch")
            outcome = AIProfileImportReservationResult(
                "reserved",
                _reservation_authority(
                    attempt[0], attempt[1], authority, attempt[12], lease
                ),
                False,
            )

        _atomic(connection, operation)
        if isinstance(outcome, AIProfileImportError):
            raise outcome
        return outcome

    def discard_checkpoint(self, connection, grant, checkpoint, *, now):
        if type(checkpoint) is not AIProfileIntakeCheckpointAuthority:
            raise AIProfileImportError("invalid_request")
        now = _trusted_time(now)

        def operation():
            _require_m011(connection)
            authority = _grant_authority(connection, grant, now)
            row = _checkpoint_row(connection, checkpoint.checkpoint_id)
            _require_checkpoint_match(row, authority, expected_version=checkpoint.row_version)
            _discard_checkpoint_in_transaction(
                connection, authority, row[0], row[6], now, expired=False
            )

        _atomic(connection, operation)
        return {"discarded": True}

    def release(self, connection, grant, reservation, *, outcome_code, now):
        if type(outcome_code) is not str or outcome_code not in _RELEASE_CODES:
            raise AIProfileImportError("invalid_request")
        now = _trusted_time(now)
        result = None

        def operation():
            nonlocal result
            _require_m011(connection)
            authority = _grant_authority(connection, grant, now)
            _require_reservation_binding(reservation, authority)
            row = _attempt_row(connection, reservation.attempt_id)
            _require_attempt_match(row, reservation, authority)
            if row[2] == "released" and row[9] == outcome_code:
                result = True
                return
            if row[2] != "reserved":
                raise AIProfileImportError("reservation_mismatch")
            timestamp = canonical_utc_timestamp(now)
            if row[7] <= timestamp:
                _expire_attempt(
                    connection,
                    reservation.attempt_id,
                    reservation.reservation_id,
                    authority,
                    now,
                )
                result = AIProfileImportError("reservation_expired")
                return
            connection.execute(
                "UPDATE ai_profile_import_attempts SET state='released',result_code=?,updated_at=?,completed_at=? WHERE attempt_id=? AND state='reserved'",
                (outcome_code, timestamp, timestamp, reservation.attempt_id),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise AIProfileImportError("reservation_mismatch")
            connection.execute(
                "UPDATE ai_profile_import_entitlements SET state='available',reservation_id=NULL,attempt_id=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE environment_namespace=? AND account_id=? AND entitlement_code=? AND state='reserved' AND attempt_id=? AND reservation_id=?",
                (timestamp, authority[2], authority[0], AI_PROFILE_IMPORT_ENTITLEMENT_CODE, reservation.attempt_id, reservation.reservation_id),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise AIProfileImportError("reservation_mismatch")
            result = False

        _atomic(connection, operation)
        if isinstance(result, AIProfileImportError):
            raise result
        return {"released": True, "replayed": bool(result)}

    def commit_confirmed_ai_profile_import(
        self,
        connection,
        grant,
        reservation,
        confirmed,
        *,
        checkpoint,
        now,
    ):
        if (
            type(confirmed) is not ConfirmedAIProfileImport
            or getattr(confirmed, "_issuer", None) is not _AUTHORITY_ISSUER
            or type(checkpoint) is not AIProfileIntakeCheckpointAuthority
            or getattr(checkpoint, "_issuer", None) is not _AUTHORITY_ISSUER
        ):
            raise AIProfileImportError("invalid_request")
        try:
            preference_model_json = json.dumps(
                confirmed.preference_model_for_service(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            education_entries_json = json.dumps(
                confirmed.education_entries_for_service(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            education_field_authorities_json = json.dumps(
                confirmed.education_field_authorities_for_service(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            unpaired_education_json = json.dumps(
                confirmed.unpaired_education_for_service(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            expected_confirmation = hashlib.sha256(
                b"ai-profile-confirmation-v1\x00"
                + confirmed.reviewed_profile.canonical_bytes
                + b"\x00"
                + preference_model_json
                + b"\x00"
                + education_entries_json
                + b"\x00"
                + education_field_authorities_json
                + b"\x00"
                + unpaired_education_json
                + b"\x00"
                + confirmed.source_metadata.canonical_json.encode("ascii")
            ).hexdigest()
            confirmed.source_metadata.to_mapping()
        except (AttributeError, TypeError, UnicodeError):
            raise AIProfileImportError("invalid_request") from None
        if type(confirmed.confirmation_fingerprint) is not str or not hmac.compare_digest(
            confirmed.confirmation_fingerprint,
            expected_confirmation,
        ):
            raise AIProfileImportError("invalid_request")
        now = _trusted_time(now)
        prepared_authority = _grant_authority(connection, grant, now)
        _require_reservation_binding(reservation, prepared_authority)
        command = _create_command(
            confirmed,
            prepared_authority,
            reservation,
            now,
        )
        outcome = None

        def operation():
            nonlocal outcome
            _require_m011(connection)
            authority = _grant_authority(connection, grant, now)
            if authority != prepared_authority:
                raise AIProfileImportError("ownership_stale")
            _require_reservation_binding(reservation, authority)
            row = _attempt_row(connection, reservation.attempt_id)
            _require_attempt_match(row, reservation, authority)
            if row[2] == "succeeded":
                if not hmac.compare_digest(row[8] or "", confirmed.confirmation_fingerprint):
                    raise AIProfileImportError("idempotency_conflict")
                outcome = AIProfileImportCommitResult(row[10], row[11], True)
                return
            if row[2] != "reserved":
                raise AIProfileImportError("reservation_mismatch")
            checkpoint_row = _checkpoint_row(connection, checkpoint.checkpoint_id)
            _require_checkpoint_match(
                checkpoint_row,
                authority,
                expected_version=checkpoint.row_version,
            )
            if checkpoint_row[7] <= canonical_utc_timestamp(now):
                raise AIProfileImportError("checkpoint_expired")
            if not hmac.compare_digest(
                checkpoint_row[5], confirmed.source_metadata.canonical_json
            ):
                raise AIProfileImportError("checkpoint_tampered")
            _validated_checkpoint_content(checkpoint_row)
            timestamp = canonical_utc_timestamp(now)
            if row[7] <= timestamp:
                _expire_attempt(connection, row[0], row[1], authority, now)
                outcome = AIProfileImportError("reservation_expired")
                return
            if not hmac.compare_digest(row[6], confirmed.source_metadata.canonical_json):
                raise AIProfileImportError("reservation_mismatch")
            _hook(self._failure_injector, "before_profile_create")
            try:
                created = self._repository.create_account_native(
                    connection,
                    command,
                    account_lineage=authority[4],
                )
            except PersistentProfileDomainError as exc:
                if exc.reason_code != "profile_already_exists":
                    raise
                connection.execute(
                    "UPDATE ai_profile_import_attempts SET state='failed',result_code='profile_already_exists',updated_at=?,completed_at=? "
                    "WHERE attempt_id=? AND state='reserved'",
                    (timestamp, timestamp, reservation.attempt_id),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise AIProfileImportError("internal_failure")
                connection.execute(
                    "UPDATE ai_profile_import_entitlements SET state='available',reservation_id=NULL,attempt_id=NULL,lease_expires_at=NULL,updated_at=? "
                    "WHERE environment_namespace=? AND account_id=? AND entitlement_code=? AND state='reserved' AND attempt_id=?",
                    (timestamp, authority[2], authority[0], AI_PROFILE_IMPORT_ENTITLEMENT_CODE, reservation.attempt_id),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise AIProfileImportError("internal_failure")
                connection.execute(
                    "DELETE FROM ai_profile_intake_checkpoints WHERE checkpoint_id=? AND row_version=?",
                    (checkpoint.checkpoint_id, checkpoint.row_version),
                )
                if connection.execute("SELECT changes()").fetchone()[0] != 1:
                    raise AIProfileImportError("checkpoint_stale")
                outcome = AIProfileImportError("profile_already_exists")
                return
            _hook(self._failure_injector, "after_profile_create")
            _hook(self._failure_injector, "during_attempt_result_update")
            connection.execute(
                "UPDATE ai_profile_import_attempts SET state='succeeded',result_code='success',confirmation_fingerprint=?,"
                "result_profile_id=?,result_revision_id=?,updated_at=?,completed_at=? WHERE attempt_id=? AND state='reserved'",
                (
                    confirmed.confirmation_fingerprint,
                    created.profile_id,
                    created.revision_id,
                    timestamp,
                    timestamp,
                    reservation.attempt_id,
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise AIProfileImportError("internal_failure")
            connection.execute(
                "UPDATE ai_profile_import_entitlements SET state='consumed',lease_expires_at=NULL,consumed_at=?,updated_at=? "
                "WHERE environment_namespace=? AND account_id=? AND entitlement_code=? AND state='reserved' AND attempt_id=? AND reservation_id=?",
                (timestamp, timestamp, authority[2], authority[0], AI_PROFILE_IMPORT_ENTITLEMENT_CODE, reservation.attempt_id, reservation.reservation_id),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise AIProfileImportError("internal_failure")
            connection.execute(
                "DELETE FROM ai_profile_intake_checkpoints WHERE checkpoint_id=? AND row_version=?",
                (checkpoint.checkpoint_id, checkpoint.row_version),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise AIProfileImportError("checkpoint_stale")
            _hook(self._failure_injector, "after_entitlement_transition")
            outcome = AIProfileImportCommitResult(created.profile_id, created.revision_id, False)

        _atomic(connection, operation, before_commit=lambda: _hook(self._failure_injector, "before_commit"))
        if isinstance(outcome, AIProfileImportError):
            raise outcome
        return outcome


def prepare_confirmed_ai_profile_import(review, source_metadata):
    """Purely map a fully resolved temporary review to create-once material."""

    if type(review) is not EditableProfileReview or (
        type(source_metadata) is not AIProfileImportSourceMetadata
        or getattr(source_metadata, "_issuer", None) is not _AUTHORITY_ISSUER
    ):
        raise AIProfileImportError("invalid_request")
    try:
        if (
            review.schema_version != REVIEW_DRAFT_SCHEMA_VERSION
            or tuple(source.document_kind.value for source in review.sources)
            != source_metadata.origins
            or type(review.user_facts) is not tuple
            or any(type(fact) is not EditableUserFact for fact in review.user_facts)
            or type(review.education_entries) is not tuple
            or any(
                type(entry) is not EditableEducationEntry
                for entry in review.education_entries
            )
        ):
            raise AIProfileImportError("invalid_request")
        accepted = []
        conflict_accepts = {}
        paired_education_indexes = managed_education_fact_indexes(review)
        for fact_index, fact in enumerate(review.facts):
            if fact.decision == "pending":
                raise AIProfileImportError("review_unresolved")
            included = fact.decision in {"keep", "accept"}
            if fact.conflict_group is not None and included:
                conflict_accepts[fact.conflict_group] = (
                    conflict_accepts.get(fact.conflict_group, 0) + 1
                )
            if included and fact_index not in paired_education_indexes:
                accepted.append(fact)
        if any(count > 1 for count in conflict_accepts.values()):
            raise AIProfileImportError("review_unresolved")
        user_inputs = dict(review.user_inputs)
        if len(user_inputs) != len(review.user_inputs):
            raise AIProfileImportError("content_rejected")
        review = update_editable_review(
            review,
            tuple(review_value_for_form(fact.value) for fact in review.facts),
            tuple(fact.decision for fact in review.facts),
            user_inputs,
        )
        paired_education_indexes = managed_education_fact_indexes(review)
        accepted = [
            fact for index, fact in enumerate(review.facts)
            if fact.decision in {"keep", "accept"}
            and index not in paired_education_indexes
        ]
        accepted_user_facts = [
            fact for fact in review.user_facts if fact.decision == "keep"
        ]
        education_entries = canonicalize_education_entries_v1(
            [
                item["value"]
                for item in education_entry_values(review)
                if item["decision"] == "keep"
            ]
        )
        education_field_authority = education_entry_field_authorities(review)
    except AIProfileImportError:
        raise
    except (
        EducationEntryContractError,
        ProfileIntakeError,
        AttributeError,
        TypeError,
        ValueError,
    ):
        raise AIProfileImportError("content_rejected") from None
    preference_model = review.preference_model
    canonical, unpaired_education = _confirmed_review_v1(
        review,
        tuple(accepted),
        tuple(accepted_user_facts),
        education_entries,
        preference_model,
    )
    try:
        reviewed = IdentityFreeCanonicalProfileV1.from_mapping(canonical)
    except (PersistentProfileDomainError, CanonicalProfileV2Error, TypeError, ValueError):
        raise AIProfileImportError("content_rejected") from None
    fingerprint = hashlib.sha256(
        b"ai-profile-confirmation-v1\x00"
        + reviewed.canonical_bytes
        + b"\x00"
        + json.dumps(
            preference_model,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\x00"
        + json.dumps(
            education_entries,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\x00"
        + json.dumps(
            education_field_authority,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\x00"
        + json.dumps(
            unpaired_education,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\x00"
        + source_metadata.canonical_json.encode("ascii")
    ).hexdigest()
    confirmed = ConfirmedAIProfileImport._issue(
        _AUTHORITY_ISSUER,
        reviewed,
        source_metadata,
        fingerprint,
        preference_model,
        education_entries,
        education_field_authority,
        unpaired_education,
    )
    # Validate the actual proposed payload with the same builder used at commit,
    # before starting an atomic save or consuming an entitlement. Only its future
    # durable identity is provisional; no candidate content is substituted.
    from wahojobs.persistent_profiles import generate_profile_id
    try:
        _confirmed_profile_v2(confirmed, generate_profile_id())
    except CanonicalProfileV2Error:
        raise AIProfileImportError("content_rejected") from None
    return confirmed


def actionable_review_validation_issue(review):
    """Locate supported editable failures using the authoritative validators."""
    for index, fact in enumerate(review.facts):
        if fact.decision not in {"keep", "accept"}:
            continue
        if fact.field_path in {"location.country", "location.residence"}:
            try:
                normalize_country(fact.value, allow_missing=True)
            except (TypeError, ValueError):
                return {"kind": "country", "index": index}
    for index, entry in enumerate(review_collection_entries(review, "skills")):
        if entry["decision"] != "keep":
            continue
        errors = []
        _validate_string_list([entry["value"]], errors)
        if errors:
            return {"kind": "skill", "index": index, "limit": MAX_DYNAMIC_LABEL_LENGTH,
                    "reason": "length" if len(entry["value"]) > MAX_DYNAMIC_LABEL_LENGTH else "characters"}
    return None


def _confirmed_review_v1(
    review,
    facts,
    user_facts,
    education_entries,
    preference_model,
):
    values = {}
    for fact in (*facts, *user_facts):
        values.setdefault(fact.field_path, []).append(fact.value)
    display_name = reviewed_display_name(review)
    if not valid_review_display_name(display_name):
        raise AIProfileImportError("review_unresolved")
    inputs = dict(review.user_inputs)
    remaining_missing = [
        name for name in review.missing_user_fields if not inputs.get(name)
    ]
    languages = []
    for value in values.get("languages", []):
        if type(value) is not LanguageValue:
            raise AIProfileImportError("content_rejected")
        languages.append(
            {
                "language": value.language,
                "proficiency": value.proficiency or UNKNOWN,
                "locale": value.locale or "",
                "evidence": [],
                "confidence": "high",
                "proficiency_explicit": value.proficiency is not None,
                "provenance": PROFILE_SOURCE_USER_CONFIRMATION,
            }
        )
    languages.sort(key=lambda item: (item["language"].casefold(), item["locale"].casefold()))
    country = _country(_singleton(values, "location.country", ""))
    residence = _country(_singleton(values, "location.residence", "")) or country
    eligible = [_country(item) for item in _user_list(inputs, "eligible_countries")]
    legacy_preferences = preference_model_to_legacy_preferences(preference_model)
    remote = legacy_preferences["remote"]
    targets = list(legacy_preferences["target_opportunity_types"])
    domains = _unique(values.get("experience.professional_domains", []))
    skills = _unique(values.get("skills.normalized", []))
    signals = signals_for_domains(domains, skills, languages)
    years = _singleton(values, "experience.total_years", None)
    if years is not None and (type(years) not in {int, float} or float(years) != int(years)):
        raise AIProfileImportError("content_rejected")
    years = None if years is None else int(years)
    unpaired_education = {}
    education_paths = {
        "education.education_level": "education_level",
        "education.degrees": "degrees",
        "education.fields_or_domains": "fields_or_domains",
        "education.institutions": "institutions",
        "education.graduation_years": "graduation_years",
        "education.completion_status": "completion_status",
    }
    for path, name in education_paths.items():
        if path not in values:
            continue
        if name in {"education_level", "completion_status"}:
            unpaired_education[name] = _singleton(values, path, None)
        elif name == "graduation_years":
            # The extraction contract represents completion years as integers.
            # Leave their validation to the existing education contract.
            unpaired_education[name] = list(values[path])
        else:
            unpaired_education[name] = _unique(values[path])
    try:
        education = project_education_entries_to_legacy(
            education_entries,
            unpaired_education,
        )
    except EducationEntryContractError:
        raise AIProfileImportError("content_rejected") from None
    canonical = {
        "schema_version": CANONICAL_PROFILE_V1,
        "identity": {"display_name": display_name, "source_inputs": [{"type": "ai_profile_import"}]},
        "languages": languages,
        "location": {
            "country": country,
            "region": _singleton(values, "location.region", ""),
            "city": _singleton(values, "location.city", ""),
            "timezone": "",
            "residence": residence,
            "work_authorization": inputs.get("work_authorization") or UNKNOWN,
            "eligible_countries": eligible,
            "remote_eligibility": "explicit" if remote else UNKNOWN,
            "restrictions": _user_list(inputs, "geographic_restrictions"),
            "geographic_work_restrictions": _user_list(inputs, "geographic_restrictions"),
        },
        "education": education,
        "credentials": {
            "certifications": _unique(values.get("credentials.certifications", [])),
            "licenses": _unique(values.get("credentials.licenses", [])),
            "jurisdictions": _unique(values.get("credentials.jurisdictions", [])),
            "security_clearances": _unique(values.get("credentials.security_clearances", [])),
            "credential_status": _singleton(values, "credentials.credential_status", UNKNOWN),
        },
        "experience": {
            "total_years": years,
            "years_by_domain": {},
            "seniority": _singleton(values, "experience.seniority", UNKNOWN),
            "recent_roles": _unique(values.get("experience.recent_roles", [])),
            "occupational_families": _unique(values.get("experience.occupational_families", [])),
            "job_titles": _unique(values.get("experience.job_titles", [])),
            "professional_domains": domains,
            "industries": _unique(values.get("experience.industries", [])),
            "contribution_type": _singleton(values, "experience.contribution_type", UNKNOWN),
            "specialties": _unique(values.get("experience.specialties", [])),
        },
        "skills": skills_block(skills),
        "preferences": legacy_preferences,
        "constraints": {
            "hard_constraints": _user_list(inputs, "hard_constraints"),
            "soft_preferences": _user_list(inputs, "soft_preferences"),
            "avoid_keywords": _user_list(inputs, "avoid_keywords"),
            "negative_constraints": [],
            "excluded_domains": _user_list(inputs, "excluded_domains"),
            "accessibility_constraints": _user_list(inputs, "accessibility_constraints"),
        },
        "derived_matcher_signals": {
            "signals": signals,
            "derived_domains": domains,
            "derived_target_work_types": targets,
            "avoid_keywords": _user_list(inputs, "avoid_keywords"),
        },
        "matcher_compatible_profile": {},
        "provenance": {
            "extracted_from": "user_confirmed_ai_import",
            "evidence_snippets": [],
            "confidence": "high",
            "missing_fields": remaining_missing,
            "ambiguous_fields": [],
            "reviewed": True,
            "field_sources": {},
        },
    }
    field_sources = field_sources_for_profile(
        canonical,
        PROFILE_SOURCE_USER_CONFIRMATION,
        explicit=True,
    )
    _apply_review_fact_provenance(field_sources, canonical, facts)
    canonical["provenance"]["field_sources"] = field_sources
    return canonical, unpaired_education


def _apply_review_fact_provenance(field_sources, canonical, facts):
    """Keep document authority unless the candidate actually changed the value."""

    by_path = {}
    for fact in facts:
        by_path.setdefault(fact.field_path, []).append(fact)

    def apply(source_path, fact):
        source = field_sources.get(source_path)
        if source is None:
            return
        source["source"] = (
            PROFILE_SOURCE_USER_CORRECTION
            if fact.candidate_edited
            else PROFILE_SOURCE_RESUME
        )
        source["explicit"] = True if fact.candidate_edited else fact.explicit

    for field_path, path_facts in by_path.items():
        if field_path == "languages":
            remaining = list(path_facts)
            for index, language in enumerate(canonical["languages"]):
                match = next(
                    (
                        fact
                        for fact in remaining
                        if fact.value.language == language["language"]
                        and (fact.value.proficiency or UNKNOWN)
                        == language["proficiency"]
                        and (fact.value.locale or "") == language["locale"]
                    ),
                    None,
                )
                if match is None:
                    continue
                remaining.remove(match)
                for suffix in (
                    "language",
                    "proficiency",
                    "locale",
                    "confidence",
                    "proficiency_explicit",
                ):
                    apply(f"languages[{index}].{suffix}", match)
            continue
        target = canonical
        try:
            for component in field_path.split("."):
                target = target[component]
        except (KeyError, TypeError):
            continue
        if type(target) is list:
            remaining = list(path_facts)
            for index, value in enumerate(target):
                match = next(
                    (fact for fact in remaining if fact.value == value),
                    None,
                )
                if match is None:
                    continue
                remaining.remove(match)
                apply(f"{field_path}[{index}]", match)
            continue
        if path_facts:
            apply(field_path, path_facts[0])

    if "location.residence" not in by_path and by_path.get("location.country"):
        apply("location.residence", by_path["location.country"][0])


def _confirmed_profile_v2(confirmed, profile_id):
    education_authorities = confirmed.education_field_authorities_for_service()

    def education_source_authority(path):
        match = re.fullmatch(
            r"education\.entries\[([0-9]+)\]\."
            r"(kind|qualification|field|institution|status|completion_year)",
            path,
        )
        if match is None:
            raise CanonicalProfileV2Error("invalid_education_source_path")
        index = int(match.group(1))
        if not 0 <= index < len(education_authorities):
            raise CanonicalProfileV2Error("invalid_education_source_path")
        detail = education_authorities[index][match.group(2)]
        return detail["source_kind"], detail["explicit"]

    profile_v2 = convert_v1_to_v2(
        confirmed.reviewed_profile.bind_durable_profile_id(profile_id),
        persistent_profile_id=profile_id,
        source_ordinal_resolver=lambda _path, _source, _explicit: (1,),
    )
    profile_v2 = add_user_confirmed_education_entries_v1(
        profile_v2,
        confirmed.education_entries_for_service(),
        confirmed.unpaired_education_for_service(),
        source_ordinal_resolver=lambda _path, _source, _explicit: (1,),
        source_authority_resolver=education_source_authority,
    )
    preference_model = confirmed.preference_model_for_service()
    writer = (
        add_user_confirmed_preference_model_v2
        if preference_model["schema_version"] == PREFERENCE_V2_SCHEMA_VERSION
        else add_user_confirmed_preference_model_v1
    )
    return writer(
        profile_v2,
        preference_model,
        source_ordinal_resolver=lambda _path, _source, _explicit: (1,),
    )


def _create_command(confirmed, authority, reservation, now):
    source = UserConfirmedAIImportSourceDraft.from_metadata(
        confirmed.source_metadata.to_mapping(),
        confirmed_at=now,
    )

    try:
        return CreatePersistentProfileCommand.prepare(
            principal=authority[5],
            canonical_profile_v2=_create_canonical_profile_v2_draft(
                lambda profile_id: _confirmed_profile_v2(confirmed, profile_id)
            ),
            sources=(source,),
            normalizer_version=AI_PROFILE_IMPORT_NORMALIZER_VERSION,
            reviewer_version=AI_PROFILE_IMPORT_REVIEWER_VERSION,
            actor_type=AI_PROFILE_IMPORT_ACTOR_TYPE,
            reason_code=AI_PROFILE_IMPORT_REASON_CODE,
            idempotency_key="ai-profile-import:" + reservation.attempt_id,
            accepted_at=now,
            capabilities=MIGRATION_010_CAPABILITIES,
        )
    except PersistentProfileDomainError:
        raise AIProfileImportError("content_rejected") from None


def _grant_authority(connection, grant, now):
    if type(grant) is not TrustedProfileIntakeGrant:
        raise AIProfileImportError("ownership_stale")
    try:
        binding = grant.artifact_binding()
        principal = grant.principal_for_repository()
        lineage = grant.lineage_for_repository()
    except (AttributeError, TypeError, ValueError, PersistentProfileDomainError):
        raise AIProfileImportError("ownership_stale") from None
    if (
        type(binding) is not tuple
        or len(binding) != 10
        or binding[-1] != PROFILE_INTAKE_PURPOSE
        or type(lineage) is not TrustedProfileCreateLineage
        or type(principal) is not TrustedPrincipalContext
        or binding[0] != lineage.account_id
        or binding[2] != lineage.environment_namespace
        or binding[3] != lineage.principal_id
        or principal.principal_id != lineage.principal_id
    ):
        raise AIProfileImportError("ownership_stale")
    timestamp = canonical_utc_timestamp(now)
    try:
        row = connection.execute(
            "SELECT session.user_id,account.lifecycle_status,session.revoked_at,session.idle_expires_at,session.absolute_expires_at "
            "FROM account_sessions session JOIN users account ON account.user_id=session.user_id "
            "WHERE session.session_id=?",
            (binding[1],),
        ).fetchone()
    except (AttributeError, sqlite3.Error):
        raise AIProfileImportError("schema_unavailable") from None
    if (
        row is None
        or row[0] != binding[0]
        or row[1] != "active"
        or row[2] is not None
        or not timestamp < row[3]
        or not timestamp < row[4]
    ):
        raise AIProfileImportError("ownership_stale")
    try:
        current = capture_profile_create_lineage(
            connection,
            account_id=binding[0],
            environment_namespace=binding[2],
            principal_id=binding[3],
        )
    except PersistentProfileDomainError:
        raise AIProfileImportError("ownership_stale") from None
    except sqlite3.Error:
        raise AIProfileImportError("schema_unavailable") from None
    if current != lineage:
        raise AIProfileImportError("ownership_stale")
    return binding[0], binding[1], binding[2], binding[3], lineage, principal


def _require_profile_absent(connection, authority):
    if connection.execute(
        "SELECT 1 FROM product_profiles WHERE principal_id=? AND environment_namespace=?",
        (authority[3], authority[2]),
    ).fetchone() is not None:
        raise AIProfileImportError("profile_already_exists")


def _reserve_in_transaction(connection, authority, request, now, token_hex):
    metadata_json = request.source_metadata.canonical_json
    key_hash = hashlib.sha256(request.idempotency_key.encode("ascii")).hexdigest()
    fingerprint = _request_fingerprint(authority, key_hash, metadata_json)
    existing = connection.execute(
        "SELECT attempt_id,reservation_id,state,request_fingerprint,lease_expires_at,"
        "binding_id,binding_version,latest_event_version,latest_event_id,lineage_sha256 "
        "FROM ai_profile_import_attempts WHERE environment_namespace=? AND account_id=? "
        "AND entitlement_code=? AND idempotency_key_sha256=?",
        (authority[2], authority[0], AI_PROFILE_IMPORT_ENTITLEMENT_CODE, key_hash),
    ).fetchone()
    if existing is not None:
        if not hmac.compare_digest(existing[3], fingerprint):
            raise AIProfileImportError("idempotency_conflict")
        lineage = authority[4]
        if tuple(existing[5:]) != (
            lineage.binding_id,
            lineage.binding_version,
            lineage.latest_event_version,
            lineage.latest_event_id,
            lineage.lineage_sha256,
        ):
            raise AIProfileImportError("ownership_stale")
        if existing[2] == "reserved" and existing[4] <= canonical_utc_timestamp(now):
            _expire_attempt(connection, existing[0], existing[1], authority, now)
            return AIProfileImportError("reservation_expired")
        if existing[2] == "released":
            return AIProfileImportError("attempt_released")
        if existing[2] in {"expired", "failed"}:
            return AIProfileImportError("attempt_failed")
        return AIProfileImportReservationResult(
            existing[2],
            _reservation_authority(
                existing[0], existing[1], authority, fingerprint, existing[4]
            ),
            True,
        )

    entitlement = connection.execute(
        "SELECT state,reservation_id,attempt_id,lease_expires_at FROM ai_profile_import_entitlements "
        "WHERE environment_namespace=? AND account_id=? AND entitlement_code=?",
        (authority[2], authority[0], AI_PROFILE_IMPORT_ENTITLEMENT_CODE),
    ).fetchone()
    timestamp = canonical_utc_timestamp(now)
    if entitlement is None:
        connection.execute(
            "INSERT INTO ai_profile_import_entitlements "
            "(environment_namespace,account_id,entitlement_code,state,reservation_id,attempt_id,lease_expires_at,consumed_at,created_at,updated_at) "
            "VALUES (?,?,?,'available',NULL,NULL,NULL,NULL,?,?)",
            (
                authority[2],
                authority[0],
                AI_PROFILE_IMPORT_ENTITLEMENT_CODE,
                timestamp,
                timestamp,
            ),
        )
        entitlement = ("available", None, None, None)
    if entitlement[0] == "consumed":
        raise AIProfileImportError("entitlement_consumed")
    if entitlement[0] == "reserved":
        if entitlement[3] > timestamp:
            raise AIProfileImportError("entitlement_reserved")
        _expire_attempt(connection, entitlement[2], entitlement[1], authority, now)

    attempt_id = _new_id(
        connection, "aip", "ai_profile_import_attempts", "attempt_id", token_hex
    )
    reservation_id = _new_id(
        connection, "air", "ai_profile_import_attempts", "reservation_id", token_hex
    )
    lease = canonical_utc_timestamp(now + AI_PROFILE_IMPORT_RESERVATION_LEASE)
    lineage = authority[4]
    connection.execute(
        "INSERT INTO ai_profile_import_attempts "
        "(attempt_id,reservation_id,environment_namespace,account_id,principal_id,entitlement_code,state,"
        "idempotency_key_sha256,request_fingerprint,confirmation_fingerprint,source_metadata_json,"
        "binding_id,binding_version,latest_event_version,latest_event_id,lineage_sha256,lease_expires_at,"
        "result_code,result_profile_id,result_revision_id,created_at,updated_at,completed_at) "
        "VALUES (?,?,?,?,?,?,'reserved',?,?,NULL,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,NULL)",
        (
            attempt_id,
            reservation_id,
            authority[2],
            authority[0],
            authority[3],
            AI_PROFILE_IMPORT_ENTITLEMENT_CODE,
            key_hash,
            fingerprint,
            metadata_json,
            lineage.binding_id,
            lineage.binding_version,
            lineage.latest_event_version,
            lineage.latest_event_id,
            lineage.lineage_sha256,
            lease,
            timestamp,
            timestamp,
        ),
    )
    connection.execute(
        "UPDATE ai_profile_import_entitlements SET state='reserved',reservation_id=?,attempt_id=?,"
        "lease_expires_at=?,updated_at=? WHERE environment_namespace=? AND account_id=? AND entitlement_code=? AND state='available'",
        (
            reservation_id,
            attempt_id,
            lease,
            timestamp,
            authority[2],
            authority[0],
            AI_PROFILE_IMPORT_ENTITLEMENT_CODE,
        ),
    )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise AIProfileImportError("entitlement_reserved")
    return AIProfileImportReservationResult(
        "reserved",
        _reservation_authority(
            attempt_id, reservation_id, authority, fingerprint, lease
        ),
        False,
    )


def _attempt_row(connection, attempt_id):
    row = connection.execute(
        "SELECT attempt_id,reservation_id,state,environment_namespace,account_id,principal_id,source_metadata_json,"
        "lease_expires_at,confirmation_fingerprint,result_code,result_profile_id,result_revision_id,"
        "request_fingerprint,binding_id,binding_version,latest_event_version,latest_event_id,lineage_sha256,"
        "created_at,updated_at "
        "FROM ai_profile_import_attempts WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise AIProfileImportError("reservation_mismatch")
    return row


def _require_attempt_match(row, reservation, authority):
    lineage = authority[4]
    if (
        row[0] != reservation.attempt_id
        or row[1] != reservation.reservation_id
        or row[3] != authority[2]
        or row[4] != authority[0]
        or row[5] != authority[3]
        or not hmac.compare_digest(row[12], reservation.request_fingerprint)
        or tuple(row[13:18])
        != (
            lineage.binding_id,
            lineage.binding_version,
            lineage.latest_event_version,
            lineage.latest_event_id,
            lineage.lineage_sha256,
        )
    ):
        raise AIProfileImportError("reservation_mismatch")


def _checkpoint_row(connection, checkpoint_id):
    row = connection.execute(
        "SELECT checkpoint_id,environment_namespace,row_version,review_payload_json,"
        "review_payload_sha256,source_metadata_json,reservation_generation,expires_at,"
        "account_id,principal_id,binding_id,binding_version,latest_event_version,"
        "latest_event_id,lineage_sha256 FROM ai_profile_intake_checkpoints "
        "WHERE checkpoint_id=? AND state='active'",
        (checkpoint_id,),
    ).fetchone()
    if row is None:
        raise AIProfileImportError("checkpoint_missing")
    return row


def _validated_checkpoint_content(row):
    try:
        source_metadata = AIProfileImportSourceMetadata._from_mapping(
            json.loads(row[5])
        )
        review = hydrate_profile_intake_checkpoint(row[3])
        review_step = profile_intake_checkpoint_review_step(row[3])
    except (
        AIProfileImportError,
        ProfileIntakeError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
    ):
        raise AIProfileImportError("checkpoint_tampered") from None
    if (
        not hmac.compare_digest(
            hashlib.sha256(row[3].encode("ascii")).hexdigest(), row[4]
        )
        or not hmac.compare_digest(source_metadata.canonical_json, row[5])
        or tuple(source.document_kind.value for source in review.sources)
        != source_metadata.origins
    ):
        raise AIProfileImportError("checkpoint_tampered")
    return review, source_metadata, review_step


def _require_checkpoint_match(row, authority, *, expected_version):
    lineage = authority[4]
    if (
        row[1] != authority[2]
        or row[8] != authority[0]
        or row[9] != authority[3]
        or row[2] != expected_version
        or tuple(row[10:15])
        != (
            lineage.binding_id,
            lineage.binding_version,
            lineage.latest_event_version,
            lineage.latest_event_id,
            lineage.lineage_sha256,
        )
    ):
        raise AIProfileImportError(
            "checkpoint_stale" if row[2] != expected_version else "ownership_stale"
        )


def _checkpoint_authority(checkpoint_id, row_version, expires_at, generation):
    return AIProfileIntakeCheckpointAuthority._issue(
        _AUTHORITY_ISSUER,
        checkpoint_id=checkpoint_id,
        row_version=row_version,
        expires_at=expires_at,
        reservation_generation=generation,
    )


def _checkpoint_reservation_request(checkpoint_id, generation, source_metadata):
    return AIProfileImportReservationRequest(
        f"checkpoint:{checkpoint_id}:{generation}", source_metadata
    )


def _discard_checkpoint_in_transaction(
    connection, authority, checkpoint_id, generation, now, *, expired
):
    request_key = f"checkpoint:{checkpoint_id}:{generation}"
    key_hash = hashlib.sha256(request_key.encode("ascii")).hexdigest()
    attempt = connection.execute(
        "SELECT attempt_id,reservation_id,state FROM ai_profile_import_attempts "
        "WHERE environment_namespace=? AND account_id=? AND entitlement_code=? "
        "AND idempotency_key_sha256=?",
        (authority[2], authority[0], AI_PROFILE_IMPORT_ENTITLEMENT_CODE, key_hash),
    ).fetchone()
    if attempt is not None and attempt[2] == "reserved":
        if expired:
            _expire_attempt(connection, attempt[0], attempt[1], authority, now)
        else:
            timestamp = canonical_utc_timestamp(now)
            connection.execute(
                "UPDATE ai_profile_import_attempts SET state='released',result_code='abandoned',"
                "updated_at=?,completed_at=? WHERE attempt_id=? AND state='reserved'",
                (timestamp, timestamp, attempt[0]),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise AIProfileImportError("reservation_mismatch")
            connection.execute(
                "UPDATE ai_profile_import_entitlements SET state='available',reservation_id=NULL,"
                "attempt_id=NULL,lease_expires_at=NULL,updated_at=? WHERE environment_namespace=? "
                "AND account_id=? AND entitlement_code=? AND state='reserved' AND attempt_id=? AND reservation_id=?",
                (
                    timestamp,
                    authority[2],
                    authority[0],
                    AI_PROFILE_IMPORT_ENTITLEMENT_CODE,
                    attempt[0],
                    attempt[1],
                ),
            )
            if connection.execute("SELECT changes()").fetchone()[0] != 1:
                raise AIProfileImportError("reservation_mismatch")
    connection.execute(
        "DELETE FROM ai_profile_intake_checkpoints WHERE checkpoint_id=?",
        (checkpoint_id,),
    )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise AIProfileImportError("checkpoint_stale")


def _expire_attempt(connection, attempt_id, reservation_id, authority, now):
    timestamp = canonical_utc_timestamp(now)
    connection.execute(
        "UPDATE ai_profile_import_attempts SET state='expired',result_code='reservation_expired',updated_at=?,completed_at=? "
        "WHERE attempt_id=? AND reservation_id=? AND state='reserved'",
        (timestamp, timestamp, attempt_id, reservation_id),
    )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise AIProfileImportError("reservation_mismatch")
    connection.execute(
        "UPDATE ai_profile_import_entitlements SET state='available',reservation_id=NULL,attempt_id=NULL,lease_expires_at=NULL,updated_at=? "
        "WHERE environment_namespace=? AND account_id=? AND entitlement_code=? AND state='reserved' AND attempt_id=? AND reservation_id=?",
        (timestamp, authority[2], authority[0], AI_PROFILE_IMPORT_ENTITLEMENT_CODE, attempt_id, reservation_id),
    )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise AIProfileImportError("reservation_mismatch")


def _reservation_authority(attempt_id, reservation_id, authority, fingerprint, lease):
    return AIProfileImportReservationAuthority._issue(
        _AUTHORITY_ISSUER,
        attempt_id=attempt_id,
        reservation_id=reservation_id,
        environment_namespace=authority[2],
        account_id=authority[0],
        principal_id=authority[3],
        request_fingerprint=fingerprint,
        lease_expires_at=lease,
    )


def _require_reservation_binding(reservation, authority):
    if (
        type(reservation) is not AIProfileImportReservationAuthority
        or getattr(reservation, "_issuer", None) is not _AUTHORITY_ISSUER
        or reservation.environment_namespace != authority[2]
        or reservation.account_id != authority[0]
        or reservation.principal_id != authority[3]
    ):
        raise AIProfileImportError("reservation_mismatch")


def _request_fingerprint(authority, key_hash, metadata_json):
    lineage = authority[4]
    payload = {
        "version": 1,
        "environment_namespace": authority[2],
        "account_id": authority[0],
        "principal_id": authority[3],
        "entitlement_code": AI_PROFILE_IMPORT_ENTITLEMENT_CODE,
        "idempotency_key_sha256": key_hash,
        "source_metadata": json.loads(metadata_json),
        "ownership_lineage": {
            "binding_id": lineage.binding_id,
            "binding_version": lineage.binding_version,
            "latest_event_version": lineage.latest_event_version,
            "latest_event_id": lineage.latest_event_id,
            "lineage_sha256": lineage.lineage_sha256,
        },
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()


def _new_id(connection, prefix, table, column, token_hex):
    for _attempt in range(16):
        token = token_hex(16)
        candidate = f"{prefix}_{token}"
        pattern = {
            "aip": _ATTEMPT,
            "air": _RESERVATION,
            "aic": _CHECKPOINT,
        }.get(prefix)
        if type(token) is str and pattern.fullmatch(candidate) is not None and connection.execute(
            f"SELECT 1 FROM {table} WHERE {column}=?", (candidate,)
        ).fetchone() is None and len(set(candidate[4:])) > 1:
            return candidate
    raise AIProfileImportError("internal_failure")


def _trusted_time(value):
    if type(value) is not datetime or value.tzinfo is None:
        raise AIProfileImportError("invalid_request")
    try:
        normalized = value.astimezone(timezone.utc).replace(microsecond=0)
        canonical_utc_timestamp(normalized)
    except (PersistentProfileDomainError, TypeError, ValueError, OverflowError, OSError):
        raise AIProfileImportError("invalid_request") from None
    return normalized


def _require_m011(connection):
    if attest_resumable_ai_profile_intake_schema(connection).get("state") != "correctly_installed":
        raise AIProfileImportError("schema_unavailable")


def _atomic(connection, operation, *, before_commit=None):
    if type(connection) is not sqlite3.Connection:
        raise AIProfileImportError("schema_unavailable")
    try:
        invalid = (
            connection.in_transaction
            or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
            or connection.execute("PRAGMA query_only").fetchone()[0] != 0
        )
    except sqlite3.Error:
        raise AIProfileImportError("schema_unavailable") from None
    if invalid:
        raise AIProfileImportError("schema_unavailable")
    try:
        connection.execute("BEGIN IMMEDIATE")
        operation()
        if before_commit is not None:
            before_commit()
        connection.commit()
    except AIProfileImportError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except sqlite3.Error as exc:
        if connection.in_transaction:
            connection.rollback()
        code = "temporary_contention" if getattr(exc, "sqlite_errorcode", None) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} else "internal_failure"
        _detach(exc)
        raise AIProfileImportError(code) from None
    except Exception as exc:
        if connection.in_transaction:
            connection.rollback()
        _detach(exc)
        raise AIProfileImportError("internal_failure") from None
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise


def _hook(callback, point):
    if callback is not None:
        callback(point)


def _detach(exc):
    try:
        exc.__traceback__ = None
        exc.__cause__ = None
        exc.__context__ = None
    except (AttributeError, TypeError):
        pass


def _singleton(values, path, default):
    items = values.get(path, [])
    if len(items) > 1:
        raise AIProfileImportError("review_unresolved")
    return items[0] if items else default


def _unique(values):
    result = []
    seen = set()
    for value in values:
        if type(value) is not str:
            raise AIProfileImportError("content_rejected")
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _country(value):
    if not value:
        return ""
    try:
        return normalize_country(value)
    except (TypeError, ValueError):
        raise AIProfileImportError("content_rejected") from None


def _user_list(inputs, name):
    value = inputs.get(name, "")
    if not value:
        return []
    return _unique([item.strip() for item in value.split(",") if item.strip()])


__all__ = (
    "AI_PROFILE_IMPORT_ENTITLEMENT_CODE",
    "AI_PROFILE_IMPORT_RESERVATION_LEASE",
    "AI_PROFILE_IMPORT_RESERVATION_RENEWAL_INTERVAL",
    "AI_PROFILE_IMPORT_RESERVATION_GENERATION_MAX",
    "AI_PROFILE_INTAKE_CHECKPOINT_RETENTION",
    "AI_PROFILE_INTAKE_CHECKPOINT_SCHEMA_VERSION",
    "AIProfileIntakeCheckpointAuthority",
    "AIProfileIntakeCheckpointResult",
    "AIProfileIntakeCheckpointSummary",
    "AIProfileImportCommitResult",
    "AIProfileImportError",
    "AIProfileImportPreflightResult",
    "AIProfileImportReservationAuthority",
    "AIProfileImportReservationRequest",
    "AIProfileImportReservationResult",
    "AIProfileImportService",
    "AIProfileImportSourceMetadata",
    "ConfirmedAIProfileImport",
    "prepare_confirmed_ai_profile_import",
)
