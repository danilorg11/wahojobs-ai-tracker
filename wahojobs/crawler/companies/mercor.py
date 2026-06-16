from wahojobs.crawler.providers.mercor import fetch_mercor_listings
from wahojobs.crawler.types import CompanyCrawlResult


def crawl_mercor(api_url):
    jobs = fetch_mercor_listings(api_url)
    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_type="mercor-marketplace",
        source_message=f"Fetched live Mercor marketplace listings from API: {api_url}",
    )
