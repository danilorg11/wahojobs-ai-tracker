import argparse
import sys
from datetime import datetime, timedelta, timezone
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
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=args.days - 1)

    with get_connection() as conn:
        market_summary = get_market_size_summary(
            conn,
            include_experimental=args.include_experimental,
            include_simulation=args.include_simulation,
        )
        active_by_company = group_active_jobs(
            conn, "company", args.include_simulation, args.include_experimental
        )
        active_by_expertise = group_active_jobs(
            conn, "expertise", args.include_simulation, args.include_experimental
        )
        new_by_company = group_events(
            conn, start_date, end_date, "discovered", "company",
            args.include_simulation, args.include_experimental
        )
        removed_by_company = group_events(
            conn, start_date, end_date, "removed", "company",
            args.include_simulation, args.include_experimental
        )
        reactivated_by_company = group_events(
            conn, start_date, end_date, "reactivated", "company",
            args.include_simulation, args.include_experimental
        )
        new_by_expertise = group_events(
            conn, start_date, end_date, "discovered", "expertise",
            args.include_simulation, args.include_experimental
        )
        removed_by_expertise = group_events(
            conn, start_date, end_date, "removed", "expertise",
            args.include_simulation, args.include_experimental
        )
        classification_summary = get_classification_summary(
            conn,
            include_experimental=args.include_experimental,
            include_simulation=args.include_simulation,
        )
        micro1_metrics = get_micro1_metrics(conn)

    print("")
    print("Wahojobs Market Trends")
    print("======================")
    print(f"Period: {start_date.isoformat()} to {end_date.isoformat()} UTC ({args.days} days)")
    print(f"Simulation: {'included' if args.include_simulation else 'excluded'}")
    print(
        "Experimental sources: "
        f"{experimental_sources_status(args.include_experimental)}"
    )
    print("")
    print(f"Raw active postings: {market_summary['raw_active_postings']}")
    print(f"Estimated market opportunities: {market_summary['estimated_market_opportunities']}")
    print("Estimate: canonicalized sources where available; raw active jobs elsewhere.")
    print(f"Alignerr raw postings: {market_summary['alignerr_raw_postings']}")
    print(
        "Alignerr canonical opportunities: "
        f"{market_summary['alignerr_canonical_opportunities']}"
    )
    print(f"Alignerr posting variants: {market_summary['alignerr_posting_variants']}")
    print(f"DataForce raw postings: {market_summary['dataforce_raw_postings']}")
    print(
        "DataForce canonical opportunities: "
        f"{market_summary['dataforce_canonical_opportunities']}"
    )
    print(f"DataForce posting variants: {market_summary['dataforce_posting_variants']}")
    print(f"Meridial raw postings: {market_summary['meridial_raw_postings']}")
    print(
        "Meridial canonical opportunities: "
        f"{market_summary['meridial_canonical_opportunities']}"
    )
    print(f"Meridial posting variants: {market_summary['meridial_posting_variants']}")
    print(f"Mindrift raw postings: {market_summary['mindrift_raw_postings']}")
    print(
        "Mindrift canonical opportunities: "
        f"{market_summary['mindrift_canonical_opportunities']}"
    )
    print(f"Mindrift posting variants: {market_summary['mindrift_posting_variants']}")
    print(f"OneForma raw variants: {market_summary['oneforma_raw_variants']}")
    print(
        "OneForma canonical opportunities: "
        f"{market_summary['oneforma_canonical_opportunities']}"
    )
    print(f"OneForma posting variants: {market_summary['oneforma_posting_variants']}")
    print(f"Turing raw postings: {market_summary['turing_raw_postings']}")
    print(
        "Turing canonical opportunities: "
        f"{market_summary['turing_canonical_opportunities']}"
    )
    print(f"Turing posting variants: {market_summary['turing_posting_variants']}")
    print(f"Welocalize raw postings: {market_summary['welocalize_raw_postings']}")
    print(
        "Welocalize canonical opportunities: "
        f"{market_summary['welocalize_canonical_opportunities']}"
    )
    print(f"Welocalize posting variants: {market_summary['welocalize_posting_variants']}")
    print(f"micro1 active jobs: {micro1_metrics['active_jobs']}")
    print(f"micro1 unique titles: {micro1_metrics['unique_titles']}")
    print(f"micro1 duplicate-title count: {micro1_metrics['duplicate_title_count']}")
    print("")
    print_count_section("Sources by tier", classification_summary["source_tiers"])
    print_count_section(
        "Active jobs by inventory model",
        classification_summary["inventory_models"],
    )
    print_count_section(
        "Active jobs by opportunity kind",
        classification_summary["opportunity_kinds"],
    )

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
    parser.add_argument(
        "--include-experimental",
        action="store_true",
        help="Include non-core/experimental sources such as Invisible.",
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


def group_active_jobs(conn, group_by, include_simulation, include_experimental):
    label_sql = {
        "company": company_label("c"),
        "expertise": expertise_label("j"),
    }[group_by]

    return conn.execute(
        f"""
        SELECT {label_sql} AS label, COUNT(*) AS count
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          {live_market_filter("c", "j")}
          {simulation_filter("j", include_simulation)}
          {experimental_filter("c", include_experimental)}
        GROUP BY label
        ORDER BY count DESC, label ASC
        """
    ).fetchall()


def group_events(
    conn, start_date, end_date, event_type, group_by,
    include_simulation, include_experimental
):
    label_sql = {
        "company": company_label("c"),
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
          {live_market_filter("c", "j")}
          {simulation_filter("j", include_simulation)}
          {experimental_filter("c", include_experimental)}
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
