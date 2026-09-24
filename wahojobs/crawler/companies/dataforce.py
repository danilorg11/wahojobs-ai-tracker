from wahojobs.crawler.providers.dataforce import fetch_dataforce_jobs
from wahojobs.crawler.types import CompanyCrawlResult, ProviderOutcome


def crawl_dataforce(projects_url):
    jobs = fetch_dataforce_jobs(projects_url)
    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_type="dataforce-community-html",
        source_message=f"Observed DataForce project rows; terminal completeness awaits retained source proof: {projects_url}",
        outcome=ProviderOutcome.PARTIAL,
        snapshot_complete=False,
        pagination_complete=False,
        raw_record_count=len(jobs),
        normalized_record_count=len(jobs),
    )
