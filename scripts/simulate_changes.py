import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wahojobs.crawler.pipeline import utc_now
from wahojobs.crawler.types import JobCandidate
from wahojobs.db.connection import get_connection
from wahojobs.db.repository import (
    create_crawl_run,
    create_job_event,
    finish_crawl_run,
    get_company_by_slug,
    insert_job,
)


class SimulationSummary:
    jobs_found = 0
    jobs_new = 0
    jobs_reactivated = 0
    jobs_updated = 0
    jobs_removed = 0
    used_sample_data = True


def main():
    now = utc_now()

    with get_connection() as conn:
        company = get_company_by_slug(conn, "meridial") or get_any_company(conn)
        if company is None:
            raise SystemExit("No companies found. Run python scripts/init_db.py first.")

        crawl_run_id = create_crawl_run(conn, company["id"], now)
        removed = remove_active_jobs(conn, crawl_run_id, now, limit=2)
        reactivated = reactivate_inactive_job(
            conn,
            crawl_run_id,
            now,
            excluded_job_ids=[job["id"] for job in removed],
        )
        discovered = insert_sample_jobs(conn, company["id"], crawl_run_id, now)

        summary = SimulationSummary()
        summary.jobs_new = len(discovered)
        summary.jobs_reactivated = 1 if reactivated else 0
        summary.jobs_removed = len(removed)
        finish_crawl_run(conn, crawl_run_id, summary, now)
        conn.commit()

    print("")
    print("SIMULATION - Wahojobs Market Changes")
    print("====================================")
    print("Local demo/testing only. No crawler was run.")
    print(f"Created simulation crawl_run_id: {crawl_run_id}")
    print("")

    print_changes("Removed jobs", removed)
    if reactivated:
        print_changes("Reactivated jobs", [reactivated])
    else:
        print_changes("Reactivated jobs", [])
    print_changes("Discovered sample jobs", discovered)

    print("Next commands:")
    print("  python scripts/daily_report.py")
    print("  python scripts/events.py --today")
    print("")


def get_any_company(conn):
    return conn.execute(
        """
        SELECT *
        FROM companies
        ORDER BY id ASC
        LIMIT 1
        """
    ).fetchone()


def remove_active_jobs(conn, crawl_run_id, now, limit):
    jobs = conn.execute(
        """
        SELECT j.id, j.title, c.name AS company_name
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 1
          AND j.title NOT LIKE '[SIMULATION]%'
        ORDER BY j.last_seen_at DESC, j.id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    for job in jobs:
        conn.execute(
            """
            UPDATE jobs
            SET is_active = 0,
                removed_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (now, now, job["id"]),
        )
        create_job_event(conn, job["id"], crawl_run_id, "removed", now)

    return jobs


def reactivate_inactive_job(conn, crawl_run_id, now, excluded_job_ids):
    excluded_sql = ""
    params = []
    if excluded_job_ids:
        placeholders = ",".join("?" for _ in excluded_job_ids)
        excluded_sql = f"AND j.id NOT IN ({placeholders})"
        params.extend(excluded_job_ids)

    job = conn.execute(
        f"""
        SELECT j.id, j.title, c.name AS company_name
        FROM jobs j
        JOIN companies c ON c.id = j.company_id
        WHERE j.is_active = 0
          {excluded_sql}
        ORDER BY j.removed_at DESC, j.id ASC
        LIMIT 1
        """,
        params,
    ).fetchone()

    if job is None:
        return None

    conn.execute(
        """
        UPDATE jobs
        SET is_active = 1,
            removed_at = NULL,
            last_seen_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (now, now, job["id"]),
    )
    create_job_event(conn, job["id"], crawl_run_id, "reactivated", now)
    return job


def insert_sample_jobs(conn, company_id, crawl_run_id, now):
    candidates = [
        make_sample_candidate(
            "simulation-ai-safety-evaluator",
            "[SIMULATION] AI Safety Evaluator - Demo Market Event",
            "Remote",
            "Safety & Evaluation",
        ),
        make_sample_candidate(
            "simulation-multilingual-data-annotator",
            "[SIMULATION] Multilingual Data Annotator - Demo Market Event",
            "Remote",
            "Language & Linguistics",
        ),
    ]

    inserted = []
    for candidate in candidates:
        existing = conn.execute(
            """
            SELECT id
            FROM jobs
            WHERE company_id = ?
              AND source_hash = ?
            """,
            (company_id, candidate.source_hash),
        ).fetchone()
        if existing:
            continue

        job_id = insert_job(conn, company_id, candidate, now)
        create_job_event(conn, job_id, crawl_run_id, "discovered", now)
        inserted.append(
            {
                "id": job_id,
                "title": candidate.title,
                "company_name": "Meridial",
            }
        )

    return inserted


def make_sample_candidate(external_id, title, location, expertise):
    source_hash = hashlib.sha256(external_id.encode("utf-8")).hexdigest()
    return JobCandidate(
        external_id=external_id,
        title=title,
        location=location,
        department=f"SIMULATION > {expertise}",
        expertise=expertise,
        commitment="Simulation",
        url=f"https://example.com/simulation/{external_id}",
        source_hash=source_hash,
    )


def print_changes(title, rows):
    print(title)
    print("-" * len(title))
    if not rows:
        print("  None")
        print("")
        return

    for row in rows:
        print(f"  {row['company_name']}: {row['title']}")
    print("")


if __name__ == "__main__":
    main()
