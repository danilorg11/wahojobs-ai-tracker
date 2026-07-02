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

from wahojobs.profiles.canonical import validate_canonical_profile  # noqa: E402
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
        "case_results": case_results,
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
