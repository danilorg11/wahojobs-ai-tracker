import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.db.connection import get_connection


def main():
    with get_connection() as conn:
        summary = get_summary(conn)
        duplicate_external_ids = get_duplicate_external_ids(conn)
        duplicate_urls = get_duplicate_urls(conn)
        companies_without_success = get_companies_without_successful_crawl(conn)
        failed_crawls = get_failed_crawls(conn)
        last_statuses = get_last_crawl_status_by_company(conn)

    print("")
    print("Wahojobs Data Quality Report")
    print("============================")
    print("")

    print("Job Checks")
    print("----------")
    for label, value in summary:
        print(f"{label}: {value}")
    print("")

    print_rows(
        "Duplicate external_id within same company",
        duplicate_external_ids,
        lambda row: f"{row['company']}: {row['external_id']} ({row['count']} jobs)",
    )
    print_rows(
        "Duplicate URL within same company",
        duplicate_urls,
        lambda row: f"{row['company']}: {row['url']} ({row['count']} jobs)",
    )
    print_rows(
        "Companies with no successful crawl",
        companies_without_success,
        lambda row: row["company"],
    )
    print_rows(
        "Failed crawl runs",
        failed_crawls,
        lambda row: f"{row['company']}: {row['started_at']} - {row['error_message'] or 'No error message'}",
    )
    print_rows(
        "Last crawl status by company",
        last_statuses,
        lambda row: f"{row['company']}: {row['status'] or 'Never'} at {row['finished_at'] or row['started_at'] or 'N/A'}",
    )


def get_summary(conn):
    checks = [
        ("Total jobs", "SELECT COUNT(*) FROM jobs"),
        ("Active jobs", "SELECT COUNT(*) FROM jobs WHERE is_active = 1"),
        ("Inactive jobs", "SELECT COUNT(*) FROM jobs WHERE is_active = 0"),
        ("Jobs missing title", "SELECT COUNT(*) FROM jobs WHERE title IS NULL OR TRIM(title) = ''"),
        ("Jobs missing URL", "SELECT COUNT(*) FROM jobs WHERE url IS NULL OR TRIM(url) = ''"),
        ("Jobs missing location", "SELECT COUNT(*) FROM jobs WHERE location IS NULL OR TRIM(location) = ''"),
        (
            "Jobs with Unknown expertise/department",
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE COALESCE(NULLIF(TRIM(expertise), ''), NULLIF(TRIM(department), ''), 'Unknown') = 'Unknown'
            """,
        ),
        ("Jobs with simulation titles", "SELECT COUNT(*) FROM jobs WHERE title LIKE '[SIMULATION]%'"),
    ]

    rows = []
    for label, sql in checks:
        count = conn.execute(sql).fetchone()[0]
        rows.append((label, count))
    return rows


def get_duplicate_external_ids(conn):
    return conn.execute(
        """
        SELECT c.name AS company, j.external_id, COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.external_id IS NOT NULL
          AND TRIM(j.external_id) != ''
        GROUP BY j.company_id, j.external_id
        HAVING COUNT(*) > 1
        ORDER BY count DESC, c.name ASC, j.external_id ASC
        """
    ).fetchall()


def get_duplicate_urls(conn):
    return conn.execute(
        """
        SELECT c.name AS company, j.url, COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.url IS NOT NULL
          AND TRIM(j.url) != ''
        GROUP BY j.company_id, j.url
        HAVING COUNT(*) > 1
        ORDER BY count DESC, c.name ASC, j.url ASC
        """
    ).fetchall()


def get_companies_without_successful_crawl(conn):
    return conn.execute(
        """
        SELECT c.name AS company
        FROM companies c
        WHERE NOT EXISTS (
          SELECT 1
          FROM crawl_runs cr
          WHERE cr.company_id = c.id
            AND cr.status = 'success'
        )
        ORDER BY c.name ASC
        """
    ).fetchall()


def get_failed_crawls(conn):
    return conn.execute(
        """
        SELECT c.name AS company, cr.started_at, cr.error_message
        FROM crawl_runs cr
        JOIN companies c ON c.id = cr.company_id
        WHERE cr.status = 'failed'
        ORDER BY cr.started_at DESC
        LIMIT 20
        """
    ).fetchall()


def get_last_crawl_status_by_company(conn):
    return conn.execute(
        """
        SELECT
          c.name AS company,
          cr.status,
          cr.started_at,
          cr.finished_at
        FROM companies c
        LEFT JOIN crawl_runs cr
          ON cr.id = (
            SELECT cr2.id
            FROM crawl_runs cr2
            WHERE cr2.company_id = c.id
            ORDER BY cr2.started_at DESC
            LIMIT 1
          )
        ORDER BY c.name ASC
        """
    ).fetchall()


def print_rows(title, rows, formatter):
    print(title)
    print("-" * len(title))
    if not rows:
        print("  None")
        print("")
        return

    for row in rows:
        print(f"  {formatter(row)}")
    print("")


if __name__ == "__main__":
    main()
