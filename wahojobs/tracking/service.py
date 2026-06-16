from wahojobs.canonical.service import sync_alignerr_canonical_opportunities
from wahojobs.crawler.types import CompanyCrawlResult, TrackingSummary
from wahojobs.db.repository import (
    count_active_jobs,
    create_job_event,
    get_job_by_hash,
    insert_job,
    mark_missing_jobs_inactive,
    update_seen_job,
)
from wahojobs.tracking.normalize import with_source_hash


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

    jobs_new = 0
    jobs_reactivated = 0
    jobs_updated = 0
    seen_hashes = []

    for candidate in candidates:
        existing = get_job_by_hash(conn, company_id, candidate.source_hash)
        seen_hashes.append(candidate.source_hash)

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
