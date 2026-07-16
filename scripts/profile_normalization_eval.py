#!/usr/bin/env python3
"""Evaluate profile normalizers against Profile Normalization Suite V1."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wahojobs.profiles.canonical import (  # noqa: E402
    complete_trusted_fixture_provenance,
    validate_canonical_profile,
)
from wahojobs.profiles.normalizer import (  # noqa: E402
    BaselineHeuristicProfileNormalizer,
    FixtureExpectedProfileNormalizer,
    compare_canonical_profiles,
)

DEFAULT_SUITE_PATH = ROOT / "tests" / "fixtures" / "profile_normalization_v1.json"


def main() -> int:
    args = parse_args()
    suite = load_suite(args.suite)
    normalizer = build_normalizer(args.normalizer, suite)
    evaluation = evaluate_suite(suite, normalizer)
    summary = render_terminal_summary(evaluation)
    print(summary)
    if args.out:
        output_path = Path(args.out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(render_markdown_report(evaluation), encoding="utf-8")
        print(f"Wrote report to {output_path}")
    return 0 if evaluation["valid_outputs"] == evaluation["total_cases"] else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--suite",
        type=Path,
        default=DEFAULT_SUITE_PATH,
        help="Profile normalization suite JSON path.",
    )
    parser.add_argument(
        "--normalizer",
        choices=("fixture", "baseline"),
        default="baseline",
        help="Normalizer implementation to evaluate.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Optional Markdown output path. No files are written unless this is provided.",
    )
    return parser.parse_args()


def load_suite(path: Path) -> dict:
    suite = json.loads(path.read_text(encoding="utf-8"))
    if suite.get("schema_version") != "profile_normalization_suite_v1":
        raise SystemExit(f"Unsupported suite schema_version: {suite.get('schema_version')!r}")
    for case in suite.get("cases", []):
        case["expected_canonical_profile"] = complete_trusted_fixture_provenance(
            case["expected_canonical_profile"]
        )
    return suite


def build_normalizer(name: str, suite: dict):
    if name == "fixture":
        return FixtureExpectedProfileNormalizer(suite)
    if name == "baseline":
        return BaselineHeuristicProfileNormalizer()
    raise SystemExit(f"Unsupported normalizer: {name}")


def evaluate_suite(suite: dict, normalizer) -> dict:
    cases = suite["cases"]
    case_results = []
    input_styles = Counter()
    archetypes = Counter()
    field_matches = Counter()
    field_totals = Counter()
    warnings = Counter()
    missing_critical_fields = Counter()
    valid_outputs = 0
    exact_matches = 0
    critical_details = []

    for case in cases:
        input_styles[case["input_style"]] += 1
        archetypes[case["archetype_id"]] += 1
        result = normalizer.normalize(
            case["raw_input"],
            case["input_style"],
            {
                "case_id": case["case_id"],
                "archetype_id": case["archetype_id"],
            },
        )
        validation_errors = []
        try:
            validate_canonical_profile(result.canonical_profile)
            valid_outputs += 1
        except ValueError as exc:
            validation_errors = [part.strip() for part in str(exc).split(";") if part.strip()]

        comparison = compare_canonical_profiles(
            case["expected_canonical_profile"],
            result.canonical_profile,
        ) if not validation_errors else invalid_comparison()
        if comparison["exact_match"]:
            exact_matches += 1
        for field_result in comparison["field_results"]:
            field = field_result["field"]
            field_totals[field] += 1
            if field_result["match"]:
                field_matches[field] += 1
        warnings.update(result.warnings)
        missing_critical_fields.update(comparison["missing_critical_fields"])
        if not validation_errors:
            critical_details.extend(
                critical_field_findings(
                    case["case_id"],
                    case["expected_canonical_profile"],
                    result.canonical_profile,
                )
            )
        case_results.append(
            {
                "case_id": case["case_id"],
                "archetype_id": case["archetype_id"],
                "input_style": case["input_style"],
                "valid": not validation_errors,
                "validation_errors": validation_errors,
                "exact_match": comparison["exact_match"],
                "matched_fields": comparison["matched_fields"],
                "total_fields": comparison["total_fields"],
                "field_match_rate": comparison["field_match_rate"],
                "missing_critical_fields": comparison["missing_critical_fields"],
                "top_mismatches": [
                    field_result
                    for field_result in comparison["field_results"]
                    if not field_result["match"]
                ][:8],
                "warnings": result.warnings,
                "missing_fields": result.missing_fields,
                "ambiguous_fields": result.ambiguous_fields,
                "extraction_quality": result.extraction_quality,
            }
        )

    total_field_matches = sum(field_matches.values())
    total_field_comparisons = sum(field_totals.values())
    critical_summary = summarize_critical_findings(
        critical_details,
        total_judgments=len(cases) * len(CRITICAL_FIELD_NAMES),
    )
    return {
        "normalizer": normalizer.name,
        "total_cases": len(cases),
        "valid_outputs": valid_outputs,
        "exact_matches": exact_matches,
        "input_styles": input_styles,
        "archetypes": archetypes,
        "field_matches": field_matches,
        "field_totals": field_totals,
        "field_match_rate": (
            total_field_matches / total_field_comparisons
            if total_field_comparisons
            else 1.0
        ),
        "warnings": warnings,
        "missing_critical_fields": missing_critical_fields,
        "critical_field_safety": critical_summary,
        "case_results": case_results,
    }


CRITICAL_FIELD_NAMES = (
    "country",
    "education_level",
    "degrees",
    "education_domains",
    "credentials",
    "professional_domains",
    "total_years",
    "excluded_domains",
    "languages",
)


def critical_field_findings(case_id, expected, actual):
    findings = []
    fields = {
        "country": (
            expected["location"].get("country") or "",
            actual["location"].get("country") or "",
        ),
        "education_level": (
            expected["education"].get("education_level") or "not_specified",
            actual["education"].get("education_level") or "not_specified",
        ),
        "degrees": (
            expected["education"].get("degrees") or [],
            actual["education"].get("degrees") or [],
        ),
        "education_domains": (
            expected["education"].get("fields_or_domains") or [],
            actual["education"].get("fields_or_domains") or [],
        ),
        "credentials": (
            list(expected["credentials"].get("certifications") or [])
            + list(expected["credentials"].get("licenses") or []),
            list(actual["credentials"].get("certifications") or [])
            + list(actual["credentials"].get("licenses") or []),
        ),
        "professional_domains": (
            expected["experience"].get("professional_domains") or [],
            actual["experience"].get("professional_domains") or [],
        ),
        "total_years": (
            expected["experience"].get("total_years"),
            actual["experience"].get("total_years"),
        ),
        "excluded_domains": (
            expected["constraints"].get("excluded_domains") or [],
            actual["constraints"].get("excluded_domains") or [],
        ),
    }
    expected_languages = {
        str(row["language"]).casefold(): str(row.get("proficiency") or "unknown")
        for row in expected["languages"]
    }
    actual_languages = {
        str(row["language"]).casefold(): str(row.get("proficiency") or "unknown")
        for row in actual["languages"]
    }
    fields["languages"] = (expected_languages, actual_languages)

    ambiguous_fields = set(actual["provenance"].get("ambiguous_fields") or [])
    for field, (expected_value, actual_value) in fields.items():
        findings.extend(
            compare_critical_field(
                case_id,
                field,
                expected_value,
                actual_value,
                ambiguous=field in ambiguous_fields,
            )
        )
    return findings


def compare_critical_field(case_id, field, expected, actual, *, ambiguous=False):
    if normalized_critical_value(expected, field=field) == normalized_critical_value(actual, field=field):
        return [critical_finding(case_id, field, "uncertain", expected, actual)] if ambiguous else []

    false_positive = False
    false_negative = False
    uncertain = ambiguous

    if isinstance(expected, list) and isinstance(actual, list):
        expected_items = {normalize_critical_item(field, item) for item in expected}
        actual_items = {normalize_critical_item(field, item) for item in actual}
        false_positive = bool(actual_items - expected_items)
        false_negative = bool(expected_items - actual_items)
        uncertain = uncertain or false_negative
    elif isinstance(expected, dict) and isinstance(actual, dict):
        expected_items = {
            str(key).strip().casefold(): str(value).strip().casefold()
            for key, value in expected.items()
        }
        actual_items = {
            str(key).strip().casefold(): str(value).strip().casefold()
            for key, value in actual.items()
        }
        false_positive = bool(actual_items.keys() - expected_items.keys())
        false_negative = bool(expected_items.keys() - actual_items.keys())
        for key in expected_items.keys() & actual_items.keys():
            expected_detail = expected_items[key]
            actual_detail = actual_items[key]
            if expected_detail == actual_detail:
                continue
            if actual_detail in {"", "unknown", "not_specified"}:
                false_negative = True
                uncertain = True
            elif expected_detail in {"", "unknown", "not_specified"}:
                false_positive = True
            else:
                false_positive = True
                false_negative = True
        uncertain = uncertain or false_negative
    else:
        expected_material = critical_value_is_material(field, expected)
        actual_material = critical_value_is_material(field, actual)
        false_positive = actual_material
        false_negative = expected_material
        uncertain = uncertain or not actual_material or false_negative

    findings = []
    if false_positive:
        findings.append(critical_finding(case_id, field, "false_positive", expected, actual))
    if false_negative:
        findings.append(critical_finding(case_id, field, "false_negative", expected, actual))
    if uncertain:
        findings.append(critical_finding(case_id, field, "uncertain", expected, actual))
    return findings


def normalize_critical_item(field, value):
    normalized = str(value).strip().casefold()
    if field == "professional_domains":
        aliases = {
            "software engineering": "software",
            "coding": "software",
            "python": "software",
            "microbiology": "biology",
            "life sciences": "biology",
            "law": "legal",
        }
        return aliases.get(normalized, normalized)
    if field == "credentials":
        aliases = {
            "attorney license": "attorney",
            "registered nurse license": "registered nurse",
        }
        return aliases.get(normalized, normalized)
    return normalized


def normalized_critical_value(value, *, field=""):
    if isinstance(value, dict):
        return tuple(sorted((str(key).casefold(), str(item).casefold()) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(sorted({normalize_critical_item(field, item) for item in value}))
    if isinstance(value, str):
        return value.casefold()
    return value


def critical_value_is_material(field, value):
    if field == "education_level":
        return value not in {None, "", "unknown", "not_specified"}
    if field == "total_years":
        return value is not None
    return bool(value)


def critical_finding(case_id, field, kind, expected, actual):
    blocking = kind == "false_positive" and field in {
        "country", "languages", "credentials", "professional_domains"
    }
    if field == "professional_domains" and normalized_critical_value(actual, field=field) == ("generalist",):
        blocking = False
    return {
        "case_id": case_id,
        "field": field,
        "kind": kind,
        "blocking": blocking,
        "expected": expected,
        "actual": actual,
    }


def summarize_critical_findings(findings, *, total_judgments):
    counts = Counter(item["kind"] for item in findings)
    blocking = [item for item in findings if item["blocking"]]
    evaluated = max(1, total_judgments)
    return {
        "total_judgments": total_judgments,
        "false_positive_count": counts["false_positive"],
        "false_negative_count": counts["false_negative"],
        "uncertain_count": counts["uncertain"],
        "false_positive_rate": counts["false_positive"] / evaluated,
        "false_negative_rate": counts["false_negative"] / evaluated,
        "uncertain_rate": counts["uncertain"] / evaluated,
        "blocking_false_positive_count": len(blocking),
        "blocking_false_positives": blocking,
        "findings": findings,
    }


def invalid_comparison() -> dict:
    return {
        "exact_match": False,
        "matched_fields": 0,
        "total_fields": 0,
        "field_match_rate": 0.0,
        "field_results": [],
        "missing_critical_fields": [],
    }


def render_terminal_summary(evaluation: dict) -> str:
    lines = [
        "",
        "Profile Normalization Evaluation",
        "================================",
        f"Normalizer: {evaluation['normalizer']}",
        f"Total cases: {evaluation['total_cases']}",
        f"Valid canonical_profile_v1 outputs: {evaluation['valid_outputs']}/{evaluation['total_cases']}",
        f"Exact canonical matches: {evaluation['exact_matches']}/{evaluation['total_cases']}",
        f"Structured field match rate: {format_percent(evaluation['field_match_rate'])}",
        f"Input styles: {format_counter(evaluation['input_styles'])}",
        f"Archetypes: {format_counter(evaluation['archetypes'])}",
        f"Warnings: {format_counter(evaluation['warnings'])}",
        f"Missing critical fields: {format_counter(evaluation['missing_critical_fields'])}",
        (
            "Critical-field safety: "
            f"false positives={evaluation['critical_field_safety']['false_positive_count']} "
            f"({format_percent(evaluation['critical_field_safety']['false_positive_rate'])}), "
            f"false negatives={evaluation['critical_field_safety']['false_negative_count']} "
            f"({format_percent(evaluation['critical_field_safety']['false_negative_rate'])}), "
            f"uncertain={evaluation['critical_field_safety']['uncertain_count']} "
            f"({format_percent(evaluation['critical_field_safety']['uncertain_rate'])}), "
            f"blocking false positives={evaluation['critical_field_safety']['blocking_false_positive_count']}"
        ),
        "",
        "Lowest-match cases:",
    ]
    lowest = sorted(
        evaluation["case_results"],
        key=lambda item: (item["field_match_rate"], item["case_id"]),
    )[:8]
    for item in lowest:
        mismatch_labels = ", ".join(field["field"] for field in item["top_mismatches"][:4]) or "-"
        lines.append(
            f"- {item['case_id']} ({item['input_style']}): "
            f"{item['matched_fields']}/{item['total_fields']} fields; mismatches: {mismatch_labels}"
        )
    return "\n".join(lines)


def render_markdown_report(evaluation: dict) -> str:
    lines = [
        "# Profile Normalization Evaluation",
        "",
        f"- Normalizer: `{evaluation['normalizer']}`",
        f"- Total cases: {evaluation['total_cases']}",
        f"- Valid canonical outputs: {evaluation['valid_outputs']}/{evaluation['total_cases']}",
        f"- Exact canonical matches: {evaluation['exact_matches']}/{evaluation['total_cases']}",
        f"- Structured field match rate: {format_percent(evaluation['field_match_rate'])}",
        f"- Input styles: {format_counter(evaluation['input_styles'])}",
        f"- Archetypes: {format_counter(evaluation['archetypes'])}",
        f"- Warnings: {format_counter(evaluation['warnings'])}",
        f"- Missing critical fields: {format_counter(evaluation['missing_critical_fields'])}",
        f"- Critical false positives: {evaluation['critical_field_safety']['false_positive_count']} ({format_percent(evaluation['critical_field_safety']['false_positive_rate'])})",
        f"- Critical false negatives: {evaluation['critical_field_safety']['false_negative_count']} ({format_percent(evaluation['critical_field_safety']['false_negative_rate'])})",
        f"- Critical uncertain/review-required: {evaluation['critical_field_safety']['uncertain_count']} ({format_percent(evaluation['critical_field_safety']['uncertain_rate'])})",
        f"- Blocking critical false positives: {evaluation['critical_field_safety']['blocking_false_positive_count']}",
        "",
        "## Cases",
        "",
        "| Case | Style | Valid | Exact | Field match | Top mismatches |",
        "|---|---|---:|---:|---:|---|",
    ]
    for item in evaluation["case_results"]:
        mismatches = ", ".join(field["field"] for field in item["top_mismatches"][:5]) or "-"
        lines.append(
            "| "
            + " | ".join(
                [
                    item["case_id"],
                    item["input_style"],
                    "yes" if item["valid"] else "no",
                    "yes" if item["exact_match"] else "no",
                    format_percent(item["field_match_rate"]),
                    mismatches,
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def format_counter(counter: Counter) -> str:
    return ", ".join(f"{key}: {value}" for key, value in sorted(counter.items())) or "-"


def format_percent(value: float) -> str:
    return f"{value * 100:.1f}%"


if __name__ == "__main__":
    raise SystemExit(main())
