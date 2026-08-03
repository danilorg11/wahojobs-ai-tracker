"""Canonical Profile V2 durable contract and pure V1 compatibility adapters.

This module is deliberately dormant.  It performs no I/O and is not imported
by normal product runtime paths.  V2 is the future durable representation;
canonical_profile_v1 remains the normalizer input and matcher compatibility
format until a separately approved migration and service milestone.
"""

from __future__ import annotations

from copy import deepcopy
from functools import wraps
from itertools import islice
import json
import math
import re
from types import MappingProxyType
import unicodedata

from wahojobs.persistent_profile_schema import (
    STRUCTURED_PROFILE_DENIED_KEY_FORMS,
    STRUCTURED_PROFILE_KEY_PATTERN,
)
from wahojobs.profiles.canonical import (
    CONFIDENCE_LEVELS,
    LANGUAGE_PROFICIENCIES,
    PROFILE_SOURCE_USER_CONFIRMATION,
    PROFILE_SOURCE_USER_CORRECTION,
    PROFILE_SOURCES,
    SCHEMA_VERSION as V1_SCHEMA_VERSION,
    UNKNOWN,
    canonical_to_matcher_profile,
    field_sources_for_profile,
    validate_canonical_profile,
)


SCHEMA_VERSION = "canonical_profile_v2"
FIELD_PATH_VERSION = "canonical_profile_v2_path_v1"

CANONICAL_PROFILE_V2_LIMITS = MappingProxyType(
    {
        "document_bytes": 131_072,
        "document_nodes": 4_096,
        "document_depth": 12,
        "object_children": 256,
        "list_children": 256,
        "structural_key_length": 64,
        "scalar_string_length": 4_096,
        "dynamic_label_length": 128,
        "display_name_length": 160,
        "languages": 32,
        "domain_year_records": 64,
        "skill_records": 96,
        "derived_signals": 64,
        "signal_keywords": 32,
        "signal_keyword_length": 128,
        "signal_reason_length": 64,
        "signal_points_absolute": 100,
        "field_source_records": 256,
        "field_path_length": 256,
        "field_path_steps": 12,
        "source_ordinal_value": 16,
        "source_ordinals_per_field": 16,
        "string_list_items": 128,
        "decimal_places": 2,
        "matcher_profile_id_length": 128,
    }
)

STRUCTURED_WHITESPACE_POLICY = (
    "Structured Canonical Profile V2 values normalize Unicode to NFC; trim leading "
    "and trailing permitted Unicode space separators; collapse repeated permitted "
    "Unicode space separators to one ASCII space; and reject tab U+0009, LF U+000A, "
    "CR U+000D, all other C0 controls, DEL U+007F, and C1 controls before "
    "normalization."
)

MAX_DOCUMENT_BYTES = CANONICAL_PROFILE_V2_LIMITS["document_bytes"]
MAX_DOCUMENT_NODES = CANONICAL_PROFILE_V2_LIMITS["document_nodes"]
MAX_DOCUMENT_DEPTH = CANONICAL_PROFILE_V2_LIMITS["document_depth"]
MAX_OBJECT_CHILDREN = CANONICAL_PROFILE_V2_LIMITS["object_children"]
MAX_LIST_CHILDREN = CANONICAL_PROFILE_V2_LIMITS["list_children"]
MAX_STRUCTURAL_KEY_LENGTH = CANONICAL_PROFILE_V2_LIMITS["structural_key_length"]
MAX_SCALAR_LENGTH = CANONICAL_PROFILE_V2_LIMITS["scalar_string_length"]
MAX_DYNAMIC_LABEL_LENGTH = CANONICAL_PROFILE_V2_LIMITS["dynamic_label_length"]
MAX_DISPLAY_NAME_LENGTH = CANONICAL_PROFILE_V2_LIMITS["display_name_length"]
MAX_LANGUAGES = CANONICAL_PROFILE_V2_LIMITS["languages"]
MAX_DOMAIN_YEARS = CANONICAL_PROFILE_V2_LIMITS["domain_year_records"]
MAX_SKILL_ENTRIES = CANONICAL_PROFILE_V2_LIMITS["skill_records"]
MAX_SIGNALS = CANONICAL_PROFILE_V2_LIMITS["derived_signals"]
MAX_SIGNAL_KEYWORDS = CANONICAL_PROFILE_V2_LIMITS["signal_keywords"]
MAX_SIGNAL_KEYWORD_LENGTH = CANONICAL_PROFILE_V2_LIMITS["signal_keyword_length"]
MAX_SIGNAL_REASON_LENGTH = CANONICAL_PROFILE_V2_LIMITS["signal_reason_length"]
MAX_SIGNAL_POINTS = CANONICAL_PROFILE_V2_LIMITS["signal_points_absolute"]
MAX_FIELD_SOURCES = CANONICAL_PROFILE_V2_LIMITS["field_source_records"]
MAX_FIELD_PATH_LENGTH = CANONICAL_PROFILE_V2_LIMITS["field_path_length"]
MAX_FIELD_PATH_STEPS = CANONICAL_PROFILE_V2_LIMITS["field_path_steps"]
MAX_SOURCE_ORDINAL = CANONICAL_PROFILE_V2_LIMITS["source_ordinal_value"]
MAX_SOURCE_ORDINALS_PER_FIELD = CANONICAL_PROFILE_V2_LIMITS[
    "source_ordinals_per_field"
]
MAX_MATCHER_PROFILE_ID_LENGTH = CANONICAL_PROFILE_V2_LIMITS[
    "matcher_profile_id_length"
]

_PROFILE_ID_PATTERN = re.compile(r"^prf_([0-9a-f]{32})$")
_DURABLE_RESOURCE_PREFIXES = (
    "prf",
    "pvr",
    "pfs",
    "usr",
    "auth",
    "ses",
    "inv",
    "rot",
    "cns",
    "del",
    "life",
    "prn",
    "loa",
    "pab",
    "obe",
)
_DURABLE_RESOURCE_PATTERN = re.compile(
    rf"(?:{'|'.join(_DURABLE_RESOURCE_PREFIXES)})_"
    r"[0-9a-f]{32}",
    re.IGNORECASE,
)
_MATCHER_PROFILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SIGNAL_REASON_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_FIELD_SEGMENT_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PROVENANCE_ROOTS = frozenset(
    {
        "languages",
        "location",
        "education",
        "credentials",
        "experience",
        "skills",
        "preferences",
        "constraints",
    }
)
_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "identity",
        "languages",
        "location",
        "education",
        "credentials",
        "experience",
        "skills",
        "preferences",
        "constraints",
        "derived_matcher_signals",
        "provenance",
    }
)
_IDENTITY_FIELDS = frozenset({"profile_id", "display_name"})
_LANGUAGE_FIELDS = frozenset(
    {
        "language",
        "proficiency",
        "locale",
        "confidence",
        "proficiency_explicit",
        "provenance",
    }
)
_SKILL_ENTRY_FIELDS = frozenset({"skill", "confidence", "provenance"})
_SIGNAL_FIELDS = frozenset({"reason", "keywords", "points", "confidence"})
_FIELD_SOURCE_FIELDS = frozenset(
    {"field_path", "path_version", "source_ordinals", "source_kind", "explicit"}
)
_DOMAIN_YEAR_FIELDS = frozenset({"domain", "years"})
_PROVENANCE_FIELDS = frozenset(
    {
        "extracted_from",
        "confidence",
        "missing_fields",
        "ambiguous_fields",
        "reviewed",
        "field_sources",
    }
)

_SECTION_FIELDS = {
    "location": frozenset(
        {
            "country",
            "region",
            "city",
            "timezone",
            "residence",
            "work_authorization",
            "eligible_countries",
            "remote_eligibility",
            "restrictions",
            "geographic_work_restrictions",
        }
    ),
    "education": frozenset(
        {
            "education_level",
            "degrees",
            "fields_or_domains",
            "institutions",
            "graduation_years",
            "completion_status",
        }
    ),
    "credentials": frozenset(
        {
            "certifications",
            "licenses",
            "jurisdictions",
            "security_clearances",
            "credential_status",
        }
    ),
    "experience": frozenset(
        {
            "total_years",
            "years_by_domain",
            "seniority",
            "recent_roles",
            "occupational_families",
            "job_titles",
            "professional_domains",
            "industries",
            "contribution_type",
            "specialties",
        }
    ),
    "skills": frozenset(
        {
            "normalized",
            "free_text_labels",
            "entries",
            "technical",
            "software_tools",
            "writing_research",
            "administrative_support",
            "domain_specific",
        }
    ),
    "preferences": frozenset(
        {
            "remote",
            "flexible",
            "employment_types",
            "synchronous_preference",
            "phone_preference",
            "schedule",
            "availability",
            "rate_pay_preference",
            "target_opportunity_types",
            "preferred_task_types",
            "work_preferences",
        }
    ),
    "constraints": frozenset(
        {
            "hard_constraints",
            "soft_preferences",
            "avoid_keywords",
            "negative_constraints",
            "excluded_domains",
            "accessibility_constraints",
        }
    ),
    "derived_matcher_signals": frozenset(
        {"signals", "derived_domains", "derived_target_work_types", "avoid_keywords"}
    ),
}

_STRING_LIST_FIELDS = {
    "location": (
        "eligible_countries",
        "restrictions",
        "geographic_work_restrictions",
    ),
    "education": ("degrees", "fields_or_domains", "institutions"),
    "credentials": ("certifications", "licenses", "jurisdictions", "security_clearances"),
    "experience": (
        "recent_roles",
        "occupational_families",
        "job_titles",
        "professional_domains",
        "industries",
        "specialties",
    ),
    "skills": (
        "normalized",
        "free_text_labels",
        "technical",
        "software_tools",
        "writing_research",
        "administrative_support",
        "domain_specific",
    ),
    "preferences": (
        "employment_types",
        "schedule",
        "target_opportunity_types",
        "preferred_task_types",
        "work_preferences",
    ),
    "constraints": (
        "hard_constraints",
        "soft_preferences",
        "avoid_keywords",
        "negative_constraints",
        "excluded_domains",
        "accessibility_constraints",
    ),
    "derived_matcher_signals": (
        "derived_domains",
        "derived_target_work_types",
        "avoid_keywords",
    ),
}

_REMOVED_V1_PROVENANCE_PREFIXES = (
    "identity.",
    "provenance.",
    "derived_matcher_signals.",
    "matcher_compatible_profile.",
)


class CanonicalProfileV2Error(ValueError):
    """Bounded validation/conversion failure that never includes profile values."""

    def __init__(self, *reason_codes: str):
        codes = tuple(sorted(set(reason_codes or ("invalid_profile",))))[:32]
        self.reason_codes = codes
        super().__init__(
            "canonical_profile_v2 rejected; reason_codes=" + ",".join(codes)
        )

    def __repr__(self) -> str:
        return f"CanonicalProfileV2Error(reason_codes={self.reason_codes!r})"

    def as_dict(self) -> dict:
        return {"error": "canonical_profile_v2_rejected", "reason_codes": list(self.reason_codes)}


class _DuplicateJsonKey(ValueError):
    pass


def _sanitized_public_boundary(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except CanonicalProfileV2Error:
            raise
        except RecursionError as exc:
            raise CanonicalProfileV2Error("document_too_deep") from exc
        except (OverflowError, MemoryError) as exc:
            raise CanonicalProfileV2Error("invalid_structure") from exc
        except Exception as exc:
            raise CanonicalProfileV2Error("internal_validation_failure") from exc

    return wrapped


@_sanitized_public_boundary
def normalize_comparison_label(value: str) -> str:
    """Return the comparison-only key for a validated human label."""
    if type(value) is not str:
        raise CanonicalProfileV2Error("invalid_label")
    _preflight_structure(value)
    return _normalize_durable_string(value).casefold()


@_sanitized_public_boundary
def parse_canonical_profile_v2_json(raw_json: str | bytes) -> dict:
    """Parse raw V2 JSON with duplicate-key and non-finite-number rejection."""

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJsonKey
            result[key] = value
        return result

    def reject_constant(_value):
        raise ValueError

    try:
        parsed = json.loads(
            raw_json,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except _DuplicateJsonKey as exc:
        raise CanonicalProfileV2Error("duplicate_json_key") from exc
    except RecursionError as exc:
        raise CanonicalProfileV2Error("document_too_deep") from exc
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise CanonicalProfileV2Error("invalid_json") from exc
    if type(parsed) is not dict:
        raise CanonicalProfileV2Error("root_not_object")
    return validate_canonical_profile_v2(parsed)


@_sanitized_public_boundary
def canonical_profile_v2_json_bytes(value: dict) -> bytes:
    """Return deterministic UTF-8 bytes for one validated V2 document."""
    validated = validate_canonical_profile_v2(value)
    try:
        return json.dumps(
            validated,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (UnicodeEncodeError, ValueError, TypeError) as exc:
        raise CanonicalProfileV2Error("serialization_failed") from exc


@_sanitized_public_boundary
def parse_field_path(field_path: str) -> tuple[str | int, ...]:
    """Parse the bounded canonical_profile_v2_path_v1 grammar."""
    if type(field_path) is not str:
        raise CanonicalProfileV2Error("invalid_field_path")
    if not field_path or len(field_path) > MAX_FIELD_PATH_LENGTH:
        raise CanonicalProfileV2Error("invalid_field_path")
    if field_path != unicodedata.normalize("NFC", field_path):
        raise CanonicalProfileV2Error("invalid_field_path")

    tokens: list[str | int] = []
    position = 0
    while position < len(field_path):
        match = _FIELD_SEGMENT_PATTERN.match(field_path, position)
        if match is None:
            raise CanonicalProfileV2Error("invalid_field_path")
        tokens.append(match.group(0))
        position = match.end()
        while position < len(field_path) and field_path[position] == "[":
            closing = field_path.find("]", position + 1)
            if closing < 0:
                raise CanonicalProfileV2Error("invalid_field_path")
            index_text = field_path[position + 1 : closing]
            if (
                not index_text.isascii()
                or not index_text.isdigit()
                or (len(index_text) > 1 and index_text.startswith("0"))
            ):
                raise CanonicalProfileV2Error("invalid_field_path")
            index = int(index_text)
            if index > 255:
                raise CanonicalProfileV2Error("invalid_field_path")
            tokens.append(index)
            position = closing + 1
        if position == len(field_path):
            break
        if field_path[position] != ".":
            raise CanonicalProfileV2Error("invalid_field_path")
        position += 1
        if position == len(field_path):
            raise CanonicalProfileV2Error("invalid_field_path")
    if not tokens or tokens[0] not in _PROVENANCE_ROOTS:
        raise CanonicalProfileV2Error("prohibited_field_path_root")
    if len(tokens) > MAX_FIELD_PATH_STEPS:
        raise CanonicalProfileV2Error("field_path_too_deep")
    return tuple(tokens)


@_sanitized_public_boundary
def resolve_field_path(profile_v2: dict, field_path: str):
    """Resolve one valid path to a material V2 scalar value."""
    profile = validate_canonical_profile_v2(profile_v2)
    return _resolve_field_path(profile, field_path)


def _resolve_field_path(profile_v2: dict, field_path: str):
    value = profile_v2
    for token in parse_field_path(field_path):
        if type(token) is int:
            if type(value) is not list or token >= len(value):
                raise CanonicalProfileV2Error("field_path_not_found")
            value = value[token]
        else:
            if type(value) is not dict or token not in value:
                raise CanonicalProfileV2Error("field_path_not_found")
            value = value[token]
    if type(value) in {dict, list} or value in (None, ""):
        raise CanonicalProfileV2Error("field_path_not_material")
    return deepcopy(value)


@_sanitized_public_boundary
def validate_canonical_profile_v2(value: dict) -> dict:
    """Validate V2 and return a defensive deep copy.

    Duplicate object keys can only be detected at the raw JSON boundary; an
    already-parsed Python dictionary has necessarily lost that information.
    """
    _preflight_structure(value)
    if type(value) is not dict:
        raise CanonicalProfileV2Error("root_not_object")
    candidate = _canonicalize_profile(_normalized_deepcopy(value))
    errors: list[str] = []

    _validate_structural_limits(candidate, errors)
    _require_exact_keys(candidate, _ROOT_FIELDS, "invalid_root_fields", errors)
    if candidate.get("schema_version") != SCHEMA_VERSION:
        errors.append("invalid_schema_version")
    _validate_identity(candidate.get("identity"), errors)
    _validate_languages(candidate.get("languages"), errors)
    for section in (
        "location",
        "education",
        "credentials",
        "experience",
        "skills",
        "preferences",
        "constraints",
        "derived_matcher_signals",
    ):
        _validate_section(section, candidate.get(section), errors)
    _validate_provenance(candidate, errors)

    if not errors:
        try:
            compatibility = _v2_to_v1(candidate, matcher_profile_id="canonical_v2_validation")
            validate_canonical_profile(compatibility)
        except (ValueError, TypeError, KeyError, CanonicalProfileV2Error):
            errors.append("v1_semantic_incompatibility")

    if errors:
        raise CanonicalProfileV2Error(*errors)
    return candidate


@_sanitized_public_boundary
def convert_v1_to_v2(
    v1: dict,
    *,
    persistent_profile_id: str,
    source_ordinal_resolver,
) -> dict:
    """Convert validated V1 content into deterministic durable V2 content.

    The resolver is called as ``resolver(field_path, source_kind, explicit)``
    and must return a nonempty iterable of unique source ordinals from 1 to 16.
    """
    _preflight_structure(v1, structured_keys=False)
    original = deepcopy(v1)
    try:
        validate_canonical_profile(original)
    except (ValueError, TypeError) as exc:
        raise CanonicalProfileV2Error("invalid_v1_input") from exc
    return _convert_validated_v1_to_v2(
        original,
        persistent_profile_id=persistent_profile_id,
        source_ordinal_resolver=source_ordinal_resolver,
    )


def _convert_validated_v1_to_v2(
    original,
    *,
    persistent_profile_id,
    source_ordinal_resolver,
):
    """Convert a private V1 value that already passed its domain validator."""
    if not _is_valid_profile_id(persistent_profile_id):
        raise CanonicalProfileV2Error("invalid_persistent_profile_id")
    if not callable(source_ordinal_resolver):
        raise CanonicalProfileV2Error("invalid_source_resolver")

    languages, language_indexes = _convert_languages(original["languages"])
    skills, skill_indexes = _convert_skills(original["skills"])
    experience, domain_indexes = _convert_experience(original["experience"])
    derived = _convert_signals(original["derived_matcher_signals"])

    profile_v2 = {
        "schema_version": SCHEMA_VERSION,
        "identity": {
            "profile_id": persistent_profile_id,
            "display_name": _nfc(original["identity"]["display_name"]),
        },
        "languages": languages,
        "location": _nfc_copy(original["location"]),
        "education": _nfc_copy(original["education"]),
        "credentials": _nfc_copy(original["credentials"]),
        "experience": experience,
        "skills": skills,
        "preferences": _nfc_copy(original["preferences"]),
        "constraints": _nfc_copy(original["constraints"]),
        "derived_matcher_signals": derived,
        "provenance": {
            "extracted_from": _nfc(original["provenance"]["extracted_from"]),
            "confidence": original["provenance"].get("confidence", UNKNOWN),
            "missing_fields": _nfc_copy(original["provenance"].get("missing_fields", [])),
            "ambiguous_fields": _nfc_copy(original["provenance"].get("ambiguous_fields", [])),
            "reviewed": bool(original["provenance"].get("reviewed", False)),
            "field_sources": [],
        },
    }

    converted_sources = []
    for old_path, detail in original["provenance"]["field_sources"].items():
        mapped_paths = _map_v1_field_source_path(
            old_path,
            language_indexes=language_indexes,
            skill_indexes=skill_indexes,
            domain_indexes=domain_indexes,
        )
        if mapped_paths is None:
            continue
        try:
            ordinals = source_ordinal_resolver(
                old_path,
                detail["source"],
                bool(detail["explicit"]),
            )
        except Exception as exc:
            raise CanonicalProfileV2Error("source_resolution_failed") from exc
        ordinals = _validated_ordinals(ordinals)
        for mapped_path in mapped_paths:
            converted_sources.append(
                {
                    "field_path": mapped_path,
                    "path_version": FIELD_PATH_VERSION,
                    "source_ordinals": ordinals,
                    "source_kind": detail["source"],
                    "explicit": bool(detail["explicit"]),
                }
            )
    converted_sources.sort(key=lambda item: (item["field_path"].casefold(), item["field_path"]))
    profile_v2["provenance"]["field_sources"] = converted_sources
    return validate_canonical_profile_v2(profile_v2)


@_sanitized_public_boundary
def project_v2_to_review_v1(v2: dict) -> dict:
    """Project authoritative V2 content into identity-free V1 review state.

    This adapter is deliberately separate from the lossy matcher projection.
    Review state preserves all durable semantic content, including the ordered
    ``years_by_domain`` records, while rebuilding V1 field provenance from the
    server-validated snapshot as an explicit user confirmation.
    """
    profile_v2 = validate_canonical_profile_v2(v2)
    persistent_profile_id = profile_v2["identity"]["profile_id"]

    languages = [dict(deepcopy(item), evidence=[]) for item in profile_v2["languages"]]
    skills = deepcopy(profile_v2["skills"])
    skills["entries"] = [
        dict(deepcopy(item), evidence=[])
        for item in profile_v2["skills"].get("entries", [])
    ]
    signals = [
        dict(deepcopy(item), evidence=[])
        for item in profile_v2["derived_matcher_signals"]["signals"]
    ]
    experience = deepcopy(profile_v2["experience"])
    experience["years_by_domain"] = {
        item["domain"]: item["years"]
        for item in profile_v2["experience"]["years_by_domain"]
    }

    review_v1 = {
        "schema_version": V1_SCHEMA_VERSION,
        "identity": {
            "display_name": profile_v2["identity"]["display_name"],
            "source_inputs": [],
        },
        "languages": languages,
        "location": deepcopy(profile_v2["location"]),
        "education": deepcopy(profile_v2["education"]),
        "credentials": deepcopy(profile_v2["credentials"]),
        "experience": experience,
        "skills": skills,
        "preferences": deepcopy(profile_v2["preferences"]),
        "constraints": deepcopy(profile_v2["constraints"]),
        "derived_matcher_signals": {
            **deepcopy(profile_v2["derived_matcher_signals"]),
            "signals": signals,
        },
        "matcher_compatible_profile": {},
        "provenance": {
            "extracted_from": profile_v2["provenance"]["extracted_from"],
            "evidence_snippets": [],
            "confidence": profile_v2["provenance"]["confidence"],
            "missing_fields": deepcopy(profile_v2["provenance"]["missing_fields"]),
            "ambiguous_fields": deepcopy(profile_v2["provenance"]["ambiguous_fields"]),
            "reviewed": True,
        },
    }
    sources = field_sources_for_profile(
        review_v1,
        PROFILE_SOURCE_USER_CONFIRMATION,
        explicit=True,
    )
    review_v1["provenance"]["field_sources"] = {
        path: sources[path]
        for path in sorted(sources, key=lambda value: (value.casefold(), value))
    }

    # Validate the complete V1 shape through the accepted validator without
    # placing any synthetic identity in the returned review document.
    validation_copy = deepcopy(review_v1)
    validation_copy["identity"]["profile_id"] = "canonical_v2_review_validation"
    validation_copy["provenance"]["field_sources"] = field_sources_for_profile(
        validation_copy,
        PROFILE_SOURCE_USER_CONFIRMATION,
        explicit=True,
    )
    try:
        validate_canonical_profile(validation_copy)
    except (ValueError, TypeError, KeyError) as exc:
        raise CanonicalProfileV2Error("invalid_v1_review_projection") from exc

    if (
        _contains_text_fragment(review_v1, persistent_profile_id)
        or _contains_durable_resource_id(review_v1)
    ):
        raise CanonicalProfileV2Error("persistent_identity_leak")
    return deepcopy(review_v1)


@_sanitized_public_boundary
def merge_server_review_correction_v2(
    base_v2: dict,
    baseline_review_v1: dict,
    corrected_review_v1: dict,
) -> dict:
    """Merge one server-validated review delta into an authoritative V2 snapshot.

    The review form is intentionally narrower than Canonical V2.  This merge
    therefore compares two server-produced review results, copies only fields
    whose normalized review semantics changed, and leaves every unrepresented
    V2 value and ordered record on the trusted base untouched.  Browser input
    is never accepted as a V2 document.
    """
    base = validate_canonical_profile_v2(base_v2)
    profile_id = base["identity"]["profile_id"]
    baseline = _validated_bound_review_v1(baseline_review_v1, profile_id)
    corrected = _validated_bound_review_v1(corrected_review_v1, profile_id)
    field_changes = [
        (name, review_path, target_paths)
        for name, review_path, target_paths in _REVIEW_CORRECTION_FIELD_TARGETS
        if _nested_value(baseline, review_path)
        != _nested_value(corrected, review_path)
    ]
    languages_changed = baseline["languages"] != corrected["languages"]
    skills_changed = (
        baseline["skills"]["normalized"]
        != corrected["skills"]["normalized"]
    )
    changed_fields = {name for name, _path, _targets in field_changes}
    changed_roots = {
        target_path[0]
        for _name, _path, target_paths in field_changes
        for target_path in target_paths
    }
    if languages_changed:
        changed_fields.add("languages")
        changed_roots.add("languages")
    if skills_changed:
        changed_fields.add("skills")
        changed_roots.add("skills")

    candidate = None
    if changed_fields:
        candidate_review = project_v2_to_review_v1(base)
        for _name, review_path, target_paths in field_changes:
            _set_nested_value(
                candidate_review,
                review_path,
                _nested_value(corrected, review_path),
            )
            for target_path in target_paths:
                _set_nested_value(
                    candidate_review,
                    target_path,
                    _nested_value(corrected, target_path),
                )
        if languages_changed:
            candidate_review["languages"] = deepcopy(corrected["languages"])
        if skills_changed:
            candidate_review["skills"] = deepcopy(corrected["skills"])
        for trigger_fields, target_paths in _REVIEW_CORRECTION_DEPENDENT_TARGETS:
            if changed_fields.intersection(trigger_fields):
                for target_path in target_paths:
                    _set_nested_value(
                        candidate_review,
                        target_path,
                        _nested_value(corrected, target_path),
                    )
        candidate_review["matcher_compatible_profile"] = {}
        candidate_review["provenance"]["missing_fields"] = deepcopy(
            corrected["provenance"]["missing_fields"]
        )
        candidate_review["provenance"]["ambiguous_fields"] = deepcopy(
            corrected["provenance"]["ambiguous_fields"]
        )
        candidate_review["provenance"]["reviewed"] = True
        candidate_review["provenance"]["field_sources"] = field_sources_for_profile(
            candidate_review,
            PROFILE_SOURCE_USER_CONFIRMATION,
            explicit=True,
        )
        candidate = _convert_validated_v1_to_v2(
            _validated_bound_review_v1(candidate_review, profile_id),
            persistent_profile_id=profile_id,
            source_ordinal_resolver=lambda _path, _kind, _explicit: (2,),
        )

    result = deepcopy(base)
    for _name, _review_path, target_paths in field_changes:
        for target_path in target_paths:
            _set_nested_value(
                result,
                target_path,
                _nested_value(candidate, target_path),
            )

    if languages_changed:
        result["languages"] = _preserve_unchanged_language_records(
            base["languages"],
            candidate["languages"],
        )

    if skills_changed:
        result["skills"]["normalized"] = deepcopy(
            candidate["skills"]["normalized"]
        )
        result["skills"]["free_text_labels"] = deepcopy(
            candidate["skills"]["free_text_labels"]
        )
        result["skills"]["entries"] = _preserve_unchanged_skill_records(
            base["skills"]["entries"],
            candidate["skills"]["entries"],
        )

    for trigger_fields, target_paths in _REVIEW_CORRECTION_DEPENDENT_TARGETS:
        if changed_fields.intersection(trigger_fields):
            for target_path in target_paths:
                _set_nested_value(
                    result,
                    target_path,
                    _nested_value(candidate, target_path),
                )

    if changed_fields:
        result["provenance"]["missing_fields"] = deepcopy(
            candidate["provenance"]["missing_fields"]
        )
        result["provenance"]["ambiguous_fields"] = deepcopy(
            candidate["provenance"]["ambiguous_fields"]
        )
    result["provenance"]["reviewed"] = True
    result["provenance"]["field_sources"] = [
        {
            "field_path": path,
            "path_version": FIELD_PATH_VERSION,
            "source_ordinals": [2 if _field_path_root(path) in changed_roots else 1],
            "source_kind": (
                PROFILE_SOURCE_USER_CORRECTION
                if _field_path_root(path) in changed_roots
                else PROFILE_SOURCE_USER_CONFIRMATION
            ),
            "explicit": True,
        }
        for path in sorted(
            _material_field_paths(result),
            key=lambda value: (value.casefold(), value),
        )
    ]
    return validate_canonical_profile_v2(result)


_REVIEW_CORRECTION_FIELD_TARGETS = (
    ("country", ("location", "country"), (("location", "country"), ("location", "residence"))),
    ("region", ("location", "region"), (("location", "region"),)),
    ("city", ("location", "city"), (("location", "city"),)),
    (
        "work_authorization",
        ("location", "work_authorization"),
        (("location", "work_authorization"),),
    ),
    (
        "eligible_countries",
        ("location", "eligible_countries"),
        (("location", "eligible_countries"),),
    ),
    (
        "geographic_restrictions",
        ("location", "geographic_work_restrictions"),
        (("location", "restrictions"), ("location", "geographic_work_restrictions")),
    ),
    (
        "remote",
        ("preferences", "remote"),
        (("location", "remote_eligibility"), ("preferences", "remote")),
    ),
    (
        "education_level",
        ("education", "education_level"),
        (("education", "education_level"),),
    ),
    ("degrees", ("education", "degrees"), (("education", "degrees"),)),
    (
        "education_fields",
        ("education", "fields_or_domains"),
        (("education", "fields_or_domains"),),
    ),
    (
        "institutions",
        ("education", "institutions"),
        (("education", "institutions"),),
    ),
    (
        "education_status",
        ("education", "completion_status"),
        (("education", "completion_status"),),
    ),
    (
        "credential_status",
        ("credentials", "credential_status"),
        (("credentials", "credential_status"),),
    ),
    (
        "certifications",
        ("credentials", "certifications"),
        (("credentials", "certifications"),),
    ),
    ("licenses", ("credentials", "licenses"), (("credentials", "licenses"),)),
    (
        "jurisdictions",
        ("credentials", "jurisdictions"),
        (("credentials", "jurisdictions"),),
    ),
    (
        "security_clearances",
        ("credentials", "security_clearances"),
        (("credentials", "security_clearances"),),
    ),
    (
        "total_years",
        ("experience", "total_years"),
        (("experience", "total_years"),),
    ),
    ("seniority", ("experience", "seniority"), (("experience", "seniority"),)),
    (
        "job_titles",
        ("experience", "job_titles"),
        (("experience", "recent_roles"), ("experience", "job_titles")),
    ),
    (
        "occupational_families",
        ("experience", "occupational_families"),
        (("experience", "occupational_families"),),
    ),
    (
        "professional_domains",
        ("experience", "professional_domains"),
        (("experience", "professional_domains"),),
    ),
    ("industries", ("experience", "industries"), (("experience", "industries"),)),
    (
        "contribution_type",
        ("experience", "contribution_type"),
        (("experience", "contribution_type"),),
    ),
    (
        "specialties",
        ("experience", "specialties"),
        (("experience", "specialties"),),
    ),
    (
        "technical_skills",
        ("skills", "technical"),
        (("skills", "technical"),),
    ),
    (
        "software_tools",
        ("skills", "software_tools"),
        (("skills", "software_tools"),),
    ),
    (
        "writing_research_skills",
        ("skills", "writing_research"),
        (("skills", "writing_research"),),
    ),
    (
        "administrative_support_skills",
        ("skills", "administrative_support"),
        (("skills", "administrative_support"),),
    ),
    (
        "domain_specific_skills",
        ("skills", "domain_specific"),
        (("skills", "domain_specific"),),
    ),
    (
        "employment_types",
        ("preferences", "employment_types"),
        (("preferences", "employment_types"),),
    ),
    (
        "synchronous_preference",
        ("preferences", "synchronous_preference"),
        (("preferences", "synchronous_preference"),),
    ),
    (
        "phone_preference",
        ("preferences", "phone_preference"),
        (("preferences", "phone_preference"),),
    ),
    ("schedule", ("preferences", "schedule"), (("preferences", "schedule"),)),
    (
        "availability",
        ("preferences", "availability"),
        (("preferences", "availability"),),
    ),
    (
        "target_opportunity_types",
        ("preferences", "target_opportunity_types"),
        (
            ("preferences", "target_opportunity_types"),
            ("preferences", "preferred_task_types"),
        ),
    ),
    ("flexible", ("preferences", "flexible"), (("preferences", "flexible"),)),
    (
        "hard_constraints",
        ("constraints", "hard_constraints"),
        (("constraints", "hard_constraints"),),
    ),
    (
        "soft_preferences",
        ("constraints", "soft_preferences"),
        (("constraints", "soft_preferences"),),
    ),
    (
        "avoid_keywords",
        ("constraints", "avoid_keywords"),
        (("constraints", "avoid_keywords"),),
    ),
    (
        "excluded_domains",
        ("constraints", "excluded_domains"),
        (("constraints", "excluded_domains"),),
    ),
    (
        "accessibility_constraints",
        ("constraints", "accessibility_constraints"),
        (("constraints", "accessibility_constraints"),),
    ),
)

_REVIEW_CORRECTION_DEPENDENT_TARGETS = (
    (
        frozenset({"education_fields", "professional_domains"}),
        (("derived_matcher_signals", "derived_domains"),),
    ),
    (
        frozenset(
            {"languages", "education_fields", "professional_domains", "skills"}
        ),
        (("derived_matcher_signals", "signals"),),
    ),
    (
        frozenset({"target_opportunity_types"}),
        (("derived_matcher_signals", "derived_target_work_types"),),
    ),
    (
        frozenset({"remote", "flexible", "employment_types"}),
        (("preferences", "work_preferences"),),
    ),
    (
        frozenset({"excluded_domains", "accessibility_constraints"}),
        (("constraints", "negative_constraints"),),
    ),
    (
        frozenset({"avoid_keywords", "excluded_domains"}),
        (
            ("constraints", "avoid_keywords"),
            ("derived_matcher_signals", "avoid_keywords"),
        ),
    ),
)


def _validated_bound_review_v1(review, profile_id):
    if type(review) is not dict or type(review.get("identity")) is not dict:
        raise CanonicalProfileV2Error("invalid_v1_review_projection")
    bound = deepcopy(review)
    if "profile_id" in bound["identity"]:
        raise CanonicalProfileV2Error("invalid_v1_review_projection")
    bound["identity"]["profile_id"] = profile_id
    sources = bound.get("provenance", {}).get("field_sources")
    identity_source = sources.get("identity.display_name") if type(sources) is dict else None
    if type(identity_source) is not dict:
        raise CanonicalProfileV2Error("invalid_v1_review_projection")
    sources["identity.profile_id"] = deepcopy(identity_source)
    try:
        validate_canonical_profile(bound)
    except (ValueError, TypeError, KeyError) as exc:
        raise CanonicalProfileV2Error("invalid_v1_review_projection") from exc
    return bound


def _nested_value(value, path):
    current = value
    for key in path:
        current = current[key]
    return current


def _set_nested_value(value, path, replacement):
    current = value
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = deepcopy(replacement)


def _preserve_unchanged_language_records(base, candidate):
    by_semantics = {
        (item["language"], item["proficiency"], item.get("locale", "")): item
        for item in base
    }
    return [
        deepcopy(
            by_semantics.get(
                (item["language"], item["proficiency"], item.get("locale", "")),
                item,
            )
        )
        for item in candidate
    ]


def _preserve_unchanged_skill_records(base, candidate):
    by_skill = {item["skill"]: item for item in base}
    return [deepcopy(by_skill.get(item["skill"], item)) for item in candidate]


def _field_path_root(path):
    return path.split(".", 1)[0].split("[", 1)[0]


@_sanitized_public_boundary
def project_v2_to_matcher_v1(v2: dict, *, matcher_profile_id: str) -> dict:
    """Project authoritative V2 content into an ephemeral valid V1 document."""
    profile_v2 = validate_canonical_profile_v2(v2)
    persistent_profile_id = profile_v2["identity"]["profile_id"]
    validate_ephemeral_matcher_profile_id(
        matcher_profile_id,
        persistent_profile_id=persistent_profile_id,
    )
    projected = _v2_to_v1(profile_v2, matcher_profile_id=matcher_profile_id)
    projected["matcher_compatible_profile"] = canonical_to_matcher_profile(projected)
    if _contains_text_fragment(projected, persistent_profile_id):
        raise CanonicalProfileV2Error("persistent_identity_leak")
    if _contains_durable_resource_id(projected):
        raise CanonicalProfileV2Error("persistent_identity_leak")
    try:
        validate_canonical_profile(projected)
    except (ValueError, TypeError, KeyError) as exc:
        raise CanonicalProfileV2Error("invalid_v1_projection") from exc
    return deepcopy(projected)


def _v2_to_v1(v2: dict, *, matcher_profile_id: str) -> dict:
    languages = []
    for item in v2["languages"]:
        projected = deepcopy(item)
        projected["evidence"] = []
        languages.append(projected)
    skills = deepcopy(v2["skills"])
    skills["entries"] = [dict(item, evidence=[]) for item in skills.get("entries", [])]
    signals = []
    for item in v2["derived_matcher_signals"]["signals"]:
        projected = deepcopy(item)
        projected["evidence"] = []
        signals.append(projected)
    experience = deepcopy(v2["experience"])
    # The production matcher does not consume years_by_domain.  Keeping this
    # empty avoids reintroducing dynamic object keys and Unicode/path ambiguity
    # at the V1 compatibility boundary.
    experience["years_by_domain"] = {}
    profile_v1 = {
        "schema_version": V1_SCHEMA_VERSION,
        "identity": {
            "profile_id": matcher_profile_id,
            "display_name": v2["identity"]["display_name"],
            "source_inputs": [],
        },
        "languages": languages,
        "location": deepcopy(v2["location"]),
        "education": deepcopy(v2["education"]),
        "credentials": deepcopy(v2["credentials"]),
        "experience": experience,
        "skills": skills,
        "preferences": deepcopy(v2["preferences"]),
        "constraints": deepcopy(v2["constraints"]),
        "derived_matcher_signals": {
            **deepcopy(v2["derived_matcher_signals"]),
            "signals": signals,
        },
        "matcher_compatible_profile": {},
        "provenance": {
            "extracted_from": v2["provenance"]["extracted_from"],
            "evidence_snippets": [],
            "confidence": v2["provenance"]["confidence"],
            "missing_fields": deepcopy(v2["provenance"]["missing_fields"]),
            "ambiguous_fields": deepcopy(v2["provenance"]["ambiguous_fields"]),
            "reviewed": bool(v2["provenance"]["reviewed"]),
        },
    }
    profile_v1["provenance"]["field_sources"] = field_sources_for_profile(
        profile_v1,
        "external_import",
        explicit=True,
    )
    return profile_v1


def _convert_languages(items: list) -> tuple[list, dict[int, int]]:
    converted = []
    for old_index, item in enumerate(items):
        converted.append(
            (
                old_index,
                {
                    key: _nfc_copy(value)
                    for key, value in item.items()
                    if key != "evidence"
                }
                | {"confidence": item.get("confidence", UNKNOWN)},
            )
        )
    converted.sort(
        key=lambda pair: (
            normalize_comparison_label(pair[1]["language"]),
            normalize_comparison_label(pair[1].get("locale", "")),
            pair[1]["language"],
            pair[1].get("locale", ""),
        )
    )
    indexes = {old: new for new, (old, _item) in enumerate(converted)}
    return [item for _old, item in converted], indexes


def _convert_skills(section: dict) -> tuple[dict, dict[int, int]]:
    result = _nfc_copy(section)
    converted = []
    for old_index, item in enumerate(section.get("entries", [])):
        converted.append(
            (
                old_index,
                {
                    key: _nfc_copy(value)
                    for key, value in item.items()
                    if key != "evidence"
                }
                | {"confidence": item.get("confidence", UNKNOWN)},
            )
        )
    converted.sort(
        key=lambda pair: (
            normalize_comparison_label(pair[1]["skill"]),
            pair[1]["skill"],
        )
    )
    indexes = {old: new for new, (old, _item) in enumerate(converted)}
    result["entries"] = [item for _old, item in converted]
    return result, indexes


def _convert_experience(section: dict) -> tuple[dict, dict[str, int]]:
    result = _nfc_copy(section)
    converted = [
        {"domain": _nfc(domain), "years": years}
        for domain, years in section.get("years_by_domain", {}).items()
    ]
    converted.sort(
        key=lambda item: (normalize_comparison_label(item["domain"]), item["domain"])
    )
    result["years_by_domain"] = converted
    indexes = {item["domain"]: index for index, item in enumerate(converted)}
    return result, indexes


def _convert_signals(section: dict) -> dict:
    result = _nfc_copy(section)
    signals = []
    for item in section.get("signals", []):
        converted = {
            key: _nfc_copy(value)
            for key, value in item.items()
            if key != "evidence"
        }
        converted.setdefault("confidence", UNKNOWN)
        converted["reason"] = _signal_reason_identifier(converted.get("reason", ""))
        converted["keywords"] = sorted(
            converted.get("keywords", []),
            key=lambda value: (normalize_comparison_label(value), value),
        )
        signals.append(converted)
    signals.sort(key=_signal_sort_key)
    result["signals"] = signals
    return result


def _map_v1_field_source_path(
    old_path: str,
    *,
    language_indexes: dict[int, int],
    skill_indexes: dict[int, int],
    domain_indexes: dict[str, int],
) -> tuple[str, ...] | None:
    if old_path.startswith(_REMOVED_V1_PROVENANCE_PREFIXES):
        return None
    if ".evidence[" in old_path or old_path.endswith(".evidence"):
        return None
    language = re.fullmatch(r"languages\[(\d+)\](\..+)", old_path)
    if language:
        old_index = int(language.group(1))
        return (f"languages[{language_indexes[old_index]}]{language.group(2)}",)
    skill = re.fullmatch(r"skills\.entries\[(\d+)\](\..+)", old_path)
    if skill:
        old_index = int(skill.group(1))
        return (f"skills.entries[{skill_indexes[old_index]}]{skill.group(2)}",)
    prefix = "experience.years_by_domain."
    if old_path.startswith(prefix):
        domain = _nfc(old_path[len(prefix) :])
        index = domain_indexes[domain]
        return (
            f"experience.years_by_domain[{index}].domain",
            f"experience.years_by_domain[{index}].years",
        )
    return (old_path,)


def _validated_ordinals(value) -> list[int]:
    if type(value) not in {list, tuple} or not value:
        raise CanonicalProfileV2Error("invalid_source_ordinals")
    if len(value) > MAX_SOURCE_ORDINALS_PER_FIELD:
        raise CanonicalProfileV2Error("invalid_source_ordinals")
    if any(type(item) is not int or not 1 <= item <= MAX_SOURCE_ORDINAL for item in value):
        raise CanonicalProfileV2Error("invalid_source_ordinals")
    result = sorted(value)
    if len(result) != len(set(result)):
        raise CanonicalProfileV2Error("invalid_source_ordinals")
    return result


def _preflight_structure(value, *, structured_keys=True) -> None:
    errors: list[str] = []
    nodes = 0
    active: set[int] = set()
    stack = [(value, 0, False)]
    while stack:
        item, depth, exiting = stack.pop()
        if exiting:
            active.discard(id(item))
            continue
        nodes += 1
        if nodes > MAX_DOCUMENT_NODES:
            errors.append("document_too_many_nodes")
            break
        if depth > MAX_DOCUMENT_DEPTH:
            errors.append("document_too_deep")
            continue
        if type(item) is dict:
            identity = id(item)
            if identity in active:
                errors.append("cyclic_structure")
                continue
            active.add(identity)
            stack.append((item, depth, True))
            if len(item) > MAX_OBJECT_CHILDREN:
                errors.append("object_too_large")
            children = tuple(islice(item.items(), MAX_OBJECT_CHILDREN + 1))
            for key, child in reversed(children):
                if type(key) is not str:
                    errors.append("invalid_structural_key")
                elif structured_keys:
                    if len(key) > MAX_STRUCTURAL_KEY_LENGTH:
                        errors.append("structural_key_too_large")
                    elif not STRUCTURED_PROFILE_KEY_PATTERN.fullmatch(key):
                        errors.append("invalid_structural_key")
                    elif key.replace("_", "") in STRUCTURED_PROFILE_DENIED_KEY_FORMS:
                        errors.append("prohibited_structural_key")
                elif len(key) > MAX_SCALAR_LENGTH:
                    errors.append("structural_key_too_large")
                stack.append((child, depth + 1, False))
        elif type(item) is list:
            identity = id(item)
            if identity in active:
                errors.append("cyclic_structure")
                continue
            active.add(identity)
            stack.append((item, depth, True))
            if len(item) > MAX_LIST_CHILDREN:
                errors.append("list_too_large")
            for child in reversed(item[: MAX_LIST_CHILDREN + 1]):
                stack.append((child, depth + 1, False))
        elif type(item) is str:
            if len(item) > MAX_SCALAR_LENGTH:
                errors.append("scalar_too_large")
            if any(
                ord(char) < 32
                or 127 <= ord(char) <= 159
                or 0xD800 <= ord(char) <= 0xDFFF
                for char in item
            ):
                errors.append("prohibited_control_character")
        elif type(item) is float and not math.isfinite(item):
            errors.append("non_finite_number")
        elif item is not None and type(item) not in {bool, int, float}:
            errors.append("unsupported_value_type")
        if len(errors) >= 32:
            break
    if errors:
        raise CanonicalProfileV2Error(*errors)


def _validate_structural_limits(value, errors: list[str]) -> None:
    try:
        _preflight_structure(value)
    except CanonicalProfileV2Error as exc:
        errors.extend(exc.reason_codes)
        return
    try:
        size = len(
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        )
        if size > MAX_DOCUMENT_BYTES:
            errors.append("document_too_large")
    except (UnicodeEncodeError, ValueError, TypeError):
        errors.append("invalid_json_value")


def _normalized_deepcopy(value):
    copied = deepcopy(value)

    def normalize(item):
        if type(item) is str:
            return _normalize_durable_string(item)
        if type(item) is float:
            if item == 0:
                return 0
            if item.is_integer():
                return int(item)
            if round(item, CANONICAL_PROFILE_V2_LIMITS["decimal_places"]) == item:
                return float(
                    f"{item:.{CANONICAL_PROFILE_V2_LIMITS['decimal_places']}f}"
                )
            return item
        if type(item) is list:
            return [normalize(child) for child in item]
        if type(item) is dict:
            return {key: normalize(child) for key, child in item.items()}
        return item

    return normalize(copied)


def _canonicalize_profile(profile):
    if type(profile) is not dict:
        return profile
    index_mappings: dict[str, dict[int, int]] = {}

    def sort_indexed(path, values, key):
        if type(values) is not list:
            return values
        indexed = list(enumerate(values))
        try:
            indexed.sort(key=lambda pair: key(pair[1]))
        except (AttributeError, KeyError, TypeError, ValueError):
            return values
        index_mappings[path] = {
            old_index: new_index
            for new_index, (old_index, _item) in enumerate(indexed)
        }
        return [item for _old_index, item in indexed]

    languages = profile.get("languages")
    if type(languages) is list and all(type(item) is dict for item in languages):
        profile["languages"] = sort_indexed(
            "languages",
            languages,
            lambda item: (
                normalize_comparison_label(item.get("language", "")),
                normalize_comparison_label(item.get("locale", "")),
                item.get("language", ""),
                item.get("locale", ""),
            ),
        )

    experience = profile.get("experience")
    if type(experience) is dict:
        years = experience.get("years_by_domain")
        if type(years) is list and all(type(item) is dict for item in years):
            experience["years_by_domain"] = sort_indexed(
                "experience.years_by_domain",
                years,
                lambda item: (
                    normalize_comparison_label(item.get("domain", "")),
                    item.get("domain", ""),
                ),
            )

    skills = profile.get("skills")
    if type(skills) is dict:
        entries = skills.get("entries")
        if type(entries) is list and all(type(item) is dict for item in entries):
            skills["entries"] = sort_indexed(
                "skills.entries",
                entries,
                lambda item: (
                    normalize_comparison_label(item.get("skill", "")),
                    item.get("skill", ""),
                ),
            )

    for section, fields in _STRING_LIST_FIELDS.items():
        section_value = profile.get(section)
        if type(section_value) is not dict:
            continue
        for field in fields:
            values = section_value.get(field)
            if type(values) is list and all(type(item) is str for item in values):
                section_value[field] = sort_indexed(
                    f"{section}.{field}",
                    values,
                    lambda item: (normalize_comparison_label(item), item),
                )

    education = profile.get("education")
    if type(education) is dict:
        years = education.get("graduation_years")
        if type(years) is list and all(type(item) is int for item in years):
            education["graduation_years"] = sort_indexed(
                "education.graduation_years",
                years,
                lambda item: item,
            )

    provenance = profile.get("provenance")
    if type(provenance) is dict:
        for field in ("missing_fields", "ambiguous_fields"):
            values = provenance.get(field)
            if type(values) is list and all(type(item) is str for item in values):
                provenance[field] = sorted(
                    values,
                    key=lambda item: (normalize_comparison_label(item), item),
                )

    derived = profile.get("derived_matcher_signals")
    if type(derived) is dict:
        signals = derived.get("signals")
        if type(signals) is list and all(type(item) is dict for item in signals):
            for signal in signals:
                keywords = signal.get("keywords")
                if type(keywords) is list and all(type(item) is str for item in keywords):
                    signal["keywords"] = sorted(
                        keywords,
                        key=lambda item: (normalize_comparison_label(item), item),
                    )
            derived["signals"] = sorted(signals, key=_signal_sort_key)

    if type(provenance) is dict and type(provenance.get("field_sources")) is list:
        for record in provenance["field_sources"]:
            if type(record) is not dict:
                continue
            path = record.get("field_path")
            if type(path) is str:
                record["field_path"] = _remap_field_path(path, index_mappings)
            if type(record.get("source_ordinals")) is list:
                try:
                    record["source_ordinals"] = sorted(record["source_ordinals"])
                except TypeError:
                    pass
        if all(type(item) is dict for item in provenance["field_sources"]):
            provenance["field_sources"] = sorted(
                provenance["field_sources"],
                key=lambda item: (
                    str(item.get("field_path", "")).casefold(),
                    str(item.get("field_path", "")),
                ),
            )
    return profile


def _remap_field_path(path, mappings):
    for collection_path in sorted(mappings, key=len, reverse=True):
        match = re.match(rf"^{re.escape(collection_path)}\[(\d+)\](.*)$", path)
        if match is None:
            continue
        old_index = int(match.group(1))
        if old_index in mappings[collection_path]:
            return (
                f"{collection_path}[{mappings[collection_path][old_index]}]"
                f"{match.group(2)}"
            )
    return path


def _validate_string_value(value: str, errors: list[str]) -> None:
    if value != unicodedata.normalize("NFC", value):
        errors.append("string_not_nfc")
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        errors.append("prohibited_control_character")


def _require_exact_keys(value, allowed, reason, errors):
    if type(value) is not dict or set(value) != set(allowed):
        errors.append(reason)


def _require_allowed_keys(value, allowed, reason, errors):
    if type(value) is not dict:
        errors.append(reason)
        return False
    if set(value) - set(allowed):
        errors.append(reason)
    return True


def _is_valid_profile_id(value) -> bool:
    if type(value) is not str:
        return False
    match = _PROFILE_ID_PATTERN.fullmatch(value)
    return bool(match and len(set(match.group(1))) > 1)


@_sanitized_public_boundary
def validate_ephemeral_matcher_profile_id(
    value,
    *,
    persistent_profile_id: str,
) -> str:
    """Validate one bounded runtime-only matcher identity."""
    if not _is_valid_profile_id(persistent_profile_id):
        raise CanonicalProfileV2Error("invalid_persistent_profile_id")
    if (
        type(value) is not str
        or not value
        or len(value) > MAX_MATCHER_PROFILE_ID_LENGTH
        or value != value.strip()
        or _MATCHER_PROFILE_ID_PATTERN.fullmatch(value) is None
    ):
        raise CanonicalProfileV2Error("invalid_matcher_profile_id")
    folded = value.casefold()
    if (
        persistent_profile_id.casefold() in folded
        or folded.startswith(tuple(f"{prefix}_" for prefix in _DURABLE_RESOURCE_PREFIXES))
        or _contains_durable_resource_id(value)
    ):
        raise CanonicalProfileV2Error("invalid_matcher_profile_id")
    return value


def _contains_durable_resource_id(value) -> bool:
    stack = [value]
    while stack:
        item = stack.pop()
        if type(item) is str:
            if _DURABLE_RESOURCE_PATTERN.search(item) is not None:
                return True
        elif type(item) is dict:
            stack.extend(item.keys())
            stack.extend(item.values())
        elif type(item) is list:
            stack.extend(item)
    return False


def _contains_text_fragment(value, fragment: str) -> bool:
    target = fragment.casefold()
    stack = [value]
    while stack:
        item = stack.pop()
        if type(item) is str:
            if target in item.casefold():
                return True
        elif type(item) is dict:
            stack.extend(item.keys())
            stack.extend(item.values())
        elif type(item) is list:
            stack.extend(item)
    return False


def _validate_profile_id(value, errors):
    if not _is_valid_profile_id(value):
        errors.append("invalid_persistent_profile_id")


def _validate_identity(value, errors):
    _require_exact_keys(value, _IDENTITY_FIELDS, "invalid_identity_fields", errors)
    if type(value) is not dict:
        return
    _validate_profile_id(value.get("profile_id"), errors)
    display_name = value.get("display_name")
    if (
        type(display_name) is not str
        or not display_name
        or display_name != display_name.strip()
        or len(display_name) > MAX_DISPLAY_NAME_LENGTH
    ):
        errors.append("invalid_display_name")


def _valid_dynamic_label(value) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and len(value) <= MAX_DYNAMIC_LABEL_LENGTH
        and value == unicodedata.normalize("NFC", value)
        and not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
    )


def _validate_languages(value, errors):
    if type(value) is not list or len(value) > MAX_LANGUAGES:
        errors.append("invalid_languages")
        return
    pair_keys = []
    base_keys = []
    sort_keys = []
    for item in value:
        if not _require_allowed_keys(item, _LANGUAGE_FIELDS, "invalid_language_item", errors):
            continue
        if not {"language", "proficiency", "locale", "confidence"} <= set(item):
            errors.append("invalid_language_item")
            continue
        language = item.get("language")
        locale = item.get("locale")
        if (
            not _valid_dynamic_label(language)
            or type(locale) is not str
            or (locale != "" and not _valid_dynamic_label(locale))
        ):
            errors.append("invalid_language_label")
            continue
        if item.get("proficiency") not in LANGUAGE_PROFICIENCIES:
            errors.append("invalid_language_proficiency")
        if item.get("confidence") not in CONFIDENCE_LEVELS:
            errors.append("invalid_confidence")
        if "proficiency_explicit" in item and type(item["proficiency_explicit"]) is not bool:
            errors.append("invalid_language_item")
        if "provenance" in item and item["provenance"] not in PROFILE_SOURCES:
            errors.append("invalid_source_kind")
        pair_key = (normalize_comparison_label(language), normalize_comparison_label(locale))
        pair_keys.append(pair_key)
        base_keys.append(pair_key[0])
        sort_keys.append(pair_key + (language, locale))
    if len(pair_keys) != len(set(pair_keys)):
        errors.append("duplicate_language_locale")
    # V1 cannot represent two locale variants of one base language without
    # silently merging them.  Reject until that compatibility contract changes.
    if len(base_keys) != len(set(base_keys)):
        errors.append("v1_language_projection_collision")
    if sort_keys != sorted(sort_keys):
        errors.append("languages_not_sorted")
def _validate_section(name, value, errors):
    if not _require_allowed_keys(value, _SECTION_FIELDS[name], f"invalid_{name}_fields", errors):
        return
    for field in _STRING_LIST_FIELDS.get(name, ()):
        if field in value:
            limit = 128 if name == "skills" else 64
            _validate_string_list(value[field], errors, limit=limit)
    if name == "education" and "graduation_years" in value:
        years = value["graduation_years"]
        if type(years) is not list or any(type(year) is not int or not 1900 <= year <= 2200 for year in years):
            errors.append("invalid_graduation_years")
    if name == "experience":
        total = value.get("total_years")
        if total is not None and (type(total) is not int or not 0 <= total <= 80):
            errors.append("invalid_total_years")
        _validate_domain_years(value.get("years_by_domain"), errors)
    if name == "skills":
        _validate_skill_entries(value.get("entries"), errors)
    if name == "derived_matcher_signals":
        _validate_signals(value.get("signals"), errors)


def _validate_string_list(
    value,
    errors,
    *,
    limit=64,
    item_length=MAX_DYNAMIC_LABEL_LENGTH,
):
    if type(value) is not list or len(value) > limit:
        errors.append("invalid_string_list")
        return
    keys = []
    for item in value:
        if not _valid_dynamic_label(item) or len(item) > item_length:
            errors.append("invalid_string_list")
            continue
        keys.append(normalize_comparison_label(item))
    if len(keys) != len(set(keys)):
        errors.append("duplicate_string_value")
    if keys != sorted(keys):
        errors.append("string_list_not_sorted")


def _validate_domain_years(value, errors):
    if type(value) is not list or len(value) > MAX_DOMAIN_YEARS:
        errors.append("invalid_domain_years")
        return
    keys = []
    sort_keys = []
    for item in value:
        _require_exact_keys(item, _DOMAIN_YEAR_FIELDS, "invalid_domain_year_item", errors)
        if type(item) is not dict:
            continue
        domain = item.get("domain")
        years = item.get("years")
        if not _valid_dynamic_label(domain):
            errors.append("invalid_domain_label")
            continue
        if type(years) not in {int, float} or not math.isfinite(years) or not 0 <= years <= 80:
            errors.append("invalid_domain_year_value")
        elif isinstance(years, float) and round(years, 2) != years:
            errors.append("invalid_domain_year_precision")
        key = normalize_comparison_label(domain)
        keys.append(key)
        sort_keys.append((key, domain))
    if len(keys) != len(set(keys)):
        errors.append("duplicate_domain")
    if sort_keys != sorted(sort_keys):
        errors.append("domain_years_not_sorted")


def _validate_skill_entries(value, errors):
    if type(value) is not list or len(value) > MAX_SKILL_ENTRIES:
        errors.append("invalid_skill_entries")
        return
    keys = []
    sort_keys = []
    for item in value:
        if not _require_allowed_keys(item, _SKILL_ENTRY_FIELDS, "invalid_skill_item", errors):
            continue
        if not {"skill", "confidence"} <= set(item):
            errors.append("invalid_skill_item")
            continue
        skill = item.get("skill")
        if not _valid_dynamic_label(skill):
            errors.append("invalid_skill_label")
            continue
        if item.get("confidence") not in CONFIDENCE_LEVELS:
            errors.append("invalid_confidence")
        if "provenance" in item and item["provenance"] not in PROFILE_SOURCES:
            errors.append("invalid_source_kind")
        key = normalize_comparison_label(skill)
        keys.append(key)
        sort_keys.append((key, skill))
    if len(keys) != len(set(keys)):
        errors.append("duplicate_skill")
    if sort_keys != sorted(sort_keys):
        errors.append("skills_not_sorted")


def _validate_signals(value, errors):
    if type(value) is not list or len(value) > MAX_SIGNALS:
        errors.append("invalid_signals")
        return
    sort_keys = []
    reason_keys = []
    for item in value:
        _require_exact_keys(item, _SIGNAL_FIELDS, "invalid_signal_item", errors)
        if type(item) is not dict:
            continue
        reason = item.get("reason")
        if (
            type(reason) is not str
            or len(reason) > MAX_SIGNAL_REASON_LENGTH
            or _SIGNAL_REASON_PATTERN.fullmatch(reason) is None
        ):
            errors.append("invalid_signal_reason")
            continue
        reason_keys.append(reason)
        keywords = item.get("keywords")
        _validate_string_list(
            keywords,
            errors,
            limit=MAX_SIGNAL_KEYWORDS,
            item_length=MAX_SIGNAL_KEYWORD_LENGTH,
        )
        points = item.get("points")
        if type(points) is not int or not 1 <= points <= MAX_SIGNAL_POINTS:
            errors.append("invalid_signal_points")
        if item.get("confidence") not in CONFIDENCE_LEVELS:
            errors.append("invalid_confidence")
        keywords = keywords if type(keywords) is list else []
        keyword_keys = [normalize_comparison_label(value) for value in keywords if type(value) is str]
        if keyword_keys != sorted(keyword_keys):
            errors.append("signal_keywords_not_sorted")
        sort_keys.append(_signal_sort_key(item))
    if len(reason_keys) != len(set(reason_keys)):
        errors.append("duplicate_signal_reason")
    if sort_keys != sorted(sort_keys):
        errors.append("signals_not_sorted")


def _validate_provenance(profile, errors):
    value = profile.get("provenance")
    _require_exact_keys(value, _PROVENANCE_FIELDS, "invalid_provenance_fields", errors)
    if type(value) is not dict:
        return
    if not _valid_dynamic_label(value.get("extracted_from")):
        errors.append("invalid_provenance")
    if value.get("confidence") not in CONFIDENCE_LEVELS:
        errors.append("invalid_confidence")
    _validate_string_list(value.get("missing_fields"), errors, limit=64)
    _validate_string_list(value.get("ambiguous_fields"), errors, limit=64)
    if type(value.get("reviewed")) is not bool:
        errors.append("invalid_provenance")
    field_sources = value.get("field_sources")
    if type(field_sources) is not list or len(field_sources) > MAX_FIELD_SOURCES:
        errors.append("invalid_field_sources")
        return
    paths = []
    for item in field_sources:
        _require_exact_keys(item, _FIELD_SOURCE_FIELDS, "invalid_field_source_item", errors)
        if type(item) is not dict:
            continue
        path = item.get("field_path")
        if item.get("path_version") != FIELD_PATH_VERSION:
            errors.append("invalid_field_path_version")
        if item.get("source_kind") not in PROFILE_SOURCES:
            errors.append("invalid_source_kind")
        if type(item.get("explicit")) is not bool:
            errors.append("invalid_field_source_item")
        try:
            ordinals = _validated_ordinals(item.get("source_ordinals"))
            if type(item.get("source_ordinals")) is not list or item["source_ordinals"] != ordinals:
                errors.append("invalid_source_ordinals")
            _resolve_field_path(profile, path)
        except CanonicalProfileV2Error as exc:
            errors.extend(exc.reason_codes)
        if type(path) is str:
            paths.append(path)
    normalized_paths = [path.casefold() for path in paths]
    if len(normalized_paths) != len(set(normalized_paths)):
        errors.append("duplicate_field_path")
    expected_order = sorted(paths, key=lambda path: (path.casefold(), path))
    if paths != expected_order:
        errors.append("field_sources_not_sorted")
    expected_paths = set(_material_field_paths(profile))
    if set(paths) != expected_paths:
        errors.append("field_source_coverage_mismatch")


def _material_field_paths(profile):
    result = []

    def visit(value, path):
        if type(value) is dict:
            for key, child in value.items():
                visit(child, f"{path}.{key}" if path else key)
            return
        if type(value) is list:
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")
            return
        if value not in (None, ""):
            result.append(path)

    for root in _PROVENANCE_ROOTS:
        visit(profile[root], root)
    return result


def _nfc(value: str) -> str:
    return _normalize_durable_string(value)


def _normalize_durable_string(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).strip().split())


def _signal_reason_identifier(value) -> str:
    if type(value) is not str:
        return ""
    normalized = _normalize_durable_string(value).casefold()
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")
    return normalized


def _signal_sort_key(item):
    if type(item) is not dict:
        return ("", (), 0, "")
    keywords = item.get("keywords")
    if type(keywords) is not list:
        keywords = []
    return (
        str(item.get("reason", "")),
        tuple(
            normalize_comparison_label(value)
            for value in keywords
            if type(value) is str
        ),
        item.get("points") if type(item.get("points")) is int else 0,
        str(item.get("confidence", "")),
    )


def _nfc_copy(value):
    if type(value) is str:
        return _nfc(value)
    if type(value) is list:
        return [_nfc_copy(item) for item in value]
    if type(value) is dict:
        return {key: _nfc_copy(item) for key, item in value.items()}
    return deepcopy(value)
