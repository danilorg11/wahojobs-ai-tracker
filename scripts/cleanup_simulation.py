import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.db.connection import get_connection


SIMULATION_TITLE_PREFIX = "[SIMULATION]%"


def main():
    args = parse_args()

    with get_connection() as conn:
        plan = build_cleanup_plan(conn)
        print_plan(plan, dry_run=args.dry_run)

        if args.dry_run:
            return

        if not has_anything_to_delete(plan):
            print("Nothing to delete.")
            return

        if not confirm():
            print("Cleanup cancelled.")
            return

        delete_simulation_data(conn, plan)
        conn.commit()
        print("")
        print("Deleted simulation data.")
        print(f"Deleted job_events: {len(plan['event_ids'])}")
        print(f"Deleted jobs: {len(plan['job_ids'])}")
        print(f"Deleted crawl_runs: {len(plan['crawl_run_ids'])}")
        print("")


def parse_args():
    parser = argparse.ArgumentParser(description="Remove local Wahojobs simulation data.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be deleted without changing the database.",
    )
    return parser.parse_args()


def build_cleanup_plan(conn):
    simulation_jobs = conn.execute(
        """
        SELECT id, title
        FROM jobs
        WHERE title LIKE ?
        ORDER BY id ASC
        """,
        (SIMULATION_TITLE_PREFIX,),
    ).fetchall()
    job_ids = [row["id"] for row in simulation_jobs]

    event_ids = []
    if job_ids:
        placeholders = ",".join("?" for _ in job_ids)
        event_ids = [
            row["id"]
            for row in conn.execute(
                f"""
                SELECT id
                FROM job_events
                WHERE job_id IN ({placeholders})
                ORDER BY id ASC
                """,
                job_ids,
            ).fetchall()
        ]

    crawl_run_ids = get_simulation_only_crawl_run_ids(conn)

    return {
        "simulation_jobs": simulation_jobs,
        "job_ids": job_ids,
        "event_ids": event_ids,
        "crawl_run_ids": crawl_run_ids,
    }


def get_simulation_only_crawl_run_ids(conn):
    rows = conn.execute(
        """
        SELECT
          cr.id,
          SUM(CASE WHEN j.title LIKE ? THEN 1 ELSE 0 END) AS simulation_events,
          COUNT(je.id) AS total_events
        FROM crawl_runs cr
        JOIN job_events je ON je.crawl_run_id = cr.id
        JOIN jobs j ON j.id = je.job_id
        GROUP BY cr.id
        HAVING total_events > 0
           AND total_events = simulation_events
        ORDER BY cr.id ASC
        """,
        (SIMULATION_TITLE_PREFIX,),
    ).fetchall()
    return [row["id"] for row in rows]


def print_plan(plan, dry_run):
    mode = "DRY RUN" if dry_run else "LIVE CLEANUP"
    print("")
    print(f"{mode} - Simulation Cleanup")
    print("=" * (len(mode) + 22))
    print("Only jobs with titles starting with [SIMULATION] are targeted.")
    print("")
    print(f"job_events to delete: {len(plan['event_ids'])}")
    print(f"jobs to delete:       {len(plan['job_ids'])}")
    print(f"crawl_runs to delete: {len(plan['crawl_run_ids'])}")
    print("")

    if plan["simulation_jobs"]:
        print("Simulation jobs")
        print("---------------")
        for row in plan["simulation_jobs"]:
            print(f"  {row['id']}: {row['title']}")
        print("")

    if plan["crawl_run_ids"]:
        print("Simulation-only crawl runs")
        print("--------------------------")
        for crawl_run_id in plan["crawl_run_ids"]:
            print(f"  {crawl_run_id}")
        print("")


def has_anything_to_delete(plan):
    return bool(plan["event_ids"] or plan["job_ids"] or plan["crawl_run_ids"])


def confirm():
    try:
        answer = input("Delete this simulation data? Type DELETE to continue: ")
    except EOFError:
        return False
    return answer == "DELETE"


def delete_simulation_data(conn, plan):
    delete_by_ids(conn, "job_events", plan["event_ids"])
    delete_by_ids(conn, "jobs", plan["job_ids"])
    delete_by_ids(conn, "crawl_runs", plan["crawl_run_ids"])


def delete_by_ids(conn, table, ids):
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"DELETE FROM {table} WHERE id IN ({placeholders})",
        ids,
    )


if __name__ == "__main__":
    main()
