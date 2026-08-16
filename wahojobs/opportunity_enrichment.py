"""Durable deterministic opportunity enrichment and field-level overrides."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from wahojobs.matching.domains import detect_role_domains
from wahojobs.matching.languages import (
    find_language_mentions,
    normalize_language_name,
    requirement_mode_for_mentions,
)
from wahojobs.matching.locations import (
    classify_job_location,
    countries_in_location,
    regions_in_location,
)
from wahojobs.matching.specializations import specialization_requirements
from wahojobs.matching.taxonomy import OCCUPATIONAL_FAMILIES


SCHEMA_VERSION = "opportunity_enrichment_v2"
TAXONOMY_VERSION = "opportunity_taxonomy_v2_2026_08"
EXTRACTOR_VERSION = "deterministic_v1"

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
SENIORITY_VALUES = frozenset(
    {"unknown", "internship", "entry", "mid", "senior", "lead", "principal", "manager"}
)
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
EVIDENCE_BASES = frozenset({"source_explicit", "deterministic_parse", "deterministic_classification"})
CONFIDENCE_VALUES = frozenset({"low", "medium", "high"})


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
                },
                "credentials": [],
                "licenses": [],
                "years_experience_min": None,
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


def load_semantic_input(conn, canonical_opportunity_id: int) -> dict:
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

    rows = conn.execute(
        """
        SELECT
          title, location, department, expertise, commitment, url,
          opportunity_kind, availability_basis, include_in_live_market_estimate,
          is_active
        FROM jobs
        WHERE canonical_opportunity_id = ?
          AND title NOT LIKE '[SIMULATION]%'
        ORDER BY source_hash ASC, id ASC
        """,
        (canonical_opportunity_id,),
    ).fetchall()
    active_rows = [row for row in rows if row["is_active"]]
    selected = active_rows or list(rows)
    variants = []
    seen = set()
    for row in selected:
        variant = {
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
        key = canonical_json(variant)
        if key in seen:
            continue
        seen.add(key)
        variants.append(variant)
    variants.sort(key=canonical_json)

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
    }


def semantic_input_sha256(semantic_input: dict) -> str:
    return hashlib.sha256(canonical_json(semantic_input).encode("utf-8")).hexdigest()


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
        add_evidence(evidence, "attributes.role.role_family", "title", title_text or category_text, "deterministic_classification", "medium")

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
        add_evidence(evidence, "attributes.role.professional_domains", "role_text", f"{title_text} | {category_text}".strip(" |"), "deterministic_classification", "medium")

    activities = classify_many(role_text, WORK_ACTIVITY_RULES)
    if activities:
        attributes["role"]["work_activities"] = activities
        add_evidence(evidence, "attributes.role.work_activities", "role_text", f"{title_text} | {category_text}".strip(" |"), "deterministic_classification", "medium")

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
        add_evidence(evidence, "attributes.role.specializations", "title", title_text, "deterministic_parse", "high")

    seniority = parse_seniority(role_text)
    if seniority != "unknown":
        attributes["role"]["seniority"] = seniority
        add_evidence(evidence, "attributes.role.seniority", "title", title_text, "deterministic_parse", "high")

    extract_location(attributes["work_arrangement"], evidence, fields["location"])
    commitment_text = " | ".join(fields["commitment"])
    extract_engagement(attributes["work_arrangement"], evidence, commitment_text)
    extract_languages(attributes["requirements"], evidence, title_text, canonical)
    extract_compensation(attributes["compensation"], evidence, commitment_text)
    extract_application(attributes["application"], evidence, semantic_input)

    document["unknown_fields"] = sorted(
        path
        for path, default in FIELD_DEFAULTS.items()
        if field_is_unknown(get_path(document, path), default)
    )
    document["field_evidence"] = sorted(
        evidence,
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


def extract_location(target: dict, evidence: list[dict], locations: list[str]) -> None:
    location_text = " | ".join(locations)
    if not location_text:
        return
    scope, remote_status, requirements, _restriction_type = classify_job_location(location_text)
    workplace_mode = {
        "remote": "remote",
        "hybrid": "hybrid",
        "onsite": "onsite",
    }.get(remote_status, "unknown")
    if workplace_mode != "unknown":
        target["workplace_mode"] = workplace_mode
        add_evidence(evidence, "attributes.work_arrangement.workplace_mode", "location", location_text, "deterministic_parse", "high")
    if scope in LOCATION_SCOPES and scope != "unknown":
        target["location_scope"] = scope
        add_evidence(evidence, "attributes.work_arrangement.location_scope", "location", location_text, "deterministic_parse", "high")
    countries = sorted(countries_in_location(location_text))
    regions = sorted(regions_in_location(location_text))
    if countries:
        target["eligible_countries"] = countries
        add_evidence(evidence, "attributes.work_arrangement.eligible_countries", "location", location_text, "deterministic_parse", "high")
    if regions:
        target["eligible_regions"] = regions
        add_evidence(evidence, "attributes.work_arrangement.eligible_regions", "location", location_text, "deterministic_parse", "high")
    if requirements:
        target["eligible_locations"] = unique_strings([requirements])
        add_evidence(evidence, "attributes.work_arrangement.eligible_locations", "location", location_text, "source_explicit", "high")


def extract_engagement(target: dict, evidence: list[dict], text: str) -> None:
    normalized = normalize_text(text)
    if not normalized:
        return
    rules = (
        ("internship", ("internship", "intern")),
        ("volunteer", ("volunteer",)),
        ("freelance", ("freelance", "independent contractor")),
        ("temporary", ("temporary",)),
        ("contract", ("contract", "project based", "project-based")),
        ("part_time", ("part time", "part-time")),
        ("full_time", ("full time", "full-time")),
    )
    for value, terms in rules:
        if any(contains_term(normalized, term) for term in terms):
            target["engagement_type"] = value
            add_evidence(evidence, "attributes.work_arrangement.engagement_type", "commitment", text, "deterministic_parse", "high")
            break
    if contains_term(normalized, "flexible"):
        target["schedule_type"] = "flexible"
        add_evidence(evidence, "attributes.work_arrangement.schedule_type", "commitment", text, "deterministic_parse", "high")
    elif any(contains_term(normalized, term) for term in ("fixed schedule", "fixed hours", "shift")):
        target["schedule_type"] = "fixed"
        add_evidence(evidence, "attributes.work_arrangement.schedule_type", "commitment", text, "deterministic_parse", "high")

    hours = re.search(
        r"(?<!\d)(\d{1,3})(?:\s*(?:-|to)\s*(\d{1,3}))?\s*(?:hours?|hrs?)\s*(?:per|/)?\s*week",
        normalized,
    )
    if hours:
        minimum = int(hours.group(1))
        maximum = int(hours.group(2) or hours.group(1))
        if 0 < minimum <= maximum <= 168:
            target["hours_per_week_min"] = minimum
            target["hours_per_week_max"] = maximum
            add_evidence(evidence, "attributes.work_arrangement.hours_per_week_min", "commitment", text, "deterministic_parse", "high")
            add_evidence(evidence, "attributes.work_arrangement.hours_per_week_max", "commitment", text, "deterministic_parse", "high")

    duration = re.search(r"(?<!\d)(\d{1,3})\s*(day|week|month|year)s?\b", normalized)
    if duration:
        target["duration"] = duration.group(0)
        add_evidence(evidence, "attributes.work_arrangement.duration", "commitment", text, "deterministic_parse", "high")


def extract_languages(target: dict, evidence: list[dict], title_text: str, canonical: dict) -> None:
    language_text = " ".join(
        value
        for value in (title_text, canonical.get("language") or "", canonical.get("language_locale") or "")
        if value
    )
    mentions = find_language_mentions(language_text)
    languages = {mention["language"] for mention in mentions}
    canonical_language = normalize_language_name(canonical.get("language"))
    if canonical_language:
        languages.add(canonical_language)
    if not languages:
        return
    mode = requirement_mode_for_mentions(language_text, mentions)
    if len(languages) == 1 and mode == "none":
        mode = "single"
    locale = clean(canonical.get("language_locale"))
    entries = []
    for language in sorted(languages):
        entries.append(
            {
                "language": language,
                "locale": locale if locale and language == canonical_language else None,
                "requirement_mode": mode,
            }
        )
    target["languages"] = entries
    add_evidence(evidence, "attributes.requirements.languages", "title", language_text, "deterministic_parse", "high")


def extract_compensation(target: dict, evidence: list[dict], text: str) -> None:
    normalized = normalize_text(text)
    if not normalized:
        return
    rate_signal = re.search(r"(?:rate|pay|salary|compensation|[$€£])", normalized)
    if not rate_signal:
        return
    target["notes"] = text
    add_evidence(evidence, "attributes.compensation.notes", "commitment", text, "source_explicit", "high")

    amount_match = re.search(
        r"(?:(usd|cad|aud|eur|gbp|us\$|c\$|a\$|[$€£])\s*)?"
        r"(\d{1,6}(?:[.,]\d{1,2})?)"
        r"(?:\s*(?:-|to)\s*(?:(?:usd|cad|aud|eur|gbp|us\$|c\$|a\$|[$€£])\s*)?"
        r"(\d{1,6}(?:[.,]\d{1,2})?))?",
        normalized,
    )
    if not amount_match:
        return
    minimum = parse_decimal(amount_match.group(2))
    maximum = parse_decimal(amount_match.group(3)) if amount_match.group(3) else minimum
    if minimum is None or maximum is None:
        return
    target["disclosed"] = True
    target["amount_min"] = minimum
    target["amount_max"] = maximum
    target["amount_type"] = "range" if amount_match.group(3) else "exact"
    currency = compensation_currency(amount_match.group(1), normalized)
    if currency:
        target["currency"] = currency
    period = compensation_period(normalized)
    if period:
        target["period"] = period
    for field in ("disclosed", "amount_min", "amount_max", "amount_type"):
        add_evidence(evidence, f"attributes.compensation.{field}", "commitment", text, "deterministic_parse", "high")
    if currency:
        add_evidence(evidence, "attributes.compensation.currency", "commitment", text, "deterministic_parse", "high")
    if period:
        add_evidence(evidence, "attributes.compensation.period", "commitment", text, "deterministic_parse", "high")


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


def build_enrichment(semantic_input: dict) -> tuple[str, dict, str]:
    input_sha256 = semantic_input_sha256(semantic_input)
    document = extract_deterministic_document(semantic_input)
    status = STATUS_PARTIAL if document["unknown_fields"] else STATUS_COMPLETE
    return input_sha256, document, status


def enrich_canonical_opportunity(
    conn,
    canonical_opportunity_id: int,
    *,
    now: str | None = None,
    ensure_schema: bool = True,
) -> dict:
    from wahojobs.db.repository import ensure_opportunity_enrichment_schema

    if ensure_schema:
        ensure_opportunity_enrichment_schema(conn)
    semantic_input = load_semantic_input(conn, canonical_opportunity_id)
    input_sha256, document, status = build_enrichment(semantic_input)
    existing = conn.execute(
        "SELECT * FROM opportunity_enrichments WHERE canonical_opportunity_id = ?",
        (canonical_opportunity_id,),
    ).fetchone()
    if (
        existing is not None
        and existing["input_sha256"] == input_sha256
        and existing["schema_version"] == SCHEMA_VERSION
        and existing["taxonomy_version"] == TAXONOMY_VERSION
        and existing["extractor_version"] == EXTRACTOR_VERSION
    ):
        return {
            "canonical_opportunity_id": canonical_opportunity_id,
            "outcome": "unchanged",
            "status": existing["status"],
            "input_sha256": input_sha256,
            "document": json.loads(existing["automatic_document_json"]),
        }

    generated_at = now or utc_now()
    document_json = canonical_json(document)
    outcome = "created" if existing is None else "updated"
    conn.execute(
        """
        INSERT INTO opportunity_enrichments (
          canonical_opportunity_id, schema_version, taxonomy_version,
          extractor_version, input_sha256, status, automatic_document_json,
          model_provider, model_name, prompt_version, generated_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
        ON CONFLICT(canonical_opportunity_id) DO UPDATE SET
          schema_version = excluded.schema_version,
          taxonomy_version = excluded.taxonomy_version,
          extractor_version = excluded.extractor_version,
          input_sha256 = excluded.input_sha256,
          status = excluded.status,
          automatic_document_json = excluded.automatic_document_json,
          model_provider = NULL,
          model_name = NULL,
          prompt_version = NULL,
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
            generated_at,
            generated_at,
        ),
    )
    return {
        "canonical_opportunity_id": canonical_opportunity_id,
        "outcome": outcome,
        "status": status,
        "input_sha256": input_sha256,
        "document": document,
    }


def enrich_company_opportunities(conn, company_id: int) -> dict:
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
    return summarize_enrichment_results(
        [
            enrich_canonical_opportunity(conn, item, ensure_schema=False)
            for item in ids
        ]
    )


def enrich_all_opportunities(conn) -> dict:
    from wahojobs.db.repository import ensure_opportunity_enrichment_schema

    ensure_opportunity_enrichment_schema(conn)
    ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM canonical_opportunities ORDER BY id"
        ).fetchall()
    ]
    return summarize_enrichment_results(
        [
            enrich_canonical_opportunity(conn, item, ensure_schema=False)
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
    return {
        "total": len(results),
        "created": outcomes["created"],
        "updated": outcomes["updated"],
        "unchanged": outcomes["unchanged"],
        "complete": statuses[STATUS_COMPLETE],
        "partial": statuses[STATUS_PARTIAL],
        "failed": statuses[STATUS_FAILED],
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
    return {
        "jobs_total": int(row["jobs_total"] or 0),
        "jobs_canonicalized": int(row["jobs_canonicalized"] or 0),
        "active_jobs_total": int(row["active_jobs_total"] or 0),
        "active_jobs_canonicalized": int(row["active_jobs_canonicalized"] or 0),
        "canonical_total": int(canonical["canonical_total"] or 0),
        "active_canonical_total": int(canonical["active_canonical_total"] or 0),
        "enriched_total": int(enriched["enriched_total"] or 0),
        "active_enriched_total": int(enriched["active_enriched_total"] or 0),
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
    document = json.loads(enrichment["automatic_document_json"])
    validate_enrichment_document(document)
    sources = {path: "automatic" for path in OVERRIDABLE_FIELDS}
    applied = []
    stale = []
    overrides = conn.execute(
        """
        SELECT * FROM opportunity_enrichment_overrides
        WHERE canonical_opportunity_id = ?
        ORDER BY field_path
        """,
        (canonical_opportunity_id,),
    ).fetchall()
    for row in overrides:
        value = json.loads(row["value_json"]) if row["operation"] == "set" else None
        apply_override_value(document, row["field_path"], row["operation"], value)
        sources[row["field_path"]] = "human_override"
        applied.append(row["field_path"])
        if row["automatic_input_sha256_at_override"] != enrichment["input_sha256"]:
            stale.append(row["field_path"])
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
    require_exact_keys(
        document,
        {"schema_version", "taxonomy_version", "source", "attributes", "field_evidence", "unknown_fields"},
        "document",
    )
    if document["schema_version"] != SCHEMA_VERSION:
        raise EnrichmentValidationError("Unsupported enrichment schema_version.")
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
    for field in ("eligible_countries", "eligible_regions", "eligible_locations"):
        require_string_list(arrangement[field], f"attributes.work_arrangement.{field}")
    require_enum(arrangement["engagement_type"], ENGAGEMENT_TYPES, "attributes.work_arrangement.engagement_type")
    require_enum(arrangement["schedule_type"], SCHEDULE_TYPES, "attributes.work_arrangement.schedule_type")
    for field in ("hours_per_week_min", "hours_per_week_max"):
        require_optional_number(arrangement[field], f"attributes.work_arrangement.{field}", minimum=0, maximum=168)
    require_optional_string(arrangement["duration"], "attributes.work_arrangement.duration")

    requirements = attributes["requirements"]
    require_exact_keys(requirements, {"languages", "skills_required", "skills_preferred", "education", "credentials", "licenses", "years_experience_min"}, "attributes.requirements")
    require_language_list(requirements["languages"])
    for field in ("skills_required", "skills_preferred", "credentials", "licenses"):
        require_string_list(requirements[field], f"attributes.requirements.{field}")
    education = requirements["education"]
    require_exact_keys(education, {"minimum_level", "accepted_alternatives"}, "attributes.requirements.education")
    require_enum(education["minimum_level"], EDUCATION_LEVELS, "attributes.requirements.education.minimum_level")
    require_enum_list(education["accepted_alternatives"], EDUCATION_LEVELS - {"unknown"}, "attributes.requirements.education.accepted_alternatives")
    require_optional_number(requirements["years_experience_min"], "attributes.requirements.years_experience_min", minimum=0, maximum=80)

    compensation = attributes["compensation"]
    require_exact_keys(compensation, {"disclosed", "currency", "amount_min", "amount_max", "period", "amount_type", "notes"}, "attributes.compensation")
    if compensation["disclosed"] is not None and type(compensation["disclosed"]) is not bool:
        raise EnrichmentValidationError("attributes.compensation.disclosed must be boolean or null.")
    require_optional_string(compensation["currency"], "attributes.compensation.currency")
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
        require_exact_keys(item, {"field_path", "source_ref", "evidence_text", "basis", "confidence"}, f"field_evidence[{index}]")
        if item["field_path"] not in OVERRIDABLE_FIELDS:
            raise EnrichmentValidationError("field_evidence contains an unsupported field path.")
        for field in ("source_ref", "evidence_text"):
            if type(item[field]) is not str or not item[field].strip():
                raise EnrichmentValidationError(f"field_evidence[{index}].{field} must be non-empty text.")
        require_enum(item["basis"], EVIDENCE_BASES, f"field_evidence[{index}].basis")
        require_enum(item["confidence"], CONFIDENCE_VALUES, f"field_evidence[{index}].confidence")


def add_evidence(evidence: list[dict], field_path: str, source_ref: str, evidence_text: str, basis: str, confidence: str) -> None:
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


def compensation_currency(marker: str | None, text: str) -> str | None:
    marker = normalize_text(marker)
    explicit = {
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
    }
    if marker in explicit:
        return explicit[marker]
    for token, currency in explicit.items():
        if token and contains_term(text, token):
            return currency
    return None


def compensation_period(text: str) -> str | None:
    rules = (
        ("hour", ("/hr", "per hour", "hourly")),
        ("day", ("per day", "/day")),
        ("week", ("per week", "/week")),
        ("month", ("per month", "/month")),
        ("year", ("per year", "annual", "annually", "/year")),
        ("project", ("per project", "upon completion")),
        ("asset", ("per approved asset", "per asset")),
        ("source_word", ("per source word",)),
    )
    for period, terms in rules:
        if any(term in text for term in terms):
            return period
    return None
