import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.db.connection import get_connection


def main():
    args = parse_args()
    today = datetime.now(timezone.utc).date().isoformat()

    with get_connection() as conn:
        print("")
        print("Wahojobs Stats")
        print("=" * 14)
        print(f"Date: {today} UTC")
        print(f"Experimental sources: {'included' if args.include_experimental else 'excluded'}")
        print("")

        print(f"Total active raw postings: {count_total_active_jobs(conn, args.include_experimental)}")
        alignerr = get_alignerr_canonical_summary(conn)
        if alignerr:
            print(f"Alignerr active raw postings: {alignerr['raw_postings']}")
            print(f"Alignerr active canonical opportunities: {alignerr['canonical_opportunities']}")
            print(f"Alignerr active posting variants: {alignerr['variant_count']}")
        print("")

        print_section("Jobs by company", get_jobs_by_company(conn, args.include_experimental))
        print_section("Jobs by expertise/department", get_jobs_by_expertise(conn, args.include_experimental))

        print(f"New jobs today: {count_new_jobs_today(conn, today, args.include_experimental)}")
        print(f"Removed jobs today: {count_removed_jobs_today(conn, today, args.include_experimental)}")
        print("")

        print_section("Last successful crawl per company", get_last_crawls(conn, args.include_experimental))


def parse_args():
    parser = argparse.ArgumentParser(description="Show Wahojobs stats.")
    parser.add_argument(
        "--include-experimental",
        action="store_true",
        help="Include non-core/experimental sources such as Invisible.",
    )
    return parser.parse_args()


def experimental_filter(alias, include_experimental):
    if include_experimental:
        return ""
    return f"AND {alias}.slug != 'invisible'"


def company_label(alias):
    return (
        f"CASE WHEN {alias}.slug = 'invisible' "
        f"THEN {alias}.name || ' [EXPERIMENTAL]' ELSE {alias}.name END"
    )


def count_total_active_jobs(conn, include_experimental):
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          {experimental_filter("c", include_experimental)}
        """
    ).fetchone()
    return row["count"]


def get_alignerr_canonical_summary(conn):
    row = conn.execute(
        """
        SELECT
          COUNT(j.id) AS raw_postings,
          COUNT(DISTINCT co.id) AS canonical_opportunities,
          COUNT(j.id) - COUNT(DISTINCT co.id) AS variant_count
        FROM companies c
        LEFT JOIN jobs j
          ON j.company_id = c.id
         AND j.is_active = 1
         AND j.title NOT LIKE '[SIMULATION]%'
        LEFT JOIN canonical_opportunities co
          ON co.id = j.canonical_opportunity_id
         AND co.is_active = 1
        WHERE c.slug = 'alignerr'
        """
    ).fetchone()
    if row is None or row["raw_postings"] == 0:
        return None
    return row


def get_jobs_by_company(conn, include_experimental):
    return conn.execute(
        f"""
        SELECT {company_label("c")} AS label, COUNT(j.id) AS count
        FROM companies c
        LEFT JOIN jobs j
          ON j.company_id = c.id
         AND j.is_active = 1
        WHERE 1 = 1
          {experimental_filter("c", include_experimental)}
        GROUP BY c.id, c.name
        ORDER BY count DESC, c.name ASC
        """
    ).fetchall()


def get_jobs_by_expertise(conn, include_experimental):
    return conn.execute(
        f"""
        SELECT
          COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown') AS label,
          COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          {experimental_filter("c", include_experimental)}
        GROUP BY label
        ORDER BY count DESC, label ASC
        """
    ).fetchall()


def count_new_jobs_today(conn, today, include_experimental):
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE date(j.first_seen_at) = ?
          {experimental_filter("c", include_experimental)}
        """,
        (today,),
    ).fetchone()
    return row["count"]


def count_removed_jobs_today(conn, today, include_experimental):
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.removed_at IS NOT NULL
          AND date(j.removed_at) = ?
          {experimental_filter("c", include_experimental)}
        """,
        (today,),
    ).fetchone()
    return row["count"]


def get_last_crawls(conn, include_experimental):
    return conn.execute(
        f"""
        SELECT
          {company_label("c")} AS label,
          COALESCE(MAX(cr.finished_at), 'Never') AS value
        FROM companies c
        LEFT JOIN crawl_runs cr
          ON cr.company_id = c.id
         AND cr.status = 'success'
        WHERE 1 = 1
          {experimental_filter("c", include_experimental)}
        GROUP BY c.id, c.name
        ORDER BY c.name ASC
        """
    ).fetchall()


def print_section(title, rows):
    print(title)
    print("-" * len(title))
    if not rows:
        print("  None")
        print("")
        return

    for row in rows:
        if "count" in row.keys():
            print(f"  {row['label']}: {row['count']}")
        else:
            print(f"  {row['label']}: {row['value']}")
    print("")


if __name__ == "__main__":
    main()
