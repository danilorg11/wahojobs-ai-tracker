import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.crawler.pipeline import run_crawl
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import initialize_database
from wahojobs.reporting.market import get_market_size_summary
from wahojobs.reporting.micro1 import get_micro1_metrics
from wahojobs.reporting.terminal import print_crawl_summary


CORE_SOURCES = ["alignerr", "appen", "meridial", "mercor", "micro1", "oneforma", "outlier"]
EXPERIMENTAL_SOURCES = ["invisible"]
EXPORT_FILES = [Path("exports/jobs.csv"), Path("exports/events.csv")]


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

    for source in sources:
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

    print_final_summary(succeeded, failed, args.include_experimental)


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


def get_market_summary(include_experimental=False):
    with get_connection() as conn:
        return (
            get_market_size_summary(conn, include_experimental=include_experimental),
            get_micro1_metrics(conn),
        )


def print_final_summary(succeeded, failed, include_experimental=False):
    market_summary, micro1_metrics = get_market_summary(include_experimental)

    print("")
    print("Final Summary")
    print("=============")
    print(f"Sources succeeded: {', '.join(succeeded) if succeeded else 'None'}")
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
    print(f"OneForma raw variants: {market_summary['oneforma_raw_variants']}")
    print(
        "OneForma canonical opportunities: "
        f"{market_summary['oneforma_canonical_opportunities']}"
    )
    print(f"OneForma posting variants: {market_summary['oneforma_posting_variants']}")
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
