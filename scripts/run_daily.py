import argparse
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.crawler.pipeline import run_crawl
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import (
    get_company_by_slug,
    get_last_successful_crawl,
    initialize_database,
)
from wahojobs.reporting.market import get_market_size_summary
from wahojobs.reporting.micro1 import get_micro1_metrics
from wahojobs.reporting.terminal import print_crawl_summary


CORE_SOURCES = [
    "alignerr",
    "appen",
    "dataforce",
    "meridial",
    "mercor",
    "micro1",
    "mindrift",
    "oneforma",
    "outlier",
    "rws",
    "welocalize",
]
EXPERIMENTAL_SOURCES = ["invisible"]
EXPORT_FILES = [Path("exports/jobs.csv"), Path("exports/events.csv")]
MINDRIFT_COOLDOWN_HOURS = 12


def main():
    args = parse_args()
    sources = list(CORE_SOURCES)
    if args.include_experimental:
        sources.extend(EXPERIMENTAL_SOURCES)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    print("")
    print("Wahojobs Daily Workflow")
    print("=======================")
    print("Simulation: excluded")
    print(f"Core sources: {', '.join(CORE_SOURCES)}")
    if args.include_experimental:
        print(f"Experimental sources included: {', '.join(EXPERIMENTAL_SOURCES)}")
    else:
        print(
            "Experimental sources skipped: invisible "
            "(non-core corporate careers source)"
        )
    print("")

    print("Initializing database and seed companies...")
    initialize_database()
    print("Initialization complete.")

    succeeded = []
    failed = []
    skipped = []

    for source in sources:
        if source == "mindrift":
            last_success = get_recent_mindrift_success()
            if last_success is not None:
                print("")
                print(
                    "Skipping Mindrift: recently crawled successfully at "
                    f"{last_success.isoformat()}"
                )
                skipped.append(source)
                continue

        print("")
        print(f"Running crawler: {source}")
        print("-" * (17 + len(source)))
        try:
            company, summary = run_crawl(source)
            print_crawl_summary(company, summary)
            succeeded.append(source)
        except Exception as exc:
            print(f"FAILED: {source}")
            print(f"Error: {exc}")
            failed.append((source, str(exc)))

    run_script("scripts/stats.py", "Stats Report")
    run_script("scripts/daily_report.py", "Daily Report")
    run_script("scripts/trends.py", "Trends Report")
    run_script("scripts/export_jobs.py", "Export Jobs")
    run_script("scripts/export_events.py", "Export Events")

    print_final_summary(succeeded, failed, skipped, args.include_experimental)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run the local Wahojobs daily market tracker workflow."
    )
    parser.add_argument(
        "--include-experimental",
        action="store_true",
        help="Also run non-core/experimental sources such as Invisible.",
    )
    return parser.parse_args()


def run_script(script_path, title):
    print("")
    print(title)
    print("=" * len(title))
    result = subprocess.run([sys.executable, script_path], check=False)
    if result.returncode != 0:
        print(f"{title} failed with exit code {result.returncode}.")


def get_recent_mindrift_success():
    with get_connection() as conn:
        company = get_company_by_slug(conn, "mindrift")
        if company is None:
            return None
        crawl_run = get_last_successful_crawl(conn, company["id"])
        if crawl_run is None:
            return None

    started_at = parse_utc_datetime(crawl_run["started_at"])
    if started_at is None:
        return None
    cooldown = timedelta(hours=MINDRIFT_COOLDOWN_HOURS)
    if datetime.now(timezone.utc) - started_at < cooldown:
        return started_at
    return None


def parse_utc_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_market_summary(include_experimental=False):
    with get_connection() as conn:
        return (
            get_market_size_summary(conn, include_experimental=include_experimental),
            get_micro1_metrics(conn),
        )


def print_final_summary(succeeded, failed, skipped, include_experimental=False):
    market_summary, micro1_metrics = get_market_summary(include_experimental)

    print("")
    print("Final Summary")
    print("=============")
    print(f"Sources succeeded: {', '.join(succeeded) if succeeded else 'None'}")
    print(f"Sources skipped: {', '.join(skipped) if skipped else 'None'}")
    if failed:
        print("Sources failed:")
        for source, error in failed:
            print(f"  {source}: {error}")
    else:
        print("Sources failed: None")

    print(f"Raw active postings: {market_summary['raw_active_postings']}")
    print(
        "Estimated market opportunities: "
        f"{market_summary['estimated_market_opportunities']}"
    )
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
    print(f"Welocalize raw postings: {market_summary['welocalize_raw_postings']}")
    print(
        "Welocalize canonical opportunities: "
        f"{market_summary['welocalize_canonical_opportunities']}"
    )
    print(f"Welocalize posting variants: {market_summary['welocalize_posting_variants']}")
    print(f"micro1 active jobs: {micro1_metrics['active_jobs']}")
    print(f"micro1 unique titles: {micro1_metrics['unique_titles']}")
    print(f"micro1 duplicate-title count: {micro1_metrics['duplicate_title_count']}")
    print("Export files written:")
    for path in EXPORT_FILES:
        status = "yes" if path.exists() else "no"
        print(f"  {path}: {status}")
    print("")


if __name__ == "__main__":
    main()
