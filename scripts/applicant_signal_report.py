import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


DEFAULT_UPDATES_FILE = Path("profiles/sample_applicant_updates.json")
OUTPUT_PATH = Path("exports/applicant_signal_report.md")
DEFAULT_DAYS = 14

ASSESSMENT_STATUSES = {
    "assessment_invited",
    "assessment_started",
    "assessment_completed",
}
SUCCESS_STATUSES = {
    "accepted",
    "active_worker",
    "paid_task_received",
}
WAITING_STATUSES = {
    "waiting",
    "no_response_14d",
    "no_response_30d",
}
NEGATIVE_STATUSES = {"rejected", "withdrawn"}
VALID_STATUSES = {
    "applied",
    "assessment_invited",
    "assessment_started",
    "assessment_completed",
    "waiting",
    "rejected",
    "accepted",
    "active_worker",
    "paid_task_received",
    "no_response_14d",
    "no_response_30d",
    "withdrawn",
}
VALID_EVIDENCE_TYPES = {
    "self_reported",
    "screenshot_claimed",
    "email_claimed",
    "platform_connected_future",
    "imported_from_pipeline_mock",
}
VALID_CONFIDENCE_LEVELS = {"low", "medium", "high"}


def main():
    args = parse_args()
    generated_at = datetime.now(timezone.utc).replace(microsecond=0)
    cutoff = generated_at - timedelta(days=args.days)
    updates, source_path = load_updates(args.updates_file)
    filtered_updates = filter_updates(
        updates,
        source=args.source,
        profile_id=args.profile,
    )

    summary = build_summary(filtered_updates, cutoff)
    source_signals = build_source_signals(filtered_updates, cutoff)
    opportunity_signals = build_opportunity_signals(filtered_updates, cutoff)
    profile_signals = build_profile_signals(filtered_updates, cutoff)
    transitions = build_recent_transitions(filtered_updates, cutoff)
    watchlist = build_watchlist(source_signals, opportunity_signals)

    markdown = render_markdown(
        generated_at,
        source_path,
        args,
        summary,
        source_signals,
        opportunity_signals,
        profile_signals,
        transitions,
        watchlist,
    )
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(markdown, encoding="utf-8")

    print("")
    print("Wahojobs Applicant Signal Report")
    print("================================")
    print(f"Generated: {generated_at.isoformat()} UTC")
    print(f"Updates file: {source_path}")
    print(f"Window: last {args.days} days")
    if args.source:
        print(f"Source filter: {args.source}")
    if args.profile:
        print(f"Profile filter: {args.profile}")
    print(f"Applicant updates: {summary['total_updates']}")
    print(f"Unique users: {summary['unique_users']}")
    print(f"Unique sources: {summary['unique_sources']}")
    print(f"Recent updates: {summary['recent_updates']}")
    print(f"Assessment-related updates: {summary['assessment_updates']}")
    print(f"Accepted/active/paid-task updates: {summary['success_updates']}")
    print(f"Waiting/no-response updates: {summary['waiting_updates']}")
    print(f"Wrote Markdown report to {OUTPUT_PATH}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a read-only aggregate applicant signal report."
    )
    parser.add_argument(
        "--updates-file",
        type=Path,
        default=DEFAULT_UPDATES_FILE,
        help="Load mock applicant status updates from a JSON file.",
    )
    parser.add_argument(
        "--source",
        help="Filter to one source/company name, case-insensitive.",
    )
    parser.add_argument(
        "--profile",
        help="Filter to one profile_id.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"Recent activity window in days. Defaults to {DEFAULT_DAYS}.",
    )
    args = parser.parse_args()
    if args.days <= 0:
        raise SystemExit("--days must be a positive integer.")
    return args


def load_updates(path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Applicant updates file not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Applicant updates file is not valid JSON: {path} ({exc})")

    if isinstance(raw, dict) and "updates" in raw:
        raw_updates = raw["updates"]
    elif isinstance(raw, list):
        raw_updates = raw
    else:
        raise SystemExit(
            "Applicant updates file must be a list or an object with an 'updates' list."
        )
    if not isinstance(raw_updates, list):
        raise SystemExit("Applicant updates must be a list.")

    updates = [
        normalize_update(record, index)
        for index, record in enumerate(raw_updates, start=1)
    ]
    return updates, str(path)


def normalize_update(record, index):
    if not isinstance(record, dict):
        raise SystemExit(f"Malformed update #{index}: expected an object.")

    status = require_string(record, "status", index)
    evidence_type = require_string(record, "evidence_type", index)
    confidence_level = require_string(record, "confidence_level", index)
    if status not in VALID_STATUSES:
        raise SystemExit(f"Malformed update #{index}: unsupported status '{status}'.")
    if evidence_type not in VALID_EVIDENCE_TYPES:
        raise SystemExit(
            f"Malformed update #{index}: unsupported evidence_type '{evidence_type}'."
        )
    if confidence_level not in VALID_CONFIDENCE_LEVELS:
        raise SystemExit(
            f"Malformed update #{index}: unsupported confidence_level '{confidence_level}'."
        )

    reported_at = parse_datetime(require_string(record, "reported_at", index), index)
    status_date = require_string(record, "status_date", index)

    return {
        "update_id": require_string(record, "update_id", index),
        "user_id": require_string(record, "user_id", index),
        "profile_id": require_string(record, "profile_id", index),
        "source": require_string(record, "source", index),
        "opportunity_title": require_string(record, "opportunity_title", index),
        "opportunity_url": optional_string(record, "opportunity_url"),
        "opportunity_id": optional_string(record, "opportunity_id"),
        "status": status,
        "previous_status": optional_string(record, "previous_status"),
        "status_date": status_date,
        "reported_at": record["reported_at"],
        "reported_at_dt": reported_at,
        "evidence_type": evidence_type,
        "confidence_level": confidence_level,
        "notes": optional_string(record, "notes"),
    }


def require_string(record, field, index):
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"Malformed update #{index}: '{field}' must be a non-empty string.")
    return value.strip()


def optional_string(record, field, default=""):
    value = record.get(field, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise SystemExit(f"Malformed update: '{field}' must be a string.")
    return value.strip()


def parse_datetime(value, index):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SystemExit(
            f"Malformed update #{index}: 'reported_at' must be ISO-like datetime."
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def filter_updates(updates, source=None, profile_id=None):
    filtered = updates
    if source:
        filtered = [
            update for update in filtered
            if normalize(update["source"]) == normalize(source)
        ]
    if profile_id:
        filtered = [
            update for update in filtered
            if update["profile_id"] == profile_id
        ]
    return filtered


def build_summary(updates, cutoff):
    confidence = Counter(update["confidence_level"] for update in updates)
    return {
        "total_updates": len(updates),
        "unique_users": len({update["user_id"] for update in updates}),
        "unique_sources": len({update["source"] for update in updates}),
        "unique_opportunities": len({opportunity_key(update) for update in updates}),
        "recent_updates": count_recent(updates, cutoff),
        "assessment_updates": count_statuses(updates, ASSESSMENT_STATUSES),
        "success_updates": count_statuses(updates, SUCCESS_STATUSES),
        "waiting_updates": count_statuses(updates, WAITING_STATUSES),
        "confidence": confidence,
    }


def build_source_signals(updates, cutoff):
    grouped = defaultdict(list)
    for update in updates:
        grouped[update["source"]].append(update)
    rows = []
    for source, source_updates in grouped.items():
        row = aggregate_updates(source_updates, cutoff)
        row["source"] = source
        row["signal_label"] = source_signal_label(row)
        rows.append(row)
    return sorted(rows, key=lambda row: (-row["recent_reports"], -row["reports"], row["source"]))


def build_opportunity_signals(updates, cutoff):
    grouped = defaultdict(list)
    for update in updates:
        grouped[opportunity_key(update)].append(update)
    rows = []
    for _, opportunity_updates in grouped.items():
        row = aggregate_updates(opportunity_updates, cutoff)
        first = opportunity_updates[0]
        row["source"] = first["source"]
        row["title"] = first["opportunity_title"]
        row["url"] = first["opportunity_url"]
        row["status_mix"] = Counter(update["status"] for update in opportunity_updates)
        row["confidence_label"] = confidence_label(opportunity_updates)
        row["signal_label"] = opportunity_signal_label(row)
        rows.append(row)
    return sorted(rows, key=lambda row: (-row["recent_reports"], -row["reports"], row["source"], row["title"]))


def build_profile_signals(updates, cutoff):
    grouped = defaultdict(list)
    for update in updates:
        grouped[update["profile_id"]].append(update)
    rows = []
    for profile_id, profile_updates in grouped.items():
        row = aggregate_updates(profile_updates, cutoff)
        row["profile_id"] = profile_id
        row["signal_label"] = profile_signal_label(row)
        rows.append(row)
    return sorted(rows, key=lambda row: (-row["reports"], row["profile_id"]))


def build_recent_transitions(updates, cutoff):
    transitions = [
        update for update in updates
        if update["previous_status"] and update["reported_at_dt"] >= cutoff
    ]
    return sorted(transitions, key=lambda update: update["reported_at_dt"], reverse=True)[:20]


def build_watchlist(source_signals, opportunity_signals):
    watch = {
        "waiting_sources": [],
        "assessment_sources": [],
        "success_opportunities": [],
        "limited_data": [],
    }
    for row in source_signals:
        if row["waiting_reports"] >= 2 and row["assessment_reports"] == 0:
            watch["waiting_sources"].append(row)
        if row["recent_reports"] >= 2 and row["assessment_reports"] >= 2:
            watch["assessment_sources"].append(row)
    for row in opportunity_signals:
        if row["success_reports"] > 0:
            watch["success_opportunities"].append(row)
        if row["reports"] <= 1:
            watch["limited_data"].append(row)
    return watch


def aggregate_updates(updates, cutoff):
    latest = max(update["reported_at_dt"] for update in updates) if updates else None
    return {
        "reports": len(updates),
        "unique_users": len({update["user_id"] for update in updates}),
        "recent_reports": count_recent(updates, cutoff),
        "assessment_reports": count_statuses(updates, ASSESSMENT_STATUSES),
        "assessment_invited": count_status(updates, "assessment_invited"),
        "assessment_started": count_status(updates, "assessment_started"),
        "assessment_completed": count_status(updates, "assessment_completed"),
        "success_reports": count_statuses(updates, SUCCESS_STATUSES),
        "accepted": count_status(updates, "accepted"),
        "active_worker": count_status(updates, "active_worker"),
        "paid_task_received": count_status(updates, "paid_task_received"),
        "waiting_reports": count_statuses(updates, WAITING_STATUSES),
        "latest_report_date": latest.date().isoformat() if latest else "N/A",
        "confidence": Counter(update["confidence_level"] for update in updates),
    }


def source_signal_label(row):
    if row["reports"] < 3:
        return "Low confidence / limited data"
    if row["success_reports"]:
        return "Acceptance/paid-task reported"
    if row["assessment_reports"] >= 2:
        return "Assessment activity reported"
    if row["waiting_reports"] >= max(2, row["assessment_reports"] + row["success_reports"]):
        return "Mostly waiting/no-response"
    if row["recent_reports"] >= 3:
        return "High recent applicant activity"
    return "Directional activity signal"


def opportunity_signal_label(row):
    if row["reports"] < 2:
        return "Too few reports to trust"
    if row["success_reports"]:
        return "Acceptance/paid-task reported"
    if row["assessment_reports"]:
        return "Assessment activity reported"
    if row["waiting_reports"]:
        return "Waiting/no-response reported"
    return "Applicant activity reported"


def profile_signal_label(row):
    if row["success_reports"]:
        return "Acceptance/active/paid-task reports present"
    if row["assessment_reports"] >= 2:
        return "Assessment activity reported"
    if row["waiting_reports"] >= 2:
        return "Mostly waiting/no-response"
    return "Limited directional reports"


def confidence_label(updates):
    counts = Counter(update["confidence_level"] for update in updates)
    if counts["high"]:
        return "high"
    if counts["medium"]:
        return "medium"
    return "low"


def count_recent(updates, cutoff):
    return sum(1 for update in updates if update["reported_at_dt"] >= cutoff)


def count_statuses(updates, statuses):
    return sum(1 for update in updates if update["status"] in statuses)


def count_status(updates, status):
    return sum(1 for update in updates if update["status"] == status)


def render_markdown(
    generated_at,
    source_path,
    args,
    summary,
    source_signals,
    opportunity_signals,
    profile_signals,
    transitions,
    watchlist,
):
    lines = [
        "# Applicant Signal Report",
        "",
        f"Generated: {generated_at.isoformat()} UTC",
        "",
        "## Executive Summary",
        "",
        f"- Updates file: **{escape(source_path)}**",
        f"- Window: **last {args.days} days**",
        f"- Source filter: **{escape(args.source or 'All sources')}**",
        f"- Profile filter: **{escape(args.profile or 'All profiles')}**",
        f"- Total applicant updates: **{summary['total_updates']}**",
        f"- Unique users: **{summary['unique_users']}**",
        f"- Unique sources/companies: **{summary['unique_sources']}**",
        f"- Unique opportunity references: **{summary['unique_opportunities']}**",
        f"- Recent updates in window: **{summary['recent_updates']}**",
        f"- Assessment-related updates: **{summary['assessment_updates']}**",
        f"- Accepted/active/paid-task updates: **{summary['success_updates']}**",
        f"- Waiting/no-response updates: **{summary['waiting_updates']}**",
        f"- Confidence distribution: {format_counter(summary['confidence'])}",
        "",
        "All records in this report are mock applicant updates. Treat signals as product-shaping examples, not real outcomes.",
        "",
    ]

    append_source_activity(lines, source_signals)
    append_opportunity_signals(lines, opportunity_signals)
    append_profile_signals(lines, profile_signals)
    append_transitions(lines, transitions)
    append_watchlist(lines, watchlist)
    append_methodology(lines)
    return "\n".join(lines)


def append_source_activity(lines, rows):
    lines.extend(
        [
            "## Source Activity Signals",
            "",
            "| Source | Reports | Users | Recent | Invited | Started | Completed | Accepted/Active/Paid | Waiting/No-response | Latest | Signal |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    if not rows:
        lines.append("| None | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | N/A | No data |")
    for row in rows:
        lines.append(
            "| "
            f"{escape(row['source'])} | "
            f"{row['reports']} | "
            f"{row['unique_users']} | "
            f"{row['recent_reports']} | "
            f"{row['assessment_invited']} | "
            f"{row['assessment_started']} | "
            f"{row['assessment_completed']} | "
            f"{row['success_reports']} | "
            f"{row['waiting_reports']} | "
            f"{row['latest_report_date']} | "
            f"{escape(row['signal_label'])} |"
        )
    lines.append("")


def append_opportunity_signals(lines, rows):
    lines.extend(
        [
            "## Opportunity-Level Signals",
            "",
            "| Source | Opportunity | Reports | Users | Recent | Status Mix | Confidence | Signal | URL |",
            "| --- | --- | ---: | ---: | ---: | --- | --- | --- | --- |",
        ]
    )
    if not rows:
        lines.append("| None | None | 0 | 0 | 0 | None | low | No data | |")
    for row in rows[:20]:
        url = f"[Open]({row['url']})" if row["url"] else ""
        lines.append(
            "| "
            f"{escape(row['source'])} | "
            f"{escape(row['title'])} | "
            f"{row['reports']} | "
            f"{row['unique_users']} | "
            f"{row['recent_reports']} | "
            f"{escape(format_counter(row['status_mix']))} | "
            f"{row['confidence_label']} | "
            f"{escape(row['signal_label'])} | "
            f"{url} |"
        )
    lines.append("")


def append_profile_signals(lines, rows):
    lines.extend(
        [
            "## Profile-Based Signals",
            "",
            "| Profile | Reports | Users | Recent | Assessments | Accepted/Active/Paid | Waiting/No-response | Signal |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    if not rows:
        lines.append("| None | 0 | 0 | 0 | 0 | 0 | 0 | No data |")
    for row in rows:
        lines.append(
            "| "
            f"{escape(row['profile_id'])} | "
            f"{row['reports']} | "
            f"{row['unique_users']} | "
            f"{row['recent_reports']} | "
            f"{row['assessment_reports']} | "
            f"{row['success_reports']} | "
            f"{row['waiting_reports']} | "
            f"{escape(row['signal_label'])} |"
        )
    lines.append("")


def append_transitions(lines, transitions):
    lines.extend(
        [
            "## Recent Applicant Movement",
            "",
            "| Reported | Source | Opportunity | Profile | Transition | Confidence |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    if not transitions:
        lines.append("| None | None | None | None | None | low |")
    for update in transitions:
        lines.append(
            "| "
            f"{update['reported_at_dt'].date().isoformat()} | "
            f"{escape(update['source'])} | "
            f"{escape(update['opportunity_title'])} | "
            f"{escape(update['profile_id'])} | "
            f"{escape(update['previous_status'])} -> {escape(update['status'])} | "
            f"{escape(update['confidence_level'])} |"
        )
    lines.append("")


def append_watchlist(lines, watchlist):
    lines.extend(["## Watchlist", ""])
    append_watch_section(
        lines,
        "Sources with many waiting/no-response reports and few assessments",
        watchlist["waiting_sources"],
        lambda row: f"{row['source']}: {row['waiting_reports']} waiting/no-response reports, {row['assessment_reports']} assessment reports",
    )
    append_watch_section(
        lines,
        "Sources with recent assessment activity",
        watchlist["assessment_sources"],
        lambda row: f"{row['source']}: {row['assessment_reports']} assessment reports, {row['recent_reports']} recent reports",
    )
    append_watch_section(
        lines,
        "Opportunities with acceptance/paid-task reports",
        watchlist["success_opportunities"],
        lambda row: f"{row['source']} - {row['title']}: {row['success_reports']} success report(s), confidence {row['confidence_label']}",
    )
    append_watch_section(
        lines,
        "Opportunities with too few reports to trust",
        watchlist["limited_data"][:10],
        lambda row: f"{row['source']} - {row['title']}: {row['reports']} report",
    )


def append_watch_section(lines, title, rows, formatter):
    lines.extend([f"### {title}", ""])
    if not rows:
        lines.extend(["None.", ""])
        return
    for row in rows:
        lines.append(f"- {escape(formatter(row))}")
    lines.append("")


def append_methodology(lines):
    lines.extend(
        [
            "## Methodology / Trust Notes",
            "",
            "- This prototype uses mock user-reported data only; it does not describe real applicant outcomes.",
            "- Signals are directional, not proof of source behavior or individual eligibility.",
            "- Future production versions should aggregate data anonymously and avoid exposing individual user records.",
            "- Evidence type and confidence level should affect how strongly signals are displayed.",
            "- Applicant signals should never be presented as guarantees of assessment, acceptance, or paid work.",
            "",
        ]
    )


def opportunity_key(update):
    if update["opportunity_id"]:
        return normalize(update["source"]) + "::" + normalize(update["opportunity_id"])
    if update["opportunity_url"]:
        return normalize(update["source"]) + "::" + normalize(update["opportunity_url"])
    return normalize(update["source"]) + "::" + normalize(update["opportunity_title"])


def format_counter(counter):
    if not counter:
        return "none"
    return ", ".join(
        f"{label}: {count}"
        for label, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    )


def normalize(value):
    return str(value or "").strip().lower()


def escape(value):
    return str(value or "").replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    main()
