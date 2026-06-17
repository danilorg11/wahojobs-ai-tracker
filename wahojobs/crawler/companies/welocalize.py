from wahojobs.crawler.providers.lever import clean_value, fetch_lever_postings
from wahojobs.crawler.types import CompanyCrawlResult, JobCandidate


INCLUDED_DEPARTMENT = "Welo Data - AI Services"


def crawl_welocalize(api_url):
    jobs = []
    for posting in fetch_lever_postings(api_url):
        candidate = parse_welocalize_posting(posting)
        if candidate is not None:
            jobs.append(candidate)

    return CompanyCrawlResult(
        jobs=jobs,
        used_sample_data=False,
        source_type="lever",
        source_message=f"Fetched live Welocalize AI Services jobs from Lever API: {api_url}",
    )


def parse_welocalize_posting(posting):
    categories = posting.get("categories") or {}
    if clean_value(categories.get("department")) != INCLUDED_DEPARTMENT:
        return None

    external_id = clean_value(posting.get("id"))
    title = clean_value(posting.get("text"))
    url = clean_value(posting.get("hostedUrl"))
    if not (external_id and title and url):
        return None

    category = clean_value(categories.get("team")) or clean_value(
        categories.get("department")
    )

    return JobCandidate(
        external_id=external_id,
        title=title,
        location=clean_value(categories.get("location")),
        url=url,
        department=category,
        expertise=category,
        commitment=clean_value(categories.get("commitment")),
    )
