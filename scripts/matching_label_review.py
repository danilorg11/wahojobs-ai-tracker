#!/usr/bin/env python3
"""Generate and apply human review packets for matching golden-set labels."""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
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
CSV_COLUMNS = [
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
    "human_label",
    "human_expected_section",
    "human_review_required",
    "human_notes",
    "approved_by",
    "approved_at",
]


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
    elif args.command == "apply":
        apply_reviews(args)
    else:
        raise SystemExit("Choose a command: generate or apply")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate/apply human review packets for matching golden-set labels."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate", help="Generate review HTML/CSV/summary.")
    generate_parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    generate_parser.add_argument("--profile")
    generate_parser.add_argument("--include-all", action="store_true")

    apply_parser = subparsers.add_parser("apply", help="Apply completed review CSV decisions.")
    apply_parser.add_argument("--input", type=Path, default=CSV_PATH)
    apply_parser.add_argument("--dry-run", action="store_true", default=True)
    apply_parser.add_argument("--yes", action="store_true")

    return parser.parse_args()


def generate(args) -> None:
    fixture, profiles, evaluated = load_evaluated_cases()
    selected = select_review_cases(evaluated, profiles, args.limit, args.profile, args.include_all)

    HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    HTML_PATH.write_text(render_html(selected), encoding="utf-8")
    write_csv(selected, CSV_PATH)
    SUMMARY_PATH.write_text(render_summary(selected, args), encoding="utf-8")

    print("Matching Label Review Packet")
    print("============================")
    print(f"Cases selected: {len(selected)}")
    print(f"Profiles: {format_counter(Counter(case.item.case['profile_id'] for case in selected))}")
    print(f"Review reasons: {format_counter(Counter(reason for case in selected for reason in case.reasons))}")
    print(f"Wrote HTML: {HTML_PATH}")
    print(f"Wrote CSV: {CSV_PATH}")
    print(f"Wrote summary: {SUMMARY_PATH}")


def load_evaluated_cases():
    fixture = benchmark.load_fixture()
    profiles = benchmark.load_benchmark_profiles(fixture)
    with get_connection() as conn:
        rows = [benchmark.row_to_dict(row) for row in benchmark.matcher.get_active_rows(conn)]
    evaluated = [
        benchmark.evaluate_case(case, profiles[case["profile_id"]], rows, benchmark.matcher)
        for case in fixture["cases"]
    ]
    return fixture, profiles, evaluated


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
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for review_case in review_cases:
            writer.writerow(csv_row(review_case))


def csv_row(review_case):
    item = review_case.item
    case = item.case
    return {
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


def apply_reviews(args) -> None:
    fixture = benchmark.load_fixture()
    case_index = {}
    for case in fixture["cases"]:
        if case["case_id"] in case_index:
            raise SystemExit(f"Duplicate case_id in fixture: {case['case_id']}")
        case_index[case["case_id"]] = case

    rows = read_review_csv(args.input)
    changes = collect_changes(rows, case_index)
    print("Matching Label Review Apply")
    print("===========================")
    print(f"Input: {args.input}")
    print(f"Rows with human decisions: {len(changes)}")
    if not changes:
        print("No human decisions found. Nothing to apply.")
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
    missing = [column for column in CSV_COLUMNS if column not in (reader.fieldnames or [])]
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
        changes.append({"case": case_index[case_id], "row": row, "updates": updates})
    return changes


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

