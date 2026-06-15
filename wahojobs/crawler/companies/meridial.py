from wahojobs.crawler.providers.greenhouse import fetch_greenhouse_jobs
from wahojobs.crawler.types import CompanyCrawlResult


def crawl_meridial(api_url):
    jobs = [
        job
        for job in fetch_greenhouse_jobs(api_url)
        if job.external_id and job.title and job.url
    ]
    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_type="greenhouse-tree",
        source_message=f"Fetched live Meridial projects from Greenhouse department tree: {api_url}",
    )
