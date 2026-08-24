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
    canonicalize_profile_preferences_v1,
    empty_profile_preferences_v1,
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

_OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_ACTIONS = frozenset({"upload", "update", "renew", "cancel", "save"})
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


@dataclass(frozen=True, slots=True)
class EditableProfileReview:
    schema_version: str
    sources: tuple[ReviewDraftSource, ...]
    facts: tuple[EditableReviewFact, ...]
    missing_user_fields: tuple[str, ...]
    user_inputs: tuple[tuple[str, str], ...]
    issue_count: int
    _preference_model_json: bytes = field(repr=False)

    @property
    def document_reference(self):
        return self.sources[0].document_reference if len(self.sources) == 1 else None

    @property
    def preference_model(self):
        """Return a defensive copy of the server-validated preference model."""

        try:
            value = json.loads(self._preference_model_json.decode("ascii"))
            return canonicalize_profile_preferences_v1(value)
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


@dataclass(frozen=True, slots=True, repr=False)
class IntakeDraftSnapshot:
    review: EditableProfileReview = field(repr=False)
    document: SafeDocumentBundleMetadata
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
                    "reserve",
                    "renew",
                    "release",
                    "discard_checkpoint",
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
        expired = self._vault.expire_bound(grant)
        if self._durable is not None:
            for authority in expired:
                try:
                    self._durable.release(
                        grant,
                        authority,
                        outcome_code="draft_expired",
                    )
                except ProfileIntakeError:
                    pass
        if self._durable is None:
            return "eligible"
        return self._durable.preflight(grant)

    def lookup(self, reference, grant):
        state, value = self._vault.lookup(reference, grant)
        if state == "expired" and value is not None and self._durable is not None:
            try:
                self._durable.release(grant, value, outcome_code="draft_expired")
            except ProfileIntakeError:
                pass
        return ("gone", None) if state == "expired" else (state, value)

    def renew(self, reference, grant, *, expected_version):
        state, value = self._vault.renew(
            reference,
            grant,
            expected_version=expected_version,
        )
        if state == "expired" and value is not None and self._durable is not None:
            try:
                self._durable.release(grant, value, outcome_code="draft_expired")
            except ProfileIntakeError:
                pass
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
        if state == "expired" and value is not None and self._durable is not None:
            try:
                self._durable.release(grant, value, outcome_code="draft_expired")
            except ProfileIntakeError:
                pass
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
    preference_model = empty_profile_preferences_v1()
    return EditableProfileReview(
        schema_version=draft.schema_version,
        sources=draft.sources,
        facts=facts,
        missing_user_fields=missing_user_fields,
        user_inputs=tuple((name, "") for name in missing_user_fields),
        issue_count=len(draft.issues),
        _preference_model_json=_preference_model_json(preference_model),
    )


def update_editable_review(
    review,
    values,
    decisions,
    user_inputs,
    preference_model=None,
):
    """Strict pure update; browser indexes never select authority or field paths."""

    if (
        type(review) is not EditableProfileReview
        or type(values) is not tuple
        or type(decisions) is not tuple
        or len(values) != len(review.facts)
        or len(decisions) != len(review.facts)
        or type(user_inputs) is not dict
        or set(user_inputs) != set(review.missing_user_fields)
    ):
        raise ProfileIntakeError("invalid_review_submission")
    updated = []
    for fact, raw_value, decision in zip(review.facts, values, decisions):
        allowed = {"pending", "accept", "reject"} if fact.suggested else {"keep", "remove"}
        if decision not in allowed:
            raise ProfileIntakeError("invalid_review_submission")
        value = _parse_review_value(fact.field_path, raw_value)
        updated.append(replace(fact, value=value, decision=decision))
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
        canonical_preferences = canonicalize_profile_preferences_v1(
            review.preference_model if preference_model is None else preference_model
        )
    except ProfilePreferenceModelError:
        raise ProfileIntakeError("invalid_review_submission") from None
    return replace(
        review,
        facts=tuple(updated),
        user_inputs=tuple(normalized_inputs),
        _preference_model_json=_preference_model_json(canonical_preferences),
    )


def _preference_model_json(value):
    canonical = canonicalize_profile_preferences_v1(value)
    return json.dumps(
        canonical,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def serialize_profile_intake_checkpoint(review: EditableProfileReview) -> str:
    """Return the closed, privacy-gated durable projection of one review.

    Evidence identities, document identities, uploaded-file details, model
    diagnostics, and raw extraction material are intentionally not represented.
    """

    if type(review) is not EditableProfileReview:
        raise ProfileIntakeError("invalid_checkpoint_content")
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
        facts.append(
            {
                "field_path": fact.field_path,
                "value": value,
                "source_origins": list(origins),
                "suggested": fact.suggested,
                "decision": fact.decision,
                "conflict_id": conflict_id,
            }
        )
    user_inputs = dict(review.user_inputs)
    if len(user_inputs) != len(review.user_inputs):
        raise ProfileIntakeError("invalid_checkpoint_content")
    _checkpoint_require_no_contact_pii(user_inputs)
    payload = {
        "schema_version": PROFILE_INTAKE_CHECKPOINT_SCHEMA_VERSION,
        "review_schema_version": review.schema_version,
        "source_origins": list(source_origins),
        "facts": facts,
        "missing_user_fields": list(review.missing_user_fields),
        "user_inputs": user_inputs,
        "issue_count": review.issue_count,
        "preference_model": review.preference_model,
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
    if type(payload) is not dict or set(payload) != {
        "schema_version",
        "review_schema_version",
        "source_origins",
        "facts",
        "missing_user_fields",
        "user_inputs",
        "issue_count",
        "preference_model",
    }:
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
        if type(raw) is not dict or set(raw) != {
            "field_path", "value", "source_origins", "suggested", "decision", "conflict_id"
        }:
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
            )
        )
        values.append(review_value_for_form(value))
        decisions.append(raw["decision"])
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
    try:
        review = EditableProfileReview(
            schema_version=REVIEW_DRAFT_SCHEMA_VERSION,
            sources=sources,
            facts=tuple(facts),
            missing_user_fields=tuple(missing),
            user_inputs=tuple((name, "") for name in missing),
            issue_count=payload["issue_count"],
            _preference_model_json=_preference_model_json(payload["preference_model"]),
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
    canonical = json.dumps(
        json.loads(_serialize_profile_intake_checkpoint_unchecked(review)),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    incoming = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if (
        not hmac.compare_digest(incoming, payload_json)
        or not hmac.compare_digest(canonical, incoming)
    ):
        raise ProfileIntakeError("invalid_checkpoint_content")
    return review


def _serialize_profile_intake_checkpoint_unchecked(review: EditableProfileReview) -> str:
    """Internal canonicalizer used to avoid recursive validation."""

    source_origins = tuple(source.document_kind.value for source in review.sources)
    conflict_ids = {}
    facts = []
    for fact in review.facts:
        conflict_id = None
        if fact.conflict_group is not None:
            conflict_id = conflict_ids.setdefault(fact.conflict_group, fact.conflict_group)
        facts.append({
            "field_path": fact.field_path,
            "value": _checkpoint_value(fact.value),
            "source_origins": [item.document_kind.value for item in fact.source_attributions],
            "suggested": fact.suggested,
            "decision": fact.decision,
            "conflict_id": conflict_id,
        })
    return json.dumps({
        "schema_version": PROFILE_INTAKE_CHECKPOINT_SCHEMA_VERSION,
        "review_schema_version": review.schema_version,
        "source_origins": list(source_origins),
        "facts": facts,
        "missing_user_fields": list(review.missing_user_fields),
        "user_inputs": dict(review.user_inputs),
        "issue_count": review.issue_count,
        "preference_model": review.preference_model,
    }, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


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
            value = float(candidate)
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
