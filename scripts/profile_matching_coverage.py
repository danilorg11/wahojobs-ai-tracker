#!/usr/bin/env python3
"""Read-only matching coverage report for reviewed product personas."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import profile_match_digest as matcher  # noqa: E402
import profile_to_matches_preview as preview  # noqa: E402
import local_product_app as product_app  # noqa: E402
from wahojobs.classification import (  # noqa: E402
    INVENTORY_MODEL_EVERGREEN_APPLICATION,
    INVENTORY_MODEL_MIXED,
    INVENTORY_MODEL_PUBLIC_INVENTORY,
    MARKET_COUNT_POLICY_COUNT_LIVE,
    SOURCE_TIER_EXPERIMENTAL,
)
from wahojobs.config import DB_PATH  # noqa: E402
from wahojobs.matching.metadata_overlay import (  # noqa: E402
    apply_overlay_to_rows,
    load_overlay,
)
from wahojobs.profiles.canonical import (  # noqa: E402
    canonical_to_matcher_profile,
    validate_canonical_profile,
)
from wahojobs.profiles.normalizer import BaselineHeuristicProfileNormalizer  # noqa: E402
from wahojobs.profiles.review import apply_reviewed_profile  # noqa: E402


DEFAULT_SUITE = ROOT / "tests" / "fixtures" / "product_readiness_personas_v1.json"
PERSONALIZED_SECTIONS = (
    "do_these_first",
    "best_matches",
    "also_worth_reviewing",
)
REPORT_SECTIONS = (*PERSONALIZED_SECTIONS, "explore_only", "excluded")
LAUNCH_SCOPES = {"core", "adjacent", "outside_initial_launch_scope"}

FAMILY_TERMS = {
    "administrative support": ("administrative", "virtual assistant", "data entry"),
    "biology": ("biology", "biological", "genomics", "microbiology", "life science"),
    "chemistry": ("chemistry", "chemical"),
    "customer support": ("customer support", "customer service", "support agent"),
    "data analysis": ("data analyst", "data analysis", "analytics", "statistics"),
    "data annotation": ("annotation", "annotator", "data labeling", "data labelling"),
    "design": ("design", "figma", "ux", "graphic"),
    "finance": ("finance", "financial", "accounting", "accountant", "investment"),
    "generalist": ("generalist", "ai trainer", "content evaluation", "content review"),
    "healthcare": ("healthcare", "medical", "medicine", "clinical", "physician"),
    "language": ("language", "linguistic", "translation", "localization", "transcription"),
    "legal": ("legal", "law", "lawyer", "attorney", "litigation"),
    "marketing": ("marketing", "social media", "advertising"),
    "operations": ("operations", "project management", "project manager"),
    "quality assurance": ("quality assurance", "qa tester", "software testing", "test engineer"),
    "research": ("research", "fact check", "source evaluation"),
    "software engineering": ("software", "developer", "engineer", "coding", "programming"),
    "teaching": ("teacher", "teaching", "tutor", "education"),
    "writing": ("writer", "writing", "editor", "editing"),
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Evaluate reviewed product personas against current inventory without writing SQLite."
    )
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument(
        "--out",
        type=Path,
        help="Explicit output path. Omit with --stdout-only for a non-writing evaluation.",
    )
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--desired-matches", type=int, default=5)
    parser.add_argument(
        "--evaluated-at",
        help="UTC ISO timestamp for deterministic diagnostics (defaults to now).",
    )
    parser.add_argument(
        "--stdout-only",
        action="store_true",
        help="Print JSON without writing the output file.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    if not args.stdout_only and args.out is None:
        raise SystemExit("Choose --stdout-only or provide an explicit --out path.")
    evaluated_at = parse_evaluated_at(args.evaluated_at)
    suite = load_persona_suite(args.suite)
    rows, inventory = load_rows_read_only(DB_PATH)
    report = evaluate_suite(
        suite,
        rows,
        evaluated_at=evaluated_at,
        limit=args.limit,
        desired_matches=args.desired_matches,
        inventory=inventory,
    )
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.stdout_only:
        print(payload, end="")
        return
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(payload, encoding="utf-8")
    print(
        f"Evaluated {report['persona_count']} personas against "
        f"{report['inventory']['active_rows']} read-only inventory rows."
    )
    print(
        "Coverage diagnoses: "
        + ", ".join(
            f"{key}={value}"
            for key, value in report["summary"]["diagnosis_counts"].items()
        )
    )
    print(f"Wrote {args.out}")


def parse_evaluated_at(value):
    if not value:
        return datetime.now(timezone.utc).replace(microsecond=0)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def load_persona_suite(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    personas = data.get("personas")
    if data.get("suite_version") != "product_readiness_personas_v1":
        raise ValueError("Unsupported product-readiness persona suite version.")
    if not isinstance(personas, list) or len(personas) < 24:
        raise ValueError("The product-readiness suite must contain at least 24 personas.")
    seen = set()
    required = {
        "persona_id",
        "launch_scope",
        "raw_input",
        "review",
        "expected_strong_families",
        "acceptable_fallback_families",
        "excluded_specialist_families",
        "language_requirements",
        "location_constraints",
        "credential_requirements",
        "explanation_expectations",
    }
    for persona in personas:
        missing = required - set(persona)
        if missing:
            raise ValueError(
                f"{persona.get('persona_id') or '<unknown>'} is missing: {', '.join(sorted(missing))}"
            )
        persona_id = str(persona["persona_id"])
        if persona_id in seen:
            raise ValueError(f"Duplicate persona_id: {persona_id}")
        if persona["launch_scope"] not in LAUNCH_SCOPES:
            raise ValueError(
                f"{persona_id} has unsupported launch_scope: {persona['launch_scope']!r}"
            )
        seen.add(persona_id)
    return data


def canonical_profile_for_persona(persona):
    normalizer = BaselineHeuristicProfileNormalizer()
    normalized = normalizer.normalize(
        persona["raw_input"],
        persona.get("input_style") or "long_paragraph",
    )
    canonical = normalized.canonical_profile
    persona_id = str(persona["persona_id"])
    canonical["identity"]["profile_id"] = persona_id
    canonical["identity"]["display_name"] = persona_id.replace("_", " ").title()
    reviewed = apply_reviewed_profile(canonical, persona["review"])
    validate_canonical_profile(reviewed)
    return reviewed


def load_rows_read_only(db_path):
    uri = Path(db_path).resolve().as_uri() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        live = matcher.get_active_rows(conn, policy=MARKET_COUNT_POLICY_COUNT_LIVE)
        evergreen = matcher.get_active_rows(
            conn,
            policy_not=MARKET_COUNT_POLICY_COUNT_LIVE,
            inventory_models=(INVENTORY_MODEL_EVERGREEN_APPLICATION,),
        )
        public = matcher.get_active_rows(
            conn,
            policy_not=MARKET_COUNT_POLICY_COUNT_LIVE,
            inventory_models=(INVENTORY_MODEL_PUBLIC_INVENTORY, INVENTORY_MODEL_MIXED),
        )
    finally:
        conn.close()
    raw_rows = [
        dict(row)
        for row in [*live, *evergreen, *public]
        if row["source_tier"] != SOURCE_TIER_EXPERIMENTAL
    ]
    overlay = load_overlay()
    rows = apply_overlay_to_rows(raw_rows, overlay)
    inventory = {
        "active_rows": len(rows),
        "live_rows": len(live),
        "evergreen_rows": len(evergreen),
        "public_rows": len(public),
        "overlay_enabled": overlay.enabled,
        "overlay_records": len(overlay.records_by_key),
        "rows_enriched": sum(1 for row in rows if row.get("metadata_overlay_applied")),
        "database_mode": "read_only_immutable",
    }
    return rows, inventory


def evaluate_suite(
    suite,
    rows,
    *,
    evaluated_at,
    limit,
    desired_matches,
    inventory=None,
):
    persona_reports = []
    for persona in suite["personas"]:
        canonical = canonical_profile_for_persona(persona)
        matcher_profile = canonical_to_matcher_profile(canonical)
        grouped = preview.build_grouped_matches_from_rows(
            matcher_profile,
            rows,
            max(limit, len(rows)),
            evaluated_at=evaluated_at,
        )
        synthetic_contract = evaluate_synthetic_contract(
            persona,
            canonical,
            evaluated_at=evaluated_at,
        )
        persona_reports.append(
            evaluate_persona(
                persona,
                canonical,
                grouped,
                desired_matches,
                synthetic_contract=synthetic_contract,
                total_inventory_candidates=len(rows),
                display_limit=limit,
            )
        )
    return {
        "report_version": "product_readiness_profile_coverage_v1",
        "generated_at": evaluated_at.isoformat(),
        "suite_version": suite["suite_version"],
        "persona_count": len(persona_reports),
        "desired_personalized_matches": desired_matches,
        "inventory": inventory or {"active_rows": len(rows)},
        "summary": summarize_reports(persona_reports, desired_matches),
        "personas": persona_reports,
    }


def evaluate_persona(
    persona,
    canonical,
    grouped,
    desired_matches,
    *,
    synthetic_contract=None,
    total_inventory_candidates=None,
    display_limit=30,
):
    personalized = product_app.build_browser_presentation_matches(
        {"matches": grouped},
        limit=sum(len(grouped.get(section, [])) for section in PERSONALIZED_SECTIONS),
    )
    all_visible = [
        match for section in REPORT_SECTIONS for match in grouped.get(section, [])
    ]
    family_counts = Counter(
        family
        for match in personalized[:10]
        for family in occupational_families(match)
    )
    specialist_mismatches = mismatch_records(
        personalized,
        persona["excluded_specialist_families"],
    )
    language_leaks = [
        compact_match(match)
        for match in personalized
        if match.get("eligible_for_personalized") is False
        or match.get("unsupported_languages")
    ]
    location_leaks = [
        compact_match(match)
        for match in personalized
        if match.get("location_eligibility_status") == "incompatible"
    ]
    location_coverage_gaps = [
        compact_match(match)
        for match in all_visible
        if match.get("affirmative_fit_status") == "supported"
        and match.get("location_eligibility_status") == "incompatible"
    ][:10]
    credential_leaks = [
        compact_match(match)
        for match in personalized
        if "explicit_credential_incompatibility"
        in (match.get("actionability_cap_reasons") or [])
        or match.get("professional_domain_hard_gate_applied")
    ]
    explanation_findings = explanation_quality_findings(
        persona,
        personalized[:10],
    )
    fallback_usage = fallback_usage_for(persona, personalized[:10])
    uncovered_languages = uncovered_language_requirements(persona, personalized)
    supported_untrusted = sum(
        1
        for match in all_visible
        if match.get("affirmative_fit_status") == "supported"
        and match.get("opportunity_trust_status") != "trusted"
    )
    funnel = coverage_funnel(
        all_visible,
        total_inventory_candidates=(
            len(all_visible)
            if total_inventory_candidates is None
            else total_inventory_candidates
        ),
    )
    diagnosis = coverage_diagnosis(
        persona,
        funnel,
        desired_matches,
        has_matcher_leak=bool(
            specialist_mismatches
            or language_leaks
            or location_leaks
            or credential_leaks
        ),
        synthetic_contract_failure=bool(
            synthetic_contract and not synthetic_contract["contract_passed"]
        ),
    )
    return {
        "persona_id": persona["persona_id"],
        "launch_scope": persona["launch_scope"],
        "expected_strong_families": list(persona["expected_strong_families"]),
        "canonical_profile": {
            "country": canonical["location"].get("country") or "",
            "languages": [
                {
                    "language": row["language"],
                    "proficiency": row["proficiency"],
                    "locale": row.get("locale") or "",
                }
                for row in canonical["languages"]
            ],
            "education_level": canonical["education"]["education_level"],
            "credential_status": canonical["credentials"]["credential_status"],
            "professional_domains": canonical["experience"]["professional_domains"],
            "hard_constraints": canonical["constraints"]["hard_constraints"],
        },
        "result_count": len(personalized),
        "diagnostic_candidate_count": len(all_visible),
        "personalized_result_count": len(personalized),
        "section_counts": {
            section: len(grouped.get(section, [])) for section in REPORT_SECTIONS
        },
        "top_occupational_families": [
            {"family": family, "count": count}
            for family, count in family_counts.most_common(8)
        ],
        "top_results": [compact_match(match) for match in personalized[: min(10, display_limit)]],
        "top_excluded_or_deferred": [
            compact_match(match)
            for section in ("explore_only", "excluded")
            for match in grouped.get(section, [])
        ][:10],
        "specialist_mismatches": specialist_mismatches,
        "unsupported_language_leaks": language_leaks,
        "uncovered_language_requirements": uncovered_languages,
        "location_leaks": location_leaks,
        "location_coverage_gaps": location_coverage_gaps,
        "credential_leaks": credential_leaks,
        "explanation_quality_findings": explanation_findings,
        "fallback_usage": fallback_usage,
        "fallback_only": bool(
            fallback_usage["fallback_family_results"]
            and not fallback_usage["strong_family_results"]
        ),
        "synthetic_strong_family_contract": synthetic_contract or {},
        "live_inventory_strong_family_coverage": {
            "strong_family_results": fallback_usage["strong_family_results"],
            "fallback_family_results": fallback_usage["fallback_family_results"],
        },
        "explanation_contract_violations": (
            list((synthetic_contract or {}).get("explanation_violations") or [])
        ),
        "coverage_funnel": funnel,
        "coverage_causes": diagnosis["flags"],
        "coverage_diagnosis": diagnosis["primary_cause"],
        "supported_but_not_currently_usable": supported_untrusted,
    }


def compact_match(match):
    return {
        "title": match.get("display_title") or "",
        "source": match.get("source") or "",
        "section": match.get("presentation_source_section") or match.get("preview_section") or "",
        "score": int(match.get("ranking_score", match.get("score")) or 0),
        "raw_score": int(match.get("raw_matcher_score", match.get("score")) or 0),
        "occupational_families": occupational_families(match),
        "reasons": list(match.get("affirmative_fit_why") or match.get("reasons") or [])[:4],
        "exclusion_reasons": list(match.get("primary_admission_reasons") or [])[:6],
        "language_eligibility": match.get("language_eligibility_reason") or "",
        "matched_languages": list(match.get("matched_languages") or []),
        "location_eligibility": match.get("location_eligibility_status") or "unknown",
        "trust_status": match.get("opportunity_trust_status") or "unknown",
        "score_components": dict(match.get("score_components") or {}),
    }


def occupational_families(match):
    text = " ".join(
        str(value or "")
        for value in (
            match.get("display_title"),
            match.get("expertise"),
            " ".join(match.get("core_role_domains") or []),
        )
    ).casefold()
    labels = [
        family
        for family, terms in FAMILY_TERMS.items()
        if any(term in text for term in terms)
    ]
    return labels or [str(match.get("expertise") or "broader market").strip().casefold()]


def mismatch_records(matches, excluded_families):
    records = []
    for match in matches:
        haystack = " ".join(
            [
                str(match.get("display_title") or ""),
                str(match.get("expertise") or ""),
                *occupational_families(match),
            ]
        ).casefold()
        matched = [
            expected
            for expected in excluded_families
            if family_expectation_matches(expected, haystack)
        ]
        if matched:
            record = compact_match(match)
            record["matched_exclusions"] = matched
            records.append(record)
    return records


def family_expectation_matches(expected, haystack):
    normalized = str(expected).casefold()
    if normalized in haystack:
        return True
    aliases = {
        "advanced science": ("biology", "chemistry", "physics", "science"),
        "coding": ("software engineering", "developer", "coding"),
        "licensed healthcare": ("healthcare", "medical", "medicine", "physician"),
        "licensed work": ("healthcare", "legal"),
        "non-english language": ("thai language", "unsupported language"),
        "phone support": ("customer support", "phone"),
        "senior roles": ("senior", "lead", "principal"),
        "us-only": ("us only", "united states only"),
        "unsupported language": ("thai language",),
    }
    return any(term in haystack for term in aliases.get(normalized, ()))


def explanation_quality_findings(persona, matches):
    findings = []
    if not matches:
        return [
            {
                "category": "missing_explanation",
                "title": "",
                "detail": "No personalized results were available to explain.",
            }
        ]
    missing_reasons = [
        match.get("display_title") or "Untitled opportunity"
        for match in matches
        if not (match.get("affirmative_fit_why") or match.get("reasons"))
    ]
    if missing_reasons:
        findings.append(
            {
                "category": "missing_explanation",
                "title": ", ".join(missing_reasons[:4]),
                "detail": "No matcher fit evidence was available.",
            }
        )
    for match in matches:
        title = match.get("display_title") or "Untitled opportunity"
        explanation = preview.user_fit_reason(match)
        lowered = explanation.casefold()
        if not explanation.strip() or "shares some profile signals" in lowered:
            findings.append(
                {
                    "category": "generic_explanation",
                    "title": title,
                    "detail": explanation,
                }
            )
        if any(term in lowered for term in ("fresh", "recent source", "currently tracked")):
            findings.append(
                {
                    "category": "freshness_language",
                    "title": title,
                    "detail": explanation,
                }
            )
        excluded = [
            value
            for value in persona["excluded_specialist_families"]
            if family_expectation_matches(value, lowered)
        ]
        if excluded:
            findings.append(
                {
                    "category": "excluded_or_negative_evidence",
                    "title": title,
                    "detail": ", ".join(excluded),
                }
            )
    return findings


def evaluate_synthetic_contract(persona, canonical, *, evaluated_at):
    rows = synthetic_contract_rows(persona, canonical, evaluated_at=evaluated_at)
    grouped = preview.build_grouped_matches_from_rows(
        canonical_to_matcher_profile(canonical),
        rows,
        len(rows),
        evaluated_at=evaluated_at,
    )
    personalized = product_app.build_browser_presentation_matches(
        {"matches": grouped},
        limit=sum(len(grouped.get(section, [])) for section in PERSONALIZED_SECTIONS),
    )
    strong_family = persona["expected_strong_families"][0]
    fallback_family = persona["acceptable_fallback_families"][0]
    base = synthetic_contract_base_id(persona)
    by_job_id = {int(match["job_id"]): match for match in personalized}
    strong = [by_job_id[base + 1]] if base + 1 in by_job_id else []
    fallback = [by_job_id[base + 2]] if base + 2 in by_job_id else []
    prohibited = (
        [compact_match(by_job_id[base + 3])]
        if base + 3 in by_job_id
        else []
    )
    outside = persona["launch_scope"] == "outside_initial_launch_scope"
    explanation_violations = explanation_contract_violations(
        persona,
        canonical,
        strong,
    )
    strong_before_fallback = bool(strong) and (
        not fallback
        or max(int(match.get("ranking_score") or 0) for match in strong)
        >= max(int(match.get("ranking_score") or 0) for match in fallback)
    )
    if outside:
        passed = not prohibited
    else:
        passed = (
            bool(strong)
            and bool(fallback)
            and strong_before_fallback
            and not prohibited
            and not explanation_violations
        )
    return {
        "contract_passed": passed,
        "strong_family": strong_family,
        "strong_admitted": bool(strong),
        "strong_sections": sorted(
            {
                match.get("presentation_source_section") or match.get("preview_section")
                for match in strong
            }
        ),
        "fallback_family": fallback_family,
        "fallback_admitted": bool(fallback),
        "strong_before_fallback": strong_before_fallback,
        "prohibited_admissions": prohibited,
        "explanation_violations": explanation_violations,
        "outside_launch_scope": outside,
    }


def matching_family_records(matches, family):
    result = []
    for match in matches:
        haystack = " ".join(
            [
                str(match.get("display_title") or ""),
                str(match.get("expertise") or ""),
                *occupational_families(match),
            ]
        ).casefold()
        if family_expectation_matches(family, haystack):
            result.append(match)
    return result


def synthetic_contract_rows(persona, canonical, *, evaluated_at):
    profile = canonical_to_matcher_profile(canonical)
    language = (profile.get("languages") or ["English"])[0]
    review = persona["review"]
    strong_evidence = [
        *(review.get("professional_domains") or [])[:1],
        *(review.get("skills") or [])[:2],
        *(review.get("accessibility_constraints") or [])[:1],
    ]
    if review.get("phone_preference") not in (None, "", "unknown"):
        strong_evidence.append(review["phone_preference"])
    if review.get("no_experience"):
        strong_evidence.append("entry level")
    if persona["persona_id"] == "multilingual_language_specialist":
        strong_evidence.append("writing review")
    strong = persona["expected_strong_families"][0]
    fallback = persona["acceptable_fallback_families"][0]
    prohibited = persona["excluded_specialist_families"][0]
    expectations = explanation_categories(
        " ".join(persona["explanation_expectations"])
    )
    if "location" in expectations and review.get("country"):
        strong_evidence.append(review["country"])
    base = synthetic_contract_base_id(persona)
    strong_labels = {
        "healthcare_credentialed": "Medical",
    }
    strong_label = strong_labels.get(persona["persona_id"], strong.title())
    strong_title = " ".join(
        [
            strong_label,
            "AI Evaluation Language Data Contributor",
            language,
            *strong_evidence,
        ]
    )
    fallback_label = (
        "General Data Annotation"
        if persona["persona_id"] == "healthcare_interested_uncredentialed"
        else fallback.title()
    )
    fallback_title = " ".join(
        [fallback_label, "Language Data Contributor", language]
    )
    return [
        synthetic_contract_row(
            base + 1,
            strong_title,
            strong,
            language,
            evaluated_at,
        ),
        synthetic_contract_row(
            base + 2,
            fallback_title,
            (
                "data annotation"
                if persona["persona_id"] == "healthcare_interested_uncredentialed"
                else fallback
            ),
            language,
            evaluated_at,
            evergreen=True,
        ),
        synthetic_contract_row(
            base + 3,
            prohibited_specialist_title(prohibited),
            prohibited,
            None,
            evaluated_at,
        ),
    ]


def synthetic_contract_base_id(persona):
    return sum(ord(character) for character in persona["persona_id"]) * 10


def synthetic_contract_row(
    job_id,
    title,
    expertise,
    language,
    evaluated_at,
    *,
    evergreen=False,
):
    observed = evaluated_at.isoformat()
    return {
        "job_id": job_id,
        "title": title,
        "canonical_title": title,
        "location": "Remote",
        "applicant_location_requirements": "Worldwide",
        "url": f"https://example.test/persona-contract/{job_id}",
        "department": expertise,
        "expertise": expertise,
        "commitment": "Freelance",
        "source_category": expertise,
        "source": "Synthetic Persona Contract",
        "source_slug": "synthetic-persona-contract",
        "source_tier": "core",
        "inventory_model": "evergreen_application" if evergreen else "live_feed",
        "market_count_policy": "report_separately" if evergreen else "count_live",
        "opportunity_kind": "application_portal" if evergreen else "live_posting",
        "availability_basis": "always_open" if evergreen else "api_feed",
        "include_in_live_market_estimate": 0 if evergreen else 1,
        "canonical_opportunity_id": job_id,
        "job_is_active": True,
        "canonical_is_active": True,
        "job_last_seen_at": observed,
        "latest_successful_source_run_at": observed,
        "source_run_started_at": observed,
        "source_run_id": job_id,
        "source_run_qualifies": True,
        "language": language,
        "language_locale": None,
        "required_languages": language,
        "description": "",
    }


def prohibited_specialist_title(family):
    titles = {
        "advanced science": "Advanced Chemistry Scientist",
        "coding": "Software Coding Specialist",
        "licensed clinical medicine": "Licensed Clinical Medicine Specialist",
        "licensed healthcare": "Licensed Healthcare Physician",
        "licensed work": "Licensed Healthcare Specialist",
        "non-English language": "Thai Language Specialist",
        "phone support": "Phone Customer Support Specialist",
        "senior roles": "Senior Principal Specialist",
        "unsupported language": "Thai Language Specialist",
        "us-only": "United States Only Specialist",
    }
    return titles.get(family, f"{family.title()} Specialist")


def explanation_contract_violations(persona, canonical, matches):
    violations = []
    allowed_categories = {
        category
        for expectation in persona["explanation_expectations"]
        for category in explanation_categories(str(expectation))
    }
    profile_categories = profile_evidence_categories(canonical)
    for match in matches:
        title = match.get("display_title") or "Untitled opportunity"
        explanation = preview.user_fit_reason(match)
        lowered = explanation.casefold()
        categories = explanation_categories(explanation)
        if not explanation.strip():
            violations.append({"category": "missing_explanation", "title": title})
            continue
        if "shares some profile signals" in lowered:
            violations.append({"category": "generic_explanation", "title": title})
        if any(term in lowered for term in ("fresh", "recent source", "currently tracked")):
            violations.append({"category": "freshness_language", "title": title})
        unsupported = sorted(categories - profile_categories)
        if unsupported:
            violations.append(
                {
                    "category": "unsupported_evidence",
                    "title": title,
                    "evidence": unsupported,
                }
            )
        if allowed_categories and not (categories & allowed_categories):
            violations.append(
                {
                    "category": "missing_allowed_evidence",
                    "title": title,
                    "allowed": sorted(allowed_categories),
                }
            )
    return violations


def explanation_categories(text):
    normalized = str(text or "").casefold()
    aliases = {
        "language": ("language", "fluent", "native", "bilingual", "multilingual"),
        "domain": ("domain",),
        "degree": ("degree", "education", "phd", "doctorate"),
        "experience": (
            "student",
            "entry-level",
            "entry level",
            "build experience",
            "no experience",
            "no prior experience",
        ),
        "credential": ("credential", "license", "licensed", "clearance"),
        "software skill": ("software", "coding", "python", "technical", "qa", "testing"),
        "research": ("research", "source evaluation"),
        "support": ("support", "customer service"),
        "administration": (
            "administrative",
            "administration",
            "coordination",
            "operations",
            "management",
        ),
        "writing": ("writing", "writer", "editing", "editor"),
        "location": (
            "location",
            "country",
            "remote",
            "eligibility",
            "applicants in",
        ),
        "work preference": ("flexible", "asynchronous", "non-phone", "text-only"),
        "generalist": ("generalist", "annotation", "evaluation", "content review"),
        "science": ("science", "biology", "chemistry", "medical", "clinical"),
        "legal": ("legal", "lawyer", "law degree", "paralegal"),
        "finance": ("finance", "accounting"),
        "design": ("design", "visual", "figma", "ux"),
        "marketing": ("marketing", "social media", "content"),
    }
    return {
        category
        for category, terms in aliases.items()
        if any(term in normalized for term in terms)
    }


def profile_evidence_categories(canonical):
    material = " ".join(
        [
            *(item.get("language") or "" for item in canonical["languages"]),
            *(canonical["education"].get("degrees") or []),
            *(canonical["education"].get("fields_or_domains") or []),
            *(canonical["credentials"].get("licenses") or []),
            *(canonical["credentials"].get("certifications") or []),
            *(canonical["credentials"].get("security_clearances") or []),
            *(canonical["experience"].get("professional_domains") or []),
            *(canonical["experience"].get("occupational_families") or []),
            *(canonical["experience"].get("specialties") or []),
            *(canonical["skills"].get("normalized") or []),
            *(canonical["preferences"].get("target_opportunity_types") or []),
            *(canonical["preferences"].get("work_preferences") or []),
            str(canonical["preferences"].get("phone_preference") or ""),
            *(canonical["preferences"].get("schedule") or []),
            *(canonical["constraints"].get("hard_constraints") or []),
            *(canonical["constraints"].get("negative_constraints") or []),
            *(canonical["constraints"].get("accessibility_constraints") or []),
        ]
    )
    categories = explanation_categories(material)
    if canonical["languages"]:
        categories.add("language")
    if canonical["location"].get("country") or canonical["preferences"].get("remote"):
        categories.add("location")
    if canonical["credentials"].get("credential_status") == "explicit":
        categories.add("credential")
    return categories


def fallback_usage_for(persona, matches):
    strong = [str(value).casefold() for value in persona["expected_strong_families"]]
    fallback = [str(value).casefold() for value in persona["acceptable_fallback_families"]]
    strong_count = 0
    fallback_count = 0
    for match in matches:
        haystack = " ".join(occupational_families(match)).casefold()
        if any(family_expectation_matches(value, haystack) for value in strong):
            strong_count += 1
        elif any(family_expectation_matches(value, haystack) for value in fallback):
            fallback_count += 1
    return {
        "strong_family_results": strong_count,
        "fallback_family_results": fallback_count,
        "mostly_fallback": fallback_count > strong_count,
    }


def uncovered_language_requirements(persona, matches):
    covered = set()
    for match in matches:
        covered.update(str(value).casefold() for value in match.get("matched_languages") or [])
        title = str(match.get("display_title") or "").casefold()
        for required in persona["language_requirements"]:
            if str(required).casefold() in title:
                covered.add(str(required).casefold())
    return [
        required
        for required in persona["language_requirements"]
        if str(required).casefold() not in covered
    ]


def coverage_funnel(matches, *, total_inventory_candidates):
    """Count candidates sequentially so later stages cannot hide earlier losses."""
    stages = {}
    domain = [
        match
        for match in matches
        if not match.get("professional_domain_hard_gate_applied")
    ]
    language = [
        match
        for match in domain
        if match.get("eligible_for_personalized", True)
        and not match.get("unsupported_languages")
    ]
    location = [
        match
        for match in language
        if match.get("location_eligibility_status") != "incompatible"
    ]
    credential = [
        match
        for match in location
        if "explicit_credential_incompatibility"
        not in (match.get("actionability_cap_reasons") or [])
    ]
    fresh = [
        match
        for match in credential
        if match.get("opportunity_trust_status") == "trusted"
    ]
    admitted = [
        match
        for match in fresh
        if match.get("primary_recommendation_eligible")
    ]
    stages.update(
        {
            "total_inventory_candidates": int(total_inventory_candidates),
            "domain_relevant_candidates": len(domain),
            "language_compatible_candidates": len(language),
            "location_compatible_candidates": len(location),
            "credential_compatible_candidates": len(credential),
            "freshness_eligible_candidates": len(fresh),
            "admitted_matches": len(admitted),
        }
    )
    return stages


def coverage_diagnosis(
    persona,
    funnel,
    desired_matches,
    *,
    has_matcher_leak=False,
    synthetic_contract_failure=False,
):
    review = persona["review"]
    explicit_constraints = bool(
        review.get("geographic_restrictions")
        or review.get("accessibility_constraints")
        or review.get("excluded_domains")
        or review.get("no_specialized_credentials")
        or review.get("no_degree")
        or review.get("no_experience")
    )
    flags = {
        "inventory_shortage": funnel["domain_relevant_candidates"] < desired_matches,
        "language_shortage": (
            funnel["language_compatible_candidates"] < desired_matches
            and funnel["language_compatible_candidates"]
            < funnel["domain_relevant_candidates"]
        ),
        "location_shortage": (
            funnel["location_compatible_candidates"] < desired_matches
            and funnel["location_compatible_candidates"]
            < funnel["language_compatible_candidates"]
        ),
        "credential_constraint": (
            funnel["credential_compatible_candidates"] < desired_matches
            and funnel["credential_compatible_candidates"]
            < funnel["location_compatible_candidates"]
        ),
        "source_freshness_shortage": (
            funnel["freshness_eligible_candidates"] < desired_matches
            and funnel["freshness_eligible_candidates"]
            < funnel["credential_compatible_candidates"]
        ),
        "matcher_or_ranking_gap": bool(
            has_matcher_leak
            or synthetic_contract_failure
            or (
                funnel["admitted_matches"] < desired_matches
                and funnel["freshness_eligible_candidates"] >= desired_matches
            )
        ),
        "genuine_profile_constraint": bool(
            explicit_constraints and funnel["admitted_matches"] < desired_matches
        ),
        "outside_launch_scope": (
            persona.get("launch_scope") == "outside_initial_launch_scope"
            or bool(persona.get("outside_launch_scope", False))
        ),
    }
    if flags["outside_launch_scope"]:
        primary = "outside_launch_scope"
    elif funnel["admitted_matches"] >= desired_matches and not has_matcher_leak:
        primary = "adequate"
    else:
        priority = (
            "inventory_shortage",
            "language_shortage",
            "location_shortage",
            "credential_constraint",
            "source_freshness_shortage",
            "matcher_or_ranking_gap",
            "genuine_profile_constraint",
        )
        primary = next((cause for cause in priority if flags[cause]), "matcher_or_ranking_gap")
    return {"primary_cause": primary, "flags": flags}


def summarize_reports(reports, desired_matches):
    diagnosis_counts = Counter(report["coverage_diagnosis"] for report in reports)
    weak = [
        report["persona_id"]
        for report in reports
        if report["personalized_result_count"] < desired_matches
    ]
    generalist_fallback = [
        report["persona_id"]
        for report in reports
        if report["fallback_usage"]["mostly_fallback"]
        or (
            report["top_occupational_families"]
            and report["top_occupational_families"][0]["family"] == "generalist"
        )
    ]
    issue_counts = {
        "specialist_mismatches": sum(len(report["specialist_mismatches"]) for report in reports),
        "unsupported_language_leaks": sum(len(report["unsupported_language_leaks"]) for report in reports),
        "location_leaks": sum(len(report["location_leaks"]) for report in reports),
        "credential_leaks": sum(len(report["credential_leaks"]) for report in reports),
        "explanation_quality_findings": sum(
            len(report["explanation_quality_findings"]) for report in reports
        ),
    }
    result_pairs = Counter(
        (match["source"], match["title"])
        for report in reports
        for match in report["top_results"][:5]
    )
    repetitive = [
        {"source": source, "title": title, "persona_count": count}
        for (source, title), count in result_pairs.most_common()
        if count >= 4
    ]
    top_family_coverage = Counter(
        item["family"]
        for report in reports
        for item in report["top_occupational_families"][:3]
    )
    return {
        "personas_below_desired_matches": weak,
        "insufficient_expected_family_coverage": {
            report["persona_id"]: report["expected_strong_families"]
            for report in reports
            if report["personalized_result_count"] < desired_matches
        },
        "diagnosis_counts": dict(sorted(diagnosis_counts.items())),
        "issue_counts": issue_counts,
        "personas_with_language_gaps": [
            report["persona_id"]
            for report in reports
            if report["uncovered_language_requirements"]
        ],
        "personas_with_country_or_location_gaps": [
            report["persona_id"]
            for report in reports
            if report["location_coverage_gaps"]
        ],
        "personas_with_qualification_false_positives": [
            report["persona_id"]
            for report in reports
            if report["credential_leaks"] or report["specialist_mismatches"]
        ],
        "personas_limited_by_source_freshness": [
            report["persona_id"]
            for report in reports
            if report["coverage_diagnosis"] == "source_freshness_shortage"
        ],
        "profiles_falling_back_to_generalist_or_adjacent_work": generalist_fallback,
        "top_family_coverage": dict(top_family_coverage.most_common()),
        "highly_repetitive_results": repetitive,
    }


if __name__ == "__main__":
    main()
