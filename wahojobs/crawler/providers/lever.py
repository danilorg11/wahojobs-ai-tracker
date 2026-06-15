import json
from urllib.request import Request, urlopen

from wahojobs.crawler.types import JobCandidate


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "application/json",
}


def fetch_lever_jobs(api_url):
    request = Request(api_url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset, errors="replace")
    postings = json.loads(payload)
    if not isinstance(postings, list):
        raise ValueError("Lever response was not a list of postings.")
    return [parse_lever_posting(posting) for posting in postings]


def parse_lever_posting(posting):
    categories = posting.get("categories") or {}
    return JobCandidate(
        external_id=clean_value(posting.get("id")),
        title=clean_value(posting.get("text")),
        location=clean_value(categories.get("location")),
        url=clean_value(posting.get("hostedUrl")),
        department=clean_value(categories.get("department")),
        commitment=clean_value(categories.get("commitment")),
    )


def clean_value(value):
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value or None

