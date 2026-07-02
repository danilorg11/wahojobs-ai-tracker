import json
import sys
from collections import Counter
from html import escape
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wahojobs.profiles.canonical import canonical_profile_debug_summary, validate_canonical_profile

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "profile_normalization_v1.json"
HTML_PATH = ROOT / "exports" / "profile_normalization_v1_review.html"
SUMMARY_PATH = ROOT / "exports" / "profile_normalization_v1_summary.md"


def main():
    suite = load_suite()
    cases = suite["cases"]
    for case in cases:
        validate_canonical_profile(case["expected_canonical_profile"])

    HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    HTML_PATH.write_text(render_html(suite), encoding="utf-8")
    SUMMARY_PATH.write_text(render_markdown(suite), encoding="utf-8")

    print("")
    print("Profile Normalization Suite V1")
    print("==============================")
    print(f"Cases: {len(cases)}")
    print(f"Archetypes: {len(count_by(cases, 'archetype_id'))}")
    print(f"Input styles: {format_counter(count_by(cases, 'input_style'))}")
    print(f"Wrote HTML review to {HTML_PATH.relative_to(ROOT)}")
    print(f"Wrote Markdown summary to {SUMMARY_PATH.relative_to(ROOT)}")


def load_suite():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def render_markdown(suite):
    cases = suite["cases"]
    lines = [
        "# Profile Normalization Suite V1",
        "",
        "This read-only suite defines expected `canonical_profile_v1` outputs for raw user profile inputs. It is not a parser and does not call external services.",
        "",
        "## Summary",
        "",
        f"- Total cases: {len(cases)}",
        f"- Archetypes: {format_counter(count_by(cases, 'archetype_id'))}",
        f"- Input styles: {format_counter(count_by(cases, 'input_style'))}",
        f"- Normalization focus: {format_counter(focus_counts(cases))}",
        "",
        "## Cases",
        "",
        "| Case | Archetype | Input style | Languages | Location | Credentials | Missing / ambiguous | Focus |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for case in cases:
        canonical = case["expected_canonical_profile"]
        summary = canonical_profile_debug_summary(canonical)
        lines.append(
            "| "
            + " | ".join(
                [
                    escape_md(case["case_id"]),
                    escape_md(case["archetype_id"]),
                    escape_md(case["input_style"]),
                    escape_md(join_languages(canonical)),
                    escape_md(location_label(canonical)),
                    escape_md(credentials_label(canonical)),
                    escape_md(", ".join(summary["missing_fields"] + summary["ambiguous_fields"]) or "-"),
                    escape_md(", ".join(case["normalization_focus"])),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Preserve explicit facts from resumes, LinkedIn-style profiles, paragraphs, and messy sparse self-descriptions.",
            "- Use `unknown`, `absent`, missing fields, and ambiguous fields instead of inventing credentials, licenses, locations, years, or languages.",
            "- This suite is a contract for future deterministic or reviewed extraction; it does not implement extraction.",
            "",
        ]
    )
    return "\n".join(lines)


def render_html(suite):
    cases = suite["cases"]
    cards = "\n".join(render_case_card(case) for case in cases)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Profile Normalization Suite V1</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 32px; color: #17202a; }}
    h1, h2 {{ margin-bottom: 0.3rem; }}
    .summary {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 20px 0; }}
    .box {{ border: 1px solid #d8dee4; border-radius: 6px; padding: 12px; background: #f8fafc; }}
    .case {{ border-top: 1px solid #d8dee4; padding: 18px 0; }}
    .meta span {{ display: inline-block; margin: 0 6px 6px 0; padding: 3px 7px; border-radius: 999px; background: #eef2ff; font-size: 12px; }}
    pre {{ white-space: pre-wrap; background: #f6f8fa; padding: 12px; border-radius: 6px; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 8px; }}
    th, td {{ border: 1px solid #d8dee4; padding: 6px; text-align: left; vertical-align: top; }}
    th {{ background: #f6f8fa; }}
  </style>
</head>
<body>
  <h1>Profile Normalization Suite V1</h1>
  <p>This suite defines expected <code>canonical_profile_v1</code> outputs for future profile extraction. It is not an extractor.</p>
  <div class="summary">
    <div class="box"><strong>Total cases</strong><br>{len(cases)}</div>
    <div class="box"><strong>Archetypes</strong><br>{escape(format_counter(count_by(cases, 'archetype_id')))}</div>
    <div class="box"><strong>Input styles</strong><br>{escape(format_counter(count_by(cases, 'input_style')))}</div>
  </div>
  <h2>Cases</h2>
  {cards}
</body>
</html>
"""


def render_case_card(case):
    canonical = case["expected_canonical_profile"]
    summary = canonical_profile_debug_summary(canonical)
    return f"""
<section class="case" id="{escape(case['case_id'])}">
  <h3>{escape(case['case_id'])}</h3>
  <div class="meta">
    <span>{escape(case['archetype_id'])}</span>
    <span>{escape(case['input_style'])}</span>
    {''.join(f'<span>{escape(tag)}</span>' for tag in case['normalization_focus'])}
  </div>
  <p><strong>Raw input:</strong></p>
  <pre>{escape(case['raw_input'])}</pre>
  <table>
    <tr><th>Languages</th><td>{escape(join_languages(canonical))}</td></tr>
    <tr><th>Location</th><td>{escape(location_label(canonical))}</td></tr>
    <tr><th>Education</th><td>{escape(canonical['education'].get('education_level') or '-')} / {escape(', '.join(canonical['education'].get('fields_or_domains') or []) or '-')}</td></tr>
    <tr><th>Credentials</th><td>{escape(credentials_label(canonical))}</td></tr>
    <tr><th>Experience</th><td>{escape(experience_label(canonical))}</td></tr>
    <tr><th>Skills</th><td>{escape(', '.join(canonical['skills'].get('normalized') or []) or '-')}</td></tr>
    <tr><th>Missing / ambiguous</th><td>{escape(', '.join(summary['missing_fields'] + summary['ambiguous_fields']) or '-')}</td></tr>
    <tr><th>Review notes</th><td>{escape(case['review_notes'])}</td></tr>
  </table>
</section>
"""


def count_by(cases, field):
    return Counter(case[field] for case in cases)


def focus_counts(cases):
    counts = Counter()
    for case in cases:
        counts.update(case["normalization_focus"])
    return counts


def format_counter(counter):
    return ", ".join(f"{key}: {value}" for key, value in sorted(counter.items())) or "-"


def join_languages(canonical):
    parts = []
    for item in canonical["languages"]:
        label = item["language"]
        if item.get("proficiency") and item["proficiency"] != "unknown":
            label += f" ({item['proficiency']})"
        if item.get("locale"):
            label += f" [{item['locale']}]"
        parts.append(label)
    return ", ".join(parts) or "-"


def location_label(canonical):
    location = canonical["location"]
    parts = [location.get(field, "") for field in ("city", "region", "country") if location.get(field)]
    return ", ".join(parts) or location.get("remote_eligibility") or "-"


def credentials_label(canonical):
    credentials = canonical["credentials"]
    parts = []
    if credentials.get("certifications"):
        parts.append("certifications: " + ", ".join(credentials["certifications"]))
    if credentials.get("licenses"):
        parts.append("licenses: " + ", ".join(credentials["licenses"]))
    if credentials.get("jurisdictions"):
        parts.append("jurisdictions: " + ", ".join(credentials["jurisdictions"]))
    parts.append("status: " + (credentials.get("credential_status") or "unknown"))
    return "; ".join(parts)


def experience_label(canonical):
    experience = canonical["experience"]
    parts = []
    if experience.get("total_years") is not None:
        parts.append(f"{experience['total_years']} years")
    if experience.get("seniority") and experience["seniority"] != "unknown":
        parts.append(experience["seniority"])
    if experience.get("specialties"):
        parts.append(", ".join(experience["specialties"]))
    return "; ".join(parts) or "-"


def escape_md(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    main()
