from wahojobs.crawler.providers.surge import fetch_surge_jobs
from wahojobs.crawler.types import CompanyCrawlResult, ProviderOutcome


def crawl_surge(base_url):
    jobs, workforce_count, fellowship_stored = fetch_surge_jobs(base_url)
    source_message = (
        f"Fetched Surge worker-facing workforce and fellowship pages: {base_url}; "
        f"workforce opportunities={workforce_count}; "
        f"fellowship stored={'yes' if fellowship_stored else 'no'}"
    )

    qualified = [job for job in jobs if job.record_promotion_attestation is not None]
    failed_details = workforce_count - sum(job.external_id.startswith('surge::workforce::') for job in jobs)
    return CompanyCrawlResult(
        jobs=qualified,
        used_sample_data=False,
        source_message=source_message,
        source_type="public-worker-pages",
        outcome=ProviderOutcome.PARTIAL,
        snapshot_complete=False,
        pagination_complete=False,
        raw_record_count=workforce_count+int(fellowship_stored),
        normalized_record_count=len(qualified),
        filtered_record_count=len(jobs)-len(qualified),
        rejected_record_count=failed_details,
        warnings=tuple(([f'surge_workforce_details_unqualified:{failed_details}'] if failed_details else [])
            + ([] if fellowship_stored else ['surge_fellowship_unavailable_unpublished'])),
        payload_shape='surge_remote_workforce_record_v1',
        schema_fingerprint='surge_remote_workforce_record_v1',
    )
