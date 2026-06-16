import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.db.connection import get_connection


def main():
    args = parse_args()
    report_date = args.report_date

    with get_connection() as conn:
        total_active = count_total_active_jobs(conn, args.include_simulation)
        discovered = count_events(conn, report_date, "discovered", args.include_simulation)
        removed = count_events(conn, report_date, "removed", args.include_simulation)
        reactivated = count_events(conn, report_date, "reactivated", args.include_simulation)
        new_by_company = group_events(conn, report_date, "discovered", "company", args.include_simulation)
        removed_by_company = group_events(conn, report_date, "removed", "company", args.include_simulation)
        new_by_expertise = group_events(conn, report_date, "discovered", "expertise", args.include_simulation)
        removed_by_expertise = group_events(conn, report_date, "removed", "expertise", args.include_simulation)
        recent_events = get_recent_events(conn, report_date, args.include_simulation, limit=10)

    print("")
    print("Wahojobs Daily Market Report")
    print("============================")
    print(f"Report date: {report_date} UTC")
    print(f"Simulation: {'included' if args.include_simulation else 'excluded'}")
    print("")
    print(f"Total active jobs:        {total_active}")
    print(f"New jobs today:           {discovered}")
    print(f"Removed jobs today:       {removed}")
    print(f"Reactivated jobs today:   {reactivated}")
    print("")

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
    args = parser.parse_args()
    validate_date(args.report_date)
    return args


def validate_date(value):
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise SystemExit("--date must use YYYY-MM-DD format")


def count_total_active_jobs(conn, include_simulation):
    simulation_filter = "" if include_simulation else "AND title NOT LIKE '[SIMULATION]%'"
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM jobs
        WHERE is_active = 1
          {simulation_filter}
        """
    ).fetchone()
    return row["count"]


def count_events(conn, report_date, event_type, include_simulation):
    simulation_filter = "" if include_simulation else "AND j.title NOT LIKE '[SIMULATION]%'"
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        WHERE date(je.created_at) = ?
          AND je.event_type = ?
          {simulation_filter}
        """,
        (report_date, event_type),
    ).fetchone()
    return row["count"]


def group_events(conn, report_date, event_type, group_by, include_simulation):
    label_sql = {
        "company": "c.name",
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
          {simulation_filter}
        GROUP BY label
        ORDER BY count DESC, label ASC
        """,
        (report_date, event_type),
    ).fetchall()


def get_recent_events(conn, report_date, include_simulation, limit=10):
    simulation_filter = "" if include_simulation else "AND j.title NOT LIKE '[SIMULATION]%'"
    return conn.execute(
        f"""
        SELECT
          je.event_type,
          je.created_at,
          c.name AS company_name,
          j.title,
          j.location,
          COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown') AS expertise_label,
          j.url,
          CASE WHEN j.title LIKE '[SIMULATION]%' THEN 1 ELSE 0 END AS is_simulation
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        JOIN companies c ON c.id = j.company_id
        WHERE date(je.created_at) = ?
          {simulation_filter}
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
