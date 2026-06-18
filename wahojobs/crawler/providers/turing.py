import json
from urllib.request import Request, urlopen

from wahojobs.crawler.types import JobCandidate


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
    page = 1
    page_size = 500

    while total is None or len(jobs) < total:
        data = fetch_page(api_url, page, page_size)
        total = int(data.get("totalCount") or 0)
        page_jobs = data.get("jobs") or []
        if not isinstance(page_jobs, list):
            raise ValueError("Turing response did not include a jobs list.")

        jobs.extend(
            parse_turing_job(job)
            for job in page_jobs
            if should_include_job(job)
        )

        if not page_jobs:
            break
        page += 1

    return jobs


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
    role_group = clean_value(job.get("roleGroup")) or "Unknown"

    return JobCandidate(
        external_id=external_id,
        title=clean_value(job.get("title")),
        location=clean_value(job.get("locationType")) or "Remote",
        url=f"https://work.turing.com/api/job/public?jobCode={external_id}",
        department=role_group,
        expertise=role_group,
        commitment=format_commitment(job),
    )


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
