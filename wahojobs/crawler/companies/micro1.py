from wahojobs.crawler.providers.micro1 import fetch_micro1_jobs
from wahojobs.crawler.types import CompanyCrawlResult


def crawl_micro1(api_url):
    jobs = fetch_micro1_jobs(api_url)
    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_type="micro1-marketplace",
        source_message=f"Fetched live micro1 expert opportunities from API: {api_url}",
    )
