import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.db.connection import get_connection


def main():
    today = datetime.now(timezone.utc).date().isoformat()

    with get_connection() as conn:
        print("")
        print("Wahojobs Stats")
        print("=" * 14)
        print(f"Date: {today} UTC")
        print("")

        print(f"Total active raw postings: {count_total_active_jobs(conn)}")
        alignerr = get_alignerr_canonical_summary(conn)
        if alignerr:
            print(f"Alignerr active raw postings: {alignerr['raw_postings']}")
            print(f"Alignerr active canonical opportunities: {alignerr['canonical_opportunities']}")
            print(f"Alignerr active posting variants: {alignerr['variant_count']}")
        print("")

        print_section("Jobs by company", get_jobs_by_company(conn))
        print_section("Jobs by expertise/department", get_jobs_by_expertise(conn))

        print(f"New jobs today: {count_new_jobs_today(conn, today)}")
        print(f"Removed jobs today: {count_removed_jobs_today(conn, today)}")
        print("")

        print_section("Last successful crawl per company", get_last_crawls(conn))


def count_total_active_jobs(conn):
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM jobs
        WHERE is_active = 1
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


def get_jobs_by_company(conn):
    return conn.execute(
        """
        SELECT c.name AS label, COUNT(j.id) AS count
        FROM companies c
        LEFT JOIN jobs j
          ON j.company_id = c.id
         AND j.is_active = 1
        GROUP BY c.id, c.name
        ORDER BY count DESC, c.name ASC
        """
    ).fetchall()


def get_jobs_by_expertise(conn):
    return conn.execute(
        """
        SELECT
          COALESCE(NULLIF(TRIM(expertise), ''), NULLIF(TRIM(department), ''), 'Unknown') AS label,
          COUNT(*) AS count
        FROM jobs
        WHERE is_active = 1
        GROUP BY label
        ORDER BY count DESC, label ASC
        """
    ).fetchall()


def count_new_jobs_today(conn, today):
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM jobs
        WHERE date(first_seen_at) = ?
        """,
        (today,),
    ).fetchone()
    return row["count"]


def count_removed_jobs_today(conn, today):
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM jobs
        WHERE removed_at IS NOT NULL
          AND date(removed_at) = ?
        """,
        (today,),
    ).fetchone()
    return row["count"]


def get_last_crawls(conn):
    return conn.execute(
        """
        SELECT
          c.name AS label,
          COALESCE(MAX(cr.finished_at), 'Never') AS value
        FROM companies c
        LEFT JOIN crawl_runs cr
          ON cr.company_id = c.id
         AND cr.status = 'success'
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
