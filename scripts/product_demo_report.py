import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

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
    parse_datetime,
)
from profile_coverage_report import build_profile_coverage
from profile_match_digest import (
    console_text,
    escape,
    get_active_rows,
    get_post_baseline_new_rows,
    load_profiles,
    normalize_profile,
    rank_opportunities,
    select_profiles,
)
from user_pipeline_digest import (
    DEFAULT_PIPELINE_FILE,
    build_pipeline_report,
    build_row_index,
    days_since,
    get_all_tracker_rows,
    get_recommendation_rows,
    load_pipeline,
    load_selected_profiles,
    match_key_from_match,
    priority_rank,
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
MATCH_POOL_LIMIT = 40
PRIMARY_ACTION_LIMIT = 4
PRIMARY_URGENT_LIMIT = 2
PRIMARY_APPLY_LIMIT = 2
PRIMARY_PASSIVE_LIMIT = 1
PRIMARY_EVERGREEN_LIMIT = 1
BACKLOG_ACTION_LIMIT = 10
STALE_ASSESSMENT_DAYS = 7
STALE_PASSIVE_DAYS = 14
SUPPRESSED_ACTION_STATUSES = {
    "not_interested",
    "rejected",
    "expired",
    "accepted",
    "active_worker",
    "paid_task_received",
}

EXPLICIT_LANGUAGES = {
    "arabic",
    "bengali",
    "catalan",
    "chinese",
    "czech",
    "danish",
    "dutch",
    "english",
    "finnish",
    "french",
    "german",
    "greek",
    "gujarati",
    "hebrew",
    "hindi",
    "italian",
    "japanese",
    "kannada",
    "khmer",
    "kiswahili",
    "korean",
    "norwegian",
    "polish",
    "portuguese",
    "romanian",
    "spanish",
    "swedish",
    "swahili",
    "thai",
    "turkish",
    "ukrainian",
    "vietnamese",
}

LANGUAGE_ALIASES = {
    "brazilian": "portuguese",
    "brazil": "portuguese",
    "portugal": "portuguese",
    "español": "spanish",
    "français": "french",
}


def main():
    args = parse_args()
    context = build_demo_context(
        profile_id=args.profile,
        use_product_state=args.use_product_state,
        profiles_file=args.profiles_file,
        pipeline_file=args.pipeline_file,
        applicant_updates_file=args.applicant_updates_file,
    )

    markdown = render_markdown(
        context["generated_at"],
        context["profile_source"],
        context["pipeline_source"],
        context["updates_source"],
        context["market_summary"],
        context["profile"],
        context["coverage_report"],
        context["matches"],
        context["tracked"],
        context["pipeline_report"],
        context["next_actions"],
        context["also_worth_reviewing"],
        context["applicant_signals"],
    )
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(markdown, encoding="utf-8")

    print("")
    print("Wahojobs User Demo")
    print("==================")
    print(f"Generated: {context['generated_at'].isoformat()} UTC")
    print(f"Profile: {context['profile']['display_name']} ({context['profile']['profile_id']})")
    print(f"Live opportunities tracked: {context['market_summary']['estimated_market_opportunities']}")
    print(f"Top live matches: {len(context['matches']['live'])}")
    print(f"Pipeline items: {context['pipeline_report']['summary']['total']}")
    print(f"Applicant signal updates used: {context['applicant_signals']['summary']['total_updates']}")
    if context["next_actions"]:
        print(f"Top next action: {console_text(context['next_actions'][0]['action'])}")
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
    parser.add_argument(
        "--use-product-state",
        action="store_true",
        help="Load profiles, pipeline items, and applicant updates from SQLite product-state tables.",
    )
    return parser.parse_args()


def build_demo_context(
    profile_id=None,
    use_product_state=False,
    profiles_file=None,
    pipeline_file=DEFAULT_PIPELINE_FILE,
    applicant_updates_file=DEFAULT_UPDATES_FILE,
):
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    recent_cutoff = (generated_at - timedelta(days=RECENT_DAYS)).isoformat()
    applicant_cutoff = generated_at - timedelta(days=APPLICANT_SIGNAL_DAYS)

    if use_product_state:
        profiles, profile_source = load_product_state_profiles()
        pipeline_records, pipeline_source = load_product_state_pipeline()
        updates, updates_source = load_product_state_updates()
    else:
        profiles, profile_source = load_selected_profiles(profiles_file, None)
        pipeline_records, pipeline_source = load_pipeline(pipeline_file)
        updates, updates_source = load_updates(applicant_updates_file)

    profile = choose_profile(profiles, profile_id)

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
    action_plan = build_demo_action_plan(profile, pipeline_report, matches, tracked)

    return {
        "generated_at": generated_at,
        "profile_source": profile_source,
        "pipeline_source": pipeline_source,
        "updates_source": updates_source,
        "market_summary": market_summary,
        "profile": profile,
        "coverage_report": coverage_report,
        "matches": matches,
        "tracked": tracked,
        "pipeline_report": pipeline_report,
        "next_actions": action_plan["primary"],
        "also_worth_reviewing": action_plan["secondary"],
        "applicant_signals": applicant_signals,
    }


def load_product_state_profiles():
    with get_connection() as conn:
        ensure_product_state_ready(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM user_profiles
            ORDER BY profile_id
            """
        ).fetchall()
    built_in_profiles = {
        profile["profile_id"]: profile
        for profile in load_profiles(None)[0]
    }
    profiles = [
        normalize_product_state_profile(row, built_in_profiles)
        for row in rows
    ]
    return profiles, "SQLite product-state user_profiles"


def normalize_product_state_profile(row, built_in_profiles):
    profile = normalize_profile(
        {
            "profile_id": row["profile_id"],
            "display_name": row["display_name"],
            "summary": row["notes"] or "",
            "education_level": row["education_level"] or "not_specified",
            "degrees_or_domains": loads_list(row["degrees_or_domains_json"]),
            "languages": loads_list(row["languages_json"]),
            "skills": loads_list(row["skills_json"]),
            "work_preferences": loads_list(row["work_preferences_json"]),
            "constraints": loads_list(row["constraints_json"]),
            "target_opportunity_types": loads_list(row["target_opportunity_types_json"]),
            "notes": row["notes"] or "",
        },
        source=f"product-state profile {row['profile_id']}",
    )
    built_in = built_in_profiles.get(row["profile_id"])
    if built_in is not None:
        profile["signals"] = built_in["signals"]
        profile["avoid_keywords"] = built_in.get("avoid_keywords", [])
    return profile


def load_product_state_pipeline():
    with get_connection() as conn:
        ensure_product_state_ready(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM user_pipeline_items
            ORDER BY id
            """
        ).fetchall()
    records = [
        {
            "id": row["id"],
            "pipeline_item_id": row["pipeline_item_id"],
            "profile_id": row["profile_id"],
            "source": row["source"],
            "title": row["opportunity_title"],
            "url": row["opportunity_url"] or "",
            "status": row["status"],
            "status_date": row["status_date"] or "",
            "notes": row["notes"] or "",
            "user_priority": row["user_priority"] or "medium",
            "reminder_date": row["reminder_date"] or "",
            "last_user_action": row["last_user_action"] or "",
        }
        for row in rows
    ]
    return records, "SQLite product-state user_pipeline_items"


def load_product_state_updates():
    with get_connection() as conn:
        ensure_product_state_ready(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM applicant_status_updates
            ORDER BY reported_at, update_id
            """
        ).fetchall()
    updates = [
        {
            "update_id": row["update_id"],
            "user_id": row["user_id"] or row["anonymous_user_key"] or "",
            "profile_id": row["profile_id"],
            "source": row["source"],
            "opportunity_title": row["opportunity_title"],
            "opportunity_url": row["opportunity_url"] or "",
            "opportunity_id": row["opportunity_external_id"] or "",
            "status": row["status"],
            "previous_status": row["previous_status"] or "",
            "status_date": row["status_date"],
            "reported_at": row["reported_at"],
            "reported_at_dt": parse_datetime(row["reported_at"], row["update_id"]),
            "evidence_type": row["evidence_type"],
            "confidence_level": row["confidence_level"],
            "notes": row["notes"] or "",
        }
        for row in rows
    ]
    return updates, "SQLite product-state applicant_status_updates"


def ensure_product_state_ready(conn):
    tables = {
        "user_profiles": "python scripts/product_state.py import-profiles profiles/sample_profiles.json",
        "user_pipeline_items": "python scripts/product_state.py import-pipeline profiles/sample_user_pipeline.json",
        "applicant_status_updates": "python scripts/product_state.py import-applicant-updates profiles/sample_applicant_updates.json",
    }
    missing = []
    empty = []
    for table, command in tables.items():
        if not table_exists(conn, table):
            missing.append(table)
            continue
        count = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
        if count == 0:
            empty.append(table)
    if not missing and not empty:
        return

    details = []
    if missing:
        details.append("missing tables: " + ", ".join(missing))
    if empty:
        details.append("empty tables: " + ", ".join(empty))
    raise SystemExit(
        "SQLite product-state data is not ready ("
        + "; ".join(details)
        + ").\n\n"
        "Import the sample product state first:\n"
        "  python scripts/product_state.py import-profiles profiles/sample_profiles.json\n"
        "  python scripts/product_state.py import-pipeline profiles/sample_user_pipeline.json\n"
        "  python scripts/product_state.py import-applicant-updates profiles/sample_applicant_updates.json"
    )


def table_exists(conn, table):
    return conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table,),
    ).fetchone() is not None


def loads_list(value):
    try:
        loaded = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return loaded if isinstance(loaded, list) else []


def choose_profile(profiles, profile_id):
    if profile_id:
        return select_profiles(profiles, profile_id)[0]
    preferred = [profile for profile in profiles if profile["profile_id"] == DEFAULT_PROFILE_ID]
    return preferred[0] if preferred else profiles[0]


def build_matches(profile, live_rows, evergreen_rows, public_rows, new_rows):
    live_matches = rank_opportunities(
        profile,
        live_rows,
        group_canonical=True,
        limit=MATCH_POOL_LIMIT,
        min_score=22,
    )
    new_matches = rank_opportunities(
        profile,
        new_rows,
        group_canonical=True,
        limit=MATCH_POOL_LIMIT,
        min_score=18,
    )
    evergreen_matches = rank_opportunities(
        profile,
        evergreen_rows,
        group_canonical=False,
        limit=MATCH_POOL_LIMIT,
        min_score=18,
    )
    public_matches = rank_opportunities(
        profile,
        public_rows,
        group_canonical=False,
        limit=MATCH_POOL_LIMIT,
        min_score=18,
    )
    return {
        "live": user_relevant_matches(profile, live_matches)[:TOP_LIVE_LIMIT],
        "new": user_relevant_matches(profile, new_matches)[:NEW_LIMIT],
        "evergreen": user_relevant_matches(profile, evergreen_matches)[:REPORT_SEPARATELY_LIMIT],
        "public": user_relevant_matches(profile, public_matches)[:REPORT_SEPARATELY_LIMIT],
    }


def build_tracked_index(records):
    by_key = {}
    by_source_title = {}
    by_source_near_title = {}
    for record in records:
        by_key[record["match_key"]] = record
        by_source_title[(normalize(record["source"]), normalize(record["title"]))] = record
        by_source_near_title[
            (normalize(record["source"]), normalize_action_target(record["title"]))
        ] = record
    return {
        "by_key": by_key,
        "by_source_title": by_source_title,
        "by_source_near_title": by_source_near_title,
    }


def tracked_record_for_match(match, tracked):
    key = match_key_from_match(match)
    if key in tracked["by_key"]:
        return tracked["by_key"][key]
    exact = tracked["by_source_title"].get(
        (normalize(match["source"]), normalize(match["display_title"]))
    )
    if exact:
        return exact
    return tracked.get("by_source_near_title", {}).get(
        (normalize(match["source"]), normalize_action_target(match["display_title"]))
    )


def build_demo_action_plan(profile, pipeline_report, matches, tracked):
    candidates = unique_actions(build_demo_action_candidates(profile, pipeline_report, matches, tracked))
    primary = []
    secondary = []
    category_counts = Counter()

    for action in sorted(candidates, key=action_sort_key):
        if can_select_primary_action(action, primary, category_counts):
            primary.append(action)
            category_counts[action["category"]] += 1
        else:
            secondary.append(action)

    return {
        "primary": primary[:PRIMARY_ACTION_LIMIT],
        "secondary": secondary[:BACKLOG_ACTION_LIMIT],
    }


def build_demo_action_candidates(profile, pipeline_report, matches, tracked):
    actions = []

    for record in pipeline_report["records"]:
        status = record["status"]
        if status in SUPPRESSED_ACTION_STATUSES:
            continue
        if not record["next_action"] or has_unrelated_language_text(profile, record["next_action"]):
            continue
        category = action_category_for_record(record)
        actions.append(
            {
                "priority": action_priority_for_category(category, record.get("user_priority")),
                "category": category,
                "action": record["next_action"],
                "title": record["title"],
                "source": record["source"],
                "score": record["match_score"] or 0,
            }
        )

    for match in matches["live"]:
        if tracked_record_for_match(match, tracked):
            continue
        category = "apply" if match["score"] >= 30 else "review"
        actions.append(
            {
                "priority": "high" if category == "apply" else "medium",
                "category": category,
                "action": (
                    f"Apply to {match['display_title']} "
                    f"from {match['source']}."
                ) if category == "apply" else (
                    f"Review {match['display_title']} "
                    f"from {match['source']}."
                ),
                "title": match["display_title"],
                "source": match["source"],
                "score": match["score"],
            }
        )

    for match in matches["new"]:
        if tracked_record_for_match(match, tracked):
            continue
        actions.append(
            {
                "priority": "medium",
                "category": "review",
                "action": (
                    f"Review new match {match['display_title']} "
                    f"from {match['source']}."
                ),
                "title": match["display_title"],
                "source": match["source"],
                "score": match["score"],
            }
        )

    for match in matches["evergreen"]:
        if tracked_record_for_match(match, tracked):
            continue
        actions.append(
            {
                "priority": "medium",
                "category": "evergreen",
                "action": (
                    f"Revisit always-open application {match['display_title']} "
                    f"from {match['source']}."
                ),
                "title": match["display_title"],
                "source": match["source"],
                "score": match["score"],
            }
        )

    return actions


def action_category_for_record(record):
    status = record["status"]
    if status in {"assessment_invited", "assessment_started"}:
        return "urgent_assessment"
    if status == "assessment_completed":
        age = days_since(record["status_date"]) or 0
        return "passive_followup" if age >= STALE_ASSESSMENT_DAYS else "backlog"
    if status == "saved":
        if not saved_item_ready_for_primary_action(record):
            return "backlog"
        if "apply" in normalize_text(record["next_action"]) and (record["match_score"] or 0) >= 30:
            return "apply"
        return "backlog"
    if status == "remind_later":
        if not reminder_is_due(record):
            return "backlog"
        if "apply" in normalize_text(record["next_action"]) and (record["match_score"] or 0) >= 30:
            return "apply"
        return "review"
    if status == "recommended":
        if "apply" in normalize_text(record["next_action"]) and (record["match_score"] or 0) >= 25:
            return "apply"
        return "review"
    if status in {"waiting", "applied"}:
        age = days_since(record["status_date"]) or 0
        return "passive_followup" if age >= STALE_PASSIVE_DAYS else "backlog"
    if record["availability"] == "inactive/removed":
        return "backlog"
    return "review"


def saved_item_ready_for_primary_action(record):
    age = days_since(record["status_date"])
    return age is not None and age > 0


def reminder_is_due(record):
    age = days_since(record.get("reminder_date"))
    return age is not None and age >= 0


def action_priority_for_category(category, fallback=None):
    if category == "urgent_assessment":
        return "high"
    if category == "apply":
        return "high"
    if category in {"review", "evergreen"}:
        return "medium"
    if category == "passive_followup":
        return "medium"
    return fallback or "low"


def action_sort_key(action):
    category_order = {
        "urgent_assessment": 0,
        "apply": 1,
        "review": 2,
        "evergreen": 3,
        "passive_followup": 4,
        "backlog": 5,
    }
    return (
        category_order.get(action.get("category"), 9),
        priority_rank(action.get("priority")),
        -(action.get("score") or 0),
        normalize(action.get("source")),
        normalize(action.get("title")),
    )


def can_select_primary_action(action, primary, category_counts):
    category = action.get("category")
    if len(primary) >= PRIMARY_ACTION_LIMIT:
        return False
    if category == "urgent_assessment":
        return category_counts[category] < PRIMARY_URGENT_LIMIT
    if category == "apply":
        return category_counts[category] < PRIMARY_APPLY_LIMIT
    if category == "passive_followup":
        return category_counts[category] < PRIMARY_PASSIVE_LIMIT
    if category == "evergreen":
        return category_counts[category] < PRIMARY_EVERGREEN_LIMIT
    if category == "review":
        return True
    return False


def unique_actions(actions):
    seen = set()
    seen_targets = set()
    seen_near_targets = set()
    result = []
    priority_order = {"high": 0, "medium": 1, "low": 2}
    for action in sorted(actions, key=lambda item: priority_order.get(item["priority"], 1)):
        key = normalize(action["action"])
        target_key = (normalize(action.get("source")), normalize(action.get("title")))
        near_target_key = action_near_target_key(action)
        if (
            key in seen
            or target_key in seen_targets
            or near_target_key in seen_near_targets
        ):
            continue
        seen.add(key)
        seen_targets.add(target_key)
        seen_near_targets.add(near_target_key)
        result.append(action)
    return result


def action_near_target_key(action):
    source = normalize(action.get("source"))
    title = normalize_action_target(action.get("title") or action.get("action"))
    return (source, title)


def normalize_action_target(value):
    text = normalize_text(value)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(
        r"\b(apply|review|revisit|continue|watch|new|match|from|for|the|an|a|to)\b",
        " ",
        text,
    )
    text = re.sub(r"\b(rater|reviewer|rating|reviewing)\b", " ", text)
    text = re.sub(r"\b(project|role|opportunity|application)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


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
        and update_relevant_to_profile(update, profile)
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
        "opportunity_signals": build_opportunity_signals(
            [update for update in combined if update_relevant_to_profile(update, profile)],
            cutoff,
        )[:8],
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
    also_worth_reviewing,
    applicant_signals,
):
    lines = [
        "# Your Wahojobs Demo",
        "",
        f"Generated: {generated_at.isoformat()} UTC",
        "",
        "A focused daily view of your best AI-work leads, what is already in motion, and what to do next.",
        "",
    ]

    append_user_summary(lines, profile, coverage_report)
    append_do_these_first(lines, next_actions)
    append_also_worth_reviewing(lines, also_worth_reviewing)
    append_best_matches(lines, matches["live"], tracked)
    append_pipeline_snapshot(lines, pipeline_report)
    append_new_since_baseline(lines, matches["new"], tracked)
    append_report_separate_matches(lines, "Always-Open Applications", matches["evergreen"], tracked)
    append_report_separate_matches(lines, "Other Leads", matches["public"], tracked)
    append_applicant_signals(lines, applicant_signals)
    append_coverage_notes(lines, coverage_report)
    append_product_notes(lines, market_summary)
    return "\n".join(lines)


def append_user_summary(lines, profile, coverage_report):
    lines.extend(
        [
            "## Demo User Summary",
            "",
            f"- Profile: **{escape(profile['display_name'])}** (`{profile['profile_id']}`)",
            f"- What you are looking for: {escape(profile['summary'])}",
            f"- Background: {escape(join_values(profile['degrees_or_domains']))}",
            f"- Languages: {escape(join_values(profile['languages']))}",
            f"- Skills: {escape(join_values(profile['skills']))}",
            f"- Preferences: {escape(join_values(profile['work_preferences']))}",
            f"- Current fit: **{profile_strength_label(coverage_report)}**",
            "",
        ]
    )


def append_do_these_first(lines, actions):
    lines.extend(["## Do These First", ""])
    if not actions:
        lines.extend(
            [
                "No urgent new applications today. We'll keep watching for strong matches.",
                "",
            ]
        )
        return
    lines.append("A short daily plan. You do not need to act on everything in the tracker today.")
    lines.append("")
    for action in actions[:PRIMARY_ACTION_LIMIT]:
        lines.append(f"- {escape(make_action_user_facing(action['action']))}")
    lines.append("")


def append_also_worth_reviewing(lines, actions):
    lines.extend(["## Also Worth Reviewing", ""])
    lines.append("Good matches, but not today's top priority. Review these when you have more time.")
    lines.append("")
    if not actions:
        lines.extend(["No additional backlog items surfaced for this profile today.", ""])
        return
    for action in actions[:6]:
        lines.append(f"- {escape(make_action_user_facing(action['action']))}")
    lines.append("")


def append_best_matches(lines, matches, tracked):
    lines.extend(
        [
            "## Today's Best Matches",
            "",
            "| Match strength | Title | Source | Location | Area | Status | Why it matched you | URL |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    if not matches:
        lines.append("| - | No strong live opportunities found today | - | - | - | - | - | |")
    for match in matches[:TOP_LIVE_LIMIT]:
        record = tracked_record_for_match(match, tracked)
        lines.append(
            "| "
            f"{match_strength(match)} | "
            f"{escape(match['display_title'])} | "
            f"{escape(match['source'])} | "
            f"{escape(match['location'])} | "
            f"{escape(match['expertise'])} | "
            f"{escape(pipeline_label(record))} | "
            f"{escape('; '.join(plain_reasons(match, record)[:3]))} | "
            f"[Open]({match['url']}) |"
        )
    lines.append("")


def append_pipeline_snapshot(lines, pipeline_report):
    summary = pipeline_report["summary"]
    grouped = Counter(status_group(record["status"]) for record in pipeline_report["records"])
    lines.extend(
        [
            "## Your Application Tracker",
            "",
            f"- Total tracked: **{summary['total']}**",
            f"- Saved: **{summary['saved']}**",
            f"- Applied: **{summary['applied']}**",
            f"- Assessments in progress or recently completed: **{summary['assessment']}**",
            f"- Waiting: **{summary['waiting']}**",
            f"- Accepted, active, or rejected: **{summary['accepted_rejected']}**",
            f"- Not interested: **{summary['not_interested']}**",
            f"- Expired: **{grouped['Expired']}**",
            "",
        ]
    )
    append_pipeline_items(lines, pipeline_report["records"])


def append_pipeline_items(lines, records):
    lines.extend(
        [
            "| Status | Title | Source | Availability | Match strength | Suggested next step |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    if not records:
        lines.append("| None | No tracked pipeline items | - | - | - | - |")
    for record in sorted(records, key=pipeline_sort_key)[:8]:
        score = record["match_score"] if record["match_score"] is not None else "-"
        lines.append(
            "| "
            f"{escape(readable_status(record['status']))} | "
            f"{escape(record['title'])} | "
            f"{escape(record['source'])} | "
            f"{escape(readable_availability(record['availability']))} | "
            f"{match_strength_from_score(score)} | "
            f"{escape(make_action_user_facing(record['next_action']))} |"
        )
    lines.append("")


def append_new_since_baseline(lines, matches, tracked):
    lines.extend(["## New Matches This Week", ""])
    if not matches:
        lines.extend(
            [
                "Few or no newly discovered matches stood out for this profile this week. Your existing strong matches may still be the best place to focus.",
                "",
            ]
        )
        return
    append_match_cards(lines, matches, tracked, include_live=True)


def append_report_separate_matches(lines, title, matches, tracked):
    lines.extend([f"## {title}", ""])
    if not matches:
        lines.extend(["No especially relevant leads found here today.", ""])
        return
    if title.startswith("Always"):
        lines.append("These are broad application paths that may stay open over time. They are useful, but they are not fresh live postings.")
    else:
        lines.append("These are useful public leads or broader opportunity pages. Review them as supplemental options.")
    lines.append("")
    append_match_cards(lines, matches, tracked, include_live=False)


def append_match_cards(lines, matches, tracked, include_live):
    lines.extend(
        [
            "| Match strength | Title | Source | Type | Status | URL |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for match in matches[:REPORT_SEPARATELY_LIMIT]:
        record = tracked_record_for_match(match, tracked)
        lines.append(
            "| "
            f"{match_strength(match)} | "
            f"{escape(match['display_title'])} | "
            f"{escape(match['source'])} | "
            f"{escape(opportunity_type_label(match))} | "
            f"{escape(pipeline_label(record))} | "
            f"[Open]({match['url']}) |"
        )
    lines.append("")


def append_applicant_signals(lines, applicant_signals):
    summary = applicant_signals["summary"]
    lines.extend(
        [
            "## Applicant Signals",
            "",
            (
                "These sample signals show what similar users have reported on relevant sources. "
                "Treat them as directional, not as guarantees."
            ),
            "",
            f"- Relevant sample reports: **{summary['total_updates']}**",
            f"- Recent reports: **{summary['recent_updates']}**",
            f"- Assessment activity reported: **{summary['assessment_updates']}**",
            f"- Accepted/active/paid-task reports: **{summary['success_updates']}**",
            f"- Waiting/no-response reports: **{summary['waiting_updates']}**",
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
            f"{escape(readable_signal(row['signal_label']))} |"
        )
    lines.append("")


def append_opportunity_signal_list(lines, rows):
    lines.extend(["### Relevant Examples", ""])
    if not rows:
        lines.extend(["Limited relevant applicant signals are available for this profile today.", ""])
        return
    for row in rows[:5]:
        url = f" ([Open]({row['url']}))" if row["url"] else ""
        lines.append(
            f"- {escape(row['source'])}: {escape(row['title'])} - "
            f"{escape(readable_signal(row['signal_label']))}; confidence {escape(row['confidence_label'])}.{url}"
        )
    lines.append("")


def append_coverage_notes(lines, coverage_report):
    lines.extend(
        [
            "## How Strong Is This Profile Right Now?",
            "",
            f"- Overall fit: **{profile_strength_label(coverage_report)}**",
            f"- Strong matches available now: **{coverage_report['strong_live']}**",
            f"- Good backup matches: **{coverage_report['medium_live']}**",
            f"- New relevant matches this week: **{coverage_report['new_7d']}**",
            f"- Always-open or other supplemental leads: **{coverage_report['evergreen'] + coverage_report['public']}**",
            "",
            user_coverage_note(coverage_report),
            "",
        ]
    )


def append_product_notes(lines, market_summary):
    lines.extend(
        [
            "## About This Demo",
            "",
            "- This is a mock, read-only demo. It does not apply to jobs, update your tracker, or change any stored data.",
            "- Profile, application tracker, and applicant signal examples are sample data.",
            "- Matches are based on visible profile keywords and opportunity text. They are suggestions to review, not eligibility decisions.",
            "- Wahojobs is currently tracking "
            f"**{market_summary['estimated_market_opportunities']}** active live opportunities in its current market database.",
            "- A future app could let users edit profiles, update statuses, hide irrelevant roles, set reminders, and contribute anonymous aggregate applicant signals.",
            "",
        ]
    )


def pipeline_label(record):
    if not record:
        return "Not tracked yet"
    return f"{readable_status(record['status'])} ({readable_availability(record['availability'])})"


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


def escape(value):
    return clean_display_text(value).replace("|", "\\|").replace("\n", " ")


def clean_display_text(value):
    text = str(value or "")
    replacements = {
        "â€“": "-",
        "â€”": "-",
        "â€™": "'",
        "â€œ": '"',
        "â€": '"',
        "Â": "",
        "�": "-",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def user_relevant_matches(profile, matches):
    return [
        match for match in matches
        if not has_unrelated_explicit_language(profile, match)
    ]


def update_relevant_to_profile(update, profile):
    text = " ".join(
        str(update.get(field) or "")
        for field in ("source", "opportunity_title", "notes")
    )
    if has_unrelated_language_text(profile, text):
        return False
    update_text = normalize_text(text)
    if update["profile_id"] == profile["profile_id"]:
        return True
    for _, keywords, _ in profile["signals"]:
        if any(keyword_in_text(update_text, keyword) for keyword in keywords):
            return True
    return any(
        keyword_in_text(update_text, language)
        for language in profile.get("languages", [])
    )


def has_unrelated_explicit_language(profile, match):
    text = " ".join(
        str(match.get(field) or "")
        for field in ("display_title", "expertise", "location", "source")
    )
    return has_unrelated_language_text(profile, text)


def has_unrelated_language_text(profile, text):
    allowed = profile_languages(profile)
    explicit = explicit_languages_in_text(text)
    return bool(explicit and not explicit.intersection(allowed))


def profile_languages(profile):
    languages = {normalize_language(value) for value in profile.get("languages", [])}
    return {language for language in languages if language}


def explicit_languages_in_text(text):
    normalized = normalize_text(text)
    found = set()
    for language in EXPLICIT_LANGUAGES:
        if re.search(rf"\b{re.escape(language)}\b", normalized):
            found.add(normalize_language(language))
    for alias, language in LANGUAGE_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", normalized):
            found.add(language)
    return found


def normalize_language(value):
    normalized = normalize_text(value)
    return LANGUAGE_ALIASES.get(normalized, normalized)


def normalize_text(value):
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def keyword_in_text(text, keyword):
    keyword = normalize_text(keyword)
    if len(keyword) <= 3 and keyword.isalnum():
        return re.search(rf"\b{re.escape(keyword)}\b", text) is not None
    return keyword in text


def match_strength(match):
    return match_strength_from_score(match["score"])


def match_strength_from_score(score):
    if score == "-":
        return "-"
    if score >= 34:
        return "Strong"
    if score >= 24:
        return "Medium"
    return "Possible"


def plain_reasons(match, record=None):
    mapped = []
    for reason in match["reasons"]:
        mapped.append(plain_reason(reason))
    if record:
        mapped.append("Matches your current application tracker")
    return unique(mapped)


def plain_reason(reason):
    lower = normalize(reason)
    if "portuguese" in lower:
        return "Portuguese language match"
    if "english" in lower:
        return "English language match"
    if "language review" in lower or "linguistic" in lower or "translation" in lower:
        return "Language or translation review work"
    if "evaluation" in lower or "reviewer" in lower or "quality" in lower:
        return "AI evaluation or quality review role"
    if "search" in lower or "ads" in lower:
        return "Search or ads quality work"
    if "remote" in lower or "flexible" in lower:
        return "Remote or flexible work signal"
    if "live/countable" in lower:
        return "Active opportunity"
    if "evergreen" in lower:
        return "Always-open application"
    if "public inventory" in lower or "mixed" in lower:
        return "Public lead"
    if "canonical" in lower:
        return "Similar postings grouped together"
    return reason


def readable_status(status):
    labels = {
        "recommended": "Recommended",
        "saved": "Saved",
        "applied": "Applied",
        "assessment_invited": "Assessment invited",
        "assessment_started": "Assessment started",
        "assessment_completed": "Assessment completed",
        "waiting": "Waiting",
        "accepted": "Accepted",
        "rejected": "Rejected",
        "active_worker": "Active worker",
        "paid_task_received": "Paid task received",
        "not_interested": "Not interested",
        "expired": "Expired",
        "remind_later": "Remind later",
    }
    return labels.get(status, str(status or "").replace("_", " ").title())


def readable_availability(value):
    if value == "active":
        return "Still open"
    if value == "inactive/removed":
        return "No longer active"
    return str(value or "Unknown").replace("_", " ").title()


def opportunity_type_label(match):
    kind = match["opportunity_kind"]
    basis = match["availability_basis"]
    if kind == "live_posting":
        return "Live opportunity"
    if kind == "evergreen_application":
        return "Always-open application"
    if kind == "public_inventory_opportunity":
        return "Public lead"
    if basis == "public_page":
        return "Public lead"
    return "Opportunity"


def readable_signal(label):
    replacements = {
        "Assessment activity reported": "Some users report assessment activity",
        "Acceptance/paid-task reported": "Some users report acceptance or paid tasks",
        "Mostly waiting/no-response": "Some users report waiting or no response",
        "High recent applicant activity": "Recent applicant activity reported",
        "Low confidence / limited data": "Limited reports so far",
        "Too few reports to trust": "Too few reports to rely on",
        "Waiting/no-response reported": "Waiting or no response reported",
        "Applicant activity reported": "Applicant activity reported",
    }
    return replacements.get(label, label)


def make_action_user_facing(action):
    text = action
    replacements = {
        "strong untracked match ": "",
        "new post-baseline match ": "new match ",
        "evergreen application ": "always-open application ",
        "remains an active strong match": "still looks like a strong fit",
        "tracker currently marks it inactive": "it may no longer be open",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"; score \d+", "", text)
    return text


def profile_strength_label(report):
    if report["risk_label"] == "Low":
        return "Strong fit today"
    if report["risk_label"] == "Medium":
        return "Good fit, but monitor new leads"
    return "Needs more leads or broader targeting"


def user_coverage_note(report):
    if report["risk_label"] == "Low":
        return "This profile has enough strong current matches to support a useful daily or weekly job-search workflow."
    if report["risk_label"] == "Medium":
        return "This profile has useful matches, but engagement may depend on fresh openings and careful follow-up."
    return "This profile may need more always-open applications, broader targeting, or additional sources before it feels reliably useful."


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
