import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.db.connection import get_connection


SANDBOX_ERROR_MARKERS = [
    "WinError 10013",
    "proibida pelas permissões de acesso",
]


def main():
    args = parse_args()

    with get_connection() as conn:
        runs = get_failed_test_runs(conn)
        print_plan(runs, dry_run=not args.execute)

        if not args.execute:
            return

        if not runs:
            print("Nothing to delete.")
            return

        if not confirm():
            print("Cleanup cancelled.")
            return

        delete_failed_runs(conn, [row["id"] for row in runs])
        conn.commit()
        print("")
        print(f"Deleted failed test crawl_runs: {len(runs)}")
        print("")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Remove local failed crawl runs caused by sandbox/network test blocks."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Delete matching failed test crawl runs. Defaults to dry-run.",
    )
    return parser.parse_args()


def get_failed_test_runs(conn):
    rows = conn.execute(
        """
        SELECT
          cr.id,
          c.slug,
          c.name,
          cr.started_at,
          cr.finished_at,
          cr.error_message,
          cr.jobs_found_count,
          cr.jobs_new_count,
          cr.jobs_reactivated_count,
          cr.jobs_updated_count,
          cr.jobs_removed_count,
          COUNT(je.id) AS event_count
        FROM crawl_runs cr
        JOIN companies c ON c.id = cr.company_id
        LEFT JOIN job_events je ON je.crawl_run_id = cr.id
        WHERE cr.status = 'failed'
        GROUP BY cr.id
        HAVING event_count = 0
           AND cr.jobs_found_count = 0
           AND cr.jobs_new_count = 0
           AND cr.jobs_reactivated_count = 0
           AND cr.jobs_updated_count = 0
           AND cr.jobs_removed_count = 0
        ORDER BY cr.started_at ASC, cr.id ASC
        """
    ).fetchall()
    return [
        row for row in rows
        if is_sandbox_network_failure(row["error_message"] or "")
    ]


def is_sandbox_network_failure(error_message):
    return any(marker in error_message for marker in SANDBOX_ERROR_MARKERS)


def print_plan(runs, dry_run):
    mode = "DRY RUN" if dry_run else "LIVE CLEANUP"
    print("")
    print(f"{mode} - Failed Test Crawl Run Cleanup")
    print("=" * (len(mode) + 34))
    print("Only failed crawl_runs with zero events, zero job counters,")
    print("and sandbox/network permission error messages are targeted.")
    print("Successful crawl_runs are never targeted.")
    print("")
    print(f"crawl_runs to delete: {len(runs)}")
    print("")

    if runs:
        print("Failed test crawl_runs")
        print("----------------------")
        for row in runs:
            print(
                f"  {row['id']}: {row['name']} ({row['slug']}) "
                f"started {row['started_at']}"
            )
            print(f"      error: {row['error_message']}")
        print("")


def confirm():
    try:
        answer = input("Delete these failed test crawl runs? Type DELETE to continue: ")
    except EOFError:
        return False
    return answer == "DELETE"


def delete_failed_runs(conn, crawl_run_ids):
    if not crawl_run_ids:
        return
    placeholders = ",".join("?" for _ in crawl_run_ids)
    conn.execute(
        f"DELETE FROM crawl_runs WHERE id IN ({placeholders})",
        crawl_run_ids,
    )


if __name__ == "__main__":
    main()
