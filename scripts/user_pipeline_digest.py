import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from profile_match_digest import (
    load_profiles,
    select_profiles,
    get_active_rows,
    get_post_baseline_new_rows,
    rank_opportunities,
    score_opportunity,
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


DEFAULT_PROFILES_FILE = Path("profiles/sample_profiles.json")
DEFAULT_PIPELINE_FILE = Path("profiles/sample_user_pipeline.json")
OUTPUT_PATH = Path("exports/user_pipeline_digest.md")
RECENT_DAYS = 7
NEW_MATCH_LIMIT = 8
MATCH_LIMIT = 100

ASSESSMENT_STATUSES = {
    "assessment_invited",
    "assessment_started",
    "assessment_completed",
}
WAITING_STATUSES = {"waiting", "applied", "assessment_completed"}
ACCEPTED_STATUSES = {"accepted", "active_worker", "paid_task_received"}
NEGATIVE_STATUSES = {"rejected", "not_interested"}
EXPIRED_STATUSES = {"expired"}
OPEN_ACTION_STATUSES = {"recommended", "saved", "remind_later"}
VALID_STATUSES = (
    "recommended",
    "saved",
    "applied",
    "assessment_invited",
    "assessment_started",
    "assessment_completed",
    "waiting",
    "accepted",
    "rejected",
    "active_worker",
    "paid_task_received",
    "not_interested",
    "expired",
    "remind_later",
)


def main():
    args = parse_args()
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    cutoff = (generated_at - timedelta(days=RECENT_DAYS)).isoformat()

    profiles, profile_source = load_selected_profiles(args.profiles_file, args.profile)
    pipeline_records, pipeline_source = load_pipeline(args.pipeline_file)
    if args.profile:
        pipeline_records = [
            record for record in pipeline_records
            if record["profile_id"] == args.profile
        ]

    with get_connection() as conn:
        market_summary = get_market_size_summary(
            conn,
            include_experimental=False,
            include_simulation=False,
        )
        active_rows = get_recommendation_rows(conn)
        all_rows = get_all_tracker_rows(conn)
        new_rows = get_post_baseline_new_rows(conn, cutoff)

    row_index = build_row_index(all_rows)
    reports = [
        build_pipeline_report(profile, pipeline_records, active_rows, new_rows, row_index)
        for profile in profiles
        if has_profile_context(profile, pipeline_records, args.profile)
    ]

    if not reports:
        raise SystemExit("No profiles matched the selected pipeline/profile filters.")

    markdown = render_markdown(
        generated_at,
        profile_source,
        pipeline_source,
        market_summary,
        reports,
    )
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(markdown, encoding="utf-8")

    print("")
    print("Wahojobs User Pipeline Digest")
    print("=============================")
    print(f"Generated: {generated_at.isoformat()} UTC")
    print(f"Profile source: {profile_source}")
    print(f"Pipeline source: {pipeline_source}")
    print(f"Profiles rendered: {len(reports)}")
    print(
        "Estimated Live Market Opportunities: "
        f"{market_summary['estimated_market_opportunities']}"
    )
    for report in reports[:3]:
        next_action = report["next_actions"][0]["action"] if report["next_actions"] else "No immediate action"
        print(
            f"{report['profile']['profile_id']}: "
            f"{report['summary']['total']} tracked, "
            f"next: {console_text(next_action)}"
        )
    print(f"Wrote Markdown report to {OUTPUT_PATH}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a read-only personal opportunity pipeline digest."
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
        "--profile",
        help="Render one profile_id only.",
    )
    return parser.parse_args()


def load_selected_profiles(path, profile_id):
    if path:
        profiles, source = load_profiles(path)
        return select_profiles(profiles, profile_id), source

    builtin_profiles, _ = load_profiles(None)
    profiles = builtin_profiles
    source = "built-in mock profiles"
    if DEFAULT_PROFILES_FILE.exists():
        sample_profiles, sample_source = load_profiles(DEFAULT_PROFILES_FILE)
        profiles = merge_profiles(builtin_profiles, sample_profiles)
        source = f"built-in mock profiles + {sample_source}"
    return select_profiles(profiles, profile_id) if profile_id else profiles, source


def merge_profiles(primary, secondary):
    merged = []
    seen = set()
    for profile in primary + secondary:
        if profile["profile_id"] in seen:
            continue
        seen.add(profile["profile_id"])
        merged.append(profile)
    return merged


def load_pipeline(path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Pipeline file not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Pipeline file is not valid JSON: {path} ({exc})")

    if isinstance(raw, dict) and "pipeline" in raw:
        raw_records = raw["pipeline"]
    elif isinstance(raw, list):
        raw_records = raw
    else:
        raise SystemExit(
            "Pipeline file must be either a list or an object with a 'pipeline' list."
        )
    if not isinstance(raw_records, list):
        raise SystemExit("Pipeline records must be a list.")

    records = [
        normalize_pipeline_record(record, index)
        for index, record in enumerate(raw_records, start=1)
    ]
    return records, str(path)


def normalize_pipeline_record(record, index):
    if not isinstance(record, dict):
        raise SystemExit(f"Malformed pipeline record #{index}: expected an object.")

    profile_id = require_string(record, "profile_id", index)
    source = require_string(record, "source", index)
    title = require_string(record, "title", index)
    status = require_string(record, "status", index)
    if status not in VALID_STATUSES:
        raise SystemExit(
            f"Malformed pipeline record #{index}: unsupported status '{status}'."
        )

    return {
        "profile_id": profile_id,
        "source": source,
        "title": title,
        "url": optional_string(record, "url"),
        "status": status,
        "status_date": optional_string(record, "status_date"),
        "notes": optional_string(record, "notes"),
        "user_priority": optional_string(record, "user_priority", "medium"),
        "reminder_date": optional_string(record, "reminder_date"),
        "last_user_action": optional_string(record, "last_user_action"),
    }


def require_string(record, field, index):
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(
            f"Malformed pipeline record #{index}: '{field}' must be a non-empty string."
        )
    return value.strip()


def optional_string(record, field, default=""):
    value = record.get(field, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise SystemExit(f"Malformed pipeline record: '{field}' must be a string.")
    return value.strip()


def has_profile_context(profile, pipeline_records, requested_profile_id):
    if requested_profile_id:
        return True
    return any(record["profile_id"] == profile["profile_id"] for record in pipeline_records)


def get_recommendation_rows(conn):
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
    return live_rows + evergreen_rows + public_rows


def get_all_tracker_rows(conn):
    return conn.execute(
        """
        SELECT
          j.id AS job_id,
          j.title,
          j.location,
          j.url,
          j.department,
          j.expertise,
          j.commitment,
          j.opportunity_kind,
          j.availability_basis,
          j.include_in_live_market_estimate,
          j.canonical_opportunity_id,
          j.is_active,
          j.removed_at,
          c.name AS source,
          c.slug AS source_slug,
          c.source_tier,
          c.inventory_model,
          c.market_count_policy,
          co.canonical_title,
          co.source_category
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        LEFT JOIN canonical_opportunities co ON co.id = j.canonical_opportunity_id
        WHERE j.title NOT LIKE '[SIMULATION]%'
        ORDER BY j.is_active DESC, c.name ASC, j.title ASC
        """
    ).fetchall()


def build_row_index(rows):
    by_url = {}
    by_source_title = {}
    for row in rows:
        if row["url"]:
            by_url.setdefault(row["url"], row)
        by_source_title.setdefault(source_title_key(row["source"], row["title"]), row)
    return {
        "by_url": by_url,
        "by_source_title": by_source_title,
    }


def build_pipeline_report(profile, pipeline_records, active_rows, new_rows, row_index):
    records = [record for record in pipeline_records if record["profile_id"] == profile["profile_id"]]
    enriched = [
        enrich_pipeline_record(profile, record, row_index)
        for record in records
    ]
    tracked_keys = {record["match_key"] for record in enriched}
    recommended_rows = rank_opportunities(
        profile,
        active_rows,
        group_canonical=True,
        limit=30,
        min_score=22,
    )
    new_matches = [
        match for match in rank_opportunities(
            profile,
            new_rows,
            group_canonical=True,
            limit=30,
            min_score=22,
        )
        if match_key_from_match(match) not in tracked_keys
    ][:NEW_MATCH_LIMIT]
    evergreen_matches = [
        match for match in recommended_rows
        if "Evergreen application" in "; ".join(match["reasons"])
    ]

    return {
        "profile": profile,
        "records": enriched,
        "summary": summarize_records(enriched),
        "next_actions": build_next_actions(enriched, new_matches, evergreen_matches),
        "new_matches": [
            match for match in recommended_rows
            if match_key_from_match(match) not in tracked_keys
        ][:NEW_MATCH_LIMIT],
        "new_since_baseline": new_matches,
        "risk_items": build_risk_items(enriched, new_matches, evergreen_matches),
        "evergreen_checklist": build_evergreen_checklist(evergreen_matches, enriched),
    }


def enrich_pipeline_record(profile, record, row_index):
    row = find_tracker_row(record, row_index)
    availability = availability_for(row)
    scored = score_opportunity(profile, row) if row else None
    record_match_key = pipeline_match_key(record, row)
    return {
        **record,
        "tracker_row": row,
        "availability": availability,
        "match_score": scored["score"] if scored else None,
        "match_reasons": scored["reasons"] if scored else [],
        "match_key": record_match_key,
        "next_action": action_for_record(record, availability, scored),
    }


def find_tracker_row(record, row_index):
    if record["url"] and record["url"] in row_index["by_url"]:
        return row_index["by_url"][record["url"]]
    return row_index["by_source_title"].get(source_title_key(record["source"], record["title"]))


def availability_for(row):
    if row is None:
        return "not found in tracker"
    if row["is_active"]:
        return "active"
    return "inactive/removed"


def action_for_record(record, availability, scored):
    source = record["source"]
    title = record["title"]
    status = record["status"]
    score = scored["score"] if scored else 0

    if availability == "inactive/removed":
        return f"Review alternatives for {title} at {source}; tracker currently marks it inactive."
    if status in OPEN_ACTION_STATUSES and score >= 25:
        return f"Apply to {title} at {source}; it remains an active strong match."
    if status == "assessment_invited":
        return f"Schedule or start the {source} assessment for {title}."
    if status == "assessment_started":
        return f"Continue the in-progress {source} assessment for {title}."
    if status == "assessment_completed":
        return f"Check status for {title} at {source} if no update has arrived."
    if status == "waiting":
        days = days_since(record["status_date"])
        if days is not None and days >= 14:
            return f"Follow up or check the portal for {title} at {source}; waiting for {days} days."
        return f"Keep monitoring {title} at {source}; currently waiting."
    if status == "applied":
        return f"Watch for assessment or next-step email from {source} for {title}."
    if status in ACCEPTED_STATUSES:
        return f"Keep {source} active; check for new paid tasks or worker updates."
    if status in NEGATIVE_STATUSES:
        return f"Hide or deprioritize {title} at {source}; user marked it {status}."
    if status == "expired":
        return f"Archive {title} at {source} and review similar active matches."
    return f"Review {title} at {source}."


def build_next_actions(records, new_matches, evergreen_matches):
    actions = []
    for record in records:
        if record["status"] in NEGATIVE_STATUSES or record["status"] in ACCEPTED_STATUSES:
            continue
        if record["next_action"]:
            actions.append(
                {
                    "priority": action_priority(record),
                    "action": record["next_action"],
                    "title": record["title"],
                    "source": record["source"],
                }
            )

    for match in new_matches[:3]:
        actions.append(
            {
                "priority": "medium",
                "action": (
                    f"Review new post-baseline match {match['display_title']} "
                    f"from {match['source']}; score {match['score']}."
                ),
                "title": match["display_title"],
                "source": match["source"],
            }
        )

    for match in evergreen_matches[:2]:
        actions.append(
            {
                "priority": "medium",
                "action": (
                    f"Revisit evergreen application {match['display_title']} "
                    f"from {match['source']} if not already attempted."
                ),
                "title": match["display_title"],
                "source": match["source"],
            }
        )

    return sorted(actions, key=lambda item: priority_rank(item["priority"]))[:10]


def action_priority(record):
    if record["status"] in {"assessment_invited", "assessment_started"}:
        return "high"
    if record["status"] in {"recommended", "saved", "remind_later"} and (record["match_score"] or 0) >= 30:
        return "high"
    if record["status"] == "waiting" and (days_since(record["status_date"]) or 0) >= 14:
        return "medium"
    if record["availability"] == "inactive/removed":
        return "medium"
    return record["user_priority"] or "medium"


def priority_rank(priority):
    return {"high": 0, "medium": 1, "low": 2}.get(priority, 1)


def build_risk_items(records, new_matches, evergreen_matches):
    items = []
    for record in records:
        if record["availability"] == "inactive/removed" or record["status"] == "expired":
            items.append(
                f"{record['title']} at {record['source']} may be stale or expired."
            )
        elif record["status"] in {"waiting", "assessment_completed"} and (days_since(record["status_date"]) or 0) >= 14:
            items.append(
                f"{record['title']} at {record['source']} has been waiting for {days_since(record['status_date'])} days."
            )
        elif record["status"] in OPEN_ACTION_STATUSES and (record["match_score"] or 0) >= 30:
            items.append(
                f"{record['title']} at {record['source']} is a strong match but not yet applied."
            )
    for match in evergreen_matches[:2]:
        items.append(
            f"Evergreen application {match['display_title']} at {match['source']} is relevant; verify whether it has been attempted."
        )
    if not items and new_matches:
        items.append("No urgent risks; review new high-fit matches before they age out.")
    return items[:8]


def build_evergreen_checklist(evergreen_matches, records):
    checklist = []
    for match in evergreen_matches[:8]:
        status = "not started"
        for record in records:
            if match_key_from_match(match) == record["match_key"]:
                status = record["status"]
                break
        checklist.append(
            {
                "title": match["display_title"],
                "source": match["source"],
                "score": match["score"],
                "status": status,
                "url": match["url"],
            }
        )
    return checklist


def summarize_records(records):
    statuses = Counter(record["status"] for record in records)
    return {
        "total": len(records),
        "applied": statuses["applied"],
        "saved": statuses["saved"],
        "assessment": sum(statuses[status] for status in ASSESSMENT_STATUSES),
        "waiting": statuses["waiting"],
        "accepted_rejected": sum(statuses[status] for status in ACCEPTED_STATUSES | {"rejected"}),
        "not_interested": statuses["not_interested"],
        "recommended_open": statuses["recommended"] + statuses["remind_later"],
    }


def render_markdown(generated_at, profile_source, pipeline_source, market_summary, reports):
    lines = [
        "# User Opportunity Pipeline Digest",
        "",
        f"Generated: {generated_at.isoformat()} UTC",
        "",
        "## Prototype Notes",
        "",
        (
            "This read-only prototype combines selected profiles, deterministic "
            "opportunity matches, mock user pipeline statuses, current tracker "
            "availability, and recent post-baseline movement."
        ),
        "",
        f"- Profile source: **{escape(profile_source)}**",
        f"- Pipeline source: **{escape(pipeline_source)}**",
        f"- Profiles rendered: **{len(reports)}**",
        f"- Estimated Live Market Opportunities: **{market_summary['estimated_market_opportunities']}**",
        "- User status data is sample/mock data and does not update the tracker.",
        "",
    ]
    for report in reports:
        append_profile_pipeline(lines, report)
    append_product_notes(lines)
    return "\n".join(lines)


def append_profile_pipeline(lines, report):
    profile = report["profile"]
    summary = report["summary"]
    lines.extend(
        [
            f"## {escape(profile['display_name'])}",
            "",
            "### User Pipeline Summary",
            "",
            f"- Profile ID: `{profile['profile_id']}`",
            f"- Total tracked opportunities: {summary['total']}",
            f"- Applied: {summary['applied']}",
            f"- Saved: {summary['saved']}",
            f"- Assessment-related: {summary['assessment']}",
            f"- Waiting: {summary['waiting']}",
            f"- Accepted/rejected: {summary['accepted_rejected']}",
            f"- Not interested: {summary['not_interested']}",
            f"- Recommended/remind later but not acted on: {summary['recommended_open']}",
            "",
        ]
    )

    append_next_actions(lines, report["next_actions"])
    append_pipeline_by_status(lines, report["records"])
    append_match_table(lines, "New Matches Not Yet Tracked", report["new_matches"])
    append_risk_items(lines, report["risk_items"])
    append_evergreen_checklist(lines, report["evergreen_checklist"])


def append_next_actions(lines, actions):
    lines.extend(["### Recommended Next Actions", ""])
    if not actions:
        lines.extend(["No immediate action found from the sample pipeline.", ""])
        return
    for action in actions:
        lines.append(
            f"- **{escape(action['priority'].title())}**: {escape(action['action'])}"
        )
    lines.append("")


def append_pipeline_by_status(lines, records):
    grouped = defaultdict(list)
    for record in records:
        grouped[status_group(record["status"])].append(record)

    lines.extend(["### Pipeline by Status", ""])
    if not records:
        lines.extend(["No tracked pipeline records for this profile.", ""])
        return
    for group in (
        "Saved",
        "Applied",
        "Assessment",
        "Waiting",
        "Accepted / Active",
        "Rejected / Not interested",
        "Expired",
        "Recommended / Remind later",
    ):
        if group not in grouped:
            continue
        lines.extend([f"#### {group}", ""])
        lines.extend(
            [
                "| Title | Source | Status | Date | Availability | Score | Next Action |",
                "| --- | --- | --- | --- | --- | ---: | --- |",
            ]
        )
        for record in grouped[group]:
            score = record["match_score"] if record["match_score"] is not None else "-"
            lines.append(
                "| "
                f"{escape(record['title'])} | "
                f"{escape(record['source'])} | "
                f"{escape(record['status'])} | "
                f"{escape(record['status_date'] or 'Unknown')} | "
                f"{escape(record['availability'])} | "
                f"{score} | "
                f"{escape(record['next_action'])} |"
            )
        lines.append("")


def append_match_table(lines, title, matches):
    lines.extend([f"### {title}", ""])
    if not matches:
        lines.extend(["No high-quality untracked matches found.", ""])
        return
    lines.extend(
        [
            "| Score | Title | Source | Location | Area | URL |",
            "| ---: | --- | --- | --- | --- | --- |",
        ]
    )
    for match in matches[:NEW_MATCH_LIMIT]:
        lines.append(
            "| "
            f"{match['score']} | "
            f"{escape(match['display_title'])} | "
            f"{escape(match['source'])} | "
            f"{escape(match['location'])} | "
            f"{escape(match['expertise'])} | "
            f"[Open]({match['url']}) |"
        )
    lines.append("")


def append_risk_items(lines, items):
    lines.extend(["### Opportunities at Risk", ""])
    if not items:
        lines.extend(["No obvious opportunity risks from the sample pipeline.", ""])
        return
    for item in items:
        lines.append(f"- {escape(item)}")
    lines.append("")


def append_evergreen_checklist(lines, checklist):
    lines.extend(["### Evergreen Application Checklist", ""])
    if not checklist:
        lines.extend(["No relevant evergreen applications found for this profile.", ""])
        return
    lines.extend(
        [
            "| Status | Score | Title | Source | URL |",
            "| --- | ---: | --- | --- | --- |",
        ]
    )
    for item in checklist:
        lines.append(
            "| "
            f"{escape(item['status'])} | "
            f"{item['score']} | "
            f"{escape(item['title'])} | "
            f"{escape(item['source'])} | "
            f"[Open]({item['url']}) |"
        )
    lines.append("")


def append_product_notes(lines):
    lines.extend(
        [
            "## Product Notes",
            "",
            "- This is a mock read-only prototype; no user pipeline statuses are written back to SQLite.",
            "- Future app versions could let users update statuses, set reminders, and hide not-interested opportunities.",
            "- Aggregated applicant signals could later identify which sources create interviews, assessments, paid tasks, or churn.",
            "- Current actions are deterministic and operational; they are not eligibility decisions.",
            "",
        ]
    )


def status_group(status):
    if status == "saved":
        return "Saved"
    if status == "applied":
        return "Applied"
    if status in ASSESSMENT_STATUSES:
        return "Assessment"
    if status == "waiting":
        return "Waiting"
    if status in ACCEPTED_STATUSES:
        return "Accepted / Active"
    if status in NEGATIVE_STATUSES:
        return "Rejected / Not interested"
    if status in EXPIRED_STATUSES:
        return "Expired"
    return "Recommended / Remind later"


def source_title_key(source, title):
    return (normalize_key(source), normalize_key(title))


def pipeline_match_key(record, row):
    if row and row["url"]:
        return normalize_key(row["url"])
    if record["url"]:
        return normalize_key(record["url"])
    return "|".join(source_title_key(record["source"], record["title"]))


def match_key_from_match(match):
    return normalize_key(match["url"]) if match["url"] else "|".join(
        (normalize_key(match["source"]), normalize_key(match["display_title"]))
    )


def normalize_key(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def days_since(value):
    if not value:
        return None
    try:
        date_value = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (datetime.now(timezone.utc).date() - date_value).days


if __name__ == "__main__":
    main()
