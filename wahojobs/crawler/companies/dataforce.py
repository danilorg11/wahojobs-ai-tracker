from wahojobs.crawler.providers.dataforce import fetch_dataforce_jobs
from wahojobs.crawler.types import CompanyCrawlResult, ProviderOutcome


def crawl_dataforce(projects_url):
    observed = fetch_dataforce_jobs(projects_url)
    # Index cards expose a preview URL, not a verified application action.
    # Keep them as source observations until a source-specific record contract
    # can attest application authority; passing them to tracking can reactivate
    # old accepted variants before a held capture is considered.
    return CompanyCrawlResult(
        jobs=[],
        used_sample_data=False,
        source_type="dataforce-community-html",
        source_message=f"Observed DataForce index rows; application and publication authority remain unverified: {projects_url}",
        outcome=ProviderOutcome.PARTIAL,
        snapshot_complete=False,
        pagination_complete=False,
        raw_record_count=len(observed),
        normalized_record_count=0,
        filtered_record_count=len(observed),
    )
