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
    today = datetime.now(timezone.utc).date().isoformat()

    with get_connection() as conn:
        print("")
        print("Wahojobs Stats")
        print("=" * 14)
        print(f"Date: {today} UTC")
        print(
            "Experimental sources: "
            f"{experimental_sources_status(args.include_experimental)}"
        )
        print("")

        print_market_size_summary(conn, args.include_experimental)
        print("")
        print_micro1_metrics(conn)
        print("")
        print_classification_summary(conn, args.include_experimental)
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


def print_market_size_summary(conn, include_experimental):
    summary = get_market_size_summary(
        conn,
        include_experimental=include_experimental,
        include_simulation=False,
    )
    print(f"Raw active postings: {summary['raw_active_postings']}")
    print(f"Estimated market opportunities: {summary['estimated_market_opportunities']}")
    print(
        "Estimate: canonicalized sources where available; "
        "raw active jobs elsewhere."
    )
    print(f"Alignerr raw postings: {summary['alignerr_raw_postings']}")
    print(f"Alignerr canonical opportunities: {summary['alignerr_canonical_opportunities']}")
    print(f"Alignerr posting variants: {summary['alignerr_posting_variants']}")
    print(f"DataForce raw postings: {summary['dataforce_raw_postings']}")
    print(f"DataForce canonical opportunities: {summary['dataforce_canonical_opportunities']}")
    print(f"DataForce posting variants: {summary['dataforce_posting_variants']}")
    print(f"Meridial raw postings: {summary['meridial_raw_postings']}")
    print(f"Meridial canonical opportunities: {summary['meridial_canonical_opportunities']}")
    print(f"Meridial posting variants: {summary['meridial_posting_variants']}")
    print(f"Mindrift raw postings: {summary['mindrift_raw_postings']}")
    print(f"Mindrift canonical opportunities: {summary['mindrift_canonical_opportunities']}")
    print(f"Mindrift posting variants: {summary['mindrift_posting_variants']}")
    print(f"micro1 raw postings: {summary['micro1_raw_postings']}")
    print(f"micro1 canonical opportunities: {summary['micro1_canonical_opportunities']}")
    print(f"micro1 posting variants: {summary['micro1_posting_variants']}")
    print(f"OneForma raw variants: {summary['oneforma_raw_variants']}")
    print(f"OneForma canonical opportunities: {summary['oneforma_canonical_opportunities']}")
    print(f"OneForma posting variants: {summary['oneforma_posting_variants']}")
    print(f"Turing raw postings: {summary['turing_raw_postings']}")
    print(f"Turing canonical opportunities: {summary['turing_canonical_opportunities']}")
    print(f"Turing posting variants: {summary['turing_posting_variants']}")
    print(f"Welocalize raw postings: {summary['welocalize_raw_postings']}")
    print(
        "Welocalize canonical opportunities: "
        f"{summary['welocalize_canonical_opportunities']}"
    )
    print(f"Welocalize posting variants: {summary['welocalize_posting_variants']}")


def print_micro1_metrics(conn):
    metrics = get_micro1_metrics(conn)
    print("micro1 Marketplace Metrics")
    print("--------------------------")
    print(f"micro1 active jobs: {metrics['active_jobs']}")
    print(f"micro1 unique titles: {metrics['unique_titles']}")
    print(f"micro1 duplicate-title count: {metrics['duplicate_title_count']}")


def print_classification_summary(conn, include_experimental):
    summary = get_classification_summary(
        conn,
        include_experimental=include_experimental,
        include_simulation=False,
    )
    print("Classification Summary")
    print("----------------------")
    print_section("Sources by tier", summary["source_tiers"])
    print_section("Active jobs by inventory model", summary["inventory_models"])
    print_section("Active jobs by market count policy", summary["market_count_policies"])
    print_section("Active jobs by opportunity kind", summary["opportunity_kinds"])


def get_jobs_by_company(conn, include_experimental):
    return conn.execute(
        f"""
        SELECT {company_label("c")} AS label, COUNT(j.id) AS count
        FROM companies c
        LEFT JOIN jobs j
          ON j.company_id = c.id
         AND j.is_active = 1
         {live_market_filter("c", "j")}
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
          {live_market_filter("c", "j")}
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
          {live_market_filter("c", "j")}
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
          {live_market_filter("c", "j")}
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
