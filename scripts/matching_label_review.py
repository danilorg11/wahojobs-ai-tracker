#!/usr/bin/env python3
"""Generate and apply human review packets for matching golden-set labels."""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import matching_quality_report as benchmark  # noqa: E402
from wahojobs.db.connection import get_connection  # noqa: E402


FIXTURE_PATH = ROOT / "tests" / "fixtures" / "matching_golden_set.json"
HTML_PATH = ROOT / "exports" / "matching_label_review.html"
CSV_PATH = ROOT / "exports" / "matching_label_review.csv"
SUMMARY_PATH = ROOT / "exports" / "matching_label_review_summary.md"

DEFAULT_LIMIT = 30
PRIORITY_PROFILES = {
    "phd_history_researcher",
    "beginner_bilingual_no_degree",
    "english_teacher_remote",
    "multilingual_translator",
    "generalist_no_degree",
}
IMPORTANT_TITLE_TERMS = (
    "dataannotation generalist",
    "dataannotation bilingual",
    "bilingual ai trainer",
    "generalist ai trainer",
    "ip expert",
    "microbiology specialist",
    "academic dermatologist",
    "social media annotation",
    "pavement condition index",
    "pci survey",
)
ALLOWED_LABELS = {"strong", "plausible", "weak", "false_positive"}
ALLOWED_SECTIONS = {"do_these_first", "best_matches", "also_worth_reviewing", "explore_only", "exclude"}
HUMAN_REVIEW_COLUMNS = [
    "human_label",
    "human_expected_section",
    "human_review_required",
    "human_notes",
    "approved_by",
    "approved_at",
]
CONTEXT_COLUMNS = [
    "job_id",
    "job_external_id",
    "job_source_hash",
    "job_canonical_opportunity_id",
    "job_url",
    "job_location",
    "applicant_location_requirements",
    "job_location_scope",
    "job_remote_status",
    "job_description_or_requirements_excerpt",
    "job_context_source",
    "job_context_resolution_status",
    "ambiguous_candidate_count",
    "ambiguous_candidate_locations",
    "ambiguous_candidate_urls",
    "ambiguous_candidate_ids",
    "profile_display_name",
    "profile_education_level",
    "profile_degrees_or_domains",
    "profile_languages",
    "profile_skills",
    "profile_work_preferences",
    "profile_constraints",
    "profile_target_opportunity_types",
    "profile_notes",
    "profile_location",
    "profile_location_status",
    "location_eligibility",
    "location_eligibility_reason",
]
REFERENCE_COLUMNS = [
    "review_priority",
    "case_id",
    "profile_id",
    "source",
    "title",
    "current_expected_label",
    "current_expected_section",
    "current_review_required",
    "label_source",
    "regression_rule",
    "current_score",
    "current_match_label",
    "current_section",
    "gates_triggered",
    "current_reasons",
    "fixture_rationale",
    "human_review_question",
]
CSV_COLUMNS = REFERENCE_COLUMNS + CONTEXT_COLUMNS + HUMAN_REVIEW_COLUMNS
APPLY_REQUIRED_COLUMNS = [
    "case_id",
    *HUMAN_REVIEW_COLUMNS,
]
LOCATION_COUNTRY_TERMS = {
    "argentina",
    "australia",
    "austria",
    "belgium",
    "brazil",
    "canada",
    "china",
    "france",
    "germany",
    "india",
    "ireland",
    "italy",
    "japan",
    "mexico",
    "netherlands",
    "new zealand",
    "portugal",
    "spain",
    "united kingdom",
    "uk",
    "united states",
    "usa",
    "us",
}


@dataclass
class ReviewCase:
    item: benchmark.EvaluatedCase
    profile: dict
    rank: int
    priority: int
    reasons: list[str]
    question: str


def main() -> None:
    args = parse_args()
    if args.command == "generate":
        generate(args)
    elif args.command == "enrich":
        enrich(args)
    elif args.command == "apply":
        apply_reviews(args)
    else:
        raise SystemExit("Choose a command: generate, enrich, or apply")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate/apply human review packets for matching golden-set labels."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate", help="Generate review HTML/CSV/summary.")
    generate_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    generate_parser.add_argument("--profile")
    generate_parser.add_argument("--include-all", action="store_true")

    enrich_parser = subparsers.add_parser(
        "enrich",
        help="Enrich an existing review CSV without changing the selected batch.",
    )
    enrich_parser.add_argument("--input", type=Path, default=CSV_PATH)

    apply_parser = subparsers.add_parser("apply", help="Apply completed review CSV decisions.")
    apply_parser.add_argument("--input", type=Path, default=CSV_PATH)
    apply_parser.add_argument("--dry-run", action="store_true", default=True)
    apply_parser.add_argument("--yes", action="store_true")

    return parser.parse_args()


def generate(args) -> None:
    fixture, profiles, evaluated = load_evaluated_cases()
    selected = select_review_cases(evaluated, profiles, args.limit, args.profile, args.include_all)

    HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(HTML_PATH, render_html(selected))
    write_csv(selected, CSV_PATH)
    atomic_write_text(SUMMARY_PATH, render_summary(selected, args))

    print("Matching Label Review Packet")
    print("============================")
    print(f"Cases selected: {len(selected)}")
    print(f"Profiles: {format_counter(Counter(case.item.case['profile_id'] for case in selected))}")
    print(f"Review reasons: {format_counter(Counter(reason for case in selected for reason in case.reasons))}")
    print(f"Wrote HTML: {HTML_PATH}")
    print(f"Wrote CSV: {CSV_PATH}")
    print(f"Wrote summary: {SUMMARY_PATH}")


def enrich(args) -> None:
    rows, original_fieldnames = read_existing_batch(args.input)
    fixture = benchmark.load_fixture()
    profiles = load_all_review_profiles(fixture)
    case_index = {case["case_id"]: case for case in fixture["cases"]}
    db_rows = load_context_db_rows()

    before_rows = [dict(row) for row in rows]
    enriched_rows = []
    stats = Counter()
    profile_unknown_locations = set()
    restricted_unknown_profile = []

    for row in rows:
        case_id = row["case_id"].strip()
        if case_id not in case_index:
            raise SystemExit(f"Review CSV case_id not found in fixture: {case_id}")
        case = case_index[case_id]
        profile = profiles.get(row["profile_id"] or case["profile_id"], {})
        profile_context = build_profile_context(profile)
        if profile_context["profile_location_status"] == "unknown":
            profile_unknown_locations.add(row["profile_id"] or case["profile_id"])
        job_context = build_job_context(case, db_rows)
        location_context = build_location_eligibility(profile_context, job_context)
        if (
            job_context["job_location_scope"] in {"remote_restricted", "onsite_or_hybrid_restricted"}
            and profile_context["profile_location_status"] == "unknown"
        ):
            restricted_unknown_profile.append(case_id)
        stats[job_context["job_context_resolution_status"]] += 1
        if job_context["job_url"]:
            stats["usable_job_url"] += 1
        if job_context["applicant_location_requirements"]:
            stats["explicit_location_restriction"] += 1

        enriched = dict(row)
        for column in CONTEXT_COLUMNS:
            enriched[column] = ""
        enriched.update(profile_context)
        enriched.update(job_context)
        enriched.update(location_context)
        enriched_rows.append(enriched)

    output_fieldnames = enriched_fieldnames(original_fieldnames)
    preserved = validate_enrichment_preserved(before_rows, enriched_rows, original_fieldnames)
    write_csv_rows(enriched_rows, args.input, output_fieldnames)
    atomic_write_text(HTML_PATH, render_enriched_html(enriched_rows))
    atomic_write_text(
        SUMMARY_PATH,
        render_enriched_summary(
            args.input,
            before_rows,
            enriched_rows,
            stats,
            profile_unknown_locations,
            restricted_unknown_profile,
            preserved,
        ),
    )

    print("Matching Label Review Enrichment")
    print("================================")
    print(f"Input batch: {args.input}")
    print(f"Rows before: {len(before_rows)}")
    print(f"Rows after: {len(enriched_rows)}")
    print("Ordered case_id list identical: yes")
    print("Preserved pre-existing reference/matcher/human-review values: yes")
    print(f"Cases with usable job URL: {stats['usable_job_url']}")
    print(
        "URL/context resolution: "
        + format_counter(
            Counter(
                {
                    "resolved": stats["resolved"],
                    "partial": stats["partial"],
                    "ambiguous": stats["ambiguous"],
                    "unavailable": stats["unavailable"],
                }
            )
        )
    )
    print(f"Explicit location restrictions: {stats['explicit_location_restriction']}")
    print(f"Restricted cases with unknown profile location: {len(restricted_unknown_profile)}")
    print(f"Wrote HTML: {HTML_PATH}")
    print(f"Wrote CSV: {args.input}")
    print(f"Wrote summary: {SUMMARY_PATH}")


def load_evaluated_cases():
    fixture = benchmark.load_fixture()
    profiles = benchmark.load_benchmark_profiles(fixture)
    rows = benchmark.load_benchmark_db_rows()
    evaluated = [
        benchmark.evaluate_case(case, profiles[case["profile_id"]], rows, benchmark.matcher)
        for case in fixture["cases"]
    ]
    return fixture, profiles, evaluated


def load_all_review_profiles(fixture):
    return benchmark.load_benchmark_profiles(fixture)


def read_existing_batch(path):
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
            fieldnames = list(reader.fieldnames or [])
    except FileNotFoundError:
        raise SystemExit(f"Review CSV not found: {path}")
    if not rows:
        raise SystemExit(f"Review CSV is empty: {path}")
    missing = [column for column in REFERENCE_COLUMNS + HUMAN_REVIEW_COLUMNS if column not in fieldnames]
    if missing:
        raise SystemExit(f"Review CSV is missing required batch columns: {', '.join(missing)}")
    seen = set()
    for row in rows:
        case_id = row.get("case_id", "").strip()
        if not case_id:
            raise SystemExit("Review CSV contains a row with blank case_id.")
        if case_id in seen:
            raise SystemExit(f"Review CSV contains duplicate case_id: {case_id}")
        seen.add(case_id)
    return rows, fieldnames


def load_context_db_rows():
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT
              j.id AS job_id,
              j.external_id,
              j.source_hash,
              j.title,
              j.location,
              j.url,
              j.department,
              j.expertise,
              j.commitment,
              j.opportunity_kind,
              j.availability_basis,
              j.include_in_live_market_estimate,
              j.canonical_opportunity_id,
              c.name AS source,
              c.slug AS source_slug,
              c.source_tier,
              c.inventory_model,
              c.market_count_policy,
              co.canonical_title,
              co.source_category
            FROM jobs j
            JOIN companies c ON c.id = j.company_id
            LEFT JOIN canonical_opportunities co ON co.id = j.canonical_opportunity_id
            WHERE j.is_active = 1
              AND j.title NOT LIKE '[SIMULATION]%'
            ORDER BY c.name ASC, j.title ASC, j.id ASC
            """
        ).fetchall()
    return [benchmark.row_to_dict(row) for row in rows]


def build_profile_context(profile):
    location = explicit_profile_location(profile)
    return {
        "profile_display_name": profile.get("display_name") or profile.get("profile_id") or "",
        "profile_education_level": profile.get("education_level") or "",
        "profile_degrees_or_domains": compact_list(profile.get("degrees_or_domains")),
        "profile_languages": compact_list(profile.get("languages")),
        "profile_skills": compact_list(profile.get("skills")),
        "profile_work_preferences": compact_list(profile.get("work_preferences")),
        "profile_constraints": compact_list(profile.get("constraints")),
        "profile_target_opportunity_types": compact_list(profile.get("target_opportunity_types")),
        "profile_notes": compact_text(profile.get("notes") or profile.get("summary") or "", 260),
        "profile_location": location,
        "profile_location_status": "known" if location else "unknown",
    }


def explicit_profile_location(profile):
    for key in ("location", "country", "residence", "city", "region"):
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def build_job_context(case, db_rows):
    row, status, candidates = resolve_context_row(case, db_rows)
    source = "unavailable"
    if status in {"resolved", "partial", "ambiguous"} and row:
        source = "current_db"
    elif row:
        source = "fixture_snapshot"

    location = clean_cell(row.get("location") if row else case.get("location"))
    location_scope, remote_status, requirements = classify_job_location(location)
    excerpt = requirements_excerpt(row or case)
    context = {
        "job_id": "" if status == "ambiguous" else clean_cell(row.get("job_id") if row else case.get("job_id")),
        "job_external_id": "" if status == "ambiguous" else clean_cell(row.get("external_id") if row else case.get("external_id")),
        "job_source_hash": "" if status == "ambiguous" else clean_cell(row.get("source_hash") if row else case.get("source_hash")),
        "job_canonical_opportunity_id": "" if status == "ambiguous" else clean_cell(row.get("canonical_opportunity_id") if row else case.get("canonical_opportunity_id")),
        "job_url": "" if status == "ambiguous" else clean_cell(row.get("url") if row else case.get("url")),
        "job_location": location,
        "applicant_location_requirements": requirements,
        "job_location_scope": location_scope,
        "job_remote_status": remote_status,
        "job_description_or_requirements_excerpt": excerpt,
        "job_context_source": source,
        "job_context_resolution_status": status,
        "ambiguous_candidate_count": str(len(candidates)) if candidates else "",
        "ambiguous_candidate_locations": compact_candidate_values(candidates, "location"),
        "ambiguous_candidate_urls": compact_candidate_values(candidates, "url"),
        "ambiguous_candidate_ids": compact_candidate_ids(candidates),
        "_ambiguous_candidates": candidates,
    }
    return context


def resolve_context_row(case, db_rows):
    source_slug = benchmark.normalize_slug(case.get("source_slug") or case["source"])
    candidates = [
        row
        for row in db_rows
        if benchmark.normalize_slug(row.get("source_slug") or row.get("source")) == source_slug
    ]
    if not candidates:
        return benchmark.build_snapshot_row(case), "partial", []

    external_id = clean_cell(case.get("external_id"))
    if external_id:
        matches = [row for row in candidates if clean_cell(row.get("external_id")) == external_id]
        if len(matches) == 1:
            return matches[0], "resolved", []
        if len(matches) > 1:
            return benchmark.build_snapshot_row(case), "ambiguous", matches

    url = benchmark.normalize_url(case.get("url"))
    if url:
        matches = [row for row in candidates if benchmark.normalize_url(row.get("url")) == url]
        if len(matches) == 1:
            return matches[0], "resolved", []
        if len(matches) > 1:
            return benchmark.build_snapshot_row(case), "ambiguous", matches

    canonical_id = clean_cell(case.get("canonical_opportunity_id"))
    title_key = benchmark.normalize_title_key(case.get("title"))
    if canonical_id:
        matches = [
            row
            for row in candidates
            if clean_cell(row.get("canonical_opportunity_id")) == canonical_id
            and benchmark.normalize_title_key(row.get("title") or row.get("canonical_title")) == title_key
        ]
        if len(matches) == 1:
            return matches[0], "resolved", []
        if len(matches) > 1:
            return benchmark.build_snapshot_row(case), "ambiguous", matches

    title_matches = [
        row
        for row in candidates
        if benchmark.normalize_title_key(row.get("title") or row.get("canonical_title")) == title_key
    ]
    if len(title_matches) == 1:
        return title_matches[0], "partial", []
    if len(title_matches) > 1:
        return benchmark.build_snapshot_row(case), "ambiguous", title_matches

    return benchmark.build_snapshot_row(case), "partial", []


def compact_candidate_values(candidates, field, limit=8):
    values = unique(clean_cell(candidate.get(field)) for candidate in candidates if clean_cell(candidate.get(field)))
    if not values:
        return ""
    rendered = values[:limit]
    suffix = f" | +{len(values) - limit} more" if len(values) > limit else ""
    return " | ".join(rendered) + suffix


def compact_candidate_ids(candidates, limit=8):
    values = []
    for candidate in candidates[:limit]:
        parts = []
        for label, field in (
            ("job_id", "job_id"),
            ("external_id", "external_id"),
            ("canonical", "canonical_opportunity_id"),
            ("hash", "source_hash"),
        ):
            value = clean_cell(candidate.get(field))
            if value:
                parts.append(f"{label}={value}")
        values.append("; ".join(parts) or "no identifiers")
    if len(candidates) > limit:
        values.append(f"+{len(candidates) - limit} more")
    return " || ".join(values)


def classify_job_location(location):
    text = normalize(location)
    if not text or text in {"unknown", "not specified", "n/a", "none"}:
        return "unknown", "unknown", ""

    remote_status = "unknown"
    if "hybrid" in text:
        remote_status = "hybrid"
    elif "onsite" in text or "on-site" in text or "on site" in text:
        remote_status = "onsite"
    elif "remote" in text or "work from home" in text:
        remote_status = "remote"

    worldwide = any(term in text for term in ("worldwide", "global", "anywhere"))
    restricted = has_location_restriction(text)

    if remote_status == "remote" and worldwide:
        return "remote_worldwide", remote_status, ""
    if remote_status == "remote" and restricted:
        return "remote_restricted", remote_status, location
    if remote_status in {"hybrid", "onsite"} or (remote_status == "unknown" and restricted):
        return "onsite_or_hybrid_restricted", remote_status, location
    if remote_status == "remote":
        return "unknown", remote_status, ""
    return "unknown", remote_status, ""


def has_location_restriction(text):
    if any(term in text for term in ("selected locations", "specific locations", "must be based")):
        return True
    if " - " in text or "," in text:
        return any(term in text for term in LOCATION_COUNTRY_TERMS)
    return any(re.search(rf"\b{re.escape(term)}\b", text) for term in LOCATION_COUNTRY_TERMS)


def requirements_excerpt(row):
    fields = [
        row.get("title"),
        row.get("location"),
        row.get("department"),
        row.get("expertise"),
        row.get("source_category"),
        row.get("commitment"),
    ]
    return compact_text(" | ".join(clean_cell(value) for value in fields if clean_cell(value)), 320)


def build_location_eligibility(profile_context, job_context):
    scope = job_context["job_location_scope"]
    requirement = job_context["applicant_location_requirements"]
    profile_location = profile_context["profile_location"]
    if scope == "remote_worldwide":
        return {
            "location_eligibility": "not_applicable",
            "location_eligibility_reason": "Stored location indicates remote worldwide/global availability.",
        }
    if not requirement:
        return {
            "location_eligibility": "unknown",
            "location_eligibility_reason": "Stored data does not provide enough applicant-location evidence.",
        }
    if not profile_location:
        return {
            "location_eligibility": "unknown",
            "location_eligibility_reason": "Job appears geographically restricted, but profile location is unknown.",
        }

    req = normalize(requirement)
    loc = normalize(profile_location)
    if loc and (loc in req or req in loc):
        return {
            "location_eligibility": "eligible",
            "location_eligibility_reason": "Profile location appears in the stored applicant-location requirement.",
        }
    return {
        "location_eligibility": "ineligible",
        "location_eligibility_reason": "Stored profile location does not appear to match the applicant-location requirement.",
    }


def compact_list(value):
    if not value:
        return ""
    if isinstance(value, str):
        return value.strip()
    return " | ".join(str(item).strip() for item in value if str(item).strip())


def compact_text(value, limit):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def clean_cell(value):
    if value is None:
        return ""
    return str(value).strip()


def select_review_cases(evaluated, profiles, limit, profile_id=None, include_all=False):
    ranks = fixture_pool_ranks(evaluated)
    review_cases = []
    for item in evaluated:
        if profile_id and item.case["profile_id"] != profile_id:
            continue
        reasons = review_reasons(item, ranks[item.case["case_id"]])
        priority = priority_score(item, reasons)
        if include_all or reasons:
            review_cases.append(
                ReviewCase(
                    item=item,
                    profile=profiles[item.case["profile_id"]],
                    rank=ranks[item.case["case_id"]],
                    priority=priority,
                    reasons=reasons or ["reference_case"],
                    question=human_question(item, reasons),
                )
            )

    review_cases.sort(
        key=lambda case: (
            -case.priority,
            case.item.case["profile_id"],
            case.rank,
            case.item.case["case_id"],
        )
    )
    if include_all:
        return review_cases
    return review_cases[: max(1, limit)]


def fixture_pool_ranks(evaluated):
    by_profile = defaultdict(list)
    for item in evaluated:
        by_profile[item.case["profile_id"]].append(item)

    ranks = {}
    for items in by_profile.values():
        ranked = sorted(
            items,
            key=lambda item: (-item.score, item.case["source"], item.case["title"], item.case["case_id"]),
        )
        for index, item in enumerate(ranked, start=1):
            ranks[item.case["case_id"]] = index
    return ranks


def review_reasons(item, rank):
    reasons = []
    case = item.case
    if benchmark.is_visible_false_negative(item):
        reasons.append("visible_false_negative")
    if case.get("review_required"):
        reasons.append("review_required")
    if matcher_disagreement(item):
        reasons.append("matcher_disagreement")
    if rank <= 4:
        reasons.append("top_4_impact")
    elif rank <= 10:
        reasons.append("top_10_impact")
    if sparse_metadata(item):
        reasons.append("sparse_metadata")
    if case["profile_id"] in PRIORITY_PROFILES:
        reasons.append("priority_profile")
    if important_case(case):
        reasons.append("important_case")
    if case["expected_label"] == "weak" and case["profile_id"] in PRIORITY_PROFILES:
        reasons.append("ambiguous_weak_profile_label")
    return unique(reasons)


def priority_score(item, reasons):
    weights = {
        "visible_false_negative": 100,
        "review_required": 85,
        "important_case": 75,
        "matcher_disagreement": 45,
        "top_4_impact": 35,
        "top_10_impact": 20,
        "sparse_metadata": 30,
        "priority_profile": 15,
        "ambiguous_weak_profile_label": 15,
    }
    return sum(weights.get(reason, 0) for reason in reasons)


def matcher_disagreement(item):
    expected = item.case["expected_label"]
    expected_level = benchmark.SECTION_LEVELS[benchmark.expected_section(item.case)]
    current_level = benchmark.SECTION_LEVELS[item.current_section]
    if expected == "false_positive":
        return item.score >= benchmark.MEDIUM_SCORE_THRESHOLD or item.current_section in benchmark.PERSONALIZED_SECTIONS
    if expected == "strong":
        return item.match_label != "Strong" or current_level < expected_level
    if expected == "plausible":
        return current_level < expected_level
    if expected == "weak":
        return item.current_section in benchmark.PERSONALIZED_SECTIONS
    return False


def sparse_metadata(item):
    row = item.row
    if item.source_status == "fixture_snapshot":
        return True
    fields = [row.get("department"), row.get("expertise"), row.get("source_category")]
    return all(not value or str(value).lower() == "unknown" for value in fields)


def important_case(case):
    text = normalize(f"{case['source']} {case['title']}")
    return any(term in text for term in IMPORTANT_TITLE_TERMS)


def human_question(item, reasons):
    case = item.case
    title = case["title"]
    if "visible_false_negative" in reasons:
        return "Is sparse metadata causing a valid opportunity to be under-ranked, or is the current label too optimistic?"
    if case.get("review_required"):
        return "Should this draft label be approved, softened, or kept review-required?"
    if case["expected_label"] == "false_positive":
        return "Does this role require skills, credentials, or languages absent from the profile?"
    if case["expected_label"] == "weak":
        return "Should this stay Explore Market only, or is it a useful personalized shortlist item?"
    if "DataAnnotation" in case["source"] or "DataAnnotation" in title:
        return "Is this evergreen application genuinely profile-relevant, or merely a broad application surface?"
    return "Is this genuinely a strong fit, an adjacent opportunity, Explore Market only, or a false positive?"


def render_html(review_cases):
    now = datetime.now(timezone.utc).isoformat()
    by_profile = defaultdict(list)
    for case in review_cases:
        by_profile[case.item.case["profile_id"]].append(case)

    parts = [
        "<!doctype html>",
        "<html><head><meta charset='utf-8'>",
        "<title>Wahojobs Matching Label Review</title>",
        "<style>",
        "body{font-family:Arial,sans-serif;line-height:1.45;margin:24px;color:#202124;background:#fafafa}",
        "h1,h2{color:#111} .toc a{margin-right:12px}",
        ".case{background:#fff;border:1px solid #ddd;border-radius:8px;padding:16px;margin:14px 0}",
        ".meta{display:grid;grid-template-columns:180px 1fr;gap:6px 14px}",
        ".badge{display:inline-block;border-radius:999px;padding:3px 8px;margin:2px;font-size:12px;background:#eef2ff;color:#243b80}",
        ".badge.strong{background:#dcfce7;color:#166534}.badge.plausible{background:#dbeafe;color:#1e40af}",
        ".badge.weak{background:#fef3c7;color:#92400e}.badge.false_positive{background:#fee2e2;color:#991b1b}",
        ".question{font-weight:bold;background:#f8fafc;border-left:4px solid #64748b;padding:10px;margin-top:12px}",
        ".reasons{margin-top:8px}.small{color:#5f6368;font-size:13px}",
        "code{background:#f1f3f4;padding:1px 4px;border-radius:4px}",
        "</style></head><body>",
        "<h1>Wahojobs Matching Label Review</h1>",
        f"<p class='small'>Generated {escape(now)}. This is one-time calibration work, not a daily workflow.</p>",
        "<h2>Summary</h2>",
        f"<p>Selected cases: <strong>{len(review_cases)}</strong></p>",
        "<div class='toc'><strong>Profiles:</strong> "
        + " ".join(
            f"<a href='#{escape(profile_id)}'>{escape(profile_id)}</a>"
            for profile_id in sorted(by_profile)
        )
        + "</div>",
    ]

    for profile_id, cases in sorted(by_profile.items()):
        profile = cases[0].profile
        parts.extend(
            [
                f"<h2 id='{escape(profile_id)}'>{escape(profile.get('display_name') or profile_id)}</h2>",
                "<p class='small'>"
                + escape(profile.get("summary") or profile.get("notes") or "")
                + "</p>",
            ]
        )
        for review_case in cases:
            parts.append(render_html_case(review_case))

    parts.extend(["</body></html>"])
    return "\n".join(parts) + "\n"


def render_enriched_html(rows):
    now = datetime.now(timezone.utc).isoformat()
    by_profile = defaultdict(list)
    for row in rows:
        by_profile[row["profile_id"]].append(row)

    parts = [
        "<!doctype html>",
        "<html><head><meta charset='utf-8'>",
        "<title>Wahojobs Matching Label Review</title>",
        "<style>",
        "body{font-family:Arial,sans-serif;line-height:1.45;margin:24px;color:#202124;background:#fafafa}",
        "h1,h2{color:#111}.toc a{margin-right:12px}",
        ".case{background:#fff;border:1px solid #ddd;border-radius:8px;padding:16px;margin:14px 0}",
        ".profile-panel{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px;margin:10px 0 16px}",
        ".meta{display:grid;grid-template-columns:220px 1fr;gap:6px 14px}",
        ".badge{display:inline-block;border-radius:999px;padding:3px 8px;margin:2px;font-size:12px;background:#eef2ff;color:#243b80}",
        ".badge.strong{background:#dcfce7;color:#166534}.badge.plausible{background:#dbeafe;color:#1e40af}",
        ".badge.weak{background:#fef3c7;color:#92400e}.badge.false_positive{background:#fee2e2;color:#991b1b}",
        ".badge.warn{background:#fee2e2;color:#991b1b}.badge.context{background:#f1f5f9;color:#334155}",
        ".question{font-weight:bold;background:#f8fafc;border-left:4px solid #64748b;padding:10px;margin-top:12px}",
        ".warning{background:#fff7ed;border-left:4px solid #f97316;padding:10px;margin:10px 0}",
        ".ambiguous{background:#f8fafc;border:1px solid #cbd5e1;border-radius:8px;padding:12px;margin:10px 0}",
        "table{border-collapse:collapse;width:100%;font-size:13px;margin-top:8px}th,td{border:1px solid #e2e8f0;padding:6px;text-align:left;vertical-align:top}th{background:#f1f5f9}",
        ".small{color:#5f6368;font-size:13px}code{background:#f1f3f4;padding:1px 4px;border-radius:4px}",
        "a{color:#1d4ed8}",
        "</style></head><body>",
        "<h1>Wahojobs Matching Label Review</h1>",
        f"<p class='small'>Enriched {escape(now)}. This preserves the current CSV batch and adds review context only.</p>",
        "<h2>Summary</h2>",
        f"<p>Review cases: <strong>{len(rows)}</strong></p>",
        "<div class='toc'><strong>Profiles:</strong> "
        + " ".join(
            f"<a href='#{escape(profile_id)}'>{escape(profile_id)}</a>"
            for profile_id in sorted(by_profile)
        )
        + "</div>",
    ]

    for profile_id, profile_rows in sorted(by_profile.items()):
        first = profile_rows[0]
        parts.extend(
            [
                f"<h2 id='{escape(profile_id)}'>{escape(first.get('profile_display_name') or profile_id)}</h2>",
                render_profile_panel(first),
            ]
        )
        for row in profile_rows:
            parts.append(render_enriched_html_case(row))

    parts.extend(["</body></html>"])
    return "\n".join(parts) + "\n"


def render_profile_panel(row):
    return "\n".join(
        [
            "<div class='profile-panel'>",
            "<div class='meta'>",
            f"<div>Education</div><div>{escape(row.get('profile_education_level') or '-')}</div>",
            f"<div>Domains</div><div>{escape(row.get('profile_degrees_or_domains') or '-')}</div>",
            f"<div>Languages</div><div>{escape(row.get('profile_languages') or '-')}</div>",
            f"<div>Skills</div><div>{escape(row.get('profile_skills') or '-')}</div>",
            f"<div>Preferences</div><div>{escape(row.get('profile_work_preferences') or '-')}</div>",
            f"<div>Constraints</div><div>{escape(row.get('profile_constraints') or '-')}</div>",
            f"<div>Targets</div><div>{escape(row.get('profile_target_opportunity_types') or '-')}</div>",
            f"<div>Profile location</div><div>{escape(row.get('profile_location') or 'Unknown')} "
            f"(<code>{escape(row.get('profile_location_status') or 'unknown')}</code>)</div>",
            f"<div>Notes</div><div>{escape(row.get('profile_notes') or '-')}</div>",
            "</div>",
            "</div>",
        ]
    )


def render_enriched_html_case(row):
    label = row["current_expected_label"]
    title = escape(row["title"])
    if row.get("job_url"):
        title_html = (
            f"<a href='{escape(row['job_url'])}' target='_blank' rel='noopener noreferrer'>{title}</a>"
        )
    else:
        title_html = title
    badges = [
        f"<span class='badge {escape(label)}'>{escape(label)}</span>",
        f"<span class='badge context'>location: {escape(row.get('location_eligibility') or 'unknown')}</span>",
        f"<span class='badge context'>{escape(row.get('job_context_resolution_status') or 'unavailable')}</span>",
    ]
    for reason in split_semicolon(row.get("gates_triggered")):
        badges.append(f"<span class='badge warn'>{escape(reason)}</span>")
    warning = ""
    if (
        row.get("job_location_scope") in {"remote_restricted", "onsite_or_hybrid_restricted"}
        and row.get("profile_location_status") == "unknown"
    ):
        warning = (
            "<div class='warning'><strong>Location review needed:</strong> "
            "this role appears geographically restricted, but the profile location is unknown. "
            "Do not infer residence from language.</div>"
        )
    ambiguous_section = render_ambiguous_candidates(row)
    return "\n".join(
        [
            f"<section class='case' id='{escape(row['case_id'])}'>",
            f"<h3>{escape(row['source'])}: {title_html}</h3>",
            "<div>" + " ".join(badges) + "</div>",
            warning,
            ambiguous_section,
            "<div class='meta'>",
            f"<div>Case ID</div><div><code>{escape(row['case_id'])}</code></div>",
            f"<div>Expected</div><div>{escape(label)} / {escape(row.get('current_expected_section') or '-')}</div>",
            f"<div>Current matcher</div><div>score {escape(row.get('current_score'))}, "
            f"{escape(row.get('current_match_label'))}, {escape(row.get('current_section'))}</div>",
            f"<div>Gates / penalties</div><div>{escape(row.get('gates_triggered') or '-')}</div>",
            f"<div>User-facing reasons</div><div>{escape(row.get('current_reasons') or '-')}</div>",
            f"<div>Stable job identifiers</div><div>{escape(render_identifier_summary(row) or '-')}</div>",
            f"<div>Job URL</div><div>{link_or_dash(row.get('job_url'))}</div>",
            f"<div>Job location</div><div>{escape(row.get('job_location') or '-')}</div>",
            f"<div>Applicant-location restriction</div><div>{escape(row.get('applicant_location_requirements') or '-')}</div>",
            f"<div>Location scope</div><div>{escape(row.get('job_location_scope') or 'unknown')}</div>",
            f"<div>Remote status</div><div>{escape(row.get('job_remote_status') or 'unknown')}</div>",
            f"<div>Profile location</div><div>{escape(row.get('profile_location') or 'Unknown')} "
            f"(<code>{escape(row.get('profile_location_status') or 'unknown')}</code>)</div>",
            f"<div>Location eligibility</div><div><strong>{escape(row.get('location_eligibility') or 'unknown')}</strong>: "
            f"{escape(row.get('location_eligibility_reason') or '-')}</div>",
            f"<div>Job context</div><div>{escape(row.get('job_context_source') or '-')} / "
            f"{escape(row.get('job_context_resolution_status') or '-')}</div>",
            f"<div>Job excerpt</div><div>{escape(row.get('job_description_or_requirements_excerpt') or '-')}</div>",
            f"<div>Regression rule</div><div>{escape(row.get('regression_rule') or '-')}</div>",
            f"<div>Review required</div><div>{escape(row.get('current_review_required') or '-')}</div>",
            f"<div>Fixture rationale</div><div>{escape(row.get('fixture_rationale') or '-')}</div>",
            "</div>",
            f"<div class='question'>{escape(row.get('human_review_question') or '')}</div>",
            "</section>",
        ]
    )


def render_ambiguous_candidates(row):
    candidates = row.get("_ambiguous_candidates") or []
    if not candidates:
        return ""
    rows = []
    for candidate in candidates:
        location = clean_cell(candidate.get("location"))
        scope, _remote_status, requirements = classify_job_location(location)
        rows.append(
            "<tr>"
            f"<td>{escape(candidate.get('source') or '')}</td>"
            f"<td>{escape(candidate.get('title') or candidate.get('canonical_title') or '')}</td>"
            f"<td>{escape(location or '-')}</td>"
            f"<td>{escape(requirements or '-')}</td>"
            f"<td>{escape(scope or 'unknown')}</td>"
            f"<td>{link_or_dash(candidate.get('url'))}</td>"
            f"<td>{escape(candidate_identifier_summary(candidate) or '-')}</td>"
            "</tr>"
        )
    return "\n".join(
        [
            "<div class='ambiguous'>",
            "<strong>Ambiguous job candidates</strong>",
            "<p class='small'>This case matched multiple stored rows. Candidate URLs and IDs are shown for review only; no candidate is treated as the definitive job URL.</p>",
            "<table>",
            "<thead><tr><th>Source</th><th>Title</th><th>Job location</th><th>Applicant-location requirement</th><th>Location scope</th><th>Candidate URL</th><th>Identifiers</th></tr></thead>",
            "<tbody>",
            *rows,
            "</tbody></table>",
            "</div>",
        ]
    )


def render_identifier_summary(row):
    parts = []
    for label, field in (
        ("job_id", "job_id"),
        ("external_id", "job_external_id"),
        ("canonical", "job_canonical_opportunity_id"),
        ("hash", "job_source_hash"),
    ):
        value = clean_cell(row.get(field))
        if value:
            parts.append(f"{label}={value}")
    return "; ".join(parts)


def candidate_identifier_summary(candidate):
    parts = []
    for label, field in (
        ("job_id", "job_id"),
        ("external_id", "external_id"),
        ("canonical", "canonical_opportunity_id"),
        ("hash", "source_hash"),
    ):
        value = clean_cell(candidate.get(field))
        if value:
            parts.append(f"{label}={value}")
    return "; ".join(parts)


def link_or_dash(url):
    if not url:
        return "-"
    escaped = escape(url)
    return f"<a href='{escaped}' target='_blank' rel='noopener noreferrer'>{escaped}</a>"


def split_semicolon(value):
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def render_html_case(review_case):
    item = review_case.item
    case = item.case
    label = case["expected_label"]
    badges = [
        f"<span class='badge {escape(label)}'>{escape(label)}</span>",
        *(f"<span class='badge'>{escape(reason.replace('_', ' '))}</span>" for reason in review_case.reasons),
    ]
    return "\n".join(
        [
            f"<section class='case' id='{escape(case['case_id'])}'>",
            f"<h3>{escape(case['source'])}: {escape(case['title'])}</h3>",
            "<div>" + " ".join(badges) + "</div>",
            "<div class='meta'>",
            f"<div>Case ID</div><div><code>{escape(case['case_id'])}</code></div>",
            f"<div>Expected</div><div>{escape(label)} / {escape(benchmark.expected_section(case))}</div>",
            f"<div>Current matcher</div><div>score {item.score}, {escape(item.match_label)}, {escape(item.current_section)}</div>",
            f"<div>Gates / penalties</div><div>{escape(', '.join(item.penalties) or '-')}</div>",
            f"<div>User-facing reasons</div><div>{escape(', '.join(item.reasons) or '-')}</div>",
            f"<div>Regression rule</div><div>{escape(case.get('regression_rule') or '-')}</div>",
            f"<div>Review required</div><div>{'yes' if case.get('review_required') else 'no'}</div>",
            f"<div>Fixture rationale</div><div>{escape(case['rationale'])}</div>",
            "</div>",
            f"<div class='question'>{escape(review_case.question)}</div>",
            "</section>",
        ]
    )


def write_csv(review_cases, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    write_csv_rows([csv_row(review_case) for review_case in review_cases], path, CSV_COLUMNS)


def write_csv_rows(rows, path, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in fieldnames})
    os.replace(tmp, path)


def enriched_fieldnames(original_fieldnames):
    result = []
    human_start = min(
        (original_fieldnames.index(column) for column in HUMAN_REVIEW_COLUMNS if column in original_fieldnames),
        default=len(original_fieldnames),
    )
    for column in original_fieldnames[:human_start]:
        if column not in CONTEXT_COLUMNS and column not in result:
            result.append(column)
    for column in CONTEXT_COLUMNS:
        if column not in result:
            result.append(column)
    for column in original_fieldnames[human_start:]:
        if column not in CONTEXT_COLUMNS and column not in result:
            result.append(column)
    for column in HUMAN_REVIEW_COLUMNS:
        if column not in result:
            result.append(column)
    return result


def validate_enrichment_preserved(before_rows, after_rows, original_fieldnames):
    if len(before_rows) != len(after_rows):
        raise SystemExit(
            f"Enrichment changed row count: before={len(before_rows)} after={len(after_rows)}"
        )
    before_case_ids = [row["case_id"] for row in before_rows]
    after_case_ids = [row["case_id"] for row in after_rows]
    if before_case_ids != after_case_ids:
        raise SystemExit("Enrichment changed the ordered case_id list. Aborting.")

    preserved_columns = [
        column for column in original_fieldnames
        if column not in CONTEXT_COLUMNS
    ]
    changed = []
    for index, (before, after) in enumerate(zip(before_rows, after_rows), start=1):
        for column in preserved_columns:
            if before.get(column, "") != after.get(column, ""):
                changed.append((index, before.get("case_id", ""), column))
                break
    if changed:
        row_number, case_id, column = changed[0]
        raise SystemExit(
            "Enrichment changed a pre-existing non-context value. "
            f"Row {row_number}, case_id={case_id}, column={column}. Aborting."
        )
    return {
        "row_count": len(before_rows),
        "case_ids_identical": True,
        "preserved_columns": preserved_columns,
    }


def csv_row(review_case):
    item = review_case.item
    case = item.case
    row = {
        "review_priority": review_case.priority,
        "case_id": case["case_id"],
        "profile_id": case["profile_id"],
        "source": case["source"],
        "title": case["title"],
        "current_expected_label": case["expected_label"],
        "current_expected_section": benchmark.expected_section(case),
        "current_review_required": str(bool(case.get("review_required"))).lower(),
        "label_source": case.get("label_source", ""),
        "regression_rule": case.get("regression_rule", ""),
        "current_score": item.score,
        "current_match_label": item.match_label,
        "current_section": item.current_section,
        "gates_triggered": "; ".join(item.penalties),
        "current_reasons": "; ".join(item.reasons),
        "fixture_rationale": case["rationale"],
        "human_review_question": review_case.question,
        "human_label": "",
        "human_expected_section": "",
        "human_review_required": "",
        "human_notes": "",
        "approved_by": "",
        "approved_at": "",
    }
    for column in CONTEXT_COLUMNS:
        row[column] = ""
    row["job_id"] = clean_cell(case.get("job_id"))
    row["job_external_id"] = clean_cell(case.get("external_id"))
    row["job_source_hash"] = clean_cell(case.get("source_hash"))
    row["job_canonical_opportunity_id"] = clean_cell(case.get("canonical_opportunity_id"))
    row["job_url"] = clean_cell(case.get("url"))
    return row


def render_summary(review_cases, args):
    by_profile = Counter(case.item.case["profile_id"] for case in review_cases)
    by_reason = Counter(reason for case in review_cases for reason in case.reasons)
    visible_fn = [case for case in review_cases if "visible_false_negative" in case.reasons]
    review_required = [case for case in review_cases if "review_required" in case.reasons]
    top_impact = [
        case for case in review_cases
        if "top_4_impact" in case.reasons or "top_10_impact" in case.reasons
    ]
    sparse = [case for case in review_cases if "sparse_metadata" in case.reasons]
    lines = [
        "# Matching Label Review Summary",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "This packet is for one-time benchmark calibration. It does not change runtime matching behavior.",
        "",
        f"- Cases prioritized: **{len(review_cases)}**",
        f"- Limit: **{'all' if args.include_all else args.limit}**",
        f"- Profile filter: **{args.profile or 'none'}**",
        f"- HTML review file: `{relative(HTML_PATH)}`",
        f"- Editable CSV: `{relative(CSV_PATH)}`",
        "",
        "## Cases By Profile",
        "",
        *[f"- `{profile}`: {count}" for profile, count in by_profile.most_common()],
        "",
        "## Cases By Review Reason",
        "",
        *[f"- `{reason}`: {count}" for reason, count in by_reason.most_common()],
        "",
        "## Key Buckets",
        "",
        f"- Visible false negatives: {len(visible_fn)}",
        f"- Review-required cases: {len(review_required)}",
        f"- Top-4 / top-10 impact cases: {len(top_impact)}",
        f"- Sparse metadata cases: {len(sparse)}",
        "",
        "## Human Review Instructions",
        "",
        "1. Open the HTML file for context.",
        "2. Edit only the human columns in the CSV.",
        "3. Use labels: `strong`, `plausible`, `weak`, `false_positive`.",
        "4. Use sections: `do_these_first`, `best_matches`, `also_worth_reviewing`, `explore_only`, `exclude`.",
        "5. Leave a row blank if no decision is approved yet.",
        "",
        "## Apply Instructions",
        "",
        "Dry run first:",
        "",
        "```bash",
        "python scripts/matching_label_review.py apply --input exports/matching_label_review.csv --dry-run",
        "```",
        "",
        "Apply only after approved human decisions are present:",
        "",
        "```bash",
        "python scripts/matching_label_review.py apply --input exports/matching_label_review.csv --yes",
        "python scripts/matching_quality_report.py",
        "```",
    ]
    return "\n".join(lines) + "\n"


def render_enriched_summary(
    input_path,
    before_rows,
    enriched_rows,
    stats,
    profile_unknown_locations,
    restricted_unknown_profile,
    preserved,
):
    by_profile = Counter(row["profile_id"] for row in enriched_rows)
    context_status = Counter(row.get("job_context_resolution_status") or "unavailable" for row in enriched_rows)
    location_scope = Counter(row.get("job_location_scope") or "unknown" for row in enriched_rows)
    location_eligibility = Counter(row.get("location_eligibility") or "unknown" for row in enriched_rows)
    usable_urls = [row for row in enriched_rows if row.get("job_url")]
    unavailable_or_ambiguous = [
        row for row in enriched_rows
        if row.get("job_context_resolution_status") in {"unavailable", "ambiguous"}
    ]
    ambiguous_rows = [
        row for row in enriched_rows
        if row.get("job_context_resolution_status") == "ambiguous"
    ]
    restricted = [
        row for row in enriched_rows
        if row.get("applicant_location_requirements")
    ]
    lines = [
        "# Matching Label Review Summary",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "This enrichment preserves the existing review batch and adds review context only.",
        "",
        "## Preservation Check",
        "",
        f"- Input batch: `{relative(input_path)}`",
        f"- Rows before: **{len(before_rows)}**",
        f"- Rows after: **{len(enriched_rows)}**",
        f"- Ordered `case_id` list identical: **{'yes' if preserved['case_ids_identical'] else 'no'}**",
        "- Pre-existing reference, matcher-output, rationale, question, and human-review values preserved: **yes**",
        "- No cases were added or removed.",
        "- `review_priority`, expected labels/sections, review-required flags, label source, regression rules, scores, matcher labels, sections, gates, reasons, rationale, questions, and existing human-review values were not changed.",
        "",
        "## Context Added",
        "",
        f"- Cases with usable job URL: **{len(usable_urls)}**",
        f"- Cases with unavailable or ambiguous URL/context resolution: **{len(unavailable_or_ambiguous)}**",
        f"- Ambiguous cases with candidate rows: **{len(ambiguous_rows)}**",
        f"- Cases with explicit location restrictions: **{len(restricted)}**",
        f"- Restricted cases where profile location is unknown: **{len(restricted_unknown_profile)}**",
        f"- Profiles with unknown location: **{len(profile_unknown_locations)}**",
        "",
        "## Cases By Profile",
        "",
        *[f"- `{profile}`: {count}" for profile, count in by_profile.most_common()],
        "",
        "## Job Context Resolution",
        "",
        *[f"- `{key}`: {value}" for key, value in context_status.most_common()],
        "",
        "## Ambiguous Job Candidates",
        "",
        *render_ambiguous_summary_lines(ambiguous_rows),
        "",
        "## Location Scope",
        "",
        *[f"- `{key}`: {value}" for key, value in location_scope.most_common()],
        "",
        "## Location Eligibility",
        "",
        *[f"- `{key}`: {value}" for key, value in location_eligibility.most_common()],
        "",
        "## Restricted Cases With Unknown Profile Location",
        "",
        *([
            f"- `{row['case_id']}`: {row['source']} - {row['title']} ({row.get('applicant_location_requirements')})"
            for row in enriched_rows
            if row["case_id"] in restricted_unknown_profile
        ] or ["- None"]),
        "",
        "## Review Notes",
        "",
        "- Missing profile location remains `unknown`; no profile locations were invented.",
        "- Speaking a language was not treated as evidence of residence.",
        "- `Remote` alone was not treated as worldwide availability.",
        "- This did not change production matching behavior, scores, gates, planner behavior, UI behavior, crawlers, schema, product-state data, live market estimates, sample profiles, or golden-set labels.",
        "",
        "## Human Review Instructions",
        "",
        "1. Open the HTML file for context.",
        "2. Edit only the human columns in the CSV.",
        "3. Use labels: `strong`, `plausible`, `weak`, `false_positive`.",
        "4. Use sections: `do_these_first`, `best_matches`, `also_worth_reviewing`, `explore_only`, `exclude`.",
        "5. Leave a row blank if no decision is approved yet.",
        "",
        "Dry run before applying approved decisions:",
        "",
        "```bash",
        "python scripts/matching_label_review.py apply --input exports/matching_label_review.csv --dry-run",
        "```",
    ]
    return "\n".join(lines) + "\n"


def render_ambiguous_summary_lines(rows):
    if not rows:
        return ["- None"]
    lines = []
    for row in rows:
        candidates = row.get("_ambiguous_candidates") or []
        lines.append(
            f"- `{row['case_id']}`: {row['source']} - {row['title']} "
            f"({len(candidates)} candidate rows)"
        )
        for candidate in candidates[:8]:
            location = clean_cell(candidate.get("location")) or "-"
            url = clean_cell(candidate.get("url")) or "-"
            ids = candidate_identifier_summary(candidate) or "-"
            lines.append(f"  - Location: {location}; URL: {url}; IDs: {ids}")
        if len(candidates) > 8:
            lines.append(f"  - +{len(candidates) - 8} more candidate rows")
    return lines


def apply_reviews(args) -> None:
    fixture = benchmark.load_fixture()
    case_index = {}
    for case in fixture["cases"]:
        if case["case_id"] in case_index:
            raise SystemExit(f"Duplicate case_id in fixture: {case['case_id']}")
        case_index[case["case_id"]] = case

    rows = read_review_csv(args.input)
    changes = collect_changes(rows, case_index)
    reviewed_rows = count_reviewed_rows(rows)
    print("Matching Label Review Apply")
    print("===========================")
    print(f"Input: {args.input}")
    print(f"Rows with human decisions: {reviewed_rows}")
    print(f"Rows with pending fixture updates: {len(changes)}")
    if not changes:
        print("No pending fixture updates found. Nothing to apply.")
        print("Rerun benchmark after approved changes with: python scripts/matching_quality_report.py")
        return

    for change in changes:
        print_change(change)

    if not args.yes:
        print("")
        print("Dry run only. Use --yes to modify tests/fixtures/matching_golden_set.json.")
        print("Rerun benchmark after approved changes with: python scripts/matching_quality_report.py")
        return

    for change in changes:
        case = change["case"]
        for field, value in change["updates"].items():
            case[field] = value
        case["label_source"] = "human_reviewed"

    backup = FIXTURE_PATH.with_suffix(
        FIXTURE_PATH.suffix + f".bak-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    )
    shutil.copy2(FIXTURE_PATH, backup)
    atomic_write_json(FIXTURE_PATH, fixture)
    print("")
    print(f"Applied {len(changes)} reviewed decisions.")
    print(f"Backup written to: {backup}")
    print("Rerun benchmark: python scripts/matching_quality_report.py")


def read_review_csv(path):
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
    except FileNotFoundError:
        raise SystemExit(f"Review CSV not found: {path}")
    missing = [column for column in APPLY_REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
    if missing:
        raise SystemExit(f"Review CSV is missing columns: {', '.join(missing)}")
    seen = set()
    for row in rows:
        case_id = row.get("case_id", "").strip()
        if not case_id:
            raise SystemExit("Review CSV contains a row with blank case_id.")
        if case_id in seen:
            raise SystemExit(f"Review CSV contains duplicate case_id: {case_id}")
        seen.add(case_id)
    return rows


def collect_changes(rows, case_index):
    changes = []
    for row in rows:
        case_id = row["case_id"].strip()
        if case_id not in case_index:
            raise SystemExit(f"Review CSV case_id not found in fixture: {case_id}")
        updates = review_updates(row)
        if not updates:
            continue
        case = case_index[case_id]
        if case.get("label_source") != "human_reviewed":
            updates.update(identifier_updates(row, case))
        if reviewed_values_already_applied(case, updates):
            continue
        changes.append({"case": case, "row": row, "updates": updates})
    return changes


def count_reviewed_rows(rows):
    human_fields = [
        "human_label",
        "human_expected_section",
        "human_review_required",
        "human_notes",
        "approved_by",
        "approved_at",
    ]
    return sum(
        1 for row in rows
        if any(row.get(field, "").strip() for field in human_fields)
    )


def reviewed_values_already_applied(case, updates):
    if case.get("label_source") != "human_reviewed":
        return False
    return all(case.get(field) == value for field, value in updates.items())


def review_updates(row):
    human_fields = [
        "human_label",
        "human_expected_section",
        "human_review_required",
        "human_notes",
        "approved_by",
        "approved_at",
    ]
    if not any(row.get(field, "").strip() for field in human_fields):
        return {}

    updates = {}
    label = row.get("human_label", "").strip()
    section = row.get("human_expected_section", "").strip()
    review_required = row.get("human_review_required", "").strip()

    if label:
        if label not in ALLOWED_LABELS:
            raise SystemExit(f"Invalid human_label for {row['case_id']}: {label}")
        updates["expected_label"] = label
    if section:
        if section not in ALLOWED_SECTIONS:
            raise SystemExit(f"Invalid human_expected_section for {row['case_id']}: {section}")
        updates["expected_section"] = section
    if review_required:
        lowered = review_required.lower()
        if lowered not in {"true", "false", "yes", "no", "1", "0"}:
            raise SystemExit(f"Invalid human_review_required for {row['case_id']}: {review_required}")
        updates["review_required"] = lowered in {"true", "yes", "1"}

    for csv_field, fixture_field in (
        ("human_notes", "human_notes"),
        ("approved_by", "approved_by"),
        ("approved_at", "approved_at"),
    ):
        value = row.get(csv_field, "").strip()
        if value:
            updates[fixture_field] = value

    return updates


def identifier_updates(row, case):
    updates = {}
    for csv_field, fixture_field in (
        ("job_url", "url"),
        ("job_external_id", "external_id"),
        ("job_source_hash", "source_hash"),
        ("job_canonical_opportunity_id", "canonical_opportunity_id"),
    ):
        value = row.get(csv_field, "").strip()
        if value and case.get(fixture_field) != value:
            updates[fixture_field] = value
    return updates


def print_change(change):
    case = change["case"]
    updates = change["updates"]
    print("")
    print(f"- {case['case_id']}: {case['source']} - {case['title']}")
    for field, new_value in updates.items():
        old_value = case.get(field)
        print(f"  {field}: {old_value!r} -> {new_value!r}")
    print("  label_source: {!r} -> 'human_reviewed'".format(case.get("label_source")))


def atomic_write_json(path, payload):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def unique(values):
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def normalize(value):
    return str(value or "").strip().lower()


def escape(value):
    return html.escape(str(value or ""), quote=True)


def format_counter(counter):
    if not counter:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in counter.most_common())


def relative(path):
    try:
        return path.relative_to(ROOT)
    except ValueError:
        return path


if __name__ == "__main__":
    main()
