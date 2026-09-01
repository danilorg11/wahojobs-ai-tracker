"""Offline proposition-first semantic contract and compatibility projection.

This module is intentionally disconnected from opportunity extraction, persistence,
matching, and runtime presentation.  It accepts a closed proposition contract plus a
server-owned accepted-evidence catalog, validates both, and produces a sparse legacy
compatibility patch only where the logical meaning is preserved.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from functools import lru_cache


CONTRACT_VERSION = "oe_semantic_contract_v0"
FIXTURE_VERSION = "oe_semantic_contract_gold_v0"

MAX_ATOMS = 64
MAX_CONSTRAINT_GROUPS = 32
MAX_ALTERNATIVES_PER_GROUP = 8
MAX_ATOMS_PER_CONJUNCTION = 8
MAX_EVIDENCE_REFS_PER_ATOM = 4

SUBJECTS = frozenset({"candidate", "role"})
POLARITIES = frozenset({"affirmed", "negated"})
TEMPORALS = frozenset({"current", "prior", "by_start", "ongoing", "unspecified"})
MODALITIES = frozenset({"required", "preferred", "descriptive"})
TRUTH_VALUES = frozenset({"true", "false", "unknown"})

ATOM_KINDS = (
    "capability",
    "experience",
    "education",
    "professional_standing",
    "professional_status",
    "professional_credential",
    "regulatory_registration",
    "work_authorization",
    "asset_access",
    "language_proficiency",
    "locale_dialect_expertise",
    "domain_expertise",
    "interest_involvement",
    "role_activity",
)

CAPABILITIES = frozenset(
    {
        "analytical_communication",
        "fact_checking",
        "latex_typesetting",
        "python_programming",
        "quality_assurance",
        "software_testing",
    }
)
EXPERIENCE_AREAS = frozenset(
    {
        "ai_evaluation",
        "content_evaluation",
        "fact_checking",
        "latex_typesetting",
        "linguistics",
        "litigation",
        "retail_operations",
        "software_testing",
        "sports_industry",
        "technical_writing",
        "translation",
    }
)
EDUCATION_LEVELS = frozenset(
    {
        "no_degree",
        "secondary",
        "associate",
        "bachelor",
        "master",
        "doctorate",
        "advanced_degree",
    }
)
EDUCATION_FIELDS = frozenset(
    {
        "computer_science",
        "economics",
        "law",
        "mathematics",
        "related_field",
    }
)
PROFESSIONAL_STANDINGS = frozenset(
    {
        "industry_recognized",
        "published_author",
        "senior_principal",
        "award_winning",
    }
)
PROFESSIONAL_STATUSES = frozenset(
    {
        "owner",
        "operator",
        "primary_manager",
        "independent_consultant",
        "freelancer",
    }
)
PROFESSIONAL_SCOPES = frozenset({"business", "professional_practice"})
PROFESSIONAL_CREDENTIALS = frozenset(
    {"industry_certification", "professional_license"}
)
REGULATORY_REGISTRATIONS = frozenset({"contractor_registration"})
JURISDICTIONS = frozenset(
    {"candidate_jurisdiction", "job_jurisdiction", "united_states"}
)
WORK_AUTHORIZATIONS = frozenset({"independent_without_sponsorship"})
ASSETS = frozenset(
    {
        "digital_storefront",
        "google_business_profile",
        "high_speed_internet",
        "secure_computer",
    }
)
ASSET_RELATIONS = frozenset(
    {"access", "possess", "possess_and_manage", "supply"}
)
LANGUAGES = frozenset(
    {
        "chinese",
        "english",
        "french",
        "galician",
        "japanese",
        "korean",
        "spanish",
    }
)
LOCALES_BY_LANGUAGE = {
    "chinese": frozenset({"mandarin", "taiwan"}),
    "english": frozenset(),
    "french": frozenset(),
    "galician": frozenset(),
    "japanese": frozenset(),
    "korean": frozenset(),
    "spanish": frozenset({"cordobes", "latin_america", "spain"}),
}
PROFICIENCIES = frozenset({"fluent", "native", "professional"})
DIALECT_EXPERTISE_TYPES = frozenset({"dialect", "locale"})
DOMAINS = frozenset(
    {"finance", "insurance", "mathematics", "retail", "software", "sports"}
)
INVOLVEMENT_RELATIONS = frozenset({"active_involvement", "strong_interest"})
ROLE_ACTIVITIES = frozenset(
    {
        "ai_training_evaluation",
        "document_typesetting",
        "fact_checking",
        "quality_assurance",
        "software_testing",
    }
)
ROLE_ARTIFACTS = frozenset(
    {
        "ai_output",
        "digital_tool",
        "document",
        "generated_task",
        "platform_feature",
        "scoring_criteria",
        "software",
    }
)

TEMPORALS_BY_KIND = {
    "capability": frozenset({"current", "by_start", "ongoing", "unspecified"}),
    "experience": frozenset({"prior"}),
    "education": frozenset({"current", "by_start", "unspecified"}),
    "professional_standing": frozenset(
        {"current", "by_start", "ongoing", "unspecified"}
    ),
    "professional_status": frozenset(
        {"current", "by_start", "ongoing", "unspecified"}
    ),
    "professional_credential": frozenset(
        {"current", "by_start", "ongoing", "unspecified"}
    ),
    "regulatory_registration": frozenset(
        {"current", "by_start", "ongoing", "unspecified"}
    ),
    "work_authorization": frozenset(
        {"current", "by_start", "ongoing", "unspecified"}
    ),
    "asset_access": frozenset({"current", "by_start", "ongoing", "unspecified"}),
    "language_proficiency": frozenset(
        {"current", "by_start", "ongoing", "unspecified"}
    ),
    "locale_dialect_expertise": frozenset(
        {"current", "by_start", "ongoing", "unspecified"}
    ),
    "domain_expertise": frozenset(
        {"current", "by_start", "ongoing", "unspecified"}
    ),
    "interest_involvement": frozenset({"current", "ongoing", "unspecified"}),
    "role_activity": frozenset({"ongoing", "unspecified"}),
}

SUBJECT_BY_KIND = {
    **{kind: "candidate" for kind in ATOM_KINDS if kind != "role_activity"},
    "role_activity": "role",
}

CAPABILITY_LABELS = {
    "analytical_communication": "Analytical communication",
    "fact_checking": "Fact-checking",
    "latex_typesetting": "LaTeX/typesetting",
    "python_programming": "Python programming",
    "quality_assurance": "Quality assurance",
    "software_testing": "Software testing",
}
EXPERIENCE_LABELS = {
    "ai_evaluation": "AI evaluation experience",
    "content_evaluation": "Content evaluation experience",
    "fact_checking": "Fact-checking experience",
    "latex_typesetting": "LaTeX/typesetting experience",
    "linguistics": "Linguistics experience",
    "litigation": "Litigation experience",
    "retail_operations": "Retail operations experience",
    "software_testing": "Software testing experience",
    "sports_industry": "Sports-industry experience",
    "technical_writing": "Technical writing experience",
    "translation": "Translation experience",
}
STATUS_LABELS = {
    ("owner", "business"): "Current business owner",
    ("operator", "business"): "Current business operator",
    ("primary_manager", "business"): "Current primary business manager",
    (
        "independent_consultant",
        "professional_practice",
    ): "Current independent consultant in professional practice",
    ("freelancer", "professional_practice"): "Current freelancer in professional practice",
}
VALID_ASSET_PAIRS = frozenset(
    {
        ("digital_storefront", "access"),
        ("google_business_profile", "possess_and_manage"),
        ("high_speed_internet", "supply"),
        ("secure_computer", "supply"),
    }
)
DOMAIN_LABELS = {
    domain: f"{domain.replace('_', ' ').title()} expertise" for domain in DOMAINS
}
LOCALE_LABELS = {
    "cordobes": "Cordobes",
    "latin_america": "Latin America",
    "mandarin": "Mandarin",
    "spain": "Spain",
    "taiwan": "Taiwan",
}
HARD_LEGACY_FIELD_PATHS = frozenset(
    {
        "attributes.requirements.credentials",
        "attributes.requirements.current_status_requirements",
        "attributes.requirements.education.minimum_level",
        "attributes.requirements.experience_required",
        "attributes.requirements.languages",
        "attributes.requirements.licenses",
        "attributes.requirements.skills_required",
    }
)

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SOURCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9:._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SemanticContractValidationError(ValueError):
    """Raised when evidence or a semantic contract is outside the closed boundary."""


def _fail(path: str, message: str) -> None:
    raise SemanticContractValidationError(f"{path}: {message}")


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _expect_exact_keys(value, required, optional, path: str) -> None:
    if type(value) is not dict:
        _fail(path, "must be an object")
    keys = set(value)
    missing = set(required) - keys
    unexpected = keys - set(required) - set(optional)
    if missing:
        _fail(path, f"missing keys {sorted(missing)}")
    if unexpected:
        _fail(path, f"unexpected keys {sorted(unexpected)}")


def _expect_string(value, path: str, *, max_length: int = 256) -> str:
    if type(value) is not str or not value or len(value) > max_length:
        _fail(path, f"must be a non-empty string of at most {max_length} characters")
    if value != value.strip():
        _fail(path, "must not contain leading or trailing whitespace")
    return value


def _expect_enum(value, allowed, path: str) -> str:
    value = _expect_string(value, path)
    if value not in allowed:
        _fail(path, f"must be one of {sorted(allowed)}")
    return value


def _enum_schema(values) -> dict:
    return {"type": "string", "enum": sorted(values)}


def _payload_schema(kind: str) -> dict:
    specifications = {
        "capability": (
            {"capability": _enum_schema(CAPABILITIES)},
            ["capability"],
        ),
        "experience": (
            {
                "area": _enum_schema(EXPERIENCE_AREAS),
                "minimum_years": {"type": "integer", "minimum": 0, "maximum": 50},
            },
            ["area"],
        ),
        "education": (
            {
                "level": _enum_schema(EDUCATION_LEVELS),
                "field": _enum_schema(EDUCATION_FIELDS),
            },
            ["level"],
        ),
        "professional_standing": (
            {"standing": _enum_schema(PROFESSIONAL_STANDINGS)},
            ["standing"],
        ),
        "professional_status": (
            {
                "status": _enum_schema(PROFESSIONAL_STATUSES),
                "scope": _enum_schema(PROFESSIONAL_SCOPES),
            },
            ["status", "scope"],
        ),
        "professional_credential": (
            {
                "credential": _enum_schema(PROFESSIONAL_CREDENTIALS),
                "jurisdiction": _enum_schema(JURISDICTIONS),
            },
            ["credential"],
        ),
        "regulatory_registration": (
            {
                "registration": _enum_schema(REGULATORY_REGISTRATIONS),
                "jurisdiction": _enum_schema(JURISDICTIONS),
            },
            ["registration", "jurisdiction"],
        ),
        "work_authorization": (
            {
                "authorization": _enum_schema(WORK_AUTHORIZATIONS),
                "jurisdiction": _enum_schema(JURISDICTIONS),
            },
            ["authorization", "jurisdiction"],
        ),
        "asset_access": (
            {
                "asset": _enum_schema(ASSETS),
                "relation": _enum_schema(ASSET_RELATIONS),
            },
            ["asset", "relation"],
        ),
        "language_proficiency": (
            {
                "language": _enum_schema(LANGUAGES),
                "locale": {"type": ["string", "null"]},
                "proficiency": _enum_schema(PROFICIENCIES),
            },
            ["language", "locale", "proficiency"],
        ),
        "locale_dialect_expertise": (
            {
                "language": _enum_schema(LANGUAGES),
                "locale": _enum_schema(set().union(*LOCALES_BY_LANGUAGE.values())),
                "expertise": _enum_schema(DIALECT_EXPERTISE_TYPES),
            },
            ["language", "locale", "expertise"],
        ),
        "domain_expertise": (
            {"domain": _enum_schema(DOMAINS)},
            ["domain"],
        ),
        "interest_involvement": (
            {
                "domain": _enum_schema(DOMAINS),
                "relation": _enum_schema(INVOLVEMENT_RELATIONS),
            },
            ["domain", "relation"],
        ),
        "role_activity": (
            {
                "activity": _enum_schema(ROLE_ACTIVITIES),
                "artifact": _enum_schema(ROLE_ARTIFACTS),
            },
            ["activity", "artifact"],
        ),
    }
    properties, required = specifications[kind]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def semantic_contract_schema() -> dict:
    """Return the closed JSON-schema description of the model-facing contract."""

    evidence_ref = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source_id": {"type": "string", "pattern": _SOURCE_ID_RE.pattern},
            "start": {"type": "integer", "minimum": 0},
            "end": {"type": "integer", "minimum": 1},
            "quote": {"type": "string", "minLength": 1, "maxLength": 800},
        },
        "required": ["source_id", "start", "end", "quote"],
    }
    atom_variants = []
    for kind in ATOM_KINDS:
        atom_variants.append(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "pattern": _ID_RE.pattern},
                    "subject": {"const": SUBJECT_BY_KIND[kind]},
                    "kind": {"const": kind},
                    "typed_payload": _payload_schema(kind),
                    "polarity": _enum_schema(POLARITIES),
                    "temporal": _enum_schema(TEMPORALS_BY_KIND[kind]),
                    "normalized_value": {"type": "string"},
                    "evidence": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_EVIDENCE_REFS_PER_ATOM,
                        "items": evidence_ref,
                    },
                },
                "required": [
                    "id",
                    "subject",
                    "kind",
                    "typed_payload",
                    "polarity",
                    "temporal",
                    "evidence",
                ],
            }
        )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": CONTRACT_VERSION,
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "contract_version": {"const": CONTRACT_VERSION},
            "atoms": {
                "type": "array",
                "maxItems": MAX_ATOMS,
                "items": {"oneOf": atom_variants},
            },
            "constraint_groups": {
                "type": "array",
                "maxItems": MAX_CONSTRAINT_GROUPS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "modality": _enum_schema(MODALITIES),
                        "any_of": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": MAX_ALTERNATIVES_PER_GROUP,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "all_of": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": MAX_ATOMS_PER_CONJUNCTION,
                                        "items": {
                                            "type": "string",
                                            "pattern": _ID_RE.pattern,
                                        },
                                    }
                                },
                                "required": ["all_of"],
                            },
                        },
                    },
                    "required": ["modality", "any_of"],
                },
            },
        },
        "required": ["contract_version", "atoms", "constraint_groups"],
    }


def validate_accepted_evidence_catalog(catalog) -> dict[str, dict]:
    """Validate and index server-owned accepted evidence sources."""

    if type(catalog) is not list:
        _fail("evidence_catalog", "must be a list")
    if len(catalog) > MAX_ATOMS * MAX_EVIDENCE_REFS_PER_ATOM:
        _fail("evidence_catalog", "contains too many sources")
    indexed = {}
    for index, raw_source in enumerate(catalog):
        path = f"evidence_catalog[{index}]"
        _expect_exact_keys(
            raw_source,
            {"id", "authority", "text", "text_sha256", "provenance"},
            set(),
            path,
        )
        source_id = _expect_string(raw_source["id"], f"{path}.id", max_length=128)
        if not _SOURCE_ID_RE.fullmatch(source_id):
            _fail(f"{path}.id", "has an invalid source identifier")
        if source_id in indexed:
            _fail(f"{path}.id", "must be unique")
        authority = _expect_enum(
            raw_source["authority"],
            {"accepted_capture", "accepted_review_checkpoint"},
            f"{path}.authority",
        )
        text = _expect_string(raw_source["text"], f"{path}.text", max_length=2000)
        text_sha256 = _expect_string(
            raw_source["text_sha256"], f"{path}.text_sha256", max_length=64
        )
        if not _SHA256_RE.fullmatch(text_sha256):
            _fail(f"{path}.text_sha256", "must be a lowercase SHA-256 digest")
        if text_sha256 != _sha256_text(text):
            _fail(f"{path}.text_sha256", "does not authenticate the evidence text")

        provenance = raw_source["provenance"]
        if authority == "accepted_capture":
            _expect_exact_keys(
                provenance,
                {
                    "accepted_capture_id",
                    "job_id",
                    "material_content_sha256",
                    "source_url",
                },
                set(),
                f"{path}.provenance",
            )
            for key in ("accepted_capture_id", "job_id"):
                if type(provenance[key]) is not int or provenance[key] <= 0:
                    _fail(f"{path}.provenance.{key}", "must be a positive integer")
            material_hash = _expect_string(
                provenance["material_content_sha256"],
                f"{path}.provenance.material_content_sha256",
                max_length=64,
            )
            if not _SHA256_RE.fullmatch(material_hash):
                _fail(
                    f"{path}.provenance.material_content_sha256",
                    "must be a lowercase SHA-256 digest",
                )
            source_url = _expect_string(
                provenance["source_url"],
                f"{path}.provenance.source_url",
                max_length=1000,
            )
            if not source_url.startswith("https://"):
                _fail(f"{path}.provenance.source_url", "must be an HTTPS URL")
        else:
            _expect_exact_keys(
                provenance,
                {"review_id", "case_id"},
                set(),
                f"{path}.provenance",
            )
            _expect_string(
                provenance["review_id"],
                f"{path}.provenance.review_id",
                max_length=128,
            )
            _expect_string(
                provenance["case_id"],
                f"{path}.provenance.case_id",
                max_length=128,
            )
        indexed[source_id] = copy.deepcopy(raw_source)
    return indexed


def _validate_payload(
    kind: str,
    payload,
    path: str,
    *,
    allow_role_activity_composition: bool = False,
) -> dict:
    if kind == "capability":
        _expect_exact_keys(payload, {"capability"}, set(), path)
        _expect_enum(payload["capability"], CAPABILITIES, f"{path}.capability")
    elif kind == "experience":
        _expect_exact_keys(payload, {"area"}, {"minimum_years"}, path)
        _expect_enum(payload["area"], EXPERIENCE_AREAS, f"{path}.area")
        if "minimum_years" in payload:
            years = payload["minimum_years"]
            if type(years) is not int or not 0 <= years <= 50:
                _fail(f"{path}.minimum_years", "must be an integer from 0 through 50")
    elif kind == "education":
        _expect_exact_keys(payload, {"level"}, {"field"}, path)
        _expect_enum(payload["level"], EDUCATION_LEVELS, f"{path}.level")
        if "field" in payload:
            _expect_enum(payload["field"], EDUCATION_FIELDS, f"{path}.field")
    elif kind == "professional_standing":
        _expect_exact_keys(payload, {"standing"}, set(), path)
        _expect_enum(
            payload["standing"], PROFESSIONAL_STANDINGS, f"{path}.standing"
        )
    elif kind == "professional_status":
        _expect_exact_keys(payload, {"status", "scope"}, set(), path)
        _expect_enum(payload["status"], PROFESSIONAL_STATUSES, f"{path}.status")
        _expect_enum(payload["scope"], PROFESSIONAL_SCOPES, f"{path}.scope")
        if (payload["status"], payload["scope"]) not in STATUS_LABELS:
            _fail(path, "status and scope are not a supported typed pair")
    elif kind == "professional_credential":
        _expect_exact_keys(payload, {"credential"}, {"jurisdiction"}, path)
        _expect_enum(
            payload["credential"],
            PROFESSIONAL_CREDENTIALS,
            f"{path}.credential",
        )
        if "jurisdiction" in payload:
            _expect_enum(payload["jurisdiction"], JURISDICTIONS, f"{path}.jurisdiction")
    elif kind == "regulatory_registration":
        _expect_exact_keys(payload, {"registration", "jurisdiction"}, set(), path)
        _expect_enum(
            payload["registration"],
            REGULATORY_REGISTRATIONS,
            f"{path}.registration",
        )
        _expect_enum(payload["jurisdiction"], JURISDICTIONS, f"{path}.jurisdiction")
    elif kind == "work_authorization":
        _expect_exact_keys(payload, {"authorization", "jurisdiction"}, set(), path)
        _expect_enum(
            payload["authorization"], WORK_AUTHORIZATIONS, f"{path}.authorization"
        )
        _expect_enum(payload["jurisdiction"], JURISDICTIONS, f"{path}.jurisdiction")
    elif kind == "asset_access":
        _expect_exact_keys(payload, {"asset", "relation"}, set(), path)
        _expect_enum(payload["asset"], ASSETS, f"{path}.asset")
        _expect_enum(payload["relation"], ASSET_RELATIONS, f"{path}.relation")
        if (payload["asset"], payload["relation"]) not in VALID_ASSET_PAIRS:
            _fail(path, "asset and relation are not a supported typed pair")
    elif kind == "language_proficiency":
        _expect_exact_keys(
            payload, {"language", "locale", "proficiency"}, set(), path
        )
        language = _expect_enum(payload["language"], LANGUAGES, f"{path}.language")
        _expect_enum(payload["proficiency"], PROFICIENCIES, f"{path}.proficiency")
        locale = payload["locale"]
        if locale is not None:
            _expect_string(locale, f"{path}.locale", max_length=64)
            if locale not in LOCALES_BY_LANGUAGE[language]:
                _fail(f"{path}.locale", "is not valid for the selected language")
    elif kind == "locale_dialect_expertise":
        _expect_exact_keys(
            payload, {"language", "locale", "expertise"}, set(), path
        )
        language = _expect_enum(payload["language"], LANGUAGES, f"{path}.language")
        locale = _expect_string(payload["locale"], f"{path}.locale", max_length=64)
        if locale not in LOCALES_BY_LANGUAGE[language]:
            _fail(f"{path}.locale", "is not valid for the selected language")
        _expect_enum(
            payload["expertise"], DIALECT_EXPERTISE_TYPES, f"{path}.expertise"
        )
    elif kind == "domain_expertise":
        _expect_exact_keys(payload, {"domain"}, set(), path)
        _expect_enum(payload["domain"], DOMAINS, f"{path}.domain")
    elif kind == "interest_involvement":
        _expect_exact_keys(payload, {"domain", "relation"}, set(), path)
        _expect_enum(payload["domain"], DOMAINS, f"{path}.domain")
        _expect_enum(
            payload["relation"], INVOLVEMENT_RELATIONS, f"{path}.relation"
        )
    elif kind == "role_activity":
        _expect_exact_keys(payload, {"activity", "artifact"}, set(), path)
        activity = _expect_enum(
            payload["activity"], ROLE_ACTIVITIES, f"{path}.activity"
        )
        artifact = _expect_enum(
            payload["artifact"], ROLE_ARTIFACTS, f"{path}.artifact"
        )
        if not allow_role_activity_composition:
            valid_artifacts = {
                "ai_training_evaluation": {"ai_output"},
                "document_typesetting": {"document"},
                "fact_checking": {"generated_task", "scoring_criteria"},
                "quality_assurance": {"generated_task", "scoring_criteria"},
                "software_testing": {
                    "digital_tool",
                    "platform_feature",
                    "software",
                },
            }
            if artifact not in valid_artifacts[activity]:
                _fail(path, "activity and artifact are not a supported typed pair")
    else:
        _fail(path, "has an unsupported atom kind")
    return copy.deepcopy(payload)


def _evidence_supports_atom(atom: dict, evidence_text: str, path: str) -> None:
    text = _normalized_text(evidence_text)

    def require(pattern: str, description: str) -> None:
        if re.search(pattern, text, re.IGNORECASE) is None:
            _fail(path, f"evidence does not support {description}")

    def require_jurisdiction(jurisdiction: str) -> None:
        witnesses = {
            "candidate_jurisdiction": (
                r"\b(?:candidate(?:'s)? jurisdiction|country of residence|"
                r"their jurisdiction|your jurisdiction)\b"
            ),
            "job_jurisdiction": r"\b(?:job|engagement|work) jurisdiction\b",
            "united_states": r"\b(?:united states|u\.s\.|us jurisdiction)\b",
        }
        require(witnesses[jurisdiction], jurisdiction)

    polarity = atom["polarity"]
    direct_negation = re.search(
        r"\b(?:must\s+not|cannot|can't|prohibited|not\s+permitted)\b", text
    )
    if polarity == "negated" and direct_negation is None:
        _fail(path, "negated polarity lacks direct negative evidence")
    if polarity == "affirmed" and direct_negation is not None:
        _fail(path, "affirmed polarity conflicts with direct negative evidence")

    payload = atom["typed_payload"]
    kind = atom["kind"]
    if kind == "capability":
        witnesses = {
            "analytical_communication": r"\banalytical\b.*\bcommunication\b",
            "fact_checking": r"\b(?:fact[ -]?check|factual accuracy)\w*\b",
            "latex_typesetting": r"\blatex\b.*\b(?:typesett|format|package|template)",
            "python_programming": r"\bpython\b.*\b(?:program|cod|capabilit|skill)",
            "quality_assurance": r"\bquality assurance\b",
            "software_testing": r"\b(?:software test|test\w*)\b.*\b(?:software|platform|feature|application|digital tool)",
        }
        require(witnesses[payload["capability"]], payload["capability"])
    elif kind == "experience":
        require(
            r"\b(?:background|experience|experienced|history|prior|previous|years?)\b",
            "historical experience",
        )
        witnesses = {
            "ai_evaluation": r"\bai evaluation\b",
            "content_evaluation": r"\bcontent evaluation\b",
            "fact_checking": r"\bfact[ -]?check\w*\b",
            "latex_typesetting": r"\b(?:latex|typesett)\w*\b",
            "linguistics": r"\blinguistic\w*\b",
            "litigation": r"\blitigation\b",
            "retail_operations": r"\b(?:retail|operat\w* stores?)\b",
            "software_testing": r"\b(?:software test|testing software)\b",
            "sports_industry": r"\bsports?\b",
            "technical_writing": r"\btechnical writing\b",
            "translation": r"\btranslat\w*\b",
        }
        require(witnesses[payload["area"]], payload["area"])
        if "minimum_years" in payload:
            require(
                rf"\b{payload['minimum_years']}\+?\s+years?\b",
                "the stated minimum years",
            )
    elif kind == "education":
        witnesses = {
            "no_degree": r"\bno degree\b",
            "secondary": r"\b(?:secondary|high school)\b",
            "associate": r"\bassociate(?:'s)?\b",
            "bachelor": r"\bbachelor(?:'s)?\b",
            "master": r"\bmaster(?:'s)?\b",
            "doctorate": r"\b(?:doctorate|phd|ph\.d\.)\b",
            "advanced_degree": r"\badvanced degrees?\b",
        }
        require(witnesses[payload["level"]], payload["level"])
    elif kind == "professional_standing":
        witnesses = {
            "industry_recognized": r"\bindustry[ -]recognized status\b",
            "published_author": r"\bpublished author\b",
            "senior_principal": r"\bsenior principal\b",
            "award_winning": r"\baward[ -]winning\b",
        }
        require(witnesses[payload["standing"]], payload["standing"])
    elif kind == "professional_status":
        witnesses = {
            "owner": r"\bowners?\b",
            "operator": r"\boperators?\b",
            "primary_manager": r"\bprimary manager\b",
            "independent_consultant": r"\bindependent consultant\b",
            "freelancer": r"\bfreelancers?\b",
        }
        require(witnesses[payload["status"]], payload["status"])
        scope_witnesses = {
            "business": r"\b(?:business|enterprise|store)\b",
            "professional_practice": (
                r"\b(?:professional (?:practice|methodology)|independent consultant|freelancer)\b"
            ),
        }
        require(scope_witnesses[payload["scope"]], payload["scope"])
    elif kind == "professional_credential":
        witnesses = {
            "industry_certification": r"\bcertif(?:ication|ied)\b",
            "professional_license": r"\b(?:license|licence|licensure)\b",
        }
        require(witnesses[payload["credential"]], payload["credential"])
        if "jurisdiction" in payload:
            require_jurisdiction(payload["jurisdiction"])
    elif kind == "regulatory_registration":
        require(
            r"\bregistered\b.*\bindependent contractors?\b",
            payload["registration"],
        )
        require_jurisdiction(payload["jurisdiction"])
    elif kind == "work_authorization":
        require(r"\bindependently authorized\b", "independent work authorization")
        require(r"\b(?:jurisdiction|sponsor\w*)\b", "authorization jurisdiction")
        require_jurisdiction(payload["jurisdiction"])
    elif kind == "asset_access":
        witnesses = {
            "digital_storefront": r"\bdigital storefront\b",
            "google_business_profile": r"\bgoogle business profile\b",
            "high_speed_internet": r"\bhigh[ -]?speed internet\b",
            "secure_computer": r"\bsecure computer\b",
        }
        relation_witnesses = {
            "access": r"\baccess\b",
            "possess": r"\bpossess\b",
            "possess_and_manage": r"\bpossess\b.*\bmanage\b",
            "supply": r"\bsupply\b",
        }
        require(witnesses[payload["asset"]], payload["asset"])
        require(relation_witnesses[payload["relation"]], payload["relation"])
    elif kind == "language_proficiency":
        language_witnesses = {
            "chinese": r"\bchinese\b",
            "english": r"\benglish\b",
            "french": r"\bfrench\b",
            "galician": r"\bgalician\b",
            "japanese": r"\bjapanese\b",
            "korean": r"\bkorean\b",
            "spanish": r"\bspanish\b",
        }
        require(language_witnesses[payload["language"]], payload["language"])
        proficiency_witnesses = {
            "fluent": r"\bfluen\w*\b",
            "native": r"\bnative\b",
            "professional": (
                r"\b(?:professional(?: working)? proficiency|professionally proficient)\b"
            ),
        }
        require(
            proficiency_witnesses[payload["proficiency"]],
            payload["proficiency"],
        )
        if payload["locale"] is not None:
            require(
                rf"\b{re.escape(LOCALE_LABELS[payload['locale']].casefold())}\b",
                payload["locale"],
            )
    elif kind == "locale_dialect_expertise":
        require(
            rf"\b{re.escape(LOCALE_LABELS[payload['locale']].casefold())}\b",
            payload["locale"],
        )
        require(rf"\b{re.escape(payload['expertise'])}\b", payload["expertise"])
        require(r"\bexpert\w*\b", "dialect expertise")
    elif kind == "domain_expertise":
        require(rf"\b{re.escape(payload['domain'])}\b", payload["domain"])
        require(r"\b(?:deep knowledge|expert\w*|specialist)\b", "domain expertise")
    elif kind == "interest_involvement":
        require(rf"\b{re.escape(payload['domain'])}\b", payload["domain"])
        if payload["relation"] == "strong_interest":
            require(r"\bstrong interest\b", "strong interest")
        else:
            require(r"\b(?:demonstrated involvement|active involvement)\b", "involvement")
    elif kind == "role_activity":
        activity_witnesses = {
            "ai_training_evaluation": r"\b(?:evaluat|review|assess)\w*\b.*\b(?:ai|model outputs?)\b",
            "document_typesetting": r"\b(?:typesett|document preparation)\w*\b",
            "fact_checking": r"\b(?:fact[ -]?check|factual accuracy)\w*\b",
            "quality_assurance": r"\bquality assurance\b",
            "software_testing": r"\btest\w*\b.*\b(?:software|platform features?|digital (?:storefront )?tools?)\b",
        }
        artifact_witnesses = {
            "ai_output": r"\b(?:ai|model outputs?)\b",
            "digital_tool": r"\bdigital (?:storefront )?tools?\b",
            "document": r"\b(?:document|typesett)\w*\b",
            "generated_task": r"\bgenerated tasks?\b",
            "platform_feature": r"\bplatform features?\b",
            "scoring_criteria": r"\bscoring criteria\b",
            "software": r"\bsoftware\b",
        }
        require(activity_witnesses[payload["activity"]], payload["activity"])
        require(artifact_witnesses[payload["artifact"]], payload["artifact"])


def _evidence_supports_temporal(atom: dict, evidence_text: str, path: str) -> None:
    """Require the selected temporal meaning to be explicit in one evidence span."""

    text = _normalized_text(evidence_text)
    temporal = atom["temporal"]
    current = re.search(
        r"\b(?:active|already|current|currently|presently)\b|"
        r"\bare independently authorized\b",
        text,
    ) is not None
    by_start = re.search(
        r"\b(?:by|before|prior to) (?:the )?(?:engagement |project |work )?start\b|"
        r"\bbefore (?:beginning|commencement)\b",
        text,
    ) is not None
    ongoing = re.search(
        r"\b(?:ongoing|throughout (?:the )?(?:engagement|project|assignment)|"
        r"for the (?:entire )?duration|remain(?:s|ing)?|maintain(?:ed|s|ing)?)\b",
        text,
    ) is not None
    explicit_prior = re.search(
        r"\b(?:formerly|historical|previous|previously|prior)\b", text
    ) is not None
    prior = (
        atom["kind"] == "experience"
        and re.search(
            r"\b(?:background|experience|experienced|history|prior|previous|years?)\b",
            text,
        )
        is not None
    )
    supported = {
        "current": current,
        "prior": prior,
        "by_start": by_start,
        "ongoing": ongoing,
        "unspecified": not (current or explicit_prior or by_start or ongoing),
    }
    if not supported[temporal]:
        _fail(path, f"evidence does not support temporal meaning {temporal!r}")


def _candidate_modality(evidence_text: str, path: str) -> str:
    text = _normalized_text(evidence_text)
    preference_text = re.sub(
        r"\b(?:no|not)\b.{0,24}\b(?:preferred|preference)\b", "", text
    )
    preferred = re.search(
        r"\b(?:bonus|ideal|preferred|strong plus|signal(?:s)? fit|nice to have|beneficial)\b",
        preference_text,
    ) is not None
    requirement_text = re.sub(
        r"\b(?:no|not)\b.{0,32}\b(?:expected|required|requirement)\b", "", text
    )
    required = re.search(
        r"\b(?:core skillset includes|essential|expected|is required|are required|"
        r"looking for|must|need(?:s|ed)?|professional autonomy|seeking|"
        r"should have|confirm they are)\b",
        requirement_text,
    ) is not None
    if preferred and required:
        _fail(path, "candidate evidence has mixed required/preferred modality")
    if preferred:
        return "preferred"
    if required:
        return "required"
    _fail(path, "candidate evidence has no authoritative required/preferred modality")


def _normalized_value(kind: str, payload: dict) -> str:
    return f"{kind}:{_canonical_json(payload)}"


def _contract_atom_hash_payload(atom: dict) -> dict:
    return {
        key: copy.deepcopy(atom[key])
        for key in (
            "id",
            "subject",
            "kind",
            "typed_payload",
            "polarity",
            "temporal",
            "evidence",
        )
    }


def contract_atom_sha256(atom: dict) -> str:
    """Return the immutable identity used by the offline verification boundary."""

    if type(atom) is not dict:
        _fail("atom", "must be an object")
    required = {
        "id",
        "subject",
        "kind",
        "typed_payload",
        "polarity",
        "temporal",
        "evidence",
    }
    if not required.issubset(atom):
        _fail("atom", f"missing identity keys {sorted(required - set(atom))}")
    return _sha256_text(_canonical_json(_contract_atom_hash_payload(atom)))


def _validate_and_normalize_contract(
    contract,
    accepted_evidence_catalog,
    *,
    verified_atom_assurances: dict | None = None,
    authoritative_modalities: dict | None = None,
    allow_role_activity_composition: bool = False,
) -> dict:
    """Shared closed validation for lexical and explicitly verified assurance."""

    verified_mode = verified_atom_assurances is not None
    if verified_mode != (authoritative_modalities is not None):
        _fail(
            "verification",
            "atom assurances and authoritative modalities must be supplied together",
        )
    if verified_mode:
        if type(verified_atom_assurances) is not dict:
            _fail("verified_atom_assurances", "must be an object")
        if type(authoritative_modalities) is not dict:
            _fail("authoritative_modalities", "must be an object")

    evidence_sources = validate_accepted_evidence_catalog(accepted_evidence_catalog)
    _expect_exact_keys(
        contract,
        {"contract_version", "atoms", "constraint_groups"},
        set(),
        "contract",
    )
    if contract["contract_version"] != CONTRACT_VERSION:
        _fail("contract.contract_version", f"must equal {CONTRACT_VERSION!r}")
    if type(contract["atoms"]) is not list:
        _fail("contract.atoms", "must be a list")
    if len(contract["atoms"]) > MAX_ATOMS:
        _fail("contract.atoms", f"must contain at most {MAX_ATOMS} atoms")
    if type(contract["constraint_groups"]) is not list:
        _fail("contract.constraint_groups", "must be a list")
    if len(contract["constraint_groups"]) > MAX_CONSTRAINT_GROUPS:
        _fail(
            "contract.constraint_groups",
            f"must contain at most {MAX_CONSTRAINT_GROUPS} groups",
        )

    normalized_atoms = []
    atoms_by_id = {}
    authoritative_modality_by_atom = {}
    for index, raw_atom in enumerate(contract["atoms"]):
        path = f"contract.atoms[{index}]"
        _expect_exact_keys(
            raw_atom,
            {"id", "subject", "kind", "typed_payload", "polarity", "temporal", "evidence"},
            {"normalized_value"},
            path,
        )
        atom_id = _expect_string(raw_atom["id"], f"{path}.id", max_length=64)
        if not _ID_RE.fullmatch(atom_id):
            _fail(f"{path}.id", "has an invalid atom identifier")
        if atom_id in atoms_by_id:
            _fail(f"{path}.id", "must be unique")
        kind = _expect_enum(raw_atom["kind"], ATOM_KINDS, f"{path}.kind")
        subject = _expect_enum(raw_atom["subject"], SUBJECTS, f"{path}.subject")
        if subject != SUBJECT_BY_KIND[kind]:
            _fail(f"{path}.subject", f"must be {SUBJECT_BY_KIND[kind]!r} for {kind}")
        polarity = _expect_enum(
            raw_atom["polarity"], POLARITIES, f"{path}.polarity"
        )
        temporal = _expect_enum(raw_atom["temporal"], TEMPORALS, f"{path}.temporal")
        if temporal not in TEMPORALS_BY_KIND[kind]:
            _fail(
                f"{path}.temporal",
                f"must be one of {sorted(TEMPORALS_BY_KIND[kind])} for {kind}",
            )
        payload = _validate_payload(
            kind,
            raw_atom["typed_payload"],
            f"{path}.typed_payload",
            allow_role_activity_composition=allow_role_activity_composition,
        )
        computed_normalized = _normalized_value(kind, payload)
        if "normalized_value" in raw_atom:
            supplied = _expect_string(
                raw_atom["normalized_value"],
                f"{path}.normalized_value",
                max_length=1000,
            )
            if supplied != computed_normalized:
                _fail(
                    f"{path}.normalized_value",
                    "does not match the server-computed normalized value",
                )
        evidence = raw_atom["evidence"]
        if type(evidence) is not list or not evidence:
            _fail(f"{path}.evidence", "must be a non-empty list")
        if len(evidence) > MAX_EVIDENCE_REFS_PER_ATOM:
            _fail(
                f"{path}.evidence",
                f"must contain at most {MAX_EVIDENCE_REFS_PER_ATOM} references",
            )
        normalized_evidence = []
        seen_refs = set()
        for evidence_index, raw_ref in enumerate(evidence):
            ref_path = f"{path}.evidence[{evidence_index}]"
            _expect_exact_keys(
                raw_ref,
                {"source_id", "start", "end", "quote"},
                set(),
                ref_path,
            )
            source_id = _expect_string(
                raw_ref["source_id"], f"{ref_path}.source_id", max_length=128
            )
            if source_id not in evidence_sources:
                _fail(f"{ref_path}.source_id", "does not name accepted evidence")
            start = raw_ref["start"]
            end = raw_ref["end"]
            if type(start) is not int or type(end) is not int or start < 0 or end <= start:
                _fail(ref_path, "start/end must describe a non-empty forward span")
            quote = _expect_string(raw_ref["quote"], f"{ref_path}.quote", max_length=800)
            source_text = evidence_sources[source_id]["text"]
            if end > len(source_text) or source_text[start:end] != quote:
                _fail(ref_path, "quote is not the exact accepted-evidence span")
            identity = (source_id, start, end)
            if identity in seen_refs:
                _fail(ref_path, "duplicates another evidence reference on this atom")
            seen_refs.add(identity)
            normalized_evidence.append(
                {"source_id": source_id, "start": start, "end": end, "quote": quote}
            )
        normalized_evidence.sort(key=lambda item: (item["source_id"], item["start"], item["end"]))
        normalized_atom = {
            "id": atom_id,
            "subject": subject,
            "kind": kind,
            "typed_payload": payload,
            "polarity": polarity,
            "temporal": temporal,
            "normalized_value": computed_normalized,
            "evidence": normalized_evidence,
        }
        if verified_mode:
            assurance = verified_atom_assurances.get(atom_id)
            assurance_path = f"verified_atom_assurances[{atom_id!r}]"
            _expect_exact_keys(
                assurance,
                {
                    "atom_sha256",
                    "semantic_decision",
                    "qualifier_assurance",
                    "projection_assurance",
                },
                set(),
                assurance_path,
            )
            atom_hash = _expect_string(
                assurance["atom_sha256"],
                f"{assurance_path}.atom_sha256",
                max_length=64,
            )
            if not _SHA256_RE.fullmatch(atom_hash):
                _fail(f"{assurance_path}.atom_sha256", "must be a SHA-256 digest")
            if atom_hash != contract_atom_sha256(normalized_atom):
                _fail(
                    f"{assurance_path}.atom_sha256",
                    "does not match the immutable contract atom",
                )
            if assurance["semantic_decision"] != "entails":
                _fail(
                    f"{assurance_path}.semantic_decision",
                    "must be 'entails' for final contract admission",
                )
            if assurance["qualifier_assurance"] != "complete":
                _fail(
                    f"{assurance_path}.qualifier_assurance",
                    "must be 'complete' for final contract admission",
                )
            if assurance["projection_assurance"] != "hard_projection_authorized":
                _fail(
                    f"{assurance_path}.projection_assurance",
                    "must explicitly authorize deterministic hard projection",
                )
            authoritative_modality_by_atom[atom_id] = _expect_enum(
                authoritative_modalities.get(atom_id),
                MODALITIES,
                f"authoritative_modalities[{atom_id!r}]",
            )
        else:
            complete_span_modalities = set()
            for evidence_index, item in enumerate(normalized_evidence):
                support_path = f"{path}.evidence[{evidence_index}]"
                try:
                    _evidence_supports_atom(
                        normalized_atom, item["quote"], support_path
                    )
                    _evidence_supports_temporal(
                        normalized_atom, item["quote"], support_path
                    )
                    modality = (
                        "descriptive"
                        if kind == "role_activity"
                        else _candidate_modality(item["quote"], support_path)
                    )
                except SemanticContractValidationError:
                    continue
                complete_span_modalities.add(modality)
            if not complete_span_modalities:
                _fail(
                    path,
                    "no single authenticated evidence span supports the proposition, "
                    "polarity, temporal meaning, and modality",
                )
            if len(complete_span_modalities) != 1:
                _fail(path, "complete evidence spans disagree on modality")
            authoritative_modality_by_atom[atom_id] = next(
                iter(complete_span_modalities)
            )
        normalized_atoms.append(normalized_atom)
        atoms_by_id[atom_id] = normalized_atom

    if verified_mode:
        atom_ids = set(atoms_by_id)
        if set(verified_atom_assurances) != atom_ids:
            _fail(
                "verified_atom_assurances",
                "must account for every and only final contract atom",
            )
        if set(authoritative_modalities) != atom_ids:
            _fail(
                "authoritative_modalities",
                "must account for every and only final contract atom",
            )

    normalized_groups = []
    referenced_atoms = set()
    for group_index, raw_group in enumerate(contract["constraint_groups"]):
        path = f"contract.constraint_groups[{group_index}]"
        _expect_exact_keys(raw_group, {"modality", "any_of"}, set(), path)
        modality = _expect_enum(raw_group["modality"], MODALITIES, f"{path}.modality")
        alternatives = raw_group["any_of"]
        if type(alternatives) is not list or not alternatives:
            _fail(f"{path}.any_of", "must be a non-empty list")
        if len(alternatives) > MAX_ALTERNATIVES_PER_GROUP:
            _fail(
                f"{path}.any_of",
                f"must contain at most {MAX_ALTERNATIVES_PER_GROUP} alternatives",
            )
        normalized_alternatives = []
        seen_alternatives = set()
        group_atom_ids = []
        for alternative_index, raw_alternative in enumerate(alternatives):
            alternative_path = f"{path}.any_of[{alternative_index}]"
            _expect_exact_keys(raw_alternative, {"all_of"}, set(), alternative_path)
            conjunction = raw_alternative["all_of"]
            if type(conjunction) is not list or not conjunction:
                _fail(f"{alternative_path}.all_of", "must be a non-empty list")
            if len(conjunction) > MAX_ATOMS_PER_CONJUNCTION:
                _fail(
                    f"{alternative_path}.all_of",
                    f"must contain at most {MAX_ATOMS_PER_CONJUNCTION} atoms",
                )
            normalized_conjunction = []
            for atom_index, atom_id in enumerate(conjunction):
                atom_path = f"{alternative_path}.all_of[{atom_index}]"
                atom_id = _expect_string(atom_id, atom_path, max_length=64)
                if atom_id not in atoms_by_id:
                    _fail(atom_path, "references an unknown atom")
                if atom_id in normalized_conjunction:
                    _fail(atom_path, "duplicates an atom in the same conjunction")
                if atom_id in referenced_atoms:
                    _fail(atom_path, "an atom may belong to exactly one constraint group")
                normalized_conjunction.append(atom_id)
                referenced_atoms.add(atom_id)
                group_atom_ids.append(atom_id)
            normalized_conjunction.sort()
            identity = tuple(normalized_conjunction)
            if identity in seen_alternatives:
                _fail(alternative_path, "duplicates another DNF alternative")
            seen_alternatives.add(identity)
            normalized_alternatives.append({"all_of": normalized_conjunction})
        normalized_alternatives.sort(key=lambda item: tuple(item["all_of"]))
        group_atoms = [atoms_by_id[atom_id] for atom_id in group_atom_ids]
        if all(atom["kind"] == "role_activity" for atom in group_atoms):
            authoritative_modality = "descriptive"
        elif any(atom["kind"] == "role_activity" for atom in group_atoms):
            _fail(path, "role activities cannot share a group with candidate constraints")
        else:
            atom_modalities = {
                authoritative_modality_by_atom[atom["id"]]
                for atom in group_atoms
            }
            if len(atom_modalities) != 1:
                _fail(path, "group atoms have conflicting evidence modalities")
            authoritative_modality = next(iter(atom_modalities))
        if modality != authoritative_modality:
            _fail(
                f"{path}.modality",
                f"must be {authoritative_modality!r} according to accepted evidence",
            )
        normalized_groups.append(
            {"modality": authoritative_modality, "any_of": normalized_alternatives}
        )

    orphaned = set(atoms_by_id) - referenced_atoms
    if orphaned:
        _fail("contract.atoms", f"atoms are not assigned to a group: {sorted(orphaned)}")

    normalized_atoms.sort(key=lambda atom: atom["id"])
    return {
        "contract_version": CONTRACT_VERSION,
        "atoms": normalized_atoms,
        "constraint_groups": normalized_groups,
    }


def validate_and_normalize_contract(contract, accepted_evidence_catalog) -> dict:
    """Validate using the frozen deterministic lexical assurance path."""

    return _validate_and_normalize_contract(contract, accepted_evidence_catalog)


def validate_and_normalize_verified_contract(
    contract,
    accepted_evidence_catalog,
    verified_atom_assurances: dict,
    authoritative_modalities: dict,
) -> dict:
    """Validate a final contract admitted by explicit offline verification.

    Evidence identity, bounds, payload registries, DNF, and server authority remain
    deterministic. Only the lexical entailment fast path is replaced, and only by
    immutable, qualifier-complete assurances that explicitly authorize projection.
    """

    return _validate_and_normalize_contract(
        contract,
        accepted_evidence_catalog,
        verified_atom_assurances=verified_atom_assurances,
        authoritative_modalities=authoritative_modalities,
        allow_role_activity_composition=True,
    )


def evaluate_constraint_group(group: dict, atom_truth: dict[str, str]) -> str:
    """Evaluate a bounded DNF group with Kleene-style three-valued logic."""

    _expect_exact_keys(group, {"modality", "any_of"}, set(), "group")
    _expect_enum(group["modality"], MODALITIES, "group.modality")
    alternatives = group["any_of"]
    if type(alternatives) is not list or not alternatives:
        _fail("group.any_of", "must be a non-empty list")
    alternative_values = []
    for alternative_index, alternative in enumerate(alternatives):
        path = f"group.any_of[{alternative_index}]"
        _expect_exact_keys(alternative, {"all_of"}, set(), path)
        conjunction = alternative["all_of"]
        if type(conjunction) is not list or not conjunction:
            _fail(f"{path}.all_of", "must be a non-empty list")
        values = []
        for atom_id in conjunction:
            if atom_id not in atom_truth:
                values.append("unknown")
                continue
            values.append(_expect_enum(atom_truth[atom_id], TRUTH_VALUES, f"truth[{atom_id!r}]"))
        if "false" in values:
            alternative_values.append("false")
        elif "unknown" in values:
            alternative_values.append("unknown")
        else:
            alternative_values.append("true")
    if "true" in alternative_values:
        return "true"
    if "unknown" in alternative_values:
        return "unknown"
    return "false"


def _atom_identity(atom: dict) -> tuple:
    return (
        atom["subject"],
        atom["kind"],
        atom["normalized_value"],
        atom["temporal"],
    )


def _required_groups_are_satisfiable(groups: list[dict], atoms_by_id: dict) -> bool:
    """Return whether all required DNF groups have a polarity-consistent choice."""

    required_groups = [group for group in groups if group["modality"] == "required"]
    identities = sorted(
        {
            _atom_identity(atoms_by_id[atom_id])
            for group in required_groups
            for alternative in group["any_of"]
            for atom_id in alternative["all_of"]
        }
    )
    identity_bits = {identity: 1 << index for index, identity in enumerate(identities)}
    encoded_groups = []
    for group in required_groups:
        encoded_alternatives = set()
        for alternative in group["any_of"]:
            affirmed = 0
            negated = 0
            for atom_id in alternative["all_of"]:
                atom = atoms_by_id[atom_id]
                bit = identity_bits[_atom_identity(atom)]
                if atom["polarity"] == "affirmed":
                    affirmed |= bit
                else:
                    negated |= bit
            if affirmed & negated:
                continue
            encoded_alternatives.add((affirmed, negated))
        if not encoded_alternatives:
            return False
        encoded_groups.append(tuple(sorted(encoded_alternatives)))
    encoded_groups.sort(key=len)

    @lru_cache(maxsize=None)
    def choose(group_index: int, affirmed: int, negated: int) -> bool:
        if group_index == len(encoded_groups):
            return True
        for alternative_affirmed, alternative_negated in encoded_groups[group_index]:
            if affirmed & alternative_negated or negated & alternative_affirmed:
                continue
            if choose(
                group_index + 1,
                affirmed | alternative_affirmed,
                negated | alternative_negated,
            ):
                return True
        return False

    return choose(0, 0, 0)


def _semantic_conflict_group_indices(groups: list[dict], atoms_by_id: dict) -> set[int]:
    """Identify required groups participating in an unsatisfiable polarity set."""

    if _required_groups_are_satisfiable(groups, atoms_by_id):
        return set()
    polarities_by_identity = defaultdict(set)
    for group in groups:
        if group["modality"] != "required":
            continue
        for alternative in group["any_of"]:
            for atom_id in alternative["all_of"]:
                atom = atoms_by_id[atom_id]
                polarities_by_identity[_atom_identity(atom)].add(atom["polarity"])
    opposed_identities = {
        identity
        for identity, polarities in polarities_by_identity.items()
        if polarities == POLARITIES
    }
    return {
        group_index
        for group_index, group in enumerate(groups)
        if group["modality"] == "required"
        and any(
            _atom_identity(atoms_by_id[atom_id]) in opposed_identities
            for alternative in group["any_of"]
            for atom_id in alternative["all_of"]
        )
    }


def _assignment(field_path: str, values, *, merge: str, group_index: int) -> dict:
    return {
        "field_path": field_path,
        "values": values,
        "merge": merge,
        "group_index": group_index,
    }


def _project_group(group_index: int, group: dict, atoms_by_id: dict[str, dict]):
    alternatives = group["any_of"]
    modality = group["modality"]
    atoms = [
        atoms_by_id[atom_id]
        for alternative in alternatives
        for atom_id in alternative["all_of"]
    ]
    if any(atom["polarity"] == "negated" for atom in atoms):
        return [], "negative_fact_not_legacy_projectable"

    if len(alternatives) != 1 or len(alternatives[0]["all_of"]) != 1:
        return [], "logical_shape_not_representable"
    atom = atoms_by_id[alternatives[0]["all_of"][0]]
    kind = atom["kind"]
    payload = atom["typed_payload"]

    if kind == "language_proficiency":
        return [], "language_proficiency_not_losslessly_legacy_projectable"
    if kind == "role_activity":
        return [], "role_activity_artifact_not_legacy_projectable"
    if kind == "asset_access":
        return [], "asset_access_has_no_exact_legacy_field"
    if kind == "regulatory_registration":
        return [], "regulatory_registration_has_no_exact_legacy_field"

    def require_temporal(expected: str = "unspecified"):
        if atom["temporal"] != expected:
            return "temporal_not_losslessly_legacy_projectable"
        return None

    def locale_expertise_label() -> str:
        language = payload["language"].replace("_", " ").title()
        locale = LOCALE_LABELS[payload["locale"]]
        return f"{language} ({locale}) {payload['expertise']} expertise"

    def professional_credential_projection(preferred: bool):
        if "jurisdiction" in payload:
            return [], "credential_jurisdiction_not_legacy_projectable"
        temporal_reason = require_temporal()
        if temporal_reason:
            return [], temporal_reason
        credential = payload["credential"]
        if credential == "industry_certification":
            field = (
                "attributes.requirements.credentials_preferred"
                if preferred
                else "attributes.requirements.credentials"
            )
            label = "Industry certification"
        else:
            field = (
                "attributes.requirements.licenses_preferred"
                if preferred
                else "attributes.requirements.licenses"
            )
            label = "Professional license"
        return [
            _assignment(field, [label], merge="append", group_index=group_index)
        ], None

    if modality == "required":
        if kind == "capability":
            temporal_reason = require_temporal()
            if temporal_reason:
                return [], temporal_reason
            return [
                _assignment(
                    "attributes.requirements.skills_required",
                    [CAPABILITY_LABELS[payload["capability"]]],
                    merge="append",
                    group_index=group_index,
                )
            ], None
        if kind == "experience":
            if "minimum_years" in payload:
                return [], "experience_minimum_years_not_legacy_projectable"
            return [
                _assignment(
                    "attributes.requirements.experience_required",
                    [EXPERIENCE_LABELS[payload["area"]]],
                    merge="append",
                    group_index=group_index,
                )
            ], None
        if kind == "education":
            temporal_reason = require_temporal()
            if temporal_reason:
                return [], temporal_reason
            if payload["level"] == "advanced_degree" or "field" in payload:
                return [], "education_not_losslessly_legacy_projectable"
            return [
                _assignment(
                    "attributes.requirements.education.minimum_level",
                    payload["level"],
                    merge="scalar",
                    group_index=group_index,
                )
            ], None
        if kind == "professional_status":
            if atom["temporal"] != "current":
                return [], "temporal_not_losslessly_legacy_projectable"
            return [
                _assignment(
                    "attributes.requirements.current_status_requirements",
                    [STATUS_LABELS[(payload["status"], payload["scope"])]],
                    merge="append",
                    group_index=group_index,
                )
            ], None
        if kind == "locale_dialect_expertise":
            temporal_reason = require_temporal()
            if temporal_reason:
                return [], temporal_reason
            return [
                _assignment(
                    "attributes.requirements.skills_required",
                    [locale_expertise_label()],
                    merge="append",
                    group_index=group_index,
                )
            ], None
        if kind == "domain_expertise":
            temporal_reason = require_temporal()
            if temporal_reason:
                return [], temporal_reason
            return [
                _assignment(
                    "attributes.requirements.skills_required",
                    [DOMAIN_LABELS[payload["domain"]]],
                    merge="append",
                    group_index=group_index,
                )
            ], None
        if kind == "professional_credential":
            return professional_credential_projection(preferred=False)
        return [], "kind_not_legacy_projectable"

    if modality == "preferred":
        if kind == "capability":
            temporal_reason = require_temporal()
            if temporal_reason:
                return [], temporal_reason
            return [
                _assignment(
                    "attributes.requirements.skills_preferred",
                    [CAPABILITY_LABELS[payload["capability"]]],
                    merge="append",
                    group_index=group_index,
                )
            ], None
        if kind == "experience":
            if "minimum_years" in payload:
                return [], "experience_minimum_years_not_legacy_projectable"
            return [
                _assignment(
                    "attributes.requirements.experience_preferred",
                    [EXPERIENCE_LABELS[payload["area"]]],
                    merge="append",
                    group_index=group_index,
                )
            ], None
        if kind == "education":
            temporal_reason = require_temporal()
            if temporal_reason:
                return [], temporal_reason
            if payload["level"] == "advanced_degree" or "field" in payload:
                return [], "education_not_losslessly_legacy_projectable"
            return [
                _assignment(
                    "attributes.requirements.education.preferred_levels",
                    [payload["level"]],
                    merge="append",
                    group_index=group_index,
                )
            ], None
        if kind == "locale_dialect_expertise":
            temporal_reason = require_temporal()
            if temporal_reason:
                return [], temporal_reason
            return [
                _assignment(
                    "attributes.requirements.skills_preferred",
                    [locale_expertise_label()],
                    merge="append",
                    group_index=group_index,
                )
            ], None
        if kind == "domain_expertise":
            temporal_reason = require_temporal()
            if temporal_reason:
                return [], temporal_reason
            return [
                _assignment(
                    "attributes.requirements.skills_preferred",
                    [DOMAIN_LABELS[payload["domain"]]],
                    merge="append",
                    group_index=group_index,
                )
            ], None
        if kind == "professional_credential":
            return professional_credential_projection(preferred=True)
        return [], "kind_not_legacy_projectable"

    return [], "modality_not_legacy_projectable"


def _set_sparse_path(target: dict, field_path: str, value) -> None:
    parts = field_path.split(".")
    cursor = target
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value


def project_legacy_compatibility(
    contract,
    accepted_evidence_catalog,
    *,
    verified_atom_assurances: dict | None = None,
    authoritative_modalities: dict | None = None,
) -> dict:
    """Validate and conservatively project a semantic contract offline.

    Boundary v1 keeps this projection for diagnostics and backward-compatible
    evaluation only.  Its machine-readable authority envelope explicitly denies
    hard-eligibility and exclusion authority even when a projected field name
    historically looked like a hard requirement.
    """

    if verified_atom_assurances is None and authoritative_modalities is None:
        normalized = validate_and_normalize_contract(
            contract, accepted_evidence_catalog
        )
    else:
        normalized = validate_and_normalize_verified_contract(
            contract,
            accepted_evidence_catalog,
            verified_atom_assurances,
            authoritative_modalities,
        )
    atoms_by_id = {atom["id"]: atom for atom in normalized["atoms"]}
    semantic_conflict_groups = _semantic_conflict_group_indices(
        normalized["constraint_groups"], atoms_by_id
    )

    group_outcomes = []
    candidate_assignments = []
    atom_to_group = {}
    for group_index, group in enumerate(normalized["constraint_groups"]):
        atom_ids = sorted(
            atom_id
            for alternative in group["any_of"]
            for atom_id in alternative["all_of"]
        )
        for atom_id in atom_ids:
            atom_to_group[atom_id] = group_index
        if group_index in semantic_conflict_groups:
            group_outcomes.append(
                {
                    "group_index": group_index,
                    "atom_ids": atom_ids,
                    "state": "conflicted",
                    "reason": "contradictory_required_assertions",
                    "field_paths": [],
                }
            )
            continue
        assignments, reason = _project_group(group_index, group, atoms_by_id)
        if reason is not None:
            group_outcomes.append(
                {
                    "group_index": group_index,
                    "atom_ids": atom_ids,
                    "state": "grounded_unprojected",
                    "reason": reason,
                    "field_paths": [],
                }
            )
        else:
            candidate_assignments.extend(assignments)
            group_outcomes.append(
                {
                    "group_index": group_index,
                    "atom_ids": atom_ids,
                    "state": "projected",
                    "reason": None,
                    "field_paths": sorted(
                        {assignment["field_path"] for assignment in assignments}
                    ),
                }
            )

    conflicting_projection_groups = set()
    scalar_assignments = defaultdict(list)
    for assignment in candidate_assignments:
        if assignment["merge"] == "scalar":
            scalar_assignments[assignment["field_path"]].append(assignment)
    for assignments in scalar_assignments.values():
        values = {_canonical_json(assignment["values"]) for assignment in assignments}
        if len(values) > 1:
            conflicting_projection_groups.update(
                assignment["group_index"] for assignment in assignments
            )
    if conflicting_projection_groups:
        candidate_assignments = [
            assignment
            for assignment in candidate_assignments
            if assignment["group_index"] not in conflicting_projection_groups
        ]
        for outcome in group_outcomes:
            if outcome["group_index"] in conflicting_projection_groups:
                outcome.update(
                    {
                        "state": "grounded_unprojected",
                        "reason": "legacy_projection_conflict",
                        "field_paths": [],
                    }
                )

    values_by_path = {}
    merge_by_path = {}
    for assignment in candidate_assignments:
        path = assignment["field_path"]
        merge = assignment["merge"]
        merge_by_path[path] = merge
        if merge == "scalar":
            values_by_path[path] = copy.deepcopy(assignment["values"])
        else:
            values_by_path.setdefault(path, [])
            values_by_path[path].extend(copy.deepcopy(assignment["values"]))
    for path, merge in merge_by_path.items():
        if merge != "append":
            continue
        unique = {_canonical_json(value): value for value in values_by_path[path]}
        values_by_path[path] = [unique[key] for key in sorted(unique)]

    legacy_patch = {}
    for path in sorted(values_by_path):
        value = values_by_path[path]
        if type(value) is list and not value:
            raise AssertionError("sparse projection must never emit an empty list")
        _set_sparse_path(legacy_patch, path, value)

    outcomes_by_group = {
        outcome["group_index"]: outcome["state"] for outcome in group_outcomes
    }
    kind_states = {}
    for kind in ATOM_KINDS:
        atom_ids = [atom["id"] for atom in normalized["atoms"] if atom["kind"] == kind]
        if not atom_ids:
            kind_states[kind] = "unknown"
            continue
        states = {outcomes_by_group[atom_to_group[atom_id]] for atom_id in atom_ids}
        if "conflicted" in states:
            kind_states[kind] = "conflicted"
        elif states == {"grounded_unprojected"}:
            kind_states[kind] = "grounded_unprojected"
        elif states == {"projected"}:
            kind_states[kind] = "grounded_projected"
        else:
            kind_states[kind] = "grounded_mixed"

    # Local import avoids making the proposition contract depend on matching and
    # avoids a module-load cycle with the boundary packet builder.
    from wahojobs.opportunity_semantic_authority import (
        semantic_compatibility_projection_authority,
    )

    return {
        "contract_version": CONTRACT_VERSION,
        "normalized_contract": normalized,
        "legacy_patch": legacy_patch,
        "compatibility_authority": semantic_compatibility_projection_authority(),
        "group_outcomes": group_outcomes,
        "knowledge_by_kind": kind_states,
    }


def project_verified_legacy_compatibility(
    contract,
    accepted_evidence_catalog,
    verified_atom_assurances: dict,
    authoritative_modalities: dict,
) -> dict:
    """Build a diagnostic projection from a qualifier-complete final contract.

    ``hard_projection_authorized`` is retained as an experimental v0 admission
    token for artifact compatibility.  It is not product hard-gate authority;
    the returned compatibility authority is always semantic non-exclusionary.
    """

    return project_legacy_compatibility(
        contract,
        accepted_evidence_catalog,
        verified_atom_assurances=verified_atom_assurances,
        authoritative_modalities=authoritative_modalities,
    )


def flatten_patch_paths(patch: dict, prefix: str = "") -> dict[str, object]:
    """Flatten a sparse compatibility patch for audit assertions and reports."""

    flattened = {}
    for key, value in patch.items():
        path = f"{prefix}.{key}" if prefix else key
        if type(value) is dict:
            flattened.update(flatten_patch_paths(value, path))
        else:
            flattened[path] = value
    return flattened
