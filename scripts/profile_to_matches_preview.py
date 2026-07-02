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
UNCONFIRMED_LANGUAGE_TERMS = {
    "american sign language": "american sign language",
    "assamese": "assamese",
    "asl": "american sign language",
    "asturian": "asturian",
    "aymara": "aymara",
    "basque": "basque",
}
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
ACTIONABILITY_RANK = {
    "excluded": 0,
    "explore_only": 1,
    "also_worth_reviewing": 2,
    "best_matches": 3,
    "do_these_first": 4,
}


def main() -> int:
    args = parse_args()
    raw_input = read_input(args)
    context = build_preview_context(raw_input, args.input_style, limit=args.limit)
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
    return parser.parse_args()


def read_input(args: argparse.Namespace) -> str:
    if args.input_text is not None:
        value = args.input_text
    else:
        value = args.input_file.read_text(encoding="utf-8")
    if not value.strip():
        raise SystemExit("Profile input is empty.")
    return value.strip()


def build_preview_context(raw_input: str, input_style: str, limit: int = DEFAULT_LIMIT) -> dict:
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
    grouped_matches = build_grouped_matches(matcher_profile, limit)
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
    }


def build_grouped_matches(profile: dict, limit: int) -> dict:
    rows = load_preview_rows()
    scored = []
    for row in rows:
        match = matcher.score_opportunity(profile, row)
        match = apply_preview_guardrails(profile, row, match)
        scored.append(match)

    deduped = dedupe_matches(scored)
    groups = {section: [] for section in SECTION_ORDER}
    for match in sorted(deduped, key=match_sort_key):
        section = match["preview_section"]
        if len(groups[section]) >= limit:
            continue
        groups[section].append(match)
    return groups


def load_preview_rows() -> list[dict]:
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
    return rows


def apply_preview_guardrails(profile: dict, row: dict, match: dict) -> dict:
    match = dict(match)
    base_section = preview_section_for_match(match)
    diagnostics = preview_diagnostics_for_match(profile, row, match)
    capped_section = base_section
    for diagnostic in diagnostics:
        if diagnostic.startswith("Possible unconfirmed language requirement"):
            capped_section = cap_section(capped_section, "explore_only")
        elif diagnostic.startswith("Profile states no biology or medical credentials"):
            capped_section = cap_section(capped_section, "explore_only")
        elif diagnostic.startswith("Medical license or credential may be required"):
            capped_section = cap_section(capped_section, "explore_only")
    match["preview_section"] = capped_section
    match["preview_diagnostics"] = diagnostics
    return match


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


def preview_diagnostics_for_match(profile: dict, row: dict, match: dict) -> list[str]:
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
    if profile_has_no_biology_medical_credentials(profile) and cross_domain_science_coding_row(row_text, profile):
        diagnostics.append(
            "Profile states no biology or medical credentials; cross-domain science/coding role "
            "capped to Explore Only."
        )
    if profile_has_no_medical_license(profile) and licensed_medical_row(row_text):
        diagnostics.append(
            "Medical license or credential may be required; capped to Explore Only until confirmed."
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


def render_html(context: dict) -> str:
    canonical = context["canonical_profile"]
    sections = "\n".join(render_html_section(section, context["matches"][section]) for section in SECTION_ORDER)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Profile to Matches Preview</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 32px; color: #17202a; line-height: 1.45; }}
    .notice {{ border: 1px solid #d8dee4; background: #f6f8fa; padding: 12px; border-radius: 6px; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }}
    .box, .match {{ border: 1px solid #d8dee4; border-radius: 6px; padding: 12px; margin: 10px 0; }}
    .match {{ background: #fff; }}
    .meta {{ color: #57606a; font-size: 0.92rem; }}
    code {{ background: #f6f8fa; padding: 1px 4px; border-radius: 4px; }}
  </style>
</head>
<body>
  <h1>Profile to Matches Preview</h1>
  <p class="notice">{html_escape(context['disclaimer'])}</p>
  <p class="meta">Generated: {html_escape(context['generated_at'])} | Input style: {html_escape(context['input_style'])}</p>
  <h2>Canonical Profile Preview</h2>
  <div class="grid">
    <div class="box"><strong>Languages</strong><br>{html_escape(join_languages(canonical))}</div>
    <div class="box"><strong>Location</strong><br>{html_escape(location_label(canonical))}</div>
    <div class="box"><strong>Remote preference</strong><br>{html_escape(remote_preference_label(canonical))}</div>
    <div class="box"><strong>Education</strong><br>{html_escape(canonical['education'].get('education_level') or 'unknown')}</div>
    <div class="box"><strong>Domains</strong><br>{html_escape(', '.join(canonical['education'].get('fields_or_domains') or []) or '-')}</div>
    <div class="box"><strong>Credentials/licenses</strong><br>{html_escape(credentials_label(canonical))}</div>
    <div class="box"><strong>Missing</strong><br>{html_escape(', '.join(context['missing_fields']) or '-')}</div>
    <div class="box"><strong>Ambiguous</strong><br>{html_escape(', '.join(context['ambiguous_fields']) or '-')}</div>
  </div>
  <h2>Warnings</h2>
  <ul>
    {''.join(f'<li>{html_escape(warning)}</li>' for warning in context['warnings']) or '<li>-</li>'}
  </ul>
  <h2>Recommended Opportunities</h2>
  {sections}
</body>
</html>
"""


def render_html_section(section: str, matches: list[dict]) -> str:
    cards = "\n".join(render_html_match(match) for match in matches) if matches else "<p>None in this preview.</p>"
    if section in {"explore_only", "excluded"}:
        return (
            f"<details><summary>{html_escape(SECTION_LABELS[section])} ({len(matches)}) "
            "- diagnostic browse results</summary>"
            f"{cards}</details>"
        )
    return f"<section><h3>{html_escape(SECTION_LABELS[section])} ({len(matches)})</h3>{cards}</section>"


def render_html_match(match: dict) -> str:
    reasons = "; ".join(match.get("reasons") or []) or "-"
    url = match.get("url") or ""
    link = f'<a href="{html_escape(url)}">Open</a>' if url else "-"
    return f"""
<article class="match">
  <h4>{html_escape(match['display_title'])}</h4>
  <p class="meta">{html_escape(match['source'])} | {html_escape(match.get('location') or 'Unknown')} | {html_escape(match.get('expertise') or 'Unknown')} | {match['score']} pts</p>
  <p>{html_escape(reasons)}</p>
  <p class="meta">Diagnostics: {html_escape('; '.join(match.get('preview_diagnostics') or []) or '-')}</p>
  <p>{link}</p>
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
