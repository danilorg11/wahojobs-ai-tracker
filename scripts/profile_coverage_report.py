import argparse
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from profile_match_digest import (
    MOCK_PROFILES,
    load_profiles,
    select_profiles,
    get_active_rows,
    get_post_baseline_new_rows,
    rank_opportunities,
    console_text,
    escape,
)
from wahojobs.classification import (
    INVENTORY_MODEL_EVERGREEN_APPLICATION,
    INVENTORY_MODEL_PUBLIC_INVENTORY,
    INVENTORY_MODEL_MIXED,
    MARKET_COUNT_POLICY_COUNT_LIVE,
)
from wahojobs.db.connection import get_connection
from wahojobs.reporting.market import get_market_size_summary


OUTPUT_PATH = Path("exports/profile_coverage_report.md")
RECENT_DAYS = 7
MONTH_DAYS = 30
COUNT_LIMIT = 10000
STRONG_MIN = 30
MEDIUM_MIN = 22
WEAK_MIN = 14


def main():
    args = parse_args()
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    cutoff_7d = (generated_at - timedelta(days=RECENT_DAYS)).isoformat()
    cutoff_30d = (generated_at - timedelta(days=MONTH_DAYS)).isoformat()

    profiles, profile_source = load_profiles(args.profiles_file)
    profiles = select_profiles(profiles, args.profile)

    with get_connection() as conn:
        market_summary = get_market_size_summary(
            conn,
            include_experimental=False,
            include_simulation=False,
        )
        live_rows = get_active_rows(conn, policy=MARKET_COUNT_POLICY_COUNT_LIVE)
        evergreen_rows = get_active_rows(
            conn,
            policy_not=MARKET_COUNT_POLICY_COUNT_LIVE,
            inventory_models=(INVENTORY_MODEL_EVERGREEN_APPLICATION,),
        )
        public_rows = get_active_rows(
            conn,
            policy_not=MARKET_COUNT_POLICY_COUNT_LIVE,
            inventory_models=(INVENTORY_MODEL_PUBLIC_INVENTORY, INVENTORY_MODEL_MIXED),
        )
        new_7d_rows = get_post_baseline_new_rows(conn, cutoff_7d)
        new_30d_rows = get_post_baseline_new_rows(conn, cutoff_30d)

    reports = [
        build_profile_coverage(
            profile,
            live_rows,
            evergreen_rows,
            public_rows,
            new_7d_rows,
            new_30d_rows,
        )
        for profile in profiles
    ]

    markdown = render_markdown(
        generated_at,
        profile_source,
        market_summary,
        reports,
    )
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(markdown, encoding="utf-8")

    print("")
    print("Wahojobs Profile Coverage Report")
    print("================================")
    print(f"Generated: {generated_at.isoformat()} UTC")
    print(f"Profile source: {profile_source}")
    print(f"Profiles evaluated: {len(reports)}")
    print(
        "Estimated Live Market Opportunities: "
        f"{market_summary['estimated_market_opportunities']}"
    )
    for report in sorted(reports, key=lambda item: item["coverage_score"], reverse=True)[:3]:
        print(
            f"{report['profile']['profile_id']}: "
            f"{report['risk_label']} risk, "
            f"{report['strong_live']} strong live, "
            f"{report['source_diversity']} sources"
        )
    print(f"Wrote Markdown report to {OUTPUT_PATH}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Evaluate profile coverage against the current Wahojobs database."
    )
    parser.add_argument(
        "--profiles-file",
        type=Path,
        help="Load editable profiles from a JSON file.",
    )
    parser.add_argument(
        "--profile",
        help="Evaluate one profile_id only.",
    )
    return parser.parse_args()


def build_profile_coverage(profile, live_rows, evergreen_rows, public_rows, new_7d_rows, new_30d_rows):
    strong_live = match_band(profile, live_rows, STRONG_MIN)
    medium_live = match_band(profile, live_rows, MEDIUM_MIN, STRONG_MIN - 1)
    weak_live = match_band(profile, live_rows, WEAK_MIN, MEDIUM_MIN - 1)
    evergreen = match_band(profile, evergreen_rows, WEAK_MIN)
    public = match_band(profile, public_rows, WEAK_MIN)
    new_7d = match_band(profile, new_7d_rows, WEAK_MIN)
    new_30d = match_band(profile, new_30d_rows, WEAK_MIN)

    live_matches = strong_live + medium_live + weak_live
    source_counts = Counter(match["source"] for match in live_matches)
    area_counts = Counter(match["expertise"] for match in live_matches)
    top_score = strong_live[0]["score"] if strong_live else (
        medium_live[0]["score"] if medium_live else (weak_live[0]["score"] if weak_live else 0)
    )
    average_score = round(
        sum(match["score"] for match in live_matches) / len(live_matches),
        1,
    ) if live_matches else 0
    source_diversity = len(source_counts)
    concentration = source_concentration(source_counts, live_matches)
    risk_label = assess_risk(
        strong_live,
        medium_live,
        new_7d,
        new_30d,
        source_diversity,
        concentration,
    )

    return {
        "profile": profile,
        "strong_live": len(strong_live),
        "medium_live": len(medium_live),
        "weak_live": len(weak_live),
        "evergreen": len(evergreen),
        "public": len(public),
        "new_7d": len(new_7d),
        "new_30d": len(new_30d),
        "source_diversity": source_diversity,
        "top_score": top_score,
        "average_score": average_score,
        "concentration": concentration,
        "risk_label": risk_label,
        "coverage_score": coverage_score(
            strong_live,
            medium_live,
            weak_live,
            evergreen,
            public,
            new_7d,
            new_30d,
            source_diversity,
        ),
        "top_sources": source_counts.most_common(6),
        "top_areas": area_counts.most_common(6),
        "examples": strong_live[:5] or medium_live[:5] or weak_live[:5],
        "recent_examples": new_7d[:5] or new_30d[:5],
        "action_cards": action_cards(
            profile,
            risk_label,
            strong_live,
            medium_live,
            evergreen,
            public,
            new_7d,
            source_counts,
            area_counts,
        ),
    }


def match_band(profile, rows, min_score, max_score=None):
    return rank_opportunities(
        profile,
        rows,
        group_canonical=True,
        limit=COUNT_LIMIT,
        min_score=min_score,
        max_score=max_score,
    )


def source_concentration(source_counts, live_matches):
    if not live_matches or not source_counts:
        return 0
    return round(source_counts.most_common(1)[0][1] / len(live_matches), 2)


def assess_risk(strong_live, medium_live, new_7d, new_30d, source_diversity, concentration):
    strong = len(strong_live)
    strong_medium = len(strong_live) + len(medium_live)
    recent = len(new_7d) + len(new_30d)

    if strong >= 20 and strong_medium >= 50 and source_diversity >= 4 and recent >= 5:
        return "Low"
    if strong >= 5 and strong_medium >= 15 and source_diversity >= 2:
        if recent == 0 or concentration >= 0.75:
            return "Medium"
        return "Low"
    if strong_medium >= 8 and source_diversity >= 2:
        return "Medium"
    return "High"


def coverage_score(strong_live, medium_live, weak_live, evergreen, public, new_7d, new_30d, source_diversity):
    return (
        len(strong_live) * 5
        + len(medium_live) * 3
        + len(weak_live)
        + min(len(evergreen), 10)
        + min(len(public), 10)
        + len(new_7d) * 2
        + len(new_30d)
        + source_diversity * 4
    )


def action_cards(profile, risk_label, strong_live, medium_live, evergreen, public, new_7d, source_counts, area_counts):
    cards = []
    if evergreen:
        cards.append("Apply to strong evergreen/application platforms while monitoring live postings.")
    if public:
        cards.append("Review public-inventory/report-separately matches as supplemental leads.")
    if not new_7d:
        cards.append("Set expectations: recent movement is thin, so weekly monitoring may be enough.")
    if len(source_counts) <= 2:
        cards.append("Broaden acceptable opportunity types or monitor adjacent sources to avoid one-source dependence.")
    if risk_label in {"Medium", "High"}:
        cards.append("Improve profile positioning with clearer domain, language, and review/evaluation keywords.")
        cards.append("Prepare reusable assessment materials such as a writing sample or domain-work sample.")
    if any("language" in value.lower() or "translation" in value.lower() for value in profile["degrees_or_domains"] + profile["skills"]):
        cards.append("List exact languages, locales, and proficiency levels in applications.")
    if any("coding" in value.lower() or "python" in value.lower() or "software" in value.lower() for value in profile["degrees_or_domains"] + profile["skills"]):
        cards.append("Prepare a small code-review or debugging sample for coding-evaluation assessments.")
    if area_counts:
        top_area = area_counts.most_common(1)[0][0]
        cards.append(f"Lead with the strongest current area: {top_area}.")
    return unique(cards)[:5]


def render_markdown(generated_at, profile_source, market_summary, reports):
    strongest = sorted(reports, key=lambda item: item["coverage_score"], reverse=True)
    weakest = sorted(reports, key=lambda item: item["coverage_score"])
    lines = [
        "# Profile Coverage Report",
        "",
        f"Generated: {generated_at.isoformat()} UTC",
        "",
        "## Executive Summary",
        "",
        f"- Profile source: **{escape(profile_source)}**",
        f"- Profiles evaluated: **{len(reports)}**",
        f"- Estimated Live Market Opportunities: **{market_summary['estimated_market_opportunities']}**",
        f"- Strongest coverage: **{escape(strongest[0]['profile']['display_name'])}** ({strongest[0]['risk_label']} risk)",
        f"- Weakest coverage: **{escape(weakest[0]['profile']['display_name'])}** ({weakest[0]['risk_label']} risk)",
        f"- Main product insight: {product_insight(reports)}",
        "",
    ]

    append_coverage_table(lines, reports)
    append_profile_details(lines, strongest)
    append_product_notes(lines, reports)
    return "\n".join(lines)


def append_coverage_table(lines, reports):
    lines.extend(
        [
            "## Coverage Table",
            "",
            "| Profile | Strong Live | Medium Live | Evergreen | Public/Separate | New 7d | New 30d | Sources | Risk |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for report in sorted(reports, key=lambda item: item["coverage_score"], reverse=True):
        lines.append(
            "| "
            f"{escape(report['profile']['display_name'])} | "
            f"{report['strong_live']} | "
            f"{report['medium_live']} | "
            f"{report['evergreen']} | "
            f"{report['public']} | "
            f"{report['new_7d']} | "
            f"{report['new_30d']} | "
            f"{report['source_diversity']} | "
            f"{report['risk_label']} |"
        )
    lines.append("")


def append_profile_details(lines, reports):
    lines.extend(["## Profile Details", ""])
    for report in reports:
        profile = report["profile"]
        lines.extend(
            [
                f"### {escape(profile['display_name'])}",
                "",
                f"- Profile ID: `{profile['profile_id']}`",
                f"- Risk label: **{report['risk_label']}**",
                f"- Strong/medium/weak live matches: {report['strong_live']} / {report['medium_live']} / {report['weak_live']}",
                f"- Evergreen / public-separate matches: {report['evergreen']} / {report['public']}",
                f"- Post-baseline new matches: {report['new_7d']} in 7d; {report['new_30d']} in 30d",
                f"- Source diversity: {report['source_diversity']} sources; top-source concentration: {int(report['concentration'] * 100)}%",
                f"- Average/top score: {report['average_score']} / {report['top_score']}",
                "",
            ]
        )
        append_counter_list(lines, "Top Sources", report["top_sources"])
        append_counter_list(lines, "Top Opportunity Areas", report["top_areas"])
        append_match_examples(lines, "Strongest Match Examples", report["examples"])
        append_match_examples(lines, "Recent Post-Baseline Match Examples", report["recent_examples"])
        append_action_cards(lines, report["action_cards"])
        lines.extend(
            [
                "#### Retention / Coverage Notes",
                "",
                coverage_note(report),
                "",
            ]
        )


def append_counter_list(lines, title, rows):
    lines.extend([f"#### {title}", ""])
    if not rows:
        lines.extend(["None.", ""])
        return
    for label, count in rows:
        lines.append(f"- {escape(label)}: {count}")
    lines.append("")


def append_match_examples(lines, title, matches):
    lines.extend([f"#### {title}", ""])
    if not matches:
        lines.extend(["No examples found.", ""])
        return
    lines.extend(
        [
            "| Score | Title | Source | Area | URL |",
            "| ---: | --- | --- | --- | --- |",
        ]
    )
    for match in matches[:5]:
        lines.append(
            "| "
            f"{match['score']} | "
            f"{escape(match['display_title'])} | "
            f"{escape(match['source'])} | "
            f"{escape(match['expertise'])} | "
            f"[Open]({match['url']}) |"
        )
    lines.append("")


def append_action_cards(lines, cards):
    lines.extend(["#### Recommended Action Cards", ""])
    if not cards:
        lines.extend(["No special actions; current coverage looks healthy.", ""])
        return
    for card in cards:
        lines.append(f"- {escape(card)}")
    lines.append("")


def append_product_notes(lines, reports):
    strongest = sorted(reports, key=lambda item: item["coverage_score"], reverse=True)
    high_risk = [report for report in reports if report["risk_label"] == "High"]
    medium_risk = [report for report in reports if report["risk_label"] == "Medium"]
    lines.extend(
        [
            "## Product Notes",
            "",
            (
                "Profiles with the strongest MVP coverage are currently: "
                + ", ".join(escape(report["profile"]["display_name"]) for report in strongest[:3])
                + "."
            ),
            "",
        ]
    )
    if high_risk or medium_risk:
        lines.append(
            "Profiles with medium/high risk may need more evergreen guidance, clearer positioning, or additional sources before they feel compelling."
        )
    else:
        lines.append(
            "All evaluated profiles have enough deterministic match coverage for a useful MVP-style digest."
        )
    lines.extend(
        [
            "",
            "This report uses deterministic keyword/profile scoring, not true resume understanding. It should guide product prioritization, not determine user eligibility.",
            "",
        ]
    )


def product_insight(reports):
    low = sum(1 for report in reports if report["risk_label"] == "Low")
    medium = sum(1 for report in reports if report["risk_label"] == "Medium")
    high = sum(1 for report in reports if report["risk_label"] == "High")
    if high:
        return f"{low} low-risk, {medium} medium-risk, and {high} high-risk profiles; weakest profiles need guidance or more sources."
    if medium:
        return f"{low} low-risk and {medium} medium-risk profiles; coverage is useful but engagement may depend on recent movement."
    return f"All {len(reports)} profiles have low coverage risk under current scoring."


def coverage_note(report):
    if report["risk_label"] == "Low":
        return "Healthy opportunity density: enough active matches, source diversity, and recent movement to support ongoing engagement."
    if report["risk_label"] == "Medium":
        return "Current match inventory is usable, but engagement could weaken if recent movement is sparse or one source dominates."
    return "Thin current coverage: this profile likely needs evergreen/application guidance, broader positioning, or additional source coverage."


def unique(values):
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


if __name__ == "__main__":
    main()
