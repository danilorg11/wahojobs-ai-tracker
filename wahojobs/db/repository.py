from pathlib import Path

from wahojobs.config import DB_PATH
from wahojobs.db.connection import get_connection


OUTLIER_SEED = {
    "name": "Outlier",
    "slug": "outlier",
    "careers_url": "https://outlier.ai/opportunities",
}

APPEN_SEED = {
    "name": "Appen",
    "slug": "appen",
    "careers_url": "https://api.lever.co/v0/postings/appen?mode=json&expand=location",
}


def initialize_database(db_path=DB_PATH):
    schema_path = Path(__file__).with_name("schema.sql")
    with get_connection(db_path) as conn:
        conn.executescript(schema_path.read_text(encoding="utf-8"))
        ensure_job_optional_columns(conn)
        for seed in (APPEN_SEED, OUTLIER_SEED):
            conn.execute(
                """
                INSERT INTO companies (name, slug, careers_url)
                VALUES (:name, :slug, :careers_url)
                ON CONFLICT(slug) DO UPDATE SET
                  name = excluded.name,
                  careers_url = excluded.careers_url,
                  updated_at = CURRENT_TIMESTAMP
                """,
                seed,
            )


def ensure_job_optional_columns(conn):
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "department" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN department TEXT")
    if "commitment" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN commitment TEXT")


def get_company_by_slug(conn, slug):
    return conn.execute(
        "SELECT * FROM companies WHERE slug = ?",
        (slug,),
    ).fetchone()


def create_crawl_run(conn, company_id, started_at):
    cursor = conn.execute(
        """
        INSERT INTO crawl_runs (company_id, status, started_at)
        VALUES (?, 'running', ?)
        """,
        (company_id, started_at),
    )
    return cursor.lastrowid


def finish_crawl_run(conn, crawl_run_id, summary, finished_at):
    conn.execute(
        """
        UPDATE crawl_runs
        SET status = 'success',
            finished_at = ?,
            jobs_found_count = ?,
            jobs_new_count = ?,
            jobs_reactivated_count = ?,
            jobs_updated_count = ?,
            jobs_removed_count = ?,
            used_sample_data = ?,
            error_message = NULL
        WHERE id = ?
        """,
        (
            finished_at,
            summary.jobs_found,
            summary.jobs_new,
            summary.jobs_reactivated,
            summary.jobs_updated,
            summary.jobs_removed,
            int(summary.used_sample_data),
            crawl_run_id,
        ),
    )


def fail_crawl_run(conn, crawl_run_id, error_message, finished_at):
    conn.execute(
        """
        UPDATE crawl_runs
        SET status = 'failed',
            finished_at = ?,
            error_message = ?
        WHERE id = ?
        """,
        (finished_at, error_message, crawl_run_id),
    )


def get_job_by_hash(conn, company_id, source_hash):
    return conn.execute(
        """
        SELECT * FROM jobs
        WHERE company_id = ? AND source_hash = ?
        """,
        (company_id, source_hash),
    ).fetchone()


def insert_job(conn, company_id, candidate, now):
    conn.execute(
        """
        INSERT INTO jobs (
          company_id, external_id, title, location, department, commitment, url, source_hash,
          first_seen_at, last_seen_at, is_active, removed_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, ?)
        """,
        (
            company_id,
            candidate.external_id,
            candidate.title,
            candidate.location,
            candidate.department,
            candidate.commitment,
            candidate.url,
            candidate.source_hash,
            now,
            now,
            now,
        ),
    )


def update_seen_job(conn, job_id, candidate, now):
    conn.execute(
        """
        UPDATE jobs
        SET external_id = ?,
            title = ?,
            location = ?,
            department = ?,
            commitment = ?,
            url = ?,
            last_seen_at = ?,
            is_active = 1,
            removed_at = NULL,
            updated_at = ?
        WHERE id = ?
        """,
        (
            candidate.external_id,
            candidate.title,
            candidate.location,
            candidate.department,
            candidate.commitment,
            candidate.url,
            now,
            now,
            job_id,
        ),
    )


def mark_missing_jobs_inactive(conn, company_id, seen_hashes, now):
    if seen_hashes:
        placeholders = ",".join("?" for _ in seen_hashes)
        params = [now, now, company_id, *seen_hashes]
        cursor = conn.execute(
            f"""
            UPDATE jobs
            SET is_active = 0,
                removed_at = ?,
                updated_at = ?
            WHERE company_id = ?
              AND is_active = 1
              AND source_hash NOT IN ({placeholders})
            """,
            params,
        )
    else:
        cursor = conn.execute(
            """
            UPDATE jobs
            SET is_active = 0,
                removed_at = ?,
                updated_at = ?
            WHERE company_id = ?
              AND is_active = 1
            """,
            (now, now, company_id),
        )
    return cursor.rowcount


def count_active_jobs(conn, company_id):
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM jobs
        WHERE company_id = ? AND is_active = 1
        """,
        (company_id,),
    ).fetchone()
    return row["count"]


def get_last_successful_crawl(conn, company_id):
    return conn.execute(
        """
        SELECT *
        FROM crawl_runs
        WHERE company_id = ? AND status = 'success'
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (company_id,),
    ).fetchone()
