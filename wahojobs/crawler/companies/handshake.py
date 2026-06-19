from wahojobs.crawler.providers.handshake import fetch_handshake_jobs
from wahojobs.crawler.types import CompanyCrawlResult


def crawl_handshake(opportunities_url):
    jobs = fetch_handshake_jobs(opportunities_url)
    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_message=(
            "Fetched public Handshake AI opportunities from Framer CMS-backed "
            f"inventory: {opportunities_url}"
        ),
        source_type="framer-public-inventory",
    )
