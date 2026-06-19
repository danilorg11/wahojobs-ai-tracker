from wahojobs.crawler.providers.dataannotation import fetch_dataannotation_jobs
from wahojobs.crawler.types import CompanyCrawlResult


def crawl_dataannotation(base_url):
    jobs, skipped = fetch_dataannotation_jobs(base_url)
    source_message = (
        f"Checked public DataAnnotation evergreen application pages: {base_url}"
    )
    if skipped:
        source_message += f"; skipped {len(skipped)} page(s): {', '.join(skipped)}"

    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_message=source_message,
        source_type="evergreen-application-pages",
    )
