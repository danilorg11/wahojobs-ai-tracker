import json
from urllib.request import Request, urlopen

from wahojobs.crawler.types import JobCandidate


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "application/json",
    "Referer": "https://www.alignerr.com/jobs",
}


def fetch_alignerr_jobs(api_url):
    request = Request(api_url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=90) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset, errors="replace")

    data = json.loads(payload)
    if not isinstance(data, list):
        raise ValueError("Alignerr response was not a jobs list.")

    return [
        parse_alignerr_job(job)
        for job in data
        if should_include_job(job)
    ]


def should_include_job(job):
    return (
        isinstance(job, dict)
        and job.get("isActive") is True
        and bool(clean_value(job.get("id")))
        and bool(clean_value(job.get("name")))
    )


def parse_alignerr_job(job):
    job_id = clean_value(job.get("id"))
    category = clean_value(job.get("category")) or "Unknown"

    return JobCandidate(
        external_id=job_id,
        title=clean_value(job.get("name")),
        location=clean_value(job.get("location")) or "Remote",
        url=clean_value(job.get("absolute_url"))
        or f"https://www.alignerr.com/jobs/{job_id}",
        department=category,
        expertise=category,
        commitment=clean_value(job.get("jobType")),
    )


def clean_value(value):
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value or None
