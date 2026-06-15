from wahojobs.crawler.providers.lever import fetch_lever_jobs
from wahojobs.crawler.types import CompanyCrawlResult


def crawl_appen(api_url):
    jobs = [
        job
        for job in fetch_lever_jobs(api_url)
        if job.external_id and job.title and job.url
    ]
    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_type="lever",
        source_message=f"Fetched live Appen jobs from Lever API: {api_url}",
    )
