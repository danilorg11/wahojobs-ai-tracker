import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.db.connection import get_connection
from wahojobs.reporting.market import (
    company_label,
    experimental_sources_status,
    experimental_filter,
    get_classification_summary,
    get_market_size_summary,
    live_market_filter,
)
from wahojobs.reporting.micro1 import get_micro1_metrics


def main():
    args = parse_args()
    report_date = args.report_date

    with get_connection() as conn:
        market_summary = get_market_size_summary(
            conn,
            include_experimental=args.include_experimental,
            include_simulation=args.include_simulation,
        )
        discovered = count_events(
            conn, report_date, "discovered", args.include_simulation,
            args.include_experimental
        )
        removed = count_events(
            conn, report_date, "removed", args.include_simulation,
            args.include_experimental
        )
        reactivated = count_events(
            conn, report_date, "reactivated", args.include_simulation,
            args.include_experimental
        )
        new_by_company = group_events(
            conn, report_date, "discovered", "company",
            args.include_simulation, args.include_experimental
        )
        removed_by_company = group_events(
            conn, report_date, "removed", "company",
            args.include_simulation, args.include_experimental
        )
        new_by_expertise = group_events(
            conn, report_date, "discovered", "expertise",
            args.include_simulation, args.include_experimental
        )
        removed_by_expertise = group_events(
            conn, report_date, "removed", "expertise",
            args.include_simulation, args.include_experimental
        )
        recent_events = get_recent_events(
            conn, report_date, args.include_simulation,
            args.include_experimental, limit=10
        )
        classification_summary = get_classification_summary(
            conn,
            include_experimental=args.include_experimental,
            include_simulation=args.include_simulation,
        )
        micro1_metrics = get_micro1_metrics(conn)

    print("")
    print("Wahojobs Daily Market Report")
    print("============================")
    print(f"Report date: {report_date} UTC")
    print(f"Simulation: {'included' if args.include_simulation else 'excluded'}")
    print(
        "Experimental sources: "
        f"{experimental_sources_status(args.include_experimental)}"
    )
    print("")
    print(f"Raw active postings:              {market_summary['raw_active_postings']}")
    print(
        "Estimated market opportunities:   "
        f"{market_summary['estimated_market_opportunities']}"
    )
    print("Estimate: canonicalized sources where available; raw active jobs elsewhere.")
    print(f"Alignerr raw postings:            {market_summary['alignerr_raw_postings']}")
    print(
        "Alignerr canonical opportunities: "
        f"{market_summary['alignerr_canonical_opportunities']}"
    )
    print(f"Alignerr posting variants:        {market_summary['alignerr_posting_variants']}")
    print(f"DataForce raw postings:           {market_summary['dataforce_raw_postings']}")
    print(
        "DataForce canonical opportunities: "
        f"{market_summary['dataforce_canonical_opportunities']}"
    )
    print(f"DataForce posting variants:       {market_summary['dataforce_posting_variants']}")
    print(f"Meridial raw postings:            {market_summary['meridial_raw_postings']}")
    print(
        "Meridial canonical opportunities: "
        f"{market_summary['meridial_canonical_opportunities']}"
    )
    print(f"Meridial posting variants:        {market_summary['meridial_posting_variants']}")
    print(f"Mindrift raw postings:            {market_summary['mindrift_raw_postings']}")
    print(
        "Mindrift canonical opportunities: "
        f"{market_summary['mindrift_canonical_opportunities']}"
    )
    print(f"Mindrift posting variants:        {market_summary['mindrift_posting_variants']}")
    print(f"OneForma raw variants:            {market_summary['oneforma_raw_variants']}")
    print(
        "OneForma canonical opportunities: "
        f"{market_summary['oneforma_canonical_opportunities']}"
    )
    print(f"OneForma posting variants:        {market_summary['oneforma_posting_variants']}")
    print(f"Turing raw postings:              {market_summary['turing_raw_postings']}")
    print(
        "Turing canonical opportunities:   "
        f"{market_summary['turing_canonical_opportunities']}"
    )
    print(f"Turing posting variants:          {market_summary['turing_posting_variants']}")
    print(f"Welocalize raw postings:          {market_summary['welocalize_raw_postings']}")
    print(
        "Welocalize canonical opportunities: "
        f"{market_summary['welocalize_canonical_opportunities']}"
    )
    print(f"Welocalize posting variants:      {market_summary['welocalize_posting_variants']}")
    print(f"micro1 active jobs:               {micro1_metrics['active_jobs']}")
    print(f"micro1 unique titles:             {micro1_metrics['unique_titles']}")
    print(f"micro1 duplicate-title count:     {micro1_metrics['duplicate_title_count']}")
    print(f"New jobs today:           {discovered}")
    print(f"Removed jobs today:       {removed}")
    print(f"Reactivated jobs today:   {reactivated}")
    print("")
    print_count_section("Active jobs by inventory model", classification_summary["inventory_models"])
    print_count_section("Active jobs by opportunity kind", classification_summary["opportunity_kinds"])

    print_count_section("New jobs by company", new_by_company)
    print_count_section("Removed jobs by company", removed_by_company)
    print_count_section("New jobs by expertise/department", new_by_expertise)
    print_count_section("Removed jobs by expertise/department", removed_by_expertise)
    print_events("Top 10 recent events today", recent_events)


def parse_args():
    parser = argparse.ArgumentParser(description="Show a daily Wahojobs market report.")
    parser.add_argument(
        "--date",
        dest="report_date",
        default=datetime.now(timezone.utc).date().isoformat(),
        help="Report date in YYYY-MM-DD format. Defaults to today in UTC.",
    )
    parser.add_argument(
        "--include-simulation",
        action="store_true",
        help="Include local simulation events and sample jobs in the report.",
    )
    parser.add_argument(
        "--include-experimental",
        action="store_true",
        help="Include non-core/experimental sources such as Invisible.",
    )
    args = parser.parse_args()
    validate_date(args.report_date)
    return args


def validate_date(value):
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise SystemExit("--date must use YYYY-MM-DD format")


def count_events(conn, report_date, event_type, include_simulation, include_experimental):
    simulation_filter = "" if include_simulation else "AND j.title NOT LIKE '[SIMULATION]%'"
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        JOIN companies c ON c.id = j.company_id
        WHERE date(je.created_at) = ?
          AND je.event_type = ?
          {live_market_filter("c", "j")}
          {simulation_filter}
          {experimental_filter("c", include_experimental)}
        """,
        (report_date, event_type),
    ).fetchone()
    return row["count"]


def group_events(
    conn, report_date, event_type, group_by,
    include_simulation, include_experimental
):
    label_sql = {
        "company": company_label("c"),
        "expertise": "COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown')",
    }[group_by]
    simulation_filter = "" if include_simulation else "AND j.title NOT LIKE '[SIMULATION]%'"

    return conn.execute(
        f"""
        SELECT {label_sql} AS label, COUNT(*) AS count
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        JOIN companies c ON c.id = j.company_id
        WHERE date(je.created_at) = ?
          AND je.event_type = ?
          {live_market_filter("c", "j")}
          {simulation_filter}
          {experimental_filter("c", include_experimental)}
        GROUP BY label
        ORDER BY count DESC, label ASC
        """,
        (report_date, event_type),
    ).fetchall()


def get_recent_events(conn, report_date, include_simulation, include_experimental, limit=10):
    simulation_filter = "" if include_simulation else "AND j.title NOT LIKE '[SIMULATION]%'"
    return conn.execute(
        f"""
        SELECT
          je.event_type,
          je.created_at,
          {company_label("c")} AS company_name,
          j.title,
          j.location,
          COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown') AS expertise_label,
          j.url,
          CASE WHEN j.title LIKE '[SIMULATION]%' THEN 1 ELSE 0 END AS is_simulation
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        JOIN companies c ON c.id = j.company_id
        WHERE date(je.created_at) = ?
          {live_market_filter("c", "j")}
          {simulation_filter}
          {experimental_filter("c", include_experimental)}
        ORDER BY je.created_at DESC, je.id DESC
        LIMIT ?
        """,
        (report_date, limit),
    ).fetchall()


def print_count_section(title, rows):
    print(title)
    print("-" * len(title))
    if not rows:
        print("  None")
        print("")
        return

    for row in rows:
        print(f"  {row['label']}: {row['count']}")
    print("")


def print_events(title, rows):
    print(title)
    print("-" * len(title))
    if not rows:
        print("  None")
        print("")
        return

    for row in rows:
        label = f"{row['event_type']} SIMULATION" if row["is_simulation"] else row["event_type"]
        print(f"  [{label}] {row['created_at']}")
        print(f"    Company:   {row['company_name']}")
        print(f"    Title:     {row['title']}")
        print(f"    Location:  {row['location'] or 'Unknown'}")
        print(f"    Expertise: {row['expertise_label']}")
        print(f"    URL:       {row['url']}")
    print("")


if __name__ == "__main__":
    main()
