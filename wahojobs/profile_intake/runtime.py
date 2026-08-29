"""Authenticated process-local runtime primitives for AI profile intake.

The optional Slice 4B finalizer owns the explicit connection scopes.  This
module still performs document/model work outside database transactions and
retains durable authority only as an opaque server-side vault capability.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import threading
import time

from wahojobs.accounts import PublicSession, SessionUnavailable, validate_session_csrf
from wahojobs.browser_session_authentication import BrowserSessionAuthenticationUnavailable
from wahojobs.persistent_profile_read_authorization import (
    PersistentProfileReadAuthorizationDecision,
)
from wahojobs.persistent_profiles import TrustedPrincipalContext
from wahojobs.persistent_profiles_repository import (
    TrustedProfileCreateLineage,
    capture_profile_create_lineage,
)
from wahojobs.profile_intake import contracts
from wahojobs.profile_intake.contracts import (
    AI_EXTRACTION_SCHEMA_VERSION,
    AIProfileExtraction,
    DocumentFormat,
    DocumentKind,
    LanguageValue,
    ModelEvidencePacket,
    ProfileIntakeError,
    new_document_reference,
    validate_ai_profile_extraction,
)
from wahojobs.profile_intake.documents import extract_profile_document
from wahojobs.profile_intake.minimization import (
    contains_detectable_contact_pii,
    minimize_evidence_packet,
)
from wahojobs.profile_intake.openai_adapter import (
    ProfileExtractionDiagnostics,
    ProfileExtractionOutcome,
)
from wahojobs.profile_intake.review_draft import (
    AIProfileReviewDraft,
    REVIEW_DRAFT_SCHEMA_VERSION,
    ReviewDraftSource,
    ReviewSourceAttribution,
    ValidatedProfileSource,
    _REVIEW_FIELDS,
    _USER_ONLY_REVIEW_FIELDS,
    reconcile_profile_extractions,
)
from wahojobs.profiles.preference_model import (
    ProfilePreferenceModelError,
    empty_profile_preferences_v2,
    validate_profile_preferences,
)
from wahojobs.profiles.education_entries import (
    EducationEntryContractError,
    MAX_EDUCATION_ENTRIES,
    canonicalize_education_entries_v1,
    education_entry_identity,
)


PROFILE_INTAKE_ROUTE = "/account/profile/intake"
PROFILE_INTAKE_REVIEW_ROUTE = "/account/profile/intake/review"
PROFILE_INTAKE_PURPOSE = "ai_profile_intake_review_v1"
PROFILE_INTAKE_DRAFT_LIFETIME_SECONDS = 30 * 60
PROFILE_INTAKE_DRAFT_ABSOLUTE_LIFETIME_SECONDS = 2 * 60 * 60
PROFILE_INTAKE_DRAFT_RENEWAL_INTERVAL_SECONDS = 5 * 60
PROFILE_INTAKE_DRAFT_CAPACITY = 64
PROFILE_INTAKE_COMPLETION_RECEIPT_SECONDS = 120
PROFILE_INTAKE_MIN_SAFE_REVIEW_SECONDS = 60
PROFILE_INTAKE_CSRF_MESSAGE_PREFIX = b"wahojobs.profile-intake.v1\x00"
MAX_REVIEW_VALUE_CHARS = 512
MAX_REVIEW_USER_INPUT_CHARS = 512
PROFILE_INTAKE_CHECKPOINT_SCHEMA_VERSION = "profile_intake_checkpoint_v1"
PROFILE_INTAKE_CHECKPOINT_MAX_BYTES = 65_536
PROFILE_INTAKE_REVIEW_STEPS = (
    "review-found",
    "review-suggestions",
    "review-preferences",
    "review-finish",
)
PROFILE_INTAKE_DEFAULT_REVIEW_STEP = PROFILE_INTAKE_REVIEW_STEPS[0]
PROFILE_INTAKE_REVIEW_COLLECTIONS = {
    "skills": {
        "title": "Skills and areas of expertise",
        "paths": ("skills.normalized", "experience.specialties"),
        "add_path": "skills.normalized",
        "kind": "string",
        # The visual collection can contain both canonical lists. Candidate
        # additions remain bounded by the Canonical V2 skills limit.
        "limit": 224,
        "add_limit": 96,
        "include_suggested": True,
        "deduplicate": True,
        "browser_visible": True,
    },
    "job_titles": {
        "title": "Job titles",
        "paths": ("experience.job_titles", "experience.recent_roles"),
        "add_path": "experience.job_titles",
        "kind": "string",
        "limit": 256,
        "add_limit": 128,
        "deduplicate": True,
        "browser_visible": True,
    },
    "industries": {
        "title": "Industries in your experience",
        "paths": ("experience.industries",),
        "add_path": "experience.industries",
        "kind": "string",
        "limit": 128,
        "add_limit": 128,
        "include_suggested": True,
        "deduplicate": True,
        # Compatibility-only collection. New assisted intake does not ask the
        # candidate to maintain an industry taxonomy.
        "browser_visible": False,
    },
    "languages": {
        "title": "Languages",
        "paths": ("languages",),
        "add_path": "languages",
        "kind": "language",
        "limit": 32,
        "browser_visible": True,
    },
}
_USER_FACT_REFERENCE = re.compile(
    r"^uci_(?:skills|job_titles|industries|languages)_[0-9]{3}$"
)
from wahojobs.profiles.canonical import (
    PROFILE_SOURCE_RESUME,
    PROFILE_SOURCE_USER_CONFIRMATION,
    PROFILE_SOURCE_USER_CORRECTION,
)
_EDUCATION_ENTRY_REFERENCE = re.compile(r"^edu_[0-9]{3}$")
_EDUCATION_ENTRY_COMPONENTS = {
    "education.education_level": "kind",
    "education.degrees": "qualification",
    "education.fields_or_domains": "field",
    "education.institutions": "institution",
    "education.completion_status": "status",
    "education.graduation_years": "completion_year",
}
_EDUCATION_ENTRY_FIELDS = (
    "kind",
    "qualification",
    "field",
    "institution",
    "status",
    "completion_year",
)

PROFILE_INTAKE_RESET_SECTIONS = frozenset(
    {
        "profile_basics",
        "work_history",
        "education",
        "languages",
        "expertise",
        "professional_experience",
    }
)


def _review_reset_section_for_fact(fact):
    """Return the only candidate-facing reset section owning one extracted fact."""

    if fact.field_path.startswith(("identity.", "location.")):
        return "profile_basics"
    if fact.conflict_group is not None:
        return None
    if fact.field_path in PROFILE_INTAKE_REVIEW_COLLECTIONS["job_titles"]["paths"]:
        return "work_history"
    if fact.field_path in _EDUCATION_ENTRY_COMPONENTS:
        return "education"
    if fact.field_path in PROFILE_INTAKE_REVIEW_COLLECTIONS["languages"]["paths"]:
        return "languages"
    if fact.field_path in PROFILE_INTAKE_REVIEW_COLLECTIONS["skills"]["paths"]:
        return "expertise"
    if fact.field_path == "experience.total_years":
        return "professional_experience"
    return None

_OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_ACTIONS = frozenset(
    {
        "upload",
        "continue",
        "discard_saved",
        "update",
        "autosave",
        "renew",
        "cancel",
        "save",
    }
)
_REQUEST_ROUTES = frozenset({PROFILE_INTAKE_ROUTE, PROFILE_INTAKE_REVIEW_ROUTE})
_GRANT_ISSUER = object()
_TYPED_PREFERENCE_REPLACED_USER_FIELDS = frozenset(
    {
        "remote",
        "flexible",
        "employment_types",
        "synchronous_preference",
        "phone_preference",
        "schedule",
        "availability",
        "target_opportunity_types",
        "work_preferences",
    }
)


def _configuration_error():
    return ValueError("invalid_profile_intake_runtime_configuration")


def normalize_profile_intake_review_step(value):
    """Return the closed presentation hint, defaulting away invalid input."""

    return (
        value
        if type(value) is str and value in PROFILE_INTAKE_REVIEW_STEPS
        else PROFILE_INTAKE_DEFAULT_REVIEW_STEP
    )


class ProfileIntakeRequestContext:
    """Sealed request facts accepted by the existing durable auth gateway."""

    __slots__ = ("method", "route", "_authentication_input", "_sealed")

    def __init__(self, method, route, authentication_input=None):
        if method not in {"GET", "HEAD", "POST"} or route not in _REQUEST_ROUTES:
            raise _configuration_error()
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "route", route)
        object.__setattr__(self, "_authentication_input", authentication_input)
        object.__setattr__(self, "_sealed", True)

    def authentication_input_for_gateway(self):
        return self._authentication_input

    def __setattr__(self, _name, _value):
        raise AttributeError("profile_intake_request_context_is_immutable")

    def __repr__(self):
        return (
            "ProfileIntakeRequestContext("
            f"method={self.method!r}, route={self.route!r}, "
            "authentication_input=<redacted>)"
        )

    def __reduce_ex__(self, _protocol):
        raise TypeError("profile_intake_request_context_not_serializable")


class TrustedProfileIntakeGrant:
    """Server-issued authority for one exact intake browser lineage."""

    __slots__ = ("_lineage", "_principal", "_session_id", "_sealed")

    def __new__(cls, *_args, **_kwargs):
        raise _configuration_error()

    @classmethod
    def _issue(cls, capability, *, session_id, principal, lineage):
        if (
            cls is not TrustedProfileIntakeGrant
            or capability is not _GRANT_ISSUER
            or type(session_id) is not str
            or type(principal) is not TrustedPrincipalContext
            or type(lineage) is not TrustedProfileCreateLineage
            or principal.principal_id != lineage.principal_id
            or principal.environment_namespace != lineage.environment_namespace
        ):
            raise _configuration_error()
        instance = object.__new__(cls)
        object.__setattr__(instance, "_session_id", session_id)
        object.__setattr__(instance, "_principal", principal)
        object.__setattr__(instance, "_lineage", lineage)
        object.__setattr__(instance, "_sealed", True)
        return instance

    def artifact_binding(self):
        return self._lineage.artifact_binding(
            self._session_id,
            PROFILE_INTAKE_PURPOSE,
        )

    def principal_for_repository(self):
        return self._principal

    def lineage_for_repository(self):
        return self._lineage

    def __setattr__(self, _name, _value):
        raise AttributeError("trusted_profile_intake_grant_is_immutable")

    def __repr__(self):
        return "TrustedProfileIntakeGrant(<redacted>)"

    def __reduce_ex__(self, _protocol):
        raise TypeError("trusted_profile_intake_grant_not_serializable")


@dataclass(frozen=True, slots=True, repr=False)
class ProfileIntakeAuthorityOutcome:
    state: str
    _grant: object | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.state not in {
            "authorized",
            "authentication_required",
            "csrf_denied",
            "authorization_denied",
            "unavailable",
        }:
            raise _configuration_error()
        if self.state == "authorized":
            if type(self._grant) is not TrustedProfileIntakeGrant:
                raise _configuration_error()
        elif self._grant is not None:
            raise _configuration_error()

    def grant_for_service(self):
        return self._grant if self.state == "authorized" else None


def profile_intake_csrf_proof(
    csrf_secret,
    action,
    *,
    draft_reference=None,
    version=None,
):
    if (
        type(csrf_secret) is not str
        or _OPAQUE_REFERENCE.fullmatch(csrf_secret) is None
        or action not in _ACTIONS
        or (draft_reference is None) != (version is None)
        or (
            draft_reference is not None
            and (
                type(draft_reference) is not str
                or _OPAQUE_REFERENCE.fullmatch(draft_reference) is None
                or type(version) is not int
                or version < 1
            )
        )
    ):
        raise _configuration_error()
    message = action
    if draft_reference is not None:
        message += "\x00" + draft_reference + "\x00" + str(version)
    digest = hmac.new(
        csrf_secret.encode("ascii"),
        PROFILE_INTAKE_CSRF_MESSAGE_PREFIX + message.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class ProfileIntakeAuthorityService:
    """Revalidate durable identity and ownership without retaining a transaction."""

    __slots__ = (
        "_authentication_gateway",
        "_authorization_gateway",
        "_clock",
        "_read_connection_provider",
    )

    def __init__(
        self,
        *,
        authentication_gateway,
        authorization_gateway,
        read_connection_provider,
        clock,
    ):
        if not all(
            callable(value)
            for value in (read_connection_provider, clock)
        ) or not callable(
            getattr(authentication_gateway, "authenticate_browser_request", None)
        ) or not callable(
            getattr(authorization_gateway, "authorize_persistent_profile_read", None)
        ):
            raise _configuration_error()
        self._authentication_gateway = authentication_gateway
        self._authorization_gateway = authorization_gateway
        self._read_connection_provider = read_connection_provider
        self._clock = clock

    def authorize(
        self,
        *,
        method,
        route,
        authentication_input,
        session_token,
        csrf_secret,
        action=None,
        proof=None,
        draft_reference=None,
        version=None,
    ):
        if method == "POST" and action is not None:
            try:
                expected = profile_intake_csrf_proof(
                    csrf_secret,
                    action,
                    draft_reference=draft_reference,
                    version=version,
                )
            except (TypeError, ValueError):
                return ProfileIntakeAuthorityOutcome("csrf_denied")
            if (
                type(proof) is not str
                or _OPAQUE_REFERENCE.fullmatch(proof) is None
                or not hmac.compare_digest(proof, expected)
            ):
                return ProfileIntakeAuthorityOutcome("csrf_denied")
        now = self._clock()
        if type(now) is not datetime or now.tzinfo is None:
            return ProfileIntakeAuthorityOutcome("unavailable")
        try:
            with self._read_connection_provider() as connection:
                if (
                    not isinstance(connection, sqlite3.Connection)
                    or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
                    or connection.execute("PRAGMA query_only").fetchone()[0] != 1
                    or connection.in_transaction
                ):
                    return ProfileIntakeAuthorityOutcome("unavailable")
                connection.execute("BEGIN")
                try:
                    actor = self._authentication_gateway.authenticate_browser_request(
                        connection,
                        ProfileIntakeRequestContext(method, route, authentication_input),
                        now=now,
                    )
                    if actor is None:
                        return ProfileIntakeAuthorityOutcome("authentication_required")
                    try:
                        session = validate_session_csrf(
                            connection,
                            session_token=session_token,
                            csrf_secret=csrf_secret,
                            now=now,
                        )
                    except SessionUnavailable:
                        return ProfileIntakeAuthorityOutcome("csrf_denied")
                    if type(session) is not PublicSession:
                        return ProfileIntakeAuthorityOutcome("unavailable")
                    account_reference = actor.account_reference_for_authorization()
                    if (
                        type(account_reference) is not tuple
                        or len(account_reference) != 2
                        or account_reference[0] != session.user_id
                    ):
                        return ProfileIntakeAuthorityOutcome("unavailable")
                    decision = self._authorization_gateway.authorize_persistent_profile_read(
                        connection,
                        actor,
                    )
                    if type(decision) is not PersistentProfileReadAuthorizationDecision:
                        return ProfileIntakeAuthorityOutcome("unavailable")
                    if decision.state == "denied":
                        return ProfileIntakeAuthorityOutcome("authorization_denied")
                    if decision.state != "authorized":
                        return ProfileIntakeAuthorityOutcome("unavailable")
                    principal = decision.grant_for_application().principal_for_repository()
                    lineage = capture_profile_create_lineage(
                        connection,
                        account_id=session.user_id,
                        environment_namespace=account_reference[1],
                        principal_id=principal.principal_id,
                    )
                    return ProfileIntakeAuthorityOutcome(
                        "authorized",
                        TrustedProfileIntakeGrant._issue(
                            _GRANT_ISSUER,
                            session_id=session.session_id,
                            principal=principal,
                            lineage=lineage,
                        ),
                    )
                finally:
                    if connection.in_transaction:
                        connection.rollback()
        except BrowserSessionAuthenticationUnavailable:
            return ProfileIntakeAuthorityOutcome("unavailable")
        except (sqlite3.Error, TypeError, ValueError):
            return ProfileIntakeAuthorityOutcome("unavailable")


@dataclass(frozen=True, slots=True)
class EditableReviewFact:
    field_path: str
    review_field: str
    value: str | int | float | bool | LanguageValue
    source_attributions: tuple[ReviewSourceAttribution, ...]
    suggested: bool
    decision: str
    conflict_group: str | None
    explicit: bool = True
    candidate_edited: bool = False

    def __post_init__(self):
        if type(self.explicit) is not bool or type(self.candidate_edited) is not bool:
            raise ProfileIntakeError("invalid_review_submission")


@dataclass(frozen=True, slots=True, repr=False)
class EditableResetBaselineEntry:
    """Immutable checkpoint-scoped extraction state for one resettable fact."""

    section_id: str
    fact_index: int
    value: str | int | float | bool | LanguageValue = field(repr=False)
    decision: str

    def __post_init__(self):
        if (
            self.section_id not in PROFILE_INTAKE_RESET_SECTIONS
            or type(self.fact_index) is not int
            or self.fact_index < 0
            or self.decision not in {"pending", "accept", "reject", "keep", "remove"}
        ):
            raise ProfileIntakeError("invalid_review_submission")


@dataclass(frozen=True, slots=True, repr=False)
class EditableUserFact:
    """A closed, evidence-free review item added directly by the candidate."""

    item_reference: str
    collection_id: str
    field_path: str
    value: str | LanguageValue = field(repr=False)
    decision: str

    def __post_init__(self):
        spec = PROFILE_INTAKE_REVIEW_COLLECTIONS.get(self.collection_id)
        field_spec = contracts._FIELD_SPECS.get(self.field_path)
        if (
            spec is None
            or self.field_path != spec["add_path"]
            or _USER_FACT_REFERENCE.fullmatch(self.item_reference) is None
            or not self.item_reference.startswith(f"uci_{self.collection_id}_")
            or field_spec is None
            or contracts._validate_fact_value(self.value, field_spec) != self.value
            or self.decision not in {"keep", "remove"}
        ):
            raise ProfileIntakeError("invalid_review_submission")


@dataclass(frozen=True, slots=True, repr=False)
class EditableEducationEntry:
    """One closed review entry; source relationships remain server-owned."""

    item_reference: str
    origin: str
    kind: str
    qualification: str = field(repr=False)
    field_of_study: str = field(repr=False)
    institution: str = field(repr=False)
    status: str
    completion_year: int | None
    source_fact_indexes: tuple[int, ...] = field(repr=False)
    source_attributions: tuple[ReviewSourceAttribution, ...] = field(repr=False)
    decision: str
    extraction_components: tuple[str, ...] | None = field(default=None, repr=False)
    candidate_edited_components: tuple[str, ...] = field(default=(), repr=False)

    def __post_init__(self):
        if (
            _EDUCATION_ENTRY_REFERENCE.fullmatch(self.item_reference) is None
            or self.origin not in {"document", "user"}
            or type(self.source_fact_indexes) is not tuple
            or type(self.source_attributions) is not tuple
            or (
                self.extraction_components is not None
                and (
                    type(self.extraction_components) is not tuple
                    or tuple(sorted(set(self.extraction_components)))
                    != self.extraction_components
                    or not set(self.extraction_components) <= set(_EDUCATION_ENTRY_FIELDS)
                )
            )
            or type(self.candidate_edited_components) is not tuple
            or tuple(sorted(set(self.candidate_edited_components)))
            != self.candidate_edited_components
            or not set(self.candidate_edited_components) <= set(_EDUCATION_ENTRY_FIELDS)
            or self.decision not in {"keep", "remove"}
            or (
                self.origin == "document"
                and (not self.source_fact_indexes or not self.source_attributions)
            )
            or (
                self.origin == "user"
                and (self.source_fact_indexes or self.source_attributions)
            )
        ):
            raise ProfileIntakeError("invalid_review_submission")
        try:
            canonical = canonicalize_education_entries_v1(
                [_education_entry_mapping(self)]
            )[0]
        except EducationEntryContractError:
            raise ProfileIntakeError("invalid_review_submission") from None
        if canonical != _education_entry_mapping(self):
            raise ProfileIntakeError("invalid_review_submission")


@dataclass(frozen=True, slots=True)
class EditableProfileReview:
    schema_version: str
    sources: tuple[ReviewDraftSource, ...]
    facts: tuple[EditableReviewFact, ...]
    missing_user_fields: tuple[str, ...]
    user_inputs: tuple[tuple[str, str], ...]
    issue_count: int
    _preference_model_json: bytes = field(repr=False)
    user_facts: tuple[EditableUserFact, ...] = field(default=(), repr=False)
    education_entries: tuple[EditableEducationEntry, ...] = field(
        default=(), repr=False
    )
    reset_baseline: tuple[EditableResetBaselineEntry, ...] = field(
        default=(), repr=False
    )

    @property
    def document_reference(self):
        return self.sources[0].document_reference if len(self.sources) == 1 else None

    @property
    def preference_model(self):
        """Return a defensive copy of the server-validated preference model."""

        try:
            value = json.loads(self._preference_model_json.decode("ascii"))
            return validate_profile_preferences(value)
        except (UnicodeError, ValueError, TypeError):
            raise ProfileIntakeError("invalid_review_submission") from None


@dataclass(frozen=True, slots=True, repr=False)
class ProfileIntakeDocumentInput:
    document_bytes: bytes = field(repr=False)
    document_kind: DocumentKind
    document_format: DocumentFormat

    def __post_init__(self):
        if (
            type(self.document_bytes) is not bytes
            or not self.document_bytes
            or type(self.document_kind) is not DocumentKind
            or type(self.document_format) is not DocumentFormat
            or (
                self.document_kind is DocumentKind.LINKEDIN_PROFILE_EXPORT
                and self.document_format is not DocumentFormat.PDF
            )
        ):
            raise ProfileIntakeError("invalid_profile_intake_bundle")

    def __repr__(self):
        return (
            "ProfileIntakeDocumentInput("
            f"document_kind={self.document_kind!r}, "
            f"document_format={self.document_format!r}, content=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class SafeDocumentMetadata:
    document_reference: str
    origin: str
    format: str
    byte_size: int
    page_count: int | None
    parser: str
    parser_version: str

    def __post_init__(self):
        try:
            contracts._require_document_reference(self.document_reference)
            origin = DocumentKind(self.origin)
            document_format = DocumentFormat(self.format)
        except (ProfileIntakeError, ValueError):
            raise ProfileIntakeError("invalid_profile_intake_bundle_metadata") from None
        if (
            type(self.byte_size) is not int
            or not 1 <= self.byte_size <= contracts.DEFAULT_DOCUMENT_LIMITS.max_upload_bytes
            or (
                document_format is DocumentFormat.PDF
                and (
                    type(self.page_count) is not int
                    or not 1 <= self.page_count <= contracts.DEFAULT_DOCUMENT_LIMITS.max_pdf_pages
                )
            )
            or (document_format is DocumentFormat.DOCX and self.page_count is not None)
            or type(self.parser) is not str
            or not self.parser
            or type(self.parser_version) is not str
            or not self.parser_version
            or (
                origin is DocumentKind.LINKEDIN_PROFILE_EXPORT
                and document_format is not DocumentFormat.PDF
            )
        ):
            raise ProfileIntakeError("invalid_profile_intake_bundle_metadata")

    def __repr__(self):
        return (
            "SafeDocumentMetadata("
            f"origin={self.origin!r}, format={self.format!r}, "
            f"byte_size={self.byte_size!r}, page_count={self.page_count!r}, "
            f"parser={self.parser!r}, parser_version={self.parser_version!r}, "
            "document_reference=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class SafeDocumentBundleMetadata:
    documents: tuple[SafeDocumentMetadata, ...]

    def __post_init__(self):
        if (
            type(self.documents) is not tuple
            or not 1 <= len(self.documents) <= 2
            or any(type(item) is not SafeDocumentMetadata for item in self.documents)
            or len({item.origin for item in self.documents}) != len(self.documents)
        ):
            raise ProfileIntakeError("invalid_profile_intake_bundle_metadata")

    @property
    def origin(self):
        return self.documents[0].origin if len(self.documents) == 1 else "bundle"

    @property
    def format(self):
        return self.documents[0].format if len(self.documents) == 1 else "multiple"

    @property
    def byte_size(self):
        return sum(item.byte_size for item in self.documents)

    @property
    def page_count(self):
        if len(self.documents) == 1:
            return self.documents[0].page_count
        return None

    @property
    def parser(self):
        return self.documents[0].parser if len(self.documents) == 1 else "multiple"

    @property
    def parser_version(self):
        return self.documents[0].parser_version if len(self.documents) == 1 else "multiple"

    def __repr__(self):
        return f"SafeDocumentBundleMetadata(documents={self.documents!r})"


@dataclass(frozen=True, slots=True)
class SafeModelDiagnostics:
    document_kind: str
    model: str
    prompt_version: str
    schema_version: str
    input_tokens: int
    output_tokens: int
    duration_ms: int
    provider_request_id: str | None
    success: bool
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class SavedProfileIntakeProgress:
    created_at: str
    saved_at: str
    age_seconds: int
    expires_in_seconds: int

    def __post_init__(self):
        if (
            type(self.created_at) is not str
            or type(self.saved_at) is not str
            or type(self.age_seconds) is not int
            or self.age_seconds < 0
            or type(self.expires_in_seconds) is not int
            or self.expires_in_seconds <= 0
        ):
            raise _configuration_error()


@dataclass(frozen=True, slots=True, repr=False)
class IntakeDraftSnapshot:
    review: EditableProfileReview = field(repr=False)
    review_step: str
    document: SafeDocumentBundleMetadata | None
    diagnostics: tuple[SafeModelDiagnostics, ...] | None
    created_at: str
    expires_at_monotonic: float
    absolute_expires_at_monotonic: float
    last_renewed_at_monotonic: float
    version: int

    def __repr__(self):
        return (
            "IntakeDraftSnapshot("
            f"document={self.document!r}, version={self.version}, content=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _DraftRecord:
    binding: tuple = field(repr=False)
    snapshot: IntakeDraftSnapshot | None = field(repr=False)
    durable_authority: object | None = field(default=None, repr=False)
    state: str = "active"
    confirmation_fingerprint: str | None = field(default=None, repr=False)
    save_request_digest: str | None = field(default=None, repr=False)
    completion_expires_at_monotonic: float | None = None


class IntakeDraftVault:
    """Bounded, process-local draft storage with exact lineage binding."""

    __slots__ = (
        "_absolute_ttl",
        "_capacity",
        "_closed",
        "_lock",
        "_monotonic",
        "_records",
        "_renewal_interval",
        "_token_factory",
        "_ttl",
    )

    def __init__(
        self,
        *,
        monotonic=time.monotonic,
        token_factory=lambda: secrets.token_urlsafe(32),
        ttl_seconds=PROFILE_INTAKE_DRAFT_LIFETIME_SECONDS,
        absolute_ttl_seconds=PROFILE_INTAKE_DRAFT_ABSOLUTE_LIFETIME_SECONDS,
        renewal_interval_seconds=PROFILE_INTAKE_DRAFT_RENEWAL_INTERVAL_SECONDS,
        capacity=PROFILE_INTAKE_DRAFT_CAPACITY,
    ):
        if (
            not callable(monotonic)
            or not callable(token_factory)
            or type(ttl_seconds) not in (int, float)
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
            or type(absolute_ttl_seconds) not in (int, float)
            or not math.isfinite(absolute_ttl_seconds)
            or absolute_ttl_seconds < ttl_seconds
            or type(renewal_interval_seconds) not in (int, float)
            or not math.isfinite(renewal_interval_seconds)
            or not 0 < renewal_interval_seconds < ttl_seconds
            or type(capacity) is not int
            or capacity < 1
        ):
            raise _configuration_error()
        self._monotonic = monotonic
        self._token_factory = token_factory
        self._ttl = float(ttl_seconds)
        self._absolute_ttl = float(absolute_ttl_seconds)
        self._renewal_interval = float(renewal_interval_seconds)
        self._capacity = capacity
        self._lock = threading.RLock()
        self._records = {}
        self._closed = False

    def activate(self):
        with self._lock:
            return not self._closed

    def close(self):
        with self._lock:
            self._closed = True
            self._records.clear()

    @property
    def closed(self):
        with self._lock:
            return self._closed

    def issue(
        self,
        grant,
        review,
        document,
        diagnostics,
        *,
        created_at,
        durable_authority=None,
        lifetime_seconds=None,
    ):
        binding = _grant_binding(grant)
        if (
            type(review) is not EditableProfileReview
            or type(document) is not SafeDocumentBundleMetadata
            or diagnostics is not None
            and (
                type(diagnostics) is not tuple
                or not 1 <= len(diagnostics) <= 2
                or any(type(item) is not SafeModelDiagnostics for item in diagnostics)
            )
            or type(created_at) is not datetime
            or created_at.tzinfo is None
            or lifetime_seconds is not None
            and (
                type(lifetime_seconds) not in (int, float)
                or not math.isfinite(lifetime_seconds)
                or lifetime_seconds <= 0
            )
        ):
            raise _configuration_error()
        review_sources = {
            source.document_reference: source.document_kind.value
            for source in review.sources
        }
        document_sources = {
            item.document_reference: item.origin
            for item in document.documents
        }
        if review_sources != document_sources or (
            diagnostics is not None
            and tuple(item.document_kind for item in diagnostics)
            != tuple(item.origin for item in document.documents)
        ):
            raise _configuration_error()
        return self._issue_bound(
            binding,
            review,
            document,
            diagnostics,
            created_at=created_at,
            durable_authority=durable_authority,
            lifetime_seconds=lifetime_seconds,
        )

    def issue_resumed(
        self,
        grant,
        review,
        *,
        created_at,
        durable_authority,
        lifetime_seconds,
        review_step=PROFILE_INTAKE_DEFAULT_REVIEW_STEP,
    ):
        """Issue a new local draft from validated durable state only."""

        binding = _grant_binding(grant)
        if (
            type(review) is not EditableProfileReview
            or durable_authority is None
            or type(created_at) is not datetime
            or created_at.tzinfo is None
            or type(lifetime_seconds) not in (int, float)
            or not math.isfinite(lifetime_seconds)
            or lifetime_seconds <= 0
        ):
            raise _configuration_error()
        return self._issue_bound(
            binding,
            review,
            None,
            None,
            created_at=created_at,
            durable_authority=durable_authority,
            lifetime_seconds=lifetime_seconds,
            review_step=review_step,
        )

    def _issue_bound(
        self,
        binding,
        review,
        document,
        diagnostics,
        *,
        created_at,
        durable_authority,
        lifetime_seconds,
        review_step=PROFILE_INTAKE_DEFAULT_REVIEW_STEP,
    ):
        review_step = normalize_profile_intake_review_step(review_step)
        now = _monotonic(self._monotonic())
        with self._lock:
            if self._closed:
                raise ProfileIntakeError("draft_vault_unavailable")
            self._purge_locked(now)
            for reference, record in tuple(self._records.items()):
                if record.binding == binding:
                    del self._records[reference]
            if len(self._records) >= self._capacity:
                raise ProfileIntakeError("draft_vault_capacity")
            reference = None
            for _ in range(16):
                candidate = self._token_factory()
                if (
                    type(candidate) is str
                    and _OPAQUE_REFERENCE.fullmatch(candidate) is not None
                    and candidate not in self._records
                ):
                    reference = candidate
                    break
            if reference is None:
                raise ProfileIntakeError("draft_vault_unavailable")
            absolute_lifetime = self._absolute_ttl
            if lifetime_seconds is not None:
                absolute_lifetime = min(absolute_lifetime, float(lifetime_seconds))
            lifetime = min(self._ttl, absolute_lifetime)
            snapshot = IntakeDraftSnapshot(
                review=review,
                review_step=review_step,
                document=document,
                diagnostics=diagnostics,
                created_at=created_at.astimezone(timezone.utc).isoformat(),
                expires_at_monotonic=now + lifetime,
                absolute_expires_at_monotonic=now + absolute_lifetime,
                last_renewed_at_monotonic=now,
                version=1,
            )
            self._records[reference] = _DraftRecord(
                binding=binding,
                snapshot=snapshot,
                durable_authority=durable_authority,
            )
            return reference, snapshot

    def get(self, reference, grant):
        state, value = self.lookup(reference, grant)
        return value if state == "active" else None

    def expire_bound(self, grant):
        """Discard unusable drafts for one binding and return sealed authorities."""

        binding = _grant_binding(grant)
        now = _monotonic(self._monotonic())
        expired = []
        with self._lock:
            for reference, record in tuple(self._records.items()):
                if record.binding != binding or record.state == "completed":
                    continue
                if (
                    record.snapshot is None
                    or now >= record.snapshot.expires_at_monotonic
                ):
                    if record.durable_authority is not None:
                        expired.append(record.durable_authority)
                    del self._records[reference]
            self._purge_locked(now)
        return tuple(expired)

    def lookup(self, reference, grant):
        """Return active state, a minimal completion receipt, or an expiry."""

        binding = _grant_binding(grant)
        if type(reference) is not str or _OPAQUE_REFERENCE.fullmatch(reference) is None:
            return "gone", None
        now = _monotonic(self._monotonic())
        with self._lock:
            record = self._records.get(reference)
            if record is None or record.binding != binding:
                self._purge_locked(now)
                return "gone", None
            if record.state == "completed":
                if (
                    record.completion_expires_at_monotonic is None
                    or now >= record.completion_expires_at_monotonic
                ):
                    del self._records[reference]
                    self._purge_locked(now)
                    return "gone", None
                self._purge_locked(now, skip_reference=reference)
                return "completed", record
            if record.snapshot is None:
                del self._records[reference]
                self._purge_locked(now)
                return "gone", None
            if now >= record.snapshot.expires_at_monotonic:
                expired_authority = record.durable_authority
                del self._records[reference]
                self._purge_locked(now)
                return "expired", expired_authority
            self._purge_locked(now, skip_reference=reference)
            return "active", record.snapshot

    def renew(self, reference, grant, *, expected_version):
        """Refresh one correctly bound active draft without accepting content."""

        binding = _grant_binding(grant)
        if (
            type(reference) is not str
            or _OPAQUE_REFERENCE.fullmatch(reference) is None
            or type(expected_version) is not int
            or expected_version < 1
        ):
            return "gone", None
        now = _monotonic(self._monotonic())
        with self._lock:
            record = self._records.get(reference)
            if record is None or record.binding != binding:
                self._purge_locked(now)
                return "gone", None
            if record.state != "active" or record.snapshot is None:
                self._purge_locked(now, skip_reference=reference)
                return "stale", None
            snapshot = record.snapshot
            if now >= snapshot.expires_at_monotonic:
                authority = record.durable_authority
                del self._records[reference]
                self._purge_locked(now)
                return "expired", authority
            if snapshot.version != expected_version:
                self._purge_locked(now, skip_reference=reference)
                return "stale", None
            state = "rate_limited"
            if now - snapshot.last_renewed_at_monotonic >= self._renewal_interval:
                snapshot = self._refresh_snapshot(snapshot, now)
                self._records[reference] = replace(record, snapshot=snapshot)
                state = "renewed"
            self._purge_locked(now, skip_reference=reference)
            return state, (
                snapshot,
                max(0, int(snapshot.expires_at_monotonic - now)),
                max(0, int(snapshot.absolute_expires_at_monotonic - now)),
                record.durable_authority,
            )

    def replace_durable_authority(
        self, reference, grant, *, expected_version, authority
    ):
        """Replace only the sealed server-side capability for one live draft."""

        binding = _grant_binding(grant)
        now = _monotonic(self._monotonic())
        with self._lock:
            record = self._records.get(reference)
            if (
                record is None
                or record.binding != binding
                or record.state != "active"
                or record.snapshot is None
                or record.snapshot.version != expected_version
                or now >= record.snapshot.expires_at_monotonic
            ):
                self._purge_locked(now)
                return False
            self._records[reference] = replace(
                record, durable_authority=authority
            )
            return True

    def update(self, reference, grant, *, expected_version, review):
        binding = _grant_binding(grant)
        if (
            type(reference) is not str
            or _OPAQUE_REFERENCE.fullmatch(reference) is None
            or type(expected_version) is not int
            or expected_version < 1
            or type(review) is not EditableProfileReview
        ):
            return "gone", None
        now = _monotonic(self._monotonic())
        with self._lock:
            record = self._records.get(reference)
            if record is None or record.binding != binding:
                self._purge_locked(now)
                return "gone", None
            if record.state != "active" or record.snapshot is None:
                self._purge_locked(now, skip_reference=reference)
                return "stale", None
            if now >= record.snapshot.expires_at_monotonic:
                authority = record.durable_authority
                del self._records[reference]
                self._purge_locked(now)
                return "expired", authority
            if record.snapshot.version != expected_version:
                self._purge_locked(now, skip_reference=reference)
                return "stale", None
            snapshot = replace(
                self._refresh_snapshot(record.snapshot, now),
                review=review,
                version=expected_version + 1,
            )
            self._records[reference] = replace(record, snapshot=snapshot)
            self._purge_locked(now, skip_reference=reference)
            return "updated", snapshot

    def checkpoint_update_material(self, reference, grant, *, expected_version):
        """Return the current sealed authority for one optimistic autosave."""

        binding = _grant_binding(grant)
        now = _monotonic(self._monotonic())
        with self._lock:
            record = self._records.get(reference)
            if record is None or record.binding != binding:
                self._purge_locked(now)
                return "gone", None
            if record.state != "active" or record.snapshot is None:
                self._purge_locked(now, skip_reference=reference)
                return "stale", None
            if now >= record.snapshot.expires_at_monotonic:
                authority = record.durable_authority
                del self._records[reference]
                self._purge_locked(now)
                return "expired", authority
            if record.snapshot.version != expected_version:
                self._purge_locked(now, skip_reference=reference)
                return "stale", None
            return "active", (record.snapshot, record.durable_authority)

    def complete_checkpoint_update(
        self,
        reference,
        grant,
        *,
        expected_version,
        review,
        review_step,
        durable_authority,
    ):
        """Publish one durable save only if the local optimistic version still wins."""

        binding = _grant_binding(grant)
        now = _monotonic(self._monotonic())
        with self._lock:
            record = self._records.get(reference)
            if record is None or record.binding != binding:
                self._purge_locked(now)
                return "gone", None
            if (
                record.state != "active"
                or record.snapshot is None
                or record.snapshot.version != expected_version
                or now >= record.snapshot.expires_at_monotonic
            ):
                self._purge_locked(now, skip_reference=reference)
                return "stale", None
            snapshot = replace(
                self._refresh_snapshot(record.snapshot, now),
                review=review,
                review_step=normalize_profile_intake_review_step(review_step),
                version=expected_version + 1,
            )
            self._records[reference] = replace(
                record,
                snapshot=snapshot,
                durable_authority=durable_authority,
            )
            self._purge_locked(now, skip_reference=reference)
            return "updated", snapshot

    def invalidate(self, reference, grant, *, expected_version):
        """Drop only stale process-local state; never discard durable progress."""

        binding = _grant_binding(grant)
        with self._lock:
            record = self._records.get(reference)
            if (
                record is not None
                and record.binding == binding
                and record.snapshot is not None
                and record.snapshot.version == expected_version
            ):
                del self._records[reference]
                return True
            return False

    def discard_lineage(self, grant):
        """Remove local drafts for the current lineage after explicit discard."""

        binding = _grant_binding(grant)
        lineage_binding = binding[:1] + binding[2:]
        removed = 0
        with self._lock:
            for reference, record in tuple(self._records.items()):
                candidate = record.binding[:1] + record.binding[2:]
                if candidate == lineage_binding:
                    del self._records[reference]
                    removed += 1
        return removed

    def _refresh_snapshot(self, snapshot, now):
        if now - snapshot.last_renewed_at_monotonic < self._renewal_interval:
            return snapshot
        return replace(
            snapshot,
            expires_at_monotonic=min(
                now + self._ttl,
                snapshot.absolute_expires_at_monotonic,
            ),
            last_renewed_at_monotonic=now,
        )

    def cancel(self, reference, grant, *, expected_version):
        state, _authority = self.cancel_bound(
            reference,
            grant,
            expected_version=expected_version,
        )
        return state

    def cancel_bound(self, reference, grant, *, expected_version):
        binding = _grant_binding(grant)
        if type(reference) is not str or _OPAQUE_REFERENCE.fullmatch(reference) is None:
            return "gone", None
        now = _monotonic(self._monotonic())
        with self._lock:
            self._purge_locked(now)
            record = self._records.get(reference)
            if record is None or record.binding != binding:
                return "gone", None
            if record.state != "active" or record.snapshot is None:
                return "stale", None
            if record.snapshot.version != expected_version:
                return "stale", None
            authority = record.durable_authority
            del self._records[reference]
            return "cancelled", authority

    def begin_save(
        self,
        reference,
        grant,
        *,
        expected_version,
        review,
        confirmation_fingerprint,
        request_digest,
    ):
        binding = _grant_binding(grant)
        if (
            type(reference) is not str
            or _OPAQUE_REFERENCE.fullmatch(reference) is None
            or type(expected_version) is not int
            or expected_version < 1
            or type(review) is not EditableProfileReview
            or not _is_sha256(confirmation_fingerprint)
            or not _is_sha256(request_digest)
        ):
            return "gone", None
        now = _monotonic(self._monotonic())
        with self._lock:
            self._purge_locked(now)
            record = self._records.get(reference)
            if record is None or record.binding != binding:
                return "gone", None
            if record.state == "completed":
                if (
                    record.save_request_digest == request_digest
                    and record.confirmation_fingerprint == confirmation_fingerprint
                ):
                    return "completed", None
                return "stale", None
            if record.snapshot is None or record.snapshot.version != expected_version:
                return "stale", None
            if record.state == "committing":
                if (
                    record.save_request_digest == request_digest
                    and record.confirmation_fingerprint == confirmation_fingerprint
                ):
                    return "replay", (record.snapshot, record.durable_authority)
                return "stale", None
            if record.state != "active":
                return "stale", None
            snapshot = replace(record.snapshot, review=review)
            self._records[reference] = replace(
                record,
                snapshot=snapshot,
                state="committing",
                confirmation_fingerprint=confirmation_fingerprint,
                save_request_digest=request_digest,
            )
            return "ready", (snapshot, record.durable_authority)

    def save_authority(self, reference, grant, *, expected_version):
        binding = _grant_binding(grant)
        now = _monotonic(self._monotonic())
        with self._lock:
            self._purge_locked(now)
            record = self._records.get(reference)
            if (
                record is None
                or record.binding != binding
                or record.snapshot is None
                or record.snapshot.version != expected_version
                or record.state not in {"active", "committing"}
            ):
                return None
            return record.durable_authority

    def complete_save(
        self,
        reference,
        grant,
        *,
        confirmation_fingerprint,
        request_digest,
    ):
        binding = _grant_binding(grant)
        now = _monotonic(self._monotonic())
        with self._lock:
            record = self._records.get(reference)
            if (
                record is None
                or record.binding != binding
                or record.state != "committing"
                or record.snapshot is None
                or record.confirmation_fingerprint != confirmation_fingerprint
                or record.save_request_digest != request_digest
            ):
                return False
            receipt_lifetime = min(
                PROFILE_INTAKE_COMPLETION_RECEIPT_SECONDS,
                max(1.0, record.snapshot.expires_at_monotonic - now),
            )
            self._records[reference] = replace(
                record,
                snapshot=None,
                durable_authority=None,
                state="completed",
                confirmation_fingerprint=None,
                completion_expires_at_monotonic=now + receipt_lifetime,
            )
            return True

    def completion_matches(self, reference, grant, *, expected_version, request_digest):
        state, value = self.lookup(reference, grant)
        if state != "completed" or value is None:
            return state
        if (
            type(expected_version) is not int
            or expected_version < 1
            or not _is_sha256(request_digest)
        ):
            return "stale"
        return (
            "completed"
            if value.save_request_digest == request_digest
            else "stale"
        )

    def _purge_locked(self, now, *, skip_reference=None):
        for reference, record in tuple(self._records.items()):
            if reference == skip_reference:
                continue
            expired = (
                record.state == "completed"
                and (
                    record.completion_expires_at_monotonic is None
                    or now >= record.completion_expires_at_monotonic
                )
            ) or (
                record.state != "completed"
                and (
                    record.snapshot is None
                    or now >= record.snapshot.expires_at_monotonic
                )
            )
            if expired:
                del self._records[reference]


class ProfileIntakeProcessingService:
    """Run the ephemeral extraction pipeline outside every DB transaction."""

    __slots__ = (
        "_adapter",
        "_clock",
        "_durable",
        "_guard",
        "_in_flight",
        "_save_failure_injector",
        "_vault",
    )

    def __init__(
        self,
        *,
        adapter,
        vault,
        clock,
        durable_finalizer=None,
        save_failure_injector=None,
    ):
        if (
            adapter is not None
            and not callable(getattr(adapter, "extract", None))
        ) or type(vault) is not IntakeDraftVault or not callable(clock) or (
            durable_finalizer is not None
            and not all(
                callable(getattr(durable_finalizer, name, None))
                for name in (
                    "preflight",
                    "checkpoint_summary",
                    "reserve",
                    "resume",
                    "save_checkpoint",
                    "renew",
                    "release",
                    "discard_checkpoint",
                    "discard_saved_checkpoint",
                    "prepare",
                    "commit",
                )
            )
        ) or (save_failure_injector is not None and not callable(save_failure_injector)):
            raise _configuration_error()
        self._adapter = adapter
        self._vault = vault
        self._clock = clock
        self._durable = durable_finalizer
        self._save_failure_injector = save_failure_injector
        self._guard = threading.Lock()
        self._in_flight = set()

    @property
    def vault(self):
        return self._vault

    @property
    def durable_save_enabled(self):
        return self._durable is not None

    def preflight(self, grant):
        _grant_binding(grant)
        self._vault.expire_bound(grant)
        if self._durable is None:
            return "eligible"
        return self._durable.preflight(grant)

    def saved_progress(self, grant):
        """Return only presentation-safe checkpoint timing, never its authority."""

        _grant_binding(grant)
        if self._durable is None:
            return None
        summary = self._durable.checkpoint_summary(grant)
        if summary is None:
            return None
        now = self._clock()
        if type(now) is not datetime or now.tzinfo is None:
            raise ProfileIntakeError("durable_intake_unavailable")
        try:
            saved_at = datetime.fromisoformat(summary.review_saved_at)
            expires_at = datetime.fromisoformat(summary.checkpoint.expires_at)
            age_seconds = max(0, int((now - saved_at).total_seconds()))
            expires_in_seconds = int((expires_at - now).total_seconds())
        except (TypeError, ValueError, OverflowError):
            raise ProfileIntakeError("durable_intake_unavailable") from None
        if expires_in_seconds <= 0:
            return None
        return SavedProfileIntakeProgress(
            created_at=summary.created_at,
            saved_at=summary.review_saved_at,
            age_seconds=age_seconds,
            expires_in_seconds=expires_in_seconds,
        )

    def resume_saved(self, grant):
        """Hydrate a fresh local review from the current durable checkpoint."""

        _grant_binding(grant)
        if self._durable is None:
            raise ProfileIntakeError("ai_import_schema_unavailable")
        summary = self._durable.checkpoint_summary(grant)
        if summary is None:
            raise ProfileIntakeError("ai_import_checkpoint_expired")
        review, authority, lifetime_seconds, review_step = self._durable.resume(
            grant,
            summary.checkpoint.checkpoint_id,
            expected_version=summary.checkpoint.row_version,
        )
        return self._vault.issue_resumed(
            grant,
            review,
            created_at=self._clock(),
            durable_authority=authority,
            lifetime_seconds=lifetime_seconds,
            review_step=review_step,
        )

    def discard_saved(self, grant):
        """Explicitly destroy the current checkpoint after revalidation."""

        _grant_binding(grant)
        if self._durable is None:
            raise ProfileIntakeError("ai_import_schema_unavailable")
        summary = self._durable.checkpoint_summary(grant)
        if summary is None:
            return "gone"
        self._durable.discard_saved_checkpoint(grant, summary.checkpoint)
        self._vault.discard_lineage(grant)
        return "discarded"

    def lookup(self, reference, grant):
        state, value = self._vault.lookup(reference, grant)
        return ("gone", None) if state == "expired" else (state, value)

    def autosave(
        self,
        reference,
        grant,
        *,
        expected_version,
        review,
        review_step=PROFILE_INTAKE_DEFAULT_REVIEW_STEP,
    ):
        """Validate and durably save one optimistic full-review replacement."""

        if self._durable is None or type(review) is not EditableProfileReview:
            raise ProfileIntakeError("ai_import_schema_unavailable")
        state, material = self._vault.checkpoint_update_material(
            reference,
            grant,
            expected_version=expected_version,
        )
        if state == "expired":
            raise ProfileIntakeError("draft_expired")
        if state == "stale":
            raise ProfileIntakeError("stale_review")
        if state != "active" or material is None:
            raise ProfileIntakeError("draft_expired")
        snapshot, authority = material
        review_step = normalize_profile_intake_review_step(review_step)
        if review == snapshot.review and review_step == snapshot.review_step:
            return "unchanged", snapshot
        try:
            durable_authority = self._durable.save_checkpoint(
                grant,
                authority,
                review,
                review_step=review_step,
            )
        except ProfileIntakeError as exc:
            if exc.code == "stale_review":
                self._vault.invalidate(
                    reference,
                    grant,
                    expected_version=expected_version,
                )
            raise
        state, updated = self._vault.complete_checkpoint_update(
            reference,
            grant,
            expected_version=expected_version,
            review=review,
            review_step=review_step,
            durable_authority=durable_authority,
        )
        if state != "updated" or updated is None:
            self._vault.invalidate(
                reference,
                grant,
                expected_version=expected_version,
            )
            raise ProfileIntakeError("stale_review")
        return "saved", updated

    def renew(self, reference, grant, *, expected_version):
        state, value = self._vault.renew(
            reference,
            grant,
            expected_version=expected_version,
        )
        if state in {"renewed", "rate_limited"} and value is not None:
            snapshot, idle_seconds, absolute_seconds, authority = value
            if self._durable is not None:
                try:
                    renewed_authority = self._durable.renew(grant, authority)
                except ProfileIntakeError:
                    return "gone", None
                if not self._vault.replace_durable_authority(
                    reference,
                    grant,
                    expected_version=expected_version,
                    authority=renewed_authority,
                ):
                    return "stale", None
            value = snapshot, idle_seconds, absolute_seconds
        return ("gone", None) if state == "expired" else (state, value)

    def update(self, reference, grant, *, expected_version, review):
        state, value = self._vault.update(
            reference,
            grant,
            expected_version=expected_version,
            review=review,
        )
        return ("gone", None) if state == "expired" else (state, value)

    def cancel(self, reference, grant, *, expected_version):
        state, authority = self._vault.cancel_bound(
            reference,
            grant,
            expected_version=expected_version,
        )
        if state == "cancelled" and authority is not None and self._durable is not None:
            try:
                self._durable.discard_checkpoint(grant, authority)
            except ProfileIntakeError:
                pass
        return state

    def completion_matches(self, reference, grant, *, expected_version, request_digest):
        return self._vault.completion_matches(
            reference,
            grant,
            expected_version=expected_version,
            request_digest=request_digest,
        )

    def process(self, grant, document_bytes, *, document_kind, document_format):
        """Backward-compatible single-document bundle entry point."""

        return self.process_bundle(
            grant,
            (
                ProfileIntakeDocumentInput(
                    document_bytes=document_bytes,
                    document_kind=document_kind,
                    document_format=document_format,
                ),
            ),
        )

    def process_bundle(self, grant, documents):
        """Atomically create one draft from one or two independent sources."""

        binding = _grant_binding(grant)
        if (
            type(documents) is not tuple
            or not 1 <= len(documents) <= 2
            or any(type(item) is not ProfileIntakeDocumentInput for item in documents)
            or len({item.document_kind for item in documents}) != len(documents)
        ):
            raise ProfileIntakeError("invalid_profile_intake_bundle")
        if self._adapter is None:
            raise ProfileIntakeError("profile_extraction_unavailable")
        if self._durable is not None:
            state = self._durable.preflight(grant)
            if state != "eligible":
                raise ProfileIntakeError("ai_import_" + state)
        with self._guard:
            if binding in self._in_flight:
                raise ProfileIntakeError("profile_intake_in_flight")
            self._in_flight.add(binding)
        raw_document = None
        raw_evidence = None
        model_evidence = None
        extraction = None
        validated_sources = []
        metadata_items = []
        diagnostic_items = []
        durable_authority = None
        try:
            ordered_documents = tuple(
                sorted(
                    documents,
                    key=lambda item: (
                        0
                        if item.document_kind is DocumentKind.RESUME
                        else 1
                    ),
                )
            )
            for item in ordered_documents:
                raw_document = extract_profile_document(
                    item.document_bytes,
                    document_reference=new_document_reference(),
                    document_kind=item.document_kind,
                    document_format=item.document_format,
                )
                raw_evidence = raw_document.evidence_packet()
                model_evidence = minimize_evidence_packet(raw_evidence)
                metadata_items.append(
                    SafeDocumentMetadata(
                        document_reference=raw_document.document_reference,
                        origin=raw_document.document_kind.value,
                        format=raw_document.document_format.value,
                        byte_size=raw_document.original_byte_size,
                        page_count=raw_document.page_count,
                        parser=raw_document.parser.parser,
                        parser_version=raw_document.parser.version,
                    )
                )
                raw_evidence = None
                raw_document = None
                extractor = getattr(self._adapter, "extract_with_diagnostics", None)
                if callable(extractor):
                    outcome = extractor(model_evidence)
                    if type(outcome) is not ProfileExtractionOutcome:
                        raise ProfileIntakeError("invalid_profile_extraction")
                    extraction = outcome.extraction
                    diagnostic_items.append(
                        _safe_diagnostics(outcome.diagnostics, item.document_kind)
                    )
                else:
                    extraction = self._adapter.extract(model_evidence)
                extraction = validate_ai_profile_extraction(
                    _extraction_mapping(extraction),
                    model_evidence,
                )
                validated_sources.append(
                    ValidatedProfileSource(
                        document_kind=item.document_kind,
                        extraction=extraction,
                    )
                )
                extraction = None
                model_evidence = None
            draft = reconcile_profile_extractions(tuple(validated_sources))
            review = editable_profile_review(draft)
            document_metadata = SafeDocumentBundleMetadata(tuple(metadata_items))
            diagnostics = tuple(diagnostic_items) if diagnostic_items else None
            lifetime_seconds = None
            if self._durable is not None:
                durable_authority, lifetime_seconds = self._durable.reserve(
                    grant,
                    review,
                    document_metadata,
                    diagnostics,
                )
                if (
                    type(lifetime_seconds) not in (int, float)
                    or not math.isfinite(lifetime_seconds)
                    or lifetime_seconds < PROFILE_INTAKE_MIN_SAFE_REVIEW_SECONDS
                ):
                    try:
                        self._durable.discard_checkpoint(grant, durable_authority)
                    finally:
                        durable_authority = None
                    raise ProfileIntakeError("ai_import_lease_unavailable")
            try:
                issued = self._vault.issue(
                    grant,
                    review,
                    document_metadata,
                    diagnostics,
                    created_at=self._clock(),
                    durable_authority=durable_authority,
                    lifetime_seconds=lifetime_seconds,
                )
            except Exception:
                if durable_authority is not None and self._durable is not None:
                    try:
                        self._durable.discard_checkpoint(grant, durable_authority)
                    except ProfileIntakeError:
                        pass
                raise
            durable_authority = None
            return issued
        finally:
            extraction = None
            model_evidence = None
            raw_evidence = None
            raw_document = None
            validated_sources.clear()
            metadata_items.clear()
            diagnostic_items.clear()
            documents = None
            with self._guard:
                self._in_flight.discard(binding)

    def save(
        self,
        reference,
        grant,
        *,
        expected_version,
        review,
        request_digest,
    ):
        if self._durable is None:
            raise ProfileIntakeError("ai_import_schema_unavailable")
        state, snapshot = self.lookup(reference, grant)
        if state != "active" or snapshot is None:
            raise ProfileIntakeError(
                "ai_import_save_completed" if state == "completed" else "draft_expired"
            )
        if snapshot.version != expected_version:
            raise ProfileIntakeError("stale_review")
        authority = self._vault.save_authority(
            reference,
            grant,
            expected_version=expected_version,
        )
        if authority is None:
            raise ProfileIntakeError("invalid_durable_intake_authority")
        confirmed = self._durable.prepare(review, authority)
        confirmation_fingerprint = confirmed.confirmation_fingerprint
        begin_state, commit_material = self._vault.begin_save(
            reference,
            grant,
            expected_version=expected_version,
            review=review,
            confirmation_fingerprint=confirmation_fingerprint,
            request_digest=request_digest,
        )
        if begin_state == "completed":
            return "replayed", None
        if begin_state != "ready" and begin_state != "replay":
            raise ProfileIntakeError(
                "stale_review" if begin_state == "stale" else "draft_expired"
            )
        _committed_snapshot, authority = commit_material
        result = self._durable.commit(grant, authority, confirmed)
        _save_hook(self._save_failure_injector, "after_durable_commit")
        _save_hook(self._save_failure_injector, "before_vault_completion")
        if not self._vault.complete_save(
            reference,
            grant,
            confirmation_fingerprint=confirmation_fingerprint,
            request_digest=request_digest,
        ):
            raise ProfileIntakeError("durable_intake_unavailable")
        return "replayed" if result.replayed else "saved", result


def editable_profile_review(draft):
    if type(draft) is not AIProfileReviewDraft:
        raise _configuration_error()
    facts = tuple(
        EditableReviewFact(
            field_path=fact.field_path,
            review_field=fact.review_field,
            value=fact.value,
            source_attributions=fact.source_attributions,
            suggested=suggested,
            decision="pending" if suggested else "keep",
            conflict_group=fact.conflict_group,
            explicit=fact.explicit,
        )
        for suggested, collection in (
            (False, draft.prefilled_facts),
            (True, draft.suggested_facts),
        )
        for fact in collection
    )
    missing_user_fields = tuple(
        name
        for name in draft.missing_user_fields
        if name not in _TYPED_PREFERENCE_REPLACED_USER_FIELDS
    )
    preference_model = empty_profile_preferences_v2()
    return EditableProfileReview(
        schema_version=draft.schema_version,
        sources=draft.sources,
        facts=facts,
        missing_user_fields=missing_user_fields,
        user_inputs=tuple((name, "") for name in missing_user_fields),
        issue_count=len(draft.issues),
        _preference_model_json=_preference_model_json(preference_model),
        education_entries=_associate_education_entries(facts),
        reset_baseline=_reset_baseline_for_facts(facts),
    )


def _reset_baseline_for_facts(facts):
    """Capture the original candidate-facing suggestion state exactly once."""

    return tuple(
        EditableResetBaselineEntry(section_id, index, fact.value, fact.decision)
        for index, fact in enumerate(facts)
        if (section_id := _review_reset_section_for_fact(fact)) is not None
    )


def _valid_reset_baseline(facts, baseline):
    """Validate complete per-section snapshots while allowing older checkpoints.

    A section is either absent (so Reset is unavailable) or represented by every
    fact belonging to that section.  This lets pre-feature checkpoints resume
    without pretending their edited values are an original extraction baseline.
    """

    if type(facts) is not tuple or type(baseline) is not tuple:
        return False
    if any(type(entry) is not EditableResetBaselineEntry for entry in baseline):
        return False
    seen = set()
    represented_sections = {entry.section_id for entry in baseline}
    for entry in baseline:
        identity = (entry.section_id, entry.fact_index)
        if identity in seen or not 0 <= entry.fact_index < len(facts):
            return False
        seen.add(identity)
        fact = facts[entry.fact_index]
        if _review_reset_section_for_fact(fact) != entry.section_id:
            return False
        allowed = {"pending", "accept", "reject"} if fact.suggested else {"keep", "remove"}
        if entry.decision not in allowed:
            return False
        try:
            if (
                _parse_review_value(
                    fact.field_path, review_value_for_form(entry.value)
                )
                != entry.value
            ):
                return False
        except ProfileIntakeError:
            return False
    for section_id in represented_sections:
        expected = {
            index
            for index, fact in enumerate(facts)
            if _review_reset_section_for_fact(fact) == section_id
        }
        actual = {
            entry.fact_index for entry in baseline if entry.section_id == section_id
        }
        if actual != expected:
            return False
    return len(seen) == len(baseline)


def review_reset_section_available(review, section_id):
    """Return whether this intake has a genuine immutable baseline for a section."""

    if (
        type(review) is not EditableProfileReview
        or type(section_id) is not str
        or section_id not in PROFILE_INTAKE_RESET_SECTIONS
        or not _valid_reset_baseline(review.facts, review.reset_baseline)
    ):
        return False
    return any(entry.section_id == section_id for entry in review.reset_baseline)


def _education_entry_mapping(entry):
    return {
        "kind": entry.kind,
        "qualification": entry.qualification,
        "field": entry.field_of_study,
        "institution": entry.institution,
        "status": entry.status,
        "completion_year": entry.completion_year,
    }


def education_entry_values(review):
    if type(review) is not EditableProfileReview or not _valid_education_entries(
        review.facts,
        review.education_entries,
    ):
        raise ProfileIntakeError("invalid_review_submission")
    return tuple(
        {
            "item_reference": entry.item_reference,
            "origin": entry.origin,
            "value": _education_entry_mapping(entry),
            "source_attributions": entry.source_attributions,
            "decision": entry.decision,
        }
        for entry in review.education_entries
    )


def education_entry_field_authorities(review):
    """Return closed field-level authority for active entries in canonical order."""

    if type(review) is not EditableProfileReview or not _valid_education_entries(
        review.facts,
        review.education_entries,
    ):
        raise ProfileIntakeError("invalid_review_submission")
    active = tuple(entry for entry in review.education_entries if entry.decision == "keep")
    by_identity = {
        education_entry_identity(_education_entry_mapping(entry)): entry
        for entry in active
    }
    try:
        canonical_entries = canonicalize_education_entries_v1(
            [_education_entry_mapping(entry) for entry in active]
        )
    except EducationEntryContractError:
        raise ProfileIntakeError("invalid_review_submission") from None
    result = []
    for value in canonical_entries:
        entry = by_identity.get(education_entry_identity(value))
        if entry is None:
            raise ProfileIntakeError("invalid_review_submission")
        source_facts = {
            _EDUCATION_ENTRY_COMPONENTS[review.facts[index].field_path]: review.facts[index]
            for index in entry.source_fact_indexes
        }
        fields = {}
        for component in _EDUCATION_ENTRY_FIELDS:
            if entry.origin == "user":
                authority = (PROFILE_SOURCE_USER_CONFIRMATION, True)
            elif component in entry.candidate_edited_components:
                authority = (PROFILE_SOURCE_USER_CORRECTION, True)
            elif (
                entry.extraction_components is not None
                and component in entry.extraction_components
                and component in source_facts
            ):
                authority = (PROFILE_SOURCE_RESUME, source_facts[component].explicit)
            else:
                # Older checkpoints did not retain mutation-aware education
                # metadata. Conservatively avoid claiming document authority.
                authority = (PROFILE_SOURCE_USER_CONFIRMATION, True)
            fields[component] = {
                "source_kind": authority[0],
                "explicit": authority[1],
            }
        result.append(fields)
    return tuple(result)


def managed_education_fact_indexes(review):
    if type(review) is not EditableProfileReview or not _valid_education_entries(
        review.facts,
        review.education_entries,
    ):
        raise ProfileIntakeError("invalid_review_submission")
    return frozenset(
        index
        for entry in review.education_entries
        for index in entry.source_fact_indexes
    )


def _associate_education_entries(facts):
    groups = {}
    for index, fact in enumerate(facts):
        if (
            fact.field_path not in _EDUCATION_ENTRY_COMPONENTS
            or fact.conflict_group is not None
            or len(fact.source_attributions) != 1
            or len(fact.source_attributions[0].evidence_block_references) != 1
        ):
            continue
        attribution = fact.source_attributions[0]
        key = (
            attribution.document_kind.value,
            attribution.document_reference,
            attribution.evidence_block_references[0],
        )
        groups.setdefault(key, []).append(index)

    entries = []
    for key, indexes in sorted(groups.items()):
        if len(entries) >= MAX_EDUCATION_ENTRIES:
            break
        components = {}
        duplicate_component = False
        for index in indexes:
            component = _EDUCATION_ENTRY_COMPONENTS[facts[index].field_path]
            if component in components:
                duplicate_component = True
                break
            components[component] = index
        if (
            duplicate_component
            or len(components) < 2
            or not {"qualification", "field", "institution"}.intersection(components)
        ):
            continue
        value = {
            "kind": "not_specified",
            "qualification": "",
            "field": "",
            "institution": "",
            "status": "not_specified",
            "completion_year": None,
        }
        for component, index in components.items():
            value[component] = facts[index].value
        try:
            value = canonicalize_education_entries_v1([value])[0]
            entry = EditableEducationEntry(
                item_reference=f"edu_{len(entries):03d}",
                origin="document",
                kind=value["kind"],
                qualification=value["qualification"],
                field_of_study=value["field"],
                institution=value["institution"],
                status=value["status"],
                completion_year=value["completion_year"],
                source_fact_indexes=tuple(sorted(components.values())),
                source_attributions=(facts[indexes[0]].source_attributions[0],),
                decision="keep",
                extraction_components=tuple(sorted(components)),
            )
        except (EducationEntryContractError, ProfileIntakeError):
            continue
        entries.append(entry)
    return tuple(entries)


def _valid_education_entries(facts, entries):
    if type(facts) is not tuple or type(entries) is not tuple:
        return False
    used_indexes = set()
    active_identities = set()
    for ordinal, entry in enumerate(entries):
        if (
            type(entry) is not EditableEducationEntry
            or entry.item_reference != f"edu_{ordinal:03d}"
        ):
            return False
        if entry.decision == "keep":
            try:
                identity = education_entry_identity(_education_entry_mapping(entry))
            except EducationEntryContractError:
                return False
            if identity in active_identities:
                return False
            active_identities.add(identity)
        if entry.origin == "user":
            if entry.extraction_components not in {None, ()}:
                return False
            continue
        if (
            tuple(sorted(entry.source_fact_indexes)) != entry.source_fact_indexes
            or len(set(entry.source_fact_indexes)) != len(entry.source_fact_indexes)
            or used_indexes.intersection(entry.source_fact_indexes)
        ):
            return False
        components = set()
        attributions = []
        for index in entry.source_fact_indexes:
            if type(index) is not int or not 0 <= index < len(facts):
                return False
            fact = facts[index]
            component = _EDUCATION_ENTRY_COMPONENTS.get(fact.field_path)
            if component is None or component in components or fact.conflict_group is not None:
                return False
            components.add(component)
            attributions.extend(fact.source_attributions)
        if (
            entry.extraction_components is not None
            and set(entry.extraction_components) != components
        ):
            return False
        expected_attributions = tuple(
            sorted(
                set(attributions),
                key=lambda item: (
                    item.document_kind.value,
                    item.document_reference,
                    item.evidence_block_references,
                ),
            )
        )
        if expected_attributions != entry.source_attributions:
            return False
        used_indexes.update(entry.source_fact_indexes)
    return len(entries) <= MAX_EDUCATION_ENTRIES


def review_collection_entries(review, collection_id):
    """Return one closed server-owned view of a supported review collection."""

    if type(review) is not EditableProfileReview:
        raise ProfileIntakeError("invalid_review_submission")
    spec = PROFILE_INTAKE_REVIEW_COLLECTIONS.get(collection_id)
    if spec is None:
        raise ProfileIntakeError("invalid_review_submission")
    entries = []
    for index, fact in enumerate(review.facts):
        if (
            fact.field_path in spec["paths"]
            and fact.conflict_group is None
            and (not fact.suggested or spec.get("include_suggested") is True)
        ):
            entries.append(
                {
                    "origin": "document",
                    "index": index,
                    "members": (("document", index),),
                    "value": fact.value,
                    "decision": _collection_decision_for_fact(fact),
                    "requires_confirmation": fact.decision == "pending",
                    "suggested": fact.suggested,
                    "mixed_decisions": False,
                    "source_attributions": fact.source_attributions,
                    "field_paths": (fact.field_path,),
                }
            )
    for index, fact in enumerate(review.user_facts):
        if fact.collection_id == collection_id:
            entries.append(
                {
                    "origin": "user",
                    "index": index,
                    "members": (("user", index),),
                    "value": fact.value,
                    "decision": fact.decision,
                    "requires_confirmation": False,
                    "suggested": False,
                    "mixed_decisions": False,
                    "source_attributions": (),
                    "field_paths": (fact.field_path,),
                }
            )
    if spec.get("deduplicate") is not True:
        return tuple(entries)
    grouped = []
    grouped_by_identity = {}
    for entry in entries:
        identity = _review_collection_identity(entry["value"])
        current = grouped_by_identity.get(identity)
        if current is None:
            current = dict(entry)
            grouped_by_identity[identity] = current
            grouped.append(current)
            continue
        current["members"] = (*current["members"], *entry["members"])
        current["source_attributions"] = tuple(
            dict.fromkeys((*current["source_attributions"], *entry["source_attributions"]))
        )
        current["field_paths"] = tuple(
            dict.fromkeys((*current["field_paths"], *entry["field_paths"]))
        )
        current["requires_confirmation"] = bool(
            current["requires_confirmation"] or entry["requires_confirmation"]
        )
        current["suggested"] = bool(current["suggested"] or entry["suggested"])
        decisions = {current["decision"], entry["decision"]}
        current["mixed_decisions"] = bool(
            current["mixed_decisions"]
            or entry["mixed_decisions"]
            or len(decisions) > 1
        )
        current["decision"] = (
            "pending"
            if "pending" in decisions
            else "keep"
            if "keep" in decisions
            else "remove"
        )
        if current["origin"] != entry["origin"]:
            current["origin"] = "mixed"
    return tuple(grouped)


def _collection_decision_for_fact(fact):
    if fact.suggested:
        return {"pending": "pending", "accept": "keep", "reject": "remove"}[
            fact.decision
        ]
    return fact.decision


def managed_review_collection_fact_indexes(review):
    if type(review) is not EditableProfileReview:
        raise ProfileIntakeError("invalid_review_submission")
    indexes = set()
    for collection_id, spec in PROFILE_INTAKE_REVIEW_COLLECTIONS.items():
        if spec.get("browser_visible") is not True:
            continue
        for entry in review_collection_entries(review, collection_id):
            indexes.update(
                index
                for origin, index in entry["members"]
                if origin == "document"
            )
    return frozenset(indexes)


def _valid_user_fact_references(user_facts):
    if type(user_facts) is not tuple:
        return False
    next_ordinal = {collection_id: 0 for collection_id in PROFILE_INTAKE_REVIEW_COLLECTIONS}
    for fact in user_facts:
        if type(fact) is not EditableUserFact:
            return False
        ordinal = next_ordinal[fact.collection_id]
        if fact.item_reference != f"uci_{fact.collection_id}_{ordinal:03d}":
            return False
        next_ordinal[fact.collection_id] = ordinal + 1
    return True


def update_editable_review(
    review,
    values,
    decisions,
    user_inputs,
    preference_model=None,
    collection_updates=None,
    education_updates=None,
    confirm_background=False,
    confirm_profile_basics=False,
    reset_section=None,
):
    """Strict pure update; browser indexes never select authority or field paths."""

    if (
        type(review) is not EditableProfileReview
        or type(values) is not tuple
        or type(decisions) is not tuple
        or len(values) != len(review.facts)
        or len(decisions) != len(review.facts)
        or type(user_inputs) is not dict
        or type(confirm_background) is not bool
        or type(confirm_profile_basics) is not bool
        or (
            reset_section is not None
            and (
                type(reset_section) is not str
                or reset_section not in PROFILE_INTAKE_RESET_SECTIONS
            )
        )
        or set(user_inputs) != set(review.missing_user_fields)
        or not _valid_user_fact_references(review.user_facts)
        or not _valid_education_entries(review.facts, review.education_entries)
        or not _valid_reset_baseline(review.facts, review.reset_baseline)
    ):
        raise ProfileIntakeError("invalid_review_submission")
    updated = []
    for fact, raw_value, decision in zip(review.facts, values, decisions):
        allowed = {"pending", "accept", "reject"} if fact.suggested else {"keep", "remove"}
        if decision not in allowed:
            raise ProfileIntakeError("invalid_review_submission")
        value = _parse_review_value(fact.field_path, raw_value)
        updated.append(
            replace(
                fact,
                value=value,
                decision=decision,
                candidate_edited=fact.candidate_edited or value != fact.value,
            )
        )
    user_facts = review.user_facts
    if collection_updates is not None:
        updated, user_facts = _apply_review_collection_updates(
            review,
            updated,
            collection_updates,
            confirm_background=confirm_background,
        )
    if confirm_background:
        updated = [
            replace(fact, decision="accept")
            if fact.field_path == "experience.total_years"
            and fact.suggested
            and fact.decision == "pending"
            else fact
            for fact in updated
        ]
    if confirm_profile_basics:
        updated = [
            replace(fact, decision="accept")
            if fact.field_path.startswith(("identity.", "location."))
            and fact.suggested
            and fact.decision == "pending"
            and fact.conflict_group is None
            else fact
            for fact in updated
        ]
    education_entries = review.education_entries
    if education_updates is not None:
        updated, education_entries = _apply_education_entry_updates(
            review,
            updated,
            education_updates,
        )
    if reset_section is not None:
        updated, user_facts, education_entries = _reset_review_section(
            review,
            updated,
            user_facts,
            education_entries,
            reset_section,
        )
    accepted_conflicts: dict[str, int] = {}
    for fact in updated:
        if fact.conflict_group is not None and fact.decision == "accept":
            accepted_conflicts[fact.conflict_group] = (
                accepted_conflicts.get(fact.conflict_group, 0) + 1
            )
    if any(count > 1 for count in accepted_conflicts.values()):
        raise ProfileIntakeError("invalid_review_submission")
    normalized_inputs = []
    for name in review.missing_user_fields:
        normalized_inputs.append((name, _validate_user_input(name, user_inputs[name])))
    try:
        canonical_preferences = validate_profile_preferences(
            review.preference_model if preference_model is None else preference_model
        )
    except ProfilePreferenceModelError:
        raise ProfileIntakeError("invalid_review_submission") from None
    return replace(
        review,
        facts=tuple(updated),
        user_inputs=tuple(normalized_inputs),
        _preference_model_json=_preference_model_json(canonical_preferences),
        user_facts=user_facts,
        education_entries=education_entries,
    )


def _reset_review_section(
    review,
    updated_facts,
    user_facts,
    education_entries,
    section_id,
):
    """Restore one immutable extraction snapshot without touching other sections."""

    if not review_reset_section_available(review, section_id):
        raise ProfileIntakeError("invalid_review_submission")
    restored = list(updated_facts)
    baseline = tuple(
        entry for entry in review.reset_baseline if entry.section_id == section_id
    )
    for entry in baseline:
        fact = restored[entry.fact_index]
        restored[entry.fact_index] = replace(
            fact,
            value=entry.value,
            decision=entry.decision,
            candidate_edited=False,
        )
    reset_collection = {
        "work_history": "job_titles",
        "languages": "languages",
        "expertise": "skills",
    }.get(section_id)
    if reset_collection is not None:
        user_facts = tuple(
            replace(fact, decision="remove")
            if fact.collection_id == reset_collection
            else fact
            for fact in user_facts
        )
    if section_id == "education":
        education_entries = _reset_education_entries(
            tuple(restored), education_entries, baseline
        )
    return restored, user_facts, education_entries


def _reset_education_entries(restored_facts, entries, baseline):
    """Rebuild extracted entry values while retaining server-owned grouping identity."""

    baseline_by_index = {entry.fact_index: entry for entry in baseline}
    reset_entries = []
    for entry in entries:
        if entry.origin == "user":
            reset_entries.append(replace(entry, decision="remove"))
            continue
        value = {
            "kind": "not_specified",
            "qualification": "",
            "field": "",
            "institution": "",
            "status": "not_specified",
            "completion_year": None,
        }
        for fact_index in entry.source_fact_indexes:
            baseline_entry = baseline_by_index.get(fact_index)
            if baseline_entry is None:
                raise ProfileIntakeError("invalid_review_submission")
            component = _EDUCATION_ENTRY_COMPONENTS[restored_facts[fact_index].field_path]
            value[component] = baseline_entry.value
        try:
            value = canonicalize_education_entries_v1([value])[0]
        except EducationEntryContractError:
            raise ProfileIntakeError("invalid_review_submission") from None
        reset_entries.append(
            replace(
                entry,
                kind=value["kind"],
                qualification=value["qualification"],
                field_of_study=value["field"],
                institution=value["institution"],
                status=value["status"],
                completion_year=value["completion_year"],
                decision="keep",
                extraction_components=tuple(
                    sorted(
                        _EDUCATION_ENTRY_COMPONENTS[
                            restored_facts[fact_index].field_path
                        ]
                        for fact_index in entry.source_fact_indexes
                    )
                ),
                candidate_edited_components=(),
            )
        )
    candidate = tuple(reset_entries)
    if not _valid_education_entries(restored_facts, candidate):
        raise ProfileIntakeError("invalid_review_submission")
    return candidate


def _apply_education_entry_updates(review, updated_facts, updates):
    if type(updates) is not tuple:
        raise ProfileIntakeError("invalid_review_submission")
    existing = list(review.education_entries)
    if not len(existing) <= len(updates) <= MAX_EDUCATION_ENTRIES:
        raise ProfileIntakeError("invalid_review_submission")
    result = list(existing)
    for index, raw in enumerate(updates):
        if type(raw) is not tuple or len(raw) != 7:
            raise ProfileIntakeError("invalid_review_submission")
        kind, qualification, field_of_study, institution, status, raw_year, decision = raw
        if decision not in {"keep", "remove"}:
            raise ProfileIntakeError("invalid_review_submission")
        year = None
        if raw_year:
            if type(raw_year) is not str or re.fullmatch(r"[0-9]{4}", raw_year) is None:
                raise ProfileIntakeError("invalid_review_submission")
            year = int(raw_year)
        value = {
            "kind": kind,
            "qualification": qualification,
            "field": field_of_study,
            "institution": institution,
            "status": status,
            "completion_year": year,
        }
        try:
            value = canonicalize_education_entries_v1([value])[0]
        except EducationEntryContractError:
            raise ProfileIntakeError("invalid_review_submission") from None
        if index < len(existing):
            current = existing[index]
            changed_components = set(current.candidate_edited_components)
            if current.origin == "document":
                current_value = _education_entry_mapping(current)
                changed_components.update(
                    component
                    for component in _EDUCATION_ENTRY_FIELDS
                    if value[component] != current_value[component]
                )
            result[index] = replace(
                current,
                kind=value["kind"],
                qualification=value["qualification"],
                field_of_study=value["field"],
                institution=value["institution"],
                status=value["status"],
                completion_year=value["completion_year"],
                decision=decision,
                candidate_edited_components=tuple(sorted(changed_components)),
            )
            for fact_index in current.source_fact_indexes:
                fact = updated_facts[fact_index]
                updated_facts[fact_index] = replace(
                    fact,
                    decision=(
                        "accept" if decision == "keep" else "reject"
                    ) if fact.suggested else (
                        "keep" if decision == "keep" else "remove"
                    ),
                )
        else:
            if decision != "keep":
                raise ProfileIntakeError("invalid_review_submission")
            result.append(
                EditableEducationEntry(
                    item_reference=f"edu_{index:03d}",
                    origin="user",
                    kind=value["kind"],
                    qualification=value["qualification"],
                    field_of_study=value["field"],
                    institution=value["institution"],
                    status=value["status"],
                    completion_year=value["completion_year"],
                    source_fact_indexes=(),
                    source_attributions=(),
                    decision="keep",
                    extraction_components=(),
                )
            )
    candidate = tuple(result)
    if not _valid_education_entries(tuple(updated_facts), candidate):
        raise ProfileIntakeError("invalid_review_submission")
    return updated_facts, candidate


def _apply_review_collection_updates(
    review,
    updated_facts,
    updates,
    *,
    confirm_background=False,
):
    visible_collections = tuple(
        collection_id
        for collection_id, spec in PROFILE_INTAKE_REVIEW_COLLECTIONS.items()
        if spec.get("browser_visible") is True
    )
    if (
        type(updates) is not dict
        or set(updates) != set(visible_collections)
        or type(confirm_background) is not bool
    ):
        raise ProfileIntakeError("invalid_review_submission")
    updated_user_facts = list(review.user_facts)
    references = {fact.item_reference for fact in review.user_facts}
    if len(references) != len(review.user_facts):
        raise ProfileIntakeError("invalid_review_submission")
    new_fact_count = 0
    for collection_id in visible_collections:
        spec = PROFILE_INTAKE_REVIEW_COLLECTIONS[collection_id]
        submitted = updates[collection_id]
        if type(submitted) is not tuple:
            raise ProfileIntakeError("invalid_review_submission")
        existing = review_collection_entries(review, collection_id)
        if not len(existing) <= len(submitted) <= spec["limit"]:
            raise ProfileIntakeError("invalid_review_submission")
        normalized_active = []
        for item_index, raw_item in enumerate(submitted):
            value, decision = _review_collection_value(spec, raw_item)
            if item_index < len(existing):
                entry = existing[item_index]
                if (
                    confirm_background
                    and collection_id == "skills"
                    and entry["suggested"]
                    and decision == "pending"
                ):
                    decision = "keep"
                if decision == "pending" and not entry["requires_confirmation"]:
                    raise ProfileIntakeError("invalid_review_submission")
                if entry["suggested"] and value != entry["value"]:
                    # Inferred document evidence does not become evidence for a
                    # browser-authored correction.  Candidates can remove the
                    # suggestion and add the corrected expertise as a user fact.
                    raise ProfileIntakeError("invalid_review_submission")
                preserve_member_decisions = bool(
                    entry["mixed_decisions"] and decision == entry["decision"]
                )
                for origin, member_index in entry["members"]:
                    if origin == "document":
                        current = updated_facts[member_index]
                        member_decision = (
                            current.decision
                            if decision == "pending" or preserve_member_decisions
                            else (
                                "accept" if decision == "keep" else "reject"
                            )
                            if current.suggested
                            else decision
                        )
                        updated_facts[member_index] = replace(
                            current,
                            value=(current.value if value == entry["value"] else value),
                            decision=member_decision,
                            candidate_edited=(
                                current.candidate_edited
                                or value != entry["value"]
                            ),
                        )
                    else:
                        current = updated_user_facts[member_index]
                        updated_user_facts[member_index] = replace(
                            current,
                            value=(current.value if value == entry["value"] else value),
                            decision=(
                                current.decision
                                if decision == "pending" or preserve_member_decisions
                                else decision
                            ),
                        )
            else:
                if decision != "keep":
                    raise ProfileIntakeError("invalid_review_submission")
                ordinal = sum(
                    fact.collection_id == collection_id
                    for fact in updated_user_facts
                )
                reference = f"uci_{collection_id}_{ordinal:03d}"
                if reference in references:
                    raise ProfileIntakeError("invalid_review_submission")
                references.add(reference)
                updated_user_facts.append(
                    EditableUserFact(
                        item_reference=reference,
                        collection_id=collection_id,
                        field_path=spec["add_path"],
                        value=value,
                        decision="keep",
                    )
                )
                new_fact_count += 1
            if decision in {"keep", "pending"}:
                identity = _review_collection_identity(value)
                if identity in normalized_active:
                    raise ProfileIntakeError("invalid_review_submission")
                normalized_active.append(identity)
        add_limit = spec.get("add_limit")
        if add_limit is not None:
            active_add_path_facts = sum(
                fact.field_path == spec["add_path"]
                and fact.decision in ({"accept"} if fact.suggested else {"keep"})
                for fact in updated_facts
            ) + sum(
                fact.collection_id == collection_id and fact.decision == "keep"
                for fact in updated_user_facts
            )
            if active_add_path_facts > add_limit:
                raise ProfileIntakeError("invalid_review_submission")
    if len(review.facts) + len(review.user_facts) + new_fact_count > contracts.MAX_EXTRACTION_FACTS:
        raise ProfileIntakeError("invalid_review_submission")
    return updated_facts, tuple(updated_user_facts)


def _review_collection_value(spec, raw_item):
    try:
        if spec["kind"] == "string":
            if type(raw_item) is not tuple or len(raw_item) != 2:
                raise ProfileIntakeError("invalid_review_submission")
            raw_value, decision = raw_item
            value = _parse_review_value(spec["add_path"], raw_value)
        elif spec["kind"] == "language":
            if type(raw_item) is not tuple or len(raw_item) != 4:
                raise ProfileIntakeError("invalid_review_submission")
            language, proficiency, locale, decision = raw_item
            value = contracts._validate_fact_value(
                {
                    "language": language,
                    "proficiency": proficiency or None,
                    "locale": locale or None,
                },
                contracts._FIELD_SPECS[spec["add_path"]],
            )
        else:
            raise ProfileIntakeError("invalid_review_submission")
    except (ProfileIntakeError, TypeError, ValueError):
        raise ProfileIntakeError("invalid_review_submission") from None
    allowed_decisions = (
        {"pending", "keep", "remove"}
        if spec.get("include_suggested") is True
        else {"keep", "remove"}
    )
    if decision not in allowed_decisions:
        raise ProfileIntakeError("invalid_review_submission")
    return value, decision


def _review_collection_identity(value):
    if type(value) is LanguageValue:
        return ("language", value.language.casefold())
    if type(value) is str:
        return ("string", value.casefold())
    raise ProfileIntakeError("invalid_review_submission")


def _preference_model_json(value):
    canonical = validate_profile_preferences(value)
    return json.dumps(
        canonical,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def serialize_profile_intake_checkpoint(
    review: EditableProfileReview,
    *,
    review_step=PROFILE_INTAKE_DEFAULT_REVIEW_STEP,
) -> str:
    """Return the closed, privacy-gated durable projection of one review.

    Evidence identities, document identities, uploaded-file details, model
    diagnostics, and raw extraction material are intentionally not represented.
    """

    if (
        type(review) is not EditableProfileReview
        or not _valid_reset_baseline(review.facts, review.reset_baseline)
    ):
        raise ProfileIntakeError("invalid_checkpoint_content")
    review_step = normalize_profile_intake_review_step(review_step)
    source_origins = tuple(source.document_kind.value for source in review.sources)
    source_set = set(source_origins)
    if not 1 <= len(source_origins) <= 2 or len(source_set) != len(source_origins):
        raise ProfileIntakeError("invalid_checkpoint_content")
    conflict_ids = {}
    facts = []
    for fact in review.facts:
        origins = tuple(
            attribution.document_kind.value
            for attribution in fact.source_attributions
        )
        if not origins or len(set(origins)) != len(origins) or not set(origins) <= source_set:
            raise ProfileIntakeError("invalid_checkpoint_content")
        conflict_id = None
        if fact.conflict_group is not None:
            conflict_id = conflict_ids.setdefault(
                fact.conflict_group,
                f"conflict_{len(conflict_ids) + 1:03d}",
            )
        value = _checkpoint_value(fact.value)
        _checkpoint_require_no_contact_pii(value)
        item = {
            "field_path": fact.field_path,
            "value": value,
            "source_origins": list(origins),
            "suggested": fact.suggested,
            "decision": fact.decision,
            "conflict_id": conflict_id,
        }
        if fact.explicit != (not fact.suggested):
            item["explicit"] = fact.explicit
        if fact.candidate_edited:
            item["candidate_edited"] = True
        facts.append(item)
    user_inputs = dict(review.user_inputs)
    if len(user_inputs) != len(review.user_inputs):
        raise ProfileIntakeError("invalid_checkpoint_content")
    _checkpoint_require_no_contact_pii(user_inputs)
    user_facts = []
    if (
        not _valid_user_fact_references(review.user_facts)
        or len(review.facts) + len(review.user_facts) > contracts.MAX_EXTRACTION_FACTS
    ):
        raise ProfileIntakeError("invalid_checkpoint_content")
    for fact in review.user_facts:
        if type(fact) is not EditableUserFact:
            raise ProfileIntakeError("invalid_checkpoint_content")
        value = _checkpoint_value(fact.value)
        _checkpoint_require_no_contact_pii(value)
        user_facts.append(
            {
                "item_reference": fact.item_reference,
                "collection_id": fact.collection_id,
                "field_path": fact.field_path,
                "value": value,
                "decision": fact.decision,
            }
        )
    if not _valid_education_entries(review.facts, review.education_entries):
        raise ProfileIntakeError("invalid_checkpoint_content")
    education_entries = []
    for entry in review.education_entries:
        value = _education_entry_mapping(entry)
        _checkpoint_require_no_contact_pii(value)
        item = {
            "item_reference": entry.item_reference,
            "origin": entry.origin,
            "value": value,
            "source_fact_indexes": list(entry.source_fact_indexes),
            "source_origins": [
                attribution.document_kind.value
                for attribution in entry.source_attributions
            ],
            "decision": entry.decision,
        }
        if entry.extraction_components is not None:
            item["extraction_components"] = list(entry.extraction_components)
        if entry.candidate_edited_components:
            item["candidate_edited_components"] = list(
                entry.candidate_edited_components
            )
        education_entries.append(item)
    reset_baseline = []
    for entry in review.reset_baseline:
        value = _checkpoint_value(entry.value)
        _checkpoint_require_no_contact_pii(value)
        reset_baseline.append(
            {
                "section_id": entry.section_id,
                "fact_index": entry.fact_index,
                "value": value,
                "decision": entry.decision,
            }
        )
    payload = {
        "schema_version": PROFILE_INTAKE_CHECKPOINT_SCHEMA_VERSION,
        "review_schema_version": review.schema_version,
        "source_origins": list(source_origins),
        "facts": facts,
        "missing_user_fields": list(review.missing_user_fields),
        "user_inputs": user_inputs,
        "issue_count": review.issue_count,
        "preference_model": review.preference_model,
        "user_facts": user_facts,
        "education_entries": education_entries,
        "reset_baseline": reset_baseline,
        "review_step": review_step,
    }
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, UnicodeError, ValueError):
        raise ProfileIntakeError("invalid_checkpoint_content") from None
    if not 2 <= len(encoded) <= PROFILE_INTAKE_CHECKPOINT_MAX_BYTES:
        raise ProfileIntakeError("checkpoint_too_large")
    # Hydration is also the strict structural validator for the serialized form.
    hydrate_profile_intake_checkpoint(encoded.decode("ascii"))
    return encoded.decode("ascii")


def hydrate_profile_intake_checkpoint(payload_json: str) -> EditableProfileReview:
    """Strictly reconstruct a process-local review without document/model work."""

    if type(payload_json) is not str:
        raise ProfileIntakeError("invalid_checkpoint_content")
    try:
        encoded = payload_json.encode("ascii")
    except UnicodeError:
        raise ProfileIntakeError("invalid_checkpoint_content") from None
    if not 2 <= len(encoded) <= PROFILE_INTAKE_CHECKPOINT_MAX_BYTES:
        raise ProfileIntakeError("checkpoint_too_large")
    try:
        payload = json.loads(
            payload_json,
            object_pairs_hook=_unique_checkpoint_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ProfileIntakeError("invalid_checkpoint_content") from None
    required_keys = {
        "schema_version",
        "review_schema_version",
        "source_origins",
        "facts",
        "missing_user_fields",
        "user_inputs",
        "issue_count",
        "preference_model",
    }
    optional_keys = {
        "review_step",
        "user_facts",
        "education_entries",
        "reset_baseline",
        "expertise_baseline",
        "work_history_baseline",
    }
    if (
        type(payload) is not dict
        or not required_keys <= set(payload)
        or not set(payload) <= required_keys | optional_keys
    ):
        raise ProfileIntakeError("invalid_checkpoint_content")
    if (
        payload["schema_version"] != PROFILE_INTAKE_CHECKPOINT_SCHEMA_VERSION
        or payload["review_schema_version"] != REVIEW_DRAFT_SCHEMA_VERSION
    ):
        raise ProfileIntakeError("checkpoint_version_unsupported")
    raw_origins = payload["source_origins"]
    try:
        origins = tuple(DocumentKind(item) for item in raw_origins)
    except (TypeError, ValueError):
        raise ProfileIntakeError("invalid_checkpoint_content") from None
    if (
        type(raw_origins) is not list
        or not 1 <= len(origins) <= 2
        or len(set(origins)) != len(origins)
    ):
        raise ProfileIntakeError("invalid_checkpoint_content")
    references = {
        origin: f"doc_{index:032x}" for index, origin in enumerate(origins, start=1)
    }
    sources = tuple(
        ReviewDraftSource(references[origin], origin) for origin in origins
    )
    raw_facts = payload["facts"]
    if type(raw_facts) is not list or len(raw_facts) > contracts.MAX_EXTRACTION_FACTS:
        raise ProfileIntakeError("invalid_checkpoint_content")
    facts = []
    values = []
    decisions = []
    for raw in raw_facts:
        base_fact_keys = {
            "field_path", "value", "source_origins", "suggested", "decision", "conflict_id"
        }
        optional_fact_keys = {"explicit", "candidate_edited"}
        if (
            type(raw) is not dict
            or not base_fact_keys <= set(raw)
            or not set(raw) <= base_fact_keys | optional_fact_keys
            or (
                "explicit" in raw
                and type(raw["explicit"]) is not bool
            )
            or (
                "candidate_edited" in raw
                and raw["candidate_edited"] is not True
            )
        ):
            raise ProfileIntakeError("invalid_checkpoint_content")
        field_path = raw["field_path"]
        if field_path not in _REVIEW_FIELDS or type(raw["suggested"]) is not bool:
            raise ProfileIntakeError("invalid_checkpoint_content")
        raw_fact_origins = raw["source_origins"]
        try:
            fact_origins = tuple(DocumentKind(item) for item in raw_fact_origins)
        except (TypeError, ValueError):
            raise ProfileIntakeError("invalid_checkpoint_content") from None
        if (
            type(raw_fact_origins) is not list
            or not fact_origins
            or len(set(fact_origins)) != len(fact_origins)
            or not set(fact_origins) <= set(origins)
        ):
            raise ProfileIntakeError("invalid_checkpoint_content")
        conflict_id = raw["conflict_id"]
        if conflict_id is not None and (
            type(conflict_id) is not str
            or re.fullmatch(r"conflict_[0-9]{3}", conflict_id) is None
        ):
            raise ProfileIntakeError("invalid_checkpoint_content")
        value = _checkpoint_value_from_json(raw["value"])
        _checkpoint_require_no_contact_pii(raw["value"])
        facts.append(
            EditableReviewFact(
                field_path=field_path,
                review_field=_REVIEW_FIELDS[field_path],
                value=value,
                source_attributions=tuple(
                    ReviewSourceAttribution(
                        references[origin], origin, ("b001",)
                    )
                    for origin in fact_origins
                ),
                suggested=raw["suggested"],
                decision=raw["decision"],
                conflict_group=conflict_id,
                explicit=raw.get("explicit", not raw["suggested"]),
                candidate_edited=raw.get("candidate_edited", False),
            )
        )
        values.append(review_value_for_form(value))
        decisions.append(raw["decision"])
    if "reset_baseline" in payload and {
        "expertise_baseline",
        "work_history_baseline",
    }.intersection(payload):
        raise ProfileIntakeError("invalid_checkpoint_content")
    reset_baseline_items = []
    raw_reset_baseline = payload.get("reset_baseline")
    if raw_reset_baseline is not None:
        if type(raw_reset_baseline) is not list:
            raise ProfileIntakeError("invalid_checkpoint_content")
        reset_sources = ((None, raw_reset_baseline),)
    else:
        reset_sources = tuple(
            (section_id, payload[key])
            for key, section_id in (
                ("expertise_baseline", "expertise"),
                ("work_history_baseline", "work_history"),
            )
            if key in payload
        )
    for legacy_section_id, raw_items in reset_sources:
        if type(raw_items) is not list:
            raise ProfileIntakeError("invalid_checkpoint_content")
        for raw in raw_items:
            expected_keys = {"fact_index", "value", "decision"}
            if legacy_section_id is None:
                expected_keys.add("section_id")
            if type(raw) is not dict or set(raw) != expected_keys:
                raise ProfileIntakeError("invalid_checkpoint_content")
            _checkpoint_require_no_contact_pii(raw["value"])
            try:
                reset_baseline_items.append(
                    EditableResetBaselineEntry(
                        section_id=(
                            raw["section_id"]
                            if legacy_section_id is None
                            else legacy_section_id
                        ),
                        fact_index=raw["fact_index"],
                        value=_checkpoint_value_from_json(raw["value"]),
                        decision=raw["decision"],
                    )
                )
            except (KeyError, ProfileIntakeError, TypeError, ValueError):
                raise ProfileIntakeError("invalid_checkpoint_content") from None
    reset_baseline = tuple(reset_baseline_items)
    if not _valid_reset_baseline(tuple(facts), reset_baseline):
        raise ProfileIntakeError("invalid_checkpoint_content")
    missing = payload["missing_user_fields"]
    user_inputs = payload["user_inputs"]
    allowed_missing = set(_USER_ONLY_REVIEW_FIELDS) - _TYPED_PREFERENCE_REPLACED_USER_FIELDS
    if (
        type(missing) is not list
        or any(type(item) is not str or item not in allowed_missing for item in missing)
        or len(set(missing)) != len(missing)
        or type(user_inputs) is not dict
        or set(user_inputs) != set(missing)
        or any(type(value) is not str for value in user_inputs.values())
        or type(payload["issue_count"]) is not int
        or not 0 <= payload["issue_count"] <= contracts.MAX_EXTRACTION_FACTS
    ):
        raise ProfileIntakeError("invalid_checkpoint_content")
    _checkpoint_require_no_contact_pii(user_inputs)
    raw_user_facts = payload.get("user_facts", [])
    if (
        type(raw_user_facts) is not list
        or len(raw_facts) + len(raw_user_facts) > contracts.MAX_EXTRACTION_FACTS
    ):
        raise ProfileIntakeError("invalid_checkpoint_content")
    user_facts = []
    for raw in raw_user_facts:
        if type(raw) is not dict or set(raw) != {
            "item_reference",
            "collection_id",
            "field_path",
            "value",
            "decision",
        }:
            raise ProfileIntakeError("invalid_checkpoint_content")
        value = _checkpoint_value_from_json(raw["value"])
        _checkpoint_require_no_contact_pii(raw["value"])
        try:
            user_facts.append(
                EditableUserFact(
                    item_reference=raw["item_reference"],
                    collection_id=raw["collection_id"],
                    field_path=raw["field_path"],
                    value=value,
                    decision=raw["decision"],
                )
            )
        except (ProfileIntakeError, TypeError, ValueError):
            raise ProfileIntakeError("invalid_checkpoint_content") from None
    if not _valid_user_fact_references(tuple(user_facts)):
        raise ProfileIntakeError("invalid_checkpoint_content")
    raw_education_entries = payload.get("education_entries", [])
    if type(raw_education_entries) is not list or len(raw_education_entries) > MAX_EDUCATION_ENTRIES:
        raise ProfileIntakeError("invalid_checkpoint_content")
    education_entries = []
    for raw in raw_education_entries:
        base_entry_keys = {
            "item_reference",
            "origin",
            "value",
            "source_fact_indexes",
            "source_origins",
            "decision",
        }
        optional_entry_keys = {
            "extraction_components",
            "candidate_edited_components",
        }
        if (
            type(raw) is not dict
            or not base_entry_keys <= set(raw)
            or not set(raw) <= base_entry_keys | optional_entry_keys
        ):
            raise ProfileIntakeError("invalid_checkpoint_content")
        value = raw["value"]
        _checkpoint_require_no_contact_pii(value)
        source_indexes = raw["source_fact_indexes"]
        raw_entry_origins = raw["source_origins"]
        if (
            type(source_indexes) is not list
            or any(type(index) is not int for index in source_indexes)
            or type(raw_entry_origins) is not list
            or (
                "extraction_components" in raw
                and type(raw["extraction_components"]) is not list
            )
            or type(raw.get("candidate_edited_components", [])) is not list
        ):
            raise ProfileIntakeError("invalid_checkpoint_content")
        try:
            entry_origins = tuple(DocumentKind(item) for item in raw_entry_origins)
            education_entries.append(
                EditableEducationEntry(
                    item_reference=raw["item_reference"],
                    origin=raw["origin"],
                    kind=value["kind"],
                    qualification=value["qualification"],
                    field_of_study=value["field"],
                    institution=value["institution"],
                    status=value["status"],
                    completion_year=value["completion_year"],
                    source_fact_indexes=tuple(source_indexes),
                    source_attributions=tuple(
                        ReviewSourceAttribution(
                            references[origin],
                            origin,
                            ("b001",),
                        )
                        for origin in entry_origins
                    ),
                    decision=raw["decision"],
                    extraction_components=(
                        tuple(raw["extraction_components"])
                        if "extraction_components" in raw
                        else None
                    ),
                    candidate_edited_components=tuple(
                        raw.get("candidate_edited_components", [])
                    ),
                )
            )
        except (KeyError, ProfileIntakeError, TypeError, ValueError):
            raise ProfileIntakeError("invalid_checkpoint_content") from None
    if not _valid_education_entries(tuple(facts), tuple(education_entries)):
        raise ProfileIntakeError("invalid_checkpoint_content")
    try:
        review = EditableProfileReview(
            schema_version=REVIEW_DRAFT_SCHEMA_VERSION,
            sources=sources,
            facts=tuple(facts),
            missing_user_fields=tuple(missing),
            user_inputs=tuple((name, "") for name in missing),
            issue_count=payload["issue_count"],
            _preference_model_json=_preference_model_json(payload["preference_model"]),
            user_facts=tuple(user_facts),
            education_entries=tuple(education_entries),
            reset_baseline=reset_baseline,
        )
        review = update_editable_review(
            review,
            tuple(values),
            tuple(decisions),
            user_inputs,
            payload["preference_model"],
        )
    except (ProfileIntakeError, ProfilePreferenceModelError, TypeError, ValueError):
        raise ProfileIntakeError("invalid_checkpoint_content") from None
    semantic_payload = dict(payload)
    semantic_payload.pop("review_step", None)
    semantic_payload.setdefault("user_facts", [])
    semantic_payload.setdefault("education_entries", [])
    canonical = json.dumps(
        json.loads(
            _serialize_profile_intake_checkpoint_unchecked(
                review,
                include_review_step=False,
                include_reset_baseline=("reset_baseline" in payload),
                include_expertise_baseline=("expertise_baseline" in payload),
                include_work_history_baseline=("work_history_baseline" in payload),
            )
        ),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    incoming = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    semantic_incoming = json.dumps(
        semantic_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    if (
        not hmac.compare_digest(incoming, payload_json)
        or not hmac.compare_digest(canonical, semantic_incoming)
    ):
        raise ProfileIntakeError("invalid_checkpoint_content")
    return review


def profile_intake_checkpoint_review_step(payload_json):
    """Read the non-authoritative step after strict checkpoint validation."""

    hydrate_profile_intake_checkpoint(payload_json)
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ProfileIntakeError("invalid_checkpoint_content") from None
    return normalize_profile_intake_review_step(payload.get("review_step"))


def _serialize_profile_intake_checkpoint_unchecked(
    review: EditableProfileReview,
    *,
    review_step=PROFILE_INTAKE_DEFAULT_REVIEW_STEP,
    include_review_step=True,
    include_reset_baseline=True,
    include_expertise_baseline=False,
    include_work_history_baseline=False,
) -> str:
    """Internal canonicalizer used to avoid recursive validation."""

    source_origins = tuple(source.document_kind.value for source in review.sources)
    conflict_ids = {}
    facts = []
    for fact in review.facts:
        conflict_id = None
        if fact.conflict_group is not None:
            conflict_id = conflict_ids.setdefault(fact.conflict_group, fact.conflict_group)
        item = {
            "field_path": fact.field_path,
            "value": _checkpoint_value(fact.value),
            "source_origins": [item.document_kind.value for item in fact.source_attributions],
            "suggested": fact.suggested,
            "decision": fact.decision,
            "conflict_id": conflict_id,
        }
        if fact.explicit != (not fact.suggested):
            item["explicit"] = fact.explicit
        if fact.candidate_edited:
            item["candidate_edited"] = True
        facts.append(item)
    payload = {
        "schema_version": PROFILE_INTAKE_CHECKPOINT_SCHEMA_VERSION,
        "review_schema_version": review.schema_version,
        "source_origins": list(source_origins),
        "facts": facts,
        "missing_user_fields": list(review.missing_user_fields),
        "user_inputs": dict(review.user_inputs),
        "issue_count": review.issue_count,
        "preference_model": review.preference_model,
        "user_facts": [
            {
                "item_reference": fact.item_reference,
                "collection_id": fact.collection_id,
                "field_path": fact.field_path,
                "value": _checkpoint_value(fact.value),
                "decision": fact.decision,
            }
            for fact in review.user_facts
        ],
        "education_entries": [
            {
                "item_reference": entry.item_reference,
                "origin": entry.origin,
                "value": _education_entry_mapping(entry),
                "source_fact_indexes": list(entry.source_fact_indexes),
                "source_origins": [
                    attribution.document_kind.value
                    for attribution in entry.source_attributions
                ],
                "decision": entry.decision,
                **(
                    {"extraction_components": list(entry.extraction_components)}
                    if entry.extraction_components is not None
                    else {}
                ),
                **(
                    {
                        "candidate_edited_components": list(
                            entry.candidate_edited_components
                        )
                    }
                    if entry.candidate_edited_components
                    else {}
                ),
            }
            for entry in review.education_entries
        ],
    }
    if include_reset_baseline:
        payload["reset_baseline"] = [
            {
                "section_id": entry.section_id,
                "fact_index": entry.fact_index,
                "value": _checkpoint_value(entry.value),
                "decision": entry.decision,
            }
            for entry in review.reset_baseline
        ]
    if include_expertise_baseline:
        payload["expertise_baseline"] = [
            {
                "fact_index": entry.fact_index,
                "value": _checkpoint_value(entry.value),
                "decision": entry.decision,
            }
            for entry in review.reset_baseline
            if entry.section_id == "expertise"
        ]
    if include_work_history_baseline:
        payload["work_history_baseline"] = [
            {
                "fact_index": entry.fact_index,
                "value": _checkpoint_value(entry.value),
                "decision": entry.decision,
            }
            for entry in review.reset_baseline
            if entry.section_id == "work_history"
        ]
    if include_review_step:
        payload["review_step"] = normalize_profile_intake_review_step(review_step)
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _checkpoint_value(value):
    if type(value) is LanguageValue:
        return {
            "type": "language",
            "language": value.language,
            "proficiency": value.proficiency,
            "locale": value.locale,
        }
    if type(value) in {str, int, float, bool}:
        return value
    raise ProfileIntakeError("invalid_checkpoint_content")


def _checkpoint_value_from_json(value):
    if type(value) is dict:
        if set(value) != {"type", "language", "proficiency", "locale"} or value["type"] != "language":
            raise ProfileIntakeError("invalid_checkpoint_content")
        if type(value["language"]) is not str or (
            value["proficiency"] is not None and type(value["proficiency"]) is not str
        ) or (value["locale"] is not None and type(value["locale"]) is not str):
            raise ProfileIntakeError("invalid_checkpoint_content")
        return LanguageValue(value["language"], value["proficiency"], value["locale"])
    if type(value) in {str, int, float, bool}:
        return value
    raise ProfileIntakeError("invalid_checkpoint_content")


def _checkpoint_require_no_contact_pii(value):
    if type(value) is str:
        if contains_detectable_contact_pii(value):
            raise ProfileIntakeError("checkpoint_contact_pii_rejected")
        return
    if type(value) is dict:
        for item in value.values():
            _checkpoint_require_no_contact_pii(item)
        return
    if type(value) is list:
        for item in value:
            _checkpoint_require_no_contact_pii(item)
        return
    if value is None or type(value) in {int, float, bool}:
        return
    raise ProfileIntakeError("invalid_checkpoint_content")


def _unique_checkpoint_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def review_value_for_form(value):
    if type(value) is LanguageValue:
        return " | ".join(
            (value.language, value.proficiency or "", value.locale or "")
        ).rstrip(" |")
    if type(value) is bool:
        return "true" if value else "false"
    return str(value)


def _parse_review_value(field_path, raw):
    if type(raw) is not str or "\x00" in raw or len(raw) > MAX_REVIEW_VALUE_CHARS:
        raise ProfileIntakeError("invalid_review_submission")
    spec = contracts._FIELD_SPECS.get(field_path)
    if spec is None:
        raise ProfileIntakeError("invalid_review_submission")
    candidate = " ".join(raw.split())
    try:
        if spec.kind == "boolean":
            if candidate not in {"true", "false"}:
                raise ProfileIntakeError("invalid_review_submission")
            value = candidate == "true"
        elif spec.kind == "years":
            # Preserve the JSON numeric category through the form-backed review
            # round-trip.  This keeps both existing float checkpoints ("6.0")
            # and integer extraction values ("6") canonically stable.
            value = (
                int(candidate)
                if re.fullmatch(r"\+?[0-9]+", candidate) is not None
                else float(candidate)
            )
        elif spec.kind == "completion_year":
            if re.fullmatch(r"[0-9]{4}", candidate) is None:
                raise ProfileIntakeError("invalid_review_submission")
            value = int(candidate)
        elif spec.kind == "language":
            parts = tuple(part.strip() for part in raw.split("|"))
            if not 1 <= len(parts) <= 3:
                raise ProfileIntakeError("invalid_review_submission")
            value = {
                "language": parts[0],
                "proficiency": parts[1] or None if len(parts) > 1 else None,
                "locale": parts[2] or None if len(parts) > 2 else None,
            }
        else:
            value = candidate
        return contracts._validate_fact_value(value, spec)
    except (ProfileIntakeError, ValueError, OverflowError):
        raise ProfileIntakeError("invalid_review_submission") from None


def _validate_user_input(name, raw):
    if type(raw) is not str or "\x00" in raw or len(raw) > MAX_REVIEW_USER_INPUT_CHARS:
        raise ProfileIntakeError("invalid_review_submission")
    normalized = " ".join(raw.split())
    if name in {"remote", "flexible"} and normalized not in {"", "yes", "no"}:
        raise ProfileIntakeError("invalid_review_submission")
    enum_sets = {
        "employment_types": contracts.EMPLOYMENT_TYPES,
        "synchronous_preference": contracts.SYNCHRONOUS_PREFERENCES,
        "phone_preference": contracts.PHONE_PREFERENCES,
        "schedule": contracts.SCHEDULE_PREFERENCES,
        "availability": contracts.AVAILABILITY_STATUSES,
    }
    if name in enum_sets and normalized:
        items = tuple(item.strip() for item in normalized.split(","))
        if not items or len(items) > 16 or any(item not in enum_sets[name] for item in items):
            raise ProfileIntakeError("invalid_review_submission")
        normalized = ", ".join(items)
    return normalized


def _extraction_mapping(extraction):
    if type(extraction) is not AIProfileExtraction:
        raise ProfileIntakeError("invalid_profile_extraction")
    facts = []
    for fact in extraction.facts:
        value = fact.value
        if type(value) is LanguageValue:
            value = {
                "language": value.language,
                "proficiency": value.proficiency,
                "locale": value.locale,
            }
        facts.append(
            {
                "field_path": fact.field_path,
                "value": value,
                "source_document_reference": fact.source_document_reference,
                "evidence_block_references": list(fact.evidence_block_references),
                "confidence": fact.confidence,
                "explicit": fact.explicit,
            }
        )
    return {
        "schema_version": AI_EXTRACTION_SCHEMA_VERSION,
        "document_reference": extraction.document_reference,
        "facts": facts,
    }


def _safe_diagnostics(value, document_kind):
    if type(value) is not ProfileExtractionDiagnostics:
        raise ProfileIntakeError("invalid_profile_extraction_diagnostics")
    if type(document_kind) is not DocumentKind:
        raise ProfileIntakeError("invalid_profile_extraction_diagnostics")
    return SafeModelDiagnostics(
        document_kind=document_kind.value,
        model=value.model,
        prompt_version=value.prompt_version,
        schema_version=value.schema_version,
        input_tokens=value.input_tokens,
        output_tokens=value.output_tokens,
        duration_ms=value.duration_ms,
        provider_request_id=value.provider_request_id,
        success=value.success,
        failure_code=value.failure_code,
    )


def _grant_binding(grant):
    if type(grant) is not TrustedProfileIntakeGrant:
        raise _configuration_error()
    binding = grant.artifact_binding()
    if type(binding) is not tuple or len(binding) != 10 or binding[-1] != PROFILE_INTAKE_PURPOSE:
        raise _configuration_error()
    return binding


def _monotonic(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise _configuration_error()
    return float(value)


def _is_sha256(value):
    return (
        type(value) is str
        and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    )


def _save_hook(callback, boundary):
    if callback is not None:
        callback(boundary)
