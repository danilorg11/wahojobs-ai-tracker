#!/usr/bin/env python3
"""Generate a human-review batch for sparse golden-set matcher snapshots.

This tool is intentionally review-only. It reads the calibrated golden set and
the current active DB rows for diagnostics, then writes spreadsheet/HTML/summary
artifacts that help a human decide whether snapshot metadata should be enriched
in a later, explicit task. It does not mutate the fixture, matching behavior, or
database rows.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import matching_quality_report as benchmark  # noqa: E402

CSV_PATH = ROOT / "exports" / "matching_metadata_enrichment_review.csv"
HTML_PATH = ROOT / "exports" / "matching_metadata_enrichment_review.html"
SUMMARY_PATH = ROOT / "exports" / "matching_metadata_enrichment_review_summary.md"

FOCUSED_CASE_ORDER = [
    "portuguese_english_reviewer_010",
    "multilingual_translator_010",
    "beginner_bilingual_no_degree_005",
    "lawyer_002",
    "biology_or_medicine_academic_003",
    "biology_or_medicine_academic_007",
    "phd_history_researcher_011",
    "generalist_no_degree_013",
    "phd_history_researcher_013",
]

FOCUSED_CASE_ISSUES = {
    "portuguese_english_reviewer_010": {
        "fidelity_classification": "fixture_missing_structured_metadata",
        "issue_group": "DataAnnotation Bilingual / evergreen archetype issues",
        "primary_issue": "dataannotation_bilingual_evergreen_archetype_gap",
        "why_review": (
            "Bilingual DataAnnotation evergreen archetype is human-labeled as useful, "
            "but the snapshot still carries live-posting/api-feed defaults and lacks "
            "structured language/taxonomy fields."
        ),
    },
    "multilingual_translator_010": {
        "fidelity_classification": "fixture_missing_structured_metadata",
        "issue_group": "DataAnnotation Bilingual / evergreen archetype issues",
        "primary_issue": "dataannotation_bilingual_evergreen_archetype_gap",
        "why_review": (
            "Bilingual DataAnnotation evergreen archetype is relevant to the profile, "
            "but the snapshot needs human-approved fixture-only archetype metadata."
        ),
    },
    "beginner_bilingual_no_degree_005": {
        "fidelity_classification": "fixture_missing_structured_metadata",
        "issue_group": "DataAnnotation Bilingual / evergreen archetype issues",
        "primary_issue": "dataannotation_bilingual_evergreen_archetype_gap",
        "why_review": (
            "Broad bilingual evergreen application is plausible for this beginner "
            "profile, but structured language/taxonomy fields are missing."
        ),
    },
    "lawyer_002": {
        "fidelity_classification": "fixture_missing_structured_metadata",
        "issue_group": "IP/legal metadata gaps",
        "primary_issue": "legal_ip_metadata_gap",
        "why_review": (
            "The human label treats IP Expert as legal-domain work, but the fixture "
            "snapshot lacks legal/IP structured category fields. The title is context "
            "only and must not be auto-converted into legal taxonomy."
        ),
    },
    "biology_or_medicine_academic_003": {
        "fidelity_classification": "fixture_missing_structured_metadata",
        "issue_group": "Microbiology/dermatology medical/science metadata gaps",
        "primary_issue": "medical_science_metadata_gap",
        "why_review": (
            "The human label treats Microbiology Specialist as direct science-domain "
            "work, but the snapshot lacks structured science/medical metadata."
        ),
    },
    "biology_or_medicine_academic_007": {
        "fidelity_classification": "fixture_missing_structured_metadata",
        "issue_group": "Microbiology/dermatology medical/science metadata gaps",
        "primary_issue": "medical_science_metadata_gap",
        "why_review": (
            "The human label treats Academic Dermatologist as medical-domain work, "
            "but the snapshot lacks structured medical/credential metadata."
        ),
    },
    "phd_history_researcher_011": {
        "fidelity_classification": "product_policy_not_represented",
        "issue_group": "Product-policy-not-represented cases",
        "primary_issue": "professional_domain_gate_not_represented",
        "why_review": (
            "The human label excludes a medical-specialty role for a humanities "
            "profile. That product policy is not represented by current structured "
            "fixture fields, so matching changes should wait for explicit metadata "
            "or gate design."
        ),
    },
    "generalist_no_degree_013": {
        "fidelity_classification": "ambiguous_identity",
        "issue_group": "Ambiguous identity cases",
        "primary_issue": "unsafe_meridial_pci_title_variants",
        "why_review": (
            "Meridial PCI appears to have multiple title-only live candidates. The "
            "review batch must not select a representative row without human-approved "
            "stable identity."
        ),
    },
    "phd_history_researcher_013": {
        "fidelity_classification": "fixture_missing_structured_metadata",
        "issue_group": "Location/actionability gaps",
        "primary_issue": "location_actionability_metadata_gap",
        "why_review": (
            "The title mentions Morocco, but the snapshot lacks structured applicant "
            "location requirements. The title-location clue is diagnostic only."
        ),
    },
}

REVIEW_DECISIONS = [
    "leave_unchanged",
    "approve_metadata_update",
    "needs_more_research",
    "ambiguous_keep_fixture_only",
    "tie_to_stable_row",
    "remove_or_replace_case_later",
]

CURRENT_METADATA_FIELDS = [
    "inventory_model",
    "opportunity_kind",
    "market_count_policy",
    "availability_basis",
    "source_category",
    "expertise",
    "department",
    "location",
    "language",
    "language_locale",
    "required_languages",
    "commitment",
    "employment_type",
    "profile_or_opportunity_kind",
]

PROPOSED_FIELDS = [
    "proposed_inventory_model",
    "proposed_opportunity_kind",
    "proposed_market_count_policy",
    "proposed_availability_basis",
    "proposed_source_category",
    "proposed_expertise",
    "proposed_department",
    "proposed_location",
    "proposed_language",
    "proposed_language_locale",
    "proposed_required_languages",
    "proposed_canonical_opportunity_id",
    "proposed_url",
    "proposed_external_id",
    "proposed_source_hash",
    "proposed_enrichment_notes",
]

CSV_COLUMNS = [
    "case_id",
    "profile_id",
    "profile_name",
    "profile_summary",
    "source",
    "title",
    "url",
    "canonical_opportunity_id",
    "external_id",
    "source_hash",
    "snapshot_source_type",
    "fidelity_classification",
    "issue_group",
    "primary_issue",
    "ambiguity_reason",
    "diagnostic_candidate_count",
    "human_label",
    "human_expected_section",
    "human_notes",
    "current_benchmark_label",
    "current_effective_section",
    "score",
    "raw_match_label",
    "raw_product_section",
    *CURRENT_METADATA_FIELDS,
    "missing_fields",
    "suspicious_fields",
    "internal_inconsistencies",
    "similar_live_candidates_count",
    "exact_stable_live_match_available",
    "unsafe_title_or_similarity_candidates",
    "candidate_summary_for_diagnostics",
    "why_review",
    "reviewer_should_decide",
    "matching_changes_blocked_until_review",
    "review_decision",
    *PROPOSED_FIELDS,
]

INCLUSION_CLASSIFICATIONS = {
    "fixture_sparse_but_faithful",
    "fixture_missing_structured_metadata",
    "ambiguous_identity",
    "product_policy_not_represented",
    "needs_manual_review",
}


def main() -> None:
    args = parse_args()
    if args.command != "generate":
        raise SystemExit("Only the 'generate' review-artifact command is supported.")

    rows, summary = build_review_batch()
    write_csv(rows, args.csv)
    write_html(rows, args.html)
    write_summary(rows, summary, args.summary)
    print("Matching Metadata Enrichment Review")
    print("===================================")
    print("No fixture, matcher, database, or product-state rows were changed.")
    print(f"Rows in review batch: {len(rows)}")
    print(f"Fidelity classifications: {format_counter(summary['by_fidelity'])}")
    print(f"Primary issues: {format_counter(summary['by_primary_issue'])}")
    print(f"Missing fields: {format_counter(summary['missing_fields'])}")
    print(f"Wrote CSV: {relative(args.csv)}")
    print(f"Wrote HTML: {relative(args.html)}")
    print(f"Wrote summary: {relative(args.summary)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate review artifacts for sparse golden-set matcher snapshots."
    )
    subparsers = parser.add_subparsers(dest="command")
    generate = subparsers.add_parser("generate", help="Generate metadata enrichment review artifacts.")
    generate.add_argument("--csv", type=Path, default=CSV_PATH)
    generate.add_argument("--html", type=Path, default=HTML_PATH)
    generate.add_argument("--summary", type=Path, default=SUMMARY_PATH)
    args = parser.parse_args()
    if args.command is None:
        args.command = "generate"
        args.csv = CSV_PATH
        args.html = HTML_PATH
        args.summary = SUMMARY_PATH
    return args


def build_review_batch() -> tuple[list[dict], dict]:
    fixture = benchmark.load_fixture()
    profiles = benchmark.load_benchmark_profiles(fixture)
    db_rows = benchmark.load_benchmark_db_rows()
    evaluated = [
        benchmark.evaluate_case(case, profiles[case["profile_id"]], db_rows, benchmark.matcher)
        for case in fixture["cases"]
    ]
    rows = select_review_rows(evaluated, profiles, db_rows)
    summary = summarize_rows(rows, evaluated)
    return rows, summary


def select_review_rows(evaluated: list[benchmark.EvaluatedCase], profiles: dict, db_rows: list[dict]) -> list[dict]:
    selected = []
    for item in evaluated:
        if item.case.get("label_source") != "human_reviewed":
            continue
        classification = classify_item(item, db_rows)
        if should_include(item, classification):
            selected.append(review_row(item, profiles[item.case["profile_id"]], db_rows, classification))

    selected.sort(key=review_sort_key)
    return selected


def should_include(item: benchmark.EvaluatedCase, classification: dict) -> bool:
    if item.case["case_id"] in FOCUSED_CASE_ISSUES:
        return True
    return classification["fidelity_classification"] in INCLUSION_CLASSIFICATIONS


def review_sort_key(row: dict) -> tuple[int, str, str]:
    try:
        focused_index = FOCUSED_CASE_ORDER.index(row["case_id"])
    except ValueError:
        focused_index = 10_000
    return (focused_index, row["issue_group"], row["case_id"])


def classify_item(item: benchmark.EvaluatedCase, db_rows: list[dict]) -> dict:
    case_id = item.case["case_id"]
    snapshot = item.case.get("matcher_input_snapshot") or {}
    metadata = snapshot.get("snapshot_metadata") or {}
    snapshot_type = metadata.get("snapshot_source_type") or metadata.get("snapshot_created_from") or ""

    if case_id in FOCUSED_CASE_ISSUES:
        focused = dict(FOCUSED_CASE_ISSUES[case_id])
        return {
            **focused,
            "ambiguity_reason": ambiguity_reason_for_item(item, focused["fidelity_classification"]),
            "reviewer_should_decide": reviewer_decision_prompt(focused["fidelity_classification"]),
            "matching_changes_blocked_until_review": "yes",
        }

    missing = snapshot_missing_fields(item)
    suspicious, inconsistencies = snapshot_suspicious_fields(item)
    exact_live = exact_stable_live_match(item.row, db_rows)

    if snapshot_type == "exact_live_db_resolution":
        fidelity = "exact_live_complete"
        issue_group = "Complete examples"
        primary_issue = "complete_exact_live_snapshot"
    elif not missing and not inconsistencies:
        fidelity = "fixture_complete"
        issue_group = "Complete examples"
        primary_issue = "complete_fixture_snapshot"
    elif missing_structured_metadata(missing, inconsistencies):
        fidelity = "fixture_missing_structured_metadata"
        issue_group = "Structured metadata gaps"
        primary_issue = "missing_structured_metadata"
    elif snapshot_type == "fixture_snapshot_fields":
        fidelity = "fixture_sparse_but_faithful"
        issue_group = "Sparse but faithful fixture snapshots"
        primary_issue = "sparse_fixture_snapshot"
    elif exact_live["status"] == "ambiguous":
        fidelity = "ambiguous_identity"
        issue_group = "Ambiguous identity cases"
        primary_issue = "ambiguous_stable_identifier"
    else:
        fidelity = "needs_manual_review"
        issue_group = "Needs manual review"
        primary_issue = "needs_manual_review"

    return {
        "fidelity_classification": fidelity,
        "issue_group": issue_group,
        "primary_issue": primary_issue,
        "ambiguity_reason": ambiguity_reason_for_item(item, fidelity),
        "why_review": why_review_text(fidelity, missing, suspicious, inconsistencies),
        "reviewer_should_decide": reviewer_decision_prompt(fidelity),
        "matching_changes_blocked_until_review": (
            "yes" if fidelity in {"fixture_missing_structured_metadata", "ambiguous_identity", "product_policy_not_represented"} else "no"
        ),
    }


def review_row(item: benchmark.EvaluatedCase, profile: dict, db_rows: list[dict], classification: dict) -> dict:
    row = item.row
    metadata = (item.case.get("matcher_input_snapshot") or {}).get("snapshot_metadata") or {}
    missing = snapshot_missing_fields(item)
    suspicious, inconsistencies = snapshot_suspicious_fields(item)
    exact_live = exact_stable_live_match(row, db_rows)
    candidates = item.resolution.diagnostic_candidates
    candidate_summary = candidate_summary_for_diagnostics(candidates)

    output = {
        "case_id": item.case["case_id"],
        "profile_id": item.case["profile_id"],
        "profile_name": profile.get("display_name") or profile.get("profile_id") or item.case["profile_id"],
        "profile_summary": compact(profile.get("notes") or profile.get("summary") or "", 260),
        "source": item.case.get("source") or row.get("source") or "",
        "title": item.case.get("title") or row.get("title") or "",
        "url": row.get("url") or "",
        "canonical_opportunity_id": stringify(row.get("canonical_opportunity_id")),
        "external_id": stringify(row.get("external_id")),
        "source_hash": stringify(row.get("source_hash")),
        "snapshot_source_type": metadata.get("snapshot_source_type") or metadata.get("snapshot_created_from") or "",
        "fidelity_classification": classification["fidelity_classification"],
        "issue_group": classification["issue_group"],
        "primary_issue": classification["primary_issue"],
        "ambiguity_reason": classification["ambiguity_reason"],
        "diagnostic_candidate_count": len(candidates),
        "human_label": item.case["expected_label"],
        "human_expected_section": benchmark.expected_section(item.case),
        "human_notes": item.case.get("human_notes") or "",
        "current_benchmark_label": item.evaluation_label,
        "current_effective_section": item.evaluation_section,
        "score": item.score,
        "raw_match_label": item.raw_match_label,
        "raw_product_section": item.raw_section,
        "missing_fields": join_list(missing),
        "suspicious_fields": join_list(suspicious),
        "internal_inconsistencies": join_list(inconsistencies),
        "similar_live_candidates_count": len(candidates),
        "exact_stable_live_match_available": exact_live["status"],
        "unsafe_title_or_similarity_candidates": "yes" if candidates else "no",
        "candidate_summary_for_diagnostics": candidate_summary,
        "why_review": classification.get("why_review") or why_review_text(
            classification["fidelity_classification"], missing, suspicious, inconsistencies
        ),
        "reviewer_should_decide": classification["reviewer_should_decide"],
        "matching_changes_blocked_until_review": classification["matching_changes_blocked_until_review"],
        "review_decision": "",
    }
    for field in CURRENT_METADATA_FIELDS:
        output[field] = metadata_value(row, field)
    for field in PROPOSED_FIELDS:
        output[field] = ""
    return output


def snapshot_missing_fields(item: benchmark.EvaluatedCase) -> list[str]:
    row = item.row
    fields = [
        "url",
        "external_id",
        "source_hash",
        "canonical_opportunity_id",
        "source_category",
        "expertise",
        "department",
        "opportunity_kind",
        "inventory_model",
        "market_count_policy",
        "availability_basis",
        "language",
        "language_locale",
        "required_languages",
    ]
    return [field for field in fields if missing_value(row.get(field))]


def snapshot_suspicious_fields(item: benchmark.EvaluatedCase) -> tuple[list[str], list[str]]:
    row = item.row
    suspicious = []
    inconsistencies = []
    inventory_model = stringify(row.get("inventory_model"))
    opportunity_kind = stringify(row.get("opportunity_kind"))
    availability_basis = stringify(row.get("availability_basis"))

    if inventory_model == "evergreen_application" and opportunity_kind == "live_posting":
        inconsistencies.append("evergreen_application snapshot has opportunity_kind=live_posting")
        suspicious.append("opportunity_kind")
    if inventory_model == "evergreen_application" and availability_basis == "api_feed":
        inconsistencies.append("evergreen_application snapshot has availability_basis=api_feed")
        suspicious.append("availability_basis")
    if all(stringify(row.get(field)).lower() == "unknown" for field in ("department", "expertise", "source_category")):
        suspicious.append("taxonomy")
    if item.case["case_id"] in {
        "portuguese_english_reviewer_010",
        "multilingual_translator_010",
        "beginner_bilingual_no_degree_005",
    }:
        if missing_value(row.get("language")) and missing_value(row.get("required_languages")):
            suspicious.append("language")
            inconsistencies.append("DataAnnotation Bilingual fixture lacks structured language metadata")
    if item.case["case_id"] == "phd_history_researcher_013":
        suspicious.append("applicant_location_requirements")
        inconsistencies.append("title contains Morocco context, but structured applicant location requirements are missing")
    return sorted(set(suspicious)), inconsistencies


def missing_structured_metadata(missing: list[str], inconsistencies: list[str]) -> bool:
    structured = {
        "source_category",
        "expertise",
        "department",
        "opportunity_kind",
        "inventory_model",
        "market_count_policy",
        "availability_basis",
    }
    return bool(structured.intersection(missing) or inconsistencies)


def ambiguity_reason_for_item(item: benchmark.EvaluatedCase, fidelity: str) -> str:
    if fidelity == "ambiguous_identity":
        if item.resolution.diagnostic_candidates:
            return (
                f"{len(item.resolution.diagnostic_candidates)} title/source diagnostic only candidates exist; "
                "no stable representative row was selected."
            )
        return "Opportunity identity needs human-approved stable representative before enrichment."
    if item.resolution.diagnostic_candidates:
        return "Title/source candidates are diagnostic only and were not used as fixture metadata."
    return ""


def why_review_text(fidelity: str, missing: list[str], suspicious: list[str], inconsistencies: list[str]) -> str:
    if fidelity == "fixture_missing_structured_metadata":
        return "Structured metadata is missing or internally inconsistent: " + join_list(missing + suspicious + inconsistencies)
    if fidelity == "fixture_sparse_but_faithful":
        return "Snapshot is fixture-derived and sparse; enrichment may improve future benchmark fidelity."
    if fidelity == "ambiguous_identity":
        return "Opportunity identity is ambiguous and must remain fixture-only until a human selects a stable row."
    if fidelity == "product_policy_not_represented":
        return "Human decision depends on product policy not yet captured by structured matcher fields."
    return "Manual review may be useful before future metadata enrichment."


def reviewer_decision_prompt(fidelity: str) -> str:
    if fidelity == "ambiguous_identity":
        return "Decide whether to keep fixture-only, choose a stable representative row later, or replace the case."
    if fidelity == "product_policy_not_represented":
        return "Decide whether future matcher work needs explicit policy/gate metadata before tuning."
    if fidelity == "fixture_missing_structured_metadata":
        return "Decide whether to approve fixture-only metadata updates, research a stable row, or leave unchanged."
    return "Decide whether enrichment is useful or the fixture should remain unchanged."


def exact_stable_live_match(row: dict, db_rows: list[dict]) -> dict:
    source_slug = benchmark.normalize_slug(row.get("source_slug") or row.get("source") or "")
    source_rows = [
        live_row for live_row in db_rows
        if benchmark.normalize_slug(live_row.get("source_slug") or live_row.get("source") or "") == source_slug
    ]
    for identifier in ("source_hash", "external_id", "url", "canonical_opportunity_id"):
        value = row_identifier_value(row, identifier)
        if not value:
            continue
        matches = benchmark.identifier_matches(identifier, value, db_rows, source_rows)
        if len(matches) == 1:
            return {"status": "yes", "identifier": identifier, "count": 1}
        if len(matches) > 1:
            return {"status": "ambiguous", "identifier": identifier, "count": len(matches)}
    return {"status": "no", "identifier": "", "count": 0}


def row_identifier_value(row: dict, identifier: str) -> str:
    if identifier == "url":
        return benchmark.normalize_url(row.get("url"))
    return stringify(row.get(identifier)).strip()


def candidate_summary_for_diagnostics(candidates: list[dict]) -> str:
    pieces = []
    for candidate in candidates:
        identifiers = []
        for key in ("job_id", "external_id", "source_hash", "canonical_opportunity_id"):
            value = stringify(candidate.get(key))
            if value:
                identifiers.append(f"{key}={value}")
        url = stringify(candidate.get("url"))
        if url:
            identifiers.append(f"url={url}")
        location = stringify(candidate.get("location")) or "Unknown location"
        title = stringify(candidate.get("title")) or "Untitled"
        pieces.append(f"{title} [{location}] ({'; '.join(identifiers) or 'no stable id shown'})")
    return " | ".join(pieces)


def summarize_rows(rows: list[dict], evaluated: list[benchmark.EvaluatedCase]) -> dict:
    missing = Counter()
    for row in rows:
        missing.update(split_list(row["missing_fields"]))
    baseline = benchmark.human_reviewed_agreement(evaluated)
    return {
        "human_reviewed_cases": sum(1 for item in evaluated if item.case.get("label_source") == "human_reviewed"),
        "included_cases": len(rows),
        "by_fidelity": Counter(row["fidelity_classification"] for row in rows),
        "by_primary_issue": Counter(row["primary_issue"] for row in rows),
        "missing_fields": missing,
        "exact_stable_live_match": Counter(row["exact_stable_live_match_available"] for row in rows),
        "unsafe_title_candidates": sum(1 for row in rows if row["unsafe_title_or_similarity_candidates"] == "yes"),
        "requires_human_approval": sum(1 for row in rows if row["matching_changes_blocked_until_review"] == "yes"),
        "blocked_until_metadata_review": sum(1 for row in rows if row["matching_changes_blocked_until_review"] == "yes"),
        "baseline": baseline,
    }


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_html(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["issue_group"]].append(row)
    parts = [
        "<!doctype html>",
        "<html><head><meta charset='utf-8'>",
        "<title>Matching Metadata Enrichment Review</title>",
        "<style>",
        "body{font-family:Arial,sans-serif;max-width:1180px;margin:32px auto;padding:0 18px;line-height:1.45;color:#1f2937}",
        "h1,h2{color:#111827}.case{border:1px solid #d1d5db;border-radius:8px;padding:16px;margin:14px 0;background:#fff}",
        ".grid{display:grid;grid-template-columns:220px 1fr;gap:6px 14px}.label{font-weight:700;color:#374151}",
        ".badges{margin:8px 0}.badge{display:inline-block;border:1px solid #cbd5e1;border-radius:999px;padding:2px 8px;margin:2px;background:#f8fafc;font-size:12px}",
        ".warning{background:#fff7ed;border-left:4px solid #f97316;padding:10px;margin:10px 0}.small{color:#6b7280;font-size:13px}",
        "pre{white-space:pre-wrap;background:#f9fafb;border:1px solid #e5e7eb;padding:10px;border-radius:6px}",
        "</style></head><body>",
        "<h1>Matching Metadata Enrichment Review</h1>",
        "<p>This review batch is for human metadata calibration only. It does not apply fixture, matcher, database, or product changes.</p>",
        f"<p class='small'>Allowed review decisions: {escape(', '.join(REVIEW_DECISIONS))}</p>",
    ]
    for group in sorted(grouped):
        parts.append(f"<h2>{escape(group)}</h2>")
        for row in grouped[group]:
            parts.append(render_html_case(row))
    parts.append("</body></html>")
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def render_html_case(row: dict) -> str:
    candidates = row["candidate_summary_for_diagnostics"] or "No title/source diagnostic candidates."
    fields = [
        ("Case", row["case_id"]),
        ("Profile", f"{row['profile_name']} ({row['profile_id']})"),
        ("Opportunity", f"{row['source']}: {row['title']}"),
        ("Human decision", f"{row['human_label']} / {row['human_expected_section']}"),
        ("Current matcher", f"{row['current_benchmark_label']} / {row['current_effective_section']} / score {row['score']}"),
        ("Snapshot source", row["snapshot_source_type"]),
        ("Primary issue", row["primary_issue"]),
        ("Missing fields", row["missing_fields"] or "-"),
        ("Suspicious fields", row["suspicious_fields"] or "-"),
        ("Internal inconsistencies", row["internal_inconsistencies"] or "-"),
        ("Exact stable live match", row["exact_stable_live_match_available"]),
        ("Reviewer should decide", row["reviewer_should_decide"]),
    ]
    metadata = [
        ("inventory_model", row["inventory_model"]),
        ("opportunity_kind", row["opportunity_kind"]),
        ("availability_basis", row["availability_basis"]),
        ("source_category", row["source_category"]),
        ("expertise", row["expertise"]),
        ("department", row["department"]),
        ("location", row["location"]),
        ("language", row["language"]),
        ("required_languages", row["required_languages"]),
    ]
    html_fields = "".join(
        f"<div class='label'>{escape(label)}</div><div>{escape(value)}</div>"
        for label, value in fields
    )
    html_metadata = "".join(
        f"<div class='label'>{escape(label)}</div><div>{escape(value)}</div>"
        for label, value in metadata
    )
    return "\n".join(
        [
            "<section class='case'>",
            f"<h3>{escape(row['case_id'])}: {escape(row['title'])}</h3>",
            "<div class='badges'>",
            f"<span class='badge'>{escape(row['fidelity_classification'])}</span>",
            f"<span class='badge'>{escape(row['primary_issue'])}</span>",
            "</div>",
            f"<div class='warning'><strong>Why review:</strong> {escape(row['why_review'])}</div>",
            "<div class='grid'>",
            html_fields,
            "</div>",
            "<h4>Current snapshot metadata</h4>",
            "<div class='grid'>",
            html_metadata,
            "</div>",
            "<h4>Unsafe live/title candidates (diagnostic only)</h4>",
            f"<pre>{escape(candidates)}</pre>",
            "<h4>Human notes</h4>",
            f"<pre>{escape(row['human_notes'])}</pre>",
            "</section>",
        ]
    )


def write_summary(rows: list[dict], summary: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    focused = [row for row in rows if row["case_id"] in FOCUSED_CASE_ORDER]
    lines = [
        "# Matching Metadata Enrichment Review Summary",
        "",
        "This is a review-only artifact. No fixture snapshots, matcher behavior, database rows, labels, sections, or product UI were changed.",
        "",
        "## Counts",
        "",
        f"- Total human-reviewed cases inspected: **{summary['human_reviewed_cases']}**",
        f"- Cases included in enrichment review batch: **{summary['included_cases']}**",
        f"- Counts by fidelity classification: {format_counter(summary['by_fidelity'])}",
        f"- Counts by primary issue: {format_counter(summary['by_primary_issue'])}",
        f"- Missing field counts: {format_counter(summary['missing_fields'])}",
        f"- Exact stable live row availability: {format_counter(summary['exact_stable_live_match'])}",
        f"- Cases with unsafe title/similar candidates only: {summary['unsafe_title_candidates']}",
        f"- Cases requiring human approval before enrichment: {summary['requires_human_approval']}",
        f"- Cases where matching changes should be blocked until metadata review: {summary['blocked_until_metadata_review']}",
        "",
        "## Baseline Check",
        "",
        (
            f"- Human-reviewed baseline remains: labels "
            f"{summary['baseline']['label_agreement']}/{summary['baseline']['total']}, "
            f"sections {summary['baseline']['section_agreement']}/{summary['baseline']['total']}, "
            f"full {summary['baseline']['full_agreement']}/{summary['baseline']['total']}"
        ),
        "",
        "## Focused Case Classifications",
        "",
    ]
    for row in focused:
        lines.append(
            f"- `{row['case_id']}`: `{row['fidelity_classification']}` / "
            f"`{row['primary_issue']}` - {row['why_review']}"
        )
    lines.extend(
        [
            "",
            "## Reviewer Notes",
            "",
            "- Title/source live candidates are diagnostics only and were not applied as fixture metadata.",
            "- Proposed metadata fields are intentionally blank for human editing.",
            "- Use review decisions: `" + "`, `".join(REVIEW_DECISIONS) + "`.",
            "- Do not tune matching behavior from these cases until sparse or ambiguous fixture metadata has been reviewed.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def metadata_value(row: dict, field: str) -> str:
    if field == "employment_type":
        return stringify(row.get("employment_type"))
    if field == "profile_or_opportunity_kind":
        return stringify(row.get("profile_kind") or row.get("opportunity_kind"))
    value = row.get(field)
    if isinstance(value, (list, tuple)):
        return "; ".join(stringify(item) for item in value)
    return stringify(value)


def missing_value(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == "" or value.strip().lower() == "unknown"
    if isinstance(value, (list, tuple)):
        return len(value) == 0
    return False


def stringify(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(stringify(item) for item in value)
    return str(value)


def join_list(values: list[str]) -> str:
    return "; ".join(value for value in values if value)


def split_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(";") if part.strip()]


def compact(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def escape(value) -> str:
    return html.escape(str(value or ""), quote=True)


def format_counter(counter: Counter) -> str:
    if not counter:
        return "none"
    return ", ".join(f"{key}={counter[key]}" for key in sorted(counter))


def relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


if __name__ == "__main__":
    main()
