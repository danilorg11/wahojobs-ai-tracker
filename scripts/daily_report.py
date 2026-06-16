import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.db.connection import get_connection


def main():
    report_date = parse_args().report_date

    with get_connection() as conn:
        total_active = count_total_active_jobs(conn)
        discovered = count_events(conn, report_date, "discovered")
        removed = count_events(conn, report_date, "removed")
        reactivated = count_events(conn, report_date, "reactivated")
        new_by_company = group_events(conn, report_date, "discovered", "company")
        removed_by_company = group_events(conn, report_date, "removed", "company")
        new_by_expertise = group_events(conn, report_date, "discovered", "expertise")
        removed_by_expertise = group_events(conn, report_date, "removed", "expertise")
        recent_events = get_recent_events(conn, report_date, limit=10)

    print("")
    print("Wahojobs Daily Market Report")
    print("============================")
    print(f"Report date: {report_date} UTC")
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
    args = parser.parse_args()
    validate_date(args.report_date)
    return args


def validate_date(value):
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise SystemExit("--date must use YYYY-MM-DD format")


def count_total_active_jobs(conn):
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM jobs
        WHERE is_active = 1
        """
    ).fetchone()
    return row["count"]


def count_events(conn, report_date, event_type):
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM job_events
        WHERE date(created_at) = ?
          AND event_type = ?
        """,
        (report_date, event_type),
    ).fetchone()
    return row["count"]


def group_events(conn, report_date, event_type, group_by):
    label_sql = {
        "company": "c.name",
        "expertise": "COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown')",
    }[group_by]

    return conn.execute(
        f"""
        SELECT {label_sql} AS label, COUNT(*) AS count
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        JOIN companies c ON c.id = j.company_id
        WHERE date(je.created_at) = ?
          AND je.event_type = ?
        GROUP BY label
        ORDER BY count DESC, label ASC
        """,
        (report_date, event_type),
    ).fetchall()


def get_recent_events(conn, report_date, limit=10):
    return conn.execute(
        """
        SELECT
          je.event_type,
          je.created_at,
          c.name AS company_name,
          j.title,
          j.location,
          COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown') AS expertise_label,
          j.url
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        JOIN companies c ON c.id = j.company_id
        WHERE date(je.created_at) = ?
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
        print(f"  [{row['event_type']}] {row['created_at']}")
        print(f"    Company:   {row['company_name']}")
        print(f"    Title:     {row['title']}")
        print(f"    Location:  {row['location'] or 'Unknown'}")
        print(f"    Expertise: {row['expertise_label']}")
        print(f"    URL:       {row['url']}")
    print("")


if __name__ == "__main__":
    main()
