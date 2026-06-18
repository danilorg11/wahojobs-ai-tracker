from wahojobs.crawler.providers.turing import fetch_turing_jobs
from wahojobs.crawler.types import CompanyCrawlResult


def crawl_turing(api_url):
    jobs = fetch_turing_jobs(api_url)
    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_type="turing-talent-api",
        source_message=f"Fetched live Turing worker-facing opportunities from API: {api_url}",
    )
