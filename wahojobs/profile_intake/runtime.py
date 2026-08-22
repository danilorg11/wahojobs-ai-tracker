"""Authenticated, non-durable runtime primitives for AI profile intake.

This module deliberately owns no database writer. Durable state is consulted only
to revalidate the browser session, account-native principal, and exact PB-OWN-1
lineage before process-local review state is accessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import base64
import hashlib
import hmac
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
from wahojobs.profile_intake.minimization import minimize_evidence_packet
from wahojobs.profile_intake.openai_adapter import (
    ProfileExtractionDiagnostics,
    ProfileExtractionOutcome,
)
from wahojobs.profile_intake.review_draft import (
    AIProfileReviewDraft,
    ReviewDraftSource,
    ReviewSourceAttribution,
    ValidatedProfileSource,
    reconcile_profile_extractions,
)


PROFILE_INTAKE_ROUTE = "/account/profile/intake"
PROFILE_INTAKE_REVIEW_ROUTE = "/account/profile/intake/review"
PROFILE_INTAKE_PURPOSE = "ai_profile_intake_review_v1"
PROFILE_INTAKE_DRAFT_LIFETIME_SECONDS = 600
PROFILE_INTAKE_DRAFT_CAPACITY = 64
PROFILE_INTAKE_CSRF_MESSAGE_PREFIX = b"wahojobs.profile-intake.v1\x00"
MAX_REVIEW_VALUE_CHARS = 512
MAX_REVIEW_USER_INPUT_CHARS = 512

_OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_ACTIONS = frozenset({"upload", "update", "cancel"})
_REQUEST_ROUTES = frozenset({PROFILE_INTAKE_ROUTE, PROFILE_INTAKE_REVIEW_ROUTE})
_GRANT_ISSUER = object()


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

    @property
    def document_reference(self):
        return self.sources[0].document_reference if len(self.sources) == 1 else None


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
    version: int

    def __repr__(self):
        return (
            "IntakeDraftSnapshot("
            f"document={self.document!r}, version={self.version}, content=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class _DraftRecord:
    binding: tuple = field(repr=False)
    snapshot: IntakeDraftSnapshot = field(repr=False)


class IntakeDraftVault:
    """Bounded, process-local draft storage with exact lineage binding."""

    __slots__ = ("_capacity", "_closed", "_lock", "_monotonic", "_records", "_token_factory", "_ttl")

    def __init__(
        self,
        *,
        monotonic=time.monotonic,
        token_factory=lambda: secrets.token_urlsafe(32),
        ttl_seconds=PROFILE_INTAKE_DRAFT_LIFETIME_SECONDS,
        capacity=PROFILE_INTAKE_DRAFT_CAPACITY,
    ):
        if (
            not callable(monotonic)
            or not callable(token_factory)
            or type(ttl_seconds) not in (int, float)
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
            or type(capacity) is not int
            or capacity < 1
        ):
            raise _configuration_error()
        self._monotonic = monotonic
        self._token_factory = token_factory
        self._ttl = float(ttl_seconds)
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

    def issue(self, grant, review, document, diagnostics, *, created_at):
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
            snapshot = IntakeDraftSnapshot(
                review=review,
                document=document,
                diagnostics=diagnostics,
                created_at=created_at.astimezone(timezone.utc).isoformat(),
                expires_at_monotonic=now + self._ttl,
                version=1,
            )
            self._records[reference] = _DraftRecord(binding=binding, snapshot=snapshot)
            return reference, snapshot

    def get(self, reference, grant):
        binding = _grant_binding(grant)
        if type(reference) is not str or _OPAQUE_REFERENCE.fullmatch(reference) is None:
            return None
        now = _monotonic(self._monotonic())
        with self._lock:
            self._purge_locked(now)
            record = self._records.get(reference)
            if record is None or record.binding != binding:
                return None
            return record.snapshot

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
            self._purge_locked(now)
            record = self._records.get(reference)
            if record is None or record.binding != binding:
                return "gone", None
            if record.snapshot.version != expected_version:
                return "stale", None
            snapshot = replace(
                record.snapshot,
                review=review,
                version=expected_version + 1,
            )
            self._records[reference] = replace(record, snapshot=snapshot)
            return "updated", snapshot

    def cancel(self, reference, grant, *, expected_version):
        binding = _grant_binding(grant)
        if type(reference) is not str or _OPAQUE_REFERENCE.fullmatch(reference) is None:
            return "gone"
        now = _monotonic(self._monotonic())
        with self._lock:
            self._purge_locked(now)
            record = self._records.get(reference)
            if record is None or record.binding != binding:
                return "gone"
            if record.snapshot.version != expected_version:
                return "stale"
            del self._records[reference]
            return "cancelled"

    def _purge_locked(self, now):
        for reference, record in tuple(self._records.items()):
            if now >= record.snapshot.expires_at_monotonic:
                del self._records[reference]


class ProfileIntakeProcessingService:
    """Run the ephemeral extraction pipeline outside every DB transaction."""

    __slots__ = ("_adapter", "_clock", "_guard", "_in_flight", "_vault")

    def __init__(self, *, adapter, vault, clock):
        if (
            adapter is not None
            and not callable(getattr(adapter, "extract", None))
        ) or type(vault) is not IntakeDraftVault or not callable(clock):
            raise _configuration_error()
        self._adapter = adapter
        self._vault = vault
        self._clock = clock
        self._guard = threading.Lock()
        self._in_flight = set()

    @property
    def vault(self):
        return self._vault

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
            return self._vault.issue(
                grant,
                review,
                SafeDocumentBundleMetadata(tuple(metadata_items)),
                tuple(diagnostic_items) if diagnostic_items else None,
                created_at=self._clock(),
            )
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
    return EditableProfileReview(
        schema_version=draft.schema_version,
        sources=draft.sources,
        facts=facts,
        missing_user_fields=draft.missing_user_fields,
        user_inputs=tuple((name, "") for name in draft.missing_user_fields),
        issue_count=len(draft.issues),
    )


def update_editable_review(review, values, decisions, user_inputs):
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
        allowed = {"accept", "reject"} if fact.suggested else {"keep", "remove"}
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
    return replace(
        review,
        facts=tuple(updated),
        user_inputs=tuple(normalized_inputs),
    )


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
