"""Strict ephemeral contracts for profile-intake documents and model output.

Nothing in this module carries persistent-profile authority.  In particular,
the contracts intentionally have no account, principal, profile, revision,
durable source, entitlement, or matcher-signal fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
import re
import secrets
from typing import Any, Mapping

from wahojobs.profiles.canonical import (
    AVAILABILITY_STATUSES,
    CONTRIBUTION_TYPES,
    CREDENTIAL_STATUSES,
    EDUCATION_COMPLETION_STATUSES,
    EDUCATION_LEVELS,
    EMPLOYMENT_TYPES,
    LANGUAGE_PROFICIENCIES,
    PHONE_PREFERENCES,
    SCHEDULE_PREFERENCES,
    SENIORITY_LEVELS,
    SYNCHRONOUS_PREFERENCES,
)


AI_EXTRACTION_SCHEMA_VERSION = "ai_profile_extraction_v1"
MAX_EXTRACTION_FACTS = 256
MAX_FACT_STRING_CHARS = 512
MAX_EXTRACTION_STRUCTURE_DEPTH = 12
MAX_EXTRACTION_STRUCTURE_NODES = 4096

_DOCUMENT_REFERENCE = re.compile(r"doc_[0-9a-f]{32}")
_EVIDENCE_REFERENCE = re.compile(r"b[0-9]{3}")
_VERSION_LABEL = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_PARSER_VERSION = re.compile(r"[0-9a-z][0-9a-z_.+-]{0,63}")


class ProfileIntakeError(ValueError):
    """A fail-closed intake error whose string contains only a stable code."""

    def __init__(self, code: str, *, diagnostics: Mapping[str, int | str] | None = None):
        if type(code) is not str or _VERSION_LABEL.fullmatch(code) is None:
            code = "internal_profile_intake_error"
        safe_diagnostics: dict[str, int | str] = {}
        for key, value in (diagnostics or {}).items():
            if (
                type(key) is str
                and _VERSION_LABEL.fullmatch(key) is not None
                and type(value) in (int, str)
                and (type(value) is int or _VERSION_LABEL.fullmatch(value) is not None)
            ):
                safe_diagnostics[key] = value
        self.code = code
        self.diagnostics = safe_diagnostics
        super().__init__(code)


class DocumentKind(str, Enum):
    RESUME = "resume"
    LINKEDIN_PROFILE_EXPORT = "linkedin_profile_export"


class DocumentFormat(str, Enum):
    PDF = "pdf"
    DOCX = "docx"


@dataclass(frozen=True, slots=True)
class DocumentLimits:
    max_upload_bytes: int = 10 * 1024 * 1024
    max_pdf_pages: int = 50
    max_normalized_text_chars: int = 100_000
    max_evidence_blocks: int = 128
    max_evidence_block_chars: int = 2_000
    min_extractable_alphanumeric_chars: int = 20
    max_docx_members: int = 256
    max_docx_uncompressed_bytes: int = 50 * 1024 * 1024
    max_docx_member_bytes: int = 20 * 1024 * 1024
    max_docx_compression_ratio: int = 100
    max_relationship_xml_bytes: int = 1024 * 1024

    def __post_init__(self):
        values = tuple(getattr(self, name) for name in self.__dataclass_fields__)
        if any(type(value) is not int or value < 1 for value in values):
            raise ProfileIntakeError("invalid_document_limits")
        if self.max_evidence_blocks > 999:
            raise ProfileIntakeError("invalid_document_limits")


DEFAULT_DOCUMENT_LIMITS = DocumentLimits()


def new_document_reference() -> str:
    """Return a server-generated opaque reference with no durable authority."""

    return f"doc_{secrets.token_hex(16)}"


def _require_document_reference(value: object) -> str:
    if type(value) is not str or _DOCUMENT_REFERENCE.fullmatch(value) is None:
        raise ProfileIntakeError("invalid_document_reference")
    return value


@dataclass(frozen=True, slots=True)
class ParserMetadata:
    parser: str
    version: str

    def __post_init__(self):
        if (
            type(self.parser) is not str
            or _VERSION_LABEL.fullmatch(self.parser) is None
            or type(self.version) is not str
            or _PARSER_VERSION.fullmatch(self.version) is None
        ):
            raise ProfileIntakeError("invalid_parser_metadata")


@dataclass(frozen=True, slots=True)
class EvidenceBlock:
    reference: str
    text: str

    def __post_init__(self):
        if type(self.reference) is not str or _EVIDENCE_REFERENCE.fullmatch(self.reference) is None:
            raise ProfileIntakeError("invalid_evidence_reference")
        if type(self.text) is not str or not self.text or "\x00" in self.text:
            raise ProfileIntakeError("invalid_evidence_block")
        if len(self.text) > DEFAULT_DOCUMENT_LIMITS.max_evidence_block_chars:
            raise ProfileIntakeError("evidence_block_too_large")


def _validate_evidence_blocks(
    blocks: tuple[EvidenceBlock, ...],
    *,
    limits: DocumentLimits = DEFAULT_DOCUMENT_LIMITS,
) -> None:
    if type(blocks) is not tuple or not blocks:
        raise ProfileIntakeError("no_extractable_text")
    if len(blocks) > limits.max_evidence_blocks:
        raise ProfileIntakeError("evidence_block_limit_exceeded")
    total_characters = sum(len(block.text) for block in blocks)
    total_characters += max(0, len(blocks) - 1) * 2
    if total_characters > limits.max_normalized_text_chars:
        raise ProfileIntakeError("extracted_text_too_large")
    for index, block in enumerate(blocks, start=1):
        if type(block) is not EvidenceBlock:
            raise ProfileIntakeError("invalid_evidence_block")
        if block.reference != f"b{index:03d}":
            raise ProfileIntakeError("invalid_evidence_order")
        if len(block.text) > limits.max_evidence_block_chars:
            raise ProfileIntakeError("evidence_block_too_large")


@dataclass(frozen=True, slots=True)
class EvidencePacket:
    document_reference: str
    document_kind: DocumentKind
    document_format: DocumentFormat
    blocks: tuple[EvidenceBlock, ...]

    def __post_init__(self):
        _require_document_reference(self.document_reference)
        if type(self.document_kind) is not DocumentKind:
            raise ProfileIntakeError("invalid_document_kind")
        if type(self.document_format) is not DocumentFormat:
            raise ProfileIntakeError("invalid_document_format")
        _validate_evidence_blocks(self.blocks)


@dataclass(frozen=True, slots=True)
class ModelEvidenceBlock:
    """One PII-minimized block that is safe to cross the model boundary."""

    reference: str
    text: str

    def __post_init__(self):
        if type(self.reference) is not str or _EVIDENCE_REFERENCE.fullmatch(self.reference) is None:
            raise ProfileIntakeError("invalid_model_evidence_reference")
        if type(self.text) is not str or not self.text or "\x00" in self.text:
            raise ProfileIntakeError("invalid_model_evidence_block")
        if len(self.text) > DEFAULT_DOCUMENT_LIMITS.max_evidence_block_chars:
            raise ProfileIntakeError("model_evidence_block_too_large")


@dataclass(frozen=True, slots=True)
class ModelEvidencePacket:
    """Bounded, minimized evidence; intentionally distinct from raw evidence."""

    document_reference: str
    document_kind: DocumentKind
    document_format: DocumentFormat
    blocks: tuple[ModelEvidenceBlock, ...]
    removed_block_references: tuple[str, ...] = ()

    def __post_init__(self):
        _require_document_reference(self.document_reference)
        if type(self.document_kind) is not DocumentKind:
            raise ProfileIntakeError("invalid_document_kind")
        if type(self.document_format) is not DocumentFormat:
            raise ProfileIntakeError("invalid_document_format")
        if type(self.blocks) is not tuple or any(
            type(block) is not ModelEvidenceBlock for block in self.blocks
        ):
            raise ProfileIntakeError("invalid_model_evidence_blocks")
        if type(self.removed_block_references) is not tuple or any(
            type(reference) is not str
            or _EVIDENCE_REFERENCE.fullmatch(reference) is None
            for reference in self.removed_block_references
        ):
            raise ProfileIntakeError("invalid_removed_evidence_references")
        if len(self.blocks) + len(self.removed_block_references) > DEFAULT_DOCUMENT_LIMITS.max_evidence_blocks:
            raise ProfileIntakeError("model_evidence_block_limit_exceeded")
        kept = tuple(block.reference for block in self.blocks)
        removed = self.removed_block_references
        if len(set(kept + removed)) != len(kept) + len(removed):
            raise ProfileIntakeError("duplicate_model_evidence_reference")
        if kept != tuple(sorted(kept)) or removed != tuple(sorted(removed)):
            raise ProfileIntakeError("invalid_model_evidence_order")
        all_references = tuple(sorted(kept + removed))
        expected = tuple(f"b{index:03d}" for index in range(1, len(all_references) + 1))
        if all_references != expected:
            raise ProfileIntakeError("invalid_model_evidence_order")
        total_characters = sum(len(block.text) for block in self.blocks)
        total_characters += max(0, len(self.blocks) - 1) * 2
        if total_characters > DEFAULT_DOCUMENT_LIMITS.max_normalized_text_chars:
            raise ProfileIntakeError("model_evidence_too_large")


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    document_reference: str
    document_kind: DocumentKind
    document_format: DocumentFormat
    original_byte_size: int
    normalized_text_chars: int
    page_count: int | None
    parser: ParserMetadata
    evidence_blocks: tuple[EvidenceBlock, ...]

    def __post_init__(self):
        _require_document_reference(self.document_reference)
        if type(self.document_kind) is not DocumentKind:
            raise ProfileIntakeError("invalid_document_kind")
        if type(self.document_format) is not DocumentFormat:
            raise ProfileIntakeError("invalid_document_format")
        if (
            type(self.original_byte_size) is not int
            or not 1 <= self.original_byte_size <= DEFAULT_DOCUMENT_LIMITS.max_upload_bytes
        ):
            raise ProfileIntakeError("invalid_original_byte_size")
        if (
            type(self.normalized_text_chars) is not int
            or not 1
            <= self.normalized_text_chars
            <= DEFAULT_DOCUMENT_LIMITS.max_normalized_text_chars
        ):
            raise ProfileIntakeError("invalid_normalized_text_size")
        if self.document_format is DocumentFormat.PDF:
            if type(self.page_count) is not int or not 1 <= self.page_count <= DEFAULT_DOCUMENT_LIMITS.max_pdf_pages:
                raise ProfileIntakeError("invalid_page_count")
        elif self.page_count is not None:
            raise ProfileIntakeError("invalid_page_count")
        if type(self.parser) is not ParserMetadata:
            raise ProfileIntakeError("invalid_parser_metadata")
        _validate_evidence_blocks(self.evidence_blocks)

    def evidence_packet(self) -> EvidencePacket:
        return EvidencePacket(
            document_reference=self.document_reference,
            document_kind=self.document_kind,
            document_format=self.document_format,
            blocks=self.evidence_blocks,
        )


@dataclass(frozen=True, slots=True)
class LanguageValue:
    language: str
    proficiency: str | None
    locale: str | None


@dataclass(frozen=True, slots=True)
class ExtractedFact:
    field_path: str
    value: str | int | float | bool | LanguageValue
    source_document_reference: str
    evidence_block_references: tuple[str, ...]
    confidence: float
    explicit: bool

    def __post_init__(self):
        spec = _FIELD_SPECS.get(self.field_path)
        if spec is None:
            raise ProfileIntakeError("unsupported_extraction_field")
        if _validate_fact_value(self.value, spec) != self.value:
            raise ProfileIntakeError("invalid_fact_value")
        _require_document_reference(self.source_document_reference)
        if (
            type(self.evidence_block_references) is not tuple
            or not self.evidence_block_references
            or len(self.evidence_block_references) > 16
            or len(set(self.evidence_block_references)) != len(self.evidence_block_references)
            or any(
                type(reference) is not str
                or _EVIDENCE_REFERENCE.fullmatch(reference) is None
                for reference in self.evidence_block_references
            )
        ):
            raise ProfileIntakeError("invalid_fact_evidence")
        if (
            type(self.confidence) is not float
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            raise ProfileIntakeError("invalid_confidence")
        if type(self.explicit) is not bool:
            raise ProfileIntakeError("invalid_explicit_flag")
        if spec.explicit_only and not self.explicit:
            raise ProfileIntakeError("inferred_sensitive_fact_forbidden")
        if spec.inferred_only and self.explicit:
            raise ProfileIntakeError("classification_must_be_inferred")


@dataclass(frozen=True, slots=True)
class AIProfileExtraction:
    schema_version: str
    document_reference: str
    facts: tuple[ExtractedFact, ...]

    def __post_init__(self):
        if self.schema_version != AI_EXTRACTION_SCHEMA_VERSION:
            raise ProfileIntakeError("invalid_extraction_schema_version")
        _require_document_reference(self.document_reference)
        if type(self.facts) is not tuple or len(self.facts) > MAX_EXTRACTION_FACTS:
            raise ProfileIntakeError("extraction_fact_limit_exceeded")
        if any(type(fact) is not ExtractedFact for fact in self.facts):
            raise ProfileIntakeError("invalid_extraction_fact")
        singleton_fields: set[str] = set()
        identities: set[tuple[str, object]] = set()
        for fact in self.facts:
            if fact.source_document_reference != self.document_reference:
                raise ProfileIntakeError("document_reference_mismatch")
            spec = _FIELD_SPECS[fact.field_path]
            if not spec.multiple:
                if fact.field_path in singleton_fields:
                    raise ProfileIntakeError("duplicate_singleton_fact")
                singleton_fields.add(fact.field_path)
            identity = (fact.field_path, _value_identity(fact.value))
            if identity in identities:
                raise ProfileIntakeError("duplicate_extraction_fact")
            identities.add(identity)


@dataclass(frozen=True, slots=True)
class _FieldSpec:
    kind: str
    multiple: bool = False
    allowed: frozenset[str] | None = None
    explicit_only: bool = False
    inferred_only: bool = False


def _strings(*, multiple=False, explicit_only=False):
    return _FieldSpec("string", multiple=multiple, explicit_only=explicit_only)


def _enum(values, *, multiple=False, explicit_only=False, inferred_only=False):
    return _FieldSpec(
        "enum",
        multiple=multiple,
        allowed=frozenset(values),
        explicit_only=explicit_only,
        inferred_only=inferred_only,
    )


_FIELD_SPECS = {
    "identity.display_name": _strings(),
    "languages": _FieldSpec("language", multiple=True),
    "location.country": _strings(explicit_only=True),
    "location.region": _strings(explicit_only=True),
    "location.city": _strings(explicit_only=True),
    "location.residence": _strings(explicit_only=True),
    "education.education_level": _enum(EDUCATION_LEVELS, inferred_only=True),
    "education.degrees": _strings(multiple=True),
    "education.fields_or_domains": _strings(multiple=True),
    "education.institutions": _strings(multiple=True),
    "education.graduation_years": _FieldSpec("completion_year", multiple=True),
    "education.completion_status": _enum(EDUCATION_COMPLETION_STATUSES, inferred_only=True),
    "credentials.certifications": _strings(multiple=True),
    "credentials.licenses": _strings(multiple=True),
    "credentials.jurisdictions": _strings(multiple=True),
    "credentials.security_clearances": _strings(multiple=True),
    "credentials.credential_status": _enum(CREDENTIAL_STATUSES, inferred_only=True),
    "experience.total_years": _FieldSpec("years"),
    "experience.seniority": _enum(SENIORITY_LEVELS, inferred_only=True),
    "experience.recent_roles": _strings(multiple=True),
    "experience.occupational_families": _FieldSpec("string", multiple=True, inferred_only=True),
    "experience.job_titles": _strings(multiple=True),
    "experience.professional_domains": _FieldSpec("string", multiple=True, inferred_only=True),
    "experience.industries": _FieldSpec("string", multiple=True, inferred_only=True),
    "experience.contribution_type": _enum(CONTRIBUTION_TYPES, inferred_only=True),
    "experience.specialties": _FieldSpec("string", multiple=True, inferred_only=True),
    "skills.normalized": _strings(multiple=True),
    "preferences.remote": _FieldSpec("boolean", explicit_only=True),
    "preferences.flexible": _FieldSpec("boolean", explicit_only=True),
    "preferences.employment_types": _enum(EMPLOYMENT_TYPES, multiple=True, explicit_only=True),
    "preferences.synchronous_preference": _enum(SYNCHRONOUS_PREFERENCES, explicit_only=True),
    "preferences.phone_preference": _enum(PHONE_PREFERENCES, explicit_only=True),
    "preferences.schedule": _enum(SCHEDULE_PREFERENCES, multiple=True, explicit_only=True),
    "preferences.availability": _enum(AVAILABILITY_STATUSES, explicit_only=True),
    "preferences.target_opportunity_types": _strings(multiple=True, explicit_only=True),
    "preferences.preferred_task_types": _strings(multiple=True, explicit_only=True),
    "preferences.work_preferences": _strings(multiple=True, explicit_only=True),
}

SUPPORTED_EXTRACTION_FIELD_PATHS = frozenset(_FIELD_SPECS)
EXPLICIT_ONLY_EXTRACTION_FIELD_PATHS = frozenset(
    path for path, spec in _FIELD_SPECS.items() if spec.explicit_only
)
INFERRED_ONLY_EXTRACTION_FIELD_PATHS = frozenset(
    path for path, spec in _FIELD_SPECS.items() if spec.inferred_only
)

# These classifications may remain useful to server-side profile and matching
# projections, but they are not candidate-authored preferences or useful V1
# review questions.  Browser review never accepts values for these paths.
INTERNAL_INFERRED_CLASSIFICATION_FIELD_PATHS = frozenset(
    {
        "experience.occupational_families",
        "experience.professional_domains",
        "experience.contribution_type",
    }
)
if not INTERNAL_INFERRED_CLASSIFICATION_FIELD_PATHS <= (
    INFERRED_ONLY_EXTRACTION_FIELD_PATHS
):
    raise RuntimeError("invalid_internal_inferred_classification_paths")

_FORBIDDEN_AUTHORITY_KEYS = frozenset(
    {
        "account_id",
        "principal_id",
        "profile_id",
        "revision_id",
        "source_id",
        "source_ordinal",
        "durable_provenance",
        "provenance",
        "entitlement",
        "entitlement_state",
        "derived_matcher_signals",
        "matcher_signals",
    }
)


def _bounded_json_copy(value: object, *, depth: int = 0, counter: list[int] | None = None):
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_EXTRACTION_STRUCTURE_NODES:
        raise ProfileIntakeError("extraction_structure_too_large")
    if depth > MAX_EXTRACTION_STRUCTURE_DEPTH:
        raise ProfileIntakeError("extraction_structure_too_deep")
    if type(value) is dict:
        copied = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ProfileIntakeError("invalid_extraction_structure")
            copied[key] = _bounded_json_copy(item, depth=depth + 1, counter=counter)
        return copied
    if type(value) is list:
        return [
            _bounded_json_copy(item, depth=depth + 1, counter=counter)
            for item in value
        ]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise ProfileIntakeError("invalid_extraction_structure")


def _contains_forbidden_authority(value: object) -> bool:
    if type(value) is dict:
        for key, item in value.items():
            if key in _FORBIDDEN_AUTHORITY_KEYS or _contains_forbidden_authority(item):
                return True
    elif type(value) is list:
        return any(_contains_forbidden_authority(item) for item in value)
    return False


def _exact_keys(value: object, keys: frozenset[str], code: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != set(keys):
        raise ProfileIntakeError(code)
    return value


def _normalized_string(value: object) -> str:
    if type(value) is not str or "\x00" in value:
        raise ProfileIntakeError("invalid_fact_value")
    normalized = " ".join(value.split())
    if not normalized:
        raise ProfileIntakeError("invalid_fact_value")
    if len(normalized) > MAX_FACT_STRING_CHARS:
        raise ProfileIntakeError("fact_value_too_large")
    return normalized


def _validate_language(value: object) -> LanguageValue:
    if type(value) is LanguageValue:
        value = {
            "language": value.language,
            "proficiency": value.proficiency,
            "locale": value.locale,
        }
    item = _exact_keys(
        value,
        frozenset({"language", "proficiency", "locale"}),
        "invalid_language_value",
    )
    language = _normalized_string(item["language"])
    proficiency = item["proficiency"]
    if proficiency is not None:
        proficiency = _normalized_string(proficiency)
        if proficiency not in LANGUAGE_PROFICIENCIES:
            raise ProfileIntakeError("invalid_fact_enum")
    locale = item["locale"]
    if locale is not None:
        locale = _normalized_string(locale)
    return LanguageValue(language=language, proficiency=proficiency, locale=locale)


def _validate_fact_value(value: object, spec: _FieldSpec):
    if spec.kind == "string":
        return _normalized_string(value)
    if spec.kind == "enum":
        normalized = _normalized_string(value)
        if normalized not in spec.allowed:
            raise ProfileIntakeError("invalid_fact_enum")
        return normalized
    if spec.kind == "boolean":
        if type(value) is not bool:
            raise ProfileIntakeError("invalid_fact_value")
        return value
    if spec.kind == "years":
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 80:
            raise ProfileIntakeError("invalid_fact_number")
        return value
    if spec.kind == "completion_year":
        if type(value) is not int or not 1900 <= value <= 2200:
            raise ProfileIntakeError("invalid_fact_number")
        return value
    if spec.kind == "language":
        return _validate_language(value)
    raise ProfileIntakeError("internal_profile_intake_error")


def _value_identity(value: object) -> object:
    if type(value) is LanguageValue:
        return (value.language, value.proficiency, value.locale)
    return value


def _validation_value_shape(value: object) -> str | None:
    if type(value) is str:
        return "string"
    if type(value) is bool:
        return "boolean"
    if type(value) in (int, float):
        return "number"
    if type(value) is list:
        return "array"
    if type(value) is dict:
        if set(value) == {"language", "proficiency", "locale"}:
            return "language_object"
        return "object"
    if value is None:
        return "null"
    return None


def _fact_validation_diagnostics(
    raw_fact: object, fact_index: int
) -> dict[str, int | str]:
    diagnostics: dict[str, int | str] = {"fact_index": fact_index}
    if type(raw_fact) is not dict:
        return diagnostics

    field_path = raw_fact.get("field_path")
    if type(field_path) is str and field_path in SUPPORTED_EXTRACTION_FIELD_PATHS:
        diagnostics["field_path"] = field_path

    if "value" in raw_fact:
        value_shape = _validation_value_shape(raw_fact["value"])
        if value_shape is not None:
            diagnostics["value_shape"] = value_shape

    explicit = raw_fact.get("explicit")
    if type(explicit) is bool:
        diagnostics["explicit"] = "true" if explicit else "false"

    references = raw_fact.get("evidence_block_references")
    if type(references) is list:
        diagnostics["evidence_reference_count"] = len(references)
        if all(type(reference) is str for reference in references):
            diagnostics["evidence_references_contain_duplicates"] = (
                "true" if len(set(references)) != len(references) else "false"
            )
    return diagnostics


def validate_ai_profile_extraction(
    value: object,
    evidence: EvidencePacket | ModelEvidencePacket,
) -> AIProfileExtraction:
    """Validate untrusted future-adapter output against one evidence packet."""

    if type(evidence) not in (EvidencePacket, ModelEvidencePacket):
        raise ProfileIntakeError("invalid_evidence_packet")
    value = _bounded_json_copy(value)
    if _contains_forbidden_authority(value):
        raise ProfileIntakeError("forbidden_importer_authority")
    root = _exact_keys(
        value,
        frozenset({"schema_version", "document_reference", "facts"}),
        "invalid_extraction_envelope",
    )
    if root["schema_version"] != AI_EXTRACTION_SCHEMA_VERSION:
        raise ProfileIntakeError("invalid_extraction_schema_version")
    if root["document_reference"] != evidence.document_reference:
        raise ProfileIntakeError("document_reference_mismatch")
    raw_facts = root["facts"]
    if type(raw_facts) is not list:
        raise ProfileIntakeError("invalid_extraction_facts")
    if len(raw_facts) > MAX_EXTRACTION_FACTS:
        raise ProfileIntakeError("extraction_fact_limit_exceeded")

    known_references = {block.reference for block in evidence.blocks}
    facts: list[ExtractedFact] = []
    singleton_fields: dict[str, int] = {}
    fact_identities: dict[tuple[str, object], int] = {}
    for fact_index, raw_fact in enumerate(raw_facts):
        safe_diagnostics = _fact_validation_diagnostics(raw_fact, fact_index)
        try:
            item = _exact_keys(
                raw_fact,
                frozenset(
                    {
                        "field_path",
                        "value",
                        "source_document_reference",
                        "evidence_block_references",
                        "confidence",
                        "explicit",
                    }
                ),
                "invalid_extraction_fact",
            )
            field_path = item["field_path"]
            if type(field_path) is not str or field_path not in _FIELD_SPECS:
                raise ProfileIntakeError("unsupported_extraction_field")
            spec = _FIELD_SPECS[field_path]
            normalized_value = _validate_fact_value(item["value"], spec)
            explicit = item["explicit"]
            if type(explicit) is not bool:
                raise ProfileIntakeError("invalid_explicit_flag")
            if spec.explicit_only and not explicit:
                raise ProfileIntakeError("inferred_sensitive_fact_forbidden")
            if spec.inferred_only and explicit:
                raise ProfileIntakeError("classification_must_be_inferred")
            source_reference = item["source_document_reference"]
            if source_reference != evidence.document_reference:
                raise ProfileIntakeError("document_reference_mismatch")
            references = item["evidence_block_references"]
            if (
                type(references) is not list
                or not references
                or len(references) > 16
                or any(type(reference) is not str for reference in references)
                or len(set(references)) != len(references)
            ):
                raise ProfileIntakeError("invalid_fact_evidence")
            if any(reference not in known_references for reference in references):
                raise ProfileIntakeError("unknown_evidence_reference")
            confidence = item["confidence"]
            if (
                type(confidence) not in (int, float)
                or not math.isfinite(confidence)
                or not 0 <= confidence <= 1
            ):
                raise ProfileIntakeError("invalid_confidence")
            if not spec.multiple:
                if field_path in singleton_fields:
                    safe_diagnostics["duplicate_prior_fact_index"] = singleton_fields[
                        field_path
                    ]
                    raise ProfileIntakeError("duplicate_singleton_fact")
                singleton_fields[field_path] = fact_index
            identity = (field_path, _value_identity(normalized_value))
            if identity in fact_identities:
                safe_diagnostics["duplicate_prior_fact_index"] = fact_identities[
                    identity
                ]
                raise ProfileIntakeError("duplicate_extraction_fact")
            fact_identities[identity] = fact_index
            facts.append(
                ExtractedFact(
                    field_path=field_path,
                    value=normalized_value,
                    source_document_reference=source_reference,
                    evidence_block_references=tuple(references),
                    confidence=float(confidence),
                    explicit=explicit,
                )
            )
        except ProfileIntakeError as exc:
            raise ProfileIntakeError(exc.code, diagnostics=safe_diagnostics) from None

    return AIProfileExtraction(
        schema_version=AI_EXTRACTION_SCHEMA_VERSION,
        document_reference=evidence.document_reference,
        facts=tuple(facts),
    )
