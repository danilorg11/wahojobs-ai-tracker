import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from wahojobs.crawler.types import JobCandidate
from wahojobs.crawler.source_content import first_text, nonempty_metadata, selected_metadata


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://work.turing.com",
    "Referer": "https://work.turing.com/jobs",
}


def fetch_turing_jobs(api_url):
    jobs = []
    total = None
    raw_record_count = 0
    page = 1
    page_size = 500
    seen_external_ids = set()

    while total is None or raw_record_count < total:
        data = fetch_page(api_url, page, page_size)
        page_total = validated_total_count(data)
        if total is None:
            total = page_total
        elif page_total != total:
            raise ValueError("Turing totalCount changed during pagination.")

        page_jobs = data.get("jobs")
        if not isinstance(page_jobs, list):
            raise ValueError("Turing response did not include a jobs list.")
        if not page_jobs and raw_record_count < total:
            raise ValueError("Turing pagination ended before totalCount was reached.")

        for job in page_jobs:
            raw_record_count += 1
            if not should_include_job(job):
                raise ValueError("Turing returned a job without required fields.")
            candidate = parse_turing_job(job)
            if candidate.external_id in seen_external_ids:
                raise ValueError("Turing returned a duplicate job identifier.")
            seen_external_ids.add(candidate.external_id)
            jobs.append(candidate)

        if raw_record_count > total:
            raise ValueError("Turing returned more jobs than totalCount declared.")
        page += 1

    return jobs


def validated_total_count(data):
    total = data.get("totalCount")
    if isinstance(total, bool):
        raise ValueError("Turing totalCount was not a non-negative integer.")
    try:
        total = int(total)
    except (TypeError, ValueError):
        raise ValueError(
            "Turing totalCount was not a non-negative integer."
        ) from None
    if total < 0:
        raise ValueError("Turing totalCount was not a non-negative integer.")
    return total


def fetch_page(api_url, page, page_size):
    body = json.dumps(
        {
            "searchQuery": "",
            "expertise": [],
            "location": [],
            "pageNumber": page,
            "pageSize": page_size,
            "sortingCriteria": "newest",
        }
    ).encode("utf-8")
    request = Request(api_url, data=body, headers=REQUEST_HEADERS, method="POST")

    with urlopen(request, timeout=60) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset, errors="replace")

    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("Turing response was not a JSON object.")
    if data.get("success") is not True:
        raise ValueError("Turing response was not successful.")
    return data


def should_include_job(job):
    return (
        isinstance(job, dict)
        and bool(clean_value(job.get("title")))
        and bool(clean_value(job.get("jobCode")) or clean_value(job.get("id")))
    )


def parse_turing_job(job):
    external_id = clean_value(job.get("jobCode")) or clean_value(job.get("id"))
    job_id = clean_value(job.get("id"))
    role_group = clean_value(job.get("roleGroup")) or "Unknown"
    source_body = first_text(
        job,
        ("description", "jobDescription", "descriptionText", "details"),
    )

    return JobCandidate(
        external_id=external_id,
        title=clean_value(job.get("title")),
        location=clean_value(job.get("locationType")) or "Remote",
        url=build_turing_jobs_url(job_id),
        department=role_group,
        expertise=role_group,
        commitment=format_commitment(job),
        source_body=source_body,
        source_body_format="text/plain" if source_body else None,
        source_metadata=nonempty_metadata(
            selected_metadata(
                job,
                (
                    "roleGroup",
                    "contract",
                    "skills",
                    "techStack",
                    "responsibilities",
                    "requirements",
                    "qualifications",
                ),
            )
        ),
        source_updated_at=clean_value(job.get("updatedAt")),
    )


def build_turing_jobs_url(job_id):
    if job_id:
        return f"https://work.turing.com/jobs?{urlencode({'jobId': job_id, 'search': job_id})}"
    return "https://work.turing.com/jobs"


def format_commitment(job):
    contract = job.get("contract")
    if isinstance(contract, dict):
        values = [
            clean_value(value)
            for value in contract.values()
            if isinstance(value, (str, int, float))
        ]
        values = [value for value in values if value]
        if values:
            return "; ".join(dict.fromkeys(values))
    return clean_value(contract)


def clean_value(value):
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value or None
