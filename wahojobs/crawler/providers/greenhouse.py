import json
from urllib.request import Request, urlopen

from wahojobs.crawler.types import JobCandidate


REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; WahojobsTracker/0.1)",
    "Accept": "application/json",
}


def fetch_greenhouse_jobs(api_url):
    request = Request(api_url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset, errors="replace")
    data = json.loads(payload)

    if is_department_tree(data):
        return parse_greenhouse_department_tree(data)

    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        raise ValueError("Greenhouse response did not include a jobs list.")
    return [parse_greenhouse_job(job) for job in jobs]


def is_department_tree(data):
    return isinstance(data, dict) and (
        isinstance(data.get("children"), list)
        or isinstance(data.get("departments"), list)
    )


def parse_greenhouse_department_tree(root):
    parsed = []

    def walk(node, path):
        node_name = clean_value(node.get("name")) if isinstance(node, dict) else None
        current_path = [*path, node_name] if node_name else path

        for job in node.get("jobs") or []:
            department_path = " > ".join(current_path) if current_path else None
            parsed.append(parse_greenhouse_job(job, department_path=department_path))

        children = []
        children.extend(node.get("children") or [])
        children.extend(node.get("departments") or [])
        for child in children:
            if isinstance(child, dict):
                walk(child, current_path)

    walk(root, [])
    return dedupe_jobs(parsed)


def parse_greenhouse_job(job, department_path=None):
    department = department_path or extract_departments(job)
    return JobCandidate(
        external_id=clean_value(job.get("id")),
        title=clean_value(job.get("title")),
        location=clean_value(extract_location(job)),
        url=clean_value(job.get("absolute_url")),
        department=clean_value(department),
        expertise=clean_value(extract_expertise(department)),
        commitment=None,
    )


def extract_location(job):
    location = job.get("location")
    if isinstance(location, dict):
        return location.get("name")
    if isinstance(location, str):
        return location
    return None


def extract_departments(job):
    departments = job.get("departments")
    if not isinstance(departments, list):
        return None
    names = [
        department.get("name")
        for department in departments
        if isinstance(department, dict) and department.get("name")
    ]
    return ", ".join(names) if names else None


def extract_expertise(department):
    if not department:
        return None
    parts = [part.strip() for part in str(department).split(">") if part.strip()]
    if len(parts) >= 2:
        return parts[1]
    if len(parts) == 1:
        return parts[0]
    return None


def dedupe_jobs(jobs):
    unique = []
    seen = set()
    for job in jobs:
        key = job.external_id or job.url
        if key in seen:
            continue
        seen.add(key)
        unique.append(job)
    return unique


def clean_value(value):
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value or None
