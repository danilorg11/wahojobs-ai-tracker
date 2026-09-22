from wahojobs.crawler.providers.lever import (
    clean_value,
    complete_board,
    fetch_lever_postings,
    lever_source_fields,
)
from wahojobs.crawler.types import CompanyCrawlResult, JobCandidate, ProviderOutcome


INCLUDED_DEPARTMENT = "Welo Data - AI Services"


def crawl_welocalize(api_url):
    postings = fetch_lever_postings(api_url)
    jobs = []
    seen_external_ids = set()
    for posting in postings:
        candidate = parse_welocalize_posting(posting)
        categories = posting.get("categories") or {}
        is_in_scope = clean_value(categories.get("department")) == INCLUDED_DEPARTMENT
        if is_in_scope and candidate is None:
            raise ValueError(
                "Welocalize returned an AI Services posting without required fields."
            )
        if candidate is not None:
            if candidate.external_id in seen_external_ids:
                raise ValueError("Welocalize returned a duplicate posting identifier.")
            seen_external_ids.add(candidate.external_id)
            jobs.append(candidate)

    return complete_board(jobs, len(postings), f"Observed public Welocalize AI Services Lever board: {api_url}")


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
        **lever_source_fields(posting),
    )
