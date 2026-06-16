import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.crawler.pipeline import run_crawl
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import initialize_database
from wahojobs.reporting.terminal import print_crawl_summary


SOURCES = ["appen", "invisible", "meridial", "mercor"]
EXPORT_FILES = [Path("exports/jobs.csv"), Path("exports/events.csv")]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    print("")
    print("Wahojobs Daily Workflow")
    print("=======================")
    print("Simulation: excluded")
    print("Outlier: skipped by default because it is sample data")
    print("")

    print("Initializing database and seed companies...")
    initialize_database()
    print("Initialization complete.")

    succeeded = []
    failed = []

    for source in SOURCES:
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

    print_final_summary(succeeded, failed)


def run_script(script_path, title):
    print("")
    print(title)
    print("=" * len(title))
    result = subprocess.run([sys.executable, script_path], check=False)
    if result.returncode != 0:
        print(f"{title} failed with exit code {result.returncode}.")


def get_total_active_jobs():
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM jobs
            WHERE is_active = 1
              AND title NOT LIKE '[SIMULATION]%'
            """
        ).fetchone()
    return row["count"]


def print_final_summary(succeeded, failed):
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

    print(f"Total active jobs: {get_total_active_jobs()}")
    print("Export files written:")
    for path in EXPORT_FILES:
        status = "yes" if path.exists() else "no"
        print(f"  {path}: {status}")
    print("")


if __name__ == "__main__":
    main()
