from wahojobs.crawler.providers.dataforce import fetch_dataforce_jobs
from wahojobs.crawler.types import CompanyCrawlResult


def crawl_dataforce(projects_url):
    jobs = fetch_dataforce_jobs(projects_url)
    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_type="dataforce-community-html",
        source_message=f"Fetched live DataForce Community opportunities from public projects pages: {projects_url}",
    )
