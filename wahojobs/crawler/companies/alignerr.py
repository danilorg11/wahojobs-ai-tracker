from wahojobs.crawler.providers.alignerr import fetch_alignerr_jobs
from wahojobs.crawler.types import CompanyCrawlResult


def crawl_alignerr(api_url):
    jobs = fetch_alignerr_jobs(api_url)
    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_type="alignerr-marketplace",
        source_message=f"Fetched live Alignerr opportunities from API: {api_url}",
    )
