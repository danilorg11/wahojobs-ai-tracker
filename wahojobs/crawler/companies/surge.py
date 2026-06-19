from wahojobs.crawler.providers.surge import fetch_surge_jobs
from wahojobs.crawler.types import CompanyCrawlResult


def crawl_surge(base_url):
    jobs, workforce_count, fellowship_stored = fetch_surge_jobs(base_url)
    source_message = (
        f"Fetched Surge worker-facing workforce and fellowship pages: {base_url}; "
        f"workforce opportunities={workforce_count}; "
        f"fellowship stored={'yes' if fellowship_stored else 'no'}"
    )

    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_message=source_message,
        source_type="public-worker-pages",
    )
