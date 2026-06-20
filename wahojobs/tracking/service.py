from wahojobs.canonical.service import (
    sync_alignerr_canonical_opportunities,
    sync_dataforce_canonical_opportunities,
    sync_meridial_canonical_opportunities,
    sync_micro1_canonical_opportunities,
    sync_mindrift_canonical_opportunities,
    sync_oneforma_canonical_opportunities,
    sync_turing_canonical_opportunities,
    sync_welocalize_canonical_opportunities,
)
from wahojobs.crawler.types import CompanyCrawlResult, TrackingSummary
from wahojobs.db.repository import (
    count_active_jobs,
    create_job_event,
    get_missing_active_jobs,
    get_job_by_hash,
    insert_job,
    mark_missing_jobs_inactive,
    update_seen_job,
)
from wahojobs.tracking.normalize import with_source_hash


MINDRIFT_PARTIAL_DROP_THRESHOLD = 0.20
MINDRIFT_MIN_REMOVALS_FOR_GUARD = 50
MINDRIFT_BASELINE_SUCCESS_RUNS = 3


def track_crawl_result(conn, company_id, crawl_run_id, crawl_result: CompanyCrawlResult, now):
    company = conn.execute(
        "SELECT slug FROM companies WHERE id = ?",
        (company_id,),
    ).fetchone()
    if company is None:
        raise RuntimeError(f"Unknown company id: {company_id}")

    candidates = dedupe_candidates(
        with_source_hash(company["slug"], candidate)
        for candidate in crawl_result.jobs
    )
    seen_hashes = [candidate.source_hash for candidate in candidates]
    guard_suspicious_mindrift_partial_crawl(
        conn,
        company["slug"],
        company_id,
        len(candidates),
        seen_hashes,
        crawl_result.used_sample_data,
    )

    jobs_new = 0
    jobs_reactivated = 0
    jobs_updated = 0

    for candidate in candidates:
        existing = get_job_by_hash(conn, company_id, candidate.source_hash)

        if existing is None:
            job_id = insert_job(conn, company_id, candidate, now)
            create_job_event(conn, job_id, crawl_run_id, "discovered", now)
            jobs_new += 1
            continue

        if existing["is_active"] == 0:
            jobs_reactivated += 1
            create_job_event(conn, existing["id"], crawl_run_id, "reactivated", now)
        else:
            jobs_updated += 1
        update_seen_job(conn, existing["id"], candidate, now)

    jobs_removed = 0
    if not crawl_result.used_sample_data:
        removed_job_ids = mark_missing_jobs_inactive(conn, company_id, seen_hashes, now)
        jobs_removed = len(removed_job_ids)
        for job_id in removed_job_ids:
            create_job_event(conn, job_id, crawl_run_id, "removed", now)

    if company["slug"] == "alignerr":
        sync_alignerr_canonical_opportunities(conn, company_id)
    elif company["slug"] == "dataforce":
        sync_dataforce_canonical_opportunities(conn, company_id)
    elif company["slug"] == "meridial":
        sync_meridial_canonical_opportunities(conn, company_id)
    elif company["slug"] == "mindrift":
        sync_mindrift_canonical_opportunities(conn, company_id)
    elif company["slug"] == "micro1":
        sync_micro1_canonical_opportunities(conn, company_id)
    elif company["slug"] == "oneforma":
        sync_oneforma_canonical_opportunities(conn, company_id)
    elif company["slug"] == "turing":
        sync_turing_canonical_opportunities(conn, company_id)
    elif company["slug"] == "welocalize":
        sync_welocalize_canonical_opportunities(conn, company_id)

    active_jobs_total = count_active_jobs(conn, company_id)

    return TrackingSummary(
        source_type=crawl_result.source_type,
        jobs_found=len(candidates),
        jobs_new=jobs_new,
        jobs_reactivated=jobs_reactivated,
        jobs_updated=jobs_updated,
        jobs_removed=jobs_removed,
        active_jobs_total=active_jobs_total,
        used_sample_data=crawl_result.used_sample_data,
        source_message=crawl_result.source_message,
    )


def dedupe_candidates(candidates):
    unique = []
    seen = set()
    for candidate in candidates:
        if candidate.source_hash in seen:
            continue
        seen.add(candidate.source_hash)
        unique.append(candidate)
    return unique


def guard_suspicious_mindrift_partial_crawl(
    conn,
    company_slug,
    company_id,
    fetched_count,
    seen_hashes,
    used_sample_data,
):
    if company_slug != "mindrift" or used_sample_data:
        return

    active_count = count_active_jobs(conn, company_id)
    if active_count == 0:
        return

    baseline_count = max(
        active_count,
        get_recent_mindrift_success_high_water_mark(conn, company_id),
    )
    if baseline_count == 0:
        return

    missing_count = len(get_missing_active_jobs(conn, company_id, seen_hashes))
    drop_fraction = (baseline_count - fetched_count) / baseline_count

    # Mindrift/Workable has shown rate-limit and partial-fetch sensitivity.
    # Treat a sharp successful-looking count drop as non-authoritative so
    # missing rows are not marked removed from a likely incomplete response.
    if (
        drop_fraction > MINDRIFT_PARTIAL_DROP_THRESHOLD
        and missing_count >= MINDRIFT_MIN_REMOVALS_FOR_GUARD
    ):
        drop_percent = round(drop_fraction * 100, 1)
        raise RuntimeError(
            "Suspicious Mindrift partial crawl: "
            f"fetched {fetched_count} jobs vs {baseline_count} recent baseline "
            f"({drop_percent}% drop), with {missing_count} active jobs missing. "
            "Failing this crawl as non-authoritative to avoid false removals."
        )


def get_recent_mindrift_success_high_water_mark(conn, company_id):
    rows = conn.execute(
        """
        SELECT jobs_found_count
        FROM crawl_runs
        WHERE company_id = ?
          AND status = 'success'
          AND used_sample_data = 0
          AND jobs_found_count IS NOT NULL
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (company_id, MINDRIFT_BASELINE_SUCCESS_RUNS),
    ).fetchall()
    if not rows:
        return 0
    return max(row["jobs_found_count"] for row in rows)
