#!/usr/bin/env python3
"""Preview matches from raw profile text using the baseline canonical normalizer.

This is a local/demo flow only. It does not change matcher scoring, database
rows, fixtures, or product-state data.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import profile_match_digest as matcher  # noqa: E402
from wahojobs.classification import (  # noqa: E402
    INVENTORY_MODEL_EVERGREEN_APPLICATION,
    INVENTORY_MODEL_MIXED,
    INVENTORY_MODEL_PUBLIC_INVENTORY,
    MARKET_COUNT_POLICY_COUNT_LIVE,
    SOURCE_TIER_EXPERIMENTAL,
)
from wahojobs.db.connection import get_connection  # noqa: E402
from wahojobs.matching.metadata_overlay import (  # noqa: E402
    DEFAULT_OVERLAY_PATH,
    apply_overlay_to_rows,
    load_overlay,
)
from wahojobs.matching.fit_evidence import (  # noqa: E402
    SUPPORTED as AFFIRMATIVE_FIT_SUPPORTED,
    assess_affirmative_fit,
    build_profile_fit_evidence,
)
from wahojobs.matching.specializations import (  # noqa: E402
    evaluate_specialization_requirements,
    specialization_evidence,
)
from wahojobs.profiles.canonical import (  # noqa: E402
    canonical_profile_debug_summary,
    canonical_to_matcher_profile,
    validate_canonical_profile,
)
from wahojobs.profiles.normalizer import BaselineHeuristicProfileNormalizer  # noqa: E402


INPUT_STYLES = {
    "short_paragraph",
    "long_paragraph",
    "resume_or_linkedin_style",
    "messy_sparse_input",
}
OUTPUT_FORMATS = {"text", "json", "html"}
SECTION_ORDER = (
    "do_these_first",
    "best_matches",
    "also_worth_reviewing",
    "explore_only",
    "excluded",
)
SECTION_LABELS = {
    "do_these_first": "Do These First",
    "best_matches": "Best Matches",
    "also_worth_reviewing": "Also Worth Reviewing",
    "explore_only": "Explore Only",
    "excluded": "Excluded / Not Personalized",
}
DEFAULT_LIMIT = 5
HTML_SECTION_LIMITS = {
    "do_these_first": 5,
    "best_matches": 8,
    "also_worth_reviewing": 8,
    "explore_only": 8,
    "excluded": 8,
}
UNCONFIRMED_LANGUAGE_TERMS = {
    "american sign language": "american sign language",
    "assamese": "assamese",
    "asl": "american sign language",
    "asturian": "asturian",
    "aymara": "aymara",
    "azeri": "azeri",
    "basque": "basque",
}
LANGUAGE_LOCALE_BASES = (
    "english",
    "spanish",
    "portuguese",
    "french",
    "chinese",
    "mandarin chinese",
)
LANGUAGE_LOCALE_TERMS = (
    ("united states", ("us", "u s", "usa", "united states", "american")),
    ("united kingdom", ("uk", "u k", "united kingdom", "british")),
    ("australia", ("australia", "australian")),
    ("new zealand", ("new zealand", "nz")),
    ("singapore", ("singapore",)),
    ("malta", ("malta",)),
    ("ireland", ("ireland", "irish")),
    ("india", ("india", "indian")),
    ("andean", ("andean", "peru", "bolivia", "ecuador")),
    ("chile", ("chile", "cl")),
    ("rioplatense", ("rioplatense",)),
    ("argentina", ("argentina", "argentinian")),
    ("uruguay", ("uruguay", "uruguayan")),
    ("caribbean", ("caribbean",)),
    ("belize", ("belize",)),
    ("guyana", ("guyana",)),
    ("mexico", ("mexico", "mx")),
    ("colombia", ("colombia", "colombian")),
    ("el salvador", ("el salvador", "salvadoran")),
    ("spain", ("spain", "es")),
    ("latin america", ("latin america", "latin american", "latam", "lat am")),
    ("brazil", ("brazil", "br", "brazilian")),
    ("portugal", ("portugal", "pt")),
    ("canada", ("canada", "canadian")),
    ("france", ("france",)),
)
LOCATION_RESTRICTION_PATTERNS = (
    (r"\bus only\b|\bunited states only\b|\bu s only\b", "US-only"),
    (r"\b(?:us|u s|united states)[-\s]?based\b", "US-based"),
    (r"\buk[-\s]?based\b|\bunited kingdom[-\s]?based\b", "UK-based"),
    (r"\bremote\s*[-–—]\s*(india|brazil|canada|united states|uk|united kingdom)\b", "remote country-specific"),
    (r"\((india)(?:\s*[,;-][^)]*)?\)", "India"),
    (r"\((latam|latin america)\)|\blatam\b|\blatin america\b", "LatAm/Latin America"),
    (r"\b(india|latam|latin america)[-\s]?based\b", "regional"),
    (r"\bonly\s+(cal|ca|california)\s*(and|&)\s*(fl|florida)\b", "California/Florida only"),
)
TITLE_ONLY_LANGUAGE_REQUIREMENTS = (
    ("alexandrian dialect", ("alexandrian",)),
    ("bedawi dialect", ("bedawi",)),
    ("belarusian", ("belarusian",)),
    ("british sign language", ("british sign language", "bsl")),
    ("cebuano", ("cebuano",)),
    ("chichewa", ("chichewa",)),
    ("guarani", ("guarani", "guaraní")),
    ("k'iche'", ("k'iche'", "kiche", "k'iche' mayan")),
    ("kaqchikel", ("kaqchikel", "kaqchikel mayan")),
    ("kurdish kurmanji", ("kurdish kurmanji", "kurmanji")),
    ("kurdish sorani", ("kurdish sorani", "sorani")),
    ("odia", ("odia",)),
)
PRIMARY_PLAN_FALLBACK_LIMIT = 3
PRIMARY_PLAN_BLOCKING_DIAGNOSTIC_PREFIXES = (
    "Detected unsupported language requirement",
    "Possible unconfirmed language requirement",
    "Unsupported title-only language or dialect",
    "Specific language locale/accent may be required",
    "Location or regional eligibility needs confirmation",
    "Specialized annotation or survey domain does not match",
    "Science subdomain appears outside profile specialty",
    "Cross-domain technical role may require",
    "Profile states no biology or medical credentials",
    "Medical license or credential may be required",
    "Credential or education requirement may apply",
    "Unsupported explicit specialization requirements",
)
SCIENCE_MEDICAL_PREVIEW_TERMS = (
    "biology",
    "biologist",
    "biomedical",
    "chemistry",
    "chemical engineering",
    "clinical",
    "dermatology",
    "healthcare",
    "life science",
    "material science",
    "materials science",
    "medical",
    "medicine",
    "microbiology",
    "pharma",
    "physician",
)
CODING_PREVIEW_TERMS = (
    "api",
    "code",
    "coding",
    "developer",
    "engineering",
    "javascript",
    "python",
    "software",
    "typescript",
)
LICENSED_MEDICAL_PREVIEW_TERMS = (
    "licensed physician",
    "medical doctor",
    "physician",
    "physicians",
    "registered nurse",
    "registered nurses",
    "nurse",
    "nurses",
)
CREDENTIAL_REQUIREMENT_PATTERNS = (
    (r"\bba\b|\bbachelor'?s?\b", "bachelor's degree"),
    (r"\bms\b|\bmaster'?s?\b", "master's degree"),
    (r"\bphd\b|\bph\.d\b|\bdoctorate\b", "PhD or doctorate"),
    (r"\bmedical doctor\b|\bphysician\b|\blicensed\b", "medical/professional license"),
)
SPECIALIZED_ANNOTATION_TERMS = (
    "pavement condition index",
    "pci",
    "survey and annotation",
    "survey annotation",
)
SPECIALIZED_ANNOTATION_PROFILE_TERMS = (
    "civil engineering",
    "geospatial",
    "infrastructure",
    "pavement",
    "road survey",
    "survey",
    "transportation",
)
BIOLOGY_PROFILE_TERMS = (
    "biology",
    "biomedical",
    "clinical",
    "life science",
    "medical",
    "medicine",
    "microbiology",
)
BIOLOGY_COMPATIBLE_ROLE_TERMS = (
    "biology",
    "biomedical",
    "clinical",
    "computational biology",
    "medical",
    "medicine",
    "microbiology",
)
UNRELATED_SCIENCE_ROLE_TERMS = (
    "advanced math",
    "chemistry",
    "chemical engineering",
    "computational chemistry",
    "computational physics",
    "material science",
    "materials science",
    "mathematics",
    "optical",
    "physics",
    "semiconductor",
)
ACTIONABILITY_RANK = {
    "excluded": 0,
    "explore_only": 1,
    "also_worth_reviewing": 2,
    "best_matches": 3,
    "do_these_first": 4,
}
DECISIVE_PREVIEW_CAP_RULES = (
    ("Possible unconfirmed language requirement", "explore_only", "unconfirmed_language_requirement"),
    ("Unsupported title-only language or dialect", "excluded", "unsupported_title_language_or_dialect"),
    ("Specific language locale/accent may be required", "also_worth_reviewing", "unconfirmed_language_locale"),
    ("Location or regional eligibility needs confirmation", "explore_only", "unconfirmed_location_restriction"),
    ("Specialized annotation or survey domain does not match", "explore_only", "specialized_annotation_mismatch"),
    ("Science subdomain appears outside profile specialty", "explore_only", "science_subdomain_mismatch"),
    ("Profile states no biology or medical credentials", "explore_only", "absent_science_credentials"),
    ("Medical license or credential may be required", "explore_only", "medical_credential_requirement"),
    ("Unsupported explicit specialization requirements", "also_worth_reviewing", "unsupported_specialization"),
)


def main() -> int:
    args = parse_args()
    raw_input = read_input(args)
    context = build_preview_context(
        raw_input,
        args.input_style,
        limit=args.limit,
        use_overlay=not args.no_overlay,
    )
    rendered = render_context(context, args.format)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
        print(f"Wrote preview to {args.out}")
    else:
        print(rendered)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--input-text", help="Raw profile/background text.")
    input_group.add_argument("--input-file", type=Path, help="Path to raw profile/background text.")
    parser.add_argument(
        "--input-style",
        choices=sorted(INPUT_STYLES),
        default="short_paragraph",
        help="Input style hint for the baseline normalizer.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Maximum opportunities to show per section.",
    )
    parser.add_argument(
        "--format",
        choices=sorted(OUTPUT_FORMATS),
        default="text",
        help="Output format.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Optional output path. No files are written unless this is provided.",
    )
    parser.add_argument(
        "--no-overlay",
        action="store_true",
        help="Disable the reviewed opportunity metadata overlay sidecar.",
    )
    return parser.parse_args()


def read_input(args: argparse.Namespace) -> str:
    if args.input_text is not None:
        value = args.input_text
    else:
        value = args.input_file.read_text(encoding="utf-8")
    if not value.strip():
        raise SystemExit("Profile input is empty.")
    return value.strip()


def build_preview_context(
    raw_input: str,
    input_style: str,
    limit: int = DEFAULT_LIMIT,
    use_overlay: bool = True,
) -> dict:
    normalizer = BaselineHeuristicProfileNormalizer()
    normalization = normalizer.normalize(
        raw_input,
        input_style,
        {
            "profile_id": "preview_profile",
            "display_name": "Preview Profile",
        },
    )
    canonical = normalization.canonical_profile
    validate_canonical_profile(canonical)
    matcher_profile = canonical_to_matcher_profile(canonical)
    matcher_profile["language_locale_keys"] = canonical_language_locale_keys(canonical)
    grouped_matches, overlay_status = build_grouped_matches(matcher_profile, limit, use_overlay=use_overlay)
    canonical_summary = canonical_profile_debug_summary(canonical)
    preview_warnings = build_preview_warnings(normalization.warnings, normalization, grouped_matches)

    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "disclaimer": (
            "BaselineHeuristicProfileNormalizer is heuristic/demo-only. "
            "It may miss resume or LinkedIn facts and must not invent credentials, "
            "licenses, countries, years, or language proficiency."
        ),
        "raw_input": raw_input,
        "input_style": input_style,
        "normalizer": normalizer.name,
        "canonical_profile": canonical,
        "canonical_summary": canonical_summary,
        "matcher_profile": matcher_profile,
        "warnings": preview_warnings,
        "normalization_warnings": normalization.warnings,
        "missing_fields": normalization.missing_fields,
        "ambiguous_fields": normalization.ambiguous_fields,
        "extraction_quality": normalization.extraction_quality,
        "matches": grouped_matches,
        "match_summary": {section: len(grouped_matches[section]) for section in SECTION_ORDER},
        "metadata_overlay": overlay_status,
    }


def build_grouped_matches(profile: dict, limit: int, use_overlay: bool = True) -> tuple[dict, dict]:
    rows, overlay_status = load_preview_rows(use_overlay=use_overlay)
    supported_specializations = specialization_evidence(profile)
    profile_fit_evidence = build_profile_fit_evidence(profile)
    scored = []
    for row in rows:
        match = matcher.score_opportunity(profile, row)
        match = apply_preview_guardrails(
            profile,
            row,
            match,
            supported_specializations=supported_specializations,
            profile_fit_evidence=profile_fit_evidence,
        )
        scored.append(match)

    deduped = dedupe_matches(scored)
    deduped = ensure_safe_do_these_first(deduped, profile)
    groups = {section: [] for section in SECTION_ORDER}
    for match in sorted(deduped, key=match_sort_key):
        section = match["preview_section"]
        if len(groups[section]) >= limit:
            continue
        groups[section].append(match)
    return groups, overlay_status


def load_preview_rows(use_overlay: bool = True) -> tuple[list[dict], dict]:
    with get_connection() as conn:
        live_rows = matcher.get_active_rows(conn, policy=MARKET_COUNT_POLICY_COUNT_LIVE)
        evergreen_rows = matcher.get_active_rows(
            conn,
            policy_not=MARKET_COUNT_POLICY_COUNT_LIVE,
            inventory_models=(INVENTORY_MODEL_EVERGREEN_APPLICATION,),
        )
        public_rows = matcher.get_active_rows(
            conn,
            policy_not=MARKET_COUNT_POLICY_COUNT_LIVE,
            inventory_models=(INVENTORY_MODEL_PUBLIC_INVENTORY, INVENTORY_MODEL_MIXED),
        )
    rows = [
        dict(row) for row in list(live_rows) + list(evergreen_rows) + list(public_rows)
        if row["source_tier"] != SOURCE_TIER_EXPERIMENTAL
    ]
    if not use_overlay:
        return rows, {
            "enabled": False,
            "path": str(DEFAULT_OVERLAY_PATH),
            "records_loaded": 0,
            "rows_enriched": 0,
        }

    overlay = load_overlay()
    enriched_rows = apply_overlay_to_rows(rows, overlay)
    return enriched_rows, {
        "enabled": overlay.enabled,
        "path": str(overlay.path),
        "records_loaded": len(overlay.records_by_key),
        "rows_enriched": sum(1 for row in enriched_rows if row.get("metadata_overlay_applied")),
    }


def apply_preview_guardrails(
    profile: dict,
    row: dict,
    match: dict,
    supported_specializations: set[str] | None = None,
    profile_fit_evidence=None,
) -> dict:
    match = dict(match)
    base_section = preview_section_for_match(match)
    diagnostics = preview_diagnostics_for_match(
        profile,
        row,
        match,
        supported_specializations=supported_specializations,
    )
    capped_section = base_section
    cap_reasons = []
    if not match.get("eligible_for_personalized", True):
        cap_reasons.append("personalized_eligibility_failed")
    if match.get("professional_domain_hard_gate_applied"):
        cap_reasons.append("professional_domain_hard_gate")
    if match.get("location_actionability_cap_applied"):
        cap_reasons.append("location_actionability_cap")
    for diagnostic in diagnostics:
        rule = decisive_preview_cap_rule(diagnostic)
        if not rule:
            continue
        cap, reason = rule
        capped_section = cap_section(capped_section, cap)
        cap_reasons.append(reason)
    credential_label = match.get("preview_credential_requirement") or ""
    if credential_label and credential_requirement_conflicts(profile, credential_label):
        cap_reasons.append("explicit_credential_incompatibility")
    match["preview_section"] = capped_section
    match["preview_diagnostics"] = diagnostics
    match["actionability_cap_reasons"] = unique_list(cap_reasons)
    assessment = assess_affirmative_fit(
        profile,
        row,
        match,
        profile_fit_evidence=profile_fit_evidence,
    )
    match["affirmative_fit"] = assessment.as_dict()
    match["affirmative_fit_status"] = assessment.status
    match["affirmative_fit_supported_evidence"] = [
        {
            "requirement": item.requirement,
            "profile_evidence": item.profile_evidence,
            "source": item.source,
        }
        for item in assessment.supported_evidence
    ]
    match["affirmative_fit_why"] = list(assessment.why_fit_statements)
    admission_reasons = list(match["actionability_cap_reasons"])
    if assessment.status != AFFIRMATIVE_FIT_SUPPORTED:
        admission_reasons.append(f"affirmative_fit_{assessment.status}")
    match["primary_admission_reasons"] = unique_list(admission_reasons)
    match["primary_recommendation_eligible"] = (
        not match["actionability_cap_reasons"]
        and assessment.status == AFFIRMATIVE_FIT_SUPPORTED
    )
    if match["primary_recommendation_eligible"]:
        match["primary_admission_source"] = "affirmative_fit_supported"
    elif match["actionability_cap_reasons"]:
        match["primary_admission_source"] = "guardrail_demoted"
    else:
        match["primary_admission_source"] = f"affirmative_fit_{assessment.status}"
    return match


def decisive_preview_cap_rule(diagnostic: str) -> tuple[str, str] | None:
    for prefix, cap, reason in DECISIVE_PREVIEW_CAP_RULES:
        if diagnostic.startswith(prefix):
            return cap, reason
    return None


def preview_section_for_match(match: dict) -> str:
    if (
        not match.get("eligible_for_personalized", True)
        or match.get("professional_domain_hard_gate_applied")
    ):
        return "excluded"
    if match.get("location_actionability_cap_applied"):
        return "explore_only"
    section = match.get("effective_product_section") or "explore_only"
    return section if section in SECTION_ORDER else "explore_only"


def cap_section(section: str, cap: str) -> str:
    if ACTIONABILITY_RANK.get(section, 1) > ACTIONABILITY_RANK[cap]:
        return cap
    return section


def preview_diagnostics_for_match(
    profile: dict,
    row: dict,
    match: dict,
    supported_specializations: set[str] | None = None,
) -> list[str]:
    diagnostics = []
    if match.get("unsupported_languages"):
        diagnostics.append(
            "Detected unsupported language requirement: "
            + ", ".join(match["unsupported_languages"])
        )
    if match.get("location_actionability_cap_applied"):
        diagnostics.append("Location/actionability needs review before prioritizing.")
    if match.get("professional_domain_hard_gate_applied"):
        diagnostics.append("Professional-domain mismatch.")
    if match.get("metadata_overlay_applied"):
        pieces = []
        if match.get("overlay_required_languages"):
            pieces.append("required languages: " + ", ".join(match["overlay_required_languages"]))
        if match.get("overlay_language_locale"):
            pieces.append("language locale: " + ", ".join(match["overlay_language_locale"][:4]))
        if match.get("overlay_location_restriction"):
            pieces.append("location restriction: " + ", ".join(match["overlay_location_restriction"]))
        review_ids = match.get("overlay_review_ids") or []
        diagnostics.append(
            "Reviewed metadata overlay applied"
            + (": " + "; ".join(pieces) if pieces else "")
            + (f" (reviews: {', '.join(review_ids[:3])})" if review_ids else "")
        )
    if match.get("overlay_location_restriction"):
        diagnostics.append(
            "Reviewed title-derived location restriction exists; inspect before prioritizing."
        )
    title_text = normalize_text(match.get("display_title"))
    row_text = preview_row_text(row)
    profile_languages = matcher.profile_language_set(profile)
    detected_languages = set(match.get("detected_languages") or [])
    for term, canonical_language in sorted(UNCONFIRMED_LANGUAGE_TERMS.items()):
        if (
            re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", title_text)
            and canonical_language not in detected_languages
            and canonical_language not in profile_languages
        ):
            diagnostics.append(
                f"Possible unconfirmed language requirement in title: {term}. "
                "Capped to Explore Only pending opportunity metadata review."
            )
            break
    title_language_labels = title_only_language_requirement_labels(title_text, profile_languages)
    if title_language_labels:
        diagnostics.append(
            "Unsupported title-only language or dialect appears required: "
            + ", ".join(title_language_labels[:3])
            + "."
        )
    locale_requirements = language_locale_requirements_for_row(row, match)
    unmatched_locale_requirements = [
        requirement
        for requirement in locale_requirements
        if locale_requirement_needs_confirmation(requirement, profile, profile_languages)
    ]
    if unmatched_locale_requirements:
        diagnostics.append(
            "Specific language locale/accent may be required but is not confirmed in profile: "
            + ", ".join(format_locale_requirement(requirement) for requirement in unmatched_locale_requirements[:3])
            + "."
        )
    regional_label = regional_location_restriction_label(row, match)
    if regional_label and not profile_location_known(profile):
        diagnostics.append(
            f"Location or regional eligibility needs confirmation for {regional_label}; "
            "capped to Explore Only until profile location is known."
        )
    if specialized_annotation_needs_cap(row_text, profile):
        diagnostics.append(
            "Specialized annotation or survey domain does not match profile specialties; "
            "generic annotation interest is not enough."
        )
    science_label = science_subdomain_mismatch_label(row_text, profile)
    if science_label:
        diagnostics.append(
            f"Science subdomain appears outside profile specialty: {science_label}; "
            "capped until that specialty is confirmed."
        )
    cross_domain_label = cross_domain_expertise_label(row_text, profile)
    if cross_domain_label:
        diagnostics.append(
            f"Cross-domain technical role may require {cross_domain_label} expertise not confirmed in profile."
        )
    if profile_has_no_biology_medical_credentials(profile) and cross_domain_science_coding_row(row_text, profile):
        diagnostics.append(
            "Profile states no biology or medical credentials; cross-domain science/coding role "
            "capped to Explore Only."
        )
    if profile_has_no_medical_license(profile) and licensed_medical_row(row_text):
        diagnostics.append(
            "Medical license or credential may be required; capped to Explore Only until confirmed."
        )
    credential_label = credential_requirement_label(row_text, profile)
    match["preview_credential_requirement"] = credential_label
    if credential_label:
        diagnostics.append(
            f"Credential or education requirement may apply: {credential_label} not confirmed in profile."
        )
    specialization = evaluate_specialization_requirements(
        match.get("display_title") or row.get("title") or "",
        profile,
        supported_concepts=supported_specializations,
    )
    match["specialization_requirements"] = specialization["requirements"]
    match["supported_specialization_groups"] = specialization["supported_groups"]
    match["missing_specialization_groups"] = specialization["missing_groups"]
    match["supported_specialization_concepts"] = specialization["supported_concepts"]
    if specialization["missing_groups"]:
        diagnostics.append(
            "Unsupported explicit specialization requirements: "
            + "; ".join(group["label"] for group in specialization["missing_groups"])
            + "."
        )
    return diagnostics


def preview_row_text(row: dict) -> str:
    values = [
        row.get("title"),
        row.get("canonical_title"),
        row.get("source_category"),
        row.get("department"),
        row.get("expertise"),
        row.get("description"),
        row.get("location"),
    ]
    return normalize_text(" ".join(str(value or "") for value in values))


def canonical_language_locale_keys(canonical: dict) -> list[str]:
    keys = set()
    location = canonical.get("location") or {}
    country = normalize_locale(location.get("country"))
    for entry in canonical.get("languages") or []:
        language = normalize_text(entry.get("language"))
        if not language:
            continue
        locale_values = []
        if entry.get("locale"):
            locale_values.append(entry["locale"])
        locale_values.extend(entry.get("evidence") or [])
        if country:
            locale_values.append(country)
        for locale_value in locale_values:
            for locale in locale_tokens_from_text(locale_value):
                keys.add(locale_key(language, locale))
    return sorted(keys)


def language_locale_requirements_for_row(row: dict, match: dict) -> list[dict]:
    requirements = []
    values = [
        match.get("language_locale"),
        match.get("overlay_language_locale"),
        row.get("language_locale"),
        row.get("title"),
        row.get("canonical_title"),
    ]
    for value in flatten_values(values):
        requirements.extend(language_locale_requirements_from_text(value))
    return unique_locale_requirements(requirements)


def language_locale_requirements_for_match(match: dict) -> list[dict]:
    requirements = []
    values = [
        match.get("language_locale"),
        match.get("overlay_language_locale"),
        match.get("display_title"),
    ]
    for value in flatten_values(values):
        requirements.extend(language_locale_requirements_from_text(value))
    return unique_locale_requirements(requirements)


def title_only_language_requirement_labels(title_text: str, profile_languages: set[str]) -> list[str]:
    labels = []
    for label, aliases in TITLE_ONLY_LANGUAGE_REQUIREMENTS:
        if any(re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", title_text) for alias in aliases):
            labels.append(label)
    generic = re.search(
        r"\b([a-z][a-z\s]{2,40})\s+(language|dialect|audio)\s+(specialist|expert|evaluator|trainer)\b",
        title_text,
    )
    if generic:
        candidate = normalize_text(generic.group(1))
        if candidate not in profile_languages and candidate not in {"english", "spanish", "portuguese", "french"}:
            labels.append(candidate)
    sign_language = re.search(r"\b([a-z][a-z\s]{2,40})\s+sign language\b", title_text)
    if sign_language:
        labels.append(normalize_text(sign_language.group(0)))
    return unique_list(labels)


def language_locale_requirements_from_text(value: str) -> list[dict]:
    text = normalize_text(value)
    if not text:
        return []
    requirements = []
    for language in LANGUAGE_LOCALE_BASES:
        language_pattern = re.escape(language)
        for match in re.finditer(rf"(?<![a-z0-9]){language_pattern}\s*\(([^)]{{1,80}})\)", text):
            requirements.extend(locale_requirements_for_piece(language, match.group(1)))
        for match in re.finditer(rf"(?<![a-z0-9]){language_pattern}\s*[-–—]\s*([a-z][a-z\s]{{1,35}})", text):
            requirements.extend(locale_requirements_for_piece(language, match.group(1)))
        role_terms = (
            r"(?:language|audio)\s+"
            r"(?:data\s+contributor|specialist|expert|evaluator|trainer|rater|reviewer|annotator)"
        )
        for match in re.finditer(rf"(?<![a-z0-9]){language_pattern}\s+{role_terms}\s*\(([^)]{{1,80}})\)", text):
            requirements.extend(locale_requirements_for_piece(language, match.group(1)))
    for match in re.finditer(r"(?<![a-z0-9])espa.{0,3}ol\s*\(([^)]{1,80})\)", text):
        requirements.extend(locale_requirements_for_piece("spanish", match.group(1)))
    return requirements


def locale_requirements_for_piece(language: str, piece: str) -> list[dict]:
    return [
        {"language": base_language(language), "locale": locale}
        for locale in locale_tokens_from_text(piece)
    ]


def locale_tokens_from_text(value: str) -> list[str]:
    text = normalize_text(value)
    tokens = []
    for canonical, aliases in LANGUAGE_LOCALE_TERMS:
        if any(re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text) for alias in aliases):
            tokens.append(canonical)
    return unique_list(tokens)


def locale_requirement_needs_confirmation(requirement: dict, profile: dict, profile_languages: set[str]) -> bool:
    language = requirement["language"]
    locale = requirement["locale"]
    if language not in profile_languages:
        return False
    profile_locale_keys = set(profile.get("language_locale_keys") or [])
    return locale_key(language, locale) not in profile_locale_keys


def locale_key(language: str, locale: str) -> str:
    return f"{base_language(language)}:{normalize_locale(locale)}"


def base_language(language: str) -> str:
    language = normalize_text(language)
    if language == "mandarin chinese":
        return "chinese"
    return language


def normalize_locale(value: str) -> str:
    text = normalize_text(value)
    for canonical, aliases in LANGUAGE_LOCALE_TERMS:
        if text == canonical or text in aliases:
            return canonical
    return text


def format_locale_requirement(requirement: dict) -> str:
    language = requirement["language"].title()
    locale = requirement["locale"].title()
    return f"{language} ({locale})"


def unique_locale_requirements(requirements: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for requirement in requirements:
        key = locale_key(requirement["language"], requirement["locale"])
        if key in seen:
            continue
        seen.add(key)
        result.append(requirement)
    return result


def flatten_values(values: list) -> list[str]:
    result = []
    for value in values:
        if isinstance(value, (list, tuple, set)):
            result.extend(flatten_values(list(value)))
        elif value:
            result.append(str(value))
    return result


def regional_location_restriction_label(row: dict, match: dict) -> str:
    title = normalize_text(match.get("display_title") or row.get("title") or "")
    if not title:
        return ""
    for pattern, label in LOCATION_RESTRICTION_PATTERNS:
        if re.search(pattern, title):
            return label
    return ""


def profile_location_known(profile: dict) -> bool:
    return any(
        normalize_text(profile.get(field))
        for field in ("country", "location", "residence", "city", "region")
    )


def specialized_annotation_needs_cap(row_text: str, profile: dict) -> bool:
    if not contains_preview_term(row_text, SPECIALIZED_ANNOTATION_TERMS):
        return False
    return not profile_has_term(profile, SPECIALIZED_ANNOTATION_PROFILE_TERMS)


def science_subdomain_mismatch_label(row_text: str, profile: dict) -> str:
    if not profile_positive_text_has_term(profile, BIOLOGY_PROFILE_TERMS):
        return ""
    if contains_preview_term(row_text, BIOLOGY_COMPATIBLE_ROLE_TERMS):
        return ""
    for term in UNRELATED_SCIENCE_ROLE_TERMS:
        if contains_preview_term(row_text, (term,)) and not profile_has_term(profile, (term,)):
            return term
    return ""


def cross_domain_expertise_label(row_text: str, profile: dict) -> str:
    if not profile_has_term(profile, ("software engineering", "software", "coding", "python", "javascript")):
        return ""
    if profile_positive_text_has_term(profile, BIOLOGY_PROFILE_TERMS + UNRELATED_SCIENCE_ROLE_TERMS):
        return ""
    for term in BIOLOGY_PROFILE_TERMS + UNRELATED_SCIENCE_ROLE_TERMS:
        if contains_preview_term(row_text, (term,)):
            return term
    return ""


def credential_requirement_label(row_text: str, profile: dict) -> str:
    for pattern, label in CREDENTIAL_REQUIREMENT_PATTERNS:
        if re.search(pattern, row_text) and not profile_confirms_credential(profile, label):
            return label
    return ""


def profile_confirms_credential(profile: dict, label: str) -> bool:
    text = profile_specificity_text(profile)
    education = normalize_text(profile.get("education_level"))
    if "bachelor" in label:
        return education in {"bachelor", "masters", "master", "doctorate"} or "bachelor" in text
    if "master" in label:
        return education in {"masters", "master", "doctorate"} or "master" in text or re.search(r"\bms\b", text)
    if "phd" in label.lower() or "doctorate" in label:
        return education == "doctorate" or "phd" in text or "doctorate" in text
    if "license" in label:
        if "no medical license" in text or "no license" in text or "not licensed" in text:
            return False
        return "licensed" in text or "license" in text or "medical doctor" in text or "physician" in text
    return False


def credential_requirement_conflicts(profile: dict, label: str) -> bool:
    text = profile_specificity_text(profile)
    education = normalize_text(profile.get("education_level"))
    if any(term in label.lower() for term in ("bachelor", "master", "phd", "doctorate")):
        return education == "no_degree" or "no college degree" in text or "no degree" in text
    if "license" in label.lower():
        return (
            "no medical license" in text
            or "no law license" in text
            or "no professional license" in text
            or "not licensed" in text
        )
    return False


def profile_has_term(profile: dict, terms: tuple[str, ...]) -> bool:
    text = profile_specificity_text(profile)
    return contains_preview_term(text, terms)


def profile_positive_text_has_term(profile: dict, terms: tuple[str, ...]) -> bool:
    text = profile_positive_specificity_text(profile)
    return contains_preview_term(text, terms)


def profile_positive_specificity_text(profile: dict) -> str:
    values = [
        profile.get("notes"),
        " ".join(profile.get("degrees_or_domains") or []),
        " ".join(profile.get("skills") or []),
        " ".join(profile.get("target_opportunity_types") or []),
        " ".join(profile.get("work_preferences") or []),
    ]
    return normalize_text(" ".join(str(value or "") for value in values))


def profile_specificity_text(profile: dict) -> str:
    values = [
        profile.get("summary"),
        profile.get("notes"),
        " ".join(profile.get("degrees_or_domains") or []),
        " ".join(profile.get("skills") or []),
        " ".join(profile.get("constraints") or []),
        " ".join(profile.get("target_opportunity_types") or []),
        " ".join(profile.get("work_preferences") or []),
    ]
    return normalize_text(" ".join(str(value or "") for value in values))


def profile_has_no_biology_medical_credentials(profile: dict) -> bool:
    text = normalize_text(
        " ".join(
            str(value or "")
            for value in (
                " ".join(profile.get("constraints") or []),
                " ".join(profile.get("avoid_keywords") or []),
                profile.get("summary", ""),
            )
        )
    )
    return (
        "no biology or medical credentials" in text
        or "biology credentials" in text
        or "medical credentials" in text
    )


def profile_has_no_medical_license(profile: dict) -> bool:
    text = normalize_text(
        " ".join(
            str(value or "")
            for value in (
                " ".join(profile.get("constraints") or []),
                " ".join(profile.get("avoid_keywords") or []),
                profile.get("summary", ""),
            )
        )
    )
    return "no medical license" in text or "licensed physician" in text


def cross_domain_science_coding_row(row_text: str, profile: dict) -> bool:
    profile_domains = {normalize_text(value) for value in profile.get("degrees_or_domains") or []}
    if "biology" in profile_domains or "medicine" in profile_domains or "microbiology" in profile_domains:
        return False
    return contains_preview_term(row_text, SCIENCE_MEDICAL_PREVIEW_TERMS) and contains_preview_term(
        row_text,
        CODING_PREVIEW_TERMS,
    )


def licensed_medical_row(row_text: str) -> bool:
    return contains_preview_term(row_text, LICENSED_MEDICAL_PREVIEW_TERMS)


def contains_preview_term(text: str, terms: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) for term in terms)


def build_preview_warnings(normalizer_warnings: list[str], normalization, grouped_matches: dict) -> list[str]:
    warnings = list(normalizer_warnings)
    missing = set(normalization.missing_fields)
    ambiguous = set(normalization.ambiguous_fields)
    if "location" in missing:
        warnings.append("Location is missing; country-specific opportunities may be demoted or need review.")
    if "languages" in missing or "language proficiency" in ambiguous:
        warnings.append(
            "Language information is incomplete; unconfirmed language requirements in titles may need review."
        )
    else:
        warnings.append(
            "Opportunity language metadata may be incomplete; unconfirmed language requirements in titles should be reviewed."
        )
    if "licenses" in missing:
        warnings.append("No license was extracted; licensed professional roles may require manual review.")

    unsupported_count = sum(
        1
        for match in grouped_matches.get("excluded", [])
        if match.get("unsupported_languages")
    )
    if unsupported_count:
        warnings.append(
            f"{unsupported_count} shown excluded rows have detected unsupported language requirements."
        )

    metadata_gap_count = sum(
        1
        for section in ("do_these_first", "best_matches", "also_worth_reviewing", "explore_only")
        for match in grouped_matches.get(section, [])
        if any(
            "Possible unconfirmed language requirement" in diagnostic
            for diagnostic in match.get("preview_diagnostics", [])
        )
    )
    if metadata_gap_count:
        warnings.append(
            f"{metadata_gap_count} visible rows may have unconfirmed language requirements in titles; "
            "this is an opportunity metadata normalization gap."
        )
    return unique_list(warnings)


def dedupe_matches(matches: list[dict]) -> list[dict]:
    best_by_key = {}
    variant_counts = Counter()
    for match in matches:
        key = match_key(match)
        variant_counts[key] += 1
        existing = best_by_key.get(key)
        if existing is None or match["score"] > existing["score"]:
            best_by_key[key] = match
    deduped = []
    for key, match in best_by_key.items():
        match = dict(match)
        match["variant_count"] = variant_counts[key]
        deduped.append(match)
    return deduped


def ensure_safe_do_these_first(matches: list[dict], profile: dict) -> list[dict]:
    if any(
        match["preview_section"] == "do_these_first"
        and match.get("primary_recommendation_eligible")
        and match.get("affirmative_fit_status") == AFFIRMATIVE_FIT_SUPPORTED
        for match in matches
    ):
        return matches

    profile_languages = matcher.profile_language_set(profile)
    promoted_by_key = {}
    for match in sorted(matches, key=match_sort_key):
        if not safe_generic_language_primary_action(match, profile_languages):
            continue
        promoted = dict(match)
        promoted["preview_section"] = "do_these_first"
        promoted["primary_recommendation_eligible"] = True
        promoted["primary_admission_source"] = "safe_fallback"
        diagnostics = list(promoted.get("preview_diagnostics") or [])
        diagnostics.append(
            "Preview daily plan fallback promoted safe generic language match because "
            "no higher-priority action was available."
        )
        promoted["preview_diagnostics"] = diagnostics
        promoted_by_key[match_key(match)] = promoted
        if len(promoted_by_key) >= PRIMARY_PLAN_FALLBACK_LIMIT:
            break

    if not promoted_by_key:
        return matches
    return [promoted_by_key.get(match_key(match), match) for match in matches]


def safe_generic_language_primary_action(match: dict, profile_languages: set[str]) -> bool:
    if match["preview_section"] not in {"best_matches", "also_worth_reviewing"}:
        return False
    if not match.get("eligible_for_personalized", True):
        return False
    if match.get("unsupported_languages"):
        return False
    if match.get("location_actionability_cap_applied") or match.get("professional_domain_hard_gate_applied"):
        return False
    if not match.get("primary_recommendation_eligible", True):
        return False
    if match.get("affirmative_fit_status") != AFFIRMATIVE_FIT_SUPPORTED:
        return False
    diagnostics = match.get("preview_diagnostics") or []
    if any(
        diagnostic.startswith(PRIMARY_PLAN_BLOCKING_DIAGNOSTIC_PREFIXES)
        for diagnostic in diagnostics
    ):
        return False
    if language_locale_requirements_for_match(match):
        return False
    title_text = normalize_text(match.get("display_title"))
    return bool(supported_profile_languages_in_title(title_text, profile_languages))


def supported_profile_languages_in_title(title_text: str, profile_languages: set[str]) -> list[str]:
    supported = []
    for language in sorted(profile_languages):
        variants = getattr(matcher, "language_variants_for_name", lambda value: [value])(language)
        if any(
            re.search(rf"(?<![a-z0-9]){re.escape(normalize_text(variant))}(?![a-z0-9])", title_text)
            for variant in variants
            if normalize_text(variant)
        ):
            supported.append(language)
    return supported


def match_key(match: dict):
    canonical_id = match.get("canonical_opportunity_id")
    if canonical_id:
        return ("canonical", match["source_slug"], canonical_id)
    if match.get("url"):
        return ("url", match["url"])
    return ("title", match["source_slug"], normalize_text(match["display_title"]))


def match_sort_key(match: dict):
    section_rank = {section: index for index, section in enumerate(SECTION_ORDER)}
    return (
        section_rank.get(match["preview_section"], 99),
        -match["score"],
        match["source"].lower(),
        match["display_title"].lower(),
    )


def render_context(context: dict, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(serializable_context(context), indent=2, sort_keys=True)
    if output_format == "html":
        return render_html(context)
    return render_text(context)


def serializable_context(context: dict) -> dict:
    data = dict(context)
    data["matches"] = {
        section: [match_summary(match) for match in matches]
        for section, matches in context["matches"].items()
    }
    return data


def render_text(context: dict) -> str:
    canonical = context["canonical_profile"]
    lines = [
        "",
        "Profile to Matches Preview",
        "==========================",
        context["disclaimer"],
        "",
        f"Generated: {context['generated_at']}",
        f"Input style: {context['input_style']}",
        f"Normalizer: {context['normalizer']} ({context['extraction_quality']})",
        "",
        "Canonical Profile Preview",
        "-------------------------",
        f"Languages: {join_languages(canonical)}",
        f"Location: {location_label(canonical)}",
        f"Remote preference: {remote_preference_label(canonical)}",
        f"Education: {canonical['education'].get('education_level') or 'unknown'}",
        f"Domains: {', '.join(canonical['education'].get('fields_or_domains') or []) or '-'}",
        f"Specialties: {', '.join(canonical['experience'].get('specialties') or []) or '-'}",
        f"Skills: {', '.join(canonical['skills'].get('normalized') or []) or '-'}",
        f"Preferences: {', '.join(canonical['preferences'].get('work_preferences') or []) or '-'}",
        f"Credentials/licenses: {credentials_label(canonical)}",
        f"Constraints: {', '.join(canonical['constraints'].get('hard_constraints') or []) or '-'}",
        f"Missing fields: {', '.join(context['missing_fields']) or '-'}",
        f"Ambiguous fields: {', '.join(context['ambiguous_fields']) or '-'}",
        f"Warnings: {', '.join(context['warnings']) or '-'}",
        (
            "Metadata overlay: "
            f"{'enabled' if context['metadata_overlay']['enabled'] else 'disabled'}; "
            f"records={context['metadata_overlay']['records_loaded']}; "
            f"rows enriched={context['metadata_overlay']['rows_enriched']}"
        ),
        "",
        "Recommended Opportunities",
        "-------------------------",
    ]
    for section in SECTION_ORDER:
        lines.append("")
        lines.append(f"{SECTION_LABELS[section]} ({len(context['matches'][section])})")
        if not context["matches"][section]:
            lines.append("- None in this preview.")
            continue
        for match in context["matches"][section]:
            lines.extend(format_text_match(match))
    return "\n".join(lines)


def format_text_match(match: dict) -> list[str]:
    reasons = "; ".join(match.get("reasons") or []) or "-"
    flags = []
    if match.get("unsupported_languages"):
        flags.append("unsupported languages: " + ", ".join(match["unsupported_languages"]))
    if match.get("location_actionability_cap_applied"):
        flags.append("location needs review")
    if match.get("professional_domain_hard_gate_applied"):
        flags.append("professional-domain mismatch")
    for diagnostic in match.get("preview_diagnostics") or []:
        if diagnostic.startswith("Possible unconfirmed language requirement"):
            flags.append("possible unconfirmed language requirement")
    flag_text = f" [{'; '.join(flags)}]" if flags else ""
    return [
        f"- {match['display_title']} — {match['source']} ({match['score']} pts){flag_text}",
        f"  Location: {match.get('location') or 'Unknown'} | Area: {match.get('expertise') or 'Unknown'}",
        f"  Reasons: {reasons}",
        f"  Diagnostics: {'; '.join(match.get('preview_diagnostics') or []) or '-'}",
        f"  URL: {match.get('url') or '-'}",
    ]


def html_profile_chips(canonical: dict) -> str:
    chips = [
        ("Languages", join_languages(canonical)),
        ("Location", location_label(canonical)),
        ("Remote", remote_preference_label(canonical)),
        ("Education", canonical["education"].get("education_level") or "unknown"),
    ]
    domains = ", ".join(canonical["education"].get("fields_or_domains") or [])
    if domains:
        chips.append(("Domains", domains))
    skills = ", ".join((canonical["skills"].get("normalized") or [])[:4])
    if skills:
        chips.append(("Skills", skills))
    return "".join(
        f'<span class="chip"><strong>{html_escape(label)}:</strong> {html_escape(value or "-")}</span>'
        for label, value in chips
    )


def html_missing_items(context: dict) -> str:
    prompts = clarification_prompts(context)
    if not prompts:
        prompts = ["Nothing urgent. The profile has enough information for a first matching pass."]
    return "".join(f"<li>{html_escape(prompt)}</li>" for prompt in prompts)


def clarification_prompts(context: dict) -> list[str]:
    prompts = []
    missing = set(context.get("missing_fields") or [])
    ambiguous = set(context.get("ambiguous_fields") or [])
    canonical = context["canonical_profile"]
    credentials = canonical.get("credentials") or {}
    experience = canonical.get("experience") or {}

    if "location" in missing or location_label(canonical) == "-":
        prompts.append("Your country or work location, so country-specific roles can be prioritized safely.")
    if "languages" in missing or "language proficiency" in ambiguous:
        prompts.append("Your working languages and proficiency levels, especially for language-review roles.")
    if not canonical["education"].get("education_level") or canonical["education"].get("education_level") == "unknown":
        prompts.append("Your education level or degree status, if relevant to expert roles.")
    if credentials.get("credential_status") in {"unknown", ""}:
        prompts.append("Whether you hold any professional licenses or certifications.")
    if experience.get("total_years") in {None, ""}:
        prompts.append("Approximate years of experience or seniority.")
    if "messy_input" in ambiguous:
        prompts.append("A little more detail about the work you want to do.")
    return unique_list(prompts)


def html_section_note(section: str) -> str:
    notes = {
        "do_these_first": "Start here. These are the few matches most worth acting on first.",
        "best_matches": "Strong fits worth reviewing next.",
        "also_worth_reviewing": "Good possibilities, but not today's top priority.",
    }
    note = notes.get(section, "")
    return f'<p class="section-note">{html_escape(note)}</p>' if note else ""


def user_fit_reason(match: dict) -> str:
    affirmative_reasons = match.get("affirmative_fit_why") or []
    if affirmative_reasons:
        return " ".join(affirmative_reasons[:2])
    reasons = match.get("reasons") or []
    friendly = []
    for reason in reasons:
        text = friendly_reason(reason)
        if text and text not in friendly:
            friendly.append(text)
        if len(friendly) >= 2:
            break
    if friendly:
        return " ".join(friendly)
    if match.get("matched_languages"):
        languages = ", ".join(language.title() for language in match["matched_languages"])
        return f"It appears to match your listed language(s): {languages}."
    return "It shares some profile signals, but you should review the details before acting."


def friendly_reason(reason: str) -> str:
    text = str(reason or "")
    lowered = text.lower()
    if "generalist ai-work signal" in lowered:
        return ""
    if "language" in lowered:
        return "It lines up with your language background."
    if "remote" in lowered or "flexible" in lowered:
        return "It appears compatible with remote or flexible work."
    if "coding" in lowered or "technical" in lowered or "python" in lowered:
        return "It matches your technical or coding background."
    if "legal" in lowered:
        return "It matches your legal background."
    if "finance" in lowered or "accounting" in lowered:
        return "It matches your finance or accounting background."
    if "biology" in lowered or "medical" in lowered or "science" in lowered or "microbiology" in lowered:
        return "It matches your science or medical background."
    if "evergreen" in lowered:
        return "It is an always-open application surface that may be useful."
    if "public inventory" in lowered or "mixed" in lowered:
        return "It is a useful public lead, even if it is not a live-market posting."
    if "review" in lowered or "rater" in lowered or "evaluation" in lowered:
        return "It matches review or evaluation work signals."
    if "live/countable" in lowered:
        return "It is currently tracked as an active opportunity."
    return text.rstrip(".") + "." if text else ""


def user_caution_note(match: dict) -> str:
    cautions = []
    if match.get("unsupported_languages"):
        cautions.append(
            "This role appears to require "
            + ", ".join(language.title() for language in match["unsupported_languages"])
            + ", which is not listed on the profile."
        )
    if match.get("location_actionability_cap_applied"):
        cautions.append("Location eligibility needs review before prioritizing this.")
    if match.get("professional_domain_hard_gate_applied"):
        cautions.append("The professional domain does not appear to match this profile.")
    for diagnostic in match.get("preview_diagnostics") or []:
        if diagnostic.startswith("Possible unconfirmed language requirement"):
            cautions.append("The title may contain an unconfirmed language requirement.")
        elif diagnostic.startswith("Unsupported title-only language or dialect"):
            title_only_labels = title_only_labels_from_diagnostic(diagnostic)
            if title_only_labels:
                cautions.append(
                    "This role appears to require "
                    + format_title_only_labels(title_only_labels)
                    + ", which is not listed on the profile."
                )
            else:
                cautions.append("This appears to require a language or dialect not listed in your profile.")
        elif diagnostic.startswith("Specific language locale/accent may be required"):
            cautions.append(
                "This may require a specific language locale or accent not confirmed in your profile."
            )
        elif diagnostic.startswith("Location or regional eligibility needs confirmation"):
            cautions.append("Location or regional eligibility needs confirmation before prioritizing this.")
        elif diagnostic.startswith("Specialized annotation or survey domain does not match"):
            cautions.append("This appears to be a specialized annotation task outside your stated specialty.")
        elif diagnostic.startswith("Science subdomain appears outside profile specialty"):
            cautions.append("This science specialty may require domain expertise not confirmed in your profile.")
        elif diagnostic.startswith("Cross-domain technical role may require"):
            cautions.append(
                "This also appears to require domain expertise not confirmed in your profile."
            )
        elif diagnostic.startswith("Medical license or credential may be required"):
            cautions.append("A medical or professional credential may be required.")
        elif diagnostic.startswith("Credential or education requirement may apply"):
            cautions.append("This may require a degree, seniority, or license not confirmed in your profile.")
        elif diagnostic.startswith("Unsupported explicit specialization requirements"):
            labels = specialization_labels_from_diagnostic(diagnostic)
            if labels:
                requirement = " and ".join(labels)
                cautions.append(
                    f"This role appears to require {requirement}, which is not listed in your profile."
                )
            else:
                cautions.append(
                    "This role appears to require a specialization not listed in your profile."
                )
        elif diagnostic.startswith("Reviewed title-derived location restriction"):
            cautions.append("A reviewed title-derived location restriction may apply.")
    return " ".join(unique_list(cautions)[:2])


def title_only_labels_from_diagnostic(diagnostic: str) -> list[str]:
    if ":" not in diagnostic:
        return []
    label_text = diagnostic.split(":", 1)[1].strip().rstrip(".")
    return [label.strip() for label in label_text.split(",") if label.strip()]


def specialization_labels_from_diagnostic(diagnostic: str) -> list[str]:
    if ":" not in diagnostic:
        return []
    label_text = diagnostic.split(":", 1)[1].strip().rstrip(".")
    return [label.strip() for label in label_text.split(";") if label.strip()]


def format_title_only_labels(labels: list[str]) -> str:
    return ", ".join(label.title() for label in labels)


def render_html(context: dict) -> str:
    canonical = context["canonical_profile"]
    sections = "\n".join(render_html_section(section, context["matches"][section]) for section in SECTION_ORDER)
    missing_items = html_missing_items(context)
    profile_chips = html_profile_chips(canonical)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Profile to Matches Preview</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 0; color: #17202a; line-height: 1.45; background: #f7f8fb; }}
    main {{ max-width: 1080px; margin: 0 auto; padding: 32px; }}
    .hero {{ background: #ffffff; border-bottom: 1px solid #d8dee4; padding: 28px 32px; }}
    .hero-inner {{ max-width: 1080px; margin: 0 auto; }}
    .subtle {{ color: #57606a; }}
    .notice {{ border: 1px solid #d8dee4; background: #f6f8fa; padding: 12px; border-radius: 6px; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .box, .match {{ border: 1px solid #d8dee4; border-radius: 6px; padding: 14px; margin: 10px 0; background: #fff; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 12px 0; }}
    .chip {{ border: 1px solid #d8dee4; border-radius: 999px; padding: 6px 10px; background: #fff; font-size: 0.92rem; }}
    .section-note {{ color: #57606a; margin-top: -4px; }}
    .meta {{ color: #57606a; font-size: 0.92rem; }}
    .card-top {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }}
    .badge {{ border: 1px solid #d8dee4; border-radius: 999px; padding: 4px 8px; white-space: nowrap; color: #57606a; font-size: 0.82rem; }}
    .fit {{ margin: 8px 0; }}
    .caution {{ border-left: 3px solid #bf8700; padding-left: 10px; color: #5f4b00; }}
    details {{ margin: 14px 0; }}
    details.diagnostic {{ border: 1px solid #d8dee4; border-radius: 6px; padding: 12px; background: #fff; }}
    summary {{ cursor: pointer; font-weight: 700; }}
    code {{ background: #f6f8fa; padding: 1px 4px; border-radius: 4px; }}
    @media (max-width: 760px) {{ main, .hero {{ padding: 20px; }} .grid {{ grid-template-columns: 1fr; }} .card-top {{ display: block; }} }}
  </style>
</head>
<body>
  <header class="hero">
    <div class="hero-inner">
      <h1>AI Work Match Preview</h1>
      <p class="subtle">A local preview of how Wahojobs understands a profile and turns it into a short opportunity plan.</p>
      <p class="notice">{html_escape(context['disclaimer'])}</p>
      <p class="meta">Generated: {html_escape(context['generated_at'])} | Input style: {html_escape(context['input_style'])}</p>
    </div>
  </header>
  <main>
    <section>
      <h2>Profile Understood</h2>
      <p class="subtle">These are the main signals we extracted from the profile.</p>
      <div class="chips">{profile_chips}</div>
      <div class="grid">
        <div class="box"><strong>Languages</strong><br>{html_escape(join_languages(canonical))}</div>
        <div class="box"><strong>Remote preference</strong><br>{html_escape(remote_preference_label(canonical))}</div>
        <div class="box"><strong>Domains</strong><br>{html_escape(', '.join(canonical['education'].get('fields_or_domains') or []) or '-')}</div>
        <div class="box"><strong>Credentials/licenses</strong><br>{html_escape(credentials_label(canonical))}</div>
      </div>
    </section>
    <section>
      <h2>What We Still Need To Know</h2>
      <p class="subtle">Clarifying these details can improve fit and prevent bad recommendations.</p>
      <ul>{missing_items}</ul>
    </section>
    <section>
      <h2>Recommended Opportunities</h2>
      <p class="subtle">The short list below is capped for readability. Broader browse and excluded results are collapsed by default.</p>
      {sections}
    </section>
    <details class="diagnostic">
      <summary>Technical details</summary>
      <p class="meta">Metadata overlay: {html_escape('enabled' if context['metadata_overlay']['enabled'] else 'disabled')} |
         records={context['metadata_overlay']['records_loaded']} |
         rows enriched={context['metadata_overlay']['rows_enriched']}</p>
      <p class="meta">Normalizer: {html_escape(context['normalizer'])} ({html_escape(context['extraction_quality'])})</p>
      <p class="meta">Warnings: {html_escape('; '.join(context['warnings']) or '-')}</p>
      <p class="meta">Missing fields: {html_escape(', '.join(context['missing_fields']) or '-')}</p>
      <p class="meta">Ambiguous fields: {html_escape(', '.join(context['ambiguous_fields']) or '-')}</p>
    </details>
  </main>
</body>
</html>
"""


def render_html_section(section: str, matches: list[dict]) -> str:
    visible_limit = HTML_SECTION_LIMITS.get(section, 8)
    visible_matches = matches[:visible_limit]
    cards = "\n".join(render_html_match(match, section) for match in visible_matches) if visible_matches else "<p>None in this preview.</p>"
    more = len(matches) - len(visible_matches)
    more_note = f"<p class=\"meta\">Showing {len(visible_matches)} of {len(matches)}. {more} more kept out of the main view.</p>" if more > 0 else ""
    if section in {"explore_only", "excluded"}:
        return (
            f"<details class=\"diagnostic\"><summary>{html_escape(SECTION_LABELS[section])} ({len(matches)}) "
            "- broader browse and diagnostic results</summary>"
            f"<p class=\"section-note\">These are not primary recommendations. Open when you want to inspect edge cases or broader market inventory.</p>"
            f"{cards}{more_note}</details>"
        )
    note = html_section_note(section)
    return f"<section><h3>{html_escape(SECTION_LABELS[section])} ({len(matches)})</h3>{note}{cards}{more_note}</section>"


def render_html_match(match: dict, section: str) -> str:
    fit_reason = user_fit_reason(match)
    caution = user_caution_note(match)
    url = match.get("url") or ""
    link = f'<a href="{html_escape(url)}">Open</a>' if url else "-"
    return f"""
<article class="match">
  <div class="card-top">
    <h4>{html_escape(match['display_title'])}</h4>
    <span class="badge">{html_escape(SECTION_LABELS.get(section, section))}</span>
  </div>
  <p class="meta">{html_escape(match['source'])} | {html_escape(match.get('location') or 'Unknown')} | {html_escape(match.get('expertise') or 'Unknown')}</p>
  <p class="fit"><strong>Why it may fit:</strong> {html_escape(fit_reason)}</p>
  {f'<p class="caution"><strong>Check first:</strong> {html_escape(caution)}</p>' if caution else ''}
  <p>{link}</p>
  <details>
    <summary>Technical details</summary>
    <p class="meta">Score: {match['score']} | Reasons: {html_escape('; '.join(match.get('reasons') or []) or '-')}</p>
    <p class="meta">Diagnostics: {html_escape('; '.join(match.get('preview_diagnostics') or []) or '-')}</p>
  </details>
</article>
"""


def match_summary(match: dict) -> dict:
    keys = (
        "display_title",
        "source",
        "location",
        "expertise",
        "url",
        "score",
        "preview_section",
        "effective_product_section",
        "raw_product_section",
        "eligible_for_personalized",
        "language_requirement_mode",
        "language",
        "language_locale",
        "required_languages",
        "detected_languages",
        "matched_languages",
        "unsupported_languages",
        "location_actionability_cap_applied",
        "professional_domain_hard_gate_applied",
        "reasons",
        "preview_diagnostics",
        "variant_count",
        "metadata_overlay_applied",
        "overlay_required_languages",
        "overlay_language_locale",
        "overlay_location_restriction",
        "overlay_review_ids",
        "primary_recommendation_eligible",
        "primary_admission_source",
        "primary_admission_reasons",
        "actionability_cap_reasons",
        "affirmative_fit_status",
        "affirmative_fit_supported_evidence",
        "affirmative_fit_why",
        "affirmative_fit",
    )
    return {key: match.get(key) for key in keys}


def join_languages(canonical: dict) -> str:
    parts = []
    for item in canonical.get("languages") or []:
        label = item["language"]
        if item.get("proficiency") and item["proficiency"] != "unknown":
            label += f" ({item['proficiency']})"
        if item.get("locale"):
            label += f" [{item['locale']}]"
        parts.append(label)
    return ", ".join(parts) or "-"


def location_label(canonical: dict) -> str:
    location = canonical["location"]
    parts = [location.get(field, "") for field in ("city", "region", "country") if location.get(field)]
    return ", ".join(parts) or "-"


def remote_preference_label(canonical: dict) -> str:
    preferences = canonical["preferences"]
    if preferences.get("remote"):
        return "remote preferred"
    return "not specified"


def credentials_label(canonical: dict) -> str:
    credentials = canonical["credentials"]
    parts = []
    if credentials.get("certifications"):
        parts.append("certifications: " + ", ".join(credentials["certifications"]))
    if credentials.get("licenses"):
        parts.append("licenses: " + ", ".join(credentials["licenses"]))
    if credentials.get("jurisdictions"):
        parts.append("jurisdictions: " + ", ".join(credentials["jurisdictions"]))
    status = credentials.get("credential_status") or "unknown"
    if status == "absent":
        parts.append("license/credential absence stated")
    else:
        parts.append("status: " + status)
    return "; ".join(parts)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def unique_list(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
