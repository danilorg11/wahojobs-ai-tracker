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
        alignerr_summary = get_alignerr_canonical_summary(conn)
        top_alignerr_variants = get_top_alignerr_variants(conn)
        multi_location_groups = get_multi_location_alignerr_groups(conn)
        multi_rate_groups = get_multi_rate_alignerr_groups(conn)
        unknown_canonical_groups = get_unknown_alignerr_canonical_groups(conn)

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
    print("")
    print("Alignerr Canonical Checks")
    print("-------------------------")
    if alignerr_summary:
        print(f"Raw active postings: {alignerr_summary['raw_postings']}")
        print(f"Canonical active opportunities: {alignerr_summary['canonical_opportunities']}")
        print(f"Posting variants: {alignerr_summary['variant_count']}")
        print(f"Unlinked active postings: {alignerr_summary['unlinked_postings']}")
    else:
        print("No active Alignerr postings found.")
    print("")

    print_rows(
        "Top Alignerr canonical opportunities by variant count",
        top_alignerr_variants,
        lambda row: (
            f"{row['canonical_title']} ({row['source_category']}): "
            f"{row['variant_count']} variants"
        ),
    )
    print_rows(
        "Alignerr multi-location canonical opportunities",
        multi_location_groups,
        lambda row: (
            f"{row['canonical_title']} ({row['source_category']}): "
            f"{row['location_count']} locations, {row['variant_count']} variants"
        ),
    )
    print_rows(
        "Alignerr multi-rate canonical opportunities",
        multi_rate_groups,
        lambda row: f"{row['canonical_title']}: {row['rate_count']} rate groups",
    )
    print_rows(
        "Unknown/low-confidence Alignerr canonical groups",
        unknown_canonical_groups,
        lambda row: f"{row['canonical_title']} ({row['source_category']}): {row['variant_count']} variants",
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


def get_alignerr_canonical_summary(conn):
    return conn.execute(
        """
        SELECT
          COUNT(j.id) AS raw_postings,
          COUNT(DISTINCT co.id) AS canonical_opportunities,
          COUNT(j.id) - COUNT(DISTINCT co.id) AS variant_count,
          SUM(CASE WHEN j.canonical_opportunity_id IS NULL THEN 1 ELSE 0 END) AS unlinked_postings
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


def get_top_alignerr_variants(conn):
    return conn.execute(
        """
        SELECT co.canonical_title, co.source_category, co.variant_count
        FROM canonical_opportunities co
        JOIN companies c ON c.id = co.company_id
        WHERE c.slug = 'alignerr'
          AND co.is_active = 1
        ORDER BY co.variant_count DESC, co.canonical_title ASC
        LIMIT 20
        """
    ).fetchall()


def get_multi_location_alignerr_groups(conn):
    return conn.execute(
        """
        SELECT
          co.canonical_title,
          co.source_category,
          co.variant_count,
          COUNT(DISTINCT j.location) AS location_count
        FROM canonical_opportunities co
        JOIN companies c ON c.id = co.company_id
        JOIN jobs j ON j.canonical_opportunity_id = co.id
        WHERE c.slug = 'alignerr'
          AND co.is_active = 1
          AND j.is_active = 1
        GROUP BY co.id
        HAVING location_count > 1
        ORDER BY location_count DESC, co.variant_count DESC, co.canonical_title ASC
        LIMIT 20
        """
    ).fetchall()


def get_multi_rate_alignerr_groups(conn):
    return conn.execute(
        """
        SELECT co.canonical_title, co.source_category, 0 AS rate_count
        FROM canonical_opportunities co
        WHERE 1 = 0
        """
    ).fetchall()


def get_unknown_alignerr_canonical_groups(conn):
    return conn.execute(
        """
        SELECT co.canonical_title, co.source_category, co.variant_count
        FROM canonical_opportunities co
        JOIN companies c ON c.id = co.company_id
        WHERE c.slug = 'alignerr'
          AND co.is_active = 1
          AND (
            co.source_category IS NULL
            OR TRIM(co.source_category) = ''
            OR co.source_category IN ('Unknown', 'OTHER')
          )
        ORDER BY co.variant_count DESC, co.canonical_title ASC
        LIMIT 20
        """
    ).fetchall()


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
