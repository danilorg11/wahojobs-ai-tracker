#!/usr/bin/env python3
"""Report language, locale, and location metadata gaps in active opportunities.

This is diagnostic only. It does not infer or write opportunity metadata.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wahojobs.db.connection import get_connection  # noqa: E402
from wahojobs.matching.languages import normalize_language_name  # noqa: E402


MAX_EXAMPLES_PER_BUCKET = 5

HIGH_CONFIDENCE_LANGUAGE_PATTERNS = (
    "fluent_in",
    "language_specialist_or_expert",
    "audio_language_role",
    "bilingual_audio_role",
    "translation_or_translator",
)
HIGH_CONFIDENCE_LOCALE_PATTERNS = (
    "language_parenthetical_locale",
    "language_dash_locale",
)
HIGH_CONFIDENCE_LOCATION_PATTERNS = (
    "us_only",
    "uk_based",
    "remote_country_restricted",
)
AMBIGUOUS_PATTERNS = (
    "ampersand_language_list",
    "trilingual_or_multilingual",
    "unmapped_dialect_or_language_token",
    "country_only_title_text",
)

KNOWN_LANGUAGE_TERMS = {
    "arabic",
    "chinese",
    "english",
    "french",
    "german",
    "hindi",
    "japanese",
    "korean",
    "mandarin",
    "portuguese",
    "russian",
    "spanish",
}
UNMAPPED_LANGUAGE_OR_DIALECT_TERMS = {
    "american sign language",
    "asl",
    "asturian",
    "aymara",
    "basque",
}
COUNTRY_TERMS = (
    "argentina",
    "australia",
    "brazil",
    "canada",
    "france",
    "germany",
    "india",
    "ireland",
    "japan",
    "mexico",
    "morocco",
    "spain",
    "taiwan",
    "united kingdom",
    "united states",
    "usa",
)


@dataclass(frozen=True)
class PatternHit:
    bucket: str
    pattern: str
    value: str = ""


def main() -> int:
    args = parse_args()
    with get_connection() as conn:
        rows = load_active_rows(conn)
    report = analyze_rows(rows)
    rendered = render_report(report)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
        print(f"Wrote metadata gap report to {args.out}")
    else:
        print(rendered)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="Optional markdown output path.")
    return parser.parse_args()


def load_active_rows(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
          j.id AS job_id,
          j.title,
          j.location,
          j.department,
          j.expertise,
          j.commitment,
          j.canonical_opportunity_id,
          c.name AS source,
          c.slug AS source_slug,
          c.inventory_model,
          c.market_count_policy,
          co.canonical_title,
          co.source_category,
          co.language,
          co.language_locale
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        LEFT JOIN canonical_opportunities co ON co.id = j.canonical_opportunity_id
        WHERE j.is_active = 1
          AND j.title NOT LIKE '[SIMULATION]%'
        ORDER BY c.name ASC, j.title ASC, j.id ASC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def analyze_rows(rows: list[dict]) -> dict:
    by_source = defaultdict(Counter)
    examples = defaultdict(lambda: defaultdict(list))
    totals = Counter()

    for row in rows:
        source = row.get("source_slug") or row.get("source") or "unknown"
        title = row.get("title") or ""
        location = row.get("location") or ""
        canonical_language = clean_value(row.get("language"))
        canonical_locale = clean_value(row.get("language_locale"))

        totals["rows_inspected"] += 1
        by_source[source]["rows_inspected"] += 1
        if canonical_language:
            totals["canonical_language"] += 1
            by_source[source]["canonical_language"] += 1
        if canonical_locale:
            totals["canonical_language_locale"] += 1
            by_source[source]["canonical_language_locale"] += 1

        hits = classify_title_patterns(title)
        language_hits = [hit for hit in hits if hit.bucket == "language"]
        locale_hits = [hit for hit in hits if hit.bucket == "locale"]
        location_hits = [hit for hit in hits if hit.bucket == "location"]
        ambiguous_hits = [hit for hit in hits if hit.bucket == "ambiguous"]

        if language_hits:
            increment_gap(
                totals,
                by_source[source],
                examples[source],
                "title_language_pattern",
                row,
                language_hits,
            )
            if not canonical_language:
                increment_gap(
                    totals,
                    by_source[source],
                    examples[source],
                    "title_language_pattern_missing_canonical_language",
                    row,
                    language_hits,
                )

        if locale_hits:
            increment_gap(
                totals,
                by_source[source],
                examples[source],
                "title_locale_pattern",
                row,
                locale_hits,
            )
            if not canonical_locale:
                increment_gap(
                    totals,
                    by_source[source],
                    examples[source],
                    "title_locale_pattern_missing_canonical_locale",
                    row,
                    locale_hits,
                )

        if location_hits:
            increment_gap(
                totals,
                by_source[source],
                examples[source],
                "title_location_restriction_pattern",
                row,
                location_hits,
            )
            if location_field_may_not_express_restriction(location):
                increment_gap(
                    totals,
                    by_source[source],
                    examples[source],
                    "title_location_restriction_needs_review",
                    row,
                    location_hits,
                )

        if ambiguous_hits:
            increment_gap(
                totals,
                by_source[source],
                examples[source],
                "ambiguous_or_risky_pattern",
                row,
                ambiguous_hits,
            )

    return {
        "totals": totals,
        "by_source": by_source,
        "examples": examples,
    }


def increment_gap(totals, source_counts, source_examples, key, row, hits):
    totals[key] += 1
    source_counts[key] += 1
    if len(source_examples[key]) < MAX_EXAMPLES_PER_BUCKET:
        source_examples[key].append(format_example(row, hits))


def classify_title_patterns(title: str) -> list[PatternHit]:
    normalized = normalize_text(title)
    hits: list[PatternHit] = []

    fluent = re.search(r"\bfluent in ([a-z][a-z ]+(?:\s*-\s*[a-z ]+)?)", normalized)
    if fluent:
        hits.append(PatternHit("language", "fluent_in", fluent.group(1).strip()))

    role_language = leading_language_for_role(
        normalized,
        role_pattern=r"\blanguage (?:specialist|expert)\b",
    )
    if role_language:
        hits.append(PatternHit("language", "language_specialist_or_expert", role_language))

    audio_language = leading_language_for_role(
        normalized,
        role_pattern=r"\baudio (?:specialist|generalist|evaluator|transcription)\b",
    )
    if audio_language:
        hits.append(PatternHit("language", "audio_language_role", audio_language))

    bilingual_audio = leading_language_for_role(normalized, role_pattern=r"\bbilingual audio\b")
    if bilingual_audio:
        hits.append(PatternHit("language", "bilingual_audio_role", bilingual_audio))

    if re.search(r"\b(translation|translator|mtpe|post editing)\b", normalized):
        language_terms = recognized_language_terms(normalized)
        if language_terms:
            hits.append(PatternHit("language", "translation_or_translator", ", ".join(language_terms)))

    if re.search(r"\b(english|spanish|portuguese|french|chinese|mandarin)\s*\([^)]+\)", normalized):
        hits.append(PatternHit("locale", "language_parenthetical_locale"))

    if re.search(
        r"\b(english|spanish|portuguese|french|chinese|mandarin)\s*-\s*"
        r"(latin america|latam|spain|brazil|mexico|us|usa|united states|canada|france|germany)",
        normalized,
    ):
        hits.append(PatternHit("locale", "language_dash_locale"))

    if re.search(r"\bus only\b", normalized):
        hits.append(PatternHit("location", "us_only"))
    if re.search(r"\buk[- ]based\b", normalized):
        hits.append(PatternHit("location", "uk_based"))
    if re.search(
        r"\bremote\s*-\s*(united states|usa|canada|brazil|mexico|spain|france|germany|india|taiwan)\b",
        normalized,
    ):
        hits.append(PatternHit("location", "remote_country_restricted"))

    if re.search(r"\bgeneralist\s*-\s*[a-z ]+\s+(?:and|&)\s+[a-z ]+\b", normalized):
        hits.append(PatternHit("ambiguous", "ampersand_language_list"))
    if re.search(r"\b(trilingual|multilingual)\b", normalized):
        hits.append(PatternHit("ambiguous", "trilingual_or_multilingual"))
    for term in sorted(UNMAPPED_LANGUAGE_OR_DIALECT_TERMS):
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", normalized):
            hits.append(PatternHit("ambiguous", "unmapped_dialect_or_language_token", term))
            break
    if country_only_pattern(normalized):
        hits.append(PatternHit("ambiguous", "country_only_title_text"))

    return unique_hits(hits)


def leading_language_for_role(normalized_title: str, role_pattern: str) -> str:
    role_match = re.search(role_pattern, normalized_title)
    if not role_match:
        return ""
    prefix = normalized_title[: role_match.start()]
    candidates = recognized_language_terms(prefix)
    return candidates[-1] if candidates else ""


def recognized_language_terms(text: str) -> list[str]:
    terms = []
    for term in sorted(KNOWN_LANGUAGE_TERMS, key=lambda value: (-len(value), value)):
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text):
            canonical = normalize_language_name(term)
            if canonical and canonical not in terms:
                terms.append(canonical)
    return terms


def country_only_pattern(normalized_title: str) -> bool:
    if recognized_language_terms(normalized_title):
        return False
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(country)}(?![a-z0-9])", normalized_title)
        for country in COUNTRY_TERMS
    )


def location_field_may_not_express_restriction(location: str) -> bool:
    normalized = normalize_text(location)
    return normalized in {
        "",
        "any",
        "global",
        "remote",
        "selected locations",
        "unknown",
        "world wide",
        "worldwide",
    }


def unique_hits(hits: list[PatternHit]) -> list[PatternHit]:
    seen = set()
    result = []
    for hit in hits:
        key = (hit.bucket, hit.pattern, hit.value)
        if key in seen:
            continue
        seen.add(key)
        result.append(hit)
    return result


def format_example(row: dict, hits: list[PatternHit]) -> str:
    pattern_text = ", ".join(hit.pattern for hit in hits)
    language = clean_value(row.get("language")) or "-"
    locale = clean_value(row.get("language_locale")) or "-"
    return (
        f"{row.get('title') or 'Untitled'} | source={row.get('source_slug') or '-'} "
        f"| location={row.get('location') or '-'} | canonical_language={language} "
        f"| canonical_locale={locale} | patterns={pattern_text}"
    )


def render_report(report: dict) -> str:
    totals = report["totals"]
    by_source = report["by_source"]
    examples = report["examples"]
    lines = [
        "Opportunity Metadata Gap Report",
        "================================",
        "",
        "Read-only diagnostic. No metadata was inferred or written.",
        "",
        "Totals",
        "------",
    ]
    for key in (
        "rows_inspected",
        "canonical_language",
        "canonical_language_locale",
        "title_language_pattern",
        "title_language_pattern_missing_canonical_language",
        "title_locale_pattern",
        "title_locale_pattern_missing_canonical_locale",
        "title_location_restriction_pattern",
        "title_location_restriction_needs_review",
        "ambiguous_or_risky_pattern",
    ):
        lines.append(f"- {label_for_key(key)}: {totals.get(key, 0)}")

    lines.extend(["", "By Source", "---------", ""])
    lines.append(
        "| Source | Rows | Canonical language | Canonical locale | Missing language | "
        "Missing locale | Location review | Ambiguous |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for source, counts in sorted(
        by_source.items(),
        key=lambda item: (-item[1].get("rows_inspected", 0), item[0]),
    ):
        lines.append(
            f"| {source} | {counts.get('rows_inspected', 0)} | "
            f"{counts.get('canonical_language', 0)} | "
            f"{counts.get('canonical_language_locale', 0)} | "
            f"{counts.get('title_language_pattern_missing_canonical_language', 0)} | "
            f"{counts.get('title_locale_pattern_missing_canonical_locale', 0)} | "
            f"{counts.get('title_location_restriction_needs_review', 0)} | "
            f"{counts.get('ambiguous_or_risky_pattern', 0)} |"
        )

    lines.extend(["", "Examples", "--------"])
    for source in sorted(examples):
        lines.extend(["", f"### {source}"])
        for key in (
            "title_language_pattern_missing_canonical_language",
            "title_locale_pattern_missing_canonical_locale",
            "title_location_restriction_needs_review",
            "ambiguous_or_risky_pattern",
        ):
            values = examples[source].get(key) or []
            if not values:
                continue
            lines.extend(["", f"{label_for_key(key)}:"])
            for value in values:
                lines.append(f"- {value}")

    lines.extend(
        [
            "",
            "Pattern Notes",
            "-------------",
            "- High-confidence language patterns: " + ", ".join(HIGH_CONFIDENCE_LANGUAGE_PATTERNS),
            "- High-confidence locale patterns: " + ", ".join(HIGH_CONFIDENCE_LOCALE_PATTERNS),
            "- High-confidence location patterns: " + ", ".join(HIGH_CONFIDENCE_LOCATION_PATTERNS),
            "- Ambiguous/risky patterns: " + ", ".join(AMBIGUOUS_PATTERNS),
        ]
    )
    return "\n".join(lines)


def label_for_key(key: str) -> str:
    return key.replace("_", " ").capitalize()


def normalize_text(value: str | None) -> str:
    text = str(value or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_value(value) -> str:
    return str(value or "").strip()


if __name__ == "__main__":
    raise SystemExit(main())
