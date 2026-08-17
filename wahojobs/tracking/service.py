from datetime import datetime, timezone

from wahojobs.canonical.service import (
    sync_alignerr_canonical_opportunities,
    sync_dataforce_canonical_opportunities,
    sync_fallback_canonical_opportunities,
    sync_meridial_canonical_opportunities,
    sync_micro1_canonical_opportunities,
    sync_mindrift_canonical_opportunities,
    sync_oneforma_canonical_opportunities,
    sync_turing_canonical_opportunities,
    sync_welocalize_canonical_opportunities,
)
from wahojobs.crawler.types import (
    LEGACY_CONTRACT_WARNING,
    CompanyCrawlResult,
    ProviderOutcome,
    TrackingSummary,
    evaluate_removal_authorization,
)
from wahojobs.db.repository import (
    count_active_jobs,
    create_job_event,
    ensure_opportunity_enrichment_schema,
    get_missing_active_jobs,
    get_job_by_hash,
    insert_job,
    mark_missing_jobs_inactive,
    upsert_job_source_content,
    update_seen_job,
)
from wahojobs.matching.opportunity_trust import LIVE_FEED_MAX_AGE_HOURS
from wahojobs.opportunity_enrichment import enrich_company_opportunities
from wahojobs.opportunity_llm import tracking_openai_client
from wahojobs.tracking.normalize import with_source_hash


MINDRIFT_PARTIAL_DROP_THRESHOLD = 0.20
MINDRIFT_MIN_REMOVALS_FOR_GUARD = 50
MINDRIFT_BASELINE_SUCCESS_RUNS = 3


def track_crawl_result(conn, company_id, crawl_run_id, crawl_result: CompanyCrawlResult, now):
    ensure_opportunity_enrichment_schema(conn)
    company = conn.execute(
        "SELECT slug FROM companies WHERE id = ?",
        (company_id,),
    ).fetchone()
    if company is None:
        raise RuntimeError(f"Unknown company id: {company_id}")

    if crawl_result.outcome == ProviderOutcome.CONTRACT_DRIFT:
        return summarize_crawl_result_without_lifecycle(
            conn,
            company_id,
            crawl_result,
        )

    candidates = dedupe_candidates(
        with_source_hash(company["slug"], candidate)
        for candidate in crawl_result.jobs
    )
    seen_hashes = [candidate.source_hash for candidate in candidates]
    removal_authorization = evaluate_removal_authorization(crawl_result)
    if removal_authorization.authorized:
        guard_suspicious_mindrift_partial_crawl(
            conn,
            company["slug"],
            company_id,
            len(candidates),
            seen_hashes,
            crawl_result.used_sample_data,
            now,
        )

    jobs_new = 0
    jobs_reactivated = 0
    jobs_updated = 0

    for candidate in candidates:
        existing = get_job_by_hash(conn, company_id, candidate.source_hash)

        if existing is None:
            job_id = insert_job(conn, company_id, candidate, now)
            upsert_job_source_content(
                conn,
                job_id,
                company["slug"],
                crawl_result.source_type,
                candidate,
                now,
            )
            create_job_event(conn, job_id, crawl_run_id, "discovered", now)
            jobs_new += 1
            continue

        if existing["is_active"] == 0:
            jobs_reactivated += 1
            create_job_event(conn, existing["id"], crawl_run_id, "reactivated", now)
        else:
            jobs_updated += 1
        update_seen_job(conn, existing["id"], candidate, now)
        upsert_job_source_content(
            conn,
            existing["id"],
            company["slug"],
            crawl_result.source_type,
            candidate,
            now,
        )

    jobs_removed = 0
    if removal_authorization.authorized:
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

    # Preserve every provider-specific canonicalization above, then give only
    # the remaining non-simulation jobs conservative one-job identities.
    sync_fallback_canonical_opportunities(conn, company_id)
    enrich_company_opportunities(
        conn,
        company_id,
        llm_client=tracking_openai_client(),
    )

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
        provider_outcome=crawl_result.outcome,
        snapshot_complete=crawl_result.snapshot_complete,
        pagination_complete=crawl_result.pagination_complete,
        removals_authorized=removal_authorization.authorized,
        removal_skip_reasons=removal_authorization.skip_reasons,
        raw_record_count=crawl_result.raw_record_count,
        normalized_record_count=crawl_result.normalized_record_count,
        rejected_record_count=crawl_result.rejected_record_count,
        warnings=result_warnings(crawl_result),
        payload_shape=crawl_result.payload_shape,
        schema_fingerprint=crawl_result.schema_fingerprint,
    )


def summarize_crawl_result_without_lifecycle(conn, company_id, crawl_result):
    removal_authorization = evaluate_removal_authorization(crawl_result)
    return TrackingSummary(
        source_type=crawl_result.source_type,
        jobs_found=0,
        jobs_new=0,
        jobs_reactivated=0,
        jobs_updated=0,
        jobs_removed=0,
        active_jobs_total=count_active_jobs(conn, company_id),
        used_sample_data=crawl_result.used_sample_data,
        source_message=crawl_result.source_message,
        provider_outcome=crawl_result.outcome,
        snapshot_complete=crawl_result.snapshot_complete,
        pagination_complete=crawl_result.pagination_complete,
        removals_authorized=removal_authorization.authorized,
        removal_skip_reasons=removal_authorization.skip_reasons,
        raw_record_count=crawl_result.raw_record_count,
        normalized_record_count=crawl_result.normalized_record_count,
        rejected_record_count=crawl_result.rejected_record_count,
        warnings=result_warnings(crawl_result),
        payload_shape=crawl_result.payload_shape,
        schema_fingerprint=crawl_result.schema_fingerprint,
    )


def result_warnings(crawl_result):
    warnings = list(crawl_result.warnings)
    if (
        crawl_result.outcome == ProviderOutcome.PARTIAL
        and not crawl_result.snapshot_complete
        and not crawl_result.pagination_complete
        and not crawl_result.payload_shape
        and not crawl_result.schema_fingerprint
        and LEGACY_CONTRACT_WARNING not in warnings
    ):
        warnings.append(LEGACY_CONTRACT_WARNING)
    return tuple(warnings)


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
    now,
):
    if company_slug != "mindrift" or used_sample_data:
        return

    # A stale high-water mark is not a reliable anomaly baseline. Mindrift's
    # provider contract now verifies the source-declared total and every
    # continuation page before this guard runs, so allow that complete snapshot
    # to recover freshness. Once a fresh success exists, sharp subsequent drops
    # remain protected by the existing guard.
    if not has_fresh_mindrift_guard_baseline(conn, company_id, now):
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


def has_fresh_mindrift_guard_baseline(conn, company_id, now):
    row = conn.execute(
        """
        SELECT COALESCE(finished_at, started_at) AS observed_at
        FROM crawl_runs
        WHERE company_id = ?
          AND status = 'success'
          AND used_sample_data = 0
          AND error_message IS NULL
        ORDER BY COALESCE(finished_at, started_at) DESC, id DESC
        LIMIT 1
        """,
        (company_id,),
    ).fetchone()
    if row is None:
        return False

    observed_at = parse_utc_datetime(row["observed_at"])
    evaluated_at = parse_utc_datetime(now)
    if observed_at is None or evaluated_at is None:
        return True
    age_hours = max(
        0.0,
        (evaluated_at - observed_at).total_seconds() / 3600,
    )
    return age_hours <= LIVE_FEED_MAX_AGE_HOURS


def parse_utc_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
