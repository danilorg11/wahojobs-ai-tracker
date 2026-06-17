from wahojobs.crawler.providers.oneforma import fetch_oneforma_jobs
from wahojobs.crawler.types import CompanyCrawlResult


def crawl_oneforma(api_url):
    jobs = fetch_oneforma_jobs(api_url)
    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_type="oneforma-wordpress-marketplace",
        source_message=f"Fetched live OneForma opportunities from WordPress API: {api_url}",
    )
