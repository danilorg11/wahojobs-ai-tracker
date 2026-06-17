import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.db.connection import get_connection
from wahojobs.reporting.micro1 import get_micro1_metrics


def main():
    args = parse_args()
    with get_connection() as conn:
        summary = get_summary(conn, args.include_experimental)
        duplicate_external_ids = get_duplicate_external_ids(conn, args.include_experimental)
        duplicate_urls = get_duplicate_urls(conn, args.include_experimental)
        companies_without_success = get_companies_without_successful_crawl(conn, args.include_experimental)
        failed_crawls = get_failed_crawls(conn, args.include_experimental)
        last_statuses = get_last_crawl_status_by_company(conn, args.include_experimental)
        alignerr_summary = get_alignerr_canonical_summary(conn)
        top_alignerr_variants = get_top_alignerr_variants(conn)
        multi_location_groups = get_multi_location_alignerr_groups(conn)
        multi_rate_groups = get_multi_rate_alignerr_groups(conn)
        unknown_canonical_groups = get_unknown_alignerr_canonical_groups(conn)
        oneforma_summary = get_company_canonical_summary(conn, "oneforma")
        top_oneforma_variants = get_top_company_variants(conn, "oneforma")
        micro1_metrics = get_micro1_metrics(conn)

    print("")
    print("Wahojobs Data Quality Report")
    print("============================")
    print(f"Experimental sources: {'included' if args.include_experimental else 'excluded'}")
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
    print("")
    print("OneForma Canonical Checks")
    print("-------------------------")
    if oneforma_summary and oneforma_summary["raw_postings"]:
        print(f"Raw active variants: {oneforma_summary['raw_postings']}")
        print(f"Canonical active opportunities: {oneforma_summary['canonical_opportunities']}")
        print(f"Posting variants: {oneforma_summary['variant_count']}")
        print(f"Unlinked active variants: {oneforma_summary['unlinked_postings']}")
    else:
        print("No active OneForma variants found.")
    print("")

    print_rows(
        "Top OneForma canonical opportunities by variant count",
        top_oneforma_variants,
        lambda row: (
            f"{row['canonical_title']} ({row['source_category']}): "
            f"{row['variant_count']} variants"
        ),
    )
    print("")
    print("micro1 Duplicate-Title Checks")
    print("-----------------------------")
    print(f"micro1 active jobs: {micro1_metrics['active_jobs']}")
    print(f"micro1 unique titles: {micro1_metrics['unique_titles']}")
    print(f"micro1 duplicate-title count: {micro1_metrics['duplicate_title_count']}")
    print("")


def parse_args():
    parser = argparse.ArgumentParser(description="Show Wahojobs data quality checks.")
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


def scalar_count(conn, sql):
    return conn.execute(sql).fetchone()[0]


def get_summary(conn, include_experimental):
    source_filter = experimental_filter("c", include_experimental)
    checks = [
        ("Total jobs", f"SELECT COUNT(*) FROM jobs j JOIN companies c ON c.id = j.company_id WHERE 1 = 1 {source_filter}"),
        ("Active jobs", f"SELECT COUNT(*) FROM jobs j JOIN companies c ON c.id = j.company_id WHERE j.is_active = 1 {source_filter}"),
        ("Inactive jobs", f"SELECT COUNT(*) FROM jobs j JOIN companies c ON c.id = j.company_id WHERE j.is_active = 0 {source_filter}"),
        (
            "Jobs missing title",
            f"""
            SELECT COUNT(*)
            FROM jobs j JOIN companies c ON c.id = j.company_id
            WHERE (j.title IS NULL OR TRIM(j.title) = '')
              {source_filter}
            """,
        ),
        (
            "Jobs missing URL",
            f"""
            SELECT COUNT(*)
            FROM jobs j JOIN companies c ON c.id = j.company_id
            WHERE (j.url IS NULL OR TRIM(j.url) = '')
              {source_filter}
            """,
        ),
        (
            "Jobs missing location",
            f"""
            SELECT COUNT(*)
            FROM jobs j JOIN companies c ON c.id = j.company_id
            WHERE (j.location IS NULL OR TRIM(j.location) = '')
              {source_filter}
            """,
        ),
        (
            "Active jobs with Unknown expertise/department",
            f"""
            SELECT COUNT(*)
            FROM jobs j JOIN companies c ON c.id = j.company_id
            WHERE COALESCE(NULLIF(TRIM(j.expertise), ''), NULLIF(TRIM(j.department), ''), 'Unknown') = 'Unknown'
              AND j.is_active = 1
              {source_filter}
            """,
        ),
        (
            "Jobs with simulation titles",
            f"""
            SELECT COUNT(*)
            FROM jobs j JOIN companies c ON c.id = j.company_id
            WHERE j.title LIKE '[SIMULATION]%'
              {source_filter}
            """,
        ),
    ]

    rows = []
    for label, sql in checks:
        count = scalar_count(conn, sql)
        rows.append((label, count))
    return rows


def get_alignerr_canonical_summary(conn):
    return get_company_canonical_summary(conn, "alignerr")


def get_company_canonical_summary(conn, slug):
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
        WHERE c.slug = ?
        """,
        (slug,),
    ).fetchone()


def get_top_alignerr_variants(conn):
    return get_top_company_variants(conn, "alignerr")


def get_top_company_variants(conn, slug):
    return conn.execute(
        """
        SELECT co.canonical_title, co.source_category, co.variant_count
        FROM canonical_opportunities co
        JOIN companies c ON c.id = co.company_id
        WHERE c.slug = ?
          AND co.is_active = 1
        ORDER BY co.variant_count DESC, co.canonical_title ASC
        LIMIT 20
        """,
        (slug,),
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


def get_duplicate_external_ids(conn, include_experimental):
    return conn.execute(
        f"""
        SELECT {company_label("c")} AS company, j.external_id, COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.external_id IS NOT NULL
          AND TRIM(j.external_id) != ''
          {experimental_filter("c", include_experimental)}
        GROUP BY j.company_id, j.external_id
        HAVING COUNT(*) > 1
        ORDER BY count DESC, c.name ASC, j.external_id ASC
        """
    ).fetchall()


def get_duplicate_urls(conn, include_experimental):
    return conn.execute(
        f"""
        SELECT {company_label("c")} AS company, j.url, COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.url IS NOT NULL
          AND TRIM(j.url) != ''
          {experimental_filter("c", include_experimental)}
        GROUP BY j.company_id, j.url
        HAVING COUNT(*) > 1
        ORDER BY count DESC, c.name ASC, j.url ASC
        """
    ).fetchall()


def get_companies_without_successful_crawl(conn, include_experimental):
    return conn.execute(
        f"""
        SELECT {company_label("c")} AS company
        FROM companies c
        WHERE NOT EXISTS (
          SELECT 1
          FROM crawl_runs cr
          WHERE cr.company_id = c.id
            AND cr.status = 'success'
        )
          {experimental_filter("c", include_experimental)}
        ORDER BY c.name ASC
        """
    ).fetchall()


def get_failed_crawls(conn, include_experimental):
    return conn.execute(
        f"""
        SELECT {company_label("c")} AS company, cr.started_at, cr.error_message
        FROM crawl_runs cr
        JOIN companies c ON c.id = cr.company_id
        WHERE cr.status = 'failed'
          {experimental_filter("c", include_experimental)}
        ORDER BY cr.started_at DESC
        LIMIT 20
        """
    ).fetchall()


def get_last_crawl_status_by_company(conn, include_experimental):
    return conn.execute(
        f"""
        SELECT
          {company_label("c")} AS company,
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
        WHERE 1 = 1
          {experimental_filter("c", include_experimental)}
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
