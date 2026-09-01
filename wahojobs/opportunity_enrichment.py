"""Durable deterministic opportunity enrichment and field-level overrides."""

from __future__ import annotations

import copy
import html
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

from wahojobs.matching.domains import detect_role_domains
from wahojobs.matching.languages import (
    CANONICAL_LANGUAGES,
    find_language_mentions,
    normalize_language_name,
    requirement_mode_for_mentions,
)
from wahojobs.matching.locations import (
    REGIONAL_LOCATION_TOKENS,
    classify_job_location,
    countries_in_location,
    regions_in_location,
)
from wahojobs.matching.specializations import specialization_requirements
from wahojobs.matching.taxonomy import CAREER_LEVELS, OCCUPATIONAL_FAMILIES
from wahojobs.profiles.countries import CANONICAL_COUNTRIES
from wahojobs.profiles.preference_model import ISO_4217_CURRENCIES


SCHEMA_VERSION = "opportunity_enrichment_v5"
LEGACY_SCHEMA_VERSION = "opportunity_enrichment_v2"
TAXONOMY_VERSION = "opportunity_taxonomy_v2_2026_08"
EXTRACTOR_VERSION = "hybrid_evidence_vnext_v4"
SEMANTIC_INPUT_VERSION = "opportunity_semantic_input_v3"
LEGACY_SEMANTIC_INPUT_VERSION = "opportunity_semantic_input_v1"
DERIVATION_RECIPE_VERSION = "opportunity_enrichment_derivation_v8"
LLM_ACCEPTANCE_GUARDS_VERSION = "opportunity_llm_acceptance_guards_v9"

STALE_REASON_DERIVATION_CONTRACT_CHANGED = "derivation_contract_changed"
STALE_REASON_SOURCE_INPUT_CHANGED = "source_input_changed"
STALE_REASON_MISSING_ENRICHMENT = "missing_enrichment"

# Historical semantic-input projections are immutable contracts.  Keeping both
# versions literal means a future current-version bump cannot change the bytes
# used to compare source evidence produced under V1 or V2.
HISTORICAL_SEMANTIC_INPUT_FIELDS_BY_VERSION = {
    "opportunity_semantic_input_v1": (
        "company",
        "canonical",
        "source_fields",
        "variants",
    ),
    "opportunity_semantic_input_v2": (
        "company",
        "canonical",
        "source_fields",
        "variants",
        "rich_content",
    ),
    "opportunity_semantic_input_v3": (
        "company",
        "canonical",
        "source_fields",
        "variants",
        "rich_content",
    ),
}

# Rows created before semantic-input versioning can only be interpreted from
# version evidence they actually persisted.  Every key and value in this map
# is a literal historical fact; neither side may inherit mutable current-version
# constants.  This does not rewrite or backfill legacy rows.
LEGACY_SEMANTIC_INPUT_VERSION_BY_EXTRACTOR = {
    "deterministic_v1": "opportunity_semantic_input_v1",
    "deterministic_plus_structured_llm_v1": "opportunity_semantic_input_v2",
}
# This literal is intentionally not derived from DERIVATION_RECIPE_VERSION: a
# future recipe bump must make fingerprint-less rows stale instead of silently
# extending their compatibility claim.
LEGACY_DERIVATION_RECIPE_VERSION_BY_EXTRACTOR = {
    "deterministic_plus_structured_llm_v1": "opportunity_enrichment_derivation_v1",
}

LEGACY_VARIANT_FIELDS = (
    "title",
    "location",
    "department",
    "expertise",
    "commitment",
    "url",
    "opportunity_kind",
    "availability_basis",
    "include_in_live_market_estimate",
)
LEGACY_RICH_CONTENT_FIELDS = (
    "source_ref",
    "provider",
    "source_type",
    "source_url",
    "external_id",
    "body",
    "body_format",
    "metadata",
    "material_content_sha256",
)

STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"

ROLE_FAMILIES = frozenset(OCCUPATIONAL_FAMILIES) | frozenset(
    {"audio_speech", "data_collection"}
)
PROFESSIONAL_DOMAINS = frozenset(
    {
        "biology",
        "chemistry",
        "finance",
        "legal",
        "material_science",
        "mathematics",
        "medicine",
        "physics",
        "technical",
    }
)
WORK_ACTIVITIES = frozenset(
    {
        "ads_evaluation",
        "ai_training_evaluation",
        "audio_speech",
        "content_moderation",
        "data_annotation",
        "data_collection",
        "localization",
        "operations",
        "research_analysis",
        "search_evaluation",
        "software_development",
        "software_testing",
        "transcription",
        "translation",
        "writing_editing",
    }
)
SENIORITY_VALUES = frozenset({"unknown"}) | CAREER_LEVELS
ENGAGEMENT_TYPES = frozenset(
    {"unknown", "full_time", "part_time", "contract", "freelance", "temporary", "internship", "volunteer"}
)
SCHEDULE_TYPES = frozenset({"unknown", "flexible", "fixed"})
WORKPLACE_MODES = frozenset({"unknown", "remote", "hybrid", "onsite"})
LOCATION_SCOPES = frozenset(
    {
        "unknown",
        "remote_worldwide",
        "remote_restricted",
        "onsite_or_hybrid_restricted",
    }
)
LANGUAGE_REQUIREMENT_MODES = frozenset(
    {"none", "single", "all_required", "any_supported", "ambiguous"}
)
EDUCATION_LEVELS = frozenset(
    {"unknown", "no_degree", "secondary", "associate", "bachelor", "master", "doctorate"}
)
COMPENSATION_PERIODS = frozenset(
    {"unknown", "hour", "day", "week", "month", "year", "project", "asset", "source_word"}
)
COMPENSATION_AMOUNT_TYPES = frozenset(
    {"unknown", "exact", "range", "from", "up_to"}
)
EVIDENCE_BASES = frozenset(
    {
        "source_explicit",
        "deterministic_parse",
        "deterministic_classification",
        "llm_source_evidence",
    }
)
CONFIDENCE_VALUES = frozenset({"low", "medium", "high"})
KNOWLEDGE_STATES = frozenset({"known_value", "known_empty"})
MIN_LLM_BODY_CHARACTERS = 400
MIN_LLM_MATERIAL_CHARACTERS = 800
MAX_LLM_SOURCE_CHARACTERS = 40_000

ATOMIC_LIST_FIELD_PATHS = frozenset(
    {
        "attributes.role.professional_domains",
        "attributes.role.work_activities",
        "attributes.role.specializations",
        "attributes.requirements.languages",
        "attributes.requirements.skills_required",
        "attributes.requirements.skills_preferred",
        "attributes.requirements.education.accepted_alternatives",
        "attributes.requirements.education.preferred_levels",
        "attributes.requirements.credentials",
        "attributes.requirements.credentials_preferred",
        "attributes.requirements.licenses",
        "attributes.requirements.licenses_preferred",
        "attributes.requirements.experience_required",
        "attributes.requirements.experience_preferred",
        "attributes.requirements.current_status_requirements",
        "attributes.work_arrangement.eligible_countries",
        "attributes.work_arrangement.eligible_regions",
        "attributes.work_arrangement.eligible_locations",
        "attributes.content.responsibilities",
        "attributes.content.benefits",
        "attributes.content.caveats",
    }
)
KNOWN_EMPTY_FIELD_PATHS = ATOMIC_LIST_FIELD_PATHS

LLM_SCALAR_FIELD_PATHS = {
    "role_family": "attributes.role.role_family",
    "education_minimum_level": "attributes.requirements.education.minimum_level",
    "years_experience_min": "attributes.requirements.years_experience_min",
    "years_experience_preferred_min": (
        "attributes.requirements.years_experience_preferred_min"
    ),
    "workplace_mode": "attributes.work_arrangement.workplace_mode",
    "location_scope": "attributes.work_arrangement.location_scope",
    "engagement_type": "attributes.work_arrangement.engagement_type",
    "schedule_type": "attributes.work_arrangement.schedule_type",
    "hours_per_week_min": "attributes.work_arrangement.hours_per_week_min",
    "hours_per_week_max": "attributes.work_arrangement.hours_per_week_max",
    "duration": "attributes.work_arrangement.duration",
    "compensation_disclosed": "attributes.compensation.disclosed",
    "compensation_currency": "attributes.compensation.currency",
    "compensation_amount_min": "attributes.compensation.amount_min",
    "compensation_amount_max": "attributes.compensation.amount_max",
    "compensation_period": "attributes.compensation.period",
    "compensation_amount_type": "attributes.compensation.amount_type",
    "compensation_notes": "attributes.compensation.notes",
    "candidate_profile": "attributes.content.candidate_profile",
    "quick_take": "attributes.content.quick_take",
}
LLM_LIST_FIELD_PATHS = {
    "professional_domains": "attributes.role.professional_domains",
    "work_activities": "attributes.role.work_activities",
    "specializations": "attributes.role.specializations",
    "skills_required": "attributes.requirements.skills_required",
    "skills_preferred": "attributes.requirements.skills_preferred",
    "education_accepted_alternatives": (
        "attributes.requirements.education.accepted_alternatives"
    ),
    "education_preferred_levels": (
        "attributes.requirements.education.preferred_levels"
    ),
    "credentials": "attributes.requirements.credentials",
    "credentials_preferred": "attributes.requirements.credentials_preferred",
    "licenses": "attributes.requirements.licenses",
    "licenses_preferred": "attributes.requirements.licenses_preferred",
    "experience_required": "attributes.requirements.experience_required",
    "experience_preferred": "attributes.requirements.experience_preferred",
    "current_status_requirements": (
        "attributes.requirements.current_status_requirements"
    ),
    "languages": "attributes.requirements.languages",
    "eligible_countries": "attributes.work_arrangement.eligible_countries",
    "eligible_regions": "attributes.work_arrangement.eligible_regions",
    "eligible_locations": "attributes.work_arrangement.eligible_locations",
    "responsibilities": "attributes.content.responsibilities",
    "caveats": "attributes.content.caveats",
}
LLM_PAYLOAD_FIELDS = frozenset(
    {*LLM_SCALAR_FIELD_PATHS, *LLM_LIST_FIELD_PATHS, "known_empty_fields"}
)


def blank_llm_payload() -> dict:
    return {
        **{
            field: {"value": None, "evidence": []}
            for field in LLM_SCALAR_FIELD_PATHS
        },
        **{field: [] for field in LLM_LIST_FIELD_PATHS},
        "known_empty_fields": [],
    }


class EnrichmentValidationError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def blank_document() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "source": {
            "company_name": None,
            "company_slug": None,
            "canonical_key": None,
            "canonical_title": None,
            "source_category": None,
            "source_tier": None,
            "inventory_model": None,
            "market_count_policy": None,
            "opportunity_kinds": [],
            "availability_bases": [],
            "include_in_live_market_estimate": None,
        },
        "attributes": {
            "role": {
                "role_family": None,
                "professional_domains": [],
                "work_activities": [],
                "specializations": [],
                "seniority": "unknown",
            },
            "work_arrangement": {
                "workplace_mode": "unknown",
                "location_scope": "unknown",
                "eligible_countries": [],
                "eligible_regions": [],
                "eligible_locations": [],
                "engagement_type": "unknown",
                "schedule_type": "unknown",
                "hours_per_week_min": None,
                "hours_per_week_max": None,
                "duration": None,
            },
            "requirements": {
                "languages": [],
                "skills_required": [],
                "skills_preferred": [],
                "education": {
                    "minimum_level": "unknown",
                    "accepted_alternatives": [],
                    "preferred_levels": [],
                },
                "credentials": [],
                "credentials_preferred": [],
                "licenses": [],
                "licenses_preferred": [],
                "experience_required": [],
                "experience_preferred": [],
                "current_status_requirements": [],
                "years_experience_min": None,
                "years_experience_preferred_min": None,
            },
            "compensation": {
                "disclosed": None,
                "currency": None,
                "amount_min": None,
                "amount_max": None,
                "period": "unknown",
                "amount_type": "unknown",
                "notes": None,
            },
            "application": {
                "application_url": None,
                "deadline": None,
                "assessment_required": None,
                "portfolio_or_sample_required": None,
                "login_required": None,
            },
            "content": {
                "quick_take": None,
                "responsibilities": [],
                "candidate_profile": None,
                "benefits": [],
                "caveats": [],
            },
        },
        "field_evidence": [],
        "variant_facts": [],
        "unknown_fields": [],
    }


def _field_defaults() -> dict[str, object]:
    document = blank_document()
    defaults = {}
    for group, fields in document["attributes"].items():
        for field, value in fields.items():
            if isinstance(value, dict):
                for child, child_value in value.items():
                    defaults[f"attributes.{group}.{field}.{child}"] = child_value
            else:
                defaults[f"attributes.{group}.{field}"] = value
    return defaults


FIELD_DEFAULTS = _field_defaults()
OVERRIDABLE_FIELDS = frozenset(FIELD_DEFAULTS)


ROLE_FAMILY_RULES = (
    ("software_testing", ("software tester", "software testing", "quality assurance", "qa engineer")),
    ("software_engineering", ("software engineer", "software developer", "coding", "programming", "developer")),
    ("translation_localization", ("translation", "translator", "localization", "mtpe", "post editing")),
    ("search_evaluation", ("search quality", "search evaluator", "search rater", "ads quality", "ads evaluator")),
    ("writing_editing", ("writer", "writing", "editing", "content reviewer", "prompt author")),
    ("data_collection", ("data collection", "collector", "recording project", "photo collection", "video collection")),
    ("data_annotation", ("annotation", "annotator", "labeling", "labelling")),
    ("language_data", ("language specialist", "language expert", "linguistic", "bilingual")),
    ("audio_speech", ("audio", "speech", "voice actor", "voice coach", "transcription")),
    ("accounting_finance", ("accounting", "finance", "financial", "investment", "banking")),
    ("legal", ("legal", "lawyer", "attorney", "law ", "patent", "trademark")),
    ("healthcare", ("medical", "medicine", "physician", "nurse", "clinical", "healthcare")),
    ("science_research", ("biology", "chemistry", "physics", "scientist", "research")),
    ("customer_support", ("customer support", "customer service")),
    ("sales_marketing", ("sales", "marketing", "advertising")),
    ("design", ("designer", "graphic design", "brand design", "3d modeling", "cad")),
    ("data_analysis", ("data analyst", "data science", "statistics", "analytics")),
    ("ai_training", ("ai trainer", "ai training", "ai evaluator", "llm evaluation", "rlhf")),
    ("expert_review", ("domain expert", "subject matter expert", "expert review")),
    ("operations", ("operations", "project manager", "program manager")),
)

WORK_ACTIVITY_RULES = (
    ("ai_training_evaluation", ("ai trainer", "ai training", "ai evaluator", "llm evaluation", "rlhf", "model response")),
    ("data_annotation", ("annotation", "annotator", "labeling", "labelling")),
    ("data_collection", ("data collection", "collector", "photo collection", "video collection", "recording project")),
    ("search_evaluation", ("search quality", "search evaluator", "search rater")),
    ("ads_evaluation", ("ads quality", "ads evaluator", "advertisement reviewer")),
    ("translation", ("translation", "translator", "mtpe", "post editing")),
    ("localization", ("localization", "localisation")),
    ("transcription", ("transcription", "transcriber")),
    ("audio_speech", ("audio", "speech", "voice actor", "voice coach", "dubbing")),
    ("writing_editing", ("writer", "writing", "editing", "content reviewer", "prompt author")),
    ("software_development", ("software engineer", "software developer", "coding", "programming", "developer")),
    ("software_testing", ("software tester", "software testing", "quality assurance", "qa engineer")),
    ("content_moderation", ("content moderation", "content moderator")),
    ("research_analysis", ("research", "analysis", "analyst")),
    ("operations", ("operations", "project management", "program management")),
)

SUBSTANTIVE_ACTIVITY_PATTERNS = {
    "ads_evaluation": (
        r"\b(?:ad|ads|advertisement)s?\b.{0,100}\b(?:evaluat|rat|review)",
        r"\b(?:evaluat|rat|review)\w*\b.{0,100}\b(?:ad|ads|advertisement)s?\b",
    ),
    "ai_training_evaluation": (
        r"\b(?:ai|language model|models?|model outputs?|model responses?|llm)\b.{0,140}"
        r"\b(?:assess|challeng|convers|evaluat|provid\w* feedback|rank|rat|review|test|train|validat|verif)\w*\b",
        r"\b(?:assess|challeng|convers|evaluat|provid\w* feedback|rank|rat|review|test|train|validat|verif)\w*\b.{0,140}"
        r"\b(?:ai|language model|models?|model outputs?|model responses?|llm)\b",
    ),
    "audio_speech": (r"\b(?:audio|speech|voice|recording)s?\b",),
    "content_moderation": (r"\bmoderat(?:e|es|ing|ion)\b",),
    "data_annotation": (
        r"\b(?:content|data|errors?|image|text|video)s?\b.{0,80}\b(?:annotat|label)\w*\b",
        r"\b(?:annotat|label)\w*\b.{0,80}\b(?:content|data|errors?|image|text|video)s?\b",
        r"\b(?:tag|categor)\w*\b.{0,60}\bstructured data\b",
        r"\bstructured data\b.{0,60}\b(?:tag|categor)\w*\b",
    ),
    "data_collection": (r"\b(?:collect|capture|record|gather)(?:s|ed|ing)?\b",),
    "localization": (r"\blocali[sz](?:e|es|ed|ing|ation)\b",),
    "operations": (
        r"\b(?:coordinat|implement|maintain|manag|monitor|onboard|operat|process|"
        r"reconcil|record|review|settle|support|validat)\w*\b.{0,100}"
        r"\b(?:accounts?|assets?|cash|clients?|documents?|funds?|implementations?|"
        r"onboarding|payroll|plans?|process(?:es)?|requests?|settlements?|systems?|"
        r"trades?|transfers?|workflows?)\b",
        r"\b(?:responsible for|day[- ]to[- ]day duties|typical day)\b.{0,160}"
        r"\b(?:coordinat|implement|maintain|manag|monitor|onboard|operat|process|"
        r"reconcil|record|review|settle|support|validat)\w*\b",
    ),
    "research_analysis": (
        r"\b(?:data|financial|market|policy|qualitative|quantitative|scientific|statistical)\s+analys(?:is|t)\b",
        r"\bresearch(?:er|ing)?\b",
        r"\b(?:conduct|perform|carry out)\b.{0,30}\bresearch\b",
    ),
    "search_evaluation": (r"\bsearch(?: result| quality| engine)?.*\b(?:evaluat|rat|review)",),
    "software_development": (
        r"\b(?:code|coding|program|develop|developer|implement|build)(?:s|ed|ing)?\b",
    ),
    "software_testing": (r"\b(?:test|debug|qa|quality assurance)(?:s|ed|ing)?\b",),
    "transcription": (r"\btranscri(?:be|bes|bed|bing|pt|ption)\b",),
    "translation": (r"\btranslat(?:e|es|ed|ing|ion)\b",),
    "writing_editing": (
        r"\b(?:writ(?:e|es|ing|ten)|edit(?:s|ed|ing)?|rewrit(?:e|es|ing|ten)|"
        r"draft(?:s|ed|ing)?|author(?:s|ed|ing)?)\b",
        r"\b(?:creat|develop|produc)\w*\b.{0,80}"
        r"\b(?:documentation|educational resources?|guides?|reports?|rubrics?|"
        r"prompts?|written materials?)\b",
    ),
}

SOFTWARE_TEST_ACTION_PATTERN = re.compile(
    r"\b(?:debug|qa|quality assurance|test)(?:s|ed|ing)?\b"
)
SOFTWARE_TEST_ARTIFACT_PATTERN = re.compile(
    r"\b(?:applications?|apps?|code|digital tools?|features?|interfaces?|platforms?|"
    r"software|systems?|technical workflows?|user experience|ux|websites?)\b"
)

# Professional domains are accepted only from body evidence that establishes
# actual domain expertise or substantive domain work. Tool names, formats,
# listing labels, and degree preferences are not domain authority.
PROFESSIONAL_DOMAIN_EVIDENCE_PATTERNS = {
    "biology": (
        r"\b(?:biology|biological|life sciences?)\b.{0,80}"
        r"\b(?:analysis|expert|expertise|knowledge|research|scientist|specialist)\w*\b",
        r"\b(?:biologist|biological scientist)\b",
    ),
    "chemistry": (
        r"\b(?:chemistry|chemical sciences?)\b.{0,80}"
        r"\b(?:analysis|expert|expertise|knowledge|research|scientist|specialist)\w*\b",
        r"\bchemist\b",
    ),
    "finance": (
        r"\b(?:finance|financial|insurance)\b.{0,100}"
        r"\b(?:deliverables?|expert|expertise|industry|knowledge|practice|standards?|specialist)\w*\b",
        r"\b(?:accountants?|actuar(?:y|ies|ial)|auditors?|financial analysts?)\b",
    ),
    "legal": (
        r"\b(?:law|legal|litigation)\b.{0,100}"
        r"\b(?:attorney|counsel|expert|expertise|knowledge|practice|regulations?|specialist)\w*\b",
        r"\b(?:attorneys?|lawyers?|litigators?)\b",
    ),
    "material_science": (
        r"\b(?:material science|materials science)\b.{0,80}"
        r"\b(?:analysis|expert|expertise|knowledge|research|scientist|specialist)\w*\b",
    ),
    "mathematics": (
        r"\b(?:math|mathematics|mathematical)\b.{0,80}"
        r"\b(?:analysis|expert|expertise|knowledge|modeling|proofs?|reasoning|research|specialist)\w*\b",
        r"\b(?:expert|expertise|specialist)\w*\b.{0,80}\b(?:math|mathematics)\b",
        r"\b(?:algebra|calculus|geometry|number theory|probability|statistics)\b",
    ),
    "medicine": (
        r"\b(?:clinical|health sciences?|medical|medicine)\b.{0,100}"
        r"\b(?:clinician|expert|expertise|knowledge|practice|professional|specialist)\w*\b",
        r"\b(?:doctors?|nurses?|physicians?)\b",
    ),
    "physics": (
        r"\bphysics\b.{0,80}"
        r"\b(?:analysis|expert|expertise|knowledge|research|scientist|specialist)\w*\b",
        r"\bphysicists?\b",
    ),
    "technical": (
        r"\b(?:code|coding|computer science|engineering|programming|software|systems?)\b.{0,100}"
        r"\b(?:build|develop|engineer|expert|expertise|implement|knowledge|specialist)\w*\b",
        r"\b(?:build|develop|engineer|implement|program)\w*\b.{0,100}"
        r"\b(?:applications?|code|platforms?|services?|software|systems?)\b",
    ),
}

TASK_LEADING_PATTERN = re.compile(
    r"^(?:captur(?:e|ing)|collect(?:ing)?|complet(?:e|ing)|creat(?:e|ing)|"
    r"design(?:ing)?|draft(?:ing)?|ensur(?:e|ing)|evaluat(?:e|ing)|follow(?:ing)?|"
    r"identif(?:y|ying)|install(?:ing)?|log(?: in|ging in)|maintain(?:ing)?|"
    r"manag(?:e|ing)|meet(?:ing)?|monitor(?:ing)?|perform(?:ing)?|provid(?:e|ing)|"
    r"prepar(?:e|ing)|record(?:ing)?|review(?:ing)?|run(?:ning)?|"
    r"set(?: up|ting up)|sourc(?:e|ing)|"
    r"submit(?:ting)?|tag(?:ging)?|upload(?:ing)?|writ(?:e|ing))\b"
)
CANDIDATE_ATTRIBUTE_PATTERN = re.compile(
    r"\b(?:abilit(?:y|ies)|capabilit(?:y|ies)|certification|competenc(?:e|y|ies)|"
    r"experience|expertise|familiarity|fluency|knowledge|licen[cs]e|proficien(?:cy|t)|"
    r"skills?|skilled)\b"
)
TASK_ACTION_PATTERN = re.compile(
    r"\b(?:captur(?:e|ing)|collect(?:ion|ing)?|complet(?:e|ing)|creat(?:e|ing)|"
    r"draft(?:ing)?|ensur(?:e|ing)|evaluat(?:e|ing)|follow(?:ing)?|"
    r"identif(?:y|ying)|install(?:ation|ing)?|log(?: in|ging in)|maintain(?:ing)?|"
    r"monitor(?:ing)?|perform(?:ing)?|prepar(?:ation|e|ing)|provid(?:e|ing)|"
    r"record(?:ing)?|review(?:ing)?|run(?:ning)?|set(?: up|ting up)|sourc(?:e|ing)|"
    r"submit(?:ting|ssion)?|tag(?:ging)?|upload(?:ing)?|writ(?:e|ing))\b"
)
TASK_OBJECT_PATTERN = re.compile(
    r"\b(?:data|deliverables?|files?|footage|forms?|images?|media|metadata|"
    r"recordings?|reports?|responses?|standards?|submissions?|tasks?|videos?)\b"
)
TASK_COMPOUND_PATTERN = re.compile(r"\b(?:and|or)\b")
TASK_OBJECT_LINK_PATTERN = re.compile(r"\b(?:for|from|of|to)\b")
DELIVERABLE_CONSTRAINT_PATTERN = re.compile(
    r"\b(?:accept(?:ed|able)|format(?:s|ted)?|guidelines?|maximum|minimum|required|"
    r"specifications?|specified|standards?|supported)\b"
)
SKILL_CONSTRAINT_PATTERN = re.compile(
    r"\b(?:"
    r"(?:own|reliable|secure|required) (?:computer|device|equipment)|"
    r"internet (?:access|connection)|anti-?virus|headset|"
    r"(?:sign|execute) (?:an? )?(?:nda|confidentiality agreement)|"
    r"(?:pass|complete|take) (?:an? )?(?:test|assessment|screening|training)|"
    r"(?:hours? per week|working hours?|schedule|time ?zone|overlap)|"
    r"(?:pay|paid|rate|compensation|salary)|"
    r"(?:remote|on-?site|hybrid|work arrangement|full-?time|part-?time|contractor|freelance)"
    r")\b"
)
ROUTINE_CAVEAT_PATTERN = re.compile(
    r"\b(?:"
    r"schedule|working hours?|hours? per week|time ?zone|overlap|"
    r"pay|paid|rate|compensation|salary|remote|on-?site|hybrid|"
    r"work arrangement|full-?time|part-?time|contractor|freelance|"
    r"nda|confidentiality agreement|screening|assessment|test|training program"
    r")\b"
)
MATERIAL_EQUIPMENT_WARNING_PATTERN = re.compile(
    r"\b(?:must|required to|need to)\b.{0,40}\b(?:own|provide|supply)\b.{0,40}"
    r"\b(?:computer|device|equipment|internet)\b"
)
UNUSUAL_ELIGIBILITY_PATTERN = re.compile(
    r"\b(?:"
    r"(?:all|every|no) (?:members? of (?:your|the) )?(?:household|family|home)\b"
    r".{0,140}\b(?:age|aged|older|younger|under)\b|"
    r"(?:household|family) members?\b.{0,140}\b(?:age|aged|older|younger|under)\b|"
    r"no children\b.{0,60}\b(?:under|younger than|below the age of)\b|"
    r"(?:not eligible|ineligible|cannot participate|may not participate)\b"
    r".{0,140}\b(?:household|family member|previously participated|current employee|"
    r"former employee|employed by)\b"
    r")"
)
HOUSEHOLD_AGE_RESTRICTION_PATTERN = re.compile(
    r"\ball household members must be (?:age |at least )?"
    r"(?P<minimum_age>\d{1,2}) (?:years? (?:of age )?)?or older\b"
    r".{0,100}\bno children under (?P<child_age>\d{1,2})\b"
)


def load_semantic_input(conn, canonical_opportunity_id: int) -> dict:
    job_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
    }
    authority_filter = (
        "AND j.semantic_authority_state != 'pending'"
        if "semantic_authority_state" in job_columns
        else ""
    )
    canonical = conn.execute(
        """
        SELECT
          co.id, co.company_id, co.canonical_key, co.canonical_title,
          co.normalized_title, co.source_category, co.language,
          co.language_locale, co.is_active,
          c.name AS company_name, c.slug AS company_slug, c.source_tier,
          c.inventory_model, c.market_count_policy
        FROM canonical_opportunities co
        JOIN companies c ON c.id = co.company_id
        WHERE co.id = ?
        """,
        (canonical_opportunity_id,),
    ).fetchone()
    if canonical is None:
        raise EnrichmentValidationError(
            f"Unknown canonical opportunity: {canonical_opportunity_id}"
        )

    capture_authority_installed = all(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
        for table in (
            "job_source_content_captures",
            "job_source_content_acceptances",
        )
    )
    acceptance_columns = (
        "a.accepted_capture_id, a.promotion_policy_version AS accepted_policy_version, "
        "ac.capture_contract_version AS accepted_capture_contract_version, "
        "ac.semantic_material_sha256 AS accepted_semantic_material_sha256"
        if capture_authority_installed
        else (
            "NULL AS accepted_capture_id, NULL AS accepted_policy_version, "
            "NULL AS accepted_capture_contract_version, "
            "NULL AS accepted_semantic_material_sha256"
        )
    )
    acceptance_joins = (
        "LEFT JOIN job_source_content_acceptances a ON a.job_id = j.id "
        "LEFT JOIN job_source_content_captures ac "
        "ON ac.id = a.accepted_capture_id AND ac.job_id = j.id"
        if capture_authority_installed
        else ""
    )
    semantic_authority_column = (
        "j.semantic_authority_state"
        if "semantic_authority_state" in job_columns
        else "'legacy_accepted' AS semantic_authority_state"
    )
    rows = conn.execute(
        f"""
        SELECT
          j.id AS job_id,
          j.title, j.location, j.department, j.expertise, j.commitment, j.url,
          j.external_id, j.source_hash, j.opportunity_kind, j.availability_basis,
          j.include_in_live_market_estimate, j.is_active,
          {semantic_authority_column},
          sc.provider AS rich_provider, sc.source_type AS rich_source_type,
          sc.source_url AS rich_source_url, sc.external_id AS rich_external_id,
          sc.body AS rich_body, sc.body_format AS rich_body_format,
          sc.metadata_json AS rich_metadata_json,
          sc.material_content_sha256, sc.source_updated_at,
          {acceptance_columns}
        FROM jobs j
        LEFT JOIN job_source_contents sc ON sc.job_id = j.id
        {acceptance_joins}
        WHERE j.canonical_opportunity_id = ?
          {authority_filter}
          AND j.title NOT LIKE '[SIMULATION]%'
        ORDER BY j.source_hash ASC, j.id ASC
        """,
        (canonical_opportunity_id,),
    ).fetchall()
    active_rows = [row for row in rows if row["is_active"]]
    selected = active_rows or list(rows)
    if capture_authority_installed:
        from wahojobs.db.repository import verify_job_source_acceptance_integrity

        for row in selected:
            verify_job_source_acceptance_integrity(conn, row["job_id"])
    variants = []
    for row in selected:
        variant_ref = f"source_hash:{row['source_hash']}"
        variant = {
            "variant_ref": variant_ref,
            "title": clean(row["title"]),
            "location": clean(row["location"]),
            "department": clean(row["department"]),
            "expertise": clean(row["expertise"]),
            "commitment": clean(row["commitment"]),
            "url": clean(row["url"]),
            "opportunity_kind": clean(row["opportunity_kind"]),
            "availability_basis": clean(row["availability_basis"]),
            "include_in_live_market_estimate": bool(
                row["include_in_live_market_estimate"]
            ),
        }
        variants.append(variant)
    variants.sort(key=canonical_json)

    rich_content = []
    for row in selected:
        if row["material_content_sha256"] is None:
            continue
        try:
            metadata = json.loads(row["rich_metadata_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise EnrichmentValidationError(
                "Persisted source metadata is not valid JSON."
            ) from exc
        if type(metadata) is not dict:
            raise EnrichmentValidationError(
                "Persisted source metadata must be a JSON object."
            )
        rich_content.append(
            {
                "source_ref": f"source_hash:{row['source_hash']}",
                "variant_ref": f"source_hash:{row['source_hash']}",
                "provider": row["rich_provider"],
                "source_type": row["rich_source_type"],
                "source_url": row["rich_source_url"],
                "external_id": row["rich_external_id"],
                "body": row["rich_body"],
                "body_format": row["rich_body_format"],
                "metadata": metadata,
                "material_content_sha256": row["material_content_sha256"],
                "semantic_material_sha256": row[
                    "accepted_semantic_material_sha256"
                ],
                "authority": {
                    "semantic_authority_state": row["semantic_authority_state"],
                    "accepted_capture_ref": (
                        f"source_capture:{row['accepted_capture_id']}"
                        if row["accepted_capture_id"] is not None
                        else None
                    ),
                    "promotion_policy_version": row["accepted_policy_version"],
                    "capture_contract_version": row[
                        "accepted_capture_contract_version"
                    ],
                },
            }
        )
    rich_content.sort(key=canonical_json)

    source_fields = {}
    for field in ("title", "location", "department", "expertise", "commitment"):
        source_fields[field] = unique_strings(
            [variant[field] for variant in variants if variant[field]]
        )

    return {
        "company": {
            "name": canonical["company_name"],
            "slug": canonical["company_slug"],
            "source_tier": canonical["source_tier"],
            "inventory_model": canonical["inventory_model"],
            "market_count_policy": canonical["market_count_policy"],
        },
        "canonical": {
            "canonical_key": canonical["canonical_key"],
            "canonical_title": canonical["canonical_title"],
            "normalized_title": canonical["normalized_title"],
            "source_category": canonical["source_category"],
            "language": clean(canonical["language"]),
            "language_locale": clean(canonical["language_locale"]),
        },
        "source_fields": source_fields,
        "variants": variants,
        "rich_content": rich_content,
    }


def semantic_input_for_version(semantic_input: dict, version: str) -> dict:
    """Project current source evidence through an explicit input contract."""

    if version == "opportunity_semantic_input_v3":
        projected = {
            field: copy.deepcopy(semantic_input[field])
            for field in HISTORICAL_SEMANTIC_INPUT_FIELDS_BY_VERSION[
                "opportunity_semantic_input_v3"
            ]
            if field in semantic_input
        }
        for item in projected.get("rich_content") or []:
            # An identical healthy recapture may advance the accepted capture row
            # without changing the semantic material. Keep that append-only capture
            # reference for provenance, but not in the enrichment identity.
            authority = item.get("authority")
            if type(authority) is dict:
                authority.pop("accepted_capture_ref", None)
        return projected
    if version == SEMANTIC_INPUT_VERSION:
        return copy.deepcopy(semantic_input)
    historical_fields = HISTORICAL_SEMANTIC_INPUT_FIELDS_BY_VERSION.get(version)
    if historical_fields is not None:
        projected = {
            field: copy.deepcopy(semantic_input[field])
            for field in historical_fields
            if field in semantic_input
        }
        legacy_variants = {}
        for item in semantic_input.get("variants") or []:
            legacy_item = {
                field: copy.deepcopy(item.get(field))
                for field in LEGACY_VARIANT_FIELDS
            }
            legacy_variants.setdefault(canonical_json(legacy_item), legacy_item)
        projected["variants"] = [
            legacy_variants[key] for key in sorted(legacy_variants)
        ]
        if "rich_content" in projected:
            projected["rich_content"] = [
                {
                    field: copy.deepcopy(item.get(field))
                    for field in LEGACY_RICH_CONTENT_FIELDS
                }
                for item in semantic_input.get("rich_content") or []
            ]
            projected["rich_content"].sort(key=canonical_json)
        return projected
    raise EnrichmentValidationError(f"Unsupported semantic input version: {version}")


def semantic_input_sha256(
    semantic_input: dict,
    version: str = SEMANTIC_INPUT_VERSION,
) -> str:
    projected = semantic_input_for_version(semantic_input, version)
    return hashlib.sha256(canonical_json(projected).encode("utf-8")).hexdigest()


def derivation_recipe_fingerprint(
    *,
    semantic_input_version: str = SEMANTIC_INPUT_VERSION,
    schema_version: str = SCHEMA_VERSION,
    taxonomy_version: str = TAXONOMY_VERSION,
    extractor_version: str = EXTRACTOR_VERSION,
    model_provider: str | None = None,
    model_name: str | None = None,
    prompt_version: str | None = None,
    recipe_version: str = DERIVATION_RECIPE_VERSION,
    llm_acceptance_guards_version: str = LLM_ACCEPTANCE_GUARDS_VERSION,
) -> str:
    """Hash the recipe independently from the source payload it derives."""

    model_values = (model_provider, model_name, prompt_version)
    uses_llm = any(value is not None for value in model_values)
    llm_recipe = None
    if uses_llm:
        llm_recipe = {
            "provider": model_provider,
            "model": model_name,
            "prompt_version": prompt_version,
            "acceptance_guards_version": llm_acceptance_guards_version,
        }
    recipe = {
        "recipe_version": recipe_version,
        "semantic_input_version": semantic_input_version,
        "schema_version": schema_version,
        "taxonomy_version": taxonomy_version,
        "extractor_version": extractor_version,
        "llm": llm_recipe,
    }
    return hashlib.sha256(canonical_json(recipe).encode("utf-8")).hexdigest()


def _persisted_value(row, key):
    if row is None:
        return None
    if type(row) is dict:
        return row.get(key)
    try:
        if key not in row.keys():
            return None
        return row[key]
    except (AttributeError, IndexError, KeyError, TypeError):
        return None


def persisted_semantic_input_version(row) -> tuple[str | None, str]:
    """Return the stored or safely inferred input version and its evidence basis."""

    explicit = _persisted_value(row, "semantic_input_version")
    if explicit is not None:
        if type(explicit) is str and explicit:
            return explicit, "explicit"
        return None, "invalid_explicit"
    inferred = LEGACY_SEMANTIC_INPUT_VERSION_BY_EXTRACTOR.get(
        _persisted_value(row, "extractor_version")
    )
    if inferred is not None:
        return inferred, "legacy_extractor_mapping"
    return None, "unknown"


def classify_enrichment_freshness(semantic_input: dict, persisted) -> dict:
    """Attribute freshness to source input and derivation evidence separately."""

    current_input_sha256 = semantic_input_sha256(semantic_input)
    current_derivation_fingerprint = derivation_recipe_fingerprint()
    if persisted is None:
        return {
            "freshness": "missing",
            "stale_reasons": [STALE_REASON_MISSING_ENRICHMENT],
            "current_semantic_input_version": SEMANTIC_INPUT_VERSION,
            "stored_semantic_input_version": None,
            "semantic_input_version_basis": "missing",
            "current_input_sha256": current_input_sha256,
            "comparable_input_sha256": None,
            "stored_input_sha256": None,
            "source_input_status": "missing",
            "current_derivation_fingerprint": current_derivation_fingerprint,
            "stored_derivation_fingerprint": None,
            "derivation_status": "missing",
            "changed_derivation_components": [],
        }

    stored_input_version, input_version_basis = persisted_semantic_input_version(
        persisted
    )
    comparable_input_sha256 = None
    if (
        stored_input_version in HISTORICAL_SEMANTIC_INPUT_FIELDS_BY_VERSION
        or stored_input_version == SEMANTIC_INPUT_VERSION
    ):
        comparable_input_sha256 = semantic_input_sha256(
            semantic_input,
            stored_input_version,
        )
    stored_input_sha256 = _persisted_value(persisted, "input_sha256")
    if comparable_input_sha256 is None:
        source_input_status = "not_comparable"
    elif stored_input_sha256 == comparable_input_sha256:
        source_input_status = "current"
    else:
        source_input_status = "changed"

    changed_components = []
    for field, current_value in (
        ("semantic_input_version", SEMANTIC_INPUT_VERSION),
        ("schema_version", SCHEMA_VERSION),
        ("taxonomy_version", TAXONOMY_VERSION),
        ("extractor_version", EXTRACTOR_VERSION),
    ):
        stored_value = (
            stored_input_version
            if field == "semantic_input_version"
            else _persisted_value(persisted, field)
        )
        if stored_value != current_value:
            changed_components.append(field)

    stored_derivation_fingerprint = _persisted_value(
        persisted, "derivation_fingerprint"
    )
    model_values = tuple(
        _persisted_value(persisted, field)
        for field in ("model_provider", "model_name", "prompt_version")
    )
    has_model_derivation = any(value is not None for value in model_values)
    if has_model_derivation:
        from wahojobs.opportunity_llm import DEFAULT_MODEL, PROMPT_VERSION

        current_derivation_fingerprint = derivation_recipe_fingerprint(
            model_provider="openai",
            model_name=DEFAULT_MODEL,
            prompt_version=PROMPT_VERSION,
        )
    if changed_components:
        derivation_status = "changed"
    elif stored_derivation_fingerprint is not None:
        if stored_derivation_fingerprint == current_derivation_fingerprint:
            derivation_status = "current"
        else:
            derivation_status = "changed"
            changed_components.append("derivation_fingerprint")
    elif (
        input_version_basis == "legacy_extractor_mapping"
        and not has_model_derivation
        and LEGACY_DERIVATION_RECIPE_VERSION_BY_EXTRACTOR.get(
            _persisted_value(persisted, "extractor_version")
        )
        == DERIVATION_RECIPE_VERSION
    ):
        # The stored version tuple is sufficient to accept deterministic legacy
        # output, but there is deliberately no invented stored fingerprint.
        derivation_status = "legacy_compatible"
    else:
        derivation_status = "unknown"
        changed_components.append("derivation_fingerprint")

    stale_reasons = []
    if derivation_status not in {"current", "legacy_compatible"}:
        stale_reasons.append(STALE_REASON_DERIVATION_CONTRACT_CHANGED)
    if source_input_status == "changed":
        stale_reasons.append(STALE_REASON_SOURCE_INPUT_CHANGED)
    freshness = "current" if not stale_reasons else "stale"
    return {
        "freshness": freshness,
        "stale_reasons": stale_reasons,
        "current_semantic_input_version": SEMANTIC_INPUT_VERSION,
        "stored_semantic_input_version": stored_input_version,
        "semantic_input_version_basis": input_version_basis,
        "current_input_sha256": current_input_sha256,
        "comparable_input_sha256": comparable_input_sha256,
        "stored_input_sha256": stored_input_sha256,
        "source_input_status": source_input_status,
        "current_derivation_fingerprint": current_derivation_fingerprint,
        "stored_derivation_fingerprint": stored_derivation_fingerprint,
        "derivation_status": derivation_status,
        "changed_derivation_components": sorted(changed_components),
    }


def extract_deterministic_document(semantic_input: dict) -> dict:
    document = blank_document()
    company = semantic_input["company"]
    canonical = semantic_input["canonical"]
    variants = semantic_input["variants"]
    document["source"] = {
        "company_name": company["name"],
        "company_slug": company["slug"],
        "canonical_key": canonical["canonical_key"],
        "canonical_title": canonical["canonical_title"],
        "source_category": canonical["source_category"],
        "source_tier": company["source_tier"],
        "inventory_model": company["inventory_model"],
        "market_count_policy": company["market_count_policy"],
        "opportunity_kinds": unique_strings(
            variant["opportunity_kind"] for variant in variants
        ),
        "availability_bases": unique_strings(
            variant["availability_basis"] for variant in variants
        ),
        "include_in_live_market_estimate": common_boolean(
            variant["include_in_live_market_estimate"] for variant in variants
        ),
    }
    attributes = document["attributes"]
    evidence = document["field_evidence"]
    fields = semantic_input["source_fields"]
    all_variant_refs = semantic_variant_refs(semantic_input)

    titles = unique_strings(
        [canonical["canonical_title"], *fields["title"]]
    )
    categories = unique_strings(
        [canonical["source_category"], *fields["department"], *fields["expertise"]]
    )
    title_text = " | ".join(titles)
    category_text = " | ".join(categories)
    role_text = normalize_text(f"{title_text} {category_text}")

    role_family = classify_role_family(role_text)
    if role_family:
        attributes["role"]["role_family"] = role_family
        add_evidence(evidence, "attributes.role.role_family", "title", title_text or category_text, "deterministic_classification", "medium", variant_refs=all_variant_refs)

    domain_row = {
        "title": title_text,
        "canonical_title": canonical["canonical_title"],
        "expertise": " | ".join(fields["expertise"]),
        "department": " | ".join(fields["department"]),
        "source_category": canonical["source_category"],
    }
    domains = sorted(detect_role_domains(domain_row) & PROFESSIONAL_DOMAINS)
    if domains:
        attributes["role"]["professional_domains"] = domains
        add_evidence(evidence, "attributes.role.professional_domains", "role_text", f"{title_text} | {category_text}".strip(" |"), "deterministic_classification", "medium", variant_refs=all_variant_refs)

    # Category labels can contain incidental capabilities (for example, a coding
    # role grouped under "Creator (Writer)"). A title is a sufficiently direct
    # deterministic signal; richer activity classification belongs to the
    # evidence-grounded semantic pass.
    normalized_title = normalize_text(title_text)
    activities = classify_many(normalized_title, WORK_ACTIVITY_RULES)
    if (
        "research_analysis" in activities
        and re.search(r"\bresearch (?:study|participant|project)\b", normalized_title)
        and re.search(
            r"\b(?:analys(?:is|t)|researcher|conduct research)\b",
            normalized_title,
        )
        is None
    ):
        activities.remove("research_analysis")
    if activities:
        attributes["role"]["work_activities"] = activities
        add_evidence(evidence, "attributes.role.work_activities", "role_text", f"{title_text} | {category_text}".strip(" |"), "deterministic_classification", "medium", variant_refs=all_variant_refs)

    specialization_groups = specialization_requirements(title_text)
    specializations = sorted(
        {
            concept
            for group in specialization_groups
            for concept in group["concepts"]
        }
    )
    if specializations:
        attributes["role"]["specializations"] = specializations
        add_evidence(evidence, "attributes.role.specializations", "title", title_text, "deterministic_parse", "high", variant_refs=all_variant_refs)

    seniority = parse_seniority(role_text)
    if seniority != "unknown":
        attributes["role"]["seniority"] = seniority
        add_evidence(evidence, "attributes.role.seniority", "title", title_text, "deterministic_parse", "high", variant_refs=all_variant_refs)

    objective_facts = extract_deterministic_objective_facts(semantic_input)
    project_variant_facts(
        document,
        objective_facts,
        all_variant_refs,
        preserve_unprojected=False,
    )
    extract_application(
        attributes["application"], document["field_evidence"], semantic_input
    )

    refresh_unknown_fields(document)
    document["field_evidence"] = sorted(
        document["field_evidence"],
        key=lambda item: (
            item["field_path"],
            item["source_ref"],
            item["evidence_text"],
        ),
    )
    validate_enrichment_document(document)
    return document


def classify_role_family(text: str) -> str | None:
    for family, terms in ROLE_FAMILY_RULES:
        if any(contains_term(text, term) for term in terms):
            return family
    return None


def classify_many(text: str, rules) -> list[str]:
    return sorted(
        value
        for value, terms in rules
        if any(contains_term(text, term) for term in terms)
    )


def parse_seniority(text: str) -> str:
    rules = (
        ("principal", ("principal",)),
        ("lead", (" lead ", "team lead", "tech lead")),
        ("senior", ("senior", "sr.")),
        ("manager", ("manager", "management")),
        ("internship", ("internship", "intern")),
        ("entry", ("entry level", "entry-level", "junior", "early career")),
        ("mid", ("mid level", "mid-level")),
    )
    padded = f" {text} "
    for value, terms in rules:
        if any(contains_term(padded, term) for term in terms):
            return value
    return "unknown"


def extract_application(target: dict, evidence: list[dict], semantic_input: dict) -> None:
    urls = sorted(
        {
            variant["url"]
            for variant in semantic_input["variants"]
            if variant["url"]
        }
    )
    if urls:
        target["application_url"] = urls[0]
        add_evidence(evidence, "attributes.application.application_url", "url", urls[0], "source_explicit", "high")
    bases = {
        variant["availability_basis"]
        for variant in semantic_input["variants"]
        if variant["availability_basis"]
    }
    if "login_gated_after_apply" in bases:
        target["login_required"] = True
        add_evidence(evidence, "attributes.application.login_required", "availability_basis", "login_gated_after_apply", "source_explicit", "high")


class _SourceHTMLTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        if tag in {"script", "style", "noscript"}:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.casefold()
        if tag in {"script", "style", "noscript"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif not self.ignored_depth and tag in {
            "p",
            "li",
            "div",
            "section",
            "article",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored_depth and data.strip():
            self.parts.append(data)


def source_body_text(body, body_format) -> str:
    if not body:
        return ""
    if body_format != "text/html":
        return clean(body)
    parser = _SourceHTMLTextParser()
    try:
        parser.feed(html.unescape(str(body)))
        parser.close()
    except (ValueError, TypeError):
        return clean(html.unescape(re.sub(r"<[^>]+>", " ", str(body))))
    return clean(" ".join(parser.parts))


def source_body_paragraphs(body, body_format) -> list[str]:
    if not body:
        return []
    if body_format == "text/html":
        parser = _SourceHTMLTextParser()
        try:
            parser.feed(html.unescape(str(body)))
            parser.close()
            raw = "".join(parser.parts)
        except (ValueError, TypeError):
            raw = re.sub(
                r"(?i)</?(?:p|li|div|section|article|h[1-6]|br)[^>]*>",
                "\n",
                html.unescape(str(body)),
            )
            raw = re.sub(r"<[^>]+>", " ", raw)
        candidates = re.split(r"\n+", raw)
    else:
        candidates = re.split(r"(?:\r?\n\s*){2,}", str(body))
    return [paragraph for paragraph in (clean(item) for item in candidates) if paragraph]


_COMPENSATION_RANGE_PATTERN = re.compile(
    r"(?P<currency>usd|cad|aud|eur|gbp|us\$|c\$|a\$|[$€£])\s*"
    r"(?P<minimum>\d{1,7}(?:[.,]\d{1,2})?)\s*"
    r"(?:(?:[-‐‑‒–—−]\s*)?(?:to|[-‐‑‒–—−])\s*[-‐‑‒–—−]?\s*)"
    r"(?:(?P<currency_2>usd|cad|aud|eur|gbp|us\$|c\$|a\$|[$€£])\s*)?"
    r"(?P<maximum>\d{1,7}(?:[.,]\d{1,2})?)\s*"
    r"(?:(?:per\s+|/\s*)?"
    r"(?P<period>hour|hours|hr|hrs|hourly|day|daily|week|weekly|month|monthly|year|yearly|annual(?:ly)?|project|asset|source word))",
    re.I,
)
_COMPENSATION_EXACT_PATTERN = re.compile(
    r"(?P<currency>usd|cad|aud|eur|gbp|us\$|c\$|a\$|[$€£])\s*"
    r"(?P<amount>\d{1,7}(?:[.,]\d{1,2})?)\s*"
    r"(?:(?:per\s+|/\s*)?"
    r"(?P<period>hour|hours|hr|hrs|hourly|day|daily|week|weekly|month|monthly|year|yearly|annual(?:ly)?|project|asset|source word))",
    re.I,
)
_LABELED_HOURLY_RATE_PATTERN = re.compile(
    r"\bhourly\s+(?:pay\s+)?rate\s*:\s*"
    r"(?P<currency>usd|cad|aud|eur|gbp|us\$|c\$|a\$|[$€£])\s*"
    r"(?P<amount>\d{1,7}(?:[.,]\d{1,2})?)\b",
    re.I,
)
_WEEKLY_HOURS_PATTERN = re.compile(
    r"(?<!\d)(?P<minimum>\d{1,3})"
    r"(?:\s*[-‐‑‒–—−]\s*(?P<maximum>\d{1,3}))?\s*"
    r"(?:hours?|hrs?)\s*(?:per|/)\s*week\b",
    re.I,
)
_LABELED_WEEKLY_HOURS_PATTERN = re.compile(
    r"(?<!\d)(?P<minimum>\d{1,3})"
    r"(?:\s*[-‐‑‒–—−]\s*(?P<maximum>\d{1,3}))?\s*(?:per|/)\s*week\b",
    re.I,
)
_FIXED_TIME_WINDOW_PATTERN = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*[-‐‑‒–—−]\s*"
    r"\d{1,2}(?::\d{2})?\s*(?:am|pm)\b",
    re.I,
)


def semantic_variant_refs(semantic_input: dict) -> list[str]:
    return sorted(
        {
            clean(item.get("variant_ref"))
            for item in semantic_input.get("variants") or []
            if clean(item.get("variant_ref"))
        }
    )


def _body_authority_refs(source: dict) -> list[str]:
    authority = source.get("authority") or {}
    values = [clean(authority.get("accepted_capture_ref"))]
    material_hash = clean(source.get("material_content_sha256"))
    if material_hash:
        values.append(f"source_content:{material_hash}")
    return sorted({value for value in values if value})


def _listing_authority_refs(source: dict) -> list[str]:
    authority = source.get("authority") or {}
    values = [clean(authority.get("accepted_capture_ref"))]
    semantic_hash = clean(source.get("semantic_material_sha256"))
    if semantic_hash:
        values.append(f"semantic_source:{semantic_hash}")
    return sorted({value for value in values if value})


def scoped_fact_evidence(
    *,
    source_ref: str,
    kind: str,
    label: str,
    evidence_text: str,
    basis: str,
    confidence: str,
    authority_refs=(),
) -> dict:
    evidence_text = clean(evidence_text)
    return {
        "evidence_block_id": evidence_block_id(
            source_ref,
            kind,
            label,
            evidence_text,
        ),
        "source_refs": sorted({clean(source_ref)}),
        "authority_refs": sorted(
            {clean(value) for value in authority_refs if clean(value)}
        ),
        "evidence_text": evidence_text,
        "basis": basis,
        "confidence": confidence,
    }


def make_variant_fact(
    field_path: str,
    value,
    variant_refs,
    evidence,
    *,
    knowledge_state: str = "known_value",
) -> dict:
    return {
        "field_path": field_path,
        "value": copy.deepcopy(value),
        "knowledge_state": knowledge_state,
        "variant_refs": sorted(
            {clean(value) for value in variant_refs if clean(value)}
        ),
        "evidence": copy.deepcopy(evidence),
    }


def _fact_identity(fact: dict) -> str:
    return canonical_json(
        {
            "field_path": fact["field_path"],
            "value": fact["value"],
            "knowledge_state": fact["knowledge_state"],
        }
    )


def normalize_variant_facts(facts) -> list[dict]:
    normalized = {}
    for raw in facts or []:
        fact = copy.deepcopy(raw)
        fact["variant_refs"] = sorted(set(fact.get("variant_refs") or []))
        evidence_by_id = {}
        for item in fact.get("evidence") or []:
            evidence_item = copy.deepcopy(item)
            evidence_item["source_refs"] = sorted(
                set(evidence_item.get("source_refs") or [])
            )
            evidence_item["authority_refs"] = sorted(
                set(evidence_item.get("authority_refs") or [])
            )
            identity = evidence_item.get("evidence_block_id")
            evidence_by_id.setdefault(identity, evidence_item)
        fact["evidence"] = [
            evidence_by_id[key]
            for key in sorted(evidence_by_id, key=lambda item: str(item))
        ]
        identity = _fact_identity(fact)
        existing = normalized.get(identity)
        if existing is None:
            normalized[identity] = fact
            continue
        existing["variant_refs"] = sorted(
            set([*existing["variant_refs"], *fact["variant_refs"]])
        )
        by_evidence = {
            item["evidence_block_id"]: item for item in existing["evidence"]
        }
        for item in fact["evidence"]:
            prior = by_evidence.get(item["evidence_block_id"])
            if prior is None:
                by_evidence[item["evidence_block_id"]] = item
                continue
            prior["source_refs"] = sorted(
                set([*prior["source_refs"], *item["source_refs"]])
            )
            prior["authority_refs"] = sorted(
                set([*prior["authority_refs"], *item["authority_refs"]])
            )
        existing["evidence"] = [by_evidence[key] for key in sorted(by_evidence)]
    return sorted(
        normalized.values(),
        key=lambda item: (
            item["field_path"],
            canonical_json(item["value"]),
            item["knowledge_state"],
            canonical_json(item["variant_refs"]),
        ),
    )


def _projected_list(values, field_path):
    values_by_identity = {canonical_json(value): copy.deepcopy(value) for value in values}
    result = [values_by_identity[key] for key in sorted(values_by_identity)]
    if field_path == "attributes.requirements.languages":
        return sorted(
            result,
            key=lambda item: (
                normalize_text(item.get("language")),
                normalize_text(item.get("locale")),
            ),
        )
    if all(type(item) is str for item in result):
        return sorted(result, key=str.casefold)
    return result


def _resolve_language_requirement_values(values) -> list[dict]:
    """Collapse per-language mode evidence without using extractor origin as authority.

    ``single`` and ``all_required`` are equivalent when exactly one language is
    present.  With multiple languages, ``all_required`` is safe only when every
    language has direct all-required evidence. Other mixtures retain the language
    identity but expose an ambiguous mode instead of producing duplicate or
    internally contradictory language entries.
    """

    by_language = defaultdict(lambda: defaultdict(list))
    for value in values:
        if type(value) is not dict:
            continue
        language = normalize_text(value.get("language"))
        locale = normalize_text(value.get("locale")) or None
        if not language:
            continue
        by_language[language][locale].append(copy.deepcopy(value))
    if not by_language:
        return []

    # An absent locale is a less-specific statement about the same language,
    # not a second language requirement. When exactly one specific locale is
    # supported, the combined fact can safely retain that specificity. Multiple
    # distinct locales remain separate because the contract cannot infer whether
    # they are alternatives or a conjunction.
    by_identity = {}
    for language, by_locale in by_language.items():
        specific_locales = sorted(locale for locale in by_locale if locale is not None)
        if len(specific_locales) <= 1:
            locale = specific_locales[0] if specific_locales else None
            language_values = list(by_locale.get(None, []))
            if locale is not None:
                language_values.extend(by_locale.get(locale, []))
            by_identity[(language, locale)] = language_values
            continue
        if by_locale.get(None):
            by_identity[(language, None)] = list(by_locale[None])
        for locale in specific_locales:
            by_identity[(language, locale)] = list(by_locale[locale])

    modes_by_language = {
        identity: {
            value.get("requirement_mode")
            for value in language_values
            if value.get("requirement_mode") in LANGUAGE_REQUIREMENT_MODES
        }
        for identity, language_values in by_identity.items()
    }
    language_count = len(by_identity)
    all_required_is_coherent = language_count > 1 and all(
        "all_required" in modes
        and modes <= {"single", "all_required"}
        for modes in modes_by_language.values()
    )
    any_supported_is_coherent = language_count > 1 and all(
        modes == {"any_supported"} for modes in modes_by_language.values()
    )

    resolved = []
    for identity in sorted(by_identity, key=lambda item: (item[0], item[1] or "")):
        language, locale = identity
        modes = modes_by_language[identity]
        if language_count == 1 and modes and modes <= {"single", "all_required"}:
            mode = "single"
        elif all_required_is_coherent:
            mode = "all_required"
        elif any_supported_is_coherent:
            mode = "any_supported"
        elif len(modes) == 1 and language_count == 1:
            mode = next(iter(modes))
        else:
            mode = "ambiguous"
        resolved.append(
            {
                "language": language,
                "locale": locale,
                "requirement_mode": mode,
            }
        )
    return resolved


def _language_fact_supports_projection(fact_value: dict, projected_value: dict) -> bool:
    fact_language = normalize_text(fact_value.get("language"))
    projected_language = normalize_text(projected_value.get("language"))
    if not fact_language or fact_language != projected_language:
        return False
    fact_locale = normalize_text(fact_value.get("locale")) or None
    projected_locale = normalize_text(projected_value.get("locale")) or None
    if projected_locale is None:
        return fact_locale is None
    return fact_locale in {None, projected_locale}


def project_variant_facts(
    document: dict,
    facts,
    all_variant_refs,
    *,
    preserve_unprojected=True,
) -> set[str]:
    """Recompute touched canonical fields from variant-complete, conflict-free facts.

    A prior canonical projection is not an authority tie-breaker. If newly combined
    deterministic and semantic facts disagree, the canonical value returns to unknown
    while each scoped fact remains available for review and later resolution.
    """

    all_variant_refs = tuple(sorted(set(all_variant_refs)))
    facts = normalize_variant_facts(facts)
    document["variant_facts"] = normalize_variant_facts(
        [*(document.get("variant_facts") or []), *facts]
    )
    by_path = defaultdict(list)
    for fact in document["variant_facts"]:
        by_path[fact["field_path"]].append(fact)

    projected_paths = set()
    for field_path in sorted({fact["field_path"] for fact in facts}):
        path_facts = by_path[field_path]
        set_path(document, field_path, copy.deepcopy(FIELD_DEFAULTS[field_path]))
        document["field_evidence"] = [
            item
            for item in document["field_evidence"]
            if item["field_path"] != field_path
        ]
        per_variant = {
            variant_ref: {
                "known": False,
                "known_empty": False,
                "values": {},
            }
            for variant_ref in all_variant_refs
        }
        for fact in path_facts:
            for variant_ref in fact["variant_refs"]:
                if variant_ref not in per_variant:
                    continue
                per_variant[variant_ref]["known"] = True
                if fact["knowledge_state"] == "known_empty":
                    per_variant[variant_ref]["known_empty"] = True
                else:
                    per_variant[variant_ref]["values"].setdefault(
                        canonical_json(fact["value"]),
                        copy.deepcopy(fact["value"]),
                    )
        if not per_variant or not all(item["known"] for item in per_variant.values()):
            continue
        if any(
            item["known_empty"] and item["values"]
            for item in per_variant.values()
        ):
            continue
        if field_path == "attributes.requirements.languages":
            for item in per_variant.values():
                if item["known_empty"]:
                    continue
                resolved = _resolve_language_requirement_values(
                    item["values"].values()
                )
                item["values"] = {
                    canonical_json(value): value for value in resolved
                }
        signatures = {
            (
                ("known_empty",)
                if item["known_empty"]
                else ("known_value", *sorted(item["values"]))
            )
            for item in per_variant.values()
        }
        if len(signatures) != 1:
            continue
        signature = next(iter(signatures))
        value_keys = signature[1:]
        if field_path in ATOMIC_LIST_FIELD_PATHS:
            first = next(iter(per_variant.values()))
            projected_value = _projected_list(
                [first["values"][key] for key in value_keys],
                field_path,
            )
        else:
            if len(value_keys) != 1:
                continue
            first = next(iter(per_variant.values()))
            projected_value = copy.deepcopy(first["values"][value_keys[0]])

        set_path(document, field_path, projected_value)
        document["field_evidence"] = [
            item
            for item in document["field_evidence"]
            if item["field_path"] != field_path
        ]
        matching_keys = set(value_keys)
        matching_language_values = []
        if field_path == "attributes.requirements.languages":
            matching_language_values = [
                value for value in projected_value if type(value) is dict
            ]
        for fact in path_facts:
            if fact["knowledge_state"] == "known_value":
                if field_path == "attributes.requirements.languages":
                    fact_value = fact["value"]
                    if not any(
                        _language_fact_supports_projection(fact_value, projected)
                        for projected in matching_language_values
                    ):
                        continue
                elif canonical_json(fact["value"]) not in matching_keys:
                    continue
            for evidence in fact["evidence"]:
                for source_ref in evidence["source_refs"]:
                    add_evidence(
                        document["field_evidence"],
                        field_path,
                        source_ref,
                        evidence["evidence_text"],
                        evidence["basis"],
                        evidence["confidence"],
                        evidence_block_ref=evidence["evidence_block_id"],
                        variant_refs=fact["variant_refs"],
                        authority_refs=evidence["authority_refs"],
                    )
        projected_paths.add(field_path)
    return projected_paths


def _objective_fact(
    field_path,
    value,
    variant_ref,
    source_ref,
    evidence_text,
    *,
    kind,
    label,
    authority_refs=(),
    basis="deterministic_parse",
):
    return make_variant_fact(
        field_path,
        value,
        [variant_ref],
        [
            scoped_fact_evidence(
                source_ref=source_ref,
                kind=kind,
                label=label,
                evidence_text=evidence_text,
                basis=basis,
                confidence="high",
                authority_refs=authority_refs,
            )
        ],
    )


def _compensation_currency_from_marker(marker):
    marker = normalize_text(marker)
    return {
        "usd": "USD",
        "us$": "USD",
        "cad": "CAD",
        "c$": "CAD",
        "aud": "AUD",
        "a$": "AUD",
        "eur": "EUR",
        "€": "EUR",
        "gbp": "GBP",
        "£": "GBP",
    }.get(marker)


def _compensation_period_from_marker(marker):
    marker = normalize_text(marker)
    return {
        "hour": "hour",
        "hours": "hour",
        "hr": "hour",
        "hrs": "hour",
        "hourly": "hour",
        "day": "day",
        "daily": "day",
        "week": "week",
        "weekly": "week",
        "month": "month",
        "monthly": "month",
        "year": "year",
        "yearly": "year",
        "annual": "year",
        "annually": "year",
        "project": "project",
        "asset": "asset",
        "source word": "source_word",
    }.get(marker)


def parse_explicit_compensation(text: str) -> dict | None:
    text = clean(text)
    match = _COMPENSATION_RANGE_PATTERN.search(text)
    if match is not None:
        minimum = parse_decimal(match.group("minimum"))
        maximum = parse_decimal(match.group("maximum"))
        if minimum is None or maximum is None or minimum > maximum:
            return None
        return {
            "disclosed": True,
            "currency": _compensation_currency_from_marker(
                match.group("currency_2") or match.group("currency")
            ),
            "amount_min": minimum,
            "amount_max": maximum,
            "period": _compensation_period_from_marker(match.group("period")),
            "amount_type": "range",
            "notes": text,
        }
    match = _COMPENSATION_EXACT_PATTERN.search(text)
    if match is None:
        labeled = _LABELED_HOURLY_RATE_PATTERN.search(text)
        if labeled is not None:
            amount = parse_decimal(labeled.group("amount"))
            if amount is None:
                return None
            return {
                "disclosed": True,
                "currency": _compensation_currency_from_marker(
                    labeled.group("currency")
                ),
                "amount_min": amount,
                "amount_max": amount,
                "period": "hour",
                "amount_type": "exact",
                "notes": text,
            }
    if match is None:
        return None
    amount = parse_decimal(match.group("amount"))
    if amount is None:
        return None
    return {
        "disclosed": True,
        "currency": _compensation_currency_from_marker(match.group("currency")),
        "amount_min": amount,
        "amount_max": amount,
        "period": _compensation_period_from_marker(match.group("period")),
        "amount_type": "exact",
        "notes": text,
    }


def parse_explicit_weekly_hours(text: str, *, hours_heading=False):
    match = _WEEKLY_HOURS_PATTERN.search(text)
    if match is None and hours_heading:
        match = _LABELED_WEEKLY_HOURS_PATTERN.search(text)
    if match is None:
        return None
    minimum = int(match.group("minimum"))
    maximum = int(match.group("maximum") or minimum)
    if not 0 < minimum <= maximum <= 168:
        return None
    return minimum, maximum


def education_levels_in_text(text: str) -> list[str]:
    normalized = normalize_text(text).replace("’", "'")
    levels = []
    for level, pattern in (
        ("doctorate", r"\b(?:doctorate|doctoral|ph\.?d\.?)\b"),
        ("master", r"\bmaster(?:'s|s)?\b"),
        ("bachelor", r"\bbachelor(?:'s|s)?\b"),
        ("associate", r"\bassociate(?:'s|s)?\b"),
        ("secondary", r"\b(?:high school|secondary school)\b"),
    ):
        if re.search(pattern, normalized):
            levels.append(level)
    order = ["secondary", "associate", "bachelor", "master", "doctorate"]
    return sorted(set(levels), key=order.index)


def parse_required_education_level(text: str) -> str | None:
    normalized = normalize_text(text).replace("’", "'")
    if re.search(r"\b(?:no|without a) (?:college |university )?degree (?:is )?required\b", normalized):
        return "no_degree"
    if re.search(r"\bdegree\b.{0,24}\b(?:not required|optional|preferred|ideal)\b", normalized):
        return None
    if re.search(r"\b(?:preferred|ideal|a plus|helpful)\b.{0,60}\bdegree\b", normalized):
        return None
    required = re.search(
        r"(?:\b(?:minimum|required|requires?|must have|need(?:s)? to have)\b.{0,80}"
        r"\b(?:degree|diploma)\b|\b(?:degree|diploma)\b.{0,50}"
        r"\b(?:required|minimum|must)\b)",
        normalized,
    )
    if required is None:
        return None
    levels = education_levels_in_text(normalized)
    return levels[0] if levels else None


def parse_preferred_education_levels(text: str) -> list[str]:
    normalized = normalize_text(text).replace("’", "'")
    levels = education_levels_in_text(normalized)
    if not levels:
        return []
    education_anchor = (
        r"(?:degree|diploma|doctorate|doctoral|ph\.?d\.?|master(?:'s|s)?|"
        r"bachelor(?:'s|s)?|associate(?:'s|s)?|high school|secondary school)"
    )
    preference = r"(?:preferred|ideal|strong signal|not required)"
    if not (
        re.search(
            rf"\b{education_anchor}\b[^.;]{{0,140}}\b{preference}\b",
            normalized,
        )
        or re.search(
            rf"\b{preference}\b[^.;]{{0,140}}\b{education_anchor}\b",
            normalized,
        )
    ):
        return []
    return levels


def parse_required_years_experience(text: str):
    normalized = normalize_text(text)
    if re.search(r"\b(?:no prior|no previous)\b.{0,30}\bexperience\b.{0,20}\b(?:necessary|required|needed)\b", normalized):
        return 0
    if re.search(r"\b(?:preferred|a plus|helpful|not required|ideal|valuable)\b", normalized):
        return None
    match = re.search(
        r"\b(?:minimum(?: of)?|at least)\s*(\d{1,2})\+?\s*years?\b"
        r".{0,80}\bexperience\b",
        normalized,
    )
    if match is None:
        match = re.search(
            r"\b(\d{1,2})(?:\s*[-–—]\s*\d{1,2}|\+)?\s*years?\b"
            r".{0,50}\b(?:of )?experience\b",
            normalized,
        )
    if match is None:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= 80 else None


def extract_deterministic_objective_facts(semantic_input: dict) -> list[dict]:
    facts = []
    canonical = semantic_input["canonical"]
    sources_by_variant_ref = {
        item.get("variant_ref"): item
        for item in semantic_input.get("rich_content") or []
        if item.get("variant_ref")
    }

    for variant in semantic_input.get("variants") or []:
        variant_ref = variant.get("variant_ref")
        if not variant_ref:
            continue
        listing_authority_refs = _listing_authority_refs(
            sources_by_variant_ref.get(variant_ref) or {}
        )
        location = clean(variant.get("location"))
        if location:
            scope, remote_status, requirements, _restriction_type = classify_job_location(
                location
            )
            evidence_args = (variant_ref, variant_ref, location)
            workplace_mode = {
                "remote": "remote",
                "hybrid": "hybrid",
                "onsite": "onsite",
            }.get(remote_status)
            if workplace_mode:
                facts.append(
                    _objective_fact(
                        "attributes.work_arrangement.workplace_mode",
                        workplace_mode,
                        *evidence_args,
                        kind="listing_field",
                        label="listing.location",
                        authority_refs=listing_authority_refs,
                    )
                )
            if (
                remote_status in {"remote", "hybrid", "onsite"}
                and scope in LOCATION_SCOPES
                and scope != "unknown"
            ):
                facts.append(
                    _objective_fact(
                        "attributes.work_arrangement.location_scope",
                        scope,
                        *evidence_args,
                        kind="listing_field",
                        label="listing.location",
                        authority_refs=listing_authority_refs,
                    )
                )
            for country in sorted(countries_in_location(location)):
                facts.append(
                    _objective_fact(
                        "attributes.work_arrangement.eligible_countries",
                        country,
                        *evidence_args,
                        kind="listing_field",
                        label="listing.location",
                        authority_refs=listing_authority_refs,
                    )
                )
            for region in sorted(regions_in_location(location)):
                facts.append(
                    _objective_fact(
                        "attributes.work_arrangement.eligible_regions",
                        region,
                        *evidence_args,
                        kind="listing_field",
                        label="listing.location",
                        authority_refs=listing_authority_refs,
                    )
                )
            if requirements:
                facts.append(
                    _objective_fact(
                        "attributes.work_arrangement.eligible_locations",
                        requirements,
                        *evidence_args,
                        kind="listing_field",
                        label="listing.location",
                        authority_refs=listing_authority_refs,
                        basis="source_explicit",
                    )
                )

        language_text = " ".join(
            value
            for value in (
                clean(variant.get("title")),
                clean(canonical.get("language")),
                clean(canonical.get("language_locale")),
            )
            if value
        )
        mentions = find_language_mentions(language_text)
        languages = {mention["language"] for mention in mentions}
        canonical_language = normalize_language_name(canonical.get("language"))
        if canonical_language:
            languages.add(canonical_language)
        if languages:
            mode = requirement_mode_for_mentions(language_text, mentions)
            if len(languages) == 1 and mode == "none":
                mode = "single"
            locale = clean(canonical.get("language_locale")) or None
            for language in sorted(languages):
                facts.append(
                    _objective_fact(
                        "attributes.requirements.languages",
                        {
                            "language": language,
                            "locale": (
                                locale if language == canonical_language else None
                            ),
                            "requirement_mode": mode,
                        },
                        variant_ref,
                        variant_ref,
                        language_text,
                        kind="listing_field",
                        label="listing.title_language",
                        authority_refs=listing_authority_refs,
                    )
                )

        commitment = clean(variant.get("commitment"))
        if commitment:
            engagement = _engagement_value(commitment)
            if engagement:
                facts.append(
                    _objective_fact(
                        "attributes.work_arrangement.engagement_type",
                        engagement,
                        variant_ref,
                        variant_ref,
                        commitment,
                        kind="listing_field",
                        label="listing.commitment",
                        authority_refs=listing_authority_refs,
                    )
                )
            compensation = parse_explicit_compensation(commitment)
            if compensation:
                facts.extend(
                    _compensation_facts(
                        compensation,
                        variant_ref,
                        variant_ref,
                        kind="listing_field",
                        label="listing.commitment",
                        authority_refs=listing_authority_refs,
                    )
                )
            hours = parse_explicit_weekly_hours(commitment)
            if hours:
                facts.extend(
                    _hours_facts(
                        hours,
                        variant_ref,
                        variant_ref,
                        commitment,
                        kind="listing_field",
                        label="listing.commitment",
                        authority_refs=listing_authority_refs,
                    )
                )

    for source in semantic_input.get("rich_content") or []:
        variant_ref = source.get("variant_ref") or source.get("source_ref")
        source_ref = source.get("source_ref") or variant_ref
        if not variant_ref or not source_ref:
            continue
        authority_refs = _body_authority_refs(source)
        paragraphs = source_body_paragraphs(
            source.get("body"), source.get("body_format")
        )
        previous_was_hours_heading = False
        for index, paragraph in enumerate(paragraphs, start=1):
            label = f"body paragraph {index}"
            normalized = normalize_text(paragraph)
            compensation = parse_explicit_compensation(paragraph)
            if compensation:
                facts.extend(
                    _compensation_facts(
                        compensation,
                        variant_ref,
                        source_ref,
                        kind="body_paragraph",
                        label=label,
                        authority_refs=authority_refs,
                    )
                )

            inline_hours_heading = normalized.startswith("hours:")
            hours = parse_explicit_weekly_hours(
                paragraph,
                hours_heading=previous_was_hours_heading or inline_hours_heading,
            )
            if hours:
                facts.extend(
                    _hours_facts(
                        hours,
                        variant_ref,
                        source_ref,
                        paragraph,
                        kind="body_paragraph",
                        label=label,
                        authority_refs=authority_refs,
                    )
                )

            schedule_type = None
            if _FIXED_TIME_WINDOW_PATTERN.search(paragraph) or re.search(
                r"\bmust (?:be able to )?work\b.{0,80}\b(?:time ?zone|hours?|schedule)\b",
                normalized,
            ):
                schedule_type = "fixed"
            elif re.search(r"\bflexible (?:hours?|schedule)\b", normalized):
                schedule_type = "flexible"
            if schedule_type:
                facts.append(
                    _objective_fact(
                        "attributes.work_arrangement.schedule_type",
                        schedule_type,
                        variant_ref,
                        source_ref,
                        paragraph,
                        kind="body_paragraph",
                        label=label,
                        authority_refs=authority_refs,
                    )
                )

            engagement_match = re.search(
                r"\bemployment type\s*:\s*([^|;,.]+)", paragraph, re.I
            )
            if engagement_match:
                engagement = _engagement_value(engagement_match.group(1))
                if engagement:
                    facts.append(
                        _objective_fact(
                            "attributes.work_arrangement.engagement_type",
                            engagement,
                            variant_ref,
                            source_ref,
                            paragraph,
                            kind="body_paragraph",
                            label=label,
                            authority_refs=authority_refs,
                        )
                    )
            workplace_match = re.search(
                r"\bworkplace type\s*:\s*(?P<value>[^|;.!?]{1,160})",
                paragraph,
                re.I,
            )
            if workplace_match:
                workplace_value = clean(workplace_match.group("value"))
                mode_match = re.search(
                    r"\b(remote|hybrid|on[- ]?site)\b",
                    workplace_value,
                    re.I,
                )
                workplace = None
                if mode_match:
                    workplace = re.sub(r"[- ]", "", mode_match.group(1).casefold())
                    workplace = "onsite" if workplace == "onsite" else workplace
                    facts.append(
                        _objective_fact(
                            "attributes.work_arrangement.workplace_mode",
                            workplace,
                            variant_ref,
                            source_ref,
                            paragraph,
                            kind="body_paragraph",
                            label=label,
                            authority_refs=authority_refs,
                        )
                    )
                scope, _remote, _requirements, _restriction = classify_job_location(
                    workplace_value
                )
                if workplace and scope in LOCATION_SCOPES and scope != "unknown":
                    facts.append(
                        _objective_fact(
                            "attributes.work_arrangement.location_scope",
                            scope,
                            variant_ref,
                            source_ref,
                            paragraph,
                            kind="body_paragraph",
                            label=label,
                            authority_refs=authority_refs,
                        )
                    )

            education = parse_required_education_level(paragraph)
            if education:
                facts.append(
                    _objective_fact(
                        "attributes.requirements.education.minimum_level",
                        education,
                        variant_ref,
                        source_ref,
                        paragraph,
                        kind="body_paragraph",
                        label=label,
                        authority_refs=authority_refs,
                    )
                )
            for preferred_education in parse_preferred_education_levels(
                paragraph
            ):
                facts.append(
                    _objective_fact(
                        "attributes.requirements.education.preferred_levels",
                        preferred_education,
                        variant_ref,
                        source_ref,
                        paragraph,
                        kind="body_paragraph",
                        label=label,
                        authority_refs=authority_refs,
                    )
                )
            experience = parse_required_years_experience(paragraph)
            if experience is not None:
                facts.append(
                    _objective_fact(
                        "attributes.requirements.years_experience_min",
                        experience,
                        variant_ref,
                        source_ref,
                        paragraph,
                        kind="body_paragraph",
                        label=label,
                        authority_refs=authority_refs,
                    )
                )
            previous_was_hours_heading = normalized.rstrip(":") == "hours"
    return normalize_variant_facts(facts)


def _engagement_value(text):
    normalized = normalize_text(text)
    for value, patterns in (
        ("internship", ("internship", "intern")),
        ("volunteer", ("volunteer",)),
        ("freelance", ("freelance", "independent contractor")),
        ("temporary", ("temporary",)),
        ("contract", ("contract", "project based", "project-based")),
        ("part_time", ("part time", "part-time")),
        ("full_time", ("full time", "full-time")),
    ):
        if any(contains_term(normalized, pattern) for pattern in patterns):
            return value
    return None


def _compensation_facts(
    compensation,
    variant_ref,
    source_ref,
    *,
    kind,
    label,
    authority_refs=(),
):
    facts = []
    for field, value in compensation.items():
        if value is None:
            continue
        facts.append(
            _objective_fact(
                f"attributes.compensation.{field}",
                value,
                variant_ref,
                source_ref,
                compensation["notes"],
                kind=kind,
                label=label,
                authority_refs=authority_refs,
            )
        )
    return facts


def _hours_facts(
    hours,
    variant_ref,
    source_ref,
    evidence_text,
    *,
    kind,
    label,
    authority_refs=(),
):
    return [
        _objective_fact(
            f"attributes.work_arrangement.hours_per_week_{suffix}",
            value,
            variant_ref,
            source_ref,
            evidence_text,
            kind=kind,
            label=label,
            authority_refs=authority_refs,
        )
        for suffix, value in zip(("min", "max"), hours)
    ]


def explicit_source_fields(value, path=()):
    if type(value) is dict:
        for key in sorted(value):
            yield from explicit_source_fields(value[key], (*path, str(key)))
        return
    if type(value) is list:
        for index, item in enumerate(value):
            yield from explicit_source_fields(item, (*path, str(index)))
        return
    if value is None:
        return
    rendered = clean(value) if type(value) is str else canonical_json(value)
    if rendered:
        yield ".".join(path), rendered


def evidence_block_id(source_ref, kind, label, content) -> str:
    identity = {
        "kind": kind,
        "content": content,
    }
    if kind != "body_paragraph":
        identity["label"] = label
    digest = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    return f"E{digest[:16]}"


def llm_source_packet(semantic_input: dict) -> tuple[dict, dict[str, dict]]:
    evidence_blocks = {}
    remaining = MAX_LLM_SOURCE_CHARACTERS

    def add_block(
        source_ref,
        variant_ref,
        kind,
        label,
        value,
        *,
        authority_refs=(),
    ):
        nonlocal remaining
        value = clean(value)
        if not value:
            return
        full_block_id = evidence_block_id(source_ref, kind, label, value)
        existing = evidence_blocks.get(full_block_id)
        if existing is not None:
            existing["source_refs"] = sorted(
                set([*existing["source_refs"], source_ref])
            )
            existing["variant_refs"] = sorted(
                set([*existing["variant_refs"], variant_ref])
            )
            existing["authority_refs"] = sorted(
                set([*existing["authority_refs"], *authority_refs])
            )
            return
        if remaining <= 0:
            return
        value = value[:remaining]
        if not value:
            return
        block_id = evidence_block_id(source_ref, kind, label, value)
        evidence_blocks.setdefault(
            block_id,
            {
                "evidence_block_id": block_id,
                "source_ref": source_ref,
                "source_refs": [source_ref],
                "variant_refs": [variant_ref],
                "authority_refs": sorted(set(authority_refs)),
                "kind": kind,
                "authority_class": {
                    "body_paragraph": "accepted_body_evidence",
                    "metadata_field": "page_metadata_context",
                    "listing_field": "variant_listing_evidence",
                }[kind],
                "label": label,
                "content": value,
            },
        )
        remaining -= len(value)

    for item in semantic_input.get("rich_content") or []:
        source_ref = item["source_ref"]
        variant_ref = item.get("variant_ref") or source_ref
        authority = item.get("authority") or {}
        authority_refs = [
            value
            for value in (
                clean(authority.get("accepted_capture_ref")),
                (
                    f"source_content:{item.get('material_content_sha256')}"
                    if clean(item.get("material_content_sha256"))
                    else None
                ),
            )
            if value
        ]
        for index, paragraph in enumerate(
            source_body_paragraphs(item.get("body"), item.get("body_format")),
            start=1,
        ):
            add_block(
                source_ref,
                variant_ref,
                "body_paragraph",
                f"body paragraph {index}",
                paragraph,
                authority_refs=authority_refs,
            )
        for path, value in explicit_source_fields(item.get("metadata") or {}):
            label = f"metadata.{path}"
            add_block(
                source_ref,
                variant_ref,
                "metadata_field",
                label,
                f"{label}: {value}",
                authority_refs=authority_refs,
            )

    sources_by_variant_ref = {
        item.get("variant_ref"): item
        for item in semantic_input.get("rich_content") or []
        if item.get("variant_ref")
    }
    for variant in semantic_input.get("variants") or []:
        variant_ref = variant.get("variant_ref") or "listing_fields"
        authority_refs = _listing_authority_refs(
            sources_by_variant_ref.get(variant_ref) or {}
        )
        for path, value in explicit_source_fields(variant):
            if path == "variant_ref":
                continue
            label = f"listing.{path}"
            add_block(
                variant_ref,
                variant_ref,
                "listing_field",
                label,
                f"{label}: {value}",
                authority_refs=authority_refs,
            )

    for block in evidence_blocks.values():
        block["source_refs"] = sorted(set(block["source_refs"]))
        block["variant_refs"] = sorted(set(block["variant_refs"]))
        block["authority_refs"] = sorted(set(block["authority_refs"]))

    packet = {
        "company": semantic_input["company"],
        "canonical": semantic_input["canonical"],
        "variants": semantic_input.get("variants") or [],
        "evidence_blocks": [evidence_blocks[key] for key in sorted(evidence_blocks)],
    }
    return packet, evidence_blocks


def has_sufficient_llm_source_content(semantic_input: dict) -> bool:
    body_characters = 0
    material_characters = 0
    seen_hashes = set()
    for item in semantic_input.get("rich_content") or []:
        material_hash = item.get("material_content_sha256")
        if material_hash in seen_hashes:
            continue
        seen_hashes.add(material_hash)
        body = source_body_text(item.get("body"), item.get("body_format"))
        metadata = canonical_json(item.get("metadata") or {})
        body_characters += len(body)
        material_characters += len(body) + (len(metadata) if metadata != "{}" else 0)
    return (
        body_characters >= MIN_LLM_BODY_CHARACTERS
        or material_characters >= MIN_LLM_MATERIAL_CHARACTERS
    )


def normalize_llm_list_fields(payload: dict, evidence_blocks: dict[str, dict]) -> dict:
    normalized = copy.deepcopy(payload)
    if type(payload) is not dict:
        return normalized

    def deduplicate_evidence(item):
        if type(item) is not dict or "evidence" not in item:
            return
        evidence = item["evidence"]
        if type(evidence) is not list:
            return
        unique = []
        seen = set()
        for block_id in evidence:
            if type(block_id) is not str or block_id not in seen:
                unique.append(block_id)
            if type(block_id) is str:
                seen.add(block_id)
        item["evidence"] = unique

    for field in LLM_SCALAR_FIELD_PATHS:
        if field in normalized:
            deduplicate_evidence(normalized[field])

    for field in (*LLM_LIST_FIELD_PATHS, "known_empty_fields"):
        if field not in payload:
            continue
        if type(normalized[field]) is list:
            for item in normalized[field]:
                deduplicate_evidence(item)
        if type(normalized[field]) is not list:
            continue
        items = []
        items_by_value = {}
        for item in normalized[field]:
            if type(item) is not dict:
                items.append(item)
                continue
            value = (
                item.get("field_path")
                if field == "known_empty_fields"
                else item.get("value")
            )
            value_key = (
                normalize_text(value)
                if type(value) is str
                else canonical_json(value)
            )
            existing = items_by_value.get(value_key)
            if existing is None:
                existing = copy.deepcopy(item)
                items_by_value[value_key] = existing
                items.append(existing)
                continue
            seen_evidence = set(existing.get("evidence") or [])
            for block_id in item.get("evidence") or []:
                if block_id not in seen_evidence:
                    existing["evidence"].append(block_id)
                    seen_evidence.add(block_id)
        normalized[field] = items
        if field == "known_empty_fields" and all(
            type(item) is dict and type(item.get("field_path")) is str
            for item in items
        ):
            normalized[field] = sorted(items, key=lambda item: item["field_path"])
        if field == "known_empty_fields":
            validate_known_empty_fields(normalized[field], evidence_blocks)
        else:
            allowed = {
                "professional_domains": PROFESSIONAL_DOMAINS,
                "work_activities": WORK_ACTIVITIES,
                "education_accepted_alternatives": EDUCATION_LEVELS - {"unknown"},
                "education_preferred_levels": EDUCATION_LEVELS - {"unknown"},
            }.get(field)
            validate_llm_list(
                normalized[field],
                f"llm_payload.{field}",
                evidence_blocks,
                allowed=allowed,
                value_kind="language" if field == "languages" else "text",
            )
    return normalized


def apply_llm_semantic_acceptance_guards(
    payload: dict,
    evidence_blocks: dict[str, dict],
    deterministic_document: dict,
) -> dict:
    guarded = copy.deepcopy(payload)
    if type(payload) is not dict:
        return guarded

    if llm_role_family_conflicts_with_substantive_work(
        guarded.get("role_family") or {},
        guarded["work_activities"],
        deterministic_document,
    ):
        guarded["role_family"] = {"value": None, "evidence": []}
        deterministic_document["attributes"]["role"]["role_family"] = None
        deterministic_document["field_evidence"] = [
            item
            for item in deterministic_document["field_evidence"]
            if item["field_path"] != "attributes.role.role_family"
        ]

    guarded["work_activities"] = guard_llm_work_activities(
        guarded.get("work_activities") or [], evidence_blocks
    )
    guarded["professional_domains"] = guard_llm_professional_domains(
        guarded.get("professional_domains") or [], evidence_blocks
    )
    guarded["languages"] = [
        item
        for item in guarded.get("languages") or []
        if llm_language_requirement_is_explicit(item, evidence_blocks)
    ]
    for field in (
        "workplace_mode",
        "location_scope",
        "eligible_countries",
        "eligible_regions",
        "eligible_locations",
    ):
        if field in {"workplace_mode", "location_scope"}:
            item = guarded.get(field) or {"value": None, "evidence": []}
            if item.get("value") is not None and not llm_location_fact_is_explicit(
                field, item, evidence_blocks
            ):
                guarded[field] = {"value": None, "evidence": []}
        else:
            guarded[field] = [
                item
                for item in guarded.get(field) or []
                if llm_location_fact_is_explicit(field, item, evidence_blocks)
            ]

    currency = guarded.get("compensation_currency") or {
        "value": None,
        "evidence": [],
    }
    if currency.get("value") is not None and not llm_currency_is_explicit(
        currency, evidence_blocks
    ):
        guarded["compensation_currency"] = {"value": None, "evidence": []}

    reclassify_requirement_modalities(guarded, evidence_blocks)
    ensure_required_role_domain_expertise(
        guarded, evidence_blocks, deterministic_document
    )
    move_current_status_facts_out_of_other_requirements(
        guarded, evidence_blocks
    )
    move_experience_facts_out_of_skills(guarded, evidence_blocks)
    normalize_experience_field_authority(guarded, evidence_blocks)
    move_mixed_experience_alternatives_to_capabilities(guarded)
    ensure_numeric_experience_has_qualitative_fact(guarded, evidence_blocks)

    required_languages = {
        normalize_text((item.get("value") or {}).get("language"))
        for item in guarded.get("languages") or []
    }

    responsibilities = [
        item["value"] for item in guarded.get("responsibilities") or []
    ]
    for field in ("skills_required", "skills_preferred"):
        guarded[field] = [
            item
            for item in guarded.get(field) or []
            if not skill_is_constraint(item["value"])
            and not skill_is_task_description(item["value"])
            and not skill_duplicates_responsibility(item["value"], responsibilities)
            and not skill_is_unqualified_metadata_keyword(item, evidence_blocks)
            and not skill_duplicates_language_requirement(
                item["value"], required_languages
            )
        ]

    guarded["caveats"] = [
        item
        for item in guarded.get("caveats") or []
        if caveat_is_candidate_warning(item["value"])
    ]
    for item in unusual_eligibility_caveats(evidence_blocks):
        if not any(
            caveats_materially_overlap(item, existing)
            for existing in guarded["caveats"]
        ):
            guarded["caveats"].append(item)
    return guarded


PREFERRED_MODALITY_PATTERN = re.compile(
    r"\b(?:a plus|beneficial|bonus|desirable|ideal|ideally|nice to have|not required|"
    r"preferred|preference|signal(?:s)? (?:a )?(?:good )?fit|strong indicators?|"
    r"strong signals?|strongly valued|highly valuable)\b",
    re.I,
)
REQUIRED_MODALITY_PATTERN = re.compile(
    r"\b(?:at least|essential|minimum(?: of)?|must|need(?:ed|s)?|required|requires?|"
    r"what matters(?: most)?|we(?: are|'re|’re)? looking for|we seek)\b",
    re.I,
)
EXPERIENCE_VALUE_PATTERN = re.compile(
    r"\b(?:background|career|experience|experienced|former|previously worked|"
    r"professional (?:background|history|practice)|work history|"
    r"prior (?:work|practice|employment|role)|tenure|track record|worked as|"
    r"years? (?:in|of|with))\b",
    re.I,
)
NON_EXPERIENCE_ALTERNATIVE_PATTERN = re.compile(
    r"\b(?:abilit(?:y|ies)|capabilit(?:y|ies)|curiosity|degree|education|"
    r"enthusiasm|familiarity|interest|involvement|knowledge|participation|"
    r"skill|training|willingness)\b",
    re.I,
)
CURRENT_STATUS_VALUE_PATTERN = re.compile(
    r"\b(?:co[- ]?founders?|co[- ]?owners?|directors?|employees?|founders?|licensees?|"
    r"managers?|members?|officers?|owners?|participants?|practitioners?|professionals?|"
    r"residents?|students?|workers?|co[- ]?own(?:s|ed|ing)?|own(?:s|ed|ing)?)\b",
    re.I,
)
CURRENT_STATUS_EVIDENCE_PATTERN = re.compile(
    r"\b(?:active|current|currently|presently|practicing)\b.{0,100}"
    r"\b(?:co[- ]?founders?|co[- ]?owners?|directors?|employees?|founders?|licensees?|"
    r"managers?|members?|officers?|owners?|participants?|practitioners?|professionals?|"
    r"residents?|students?|workers?|co[- ]?own(?:s|ed|ing)?|own(?:s|ed|ing)?)\b|"
    r"\b(?:co[- ]?founders?|co[- ]?owners?|directors?|employees?|founders?|licensees?|"
    r"managers?|members?|officers?|owners?|participants?|practitioners?|professionals?|"
    r"residents?|students?|workers?|co[- ]?own(?:s|ed|ing)?|own(?:s|ed|ing)?)\b.{0,100}"
    r"\b(?:active|current|currently|presently|practicing)\b",
    re.I,
)
CURRENT_ASSET_VALUE_PATTERN = re.compile(
    r"\b(?:currently\s+)?(?:access|have access to|hold|maintain|manage|own|possess|use)\w*\b"
    r".{0,100}\b(?:accounts?|assets?|equipment|profiles?|resources?|systems?|tools?)\b|"
    r"\b(?:accounts?|assets?|equipment|profiles?|resources?|systems?|tools?)\b"
    r".{0,100}\b(?:access|available|maintain|manage|own|possess|required)\w*\b",
    re.I,
)
CURRENT_ASSET_EVIDENCE_PATTERN = re.compile(
    r"\b(?:must|required|requires?|need(?:ed|s)?)\b.{0,120}"
    r"\b(?:access|accounts?|assets?|equipment|profiles?|resources?|systems?|tools?)\b|"
    r"\b(?:access|accounts?|assets?|equipment|profiles?|resources?|systems?|tools?)\b"
    r".{0,120}\b(?:must|required|requires?|need(?:ed|s)?)\b",
    re.I,
)
CAPABILITY_VALUE_PATTERN = re.compile(
    r"\b(?:abilit(?:y|ies)|capabilit(?:y|ies)|competenc(?:e|y|ies)|expertise|"
    r"familiarity|fluency|knowledge|proficien(?:cy|t)|skills?|talent)\b",
    re.I,
)
MIXED_EXPERIENCE_ASSET_PATTERN = re.compile(
    r"\b(?:background|experience|history|practice|tenure|track record)\b"
    r".{0,140}\b(?:and|or)\b.{0,100}"
    r"\b(?:access|accounts?|assets?|equipment|profiles?|resources?|systems?|tools?)\b",
    re.I,
)


def _llm_item_blocks(item: dict, evidence_blocks: dict[str, dict]) -> list[dict]:
    return [
        evidence_blocks[block_id]
        for block_id in item.get("evidence") or []
        if block_id in evidence_blocks
    ]


def _fact_value_text(value) -> str:
    if type(value) is dict:
        return " ".join(clean(item) for item in value.values() if clean(item))
    return clean(value)


def _relevant_evidence_text(item: dict, evidence_blocks: dict[str, dict]) -> str:
    value_tokens = fact_tokens(_fact_value_text(item.get("value")))
    candidates = []
    for block in _llm_item_blocks(item, evidence_blocks):
        content = clean(block.get("content"))
        segments = [
            clean(segment)
            for segment in re.split(r"(?<=[.!?])\s+|\s*[;•|]\s*", content)
            if clean(segment)
        ] or [content]
        ranked = sorted(
            segments,
            key=lambda segment: len(value_tokens & fact_tokens(segment)),
            reverse=True,
        )
        if ranked:
            candidates.append(ranked[0])
    return " ".join(candidates)


def evidence_requirement_modality(
    item: dict, evidence_blocks: dict[str, dict]
) -> str:
    text = _relevant_evidence_text(item, evidence_blocks)
    value_tokens = fact_tokens(_fact_value_text(item.get("value")))
    for modifier in re.finditer(r"\bespecially\b", text, re.I):
        before = fact_tokens(text[: modifier.start()])
        after = fact_tokens(text[modifier.end() :])
        before_overlap = len(value_tokens & before)
        after_overlap = len(value_tokens & after)
        if after_overlap >= 2 and after_overlap > before_overlap:
            return "preferred"
    preferred = PREFERRED_MODALITY_PATTERN.search(text) is not None
    required_text = re.sub(r"\bnot required\b", "", text, flags=re.I)
    required = REQUIRED_MODALITY_PATTERN.search(required_text) is not None
    if preferred and not required:
        return "preferred"
    if required and not preferred:
        return "required"
    return "ambiguous"


def _deduplicate_llm_items(items: list[dict]) -> list[dict]:
    by_value = {}
    ordered = []
    for item in items:
        key = (
            normalize_text(item.get("value"))
            if type(item.get("value")) is str
            else canonical_json(item.get("value"))
        )
        prior = by_value.get(key)
        if prior is None:
            prior = copy.deepcopy(item)
            prior["evidence"] = list(dict.fromkeys(prior.get("evidence") or []))
            by_value[key] = prior
            ordered.append(prior)
            continue
        prior["evidence"] = list(
            dict.fromkeys([*(prior.get("evidence") or []), *(item.get("evidence") or [])])
        )
    return ordered


def _reclassify_list_pair(
    payload: dict,
    required_field: str,
    preferred_field: str,
    evidence_blocks: dict[str, dict],
) -> None:
    required = []
    preferred = []
    for item in payload.get(required_field) or []:
        if evidence_requirement_modality(item, evidence_blocks) == "preferred":
            preferred.append(item)
        else:
            required.append(item)
    for item in payload.get(preferred_field) or []:
        if evidence_requirement_modality(item, evidence_blocks) == "required":
            required.append(item)
        else:
            preferred.append(item)
    payload[required_field] = _deduplicate_llm_items(required)
    payload[preferred_field] = _deduplicate_llm_items(preferred)


def reclassify_requirement_modalities(
    payload: dict, evidence_blocks: dict[str, dict]
) -> None:
    for required_field, preferred_field in (
        ("skills_required", "skills_preferred"),
        ("credentials", "credentials_preferred"),
        ("licenses", "licenses_preferred"),
        ("experience_required", "experience_preferred"),
    ):
        _reclassify_list_pair(
            payload,
            required_field,
            preferred_field,
            evidence_blocks,
        )

    minimum = payload.get("education_minimum_level") or {
        "value": None,
        "evidence": [],
    }
    preferred_levels = list(payload.get("education_preferred_levels") or [])
    if (
        minimum.get("value") is not None
        and evidence_requirement_modality(minimum, evidence_blocks) == "preferred"
    ):
        preferred_levels.append(copy.deepcopy(minimum))
        payload["education_minimum_level"] = {"value": None, "evidence": []}
    retained_preferred = []
    for item in preferred_levels:
        if (
            evidence_requirement_modality(item, evidence_blocks) == "required"
            and payload["education_minimum_level"]["value"] is None
        ):
            payload["education_minimum_level"] = copy.deepcopy(item)
        else:
            retained_preferred.append(item)
    payload["education_preferred_levels"] = _deduplicate_llm_items(
        retained_preferred
    )

    required_years = payload.get("years_experience_min") or {
        "value": None,
        "evidence": [],
    }
    preferred_years = payload.get("years_experience_preferred_min") or {
        "value": None,
        "evidence": [],
    }
    if (
        required_years.get("value") is not None
        and evidence_requirement_modality(required_years, evidence_blocks)
        == "preferred"
    ):
        if preferred_years.get("value") is None:
            payload["years_experience_preferred_min"] = copy.deepcopy(required_years)
        payload["years_experience_min"] = {"value": None, "evidence": []}
    elif (
        preferred_years.get("value") is not None
        and evidence_requirement_modality(preferred_years, evidence_blocks)
        == "required"
    ):
        if required_years.get("value") is None:
            payload["years_experience_min"] = copy.deepcopy(preferred_years)
        payload["years_experience_preferred_min"] = {
            "value": None,
            "evidence": [],
        }


def ensure_required_role_domain_expertise(
    payload: dict,
    evidence_blocks: dict[str, dict],
    deterministic_document: dict,
) -> None:
    """Retain broad required expertise only from an explicit body role target.

    The canonical title is deliberately not authority. The source body must name
    the targeted experts/specialists, and the model must already have extracted a
    matching expertise fact from that evidence.
    """

    del deterministic_document
    target_pattern = re.compile(
        r"\b(?:are you|looking for|seeking|sourcing)\b\s+"
        r"(?:independent\s+)?(?P<domain>[^.;:]{1,80}?)\s+"
        r"(?:experts?|specialists?)\b",
        re.I,
    )
    targets = []
    for block_id, block in evidence_blocks.items():
        if block.get("kind") != "body_paragraph":
            continue
        match = target_pattern.search(clean(block.get("content")))
        if match is not None:
            domain = clean(match.group("domain"))
            targets.append((domain, normalize_text(domain), block_id))

    all_skill_items = [
        *(payload.get("skills_required") or []),
        *(payload.get("skills_preferred") or []),
    ]
    for domain, normalized_domain, block_id in targets:
        matching_items = [
            item
            for item in all_skill_items
            if contains_term(
                normalize_text(_fact_value_text(item.get("value"))),
                normalized_domain,
            )
            and block_id in (item.get("evidence") or [])
            and re.search(
                r"\b(?:expertise|expert|specialist)\b",
                _fact_value_text(item.get("value")),
                re.I,
            )
        ]
        if not matching_items:
            continue
        if not any(
            item in (payload.get("skills_preferred") or [])
            for item in matching_items
        ):
            continue
        broad_value = f"{domain[:1].upper() + domain[1:]} expertise"
        if any(
            normalize_text(_fact_value_text(item.get("value")))
            == normalize_text(broad_value)
            for item in payload.get("skills_required") or []
        ):
            continue
        payload["skills_required"] = _deduplicate_llm_items(
            [
                *(payload.get("skills_required") or []),
                {"value": broad_value, "evidence": [block_id]},
            ]
        )


def move_experience_facts_out_of_skills(
    payload: dict, evidence_blocks: dict[str, dict]
) -> None:
    for skill_field, default_experience_field in (
        ("skills_required", "experience_required"),
        ("skills_preferred", "experience_preferred"),
    ):
        retained = []
        for item in payload.get(skill_field) or []:
            # Evidence establishes support and modality, but an unrelated mention
            # of experience in the same block cannot change this fact's family.
            # Cross into an experience field only when the extracted value itself
            # describes an experiential qualification.
            if EXPERIENCE_VALUE_PATTERN.search(
                _fact_value_text(item.get("value"))
            ) is None:
                retained.append(item)
                continue
            modality = evidence_requirement_modality(item, evidence_blocks)
            experience_field = {
                "required": "experience_required",
                "preferred": "experience_preferred",
            }.get(modality, default_experience_field)
            payload[experience_field] = _deduplicate_llm_items(
                [*(payload.get(experience_field) or []), item]
            )
        payload[skill_field] = retained


def _is_mixed_experience_alternative(value: str) -> bool:
    alternatives = [
        clean(item)
        for item in re.split(r"\bor\b", value, flags=re.I)
        if clean(item)
    ]
    if len(alternatives) < 2:
        return False
    return any(EXPERIENCE_VALUE_PATTERN.search(item) for item in alternatives) and any(
        EXPERIENCE_VALUE_PATTERN.search(item) is None
        and NON_EXPERIENCE_ALTERNATIVE_PATTERN.search(item)
        for item in alternatives
    )


def move_mixed_experience_alternatives_to_capabilities(payload: dict) -> None:
    """Keep a mixed experiential/non-experiential OR qualification intact.

    The existing skill/capability list can conservatively retain the complete
    source proposition as one value. Splitting it would falsely make an
    experiential branch mandatory, so no general boolean rule language is added.
    """

    for experience_field, skill_field in (
        ("experience_required", "skills_required"),
        ("experience_preferred", "skills_preferred"),
    ):
        retained = []
        capabilities = list(payload.get(skill_field) or [])
        for item in payload.get(experience_field) or []:
            if _is_mixed_experience_alternative(
                _fact_value_text(item.get("value"))
            ):
                capabilities.append(item)
            else:
                retained.append(item)
        payload[experience_field] = retained
        payload[skill_field] = _deduplicate_llm_items(capabilities)


def llm_current_status_is_explicit(
    item: dict, evidence_blocks: dict[str, dict]
) -> bool:
    value = _fact_value_text(item.get("value"))
    status_value = CURRENT_STATUS_VALUE_PATTERN.search(value) is not None
    asset_value = CURRENT_ASSET_VALUE_PATTERN.search(value) is not None
    if not status_value and not asset_value:
        return False
    relevant = _relevant_evidence_text(item, evidence_blocks)
    supported = (
        CURRENT_STATUS_EVIDENCE_PATTERN.search(relevant) is not None
        if status_value
        else CURRENT_ASSET_EVIDENCE_PATTERN.search(relevant) is not None
    )
    if not supported:
        return False
    modality = evidence_requirement_modality(item, evidence_blocks)
    return modality == "required" or (
        modality == "ambiguous"
        and PREFERRED_MODALITY_PATTERN.search(relevant) is None
        and re.search(r"\b(?:active|current|currently|must|required)\b", relevant, re.I)
        is not None
    )


def _looks_like_current_status_or_asset(
    item: dict, evidence_blocks: dict[str, dict]
) -> bool:
    value = _fact_value_text(item.get("value"))
    relevant = _relevant_evidence_text(item, evidence_blocks)
    return bool(
        (
            CURRENT_STATUS_VALUE_PATTERN.search(value)
            and CURRENT_STATUS_EVIDENCE_PATTERN.search(relevant)
        )
        or (
            CURRENT_ASSET_VALUE_PATTERN.search(value)
            and CURRENT_ASSET_EVIDENCE_PATTERN.search(relevant)
        )
    )


def _normalize_current_status_item(item: dict) -> dict | None:
    retained = copy.deepcopy(item)
    value = _fact_value_text(retained.get("value"))
    if EXPERIENCE_VALUE_PATTERN.search(value) is None:
        return retained
    if re.search(r"\b(?:or|and)\s+former\b", value, re.I):
        return None
    match = re.search(
        r"\b(?:an?\s+)?(?P<status>(?:active|current|currently|presently|practicing)\b.+)",
        value,
        re.I,
    )
    if match is None:
        return None
    status = clean(match.group("status"))
    retained["value"] = status[:1].upper() + status[1:]
    return retained


def move_current_status_facts_out_of_other_requirements(
    payload: dict, evidence_blocks: dict[str, dict]
) -> None:
    statuses = []
    for item in payload.get("current_status_requirements") or []:
        if not llm_current_status_is_explicit(item, evidence_blocks):
            continue
        retained = _normalize_current_status_item(item)
        if retained is not None:
            statuses.append(retained)
    for field in (
        "skills_required",
        "skills_preferred",
        "experience_required",
        "experience_preferred",
    ):
        retained = []
        for item in payload.get(field) or []:
            if llm_current_status_is_explicit(item, evidence_blocks):
                retained_status = _normalize_current_status_item(item)
                if retained_status is not None:
                    statuses.append(retained_status)
            elif _looks_like_current_status_or_asset(item, evidence_blocks):
                # There is no preferred/ambiguous current-status field. Do not
                # misrepresent such a proposition as skill or experience.
                continue
            else:
                retained.append(item)
        payload[field] = retained
    payload["current_status_requirements"] = _deduplicate_llm_items(statuses)


def normalize_experience_field_authority(
    payload: dict, evidence_blocks: dict[str, dict]
) -> None:
    """Require each accepted experience proposition to be experiential itself."""

    for experience_field, default_skill_field in (
        ("experience_required", "skills_required"),
        ("experience_preferred", "skills_preferred"),
    ):
        retained = []
        for item in payload.get(experience_field) or []:
            value = _fact_value_text(item.get("value"))
            if MIXED_EXPERIENCE_ASSET_PATTERN.search(value) is not None:
                # A compound experience-and-asset claim cannot be put wholesale
                # into either family. Separate model facts may still survive.
                continue
            if EXPERIENCE_VALUE_PATTERN.search(value) is not None:
                retained.append(item)
                continue
            if CAPABILITY_VALUE_PATTERN.search(value) is None:
                continue
            modality = evidence_requirement_modality(item, evidence_blocks)
            skill_field = {
                "required": "skills_required",
                "preferred": "skills_preferred",
            }.get(modality, default_skill_field)
            payload[skill_field] = _deduplicate_llm_items(
                [*(payload.get(skill_field) or []), item]
            )
        payload[experience_field] = retained


def _qualitative_experience_from_evidence(
    item: dict, evidence_blocks: dict[str, dict]
) -> str | None:
    for block in _llm_item_blocks(item, evidence_blocks):
        text = clean(block.get("content"))
        match = re.search(
            r"\b\d{1,2}\+?\s*years?\s+(?:of\s+)?(?P<area>[^.;]{1,100}?)\s+experience\b",
            text,
            re.I,
        )
        if match is not None:
            area = clean(match.group("area"))
            if normalize_text(area) != "of":
                return f"{area} experience"
        match = re.search(
            r"\b\d{1,2}\+?\s*years?\s+of\s+experience\s+(?:in|with)\s+"
            r"(?P<area>[^.;]{1,100})",
            text,
            re.I,
        )
        if match is not None:
            return f"Experience {clean(match.group('area'))}"
    return None


def ensure_numeric_experience_has_qualitative_fact(
    payload: dict, evidence_blocks: dict[str, dict]
) -> None:
    for years_field, experience_field in (
        ("years_experience_min", "experience_required"),
        ("years_experience_preferred_min", "experience_preferred"),
    ):
        years = payload.get(years_field) or {"value": None, "evidence": []}
        if years.get("value") is None or payload.get(experience_field):
            continue
        value = _qualitative_experience_from_evidence(years, evidence_blocks)
        if value:
            payload[experience_field] = [
                {"value": value, "evidence": list(years["evidence"])}
            ]


def _metadata_has_explicit_candidate_authority(block: dict, subject: str) -> bool:
    label = normalize_text(block.get("label"))
    if subject == "language":
        return re.search(
            r"(?:candidate|eligib|qualif|require).{0,30}language|"
            r"language.{0,30}(?:candidate|eligib|qualif|require)",
            label,
        ) is not None
    return re.search(
        r"(?:candidate|eligib|require).{0,30}(?:location|countr|region)|"
        r"(?:location|countr|region).{0,30}(?:candidate|eligib|require)|"
        r"workplace[_ .-]?type",
        label,
    ) is not None


def llm_language_requirement_is_explicit(
    item: dict, evidence_blocks: dict[str, dict]
) -> bool:
    blocks = _llm_item_blocks(item, evidence_blocks)
    if not blocks:
        return False
    if any(
        block.get("kind") == "metadata_field"
        and not _metadata_has_explicit_candidate_authority(block, "language")
        for block in blocks
    ):
        return False
    language = normalize_text((item.get("value") or {}).get("language"))
    locale = normalize_text((item.get("value") or {}).get("locale"))
    text = normalize_text(" ".join(clean(block.get("content")) for block in blocks))
    named_terms = [term for term in (language, locale) if term]
    if not language or not any(contains_term(text, term) for term in named_terms):
        return False
    capability_signal = re.search(
        r"\b(?:bilingual|fluency|fluent|language (?:ability|proficiency|skills?)|"
        r"must (?:read|speak|write)|native|proficien(?:cy|t)|required language)\b",
        text,
    ) is not None
    duty_signal = any(
        block.get("kind") == "body_paragraph"
        and re.search(
            r"\b(?:candidate|contractor|you)\b.{0,100}"
            r"\b(?:converse|communicat|perform|read|respond|speak|work|write)\w*\b"
            r".{0,100}\b(?:in|using)\b.{0,60}\b(?:"
            + "|".join(re.escape(term) for term in named_terms)
            + r")\b",
            normalize_text(block.get("content")),
        )
        is not None
        for block in blocks
    )
    modality = evidence_requirement_modality(item, evidence_blocks)
    if modality == "preferred":
        return False
    if modality == "required":
        return capability_signal or duty_signal
    verified_proficiency = re.search(
        r"\bverified\b.{0,80}\b(?:fluency|language|proficien(?:cy|t))\b|"
        r"\b(?:fluency|language|proficien(?:cy|t))\b.{0,80}\bverified\b",
        text,
    ) is not None
    return duty_signal or verified_proficiency


def llm_currency_is_explicit(
    item: dict, evidence_blocks: dict[str, dict]
) -> bool:
    markers = {
        "USD": r"(?:\busd\b|\bus\$|\bu\.s\. dollars?\b|\bunited states dollars?\b)",
        "CAD": r"(?:\bcad\b|\bc\$|\bcanadian dollars?\b)",
        "AUD": r"(?:\baud\b|\ba\$|\baustralian dollars?\b)",
        "EUR": r"(?:\beur\b|€|\beuros?\b)",
        "GBP": r"(?:\bgbp\b|£|\bpounds? sterling\b)",
    }
    pattern = markers.get(item.get("value"))
    blocks = _llm_item_blocks(item, evidence_blocks)
    return bool(
        pattern
        and blocks
        and all(re.search(pattern, clean(block.get("content")), re.I) for block in blocks)
    )


def llm_location_fact_is_explicit(
    field: str, item: dict, evidence_blocks: dict[str, dict]
) -> bool:
    blocks = _llm_item_blocks(item, evidence_blocks)
    if not blocks:
        return False
    for block in blocks:
        kind = block.get("kind")
        if kind == "metadata_field" and not _metadata_has_explicit_candidate_authority(
            block, "location"
        ):
            return False
        if kind == "listing_field" and block.get("label") != "listing.location":
            return False
    text = clean(" ".join(clean(block.get("content")) for block in blocks))
    normalized = normalize_text(text)
    value = item.get("value")
    if field == "workplace_mode":
        patterns = {
            "remote": r"\bremote\b",
            "hybrid": r"\bhybrid\b",
            "onsite": r"\b(?:on[- ]?site|in[- ]person)\b",
        }
        return re.search(patterns.get(value, r"(?!x)x"), normalized) is not None
    if field == "location_scope":
        scope, _remote, _requirements, _restriction = classify_job_location(text)
        return scope == value
    if field == "eligible_countries":
        return value in countries_in_location(text)
    if field == "eligible_regions":
        return value in regions_in_location(text)
    if field == "eligible_locations":
        value_tokens = fact_tokens(str(value))
        text_tokens = fact_tokens(text)
        return bool(value_tokens) and len(value_tokens & text_tokens) / len(value_tokens) >= 0.75
    return False


def _activity_block_is_substantive(activity: str, block: dict) -> bool:
    if block.get("kind") != "body_paragraph":
        return False
    patterns = SUBSTANTIVE_ACTIVITY_PATTERNS.get(activity, ())
    if not patterns:
        return False
    content = normalize_text(block.get("content"))
    if len(content) < 24:
        return False
    segments = [
        normalize_text(segment)
        for segment in re.split(r"(?<=[.!?])\s+|\s*[;•|]\s*|\r?\n+", content)
        if normalize_text(segment)
    ] or [content]
    for segment in segments:
        if activity == "software_testing" and not (
            SOFTWARE_TEST_ACTION_PATTERN.search(segment)
            and SOFTWARE_TEST_ARTIFACT_PATTERN.search(segment)
        ):
            continue
        if (
            activity == "research_analysis"
            and re.search(r"\bresearch (?:study|participant|project)\b", segment)
            and re.search(
                r"\b(?:analys(?:is|t)|researcher|conduct research)\b",
                segment,
            )
            is None
        ):
            continue
        if any(re.search(pattern, segment) for pattern in patterns):
            return True
    return False


def _professional_domain_block_is_explicit(domain: str, block: dict) -> bool:
    if block.get("kind") != "body_paragraph":
        return False
    patterns = PROFESSIONAL_DOMAIN_EVIDENCE_PATTERNS.get(domain, ())
    if not patterns:
        return False
    segments = [
        normalize_text(segment)
        for segment in re.split(
            r"(?<=[.!?])\s+|\s*[;•|]\s*|\r?\n+",
            clean(block.get("content")),
        )
        if normalize_text(segment)
    ]
    return any(
        any(re.search(pattern, segment) for pattern in patterns)
        for segment in segments
    )


def guard_llm_professional_domains(
    items: list[dict], evidence_blocks: dict[str, dict]
) -> list[dict]:
    guarded = []
    for item in items:
        supporting_aliases = [
            block_id
            for block_id in item.get("evidence") or []
            if block_id in evidence_blocks
            and _professional_domain_block_is_explicit(
                item.get("value"), evidence_blocks[block_id]
            )
        ]
        if not supporting_aliases:
            continue
        retained = copy.deepcopy(item)
        retained["evidence"] = list(dict.fromkeys(supporting_aliases))
        guarded.append(retained)
    return guarded


def guard_llm_work_activities(
    items: list[dict], evidence_blocks: dict[str, dict]
) -> list[dict]:
    guarded = []
    for item in items:
        supporting_aliases = [
            block_id
            for block_id in item.get("evidence") or []
            if block_id in evidence_blocks
            and _activity_block_is_substantive(
                item.get("value"), evidence_blocks[block_id]
            )
        ]
        if not supporting_aliases:
            continue
        retained = copy.deepcopy(item)
        retained["evidence"] = list(dict.fromkeys(supporting_aliases))
        guarded.append(retained)
    return guarded


def llm_activity_is_substantive(item: dict, evidence_blocks: dict[str, dict]) -> bool:
    patterns = SUBSTANTIVE_ACTIVITY_PATTERNS.get(item["value"], ())
    blocks = _llm_item_blocks(item, evidence_blocks)
    return bool(
        patterns
        and blocks
        and any(
            _activity_block_is_substantive(item["value"], block)
            for block in blocks
        )
    )


def skill_is_constraint(value: str) -> bool:
    normalized = normalize_text(value)
    if CANDIDATE_ATTRIBUTE_PATTERN.search(normalized) is not None:
        return False
    return SKILL_CONSTRAINT_PATTERN.search(normalized) is not None


def skill_duplicates_language_requirement(
    value: str, required_languages: set[str]
) -> bool:
    normalized = normalize_text(value)
    if re.search(r"\b(?:bilingual|fluency|fluent|language|native|proficien)\w*\b", normalized) is None:
        return False
    return any(contains_term(normalized, language) for language in required_languages)


def skill_is_task_description(value: str) -> bool:
    normalized = normalize_text(value)
    if CANDIDATE_ATTRIBUTE_PATTERN.search(normalized):
        return False
    if TASK_LEADING_PATTERN.search(normalized):
        return True

    actions = list(TASK_ACTION_PATTERN.finditer(normalized))
    has_task_object = TASK_OBJECT_PATTERN.search(normalized) is not None
    if not actions or not has_task_object:
        return False

    # A compact noun phrase can name a genuine method or competency (for example,
    # "video editing" or "data collection"). It becomes a task description when
    # its action is directed at a deliverable, or when multiple actions are joined
    # into a description of what the worker will do.
    if len(actions) >= 2 and TASK_COMPOUND_PATTERN.search(normalized):
        return True
    first_action_end = actions[0].end()
    if TASK_OBJECT_LINK_PATTERN.search(normalized[first_action_end:]):
        return True
    return DELIVERABLE_CONSTRAINT_PATTERN.search(normalized) is not None


def llm_role_family_conflicts_with_substantive_work(
    role_family: dict,
    work_activities: list[dict],
    deterministic_document: dict,
) -> bool:
    proposed = role_family.get("value")
    if not proposed:
        return False
    title_family = classify_role_family(
        normalize_text(deterministic_document["source"]["canonical_title"])
    )
    activities = {
        *deterministic_document["attributes"]["role"]["work_activities"],
        *(item["value"] for item in work_activities),
    }
    if title_family == "ai_training" and activities == {"ai_training_evaluation"}:
        return proposed not in {"ai_training", "expert_review"}
    return False


def fact_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9+#.]+", normalize_text(value))
        if token not in {"a", "an", "and", "for", "of", "or", "the", "to", "with"}
    }


def skill_duplicates_responsibility(value: str, responsibilities: list[str]) -> bool:
    normalized = normalize_text(value)
    if not skill_is_task_description(normalized):
        return False
    skill_tokens = fact_tokens(normalized)
    for responsibility in responsibilities:
        responsibility_tokens = fact_tokens(responsibility)
        if not skill_tokens or not responsibility_tokens:
            continue
        overlap = len(skill_tokens & responsibility_tokens) / min(
            len(skill_tokens), len(responsibility_tokens)
        )
        if overlap >= 0.7:
            return True
    return False


def skill_is_unqualified_metadata_keyword(
    item: dict, evidence_blocks: dict[str, dict]
) -> bool:
    blocks = [evidence_blocks[block_id] for block_id in item["evidence"]]
    if not blocks or not all(block["kind"] == "metadata_field" for block in blocks):
        return False
    if not all(".skills" in block["label"] for block in blocks):
        return False
    return not any(
        re.search(r"\b(?:required|preferred|must|need|qualification)\b", block["content"], re.I)
        for block in blocks
    )


def caveat_is_candidate_warning(value: str) -> bool:
    normalized = normalize_text(value)
    if MATERIAL_EQUIPMENT_WARNING_PATTERN.search(normalized):
        return True
    return ROUTINE_CAVEAT_PATTERN.search(normalized) is None


def unusual_eligibility_caveats(evidence_blocks: dict[str, dict]) -> list[dict]:
    caveats = []
    for block_id in sorted(evidence_blocks):
        block = evidence_blocks[block_id]
        content = clean(block.get("content"))
        if UNUSUAL_ELIGIBILITY_PATTERN.search(normalize_text(content)) is None:
            continue
        household_age = HOUSEHOLD_AGE_RESTRICTION_PATTERN.search(
            normalize_text(content)
        )
        if household_age is not None:
            caveats.append(
                {
                    "value": (
                        "All household members must be "
                        f"{household_age.group('minimum_age')} years or older; "
                        f"no children under {household_age.group('child_age')} may live "
                        "in the home."
                    ),
                    "evidence": [block_id],
                }
            )
            continue
        sentences = [
            clean(sentence)
            for sentence in re.split(r"(?<=[.!?])\s+|\s*[•|]\s*", content)
            if clean(sentence)
        ]
        matching_sentences = [
            sentence
            for sentence in sentences
            if UNUSUAL_ELIGIBILITY_PATTERN.search(normalize_text(sentence))
        ]
        if not matching_sentences:
            matching_sentences = [content]
        value = clean(" ".join(matching_sentences))
        value = re.sub(
            r"^(?:(?:metadata|listing)\.[^:]+|eligibility|requirements?):\s*",
            "",
            value,
            flags=re.I,
        )
        if not value or len(value) > 500:
            continue
        caveats.append({"value": value, "evidence": [block_id]})
    return caveats


def caveats_materially_overlap(left: dict, right: dict) -> bool:
    if normalize_text(left["value"]) == normalize_text(right["value"]):
        return True
    left_tokens = fact_tokens(left["value"])
    right_tokens = fact_tokens(right["value"])
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))
    return overlap >= 0.6


def validate_llm_payload(payload: dict, evidence_blocks: dict[str, dict]) -> None:
    require_exact_keys(payload, set(LLM_PAYLOAD_FIELDS), "llm_payload")
    validate_llm_scalar(
        payload["role_family"],
        "llm_payload.role_family",
        evidence_blocks,
        allowed=ROLE_FAMILIES,
    )
    validate_llm_list(
        payload["professional_domains"],
        "llm_payload.professional_domains",
        evidence_blocks,
        allowed=PROFESSIONAL_DOMAINS,
    )
    validate_llm_list(
        payload["work_activities"],
        "llm_payload.work_activities",
        evidence_blocks,
        allowed=WORK_ACTIVITIES,
    )
    validate_llm_list(
        payload["education_accepted_alternatives"],
        "llm_payload.education_accepted_alternatives",
        evidence_blocks,
        allowed=EDUCATION_LEVELS - {"unknown"},
    )
    validate_llm_list(
        payload["education_preferred_levels"],
        "llm_payload.education_preferred_levels",
        evidence_blocks,
        allowed=EDUCATION_LEVELS - {"unknown"},
    )
    for field in (
        "specializations",
        "skills_required",
        "skills_preferred",
        "credentials",
        "credentials_preferred",
        "licenses",
        "licenses_preferred",
        "experience_required",
        "experience_preferred",
        "current_status_requirements",
        "eligible_countries",
        "eligible_regions",
        "eligible_locations",
        "responsibilities",
        "caveats",
    ):
        allowed = None
        if field == "eligible_countries":
            allowed = CANONICAL_COUNTRIES
        elif field == "eligible_regions":
            allowed = REGIONAL_LOCATION_TOKENS
        validate_llm_list(
            payload[field],
            f"llm_payload.{field}",
            evidence_blocks,
            allowed=allowed,
        )
    validate_llm_list(
        payload["languages"],
        "llm_payload.languages",
        evidence_blocks,
        value_kind="language",
    )
    scalar_specs = {
        "education_minimum_level": {
            "allowed": EDUCATION_LEVELS - {"unknown"}
        },
        "years_experience_min": {
            "value_kind": "integer",
            "minimum": 0,
            "maximum": 80,
        },
        "years_experience_preferred_min": {
            "value_kind": "integer",
            "minimum": 0,
            "maximum": 80,
        },
        "workplace_mode": {"allowed": WORKPLACE_MODES - {"unknown"}},
        "location_scope": {"allowed": LOCATION_SCOPES - {"unknown"}},
        "engagement_type": {"allowed": ENGAGEMENT_TYPES - {"unknown"}},
        "schedule_type": {"allowed": SCHEDULE_TYPES - {"unknown"}},
        "hours_per_week_min": {
            "value_kind": "integer",
            "minimum": 1,
            "maximum": 168,
        },
        "hours_per_week_max": {
            "value_kind": "integer",
            "minimum": 1,
            "maximum": 168,
        },
        "duration": {},
        "compensation_disclosed": {"value_kind": "boolean"},
        "compensation_currency": {"allowed": ISO_4217_CURRENCIES},
        "compensation_amount_min": {
            "value_kind": "number",
            "minimum": 0,
        },
        "compensation_amount_max": {
            "value_kind": "number",
            "minimum": 0,
        },
        "compensation_period": {
            "allowed": COMPENSATION_PERIODS - {"unknown"}
        },
        "compensation_amount_type": {
            "allowed": COMPENSATION_AMOUNT_TYPES - {"unknown"}
        },
        "compensation_notes": {},
        "candidate_profile": {},
        "quick_take": {},
    }
    for field, options in scalar_specs.items():
        validate_llm_scalar(
            payload[field],
            f"llm_payload.{field}",
            evidence_blocks,
            **options,
        )
    validate_known_empty_fields(payload["known_empty_fields"], evidence_blocks)
    known_empty_paths = {
        item["field_path"] for item in payload["known_empty_fields"]
    }
    for payload_field, field_path in LLM_LIST_FIELD_PATHS.items():
        if payload[payload_field] and field_path in known_empty_paths:
            raise EnrichmentValidationError(
                f"llm_payload.{payload_field} conflicts with known-empty evidence."
            )

    hours_min = payload["hours_per_week_min"]["value"]
    hours_max = payload["hours_per_week_max"]["value"]
    if hours_min is not None and hours_max is not None and hours_min > hours_max:
        raise EnrichmentValidationError(
            "llm_payload weekly hour bounds are inconsistent."
        )
    amount_min = payload["compensation_amount_min"]["value"]
    amount_max = payload["compensation_amount_max"]["value"]
    disclosed = payload["compensation_disclosed"]["value"]
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        raise EnrichmentValidationError(
            "llm_payload compensation bounds are inconsistent."
        )
    if (amount_min is not None or amount_max is not None) and disclosed is not True:
        raise EnrichmentValidationError(
            "llm_payload compensation amounts require disclosed=true."
        )
    if disclosed is False and any(
        payload[field]["value"] is not None
        for field in (
            "compensation_currency",
            "compensation_amount_min",
            "compensation_amount_max",
            "compensation_period",
            "compensation_amount_type",
        )
    ):
        raise EnrichmentValidationError(
            "llm_payload undisclosed compensation cannot contain structured amounts."
        )


def validate_llm_scalar(
    value,
    path,
    evidence_blocks,
    *,
    allowed=None,
    value_kind="text",
    minimum=None,
    maximum=None,
):
    require_exact_keys(value, {"value", "evidence"}, path)
    fact = value["value"]
    if fact is None:
        if value["evidence"] != []:
            raise EnrichmentValidationError(f"{path}.evidence must be empty for null.")
        return
    if value_kind == "text":
        if type(fact) is not str or not fact.strip():
            raise EnrichmentValidationError(
                f"{path}.value must be non-empty text or null."
            )
    elif value_kind == "boolean":
        if type(fact) is not bool:
            raise EnrichmentValidationError(f"{path}.value must be boolean or null.")
    elif value_kind in {"integer", "number"}:
        valid_type = type(fact) is int if value_kind == "integer" else type(fact) in {int, float}
        if not valid_type:
            raise EnrichmentValidationError(f"{path}.value must be numeric or null.")
        if minimum is not None and fact < minimum:
            raise EnrichmentValidationError(f"{path}.value is below its minimum.")
        if maximum is not None and fact > maximum:
            raise EnrichmentValidationError(f"{path}.value is above its maximum.")
    else:
        raise EnrichmentValidationError(f"{path}.value kind is unsupported.")
    if allowed is not None and fact not in allowed:
        raise EnrichmentValidationError(f"{path}.value is unsupported.")
    validate_llm_evidence(value["evidence"], path, evidence_blocks)


def validate_llm_list(
    value,
    path,
    evidence_blocks,
    *,
    allowed=None,
    value_kind="text",
    reject_duplicate_facts=True,
):
    if type(value) is not list:
        raise EnrichmentValidationError(f"{path} must be a list.")
    facts = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        require_exact_keys(item, {"value", "evidence"}, item_path)
        fact = item["value"]
        if value_kind == "language":
            require_language_list([fact])
            if fact["language"] not in CANONICAL_LANGUAGES:
                raise EnrichmentValidationError(
                    f"{item_path}.value.language must be canonical."
                )
        elif type(fact) is not str or not fact.strip():
            raise EnrichmentValidationError(
                f"{item_path}.value must be non-empty text."
            )
        if allowed is not None and fact not in allowed:
            raise EnrichmentValidationError(f"{item_path}.value is unsupported.")
        validate_llm_evidence(item["evidence"], item_path, evidence_blocks)
        facts.append(
            fact.casefold() if type(fact) is str else canonical_json(fact)
        )
    if reject_duplicate_facts and len(facts) != len(set(facts)):
        raise EnrichmentValidationError(f"{path} contains duplicate facts.")


def validate_known_empty_fields(value, evidence_blocks):
    if type(value) is not list:
        raise EnrichmentValidationError("llm_payload.known_empty_fields must be a list.")
    fields = []
    for index, item in enumerate(value):
        path = f"llm_payload.known_empty_fields[{index}]"
        require_exact_keys(item, {"field_path", "evidence"}, path)
        if item["field_path"] not in KNOWN_EMPTY_FIELD_PATHS:
            raise EnrichmentValidationError(f"{path}.field_path is unsupported.")
        validate_llm_evidence(item["evidence"], path, evidence_blocks)
        fields.append(item["field_path"])
    if fields != sorted(set(fields)):
        raise EnrichmentValidationError(
            "llm_payload.known_empty_fields must be sorted and unique."
        )


def validate_llm_evidence(value, path, evidence_blocks):
    if type(value) is not list or not value:
        raise EnrichmentValidationError(f"{path}.evidence must not be empty.")
    if len(value) != len(set(value)):
        raise EnrichmentValidationError(f"{path}.evidence contains duplicate IDs.")
    for index, block_id in enumerate(value):
        if type(block_id) is not str or block_id not in evidence_blocks:
            raise EnrichmentValidationError(
                f"{path}.evidence[{index}] references an unknown evidence block ID."
            )


def _llm_item_scope_and_evidence(item, evidence_blocks):
    variant_refs = set()
    evidence = []
    for block_id in item["evidence"]:
        block = evidence_blocks[block_id]
        block_variant_refs = block.get("variant_refs") or []
        variant_refs.update(block_variant_refs)
        source_refs = block.get("source_refs") or [block.get("source_ref")]
        source_refs = sorted(
            {clean(value) for value in source_refs if clean(value)}
        )
        evidence.append(
            {
                "evidence_block_id": block_id,
                "source_refs": source_refs,
                "authority_refs": sorted(
                    {
                        clean(value)
                        for value in block.get("authority_refs") or []
                        if clean(value)
                    }
                ),
                "evidence_text": block["content"],
                "basis": "llm_source_evidence",
                "confidence": "high",
            }
        )
    if not variant_refs:
        raise EnrichmentValidationError(
            "LLM facts must cite evidence bound to at least one variant."
        )
    return sorted(variant_refs), evidence


def llm_payload_variant_facts(payload: dict, evidence_blocks: dict[str, dict]):
    facts = []
    for payload_field, field_path in LLM_SCALAR_FIELD_PATHS.items():
        item = payload[payload_field]
        if item["value"] is None:
            continue
        variant_refs, evidence = _llm_item_scope_and_evidence(item, evidence_blocks)
        facts.append(
            make_variant_fact(field_path, item["value"], variant_refs, evidence)
        )
    for payload_field, field_path in LLM_LIST_FIELD_PATHS.items():
        for item in payload[payload_field]:
            variant_refs, evidence = _llm_item_scope_and_evidence(
                item, evidence_blocks
            )
            facts.append(
                make_variant_fact(field_path, item["value"], variant_refs, evidence)
            )
    for item in payload["known_empty_fields"]:
        variant_refs, evidence = _llm_item_scope_and_evidence(item, evidence_blocks)
        facts.append(
            make_variant_fact(
                item["field_path"],
                [],
                variant_refs,
                evidence,
                knowledge_state="known_empty",
            )
        )
    return normalize_variant_facts(facts)


def merge_llm_payload(
    document: dict,
    payload: dict,
    evidence_blocks: dict[str, dict],
    *,
    all_variant_refs=None,
) -> dict:
    document = copy.deepcopy(document)
    facts = llm_payload_variant_facts(payload, evidence_blocks)
    all_variant_refs = all_variant_refs or sorted(
        {
            variant_ref
            for block in evidence_blocks.values()
            for variant_ref in block.get("variant_refs") or []
        }
    )
    project_variant_facts(document, facts, all_variant_refs)
    refresh_unknown_fields(document)
    document["field_evidence"] = sorted(
        document["field_evidence"],
        key=lambda item: (
            item["field_path"],
            item["source_ref"],
            item["evidence_text"],
        ),
    )
    validate_enrichment_document(document)
    return document


def build_enrichment(semantic_input: dict) -> tuple[str, dict, str]:
    input_sha256 = semantic_input_sha256(semantic_input)
    document = extract_deterministic_document(semantic_input)
    status = STATUS_PARTIAL if document["unknown_fields"] else STATUS_COMPLETE
    return input_sha256, document, status


def has_llm_attempt(conn, canonical_opportunity_id, input_sha256, llm_client) -> bool:
    derivation_fingerprint = derivation_recipe_fingerprint(
        model_provider=llm_client.provider,
        model_name=llm_client.model,
        prompt_version=llm_client.prompt_version,
    )
    return (
        conn.execute(
            """
            SELECT 1
            FROM opportunity_enrichment_runs
            WHERE canonical_opportunity_id = ?
              AND input_sha256 = ?
              AND model_provider = ?
              AND model_name = ?
              AND prompt_version = ?
              AND semantic_input_version = ?
              AND derivation_fingerprint = ?
            LIMIT 1
            """,
            (
                canonical_opportunity_id,
                input_sha256,
                llm_client.provider,
                llm_client.model,
                llm_client.prompt_version,
                SEMANTIC_INPUT_VERSION,
                derivation_fingerprint,
            ),
        ).fetchone()
        is not None
    )


def record_llm_run(
    conn,
    canonical_opportunity_id,
    input_sha256,
    llm_client,
    outcome,
    started_at,
    finished_at,
    *,
    result=None,
    error_type=None,
    diagnostic=None,
):
    derivation_fingerprint = derivation_recipe_fingerprint(
        model_provider=llm_client.provider,
        model_name=llm_client.model,
        prompt_version=llm_client.prompt_version,
    )
    cursor = conn.execute(
        """
        INSERT INTO opportunity_enrichment_runs (
          canonical_opportunity_id, input_sha256, outcome,
          model_provider, model_name, prompt_version, response_id,
          input_tokens, output_tokens, total_tokens, estimated_cost_usd,
          error_type, started_at, finished_at, semantic_input_version,
          derivation_fingerprint
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            canonical_opportunity_id,
            input_sha256,
            outcome,
            llm_client.provider,
            llm_client.model,
            llm_client.prompt_version,
            getattr(result, "response_id", None),
            int(getattr(result, "input_tokens", 0) or 0),
            int(getattr(result, "output_tokens", 0) or 0),
            int(getattr(result, "total_tokens", 0) or 0),
            getattr(result, "estimated_cost_usd", None),
            error_type,
            started_at,
            finished_at,
            SEMANTIC_INPUT_VERSION,
            derivation_fingerprint,
        ),
    )
    conn.execute(
        """
        INSERT INTO opportunity_enrichment_run_diagnostics (
          run_id, diagnostic_json
        )
        VALUES (?, ?)
        """,
        (cursor.lastrowid, canonical_json(diagnostic or {})),
    )


def llm_failure_diagnostic(exc):
    diagnostic = getattr(exc, "diagnostic", None)
    if type(diagnostic) is dict:
        return diagnostic

    from wahojobs.opportunity_llm import diagnostic_record, sanitize_diagnostic_text

    if isinstance(exc, EnrichmentValidationError):
        message = str(exc)
        lowered = message.casefold()
        evidence_failure = any(
            marker in lowered
            for marker in (
                ".evidence",
                ".quote",
                "source content",
                "source_ref",
            )
        )
        diagnostic = diagnostic_record(
            "evidence_validation" if evidence_failure else "schema_validation"
        )
        diagnostic["validation_error_type"] = type(exc).__name__[:100]
        diagnostic["validation_message"] = sanitize_diagnostic_text(message)
        return diagnostic

    diagnostic = diagnostic_record("runtime_error")
    diagnostic["runtime_error_type"] = type(exc).__name__[:100]
    return diagnostic


def llm_success_diagnostic(result):
    from wahojobs.opportunity_llm import diagnostic_record

    return diagnostic_record(
        "succeeded",
        http_status=getattr(result, "http_status", None),
        response_status=getattr(result, "response_status", None),
    )


def enrich_canonical_opportunity(
    conn,
    canonical_opportunity_id: int,
    *,
    now: str | None = None,
    ensure_schema: bool = True,
    llm_client=None,
) -> dict:
    from wahojobs.db.repository import ensure_opportunity_enrichment_schema

    if ensure_schema:
        ensure_opportunity_enrichment_schema(conn)
    semantic_input = load_semantic_input(conn, canonical_opportunity_id)
    input_sha256, document, status = build_enrichment(semantic_input)
    llm_eligible = has_sufficient_llm_source_content(semantic_input)
    existing = conn.execute(
        "SELECT * FROM opportunity_enrichments WHERE canonical_opportunity_id = ?",
        (canonical_opportunity_id,),
    ).fetchone()
    freshness_evidence = classify_enrichment_freshness(semantic_input, existing)
    stored_derivation_fingerprint = freshness_evidence[
        "stored_derivation_fingerprint"
    ]
    stored_recipe_fingerprint = None
    if existing is not None:
        stored_recipe_fingerprint = derivation_recipe_fingerprint(
            model_provider=existing["model_provider"],
            model_name=existing["model_name"],
            prompt_version=existing["prompt_version"],
        )
    same_automatic_input = (
        existing is not None
        and freshness_evidence["source_input_status"] == "current"
        and existing["schema_version"] == SCHEMA_VERSION
        and existing["taxonomy_version"] == TAXONOMY_VERSION
        and existing["extractor_version"] == EXTRACTOR_VERSION
        and freshness_evidence["stored_semantic_input_version"]
        == SEMANTIC_INPUT_VERSION
        and (
            stored_derivation_fingerprint is None
            or stored_derivation_fingerprint == stored_recipe_fingerprint
        )
    )
    should_attempt_llm = llm_client is not None and llm_eligible
    if same_automatic_input:
        prior_model_is_current = (
            should_attempt_llm
            and existing["model_provider"] == llm_client.provider
            and existing["model_name"] == llm_client.model
            and existing["prompt_version"] == llm_client.prompt_version
        )
        already_attempted = should_attempt_llm and has_llm_attempt(
            conn,
            canonical_opportunity_id,
            input_sha256,
            llm_client,
        )
        if not should_attempt_llm or prior_model_is_current or already_attempted:
            if not llm_eligible:
                llm_outcome = "not_eligible"
            elif llm_client is None:
                llm_outcome = "not_requested"
            elif prior_model_is_current:
                llm_outcome = "already_enriched"
            else:
                llm_outcome = "already_attempted"
            stored_document = json.loads(existing["automatic_document_json"])
            validate_enrichment_document(stored_document)
            return {
                "canonical_opportunity_id": canonical_opportunity_id,
                "outcome": "unchanged",
                "status": existing["status"],
                "input_sha256": input_sha256,
                "semantic_input_version": freshness_evidence[
                    "stored_semantic_input_version"
                ],
                "derivation_fingerprint": freshness_evidence[
                    "stored_derivation_fingerprint"
                ],
                "document": stored_document,
                "llm": {
                    "eligible": llm_eligible,
                    "called": False,
                    "outcome": llm_outcome,
                    "model_provider": existing["model_provider"],
                    "model_name": existing["model_name"],
                    "prompt_version": existing["prompt_version"],
                },
            }

    generated_at = now or utc_now()
    model_provider = None
    model_name = None
    prompt_version = None
    llm_result = None
    llm_outcome = "not_eligible" if not llm_eligible else "not_requested"
    if should_attempt_llm:
        packet, evidence_blocks = llm_source_packet(semantic_input)
        started_at = generated_at
        try:
            llm_result = llm_client.enrich(packet)
            normalized_payload = normalize_llm_list_fields(
                llm_result.payload,
                evidence_blocks,
            )
            normalized_payload = apply_llm_semantic_acceptance_guards(
                normalized_payload,
                evidence_blocks,
                document,
            )
            validate_llm_payload(normalized_payload, evidence_blocks)
            document = merge_llm_payload(
                document,
                normalized_payload,
                evidence_blocks,
                all_variant_refs=semantic_variant_refs(semantic_input),
            )
            status = STATUS_PARTIAL if document["unknown_fields"] else STATUS_COMPLETE
        except Exception as exc:
            llm_outcome = "failed"
            failure_result = getattr(exc, "response_metadata", None) or llm_result
            record_llm_run(
                conn,
                canonical_opportunity_id,
                input_sha256,
                llm_client,
                "failed",
                started_at,
                now or utc_now(),
                result=failure_result,
                error_type=type(exc).__name__[:100],
                diagnostic=llm_failure_diagnostic(exc),
            )
            llm_result = failure_result
        else:
            llm_outcome = "succeeded"
            model_provider = llm_client.provider
            model_name = llm_client.model
            prompt_version = llm_client.prompt_version
            record_llm_run(
                conn,
                canonical_opportunity_id,
                input_sha256,
                llm_client,
                "succeeded",
                started_at,
                now or utc_now(),
                result=llm_result,
                diagnostic=llm_success_diagnostic(llm_result),
            )

    preserve_previous_success = (
        llm_outcome == "failed"
        and same_automatic_input
        and existing["model_provider"] is not None
        and existing["model_name"] is not None
        and existing["prompt_version"] is not None
    )
    if preserve_previous_success:
        stored_document = json.loads(existing["automatic_document_json"])
        validate_enrichment_document(stored_document)
        return {
            "canonical_opportunity_id": canonical_opportunity_id,
            "outcome": "unchanged",
            "status": existing["status"],
            "input_sha256": input_sha256,
            "semantic_input_version": freshness_evidence[
                "stored_semantic_input_version"
            ],
            "derivation_fingerprint": freshness_evidence[
                "stored_derivation_fingerprint"
            ],
            "document": stored_document,
            "llm": {
                "eligible": llm_eligible,
                "called": True,
                "outcome": llm_outcome,
                "model_provider": llm_client.provider,
                "model_name": llm_client.model,
                "prompt_version": llm_client.prompt_version,
                "preserved_previous_success": True,
                "input_tokens": int(getattr(llm_result, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(llm_result, "output_tokens", 0) or 0),
                "total_tokens": int(getattr(llm_result, "total_tokens", 0) or 0),
                "estimated_cost_usd": getattr(
                    llm_result, "estimated_cost_usd", None
                ),
            },
        }

    document_json = canonical_json(document)
    derivation_fingerprint = derivation_recipe_fingerprint(
        model_provider=model_provider,
        model_name=model_name,
        prompt_version=prompt_version,
    )
    outcome = "created" if existing is None else "updated"
    conn.execute(
        """
        INSERT INTO opportunity_enrichments (
          canonical_opportunity_id, schema_version, taxonomy_version,
          extractor_version, input_sha256, status, automatic_document_json,
          model_provider, model_name, prompt_version, semantic_input_version,
          derivation_fingerprint, generated_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_opportunity_id) DO UPDATE SET
          schema_version = excluded.schema_version,
          taxonomy_version = excluded.taxonomy_version,
          extractor_version = excluded.extractor_version,
          input_sha256 = excluded.input_sha256,
          status = excluded.status,
          automatic_document_json = excluded.automatic_document_json,
          model_provider = excluded.model_provider,
          model_name = excluded.model_name,
          prompt_version = excluded.prompt_version,
          semantic_input_version = excluded.semantic_input_version,
          derivation_fingerprint = excluded.derivation_fingerprint,
          generated_at = excluded.generated_at,
          updated_at = excluded.updated_at
        """,
        (
            canonical_opportunity_id,
            SCHEMA_VERSION,
            TAXONOMY_VERSION,
            EXTRACTOR_VERSION,
            input_sha256,
            status,
            document_json,
            model_provider,
            model_name,
            prompt_version,
            SEMANTIC_INPUT_VERSION,
            derivation_fingerprint,
            generated_at,
            generated_at,
        ),
    )
    return {
        "canonical_opportunity_id": canonical_opportunity_id,
        "outcome": outcome,
        "status": status,
        "input_sha256": input_sha256,
        "semantic_input_version": SEMANTIC_INPUT_VERSION,
        "derivation_fingerprint": derivation_fingerprint,
        "document": document,
        "llm": {
            "eligible": llm_eligible,
            "called": should_attempt_llm,
            "outcome": llm_outcome,
            "model_provider": model_provider,
            "model_name": model_name,
            "prompt_version": prompt_version,
            "preserved_previous_success": False,
            "input_tokens": int(getattr(llm_result, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(llm_result, "output_tokens", 0) or 0),
            "total_tokens": int(getattr(llm_result, "total_tokens", 0) or 0),
            "estimated_cost_usd": getattr(llm_result, "estimated_cost_usd", None),
        },
    }


def enrich_company_opportunities(conn, company_id: int, *, llm_client=None) -> dict:
    from wahojobs.db.repository import ensure_opportunity_enrichment_schema

    ensure_opportunity_enrichment_schema(conn)
    ids = [
        row["id"]
        for row in conn.execute(
            """
            SELECT id
            FROM canonical_opportunities
            WHERE company_id = ?
            ORDER BY id
            """,
            (company_id,),
        ).fetchall()
    ]
    return enrich_selected_opportunities(
        conn,
        ids,
        llm_client=llm_client,
        ensure_schema=False,
    )


def enrich_all_opportunities(conn, *, llm_client=None) -> dict:
    from wahojobs.db.repository import ensure_opportunity_enrichment_schema

    ensure_opportunity_enrichment_schema(conn)
    ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM canonical_opportunities ORDER BY id"
        ).fetchall()
    ]
    return enrich_selected_opportunities(
        conn,
        ids,
        llm_client=llm_client,
        ensure_schema=False,
    )


def enrich_selected_opportunities(
    conn,
    canonical_opportunity_ids,
    *,
    llm_client=None,
    ensure_schema: bool = True,
) -> dict:
    """Enrich an explicit canonical set without recipe-driven bulk discovery.

    Tracking uses this boundary for canonicals whose accepted semantic input or
    active-variant membership changed in the current crawl.  Manual migration
    tools may still choose the company/all helpers explicitly.
    """

    from wahojobs.db.repository import ensure_opportunity_enrichment_schema

    if ensure_schema:
        ensure_opportunity_enrichment_schema(conn)
    ids = sorted({int(item) for item in canonical_opportunity_ids})
    return summarize_enrichment_results(
        [
            enrich_canonical_opportunity(
                conn,
                item,
                ensure_schema=False,
                llm_client=llm_client,
            )
            for item in ids
        ]
    )


def summarize_enrichment_results(results: list[dict]) -> dict:
    outcomes = Counter(result["outcome"] for result in results)
    statuses = Counter(result["status"] for result in results)
    unknown_fields = Counter(
        path
        for result in results
        for path in result["document"].get("unknown_fields", [])
    )
    llm_outcomes = Counter(
        result.get("llm", {}).get("outcome", "not_requested")
        for result in results
    )
    return {
        "total": len(results),
        "created": outcomes["created"],
        "updated": outcomes["updated"],
        "unchanged": outcomes["unchanged"],
        "complete": statuses[STATUS_COMPLETE],
        "partial": statuses[STATUS_PARTIAL],
        "failed": statuses[STATUS_FAILED],
        "llm_eligible": sum(
            bool(result.get("llm", {}).get("eligible")) for result in results
        ),
        "llm_calls": sum(
            bool(result.get("llm", {}).get("called")) for result in results
        ),
        "llm_succeeded": llm_outcomes["succeeded"],
        "llm_failed": llm_outcomes["failed"],
        "llm_input_tokens": sum(
            int(result.get("llm", {}).get("input_tokens") or 0)
            for result in results
        ),
        "llm_output_tokens": sum(
            int(result.get("llm", {}).get("output_tokens") or 0)
            for result in results
        ),
        "llm_estimated_cost_usd": round(
            sum(
                float(result.get("llm", {}).get("estimated_cost_usd") or 0)
                for result in results
            ),
            8,
        ),
        "unknown_field_counts": dict(sorted(unknown_fields.items())),
    }


def canonical_coverage(conn) -> dict:
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS jobs_total,
          SUM(canonical_opportunity_id IS NOT NULL) AS jobs_canonicalized,
          SUM(is_active = 1) AS active_jobs_total,
          SUM(is_active = 1 AND canonical_opportunity_id IS NOT NULL) AS active_jobs_canonicalized
        FROM jobs
        WHERE title NOT LIKE '[SIMULATION]%'
        """
    ).fetchone()
    canonical = conn.execute(
        """
        SELECT
          COUNT(*) AS canonical_total,
          SUM(is_active = 1) AS active_canonical_total
        FROM canonical_opportunities
        """
    ).fetchone()
    enriched = conn.execute(
        """
        SELECT
          COUNT(*) AS enriched_total,
          SUM(co.is_active = 1) AS active_enriched_total
        FROM opportunity_enrichments oe
        JOIN canonical_opportunities co ON co.id = oe.canonical_opportunity_id
        """
    ).fetchone()
    rich = conn.execute(
        """
        SELECT
          COUNT(*) AS rich_source_jobs,
          COUNT(DISTINCT j.canonical_opportunity_id) AS rich_source_canonical_total
        FROM job_source_contents sc
        JOIN jobs j ON j.id = sc.job_id
        WHERE j.canonical_opportunity_id IS NOT NULL
          AND (
            COALESCE(LENGTH(TRIM(sc.body)), 0) > 0
            OR sc.metadata_json != '{}'
          )
        """
    ).fetchone()
    llm = conn.execute(
        """
        SELECT COUNT(*) AS llm_enriched_total
        FROM opportunity_enrichments
        WHERE model_provider IS NOT NULL
        """
    ).fetchone()
    return {
        "jobs_total": int(row["jobs_total"] or 0),
        "jobs_canonicalized": int(row["jobs_canonicalized"] or 0),
        "active_jobs_total": int(row["active_jobs_total"] or 0),
        "active_jobs_canonicalized": int(row["active_jobs_canonicalized"] or 0),
        "canonical_total": int(canonical["canonical_total"] or 0),
        "active_canonical_total": int(canonical["active_canonical_total"] or 0),
        "enriched_total": int(enriched["enriched_total"] or 0),
        "active_enriched_total": int(enriched["active_enriched_total"] or 0),
        "rich_source_jobs": int(rich["rich_source_jobs"] or 0),
        "rich_source_canonical_total": int(
            rich["rich_source_canonical_total"] or 0
        ),
        "without_rich_source_canonical_total": max(
            0,
            int(canonical["canonical_total"] or 0)
            - int(rich["rich_source_canonical_total"] or 0),
        ),
        "llm_enriched_total": int(llm["llm_enriched_total"] or 0),
    }


def llm_usage_observability(conn) -> dict:
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS calls,
          SUM(outcome = 'succeeded') AS succeeded,
          SUM(outcome = 'failed') AS failed,
          SUM(input_tokens) AS input_tokens,
          SUM(output_tokens) AS output_tokens,
          SUM(total_tokens) AS total_tokens,
          SUM(estimated_cost_usd) AS estimated_cost_usd
        FROM opportunity_enrichment_runs
        """
    ).fetchone()
    by_model = [
        dict(item)
        for item in conn.execute(
            """
            SELECT
              model_provider, model_name, prompt_version,
              COUNT(*) AS calls,
              SUM(input_tokens) AS input_tokens,
              SUM(output_tokens) AS output_tokens,
              SUM(total_tokens) AS total_tokens,
              SUM(estimated_cost_usd) AS estimated_cost_usd
            FROM opportunity_enrichment_runs
            GROUP BY model_provider, model_name, prompt_version
            ORDER BY model_provider, model_name, prompt_version
            """
        ).fetchall()
    ]
    return {
        "calls": int(row["calls"] or 0),
        "succeeded": int(row["succeeded"] or 0),
        "failed": int(row["failed"] or 0),
        "input_tokens": int(row["input_tokens"] or 0),
        "output_tokens": int(row["output_tokens"] or 0),
        "total_tokens": int(row["total_tokens"] or 0),
        "estimated_cost_usd": round(float(row["estimated_cost_usd"] or 0), 8),
        "by_model": by_model,
    }


def save_override(
    conn,
    canonical_opportunity_id: int,
    field_path: str,
    operation: str,
    *,
    value=None,
    actor: str,
    reason: str,
    provenance: dict | list | None = None,
    now: str | None = None,
) -> str:
    field_path = clean(field_path)
    actor = clean(actor)
    reason = clean(reason)
    if field_path not in OVERRIDABLE_FIELDS:
        raise EnrichmentValidationError(f"Unsupported override field_path: {field_path}")
    if operation not in {"set", "set_unknown"}:
        raise EnrichmentValidationError(f"Unsupported override operation: {operation}")
    if not actor or not reason:
        raise EnrichmentValidationError("Override actor and reason are required.")
    if operation == "set_unknown" and value is not None:
        raise EnrichmentValidationError("set_unknown cannot include a value.")

    enrichment = conn.execute(
        "SELECT * FROM opportunity_enrichments WHERE canonical_opportunity_id = ?",
        (canonical_opportunity_id,),
    ).fetchone()
    if enrichment is None:
        raise EnrichmentValidationError(
            f"Opportunity {canonical_opportunity_id} must be enriched before an override is stored."
        )
    document = json.loads(enrichment["automatic_document_json"])
    candidate = copy.deepcopy(document)
    apply_override_value(candidate, field_path, operation, value)
    validate_enrichment_document(candidate)

    value_json = canonical_json(value) if operation == "set" else None
    provenance_json = canonical_json(provenance or {})
    existing = conn.execute(
        """
        SELECT * FROM opportunity_enrichment_overrides
        WHERE canonical_opportunity_id = ? AND field_path = ?
        """,
        (canonical_opportunity_id, field_path),
    ).fetchone()
    unchanged = (
        existing is not None
        and existing["operation"] == operation
        and existing["value_json"] == value_json
        and existing["actor"] == actor
        and existing["reason"] == reason
        and existing["provenance_json"] == provenance_json
        and existing["automatic_input_sha256_at_override"] == enrichment["input_sha256"]
    )
    if unchanged:
        return "unchanged"

    timestamp = now or utc_now()
    conn.execute(
        """
        INSERT INTO opportunity_enrichment_overrides (
          canonical_opportunity_id, field_path, operation, value_json,
          actor, reason, provenance_json, automatic_input_sha256_at_override,
          created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_opportunity_id, field_path) DO UPDATE SET
          operation = excluded.operation,
          value_json = excluded.value_json,
          actor = excluded.actor,
          reason = excluded.reason,
          provenance_json = excluded.provenance_json,
          automatic_input_sha256_at_override = excluded.automatic_input_sha256_at_override,
          updated_at = excluded.updated_at
        """,
        (
            canonical_opportunity_id,
            field_path,
            operation,
            value_json,
            actor,
            reason,
            provenance_json,
            enrichment["input_sha256"],
            timestamp,
            timestamp,
        ),
    )
    return "created" if existing is None else "updated"


def resolve_effective_enrichment(conn, canonical_opportunity_id: int) -> dict | None:
    enrichment = conn.execute(
        "SELECT * FROM opportunity_enrichments WHERE canonical_opportunity_id = ?",
        (canonical_opportunity_id,),
    ).fetchone()
    if enrichment is None:
        return None
    overrides = conn.execute(
        """
        SELECT * FROM opportunity_enrichment_overrides
        WHERE canonical_opportunity_id = ?
        ORDER BY field_path
        """,
        (canonical_opportunity_id,),
    ).fetchall()
    return _resolve_effective_enrichment_rows(enrichment, overrides)


def resolve_effective_enrichments(
    conn,
    canonical_opportunity_ids,
) -> dict[int, dict]:
    """Resolve effective enrichment for many canonicals without per-row queries."""

    canonical_ids = sorted({int(value) for value in canonical_opportunity_ids})
    if not canonical_ids:
        return {}

    enrichments = {}
    overrides = defaultdict(list)
    chunk_size = 500
    for offset in range(0, len(canonical_ids), chunk_size):
        chunk = canonical_ids[offset : offset + chunk_size]
        placeholders = ",".join("?" for _value in chunk)
        for row in conn.execute(
            "SELECT * FROM opportunity_enrichments "
            f"WHERE canonical_opportunity_id IN ({placeholders})",
            chunk,
        ).fetchall():
            enrichments[int(row["canonical_opportunity_id"])] = row
        for row in conn.execute(
            "SELECT * FROM opportunity_enrichment_overrides "
            f"WHERE canonical_opportunity_id IN ({placeholders}) "
            "ORDER BY canonical_opportunity_id, field_path",
            chunk,
        ).fetchall():
            overrides[int(row["canonical_opportunity_id"])].append(row)

    return {
        canonical_id: _resolve_effective_enrichment_rows(
            enrichment,
            overrides.get(canonical_id, ()),
        )
        for canonical_id, enrichment in enrichments.items()
    }


def _resolve_effective_enrichment_rows(enrichment, overrides) -> dict:
    document = json.loads(enrichment["automatic_document_json"])
    validate_enrichment_document(document)
    sources = {path: "automatic" for path in OVERRIDABLE_FIELDS}
    applied = []
    stale = []
    for row in overrides:
        value = json.loads(row["value_json"]) if row["operation"] == "set" else None
        apply_override_value(document, row["field_path"], row["operation"], value)
        sources[row["field_path"]] = "human_override"
        applied.append(row["field_path"])
        if row["automatic_input_sha256_at_override"] != enrichment["input_sha256"]:
            stale.append(row["field_path"])
    if applied:
        validate_enrichment_document(document)
    return {
        "document": document,
        "field_sources": sources,
        "overridden_fields": applied,
        "stale_override_fields": stale,
        "automatic_input_sha256": enrichment["input_sha256"],
    }


def apply_override_value(document: dict, field_path: str, operation: str, value) -> None:
    document["field_evidence"] = [
        item
        for item in document.get("field_evidence", [])
        if item.get("field_path") != field_path
    ]
    unknown = set(document.get("unknown_fields") or [])
    if operation == "set_unknown":
        set_path(document, field_path, copy.deepcopy(FIELD_DEFAULTS[field_path]))
        unknown.add(field_path)
    else:
        set_path(document, field_path, copy.deepcopy(value))
        unknown.discard(field_path)
    document["unknown_fields"] = sorted(unknown)


def import_reviewed_overlay(conn, path: Path) -> dict:
    from wahojobs.matching.metadata_overlay import validate_overlay_payload

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_overlay_payload(data, Path(path))
    grouped = defaultdict(list)
    unresolved = 0
    for record in data["records"]:
        resolved = resolve_overlay_record(conn, record)
        if resolved is None:
            unresolved += 1
            continue
        canonical_id, job_id = resolved
        grouped[canonical_id].append((record, job_id))

    summary = Counter()
    summary["records_total"] = len(data["records"])
    summary["records_resolved"] = len(data["records"]) - unresolved
    summary["records_unresolved"] = unresolved
    for canonical_id, resolved_records in sorted(grouped.items()):
        linked_job_ids = {
            int(row["id"])
            for row in conn.execute(
                """
                SELECT id FROM jobs
                WHERE canonical_opportunity_id = ?
                  AND title NOT LIKE '[SIMULATION]%'
                """,
                (canonical_id,),
            ).fetchall()
        }
        reviewed_job_ids = {job_id for _record, job_id in resolved_records if job_id is not None}
        canonical_keyed = any(
            clean(record.get("stable_opportunity_key")).startswith("canonical_opportunity_id:")
            for record, _job_id in resolved_records
        )
        if len(linked_job_ids) > 1 and not canonical_keyed and not linked_job_ids.issubset(reviewed_job_ids):
            summary["records_skipped_multi_variant"] += len(resolved_records)
            continue

        field_candidates = defaultdict(dict)
        for record, _job_id in resolved_records:
            languages = overlay_language_value(record)
            if languages:
                field_candidates["attributes.requirements.languages"][canonical_json(languages)] = languages
            restrictions = sorted(unique_strings(record.get("location_restriction") or []))
            if restrictions:
                field_candidates["attributes.work_arrangement.eligible_locations"][canonical_json(restrictions)] = restrictions

        provenance = {
            "imported_from": str(path),
            "overlay_records": [
                {
                    "stable_opportunity_key": record.get("stable_opportunity_key"),
                    "provenance": record.get("provenance") or [],
                    "warnings": record.get("warnings") or [],
                }
                for record, _job_id in resolved_records
            ],
        }
        review_ids = sorted(
            {
                clean(item.get("review_id"))
                for record, _job_id in resolved_records
                for item in record.get("provenance") or []
                if clean(item.get("review_id"))
            }
        )
        reason = "Imported reviewed opportunity metadata"
        if review_ids:
            reason += ": " + ", ".join(review_ids)

        imported_group = False
        for field_path, values_by_json in sorted(field_candidates.items()):
            if len(values_by_json) != 1:
                summary["field_conflicts"] += 1
                continue
            value = next(iter(values_by_json.values()))
            outcome = save_override(
                conn,
                canonical_id,
                field_path,
                "set",
                value=value,
                actor="metadata_overlay_import",
                reason=reason,
                provenance=provenance,
            )
            summary[f"fields_{outcome}"] += 1
            imported_group = True
        if imported_group:
            summary["canonical_opportunities_imported"] += 1
            summary["records_imported"] += len(resolved_records)

    return {
        key: int(summary[key])
        for key in (
            "records_total",
            "records_resolved",
            "records_unresolved",
            "records_imported",
            "records_skipped_multi_variant",
            "canonical_opportunities_imported",
            "fields_created",
            "fields_updated",
            "fields_unchanged",
            "field_conflicts",
        )
    }


def resolve_overlay_record(conn, record: dict) -> tuple[int, int | None] | None:
    source = clean(record.get("source"))
    candidates = []
    job_id = integer_or_none(record.get("job_id"))
    if job_id is not None:
        candidates.append(("j.id = ?", job_id))
    if clean(record.get("external_id")):
        candidates.append(("j.external_id = ?", clean(record.get("external_id"))))
    if clean(record.get("source_hash")):
        candidates.append(("j.source_hash = ?", clean(record.get("source_hash"))))
    for predicate, value in candidates:
        row = conn.execute(
            f"""
            SELECT j.id, j.canonical_opportunity_id
            FROM jobs j JOIN companies c ON c.id = j.company_id
            WHERE c.slug = ? AND {predicate}
            ORDER BY j.id LIMIT 1
            """,
            (source, value),
        ).fetchone()
        if row is not None and row["canonical_opportunity_id"] is not None:
            return int(row["canonical_opportunity_id"]), int(row["id"])

    canonical_id = integer_or_none(record.get("canonical_opportunity_id"))
    if canonical_id is not None:
        exists = conn.execute(
            "SELECT 1 FROM canonical_opportunities WHERE id = ?",
            (canonical_id,),
        ).fetchone()
        if exists is not None:
            return canonical_id, job_id
    return None


def overlay_language_value(record: dict) -> list[dict]:
    language_values = [
        normalize_language_name(value)
        for value in record.get("required_languages") or []
        if normalize_language_name(value)
    ]
    locale_by_language = {}
    for value in record.get("language_locale") or []:
        match = re.fullmatch(r"\s*([^()]+?)\s*\(([^()]+)\)\s*", str(value))
        if not match:
            continue
        language = normalize_language_name(match.group(1))
        locale_by_language[language] = clean(match.group(2)) or None
        if language:
            language_values.append(language)
    languages = sorted(set(language_values))
    if not languages:
        return []
    title = clean(record.get("title"))
    mentions = find_language_mentions(title)
    mode = requirement_mode_for_mentions(title, mentions)
    if len(languages) == 1 and mode == "none":
        mode = "single"
    return [
        {
            "language": language,
            "locale": locale_by_language.get(language),
            "requirement_mode": mode,
        }
        for language in languages
    ]


def validate_enrichment_document(document: dict) -> None:
    schema_version = document.get("schema_version") if type(document) is dict else None
    if schema_version == SCHEMA_VERSION:
        document_keys = {
            "schema_version",
            "taxonomy_version",
            "source",
            "attributes",
            "field_evidence",
            "variant_facts",
            "unknown_fields",
        }
    elif schema_version == LEGACY_SCHEMA_VERSION:
        document_keys = {
            "schema_version",
            "taxonomy_version",
            "source",
            "attributes",
            "field_evidence",
            "unknown_fields",
        }
    else:
        raise EnrichmentValidationError("Unsupported enrichment schema_version.")
    require_exact_keys(
        document,
        document_keys,
        "document",
    )
    if document["taxonomy_version"] != TAXONOMY_VERSION:
        raise EnrichmentValidationError("Unsupported enrichment taxonomy_version.")
    source = document["source"]
    require_exact_keys(
        source,
        {
            "company_name",
            "company_slug",
            "canonical_key",
            "canonical_title",
            "source_category",
            "source_tier",
            "inventory_model",
            "market_count_policy",
            "opportunity_kinds",
            "availability_bases",
            "include_in_live_market_estimate",
        },
        "source",
    )
    for field in (
        "company_name",
        "company_slug",
        "canonical_key",
        "canonical_title",
        "source_category",
        "source_tier",
        "inventory_model",
        "market_count_policy",
    ):
        require_optional_string(source[field], f"source.{field}")
    require_string_list(source["opportunity_kinds"], "source.opportunity_kinds")
    require_string_list(source["availability_bases"], "source.availability_bases")
    if (
        source["include_in_live_market_estimate"] is not None
        and type(source["include_in_live_market_estimate"]) is not bool
    ):
        raise EnrichmentValidationError(
            "source.include_in_live_market_estimate must be boolean or null."
        )
    attributes = document["attributes"]
    require_exact_keys(attributes, {"role", "work_arrangement", "requirements", "compensation", "application", "content"}, "attributes")

    role = attributes["role"]
    require_exact_keys(role, {"role_family", "professional_domains", "work_activities", "specializations", "seniority"}, "attributes.role")
    require_optional_enum(role["role_family"], ROLE_FAMILIES, "attributes.role.role_family")
    require_enum_list(role["professional_domains"], PROFESSIONAL_DOMAINS, "attributes.role.professional_domains")
    require_enum_list(role["work_activities"], WORK_ACTIVITIES, "attributes.role.work_activities")
    require_string_list(role["specializations"], "attributes.role.specializations")
    require_enum(role["seniority"], SENIORITY_VALUES, "attributes.role.seniority")

    arrangement = attributes["work_arrangement"]
    require_exact_keys(arrangement, {"workplace_mode", "location_scope", "eligible_countries", "eligible_regions", "eligible_locations", "engagement_type", "schedule_type", "hours_per_week_min", "hours_per_week_max", "duration"}, "attributes.work_arrangement")
    require_enum(arrangement["workplace_mode"], WORKPLACE_MODES, "attributes.work_arrangement.workplace_mode")
    require_enum(arrangement["location_scope"], LOCATION_SCOPES, "attributes.work_arrangement.location_scope")
    require_enum_list(
        arrangement["eligible_countries"],
        CANONICAL_COUNTRIES,
        "attributes.work_arrangement.eligible_countries",
    )
    require_enum_list(
        arrangement["eligible_regions"],
        REGIONAL_LOCATION_TOKENS,
        "attributes.work_arrangement.eligible_regions",
    )
    require_string_list(
        arrangement["eligible_locations"],
        "attributes.work_arrangement.eligible_locations",
    )
    require_enum(arrangement["engagement_type"], ENGAGEMENT_TYPES, "attributes.work_arrangement.engagement_type")
    require_enum(arrangement["schedule_type"], SCHEDULE_TYPES, "attributes.work_arrangement.schedule_type")
    for field in ("hours_per_week_min", "hours_per_week_max"):
        require_optional_number(arrangement[field], f"attributes.work_arrangement.{field}", minimum=0, maximum=168)
    require_optional_string(arrangement["duration"], "attributes.work_arrangement.duration")

    requirements = attributes["requirements"]
    requirement_keys = {
        "languages",
        "skills_required",
        "skills_preferred",
        "education",
        "credentials",
        "licenses",
        "years_experience_min",
    }
    if schema_version == SCHEMA_VERSION:
        requirement_keys.update(
            {
                "credentials_preferred",
                "licenses_preferred",
                "experience_required",
                "experience_preferred",
                "current_status_requirements",
                "years_experience_preferred_min",
            }
        )
    require_exact_keys(requirements, requirement_keys, "attributes.requirements")
    require_language_list(requirements["languages"])
    text_list_fields = [
        "skills_required",
        "skills_preferred",
        "credentials",
        "licenses",
    ]
    if schema_version == SCHEMA_VERSION:
        text_list_fields.extend(
            (
                "credentials_preferred",
                "licenses_preferred",
                "experience_required",
                "experience_preferred",
                "current_status_requirements",
            )
        )
    for field in text_list_fields:
        require_string_list(requirements[field], f"attributes.requirements.{field}")
    education = requirements["education"]
    education_keys = {"minimum_level", "accepted_alternatives"}
    if schema_version == SCHEMA_VERSION:
        education_keys.add("preferred_levels")
    require_exact_keys(education, education_keys, "attributes.requirements.education")
    require_enum(education["minimum_level"], EDUCATION_LEVELS, "attributes.requirements.education.minimum_level")
    require_enum_list(education["accepted_alternatives"], EDUCATION_LEVELS - {"unknown"}, "attributes.requirements.education.accepted_alternatives")
    if schema_version == SCHEMA_VERSION:
        require_enum_list(education["preferred_levels"], EDUCATION_LEVELS - {"unknown"}, "attributes.requirements.education.preferred_levels")
    require_optional_number(requirements["years_experience_min"], "attributes.requirements.years_experience_min", minimum=0, maximum=80)
    if schema_version == SCHEMA_VERSION:
        require_optional_number(requirements["years_experience_preferred_min"], "attributes.requirements.years_experience_preferred_min", minimum=0, maximum=80)

    compensation = attributes["compensation"]
    require_exact_keys(compensation, {"disclosed", "currency", "amount_min", "amount_max", "period", "amount_type", "notes"}, "attributes.compensation")
    if compensation["disclosed"] is not None and type(compensation["disclosed"]) is not bool:
        raise EnrichmentValidationError("attributes.compensation.disclosed must be boolean or null.")
    require_optional_enum(
        compensation["currency"],
        ISO_4217_CURRENCIES,
        "attributes.compensation.currency",
    )
    require_optional_number(compensation["amount_min"], "attributes.compensation.amount_min", minimum=0)
    require_optional_number(compensation["amount_max"], "attributes.compensation.amount_max", minimum=0)
    require_enum(compensation["period"], COMPENSATION_PERIODS, "attributes.compensation.period")
    require_enum(compensation["amount_type"], COMPENSATION_AMOUNT_TYPES, "attributes.compensation.amount_type")
    require_optional_string(compensation["notes"], "attributes.compensation.notes")

    application = attributes["application"]
    require_exact_keys(application, {"application_url", "deadline", "assessment_required", "portfolio_or_sample_required", "login_required"}, "attributes.application")
    require_optional_string(application["application_url"], "attributes.application.application_url")
    require_optional_string(application["deadline"], "attributes.application.deadline")
    for field in ("assessment_required", "portfolio_or_sample_required", "login_required"):
        if application[field] is not None and type(application[field]) is not bool:
            raise EnrichmentValidationError(f"attributes.application.{field} must be boolean or null.")

    content = attributes["content"]
    require_exact_keys(content, {"quick_take", "responsibilities", "candidate_profile", "benefits", "caveats"}, "attributes.content")
    require_optional_string(content["quick_take"], "attributes.content.quick_take")
    require_optional_string(content["candidate_profile"], "attributes.content.candidate_profile")
    for field in ("responsibilities", "benefits", "caveats"):
        require_string_list(content[field], f"attributes.content.{field}")

    require_string_list(document["unknown_fields"], "unknown_fields")
    if any(path not in OVERRIDABLE_FIELDS for path in document["unknown_fields"]):
        raise EnrichmentValidationError("unknown_fields contains an unsupported field path.")
    if type(document["field_evidence"]) is not list:
        raise EnrichmentValidationError("field_evidence must be a list.")
    for index, item in enumerate(document["field_evidence"]):
        expected_evidence_keys = {
            "field_path",
            "source_ref",
            "evidence_text",
            "basis",
            "confidence",
        }
        if schema_version == SCHEMA_VERSION:
            expected_evidence_keys.update(
                {"evidence_block_id", "variant_refs", "authority_refs"}
            )
        require_exact_keys(item, expected_evidence_keys, f"field_evidence[{index}]")
        if item["field_path"] not in OVERRIDABLE_FIELDS:
            raise EnrichmentValidationError("field_evidence contains an unsupported field path.")
        for field in ("source_ref", "evidence_text"):
            if type(item[field]) is not str or not item[field].strip():
                raise EnrichmentValidationError(f"field_evidence[{index}].{field} must be non-empty text.")
        require_enum(item["basis"], EVIDENCE_BASES, f"field_evidence[{index}].basis")
        require_enum(item["confidence"], CONFIDENCE_VALUES, f"field_evidence[{index}].confidence")
        if schema_version == SCHEMA_VERSION:
            if not re.fullmatch(r"E[0-9a-f]{16}", item["evidence_block_id"]):
                raise EnrichmentValidationError(
                    f"field_evidence[{index}].evidence_block_id is invalid."
                )
            require_reference_list(
                item["variant_refs"], f"field_evidence[{index}].variant_refs"
            )
            require_reference_list(
                item["authority_refs"], f"field_evidence[{index}].authority_refs"
            )

    if schema_version == SCHEMA_VERSION:
        validate_variant_facts(document["variant_facts"])


def validate_variant_facts(value) -> None:
    if type(value) is not list:
        raise EnrichmentValidationError("variant_facts must be a list.")
    normalized = normalize_variant_facts(value)
    if value != normalized:
        raise EnrichmentValidationError("variant_facts must be normalized and unique.")
    for index, fact in enumerate(value):
        path = f"variant_facts[{index}]"
        require_exact_keys(
            fact,
            {
                "field_path",
                "value",
                "knowledge_state",
                "variant_refs",
                "evidence",
            },
            path,
        )
        field_path = fact["field_path"]
        if field_path not in OVERRIDABLE_FIELDS:
            raise EnrichmentValidationError(f"{path}.field_path is unsupported.")
        require_enum(fact["knowledge_state"], KNOWLEDGE_STATES, f"{path}.knowledge_state")
        require_reference_list(fact["variant_refs"], f"{path}.variant_refs", nonempty=True)
        if fact["knowledge_state"] == "known_empty":
            if field_path not in KNOWN_EMPTY_FIELD_PATHS or fact["value"] != []:
                raise EnrichmentValidationError(f"{path} has an invalid known-empty value.")
        else:
            probe = blank_document()
            probe_value = (
                [copy.deepcopy(fact["value"])]
                if field_path in ATOMIC_LIST_FIELD_PATHS
                else copy.deepcopy(fact["value"])
            )
            set_path(probe, field_path, probe_value)
            validate_enrichment_document(probe)
        evidence = fact["evidence"]
        if type(evidence) is not list or not evidence:
            raise EnrichmentValidationError(f"{path}.evidence must not be empty.")
        for evidence_index, item in enumerate(evidence):
            evidence_path = f"{path}.evidence[{evidence_index}]"
            require_exact_keys(
                item,
                {
                    "evidence_block_id",
                    "source_refs",
                    "authority_refs",
                    "evidence_text",
                    "basis",
                    "confidence",
                },
                evidence_path,
            )
            if not re.fullmatch(r"E[0-9a-f]{16}", item["evidence_block_id"]):
                raise EnrichmentValidationError(
                    f"{evidence_path}.evidence_block_id is invalid."
                )
            require_reference_list(
                item["source_refs"], f"{evidence_path}.source_refs", nonempty=True
            )
            require_reference_list(
                item["authority_refs"], f"{evidence_path}.authority_refs"
            )
            if type(item["evidence_text"]) is not str or not item["evidence_text"].strip():
                raise EnrichmentValidationError(
                    f"{evidence_path}.evidence_text must be non-empty."
                )
            require_enum(item["basis"], EVIDENCE_BASES, f"{evidence_path}.basis")
            require_enum(
                item["confidence"], CONFIDENCE_VALUES, f"{evidence_path}.confidence"
            )


def add_evidence(
    evidence: list[dict],
    field_path: str,
    source_ref: str,
    evidence_text: str,
    basis: str,
    confidence: str,
    *,
    evidence_block_ref: str | None = None,
    variant_refs=(),
    authority_refs=(),
) -> None:
    evidence_text = clean(evidence_text)
    if not evidence_text:
        return
    evidence.append(
        {
            "field_path": field_path,
            "source_ref": source_ref,
            "evidence_text": evidence_text,
            "basis": basis,
            "confidence": confidence,
            "evidence_block_id": evidence_block_ref
            or evidence_block_id(
                source_ref,
                "field_evidence",
                field_path,
                evidence_text,
            ),
            "variant_refs": sorted(
                {clean(value) for value in variant_refs if clean(value)}
            ),
            "authority_refs": sorted(
                {clean(value) for value in authority_refs if clean(value)}
            ),
        }
    )


def require_exact_keys(value, expected: set[str], path: str) -> None:
    if type(value) is not dict or set(value) != expected:
        raise EnrichmentValidationError(f"{path} must contain exactly {sorted(expected)}.")


def require_enum(value, allowed, path: str) -> None:
    if type(value) is not str or value not in allowed:
        raise EnrichmentValidationError(f"{path} contains an unsupported value.")


def require_optional_enum(value, allowed, path: str) -> None:
    if value is not None:
        require_enum(value, allowed, path)


def require_string_list(value, path: str) -> None:
    if type(value) is not list or any(type(item) is not str or not item.strip() for item in value):
        raise EnrichmentValidationError(f"{path} must be a list of non-empty strings.")
    if value != sorted(set(value), key=lambda item: item.casefold()):
        raise EnrichmentValidationError(f"{path} must be sorted and unique.")


def require_reference_list(value, path: str, *, nonempty=False) -> None:
    require_string_list(value, path)
    if nonempty and not value:
        raise EnrichmentValidationError(f"{path} must not be empty.")
    if any(
        re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}:[^\s]{1,256}", item) is None
        for item in value
    ):
        raise EnrichmentValidationError(f"{path} contains an invalid reference.")


def require_enum_list(value, allowed, path: str) -> None:
    require_string_list(value, path)
    if any(item not in allowed for item in value):
        raise EnrichmentValidationError(f"{path} contains an unsupported value.")


def require_language_list(value) -> None:
    if type(value) is not list:
        raise EnrichmentValidationError("attributes.requirements.languages must be a list.")
    keys = []
    for index, item in enumerate(value):
        require_exact_keys(item, {"language", "locale", "requirement_mode"}, f"attributes.requirements.languages[{index}]")
        if type(item["language"]) is not str or not item["language"].strip():
            raise EnrichmentValidationError("Language names must be non-empty strings.")
        require_optional_string(item["locale"], f"attributes.requirements.languages[{index}].locale")
        require_enum(item["requirement_mode"], LANGUAGE_REQUIREMENT_MODES, f"attributes.requirements.languages[{index}].requirement_mode")
        keys.append((item["language"].casefold(), clean(item["locale"]).casefold()))
    if keys != sorted(set(keys)):
        raise EnrichmentValidationError("Language requirements must be sorted and unique.")


def require_optional_string(value, path: str) -> None:
    if value is not None and (type(value) is not str or not value.strip()):
        raise EnrichmentValidationError(f"{path} must be non-empty text or null.")


def require_optional_number(value, path: str, *, minimum=None, maximum=None) -> None:
    if value is None:
        return
    if type(value) not in {int, float}:
        raise EnrichmentValidationError(f"{path} must be numeric or null.")
    if minimum is not None and value < minimum:
        raise EnrichmentValidationError(f"{path} is below its minimum.")
    if maximum is not None and value > maximum:
        raise EnrichmentValidationError(f"{path} is above its maximum.")


def get_path(document: dict, field_path: str):
    value = document
    for part in field_path.split("."):
        value = value[part]
    return value


def set_path(document: dict, field_path: str, value) -> None:
    parts = field_path.split(".")
    target = document
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


def field_is_unknown(value, default) -> bool:
    return value == default


def refresh_unknown_fields(document: dict) -> None:
    evidenced_paths = {
        item["field_path"] for item in document.get("field_evidence") or []
    }
    document["unknown_fields"] = sorted(
        path
        for path, default in FIELD_DEFAULTS.items()
        if field_is_unknown(get_path(document, path), default)
        and path not in evidenced_paths
    )


def contains_term(text: str, term: str) -> bool:
    normalized = normalize_text(term)
    if not normalized:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", text) is not None


def normalize_text(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def unique_strings(values) -> list[str]:
    result = []
    seen = set()
    for value in values or []:
        text = clean(value)
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return sorted(result, key=lambda item: item.casefold())


def common_boolean(values) -> bool | None:
    unique = {value for value in values if type(value) is bool}
    return next(iter(unique)) if len(unique) == 1 else None


def integer_or_none(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_decimal(value):
    if not value:
        return None
    normalized = str(value).replace(",", ".")
    try:
        number = float(normalized)
    except ValueError:
        return None
    return int(number) if number.is_integer() else round(number, 2)
