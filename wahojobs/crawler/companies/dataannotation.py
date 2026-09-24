from wahojobs.crawler.providers.dataannotation import fetch_dataannotation_jobs
from wahojobs.crawler.types import CompanyCrawlResult, ProviderOutcome


def crawl_dataannotation(base_url):
    observed, skipped = fetch_dataannotation_jobs(base_url)
    # The other domain pages are useful retained observations, but lack a
    # reviewed record contract. Do not insert or reactivate them via lifecycle.
    jobs = [job for job in observed if job.record_promotion_attestation is not None]
    source_message = (
        f"Checked public DataAnnotation evergreen application pages: {base_url}"
    )
    if skipped:
        source_message += f"; skipped {len(skipped)} page(s): {', '.join(skipped)}"

    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_message=source_message,
        source_type="evergreen-application-pages",
        outcome=ProviderOutcome.PARTIAL,
        raw_record_count=len(observed),
        normalized_record_count=len(jobs),
        filtered_record_count=len(observed) - len(jobs),
        payload_shape="dataannotation_evergreen_roles_v2",
        schema_fingerprint="dataannotation_evergreen_roles_v2",
    )
