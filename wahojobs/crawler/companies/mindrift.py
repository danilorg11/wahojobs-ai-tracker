from wahojobs.crawler.providers.workable_markdown import fetch_workable_jobs
from wahojobs.crawler.types import CompanyCrawlResult


ACCOUNT_SLUG = "toloka-ai"


def crawl_mindrift(api_url):
    jobs = fetch_workable_jobs(api_url, ACCOUNT_SLUG)
    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_type="workable-careers-api",
        source_message=f"Fetched live Mindrift/Toloka AI jobs from Workable API: {api_url}",
    )
