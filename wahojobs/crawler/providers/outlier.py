import json
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from wahojobs.classification import (AVAILABILITY_BASIS_PUBLIC_FEED, OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY)
from wahojobs.crawler.types import JobCandidate
from wahojobs.crawler.source_content import first_text, nonempty_metadata, selected_metadata


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://app.outlier.ai",
    "Referer": "https://app.outlier.ai/opportunities",
}


def fetch_outlier_jobs(api_url):
    request = Request(
        api_url,
        data=b"{}",
        headers=REQUEST_HEADERS,
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"Outlier returned HTTP {response.status}.")
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset, errors="replace")

    data = json.loads(payload)
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("Outlier response did not include a jobs list.")
    if not jobs:
        raise ValueError("Outlier empty list has no validated snapshot authority.")
    if not all(should_include_job(job) for job in jobs):
        raise ValueError("Outlier response contains unsupported or nonpublic records.")
    ids = [clean_value(job["id"]) for job in jobs]
    if len(ids) != len(set(ids)):
        raise ValueError("Outlier response contains duplicate record IDs.")
    return [parse_outlier_job(job) for job in jobs]


def should_include_job(job):
    return (
        isinstance(job, dict)
        and bool(clean_value(job.get("id")))
        and bool(clean_value(job.get("title")))
        and job.get("isPublic") is True
        and valid_public_url(job.get("absolute_url"), clean_value(job.get("id")))
    )


def valid_public_url(value, job_id):
    if not isinstance(value, str) or not job_id:
        return False
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "app.outlier.ai"
        and parsed.path == f"/en/expert/opportunities/{job_id}"
        and not parsed.query and not parsed.fragment
    )


def parse_outlier_job(job):
    job_id = clean_value(job.get("id"))
    skill_names = clean_list(job.get("skillNames"))
    pod_group = clean_value(job.get("pod_group"))
    source_body = first_text(
        job,
        ("description", "jobDescription", "descriptionText", "details"),
    )

    return JobCandidate(
        external_id=job_id,
        title=clean_value(job.get("title")),
        location=extract_location(job) or "Unknown",
        url=clean_value(job.get("absolute_url")),
        department=pod_group,
        opportunity_kind=OPPORTUNITY_KIND_PUBLIC_INVENTORY_OPPORTUNITY,
        availability_basis=AVAILABILITY_BASIS_PUBLIC_FEED,
        include_in_live_market_estimate=False,
        expertise=", ".join(skill_names) if skill_names else None,
        source_body=source_body,
        source_body_format="text/plain" if source_body else None,
        source_metadata=nonempty_metadata(
            selected_metadata(
                job,
                (
                    "skillNames",
                    "pod_group",
                    "responsibilities",
                    "requirements",
                    "qualifications",
                    "pay",
                ),
            )
        ),
        source_updated_at=clean_value(job.get("updatedAt")),
    )


def extract_location(job):
    location = job.get("location")
    if isinstance(location, dict):
        return clean_value(location.get("name"))
    if isinstance(location, str):
        return clean_value(location)
    return None


def clean_list(value):
    if not isinstance(value, list):
        return []
    return [
        cleaned
        for cleaned in (clean_value(item) for item in value)
        if cleaned
    ]


def clean_value(value):
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value or None
