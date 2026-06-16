import json
from urllib.request import Request, urlopen

from wahojobs.crawler.types import JobCandidate


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
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset, errors="replace")

    data = json.loads(payload)
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("Outlier response did not include a jobs list.")

    return [
        parse_outlier_job(job)
        for job in jobs
        if should_include_job(job)
    ]


def should_include_job(job):
    return (
        isinstance(job, dict)
        and bool(clean_value(job.get("id")))
        and bool(clean_value(job.get("title")))
    )


def parse_outlier_job(job):
    job_id = clean_value(job.get("id"))
    skill_names = clean_list(job.get("skillNames"))
    pod_group = clean_value(job.get("pod_group"))

    return JobCandidate(
        external_id=job_id,
        title=clean_value(job.get("title")),
        location=extract_location(job) or "Remote",
        url=clean_value(job.get("absolute_url"))
        or f"https://app.outlier.ai/en/expert/opportunities/{job_id}",
        department=pod_group,
        expertise=", ".join(skill_names) if skill_names else None,
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
