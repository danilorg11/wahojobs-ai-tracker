import argparse
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from applicant_signal_report import (
    DEFAULT_UPDATES_FILE,
    build_opportunity_signals,
    build_profile_signals,
    build_source_signals,
    build_summary,
    filter_updates,
    load_updates,
)
from profile_coverage_report import build_profile_coverage, coverage_note
from profile_match_digest import (
    console_text,
    escape,
    get_active_rows,
    get_post_baseline_new_rows,
    rank_opportunities,
    select_profiles,
)
from user_pipeline_digest import (
    DEFAULT_PIPELINE_FILE,
    build_pipeline_report,
    build_row_index,
    get_all_tracker_rows,
    get_recommendation_rows,
    load_pipeline,
    load_selected_profiles,
    match_key_from_match,
    status_group,
)
from wahojobs.classification import (
    INVENTORY_MODEL_EVERGREEN_APPLICATION,
    INVENTORY_MODEL_MIXED,
    INVENTORY_MODEL_PUBLIC_INVENTORY,
    MARKET_COUNT_POLICY_COUNT_LIVE,
)
from wahojobs.db.connection import get_connection
from wahojobs.reporting.market import get_market_size_summary


OUTPUT_PATH = Path("exports/product_demo_report.md")
DEFAULT_PROFILE_ID = "portuguese_english_reviewer"
RECENT_DAYS = 7
APPLICANT_SIGNAL_DAYS = 14
TOP_LIVE_LIMIT = 8
REPORT_SEPARATELY_LIMIT = 5
NEW_LIMIT = 5


def main():
    args = parse_args()
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    recent_cutoff = (generated_at - timedelta(days=RECENT_DAYS)).isoformat()
    applicant_cutoff = generated_at - timedelta(days=APPLICANT_SIGNAL_DAYS)

    profiles, profile_source = load_selected_profiles(args.profiles_file, None)
    profile = choose_profile(profiles, args.profile)
    pipeline_records, pipeline_source = load_pipeline(args.pipeline_file)
    updates, updates_source = load_updates(args.applicant_updates_file)

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
        recommendation_rows = get_recommendation_rows(conn)
        all_rows = get_all_tracker_rows(conn)
        new_rows = get_post_baseline_new_rows(conn, recent_cutoff)
        new_30d_rows = get_post_baseline_new_rows(
            conn,
            (generated_at - timedelta(days=30)).isoformat(),
        )

    row_index = build_row_index(all_rows)
    pipeline_report = build_pipeline_report(
        profile,
        pipeline_records,
        recommendation_rows,
        new_rows,
        row_index,
    )
    coverage_report = build_profile_coverage(
        profile,
        live_rows,
        evergreen_rows,
        public_rows,
        new_rows,
        new_30d_rows,
    )
    matches = build_matches(profile, live_rows, evergreen_rows, public_rows, new_rows)
    tracked = build_tracked_index(pipeline_report["records"])
    applicant_signals = build_relevant_applicant_signals(
        profile,
        updates,
        matches,
        pipeline_report,
        applicant_cutoff,
    )
    next_actions = build_demo_actions(pipeline_report, matches, tracked)

    markdown = render_markdown(
        generated_at,
        profile_source,
        pipeline_source,
        updates_source,
        market_summary,
        profile,
        coverage_report,
        matches,
        tracked,
        pipeline_report,
        next_actions,
        applicant_signals,
    )
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(markdown, encoding="utf-8")

    print("")
    print("Wahojobs Product Demo Report")
    print("============================")
    print(f"Generated: {generated_at.isoformat()} UTC")
    print(f"Profile: {profile['display_name']} ({profile['profile_id']})")
    print(
        "Estimated Live Market Opportunities: "
        f"{market_summary['estimated_market_opportunities']}"
    )
    print(f"Top live matches: {len(matches['live'])}")
    print(f"Pipeline items: {pipeline_report['summary']['total']}")
    print(f"Applicant signal updates used: {applicant_signals['summary']['total_updates']}")
    if next_actions:
        print(f"Top next action: {console_text(next_actions[0]['action'])}")
    print(f"Wrote Markdown report to {OUTPUT_PATH}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a read-only product demo report for one Wahojobs profile."
    )
    parser.add_argument(
        "--profile",
        help="Render one profile_id. Defaults to portuguese_english_reviewer when available.",
    )
    parser.add_argument(
        "--profiles-file",
        type=Path,
        help="Load editable profiles from a JSON file.",
    )
    parser.add_argument(
        "--pipeline-file",
        type=Path,
        default=DEFAULT_PIPELINE_FILE,
        help="Load mock user pipeline records from a JSON file.",
    )
    parser.add_argument(
        "--applicant-updates-file",
        type=Path,
        default=DEFAULT_UPDATES_FILE,
        help="Load mock applicant status updates from a JSON file.",
    )
    return parser.parse_args()


def choose_profile(profiles, profile_id):
    if profile_id:
        return select_profiles(profiles, profile_id)[0]
    preferred = [profile for profile in profiles if profile["profile_id"] == DEFAULT_PROFILE_ID]
    return preferred[0] if preferred else profiles[0]


def build_matches(profile, live_rows, evergreen_rows, public_rows, new_rows):
    return {
        "live": rank_opportunities(
            profile,
            live_rows,
            group_canonical=True,
            limit=TOP_LIVE_LIMIT,
            min_score=22,
        ),
        "new": rank_opportunities(
            profile,
            new_rows,
            group_canonical=True,
            limit=NEW_LIMIT,
            min_score=18,
        ),
        "evergreen": rank_opportunities(
            profile,
            evergreen_rows,
            group_canonical=False,
            limit=REPORT_SEPARATELY_LIMIT,
            min_score=18,
        ),
        "public": rank_opportunities(
            profile,
            public_rows,
            group_canonical=False,
            limit=REPORT_SEPARATELY_LIMIT,
            min_score=18,
        ),
    }


def build_tracked_index(records):
    by_key = {}
    by_source_title = {}
    for record in records:
        by_key[record["match_key"]] = record
        by_source_title[(normalize(record["source"]), normalize(record["title"]))] = record
    return {
        "by_key": by_key,
        "by_source_title": by_source_title,
    }


def tracked_record_for_match(match, tracked):
    key = match_key_from_match(match)
    if key in tracked["by_key"]:
        return tracked["by_key"][key]
    return tracked["by_source_title"].get(
        (normalize(match["source"]), normalize(match["display_title"]))
    )


def build_demo_actions(pipeline_report, matches, tracked):
    actions = list(pipeline_report["next_actions"][:6])

    for match in matches["live"]:
        if tracked_record_for_match(match, tracked):
            continue
        actions.append(
            {
                "priority": "high" if match["score"] >= 30 else "medium",
                "action": (
                    f"Apply to strong untracked match {match['display_title']} "
                    f"from {match['source']}."
                ),
                "title": match["display_title"],
                "source": match["source"],
            }
        )
        break

    for match in matches["new"]:
        if tracked_record_for_match(match, tracked):
            continue
        actions.append(
            {
                "priority": "medium",
                "action": (
                    f"Review new post-baseline match {match['display_title']} "
                    f"from {match['source']}."
                ),
                "title": match["display_title"],
                "source": match["source"],
            }
        )
        break

    for match in matches["evergreen"]:
        if tracked_record_for_match(match, tracked):
            continue
        actions.append(
            {
                "priority": "medium",
                "action": (
                    f"Revisit evergreen application {match['display_title']} "
                    f"from {match['source']}."
                ),
                "title": match["display_title"],
                "source": match["source"],
            }
        )
        break

    return unique_actions(actions)[:8]


def unique_actions(actions):
    seen = set()
    seen_targets = set()
    result = []
    priority_order = {"high": 0, "medium": 1, "low": 2}
    for action in sorted(actions, key=lambda item: priority_order.get(item["priority"], 1)):
        key = normalize(action["action"])
        target_key = (normalize(action.get("source")), normalize(action.get("title")))
        if key in seen or target_key in seen_targets:
            continue
        seen.add(key)
        seen_targets.add(target_key)
        result.append(action)
    return result


def build_relevant_applicant_signals(profile, updates, matches, pipeline_report, cutoff):
    relevant_sources = {
        match["source"]
        for bucket in matches.values()
        for match in bucket[:5]
    }
    relevant_sources.update(record["source"] for record in pipeline_report["records"])

    profile_updates = filter_updates(updates, profile_id=profile["profile_id"])
    source_updates = [
        update for update in updates
        if normalize(update["source"]) in {normalize(source) for source in relevant_sources}
    ]
    combined = []
    seen = set()
    for update in profile_updates + source_updates:
        if update["update_id"] in seen:
            continue
        seen.add(update["update_id"])
        combined.append(update)

    return {
        "summary": build_summary(combined, cutoff),
        "source_signals": build_source_signals(combined, cutoff)[:6],
        "opportunity_signals": build_opportunity_signals(combined, cutoff)[:8],
        "profile_signals": build_profile_signals(profile_updates, cutoff),
    }


def render_markdown(
    generated_at,
    profile_source,
    pipeline_source,
    updates_source,
    market_summary,
    profile,
    coverage_report,
    matches,
    tracked,
    pipeline_report,
    next_actions,
    applicant_signals,
):
    lines = [
        "# Wahojobs Product Demo Report",
        "",
        f"Generated: {generated_at.isoformat()} UTC",
        "",
        "This read-only demo shows how Wahojobs could feel for one selected user: matched opportunities, pipeline state, next actions, recent movement, evergreen applications, public inventory, and cautious applicant signals.",
        "",
        f"- Profile source: **{escape(profile_source)}**",
        f"- Pipeline source: **{escape(pipeline_source)}**",
        f"- Applicant signals source: **{escape(updates_source)}**",
        f"- Estimated Live Market Opportunities: **{market_summary['estimated_market_opportunities']}**",
        "- Public inventory, evergreen, mixed/report-separately, and experimental sources remain outside the live estimate.",
        "",
    ]

    append_user_summary(lines, profile, coverage_report)
    append_best_matches(lines, matches["live"], tracked)
    append_next_actions(lines, next_actions)
    append_pipeline_snapshot(lines, pipeline_report)
    append_new_since_baseline(lines, matches["new"], tracked)
    append_report_separate_matches(lines, "Evergreen / Always-Open Applications", matches["evergreen"], tracked)
    append_report_separate_matches(lines, "Public Inventory / Report-Separately Matches", matches["public"], tracked)
    append_applicant_signals(lines, applicant_signals)
    append_coverage_notes(lines, coverage_report)
    append_product_notes(lines)
    return "\n".join(lines)


def append_user_summary(lines, profile, coverage_report):
    lines.extend(
        [
            "## Demo User Summary",
            "",
            f"- Profile: **{escape(profile['display_name'])}** (`{profile['profile_id']}`)",
            f"- Summary: {escape(profile['summary'])}",
            f"- Education level: {escape(profile['education_level'])}",
            f"- Domains: {escape(join_values(profile['degrees_or_domains']))}",
            f"- Languages: {escape(join_values(profile['languages']))}",
            f"- Skills: {escape(join_values(profile['skills']))}",
            f"- Preferences: {escape(join_values(profile['work_preferences']))}",
            f"- Target opportunity types: {escape(join_values(profile['target_opportunity_types']))}",
            f"- Coverage risk label: **{coverage_report['risk_label']}**",
            "",
        ]
    )


def append_best_matches(lines, matches, tracked):
    lines.extend(
        [
            "## Today's Best Matches",
            "",
            "| Score | Title | Source | Location | Expertise/Department | Pipeline | Reasons | URL |",
            "| ---: | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    if not matches:
        lines.append("| 0 | No strong live matches found | - | - | - | - | - | |")
    for match in matches[:TOP_LIVE_LIMIT]:
        record = tracked_record_for_match(match, tracked)
        lines.append(
            "| "
            f"{match['score']} | "
            f"{escape(match['display_title'])} | "
            f"{escape(match['source'])} | "
            f"{escape(match['location'])} | "
            f"{escape(match['expertise'])} | "
            f"{escape(pipeline_label(record))} | "
            f"{escape('; '.join(match['reasons'][:3]))} | "
            f"[Open]({match['url']}) |"
        )
    lines.append("")


def append_next_actions(lines, actions):
    lines.extend(["## Recommended Next Actions", ""])
    if not actions:
        lines.extend(["No immediate specific action found for this sample user.", ""])
        return
    for action in actions[:8]:
        lines.append(
            f"- **{escape(action['priority'].title())}**: {escape(action['action'])}"
        )
    lines.append("")


def append_pipeline_snapshot(lines, pipeline_report):
    summary = pipeline_report["summary"]
    grouped = Counter(status_group(record["status"]) for record in pipeline_report["records"])
    lines.extend(
        [
            "## Pipeline Snapshot",
            "",
            f"- Total tracked: **{summary['total']}**",
            f"- Saved: **{summary['saved']}**",
            f"- Applied: **{summary['applied']}**",
            f"- Assessment-related: **{summary['assessment']}**",
            f"- Waiting: **{summary['waiting']}**",
            f"- Accepted/active or rejected: **{summary['accepted_rejected']}**",
            f"- Not interested: **{summary['not_interested']}**",
            f"- Expired: **{grouped['Expired']}**",
            "",
        ]
    )
    append_pipeline_items(lines, pipeline_report["records"])


def append_pipeline_items(lines, records):
    lines.extend(
        [
            "| Status | Title | Source | Availability | Score | Next Action |",
            "| --- | --- | --- | --- | ---: | --- |",
        ]
    )
    if not records:
        lines.append("| None | No tracked pipeline items | - | - | - | - |")
    for record in sorted(records, key=pipeline_sort_key)[:8]:
        score = record["match_score"] if record["match_score"] is not None else "-"
        lines.append(
            "| "
            f"{escape(record['status'])} | "
            f"{escape(record['title'])} | "
            f"{escape(record['source'])} | "
            f"{escape(record['availability'])} | "
            f"{score} | "
            f"{escape(record['next_action'])} |"
        )
    lines.append("")


def append_new_since_baseline(lines, matches, tracked):
    lines.extend(["## New Since Baseline", ""])
    if not matches:
        lines.extend(
            [
                "Few or no post-baseline new matches were found for this profile in the recent window. That can mean the market is quiet, or simply that current high-fit inventory is already known.",
                "",
            ]
        )
        return
    append_match_cards(lines, matches, tracked, include_live=True)


def append_report_separate_matches(lines, title, matches, tracked):
    lines.extend([f"## {title}", ""])
    if not matches:
        lines.extend(["No relevant report-separately matches found for this profile.", ""])
        return
    if title.startswith("Evergreen"):
        lines.append("These are application surfaces or always-open paths, not live postings in the market estimate.")
    else:
        lines.append("These are useful public or mixed-source leads, but they are reported separately from Estimated Live Market Opportunities.")
    lines.append("")
    append_match_cards(lines, matches, tracked, include_live=False)


def append_match_cards(lines, matches, tracked, include_live):
    lines.extend(
        [
            "| Score | Title | Source | Kind | Pipeline | URL |",
            "| ---: | --- | --- | --- | --- | --- |",
        ]
    )
    for match in matches[:REPORT_SEPARATELY_LIMIT]:
        record = tracked_record_for_match(match, tracked)
        live_label = "live estimate" if match["include_in_live_market_estimate"] else "separate"
        kind = f"{match['opportunity_kind']} / {match['availability_basis']}"
        if include_live:
            kind = f"{kind} ({live_label})"
        lines.append(
            "| "
            f"{match['score']} | "
            f"{escape(match['display_title'])} | "
            f"{escape(match['source'])} | "
            f"{escape(kind)} | "
            f"{escape(pipeline_label(record))} | "
            f"[Open]({match['url']}) |"
        )
    lines.append("")


def append_applicant_signals(lines, applicant_signals):
    summary = applicant_signals["summary"]
    lines.extend(
        [
            "## Applicant Signals Relevant to This User",
            "",
            (
                "These are mock applicant updates from similar profiles or relevant sources. "
                "They are directional product signals, not guarantees of assessment, acceptance, or paid work."
            ),
            "",
            f"- Relevant mock updates: **{summary['total_updates']}**",
            f"- Recent updates in window: **{summary['recent_updates']}**",
            f"- Assessment-related updates: **{summary['assessment_updates']}**",
            f"- Accepted/active/paid-task updates: **{summary['success_updates']}**",
            f"- Waiting/no-response updates: **{summary['waiting_updates']}**",
            "",
        ]
    )
    append_signal_table(lines, applicant_signals["source_signals"])
    append_opportunity_signal_list(lines, applicant_signals["opportunity_signals"])


def append_signal_table(lines, rows):
    lines.extend(
        [
            "| Source | Reports | Recent | Assessments | Accepted/Active/Paid | Waiting/No-response | Signal |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    if not rows:
        lines.append("| None | 0 | 0 | 0 | 0 | 0 | No relevant applicant signals |")
    for row in rows[:6]:
        lines.append(
            "| "
            f"{escape(row['source'])} | "
            f"{row['reports']} | "
            f"{row['recent_reports']} | "
            f"{row['assessment_reports']} | "
            f"{row['success_reports']} | "
            f"{row['waiting_reports']} | "
            f"{escape(row['signal_label'])} |"
        )
    lines.append("")


def append_opportunity_signal_list(lines, rows):
    lines.extend(["### Opportunity Signal Examples", ""])
    if not rows:
        lines.extend(["No opportunity-level signal examples for this profile/source set.", ""])
        return
    for row in rows[:5]:
        url = f" ([Open]({row['url']}))" if row["url"] else ""
        lines.append(
            f"- {escape(row['source'])}: {escape(row['title'])} - "
            f"{escape(row['signal_label'])}; confidence {escape(row['confidence_label'])}.{url}"
        )
    lines.append("")


def append_coverage_notes(lines, coverage_report):
    lines.extend(
        [
            "## Coverage / Retention Notes",
            "",
            f"- Coverage risk: **{coverage_report['risk_label']}**",
            f"- Strong / medium / weak live matches: **{coverage_report['strong_live']} / {coverage_report['medium_live']} / {coverage_report['weak_live']}**",
            f"- Evergreen / public-separate matches: **{coverage_report['evergreen']} / {coverage_report['public']}**",
            f"- Post-baseline new matches: **{coverage_report['new_7d']} in 7d; {coverage_report['new_30d']} in 30d**",
            f"- Source diversity: **{coverage_report['source_diversity']}** sources",
            "",
            coverage_note(coverage_report),
            "",
        ]
    )


def append_product_notes(lines):
    lines.extend(
        [
            "## Product Notes",
            "",
            "- This is a mock read-only product demo; it does not update profiles, pipeline records, applicant signals, or tracker database rows.",
            "- Profile, pipeline, and applicant status data are sample data.",
            "- Matching is deterministic keyword/profile scoring, not true resume understanding and not an eligibility decision.",
            "- A future app could let users edit profiles, update statuses, hide irrelevant roles, set reminders, and contribute anonymous aggregate applicant signals.",
            "",
        ]
    )


def pipeline_label(record):
    if not record:
        return "not tracked"
    return f"{record['status']} ({record['availability']})"


def pipeline_sort_key(record):
    group_order = {
        "Assessment": 0,
        "Waiting": 1,
        "Applied": 2,
        "Saved": 3,
        "Recommended / Remind later": 4,
        "Accepted / Active": 5,
        "Rejected / Not interested": 6,
        "Expired": 7,
    }
    score = record["match_score"] if record["match_score"] is not None else 0
    return (group_order.get(status_group(record["status"]), 9), -score, record["source"], record["title"])


def join_values(values):
    return ", ".join(values) if values else "None specified"


def normalize(value):
    return str(value or "").strip().lower()


if __name__ == "__main__":
    main()
