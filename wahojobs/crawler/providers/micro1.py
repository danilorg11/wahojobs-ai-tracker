import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from wahojobs.crawler.types import JobCandidate


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://www.micro1.ai",
    "Referer": "https://www.micro1.ai/experts/opportunities",
}

REQUEST_BODY = {
    "action": "get_all_jobs",
    "filters": {
        "type": ["EXPERT"],
    },
}


def fetch_micro1_jobs(api_url):
    jobs = []
    total = None
    page = 1
    limit = 100

    while total is None or len(jobs) < total:
        data = fetch_page(api_url, page, limit)
        total = int(data.get("total") or 0)
        page_jobs = data.get("data") or []
        if not isinstance(page_jobs, list):
            raise ValueError("micro1 response data was not a job list.")

        jobs.extend(
            parse_micro1_job(job)
            for job in page_jobs
            if should_include_job(job)
        )

        if not page_jobs:
            break
        page += 1

    return jobs


def fetch_page(api_url, page, limit):
    query = urlencode({"page": page, "limit": limit, "keyword": ""})
    separator = "&" if "?" in api_url else "?"
    url = f"{api_url}{separator}{query}"
    body = json.dumps(REQUEST_BODY).encode("utf-8")
    request = Request(url, data=body, headers=REQUEST_HEADERS, method="POST")

    with urlopen(request, timeout=60) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset, errors="replace")

    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("micro1 response was not a JSON object.")
    if data.get("status") is not True:
        raise ValueError(f"micro1 response was not successful: {data.get('message')}")
    return data


def should_include_job(job):
    return (
        isinstance(job, dict)
        and bool(clean_value(job.get("job_id")))
        and bool(clean_value(job.get("job_name")))
        and bool(clean_value(job.get("apply_url")))
    )


def parse_micro1_job(job):
    domain = clean_value(job.get("domain_slug"))
    role_type = clean_value(job.get("role_type"))
    category = domain or role_type or "Unknown"

    return JobCandidate(
        external_id=clean_value(job.get("job_id")),
        title=clean_value(job.get("job_name")),
        location=clean_value(job.get("location_type")) or "Remote",
        url=clean_value(job.get("apply_url")),
        department=category,
        expertise=category,
        commitment=clean_value(job.get("engagement_type")),
    )


def clean_value(value):
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value or None
