#!/usr/bin/env python3
"""Generate human-review artifacts for opportunity metadata gap candidates.

This creates review files only. It does not apply inferred metadata to jobs,
canonical opportunities, fixtures, or any database rows.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from html import escape as html_escape
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import opportunity_metadata_gap_report as gap_report  # noqa: E402
from wahojobs.db.connection import get_connection  # noqa: E402


DEFAULT_OUTPUT_PREFIX = ROOT / "exports" / "opportunity_metadata_gap_review"
DEFAULT_MAX_ROWS = 100
MIN_REVIEW_ROWS = 80
MAX_REVIEW_ROWS = 120
PER_SOURCE_CAP = 18
PER_PATTERN_CAP = 16
PER_SOURCE_PATTERN_CAP = 6

REVIEW_DECISIONS = {
    "pending_review",
    "approve_inferred_metadata",
    "reject_inference",
    "needs_more_research",
    "keep_diagnostic_only",
    "not_applicable",
}

CSV_COLUMNS = [
    "review_id",
    "candidate_type",
    "source",
    "company",
    "job_id",
    "canonical_opportunity_id",
    "external_id",
    "source_hash",
    "title",
    "url",
    "location",
    "department",
    "expertise",
    "current_language",
    "current_language_locale",
    "current_required_languages",
    "candidate_required_languages",
    "candidate_language_locale",
    "candidate_location_restriction",
    "candidate_pattern",
    "candidate_confidence",
    "evidence_text",
    "risk_notes",
    "suggested_review_action",
    "review_decision",
    "human_required_languages",
    "human_language_locale",
    "human_location_restriction",
    "human_notes",
    "apply_eligible",
]

SOURCE_PRIORITY = {
    "oneforma": 0,
    "mercor": 1,
    "welocalize": 2,
    "rws": 3,
    "appen": 4,
    "outlier": 5,
    "meridial": 6,
    "alignerr": 7,
}
TYPE_PRIORITY = {
    "language_locale": 0,
    "language_requirement": 1,
    "location_restriction": 2,
    "ambiguous_metadata": 3,
}
CONFIDENCE_PRIORITY = {"high": 0, "medium": 1, "low": 2, "ambiguous": 3}


@dataclass(frozen=True)
class ReviewCandidate:
    data: dict

    @property
    def source(self) -> str:
        return self.data["source"]

    @property
    def pattern(self) -> str:
        return self.data["candidate_pattern"]

    @property
    def candidate_type(self) -> str:
        return self.data["candidate_type"]

    @property
    def confidence(self) -> str:
        return self.data["candidate_confidence"]


def main() -> int:
    args = parse_args()
    with get_connection() as conn:
        rows = load_review_rows(conn)
    candidates = select_review_batch(build_candidates(rows), max_rows=args.max_rows)
    write_artifacts(candidates, args.output_prefix)
    print_summary(candidates, args.output_prefix)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-prefix",
        type=Path,
        default=DEFAULT_OUTPUT_PREFIX,
        help="Output path prefix without extension.",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=DEFAULT_MAX_ROWS,
        help="Maximum curated review rows to write.",
    )
    return parser.parse_args()


def load_review_rows(conn) -> list[dict]:
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
          j.canonical_opportunity_id,
          c.name AS company,
          c.slug AS source,
          co.language AS current_language,
          co.language_locale AS current_language_locale,
          co.canonical_title,
          co.source_category
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        LEFT JOIN canonical_opportunities co ON co.id = j.canonical_opportunity_id
        WHERE j.is_active = 1
          AND j.title NOT LIKE '[SIMULATION]%'
        ORDER BY c.slug ASC, j.title ASC, j.id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def build_candidates(rows: list[dict]) -> list[ReviewCandidate]:
    candidates = []
    seen = set()
    for row in rows:
        hits = gap_report.classify_title_patterns(row.get("title") or "")
        for hit in hits:
            candidate = candidate_from_hit(row, hit)
            if candidate is None:
                continue
            key = candidate_key(candidate.data)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
    return sorted(candidates, key=candidate_sort_key)


def candidate_from_hit(row: dict, hit) -> ReviewCandidate | None:
    if hit.bucket == "language":
        if clean(row.get("current_language")):
            return None
        return language_candidate(row, hit)
    if hit.bucket == "locale":
        if clean(row.get("current_language_locale")):
            return None
        return locale_candidate(row, hit)
    if hit.bucket == "location":
        if not gap_report.location_field_may_not_express_restriction(row.get("location") or ""):
            return None
        return location_candidate(row, hit)
    if hit.bucket == "ambiguous":
        return ambiguous_candidate(row, hit)
    return None


def language_candidate(row: dict, hit) -> ReviewCandidate:
    languages = candidate_languages(row.get("title") or "", hit)
    confidence = "high" if hit.pattern in {"fluent_in", "language_specialist_or_expert", "audio_language_role", "bilingual_audio_role"} else "medium"
    risk = "Title-derived language candidate; verify this is an applicant requirement before applying."
    if "," in languages or hit.pattern == "translation_or_translator":
        risk = "Translation or multi-language title; verify whether all listed languages are required."
    return make_candidate(
        row,
        candidate_type="language_requirement",
        hit=hit,
        candidate_required_languages=languages,
        confidence=confidence,
        risk_notes=risk,
        suggested_review_action="Confirm required language(s) or mark diagnostic only.",
        apply_eligible=True,
    )


def locale_candidate(row: dict, hit) -> ReviewCandidate:
    language, locale = parse_locale_candidate(row.get("title") or "")
    return make_candidate(
        row,
        candidate_type="language_locale",
        hit=hit,
        candidate_required_languages=language,
        candidate_language_locale=locale,
        confidence="high" if language and locale else "medium",
        risk_notes="Locale is title-derived; verify it is language locale rather than applicant country.",
        suggested_review_action="Confirm language locale or keep diagnostic only.",
        apply_eligible=bool(language and locale),
    )


def location_candidate(row: dict, hit) -> ReviewCandidate:
    location = parse_location_restriction(row.get("title") or "")
    return make_candidate(
        row,
        candidate_type="location_restriction",
        hit=hit,
        candidate_location_restriction=location,
        confidence="high" if location else "medium",
        risk_notes="Location restriction is title-derived; verify applicant eligibility wording.",
        suggested_review_action="Confirm applicant location restriction or reject inference.",
        apply_eligible=bool(location),
    )


def ambiguous_candidate(row: dict, hit) -> ReviewCandidate:
    return make_candidate(
        row,
        candidate_type="ambiguous_metadata",
        hit=hit,
        confidence="ambiguous",
        risk_notes="Ambiguous title pattern; do not apply without human interpretation.",
        suggested_review_action="Choose keep_diagnostic_only, needs_more_research, or provide corrected metadata.",
        apply_eligible=False,
    )


def make_candidate(
    row: dict,
    candidate_type: str,
    hit,
    candidate_required_languages: str = "",
    candidate_language_locale: str = "",
    candidate_location_restriction: str = "",
    confidence: str = "medium",
    risk_notes: str = "",
    suggested_review_action: str = "",
    apply_eligible: bool = False,
) -> ReviewCandidate:
    data = {
        "review_id": review_id(row, candidate_type, hit.pattern),
        "candidate_type": candidate_type,
        "source": clean(row.get("source")),
        "company": clean(row.get("company")),
        "job_id": clean(row.get("job_id")),
        "canonical_opportunity_id": clean(row.get("canonical_opportunity_id")),
        "external_id": clean(row.get("external_id")),
        "source_hash": clean(row.get("source_hash")),
        "title": clean(row.get("title")),
        "url": clean(row.get("url")),
        "location": clean(row.get("location")),
        "department": clean(row.get("department")),
        "expertise": clean(row.get("expertise")),
        "current_language": clean(row.get("current_language")),
        "current_language_locale": clean(row.get("current_language_locale")),
        "current_required_languages": "",
        "candidate_required_languages": candidate_required_languages,
        "candidate_language_locale": candidate_language_locale,
        "candidate_location_restriction": candidate_location_restriction,
        "candidate_pattern": hit.pattern,
        "candidate_confidence": confidence,
        "evidence_text": evidence_text(row, hit),
        "risk_notes": risk_notes,
        "suggested_review_action": suggested_review_action,
        "review_decision": "pending_review",
        "human_required_languages": "",
        "human_language_locale": "",
        "human_location_restriction": "",
        "human_notes": "",
        "apply_eligible": "yes" if apply_eligible and confidence != "ambiguous" else "no",
    }
    return ReviewCandidate(data)


def select_review_batch(candidates: list[ReviewCandidate], max_rows: int = DEFAULT_MAX_ROWS) -> list[ReviewCandidate]:
    max_rows = min(max(max_rows, 1), MAX_REVIEW_ROWS)
    selected = []
    source_counts = Counter()
    pattern_counts = Counter()
    source_pattern_counts = Counter()

    for candidate in candidates:
        if source_counts[candidate.source] >= PER_SOURCE_CAP:
            continue
        if pattern_counts[candidate.pattern] >= PER_PATTERN_CAP:
            continue
        source_pattern_key = (candidate.source, candidate.pattern)
        if source_pattern_counts[source_pattern_key] >= PER_SOURCE_PATTERN_CAP:
            continue
        selected.append(candidate)
        source_counts[candidate.source] += 1
        pattern_counts[candidate.pattern] += 1
        source_pattern_counts[source_pattern_key] += 1
        if len(selected) >= max_rows:
            break

    if len(selected) < min(MIN_REVIEW_ROWS, max_rows):
        selected_keys = {candidate_key(candidate.data) for candidate in selected}
        for candidate in candidates:
            if candidate_key(candidate.data) in selected_keys:
                continue
            if source_counts[candidate.source] >= PER_SOURCE_CAP + 6:
                continue
            if pattern_counts[candidate.pattern] >= PER_PATTERN_CAP + 10:
                continue
            selected.append(candidate)
            selected_keys.add(candidate_key(candidate.data))
            source_counts[candidate.source] += 1
            pattern_counts[candidate.pattern] += 1
            if len(selected) >= max_rows:
                break

    return sorted(selected, key=candidate_sort_key)


def write_artifacts(candidates: list[ReviewCandidate], output_prefix: Path) -> None:
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    write_csv(candidates, output_prefix.with_suffix(".csv"))
    output_prefix.with_suffix(".html").write_text(render_html(candidates), encoding="utf-8")
    summary_path(output_prefix).write_text(render_summary(candidates), encoding="utf-8")


def write_csv(candidates: list[ReviewCandidate], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(candidate.data)


def render_html(candidates: list[ReviewCandidate]) -> str:
    grouped = defaultdict(list)
    for candidate in candidates:
        key = (
            candidate.source,
            candidate.pattern,
            candidate.confidence,
            candidate.candidate_type,
        )
        grouped[key].append(candidate)

    sections = []
    for (source, pattern, confidence, candidate_type), group in sorted(grouped.items()):
        cards = "\n".join(render_html_card(candidate) for candidate in group)
        sections.append(
            "<section>"
            f"<h2>{html_escape(source)} / {html_escape(pattern)} / "
            f"{html_escape(confidence)} / {html_escape(candidate_type)}</h2>"
            f"{cards}</section>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Opportunity Metadata Gap Review</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 32px; color: #1f2328; line-height: 1.45; }}
    .notice {{ border: 1px solid #d8dee4; background: #f6f8fa; padding: 12px; border-radius: 6px; }}
    .card {{ border: 1px solid #d8dee4; border-radius: 6px; padding: 12px; margin: 12px 0; }}
    .meta {{ color: #57606a; font-size: 0.92rem; }}
    code {{ background: #f6f8fa; padding: 1px 4px; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>Opportunity Metadata Gap Review</h1>
  <p class="notice">Review artifact only. Nothing has been applied to jobs, canonical opportunities, fixtures, or the database.</p>
  <p>Total candidates: {len(candidates)}</p>
  {''.join(sections)}
</body>
</html>
"""


def render_html_card(candidate: ReviewCandidate) -> str:
    row = candidate.data
    url = row["url"]
    link = f'<a href="{html_escape(url)}">Open</a>' if url else "-"
    return f"""
<article class="card">
  <h3>{html_escape(row['title'])}</h3>
  <p class="meta">Review ID: <code>{html_escape(row['review_id'])}</code> | {html_escape(row['company'])} | {link}</p>
  <p><strong>Current metadata:</strong> language={html_escape(row['current_language'] or '-')} |
     locale={html_escape(row['current_language_locale'] or '-')} |
     required={html_escape(row['current_required_languages'] or '-')}</p>
  <p><strong>Candidate metadata:</strong> required={html_escape(row['candidate_required_languages'] or '-')} |
     locale={html_escape(row['candidate_language_locale'] or '-')} |
     location={html_escape(row['candidate_location_restriction'] or '-')}</p>
  <p><strong>Evidence:</strong> {html_escape(row['evidence_text'])}</p>
  <p><strong>Risk notes:</strong> {html_escape(row['risk_notes'])}</p>
  <p><strong>Why selected:</strong> {html_escape(row['candidate_pattern'])}; confidence={html_escape(row['candidate_confidence'])}; apply eligible={html_escape(row['apply_eligible'])}</p>
</article>
"""


def render_summary(candidates: list[ReviewCandidate]) -> str:
    by_source = Counter(candidate.source for candidate in candidates)
    by_type = Counter(candidate.candidate_type for candidate in candidates)
    by_confidence = Counter(candidate.confidence for candidate in candidates)
    high = [candidate for candidate in candidates if candidate.confidence == "high"][:10]
    ambiguous = [candidate for candidate in candidates if candidate.confidence == "ambiguous"][:10]
    lines = [
        "# Opportunity Metadata Gap Review Summary",
        "",
        "Review artifact only. Nothing has been applied to jobs, canonical opportunities, fixtures, or the database.",
        "",
        f"- Total candidates: **{len(candidates)}**",
        "",
        "## Counts by Source",
        "",
    ]
    lines.extend(format_counter(by_source))
    lines.extend(["", "## Counts by Candidate Type", ""])
    lines.extend(format_counter(by_type))
    lines.extend(["", "## Counts by Confidence", ""])
    lines.extend(format_counter(by_confidence))
    lines.extend(["", "## High-Confidence Examples", ""])
    lines.extend(format_candidate_examples(high))
    lines.extend(["", "## Ambiguous Examples", ""])
    lines.extend(format_candidate_examples(ambiguous))
    lines.extend(
        [
            "",
            "## Human Review Instructions",
            "",
            "- Edit `review_decision` in the CSV; default is `pending_review`.",
            "- High-confidence candidates still require human approval before any future apply workflow.",
            "- Ambiguous candidates are `apply_eligible=no` by default and should usually be kept diagnostic-only or sent for more research.",
            "- This workflow does not apply metadata. A future guarded apply mode would need separate review.",
        ]
    )
    return "\n".join(lines)


def format_counter(counter: Counter) -> list[str]:
    if not counter:
        return ["- None"]
    return [f"- {key}: {value}" for key, value in sorted(counter.items(), key=lambda item: (-item[1], item[0]))]


def format_candidate_examples(candidates: list[ReviewCandidate]) -> list[str]:
    if not candidates:
        return ["- None"]
    return [
        f"- `{candidate.data['review_id']}`: {candidate.data['title']} "
        f"({candidate.source}, {candidate.pattern})"
        for candidate in candidates
    ]


def print_summary(candidates: list[ReviewCandidate], output_prefix: Path) -> None:
    by_source = Counter(candidate.source for candidate in candidates)
    by_type = Counter(candidate.candidate_type for candidate in candidates)
    by_confidence = Counter(candidate.confidence for candidate in candidates)
    print("Opportunity Metadata Gap Review")
    print("================================")
    print("Review artifact only; no metadata was applied.")
    print(f"Candidates: {len(candidates)}")
    print(f"CSV: {output_prefix.with_suffix('.csv')}")
    print(f"HTML: {output_prefix.with_suffix('.html')}")
    print(f"Summary: {summary_path(output_prefix)}")
    print("By source: " + compact_counter(by_source))
    print("By type: " + compact_counter(by_type))
    print("By confidence: " + compact_counter(by_confidence))


def compact_counter(counter: Counter) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(counter.items())) or "-"


def candidate_languages(title: str, hit) -> str:
    if hit.value:
        values = [part.strip() for part in re.split(r",|/", hit.value) if part.strip()]
    else:
        values = gap_report.recognized_language_terms(gap_report.normalize_text(title))
    normalized = []
    for value in values:
        language = gap_report.normalize_language_name(value)
        if language and language not in normalized:
            normalized.append(language)
    return ", ".join(normalized)


def parse_locale_candidate(title: str) -> tuple[str, str]:
    normalized = gap_report.normalize_text(title)
    parenthetical = re.search(r"\b(english|spanish|portuguese|french|chinese|mandarin)\s*\(([^)]+)\)", normalized)
    if parenthetical:
        language = gap_report.normalize_language_name(parenthetical.group(1))
        locale = normalize_locale(parenthetical.group(2))
        return title_case_language(language), f"{title_case_language(language)} {locale}".strip()

    dash = re.search(
        r"\b(english|spanish|portuguese|french|chinese|mandarin)\s*-\s*"
        r"(latin america|latam|spain|brazil|portugal|mexico|us|usa|united states|canada|france|germany)",
        normalized,
    )
    if dash:
        language = gap_report.normalize_language_name(dash.group(1))
        locale = normalize_locale(dash.group(2))
        return title_case_language(language), f"{title_case_language(language)} {locale}".strip()
    return "", ""


def parse_location_restriction(title: str) -> str:
    normalized = gap_report.normalize_text(title)
    if re.search(r"\bus only\b", normalized):
        return "United States"
    if re.search(r"\buk[- ]based\b", normalized):
        return "United Kingdom"
    remote = re.search(
        r"\bremote\s*-\s*(united states|usa|canada|brazil|mexico|spain|france|germany|india|taiwan)\b",
        normalized,
    )
    if remote:
        return normalize_locale(remote.group(1))
    return ""


def normalize_locale(value: str) -> str:
    replacements = {
        "latam": "Latin America",
        "us": "United States",
        "usa": "United States",
    }
    value = gap_report.normalize_text(value)
    return replacements.get(value, value.title())


def title_case_language(value: str) -> str:
    return " ".join(part.capitalize() for part in gap_report.normalize_text(value).split())


def evidence_text(row: dict, hit) -> str:
    title = clean(row.get("title"))
    location = clean(row.get("location"))
    value = f"; value={hit.value}" if hit.value else ""
    return f"title={title}; location={location}; pattern={hit.pattern}{value}"


def review_id(row: dict, candidate_type: str, pattern: str) -> str:
    source = slug(clean(row.get("source")) or "source")
    identifier = clean(row.get("job_id")) or clean(row.get("external_id")) or clean(row.get("canonical_opportunity_id")) or "row"
    return f"omgr_{source}_{slug(candidate_type)}_{slug(pattern)}_{slug(identifier)}"


def candidate_key(data: dict) -> tuple:
    return (
        data["source"],
        data["job_id"],
        data["canonical_opportunity_id"],
        data["candidate_type"],
        data["candidate_pattern"],
    )


def candidate_sort_key(candidate: ReviewCandidate) -> tuple:
    row = candidate.data
    return (
        SOURCE_PRIORITY.get(candidate.source, 99),
        TYPE_PRIORITY.get(candidate.candidate_type, 99),
        CONFIDENCE_PRIORITY.get(candidate.confidence, 99),
        candidate.pattern,
        row["title"].lower(),
        row["job_id"],
    )


def slug(value: str) -> str:
    value = gap_report.normalize_text(value)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "unknown"


def clean(value) -> str:
    return str(value or "").strip()


def summary_path(output_prefix: Path) -> Path:
    return output_prefix.parent / f"{output_prefix.name}_summary.md"


if __name__ == "__main__":
    raise SystemExit(main())
