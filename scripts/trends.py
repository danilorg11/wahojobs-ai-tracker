import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.db.connection import get_connection


def main():
    args = parse_args()
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=args.days - 1)

    with get_connection() as conn:
        total_active = count_total_active_jobs(conn, args.include_simulation)
        alignerr_canonical = get_alignerr_canonical_summary(conn, args.include_simulation)
        active_by_company = group_active_jobs(conn, "company", args.include_simulation)
        active_by_expertise = group_active_jobs(conn, "expertise", args.include_simulation)
        new_by_company = group_events(conn, start_date, end_date, "discovered", "company", args.include_simulation)
        removed_by_company = group_events(conn, start_date, end_date, "removed", "company", args.include_simulation)
        reactivated_by_company = group_events(conn, start_date, end_date, "reactivated", "company", args.include_simulation)
        new_by_expertise = group_events(conn, start_date, end_date, "discovered", "expertise", args.include_simulation)
        removed_by_expertise = group_events(conn, start_date, end_date, "removed", "expertise", args.include_simulation)

    print("")
    print("Wahojobs Market Trends")
    print("======================")
    print(f"Period: {start_date.isoformat()} to {end_date.isoformat()} UTC ({args.days} days)")
    print(f"Simulation: {'included' if args.include_simulation else 'excluded'}")
    print("")
    print(f"Total active raw postings: {total_active}")
    if alignerr_canonical:
        print(f"Alignerr active raw postings: {alignerr_canonical['raw_postings']}")
        print(
            "Alignerr active canonical opportunities: "
            f"{alignerr_canonical['canonical_opportunities']}"
        )
        print(f"Alignerr active posting variants: {alignerr_canonical['variant_count']}")
    print("")

    print_count_section("Active jobs by company", active_by_company)
    print_count_section("Active jobs by expertise/department", active_by_expertise)
    print_count_section("Top companies by active jobs", active_by_company[:10])
    print_count_section("Top expertise/department by active jobs", active_by_expertise[:10])

    print_count_section("New jobs by company", new_by_company)
    print_count_section("Removed jobs by company", removed_by_company)
    print_count_section("Reactivated jobs by company", reactivated_by_company)
    print_count_section("New jobs by expertise/department", new_by_expertise)
    print_count_section("Removed jobs by expertise/department", removed_by_expertise)


def parse_args():
    parser = argparse.ArgumentParser(description="Show Wahojobs market trends.")
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Lookback period in days. Defaults to 7.",
    )
    parser.add_argument(
        "--include-simulation",
        action="store_true",
        help="Include local simulation jobs and events.",
    )
    args = parser.parse_args()
    if args.days < 1:
        raise SystemExit("--days must be at least 1")
    return args


def simulation_filter(alias, include_simulation):
    if include_simulation:
        return ""
    return f"AND {alias}.title NOT LIKE '[SIMULATION]%'"


def expertise_label(alias):
    return f"COALESCE(NULLIF(TRIM({alias}.expertise), ''), NULLIF(TRIM({alias}.department), ''), 'Unknown')"


def count_total_active_jobs(conn, include_simulation):
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM jobs j
        WHERE j.is_active = 1
          {simulation_filter("j", include_simulation)}
        """
    ).fetchone()
    return row["count"]


def get_alignerr_canonical_summary(conn, include_simulation):
    row = conn.execute(
        f"""
        SELECT
          COUNT(j.id) AS raw_postings,
          COUNT(DISTINCT co.id) AS canonical_opportunities,
          COUNT(j.id) - COUNT(DISTINCT co.id) AS variant_count
        FROM companies c
        LEFT JOIN jobs j
          ON j.company_id = c.id
         AND j.is_active = 1
         {simulation_filter("j", include_simulation)}
        LEFT JOIN canonical_opportunities co
          ON co.id = j.canonical_opportunity_id
         AND co.is_active = 1
        WHERE c.slug = 'alignerr'
        """
    ).fetchone()
    if row is None or row["raw_postings"] == 0:
        return None
    return row


def group_active_jobs(conn, group_by, include_simulation):
    label_sql = {
        "company": "c.name",
        "expertise": expertise_label("j"),
    }[group_by]

    return conn.execute(
        f"""
        SELECT {label_sql} AS label, COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          {simulation_filter("j", include_simulation)}
        GROUP BY label
        ORDER BY count DESC, label ASC
        """
    ).fetchall()


def group_events(conn, start_date, end_date, event_type, group_by, include_simulation):
    label_sql = {
        "company": "c.name",
        "expertise": expertise_label("j"),
    }[group_by]

    return conn.execute(
        f"""
        SELECT {label_sql} AS label, COUNT(*) AS count
        FROM job_events je
        JOIN jobs j ON j.id = je.job_id
        JOIN companies c ON c.id = j.company_id
        WHERE date(je.created_at) BETWEEN ? AND ?
          AND je.event_type = ?
          {simulation_filter("j", include_simulation)}
        GROUP BY label
        ORDER BY count DESC, label ASC
        """,
        (start_date.isoformat(), end_date.isoformat(), event_type),
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


if __name__ == "__main__":
    main()
