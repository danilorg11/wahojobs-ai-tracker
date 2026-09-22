import json
from urllib.request import Request
from wahojobs.crawler.local_inventory import open_public as urlopen

from wahojobs.crawler.types import JobCandidate
from wahojobs.crawler.source_content import nonempty_metadata, selected_metadata


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "application/json",
}


def fetch_lever_jobs(api_url):
    return [parse_lever_posting(posting) for posting in fetch_lever_postings(api_url)]


def fetch_lever_postings(api_url):
    request = Request(api_url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset, errors="replace")
    postings = json.loads(payload)
    if not isinstance(postings, list):
        raise ValueError("Lever response was not a list of postings.")
    validate_postings(postings)
    return postings


def validate_postings(postings):
    """A public Lever list is one board snapshot; malformed rows cannot vanish."""
    seen = set()
    for posting in postings:
        if (not isinstance(posting, dict)
                or any(not isinstance(posting.get(k), str) or not posting[k].strip()
                       for k in ('id', 'text', 'hostedUrl'))
                or not isinstance(posting.get('categories') or {}, dict)):
            raise ValueError('Lever posting omitted required fields')
        identity = posting['id'].strip()
        if identity in seen:raise ValueError('Lever returned duplicate posting identifiers')
        seen.add(identity)


def complete_board(jobs, total, message):
    from wahojobs.crawler.types import CompanyCrawlResult, ProviderOutcome
    from wahojobs.crawler.local_inventory import record_surface_counts
    record_surface_counts(upstream_records=total, upstream_unit='Lever postings',
        variants=len(jobs), filtered=total-len(jobs))
    return CompanyCrawlResult(jobs=jobs, used_sample_data=False, source_type='lever',
        source_message=message, outcome=ProviderOutcome.SUCCESS,
        snapshot_complete=True, pagination_complete=True,
        raw_record_count=total, normalized_record_count=len(jobs),
        filtered_record_count=total-len(jobs))


def parse_lever_posting(posting):
    categories = posting.get("categories") or {}
    source_body, source_body_format = lever_source_body(posting)
    return JobCandidate(
        external_id=clean_value(posting.get("id")),
        title=clean_value(posting.get("text")),
        location=clean_value(categories.get("location")),
        url=clean_value(posting.get("hostedUrl")),
        department=clean_value(categories.get("department")),
        commitment=clean_value(categories.get("commitment")),
        source_body=source_body,
        source_body_format=source_body_format,
        source_metadata=nonempty_metadata(
            selected_metadata(
                posting,
                (
                    "additionalPlain",
                    "lists",
                    "categories",
                    "workplaceType",
                    "salaryRange",
                    "country",
                ),
            )
        ),
        source_updated_at=clean_value(posting.get("updatedAt")),
    )


def lever_source_body(posting):
    plain = posting.get("descriptionPlain")
    if isinstance(plain, str) and plain.strip():
        return plain, "text/plain"
    markup = posting.get("description")
    if isinstance(markup, str) and markup.strip():
        return markup, "text/html"
    return None, None


def lever_source_fields(posting):
    source_body, source_body_format = lever_source_body(posting)
    return {
        "source_body": source_body,
        "source_body_format": source_body_format,
        "source_metadata": nonempty_metadata(
            selected_metadata(
                posting,
                (
                    "additionalPlain",
                    "lists",
                    "categories",
                    "workplaceType",
                    "salaryRange",
                    "country",
                ),
            )
        ),
        "source_updated_at": clean_value(posting.get("updatedAt")),
    }


def clean_value(value):
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value or None
